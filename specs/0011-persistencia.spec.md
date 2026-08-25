# SPEC-0011 — Persistencia, esquema relacional e migracoes

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**

## 1. Objetivo

Adaptador de persistencia (driven) sobre **PostgreSQL 16 + pgvector**, com degradacao
automatica para **SQLite/aiosqlite** em desenvolvimento e testes. Implementa todos os
repositorios e o `UnitOfWork` definidos na secao 7 do SPEC-0000.

## 2. Arquivos

```text
src/lukato/adapters/persistence/
├── __init__.py
├── types.py                 # tipos cross-dialect (JSONType, VectorType, UUIDStr)
├── base.py                  # class Base(DeclarativeBase) + naming convention
├── orm.py                   # TODAS as tabelas (declarative)
├── session.py               # engine, sessionmaker, create_all, ping, dialect helpers
├── uow.py                   # SqlAlchemyUnitOfWork + factory
├── mappers.py               # helpers row <-> modelo de dominio
├── pgvector_store.py        # VectorStorePort (pgvector no PG, numpy no SQLite)
└── repositories/
    ├── __init__.py
    ├── modules.py  prompts.py  guardrails.py  runs.py
    ├── usage.py    budgets.py  documents.py
    ├── users.py    api_keys.py
    └── commercials.py  media.py  detections.py
```

## 3. Convencoes obrigatorias

1. `Base` usa `DeclarativeBase` (SQLAlchemy 2.0) com convencao de nomes:
   ```python
   NAMING = {
     "ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
     "ck": "ck_%(table_name)s_%(constraint_name)s",
     "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
     "pk": "pk_%(table_name)s",
   }
   ```
2. Chaves primarias: `String(36)` contendo UUID em texto (`Id` do dominio). **Nao** usar
   `UUID` nativo (quebra o SQLite).
3. Colunas JSON: tipo `JSONType = JSON().with_variant(JSONB, "postgresql")`.
4. Colunas de vetor: `VectorType(dim)` — `TypeDecorator` que usa
   `pgvector.sqlalchemy.Vector(dim)` no dialeto `postgresql` e `JSON` nos demais.
5. Datas: `DateTime(timezone=True)`, sempre UTC.
6. **Palavras reservadas / colisoes**: nunca use os nomes de atributo `metadata`,
   `index`, `start`, `end` diretamente numa classe declarativa.
   Mapeamento obrigatorio atributo → coluna:
   | atributo Python | coluna SQL |
   | --- | --- |
   | `meta` | `metadata` |
   | `position` | `position` (substitui `index` do dominio) |
   | `start_seconds` | `start_seconds` |
   | `end_seconds` | `end_seconds` |
7. Toda tabela persistida tem `created_at` e `updated_at` (`onupdate=utcnow`).
8. Repositorios **nunca** retornam objetos ORM: sempre modelos de dominio (`mappers.py`).
9. Toda excecao de driver e convertida: `IntegrityError` → `ConflictError`,
   demais `SQLAlchemyError` → `ProviderError`.
10. Nenhum `select()` sem `limit` em endpoints de listagem.

## 4. Esquema de tabelas (normativo)

