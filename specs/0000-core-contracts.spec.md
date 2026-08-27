# SPEC-0000 — Contratos Nucleares (fonte da verdade)

> **Status:** aceito · **Versao:** 1.0.0 · **Escopo:** normativo para todo o codigo de `lukato`.
>
> Este documento e a *especificacao mestre* do Spec-Driven Development (SDD) do projeto.
> Toda assinatura publica (modulo Python, classe, metodo, campo) descrita aqui e
> **normativa**: o codigo deve implementa-la exatamente. Divergencias sao defeito.
> As demais SPECs (`0001`..`0011`) detalham comportamento; nenhuma delas pode
> contradizer este documento.

---

## 1. Identidade do projeto

| Item | Valor |
| --- | --- |
| Nome | `lukato` |
| Versao | `1.0.0` |
| Pacote Python | `lukato` (layout `src/`) |
| Python | `>=3.11,<3.13` |
| Abordagem | Spec-Driven Development |
| Arquitetura | Hexagonal (Ports & Adapters) |
| Framework HTTP | FastAPI (OpenAPI 3.1 / Swagger UI) |
| Template engine | Jinja2 |
| Banco | PostgreSQL 16 + pgvector (fallback SQLite/aiosqlite em dev e testes) |
| Orquestracao de agentes | LangGraph + Deep-Agent Harness (`deepagents`) |
| Observabilidade | Langfuse (fallback no-op) |

`lukato.__version__ == "1.0.0"`.

---

## 2. Regra de dependencia (hexagonal)

```text
interfaces/  (driving)  ─┐
                         ├─> application/ ─> domain/  <─ adapters/ (driven)
modules/     (blocks)   ─┘
```

Regras **obrigatorias**, verificadas por teste de arquitetura (`tests/unit/test_architecture.py`):

1. `lukato.domain.*` **nao** importa `application`, `adapters`, `interfaces`, `modules`,
   nem qualquer biblioteca de I/O (`sqlalchemy`, `fastapi`, `httpx`, `openai`,
   `langgraph`, `langfuse`, `jinja2`, `psycopg`, `asyncpg`). Somente stdlib +
   `pydantic` (usado apenas para modelos de dados puros) sao permitidos.
2. `lukato.application.*` importa `domain` e stdlib/`pydantic`. Nao importa
   `adapters` nem `interfaces` (a composicao acontece no *composition root*).
3. `lukato.adapters.*` importa `domain` (para implementar portas) e bibliotecas externas.
4. `lukato.interfaces.*` importa `application` + `domain`; recebe adaptadores por injecao.
5. `lukato.composition` (composition root) e o **unico** modulo que **monta o
   `Container`**. Nenhum outro modulo pode chama-lo — e assim que a fiacao fica em
   um lugar so e a troca de adaptador vira uma linha.

   Isto **nao** proibe `interfaces/` de importar um adaptador. Interfaces sao
   infraestrutura (driving adapters), e ha tres usos legitimos e deliberados:

   | Modulo | Importa | Por que |
   | --- | --- | --- |
   | `interfaces/http/api/v1/routers/adwatch.py` | `adapters.media.importers`, `adapters.media.factory` | o parsing do JSON do WhisperX acontece na borda, e a aplicacao recebe `list[TranscriptWord]` ja tipada (SPEC-0010) |
   | `interfaces/http/api/v1/routers/health.py` | `adapters.observability.metrics` | servir `/metrics` exige o registro de metricas |
   | `interfaces/cli.py` | `adapters.guardrails.policies` | o `seed` precisa das politicas concretas |

   A regra que importa e a de dentro: **`domain/` e `application/` nunca importam
   `adapters` nem `interfaces`** (itens 1 e 2). Essa e verificada por lint e por
   `tests/unit/test_architecture.py`.

---

## 3. Layout canonico de diretorios

```text
src/lukato/
├── __init__.py                 # __version__
├── main.py                     # entrypoint ASGI: app = create_app()
├── composition.py              # composition root (monta o Container)
├── config/
│   ├── __init__.py
│   ├── settings.py             # Settings (pydantic-settings), get_settings()
│   └── logging.py              # configure_logging()
├── domain/
│   ├── __init__.py
│   ├── errors.py
│   ├── types.py                # Id, Json, utcnow(), new_id()
│   ├── models/                 # entidades e value objects (pydantic BaseModel)
│   ├── ports/                  # Protocolos (interfaces abstratas)
│   └── services/               # servicos de dominio puros
├── application/
│   ├── __init__.py
│   ├── container.py            # Container (dataclass de portas) + UseCases
│   ├── dto.py
│   └── use_cases/
├── adapters/
│   ├── llm/  embeddings/  persistence/  guardrails/
│   ├── orchestrator/  observability/  media/  security/
├── interfaces/
│   ├── http/                   # routers + schemas (API v1)
│   ├── ui/                     # Jinja2: router, templates/, static/
│   └── cli.py
└── modules/                    # BUILDING BLOCKS plugaveis
    ├── base.py  registry.py  errors.py
    └── builtin/
```

---

## 4. `lukato.domain.types`

