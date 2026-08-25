"""Middlewares HTTP do lukato: correlacao, tempo, cabecalhos de seguranca e taxa.

Todos sao `BaseHTTPMiddleware` do Starlette e seguem a mesma disciplina: **um
middleware nunca transforma o proprio defeito em 500 silencioso**. Telemetria que
falha e registrada e a requisicao segue; so o limitador de taxa interrompe o
fluxo, e ainda assim com o envelope de erro normativo.

Ordem de instalacao (de fora para dentro), garantida por
:func:`install_middlewares`::

    RequestId -> SecurityHeaders -> RateLimit -> Timing -> rotas

Ela nao e arbitraria: a resposta `429` do limitador precisa sair com
`X-Request-ID` e com os cabecalhos de seguranca, o que so acontece se ele estiver
**dentro** dos dois primeiros.

Uma excecao genuinamente nao tratada e a unica resposta que escapa desta pilha: o
`ServerErrorMiddleware` do Starlette e sempre o mais externo e responde antes de
voltar por aqui. Por isso o handler de
:mod:`lukato.interfaces.http.errors` carimba o `X-Request-ID` por conta propria —
nao ha middleware entre ele e o cliente.
"""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from lukato.adapters.observability.metrics import Metrics, get_metrics
from lukato.config import Settings, bind_request_context, clear_request_context, get_logger
from lukato.domain.types import new_id
from lukato.interfaces.http.deps import DEFAULT_API_KEY_HEADER, credential_fingerprint
from lukato.interfaces.http.errors import error_payload

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_WINDOW",
    "EXEMPT_PATHS",
    "EXEMPT_PREFIXES",
    "HSTS_VALUE",
    "PERMISSIONS_POLICY",
    "REQUEST_ID_HEADER",
    "RESPONSE_TIME_HEADER",
    "UNMATCHED_ROUTE",
    "RateLimitMiddleware",
    "RequestIdMiddleware",
    "SecurityHeadersMiddleware",
    "TimingMiddleware",
    "install_middlewares",
]

_logger = get_logger(__name__)

_Handler = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
"""Cabecalho de correlacao aceito na entrada e devolvido em toda resposta."""

RESPONSE_TIME_HEADER: Final[str] = "X-Response-Time-ms"
"""Cabecalho com a duracao do atendimento, em milissegundos."""

UNMATCHED_ROUTE: Final[str] = "unmatched"
"""Label de metrica usado quando nenhuma rota casou (404 e afins).

Sem ele, cada URL inexistente viraria uma serie nova no Prometheus — um pedido
malicioso com caminhos aleatorios explodiria a cardinalidade da metrica.
"""

CONTENT_SECURITY_POLICY: Final[str] = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "script-src 'self'; frame-ancestors 'none'"
)
"""CSP do console: tudo servido de `static/`, nenhum CDN (requisito offline-first)."""

PERMISSIONS_POLICY: Final[str] = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=()"
)
"""Superficie minima de APIs do navegador: o console nao usa nenhuma delas."""

HSTS_VALUE: Final[str] = "max-age=31536000; includeSubDomains"
"""Valor de `Strict-Transport-Security`, enviado somente em producao."""

DEFAULT_RATE_LIMIT: Final[int] = 240
"""Requisicoes permitidas por janela para cada chamador."""

DEFAULT_RATE_WINDOW: Final[float] = 60.0
"""Tamanho da janela deslizante do limitador, em segundos."""

EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/readyz", "/metrics"})
"""Sondas e metricas nunca sao limitadas: o orquestrador precisa delas sempre."""

EXEMPT_PREFIXES: Final[tuple[str, ...]] = ("/static",)
"""Prefixos isentos do limitador (arquivos estaticos do console)."""

_MAX_LOCAL_KEYS: Final[int] = 10_000
"""Teto de chaveiros do limitador em memoria; a chave mais antiga sai primeiro."""

_REQUEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
"""Formato aceito para um `X-Request-ID` vindo do cliente."""

