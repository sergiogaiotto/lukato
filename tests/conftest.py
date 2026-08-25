"""Fixtures compartilhadas da suite do lukato.

A suite inteira roda **offline** (SPEC-0000 secao 14): SQLite/aiosqlite em memoria
compartilhada, `EchoLLM`, `HashingEmbedder`, `NoopTracer`. Nenhuma fixture abre
socket, le `.env` ou depende do relogio real.

Mapa das fixtures, de baixo para cima::

    settings ─┬─ engine ── session_factory ── uow_factory ── uow
              ├─ llm / spy_llm / embedder / tracer / guardrails / cost_calculator
              ├─ registry ── builtin_registry
              └─ container ── app ── client
                                 └─ seeded (politicas + prompts + modulos)

Isolamento: cada teste recebe um banco novo (`engine` tem escopo de funcao), o
registry de building blocks e esvaziado antes e depois, o cache de instancias de
`InvokeModule` e limpo e as variaveis de ambiente `LUKATO_*` sao removidas — o
ambiente da maquina nunca muda o resultado.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lukato.adapters.embeddings.hashing import HashingEmbedder
from lukato.adapters.guardrails.composite import build_default_evaluators
from lukato.adapters.guardrails.policies import default_policies
from lukato.adapters.llm.echo import EchoLLM
from lukato.adapters.observability.noop_tracer import NoopTracer
from lukato.adapters.orchestrator.factory import build_orchestrators
from lukato.adapters.orchestrator.tools import ToolContext, ToolRegistry, build_tool_registry
from lukato.adapters.persistence.pgvector_store import PgVectorStore
from lukato.adapters.persistence.session import build_engine, build_sessionmaker, create_all
from lukato.adapters.persistence.uow import SqlAlchemyUnitOfWork, UnitOfWorkFactoryImpl
from lukato.adapters.security.cache import InMemoryCache
from lukato.adapters.security.hashing import BcryptHasher
from lukato.adapters.security.tokens import JwtTokenService
from lukato.application.container import Container
from lukato.application.use_cases.modules import InvokeModule
from lukato.config.settings import Settings, reset_settings_cache
from lukato.domain.models.finops import ModelPrice
from lukato.domain.models.identity import Principal
from lukato.domain.models.module import ModuleKind, ModuleStatus
from lukato.domain.ports.media import MediaToolbox
from lukato.domain.services.cost_calculator import CostCalculator
from lukato.domain.services.guardrail_engine import GuardrailEngine
from lukato.domain.services.module_composer import ModuleComposer
from lukato.domain.types import Id
from lukato.main import create_app
from lukato.modules.registry import ModuleRegistry
from lukato.modules.registry import registry as registry_singleton
from tests.factories import make_binding, make_module, make_prompt
from tests.fakes import CountingLLM

# --------------------------------------------------------------------------- #
# Constantes de teste
# --------------------------------------------------------------------------- #
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
"""Banco da suite: SQLite em memoria.

O dialeto aiosqlite usa `StaticPool` para `:memory:`, entao todas as sessoes de
um mesmo `engine` compartilham a mesma base — e cada teste recebe um `engine`
novo, portanto uma base vazia.
"""

TEST_MODEL = "modelo-de-teste"
"""Modelo declarado em `Settings.llm.model` e precificado no `cost_calculator`."""

TEST_PRICES: dict[str, ModelPrice] = {
    TEST_MODEL: ModelPrice(model=TEST_MODEL, input_usd_per_1k=1.0, output_usd_per_1k=2.0),
    "echo": ModelPrice(model="echo", input_usd_per_1k=0.0, output_usd_per_1k=0.0),
}
"""Precos redondos: 1.000 tokens de entrada custam US$ 1,00 e os de saida, US$ 2,00."""


# --------------------------------------------------------------------------- #
# Isolamento de processo
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _processo_isolado(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Neutraliza todo estado global entre testes (autouse, roda em todos).

    Remove as variaveis `LUKATO_*` do ambiente, limpa a memoizacao de
    `get_settings()`, esvazia o registry singleton de building blocks e descarta o
    cache de instancias de `InvokeModule`. Sem isso, um `.env` da maquina ou um
    modulo registrado por outro teste mudaria o resultado.
    """
    for nome in [chave for chave in os.environ if chave.startswith("LUKATO_")]:
        monkeypatch.delenv(nome, raising=False)
    reset_settings_cache()
    registry_singleton.clear()
    InvokeModule._initialized.clear()
    yield
    reset_settings_cache()
    registry_singleton.clear()
    InvokeModule._initialized.clear()


