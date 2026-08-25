"""Schemas do recurso `/api/v1/adwatch`: catalogo, midia e deteccao multimodal.

O AdWatch responde a uma pergunta operacional: *este comercial foi ao ar nesta
midia, quando e com qual confianca?* Estes schemas cobrem o CRUD do catalogo, a
ingestao (com importacao manual de transcricao, cenas e OCR, o caminho que roda
sem GPU e sem rede) e o relatorio de deteccao com as evidencias por modalidade.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field, RootModel

from lukato.application.dto import UNSET, Maybe
from lukato.application.use_cases.adwatch import (
    DEFAULT_ASR_LANGUAGE,
    DEFAULT_COMMERCIAL_DURATION,
    DEFAULT_LANGUAGE,
    BulkImportResult,
    CommercialDetail,
    CommercialInput,
    CommercialUpdateInput,
    DetectionReport,
    IngestReport,
    IngestStep,
    MediaInput,
)
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionEvidence,
    DetectionStatus,
    MediaAsset,
    MediaKind,
    OcrText,
    SceneCut,
    Transcript,
    TranscriptWord,
)
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import InSchema, OutSchema

__all__ = [
    "AdWatchEmbeddingsInfo",
    "AdWatchMaxScoreWithout",
    "AdWatchThresholds",
    "AdWatchTopK",
    "AdWatchWindowsInfo",
    "BulkImportBody",
    "BulkImportRequest",
    "BulkImportResponse",
    "CapabilitiesOut",
    "CommercialCreate",
    "CommercialDetailOut",
    "CommercialOut",
    "CommercialUpdate",
    "DetectRequest",
    "DetectionEvidenceOut",
    "DetectionOut",
    "DetectionReportOut",
    "DetectionReviewRequest",
    "FingerprintOut",
    "ImportResultOut",
    "IngestReportOut",
    "IngestStepOut",
    "MediaCreate",
    "MediaOut",
    "OcrImportRequest",
    "OcrTextOut",
    "SceneCutOut",
    "SceneImportRequest",
    "TranscriptImportRequest",
    "TranscriptOut",
    "TranscriptWordOut",
]

_COMMERCIAL_EXAMPLE: dict[str, Any] = {
    "commercial_id": "COM_000234",
    "campaign": "Verao 2026",
    "brand": "Claro",
    "text": "Chegou o novo plano com internet ilimitada para voce falar a vontade.",
    "duration_expected": 30.0,
    "keywords": ["plano", "ilimitada"],
    "key_phrases": ["internet ilimitada"],
    "language": "pt-BR",
    "is_active": True,
    "metadata": {"agencia": "interna"},
}


# ---------------------------------------------------------------------------
# Catalogo de comerciais
# ---------------------------------------------------------------------------
class CommercialCreate(InSchema):
    """Corpo de `POST /api/v1/adwatch/commercials`."""

    commercial_id: str = Field(min_length=1, description="Codigo de negocio, unico no catalogo.")
    campaign: str = Field(default="", description="Campanha a que o comercial pertence.")
    brand: str = Field(default="", description="Marca anunciante.")
    text: str = Field(min_length=1, description="Texto conhecido (locucao) do comercial.")
    duration_expected: float = Field(
        default=DEFAULT_COMMERCIAL_DURATION, gt=0.0, description="Duracao esperada em segundos."
    )
    keywords: list[str] = Field(default_factory=list, description="Palavras-chave de reforco.")
    key_phrases: list[str] = Field(default_factory=list, description="Frases marcantes da peca.")
    language: str = Field(default=DEFAULT_LANGUAGE, description="Idioma da locucao.")
    is_active: bool = Field(default=True, description="Comercial inativo nao entra na deteccao.")
    metadata: Json = Field(default_factory=dict, description="Metadados livres.")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": _COMMERCIAL_EXAMPLE})

    def to_input(self) -> CommercialInput:
        """Converte para o DTO do caso de uso `CreateCommercial`."""
        return CommercialInput(
            commercial_id=self.commercial_id,
            campaign=self.campaign,
            brand=self.brand,
            text=self.text,
            duration_expected=self.duration_expected,
            keywords=list(self.keywords),
            key_phrases=list(self.key_phrases),
            language=self.language,
            is_active=self.is_active,
            metadata=dict(self.metadata),
        )


class CommercialUpdate(InSchema):
    """Corpo de `PUT /api/v1/adwatch/commercials/{id}`: so muda o que foi enviado."""

    commercial_id: str | None = None
    campaign: str | None = None
    brand: str | None = None
    text: str | None = None
    duration_expected: float | None = Field(default=None, gt=0.0)
    keywords: list[str] | None = None
    key_phrases: list[str] | None = None
    language: str | None = None
    is_active: bool | None = None
    metadata: Json | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"campaign": "Verao 2026 — reforco", "is_active": False}},
    )

    def to_input(self) -> CommercialUpdateInput:
        """Converte para o DTO parcial, preservando o que nao foi enviado."""
        sent = self.model_fields_set

        def maybe(field: str, value: Any) -> Maybe[Any]:
            return value if field in sent else UNSET

        return CommercialUpdateInput(
            commercial_id=maybe("commercial_id", self.commercial_id),
            campaign=maybe("campaign", self.campaign),
            brand=maybe("brand", self.brand),
            text=maybe("text", self.text),
            duration_expected=maybe("duration_expected", self.duration_expected),
            keywords=maybe("keywords", list(self.keywords) if self.keywords is not None else None),
            key_phrases=maybe(
                "key_phrases", list(self.key_phrases) if self.key_phrases is not None else None
            ),
            language=maybe("language", self.language),
            is_active=maybe("is_active", self.is_active),
            metadata=maybe("metadata", dict(self.metadata) if self.metadata is not None else None),
        )


class CommercialOut(OutSchema):
    """Comercial devolvido pela API."""

    id: Id
    commercial_id: str
    campaign: str = ""
    brand: str = ""
    text: str
    duration_expected: float = DEFAULT_COMMERCIAL_DURATION
    keywords: list[str] = Field(default_factory=list)
    key_phrases: list[str] = Field(default_factory=list)
    language: str = DEFAULT_LANGUAGE
    is_active: bool = True
    metadata: Json = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, commercial: Commercial) -> CommercialOut:
        """Converte a entidade de dominio."""
        return cls(
            id=commercial.id,
            commercial_id=commercial.commercial_id,
            campaign=commercial.campaign,
            brand=commercial.brand,
            text=commercial.text,
            duration_expected=commercial.duration_expected,
            keywords=list(commercial.keywords),
            key_phrases=list(commercial.key_phrases),
            language=commercial.language,
            is_active=commercial.is_active,
            metadata=dict(commercial.metadata),
            created_at=commercial.created_at,
            updated_at=commercial.updated_at,
        )


class FingerprintOut(OutSchema):
    """Assinatura do comercial — **sem** o vetor de embedding.

    O embedding e detalhe de indice, tem centenas de dimensoes e nao ajuda quem
    consome a API; o que interessa e o texto normalizado e os termos derivados.
    """

    id: Id
    commercial_id: Id
    normalized_text: str = ""
    token_set: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    key_phrases: list[str] = Field(default_factory=list)
    duration: float = DEFAULT_COMMERCIAL_DURATION
    expected_brand: str = ""
    has_embedding: bool = Field(default=False, description="Se ha vetor semantico indexado.")

    @classmethod
    def from_domain(cls, fingerprint: AdFingerprint) -> FingerprintOut:
        """Converte a assinatura de dominio descartando o vetor."""
        return cls(
            id=fingerprint.id,
            commercial_id=fingerprint.commercial_id,
            normalized_text=fingerprint.normalized_text,
            token_set=list(fingerprint.token_set),
            keywords=list(fingerprint.keywords),
            key_phrases=list(fingerprint.key_phrases),
            duration=fingerprint.duration,
            expected_brand=fingerprint.expected_brand,
            has_embedding=fingerprint.embedding is not None,
        )


class CommercialDetailOut(OutSchema):
    """Detalhe de `GET /commercials/{id}`: o comercial e a sua assinatura."""

    commercial: CommercialOut
    fingerprint: FingerprintOut | None = None

    @classmethod
    def from_result(cls, detail: CommercialDetail) -> CommercialDetailOut:
        """Converte o DTO do caso de uso `GetCommercial`."""
        return cls(
            commercial=CommercialOut.from_domain(detail.commercial),
            fingerprint=(
                None
                if detail.fingerprint is None
                else FingerprintOut.from_domain(detail.fingerprint)
            ),
        )


class BulkImportBody(InSchema):
    """Lote com opcoes explicitas (forma alternativa a lista pura)."""

    items: list[CommercialCreate] = Field(default_factory=list, description="Comerciais do lote.")
    update_existing: bool = Field(
        default=False, description="True atualiza o comercial ja existente em vez de pular."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"items": [_COMMERCIAL_EXAMPLE], "update_existing": True}},
    )


class BulkImportRequest(RootModel[list[CommercialCreate] | BulkImportBody]):
    """Corpo de `POST /commercials/bulk`: um array puro **ou** `{items, update_existing}`.

    O array puro e o formato citado pela SPEC-0010; o objeto existe para quem
    precisa ligar `update_existing` sem mudar o corpo do lote.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                [_COMMERCIAL_EXAMPLE],
                {"items": [_COMMERCIAL_EXAMPLE], "update_existing": True},
            ]
        }
    )

    @property
    def items(self) -> list[CommercialCreate]:
        """Comerciais do lote, qualquer que tenha sido a forma enviada."""
        if isinstance(self.root, BulkImportBody):
            return list(self.root.items)
        return list(self.root)

    @property
    def update_existing(self) -> bool:
        """True quando o lote pediu atualizacao dos codigos ja cadastrados."""
        return self.root.update_existing if isinstance(self.root, BulkImportBody) else False

    def to_inputs(self) -> list[CommercialInput]:
        """Converte o lote para os DTOs do caso de uso `BulkImportCommercials`."""
        return [item.to_input() for item in self.items]


