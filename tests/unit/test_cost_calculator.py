"""Testes de unidade do calculo de custo e de orcamento (SPEC-0005 secao 6).

Os quatro criterios de aceite da SPEC-0005 estao cobertos aqui, exceto o que
depende do HTTP 402 (esse vive nos testes de integracao do `InvokeModule`):

1. `TokenUsage(1000, 500)` com preco `(0.002, 0.006)` custa exatamente `0.005`;
2. orcamento com `hard_stop=true` estourado bloqueia;
3. orcamento em 80% marca `alert=true` sem bloquear;
4. o resumo agrega corretamente por modulo e por modelo.

Todos os precos usados sao redondos, para a conta poder ser conferida a mao.
"""

from __future__ import annotations

import pytest

from lukato.domain.models.finops import Budget, ModelPrice, UsageRecord
from lukato.domain.models.run import TokenUsage
from lukato.domain.services.cost_calculator import BudgetCheck, CostCalculator
from tests.factories import id_de, make_budget, make_usage_record

pytestmark = pytest.mark.unit

PRECO_SPEC = ModelPrice(model="qwen-latest", input_usd_per_1k=0.002, output_usd_per_1k=0.006)
"""Preco do exemplo normativo da SPEC-0005 secao 6.1."""


def _calculadora(
    *precos: ModelPrice, entrada_default: float = 0.0, saida_default: float = 0.0
) -> CostCalculator:
    """Calculadora com a tabela informada e os defaults de modelo desconhecido."""
    return CostCalculator(
        {preco.model: preco for preco in precos},
        default_input=entrada_default,
        default_output=saida_default,
    )


# --------------------------------------------------------------------------- #
# Criterio 1: a conta exata da SPEC-0005
# --------------------------------------------------------------------------- #
def test_conta_normativa_da_spec_1000_e_500_tokens_custa_0_005() -> None:
    """(1000/1000)*0.002 + (500/1000)*0.006 = 0.002 + 0.003 = 0.005."""
    calculadora = _calculadora(PRECO_SPEC)

    custo = calculadora.cost("qwen-latest", TokenUsage.of(1000, 500))

    assert custo == 0.005


def test_custo_de_consumo_zerado_e_zero() -> None:
    """Execucao que nao gastou token nao gera custo."""
    assert _calculadora(PRECO_SPEC).cost("qwen-latest", TokenUsage()) == 0.0


def test_custo_ignora_o_total_e_usa_prompt_e_completion_separadamente() -> None:
    """Entrada e saida tem precos diferentes: o total agregado nao serve para a conta."""
    calculadora = _calculadora(PRECO_SPEC)

    inflado = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=999_999)

    assert calculadora.cost("qwen-latest", inflado) == 0.005


def test_custo_e_arredondado_em_oito_casas_decimais() -> None:
    """SPEC-0005 secao 2: 8 casas no dominio; a UI e que formata com 5."""
    calculadora = _calculadora(ModelPrice(model="m", input_usd_per_1k=1.0 / 3.0))

    assert calculadora.cost("m", TokenUsage.of(1, 0)) == 0.00033333


def test_custo_trata_token_negativo_como_zero() -> None:
    """Provedor com contabilidade quebrada nao pode gerar credito de custo."""
    calculadora = _calculadora(PRECO_SPEC)

    negativo = TokenUsage(prompt_tokens=-1000, completion_tokens=500, total_tokens=0)

    assert calculadora.cost("qwen-latest", negativo) == 0.003


# --------------------------------------------------------------------------- #
# Resolucao de preco e modelo desconhecido
# --------------------------------------------------------------------------- #
def test_price_for_devolve_o_preco_cadastrado() -> None:
    """Match exato e o caminho normal."""
    assert _calculadora(PRECO_SPEC).price_for("qwen-latest") == PRECO_SPEC


def test_price_for_de_modelo_desconhecido_usa_os_precos_default() -> None:
    """Modelo fora da tabela nao zera o custo silenciosamente."""
    calculadora = _calculadora(PRECO_SPEC, entrada_default=0.001, saida_default=0.003)

    preco = calculadora.price_for("modelo-que-nao-existe")

    assert preco.model == "modelo-que-nao-existe"
    assert (preco.input_usd_per_1k, preco.output_usd_per_1k) == (0.001, 0.003)


