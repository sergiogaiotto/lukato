"""Portas multimodais do AdWatch — todas opcionais e degradaveis.

Nenhuma delas pode ser obrigatoria: quando o adaptador correspondente nao esta
instalado, `available` e `False` e o pipeline segue registrando a etapa pulada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from lukato.domain.models.adwatch import Commercial, OcrText, SceneCut, TranscriptWord
from lukato.domain.types import Json

__all__ = [
    "ASRPort",
    "MediaProbePort",
    "MediaToolbox",
    "OCRPort",
    "SceneDetectorPort",
    "VisionJudgePort",
]


@runtime_checkable
class MediaProbePort(Protocol):
    """Inspecao e recorte de arquivos de midia (tipicamente FFmpeg)."""

    async def probe(self, uri: str) -> Json:
        """Metadados tecnicos do arquivo: duracao, fps, codecs, faixas."""
        ...

    async def extract_audio(self, uri: str, out_path: str) -> str:
        """Extrai a faixa de audio (WAV mono 16 kHz) e devolve o caminho gerado."""
        ...

    async def cut(self, uri: str, start: float, end: float, out_path: str) -> str:
        """Recorta o intervalo `[start, end]` e devolve o caminho gerado."""
        ...

    @property
    def available(self) -> bool:
        """True quando a ferramenta externa esta instalada e utilizavel."""
        ...


@runtime_checkable
class ASRPort(Protocol):
    """Transcricao automatica de fala com alinhamento temporal por palavra."""

    async def transcribe(self, audio_uri: str, *, language: str = "pt") -> list[TranscriptWord]:
        """Transcreve o audio e devolve as palavras com `start`/`end` em segundos."""
        ...

    @property
    def available(self) -> bool:
        """True quando o modelo de ASR esta instalado e utilizavel."""
        ...


@runtime_checkable
class OCRPort(Protocol):
    """Leitura de texto sobreposto nos quadros do video."""

    async def extract(
        self, media_uri: str, *, start: float, end: float, fps: float = 1.0
    ) -> list[OcrText]:
        """Extrai textos do intervalo, amostrando `fps` quadros por segundo."""
        ...

    @property
    def available(self) -> bool:
        """True quando o motor de OCR esta instalado e utilizavel."""
        ...


@runtime_checkable
class SceneDetectorPort(Protocol):
    """Deteccao de cortes de cena, usada no refino de fronteira das deteccoes."""

    async def detect(self, media_uri: str) -> list[SceneCut]:
        """Devolve os cortes de cena do arquivo em ordem temporal."""
        ...

    @property
    def available(self) -> bool:
        """True quando o detector esta instalado e utilizavel."""
        ...


@runtime_checkable
class VisionJudgePort(Protocol):
    """Juiz multimodal acionado apenas na faixa de revisao (`NEEDS_REVIEW`)."""

    async def verify(
        self,
        *,
        media_uri: str,
        start: float,
        end: float,
        commercial: Commercial,
        transcript_excerpt: str,
    ) -> Json:
        """Julga se o trecho corresponde ao comercial; devolve veredito e justificativa."""
        ...

    @property
    def available(self) -> bool:
        """True quando o modelo de visao esta configurado e alcancavel."""
        ...


@dataclass(slots=True)
class MediaToolbox:
    """Conjunto de capacidades multimodais disponiveis nesta instalacao.

    Cada campo e opcional: `None` (ou adaptador indisponivel) significa que a
    etapa correspondente do pipeline sera registrada e pulada.
    """

    probe: MediaProbePort | None = None
    asr: ASRPort | None = None
    ocr: OCRPort | None = None
    scenes: SceneDetectorPort | None = None
    vision: VisionJudgePort | None = None

    def capabilities(self) -> dict[str, bool]:
        """Mapa `nome -> disponivel` de cada capacidade multimodal."""
        return {
            "probe": self.probe is not None and self.probe.available,
            "asr": self.asr is not None and self.asr.available,
            "ocr": self.ocr is not None and self.ocr.available,
            "scenes": self.scenes is not None and self.scenes.available,
            "vision": self.vision is not None and self.vision.available,
        }
