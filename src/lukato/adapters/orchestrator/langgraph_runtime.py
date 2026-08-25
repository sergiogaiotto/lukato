"""Orquestrador `langgraph`: grafo de estados plano -> acao -> observacao -> reflexao.

Implementa literalmente o fluxo da SPEC-0004 secao 2:

```text
START -> prepare -> plan -> act <-> observe -> reflect -> finalize -> END
```

Decisoes de projeto que a SPEC exige e que este modulo honra:

* o grafo fala com o **`LLMPort`**, nunca com um cliente LangChain — o hexagono
  continua intacto e o runtime roda offline com o `EchoLLM`;
* `max_iterations` (padrao 6) impede laco infinito; ao estourar, `finalize` devolve
  o melhor resultado parcial e registra um step `REFLECT` com a razao;
* erro de ferramenta vira observacao com step `ERROR`, jamais excecao que aborta;
* o grafo e compilado **uma vez por instancia** e reaproveitado em toda requisicao;
  o que muda a cada chamada viaja no `context` tipado do LangGraph, nunca em
  atributo mutavel da instancia (duas execucoes concorrentes nao se misturam).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Final, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from lukato.adapters.orchestrator.direct import (
    build_messages,
    call_llm,
    clip_text,
    new_step,
    run_id_of,
)
from lukato.adapters.orchestrator.tools import ToolContext, ToolRegistry
from lukato.config import Settings, get_logger
from lukato.domain.errors import LukatoError
from lukato.domain.models.module import ModuleBinding
from lukato.domain.models.run import RunStatus, RunStep, StepKind, TokenUsage
from lukato.domain.ports.llm import ChatMessage, LLMPort
from lukato.domain.ports.orchestrator import OrchestratorRequest, OrchestratorResult
from lukato.domain.types import DEFAULT_TENANT, Id, Json

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "EXHAUSTED_STEP_NAME",
    "PLAN_MAX_TOKENS",
    "GraphContext",
    "GraphState",
    "LangGraphOrchestrator",
    "parse_tool_call",
]

_logger = get_logger(__name__)

DEFAULT_MAX_ITERATIONS: Final[int] = 6
"""Teto padrao de voltas no ciclo `act -> observe` (SPEC-0004 secao 2)."""

MAX_ITERATIONS_CEILING: Final[int] = 32
"""Teto absoluto: nem a configuracao do modulo pode pedir mais do que isso."""

PLAN_MAX_TOKENS: Final[int] = 384
"""Teto de tokens do plano: e um roteiro curto, nao a resposta."""

REFLECT_MAX_TOKENS: Final[int] = 1024
"""Teto de tokens da revisao final quando `config['reflection']` esta ligado."""

EXHAUSTED_STEP_NAME: Final[str] = "finalize.exhausted"
"""Nome do step `REFLECT` gravado quando o ciclo estoura `max_iterations`."""

_FENCE = re.compile(r"^```[A-Za-z0-9_+\-]*\s*|\s*```$")
"""Cercas de markdown que os modelos insistem em colocar em volta do JSON."""

_PLAN_INSTRUCTION: Final[str] = (
    "Antes de responder, escreva um plano curto de no maximo 5 passos objetivos "
    "para atender o pedido acima. Escreva apenas o plano, em topicos, sem executa-lo."
)

_TOOL_INSTRUCTION_HEADER: Final[str] = (
    "Voce pode usar ferramentas. Para chamar UMA ferramenta, responda "
    'EXCLUSIVAMENTE com um objeto JSON no formato {"tool": "<nome>", "args": {...}}, '
    "sem texto antes ou depois e sem cercas de codigo. Para responder ao usuario, "
    "escreva a resposta final em texto normal, sem JSON. Ferramentas disponiveis:"
)

_OBSERVATION_INSTRUCTION: Final[str] = (
    "Resultado das ferramentas que voce pediu. Use estes dados para responder ao "
    "pedido original; chame outra ferramenta apenas se ainda faltar informacao."
)


class GraphState(TypedDict, total=False):
    """Estado do grafo (SPEC-0004 secao 2). Todas as chaves sao opcionais."""

    messages: list[ChatMessage]
    plan: str
    scratchpad: list[str]
    tool_calls: list[Json]
    observations: list[Json]
    iterations: int
    output: str
    steps: list[RunStep]
    usage: TokenUsage


@dataclass(slots=True, frozen=True)
class GraphContext:
    """Dados imutaveis de UMA requisicao, entregues aos nos pelo `Runtime` do LangGraph."""

    request: OrchestratorRequest
    run_id: Id
    binding: ModuleBinding
    tool_names: tuple[str, ...]
    max_iterations: int
    planning: bool
    reflection: bool
    tool_context: ToolContext


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Le um inteiro de configuracao livre, sempre dentro da faixa util."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Le um booleano de configuracao livre, aceitando as grafias textuais usuais."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "sim", "on"}:
            return True
        if lowered in {"false", "0", "no", "nao", "off"}:
            return False
    return default


def _balanced_object(text: str) -> str | None:
    """Extrai o primeiro objeto JSON balanceado do texto, ignorando chaves em strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : position + 1]
    return None


