"""Testes de unidade da observabilidade (SPEC-0008 secoes 3, 4 e criterio de aceite 4).

O ponto central da SPEC-0008 e um so: **falha de telemetria nunca derruba negocio**.
Este arquivo prova isso nos dois adaptadores.

* `NoopTracer` — os tres context managers entregam um `SpanHandle` inerte, nenhum
  metodo levanta e nada muda no mundo. E o padrao quando o Langfuse esta desligado.
* `LangfuseTracer` com credenciais falsas apontando para um host inalcancavel —
  abrir trace, span e generation, atualizar, pontuar, descarregar o buffer e fechar
  **nao levanta em nenhuma operacao**. O host `127.0.0.1:9` recusa a conexao na hora,
  e os tempos de espera sao encurtados no construtor: o teste nao espera relogio.

As metricas ficam em um `CollectorRegistry` proprio por instancia, entao cada teste
monta o seu e o registro global do `prometheus_client` nunca e tocado.
"""

from __future__ import annotations

import logging

import pytest

from lukato.adapters.observability.factory import NOOP_REASONS, build_tracer
from lukato.adapters.observability.langfuse_tracer import LangfuseTracer
from lukato.adapters.observability.metrics import (
    METRIC_NAMES,
    UNKNOWN_LABEL,
    Metrics,
    get_metrics,
    normalize_path,
    reset_metrics,
)
from lukato.adapters.observability.noop_tracer import NOOP_SPAN, NoopSpan, NoopTracer
from lukato.config.settings import Settings
from lukato.domain.models.run import TokenUsage
from lukato.domain.ports.observability import TracerPort

pytestmark = pytest.mark.unit

HOST_INALCANCAVEL = "http://127.0.0.1:9"
"""Porta 9 (discard): recusa a conexao imediatamente, sem DNS e sem espera."""

CHAVE_FALSA_PUBLICA = "pk-lf-00000000-0000-0000-0000-000000000000"
CHAVE_FALSA_SECRETA = "sk-lf-00000000-0000-0000-0000-000000000000"

_OTEL_EXPORTER_LOGGER = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
"""Logger do exportador OTel: e ele que reclama do host inalcancavel deste arquivo."""


@pytest.fixture(autouse=True)
def _silenciar_exportador_otel() -> None:
    """Cala o exportador OTel enquanto este arquivo roda.

    O host de teste recusa conexao de proposito, entao o exportador em background
    reclama em nivel de erro depois que o teste ja passou. E ruido esperado — e
    nao pode poluir a saida da suite. Nenhum teste deste arquivo le esse logger.
    """
    logging.getLogger(_OTEL_EXPORTER_LOGGER).setLevel(logging.CRITICAL)


def _settings(**observability: object) -> Settings:
    """`Settings` de teste sem `.env`, com o grupo de observabilidade do chamador."""
    base = {"langfuse_enabled": False, "langfuse_host": HOST_INALCANCAVEL}
    return Settings(
        _env_file=None,
        llm={"provider": "echo"},
        embedding={"provider": "hashing"},
        observability={**base, **observability},
    )


# --------------------------------------------------------------------------- #
# NoopTracer
# --------------------------------------------------------------------------- #
async def test_noop_tracer_abre_os_tres_context_managers_sem_efeito_e_sem_levantar() -> None:
    tracer = NoopTracer()

    async with tracer.trace("module.invoke:assistente", input={"texto": "ola"}) as raiz:
        raiz.update(output="ok", metadata={"passo": 1})
        async with tracer.span("guardrail.input", kind="guardrail") as filho:
            filho.update(findings=0)
            filho.end()
        async with tracer.generation("llm.chat", model="echo") as geracao:
            geracao.update(usage=TokenUsage.of(3, 4), cost=0.0)
        raiz.end(output="ok")

    assert isinstance(raiz, NoopSpan)
    assert raiz.id is None
    assert raiz.trace_id is None


async def test_noop_tracer_score_e_flush_nao_fazem_nada_e_nao_levantam() -> None:
    tracer = NoopTracer()

    assert await tracer.score(name="guardrail_blocked", value=1.0) is None
    assert await tracer.flush() is None
    assert tracer.current_trace_id() is None


def test_noop_tracer_satisfaz_a_porta_e_se_declara_desligado() -> None:
    tracer = NoopTracer()

    assert isinstance(tracer, TracerPort)
    assert tracer.enabled is False
    assert repr(tracer) == "NoopTracer()"


