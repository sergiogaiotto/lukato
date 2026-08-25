"""Building blocks plugaveis do lukato.

Este pacote expoe apenas o *contrato* (`lukato.modules.base`) e o *registry*
(`lukato.modules.registry`). O nucleo nunca importa um modulo concreto: os
embutidos sao carregados por nome e os externos por entry point.

Regra hexagonal: um building block pode importar `lukato.domain` e
`lukato.application`; nunca `lukato.adapters` nem `lukato.interfaces`.
"""

from __future__ import annotations

from lukato.modules.base import (
    SCHEMA_TYPES,
    BaseModule,
    ModuleContext,
    ModuleRequest,
    ModuleResponse,
    UIDescriptor,
    UINavItem,
    validate_against_schema,
)
from lukato.modules.registry import (
    BUILTIN_MODULE_NAMES,
    BUILTIN_PACKAGE,
    DEFAULT_ENTRY_POINT_GROUP,
    ModuleDescriptor,
    ModuleRegistry,
    ModuleSource,
    register_module,
    registry,
)

__all__ = [
    "BUILTIN_MODULE_NAMES",
    "BUILTIN_PACKAGE",
    "DEFAULT_ENTRY_POINT_GROUP",
    "SCHEMA_TYPES",
    "BaseModule",
    "ModuleContext",
    "ModuleDescriptor",
    "ModuleRegistry",
    "ModuleRequest",
    "ModuleResponse",
    "ModuleSource",
    "UIDescriptor",
    "UINavItem",
    "register_module",
    "registry",
    "validate_against_schema",
]
