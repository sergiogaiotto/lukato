"""Console web servido inteiro pela aplicacao real (SPEC-0009, criterios de aceite).

O que este arquivo prova, subindo a `FastAPI` de verdade sobre SQLite em memoria:

* **as dezesseis rotas de pagina da secao 8 respondem `200`** e cada uma traz o
  `<title>` que a SPEC nomeia (criterio 3);
* **a moldura existe**: os cinco landmarks (`header`/`nav`/`main`/`aside`/`footer`)
  mais topbar, sidebar, barra de status e painel de contexto (criterio 1);
* **rede fechada**: nenhuma pagina renderizada e nenhum arquivo de `static/`
  carrega recurso de host externo (criterio 5). A varredura e dupla — o texto
  cru dos arquivos e o HTML servido — e o que sobra e conferido posicao a
  posicao, para separar *referencia* (o navegador busca) de *valor* (o operador
  le);
* **o painel de contexto funciona sem JavaScript**: `?sel=<id>` resolve no
  servidor e `GET /ui/context/{entidade}/{id}` devolve o mesmo miolo como
  fragmento (criterio 4);
* **segredo nao vaza**: com uma `api_key` configurada, `/settings` mostra apenas
  a forma mascarada (secao 10);
* **autoescape ligado**: um modulo chamado `<script>alert(1)</script>` aparece
  escapado, nunca como tag executavel (secao 10);
* **erro vira pagina**, nao JSON, tanto para um identificador que nao existe
  quanto para um caminho que rota nenhuma reclama — sem quebrar o cliente de
  API, que continua recebendo o envelope `{"error": {...}}`;
* **todo formulario de mutacao e `POST` com `action` preenchido**, verificado com
  o `html.parser` da biblioteca padrao — sem dependencia nova.

Tudo roda offline: `EchoLLM`, `HashingEmbedder`, `NoopTracer` e SQLite em
memoria, exatamente como manda a SPEC-0000 secao 14.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.application.container import Container
from lukato.domain.models.module import ModuleKind, ModuleStatus
from lukato.domain.models.run import RunStatus, StepKind, TokenUsage
from lukato.domain.types import Id
from lukato.interfaces.ui.context import NOT_CONFIGURED, mask_secret
from lukato.interfaces.ui.router import STATIC_DIR, TEMPLATES_DIR
from tests.conftest import SeedIds
from tests.factories import make_module, make_run, make_step

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# O mapa normativo de rotas (SPEC-0009 secao 8)
# --------------------------------------------------------------------------- #
ROTAS_ESTATICAS: Final[tuple[tuple[str, str], ...]] = (
    ("/", "Cockpit · lukato"),
    ("/modules", "Módulos · lukato"),
    ("/prompts", "Prompts · lukato"),
    ("/guardrails", "Guardrails · lukato"),
    ("/runs", "Execuções · lukato"),
    ("/knowledge", "Conhecimento · lukato"),
    ("/finops", "FinOps · lukato"),
    ("/observability", "Observabilidade · lukato"),
    ("/registry", "Registry · lukato"),
    ("/adwatch", "AdWatch · lukato"),
    ("/adwatch/commercials", "Comerciais · lukato"),
    ("/adwatch/detections", "Detecções · lukato"),
    ("/identity", "Identidade · lukato"),
    ("/settings", "Configurações · lukato"),
)
"""Rotas de pagina sem parametro, com o `<title>` exigido pela SPEC-0009 secao 8."""

ROTAS_PARAMETRIZADAS: Final[int] = 2
"""`/modules/{slug}` e `/runs/{run_id}`, cobertas com dados semeados."""

MINIMO_DE_ROTAS: Final[int] = 15
"""Piso do enunciado; a SPEC lista dezesseis linhas de pagina na secao 8."""

LANDMARKS: Final[tuple[tuple[str, str], ...]] = (
    ("<header", "cabecalho (topbar)"),
    ("<nav", "navegacao (sidebar)"),
    ("<main", "conteudo principal"),
    ("<aside", "painel de contexto"),
    ("<footer", "barra de status"),
)
"""Os cinco landmarks de acessibilidade exigidos pela SPEC-0009 secao 1."""

MARCADORES_DE_MOLDURA: Final[tuple[tuple[str, str], ...]] = (
    ('class="lk-topbar"', "topbar"),
    ('id="lk-sidebar"', "sidebar"),
    ('id="lk-main"', "miolo"),
    ('id="lk-aside"', "gaveta do painel de contexto"),
    ('id="lk-context-body"', "corpo do painel de contexto"),
    ('class="lk-statusbar"', "barra de status"),
)
"""Ganchos estruturais que a SPEC-0009 secao 3 exige na moldura de toda pagina."""

CHAVE_DE_TESTE: Final[str] = "sk-lukato-chave-secreta-de-teste-9042"
"""Segredo plantado em `Settings` para provar que a tela nunca o imprime inteiro."""

NOME_MALICIOSO: Final[str] = "<script>alert(1)</script>"
"""Nome de modulo usado para provar que o autoescape do Jinja esta ligado."""

ATRIBUTOS_QUE_CARREGAM: Final[frozenset[str]] = frozenset(
    {
        "src",
        "srcset",
        "href",
        "action",
        "formaction",
        "poster",
        "data",
        "background",
        "manifest",
        "cite",
    }
)
"""Atributos cujo valor o navegador busca ou navega — uma URL aqui e referencia."""

_URL_EXTERNA = re.compile(r"https?://[^\s\"'<>()]+")
"""Qualquer URL absoluta com esquema HTTP."""

_NAMESPACE_XML: Final[str] = "http://www.w3.org/"
"""Prefixo dos namespaces XML/SVG.