| Tabela | Colunas |
| --- | --- |
| `modules` | `id` PK, `slug` UNIQUE NOT NULL, `name`, `description`, `kind`, `status`, `runtime`, `binding` JSON, `config` JSON, `tags` JSON, `owner`, `version`, `created_at`, `updated_at`. Indices: `ix_modules_kind`, `ix_modules_status`. |
| `prompts` | `id` PK, `slug` NOT NULL, `name`, `description`, `role`, `template` TEXT, `variables` JSON, `version` INT NOT NULL DEFAULT 1, `is_active` BOOL, `labels` JSON, `created_at`, `updated_at`. UNIQUE(`slug`,`version`). Indice `ix_prompts_slug`. |
| `guardrail_policies` | `id` PK, `slug` UNIQUE, `name`, `description`, `stage`, `rules` JSON, `fail_open` BOOL, `is_active` BOOL, `created_at`, `updated_at`. Indice `ix_guardrail_policies_stage`. |
| `agent_runs` | `id` PK, `module_id`, `module_slug`, `status`, `input` JSON, `output` JSON, `prompt_tokens` INT, `completion_tokens` INT, `total_tokens` INT, `cost_usd` FLOAT, `latency_ms` FLOAT, `trace_id`, `error` TEXT, `tenant_id`, `actor`, `created_at`, `updated_at`, `finished_at`. Indices: (`module_slug`,`created_at`), (`status`), (`tenant_id`,`created_at`). |
| `run_steps` | `id` PK, `run_id` FK→`agent_runs.id` ON DELETE CASCADE, `position` INT, `kind`, `name`, `status`, `input` JSON, `output` JSON, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`, `latency_ms`, `error`, `started_at`, `finished_at`. Indice (`run_id`,`position`). |
| `usage_records` | `id` PK, `run_id` NULL, `module_slug`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`, `tenant_id`, `occurred_at`. Indices: (`occurred_at`), (`module_slug`,`occurred_at`), (`model`). |
| `budgets` | `id` PK, `name`, `scope`, `limit_usd`, `period`, `alert_threshold`, `hard_stop`, `is_active`, `created_at`, `updated_at`. Indice (`scope`). |
| `documents` | `id` PK, `collection`, `title`, `source`, `content` TEXT, `metadata` JSON, `checksum`, `created_at`, `updated_at`. Indices: (`collection`), (`checksum`). |
| `chunks` | `id` PK, `document_id` FK→`documents.id` ON DELETE CASCADE, `collection`, `position` INT, `content` TEXT, `metadata` JSON, `embedding` VECTOR(dim) NULL, `token_count` INT. Indices: (`document_id`,`position`), (`collection`). |
| `users` | `id` PK, `email` UNIQUE, `name`, `role`, `password_hash`, `is_active`, `tenant_id`, `created_at`, `updated_at`. |
| `api_keys` | `id` PK, `name`, `prefix` UNIQUE, `hashed_secret`, `role`, `tenant_id`, `is_active`, `expires_at`, `last_used_at`, `created_at`, `updated_at`. |
| `commercials` | `id` PK, `commercial_id` UNIQUE (codigo de negocio), `campaign`, `brand`, `text` TEXT, `duration_expected` FLOAT, `keywords` JSON, `key_phrases` JSON, `language`, `is_active`, `metadata` JSON, `created_at`, `updated_at`. Indices: (`brand`), (`campaign`), (`is_active`). |
| `ad_fingerprints` | `id` PK, `commercial_id` FK→`commercials.id` ON DELETE CASCADE UNIQUE, `normalized_text` TEXT, `token_set` JSON, `keywords` JSON, `key_phrases` JSON, `embedding` VECTOR(dim) NULL, `duration` FLOAT, `expected_brand`, `created_at`, `updated_at`. |
| `media_assets` | `id` PK, `uri`, `kind`, `duration_seconds` FLOAT, `fps` FLOAT, `title`, `status`, `metadata` JSON, `created_at`, `updated_at`. Indice (`status`). |
| `transcripts` | `id` PK, `media_id` FK→`media_assets.id` ON DELETE CASCADE UNIQUE, `language`, `words` JSON, `source`, `created_at`, `updated_at`. |
| `scene_cuts` | `id` PK, `media_id` FK CASCADE, `position` INT, `start_seconds` FLOAT, `end_seconds` FLOAT, `kind`. Indice (`media_id`,`position`). |
| `ocr_texts` | `id` PK, `media_id` FK CASCADE, `text` TEXT, `start_seconds`, `end_seconds`, `confidence`, `bbox` JSON. Indice (`media_id`,`start_seconds`). |
| `detections` | `id` PK, `media_id` FK CASCADE, `commercial_id` FK→`commercials.id`, `commercial_code`, `campaign`, `brand`, `start_seconds`, `end_seconds`, `confidence`, `status`, `evidence` JSON, `refined_by_scene` BOOL, `verified_by_vlm` BOOL, `created_at`, `updated_at`. Indices: (`media_id`,`start_seconds`), (`commercial_id`), (`status`). |

Enums do dominio sao persistidos como `String` (o valor de `StrEnum`), nunca como
`ENUM` nativo — evita migracoes dolorosas e mantem compatibilidade com SQLite.

