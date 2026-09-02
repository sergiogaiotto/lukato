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

import json
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
ROTAS_DE_CONSOLE: Final[dict[str, str]] = {
    "/prompts/preview": (
        "pre-visualizacao renderiza um texto para ser LIDO nesta tela; postando em "
        "/api/ a ponte trocaria o 200 por um 303 e o resultado se perderia"
    ),
    "/guardrails/test": "testador de politica, mesmo motivo do preview de prompt",
}
"""POST que a ponte NAO deve traduzir — porque a rota ja devolve HTML.

A regra geral existe por um bom motivo: um `method="post"` fora de `/api/` fica
fora da ponte, e o botao volta a devolver JSON cru. Estas duas sao o caso
contrario: sao rotas de CONSOLE, que renderizam a pagina inteira com o resultado
calculado. Passar por `/api/` e que as quebrava.

A licenca nao e de graca: `test_rota_de_console_isenta_existe_e_devolve_html`
exige que cada entrada daqui seja uma rota de verdade que responde HTML.
"""


def test_todo_formulario_do_console_aponta_para_um_alvo_traduzivel() -> None:
    """Nenhum formulario pode escapar da ponte.

    Varre os templates de verdade: se alguem acrescentar um `method="post"` para
    fora de `/api/`, a ponte nao o alcanca e o botao volta a devolver JSON cru na
    cara do usuario. Trava a CLASSE, nao os 33 casos de hoje.
    """
    fora: list[str] = []
    total = 0
    for arquivo in sorted(TEMPLATES.rglob("*.html")):
        # encoding explicito: no Windows o padrao e cp1252, que nao decodifica
        # todos os bytes do UTF-8 dos templates (ex.: aspas tipograficas).
        texto = arquivo.read_text(encoding="utf-8")
        for achado in re.finditer(r"<form\b[^>]*>", texto, re.S):
            tag = " ".join(achado.group(0).split())
            if 'method="post"' not in tag.lower():
                continue
            total += 1
            acao = re.search(r'action="([^"]*)"', tag)
            destino = acao.group(1) if acao else ""
            # `{{ ... }}` no inicio e uma variavel do template que resolve para
            # /api/... em runtime; conferida pelos testes de pagina.
            if destino in ROTAS_DE_CONSOLE:
                continue
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
    "/api/v1/adwatch/media/{media_id}/transcript": (
        "form multipart: o endpoint le o corpo cru (`_import_payload`) em vez de um "
        "schema de campos, entao nao ha propriedades para conferir. Coberto por "
        "test_importar_transcricao_pelo_formulario_multipart, que prova o caminho."
    ),
}
"""Rotas que a conferencia campo-a-campo nao alcanca — e o motivo de cada uma.

Esta lista foi onde um defeito se escondeu: `/adwatch/commercials/bulk` estava
isento com a justificativa "aceita array puro ou {items, update_existing}", e o
formulario mandava `items` como texto plano. O endpoint recusava com `list_type`
e a importacao em lote simplesmente nao funcionava — com o teste verde por cima.

Duas travas agora impedem que isso se repita:

* `test_toda_isencao_ainda_corresponde_a_um_formulario` recusa entrada morta.
  `/scenes` e `/ocr` estavam aqui sem que o console tivesse formulario para elas;
  isencao que nao protege nada so ensina a confiar na lista.
* toda rota isenta precisa de um teste FUNCIONAL nomeado no proprio motivo. Uma
  isencao vale "o teste generico nao alcanca isto", nunca "isto nao e testado".
"""


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
        # Cru de proposito: quem desembrulha e `_propriedades`, que olha TODOS os
        # ramos da uniao. Resolver aqui escolheria o primeiro e perderia os outros.
        return rota, corpo["schema"]
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


def _deref(esquema: dict, documento: dict) -> dict:
    """Segue um `$ref` e para ai — sem tocar em `anyOf`/`oneOf`/`allOf`."""
    alvo: object = documento
    for parte in esquema["$ref"].lstrip("#/").split("/"):
        alvo = alvo[parte]  # type: ignore[index]
    return dict(alvo)  # type: ignore[arg-type]