class BulkImportResponse(OutSchema):
    """Desfecho da importacao em lote, item a item."""

    created: list[CommercialOut] = Field(default_factory=list, description="Comerciais criados.")
    updated: list[CommercialOut] = Field(
        default_factory=list, description="Comerciais atualizados."
    )
    skipped: list[Json] = Field(
        default_factory=list, description="Itens pulados, com o motivo de cada um."
    )
    errors: list[Json] = Field(
        default_factory=list, description="Itens recusados, com codigo e mensagem."
    )
    total: int = Field(default=0, ge=0, description="Itens processados com desfecho conhecido.")

    @classmethod
    def from_result(cls, result: BulkImportResult) -> BulkImportResponse:
        """Converte o DTO do caso de uso `BulkImportCommercials`."""
        return cls(
            created=[CommercialOut.from_domain(item) for item in result.created],
            updated=[CommercialOut.from_domain(item) for item in result.updated],
            skipped=list(result.skipped),
            errors=list(result.errors),
            total=result.total,
        )


# ---------------------------------------------------------------------------
# Midia
# ---------------------------------------------------------------------------
class MediaCreate(InSchema):
    """Corpo de `POST /api/v1/adwatch/media`."""

    uri: str = Field(min_length=1, description="Caminho ou URL do arquivo de midia.")
    kind: MediaKind = Field(default=MediaKind.VIDEO, description="Natureza do ativo.")
    title: str = Field(default="", description="Titulo exibido no console.")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Duracao conhecida.")
    fps: float = Field(default=0.0, ge=0.0, description="Quadros por segundo conhecidos.")
    metadata: Json = Field(default_factory=dict, description="Metadados livres do ativo.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "uri": "./var/adwatch/gravacao-2026-08-24.mp4",
                "kind": "video",
                "title": "Gravacao 24/08 — faixa 1",
                "duration_seconds": 3600.0,
                "fps": 29.97,
                "metadata": {"emissora": "afiliada-1"},
            }
        },
    )

    def to_input(self) -> MediaInput:
        """Converte para o DTO do caso de uso `RegisterMedia`."""
        return MediaInput(
            uri=self.uri,
            kind=self.kind,
            title=self.title,
            duration_seconds=self.duration_seconds,
            fps=self.fps,
            metadata=dict(self.metadata),
        )


