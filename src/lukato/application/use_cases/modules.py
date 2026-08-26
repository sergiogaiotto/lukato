"""Casos de uso das definicoes de modulo, incluindo o caminho unico de execucao.

`InvokeModule` e o coracao da plataforma: e o **unico** lugar onde um building
block executa, e ele cumpre as onze etapas normativas da SPEC-0001 secao 4 nesta
ordem exata, sem atalho.

```text
 1 resolve definicao        2 exige status ACTIVE       3 exige MODULE_INVOKE
 4 verifica orcamentos      5 abre trace + AgentRun     6 GUARDRAIL DE ENTRADA
 7 renderiza system prompt  8 executa o modulo          9 GUARDRAIL DE SAIDA
10 UsageRecord + custo     11 finaliza o run e commita
```

A trinca **envolve sempre o `handle`**, seja qual for o building block:

```text
guardrail de entrada -> module.handle(request, ctx) -> guardrail de saida
```

O modulo generico `processing` executa as etapas 7 a 9 pela fachada
`ctx.services["pipeline"]` (:class:`ModulePipeline`), que ja recebe o system
prompt renderizado e chama o runtime declarado no binding; os demais modulos
apenas implementam `handle` e continuam cercados pela mesma trinca. Um modulo
`AGENT` que devolve resposta vazia sem ter usado a fachada recebe a execucao
padrao do runtime — a etapa 8 nunca deixa de acontecer para um agente.

Nada e enviado ao provedor antes da etapa 6, e qualquer excecao entre as etapas
5 e 11 grava `AgentRun(FAILED)` antes de propagar: nao existe execucao invisivel.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final

from lukato.application.container import Container
from lukato.application.dto import (
    InvokeInput,
    InvokeOutput,
    ModuleCreateInput,
    ModuleFilter,
    ModuleUpdateInput,
    Page,
)
from lukato.config import get_logger
from lukato.domain.errors import (
    BudgetExceededError,
    ConflictError,
    ForbiddenError,
    GuardrailViolation,
    LukatoError,
    ModuleError,
    ModuleNotFound,
    ProviderError,
)
from lukato.domain.models.finops import Budget, BudgetPeriod, UsageRecord
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailFinding,
    GuardrailPolicy,
    GuardrailStage,
    GuardrailVerdict,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.module import ModuleBinding, ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.models.run import AgentRun, RunStatus, RunStep, StepKind, TokenUsage
from lukato.domain.ports.llm import ChatMessage, LLMPort, LLMResponse
from lukato.domain.ports.observability import SpanHandle
from lukato.domain.ports.orchestrator import (
    OrchestratorPort,
    OrchestratorRequest,
    OrchestratorResult,
)
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.services.module_composer import ComposedPipeline
from lukato.domain.types import Id, Json, slugify, utcnow
from lukato.modules.base import BaseModule, ModuleContext, ModuleRequest, ModuleResponse

__all__ = [
    "CLASS_CONFIG_KEYS",
    "COST_DIGITS",
    "MAX_STEP_TEXT_CHARS",
    "CreateModule",
    "DeleteModule",
    "GetModule",
    "InvokeModule",
    "ListModules",
    "ModulePipeline",
    "SetModuleStatus",
    "UpdateModule",
    "authorize",
    "class_candidates",
]

_logger = get_logger(__name__)

MAX_STEP_TEXT_CHARS: Final[int] = 4000
"""Recorte de texto gravado na trilha de execucao (a trilha nao e o dado)."""

COST_DIGITS: Final[int] = 8
"""Casas decimais do custo agregado no run (SPEC-0005 secao 2)."""

CLASS_CONFIG_KEYS: Final[tuple[str, ...]] = ("module", "implementation")
"""Chaves de `ModuleDefinition.config` que apontam a classe do building block.

E o que permite duas definicoes (`triagem`, `resumo`) sobre a mesma classe
`processing`: a definicao e configuracao, a classe e codigo (SPEC-0001 secao 3).
Sem essas chaves, o slug da definicao e procurado no registry.
"""

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
"""Inicio de periodo usado por orcamentos de escopo `TOTAL`."""


# ---------------------------------------------------------------------------
# Utilitarios internos
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int = MAX_STEP_TEXT_CHARS) -> str:
    """Recorta textos longos antes de grava-los na trilha de execucao."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def class_candidates(definition: ModuleDefinition) -> list[str]:
    """Slugs de building block que podem atender a definicao, na ordem de busca.

    A definicao (`assistente`) e a classe (`processing`) sao coisas diferentes:
    duas definicoes rodam sobre a mesma classe, com bindings distintos. Quem
    invoca sempre soube disso; o painel do console nao — ele procurava a classe
    pelo slug da DEFINICAO e, nao achando, escrevia "nenhuma classe registrada
    com este slug; a invocacao precisa de um building block no Registry". O aviso
    aparecia em todo modulo cuja definicao tem nome de dominio, e era falso: a
    invocacao funcionava. Com a regra num lugar so, painel e invocacao nao tem
    como divergir de novo.
    """
    candidatos = [
        valor.strip()
        for chave in CLASS_CONFIG_KEYS
        if isinstance(valor := definition.config.get(chave), str) and valor.strip()
    ]
    candidatos.append(definition.slug)
    return candidatos


def authorize(principal: Principal, permission: Permission, action: str) -> None:
    """Exige a permissao do principal; sem ela levanta :class:`ForbiddenError`.

    `Principal.can` e a unica forma de autorizar no lukato (SPEC-0006 secao 1):
    nenhum caso de uso e nenhum endpoint compara papeis diretamente.
    """
    if principal.can(permission):
        return
    raise ForbiddenError(
        f"O principal '{principal.subject}' ({principal.role.value}) nao pode {action}.",
        details={
            "subject": principal.subject,
            "role": principal.role.value,
            "required_permission": permission.value,
        },
    )


