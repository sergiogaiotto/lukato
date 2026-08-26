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
import urllib.parse
from pathlib import Path
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


# ---------------------------------------------------------------------------
# POST que calcula para exibir, e POST que grava
# ---------------------------------------------------------------------------
async def test_preview_de_prompt_devolve_o_texto_renderizado(client: AsyncClient) -> None:
    """Pre-visualizar tem que MOSTRAR o texto, nao redirecionar para longe dele.

    O formulario postava em `/api/v1/prompts/{id}/preview` e a ponte trocava o
    200 por um 303 de volta para a pagina — certo para quem gravou, e aqui
    jogando fora exatamente o que o usuario pediu para ver. Clicar em
    "Pre-visualizar" devolvia a mesma tela, sem resultado e sem erro.
    """
    criado = await client.post(
        "/api/v1/prompts",
        json={
            "slug": "com-variaveis",
            "role": "system",
            "template": "Voce atende a marca {{ marca }} na campanha {{ campanha }}.",
        },
    )
    assert criado.status_code in {200, 201}, criado.text
    prompt_id = criado.json()["id"]

    # Enche a biblioteca ate o prompt alvo sair da PRIMEIRA pagina. A lista
    # ordena por slug e pagina em 25; o bloco do resultado mora dentro de
    # `{% if prompt_atual %}`, e `prompt_atual` sai da lista da pagina. Sem esta
    # massa o teste passa com um unico prompt cadastrado e nao ve o defeito que
    # aparece em qualquer instalacao com biblioteca de verdade.
    for i in range(30):
        enchimento = await client.post(
            "/api/v1/prompts",
            json={"slug": f"aa-enche-{i:02d}", "role": "system", "template": "x"},
        )
        assert enchimento.status_code in {200, 201}, enchimento.text

    corpo = urllib.parse.urlencode(
        {
            "prompt": prompt_id,
            "sel": prompt_id,
            "variables[marca]": "VIVO",
            "variables[campanha]": "fibra-300",
        }
    )
    resposta = await client.post(
        "/prompts/preview",
        content=corpo,
        headers={**CABECALHOS_DE_NAVEGADOR, "Referer": "http://testserver/prompts"},
    )
    assert resposta.status_code == 200, resposta.text[:300]
    assert resposta.headers["content-type"].startswith("text/html")
    assert "VIVO" in resposta.text, "o texto renderizado nao trouxe a variavel substituida"
    assert "fibra-300" in resposta.text


async def test_teste_de_guardrail_devolve_o_veredito_na_tela(client: AsyncClient) -> None:
    """Testar uma politica tem que mostrar o veredito, pelo mesmo motivo."""
    await client.post(
        "/api/v1/guardrails",
        json={
            "slug": "testavel",
            "name": "Testavel",
            "stage": "output",
            "rules": [
                {
                    "id": "pii",
                    "kind": "pii_redact",
                    "action": "redact",
                    "config": {"types": ["cpf"]},
                }
            ],
        },
    )
    corpo = urllib.parse.urlencode(
        {
            "policy": "testavel",
            "stage": "output",
            "content": "O cliente 529.982.247-25 pediu retorno.",
        }
    )
    resposta = await client.post(
        "/guardrails/test",
        content=corpo,
        headers={**CABECALHOS_DE_NAVEGADOR, "Referer": "http://testserver/guardrails"},
    )
    assert resposta.status_code == 200, resposta.text[:300]
    assert resposta.headers["content-type"].startswith("text/html")
    assert "529.982.247-25" not in resposta.text, "o CPF apareceu sem redigir no veredito"


async def test_salvar_leva_a_pagina_ao_item_recem_gravado(client: AsyncClient) -> None:
    """O 303 carrega `sel=<id>` do que acabou de ser criado.

    A lista de prompts ordena por slug e pagina em 25. Passando de 25 itens, o
    que o usuario acabou de criar caia na segunda pagina: a tela dizia "salvo" e
    nao mostrava o que foi salvo.
    """
    corpo = urllib.parse.urlencode(
        {"slug": "aparece-depois-de-salvar", "role": "system", "template": "oi"}
    )
    resposta = await client.post(
        "/api/v1/prompts",
        content=corpo,
        headers={**CABECALHOS_DE_NAVEGADOR, "Referer": "http://testserver/prompts"},
    )
    assert resposta.status_code == 303
    destino = resposta.headers["location"]
    assert "sel=" in destino, f"o redirecionamento nao realca o item gravado: {destino}"

    pagina = await client.get(destino)
    assert pagina.status_code == 200
    assert "aparece-depois-de-salvar" in pagina.text


