"""Schemas HTTP compartilhados: erro, paginacao, saude, consumo e confirmacao.

Este modulo e a base de vocabulario de toda a API v1. Os envelopes normativos da
SPEC-0000 secao 11 moram aqui:

* lista -> ``{"items": [...], "total": int, "limit": int, "offset": int}``
* erro  -> ``{"error": {"code": str, "message": str, "details": {...}}}``

Os schemas de **entrada** herdam de :class:`InSchema` (que proibe campos extras) e
os de **saida** de :class:`OutSchema` (que aceita construcao a partir dos modelos
de dominio). Nenhum schema expoe campo interno: `password_hash`, `hashed_secret`,
`embedding` e afins nunca aparecem em resposta.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from lukato.application.dto import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, PageRequest
from lukato.application.dto import Page as ResultPage
from lukato.application.use_cases.health import ComponentHealth as ComponentHealthReport
from lukato.application.use_cases.health import (
    LivenessReport,
    ProviderInfo,
    ProviderReport,
    ReadinessReport,
)
from lukato.domain.models.run import TokenUsage
from lukato.domain.types import Json

__all__ = [
    "DEFAULT_LIMIT",
    "ERROR_RESPONSES",
    "MAX_LIMIT",
    "Acknowledged",
    "ComponentHealth",
    "DeletedResponse",
    "ErrorBody",
    "ErrorResponse",
    "HealthStatus",
    "InSchema",
    "LivenessResponse",
    "OutSchema",
    "Page",
    "PaginationParams",
    "ProviderInfoOut",
    "ProvidersResponse",
    "ReadinessResponse",
    "TokenUsageOut",
    "error_responses",
]

T = TypeVar("T")

DEFAULT_LIMIT: Final[int] = DEFAULT_PAGE_LIMIT
"""Tamanho de pagina padrao da API (espelha `application.dto.DEFAULT_PAGE_LIMIT`)."""

MAX_LIMIT: Final[int] = MAX_PAGE_LIMIT
"""Teto de itens por pagina aceito pela borda HTTP."""


# ---------------------------------------------------------------------------
# Bases
# ---------------------------------------------------------------------------
class InSchema(BaseModel):
    """Base dos corpos de **entrada**: campo desconhecido e erro de validacao.

    Recusar o campo extra e deliberado: um `PUT` com `temperatura` no lugar de
    `temperature` seria silenciosamente ignorado e o cliente acreditaria ter
    configurado algo que nunca foi aplicado.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OutSchema(BaseModel):
    """Base dos corpos de **saida**: monta-se direto do modelo de dominio."""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Erro
# ---------------------------------------------------------------------------
class ErrorBody(OutSchema):
    """Conteudo do envelope de erro (espelha `LukatoError.to_dict`)."""

    code: str = Field(description="Codigo estavel do erro, proprio do dominio.")
    message: str = Field(description="Mensagem legivel, em portugues.")
    details: Json = Field(default_factory=dict, description="Detalhes estruturados do erro.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": "not_found",
                "message": "Modulo 'atendimento' nao encontrado.",
                "details": {"reference": "atendimento"},
            }
        }
    )


