"""Testes de unidade dos runtimes de agente (SPEC-0004, criterios de aceite 1 a 4).

Todos os tres runtimes recebem o `LLMPort` por injecao, entao a suite inteira roda
com `EchoLLM` — sem rede, sem cliente LangChain, sem chave de API. O eco tem um
recurso que faz este arquivo funcionar: a marca `[[JSON]]` na entrada devolve
**exatamente** o texto que vem depois dela, o que permite escrever uma chamada de
ferramenta em JSON e ver o grafo executa-la de verdade.

Os quatro criterios de aceite da SPEC-0004 estao cobertos aqui:

1. `direct` executa offline e produz um `RunStep` de tipo `LLM`;
2. `langgraph` com `max_iterations=1` nao entra em laco;
3. `deepagent` sem credencial degrada para `langgraph`;
4. `calculator` recusa `__import__("os")` sem executar nada.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lukato.adapters.llm.echo import ECHO_PREFIX, EchoLLM
from lukato.adapters.orchestrator.deep_agent_harness import (
    UNAVAILABLE_REASONS,
    DeepAgentOrchestrator,
)
from lukato.adapters.orchestrator.direct import DirectOrchestrator
from lukato.adapters.orchestrator.factory import (
    DEFAULT_RUNTIME,
    KNOWN_RUNTIMES,
    build_orchestrators,
    resolve,
)
from lukato.adapters.orchestrator.langgraph_runtime import (
    EXHAUSTED_STEP_NAME,
    LangGraphOrchestrator,
    parse_tool_call,
)
from lukato.adapters.orchestrator.tools import (
    MAX_EXPONENT,
    ToolContext,
    ToolRegistry,
    build_tool_registry,
    safe_arithmetic,
)
from lukato.config.settings import Settings
from lukato.domain.errors import ProviderError, UnsupportedCapability, ValidationError
from lukato.domain.models.module import ModuleDefinition
from lukato.domain.models.run import RunStatus, StepKind
from lukato.domain.ports.llm import ChatMessage
from lukato.domain.ports.orchestrator import OrchestratorRequest
from tests.factories import make_binding, make_module
from tests.fakes import CountingLLM, SlowLLM

pytestmark = pytest.mark.unit


def _settings(**llm: Any) -> Settings:
    """`Settings` de teste sem `.env`, com o grupo de LLM do chamador."""
    base = {"provider": "echo", "model": "modelo-de-teste", "api_key": None}
    return Settings(_env_file=None, llm={**base, **llm}, embedding={"provider": "hashing"})


def _pedido(
    modulo: ModuleDefinition,
    entrada: str = "qual e o plano?",
    *,
    system_prompt: str = "Voce e o assistente do lukato.",
) -> OrchestratorRequest:
    """Monta um `OrchestratorRequest` completo para o modulo informado."""
    return OrchestratorRequest(
        module=modulo,
        input_text=entrada,
        system_prompt=system_prompt,
        metadata={"run_id": "run-de-teste", "tenant_id": "claro"},
    )


def _chamada_de_ferramenta(nome: str, **argumentos: Any) -> str:
    """Entrada que faz o eco devolver literalmente uma chamada de ferramenta em JSON."""
    return "[[JSON]] " + json.dumps({"tool": nome, "args": argumentos})


@pytest.fixture
def llm() -> EchoLLM:
    """Provedor deterministico offline compartilhado pelos runtimes."""
    return EchoLLM()


@pytest.fixture
def grafo(llm: EchoLLM) -> LangGraphOrchestrator:
    """`LangGraphOrchestrator` com o catalogo normativo de ferramentas."""
    return LangGraphOrchestrator(
        llm, tools=build_tool_registry(), tool_context=ToolContext(), settings=_settings()
    )


# --------------------------------------------------------------------------- #
# DirectOrchestrator
# --------------------------------------------------------------------------- #
async def test_direct_executa_offline_com_o_eco_e_produz_um_unico_step_llm(
    llm: EchoLLM,
) -> None:
    modulo = make_module("modulo-direto", runtime="direct")

    resultado = await DirectOrchestrator(llm, settings=_settings()).run(_pedido(modulo))

    assert resultado.output_text == f"{ECHO_PREFIX}qual e o plano?"
    assert [passo.kind for passo in resultado.steps] == [StepKind.LLM]
    assert resultado.steps[0].name == "direct.llm"
    assert resultado.steps[0].run_id == "run-de-teste"
    assert resultado.metadata["runtime"] == "direct"


async def test_direct_monta_system_prompt_historico_e_pergunta_nessa_ordem() -> None:
    espiao = CountingLLM()
    modulo = make_module("modulo-direto", runtime="direct")
    pedido = _pedido(modulo)
    pedido.history.append(ChatMessage.assistant("resposta anterior"))

    await DirectOrchestrator(espiao, settings=_settings()).run(pedido)

    assert [mensagem.role for mensagem in espiao.last_messages] == [
        "system",
        "assistant",
        "user",
    ]
    assert espiao.last_system_prompt == "Voce e o assistente do lukato."
    assert espiao.last_user_text == "qual e o plano?"


async def test_direct_respeita_o_binding_do_modulo() -> None:
    espiao = CountingLLM()
    modulo = make_module(
        "modulo-direto",
        runtime="direct",
        binding=make_binding(model="modelo-do-binding", temperature=0.0, max_tokens=64),
    )

    await DirectOrchestrator(espiao, settings=_settings()).run(_pedido(modulo))

    enviado = espiao.kwargs[-1]
    assert enviado["model"] == "modelo-do-binding"
    assert enviado["temperature"] == 0.0
    assert enviado["max_tokens"] == 64


async def test_direct_traduz_estouro_de_prazo_do_provedor_em_provider_error() -> None:
    modulo = make_module("modulo-direto", runtime="direct")

    with pytest.raises(ProviderError):
        await DirectOrchestrator(SlowLLM(raises_timeout=True)).run(_pedido(modulo))


def test_direct_so_suporta_o_runtime_direct(llm: EchoLLM) -> None:
    orquestrador = DirectOrchestrator(llm)

    assert orquestrador.supports("direct") is True
    assert orquestrador.supports("DIRECT") is True
    assert orquestrador.supports("langgraph") is False


# --------------------------------------------------------------------------- #
# LangGraphOrchestrator
# --------------------------------------------------------------------------- #
async def test_langgraph_com_max_iterations_1_nao_entra_em_laco_e_produz_steps(
    grafo: LangGraphOrchestrator,
) -> None:
    modulo = make_module("modulo-grafo", runtime="langgraph", config={"max_iterations": 1})

    resultado = await grafo.run(_pedido(modulo))

    assert resultado.metadata["iterations"] == 1
    assert resultado.metadata["max_iterations"] == 1
    assert resultado.metadata["exhausted"] is False
    assert [passo.kind for passo in resultado.steps] == [
        StepKind.PROMPT,
        StepKind.LLM,
        StepKind.REFLECT,
    ]
    assert resultado.output_text == f"{ECHO_PREFIX}qual e o plano?"


async def test_langgraph_executa_a_ferramenta_pedida_e_grava_um_step_tool(
    grafo: LangGraphOrchestrator,
) -> None:
    modulo = make_module(
        "modulo-ferramenta",
        runtime="langgraph",
        binding=make_binding(tools=["calculator"]),
        config={"max_iterations": 2, "planning": False},
    )

    resultado = await grafo.run(
        _pedido(modulo, _chamada_de_ferramenta("calculator", expression="2 + 3 * 4"))
    )

    ferramentas = [passo for passo in resultado.steps if passo.kind is StepKind.TOOL]
    assert len(ferramentas) == 1, "a chamada em JSON tinha de virar uma execucao de ferramenta"
    assert ferramentas[0].name == "tool:calculator"
    assert ferramentas[0].output["result"]["result"] == 14
    assert resultado.metadata["observations"] == 1


async def test_langgraph_erro_de_ferramenta_vira_step_error_sem_abortar_a_execucao(
    grafo: LangGraphOrchestrator,
) -> None:
    modulo = make_module(
        "modulo-ferramenta-ruim",
        runtime="langgraph",
        binding=make_binding(tools=["calculator"]),
        config={"max_iterations": 1, "planning": False},
    )

    resultado = await grafo.run(
        _pedido(modulo, _chamada_de_ferramenta("calculator", expression="1 / 0"))
    )

    erros = [passo for passo in resultado.steps if passo.kind is StepKind.ERROR]
    assert len(erros) == 1, "a falha da ferramenta tinha de virar um step ERROR"
    assert erros[0].status is RunStatus.FAILED
    assert "Divisao por zero" in (erros[0].error or "")
    assert resultado.output_text, "a execucao segue e devolve o melhor resultado parcial"
    assert any(passo.name == EXHAUSTED_STEP_NAME for passo in resultado.steps)


async def test_langgraph_recusa_ferramenta_inexistente_antes_de_executar(
    grafo: LangGraphOrchestrator,
) -> None:
    modulo = make_module(
        "modulo-sem-ferramenta",
        runtime="langgraph",
        binding=make_binding(tools=["ferramenta-que-nao-existe"]),
    )

    with pytest.raises(ValidationError) as capturado:
        await grafo.run(_pedido(modulo))

    assert capturado.value.details["tool"] == "ferramenta-que-nao-existe"


async def test_langgraph_com_planejamento_ligado_registra_um_step_plan(
    grafo: LangGraphOrchestrator,
) -> None:
    modulo = make_module(
        "modulo-com-plano",
        runtime="langgraph",
        config={"max_iterations": 1, "planning": True},
    )

    resultado = await grafo.run(_pedido(modulo))

    assert [passo.kind for passo in resultado.steps][:3] == [
        StepKind.PROMPT,
        StepKind.PLAN,
        StepKind.LLM,
    ]
    assert resultado.metadata["plan"], "o plano gerado precisa viajar nos metadados"


@pytest.mark.parametrize(
    ("conteudo", "esperado"),
    [
        ('{"tool": "now", "args": {}}', {"tool": "now", "args": {}}),
        ('```json\n{"tool": "now"}\n```', {"tool": "now", "args": {}}),
        ("resposta final em texto", None),
        ("{sem json valido", None),
        ('{"resposta": "sem campo tool"}', None),
    ],
)
def test_parse_tool_call_le_o_json_do_modelo_com_tolerancia(
    conteudo: str, esperado: dict[str, Any] | None
) -> None:
    assert parse_tool_call(conteudo) == esperado


# --------------------------------------------------------------------------- #
# calculator (SPEC-0004 criterio 4)
# --------------------------------------------------------------------------- #
def test_calculator_recusa_import_sem_executar_nada() -> None:
    with pytest.raises(ValidationError) as capturado:
        safe_arithmetic("__import__('os')")

    assert capturado.value.details["rejected_node"] == "Call", (
        "a chamada e recusada na inspecao da AST, antes de qualquer avaliacao"
    )


def test_calculator_recusa_bomba_de_expoente() -> None:
    with pytest.raises(ValidationError) as capturado:
        safe_arithmetic("9**9**9")

    assert capturado.value.details["tool"] == "calculator"
    assert MAX_EXPONENT == 64.0


def test_calculator_recusa_nome_nao_permitido() -> None:
    with pytest.raises(ValidationError) as capturado:
        safe_arithmetic("os + 1")

    assert capturado.value.details["rejected_node"] == "Name"


@pytest.mark.parametrize(
    ("expressao", "resultado"),
    [("2 + 3 * 4", 14), ("(2 + 3) * 4.5", 22.5), ("-7 // 2", -4), ("10 % 3", 1), ("2 ** 10", 1024)],
)
def test_calculator_avalia_aritmetica_permitida(expressao: str, resultado: float) -> None:
    assert safe_arithmetic(expressao) == resultado


async def test_registro_de_ferramentas_publica_as_cinco_da_spec() -> None:
    registro = build_tool_registry()

    assert registro.names() == [
        "calculator",
        "commercial_lookup",
        "cost_lookup",
        "knowledge_search",
        "now",
    ]


async def test_ferramenta_sem_dependencia_devolve_erro_de_capacidade_em_vez_de_levantar() -> None:
    registro = build_tool_registry()

    resultado = await registro.execute("knowledge_search", {"query": "fatura"}, ToolContext())

    assert resultado == {"error": "capacidade indisponivel"}


async def test_ferramenta_desconhecida_no_registro_levanta_validation_error() -> None:
    with pytest.raises(ValidationError):
        ToolRegistry().get("inexistente")


# --------------------------------------------------------------------------- #
# DeepAgentOrchestrator e escolha de runtime
# --------------------------------------------------------------------------- #
def test_deepagent_fica_indisponivel_sem_chave_de_api(llm: EchoLLM) -> None:
    harness = DeepAgentOrchestrator(llm, settings=_settings(api_key=None))

    assert harness.available is False
    assert harness.supports("deepagent") is False
    assert harness.unavailable_reason in {
        UNAVAILABLE_REASONS["missing_api_key"],
        UNAVAILABLE_REASONS["missing_libraries"],
    }


async def test_deepagent_indisponivel_recusa_executar_com_provider_error(llm: EchoLLM) -> None:
    harness = DeepAgentOrchestrator(llm, settings=_settings(api_key=None))
    modulo = make_module("modulo-harness", runtime="deepagent")

    with pytest.raises(ProviderError):
        await harness.run(_pedido(modulo))


def test_resolve_degrada_deepagent_para_langgraph_sem_chave(llm: EchoLLM) -> None:
    settings = _settings(api_key=None)
    orquestradores = build_orchestrators(llm, settings=settings)

    escolhido = resolve(orquestradores, "deepagent")

    assert "deepagent" not in orquestradores, "sem credencial o harness nem entra no mapa"
    assert escolhido.name == DEFAULT_RUNTIME
    assert escolhido.supports(DEFAULT_RUNTIME) is True


def test_resolve_devolve_o_runtime_pedido_quando_ele_esta_disponivel(llm: EchoLLM) -> None:
    orquestradores = build_orchestrators(llm, settings=_settings())

    assert resolve(orquestradores, "direct").name == "direct"
    assert resolve(orquestradores, "langgraph").name == "langgraph"
    assert resolve(orquestradores, "  DIRECT  ").name == "direct"


def test_resolve_sem_runtime_informado_cai_no_padrao(llm: EchoLLM) -> None:
    orquestradores = build_orchestrators(llm, settings=_settings())

    assert resolve(orquestradores, "").name == DEFAULT_RUNTIME


def test_resolve_com_runtime_desconhecido_levanta_unsupported_capability(llm: EchoLLM) -> None:
    orquestradores = build_orchestrators(llm, settings=_settings())

    with pytest.raises(UnsupportedCapability) as capturado:
        resolve(orquestradores, "runtime-inventado")

    assert capturado.value.http_status == 501
    assert capturado.value.details["runtime"] == "runtime-inventado"
    assert "runtime-inventado" not in KNOWN_RUNTIMES
