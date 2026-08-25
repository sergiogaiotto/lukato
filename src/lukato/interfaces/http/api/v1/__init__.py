"""Roteador agregador da API v1: `/api/v1` com todos os recursos da plataforma.

O prefixo aparece **uma unica vez**, aqui. Cada router traz apenas o seu proprio
caminho relativo (`/modules`, `/prompts`, ...) e a sua tag, o que mantem cada
arquivo legivel e evita que dois recursos divirjam sobre onde a versao entra.

A ordem de inclusao e a mesma da SPEC-0000 secao 11 e define a ordem das secoes
no Swagger: primeiro o que descreve a instalacao (sistema, registry), depois o
nucleo de execucao (modulos, execucoes), a configuracao da trinca (prompts,
guardrails) e por fim os dominios de apoio (conhecimento, finops, identidade,
adwatch).
"""

from __future__ import annotations

from fastapi import APIRouter

from lukato.interfaces.http.api.v1.routers import (
    adwatch,
    finops,
    guardrails,
    health,
    identity,
    knowledge,
    modules,
    prompts,
    registry,
    runs,
)

__all__ = ["API_V1_PREFIX", "api_router"]

API_V1_PREFIX = "/api/v1"
"""Prefixo normativo de toda a API versionada."""

api_router = APIRouter(prefix=API_V1_PREFIX)
"""Roteador unico incluido pela aplicacao em `create_app`."""

api_router.include_router(health.router)
api_router.include_router(registry.router)
api_router.include_router(modules.router)
api_router.include_router(runs.router)
api_router.include_router(prompts.router)
api_router.include_router(guardrails.router)
api_router.include_router(knowledge.router)
api_router.include_router(finops.router)
api_router.include_router(identity.router)
api_router.include_router(adwatch.router)
