"""Building block de conhecimento: ingestao, indexacao, busca semantica e RAG (SPEC-0007).

O caminho normativo (`Document -> normalizacao -> chunking -> embeddings ->
vector store`), a idempotencia por checksum e a guarda de compatibilidade de
colecao vivem nos casos de uso de `lukato.application.use_cases.knowledge`. Este
modulo apenas traduz o `payload` de um :class:`~lukato.modules.base.ModuleRequest`
naqueles casos de uso, com o `Container` publicado em `ctx.services["container"]`.

A unica logica propria daqui e a **resposta com RAG**. Quando
`payload["answer"]` e verdadeiro, a busca deixa de devolver so os trechos: os
`top_k` melhores viram um contexto numerado e a pergunta e respondida por
`ctx.services["pipeline"].complete(...)`, com instrucao explicita de citar as
fontes pelo numero. Chamar a fachada e o que mantem a chamada dentro da trinca —
`InvokeModule` envolve o `handle` com o guardrail de entrada e o de saida, e
registra o `RunStep` de LLM que alimenta o custo (SPEC-0001 secao 4). Um modulo
que abrisse um cliente proprio de LLM escaparia dos tres.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final

from lukato.application.container import Container
from lukato.application.use_cases.knowledge import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    DeleteDocument,
    DocumentFilter,
    DocumentInput,
    IngestDocument,
    KnowledgeHealth,
    ListCollections,
    ListDocuments,
    ReindexDocument,
    SearchKnowledge,
    SearchQuery,
)
from lukato.application.use_cases.modules import ModulePipeline
from lukato.config import get_logger
from lukato.domain.errors import ValidationError
from lukato.domain.models.knowledge import Document, SearchHit
from lukato.domain.models.module import ModuleBinding, ModuleKind
from lukato.domain.types import Json
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import register_module

__all__ = [
    "CONTAINER_SERVICE",
    "DEFAULT_ACTION",
    "KNOWLEDGE_ACTIONS",
    "PIPELINE_SERVICE",
    "RAG_INSTRUCTION",
    "RAG_MAX_CONTEXT_CHARS",
    "RAG_SNIPPET_CHARS",
    "KnowledgeModule",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
CONTAINER_SERVICE: Final[str] = "container"
"""Chave de `ctx.services` onde a plataforma publica o `Container` da aplicacao."""

PIPELINE_SERVICE: Final[str] = "pipeline"
"""Chave de `ctx.services` com a fachada da trinca (`ModulePipeline`)."""

KNOWLEDGE_ACTIONS: Final[tuple[str, ...]] = (
    "ingest",
    "search",
    "list",
    "delete",
    "reindex",
    "collections",
    "health",
)
"""Acoes aceitas em `payload["action"]`."""

DEFAULT_ACTION: Final[str] = "search"
"""Acao assumida quando o chamador nao informa nenhuma: o uso mais comum e buscar."""

RAG_SNIPPET_CHARS: Final[int] = 1_200
"""Recorte maximo de cada trecho colocado no contexto do RAG."""

RAG_MAX_CONTEXT_CHARS: Final[int] = 8_000
"""Teto do contexto montado para o RAG: protege o `max_tokens` do binding."""

RAG_DEFAULT_TOP_K: Final[int] = 5
"""Trechos usados na resposta quando o chamador nao informa `top_k`."""

RAG_INSTRUCTION: Final[str] = (
    "Responda a pergunta usando exclusivamente os trechos numerados abaixo.\n"
    "Cite as fontes pelo numero entre colchetes, por exemplo [1] ou [1][3].\n"
    "Se os trechos nao contiverem a resposta, diga isso explicitamente e nao invente."
)
"""Instrucao fixa do RAG; o system prompt do binding continua valendo por cima."""

MAX_PAGE_LIMIT: Final[int] = 200
MAX_OFFSET: Final[int] = 1_000_000

_TRUE_WORDS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on", "sim"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off", "nao", "não"})


# ---------------------------------------------------------------------------
# Leitura defensiva do payload
# ---------------------------------------------------------------------------
def _text(payload: Mapping[str, Any], key: str, *, default: str = "") -> str:
    """Le um campo textual do payload, com `default` quando ausente ou em branco."""
    raw = payload.get(key)
    if raw is None:
        return default
    candidate = str(raw).strip()
    return candidate or default


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    """Le um campo textual opcional; ausente ou em branco vira `None`."""
    return _text(payload, key) or None


def _require_text(payload: Mapping[str, Any], key: str, *, action: str) -> str:
    """Le um campo textual obrigatorio da acao."""
    found = _optional_text(payload, key)
    if found is None:
        raise ValidationError(
            f"A acao '{action}' exige o campo '{key}' no payload.",
            details={"action": action, "field": key},
        )
    return found


def _flag(payload: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    """Le um booleano do payload aceitando as formas textuais usuais."""
    raw = payload.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    candidate = str(raw).strip().lower()
    if not candidate:
        return default
    if candidate in _TRUE_WORDS:
        return True
    if candidate in _FALSE_WORDS:
        return False
    raise ValidationError(
        f"Valor booleano invalido em '{key}': {raw!r}.",
        details={"field": key, "value": str(raw)},
    )


def _integer(
    payload: Mapping[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    """Le um inteiro do payload dentro de uma faixa fechada."""
    raw = payload.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Valor inteiro invalido em '{key}': {raw!r}.",
            details={"field": key, "value": str(raw)},
        ) from exc
    return max(minimum, min(value, maximum))


def _mapping(payload: Mapping[str, Any], key: str) -> Json:
    """Le um sub-objeto do payload; ausente vira mapa vazio."""
    raw = payload.get(key)
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return {str(name): value for name, value in raw.items()}
    raise ValidationError(
        f"O campo '{key}' precisa ser um objeto.",
        details={"field": key, "received": type(raw).__name__},
    )


def _action_of(request: ModuleRequest, payload: Mapping[str, Any], *, default: str) -> str:
    """Resolve a acao pedida: `payload["action"]` e, como atalho, `request.input`."""
    candidate = _text(payload, "action")
    if not candidate:
        typed = request.input.strip().lower()
        candidate = typed if typed in KNOWLEDGE_ACTIONS else default
    candidate = candidate.strip().lower()
    if candidate not in KNOWLEDGE_ACTIONS:
        raise ValidationError(
            f"Acao de conhecimento desconhecida: {candidate!r}.",
            details={"action": candidate, "supported": list(KNOWLEDGE_ACTIONS)},
        )
    return candidate


# ---------------------------------------------------------------------------
# Contexto do RAG
# ---------------------------------------------------------------------------
def _hit_label(hit: SearchHit) -> str:
    """Nome legivel da fonte de um trecho, para a citacao no contexto."""
    metadata = hit.metadata or {}
    for key in ("document_title", "title", "source"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return hit.document_id


def _dump_hit(hit: SearchHit) -> Json:
    """Serializa um resultado de busca para a resposta do modulo."""
    return hit.model_dump(mode="json")


def _dump_document(document: Document) -> Json:
    """Serializa um documento para a resposta do modulo."""
    return document.model_dump(mode="json")


def build_context(
    hits: Sequence[SearchHit],
    *,
    top_k: int,
    snippet_chars: int = RAG_SNIPPET_CHARS,
    max_chars: int = RAG_MAX_CONTEXT_CHARS,
) -> tuple[str, list[Json]]:
    """Monta o bloco de trechos numerados do RAG e a lista de fontes citaveis.

    O corte por `max_chars` e por trecho inteiro: um fragmento truncado no meio
    de uma frase seria citado como se fosse a fonte completa.
    """
    blocks: list[str] = []
    sources: list[Json] = []
    used = 0
    for position, hit in enumerate(hits[:top_k], start=1):
        label = _hit_label(hit)
        snippet = hit.content.strip()[:snippet_chars]
        if not snippet:
            continue
        block = f"[{position}] {label}\n{snippet}"
        if blocks and used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
        sources.append(
            {
                "ref": position,
                "chunk_id": hit.chunk_id,
                "document_id": hit.document_id,
                "collection": hit.collection,
                "title": label,
                "score": hit.score,
            }
        )
    return ("\n\n".join(blocks), sources)


def build_rag_prompt(question: str, context: str) -> str:
    """Monta a mensagem de usuario do RAG: instrucao, pergunta e trechos."""
    return f"{RAG_INSTRUCTION}\n\nPergunta:\n{question}\n\nTrechos:\n{context}\n"


# ---------------------------------------------------------------------------
# Building block
# ---------------------------------------------------------------------------
@register_module
class KnowledgeModule(BaseModule):
    """Base de conhecimento: ingestao, chunking, embeddings e busca semantica.

    Despacha por `payload["action"]`: `ingest`, `search`, `list`, `delete`,
    `reindex`, `collections` e `health`. Com `payload["answer"] = true` a busca
    responde a pergunta citando as fontes, pela fachada da trinca.
    """

    kind: ClassVar[ModuleKind] = ModuleKind.KNOWLEDGE
    slug: ClassVar[str] = "knowledge"
    name: ClassVar[str] = "Conhecimento"
    description: ClassVar[str] = (
        "Base de conhecimento com chunking, embeddings, busca semantica e resposta com RAG."
    )
    version: ClassVar[str] = "1.0.0"
    capabilities: ClassVar[tuple[str, ...]] = ("ingest", "chunk", "embed", "semantic_search")
    config_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "default_action": {
                "type": "string",
                "enum": list(KNOWLEDGE_ACTIONS),
                "default": DEFAULT_ACTION,
            },
            "collection": {"type": "string", "default": ""},
            "search_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_LIMIT,
                "default": DEFAULT_SEARCH_LIMIT,
            },
            "rerank": {"type": "boolean", "default": False},
            "answer": {"type": "boolean", "default": False},
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_LIMIT,
                "default": RAG_DEFAULT_TOP_K,
            },
            "use_history": {"type": "boolean", "default": False},
        },
    }
    default_binding: ClassVar[ModuleBinding] = ModuleBinding(timeout_seconds=90.0)

    # -- execucao ----------------------------------------------------------
    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Despacha a acao pedida sobre os casos de uso de conhecimento."""
        container = ctx.service(CONTAINER_SERVICE, Container)
        config = self.validate_config(dict(ctx.definition.config or {}))
        payload: Json = dict(request.payload or {})
        action = _action_of(
            request, payload, default=str(config.get("default_action", DEFAULT_ACTION))
        )

        if action == "ingest":
            return await self._ingest(container, ctx, request, payload, config)
        if action == "search":
            return await self._search(container, ctx, request, payload, config)
        if action == "list":
            return await self._list(container, ctx, payload, config)
        if action == "delete":
            return await self._delete(container, ctx, payload)
        if action == "reindex":
            return await self._reindex(container, ctx, payload)
        if action == "collections":
            return await self._collections(container, ctx)
        return await self._health(container, ctx)

    # -- escrita -----------------------------------------------------------
    async def _ingest(
        self,
        container: Container,
        ctx: ModuleContext,
        request: ModuleRequest,
        payload: Json,
        config: Json,
    ) -> ModuleResponse:
        """`ingest`: grava e indexa um documento; reingestao identica e idempotente."""
        content = _text(payload, "content") or request.input.strip()
        if not content:
            raise ValidationError(
                "A acao 'ingest' exige 'content' no payload ou texto em 'input'.",
                details={"action": "ingest", "field": "content"},
            )
        data_input = DocumentInput(
            title=_require_text(payload, "title", action="ingest"),
            content=content,
            collection=_text(payload, "collection", default=str(config.get("collection", ""))),
            source=_text(payload, "source"),
            metadata=_mapping(payload, "metadata"),
            document_id=_optional_text(payload, "document_id"),
        )
        result = await IngestDocument(container).execute(data_input, ctx.principal)
        data: Json = {"action": "ingest"}
        data.update(result.to_dict())
        output = (
            f"Documento '{result.document.title}' ja estava indexado com o mesmo conteudo: "
            f"{result.chunks} trecho(s) preservado(s)."
            if result.idempotent
            else (
                f"Documento '{result.document.title}' indexado na colecao "
                f"'{result.document.collection}' em {result.chunks} trecho(s)."
            )
        )
        return ModuleResponse(output=output, data=data, metadata={"action": "ingest"})

    async def _reindex(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`reindex`: recalcula chunks e embeddings de um documento ja gravado."""
        document_id = _require_text(payload, "document_id", action="reindex")
        result = await ReindexDocument(container).execute(document_id, ctx.principal)
        data: Json = {"action": "reindex"}
        data.update(result.to_dict())
        output = (
            f"Documento '{result.document.title}' reindexado em {result.chunks} trecho(s) "
            f"com o embedder '{result.fingerprint.model}'."
        )
        return ModuleResponse(output=output, data=data, metadata={"action": "reindex"})

    async def _delete(
        self, container: Container, ctx: ModuleContext, payload: Json
    ) -> ModuleResponse:
        """`delete`: remove um documento e os seus vetores do indice."""
        document_id = _require_text(payload, "document_id", action="delete")
        await DeleteDocument(container).execute(document_id, ctx.principal)
        data: Json = {"action": "delete", "document_id": document_id, "deleted": True}
        return ModuleResponse(
            output=f"Documento '{document_id}' removido da base de conhecimento.",
            data=data,
            metadata={"action": "delete"},
        )

    # -- leitura -----------------------------------------------------------
    async def _list(
        self, container: Container, ctx: ModuleContext, payload: Json, config: Json
    ) -> ModuleResponse:
        """`list`: documentos paginados da colecao."""
        filters = DocumentFilter(
            collection=_optional_text(payload, "collection")
            or (str(config.get("collection", "")) or None),
            search=_optional_text(payload, "search"),
            limit=_integer(payload, "limit", default=50, minimum=1, maximum=MAX_PAGE_LIMIT),
            offset=_integer(payload, "offset", default=0, minimum=0, maximum=MAX_OFFSET),
        )
        page = await ListDocuments(container).execute(filters, ctx.principal)
        data: Json = {"action": "list", "collection": filters.collection}
        data.update(page.to_dict(_dump_document))
        return ModuleResponse(
            output=f"{page.count} de {page.total} documento(s).",
            data=data,
            metadata={"action": "list"},
        )

    async def _collections(self, container: Container, ctx: ModuleContext) -> ModuleResponse:
        """`collections`: colecoes existentes e o embedder que produziu cada uma."""
        items = await ListCollections(container).execute(ctx.principal)
        data: Json = {"action": "collections", "items": items, "total": len(items)}
        return ModuleResponse(
            output=f"{len(items)} colecao(oes) na base de conhecimento.",
            data=data,
            metadata={"action": "collections"},
        )

    async def _health(self, container: Container, ctx: ModuleContext) -> ModuleResponse:
        """`health`: embedder corrente, modo degradado e mapa de colecoes."""
        report = await KnowledgeHealth(container).execute(ctx.principal)
        data: Json = {"action": "health", "health": report}
        state = "degradado" if report.get("degraded") else "ativo"
        output = (
            f"Embedder '{report.get('provider')}' / '{report.get('model')}' "
            f"({report.get('dimensions')} dimensoes) {state}."
        )
        return ModuleResponse(output=output, data=data, metadata={"action": "health"})

    # -- busca e RAG -------------------------------------------------------
    async def _search(
        self,
        container: Container,
        ctx: ModuleContext,
        request: ModuleRequest,
        payload: Json,
        config: Json,
    ) -> ModuleResponse:
        """`search`: busca semantica e, com `answer=true`, resposta citando as fontes."""
        question = _text(payload, "query") or request.input.strip()
        if not question:
            raise ValidationError(
                "A acao 'search' exige 'query' no payload ou texto em 'input'.",
                details={"action": "search", "field": "query"},
            )
        limit = _integer(
            payload,
            "limit",
            default=int(config.get("search_limit", DEFAULT_SEARCH_LIMIT)),
            minimum=1,
            maximum=MAX_SEARCH_LIMIT,
        )
        query = SearchQuery(
            query=question,
            collection=_text(payload, "collection", default=str(config.get("collection", ""))),
            limit=limit,
            filters=_mapping(payload, "filters"),
            rerank=_flag(payload, "rerank", default=bool(config.get("rerank", False))),
        )
        hits = await SearchKnowledge(container).execute(query, ctx.principal)
        data: Json = {
            "action": "search",
            "query": question,
            "collection": query.collection or container.settings.embedding.collection,
            "limit": limit,
            "rerank": query.rerank,
            "items": [_dump_hit(hit) for hit in hits],
            "total": len(hits),
            "answered": False,
        }

        if not _flag(payload, "answer", default=bool(config.get("answer", False))):
            return ModuleResponse(
                output=f"{len(hits)} trecho(s) encontrado(s) para a consulta.",
                data=data,
                metadata={"action": "search", "answered": False},
            )
        return await self._answer(ctx, request, question, hits, payload, config, data=data)

    async def _answer(
        self,
        ctx: ModuleContext,
        request: ModuleRequest,
        question: str,
        hits: Sequence[SearchHit],
        payload: Json,
        config: Json,
        *,
        data: Json,
    ) -> ModuleResponse:
        """Responde a pergunta com os melhores trechos, pela fachada da trinca.

        A chamada passa por `ctx.services["pipeline"]` e nao por um cliente
        proprio: e assim que a resposta continua cercada pelos guardrails e pelo
        system prompt do binding, e que o consumo entra no custo do run.
        """
        top_k = _integer(
            payload,
            "top_k",
            default=int(config.get("top_k", RAG_DEFAULT_TOP_K)),
            minimum=1,
            maximum=MAX_SEARCH_LIMIT,
        )
        context, sources = build_context(hits, top_k=top_k)
        if not context:
            data["answer"] = {
                "grounded": False,
                "sources": [],
                "reason": "a busca nao devolveu nenhum trecho para fundamentar a resposta",
            }
            return ModuleResponse(
                output=(
                    "Nao encontrei nenhum trecho na base de conhecimento para responder "
                    "a essa pergunta."
                ),
                data=data,
                metadata={"action": "search", "answered": False, "grounded": False},
            )

        use_history = _flag(payload, "use_history", default=bool(config.get("use_history", False)))
        history = list(request.history) if use_history else []
        pipeline = ctx.service(PIPELINE_SERVICE, ModulePipeline)
        response = await pipeline.complete(build_rag_prompt(question, context), history=history)
        data["answered"] = True
        data["answer"] = {
            "grounded": True,
            "sources": sources,
            "context_chars": len(context),
            "top_k": len(sources),
            "history_messages": len(history),
            "model": response.model,
            "finish_reason": response.finish_reason,
            "usage": response.usage.model_dump(mode="json"),
        }
        _logger.info(
            "knowledge_rag_answer",
            collection=data["collection"],
            hits=len(hits),
            sources=len(sources),
            context_chars=len(context),
            model=response.model,
        )
        return ModuleResponse(
            output=response.content,
            data=data,
            usage=response.usage,
            metadata={"action": "search", "answered": True, "grounded": True},
        )

    # -- presenca na plataforma -------------------------------------------
    def ui(self) -> UIDescriptor:
        """Publica o item Conhecimento na secao FUNCIONALIDADE (SPEC-0009 secao 4)."""
        return UIDescriptor(
            nav=[
                UINavItem(
                    label="Conhecimento",
                    icon="book",
                    endpoint="/knowledge",
                    section="FUNCIONALIDADE",
                    order=30,
                )
            ],
            center_template="pages/knowledge.html",
            context_template="context/document.html",
        )

    def health(self) -> Json:
        """Resumo de saude com as acoes efetivamente atendidas."""
        report = super().health()
        report["actions"] = list(KNOWLEDGE_ACTIONS)
        report["rag"] = True
        return report
