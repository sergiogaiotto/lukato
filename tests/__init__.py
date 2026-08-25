"""Suite de testes do lukato 1.0.0.

Roda inteira offline: SQLite/aiosqlite em memoria, `EchoLLM`, `HashingEmbedder` e
`NoopTracer`. Nenhum teste desta suite abre socket, toca o relogio real ou depende
da ordem de execucao.
"""

from __future__ import annotations