_PRODUCTION_ENVS: Final[frozenset[str]] = frozenset({"prod", "production"})
"""Ambientes em que o HSTS e enviado."""


def _settings_of(request: Request) -> Settings | None:
    """Le `Settings` do container montado em `app.state`, sem nunca levantar."""
    container = getattr(request.app.state, "container", None)
    settings = getattr(container, "settings", None)
    return settings if isinstance(settings, Settings) else None


def _route_template(request: Request) -> str:
    """Template da rota casada (`/api/v1/modules/{slug}`) ou :data:`UNMATCHED_ROUTE`.

    O Starlette 1.6 aninha cada `include_router`, entao `scope["route"].path` traz
    apenas o caminho **local** do router (`/modules/{slug}`) e o prefixo fica em
    `scope["root_path"]`. Sem juntar os dois, a metrica perde o `/api/v1` e duas
    rotas homonimas em versoes diferentes da API colidiriam na mesma serie.
    """
    scope = request.scope
    route = scope.get("route")
    local = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not local:
        return UNMATCHED_ROUTE

    local = str(local)
    concreto = str(scope.get("path") or "")
    params: dict[str, object] = scope.get("path_params") or {}

    # `local` cobre so o sufixo casado pelo router aninhado; o prefixo (`/api/v1`)
    # esta apenas no caminho concreto. Substituindo os parametros em `local`
    # obtem-se o sufixo concreto, e o que sobra a esquerda e exatamente o prefixo.
    sufixo_concreto = local
    for nome, valor in params.items():
        sufixo_concreto = sufixo_concreto.replace(f"{{{nome}}}", str(valor))
    if concreto.endswith(sufixo_concreto):
        prefixo = concreto[: len(concreto) - len(sufixo_concreto)]
        return f"{prefixo}{local}"
    return local


# ---------------------------------------------------------------------------
# Correlacao
# ---------------------------------------------------------------------------
class RequestIdMiddleware(BaseHTTPMiddleware):
    """Gera ou propaga `X-Request-ID` e o injeta no contexto de log.

    Um identificador vindo do cliente e aceito apenas se couber em um formato
    curto e imprimivel: repassar texto arbitrario para dentro de um cabecalho de
    resposta e de uma linha de log e caminho conhecido de injecao.
    """

    def __init__(self, app: Any, *, header: str = REQUEST_ID_HEADER) -> None:
        super().__init__(app)
        self.header = header

    async def dispatch(self, request: Request, call_next: _Handler) -> Response:
        """Carimba o identificador na requisicao, no log e na resposta."""
        request_id = self._resolve(request)
        request.state.request_id = request_id
        bind_request_context(request_id, method=request.method, path=request.url.path)
        try:
            response = await call_next(request)
        finally:
            clear_request_context()
        response.headers[self.header] = request_id
        return response

    def _resolve(self, request: Request) -> str:
        """Aproveita o identificador do cliente quando ele for aceitavel."""
        incoming = (request.headers.get(self.header) or "").strip()
        if incoming and _REQUEST_ID_PATTERN.match(incoming):
            return incoming
        return new_id()


# ---------------------------------------------------------------------------
# Tempo de resposta
# ---------------------------------------------------------------------------
class TimingMiddleware(BaseHTTPMiddleware):
    """Mede o atendimento, publica `X-Response-Time-ms` e alimenta o Prometheus.

    A metrica usa o **template** da rota, nunca o caminho concreto: `/modules/{slug}`
    e uma serie; `/modules/atendimento` seriam infinitas.
    """

    def __init__(self, app: Any, *, metrics: Metrics | None = None) -> None:
        super().__init__(app)
        self._metrics = metrics

    @property
    def metrics(self) -> Metrics:
        """Registro de metricas do processo (memoizado)."""
        if self._metrics is None:
            self._metrics = get_metrics()
        return self._metrics

    async def dispatch(self, request: Request, call_next: _Handler) -> Response:
        """Cronometra a requisicao e observa o resultado, mesmo em caso de falha."""
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._observe(request, status=500, elapsed=time.perf_counter() - started)
            raise
        elapsed = time.perf_counter() - started
        response.headers[RESPONSE_TIME_HEADER] = f"{elapsed * 1000.0:.2f}"
        self._observe(request, status=response.status_code, elapsed=elapsed)
        return response

    def _observe(self, request: Request, *, status: int, elapsed: float) -> None:
        """Registra a metrica; falha de telemetria nunca derruba a requisicao."""
        try:
            self.metrics.observe_http(
                request.method, _route_template(request), status, max(0.0, elapsed)
            )
        except Exception as exc:  # pragma: no cover - registro defeituoso
            _logger.warning(
                "http_metrics_failed",
                error=f"{type(exc).__name__}: {exc}",
                path=request.url.path,
            )