class ErrorResponse(OutSchema):
    """Envelope normativo de erro da API v1."""

    error: ErrorBody

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "guardrail_violation",
                    "message": "Conteudo bloqueado pela politica 'entrada-padrao'.",
                    "details": {"stage": "input", "findings": []},
                }
            }
        }
    )


ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    400: {"model": ErrorResponse, "description": "Requisicao malformada"},
    401: {"model": ErrorResponse, "description": "Credencial ausente ou invalida"},
    402: {"model": ErrorResponse, "description": "Orcamento FinOps estourado"},
    403: {"model": ErrorResponse, "description": "Sem a permissao exigida"},
    404: {"model": ErrorResponse, "description": "Recurso inexistente"},
    409: {"model": ErrorResponse, "description": "Conflito de estado"},
    422: {"model": ErrorResponse, "description": "Entrada invalida ou guardrail acionado"},
    429: {"model": ErrorResponse, "description": "Limite de requisicoes excedido"},
    500: {"model": ErrorResponse, "description": "Erro interno"},
    501: {"model": ErrorResponse, "description": "Capacidade indisponivel nesta instalacao"},
    502: {"model": ErrorResponse, "description": "Falha em provedor externo"},
    503: {"model": ErrorResponse, "description": "Servico nao esta pronto"},
}
"""Respostas de erro reutilizaveis, para o argumento `responses=` das rotas."""


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Recorta :data:`ERROR_RESPONSES` nos status pedidos pela rota.

    Status desconhecido e ignorado em silencio: documentacao nunca deve quebrar
    o registro de uma rota.
    """
    return {status: ERROR_RESPONSES[status] for status in statuses if status in ERROR_RESPONSES}


# ---------------------------------------------------------------------------
# Paginacao
# ---------------------------------------------------------------------------
class Page(BaseModel, Generic[T]):
    """Envelope de lista da API v1 (SPEC-0000 secao 11)."""

    items: list[T] = Field(default_factory=list, description="Itens desta pagina.")
    total: int = Field(default=0, ge=0, description="Total de itens que satisfazem o filtro.")
    limit: int = Field(
        default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Tamanho da pagina pedida."
    )
    offset: int = Field(default=0, ge=0, description="Deslocamento desta pagina.")

    @classmethod
    def of(
        cls,
        items: Iterable[T],
        total: int | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> Page[T]:
        """Monta a pagina; `total` ausente assume o tamanho da propria lista."""
        materialized = list(items)
        return cls(
            items=materialized,
            total=len(materialized) if total is None else max(0, int(total)),
            limit=max(1, min(int(limit), MAX_LIMIT)),
            offset=max(0, int(offset)),
        )

    @classmethod
    def from_result(cls, page: ResultPage[Any], transform: Callable[[Any], T]) -> Page[T]:
        """Converte a `Page` da camada de aplicacao aplicando `transform` a cada item."""
        return cls.of(
            (transform(item) for item in page.items),
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )

    @property
    def count(self) -> int:
        """Quantidade de itens presentes nesta pagina."""
        return len(self.items)

    @property
    def has_more(self) -> bool:
        """True quando ainda restam itens depois desta pagina."""
        return self.offset + len(self.items) < self.total


def _as_int(value: Any, fallback: int) -> int:
    """Converte o valor cru em inteiro, caindo no padrao quando nao for numerico.

    A dataclass tambem e usada como dependencia do FastAPI, e nesse caminho os
    defaults declarados sao objetos `Query`. Instanciar `PaginationParams()` na
    mao (em um teste, por exemplo) nao pode deixar um `Query` no lugar do numero.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """Janela de paginacao lida da query string (`?limit=&offset=`).

    Serve como dependencia direta (`Depends(PaginationParams)`) e como valor de
    retorno de :func:`lukato.interfaces.http.deps.get_pagination`.
    """

    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Quantidade maxima de itens na pagina (1..200).",
    )
    offset: int = Query(
        0,
        ge=0,
        description="Quantidade de itens a pular antes de montar a pagina.",
    )

    def __post_init__(self) -> None:
        """Normaliza a janela: `limit` em 1..200 e `offset` nunca negativo."""
        object.__setattr__(
            self, "limit", max(1, min(_as_int(self.limit, DEFAULT_LIMIT), MAX_LIMIT))
        )
        object.__setattr__(self, "offset", max(0, _as_int(self.offset, 0)))

    @property
    def page(self) -> PageRequest:
        """Janela equivalente no vocabulario da camada de aplicacao."""
        return PageRequest(limit=self.limit, offset=self.offset)


