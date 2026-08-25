"""Building block `auth`: identidade, JWT, chaves de API e RBAC (SPEC-0006).

Este modulo e a face *plugavel* da identidade: ele nao reimplementa nenhuma
regra de autenticacao, apenas despacha a acao pedida em `request.payload` para
os casos de uso de :mod:`lukato.application.use_cases.identity`, que continuam
sendo o unico lugar onde senha, token e chave de API sao tratados.

Como o modulo obtem o `Container`
--------------------------------
`ModuleContext` transporta portas, nao o `Container`. Os casos de uso de
identidade, porem, recebem um `Container` no construtor. A ponte e feita em duas
camadas, nesta ordem:

1. `ctx.services["container"]` — caminho normal. O caso de uso `InvokeModule`
   publica o container montado pelo *composition root* nos servicos auxiliares
   do contexto, e este modulo simplesmente o usa.
2. Montagem a partir do proprio contexto — rede de seguranca para contextos
   montados a mao (testes, ferramentas de linha de comando). As portas vem de
   `ctx` (`uow_factory`, `llm`, `embeddings`, `guardrails`, `tracer`,
   `orchestrators`, `settings`) e o que falta vem de `ctx.services`
   (`hasher`, `tokens`, `vector_store`, `cost_calculator`, `composer`).
   Servico ausente vira :class:`UnsupportedCapability` nomeando exatamente o que
   faltou — degradacao explicita, nunca falha silenciosa (SPEC-0001 secao 2.7).

Onde mora o segredo
-------------------
`ModuleResponse.data` e persistido em `AgentRun.output` pelo `InvokeModule`;
`ModuleResponse.metadata` **nao** e. Por isso todo material sigiloso emitido por
este modulo (JWT recem-assinado, segredo de chave de API) sai em
`metadata["credential"]`, e `data` carrega apenas a visao publica. E o que
mantem a promessa da SPEC-0006 secao 3 — o segredo aparece uma unica vez e nao
fica guardado na trilha de execucao.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, Final

from lukato.application.container import Container
from lukato.application.use_cases.identity import (
    AccessToken,
    ApiKeyCreated,
    ApiKeyCreateInput,
    ApiKeyFilter,
    CreateApiKey,
    EnsureBootstrapAdmin,
    GetMe,
    ListApiKeys,
    Login,
    LoginInput,
    RefreshToken,
    RevokeApiKey,
    RotateApiKey,
)
from lukato.config import get_logger
from lukato.domain.errors import ConfigurationError, UnsupportedCapability, ValidationError
from lukato.domain.models.identity import ApiKey
from lukato.domain.models.module import ModuleKind
from lukato.domain.types import Json
from lukato.modules.base import (
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
)
from lukato.modules.registry import register_module

__all__ = [
    "ACTIONS",
    "CONTAINER_SERVICE",
    "CREDENTIAL_KEY",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "MAX_TOKEN_TTL_SECONDS",
    "MIN_TOKEN_TTL_SECONDS",
    "REQUIRED_SERVICES",
    "AuthModule",
]

_logger = get_logger(__name__)

ACTIONS: Final[tuple[str, ...]] = (
    "login",
    "refresh",
    "me",
    "create_api_key",
    "list_api_keys",
    "revoke_api_key",
    "rotate_api_key",
)
"""Acoes aceitas em `request.payload["action"]`, na ordem em que sao documentadas."""

CONTAINER_SERVICE: Final[str] = "container"
"""Chave de `ctx.services` onde o `InvokeModule` publica o `Container` da aplicacao."""

REQUIRED_SERVICES: Final[tuple[str, ...]] = (
    "hasher",
    "tokens",
    "vector_store",
    "cost_calculator",
    "composer",
)
"""Servicos exigidos para montar um `Container` quando `ctx.services` nao traz um."""

CREDENTIAL_KEY: Final[str] = "credential"
"""Chave de `ModuleResponse.metadata` que carrega o segredo emitido (nunca `data`)."""

DEFAULT_TOKEN_TTL_SECONDS: Final[int] = 3600
"""Validade padrao do JWT emitido por este modulo (SPEC-0000 secao 13)."""

MIN_TOKEN_TTL_SECONDS: Final[int] = 60
"""Piso da validade: abaixo de um minuto o token e inutil para qualquer cliente."""

MAX_TOKEN_TTL_SECONDS: Final[int] = 86_400
"""Teto da validade: um dia, para que revogacao por rebaixamento tenha efeito util."""

_ID_KEYS: Final[tuple[str, ...]] = ("api_key_id", "id")
"""Nomes aceitos para o identificador da chave nas acoes de revogacao e rotacao."""


# ---------------------------------------------------------------------------
# Leitura do payload
# ---------------------------------------------------------------------------
def _payload_of(request: ModuleRequest) -> Json:
    """Devolve o payload da requisicao ja garantido como objeto."""
    payload = request.payload
    if not isinstance(payload, dict):
        raise ValidationError(
            "O payload do modulo 'auth' deve ser um objeto JSON.",
            details={"received": type(payload).__name__},
        )
    return payload


def _required_text(payload: Json, key: str, *, what: str) -> str:
    """Le um campo de texto obrigatorio, recusando ausencia e vazio."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"O campo '{key}' e obrigatorio para {what}.",
            details={"field": key, "action": payload.get("action")},
        )
    return value.strip()


