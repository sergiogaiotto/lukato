"""Composition root do lukato: onde os adaptadores concretos viram um `Container`.

Este e o **unico** modulo autorizado a importar `adapters`, `application` e
`interfaces` ao mesmo tempo (SPEC-0000 secao 2, regra 5). Todo o resto do sistema
so conhece portas; quem decide qual implementacao ocupa cada porta e esta funcao,
uma unica vez por processo.

Duas responsabilidades, e nada alem delas:

* :func:`build_container` — resolve banco, LLM, embeddings, guardrails, tracer,
  orquestradores, registry, precos, seguranca e capacidades multimodais;
* :func:`dispose_container` — desfaz tudo na ordem inversa, sem deixar telemetria
  ou pool de conexao pendurados no encerramento.

**O log de selecao e parte do contrato.** Cada porta emite uma linha INFO
`port_adapter_selected` com o adaptador escolhido, se ele esta degradado e o
*motivo* da escolha (chave ausente, biblioteca ausente, ping que falhou). Sem
esse log, uma instalacao rodando com `EchoLLM` e `HashingEmbedder` responderia
`200` em tudo e pareceria saudavel — o modo degradado tem de ser legivel por quem
opera, nao apenas por quem le o codigo.

A montagem **nunca** falha por indisponibilidade de rede (SPEC-0000 secao 14). O que
derruba o boot e defeito de configuracao ou de esquema — coisas que nao se resolvem
sozinhas em producao.

Duas coisas diferentes levam ao modo degradado, e vale distinguir:

* **configuracao** escolhe o adaptador — sem `LUKATO_LLM__API_KEY` entra o `EchoLLM`,
  sem endpoint de embeddings entra o `HashingEmbedder`, sem chaves do Langfuse entra
  o `NoopTracer`. A decisao e estavel e previsivel;
* **sonda** so classifica — um hub que nao responde marca a porta como `degraded` no
  log e em `/readyz`, mas nao troca o adaptador. As duas excecoes sao deliberadas: o
  banco troca por SQLite quando o `ping` falha (e o que `LUKATO_DB__AUTO_FALLBACK`
  autoriza explicitamente) e o Langfuse vira `NoopTracer` quando o `auth_check()`
  falha (exigencia da SPEC-0008 secao 3).
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from lukato.adapters.embeddings.factory import build_embedder
from lukato.adapters.guardrails.composite import build_default_evaluators
from lukato.adapters.llm.factory import build_llm
from lukato.adapters.media.factory import build_media_toolbox
from lukato.adapters.observability.factory import build_tracer_with_health
from lukato.adapters.observability.noop_tracer import NoopTracer
from lukato.adapters.orchestrator.factory import build_orchestrators
from lukato.adapters.orchestrator.tools import ToolContext, ToolRegistry, build_tool_registry
from lukato.adapters.persistence.pgvector_store import PgVectorStore
from lukato.adapters.persistence.session import (
    build_sessionmaker,
    create_all,
    dispose_engine,
    is_postgres,
    resolve_engine,
)
from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.adapters.security.cache import InMemoryCache
from lukato.adapters.security.hashing import BcryptHasher
from lukato.adapters.security.tokens import JwtTokenService
from lukato.application.container import Container
from lukato.application.use_cases.modules import InvokeModule
from lukato.config import Settings, get_logger
from lukato.domain.models.finops import ModelPrice
from lukato.domain.ports.embeddings import EmbeddingPort
from lukato.domain.ports.llm import LLMPort
from lukato.domain.ports.observability import TracerPort
from lukato.domain.services.cost_calculator import CostCalculator
from lukato.domain.services.guardrail_engine import GuardrailEngine
from lukato.domain.services.module_composer import ModuleComposer
from lukato.modules.registry import registry as module_registry

__all__ = [
    "BOOT_PROBE_TIMEOUT_SECONDS",
    "build_container",
    "build_cost_calculator",
    "dispose_container",
    "safe_url",
]

_logger = get_logger(__name__)

BOOT_PROBE_TIMEOUT_SECONDS: Final[float] = 8.0
"""Teto de espera por sonda de provedor durante o boot.

