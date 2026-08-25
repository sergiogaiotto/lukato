"""Hierarquia unica de erros de dominio do lukato.

Toda excecao levantada pelo dominio, pela camada de aplicacao ou convertida por um
adaptador herda de :class:`LukatoError` e carrega `code` + `http_status` estaveis,
usados pelo handler HTTP para montar `{"error": {...}}`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from lukato.domain.types import Id, Json

__all__ = [
    "ERROR_BY_CODE",
    "BudgetExceededError",
    "ConfigurationError",
    "ConflictError",
    "ForbiddenError",
    "GuardrailViolation",
    "LukatoError",
    "ModuleError",
    "ModuleNotFound",
    "NotFoundError",
    "ProviderError",
    "RateLimitedError",
    "UnauthorizedError",
    "UnsupportedCapability",
    "ValidationError",
    "error_for_code",
]


class LukatoError(Exception):
    """Erro base do ecossistema: mensagem legivel + detalhes serializaveis."""

    code: ClassVar[str] = "lukato_error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str, *, details: Json | None = None) -> None:
        super().__init__(message)
        self.message = message
        self._details: Json = dict(details) if details else {}

    @property
    def details(self) -> Json:
        """Detalhes estruturados do erro (sempre um dicionario)."""
        return self._details

    def to_dict(self) -> Json:
        """Serializa o erro no formato do envelope de erro da API."""
        return {"code": self.code, "message": self.message, "details": dict(self._details)}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ValidationError(LukatoError):
    """Entrada invalida segundo as regras do dominio."""

    code: ClassVar[str] = "validation_error"
    http_status: ClassVar[int] = 422


class NotFoundError(LukatoError):
    """Recurso inexistente."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class ConflictError(LukatoError):
    """Conflito de estado (ex.: slug ja utilizado)."""

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409


class UnauthorizedError(LukatoError):
    """Credencial ausente ou invalida."""

    code: ClassVar[str] = "unauthorized"
    http_status: ClassVar[int] = 401


class ForbiddenError(LukatoError):
    """Credencial valida, porem sem a permissao exigida."""

    code: ClassVar[str] = "forbidden"
    http_status: ClassVar[int] = 403


class GuardrailViolation(LukatoError):
    """Conteudo bloqueado por uma politica de guardrail (entrada ou saida)."""

    code: ClassVar[str] = "guardrail_violation"
    http_status: ClassVar[int] = 422

    def __init__(
        self,
        message: str,
        *,
        details: Json | None = None,
        policy_id: Id | None = None,
        rule_id: str | None = None,
        stage: str = "input",
    ) -> None:
        merged: Json = dict(details) if details else {}
        merged["policy_id"] = policy_id
        merged["rule_id"] = rule_id
        merged["stage"] = stage
        super().__init__(message, details=merged)
        self.policy_id = policy_id
        self.rule_id = rule_id
        self.stage = stage


class BudgetExceededError(LukatoError):
    """Orcamento FinOps estourado com `hard_stop` ativo."""

    code: ClassVar[str] = "budget_exceeded"
    http_status: ClassVar[int] = 402


class ProviderError(LukatoError):
    """Falha em provedor externo (LLM, embeddings, banco, midia)."""

    code: ClassVar[str] = "provider_error"
    http_status: ClassVar[int] = 502


class RateLimitedError(LukatoError):
    """Provedor externo aplicou limite de taxa."""

    code: ClassVar[str] = "rate_limited"
    http_status: ClassVar[int] = 429


class ModuleError(LukatoError):
    """Falha na execucao de um building block."""

    code: ClassVar[str] = "module_error"
    http_status: ClassVar[int] = 500


class ModuleNotFound(NotFoundError):
    """Building block nao registrado no registry."""

    code: ClassVar[str] = "module_not_found"
    http_status: ClassVar[int] = 404


class ConfigurationError(LukatoError):
    """Configuracao ausente ou incoerente em `Settings`."""

    code: ClassVar[str] = "configuration_error"
    http_status: ClassVar[int] = 500


class UnsupportedCapability(LukatoError):
    """Capacidade opcional indisponivel neste ambiente (ex.: OCR sem PaddleOCR)."""

    code: ClassVar[str] = "unsupported_capability"
    http_status: ClassVar[int] = 501


def _descendants(base: type[LukatoError]) -> Iterator[type[LukatoError]]:
    """Percorre recursivamente as subclasses conhecidas de um erro."""
    for subclass in base.__subclasses__():
        yield subclass
        yield from _descendants(subclass)


ERROR_BY_CODE: dict[str, type[LukatoError]] = {LukatoError.code: LukatoError}
ERROR_BY_CODE.update({subclass.code: subclass for subclass in _descendants(LukatoError)})


def error_for_code(code: str) -> type[LukatoError]:
    """Resolve a classe de erro pelo `code`; devolve :class:`LukatoError` se desconhecido."""
    return ERROR_BY_CODE.get(code, LukatoError)
