"""Rotas de pagina do console web (SPEC-0009 secao 8).

Este modulo e a borda de apresentacao do lukato: cada rota resolve o que a tela
precisa **atraves de casos de uso**, monta o contexto com
:func:`~lukato.interfaces.ui.context.base_context` e devolve HTML renderizado no
servidor. Nenhuma rota abre repositorio, nenhuma chama adaptador, nenhuma decide
regra de negocio.

Tres compromissos estruturam o arquivo:

* **Uma pagina nunca devolve JSON.** Qualquer falha — dominio, template ausente,
  defeito inesperado — vira `pages/error.html` com o status certo. O ultimo
  recurso, quando ate o template de erro falta, e :data:`FALLBACK_ERROR_HTML`:
  um documento minimo, escapado, que ainda diz o que aconteceu.
* **O painel de contexto funciona sem JavaScript.** `?sel=<id>` e resolvido no
  servidor por `base_context`; `GET /ui/context/{entity}/{id}` devolve o mesmo
  fragmento para quem tem JavaScript. As duas rotas compartilham o carregador.
* **Offline-first.** O CSS, o JS, as fontes e os icones saem de `static/`. A
  politica de seguranca desta borda (:data:`CSP_POLICY`) so autoriza a propria
  origem e o script de boot inline, identificado por hash SHA-256 — nao por
  `'unsafe-inline'`, que valeria para qualquer script injetado.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from html import escape
from pathlib import Path
from typing import Any, Final, TypeVar

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from lukato.application.container import Container
from lukato.application.dto import ModuleFilter, RunFilter
from lukato.application.use_cases.adwatch import (
    CommercialFilter,
    DetectionFilter,
    GetMediaCapabilities,
    ListCommercials,
    ListDetections,
    ListMedia,
    MediaFilter,
)
from lukato.application.use_cases.finops import (
    BUCKET_DAY,
    BUCKET_HOUR,
    BudgetFilter,
    CostFilter,
    GetCostSeries,
    GetCostSummary,
    GetPrices,
    ListBudgets,
    SeriesRequest,
)
from lukato.application.use_cases.guardrails import (
    ListPolicies,
    ListRuleKinds,
    PolicyFilter,
)
from lukato.application.use_cases.identity import (
    ApiKeyFilter,
    ListApiKeys,
    ListUsers,
    UserFilter,
)
from lukato.application.use_cases.knowledge import (
    DocumentFilter,
    KnowledgeHealth,
    ListCollections,
    ListDocuments,
)
from lukato.application.use_cases.modules import GetModule, ListModules
from lukato.application.use_cases.prompts import ListPrompts, PromptFilter
from lukato.application.use_cases.runs import GetRun, GetRunSteps, ListRuns
from lukato.config import get_logger
from lukato.domain.errors import LukatoError
from lukato.domain.models.adwatch import DetectionStatus
from lukato.domain.models.guardrail import GuardrailStage
from lukato.domain.models.identity import Role
from lukato.domain.models.module import ModuleKind, ModuleStatus
from lukato.domain.models.run import RunStatus
from lukato.domain.types import Json, utcnow
from lukato.interfaces.http.deps import ContainerDep, PrincipalDep
from lukato.interfaces.ui.context import (
    Crumb,
    CrumbLike,
    base_context,
    context_template_for,
    load_entity,
)
from lukato.interfaces.ui.filters import register_filters

__all__ = [
    "BOOT_SCRIPT",
    "CSP_POLICY",
    "DEFAULT_PAGE_SIZE",
    "FALLBACK_ERROR_HTML",
    "STATIC_DIR",
    "STATIC_URL",
    "TEMPLATES_DIR",
    "mount_static",
    "router",
    "templates",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

_HERE: Final[Path] = Path(__file__).resolve().parent

TEMPLATES_DIR: Final[Path] = _HERE / "templates"
"""Raiz dos templates Jinja do console."""

STATIC_DIR: Final[Path] = _HERE / "static"
"""Raiz dos ativos estaticos; nada e servido de fora daqui (requisito offline-first)."""

STATIC_URL: Final[str] = "/static"
"""Prefixo publico dos ativos estaticos."""

DEFAULT_PAGE_SIZE: Final[int] = 25
"""Tamanho de pagina das listagens do console."""

MAX_PAGE_SIZE: Final[int] = 200
"""Teto de itens por pagina aceito pela borda."""

RECENT_RUNS: Final[int] = 8
"""Quantidade de execucoes recentes mostradas no cockpit."""

OPTION_LIMIT: Final[int] = 100
"""Teto de itens carregados para preencher um `<select>` de filtro."""

BOOT_SCRIPT: Final[str] = (
    "(function(){try{var r=document.documentElement,s=localStorage;"
    'var v=s.getItem("lukato.sidebar");'
    'r.setAttribute("data-sidebar",v==="collapsed"?"collapsed":"expanded");'
    'var t=s.getItem("lukato.theme");'
    'if(t==="dark"||t==="light"){r.setAttribute("data-theme",t);}'
    'var a=s.getItem("lukato.aside");'
    'r.setAttribute("data-aside",a==="closed"?"closed":"open");'
    "}catch(e){}})();"
)
"""Script inline do `<head>`: restaura sidebar, tema e gaveta **antes** do primeiro paint.