## 5. `session.py`

```python
def build_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine
async def ping(engine: AsyncEngine) -> bool
async def resolve_engine(settings: Settings) -> tuple[AsyncEngine, str]   # aplica auto_fallback
def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]
async def create_all(engine: AsyncEngine, *, vector_dim: int) -> None
async def ensure_pgvector(engine: AsyncEngine) -> bool   # CREATE EXTENSION IF NOT EXISTS vector
def is_postgres(engine_or_url) -> bool
```
`resolve_engine` tenta `settings.db.url`; se falhar e `auto_fallback` for `True`,
registra WARNING e devolve o engine de `fallback_url`. `pool_size`/`max_overflow`
so sao passados quando o dialeto e PostgreSQL.

## 6. `uow.py`

```python
class SqlAlchemyUnitOfWork:                 # implementa UnitOfWork
    def __init__(self, session_factory, *, vector_dim: int) -> None
    async def __aenter__(self) -> Self      # abre AsyncSession, instancia os 12 repos
    async def __aexit__(self, *exc) -> None # rollback em excecao, sempre close
    async def commit(self) -> None
    async def rollback(self) -> None

class UnitOfWorkFactoryImpl:
    def __init__(self, session_factory, *, vector_dim: int) -> None
    def __call__(self) -> SqlAlchemyUnitOfWork
```
`commit()` traduz `IntegrityError` em `ConflictError`.

## 7. `pgvector_store.py`

`PgVectorStore(VectorStorePort)`:
* `upsert(collection, chunks)` grava/atualiza `chunks.embedding`.
* `search(collection, vector, limit, filters)`:
  * PostgreSQL: `ORDER BY embedding <=> :vec` via `Chunk.embedding.cosine_distance(vec)`;
    `score = 1 - distance`.
  * SQLite: carrega os chunks da colecao (com `limit` de seguranca configuravel,
    padrao 10_000), calcula cosseno com `numpy` e ordena em memoria.
* `filters` aplica igualdade sobre chaves do JSON `metadata` (`document_id` tratado
  como coluna).
* `collections()` -> `SELECT DISTINCT collection`.

## 8. Migracoes (Alembic)

```text
alembic.ini
migrations/env.py          # async, le a URL de Settings, target_metadata = Base.metadata
migrations/script.py.mako
migrations/versions/0001_initial_schema.py
migrations/versions/0002_pgvector_indexes.py
```
* `0001` cria todas as tabelas da secao 4.
* `0002` executa, **somente no PostgreSQL**:
  `CREATE EXTENSION IF NOT EXISTS vector;` e os indices HNSW
  `CREATE INDEX ... ON chunks USING hnsw (embedding vector_cosine_ops);`
  `CREATE INDEX ... ON ad_fingerprints USING hnsw (embedding vector_cosine_ops);`
* `env.py` deve funcionar em modo online assincrono (`connectable.run_sync`).
* Em SQLite as migracoes rodam com `render_as_batch=True`.

## 9. Armadilha do SQLite: chaves estrangeiras

O SQLite **ignora `ON DELETE CASCADE` por padrao**. As cascatas so funcionam com
`PRAGMA foreign_keys=ON` ligado em **cada conexao**, o que `build_engine` faz por um
listener de `connect`.

Consequencia pratica, e obrigatoria para quem escreve teste: **sempre obtenha o engine
por `build_engine`/`resolve_engine`**, nunca por `create_async_engine` direto. Um
engine criado a mao passa por todos os testes de CRUD e falha silenciosamente nos de
cascata — apagar uma midia deixa deteccoes orfas, e o teste acusa o codigo de producao
por um defeito que esta no proprio teste. Em PostgreSQL o mesmo codigo cascateia
normalmente, entao a divergencia so aparece no ambiente local e no CI.

A fixture `engine` de `tests/conftest.py` deve usar `build_engine`.

## 10. Criterios de aceite

* `pytest tests/integration/test_persistence.py` passa em SQLite sem servico externo.
* `alembic upgrade head` funciona em PostgreSQL e em SQLite.
* Nenhum repositorio vaza objeto ORM.
* Round-trip de todos os 12 agregados: criar → ler → atualizar → listar → apagar.
