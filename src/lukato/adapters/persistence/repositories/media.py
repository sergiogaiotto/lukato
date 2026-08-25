"""Repositorio de ativos de midia do AdWatch (SPEC-0010 secao 3.1, SPEC-0011 secao 4).

Implementa a porta :class:`~lukato.domain.ports.repositories.MediaRepository`. O ativo
`media_assets` e a raiz do agregado; transcricao, cortes de cena e OCR sao artefatos
derivados que so existem enquanto o ativo existir (`ON DELETE CASCADE`).

Regras proprias deste agregado:

* `save_transcript` e um **upsert**: ha no maximo uma transcricao por midia e a linha
  existente e reaproveitada, preservando sua chave primaria;
* `save_scenes` e `save_ocr` sao **idempotentes por reposicao**: apagam tudo o que a
  midia ja tinha e inserem o lote recebido, devolvendo quantos registros ficaram;
* `SceneCut` e `OcrText` sao value objects sem identidade propria — a chave primaria da
  linha e gerada aqui (`new_id`) e o vinculo com a midia entra pelo mapper;
* `list_scenes` ordena por `position` e `list_ocr` por `start_seconds`.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from lukato.adapters.persistence.mappers import (
    media_apply,
    media_to_domain,
    ocr_apply,
    ocr_to_domain,
    scene_apply,
    scene_to_domain,
    transcript_apply,
    transcript_to_domain,
)
from lukato.adapters.persistence.orm import MediaAssetRow, OcrTextRow, SceneCutRow, TranscriptRow
from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.adwatch import MediaAsset, OcrText, SceneCut, Transcript
from lukato.domain.types import Id, new_id

__all__ = [
    "MAX_OCR_PER_MEDIA",
    "MAX_SCENES_PER_MEDIA",
    "SqlAlchemyMediaRepository",
]

MAX_SCENES_PER_MEDIA: Final[int] = 50_000
"""Teto de seguranca de `list_scenes` (o contrato nao expoe paginacao neste metodo)."""

MAX_OCR_PER_MEDIA: Final[int] = 200_000
"""Teto de seguranca de `list_ocr` (uma hora de video a 1 fps ja gera milhares de linhas)."""

_FILTER_KEYS: Final[frozenset[str]] = frozenset({"status", "search"})
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
    *, status: str | None = None, search: str | None = None
) -> list[ColumnElement[bool]]:
    """Monta as condicoes WHERE compartilhadas por `list` e `count`."""
    clauses: list[ColumnElement[bool]] = []
    if status:
        clauses.append(MediaAssetRow.status == status)
    if search:
        pattern = f"%{search.strip()}%"
        clauses.append(or_(MediaAssetRow.title.ilike(pattern), MediaAssetRow.uri.ilike(pattern)))
    return clauses


def _page(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza paginacao: `limit` sempre positivo e `offset` nunca negativo."""
    return max(1, int(limit)), max(0, int(offset))


def _filters_from(filters: dict[str, Any]) -> dict[str, Any]:
    """Valida `**filters` de `count`; `limit`/`offset` sao aceitos e ignorados."""
    unknown = sorted(set(filters) - _FILTER_KEYS - _PAGING_KEYS)
    if unknown:
        raise ValidationError(
            "filtro desconhecido para ativos de midia",
            details={"unknown": unknown, "supported": sorted(_FILTER_KEYS)},
        )
    return {key: value for key, value in filters.items() if key in _FILTER_KEYS}


