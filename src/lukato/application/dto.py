"""DTOs de entrada e saida dos casos de uso da aplicacao.

Sao objetos de transporte puros: nenhuma regra de negocio, nenhuma dependencia de
I/O. A camada `interfaces` traduz JSON para estes tipos, os casos de uso os
consomem e devolvem modelos de dominio ou :class:`Page`.

`UNSET` distingue "campo ausente" de "campo enviado como null" nas atualizacoes
parciais: `ModuleUpdateInput(owner=None)` apaga o dono, enquanto
`ModuleUpdateInput()` o preserva.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Final, Generic, TypeAlias, TypeGuard, TypeVar

from lukato.domain.models.identity import Principal
from lukato.domain.models.module import ModuleBinding, ModuleKind, ModuleStatus
from lukato.domain.models.run import RunStatus
from lukato.domain.types import Id, Json
from lukato.modules.base import ModuleRequest, ModuleResponse

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "UNSET",
    "InvokeInput",
    "InvokeOutput",
    "Maybe",
    "ModuleCreateInput",
    "ModuleFilter",
    "ModuleUpdateInput",
    "Page",
    "PageRequest",
    "RunFilter",
    "UnsetType",
    "is_set",
    "value_or",
]

T = TypeVar("T")

DEFAULT_PAGE_LIMIT: Final[int] = 50
"""Tamanho de pagina usado quando o chamador nao informa um."""

MAX_PAGE_LIMIT: Final[int] = 200
"""Teto de itens por pagina: protege banco e serializacao de pedidos abusivos."""


# ---------------------------------------------------------------------------
# Sentinela de campo ausente
# ---------------------------------------------------------------------------
class UnsetType:
    """Sentinela de "campo nao informado" em atualizacoes parciais.

    E falsy e tem instancia unica, entao `if value:` e `value is UNSET` funcionam
    como esperado.
    """

    __slots__ = ()
    _instance: ClassVar[UnsetType | None] = None

    def __new__(cls) -> UnsetType:
        """Garante a instancia unica da sentinela."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        """A sentinela e sempre falsy."""
        return False

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final[UnsetType] = UnsetType()
"""Valor sentinela para campos omitidos em um `PATCH`/`PUT` parcial."""

Maybe: TypeAlias = T | UnsetType
"""Campo opcional de atualizacao parcial: o valor informado ou :data:`UNSET`."""


def is_set(value: Maybe[T]) -> TypeGuard[T]:
    """True quando o campo foi informado (mesmo que o valor seja `None`)."""
    return not isinstance(value, UnsetType)


def value_or(value: Maybe[T], fallback: T) -> T:
    """Devolve o valor informado ou o `fallback` quando o campo veio ausente."""
    return value if is_set(value) else fallback


# ---------------------------------------------------------------------------
# Paginacao
# ---------------------------------------------------------------------------
def _clamp_limit(limit: int) -> int:
    """Mantem o limite dentro de `1..MAX_PAGE_LIMIT`."""
    return max(1, min(int(limit), MAX_PAGE_LIMIT))


def _clamp_offset(offset: int) -> int:
    """Impede deslocamento negativo."""
    return max(0, int(offset))


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Pedido de paginacao ja normalizado (`limit` e `offset` sempre validos)."""

    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza os limites recebidos da borda HTTP."""
        object.__setattr__(self, "limit", _clamp_limit(self.limit))
        object.__setattr__(self, "offset", _clamp_offset(self.offset))


@dataclass(slots=True)
class Page(Generic[T]):
    """Pagina de resultados no formato normativo `items/total/limit/offset`."""

    items: list[T] = field(default_factory=list)
    total: int = 0
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    @classmethod
    def of(
        cls, items: Iterable[T], *, total: int | None = None, page: PageRequest | None = None
    ) -> Page[T]:
        """Monta a pagina a partir de um iteravel, herdando `limit`/`offset` do pedido."""
        materialized = list(items)
        window = page or PageRequest()
        return cls(
            items=materialized,
            total=len(materialized) if total is None else max(0, int(total)),
            limit=window.limit,
            offset=window.offset,
        )

    @property
    def count(self) -> int:
        """Quantidade de itens nesta pagina."""
        return len(self.items)

    @property
    def has_more(self) -> bool:
        """True quando ainda existem itens depois desta pagina."""
        return self.offset + len(self.items) < self.total

    def map(self, transform: Callable[[T], Any]) -> Page[Any]:
        """Aplica `transform` a cada item preservando os metadados de paginacao."""
        return Page(
            items=[transform(item) for item in self.items],
            total=self.total,
            limit=self.limit,
            offset=self.offset,
        )

    def to_dict(self, serializer: Callable[[T], Any] | None = None) -> Json:
        """Serializa a pagina no envelope de lista da API."""
        convert = serializer or (lambda item: item)
        return {
            "items": [convert(item) for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


# ---------------------------------------------------------------------------
# Invocacao de modulo
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class InvokeInput:
    """Pedido completo de invocacao de um building block."""

    slug: str
    request: ModuleRequest
    principal: Principal


@dataclass(frozen=True, slots=True)
class InvokeOutput:
    """Resposta de uma invocacao, com a trilha de execucao ja resolvida."""

    response: ModuleResponse
    run_id: Id | None = None
    trace_id: str | None = None
    status: RunStatus = RunStatus.SUCCEEDED
    latency_ms: float = 0.0

    @classmethod
    def from_response(cls, response: ModuleResponse) -> InvokeOutput:
        """Extrai `run_id`, `trace_id`, status e latencia dos metadados da resposta."""
        metadata = response.metadata or {}
        raw_status = metadata.get("status", RunStatus.SUCCEEDED.value)
        try:
            status = RunStatus(str(raw_status))
        except ValueError:
            status = RunStatus.SUCCEEDED
        trace_id = metadata.get("trace_id")
        return cls(
            response=response,
            run_id=response.run_id,
            trace_id=str(trace_id) if isinstance(trace_id, str) else None,
            status=status,
            latency_ms=float(metadata.get("latency_ms", 0.0) or 0.0),
        )


# ---------------------------------------------------------------------------
# CRUD de definicoes de modulo
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ModuleCreateInput:
    """Dados de criacao de uma `ModuleDefinition`.

    A classe do building block e o *codigo*; esta definicao e a *configuracao*.
    Duas definicoes sobre a mesma classe, com bindings diferentes, sao dois
    agentes diferentes (SPEC-0001 secao 5).
    """

    slug: str
    name: str = ""
    description: str = ""
    kind: ModuleKind = ModuleKind.AGENT
    status: ModuleStatus = ModuleStatus.DRAFT
    runtime: str = "langgraph"
    binding: ModuleBinding | None = None
    config: Json = field(default_factory=dict)
    tags: Sequence[str] = ()
    owner: str | None = None
    version: str = "1.0.0"


@dataclass(frozen=True, slots=True)
class ModuleUpdateInput:
    """Atualizacao parcial de uma `ModuleDefinition`; campos ausentes ficam :data:`UNSET`."""

    name: Maybe[str] = UNSET
    description: Maybe[str] = UNSET
    kind: Maybe[ModuleKind] = UNSET
    status: Maybe[ModuleStatus] = UNSET
    runtime: Maybe[str] = UNSET
    binding: Maybe[ModuleBinding] = UNSET
    config: Maybe[Json] = UNSET
    tags: Maybe[Sequence[str]] = UNSET
    owner: Maybe[str | None] = UNSET
    version: Maybe[str] = UNSET

    def changes(self) -> Json:
        """Mapa `campo -> valor` apenas com o que foi efetivamente informado."""
        candidates: dict[str, Maybe[Any]] = {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "status": self.status,
            "runtime": self.runtime,
            "binding": self.binding,
            "config": self.config,
            "tags": self.tags,
            "owner": self.owner,
            "version": self.version,
        }
        changed: Json = {}
        for name, value in candidates.items():
            if not is_set(value):
                continue
            changed[name] = list(value) if name == "tags" else value
        return changed


@dataclass(frozen=True, slots=True)
class ModuleFilter:
    """Filtros de listagem de definicoes de modulo."""

    kind: ModuleKind | None = None
    status: ModuleStatus | None = None
    search: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao."""
        object.__setattr__(self, "limit", _clamp_limit(self.limit))
        object.__setattr__(self, "offset", _clamp_offset(self.offset))

    @property
    def page(self) -> PageRequest:
        """Janela de paginacao correspondente a este filtro."""
        return PageRequest(limit=self.limit, offset=self.offset)


# ---------------------------------------------------------------------------
# Consulta de execucoes
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunFilter:
    """Filtros de listagem do historico de execucoes."""

    module_slug: str | None = None
    status: RunStatus | None = None
    since: datetime | None = None
    until: datetime | None = None
    tenant_id: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        """Normaliza a janela de paginacao."""
        object.__setattr__(self, "limit", _clamp_limit(self.limit))
        object.__setattr__(self, "offset", _clamp_offset(self.offset))

    @property
    def page(self) -> PageRequest:
        """Janela de paginacao correspondente a este filtro."""
        return PageRequest(limit=self.limit, offset=self.offset)