def test_noop_span_e_uma_instancia_unica_sem_estado() -> None:
    assert NOOP_SPAN.update(qualquer="coisa") is None
    assert NOOP_SPAN.end() is None
    assert isinstance(NOOP_SPAN, NoopSpan)


def test_factory_devolve_noop_quando_o_langfuse_esta_desligado() -> None:
    tracer = build_tracer(_settings(langfuse_enabled=False))

    assert isinstance(tracer, NoopTracer)
    assert NOOP_REASONS["disabled"].startswith("LUKATO_OBSERVABILITY__LANGFUSE_ENABLED=false")


def test_factory_devolve_noop_quando_faltam_as_credenciais() -> None:
    tracer = build_tracer(
        _settings(langfuse_enabled=True, langfuse_public_key=None, langfuse_secret_key=None)
    )

    assert isinstance(tracer, NoopTracer), (
        "sem as duas chaves nao ha como autenticar; a aplicacao sobe cega, nao quebrada"
    )


# --------------------------------------------------------------------------- #
# LangfuseTracer com credenciais falsas
# --------------------------------------------------------------------------- #
@pytest.fixture
def tracer_falso() -> LangfuseTracer:
    """`LangfuseTracer` apontado para um host que recusa conexao, com esperas curtas."""
    return LangfuseTracer(
        CHAVE_FALSA_PUBLICA,
        CHAVE_FALSA_SECRETA,
        HOST_INALCANCAVEL,
        environment="test",
        release="1.0.0",
        timeout_seconds=1,
        flush_timeout_seconds=0.5,
        health_timeout_seconds=0.5,
    )


async def test_langfuse_com_credenciais_falsas_nao_levanta_em_nenhuma_operacao(
    tracer_falso: LangfuseTracer,
) -> None:
    async with tracer_falso.trace(
        "module.invoke:assistente",
        input={"texto": "ola"},
        metadata={"module_slug": "assistente"},
        user_id="anonymous",
        session_id="sessao-1",
        tags=["teste"],
    ) as raiz:
        raiz.update(output="parcial")
        async with tracer_falso.span("guardrail.input", kind="guardrail") as filho:
            filho.update(metadata={"findings": 0})
            filho.end()
        async with tracer_falso.generation("llm.chat", model="echo") as geracao:
            geracao.update(usage=TokenUsage.of(10, 20), cost=0.0025)
        raiz.end(output="final")

    await tracer_falso.score(name="guardrail_blocked", value=0.0)
    assert tracer_falso.current_trace_id() is None or isinstance(
        tracer_falso.current_trace_id(), str
    )
    assert await tracer_falso.health() is False, "as credenciais falsas nao autenticam"
    await tracer_falso.flush()
    await tracer_falso.aclose()


async def test_langfuse_repassa_a_excecao_do_corpo_sem_engolir_o_erro_de_negocio(
    tracer_falso: LangfuseTracer,
) -> None:
    with pytest.raises(RuntimeError, match="erro de negocio"):
        async with tracer_falso.trace("module.invoke:assistente"):
            raise RuntimeError("erro de negocio")

    await tracer_falso.aclose()


async def test_langfuse_desligado_por_credencial_vazia_vira_noop_silencioso() -> None:
    tracer = LangfuseTracer("", "", HOST_INALCANCAVEL)

    async with tracer.trace("qualquer") as span:
        span.update(output="x")

    assert tracer.enabled is False
    assert await tracer.health() is False
    await tracer.flush()
    await tracer.aclose()


async def test_langfuse_desabilitado_na_configuracao_nao_constroi_cliente() -> None:
    tracer = LangfuseTracer(
        CHAVE_FALSA_PUBLICA, CHAVE_FALSA_SECRETA, HOST_INALCANCAVEL, enabled=False
    )

    assert tracer.enabled is False
    assert tracer.host == HOST_INALCANCAVEL
    async with tracer.span("nada") as span:
        assert span is NOOP_SPAN


# --------------------------------------------------------------------------- #
# Metricas Prometheus
# --------------------------------------------------------------------------- #
@pytest.fixture
def metricas() -> Metrics:
    """`Metrics` em registro proprio: nenhum teste toca o registro global."""
    return Metrics()