def test_custo_de_modelo_desconhecido_usa_os_defaults() -> None:
    """A conta com default e a mesma, so muda de onde vem o preco."""
    calculadora = _calculadora(entrada_default=0.002, saida_default=0.006)

    assert calculadora.cost("modelo-novo-do-hub", TokenUsage.of(1000, 500)) == 0.005


def test_is_known_separa_modelo_precificado_de_modelo_sem_preco() -> None:
    """`is_known` e o sinal que impede a lacuna de precificacao de passar batida."""
    calculadora = _calculadora(PRECO_SPEC)

    assert calculadora.is_known("qwen-latest") is True
    assert calculadora.is_known("modelo-que-nao-existe") is False


def test_price_for_resolve_pelo_prefixo_do_provedor() -> None:
    """`openai/gpt-oss-20b` cai no preco cadastrado para `openai` quando existe."""
    calculadora = _calculadora(ModelPrice(model="openai", input_usd_per_1k=0.01))

    assert calculadora.price_for("openai/gpt-oss-20b").input_usd_per_1k == 0.01
    assert calculadora.is_known("openai/gpt-oss-20b") is True


def test_price_for_ignora_diferenca_de_caixa() -> None:
    """O hub as vezes devolve o nome do modelo com outra caixa."""
    calculadora = _calculadora(PRECO_SPEC)

    assert calculadora.price_for("QWEN-Latest") == PRECO_SPEC


def test_upsert_price_cadastra_e_substitui_o_preco_de_um_modelo() -> None:
    """A tabela e editavel em tempo de execucao (`PUT /finops/prices`)."""
    calculadora = _calculadora()

    calculadora.upsert_price(ModelPrice(model="m", input_usd_per_1k=1.0))
    primeiro = calculadora.cost("m", TokenUsage.of(1000, 0))
    calculadora.upsert_price(ModelPrice(model="m", input_usd_per_1k=2.0))

    assert primeiro == 1.0
    assert calculadora.cost("m", TokenUsage.of(1000, 0)) == 2.0


def test_tabela_de_precos_exposta_e_somente_leitura() -> None:
    """`prices` e um `MappingProxyType`: ninguem edita a tabela pelas costas."""
    calculadora = _calculadora(PRECO_SPEC)

    with pytest.raises(TypeError):
        calculadora.prices["novo"] = PRECO_SPEC  # type: ignore[index]


def test_estimate_usage_conta_quatro_caracteres_por_token() -> None:
    """SPEC-0005 secao 3: sem tokens reportados, estima por `len(texto)/4`."""
    uso = _calculadora().estimate_usage("12345678", "123")

    assert uso.prompt_tokens == 2
    assert uso.completion_tokens == 1
    assert uso.total_tokens == 3


def test_estimate_usage_de_textos_vazios_e_zero() -> None:
    """Texto vazio nao gera token estimado."""
    assert _calculadora().estimate_usage("", "") == TokenUsage()


# --------------------------------------------------------------------------- #
# Criterio 4: summarize agrega por modulo e por modelo
# --------------------------------------------------------------------------- #
def _registros() -> list[UsageRecord]:
    """Tres registros de dois modulos e dois modelos, com custos redondos."""
    return [
        make_usage_record(
            module_slug="assistente",
            model="qwen-latest",
            usage=TokenUsage.of(1000, 500),
            cost_usd=0.005,
            run_id=id_de("execucao", "a"),
            record_id=id_de("consumo", 1),
        ),
        make_usage_record(
            module_slug="assistente",
            model="openai/gpt-oss-20b",
            usage=TokenUsage.of(200, 100),
            cost_usd=0.002,
            run_id=id_de("execucao", "a"),
            record_id=id_de("consumo", 2),
        ),
        make_usage_record(
            module_slug="adwatch",
            model="qwen-latest",
            usage=TokenUsage.of(400, 200),
            cost_usd=0.003,
            run_id=id_de("execucao", "b"),
            record_id=id_de("consumo", 3),
        ),
    ]


def test_summarize_soma_o_custo_total() -> None:
    """0.005 + 0.002 + 0.003 = 0.010."""
    assert _calculadora(PRECO_SPEC).summarize(_registros()).total_usd == 0.01


def test_summarize_soma_o_total_de_tokens() -> None:
    """1500 + 300 + 600 = 2400."""
    assert _calculadora(PRECO_SPEC).summarize(_registros()).total_tokens == 2400


