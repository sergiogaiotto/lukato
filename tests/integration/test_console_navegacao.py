"""O console tem que ser NAVEGAVEL: nenhum link do menu pode dar 404.

Este arquivo nasce de dois defeitos que so aparecem quando alguem usa a tela.

1. O menu esquerdo trazia "Processamento" apontando para `/modules/processing`.
   `processing` e o nome de uma IMPLEMENTACAO; `/modules/{slug}` recebe o slug
   de uma INSTANCIA. As instancias que rodam esse codigo nascem com nome de
   dominio (`assistente`, `triagem`), entao o link dava 404 — em toda pagina do
   console, porque o menu e o mesmo em todas.

2. Um formulario que falhava devolvia JSON cru ao navegador. A negociacao de
   conteudo existia so no tratador de `HTTPException` (404, 405); os erros que
   um formulario realmente produz — `409 conflict` de slug repetido, `422
   validation_error` de campo fora do schema — passavam por outro tratador e
   voltavam como texto. Quem clicava em Salvar caia numa tela de JSON.

Os dois passaram por toda a bateria anterior porque ela conferia o GET das
paginas e o POST em JSON, nunca a navegacao de ponta a ponta.
"""

from __future__ import annotations

import re
from typing import Final

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

CABECALHOS_DE_NAVEGADOR: Final[dict[str, str]] = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "http://testserver/prompts",
}

MENU: Final[frozenset[str]] = frozenset(
    {
        "/",
        "/modules",
        "/modules?kind=agent",
        "/runs",
        "/knowledge",
        "/adwatch",
        "/adwatch/commercials",
        "/adwatch/detections",
        "/prompts",
        "/guardrails",
        "/registry",
        "/finops",
        "/observability",
        "/identity",
        "/identity?tab=api-keys",
        "/settings",
    }
)
"""Todo destino do menu esquerdo. Um item novo entra aqui junto com o codigo."""

PAGINAS: Final[tuple[str, ...]] = (
    "/",
    "/modules",
    "/runs",
    "/prompts",
    "/guardrails",
    "/knowledge",
    "/registry",
    "/identity",
    "/finops",
    "/observability",
    "/settings",
    "/adwatch",
    "/adwatch/commercials",
    "/adwatch/detections",
)


async def test_nenhum_link_interno_do_console_da_404(client: AsyncClient) -> None:
    """Varre os links de todas as paginas e abre cada um.

    E o teste que teria pego "Processamento" no dia em que o item foi escrito.
    Um link de menu quebrado nao aparece em teste de rota: a rota `/modules/{slug}`
    existe e responde — so nao existe instancia com aquele slug.
    """
    vistos: set[str] = set()
    quebrados: list[str] = []

    for pagina in PAGINAS:
        resposta = await client.get(pagina)
        assert resposta.status_code == 200, f"{pagina} respondeu {resposta.status_code}"
        for destino in re.findall(r'href="(/[^"#]*)"', resposta.text):
            if destino in vistos or destino.startswith("/static"):
                continue
            vistos.add(destino)
            alvo = await client.get(destino)
            if alvo.status_code != 200:
                quebrados.append(f"{alvo.status_code} em {destino} (link de {pagina})")

    # Conjunto nomeado em vez de um piso numerico: a contagem oscila com os dados
    # de exemplo (37 num banco vazio, 75 depois do seed) e um piso arbitrario ou
    # quebra sozinho ou para de significar alguma coisa. Estes destinos sao o
    # menu inteiro; se um sumir do HTML, a varredura deixou de olhar o menu.
    assert MENU <= vistos, f"o menu nao foi varrido por inteiro; faltou {sorted(MENU - vistos)}"
    assert not quebrados, "links quebrados no console:\n  " + "\n  ".join(sorted(quebrados))


async def test_erro_de_formulario_volta_como_pagina_e_nao_como_json(
    client: AsyncClient,
) -> None:
    """Slug repetido: o navegador recebe a pagina de erro, com moldura.

    409 e o desfecho mais comum de quem clica em Salvar duas vezes. Antes ele
    passava pelo tratador de erro de dominio, que nao negociava conteudo, e
    devolvia o envelope cru.
    """
    corpo = "slug=repetido-de-proposito&name=Um&role=system&template=oi&labels[]=teste"
    primeira = await client.post("/api/v1/prompts", content=corpo, headers=CABECALHOS_DE_NAVEGADOR)
    assert primeira.status_code == 303, "o primeiro salvamento tem que redirecionar"

    segunda = await client.post("/api/v1/prompts", content=corpo, headers=CABECALHOS_DE_NAVEGADOR)
    assert segunda.status_code == 409
    assert segunda.headers["content-type"].startswith("text/html"), (
        "o navegador recebeu JSON cru depois de clicar em Salvar: " + segunda.text[:200]
    )
    assert "Erro 409" in segunda.text
    # O texto tem que descrever o que aconteceu: um Salvar que nao gravou, e nao
    # uma tela que nao abriu. A mesma pagina atende os dois casos.
    assert "Não foi possível salvar" in segunda.text
    assert "Nada foi gravado" in segunda.text


