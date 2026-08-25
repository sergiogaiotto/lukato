"""Modelos do AdWatch: catalogo de comerciais e deteccao multimodal em midia."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.types import Id, Json

__all__ = [
    "AdFingerprint",
    "Commercial",
    "Detection",
    "DetectionCandidate",
    "DetectionEvidence",
    "DetectionStatus",
    "MediaAsset",
    "MediaKind",
    "OcrText",
    "SceneCut",
    "Transcript",
    "TranscriptWord",
]


class MediaKind(StrEnum):
    """Natureza do ativo de midia analisado."""

    VIDEO = "video"
    AUDIO = "audio"


class Commercial(Entity):
    """Comercial catalogado (CRUD completo); `commercial_id` e o codigo de negocio."""

    commercial_id: str = Field(min_length=1)
    campaign: str
    brand: str
    text: str
    duration_expected: float = 30.0
    keywords: list[str] = Field(default_factory=list)
    key_phrases: list[str] = Field(default_factory=list)
    language: str = "pt-BR"
    is_active: bool = True
    metadata: Json = Field(default_factory=dict)


class AdFingerprint(Entity):
    """Assinatura derivada de um comercial, usada pelo motor de matching."""

    commercial_id: Id
    normalized_text: str
    token_set: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    key_phrases: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None
    duration: float = 30.0
    expected_brand: str = ""


class MediaAsset(Entity):
    """Arquivo de midia registrado para ingestao e analise."""

    uri: str
    kind: MediaKind = MediaKind.VIDEO
    duration_seconds: float = 0.0
    fps: float = 0.0
    title: str = ""
    status: str = "registered"
    metadata: Json = Field(default_factory=dict)


class TranscriptWord(DomainModel):
    """Palavra transcrita com marcacao temporal."""

    word: str
    start: float
    end: float
    score: float = 1.0
    speaker: str | None = None


class Transcript(Entity):
    """Transcricao alinhada no tempo de um ativo de midia."""

    media_id: Id
    language: str = "pt"
    words: list[TranscriptWord] = Field(default_factory=list)
    source: str = "import"

    @property
    def text(self) -> str:
        """Texto corrido da transcricao, com as palavras unidas por espaco."""
        return " ".join(word.word for word in self.words)

    def window(self, start: float, end: float) -> Transcript:
        """Recorta a transcricao nas palavras que intersectam `[start, end]`."""
        selected = [word for word in self.words if word.end >= start and word.start <= end]
        return Transcript(
            id=self.id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            media_id=self.media_id,
            language=self.language,
            words=selected,
            source=self.source,
        )


class SceneCut(DomainModel):
    """Corte de cena detectado no video."""

    index: int
    start: float
    end: float
    kind: str = "cut"


class OcrText(DomainModel):
    """Texto reconhecido em quadros do video."""

    text: str
    start: float
    end: float
    confidence: float = 1.0
    bbox: tuple[int, int, int, int] | None = None


class DetectionEvidence(DomainModel):
    """Evidencias por modalidade que sustentam um candidato a deteccao."""

    speech_match: float = 0.0
    semantic_match: float = 0.0
    ocr_match: float = 0.0
    visual_match: float = 0.0
    duration_match: float = 0.0
    order_ok: bool = True
    brand_detected: str | None = None
    matched_text: str = ""


class DetectionStatus(StrEnum):
    """Desfecho da fusao de scores para uma deteccao."""

    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class DetectionCandidate(DomainModel):
    """Janela candidata produzida pelo motor de matching, antes da decisao final."""

    commercial_id: Id
    commercial_code: str
    campaign: str = ""
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    score: float = Field(ge=0.0, le=1.0)
    evidence: DetectionEvidence = Field(default_factory=DetectionEvidence)

    @model_validator(mode="after")
    def _check_interval(self) -> DetectionCandidate:
        """Garante 0 <= start <= end."""
        if self.end < self.start:
            raise ValueError("intervalo invalido: exige 0 <= start <= end")
        return self


class Detection(Entity):
    """Deteccao consolidada de um comercial dentro de um ativo de midia."""

    media_id: Id
    commercial_id: Id
    commercial_code: str
    campaign: str = ""
    brand: str = ""
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    status: DetectionStatus
    evidence: DetectionEvidence = Field(default_factory=DetectionEvidence)
    refined_by_scene: bool = False
    verified_by_vlm: bool = False

    @model_validator(mode="after")
    def _check_interval(self) -> Detection:
        """Garante 0 <= start <= end."""
        if self.end < self.start:
            raise ValueError("intervalo invalido: exige 0 <= start <= end")
        return self
