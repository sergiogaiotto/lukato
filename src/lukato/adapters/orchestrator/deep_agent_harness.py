"""Orquestrador `deepagent`: Deep-Agent Harness (SPEC-0004 secao 3).

O harness da biblioteca `deepagents` traz planejamento, sub-agentes e sistema de
arquivos virtual. Ele exige um modelo LangChain, entao este e o unico runtime que
constroi um `ChatOpenAI` apontado para o hub — e so o faz **dentro** de `run()`.

Regras que este adaptador respeita a risca:

* import de `deepagents` e `langchain_openai` e **preguicoso**: importar este modulo
  nao carrega nenhuma das duas, e o boot da aplicacao nao depende delas;
* `available` e `False` quando qualquer uma das bibliotecas falta **ou** quando nao
  ha chave de API — nesse caso `supports()` devolve `False` e o container degrada
  para `langgraph`, registrando o motivo;
* nenhuma excecao de biblioteca externa escapa: tudo vira `ProviderError` com a causa.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Final

from lukato.adapters.orchestrator.direct import clip_text, new_step, run_id_of
from lukato.adapters.orchestrator.tools import ToolContext, ToolRegistry, ToolSpec
from lukato.config import Settings, get_logger
from lukato.domain.errors import LukatoError, ProviderError, ValidationError
from lukato.domain.models.run import RunStep, StepKind, TokenUsage
from lukato.domain.ports.llm import LLMPort
from lukato.domain.ports.orchestrator import OrchestratorRequest, OrchestratorResult
from lukato.domain.types import DEFAULT_TENANT, Id, Json

__all__ = [
    "REQUIRED_LIBRARIES",
    "UNAVAILABLE_REASONS",
    "DeepAgentOrchestrator",
]

_logger = get_logger(__name__)

REQUIRED_LIBRARIES: Final[tuple[str, ...]] = ("deepagents", "langchain_openai")
"""Bibliotecas que precisam existir para o harness poder rodar."""

UNAVAILABLE_REASONS: Final[dict[str, str]] = {
    "missing_libraries": (
        "as bibliotecas do Deep-Agent Harness nao estao instaladas "
        "(pip install deepagents langchain-openai)"
    ),
    "missing_api_key": (
        "LUKATO_LLM__API_KEY ausente: o harness precisa falar com o hub por HTTP, "
        "e sem credencial nao ha como autenticar"
    ),
    "available": "harness disponivel: bibliotecas instaladas e credencial presente",
}
"""Motivos legiveis de indisponibilidade, prontos para log, `/readyz` e console."""

_MAX_SUBAGENTS: Final[int] = 12
"""Teto de sub-agentes aceitos em `config['subagents']` (protege custo e latencia)."""


def _message_text(content: Any) -> str:
    """Normaliza o `content` de uma mensagem LangChain (texto puro ou blocos) em texto."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def _message_usage(message: Any) -> TokenUsage:
    """Extrai `usage_metadata` de uma mensagem LangChain, quando o provedor a reporta."""
    metadata = getattr(message, "usage_metadata", None)
    if not isinstance(metadata, dict):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=int(metadata.get("input_tokens", 0) or 0),
        completion_tokens=int(metadata.get("output_tokens", 0) or 0),
        total_tokens=int(metadata.get("total_tokens", 0) or 0),
    )


def _tool_call_names(message: Any) -> list[str]:
    """Nomes das ferramentas pedidas por uma mensagem do assistente."""
    calls = getattr(message, "tool_calls", None)
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        if isinstance(call, dict):
            name = call.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