`xmlns="http://www.w3.org/2000/svg"` e exigido pelo formato SVG e **nao** e uma
busca de rede: nenhum agente de usuario resolve um namespace. E a unica excecao
da varredura, e ela e reconhecida pelo prefixo, nao por arquivo.
"""

_COMENTARIOS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"<!--.*?-->", re.DOTALL),  # HTML/SVG
    re.compile(r"\{#.*?#\}", re.DOTALL),  # Jinja
    re.compile(r"/\*.*?\*/", re.DOTALL),  # CSS e JS em bloco
    re.compile(r"(?m)^\s*//.*$"),  # JS de linha
)
"""Formas de comentario descartadas antes da varredura (SPEC-0009 criterio 5)."""

_EXTENSOES_DE_TEXTO: Final[frozenset[str]] = frozenset(
    {".css", ".js", ".svg", ".html", ".json", ".txt", ".map"}
)
"""Extensoes varridas em `static/` e `templates/`; binarios sao ignorados."""


# --------------------------------------------------------------------------- #
# Ferramentas de inspecao do HTML (somente biblioteca padrao)
# --------------------------------------------------------------------------- #
class ColetorDeHtml(HTMLParser):
    """Extrai do HTML o que os testes precisam medir, sem dependencia nova.

    Guarda os formularios com os seus atributos, os pares
    `(atributo, valor)` de todo atributo que faz o navegador buscar algo, o
    conteudo dos blocos `<style>` somado aos atributos `style=` e o texto do
    `<title>`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.formularios: list[dict[str, str]] = []
        self.recursos: list[tuple[str, str]] = []
        self.estilos: list[str] = []
        self.titulo: str = ""
        self._dentro_do_titulo = False
        self._dentro_do_estilo = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Registra recursos, formularios e a abertura de `<title>`/`<style>`."""
        mapa = {nome: (valor or "") for nome, valor in attrs}
        if tag == "form":
            self.formularios.append(mapa)
        if tag == "title":
            self._dentro_do_titulo = True
        if tag == "style":
            self._dentro_do_estilo = True
        for nome, valor in mapa.items():
            if nome in ATRIBUTOS_QUE_CARREGAM and valor:
                self.recursos.append((nome, valor))
            if nome == "style" and valor:
                self.estilos.append(valor)

    def handle_endtag(self, tag: str) -> None:
        """Fecha o rastreio de `<title>` e `<style>`."""
        if tag == "title":
            self._dentro_do_titulo = False
        if tag == "style":
            self._dentro_do_estilo = False

    def handle_data(self, data: str) -> None:
        """Acumula o texto do titulo e o CSS embutido."""
        if self._dentro_do_titulo:
            self.titulo += data
        if self._dentro_do_estilo:
            self.estilos.append(data)


def analisar(html: str) -> ColetorDeHtml:
    """Roda o `html.parser` da stdlib sobre o documento e devolve o coletor."""
    coletor = ColetorDeHtml()
    coletor.feed(html)
    coletor.close()
    return coletor


def sem_comentarios(texto: str) -> str:
    """Remove comentarios HTML, Jinja, CSS e JS antes de varrer o conteudo."""
    limpo = texto
    for padrao in _COMENTARIOS:
        limpo = padrao.sub(" ", limpo)
    return limpo


def urls_externas(texto: str) -> list[str]:
    """URLs absolutas do texto, ja descontados os namespaces XML."""
    return [url for url in _URL_EXTERNA.findall(texto) if not url.startswith(_NAMESPACE_XML)]


def arquivos_de_texto(raiz: Path) -> Iterator[Path]:
    """Percorre a arvore devolvendo apenas os arquivos textuais conhecidos."""
    for caminho in sorted(raiz.rglob("*")):
        if caminho.is_file() and caminho.suffix.lower() in _EXTENSOES_DE_TEXTO:
            yield caminho


def referencias_externas(html: str) -> list[str]:
    """URLs externas em posicao de carregamento: `src`, `href`, `url(...)`, `@import`.

    Uma URL que aparece como texto — o endpoint do provedor impresso na tela de
    configuracoes, por exemplo — nao entra: o navegador nao a busca. O que esta
    funcao procura e o que abriria conexao com um host de fora em rede fechada.
    """
    coletor = analisar(html)
    achados: list[str] = []
    for atributo, valor in coletor.recursos:
        achados.extend(f"{atributo}={url}" for url in urls_externas(valor))
    for css in coletor.estilos:
        achados.extend(f"css={url}" for url in urls_externas(css))
    return achados


# --------------------------------------------------------------------------- #
# Fixtures locais
# --------------------------------------------------------------------------- #
@pytest.fixture
async def console(
    client: AsyncClient, seeded: SeedIds, uow_factory: UnitOfWorkFactoryImpl
) -> tuple[AsyncClient, dict[str, Id]]:
    """Cliente do console com o minimo semeado para cobrir as rotas com parametro.

    Alem do seed padrao (politicas, prompts e dois modulos), acrescenta uma
    execucao com trilha — sem ela `/runs/{run_id}` nao teria o que renderizar — e
    um modulo cujo **nome e um `<script>`**, que e o corpo de prova do autoescape.
    """
    execucao = make_run(
        module_slug="assistente",
        module_id=seeded.module_id,
        status=RunStatus.SUCCEEDED,
        usage=TokenUsage.of(120, 40),
        cost_usd=0.2,
        latency_ms=12.5,
    )
    async with uow_factory() as unidade:
        gravada = await unidade.runs.add(execucao)
        for indice, tipo in enumerate(
            (StepKind.GUARDRAIL_IN, StepKind.PROMPT, StepKind.LLM, StepKind.GUARDRAIL_OUT)
        ):
            await unidade.runs.add_step(
                make_step(gravada.id, index=indice, kind=tipo, name=tipo.value)
            )
        malicioso = await unidade.modules.add(
            make_module(
                slug="modulo-com-script",
                name=NOME_MALICIOSO,
                kind=ModuleKind.AGENT,
                status=ModuleStatus.DRAFT,
                runtime="direct",
                config={"module": "processing"},
            )
        )
        await unidade.commit()
    return client, {
        "modulo": seeded.module_id,
        "modulo_malicioso": malicioso.id,
        "execucao": gravada.id,
    }


@pytest.fixture
async def paginas(console: tuple[AsyncClient, dict[str, Id]]) -> dict[str, str]:
    """Renderiza **todas** as rotas de pagina uma vez e devolve o HTML de cada uma."""
    http, ids = console
    rotas = [rota for rota, _ in ROTAS_ESTATICAS]
    rotas.append("/modules/assistente")
    rotas.append(f"/runs/{ids['execucao']}")
    renderizadas: dict[str, str] = {}
    for rota in rotas:
        resposta = await http.get(rota)
        assert resposta.status_code == 200, f"{rota} respondeu {resposta.status_code}"
        renderizadas[rota] = resposta.text
    return renderizadas


# --------------------------------------------------------------------------- #
# Criterio 3 — toda rota de pagina responde 200 com o titulo certo
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("rota", "titulo"), ROTAS_ESTATICAS)
async def test_rota_de_pagina_responde_200_com_o_titulo_da_spec(
    console: tuple[AsyncClient, dict[str, Id]], rota: str, titulo: str
) -> None:
    """Cada rota da secao 8 devolve HTML com o `<title>` normativo."""
    http, _ = console
    resposta = await http.get(rota)

    assert resposta.status_code == 200, f"{rota} devolveu {resposta.status_code}"
    assert resposta.headers["content-type"].startswith("text/html"), (
        f"{rota} nao devolveu HTML: {resposta.headers['content-type']}"
    )
    assert analisar(resposta.text).titulo.strip() == titulo


async def test_rota_de_modulo_por_slug_responde_200_com_o_titulo_do_modulo(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`/modules/{slug}` e a tela de operacao e nomeia o modulo no titulo."""
    http, _ = console
    resposta = await http.get("/modules/assistente")

    assert resposta.status_code == 200
    assert analisar(resposta.text).titulo.strip() == "Módulo assistente · lukato"


