# Notas de biblioteca — versoes verificadas (2026-08)

Versoes **efetivamente instaladas e testadas** neste projeto. As APIs abaixo foram
introspectadas no ambiente real; nao use assinaturas de versoes anteriores.

| Biblioteca | Versao | Repositorio |
| --- | --- | --- |
| FastAPI | 0.141.1 | https://github.com/fastapi/fastapi |
| Starlette | 1.6.0 | https://github.com/encode/starlette |
| Pydantic | 2.13.4 | https://github.com/pydantic/pydantic |
| pydantic-settings | 2.15.0 | https://github.com/pydantic/pydantic-settings |
| SQLAlchemy | 2.0.52 | https://github.com/sqlalchemy/sqlalchemy |
| Alembic | 1.19.1 | https://github.com/sqlalchemy/alembic |
| pgvector-python | 0.5.0 | https://github.com/pgvector/pgvector-python |
| LangGraph | 1.2.11 | https://github.com/langchain-ai/langgraph |
| langchain-core | 1.6.0 | https://github.com/langchain-ai/langchain |
| langchain-openai | 1.6.0 | https://github.com/langchain-ai/langchain |
| deepagents (Deep-Agent Harness) | 0.7.8 | https://github.com/langchain-ai/deepagents |
| Langfuse | 4.14.5 | https://github.com/langfuse/langfuse-python |
| OpenAI SDK | 3.3.1 | https://github.com/openai/openai-python |
| RapidFuzz | 3.14.5 | https://github.com/rapidfuzz/RapidFuzz |
| Jinja2 | 3.1.6 | https://github.com/pallets/jinja |
| bcrypt | 5.0.0 | https://github.com/pyca/bcrypt |
| PyJWT | 2.13.0 | https://github.com/jpadilla/pyjwt |
| structlog | 26.1.0 | https://github.com/hynek/structlog |

