# ADR-0001 — Arquitetura hexagonal

**Status:** aceito · **Data:** 2026-08

## Contexto
O ecossistema precisa trocar provedor de LLM, banco vetorial, backend de tracing e
runtime de agente sem reescrever regra de negocio. Tambem precisa ser testavel sem
acesso a rede corporativa.

## Decisao
Ports & Adapters. `domain/` contem modelos, protocolos (portas) e servicos puros;
`application/` contem casos de uso; `adapters/` implementa as portas; `interfaces/`
sao os driving adapters. O composition root e o unico ponto que enxerga tudo.

## Consequencias
**Positivas:** motor de guardrails, calculo de custo e matching do AdWatch testaveis
sem I/O; trocar provedor e escrever um adaptador; a regra de dependencia e verificavel
por teste automatizado.
**Negativas:** mais arquivos e uma camada de indirecao; exige disciplina para nao
vazar tipos de biblioteca para dentro do dominio.
