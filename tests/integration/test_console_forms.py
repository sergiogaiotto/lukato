"""Os botoes do console tem que GRAVAR, nao so aparecer.

Este arquivo existe por causa de um defeito que passou por 703 chamadas de
varredura automatizada sem ser visto, e que um usuario achou em um clique.

O console tem 33 formularios `method="post"` apontando para `/api/v1/...`. Um
formulario HTML manda `application/x-www-form-urlencoded`; a API so aceita JSON.
Clicar em qualquer botao de gravar devolvia uma tela de JSON cru:

    "msg": "Input should be a valid dictionary or object to extract fields from"
    "input": "uri=C%3A%5CUsers%5C...&title=tv&kind=video"

Nenhuma acao de escrita funcionava. Os testes nao pegaram porque exercitavam a
API com JSON — que sempre funcionou — e o console apenas pelo GET das paginas.
Testavam a fachada, nao o uso.

A licao esta na forma destes testes: eles mandam o corpo EXATAMENTE como um
navegador manda, com `Content-Type: application/x-www-form-urlencoded` e
`Accept: text/html`. Um teste que monte o JSON na mao volta a nao provar nada.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from httpx import AsyncClient

from lukato.interfaces.http.console_forms import form_para_json

pytestmark = pytest.mark.anyio

TEMPLATES: Final[Path] = Path(__file__).resolve().parents[2] / "src/lukato/interfaces/ui/templates"

CABECALHOS_DE_NAVEGADOR: Final[dict[str, str]] = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "http://testserver/adwatch",
}


# ---------------------------------------------------------------------------
# A traducao em si
# ---------------------------------------------------------------------------
def test_campo_repetido_o_ultimo_vence() -> None:
    """Idioma de caixa de selecao: hidden `false` antes, caixa `true` depois.

    Sem esta regra, desmarcar uma caixa nao mandaria nada e o valor anterior
    sobreviveria — o usuario desliga e continua ligado.
    """
    dados, _ = form_para_json(b"is_active=false&is_active=true")
    assert dados["is_active"] is True

    dados, _ = form_para_json(b"is_active=false")
    assert dados["is_active"] is False, "caixa desmarcada precisa chegar como False"


def test_campo_vazio_e_omitido_para_o_default_valer() -> None:
    """String vazia nao e um valor: e o usuario nao tendo digitado nada."""
    dados, _ = form_para_json(b"slug=x&description=")
    assert "description" not in dados
    assert dados["slug"] == "x"


def test_method_carrega_o_verbo_que_o_html_nao_tem() -> None:
    """HTML so tem GET e POST; o resto vem no campo oculto."""
    dados, metodo = form_para_json(b"_method=delete&id=abc")
    assert metodo == "DELETE"
    assert dados["id"] == "abc"
    assert "_method" not in dados, "o campo de controle nao pode virar dado"


# ---------------------------------------------------------------------------
# O caminho completo, como o navegador faz
# ---------------------------------------------------------------------------
async def test_registrar_midia_pelo_formulario_do_console(client: AsyncClient) -> None:
    """O clique exato que o usuario reportou: AdWatch > Registrar a midia."""
    resposta = await client.post(
        "/api/v1/adwatch/media",
        content="uri=%2Fdados%2Ftv.mp4&title=tv&kind=video",
        headers=CABECALHOS_DE_NAVEGADOR,
        follow_redirects=False,
    )

    assert resposta.status_code == 303, (
        f"esperava 303 de volta para a pagina, veio {resposta.status_code}: {resposta.text[:200]}"
    )
    assert resposta.headers["location"].startswith("/adwatch"), (
        "o redirecionamento precisa voltar para a pagina de onde o formulario veio"
    )

    catalogo = await client.get("/api/v1/adwatch/media")
    titulos = [item["title"] for item in catalogo.json()["items"]]
    assert "tv" in titulos, "o formulario respondeu bonito mas nao gravou nada"


async def test_cliente_de_api_continua_recebendo_json(client: AsyncClient) -> None:
    """A ponte e para o navegador; quem manda JSON nao pode ser redirecionado."""
    resposta = await client.post(
        "/api/v1/adwatch/media",
        json={"uri": "/dados/outro.mp4", "title": "outro", "kind": "video"},
        follow_redirects=False,
    )
    assert resposta.status_code == 201, resposta.text
    assert resposta.json()["title"] == "outro"


async def test_o_erro_de_esquema_que_o_usuario_viu_nao_volta(client: AsyncClient) -> None:
    """Trava a mensagem exata do defeito reportado."""
    resposta = await client.post(
        "/api/v1/adwatch/media",
        content="uri=%2Fdados%2Fx.mp4&title=x&kind=video",
        headers=CABECALHOS_DE_NAVEGADOR,
        follow_redirects=False,
    )
    corpo = resposta.text
    assert "valid dictionary or object" not in corpo, (
        "o corpo do formulario voltou a chegar como string na validacao"
    )
    assert "validation_error" not in corpo


# ---------------------------------------------------------------------------
# A classe inteira, nao o caso reportado
# ---------------------------------------------------------------------------
def test_todo_formulario_do_console_aponta_para_um_alvo_traduzivel() -> None:
    """Nenhum formulario pode escapar da ponte.

    Varre os templates de verdade: se alguem acrescentar um `method="post"` para
    fora de `/api/`, a ponte nao o alcanca e o botao volta a devolver JSON cru na
    cara do usuario. Trava a CLASSE, nao os 33 casos de hoje.
    """
    fora: list[str] = []
    total = 0
    for arquivo in sorted(TEMPLATES.rglob("*.html")):
        texto = arquivo.read_text()
        for achado in re.finditer(r"<form\b[^>]*>", texto, re.S):
            tag = " ".join(achado.group(0).split())
            if 'method="post"' not in tag.lower():
                continue
            total += 1
            acao = re.search(r'action="([^"]*)"', tag)
            destino = acao.group(1) if acao else ""
            # `{{ ... }}` no inicio e uma variavel do template que resolve para
            # /api/... em runtime; conferida pelos testes de pagina.
            if not destino.startswith(("/api/", "{{")):
                linha = texto[: achado.start()].count("\n") + 1
                fora.append(f"{arquivo.name}:{linha} -> {destino or '(sem action)'}")

    assert total >= 30, f"a varredura achou so {total} formularios; o regex quebrou?"
    assert not fora, "formulario POST fora do alcance da ponte console->API:\n  " + "\n  ".join(
        fora
    )
