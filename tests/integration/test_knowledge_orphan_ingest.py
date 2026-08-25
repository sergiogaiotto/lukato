"""Ingestao que falha no embedder nao pode deixar documento orfao (SPEC-0007).

A SPEC-0007 secao 1.1 protege a base contra o pior tipo de dano: o que nao avisa.
La o exemplo e a colecao com dois espacos semanticos misturados, que passa a
"devolver resultados errados, para sempre, sem sinal". O caso destes testes e a
outra face da mesma moeda: **nenhum** resultado, para sempre, sem sinal.

O caminho e este. `POST /knowledge/documents` grava a linha do documento numa
transacao curta e so depois pede os vetores ao hub. Se o hub estiver fora do ar,
a resposta e 502 — correto — mas o documento ja esta gravado, com zero chunks.
Sem os dois cuidados exercitados aqui, o segundo envio identico cairia no caminho
idempotente por checksum e responderia `201 Created`, `idempotent: true`,
`chunks: 0`: uma indisponibilidade transitoria do hub viraria um documento
permanente, invisivel a busca, com a API dizendo que deu tudo certo.

Os testes cobrem as duas metades:

* a ingestao que falha e **compensada** — o documento recem-gravado sai do banco;
* um documento sem nenhum vetor **volta ao embedder** na reingestao, em vez de
  ser confundido com conteudo ja indexado. E o que cura os orfaos que ficaram
  gravados antes desta correcao.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from httpx import AsyncClient

from lukato.adapters.embeddings.hashing import HashingEmbedder
from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.application.container import Container
from lukato.application.use_cases.knowledge import content_checksum, normalize_content
from lukato.config.settings import Settings
from lukato.domain.errors import ProviderError
from lukato.domain.models.knowledge import Document

pytestmark = pytest.mark.integration

TEXTO_COBRANCA = (
    "A fatura do plano residencial vence no dia dez de cada mes. "
    "O pagamento em atraso gera multa de dois por cento sobre o valor total."
)

ORIGEM = "intranet/cobranca.md"
TITULO = "Politica de cobranca"


def _corpo() -> dict[str, Any]:
    """Corpo do `POST /knowledge/documents` usado por todos os testes do arquivo."""
    return {"title": TITULO, "content": TEXTO_COBRANCA, "source": ORIGEM}


class HubInstavel:
    """`EmbeddingPort` que so responde quando `disponivel` e True.

    Reproduz a indisponibilidade transitoria do hub de embeddings: fora do ar,
    `embed` levanta :class:`ProviderError`, que a borda HTTP traduz em 502.
    Provider, modelo e dimensao continuam os do embedder real, para que a guarda
    de compatibilidade da colecao (SPEC-0007 secao 1.2) nao entre no meio do
    caminho quando o hub voltar.
    """

    def __init__(self, real: HashingEmbedder) -> None:
        self._real = real
        self.disponivel = True
        self.tentativas = 0

    @property
    def provider(self) -> str:
        """Mesmo provedor do embedder real: a colecao nao muda de dono."""
        return str(self._real.provider)

    @property
    def model(self) -> str:
        """Mesmo modelo do embedder real."""
        return self._real.model

    @property
    def dimensions(self) -> int:
        """Mesma dimensao do embedder real."""
        return self._real.dimensions

    def _falha(self) -> ProviderError:
        """Erro identico ao do adaptador de rede quando o hub nao responde."""
        return ProviderError(
            "falha de rede com o hub de embeddings em embed",
            details={"action": "embed"},
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embedda em lote; fora do ar, levanta `ProviderError`."""
        self.tentativas += 1
        if not self.disponivel:
            raise self._falha()
        return await self._real.embed(texts)

    async def embed_one(self, text: str) -> list[float]:
        """Embedda um texto; fora do ar, levanta `ProviderError`."""
        self.tentativas += 1
        if not self.disponivel:
            raise self._falha()
        return await self._real.embed_one(text)

    async def health(self) -> bool:
        """Saude do hub simulado."""
        return self.disponivel


@pytest.fixture
def hub(container: Container, embedder: HashingEmbedder) -> HubInstavel:
    """Troca o embedder do container por um hub que da para derrubar em teste."""
    instavel = HubInstavel(embedder)
    container.embeddings = instavel
    return instavel


async def _total_de_documentos(client: AsyncClient) -> int:
    """Quantos documentos o catalogo lista."""
    resposta = await client.get("/api/v1/knowledge/documents")
    assert resposta.status_code == 200, resposta.text
    return int(resposta.json()["total"])