Precisa ser inline e sincrono. Um arquivo externo, mesmo local, so executa depois
de uma ida ao servidor: a sidebar apareceria expandida e saltaria para recolhida,
que e exatamente o flash que a SPEC-0009 secao 3 proibe.
"""


def _script_hash(source: str) -> str:
    """Hash CSP (`sha256-...`) do conteudo de um script inline."""
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return f"sha256-{base64.b64encode(digest).decode('ascii')}"


CSP_POLICY: Final[str] = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    f"script-src 'self' '{_script_hash(BOOT_SCRIPT)}'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)
"""CSP do console: nada de fora da origem, e o boot inline liberado por hash.

O middleware global usa `setdefault`, entao esta politica — mais estrita quanto a
`connect-src`/`font-src` e mais precisa quanto ao script inline — prevalece nas
respostas da UI sem alterar a da API.
"""

FALLBACK_ERROR_HTML: Final[str] = (
    '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
    "<title>lukato — erro</title></head><body>"
    "<h1>Nao foi possivel renderizar esta pagina</h1><p>{message}</p>"
    '<p><a href="/">Voltar ao cockpit</a></p></body></html>'
)
"""Documento de ultimo recurso, usado apenas se ate `pages/error.html` faltar."""


# ---------------------------------------------------------------------------
# Ambiente Jinja
# ---------------------------------------------------------------------------
templates: Final[Jinja2Templates] = Jinja2Templates(directory=str(TEMPLATES_DIR))
"""Ambiente Jinja do console, com autoescape ligado e os filtros pt-BR registrados."""


def _template_exists(name: str) -> bool:
    """True quando o template existe no loader — usado pelos `include` opcionais."""
    try:
        templates.env.get_template(name)
    except Exception:
        return False
    return True


register_filters(templates.env)
templates.env.globals["template_exists"] = _template_exists
templates.env.globals["boot_script"] = BOOT_SCRIPT
templates.env.globals["static_url"] = STATIC_URL


def mount_static(app: Any) -> None:
    """Monta `static/` em `/static`; chamada pelo composition root.

    Fica aqui, e nao no composition root, porque o caminho dos ativos e um detalhe
    desta borda: se o pacote da UI mudar de lugar, so este arquivo precisa saber.
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount(
        STATIC_URL,
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="lukato-static",
    )


# ---------------------------------------------------------------------------
# Renderizacao
# ---------------------------------------------------------------------------
router = APIRouter(tags=["console"], include_in_schema=False)
"""Rotas de pagina do console, montadas na raiz pelo composition root."""


