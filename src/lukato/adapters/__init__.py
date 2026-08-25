"""Adaptadores dirigidos (driven) do lukato: persistencia, LLM, embeddings, midia.

Este pacote e deliberadamente vazio de importacoes: cada subpacote e carregado sob
demanda pelo *composition root*, de modo que a ausencia de uma dependencia opcional
(ou de rede) nunca quebre o import de `lukato.adapters`.
"""

from __future__ import annotations

__all__: list[str] = []