```python
Id = str                                    # UUID4 em hex-dash (str(uuid.uuid4()))
Json = dict[str, Any]

def new_id() -> Id: ...                     # str(uuid.uuid4())
def utcnow() -> datetime: ...               # datetime.now(timezone.utc)
def slugify(value: str) -> str: ...         # a-z0-9 e '-'
```

---

## 5. `lukato.domain.errors`

Hierarquia unica. Toda excecao de dominio herda de `LukatoError`.

```python
class LukatoError(Exception):
    code: ClassVar[str] = "lukato_error"
    http_status: ClassVar[int] = 500
    def __init__(self, message: str, *, details: Json | None = None) -> None: ...
    @property
    def details(self) -> Json: ...
    def to_dict(self) -> Json: ...   # {"code","message","details"}
```

| Classe | `code` | `http_status` |
| --- | --- | --- |
| `ValidationError` | `validation_error` | 422 |
| `NotFoundError` | `not_found` | 404 |
| `ConflictError` | `conflict` | 409 |
| `UnauthorizedError` | `unauthorized` | 401 |
| `ForbiddenError` | `forbidden` | 403 |
| `GuardrailViolation` | `guardrail_violation` | 422 |
| `BudgetExceededError` | `budget_exceeded` | 402 |
| `ProviderError` | `provider_error` | 502 |
| `RateLimitedError` | `rate_limited` | 429 |
| `ModuleError` | `module_error` | 500 |
| `ModuleNotFound` (herda `NotFoundError`) | `module_not_found` | 404 |
| `ConfigurationError` | `configuration_error` | 500 |
| `UnsupportedCapability` | `unsupported_capability` | 501 |

`GuardrailViolation` possui atributos extra: `policy_id: Id | None`, `rule_id: str | None`,
`stage: str` (`"input"` ou `"output"`).

---

## 6. Modelos de dominio (`lukato.domain.models`)

Todos sao `pydantic.BaseModel` com `model_config = ConfigDict(extra="forbid", frozen=False)`.
Campos comuns de entidade persistida: `id: Id = Field(default_factory=new_id)`,
`created_at: datetime = Field(default_factory=utcnow)`, `updated_at: datetime = Field(default_factory=utcnow)`.

### 6.1 `models/module.py`
```python
class ModuleKind(StrEnum):        # tipo funcional do building block
    AGENT = "agent"; TOOL = "tool"; PIPELINE = "pipeline"
    AUTH = "auth"; FINOPS = "finops"; KNOWLEDGE = "knowledge"; CUSTOM = "custom"

class ModuleStatus(StrEnum):
    DRAFT = "draft"; ACTIVE = "active"; PAUSED = "paused"; DEPRECATED = "deprecated"

class ModuleBinding(BaseModel):
    """Trinca parametrizavel exigida para TODO modulo:
       guardrail de entrada -> system prompt -> guardrail de saida."""
    input_guardrail_id: Id | None = None
    system_prompt_id: Id | None = None
    output_guardrail_id: Id | None = None
    model: str | None = None
    temperature: float | None = None      # 0.0..2.0
    max_tokens: int | None = None         # 1..8192
    timeout_seconds: float = 60.0
    tools: list[str] = []

class ModuleDefinition(BaseModel):
    id: Id; slug: str; name: str; description: str = ""
    kind: ModuleKind = ModuleKind.AGENT
    status: ModuleStatus = ModuleStatus.DRAFT
    runtime: str = "langgraph"            # "langgraph" | "deepagent" | "direct" | "<custom>"
    binding: ModuleBinding = ModuleBinding()
    config: Json = {}
    tags: list[str] = []
    owner: str | None = None
    version: str = "1.0.0"
    created_at / updated_at
```

### 6.2 `models/prompt.py`
```python
class PromptRole(StrEnum): SYSTEM="system"; USER="user"; ASSISTANT="assistant"; DEVELOPER="developer"

class PromptTemplate(BaseModel):
    id, slug, name, description
    role: PromptRole = PromptRole.SYSTEM
    template: str                          # placeholders {{ var }} (Jinja-like simples)
    variables: list[str] = []
    version: int = 1
    is_active: bool = True
    labels: list[str] = []
    created_at / updated_at
    def render(self, variables: Json) -> str: ...    # substituicao segura, sem exec
```
`render` substitui `{{ nome }}` / `{{nome}}`; variavel ausente -> `ValidationError`
listando as faltantes em `details["missing"]`.