@pytest.fixture
def anyio_backend() -> str:
    """Backend unico da suite: `asyncio` (nada de trio)."""
    return "asyncio"


# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path) -> Settings:
    """`Settings` de teste: SQLite em memoria, tudo offline e autenticacao desligada.

    Nada e lido do ambiente nem do `.env`: todos os grupos sao passados
    explicitamente, o que torna o resultado igual em qualquer maquina.
    """
    return Settings(
        _env_file=None,
        app={"name": "lukato", "env": "test", "debug": True, "version": "1.0.0"},
        db={
            "url": TEST_DATABASE_URL,
            "fallback_url": TEST_DATABASE_URL,
            "auto_fallback": False,
            "create_all": True,
            "echo": False,
        },
        llm={"provider": "echo", "model": TEST_MODEL, "temperature": 0.0, "max_tokens": 512},
        embedding={"provider": "hashing", "dimensions": 1024, "collection": "teste_evidence"},
        guardrails={"enabled": True, "fail_open": False},
        observability={"langfuse_enabled": False, "log_level": "WARNING", "metrics_enabled": True},
        security={"auth_enabled": False, "jwt_secret": "segredo-de-teste-com-mais-de-32-chars"},
        finops={"enabled": True, "currency": "USD"},
        adwatch={"workdir": str(tmp_path / "adwatch")},
    )


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #
@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """`AsyncEngine` SQLite em memoria com o esquema ja criado; um por teste."""
    motor = build_engine(settings)
    await create_all(motor, vector_dim=settings.embedding.dimensions)
    try:
        yield motor
    finally:
        await motor.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Fabrica de `AsyncSession` ligada ao engine do teste."""
    return build_sessionmaker(engine)


@pytest.fixture
def uow_factory(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> UnitOfWorkFactoryImpl:
    """`UnitOfWorkFactoryImpl` real, sobre o banco em memoria do teste."""
    return UnitOfWorkFactoryImpl(session_factory, vector_dim=settings.embedding.dimensions)


@pytest.fixture
async def uow(uow_factory: UnitOfWorkFactoryImpl) -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """Uma unidade de trabalho **ja aberta**, com os doze repositorios prontos."""
    async with uow_factory() as unidade:
        yield unidade


@pytest.fixture
def vector_store(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> PgVectorStore:
    """Indice vetorial real; em SQLite a similaridade e varredura em memoria."""
    return PgVectorStore(session_factory, dimensions=settings.embedding.dimensions)


# --------------------------------------------------------------------------- #
# Portas de IA e observabilidade
# --------------------------------------------------------------------------- #
@pytest.fixture
def llm(settings: Settings) -> EchoLLM:
    """`EchoLLM`: provedor deterministico offline (prefixo `[echo] `)."""
    return EchoLLM(settings)


@pytest.fixture
def spy_llm() -> CountingLLM:
    """LLM espiao: conta chamadas e guarda as mensagens recebidas.

    E a fixture que prova a **ordem** da trinca: com o guardrail de entrada
    bloqueando, `spy_llm.calls` tem de continuar em `0` — o provedor nao pode ter
    sido chamado. Tambem expoe `last_system_prompt` e `last_user_text` para
    conferir o que o modulo realmente montou.
    """
    return CountingLLM()


@pytest.fixture
def embedder(settings: Settings) -> HashingEmbedder:
    """`HashingEmbedder`: vetores deterministicos, sem rede."""
    return HashingEmbedder(settings)


@pytest.fixture
def tracer() -> NoopTracer:
    """`NoopTracer`: observabilidade inerte, nenhum caminho levanta."""
    return NoopTracer()


@pytest.fixture
def guardrails(llm: EchoLLM, settings: Settings) -> GuardrailEngine:
    """`GuardrailEngine` com o catalogo completo dos onze avaliadores."""
    return GuardrailEngine(
        build_default_evaluators(llm=llm, settings=settings),
        redaction_token=settings.guardrails.redaction_token,
        fail_open=settings.guardrails.fail_open,
        enabled=settings.guardrails.enabled,
    )


@pytest.fixture
def cost_calculator() -> CostCalculator:
    """`CostCalculator` com precos conhecidos: 1k entrada = US$ 1, 1k saida = US$ 2."""
    return CostCalculator(TEST_PRICES, default_input=0.0, default_output=0.0)


# --------------------------------------------------------------------------- #
# Registry de building blocks
# --------------------------------------------------------------------------- #
@pytest.fixture
def registry() -> Iterator[ModuleRegistry]:
    """Registry singleton **vazio**, esvaziado antes e depois do teste.

    Quem precisa dos cinco embutidos pede `builtin_registry` (ou chama
    `registry.load_builtin()` explicitamente).
    """
    registry_singleton.clear()
    yield registry_singleton
    registry_singleton.clear()


@pytest.fixture
def builtin_registry(registry: ModuleRegistry) -> ModuleRegistry:
    """Registry com os cinco building blocks embutidos carregados."""
    registry.load_builtin()
    return registry


# --------------------------------------------------------------------------- #
# Identidade
# --------------------------------------------------------------------------- #
@pytest.fixture
def principal() -> Principal:
    """Principal root anonimo — o mesmo que a borda HTTP resolve com auth desligada."""
    return Principal.anonymous_root()


# --------------------------------------------------------------------------- #
# Container e aplicacao
# --------------------------------------------------------------------------- #
@pytest.fixture
def tools(
    settings: Settings,
    embedder: HashingEmbedder,
    vector_store: PgVectorStore,
    uow_factory: UnitOfWorkFactoryImpl,
) -> tuple[ToolRegistry, ToolContext]:
    """Catalogo normativo de ferramentas do runtime, ja ligado as portas do teste."""
    contexto = ToolContext(
        embeddings=embedder,
        vector_store=vector_store,
        uow_factory=uow_factory,
        settings=settings,
        collection=settings.embedding.collection,
    )
    return build_tool_registry(), contexto


@pytest.fixture
def container(
    settings: Settings,
    llm: EchoLLM,
    embedder: HashingEmbedder,
    vector_store: PgVectorStore,
    guardrails: GuardrailEngine,
    tracer: NoopTracer,
    uow_factory: UnitOfWorkFactoryImpl,
    cost_calculator: CostCalculator,
    builtin_registry: ModuleRegistry,
    tools: tuple[ToolRegistry, ToolContext],
) -> Container:
    """`Container` completo, montado com as fixtures acima (composicao de teste).

    E o mesmo feixe que `lukato.composition.build_container` monta em producao,
    so que sem sondas de rede: banco em memoria, LLM de eco, embeddings de
    hashing e tracer inerte. `media` fica com a `MediaToolbox` vazia — o AdWatch
    segue pelo caminho de importacao JSON, que e o suportado offline.
    """
    tool_registry, tool_context = tools
    orchestrators = build_orchestrators(
        llm, settings=settings, tools=tool_registry, tool_context=tool_context
    )
    return Container(
        settings=settings,
        llm=llm,
        embeddings=embedder,
        vector_store=vector_store,
        guardrails=guardrails,
        tracer=tracer,
        uow_factory=uow_factory,
        orchestrators=orchestrators,
        registry=builtin_registry,
        cost_calculator=cost_calculator,
        composer=ModuleComposer(
            default_model=settings.llm.model,
            default_temperature=settings.llm.temperature,
            default_max_tokens=settings.llm.max_tokens,
        ),
        hasher=BcryptHasher(rounds=4),
        tokens=JwtTokenService(settings),
        media=MediaToolbox(),
        tools=tool_registry,
        cache=InMemoryCache(),
    )


@pytest.fixture
def app(settings: Settings, container: Container) -> FastAPI:
    """Aplicacao FastAPI de `create_app` com o container injetado em `app.state`.

    O `lifespan` **nao** roda: ele montaria um segundo container pelo composition
    root, com outro banco. A injecao direta e o que mantem o teste falando com o
    mesmo `uow_factory` das outras fixtures.
    """
    aplicacao = create_app(settings)
    aplicacao.state.container = container
    aplicacao.state.settings = settings
    return aplicacao


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Cliente HTTP assincrono falando com a aplicacao por ASGI (sem socket)."""
    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://testserver") as http:
        yield http


