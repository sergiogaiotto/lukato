# ADR-0002 — Trinca guardrail-in / system prompt / guardrail-out como invariante

**Status:** aceito · **Data:** 2026-08

## Contexto
O requisito e que a trinca seja parametrizavel para **todo e qualquer** modulo. Se cada
modulo puder chamar o LLM por conta propria, a garantia vira convencao — e convencao
quebra.

## Decisao
`InvokeModule` e o unico caminho de execucao de modulo, com 11 etapas em ordem fixa.
A trinca vive em `ModuleDefinition.binding` e e resolvida por `ModuleComposer`. Nenhum
modulo instancia cliente de LLM: recebe `LLMPort` por `ModuleContext`.

## Consequencias
**Positivas:** garantia estrutural, nao documental; bloqueio de entrada acontece antes
de qualquer chamada ao provedor; toda execucao e auditavel; trocar politica nao exige
redeploy.
**Negativas:** modulos com necessidade legitima de multiplas chamadas de LLM precisam
faze-las pelo runtime (LangGraph/Deep-Agent), nao ad hoc.