async def _find_definition(uow: UnitOfWork, reference: str) -> ModuleDefinition | None:
    """Resolve a definicao por slug e, em seguida, por identificador."""
    candidate = (reference or "").strip()
    if not candidate:
        return None
    found = await uow.modules.get_by_slug(candidate)
    if found is not None:
        return found
    return await uow.modules.get(candidate)


async def _require_definition(uow: UnitOfWork, reference: str) -> ModuleDefinition:
    """Resolve a definicao ou levanta :class:`ModuleNotFound` (etapa 1)."""
    found = await _find_definition(uow, reference)
    if found is None:
        raise ModuleNotFound(
            f"Modulo '{reference}' nao encontrado.",
            details={"reference": reference},
        )
    return found


def _require_active(definition: ModuleDefinition) -> None:
    """Exige `status == ACTIVE` para invocar (etapa 2)."""
    if definition.status is ModuleStatus.ACTIVE:
        return
    raise ConflictError(
        f"O modulo '{definition.slug}' esta em '{definition.status.value}' e nao pode "
        f"ser invocado; ative a definicao antes.",
        details={
            "slug": definition.slug,
            "status": definition.status.value,
            "required_status": ModuleStatus.ACTIVE.value,
        },
    )


def _period_start(period: BudgetPeriod, *, now: datetime) -> datetime:
    """Instante inicial da janela de apuracao do orcamento."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is BudgetPeriod.DAILY:
        return midnight
    if period is BudgetPeriod.WEEKLY:
        return midnight - timedelta(days=midnight.weekday())
    if period is BudgetPeriod.MONTHLY:
        return midnight.replace(day=1)
    return _EPOCH


def _findings_payload(findings: Sequence[GuardrailFinding]) -> list[Json]:
    """Serializa achados de guardrail em JSON puro (o `span` vira lista)."""
    return [finding.model_dump(mode="json") for finding in findings]


def _blocking_finding(verdict: GuardrailVerdict) -> GuardrailFinding | None:
    """Primeiro achado com acao `BLOCK` do veredito, quando houver."""
    for finding in verdict.findings:
        if finding.action is GuardrailAction.BLOCK:
            return finding
    return verdict.findings[0] if verdict.findings else None


def _text_of(payload: Json, *keys: str) -> str:
    """Primeiro valor textual encontrado no dicionario, entre as chaves informadas."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _step_model(step: RunStep, default_model: str) -> str:
    """Modelo cobravel de um step de LLM, com o modelo do binding como padrao."""
    for source in (step.input, step.output):
        value = source.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default_model


def _span_update(span: SpanHandle, **attributes: Any) -> None:
    """Atualiza um span sem deixar falha de telemetria derrubar a execucao."""
    try:
        span.update(**attributes)
    except Exception as exc:  # pragma: no cover - adaptador de tracing defeituoso
        _logger.warning("trace_span_update_failed", error=f"{type(exc).__name__}: {exc}")


def _trace_id_of(span: SpanHandle) -> str | None:
    """Le o `trace_id` do span aberto, tolerando tracer degradado."""
    try:
        return span.trace_id
    except Exception as exc:  # pragma: no cover - adaptador de tracing defeituoso
        _logger.warning("trace_id_read_failed", error=f"{type(exc).__name__}: {exc}")
        return None


