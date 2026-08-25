"""Dependencias da API v1: container, principal, permissao, paginacao e transacao.

Estas funcoes sao o unico caminho pelo qual uma rota alcanca a aplicacao. O
`Container` chega pronto do composition root em `app.state.container`; o
`Principal` e resolvido a partir da credencial da requisicao; a autorizacao passa
sempre por `Principal.can(...)` (SPEC-0006 secao 1), nunca por comparacao de
papel.

Nenhuma rota abre repositorio por conta propria: :func:`get_uow` existe para os
poucos casos de leitura direta e os casos de uso ja abrem a sua propria unidade
de trabalho.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Final

from fastapi import Depends, Query, Request

from lukato.application.container import Container
from lukato.application.use_cases.identity import AuthenticateApiKey, AuthenticateToken
from lukato.config import bind_request_context
from lukato.domain.errors import ConfigurationError, ForbiddenError, UnauthorizedError
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.interfaces.http.schemas.common import DEFAULT_LIMIT, MAX_LIMIT, PaginationParams

__all__ = [
    "AUTHORIZATION_HEADER",
    "BEARER_SCHEME",
    "DEFAULT_API_KEY_HEADER",
    "REQUEST_ID_HEADER",
    "ContainerDep",
    "PaginationDep",
    "PrincipalDep",
    "credential_fingerprint",
    "get_container",
    "get_pagination",
    "get_principal",
    "get_request_id",
    "get_uow",
    "require",
]

AUTHORIZATION_HEADER: Final[str] = "Authorization"
"""Cabecalho do esquema `bearerAuth`."""

BEARER_SCHEME: Final[str] = "bearer"
"""Prefixo aceito em `Authorization`, comparado sem diferenciar caixa."""

DEFAULT_API_KEY_HEADER: Final[str] = "X-API-Key"
"""Cabecalho do esquema `apiKeyAuth` quando `Settings` nao define outro."""

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
"""Cabecalho de correlacao propagado em toda resposta."""

_FINGERPRINT_LENGTH: Final[int] = 16
"""Tamanho do resumo de credencial usado como chave de rate limit."""


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------
def get_container(request: Request) -> Container:
    """Devolve o `Container` montado pelo composition root.

    Ausencia do container e defeito de composicao, nao erro do cliente: a
    aplicacao subiu sem passar por `create_app`, e o diagnostico precisa dizer
    exatamente isso.
    """
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise ConfigurationError(
            "A aplicacao subiu sem um Container em 'app.state.container'. "
            "Monte a aplicacao por 'lukato.main.create_app' (composition root).",
            details={"attribute": "app.state.container"},
        )
    if not isinstance(container, Container):
        raise ConfigurationError(
            "'app.state.container' nao e um Container do lukato.",
            details={"received": type(container).__name__},
        )
    return container


ContainerDep = Annotated[Container, Depends(get_container)]
"""Anotacao pronta para injetar o `Container` em uma rota."""


# ---------------------------------------------------------------------------
# Identidade
# ---------------------------------------------------------------------------
def get_request_id(request: Request) -> str:
    """Identificador de correlacao desta requisicao (vazio antes do middleware)."""
    return str(getattr(request.state, "request_id", "") or "")


def _bearer_token(request: Request) -> str:
    """Extrai o JWT de `Authorization: Bearer <jwt>`; formato invalido devolve vazio."""
    raw = request.headers.get(AUTHORIZATION_HEADER, "")
    scheme, _, credential = raw.partition(" ")
    if scheme.strip().lower() != BEARER_SCHEME:
        return ""
    return credential.strip()


def _api_key_header(container: Container) -> str:
    """Nome do cabecalho de API key configurado nesta instalacao."""
    configured = str(container.settings.security.api_key_header or "").strip()
    return configured or DEFAULT_API_KEY_HEADER


def _remember(request: Request, principal: Principal) -> Principal:
    """Guarda o principal na requisicao e o carimba no contexto de log."""
    request.state.principal = principal
    bind_request_context(
        get_request_id(request),
        subject=principal.subject,
        role=principal.role.value,
        tenant_id=principal.tenant_id,
    )
    return principal


async def get_principal(request: Request, container: ContainerDep) -> Principal:
    """Resolve a identidade da requisicao (JWT, API key ou root anonimo).

    Com `security.auth_enabled=false` — o padrao em desenvolvimento — toda rota
    responde como `Principal.anonymous_root()` (SPEC-0006 criterio 4). Com a
    autenticacao ligada, credencial ausente ou invalida vira
    :class:`~lukato.domain.errors.UnauthorizedError`; a mensagem nunca diferencia
    "usuario inexistente" de "senha errada".
    """
    security = container.settings.security
    if not security.auth_enabled:
        return _remember(request, Principal.anonymous_root())

    token = _bearer_token(request)
    if token:
        return _remember(request, await AuthenticateToken(container).execute(token))

    header = _api_key_header(container)
    raw_key = (request.headers.get(header) or "").strip()
    if raw_key:
        return _remember(request, await AuthenticateApiKey(container).execute(raw_key))

    raise UnauthorizedError(
        "Credencial ausente: envie 'Authorization: Bearer <jwt>' ou o cabecalho "
        f"'{header}: lk_<prefixo>_<segredo>'.",
        details={"schemes": ["bearerAuth", "apiKeyAuth"], "api_key_header": header},
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]
"""Anotacao pronta para injetar o `Principal` resolvido em uma rota."""


def require(permission: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    """Devolve a dependencia que exige `permission` do principal da requisicao.

    Uso na rota::

        principal: Annotated[Principal, Depends(require(Permission.MODULE_READ))]

    A checagem duplica de proposito a que o caso de uso ja faz: a borda recusa
    antes de abrir transacao, e o caso de uso continua seguro quando chamado pela
    CLI ou por outro modulo.
    """

    async def _dependency(principal: PrincipalDep) -> Principal:
        """Verifica a permissao exigida e devolve o proprio principal."""
        if principal.can(permission):
            return principal
        raise ForbiddenError(
            f"O principal '{principal.subject}' ({principal.role.value}) nao tem a "
            f"permissao '{permission.value}'.",
            details={
                "subject": principal.subject,
                "role": principal.role.value,
                "required_permission": permission.value,
            },
        )

    _dependency.__name__ = f"require_{permission.name.lower()}"
    return _dependency


def credential_fingerprint(
    request: Request, *, api_key_header: str = DEFAULT_API_KEY_HEADER
) -> str:
    """Identidade estavel da requisicao para limitar taxa, **sem** guardar segredo.

    O middleware de rate limit roda antes do roteamento e nao tem o `Principal`
    resolvido. O que ele precisa e de uma chave estavel por chamador: o resumo do
    token ou da chave de API quando ha credencial, o IP de origem quando nao ha.
    O segredo em si nunca e armazenado nem registrado.
    """
    token = _bearer_token(request)
    if token:
        return f"jwt:{_digest(token)}"
    raw_key = (request.headers.get(api_key_header) or "").strip()
    if raw_key:
        return f"key:{_digest(raw_key)}"
    client = request.client
    return f"ip:{client.host}" if client and client.host else "ip:desconhecido"


def _digest(secret: str) -> str:
    """Resumo curto e irreversivel de uma credencial."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


# ---------------------------------------------------------------------------
# Paginacao
# ---------------------------------------------------------------------------
def get_pagination(
    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Quantidade maxima de itens na pagina (1..200).",
    ),
    offset: int = Query(
        0, ge=0, description="Quantidade de itens a pular antes de montar a pagina."
    ),
) -> PaginationParams:
    """Le `?limit=&offset=` e devolve a janela ja normalizada."""
    return PaginationParams(limit=limit, offset=offset)


PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]
"""Anotacao pronta para injetar a janela de paginacao em uma rota de listagem."""


# ---------------------------------------------------------------------------
# Unidade de trabalho
# ---------------------------------------------------------------------------
@asynccontextmanager
async def get_uow(container: Container) -> AsyncIterator[UnitOfWork]:
    """Abre uma unidade de trabalho para leitura direta na borda.

    Prefira sempre os casos de uso: eles ja abrem (e fecham) a sua propria
    transacao, com a autorizacao no lugar certo. Este atalho existe para as
    poucas leituras auxiliares da UI, e sair do contexto sem `commit()` desfaz
    qualquer escrita.
    """
    async with container.uow_factory() as uow:
        yield uow