def test_metrics_registra_e_renderiza_as_nove_metricas_normativas(metricas: Metrics) -> None:
    metricas.observe_http("get", "/api/v1/modules/{slug}", 200, 0.012)
    metricas.observe_module("assistente", "direct", "succeeded", 0.4)
    metricas.observe_llm("echo", "assistente", TokenUsage.of(10, 20), cost=0.0025)
    metricas.observe_guardrail("input", "pii_redact", "redact", blocked=True, policy="entrada")
    metricas.observe_provider_error("openai_compatible", 502)

    corpo, content_type = metricas.render()
    texto = corpo.decode("utf-8")

    assert "text/plain" in content_type
    for nome in METRIC_NAMES:
        assert nome in texto, f"a metrica {nome} da SPEC-0008 nao apareceu em /metrics"
    assert len(METRIC_NAMES) == 9


def test_metrics_conta_tokens_de_prompt_e_de_completion_separadamente(
    metricas: Metrics,
) -> None:
    metricas.observe_llm("echo", "assistente", TokenUsage.of(10, 20), cost=0.5)

    texto = metricas.render()[0].decode("utf-8")

    assert 'lukato_llm_tokens_total{kind="prompt",model="echo"} 10.0' in texto
    assert 'lukato_llm_tokens_total{kind="completion",model="echo"} 20.0' in texto
    assert 'lukato_llm_cost_usd_total{model="echo",module="assistente"} 0.5' in texto


def test_metrics_registra_bloqueio_apenas_quando_houve_bloqueio(metricas: Metrics) -> None:
    metricas.observe_guardrail("input", "pii_redact", "redact", blocked=False)
    metricas.observe_guardrail("output", "secret_scan", "block", blocked=True, policy="saida")

    texto = metricas.render()[0].decode("utf-8")

    assert 'lukato_guardrail_blocks_total{policy="saida",stage="output"} 1.0' in texto
    assert 'policy="unknown"' not in texto, "o achado sem bloqueio nao pode criar serie de bloqueio"


def test_metrics_aceita_usage_em_dicionario_do_provedor(metricas: Metrics) -> None:
    metricas.observe_llm("qwen", "assistente", {"prompt_tokens": 5, "completion_tokens": 7})

    texto = metricas.render()[0].decode("utf-8")

    assert 'lukato_llm_tokens_total{kind="prompt",model="qwen"} 5.0' in texto


def test_metrics_ignora_incremento_negativo_de_custo(metricas: Metrics) -> None:
    metricas.observe_llm("echo", "assistente", None, cost=-3.0)

    texto = metricas.render()[0].decode("utf-8")

    assert 'lukato_llm_cost_usd_total{model="echo",module="assistente"} 0.0' in texto


def test_metrics_troca_label_vazio_pelo_rotulo_desconhecido(metricas: Metrics) -> None:
    metricas.observe_module("", "", "", 0.1)

    texto = metricas.render()[0].decode("utf-8")

    assert f'module="{UNKNOWN_LABEL}"' in texto


@pytest.mark.parametrize(
    ("caminho", "esperado"),
    [
        ("/api/v1/modules/{slug}", "/api/v1/modules/{slug}"),
        ("/api/v1/runs/2f1c8a0e-1111-4222-8333-444455556666", "/api/v1/runs/{id}"),
        ("/api/v1/modules/42", "/api/v1/modules/{id}"),
        ("/api/v1/keys/deadbeefdeadbeef", "/api/v1/keys/{id}"),
        ("", "/"),
    ],
)
def test_normalize_path_protege_a_cardinalidade_do_label(caminho: str, esperado: str) -> None:
    assert normalize_path(caminho) == esperado


def test_metrics_isola_instancias_em_registros_proprios() -> None:
    primeira = Metrics()
    segunda = Metrics()

    primeira.observe_provider_error("langfuse", "timeout")

    assert 'provider="langfuse"' in primeira.render()[0].decode("utf-8")
    assert 'provider="langfuse"' not in segunda.render()[0].decode("utf-8")


def test_get_metrics_e_memoizado_e_reset_descarta_o_singleton() -> None:
    reset_metrics()
    primeira = get_metrics()
    try:
        assert get_metrics() is primeira, "o singleton do processo tem de ser memoizado"
        reset_metrics()
        assert get_metrics() is not primeira, "`reset_metrics` zera os contadores nos testes"
    finally:
        reset_metrics()
