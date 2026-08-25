"""Rotas do registry de building blocks (SPEC-0002).

O registry responde a pergunta "o que esta **instalado** nesta instancia?" — que
e diferente de "o que esta **configurado**?", respondida por `/api/v1/modules`.
A distincao e o coracao do ecossistema (SPEC-0001 secao 3):

* o **registry** guarda `slug -> classe`: e o *codigo* disponivel, descoberto por
  entry point ou carregado dos embutidos;
* o recurso **modules** guarda `ModuleDefinition`: e a *configuracao* — binding,
  status, runtime. Duas definicoes sobre a mesma classe `processing`, com
  bindings diferentes, sao dois agentes diferentes sem uma linha de codigo nova.

`GET /api/v1/registry` reflete exatamente `registry.describe()` (SPEC-0002 secao
4): a lista sai como esta, sem envelope de paginacao, porque o registry nao e uma
colecao paginavel e sim o inventario completo do processo.

`POST /api/v1/registry/discover` reexecuta a varredura sem reiniciar a aplicacao
— e o que permite instalar um modulo externo e ve-lo aparecer no console. Uma
descoberta pode falhar parcialmente: entry point quebrado vira WARNING e entra em
`errors`, nunca derruba o restante da varredura.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, status
from pydantic import Field

from lukato.config import get_logger
from lukato.domain.errors import ModuleNotFound
from lukato.domain.models.identity import Permission, Principal
from lukato.interfaces.http.deps import ContainerDep, require
from lukato.interfaces.http.schemas.common import OutSchema, Page, error_responses
from lukato.modules.registry import (
    DEFAULT_ENTRY_POINT_GROUP,
    ModuleDescriptor,
    ModuleRegistry,
)

__all__ = ["DiscoveryError", "DiscoveryResponse", "router"]

_logger = get_logger(__name__)

router = APIRouter(prefix="/registry", tags=["registry"])
"""Rotas do inventario de building blocks, sob `/api/v1/registry`."""

_ReaderDep = Annotated[Principal, Depends(require(Permission.MODULE_READ))]
"""Leitura do inventario exige `module:read`."""

_WriterDep = Annotated[Principal, Depends(require(Permission.MODULE_WRITE))]
"""Reexecutar a descoberta muda o estado do processo: exige `module:write`."""

_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = error_responses(401, 403, 404)
"""Respostas de erro das rotas que resolvem um slug."""


# ---------------------------------------------------------------------------
# Schemas proprios desta rota
# ---------------------------------------------------------------------------
class DiscoveryError(OutSchema):
    """Falha isolada de carga durante a descoberta.

    Um entry point quebrado nao pode impedir que os demais modulos carreguem: a
    falha e reportada aqui, nomeando a origem, e o boot segue (SPEC-0002 regra 2).
    """

    origin: str = Field(description="Origem que falhou (entry point ou modulo embutido).")
    reason: str = Field(description="Tipo e mensagem da excecao que impediu a carga.")


class DiscoveryResponse(OutSchema):
    """Resultado de uma reexecucao da descoberta de building blocks."""

    entry_points: int = Field(
        default=0, ge=0, description="Modulos registrados a partir de entry points."
    )
    builtin: int = Field(default=0, ge=0, description="Modulos embutidos registrados agora.")
    registered: int = Field(
        default=0, ge=0, description="Total registrado nesta varredura (novos apenas)."
    )
    total: int = Field(default=0, ge=0, description="Modulos registrados apos a varredura.")
    slugs: list[str] = Field(default_factory=list, description="Slugs disponiveis, em ordem.")
    errors: list[DiscoveryError] = Field(
        default_factory=list, description="Falhas desta varredura; lista vazia significa sucesso."
    )
    entry_point_group: str = Field(
        default=DEFAULT_ENTRY_POINT_GROUP, description="Grupo de entry points varrido."
    )


# ---------------------------------------------------------------------------
# Utilitarios
# ---------------------------------------------------------------------------
def _descriptor(registry: ModuleRegistry, slug: str) -> ModuleDescriptor:
    """Descreve um unico building block; slug ausente levanta `ModuleNotFound` (404)."""
    registry.get(slug)
    for descriptor in registry.describe():
        if descriptor.slug == slug:
            return descriptor
    raise ModuleNotFound(
        f"O building block '{slug}' esta registrado, mas nao pode ser descrito.",
        details={"slug": slug, "available": registry.slugs()},
    )


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=Page[ModuleDescriptor],
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Lista os building blocks instalados",
    description=(
        "Inventario completo do processo: para cada building block registrado, o "
        "slug, o tipo, a versao, as capacidades declaradas, o schema de "
        "configuracao, o binding padrao, a origem (`builtin` ou `entry_point`) e a "
        "presenca na UI. Reflete exatamente `registry.describe()`."
    ),
)
async def list_registry(container: ContainerDep, principal: _ReaderDep) -> Page[ModuleDescriptor]:
    """Devolve os descritores de todos os building blocks registrados.

    Envelopado em `Page` como toda listagem da API (SPEC-0000 secao 11), mesmo o
    registry sendo um conjunto pequeno e fixo: um cliente nao deveria precisar
    saber, endpoint a endpoint, se a resposta vem crua ou envelopada.
    """
    descritores = container.registry.describe()
    return Page.of(descritores, total=len(descritores), limit=len(descritores), offset=0)


@router.post(
    "/discover",
    response_model=DiscoveryResponse,
    status_code=status.HTTP_200_OK,
    responses=error_responses(401, 403),
    summary="Reexecuta a descoberta de building blocks",
    description=(
        "Varre novamente o grupo de entry points `lukato.modules` e recarrega os "
        "modulos embutidos, sem reiniciar a aplicacao — e assim que um modulo "
        "instalado agora aparece no console. A contagem informa quantos foram "
        "**registrados nesta varredura**; modulos ja presentes nao contam de novo. "
        "Falha de carga isolada nao interrompe a varredura: ela aparece em `errors`."
    ),
)
async def discover(container: ContainerDep, principal: _WriterDep) -> DiscoveryResponse:
    """Reexecuta `discover()` + `load_builtin()` e devolve contagem e falhas."""
    registry = container.registry
    seen = len(registry.discover_errors)
    entry_points = registry.discover()
    builtin = registry.load_builtin()
    failures = [
        DiscoveryError(origin=origin, reason=reason)
        for origin, reason in registry.discover_errors[seen:]
    ]
    _logger.info(
        "registry_discovered",
        actor=principal.subject,
        entry_points=entry_points,
        builtin=builtin,
        total=len(registry),
        errors=len(failures),
    )
    return DiscoveryResponse(
        entry_points=entry_points,
        builtin=builtin,
        registered=entry_points + builtin,
        total=len(registry),
        slugs=registry.slugs(),
        errors=failures,
    )


@router.get(
    "/{slug}",
    response_model=ModuleDescriptor,
    status_code=status.HTTP_200_OK,
    responses=_NOT_FOUND,
    summary="Descreve um building block",
    description=(
        "Descritor de um unico building block registrado. Slug desconhecido "
        "responde `404` com a lista dos slugs disponiveis em `details.available`."
    ),
)
async def get_registry_entry(
    container: ContainerDep,
    principal: _ReaderDep,
    slug: Annotated[str, Path(description="Slug do building block registrado.")],
) -> ModuleDescriptor:
    """Devolve o descritor do building block pedido."""
    return _descriptor(container.registry, slug)
