"""Building block AdWatch: catalogo de comerciais e deteccao temporal (SPEC-0010).

Este modulo e a **porta de entrada uniforme** do AdWatch no ecossistema: a mesma
funcionalidade que a API v1 expoe em `/api/v1/adwatch` fica disponivel como
building block, invocavel por `InvokeModule` com a trinca guardrail-in →
system prompt → guardrail-out ao redor (SPEC-0001 secao 4).

Nenhuma regra de negocio mora aqui. O funil normativo — janelas deslizantes,
retrieval, rerank, fusao de score, juiz multimodal, supressao de sobreposicao e
refino de fronteira — vive em `lukato.application.use_cases.adwatch` e em
`lukato.domain.services.matching`. O que este arquivo faz e traduzir
`payload["action"]` no caso de uso correspondente, construido com o `Container`
publicado em `ctx.services["container"]`.

Decisoes que valem registro
---------------------------
* **A deteccao e sincrona.** Um catalogo grande faz o funil demorar, mas o
  contrato do building block e uma resposta: `handle` devolve o
  `DetectionReport` inteiro e anota a duracao medida em `metadata`. Um
  `payload["async"]` verdadeiro nao e ignorado em silencio — levanta
  :class:`UnsupportedCapability` dizendo que esta instalacao nao tem fila de
  trabalho (SPEC-0001 secao 2, requisito 7).
* **Limiares vem de `Settings`, a definicao apenas os afina.** `accept_threshold`
  e `review_threshold` nao sao parametros de `DetectCommercials`: eles sao lidos
  de `Settings.adwatch`. Para honrar o `config` da `ModuleDefinition` sem tocar na
  configuracao global do processo, o modulo deriva um `Container` cujos
  `Settings` carregam os limiares pedidos — e a mesma classe servindo duas
  definicoes com comportamentos diferentes, sem alterar codigo (SPEC-0001 secao 5).
* **O parsing da transcricao acontece aqui.** `TranscriptImporter` e um adaptador
  (`lukato.adapters.media.importers`) e um building block nao importa adaptadores
  (SPEC-0001 secao 2). O router HTTP usa o importador; este modulo le o payload ja
  desserializado e monta `TranscriptWord`s de dominio, aceitando os mesmos
  formatos WhisperX descritos na SPEC-0010 secao 3.1.
* **Capacidade ausente e explicada, nao escondida.** A acao `capabilities`
  devolve o mapa de `MediaToolbox.capabilities()` e, para cada capacidade
  ausente, o que instalar para habilita-la e o que o funil perde sem ela.

Tudo aqui roda offline: sem FFmpeg, sem GPU e sem rede o caminho
`import_transcript` → `detect` continua produzindo deteccoes auditaveis.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, ClassVar, Final, TypeGuard

from pydantic import ValidationError as PydanticValidationError

from lukato.application.container import Container
from lukato.application.dto import UNSET, Maybe
from lukato.application.use_cases.adwatch import (
    DEFAULT_ASR_LANGUAGE,
    DEFAULT_COMMERCIAL_DURATION,
    DEFAULT_LANGUAGE,
    BulkImportCommercials,
    CommercialFilter,
    CommercialInput,
    CommercialUpdateInput,
    CreateCommercial,
    DeleteCommercial,
    DetectCommercials,
    DetectionFilter,
    GetCommercial,
    GetMediaCapabilities,
    ImportTranscript,
    IngestMedia,
    ListCommercials,
    ListDetections,
    ListMedia,
    MediaFilter,
    MediaInput,
    RegisterMedia,
    ReviewDetection,
    UpdateCommercial,
)
from lukato.config import AdWatchSettings, get_logger
from lukato.domain.errors import UnsupportedCapability, ValidationError
from lukato.domain.models.adwatch import (
    Commercial,
    Detection,
    DetectionStatus,
    MediaAsset,
    MediaKind,
    TranscriptWord,
)
from lukato.domain.models.module import ModuleBinding, ModuleKind
from lukato.domain.ports.media import MediaToolbox
from lukato.domain.types import Json
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import register_module

__all__ = [
    "ADWATCH_ACTIONS",
    "CAPABILITY_REMEDIES",
    "CONTAINER_SERVICE",
    "DEFAULT_ACTION",
    "MAX_OFFSET",
    "MAX_PAGE_LIMIT",
    "MAX_TOP_K",
    "MAX_WINDOW_SIZE",
    "MEDIA_SERVICE",
    "TRANSCRIPT_CONTAINER_KEYS",
    "AdWatchModule",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
CONTAINER_SERVICE: Final[str] = "container"
"""Chave de `ctx.services` onde a plataforma publica o `Container` da aplicacao."""

MEDIA_SERVICE: Final[str] = "media"
"""Chave de `ctx.services` com a `MediaToolbox` desta instalacao."""

ADWATCH_ACTIONS: Final[tuple[str, ...]] = (
    "create_commercial",
    "list_commercials",
    "get_commercial",
    "update_commercial",
    "delete_commercial",
    "bulk_import",
    "register_media",
    "list_media",
    "ingest",
    "import_transcript",
    "detect",
    "list_detections",
    "review_detection",
    "capabilities",
)
"""Acoes aceitas em `payload["action"]` (SPEC-0010 secao 5)."""

DEFAULT_ACTION: Final[str] = "capabilities"
"""Acao assumida quando o chamador nao informa nenhuma.

