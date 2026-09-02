"""`GET /media/{id}/transcript` e a pagina de transcricao do console (SPEC-0010 secao 5).

A deteccao localiza a veiculacao; estes testes provam o degrau menor: qualquer
frase dita pode ser lida e localizada com precisao de palavra, pela API e pela
tela — sem FFmpeg, sem GPU e sem rede, a partir de uma transcricao importada.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

API: Final[str] = "/api/v1/adwatch"

PALAVRAS: Final[list[dict[str, Any]]] = [
    {"word": "Também.", "start": 6.5, "end": 6.9},
    {"word": "Na", "start": 6.94, "end": 7.0},
    {"word": "compra", "start": 7.0, "end": 7.3},
    {"word": "de", "start": 7.3, "end": 7.4},
    {"word": "um", "start": 7.4, "end": 7.5},
    {"word": "Motorola", "start": 7.5, "end": 8.0},
    {"word": "exclusivo,", "start": 8.0, "end": 8.6},
    {"word": "claro.", "start": 8.6, "end": 9.0},
    {"word": "Aproveite.", "start": 9.5, "end": 10.0},
]
"""A fala do comercial como o WhisperX a devolve: pontuada e com timestamps."""

FRASE: Final[str] = "na compra de um motorola exclusivo claro"
"""A frase como o operador a digita: minuscula e sem pontuacao."""


@pytest.fixture
async def midia(client: AsyncClient) -> str:
    """Registra o ativo de midia do comercial e devolve o seu identificador."""
    resposta = await client.post(
        f"{API}/media",
        json={
            "uri": "file:///acervo/aparelhos-pais.mp4",
            "title": "Comercial de Dia dos Pais",
            "duration_seconds": 33.0,
            "fps": 30.0,
        },
    )
    assert resposta.status_code == 201, resposta.text
    return str(resposta.json()["id"])


@pytest.fixture
async def midia_transcrita(client: AsyncClient, midia: str) -> str:
    """Importa a transcricao sintetica — o caminho que dispensa FFmpeg e GPU."""
    resposta = await client.post(f"{API}/media/{midia}/transcript", json=PALAVRAS)
    assert resposta.status_code == 201, resposta.text
    return midia


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
async def test_get_transcript_devolve_a_linha_do_tempo_completa(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """Sem `q`, a resposta e a transcricao inteira, palavra a palavra."""
    resposta = await client.get(f"{API}/media/{midia_transcrita}/transcript")

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["query"] is None
    assert corpo["matches"] == []
    palavras = corpo["transcript"]["words"]
    assert len(palavras) == len(PALAVRAS)
    assert palavras[5]["word"] == "Motorola"
    assert palavras[5]["start"] == 7.5


async def test_busca_localiza_a_frase_com_a_faixa_de_tempo_exata(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """`?q=` devolve a ocorrencia com inicio, fim e a grafia original da fala."""
    resposta = await client.get(f"{API}/media/{midia_transcrita}/transcript", params={"q": FRASE})

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["query"] == FRASE
    assert len(corpo["matches"]) == 1
    ocorrencia = corpo["matches"][0]
    assert ocorrencia["start"] == 6.94
    assert ocorrencia["end"] == 9.0
    assert ocorrencia["first_word"] == 1
    assert ocorrencia["last_word"] == 7
    assert ocorrencia["text"] == "Na compra de um Motorola exclusivo, claro."


async def test_busca_sem_ocorrencia_devolve_lista_vazia(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """Frase que nao foi dita e `matches` vazio com `200` — nunca erro."""
    resposta = await client.get(
        f"{API}/media/{midia_transcrita}/transcript", params={"q": "plano ilimitado"}
    )

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["matches"] == []


async def test_q_vazio_ou_so_espacos_equivale_a_nao_buscar(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """`?q=` e `?q=%20%20` sao leitura pura: `query` nulo e nenhum casamento."""
    for valor in ("", "   "):
        resposta = await client.get(
            f"{API}/media/{midia_transcrita}/transcript", params={"q": valor}
        )

        assert resposta.status_code == 200, resposta.text
        corpo = resposta.json()
        assert corpo["query"] is None
        assert corpo["matches"] == []


async def test_busca_acentuada_atravessa_a_borda_http(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """`?q=também` percorre percent-encoding UTF-8 e casa com "Também." da fala."""
    resposta = await client.get(
        f"{API}/media/{midia_transcrita}/transcript", params={"q": "também"}
    )

    assert resposta.status_code == 200, resposta.text
    ocorrencias = resposta.json()["matches"]
    assert len(ocorrencias) == 1
    assert ocorrencias[0]["text"] == "Também."


async def test_midia_sem_transcricao_devolve_404_com_o_caminho_a_seguir(
    client: AsyncClient, midia: str
) -> None:
    """Pedir a transcricao antes de ingerir ou importar e `not_found` instrutivo."""
    resposta = await client.get(f"{API}/media/{midia}/transcript")

    assert resposta.status_code == 404, resposta.text
    erro = resposta.json()["error"]
    assert erro["code"] == "not_found"
    assert "transcricao" in erro["message"]


async def test_midia_inexistente_devolve_404(client: AsyncClient) -> None:
    """Identificador desconhecido resolve para o `NotFoundError` da midia."""
    resposta = await client.get(f"{API}/media/nao-existe/transcript")

    assert resposta.status_code == 404, resposta.text


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #
async def test_pagina_renderiza_a_transcricao(client: AsyncClient, midia_transcrita: str) -> None:
    """A tela mostra as palavras; sem busca, nenhuma ganha destaque."""
    resposta = await client.get(f"/adwatch/media/{midia_transcrita}/transcript")

    assert resposta.status_code == 200, resposta.text
    assert resposta.headers["content-type"].startswith("text/html")
    assert "Motorola" in resposta.text
    assert "<mark" not in resposta.text


async def test_pagina_destaca_as_palavras_da_frase_buscada(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """Com `?q=`, as palavras casadas viram `<mark>` e a faixa de tempo aparece."""
    resposta = await client.get(
        f"/adwatch/media/{midia_transcrita}/transcript", params={"q": FRASE}
    )

    assert resposta.status_code == 200, resposta.text
    assert resposta.text.count("<mark") == 7
    assert "Ocorrências de" in resposta.text
    assert "1 ocorrência(s)" in resposta.text
    assert "“Na compra de um Motorola exclusivo, claro.”" in resposta.text


async def test_pagina_mostra_o_estado_de_frase_nao_encontrada(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """Busca sem casamento renderiza a orientacao, nao um card vazio nem erro."""
    resposta = await client.get(
        f"/adwatch/media/{midia_transcrita}/transcript", params={"q": "plano ilimitado"}
    )

    assert resposta.status_code == 200, resposta.text
    assert "Frase não encontrada" in resposta.text
    assert "<mark" not in resposta.text


async def test_pagina_de_midia_inexistente_e_o_error_page_404(client: AsyncClient) -> None:
    """Na UI o `NotFoundError` vira pagina de erro HTML, nunca JSON cru."""
    resposta = await client.get("/adwatch/media/nao-existe/transcript")

    assert resposta.status_code == 404, resposta.text
    assert resposta.headers["content-type"].startswith("text/html")


async def test_pagina_sem_transcricao_mostra_o_estado_vazio(
    client: AsyncClient, midia: str
) -> None:
    """Sem linha do tempo a pagina orienta o proximo passo em vez de falhar."""
    resposta = await client.get(f"/adwatch/media/{midia}/transcript")

    assert resposta.status_code == 200, resposta.text
    assert "ainda não tem transcrição" in resposta.text


async def test_pipeline_aponta_para_a_transcricao_de_cada_midia(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """A coluna Acoes do pipeline linka a pagina — e como o operador chega nela."""
    resposta = await client.get("/adwatch")

    assert resposta.status_code == 200, resposta.text
    assert f"/adwatch/media/{midia_transcrita}/transcript" in resposta.text
