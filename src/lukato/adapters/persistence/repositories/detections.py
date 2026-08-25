"""Repositorio das deteccoes consolidadas do AdWatch (SPEC-0010 secao 3.6, SPEC-0011 secao 4).

Implementa a porta :class:`~lukato.domain.ports.repositories.DetectionRepository`. Uma
deteccao amarra um comercial do catalogo a um intervalo `[start, end]` de um ativo de
midia, junto com a evidencia por modalidade que sustenta a decisao.

Regras proprias deste agregado:

* `add_many` grava o lote em um unico flush e devolve os modelos **na ordem recebida**,
  que e a ordem em que o motor de matching produziu os candidatos;
* `list` ordena sempre pela linha do tempo (`start_seconds`), nao pela data de criacao —
  quem le uma deteccao quer a sequencia dos intervalos no video;
* `delete_by_media` e uma limpeza em lote: devolve a contagem e nao exige que a midia
  tenha deteccoes, enquanto `delete` de uma deteccao ausente gera `NotFoundError`.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import detection_apply, detection_to_domain
from lukato.adapters.persistence.orm import DetectionRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.adwatch import Detection, DetectionStatus
from lukato.domain.types import Id

__all__ = ["SqlAlchemyDetectionRepository"]

_FILTER_KEYS: Final[frozenset[str]] = frozenset({"media_id", "commercial_id", "status"})
_PAGING_KEYS: Final[frozenset[str]] = frozenset({"limit", "offset"})


@asynccontextmanager
async def _translate(operation: str) -> AsyncIterator[None]:
    """Converte erros do driver na hierarquia de erros do dominio."""
    try:
        yield
    except IntegrityError as exc:
        raise ConflictError(
            f"violacao de integridade em {operation}",
            details={"operation": operation, "error": str(exc.orig)},
        ) from exc
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha de persistencia em {operation}: {exc}",
            details={"operation": operation, "error": type(exc).__name__},
        ) from exc


def _as_status(value: Any) -> DetectionStatus | None:
    """Normaliza o filtro `status` para o enum do dominio."""
    if value is None or isinstance(value, DetectionStatus):
        return value
    try:
        return DetectionStatus(str(value))
    except ValueError as exc:
        raise ValidationError(
            f"status de deteccao invalido: {value!r}",
            details={"field": "status", "allowed": [item.value for item in DetectionStatus]},
        ) from exc


def _conditions(
    *,
    media_id: Id | None = None,
    commercial_id: Id | None = None,
    status: DetectionStatus | str | None = None,
) -> list[ColumnElement[bool]]:
    """Monta as condicoes WHERE compartilhadas por `list` e `count`."""
    clauses: list[ColumnElement[bool]] = []
    if media_id:
        clauses.append(DetectionRow.media_id == media_id)
    if commercial_id:
        clauses.append(DetectionRow.commercial_id == commercial_id)
    resolved = _as_status(status)
    if resolved is not None:
        clauses.append(DetectionRow.status == resolved.value)
    return clauses


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


def _filters_from(filters: dict[str, Any]) -> dict[str, Any]:
    """Valida `**filters` de `count`; `limit`/`offset` sao aceitos e ignorados."""
    unknown = sorted(set(filters) - _FILTER_KEYS - _PAGING_KEYS)
    if unknown:
        raise ValidationError(
            "filtro desconhecido para deteccoes",
            details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
        )
    return {key: value for key, value in filters.items() if key in _FILTER_KEYS}


class SqlAlchemyDetectionRepository:
    """Deteccoes em SQL; implementa a porta `DetectionRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, detection: Detection) -> Detection:
        """Insere uma deteccao e devolve o modelo de dominio gravado."""
        row = DetectionRow()
        detection_apply(row, detection)
        async with _translate("detections.add"):
            self._session.add(row)
            await self._session.flush()
        return detection_to_domain(row)

    async def add_many(self, detections: Sequence[Detection]) -> builtins.list[Detection]:
        """Insere varias deteccoes de uma vez, preservando a ordem recebida."""
        if not detections:
            return []
        rows: list[DetectionRow] = []
        for detection in detections:
            row = DetectionRow()
            detection_apply(row, detection)
            rows.append(row)
        async with _translate("detections.add_many"):
            self._session.add_all(rows)
            await self._session.flush()
        return [detection_to_domain(row) for row in rows]

    async def get(self, detection_id: Id) -> Detection | None:
        """Busca a deteccao por identificador; `None` quando nao existe."""
        row = await self._row(detection_id, operation="detections.get")
        return None if row is None else detection_to_domain(row)

    async def list(
        self,
        *,
        media_id: Id | None = None,
        commercial_id: Id | None = None,
        status: DetectionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Detection]:
        """Lista deteccoes na ordem da linha do tempo, sempre paginadas."""
        bounded_limit, bounded_offset = _page(limit, offset)
        statement = (
            select(DetectionRow)
            .where(*_conditions(media_id=media_id, commercial_id=commercial_id, status=status))
            .order_by(
                DetectionRow.start_seconds.asc(),
                DetectionRow.end_seconds.asc(),
                DetectionRow.id.asc(),
            )
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("detections.list"):
            result = await self._session.execute(statement)
            rows: Sequence[DetectionRow] = result.scalars().all()
        return [detection_to_domain(row) for row in rows]

    async def count(self, **filters: Any) -> int:
        """Conta deteccoes com os mesmos filtros aceitos por `list`."""
        statement = select(func.count()).select_from(DetectionRow)
        statement = statement.where(*_conditions(**_filters_from(filters)))
        async with _translate("detections.count"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def update(self, detection: Detection) -> Detection:
        """Grava a deteccao existente (revisao humana ou veredito do VLM)."""
        row = await self._require(detection.id, operation="detections.update")
        detection_apply(row, detection)
        async with _translate("detections.update"):
            await self._session.flush()
        return detection_to_domain(row)

    async def delete(self, detection_id: Id) -> None:
        """Remove a deteccao pelo identificador; ausente gera `NotFoundError`."""
        row = await self._require(detection_id, operation="detections.delete")
        async with _translate("detections.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def delete_by_media(self, media_id: Id) -> int:
        """Remove todas as deteccoes do ativo; devolve quantas foram apagadas."""
        statement = delete(DetectionRow).where(DetectionRow.media_id == media_id)
        async with _translate("detections.delete_by_media"):
            # DELETE/UPDATE devolvem CursorResult, o unico com `rowcount`;
            # os stubs do SQLAlchemy tipam `execute` como Result[Any].
            result = cast("CursorResult[Any]", await self._session.execute(statement))
            await self._session.flush()
        return int(result.rowcount or 0)

    async def _row(self, detection_id: Id, *, operation: str) -> DetectionRow | None:
        """Carrega a linha bruta da deteccao, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(DetectionRow, detection_id)

    async def _require(self, detection_id: Id, *, operation: str) -> DetectionRow:
        """Carrega a linha da deteccao ou levanta `NotFoundError`."""
        row = await self._row(detection_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"deteccao nao encontrada: {detection_id}",
                details={"detection_id": detection_id},
            )
        return row