### 6.3 `models/guardrail.py`
```python
class GuardrailStage(StrEnum): INPUT="input"; OUTPUT="output"

class GuardrailAction(StrEnum):
    ALLOW="allow"; WARN="warn"; REDACT="redact"; TRANSFORM="transform"; BLOCK="block"

class GuardrailSeverity(StrEnum): LOW="low"; MEDIUM="medium"; HIGH="high"; CRITICAL="critical"

class GuardrailRuleKind(StrEnum):
    REGEX_BLOCK="regex_block"; REGEX_REQUIRE="regex_require"; KEYWORD_BLOCK="keyword_block"
    PII_REDACT="pii_redact"; MAX_LENGTH="max_length"; MIN_LENGTH="min_length"
    JSON_SCHEMA="json_schema"; LANGUAGE_ALLOW="language_allow"
    TOPIC_BLOCK="topic_block"; LLM_JUDGE="llm_judge"; SECRET_SCAN="secret_scan"

class GuardrailRule(BaseModel):
    id: str                                # unico dentro da policy
    kind: GuardrailRuleKind
    action: GuardrailAction = GuardrailAction.BLOCK
    severity: GuardrailSeverity = GuardrailSeverity.MEDIUM
    config: Json = {}
    message: str = ""
    enabled: bool = True
    order: int = 0

class GuardrailPolicy(BaseModel):
    id, slug, name, description
    stage: GuardrailStage
    rules: list[GuardrailRule] = []
    fail_open: bool = False                # True: erro interno da regra nao bloqueia
    is_active: bool = True
    created_at / updated_at

class GuardrailFinding(BaseModel):
    rule_id: str; kind: GuardrailRuleKind; action: GuardrailAction
    severity: GuardrailSeverity; message: str; evidence: str = ""; span: tuple[int,int] | None = None

class GuardrailVerdict(BaseModel):
    allowed: bool
    stage: GuardrailStage
    content: str                            # conteudo final (possivelmente redigido)
    original_content: str
    findings: list[GuardrailFinding] = []
    policy_id: Id | None = None
    latency_ms: float = 0.0
    @property
    def blocked(self) -> bool
    @property
    def modified(self) -> bool
```

### 6.4 `models/run.py`
```python
class RunStatus(StrEnum):
    PENDING="pending"; RUNNING="running"; SUCCEEDED="succeeded"
    FAILED="failed"; BLOCKED="blocked"; CANCELLED="cancelled"

class StepKind(StrEnum):
    GUARDRAIL_IN="guardrail_in"; PROMPT="prompt"; LLM="llm"; TOOL="tool"
    RETRIEVAL="retrieval"; PLAN="plan"; REFLECT="reflect"
    GUARDRAIL_OUT="guardrail_out"; ERROR="error"

class TokenUsage(BaseModel):
    prompt_tokens: int = 0; completion_tokens: int = 0; total_tokens: int = 0
    def __add__(self, other: "TokenUsage") -> "TokenUsage": ...

class RunStep(BaseModel):
    id, run_id: Id, index: int, kind: StepKind, name: str
    status: RunStatus = RunStatus.SUCCEEDED
    input: Json = {}; output: Json = {}
    usage: TokenUsage = TokenUsage()
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None
    started_at / finished_at: datetime | None

class AgentRun(BaseModel):
    id, module_id: Id, module_slug: str
    status: RunStatus = RunStatus.PENDING
    input: Json = {}; output: Json = {}
    steps: list[RunStep] = []
    usage: TokenUsage = TokenUsage()
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace_id: str | None = None
    error: str | None = None
    tenant_id: str = "default"
    actor: str | None = None
    created_at / updated_at; finished_at: datetime | None = None
```

### 6.5 `models/finops.py`
```python
class ModelPrice(BaseModel):
    model: str
    input_usd_per_1k: float = 0.0
    output_usd_per_1k: float = 0.0
    currency: str = "USD"

class UsageRecord(BaseModel):
    id, run_id: Id | None, module_slug: str, model: str
    usage: TokenUsage, cost_usd: float, tenant_id: str = "default"
    occurred_at: datetime

class BudgetPeriod(StrEnum): DAILY="daily"; WEEKLY="weekly"; MONTHLY="monthly"; TOTAL="total"

class Budget(BaseModel):
    id, name, scope: str           # "global" | "module:<slug>" | "tenant:<id>"
    limit_usd: float, period: BudgetPeriod = BudgetPeriod.MONTHLY
    alert_threshold: float = 0.8   # 0..1
    hard_stop: bool = False
    is_active: bool = True

class CostSummary(BaseModel):
    total_usd: float = 0.0; total_tokens: int = 0; runs: int = 0
    by_module: dict[str, float] = {}; by_model: dict[str, float] = {}
```

### 6.6 `models/knowledge.py`
```python
class Document(BaseModel):
    id, collection: str, title: str, source: str = "", content: str
    metadata: Json = {}, checksum: str = "", created_at/updated_at

class Chunk(BaseModel):
    id, document_id: Id, collection: str, index: int, content: str
    metadata: Json = {}, embedding: list[float] | None = None, token_count: int = 0

class SearchHit(BaseModel):
    chunk_id: Id; document_id: Id; collection: str; content: str
    score: float; metadata: Json = {}
```

### 6.7 `models/identity.py`
```python
class Role(StrEnum): ROOT="root"; ADMIN="admin"; OPERATOR="operator"; VIEWER="viewer"

class Permission(StrEnum):
    MODULE_READ="module:read"; MODULE_WRITE="module:write"; MODULE_INVOKE="module:invoke"
    PROMPT_READ="prompt:read"; PROMPT_WRITE="prompt:write"
    GUARDRAIL_READ="guardrail:read"; GUARDRAIL_WRITE="guardrail:write"
    KNOWLEDGE_READ="knowledge:read"; KNOWLEDGE_WRITE="knowledge:write"
    FINOPS_READ="finops:read"; FINOPS_WRITE="finops:write"
    RUN_READ="run:read"; ADMIN_ALL="admin:*"

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]]

class User(BaseModel):
    id, email: str, name: str, role: Role = Role.VIEWER
    password_hash: str = "", is_active: bool = True, tenant_id: str = "default"

class ApiKey(BaseModel):
    id, name: str, prefix: str, hashed_secret: str, role: Role = Role.OPERATOR
    tenant_id: str = "default", is_active: bool = True
    expires_at: datetime | None = None, last_used_at: datetime | None = None

class Principal(BaseModel):     # identidade resolvida da requisicao
    subject: str; role: Role; tenant_id: str = "default"
    kind: str = "user"          # "user" | "api_key" | "anonymous"
    permissions: frozenset[Permission] = frozenset()
    def can(self, permission: Permission) -> bool: ...
```

