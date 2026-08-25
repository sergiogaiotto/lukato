"""Casos de uso de identidade, autenticacao e autorizacao (SPEC-0006).

Este modulo concentra tudo o que decide **quem** esta chamando a plataforma:
login por e-mail e senha, emissao e renovacao de JWT, CRUD de usuarios, ciclo de
vida das chaves de API e a resolucao do `Principal` a partir de uma credencial.

Tres invariantes de seguranca governam o arquivo inteiro:

* **Nao vazar existencia de conta.** E-mail inexistente, senha errada e conta
  desativada produzem exatamente a mesma :class:`UnauthorizedError`, com a mesma
  mensagem e sem `details` distintivos. O motivo real vai apenas para o log
  estruturado. Alem da mensagem, o *tempo* tambem e igualado: quando a conta nao
  existe, a senha e conferida contra um hash chamariz, para que o relogio do
  atacante nao responda o que a mensagem se recusa a dizer.
* **O segredo aparece uma unica vez.** :class:`CreateApiKey` e
  :class:`RotateApiKey` sao os unicos pontos que devolvem o texto da chave, e o
  fazem num DTO proprio (:class:`ApiKeyCreated`). Toda leitura de chave passa por
  ``_public``, que apaga tambem o `hashed_secret` — um hash bcrypt vazado e um
  ataque offline pronto, entao ele nunca sai da camada de aplicacao.
* **Autorizar e sempre `Principal.can`.** Nenhum caso de uso compara papeis: a
  administracao de identidade exige :data:`Permission.ADMIN_ALL`, e as excecoes
  de auto-atendimento (ler-se, trocar a propria senha) sao explicitas.

A comparacao de segredos usa :func:`secrets.compare_digest` e o `verify` do
hasher injetado (bcrypt, constante por construcao). Verificacoes de senha ficam
**fora** do contexto transacional: bcrypt custa centenas de milissegundos e nao
pode segurar uma conexao do pool enquanto roda.
"""

from __future__ import annotations

import re
import secrets
import string
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar, cast

