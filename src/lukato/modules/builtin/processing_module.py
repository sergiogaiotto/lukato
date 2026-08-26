"""Building block `processing`: o agente generico de referencia (SPEC-0001 secao 5).

Este e o modulo que prova o requisito central da plataforma: **ele nao contem
regra de negocio**. Nenhuma linha aqui decide o que o agente responde. Tudo o
que define o comportamento chega de fora, na `ModuleDefinition`:

```text
binding.input_guardrail_id  ->  o que e recusado na entrada
binding.system_prompt_id    ->  quem o agente e e como deve responder
binding.output_guardrail_id ->  o que e recusado na saida
binding.model/temperature/max_tokens/tools/timeout_seconds
definition.runtime          ->  direct | langgraph | deepagent
definition.config           ->  max_iterations, planning, subagents, response_schema
```

Criar um agente novo e criar uma linha na tabela `modules` — nao e escrever
codigo. Duas definicoes sobre esta mesma classe (`triagem`, `resumo`) produzem
comportamentos diferentes sem alterar este arquivo.

A trinca NAO e aplicada aqui
----------------------------
Este e o ponto central da arquitetura hexagonal do lukato, e vale repetir sem
economia de palavras: `handle` **nao** aplica guardrail de entrada, **nao**
renderiza o system prompt e **nao** aplica guardrail de saida. Quem faz isso e
o caso de uso `InvokeModule`, que cerca o `handle` de todo e qualquer building
block com a mesma trinca:

```text
InvokeModule
  guardrail de entrada  ->  module.handle(request, ctx)  ->  guardrail de saida
```

Consequencia pratica: um modulo novo, escrito por terceiros, herda a trinca de
graca e **nao tem como** escapar dela — nao existe caminho de execucao de
building block fora do `InvokeModule`. Se a checagem morasse dentro do `handle`,
cada modulo poderia esquece-la, e a garantia da SPEC-0003 secao 1 ("nenhum
modulo chama um LLM fora da trinca") deixaria de ser verificavel.

O que `handle` faz, entao, e apenas plumbing: monta o `OrchestratorRequest` com
o system prompt **ja renderizado** pela plataforma (`ctx.services["pipeline"]`),
escolhe o orquestrador do runtime declarado, executa e devolve texto, consumo e
trilha.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, ClassVar, Final

from lukato.application.container import DEFAULT_RUNTIME, KNOWN_RUNTIMES
from lukato.application.use_cases.modules import ModulePipeline
from lukato.config import get_logger
from lukato.domain.errors import ProviderError, UnsupportedCapability, ValidationError
from lukato.domain.models.module import ModuleDefinition, ModuleKind
from lukato.domain.models.run import RunStep
from lukato.domain.ports.orchestrator import (
    OrchestratorPort,
    OrchestratorRequest,
    OrchestratorResult,
)
from lukato.domain.types import Json
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import register_module

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "MAX_ITERATIONS_CEILING",
    "MAX_SUBAGENTS",
    "PIPELINE_SERVICE",
    "ProcessingModule",
]

_logger = get_logger(__name__)

PIPELINE_SERVICE: Final[str] = "pipeline"
"""Chave de `ctx.services` com a fachada da trinca montada pelo `InvokeModule`."""

DEFAULT_MAX_ITERATIONS: Final[int] = 6
"""Teto padrao de iteracoes do grafo LangGraph (SPEC-0004 secao 2)."""

MAX_ITERATIONS_CEILING: Final[int] = 32
"""Teto absoluto de iteracoes aceito pelo runtime `langgraph`."""

MAX_SUBAGENTS: Final[int] = 12
"""Teto de sub-agentes aceito pelo Deep-Agent Harness (SPEC-0004 secao 3)."""

_SUBAGENT_SCHEMA: Final[Json] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string", "minLength": 1},
        "prompt": {"type": "string", "minLength": 1},
        "tools": {"type": "array", "items": {"type": "string"}, "default": []},
    },
    "required": ["name", "description", "prompt"],
}
"""Formato de um sub-agente em `config['subagents']`, conforme o harness espera."""


def _step_summary(step: RunStep) -> Json:
    """Resumo de um passo para `metadata['steps']`.

    A trilha completa (entradas e saidas de cada passo) ja e persistida no
    `AgentRun`; a resposta carrega apenas o suficiente para a UI desenhar a
    execucao sem repetir o conteudo inteiro.
    """
    return {
        "index": step.index,
        "kind": step.kind.value,
        "name": step.name,
        "status": step.status.value,
        "latency_ms": step.latency_ms,
        "total_tokens": step.usage.total_tokens,
        "error": step.error,
    }


def _accepts(orchestrator: OrchestratorPort, runtime: str) -> bool:
    """True quando o orquestrador declara suportar o runtime, sem propagar erro."""
    try:
        return bool(orchestrator.supports(runtime))
    except Exception as exc:  # pragma: no cover - adaptador defeituoso
        _logger.warning(
            "orchestrator_supports_failed",
            orchestrator=getattr(orchestrator, "name", type(orchestrator).__name__),
            runtime=runtime,
            error=f"{type(exc).__name__}: {exc}",
        )
        return False


def _resolve_orchestrator(ctx: ModuleContext, runtime: str) -> OrchestratorPort:
    """Escolhe o orquestrador de `ctx.orchestrators` para o runtime declarado.

    Ordem: chave exata que aceite o runtime, depois qualquer orquestrador cujo
    `supports()` responda `True`. Um runtime **conhecido** cujo adaptador esta
    indisponivel nesta instalacao (tipicamente `deepagent` sem `deepagents`
    instalado) degrada para :data:`DEFAULT_RUNTIME` com WARNING, para que o
    endpoint continue respondendo (SPEC-0004 secao 5, criterio 3); um runtime
    desconhecido levanta :class:`UnsupportedCapability`.
    """
    requested = (runtime or "").strip().lower() or DEFAULT_RUNTIME

    exact = ctx.orchestrators.get(requested)
    if exact is not None and _accepts(exact, requested):
        return exact
    for orchestrator in ctx.orchestrators.values():
        if _accepts(orchestrator, requested):
            return orchestrator

    if requested in KNOWN_RUNTIMES or requested in ctx.orchestrators:
        fallback = ctx.orchestrators.get(DEFAULT_RUNTIME)
        if fallback is not None:
            _logger.warning(
                "processing_runtime_fallback",
                module=ctx.definition.slug,
                requested=requested,
                fallback=DEFAULT_RUNTIME,
                reason="runtime declarado indisponivel nesta instalacao",
            )
            return fallback

    raise UnsupportedCapability(
        f"Runtime '{requested}' nao tem orquestrador disponivel nesta instalacao.",
        details={
            "module_slug": ctx.definition.slug,
            "runtime": requested,
            "available": sorted(ctx.orchestrators),
        },
    )


@register_module
class ProcessingModule(BaseModule):
    """Agente generico configuravel: o building block de referencia.

    **A trinca de guardrails nao e aplicada aqui.** `InvokeModule` cerca o
    `handle` de qualquer building block com guardrail de entrada, system prompt
    renderizado e guardrail de saida, nesta ordem exata (SPEC-0001 secao 4).
    Este modulo recebe o prompt pronto em `ctx.services["pipeline"].system_prompt`
    e devolve texto puro; a checagem de saida acontece depois que ele retorna.
    E isso que torna a garantia da plataforma verificavel: nenhum modulo, nem
    este, tem como pular uma etapa da trinca.

    O comportamento vem inteiramente da `ModuleDefinition`: binding (guardrails,
    prompt, modelo, ferramentas), `runtime` (qual orquestrador executa) e
    `config` (`max_iterations`, `planning`, `subagents`, `response_schema`).
    """

    kind: ClassVar[ModuleKind] = ModuleKind.AGENT
    slug: ClassVar[str] = "processing"
    name: ClassVar[str] = "Processamento"
    description: ClassVar[str] = (
        "Agente generico guardrail-in -> system prompt -> LLM -> guardrail-out. "
        "Todo o comportamento vem do binding e da configuracao da definicao."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("chat", "structured_output", "tools", "streaming")
    config_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "max_iterations": {
                "type": "integer",
                "default": DEFAULT_MAX_ITERATIONS,
                "minimum": 1,
                "maximum": MAX_ITERATIONS_CEILING,
                "description": "Teto de ciclos plano/acao/observacao do runtime `langgraph`.",
            },
            "planning": {
                "type": "boolean",
                "default": True,
                "description": "Liga a etapa de planejamento antes da primeira acao.",
            },
            "subagents": {
                "type": "array",
                "default": [],
                "items": _SUBAGENT_SCHEMA,
                "description": (
                    "Sub-agentes do Deep-Agent Harness; ignorado pelos demais runtimes."
                ),
            },
            "response_schema": {
                "type": ["object", "null"],
                "default": None,
                "description": (
                    "JSON Schema da resposta esperada. Presente, o modulo tenta ler a "
                    "saida como JSON e devolve o objeto em `data` (structured_output)."
                ),
            },
        },
    }

    async def setup(self, ctx: ModuleContext) -> None:
        """Valida a configuracao da definicao antes do primeiro `handle`."""
        config = self._config(ctx)
        _logger.info(
            "processing_module_ready",
            module=ctx.definition.slug,
            runtime=ctx.definition.runtime,
            max_iterations=config["max_iterations"],
            planning=config["planning"],
            subagents=len(config["subagents"]),
            structured_output=config["response_schema"] is not None,
        )

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Executa o runtime declarado e devolve texto, consumo e trilha.

        Sem guardrails e sem renderizacao de prompt: as duas coisas pertencem ao
        `InvokeModule`, que envolve esta chamada.
        """
        text = (request.input or "").strip()
        if not text:
            raise ValidationError(
                f"O modulo '{self.slug}' precisa de um texto em `input` para executar.",
                details={"module_slug": ctx.definition.slug, "field": "input"},
            )

        config = self._config(ctx)
        definition = self._definition_with(ctx, config)
        pipeline = self._pipeline(ctx)
        orchestrator = _resolve_orchestrator(ctx, definition.runtime)

        orchestrator_request = OrchestratorRequest(
            module=definition,
            input_text=text,
            variables=dict(request.variables),
            history=list(request.history),
            tools=list(definition.binding.tools),
            system_prompt=pipeline.system_prompt if pipeline is not None else "",
            metadata=self._request_metadata(ctx, config, pipeline, stream=request.stream),
        )
        result = await self._run(orchestrator, orchestrator_request, ctx)
        self._adopt_steps(result, pipeline)

        return ModuleResponse(
            output=result.output_text,
            data=self._structured(result.output_text, config),
            usage=result.usage,
            metadata={
                "runtime": definition.runtime,
                "orchestrator": getattr(orchestrator, "name", type(orchestrator).__name__),
                "model": definition.binding.model or ctx.llm.default_model,
                "system_prompt_applied": bool(orchestrator_request.system_prompt),
                "tools": list(orchestrator_request.tools),
                "stream_requested": request.stream,
                "steps": [_step_summary(step) for step in result.steps],
                **result.metadata,
            },
        )

    def ui(self) -> UIDescriptor:
        """Publica os itens do agente generico na secao FUNCIONALIDADE."""
        return UIDescriptor(
            nav=[
                # `/modules/{slug}` recebe o slug de uma INSTANCIA de modulo, e
                # `processing` e o nome da implementacao, nao de uma instancia:
                # as instancias que rodam este codigo nascem com nome de dominio
                # (`assistente`, `triagem`). O item apontava para
                # `/modules/processing` e dava 404 em toda pagina do console —
                # e o unico link quebrado do menu. `?kind=agent` lista
                # exatamente as instancias deste agente generico, que e o que o
                # item sempre quis mostrar.
                #
                # O segundo item, "Execucoes do agente" -> `/runs?module=processing`,
                # saiu pelo mesmo motivo: `module` filtra por slug de instancia,
                # entao a lista chegava sempre vazia. Um filtro por implementacao
                # nao existe hoje, e "Execucoes" no nivel da plataforma ja leva a
                # `/runs` — o item era, na melhor das hipoteses, uma duplicata.
                UINavItem(
                    label="Processamento",
                    icon="blocks",
                    endpoint="/modules?kind=agent",
                    section="FUNCIONALIDADE",
                    order=20,
                ),
            ],
            center_template="pages/modules_detail.html",
            context_template="context/module.html",
        )

    def health(self) -> Json:
        """Resumo de saude do modulo, acrescido dos runtimes que ele sabe pedir."""
        report = super().health()
        report["runtimes"] = sorted(KNOWN_RUNTIMES)
        return report

    # -- apoio -------------------------------------------------------------
    def _config(self, ctx: ModuleContext) -> Json:
        """Configuracao da definicao normalizada pelo `config_schema` da classe."""
        config = ctx.definition.config
        return self.validate_config(dict(config) if isinstance(config, Mapping) else {})

    @staticmethod
    def _definition_with(ctx: ModuleContext, config: Json) -> ModuleDefinition:
        """Definicao com a configuracao ja normalizada (defaults do schema preenchidos).

        Os orquestradores leem `request.module.config`; entregar a versao
        normalizada evita que cada runtime reimplemente o mesmo `default`.
        """
        if config == ctx.definition.config:
            return ctx.definition
        return ctx.definition.model_copy(update={"config": config})

    @staticmethod
    def _pipeline(ctx: ModuleContext) -> ModulePipeline | None:
        """Fachada da trinca publicada pelo `InvokeModule`, quando presente.

        Ausente apenas em contextos montados a mao (testes, ferramentas): nesse
        caso nao ha system prompt renderizado nem trilha de run para alimentar,
        e o modulo executa o runtime do mesmo jeito.
        """
        found = ctx.services.get(PIPELINE_SERVICE)
        return found if isinstance(found, ModulePipeline) else None

    @staticmethod
    def _request_metadata(
        ctx: ModuleContext, config: Json, pipeline: ModulePipeline | None, *, stream: bool
    ) -> Json:
        """Metadados de correlacao entregues ao runtime."""
        metadata: Json = {
            "module_slug": ctx.definition.slug,
            "runtime": ctx.definition.runtime,
            "tenant_id": pipeline.tenant_id if pipeline is not None else ctx.principal.tenant_id,
            "actor": ctx.principal.subject,
            "stream": stream,
        }
        if pipeline is not None:
            metadata["run_id"] = pipeline.run_id
        if config["response_schema"] is not None:
            metadata["response_schema"] = config["response_schema"]
        return metadata

    @staticmethod
    async def _run(
        orchestrator: OrchestratorPort, request: OrchestratorRequest, ctx: ModuleContext
    ) -> OrchestratorResult:
        """Executa o runtime convertendo falha de adaptador em `ProviderError`."""
        try:
            return await orchestrator.run(request)
        except (ProviderError, UnsupportedCapability, ValidationError):
            raise
        except Exception as exc:
            raise ProviderError(
                f"O runtime '{request.module.runtime}' falhou ao executar o modulo "
                f"'{ctx.definition.slug}': {type(exc).__name__}: {exc}",
                details={
                    "module_slug": ctx.definition.slug,
                    "runtime": request.module.runtime,
                    "error": type(exc).__name__,
                },
            ) from exc

    @staticmethod
    def _adopt_steps(result: OrchestratorResult, pipeline: ModulePipeline | None) -> None:
        """Entrega os passos do runtime a trilha do `AgentRun`.

        Sem isso o run ficaria sem passos `LLM` e a etapa 10 do `InvokeModule`
        (`UsageRecord` + custo) nao teria o que cobrar: a contabilidade da
        plataforma le exatamente esta trilha.
        """
        if pipeline is None:
            return
        for step in result.steps:
            pipeline.record(step)

    @staticmethod
    def _structured(output: str, config: Json) -> Json:
        """Le a saida como JSON quando a definicao declara `response_schema`.

        Sem `response_schema` a capacidade `structured_output` fica desligada e
        `data` volta vazio. Com ela, saida que nao seja um objeto JSON nao vira
        erro aqui: quem recusa formato invalido e a regra `json_schema` do
        guardrail de saida, que roda depois deste retorno (SPEC-0003 secao 3).
        """
        if config["response_schema"] is None:
            return {}
        try:
            parsed: Any = json.loads(output)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
