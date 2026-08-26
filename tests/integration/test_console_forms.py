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
import urllib.parse
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
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


# ---------------------------------------------------------------------------
# Cada formulario contra o schema do endpoint que ele chama
# ---------------------------------------------------------------------------
# Endpoints que leem o corpo inteiro (RootModel ou arquivo enviado), e por isso
# nao tem propriedades nomeadas para comparar. Cada um com o motivo, para a
# isencao nao virar um lugar onde se esconde formulario quebrado.
ISENTOS: Final[dict[str, str]] = {
    "/api/v1/adwatch/media/{media_id}/transcript": "recebe o JSON inteiro (RootModel) por textarea ou upload; ver _import_payload",
    "/api/v1/adwatch/media/{media_id}/scenes": "idem transcript",
    "/api/v1/adwatch/media/{media_id}/ocr": "idem transcript",
    "/api/v1/adwatch/commercials/bulk": "aceita array puro ou {items, update_existing}; ver o hint do proprio formulario",
    "/api/v1/prompts/{prompt_id}/preview": "variables e dicionario de chave livre, resolvido por variables[<nome>]",
}


def _rota_do_openapi(app_openapi: dict, acao: str, metodo: str) -> tuple[str, dict] | None:
    """Casa a `action` do formulario com a rota do OpenAPI e devolve o schema."""
    concreto = re.sub(r"\{\{[^}]*\}\}", "X", acao)
    for rota, ops in app_openapi["paths"].items():
        if not re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", rota), concreto):
            continue
        op = ops.get(metodo.lower())
        corpo = (op or {}).get("requestBody", {}).get("content", {}).get("application/json")
        if not corpo:
            continue
        return rota, _resolver(corpo["schema"], app_openapi)
    return None


def _resolver(esquema: dict, documento: dict) -> dict:
    """Desembrulha `$ref` e corpo opcional (`anyOf: [Modelo, null]`).

    Sem isto, uma rota de corpo opcional aparece sem propriedade nenhuma e o
    teste acusa todo campo do formulario como invalido — foi o que aconteceu com
    `/detect`, que declara `anyOf: [DetectRequest, null]`.
    """
    if "$ref" in esquema:
        alvo: object = documento
        for parte in esquema["$ref"].lstrip("#/").split("/"):
            alvo = alvo[parte]  # type: ignore[index]
        return _resolver(dict(alvo), documento)  # type: ignore[arg-type]
    for combinador in ("anyOf", "oneOf", "allOf"):
        for ramo in esquema.get(combinador, []):
            if ramo.get("type") != "null":
                return _resolver(ramo, documento)
    return esquema
    return None


def _tipo_de(propriedade: dict, documento: dict) -> str | None:
    """Tipo efetivo da propriedade, atravessando `$ref` e `anyOf`."""
    if not propriedade:
        return None
    resolvida = _resolver(propriedade, documento)
    tipo = resolvida.get("type")
    return str(tipo) if tipo else None


PAGINAS: Final[tuple[str, ...]] = (
    "/",
    "/modules",
    "/prompts",
    "/guardrails",
    "/runs",
    "/knowledge",
    "/finops",
    "/observability",
    "/registry",
    "/adwatch",
    "/adwatch/commercials",
    "/adwatch/detections",
    "/identity",
    "/settings",
)