def _first_text(payload: Json, keys: tuple[str, ...], *, what: str) -> str:
    """Le o primeiro dos nomes aceitos para um mesmo campo obrigatorio."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValidationError(
        f"Informe '{keys[0]}' para {what}.",
        details={"accepted_fields": list(keys), "action": payload.get("action")},
    )


def _optional_text(payload: Json, key: str) -> str | None:
    """Le um campo de texto opcional; vazio equivale a ausente."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"O campo '{key}' deve ser um texto.",
            details={"field": key, "received": type(value).__name__},
        )
    stripped = value.strip()
    return stripped or None


def _optional_bool(payload: Json, key: str) -> bool | None:
    """Le um campo booleano opcional sem aceitar coercao silenciosa."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValidationError(
            f"O campo '{key}' deve ser booleano.",
            details={"field": key, "received": type(value).__name__},
        )
    return value


def _optional_int(payload: Json, key: str) -> int | None:
    """Le um inteiro opcional; `bool` nao passa por inteiro aqui."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"O campo '{key}' deve ser um numero inteiro.",
            details={"field": key, "received": type(value).__name__},
        )
    return value


def _present(**values: Any) -> dict[str, Any]:
    """Mantem apenas os argumentos informados, para que o DTO aplique seus padroes."""
    return {name: value for name, value in values.items() if value is not None}


