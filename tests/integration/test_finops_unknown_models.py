"""Lacuna de precificacao no resumo de custo (SPEC-0005 secao 2).

Um modelo fora da tabela de precos e custeado pelo preco default — em geral zero
— e por isso entra no resumo com `0.00`. `CostSummary.unknown_models` existe para
que esse zero nao se confunda com um modelo realmente gratuito.

Estes testes exercitam **o caminho que as rotas usam**: `GetCostSummary` sobre o
resumo agregado em SQL pelo repositorio, e a resposta de `GET
/api/v1/finops/summary`. O `CostCalculator.summarize` tambem calcula a lacuna, mas
nenhuma rota o chama — cobrir so ele daria confianca falsa, que foi exatamente
como o defeito passou.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.application.container import Container
from lukato.application.use_cases.finops import CostFilter, GetCostSummary
from lukato.domain.models.finops import CostSummary
from lukato.domain.models.identity import Principal
from tests.conftest import TEST_MODEL
from tests.factories import make_usage_record

pytestmark = pytest.mark.integration

MODELO_FANTASMA = "modelo-sem-preco"
"""Ausente da tabela de precos e sem `/`: o fallback por prefixo de provedor nao cobre."""


async def _grava(
    uow_factory: UnitOfWorkFactoryImpl,
    *,
    module_slug: str,
    model: str,
    cost_usd: float,
    record_id: str,
) -> None:
    """Persiste um registro de consumo pelo repositorio real."""
    async with uow_factory() as uow:
        await uow.usage.add(
            make_usage_record(
                module_slug=module_slug,
                model=model,
                cost_usd=cost_usd,
                record_id=record_id,
            )
        )
        await uow.commit()


async def _resumo(container: Container, principal: Principal) -> CostSummary:
    """Roda o caso de uso que a rota `GET /finops/summary` executa."""
    return await GetCostSummary(container).execute(CostFilter(), principal)


async def test_resumo_denuncia_o_modelo_sem_preco_cadastrado(
    container: Container,
    uow_factory: UnitOfWorkFactoryImpl,
    principal: Principal,
) -> None:
    """SPEC-0005 secao 2: custo nunca e silenciosamente zerado sem sinal."""
    await _grava(
        uow_factory,
        module_slug="fantasma",
        model=MODELO_FANTASMA,
        cost_usd=0.0,
        record_id="11111111-1111-5111-8111-111111111111",
    )

    resumo = await _resumo(container, principal)

    assert resumo.by_model[MODELO_FANTASMA] == 0.0, "o preco default zerou o custo"
    assert resumo.unknown_models == [MODELO_FANTASMA], (
        "o zero acima e indistinguivel de um modelo gratuito sem esta marcacao"
    )


async def test_resumo_nao_marca_modelo_que_tem_preco_cadastrado(
    container: Container,
    uow_factory: UnitOfWorkFactoryImpl,
    principal: Principal,
) -> None:
    """Modelo precificado nao vira alarme falso no resumo."""
    await _grava(
        uow_factory,
        module_slug="precificado",
        model=TEST_MODEL,
        cost_usd=2.0,
        record_id="22222222-2222-5222-8222-222222222222",
    )

    resumo = await _resumo(container, principal)

    assert resumo.by_model == {TEST_MODEL: pytest.approx(2.0)}
    assert resumo.unknown_models == []


async def test_resumo_separa_o_modelo_sem_preco_do_precificado(
    container: Container,
    uow_factory: UnitOfWorkFactoryImpl,
    principal: Principal,
) -> None:
    """Com os dois no mesmo periodo, so o desconhecido e nomeado."""
    await _grava(
        uow_factory,
        module_slug="precificado",
        model=TEST_MODEL,
        cost_usd=2.0,
        record_id="33333333-3333-5333-8333-333333333333",
    )
    await _grava(
        uow_factory,
        module_slug="fantasma",
        model=MODELO_FANTASMA,
        cost_usd=0.0,
        record_id="44444444-4444-5444-8444-444444444444",
    )

    resumo = await _resumo(container, principal)

    assert set(resumo.by_model) == {TEST_MODEL, MODELO_FANTASMA}
    assert resumo.unknown_models == [MODELO_FANTASMA]


async def test_resumo_filtrado_so_denuncia_o_modelo_do_recorte(
    container: Container,
    uow_factory: UnitOfWorkFactoryImpl,
    principal: Principal,
) -> None:
    """A lacuna acompanha o recorte: modulo de fora nao entra no aviso."""
    await _grava(
        uow_factory,
        module_slug="fantasma",
        model=MODELO_FANTASMA,
        cost_usd=0.0,
        record_id="55555555-5555-5555-8555-555555555555",
    )

    resumo = await GetCostSummary(container).execute(
        CostFilter(module_slug="precificado"), principal
    )

    assert resumo.by_model == {}
    assert resumo.unknown_models == []


async def test_rota_de_resumo_entrega_a_lacuna_na_resposta_http(
    client: AsyncClient, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    """De nada adianta calcular a lacuna se o DTO de saida nao a carrega."""
    await _grava(
        uow_factory,
        module_slug="fantasma",
        model=MODELO_FANTASMA,
        cost_usd=0.0,
        record_id="66666666-6666-5666-8666-666666666666",
    )

    resposta = await client.get("/api/v1/finops/summary")

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["by_model"][MODELO_FANTASMA] == 0.0
    assert corpo["unknown_models"] == [MODELO_FANTASMA]
