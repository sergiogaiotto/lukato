# ADR-0006 — Migracoes portateis entre PostgreSQL e SQLite

**Status:** aceito · **Data:** 2026-08

## Contexto

O alvo de producao e PostgreSQL 16 + pgvector. Dev e CI rodam em SQLite (ADR-0003).
Manter duas trilhas de migracao seria uma fonte permanente de divergencia entre o
schema que se testa e o que se implanta.

O Alembic, porem, autogera contra o dialeto conectado. Gerar em SQLite produzia dois
defeitos silenciosos:

1. `VectorType` era escrito no arquivo como
   `lukato.adapters.persistence.types.VectorType(dim=1024)` **sem o import** — a
   migracao quebrava com `NameError` na primeira execucao.
2. `JSONType` (`JSON().with_variant(JSONB, "postgresql")`) era expandido como
   `postgresql.JSONB(astext_type=Text())`, tambem sem importar `Text`.

Ambos passariam despercebidos em revisao: o arquivo *parece* correto.

## Decisao

Uma unica trilha de migracao, com `render_item` em `migrations/env.py` emitindo os
tipos proprios do projeto (`VectorType`, `JSONType`) e registrando os imports
correspondentes. Os tipos sao `TypeDecorator`/variantes que resolvem por dialeto em
tempo de execucao, entao o mesmo arquivo cria `vector(1024)` no PostgreSQL e `JSON`
no SQLite.

O que e genuinamente especifico do PostgreSQL — a extensao `vector`, os indices HNSW
e os indices `gin_trgm` — fica isolado em `0002`, que verifica o dialeto e vira no-op
nos demais.

## Consequencias

**Positivas:** o schema testado em CI e o mesmo implantado em producao — verificado
por teste que compara tabela a tabela e coluna a coluna o resultado de
`alembic upgrade head` com `Base.metadata.create_all`. Uma trilha, um `head`.

**Negativas:** `env.py` carrega um `render_item` que precisa acompanhar novos tipos
customizados. Um tipo novo sem entrada la volta a gerar migracao sem import — por
isso o teste de comparacao de schema roda no CI, e nao apenas localmente.
