"""Testes de unidade dos adaptadores de embeddings (SPEC-0007 secao 1, SPEC-0000 7.2).

`HashingEmbedder` e o provedor efetivo offline. Nao se cobra dele qualidade
semantica real — cobra-se o contrato: determinismo absoluto, dimensao igual a
configurada, norma L2 unitaria (senao o cosseno do `VectorStorePort` fica
indefinido) e uma ordenacao **relativa** util, em que dois textos parecidos ficam
mais proximos entre si do que de um texto alheio.

`QwenEmbedder` e a borda de rede. Nenhum teste aqui abre socket: o `httpx.AsyncClient`
e substituido por um transporte que devolve exatamente o corpo que o teste quer,
o que permite provar o `health()` que nunca levanta e a recusa de vetor com dimensao
divergente — que precisa dizer, na mensagem, que a colecao inteira tem de ser
re-embeddada.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
import pytest

from lukato.adapters.embeddings.factory import HASHING_REASONS, build_embedder
from lukato.adapters.embeddings.hashing import (
    DEFAULT_DIMENSIONS,
    HASHING_MODEL,
    HashingEmbedder,
)
from lukato.adapters.embeddings.qwen import EMBEDDINGS_PATH, QwenEmbedder
from lukato.config.settings import Settings
from lukato.domain.errors import ProviderError, ValidationError
from lukato.domain.ports.embeddings import EmbeddingPort

pytestmark = pytest.mark.unit

URL_FALSA = "https://hub.invalido.teste/embed/v1"
"""Endpoint inexistente: o transporte dublado responde antes de qualquer DNS."""

DIMENSOES = 64
"""Dimensao pequena: os testes leem o vetor inteiro sem custo."""


def _settings(**embedding: Any) -> Settings:
    """`Settings` de teste sem `.env`, com o grupo de embeddings do chamador."""
    base = {
        "provider": "qwen",
        "base_url": URL_FALSA,
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "dimensions": DIMENSOES,
        "batch_size": 2,
        "collection": "colecao-de-teste",
    }
    return Settings(_env_file=None, llm={"provider": "echo"}, embedding={**base, **embedding})


def _cosseno(esquerda: list[float], direita: list[float]) -> float:
    """Similaridade de cosseno entre dois vetores ja normalizados."""
    return sum(a * b for a, b in zip(esquerda, direita, strict=True))


# --------------------------------------------------------------------------- #
# HashingEmbedder
# --------------------------------------------------------------------------- #
async def test_hashing_e_deterministico_para_o_mesmo_texto() -> None:
    embedder = HashingEmbedder(dimensions=DIMENSOES)

    primeiro = await embedder.embed_one("plano de dados 50 giga")
    segundo = await embedder.embed_one("plano de dados 50 giga")
    terceiro = HashingEmbedder(dimensions=DIMENSOES).vector("plano de dados 50 giga")

    assert primeiro == segundo, "o mesmo texto tem de produzir exatamente o mesmo vetor"
    assert primeiro == terceiro, "instancias diferentes tem de concordar no vetor"


async def test_hashing_respeita_a_dimensao_configurada() -> None:
    settings = _settings(provider="hashing", dimensions=DIMENSOES)
    embedder = HashingEmbedder(settings)

    vetor = await embedder.embed_one("qualquer texto")

    assert embedder.dimensions == DIMENSOES
    assert len(vetor) == DIMENSOES
    assert embedder.model == HASHING_MODEL, "o modo degradado se identifica no `model`"


def test_hashing_sem_settings_usa_a_dimensao_padrao() -> None:
    assert HashingEmbedder().dimensions == DEFAULT_DIMENSIONS


def test_hashing_recusa_dimensao_nao_positiva() -> None:
    with pytest.raises(ValidationError):
        HashingEmbedder(dimensions=0)


async def test_hashing_devolve_vetores_com_norma_l2_igual_a_um() -> None:
    embedder = HashingEmbedder(dimensions=DIMENSOES)

    for texto in ("fatura em atraso", "", "   ", "50GB de internet"):
        vetor = await embedder.embed_one(texto)
        norma = math.sqrt(sum(valor * valor for valor in vetor))
        assert norma == pytest.approx(1.0), f"norma {norma} para o texto {texto!r}"


async def test_hashing_aproxima_textos_parecidos_mais_que_texto_alheio() -> None:
    embedder = HashingEmbedder(dimensions=512)

    parecido_a = await embedder.embed_one("plano de internet com 50 giga")
    parecido_b = await embedder.embed_one("plano de internet com 50GB")
    alheio = await embedder.embed_one("receita de bolo de cenoura com cobertura")

    proximidade = _cosseno(parecido_a, parecido_b)
    distancia = _cosseno(parecido_a, alheio)
    assert proximidade > distancia, (
        f"textos parecidos ({proximidade:.3f}) tem de ficar mais proximos que "
        f"um texto alheio ({distancia:.3f})"
    )


async def test_hashing_em_lote_preserva_a_ordem_de_entrada() -> None:
    embedder = HashingEmbedder(dimensions=DIMENSOES)
    textos = ["primeiro texto", "segundo texto", "terceiro texto"]

    lote = await embedder.embed(textos)

    assert len(lote) == len(textos)
    for posicao, texto in enumerate(textos):
        assert lote[posicao] == await embedder.embed_one(texto)


async def test_hashing_com_lista_vazia_devolve_lista_vazia() -> None:
    assert await HashingEmbedder(dimensions=DIMENSOES).embed([]) == []


async def test_hashing_esta_sempre_saudavel() -> None:
    embedder = HashingEmbedder(dimensions=DIMENSOES)

    assert await embedder.health() is True
    assert isinstance(embedder, EmbeddingPort)


def test_factory_escolhe_hashing_quando_pedido_explicitamente() -> None:
    adaptador = build_embedder(_settings(provider="hashing"))

    assert isinstance(adaptador, HashingEmbedder)
    assert HASHING_REASONS["hashing"].startswith("LUKATO_EMBEDDING__PROVIDER=hashing")


def test_factory_escolhe_hashing_quando_falta_o_endpoint_do_hub() -> None:
    settings = _settings(base_url="")

    adaptador = build_embedder(settings)

    assert isinstance(adaptador, HashingEmbedder)
    assert settings.embedding.effective_provider == "hashing"


# --------------------------------------------------------------------------- #
# QwenEmbedder
# --------------------------------------------------------------------------- #
def _cliente(responder: Any) -> httpx.AsyncClient:
    """`httpx.AsyncClient` com transporte em memoria — nenhuma conexao e aberta."""
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))


def _corpo(vetores: list[list[float]]) -> dict[str, Any]:
    """Corpo de resposta no formato compativel com OpenAI (`data[].embedding`)."""
    return {
        "data": [{"index": posicao, "embedding": vetor} for posicao, vetor in enumerate(vetores)]
    }


def test_qwen_monta_o_endpoint_concatenando_o_sufixo_de_embeddings() -> None:
    adaptador = QwenEmbedder(_settings(), client=_cliente(lambda request: httpx.Response(200)))

    assert adaptador.endpoint == URL_FALSA + EMBEDDINGS_PATH
    assert adaptador.dimensions == DIMENSOES
    assert adaptador.model == "Qwen/Qwen3-Embedding-0.6B"


async def test_qwen_health_devolve_false_sem_rede_e_sem_levantar() -> None:
    def recusa(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nome nao resolve", request=request)

    adaptador = QwenEmbedder(_settings(), client=_cliente(recusa))

    assert await adaptador.health() is False


async def test_qwen_health_devolve_true_quando_o_hub_responde_um_vetor() -> None:
    def responde(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_corpo([[0.0] * DIMENSOES]))

    adaptador = QwenEmbedder(_settings(), client=_cliente(responde))

    assert await adaptador.health() is True


async def test_qwen_divergencia_de_dimensao_vira_validation_error_pedindo_re_embedding() -> None:
    def responde(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_corpo([[0.1] * (DIMENSOES + 1)]))

    adaptador = QwenEmbedder(_settings(), client=_cliente(responde))

    with pytest.raises(ValidationError) as capturado:
        await adaptador.embed(["texto"])

    erro = capturado.value
    assert "re-embedde" in str(erro).lower(), (
        f"a mensagem precisa dizer que a colecao tem de ser re-embeddada: {erro}"
    )
    assert erro.details["expected_dimensions"] == DIMENSOES
    assert erro.details["received_dimensions"] == DIMENSOES + 1
    assert erro.details["collection"] == "colecao-de-teste"


async def test_qwen_divide_a_entrada_em_lotes_do_tamanho_configurado() -> None:
    enviados: list[list[str]] = []

    def responde(request: httpx.Request) -> httpx.Response:
        import json as _json

        entrada = _json.loads(request.content)["input"]
        enviados.append(entrada)
        return httpx.Response(200, json=_corpo([[0.0] * DIMENSOES for _ in entrada]))

    adaptador = QwenEmbedder(_settings(batch_size=2), client=_cliente(responde))

    vetores = await adaptador.embed(["a", "b", "c"])

    assert [len(lote) for lote in enviados] == [2, 1], "batch_size=2 corta 3 textos em 2 + 1"
    assert len(vetores) == 3


async def test_qwen_recusa_status_4xx_definitivo_com_provider_error() -> None:
    def responde(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="modelo desconhecido")

    adaptador = QwenEmbedder(_settings(), client=_cliente(responde))

    with pytest.raises(ProviderError) as capturado:
        await adaptador.embed(["texto"])

    assert capturado.value.details["status"] == 400


async def test_qwen_corpo_sem_data_nem_embeddings_vira_provider_error() -> None:
    def responde(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultado": "inesperado"})

    adaptador = QwenEmbedder(_settings(), client=_cliente(responde))

    with pytest.raises(ProviderError):
        await adaptador.embed(["texto"])
