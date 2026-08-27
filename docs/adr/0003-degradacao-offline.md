# ADR-0003 — Degradacao offline explicita

**Status:** aceito · **Data:** 2026-08

## Contexto
`hub-gpus.usto.re` e `hub-gpus.claro.com.br` sao hosts internos. PostgreSQL,
Langfuse, FFmpeg, WhisperX, PaddleOCR e PySceneDetect podem nao existir no ambiente de
quem desenvolve ou no runner de CI.

## Decisao
Cada adaptador de borda tem um irmao determinista offline: `EchoLLM`,
`HashingEmbedder`, `NoopTracer`, SQLite com cosseno em memoria, e importadores de
transcricao/cenas/OCR em JSON. A escolha e automatica, registrada em log e reportada
em `/readyz`, `/adwatch/capabilities` e no console.

## A excecao deliberada: embeddings nao degradam sozinhos

LLM, tracer e banco degradam automaticamente. **Embeddings nao**, e a assimetria e
proposital.

Uma resposta de LLM degradada e transitoria — some no proximo request. Um embedding
degradado e *persistido*. `HashingEmbedder` e `Qwen3-Embedding-0.6B` produzem vetores
de 1024 dimensoes em espacos semanticos completamente diferentes; grava-los na mesma
colecao pgvector nao levanta erro em lugar nenhum, so faz a busca devolver resultados
errados para sempre. Cair para hashing por causa de uma queda temporaria do hub
trocaria uma indisponibilidade visivel por uma corrupcao invisivel.

Com `provider=qwen`, portanto, hub fora do ar e `ProviderError`. Hashing so entra
quando pedido explicitamente. E, como o dano seria silencioso, a plataforma nao confia
na configuracao: cada colecao registra o provider/model/dimensao que a produziu e
recusa escrita e leitura divergentes (SPEC-0007 secao 1.2).

## Consequencias
**Positivas:** a suite roda offline; o desenvolvedor trabalha fora da VPN; o pipeline
do AdWatch e demonstravel de ponta a ponta sem GPU.
**Negativas:** dois caminhos por adaptador para manter e testar. Mitigacao: o modo
degradado nunca se disfarca de modo normal — quem consome sabe em que modo esta.
E, no caso dos embeddings, o desenvolvedor precisa pedir o modo offline
explicitamente em vez de recebe-lo de graca.
