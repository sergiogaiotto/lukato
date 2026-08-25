"""Handlers de erro da API: um unico envelope para toda falha.

Qualquer erro que chegue a borda sai como
``{"error": {"code": ..., "message": ..., "details": {...}}}`` com o cabecalho
`X-Request-ID` (SPEC-0000 secao 11). Tres regras nao se negociam:

1. o `code` vem do dominio (`LukatoError.code`) e e estavel — cliente programa
   contra ele, nao contra a mensagem;
2. um erro inesperado devolve mensagem neutra: o traceback vai para o log, nunca
   para a resposta, porque caminho de arquivo e nome de tabela sao informacao
   util para quem ataca;
3. todo handler registra log estruturado com o `request_id`, para que a resposta
   que o cliente viu possa ser encontrada depois.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lukato.config import get_logger
from lukato.domain.errors import GuardrailViolation, LukatoError
from lukato.domain.models.guardrail import GuardrailFinding
from lukato.domain.types import Json

__all__ = [
    "GENERIC_MESSAGE",
    "INTERNAL_ERROR_CODE",
    "REQUEST_ID_HEADER",
    "STATUS_CODES",
    "VALIDATION_ERROR_CODE",
    "error_payload",
    "install_error_handlers",
]

_logger = get_logger(__name__)

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
"""Cabecalho de correlacao presente em toda resposta de erro."""

INTERNAL_ERROR_CODE: Final[str] = "internal_error"
"""Codigo do erro inesperado (o unico que nao vem do dominio)."""

VALIDATION_ERROR_CODE: Final[str] = "validation_error"
"""Codigo usado quando o pydantic recusa o corpo ou os parametros."""

GENERIC_MESSAGE: Final[str] = "erro interno"
"""Mensagem neutra devolvida por um erro inesperado."""

STATUS_CODES: Final[dict[int, str]] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: VALIDATION_ERROR_CODE,
    429: "rate_limited",
    500: INTERNAL_ERROR_CODE,
    503: "service_unavailable",
}
"""Codigo estavel para as falhas HTTP que nao nascem de um `LukatoError`."""

_HTTP_ERROR_CODE: Final[str] = "http_error"
"""Codigo de reserva para um status fora de :data:`STATUS_CODES`."""


# ---------------------------------------------------------------------------
# Montagem do envelope
# ---------------------------------------------------------------------------
def error_payload(code: str, message: str, details: Json | None = None) -> Json:
    """Monta o envelope de erro ja seguro para serializacao JSON."""
    body: Json = {
        "code": code,
        "message": message,
        "details": jsonable_encoder(details or {}),
    }
    return {"error": body}


def _request_id(request: Request) -> str:
    """Identificador de correlacao da requisicao (gerado pelo middleware)."""
    return str(getattr(request.state, "request_id", "") or "")


def _route(request: Request) -> str:
    """Template da rota casada, ou o caminho cru quando nada casou."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    return str(template or request.url.path)


def _respond(
    request: Request, *, status_code: int, code: str, message: str, details: Json | None = None
) -> JSONResponse:
    """Cria a resposta de erro com o envelope e o cabecalho de correlacao."""
    request_id = _request_id(request)
    payload = error_payload(code, message, details)
    headers = {REQUEST_ID_HEADER: request_id} if request_id else {}
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def _serialize_findings(raw: Any) -> list[Json]:
    """Converte achados de guardrail (modelos ou mapas) em objetos JSON."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    findings: list[Json] = []
    for item in raw:
        if isinstance(item, GuardrailFinding):
            findings.append(item.model_dump(mode="json"))
        elif isinstance(item, Mapping):
            findings.append(jsonable_encoder(dict(item)))
    return findings


def _details_of(exc: LukatoError) -> Json:
    """Detalhes do erro, com os achados de guardrail explicitos quando existirem."""
    details: Json = dict(exc.details)
    if isinstance(exc, GuardrailViolation):
        findings = _serialize_findings(details.get("findings") or getattr(exc, "findings", None))
        if findings:
            details["findings"] = findings
        else:
            details.pop("findings", None)
    return details


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _handle_lukato_error(request: Request, exc: Exception) -> JSONResponse:
    """Traduz um erro de dominio no envelope, preservando `code` e `http_status`."""
    error = exc if isinstance(exc, LukatoError) else LukatoError(str(exc))
    details = _details_of(error)
    request_id = _request_id(request)
    _logger.warning(
        "http_domain_error",
        request_id=request_id,
        code=error.code,
        status=error.http_status,
        method=request.method,
        route=_route(request),
        path=request.url.path,
        message=error.message,
        details=details,
    )
    return _respond(
        request,
        status_code=error.http_status,
        code=error.code,
        message=error.message,
        details=details,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Traduz a recusa do pydantic em `422 validation_error` com os erros de campo."""
    raw_errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    errors = jsonable_encoder(raw_errors)
    request_id = _request_id(request)
    _logger.warning(
        "http_validation_error",
        request_id=request_id,
        method=request.method,
        route=_route(request),
        path=request.url.path,
        errors=errors,
    )
    return _respond(
        request,
        status_code=422,
        code=VALIDATION_ERROR_CODE,
        message="A requisicao nao passou na validacao de esquema.",
        details={"errors": errors},
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Traduz `404`, `405` e afins no mesmo envelope, em vez do JSON do Starlette."""
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "")
    headers = getattr(exc, "headers", None) or {}
    code = STATUS_CODES.get(status_code, _HTTP_ERROR_CODE)
    message = detail if isinstance(detail, str) and detail else f"Falha HTTP {status_code}."
    details: Json = {} if isinstance(detail, str) else {"detail": jsonable_encoder(detail)}
    _logger.info(
        "http_exception",
        request_id=_request_id(request),
        status=status_code,
        code=code,
        method=request.method,
        route=_route(request),
        path=request.url.path,
    )
    response = _respond(
        request, status_code=status_code, code=code, message=message, details=details
    )
    for name, value in headers.items():
        response.headers[str(name)] = str(value)
    return response


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Ultimo anteparo: registra o traceback e devolve `500` sem vazar nada."""
    request_id = _request_id(request)
    _logger.exception(
        "http_unhandled_error",
        request_id=request_id,
        method=request.method,
        route=_route(request),
        path=request.url.path,
        error=f"{type(exc).__name__}: {exc}",
    )
    return _respond(
        request,
        status_code=500,
        code=INTERNAL_ERROR_CODE,
        message=GENERIC_MESSAGE,
        details={"request_id": request_id},
    )


def install_error_handlers(app: FastAPI) -> None:
    """Registra os quatro handlers de erro da aplicacao.

    A ordem de especificidade e resolvida pelo Starlette, que percorre a MRO da
    excecao: um `ModuleNotFound` cai em `LukatoError` e devolve `404`, nunca no
    anteparo generico.
    """
    app.add_exception_handler(LukatoError, _handle_lukato_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
