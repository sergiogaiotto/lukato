"""Engine, sessao e bootstrap do esquema relacional (SPEC-0011 secao 5).

O lukato roda em PostgreSQL 16 + pgvector e degrada automaticamente para
SQLite/aiosqlite. Este modulo concentra as diferencas entre os dois dialetos:

* `pool_size`/`max_overflow` so existem no PostgreSQL (o pool nulo do aiosqlite recusa);
* `PRAGMA foreign_keys=ON` e ligado em cada conexao SQLite, sem o que `ON DELETE CASCADE`
  seria silenciosamente ignorado;
* `CREATE EXTENSION vector` e tentado apenas no PostgreSQL e nunca derruba o boot.

Nenhuma funcao aqui levanta excecao por indisponibilidade de rede: `ping` devolve
`False` e `resolve_engine` cai para o fallback registrando WARNING.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lukato.adapters.persistence import orm as _orm
from lukato.adapters.persistence.base import Base
from lukato.adapters.persistence.types import POSTGRES_DIALECT
from lukato.config import Settings, get_logger
from lukato.domain.errors import ConfigurationError, ProviderError

__all__ = [
    "build_engine",
    "build_sessionmaker",
    "create_all",
    "dispose_engine",
    "ensure_pgvector",
    "is_postgres",
    "is_sqlite",
    "ping",
    "resolve_engine",
]

_logger = get_logger(__name__)

_PING_STATEMENT = text("SELECT 1")
_CREATE_VECTOR_EXTENSION = text("CREATE EXTENSION IF NOT EXISTS vector")


def _url_text(engine_or_url: AsyncEngine | Engine | URL | str) -> str:
    """Extrai a URL textual de um engine (sync ou async), de uma `URL` ou de uma string."""
    if isinstance(engine_or_url, str):
        return engine_or_url
    if isinstance(engine_or_url, URL):
        return engine_or_url.render_as_string(hide_password=True)
    return engine_or_url.url.render_as_string(hide_password=True)


DialectTarget = AsyncEngine | Engine | AsyncConnection | Connection | URL | str
"""Qualquer coisa de onde se consiga extrair o dialeto do banco."""


def _backend_name(target: DialectTarget) -> str:
    """Devolve o nome do backend (`postgresql`, `sqlite`, ...) de forma tolerante.

    `Connection` e `AsyncConnection` precisam de tratamento proprio: sem eles o
    objeto caia no ramo de string e virava o proprio `repr` ("<sqlalchemy...object
    at 0x...>"), fazendo `is_postgres` devolver False para uma conexao PostgreSQL
    legitima. O efeito seria silencioso e caro — `PgVectorStore.search` chama
    `is_postgres(session.get_bind())` e passaria a varrer a colecao com numpy em
    vez de usar o indice HNSW. Uma sessao ligada a uma `Connection` (padrao quando
    o teste roda dentro de uma transacao externa) cairia exatamente nisso.
    """
    if isinstance(target, AsyncConnection | Connection):
        return target.engine.url.get_backend_name()
    if isinstance(target, AsyncEngine | Engine):
        return target.url.get_backend_name()
    if isinstance(target, URL):
        return target.get_backend_name()
    if isinstance(target, str):
        scheme = target.split("://", 1)[0]
        return scheme.split("+", 1)[0].lower()
    # Tipo inesperado: falhar alto e melhor do que devolver um nome invalido que
    # desliga o pgvector sem ninguem perceber.
    raise ConfigurationError(
        f"Nao foi possivel determinar o dialeto do banco a partir de "
        f"{type(target).__name__}; passe um Engine, Connection, URL ou string."
    )


def is_postgres(target: DialectTarget) -> bool:
    """Indica se o alvo usa o dialeto PostgreSQL (unico com `JSONB` e pgvector)."""
    return _backend_name(target) == POSTGRES_DIALECT


def is_sqlite(target: DialectTarget) -> bool:
    """Indica se o alvo usa SQLite (modo de desenvolvimento, teste e offline)."""
    return _backend_name(target) == "sqlite"


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Liga `PRAGMA foreign_keys=ON` em cada conexao SQLite (habilita ON DELETE CASCADE)."""
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def build_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    """Cria o `AsyncEngine` para a URL informada (ou `settings.db.url`).

    `pool_size` e `max_overflow` sao aplicados somente no PostgreSQL; SQLite usa
    `NullPool` e recusaria esses argumentos.
    """
    target = (url or settings.db.url).strip()
    if not target:
        raise ConfigurationError(
            "URL de banco vazia: configure LUKATO_DB__URL ou LUKATO_DB__FALLBACK_URL"
        )

    options: dict[str, Any] = {"echo": settings.db.echo, "pool_pre_ping": True}
    if is_postgres(target):
        options["pool_size"] = settings.db.pool_size
        options["max_overflow"] = settings.db.max_overflow
    # SQLite nao aceita pool_size/max_overflow; o proprio dialeto aiosqlite escolhe
    # NullPool para arquivo e StaticPool para ':memory:' (a base some se a conexao cai).

    try:
        engine = create_async_engine(target, **options)
    except (SQLAlchemyError, ArithmeticError, TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"nao foi possivel criar o engine para a URL configurada: {exc}",
            details={"url": target.split("://", 1)[0]},
        ) from exc
    except ModuleNotFoundError as exc:  # driver assincrono ausente
        raise ConfigurationError(
            f"driver de banco ausente para a URL configurada: {exc}",
            details={"url": target.split("://", 1)[0]},
        ) from exc

    if is_sqlite(engine):
        _enable_sqlite_foreign_keys(engine)
    return engine


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Cria a fabrica de sessoes assincronas usada pelo `UnitOfWork`."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def ping(engine: AsyncEngine) -> bool:
    """Executa `SELECT 1`; devolve `False` (sem levantar) quando o banco nao responde."""
    try:
        async with engine.connect() as connection:
            await connection.execute(_PING_STATEMENT)
    except Exception as exc:  # indisponibilidade nunca pode derrubar o boot
        _logger.debug("database_ping_failed", url=_url_text(engine), error=str(exc))
        return False
    return True


async def resolve_engine(settings: Settings) -> tuple[AsyncEngine, str]:
    """Devolve `(engine, url_efetiva)` aplicando o fallback automatico do SPEC-0011.

    Tenta `settings.db.url`; se o ping falhar e `auto_fallback` estiver ligado,
    registra WARNING e devolve o engine de `settings.db.fallback_url`.
    """
    primary_url = settings.db.url.strip() or settings.db.fallback_url.strip()
    engine = build_engine(settings, url=primary_url)
    if await ping(engine):
        return engine, primary_url

    fallback_url = settings.db.fallback_url.strip()
    if not settings.db.auto_fallback or not fallback_url or fallback_url == primary_url:
        _logger.warning(
            "database_unreachable_without_fallback",
            url=_url_text(engine),
            auto_fallback=settings.db.auto_fallback,
        )
        return engine, primary_url

    _logger.warning(
        "database_fallback_activated",
        primary=_url_text(engine),
        fallback=fallback_url.split("://", 1)[0],
    )
    await dispose_engine(engine)
    return build_engine(settings, url=fallback_url), fallback_url


async def ensure_pgvector(engine: AsyncEngine) -> bool:
    """Garante a extensao `vector` no PostgreSQL; falha de permissao vira WARNING."""
    if not is_postgres(engine):
        return False
    try:
        async with engine.begin() as connection:
            await connection.execute(_CREATE_VECTOR_EXTENSION)
    except Exception as exc:  # falta de privilegio nao pode impedir o boot
        _logger.warning("pgvector_extension_unavailable", error=str(exc))
        return False
    _logger.info("pgvector_extension_ready")
    return True


async def create_all(engine: AsyncEngine, *, vector_dim: int) -> None:
    """Cria todas as tabelas do esquema; no PostgreSQL garante a extensao `vector` antes.

    `vector_dim` e conferido contra a dimensao com que as colunas foram declaradas
    (`orm.VECTOR_DIM`): divergencia gera WARNING, pois o DDL ja esta fixado no import.
    """
    if vector_dim != _orm.VECTOR_DIM:
        _logger.warning("vector_dim_mismatch", requested=vector_dim, declared=_orm.VECTOR_DIM)
    if is_postgres(engine):
        await ensure_pgvector(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    except SQLAlchemyError as exc:
        raise ProviderError(
            f"falha ao criar o esquema relacional: {exc}",
            details={"url": _url_text(engine)},
        ) from exc


async def dispose_engine(engine: AsyncEngine) -> None:
    """Fecha o pool do engine, ignorando falhas de um banco ja indisponivel."""
    try:
        await engine.dispose()
    except Exception as exc:  # descarte de pool nunca propaga erro
        _logger.debug("engine_dispose_failed", error=str(exc))
