"""Modulos embutidos do lukato (`auth`, `processing`, `finops`, `knowledge`, `adwatch`).

Este pacote nao reexporta nada de proposito: `ModuleRegistry.load_builtin()`
importa cada arquivo por nome (`auth_module`, `processing_module`,
`finops_module`, `knowledge_module`, `adwatch_module`) e registra as classes de
building block que ele define. Um arquivo ausente vira WARNING em
`registry.discover_errors` e nao impede o boot.
"""

from __future__ import annotations
