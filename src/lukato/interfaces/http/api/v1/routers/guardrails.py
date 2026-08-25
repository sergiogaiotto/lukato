"""Rotas de `/api/v1/guardrails` — politicas parametrizaveis de entrada e saida.

O guardrail e a primeira e a ultima etapa de **todo** modulo (SPEC-0003 secao 1):
`entrada -> system prompt -> saida`. Este recurso e onde a trinca e parametrizada,
e ele carrega tres responsabilidades alem do CRUD:

* `GET /guardrails/rule-kinds` publica o catalogo dos tipos de regra com o
  `config_schema` de cada um, para o console montar o editor de regras
  dinamicamente, sem hard-code de campo;
* `POST /guardrails/test` e o **testador**: aplica uma politica salva (`policy`)
  ou o rascunho aberto no editor (`draft`) sobre um texto e devolve o
  `GuardrailVerdict` inteiro — `allowed`, conteudo ja redigido,
  `original_content`, achados regra a regra e `latency_ms`. Nada e persistido e
  nenhuma execucao e criada;
* trocar as regras de uma politica ja vinculada muda o comportamento dos modulos
  **sem redeploy** (SPEC-0003 criterio 4), e por isso a validacao das regras
  acontece na gravacao, nunca no meio de uma execucao.

Nenhuma rota toca repositorio: toda operacao passa por um caso de uso de
:mod:`lukato.application.use_cases.guardrails`, construido com o `Container`
injetado por :func:`lukato.interfaces.http.deps.get_container`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from lukato.application.use_cases.guardrails import (
    CreatePolicy,
    DeletePolicy,
    GetPolicy,
    GetPolicyBySlug,
    ListPolicies,
    ListRuleKinds,
    PolicyFilter,
    TestPolicy,
    UpdatePolicy,
)
from lukato.domain.models.guardrail import GuardrailStage
from lukato.domain.models.identity import Permission, Principal
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.common import Page, error_responses
from lukato.interfaces.http.schemas.guardrails import (
    PolicyCreate,
    PolicyOut,
    PolicyTestRequest,
    PolicyTestResponse,
    PolicyUpdate,
    RuleKindInfo,
)

__all__ = ["router"]

router = APIRouter(prefix="/guardrails", tags=["guardrails"])
"""Roteador do recurso de guardrails (SPEC-0000 secao 11)."""

_Reader = Annotated[Principal, Depends(require(Permission.GUARDRAIL_READ))]
"""Principal que ja provou ter `guardrail:read`."""

_Writer = Annotated[Principal, Depends(require(Permission.GUARDRAIL_WRITE))]
"""Principal que ja provou ter `guardrail:write`."""

_PolicyRef = Annotated[
    str,
    Path(min_length=1, description="Identificador da politica; o slug tambem e aceito."),
]
"""Referencia de politica recebida no caminho da rota."""

_Slug = Annotated[str, Path(min_length=1, description="Slug estavel da politica.")]
"""Slug recebido no caminho da rota."""


# ---------------------------------------------------------------------------
# Catalogo e testador (declarados antes das rotas por identificador)
# ---------------------------------------------------------------------------
@router.get(
    "/rule-kinds",
    response_model=Page[RuleKindInfo],
    summary="Catalogo de tipos de regra",
    description=(
        "Descreve os tipos de regra suportados por esta instalacao: o que cada um faz, "
        "o JSON Schema aceito em `config` e as acoes que fazem sentido para o tipo. "
        "E a fonte do editor de regras do console — a UI monta os campos a partir daqui "
        "em vez de replicar a SPEC-0003 em JavaScript."
    ),
    responses=error_responses(401, 403),
)
async def list_rule_kinds(
    container: ContainerDep,
    principal: _Reader,
) -> Page[RuleKindInfo]:
    """Devolve o descritor de cada tipo de regra no envelope de lista."""
    catalog = await ListRuleKinds(container).execute(principal)
    return Page[RuleKindInfo].of(
        [RuleKindInfo.from_catalog(entry) for entry in catalog],
        total=len(catalog),
        limit=max(1, len(catalog)),
    )


@router.post(
    "/test",
    response_model=PolicyTestResponse,
    summary="Testar uma politica sobre um texto",
    description=(
        "Aplica uma politica a um conteudo e devolve o veredito completo, sem persistir "
        "nada. Informe **`policy`** (slug ou id de uma politica salva) **ou** **`draft`** "
        "(a politica avulsa aberta no editor); sem nenhum dos dois, o teste exercita o "
        "caminho permissivo do estagio, util para comparar o antes e o depois. "
        "A resposta traz `allowed`, o `content` ja redigido, o `original_content` intacto, "
        "os `findings` na ordem de avaliacao e a `latency_ms`. Testar uma politica salva "
        "exige `guardrail:read`; testar um rascunho e trabalho de autoria e exige "
        "`guardrail:write`."
    ),
    responses=error_responses(401, 403, 404, 422),
)
async def test_policy(
    payload: PolicyTestRequest,
    container: ContainerDep,
    principal: _Reader,
) -> PolicyTestResponse:
    """Roda a politica sobre o conteudo e devolve o `GuardrailVerdict` inteiro."""
    verdict = await TestPolicy(container).execute(
        payload.policy_argument(),
        payload.content,
        principal,
        stage=payload.stage,
        context=dict(payload.context),
    )
    return PolicyTestResponse.from_domain(verdict)


# ---------------------------------------------------------------------------
# Colecao
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=Page[PolicyOut],
    summary="Listar politicas de guardrail",
    description=(
        "Lista as politicas paginadas, filtrando por estagio (`input` ou `output`), "
        "por atividade e por busca textual. O filtro de estagio e o que alimenta os dois "
        "seletores do binding de um modulo: guardrail de entrada e guardrail de saida."
    ),
    responses=error_responses(401, 403, 422),
)
async def list_policies(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    stage: Annotated[
        GuardrailStage | None,
        Query(description="Estagio da politica: `input` (entrada) ou `output` (saida)."),
    ] = None,
    is_active: Annotated[
        bool | None,
        Query(description="`true` mostra so as politicas em vigor; `false`, so as desligadas."),
    ] = None,
    search: Annotated[
        str | None,
        Query(description="Texto procurado no slug, no nome ou na descricao da politica."),
    ] = None,
) -> Page[PolicyOut]:
    """Devolve a pagina de politicas no envelope normativo `items/total/limit/offset`."""
    result = await ListPolicies(container).execute(
        PolicyFilter(
            stage=stage,
            is_active=is_active,
            search=search,
            limit=pagination.limit,
            offset=pagination.offset,
        ),
        principal,
    )
    return Page[PolicyOut].from_result(result, PolicyOut.from_domain)


@router.post(
    "",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Criar politica de guardrail",
    description=(
        "Cria a politica com as regras ja validadas: id de regra unico, tipo conhecido, "
        "acao que o tipo sabe executar e `config` que o avaliador vai aceitar. Regra "
        "incoerente devolve 422 com `rule_id` e `problema` nos detalhes — o erro aparece "
        "no editor, e nao no meio de uma execucao. Slug duplicado devolve 409."
    ),
    responses=error_responses(401, 403, 409, 422),
)
async def create_policy(
    payload: PolicyCreate,
    container: ContainerDep,
    principal: _Writer,
) -> PolicyOut:
    """Grava a politica validada e devolve o registro criado."""
    policy = await CreatePolicy(container).execute(payload.to_input(), principal)
    return PolicyOut.from_domain(policy)


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------
@router.get(
    "/slug/{slug}",
    response_model=PolicyOut,
    summary="Obter politica por slug",
    description=(
        "Resolve a politica pelo slug unico — a forma estavel de referencia-la em seed, "
        "em migracao e no binding de um modulo."
    ),
    responses=error_responses(401, 403, 404),
)
async def get_policy_by_slug(
    slug: _Slug,
    container: ContainerDep,
    principal: _Reader,
) -> PolicyOut:
    """Devolve a politica do slug informado."""
    policy = await GetPolicyBySlug(container).execute(slug, principal)
    return PolicyOut.from_domain(policy)


@router.get(
    "/{policy_id}",
    response_model=PolicyOut,
    summary="Obter politica de guardrail",
    description="Busca a politica por identificador; o slug tambem e aceito.",
    responses=error_responses(401, 403, 404),
)
async def get_policy(
    policy_id: _PolicyRef,
    container: ContainerDep,
    principal: _Reader,
) -> PolicyOut:
    """Devolve a politica resolvida pela referencia informada."""
    policy = await GetPolicy(container).execute(policy_id, principal)
    return PolicyOut.from_domain(policy)


@router.put(
    "/{policy_id}",
    response_model=PolicyOut,
    summary="Atualizar politica de guardrail",
    description=(
        "Aplica somente os campos enviados. Enviar `rules` **substitui** o conjunto "
        "inteiro, ja revalidado; os modulos vinculados passam a usar as novas regras na "
        "proxima execucao, sem redeploy. Desligar a politica (`is_active=false`) faz o "
        "estagio voltar a ser permissivo para quem a referencia."
    ),
    responses=error_responses(401, 403, 404, 422),
)
async def update_policy(
    policy_id: _PolicyRef,
    payload: PolicyUpdate,
    container: ContainerDep,
    principal: _Writer,
) -> PolicyOut:
    """Atualiza a politica e devolve o registro gravado."""
    policy = await UpdatePolicy(container).execute(policy_id, payload.to_input(), principal)
    return PolicyOut.from_domain(policy)


@router.delete(
    "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remover politica de guardrail",
    description=(
        "Apaga a politica do catalogo. Modulos que a referenciavam passam a operar **sem "
        "restricao** naquele estagio, o que e uma escolha explicita e nao um erro "
        "(SPEC-0003 secao 1): confira os bindings antes de remover."
    ),
    responses=error_responses(401, 403, 404),
)
async def delete_policy(
    policy_id: _PolicyRef,
    container: ContainerDep,
    principal: _Writer,
) -> Response:
    """Remove a politica e responde 204 sem corpo."""
    await DeletePolicy(container).execute(policy_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