### 6.8 `models/adwatch.py`
```python
class MediaKind(StrEnum): VIDEO="video"; AUDIO="audio"

class Commercial(BaseModel):            # catalogo com CRUD (requisito explicito)
    id, commercial_id: str              # codigo de negocio, ex "COM_000234" (unico)
    campaign: str; brand: str
    text: str                           # texto conhecido do comercial
    duration_expected: float = 30.0     # segundos
    keywords: list[str] = []
    key_phrases: list[str] = []
    language: str = "pt-BR"
    is_active: bool = True
    metadata: Json = {}

class AdFingerprint(BaseModel):
    id, commercial_id: Id
    normalized_text: str; token_set: list[str]
    keywords: list[str] = []; key_phrases: list[str] = []
    embedding: list[float] | None = None
    duration: float = 30.0; expected_brand: str = ""

class MediaAsset(BaseModel):
    id, uri: str, kind: MediaKind = MediaKind.VIDEO
    duration_seconds: float = 0.0, fps: float = 0.0, title: str = ""
    status: str = "registered"          # registered|ingesting|ingested|analyzing|analyzed|failed
    metadata: Json = {}

class TranscriptWord(BaseModel):
    word: str; start: float; end: float; score: float = 1.0; speaker: str | None = None

class Transcript(BaseModel):
    id, media_id: Id, language: str = "pt", words: list[TranscriptWord] = []
    source: str = "import"              # whisperx|import
    @property
    def text(self) -> str
    def window(self, start: float, end: float) -> "Transcript"

class SceneCut(BaseModel):
    index: int; start: float; end: float; kind: str = "cut"    # cut|fade

class OcrText(BaseModel):
    text: str; start: float; end: float; confidence: float = 1.0; bbox: tuple[int,int,int,int] | None = None

class DetectionEvidence(BaseModel):
    speech_match: float = 0.0; semantic_match: float = 0.0; ocr_match: float = 0.0
    visual_match: float = 0.0; duration_match: float = 0.0
    order_ok: bool = True; brand_detected: str | None = None
    matched_text: str = ""

class DetectionStatus(StrEnum):
    ACCEPTED="accepted"; NEEDS_REVIEW="needs_review"; REJECTED="rejected"

class DetectionCandidate(BaseModel):
    commercial_id: Id; commercial_code: str; campaign: str = ""
    start: float; end: float
    score: float; evidence: DetectionEvidence = DetectionEvidence()

class Detection(BaseModel):
    id, media_id: Id, commercial_id: Id, commercial_code: str
    campaign: str = "", brand: str = ""
    start: float, end: float
    confidence: float, status: DetectionStatus
    evidence: DetectionEvidence = DetectionEvidence()
    refined_by_scene: bool = False
    verified_by_vlm: bool = False
    created_at
```

---

## 7. Portas (`lukato.domain.ports`)

Todas sao `typing.Protocol` (`@runtime_checkable` quando util). Metodos de I/O sao `async`.

### 7.1 `ports/llm.py`
```python
class ChatMessage(BaseModel): role: str; content: str
class LLMResponse(BaseModel):
    content: str; model: str; usage: TokenUsage = TokenUsage()
    finish_reason: str = "stop"; raw: Json = {}; latency_ms: float = 0.0

class LLMPort(Protocol):
    @property
    def default_model(self) -> str: ...
    async def chat(self, messages: Sequence[ChatMessage], *, model: str | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   stop: Sequence[str] | None = None,
                   response_format: Json | None = None,
                   metadata: Json | None = None) -> LLMResponse: ...
    def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]: ...
        # NAO e 'async def': um 'async def' anotado com AsyncIterator tem tipo
        # Coroutine[..., AsyncIterator[str]] e recusaria a implementacao idiomatica
        # por gerador assincrono. Adaptador: 'async def stream(...)' com yield.
        # Consumidor: 'async for chunk in llm.stream(...)'.
    async def list_models(self) -> list[str]: ...
    async def health(self) -> bool: ...
```

### 7.2 `ports/embeddings.py`
```python
class EmbeddingPort(Protocol):
    @property
    def model(self) -> str: ...
    @property
    def dimensions(self) -> int: ...
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def embed_one(self, text: str) -> list[float]: ...
    async def health(self) -> bool: ...
```

### 7.3 `ports/vector_store.py`
```python
class VectorStorePort(Protocol):
    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int: ...
    async def search(self, collection: str, vector: Sequence[float], *, limit: int = 10,
                     filters: Json | None = None) -> list[SearchHit]: ...
    async def delete(self, collection: str, *, document_id: Id | None = None) -> int: ...
    async def collections(self) -> list[str]: ...
```

