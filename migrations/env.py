"""Ambiente Alembic de lukato — assincrono, URL resolvida a partir de Settings."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import JSON, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from lukato.adapters.persistence import orm as _orm  # noqa: F401  (registra as tabelas)
from lukato.adapters.persistence.base import Base
from lukato.adapters.persistence.types import VectorType
from lukato.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """URL efetiva: -x url=... > LUKATO_DB__URL > fallback configurado."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    settings = get_settings()
    return settings.db.url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Emite o import de VectorType nas migracoes autogeradas.

    Sem isto o Alembic escreve ``lukato.adapters.persistence.types.VectorType(...)``
    no arquivo mas nao acrescenta o import correspondente, e a migracao quebra com
    NameError. VectorType e um TypeDecorator: resolve para ``Vector(dim)`` no
    PostgreSQL e para ``JSON`` nos demais dialetos em tempo de execucao, entao a
    mesma migracao serve para os dois bancos.
    """
    if type_ != "type":
        return False

    imports = getattr(autogen_context, "imports", None)

    if isinstance(obj, VectorType):
        if imports is not None:
            imports.add("from lukato.adapters.persistence.types import VectorType")
        return f"VectorType(dim={obj.dim})"

    # JSONType e `JSON().with_variant(JSONB, "postgresql")`. O render padrao do
    # Alembic expande a variante como `postgresql.JSONB(astext_type=Text())` sem
    # importar `Text`, produzindo uma migracao que quebra com NameError. Emitir o
    # proprio JSONType mantem a migracao portatil e correta nos dois dialetos.
    if isinstance(obj, JSON) and getattr(obj, "_variant_mapping", None):
        if imports is not None:
            imports.add("from lukato.adapters.persistence.types import JSONType")
        return "JSONType"

    return False


def run_migrations_offline() -> None:
    """Gera SQL sem conectar ao banco."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=_is_sqlite(url),
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    url = str(connection.engine.url)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=_is_sqlite(url),
        include_schemas=False,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Aplica as migracoes usando um engine assincrono."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
