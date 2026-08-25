"""Adaptadores de orquestracao de agentes: `direct`, `langgraph` e `deepagent`.

Importar este pacote e barato e seguro offline: nem `deepagents` nem
`langchain_openai` sao carregados aqui (o Deep-Agent Harness so as importa dentro do
proprio `run()`), e nenhuma conexao e aberta. A escolha do runtime acontece em
`build_orchestrators` + `resolve`, a partir de `ModuleDefinition.runtime`.

Uso tipico no *composition root*:

```python
registry = build_tool_registry()
context = ToolContext(clock=clock, embeddings=embedder, vector_store=store,
                      uow_factory=uow_factory, settings=settings)
orchestrators = build_orchestrators(llm, settings=settings, tools=registry,
                                    tool_context=context)
runtime = resolve(orchestrators, module.runtime)
```
"""

from __future__ import annotations

from lukato.adapters.orchestrator.deep_agent_harness import (
    REQUIRED_LIBRARIES,
    UNAVAILABLE_REASONS,
    DeepAgentOrchestrator,
)
from lukato.adapters.orchestrator.direct import (
    MAX_STEP_TEXT_CHARS,
    DirectOrchestrator,
    build_messages,
    call_llm,
    clip_text,
    new_step,
    run_id_of,
)
from lukato.adapters.orchestrator.factory import (
    DEFAULT_RUNTIME,
    KNOWN_RUNTIMES,
    build_orchestrators,
    resolve,
)
from lukato.adapters.orchestrator.langgraph_runtime import (
    DEFAULT_MAX_ITERATIONS,
    EXHAUSTED_STEP_NAME,
    PLAN_MAX_TOKENS,
    GraphContext,
    GraphState,
    LangGraphOrchestrator,
    parse_tool_call,
)
from lukato.adapters.orchestrator.tools import (
    CAPABILITY_UNAVAILABLE,
    DEFAULT_COLLECTION,
    MAX_EXPONENT,
    MAX_EXPRESSION_CHARS,
    MAX_RESULT_MAGNITUDE,
    MAX_TOOL_LIMIT,
    ToolContext,
    ToolHandler,
    ToolRegistry,
    ToolSpec,
    build_tool_registry,
    calculator,
    commercial_lookup,
    cost_lookup,
    default_tool_specs,
    knowledge_search,
    now,
    safe_arithmetic,
)

__all__ = [
    "CAPABILITY_UNAVAILABLE",
    "DEFAULT_COLLECTION",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_RUNTIME",
    "EXHAUSTED_STEP_NAME",
    "KNOWN_RUNTIMES",
    "MAX_EXPONENT",
    "MAX_EXPRESSION_CHARS",
    "MAX_RESULT_MAGNITUDE",
    "MAX_STEP_TEXT_CHARS",
    "MAX_TOOL_LIMIT",
    "PLAN_MAX_TOKENS",
    "REQUIRED_LIBRARIES",
    "UNAVAILABLE_REASONS",
    "DeepAgentOrchestrator",
    "DirectOrchestrator",
    "GraphContext",
    "GraphState",
    "LangGraphOrchestrator",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolSpec",
    "build_messages",
    "build_orchestrators",
    "build_tool_registry",
    "calculator",
    "call_llm",
    "clip_text",
    "commercial_lookup",
    "cost_lookup",
    "default_tool_specs",
    "knowledge_search",
    "new_step",
    "now",
    "parse_tool_call",
    "resolve",
    "run_id_of",
    "safe_arithmetic",
]