def _ramos(esquema: dict, documento: dict) -> list[dict]:
    """Todos os ramos de um corpo em uniao, resolvidos, sem o ramo nulo.

    `_resolver` devolve o PRIMEIRO ramo, o que basta para `anyOf: [Modelo, null]`
    mas erra em `anyOf: [array, object]` — que e o corpo de
    `/adwatch/commercials/bulk`. Ele pegava o ramo `array`, que nao tem
    propriedade nenhuma, e o teste acusava TODO campo do formulario como
    invalido. Um formulario HTML so sabe mandar objeto, entao o que importa e o
    ramo objeto; olhar todos e a forma de nao depender da ordem em que o
    Pydantic escreveu a uniao.
    """
    if "$ref" in esquema:
        # So o `$ref`, nunca `_resolver`: ele tambem desembrulha `anyOf` e ja
        # teria escolhido um ramo, que e exatamente o que se quer evitar aqui.
        return _ramos(_deref(esquema, documento), documento)
    for combinador in ("anyOf", "oneOf"):
        if ramos := esquema.get(combinador):
            achados: list[dict] = []
            for ramo in ramos:
                if ramo.get("type") == "null":
                    continue
                achados.extend(_ramos(ramo, documento))
            return achados
    return [esquema]


def _propriedades(esquema: dict, documento: dict) -> dict:
    """Uniao das propriedades de todos os ramos objeto do corpo."""
    juntas: dict = {}
    for ramo in _ramos(esquema, documento):
        juntas.update(ramo.get("properties", {}))
    return juntas


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
    # Semeia UMA midia e UM comercial antes de varrer.
    #
    # Sem isto o banco da fixture nasce vazio e as paginas nao renderizam os
    # formularios que dependem de dado: importar transcricao, detectar, remover
    # uma linha — todos moram DENTRO da linha de um ativo. O teste varria as
    # telas e via so os formularios de cadastro, ficando cego para a metade do
    # console que so aparece depois que existe alguma coisa cadastrada.
    midia = await client.post(
        "/api/v1/adwatch/media",
        json={"uri": "/tmp/conformidade.mp4", "title": "conformidade", "kind": "video"},
    )
    assert midia.status_code in {200, 201}, midia.text
    comercial = await client.post(
        "/api/v1/adwatch/commercials",
        json={
            "commercial_id": "CONFORMIDADE_1",
            "brand": "Marca",
            "campaign": "conformidade",
            "text": "texto conhecido do comercial de conformidade",
        },
    )
    assert comercial.status_code in {200, 201}, comercial.text

    modulo = await client.post(
        "/api/v1/modules",
        json={
            "slug": "conformidade-modulo",
            "name": "Conformidade",
            "kind": "agent",
            "config": {"module": "processing"},
        },
    )
    assert modulo.status_code in {200, 201}, modulo.text
    prompt = await client.post(
        "/api/v1/prompts",
        json={"slug": "conformidade-prompt", "role": "system", "template": "Ola {{ marca }}."},
    )
    assert prompt.status_code in {200, 201}, prompt.text
    politica = await client.post(
        "/api/v1/guardrails",
        json={
            "slug": "conformidade-politica",
            "name": "Conformidade",
            "stage": "input",
            "rules": [{"id": "tamanho", "kind": "max_length", "config": {"max_chars": 100}}],
        },
    )
    assert politica.status_code in {200, 201}, politica.text

    # As telas de EDICAO e as de detalhe so existem com algo selecionado. Um terco
    # dos formularios de escrita do console mora nelas — `PUT /modules/{slug}` com
    # a trinca inteira, `PUT /guardrails/{id}` com as regras indexadas, o preview
    # de prompt, o invocar. Varrer so as listas deixava tudo isso invisivel, que e
    # a mesma cegueira do banco vazio, um nivel acima.
    paginas = [
        *PAGINAS,
        "/modules/conformidade-modulo",
        f"/prompts?sel={prompt.json()['id']}",
        f"/guardrails?sel={politica.json()['id']}",
        f"/adwatch/commercials?sel={comercial.json()['id']}",
        f"/adwatch/detections?sel={comercial.json()['id']}",
    ]

    openapi = app.openapi()
    problemas: list[str] = []
    conferidos: set[str] = set()
    rotas_com_formulario: set[str] = set()

    for rota_pagina in paginas:
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
            rotas_com_formulario.add(rota)
            if rota in ISENTOS:
                continue

            simulado = "&".join(
                f"{urllib.parse.quote(n)}=" + urllib.parse.quote("{}" if n.endswith("{}") else "x")
                for n in sorted(nomes)
            )
            traduzido, _ = form_para_json(simulado.encode())
            propriedades = _propriedades(esquema, openapi)
            props = set(propriedades)
            conferidos.add(f"{metodo} {rota}")

            sobrando = set(traduzido) - props
            if sobrando:
                problemas.append(
                    f"{rota_pagina} ({metodo} {rota}) manda {sorted(sobrando)}, que o schema "
                    f"nao aceita. Campos validos: {sorted(props)[:8]}"
                )

            # Campos `campo{}` carregam o JSON que o usuario digitar: o formato
            # e escolhido por quem preenche, e quem o valida com mensagem util e
            # o Pydantic. Aqui so se cobra que o schema espere ALGO estruturado —
            # um `{}` sobre um campo que o schema declara string continua sendo
            # defeito, e continua sendo pego.
            json_livre = {n[:-2] for n in nomes if n.endswith("{}")}

            for campo, valor in traduzido.items():
                tipo = _tipo_de(propriedades.get(campo, {}), openapi)
                if campo in json_livre:
                    if tipo not in {None, "array", "object"}:
                        problemas.append(
                            f"{rota_pagina} ({metodo} {rota}) manda '{campo}{{}}' como JSON, "
                            f"mas o schema declara '{tipo}'. Tire o '{{}}' do nome."
                        )
                    continue
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
        "POST /api/v1/adwatch/commercials/bulk",
        "POST /api/v1/modules",
        "POST /api/v1/identity/users",
        "POST /api/v1/guardrails",
        "POST /api/v1/prompts",
        # Edicao: `_method=put` muda o verbo, e o schema de update nao e o de
        # create. Sem estas linhas o teste podia passar cobrindo so o cadastro.
        "PUT /api/v1/prompts/{prompt_id}",
        "PUT /api/v1/guardrails/{policy_id}",
        "PUT /api/v1/modules/{slug}",
        "POST /api/v1/modules/{slug}/invoke",
    }
    mortas = set(ISENTOS) - rotas_com_formulario
    assert not mortas, (
        f"isencoes que nao correspondem a formulario nenhum: {sorted(mortas)}. "
        "Apague a entrada: uma isencao morta so ensina a confiar na lista."
    )

    faltando = obrigatorias - conferidos
    assert not faltando, (
        f"estas rotas de escrita nao foram conferidas: {sorted(faltando)}. "
        f"Conferidas: {sorted(conferidos)}"
    )
    assert not problemas, "formulario incompativel com o endpoint:\n  " + "\n  ".join(problemas)