def parse_tool_call(content: str) -> Json | None:
    """Le `{"tool": nome, "args": {...}}` de uma resposta do LLM, com tolerancia.

    Cercas de markdown, texto em volta e `args` ausente sao tolerados. Qualquer
    coisa que nao seja um objeto com `tool` textual e tratada como resposta final —
    JSON invalido nunca vira erro, vira resposta.
    """
    payload = content.strip()
    if not payload:
        return None
    if payload.startswith("```"):
        payload = _FENCE.sub("", payload).strip()
    candidate = payload if payload.startswith("{") else _balanced_object(payload)
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    name = parsed.get("tool") or parsed.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    raw_args = parsed.get("args")
    if not isinstance(raw_args, dict):
        raw_args = parsed.get("arguments")
    args = raw_args if isinstance(raw_args, dict) else {}
    return {"tool": name.strip(), "args": args}


def _error_text(exc: BaseException) -> str:
    """Mensagem curta e estavel de um erro de ferramenta, boa para prompt e para log."""
    if isinstance(exc, LukatoError):
        return f"{exc.code}: {exc}"
    return f"{type(exc).__name__}: {exc}"


class LangGraphOrchestrator:
    """Runtime `langgraph`: grafo de estados com plano, acao, observacao e reflexao."""

    name: str = "langgraph"

    def __init__(
        self,
        llm: LLMPort,
        *,
        tools: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools if tools is not None else ToolRegistry()
        self._tool_context = tool_context if tool_context is not None else ToolContext()
        self._settings = settings
        self._graph: CompiledStateGraph[GraphState, GraphContext, GraphState, GraphState] | None
        self._graph = None

    # ------------------------------------------------------------------ #
    # Porta
    # ------------------------------------------------------------------ #

    def supports(self, runtime: str) -> bool:
        """True apenas para o runtime `langgraph`."""
        return (runtime or "").strip().lower() == self.name

    async def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        """Executa o grafo compilado e devolve texto, trilha de steps e consumo."""
        context = self._build_context(request)
        graph = self._compiled()
        initial: GraphState = {
            "messages": [],
            "plan": "",
            "scratchpad": [],
            "tool_calls": [],
            "observations": [],
            "iterations": 0,
            "output": "",
            "steps": [],
            "usage": TokenUsage(),
        }
        # `ainvoke` e tipado como dict[str, Any] pelos stubs do LangGraph; o grafo
        # foi construido com GraphState, entao o formato de retorno e conhecido.
        final: GraphState = cast(
            "GraphState",
            await graph.ainvoke(
                initial,
                context=context,
                config={"recursion_limit": 8 + 4 * context.max_iterations},
            ),
        )
        steps = list(final.get("steps", []))
        usage = final.get("usage") or TokenUsage()
        exhausted = any(step.name == EXHAUSTED_STEP_NAME for step in steps)
        _logger.info(
            "orchestrator_langgraph_completed",
            module=request.module.slug,
            run_id=context.run_id,
            iterations=final.get("iterations", 0),
            steps=len(steps),
            tools=list(context.tool_names),
            observations=len(final.get("observations", [])),
            exhausted=exhausted,
            total_tokens=usage.total_tokens,
        )
        return OrchestratorResult(
            output_text=final.get("output", ""),
            steps=steps,
            usage=usage,
            metadata={
                "runtime": self.name,
                "iterations": final.get("iterations", 0),
                "max_iterations": context.max_iterations,
                "plan": final.get("plan", ""),
                "tools": list(context.tool_names),
                "observations": len(final.get("observations", [])),
                "exhausted": exhausted,
            },
        )

    # ------------------------------------------------------------------ #
    # Montagem do grafo (uma vez por instancia)
    # ------------------------------------------------------------------ #

    def _compiled(self) -> CompiledStateGraph[GraphState, GraphContext, GraphState, GraphState]:
        """Compila o grafo na primeira execucao e reaproveita o resultado."""
        if self._graph is None:
            self._graph = self._build_graph().compile()
            _logger.debug("langgraph_graph_compiled", orchestrator=self.name)
        return self._graph

    def _build_graph(self) -> StateGraph[GraphState, GraphContext, GraphState, GraphState]:
        """Declara nos e arestas exatamente como a SPEC-0004 secao 2 descreve."""
        graph: StateGraph[GraphState, GraphContext, GraphState, GraphState] = StateGraph(
            GraphState, context_schema=GraphContext
        )
        graph.add_node("prepare", self._prepare)
        graph.add_node("plan", self._plan)
        graph.add_node("act", self._act)
        graph.add_node("observe", self._observe)
        graph.add_node("reflect", self._reflect)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "prepare")
        graph.add_conditional_edges(
            "prepare", self._route_after_prepare, {"plan": "plan", "act": "act"}
        )
        graph.add_edge("plan", "act")
        graph.add_conditional_edges(
            "act", self._route_after_act, {"observe": "observe", "reflect": "reflect"}
        )
        graph.add_conditional_edges(
            "observe", self._route_after_observe, {"act": "act", "reflect": "reflect"}
        )
        graph.add_edge("reflect", "finalize")
        graph.add_edge("finalize", END)
        return graph

    def _build_context(self, request: OrchestratorRequest) -> GraphContext:
        """Resolve configuracao, ferramentas e dependencias desta requisicao."""
        config = request.module.config
        names = self._tool_names(request)
        self._tools.resolve(names)  # ferramenta inexistente -> ValidationError, antes de rodar
        tenant = request.metadata.get("tenant_id")
        tool_context = replace(
            self._tool_context,
            module_slug=request.module.slug,
            tenant_id=tenant if isinstance(tenant, str) and tenant else DEFAULT_TENANT,
        )
        return GraphContext(
            request=request,
            run_id=run_id_of(request),
            binding=request.module.binding,
            tool_names=tuple(names),
            max_iterations=_coerce_int(
                config.get("max_iterations"),
                default=DEFAULT_MAX_ITERATIONS,
                minimum=1,
                maximum=MAX_ITERATIONS_CEILING,
            ),
            planning=_coerce_bool(config.get("planning"), default=bool(names)),
            reflection=_coerce_bool(config.get("reflection"), default=False),
            tool_context=tool_context,
        )

    @staticmethod
    def _tool_names(request: OrchestratorRequest) -> list[str]:
        """Une `binding.tools` e `request.tools` preservando a ordem e sem repetir."""
        ordered: list[str] = []
        for name in [*request.module.binding.tools, *request.tools]:
            cleaned = (name or "").strip()
            if cleaned and cleaned not in ordered:
                ordered.append(cleaned)
        return ordered

    # ------------------------------------------------------------------ #
    # Roteamento
    # ------------------------------------------------------------------ #

    @staticmethod
    def _route_after_prepare(state: GraphState, runtime: Runtime[GraphContext]) -> str:
        """Vai para `plan` quando o planejamento esta ligado; senao age direto."""
        return "plan" if runtime.context.planning else "act"

    @staticmethod
    def _route_after_act(state: GraphState, runtime: Runtime[GraphContext]) -> str:
        """Ha chamada de ferramenta pendente? entao observa; senao reflete."""
        return "observe" if state.get("tool_calls") else "reflect"

    @staticmethod
    def _route_after_observe(state: GraphState, runtime: Runtime[GraphContext]) -> str:
        """Volta a agir enquanto houver orcamento de iteracoes; no estouro, reflete."""
        if state.get("iterations", 0) >= runtime.context.max_iterations:
            return "reflect"
        return "act"

    # ------------------------------------------------------------------ #
    # Nos
    # ------------------------------------------------------------------ #

    async def _prepare(self, state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
        """Monta `[system?] + history + user` e abre a trilha de execucao."""
        started = time.perf_counter()
        context = runtime.context
        messages = build_messages(context.request)
        step = new_step(
            run_id=context.run_id,
            index=len(state.get("steps", [])),
            kind=StepKind.PROMPT,
            name="prepare",
            inputs={
                "module": context.request.module.slug,
                "history": len(context.request.history),
                "tools": list(context.tool_names),
                "max_iterations": context.max_iterations,
                "planning": context.planning,
            },
            outputs={"messages": len(messages), "input": clip_text(context.request.input_text)},
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
        return {"messages": messages, "steps": [*state.get("steps", []), step]}

    async def _plan(self, state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
        """Pede ao LLM um plano curto e o guarda no scratchpad."""
        context = runtime.context
        messages = [*state.get("messages", []), ChatMessage.user(_PLAN_INSTRUCTION)]
        binding = context.binding.model_copy(
            update={
                "max_tokens": min(context.binding.max_tokens or PLAN_MAX_TOKENS, PLAN_MAX_TOKENS)
            }
        )
        response, latency_ms = await call_llm(
            self._llm,
            messages,
            binding=binding,
            metadata={"node": "plan", "run_id": context.run_id},
        )
        plan = response.content.strip()
        step = new_step(
            run_id=context.run_id,
            index=len(state.get("steps", [])),
            kind=StepKind.PLAN,
            name="plan",
            inputs={"messages": len(messages)},
            outputs={"plan": clip_text(plan)},
            usage=response.usage,
            latency_ms=latency_ms,
        )
        return {
            "plan": plan,
            "scratchpad": [*state.get("scratchpad", []), f"Plano:\n{plan}"],
            "steps": [*state.get("steps", []), step],
            "usage": (state.get("usage") or TokenUsage()) + response.usage,
        }

    async def _act(self, state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
        """Chama o LLM pedindo a resposta final ou UMA chamada de ferramenta em JSON."""
        context = runtime.context
        iterations = state.get("iterations", 0) + 1
        messages = self._act_messages(state, context)
        response, latency_ms = await call_llm(
            self._llm,
            messages,
            binding=context.binding,
            metadata={"node": "act", "run_id": context.run_id, "iteration": iterations},
        )
        call = parse_tool_call(response.content) if context.tool_names else None
        step = new_step(
            run_id=context.run_id,
            index=len(state.get("steps", [])),
            kind=StepKind.LLM,
            name=f"act#{iterations}",
            inputs={"messages": len(messages), "iteration": iterations},
            outputs={
                "content": clip_text(response.content),
                "finish_reason": response.finish_reason,
                "tool_call": call,
            },
            usage=response.usage,
            latency_ms=latency_ms,
        )
        return {
            "iterations": iterations,
            "tool_calls": [call] if call else [],
            "output": "" if call else response.content,
            "steps": [*state.get("steps", []), step],
            "usage": (state.get("usage") or TokenUsage()) + response.usage,
        }

    async def _observe(self, state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
        """Executa as ferramentas pedidas; qualquer falha vira observacao com step `ERROR`."""
        context = runtime.context
        steps = list(state.get("steps", []))
        observations = list(state.get("observations", []))
        scratchpad = list(state.get("scratchpad", []))
        for call in state.get("tool_calls", []):
            name = str(call.get("tool", ""))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            started = time.perf_counter()
            try:
                result = await self._tools.execute(name, args, context.tool_context)
            except Exception as exc:
                message = _error_text(exc)
                observation: Json = {"tool": name, "args": args, "error": message}
                steps.append(
                    new_step(
                        run_id=context.run_id,
                        index=len(steps),
                        kind=StepKind.ERROR,
                        name=f"tool:{name or 'desconhecida'}",
                        inputs={"tool": name, "args": args},
                        outputs={"error": message},
                        status=RunStatus.FAILED,
                        error=message,
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    )
                )
                _logger.warning(
                    "orchestrator_tool_failed",
                    run_id=context.run_id,
                    tool=name,
                    error=message,
                )
            else:
                observation = {"tool": name, "args": args, "result": result}
                steps.append(
                    new_step(
                        run_id=context.run_id,
                        index=len(steps),
                        kind=StepKind.TOOL,
                        name=f"tool:{name}",
                        inputs={"tool": name, "args": args},
                        outputs={"result": result},
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                    )
                )
            observations.append(observation)
            scratchpad.append(self._observation_line(observation))
        return {
            "tool_calls": [],
            "observations": observations,
            "scratchpad": scratchpad,
            "steps": steps,
        }

    async def _reflect(self, state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
        """Revisa o resultado antes de fechar; so gasta LLM quando `reflection` esta ligado."""
        context = runtime.context
        started = time.perf_counter()
        output = state.get("output", "")
        usage = state.get("usage") or TokenUsage()
        refined = output
        used_llm = False
        step_usage = TokenUsage()
        if output and context.reflection:
            binding = context.binding.model_copy(
                update={
                    "max_tokens": min(
                        context.binding.max_tokens or REFLECT_MAX_TOKENS, REFLECT_MAX_TOKENS
                    )
                }
            )
            messages = [
                *state.get("messages", []),
                ChatMessage.assistant(output),
                ChatMessage.user(
                    "Revise a resposta acima: corrija imprecisoes, remova repeticao e "
                    "devolva APENAS a versao final para o usuario."
                ),
            ]
            response, latency = await call_llm(
                self._llm,
                messages,
                binding=binding,
                metadata={"node": "reflect", "run_id": context.run_id},
            )
            used_llm = True
            step_usage = response.usage
            usage = usage + response.usage
            if response.content.strip():
                refined = response.content
            step_latency = latency
        else:
            step_latency = (time.perf_counter() - started) * 1000.0
        step = new_step(
            run_id=context.run_id,
            index=len(state.get("steps", [])),
            kind=StepKind.REFLECT,
            name="reflect",
            inputs={
                "iterations": state.get("iterations", 0),
                "observations": len(state.get("observations", [])),
                "reflection": context.reflection,
            },
            outputs={"answered": bool(output), "revised": used_llm and refined != output},
            usage=step_usage,
            latency_ms=step_latency,
        )
        return {
            "output": refined,
            "steps": [*state.get("steps", []), step],
            "usage": usage,
        }

    async def _finalize(self, state: GraphState, runtime: Runtime[GraphContext]) -> GraphState:
        """Fecha a execucao; sem resposta final, devolve o melhor parcial e explica."""
        context = runtime.context
        output = state.get("output", "")
        if output:
            return {"output": output}
        partial = self._best_partial(state)
        reason = (
            f"O ciclo act/observe terminou apos {state.get('iterations', 0)} de "
            f"{context.max_iterations} iteracoes sem uma resposta final do modelo. "
            "A saida abaixo e o melhor resultado parcial disponivel."
        )
        step = new_step(
            run_id=context.run_id,
            index=len(state.get("steps", [])),
            kind=StepKind.REFLECT,
            name=EXHAUSTED_STEP_NAME,
            inputs={
                "iterations": state.get("iterations", 0),
                "max_iterations": context.max_iterations,
            },
            outputs={"reason": reason, "partial": clip_text(partial)},
        )
        _logger.warning(
            "orchestrator_langgraph_exhausted",
            run_id=context.run_id,
            module=context.request.module.slug,
            iterations=state.get("iterations", 0),
            max_iterations=context.max_iterations,
        )
        return {"output": partial, "steps": [*state.get("steps", []), step]}

    # ------------------------------------------------------------------ #
    # Apoio
    # ------------------------------------------------------------------ #

    def _act_messages(self, state: GraphState, context: GraphContext) -> list[ChatMessage]:
        """Monta o prompt de `act`: instrucoes e plano antes do pedido, observacoes depois."""
        base = list(state.get("messages", []))
        prefix: list[ChatMessage] = []
        if context.tool_names:
            prefix.append(ChatMessage.system(self._tool_instructions(context.tool_names)))
        plan = state.get("plan", "")
        if plan:
            prefix.append(ChatMessage.system(f"Plano acordado para esta tarefa:\n{plan}"))
        head = base[:-1] if base else []
        tail = base[-1:] if base else []
        messages = [*head, *prefix, *tail]
        observations = state.get("observations", [])
        if observations:
            lines = [self._observation_line(item) for item in observations]
            messages.append(ChatMessage.user(_OBSERVATION_INSTRUCTION + "\n\n" + "\n".join(lines)))
        return messages

    def _tool_instructions(self, names: tuple[str, ...]) -> str:
        """Descreve as ferramentas liberadas para este modulo, com o schema de argumentos."""
        lines = [_TOOL_INSTRUCTION_HEADER]
        for described in self._tools.describe(list(names)):
            schema = json.dumps(described["schema"], ensure_ascii=False, sort_keys=True)
            lines.append(f"- {described['name']}: {described['description']}")
            lines.append(f"  argumentos: {schema}")
        return "\n".join(lines)

    @staticmethod
    def _observation_line(observation: Json) -> str:
        """Formata uma observacao para o prompt e para o scratchpad."""
        tool = observation.get("tool", "?")
        if "error" in observation:
            return f"Ferramenta {tool} FALHOU: {observation['error']}"
        payload = json.dumps(observation.get("result", {}), ensure_ascii=False, sort_keys=True)
        return f"Ferramenta {tool} devolveu: {clip_text(payload)}"

    @staticmethod
    def _best_partial(state: GraphState) -> str:
        """Melhor resultado parcial: ultima observacao util, senao o plano, senao vazio."""
        for observation in reversed(state.get("observations", [])):
            if "result" in observation:
                payload = json.dumps(observation["result"], ensure_ascii=False, sort_keys=True)
                return f"Resultado parcial da ferramenta {observation.get('tool', '?')}: {payload}"
        plan = state.get("plan", "")
        if plan:
            return plan
        scratchpad = state.get("scratchpad", [])
        return scratchpad[-1] if scratchpad else ""