# --------------------------------------------------------------------------- #
# Seed minimo
# --------------------------------------------------------------------------- #
SEED_MODULE_SLUG = "assistente"
"""Definicao de modulo criada pelo seed sobre a classe embutida `processing`."""

SEED_PIPELINE_SLUG = "adwatch"
"""Definicao de pipeline criada pelo seed sobre a classe embutida `adwatch`."""


@dataclass(slots=True)
class SeedIds:
    """Identificadores criados pelo seed minimo, prontos para uso nos testes."""

    policies: dict[str, Id] = field(default_factory=dict)
    prompts: dict[str, Id] = field(default_factory=dict)
    modules: dict[str, Id] = field(default_factory=dict)

    @property
    def input_policy_id(self) -> Id:
        """Id da politica de entrada padrao."""
        return self.policies["entrada-padrao"]

    @property
    def output_policy_id(self) -> Id:
        """Id da politica de saida padrao."""
        return self.policies["saida-padrao"]

    @property
    def prompt_id(self) -> Id:
        """Id do system prompt do assistente."""
        return self.prompts["assistente-geral"]

    @property
    def module_id(self) -> Id:
        """Id da definicao de modulo `assistente`."""
        return self.modules[SEED_MODULE_SLUG]


@pytest.fixture
async def seeded(uow_factory: UnitOfWorkFactoryImpl) -> SeedIds:
    """Aplica o seed minimo (politicas, prompts e modulos) e devolve os ids.

    Conteudo: as cinco politicas de `default_policies()` (SPEC-0003 secao 4), dois
    system prompts e duas definicoes de modulo com a **trinca completa** ligada —
    `assistente` sobre a classe `processing` e `adwatch` sobre a classe `adwatch`.
    E o minimo para exercitar guardrail de entrada -> prompt -> guardrail de saida
    ponta a ponta.
    """
    ids = SeedIds()
    async with uow_factory() as unidade:
        for politica in default_policies():
            gravada = await unidade.guardrails.add(politica)
            ids.policies[gravada.slug] = gravada.id

        for slug, template in (
            (
                "assistente-geral",
                "Voce e o assistente geral do lukato. Responda em portugues do Brasil.",
            ),
            (
                "triagem-atendimento",
                "Classifique o pedido do cliente e indique a proxima acao.",
            ),
        ):
            gravado = await unidade.prompts.add(make_prompt(slug=slug, template=template))
            ids.prompts[gravado.slug] = gravado.id

        assistente = await unidade.modules.add(
            make_module(
                slug=SEED_MODULE_SLUG,
                name="Assistente geral",
                kind=ModuleKind.AGENT,
                status=ModuleStatus.ACTIVE,
                runtime="direct",
                binding=make_binding(
                    input_guardrail_id=ids.policies["entrada-padrao"],
                    system_prompt_id=ids.prompts["assistente-geral"],
                    output_guardrail_id=ids.policies["saida-padrao"],
                    model=TEST_MODEL,
                    temperature=0.0,
                    max_tokens=512,
                ),
                config={"module": "processing"},
                tags=("seed",),
            )
        )
        ids.modules[assistente.slug] = assistente.id

        adwatch = await unidade.modules.add(
            make_module(
                slug=SEED_PIPELINE_SLUG,
                name="AdWatch",
                kind=ModuleKind.PIPELINE,
                status=ModuleStatus.ACTIVE,
                runtime="direct",
                binding=make_binding(
                    input_guardrail_id=ids.policies["entrada-padrao"],
                    system_prompt_id=ids.prompts["assistente-geral"],
                    output_guardrail_id=ids.policies["saida-padrao"],
                    model=TEST_MODEL,
                ),
                config={"module": "adwatch"},
                tags=("seed",),
            )
        )
        ids.modules[adwatch.slug] = adwatch.id
        await unidade.commit()
    return ids
