"""Entrypoint ASGI do lukato: `uvicorn lukato.main:app`.

Este modulo faz uma coisa so — montar a aplicacao FastAPI a partir do
*composition root*. Nenhuma regra de negocio mora aqui: rotas vivem em
`interfaces/`, dependencias em `composition.py`, comportamento em
`application/`.

O ciclo de vida e o contrato de operacao (SPEC-0001 secao 6):

```text
startup   configure_logging -> build_container -> app.state.container
serve     ... requisicoes ...
shutdown  dispose_container (teardown de modulos, flush do tracer, pool fechado)
```

Uma falha ao montar o container **derruba o boot**. E deliberado: uma replica que
sobe sem `app.state.container` responde `500` em toda rota de negocio e continua
recebendo trafego do balanceador, porque `/healthz` e uma constante. Melhor nao
subir e deixar o `RollingUpdate` manter a versao anterior no ar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, TypeAlias
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse

from lukato import __version__
from lukato.composition import build_container, dispose_container
from lukato.config import Settings, configure_logging, get_logger, get_settings
from lukato.domain.errors import ConfigurationError
from lukato.interfaces.http.api.v1 import api_router
from lukato.interfaces.http.api.v1.routers.health import root_router
from lukato.interfaces.http.console_forms import ConsoleFormMiddleware
from lukato.interfaces.http.errors import install_error_handlers
from lukato.interfaces.http.middleware import install_middlewares
from lukato.interfaces.http.openapi import API_TITLE, customize_openapi
from lukato.interfaces.ui import mount_static
from lukato.interfaces.ui import router as ui_router

__all__ = [
    "CORS_HEADERS",
    "CORS_METHODS",
    "DOCS_URL",
    "OPENAPI_URL",
    "REDOC_URL",
    "app",
    "create_app",
]

_logger = get_logger(__name__)

Lifespan: TypeAlias = Callable[[FastAPI], AbstractAsyncContextManager[None]]
"""Assinatura do gerenciador de ciclo de vida aceito pelo FastAPI."""

DOCS_URL: Final[str] = "/api/docs"
"""Swagger UI (SPEC-0000 secao 11)."""

REDOC_URL: Final[str] = "/api/redoc"
"""ReDoc, a leitura narrativa do mesmo contrato."""

OPENAPI_URL: Final[str] = "/api/openapi.json"
"""Documento OpenAPI 3.1 servido para clientes e geradores."""

CORS_METHODS: Final[tuple[str, ...]] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
"""Verbos liberados na politica de origem cruzada."""

CORS_HEADERS: Final[tuple[str, ...]] = (
    "Authorization",
    "Content-Type",
    "X-API-Key",
    "X-Request-ID",
)
"""Cabecalhos que um cliente de outra origem pode enviar."""

CORS_EXPOSE: Final[tuple[str, ...]] = ("X-Request-ID", "X-Trace-Id", "X-Response-Time-ms")
"""Cabecalhos que o navegador pode ler da resposta (correlacao e latencia)."""


def _lifespan(settings: Settings) -> Lifespan:
    """Cria o `lifespan` da aplicacao amarrado a estas `Settings`.

    E uma fabrica, e nao um `lifespan` global, para que `create_app(settings)`
    consiga montar duas aplicacoes com configuracoes diferentes no mesmo processo
    — exatamente o que a suite de testes precisa fazer.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Monta o container no startup e o desmonta no shutdown."""
        configure_logging(
            level=settings.observability.log_level,
            json_logs=settings.observability.log_json,
            service=settings.app.name,
        )
        _logger.info(
            "application_starting",
            service=settings.app.name,
            version=settings.app.version,
            environment=settings.app.env,
            root_path=settings.app.root_path or "/",
        )
        try:
            container, engine = await build_container(settings)
        except Exception as exc:
            _logger.error(
                "container_build_failed",
                error=f"{type(exc).__name__}: {exc}",
                environment=settings.app.env,
            )
            raise ConfigurationError(
                "Falha ao montar o container de dependencias do lukato; a aplicacao nao "
                f"pode subir pela metade. Causa: {type(exc).__name__}: {exc}",
                details={"stage": "startup", "cause": type(exc).__name__},
            ) from exc

        app.state.container = container
        app.state.engine = engine
        _logger.info("application_ready", routes=len(app.routes))
        try:
            yield
        finally:
            _logger.info("application_stopping")
            await dispose_container(container, engine)
            app.state.container = None
            app.state.engine = None
            _logger.info("application_stopped")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Monta a aplicacao FastAPI completa (API v1 + sondas + console)."""
    resolved = settings if settings is not None else get_settings()

    app = FastAPI(
        title=API_TITLE,
        version=resolved.app.version or __version__,
        # `None` nos dois: as rotas de documentacao sao registradas a mao em
        # `_install_docs`, porque as embutidas do FastAPI apontam para um CDN fixo
        # que a CSP desta aplicacao proibe — 200 com a pagina em branco.
        docs_url=None,
        redoc_url=None,
        openapi_url=OPENAPI_URL,
        root_path=resolved.app.root_path,
        lifespan=_lifespan(resolved),
    )
    app.state.settings = resolved
    app.state.container = None
    app.state.engine = None

    install_error_handlers(app)
    _install_middlewares(app, resolved)

    _install_docs(app, resolved)
    app.include_router(api_router)
    app.include_router(root_router)
    app.include_router(ui_router)
    mount_static(app)

    customize_openapi(app)
    return app