Uma instalacao sem saida para a internet nao pode ficar presa no `connect()` de um
hub inalcancavel: o `startupProbe` do Kubernetes desistiria antes do processo
terminar de subir. Estourado o teto, a porta entra degradada e o log diz por que.
"""

_ENTRY_POINT_GROUP: Final[str] = "lukato.modules"
"""Grupo de entry points varrido para descobrir modulos de terceiros."""


# ---------------------------------------------------------------------------
# Log de selecao
# ---------------------------------------------------------------------------
def _selected(
    port: str, adapter: str, *, reason: str, degraded: bool = False, **fields: Any
) -> None:
    """Registra em INFO qual adaptador ocupou uma porta e por que (SPEC-0008 secao 3).

    Esta linha e o que explica o modo degradado para quem opera: `port`,
    `adapter`, `degraded` e `reason` sao estaveis e podem ser consultados no
    agregador de logs sem depender do texto da mensagem.
    """
    _logger.info(
        "port_adapter_selected",
        port=port,
        adapter=adapter,
        degraded=degraded,
        reason=reason,
        **fields,
    )


def safe_url(url: str) -> str:
    """Devolve a URL do banco sem a senha, segura para log e para `/readyz`."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # URL malformada nao pode impedir o log de sair
        return url.split("://", 1)[0] + "://<ilegivel>"


