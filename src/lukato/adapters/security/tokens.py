"""Emissao e leitura de credenciais: JWT HS256 e chaves de API (SPEC-0006 secao 2).

Dois esquemas convivem, ambos declarados no OpenAPI:

* `Authorization: Bearer <JWT>` — assinado com `LUKATO_SECURITY__JWT_SECRET`,
  contendo `sub`, `role`, `tenant`, `kind`, `iat`, `exp` e `iss="lukato"`.
* `X-API-Key: lk_<prefix>_<secret>` — o prefixo indexa a linha no banco e o segredo
  e conferido contra o `hashed_secret` (bcrypt) pelo caso de uso de identidade.

O token **nao** carrega a lista de permissoes: `decode` reconstroi o `Principal`
sempre a partir de `ROLE_PERMISSIONS[role]`. Assim, mudar o mapa de permissoes de um
papel vale imediatamente para os tokens ja emitidos, e um token adulterado nao pode
pedir permissao que o seu papel nao tem.

Qualquer falha de validacao (assinatura, expiracao, emissor, papel desconhecido)
vira `UnauthorizedError`; nenhuma excecao do PyJWT vaza para a camada HTTP.
"""

from __future__ import annotations

import secrets
import string
from typing import Any, Final

import jwt

from lukato.config import Settings, get_logger
from lukato.domain.errors import ConfigurationError, UnauthorizedError
from lukato.domain.models.identity import Principal, Role, permissions_for
from lukato.domain.types import DEFAULT_TENANT, Json, utcnow

__all__ = [
    "API_KEY_NAMESPACE",
    "API_KEY_PREFIX_ALPHABET",
    "DEFAULT_EXPIRES_SECONDS",
    "DEFAULT_PREFIX_LEN",
    "ISSUER",
    "SECRET_BYTES",
    "JwtTokenService",
    "generate_api_key",
    "split_api_key",
]

_logger = get_logger(__name__)

ISSUER: Final[str] = "lukato"
"""Valor obrigatorio da claim `iss`; tokens de outro emissor sao recusados."""

DEFAULT_EXPIRES_SECONDS: Final[int] = 3600
"""Validade padrao normativa da porta `TokenServicePort.issue`."""

API_KEY_NAMESPACE: Final[str] = "lk"
"""Primeiro segmento de toda chave de API (`lk_<prefix>_<secret>`)."""

_SEPARATOR: Final[str] = "_"
_API_KEY_PARTS: Final[int] = 3

API_KEY_PREFIX_ALPHABET: Final[str] = string.ascii_lowercase + string.digits
"""Alfabeto do prefixo: sem `_`, para que o segredo possa conte-lo sem ambiguidade."""

DEFAULT_PREFIX_LEN: Final[int] = 8
"""Tamanho padrao do prefixo publico que indexa a chave no banco."""

SECRET_BYTES: Final[int] = 32
"""Entropia do segredo (`secrets.token_urlsafe(32)`), conforme SPEC-0006 secao 3."""

MIN_PREFIX_LEN: Final[int] = 4
MAX_PREFIX_LEN: Final[int] = 32
_MAX_RAW_KEY_CHARS: Final[int] = 512

_REQUIRED_CLAIMS: Final[tuple[str, ...]] = ("sub", "role", "iat", "exp", "iss")