### 7.4 `ports/guardrail.py`
```python
class GuardrailRuleEvaluator(Protocol):
    kind: GuardrailRuleKind
    async def evaluate(self, content: str, rule: GuardrailRule,
                       context: Json) -> GuardrailFinding | None: ...

class GuardrailPort(Protocol):
    async def apply(self, content: str, policy: GuardrailPolicy | None,
                    *, context: Json | None = None) -> GuardrailVerdict: ...
```

### 7.5 `ports/observability.py`
```python
class SpanHandle(Protocol):
    def update(self, **kwargs: Any) -> None: ...
    def end(self, **kwargs: Any) -> None: ...

class TracerPort(Protocol):
    @contextlib.asynccontextmanager
    def trace(self, name: str, *, input: Json | None = None,
              metadata: Json | None = None, user_id: str | None = None,
              session_id: str | None = None, tags: Sequence[str] | None = None
              ) -> AsyncIterator[SpanHandle]: ...
    @contextlib.asynccontextmanager
    def span(self, name: str, *, kind: str = "span", input: Json | None = None,
             metadata: Json | None = None) -> AsyncIterator[SpanHandle]: ...
    async def score(self, *, name: str, value: float, trace_id: str | None = None,
                    comment: str | None = None) -> None: ...
    async def flush(self) -> None: ...
    @property
    def enabled(self) -> bool: ...
```
> Nota de implementacao: `trace`/`span` sao *metodos que retornam async context managers*.
> Nos adaptadores use `@asynccontextmanager` sobre um metodo `async def`.

### 7.6 `ports/repositories.py`
Repositorios genericos e especificos, todos `Protocol`:
```python
class ModuleRepository(Protocol):
    async def add(self, module: ModuleDefinition) -> ModuleDefinition: ...
    async def get(self, module_id: Id) -> ModuleDefinition | None: ...
    async def get_by_slug(self, slug: str) -> ModuleDefinition | None: ...
    async def list(self, *, kind: ModuleKind | None = None, status: ModuleStatus | None = None,
                   search: str | None = None, limit: int = 50, offset: int = 0
                   ) -> list[ModuleDefinition]: ...
    async def count(self, **filters: Any) -> int: ...
    async def update(self, module: ModuleDefinition) -> ModuleDefinition: ...
    async def delete(self, module_id: Id) -> None: ...

class PromptRepository(Protocol):     # add/get/get_by_slug/list/update/delete (+ list_versions)
class GuardrailRepository(Protocol):  # add/get/get_by_slug/list(stage=)/update/delete
class RunRepository(Protocol):        # add/get/list(module_slug,status,since)/update/add_step
class UsageRepository(Protocol):      # add/list/summary(since,until,module_slug,tenant_id)->CostSummary
class BudgetRepository(Protocol):     # add/get/list/update/delete
class DocumentRepository(Protocol):   # add/get/list(collection)/delete/add_chunks/list_chunks
class UserRepository(Protocol):       # add/get/get_by_email/list/update/delete
class ApiKeyRepository(Protocol):     # add/get_by_prefix/list/update/delete
class CommercialRepository(Protocol): # add/get/get_by_code/list(search,brand,campaign)/update/delete/count
class MediaRepository(Protocol):      # add/get/list/update/delete + transcripts/scenes/ocr
class DetectionRepository(Protocol):  # add/list(media_id,commercial_id,status)/get/update/delete
```

### 7.7 `ports/unit_of_work.py`
```python
class UnitOfWork(Protocol):
    modules: ModuleRepository
    prompts: PromptRepository
    guardrails: GuardrailRepository
    runs: RunRepository
    usage: UsageRepository
    budgets: BudgetRepository
    documents: DocumentRepository
    users: UserRepository
    api_keys: ApiKeyRepository
    commercials: CommercialRepository
    media: MediaRepository
    detections: DetectionRepository
    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, *exc: Any) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...

class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
```

### 7.8 `ports/orchestrator.py`
```python
class OrchestratorRequest(BaseModel):
    module: ModuleDefinition
    input_text: str
    variables: Json = {}
    history: list[ChatMessage] = []
    tools: list[str] = []
    system_prompt: str = ""
    metadata: Json = {}

class OrchestratorResult(BaseModel):
    output_text: str
    steps: list[RunStep] = []
    usage: TokenUsage = TokenUsage()
    metadata: Json = {}

class OrchestratorPort(Protocol):
    name: str
    async def run(self, request: OrchestratorRequest) -> OrchestratorResult: ...
    def supports(self, runtime: str) -> bool: ...
```

