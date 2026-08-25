"""indices pgvector (HNSW) — somente PostgreSQL

Cria a extensao ``vector`` e os indices de similaridade por cosseno das duas
colunas de embedding. Em SQLite (dev e testes) a migracao e um no-op: a busca
vetorial la e feita por varredura em memoria, sem indice.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (nome do indice, tabela) — a coluna e sempre "embedding"
_HNSW_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_chunks_embedding_hnsw", "chunks"),
    ("ix_ad_fingerprints_embedding_hnsw", "ad_fingerprints"),
)


def _is_postgres() -> bool:
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # HNSW com vector_cosine_ops: e o operador usado por
    # PgVectorStore.search (Chunk.embedding.cosine_distance).
    # m e ef_construction sao os padroes recomendados pelo pgvector para
    # colecoes ate a ordem de milhoes de vetores.
    for index_name, table in _HNSW_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON {table} USING hnsw (embedding vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64)"
        )

    # Busca textual auxiliar do catalogo de comerciais e da base de conhecimento.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_commercials_text_trgm "
        "ON commercials USING gin (text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_title_trgm "
        "ON documents USING gin (title gin_trgm_ops)"
    )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("DROP INDEX IF EXISTS ix_documents_title_trgm")
    op.execute("DROP INDEX IF EXISTS ix_commercials_text_trgm")
    for index_name, _table in _HNSW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    # As extensoes NAO sao removidas: podem estar em uso por outros esquemas
    # do mesmo banco, e recria-las e barato.