Uma invocacao sem acao **nao pode** disparar um passo caro nem destrutivo:
`capabilities` e somente leitura, nao exige payload e responde exatamente o que a
instalacao consegue fazer — o melhor primeiro contato com o modulo.
"""

MAX_PAGE_LIMIT: Final[int] = 200
"""Teto de itens por pagina aceito pelas acoes de listagem."""

MAX_OFFSET: Final[int] = 1_000_000
"""Teto de deslocamento aceito pelas acoes de listagem."""

MAX_TOP_K: Final[int] = 200
"""Teto de candidatos por janela: `top_k` alto multiplica o custo do rerank."""

MAX_WINDOW_SIZE: Final[float] = 3_600.0
"""Maior janela deslizante aceita (1 h): acima disso a janela deixa de localizar."""

MAX_BULK_ITEMS: Final[int] = 5_000
"""Teto de itens por `bulk_import`, alinhado ao caso de uso de importacao."""

TRANSCRIPT_CONTAINER_KEYS: Final[tuple[str, ...]] = (
    "words",
    "word_segments",
    "transcript",
    "segments",
)
"""Chaves em que a lista de palavras pode chegar (formatos da SPEC-0010 secao 3.1)."""

CAPABILITY_REMEDIES: Final[Mapping[str, Mapping[str, str]]] = {
    "probe": {
        "adapter": "FFmpeg/ffprobe",
        "install": "instale o FFmpeg no sistema (ex.: `apt-get install -y ffmpeg`) e "
        "garanta que `ffmpeg` e `ffprobe` estejam no PATH",
        "impact": "sem probe nao ha duracao, fps nem extracao de audio a partir do video",
        "workaround": "informe 'duration_seconds' e 'fps' ao registrar a midia",
    },
    "asr": {
        "adapter": "WhisperX",
        "install": "instale as dependencias multimodais com "
        "`pip install -r requirements-media.txt` (traz whisperx)",
        "impact": "sem ASR a linha do tempo de palavras nao e extraida do audio",
        "workaround": "use a acao 'import_transcript' com a transcricao em JSON",
    },
    "ocr": {
        "adapter": "PaddleOCR",
        "install": "instale as dependencias multimodais com "
        "`pip install -r requirements-media.txt` (traz paddleocr e opencv)",
        "impact": "sem OCR o sinal 'ocr_match' vale 0.0 e o score maximo cai o peso do OCR",
        "workaround": "importe o OCR pronto em POST /api/v1/adwatch/media/{id}/ocr",
    },
    "scenes": {
        "adapter": "PySceneDetect",
        "install": "instale as dependencias multimodais com "
        "`pip install -r requirements-media.txt` (traz scenedetect e opencv)",
        "impact": "sem cortes de cena o refino de fronteira usa a primeira e a ultima "
        "palavra casada, com erro maior de inicio e fim",
        "workaround": "importe os cortes prontos em POST /api/v1/adwatch/media/{id}/scenes",
    },
    "vision": {
        "adapter": "juiz multimodal Qwen",
        "install": "configure LUKATO_LLM__BASE_URL e LUKATO_LLM__API_KEY apontando para "
        "um hub multimodal compativel com OpenAI",
        "impact": "sem juiz o 'visual_match' herda o 'speech_match' e o candidato na faixa "
        "de revisao permanece em NEEDS_REVIEW",
        "workaround": "revise manualmente com a acao 'review_detection'",
    },
}
"""Como habilitar cada capacidade multimodal ausente e o que o funil perde sem ela.

A tabela e propria do modulo por regra hexagonal: as instrucoes equivalentes de
`lukato.adapters.media.factory` vivem na camada de adaptadores, que um building
block nao pode importar (SPEC-0001 secao 2).
"""

_TRUE_WORDS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on", "sim"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off", "nao", "não"})

_WORD_FIELDS: Final[tuple[str, ...]] = ("word", "text", "value")
_START_FIELDS: Final[tuple[str, ...]] = ("start", "start_time", "start_seconds", "from")
_END_FIELDS: Final[tuple[str, ...]] = ("end", "end_time", "end_seconds", "to")
_SCORE_FIELDS: Final[tuple[str, ...]] = ("score", "confidence", "probability", "conf")


# ---------------------------------------------------------------------------
# Leitura defensiva do payload
# ---------------------------------------------------------------------------
def _text(payload: Mapping[str, Any], key: str, *, default: str = "") -> str:
    """Le um campo textual do payload, com `default` quando ausente ou em branco."""
    raw = payload.get(key)
    if raw is None:
        return default
    candidate = str(raw).strip()
    return candidate or default


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    """Le um campo textual opcional; ausente ou em branco vira `None`."""
    return _text(payload, key) or None


def _require_text(payload: Mapping[str, Any], key: str, *, action: str) -> str:
    """Le um campo textual obrigatorio da acao."""
    found = _optional_text(payload, key)
    if found is None:
        raise ValidationError(
            f"A acao '{action}' exige o campo '{key}' no payload.",
            details={"action": action, "field": key},
        )
    return found


def _reference(payload: Mapping[str, Any], keys: Sequence[str], *, action: str) -> str:
    """Resolve o identificador do alvo da acao entre as chaves aceitas."""
    for key in keys:
        found = _optional_text(payload, key)
        if found is not None:
            return found
    listing = " ou ".join(f"'{key}'" for key in keys)
    raise ValidationError(
        f"A acao '{action}' exige {listing} no payload.",
        details={"action": action, "fields": list(keys)},
    )


def _flag(payload: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    """Le um booleano do payload aceitando as formas textuais usuais."""
    raw = payload.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    candidate = str(raw).strip().lower()
    if not candidate:
        return default
    if candidate in _TRUE_WORDS:
        return True
    if candidate in _FALSE_WORDS:
        return False
    raise ValidationError(
        f"Valor booleano invalido em '{key}': {raw!r}.",
        details={"field": key, "value": str(raw)},
    )


def _integer_value(raw: Any, *, field: str, minimum: int, maximum: int) -> int:
    """Converte um valor solto em inteiro dentro de uma faixa fechada."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Valor inteiro invalido em '{field}': {raw!r}.",
            details={"field": field, "value": str(raw)},
        ) from exc
    return max(minimum, min(value, maximum))