async def test_rota_de_execucao_por_id_responde_200_com_o_titulo_da_execucao(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`/runs/{run_id}` renderiza a trilha de uma execucao existente."""
    http, ids = console
    resposta = await http.get(f"/runs/{ids['execucao']}")

    assert resposta.status_code == 200
    assert analisar(resposta.text).titulo.strip() == f"Execução {ids['execucao']} · lukato"


async def test_o_mapa_de_rotas_cobre_todas_as_paginas_da_spec(paginas: dict[str, str]) -> None:
    """A suite exercita as dezesseis paginas da secao 8, nunca menos que quinze."""
    assert len(paginas) == len(ROTAS_ESTATICAS) + ROTAS_PARAMETRIZADAS
    assert len(paginas) >= MINIMO_DE_ROTAS, (
        f"apenas {len(paginas)} rotas de pagina foram exercitadas: {sorted(paginas)}"
    )


# --------------------------------------------------------------------------- #
# Criterio 1 — a moldura de tres colunas
# --------------------------------------------------------------------------- #
async def test_cockpit_traz_os_cinco_landmarks_de_acessibilidade(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`GET /` entrega `header`, `nav`, `main`, `aside` e `footer`."""
    http, _ = console
    html = (await http.get("/")).text

    faltando = [descricao for marca, descricao in LANDMARKS if marca not in html]
    assert not faltando, f"landmarks ausentes no cockpit: {faltando}"


async def test_cockpit_traz_topbar_sidebar_statusbar_e_painel_de_contexto(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """A moldura da secao 3 esta inteira: topbar, sidebar, statusbar e aside."""
    http, _ = console
    html = (await http.get("/")).text

    faltando = [descricao for marca, descricao in MARCADORES_DE_MOLDURA if marca not in html]
    assert not faltando, f"elementos de moldura ausentes no cockpit: {faltando}"


async def test_toda_pagina_repete_a_moldura_completa(paginas: dict[str, str]) -> None:
    """Nenhuma tela abre mao da moldura — o painel de contexto e *sempre* renderizado."""
    quebradas = {
        rota: [descricao for marca, descricao in MARCADORES_DE_MOLDURA if marca not in html]
        for rota, html in paginas.items()
    }
    incompletas = {rota: falta for rota, falta in quebradas.items() if falta}
    assert not incompletas, f"paginas sem a moldura completa: {incompletas}"


# --------------------------------------------------------------------------- #
# Criterio 5 — rede fechada: nenhuma referencia externa
# --------------------------------------------------------------------------- #
def test_nenhum_arquivo_estatico_referencia_host_externo() -> None:
    """Varre `static/` inteiro: CSS, JS e SVG nao podem citar host de fora."""
    ocorrencias: list[str] = []
    for caminho in arquivos_de_texto(STATIC_DIR):
        conteudo = sem_comentarios(caminho.read_text(encoding="utf-8"))
        ocorrencias.extend(
            f"{caminho.relative_to(STATIC_DIR)}: {url}" for url in urls_externas(conteudo)
        )

    assert not ocorrencias, "referencias externas em static/:\n" + "\n".join(ocorrencias)


def test_nenhum_template_referencia_host_externo() -> None:
    """Mesma varredura sobre `templates/`, fora de comentarios (criterio 5)."""
    ocorrencias: list[str] = []
    for caminho in arquivos_de_texto(TEMPLATES_DIR):
        conteudo = sem_comentarios(caminho.read_text(encoding="utf-8"))
        ocorrencias.extend(
            f"{caminho.relative_to(TEMPLATES_DIR)}: {url}" for url in urls_externas(conteudo)
        )

    assert not ocorrencias, "referencias externas em templates/:\n" + "\n".join(ocorrencias)


async def test_nenhuma_pagina_renderizada_carrega_recurso_de_host_externo(
    paginas: dict[str, str],
) -> None:
    """No HTML servido, nenhum `src`, `href`, `url(...)` ou `@import` sai da origem."""
    ocorrencias = [
        f"{rota}: {achado}"
        for rota, html in paginas.items()
        for achado in referencias_externas(html)
    ]

    assert not ocorrencias, "recursos externos no HTML renderizado:\n" + "\n".join(ocorrencias)


async def test_as_unicas_urls_externas_do_html_sao_endpoints_configurados(
    paginas: dict[str, str], container: Container
) -> None:
    """O que sobra de `http(s)://` no HTML e valor de configuracao, nunca referencia.

    O console imprime os endpoints do provedor de LLM, do provedor de embeddings
    e do Langfuse nas telas de cockpit, observabilidade e configuracoes — a
    SPEC-0009 secao 8 pede exatamente isso. Sao textos inertes: o navegador nao
    busca nenhum deles (o teste anterior garante a posicao). Este teste fecha o
    cerco pelo outro lado: **qualquer outra** URL absoluta que apareca no HTML
    reprova, porque nao veio de `Settings`.
    """
    permitidas = {
        container.settings.llm.base_url,
        container.settings.embedding.base_url,
        container.settings.observability.langfuse_host,
    }
    intrusas = sorted(
        {
            f"{rota}: {url}"
            for rota, html in paginas.items()
            for url in urls_externas(html)
            if url.rstrip("/") not in {valor.rstrip("/") for valor in permitidas}
        }
    )

    assert not intrusas, "URLs externas que nao vieram de Settings:\n" + "\n".join(intrusas)


# --------------------------------------------------------------------------- #
# Criterio 4 — o painel de contexto funciona sem JavaScript
# --------------------------------------------------------------------------- #
async def test_painel_de_contexto_resolve_sel_no_servidor_sem_javascript(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`GET /modules?sel=<id>` ja traz os detalhes do modulo no HTML da propria pagina."""
    http, ids = console
    sem_selecao = (await http.get("/modules")).text
    com_selecao = await http.get("/modules", params={"sel": ids["modulo"]})

    assert com_selecao.status_code == 200
    corpo = com_selecao.text
    assert "lk-context__title" not in sem_selecao, (
        "sem ?sel= o painel nao deveria trazer o cabecalho de um item selecionado"
    )
    assert "lk-context__title" in corpo, "?sel= nao renderizou o painel no servidor"
    assert "Assistente geral" in corpo
    assert 'data-selected-id="' + ids["modulo"] + '"' in corpo


async def test_selecao_inexistente_nao_derruba_a_pagina(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`?sel=` com um id que nao existe volta ao painel padrao, sem erro."""
    http, _ = console
    resposta = await http.get("/modules", params={"sel": "id-que-nao-existe"})

    assert resposta.status_code == 200
    assert "lk-context__title" not in resposta.text


async def test_fragmento_de_contexto_devolve_so_o_miolo_e_nao_a_pagina_inteira(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`GET /ui/context/module/<id>` e um fragmento: sem `<html>`, sem `<title>`."""
    http, ids = console
    resposta = await http.get(f"/ui/context/module/{ids['modulo']}")

    assert resposta.status_code == 200
    corpo = resposta.text
    assert "<html" not in corpo.lower(), "o fragmento nao pode trazer o documento inteiro"
    assert "<title" not in corpo.lower()
    assert "lk-topbar" not in corpo and "lk-sidebar" not in corpo
    assert "Assistente geral" in corpo, "o fragmento precisa trazer o item pedido"


async def test_fragmento_e_a_pagina_com_sel_mostram_o_mesmo_item(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """Com e sem JavaScript o operador ve o mesmo painel — e o contrato da secao 7."""
    http, ids = console
    fragmento = (await http.get(f"/ui/context/module/{ids['modulo']}")).text
    pagina = (await http.get("/modules", params={"sel": ids["modulo"]})).text

    miolo = " ".join(fragmento.split())
    assert miolo, "o fragmento veio vazio"
    assert miolo in " ".join(pagina.split()), (
        "a pagina renderizada com ?sel= nao contem o mesmo fragmento servido a /ui/context"
    )


# --------------------------------------------------------------------------- #
# Secao 10 — segredos mascarados e autoescape
# --------------------------------------------------------------------------- #
async def test_settings_mascara_a_api_key_configurada(
    console: tuple[AsyncClient, dict[str, Id]], container: Container
) -> None:
    """Com uma chave configurada, `/settings` mostra `sk-…9042` e nunca o valor."""
    http, _ = console
    container.settings = container.settings.model_copy(
        update={
            "llm": container.settings.llm.model_copy(update={"api_key": SecretStr(CHAVE_DE_TESTE)})
        }
    )

    corpo = (await http.get("/settings")).text

    assert CHAVE_DE_TESTE not in corpo, "a chave de API foi impressa em claro na tela"
    assert mask_secret(CHAVE_DE_TESTE) in corpo, (
        f"a forma mascarada {mask_secret(CHAVE_DE_TESTE)!r} nao aparece em /settings"
    )


async def test_settings_sem_chave_configurada_diz_que_nao_ha_chave(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """Sem segredo definido a tela diz `(nao configurado)`, e nao um campo vazio."""
    http, _ = console
    corpo = (await http.get("/settings")).text

    assert NOT_CONFIGURED in corpo


async def test_settings_nao_imprime_o_segredo_do_jwt(
    console: tuple[AsyncClient, dict[str, Id]], container: Container
) -> None:
    """O segredo de assinatura do JWT tambem so aparece mascarado."""
    http, _ = console
    segredo = container.settings.security.jwt_secret.get_secret_value()

    corpo = (await http.get("/settings")).text

    assert segredo not in corpo, "o segredo do JWT vazou para a tela de configuracoes"
    assert mask_secret(segredo) in corpo


async def test_autoescape_neutraliza_nome_de_modulo_com_script(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """Um modulo chamado `<script>alert(1)</script>` aparece escapado na listagem."""
    http, _ = console
    corpo = (await http.get("/modules")).text

    assert NOME_MALICIOSO not in corpo, "o nome do modulo foi injetado como HTML executavel"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in corpo, (
        "o nome do modulo deveria aparecer escapado na tabela"
    )


async def test_autoescape_vale_tambem_no_painel_de_contexto(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """O fragmento do painel escapa o mesmo nome — o escape nao e privilegio da pagina."""
    http, ids = console
    corpo = (await http.get(f"/ui/context/module/{ids['modulo_malicioso']}")).text

    assert NOME_MALICIOSO not in corpo
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in corpo


# --------------------------------------------------------------------------- #
# Pagina de erro
# --------------------------------------------------------------------------- #
async def test_pagina_de_erro_renderiza_para_modulo_inexistente(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """Id invalido em rota de pagina vira `pages/error.html` com status 404."""
    http, _ = console
    resposta = await http.get("/modules/nao-existe-este-modulo")

    assert resposta.status_code == 404
    assert resposta.headers["content-type"].startswith("text/html")
    corpo = resposta.text
    assert analisar(corpo).titulo.strip() == "Erro 404 · lukato"
    assert "lk-error__title" in corpo, "a resposta nao usou o template de erro do console"


async def test_pagina_de_erro_renderiza_para_execucao_inexistente(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """O mesmo vale para `/runs/{id}`: pagina de erro, nunca JSON."""
    http, _ = console
    resposta = await http.get("/runs/00000000-0000-0000-0000-000000000000")

    assert resposta.status_code == 404
    assert "lk-error__title" in resposta.text
    assert analisar(resposta.text).titulo.strip() == "Erro 404 · lukato"


async def test_pagina_de_erro_preserva_a_moldura_do_console(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """Errar nao tira o operador do console: a moldura continua de pe."""
    http, _ = console
    corpo = (await http.get("/modules/nao-existe-este-modulo")).text

    faltando = [descricao for marca, descricao in MARCADORES_DE_MOLDURA if marca not in corpo]
    assert not faltando, f"a pagina de erro perdeu partes da moldura: {faltando}"


async def test_pagina_de_erro_renderiza_para_rota_inexistente(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """Caminho que nao existe, pedido por navegador, tambem vira pagina de erro.

    O contrato "uma pagina nunca devolve JSON" vale ate para o caminho que rota
    nenhuma reclama: quem manda `Accept: text/html` recebe `pages/error.html`
    com a moldura do console e um caminho de volta, e nao texto cru.
    """
    http, _ = console
    resposta = await http.get(
        "/rota-que-nunca-existiu", headers={"Accept": "text/html,application/xhtml+xml"}
    )

    assert resposta.status_code == 404
    assert resposta.headers["content-type"].startswith("text/html"), (
        f"o navegador recebeu {resposta.headers['content-type']}: {resposta.text[:120]}"
    )
    corpo = resposta.text
    assert "lk-error__title" in corpo
    assert analisar(corpo).titulo.strip() == "Erro 404 · lukato"


async def test_caminho_inexistente_de_api_continua_respondendo_no_envelope_json(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """A negociacao nao pode quebrar o cliente de API: sob `/api/`, 404 e JSON.

    E o outro lado do teste anterior. Sem esta checagem, "toda pagina de erro e
    HTML" poderia ter sido implementado devolvendo markup para todo mundo, e um
    cliente que espera `{"error": {...}}` receberia HTML.
    """
    http, _ = console
    resposta = await http.get(
        "/api/v1/rota-que-nunca-existiu", headers={"Accept": "text/html,application/xhtml+xml"}
    )

    assert resposta.status_code == 404
    assert resposta.headers["content-type"].startswith("application/json")
    assert resposta.json()["error"]["code"] == "not_found"


async def test_cliente_sem_preferencia_por_html_recebe_o_envelope_json(
    console: tuple[AsyncClient, dict[str, Id]],
) -> None:
    """`Accept: */*` e cliente de programa, nao navegador: recebe JSON."""
    http, _ = console
    resposta = await http.get("/rota-que-nunca-existiu", headers={"Accept": "*/*"})

    assert resposta.status_code == 404
    assert resposta.headers["content-type"].startswith("application/json")
    assert resposta.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Formularios: progressive enhancement sem JavaScript
# --------------------------------------------------------------------------- #
def _formularios(paginas: Iterable[tuple[str, str]]) -> list[tuple[str, dict[str, str]]]:
    """Todos os `<form>` das paginas informadas, com a rota de origem."""
    return [
        (rota, formulario) for rota, html in paginas for formulario in analisar(html).formularios
    ]


async def test_todo_formulario_declara_metodo_e_action_nao_vazio(
    paginas: dict[str, str],
) -> None:
    """Nenhum formulario depende do padrao implicito do navegador."""
    problemas = [
        f"{rota}: {formulario}"
        for rota, formulario in _formularios(paginas.items())
        if not formulario.get("action", "").strip()
        or formulario.get("method", "").strip().lower() not in {"get", "post"}
    ]

    assert not problemas, "formularios sem method/action explicitos:\n" + "\n".join(problemas)


async def test_todo_formulario_de_mutacao_usa_post(paginas: dict[str, str]) -> None:
    """Formulario que aponta para a API v1 muda estado — e muda estado com `POST`."""
    problemas = [
        f"{rota}: action={formulario.get('action')} method={formulario.get('method')}"
        for rota, formulario in _formularios(paginas.items())
        if formulario.get("action", "").startswith("/api/v1/")
        and formulario.get("method", "").strip().lower() != "post"
    ]

    assert not problemas, "formularios de mutacao fora do POST:\n" + "\n".join(problemas)


async def test_existe_formulario_de_mutacao_em_toda_tela_de_operacao(
    paginas: dict[str, str],
) -> None:
    """As telas que operam a plataforma oferecem escrita sem JavaScript.

    Sem esta checagem os dois testes acima passariam num console so de leitura:
    e facil nao ter formulario errado quando nao ha formulario nenhum.
    """
    esperadas = (
        "/modules",
        "/modules/assistente",
        "/knowledge",
        "/finops",
        "/identity",
        "/registry",
        "/guardrails",
        "/adwatch",
        "/adwatch/commercials",
    )
    sem_mutacao = [
        rota
        for rota in esperadas
        if not any(
            formulario.get("method", "").lower() == "post"
            for formulario in analisar(paginas[rota]).formularios
        )
    ]

    assert not sem_mutacao, f"telas de operacao sem formulario POST: {sem_mutacao}"