def test_controle_dentro_da_linha_nao_e_engolido_pelo_painel() -> None:
    """Trava o conserto do `context.js` que matava toda acao de linha.

    O ouvinte de clique do painel de contexto pegava `closest("[data-context-id]")`
    e chamava `preventDefault()`. Como a `<tr>` carrega esse atributo, o botao de
    QUALQUER formulario dentro de uma linha nunca submetia: aceitar e rejeitar
    deteccao, remover comercial, remover usuario, rotacionar e revogar chave,
    remover orcamento. O painel abria e nada era gravado, sem erro nenhum.

    O exercicio de verdade e o navegador (`scripts/navegacao_fim_a_fim.py`); aqui
    fica a trava barata contra a linha do guarda sumir num refactor.
    """
    origem = (
        Path(__file__).resolve().parents[2] / "src/lukato/interfaces/ui/static/js/context.js"
    ).read_text(encoding="utf-8")
    assert 'closest("button, input, select, textarea, form")' in origem, (
        "o guarda que deixa o controle da linha agir sumiu do context.js"
    )


async def test_invocar_modulo_mostra_a_resposta_na_tela(client: AsyncClient) -> None:
    """Executar um modulo grava a execucao E devolve a resposta para ser lida.

    Terceira ocorrencia da mesma classe do preview de prompt e do testador de
    politica: o 303 da ponte descartava o que o usuario pediu para ver. Aqui,
    porem, a invocacao GRAVA — a resposta ja esta persistida na execucao. Entao
    o conserto nao e virar rota de console: e o 303 levar o `run_id` e a pagina
    buscar o texto de volta, de modo que o F5 nao reinvoque nem gaste token.
    """
    criado = await client.post(
        "/api/v1/modules",
        json={
            "slug": "modulo-que-responde",
            "name": "Modulo que responde",
            "kind": "agent",
            # `active` de proposito: um modulo nasce em `draft` e invocar draft e
            # `409` — comportamento correto, e nao o que este teste mede.
            "status": "active",
            "config": {"module": "processing"},
        },
    )
    assert criado.status_code in {200, 201}, criado.text

    corpo = urllib.parse.urlencode({"input": "Explique a cobranca de julho.", "variables{}": "{}"})
    resposta = await client.post(
        "/api/v1/modules/modulo-que-responde/invoke",
        content=corpo,
        headers={
            **CABECALHOS_DE_NAVEGADOR,
            "Referer": "http://testserver/modules/modulo-que-responde",
        },
    )
    assert resposta.status_code == 303, resposta.text[:300]
    destino = resposta.headers["location"]
    assert "sel=" in destino, f"o 303 nao levou a execucao criada: {destino}"

    pagina = await client.get(destino)
    assert pagina.status_code == 200
    assert "Nenhum resultado nesta sessão" not in pagina.text, (
        "a execucao gravou mas a tela continua dizendo que nao ha resultado"
    )
    assert "Explique a cobranca de julho." in pagina.text


async def test_adwatch_diz_quando_o_aceite_automatico_e_inalcancavel(
    client: AsyncClient,
) -> None:
    """A tela precisa dizer a CONSEQUENCIA do que falta, nao so o teto isolado.

    `max_score_without` respondia "quanto se perde sem OCR?" e "sem juiz
    visual?" — hipoteses. Nenhuma respondia o que decide o funil: com esta
    maquina, ate onde uma deteccao chega? Sem OCR o teto e 85%, abaixo do limiar
    de aceite de 90%: nenhuma deteccao e aceita sozinha e todas param em revisao
    humana. Medido numa instalacao real: 81 deteccoes, maior confianca 84,5%,
    zero acima de 90%. A tela mostrava "score maximo 85,0%" e parava ai.
    """
    capacidades = await client.get("/api/v1/adwatch/capabilities")
    assert capacidades.status_code == 200
    dados = capacidades.json()
    teto = dados["max_score_effective"]
    aceite = dados["thresholds"]["accept"]

    esperado = 1.0 if dados["capabilities"]["ocr"] else 1.0 - dados["weights"]["ocr"]
    assert abs(teto - esperado) < 1e-6, (
        f"o teto efetivo ({teto}) nao reflete as capacidades instaladas ({esperado})"
    )

    pagina = await client.get("/adwatch")
    assert pagina.status_code == 200
    if teto < aceite:
        assert "Nenhuma detecção será aceita" in pagina.text, (
            "o teto nao alcanca o limiar de aceite e a tela nao avisa"
        )
    else:
        assert "é alcançável" in pagina.text
