"""Catalogo de definicoes de modulo e o caminho unico de invocacao (SPEC-0001).

O que este arquivo prova, do CRUD ate a execucao:

* uma `ModuleDefinition` e **configuracao**, nao codigo — criar, ler, atualizar,
  publicar e remover uma definicao nao encosta em Python nenhum;
* `POST /{slug}/invoke` cumpre as onze etapas normativas: definicao inexistente e
  `404 module_not_found`, definicao em rascunho e `409`, e a execucao feliz devolve
  saida, consumo, custo e o rastro da execucao nos cabecalhos;
* **o requisito central da plataforma** (SPEC-0001 criterio 2): duas definicoes sobre
  a *mesma* classe `processing`, com bindings diferentes, respondem de formas
  diferentes — sem uma linha de codigo nova. E o teste
  `test_duas_definicoes_sobre_a_mesma_classe_processing_...` abaixo;
* `POST /{slug}/dry-run` mostra o que iria ao provedor **sem** chamar o provedor.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from lukato.adapters.orchestrator.factory import build_orchestrators
from lukato.adapters.orchestrator.tools import ToolContext, ToolRegistry
from lukato.application.container import Container
from lukato.config.settings import Settings
from lukato.domain.types import Json
from lukato.interfaces.http.api.v1.routers.modules import RUN_ID_HEADER, TRACE_ID_HEADER
from tests.conftest import TEST_MODEL, SeedIds
from tests.fakes import CountingLLM

pytestmark = pytest.mark.integration

CPF_VALIDO = "529.982.247-25"
"""CPF com digito verificador correto — o avaliador `pii_redact` valida o DV."""


# --------------------------------------------------------------------------- #
# Aparato: provedor espiao e tracer com rastro
# --------------------------------------------------------------------------- #
@pytest.fixture
def llm_espiao(
    container: Container,
    spy_llm: CountingLLM,
    settings: Settings,
    tools: tuple[ToolRegistry, ToolContext],
) -> CountingLLM:
    """Troca o provedor do container pelo espiao, reconstruindo os orquestradores.

    E o que permite afirmar **quantas vezes** o provedor foi chamado — a unica
    prova honesta de que o dry-run nao gasta token.
    """
    catalogo, contexto = tools
    container.llm = spy_llm
    container.orchestrators = build_orchestrators(
        spy_llm, settings=settings, tools=catalogo, tool_context=contexto
    )
    return spy_llm


class _SpanComRastro:
    """`SpanHandle` que expoe um `trace_id` estavel, como faz o Langfuse ativo."""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id

    def update(self, **kwargs: Any) -> None:
        """Descarta a atualizacao."""

    def end(self, **kwargs: Any) -> None:
        """Descarta o encerramento."""


class _TracerAtivo:
    """`TracerPort` minimo e inerte que, ao contrario do no-op, tem `trace_id`."""

    TRACE_ID = "trace-fixo-para-teste"

    def __init__(self) -> None:
        self._span = _SpanComRastro(self.TRACE_ID)

    @property
    def enabled(self) -> bool:
        return True

    def trace(self, *args: Any, **kwargs: Any) -> Any:
        return self._contexto()

    def span(self, *args: Any, **kwargs: Any) -> Any:
        return self._contexto()

    def generation(self, *args: Any, **kwargs: Any) -> Any:
        return self._contexto()

    async def score(self, **kwargs: Any) -> None:
        """Descarta a avaliacao."""

    async def flush(self) -> None:
        """Nao ha buffer para descarregar."""

    def _contexto(self) -> Any:
        from contextlib import asynccontextmanager

        span = self._span

        @asynccontextmanager
        async def _abre() -> AsyncIterator[_SpanComRastro]:
            yield span

        return _abre()


# --------------------------------------------------------------------------- #
# Utilitarios
# --------------------------------------------------------------------------- #
def _corpo_modulo(slug: str, **extra: Any) -> Json:
    """Corpo minimo de `POST /modules` para uma definicao sobre `processing`."""
    corpo: Json = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "kind": "agent",
        "status": "active",
        "runtime": "direct",
        "config": {"module": "processing"},
    }
    corpo.update(extra)
    return corpo


async def _cria(client: AsyncClient, slug: str, **extra: Any) -> Json:
    """Cria a definicao e devolve o corpo gravado, falhando alto em caso de erro."""
    resposta = await client.post("/api/v1/modules", json=_corpo_modulo(slug, **extra))
    assert resposta.status_code == 201, resposta.text
    return dict(resposta.json())


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
async def test_criar_definicao_devolve_201_com_a_trinca_gravada(client: AsyncClient) -> None:
    """A criacao grava slug, runtime e o binding inteiro."""
    resposta = await client.post(
        "/api/v1/modules",
        json=_corpo_modulo(
            "triagem",
            description="Classifica o pedido do cliente.",
            binding={"model": TEST_MODEL, "temperature": 0.0, "max_tokens": 64, "tools": []},
            tags=["suporte"],
        ),
    )

    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()
    assert corpo["slug"] == "triagem"
    assert corpo["status"] == "active"
    assert corpo["binding"]["model"] == TEST_MODEL
    assert corpo["binding"]["max_tokens"] == 64
    assert corpo["tags"] == ["suporte"]


async def test_criar_definicao_com_slug_repetido_devolve_409(client: AsyncClient) -> None:
    """Slug e a chave estavel do catalogo: repetir e conflito, nao atualizacao."""
    await _cria(client, "repetido")

    resposta = await client.post("/api/v1/modules", json=_corpo_modulo("repetido"))

    assert resposta.status_code == 409
    assert resposta.json()["error"]["code"] == "conflict"


async def test_buscar_definicao_por_slug_e_por_id(client: AsyncClient) -> None:
    """A rota resolve a referencia por slug e, na falta dele, por identificador."""
    criado = await _cria(client, "por-referencia")

    por_slug = await client.get("/api/v1/modules/por-referencia")
    por_id = await client.get(f"/api/v1/modules/{criado['id']}")

    assert por_slug.status_code == 200
    assert por_id.status_code == 200
    assert por_slug.json()["id"] == por_id.json()["id"] == criado["id"]


async def test_atualizacao_e_parcial_e_preserva_o_que_nao_foi_enviado(
    client: AsyncClient,
) -> None:
    """`PUT` muda apenas os campos presentes no corpo."""
    await _cria(client, "parcial", description="descricao original", tags=["a"])

    resposta = await client.put("/api/v1/modules/parcial", json={"description": "nova descricao"})

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["description"] == "nova descricao"
    assert corpo["tags"] == ["a"], "tags nao foi enviado e nao podia mudar"


async def test_patch_de_status_publica_e_pausa_a_definicao(client: AsyncClient) -> None:
    """O ciclo de vida e alterado por uma rota propria e idempotente."""
    await _cria(client, "ciclo", status="draft")

    publicado = await client.patch("/api/v1/modules/ciclo/status", json={"status": "active"})
    pausado = await client.patch("/api/v1/modules/ciclo/status", json={"status": "paused"})
    repetido = await client.patch("/api/v1/modules/ciclo/status", json={"status": "paused"})

    assert publicado.json()["status"] == "active"
    assert pausado.json()["status"] == "paused"
    assert repetido.status_code == 200
    assert repetido.json()["status"] == "paused"


async def test_remover_definicao_devolve_204_e_some_do_catalogo(client: AsyncClient) -> None:
    """Apagar a configuracao nao apaga a classe: o slug volta a ficar livre."""
    await _cria(client, "descartavel")

    removido = await client.delete("/api/v1/modules/descartavel")
    depois = await client.get("/api/v1/modules/descartavel")

    assert removido.status_code == 204
    assert removido.content == b""
    assert depois.status_code == 404
    assert depois.json()["error"]["code"] == "module_not_found"


# --------------------------------------------------------------------------- #
# Paginacao e filtros
# --------------------------------------------------------------------------- #
async def test_listagem_usa_o_envelope_normativo(client: AsyncClient, seeded: SeedIds) -> None:
    """Toda lista responde `items/total/limit/offset` (SPEC-0000 secao 11)."""
    resposta = await client.get("/api/v1/modules")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert set(corpo) == {"items", "total", "limit", "offset"}
    assert corpo["total"] == 2, "o seed cria `assistente` e `adwatch`"
    assert corpo["offset"] == 0


async def test_paginacao_recorta_a_janela_sem_perder_o_total(client: AsyncClient) -> None:
    """`limit` e `offset` recortam a pagina; `total` continua sendo o do filtro."""
    for indice in range(5):
        await _cria(client, f"pagina-{indice}")

    primeira = await client.get("/api/v1/modules", params={"limit": 2, "offset": 0})
    segunda = await client.get("/api/v1/modules", params={"limit": 2, "offset": 2})

    assert primeira.json()["total"] == 5
    assert segunda.json()["total"] == 5
    assert len(primeira.json()["items"]) == 2
    assert len(segunda.json()["items"]) == 2
    ids_primeira = {item["id"] for item in primeira.json()["items"]}
    ids_segunda = {item["id"] for item in segunda.json()["items"]}
    assert not (ids_primeira & ids_segunda), "as paginas nao podem se sobrepor"


async def test_filtro_por_status_devolve_apenas_o_estado_pedido(client: AsyncClient) -> None:
    """O filtro de ciclo de vida separa rascunho de publicado."""
    await _cria(client, "publicado", status="active")
    await _cria(client, "rascunho", status="draft")

    ativos = await client.get("/api/v1/modules", params={"status": "active"})
    rascunhos = await client.get("/api/v1/modules", params={"status": "draft"})

    assert [item["slug"] for item in ativos.json()["items"]] == ["publicado"]
    assert [item["slug"] for item in rascunhos.json()["items"]] == ["rascunho"]


async def test_filtro_por_kind_e_busca_textual(client: AsyncClient) -> None:
    """`kind` filtra pelo tipo funcional e `search` procura em slug, nome e descricao."""
    await _cria(client, "agente-cobranca", kind="agent", description="fala de fatura")
    await _cria(client, "pipeline-midia", kind="pipeline", description="processa video")

    por_kind = await client.get("/api/v1/modules", params={"kind": "pipeline"})
    por_busca = await client.get("/api/v1/modules", params={"search": "fatura"})

    assert [item["slug"] for item in por_kind.json()["items"]] == ["pipeline-midia"]
    assert [item["slug"] for item in por_busca.json()["items"]] == ["agente-cobranca"]


async def test_limite_de_pagina_fora_da_faixa_e_recusado(client: AsyncClient) -> None:
    """`limit` acima do teto e erro de validacao, nao truncamento silencioso."""
    resposta = await client.get("/api/v1/modules", params={"limit": 5000})

    assert resposta.status_code == 422
    assert resposta.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# Invocacao
# --------------------------------------------------------------------------- #
async def test_invocacao_feliz_devolve_saida_consumo_e_custo(
    client: AsyncClient, seeded: SeedIds
) -> None:
    """A execucao devolve o texto aprovado, o consumo de tokens e o custo apurado."""
    resposta = await client.post(
        "/api/v1/modules/assistente/invoke",
        json={"input": "Qual o horario de atendimento?"},
    )

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["output"].startswith("[echo] "), corpo["output"]
    assert "Qual o horario de atendimento?" in corpo["output"]
    assert corpo["usage"]["total_tokens"] > 0, "o consumo estimado nunca e zero"
    assert corpo["usage"]["total_tokens"] == (
        corpo["usage"]["prompt_tokens"] + corpo["usage"]["completion_tokens"]
    )
    assert corpo["cost_usd"] > 0.0, "o modelo do binding tem preco na tabela do teste"
    assert corpo["run_id"], "toda invocacao deixa um AgentRun persistido"


async def test_invocacao_devolve_o_cabecalho_x_run_id(client: AsyncClient, seeded: SeedIds) -> None:
    """`X-Run-Id` sai em toda invocacao — e o identificador que nunca falta."""
    resposta = await client.post("/api/v1/modules/assistente/invoke", json={"input": "oi"})

    assert resposta.status_code == 200, resposta.text
    assert resposta.headers[RUN_ID_HEADER] == resposta.json()["run_id"]


async def test_invocacao_devolve_o_cabecalho_x_trace_id_quando_ha_tracer_ativo(
    client: AsyncClient, container: Container, seeded: SeedIds
) -> None:
    """Com tracer ativo, o `trace_id` do rastro volta no cabecalho (SPEC-0008 secao 2)."""
    container.tracer = _TracerAtivo()

    resposta = await client.post("/api/v1/modules/assistente/invoke", json={"input": "oi"})

    assert resposta.status_code == 200, resposta.text
    assert resposta.headers[TRACE_ID_HEADER] == _TracerAtivo.TRACE_ID


async def test_invocacao_omite_x_trace_id_com_o_tracer_no_op(
    client: AsyncClient, seeded: SeedIds
) -> None:
    """Sem trace de verdade nao ha id: devolver um inventado mandaria procurar nada."""
    resposta = await client.post("/api/v1/modules/assistente/invoke", json={"input": "oi"})

    assert resposta.status_code == 200, resposta.text
    assert TRACE_ID_HEADER not in resposta.headers
    assert RUN_ID_HEADER in resposta.headers


async def test_invocar_slug_inexistente_devolve_404_module_not_found(
    client: AsyncClient,
) -> None:
    """Etapa 1 da SPEC-0001: definicao ausente e `module_not_found`, com envelope."""
    resposta = await client.post("/api/v1/modules/nao-existe/invoke", json={"input": "oi"})

    assert resposta.status_code == 404
    assert resposta.json()["error"]["code"] == "module_not_found"
    assert resposta.json()["error"]["details"]["reference"] == "nao-existe"


async def test_invocar_definicao_em_rascunho_devolve_409(client: AsyncClient) -> None:
    """Etapa 2 da SPEC-0001: somente `active` pode ser invocado."""
    await _cria(client, "so-rascunho", status="draft")

    resposta = await client.post("/api/v1/modules/so-rascunho/invoke", json={"input": "oi"})

    assert resposta.status_code == 409
    corpo = resposta.json()["error"]
    assert corpo["code"] == "conflict"
    assert corpo["details"]["status"] == "draft"
    assert corpo["details"]["required_status"] == "active"


async def test_invocacao_persiste_um_agent_run_com_passos(
    client: AsyncClient, seeded: SeedIds
) -> None:
    """Nao existe execucao invisivel: o run aparece na trilha com seus passos."""
    invocacao = await client.post("/api/v1/modules/assistente/invoke", json={"input": "ola"})
    assert invocacao.status_code == 200, invocacao.text

    run = await client.get(f"/api/v1/runs/{invocacao.json()['run_id']}")

    assert run.status_code == 200, run.text
    corpo = run.json()
    assert corpo["module_slug"] == "assistente"
    assert corpo["status"] == "succeeded"
    assert corpo["steps"], "a execucao precisa registrar os passos"


# --------------------------------------------------------------------------- #
# O requisito central: uma classe, varias configuracoes
# --------------------------------------------------------------------------- #
async def test_duas_definicoes_sobre_a_mesma_classe_processing_respondem_diferente(
    client: AsyncClient, seeded: SeedIds
) -> None:
    """SPEC-0001 criterio 2 — a prova do requisito central da plataforma.

    Duas `ModuleDefinition` apontam para a **mesma** classe (`config.module ==
    "processing"`), recebem **o mesmo texto de entrada** e diferem apenas no
    binding: uma vincula o guardrail de saida `saida-padrao`, a outra nao vincula
    nada. O resultado tem de ser diferente — e a diferenca nasce inteiramente da
    configuracao, sem uma linha de codigo nova em `processing`.
    """
    entrada = f"Confirme o cadastro do CPF {CPF_VALIDO}, por favor."

    protegido = await _cria(
        client,
        "atendimento-protegido",
        binding={
            "model": TEST_MODEL,
            "output_guardrail_id": seeded.output_policy_id,
            "tools": [],
        },
    )
    cru = await _cria(client, "atendimento-cru", binding={"model": TEST_MODEL, "tools": []})

    assert protegido["config"]["module"] == cru["config"]["module"] == "processing", (
        "as duas definicoes precisam apontar para a mesma classe de building block"
    )

    resposta_protegida = await client.post(
        "/api/v1/modules/atendimento-protegido/invoke", json={"input": entrada}
    )
    resposta_crua = await client.post(
        "/api/v1/modules/atendimento-cru/invoke", json={"input": entrada}
    )

    assert resposta_protegida.status_code == 200, resposta_protegida.text
    assert resposta_crua.status_code == 200, resposta_crua.text

    saida_protegida = resposta_protegida.json()["output"]
    saida_crua = resposta_crua.json()["output"]

    assert saida_protegida != saida_crua, (
        "mesma classe, mesma entrada, bindings diferentes: as respostas TEM de divergir"
    )
    assert CPF_VALIDO not in saida_protegida, "o guardrail de saida vinculado tinha de redigir"
    assert "[REDIGIDO]" in saida_protegida
    assert CPF_VALIDO in saida_crua, "sem politica vinculada o estagio e permissivo"
    assert resposta_protegida.json()["findings"], "a definicao protegida registra o achado"
    assert resposta_crua.json()["findings"] == []


async def test_duas_definicoes_sobre_a_mesma_classe_diferem_tambem_no_teto_de_tokens(
    client: AsyncClient, seeded: SeedIds
) -> None:
    """O mesmo texto rende saidas de tamanhos diferentes so por causa do binding."""
    entrada = "Explique em detalhes a politica de cobranca da empresa para o cliente."

    await _cria(client, "conciso", binding={"model": TEST_MODEL, "max_tokens": 4, "tools": []})
    await _cria(client, "completo", binding={"model": TEST_MODEL, "max_tokens": 512, "tools": []})

    curto = await client.post("/api/v1/modules/conciso/invoke", json={"input": entrada})
    longo = await client.post("/api/v1/modules/completo/invoke", json={"input": entrada})

    assert curto.status_code == 200, curto.text
    assert longo.status_code == 200, longo.text
    assert len(curto.json()["output"]) < len(longo.json()["output"]), (
        "o teto de tokens do binding tinha de truncar a resposta do modulo `conciso`"
    )


# --------------------------------------------------------------------------- #
# Ensaio (dry-run)
# --------------------------------------------------------------------------- #
async def test_dry_run_nao_chama_o_provedor(
    client: AsyncClient, seeded: SeedIds, llm_espiao: CountingLLM
) -> None:
    """O ensaio mostra o envio que **nao** aconteceu: nenhum token e gasto."""
    resposta = await client.post(
        "/api/v1/modules/assistente/dry-run", json={"input": "Explique a fatura."}
    )

    assert resposta.status_code == 200, resposta.text
    assert llm_espiao.calls == 0, "dry-run nao pode encostar no provedor"
    corpo = resposta.json()
    assert corpo["would_call_provider"] is True
    assert corpo["allowed"] is True
    assert corpo["plan"]["messages"][-1]["role"] == "user"
    assert corpo["system_prompt"]["bound"] is True


async def test_dry_run_nao_cria_execucao(client: AsyncClient, seeded: SeedIds) -> None:
    """Nenhum `AgentRun` nasce de um ensaio."""
    antes = (await client.get("/api/v1/runs")).json()["total"]

    await client.post("/api/v1/modules/assistente/dry-run", json={"input": "Explique a fatura."})

    depois = (await client.get("/api/v1/runs")).json()["total"]
    assert depois == antes, "o ensaio nao pode deixar execucao no historico"


async def test_dry_run_funciona_com_definicao_em_rascunho(client: AsyncClient) -> None:
    """E para depurar rascunho que o ensaio existe: status nao impede o ensaio."""
    await _cria(client, "em-rascunho", status="draft")

    resposta = await client.post("/api/v1/modules/em-rascunho/dry-run", json={"input": "oi"})

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["module_status"] == "draft"
    assert corpo["invocable"] is False


async def test_invocacao_chama_o_provedor_uma_unica_vez(
    client: AsyncClient, seeded: SeedIds, llm_espiao: CountingLLM
) -> None:
    """O runtime `direct` faz exatamente uma chamada de LLM por invocacao."""
    resposta = await client.post("/api/v1/modules/assistente/invoke", json={"input": "ola"})

    assert resposta.status_code == 200, resposta.text
    assert llm_espiao.calls == 1
    assert llm_espiao.last_system_prompt.startswith("Voce e o assistente geral do lukato")


async def test_registry_lista_os_cinco_building_blocks(client: AsyncClient) -> None:
    """SPEC-0001 criterio 1: o registry publica os embutidos com capacidades e schema."""
    resposta = await client.get("/api/v1/registry")

    assert resposta.status_code == 200, resposta.text
    itens = resposta.json()["items"]
    slugs = {item["slug"] for item in itens}
    assert {"auth", "processing", "finops", "knowledge", "adwatch"} <= slugs
    processing = next(item for item in itens if item["slug"] == "processing")
    assert processing["capabilities"], "o registry precisa publicar as capacidades"
    assert "config_schema" in processing


def test_contrato_publica_as_quatro_rotas_do_recurso(app: FastAPI) -> None:
    """Rede de seguranca: as rotas do recurso existem no contrato da aplicacao."""
    caminhos = set(app.openapi()["paths"])

    assert "/api/v1/modules" in caminhos
    assert "/api/v1/modules/{slug}" in caminhos
    assert "/api/v1/modules/{slug}/invoke" in caminhos
    assert "/api/v1/modules/{slug}/dry-run" in caminhos
