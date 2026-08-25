"""Transcricao com alinhamento por palavra usando WhisperX (SPEC-0010 secao 3.1).

WhisperX e opcional (`requirements-media.txt`) e traz consigo torch e um modelo de
varios gigabytes. Por isso o import e **preguicoso**, feito dentro do metodo: apenas
`importlib.util.find_spec` roda no caminho de deteccao, e importar este modulo nunca
puxa torch nem quebra o boot em uma maquina sem GPU.

O que o adaptador entrega e o diferencial do WhisperX sobre o Whisper original: alem
da transcricao, um passo de **alinhamento forcado** que devolve `start`/`end` por
palavra — exatamente o que o motor de matching temporal do AdWatch consome. Sem
timestamps por palavra nao ha como dizer onde o comercial comeca e termina.

Modelo e modelo de alinhamento sao carregados uma vez e ficam em cache **no processo**
(carregar custa dezenas de segundos e alguns GB); `unload()` libera tudo.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import Any, Final

from lukato.config import get_logger
from lukato.domain.errors import ProviderError, UnsupportedCapability, ValidationError
from lukato.domain.models.adwatch import TranscriptWord

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_DEVICE",
    "DEFAULT_MODEL_SIZE",
    "INSTALL_HINT",
    "REQUIRED_PACKAGE",
    "WhisperXASR",
]

_logger = get_logger(__name__)

REQUIRED_PACKAGE: Final[str] = "whisperx"
"""Pacote cuja presenca define a capacidade de ASR."""

INSTALL_HINT: Final[str] = (
    "instale as dependencias multimodais com "
    "`pip install -r requirements-media.txt` (traz whisperx e torch) ou importe a "
    "transcricao pronta em POST /api/v1/adwatch/media/{id}/transcript"
)
"""Mensagem util devolvida quando a transcricao e pedida sem o WhisperX."""

DEFAULT_MODEL_SIZE: Final[str] = "small"
"""Tamanho padrao do modelo: equilibrio entre acuracia e CPU sem GPU."""

DEFAULT_DEVICE: Final[str] = "cpu"
"""Dispositivo padrao; use `cuda` quando houver GPU disponivel."""

DEFAULT_COMPUTE_TYPE: Final[str] = "int8"
"""Quantizacao padrao do faster-whisper (`float16` faz sentido apenas em GPU)."""

DEFAULT_BATCH_SIZE: Final[int] = 16
"""Numero de janelas de audio transcritas por lote."""

_WORD_KEYS: Final[tuple[str, ...]] = ("word", "text")
_SCORE_KEYS: Final[tuple[str, ...]] = ("score", "probability", "confidence")


class WhisperXASR:
    """Implementa `ASRPort` sobre WhisperX com alinhamento forcado por palavra."""

    def __init__(
        self,
        *,
        model_size: str = DEFAULT_MODEL_SIZE,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        """Guarda a configuracao do adaptador; nada e carregado ainda."""
        self._model_size = model_size.strip() or DEFAULT_MODEL_SIZE
        self._device = device.strip() or DEFAULT_DEVICE
        self._compute_type = compute_type.strip() or DEFAULT_COMPUTE_TYPE
        self._batch_size = max(1, int(batch_size))
        self._available: bool | None = None
        self._model: Any | None = None
        self._align: dict[str, tuple[Any, Any]] = {}

    @property
    def available(self) -> bool:
        """True quando o pacote `whisperx` esta instalado (deteccao memoizada)."""
        if self._available is None:
            self._available = _module_installed(REQUIRED_PACKAGE)
            _logger.info(
                "media_capability_detected",
                capability="asr",
                adapter="WhisperXASR",
                available=self._available,
                package=REQUIRED_PACKAGE,
                model_size=self._model_size,
                device=self._device,
            )
        return self._available

    @property
    def model_size(self) -> str:
        """Tamanho do modelo Whisper configurado."""
        return self._model_size

    @property
    def device(self) -> str:
        """Dispositivo de inferencia configurado."""
        return self._device

    async def transcribe(self, audio_uri: str, *, language: str = "pt") -> list[TranscriptWord]:
        """Transcreve o audio e devolve as palavras com `start`/`end` em segundos."""
        source = audio_uri.strip() if audio_uri else ""
        if not source:
            raise ValidationError(
                "caminho de audio vazio em transcribe",
                details={"capability": "asr"},
            )
        if not self.available:
            raise UnsupportedCapability(
                f"WhisperX indisponivel: nao e possivel transcrever {source}; {INSTALL_HINT}",
                details={
                    "capability": "asr",
                    "package": REQUIRED_PACKAGE,
                    "hint": INSTALL_HINT,
                    "uri": source,
                },
            )
        code = (language or "pt").strip().split("-")[0].lower() or "pt"
        return await asyncio.to_thread(self._transcribe_sync, source, code)

    def unload(self) -> None:
        """Libera o modelo e os modelos de alinhamento mantidos em memoria."""
        self._model = None
        self._align.clear()

    # ----------------------------------------------------------------- #
    # Execucao sincrona (roda em thread separada)
    # ----------------------------------------------------------------- #

    def _transcribe_sync(self, audio_uri: str, language: str) -> list[TranscriptWord]:
        """Carrega modelo, transcreve, alinha e converte — tudo fora do event loop."""
        whisperx = _import_whisperx()
        try:
            audio = whisperx.load_audio(audio_uri)
            model = self._load_model(whisperx)
            result = model.transcribe(audio, batch_size=self._batch_size, language=language)
            detected = str(result.get("language") or language)
            aligner, metadata = self._load_aligner(whisperx, detected)
            aligned = whisperx.align(
                result.get("segments") or [],
                aligner,
                metadata,
                audio,
                self._device,
                return_char_alignments=False,
            )
        except Exception as exc:
            raise ProviderError(
                f"WhisperX falhou ao transcrever {audio_uri}: {exc}",
                details={
                    "capability": "asr",
                    "uri": audio_uri,
                    "error": type(exc).__name__,
                    "model_size": self._model_size,
                    "device": self._device,
                },
            ) from exc
        return _to_words(aligned, uri=audio_uri)

    def _load_model(self, whisperx: Any) -> Any:
        """Carrega o modelo de transcricao uma unica vez por processo."""
        if self._model is None:
            _logger.info(
                "whisperx_model_loading",
                model_size=self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
            self._model = whisperx.load_model(
                self._model_size,
                self._device,
                compute_type=self._compute_type,
            )
        return self._model

    def _load_aligner(self, whisperx: Any, language: str) -> tuple[Any, Any]:
        """Carrega (e memoiza) o modelo de alinhamento forcado do idioma detectado."""
        cached = self._align.get(language)
        if cached is None:
            _logger.info("whisperx_align_model_loading", language=language, device=self._device)
            aligner, metadata = whisperx.load_align_model(
                language_code=language, device=self._device
            )
            cached = (aligner, metadata)
            self._align[language] = cached
        return cached


def _module_installed(name: str) -> bool:
    """True quando o modulo pode ser importado, sem de fato importa-lo."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _import_whisperx() -> Any:
    """Importa `whisperx` sob demanda, convertendo falha de import em erro de dominio."""
    try:
        return importlib.import_module(REQUIRED_PACKAGE)
    except ImportError as exc:
        raise UnsupportedCapability(
            f"WhisperX declarado disponivel mas o import falhou: {exc}; {INSTALL_HINT}",
            details={"capability": "asr", "package": REQUIRED_PACKAGE, "hint": INSTALL_HINT},
        ) from exc


