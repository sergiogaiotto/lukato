"""OCR de quadros de video com PaddleOCR (SPEC-0010 secao 3.1).

PaddleOCR e OpenCV sao opcionais (`requirements-media.txt`). O import e preguicoso e
`available` so pergunta ao `importlib` se os pacotes existem — importar este modulo
nunca carrega PaddlePaddle nem quebra o boot.

O adaptador amostra quadros no intervalo pedido, na taxa `fps` informada (1 quadro por
segundo por padrao: legendas e selos de marca ficam varios segundos na tela, amostrar
a 30 fps multiplicaria o custo sem acrescentar sinal). Cada quadro passa pelo OCR e as
deteccoes com o **mesmo texto em instantes vizinhos** sao fundidas num unico `OcrText`
cobrindo todo o periodo em que aquele texto ficou visivel — e assim que o motor de
matching quer consumir: um texto com intervalo, nao uma repeticao por quadro.

A saida do PaddleOCR mudou de forma entre as series 2.x e 3.x; `_normalize_result`
aceita as duas (lista `[box, (texto, score)]` e objeto com `rec_texts`/`dt_polys`),
porque o adaptador precisa continuar valido qualquer que seja a versao instalada.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from dataclasses import dataclass
from typing import Any, Final

from lukato.config import get_logger
from lukato.domain.errors import ProviderError, UnsupportedCapability, ValidationError
from lukato.domain.models.adwatch import OcrText

__all__ = [
    "DEFAULT_LANGUAGE",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_SAMPLE_FPS",
    "INSTALL_HINT",
    "REQUIRED_PACKAGES",
    "PaddleOCRAdapter",
]

_logger = get_logger(__name__)

REQUIRED_PACKAGES: Final[tuple[str, ...]] = ("paddleocr", "cv2")
"""Pacotes exigidos: o motor de OCR e o leitor de quadros (OpenCV)."""

INSTALL_HINT: Final[str] = (
    "instale as dependencias multimodais com "
    "`pip install -r requirements-media.txt` (traz paddleocr e opencv) ou importe o "
    "OCR pronto em POST /api/v1/adwatch/media/{id}/ocr"
)
"""Mensagem util devolvida quando o OCR e pedido sem os pacotes."""

DEFAULT_LANGUAGE: Final[str] = "pt"
"""Idioma padrao do reconhecedor (o catalogo de comerciais e pt-BR)."""

DEFAULT_SAMPLE_FPS: Final[float] = 1.0
"""Taxa padrao de amostragem de quadros, em quadros por segundo."""

DEFAULT_MIN_CONFIDENCE: Final[float] = 0.5
"""Piso de confianca: abaixo disso o OCR costuma devolver ruido de compressao."""

MAX_SAMPLES: Final[int] = 3600
"""Teto de quadros por chamada, para um intervalo largo nao virar horas de CPU."""

_MERGE_TOLERANCE: Final[float] = 1.5
"""Multiplo do intervalo de amostragem ainda considerado 'instante vizinho'."""

_MIN_POLYGON_POINTS: Final[int] = 2
_BBOX_LEN: Final[int] = 4


@dataclass(slots=True)
class _Detection:
    """Texto reconhecido em um quadro, antes do agrupamento temporal."""

    text: str
    confidence: float
    bbox: tuple[int, int, int, int] | None
    at: float


class PaddleOCRAdapter:
    """Implementa `OCRPort` amostrando quadros e rodando PaddleOCR sobre eles."""

    def __init__(
        self,
        *,
        language: str = DEFAULT_LANGUAGE,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        use_angle_cls: bool = True,
    ) -> None:
        """Guarda a configuracao do reconhecedor; nada e carregado ainda."""
        self._language = language.strip() or DEFAULT_LANGUAGE
        self._min_confidence = min(1.0, max(0.0, float(min_confidence)))
        self._use_angle_cls = use_angle_cls
        self._available: bool | None = None
        self._engine: Any | None = None

    @property
    def available(self) -> bool:
        """True quando `paddleocr` e `cv2` estao instalados (deteccao memoizada)."""
        if self._available is None:
            missing = [name for name in REQUIRED_PACKAGES if not _module_installed(name)]
            self._available = not missing
            _logger.info(
                "media_capability_detected",
                capability="ocr",
                adapter="PaddleOCRAdapter",
                available=self._available,
                missing=missing,
                language=self._language,
            )
        return self._available

    @property
    def language(self) -> str:
        """Idioma configurado no reconhecedor."""
        return self._language

    async def extract(
        self, media_uri: str, *, start: float, end: float, fps: float = DEFAULT_SAMPLE_FPS
    ) -> list[OcrText]:
        """Extrai os textos do intervalo `[start, end]`, amostrando `fps` quadros/s."""
        source = media_uri.strip() if media_uri else ""
        if not source:
            raise ValidationError(
                "caminho de midia vazio em extract",
                details={"capability": "ocr"},
            )
        begin = max(0.0, float(start))
        finish = float(end)
        if finish < begin:
            raise ValidationError(
                f"intervalo invalido para OCR: exige start <= end, recebido "
                f"start={begin} end={finish}",
                details={"capability": "ocr", "start": begin, "end": finish},
            )
        rate = float(fps)
        if rate <= 0.0:
            raise ValidationError(
                f"taxa de amostragem deve ser positiva, recebido {fps!r}",
                details={"capability": "ocr", "fps": fps},
            )
        if not self.available:
            missing = [name for name in REQUIRED_PACKAGES if not _module_installed(name)]
            raise UnsupportedCapability(
                f"PaddleOCR indisponivel: nao e possivel extrair texto de {source}; {INSTALL_HINT}",
                details={
                    "capability": "ocr",
                    "missing": missing,
                    "hint": INSTALL_HINT,
                    "uri": source,
                },
            )
        return await asyncio.to_thread(self._extract_sync, source, begin, finish, rate)

    def unload(self) -> None:
        """Libera o motor de OCR mantido em memoria."""
        self._engine = None

    # ----------------------------------------------------------------- #
    # Execucao sincrona (roda em thread separada)
    # ----------------------------------------------------------------- #

    def _extract_sync(self, media_uri: str, start: float, end: float, fps: float) -> list[OcrText]:
        """Amostra os quadros, roda OCR em cada um e agrupa por texto contiguo."""
        cv2 = _import_module("cv2")
        engine = self._load_engine()
        interval = 1.0 / fps
        detections: list[_Detection] = []
        capture = None
        try:
            capture = cv2.VideoCapture(media_uri)
            if not capture.isOpened():
                raise ProviderError(
                    f"OpenCV nao conseguiu abrir {media_uri} para OCR",
                    details={"capability": "ocr", "uri": media_uri},
                )
            for instant in _sample_instants(start, end, interval):
                capture.set(cv2.CAP_PROP_POS_MSEC, instant * 1000.0)
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                detections.extend(self._read_frame(engine, frame, instant))
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"PaddleOCR falhou ao processar {media_uri}: {exc}",
                details={
                    "capability": "ocr",
                    "uri": media_uri,
                    "error": type(exc).__name__,
                },
            ) from exc
        finally:
            if capture is not None:
                capture.release()
        texts = _group(detections, interval=interval)
        _logger.info(
            "ocr_extraction_ready",
            uri=media_uri,
            start=start,
            end=end,
            fps=fps,
            detections=len(detections),
            texts=len(texts),
        )
        return texts

    def _read_frame(self, engine: Any, frame: Any, instant: float) -> list[_Detection]:
        """Roda o OCR em um quadro e devolve as deteccoes acima do piso de confianca."""
        raw = _run_engine(engine, frame)
        found: list[_Detection] = []
        for text, confidence, polygon in _normalize_result(raw):
            if not text or confidence < self._min_confidence:
                continue
            found.append(
                _Detection(
                    text=text,
                    confidence=confidence,
                    bbox=_to_bbox(polygon),
                    at=instant,
                )
            )
        return found

    def _load_engine(self) -> Any:
        """Instancia o `PaddleOCR` uma unica vez por processo."""
        if self._engine is None:
            paddleocr = _import_module("paddleocr")
            _logger.info("paddleocr_engine_loading", language=self._language)
            self._engine = _construct_engine(paddleocr, self._language, self._use_angle_cls)
        return self._engine


# --------------------------------------------------------------------------- #
# Import preguicoso e compatibilidade entre versoes do PaddleOCR
# --------------------------------------------------------------------------- #


def _module_installed(name: str) -> bool:
    """True quando o modulo pode ser importado, sem de fato importa-lo."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _import_module(name: str) -> Any:
    """Importa o modulo opcional sob demanda, com erro de dominio claro."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise UnsupportedCapability(
            f"pacote {name!r} declarado disponivel mas o import falhou: {exc}; {INSTALL_HINT}",
            details={"capability": "ocr", "package": name, "hint": INSTALL_HINT},
        ) from exc


def _construct_engine(paddleocr: Any, language: str, use_angle_cls: bool) -> Any:
    """Instancia `PaddleOCR` aceitando a assinatura das series 2.x e 3.x."""
    factory = paddleocr.PaddleOCR
    try:
        return factory(lang=language, use_angle_cls=use_angle_cls, show_log=False)
    except TypeError:
        try:
            return factory(lang=language, use_angle_cls=use_angle_cls)
        except TypeError:
            return factory(lang=language)


def _run_engine(engine: Any, frame: Any) -> Any:
    """Chama o metodo de inferencia disponivel na versao instalada."""
    predict = getattr(engine, "predict", None)
    if callable(predict):
        return predict(frame)
    return engine.ocr(frame)


def _normalize_result(raw: Any) -> list[tuple[str, float, Any]]:
    """Achata a saida do PaddleOCR em `(texto, confianca, poligono)`.

    Cobre as duas formas conhecidas: a lista aninhada das versoes 2.x
    (`[[[box, (texto, score)], ...]]`) e o objeto por pagina das versoes 3.x
    (`{"rec_texts": [...], "rec_scores": [...], "dt_polys": [...]}`).
    """
    if raw is None:
        return []
    pages = raw if isinstance(raw, (list, tuple)) else [raw]
    found: list[tuple[str, float, Any]] = []
    for page in pages:
        if isinstance(page, dict):
            found.extend(_from_mapping(page))
        elif isinstance(page, (list, tuple)):
            found.extend(_from_lines(page))
        elif hasattr(page, "get"):
            found.extend(_from_mapping(page))
    return found


def _from_mapping(page: Any) -> list[tuple[str, float, Any]]:
    """Le o formato 3.x, com listas paralelas de textos, scores e poligonos."""
    texts = page.get("rec_texts") or page.get("texts") or []
    scores = page.get("rec_scores") or page.get("scores") or []
    polygons = page.get("dt_polys") or page.get("boxes") or []
    found: list[tuple[str, float, Any]] = []
    for position, text in enumerate(texts):
        score = scores[position] if position < len(scores) else 1.0
        polygon = polygons[position] if position < len(polygons) else None
        found.append((_clean(text), _ratio(score), polygon))
    return found


def _from_lines(page: Any) -> list[tuple[str, float, Any]]:
    """Le o formato 2.x, com uma linha `[box, (texto, score)]` por deteccao."""
    found: list[tuple[str, float, Any]] = []
    for line in page:
        if isinstance(line, (list, tuple)) and len(line) >= _MIN_POLYGON_POINTS:
            polygon, payload = line[0], line[1]
            if isinstance(payload, (list, tuple)) and payload:
                text = _clean(payload[0])
                score = _ratio(payload[1]) if len(payload) > 1 else 1.0
                found.append((text, score, polygon))
            elif isinstance(payload, str):
                found.append((_clean(payload), 1.0, polygon))
        elif isinstance(line, (list, tuple)):
            found.extend(_from_lines(line))
    return found


def _clean(value: Any) -> str:
    """Normaliza o texto reconhecido (sem espacos nas pontas)."""
    return str(value).strip() if value is not None else ""


def _ratio(value: Any) -> float:
    """Converte a confianca para `[0.0, 1.0]`, tolerando valores estranhos."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return min(1.0, max(0.0, number))


