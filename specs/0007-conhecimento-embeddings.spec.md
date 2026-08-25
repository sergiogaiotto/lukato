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

### 1.1 Por que embeddings NAO degradam automaticamente

O adaptador de LLM cai para `echo` sozinho quando falta credencial. **Embeddings nao
fazem isso, e a diferenca e deliberada.**

Uma resposta de LLM degradada e transitoria: some no proximo request. Um embedding
degradado e **persistido**. `HashingEmbedder` e `Qwen3-Embedding-0.6B` produzem vetores
de 1024 dimensoes em espacos semanticos completamente diferentes; gravar os dois na
mesma colecao pgvector nao da erro em lugar nenhum — a busca simplesmente passa a
devolver resultados errados, para sempre, sem sinal. Trocar de embedder em silencio
por causa de uma indisponibilidade temporaria do hub corromperia a colecao de forma
irreversivel.

Por isso: com `provider=qwen` configurado, o hub fora do ar e **erro** (`ProviderError`),
nunca degradacao silenciosa. O modo hashing so entra quando alguem o pede
explicitamente (`LUKATO_EMBEDDING__PROVIDER=hashing`) ou quando nao ha `base_url`.
Desenvolvimento e testes devem pedir hashing explicitamente.

### 1.2 Guarda de compatibilidade da colecao (obrigatoria)

Como o dano e silencioso, a plataforma nao confia na disciplina de quem configura:

* Toda colecao registra o `provider`, o `model` e as `dimensions` que a produziram
  (chaves `embedding_provider`, `embedding_model`, `embedding_dimensions` no
  `metadata` dos chunks, e derivados na resposta de `GET /knowledge/collections`).
* Gravar em uma colecao com provider/model/dimensao diferente do configurado e
  **recusado** com `ValidationError` explicando a divergencia e o que fazer
  (re-embeddar a colecao inteira ou voltar a configuracao anterior).
* `POST /knowledge/search` em colecao produzida por outro embedder devolve o mesmo
  erro, em vez de resultados sem sentido.
* `GET /knowledge/health` mostra o embedder corrente e, por colecao, o que a produziu.

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