def _to_words(aligned: Any, *, uri: str) -> list[TranscriptWord]:
    """Converte a saida alinhada do WhisperX em `TranscriptWord`s ordenados.

    Palavras sem timestamp (o alinhador as produz para numerais e simbolos que nao
    casam com o dicionario fonetico) sao descartadas e contabilizadas no log: elas
    nao ajudam o matching temporal e quebrariam a validacao do modelo.
    """
    segments = aligned.get("word_segments") if isinstance(aligned, dict) else None
    if not isinstance(segments, list):
        segments = []
    words: list[TranscriptWord] = []
    skipped = 0
    for entry in segments:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        text = _first_str(entry, _WORD_KEYS)
        start = _first_float(entry, ("start",))
        end = _first_float(entry, ("end",))
        if not text or start is None or end is None or end < start:
            skipped += 1
            continue
        score = _first_float(entry, _SCORE_KEYS)
        words.append(
            TranscriptWord(
                word=text,
                start=max(0.0, start),
                end=max(0.0, end),
                score=1.0 if score is None else min(1.0, max(0.0, score)),
                speaker=_speaker(entry),
            )
        )
    words.sort(key=lambda word: (word.start, word.end))
    _logger.info(
        "whisperx_transcription_ready",
        uri=uri,
        words=len(words),
        skipped=skipped,
    )
    return words


def _first_str(entry: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Primeiro campo textual nao vazio entre as chaves informadas."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_float(entry: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Primeiro campo numerico utilizavel entre as chaves informadas."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return None


def _speaker(entry: dict[str, Any]) -> str | None:
    """Locutor atribuido pela diarizacao, quando houver."""
    value = entry.get("speaker")
    return value.strip() if isinstance(value, str) and value.strip() else None