class MediaOut(OutSchema):
    """Ativo de midia devolvido pela API."""

    id: Id
    uri: str
    kind: MediaKind = MediaKind.VIDEO
    duration_seconds: float = 0.0
    fps: float = 0.0
    title: str = ""
    status: str = Field(default="registered", description="registered/ingested/analyzed/failed.")
    metadata: Json = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, asset: MediaAsset) -> MediaOut:
        """Converte a entidade de dominio."""
        return cls(
            id=asset.id,
            uri=asset.uri,
            kind=asset.kind,
            duration_seconds=asset.duration_seconds,
            fps=asset.fps,
            title=asset.title,
            status=asset.status,
            metadata=dict(asset.metadata),
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )


class IngestStepOut(OutSchema):
    """Desfecho de uma etapa da ingestao."""

    name: str = Field(description="Etapa executada: probe, audio, asr, scenes ou ocr.")
    status: str = Field(description="done, skipped ou failed.")
    detail: str = Field(default="", description="Explicacao legivel do desfecho.")
    items: int = Field(default=0, ge=0, description="Itens produzidos pela etapa.")

    @classmethod
    def from_result(cls, step: IngestStep) -> IngestStepOut:
        """Converte o DTO de etapa da camada de aplicacao."""
        return cls(name=step.name, status=step.status, detail=step.detail, items=step.items)


