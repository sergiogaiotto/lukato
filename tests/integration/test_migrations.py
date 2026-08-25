"""Testes de integracao das migracoes Alembic (SPEC-0011 secao 8).

As migracoes sao exercitadas **de fora**, por subprocesso, exatamente como um operador
as roda: `alembic upgrade head` e `alembic downgrade base` contra um SQLite temporario
apontado por `LUKATO_DB__URL`. Rodar em processo separado e o unico jeito honesto de
provar que `migrations/env.py` resolve a URL a partir de `Settings`, que o modo online
assincrono funciona e que `render_as_batch` da conta do SQLite.

O criterio de aceite mais importante e o ultimo: o esquema produzido pela migracao tem
de coincidir com `Base.metadata` — mesmo conjunto de tabelas e de colunas. Divergencia
aqui significa que o banco de producao (migrado) e o banco de teste (`create_all`) sao
objetos diferentes, e o resto da suite estaria testando um esquema que nunca existe.

Nenhuma rede e nenhum PostgreSQL sao usados: a migracao `0002` (indices HNSW) e um
no-op declarado fora do dialeto `postgresql`.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lukato.adapters.persistence import orm as _orm  # noqa: F401  (registra as tabelas)
from lukato.adapters.persistence.base import Base

pytestmark = pytest.mark.integration

RAIZ = Path(__file__).resolve().parents[2]
"""Raiz do projeto: onde vivem `alembic.ini` e o diretorio `migrations/`."""

VERSOES = RAIZ / "migrations" / "versions"
TABELA_DE_CONTROLE = "alembic_version"
TEMPO_LIMITE_SEGUNDOS = 180


def exige_migracao_inicial() -> None:
    """Pula o teste enquanto nao houver nenhuma revisao em `migrations/versions/`."""
    revisoes = (
        sorted(
            caminho.name
            for caminho in VERSOES.iterdir()
            if caminho.suffix == ".py" and not caminho.name.startswith("__")
        )
        if VERSOES.is_dir()
        else []
    )
    if not revisoes:
        pytest.skip("migracao inicial ainda nao gerada")


def url_do_arquivo(caminho: Path) -> str:
    """URL aiosqlite absoluta para o arquivo informado."""
    return f"sqlite+aiosqlite:///{caminho}"


def roda_alembic(*argumentos: str, url: str) -> subprocess.CompletedProcess[str]:
    """Executa o Alembic em subprocesso, com o ambiente limpo e a URL informada.

    Todas as variaveis `LUKATO_*` herdadas da maquina sao removidas: so
    `LUKATO_DB__URL` chega ao subprocesso, e o resultado nao depende do `.env` de
    quem estiver rodando a suite.
    """
    ambiente = {
        chave: valor for chave, valor in os.environ.items() if not chave.startswith("LUKATO_")
    }
    ambiente["LUKATO_DB__URL"] = url
    ambiente["PYTHONPATH"] = str(RAIZ / "src")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *argumentos],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
        timeout=TEMPO_LIMITE_SEGUNDOS,
        check=False,
    )


def tabelas_do_arquivo(caminho: Path) -> set[str]:
    """Nomes das tabelas de negocio gravadas no arquivo SQLite (sem a de controle)."""
    with sqlite3.connect(caminho) as conexao:
        nomes = {
            linha[0]
            for linha in conexao.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    return nomes - {TABELA_DE_CONTROLE, "sqlite_sequence"}


def colunas_do_arquivo(caminho: Path, tabela: str) -> set[str]:
    """Nomes das colunas da tabela, lidos direto do esquema fisico."""
    with sqlite3.connect(caminho) as conexao:
        return {linha[1] for linha in conexao.execute(f'PRAGMA table_info("{tabela}")')}


@pytest.fixture(scope="module")
def banco_migrado(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Um SQLite temporario com `alembic upgrade head` ja aplicado (uma vez por modulo)."""
    exige_migracao_inicial()
    caminho = tmp_path_factory.mktemp("migracoes") / "head.db"
    resultado = roda_alembic("upgrade", "head", url=url_do_arquivo(caminho))
    if resultado.returncode != 0:
        pytest.fail(f"'alembic upgrade head' falhou:\n{resultado.stdout}\n{resultado.stderr}")
    return caminho


# --------------------------------------------------------------------------- #
# Ida e volta
# --------------------------------------------------------------------------- #
async def test_upgrade_head_cria_o_esquema_em_um_sqlite_temporario(banco_migrado: Path) -> None:
    tabelas = tabelas_do_arquivo(banco_migrado)

    assert tabelas, "a migracao nao criou nenhuma tabela"
    assert "modules" in tabelas
    assert "chunks" in tabelas


async def test_upgrade_head_registra_a_revisao_aplicada(banco_migrado: Path) -> None:
    with sqlite3.connect(banco_migrado) as conexao:
        revisoes = [linha[0] for linha in conexao.execute(f"SELECT * FROM {TABELA_DE_CONTROLE}")]

    assert len(revisoes) == 1, f"esperava exatamente uma revisao corrente, veio {revisoes}"


async def test_downgrade_base_desfaz_todas_as_tabelas_do_esquema(tmp_path: Path) -> None:
    exige_migracao_inicial()
    caminho = tmp_path / "ida-e-volta.db"
    url = url_do_arquivo(caminho)
    subida = roda_alembic("upgrade", "head", url=url)
    assert subida.returncode == 0, f"upgrade falhou:\n{subida.stdout}\n{subida.stderr}"
    assert tabelas_do_arquivo(caminho), "o upgrade precisava ter criado tabelas"

    descida = roda_alembic("downgrade", "base", url=url)

    assert descida.returncode == 0, f"downgrade falhou:\n{descida.stdout}\n{descida.stderr}"
    assert tabelas_do_arquivo(caminho) == set(), (
        "'downgrade base' tem de deixar o banco vazio (so a tabela de controle sobra)"
    )


# --------------------------------------------------------------------------- #
# Migracao versus Base.metadata
# --------------------------------------------------------------------------- #
async def test_migracao_cria_exatamente_as_tabelas_de_base_metadata(banco_migrado: Path) -> None:
    da_migracao = tabelas_do_arquivo(banco_migrado)
    da_metadata = set(Base.metadata.tables)

    assert da_migracao == da_metadata, (
        f"esquema divergente — so na migracao: {sorted(da_migracao - da_metadata)}; "
        f"so em Base.metadata: {sorted(da_metadata - da_migracao)}"
    )


async def test_migracao_cria_exatamente_as_colunas_de_base_metadata(banco_migrado: Path) -> None:
    divergencias: dict[str, dict[str, list[str]]] = {}
    for nome, tabela in sorted(Base.metadata.tables.items()):
        da_migracao = colunas_do_arquivo(banco_migrado, nome)
        da_metadata = {coluna.name for coluna in tabela.columns}
        if da_migracao != da_metadata:
            divergencias[nome] = {
                "so_na_migracao": sorted(da_migracao - da_metadata),
                "so_na_metadata": sorted(da_metadata - da_migracao),
            }

    assert divergencias == {}, f"colunas divergentes entre migracao e ORM: {divergencias}"