async def test_erro_de_validacao_tambem_volta_como_pagina(client: AsyncClient) -> None:
    """422: o campo que falta tem que aparecer numa tela, nao num envelope."""
    resposta = await client.post(
        "/api/v1/prompts", content="slug=&role=system", headers=CABECALHOS_DE_NAVEGADOR
    )
    assert resposta.status_code == 422
    assert resposta.headers["content-type"].startswith("text/html"), resposta.text[:200]
    assert "Erro 422" in resposta.text
    assert "Não foi possível salvar" in resposta.text


async def test_cliente_de_api_continua_recebendo_o_envelope_json(client: AsyncClient) -> None:
    """A contrapartida: quem fala JSON nunca recebe HTML.

    Sem esta trava, "fazer o console funcionar" viraria uma quebra de contrato
    para todo cliente de API — o oposto do que se pediu.
    """
    corpo = {"slug": "so-json", "role": "system", "template": "oi"}
    assert (await client.post("/api/v1/prompts", json=corpo)).status_code in {200, 201}

    repetido = await client.post("/api/v1/prompts", json=corpo)
    assert repetido.status_code == 409
    assert repetido.headers["content-type"].startswith("application/json")
    assert repetido.json()["error"]["code"] == "conflict"

    ausente = await client.get("/api/v1/prompts/nao-existe-mesmo")
    assert ausente.status_code == 404
    assert ausente.headers["content-type"].startswith("application/json")


async def test_pagina_que_nao_existe_continua_falando_de_abrir(client: AsyncClient) -> None:
    """A contrapartida do texto: um 404 de navegacao nao pode dizer "salvar"."""
    resposta = await client.get("/rota-que-nao-existe", headers={"Accept": "text/html"})
    assert resposta.status_code == 404
    assert "Não foi possível abrir esta tela" in resposta.text


async def test_menu_aponta_para_a_lista_de_instancias_do_agente(client: AsyncClient) -> None:
    """O item do agente generico leva a uma lista com as instancias dele.

    Trava o conserto no lugar certo: nao basta o link responder 200, ele precisa
    mostrar os modulos que rodam aquela implementacao.
    """
    home = await client.get("/")
    assert "/modules/processing" not in home.text, "o link quebrado voltou ao menu"

    assert 'href="/modules?kind=agent"' in home.text, "o item do agente sumiu do menu"

    # A lista responde e e a lista de MODULOS — nao basta o link nao dar 404.
    # O banco da fixture nasce vazio, entao o que se pode afirmar aqui e a
    # moldura; que ela traga as instancias esta provado na navegacao com dados.
    lista = await client.get("/modules?kind=agent")
    assert lista.status_code == 200
    assert 'name="kind"' in lista.text


async def test_painel_do_modulo_acha_a_classe_pelo_mesmo_caminho_da_invocacao(
    client: AsyncClient,
) -> None:
    """O painel nao pode dizer que falta building block quando a invocacao roda.

    O console procurava a classe pelo slug da DEFINICAO. Uma definicao com nome
    de dominio (`assistente`) sobre a classe `processing` nao casava, e a tela
    dizia "nenhuma classe registrada com este slug; a invocacao precisa de um
    building block no Registry" — enquanto o botao Invocar, logo abaixo, gravava
    uma execucao com sucesso. Duas afirmacoes contrarias na mesma tela.
    """
    criado = await client.post(
        "/api/v1/modules",
        json={
            "slug": "definicao-com-nome-de-dominio",
            "name": "Definicao com nome de dominio",
            "kind": "agent",
            "config": {"module": "processing"},
        },
    )
    assert criado.status_code in {200, 201}, criado.text

    pagina = await client.get("/modules/definicao-com-nome-de-dominio")
    assert pagina.status_code == 200
    assert "Nenhuma classe registrada" not in pagina.text, (
        "o painel diz que falta building block para uma definicao que aponta "
        "`config.module = processing`, registrado e invocavel"
    )