async def _healthy(adapter: Any, *, port: str) -> bool:
    """Sonda `adapter.health()` com teto de tempo; qualquer desfecho ruim vira `False`.

    A sonda e informativa: ela decide o texto do log e o `degraded` da porta, nunca
    se a aplicacao sobe. Por isso engole tudo — inclusive o estouro do teto, que em
    uma rede sem saida seria a espera mais longa do boot inteiro.
    """
    try:
        async with asyncio.timeout(BOOT_PROBE_TIMEOUT_SECONDS):
            return bool(await adapter.health())
    except TimeoutError:
        _logger.warning("boot_probe_timeout", port=port, timeout_seconds=BOOT_PROBE_TIMEOUT_SECONDS)
        return False
    except Exception as exc:
        _logger.warning("boot_probe_failed", port=port, error=f"{type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Precos
# ---------------------------------------------------------------------------
def build_cost_calculator(settings: Settings) -> CostCalculator:
    """Monta o `CostCalculator` com a tabela de precos de `Settings` (SPEC-0005)."""
    finops = settings.finops
    prices: dict[str, ModelPrice] = {}
    for model in finops.prices:
        input_price, output_price = finops.price_for(model)
        prices[model] = ModelPrice(
            model=model,
            input_usd_per_1k=input_price,
            output_usd_per_1k=output_price,
            currency=finops.currency,
        )
    return CostCalculator(
        prices,
        default_input=finops.default_input_usd_per_1k,
        default_output=finops.default_output_usd_per_1k,
    )


# ---------------------------------------------------------------------------
# Montagem
# ---------------------------------------------------------------------------
async def build_container(settings: Settings) -> tuple[Container, AsyncEngine]:
    """Monta o `Container` completo e devolve `(container, engine)`.

    O engine sai junto porque ele nao pertence ao container (nenhuma porta o
    expoe) e ainda assim precisa ser descartado no encerramento: quem monta e
    quem desmonta trocam esse par explicitamente, em vez de o engine virar um
    global escondido.
    """
    # -- banco -------------------------------------------------------------
    engine, effective_url = await resolve_engine(settings)
    configured_url = settings.db.url.strip() or settings.db.fallback_url.strip()
    fell_back = effective_url != configured_url
    _selected(
        "database",
        "postgresql+asyncpg" if is_postgres(engine) else make_url(effective_url).get_backend_name(),
        reason=(
            f"ping em '{safe_url(configured_url)}' falhou e o fallback automatico assumiu"
            if fell_back
            else "ping respondeu na URL configurada"
        ),
        degraded=fell_back,
        url=safe_url(effective_url),
    )

    if settings.db.create_all:
        await create_all(engine, vector_dim=settings.embedding.dimensions)
        _logger.info(
            "database_schema_ready",
            url=safe_url(effective_url),
            reason="LUKATO_DB__CREATE_ALL=true: esquema garantido no boot",
        )
    else:
        _logger.info(
            "database_schema_skipped",
            reason="LUKATO_DB__CREATE_ALL=false: o esquema vem das migracoes Alembic",
        )

    session_factory = build_sessionmaker(engine)
    dimensions = settings.embedding.dimensions
    uow_factory = UnitOfWorkFactoryImpl(session_factory, vector_dim=dimensions)
    vector_store = PgVectorStore(session_factory, dimensions=dimensions)
    _selected(
        "vector_store",
        "pgvector" if is_postgres(engine) else "sqlite_scan",
        reason=(
            "PostgreSQL com pgvector: a busca por similaridade roda no indice do banco"
            if is_postgres(engine)
            else "dialeto sem pgvector: a similaridade e calculada em memoria, sobre uma "
            "varredura limitada da colecao"
        ),
        degraded=not is_postgres(engine),
        dimensions=dimensions,
        collection=settings.embedding.collection,
    )

    # -- provedores de IA --------------------------------------------------
    llm, llm_healthy = await _llm(settings)
    embeddings, embeddings_healthy = await _embeddings(settings)
    tracer, tracer_healthy = await _tracer(settings)

    # -- guardrails --------------------------------------------------------
    evaluators = build_default_evaluators(llm=llm, settings=settings)
    guardrails = GuardrailEngine(
        evaluators,
        redaction_token=settings.guardrails.redaction_token,
        fail_open=settings.guardrails.fail_open,
        enabled=settings.guardrails.enabled,
    )
    _selected(
        "guardrails",
        "GuardrailEngine",
        reason=(
            "catalogo completo de avaliadores deterministicos; o juiz por LLM usa o "
            f"mesmo provedor da porta 'llm' ({'ativo' if llm_healthy else 'degradado'})"
        ),
        evaluators=len(evaluators),
        enabled=settings.guardrails.enabled,
        fail_open=settings.guardrails.fail_open,
    )

    # -- ferramentas e orquestradores --------------------------------------
    tools: ToolRegistry = build_tool_registry()
    tool_context = ToolContext(
        embeddings=embeddings,
        vector_store=vector_store,
        uow_factory=uow_factory,
        settings=settings,
        collection=settings.embedding.collection,
    )
    _selected(
        "tools",
        "ToolRegistry",
        reason="catalogo normativo de ferramentas do runtime, ligado as portas ja resolvidas",
        names=tools.names(),
    )

    orchestrators = build_orchestrators(
        llm, settings=settings, tools=tools, tool_context=tool_context
    )
    _selected(
        "orchestrators",
        ",".join(sorted(orchestrators)),
        reason=(
            "todos os runtimes normativos disponiveis"
            if "deepagent" in orchestrators
            else "o Deep-Agent Harness nao esta disponivel nesta instalacao (biblioteca "
            "'deepagents' ausente ou provedor sem credencial); 'langgraph' assume no lugar"
        ),
        degraded="deepagent" not in orchestrators,
        runtimes=sorted(orchestrators),
    )

    # -- registry de building blocks ---------------------------------------
    discovered, builtin = _load_registry()

    # -- servicos de dominio e seguranca -----------------------------------
    cost_calculator = build_cost_calculator(settings)
    _selected(
        "cost_calculator",
        "CostCalculator",
        reason="tabela de precos de LUKATO_FINOPS__PRICES aplicada sobre os modelos conhecidos",
        models=sorted(cost_calculator.prices),
        currency=settings.finops.currency,
        enabled=settings.finops.enabled,
    )

    composer = ModuleComposer(
        default_model=settings.llm.model,
        default_temperature=settings.llm.temperature,
        default_max_tokens=settings.llm.max_tokens,
    )
    _selected(
        "composer",
        "ModuleComposer",
        reason="defaults da trinca herdados de LUKATO_LLM__* quando o binding nao os define",
        default_model=settings.llm.model,
    )

    hasher = BcryptHasher()
    tokens = JwtTokenService(settings)
    _selected(
        "security",
        "BcryptHasher+JwtTokenService",
        reason=(
            f"autenticacao {'ligada' if settings.security.auth_enabled else 'desligada'} "
            f"(LUKATO_SECURITY__AUTH_ENABLED); algoritmo {settings.security.jwt_algorithm}"
        ),
        auth_enabled=settings.security.auth_enabled,
    )

    media = build_media_toolbox(settings, llm=llm)
    capabilities = media.capabilities()
    missing = sorted(name for name, available in capabilities.items() if not available)
    _selected(
        "media",
        "MediaToolbox",
        reason=(
            "todas as capacidades multimodais presentes"
            if not missing
            else f"capacidades ausentes ({', '.join(missing)}); o AdWatch segue pelo caminho "
            "de importacao JSON de transcricao, cenas e OCR"
        ),
        degraded=bool(missing),
        **capabilities,
    )

    container = Container(
        settings=settings,
        llm=llm,
        embeddings=embeddings,
        vector_store=vector_store,
        guardrails=guardrails,
        tracer=tracer,
        uow_factory=uow_factory,
        orchestrators=orchestrators,
        registry=module_registry,
        cost_calculator=cost_calculator,
        composer=composer,
        hasher=hasher,
        tokens=tokens,
        media=media,
        tools=tools,
        cache=InMemoryCache(),
    )
    _logger.info(
        "container_ready",
        environment=settings.app.env,
        version=settings.app.version,
        database=safe_url(effective_url),
        modules=len(module_registry),
        builtin=builtin,
        discovered=discovered,
        runtimes=sorted(orchestrators),
        offline=not (llm_healthy and embeddings_healthy),
        tracing=tracer_healthy,
    )
    return container, engine


async def _llm(settings: Settings) -> tuple[LLMPort, bool]:
    """Resolve a porta de LLM e registra a escolha com o motivo.

    A escolha do adaptador e de **configuracao**, nao da sonda: sem credencial
    entra o eco offline, com credencial entra o hub. Uma sonda que falha marca a
    porta como degradada e nada mais — trocar um provedor real pelo eco por causa
    de uma indisponibilidade momentanea faria a plataforma responder texto
    fabricado no lugar do modelo, que e pior do que responder erro.
    """
    adapter = build_llm(settings)
    healthy = await _healthy(adapter, port="llm")
    offline = settings.llm.effective_provider == "echo"
    if offline:
        detail = "respostas locais e deterministicas, sem rede"
    elif healthy:
        detail = "o hub respondeu a sonda de saude do boot"
    else:
        detail = "o hub nao respondeu a sonda; as chamadas falharao com provider_error ate voltar"
    _selected(
        "llm",
        type(adapter).__name__,
        reason=(
            f"provedor configurado '{settings.llm.provider}', provedor efetivo "
            f"'{settings.llm.effective_provider}' — {detail}"
        ),
        degraded=not healthy,
        model=settings.llm.model,
        configured=settings.llm_configured,
    )
    return adapter, healthy


async def _embeddings(settings: Settings) -> tuple[EmbeddingPort, bool]:
    """Resolve a porta de embeddings e registra a escolha com o motivo.

    Vale a mesma regra do LLM: a configuracao escolhe o adaptador, a sonda apenas
    classifica. Trocar o hub por vetores de hashing depois de um boot infeliz
    ficaria colado no processo e passaria a **recusar** ingestao em qualquer
    colecao ja produzida pelo hub (`ensure_compatible`, SPEC-0007) — um estrago
    maior do que a indisponibilidade que se pretendia contornar.
    """
    adapter = build_embedder(settings)
    healthy = await _healthy(adapter, port="embeddings")
    offline = settings.embedding.effective_provider == "hashing"
    if offline:
        detail = "vetores locais e deterministicos, sem qualidade semantica real"
    elif healthy:
        detail = "o hub respondeu a sonda de saude do boot"
    else:
        detail = (
            "o hub nao respondeu a sonda; busca semantica e assinaturas do AdWatch ficam "
            "sem vetor ate ele voltar (LUKATO_EMBEDDING__PROVIDER=hashing forca o modo offline)"
        )
    _selected(
        "embeddings",
        type(adapter).__name__,
        reason=(
            f"provedor configurado '{settings.embedding.provider}', provedor efetivo "
            f"'{settings.embedding.effective_provider}' — {detail}"
        ),
        degraded=not healthy,
        model=settings.embedding.model,
        dimensions=settings.embedding.dimensions,
    )
    return adapter, healthy


async def _tracer(settings: Settings) -> tuple[TracerPort, bool]:
    """Resolve a porta de tracing e registra a escolha com o motivo.

    Aqui a fabrica assincrona e usada inteira, e nao apenas a sonda: a SPEC-0008
    secao 3 exige que um `auth_check()` falho **troque** o adaptador por
    `NoopTracer`, e essa troca (com o fechamento do cliente Langfuse) e
    responsabilidade de `build_tracer_with_health`, nao deste modulo.
    """
    adapter: TracerPort
    try:
        async with asyncio.timeout(BOOT_PROBE_TIMEOUT_SECONDS):
            adapter, healthy = await build_tracer_with_health(settings)
    except TimeoutError:
        _logger.warning(
            "boot_probe_timeout", port="tracer", timeout_seconds=BOOT_PROBE_TIMEOUT_SECONDS
        )
        adapter, healthy = NoopTracer(), False
    observability = settings.observability
    if healthy:
        reason = "Langfuse habilitado e autenticado no boot"
    elif not observability.langfuse_enabled:
        reason = "LUKATO_OBSERVABILITY__LANGFUSE_ENABLED=false: tracing desligado na configuracao"
    elif not observability.langfuse_configured:
        reason = "credenciais do Langfuse ausentes: sem as duas chaves nao ha como autenticar"
    else:
        reason = "auth_check() do Langfuse falhou no boot: backend inalcancavel ou chaves invalidas"
    _selected(
        "tracer",
        type(adapter).__name__,
        reason=reason,
        degraded=not healthy,
        host=observability.langfuse_host if observability.langfuse_enabled else "",
    )
    return adapter, healthy


def _load_registry() -> tuple[int, int]:
    """Popula o registry de building blocks e devolve `(descobertos, embutidos)`.

    A descoberta por entry point roda **antes** dos embutidos de proposito. Os
    cinco modulos embutidos tambem estao publicados como entry points em
    `pyproject.toml`: com a ordem inversa, cada um deles seria registrado duas
    vezes e `discover` acumularia cinco `ConflictError` em `discover_errors`,
    fazendo `/readyz` reportar o registry como degradado em toda instalacao
    empacotada. Nesta ordem, `load_builtin` reconhece a classe ja registrada
    (identidade, nao apenas slug) e a ignora sem ruido.
    """
    discovered = module_registry.discover(_ENTRY_POINT_GROUP)
    builtin = module_registry.load_builtin()
    failures = list(module_registry.discover_errors)
    _selected(
        "registry",
        "ModuleRegistry",
        reason=(
            f"{builtin} building block(s) embutido(s) e {discovered} por entry point"
            if not failures
            else f"{len(failures)} origem(ns) falharam ao carregar: "
            + "; ".join(f"{origin} ({reason})" for origin, reason in failures)
        ),
        degraded=bool(failures),
        slugs=module_registry.slugs(),
        builtin=builtin,
        discovered=discovered,
    )
    return discovered, builtin


# ---------------------------------------------------------------------------
# Desmontagem
# ---------------------------------------------------------------------------
async def dispose_container(container: Container, engine: AsyncEngine) -> None:
    """Encerra o container na ordem inversa da montagem, sem nunca levantar.

    A ordem importa: os modulos ainda podem usar as portas no `teardown`, o
    tracer precisa despachar o que esta na fila antes de o processo morrer e o
    pool do banco so fecha depois que ninguem mais vai consultar. Uma falha em
    qualquer etapa vira WARNING — um encerramento que levanta excecao deixa o
    `SIGTERM` do Kubernetes virar `SIGKILL` e derruba requisicoes em voo.
    """
    await _teardown_modules()
    await _flush_tracer(container.tracer)
    await _close_client(container.llm, port="llm")
    await _close_client(container.embeddings, port="embeddings")
    await dispose_engine(engine)
    _logger.info("database_pool_disposed")
    _logger.info("container_disposed")


async def _teardown_modules() -> None:
    """Chama `teardown()` em cada building block que chegou a ser inicializado.

    As instancias vivas ficam no cache de processo de
    :class:`~lukato.application.use_cases.modules.InvokeModule`, alimentado por
    `setup(ctx)`. So elas precisam de `teardown`: uma classe registrada e nunca
    invocada nao abriu recurso nenhum.
    """
    initialized: dict[str, Any] = getattr(InvokeModule, "_initialized", {})
    for slug, module in list(initialized.items()):
        try:
            await module.teardown()
        except Exception as exc:
            _logger.warning(
                "module_teardown_failed",
                module=slug,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            _logger.info("module_teardown", module=slug)
    initialized.clear()


async def _flush_tracer(tracer: TracerPort) -> None:
    """Despacha a fila de traces e fecha o cliente de telemetria, se houver."""
    try:
        await tracer.flush()
    except Exception as exc:
        _logger.warning("tracer_flush_failed", error=f"{type(exc).__name__}: {exc}")
    await _close_client(tracer, port="tracer")


async def _close_client(adapter: Any, *, port: str) -> None:
    """Fecha o cliente HTTP de um adaptador que exponha `aclose()`, se existir.

    Nem toda implementacao de porta abre socket — `EchoLLM`, `HashingEmbedder` e
    `NoopTracer` nao abrem — por isso o metodo e opcional e a ausencia dele nao e
    erro: e a assinatura de um adaptador offline.
    """
    close = getattr(adapter, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:
        _logger.warning(
            "adapter_close_failed",
            port=port,
            adapter=type(adapter).__name__,
            error=f"{type(exc).__name__}: {exc}",
        )
    else:
        _logger.info("adapter_closed", port=port, adapter=type(adapter).__name__)