def _render(
    request: Request, template: str, context: Json, *, status_code: int = 200
) -> HTMLResponse:
    """Renderiza um template com a politica de seguranca do console aplicada."""
    response = templates.TemplateResponse(
        request=request, name=template, context=context, status_code=status_code
    )
    response.headers["Content-Security-Policy"] = CSP_POLICY
    response.headers["Cache-Control"] = "no-store"
    return response


async def _error_page(
    request: Request,
    container: Container,
    *,
    active: str,
    code: str,
    message: str,
    status_code: int,
    details: Json | None = None,
) -> HTMLResponse:
    """Renderiza `pages/error.html`; cai no documento minimo se ele nao existir."""
    try:
        context = await base_context(request, container, active=active, breadcrumb=[Crumb("Erro")])
    except Exception:  # pragma: no cover - moldura indisponivel
        context = {"active_route": active, "breadcrumb": [Crumb("Erro")]}
    context.update(
        {
            "page_title": "Erro",
            "error_code": code,
            "error_message": message,
            "error_details": details or {},
            "error_status": status_code,
        }
    )
    if _template_exists("pages/error.html"):
        return _render(request, "pages/error.html", context, status_code=status_code)
    return HTMLResponse(
        FALLBACK_ERROR_HTML.format(message=escape(message)),
        status_code=status_code,
        headers={"Content-Security-Policy": CSP_POLICY, "Cache-Control": "no-store"},
    )