def _optional_moment(payload: Json, key: str) -> datetime | None:
    """Le um instante ISO-8601 opcional (`2026-12-31T23:59:59+00:00`)."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"O campo '{key}' deve ser uma data ISO-8601.",
            details={"field": key, "received": type(value).__name__},
        )
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(
            f"O campo '{key}' nao e uma data ISO-8601 valida.",
            details={"field": key, "value": value},
        ) from exc


# ---------------------------------------------------------------------------
# Serializacao publica
# ---------------------------------------------------------------------------
def _moment(value: datetime | None) -> str | None:
    """Serializa um instante opcional em ISO-8601."""
    return value.isoformat() if value is not None else None


def _api_key_view(api_key: ApiKey) -> Json:
    """Visao publica de uma chave de API: nunca o segredo, nunca o hash.

    Os casos de uso ja apagam `hashed_secret` antes de devolver; esta funcao
    repete a garantia por construcao, listando apenas campos seguros.
    """
    return {
        "id": api_key.id,
        "name": api_key.name,
        "prefix": api_key.prefix,
        "role": api_key.role.value,
        "tenant_id": api_key.tenant_id,
        "is_active": api_key.is_active,
        "expires_at": _moment(api_key.expires_at),
        "last_used_at": _moment(api_key.last_used_at),
        "created_at": _moment(api_key.created_at),
        "updated_at": _moment(api_key.updated_at),
    }


def _token_response(token: AccessToken, *, what: str) -> ModuleResponse:
    """Monta a resposta de login/refresh com o JWT fora de `data`."""
    payload = token.to_dict()
    secret = payload.pop("access_token")
    return ModuleResponse(
        output=(
            f"{what} para '{token.principal.subject}' "
            f"(papel {token.principal.role.value}), valido por {token.expires_in}s."
        ),
        data=payload,
        metadata={
            CREDENTIAL_KEY: {
                "access_token": secret,
                "token_type": token.token_type,
                "expires_in": token.expires_in,
            }
        },
    )


def _api_key_response(created: ApiKeyCreated, *, what: str) -> ModuleResponse:
    """Monta a resposta de criacao/rotacao com o segredo fora de `data`."""
    payload = created.to_dict()
    secret = payload.pop("secret")
    warning = payload.pop("warning", "")
    return ModuleResponse(
        output=f"{what} a chave de API '{created.api_key.name}' (prefixo {created.api_key.prefix}).",
        data=_api_key_view(created.api_key),
        metadata={CREDENTIAL_KEY: {"api_key": secret, "warning": warning}},
    )


# ---------------------------------------------------------------------------
# Resolucao do Container
# ---------------------------------------------------------------------------
def _gather_services(ctx: ModuleContext, names: tuple[str, ...]) -> dict[str, Any]:
    """Colhe servicos obrigatorios do contexto, reclamando de todos os que faltam.

    Uma unica excecao lista os ausentes: descobrir a lacuna inteira de uma vez
    e mais util para quem monta a instalacao do que descobri-la um item por vez.
    """
    found: dict[str, Any] = {}
    missing: list[str] = []
    for name in names:
        value = ctx.services.get(name)
        if value is None:
            missing.append(name)
        else:
            found[name] = value
    if missing:
        available = sorted(ctx.services)
        raise UnsupportedCapability(
            "O modulo 'auth' precisa do container da aplicacao. Sem "
            f"ctx.services['{CONTAINER_SERVICE}'], os servicos {missing} tambem "
            "faltam para monta-lo a partir do contexto.",
            details={
                "module_slug": ctx.definition.slug,
                "missing": missing,
                "available": available,
                "expected_service": CONTAINER_SERVICE,
            },
        )
    return found


def _resolve_container(ctx: ModuleContext) -> Container:
    """Devolve o `Container` publicado em `ctx.services` pelo `InvokeModule`.

    Antes havia aqui uma rede de seguranca que remontava o `Container` a partir do
    contexto. Ela foi removida: duplicava a fiacao (um campo novo no `Container`
    precisaria ser lembrado em dois lugares, e o esquecimento seria silencioso) e,
    pior, mascarava exatamente o defeito que existia — `InvokeModule` nao publicava
    a chave `container`, e so este modulo nao quebrava por causa do remendo.
    """
    found = ctx.services.get(CONTAINER_SERVICE)
    if isinstance(found, Container):
        return found
    raise ConfigurationError(
        f"O modulo '{ctx.definition.slug}' precisa de ctx.services['{CONTAINER_SERVICE}'], "
        "publicado pelo InvokeModule. Contexto montado a mao em teste deve inclui-lo.",
        details={
            "module_slug": ctx.definition.slug,
            "expected_service": CONTAINER_SERVICE,
            "available": sorted(ctx.services),
        },
    )


def _with_token_ttl(container: Container, ttl_seconds: int) -> Container:
    """Aplica `config["token_ttl_seconds"]` sem tocar na configuracao global.

    Os casos de uso leem a validade de `settings.security.jwt_expires_seconds`.
    Para que duas definicoes sobre a mesma classe possam emitir tokens com
    validades diferentes (SPEC-0001 secao 7, criterio 2), o modulo entrega a
    elas uma copia do container com um `Settings` ajustado — a instancia
    compartilhada permanece intacta.
    """
    settings = container.settings
    if int(settings.security.jwt_expires_seconds) == ttl_seconds:
        return container
    security = settings.security.model_copy(update={"jwt_expires_seconds": ttl_seconds})
    return dataclasses.replace(
        container, settings=settings.model_copy(update={"security": security})
    )


# ---------------------------------------------------------------------------
# Building block
# ---------------------------------------------------------------------------
@register_module
class AuthModule(BaseModule):
    """Identidade da plataforma exposta como building block.

    Despacha `payload["action"]` para os casos de uso de identidade: `login`,
    `refresh`, `me`, `create_api_key`, `list_api_keys`, `revoke_api_key` e
    `rotate_api_key`. Acao desconhecida gera :class:`ValidationError` listando
    as validas.

    Nenhuma decisao de autorizacao vive aqui: quem autoriza e `Principal.can`,
    dentro dos casos de uso (SPEC-0006 secao 1). E nenhum segredo entra em
    `data` — JWT e segredo de chave saem em `metadata["credential"]`, que o
    `InvokeModule` nao persiste no `AgentRun`.
    """

    kind: ClassVar[ModuleKind] = ModuleKind.AUTH
    slug: ClassVar[str] = "auth"
    name: ClassVar[str] = "Identidade"
    description: ClassVar[str] = (
        "Login, emissao e renovacao de JWT, ciclo de vida de chaves de API e RBAC."
    )
    capabilities: ClassVar[tuple[str, ...]] = ("login", "issue_token", "api_keys", "rbac")
    config_schema: ClassVar[Json] = {
        "type": "object",
        "properties": {
            "bootstrap_admin_email": {
                "type": "string",
                "default": "",
                "maxLength": 320,
                "description": (
                    "E-mail autorizado a reivindicar o primeiro usuario root de uma "
                    "instalacao ainda sem nenhum usuario. A senha e a apresentada no "
                    "primeiro login: nenhuma credencial nasce no codigo."
                ),
            },
            "token_ttl_seconds": {
                "type": "integer",
                "default": DEFAULT_TOKEN_TTL_SECONDS,
                "minimum": MIN_TOKEN_TTL_SECONDS,
                "maximum": MAX_TOKEN_TTL_SECONDS,
                "description": "Validade, em segundos, dos tokens emitidos por esta definicao.",
            },
        },
    }

    async def setup(self, ctx: ModuleContext) -> None:
        """Valida a configuracao da definicao antes do primeiro `handle`."""
        config = self._config(ctx)
        _logger.info(
            "auth_module_ready",
            module=ctx.definition.slug,
            token_ttl_seconds=config["token_ttl_seconds"],
            bootstrap_admin=bool(config["bootstrap_admin_email"]),
        )

    async def handle(self, request: ModuleRequest, ctx: ModuleContext) -> ModuleResponse:
        """Despacha a acao pedida no payload para o caso de uso correspondente."""
        payload = _payload_of(request)
        action = self._action(payload)
        config = self._config(ctx)
        container = _with_token_ttl(_resolve_container(ctx), int(config["token_ttl_seconds"]))

        if action == "login":
            return await self._login(payload, ctx, container, config)
        if action == "refresh":
            return await self._refresh(payload, container)
        if action == "me":
            return await self._me(ctx, container)
        if action == "create_api_key":
            return await self._create_api_key(payload, ctx, container)
        if action == "list_api_keys":
            return await self._list_api_keys(payload, ctx, container)
        if action == "revoke_api_key":
            return await self._revoke_api_key(payload, ctx, container)
        return await self._rotate_api_key(payload, ctx, container)

    def ui(self) -> UIDescriptor:
        """Publica os itens de identidade na secao ADMINISTRATIVO do console."""
        return UIDescriptor(
            nav=[
                UINavItem(
                    label="Identidade",
                    icon="users",
                    endpoint="/identity",
                    section="ADMINISTRATIVO",
                    order=10,
                ),
                UINavItem(
                    label="Chaves de API",
                    icon="key",
                    endpoint="/identity?tab=api-keys",
                    section="ADMINISTRATIVO",
                    order=20,
                ),
            ],
            center_template="pages/identity.html",
            context_template="context/default.html",
        )

    def health(self) -> Json:
        """Resumo de saude do modulo, acrescido das acoes que ele aceita."""
        report = super().health()
        report["actions"] = list(ACTIONS)
        return report

    # -- acoes -------------------------------------------------------------
    async def _login(
        self, payload: Json, ctx: ModuleContext, container: Container, config: Json
    ) -> ModuleResponse:
        """Autentica por e-mail e senha, emitindo o JWT do usuario."""
        email = _required_text(payload, "email", what="o login")
        password = _required_text(payload, "password", what="o login")
        await self._claim_bootstrap_admin(ctx, container, config, email=email, password=password)
        token = await Login(container).execute(LoginInput(email=email, password=password))
        return _token_response(token, what="Token emitido")

    async def _refresh(self, payload: Json, container: Container) -> ModuleResponse:
        """Troca um JWT ainda valido por outro, reconferindo o usuario no banco."""
        token = _required_text(payload, "token", what="a renovacao de token")
        renewed = await RefreshToken(container).execute(token)
        return _token_response(renewed, what="Token renovado")

    async def _me(self, ctx: ModuleContext, container: Container) -> ModuleResponse:
        """Descreve a identidade corrente, com papel e permissoes efetivas."""
        view = await GetMe(container).execute(ctx.principal)
        return ModuleResponse(
            output=(
                f"Identidade '{view.principal.subject}' ({view.principal.role.value}) "
                f"com {len(view.permissions)} permissao(oes)."
            ),
            data=view.to_dict(),
        )

    async def _create_api_key(
        self, payload: Json, ctx: ModuleContext, container: Container
    ) -> ModuleResponse:
        """Cria uma chave de API e devolve o segredo uma unica vez.

        Campos ausentes sao omitidos do DTO em vez de preenchidos aqui: o padrao
        de papel e de tenant e do caso de uso, e duplica-lo criaria duas fontes
        de verdade para a mesma decisao.
        """
        data = ApiKeyCreateInput(
            name=_required_text(payload, "name", what="a criacao de chave de API"),
            **_present(
                role=_optional_text(payload, "role"),
                tenant_id=_optional_text(payload, "tenant_id"),
                expires_at=_optional_moment(payload, "expires_at"),
            ),
        )
        created = await CreateApiKey(container).execute(data, ctx.principal)
        return _api_key_response(created, what="Criada")

    async def _list_api_keys(
        self, payload: Json, ctx: ModuleContext, container: Container
    ) -> ModuleResponse:
        """Lista as chaves de API na janela pedida, sempre sem segredo."""
        criteria = ApiKeyFilter(
            is_active=_optional_bool(payload, "is_active"),
            **_present(
                limit=_optional_int(payload, "limit"),
                offset=_optional_int(payload, "offset"),
            ),
        )
        page = await ListApiKeys(container).execute(criteria, ctx.principal)
        return ModuleResponse(
            output=f"{len(page.items)} chave(s) de API listada(s).",
            data={
                "items": [_api_key_view(api_key) for api_key in page.items],
                "total": page.total,
                "limit": page.limit,
                "offset": page.offset,
            },
        )

    async def _revoke_api_key(
        self, payload: Json, ctx: ModuleContext, container: Container
    ) -> ModuleResponse:
        """Revoga uma chave de API; repetir a operacao e inofensivo."""
        api_key_id = _first_text(payload, _ID_KEYS, what="revogar uma chave de API")
        revoked = await RevokeApiKey(container).execute(api_key_id, ctx.principal)
        return ModuleResponse(
            output=f"Chave de API '{revoked.name}' (prefixo {revoked.prefix}) revogada.",
            data=_api_key_view(revoked),
        )

    async def _rotate_api_key(
        self, payload: Json, ctx: ModuleContext, container: Container
    ) -> ModuleResponse:
        """Sorteia prefixo e segredo novos para uma chave existente."""
        api_key_id = _first_text(payload, _ID_KEYS, what="rotacionar uma chave de API")
        rotated = await RotateApiKey(container).execute(api_key_id, ctx.principal)
        return _api_key_response(rotated, what="Rotacionada")

    # -- apoio -------------------------------------------------------------
    def _action(self, payload: Json) -> str:
        """Extrai e valida `payload['action']` contra o catalogo de acoes."""
        raw = payload.get("action")
        action = raw.strip().lower() if isinstance(raw, str) else ""
        if action in ACTIONS:
            return action
        raise ValidationError(
            f"Acao '{raw}' desconhecida no modulo '{self.slug}'. "
            f"Acoes validas: {', '.join(ACTIONS)}.",
            details={"action": raw, "valid_actions": list(ACTIONS)},
        )

    def _config(self, ctx: ModuleContext) -> Json:
        """Configuracao da definicao normalizada pelo `config_schema` da classe."""
        config = ctx.definition.config
        return self.validate_config(dict(config) if isinstance(config, Mapping) else {})

    async def _claim_bootstrap_admin(
        self,
        ctx: ModuleContext,
        container: Container,
        config: Json,
        *,
        email: str,
        password: str,
    ) -> None:
        """Deixa o e-mail configurado reivindicar o root inicial da instalacao.

        So acontece quando a instalacao ainda nao tem **nenhum** usuario — a
        propria guarda de :class:`EnsureBootstrapAdmin` — e quando o e-mail
        apresentado e exatamente o configurado. A senha e a que o operador
        digitou: nenhuma credencial e inventada pelo codigo nem escrita em log.
        """
        expected = str(config["bootstrap_admin_email"]).strip().lower()
        if not expected or expected != email.strip().lower():
            return
        created = await EnsureBootstrapAdmin(container).execute(email, password)
        if created is not None:
            _logger.warning(
                "auth_bootstrap_admin_claimed",
                module=ctx.definition.slug,
                user_id=created.id,
                email=created.email,
            )