### 7.9 `ports/media.py` (AdWatch — todos opcionais/degradaveis)
```python
class MediaProbePort(Protocol):
    async def probe(self, uri: str) -> Json: ...                       # duration, fps, codecs
    async def extract_audio(self, uri: str, out_path: str) -> str: ...
    async def cut(self, uri: str, start: float, end: float, out_path: str) -> str: ...
    @property
    def available(self) -> bool: ...

class ASRPort(Protocol):
    async def transcribe(self, audio_uri: str, *, language: str = "pt") -> list[TranscriptWord]: ...
    @property
    def available(self) -> bool: ...

class OCRPort(Protocol):
    async def extract(self, media_uri: str, *, start: float, end: float,
                      fps: float = 1.0) -> list[OcrText]: ...
    @property
    def available(self) -> bool: ...

class SceneDetectorPort(Protocol):
    async def detect(self, media_uri: str) -> list[SceneCut]: ...
    @property
    def available(self) -> bool: ...

class VisionJudgePort(Protocol):
    async def verify(self, *, media_uri: str, start: float, end: float,
                     commercial: Commercial, transcript_excerpt: str) -> Json: ...
    @property
    def available(self) -> bool: ...
```

### 7.10 `ports/misc.py`
```python
class ClockPort(Protocol):
    def now(self) -> datetime: ...
class IdGeneratorPort(Protocol):
    def new(self) -> Id: ...
class PasswordHasherPort(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password: str, hashed: str) -> bool: ...
class TokenServicePort(Protocol):
    def issue(self, principal: Principal, *, expires_in: int = 3600) -> str: ...
    def decode(self, token: str) -> Principal: ...
```

---

## 8. Servicos de dominio (`lukato.domain.services`)

| Modulo | Classe | Responsabilidade |
| --- | --- | --- |
| `guardrail_engine.py` | `GuardrailEngine` | Aplica `GuardrailPolicy` sobre texto usando `GuardrailRuleEvaluator`s registrados. Implementa `GuardrailPort`. |
| `cost_calculator.py` | `CostCalculator` | `cost(model, usage) -> float`, tabela `ModelPrice`, `summarize(records) -> CostSummary`, `check_budget(...)`. |
| `module_composer.py` | `ModuleComposer` | Resolve a trinca `input guardrail -> system prompt -> output guardrail` de um `ModuleDefinition` em um `ComposedPipeline`. |
| `text_normalizer.py` | funcoes | `normalize(text)`, `tokenize(text)`, `strip_accents(text)`, `ngrams(tokens, n)`. |
| `matching.py` | `SlidingWindowBuilder`, `LexicalMatcher`, `SemanticMatcher`, `OrderMatcher`, `ScoreFusion`, `BoundaryRefiner` | Motor de matching temporal do AdWatch (SPEC-0010). |

`ScoreFusion` usa os pesos normativos:
`S = 0.40*asr_lexical + 0.25*semantic + 0.15*ocr + 0.15*visual + 0.05*duration`.
Limiares: `>= 0.90` → `ACCEPTED`; `0.60 <= S < 0.90` → `NEEDS_REVIEW` (validacao VLM);
`< 0.60` → `REJECTED`. Todos configuraveis via `Settings`.

---

## 9. Building Blocks (`lukato.modules`)

### 9.1 `modules/base.py`
```python
class ModuleRequest(BaseModel):
    input: str = ""
    payload: Json = {}
    variables: Json = {}
    history: list[ChatMessage] = []
    stream: bool = False

class ModuleResponse(BaseModel):
    output: str = ""
    data: Json = {}
    run_id: Id | None = None
    usage: TokenUsage = TokenUsage()
    cost_usd: float = 0.0
    findings: list[GuardrailFinding] = []
    metadata: Json = {}

@dataclass(slots=True)
class ModuleContext:
    definition: ModuleDefinition
    principal: Principal
    llm: LLMPort
    embeddings: EmbeddingPort
    guardrails: GuardrailPort
    tracer: TracerPort
    uow_factory: UnitOfWorkFactory
    orchestrators: Mapping[str, OrchestratorPort]
    settings: Any
    services: Mapping[str, Any] = field(default_factory=dict)

class UINavItem(BaseModel):
    label: str; icon: str; endpoint: str; section: str = "FUNCIONALIDADE"; order: int = 100

class UIDescriptor(BaseModel):
    nav: list[UINavItem] = []
    center_template: str | None = None
    context_template: str | None = None
    accent: str = "#c8102e"

class BaseModule(ABC):
    kind: ClassVar[ModuleKind]
    slug: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[tuple[str, ...]] = ()
    config_schema: ClassVar[Json] = {}
    default_binding: ClassVar[ModuleBinding] = ModuleBinding()

    async def setup(self, ctx: ModuleContext) -> None: ...
    async def teardown(self) -> None: ...
    @abstractmethod
    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse: ...
    def ui(self) -> UIDescriptor: ...
    def health(self) -> Json: ...
```

### 9.2 `modules/registry.py`
```python
class ModuleRegistry:
    def register(self, module_cls: type[BaseModule]) -> type[BaseModule]: ...
    def unregister(self, slug: str) -> None: ...
    def get(self, slug: str) -> type[BaseModule]: ...          # ModuleNotFound
    def instantiate(self, slug: str) -> BaseModule: ...
    def all(self) -> list[type[BaseModule]]: ...
    def discover(self, entry_point_group: str = "lukato.modules") -> int: ...
    def load_builtin(self) -> int: ...

registry: ModuleRegistry            # singleton do processo
def register_module(cls): ...       # decorator -> registry.register
```

### 9.3 Modulos embutidos (`modules/builtin/`)

