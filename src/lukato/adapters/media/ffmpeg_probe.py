"""Sondagem e recorte de midia via FFmpeg/ffprobe (SPEC-0010 secao 3.1).

FFmpeg e uma dependencia **opcional** (`requirements-media.txt` + binario no sistema).
Este adaptador nunca quebra o import nem o boot: `available` detecta os binarios uma
unica vez e, quando faltam, cada metodo levanta `UnsupportedCapability` com a
instrucao de instalacao. O pipeline do AdWatch consulta `available` antes de chamar e
segue pelo caminho de importacao JSON quando a capacidade nao existe.

Os subprocessos sao criados com `asyncio.create_subprocess_exec` — **nunca** com
`shell=True`: os caminhos de midia vem de entrada do usuario e um `;` no nome do
arquivo viraria execucao de comando. Cada chamada tem timeout proprio e, ao falhar,
o `stderr` do FFmpeg entra na mensagem do erro, porque a causa real (codec ausente,
arquivo corrompido, permissao) so aparece la.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Final

from lukato.config import get_logger
from lukato.domain.errors import ProviderError, UnsupportedCapability, ValidationError
from lukato.domain.types import Json

__all__ = [
    "AUDIO_CHANNELS",
    "AUDIO_SAMPLE_RATE",
    "DEFAULT_TIMEOUT_SECONDS",
    "FFMPEG_BINARY",
    "FFPROBE_BINARY",
    "INSTALL_HINT",
    "FFmpegMediaProbe",
]

_logger = get_logger(__name__)

FFMPEG_BINARY: Final[str] = "ffmpeg"
"""Nome do binario de transcodificacao procurado no `PATH`."""

FFPROBE_BINARY: Final[str] = "ffprobe"
"""Nome do binario de sondagem procurado no `PATH`."""

INSTALL_HINT: Final[str] = (
    "instale o FFmpeg no sistema (ex.: `apt-get install -y ffmpeg`) e garanta que "
    "`ffmpeg` e `ffprobe` estejam no PATH"
)
"""Instrucao devolvida quando a capacidade e pedida sem os binarios."""

DEFAULT_TIMEOUT_SECONDS: Final[float] = 300.0
"""Teto de cada subprocesso: video longo demora, mas nao pode travar para sempre."""

AUDIO_SAMPLE_RATE: Final[int] = 16_000
"""Taxa de amostragem exigida pelos modelos de ASR (WhisperX)."""

AUDIO_CHANNELS: Final[int] = 1
"""Audio mono: o ASR nao usa estereo e o arquivo fica pela metade."""

_MAX_STDERR_CHARS: Final[int] = 2000
_QUIET_ARGS: Final[tuple[str, ...]] = ("-hide_banner", "-loglevel", "error", "-nostdin")


class FFmpegMediaProbe:
    """Implementa `MediaProbePort` chamando `ffprobe`/`ffmpeg` como subprocesso."""

    def __init__(
        self,
        *,
        ffmpeg: str = FFMPEG_BINARY,
        ffprobe: str = FFPROBE_BINARY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Guarda os nomes dos binarios e o teto de tempo de cada subprocesso."""
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout = max(1.0, float(timeout_seconds))
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        """True quando `ffmpeg` e `ffprobe` existem no `PATH` (deteccao memoizada)."""
        if self._available is None:
            self._available = bool(
                shutil.which(self._ffmpeg) is not None and shutil.which(self._ffprobe) is not None
            )
            _logger.info(
                "media_capability_detected",
                capability="probe",
                adapter="FFmpegMediaProbe",
                available=self._available,
                ffmpeg=self._ffmpeg,
                ffprobe=self._ffprobe,
            )
        return self._available

    def refresh_availability(self) -> None:
        """Descarta a deteccao memoizada (util apos instalar o FFmpeg em runtime)."""
        self._available = None

    async def probe(self, uri: str) -> Json:
        """Devolve `duration`, `fps`, `width`, `height`, `codecs` e `size_bytes`."""
        source = self._require(uri, action="probe")
        args = [
            self._ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            source,
        ]
        stdout, _ = await self._run(args, action="probe")
        return _parse_probe(stdout, uri=source)

    async def extract_audio(self, uri: str, out_path: str) -> str:
        """Extrai a faixa de audio em WAV mono 16 kHz e devolve o caminho gerado."""
        source = self._require(uri, action="extract_audio")
        target = await _prepare_output(out_path, action="extract_audio")
        args = [
            self._ffmpeg,
            *_QUIET_ARGS,
            "-y",
            "-i",
            source,
            "-vn",
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            target,
        ]
        await self._run(args, action="extract_audio")
        return await _confirm_output(target, action="extract_audio")

    async def cut(self, uri: str, start: float, end: float, out_path: str) -> str:
        """Recorta `[start, end]` sem recodificar (`-c copy`) e devolve o caminho."""
        source = self._require(uri, action="cut")
        begin = max(0.0, float(start))
        finish = float(end)
        if finish <= begin:
            raise ValidationError(
                f"intervalo invalido para recorte: exige start < end, recebido "
                f"start={begin} end={finish}",
                details={"start": begin, "end": finish, "uri": source},
            )
        target = await _prepare_output(out_path, action="cut")
        args = [
            self._ffmpeg,
            *_QUIET_ARGS,
            "-y",
            "-ss",
            f"{begin:.3f}",
            "-to",
            f"{finish:.3f}",
            "-i",
            source,
            "-c",
            "copy",
            target,
        ]
        await self._run(args, action="cut")
        return await _confirm_output(target, action="cut")

    def _require(self, uri: str, *, action: str) -> str:
        """Valida a URI e garante que os binarios existem antes de gastar processo."""
        source = uri.strip() if uri else ""
        if not source:
            raise ValidationError(
                f"caminho de midia vazio em {action}",
                details={"action": action},
            )
        if not self.available:
            raise UnsupportedCapability(
                f"FFmpeg indisponivel: nao e possivel executar {action}; {INSTALL_HINT}",
                details={"capability": "probe", "action": action, "hint": INSTALL_HINT},
            )
        return source

    async def _run(self, args: list[str], *, action: str) -> tuple[bytes, bytes]:
        """Executa o subprocesso sem shell, com timeout, e devolve `(stdout, stderr)`."""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._available = False
            raise UnsupportedCapability(
                f"binario {args[0]!r} nao encontrado no PATH ao executar {action}; {INSTALL_HINT}",
                details={"capability": "probe", "action": action, "binary": args[0]},
            ) from exc
        except OSError as exc:
            raise ProviderError(
                f"falha ao iniciar {args[0]!r} para {action}: {exc}",
                details={"action": action, "error": type(exc).__name__},
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderError(
                f"{args[0]} excedeu {self._timeout:.0f}s em {action}",
                details={"action": action, "timeout_seconds": self._timeout},
            ) from exc

        if process.returncode != 0:
            detail = _tail(stderr)
            raise ProviderError(
                f"{args[0]} falhou em {action} (codigo {process.returncode}): {detail}",
                details={
                    "action": action,
                    "returncode": process.returncode,
                    "stderr": detail,
                },
            )
        return stdout, stderr


def _tail(stream: bytes) -> str:
    """Converte o `stderr` do FFmpeg em texto curto para a mensagem de erro."""
    text = stream.decode("utf-8", errors="replace").strip()
    if len(text) > _MAX_STDERR_CHARS:
        return text[-_MAX_STDERR_CHARS:]
    return text or "sem saida de erro"


async def _prepare_output(out_path: str, *, action: str) -> str:
    """Valida o destino e cria o diretorio pai, devolvendo o caminho absoluto."""
    target = out_path.strip() if out_path else ""
    if not target:
        raise ValidationError(
            f"caminho de saida vazio em {action}",
            details={"action": action},
        )
    return await asyncio.to_thread(_ensure_parent, target)


def _ensure_parent(target: str) -> str:
    """Expande `~`, cria o diretorio pai e devolve o caminho final (roda em thread)."""
    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


async def _confirm_output(target: str, *, action: str) -> str:
    """Confere que o arquivo esperado existe e nao esta vazio."""
    path = Path(target)
    size = await asyncio.to_thread(_safe_size, path)
    if size <= 0:
        raise ProviderError(
            f"{action} terminou sem erro mas nao produziu {target}",
            details={"action": action, "path": target, "size_bytes": size},
        )
    return target


def _safe_size(path: Path) -> int:
    """Tamanho do arquivo em bytes; `-1` quando ele nao existe."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _parse_probe(stdout: bytes, *, uri: str) -> Json:
    """Converte a saida JSON do ffprobe no dicionario normativo de metadados."""
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderError(
            f"ffprobe devolveu JSON invalido para {uri}",
            details={"action": "probe", "uri": uri},
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(
            f"ffprobe devolveu um documento inesperado para {uri}",
            details={"action": "probe", "uri": uri},
        )

    raw_container = payload.get("format")
    container: dict[str, Any] = raw_container if isinstance(raw_container, dict) else {}
    raw_streams = payload.get("streams")
    streams: list[dict[str, Any]] = [s for s in raw_streams or [] if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)

    duration = _first_float(container.get("duration"), *(s.get("duration") for s in streams))
    return {
        "duration": duration,
        "fps": _frame_rate(video),
        "width": _as_int(video.get("width")) if video else 0,
        "height": _as_int(video.get("height")) if video else 0,
        "codecs": [str(s["codec_name"]) for s in streams if s.get("codec_name")],
        "size_bytes": _as_int(container.get("size")),
    }


def _first_float(*values: Any) -> float:
    """Primeiro valor convertivel em float positivo; `0.0` quando nenhum serve."""
    for value in values:
        number = _as_float(value)
        if number > 0.0:
            return number
    return 0.0


def _as_float(value: Any) -> float:
    """Converte para float, devolvendo `0.0` em qualquer entrada inutilizavel."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number not in (float("inf"), float("-inf")) else 0.0


def _as_int(value: Any) -> int:
    """Converte para int, devolvendo `0` em qualquer entrada inutilizavel."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _frame_rate(video: dict[str, Any] | None) -> float:
    """Le `r_frame_rate`/`avg_frame_rate` no formato `num/den` e devolve fps."""
    if not video:
        return 0.0
    for field in ("avg_frame_rate", "r_frame_rate"):
        raw = video.get(field)
        if not isinstance(raw, str) or "/" not in raw:
            continue
        numerator, _, denominator = raw.partition("/")
        den = _as_float(denominator)
        if den <= 0.0:
            continue
        fps = _as_float(numerator) / den
        if fps > 0.0:
            return round(fps, 3)
    return 0.0