class IngestReportOut(OutSchema):
    """Relatorio de `POST /media/{id}/ingest`: o que foi alcancado e o que foi pulado."""

    media_id: Id
    status: str = Field(description="Situacao final do ativo apos a ingestao.")
    steps: list[IngestStepOut] = Field(default_factory=list, description="Etapas, em ordem.")
    completed: list[str] = Field(default_factory=list, description="Etapas executadas.")
    skipped: list[str] = Field(
        default_factory=list, description="Etapas puladas por falta de adaptador instalado."
    )
    failed: list[str] = Field(default_factory=list, description="Etapas que falharam.")
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Duracao apurada da midia.")
    fps: float = Field(default=0.0, ge=0.0, description="Quadros por segundo apurados.")
    elapsed_ms: float = Field(default=0.0, ge=0.0, description="Tempo total da ingestao em ms.")

    @classmethod
    def from_result(cls, report: IngestReport) -> IngestReportOut:
        """Converte o DTO do caso de uso `IngestMedia`."""
        return cls(
            media_id=report.media_id,
            status=report.status,
            steps=[IngestStepOut.from_result(step) for step in report.steps],
            completed=report.completed,
            skipped=report.skipped,
            failed=report.failed,
            duration_seconds=report.duration_seconds,
            fps=report.fps,
            elapsed_ms=report.elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Importacoes manuais (o caminho offline do pipeline)
# ---------------------------------------------------------------------------
class TranscriptImportRequest(RootModel[list[Any] | dict[str, Any]]):
    """Corpo de `POST /media/{id}/transcript`: aceita **lista ou objeto**.

    A lista e a forma direta (`[{"word": "...", "start": 0.0, "end": 0.2}, ...]`);
    o objeto e o JSON do WhisperX inteiro (`{"segments": [...], "language": "pt"}`).
    O parsing fica com o `TranscriptImporter` do adaptador de midia, que conhece
    os dois formatos — este schema so preserva o payload e le os metadados.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                [{"word": "chegou", "start": 0.0, "end": 0.31, "score": 0.98}],
                {
                    "language": "pt",
                    "segments": [
                        {"words": [{"word": "chegou", "start": 0.0, "end": 0.31}]},
                    ],
                },
            ]
        }
    )

    @property
    def payload(self) -> Json | list[Any]:
        """Conteudo cru, no formato aceito por `TranscriptImporter.parse`."""
        return self.root

    @property
    def language(self) -> str:
        """Idioma declarado no objeto; a lista pura assume o padrao do ASR."""
        if isinstance(self.root, dict):
            declared = self.root.get("language")
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
        return DEFAULT_ASR_LANGUAGE

    @property
    def source(self) -> str:
        """Origem declarada no objeto; ausente marca a transcricao como importada."""
        if isinstance(self.root, dict):
            declared = self.root.get("source")
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
        return "import"


class SceneImportRequest(RootModel[list[Any] | dict[str, Any]]):
    """Corpo de `POST /media/{id}/scenes`: lista de cortes ou objeto que a contem."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                [{"index": 0, "start": 0.0, "end": 12.4, "kind": "cut"}],
                {"scenes": [{"index": 0, "start": 0.0, "end": 12.4}]},
            ]
        }
    )

    @property
    def payload(self) -> Json | list[Any]:
        """Conteudo cru, no formato aceito por `SceneImporter.parse`."""
        return self.root