def _to_bbox(polygon: Any) -> tuple[int, int, int, int] | None:
    """Converte o poligono do detector na caixa `(x1, y1, x2, y2)`."""
    if polygon is None:
        return None
    points = list(polygon) if not isinstance(polygon, (list, tuple)) else polygon
    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        if isinstance(point, (list, tuple)) and len(point) >= _MIN_POLYGON_POINTS:
            xs.append(_number(point[0]))
            ys.append(_number(point[1]))
    if xs and ys:
        return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
    flat = [_number(value) for value in points if isinstance(value, (int, float))]
    if len(flat) == _BBOX_LEN:
        return (int(flat[0]), int(flat[1]), int(flat[2]), int(flat[3]))
    return None


def _number(value: Any) -> float:
    """Converte para float com tolerancia a tipos do numpy."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sample_instants(start: float, end: float, interval: float) -> list[float]:
    """Instantes a amostrar dentro do intervalo, respeitando o teto de quadros."""
    span = max(0.0, end - start)
    count = min(MAX_SAMPLES, int(span / interval) + 1)
    return [start + position * interval for position in range(count)]


def _group(detections: list[_Detection], *, interval: float) -> list[OcrText]:
    """Funde deteccoes do mesmo texto em instantes vizinhos num unico `OcrText`."""
    ordered = sorted(detections, key=lambda item: (item.text, item.at))
    tolerance = interval * _MERGE_TOLERANCE
    grouped: list[OcrText] = []
    current: _Detection | None = None
    last_at = 0.0
    best_confidence = 0.0
    best_bbox: tuple[int, int, int, int] | None = None

    def flush() -> None:
        """Fecha o grupo corrente e o adiciona ao resultado."""
        if current is None:
            return
        grouped.append(
            OcrText(
                text=current.text,
                start=current.at,
                end=last_at + interval,
                confidence=best_confidence,
                bbox=best_bbox,
            )
        )

    for detection in ordered:
        if (
            current is not None
            and detection.text == current.text
            and detection.at - last_at <= tolerance
        ):
            last_at = detection.at
            if detection.confidence > best_confidence:
                best_confidence = detection.confidence
                best_bbox = detection.bbox
            continue
        flush()
        current = detection
        last_at = detection.at
        best_confidence = detection.confidence
        best_bbox = detection.bbox
    flush()
    grouped.sort(key=lambda item: (item.start, item.end, item.text))
    return grouped