async def test_falha_do_hub_na_ingestao_nao_deixa_documento_gravado(
    client: AsyncClient, hub: HubInstavel
) -> None:
    """Hub fora do ar responde 502 e nao deixa documento nenhum para tras.

    A gravacao acontece antes do embedding, de proposito (a transacao de banco e
    curta). A atomicidade vem por compensacao: erro no embedder desfaz a linha.
    """
    hub.disponivel = False

    resposta = await client.post("/api/v1/knowledge/documents", json=_corpo())

    assert resposta.status_code == 502, resposta.text
    assert resposta.json()["error"]["code"] == "provider_error"
    assert await _total_de_documentos(client) == 0, (
        "documento gravado sem vetor e invisivel a busca e nao aparece como pendencia"
    )


async def test_segunda_ingestao_com_hub_fora_do_ar_nao_responde_201(
    client: AsyncClient, hub: HubInstavel
) -> None:
    """Reenviar o mesmo conteudo com o hub caido nao pode virar `201 Created`.

    E o coracao do defeito: o caminho idempotente por checksum reconhecia o
    documento gravado pela tentativa anterior, atualizava metadados e devolvia
    `201`, `idempotent: true`, `chunks: 0` — sem nunca pedir um vetor. A falha
    transitoria do hub virava perda permanente e silenciosa.
    """
    hub.disponivel = False

    primeira = await client.post("/api/v1/knowledge/documents", json=_corpo())
    segunda = await client.post("/api/v1/knowledge/documents", json=_corpo())

    assert primeira.status_code == 502, primeira.text
    assert segunda.status_code == 502, (
        f"a segunda ingestao respondeu {segunda.status_code} com o hub fora do ar: {segunda.text}"
    )
    assert await _total_de_documentos(client) == 0
    assert hub.tentativas >= 2, "a segunda ingestao tem de tentar embeddar de novo"


async def test_orfao_ja_gravado_volta_ao_embedder_e_passa_a_ser_achado(
    client: AsyncClient,
    uow_factory: UnitOfWorkFactoryImpl,
    settings: Settings,
) -> None:
    """Documento gravado com zero chunks e reindexado na reingestao, e a busca o acha.

    O orfao aqui e gravado direto no banco porque e exatamente o que a versao
    anterior deixava para tras quando o hub falhava. Com o hub de volta, reenviar
    o mesmo conteudo tem de re-embeddar: mesmo checksum sem nenhum vetor nao e
    conteudo ja indexado, e responder `idempotent` a ele deixaria o documento
    invisivel para sempre.
    """
    conteudo = normalize_content(TEXTO_COBRANCA)
    orfao = Document(
        collection=settings.embedding.collection,
        title=TITULO,
        source=ORIGEM,
        content=conteudo,
        checksum=content_checksum(conteudo),
    )
    async with uow_factory() as uow:
        await uow.documents.add(orfao)
        await uow.commit()

    resposta = await client.post("/api/v1/knowledge/documents", json=_corpo())

    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()
    assert corpo["document"]["id"] == orfao.id, "a reingestao reaproveita o documento gravado"
    assert corpo["embedded"] is True, "documento sem vetor precisa voltar ao embedder"
    assert corpo["idempotent"] is False
    assert corpo["chunks"] >= 1

    busca = await client.post(
        "/api/v1/knowledge/search", json={"query": "quando vence a fatura do plano?"}
    )
    assert busca.status_code == 200, busca.text
    achados = [hit["document_id"] for hit in busca.json()["hits"]]
    assert orfao.id in achados, "o documento reindexado tem de aparecer na busca"


async def test_reingestao_de_documento_indexado_continua_idempotente(
    client: AsyncClient,
) -> None:
    """A correcao nao pode custar a idempotencia (SPEC-0007 criterio de aceite 1)."""
    primeira = await client.post("/api/v1/knowledge/documents", json=_corpo())
    assert primeira.status_code == 201, primeira.text

    segunda = await client.post("/api/v1/knowledge/documents", json=_corpo())

    assert segunda.status_code == 201, segunda.text
    corpo = segunda.json()
    assert corpo["idempotent"] is True, "conteudo ja indexado nao volta ao embedder"
    assert corpo["embedded"] is False
    assert corpo["document"]["id"] == primeira.json()["document"]["id"]
    assert corpo["chunks"] == primeira.json()["chunks"]
    assert await _total_de_documentos(client) == 1
