"""Casos de uso do AdWatch: catalogo de comerciais, midia e deteccao temporal.

Implementa a SPEC-0010 na camada de aplicacao. O passo a passo do funil de
deteccao (secao 3 da SPEC) vive aqui; a matematica de matching vive em
:mod:`lukato.domain.services.matching` e **nao** e reimplementada neste modulo.

```text
transcricao -> janelas -> filtro por keyword -> retrieval semantico (top_k)
    -> rerank lexico+semantico+ordem (top_k_rerank) -> fusao -> decisao
    -> juiz multimodal (apenas na faixa de revisao) -> supressao -> refino -> persistencia
```

Fronteira de parsing
--------------------
A camada de aplicacao **nao importa adaptadores** (SPEC-0000 secao 2, regra 2).
Os importadores de JSON (`lukato.adapters.media.importers`) sao adaptadores e
por isso rodam na camada de interface: o router HTTP recebe o corpo bruto,
converte com `TranscriptImporter`/`SceneImporter`/`OcrImporter` e entrega a
:class:`ImportTranscript`, :class:`ImportScenes` e :class:`ImportOcr` **listas ja
tipadas** (`list[TranscriptWord]`, `list[SceneCut]`, `list[OcrText]`). Os
adaptadores multimodais (probe, ASR, OCR, cenas, juiz visual) chegam pelo
`Container.media`, montado no composition root.

Degradacao
----------
Nada aqui exige rede. Sem embedder o `semantic_match` vale `0.0`; sem OCR o
`ocr_match` vale `0.0`; sem juiz visual o `visual_match` herda `speech_match` e o
candidato permanece na faixa de revisao. O teto do score sem OCR e sem juiz e
`0.85` — isto e proposital: um casamento puramente textual e forte, mas nao e
prova, e a SPEC-0010 reserva a promocao a `ACCEPTED` para quem tem evidencia
multimodal.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Final

from lukato.application.container import Container
from lukato.application.dto import DEFAULT_PAGE_LIMIT, UNSET, Maybe, Page, is_set
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import ConflictError, LukatoError, NotFoundError, ValidationError
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionCandidate,
    DetectionEvidence,
    DetectionStatus,
    MediaAsset,
    MediaKind,
    OcrText,
    SceneCut,
    Transcript,
    TranscriptWord,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.services.matching import (
    DEFAULT_MAX_SHIFT,
    DEFAULT_ORDER_THRESHOLD,
    BoundaryRefiner,
    CandidateBuilder,
    LexicalMatcher,
    NonMaximumSuppression,
    OrderMatcher,
    ScoreFusion,
    SemanticMatcher,
    SlidingWindowBuilder,
    TextWindow,
)
from lukato.domain.services.text_normalizer import ngrams, normalize, tokenize
from lukato.domain.types import Id, Json

__all__ = [
    "ADWATCH_READ",
    "ADWATCH_RUN",
    "ADWATCH_WRITE",
    "AUDIO_SUFFIX",
    "DEFAULT_COMMERCIAL_DURATION",
    "DEFAULT_LANGUAGE",
    "FRAGMENT_OVERLAP_RATIO",
    "KEY_PHRASE_SIZE",
    "MAX_DERIVED_KEYWORDS",
    "MAX_KEYWORDS",
    "MAX_KEY_PHRASES",
    "MAX_VISION_CALLS",
    "MEDIA_STATUS_ANALYZED",
    "MEDIA_STATUS_FAILED",
    "MEDIA_STATUS_INGESTED",
    "MEDIA_STATUS_REGISTERED",
    "NEUTRAL_SIMILARITY",
    "STEP_DONE",
    "STEP_FAILED",
    "STEP_SKIPPED",
    "STOPWORDS",
    "BuildFingerprint",
    "BulkImportCommercials",
    "BulkImportResult",
    "CommercialDetail",
    "CommercialFilter",
    "CommercialInput",
    "CommercialUpdateInput",
    "CreateCommercial",
    "DeleteCommercial",
    "DeleteDetections",
    "DeleteMedia",
    "DetectCommercials",
    "DetectionFilter",
    "DetectionReport",
    "GetCommercial",
    "GetCommercialByCode",
    "GetDetection",
    "GetMedia",
    "GetMediaCapabilities",
    "ImportOcr",
    "ImportScenes",
    "ImportTranscript",
    "IngestMedia",
    "IngestReport",
    "IngestStep",
    "ListCommercials",
    "ListDetections",
    "ListMedia",
    "MediaFilter",
    "MediaInput",
    "RegisterMedia",
    "ReindexCommercials",
    "ReviewDetection",
    "UpdateCommercial",
    "extract_key_phrases",
    "extract_keywords",
    "fingerprint_draft",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Permissoes
# ---------------------------------------------------------------------------
# A SPEC-0000 secao 6.7 nao define permissoes proprias do AdWatch. O mapeamento
# abaixo e o unico coerente com `ROLE_PERMISSIONS`: catalogo e midia sao
# *conteudo* (como a base de conhecimento) e executar o funil e uma *invocacao*.
# O efeito pratico e o desejado pela SPEC-0006: `viewer` so le, `operator` opera
# o pipeline inteiro e `admin`/`root` fazem tudo.
ADWATCH_READ: Final[Permission] = Permission.KNOWLEDGE_READ
"""Permissao exigida para ler catalogo, midia e deteccoes."""

ADWATCH_WRITE: Final[Permission] = Permission.KNOWLEDGE_WRITE
"""Permissao exigida para escrever catalogo, midia, artefatos e revisoes."""

ADWATCH_RUN: Final[Permission] = Permission.MODULE_INVOKE
"""Permissao exigida para executar ingestao e deteccao (etapas caras)."""


# ---------------------------------------------------------------------------
# Constantes normativas e de seguranca
# ---------------------------------------------------------------------------
DEFAULT_COMMERCIAL_DURATION: Final[float] = 30.0
"""Duracao esperada de um comercial quando o catalogo nao informa outra."""

DEFAULT_LANGUAGE: Final[str] = "pt-BR"
"""Idioma padrao do catalogo (SPEC-0000 secao 6.8)."""

DEFAULT_ASR_LANGUAGE: Final[str] = "pt"
"""Idioma padrao pedido ao ASR e gravado na transcricao."""

KEY_PHRASE_SIZE: Final[int] = 4
"""Tamanho dos n-gramas usados como `key_phrases` (SPEC-0010 secao 3.2)."""

MAX_KEY_PHRASES: Final[int] = 4
"""Quantidade maxima de `key_phrases` derivadas automaticamente."""

MAX_DERIVED_KEYWORDS: Final[int] = 10
"""Teto de termos de alta distintividade extraidos do proprio texto."""

MAX_KEYWORDS: Final[int] = 32
"""Teto total de keywords de um fingerprint (informadas + derivadas)."""

MIN_KEYWORD_CHARS: Final[int] = 4
"""Tamanho minimo de um termo para ser candidato a keyword distintiva."""

MIN_ANCHOR_CHARS: Final[int] = 2
"""Tamanho minimo de um token-ancora no refino de fronteira (`5g`, `49`)."""

NEUTRAL_SIMILARITY: Final[float] = 0.5
"""Similaridade de um par sem embedding: cosseno ortogonal reescalado."""

MAX_VISION_CALLS: Final[int] = 24
"""Teto defensivo de chamadas ao juiz multimodal por execucao.

A decisao acontece antes da supressao (SPEC-0010 secoes 3.6 e 3.7), entao uma
midia longa pode produzir centenas de candidatos na faixa de revisao. Sem teto,
uma unica deteccao dispararia centenas de chamadas caras a um modelo de visao.
Os candidatos sao ordenados por score decrescente e os excedentes permanecem em
`NEEDS_REVIEW` — nunca sao promovidos nem descartados por causa do teto.
"""

FRAGMENT_OVERLAP_RATIO: Final[float] = 0.5
"""Fracao do proprio intervalo a partir da qual um candidato e fragmento de outro.

