"""Rotas de `/api/v1/identity` — autenticacao, usuarios e chaves de API (SPEC-0006).

Este e o unico recurso da API que precisa responder **antes** de haver
identidade: `POST /login` e `POST /token/refresh` sao os dois pontos de entrada
que nao exigem credencial, porque sao eles que a emitem. As demais rotas seguem a
regra geral da plataforma — a autorizacao passa por `Principal.can(...)`, nunca
por comparacao de papel (SPEC-0006 secao 1).

Tres invariantes governam o arquivo inteiro e explicam cada escolha abaixo:

* **A 401 nao conta nada.** E-mail inexistente, senha errada e conta desativada
  saem com o mesmo status e a mesma mensagem, montada uma unica vez dentro de
  :class:`~lukato.application.use_cases.identity.Login`. A borda nao acrescenta
  nenhum detalhe distintivo — nem um `details` mais rico, nem um `WWW-Authenticate`
  diferente — porque qualquer variacao observavel devolve ao atacante o oraculo de
  enumeracao que a mensagem unica se recusa a dar.
* **O segredo aparece uma unica vez.** So `POST /api-keys` e
  `POST /api-keys/{id}/rotate` respondem :class:`ApiKeyCreatedOut`; toda leitura
  usa :class:`ApiKeyOut`, que nao tem campo para o segredo nem para o hash. A
  garantia e do **schema**, nao da disciplina de quem escreve a rota: `response_model`
  descarta qualquer campo que o modelo de saida nao declare, entao mesmo um caso
  de uso que devolvesse a entidade crua nao vazaria `hashed_secret`.
* **Escrita de usuario e administracao.** `GET|POST /users`, `PUT` e `DELETE`
  exigem `admin:*` ja na borda, antes de abrir transacao. As duas excecoes de
  auto-atendimento — ler-se e trocar a propria senha — recebem apenas o
  `Principal` autenticado e delegam a decisao ao caso de uso, que sabe distinguir
  "sou eu" de "sou outro" e cobra a senha atual no primeiro caso.

Nenhuma rota toca repositorio: toda operacao passa por um caso de uso de
:mod:`lukato.application.use_cases.identity`, construido com o `Container`
injetado por :func:`lukato.interfaces.http.deps.get_container`.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Response, status

from lukato.application.use_cases.identity import (
    ApiKeyFilter,
    ChangePassword,
    ChangePasswordInput,
    CreateApiKey,
    CreateUser,
    DeleteUser,
    GetMe,
    GetUser,
    ListApiKeys,
    ListUsers,
    Login,
    RefreshToken,
    RevokeApiKey,
    RotateApiKey,
    UpdateUser,
    UserFilter,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, PrincipalDep, require
from lukato.interfaces.http.schemas.common import Page, error_responses
from lukato.interfaces.http.schemas.identity import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyOut,
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserOut,
    UserUpdate,
)

__all__ = ["PUBLIC_ENDPOINT", "router"]

router = APIRouter(prefix="/identity", tags=["identidade"])
"""Roteador do recurso de identidade (SPEC-0000 secao 11)."""

PUBLIC_ENDPOINT: Final[dict[str, Any]] = {"security": []}
"""Anula o requisito global de seguranca do OpenAPI para as rotas que emitem token.