# ---------------------------------------------------------------------------
# Os dois caminhos que a conferencia campo-a-campo nao alcanca
#
# Um formulario cujos nomes batem com o schema ainda pode nao gravar nada. Estes
# dois testes clicam de verdade: mandam o corpo do jeito que o navegador manda e
# depois conferem que a coisa apareceu no catalogo.
# ---------------------------------------------------------------------------
async def test_importar_lote_de_comerciais_pelo_formulario(client: AsyncClient) -> None:
    """A importacao em lote grava o que foi colado na textarea.

    Este e o defeito que a isencao escondia. O campo se chamava `items` e o
    endpoint espera uma LISTA; sem a marca `{}` no nome, o array colado chegava
    como string e voltava `422 list_type`. A importacao em lote — o caminho para
    subir um catalogo inteiro de uma vez — simplesmente nao funcionava, com a
    bateria verde por cima.
    """
    lote = json.dumps(
        [
            {
                "commercial_id": "LOTE_0001",
                "brand": "Marca",
                "campaign": "lote",
                "text": "primeiro comercial do lote de teste",
            },
            {
                "commercial_id": "LOTE_0002",
                "brand": "Marca",
                "campaign": "lote",
                "text": "segundo comercial do lote de teste",
            },
        ]
    )
    corpo = urllib.parse.urlencode({"items{}": lote, "update_existing": "false"})
    resposta = await client.post(
        "/api/v1/adwatch/commercials/bulk",
        content=corpo,
        headers={**CABECALHOS_DE_NAVEGADOR, "Referer": "http://testserver/adwatch/commercials"},
    )
    assert resposta.status_code == 303, f"o lote nao gravou: {resposta.text[:300]}"

    catalogo = await client.get("/api/v1/adwatch/commercials?q=LOTE_")
    codigos = {item["commercial_id"] for item in catalogo.json()["items"]}
    assert {"LOTE_0001", "LOTE_0002"} <= codigos, f"o catalogo tem {sorted(codigos)}"