A SPEC-0010 secao 3.7 funde candidatos do mesmo comercial "que se sobrepoem em
mais de 50% **do intervalo**" — sobreposicao medida sobre o intervalo do
candidato, e nao pela IoU simetrica que a `NonMaximumSuppression` aplica. As duas
leituras coincidem para janelas de tamanho parecido e divergem justamente no caso
que o refino cria: uma janela de 15 s dentro de uma veiculacao de 30 s fica 100%
contida na deteccao maior e ainda assim tem IoU de apenas 0.5 — o limite exato do
corte, que deixaria a mesma veiculacao persistida uma vez por tamanho de janela.
Esta passada aplica a regra literal da SPEC **depois** da supressao, sem
reimplementa-la: a fusao por IoU e a expansao para a uniao continuam sendo do
servico de dominio; aqui so se descarta o que ja esta contido em uma deteccao
melhor do mesmo comercial.
"""

_MIN_SPAN: Final[float] = 1e-9
"""Piso de duracao usado para nunca dividir por zero na razao de sobreposicao."""

AUDIO_SUFFIX: Final[str] = ".wav"
"""Extensao do audio extraido para o ASR (WAV mono 16 kHz)."""

MEDIA_STATUS_REGISTERED: Final[str] = "registered"
MEDIA_STATUS_INGESTED: Final[str] = "ingested"
MEDIA_STATUS_ANALYZED: Final[str] = "analyzed"
MEDIA_STATUS_FAILED: Final[str] = "failed"
"""Estados de `MediaAsset.status` alcancaveis por estes casos de uso."""

STEP_DONE: Final[str] = "done"
STEP_SKIPPED: Final[str] = "skipped"
STEP_FAILED: Final[str] = "failed"
"""Desfechos possiveis de cada etapa de ingestao."""

_MAX_BULK_ITEMS: Final[int] = 5_000
"""Teto de itens por importacao em lote."""

_SCORE_DIGITS: Final[int] = 6
"""Casas decimais dos scores devolvidos ao chamador."""

_TIME_DIGITS: Final[int] = 3
"""Casas decimais dos instantes devolvidos ao chamador."""

STOPWORDS: Final[frozenset[str]] = frozenset(
    [
        "a",
        "ao",
        "aos",
        "as",
        "ate",
        "com",
        "como",
        "da",
        "das",
        "de",
        "dela",
        "dele",
        "deles",
        "depois",
        "do",
        "dos",
        "e",
        "ela",
        "elas",
        "ele",
        "eles",
        "em",
        "entre",
        "era",
        "eram",
        "essa",
        "essas",
        "esse",
        "esses",
        "esta",
        "estas",
        "este",
        "estes",
        "eu",
        "foi",
        "foram",
        "ha",
        "isso",
        "isto",
        "ja",
        "la",
        "lhe",
        "mais",
        "mas",
        "me",
        "mesmo",
        "meu",
        "meus",
        "minha",
        "minhas",
        "muito",
        "na",
        "nas",
        "nem",
        "no",
        "nos",
        "nossa",
        "nosso",
        "num",
        "numa",
        "o",
        "os",
        "ou",
        "para",
        "pela",
        "pelas",
        "pelo",
        "pelos",
        "por",
        "qual",
        "quando",
        "que",
        "quem",
        "se",
        "sem",
        "ser",
        "seu",
        "seus",
        "sua",
        "suas",
        "so",
        "tambem",
        "te",
        "tem",
        "tem",
        "teu",
        "tu",
        "um",
        "uma",
        "umas",
        "uns",
        "voce",
        "voces",
        "vos",
        "e",
        "nao",
        "sim",
    ]
)
"""Palavras vazias do portugues: nunca viram keyword, ancora ou frase-chave."""

_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"r\$\s*\d+(?:[.,]\d+)*", re.IGNORECASE)
"""Valores em reais (`R$ 49,90`), preservados como keyword unica."""

_MEASURE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:gbps|mbps|kbps|gb|mb|tb|kb|min|minutos?|seg|segundos?|"
    r"h|horas?|dias?|meses|mes|reais|%)\b",
    re.IGNORECASE,
)
"""Numeros com unidade (`10GB`, `30 min`, `12 meses`)."""

_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b\d+(?:[.,]\d+)?[a-z]{0,4}\b", re.IGNORECASE
)
"""Numeros soltos e numeros colados a um sufixo curto (`5G`, `4K`, `49`)."""


# ---------------------------------------------------------------------------
# Funcoes puras de fingerprint (SPEC-0010 secao 3.2)
# ---------------------------------------------------------------------------
def _dedupe(values: Iterable[str]) -> list[str]:
    """Remove duplicatas pela forma normalizada, preservando a ordem de chegada."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        candidate = " ".join(str(value).split())
        key = normalize(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _distinctiveness(token: str, occurrences: int, total: int) -> float:
    """IDF local aproximado: termo longo e raro no proprio texto pontua mais."""
    rarity = math.log(1.0 + total / max(1, occurrences))
    return rarity * math.log(1.0 + len(token))


def _distinctive_terms(tokens: Sequence[str], limit: int) -> list[str]:
    """Termos de maior distintividade do texto, em ordem de aparicao."""
    if not tokens or limit <= 0:
        return []
    counts: dict[str, int] = {}
    position: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if len(token) < MIN_KEYWORD_CHARS or token in STOPWORDS or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
        position.setdefault(token, index)
    if not counts:
        return []
    total = len(tokens)
    ranked = sorted(
        counts,
        key=lambda token: (-_distinctiveness(token, counts[token], total), position[token]),
    )
    chosen = ranked[:limit]
    return sorted(chosen, key=lambda token: position[token])


def extract_keywords(text: str, informed: Sequence[str] = ()) -> list[str]:
    """Keywords do fingerprint: informadas + numeros, valores, unidades e termos raros.

    A saida sai **na ordem em que os termos aparecem no texto** (os informados que
    nao aparecem literalmente vao para o fim). Essa ordem importa: quando o
    comercial nao tem `key_phrases`, o `OrderMatcher` usa as keywords como
    sequencia-ancora esperada, e uma lista fora de ordem penalizaria casamentos
    legitimos.
    """
    derived: list[str] = []
    for pattern in (_CURRENCY_PATTERN, _MEASURE_PATTERN, _NUMBER_PATTERN):
        derived.extend(match.group(0) for match in pattern.finditer(text))
    derived.extend(_distinctive_terms(tokenize(text), MAX_DERIVED_KEYWORDS))

    haystack = f" {normalize(text)} "
    ordered = _dedupe([*informed, *derived])
    # Presentes no texto primeiro (na ordem do texto); ausentes depois, na ordem de chegada.
    ranked = sorted(
        enumerate(ordered),
        key=lambda entry: (
            (0, position, 0)
            if (position := haystack.find(f" {normalize(entry[1])} ")) >= 0
            else (1, 0, entry[0])
        ),
    )
    return [keyword for _, keyword in ranked[:MAX_KEYWORDS]]


def extract_key_phrases(text: str, informed: Sequence[str] = ()) -> list[str]:
    """Frases-chave: as informadas ou os 4-gramas mais distintivos, em ordem de texto."""
    if informed:
        return _dedupe(informed)[:MAX_KEY_PHRASES]
    tokens = tokenize(text)
    grams = ngrams(tokens, KEY_PHRASE_SIZE)
    if not grams:
        return []
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    total = len(tokens)

    def weight(gram: tuple[str, ...]) -> float:
        return math.fsum(
            _distinctiveness(token, counts.get(token, 1), total)
            for token in gram
            if token not in STOPWORDS
        )

    scored = [(index, gram, weight(gram)) for index, gram in enumerate(grams)]
    useful = [item for item in scored if item[2] > 0.0] or scored
    best = sorted(useful, key=lambda item: (-item[2], item[0]))[:MAX_KEY_PHRASES]
    return [" ".join(gram) for _, gram, _ in sorted(best, key=lambda item: item[0])]


def fingerprint_draft(commercial: Commercial) -> AdFingerprint:
    """Monta a assinatura do comercial **sem** o embedding (parte pura da secao 3.2)."""
    normalized = normalize(commercial.text)
    if not normalized:
        raise ValidationError(
            "o texto do comercial nao produz conteudo comparavel apos a normalizacao",
            details={"commercial_id": commercial.commercial_id},
        )
    return AdFingerprint(
        commercial_id=commercial.id,
        normalized_text=normalized,
        token_set=sorted(set(tokenize(commercial.text))),
        keywords=extract_keywords(commercial.text, commercial.keywords),
        key_phrases=extract_key_phrases(commercial.text, commercial.key_phrases),
        embedding=None,
        duration=float(commercial.duration_expected),
        expected_brand=commercial.brand,
    )


def _anchor_vocabulary(fingerprint: AdFingerprint) -> frozenset[str]:
    """Tokens que ancoram o comercial no tempo (usados no refino de fronteira)."""
    tokens: set[str] = set()
    for source in (fingerprint.key_phrases, fingerprint.keywords, fingerprint.token_set):
        for item in source:
            for token in tokenize(item):
                if len(token) >= MIN_ANCHOR_CHARS and token not in STOPWORDS:
                    tokens.add(token)
    return frozenset(tokens)


# ---------------------------------------------------------------------------
# Validacao de entrada
# ---------------------------------------------------------------------------
def _require_value(value: str, field_name: str) -> str:
    """Exige um texto nao vazio."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValidationError(
            f"o campo '{field_name}' e obrigatorio e nao pode ser vazio",
            details={"field": field_name},
        )
    return candidate


def _positive(value: float, field_name: str) -> float:
    """Exige um numero estritamente positivo e finito."""
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValidationError(
            f"o campo '{field_name}' deve ser um numero positivo",
            details={"field": field_name, "value": value},
        )
    return number


def _round_score(value: float) -> float:
    """Arredonda um score para a precisao publicada."""
    return round(float(value), _SCORE_DIGITS)


def _round_time(value: float) -> float:
    """Arredonda um instante para a precisao publicada."""
    return round(float(value), _TIME_DIGITS)


# ---------------------------------------------------------------------------
# DTOs de catalogo
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CommercialInput:
    """Dados de criacao de um comercial do catalogo."""

    commercial_id: str
    campaign: str = ""
    brand: str = ""
    text: str = ""
    duration_expected: float = DEFAULT_COMMERCIAL_DURATION
    keywords: Sequence[str] = ()
    key_phrases: Sequence[str] = ()
    language: str = DEFAULT_LANGUAGE
    is_active: bool = True
    metadata: Json = field(default_factory=dict)

    def to_commercial(self) -> Commercial:
        """Converte para a entidade de dominio ja validada."""
        return Commercial(
            commercial_id=_require_value(self.commercial_id, "commercial_id"),
            campaign=(self.campaign or "").strip(),
            brand=(self.brand or "").strip(),
            text=_require_value(self.text, "text"),
            duration_expected=_positive(self.duration_expected, "duration_expected"),
            keywords=_dedupe(self.keywords),
            key_phrases=_dedupe(self.key_phrases),
            language=(self.language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE,
            is_active=bool(self.is_active),
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True, slots=True)
class CommercialUpdateInput:
    """Atualizacao parcial de um comercial; campos ausentes ficam :data:`UNSET`."""

    commercial_id: Maybe[str] = UNSET
    campaign: Maybe[str] = UNSET
    brand: Maybe[str] = UNSET
    text: Maybe[str] = UNSET
    duration_expected: Maybe[float] = UNSET
    keywords: Maybe[Sequence[str]] = UNSET
    key_phrases: Maybe[Sequence[str]] = UNSET
    language: Maybe[str] = UNSET
    is_active: Maybe[bool] = UNSET
    metadata: Maybe[Json] = UNSET

    def changes(self) -> Json:
        """Mapa `campo -> valor` apenas com o que foi efetivamente informado."""
        raw: dict[str, Maybe[Any]] = {
            "commercial_id": self.commercial_id,
            "campaign": self.campaign,
            "brand": self.brand,
            "text": self.text,
            "duration_expected": self.duration_expected,
            "keywords": self.keywords,
            "key_phrases": self.key_phrases,
            "language": self.language,
            "is_active": self.is_active,
            "metadata": self.metadata,
        }
        changed: Json = {}
        for name, value in raw.items():
            if not is_set(value):
                continue
            if name == "commercial_id" or name == "text":
                changed[name] = _require_value(str(value), name)
            elif name == "duration_expected":
                changed[name] = _positive(float(value), name)
            elif name in {"keywords", "key_phrases"}:
                changed[name] = _dedupe(value)
            elif name == "metadata":
                changed[name] = dict(value)
            elif name in {"campaign", "brand", "language"}:
                changed[name] = str(value).strip()
            else:
                changed[name] = bool(value)
        return changed


@dataclass(frozen=True, slots=True)
class CommercialFilter:
    """Filtros de listagem do catalogo (SPEC-0010 secao 5)."""

    search: str | None = None
    brand: str | None = None
    campaign: str | None = None
    is_active: bool | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def criteria(self) -> Json:
        """Filtros nao vazios, no formato aceito pelo repositorio."""
        found: Json = {}
        if self.search:
            found["search"] = self.search
        if self.brand:
            found["brand"] = self.brand
        if self.campaign:
            found["campaign"] = self.campaign
        if self.is_active is not None:
            found["is_active"] = self.is_active
        return found


@dataclass(frozen=True, slots=True)
class CommercialDetail:
    """Comercial acompanhado da sua assinatura (`GET /commercials/{id}`)."""

    commercial: Commercial
    fingerprint: AdFingerprint | None = None

    def to_dict(self) -> Json:
        """Serializa comercial e assinatura para a borda HTTP."""
        return {
            "commercial": self.commercial.model_dump(mode="json"),
            "fingerprint": (
                None if self.fingerprint is None else self.fingerprint.model_dump(mode="json")
            ),
        }


@dataclass(frozen=True, slots=True)
class BulkImportResult:
    """Resultado de uma importacao em lote do catalogo."""

    created: list[Commercial] = field(default_factory=list)
    updated: list[Commercial] = field(default_factory=list)
    skipped: list[Json] = field(default_factory=list)
    errors: list[Json] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Quantidade de itens processados com desfecho conhecido."""
        return len(self.created) + len(self.updated) + len(self.skipped) + len(self.errors)

    def to_dict(self) -> Json:
        """Serializa o resultado para a borda HTTP."""
        return {
            "created": [item.model_dump(mode="json") for item in self.created],
            "updated": [item.model_dump(mode="json") for item in self.updated],
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# DTOs de midia
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MediaInput:
    """Dados de registro de um ativo de midia."""

    uri: str
    kind: MediaKind = MediaKind.VIDEO
    title: str = ""
    duration_seconds: float = 0.0
    fps: float = 0.0
    metadata: Json = field(default_factory=dict)

    def to_asset(self) -> MediaAsset:
        """Converte para a entidade de dominio ja validada."""
        return MediaAsset(
            uri=_require_value(self.uri, "uri"),
            kind=MediaKind(self.kind),
            title=(self.title or "").strip(),
            duration_seconds=max(0.0, float(self.duration_seconds)),
            fps=max(0.0, float(self.fps)),
            status=MEDIA_STATUS_REGISTERED,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True, slots=True)
class MediaFilter:
    """Filtros de listagem de ativos de midia."""

    status: str | None = None
    search: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def criteria(self) -> Json:
        """Filtros nao vazios, no formato aceito pelo repositorio."""
        found: Json = {}
        if self.status:
            found["status"] = self.status
        if self.search:
            found["search"] = self.search
        return found


@dataclass(frozen=True, slots=True)
class IngestStep:
    """Desfecho de uma etapa da ingestao (executada, pulada ou falha)."""

    name: str
    status: str
    detail: str = ""
    items: int = 0

    def to_dict(self) -> Json:
        """Serializa a etapa para a borda HTTP e para `MediaAsset.metadata`."""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "items": self.items,
        }


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Relatorio da ingestao: o que foi alcancado e o que foi pulado."""

    media_id: Id
    status: str
    steps: list[IngestStep] = field(default_factory=list)
    duration_seconds: float = 0.0
    fps: float = 0.0
    elapsed_ms: float = 0.0

    @property
    def completed(self) -> list[str]:
        """Nomes das etapas efetivamente executadas."""
        return [step.name for step in self.steps if step.status == STEP_DONE]

    @property
    def skipped(self) -> list[str]:
        """Nomes das etapas puladas por indisponibilidade de adaptador."""
        return [step.name for step in self.steps if step.status == STEP_SKIPPED]

    @property
    def failed(self) -> list[str]:
        """Nomes das etapas que falharam apesar do adaptador estar disponivel."""
        return [step.name for step in self.steps if step.status == STEP_FAILED]

    def to_dict(self) -> Json:
        """Serializa o relatorio para a borda HTTP."""
        return {
            "media_id": self.media_id,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "duration_seconds": _round_time(self.duration_seconds),
            "fps": self.fps,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


# ---------------------------------------------------------------------------
# DTOs de deteccao
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DetectionFilter:
    """Filtros de consulta de deteccoes."""

    media_id: Id | None = None
    commercial_id: Id | None = None
    status: DetectionStatus | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def criteria(self) -> Json:
        """Filtros nao vazios, no formato aceito pelo repositorio."""
        found: Json = {}
        if self.media_id:
            found["media_id"] = self.media_id
        if self.commercial_id:
            found["commercial_id"] = self.commercial_id
        if self.status is not None:
            found["status"] = self.status
        return found


@dataclass(frozen=True, slots=True)
class DetectionReport:
    """Resultado de uma execucao do funil de deteccao sobre uma midia."""

    media_id: Id
    media_uri: str = ""
    detections: list[Detection] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    windows: int = 0
    candidates: int = 0
    commercials: int = 0
    persisted: int = 0
    replaced: int = 0
    scene_cuts: int = 0
    ocr_texts: int = 0
    vision_calls: int = 0
    vision_available: bool = False
    semantic_enabled: bool = True
    keep_rejected: bool = False
    elapsed_ms: float = 0.0

    @property
    def accepted(self) -> int:
        """Quantidade de deteccoes aceitas."""
        return self.counts.get(DetectionStatus.ACCEPTED.value, 0)

    @property
    def needs_review(self) -> int:
        """Quantidade de deteccoes encaminhadas a revisao."""
        return self.counts.get(DetectionStatus.NEEDS_REVIEW.value, 0)

    @property
    def rejected(self) -> int:
        """Quantidade de candidatos rejeitados."""
        return self.counts.get(DetectionStatus.REJECTED.value, 0)

    def to_dict(self) -> Json:
        """Serializa o relatorio para a borda HTTP."""
        return {
            "media_id": self.media_id,
            "media_uri": self.media_uri,
            "detections": [item.model_dump(mode="json") for item in self.detections],
            "counts": dict(self.counts),
            "windows": self.windows,
            "candidates": self.candidates,
            "commercials": self.commercials,
            "persisted": self.persisted,
            "replaced": self.replaced,
            "scene_cuts": self.scene_cuts,
            "ocr_texts": self.ocr_texts,
            "vision_calls": self.vision_calls,
            "vision_available": self.vision_available,
            "semantic_enabled": self.semantic_enabled,
            "keep_rejected": self.keep_rejected,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


# ---------------------------------------------------------------------------
# Resolucao de entidades
# ---------------------------------------------------------------------------
async def _require_commercial(uow: UnitOfWork, reference: Id) -> Commercial:
    """Resolve o comercial por identificador interno e, depois, por codigo de negocio."""
    candidate = (reference or "").strip()
    found = await uow.commercials.get(candidate) if candidate else None
    if found is None and candidate:
        found = await uow.commercials.get_by_code(candidate)
    if found is None:
        raise NotFoundError(
            f"Comercial '{reference}' nao encontrado.", details={"commercial_id": reference}
        )
    return found


async def _require_media(uow: UnitOfWork, media_id: Id) -> MediaAsset:
    """Resolve o ativo de midia ou levanta :class:`NotFoundError`."""
    found = await uow.media.get((media_id or "").strip())
    if found is None:
        raise NotFoundError(f"Midia '{media_id}' nao encontrada.", details={"media_id": media_id})
    return found


async def _require_detection(uow: UnitOfWork, detection_id: Id) -> Detection:
    """Resolve a deteccao ou levanta :class:`NotFoundError`."""
    found = await uow.detections.get((detection_id or "").strip())
    if found is None:
        raise NotFoundError(
            f"Deteccao '{detection_id}' nao encontrada.", details={"detection_id": detection_id}
        )
    return found


# ---------------------------------------------------------------------------
# Base dos casos de uso
# ---------------------------------------------------------------------------
class _AdWatchUseCase:
    """Base dos casos de uso do AdWatch: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float] | None]:
        """Embedda textos em lote; devolve `None` por item quando o provedor falha.

        Embedding e sinal, nao pre-requisito: um provedor fora do ar reduz o
        `semantic_match` a zero e o funil continua rodando com os demais sinais
        (SPEC-0000 secao 14, fallback obrigatorio).
        """
        if not texts:
            return []
        size = max(1, int(self._container.settings.embedding.batch_size))
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), size):
                vectors.extend(await self._container.embeddings.embed(texts[start : start + size]))
        except LukatoError as exc:
            _logger.warning(
                "adwatch_embedding_degraded",
                error=f"{type(exc).__name__}: {exc}",
                texts=len(texts),
            )
            return [None] * len(texts)
        if len(vectors) != len(texts):
            _logger.warning(
                "adwatch_embedding_size_mismatch", expected=len(texts), received=len(vectors)
            )
        found: list[list[float] | None] = [None] * len(texts)
        for index, vector in enumerate(vectors[: len(texts)]):
            found[index] = list(vector) if vector else None
        return found


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------
class BuildFingerprint(_AdWatchUseCase):
    """Constroi a `AdFingerprint` de um comercial (SPEC-0010 secao 3.2).

    A parte deterministica (normalizacao, tokens, keywords, frases-chave) e pura
    e vive em :func:`fingerprint_draft`; aqui entra apenas o embedding, que
    depende da porta `EmbeddingPort` e por isso e assincrono.
    """

    async def execute(self, commercial: Commercial) -> AdFingerprint:
        """Assinatura de um unico comercial."""
        built = await self.many([commercial])
        return built[0]

    async def many(self, commercials: Sequence[Commercial]) -> list[AdFingerprint]:
        """Assinaturas de varios comerciais, com os embeddings pedidos em lote."""
        drafts = [fingerprint_draft(commercial) for commercial in commercials]
        if not drafts:
            return []
        vectors = await self.embed_batch([draft.normalized_text for draft in drafts])
        for draft, vector in zip(drafts, vectors, strict=True):
            draft.embedding = vector
        return drafts


