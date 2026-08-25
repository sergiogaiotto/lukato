"""Schemas do recurso `/api/v1/identity`: login, usuarios e chaves de API.

Regra inviolavel deste modulo: **nenhum segredo volta duas vezes**. O hash de
senha nunca sai; o segredo de uma chave de API aparece uma unica vez, em
:class:`ApiKeyCreatedOut`, no exato momento da criacao ou da rotacao.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from lukato.application.dto import UNSET, Maybe
from lukato.application.use_cases.identity import (
    AccessToken,
    ApiKeyCreated,
    ApiKeyCreateInput,
    LoginInput,
    MeView,
    UserCreateInput,
    UserUpdateInput,
)
from lukato.domain.models.identity import ApiKey, Permission, Role, User
from lukato.domain.types import Id
from lukato.interfaces.http.schemas.common import InSchema, OutSchema

__all__ = [
    "ApiKeyCreate",
    "ApiKeyCreatedOut",
    "ApiKeyOut",
    "ChangePasswordRequest",
    "LoginRequest",
    "MeResponse",
    "RefreshRequest",
    "TokenResponse",
    "UserCreate",
    "UserOut",
    "UserUpdate",
]


# ---------------------------------------------------------------------------
# Autenticacao
# ---------------------------------------------------------------------------
class LoginRequest(InSchema):
    """Corpo de `POST /api/v1/identity/login`."""

    email: str = Field(min_length=3, description="E-mail cadastrado do usuario.")
    password: str = Field(min_length=1, description="Senha em texto claro, sobre TLS.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"email": "operador@lukato.local", "password": "..."}},
    )

    def to_input(self) -> LoginInput:
        """Converte para o DTO do caso de uso `Login`."""
        return LoginInput(email=self.email, password=self.password)


class RefreshRequest(InSchema):
    """Corpo de `POST /api/v1/identity/token/refresh`."""

    token: str = Field(min_length=1, description="JWT ainda valido a ser renovado.")

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"token": "eyJhbGciOiJIUzI1NiIs..."}}
    )


class TokenResponse(OutSchema):
    """Token de acesso emitido para um principal autenticado."""

    access_token: str = Field(description="JWT assinado em HS256.")
    token_type: str = Field(default="Bearer", description="Esquema do cabecalho Authorization.")
    expires_in: int = Field(ge=1, description="Validade do token em segundos.")
    expires_at: datetime = Field(description="Instante de expiracao em UTC.")
    subject: str = Field(description="Identificador do principal.")
    role: Role = Field(description="Papel concedido.")
    tenant_id: str = Field(default="default", description="Inquilino do principal.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIs...",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": "2026-08-25T12:00:00+00:00",
                "subject": "9f2a1b0c-1111-2222-3333-444455556666",
                "role": "operator",
                "tenant_id": "default",
            }
        }
    )

    @classmethod
    def from_result(cls, token: AccessToken) -> TokenResponse:
        """Converte o DTO do caso de uso `Login`/`RefreshToken`."""
        return cls(
            access_token=token.access_token,
            token_type=token.token_type,
            expires_in=token.expires_in,
            expires_at=token.expires_at,
            subject=token.principal.subject,
            role=token.principal.role,
            tenant_id=token.principal.tenant_id,
        )


class UserOut(OutSchema):
    """Usuario devolvido pela API — **sem** o hash de senha."""

    id: Id
    email: str
    name: str = ""
    role: Role = Role.VIEWER
    is_active: bool = True
    tenant_id: str = "default"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, user: User) -> UserOut:
        """Converte a entidade de dominio descartando `password_hash`."""
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            is_active=user.is_active,
            tenant_id=user.tenant_id,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )


class MeResponse(OutSchema):
    """Identidade corrente devolvida por `GET /api/v1/identity/me`."""

    subject: str = Field(description="Identificador do principal resolvido.")
    role: Role = Field(description="Papel efetivo.")
    tenant_id: str = Field(default="default", description="Inquilino do principal.")
    kind: str = Field(default="user", description="Origem: `user`, `api_key` ou `anonymous`.")
    permissions: list[Permission] = Field(
        default_factory=list, description="Permissoes efetivas, em ordem alfabetica."
    )
    user: UserOut | None = Field(
        default=None, description="Usuario correspondente, quando a origem for `user`."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "subject": "anonymous",
                "role": "root",
                "tenant_id": "default",
                "kind": "anonymous",
                "permissions": ["admin:*", "module:read"],
                "user": None,
            }
        }
    )

    @classmethod
    def from_result(cls, view: MeView) -> MeResponse:
        """Converte o DTO do caso de uso `GetMe`."""
        return cls(
            subject=view.principal.subject,
            role=view.principal.role,
            tenant_id=view.principal.tenant_id,
            kind=view.principal.kind,
            permissions=[Permission(value) for value in view.permissions],
            user=UserOut.from_domain(view.user) if view.user is not None else None,
        )


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------
class UserCreate(InSchema):
    """Corpo de `POST /api/v1/identity/users`."""

    email: str = Field(min_length=3, description="E-mail unico do usuario.")
    password: str = Field(min_length=8, description="Senha inicial (minimo de 8 caracteres).")
    name: str = Field(default="", description="Nome exibido no console.")
    role: Role = Field(default=Role.VIEWER, description="Papel concedido.")
    tenant_id: str = Field(default="default", description="Inquilino do usuario.")
    is_active: bool = Field(default=True, description="Usuario inativo nao autentica.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "email": "operador@lukato.local",
                "password": "troque-esta-senha",
                "name": "Operador",
                "role": "operator",
                "tenant_id": "default",
                "is_active": True,
            }
        },
    )

    def to_input(self) -> UserCreateInput:
        """Converte para o DTO do caso de uso `CreateUser`."""
        return UserCreateInput(
            email=self.email,
            password=self.password,
            name=self.name,
            role=self.role,
            tenant_id=self.tenant_id,
            is_active=self.is_active,
        )


class UserUpdate(InSchema):
    """Corpo de `PUT /api/v1/identity/users/{id}`.

    A senha nao entra aqui: troca-la e operacao propria, com regra de autorizacao
    diferente (`POST /users/{id}/password`).
    """

    email: str | None = None
    name: str | None = None
    role: Role | None = None
    tenant_id: str | None = None
    is_active: bool | None = None

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"example": {"role": "viewer", "is_active": False}}
    )

    def to_input(self) -> UserUpdateInput:
        """Converte para o DTO parcial, preservando o que nao foi enviado."""
        sent = self.model_fields_set

        def maybe(field: str, value: Any) -> Maybe[Any]:
            return value if field in sent else UNSET

        return UserUpdateInput(
            email=maybe("email", self.email),
            name=maybe("name", self.name),
            role=maybe("role", self.role),
            tenant_id=maybe("tenant_id", self.tenant_id),
            is_active=maybe("is_active", self.is_active),
        )


class ChangePasswordRequest(InSchema):
    """Corpo de `POST /api/v1/identity/users/{id}/password`."""

    new_password: str = Field(min_length=8, description="Nova senha do usuario.")
    current_password: str | None = Field(
        default=None, description="Senha atual; exigida quando o proprio usuario troca a sua."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"new_password": "nova-senha-forte", "current_password": "senha-antiga"}
        },
    )


# ---------------------------------------------------------------------------
# Chaves de API
# ---------------------------------------------------------------------------
class ApiKeyCreate(InSchema):
    """Corpo de `POST /api/v1/identity/api-keys`."""

    name: str = Field(min_length=1, description="Para que serve esta chave.")
    role: Role = Field(default=Role.OPERATOR, description="Papel concedido a chave.")
    tenant_id: str = Field(default="default", description="Inquilino da chave.")
    expires_at: datetime | None = Field(
        default=None, description="Expiracao; ausente cria chave sem prazo."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "name": "integracao-billing",
                "role": "operator",
                "tenant_id": "default",
                "expires_at": "2027-01-01T00:00:00+00:00",
            }
        },
    )

    def to_input(self) -> ApiKeyCreateInput:
        """Converte para o DTO do caso de uso `CreateApiKey`."""
        return ApiKeyCreateInput(
            name=self.name,
            role=self.role,
            tenant_id=self.tenant_id,
            expires_at=self.expires_at,
        )


class ApiKeyOut(OutSchema):
    """Chave de API devolvida na listagem — **sem** o segredo."""

    id: Id
    name: str
    prefix: str = Field(description="Prefixo publico que indexa a chave.")
    role: Role = Role.OPERATOR
    tenant_id: str = "default"
    is_active: bool = True
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, key: ApiKey) -> ApiKeyOut:
        """Converte a entidade de dominio descartando `hashed_secret`."""
        return cls(
            id=key.id,
            name=key.name,
            prefix=key.prefix,
            role=key.role,
            tenant_id=key.tenant_id,
            is_active=key.is_active,
            expires_at=key.expires_at,
            last_used_at=key.last_used_at,
            created_at=key.created_at,
            updated_at=key.updated_at,
        )


class ApiKeyCreatedOut(ApiKeyOut):
    """Chave recem-criada ou rotacionada: **unica** resposta que carrega o segredo.

    O segredo nao e persistido nem recuperavel — o banco guarda apenas o prefixo e
    o hash bcrypt. Quem nao anotar agora precisa rotacionar a chave.
    """

    secret: str = Field(description="Chave completa `lk_<prefix>_<secret>`, exibida uma unica vez.")
    warning: str = Field(
        default="Guarde a chave agora: ela nao sera exibida novamente.",
        description="Aviso exibido pelo console junto do segredo.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "9f2a1b0c-1111-2222-3333-444455556666",
                "name": "integracao-billing",
                "prefix": "a1b2c3d4",
                "role": "operator",
                "tenant_id": "default",
                "is_active": True,
                "expires_at": None,
                "last_used_at": None,
                "secret": "lk_a1b2c3d4_XcQ...",
                "warning": "Guarde a chave agora: ela nao sera exibida novamente.",
            }
        }
    )

    @classmethod
    def from_result(cls, created: ApiKeyCreated) -> ApiKeyCreatedOut:
        """Converte o DTO do caso de uso `CreateApiKey`/`RotateApiKey`."""
        base = ApiKeyOut.from_domain(created.api_key)
        return cls(**base.model_dump(), secret=created.secret)
