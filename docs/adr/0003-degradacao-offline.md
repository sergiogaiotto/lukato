# ADR-0003 — Degradacao offline explicita

**Status:** aceito · **Data:** 2026-08

## Contexto
`hub-gpus-lab.usto.re` e `hub-gpus.claro.com.br` sao hosts internos. PostgreSQL,
Langfuse, FFmpeg, WhisperX, PaddleOCR e PySceneDetect podem nao existir no ambiente de
quem desenvolve ou no runner de CI.

## Decisao
Cada adaptador de borda tem um irmao determinista offline: `EchoLLM`,
`HashingEmbedder`, `NoopTracer`, SQLite com cosseno em memoria, e importadores de
transcricao/cenas/OCR em JSON. A escolha e automatica, registrada em log e reportada
em `/readyz`, `/adwatch/capabilities` e no console.

## Consequencias
**Positivas:** a suite roda offline; o desenvolvedor trabalha fora da VPN; o pipeline
do AdWatch e demonstravel de ponta a ponta sem GPU.
**Negativas:** dois caminhos por adaptador para manter e testar. Mitigacao: o modo
degradado nunca se disfarca de modo normal — quem consome sabe em que modo esta.
