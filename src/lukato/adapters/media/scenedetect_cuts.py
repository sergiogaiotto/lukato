"""Deteccao de cortes de cena com PySceneDetect (SPEC-0010 secao 3.8).

Os cortes de cena sao o que permite o **refino de fronteira** das deteccoes: o
matching textual acerta a janela, mas o inicio real do comercial e o corte de cena
mais proximo, nao o instante da primeira palavra reconhecida. Com isso o erro de
inicio/fim cai para bem abaixo dos 2 s exigidos pela meta de negocio.

PySceneDetect e opcional (`requirements-media.txt`): o import e preguicoso e
`available` apenas consulta o `importlib`.

`ContentDetector` acha cortes secos (mudanca brusca de conteudo entre quadros).
`ThresholdDetector`, opcional, acha fades para/de preto — comum entre blocos
comerciais. Quando ele esta ligado, o video e percorrido duas vezes, uma por
detector: e o unico jeito de saber **qual** detector produziu cada fronteira e,
portanto, de marcar `kind="fade"` com honestidade em vez de rotular tudo de `cut`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from typing import Any, Final

from lukato.config import get_logger
from lukato.domain.errors import ProviderError, UnsupportedCapability, ValidationError
from lukato.domain.models.adwatch import SceneCut

__all__ = [
    "DEFAULT_CONTENT_THRESHOLD",
    "DEFAULT_FADE_THRESHOLD",
    "DEFAULT_MIN_SCENE_LEN",
    "INSTALL_HINT",
    "REQUIRED_PACKAGE",
    "PySceneDetectCuts",
]

_logger = get_logger(__name__)

REQUIRED_PACKAGE: Final[str] = "scenedetect"
"""Pacote cuja presenca define a capacidade de deteccao de cenas."""

INSTALL_HINT: Final[str] = (
    "instale as dependencias multimodais com "
    "`pip install -r requirements-media.txt` (traz scenedetect e opencv) ou importe "
    "os cortes prontos em POST /api/v1/adwatch/media/{id}/scenes"
)
"""Mensagem util devolvida quando a deteccao e pedida sem o PySceneDetect."""

DEFAULT_CONTENT_THRESHOLD: Final[float] = 27.0
"""Sensibilidade do `ContentDetector` (padrao da propria biblioteca)."""

DEFAULT_FADE_THRESHOLD: Final[float] = 12.0
"""Luminancia media abaixo da qual o `ThresholdDetector` considera fade."""

DEFAULT_MIN_SCENE_LEN: Final[int] = 15
"""Duracao minima de uma cena, em quadros: evita picar o video em micro-cenas."""

_FADE_TOLERANCE_SECONDS: Final[float] = 0.5
"""Distancia maxima entre um corte e um fade para atribuir `kind="fade"`."""


class PySceneDetectCuts:
    """Implementa `SceneDetectorPort` com `ContentDetector` (+ `ThresholdDetector`)."""

    def __init__(
        self,
        *,
        content_threshold: float = DEFAULT_CONTENT_THRESHOLD,
        min_scene_len: int = DEFAULT_MIN_SCENE_LEN,
        detect_fades: bool = False,
        fade_threshold: float = DEFAULT_FADE_THRESHOLD,
    ) -> None:
        """Guarda os limiares dos detectores; nada e carregado ainda."""
        self._content_threshold = max(0.1, float(content_threshold))
        self._min_scene_len = max(1, int(min_scene_len))
        self._detect_fades = detect_fades
        self._fade_threshold = max(0.1, float(fade_threshold))
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        """True quando o pacote `scenedetect` esta instalado (deteccao memoizada)."""
        if self._available is None:
            self._available = _module_installed(REQUIRED_PACKAGE)
            _logger.info(
                "media_capability_detected",
                capability="scenes",
                adapter="PySceneDetectCuts",
                available=self._available,
                package=REQUIRED_PACKAGE,
                detect_fades=self._detect_fades,
            )
        return self._available

    @property
    def detect_fades(self) -> bool:
        """Indica se a passagem extra do `ThresholdDetector` esta ligada."""
        return self._detect_fades

    async def detect(self, media_uri: str) -> list[SceneCut]:
        """Devolve os cortes de cena do arquivo, em ordem temporal."""
        source = media_uri.strip() if media_uri else ""
        if not source:
            raise ValidationError(
                "caminho de midia vazio em detect",
                details={"capability": "scenes"},
            )
        if not self.available:
            raise UnsupportedCapability(
                f"PySceneDetect indisponivel: nao e possivel detectar cenas em {source}; "
                f"{INSTALL_HINT}",
                details={
                    "capability": "scenes",
                    "package": REQUIRED_PACKAGE,
                    "hint": INSTALL_HINT,
                    "uri": source,
                },
            )
        return await asyncio.to_thread(self._detect_sync, source)

    # ----------------------------------------------------------------- #
    # Execucao sincrona (roda em thread separada)
    # ----------------------------------------------------------------- #

    def _detect_sync(self, media_uri: str) -> list[SceneCut]:
        """Percorre o video com os detectores configurados e monta os `SceneCut`s."""
        scenedetect = _import_scenedetect()
        try:
            scenes = self._run_content_pass(scenedetect, media_uri)
            fades = self._run_fade_pass(scenedetect, media_uri) if self._detect_fades else []
        except Exception as exc:
            raise ProviderError(
                f"PySceneDetect falhou em {media_uri}: {exc}",
                details={
                    "capability": "scenes",
                    "uri": media_uri,
                    "error": type(exc).__name__,
                },
            ) from exc
        cuts = _to_cuts(scenes, fades)
        _logger.info(
            "scene_detection_ready",
            uri=media_uri,
            scenes=len(cuts),
            fades=len(fades),
            detect_fades=self._detect_fades,
        )
        return cuts

    def _run_content_pass(self, scenedetect: Any, media_uri: str) -> list[tuple[float, float]]:
        """Primeira passagem: cortes secos pelo `ContentDetector`."""
        detector = scenedetect.ContentDetector(
            threshold=self._content_threshold,
            min_scene_len=self._min_scene_len,
        )
        return _scene_seconds(scenedetect, media_uri, detector)

    def _run_fade_pass(self, scenedetect: Any, media_uri: str) -> list[float]:
        """Passagem opcional: inicios de cena marcados pelo `ThresholdDetector`."""
        detector = scenedetect.ThresholdDetector(
            threshold=self._fade_threshold,
            min_scene_len=self._min_scene_len,
        )
        return [start for start, _ in _scene_seconds(scenedetect, media_uri, detector)]


def _module_installed(name: str) -> bool:
    """True quando o modulo pode ser importado, sem de fato importa-lo."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _import_scenedetect() -> Any:
    """Importa `scenedetect` sob demanda, com erro de dominio claro."""
    try:
        return importlib.import_module(REQUIRED_PACKAGE)
    except ImportError as exc:
        raise UnsupportedCapability(
            f"PySceneDetect declarado disponivel mas o import falhou: {exc}; {INSTALL_HINT}",
            details={"capability": "scenes", "package": REQUIRED_PACKAGE, "hint": INSTALL_HINT},
        ) from exc