async def test_importar_transcricao_pelo_formulario_multipart(client: AsyncClient) -> None:
    """A unica rota isenta da conferencia, provada pelo caminho que o console usa.

    O formulario de "importar colado" e `multipart/form-data`, e o endpoint le o
    corpo cru em vez de um schema de campos — por isso a conferencia campo-a-campo
    nao tem o que conferir la. O que ela nao alcanca, este teste alcanca: cola a
    transcricao e confere que as palavras ficaram gravadas na midia.
    """
    midia = await client.post(
        "/api/v1/adwatch/media",
        json={"uri": "/tmp/transcricao.mp4", "title": "transcricao", "kind": "video"},
    )
    assert midia.status_code in {200, 201}, midia.text
    media_id = midia.json()["id"]

    palavras = [
        {"word": "chegou", "start": 10.0, "end": 10.4},
        {"word": "a", "start": 10.4, "end": 10.5},
        {"word": "promocao", "start": 10.5, "end": 11.0},
    ]
    resposta = await client.post(
        f"/api/v1/adwatch/media/{media_id}/transcript",
        files={"payload": (None, json.dumps(palavras))},
        headers={"Accept": "text/html", "Referer": "http://testserver/adwatch"},
    )
    assert resposta.status_code == 303, f"a transcricao nao entrou: {resposta.text[:300]}"

    detalhe = await client.get(f"/api/v1/adwatch/media/{media_id}")
    artefatos = detalhe.json()["artifacts"]
    assert artefatos["transcript"] is True
    assert artefatos["transcript_words"] == len(palavras)
    assert artefatos["transcript_source"] == "import"