from lukato.application.container import Container
from lukato.application.dto import (
    DEFAULT_PAGE_LIMIT,
    UNSET,
    Maybe,
    Page,
    PageRequest,
    is_set,
)
from lukato.application.use_cases.modules import authorize
from lukato.config import get_logger
from lukato.domain.errors import (
    ConflictError,
    LukatoError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from lukato.domain.models.identity import (
    ApiKey,
    Permission,
    Principal,
    Role,
    User,
    permissions_for,
)
from lukato.domain.ports.misc import PasswordHasherPort
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.types import DEFAULT_TENANT, Id, Json, utcnow

__all__ = [
    "API_KEY_NAMESPACE",
    "API_KEY_PREFIX_ALPHABET",
    "API_KEY_PREFIX_LENGTH",
    "API_KEY_SECRET_BYTES",
    "BEARER",
    "IDENTITY_PERMISSION",
    "INVALID_CREDENTIALS",
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "SELF_REFERENCE",
    "AccessToken",
    "ApiKeyCreateInput",
    "ApiKeyCreated",
    "ApiKeyFilter",
    "AuthenticateApiKey",
    "AuthenticateToken",
    "ChangePassword",
    "ChangePasswordInput",
    "CreateApiKey",
    "CreateUser",
    "DeleteUser",
    "EnsureBootstrapAdmin",
    "GetMe",
    "GetUser",
    "ListApiKeys",
    "ListUsers",
    "Login",
    "LoginInput",
    "MeView",
    "RefreshToken",
    "RevokeApiKey",
    "RotateApiKey",
    "UpdateUser",
    "UserCreateInput",
    "UserFilter",
    "UserUpdateInput",
    "build_api_key",
    "principal_for",
    "split_api_key",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

IDENTITY_PERMISSION: Final[Permission] = Permission.ADMIN_ALL
"""Permissao exigida para administrar usuarios e chaves (root e admin a possuem)."""

INVALID_CREDENTIALS: Final[str] = "Credenciais invalidas."
"""Mensagem unica de falha de autenticacao: nunca revela *qual* fator falhou."""

BEARER: Final[str] = "bearer"
"""Valor do campo `token_type` na resposta de login (RFC 6750)."""

SELF_REFERENCE: Final[str] = "me"
"""Apelido aceito no lugar de um id para referenciar o proprio principal."""

MIN_PASSWORD_CHARS: Final[int] = 8
"""Piso de tamanho da senha; abaixo disso nem o bcrypt salva a conta."""

MAX_PASSWORD_CHARS: Final[int] = 1024
"""Teto de tamanho da senha: evita gastar CPU com entradas absurdas."""

API_KEY_NAMESPACE: Final[str] = "lk"
"""Primeiro segmento de toda chave de API (`lk_<prefix>_<secret>`)."""

API_KEY_PREFIX_ALPHABET: Final[str] = string.ascii_lowercase + string.digits
"""Alfabeto do prefixo: sem `_`, para que o segredo possa conte-lo sem ambiguidade."""

API_KEY_PREFIX_LENGTH: Final[int] = 8
"""Tamanho do prefixo publico que indexa a chave no banco."""

API_KEY_SECRET_BYTES: Final[int] = 32
"""Entropia do segredo (`secrets.token_urlsafe(32)`), conforme SPEC-0006 secao 3."""

_SEPARATOR: Final[str] = "_"
_API_KEY_PARTS: Final[int] = 3
_MAX_RAW_KEY_CHARS: Final[int] = 512
_PREFIX_ATTEMPTS: Final[int] = 8
_DECOY_CACHE_SIZE: Final[int] = 4
_MATCH: Final[bytes] = b"lukato-secret-match"

_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Utilitarios internos
# ---------------------------------------------------------------------------
def _coerce(data: Any, factory: type[_T], *, what: str) -> _T:
    """Aceita o DTO ja montado ou o objeto JSON cru vindo da borda HTTP/UI.

    Chave desconhecida vira :class:`ValidationError` em vez de ser ignorada em
    silencio: um `role` escrito errado nao pode virar, sem aviso, um usuario com
    o papel padrao.
    """
    if isinstance(data, factory):
        return data
    if isinstance(data, Mapping):
        known = {item.name for item in fields(cast(Any, factory))}
        unknown = sorted(str(key) for key in data if str(key) not in known)
        if unknown:
            raise ValidationError(
                f"Campos desconhecidos em {what}: {', '.join(unknown)}.",
                details={"unknown": unknown, "supported": sorted(known)},
            )
        return factory(**{str(key): value for key, value in data.items()})
    raise ValidationError(
        f"{what} deve ser um objeto JSON ou um {factory.__name__}.",
        details={"received": type(data).__name__},
    )


def _unauthorized(reason: str, **context: Any) -> UnauthorizedError:
    """Monta a falha de autenticacao generica e registra o motivo real no log.

    A mensagem e sempre :data:`INVALID_CREDENTIALS` e `details` nunca carrega o
    motivo: quem responde 401 na borda nao pode, sem querer, contar ao cliente
    se o e-mail existe, se a senha errou ou se a conta esta desativada.
    """
    _logger.info("authentication_failed", reason=reason, **context)
    return UnauthorizedError(INVALID_CREDENTIALS)


_DECOY_HASHES: dict[str, str] = {}
"""Hashes chamariz ja calculados, por perfil de custo do hasher."""


def _decoy_profile(hasher: PasswordHasherPort) -> str:
    """Chave de cache: o que determina o CUSTO do hash, nao a instancia.

    O chamariz precisa custar o mesmo que uma verificacao real, e esse custo
    depende do algoritmo e do fator de trabalho — nao do objeto. Cachear por
    instancia (como fazia o `lru_cache`) recalcularia um bcrypt inteiro a cada
    novo container, o que pesa na suite de testes sem ganho nenhum.
    """
    tipo = type(hasher)
    custo = getattr(hasher, "rounds", "")
    return f"{tipo.__module__}.{tipo.__qualname__}:{custo}"


def _decoy_hash(hasher: PasswordHasherPort) -> str:
    """Hash chamariz de um segredo aleatorio, calculado uma vez por perfil de custo.

    Serve para gastar o mesmo tempo de bcrypt quando a conta pedida nao existe.
    Sem ele, "e-mail desconhecido" responderia em microssegundos e "senha errada"
    em centenas de milissegundos — a diferenca e um oraculo de enumeracao de
    usuarios tao eficaz quanto uma mensagem de erro distinta.
    """
    chave = _decoy_profile(hasher)
    cached = _DECOY_HASHES.get(chave)
    if cached is None:
        cached = hasher.hash(secrets.token_urlsafe(API_KEY_SECRET_BYTES))
        if len(_DECOY_HASHES) >= _DECOY_CACHE_SIZE:
            _DECOY_HASHES.clear()
        _DECOY_HASHES[chave] = cached
    return cached


def _verify_secret(hasher: PasswordHasherPort, secret: str, hashed: str) -> bool:
    """Confere o segredo contra o hash armazenado em tempo constante.

    O `verify` do hasher (bcrypt) ja compara sem curto-circuito; o
    :func:`secrets.compare_digest` final garante que tambem a decisao booleana
    devolvida por esta funcao seja tomada sobre buffers de tamanho fixo.
    """
    matched = bool(hashed) and hasher.verify(secret, hashed)
    return secrets.compare_digest(_MATCH if matched else b"", _MATCH)


def _burn_decoy(hasher: PasswordHasherPort, secret: str) -> None:
    """Executa a verificacao chamariz para igualar o tempo do caminho de falha."""
    _verify_secret(hasher, secret, _decoy_hash(hasher))


def _as_aware(moment: datetime | None) -> datetime | None:
    """Normaliza para UTC ciente de fuso; SQLite devolve carimbos ingenuos."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _normalize_email(raw: str) -> str:
    """Valida e normaliza o e-mail (minusculas, sem espacos nas pontas)."""
    candidate = (raw or "").strip().lower()
    if not _EMAIL_PATTERN.match(candidate):
        raise ValidationError(
            "E-mail invalido: informe um endereco no formato 'nome@dominio.tld'.",
            details={"field": "email"},
        )
    return candidate


def _validate_password(raw: str) -> str:
    """Exige uma senha dentro dos limites de tamanho aceitos."""
    candidate = raw or ""
    if not MIN_PASSWORD_CHARS <= len(candidate) <= MAX_PASSWORD_CHARS:
        raise ValidationError(
            f"A senha deve ter entre {MIN_PASSWORD_CHARS} e {MAX_PASSWORD_CHARS} caracteres.",
            details={"field": "password", "min": MIN_PASSWORD_CHARS, "max": MAX_PASSWORD_CHARS},
        )
    return candidate


def _as_role(value: Role | str) -> Role:
    """Converte texto em :class:`Role`; papel desconhecido vira `ValidationError`."""
    if isinstance(value, Role):
        return value
    try:
        return Role(str(value).strip().lower())
    except ValueError as exc:
        raise ValidationError(
            f"Papel desconhecido: {value!r}.",
            details={"field": "role", "supported": [role.value for role in Role]},
        ) from exc


def principal_for(user: User) -> Principal:
    """Monta o `Principal` de um usuario com as permissoes vigentes do seu papel.

    As permissoes vem sempre de `ROLE_PERMISSIONS`, nunca de algo persistido:
    mudar o mapa de um papel vale na hora para todos que ja estao logados.
    """
    return Principal(
        subject=user.id,
        role=user.role,
        tenant_id=user.tenant_id,
        kind="user",
        permissions=permissions_for(user.role),
    )


def _principal_for_key(api_key: ApiKey) -> Principal:
    """Monta o `Principal` de uma chave de API (o sujeito e o id da chave)."""
    return Principal(
        subject=api_key.id,
        role=api_key.role,
        tenant_id=api_key.tenant_id,
        kind="api_key",
        permissions=permissions_for(api_key.role),
    )


def build_api_key(prefix_length: int = API_KEY_PREFIX_LENGTH) -> tuple[str, str, str]:
    """Gera `(chave_completa, prefixo, segredo)` no formato `lk_<prefix>_<secret>`."""
    size = max(1, int(prefix_length))
    prefix = "".join(secrets.choice(API_KEY_PREFIX_ALPHABET) for _ in range(size))
    secret = secrets.token_urlsafe(API_KEY_SECRET_BYTES)
    return f"{API_KEY_NAMESPACE}{_SEPARATOR}{prefix}{_SEPARATOR}{secret}", prefix, secret


def split_api_key(raw: str) -> tuple[str, str] | None:
    """Separa `lk_<prefix>_<secret>` em `(prefixo, segredo)`; formato invalido -> `None`.

    O corte usa `maxsplit=2` porque o segredo (`token_urlsafe`) pode conter `_`;
    somente os dois primeiros separadores delimitam namespace e prefixo.
    """
    candidate = (raw or "").strip()
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


def _public(api_key: ApiKey) -> ApiKey:
    """Copia da chave sem o `hashed_secret`, unica forma que deixa a aplicacao.

    O hash nunca e material de exibicao: entregue a um cliente, ele vira alvo de
    quebra offline. Apaga-lo aqui protege qualquer borda que serialize o modelo
    inteiro por descuido.
    """
    return api_key.model_copy(update={"hashed_secret": ""})


async def _find_user(uow: UnitOfWork, reference: str) -> User | None:
    """Resolve o usuario por identificador e, em seguida, por e-mail."""
    candidate = (reference or "").strip()
    if not candidate:
        return None
    by_id = await uow.users.get(candidate)
    if by_id is not None:
        return by_id
    if "@" not in candidate:
        return None
    return await uow.users.get_by_email(candidate)


async def _require_user(uow: UnitOfWork, reference: str) -> User:
    """Resolve o usuario ou levanta :class:`NotFoundError`."""
    found = await _find_user(uow, reference)
    if found is None:
        raise NotFoundError(
            f"Usuario '{reference}' nao encontrado.",
            details={"reference": reference},
        )
    return found


async def _require_api_key(uow: UnitOfWork, api_key_id: Id) -> ApiKey:
    """Resolve a chave por identificador ou levanta :class:`NotFoundError`."""
    found = await uow.api_keys.get((api_key_id or "").strip())
    if found is None:
        raise NotFoundError(
            f"Chave de API '{api_key_id}' nao encontrada.",
            details={"api_key_id": api_key_id},
        )
    return found


async def _active_roots(uow: UnitOfWork) -> int | None:
    """Conta os `root` ativos; `None` quando o repositorio nao aceita o filtro.

    A contagem sustenta a trava de "ultimo root": derrubar o unico administrador
    absoluto deixaria a instalacao sem ninguem capaz de recria-lo. Um repositorio
    que nao suporte o filtro simplesmente nao habilita a trava, em vez de
    quebrar a operacao.
    """
    try:
        return int(await uow.users.count(role=Role.ROOT, is_active=True))
    except (TypeError, LukatoError):  # pragma: no cover - repositorio sem o filtro
        return None


async def _guard_last_root(uow: UnitOfWork, user: User, *, action: str) -> None:
    """Impede remover ou rebaixar o ultimo `root` ativo da instalacao."""
    if user.role is not Role.ROOT or not user.is_active:
        return
    remaining = await _active_roots(uow)
    if remaining is not None and remaining <= 1:
        raise ConflictError(
            f"Nao e possivel {action} o unico usuario root ativo: a instalacao "
            "ficaria sem administrador. Promova outro usuario a root antes.",
            details={"user_id": user.id, "active_roots": remaining},
        )


async def _unique_key(uow: UnitOfWork) -> tuple[str, str, str]:
    """Sorteia uma chave cujo prefixo ainda nao esteja em uso."""
    for _ in range(_PREFIX_ATTEMPTS):
        raw, prefix, secret = build_api_key()
        if await uow.api_keys.get_by_prefix(prefix) is None:
            return raw, prefix, secret
    raise ConflictError(  # pragma: no cover - exige colisao repetida em 36^8
        "Nao foi possivel sortear um prefixo livre para a chave de API.",
        details={"attempts": _PREFIX_ATTEMPTS},
    )


def _guard_not_self(principal: Principal, user: User, *, action: str) -> None:
    """Impede que o principal se auto-desative ou se apague (trava de porta)."""
    if principal.subject == user.id:
        raise ConflictError(
            f"Um usuario nao pode {action} a si mesmo; peca a outro administrador.",
            details={"user_id": user.id},
        )


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LoginInput:
    """Credenciais apresentadas em `POST /api/v1/identity/login`."""

    email: str
    password: str


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Token de acesso emitido para um principal autenticado."""

    access_token: str
    principal: Principal
    expires_in: int
    token_type: str = BEARER
    issued_at: datetime = field(default_factory=utcnow)

    @property
    def expires_at(self) -> datetime:
        """Instante de expiracao do token."""
        return self.issued_at + timedelta(seconds=self.expires_in)

    def to_dict(self) -> Json:
        """Serializa no formato OAuth 2.0 esperado pela borda HTTP."""
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "expires_at": self.expires_at.isoformat(),
            "subject": self.principal.subject,
            "role": self.principal.role.value,
            "tenant_id": self.principal.tenant_id,
        }


@dataclass(frozen=True, slots=True)
class MeView:
    """Identidade corrente devolvida por `GET /api/v1/identity/me`."""

    principal: Principal
    user: User | None = None

    @property
    def permissions(self) -> list[str]:
        """Permissoes efetivas do principal, em ordem alfabetica."""
        return sorted(permission.value for permission in self.principal.permissions)

    def to_dict(self) -> Json:
        """Serializa a identidade sem jamais incluir o hash de senha."""
        payload: Json = {
            "subject": self.principal.subject,
            "role": self.principal.role.value,
            "tenant_id": self.principal.tenant_id,
            "kind": self.principal.kind,
            "permissions": self.permissions,
        }
        if self.user is not None:
            payload["user"] = {
                "id": self.user.id,
                "email": self.user.email,
                "name": self.user.name,
                "is_active": self.user.is_active,
            }
        return payload


@dataclass(frozen=True, slots=True)
class UserCreateInput:
    """Dados de criacao de um usuario."""

    email: str
    password: str
    name: str = ""
    role: Role | str = Role.VIEWER
    tenant_id: str = DEFAULT_TENANT
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class UserUpdateInput:
    """Atualizacao parcial de um usuario; campos ausentes ficam :data:`UNSET`.

    A senha **nao** entra aqui: troca-la e uma operacao propria
    (:class:`ChangePassword`), com regra de autorizacao diferente.
    """

    email: Maybe[str] = UNSET
    name: Maybe[str] = UNSET
    role: Maybe[Role | str] = UNSET
    tenant_id: Maybe[str] = UNSET
    is_active: Maybe[bool] = UNSET


@dataclass(frozen=True, slots=True)
class UserFilter:
    """Janela de listagem de usuarios (a porta `UserRepository` pagina sem filtros)."""

    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao pelos limites normativos."""
        window = PageRequest(limit=self.limit, offset=self.offset)
        object.__setattr__(self, "limit", window.limit)
        object.__setattr__(self, "offset", window.offset)

    @property
    def page(self) -> PageRequest:
        """Janela de paginacao correspondente a este filtro."""
        return PageRequest(limit=self.limit, offset=self.offset)


@dataclass(frozen=True, slots=True)
class ChangePasswordInput:
    """Pedido de troca de senha (proprio ou administrativo)."""

    user_id: str
    new_password: str
    current_password: str | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyCreateInput:
    """Dados de criacao de uma chave de API."""

    name: str
    role: Role | str = Role.OPERATOR
    tenant_id: str = DEFAULT_TENANT
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApiKeyCreated:
    """Chave recem-criada ou rotacionada: **unico** objeto que carrega o segredo.

    `secret` traz a chave completa (`lk_<prefix>_<secret>`), no formato que o
    cliente deve enviar no cabecalho. Ela nao e persistida nem recuperavel: o
    banco guarda apenas o prefixo e o hash bcrypt.
    """

    api_key: ApiKey
    secret: str

    def to_dict(self) -> Json:
        """Serializa a resposta de criacao — a unica que expoe o segredo."""
        return {
            "id": self.api_key.id,
            "name": self.api_key.name,
            "prefix": self.api_key.prefix,
            "role": self.api_key.role.value,
            "tenant_id": self.api_key.tenant_id,
            "is_active": self.api_key.is_active,
            "expires_at": (
                self.api_key.expires_at.isoformat() if self.api_key.expires_at else None
            ),
            "secret": self.secret,
            "warning": "Guarde a chave agora: ela nao sera exibida novamente.",
        }


@dataclass(frozen=True, slots=True)
class ApiKeyFilter:
    """Filtros de listagem de chaves de API."""

    is_active: bool | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao pelos limites normativos."""
        window = PageRequest(limit=self.limit, offset=self.offset)
        object.__setattr__(self, "limit", window.limit)
        object.__setattr__(self, "offset", window.offset)

    @property
    def page(self) -> PageRequest:
        """Janela de paginacao correspondente a este filtro."""
        return PageRequest(limit=self.limit, offset=self.offset)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _IdentityUseCase:
    """Base dos casos de uso de identidade: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container

    @property
    def _hasher(self) -> PasswordHasherPort:
        """Hasher de senhas injetado pelo composition root."""
        return self._container.hasher

    @property
    def _expires_in(self) -> int:
        """Validade configurada dos tokens emitidos."""
        return int(self._container.settings.security.jwt_expires_seconds)

    def _issue(self, principal: Principal) -> AccessToken:
        """Emite o JWT do principal com a validade configurada."""
        expires_in = self._expires_in
        token = self._container.tokens.issue(principal, expires_in=expires_in)
        return AccessToken(access_token=token, principal=principal, expires_in=expires_in)


# ---------------------------------------------------------------------------
# Autenticacao
# ---------------------------------------------------------------------------
class Login(_IdentityUseCase):
    """Autentica por e-mail e senha e emite um JWT (SPEC-0006 secao 2)."""

    async def execute(self, data: LoginInput | Mapping[str, Any]) -> AccessToken:
        """Devolve o token do usuario; qualquer falha e a mesma 401 generica.

        A ordem importa: a senha e conferida **antes** de olhar `is_active`, de
        modo que uma conta desativada so seja distinguivel por quem ja provou
        conhecer a senha — e, ainda assim, a resposta continua identica.
        """
        payload = _coerce(data, LoginInput, what="as credenciais de login")
        email = (payload.email or "").strip().lower()
        password = payload.password or ""
        if not email or not password:
            raise _unauthorized("empty_credentials")

        async with self._container.uow_factory() as uow:
            user = await uow.users.get_by_email(email)

        if user is None:
            _burn_decoy(self._hasher, password)
            raise _unauthorized("unknown_email")
        if not _verify_secret(self._hasher, password, user.password_hash):
            raise _unauthorized("wrong_password", user_id=user.id)
        if not user.is_active:
            raise _unauthorized("inactive_user", user_id=user.id)

        issued = self._issue(principal_for(user))
        _logger.info("login_succeeded", user_id=user.id, role=user.role.value)
        return issued


class RefreshToken(_IdentityUseCase):
    """Troca um JWT valido por outro, reconferindo o usuario no banco."""

    async def execute(self, token: str) -> AccessToken:
        """Renova o token; conta removida, desativada ou de outro tipo -> 401.

        Reler o usuario e o que faz a revogacao valer: um papel rebaixado ou uma
        conta desligada param de render tokens novos no primeiro refresh, mesmo
        com o token antigo ainda dentro da validade.
        """
        principal = self._container.tokens.decode(token)
        if principal.kind != "user":
            raise _unauthorized("refresh_not_a_user_token", kind=principal.kind)

        async with self._container.uow_factory() as uow:
            user = await uow.users.get(principal.subject)

        if user is None:
            raise _unauthorized("refresh_unknown_user", subject=principal.subject)
        if not user.is_active:
            raise _unauthorized("refresh_inactive_user", user_id=user.id)
        return self._issue(principal_for(user))


class AuthenticateToken(_IdentityUseCase):
    """Resolve o `Principal` de um `Authorization: Bearer <jwt>`."""

    async def execute(self, token: str, *, check_user: bool = True) -> Principal:
        """Valida o token e devolve o principal ja com as permissoes do papel.

        Com `check_user=True` (padrao) o usuario e reconferido no banco a cada
        requisicao: e o preco de tornar desativacao e rebaixamento imediatos, em
        vez de esperarem a expiracao do token.
        """
        principal = self._container.tokens.decode(token)
        if not check_user or principal.kind != "user":
            return principal

        async with self._container.uow_factory() as uow:
            user = await uow.users.get(principal.subject)

        if user is None:
            raise _unauthorized("token_unknown_user", subject=principal.subject)
        if not user.is_active:
            raise _unauthorized("token_inactive_user", user_id=user.id)
        return principal_for(user)


class AuthenticateApiKey(_IdentityUseCase):
    """Resolve o `Principal` de um cabecalho `X-API-Key: lk_<prefix>_<secret>`."""

    async def execute(self, raw: str) -> Principal:
        """Valida formato, segredo, atividade e validade; carimba o ultimo uso.

        O prefixo apenas *indexa* a linha — quem autentica e o segredo, conferido
        contra o hash bcrypt. Chave inativa ou expirada devolve a mesma 401 de
        chave inexistente (SPEC-0006 secao 6, criterio 2).
        """
        parts = split_api_key(raw)
        if parts is None:
            raise _unauthorized("api_key_malformed")
        prefix, secret = parts

        async with self._container.uow_factory() as uow:
            api_key = await uow.api_keys.get_by_prefix(prefix)

        if api_key is None:
            _burn_decoy(self._hasher, secret)
            raise _unauthorized("api_key_unknown_prefix", prefix=prefix)
        if not _verify_secret(self._hasher, secret, api_key.hashed_secret):
            raise _unauthorized("api_key_wrong_secret", api_key_id=api_key.id)
        if not api_key.is_active:
            raise _unauthorized("api_key_revoked", api_key_id=api_key.id)

        now = utcnow()
        expires_at = _as_aware(api_key.expires_at)
        if expires_at is not None and expires_at <= now:
            raise _unauthorized("api_key_expired", api_key_id=api_key.id)

        await self._touch(api_key.id, now)
        return _principal_for_key(api_key)

    async def _touch(self, api_key_id: Id, when: datetime) -> None:
        """Registra `last_used_at` sem deixar a auditoria derrubar o login.

        A autenticacao ja foi decidida quando chegamos aqui; uma falha ao gravar
        o carimbo e um problema de telemetria, nao de credencial.
        """
        try:
            async with self._container.uow_factory() as uow:
                await uow.api_keys.touch(api_key_id, when)
                await uow.commit()
        except LukatoError as exc:
            _logger.warning(
                "api_key_touch_failed",
                api_key_id=api_key_id,
                error=f"{type(exc).__name__}: {exc}",
            )


class GetMe(_IdentityUseCase):
    """Descreve a identidade corrente para o console e para `GET /me`."""

    async def execute(self, principal: Principal) -> MeView:
        """Devolve principal, permissoes e — quando houver — o usuario do banco."""
        if principal.kind != "user":
            return MeView(principal=principal)
        async with self._container.uow_factory() as uow:
            user = await uow.users.get(principal.subject)
        return MeView(principal=principal, user=user)


# ---------------------------------------------------------------------------
# CRUD de usuarios
# ---------------------------------------------------------------------------
class CreateUser(_IdentityUseCase):
    """Cria um usuario autenticavel."""

    async def execute(
        self, data: UserCreateInput | Mapping[str, Any], principal: Principal
    ) -> User:
        """Grava o usuario; e-mail ja usado levanta :class:`ConflictError`."""
        authorize(principal, IDENTITY_PERMISSION, "criar usuarios")
        payload = _coerce(data, UserCreateInput, what="a criacao de usuario")
        email = _normalize_email(payload.email)
        password = _validate_password(payload.password)
        role = _as_role(payload.role)

        user = User(
            email=email,
            name=(payload.name or "").strip() or email.split("@", 1)[0],
            role=role,
            password_hash=self._hasher.hash(password),
            is_active=bool(payload.is_active),
            tenant_id=(payload.tenant_id or DEFAULT_TENANT).strip() or DEFAULT_TENANT,
        )
        async with self._container.uow_factory() as uow:
            if await uow.users.get_by_email(email) is not None:
                raise ConflictError(
                    f"Ja existe um usuario com o e-mail '{email}'.",
                    details={"email": email},
                )
            created = await uow.users.add(user)
            await uow.commit()
        _logger.info("user_created", user_id=created.id, role=created.role.value)
        return created


class GetUser(_IdentityUseCase):
    """Le um usuario por identificador, por e-mail ou pelo apelido `me`."""

    async def execute(self, reference: str, principal: Principal) -> User:
        """Devolve o usuario; ler outro exige `admin:*`, ler-se nao exige nada.

        A autorizacao vem **antes** do 404 para quem nao e o proprio usuario:
        do contrario a diferenca entre "nao encontrado" e "sem permissao" viraria
        um verificador de e-mails cadastrados aberto a qualquer `viewer`.
        """
        candidate = (reference or "").strip()
        if candidate.lower() == SELF_REFERENCE:
            candidate = principal.subject
        async with self._container.uow_factory() as uow:
            found = await _find_user(uow, candidate)
        if found is None or found.id != principal.subject:
            authorize(principal, IDENTITY_PERMISSION, "ler outros usuarios")
        if found is None:
            raise NotFoundError(
                f"Usuario '{reference}' nao encontrado.",
                details={"reference": reference},
            )
        return found


class ListUsers(_IdentityUseCase):
    """Lista usuarios paginados, do mais recente para o mais antigo."""

    async def execute(
        self, filters: UserFilter | Mapping[str, Any] | None, principal: Principal
    ) -> Page[User]:
        """Devolve a pagina no formato normativo `items/total/limit/offset`."""
        authorize(principal, IDENTITY_PERMISSION, "listar usuarios")
        criteria = (
            UserFilter() if filters is None else _coerce(filters, UserFilter, what="o filtro")
        )
        async with self._container.uow_factory() as uow:
            items = await uow.users.list(limit=criteria.limit, offset=criteria.offset)
            total = await uow.users.count()
        return Page(items=list(items), total=total, limit=criteria.limit, offset=criteria.offset)


class UpdateUser(_IdentityUseCase):
    """Atualiza os dados de um usuario (nunca a senha)."""

    async def execute(
        self,
        user_id: str,
        data: UserUpdateInput | Mapping[str, Any],
        principal: Principal,
    ) -> User:
        """Aplica somente os campos informados e grava a alteracao."""
        authorize(principal, IDENTITY_PERMISSION, "alterar usuarios")
        payload = _coerce(data, UserUpdateInput, what="a atualizacao de usuario")
        async with self._container.uow_factory() as uow:
            user = await _require_user(uow, user_id)
            changes = await self._changes(uow, user, payload, principal)
            if not changes:
                return user
            changes["updated_at"] = utcnow()
            updated = await uow.users.update(user.model_copy(update=changes))
            await uow.commit()
        _logger.info("user_updated", user_id=updated.id, fields=sorted(changes))
        return updated

    async def _changes(
        self,
        uow: UnitOfWork,
        user: User,
        payload: UserUpdateInput,
        principal: Principal,
    ) -> Json:
        """Valida e reune apenas os campos efetivamente informados."""
        changes: Json = {}
        if is_set(payload.email):
            email = _normalize_email(payload.email)
            if email != user.email:
                existing = await uow.users.get_by_email(email)
                if existing is not None and existing.id != user.id:
                    raise ConflictError(
                        f"Ja existe um usuario com o e-mail '{email}'.",
                        details={"email": email},
                    )
                changes["email"] = email
        if is_set(payload.name) and payload.name.strip():
            changes["name"] = payload.name.strip()
        if is_set(payload.role):
            role = _as_role(payload.role)
            if role is not user.role:
                await _guard_last_root(uow, user, action="rebaixar")
                changes["role"] = role
        if is_set(payload.tenant_id) and payload.tenant_id.strip():
            changes["tenant_id"] = payload.tenant_id.strip()
        if is_set(payload.is_active) and bool(payload.is_active) is not user.is_active:
            if not payload.is_active:
                _guard_not_self(principal, user, action="desativar")
                await _guard_last_root(uow, user, action="desativar")
            changes["is_active"] = bool(payload.is_active)
        return changes


class DeleteUser(_IdentityUseCase):
    """Remove um usuario da instalacao."""

    async def execute(self, user_id: str, principal: Principal) -> None:
        """Apaga o usuario; apagar a si mesmo ou o ultimo root e recusado."""
        authorize(principal, IDENTITY_PERMISSION, "remover usuarios")
        async with self._container.uow_factory() as uow:
            user = await _require_user(uow, user_id)
            _guard_not_self(principal, user, action="remover")
            await _guard_last_root(uow, user, action="remover")
            await uow.users.delete(user.id)
            await uow.commit()
        _logger.info("user_deleted", user_id=user.id)


class ChangePassword(_IdentityUseCase):
    """Troca a senha de um usuario (auto-atendimento ou reset administrativo)."""

    async def execute(
        self, data: ChangePasswordInput | Mapping[str, Any], principal: Principal
    ) -> User:
        """Grava a nova senha depois de checar quem pode troca-la.

        Trocar a **propria** senha exige apresentar a senha atual — sem isso, um
        token roubado viraria posse permanente da conta. Um administrador pode
        redefinir a senha de outro sem conhecer a anterior, que e exatamente o
        caso de recuperacao de acesso.
        """
        payload = _coerce(data, ChangePasswordInput, what="a troca de senha")
        reference = (payload.user_id or "").strip()
        if reference.lower() in {"", SELF_REFERENCE}:
            reference = principal.subject
        new_password = _validate_password(payload.new_password)

        async with self._container.uow_factory() as uow:
            found = await _find_user(uow, reference)
        if found is None or found.id != principal.subject:
            authorize(principal, IDENTITY_PERMISSION, "redefinir a senha de outros usuarios")
        if found is None:
            raise NotFoundError(
                f"Usuario '{reference}' nao encontrado.",
                details={"reference": reference},
            )

        user = found
        is_self = user.id == principal.subject
        if is_self and not _verify_secret(
            self._hasher, payload.current_password or "", user.password_hash
        ):
            raise _unauthorized("wrong_current_password", user_id=user.id)

        hashed = self._hasher.hash(new_password)
        async with self._container.uow_factory() as uow:
            fresh = await _require_user(uow, user.id)
            updated = await uow.users.update(
                fresh.model_copy(update={"password_hash": hashed, "updated_at": utcnow()})
            )
            await uow.commit()
        _logger.info("password_changed", user_id=updated.id, self_service=is_self)
        return updated


# ---------------------------------------------------------------------------
# Chaves de API
# ---------------------------------------------------------------------------
class CreateApiKey(_IdentityUseCase):
    """Cria uma chave de API e devolve o segredo em texto uma unica vez."""

    async def execute(
        self, data: ApiKeyCreateInput | Mapping[str, Any], principal: Principal
    ) -> ApiKeyCreated:
        """Gera `lk_<prefix>_<secret>`, persiste apenas prefixo e hash, devolve o texto."""
        authorize(principal, IDENTITY_PERMISSION, "criar chaves de API")
        payload = _coerce(data, ApiKeyCreateInput, what="a criacao de chave de API")
        name = (payload.name or "").strip()
        if not name:
            raise ValidationError(
                "A chave de API precisa de um nome que identifique o seu uso.",
                details={"field": "name"},
            )
        role = _as_role(payload.role)
        expires_at = self._validated_expiry(payload.expires_at)

        async with self._container.uow_factory() as uow:
            raw, prefix, secret = await _unique_key(uow)
            api_key = ApiKey(
                name=name,
                prefix=prefix,
                hashed_secret=self._hasher.hash(secret),
                role=role,
                tenant_id=(payload.tenant_id or DEFAULT_TENANT).strip() or DEFAULT_TENANT,
                is_active=True,
                expires_at=expires_at,
            )
            created = await uow.api_keys.add(api_key)
            await uow.commit()
        _logger.info("api_key_created", api_key_id=created.id, prefix=created.prefix)
        return ApiKeyCreated(api_key=_public(created), secret=raw)

    @staticmethod
    def _validated_expiry(expires_at: datetime | None) -> datetime | None:
        """Recusa uma validade ja vencida: a chave nasceria inutil."""
        moment = _as_aware(expires_at)
        if moment is not None and moment <= utcnow():
            raise ValidationError(
                "A data de expiracao da chave precisa estar no futuro.",
                details={"field": "expires_at", "expires_at": moment.isoformat()},
            )
        return moment


class ListApiKeys(_IdentityUseCase):
    """Lista chaves de API — sempre sem segredo e sem hash."""

    async def execute(
        self, filters: ApiKeyFilter | Mapping[str, Any] | None, principal: Principal
    ) -> Page[ApiKey]:
        """Devolve a pagina de chaves; `total` e um limite inferior conhecido.

        A porta `ApiKeyRepository` nao expoe `count`, entao a pagina pede um item
        alem do limite apenas para saber se ha continuacao: `total` reflete o que
        se pode afirmar sem uma contagem de tabela inteira.
        """
        authorize(principal, IDENTITY_PERMISSION, "listar chaves de API")
        criteria = (
            ApiKeyFilter()
            if filters is None
            else _coerce(filters, ApiKeyFilter, what="o filtro de chaves de API")
        )
        async with self._container.uow_factory() as uow:
            found = await uow.api_keys.list(
                is_active=criteria.is_active,
                limit=criteria.limit + 1,
                offset=criteria.offset,
            )
        has_more = len(found) > criteria.limit
        items = [_public(api_key) for api_key in found[: criteria.limit]]
        total = criteria.offset + len(items) + (1 if has_more else 0)
        return Page(items=items, total=total, limit=criteria.limit, offset=criteria.offset)


class RevokeApiKey(_IdentityUseCase):
    """Revoga uma chave de API desativando-a (a linha permanece para auditoria)."""

    async def execute(self, api_key_id: Id, principal: Principal) -> ApiKey:
        """Desativa a chave; repetir a operacao e inofensivo (idempotente).

        Apagar a linha apagaria tambem o rastro de `last_used_at`, que e a unica
        evidencia de por onde a chave andou antes de ser revogada.
        """
        authorize(principal, IDENTITY_PERMISSION, "revogar chaves de API")
        async with self._container.uow_factory() as uow:
            api_key = await _require_api_key(uow, api_key_id)
            if not api_key.is_active:
                return _public(api_key)
            revoked = await uow.api_keys.update(
                api_key.model_copy(update={"is_active": False, "updated_at": utcnow()})
            )
            await uow.commit()
        _logger.info("api_key_revoked", api_key_id=revoked.id, prefix=revoked.prefix)
        return _public(revoked)


class RotateApiKey(_IdentityUseCase):
    """Sorteia prefixo e segredo novos para uma chave existente."""

    async def execute(self, api_key_id: Id, principal: Principal) -> ApiKeyCreated:
        """Invalida o par anterior e devolve o novo segredo uma unica vez.

        O prefixo tambem muda: ele e o indice da busca, e mante-lo deixaria a
        credencial antiga apontando para a linha certa enquanto o segredo velho
        ainda circula em algum arquivo de configuracao esquecido.
        """
        authorize(principal, IDENTITY_PERMISSION, "rotacionar chaves de API")
        async with self._container.uow_factory() as uow:
            api_key = await _require_api_key(uow, api_key_id)
            if not api_key.is_active:
                raise ConflictError(
                    "A chave esta revogada; crie uma nova em vez de rotacionar esta.",
                    details={"api_key_id": api_key.id},
                )
            raw, prefix, secret = await _unique_key(uow)
            rotated = await uow.api_keys.update(
                api_key.model_copy(
                    update={
                        "prefix": prefix,
                        "hashed_secret": self._hasher.hash(secret),
                        "last_used_at": None,
                        "updated_at": utcnow(),
                    }
                )
            )
            await uow.commit()
        _logger.info("api_key_rotated", api_key_id=rotated.id, prefix=rotated.prefix)
        return ApiKeyCreated(api_key=_public(rotated), secret=raw)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
class EnsureBootstrapAdmin(_IdentityUseCase):
    """Cria o primeiro usuario `root` quando a instalacao ainda nao tem nenhum."""

    async def execute(
        self, email: str, password: str, *, name: str = "Administrador"
    ) -> User | None:
        """Cria o root inicial; devolve `None` quando ja existe algum usuario.

        Roda no boot, sem principal: nao ha a quem pedir permissao antes de
        existir a primeira conta. A guarda e a propria tabela vazia — com um
        unico usuario cadastrado a operacao vira no-op, o que a torna segura de
        chamar a cada partida de cada replica.
        """
        normalized = _normalize_email(email)
        secret = _validate_password(password)
        async with self._container.uow_factory() as uow:
            if await uow.users.count() > 0:
                _logger.debug("bootstrap_admin_skipped", reason="usuarios ja cadastrados")
                return None
            created = await uow.users.add(
                User(
                    email=normalized,
                    name=(name or "").strip() or "Administrador",
                    role=Role.ROOT,
                    password_hash=self._hasher.hash(secret),
                    is_active=True,
                )
            )
            await uow.commit()
        _logger.warning(
            "bootstrap_admin_created",
            user_id=created.id,
            email=created.email,
            note="troque a senha inicial no primeiro acesso",
        )
        return created