async def _page(
    request: Request,
    container: Container,
    *,
    template: str,
    active: str,
    title: str,
    breadcrumb: Sequence[CrumbLike],
    build: Callable[[], Awaitable[Json]] | None = None,
    selected_id: str | None = None,
) -> HTMLResponse:
    """Executa a consulta da pagina, monta o contexto e renderiza — ou mostra o erro.

    O contrato com `templates/pages/` esta em `base.html`: esta funcao entrega
    todas as chaves de `base_context` mais `page_title` e o que `build` devolver.
    """
    try:
        extra = await build() if build is not None else {}
        context = await base_context(
            request,
            container,
            active=active,
            breadcrumb=breadcrumb,
            selected_id=selected_id,
        )
        context.update(extra)
        context["page_title"] = title
        return _render(request, template, context)
    except LukatoError as exc:
        _logger.info("ui_page_error", route=active, code=exc.code, message=exc.message)
        return await _error_page(
            request,
            container,
            active=active,
            code=exc.code,
            message=exc.message,
            status_code=exc.http_status,
            details=exc.details,
        )
    except Exception as exc:
        _logger.exception("ui_page_failed", route=active, error=f"{type(exc).__name__}: {exc}")
        return await _error_page(
            request,
            container,
            active=active,
            code="internal_error",
            message="Nao foi possivel montar esta pagina. O detalhe foi registrado no log.",
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Coercao tolerante de parametros de consulta
# ---------------------------------------------------------------------------
def _enum(enum_type: type[_T], value: str | None) -> _T | None:
    """Converte texto em membro do enum; valor invalido vira `None`, nunca 422.

    Um filtro digitado errado na barra de endereco deve mostrar a lista sem
    filtro, e nao uma pagina de erro.
    """
    raw = (value or "").strip().lower()
    if not raw:
        return None
    try:
        return enum_type(raw)  # type: ignore[call-arg]
    except ValueError:
        return None


def _text(value: str | None) -> str | None:
    """Normaliza um campo de busca: vazio vira `None`."""
    cleaned = (value or "").strip()
    return cleaned or None


def _window(limit: int, offset: int) -> tuple[int, int]:
    """Normaliza a janela de paginacao dentro dos limites da plataforma."""
    return max(1, min(int(limit), MAX_PAGE_SIZE)), max(0, int(offset))


def _tribool(value: str | None) -> bool | None:
    """Le um filtro ternario (`sim`/`nao`/vazio) vindo de um `<select>`."""
    raw = (value or "").strip().lower()
    if raw in {"1", "true", "sim", "ativo", "ativa"}:
        return True
    if raw in {"0", "false", "nao", "inativo", "inativa"}:
        return False
    return None


_LimitQuery = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Itens por pagina.")
_OffsetQuery = Query(0, ge=0, description="Itens a pular antes da pagina.")
_SelQuery = Query(None, alias="sel", description="Item aberto no painel de contexto.")
_SearchQuery = Query(None, alias="q", description="Busca textual.")


# ---------------------------------------------------------------------------
# Cockpit
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse, summary="Cockpit")
async def cockpit(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Visao geral da instalacao: modulos, execucoes, custo e saude."""

    async def build() -> Json:
        since = utcnow() - timedelta(hours=24)
        modules = await ListModules(container).execute(ModuleFilter(limit=1), principal)
        active_modules = await ListModules(container).execute(
            ModuleFilter(status=ModuleStatus.ACTIVE, limit=1), principal
        )
        recent = await ListRuns(container).execute(
            RunFilter(since=since, limit=RECENT_RUNS), principal
        )
        blocked = await ListRuns(container).execute(
            RunFilter(since=since, status=RunStatus.BLOCKED, limit=1), principal
        )
        failed = await ListRuns(container).execute(
            RunFilter(since=since, status=RunStatus.FAILED, limit=1), principal
        )
        series = await GetCostSeries(container).execute(
            SeriesRequest(bucket=BUCKET_HOUR, since=since), principal
        )
        return {
            "total_modules": modules.total,
            "active_modules": active_modules.total,
            "runs_24h": recent.total,
            "blocked_24h": blocked.total,
            "failed_24h": failed.total,
            "recent_runs": recent.items,
            "series": series,
        }

    return await _page(
        request,
        container,
        template="pages/cockpit.html",
        active="/",
        title="Cockpit",
        breadcrumb=[Crumb("Cockpit")],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# Modulos
# ---------------------------------------------------------------------------
@router.get("/modules", response_class=HTMLResponse, summary="Modulos")
async def modules_list(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    q: str | None = _SearchQuery,
    kind: str | None = Query(None, description="Filtra pelo tipo do building block."),
    status: str | None = Query(None, description="Filtra pelo ciclo de vida."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Catalogo de definicoes de modulo, com filtros e paginacao."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        filters = ModuleFilter(
            kind=_enum(ModuleKind, kind),
            status=_enum(ModuleStatus, status),
            search=_text(q),
            limit=page_limit,
            offset=page_offset,
        )
        modules = await ListModules(container).execute(filters, principal)
        return {
            "modules": modules,
            "filters": {"q": _text(q) or "", "kind": kind or "", "status": status or ""},
            "kinds": list(ModuleKind),
            "statuses": list(ModuleStatus),
            "descriptors": container.registry.describe(),
        }

    return await _page(
        request,
        container,
        template="pages/modules_list.html",
        active="/modules",
        title="Módulos",
        breadcrumb=[Crumb("Módulos")],
        build=build,
        selected_id=sel,
    )


@router.get("/modules/{slug}", response_class=HTMLResponse, summary="Operacao do modulo")
async def modules_detail(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    slug: str = PathParam(description="Slug ou id da definicao de modulo."),
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Tela de operacao de um modulo: invocacao, binding e historico recente."""

    async def build() -> Json:
        module = await GetModule(container).execute(slug, principal)
        prompts = await ListPrompts(container).execute(
            PromptFilter(is_active=True, limit=OPTION_LIMIT), principal
        )
        policies = await ListPolicies(container).execute(
            PolicyFilter(is_active=True, limit=OPTION_LIMIT), principal
        )
        runs = await ListRuns(container).execute(
            RunFilter(module_slug=module.slug, limit=RECENT_RUNS), principal
        )
        descriptors = {item.slug: item for item in container.registry.describe()}
        return {
            "module": module,
            "descriptor": descriptors.get(module.slug),
            "prompts": prompts.items,
            "policies": policies.items,
            "input_policies": [
                item for item in policies.items if item.stage == GuardrailStage.INPUT
            ],
            "output_policies": [
                item for item in policies.items if item.stage == GuardrailStage.OUTPUT
            ],
            "runs": runs,
            "runtimes": container.runtimes,
        }

    return await _page(
        request,
        container,
        template="pages/modules_detail.html",
        active="/modules/{slug}",
        title=f"Módulo {slug}",
        breadcrumb=[Crumb("Módulos", "/modules"), Crumb(slug)],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# Prompts e guardrails
# ---------------------------------------------------------------------------
@router.get("/prompts", response_class=HTMLResponse, summary="Prompts")
async def prompts_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    q: str | None = _SearchQuery,
    active: str | None = Query(None, description="Filtra por versoes ativas."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Biblioteca de prompts versionados, com preview de renderizacao."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        filters = PromptFilter(
            search=_text(q),
            is_active=_tribool(active),
            limit=page_limit,
            offset=page_offset,
        )
        prompts = await ListPrompts(container).execute(filters, principal)
        return {
            "prompts": prompts,
            "filters": {"q": _text(q) or "", "active": active or ""},
        }

    return await _page(
        request,
        container,
        template="pages/prompts.html",
        active="/prompts",
        title="Prompts",
        breadcrumb=[Crumb("Prompts")],
        build=build,
        selected_id=sel,
    )


@router.get("/guardrails", response_class=HTMLResponse, summary="Guardrails")
async def guardrails_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    q: str | None = _SearchQuery,
    stage: str | None = Query(None, description="Filtra pelo estagio da politica."),
    active: str | None = Query(None, description="Filtra por politicas ativas."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Politicas de guardrail, editor de regras e testador de veredito."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        filters = PolicyFilter(
            stage=_enum(GuardrailStage, stage),
            is_active=_tribool(active),
            search=_text(q),
            limit=page_limit,
            offset=page_offset,
        )
        policies = await ListPolicies(container).execute(filters, principal)
        return {
            "policies": policies,
            "rule_kinds": await ListRuleKinds(container).execute(principal),
            "stages": list(GuardrailStage),
            "filters": {"q": _text(q) or "", "stage": stage or "", "active": active or ""},
        }

    return await _page(
        request,
        container,
        template="pages/guardrails.html",
        active="/guardrails",
        title="Guardrails",
        breadcrumb=[Crumb("Guardrails")],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# Execucoes
# ---------------------------------------------------------------------------
@router.get("/runs", response_class=HTMLResponse, summary="Execucoes")
async def runs_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    module: str | None = Query(None, description="Filtra pelo slug do modulo."),
    status: str | None = Query(None, description="Filtra pelo desfecho."),
    hours: int = Query(24, ge=1, le=720, description="Janela em horas."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Historico paginado de execucoes, com filtros de modulo, desfecho e janela."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        filters = RunFilter(
            module_slug=_text(module),
            status=_enum(RunStatus, status),
            since=utcnow() - timedelta(hours=hours),
            limit=page_limit,
            offset=page_offset,
        )
        runs = await ListRuns(container).execute(filters, principal)
        modules = await ListModules(container).execute(ModuleFilter(limit=OPTION_LIMIT), principal)
        return {
            "runs": runs,
            "statuses": list(RunStatus),
            "module_options": modules.items,
            "filters": {
                "module": module or "",
                "status": status or "",
                "hours": hours,
            },
        }

    return await _page(
        request,
        container,
        template="pages/runs.html",
        active="/runs",
        title="Execuções",
        breadcrumb=[Crumb("Execuções")],
        build=build,
        selected_id=sel,
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse, summary="Detalhe da execucao")
async def run_detail(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    run_id: str = PathParam(description="Identificador da execucao."),
) -> HTMLResponse:
    """Trilha completa de uma execucao: passos, consumo, custo e trace."""

    async def build() -> Json:
        run = await GetRun(container).execute(run_id, principal)
        steps = await GetRunSteps(container).execute(run_id, principal)
        return {"run": run, "steps": steps}

    return await _page(
        request,
        container,
        template="pages/run_detail.html",
        active="/runs/{run_id}",
        title=f"Execução {run_id}",
        breadcrumb=[Crumb("Execuções", "/runs"), Crumb(run_id)],
        build=build,
        selected_id=run_id,
    )


# ---------------------------------------------------------------------------
# Conhecimento
# ---------------------------------------------------------------------------
@router.get("/knowledge", response_class=HTMLResponse, summary="Conhecimento")
async def knowledge_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    q: str | None = _SearchQuery,
    collection: str | None = Query(None, description="Filtra pela colecao."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Colecoes, ingestao de documentos e busca semantica."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        filters = DocumentFilter(
            collection=_text(collection),
            search=_text(q),
            limit=page_limit,
            offset=page_offset,
        )
        documents = await ListDocuments(container).execute(filters, principal)
        return {
            "documents": documents,
            "collections": await ListCollections(container).execute(principal),
            "knowledge_health": await KnowledgeHealth(container).execute(principal),
            "default_collection": container.settings.embedding.collection,
            "filters": {"q": _text(q) or "", "collection": collection or ""},
        }

    return await _page(
        request,
        container,
        template="pages/knowledge.html",
        active="/knowledge",
        title="Conhecimento",
        breadcrumb=[Crumb("Conhecimento")],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# FinOps e observabilidade
# ---------------------------------------------------------------------------
@router.get("/finops", response_class=HTMLResponse, summary="FinOps")
async def finops_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    hours: int = Query(24, ge=1, le=8760, description="Janela do resumo, em horas."),
    bucket: str = Query(BUCKET_HOUR, description="Granularidade da serie (`hour` ou `day`)."),
    module: str | None = Query(None, description="Recorta por modulo."),
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Resumo de custo, serie temporal, orcamentos e tabela de precos."""

    async def build() -> Json:
        since = utcnow() - timedelta(hours=hours)
        granularity = bucket if bucket in (BUCKET_HOUR, BUCKET_DAY) else BUCKET_HOUR
        summary = await GetCostSummary(container).execute(
            CostFilter(since=since, module_slug=_text(module)), principal
        )
        series = await GetCostSeries(container).execute(
            SeriesRequest(bucket=granularity, since=since, module_slug=_text(module)),
            principal,
        )
        budgets = await ListBudgets(container).execute(BudgetFilter(), principal)
        prices = await GetPrices(container).execute(principal)
        return {
            "summary": summary,
            "series": series,
            "budgets": budgets,
            "prices": prices,
            "buckets": (BUCKET_HOUR, BUCKET_DAY),
            "filters": {"hours": hours, "bucket": granularity, "module": module or ""},
        }

    return await _page(
        request,
        container,
        template="pages/finops.html",
        active="/finops",
        title="FinOps",
        breadcrumb=[Crumb("FinOps")],
        build=build,
        selected_id=sel,
    )


@router.get("/observability", response_class=HTMLResponse, summary="Observabilidade")
async def observability_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    limit: int = _LimitQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Estado do tracing, ultimos traces emitidos e configuracao de telemetria."""
    page_limit, _ = _window(limit, 0)

    async def build() -> Json:
        observability = container.settings.observability
        runs = await ListRuns(container).execute(RunFilter(limit=page_limit), principal)
        traced = [item for item in runs.items if item.trace_id]
        return {
            "runs": runs,
            "traced_runs": traced,
            "tracing": {
                "enabled": bool(getattr(container.tracer, "enabled", False)),
                "langfuse_enabled": observability.langfuse_enabled,
                "langfuse_host": observability.langfuse_host,
                "log_level": observability.log_level,
                "log_json": observability.log_json,
                "metrics_enabled": observability.metrics_enabled,
                "traced_ratio": (len(traced) / len(runs.items)) if runs.items else 0.0,
            },
        }

    return await _page(
        request,
        container,
        template="pages/observability.html",
        active="/observability",
        title="Observabilidade",
        breadcrumb=[Crumb("Observabilidade")],
        build=build,
        selected_id=sel,
    )


@router.get("/registry", response_class=HTMLResponse, summary="Registry")
async def registry_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Building blocks descobertos, capacidades e schema de configuracao."""

    async def build() -> Json:
        descriptors = container.registry.describe()
        tools = container.tools.describe() if container.tools is not None else []
        return {
            "descriptors": descriptors,
            "discover_errors": list(container.registry.discover_errors),
            "runtimes": container.runtimes,
            "tools": tools,
            "principal_role": principal.role,
        }

    return await _page(
        request,
        container,
        template="pages/registry.html",
        active="/registry",
        title="Registry",
        breadcrumb=[Crumb("Registry")],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# AdWatch
# ---------------------------------------------------------------------------
@router.get("/adwatch", response_class=HTMLResponse, summary="AdWatch")
async def adwatch_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    q: str | None = _SearchQuery,
    status: str | None = Query(None, description="Filtra pelo estagio da midia."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Pipeline do AdWatch: registrar midia, importar transcricao, detectar."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        media = await ListMedia(container).execute(
            MediaFilter(
                status=_text(status), search=_text(q), limit=page_limit, offset=page_offset
            ),
            principal,
        )
        commercials = await ListCommercials(container).execute(CommercialFilter(limit=1), principal)
        detections = await ListDetections(container).execute(DetectionFilter(limit=1), principal)
        return {
            "media": media,
            "capabilities": await GetMediaCapabilities(container).execute(principal),
            "commercials_total": commercials.total,
            "detections_total": detections.total,
            "filters": {"q": _text(q) or "", "status": status or ""},
        }

    return await _page(
        request,
        container,
        template="pages/adwatch.html",
        active="/adwatch",
        title="AdWatch",
        breadcrumb=[Crumb("AdWatch")],
        build=build,
        selected_id=sel,
    )


@router.get("/adwatch/commercials", response_class=HTMLResponse, summary="Catalogo de comerciais")
async def adwatch_commercials(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    q: str | None = _SearchQuery,
    brand: str | None = Query(None, description="Filtra pela marca."),
    campaign: str | None = Query(None, description="Filtra pela campanha."),
    active: str | None = Query(None, description="Filtra por comerciais ativos."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """CRUD do catalogo de comerciais monitorados."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        commercials = await ListCommercials(container).execute(
            CommercialFilter(
                search=_text(q),
                brand=_text(brand),
                campaign=_text(campaign),
                is_active=_tribool(active),
                limit=page_limit,
                offset=page_offset,
            ),
            principal,
        )
        return {
            "commercials": commercials,
            "filters": {
                "q": _text(q) or "",
                "brand": brand or "",
                "campaign": campaign or "",
                "active": active or "",
            },
        }

    return await _page(
        request,
        container,
        template="pages/adwatch_commercials.html",
        active="/adwatch/commercials",
        title="Comerciais",
        breadcrumb=[Crumb("AdWatch", "/adwatch"), Crumb("Comerciais")],
        build=build,
        selected_id=sel,
    )


@router.get("/adwatch/detections", response_class=HTMLResponse, summary="Deteccoes")
async def adwatch_detections(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    media: str | None = Query(None, description="Filtra por ativo de midia."),
    commercial: str | None = Query(None, description="Filtra por comercial."),
    status: str | None = Query(None, description="Filtra pelo desfecho da fusao."),
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Deteccoes com evidencias, scores e linha do tempo."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        detections = await ListDetections(container).execute(
            DetectionFilter(
                media_id=_text(media),
                commercial_id=_text(commercial),
                status=_enum(DetectionStatus, status),
                limit=page_limit,
                offset=page_offset,
            ),
            principal,
        )
        media_options = await ListMedia(container).execute(
            MediaFilter(limit=OPTION_LIMIT), principal
        )
        commercial_options = await ListCommercials(container).execute(
            CommercialFilter(limit=OPTION_LIMIT), principal
        )
        return {
            "detections": detections,
            "statuses": list(DetectionStatus),
            "media_options": media_options.items,
            "commercial_options": commercial_options.items,
            "thresholds": {
                "accept": container.settings.adwatch.accept_threshold,
                "review": container.settings.adwatch.review_threshold,
            },
            "filters": {
                "media": media or "",
                "commercial": commercial or "",
                "status": status or "",
            },
        }

    return await _page(
        request,
        container,
        template="pages/adwatch_detections.html",
        active="/adwatch/detections",
        title="Detecções",
        breadcrumb=[Crumb("AdWatch", "/adwatch"), Crumb("Detecções")],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# Identidade e configuracoes
# ---------------------------------------------------------------------------
@router.get("/identity", response_class=HTMLResponse, summary="Identidade")
async def identity_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    limit: int = _LimitQuery,
    offset: int = _OffsetQuery,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Usuarios, papeis e chaves de API — nenhum segredo e exibido."""
    page_limit, page_offset = _window(limit, offset)

    async def build() -> Json:
        users = await ListUsers(container).execute(
            UserFilter(limit=page_limit, offset=page_offset), principal
        )
        api_keys = await ListApiKeys(container).execute(
            ApiKeyFilter(limit=page_limit, offset=page_offset), principal
        )
        return {"users": users, "api_keys": api_keys, "roles": list(Role)}

    return await _page(
        request,
        container,
        template="pages/identity.html",
        active="/identity",
        title="Identidade",
        breadcrumb=[Crumb("Identidade")],
        build=build,
        selected_id=sel,
    )


@router.get("/settings", response_class=HTMLResponse, summary="Configuracoes")
async def settings_page(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    sel: str | None = _SelQuery,
) -> HTMLResponse:
    """Configuracao efetiva da instalacao, com todo segredo mascarado."""

    async def build() -> Json:
        return {
            "runtimes": container.runtimes,
            "registered_modules": len(container.registry),
            "discover_errors": list(container.registry.discover_errors),
            "principal_role": principal.role,
        }

    return await _page(
        request,
        container,
        template="pages/settings.html",
        active="/settings",
        title="Configurações",
        breadcrumb=[Crumb("Configurações")],
        build=build,
        selected_id=sel,
    )


# ---------------------------------------------------------------------------
# Fragmento do painel de contexto
# ---------------------------------------------------------------------------
@router.get(
    "/ui/context/{entity}/{item_id}",
    response_class=HTMLResponse,
    summary="Fragmento do painel de contexto",
)
async def context_fragment(
    request: Request,
    container: ContainerDep,
    principal: PrincipalDep,
    entity: str = PathParam(description="Entidade: module, prompt, guardrail, run, ..."),
    item_id: str = PathParam(description="Identificador do item selecionado."),
) -> HTMLResponse:
    """Devolve **apenas** o miolo do painel direito, para troca por `fetch`.

    A mesma resolucao acontece no servidor quando a pagina e pedida com
    `?sel=<id>`: com ou sem JavaScript, o usuario ve o mesmo painel.
    """
    template = context_template_for(entity)
    context: Json = {
        "request": request,
        "principal": principal,
        "selected_entity": entity,
        "selected_id": item_id,
        "context_template": template,
        "fragment": True,
        "version": container.settings.app.version,
    }
    try:
        context["context_item"] = await load_entity(container, entity, item_id, principal)
    except Exception as exc:  # pragma: no cover - carregador ja degrada internamente
        _logger.warning("ui_fragment_failed", entity=entity, error=f"{type(exc).__name__}: {exc}")
        context["context_item"] = None

    name = template if _template_exists(template) else "partials/context_panel.html"
    return _render(request, name, context)
