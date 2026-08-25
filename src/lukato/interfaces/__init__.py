"""Camada de interfaces (driving adapters) do lukato.

Aqui vivem as bordas que **conduzem** a aplicacao: a API HTTP (`http/`), o console
Jinja2 (`ui/`) e a linha de comando (`cli.py`). Nenhuma delas contem regra de
negocio: toda operacao e delegada a um caso de uso de
:mod:`lukato.application.use_cases`, construido com o `Container` injetado pelo
composition root (SPEC-0000 secoes 2 e 11).
"""

from __future__ import annotations

__all__: list[str] = []