class DeepAgentOrchestrator:
    """Runtime `deepagent`: delega a execucao ao Deep-Agent Harness da `deepagents`."""

    name: str = "deepagent"

    def __init__(
        self,
        llm: LLMPort,
        *,
        settings: Settings,
        tools: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._tools = tools if tools is not None else ToolRegistry()
        self._tool_context = tool_context if tool_context is not None else ToolContext()
        self._libraries: bool | None = None

    # ------------------------------------------------------------------ #
    # Disponibilidade
    # ------------------------------------------------------------------ #

    @property
    def libraries_present(self) -> bool:
        """True quando `deepagents` e `langchain_openai` podem ser importados."""
        if self._libraries is None:
            self._libraries = all(self._has_module(name) for name in REQUIRED_LIBRARIES)
        return self._libraries

    @property
    def api_key_configured(self) -> bool:
        """True quando ha credencial para o hub (o harness so fala HTTP)."""
        return bool(self._settings.llm.api_key_value)

    @property
    def available(self) -> bool:
        """True somente com as duas bibliotecas presentes E credencial configurada."""
        return self.libraries_present and self.api_key_configured

    @property
    def unavailable_reason(self) -> str:
        """Explica em portugues por que o harness esta (in)disponivel."""
        if not self.libraries_present:
            return UNAVAILABLE_REASONS["missing_libraries"]
        if not self.api_key_configured:
            return UNAVAILABLE_REASONS["missing_api_key"]
        return UNAVAILABLE_REASONS["available"]

    @staticmethod
    def _has_module(name: str) -> bool:
        """Verifica a presenca do modulo sem importa-lo (nao pesa o boot)."""
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def supports(self, runtime: str) -> bool:
        """True apenas para `deepagent` E somente quando o harness esta disponivel."""
        return (runtime or "").strip().lower() == self.name and self.available

    # ------------------------------------------------------------------ #
    # Execucao
    # ------------------------------------------------------------------ #

    async def run(self, request: OrchestratorRequest) -> OrchestratorResult:
        """Monta o harness, executa e converte as mensagens em `RunStep`s."""
        if not self.available:
            raise ProviderError(
                f"O runtime deepagent nao esta disponivel: {self.unavailable_reason}.",
                details={"runtime": self.name, "reason": self.unavailable_reason},
            )
        run_id = run_id_of(request)
        binding = request.module.binding
        tool_context = replace(
            self._tool_context,
            module_slug=request.module.slug,
            tenant_id=self._tenant_of(request),
        )
        names = self._tool_names(request)
        subagent_config = self._subagent_config(request)
        started = time.perf_counter()
        try:
            agent = self._build_agent(request, names, subagent_config, tool_context)
            timeout = binding.timeout_seconds if binding.timeout_seconds > 0 else None
            payload = {"messages": [{"role": "user", "content": request.input_text}]}
            if timeout is None:
                out = await agent.ainvoke(payload)
            else:
                async with asyncio.timeout(timeout):
                    out = await agent.ainvoke(payload)
        except LukatoError:
            raise
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            _logger.warning(
                "orchestrator_deepagent_failed",
                run_id=run_id,
                module=request.module.slug,
                error=type(exc).__name__,
                detail=str(exc)[:300],
                latency_ms=round(latency_ms, 3),
            )
            raise ProviderError(
                f"O Deep-Agent Harness falhou: {type(exc).__name__}: {exc}",
                details={
                    "runtime": self.name,
                    "module": request.module.slug,
                    "error": type(exc).__name__,
                },
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        messages = out.get("messages") if isinstance(out, dict) else None
        if not isinstance(messages, list) or not messages:
            raise ProviderError(
                "O Deep-Agent Harness devolveu uma resposta sem mensagens.",
                details={"runtime": self.name, "module": request.module.slug},
            )
        steps, usage = self._steps_from_messages(messages, run_id=run_id)
        output_text = _message_text(getattr(messages[-1], "content", ""))
        if not steps:
            steps = [
                new_step(
                    run_id=run_id,
                    index=0,
                    kind=StepKind.LLM,
                    name="deepagent.answer",
                    inputs={"input": clip_text(request.input_text)},
                    outputs={"content": clip_text(output_text)},
                    usage=usage,
                    latency_ms=latency_ms,
                )
            ]
        else:
            steps[-1].latency_ms = round(latency_ms, 3)
        _logger.info(
            "orchestrator_deepagent_completed",
            run_id=run_id,
            module=request.module.slug,
            messages=len(messages),
            steps=len(steps),
            subagents=len(subagent_config),
            tools=names,
            latency_ms=round(latency_ms, 3),
            total_tokens=usage.total_tokens,
        )
        return OrchestratorResult(
            output_text=output_text,
            steps=steps,
            usage=usage,
            metadata={
                "runtime": self.name,
                "model": binding.model or self._settings.llm.model,
                "messages": len(messages),
                "subagents": [item["name"] for item in subagent_config],
                "tools": names,
            },
        )

    # ------------------------------------------------------------------ #
    # Montagem do harness (imports preguicosos vivem aqui)
    # ------------------------------------------------------------------ #

    def _build_agent(
        self,
        request: OrchestratorRequest,
        names: Sequence[str],
        subagent_config: Sequence[Json],
        tool_context: ToolContext,
    ) -> Any:
        """Cria o `ChatOpenAI` do hub e o agente profundo com ferramentas e sub-agentes."""
        try:
            from deepagents import create_deep_agent
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - barrado por `available`
            raise ProviderError(
                f"Deep-Agent Harness indisponivel: {exc}",
                details={"runtime": self.name, "libraries": list(REQUIRED_LIBRARIES)},
            ) from exc
        llm_settings = self._settings.llm
        binding = request.module.binding
        chat_model = ChatOpenAI(
            model=binding.model or llm_settings.model or self._llm.default_model,
            base_url=llm_settings.base_url,
            api_key=llm_settings.api_key_value,
            temperature=(
                binding.temperature if binding.temperature is not None else llm_settings.temperature
            ),
            max_tokens=binding.max_tokens or llm_settings.max_tokens,
            timeout=binding.timeout_seconds or llm_settings.timeout,
            max_retries=llm_settings.max_retries,
        )
        tools = self._langchain_tools(names, tool_context)
        subagents = self._build_subagents(subagent_config, tool_context)
        return create_deep_agent(
            model=chat_model,
            tools=tools,
            system_prompt=request.system_prompt or None,
            subagents=subagents or None,
        )

    def _langchain_tools(self, names: Sequence[str], ctx: ToolContext) -> list[Any]:
        """Converte os `ToolSpec` do registro em ferramentas estruturadas do LangChain."""
        if not names:
            return []
        from langchain_core.tools import StructuredTool

        return [
            StructuredTool(
                name=spec.name,
                description=spec.description,
                args_schema=dict(spec.schema),
                coroutine=self._coroutine_for(spec, ctx),
            )
            for spec in self._tools.resolve(list(names))
        ]

    def _coroutine_for(self, spec: ToolSpec, ctx: ToolContext) -> Callable[..., Any]:
        """Adapta a ferramenta ao harness: entra por kwargs, sai como JSON em texto.

        Falha de ferramenta volta como `{"error": ...}` para o agente seguir raciocinando,
        exatamente como no runtime `langgraph` — nunca como excecao que aborta o run.
        """
        registry = self._tools

        async def _invoke(**kwargs: Any) -> str:
            try:
                result = await registry.execute(spec.name, dict(kwargs), ctx)
            except LukatoError as exc:
                result = {"error": f"{exc.code}: {exc}"}
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            return json.dumps(result, ensure_ascii=False, default=str, sort_keys=True)

        _invoke.__name__ = spec.name
        _invoke.__doc__ = spec.description
        return _invoke

    def _build_subagents(self, config: Sequence[Json], ctx: ToolContext) -> list[Json]:
        """Traduz `config['subagents']` para o formato `SubAgent` da `deepagents`."""
        subagents: list[Json] = []
        for item in config:
            names = [str(name) for name in item.get("tools", [])]
            subagents.append(
                {
                    "name": item["name"],
                    "description": item["description"],
                    "system_prompt": item["prompt"],
                    "tools": self._langchain_tools(names, ctx),
                }
            )
        return subagents

    # ------------------------------------------------------------------ #
    # Apoio
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tenant_of(request: OrchestratorRequest) -> str:
        """Inquilino da requisicao, com o padrao do dominio quando ausente."""
        tenant = request.metadata.get("tenant_id")
        return tenant if isinstance(tenant, str) and tenant else DEFAULT_TENANT

    def _tool_names(self, request: OrchestratorRequest) -> list[str]:
        """Une `binding.tools` e `request.tools`, sem repetir, validando no registro."""
        ordered: list[str] = []
        for name in [*request.module.binding.tools, *request.tools]:
            cleaned = (name or "").strip()
            if cleaned and cleaned not in ordered:
                ordered.append(cleaned)
        self._tools.resolve(ordered)
        return ordered

    def _subagent_config(self, request: OrchestratorRequest) -> list[Json]:
        """Le e valida `config['subagents']`; entrada malformada gera `ValidationError`."""
        raw = request.module.config.get("subagents")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValidationError(
                "config['subagents'] deve ser uma lista de objetos.",
                details={"module": request.module.slug, "received": type(raw).__name__},
            )
        if len(raw) > _MAX_SUBAGENTS:
            raise ValidationError(
                f"config['subagents'] aceita no maximo {_MAX_SUBAGENTS} sub-agentes.",
                details={"module": request.module.slug, "received": len(raw)},
            )
        parsed: list[Json] = []
        for position, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValidationError(
                    "Cada sub-agente deve ser um objeto com name, description e prompt.",
                    details={"module": request.module.slug, "index": position},
                )
            values = {key: item.get(key) for key in ("name", "description", "prompt")}
            missing = [
                key
                for key, value in values.items()
                if not isinstance(value, str) or not value.strip()
            ]
            if missing:
                raise ValidationError(
                    f"Sub-agente #{position} sem os campos obrigatorios: {', '.join(missing)}.",
                    details={"module": request.module.slug, "index": position, "missing": missing},
                )
            tools = item.get("tools", [])
            if not isinstance(tools, list):
                raise ValidationError(
                    f"Sub-agente #{position}: 'tools' deve ser uma lista de nomes.",
                    details={"module": request.module.slug, "index": position},
                )
            names = [str(name).strip() for name in tools if str(name).strip()]
            self._tools.resolve(names)
            parsed.append(
                {
                    "name": values["name"],
                    "description": values["description"],
                    "prompt": values["prompt"],
                    "tools": names,
                }
            )
        return parsed

    def _steps_from_messages(
        self, messages: Sequence[Any], *, run_id: Id
    ) -> tuple[list[RunStep], TokenUsage]:
        """Deriva steps PLAN/TOOL/LLM das mensagens intermediarias do harness."""
        steps: list[RunStep] = []
        usage = TokenUsage()
        for message in messages:
            kind = str(getattr(message, "type", "") or "")
            text = _message_text(getattr(message, "content", ""))
            if kind == "ai":
                message_usage = _message_usage(message)
                usage = usage + message_usage
                tool_calls = _tool_call_names(message)
                steps.append(
                    new_step(
                        run_id=run_id,
                        index=len(steps),
                        kind=StepKind.PLAN if tool_calls else StepKind.LLM,
                        name=f"deepagent.{'plan' if tool_calls else 'llm'}#{len(steps)}",
                        inputs={"tool_calls": tool_calls},
                        outputs={"content": clip_text(text)},
                        usage=message_usage,
                    )
                )
            elif kind == "tool":
                steps.append(
                    new_step(
                        run_id=run_id,
                        index=len(steps),
                        kind=StepKind.TOOL,
                        name=f"tool:{getattr(message, 'name', '?')}",
                        inputs={"tool": getattr(message, "name", "")},
                        outputs={"result": clip_text(text)},
                    )
                )
        return steps, usage