def _scene_seconds(scenedetect: Any, media_uri: str, detector: Any) -> list[tuple[float, float]]:
    """Roda um detector sobre o video e devolve `[(inicio, fim)]` em segundos."""
    video = scenedetect.open_video(media_uri)
    manager = scenedetect.SceneManager()
    manager.add_detector(detector)
    manager.detect_scenes(video)
    return [(_seconds(start), _seconds(end)) for start, end in manager.get_scene_list() or []]


def _seconds(timecode: Any) -> float:
    """Converte um `FrameTimecode` (ou numero) em segundos."""
    getter = getattr(timecode, "get_seconds", None)
    if callable(getter):
        return max(0.0, float(getter()))
    try:
        return max(0.0, float(timecode))
    except (TypeError, ValueError):
        return 0.0


def _to_cuts(scenes: list[tuple[float, float]], fades: list[float]) -> list[SceneCut]:
    """Converte os intervalos em `SceneCut`s, marcando os que coincidem com fades."""
    ordered = sorted((start, end) for start, end in scenes if end >= start)
    cuts: list[SceneCut] = []
    for index, (start, end) in enumerate(ordered):
        kind = "fade" if _near(start, fades) else "cut"
        cuts.append(SceneCut(index=index, start=start, end=end, kind=kind))
    return cuts


def _near(value: float, marks: list[float]) -> bool:
    """True quando `value` esta a menos de meio segundo de alguma marca de fade."""
    return any(abs(value - mark) <= _FADE_TOLERANCE_SECONDS for mark in marks)
