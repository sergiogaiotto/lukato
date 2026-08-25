# ADR-0005 — PostgreSQL + pgvector como unico armazenamento

**Status:** aceito · **Data:** 2026-08

## Contexto
O sistema precisa de dados relacionais (modulos, runs, custos, catalogo) e de busca
vetorial (conhecimento, fingerprints). Um banco vetorial dedicado (Qdrant, Milvus,
FAISS) adicionaria um servico, um backup e um modo de falha.

## Decisao
PostgreSQL 16 com a extensao pgvector para tudo, indice HNSW com `vector_cosine_ops`.
Em dev e testes, SQLite com a mesma modelagem e busca por cosseno em memoria.

## Consequencias
**Positivas:** uma transacao cobre dados e vetores; um backup; um servico a operar.
**Negativas:** em catalogos muito grandes um indice dedicado seria mais rapido. A porta
`VectorStorePort` mantem essa troca barata — `faiss-cpu` ja esta previsto em
`requirements-media.txt` para o caso do catalogo do AdWatch crescer.
