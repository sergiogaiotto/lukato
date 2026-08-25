"""Rotas de `/api/v1/prompts` — a biblioteca versionada de system prompts.

Um prompt e identificado pelo par `slug` + `version`. Alterar o `template` por
`PUT` **nunca** sobrescreve o texto que ja esta em producao: `UpdatePrompt` grava
a proxima versao e aposenta a anterior, de modo que um `binding.system_prompt_id`
ja auditado continue apontando para o texto exato que rodou (SPEC-0003 secao 1).
Por isso o recurso expoe duas leituras distintas: `/prompts/slug/{slug}` resolve
a versao **vigente** — a que um modulo executa — e
`/prompts/slug/{slug}/versions` devolve o historico inteiro para auditoria.

Nenhuma rota toca repositorio: toda operacao passa por um caso de uso de
:mod:`lukato.application.use_cases.prompts`, construido com o `Container`
injetado por :func:`lukato.interfaces.http.deps.get_container`. A autorizacao
acontece duas vezes de proposito — na borda, por
:func:`lukato.interfaces.http.deps.require`, antes de abrir transacao, e dentro
do caso de uso, que continua seguro quando chamado pela CLI ou por outro modulo.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from lukato.application.use_cases.prompts import (
    ClonePromptVersion,
    CreatePrompt,
    DeletePrompt,
    GetPrompt,
    GetPromptBySlug,
    ListPrompts,
    ListPromptVersions,
    PreviewPrompt,
    PromptFilter,
    UpdatePrompt,
)
from lukato.domain.models.identity import Permission, Principal
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.common import Page, error_responses
from lukato.interfaces.http.schemas.prompts import (
    PromptCloneRequest,
    PromptCreate,
    PromptOut,
    PromptPreviewRequest,
    PromptPreviewResponse,
    PromptUpdate,
)

__all__ = ["router"]

router = APIRouter(prefix="/prompts", tags=["prompts"])
"""Roteador do recurso de prompts (SPEC-0000 secao 11)."""

_Reader = Annotated[Principal, Depends(require(Permission.PROMPT_READ))]
"""Principal que ja provou ter `prompt:read`."""

_Writer = Annotated[Principal, Depends(require(Permission.PROMPT_WRITE))]
"""Principal que ja provou ter `prompt:write`."""

_PromptRef = Annotated[
    str,
    Path(
        min_length=1,
        description="Identificador do prompt; o slug tambem e aceito e resolve a versao vigente.",
    ),
]
"""Referencia de prompt recebida no caminho da rota."""

_Slug = Annotated[str, Path(min_length=1, description="Slug estavel do prompt.")]
"""Slug recebido no caminho da rota."""


# ---------------------------------------------------------------------------
# Colecao
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=Page[PromptOut],
    summary="Listar prompts",
    description=(
        "Lista a biblioteca paginada, com busca textual e filtro de atividade. "
        "Como o repositorio guarda todas as versoes, `is_active=true` produz a visao "
        "de catalogo do console: uma linha por slug, sempre a versao vigente."
    ),
    responses=error_responses(401, 403, 422),
)
async def list_prompts(
    container: ContainerDep,
    principal: _Reader,
    pagination: PaginationDep,
    search: Annotated[
        str | None,
        Query(description="Texto procurado no slug, no nome ou no corpo do template."),
    ] = None,
    is_active: Annotated[
        bool | None,
        Query(description="`true` mostra so as versoes vigentes; `false`, so as aposentadas."),
    ] = None,
) -> Page[PromptOut]:
    """Devolve a pagina de prompts no envelope normativo `items/total/limit/offset`."""
    result = await ListPrompts(container).execute(
        PromptFilter(
            search=search,
            is_active=is_active,
            limit=pagination.limit,
            offset=pagination.offset,
        ),
        principal,
    )
    return Page[PromptOut].from_result(result, PromptOut.from_domain)


@router.post(
    "",
    response_model=PromptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Criar prompt",
    description=(
        "Cria a **versao 1** de um slug. Omitir `variables` deixa a lista ser extraida "
        "do proprio template — o editor nao precisa manter os placeholders a mao. "
        "Slug ja existente devolve 409: para mudar o texto use `PUT`, que versiona."
    ),
    responses=error_responses(401, 403, 409, 422),
)
async def create_prompt(
    payload: PromptCreate,
    container: ContainerDep,
    principal: _Writer,
) -> PromptOut:
    """Grava a primeira versao do prompt e devolve o registro criado."""
    prompt = await CreatePrompt(container).execute(payload.to_input(), principal)
    return PromptOut.from_domain(prompt)


# ---------------------------------------------------------------------------
# Leitura por slug (declarada antes das rotas por identificador)
# ---------------------------------------------------------------------------
@router.get(
    "/slug/{slug}",
    response_model=PromptOut,
    summary="Obter prompt por slug",
    description=(
        "Resolve a versao **vigente** do slug — exatamente a que um modulo executa "
        "pelo `binding.system_prompt_id`. Informe `version` para ler uma versao "
        "especifica do historico."
    ),
    responses=error_responses(401, 403, 404),
)
async def get_prompt_by_slug(
    slug: _Slug,
    container: ContainerDep,
    principal: _Reader,
    version: Annotated[
        int | None,
        Query(ge=1, description="Versao desejada; ausente devolve a versao ativa."),
    ] = None,
) -> PromptOut:
    """Devolve a versao vigente (ou a versao pedida) do slug."""
    prompt = await GetPromptBySlug(container).execute(slug, principal, version=version)
    return PromptOut.from_domain(prompt)


@router.get(
    "/slug/{slug}/versions",
    response_model=Page[PromptOut],
    summary="Listar versoes de um prompt",
    description=(
        "Historico completo do slug, da versao mais recente para a mais antiga. "
        "E a trilha de auditoria da biblioteca: cada execucao ja registrada aponta "
        "para o texto exato que rodou, e ele continua legivel aqui."
    ),
    responses=error_responses(401, 403, 404),
)
async def list_prompt_versions(
    slug: _Slug,
    container: ContainerDep,
    principal: _Reader,
) -> Page[PromptOut]:
    """Devolve todas as versoes do slug no envelope de lista."""
    versions = await ListPromptVersions(container).execute(slug, principal)
    return Page[PromptOut].of(
        [PromptOut.from_domain(prompt) for prompt in versions],
        total=len(versions),
        limit=max(1, len(versions)),
    )


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------
@router.get(
    "/{prompt_id}",
    response_model=PromptOut,
    summary="Obter prompt",
    description=(
        "Busca o prompt por identificador. O slug tambem e aceito e, nesse caso, "
        "resolve a versao vigente."
    ),
    responses=error_responses(401, 403, 404),
)
async def get_prompt(
    prompt_id: _PromptRef,
    container: ContainerDep,
    principal: _Reader,
) -> PromptOut:
    """Devolve o prompt resolvido pela referencia informada."""
    prompt = await GetPrompt(container).execute(prompt_id, principal)
    return PromptOut.from_domain(prompt)


@router.put(
    "/{prompt_id}",
    response_model=PromptOut,
    summary="Atualizar prompt",
    description=(
        "Aplica somente os campos enviados. Enviar um `template` diferente do vigente "
        "**cria uma nova versao** (`version + 1`, ativa) e aposenta a anterior; os demais "
        "campos sao metadados e mudam na propria versao. A resposta e sempre a versao "
        "resultante — confira `version` para saber se houve versionamento."
    ),
    responses=error_responses(401, 403, 404, 422),
)
async def update_prompt(
    prompt_id: _PromptRef,
    payload: PromptUpdate,
    container: ContainerDep,
    principal: _Writer,
) -> PromptOut:
    """Atualiza o prompt e devolve a versao resultante."""
    prompt = await UpdatePrompt(container).execute(prompt_id, payload.to_input(), principal)
    return PromptOut.from_domain(prompt)


@router.delete(
    "/{prompt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remover prompt",
    description=(
        "Remove a versao resolvida pela referencia. Use `all_versions=true` para apagar "
        "o slug inteiro, historico incluido — operacao destrutiva, sem resposta com corpo."
    ),
    responses=error_responses(401, 403, 404),
)
async def delete_prompt(
    prompt_id: _PromptRef,
    container: ContainerDep,
    principal: _Writer,
    all_versions: Annotated[
        bool,
        Query(description="`true` apaga todas as versoes do slug, nao apenas a resolvida."),
    ] = False,
) -> Response:
    """Apaga a versao (ou o slug inteiro) e responde 204 sem corpo."""
    await DeletePrompt(container).execute(prompt_id, principal, all_versions=all_versions)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Preview e clonagem
# ---------------------------------------------------------------------------
@router.post(
    "/{prompt_id}/preview",
    response_model=PromptPreviewResponse,
    summary="Pre-visualizar a renderizacao de um prompt",
    description=(
        "Renderiza o template com as variaveis informadas e devolve `rendered` mais "
        "`missing`. Variavel faltando **nao e erro**: a lacuna volta no texto como "
        "`{{ variavel }}` e o nome aparece em `missing`, que e o que o editor destaca. "
        "Enviar `template` no corpo pre-visualiza um rascunho ainda nao salvo, sem ler "
        "nada do repositorio."
    ),
    responses=error_responses(401, 403, 404, 422),
)
async def preview_prompt(
    prompt_id: _PromptRef,
    payload: PromptPreviewRequest,
    container: ContainerDep,
    principal: _Reader,
) -> PromptPreviewResponse:
    """Devolve o texto renderizado e as variaveis que ainda faltam."""
    result = await PreviewPrompt(container).execute(
        prompt_id,
        dict(payload.variables),
        principal,
        template=payload.template,
    )
    return PromptPreviewResponse.from_result(result)


@router.post(
    "/{prompt_id}/clone",
    response_model=PromptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Clonar uma versao de prompt",
    description=(
        "Duplica o texto e os metadados da versao resolvida. Sem `target_slug` o clone "
        "vira a proxima versao do mesmo slug — o jeito de retomar a edicao a partir de "
        "uma versao antiga sem perder o historico; com `target_slug` nasce um prompt novo."
    ),
    responses=error_responses(401, 403, 404, 422),
)
async def clone_prompt(
    prompt_id: _PromptRef,
    container: ContainerDep,
    principal: _Writer,
    payload: PromptCloneRequest | None = None,
) -> PromptOut:
    """Grava o clone como nova versao e devolve o registro criado."""
    body = payload or PromptCloneRequest()
    prompt = await ClonePromptVersion(container).execute(
        prompt_id,
        principal,
        target_slug=body.target_slug,
        name=body.name,
        activate=body.activate,
    )
    return PromptOut.from_domain(prompt)