def test_summarize_agrega_o_custo_por_modulo() -> None:
    """O `assistente` gastou 0.007 em duas chamadas; o `adwatch`, 0.003."""
    resumo = _calculadora(PRECO_SPEC).summarize(_registros())

    assert resumo.by_module == {"assistente": 0.007, "adwatch": 0.003}


def test_summarize_agrega_o_custo_por_modelo() -> None:
    """O `qwen-latest` aparece em dois modulos e soma 0.008."""
    resumo = _calculadora(PRECO_SPEC).summarize(_registros())

    assert resumo.by_model == {"qwen-latest": 0.008, "openai/gpt-oss-20b": 0.002}


def test_summarize_conta_execucoes_distintas_e_nao_registros() -> None:
    """Dois registros do mesmo `run_id` sao uma execucao so."""
    assert _calculadora(PRECO_SPEC).summarize(_registros()).runs == 2


def test_summarize_conta_registro_sem_run_id_como_execucao_propria() -> None:
    """Sem `run_id` nao ha como agrupar: cada registro vira a sua propria execucao."""
    orfaos = [
        make_usage_record(run_id=None, cost_usd=0.01, record_id=id_de("orfao", 1)),
        make_usage_record(run_id=None, cost_usd=0.02, record_id=id_de("orfao", 2)),
    ]

    assert _calculadora().summarize(orfaos).runs == 2


def test_summarize_de_lista_vazia_devolve_resumo_zerado() -> None:
    """Periodo sem consumo produz um resumo valido, nao `None`."""
    resumo = _calculadora().summarize([])

    assert (resumo.total_usd, resumo.total_tokens, resumo.runs) == (0.0, 0, 0)
    assert resumo.by_module == {}
    assert resumo.by_model == {}


def test_summarize_arredonda_os_agregados_em_oito_casas() -> None:
    """A soma de fracoes binarias nao pode vazar ruido de ponto flutuante."""
    registros = [
        make_usage_record(cost_usd=0.1, record_id=id_de("ruido", 1), run_id=id_de("r", 1)),
        make_usage_record(cost_usd=0.2, record_id=id_de("ruido", 2), run_id=id_de("r", 2)),
    ]

    resumo = _calculadora().summarize(registros)

    assert resumo.total_usd == 0.3
    assert resumo.by_module == {"modulo-teste": 0.3}


def test_unknown_models_reporta_o_modelo_sem_preco_cadastrado() -> None:
    """A lacuna de precificacao nunca fica silenciosa (SPEC-0005 secao 2)."""
    registros = [
        make_usage_record(model="qwen-latest", record_id=id_de("c", 1)),
        make_usage_record(model="modelo-fantasma", record_id=id_de("c", 2)),
    ]

    assert _calculadora(PRECO_SPEC).unknown_models(registros) == frozenset({"modelo-fantasma"})


def test_unknown_models_e_vazio_quando_tudo_esta_precificado() -> None:
    """Sem lacuna, o conjunto e vazio."""
    registros = [make_usage_record(model="qwen-latest", record_id=id_de("c", 1))]

    assert _calculadora(PRECO_SPEC).unknown_models(registros) == frozenset()