# ---------------------------------------------------------------------------
# Saude
# ---------------------------------------------------------------------------
class HealthStatus(StrEnum):
    """Estado agregado de um componente ou da instalacao inteira."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class ComponentHealth(OutSchema):
    """Saude de um componente sondado por `/readyz`."""

    status: HealthStatus = Field(description="Situacao do componente.")
    detail: str = Field(default="", description="Explicacao legivel da situacao.")

    @classmethod
    def from_report(cls, report: ComponentHealthReport) -> ComponentHealth:
        """Converte o DTO de saude da camada de aplicacao."""
        return cls(status=HealthStatus(report.status), detail=report.detail)


class LivenessResponse(OutSchema):
    """Resposta de `GET /healthz`: constante, sem tocar em dependencia alguma."""

    status: str = Field(description="Sempre 'alive' enquanto o processo responde.")
    service: str = Field(description="Nome do servico.")
    version: str = Field(description="Versao em execucao.")

    model_config = ConfigDict(
        json_schema_extra={"example": {"status": "alive", "service": "lukato", "version": "1.0.0"}}
    )

    @classmethod
    def from_report(cls, report: LivenessReport) -> LivenessResponse:
        """Converte o relatorio de liveness."""
        return cls(status=report.status, service=report.service, version=report.version)


class ReadinessResponse(OutSchema):
    """Resposta de `GET /readyz`: prontidao componente a componente."""

    status: HealthStatus = Field(description="Situacao agregada da instalacao.")
    components: dict[str, ComponentHealth] = Field(
        default_factory=dict, description="Situacao por componente sondado."
    )
    version: str = Field(default="", description="Versao em execucao.")
    service: str = Field(default="", description="Nome do servico.")
    environment: str = Field(default="", description="Ambiente configurado (dev/staging/prod).")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "degraded",
                "components": {
                    "database": {"status": "ok", "detail": "5 definicao(oes) de modulo"},
                    "llm": {"status": "degraded", "detail": "provedor echo (offline)"},
                },
                "version": "1.0.0",
                "service": "lukato",
                "environment": "dev",
            }
        }
    )

    @property
    def ready(self) -> bool:
        """True enquanto nenhum componente critico esta fora do ar."""
        return self.status is not HealthStatus.DOWN

    @classmethod
    def from_report(cls, report: ReadinessReport) -> ReadinessResponse:
        """Converte o relatorio de prontidao da camada de aplicacao."""
        return cls(
            status=HealthStatus(report.status),
            components={
                name: ComponentHealth.from_report(item) for name, item in report.components.items()
            },
            version=report.version,
            service=report.service,
            environment=report.environment,
        )


class ProviderInfoOut(OutSchema):
    """Retrato de um provedor externo (LLM, embeddings, tracer, banco)."""

    name: str = Field(description="Nome do provedor.")
    kind: str = Field(description="Papel do provedor no ecossistema.")
    status: HealthStatus = Field(default=HealthStatus.OK, description="Situacao ao vivo.")
    detail: str = Field(default="", description="Explicacao legivel da situacao.")
    configured: bool = Field(default=False, description="Se ha configuracao efetiva.")
    info: Json = Field(default_factory=dict, description="Dados publicos; nunca credenciais.")

    @classmethod
    def from_report(cls, report: ProviderInfo) -> ProviderInfoOut:
        """Converte o DTO de provedor da camada de aplicacao."""
        return cls(
            name=report.name,
            kind=report.kind,
            status=HealthStatus(report.status),
            detail=report.detail,
            configured=report.configured,
            info=dict(report.info),
        )


class ProvidersResponse(OutSchema):
    """Resposta de `GET /api/v1/health/providers` (SPEC-0008 secao 5)."""

    status: HealthStatus = Field(default=HealthStatus.OK, description="Situacao agregada.")
    version: str = Field(default="", description="Versao em execucao.")
    checked_at: str = Field(default="", description="Instante da sondagem, em ISO-8601.")
    providers: list[ProviderInfoOut] = Field(
        default_factory=list, description="Provedores sondados."
    )

    @classmethod
    def from_report(cls, report: ProviderReport) -> ProvidersResponse:
        """Converte o quadro de provedores da camada de aplicacao."""
        return cls(
            status=HealthStatus(report.status),
            version=report.version,
            checked_at=report.checked_at,
            providers=[ProviderInfoOut.from_report(item) for item in report.providers],
        )


# ---------------------------------------------------------------------------
# Consumo e confirmacoes
# ---------------------------------------------------------------------------
class TokenUsageOut(OutSchema):
    """Consumo de tokens de uma execucao ou de um passo."""

    prompt_tokens: int = Field(default=0, ge=0, description="Tokens enviados ao modelo.")
    completion_tokens: int = Field(default=0, ge=0, description="Tokens gerados pelo modelo.")
    total_tokens: int = Field(default=0, ge=0, description="Soma dos dois anteriores.")

    @classmethod
    def from_domain(cls, usage: TokenUsage | None) -> TokenUsageOut:
        """Converte o `TokenUsage` do dominio (ausente vira consumo zerado)."""
        if usage is None:
            return cls()
        return cls(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        )


class Acknowledged(OutSchema):
    """Confirmacao simples de uma operacao sem corpo proprio."""

    ok: bool = Field(default=True, description="Sempre verdadeiro quando a operacao concluiu.")
    message: str = Field(default="", description="Detalhe opcional para a interface.")

    model_config = ConfigDict(
        json_schema_extra={"example": {"ok": True, "message": "Modulo removido."}}
    )


class DeletedResponse(Acknowledged):
    """Confirmacao de remocao que informa quantos registros sairam."""

    deleted: int = Field(default=0, ge=0, description="Quantidade de registros removidos.")

    model_config = ConfigDict(
        json_schema_extra={"example": {"ok": True, "message": "", "deleted": 3}}
    )
