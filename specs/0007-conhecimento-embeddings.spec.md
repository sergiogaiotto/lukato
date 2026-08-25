# SPEC-0007 — Base de conhecimento e embeddings

> **Status:** aceito · **Depende de:** SPEC-0000, SPEC-0011 · **Normativo.**

## 1. Provedor de embeddings

Qwen3-Embedding-0.6B servido pelo hub interno, API compativel com OpenAI:

```text
POST {base_url}/embeddings
{ "model": "Qwen/Qwen3-Embedding-0.6B", "input": ["texto", ...] }
```
`base_url` **nao** inclui `/embeddings` (o cliente acrescenta). Dimensao padrao 1024.

> Trocar `dimensions` exige **re-embeddar** a colecao inteira (`agente_evidence`).
> Fazer isso com dados em producao derruba a busca semantica ate o reindex terminar.
> A aplicacao detecta a divergencia entre a dimensao configurada e a persistida e
> recusa gravar, com mensagem explicita, em vez de corromper a colecao.

`HashingEmbedder` (fallback offline determinista): projeta n-gramas de caracteres em
`dimensions` posicoes via `blake2b`, normaliza L2. Nao tem qualidade semantica real —
serve para desenvolvimento e testes, e **sempre** se identifica como `hashing` em
`/health` e no console.

## 2. Ingestao

```text
Document → normalizacao → chunking → embeddings → pgvector
```
* Chunking por caracteres com sobreposicao: `chunk_size=1200`, `overlap=200`,
  quebrando preferencialmente em `\n\n`, depois `\n`, depois `. `, depois espaco.
* `checksum` = SHA-256 do conteudo normalizado; reingestao do mesmo conteudo e
  idempotente (atualiza metadados, nao duplica chunks).
* Embeddings em lote (`batch_size`), com `tenacity` (3 tentativas, backoff exponencial).

## 3. Busca

`SearchKnowledge(query, collection, limit, filters)`:
1. embedding da consulta;
2. `VectorStorePort.search` (cosseno);
3. opcional `rerank=true` → reordena com `LexicalMatcher` sobre o texto do chunk;
4. devolve `SearchHit` com `score` normalizado em `[0,1]`.

## 4. API `/api/v1/knowledge`

`GET /collections` · `POST /documents` · `GET /documents` · `GET|DELETE /documents/{id}` ·
`POST /documents/{id}/reindex` · `POST /search` · `GET /health`.

## 5. Criterios de aceite

1. Ingerir o mesmo documento duas vezes nao duplica chunks.
2. Busca devolve o chunk correto com o `HashingEmbedder` em teste determinista.
3. Divergencia de dimensao e recusada com mensagem clara, sem corromper dados.
4. Em PostgreSQL a busca usa o indice HNSW; em SQLite, numpy em memoria.