# ---------------------------------------------------------------------------
# CRUD do catalogo
# ---------------------------------------------------------------------------
class CreateCommercial(_AdWatchUseCase):
    """Cria um comercial e a sua assinatura de matching."""

    async def execute(self, data: CommercialInput, principal: Principal) -> Commercial:
        """Grava o comercial; codigo duplicado levanta :class:`ConflictError`."""
        authorize(principal, ADWATCH_WRITE, "criar comerciais")
        commercial = data.to_commercial()
        async with self._container.uow_factory() as uow:
            existing = await uow.commercials.get_by_code(commercial.commercial_id)
            if existing is not None:
                raise ConflictError(
                    f"Ja existe um comercial com o codigo '{commercial.commercial_id}'.",
                    details={"commercial_id": commercial.commercial_id, "id": existing.id},
                )
            stored = await uow.commercials.add(commercial)
            await uow.commit()
        fingerprint = await BuildFingerprint(self._container).execute(stored)
        async with self._container.uow_factory() as uow:
            await uow.commercials.upsert_fingerprint(fingerprint)
            await uow.commit()
        _logger.info(
            "commercial_created",
            commercial_id=stored.id,
            code=stored.commercial_id,
            keywords=len(fingerprint.keywords),
            key_phrases=len(fingerprint.key_phrases),
            embedded=fingerprint.embedding is not None,
        )
        return stored