Opcionais do pipeline multimodal (`requirements-media.txt`):
WhisperX (https://github.com/m-bain/whisperX), PaddleOCR
(https://github.com/PaddlePaddle/PaddleOCR), PySceneDetect
(https://github.com/Breakthrough/PySceneDetect), FFmpeg, faiss-cpu.

---

## LangGraph 1.2.11

```python
from langgraph.graph import StateGraph, START, END
from typing import TypedDict

class State(TypedDict, total=False):
    ...

g = StateGraph(State)                 # StateGraph(state_schema, context_schema=None, ...)
g.add_node("guard_in", node_fn)       # node_fn pode ser `async def`
g.add_edge(START, "guard_in")
g.add_conditional_edges("guard_in", router_fn, {"ok": "plan", "blocked": END})
g.add_edge("finalize", END)
app = g.compile()                     # compile(checkpointer=None, *, cache=None, store=None, ...)
result = await app.ainvoke(initial_state)
```
Metodos disponiveis: `add_node`, `add_edge`, `add_conditional_edges`, `add_sequence`,
`set_entry_point`, `set_conditional_entry_point`, `set_finish_point`, `compile`, `validate`.
**Nao existe** `set_state`/`add_messages_node`. Reducers vem de `Annotated[list, add_messages]`.

## Deep-Agent Harness (`deepagents` 0.7.8)

```python
from deepagents import create_deep_agent, SubAgent, DeepAgentState

agent = create_deep_agent(
    model=chat_model,                 # str | langchain_core BaseChatModel | None
    tools=[...],
    system_prompt="...",
    subagents=[SubAgent(...)],        # opcional
    middleware=(),                    # FilesystemMiddleware, MemoryMiddleware, ...
    checkpointer=None,
)                                     # -> CompiledStateGraph
out = await agent.ainvoke({"messages": [{"role": "user", "content": "..."}]})
text = out["messages"][-1].content
```
Exports uteis: `create_deep_agent`, `SubAgent`, `AsyncSubAgent`, `CompiledSubAgent`,
`DeepAgentState`, `FilesystemMiddleware`, `FilesystemPermission`, `MemoryMiddleware`,
`SubAgentMiddleware`, `RubricMiddleware`, `HarnessProfile`, `register_harness_profile`.

Para apontar o harness ao hub Qwen (API compativel com OpenAI):
```python
from langchain_openai import ChatOpenAI
chat_model = ChatOpenAI(model="qwen-latest", base_url=..., api_key=..., temperature=...)
```

## Langfuse 4.14.5 (baseado em OpenTelemetry)

```python
from langfuse import Langfuse, get_client
client = Langfuse(public_key=..., secret_key=..., host=...)   # aceita host= e base_url=
client.auth_check()                                            # bool
with client.start_as_current_observation(as_type="span", name="x", input={...}) as span:
    span.update(output=..., metadata=...)
client.create_score(name="quality", value=0.9, trace_id=...)
client.get_current_trace_id()
client.flush(); client.shutdown()
```
**Nao existem mais** `client.trace(...)`, `trace.span(...)`, `trace.generation(...)` da v2.
Use `start_as_current_observation(as_type="generation"|"span"|"tool"|"retriever"|...)`
ou `start_observation(...)`. Todos os context managers sao **sincronos**; ao expor pela
porta `TracerPort` (assincrona) envolva-os com `contextlib.asynccontextmanager` e
`contextlib.ExitStack`.

## OpenAI SDK 3.3.1

```python
from openai import AsyncOpenAI
client = AsyncOpenAI(base_url="https://hub-gpus-lab.usto.re/v1", api_key=..., timeout=..., max_retries=...)
resp = await client.chat.completions.create(model="qwen-latest", messages=[...], max_tokens=..., temperature=...)
resp.choices[0].message.content
resp.usage.prompt_tokens / completion_tokens / total_tokens
models = await client.models.list()
```
Erros: `openai.APIStatusError` (`.status_code`), `openai.APIConnectionError`,
`openai.RateLimitError`, `openai.APITimeoutError`.

## SQLAlchemy 2.0.52 (async)

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase): ...
engine = create_async_engine(url, echo=False, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
```
`pool_size`/`max_overflow` **nao** sao aceitos por SQLite/aiosqlite (NullPool) — aplique
somente quando o dialeto for PostgreSQL.

## pgvector 0.5.0

```python
from pgvector.sqlalchemy import Vector
embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
# distancia cosseno:  col.cosine_distance(vec)   ->  similaridade = 1 - distancia
```
Requer `CREATE EXTENSION IF NOT EXISTS vector;`. Em SQLite a coluna deve degradar para
JSON e a busca ser feita em memoria (numpy).

## bcrypt 5.0.0 (sem passlib)

```python
import bcrypt
bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
bcrypt.checkpw(password.encode(), hashed.encode())
```
Limite de 72 bytes: faca pre-hash SHA-256 antes de `hashpw` para senhas longas.

## Ambiente deste repositorio

* Os hosts `hub-gpus-lab.usto.re` e `hub-gpus.claro.com.br` sao **internos da Claro** e
  nao respondem fora da rede corporativa. Todo adaptador de rede precisa de um
  fallback offline deterministico, e a suite de testes nao pode depender de rede.
* `pip` funciona; a venv de referencia fica em `.venv/`.

---

## Validacao em PostgreSQL 16 + pgvector 0.6.0 (executada)

O caminho PostgreSQL foi exercitado de verdade, nao so em SQLite:

```
alembic upgrade head            0001 -> 0002 aplicadas (Context impl PostgresqlImpl)
indices HNSW                    ix_chunks_embedding_hnsw
                                ix_ad_fingerprints_embedding_hnsw
tipos reais                     embedding -> vector   metadata/input/output -> jsonb
is_postgres(engine)             True
busca semantica                 0.785 (alvo) vs 0.260 / 0.016 (distratores)
prova_trinca.py                 7/7 asercoes, incluindo 0 chamadas apos bloqueio
readyz                          200 degraded (so o tracer, honestamente)
OpenAPI                         65 caminhos
```

As variantes cross-dialect de `types.py` fazem o que prometem: `JSONType` vira
`JSONB` e `VectorType` vira `Vector(1024)` no PostgreSQL, enquanto degradam para
`JSON` no SQLite.

> Nota de ambiente: o registry do Docker Hub e bloqueado pela politica de rede deste
> sandbox (403 em `production.cloudfront.docker.com`), entao `docker compose up` nao
> pode ser exercitado aqui. O PostgreSQL foi instalado via apt
> (`postgresql-16` + `postgresql-16-pgvector`) para conseguir validar o caminho real.
> O `docker-compose.yml` continua sendo o caminho recomendado fora deste sandbox.