def _install_docs(app: FastAPI, settings: Settings) -> None:
    """Registra `/api/docs` e `/api/redoc` servindo os bundles da origem configurada.

    As rotas embutidas do FastAPI apontam para `cdn.jsdelivr.net` num literal, e a
    CSP desta aplicacao e `script-src 'self'`. O resultado era uma pagina que
    respondia **200 e nao renderizava nada**: a propria resposta proibia o script
    que ela mandava o navegador buscar. E os dois enderecos estao documentados na
    secao 4 do readme, ou seja, e a primeira porta em que o usuario bate.

    Aqui a origem dos bundles vem de `LUKATO_APP__DOCS_ASSETS_BASE` e a CSP destas
    duas rotas — e so delas — libera exatamente essa origem. O middleware de
    seguranca usa `setdefault`, entao o cabecalho definido aqui e o que vale. O
    console continua com a politica fechada, sem CDN nenhum.
    """
    base = settings.app.docs_assets_base.rstrip("/")
    origem = _origin_of(base)
    csp = (
        f"default-src 'self'; style-src 'self' 'unsafe-inline' {origem}; "
        f"img-src 'self' data: {origem} https://fastapi.tiangolo.com; "
        f"script-src 'self' 'unsafe-inline' {origem}; "
        "worker-src 'self' blob:; frame-ancestors 'none'"
    )

    @app.get(DOCS_URL, include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        """Swagger UI apontado para os bundles da origem configurada."""
        pagina = get_swagger_ui_html(
            openapi_url=f"{settings.app.root_path}{OPENAPI_URL}",
            title=f"{API_TITLE} — Swagger UI",
            swagger_js_url=f"{base}/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url=f"{base}/swagger-ui-dist@5/swagger-ui.css",
        )
        pagina.headers["Content-Security-Policy"] = csp
        return pagina

    @app.get(REDOC_URL, include_in_schema=False)
    async def redoc() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        """ReDoc apontado para os bundles da origem configurada."""
        pagina = get_redoc_html(
            openapi_url=f"{settings.app.root_path}{OPENAPI_URL}",
            title=f"{API_TITLE} — ReDoc",
            redoc_js_url=f"{base}/redoc@next/bundles/redoc.standalone.js",
            with_google_fonts=False,
        )
        pagina.headers["Content-Security-Policy"] = csp
        return pagina


def _origin_of(url: str) -> str:
    """Esquema + host + porta de uma URL, que e a unidade que a CSP entende.

    A CSP casa por ORIGEM, nao por caminho: liberar
    `https://cdn.exemplo/npm/swagger-ui-dist@5` nao existe como conceito. Reduzir
    aqui evita uma diretiva silenciosamente invalida — que o navegador descarta,
    devolvendo a pagina em branco que este codigo existe para impedir.
    """
    partes = urlsplit(url)
    if not partes.scheme or not partes.netloc:
        return "'self'"
    return f"{partes.scheme}://{partes.netloc}"


def _install_middlewares(app: FastAPI, settings: Settings) -> None:
    """Instala a pilha de middlewares na ordem de execucao exigida.

    O Starlette monta a pilha **ao contrario da ordem de registro**: o ultimo
    `add_middleware` fica por fora e executa primeiro. Por isso o CORS e
    adicionado por ultimo — ele precisa ser o mais externo.

    Duas razoes concretas, e nenhuma delas e estilo:

    1. o *preflight* `OPTIONS` chega sem credencial e sem corpo; se o limitador de
       taxa ou o resolvedor de identidade o vissem antes, o navegador receberia
       `429`/`401` em uma requisicao que nunca deveria chegar a aplicacao;
    2. os cabecalhos `Access-Control-Allow-*` precisam sair **tambem** nas
       respostas de erro. Um `429` do limitador ou um `422` de guardrail sem eles
       viram "network error" opaco no console do navegador, e o cliente perde o
       envelope de erro que a plataforma se deu ao trabalho de padronizar.

    Resultado, de fora para dentro::

        CORS -> RequestId -> SecurityHeaders -> RateLimit -> Timing -> rotas

    A ordem interna e a de :func:`~lukato.interfaces.http.middleware.install_middlewares`
    e tambem tem motivo: o `429` do limitador precisa sair carimbado com
    `X-Request-ID` e com os cabecalhos de seguranca, o que so acontece com ele
    **dentro** dos dois primeiros.
    """
    install_middlewares(
        app,
        hsts=settings.is_production,
        rate_limit_enabled=True,
    )
    # Traduz o formulario HTML do console para o JSON que a API entende. Fica
    # DEPOIS destes na lista, o que em Starlette significa mais por dentro: ele
    # precisa rodar antes da validacao da rota, mas depois do request-id e do
    # rate limit, para que um formulario apareca no log e conte na cota igual a
    # qualquer outra requisicao.
    app.add_middleware(ConsoleFormMiddleware)
    origins = [origin for origin in settings.security.cors_origins if origin]
    allow_all = "*" in origins
    # Curinga e credencial nao convivem: `Access-Control-Allow-Origin: *` com
    # `Allow-Credentials: true` faz o navegador enviar cookie e Authorization para
    # qualquer origem. Com a lista aberta, as credenciais ficam desligadas; quem
    # precisa delas declara as origens em LUKATO_SECURITY__CORS_ORIGINS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=not allow_all,
        allow_methods=list(CORS_METHODS),
        allow_headers=list(CORS_HEADERS),
        expose_headers=list(CORS_EXPOSE),
        max_age=600,
    )
    _logger.info(
        "middlewares_installed",
        order=["cors", "request_id", "security_headers", "rate_limit", "timing", "console_form"],
        cors_origins=origins or ["*"],
        allow_credentials=not allow_all,
        hsts=settings.is_production,
    )


app = create_app()
"""Aplicacao ASGI do processo — e para ela que `uvicorn lukato.main:app` aponta."""
