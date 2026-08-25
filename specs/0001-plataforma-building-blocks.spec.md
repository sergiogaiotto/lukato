# SPEC-0001 — Plataforma e Building Blocks

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**

## 1. Visao

`lukato` e um **ecossistema de agentes de IA modulares**. Cada funcionalidade
(autenticacao, processamento, FinOps, conhecimento, deteccao de comerciais) e um
**building block** independente, fracamente acoplado, que a plataforma descobre,
registra, configura, executa, mede e observa de forma uniforme.

O nucleo nao conhece nenhum modulo em particular: conhece apenas o **contrato**
`BaseModule` e a **trinca parametrizavel** guardrail-in → system prompt → guardrail-out.

```text
        ┌──────────────────────────────────────────────────────────┐
        │                     NUCLEO DA PLATAFORMA                 │
        │  registry · composer · guardrails · runs · finops · trace│
        └───────┬───────────┬───────────┬───────────┬──────────────┘
                │           │           │           │
          ┌─────▼───┐ ┌─────▼───┐ ┌─────▼────┐ ┌────▼─────┐   ...
          │  auth   │ │processing│ │  finops  │ │ adwatch  │
          └─────────┘ └──────────┘ └──────────┘ └──────────┘
             building blocks — plugaveis, versionados, isolados
```

## 2. O que faz de um modulo um Building Block

Um building block **deve**:

1. Herdar de `lukato.modules.base.BaseModule` e declarar
   `slug`, `name`, `kind`, `version`, `capabilities`, `config_schema`.
2. Implementar `async def handle(request, ctx) -> ModuleResponse` — ponto unico de execucao.
3. Receber **todas** as dependencias por `ModuleContext` (nunca importar adaptadores
   nem instanciar clientes proprios).
4. Honrar `ModuleBinding`: qualquer chamada de LLM passa pela trinca, via
   `InvokeModule` ou via `ctx.services["composer"]`.
5. Ser registravel por **decorator** (`@register_module`) e por
   **entry point** (`lukato.modules`), sem alterar o nucleo.
6. Declarar sua presenca na UI por `ui() -> UIDescriptor` (itens de menu + templates
   de centro e de painel de contexto).
7. Degradar com clareza: capacidade ausente → `UnsupportedCapability`, nunca falha silenciosa.
8. Ser removivel: desinstalar o modulo nao pode quebrar o nucleo nem os demais modulos.

Um building block **nao pode**: abrir conexoes proprias de banco, ler variaveis de
ambiente diretamente, importar `lukato.interfaces`, ou depender de outro modulo por
import direto (comunicacao entre modulos e sempre via `registry` + `ModuleRequest`).

## 3. Ciclo de vida

```text
descoberta → registro → definicao (ModuleDefinition no banco) → binding
   → setup(ctx) → handle(request, ctx) [N vezes] → teardown()
```
* **Descoberta**: `registry.load_builtin()` + `registry.discover("lukato.modules")`.
* **Registro**: mapeia `slug → classe`. Slug duplicado → `ConflictError`.
* **Definicao**: linha em `modules`; a classe e o *codigo*, a definicao e a
  *configuracao* (binding, config, status). Uma classe pode ter varias definicoes
  (ex.: `processing` configurado como `triagem` e como `resumo`).
* **Execucao**: `InvokeModule` instancia (cache por slug), chama `setup` uma vez e
  `handle` por requisicao.

## 4. Contrato de execucao (`InvokeModule`)

Ordem **normativa** — nenhuma etapa pode ser pulada ou reordenada:

| # | Etapa | Falha |
| --- | --- | --- |
| 1 | resolve `ModuleDefinition` por slug | `ModuleNotFound` (404) |
| 2 | valida `status == ACTIVE` | `ConflictError` (409) |
| 3 | valida permissao `MODULE_INVOKE` do `Principal` | `ForbiddenError` (403) |
| 4 | verifica orcamento (`hard_stop`) | `BudgetExceededError` (402) |
| 5 | abre trace e cria `AgentRun(RUNNING)` | — |
| 6 | **guardrail de entrada** | `BLOCKED` (422) |
| 7 | renderiza **system prompt** | `ValidationError` (422) |
| 8 | executa runtime (orquestrador) | `ProviderError` (502) |
| 9 | **guardrail de saida** | `BLOCKED` (422) |
| 10 | registra `UsageRecord`, custo, steps | — |
| 11 | finaliza `AgentRun`, fecha trace, `commit` | — |

Qualquer excecao entre 5 e 11 grava `AgentRun(FAILED)` com a mensagem, **antes** de
propagar. O run e sempre persistido: nao existe execucao invisivel.

## 5. Modulos embutidos

| slug | kind | capabilities | resumo |
| --- | --- | --- | --- |
| `auth` | `auth` | `login`, `issue_token`, `api_keys`, `rbac` | identidade, JWT, chaves de API, papeis |
| `processing` | `agent` | `chat`, `structured_output`, `tools`, `streaming` | agente generico configuravel — o building block de referencia |
| `finops` | `finops` | `cost_summary`, `budgets`, `forecast` | custo por modulo/modelo/tenant, orcamentos, projecao |
| `knowledge` | `knowledge` | `ingest`, `chunk`, `embed`, `semantic_search` | base de conhecimento com pgvector |
| `adwatch` | `pipeline` | `crud_commercials`, `ingest_media`, `detect`, `review` | SPEC-0010 |

`processing` e a **prova viva** do requisito: ele nao contem regra de negocio propria —
todo o comportamento vem do binding (guardrail de entrada, system prompt, guardrail de
saida, modelo, ferramentas). Criar um agente novo e criar uma `ModuleDefinition`, sem
escrever codigo.

## 6. Escalabilidade e Kubernetes

* **Stateless**: nenhum estado de sessao em memoria; tudo em PostgreSQL. Escala
  horizontal por replicas.
* **Sem I/O bloqueante**: todo adaptador e `async`.
* **Boot rapido e resiliente**: a aplicacao sobe mesmo com provedor de LLM fora do ar
  (adaptador degrada e `/readyz` reporta o componente degradado).
* **Probes**: `/healthz` (liveness, nunca toca no banco) e `/readyz` (readiness,
  verifica banco e registry).
* **Encerramento gracioso**: `lifespan` fecha o pool, faz `flush` do tracer e aguarda
  as requisicoes em voo.
* **Configuracao 100% por ambiente** (12-factor): nenhum arquivo mutavel na imagem.
* **Recursos e HPA**: manifestos em `deploy/k8s` com requests/limits, HPA por CPU e
  `PodDisruptionBudget`.

## 7. Criterios de aceite

1. `GET /api/v1/registry` lista os 5 building blocks com capacidades e schema de config.
2. Criar duas `ModuleDefinition` sobre a **mesma** classe `processing`, com bindings
   diferentes, produz comportamentos diferentes — sem alterar codigo.
3. Um modulo externo instalado via entry point aparece no registry e na UI sem
   qualquer mudanca no nucleo.
4. Remover um modulo embutido do registry nao impede o boot da aplicacao.
5. Toda invocacao produz um `AgentRun` persistido, com steps e custo.