def _integer(
    payload: Mapping[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    """Le um inteiro do payload dentro de uma faixa fechada."""
    raw = payload.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return _integer_value(raw, field=key, minimum=minimum, maximum=maximum)


def _number_value(raw: Any, *, field: str, minimum: float, maximum: float) -> float:
    """Converte um valor solto em ponto flutuante dentro de uma faixa fechada."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Valor numerico invalido em '{field}': {raw!r}.",
            details={"field": field, "value": str(raw)},
        ) from exc
    if not minimum <= value <= maximum:
        raise ValidationError(
            f"'{field}' precisa estar entre {minimum} e {maximum} (recebido {value}).",
            details={"field": field, "value": value, "minimum": minimum, "maximum": maximum},
        )
    return value


def _number(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: float,
    minimum: float = 0.0,
    maximum: float = float("inf"),
) -> float:
    """Le um numero do payload dentro de uma faixa fechada."""
    raw = payload.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return _number_value(raw, field=key, minimum=minimum, maximum=maximum)


def _optional_number(
    payload: Mapping[str, Any], key: str, *, minimum: float, maximum: float
) -> float | None:
    """Le um numero opcional; ausente vira `None` (o valor vigente prevalece)."""
    raw = payload.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _number_value(raw, field=key, minimum=minimum, maximum=maximum)


def _is_list(value: Any) -> TypeGuard[Sequence[Any]]:
    """True quando o valor e uma sequencia iteravel que nao e texto.

    Declarado como `TypeGuard` para que o mypy estreite o tipo em quem chama —
    sem isso, `entries.extend(nested)` acusa `Any | None` mesmo com a checagem
    feita logo acima.
    """
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _string_list(payload: Mapping[str, Any], key: str) -> list[str]:
    """Le uma lista de textos do payload, aceitando tambem CSV."""
    raw = payload.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if _is_list(raw):
        return [str(item).strip() for item in raw if str(item).strip()]
    raise ValidationError(
        f"O campo '{key}' precisa ser uma lista de textos.",
        details={"field": key, "received": type(raw).__name__},
    )


def _float_list(raw: Any, *, field: str, minimum: float, maximum: float) -> list[float]:
    """Converte lista (ou CSV) de numeros validando cada item na faixa."""
    if isinstance(raw, str):
        entries: list[Any] = [item.strip() for item in raw.split(",") if item.strip()]
    elif _is_list(raw):
        entries = list(raw)
    else:
        raise ValidationError(
            f"O campo '{field}' precisa ser uma lista de numeros.",
            details={"field": field, "received": type(raw).__name__},
        )
    return [
        _number_value(item, field=f"{field}[{index}]", minimum=minimum, maximum=maximum)
        for index, item in enumerate(entries)
    ]


def _mapping(payload: Mapping[str, Any], key: str) -> Json:
    """Le um sub-objeto do payload; ausente vira mapa vazio."""
    raw = payload.get(key)
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {str(name): value for name, value in raw.items()}
    raise ValidationError(
        f"O campo '{key}' precisa ser um objeto.",
        details={"field": key, "received": type(raw).__name__},
    )


def _action_of(request: ModuleRequest, payload: Mapping[str, Any], *, default: str) -> str:
    """Resolve a acao pedida: `payload["action"]` e, como atalho, `request.input`."""
    candidate = _text(payload, "action")
    if not candidate:
        typed = request.input.strip().lower()
        candidate = typed if typed in ADWATCH_ACTIONS else default
    candidate = candidate.strip().lower()
    if candidate not in ADWATCH_ACTIONS:
        raise ValidationError(
            f"Acao de adwatch desconhecida: {candidate!r}.",
            details={"action": candidate, "supported": list(ADWATCH_ACTIONS)},
        )
    return candidate


def _media_kind(payload: Mapping[str, Any]) -> MediaKind:
    """Le a natureza do ativo de midia; ausente assume video."""
    candidate = _text(payload, "kind", default=MediaKind.VIDEO.value).lower()
    try:
        return MediaKind(candidate)
    except ValueError as exc:
        raise ValidationError(
            f"Tipo de midia invalido: {candidate!r}.",
            details={"field": "kind", "allowed": [item.value for item in MediaKind]},
        ) from exc


def _detection_status(payload: Mapping[str, Any], key: str) -> DetectionStatus | None:
    """Le um filtro de status de deteccao; ausente vira `None`."""
    candidate = _text(payload, key).lower()
    if not candidate:
        return None
    try:
        return DetectionStatus(candidate)
    except ValueError as exc:
        raise ValidationError(
            f"Status de deteccao invalido: {candidate!r}.",
            details={"field": key, "allowed": [item.value for item in DetectionStatus]},
        ) from exc


# ---------------------------------------------------------------------------
# Atualizacao parcial do catalogo
# ---------------------------------------------------------------------------
def _maybe_text(payload: Mapping[str, Any], key: str) -> Maybe[str]:
    """Campo textual so entra na atualizacao quando a chave esta presente."""
    if key not in payload:
        return UNSET
    return _text(payload, key)


def _maybe_number(payload: Mapping[str, Any], key: str) -> Maybe[float]:
    """Campo numerico so entra na atualizacao quando a chave esta presente."""
    if key not in payload:
        return UNSET
    return _number(payload, key, default=0.0)


def _maybe_flag(payload: Mapping[str, Any], key: str) -> Maybe[bool]:
    """Campo booleano so entra na atualizacao quando a chave esta presente."""
    if key not in payload:
        return UNSET
    return _flag(payload, key)


def _maybe_list(payload: Mapping[str, Any], key: str) -> Maybe[Sequence[str]]:
    """Lista de textos so entra na atualizacao quando a chave esta presente."""
    if key not in payload:
        return UNSET
    return _string_list(payload, key)


def _maybe_mapping(payload: Mapping[str, Any], key: str) -> Maybe[Json]:
    """Sub-objeto so entra na atualizacao quando a chave esta presente."""
    if key not in payload:
        return UNSET
    return _mapping(payload, key)


def _commercial_input(data: Mapping[str, Any], *, action: str) -> CommercialInput:
    """Monta o DTO de criacao de comercial a partir de um objeto do payload."""
    return CommercialInput(
        commercial_id=_require_text(data, "commercial_id", action=action),
        campaign=_text(data, "campaign"),
        brand=_text(data, "brand"),
        text=_require_text(data, "text", action=action),
        duration_expected=_number(
            data, "duration_expected", default=DEFAULT_COMMERCIAL_DURATION, minimum=0.0
        ),
        keywords=_string_list(data, "keywords"),
        key_phrases=_string_list(data, "key_phrases"),
        language=_text(data, "language", default=DEFAULT_LANGUAGE),
        is_active=_flag(data, "is_active", default=True),
        metadata=_mapping(data, "metadata"),
    )


def _commercial_update(payload: Mapping[str, Any]) -> CommercialUpdateInput:
    """Monta o DTO de atualizacao parcial: ausente e diferente de vazio."""
    return CommercialUpdateInput(
        commercial_id=_maybe_text(payload, "commercial_id"),
        campaign=_maybe_text(payload, "campaign"),
        brand=_maybe_text(payload, "brand"),
        text=_maybe_text(payload, "text"),
        duration_expected=_maybe_number(payload, "duration_expected"),
        keywords=_maybe_list(payload, "keywords"),
        key_phrases=_maybe_list(payload, "key_phrases"),
        language=_maybe_text(payload, "language"),
        is_active=_maybe_flag(payload, "is_active"),
        metadata=_maybe_mapping(payload, "metadata"),
    )


def _bulk_items(payload: Mapping[str, Any]) -> list[CommercialInput]:
    """Le o lote de comerciais de `items` (ou `commercials`) validando cada objeto."""
    raw = payload.get("items")
    if raw is None:
        raw = payload.get("commercials")
    if raw is None:
        raise ValidationError(
            "A acao 'bulk_import' exige 'items' com a lista de comerciais.",
            details={"action": "bulk_import", "field": "items"},
        )
    if not _is_list(raw):
        raise ValidationError(
            "O campo 'items' precisa ser uma lista de objetos.",
            details={"field": "items", "received": type(raw).__name__},
        )
    entries = list(raw)
    if len(entries) > MAX_BULK_ITEMS:
        raise ValidationError(
            f"A importacao em lote aceita no maximo {MAX_BULK_ITEMS} itens por chamada.",
            details={"received": len(entries), "max": MAX_BULK_ITEMS},
        )
    items: list[CommercialInput] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValidationError(
                f"items[{index}] precisa ser um objeto.",
                details={"index": index, "received": type(entry).__name__},
            )
        try:
            items.append(_commercial_input(entry, action="bulk_import"))
        except ValidationError as exc:
            details: Json = {"index": index}
            details.update(exc.details)
            raise ValidationError(f"items[{index}]: {exc}", details=details) from exc
    return items


# ---------------------------------------------------------------------------
# Transcricao importada (formatos da SPEC-0010 secao 3.1)
# ---------------------------------------------------------------------------
def _first_field(entry: Mapping[str, Any], names: Sequence[str]) -> Any:
    """Devolve o primeiro campo presente e nao vazio entre os sinonimos aceitos."""
    for name in names:
        value = entry.get(name)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _transcript_entries(payload: Mapping[str, Any]) -> list[Any]:
    """Desembrulha a lista de palavras, achatando o formato por segmentos."""
    raw: Any = None
    for key in TRANSCRIPT_CONTAINER_KEYS:
        candidate = payload.get(key)
        if _is_list(candidate):
            raw = candidate
            break
    if raw is None:
        raise ValidationError(
            "A acao 'import_transcript' exige a transcricao em 'words' (lista de "
            "palavras) ou em 'segments' (saida do WhisperX).",
            details={"action": "import_transcript", "expected": list(TRANSCRIPT_CONTAINER_KEYS)},
        )
    entries: list[Any] = []
    for item in raw:
        nested = item.get("words") if isinstance(item, Mapping) else None
        if _is_list(nested):
            entries.extend(nested)
        else:
            entries.append(item)
    if not entries:
        raise ValidationError(
            "A transcricao informada nao contem nenhuma palavra.",
            details={"action": "import_transcript"},
        )
    return entries


def _to_word(entry: Any, *, index: int) -> TranscriptWord:
    """Converte um item do payload em `TranscriptWord`, apontando o indice no erro."""
    if not isinstance(entry, Mapping):
        raise ValidationError(
            f"words[{index}] precisa ser um objeto com 'word', 'start' e 'end'.",
            details={"index": index, "received": type(entry).__name__},
        )
    word = _first_field(entry, _WORD_FIELDS)
    start = _first_field(entry, _START_FIELDS)
    end = _first_field(entry, _END_FIELDS)
    if word is None or start is None or end is None:
        missing = [
            name
            for name, value in (("word", word), ("start", start), ("end", end))
            if value is None
        ]
        raise ValidationError(
            f"words[{index}] sem os campos {missing}.",
            details={"index": index, "missing": missing},
        )
    started = _number_value(start, field=f"words[{index}].start", minimum=0.0, maximum=float("inf"))
    ended = _number_value(end, field=f"words[{index}].end", minimum=0.0, maximum=float("inf"))
    if ended < started:
        raise ValidationError(
            f"words[{index}]: 'end' ({ended}) nao pode ser menor que 'start' ({started}).",
            details={"index": index, "start": started, "end": ended},
        )
    score = _first_field(entry, _SCORE_FIELDS)
    confidence = (
        1.0
        if score is None
        else _number_value(score, field=f"words[{index}].score", minimum=0.0, maximum=1.0)
    )
    speaker = entry.get("speaker")
    return TranscriptWord(
        word=str(word).strip(),
        start=started,
        end=ended,
        score=confidence,
        speaker=str(speaker).strip() or None if speaker is not None else None,
    )


def parse_transcript(payload: Mapping[str, Any]) -> list[TranscriptWord]:
    """Converte o payload de transcricao em `TranscriptWord`s ordenados por tempo.

    Aceita os formatos normativos da SPEC-0010 secao 3.1 — lista simples de
    palavras, objeto com `words` e saida do WhisperX com `segments` — alem dos
    sinonimos usuais (`text`, `start_time`, `end_time`, `confidence`).
    """
    entries = _transcript_entries(payload)
    words = [_to_word(entry, index=index) for index, entry in enumerate(entries)]
    words.sort(key=lambda word: (word.start, word.end))
    return words


# ---------------------------------------------------------------------------
# Serializacao das respostas
# ---------------------------------------------------------------------------
def _dump_commercial(commercial: Commercial) -> Json:
    """Serializa um comercial para a resposta do modulo."""
    return commercial.model_dump(mode="json")


def _dump_media(asset: MediaAsset) -> Json:
    """Serializa um ativo de midia para a resposta do modulo."""
    return asset.model_dump(mode="json")


def _dump_detection(detection: Detection) -> Json:
    """Serializa uma deteccao para a resposta do modulo."""
    return detection.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Building block
# ---------------------------------------------------------------------------
@register_module
class AdWatchModule(BaseModule):
    """Catalogo de comerciais e deteccao temporal multimodal em midia.

    Despacha por `payload["action"]`: CRUD do catalogo (`create_commercial`,
    `list_commercials`, `get_commercial`, `update_commercial`,
    `delete_commercial`, `bulk_import`), ciclo da midia (`register_media`,
    `list_media`, `ingest`, `import_transcript`), o funil (`detect`,
    `list_detections`, `review_detection`) e o diagnostico da instalacao
    (`capabilities`).
    """

    kind: ClassVar[ModuleKind] = ModuleKind.PIPELINE
    slug: ClassVar[str] = "adwatch"
    name: ClassVar[str] = "AdWatch"
    description: ClassVar[str] = (
        "Catalogo de comerciais com CRUD e deteccao temporal multimodal em video: "
        "qual comercial apareceu, onde comecou, onde terminou e com que evidencia."
    )
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[tuple[str, ...]] = (
        "crud_commercials",
        "ingest_media",
        "detect",
        "review",
    )
    config_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "window_sizes": {
                "type": "array",
                "items": {"type": "number", "minimum": 1.0, "maximum": MAX_WINDOW_SIZE},
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K},
            "keep_rejected": {"type": "boolean", "default": False},
            "accept_threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "review_threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
    }
    default_binding: ClassVar[ModuleBinding] = ModuleBinding(timeout_seconds=300.0)

    # -- execucao ----------------------------------------------------------
    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Despacha a acao pedida sobre os casos de uso do AdWatch."""
        container = ctx.service(CONTAINER_SERVICE, Container)
        config = self.validate_config(dict(ctx.definition.config or {}))
        payload: Json = dict(request.payload or {})
        action = _action_of(request, payload, default=DEFAULT_ACTION)

        if action == "create_commercial":
            return await self._create_commercial(container, ctx, payload)
        if action == "list_commercials":
            return await self._list_commercials(container, ctx, payload)
        if action == "get_commercial":
            return await self._get_commercial(container, ctx, payload)
        if action == "update_commercial":
            return await self._update_commercial(container, ctx, payload)
        if action == "delete_commercial":
            return await self._delete_commercial(container, ctx, payload)
        if action == "bulk_import":
            return await self._bulk_import(container, ctx, payload)
        if action == "register_media":
            return await self._register_media(container, ctx, payload)
        if action == "list_media":
            return await self._list_media(container, ctx, payload)
        if action == "ingest":
            return await self._ingest(container, ctx, payload)
        if action == "import_transcript":
            return await self._import_transcript(container, ctx, payload)
        if action == "detect":
            return await self._detect(container, ctx, payload, config)
        if action == "list_detections":
            return await self._list_detections(container, ctx, payload)
        if action == "review_detection":
            return await self._review_detection(container, ctx, payload)
        return await self._capabilities(container, ctx)

    # -- catalogo ----------------------------------------------------------
    async def _create_commercial(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`create_commercial`: grava o comercial e a sua assinatura de matching."""
        data = _commercial_input(payload, action="create_commercial")
        commercial = await CreateCommercial(container).execute(data, ctx.principal)
        return ModuleResponse(
            output=(
                f"Comercial '{commercial.commercial_id}' da marca "
                f"'{commercial.brand or 'sem marca'}' criado com assinatura de matching."
            ),
            data={
                "action": "create_commercial",
                "commercial": _dump_commercial(commercial),
            },
            metadata={"action": "create_commercial", "commercial_id": commercial.id},
        )

    async def _list_commercials(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`list_commercials`: catalogo paginado com busca, marca e campanha."""
        is_active = None if "is_active" not in payload else _flag(payload, "is_active")
        filters = CommercialFilter(
            search=_optional_text(payload, "search"),
            brand=_optional_text(payload, "brand"),
            campaign=_optional_text(payload, "campaign"),
            is_active=is_active,
            limit=_integer(payload, "limit", default=50, minimum=1, maximum=MAX_PAGE_LIMIT),
            offset=_integer(payload, "offset", default=0, minimum=0, maximum=MAX_OFFSET),
        )
        page = await ListCommercials(container).execute(filters, ctx.principal)
        data: Json = {"action": "list_commercials"}
        data.update(page.to_dict(_dump_commercial))
        return ModuleResponse(
            output=f"{page.count} de {page.total} comercial(is) no catalogo.",
            data=data,
            metadata={"action": "list_commercials", "total": page.total},
        )

    async def _get_commercial(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`get_commercial`: detalhe do comercial com a assinatura derivada."""
        reference = _reference(payload, ("id", "commercial_id"), action="get_commercial")
        detail = await GetCommercial(container).detail(reference, ctx.principal)
        data: Json = {"action": "get_commercial"}
        data.update(detail.to_dict())
        fingerprint = detail.fingerprint
        embedded = fingerprint is not None and fingerprint.embedding is not None
        return ModuleResponse(
            output=(
                f"Comercial '{detail.commercial.commercial_id}' com "
                f"{len(fingerprint.keywords) if fingerprint else 0} keyword(s) e "
                f"{'embedding' if embedded else 'sem embedding'}."
            ),
            data=data,
            metadata={
                "action": "get_commercial",
                "commercial_id": detail.commercial.id,
                "has_fingerprint": fingerprint is not None,
            },
        )

    async def _update_commercial(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`update_commercial`: atualizacao parcial; texto novo regera a assinatura.

        O alvo vem em `id` (identificador interno ou codigo de negocio);
        `commercial_id` no payload e o **novo** codigo de negocio, nunca o alvo.
        """
        reference = _require_text(payload, "id", action="update_commercial")
        changes = _commercial_update(payload)
        commercial = await UpdateCommercial(container).execute(reference, changes, ctx.principal)
        applied = sorted(changes.changes())
        return ModuleResponse(
            output=(
                f"Comercial '{commercial.commercial_id}' atualizado: "
                f"{', '.join(applied) if applied else 'nenhuma alteracao informada'}."
            ),
            data={
                "action": "update_commercial",
                "commercial": _dump_commercial(commercial),
                "changed": applied,
            },
            metadata={
                "action": "update_commercial",
                "commercial_id": commercial.id,
                "changed": applied,
            },
        )

    async def _delete_commercial(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`delete_commercial`: remove o comercial e, em cascata, a assinatura."""
        reference = _reference(payload, ("id", "commercial_id"), action="delete_commercial")
        await DeleteCommercial(container).execute(reference, ctx.principal)
        return ModuleResponse(
            output=f"Comercial '{reference}' removido do catalogo.",
            data={"action": "delete_commercial", "commercial_id": reference, "deleted": True},
            metadata={"action": "delete_commercial"},
        )

    async def _bulk_import(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`bulk_import`: importa um lote sem deixar um item ruim derrubar o resto."""
        items = _bulk_items(payload)
        update_existing = _flag(payload, "update_existing")
        result = await BulkImportCommercials(container).execute(
            items, ctx.principal, update_existing=update_existing
        )
        data: Json = {"action": "bulk_import", "update_existing": update_existing}
        data.update(result.to_dict())
        return ModuleResponse(
            output=(
                f"Lote processado: {len(result.created)} criado(s), "
                f"{len(result.updated)} atualizado(s), {len(result.skipped)} pulado(s) e "
                f"{len(result.errors)} com erro."
            ),
            data=data,
            metadata={
                "action": "bulk_import",
                "created": len(result.created),
                "updated": len(result.updated),
                "skipped": len(result.skipped),
                "errors": len(result.errors),
            },
        )

    # -- midia -------------------------------------------------------------
    async def _register_media(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`register_media`: registra o ativo com `status="registered"`."""
        data_input = MediaInput(
            uri=_require_text(payload, "uri", action="register_media"),
            kind=_media_kind(payload),
            title=_text(payload, "title"),
            duration_seconds=_number(payload, "duration_seconds", default=0.0),
            fps=_number(payload, "fps", default=0.0),
            metadata=_mapping(payload, "metadata"),
        )
        asset = await RegisterMedia(container).execute(data_input, ctx.principal)
        return ModuleResponse(
            output=f"Midia '{asset.uri}' registrada com status '{asset.status}'.",
            data={"action": "register_media", "media": _dump_media(asset)},
            metadata={"action": "register_media", "media_id": asset.id},
        )

    async def _list_media(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`list_media`: ativos paginados com filtro de status e busca."""
        filters = MediaFilter(
            status=_optional_text(payload, "status"),
            search=_optional_text(payload, "search"),
            limit=_integer(payload, "limit", default=50, minimum=1, maximum=MAX_PAGE_LIMIT),
            offset=_integer(payload, "offset", default=0, minimum=0, maximum=MAX_OFFSET),
        )
        page = await ListMedia(container).execute(filters, ctx.principal)
        data: Json = {"action": "list_media"}
        data.update(page.to_dict(_dump_media))
        return ModuleResponse(
            output=f"{page.count} de {page.total} ativo(s) de midia.",
            data=data,
            metadata={"action": "list_media", "total": page.total},
        )

    async def _ingest(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`ingest`: executa a ingestao possivel, pulando o que nao esta instalado."""
        media_id = _reference(payload, ("media_id", "id"), action="ingest")
        report = await IngestMedia(container).execute(media_id, ctx.principal)
        data: Json = {"action": "ingest"}
        data.update(report.to_dict())
        skipped = report.skipped
        return ModuleResponse(
            output=(
                f"Ingestao concluida com status '{report.status}': "
                f"{len(report.completed)} etapa(s) executada(s)"
                + (f", pulada(s): {', '.join(skipped)}." if skipped else ".")
            ),
            data=data,
            metadata={
                "action": "ingest",
                "media_id": report.media_id,
                "status": report.status,
                "completed": report.completed,
                "skipped": skipped,
                "failed": report.failed,
                "elapsed_ms": round(report.elapsed_ms, 3),
            },
        )

    async def _import_transcript(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`import_transcript`: caminho offline que dispensa FFmpeg, GPU e rede."""
        media_id = _reference(payload, ("media_id", "id"), action="import_transcript")
        words = parse_transcript(payload)
        transcript = await ImportTranscript(container).execute(
            media_id,
            words,
            ctx.principal,
            language=_text(payload, "language", default=DEFAULT_ASR_LANGUAGE),
            source=_text(payload, "source", default="import"),
        )
        span = max(word.end for word in transcript.words)
        return ModuleResponse(
            output=(
                f"Transcricao importada: {len(transcript.words)} palavra(s) cobrindo "
                f"{span:.1f}s da midia."
            ),
            data={
                "action": "import_transcript",
                "media_id": transcript.media_id,
                "transcript_id": transcript.id,
                "language": transcript.language,
                "source": transcript.source,
                "words": len(transcript.words),
                "span_seconds": round(span, 3),
            },
            metadata={
                "action": "import_transcript",
                "media_id": transcript.media_id,
                "words": len(transcript.words),
            },
        )

    # -- deteccao ----------------------------------------------------------
    async def _detect(
        self, container: Container, ctx: ModuleContext, payload: Json, config: Json
    ) -> ModuleResponse:
        """`detect`: executa o funil inteiro de forma sincrona e devolve o relatorio.

        Nao existe execucao em segundo plano nesta instalacao: um `async`
        verdadeiro no payload levanta :class:`UnsupportedCapability` em vez de
        prometer um resultado que ninguem entregaria. A duracao real da execucao
        vai para `metadata`, ao lado da duracao medida pelo proprio funil.
        """
        if _flag(payload, "async"):
            raise UnsupportedCapability(
                "A deteccao do AdWatch e sincrona: esta instalacao nao tem fila de "
                "trabalho para execucao em segundo plano. Chame sem 'async' e aguarde "
                "o DetectionReport, ou reduza o catalogo com os filtros do proprio "
                "comercial (is_active).",
                details={"action": "detect", "capability": "async_detection"},
            )
        media_id = _reference(payload, ("media_id", "id"), action="detect")
        window_sizes = self._window_sizes(payload, config)
        top_k = self._top_k(payload, config)
        keep_rejected = _flag(
            payload, "keep_rejected", default=bool(config.get("keep_rejected", False))
        )
        tuned = self._tune(container, payload, config)

        started = time.perf_counter()
        report = await DetectCommercials(tuned).execute(
            media_id,
            ctx.principal,
            window_sizes=window_sizes,
            top_k=top_k,
            keep_rejected=keep_rejected,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        thresholds = tuned.settings.adwatch
        data: Json = {"action": "detect"}
        data.update(report.to_dict())
        data["thresholds"] = {
            "accept": thresholds.accept_threshold,
            "review": thresholds.review_threshold,
        }
        data["window_sizes"] = list(window_sizes or thresholds.window_sizes)
        data["top_k"] = top_k if top_k is not None else thresholds.top_k_retrieval
        _logger.info(
            "adwatch_module_detect",
            media_id=report.media_id,
            commercials=report.commercials,
            windows=report.windows,
            accepted=report.accepted,
            needs_review=report.needs_review,
            rejected=report.rejected,
            elapsed_ms=round(elapsed_ms, 3),
        )
        return ModuleResponse(
            output=(
                f"{len(report.detections)} deteccao(oes) em {report.windows} janela(s): "
                f"{report.accepted} aceita(s), {report.needs_review} em revisao e "
                f"{report.rejected} rejeitada(s), em {elapsed_ms / 1000.0:.2f}s."
            ),
            data=data,
            metadata={
                "action": "detect",
                "media_id": report.media_id,
                "synchronous": True,
                "elapsed_ms": round(elapsed_ms, 3),
                "elapsed_seconds": round(elapsed_ms / 1000.0, 3),
                "detector_elapsed_ms": round(report.elapsed_ms, 3),
                "commercials": report.commercials,
                "windows": report.windows,
                "candidates": report.candidates,
                "accepted": report.accepted,
                "needs_review": report.needs_review,
                "rejected": report.rejected,
                "vision_calls": report.vision_calls,
                "semantic_enabled": report.semantic_enabled,
            },
        )

    async def _list_detections(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`list_detections`: deteccoes com filtro por midia, comercial e status."""
        filters = DetectionFilter(
            media_id=_optional_text(payload, "media_id"),
            commercial_id=_optional_text(payload, "commercial_id"),
            status=_detection_status(payload, "status"),
            limit=_integer(payload, "limit", default=50, minimum=1, maximum=MAX_PAGE_LIMIT),
            offset=_integer(payload, "offset", default=0, minimum=0, maximum=MAX_OFFSET),
        )
        page = await ListDetections(container).execute(filters, ctx.principal)
        data: Json = {"action": "list_detections"}
        data.update(page.to_dict(_dump_detection))
        return ModuleResponse(
            output=f"{page.count} de {page.total} deteccao(oes).",
            data=data,
            metadata={"action": "list_detections", "total": page.total},
        )

    async def _review_detection(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`review_detection`: veredito humano sobre uma deteccao."""
        reference = _reference(payload, ("detection_id", "id"), action="review_detection")
        status = _require_text(payload, "status", action="review_detection")
        notes = _text(payload, "notes")
        detection = await ReviewDetection(container).execute(
            reference, status, ctx.principal, notes=notes
        )
        return ModuleResponse(
            output=(
                f"Deteccao de '{detection.commercial_code}' entre "
                f"{detection.start:.1f}s e {detection.end:.1f}s marcada como "
                f"'{detection.status.value}'."
            ),
            data={
                "action": "review_detection",
                "detection": _dump_detection(detection),
                "notes": notes,
            },
            metadata={
                "action": "review_detection",
                "detection_id": detection.id,
                "status": detection.status.value,
                "reviewer": ctx.principal.subject,
            },
        )

    # -- diagnostico -------------------------------------------------------
    async def _capabilities(self, container: Container, ctx: ModuleContext) -> ModuleResponse:
        """`capabilities`: o que esta instalado e o que falta para o resto."""
        toolbox = self._toolbox(container, ctx)
        capabilities = toolbox.capabilities()
        report = await GetMediaCapabilities(container).execute(ctx.principal)
        missing = sorted(name for name, ready in capabilities.items() if not ready)
        report["capabilities"] = capabilities
        report["degraded"] = missing
        report["can_ingest"] = capabilities["probe"] and capabilities["asr"]
        report["missing"] = {
            name: dict(CAPABILITY_REMEDIES[name]) for name in missing if name in CAPABILITY_REMEDIES
        }
        report["offline_path"] = (
            "import_transcript -> detect roda o funil inteiro sem FFmpeg, sem GPU e sem rede"
        )
        active = len(capabilities) - len(missing)
        return ModuleResponse(
            output=(
                f"{active} de {len(capabilities)} capacidade(s) multimodal(is) ativa(s)"
                + (f"; ausente(s): {', '.join(missing)}." if missing else ".")
            ),
            data={"action": "capabilities", **report},
            metadata={
                "action": "capabilities",
                "available": sorted(name for name, ready in capabilities.items() if ready),
                "degraded": missing,
            },
        )

    # -- configuracao da deteccao -----------------------------------------
    @staticmethod
    def _window_sizes(payload: Json, config: Json) -> list[float] | None:
        """Resolve os tamanhos de janela: payload > config > `Settings`."""
        raw = payload.get("window_sizes", config.get("window_sizes"))
        if raw is None:
            return None
        sizes = _float_list(raw, field="window_sizes", minimum=1.0, maximum=MAX_WINDOW_SIZE)
        if not sizes:
            raise ValidationError(
                "'window_sizes' nao pode ser uma lista vazia.",
                details={"field": "window_sizes"},
            )
        return sizes

    @staticmethod
    def _top_k(payload: Json, config: Json) -> int | None:
        """Resolve o `top_k` de retrieval: payload > config > `Settings`."""
        raw = payload.get("top_k", config.get("top_k"))
        if raw is None:
            return None
        return _integer_value(raw, field="top_k", minimum=1, maximum=MAX_TOP_K)

    @staticmethod
    def _tune(container: Container, payload: Json, config: Json) -> Container:
        """Deriva um `Container` com os limiares desta definicao de modulo.

        Os limiares sao normativos e moram em `Settings.adwatch`; o binding de uma
        `ModuleDefinition` pode afina-los sem tocar na configuracao global do
        processo. Sem override, o proprio container e devolvido — nenhuma copia,
        nenhuma divergencia possivel.
        """
        current: AdWatchSettings = container.settings.adwatch
        accept = _optional_number(payload, "accept_threshold", minimum=0.0, maximum=1.0)
        if accept is None and config.get("accept_threshold") is not None:
            accept = _number_value(
                config["accept_threshold"], field="accept_threshold", minimum=0.0, maximum=1.0
            )
        review = _optional_number(payload, "review_threshold", minimum=0.0, maximum=1.0)
        if review is None and config.get("review_threshold") is not None:
            review = _number_value(
                config["review_threshold"], field="review_threshold", minimum=0.0, maximum=1.0
            )
        wanted_accept = current.accept_threshold if accept is None else accept
        wanted_review = current.review_threshold if review is None else review
        if (wanted_accept, wanted_review) == (
            current.accept_threshold,
            current.review_threshold,
        ):
            return container
        try:
            tuned = AdWatchSettings.model_validate(
                {
                    **current.model_dump(),
                    "accept_threshold": wanted_accept,
                    "review_threshold": wanted_review,
                }
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                f"Limiares invalidos para o AdWatch: review_threshold ({wanted_review}) "
                f"precisa ser menor que accept_threshold ({wanted_accept}).",
                details={
                    "accept_threshold": wanted_accept,
                    "review_threshold": wanted_review,
                    "errors": exc.error_count(),
                },
            ) from exc
        _logger.info(
            "adwatch_thresholds_tuned",
            accept_threshold=tuned.accept_threshold,
            review_threshold=tuned.review_threshold,
        )
        return replace(container, settings=container.settings.model_copy(update={"adwatch": tuned}))

    @staticmethod
    def _toolbox(container: Container, ctx: ModuleContext) -> MediaToolbox:
        """Resolve a `MediaToolbox` publicada em `ctx.services`, com o container como rede."""
        found = ctx.services.get(MEDIA_SERVICE)
        return found if isinstance(found, MediaToolbox) else container.media

    # -- presenca na plataforma -------------------------------------------
    def ui(self) -> UIDescriptor:
        """Publica AdWatch, Comerciais e Deteccoes em FUNCIONALIDADE (SPEC-0009 secao 4)."""
        return UIDescriptor(
            nav=[
                UINavItem(
                    label="AdWatch",
                    icon="film",
                    endpoint="/adwatch",
                    section="FUNCIONALIDADE",
                    order=40,
                ),
                UINavItem(
                    label="Comerciais",
                    icon="book",
                    endpoint="/adwatch/commercials",
                    section="FUNCIONALIDADE",
                    order=41,
                ),
                UINavItem(
                    label="Deteccoes",
                    icon="pulse",
                    endpoint="/adwatch/detections",
                    section="FUNCIONALIDADE",
                    order=42,
                ),
            ],
            center_template="pages/adwatch.html",
            context_template="context/commercial.html",
        )

    def health(self) -> Json:
        """Resumo de saude com as acoes atendidas e o caminho offline garantido."""
        report = super().health()
        report["actions"] = list(ADWATCH_ACTIONS)
        report["default_action"] = DEFAULT_ACTION
        report["synchronous_detection"] = True
        report["offline_capable"] = True
        return report
