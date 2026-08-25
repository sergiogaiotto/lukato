"""Adaptadores de seguranca: senhas (bcrypt), tokens (JWT/API keys) e cache com TTL.

Nenhum destes adaptadores faz rede ou I/O de disco — importar o pacote e barato e
nunca falha por dependencia opcional ausente.
"""

from __future__ import annotations

from lukato.adapters.security.cache import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_NAMESPACE,
    InMemoryCache,
    SlidingWindowRateLimiter,
)
from lukato.adapters.security.hashing import (
    DEFAULT_ROUNDS,
    MAX_ROUNDS,
    MIN_ROUNDS,
    BcryptHasher,
    prehash,
)
from lukato.adapters.security.tokens import (
    API_KEY_NAMESPACE,
    API_KEY_PREFIX_ALPHABET,
    DEFAULT_EXPIRES_SECONDS,
    DEFAULT_PREFIX_LEN,
    ISSUER,
    SECRET_BYTES,
    JwtTokenService,
    generate_api_key,
    split_api_key,
)

__all__ = [
    "API_KEY_NAMESPACE",
    "API_KEY_PREFIX_ALPHABET",
    "DEFAULT_EXPIRES_SECONDS",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_NAMESPACE",
    "DEFAULT_PREFIX_LEN",
    "DEFAULT_ROUNDS",
    "ISSUER",
    "MAX_ROUNDS",
    "MIN_ROUNDS",
    "SECRET_BYTES",
    "BcryptHasher",
    "InMemoryCache",
    "JwtTokenService",
    "SlidingWindowRateLimiter",
    "generate_api_key",
    "prehash",
    "split_api_key",
]
