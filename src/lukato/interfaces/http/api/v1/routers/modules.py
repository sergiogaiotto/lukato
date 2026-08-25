"""Rotas das definicoes de modulo e o caminho unico de invocacao (SPEC-0001).

Uma `ModuleDefinition` e **configuracao**, nao codigo: ela aponta para a classe
de um building block registrado e amarra a trinca obrigatoria

```text
guardrail de entrada -> system prompt -> guardrail de saida
```

Trocar qualquer uma das tres pecas e editar um registro, nunca escrever Python.
Por isso o CRUD daqui e tao importante quanto a rota de execucao: duas definicoes
sobre a mesma classe `processing`, com bindings diferentes, sao dois agentes
diferentes (SPEC-0001 secao 5).

`POST /{slug}/invoke` delega **inteiramente** a `InvokeModule`, o unico lugar do
sistema onde um building block executa. A rota nao aplica guardrail, nao renderiza
prompt e nao chama provedor: se algum dia precisasse, a trinca teria dois donos e
um deles esqueceria uma etapa. Bloqueio de guardrail sobe como `GuardrailViolation`
e vira `422` no handler global — capturar aqui esconderia os `findings` que o
cliente precisa ler.

`POST /{slug}/dry-run` e o irmao seguro dessa rota: executa **somente** o
guardrail de entrada e a renderizacao do system prompt, mostra o que seria
enviado ao provedor e para ali. Nenhum token e gasto, nenhum `AgentRun` nasce.
E a ferramenta que responde "por que este modulo respondeu isso?" antes de a
pergunta virar um incidente.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Response, status
from pydantic import ConfigDict, Field

from lukato.application.dto import InvokeInput, ModuleFilter
from lukato.application.use_cases.guardrails import TestPolicy
from lukato.application.use_cases.modules import (
    CreateModule,
    DeleteModule,
    GetModule,
    InvokeModule,
    ListModules,
    SetModuleStatus,
    UpdateModule,
)
from lukato.application.use_cases.prompts import PreviewPrompt
from lukato.domain.models.guardrail import GuardrailStage
from lukato.domain.models.identity import Permission, Principal
from lukato.domain.models.module import ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.types import Id, Json
from lukato.interfaces.http.deps import ContainerDep, PaginationDep, require
from lukato.interfaces.http.schemas.common import OutSchema, Page, error_responses
from lukato.interfaces.http.schemas.guardrails import PolicyTestResponse
from lukato.interfaces.http.schemas.modules import (
    ModuleBindingOut,
    ModuleCreate,
    ModuleInvokeRequest,
    ModuleInvokeResponse,
    ModuleOut,
    ModuleStatusUpdate,
    ModuleUpdate,
)
from lukato.modules.base import ModuleRequest

__all__ = [
    "RUN_ID_HEADER",
    "TRACE_ID_HEADER",
    "DryRunMessage",
    "DryRunPlan",
    "DryRunPrompt",
    "ModuleDryRunResponse",
    "router",
]

router = APIRouter(prefix="/modules", tags=["modulos"])
"""Rotas do catalogo de definicoes de modulo, sob `/api/v1/modules`."""

TRACE_ID_HEADER: Final[str] = "X-Trace-Id"
"""Cabecalho com o `trace_id` da execucao (SPEC-0008 secao 2), quando ha tracer."""

RUN_ID_HEADER: Final[str] = "X-Run-Id"
"""Cabecalho com o `AgentRun` da execucao — presente em TODA invocacao."""

_ReaderDep = Annotated[Principal, Depends(require(Permission.MODULE_READ))]
"""Leitura do catalogo exige `module:read`."""

_WriterDep = Annotated[Principal, Depends(require(Permission.MODULE_WRITE))]
"""Escrita no catalogo exige `module:write`."""

_InvokerDep = Annotated[Principal, Depends(require(Permission.MODULE_INVOKE))]
"""Executar (ou simular) um modulo exige `module:invoke`."""

_SlugPath = Annotated[
    str,
    Path(description="Slug do modulo; o identificador (`id`) tambem e aceito."),
]
"""Referencia da definicao na URL: slug legivel ou `id`, resolvidos nesta ordem."""

_READ_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404)
"""Erros das rotas que resolvem uma definicao existente."""

_WRITE_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404, 409, 422)
"""Erros das rotas de escrita, que ainda podem conflitar ou falhar na validacao."""

_INVOKE_ERRORS: Final[dict[int | str, dict[str, Any]]] = error_responses(
    401, 402, 403, 404, 409, 422, 429, 501, 502
)
"""Erros da invocacao: orcamento, guardrail, runtime indisponivel e provedor."""


# ---------------------------------------------------------------------------
# Schemas do ensaio (`dry-run`)
# ---------------------------------------------------------------------------
class DryRunMessage(OutSchema):
    """Mensagem que seria enviada ao provedor, na ordem exata do envio."""

    role: str = Field(description="Papel da mensagem: `system`, `user` ou `assistant`.")
    content: str = Field(description="Conteudo textual ja resolvido.")


class DryRunPrompt(OutSchema):
    """System prompt renderizado, com as lacunas explicitas.

    Variavel faltante **nao** e erro aqui: o ensaio existe para mostrar o que
    falta. A lacuna volta preservada como `{{ variavel }}` dentro de `rendered` e
    nomeada em `missing` — o mesmo comportamento do preview da biblioteca de
    prompts.
    """

    bound: bool = Field(
        default=False, description="False quando o modulo nao vincula system prompt."
    )
    prompt_id: Id | None = Field(default=None, description="Prompt vinculado no binding.")
    slug: str = Field(default="", description="Slug do prompt resolvido.")
    version: int | None = Field(default=None, description="Versao efetivamente usada.")
    role: str = Field(default="", description="Papel declarado no template.")
    rendered: str = Field(default="", description="Texto final que iria como `system`.")
    variables: list[str] = Field(
        default_factory=list, description="Variaveis exigidas pelo template."
    )
    missing: list[str] = Field(
        default_factory=list, description="Variaveis exigidas que nao foram informadas."
    )
    unused: list[str] = Field(
        default_factory=list, description="Variaveis informadas que o template ignora."
    )
    complete: bool = Field(default=True, description="True quando nada ficou faltando.")


class DryRunPlan(OutSchema):
    """Parametros e mensagens que a chamada real usaria."""

    runtime: str = Field(description="Runtime que executaria o modulo.")
    model: str = Field(description="Modelo efetivo (binding ou padrao da instalacao).")
    temperature: float | None = Field(default=None, description="Temperatura efetiva.")
    max_tokens: int | None = Field(default=None, description="Teto de tokens efetivo.")
    timeout_seconds: float = Field(default=60.0, description="Teto de tempo da execucao.")
    tools: list[str] = Field(default_factory=list, description="Ferramentas liberadas.")
    messages: list[DryRunMessage] = Field(
        default_factory=list, description="Conversa montada: system, historico e a entrada."
    )


class ModuleDryRunResponse(OutSchema):
    """Resultado do ensaio: veredito de entrada, prompt e envio simulado."""

    module_id: Id = Field(description="Definicao ensaiada.")
    module_slug: str = Field(description="Slug da definicao ensaiada.")
    module_status: ModuleStatus = Field(description="Status atual da definicao.")
    invocable: bool = Field(
        description="True quando o status permite `POST /invoke`; ensaio funciona em qualquer um."
    )
    allowed: bool = Field(description="False quando o guardrail de entrada barraria o pedido.")
    would_call_provider: bool = Field(
        description="True quando a execucao real chegaria a chamar o provedor."
    )
    input_guardrail: PolicyTestResponse = Field(
        description="Veredito completo do guardrail de ENTRADA (etapa 6)."
    )
    output_guardrail_id: Id | None = Field(
        default=None, description="Politica que avaliaria a resposta (etapa 9), ainda nao aplicada."
    )
    system_prompt: DryRunPrompt = Field(description="System prompt renderizado (etapa 7).")
    plan: DryRunPlan = Field(description="O que seria enviado ao provedor (etapa 8).")
    binding: ModuleBindingOut = Field(description="Trinca vigente da definicao.")

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "module_id": "11111111-2222-3333-4444-555555555555",
                "module_slug": "atendimento",
                "module_status": "active",
                "invocable": True,
                "allowed": True,
                "would_call_provider": True,
                "input_guardrail": {
                    "allowed": True,
                    "blocked": False,
                    "modified": False,
                    "stage": "input",
                    "content": "Explique a cobranca de julho.",
                    "original_content": "Explique a cobranca de julho.",
                    "findings": [],
                    "policy_id": None,
                    "latency_ms": 0.9,
                },
                "output_guardrail_id": None,
                "system_prompt": {
                    "bound": True,
                    "prompt_id": "9f2a1b0c-1111-2222-3333-444455556666",
                    "slug": "atendimento-base",
                    "version": 3,
                    "role": "system",
                    "rendered": "Voce atende clientes pelo canal app.",
                    "variables": ["canal"],
                    "missing": [],
                    "unused": [],
                    "complete": True,
                },
                "plan": {
                    "runtime": "langgraph",
                    "model": "qwen-latest",
                    "temperature": 0.2,
                    "max_tokens": 1024,
                    "timeout_seconds": 60.0,
                    "tools": [],
                    "messages": [
                        {"role": "system", "content": "Voce atende clientes pelo canal app."},
                        {"role": "user", "content": "Explique a cobranca de julho."},
                    ],
                },
                "binding": {
                    "input_guardrail_id": None,
                    "system_prompt_id": "9f2a1b0c-1111-2222-3333-444455556666",
                    "output_guardrail_id": None,
                    "model": "qwen-latest",
                    "temperature": 0.2,
                    "max_tokens": 1024,
                    "timeout_seconds": 60.0,
                    "tools": [],
                },
            }
        },
    )


# ---------------------------------------------------------------------------
# Utilitarios do ensaio
# ---------------------------------------------------------------------------
def _prompt_variables(
    definition: ModuleDefinition, request: ModuleRequest, principal: Principal, *, text: str
) -> Json:
    """Monta as variaveis do system prompt exatamente como faz a etapa 7.

    Ordem: `config.variables` da definicao, sobrescrita pelas variaveis do pedido,
    completada pelo contexto implicito (`input`, `module_slug`, `tenant_id`). O
    `input` usado e o **ja filtrado** pelo guardrail de entrada, porque e esse o
    texto que a execucao real levaria adiante.
    """
    declared = definition.config.get("variables")
    variables: Json = dict(declared) if isinstance(declared, dict) else {}
    variables.update(request.variables)
    variables.setdefault("input", text)
    variables.setdefault("module_slug", definition.slug)
    variables.setdefault("tenant_id", principal.tenant_id)
    return variables


def _planned_messages(
    request: ModuleRequest, *, system_prompt: str, text: str
) -> list[DryRunMessage]:
    """Monta `[system?] + historico + usuario`, a mesma ordem do envio real."""
    messages: list[DryRunMessage] = []
    if system_prompt.strip():
        messages.append(DryRunMessage(role="system", content=system_prompt))
    messages.extend(DryRunMessage(role=item.role, content=item.content) for item in request.history)
    messages.append(DryRunMessage(role="user", content=text))
    return messages


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=Page[ModuleOut],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Lista definicoes de modulo",
    description=(
        "Pagina o catalogo de definicoes, filtrando por tipo funcional, status do "
        "ciclo de vida e busca textual em slug, nome e descricao. A resposta usa o "
        "envelope normativo `items/total/limit/offset`."
    ),
)
async def list_modules(
    container: ContainerDep,
    principal: _ReaderDep,
    pagination: PaginationDep,
    kind: Annotated[ModuleKind | None, Query(description="Filtra pelo tipo funcional.")] = None,
    module_status: Annotated[
        ModuleStatus | None,
        Query(alias="status", description="Filtra pelo estado do ciclo de vida."),
    ] = None,
    search: Annotated[
        str | None, Query(description="Busca textual em slug, nome e descricao.")
    ] = None,
) -> Page[ModuleOut]:
    """Devolve a pagina de definicoes que satisfazem os filtros."""
    filters = ModuleFilter(
        kind=kind,
        status=module_status,
        search=search,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    result = await ListModules(container).execute(filters, principal)
    return Page.from_result(result, ModuleOut.from_domain)


@router.post(
    "",
    response_model=ModuleOut,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 409, 422),
    summary="Cria uma definicao de modulo",
    description=(
        "Registra a *configuracao* de um building block: slug, tipo, runtime e a "
        "trinca parametrizavel. A `config` e validada contra o `config_schema` da "
        "classe correspondente quando ela ja esta registrada. Slug repetido "
        "responde `409`."
    ),
)
async def create_module(
    container: ContainerDep, principal: _WriterDep, body: ModuleCreate
) -> ModuleOut:
    """Cria a definicao e devolve o registro gravado."""
    created = await CreateModule(container).execute(body.to_input(), principal)
    return ModuleOut.from_domain(created)


@router.get(
    "/{slug}",
    response_model=ModuleOut,
    status_code=status.HTTP_200_OK,
    responses=_READ_ERRORS,
    summary="Busca uma definicao de modulo",
    description=(
        "Resolve a definicao por slug e, se nao houver, por identificador. "
        "Referencia desconhecida responde `404`."
    ),
)
async def get_module(container: ContainerDep, principal: _ReaderDep, slug: _SlugPath) -> ModuleOut:
    """Devolve a definicao pedida."""
    definition = await GetModule(container).execute(slug, principal)
    return ModuleOut.from_domain(definition)


@router.put(
    "/{slug}",
    response_model=ModuleOut,
    status_code=status.HTTP_200_OK,
    responses=_WRITE_ERRORS,
    summary="Atualiza uma definicao de modulo",
    description=(
        "Atualizacao parcial: apenas os campos enviados mudam. A distincao entre "
        "'campo ausente' e 'campo nulo' e respeitada — `owner: null` apaga o dono, "
        "`owner` ausente o preserva."
    ),
)
async def update_module(
    container: ContainerDep, principal: _WriterDep, slug: _SlugPath, body: ModuleUpdate
) -> ModuleOut:
    """Aplica as mudancas informadas e devolve a definicao resultante."""
    updated = await UpdateModule(container).execute(slug, body.to_input(), principal)
    return ModuleOut.from_domain(updated)


@router.patch(
    "/{slug}/status",
    response_model=ModuleOut,
    status_code=status.HTTP_200_OK,
    responses=_WRITE_ERRORS,
    summary="Muda o status de uma definicao",
    description=(
        "Publica (`active`), pausa (`paused`), rascunha (`draft`) ou deprecia "
        "(`deprecated`) a definicao. Somente `active` pode ser invocado. Repetir o "
        "status atual e idempotente e nao gera nova versao."
    ),
)
async def set_module_status(
    container: ContainerDep, principal: _WriterDep, slug: _SlugPath, body: ModuleStatusUpdate
) -> ModuleOut:
    """Altera o ciclo de vida da definicao."""
    updated = await SetModuleStatus(container).execute(slug, body.status, principal)
    return ModuleOut.from_domain(updated)


@router.delete(
    "/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_READ_ERRORS,
    summary="Remove uma definicao de modulo",
    description=(
        "Apaga a *configuracao*. A classe do building block continua registrada e "
        "pode receber uma definicao nova a qualquer momento; as execucoes ja "
        "gravadas permanecem no historico."
    ),
)
async def delete_module(
    container: ContainerDep, principal: _WriterDep, slug: _SlugPath
) -> Response:
    """Remove a definicao e responde sem corpo."""
    await DeleteModule(container).execute(slug, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Execucao
# ---------------------------------------------------------------------------
@router.post(
    "/{slug}/invoke",
    response_model=ModuleInvokeResponse,
    status_code=status.HTTP_200_OK,
    responses=_INVOKE_ERRORS,
    summary="Invoca um modulo",
    description=(
        "Executa o building block pelas onze etapas normativas, nesta ordem exata: "
        "resolve a definicao, exige `active`, checa a permissao, verifica orcamento, "
        "abre o trace e o `AgentRun`, aplica o **guardrail de entrada**, renderiza o "
        "**system prompt**, executa o runtime, aplica o **guardrail de saida**, "
        "registra consumo e custo e finaliza a execucao.\n\n"
        "Bloqueio em qualquer guardrail responde `422` com `code = guardrail_violation` "
        "e a lista de achados em `details.findings`. Orcamento com parada dura "
        "responde `402`. Toda invocacao devolve `X-Run-Id`; `X-Trace-Id` sai quando ha "
        "trace, e deixa um `AgentRun` persistido — nao existe execucao invisivel."
    ),
)
async def invoke_module(
    container: ContainerDep,
    principal: _InvokerDep,
    response: Response,
    slug: _SlugPath,
    body: ModuleInvokeRequest,
) -> ModuleInvokeResponse:
    """Executa o modulo e devolve saida, custo, achados e rastro da execucao."""
    output = await InvokeModule(container).invoke(
        InvokeInput(slug=slug, request=body.to_request(), principal=principal)
    )
    # `X-Run-Id` sai sempre: o AgentRun e persistido em toda invocacao, inclusive
    # nas bloqueadas, entao ele e o identificador de correlacao que nunca falta.
    # `X-Trace-Id` sai apenas quando ha tracer de verdade — com o `NoopTracer` nao
    # existe trace, e inventar um id apontaria o operador para um rastro inexistente.
    if output.response.run_id:
        response.headers[RUN_ID_HEADER] = output.response.run_id
    if output.trace_id:
        response.headers[TRACE_ID_HEADER] = output.trace_id
    return ModuleInvokeResponse.from_domain(output.response)


@router.post(
    "/{slug}/dry-run",
    response_model=ModuleDryRunResponse,
    status_code=status.HTTP_200_OK,
    responses=_READ_ERRORS,
    summary="Ensaia uma invocacao sem chamar o provedor",
    description=(
        "Executa **somente** as etapas verificaveis sem custo: o guardrail de "
        "entrada (etapa 6) e a renderizacao do system prompt (etapa 7). Devolve o "
        "veredito de entrada completo, o texto final do prompt e a conversa exata "
        "que iria ao provedor — e para ali.\n\n"
        "Nenhum token e gasto, nenhum `AgentRun` nasce e nenhum orcamento e "
        "consumido. Guardrail que barraria o pedido **nao** vira `422` aqui: o "
        "bloqueio e o resultado que se quer ver, e volta em `input_guardrail` com "
        "`allowed=false` e os achados por regra. Diferente de `/invoke`, o ensaio "
        "funciona com a definicao em qualquer status — e para depurar rascunho que "
        "ele existe."
    ),
)
async def dry_run_module(
    container: ContainerDep,
    principal: _InvokerDep,
    slug: _SlugPath,
    body: ModuleInvokeRequest,
) -> ModuleDryRunResponse:
    """Aplica guardrail de entrada e prompt, e descreve o envio que nao aconteceu."""
    definition = await GetModule(container).execute(slug, principal)
    binding = definition.binding
    request = body.to_request()

    verdict = await TestPolicy(container).execute(
        binding.input_guardrail_id,
        request.input,
        principal,
        stage=GuardrailStage.INPUT,
        context={"module_slug": definition.slug, "dry_run": True},
    )

    text = verdict.content
    if binding.system_prompt_id is None:
        prompt = DryRunPrompt()
    else:
        preview = await PreviewPrompt(container).execute(
            binding.system_prompt_id,
            _prompt_variables(definition, request, principal, text=text),
            principal,
        )
        prompt = DryRunPrompt.model_validate(
            {**preview, "bound": True, "prompt_id": binding.system_prompt_id}
        )

    composer = container.composer
    plan = DryRunPlan(
        runtime=definition.runtime,
        model=binding.model or composer.default_model,
        temperature=(
            composer.default_temperature if binding.temperature is None else binding.temperature
        ),
        max_tokens=(
            composer.default_max_tokens if binding.max_tokens is None else binding.max_tokens
        ),
        timeout_seconds=binding.timeout_seconds,
        tools=list(binding.tools),
        messages=_planned_messages(request, system_prompt=prompt.rendered, text=text),
    )
    return ModuleDryRunResponse(
        module_id=definition.id,
        module_slug=definition.slug,
        module_status=definition.status,
        invocable=definition.status is ModuleStatus.ACTIVE,
        allowed=verdict.allowed,
        would_call_provider=not verdict.blocked,
        input_guardrail=PolicyTestResponse.from_domain(verdict),
        output_guardrail_id=binding.output_guardrail_id,
        system_prompt=prompt,
        plan=plan,
        binding=ModuleBindingOut.from_domain(binding),
    )
