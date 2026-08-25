"""API HTTP do lukato (OpenAPI 3.1 sob o prefixo `/api/v1`).

Organizacao do pacote:

* :mod:`~lukato.interfaces.http.deps` — container, principal, permissao, paginacao;
* :mod:`~lukato.interfaces.http.errors` — o envelope unico de erro;
* :mod:`~lukato.interfaces.http.middleware` — correlacao, tempo, seguranca e taxa;
* :mod:`~lukato.interfaces.http.openapi` — personalizacao e exportacao do contrato;
* :mod:`~lukato.interfaces.http.schemas` — contratos de entrada e saida;
* :mod:`~lukato.interfaces.http.api.v1` — o roteador que agrega todos os recursos.

Contrato entre esta base e os routers: cada modulo de
`api/v1/routers/` expoe um atributo `router: APIRouter` ja com o seu proprio
`prefix` e a sua `tags`. Nenhum router acessa repositorio diretamente — a
operacao passa sempre por um caso de uso de `lukato.application.use_cases`,
construido com o `Container` injetado.
"""

from __future__ import annotations

from lukato.interfaces.http.deps import (
    ContainerDep,
    PaginationDep,
    PrincipalDep,
    get_container,
    get_pagination,
    get_principal,
    get_uow,
    require,
)
from lukato.interfaces.http.errors import error_payload, install_error_handlers
from lukato.interfaces.http.middleware import (
    RateLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
    install_middlewares,
)
from lukato.interfaces.http.openapi import customize_openapi, export_openapi

__all__ = [
    "ContainerDep",
    "PaginationDep",
    "PrincipalDep",
    "RateLimitMiddleware",
    "RequestIdMiddleware",
    "SecurityHeadersMiddleware",
    "TimingMiddleware",
    "customize_openapi",
    "error_payload",
    "export_openapi",
    "get_container",
    "get_pagination",
    "get_principal",
    "get_uow",
    "install_error_handlers",
    "install_middlewares",
    "require",
]
