"""As paginas de documentacao tem que RENDERIZAR, nao so responder 200.

`/api/docs` e `/api/redoc` estao na secao 4 do readme, ou seja, sao a primeira
porta em que o usuario bate. As rotas embutidas do FastAPI apontam para
`cdn.jsdelivr.net` num literal, enquanto a CSP desta aplicacao e `script-src
'self'` — o resultado era uma pagina que respondia **200 com o corpo em branco**,
porque a propria resposta proibia o script que ela mandava o navegador buscar.

Um teste de status code nunca pegaria isso: o 200 estava certo. O que estes
testes conferem e a COERENCIA entre o que a pagina pede e o que a resposta
permite — que e o que decide se a tela aparece.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio

ROTAS_DE_DOCUMENTACAO: Final[tuple[str, ...]] = ("/api/docs", "/api/redoc")

_RECURSO_EXTERNO = re.compile(r'(?:src|href)="(https?://[^"]+)"')


def _origens_pedidas(html: str) -> set[str]:
    """Origens externas que o HTML manda o navegador buscar."""
    origens = set()
    for url in _RECURSO_EXTERNO.findall(html):
        partes = urlsplit(url)
        if partes.scheme and partes.netloc:
            origens.add(f"{partes.scheme}://{partes.netloc}")
    return origens


def _diretiva(csp: str, nome: str) -> str:
    """Valor de uma diretiva da CSP, com queda para `default-src`."""
    encontradas = {
        parte.strip().split(" ", 1)[0]: parte.strip() for parte in csp.split(";") if parte.strip()
    }
    return encontradas.get(nome) or encontradas.get("default-src", "")


@pytest.mark.parametrize("rota", ROTAS_DE_DOCUMENTACAO)
async def test_a_csp_permite_tudo_que_a_pagina_manda_carregar(
    client: AsyncClient, rota: str
) -> None:
    """Nenhuma origem citada no HTML pode faltar na CSP da mesma resposta."""
    resposta = await client.get(rota)
    assert resposta.status_code == 200

    csp = resposta.headers.get("content-security-policy", "")
    assert csp, f"{rota} respondeu sem CSP"

    pedidas = _origens_pedidas(resposta.text)
    assert pedidas, (
        f"{rota} nao referencia nenhuma origem externa; se os bundles passaram a ser "
        "servidos localmente, este teste deve virar uma asercao de `script-src 'self'`"
    )

    scripts = _diretiva(csp, "script-src")
    estilos = _diretiva(csp, "style-src")
    for origem in pedidas:
        permitida = origem in scripts or origem in estilos or origem in _diretiva(csp, "img-src")
        assert permitida, (
            f"{rota} manda o navegador carregar {origem}, mas a CSP da propria resposta "
            f"nao permite: {csp!r}. A pagina responde 200 e fica em branco."
        )


async def test_o_console_nao_herda_a_folga_das_paginas_de_documentacao(
    client: AsyncClient,
) -> None:
    """A liberacao do CDN vale so nas duas rotas de documentacao.

    O console e offline-first: tudo dele sai de `static/`. Se a folga vazar para
    a moldura, uma pagina do console passa a poder executar script de terceiro —
    que e exatamente o que a CSP existe para impedir.
    """
    console = await client.get("/")
    assert console.status_code == 200
    csp = console.headers.get("content-security-policy", "")
    assert "jsdelivr" not in csp and "cdn." not in csp, (
        f"a CSP do console referencia um CDN: {csp!r}"
    )
    assert "script-src 'self'" in csp, f"o console deveria servir script so de si: {csp!r}"


async def test_a_origem_dos_bundles_e_configuravel(client: AsyncClient) -> None:
    """`LUKATO_APP__DOCS_ASSETS_BASE` decide de onde vem o Swagger e o ReDoc.

    Instalacao sem saida para a internet — o caso do cluster corporativo fechado,
    que e o alvo deste projeto — aponta a variavel para o espelho interno. Se o
    valor deixar de ser respeitado, a documentacao volta a depender de um CDN
    publico e some numa rede fechada, em silencio.
    """
    from lukato.config import get_settings

    base = get_settings().app.docs_assets_base
    resposta = await client.get("/api/docs")
    assert base.rstrip("/") in resposta.text, (
        f"a pagina nao usa a origem configurada ({base!r}); "
        "algum literal de CDN voltou para o codigo"
    )