class GetCommercial(_AdWatchUseCase):
    """Busca um comercial pelo identificador interno (ou pelo codigo de negocio)."""

    async def execute(self, commercial_id: Id, principal: Principal) -> Commercial:
        """Devolve o comercial; ausente levanta :class:`NotFoundError`."""
        authorize(principal, ADWATCH_READ, "ler comerciais")
        async with self._container.uow_factory() as uow:
            return await _require_commercial(uow, commercial_id)

    async def detail(self, commercial_id: Id, principal: Principal) -> CommercialDetail:
        """Devolve o comercial junto da sua assinatura (`GET /commercials/{id}`)."""
        authorize(principal, ADWATCH_READ, "ler comerciais")
        async with self._container.uow_factory() as uow:
            commercial = await _require_commercial(uow, commercial_id)
            fingerprint = await uow.commercials.get_fingerprint(commercial.id)
        return CommercialDetail(commercial=commercial, fingerprint=fingerprint)


class GetCommercialByCode(_AdWatchUseCase):
    """Busca um comercial pelo codigo de negocio (`COM_000234`)."""

    async def execute(self, code: str, principal: Principal) -> Commercial:
        """Devolve o comercial; ausente levanta :class:`NotFoundError`."""
        authorize(principal, ADWATCH_READ, "ler comerciais")
        wanted = _require_value(code, "commercial_id")
        async with self._container.uow_factory() as uow:
            found = await uow.commercials.get_by_code(wanted)
        if found is None:
            raise NotFoundError(
                f"Comercial de codigo '{wanted}' nao encontrado.", details={"commercial_id": wanted}
            )
        return found


class ListCommercials(_AdWatchUseCase):
    """Lista o catalogo com filtros e paginacao."""

    async def execute(self, filters: CommercialFilter, principal: Principal) -> Page[Commercial]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, ADWATCH_READ, "listar comerciais")
        criteria = filters.criteria()
        async with self._container.uow_factory() as uow:
            items = await uow.commercials.list(
                **criteria, limit=filters.limit, offset=filters.offset
            )
            total = await uow.commercials.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class UpdateCommercial(_AdWatchUseCase):
    """Atualiza um comercial; muda o texto, muda a assinatura."""

    async def execute(
        self, commercial_id: Id, data: CommercialUpdateInput, principal: Principal
    ) -> Commercial:
        """Grava as alteracoes e regera o fingerprint quando o matching e afetado."""
        authorize(principal, ADWATCH_WRITE, "atualizar comerciais")
        changes = data.changes()
        async with self._container.uow_factory() as uow:
            current = await _require_commercial(uow, commercial_id)
            code = changes.get("commercial_id")
            if code and code != current.commercial_id:
                clash = await uow.commercials.get_by_code(code)
                if clash is not None and clash.id != current.id:
                    raise ConflictError(
                        f"Ja existe um comercial com o codigo '{code}'.",
                        details={"commercial_id": code, "id": clash.id},
                    )
            updated = current.model_copy(update=changes) if changes else current
            if changes:
                updated.touch()
                updated = await uow.commercials.update(updated)
                await uow.commit()

        if not self._affects_matching(changes):
            return updated

        fingerprint = await BuildFingerprint(self._container).execute(updated)
        async with self._container.uow_factory() as uow:
            previous = await uow.commercials.get_fingerprint(updated.id)
            if previous is not None:
                fingerprint = fingerprint.model_copy(
                    update={"id": previous.id, "created_at": previous.created_at}
                )
            await uow.commercials.upsert_fingerprint(fingerprint)
            await uow.commit()
        _logger.info(
            "commercial_fingerprint_rebuilt",
            commercial_id=updated.id,
            code=updated.commercial_id,
            fields=sorted(changes),
        )
        return updated

    @staticmethod
    def _affects_matching(changes: Mapping[str, Any]) -> bool:
        """True quando alguma alteracao muda o que o motor de matching compara."""
        return bool(
            changes.keys() & {"text", "keywords", "key_phrases", "duration_expected", "brand"}
        )


class ReindexCommercials(_AdWatchUseCase):
    """Reconstroi as assinaturas de TODOS os comerciais com o embedder ATUAL.

    A assinatura carrega um embedding, e embedding so compara com embedding do
    MESMO espaco: um catalogo assinado pelo `HashingEmbedder` (o modo offline) e
    consultado pelo Qwen depois de a rede voltar produz similaridades sem
    significado — o `semantic_match` nao zera nem grita, ele devolve numeros
    errados em silencio. E exatamente o que acontece ao importar um catalogo com
    um embedder e operar com outro.

    Trocou o provedor de embeddings, rode isto. A parte deterministica da
    assinatura (tokens, keywords, frases) e recalculada junto, de graca — ela e
    pura e barata; o custo real e o lote de embeddings.
    """

    async def execute(self, principal: Principal, *, page_size: int = 200) -> Json:
        """Reassina o catalogo inteiro em lotes; devolve o placar da varredura.

        `com_embedding`/`sem_embedding` sao o resultado que interessa: um provedor
        fora do ar nao derruba a varredura (SPEC-0000 secao 14) — os comerciais
        que ficaram sem vetor sao contados e nomeados no log, e uma nova rodada
        com o provedor de pe completa o que faltou.
        """
        authorize(principal, ADWATCH_WRITE, "reindexar as assinaturas do catalogo")
        builder = BuildFingerprint(self._container)
        total = 0
        embedded = 0
        offset = 0
        sem_vetor: list[str] = []
        while True:
            async with self._container.uow_factory() as uow:
                lote = await uow.commercials.list(limit=page_size, offset=offset)
            if not lote:
                break
            fingerprints = await builder.many(lote)
            async with self._container.uow_factory() as uow:
                for commercial, fingerprint in zip(lote, fingerprints, strict=True):
                    await uow.commercials.upsert_fingerprint(fingerprint)
                    total += 1
                    if fingerprint.embedding is not None:
                        embedded += 1
                    else:
                        sem_vetor.append(commercial.commercial_id)
                await uow.commit()
            offset += page_size
        _logger.info(
            "commercials_reindexed",
            total=total,
            embedded=embedded,
            without_embedding=len(sem_vetor),
            missing=sem_vetor[:20],
            model=str(self._container.embeddings.model),
        )
        return {
            "total": total,
            "com_embedding": embedded,
            "sem_embedding": len(sem_vetor),
            "faltantes": sem_vetor,
            "model": str(self._container.embeddings.model),
        }


class DeleteCommercial(_AdWatchUseCase):
    """Remove um comercial e, em cascata, a sua assinatura."""

    async def execute(self, commercial_id: Id, principal: Principal) -> None:
        """Apaga o comercial; ausente levanta :class:`NotFoundError`."""
        authorize(principal, ADWATCH_WRITE, "remover comerciais")
        async with self._container.uow_factory() as uow:
            commercial = await _require_commercial(uow, commercial_id)
            await uow.commercials.delete(commercial.id)
            await uow.commit()
        _logger.info(
            "commercial_deleted", commercial_id=commercial.id, code=commercial.commercial_id
        )


