"""Montagem da `MediaToolbox` do AdWatch a partir de `Settings` (SPEC-0010 secao 3.1).

Todos os adaptadores multimodais sao **sempre instanciados** — construir qualquer um
deles e barato e nao importa nenhuma dependencia opcional. Quem decide se a etapa roda
e `MediaToolbox.capabilities()`, que consulta o `available` de cada adaptador em tempo
de execucao.

Manter as instancias mesmo indisponiveis e deliberado: `GET /adwatch/capabilities`
precisa dizer *o que falta*, e instalar o FFmpeg ao lado de um processo em pe passa a
funcionar sem recompor o container (basta `refresh_availability()`). Se os campos
virassem `None`, a UI so saberia dizer "ausente", sem nome nem motivo.
"""

from __future__ import annotations

from lukato.adapters.media.ffmpeg_probe import (
    INSTALL_HINT as FFMPEG_HINT,
)
from lukato.adapters.media.ffmpeg_probe import (
    FFmpegMediaProbe,
)
from lukato.adapters.media.paddle_ocr import (
    INSTALL_HINT as OCR_HINT,
)
from lukato.adapters.media.paddle_ocr import (
    PaddleOCRAdapter,
)
from lukato.adapters.media.qwen_vision import QwenVisionJudge
from lukato.adapters.media.scenedetect_cuts import (
    INSTALL_HINT as SCENES_HINT,
)
from lukato.adapters.media.scenedetect_cuts import (
    PySceneDetectCuts,
)
from lukato.adapters.media.whisperx_asr import (
    INSTALL_HINT as ASR_HINT,
)
from lukato.adapters.media.whisperx_asr import (
    WhisperXASR,
)
from lukato.config import Settings, get_logger
from lukato.domain.ports.llm import LLMPort
from lukato.domain.ports.media import MediaToolbox
from lukato.domain.types import Json

__all__ = ["CAPABILITY_HINTS", "VISION_HINT", "build_media_toolbox", "capability_report"]

_logger = get_logger(__name__)

VISION_HINT: str = (
    "configure LUKATO_LLM__BASE_URL e LUKATO_LLM__API_KEY para habilitar o juiz "
    "multimodal; sem ele o candidato em NEEDS_REVIEW permanece para revisao humana"
)
"""Instrucao exibida quando o juiz multimodal esta desligado."""

CAPABILITY_HINTS: dict[str, str] = {
    "probe": FFMPEG_HINT,
    "asr": ASR_HINT,
    "ocr": OCR_HINT,
    "scenes": SCENES_HINT,
    "vision": VISION_HINT,
}
"""Como habilitar cada capacidade multimodal ausente."""


def build_media_toolbox(settings: Settings, *, llm: LLMPort | None = None) -> MediaToolbox:
    """Constroi a `MediaToolbox` completa e registra as capacidades realmente ativas."""
    probe = FFmpegMediaProbe()
    toolbox = MediaToolbox(
        probe=probe,
        asr=WhisperXASR(),
        ocr=PaddleOCRAdapter(),
        scenes=PySceneDetectCuts(),
        vision=QwenVisionJudge(llm, settings, probe=probe),
    )
    capabilities = toolbox.capabilities()
    _logger.info(
        "media_toolbox_built",
        workdir=settings.adwatch.workdir,
        offline_path="importacao JSON de transcricao/cenas/OCR",
        **capabilities,
    )
    return toolbox


def capability_report(toolbox: MediaToolbox) -> Json:
    """Detalha cada capacidade: adaptador, disponibilidade e como habilita-la."""
    adapters = {
        "probe": toolbox.probe,
        "asr": toolbox.asr,
        "ocr": toolbox.ocr,
        "scenes": toolbox.scenes,
        "vision": toolbox.vision,
    }
    capabilities = toolbox.capabilities()
    return {
        name: {
            "available": capabilities[name],
            "adapter": type(adapter).__name__ if adapter is not None else None,
            "hint": None if capabilities[name] else CAPABILITY_HINTS[name],
        }
        for name, adapter in adapters.items()
    }