O documento declara `bearerAuth`/`apiKeyAuth` na raiz (SPEC-0006 secao 2), o que
faria o Swagger exigir credencial justamente em `/login` — a rota cuja funcao e
produzi-la. Sobrescrever com uma lista vazia e a forma que o OpenAPI 3.1 tem de
dizer "esta operacao dispensa autenticacao".
"""

_Admin = Annotated[Principal, Depends(require(Permission.ADMIN_ALL))]
"""Principal que ja provou ter `admin:*` — exigido por toda administracao de identidade."""

_UserRef = Annotated[
    str,
    Path(
        min_length=1,
        description=(
            "Identificador do usuario; o e-mail e o apelido `me` (o proprio "
            "principal) tambem sao aceitos."
        ),
    ),
]
"""Referencia de usuario recebida no caminho da rota."""

_ApiKeyId = Annotated[str, Path(min_length=1, description="Identificador da chave de API.")]
"""Identificador de chave de API recebido no caminho da rota."""


# ---------------------------------------------------------------------------
# Autenticacao
# ---------------------------------------------------------------------------
@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Autenticar por e-mail e senha",
    description=(
        "Troca e-mail e senha por um JWT HS256 valido por `expires_in` segundos.\n\n"
        "Qualquer falha — e-mail inexistente, senha errada ou conta desativada — "
        "devolve **401 com a mesma mensagem**. A indistincao e deliberada: uma "
        "resposta que diferenciasse os casos transformaria o formulario de login em "
        "um verificador de e-mails cadastrados. O tempo de resposta tambem e "
        "igualado, conferindo a senha contra um hash chamariz quando a conta nao "
        "existe (SPEC-0006 secao 2)."
    ),
    responses=error_responses(401, 422, 429),
    openapi_extra=PUBLIC_ENDPOINT,
)
async def login(payload: LoginRequest, container: ContainerDep) -> TokenResponse:
    """Emite o token do usuario autenticado."""
    token = await Login(container).execute(payload.to_input())
    return TokenResponse.from_result(token)


@router.post(
    "/token/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Renovar o token de acesso",
    description=(
        "Troca um JWT ainda valido por outro, com a validade reiniciada.\n\n"
        "O usuario e **relido do banco** a cada renovacao: e isso que faz a "
        "revogacao valer. Uma conta desativada ou um papel rebaixado param de "
        "render tokens novos no primeiro refresh, sem esperar a expiracao do token "
        "que ja esta na mao do cliente. Token de chave de API nao renova — a chave "
        "e a propria credencial de longa duracao."
    ),
    responses=error_responses(401, 422, 429),
    openapi_extra=PUBLIC_ENDPOINT,
)
async def refresh_token(payload: RefreshRequest, container: ContainerDep) -> TokenResponse:
    """Devolve um token novo para o mesmo usuario."""
    token = await RefreshToken(container).execute(payload.token)
    return TokenResponse.from_result(token)


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Descrever a identidade corrente",
    description=(
        "Devolve o principal resolvido pela credencial da requisicao, com as "
        "permissoes efetivas do papel e — quando a origem for um usuario — o "
        "cadastro correspondente, **sem** o hash de senha.\n\n"
        "Exige apenas estar autenticado: e a rota que o console chama logo apos o "
        "login para montar o menu conforme o que a identidade pode fazer. Com "
        "`security.auth_enabled=false` responde o root anonimo (SPEC-0006 criterio 4)."
    ),
    responses=error_responses(401),
)
async def get_me(container: ContainerDep, principal: PrincipalDep) -> MeResponse:
    """Descreve principal, permissoes e usuario da requisicao corrente."""
    view = await GetMe(container).execute(principal)
    return MeResponse.from_result(view)


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------
@router.get(
    "/users",
    response_model=Page[UserOut],
    summary="Listar usuarios",
    description=(
        "Pagina os usuarios da instalacao, do mais recente para o mais antigo. "
        "Exige `admin:*`. Nenhum item traz `password_hash`: o schema de saida nao "
        "possui esse campo."
    ),
    responses=error_responses(401, 403, 422),
)
async def list_users(
    container: ContainerDep,
    principal: _Admin,
    pagination: PaginationDep,
) -> Page[UserOut]:
    """Devolve a pagina de usuarios no envelope normativo `items/total/limit/offset`."""
    result = await ListUsers(container).execute(
        UserFilter(limit=pagination.limit, offset=pagination.offset),
        principal,
    )
    return Page[UserOut].from_result(result, UserOut.from_domain)


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Criar usuario",
    description=(
        "Cria um usuario autenticavel. A senha chega em texto claro sobre TLS e e "
        "gravada apenas como hash bcrypt (custo 12, com pre-hash SHA-256 para "
        "respeitar o limite de 72 bytes do algoritmo). Exige `admin:*`; e-mail ja "
        "cadastrado devolve 409."
    ),
    responses=error_responses(401, 403, 409, 422),
)
async def create_user(
    payload: UserCreate,
    container: ContainerDep,
    principal: _Admin,
) -> UserOut:
    """Grava o usuario e devolve o cadastro criado."""
    user = await CreateUser(container).execute(payload.to_input(), principal)
    return UserOut.from_domain(user)


@router.get(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Obter usuario",
    description=(
        "Busca o usuario por identificador, por e-mail ou pelo apelido `me`.\n\n"
        "Ler **a si mesmo** dispensa permissao; ler outro exige `admin:*`. A "
        "autorizacao e cobrada antes do 404 justamente para que a diferenca entre "
        "'nao encontrado' e 'sem permissao' nao vire um verificador de e-mails "
        "cadastrados aberto a qualquer `viewer`."
    ),
    responses=error_responses(401, 403, 404),
)
async def get_user(
    user_id: _UserRef,
    container: ContainerDep,
    principal: PrincipalDep,
) -> UserOut:
    """Devolve o usuario resolvido pela referencia informada."""
    user = await GetUser(container).execute(user_id, principal)
    return UserOut.from_domain(user)


@router.put(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Atualizar usuario",
    description=(
        "Aplica somente os campos enviados; os ausentes ficam como estao. A senha "
        "**nao** entra aqui — troca-la e operacao propria "
        "(`POST /users/{user_id}/password`), com regra de autorizacao diferente.\n\n"
        "Exige `admin:*`. Duas travas de porta valem mesmo para um root: ninguem "
        "desativa a si mesmo e o ultimo root ativo nao pode ser rebaixado nem "
        "desligado — a instalacao ficaria sem administrador capaz de recria-lo."
    ),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def update_user(
    user_id: _UserRef,
    payload: UserUpdate,
    container: ContainerDep,
    principal: _Admin,
) -> UserOut:
    """Atualiza o usuario e devolve o cadastro resultante."""
    user = await UpdateUser(container).execute(user_id, payload.to_input(), principal)
    return UserOut.from_domain(user)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remover usuario",
    description=(
        "Apaga o usuario da instalacao. Exige `admin:*`. Remover a si mesmo ou o "
        "ultimo root ativo e recusado com 409. Operacao sem corpo de resposta."
    ),
    responses=error_responses(401, 403, 404, 409),
)
async def delete_user(
    user_id: _UserRef,
    container: ContainerDep,
    principal: _Admin,
) -> Response:
    """Remove o usuario e responde 204 sem corpo."""
    await DeleteUser(container).execute(user_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{user_id}/password",
    response_model=UserOut,
    summary="Trocar a senha de um usuario",
    description=(
        "Grava uma senha nova. Duas situacoes, com exigencias diferentes:\n\n"
        "* **a propria senha** (`me` ou o proprio identificador): dispensa `admin:*`, "
        "mas exige `current_password`. Sem essa confirmacao, um token roubado viraria "
        "posse permanente da conta.\n"
        "* **a senha de outro**: exige `admin:*` e dispensa a senha anterior — e "
        "exatamente o caso de recuperacao de acesso.\n\n"
        "A resposta traz o cadastro atualizado, jamais o hash."
    ),
    responses=error_responses(401, 403, 404, 422),
)
async def change_password(
    user_id: _UserRef,
    payload: ChangePasswordRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> UserOut:
    """Troca a senha do usuario indicado e devolve o cadastro atualizado."""
    user = await ChangePassword(container).execute(
        ChangePasswordInput(
            user_id=user_id,
            new_password=payload.new_password,
            current_password=payload.current_password,
        ),
        principal,
    )
    return UserOut.from_domain(user)


# ---------------------------------------------------------------------------
# Chaves de API
# ---------------------------------------------------------------------------
@router.get(
    "/api-keys",
    response_model=Page[ApiKeyOut],
    summary="Listar chaves de API",
    description=(
        "Pagina as chaves da instalacao. Cada item traz o **prefixo** publico, que "
        "identifica a chave nos registros de uso, e nunca o segredo nem o hash "
        "bcrypt: um hash vazado e um ataque offline pronto (SPEC-0006 criterio 3). "
        "Exige `admin:*`."
    ),
    responses=error_responses(401, 403, 422),
)
async def list_api_keys(
    container: ContainerDep,
    principal: _Admin,
    pagination: PaginationDep,
    is_active: Annotated[
        bool | None,
        Query(description="`true` lista so as chaves ativas; `false`, so as revogadas."),
    ] = None,
) -> Page[ApiKeyOut]:
    """Devolve a pagina de chaves de API, sempre sem segredo."""
    result = await ListApiKeys(container).execute(
        ApiKeyFilter(
            is_active=is_active,
            limit=pagination.limit,
            offset=pagination.offset,
        ),
        principal,
    )
    return Page[ApiKeyOut].from_result(result, ApiKeyOut.from_domain)


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Criar chave de API",
    description=(
        "Cria uma chave `lk_<prefixo>_<segredo>` e a devolve por inteiro **uma unica "
        "vez**. O banco guarda apenas o prefixo e o hash bcrypt do segredo, entao "
        "nenhuma leitura posterior consegue reexibi-la: quem nao anotar agora precisa "
        "rotacionar a chave. Exige `admin:*`; uma expiracao ja vencida e recusada com "
        "422, porque a chave nasceria inutil."
    ),
    responses=error_responses(401, 403, 409, 422),
)
async def create_api_key(
    payload: ApiKeyCreate,
    container: ContainerDep,
    principal: _Admin,
) -> ApiKeyCreatedOut:
    """Cria a chave e devolve o segredo em texto pela unica vez."""
    created = await CreateApiKey(container).execute(payload.to_input(), principal)
    return ApiKeyCreatedOut.from_result(created)


@router.delete(
    "/api-keys/{api_key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revogar chave de API",
    description=(
        "Desativa a chave imediatamente: a proxima requisicao que a apresentar "
        "recebe 401. A linha **permanece** no banco de proposito — apaga-la levaria "
        "junto `last_used_at`, a unica evidencia de por onde a credencial andou antes "
        "de ser revogada. Repetir a chamada e inofensivo. Exige `admin:*`."
    ),
    responses=error_responses(401, 403, 404),
)
async def revoke_api_key(
    api_key_id: _ApiKeyId,
    container: ContainerDep,
    principal: _Admin,
) -> Response:
    """Revoga a chave e responde 204 sem corpo."""
    await RevokeApiKey(container).execute(api_key_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api-keys/{api_key_id}/rotate",
    response_model=ApiKeyCreatedOut,
    status_code=status.HTTP_200_OK,
    summary="Rotacionar chave de API",
    description=(
        "Sorteia prefixo e segredo novos para uma chave existente, invalidando o par "
        "anterior, e devolve o segredo **uma unica vez** — a segunda resposta que "
        "carrega segredo em toda a API.\n\n"
        "O prefixo muda junto: ele e o indice da busca, e mante-lo deixaria a "
        "credencial antiga apontando para a linha certa enquanto o segredo velho ainda "
        "circula em algum arquivo de configuracao esquecido. Uma chave ja revogada "
        "devolve 409: crie outra em vez de ressuscitar esta. Exige `admin:*`."
    ),
    responses=error_responses(401, 403, 404, 409),
)
async def rotate_api_key(
    api_key_id: _ApiKeyId,
    container: ContainerDep,
    principal: _Admin,
) -> ApiKeyCreatedOut:
    """Rotaciona a chave e devolve o novo segredo pela unica vez."""
    rotated = await RotateApiKey(container).execute(api_key_id, principal)
    return ApiKeyCreatedOut.from_result(rotated)