async def test_editar_prompt_e_politica_pelo_formulario(client: AsyncClient) -> None:
    """Editar tem que gravar — o caminho que a navegacao gravada nao exercitou.

    Os dois editores mostram o slug num input `readonly`, e navegador ENVIA campo
    readonly (so `disabled` fica de fora). `PromptUpdate` e `PolicyUpdate` tem
    `extra="forbid"`, entao todo Salvar em cima de um item existente voltava
    `422 extra_forbidden` por causa de um campo que o usuario nem tocava.

    Cadastrar funcionava e editar nao: a diferenca so aparece com algo ja
    cadastrado na tela, que e o estado em que o console passa a maior parte do
    tempo.
    """
    criado = await client.post(
        "/api/v1/prompts",
        json={"slug": "editavel", "name": "Antes", "role": "system", "template": "v1"},
    )
    assert criado.status_code in {200, 201}, criado.text
    prompt_id = criado.json()["id"]

    corpo = urllib.parse.urlencode(
        {
            "_method": "put",
            "name": "Depois",
            "role": "system",
            "template": "v2 do template",
            "labels[]": "editado",
        }
    )
    salvo = await client.post(
        f"/api/v1/prompts/{prompt_id}",
        content=corpo,
        headers={**CABECALHOS_DE_NAVEGADOR, "Referer": "http://testserver/prompts"},
    )
    assert salvo.status_code == 303, f"editar o prompt falhou: {salvo.text[:300]}"

    biblioteca = await client.get("/api/v1/prompts?q=editavel")
    versoes = {(i["version"], i["name"]) for i in biblioteca.json()["items"]}
    assert (2, "Depois") in versoes, f"a nova versao nao apareceu: {sorted(versoes)}"
    assert (1, "Antes") in versoes, "a versao anterior tem que continuar auditavel"

    politica = await client.post(
        "/api/v1/guardrails",
        json={
            "slug": "editavel-politica",
            "name": "Antes",
            "stage": "input",
            "rules": [{"id": "tamanho", "kind": "max_length", "config": {"max_chars": 100}}],
        },
    )
    assert politica.status_code in {200, 201}, politica.text

    corpo = urllib.parse.urlencode(
        {
            "_method": "put",
            "name": "Depois",
            "stage": "input",
            "rules[0].id": "tamanho",
            "rules[0].kind": "max_length",
            "rules[0].action": "block",
            "rules[0].config{}": json.dumps({"max_chars": 250}),
        }
    )
    salvo = await client.post(
        f"/api/v1/guardrails/{politica.json()['id']}",
        content=corpo,
        headers={**CABECALHOS_DE_NAVEGADOR, "Referer": "http://testserver/guardrails"},
    )
    assert salvo.status_code == 303, f"editar a politica falhou: {salvo.text[:300]}"

    lista = await client.get("/api/v1/guardrails?q=editavel-politica")
    atual = lista.json()["items"][0]
    assert atual["name"] == "Depois"
    assert atual["rules"][0]["config"] == {"max_chars": 250}, (
        "a regra editada nao gravou o config novo: " + str(atual["rules"])
    )


def _rotas_registradas(app: FastAPI) -> set[tuple[str, str]]:
    """`(caminho, verbo)` de tudo que o app resolve, descendo nos sub-routers."""
    encontradas: set[tuple[str, str]] = set()

    def descer(rotas: object) -> None:
        for rota in rotas or ():  # type: ignore[union-attr]
            caminho = getattr(rota, "path", None)
            for verbo in getattr(rota, "methods", None) or ():
                if caminho:
                    encontradas.add((str(caminho), str(verbo)))
            descer(getattr(rota, "routes", None))
            # O FastAPI embrulha `include_router` num `_IncludedRouter`, que nao
            # expoe `routes` — as rotas de verdade ficam em `original_router`.
            embrulhado = getattr(rota, "original_router", None)
            if embrulhado is not None:
                descer(getattr(embrulhado, "routes", None))

    descer(app.routes)
    return encontradas


def test_rota_de_console_isenta_existe_de_verdade(app: FastAPI) -> None:
    """Cada entrada de `ROTAS_DE_CONSOLE` precisa ser uma rota POST registrada.

    Sem esta trava, acrescentar um caminho aquele dicionario viraria uma forma de
    silenciar o teste da classe: bastaria escrever a rota la e o formulario
    passaria a apontar para lugar nenhum, com verde por cima.

    Que elas devolvem HTML com o resultado calculado esta provado em
    `test_preview_de_prompt_devolve_o_texto_renderizado` e
    `test_teste_de_guardrail_devolve_o_veredito_na_tela`. Aqui so se confere a
    existencia — chamar a rota com corpo vazio produziria um 404 legitimo do caso
    de uso, que nao diz nada sobre o registro.
    """
    registradas = _rotas_registradas(app)
    for rota, motivo in ROTAS_DE_CONSOLE.items():
        assert (rota, "POST") in registradas, (
            f"{rota} esta isenta da ponte (motivo: {motivo}) mas nao existe como rota POST"
        )