class BulkImportCommercials(_AdWatchUseCase):
    """Importa um lote de comerciais sem deixar um item ruim derrubar o resto.

    Cada item usa a sua propria unidade de trabalho: uma falha de validacao ou de
    conflito fica isolada no item, e os demais continuam sendo gravados. Os
    embeddings sao pedidos **uma unica vez**, em lote, ao fim da importacao.
    """

    async def execute(
        self,
        items: Sequence[CommercialInput],
        principal: Principal,
        *,
        update_existing: bool = False,
    ) -> BulkImportResult:
        """Grava o lote e devolve criados, atualizados, pulados e erros."""
        authorize(principal, ADWATCH_WRITE, "importar comerciais em lote")
        if len(items) > _MAX_BULK_ITEMS:
            raise ValidationError(
                f"a importacao em lote aceita no maximo {_MAX_BULK_ITEMS} itens por chamada",
                details={"received": len(items), "max": _MAX_BULK_ITEMS},
            )
        result = BulkImportResult()
        touched: list[Commercial] = []
        for index, item in enumerate(items):
            try:
                stored, created = await self._store(item, update_existing=update_existing)
            except ConflictError as exc:
                result.skipped.append(
                    {"index": index, "commercial_id": item.commercial_id, "reason": exc.code}
                )
                continue
            except LukatoError as exc:
                result.errors.append(
                    {
                        "index": index,
                        "commercial_id": item.commercial_id,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
                continue
            (result.created if created else result.updated).append(stored)
            touched.append(stored)

        if touched:
            fingerprints = await BuildFingerprint(self._container).many(touched)
            async with self._container.uow_factory() as uow:
                for fingerprint in fingerprints:
                    previous = await uow.commercials.get_fingerprint(fingerprint.commercial_id)
                    if previous is not None:
                        fingerprint = fingerprint.model_copy(
                            update={"id": previous.id, "created_at": previous.created_at}
                        )
                    await uow.commercials.upsert_fingerprint(fingerprint)
                await uow.commit()
        _logger.info(
            "commercials_bulk_imported",
            created=len(result.created),
            updated=len(result.updated),
            skipped=len(result.skipped),
            errors=len(result.errors),
        )
        return result

    async def _store(
        self, item: CommercialInput, *, update_existing: bool
    ) -> tuple[Commercial, bool]:
        """Grava um item do lote; devolve `(comercial, foi_criado)`."""
        commercial = item.to_commercial()
        async with self._container.uow_factory() as uow:
            existing = await uow.commercials.get_by_code(commercial.commercial_id)
            if existing is not None:
                if not update_existing:
                    raise ConflictError(
                        f"Ja existe um comercial com o codigo '{commercial.commercial_id}'.",
                        details={"commercial_id": commercial.commercial_id},
                    )
                merged = existing.model_copy(
                    update=commercial.model_dump(exclude={"id", "created_at", "updated_at"})
                )
                merged.touch()
                stored = await uow.commercials.update(merged)
                await uow.commit()
                return stored, False
            stored = await uow.commercials.add(commercial)
            await uow.commit()
            return stored, True


# ---------------------------------------------------------------------------
# Midia
# ---------------------------------------------------------------------------
class RegisterMedia(_AdWatchUseCase):
    """Registra um ativo de midia (`status="registered"`, SPEC-0010 secao 3.1)."""

    async def execute(self, data: MediaInput, principal: Principal) -> MediaAsset:
        """Grava o ativo e devolve a entidade registrada."""
        authorize(principal, ADWATCH_WRITE, "registrar midia")
        asset = data.to_asset()
        async with self._container.uow_factory() as uow:
            stored = await uow.media.add(asset)
            await uow.commit()
        _logger.info("media_registered", media_id=stored.id, uri=stored.uri, kind=stored.kind.value)
        return stored


class GetMedia(_AdWatchUseCase):
    """Busca um ativo de midia pelo identificador."""

    async def execute(self, media_id: Id, principal: Principal) -> MediaAsset:
        """Devolve o ativo; ausente levanta :class:`NotFoundError`."""
        authorize(principal, ADWATCH_READ, "ler midia")
        async with self._container.uow_factory() as uow:
            return await _require_media(uow, media_id)

    async def detail(self, media_id: Id, principal: Principal) -> Json:
        """Detalhe do ativo com os artefatos ja disponiveis e as capacidades instaladas."""
        authorize(principal, ADWATCH_READ, "ler midia")
        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)
            transcript = await uow.media.get_transcript(asset.id)
            scenes = await uow.media.list_scenes(asset.id)
            ocr = await uow.media.list_ocr(asset.id)
            detections = await uow.detections.count(media_id=asset.id)
        return {
            "media": asset.model_dump(mode="json"),
            "artifacts": {
                "transcript": transcript is not None,
                "transcript_words": 0 if transcript is None else len(transcript.words),
                "transcript_source": None if transcript is None else transcript.source,
                "scene_cuts": len(scenes),
                "ocr_texts": len(ocr),
                "detections": int(detections),
            },
            "capabilities": self._container.media.capabilities(),
        }


class ListMedia(_AdWatchUseCase):
    """Lista ativos de midia com filtros e paginacao."""

    async def execute(self, filters: MediaFilter, principal: Principal) -> Page[MediaAsset]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, ADWATCH_READ, "listar midia")
        criteria = filters.criteria()
        async with self._container.uow_factory() as uow:
            items = await uow.media.list(**criteria, limit=filters.limit, offset=filters.offset)
            total = await uow.media.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class DeleteMedia(_AdWatchUseCase):
    """Remove um ativo e, em cascata, transcricao, cenas, OCR e deteccoes."""

    async def execute(self, media_id: Id, principal: Principal) -> None:
        """Apaga o ativo; ausente levanta :class:`NotFoundError`."""
        authorize(principal, ADWATCH_WRITE, "remover midia")
        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)
            await uow.media.delete(asset.id)
            await uow.commit()
        _logger.info("media_deleted", media_id=asset.id, uri=asset.uri)


class GetMediaCapabilities(_AdWatchUseCase):
    """Reporta quais adaptadores multimodais estao instalados nesta instalacao."""

    async def execute(self, principal: Principal) -> Json:
        """Devolve capacidades, limiares e pesos vigentes (`GET /capabilities`)."""
        authorize(principal, ADWATCH_READ, "consultar capacidades do adwatch")
        capabilities = self._container.media.capabilities()
        settings = self._container.settings.adwatch
        embeddings = self._container.embeddings
        return {
            "capabilities": capabilities,
            "degraded": sorted(name for name, ready in capabilities.items() if not ready),
            "can_ingest": capabilities["probe"] and capabilities["asr"],
            "can_detect": True,
            "embeddings": {
                "model": str(embeddings.model),
                "dimensions": int(embeddings.dimensions),
            },
            "windows": {"sizes": list(settings.window_sizes), "stride": settings.window_stride},
            "weights": settings.weights(),
            "thresholds": {
                "accept": settings.accept_threshold,
                "review": settings.review_threshold,
            },
            "top_k": {"retrieval": settings.top_k_retrieval, "rerank": settings.top_k_rerank},
            "max_score_without": {
                "ocr": round(1.0 - settings.weight_ocr, 4),
                "vision": round(1.0 - settings.weight_visual, 4),
            },
            # Teto do que ESTA instalado, e nao de cada modalidade isolada.
            #
            # `max_score_without` responde "quanto se perde sem OCR?" e "quanto se
            # perde sem juiz visual?" — duas perguntas hipoteticas. Nenhuma
            # responde a que o operador precisa: com esta maquina, do jeito que
            # ela esta, ate onde uma deteccao consegue chegar?
            #
            # A diferenca decide o funil inteiro. Sem OCR o teto e 0,85, abaixo do
            # limiar de aceite de 0,90: NENHUMA deteccao e aceita
            # automaticamente, todas caem em revisao humana. Medido: em 81
            # deteccoes reais, a maior confianca foi 0,845 e zero passaram de
            # 0,90. A tela dizia "score maximo 85,0%" e ficava por isso mesmo.
            #
            # Sem juiz visual nao se perde peso: `visual_match` herda
            # `speech_match` (secao 3.4 da SPEC-0010), entao `vision` ausente NAO
            # entra nesta conta — so OCR entra.
            "max_score_effective": round(
                1.0 - (0.0 if capabilities["ocr"] else settings.weight_ocr), 4
            ),
        }


