"""`resolve_engine` tem que distinguir os TRES estados do banco, nao dois.

O defeito que este arquivo trava apareceu subindo a stack no Docker: o log de
boot registrou, em linhas consecutivas,

    database_unreachable_without_fallback  auto_fallback=False  url=...@postgres:5432
    port_adapter_selected  degraded=False  reason='ping respondeu na URL configurada'

A segunda linha e falsa. `resolve_engine` devolvia `(engine, url)` tanto no
sucesso quanto no caso "inalcancavel e sem fallback", e quem chamava deduzia o
resultado comparando a URL efetiva com a configurada — iguais nos dois casos.
O pior dos tres estados era o unico que aparecia como saudavel.
"""

from __future__ import annotations

import pytest

from lukato.adapters.persistence.session import EngineResolution, resolve_engine
from lukato.config import Settings

pytestmark = pytest.mark.anyio

URL_MORTA = "postgresql+asyncpg://ninguem:ninguem@127.0.0.1:1/inexistente"


def _settings(**db: object) -> Settings:
    """Settings com o grupo `db` montado a partir dos campos informados."""
    return Settings.model_validate({"db": db})


async def test_banco_responde_diz_que_respondeu(tmp_path: object) -> None:
    """Caminho feliz: alcancavel, sem fallback."""
    url = f"sqlite+aiosqlite:///{tmp_path}/ok.db"
    resolucao = await resolve_engine(_settings(url=url, auto_fallback=False))

    assert isinstance(resolucao, EngineResolution)
    assert resolucao.reachable is True
    assert resolucao.fell_back is False
    assert resolucao.url == url
    await resolucao.engine.dispose()


async def test_banco_fora_sem_fallback_nao_pode_parecer_sucesso(tmp_path: object) -> None:
    """O estado que a versao antiga reportava como saudavel.

    Com `auto_fallback=false` e o banco fora, a URL efetiva continua sendo a
    configurada e `fell_back` e False — mas `reachable` tem que ser False, senao
    quem le nao consegue separar isto de um banco que respondeu.
    """
    resolucao = await resolve_engine(
        _settings(
            url=URL_MORTA, fallback_url=f"sqlite+aiosqlite:///{tmp_path}/f.db", auto_fallback=False
        )
    )

    assert resolucao.reachable is False, (
        "banco inalcancavel reportado como alcancavel: e exatamente a linha de log "
        "'ping respondeu na URL configurada' que este teste existe para impedir"
    )
    assert resolucao.fell_back is False
    assert resolucao.url == URL_MORTA
    await resolucao.engine.dispose()


async def test_fallback_assume_e_o_novo_banco_e_sondado(tmp_path: object) -> None:
    """Caiu para o fallback: `fell_back` marcado e o destino tambem foi sondado."""
    destino = f"sqlite+aiosqlite:///{tmp_path}/fallback.db"
    resolucao = await resolve_engine(
        _settings(url=URL_MORTA, fallback_url=destino, auto_fallback=True)
    )

    assert resolucao.fell_back is True
    assert resolucao.url == destino
    assert resolucao.reachable is True, (
        "o fallback foi ativado mas ninguem sondou o destino; um fallback que "
        "tambem esta fora precisa aparecer como inalcancavel"
    )
    await resolucao.engine.dispose()


async def test_os_tres_estados_sao_distinguiveis_entre_si() -> None:
    """Nenhum par de estados colide no par (reachable, fell_back)."""
    estados = {
        "responde": (True, False),
        "fora sem fallback": (False, False),
        "caiu para o fallback": (True, True),
    }
    assert len(set(estados.values())) == len(estados), (
        f"dois estados do banco produzem a mesma leitura: {estados}"
    )
