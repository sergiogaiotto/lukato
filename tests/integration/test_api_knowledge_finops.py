"""Base de conhecimento (SPEC-0007) e FinOps (SPEC-0005) pela borda HTTP.

Os dois recursos compartilham a mesma disciplina: **nada de resultado silencioso**.

* na ingestao, reenviar o mesmo conteudo nao duplica trecho nem gasta embedding —
  o `checksum` do texto normalizado decide, e a resposta diz `idempotent: true`;
* na busca, o `HashingEmbedder` e deterministico, entao o trecho certo volta em
  primeiro lugar em qualquer maquina;
* no custo, o resumo agrega por modulo **e** por modelo, e a serie temporal devolve
  todo balde do intervalo, inclusive os de custo zero — um ponto ausente seria lido
  pelo grafico como "nao sei", quando o fato e "nao gastou";
* no orcamento, `hard_stop` estourado responde `402` na **proxima** invocacao, antes
  de qualquer chamada de provedor (etapa 4 da SPEC-0001).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from lukato.domain.types import Json
from tests.conftest import TEST_MODEL, SeedIds

pytestmark = pytest.mark.integration

MODELO_BARATO = "modelo-barato"
"""Segundo modelo precificado no teste, para separar o custo por modelo."""

TEXTO_COBRANCA = (
    "A fatura do plano residencial vence no dia dez de cada mes. "
    "O pagamento em atraso gera multa de dois por cento sobre o valor total."
)

TEXTO_INSTALACAO = (
    "A instalacao do roteador e agendada em ate cinco dias uteis. "
    "O tecnico confirma a visita por mensagem antes de sair para o endereco."
)


# --------------------------------------------------------------------------- #
# Utilitarios
# --------------------------------------------------------------------------- #
async def _ingere(client: AsyncClient, **corpo: Any) -> Json:
    """Ingere um documento e devolve o resultado da indexacao."""
    resposta = await client.post("/api/v1/knowledge/documents", json=corpo)
    assert resposta.status_code == 201, resposta.text
    return dict(resposta.json())


async def _cria_modulo(client: AsyncClient, slug: str, modelo: str) -> Json:
    """Cria uma definicao ativa sobre `processing` amarrada a um modelo especifico."""
    resposta = await client.post(
        "/api/v1/modules",
        json={
            "slug": slug,
            "name": slug,
            "kind": "agent",
            "status": "active",
            "runtime": "direct",
            "config": {"module": "processing"},
            "binding": {"model": modelo, "tools": []},
        },
    )
    assert resposta.status_code == 201, resposta.text
    return dict(resposta.json())


# --------------------------------------------------------------------------- #
# Conhecimento — ingestao idempotente
# --------------------------------------------------------------------------- #
async def test_ingestao_devolve_o_documento_com_checksum_e_trechos(
    client: AsyncClient,
) -> None:
    """A primeira ingestao recorta o texto, embedda e grava o checksum."""
    resultado = await _ingere(
        client,
        title="Politica de cobranca",
        content=TEXTO_COBRANCA,
        source="intranet/cobranca.md",
    )

    assert resultado["chunks"] >= 1
    assert resultado["embedded"] is True
    assert resultado["idempotent"] is False
    assert resultado["document"]["checksum"], "o checksum sustenta a idempotencia"
    assert resultado["embedding"]["provider"] == "hashing"
    assert resultado["embedding"]["dimensions"] == 1024


async def test_reingerir_o_mesmo_conteudo_e_idempotente_e_nao_duplica_trechos(
    client: AsyncClient,
) -> None:
    """SPEC-0007 criterio 1: dois envios iguais deixam um documento e os mesmos trechos."""
    primeira = await _ingere(
        client,
        title="Politica de cobranca",
        content=TEXTO_COBRANCA,
        source="intranet/cobranca.md",
    )

    segunda = await _ingere(
        client,
        title="Politica de cobranca",
        content=TEXTO_COBRANCA,
        source="intranet/cobranca.md",
    )

    assert segunda["idempotent"] is True, "o mesmo conteudo nao pode ser reembeddado"
    assert segunda["embedded"] is False
    assert segunda["document"]["id"] == primeira["document"]["id"]
    assert segunda["document"]["checksum"] == primeira["document"]["checksum"]
    assert segunda["chunks"] == primeira["chunks"]

    catalogo = await client.get("/api/v1/knowledge/documents")
    assert catalogo.json()["total"] == 1, "a reingestao nao pode criar um segundo documento"


async def test_reingerir_conteudo_diferente_na_mesma_origem_reindexa(
    client: AsyncClient,
) -> None:
    """Conteudo novo muda o checksum e refaz o indice — o oposto da idempotencia."""
    primeira = await _ingere(
        client, title="Politica", content=TEXTO_COBRANCA, source="intranet/politica.md"
    )

    segunda = await _ingere(
        client, title="Politica", content=TEXTO_INSTALACAO, source="intranet/politica.md"
    )

    assert segunda["document"]["id"] == primeira["document"]["id"]
    assert segunda["document"]["checksum"] != primeira["document"]["checksum"]
    assert segunda["embedded"] is True
    assert segunda["idempotent"] is False


# --------------------------------------------------------------------------- #
# Conhecimento — busca
# --------------------------------------------------------------------------- #
async def test_busca_devolve_o_trecho_certo_com_o_hashing_embedder(
    client: AsyncClient,
) -> None:
    """SPEC-0007 criterio 2: a busca determinista traz o documento correto primeiro."""
    cobranca = await _ingere(
        client, title="Cobranca", content=TEXTO_COBRANCA, source="kb/cobranca.md"
    )
    instalacao = await _ingere(
        client, title="Instalacao", content=TEXTO_INSTALACAO, source="kb/instalacao.md"
    )

    resposta = await client.post(
        "/api/v1/knowledge/search",
        json={"query": "quando vence a fatura do plano?", "limit": 5, "rerank": True},
    )

    assert resposta.status_code == 200, resposta.text
    hits = resposta.json()["hits"]
    assert hits, "a busca precisa devolver ao menos um trecho"
    assert hits[0]["document_id"] == cobranca["document"]["id"], (
        f"o trecho de cobranca deveria vir primeiro; veio {hits[0]['content'][:60]!r}"
    )
    assert hits[0]["document_id"] != instalacao["document"]["id"]
    assert 0.0 <= hits[0]["score"] <= 1.0, "o score e normalizado em [0, 1]"


async def test_busca_respeita_o_limite_pedido(client: AsyncClient) -> None:
    """`limit` recorta a quantidade de trechos devolvidos."""
    await _ingere(client, title="Cobranca", content=TEXTO_COBRANCA, source="kb/cobranca.md")
    await _ingere(client, title="Instalacao", content=TEXTO_INSTALACAO, source="kb/instalacao.md")

    resposta = await client.post("/api/v1/knowledge/search", json={"query": "fatura", "limit": 1})

    assert resposta.status_code == 200, resposta.text
    assert len(resposta.json()["hits"]) <= 1


async def test_busca_em_colecao_vazia_devolve_lista_vazia(client: AsyncClient) -> None:
    """Base sem documento nao e erro: e uma busca sem resultado."""
    resposta = await client.post("/api/v1/knowledge/search", json={"query": "qualquer coisa"})

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["hits"] == []


async def test_remover_documento_tira_o_trecho_do_indice(client: AsyncClient) -> None:
    """O que sai do indice para de aparecer na busca imediatamente."""
    documento = await _ingere(
        client, title="Cobranca", content=TEXTO_COBRANCA, source="kb/cobranca.md"
    )

    remocao = await client.delete(f"/api/v1/knowledge/documents/{documento['document']['id']}")
    busca = await client.post("/api/v1/knowledge/search", json={"query": "fatura do plano"})

    assert remocao.status_code == 204
    assert busca.json()["hits"] == []


async def test_saude_do_conhecimento_identifica_o_embedder_corrente(
    client: AsyncClient,
) -> None:
    """O modo `hashing` sempre se identifica: ele nao tem qualidade semantica real."""
    resposta = await client.get("/api/v1/knowledge/health")

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["provider"] == "hashing"
    assert corpo["dimensions"] == 1024
    assert corpo["default_collection"] == "teste_evidence"
    assert corpo["degraded"] is True
    assert corpo["reason"], "a degradacao precisa vir explicada"


async def test_colecoes_reportam_a_identidade_de_quem_as_produziu(
    client: AsyncClient,
) -> None:
    """A identidade do embedder acompanha a colecao (SPEC-0007 secao 1.2)."""
    await _ingere(client, title="Cobranca", content=TEXTO_COBRANCA, source="kb/cobranca.md")

    resposta = await client.get("/api/v1/knowledge/collections")

    assert resposta.status_code == 200, resposta.text
    colecoes = {item["name"]: item for item in resposta.json()["items"]}
    assert "teste_evidence" in colecoes
    alvo = colecoes["teste_evidence"]
    assert alvo["documents"] == 1
    assert alvo["embedding"]["provider"] == "hashing"
    assert alvo["compatible"] is True


# --------------------------------------------------------------------------- #
# FinOps — resumo e serie
# --------------------------------------------------------------------------- #
async def test_resumo_agrega_o_custo_por_modulo_e_por_modelo(
    client: AsyncClient, seeded: SeedIds
) -> None:
    """SPEC-0005 criterio 4: `GET /finops/summary` separa o gasto nas duas dimensoes."""
    precos = await client.put(
        "/api/v1/finops/prices",
        json={
            "prices": [
                {"model": TEST_MODEL, "input_usd_per_1k": 1.0, "output_usd_per_1k": 2.0},
                {"model": MODELO_BARATO, "input_usd_per_1k": 0.1, "output_usd_per_1k": 0.2},
            ]
        },
    )
    assert precos.status_code == 200, precos.text

    await _cria_modulo(client, "caro", TEST_MODEL)
    await _cria_modulo(client, "barato", MODELO_BARATO)
    for slug in ("caro", "barato"):
        invocacao = await client.post(
            f"/api/v1/modules/{slug}/invoke", json={"input": "quanto custa esta chamada?"}
        )
        assert invocacao.status_code == 200, invocacao.text

    resposta = await client.get("/api/v1/finops/summary")

    assert resposta.status_code == 200, resposta.text
    resumo = resposta.json()
    assert set(resumo["by_module"]) == {"caro", "barato"}
    assert set(resumo["by_model"]) == {TEST_MODEL, MODELO_BARATO}
    assert resumo["runs"] == 2
    assert resumo["total_tokens"] > 0
    assert resumo["by_module"]["caro"] > resumo["by_module"]["barato"], (
        "o modelo mais caro tinha de custar mais para o mesmo texto"
    )
    assert resumo["total_usd"] == pytest.approx(
        resumo["by_module"]["caro"] + resumo["by_module"]["barato"], rel=1e-6
    )
    assert sum(resumo["by_model"].values()) == pytest.approx(resumo["total_usd"], rel=1e-6)


async def test_resumo_filtrado_por_modulo_ignora_o_consumo_dos_outros(
    client: AsyncClient,
) -> None:
    """O filtro por modulo recorta o resumo sem recalcular nada por fora."""
    await _cria_modulo(client, "primeiro", TEST_MODEL)
    await _cria_modulo(client, "segundo", TEST_MODEL)
    await client.post("/api/v1/modules/primeiro/invoke", json={"input": "ola"})
    await client.post("/api/v1/modules/segundo/invoke", json={"input": "ola"})

    resposta = await client.get("/api/v1/finops/summary", params={"module_slug": "primeiro"})

    assert resposta.status_code == 200, resposta.text
    assert set(resposta.json()["by_module"]) == {"primeiro"}
    assert resposta.json()["runs"] == 1


async def test_serie_devolve_todos_os_baldes_do_intervalo_inclusive_os_vazios(
    client: AsyncClient,
) -> None:
    """Um zero explicito diz 'nao gastou'; um ponto ausente diria 'nao sei'."""
    await _cria_modulo(client, "medido", TEST_MODEL)
    invocacao = await client.post("/api/v1/modules/medido/invoke", json={"input": "ola"})
    assert invocacao.status_code == 200, invocacao.text

    resposta = await client.get("/api/v1/finops/series", params={"bucket": "hour", "since": "6h"})

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    pontos = corpo["points"]
    assert corpo["bucket"] == "hour"
    assert len(pontos) >= 6, f"a janela de 6 horas precisa de um balde por hora: {len(pontos)}"
    assert [ponto["bucket"] for ponto in pontos] == sorted(ponto["bucket"] for ponto in pontos), (
        "os baldes saem em ordem cronologica"
    )
    assert any(ponto["cost_usd"] == 0.0 for ponto in pontos), (
        "as horas sem consumo tem de aparecer com zero explicito"
    )
    assert any(ponto["cost_usd"] > 0.0 for ponto in pontos)
    assert corpo["total_usd"] == pytest.approx(sum(ponto["cost_usd"] for ponto in pontos), rel=1e-6)


async def test_serie_com_janela_invertida_devolve_422(client: AsyncClient) -> None:
    """Inicio depois do fim e pedido incoerente, nao serie vazia."""
    resposta = await client.get(
        "/api/v1/finops/series",
        params={
            "bucket": "hour",
            "since": "2026-08-25T10:00:00Z",
            "until": "2026-08-25T08:00:00Z",
        },
    )

    assert resposta.status_code == 422
    assert resposta.json()["error"]["code"] == "validation_error"


async def test_instante_em_formato_desconhecido_devolve_422_com_exemplos(
    client: AsyncClient,
) -> None:
    """A janela invalida explica o que era esperado em vez de assumir um recorte."""
    resposta = await client.get("/api/v1/finops/summary", params={"since": "ontem de tarde"})

    assert resposta.status_code == 422
    detalhes = resposta.json()["error"]["details"]
    assert detalhes["field"] == "since"
    assert detalhes["examples"]


async def test_listagem_de_consumo_traz_a_trilha_de_cada_chamada(
    client: AsyncClient,
) -> None:
    """Cada invocacao deixa um `UsageRecord` com tokens, custo e execucao de origem."""
    await _cria_modulo(client, "auditado", TEST_MODEL)
    invocacao = await client.post("/api/v1/modules/auditado/invoke", json={"input": "ola"})
    assert invocacao.status_code == 200, invocacao.text

    resposta = await client.get("/api/v1/finops/usage")

    assert resposta.status_code == 200, resposta.text
    registros = resposta.json()["items"]
    assert len(registros) == 1
    assert registros[0]["module_slug"] == "auditado"
    assert registros[0]["model"] == TEST_MODEL
    assert registros[0]["run_id"] == invocacao.json()["run_id"]
    assert registros[0]["cost_usd"] > 0.0


# --------------------------------------------------------------------------- #
# FinOps — orcamento
# --------------------------------------------------------------------------- #
async def test_orcamento_com_hard_stop_estourado_faz_a_invocacao_devolver_402(
    client: AsyncClient,
) -> None:
    """SPEC-0005 criterio 2 — o freio da plataforma, verificado ponta a ponta.

    A primeira invocacao gasta; o orcamento criado em seguida tem teto menor que o
    gasto acumulado, com parada dura. A segunda invocacao e recusada na etapa 4 da
    SPEC-0001, **antes** de abrir a execucao.
    """
    await _cria_modulo(client, "com-freio", TEST_MODEL)
    primeira = await client.post("/api/v1/modules/com-freio/invoke", json={"input": "ola"})
    assert primeira.status_code == 200, primeira.text
    gasto = primeira.json()["cost_usd"]
    assert gasto > 0.0

    orcamento = await client.post(
        "/api/v1/finops/budgets",
        json={
            "name": "teto apertado",
            "scope": "global",
            "limit_usd": gasto / 2.0,
            "period": "total",
            "hard_stop": True,
        },
    )
    assert orcamento.status_code == 201, orcamento.text

    segunda = await client.post("/api/v1/modules/com-freio/invoke", json={"input": "ola"})

    assert segunda.status_code == 402, segunda.text
    erro = segunda.json()["error"]
    assert erro["code"] == "budget_exceeded"
    assert erro["details"]["hard_stop"] is True
    assert erro["details"]["scope"] == "global"


async def test_orcamento_sem_hard_stop_nao_impede_a_invocacao(client: AsyncClient) -> None:
    """Sem parada dura o orcamento apenas informa: a operacao continua."""
    await _cria_modulo(client, "sem-freio", TEST_MODEL)
    primeira = await client.post("/api/v1/modules/sem-freio/invoke", json={"input": "ola"})
    assert primeira.status_code == 200, primeira.text

    criacao = await client.post(
        "/api/v1/finops/budgets",
        json={
            "name": "teto informativo",
            "scope": "global",
            "limit_usd": primeira.json()["cost_usd"] / 2.0,
            "period": "total",
            "hard_stop": False,
        },
    )
    assert criacao.status_code == 201, criacao.text

    segunda = await client.post("/api/v1/modules/sem-freio/invoke", json={"input": "ola"})

    assert segunda.status_code == 200, segunda.text


async def test_status_do_orcamento_acende_alerta_sem_bloquear(client: AsyncClient) -> None:
    """SPEC-0005 criterio 3: em 80% o alerta acende e `blocked` continua falso."""
    await _cria_modulo(client, "monitorado", TEST_MODEL)
    invocacao = await client.post("/api/v1/modules/monitorado/invoke", json={"input": "ola"})
    assert invocacao.status_code == 200, invocacao.text
    gasto = invocacao.json()["cost_usd"]

    criacao = await client.post(
        "/api/v1/finops/budgets",
        json={
            "name": "teto de alerta",
            "scope": "global",
            "limit_usd": gasto / 0.9,
            "period": "total",
            "alert_threshold": 0.8,
            "hard_stop": True,
        },
    )
    assert criacao.status_code == 201, criacao.text

    situacao = await client.get(f"/api/v1/finops/budgets/{criacao.json()['id']}/status")

    assert situacao.status_code == 200, situacao.text
    corpo = situacao.json()
    assert corpo["alert"] is True, "90% do teto tinha de acender o alerta"
    assert corpo["blocked"] is False, "abaixo de 100% nada e bloqueado"
    assert 0.8 <= corpo["ratio"] < 1.0

    seguinte = await client.post("/api/v1/modules/monitorado/invoke", json={"input": "ola"})
    assert seguinte.status_code == 200, "alerta nao impede execucao"


async def test_orcamento_restrito_a_outro_modulo_nao_bloqueia_o_modulo_livre(
    client: AsyncClient,
) -> None:
    """O escopo do orcamento e respeitado: o freio de um nao trava o outro."""
    await _cria_modulo(client, "vigiado", TEST_MODEL)
    await _cria_modulo(client, "livre", TEST_MODEL)
    primeira = await client.post("/api/v1/modules/vigiado/invoke", json={"input": "ola"})
    assert primeira.status_code == 200, primeira.text

    criacao = await client.post(
        "/api/v1/finops/budgets",
        json={
            "name": "teto do vigiado",
            "scope": "module:vigiado",
            "limit_usd": primeira.json()["cost_usd"] / 2.0,
            "period": "total",
            "hard_stop": True,
        },
    )
    assert criacao.status_code == 201, criacao.text

    bloqueado = await client.post("/api/v1/modules/vigiado/invoke", json={"input": "ola"})
    liberado = await client.post("/api/v1/modules/livre/invoke", json={"input": "ola"})

    assert bloqueado.status_code == 402
    assert liberado.status_code == 200, liberado.text


async def test_crud_de_orcamento_pela_api(client: AsyncClient) -> None:
    """Criar, ler, atualizar parcialmente e remover um orcamento."""
    criado = await client.post(
        "/api/v1/finops/budgets",
        json={"name": "mensal", "scope": "global", "limit_usd": 10.0, "period": "monthly"},
    )
    assert criado.status_code == 201, criado.text
    identificador = criado.json()["id"]

    lido = await client.get(f"/api/v1/finops/budgets/{identificador}")
    atualizado = await client.put(
        f"/api/v1/finops/budgets/{identificador}", json={"limit_usd": 25.0}
    )
    removido = await client.delete(f"/api/v1/finops/budgets/{identificador}")
    depois = await client.get(f"/api/v1/finops/budgets/{identificador}")

    assert lido.json()["limit_usd"] == 10.0
    assert atualizado.json()["limit_usd"] == 25.0
    assert atualizado.json()["name"] == "mensal", "o que nao foi enviado nao muda"
    assert removido.status_code == 204
    assert depois.status_code == 404


async def test_tabela_de_precos_atualizada_vale_na_proxima_invocacao(
    client: AsyncClient,
) -> None:
    """Corrigir o preco no processo em execucao muda o custo apurado em seguida."""
    await _cria_modulo(client, "reprecificado", TEST_MODEL)
    antes = await client.post("/api/v1/modules/reprecificado/invoke", json={"input": "ola"})
    assert antes.status_code == 200, antes.text

    ajuste = await client.put(
        "/api/v1/finops/prices",
        json={
            "prices": [{"model": TEST_MODEL, "input_usd_per_1k": 10.0, "output_usd_per_1k": 20.0}]
        },
    )
    assert ajuste.status_code == 200, ajuste.text

    depois = await client.post("/api/v1/modules/reprecificado/invoke", json={"input": "ola"})

    assert depois.status_code == 200, depois.text
    assert depois.json()["cost_usd"] == pytest.approx(antes.json()["cost_usd"] * 10.0, rel=1e-6)
