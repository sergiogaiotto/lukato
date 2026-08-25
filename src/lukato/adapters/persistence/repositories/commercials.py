"""Repositorio do catalogo de comerciais do AdWatch (SPEC-0010 secao 3.2, SPEC-0011 secao 4).

Implementa a porta :class:`~lukato.domain.ports.repositories.CommercialRepository`.
O agregado ocupa duas tabelas: `commercials` (catalogo com CRUD completo, chaveado pelo
codigo de negocio `commercial_id`) e `ad_fingerprints` (a assinatura de matching, no
maximo **uma** por comercial).

Regras proprias deste agregado:

* `get_by_code` resolve pelo codigo de negocio (`Commercial.commercial_id`), nunca pela
  chave primaria interna;
* `all_active` alimenta o indice de matching e por isso nao pagina — apenas respeita um
  teto de seguranca — devolvendo o catalogo em ordem estavel de codigo;
* `upsert_fingerprint` substitui a assinatura anterior preservando a linha existente;
* apagar o comercial leva a assinatura junto, via `ON DELETE CASCADE`.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import (
    commercial_apply,
    commercial_to_domain,
    fingerprint_apply,
    fingerprint_to_domain,
)
from lukato.adapters.persistence.orm import AdFingerprintRow, CommercialRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.adwatch import AdFingerprint, Commercial
from lukato.domain.types import Id

__all__ = [
    "MAX_ACTIVE_COMMERCIALS",
    "MAX_FINGERPRINTS",
    "SqlAlchemyCommercialRepository",
]

MAX_ACTIVE_COMMERCIALS: Final[int] = 50_000
"""Teto de seguranca de `all_active` (o contrato nao expoe paginacao neste metodo)."""

MAX_FINGERPRINTS: Final[int] = 50_000
"""Teto de seguranca de `list_fingerprints` (o contrato nao expoe paginacao)."""

_FILTER_KEYS: Final[frozenset[str]] = frozenset({"search", "brand", "campaign", "is_active"})
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


def _conditions(
    *,
    search: str | None = None,
    brand: str | None = None,
    campaign: str | None = None,
    is_active: bool | None = None,
) -> list[ColumnElement[bool]]:
    """Monta as condicoes WHERE compartilhadas por `list` e `count`."""
    clauses: list[ColumnElement[bool]] = []
    if brand:
        clauses.append(CommercialRow.brand == brand)
    if campaign:
        clauses.append(CommercialRow.campaign == campaign)
    if is_active is not None:
        clauses.append(CommercialRow.is_active.is_(bool(is_active)))
    if search:
        pattern = f"%{search.strip()}%"
        clauses.append(
            or_(
                CommercialRow.commercial_id.ilike(pattern),
                CommercialRow.brand.ilike(pattern),
                CommercialRow.campaign.ilike(pattern),
                CommercialRow.text.ilike(pattern),
            )
        )
    return clauses


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


def _filters_from(filters: dict[str, Any]) -> dict[str, Any]:
    """Valida `**filters` de `count`; `limit`/`offset` sao aceitos e ignorados."""
    unknown = sorted(set(filters) - _FILTER_KEYS - _PAGING_KEYS)
    if unknown:
        raise ValidationError(
            "filtro desconhecido para comerciais",
            details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
        )
    return {key: value for key, value in filters.items() if key in _FILTER_KEYS}


class SqlAlchemyCommercialRepository:
    """Catalogo de comerciais em SQL; implementa a porta `CommercialRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, commercial: Commercial) -> Commercial:
        """Insere o comercial; codigo de negocio duplicado gera `ConflictError`."""
        code = commercial.commercial_id
        if await self.get_by_code(code) is not None:
            raise ConflictError(
                f"ja existe um comercial com o codigo '{code}'",
                details={"commercial_id": code},
            )
        row = CommercialRow()
        commercial_apply(row, commercial)
        async with _translate("commercials.add"):
            self._session.add(row)
            await self._session.flush()
        return commercial_to_domain(row)

    async def get(self, commercial_id: Id) -> Commercial | None:
        """Busca o comercial por identificador interno; `None` quando nao existe."""
        row = await self._row(commercial_id, operation="commercials.get")
        return None if row is None else commercial_to_domain(row)

    async def get_by_code(self, code: str) -> Commercial | None:
        """Busca pelo codigo de negocio (`Commercial.commercial_id`)."""
        normalized = code.strip()
        if not normalized:
            return None
        statement = select(CommercialRow).where(CommercialRow.commercial_id == normalized).limit(1)
        async with _translate("commercials.get_by_code"):
            result = await self._session.execute(statement)
            row = result.scalars().first()
        return None if row is None else commercial_to_domain(row)

    async def list(
        self,
        *,
        search: str | None = None,
        brand: str | None = None,
        campaign: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Commercial]:
        """Lista comerciais em ordem estavel de codigo de negocio, sempre paginados."""
        bounded_limit, bounded_offset = _page(limit, offset)
        statement = (
            select(CommercialRow)
            .where(*_conditions(search=search, brand=brand, campaign=campaign, is_active=is_active))
            .order_by(CommercialRow.commercial_id.asc(), CommercialRow.id.asc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("commercials.list"):
            result = await self._session.execute(statement)
            rows: Sequence[CommercialRow] = result.scalars().all()
        return [commercial_to_domain(row) for row in rows]

    async def count(self, **filters: Any) -> int:
        """Conta comerciais com os mesmos filtros aceitos por `list`."""
        statement = select(func.count()).select_from(CommercialRow)
        statement = statement.where(*_conditions(**_filters_from(filters)))
        async with _translate("commercials.count"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def update(self, commercial: Commercial) -> Commercial:
        """Grava o comercial existente; ausente gera `NotFoundError`."""
        row = await self._require(commercial.id, operation="commercials.update")
        commercial_apply(row, commercial)
        async with _translate("commercials.update"):
            await self._session.flush()
        return commercial_to_domain(row)

    async def delete(self, commercial_id: Id) -> None:
        """Remove o comercial e, em cascata, sua assinatura; ausente gera `NotFoundError`."""
        row = await self._require(commercial_id, operation="commercials.delete")
        async with _translate("commercials.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def all_active(self) -> builtins.list[Commercial]:
        """Devolve todos os comerciais ativos, ordenados pelo codigo de negocio."""
        statement = (
            select(CommercialRow)
            .where(CommercialRow.is_active.is_(True))
            .order_by(CommercialRow.commercial_id.asc(), CommercialRow.id.asc())
            .limit(MAX_ACTIVE_COMMERCIALS)
        )
        async with _translate("commercials.all_active"):
            result = await self._session.execute(statement)
            rows: Sequence[CommercialRow] = result.scalars().all()
        return [commercial_to_domain(row) for row in rows]

    async def upsert_fingerprint(self, fp: AdFingerprint) -> AdFingerprint:
        """Grava a assinatura do comercial, substituindo a anterior se existir."""
        await self._require(fp.commercial_id, operation="commercials.upsert_fingerprint")
        row = await self._fingerprint_row(
            fp.commercial_id, operation="commercials.upsert_fingerprint"
        )
        is_new = row is None
        if row is None:
            row = AdFingerprintRow()
        fingerprint_apply(row, fp)
        async with _translate("commercials.upsert_fingerprint"):
            if is_new:
                self._session.add(row)
            await self._session.flush()
        return fingerprint_to_domain(row)

    async def get_fingerprint(self, commercial_id: Id) -> AdFingerprint | None:
        """Busca a assinatura de um comercial; `None` quando ainda nao foi gerada."""
        row = await self._fingerprint_row(commercial_id, operation="commercials.get_fingerprint")
        return None if row is None else fingerprint_to_domain(row)

    async def list_fingerprints(self) -> builtins.list[AdFingerprint]:
        """Lista todas as assinaturas disponiveis, em ordem estavel de comercial."""
        statement = (
            select(AdFingerprintRow)
            .order_by(AdFingerprintRow.commercial_id.asc(), AdFingerprintRow.id.asc())
            .limit(MAX_FINGERPRINTS)
        )
        async with _translate("commercials.list_fingerprints"):
            result = await self._session.execute(statement)
            rows: Sequence[AdFingerprintRow] = result.scalars().all()
        return [fingerprint_to_domain(row) for row in rows]

    async def _row(self, commercial_id: Id, *, operation: str) -> CommercialRow | None:
        """Carrega a linha bruta do comercial, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(CommercialRow, commercial_id)

    async def _require(self, commercial_id: Id, *, operation: str) -> CommercialRow:
        """Carrega a linha do comercial ou levanta `NotFoundError`."""
        row = await self._row(commercial_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"comercial nao encontrado: {commercial_id}",
                details={"commercial_id": commercial_id},
            )
        return row

    async def _fingerprint_row(
        self, commercial_id: Id, *, operation: str
    ) -> AdFingerprintRow | None:
        """Carrega a linha unica de assinatura do comercial informado."""
        statement = (
            select(AdFingerprintRow).where(AdFingerprintRow.commercial_id == commercial_id).limit(1)
        )
        async with _translate(operation):
            result = await self._session.execute(statement)
            return result.scalars().first()
