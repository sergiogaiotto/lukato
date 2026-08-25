"""Camada de aplicacao do lukato: container de dependencias, DTOs e casos de uso.

Esta camada responde *qual e o passo a passo*. Ela importa `domain` e o contrato
dos building blocks (`lukato.modules`), e **nunca** `adapters` ou `interfaces`:
a composicao concreta acontece no composition root (SPEC-0000 secao 2).
"""

from __future__ import annotations

from lukato.application.container import (
    DEFAULT_RUNTIME,
    KNOWN_RUNTIMES,
    Container,
    ToolCatalog,
)
from lukato.application.dto import (
    UNSET,
    InvokeInput,
    InvokeOutput,
    Maybe,
    ModuleCreateInput,
    ModuleFilter,
    ModuleUpdateInput,
    Page,
    PageRequest,
    RunFilter,
    UnsetType,
    is_set,
    value_or,
)
from lukato.application.use_cases import (
    CancelRun,
    CreateModule,
    DeleteModule,
    GetModule,
    GetRun,
    GetRunSteps,
    InvokeModule,
    ListModules,
    ListRuns,
    ModulePipeline,
    SetModuleStatus,
    UpdateModule,
    authorize,
)

__all__ = [
    "DEFAULT_RUNTIME",
    "KNOWN_RUNTIMES",
    "UNSET",
    "CancelRun",
    "Container",
    "CreateModule",
    "DeleteModule",
    "GetModule",
    "GetRun",
    "GetRunSteps",
    "InvokeInput",
    "InvokeModule",
    "InvokeOutput",
    "ListModules",
    "ListRuns",
    "Maybe",
    "ModuleCreateInput",
    "ModuleFilter",
    "ModulePipeline",
    "ModuleUpdateInput",
    "Page",
    "PageRequest",
    "RunFilter",
    "SetModuleStatus",
    "ToolCatalog",
    "UnsetType",
    "UpdateModule",
    "authorize",
    "is_set",
    "value_or",
]
