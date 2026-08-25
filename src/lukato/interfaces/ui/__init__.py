"""Console web do lukato: Jinja2 puro, servido da propria origem (SPEC-0009).

O pacote entrega tres coisas ao composition root e nada mais:

* :data:`~lukato.interfaces.ui.router.router` — todas as rotas de pagina, montadas
  na raiz (`/`), fora do OpenAPI;
* :func:`~lukato.interfaces.ui.router.mount_static` — montagem de `static/` em
  `/static`, o unico lugar de onde CSS, JS, icones e imagens sao servidos;
* :func:`~lukato.interfaces.ui.context.base_context` — a moldura comum de toda
  pagina (navegacao, saude, custo, configuracao publica, painel de contexto).

Uso tipico::

    from lukato.interfaces.ui import mount_static, router as ui_router

    app.include_router(ui_router)
    mount_static(app)

Regra que atravessa o pacote inteiro: **nenhuma rota acessa repositorio**. Toda
leitura passa por um caso de uso de `lukato.application.use_cases` construido com
o `Container` injetado (SPEC-0000 secao 2).
"""

from __future__ import annotations

from lukato.interfaces.ui.context import (
    NAV_SECTIONS,
    SUPPORTED_ENTITIES,
    Crumb,
    NavItem,
    NavSection,
    base_context,
    mask_secret,
    mask_url,
    settings_public,
)
from lukato.interfaces.ui.filters import register_filters
from lukato.interfaces.ui.router import (
    CSP_POLICY,
    STATIC_DIR,
    STATIC_URL,
    TEMPLATES_DIR,
    mount_static,
    router,
    templates,
)

__all__ = [
    "CSP_POLICY",
    "NAV_SECTIONS",
    "STATIC_DIR",
    "STATIC_URL",
    "SUPPORTED_ENTITIES",
    "TEMPLATES_DIR",
    "Crumb",
    "NavItem",
    "NavSection",
    "base_context",
    "mask_secret",
    "mask_url",
    "mount_static",
    "register_filters",
    "router",
    "settings_public",
    "templates",
]