| slug | classe | kind | responsabilidade |
| --- | --- | --- | --- |
| `auth` | `AuthModule` | `AUTH` | login, emissao de JWT, CRUD de API keys, RBAC |
| `processing` | `ProcessingModule` | `AGENT` | agente generico guard-in → prompt → LLM → guard-out |
| `finops` | `FinOpsModule` | `FINOPS` | custos, orcamentos, projecoes |
| `knowledge` | `KnowledgeModule` | `KNOWLEDGE` | ingestao, chunking, embeddings, busca semantica |
| `adwatch` | `AdWatchModule` | `PIPELINE` | catalogo de comerciais + deteccao multimodal |

Cada modulo embutido **deve** honrar `ModuleBinding` (guardrail-in / system prompt / guardrail-out)
quando executa qualquer chamada de LLM.

---

## 10. Application (`lukato.application`)

### 10.1 `container.py`
```python
@dataclass(slots=True)
class Container:
    settings: Settings
    llm: LLMPort
    embeddings: EmbeddingPort
    vector_store: VectorStorePort
    guardrails: GuardrailPort
    tracer: TracerPort
    uow_factory: UnitOfWorkFactory
    orchestrators: dict[str, OrchestratorPort]
    registry: ModuleRegistry
    cost_calculator: CostCalculator
    hasher: PasswordHasherPort
    tokens: TokenServicePort
    media: MediaToolbox            # probe/asr/ocr/scenes/vision
    tools: ToolCatalog | None      # registro de ferramentas dos runtimes
    cache: CachePort | None        # cache do processo; alimenta o rate limit HTTP
```

> `cache` foi acrescentado depois da primeira redacao: sem ele o
> `RateLimitMiddleware` lia `container.cache` de um `dataclass(slots=True)` que
> nao tinha o campo, caia sempre numa janela local por instancia de middleware, e
> os adaptadores de cache ficavam sem uso nenhum. `None` continua legitimo — o
> middleware degrada — mas o composition root preenche.

### 10.2 Casos de uso
Cada caso de uso e uma classe com `__init__(self, container: Container)` e
`async def execute(self, ...) -> ...`. Pastas:
`use_cases/modules/`, `prompts/`, `guardrails/`, `runs/`, `knowledge/`, `finops/`,
`identity/`, `adwatch/`.

O caso de uso central e `use_cases/modules/invoke_module.py::InvokeModule`, que executa,
**nesta ordem exata**:
```text
resolve modulo -> checa permissao -> abre trace -> cria AgentRun(RUNNING)
 -> guardrail de ENTRADA        (policy = binding.input_guardrail_id)
 -> renderiza SYSTEM PROMPT     (prompt = binding.system_prompt_id)
 -> executa runtime             (orchestrator = module.runtime)
 -> guardrail de SAIDA          (policy = binding.output_guardrail_id)
 -> registra UsageRecord + custo -> checa Budget -> finaliza AgentRun -> commit
```
Bloqueio em qualquer guardrail → `AgentRun.status = BLOCKED`, resposta HTTP 422 com
`code = guardrail_violation` e a lista de `findings`.

---

## 11. Interfaces HTTP (`lukato.interfaces.http`)

Prefixo global `/api/v1`. Todos os routers em `interfaces/http/api/v1/routers/`.

| Router | Prefixo | Tag |
| --- | --- | --- |
| `health.py` | `/health`, `/healthz`, `/readyz` (fora do prefixo v1 tambem) | `sistema` |
| `modules.py` | `/api/v1/modules` | `modulos` |
| `prompts.py` | `/api/v1/prompts` | `prompts` |
| `guardrails.py` | `/api/v1/guardrails` | `guardrails` |
| `runs.py` | `/api/v1/runs` | `execucoes` |
| `knowledge.py` | `/api/v1/knowledge` | `conhecimento` |
| `finops.py` | `/api/v1/finops` | `finops` |
| `identity.py` | `/api/v1/identity` | `identidade` |
| `adwatch.py` | `/api/v1/adwatch` | `adwatch` |
| `registry.py` | `/api/v1/registry` | `registry` |

Padroes:
* Resposta de lista: `{"items": [...], "total": int, "limit": int, "offset": int}` (`Page[T]`).
* Erro: `{"error": {"code": str, "message": str, "details": {...}}}` + `X-Request-ID`.
* Autenticacao: `Authorization: Bearer <jwt>` **ou** `X-API-Key: <key>`.
  Quando `LUKATO_SECURITY__AUTH_ENABLED=false` (padrao em dev), resolve um
  `Principal` root anonimo.
* OpenAPI 3.1 em `/api/openapi.json`, Swagger UI em `/api/docs`, ReDoc em `/api/redoc`.

---

## 12. UI (`lukato.interfaces.ui`)

Jinja2 puro + CSS/JS locais (**sem CDN**). Rotas montadas na raiz (`/`).
Layout obrigatorio, espelhando o console de referencia:

