"""Importadores JSON de transcricao, cenas e OCR (SPEC-0010 secao 3.1).

Este e o **caminho offline** que torna o pipeline inteiro do AdWatch executavel sem
FFmpeg, sem GPU e sem rede: em vez de extrair a linha do tempo multimodal do arquivo
de video, ela e importada pronta. E tambem o caminho usado pelos testes.

As tres classes sao **puras e deterministicas** — nao abrem arquivo, nao chamam rede,
nao consultam relogio. Recebem um payload ja desserializado e devolvem modelos de
dominio, ou levantam `ValidationError` apontando o **indice** do item problematico:
numa transcricao de dez mil palavras, "campo start ausente" sem o indice e inutil.

Formatos aceitos por `TranscriptImporter`:

```python
[{"word": "aproveite", "start": 12.0, "end": 12.4}]          # lista simples
{"words": [...]}                                              # objeto com words
{"segments": [{"words": [...]}, ...]}                         # saida do WhisperX
```

Sinonimos tolerados em qualquer formato: `text` no lugar de `word`, `start_time` /
`end_time` no lugar de `start` / `end`, `confidence` / `probability` no lugar de
`score`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Final

from lukato.domain.errors import ValidationError
from lukato.domain.models.adwatch import OcrText, SceneCut, TranscriptWord
from lukato.domain.types import Json

__all__ = [
    "OCR_CONTAINER_KEYS",
    "SCENE_CONTAINER_KEYS",
    "SCENE_KINDS",
    "TRANSCRIPT_CONTAINER_KEYS",
    "OcrImporter",
    "SceneImporter",
    "TranscriptImporter",
]

TRANSCRIPT_CONTAINER_KEYS: Final[tuple[str, ...]] = ("words", "word_segments", "transcript")
"""Chaves em que uma lista de palavras pode estar embrulhada."""

SCENE_CONTAINER_KEYS: Final[tuple[str, ...]] = ("scenes", "cuts", "scene_list", "items")
"""Chaves em que uma lista de cenas pode estar embrulhada."""

OCR_CONTAINER_KEYS: Final[tuple[str, ...]] = ("ocr", "texts", "results", "items")
"""Chaves em que uma lista de textos de OCR pode estar embrulhada."""

SCENE_KINDS: Final[frozenset[str]] = frozenset({"cut", "fade"})
"""Tipos de fronteira aceitos por `SceneCut.kind`."""

_WORD_FIELDS: Final[tuple[str, ...]] = ("word", "text", "value")
_START_FIELDS: Final[tuple[str, ...]] = ("start", "start_time", "start_seconds", "from")
_END_FIELDS: Final[tuple[str, ...]] = ("end", "end_time", "end_seconds", "to")
_SCORE_FIELDS: Final[tuple[str, ...]] = ("score", "confidence", "probability", "conf")
_TEXT_FIELDS: Final[tuple[str, ...]] = ("text", "content", "value", "label")
_BBOX_FIELDS: Final[tuple[str, ...]] = ("bbox", "box", "polygon", "points", "quad")
_PAIR_LEN: Final[int] = 2
_BBOX_LEN: Final[int] = 4
_POINT_LEN: Final[int] = 2


class TranscriptImporter:
    """Converte JSON de transcricao (WhisperX ou simples) em `TranscriptWord`s."""

    @staticmethod
    def parse(payload: Json | list[Any]) -> list[TranscriptWord]:
        """Le o payload, valida cada palavra e devolve a lista ordenada por `start`."""
        entries = _flatten_transcript(payload)
        words: list[TranscriptWord] = []
        for index, entry in enumerate(entries):
            words.append(_to_word(entry, index=index))
        words.sort(key=lambda word: (word.start, word.end))
        return words


class SceneImporter:
    """Converte JSON de cortes de cena em `SceneCut`s, reindexados por tempo."""

    @staticmethod
    def parse(payload: Json | list[Any]) -> list[SceneCut]:
        """Le o payload, valida cada corte e devolve a lista ordenada por `start`."""
        entries = _as_entries(payload, SCENE_CONTAINER_KEYS, what="cortes de cena")
        cuts: list[SceneCut] = []
        for index, entry in enumerate(entries):
            cuts.append(_to_scene(entry, index=index))
        cuts.sort(key=lambda cut: (cut.start, cut.end))
        return [cut.model_copy(update={"index": position}) for position, cut in enumerate(cuts)]


class OcrImporter:
    """Converte JSON de OCR em `OcrText`s com intervalo e caixa delimitadora."""

    @staticmethod
    def parse(payload: Json | list[Any]) -> list[OcrText]:
        """Le o payload, valida cada texto e devolve a lista ordenada por `start`."""
        entries = _as_entries(payload, OCR_CONTAINER_KEYS, what="textos de OCR")
        texts: list[OcrText] = []
        for index, entry in enumerate(entries):
            texts.append(_to_ocr(entry, index=index))
        texts.sort(key=lambda item: (item.start, item.end))
        return texts


# --------------------------------------------------------------------------- #
# Desembrulho dos formatos aceitos
# --------------------------------------------------------------------------- #


def _as_entries(
    payload: Json | list[Any] | None,
    container_keys: Sequence[str],
    *,
    what: str,
) -> list[Any]:
    """Aceita lista direta ou objeto com uma das chaves de container conhecidas."""
    if payload is None:
        raise ValidationError(
            f"payload de {what} ausente",
            details={"expected": list(container_keys)},
        )
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, dict):
        for key in container_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return list(value)
        raise ValidationError(
            f"payload de {what} sem lista reconhecida; use uma lista JSON ou um objeto "
            f"com uma das chaves {list(container_keys)}",
            details={"received_keys": sorted(payload)[:20], "expected": list(container_keys)},
        )
    raise ValidationError(
        f"payload de {what} deve ser lista ou objeto JSON, recebido {type(payload).__name__}",
        details={"received_type": type(payload).__name__},
    )


def _flatten_transcript(payload: Json | list[Any] | None) -> list[Any]:
    """Achata o formato WhisperX (`segments[].words[]`) numa unica lista de palavras."""
    if isinstance(payload, dict):
        segments = payload.get("segments")
        if isinstance(segments, list):
            return list(_words_from_segments(segments))
    return _as_entries(payload, TRANSCRIPT_CONTAINER_KEYS, what="transcricao")


def _words_from_segments(segments: Iterable[Any]) -> Iterable[Any]:
    """Percorre os segmentos do WhisperX e produz as palavras em ordem de leitura."""
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValidationError(
                f"segmento {position} da transcricao deve ser um objeto JSON, "
                f"recebido {type(segment).__name__}",
                details={"segment_index": position, "received_type": type(segment).__name__},
            )
        words = segment.get("words")
        if words is None:
            continue
        if not isinstance(words, list):
            raise ValidationError(
                f"campo 'words' do segmento {position} deve ser uma lista, "
                f"recebido {type(words).__name__}",
                details={"segment_index": position, "received_type": type(words).__name__},
            )
        yield from words


# --------------------------------------------------------------------------- #
# Conversao item a item
# --------------------------------------------------------------------------- #


def _to_word(entry: Any, *, index: int) -> TranscriptWord:
    """Converte um item de transcricao em `TranscriptWord` validado."""
    item = _as_object(entry, index=index, what="palavra")
    text = _require_text(item, _WORD_FIELDS, index=index, what="palavra")
    start = _require_float(item, _START_FIELDS, index=index, what="palavra")
    end = _require_float(item, _END_FIELDS, index=index, what="palavra")
    _check_interval(start, end, index=index, what="palavra")
    speaker = item.get("speaker")
    return TranscriptWord(
        word=text,
        start=start,
        end=end,
        score=_optional_ratio(item, _SCORE_FIELDS, index=index, what="palavra", default=1.0),
        speaker=str(speaker) if isinstance(speaker, str) and speaker.strip() else None,
    )


def _to_scene(entry: Any, *, index: int) -> SceneCut:
    """Converte um item de cena em `SceneCut` validado (aceita tambem `[start, end]`)."""
    if isinstance(entry, (list, tuple)):
        if len(entry) != _PAIR_LEN:
            raise ValidationError(
                f"corte de cena {index} em formato de par deve ter exatamente "
                f"[start, end], recebido {len(entry)} elementos",
                details={"index": index, "length": len(entry)},
            )
        start = _coerce_float(entry[0], index=index, field="start", what="corte de cena")
        end = _coerce_float(entry[1], index=index, field="end", what="corte de cena")
        _check_interval(start, end, index=index, what="corte de cena")
        return SceneCut(index=index, start=start, end=end, kind="cut")

    item = _as_object(entry, index=index, what="corte de cena")
    start = _require_float(item, _START_FIELDS, index=index, what="corte de cena")
    end = _require_float(item, _END_FIELDS, index=index, what="corte de cena")
    _check_interval(start, end, index=index, what="corte de cena")
    return SceneCut(index=index, start=start, end=end, kind=_scene_kind(item, index=index))


def _to_ocr(entry: Any, *, index: int) -> OcrText:
    """Converte um item de OCR em `OcrText` validado."""
    item = _as_object(entry, index=index, what="texto de OCR")
    text = _require_text(item, _TEXT_FIELDS, index=index, what="texto de OCR")
    start = _require_float(item, _START_FIELDS, index=index, what="texto de OCR")
    end = _optional_float(item, _END_FIELDS, index=index, what="texto de OCR", default=start)
    _check_interval(start, end, index=index, what="texto de OCR")
    return OcrText(
        text=text,
        start=start,
        end=end,
        confidence=_optional_ratio(
            item, _SCORE_FIELDS, index=index, what="texto de OCR", default=1.0
        ),
        bbox=_bbox(item, index=index),
    )


def _scene_kind(item: dict[str, Any], *, index: int) -> str:
    """Normaliza `kind` para `cut`/`fade`; qualquer outro valor e recusado."""
    raw = item.get("kind", item.get("type", "cut"))
    kind = str(raw).strip().lower() if raw is not None else "cut"
    if kind not in SCENE_KINDS:
        raise ValidationError(
            f"corte de cena {index} tem kind {raw!r} desconhecido; use {sorted(SCENE_KINDS)}",
            details={"index": index, "kind": raw, "allowed": sorted(SCENE_KINDS)},
        )
    return kind


def _bbox(item: dict[str, Any], *, index: int) -> tuple[int, int, int, int] | None:
    """Le a caixa delimitadora: `[x1,y1,x2,y2]`, poligono de 4 pontos ou objeto x/y/w/h."""
    for field in _BBOX_FIELDS:
        raw = item.get(field)
        if raw is None:
            continue
        box = _bbox_from_value(raw, index=index, field=field)
        if box is not None:
            return box
    if all(key in item for key in ("x", "y")):
        x = _coerce_float(item.get("x"), index=index, field="x", what="texto de OCR")
        y = _coerce_float(item.get("y"), index=index, field="y", what="texto de OCR")
        width = _coerce_float(
            item.get("width", item.get("w", 0)), index=index, field="width", what="texto de OCR"
        )
        height = _coerce_float(
            item.get("height", item.get("h", 0)), index=index, field="height", what="texto de OCR"
        )
        return (int(x), int(y), int(x + width), int(y + height))
    return None


def _bbox_from_value(raw: Any, *, index: int, field: str) -> tuple[int, int, int, int] | None:
    """Converte lista de 4 numeros ou poligono de pontos em `(x1, y1, x2, y2)`."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValidationError(
            f"campo {field!r} do texto de OCR {index} deve ser uma lista de 4 numeros "
            f"ou um poligono de pontos",
            details={"index": index, "field": field, "received_type": type(raw).__name__},
        )
    if all(isinstance(point, (list, tuple)) and len(point) >= _POINT_LEN for point in raw):
        xs = [_coerce_float(p[0], index=index, field=field, what="texto de OCR") for p in raw]
        ys = [_coerce_float(p[1], index=index, field=field, what="texto de OCR") for p in raw]
        return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
    if len(raw) == _BBOX_LEN:
        values = [
            _coerce_float(value, index=index, field=field, what="texto de OCR") for value in raw
        ]
        return (int(values[0]), int(values[1]), int(values[2]), int(values[3]))
    raise ValidationError(
        f"campo {field!r} do texto de OCR {index} precisa de 4 numeros ou 4 pontos, "
        f"recebido {len(raw)} elementos",
        details={"index": index, "field": field, "length": len(raw)},
    )


