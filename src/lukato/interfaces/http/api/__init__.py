"""Versoes da API HTTP do lukato.

Cada versao vive em um subpacote proprio e expoe um unico `APIRouter` agregador.
Hoje existe apenas :mod:`lukato.interfaces.http.api.v1`; uma `v2` conviveria aqui
lado a lado, sem quebrar quem ja integrou com a v1.
"""

from __future__ import annotations

__all__: list[str] = []