class _UseCase:
    """Base dos casos de uso: guarda o `Container` recebido por injecao."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container


# ---------------------------------------------------------------------------
# CRUD de definicoes
# ---------------------------------------------------------------------------
class CreateModule(_UseCase):
    """Cria uma `ModuleDefinition` (a *configuracao* de um building block)."""

    async def execute(self, data: ModuleCreateInput, principal: Principal) -> ModuleDefinition:
        """Grava a definicao, validando slug unico e `config` contra o schema da classe."""
        authorize(principal, Permission.MODULE_WRITE, "criar definicoes de modulo")
        slug = slugify(data.slug or data.name)
        definition = ModuleDefinition(
            slug=slug,
            name=data.name or slug,
            description=data.description,
            kind=data.kind,
            status=data.status,
            runtime=data.runtime,
            binding=data.binding or ModuleBinding(),
            config=self._validated_config(slug, dict(data.config)),
            tags=list(data.tags),
            owner=data.owner,
            version=data.version,
        )
        async with self._container.uow_factory() as uow:
            if await uow.modules.get_by_slug(slug) is not None:
                raise ConflictError(
                    f"Ja existe um modulo com o slug '{slug}'.",
                    details={"slug": slug},
                )
            created = await uow.modules.add(definition)
            await uow.commit()
        _logger.info("module_created", slug=created.slug, kind=created.kind.value)
        return created

    def _validated_config(self, slug: str, config: Json) -> Json:
        """Valida `config` contra o `config_schema` da classe, quando ela ja existe.

        Uma definicao pode preceder o codigo (seeds, importacao); nesse caso a
        validacao e simplesmente pulada e a lacuna aparece na invocacao.
        """
        registry = self._container.registry
        candidate = config.get(CLASS_CONFIG_KEYS[0]) or slug
        if not isinstance(candidate, str) or candidate not in registry:
            return config
        return registry.instantiate(candidate).validate_config(config)


class UpdateModule(_UseCase):
    """Atualiza parcialmente uma definicao existente."""

    async def execute(
        self, reference: str, data: ModuleUpdateInput, principal: Principal
    ) -> ModuleDefinition:
        """Aplica somente os campos informados e grava a definicao."""
        authorize(principal, Permission.MODULE_WRITE, "alterar definicoes de modulo")
        changes = data.changes()
        async with self._container.uow_factory() as uow:
            definition = await _require_definition(uow, reference)
            if not changes:
                return definition
            updated = definition.model_copy(update={**changes, "updated_at": utcnow()})
            stored = await uow.modules.update(updated)
            await uow.commit()
        _logger.info("module_updated", slug=stored.slug, fields=sorted(changes))
        return stored


class GetModule(_UseCase):
    """Busca uma definicao por slug ou por identificador."""

    async def execute(self, reference: str, principal: Principal) -> ModuleDefinition:
        """Devolve a definicao; ausente levanta :class:`ModuleNotFound`."""
        authorize(principal, Permission.MODULE_READ, "ler definicoes de modulo")
        async with self._container.uow_factory() as uow:
            return await _require_definition(uow, reference)


class ListModules(_UseCase):
    """Lista definicoes paginadas, com filtros de tipo, status e busca textual."""

    async def execute(self, filters: ModuleFilter, principal: Principal) -> Page[ModuleDefinition]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, Permission.MODULE_READ, "listar definicoes de modulo")
        criteria: Json = {}
        if filters.kind is not None:
            criteria["kind"] = filters.kind
        if filters.status is not None:
            criteria["status"] = filters.status
        if filters.search:
            criteria["search"] = filters.search
        async with self._container.uow_factory() as uow:
            items = await uow.modules.list(**criteria, limit=filters.limit, offset=filters.offset)
            total = await uow.modules.count(**criteria)
        return Page(items=list(items), total=total, limit=filters.limit, offset=filters.offset)


class DeleteModule(_UseCase):
    """Remove uma definicao de modulo do catalogo."""

    async def execute(self, reference: str, principal: Principal) -> None:
        """Apaga a definicao; a classe registrada permanece disponivel."""
        authorize(principal, Permission.MODULE_WRITE, "remover definicoes de modulo")
        async with self._container.uow_factory() as uow:
            definition = await _require_definition(uow, reference)
            await uow.modules.delete(definition.id)
            await uow.commit()
        _logger.info("module_deleted", slug=definition.slug)


class SetModuleStatus(_UseCase):
    """Muda o status do ciclo de vida de uma definicao."""

    async def execute(
        self, reference: str, status: ModuleStatus, principal: Principal
    ) -> ModuleDefinition:
        """Publica, pausa ou deprecia a definicao; status igual e no-op idempotente."""
        authorize(principal, Permission.MODULE_WRITE, "alterar o status de modulos")
        async with self._container.uow_factory() as uow:
            definition = await _require_definition(uow, reference)
            if definition.status is status:
                return definition
            updated = definition.model_copy(update={"status": status, "updated_at": utcnow()})
            stored = await uow.modules.update(updated)
            await uow.commit()
        _logger.info(
            "module_status_changed",
            slug=stored.slug,
            previous=definition.status.value,
            status=status.value,
        )
        return stored


# ---------------------------------------------------------------------------
# Fachada da trinca entregue ao building block
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ModulePipeline:
    """Fachada da trinca oferecida ao modulo em `ctx.services["pipeline"]`.

    O system prompt ja chega renderizado (etapa 7). `complete` faz uma chamada
    unica de LLM e `orchestrate` executa o runtime declarado no binding
    (etapa 8). Ambos registram os `RunStep` na trilha do `AgentRun`, de modo que
    o custo da etapa 10 e capturado no mesmo lugar, sempre.

    O modulo **nao** aplica guardrails: quem cerca o `handle` com a trinca e o
    caso de uso :class:`InvokeModule`.
    """

    definition: ModuleDefinition
    composed: ComposedPipeline
    system_prompt: str
    llm: LLMPort
    resolve_orchestrator: Callable[[str], OrchestratorPort]
    record: Callable[[RunStep], RunStep]
    run_id: Id
    tenant_id: str
    calls: list[str] = field(default_factory=list)

    @property
    def binding(self) -> ModuleBinding:
        """Binding efetivo do modulo (modelo, temperatura, teto de tokens, tools)."""
        return self.definition.binding

    @property
    def used_llm(self) -> bool:
        """True quando o modulo ja executou alguma chamada por esta fachada."""
        return bool(self.calls)

    def messages(
        self,
        text: str,
        *,
        history: Sequence[ChatMessage] = (),
        system_prompt: str | None = None,
    ) -> list[ChatMessage]:
        """Monta `[system?] + history + user` na ordem exigida pela plataforma."""
        prompt = self.system_prompt if system_prompt is None else system_prompt
        messages: list[ChatMessage] = []
        if prompt.strip():
            messages.append(ChatMessage.system(prompt))
        messages.extend(ChatMessage(role=item.role, content=item.content) for item in history)
        messages.append(ChatMessage.user(text))
        return messages

    async def complete(
        self,
        text: str,
        *,
        variables: Json | None = None,
        history: Sequence[ChatMessage] = (),
        response_format: Json | None = None,
    ) -> LLMResponse:
        """Aplica system prompt + LLM numa unica chamada, respeitando o binding.

        `variables` re-renderiza o system prompt quando o modulo precisa de um
        contexto diferente do resolvido na etapa 7; ausente, usa o ja renderizado.
        """
        prompt = self.system_prompt
        if variables:
            prompt = self.composed.render_system_prompt(dict(variables))
        messages = self.messages(text, history=history, system_prompt=prompt)
        started_at = utcnow()
        clock = time.perf_counter()
        response = await self._chat(messages, response_format=response_format)
        latency_ms = (time.perf_counter() - clock) * 1000.0
        self.calls.append("complete")
        self.record(
            RunStep(
                run_id=self.run_id,
                index=0,
                kind=StepKind.LLM,
                name="pipeline.complete",
                input={
                    "messages": len(messages),
                    "model": response.model or self.binding.model or "",
                    "temperature": self.binding.temperature,
                    "max_tokens": self.binding.max_tokens,
                    "prompt": _clip(text),
                },
                output={
                    "content": _clip(response.content),
                    "finish_reason": response.finish_reason,
                },
                usage=response.usage,
                latency_ms=round(latency_ms, 3),
                started_at=started_at,
                finished_at=utcnow(),
            )
        )
        return response

    async def orchestrate(
        self,
        text: str,
        *,
        variables: Json | None = None,
        history: Sequence[ChatMessage] = (),
        tools: Sequence[str] | None = None,
    ) -> OrchestratorResult:
        """Executa o runtime declarado no modulo (etapa 8) e adota os seus steps."""
        orchestrator = self.resolve_orchestrator(self.definition.runtime)
        request = OrchestratorRequest(
            module=self.definition,
            input_text=text,
            variables=dict(variables or {}),
            history=[ChatMessage(role=item.role, content=item.content) for item in history],
            tools=list(tools if tools is not None else self.composed.tools),
            system_prompt=self.system_prompt,
            metadata={
                "run_id": self.run_id,
                "module_slug": self.definition.slug,
                "tenant_id": self.tenant_id,
                "runtime": self.definition.runtime,
            },
        )
        try:
            result = await orchestrator.run(request)
        except LukatoError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"O runtime '{self.definition.runtime}' falhou ao executar o modulo "
                f"'{self.definition.slug}': {type(exc).__name__}: {exc}",
                details={
                    "runtime": self.definition.runtime,
                    "module_slug": self.definition.slug,
                    "error": type(exc).__name__,
                },
            ) from exc
        self.calls.append("orchestrate")
        for step in result.steps:
            self.record(step)
        return result

    async def _chat(
        self, messages: Sequence[ChatMessage], *, response_format: Json | None
    ) -> LLMResponse:
        """Chama o `LLMPort` com o timeout do binding, convertendo falhas em `ProviderError`."""
        timeout = self.binding.timeout_seconds if self.binding.timeout_seconds > 0 else None
        try:
            if timeout is None:
                return await self._chat_once(messages, response_format=response_format)
            async with asyncio.timeout(timeout):
                return await self._chat_once(messages, response_format=response_format)
        except TimeoutError as exc:
            raise ProviderError(
                f"O provedor de LLM nao respondeu em {timeout:.0f}s.",
                details={"timeout_seconds": timeout, "model": self.binding.model},
            ) from exc
        except LukatoError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Falha na chamada de LLM do modulo '{self.definition.slug}': "
                f"{type(exc).__name__}: {exc}",
                details={"module_slug": self.definition.slug, "error": type(exc).__name__},
            ) from exc

    async def _chat_once(
        self, messages: Sequence[ChatMessage], *, response_format: Json | None
    ) -> LLMResponse:
        """Executa a chamada de chat com os parametros do binding."""
        return await self.llm.chat(
            messages,
            model=self.binding.model,
            temperature=self.binding.temperature,
            max_tokens=self.binding.max_tokens,
            response_format=response_format,
            metadata={
                "module_slug": self.definition.slug,
                "run_id": self.run_id,
                "tenant_id": self.tenant_id,
            },
        )


# ---------------------------------------------------------------------------
# O caso de uso central
# ---------------------------------------------------------------------------
class InvokeModule(_UseCase):
    """Executa um building block cumprindo as onze etapas normativas.

    A instancia do modulo vem do registry (cache por slug) e `setup(ctx)` roda
    uma unica vez por instancia, serializado por um `asyncio.Lock` por slug: sob
    concorrencia, duas requisicoes simultaneas do mesmo modulo nao inicializam
    o building block duas vezes.
    """

    _setup_locks: ClassVar[dict[str, asyncio.Lock]] = {}
    """Trava de inicializacao por slug de classe (privada da classe)."""

    _initialized: ClassVar[dict[str, BaseModule]] = {}
    """Instancia ja inicializada por slug de classe; troca de instancia refaz o setup."""

    async def execute(
        self, slug: str, request: ModuleRequest, principal: Principal
    ) -> ModuleResponse:
        """Executa o modulo `slug` e devolve a resposta com custo, findings e run."""
        container = self._container
        started = time.perf_counter()

        async with container.uow_factory() as uow:
            definition = await _require_definition(uow, slug)  # 1
            _require_active(definition)  # 2
            authorize(principal, Permission.MODULE_INVOKE, f"invocar o modulo '{slug}'")  # 3
            await self._check_budgets(uow, definition, principal)  # 4

        run = AgentRun(
            module_id=definition.id,
            module_slug=definition.slug,
            status=RunStatus.RUNNING,
            input={
                "input": _clip(request.input),
                "payload": request.payload,
                "variables": request.variables,
                "stream": request.stream,
            },
            tenant_id=principal.tenant_id,
            actor=principal.subject,
        )

        # 5 — trace aberto e run persistido antes de qualquer execucao.
        async with container.tracer.trace(
            f"module.invoke:{definition.slug}",
            input={"input": _clip(request.input, 512)},
            metadata={
                "module_slug": definition.slug,
                "run_id": run.id,
                "tenant_id": run.tenant_id,
                "actor": run.actor,
                "environment": container.settings.app.env,
                "version": container.settings.app.version,
                "runtime": definition.runtime,
            },
            user_id=principal.subject,
            session_id=run.id,
            tags=[definition.slug, definition.kind.value],
        ) as span:
            run.trace_id = _trace_id_of(span)
            await self._open_run(run)
            try:
                response = await self._run_pipeline(
                    run=run,
                    definition=definition,
                    request=request,
                    principal=principal,
                    started=started,
                )
            except GuardrailViolation as exc:
                await self._close_blocked(run, exc, started=started)
                raise
            except LukatoError as exc:
                await self._close_failed(run, exc, started=started)
                raise
            except Exception as exc:
                failure = ModuleError(
                    f"Falha inesperada ao executar o modulo '{definition.slug}': "
                    f"{type(exc).__name__}: {exc}",
                    details={"module_slug": definition.slug, "error": type(exc).__name__},
                )
                await self._close_failed(run, failure, started=started)
                raise failure from exc
            else:
                _span_update(
                    span,
                    output={"output": _clip(response.output, 512)},
                    metadata={"status": run.status.value, "cost_usd": run.cost_usd},
                )
                return response
            finally:
                await self._emit_scores(run)

    async def invoke(self, data: InvokeInput) -> InvokeOutput:
        """Variante orientada a DTO, conveniente para a camada `interfaces`."""
        response = await self.execute(data.slug, data.request, data.principal)
        return InvokeOutput.from_response(response)

    # -- etapa 4 -----------------------------------------------------------
    async def _check_budgets(
        self, uow: UnitOfWork, definition: ModuleDefinition, principal: Principal
    ) -> None:
        """Bloqueia a invocacao quando um orcamento com `hard_stop` esta estourado."""
        calculator = self._container.cost_calculator
        now = utcnow()
        for scope in (
            "global",
            f"module:{definition.slug}",
            f"tenant:{principal.tenant_id}",
        ):
            for budget in await uow.budgets.list(scope=scope, is_active=True):
                if not budget.hard_stop:
                    continue
                spent = await uow.usage.total_since(
                    _period_start(budget.period, now=now), scope=budget.scope
                )
                check = calculator.check_budget(budget, spent)
                if check.blocked:
                    raise self._budget_error(budget, check.ratio, spent)
                if check.alert:
                    _logger.warning(
                        "budget_alert",
                        budget=budget.name,
                        scope=budget.scope,
                        ratio=check.ratio,
                        spent=check.spent,
                        limit_usd=check.limit_usd,
                    )

    @staticmethod
    def _budget_error(budget: Budget, ratio: float, spent: float) -> BudgetExceededError:
        """Monta o erro de orcamento estourado (HTTP 402)."""
        return BudgetExceededError(
            f"Orcamento '{budget.name}' ({budget.scope}) estourado: "
            f"{spent:.8g} USD de {budget.limit_usd:.8g} USD no periodo "
            f"'{budget.period.value}'.",
            details={
                "budget_id": budget.id,
                "budget_name": budget.name,
                "scope": budget.scope,
                "period": budget.period.value,
                "limit_usd": budget.limit_usd,
                "spent_usd": round(spent, COST_DIGITS),
                "ratio": ratio,
                "hard_stop": True,
            },
        )

    # -- etapas 6 a 11 -----------------------------------------------------
    async def _run_pipeline(
        self,
        *,
        run: AgentRun,
        definition: ModuleDefinition,
        request: ModuleRequest,
        principal: Principal,
        started: float,
    ) -> ModuleResponse:
        """Executa a trinca ao redor do `handle` e finaliza o `AgentRun`."""
        container = self._container
        async with container.uow_factory() as uow:
            composed = await container.composer.compose(
                definition, prompts=uow.prompts, guardrails=uow.guardrails
            )
        module = self._resolve_module(definition)

        # 6 — guardrail de entrada: nada sai para o provedor antes daqui.
        verdict_in = await self._guardrail(
            run=run,
            definition=definition,
            principal=principal,
            stage=GuardrailStage.INPUT,
            policy=composed.input_policy,
            content=request.input,
        )
        guarded = (
            request.model_copy(update={"input": verdict_in.content})
            if verdict_in.modified
            else request
        )

        # 7 — system prompt.
        variables = self._variables(definition, guarded, principal)
        system_prompt = self._render_prompt(run=run, composed=composed, variables=variables)

        # 8 — execucao do modulo, sempre cercada pela trinca.
        pipeline = ModulePipeline(
            definition=definition,
            composed=composed,
            system_prompt=system_prompt,
            llm=container.llm,
            resolve_orchestrator=container.orchestrator_for,
            record=lambda step: self._adopt(run, step),
            run_id=run.id,
            tenant_id=run.tenant_id,
        )
        context = self._build_context(definition, principal, pipeline, run)
        await self._ensure_setup(module, context)
        response = await self._handle(module, guarded, context)
        response = await self._ensure_runtime(
            response, module=module, pipeline=pipeline, request=guarded, variables=variables
        )

        # 9 — guardrail de saida.
        verdict_out = await self._guardrail(
            run=run,
            definition=definition,
            principal=principal,
            stage=GuardrailStage.OUTPUT,
            policy=composed.output_policy,
            content=response.output,
        )

        # 10 — consumo e custo por chamada de LLM.
        records = self._bill(run, definition, default_model=composed.model)

        # 11 — fecha o run e commita tudo numa unica transacao.
        findings = [*verdict_in.findings, *verdict_out.findings]
        final = response.model_copy(
            update={
                "output": verdict_out.content,
                "run_id": run.id,
                "usage": run.usage,
                "cost_usd": run.cost_usd,
                "findings": findings,
                "metadata": {
                    **response.metadata,
                    "module_slug": definition.slug,
                    "runtime": definition.runtime,
                    "model": composed.model,
                    "run_id": run.id,
                    "trace_id": run.trace_id,
                    "status": RunStatus.SUCCEEDED.value,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "guardrail_findings": len(findings),
                },
            }
        )
        run.status = RunStatus.SUCCEEDED
        run.output = {
            "output": _clip(final.output),
            "data": final.data,
            "findings": len(findings),
        }
        run.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        run.finished_at = utcnow()
        run.touch()
        await self._persist(run, records=records)
        self._observe_metrics(run, definition, records=records)
        _logger.info(
            "module_invoked",
            module=definition.slug,
            run_id=run.id,
            status=run.status.value,
            steps=len(run.steps),
            cost_usd=run.cost_usd,
            total_tokens=run.usage.total_tokens,
            latency_ms=run.latency_ms,
        )
        return final

    # -- metricas ----------------------------------------------------------
    def _observe_metrics(
        self,
        run: AgentRun,
        definition: ModuleDefinition,
        *,
        records: Sequence[UsageRecord] = (),
    ) -> None:
        """Alimenta os contadores de negocio da SPEC-0008 secao 4.

        Sem esta chamada, seis das nove metricas ficam declaradas e permanentemente
        vazias: um painel de Prometheus montado sobre a SPEC mostraria series que
        nunca recebem amostra. Falha de telemetria nunca derruba a requisicao, entao
        tudo aqui e best-effort.
        """
        metrics = self._container.metrics
        if metrics is None:
            return
        try:
            metrics.observe_module(
                module=definition.slug,
                runtime=definition.runtime,
                status=run.status.value,
                duration=max(0.0, run.latency_ms) / 1000.0,
            )
            for record in records:
                metrics.observe_llm(
                    model=record.model,
                    module=definition.slug,
                    usage=record.usage,
                    cost=record.cost_usd,
                )
        except Exception as exc:  # pragma: no cover - telemetria nunca derruba
            _logger.warning(
                "metrics_observation_failed",
                module=definition.slug,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _observe_guardrail(
        self,
        stage: GuardrailStage,
        policy: GuardrailPolicy | None,
        verdict: GuardrailVerdict,
    ) -> None:
        """Contabiliza os achados do estagio, onde `stage` e `policy` sao conhecidos.

        Observar isto no fim da invocacao perderia a informacao: `GuardrailFinding`
        nao carrega o estagio nem a politica, e a metrica sairia com `stage=unknown`.
        """
        metrics = self._container.metrics
        if metrics is None or not verdict.findings:
            return
        slug = policy.slug if policy is not None else None
        try:
            for finding in verdict.findings:
                metrics.observe_guardrail(
                    stage=stage.value,
                    kind=finding.kind.value,
                    action=finding.action.value,
                    blocked=finding.action is GuardrailAction.BLOCK,
                    policy=slug,
                )
        except Exception as exc:  # pragma: no cover - telemetria nunca derruba
            _logger.warning(
                "metrics_guardrail_failed", stage=stage.value, error=f"{type(exc).__name__}: {exc}"
            )

    # -- guardrails --------------------------------------------------------
    async def _guardrail(
        self,
        *,
        run: AgentRun,
        definition: ModuleDefinition,
        principal: Principal,
        stage: GuardrailStage,
        policy: GuardrailPolicy | None,
        content: str,
    ) -> GuardrailVerdict:
        """Aplica a politica do estagio e registra o step correspondente.

        Politica `None` e "sem restricao naquele estagio": escolha explicita,
        registrada com `policy_id=null`, nunca um erro (SPEC-0003 secao 1).
        """
        kind = StepKind.GUARDRAIL_IN if stage is GuardrailStage.INPUT else StepKind.GUARDRAIL_OUT
        name = f"guardrail.{stage.value}"
        context: Json = {
            "stage": stage.value,
            "module_slug": definition.slug,
            "run_id": run.id,
            "tenant_id": run.tenant_id,
            "actor": principal.subject,
        }
        started_at = utcnow()
        clock = time.perf_counter()
        async with self._container.tracer.span(
            name,
            kind="guardrail",
            input={"policy": policy.slug if policy else None, "chars": len(content)},
        ) as span:
            try:
                verdict = await self._container.guardrails.apply(content, policy, context=context)
            except LukatoError as exc:
                self._append_step(
                    run,
                    kind=kind,
                    name=name,
                    status=RunStatus.FAILED,
                    inputs={"policy": policy.slug if policy else None},
                    error=exc.message,
                    latency_ms=(time.perf_counter() - clock) * 1000.0,
                    started_at=started_at,
                )
                raise
            self._observe_guardrail(stage, policy, verdict)
            _span_update(
                span,
                output={
                    "blocked": verdict.blocked,
                    "findings": len(verdict.findings),
                    "modified": verdict.modified,
                },
            )

        self._append_step(
            run,
            kind=kind,
            name=name,
            status=RunStatus.BLOCKED if verdict.blocked else RunStatus.SUCCEEDED,
            inputs={
                "policy": policy.slug if policy else None,
                "policy_id": verdict.policy_id,
                "rules": len(policy.rules) if policy else 0,
                "chars": len(content),
            },
            outputs={
                "allowed": verdict.allowed,
                "blocked": verdict.blocked,
                "modified": verdict.modified,
                "findings": _findings_payload(verdict.findings),
            },
            latency_ms=verdict.latency_ms or (time.perf_counter() - clock) * 1000.0,
            started_at=started_at,
        )
        if verdict.blocked:
            run.status = RunStatus.BLOCKED
            finding = _blocking_finding(verdict)
            message = (
                finding.message
                if finding and finding.message
                else f"Conteudo bloqueado pelo guardrail de {stage.value}."
            )
            raise GuardrailViolation(
                message,
                policy_id=verdict.policy_id,
                rule_id=finding.rule_id if finding else None,
                stage=stage.value,
                details={
                    "module_slug": definition.slug,
                    "run_id": run.id,
                    "findings": _findings_payload(verdict.findings),
                },
            )
        return verdict

    # -- system prompt -----------------------------------------------------
    def _variables(
        self, definition: ModuleDefinition, request: ModuleRequest, principal: Principal
    ) -> Json:
        """Monta as variaveis do prompt: `config.variables` + pedido + contexto."""
        declared = definition.config.get("variables")
        variables: Json = dict(declared) if isinstance(declared, dict) else {}
        variables.update(request.variables)
        variables.setdefault("input", request.input)
        variables.setdefault("module_slug", definition.slug)
        variables.setdefault("tenant_id", principal.tenant_id)
        return variables

    def _render_prompt(self, *, run: AgentRun, composed: ComposedPipeline, variables: Json) -> str:
        """Renderiza o system prompt vinculado (etapa 7) e registra o step `PROMPT`."""
        prompt = composed.prompt
        started_at = utcnow()
        clock = time.perf_counter()
        try:
            rendered = composed.render_system_prompt(variables)
        except LukatoError as exc:
            self._append_step(
                run,
                kind=StepKind.PROMPT,
                name="prompt.render",
                status=RunStatus.FAILED,
                inputs={"prompt": prompt.slug if prompt else None},
                error=exc.message,
                latency_ms=(time.perf_counter() - clock) * 1000.0,
                started_at=started_at,
            )
            raise
        self._append_step(
            run,
            kind=StepKind.PROMPT,
            name="prompt.render",
            inputs={
                "prompt": prompt.slug if prompt else None,
                "prompt_id": prompt.id if prompt else None,
                "version": prompt.version if prompt else None,
                "variables": sorted(variables),
            },
            outputs={"chars": len(rendered), "content": _clip(rendered)},
            latency_ms=(time.perf_counter() - clock) * 1000.0,
            started_at=started_at,
        )
        return rendered

    # -- execucao do building block ----------------------------------------
    def _resolve_module(self, definition: ModuleDefinition) -> BaseModule:
        """Instancia a classe do building block que atende a definicao."""
        registry = self._container.registry
        candidates = class_candidates(definition)
        for candidate in candidates:
            if candidate in registry:
                return registry.instantiate(candidate)
        raise ModuleNotFound(
            f"Nenhum building block registrado atende a definicao '{definition.slug}'.",
            details={
                "slug": definition.slug,
                "candidates": candidates,
                "available": registry.slugs(),
            },
        )

    def _build_context(
        self,
        definition: ModuleDefinition,
        principal: Principal,
        pipeline: ModulePipeline,
        run: AgentRun,
    ) -> ModuleContext:
        """Monta o `ModuleContext` com portas e servicos auxiliares do container.

        `tools` so entra quando ha catalogo configurado: ausente, o modulo recebe
        `UnsupportedCapability` de `ctx.service("tools")` — degradacao explicita.
        """
        container = self._container
        services: dict[str, Any] = {
            # Os building blocks que orquestram casos de uso proprios (auth, finops,
            # knowledge, adwatch) constroem-nos com o container. Sem esta chave eles
            # levantam UnsupportedCapability na primeira invocacao — so `processing`
            # sobrevive, porque nao usa caso de uso nenhum.
            "container": container,
            "composer": container.composer,
            "cost_calculator": container.cost_calculator,
            "vector_store": container.vector_store,
            "media": container.media,
            "pipeline": pipeline,
            "registry": container.registry,
            "run_id": run.id,
        }
        if container.tools is not None:
            services["tools"] = container.tools
        return ModuleContext(
            definition=definition,
            principal=principal,
            llm=container.llm,
            embeddings=container.embeddings,
            guardrails=container.guardrails,
            tracer=container.tracer,
            uow_factory=container.uow_factory,
            orchestrators=dict(container.orchestrators),
            settings=container.settings,
            services=services,
        )

    async def _ensure_setup(self, module: BaseModule, context: ModuleContext) -> None:
        """Chama `setup(ctx)` uma unica vez por instancia, sob trava por slug."""
        key = type(module).slug
        if self._initialized.get(key) is module:
            return
        lock = self._setup_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self._initialized.get(key) is module:
                return
            try:
                await module.setup(context)
            except LukatoError:
                raise
            except Exception as exc:
                raise ModuleError(
                    f"Falha no setup do modulo '{key}': {type(exc).__name__}: {exc}",
                    details={"module": key, "error": type(exc).__name__},
                ) from exc
            self._initialized[key] = module

    async def _handle(
        self, module: BaseModule, request: ModuleRequest, context: ModuleContext
    ) -> ModuleResponse:
        """Executa `handle` e garante que a resposta e um `ModuleResponse`."""
        response = await module.handle(request, context)
        if not isinstance(response, ModuleResponse):
            raise ModuleError(
                f"O modulo '{type(module).slug}' devolveu {type(response).__name__} "
                f"em vez de ModuleResponse.",
                details={"module": type(module).slug, "received": type(response).__name__},
            )
        return response

    async def _ensure_runtime(
        self,
        response: ModuleResponse,
        *,
        module: BaseModule,
        pipeline: ModulePipeline,
        request: ModuleRequest,
        variables: Json,
    ) -> ModuleResponse:
        """Executa o runtime quando um modulo agente nao produziu nada (etapa 8).

        Vale apenas para `kind == AGENT` que devolveu resposta vazia sem usar a
        fachada: a plataforma nao deixa um agente sem a etapa 8. Modulos que
        respondem com dados estruturados (`data`) nunca passam por aqui.
        """
        if pipeline.definition.kind is not ModuleKind.AGENT:
            return response
        if pipeline.used_llm or response.output or response.data:
            return response
        _logger.info(
            "module_default_runtime",
            module=type(module).slug,
            runtime=pipeline.definition.runtime,
        )
        result = await pipeline.orchestrate(request.input, variables=variables)
        return response.model_copy(
            update={
                "output": result.output_text,
                "metadata": {**response.metadata, **result.metadata},
            }
        )

    # -- custo e trilha ----------------------------------------------------
    def _bill(
        self, run: AgentRun, definition: ModuleDefinition, *, default_model: str
    ) -> list[UsageRecord]:
        """Gera um `UsageRecord` por chamada de LLM e soma consumo e custo no run."""
        calculator = self._container.cost_calculator
        records: list[UsageRecord] = []
        total = TokenUsage()
        cost_total = 0.0
        for step in run.steps:
            if step.kind is not StepKind.LLM:
                continue
            model = _step_model(step, default_model)
            usage = step.usage
            if usage.total_tokens == 0:
                usage = calculator.estimate_usage(
                    _text_of(step.input, "prompt", "content"),
                    _text_of(step.output, "content", "output"),
                )
                step.usage = usage
                step.output = {**step.output, "estimated": True}
            cost = calculator.cost(model, usage)
            step.cost_usd = cost
            total = total + usage
            cost_total += cost
            records.append(
                UsageRecord(
                    run_id=run.id,
                    module_slug=definition.slug,
                    model=model,
                    usage=usage,
                    cost_usd=cost,
                    tenant_id=run.tenant_id,
                )
            )
        run.usage = total
        run.cost_usd = round(cost_total, COST_DIGITS)
        desconhecidos = sorted({r.model for r in records if not calculator.is_known(r.model)})
        if desconhecidos:
            # O aviso pertence a AQUI, onde a lacuna nasce — uma vez por invocacao —
            # e nao ao resumo de custo, que e lido a cada render de pagina pela barra
            # de status e repetiria a mesma linha para sempre (SPEC-0005 secao 2).
            _logger.warning(
                "module_usage_unknown_model_price",
                run_id=run.id,
                module=definition.slug,
                models=desconhecidos,
                reason="modelo sem preco cadastrado: custo apurado com o preco default",
            )
        return records

    def _append_step(
        self,
        run: AgentRun,
        *,
        kind: StepKind,
        name: str,
        inputs: Json | None = None,
        outputs: Json | None = None,
        usage: TokenUsage | None = None,
        latency_ms: float = 0.0,
        status: RunStatus = RunStatus.SUCCEEDED,
        error: str | None = None,
        started_at: datetime | None = None,
    ) -> RunStep:
        """Cria e anexa um `RunStep` na proxima posicao da trilha."""
        finished = utcnow()
        step = RunStep(
            run_id=run.id,
            index=len(run.steps),
            kind=kind,
            name=name,
            status=status,
            input=inputs or {},
            output=outputs or {},
            usage=usage or TokenUsage(),
            latency_ms=round(latency_ms, 3),
            error=error,
            started_at=started_at or finished,
            finished_at=finished,
        )
        run.steps.append(step)
        return step

    def _adopt(self, run: AgentRun, step: RunStep) -> RunStep:
        """Adota um step produzido pelo runtime, renumerando indice e execucao."""
        adopted = step.model_copy(update={"run_id": run.id, "index": len(run.steps)})
        run.steps.append(adopted)
        return adopted

    # -- persistencia ------------------------------------------------------
    async def _open_run(self, run: AgentRun) -> None:
        """Persiste o `AgentRun(RUNNING)` e commita: nenhuma execucao e invisivel."""
        async with self._container.uow_factory() as uow:
            await uow.runs.add(run)
            await uow.commit()

    async def _persist(self, run: AgentRun, *, records: Sequence[UsageRecord] = ()) -> None:
        """Grava consumo e estado final do run numa unica transacao."""
        async with self._container.uow_factory() as uow:
            for record in records:
                await uow.usage.add(record)
            await uow.runs.update(run)
            await uow.commit()

    async def _close_blocked(
        self, run: AgentRun, error: GuardrailViolation, *, started: float
    ) -> None:
        """Fecha o run como `BLOCKED` e persiste a trilha antes de propagar (etapa 6/9)."""
        run.status = RunStatus.BLOCKED
        run.error = error.message
        run.output = {"blocked": True, "stage": error.stage, "policy_id": error.policy_id}
        run.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        run.finished_at = utcnow()
        run.touch()
        _logger.warning(
            "module_blocked",
            module=run.module_slug,
            run_id=run.id,
            stage=error.stage,
            policy_id=error.policy_id,
            rule_id=error.rule_id,
        )
        await self._safe_persist(run)

    async def _close_failed(self, run: AgentRun, error: LukatoError, *, started: float) -> None:
        """Fecha o run como `FAILED` com a mensagem, antes de propagar a excecao."""
        run.status = RunStatus.FAILED
        run.error = error.message
        run.latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        run.finished_at = utcnow()
        self._append_step(
            run,
            kind=StepKind.ERROR,
            name="error",
            status=RunStatus.FAILED,
            outputs={"code": error.code, "details": error.details},
            error=error.message,
        )
        run.touch()
        _logger.error(
            "module_failed",
            module=run.module_slug,
            run_id=run.id,
            code=error.code,
            error=error.message,
        )
        await self._safe_persist(run)

    async def _safe_persist(self, run: AgentRun) -> None:
        """Persiste o run em caminho de erro sem mascarar a excecao original."""
        try:
            await self._persist(run)
        except Exception as exc:
            _logger.error(
                "run_persist_failed",
                run_id=run.id,
                status=run.status.value,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _emit_scores(self, run: AgentRun) -> None:
        """Registra os scores automaticos do trace (SPEC-0008 secao 2)."""
        tracer = self._container.tracer
        scores = (
            ("guardrail_blocked", 1.0 if run.status is RunStatus.BLOCKED else 0.0),
            ("latency_ms", run.latency_ms),
            ("cost_usd", run.cost_usd),
        )
        for name, value in scores:
            try:
                await tracer.score(name=name, value=value, trace_id=run.trace_id)
            except Exception as exc:  # pragma: no cover - telemetria nunca derruba
                _logger.warning(
                    "trace_score_failed", score=name, error=f"{type(exc).__name__}: {exc}"
                )
                break