# --------------------------------------------------------------------------- #
# Coercoes com erro indexado
# --------------------------------------------------------------------------- #


def _as_object(entry: Any, *, index: int, what: str) -> dict[str, Any]:
    """Garante que o item e um objeto JSON."""
    if not isinstance(entry, dict):
        raise ValidationError(
            f"{what} {index} deve ser um objeto JSON, recebido {type(entry).__name__}",
            details={"index": index, "received_type": type(entry).__name__},
        )
    return entry


def _pick(item: dict[str, Any], fields: Sequence[str]) -> tuple[str, Any] | None:
    """Devolve o primeiro `(campo, valor)` presente entre os sinonimos aceitos."""
    for field in fields:
        if field in item and item[field] is not None:
            return field, item[field]
    return None


def _require_text(item: dict[str, Any], fields: Sequence[str], *, index: int, what: str) -> str:
    """Le um campo textual obrigatorio e nao vazio."""
    found = _pick(item, fields)
    if found is None:
        raise ValidationError(
            f"{what} {index} sem campo de texto; informe um de {list(fields)}",
            details={"index": index, "expected_fields": list(fields)},
        )
    field, raw = found
    text = str(raw).strip()
    if not text:
        raise ValidationError(
            f"{what} {index} tem {field!r} vazio",
            details={"index": index, "field": field},
        )
    return text