def test_summarize_reporta_modelos_desconhecidos_no_proprio_resumo() -> None:
    """SPEC-0005 secao 2: o modelo sem preco e reportado em `CostSummary.unknown_models`."""
    registros = [
        make_usage_record(model="qwen-latest", record_id=id_de("c", 1)),
        make_usage_record(model="modelo-fantasma", record_id=id_de("c", 2)),
    ]

    resumo = _calculadora(PRECO_SPEC).summarize(registros)

    assert resumo.unknown_models == ["modelo-fantasma"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Criterios 2 e 3: check_budget nas tres faixas
# --------------------------------------------------------------------------- #
def test_check_budget_dentro_do_limite_esta_ok_sem_alerta_nem_bloqueio() -> None:
    """Faixa verde: 50% de um orcamento de US$ 10."""
    verificacao = _calculadora().check_budget(make_budget(limit_usd=10.0), 5.0)

    assert verificacao.ok is True
    assert verificacao.alert is False
    assert verificacao.blocked is False
    assert verificacao.ratio == 0.5
    assert verificacao.remaining == 5.0


def test_check_budget_em_oitenta_por_cento_alerta_sem_bloquear() -> None:
    """SPEC-0005 criterio 3: 80% marca `alert=true` e nao bloqueia."""
    orcamento = make_budget(limit_usd=10.0, alert_threshold=0.8, hard_stop=True)

    verificacao = _calculadora().check_budget(orcamento, 8.0)

    assert verificacao.alert is True
    assert verificacao.blocked is False
    assert verificacao.ok is True
    assert verificacao.ratio == 0.8


def test_check_budget_logo_abaixo_do_limiar_nao_alerta() -> None:
    """79.9% ainda e faixa verde: o alerta e `>=`, nao `>`."""
    orcamento = make_budget(limit_usd=10.0, alert_threshold=0.8)

    assert _calculadora().check_budget(orcamento, 7.99).alert is False


def test_check_budget_estourado_com_hard_stop_bloqueia() -> None:
    """SPEC-0005 criterio 2: `hard_stop=true` estourado bloqueia a proxima invocacao."""
    orcamento = make_budget(limit_usd=10.0, hard_stop=True)

    verificacao = _calculadora().check_budget(orcamento, 10.5)

    assert verificacao.blocked is True
    assert verificacao.ok is False
    assert verificacao.alert is True
    assert verificacao.remaining == 0.0


def test_check_budget_exatamente_no_limite_com_hard_stop_bloqueia() -> None:
    """O bloqueio e em `ratio >= 1.0`: gastar exatamente o limite ja esgota."""
    orcamento = make_budget(limit_usd=10.0, hard_stop=True)

    assert _calculadora().check_budget(orcamento, 10.0).blocked is True


def test_check_budget_estourado_sem_hard_stop_nao_bloqueia() -> None:
    """Sem `hard_stop` o orcamento e um alerta contabil, nao um portao."""
    orcamento = make_budget(limit_usd=10.0, hard_stop=False)

    verificacao = _calculadora().check_budget(orcamento, 25.0)

    assert verificacao.blocked is False, "hard_stop=False nunca pode bloquear a execucao"
    assert verificacao.ok is False
    assert verificacao.alert is True
    assert verificacao.ratio == 2.5


def test_check_budget_de_orcamento_inativo_nunca_alerta_nem_bloqueia() -> None:
    """Orcamento desativado sai do caminho por completo."""
    orcamento = make_budget(limit_usd=10.0, hard_stop=True, is_active=False)

    verificacao = _calculadora().check_budget(orcamento, 100.0)

    assert verificacao.ok is True
    assert verificacao.alert is False
    assert verificacao.blocked is False


def test_check_budget_com_limite_zero_conta_como_esgotado_quando_ha_gasto() -> None:
    """Limite zero e "nao pode gastar": qualquer gasto ja estoura."""
    orcamento = make_budget(limit_usd=0.0, hard_stop=True)

    assert _calculadora().check_budget(orcamento, 0.01).blocked is True


def test_check_budget_com_limite_zero_e_gasto_zero_nao_estoura() -> None:
    """Sem gasto nao ha estouro, mesmo com limite zero."""
    orcamento = make_budget(limit_usd=0.0, hard_stop=True)

    verificacao = _calculadora().check_budget(orcamento, 0.0)

    assert verificacao.ratio == 0.0
    assert verificacao.blocked is False


def test_check_budget_devolve_o_gasto_e_o_limite_para_a_ui() -> None:
    """A barra de status precisa dos tres numeros no mesmo objeto."""
    verificacao = _calculadora().check_budget(make_budget(limit_usd=10.0), 2.5)

    assert isinstance(verificacao, BudgetCheck)
    assert verificacao.spent == 2.5
    assert verificacao.limit_usd == 10.0
    assert verificacao.remaining == 7.5


def test_check_budget_nunca_devolve_saldo_negativo() -> None:
    """`remaining` e o que ainda cabe: estourado, e zero, nao um numero negativo."""
    assert _calculadora().check_budget(make_budget(limit_usd=10.0), 15.0).remaining == 0.0


def test_check_budget_respeita_um_alert_threshold_customizado() -> None:
    """O limiar de alerta e por orcamento, nao uma constante global."""
    orcamento = Budget(name="apertado", limit_usd=100.0, alert_threshold=0.5)

    assert _calculadora().check_budget(orcamento, 49.0).alert is False
    assert _calculadora().check_budget(orcamento, 50.0).alert is True
