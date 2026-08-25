"""Portas utilitarias: relogio, identificadores, senhas, tokens e cache."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from lukato.domain.models.identity import Principal
from lukato.domain.types import Id

__all__ = [
    "CachePort",
    "ClockPort",
    "IdGeneratorPort",
    "PasswordHasherPort",
    "TokenServicePort",
]


class ClockPort(Protocol):
    """Fonte de tempo injetavel (permite congelar o relogio nos testes)."""

    def now(self) -> datetime:
        """Instante atual, sempre timezone-aware em UTC."""
        ...


class IdGeneratorPort(Protocol):
    """Gerador de identificadores de entidade."""

    def new(self) -> Id:
        """Gera um novo identificador."""
        ...


class PasswordHasherPort(Protocol):
    """Derivacao e verificacao de senhas."""

    def hash(self, password: str) -> str:
        """Devolve o hash da senha em texto (formato do algoritmo escolhido)."""
        ...

    def verify(self, password: str, hashed: str) -> bool:
        """True quando a senha corresponde ao hash informado."""
        ...


class TokenServicePort(Protocol):
    """Emissao e leitura de tokens de acesso."""

    def issue(self, principal: Principal, *, expires_in: int = 3600) -> str:
        """Emite um token assinado para o principal, valido por `expires_in` segundos."""
        ...

    def decode(self, token: str) -> Principal:
        """Valida o token e reconstroi o principal; invalido gera `UnauthorizedError`."""
        ...


class CachePort(Protocol):
    """Cache chave-valor de curta duracao (rate limiting, respostas quentes)."""

    async def get(self, key: str) -> Any | None:
        """Devolve o valor armazenado ou `None` quando ausente ou expirado."""
        ...

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        """Grava o valor, opcionalmente com tempo de vida em segundos."""
        ...

    async def delete(self, key: str) -> None:
        """Remove a chave, se existir."""
        ...

    async def clear(self) -> None:
        """Esvazia o cache inteiro."""
        ...
