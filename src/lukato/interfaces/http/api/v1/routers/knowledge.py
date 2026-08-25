"""Rotas de `/api/v1/knowledge` — base de conhecimento, ingestao e busca semantica.

Este recurso e a memoria consultavel da plataforma: documentos entram por
`POST /documents`, viram trechos com vetor, e saem por `POST /search` como o
contexto que os modulos citam nas respostas (SPEC-0007).

Tres pontos do contrato desta borda merecem explicacao:

* **a identidade do embedder acompanha a colecao.** Provedor, modelo e dimensao
  que produziram cada colecao voltam em `GET /collections` e em `GET /health`, e
  ingerir ou buscar em uma colecao produzida por outro embedder e recusado com
  `422` (SPEC-0007 secao 1.2). A recusa e deliberada: vetores de espacos
  semanticos diferentes convivem sem erro no indice e so aparecem, depois, como
  resultados errados — sem sinal nenhum de que algo quebrou;
* **a ingestao e idempotente.** Reenviar o mesmo conteudo atualiza os metadados e
  devolve `idempotent: true` sem re-embeddar nem duplicar trechos; a resposta
  sempre traz o documento gravado, quantos trechos ficaram e qual embedder foi
  usado;
* **a busca e `POST`, nao `GET`.** A consulta carrega `filters` aninhados e texto
  livre de tamanho arbitrario, que nao cabem com honestidade em uma query string.

Nenhuma rota toca repositorio ou indice vetorial: toda operacao passa por um caso
de uso de :mod:`lukato.application.use_cases.knowledge`, construido com o
`Container` injetado por :func:`lukato.interfaces.http.deps.get_container`.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Response, status

from lukato.application.use_cases.knowledge import (
    DeleteDocument,
    DocumentFilter,
    GetDocument,
    IngestDocument,
    KnowledgeHealth,
    ListCollections,
    ListDocuments,
    ReindexDocument,
    SearchKnowledge,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.common import Page, error_responses
from lukato.interfaces.http.schemas.knowledge import (
    CollectionOut,
    DocumentCreate,
    DocumentOut,
    IngestResponse,
    KnowledgeHealthOut,
    SearchRequest,
    SearchResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/knowledge", tags=["conhecimento"])
"""Roteador do recurso de conhecimento (SPEC-0000 secao 11)."""

_Reader = Annotated[Principal, Depends(require(Permission.KNOWLEDGE_READ))]
"""Principal que ja provou ter `knowledge:read`."""

_Writer = Annotated[Principal, Depends(require(Permission.KNOWLEDGE_WRITE))]
"""Principal que ja provou ter `knowledge:write`."""

_DocumentId = Annotated[
    str,
    Path(min_length=1, description="Identificador do documento (`Document.id`)."),
]
"""Referencia do documento recebida no caminho da rota."""

_DOCUMENT_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404)
"""Erros das rotas que resolvem um documento existente."""

_INDEX_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404, 422, 502)
"""Erros das rotas que chamam o provedor de embeddings."""


# ---------------------------------------------------------------------------
# Colecoes e saude (rotas literais, declaradas antes das rotas por identificador)
# ---------------------------------------------------------------------------
@router.get(
    "/collections",
    response_model=Page[CollectionOut],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Lista as colecoes da base",
    description=(
        "Devolve toda colecao conhecida — a padrao da instalacao, as que tem documentos "
        "e as que existem no indice vetorial — com a contagem de documentos e a "
        "identidade do embedder que a produziu. `compatible: false` marca a colecao "
        "indexada por outro provedor, modelo ou dimensao: ingerir ou buscar nela e "
        "recusado ate que ela seja reindexada com o embedder corrente."
    ),
)
async def list_collections(container: ContainerDep, principal: _Reader) -> Page[CollectionOut]:
    """Devolve as colecoes no envelope de lista da API."""
    entries = await ListCollections(container).execute(principal)
    return Page[CollectionOut].of(
        [CollectionOut.from_result(entry) for entry in entries],
        total=len(entries),
        limit=max(1, len(entries)),
    )


@router.get(
    "/health",
    response_model=KnowledgeHealthOut,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Saude da base de conhecimento",
    description=(
        "Mostra o embedder corrente (provedor, modelo, dimensao), se ele respondeu a "
        "sonda, os parametros de recorte em uso e, por colecao, qual embedder a "
        "produziu. `degraded: true` indica o modo `hashing` — determinista e util em "
        "desenvolvimento, sem qualidade semantica real — ou o provedor configurado "
        "fora do ar; `reason` diz qual dos dois. A rota nunca falha por causa do "
        "provedor: um provedor mudo e informacao, nao erro."
    ),
)
async def knowledge_health(container: ContainerDep, principal: _Reader) -> KnowledgeHealthOut:
    """Devolve o relatorio de saude do conhecimento (SPEC-0007 secao 1.2)."""
    report = await KnowledgeHealth(container).execute(principal)
    return KnowledgeHealthOut.from_result(report)


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------
@router.post(
    "/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403, 422, 502),
    summary="Busca semantica na base",
    description=(
        "Embedda a consulta, recupera os trechos mais proximos por cosseno e devolve "
        "cada um com `score` normalizado em `[0, 1]`. `filters` restringe por metadado "
        "do trecho e `rerank=true` reordena combinando o score vetorial com a "
        "similaridade lexical do texto, o que resgata o trecho com o termo exato que o "
        "vizinho mais proximo teria deixado para tras. Buscar em colecao produzida por "
        "outro embedder responde `422` em vez de devolver vizinhos sem sentido."
    ),
)
async def search_knowledge(
    payload: SearchRequest,
    container: ContainerDep,
    principal: _Reader,
) -> SearchResponse:
    """Devolve os trechos recuperados e o recorte efetivamente consultado."""
    use_case = SearchKnowledge(container)
    hits = await use_case.execute(payload.to_query(), principal)
    return SearchResponse.of(
        payload.query,
        use_case.collection_of(payload.collection),
        hits,
        reranked=payload.rerank,
    )


# ---------------------------------------------------------------------------
# Documentos
# ---------------------------------------------------------------------------
@router.post(
    "/documents",
    response_model=IngestResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 422, 502),
    summary="Ingerir documento",
    description=(
        "Normaliza o texto, recorta em trechos com sobreposicao, gera os embeddings e "
        "grava tudo na colecao pedida (a padrao quando `collection` e omitida). "
        "Reingerir o mesmo conteudo e **idempotente**: o documento e reconhecido pelo "
        "`document_id`, pela `source` ou pelo titulo dentro da colecao, os metadados "
        "sao atualizados e a resposta volta com `idempotent: true`, sem nenhum "
        "embedding novo e sem trecho duplicado."
    ),
)
async def ingest_document(
    payload: DocumentCreate,
    container: ContainerDep,
    principal: _Writer,
) -> IngestResponse:
    """Ingere o documento e devolve o resultado da indexacao."""
    result = await IngestDocument(container).execute(payload.to_input(), principal)
    return IngestResponse.from_result(result)


@router.get(
    "/documents",
    response_model=Page[DocumentOut],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Lista documentos",
    description=(
        "Pagina os documentos indexados, do mais recente para o mais antigo, filtrando "
        "por colecao e por texto livre em titulo, origem e conteudo. O item traz o "
        "documento inteiro e o `checksum` que sustenta a idempotencia da ingestao; os "
        "trechos e os vetores nunca aparecem em resposta."
    ),
)
async def list_documents(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    collection: Annotated[
        str | None, Query(description="Restringe a uma colecao especifica.")
    ] = None,
    search: Annotated[
        str | None, Query(description="Texto livre buscado em titulo, origem e conteudo.")
    ] = None,
) -> Page[DocumentOut]:
    """Devolve a pagina de documentos no envelope normativo da API."""
    filters = DocumentFilter(
        collection=collection,
        search=search,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListDocuments(container).execute(filters, principal)
    return Page[DocumentOut].from_result(result, DocumentOut.from_domain)


@router.get(
    "/documents/{document_id}",
    response_model=DocumentOut,
    status_code=status.HTTP_200_OK,
    responses=_DOCUMENT_ERRORS,
    summary="Busca um documento",
    description=(
        "Devolve o documento inteiro, com o conteudo normalizado tal como foi "
        "indexado. Documento inexistente responde `404`."
    ),
)
async def get_document(
    container: ContainerDep, principal: _Reader, document_id: _DocumentId
) -> DocumentOut:
    """Devolve o documento pedido."""
    document = await GetDocument(container).execute(document_id, principal)
    return DocumentOut.from_domain(document)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_DOCUMENT_ERRORS,
    summary="Remover documento",
    description=(
        "Apaga o documento e todos os seus trechos do indice vetorial. Operacao "
        "destrutiva e sem corpo de resposta: o que sai do indice para de aparecer nas "
        "buscas imediatamente."
    ),
)
async def delete_document(
    container: ContainerDep, principal: _Writer, document_id: _DocumentId
) -> Response:
    """Remove o documento e responde 204 sem corpo."""
    await DeleteDocument(container).execute(document_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/documents/{document_id}/reindex",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    responses=_INDEX_ERRORS,
    summary="Reindexar documento",
    description=(
        "Recalcula trechos e embeddings de um documento ja gravado, com o embedder "
        "corrente, e substitui os vetores antigos. E o caminho para adotar um novo "
        "modelo de embedding sem reenviar o conteudo — e o unico caminho honesto "
        "depois de trocar `provider`, `model` ou `dimensions`, porque a colecao inteira "
        "precisa voltar ao mesmo espaco semantico."
    ),
)
async def reindex_document(
    container: ContainerDep, principal: _Writer, document_id: _DocumentId
) -> IngestResponse:
    """Reindexa o documento e devolve o resultado da operacao."""
    result = await ReindexDocument(container).execute(document_id, principal)
    return IngestResponse.from_result(result)
