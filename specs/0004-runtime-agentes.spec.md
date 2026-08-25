# SPEC-0004 — Runtime de agentes (LangGraph + Deep-Agent Harness)

> **Status:** aceito · **Depende de:** SPEC-0000, SPEC-0003 · **Normativo.**

## 1. Runtimes suportados

`ModuleDefinition.runtime` seleciona o orquestrador:

| runtime | adaptador | quando usar |
| --- | --- | --- |
| `direct` | `DirectOrchestrator` | uma unica chamada de LLM (o mais barato) |
| `langgraph` | `LangGraphOrchestrator` | grafo de estados com plano/acao/reflexao |
| `deepagent` | `DeepAgentOrchestrator` | Deep-Agent Harness: planejamento, sub-agentes, sistema de arquivos virtual |

Runtime desconhecido → `UnsupportedCapability` (501). O container escolhe o
orquestrador por `supports(runtime)`.

## 2. Grafo LangGraph (`adapters/orchestrator/langgraph_runtime.py`)

```text
START → prepare → plan → act ⇄ observe → reflect → finalize → END
                    │        │
                    │        └── (tool calls pendentes) volta para act
                    └── (sem plano necessario) pula direto para finalize
```

* Estado (`TypedDict`): `messages`, `plan`, `scratchpad`, `tool_calls`, `observations`,
  `iterations`, `output`, `steps`, `usage`.
* `max_iterations` (padrao 6, de `config["max_iterations"]`) evita laco infinito;
  ao estourar, `finalize` devolve o melhor resultado parcial e registra um step `REFLECT`
  com a razao.
* Cada no gera um `RunStep` com `kind`, `latency_ms` e `usage`.
* O grafo usa **`LLMPort`**, nunca um cliente LangChain — mantem o hexagono intacto.
* Ferramentas: resolvidas de `ToolRegistry` por nome (`binding.tools`); ferramenta
  inexistente → `ValidationError` antes de executar.

## 3. Deep-Agent Harness (`adapters/orchestrator/deep_agent_harness.py`)

* Usa `deepagents.create_deep_agent(model=..., tools=..., system_prompt=..., subagents=...)`.
* O modelo e um `ChatOpenAI` apontado para o hub (`base_url`, `api_key`, `model`).
* `available` e `False` quando `deepagents`/`langchain_openai` nao estao instalados **ou**
  quando nao ha chave de API; nesse caso `supports()` devolve `False` e o container
  cai para `langgraph`, registrando a degradacao.
* Sub-agentes vem de `config["subagents"]: [{"name","description","prompt","tools"}]`.
* Import de `deepagents` e **preguicoso** (dentro do metodo), para nao pesar o boot.
* O harness ainda passa pela trinca: o system prompt entregue ao harness ja e o
  renderizado pela plataforma, e a saida final passa pelo guardrail de saida.

## 4. Ferramentas (`adapters/orchestrator/tools.py`)

Registro simples e auditavel:

| nome | descricao |
| --- | --- |
| `knowledge_search` | busca semantica na base de conhecimento |
| `cost_lookup` | custo acumulado do modulo/tenant |
| `commercial_lookup` | consulta o catalogo AdWatch |
| `now` | data/hora UTC (determinismo em testes via `ClockPort`) |
| `calculator` | aritmetica segura (AST allowlist, **sem `eval`**) |

Toda ferramenta tem: nome, descricao, JSON Schema de argumentos e execucao `async`.
Erro de ferramenta vira observacao (`ERROR` step), nunca excecao que aborta o run.

## 5. Criterios de aceite

1. Um modulo `direct` executa com o adaptador `echo` (offline) e produz `AgentRun`
   com steps `GUARDRAIL_IN`, `PROMPT`, `LLM`, `GUARDRAIL_OUT`.
2. Um modulo `langgraph` com `max_iterations=1` nao entra em laco.
3. `deepagent` sem `deepagents` instalado degrada para `langgraph` com aviso e o
   endpoint continua respondendo 200.
4. `calculator` recusa `__import__("os")` sem executar nada.
