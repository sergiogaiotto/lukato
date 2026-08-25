"""Modelos de identidade e autorizacao: papeis, permissoes, usuarios e principals."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from lukato.domain.models.base import DomainModel, Entity
from lukato.domain.types import DEFAULT_TENANT

__all__ = [
    "ROLE_PERMISSIONS",
    "ApiKey",
    "Permission",
    "Principal",
    "Role",
    "User",
    "permissions_for",
]


class Role(StrEnum):
    """Papel atribuido a um usuario ou API key."""

    ROOT = "root"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Permission(StrEnum):
    """Permissao granular verificada pelos casos de uso."""

    MODULE_READ = "module:read"
    MODULE_WRITE = "module:write"
    MODULE_INVOKE = "module:invoke"
    PROMPT_READ = "prompt:read"
    PROMPT_WRITE = "prompt:write"
    GUARDRAIL_READ = "guardrail:read"
    GUARDRAIL_WRITE = "guardrail:write"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    FINOPS_READ = "finops:read"
    FINOPS_WRITE = "finops:write"
    RUN_READ = "run:read"
    ADMIN_ALL = "admin:*"


_ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)
_READ_PERMISSIONS: frozenset[Permission] = frozenset(
    permission for permission in Permission if permission.value.endswith(":read")
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ROOT: _ALL_PERMISSIONS,
    Role.ADMIN: _ALL_PERMISSIONS,
    Role.OPERATOR: _READ_PERMISSIONS
    | frozenset({Permission.MODULE_INVOKE, Permission.KNOWLEDGE_WRITE}),
    Role.VIEWER: _READ_PERMISSIONS,
}
"""Permissoes concedidas por papel (ROOT e ADMIN recebem tudo, inclusive `admin:*`)."""


def permissions_for(role: Role) -> frozenset[Permission]:
    """Devolve as permissoes do papel informado (conjunto vazio se desconhecido)."""
    return ROLE_PERMISSIONS.get(role, frozenset())


class User(Entity):
    """Usuario autenticavel por e-mail e senha."""

    email: str
    name: str
    role: Role = Role.VIEWER
    password_hash: str = ""
    is_active: bool = True
    tenant_id: str = DEFAULT_TENANT


class ApiKey(Entity):
    """Chave de API: apenas o prefixo e o hash do segredo sao persistidos."""

    name: str
    prefix: str
    hashed_secret: str
    role: Role = Role.OPERATOR
    tenant_id: str = DEFAULT_TENANT
    is_active: bool = True
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


class Principal(DomainModel):
    """Identidade resolvida a partir da requisicao (usuario, API key ou anonimo)."""

    subject: str
    role: Role
    tenant_id: str = DEFAULT_TENANT
    kind: str = "user"
    permissions: frozenset[Permission] = frozenset()

    def can(self, permission: Permission) -> bool:
        """True se o principal possui a permissao ou o coringa `admin:*`."""
        return Permission.ADMIN_ALL in self.permissions or permission in self.permissions

    @classmethod
    def anonymous_root(cls, *, tenant_id: str = DEFAULT_TENANT) -> Principal:
        """Principal root anonimo, usado quando a autenticacao esta desligada em dev."""
        return cls(
            subject="anonymous",
            role=Role.ROOT,
            tenant_id=tenant_id,
            kind="anonymous",
            permissions=_ALL_PERMISSIONS,
        )
