"""Casos de uso do lukato: o passo a passo de cada operacao da plataforma.

Cada caso de uso e uma classe com `__init__(self, container: Container)` e
`async def execute(...)`. Nenhum deles conhece adaptador ou framework: tudo
chega pelo :class:`~lukato.application.container.Container`.

`InvokeModule` e o caminho unico de execucao de building blocks e cumpre as onze
etapas normativas da SPEC-0001 secao 4.
"""

from __future__ import annotations

from lukato.application.use_cases.modules import (
    CLASS_CONFIG_KEYS,
    CreateModule,
    DeleteModule,
    GetModule,
    InvokeModule,
    ListModules,
    ModulePipeline,
    SetModuleStatus,
    UpdateModule,
    authorize,
)
from lukato.application.use_cases.runs import (
    CANCELLABLE_STATUSES,
    CancelRun,
    GetRun,
    GetRunSteps,
    ListRuns,
)

__all__ = [
    "CANCELLABLE_STATUSES",
    "CLASS_CONFIG_KEYS",
    "CancelRun",
    "CreateModule",
    "DeleteModule",
    "GetModule",
    "GetRun",
    "GetRunSteps",
    "InvokeModule",
    "ListModules",
    "ListRuns",
    "ModulePipeline",
    "SetModuleStatus",
    "UpdateModule",
    "authorize",
]