class IngestMedia(_AdWatchUseCase):
    """Executa a ingestao possivel: probe, audio, ASR, cenas e OCR.

    Cada etapa cujo adaptador nao esteja disponivel e **registrada e pulada**
    (SPEC-0010 secao 3.1). Nenhuma indisponibilidade — e nenhuma falha de
    adaptador — derruba a ingestao: o relatorio devolve o que foi alcancado e o
    `status` do ativo reflete exatamente isso.
    """

    async def execute(self, media_id: Id, principal: Principal) -> IngestReport:
        """Roda a ingestao e devolve o relatorio das etapas."""
        authorize(principal, ADWATCH_RUN, "executar a ingestao de midia")
        started = time.perf_counter()
        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)

        steps: list[IngestStep] = []
        duration = asset.duration_seconds
        fps = asset.fps

        probe = self._available(self._container.media.probe)
        if probe is None:
            steps.append(IngestStep("probe", STEP_SKIPPED, "adaptador de probe indisponivel"))
            steps.append(IngestStep("audio", STEP_SKIPPED, "depende do probe"))
            audio_path: str | None = None
        else:
            duration, fps, probe_step = await self._probe(probe, asset, duration, fps)
            steps.append(probe_step)
            audio_path, audio_step = await self._extract_audio(probe, asset)
            steps.append(audio_step)

        transcript_words = await self._transcribe(asset, audio_path, steps)
        scenes = await self._scenes(asset, steps)
        ocr = await self._ocr(asset, duration, steps)

        produced = bool(transcript_words) or bool(scenes) or bool(ocr)
        failed = any(step.status == STEP_FAILED for step in steps)
        if produced:
            status = MEDIA_STATUS_INGESTED
        elif failed:
            status = MEDIA_STATUS_FAILED
        else:
            status = asset.status or MEDIA_STATUS_REGISTERED

        report = IngestReport(
            media_id=asset.id,
            status=status,
            steps=steps,
            duration_seconds=duration,
            fps=fps,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        async with self._container.uow_factory() as uow:
            current = await _require_media(uow, asset.id)
            current.duration_seconds = max(current.duration_seconds, duration)
            current.fps = fps or current.fps
            current.status = status
            current.metadata = {**current.metadata, "ingest": report.to_dict()}
            current.touch()
            await uow.media.update(current)
            if transcript_words:
                await uow.media.save_transcript(
                    Transcript(
                        media_id=current.id,
                        language=DEFAULT_ASR_LANGUAGE,
                        words=transcript_words,
                        source="whisperx",
                    )
                )
            if scenes:
                await uow.media.save_scenes(current.id, scenes)
            if ocr:
                await uow.media.save_ocr(current.id, ocr)
            await uow.commit()
        _logger.info(
            "media_ingested",
            media_id=asset.id,
            status=status,
            completed=report.completed,
            skipped=report.skipped,
            failed=report.failed,
        )
        return report

    @staticmethod
    def _available(adapter: Any) -> Any:
        """Devolve o adaptador quando ele existe e se declara disponivel."""
        if adapter is None:
            return None
        try:
            return adapter if bool(adapter.available) else None
        except Exception as exc:  # pragma: no cover - adaptador defeituoso
            _logger.warning("media_adapter_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return None

    async def _probe(
        self, probe: Any, asset: MediaAsset, duration: float, fps: float
    ) -> tuple[float, float, IngestStep]:
        """Etapa 1: metadados tecnicos do arquivo."""
        try:
            metadata = await probe.probe(asset.uri)
        except LukatoError as exc:
            return duration, fps, IngestStep("probe", STEP_FAILED, f"{exc.code}: {exc}")
        found_duration = float(metadata.get("duration", 0.0) or 0.0)
        found_fps = float(metadata.get("fps", 0.0) or 0.0)
        return (
            max(duration, found_duration),
            found_fps or fps,
            IngestStep("probe", STEP_DONE, f"{found_duration:.3f}s a {found_fps:.3f} fps"),
        )

    async def _extract_audio(self, probe: Any, asset: MediaAsset) -> tuple[str | None, IngestStep]:
        """Etapa 2: extracao do audio WAV mono 16 kHz para o `workdir`."""
        target = str(
            PurePosixPath(self._container.settings.adwatch.workdir) / f"{asset.id}{AUDIO_SUFFIX}"
        )
        try:
            path = await probe.extract_audio(asset.uri, target)
        except LukatoError as exc:
            return None, IngestStep("audio", STEP_FAILED, f"{exc.code}: {exc}")
        return path, IngestStep("audio", STEP_DONE, path)

    async def _transcribe(
        self, asset: MediaAsset, audio_path: str | None, steps: list[IngestStep]
    ) -> list[TranscriptWord]:
        """Etapa 3: ASR alinhado por palavra."""
        asr = self._available(self._container.media.asr)
        if asr is None:
            steps.append(IngestStep("asr", STEP_SKIPPED, "adaptador de ASR indisponivel"))
            return []
        if not audio_path:
            steps.append(IngestStep("asr", STEP_SKIPPED, "sem audio extraido"))
            return []
        try:
            words = await asr.transcribe(audio_path, language=DEFAULT_ASR_LANGUAGE)
        except LukatoError as exc:
            steps.append(IngestStep("asr", STEP_FAILED, f"{exc.code}: {exc}"))
            return []
        steps.append(IngestStep("asr", STEP_DONE, f"{len(words)} palavra(s)", items=len(words)))
        return list(words)

    async def _scenes(self, asset: MediaAsset, steps: list[IngestStep]) -> list[SceneCut]:
        """Etapa 4: cortes de cena para o refino de fronteira."""
        detector = self._available(self._container.media.scenes)
        if detector is None:
            steps.append(IngestStep("scenes", STEP_SKIPPED, "detector de cenas indisponivel"))
            return []
        try:
            cuts = await detector.detect(asset.uri)
        except LukatoError as exc:
            steps.append(IngestStep("scenes", STEP_FAILED, f"{exc.code}: {exc}"))
            return []
        steps.append(IngestStep("scenes", STEP_DONE, f"{len(cuts)} corte(s)", items=len(cuts)))
        return list(cuts)

    async def _ocr(
        self, asset: MediaAsset, duration: float, steps: list[IngestStep]
    ) -> list[OcrText]:
        """Etapa 5: texto na tela."""
        ocr = self._available(self._container.media.ocr)
        if ocr is None:
            steps.append(IngestStep("ocr", STEP_SKIPPED, "motor de OCR indisponivel"))
            return []
        if duration <= 0.0:
            steps.append(IngestStep("ocr", STEP_SKIPPED, "duracao desconhecida"))
            return []
        try:
            texts = await ocr.extract(asset.uri, start=0.0, end=duration, fps=1.0)
        except LukatoError as exc:
            steps.append(IngestStep("ocr", STEP_FAILED, f"{exc.code}: {exc}"))
            return []
        steps.append(IngestStep("ocr", STEP_DONE, f"{len(texts)} texto(s)", items=len(texts)))
        return list(texts)


class ImportTranscript(_AdWatchUseCase):
    """Importa uma transcricao ja parseada pela camada de interface.

    O parsing do JSON (WhisperX ou lista simples) e responsabilidade do adaptador
    `TranscriptImporter`, chamado pelo router: a aplicacao recebe
    `list[TranscriptWord]`. Este caminho torna o pipeline inteiro executavel sem
    FFmpeg, sem GPU e sem rede (SPEC-0010 secao 3.1).
    """

    async def execute(
        self,
        media_id: Id,
        words: Sequence[TranscriptWord],
        principal: Principal,
        *,
        language: str = DEFAULT_ASR_LANGUAGE,
        source: str = "import",
    ) -> Transcript:
        """Grava a transcricao, substituindo a anterior se existir."""
        authorize(principal, ADWATCH_WRITE, "importar transcricoes")
        if not words:
            raise ValidationError(
                "a transcricao importada nao contem nenhuma palavra",
                details={"media_id": media_id},
            )
        ordered = sorted(words, key=lambda word: (word.start, word.end))
        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)
            transcript = await uow.media.save_transcript(
                Transcript(
                    media_id=asset.id,
                    language=(language or DEFAULT_ASR_LANGUAGE).strip() or DEFAULT_ASR_LANGUAGE,
                    words=list(ordered),
                    source=(source or "import").strip() or "import",
                )
            )
            span = max(word.end for word in ordered)
            asset.duration_seconds = max(asset.duration_seconds, span)
            if asset.status == MEDIA_STATUS_REGISTERED:
                asset.status = MEDIA_STATUS_INGESTED
            asset.touch()
            await uow.media.update(asset)
            await uow.commit()
        _logger.info(
            "transcript_imported",
            media_id=media_id,
            words=len(ordered),
            source=transcript.source,
            span=_round_time(span),
        )
        return transcript


class ImportScenes(_AdWatchUseCase):
    """Importa cortes de cena ja parseados pela camada de interface."""

    async def execute(self, media_id: Id, scenes: Sequence[SceneCut], principal: Principal) -> int:
        """Substitui os cortes de cena do ativo; devolve quantos foram gravados."""
        authorize(principal, ADWATCH_WRITE, "importar cortes de cena")
        ordered = sorted(scenes, key=lambda cut: (cut.start, cut.end))
        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)
            saved = await uow.media.save_scenes(asset.id, ordered)
            await uow.commit()
        _logger.info("scenes_imported", media_id=media_id, scenes=saved)
        return int(saved)


class ImportOcr(_AdWatchUseCase):
    """Importa textos de OCR ja parseados pela camada de interface."""

    async def execute(self, media_id: Id, texts: Sequence[OcrText], principal: Principal) -> int:
        """Substitui os textos de OCR do ativo; devolve quantos foram gravados."""
        authorize(principal, ADWATCH_WRITE, "importar textos de OCR")
        ordered = sorted(texts, key=lambda item: (item.start, item.end))
        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)
            saved = await uow.media.save_ocr(asset.id, ordered)
            await uow.commit()
        _logger.info("ocr_imported", media_id=media_id, texts=saved)
        return int(saved)


# ---------------------------------------------------------------------------
# Deteccao
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _MatchingEngine:
    """Motor de matching montado a partir de `Settings` (SPEC-0010 secao 3.6)."""

    windows: SlidingWindowBuilder
    builder: CandidateBuilder
    fusion: ScoreFusion
    refiner: BoundaryRefiner
    accept: float
    review: float
    top_k_retrieval: int
    top_k_rerank: int


@dataclass(slots=True)
class _IntervalIndex:
    """Marcas temporais por comercial, consultadas por sobreposicao.

    A supressao e o refino podem substituir um candidato por uma copia com
    fronteiras diferentes, entao nao existe identidade de objeto para carregar os
    marcadores (`verificado pelo juiz`, `encaixado em corte de cena`) ate a
    persistencia. Sobreposicao temporal e o vinculo estavel entre o candidato
    original e o consolidado.
    """

    _marks: dict[Id, list[tuple[float, float]]] = field(default_factory=dict)

    def mark(self, commercial_id: Id, start: float, end: float) -> None:
        """Registra um intervalo marcado para o comercial."""
        self._marks.setdefault(commercial_id, []).append((float(start), float(end)))

    def covers(self, commercial_id: Id, start: float, end: float) -> bool:
        """True quando algum intervalo marcado do comercial intersecta `[start, end]`."""
        return any(
            min(end, marked_end) > max(start, marked_start)
            for marked_start, marked_end in self._marks.get(commercial_id, ())
        )

    def __bool__(self) -> bool:
        """True quando ha ao menos um intervalo marcado."""
        return any(self._marks.values())