async def test_todo_campo_de_formulario_existe_no_schema_do_endpoint(
    client: AsyncClient, app: FastAPI
) -> None:
    """Nenhum formulario pode mandar um campo que o endpoint recusa.

    Confere o HTML RENDERIZADO, nao o arquivo de template. A primeira versao deste
    teste lia os templates e pulava todo `action="{{ form_action }}"` — que e
    justamente o formulario de cadastro de comercial, o unico que o usuario tinha
    clicado. O teste passava verde sobre o defeito que existia.

    Alem do nome, confere o TIPO: `keywords` existe em CommercialCreate e e
    `list[str]`; um input de texto sem `[]` manda a string inteira e o endpoint
    recusa com `list_type`. Nome certo e tipo errado foi o 422 que so apareceu
    quando alguem clicou no botao.

    Os nomes passam pela MESMA traducao do middleware (`form_para_json`), entao a
    convencao — `campo[]`, `pai.filho`, `campo[chave]`, `campo{}` — e verificada
    de verdade, e nao so escrita na documentacao.
    """
    openapi = app.openapi()
    problemas: list[str] = []
    conferidos: set[str] = set()

    for rota_pagina in PAGINAS:
        pagina = await client.get(rota_pagina)
        assert pagina.status_code == 200, f"{rota_pagina} respondeu {pagina.status_code}"
        texto = pagina.text

        for achado in re.finditer(r"<form\b[^>]*>(.*?)</form>", texto, re.S):
            cabeca = achado.group(0)[: achado.group(0).find(">") + 1]
            if 'method="post"' not in cabeca.lower():
                continue
            acao_m = re.search(r'action="([^"]*)"', cabeca)
            if not acao_m or not acao_m.group(1).startswith("/api/"):
                continue
            acao = acao_m.group(1)
            corpo = achado.group(1)
            nomes = set(re.findall(r'\bname="([^"]+)"', corpo))
            metodo_m = re.search(r'name="_method"\s+value="(\w+)"', corpo)
            metodo = metodo_m.group(1).upper() if metodo_m else "POST"
            nomes.discard("_method")
            if not nomes:
                continue

            casado = _rota_do_openapi(openapi, acao, metodo)
            if not casado:
                continue
            rota, esquema = casado
            if rota in ISENTOS:
                continue

            simulado = "&".join(
                f"{urllib.parse.quote(n)}=" + urllib.parse.quote("{}" if n.endswith("{}") else "x")
                for n in sorted(nomes)
            )
            traduzido, _ = form_para_json(simulado.encode())
            propriedades = esquema.get("properties", {})
            props = set(propriedades)
            conferidos.add(f"{metodo} {rota}")

            sobrando = set(traduzido) - props
            if sobrando:
                problemas.append(
                    f"{rota_pagina} ({metodo} {rota}) manda {sorted(sobrando)}, que o schema "
                    f"nao aceita. Campos validos: {sorted(props)[:8]}"
                )

            for campo, valor in traduzido.items():
                tipo = _tipo_de(propriedades.get(campo, {}), openapi)
                if tipo == "array" and not isinstance(valor, list):
                    problemas.append(
                        f"{rota_pagina} ({metodo} {rota}) manda '{campo}' como texto, mas o "
                        f"schema quer LISTA. Renomeie o input para '{campo}[]'."
                    )
                if tipo == "object" and not isinstance(valor, dict):
                    problemas.append(
                        f"{rota_pagina} ({metodo} {rota}) manda '{campo}' como texto, mas o "
                        f"schema quer OBJETO. Use '{campo}.<subcampo>', '{campo}[<chave>]' "
                        f"ou '{campo}{{}}' se a tela pedir o JSON inteiro."
                    )

    # Piso numerico e fragil: quantos formularios existem depende de quantas linhas
    # o seed criou. O que precisa estar coberto sao as rotas de escrita, nomeadas.
    # Sem isto o teste passa verde quando o casamento de rota para de funcionar.
    obrigatorias = {
        "POST /api/v1/adwatch/commercials",
        "POST /api/v1/modules",
        "POST /api/v1/identity/users",
        "POST /api/v1/guardrails",
        "POST /api/v1/prompts",
    }
    faltando = obrigatorias - conferidos
    assert not faltando, (
        f"estas rotas de escrita nao foram conferidas: {sorted(faltando)}. "
        f"Conferidas: {sorted(conferidos)}"
    )
    assert not problemas, "formulario incompativel com o endpoint:\n  " + "\n  ".join(problemas)
