"""Tipos primitivos e utilitarios compartilhados por todo o dominio."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "DEFAULT_TENANT",
    "Id",
    "Json",
    "JsonValue",
    "new_id",
    "slugify",
    "utcnow",
]

Id = str
"""Identificador de entidade: UUID4 em formato hex-dash (`str(uuid.uuid4())`)."""

JsonValue = bool | int | float | str | None | list["JsonValue"] | dict[str, "JsonValue"]
"""Qualquer valor representavel em JSON."""

Json = dict[str, Any]
"""Objeto JSON livre (mapa de chaves textuais)."""

DEFAULT_TENANT = "default"
"""Tenant usado quando a requisicao nao informa um inquilino explicito."""

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_FALLBACK_SLUG = "item"


def new_id() -> Id:
    """Gera um novo identificador UUID4 em texto."""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Retorna o instante atual em UTC (timezone-aware)."""
    return datetime.now(UTC)


def slugify(value: str) -> str:
    """Converte um texto livre em slug `a-z0-9-` (sem acentos, sem hifens nas pontas).

    Retorna `"item"` quando nada aproveitavel sobra do texto original.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(char for char in decomposed if not unicodedata.combining(char))
    collapsed = _NON_SLUG_CHARS.sub("-", ascii_only.lower()).strip("-")
    return collapsed or _FALLBACK_SLUG