def _require_float(item: dict[str, Any], fields: Sequence[str], *, index: int, what: str) -> float:
    """Le um campo numerico obrigatorio (em segundos)."""
    found = _pick(item, fields)
    if found is None:
        raise ValidationError(
            f"{what} {index} sem marcacao temporal; informe um de {list(fields)}",
            details={"index": index, "expected_fields": list(fields)},
        )
    field, raw = found
    return _coerce_float(raw, index=index, field=field, what=what)


def _optional_float(
    item: dict[str, Any], fields: Sequence[str], *, index: int, what: str, default: float
) -> float:
    """Le um campo numerico opcional, caindo no padrao quando ausente."""
    found = _pick(item, fields)
    if found is None:
        return default
    field, raw = found
    return _coerce_float(raw, index=index, field=field, what=what)


def _optional_ratio(
    item: dict[str, Any], fields: Sequence[str], *, index: int, what: str, default: float
) -> float:
    """Le uma confianca opcional e a apara para `[0.0, 1.0]`."""
    value = _optional_float(item, fields, index=index, what=what, default=default)
    return min(1.0, max(0.0, value))


def _coerce_float(raw: Any, *, index: int, field: str, what: str) -> float:
    """Converte para float, recusando texto nao numerico, booleano, NaN e infinito."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValidationError(
            f"{what} {index} tem {field!r} nao numerico ({type(raw).__name__})",
            details={"index": index, "field": field, "received_type": type(raw).__name__},
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{what} {index} tem {field!r} nao numerico: {raw!r}",
            details={"index": index, "field": field, "value": str(raw)[:80]},
        ) from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError(
            f"{what} {index} tem {field!r} fora do dominio real: {raw!r}",
            details={"index": index, "field": field, "value": str(raw)[:80]},
        )
    return value


def _check_interval(start: float, end: float, *, index: int, what: str) -> None:
    """Exige `0 <= start <= end` para o item informado."""
    if start < 0.0:
        raise ValidationError(
            f"{what} {index} tem start negativo ({start})",
            details={"index": index, "field": "start", "start": start},
        )
    if end < start:
        raise ValidationError(
            f"{what} {index} tem intervalo invalido: exige start <= end, "
            f"recebido start={start} end={end}",
            details={"index": index, "start": start, "end": end},
        )