class OcrImportRequest(RootModel[list[Any] | dict[str, Any]]):
    """Corpo de `POST /media/{id}/ocr`: lista de textos ou objeto que a contem."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                [{"text": "INTERNET ILIMITADA", "start": 3.0, "end": 5.5, "confidence": 0.93}],
                {"texts": [{"text": "INTERNET ILIMITADA", "start": 3.0, "end": 5.5}]},
            ]
        }
    )

    @property
    def payload(self) -> Json | list[Any]:
        """Conteudo cru, no formato aceito por `OcrImporter.parse`."""
        return self.root


class TranscriptWordOut(OutSchema):
    """Palavra transcrita com marcacao temporal."""

    word: str
    start: float
    end: float
    score: float = 1.0
    speaker: str | None = None

    @classmethod
    def from_domain(cls, word: TranscriptWord) -> TranscriptWordOut:
        """Converte a palavra de dominio."""
        return cls(
            word=word.word,
            start=word.start,
            end=word.end,
            score=word.score,
            speaker=word.speaker,
        )


class TranscriptOut(OutSchema):
    """Transcricao alinhada de um ativo de midia."""

    id: Id
    media_id: Id
    language: str = DEFAULT_ASR_LANGUAGE
    source: str = "import"
    words: list[TranscriptWordOut] = Field(default_factory=list)
    text: str = Field(default="", description="Texto corrido, para leitura direta.")
    duration: float = Field(default=0.0, ge=0.0, description="Fim da ultima palavra, em segundos.")

    @classmethod
    def from_domain(cls, transcript: Transcript) -> TranscriptOut:
        """Converte a transcricao de dominio."""
        return cls(
            id=transcript.id,
            media_id=transcript.media_id,
            language=transcript.language,
            source=transcript.source,
            words=[TranscriptWordOut.from_domain(word) for word in transcript.words],
            text=transcript.text,
            duration=max((word.end for word in transcript.words), default=0.0),
        )


class SceneCutOut(OutSchema):
    """Corte de cena detectado no video."""

    index: int
    start: float
    end: float
    kind: str = "cut"

    @classmethod
    def from_domain(cls, cut: SceneCut) -> SceneCutOut:
        """Converte o corte de dominio."""
        return cls(index=cut.index, start=cut.start, end=cut.end, kind=cut.kind)


class OcrTextOut(OutSchema):
    """Texto reconhecido em quadros do video."""

    text: str
    start: float
    end: float
    confidence: float = 1.0
    bbox: tuple[int, int, int, int] | None = None

    @classmethod
    def from_domain(cls, item: OcrText) -> OcrTextOut:
        """Converte o texto de dominio."""
        return cls(
            text=item.text,
            start=item.start,
            end=item.end,
            confidence=item.confidence,
            bbox=item.bbox,
        )


class ImportResultOut(OutSchema):
    """Confirmacao de uma importacao manual: quantos itens ficaram gravados."""

    media_id: Id
    imported: int = Field(default=0, ge=0, description="Itens efetivamente gravados.")
    kind: str = Field(description="O que foi importado: transcript, scenes ou ocr.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "media_id": "11111111-2222-3333-4444-555555555555",
                "imported": 128,
                "kind": "scenes",
            }
        }
    )


# ---------------------------------------------------------------------------
# Deteccao
# ---------------------------------------------------------------------------
class DetectRequest(InSchema):
    """Corpo de `POST /media/{id}/detect`."""

    window_sizes: list[float] | None = Field(
        default=None, description="Tamanhos de janela em segundos; ausente usa a configuracao."
    )
    top_k: int | None = Field(
        default=None, ge=1, le=100, description="Candidatos por janela no retrieval."
    )
    keep_rejected: bool = Field(
        default=False, description="True persiste tambem os candidatos rejeitados."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"window_sizes": [15.0, 30.0, 60.0], "top_k": 10, "keep_rejected": False}
        },
    )


class DetectionReviewRequest(InSchema):
    """Corpo de `PATCH /detections/{id}`: veredito humano sobre uma deteccao.

    `notes` vai para o log de auditoria estruturado — `Detection` e um contrato
    normativo fechado e nao tem campo para a justificativa.
    """

    status: DetectionStatus = Field(description="Novo status atribuido pelo revisor.")
    notes: str = Field(default="", description="Justificativa da revisao, registrada em auditoria.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"status": "accepted", "notes": "Conferido no quadro 00:12:31."}
        },
    )


class DetectionEvidenceOut(OutSchema):
    """Evidencias por modalidade que sustentam a deteccao."""

    speech_match: float = Field(default=0.0, description="Similaridade lexica da locucao.")
    semantic_match: float = Field(default=0.0, description="Similaridade semantica (embeddings).")
    ocr_match: float = Field(default=0.0, description="Similaridade com o texto em tela.")
    visual_match: float = Field(default=0.0, description="Veredito do juiz visual.")
    duration_match: float = Field(default=0.0, description="Aderencia a duracao esperada.")
    order_ok: bool = Field(default=True, description="Se a ordem das frases-chave bate.")
    brand_detected: str | None = Field(default=None, description="Marca reconhecida na janela.")
    matched_text: str = Field(default="", description="Trecho da transcricao que casou.")

    @classmethod
    def from_domain(cls, evidence: DetectionEvidence) -> DetectionEvidenceOut:
        """Converte as evidencias de dominio."""
        return cls(
            speech_match=evidence.speech_match,
            semantic_match=evidence.semantic_match,
            ocr_match=evidence.ocr_match,
            visual_match=evidence.visual_match,
            duration_match=evidence.duration_match,
            order_ok=evidence.order_ok,
            brand_detected=evidence.brand_detected,
            matched_text=evidence.matched_text,
        )


class DetectionOut(OutSchema):
    """Deteccao consolidada de um comercial dentro de uma midia."""

    id: Id
    media_id: Id
    commercial_id: Id
    commercial_code: str
    campaign: str = ""
    brand: str = ""
    start: float = Field(ge=0.0, description="Inicio da veiculacao em segundos.")
    end: float = Field(ge=0.0, description="Fim da veiculacao em segundos.")
    confidence: float = Field(ge=0.0, le=1.0, description="Score final de fusao.")
    status: DetectionStatus
    evidence: DetectionEvidenceOut = Field(default_factory=DetectionEvidenceOut)
    refined_by_scene: bool = Field(default=False, description="Fronteira ajustada por corte.")
    verified_by_vlm: bool = Field(default=False, description="Confirmada pelo juiz visual.")
    created_at: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "aaaa1111-2222-3333-4444-555566667777",
                "media_id": "11111111-2222-3333-4444-555555555555",
                "commercial_id": "22222222-3333-4444-5555-666666666666",
                "commercial_code": "COM_000234",
                "campaign": "Verao 2026",
                "brand": "Claro",
                "start": 751.2,
                "end": 781.0,
                "confidence": 0.94,
                "status": "accepted",
                "evidence": {"speech_match": 0.97, "duration_match": 0.99},
                "refined_by_scene": True,
                "verified_by_vlm": False,
            }
        }
    )

    @classmethod
    def from_domain(cls, detection: Detection) -> DetectionOut:
        """Converte a deteccao de dominio."""
        return cls(
            id=detection.id,
            media_id=detection.media_id,
            commercial_id=detection.commercial_id,
            commercial_code=detection.commercial_code,
            campaign=detection.campaign,
            brand=detection.brand,
            start=detection.start,
            end=detection.end,
            confidence=detection.confidence,
            status=detection.status,
            evidence=DetectionEvidenceOut.from_domain(detection.evidence),
            refined_by_scene=detection.refined_by_scene,
            verified_by_vlm=detection.verified_by_vlm,
            created_at=detection.created_at,
        )


class DetectionReportOut(OutSchema):
    """Relatorio de `POST /media/{id}/detect`: o funil inteiro, com contagens."""

    media_id: Id
    media_uri: str = ""
    detections: list[DetectionOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict, description="Deteccoes por status.")
    windows: int = Field(default=0, ge=0, description="Janelas avaliadas.")
    candidates: int = Field(default=0, ge=0, description="Candidatos gerados no retrieval.")
    commercials: int = Field(default=0, ge=0, description="Comerciais ativos considerados.")
    persisted: int = Field(default=0, ge=0, description="Deteccoes gravadas.")
    replaced: int = Field(default=0, ge=0, description="Deteccoes anteriores substituidas.")
    scene_cuts: int = Field(default=0, ge=0, description="Cortes de cena disponiveis.")
    ocr_texts: int = Field(default=0, ge=0, description="Textos de OCR disponiveis.")
    vision_calls: int = Field(default=0, ge=0, description="Chamadas ao juiz visual.")
    vision_available: bool = Field(default=False, description="Se ha juiz visual instalado.")
    semantic_enabled: bool = Field(default=True, description="Se houve sinal semantico.")
    keep_rejected: bool = Field(default=False, description="Se os rejeitados foram persistidos.")
    elapsed_ms: float = Field(default=0.0, ge=0.0, description="Tempo total do funil em ms.")

    @classmethod
    def from_result(cls, report: DetectionReport) -> DetectionReportOut:
        """Converte o DTO do caso de uso `DetectCommercials`."""
        return cls(
            media_id=report.media_id,
            media_uri=report.media_uri,
            detections=[DetectionOut.from_domain(item) for item in report.detections],
            counts=dict(report.counts),
            windows=report.windows,
            candidates=report.candidates,
            commercials=report.commercials,
            persisted=report.persisted,
            replaced=report.replaced,
            scene_cuts=report.scene_cuts,
            ocr_texts=report.ocr_texts,
            vision_calls=report.vision_calls,
            vision_available=report.vision_available,
            semantic_enabled=report.semantic_enabled,
            keep_rejected=report.keep_rejected,
            elapsed_ms=report.elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Capacidades
# ---------------------------------------------------------------------------
class AdWatchEmbeddingsInfo(OutSchema):
    """Embedder em uso pelo funil semantico."""

    model: str = ""
    dimensions: int = 0


class AdWatchWindowsInfo(OutSchema):
    """Janelas deslizantes configuradas."""

    sizes: list[float] = Field(default_factory=list)
    stride: float = 0.0


class AdWatchThresholds(OutSchema):
    """Limiares de decisao do funil."""

    accept: float = 0.0
    review: float = 0.0


class AdWatchTopK(OutSchema):
    """Cortes de retrieval e de rerank."""

    retrieval: int = 0
    rerank: int = 0


class AdWatchMaxScoreWithout(OutSchema):
    """Teto de score alcancavel quando uma modalidade esta ausente."""

    ocr: float = 0.0
    vision: float = 0.0


class CapabilitiesOut(OutSchema):
    """Resposta de `GET /api/v1/adwatch/capabilities`.

    Diz, sem rodeios, o que esta instalado nesta maquina e qual e o teto de score
    quando uma modalidade falta — um comercial nunca alcanca 1.0 sem OCR e sem
    juiz visual, e a UI precisa explicar isso ao operador.
    """

    capabilities: dict[str, bool] = Field(
        default_factory=dict, description="Adaptador por capacidade: probe, asr, scenes, ocr, vlm."
    )
    degraded: list[str] = Field(default_factory=list, description="Capacidades ausentes.")
    can_ingest: bool = Field(default=False, description="Se a ingestao automatica e possivel.")
    can_detect: bool = Field(default=True, description="Se a deteccao pode rodar.")
    embeddings: AdWatchEmbeddingsInfo = Field(default_factory=AdWatchEmbeddingsInfo)
    windows: AdWatchWindowsInfo = Field(default_factory=AdWatchWindowsInfo)
    weights: dict[str, float] = Field(default_factory=dict, description="Pesos da fusao de score.")
    thresholds: AdWatchThresholds = Field(default_factory=AdWatchThresholds)
    top_k: AdWatchTopK = Field(default_factory=AdWatchTopK)
    max_score_without: AdWatchMaxScoreWithout = Field(default_factory=AdWatchMaxScoreWithout)

    @classmethod
    def from_result(cls, report: Json) -> CapabilitiesOut:
        """Converte o mapa devolvido pelo caso de uso `GetMediaCapabilities`."""
        return cls.model_validate(report)
