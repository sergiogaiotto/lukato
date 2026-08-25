# SPEC-0005 — FinOps

> **Status:** aceito · **Depende de:** SPEC-0000 · **Normativo.**

## 1. Objetivo

Medir e controlar o custo de cada execucao, por modulo, modelo e tenant, e expor o
resultado na API, no console e na barra de status.

## 2. Calculo

```python
custo = (prompt_tokens/1000)*preco_entrada + (completion_tokens/1000)*preco_saida
```
`CostCalculator` (dominio puro):
```python
class CostCalculator:
    def __init__(self, prices: Mapping[str, ModelPrice], *,
                 default_input: float = 0.0, default_output: float = 0.0) -> None
    def price_for(self, model: str) -> ModelPrice
    def cost(self, model: str, usage: TokenUsage) -> float
    def summarize(self, records: Iterable[UsageRecord]) -> CostSummary
    def check_budget(self, budget: Budget, spent: float) -> BudgetCheck
```
`BudgetCheck`: `{"ok": bool, "ratio": float, "alert": bool, "blocked": bool, "remaining": float}`.
`blocked` so e `True` quando `budget.hard_stop and ratio >= 1.0`.

Modelo desconhecido usa os precos default e registra a lacuna em
`CostSummary.unknown_models` — custo nunca e silenciosamente zerado sem sinal.
Arredondamento: 8 casas decimais; a UI formata com 5.

## 3. Captura

`InvokeModule` grava um `UsageRecord` por chamada de LLM (etapa 10 da SPEC-0001) e
soma no `AgentRun`. Sem tokens reportados pelo provedor, estima por
`len(texto)/4` e marca `estimated=true` nos metadados do step.

## 4. API `/api/v1/finops`

| Metodo | Rota | Descricao |
| --- | --- | --- |
| `GET` | `/summary` | `since`, `until`, `module_slug`, `tenant_id` → `CostSummary` |
| `GET` | `/series` | serie temporal (`bucket=hour|day`) para os graficos |
| `GET` | `/usage` | registros paginados |
| `GET` | `/prices` · `PUT /prices` | tabela de precos por modelo |
| `GET`/`POST` | `/budgets` | listar/criar orcamentos |
| `GET`/`PUT`/`DELETE` | `/budgets/{id}` | gerenciar |
| `GET` | `/budgets/{id}/status` | `BudgetCheck` corrente |

## 5. Barra de status do console

Mostra, para as ultimas 24 h: custo por modulo (ponto colorido + valor), barra
proporcional, **Custo total** e numero de execucoes — igual ao rodape da referencia
visual. Os dados vem de `GET /api/v1/finops/summary?since=24h`.

## 6. Criterios de aceite

1. Uma execucao com `TokenUsage(1000, 500)` e preco `(0.002, 0.006)` custa `0.005`.
2. Orcamento com `hard_stop=true` estourado bloqueia a proxima invocacao com HTTP 402.
3. Orcamento em 80% marca `alert=true` sem bloquear.
4. `GET /finops/summary` agrega corretamente por modulo e por modelo.
