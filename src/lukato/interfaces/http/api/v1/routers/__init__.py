"""Routers da API v1, um por recurso (SPEC-0000 secao 11).

Contrato que todo modulo deste pacote cumpre:

* expoe exatamente um atributo publico `router: APIRouter`;
* o proprio `router` declara o seu `prefix` (`/modules`, `/prompts`, ...) e a sua
  `tags` — o prefixo `/api/v1` e acrescentado uma unica vez por
  :data:`lukato.interfaces.http.api.v1.api_router`;
* nenhuma rota toca repositorio: a operacao passa por um caso de uso de
  `lukato.application.use_cases`, construido com o `Container` injetado por
  :func:`lukato.interfaces.http.deps.get_container`;
* a autorizacao vem de :func:`lukato.interfaces.http.deps.require`, que checa
  `Principal.can(...)` antes de a rota abrir qualquer transacao.

Este modulo nao importa nada de proposito: importar os routers aqui criaria um
ciclo com a base (`deps`, `schemas`) que eles proprios consomem.
"""

from __future__ import annotations

__all__: list[str] = []