# ---------------------------------------------------------------------------
# Cabecalhos de seguranca
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Aplica os cabecalhos de defesa do navegador a toda resposta.

    O HSTS so vai em producao: anunciar `Strict-Transport-Security` em um ambiente
    de desenvolvimento servido por HTTP tranca o navegador do desenvolvedor em
    `https://localhost` por um ano.
    """

    def __init__(
        self,
        app: Any,
        *,
        hsts: bool | None = None,
        csp: str = CONTENT_SECURITY_POLICY,
        permissions_policy: str = PERMISSIONS_POLICY,
    ) -> None:
        super().__init__(app)
        self._hsts = hsts
        self.csp = csp
        self.permissions_policy = permissions_policy

    async def dispatch(self, request: Request, call_next: _Handler) -> Response:
        """Deixa a rota responder e acrescenta os cabecalhos na saida."""
        response = await call_next(request)
        try:
            self._apply(request, response)
        except Exception as exc:  # pragma: no cover - resposta sem cabecalhos mutaveis
            _logger.warning(
                "security_headers_failed",
                error=f"{type(exc).__name__}: {exc}",
                path=request.url.path,
            )
        return response

    def _apply(self, request: Request, response: Response) -> None:
        """Escreve os cabecalhos sem sobrescrever o que a rota ja definiu."""
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", self.permissions_policy)
        headers.setdefault("Content-Security-Policy", self.csp)
        if self._production(request):
            headers.setdefault("Strict-Transport-Security", HSTS_VALUE)

    def _production(self, request: Request) -> bool:
        """Resolve (uma unica vez) se esta instalacao e de producao."""
        if self._hsts is None:
            settings = _settings_of(request)
            environment = str(getattr(getattr(settings, "app", None), "env", "") or "").lower()
            self._hsts = environment in _PRODUCTION_ENVS
        return self._hsts


# ---------------------------------------------------------------------------
# Limite de taxa
# ---------------------------------------------------------------------------
class _LocalWindow:
    """Janela deslizante em memoria, usada quando o container nao expoe cache.

    E um anteparo consciente: proteger o processo com um contador local vale mais
    do que nao limitar nada. Em varias replicas o teto efetivo e por replica, e o
    caminho correto continua sendo injetar um `CachePort` compartilhado.
    """

    __slots__ = ("_hits", "_max_keys")

    def __init__(self, max_keys: int = _MAX_LOCAL_KEYS) -> None:
        self._hits: OrderedDict[str, list[float]] = OrderedDict()
        self._max_keys = max_keys

    def hit(self, key: str, *, limit: int, window: float, now: float) -> tuple[bool, list[float]]:
        """Registra a tentativa e diz se ela cabe na janela."""
        stamps = [stamp for stamp in self._hits.get(key, []) if stamp > now - window]
        allowed = len(stamps) < limit
        if allowed:
            stamps.append(now)
        self._hits[key] = stamps
        self._hits.move_to_end(key)
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        return allowed, stamps


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Janela deslizante por chamador, apoiada no `CachePort` do container.

    A identidade vem da credencial apresentada (resumo do JWT ou da API key) e,
    na falta dela, do IP de origem — o `Principal` ainda nao foi resolvido nesta
    altura da pilha. Sondas, metricas e arquivos estaticos ficam isentos.
    """

    def __init__(
        self,
        app: Any,
        *,
        limit: int = DEFAULT_RATE_LIMIT,
        window: float = DEFAULT_RATE_WINDOW,
        enabled: bool = True,
        exempt_paths: Sequence[str] = (),
    ) -> None:
        super().__init__(app)
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self.enabled = enabled
        self.exempt_paths = frozenset(EXEMPT_PATHS | set(exempt_paths))
        self._local = _LocalWindow()

    async def dispatch(self, request: Request, call_next: _Handler) -> Response:
        """Aplica o limite e devolve `429` com `Retry-After` quando estourar."""
        path = request.url.path
        if not self.enabled or path in self.exempt_paths or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        identity = self._identity(request)
        allowed, retry_after = await self._check(request, identity)
        if allowed:
            return await call_next(request)

        _logger.warning(
            "rate_limit_exceeded",
            request_id=str(getattr(request.state, "request_id", "") or ""),
            identity=identity,
            limit=self.limit,
            window=self.window,
            path=path,
        )
        return JSONResponse(
            status_code=429,
            content=error_payload(
                "rate_limited",
                "Limite de requisicoes excedido; tente novamente em instantes.",
                {"limit": self.limit, "window_seconds": self.window, "retry_after": retry_after},
            ),
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(self.limit),
                "X-RateLimit-Window": str(int(self.window)),
            },
        )

    def _identity(self, request: Request) -> str:
        """Chave estavel do chamador, derivada da credencial ou do IP."""
        settings = _settings_of(request)
        header = DEFAULT_API_KEY_HEADER
        if settings is not None:
            header = str(settings.security.api_key_header or "").strip() or header
        return credential_fingerprint(request, api_key_header=header)

    async def _check(self, request: Request, identity: str) -> tuple[bool, int]:
        """Consulta a janela deslizante; qualquer falha de cache libera a chamada."""
        now = time.time()
        cache = getattr(getattr(request.app.state, "container", None), "cache", None)
        if cache is None:
            allowed, stamps = self._local.hit(
                identity, limit=self.limit, window=self.window, now=now
            )
            return allowed, self._retry_after(stamps, now)
        try:
            return await self._check_cache(cache, identity, now)
        except Exception as exc:
            _logger.warning(
                "rate_limit_cache_failed",
                error=f"{type(exc).__name__}: {exc}",
                identity=identity,
            )
            return True, 0

    async def _check_cache(self, cache: Any, identity: str, now: float) -> tuple[bool, int]:
        """Le, filtra e regrava a janela no cache compartilhado."""
        key = f"lukato:ratelimit:{identity}"
        stored = await cache.get(key)
        stamps = [
            float(stamp)
            for stamp in (stored if isinstance(stored, list) else [])
            if isinstance(stamp, (int, float)) and float(stamp) > now - self.window
        ]
        if len(stamps) >= self.limit:
            return False, self._retry_after(stamps, now)
        stamps.append(now)
        await cache.set(key, stamps, ttl_seconds=self.window)
        return True, 0

    def _retry_after(self, stamps: Sequence[float], now: float) -> int:
        """Segundos ate a janela abrir espaco (minimo de um segundo)."""
        if not stamps:
            return 1
        remaining = self.window - (now - min(stamps))
        return max(1, math.ceil(remaining))


# ---------------------------------------------------------------------------
# Instalacao
# ---------------------------------------------------------------------------
def install_middlewares(
    app: FastAPI,
    *,
    hsts: bool | None = None,
    rate_limit: int = DEFAULT_RATE_LIMIT,
    rate_window: float = DEFAULT_RATE_WINDOW,
    rate_limit_enabled: bool = True,
) -> None:
    """Instala os quatro middlewares na ordem correta.

    O Starlette monta a pilha ao contrario da ordem de registro (o ultimo
    registrado fica por fora), entao o registro comeca pelo mais interno.
    """
    app.add_middleware(TimingMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        limit=rate_limit,
        window=rate_window,
        enabled=rate_limit_enabled,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    app.add_middleware(RequestIdMiddleware)