class DetectCommercials(_AdWatchUseCase):
    """Executa o funil completo de deteccao sobre uma midia (SPEC-0010 secao 3).

    Re-executar a deteccao **substitui** as deteccoes anteriores da midia: o
    funil e uma reanalise completa, e manter resultados de uma execucao antiga
    ao lado dos novos produziria contagem dupla da mesma veiculacao.
    """

    async def execute(
        self,
        media_id: Id,
        principal: Principal,
        *,
        window_sizes: Sequence[float] | None = None,
        top_k: int | None = None,
        keep_rejected: bool = False,
    ) -> DetectionReport:
        """Roda janelas, retrieval, rerank, fusao, juiz, supressao e refino."""
        authorize(principal, ADWATCH_RUN, "executar a deteccao de comerciais")
        started = time.perf_counter()

        async with self._container.uow_factory() as uow:
            asset = await _require_media(uow, media_id)
            transcript = self._require_transcript(asset, await uow.media.get_transcript(asset.id))
            scenes = await uow.media.list_scenes(asset.id)
            ocr = await uow.media.list_ocr(asset.id)
            commercials = await uow.commercials.all_active()
            stored_prints = await uow.commercials.list_fingerprints()

        catalog = {commercial.id: commercial for commercial in commercials}
        fingerprints = await self._resolve_fingerprints(catalog, stored_prints)

        engine = self._engine(window_sizes=window_sizes, top_k=top_k)
        windows = engine.windows.build(transcript, ocr=ocr)
        vectors = await self.embed_batch([window.text for window in windows])
        semantic_enabled = any(vector is not None for vector in vectors)

        candidates = self._rank(engine, windows, vectors, fingerprints, catalog)
        judged, verified, vision_calls = await self._judge(
            engine, candidates, asset=asset, transcript=transcript, catalog=catalog
        )
        pool = [
            candidate
            for candidate in judged
            if keep_rejected or self._status(engine, candidate) is not DetectionStatus.REJECTED
        ]
        suppressed = NonMaximumSuppression.suppress(pool)
        refined, by_scene = self._refine(
            engine, suppressed, transcript=transcript, scenes=scenes, prints=fingerprints
        )
        # O refino encolhe cada candidato ate as palavras que realmente casaram,
        # e as janelas menores viram fragmentos contidos na veiculacao inteira.
        # Sem estas duas passadas, a mesma veiculacao seria persistida varias
        # vezes, uma por tamanho de janela.
        final = self._absorb_fragments(NonMaximumSuppression.suppress(refined))

        detections = [
            self._detection(
                engine, candidate, asset=asset, catalog=catalog, scened=by_scene, verified=verified
            )
            for candidate in final
        ]
        persisted, replaced = await self._persist(asset, detections, keep_rejected=keep_rejected)
        report = DetectionReport(
            media_id=asset.id,
            media_uri=asset.uri,
            detections=detections,
            counts=self._counts(detections),
            windows=len(windows),
            candidates=len(candidates),
            commercials=len(fingerprints),
            persisted=persisted,
            replaced=replaced,
            scene_cuts=len(scenes),
            ocr_texts=len(ocr),
            vision_calls=vision_calls,
            vision_available=self._vision() is not None,
            semantic_enabled=semantic_enabled,
            keep_rejected=keep_rejected,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        _logger.info(
            "detection_completed",
            media_id=asset.id,
            windows=report.windows,
            candidates=report.candidates,
            accepted=report.accepted,
            needs_review=report.needs_review,
            rejected=report.rejected,
            elapsed_ms=round(report.elapsed_ms, 3),
        )
        return report

    # -- preparacao --------------------------------------------------------
    @staticmethod
    def _require_transcript(asset: MediaAsset, transcript: Transcript | None) -> Transcript:
        """Sem transcricao nao existe linha do tempo: falha com instrucao acionavel."""
        if transcript is not None and transcript.words:
            return transcript
        raise ValidationError(
            f"A midia '{asset.id}' nao tem transcricao: o funil de deteccao precisa da "
            f"linha do tempo de palavras. Importe uma transcricao "
            f"(POST /api/v1/adwatch/media/{asset.id}/transcript) ou execute a ingestao "
            f"(POST /api/v1/adwatch/media/{asset.id}/ingest) antes de detectar.",
            details={
                "media_id": asset.id,
                "hint": "import_transcript",
                "endpoints": [
                    f"/api/v1/adwatch/media/{asset.id}/transcript",
                    f"/api/v1/adwatch/media/{asset.id}/ingest",
                ],
            },
        )

    def _engine(
        self, *, window_sizes: Sequence[float] | None, top_k: int | None
    ) -> _MatchingEngine:
        """Monta o motor de matching a partir de `Settings` e dos overrides do pedido."""
        settings = self._container.settings.adwatch
        sizes = [float(size) for size in (window_sizes or settings.window_sizes)]
        retrieval = settings.top_k_retrieval if top_k is None else int(top_k)
        if retrieval < 1:
            raise ValidationError("top_k deve ser maior ou igual a 1", details={"top_k": top_k})
        fusion = ScoreFusion(
            weight_lexical=settings.weight_lexical,
            weight_semantic=settings.weight_semantic,
            weight_ocr=settings.weight_ocr,
            weight_visual=settings.weight_visual,
            weight_duration=settings.weight_duration,
        )
        return _MatchingEngine(
            windows=SlidingWindowBuilder(sizes=sizes, stride=settings.window_stride),
            builder=CandidateBuilder(
                lexical=LexicalMatcher(),
                semantic=SemanticMatcher(),
                order=OrderMatcher(DEFAULT_ORDER_THRESHOLD),
                fusion=fusion,
            ),
            fusion=fusion,
            refiner=BoundaryRefiner(DEFAULT_MAX_SHIFT),
            accept=settings.accept_threshold,
            review=settings.review_threshold,
            top_k_retrieval=retrieval,
            top_k_rerank=min(settings.top_k_rerank, retrieval),
        )

    async def _resolve_fingerprints(
        self, catalog: Mapping[Id, Commercial], stored: Sequence[AdFingerprint]
    ) -> dict[Id, AdFingerprint]:
        """Assinaturas dos comerciais ativos, construindo as que faltarem.

        Um comercial ativo sem assinatura ficaria invisivel para o funil sem
        nenhum sinal; em vez disso a assinatura e construida e gravada aqui.
        """
        found = {item.commercial_id: item for item in stored if item.commercial_id in catalog}
        missing = [commercial for key, commercial in catalog.items() if key not in found]
        if not missing:
            return found
        built = await BuildFingerprint(self._container).many(missing)
        async with self._container.uow_factory() as uow:
            for fingerprint in built:
                found[fingerprint.commercial_id] = await uow.commercials.upsert_fingerprint(
                    fingerprint
                )
            await uow.commit()
        _logger.info("fingerprints_rebuilt", commercials=[item.commercial_id for item in missing])
        return found

    # -- retrieval + rerank ------------------------------------------------
    def _rank(
        self,
        engine: _MatchingEngine,
        windows: Sequence[TextWindow],
        vectors: Sequence[list[float] | None],
        fingerprints: Mapping[Id, AdFingerprint],
        catalog: Mapping[Id, Commercial],
    ) -> list[DetectionCandidate]:
        """Filtro barato -> retrieval semantico -> rerank composto (secoes 3.4 e 3.5)."""
        if not windows or not fingerprints:
            return []
        ordered = sorted(fingerprints.values(), key=lambda item: item.commercial_id)
        keywords = {
            item.commercial_id: [normalize(word) for word in item.keywords if normalize(word)]
            for item in ordered
        }
        candidates: list[DetectionCandidate] = []
        for window, vector in zip(windows, vectors, strict=True):
            haystack = f" {normalize(window.text)} {normalize(window.ocr_text)} "
            eligible = [
                item
                for item in ordered
                if self._has_keyword(haystack, keywords[item.commercial_id])
            ]
            if not eligible:
                continue
            retrieved = self._retrieve(eligible, vector, engine.top_k_retrieval)
            candidates.extend(self._rerank(engine, window, vector, retrieved, catalog))
        return candidates

    @staticmethod
    def _has_keyword(haystack: str, keywords: Sequence[str]) -> bool:
        """Filtro barato da secao 3.4: sem keywords, o fingerprint passa direto."""
        if not keywords:
            return True
        return any(f" {keyword} " in haystack for keyword in keywords)

    @staticmethod
    def _retrieve(
        eligible: Sequence[AdFingerprint], vector: Sequence[float] | None, top_k: int
    ) -> list[AdFingerprint]:
        """Top-K semantico; pares sem embedding recebem similaridade neutra."""
        scored: list[tuple[float, str, AdFingerprint]] = []
        for item in eligible:
            if vector and item.embedding:
                similarity = SemanticMatcher.similarity(vector, item.embedding)
            else:
                similarity = NEUTRAL_SIMILARITY
            scored.append((similarity, item.commercial_id, item))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [item for _, _, item in scored[:top_k]]

    @staticmethod
    def _rerank(
        engine: _MatchingEngine,
        window: TextWindow,
        vector: Sequence[float] | None,
        retrieved: Sequence[AdFingerprint],
        catalog: Mapping[Id, Commercial],
    ) -> list[DetectionCandidate]:
        """Score composto lexico+semantico+ordem; devolve os `top_k_rerank` melhores."""
        scored: list[tuple[float, DetectionEvidence, AdFingerprint]] = []
        for fingerprint in retrieved:
            score, evidence = engine.builder.evaluate(window, fingerprint, window_vec=vector)
            scored.append((score, evidence, fingerprint))
        scored.sort(key=lambda entry: (-entry[0], entry[2].commercial_id))
        candidates: list[DetectionCandidate] = []
        for score, evidence, fingerprint in scored[: engine.top_k_rerank]:
            commercial = catalog.get(fingerprint.commercial_id)
            candidates.append(
                DetectionCandidate(
                    commercial_id=fingerprint.commercial_id,
                    commercial_code=commercial.commercial_id if commercial else "",
                    campaign=commercial.campaign if commercial else "",
                    start=window.start,
                    end=window.end,
                    score=_round_score(score),
                    evidence=evidence,
                )
            )
        return candidates

    # -- decisao -----------------------------------------------------------
    @staticmethod
    def _status(engine: _MatchingEngine, candidate: DetectionCandidate) -> DetectionStatus:
        """Classifica um candidato pelos limiares vigentes."""
        return engine.fusion.classify(candidate.score, accept=engine.accept, review=engine.review)

    def _vision(self) -> Any:
        """Juiz multimodal, quando instalado e disponivel."""
        judge = self._container.media.vision
        if judge is None:
            return None
        try:
            return judge if bool(judge.available) else None
        except Exception as exc:  # pragma: no cover - adaptador defeituoso
            _logger.warning("vision_judge_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return None

    async def _judge(
        self,
        engine: _MatchingEngine,
        candidates: Sequence[DetectionCandidate],
        *,
        asset: MediaAsset,
        transcript: Transcript,
        catalog: Mapping[Id, Commercial],
    ) -> tuple[list[DetectionCandidate], _IntervalIndex, int]:
        """Chama o juiz multimodal na faixa de revisao e recalcula o score (secao 3.6)."""
        verified = _IntervalIndex()
        judge = self._vision()
        if judge is None:
            return list(candidates), verified, 0
        review = [
            index
            for index, candidate in enumerate(candidates)
            if self._status(engine, candidate) is DetectionStatus.NEEDS_REVIEW
        ]
        review.sort(key=lambda index: -candidates[index].score)
        if len(review) > MAX_VISION_CALLS:
            _logger.warning(
                "vision_judge_truncated",
                media_id=asset.id,
                candidates=len(review),
                max_calls=MAX_VISION_CALLS,
            )
            review = review[:MAX_VISION_CALLS]

        updated = list(candidates)
        calls = 0
        for index in review:
            candidate = updated[index]
            commercial = catalog.get(candidate.commercial_id)
            if commercial is None:
                continue
            excerpt = transcript.window(candidate.start, candidate.end).text
            try:
                verdict = await judge.verify(
                    media_uri=asset.uri,
                    start=candidate.start,
                    end=candidate.end,
                    commercial=commercial,
                    transcript_excerpt=excerpt,
                )
            except LukatoError as exc:
                _logger.warning(
                    "vision_judge_failed",
                    media_id=asset.id,
                    commercial_id=commercial.commercial_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            calls += 1
            visual = verdict.get("visual_match") if isinstance(verdict, dict) else None
            if not isinstance(visual, int | float) or isinstance(visual, bool):
                # JSON invalido ou sinal ausente: o candidato permanece em revisao,
                # nunca e promovido por falha de parsing (SPEC-0010 secao 4).
                continue
            evidence = candidate.evidence.model_copy(
                update={
                    "visual_match": max(0.0, min(1.0, float(visual))),
                    "brand_detected": self._brand(verdict, candidate.evidence.brand_detected),
                }
            )
            updated[index] = candidate.model_copy(
                update={"evidence": evidence, "score": _round_score(engine.fusion.fuse(evidence))}
            )
            verified.mark(candidate.commercial_id, candidate.start, candidate.end)
        return updated, verified, calls

    @staticmethod
    def _brand(verdict: Json, fallback: str | None) -> str | None:
        """Le a marca reconhecida pelo juiz, preservando a evidencia anterior."""
        evidence = verdict.get("evidence")
        brand = evidence.get("brand_detected") if isinstance(evidence, dict) else None
        if isinstance(brand, str) and brand.strip():
            return brand.strip()
        return fallback

    # -- refino de fronteira ----------------------------------------------
    def _refine(
        self,
        engine: _MatchingEngine,
        candidates: Sequence[DetectionCandidate],
        *,
        transcript: Transcript,
        scenes: Sequence[SceneCut],
        prints: Mapping[Id, AdFingerprint],
    ) -> tuple[list[DetectionCandidate], _IntervalIndex]:
        """Encaixa as fronteiras nas palavras casadas e, quando houver, nos cortes de cena."""
        words = sorted(transcript.words, key=lambda word: (word.start, word.end))
        vocabularies = {key: _anchor_vocabulary(item) for key, item in prints.items()}
        scened = _IntervalIndex()
        refined: list[DetectionCandidate] = []
        for candidate in candidates:
            start, end = candidate.start, candidate.end
            bounds = self._word_bounds(
                words, start, end, vocabularies.get(candidate.commercial_id, frozenset())
            )
            if bounds is not None:
                start, end = bounds
            snapped_start, snapped_end, by_scene = engine.refiner.refine(start, end, scenes)
            if by_scene:
                start, end = snapped_start, snapped_end
            start, end = _round_time(start), _round_time(end)
            if by_scene:
                scened.mark(candidate.commercial_id, start, end)
            refined.append(candidate.model_copy(update={"start": start, "end": end}))
        return refined, scened

    @staticmethod
    def _word_bounds(
        words: Sequence[TranscriptWord], start: float, end: float, vocabulary: frozenset[str]
    ) -> tuple[float, float] | None:
        """Primeiro e ultimo timestamp de palavra casada dentro do intervalo (secao 3.8)."""
        if not vocabulary:
            return None
        first: float | None = None
        last: float | None = None
        for word in words:
            if word.end < start:
                continue
            if word.start > end:
                break
            if not any(token in vocabulary for token in tokenize(word.word)):
                continue
            begin = max(start, word.start)
            finish = min(end, word.end)
            first = begin if first is None else min(first, begin)
            last = finish if last is None else max(last, finish)
        if first is None or last is None or last <= first:
            return None
        return first, last

    @staticmethod
    def _absorb_fragments(
        candidates: Sequence[DetectionCandidate], *, ratio: float = FRAGMENT_OVERLAP_RATIO
    ) -> list[DetectionCandidate]:
        """Descarta candidatos contidos em outro, de score maior, do mesmo comercial.

        Complementa a supressao por IoU com a leitura literal da SPEC-0010 secao
        3.7 (sobreposicao medida sobre o intervalo do candidato). Um pedaco de uma
        veiculacao ja detectada nao e uma segunda veiculacao: persistir os dois
        contaria a mesma insercao duas vezes.
        """
        ordered = sorted(candidates, key=lambda item: (-item.score, item.start, item.end))
        kept: list[DetectionCandidate] = []
        for candidate in ordered:
            span = max(candidate.end - candidate.start, _MIN_SPAN)
            covered = max(
                (
                    max(0.0, min(other.end, candidate.end) - max(other.start, candidate.start))
                    for other in kept
                    if other.commercial_id == candidate.commercial_id
                ),
                default=0.0,
            )
            if covered / span > ratio:
                continue
            kept.append(candidate)
        kept.sort(key=lambda item: (item.start, item.end, -item.score))
        return kept

    # -- persistencia ------------------------------------------------------
    def _detection(
        self,
        engine: _MatchingEngine,
        candidate: DetectionCandidate,
        *,
        asset: MediaAsset,
        catalog: Mapping[Id, Commercial],
        scened: _IntervalIndex,
        verified: _IntervalIndex,
    ) -> Detection:
        """Converte um candidato consolidado na entidade `Detection`."""
        commercial = catalog.get(candidate.commercial_id)
        code = commercial.commercial_id if commercial else candidate.commercial_code
        span = (candidate.commercial_id, candidate.start, candidate.end)
        return Detection(
            media_id=asset.id,
            commercial_id=candidate.commercial_id,
            commercial_code=code,
            campaign=commercial.campaign if commercial else candidate.campaign,
            brand=commercial.brand if commercial else "",
            start=candidate.start,
            end=candidate.end,
            confidence=candidate.score,
            status=self._status(engine, candidate),
            evidence=candidate.evidence,
            refined_by_scene=scened.covers(*span),
            verified_by_vlm=verified.covers(*span),
        )

    async def _persist(
        self, asset: MediaAsset, detections: Sequence[Detection], *, keep_rejected: bool
    ) -> tuple[int, int]:
        """Substitui as deteccoes da midia; devolve `(persistidas, substituidas)`."""
        storable = [
            item
            for item in detections
            if keep_rejected or item.status is not DetectionStatus.REJECTED
        ]
        async with self._container.uow_factory() as uow:
            replaced = await uow.detections.delete_by_media(asset.id)
            if storable:
                await uow.detections.add_many(storable)
            current = await _require_media(uow, asset.id)
            current.status = MEDIA_STATUS_ANALYZED
            current.touch()
            await uow.media.update(current)
            await uow.commit()
        return len(storable), int(replaced)

    @staticmethod
    def _counts(detections: Sequence[Detection]) -> dict[str, int]:
        """Contagem por `DetectionStatus`, sempre com as tres chaves presentes."""
        counts = {status.value: 0 for status in DetectionStatus}
        for detection in detections:
            counts[detection.status.value] += 1
        return counts


class ListDetections(_AdWatchUseCase):
    """Lista deteccoes com filtros e paginacao."""

    async def execute(self, filters: DetectionFilter, principal: Principal) -> Page[Detection]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, ADWATCH_READ, "listar deteccoes")
        criteria = filters.criteria()
        async with self._container.uow_factory() as uow:
            items = await uow.detections.list(
                **criteria, limit=filters.limit, offset=filters.offset
            )
            total = await uow.detections.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class GetDetection(_AdWatchUseCase):
    """Busca uma deteccao pelo identificador, com as evidencias."""

    async def execute(self, detection_id: Id, principal: Principal) -> Detection:
        """Devolve a deteccao; ausente levanta :class:`NotFoundError`."""
        authorize(principal, ADWATCH_READ, "ler deteccoes")
        async with self._container.uow_factory() as uow:
            return await _require_detection(uow, detection_id)


class ReviewDetection(_AdWatchUseCase):
    """Revisao humana de uma deteccao (`PATCH /detections/{id}`).

    `Detection` (SPEC-0000 secao 6.8) nao tem campo `notes` e proibe campos
    extras; a justificativa da revisao e portanto **registrada no log de
    auditoria estruturado**, com autor, status anterior e status novo. Persistir
    a nota exigiria alterar o contrato normativo, o que nao cabe a este caso de uso.
    """

    async def execute(
        self,
        detection_id: Id,
        status: DetectionStatus | str,
        principal: Principal,
        *,
        notes: str = "",
    ) -> Detection:
        """Aplica o veredito humano e devolve a deteccao atualizada."""
        authorize(principal, ADWATCH_WRITE, "revisar deteccoes")
        try:
            decided = DetectionStatus(status)
        except ValueError as exc:
            raise ValidationError(
                f"status de revisao invalido: '{status}'",
                details={
                    "status": str(status),
                    "allowed": [item.value for item in DetectionStatus],
                },
            ) from exc
        async with self._container.uow_factory() as uow:
            detection = await _require_detection(uow, detection_id)
            previous = detection.status
            detection.status = decided
            detection.touch()
            updated = await uow.detections.update(detection)
            await uow.commit()
        _logger.info(
            "detection_reviewed",
            detection_id=updated.id,
            media_id=updated.media_id,
            commercial_code=updated.commercial_code,
            previous_status=previous.value,
            status=decided.value,
            reviewer=principal.subject,
            notes=notes.strip(),
        )
        return updated


class DeleteDetections(_AdWatchUseCase):
    """Remove deteccoes de uma midia ou uma deteccao especifica."""

    async def execute(
        self,
        principal: Principal,
        *,
        media_id: Id | None = None,
        detection_id: Id | None = None,
    ) -> int:
        """Apaga por midia **ou** por identificador; devolve quantas foram removidas."""
        authorize(principal, ADWATCH_WRITE, "remover deteccoes")
        if bool(media_id) == bool(detection_id):
            raise ValidationError(
                "informe exatamente um entre 'media_id' e 'detection_id'",
                details={"media_id": media_id, "detection_id": detection_id},
            )
        async with self._container.uow_factory() as uow:
            if detection_id:
                detection = await _require_detection(uow, detection_id)
                await uow.detections.delete(detection.id)
                await uow.commit()
                removed = 1
            else:
                asset = await _require_media(uow, str(media_id))
                removed = int(await uow.detections.delete_by_media(asset.id))
                await uow.commit()
        _logger.info(
            "detections_deleted", media_id=media_id, detection_id=detection_id, removed=removed
        )
        return removed
