"""Sondas, metricas e correlacao da borda HTTP (SPEC-0008 secoes 4 e 5).

Tres contratos de operacao sao verificados aqui, e cada um existe por causa de um
incidente conhecido:

* `GET /healthz` responde uma **constante**. Se o liveness consultasse o banco, uma
  queda do PostgreSQL faria o Kubernetes matar replicas que ainda serviam o console;
* `GET /readyz` reporta componente a componente e so devolve `503` quando o **banco**
  cai. Provedor degradado (LLM offline, tracer no-op) mantem `200`, porque a
  plataforma continua util offline (SPEC-0001 secao 6);
* `X-Request-ID` e propagado quando o cliente manda e gerado quando nao manda — e o
  fio que liga a resposta que o cliente viu a linha de log que a explica.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from lukato.adapters.observability.metrics import METRIC_NAMES
from lukato.application.container import Container
from lukato.interfaces.http.middleware import REQUEST_ID_HEADER, RESPONSE_TIME_HEADER

pytestmark = pytest.mark.integration


class _BancoForaDoAr:
    """`UnitOfWorkFactory` que recusa qualquer abertura de transacao."""

    def __call__(self) -> Any:
        raise RuntimeError("conexao recusada pelo banco")


class _LlmMudo:
    """`LLMPort` minimo cuja sonda de saude responde `False` (provedor degradado)."""

    @property
    def default_model(self) -> str:
        return "modelo-mudo"

    async def chat(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - nunca chamado
        raise AssertionError("a sonda de saude nao deve chamar o provedor")

    async def list_models(self) -> list[str]:
        return []

    async def health(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
# /healthz — liveness
# --------------------------------------------------------------------------- #
async def test_healthz_responde_200_com_corpo_constante(client: AsyncClient) -> None:
    """Duas chamadas seguidas devolvem exatamente a mesma resposta."""
    primeira = await client.get("/healthz")
    segunda = await client.get("/healthz")

    assert primeira.status_code == 200
    assert segunda.status_code == 200
    assert primeira.json() == segunda.json(), "o liveness precisa ser uma constante"
    assert primeira.json() == {"status": "alive", "service": "lukato", "version": "1.0.0"}


async def test_healthz_continua_200_com_o_banco_fora_do_ar(
    client: AsyncClient, container: Container
) -> None:
    """Liveness nao toca em dependencia: banco caido nao muda a resposta."""
    container.uow_factory = _BancoForaDoAr()

    resposta = await client.get("/healthz")

    assert resposta.status_code == 200, "o liveness nao pode depender do banco"
    assert resposta.json()["status"] == "alive"


# --------------------------------------------------------------------------- #
# /readyz — readiness
# --------------------------------------------------------------------------- #
async def test_readyz_reporta_os_cinco_componentes_sondados(client: AsyncClient) -> None:
    """O relatorio traz banco, registry, LLM, embeddings e tracer, cada um com status."""
    resposta = await client.get("/readyz")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert set(corpo["components"]) == {
        "database",
        "registry",
        "llm",
        "embeddings",
        "tracer",
    }, f"componentes sondados divergem do esperado: {sorted(corpo['components'])}"
    assert all(
        item["status"] in {"ok", "degraded", "down"} for item in corpo["components"].values()
    )
    assert corpo["version"] == "1.0.0"
    assert corpo["environment"] == "test"


async def test_readyz_reporta_tracer_degradado_sem_credencial_de_langfuse(
    client: AsyncClient,
) -> None:
    """Sem Langfuse a aplicacao sobe e o tracer aparece como `degraded` (criterio 1)."""
    resposta = await client.get("/readyz")

    assert resposta.status_code == 200, "tracer degradado nao pode tirar a replica do ar"
    assert resposta.json()["components"]["tracer"]["status"] == "degraded"


async def test_readyz_com_llm_degradado_continua_200(
    client: AsyncClient, container: Container
) -> None:
    """Provedor de LLM indisponivel e degradacao, nao queda: a plataforma segue util."""
    container.llm = _LlmMudo()

    resposta = await client.get("/readyz")

    assert resposta.status_code == 200, "degradacao de provedor nunca vira 503"
    corpo = resposta.json()
    assert corpo["components"]["llm"]["status"] == "degraded"
    assert corpo["components"]["database"]["status"] == "ok"
    assert corpo["status"] == "degraded"


async def test_readyz_com_banco_fora_do_ar_responde_503(
    client: AsyncClient, container: Container
) -> None:
    """Somente o banco derruba a prontidao."""
    container.uow_factory = _BancoForaDoAr()

    resposta = await client.get("/readyz")

    assert resposta.status_code == 503
    corpo = resposta.json()
    assert corpo["status"] == "down"
    assert corpo["components"]["database"]["status"] == "down"


async def test_health_providers_nao_expoe_segredo_algum(client: AsyncClient) -> None:
    """O quadro de provedores diz apenas **se** ha credencial, nunca qual e."""
    resposta = await client.get("/api/v1/health/providers")

    assert resposta.status_code == 200
    nomes = {item["name"] for item in resposta.json()["providers"]}
    assert {"database", "llm", "embeddings", "tracer", "registry"} <= nomes
    bruto = resposta.text.lower()
    for proibido in ("api_key", "jwt_secret", "secret_key", "password"):
        assert proibido not in bruto, f"o relatorio de provedores vazou '{proibido}'"


# --------------------------------------------------------------------------- #
# /metrics
# --------------------------------------------------------------------------- #
async def test_metrics_expoe_os_nove_nomes_normativos_apos_uma_invocacao(
    client: AsyncClient, seeded: Any
) -> None:
    """Depois de invocar um modulo, `/metrics` publica as nove metricas da secao 4."""
    invocacao = await client.post("/api/v1/modules/assistente/invoke", json={"input": "bom dia"})
    assert invocacao.status_code == 200, invocacao.text

    resposta = await client.get("/metrics")

    assert resposta.status_code == 200
    corpo = resposta.text
    ausentes = [nome for nome in METRIC_NAMES if nome not in corpo]
    assert not ausentes, f"metricas normativas ausentes da exposicao: {ausentes}"


async def test_metrics_usa_o_template_da_rota_e_nunca_o_slug_concreto(
    client: AsyncClient, seeded: Any
) -> None:
    """O label `path` guarda o template versionado; o slug concreto explodiria a serie."""
    await client.get("/api/v1/modules/assistente")

    corpo = (await client.get("/metrics")).text

    assert 'path="/api/v1/modules/{slug}"' in corpo, "o label deveria usar o template da rota"
    assert 'path="/api/v1/modules/assistente"' not in corpo
    assert 'path="/modules/assistente"' not in corpo


async def test_metrics_registra_a_invocacao_do_modulo(client: AsyncClient, seeded: Any) -> None:
    """Uma invocacao alimenta as metricas de negocio, nao apenas as de HTTP."""
    resposta = await client.post("/api/v1/modules/assistente/invoke", json={"input": "oi"})
    assert resposta.status_code == 200, resposta.text

    corpo = (await client.get("/metrics")).text

    assert 'lukato_module_invocations_total{module="assistente"' in corpo
    assert 'lukato_llm_tokens_total{kind="prompt",model="modelo-de-teste"}' in corpo
    assert "lukato_llm_cost_usd_total" in corpo


# --------------------------------------------------------------------------- #
# Correlacao
# --------------------------------------------------------------------------- #
async def test_x_request_id_e_propagado_quando_o_cliente_envia(client: AsyncClient) -> None:
    """Identificador aceitavel vindo do cliente volta intacto na resposta."""
    resposta = await client.get("/healthz", headers={REQUEST_ID_HEADER: "req-teste-0001"})

    assert resposta.headers[REQUEST_ID_HEADER] == "req-teste-0001"


async def test_x_request_id_e_gerado_quando_o_cliente_nao_envia(client: AsyncClient) -> None:
    """Sem cabecalho de entrada, a borda gera um identificador proprio."""
    primeira = await client.get("/healthz")
    segunda = await client.get("/healthz")

    gerado = primeira.headers[REQUEST_ID_HEADER]
    assert gerado, "toda resposta precisa carregar X-Request-ID"
    assert gerado != segunda.headers[REQUEST_ID_HEADER], "cada requisicao tem o seu identificador"


async def test_x_request_id_do_cliente_e_recusado_quando_nao_e_imprimivel(
    client: AsyncClient,
) -> None:
    """Texto arbitrario nao e ecoado: a borda substitui por um identificador proprio."""
    injetado = "abc def\nX-Injetado: 1"

    resposta = await client.get("/healthz", headers={REQUEST_ID_HEADER: injetado.split("\n")[0]})

    assert resposta.headers[REQUEST_ID_HEADER] != "abc def"


async def test_x_request_id_acompanha_o_envelope_de_erro(client: AsyncClient) -> None:
    """Erro tambem sai carimbado — sem isso o log nao se liga a resposta."""
    resposta = await client.get(
        "/api/v1/modules/inexistente", headers={REQUEST_ID_HEADER: "req-erro-0002"}
    )

    assert resposta.status_code == 404
    assert resposta.headers[REQUEST_ID_HEADER] == "req-erro-0002"
    assert resposta.json()["error"]["code"] == "module_not_found"


async def test_toda_resposta_carrega_tempo_e_cabecalhos_de_seguranca(
    client: AsyncClient, app: FastAPI
) -> None:
    """A pilha de middlewares carimba latencia e defesas de navegador."""
    resposta = await client.get("/healthz")

    assert RESPONSE_TIME_HEADER in resposta.headers
    assert float(resposta.headers[RESPONSE_TIME_HEADER]) >= 0.0
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"
    assert resposta.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in resposta.headers, "HSTS so vale em producao"
