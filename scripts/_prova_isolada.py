"""Banco descartavel para os scripts de prova.

Os dois scripts de prova anunciam rodar "sem PostgreSQL". Isso vale enquanto
nao existe um ``.env`` — mas o primeiro passo documentado no README e justamente
``cp .env.example .env``, e a partir dai ``get_settings()`` le de la a URL do
PostgreSQL. As provas passavam a escrever no banco de verdade e morriam no
segundo run, em ``ConflictError``, sobre as proprias sementes que acabaram de
gravar.

Uma prova que so roda uma vez, e so antes de configurar o projeto, nao prova
nada para quem precisa dela. Este modulo fixa um SQLite novo em disco temporario
a cada execucao, ignorando o ambiente, e devolve o caminho para limpeza.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Final

_VARIAVEIS_DE_BANCO: Final[tuple[str, ...]] = (
    "LUKATO_DB__URL",
    "LUKATO_DB__FALLBACK_URL",
    "LUKATO_DB__AUTO_FALLBACK",
    "LUKATO_DB__CREATE_ALL",
    "LUKATO_DB__RUN_MIGRATIONS",
    "LUKATO_DB__ECHO",
)


def isolar_banco() -> str:
    """Aponta a configuracao para um SQLite novo e vazio. Devolve o diretorio.

    Sobrescreve as variaveis em ``os.environ`` porque elas vencem o ``.env`` no
    pydantic-settings. Chame ANTES de ``reset_settings_cache()``.
    """
    for nome in _VARIAVEIS_DE_BANCO:
        os.environ.pop(nome, None)

    diretorio = tempfile.mkdtemp(prefix="lukato-prova-")
    os.environ["LUKATO_DB__URL"] = f"sqlite+aiosqlite:///{diretorio}/prova.db"
    os.environ["LUKATO_DB__AUTO_FALLBACK"] = "false"
    os.environ["LUKATO_DB__CREATE_ALL"] = "true"
    os.environ["LUKATO_DB__RUN_MIGRATIONS"] = "false"
    return diretorio


def limpar_banco(diretorio: str) -> None:
    """Remove o diretorio temporario. Falha em silencio: e so lixo temporario."""
    shutil.rmtree(diretorio, ignore_errors=True)