```text
┌──────────────────────────────────────────────────────────────────────┐
│ TOPBAR: marca "lukato" · trilha · busca global (⌘K) · API · SAIR · user│
├───────────┬──────────────────────────────────────┬───────────────────┤
│ SIDEBAR   │  CENTRO: operacao do modulo          │ PAINEL DE CONTEXTO│
│ colapsavel│  (hero + cards + formularios)        │ detalhes do item  │
│ por secao │                                      │ selecionado       │
├───────────┴──────────────────────────────────────┴───────────────────┤
│ STATUSBAR: guardrails · langfuse · otel · v1.0.0 · custos · chamadas  │
└──────────────────────────────────────────────────────────────────────┘
```

* Sidebar: secoes `COCKPIT`, `FUNCIONALIDADE`, `CONFIGURACOES`, `MONITORAMENTO`,
  `ADMINISTRATIVO`. Botao de recolher persiste em `localStorage`
  (`lukato.sidebar.collapsed`).
* Painel direito: **sempre** presente; conteudo por contexto do item selecionado
  em cada modulo (bloco `{% block context %}`).
* Rotas: `/`, `/modules`, `/modules/{slug}`, `/prompts`, `/guardrails`, `/runs`,
  `/knowledge`, `/finops`, `/adwatch`, `/adwatch/commercials`, `/identity`, `/settings`.
* Tema claro/escuro via `data-theme` + `prefers-color-scheme`.

---

## 13. Configuracao (`lukato.config.settings`)

`pydantic-settings` v2, `env_prefix="LUKATO_"`, `env_nested_delimiter="__"`,
`.env` carregado automaticamente. Grupos:

```python
class AppSettings(BaseModel):        # LUKATO_APP__*
    name="lukato"; version="1.0.0"; env="dev"; debug=False
    host="0.0.0.0"; port=8000; root_path=""; workers=1

class DatabaseSettings(BaseModel):   # LUKATO_DB__*
    url="postgresql+asyncpg://lukato:lukato@localhost:5432/lukato"
    fallback_url="sqlite+aiosqlite:///./lukato.db"
    auto_fallback=True; echo=False; pool_size=10; max_overflow=20
    create_all=True                  # cria schema no boot quando alembic nao rodou

class LLMSettings(BaseModel):        # LUKATO_LLM__*
    provider="openai_compatible"     # openai_compatible|echo
    base_url="https://hub-gpus.usto.re/v1"
    api_key: SecretStr | None = None
    model="qwen-latest"
    fallback_model="openai/gpt-oss-20b"
    temperature=0.2; max_tokens=2048; timeout=60.0; max_retries=3

class EmbeddingSettings(BaseModel):  # LUKATO_EMBEDDING__*
    provider="qwen"                  # qwen|hashing
    base_url="https://hub-gpus.claro.com.br/embed06b/v1"
    api_key: SecretStr | None = None
    model="Qwen/Qwen3-Embedding-0.6B"
    dimensions=1024; batch_size=32; collection="agente_evidence"

class GuardrailSettings(BaseModel):  # LUKATO_GUARDRAILS__*
    enabled=True; fail_open=False; redaction_token="[REDIGIDO]"
    max_input_chars=32000; max_output_chars=32000

class ObservabilitySettings(BaseModel):  # LUKATO_OBSERVABILITY__*
    langfuse_enabled=False
    langfuse_host="https://cloud.langfuse.com"
    langfuse_public_key: SecretStr|None; langfuse_secret_key: SecretStr|None
    log_level="INFO"; log_json=False; metrics_enabled=True

class SecuritySettings(BaseModel):   # LUKATO_SECURITY__*
    auth_enabled=False
    jwt_secret: SecretStr = "change-me"; jwt_algorithm="HS256"; jwt_expires_seconds=3600
    api_key_header="X-API-Key"; cors_origins=["*"]

class FinOpsSettings(BaseModel):     # LUKATO_FINOPS__*
    enabled=True; currency="USD"
    prices: dict[str, dict[str,float]] = {...}   # model -> {input,output} USD/1k
    default_input_usd_per_1k=0.0; default_output_usd_per_1k=0.0

class AdWatchSettings(BaseModel):    # LUKATO_ADWATCH__*
    window_sizes=[15.0,30.0,60.0]; window_stride=5.0
    weight_lexical=0.40; weight_semantic=0.25; weight_ocr=0.15
    weight_visual=0.15; weight_duration=0.05
    accept_threshold=0.90; review_threshold=0.60
    top_k_retrieval=10; top_k_rerank=3
    workdir="./var/adwatch"

class Settings(BaseSettings):
    app / db / llm / embedding / guardrails / observability / security / finops / adwatch
    @property def is_production(self) -> bool
    @property def llm_configured(self) -> bool

@lru_cache
def get_settings() -> Settings: ...
```

---

## 14. Convencoes de codigo

* `from __future__ import annotations` no topo de todo modulo.
* Type hints completos; `mypy` em modo estrito para `domain/` e `application/`.
* `ruff` com `line-length = 100`.
* Docstrings em portugues, curtas, no nivel de modulo e de classe publica.
* Nada de `print()` — usar `structlog`/`logging`.
* Nenhum segredo em codigo: somente `Settings`.
* Toda funcao de I/O e `async`.
* Erros de biblioteca externa sao convertidos em `LukatoError` no adaptador.
* Fallback obrigatorio: a aplicacao **sobe e funciona sem rede** (LLM `echo`,
  embeddings `hashing`, tracer `noop`, banco SQLite).