class SqlAlchemyMediaRepository:
    """Ativos de midia e artefatos derivados em SQL; implementa `MediaRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        """Guarda a sessao da transacao corrente (o commit e do `UnitOfWork`)."""
        self._session = session

    async def add(self, asset: MediaAsset) -> MediaAsset:
        """Registra o ativo de midia e devolve o modelo de dominio gravado."""
        row = MediaAssetRow()
        media_apply(row, asset)
        async with _translate("media.add"):
            self._session.add(row)
            await self._session.flush()
        return media_to_domain(row)

    async def get(self, media_id: Id) -> MediaAsset | None:
        """Busca o ativo por identificador; `None` quando nao existe."""
        row = await self._row(media_id, operation="media.get")
        return None if row is None else media_to_domain(row)

    async def list(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[MediaAsset]:
        """Lista ativos do mais recente para o mais antigo, sempre paginados."""
        bounded_limit, bounded_offset = _page(limit, offset)
        statement = (
            select(MediaAssetRow)
            .where(*_conditions(status=status, search=search))
            .order_by(MediaAssetRow.created_at.desc(), MediaAssetRow.id.desc())
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
        async with _translate("media.list"):
            result = await self._session.execute(statement)
            rows: Sequence[MediaAssetRow] = result.scalars().all()
        return [media_to_domain(row) for row in rows]

    async def count(self, **filters: Any) -> int:
        """Conta ativos com os mesmos filtros aceitos por `list`."""
        statement = select(func.count()).select_from(MediaAssetRow)
        statement = statement.where(*_conditions(**_filters_from(filters)))
        async with _translate("media.count"):
            result = await self._session.execute(statement)
        return int(result.scalar_one())

    async def update(self, asset: MediaAsset) -> MediaAsset:
        """Grava o ativo existente (duracao, fps, status); ausente gera `NotFoundError`."""
        row = await self._require(asset.id, operation="media.update")
        media_apply(row, asset)
        async with _translate("media.update"):
            await self._session.flush()
        return media_to_domain(row)

    async def delete(self, media_id: Id) -> None:
        """Remove o ativo e, em cascata, transcricao, cenas, OCR e deteccoes."""
        row = await self._require(media_id, operation="media.delete")
        async with _translate("media.delete"):
            await self._session.delete(row)
            await self._session.flush()

    async def save_transcript(self, transcript: Transcript) -> Transcript:
        """Grava a transcricao do ativo, substituindo a anterior se existir."""
        await self._require(transcript.media_id, operation="media.save_transcript")
        statement = (
            select(TranscriptRow).where(TranscriptRow.media_id == transcript.media_id).limit(1)
        )
        async with _translate("media.save_transcript"):
            result = await self._session.execute(statement)
            row = result.scalars().first()
        is_new = row is None
        if row is None:
            row = TranscriptRow()
        transcript_apply(row, transcript)
        async with _translate("media.save_transcript"):
            if is_new:
                self._session.add(row)
            await self._session.flush()
        return transcript_to_domain(row)

    async def get_transcript(self, media_id: Id) -> Transcript | None:
        """Busca a transcricao do ativo; `None` quando ainda nao foi importada."""
        statement = select(TranscriptRow).where(TranscriptRow.media_id == media_id).limit(1)
        async with _translate("media.get_transcript"):
            result = await self._session.execute(statement)
            row = result.scalars().first()
        return None if row is None else transcript_to_domain(row)

    async def save_scenes(self, media_id: Id, scenes: Sequence[SceneCut]) -> int:
        """Substitui os cortes de cena do ativo; devolve quantos foram gravados."""
        await self._require(media_id, operation="media.save_scenes")
        await self._purge(SceneCutRow, media_id, operation="media.save_scenes")
        rows: list[SceneCutRow] = []
        for scene in scenes:
            row = SceneCutRow(id=new_id())
            scene_apply(row, scene, media_id=media_id)
            rows.append(row)
        if not rows:
            return 0
        async with _translate("media.save_scenes"):
            self._session.add_all(rows)
            await self._session.flush()
        return len(rows)

    async def list_scenes(self, media_id: Id) -> builtins.list[SceneCut]:
        """Lista os cortes de cena em ordem temporal (`position`)."""
        statement = (
            select(SceneCutRow)
            .where(SceneCutRow.media_id == media_id)
            .order_by(SceneCutRow.position.asc(), SceneCutRow.start_seconds.asc())
            .limit(MAX_SCENES_PER_MEDIA)
        )
        async with _translate("media.list_scenes"):
            result = await self._session.execute(statement)
            rows: Sequence[SceneCutRow] = result.scalars().all()
        return [scene_to_domain(row) for row in rows]

    async def save_ocr(self, media_id: Id, texts: Sequence[OcrText]) -> int:
        """Substitui os textos de OCR do ativo; devolve quantos foram gravados."""
        await self._require(media_id, operation="media.save_ocr")
        await self._purge(OcrTextRow, media_id, operation="media.save_ocr")
        rows: list[OcrTextRow] = []
        for text in texts:
            row = OcrTextRow(id=new_id())
            ocr_apply(row, text, media_id=media_id)
            rows.append(row)
        if not rows:
            return 0
        async with _translate("media.save_ocr"):
            self._session.add_all(rows)
            await self._session.flush()
        return len(rows)

    async def list_ocr(self, media_id: Id) -> builtins.list[OcrText]:
        """Lista os textos de OCR em ordem temporal (`start_seconds`)."""
        statement = (
            select(OcrTextRow)
            .where(OcrTextRow.media_id == media_id)
            .order_by(OcrTextRow.start_seconds.asc(), OcrTextRow.end_seconds.asc())
            .limit(MAX_OCR_PER_MEDIA)
        )
        async with _translate("media.list_ocr"):
            result = await self._session.execute(statement)
            rows: Sequence[OcrTextRow] = result.scalars().all()
        return [ocr_to_domain(row) for row in rows]

    async def _purge(
        self, row_type: type[SceneCutRow] | type[OcrTextRow], media_id: Id, *, operation: str
    ) -> int:
        """Apaga os artefatos derivados do ativo; devolve quantos foram removidos."""
        statement = delete(row_type).where(row_type.media_id == media_id)
        async with _translate(operation):
            # DELETE/UPDATE devolvem CursorResult, o unico com `rowcount`;
            # os stubs do SQLAlchemy tipam `execute` como Result[Any].
            result = cast("CursorResult[Any]", await self._session.execute(statement))
            await self._session.flush()
        return int(result.rowcount or 0)

    async def _row(self, media_id: Id, *, operation: str) -> MediaAssetRow | None:
        """Carrega a linha bruta do ativo, traduzindo erros do driver."""
        async with _translate(operation):
            return await self._session.get(MediaAssetRow, media_id)

    async def _require(self, media_id: Id, *, operation: str) -> MediaAssetRow:
        """Carrega a linha do ativo ou levanta `NotFoundError`."""
        row = await self._row(media_id, operation=operation)
        if row is None:
            raise NotFoundError(
                f"ativo de midia nao encontrado: {media_id}",
                details={"media_id": media_id},
            )
        return row
