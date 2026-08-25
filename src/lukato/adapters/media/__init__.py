"""Adaptadores multimodais do AdWatch: FFmpeg, WhisperX, PaddleOCR, PySceneDetect, Qwen.

Todas as dependencias deste pacote sao **opcionais** (`requirements-media.txt`). O
import e sempre seguro: cada adaptador so toca a biblioteca externa dentro dos seus
metodos, expoe `available` com deteccao de capacidade memoizada e levanta
`UnsupportedCapability` — nunca `ImportError` — quando e chamado sem a ferramenta.

Sem nenhuma delas instalada, o pipeline continua executavel de ponta a ponta pelo
caminho de importacao JSON (`TranscriptImporter`, `SceneImporter`, `OcrImporter`),
que e puro, deterministico e nao faz I/O.
"""

from __future__ import annotations

from lukato.adapters.media.factory import (
    CAPABILITY_HINTS,
    VISION_HINT,
    build_media_toolbox,
    capability_report,
)
from lukato.adapters.media.ffmpeg_probe import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    FFMPEG_BINARY,
    FFPROBE_BINARY,
    FFmpegMediaProbe,
)
from lukato.adapters.media.importers import (
    OCR_CONTAINER_KEYS,
    SCENE_CONTAINER_KEYS,
    SCENE_KINDS,
    TRANSCRIPT_CONTAINER_KEYS,
    OcrImporter,
    SceneImporter,
    TranscriptImporter,
)
from lukato.adapters.media.paddle_ocr import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_SAMPLE_FPS,
    PaddleOCRAdapter,
)
from lukato.adapters.media.qwen_vision import (
    JUDGE_SYSTEM_PROMPT,
    QwenVisionJudge,
    format_timecode,
)
from lukato.adapters.media.scenedetect_cuts import (
    DEFAULT_CONTENT_THRESHOLD,
    DEFAULT_FADE_THRESHOLD,
    DEFAULT_MIN_SCENE_LEN,
    PySceneDetectCuts,
)
from lukato.adapters.media.whisperx_asr import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_COMPUTE_TYPE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL_SIZE,
    WhisperXASR,
)

__all__ = [
    "AUDIO_CHANNELS",
    "AUDIO_SAMPLE_RATE",
    "CAPABILITY_HINTS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_CONTENT_THRESHOLD",
    "DEFAULT_DEVICE",
    "DEFAULT_FADE_THRESHOLD",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_SCENE_LEN",
    "DEFAULT_MODEL_SIZE",
    "DEFAULT_SAMPLE_FPS",
    "FFMPEG_BINARY",
    "FFPROBE_BINARY",
    "JUDGE_SYSTEM_PROMPT",
    "OCR_CONTAINER_KEYS",
    "SCENE_CONTAINER_KEYS",
    "SCENE_KINDS",
    "TRANSCRIPT_CONTAINER_KEYS",
    "VISION_HINT",
    "FFmpegMediaProbe",
    "OcrImporter",
    "PaddleOCRAdapter",
    "PySceneDetectCuts",
    "QwenVisionJudge",
    "SceneImporter",
    "TranscriptImporter",
    "WhisperXASR",
    "build_media_toolbox",
    "capability_report",
    "format_timecode",
]