class JwtTokenService:
    """Implementa `TokenServicePort` com PyJWT HS256 sobre `Settings.security`."""

    def __init__(self, settings: Settings) -> None:
        """Le segredo, algoritmo e validade padrao da configuracao de seguranca."""
        secret = settings.security.jwt_secret_value
        if not secret.strip():
            raise ConfigurationError(
                "LUKATO_SECURITY__JWT_SECRET esta vazio: sem segredo nao ha como "
                "assinar nem validar tokens (gere um com `openssl rand -hex 32`)",
                details={"setting": "security.jwt_secret"},
            )
        self._secret = secret
        self._algorithm = settings.security.jwt_algorithm
        self._expires_seconds = settings.security.jwt_expires_seconds

    @property
    def algorithm(self) -> str:
        """Algoritmo de assinatura configurado (normalmente `HS256`)."""
        return self._algorithm

    @property
    def expires_seconds(self) -> int:
        """Validade configurada em `LUKATO_SECURITY__JWT_EXPIRES_SECONDS`."""
        return self._expires_seconds

    def issue(self, principal: Principal, *, expires_in: int = DEFAULT_EXPIRES_SECONDS) -> str:
        """Emite um JWT assinado para o principal, valido por `expires_in` segundos."""
        lifetime = max(1, int(expires_in))
        issued_at = int(utcnow().timestamp())
        claims: Json = {
            "sub": principal.subject,
            "role": principal.role.value,
            "tenant": principal.tenant_id,
            "kind": principal.kind,
            "iat": issued_at,
            "exp": issued_at + lifetime,
            "iss": ISSUER,
        }
        return jwt.encode(claims, self._secret, algorithm=self._algorithm)

    def issue_default(self, principal: Principal) -> str:
        """Atalho: emite o token com a validade configurada em `Settings`."""
        return self.issue(principal, expires_in=self._expires_seconds)

    def decode(self, token: str) -> Principal:
        """Valida assinatura, expiracao e emissor, e reconstroi o `Principal`."""
        raw = token.strip() if token else ""
        if not raw:
            raise UnauthorizedError("token de acesso ausente")
        try:
            claims = jwt.decode(
                raw,
                self._secret,
                algorithms=[self._algorithm],
                issuer=ISSUER,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError(
                "token de acesso expirado; faca login novamente",
                details={"reason": "expired"},
            ) from exc
        except jwt.InvalidIssuerError as exc:
            raise UnauthorizedError(
                f"token de acesso emitido por outro servico (esperado iss={ISSUER!r})",
                details={"reason": "invalid_issuer"},
            ) from exc
        except jwt.InvalidTokenError as exc:
            _logger.warning("jwt_rejected", error=type(exc).__name__)
            raise UnauthorizedError(
                "token de acesso invalido",
                details={"reason": "invalid_token", "error": type(exc).__name__},
            ) from exc
        return self._to_principal(claims)

    @staticmethod
    def _to_principal(claims: dict[str, Any]) -> Principal:
        """Converte as claims validadas em `Principal` com as permissoes do papel."""
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            raise UnauthorizedError(
                "token de acesso sem sujeito (`sub`)", details={"reason": "missing_subject"}
            )
        raw_role = str(claims.get("role") or "")
        try:
            role = Role(raw_role)
        except ValueError as exc:
            raise UnauthorizedError(
                f"papel desconhecido no token: {raw_role!r}",
                details={"reason": "unknown_role", "role": raw_role},
            ) from exc
        tenant = str(claims.get("tenant") or DEFAULT_TENANT)
        kind = str(claims.get("kind") or "user")
        return Principal(
            subject=subject,
            role=role,
            tenant_id=tenant,
            kind=kind,
            permissions=permissions_for(role),
        )


def generate_api_key(prefix_len: int = DEFAULT_PREFIX_LEN) -> tuple[str, str, str]:
    """Gera `(chave_completa, prefixo, segredo)` no formato `lk_<prefix>_<secret>`.

    A chave completa e exibida uma unica vez na criacao; o banco guarda apenas o
    prefixo (para indexar a busca) e o hash bcrypt do segredo.
    """
    size = max(MIN_PREFIX_LEN, min(MAX_PREFIX_LEN, int(prefix_len)))
    prefix = "".join(secrets.choice(API_KEY_PREFIX_ALPHABET) for _ in range(size))
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return f"{API_KEY_NAMESPACE}{_SEPARATOR}{prefix}{_SEPARATOR}{secret}", prefix, secret


def split_api_key(raw: str) -> tuple[str, str] | None:
    """Separa `lk_<prefix>_<secret>` em `(prefixo, segredo)`; formato invalido -> `None`."""
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > _MAX_RAW_KEY_CHARS:
        return None
    parts = candidate.split(_SEPARATOR, _API_KEY_PARTS - 1)
    if len(parts) != _API_KEY_PARTS:
        return None
    namespace, prefix, secret = parts
    if namespace != API_KEY_NAMESPACE or not prefix or not secret:
        return None
    if any(char not in API_KEY_PREFIX_ALPHABET for char in prefix):
        return None
    return prefix, secret
