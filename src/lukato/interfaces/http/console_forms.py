"""Ponte entre os formularios HTML do console e a API JSON.

O PROBLEMA QUE ISTO RESOLVE
===========================

O console tem 33 formularios `method="post"` apontando para `/api/v1/...`. Um
formulario HTML manda o corpo em `application/x-www-form-urlencoded`; a API so
aceita `application/json`. Resultado, ao clicar em qualquer botao de gravar::

    {"error":{"code":"validation_error",
              "message":"A requisicao nao passou na validacao de esquema.",
              "details":{"errors":[{"loc":["body"],
                "msg":"Input should be a valid dictionary or object ...",
                "input":"uri=C%3A%5CUsers%5C...&title=tv&kind=video"}]}}}

Nenhuma acao de escrita do console funcionava — nem criar modulo, nem cadastrar
usuario, nem registrar midia. O console renderizava inteiro e era somente-leitura
na pratica. Os testes nao pegaram porque exercitavam a API com JSON (que sempre
funcionou) e o console so pelo GET das paginas.

Os templates ja tinham sido escritos para esta camada — `pages/guardrails.html`
documenta o campo `_method` como "o campo oculto que o console usa quando o verbo
da API nao existe em HTML". A convencao estava especificada; o codigo que a le
nunca existiu.

POR QUE UM MIDDLEWARE, E NAO 33 ROTAS
=====================================

Conferi campo a campo: os formularios mandam exatamente os nomes que os schemas
Pydantic declaram, sem aninhamento. Nao falta nem sobra nada. O unico degrau e a
codificacao. Um middleware traduz os 33 de uma vez e some quando a requisicao ja
vem em JSON — que e o caso de todo cliente de API.

Escrever 33 rotas paralelas duplicaria autorizacao, validacao e tratamento de
erro de cada endpoint, e as duas copias divergiriam no primeiro campo novo.

O QUE ELE FAZ
=============

1. `_method=put|patch|delete` troca o verbo. HTML so tem GET e POST.
2. Corpo form-urlencoded vira JSON.
3. Campo repetido: vence o ULTIMO. E o idioma de caixa de selecao em HTML — um
   `<input type="hidden" name="x" value="false">` antes da caixa garante que
   desmarcada mande `false` em vez de nao mandar nada.
4. Campo vazio e omitido, para o default do schema valer em vez de virar `""`.
5. Deu certo e quem pediu foi um navegador: **303 de volta para a pagina** com
   um aviso. Sem isto o usuario ficaria olhando JSON cru depois de salvar.
6. Deu errado: nao mexe. O tratador de erro ja negocia conteudo e devolve a
   pagina de erro em HTML para navegador.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lukato.config import get_logger

__all__ = [
    "CAMPO_METODO",
    "MARCA_CONSOLE",
    "ConsoleFormMiddleware",
    "form_para_json",
]

_logger = get_logger(__name__)

CAMPO_METODO: Final[str] = "_method"
"""Campo oculto que carrega o verbo real (`put`, `patch`, `delete`)."""

_VERBOS_PERMITIDOS: Final[frozenset[str]] = frozenset({"PUT", "PATCH", "DELETE"})
"""So estes. `_method=get` transformaria um POST em navegacao, e `_method` vem do
cliente: um formulario forjado nao pode escolher qualquer verbo."""

_FORM_MIME: Final[str] = "application/x-www-form-urlencoded"
_MULTIPART_MIME: Final[str] = "multipart/form-data"

_VERDADEIROS: Final[frozenset[str]] = frozenset({"true", "on", "1", "yes", "sim"})
_FALSOS: Final[frozenset[str]] = frozenset({"false", "off", "0", "no", "nao"})

_TAMANHO_MAXIMO: Final[int] = 1 * 1024 * 1024
"""1 MiB. Um formulario do console nao chega perto; o limite existe para o corpo
nao ser lido inteiro na memoria quando alguem manda outra coisa."""

MARCA_CONSOLE: Final[str] = "console_form"
"""Marca deixada em `request.state` quando a requisicao veio de um formulario.

O tratador de erro decidia HTML x JSON so pelo prefixo do caminho: tudo sob
`/api/` respondia JSON. A regra estava certa quando so cliente de API postava
ali. Com esta ponte, o NAVEGADOR tambem posta em `/api/v1/...` — e um slug
repetido devolvia ao usuario a tela

    {"error":{"code":"conflict","message":"Ja existe o prompt ..."}}

texto cru, sem moldura e sem caminho de volta, depois de clicar em Salvar. O
prefixo nao distingue mais os dois clientes; esta marca distingue.
"""

_PARAM_AVISO: Final[str] = "ok"
"""Query param do aviso de sucesso. Escolhido em vez de sessao ou cookie porque
funciona sem JavaScript e sem estado no servidor, que e o que a SPEC-0009 pede."""


def _valor(bruto: str) -> Any:
    """Converte um valor de formulario para o tipo que o JSON espera.

    Tudo que vem de um formulario e string. `"true"`/`"on"` viram booleano porque
    e o que uma caixa de selecao manda; o resto fica string e o Pydantic converte
    (ele aceita `"42"` para int e `"2026-01-01"` para data).
    """
    texto = bruto.strip()
    minusculo = texto.lower()
    if minusculo in _VERDADEIROS:
        return True
    if minusculo in _FALSOS:
        return False
    return texto


# --------------------------------------------------------------------------
# A convencao de nome de campo
#
# HTML nao tem tipo: todo campo e uma string plana, e `name` e o unico lugar
# onde da para dizer mais. Os schemas, do outro lado, tem lista, objeto
# aninhado, dicionario de chave livre e lista de objetos. A ponte e uma
# convencao NO NOME, declarada no template ao lado do input:
#
#     keywords[]              lista, quebrada por virgula (ou repetida)
#     binding.model           objeto aninhado: {"binding": {"model": ...}}
#     variables[empresa]      dicionario de chave livre
#     variables{}             o proprio valor e JSON, digitado numa textarea
#     rules[0].kind           lista de objetos: {"rules": [{"kind": ...}]}
#
# E a convencao de Rails e PHP, escolhida por ser conhecida e por caber no
# atributo `name` — sem campo escondido, sem JavaScript, sem uma tabela de
# tipos separada que ia divergir do schema no primeiro campo novo.
#
# Colchete com numero so e INDICE quando vem seguido de mais caminho
# (`rules[0].kind`). No fim do nome ele e chave de dicionario (`variables[2]`
# e a chave "2"). Sem essa regra os dois casos seriam indistinguiveis.
# --------------------------------------------------------------------------
_FINAL_LISTA = re.compile(r"^(?P<campo>[^.\[\]{}]+)\[\]$")
_FINAL_JSON = re.compile(r"^(?P<campo>[^.\[\]{}]+)\{\}$")
_FINAL_CHAVE = re.compile(r"^(?P<campo>[^.\[\]{}]+)\[(?P<chave>[^\]]+)\]$")
_INDICE = re.compile(r"^(?P<campo>[^.\[\]{}]+)\[(?P<indice>\d+)\]$")

CHAVE_ANCORA: Final[str] = "id"
"""Chave que ancora uma entrada de lista indexada.

O editor de politicas documenta, no proprio template, duas frases: "apagar o
identificador remove a regra ao salvar" e, na linha nova, "em branco, a linha e
ignorada ao salvar". As duas sao a mesma regra — uma entrada que chega sem `id`
e descartada — e e assim que se apaga uma regra sem JavaScript.

So vale para lista cujo formulario DECLARA um campo `id`; sem isso as entradas
passam todas. Uma lista de objetos sem identificador seria esvaziada em silencio
pela regra acima, que e o pior desfecho possivel para quem clicou em salvar.
"""


class _Indexada(dict[int, dict[str, Any]]):
    """Lista de objetos em construcao, ainda endereçada por indice.

    Os campos chegam fora de ordem e com buracos (`rules[0]`, `rules[2]`), entao
    a lista so vira lista no fim, em `_compactar`. `ancorada` lembra se o
    formulario chegou a declarar `id` para alguma entrada.
    """

    ancorada: bool = False


def _lista(bruto: str) -> list[str]:
    """Quebra `"a, b , c"` em `["a", "b", "c"]`, sem vazios."""
    return [pedaco.strip() for pedaco in bruto.split(",") if pedaco.strip()]


def _descer(
    dados: dict[str, Any], caminho: list[str], *, criando: bool
) -> tuple[dict[str, Any] | None, _Indexada | None]:
    """Percorre o caminho ate o dicionario que hospeda a folha.

    Devolve tambem a lista indexada mais interna do caminho, quando existe, para
    quem chamou poder marcar `ancorada`. `criando=False` nao inventa nivel
    nenhum: e o modo do campo vazio, que so precisa remover o que ja esta la.
    """
    atual = dados
    ancora: _Indexada | None = None
    for segmento in caminho:
        if achado := _INDICE.match(segmento):
            campo, indice = achado["campo"], int(achado["indice"])
            lista = atual.setdefault(campo, _Indexada()) if criando else atual.get(campo)
            if not isinstance(lista, _Indexada):
                return None, None
            ancora = lista
            if criando:
                atual = lista.setdefault(indice, {})
            elif indice in lista:
                atual = lista[indice]
            else:
                return None, None
            continue
        filho = atual.setdefault(segmento, {}) if criando else atual.get(segmento)
        if not isinstance(filho, dict):
            return None, None
        atual = filho
    return atual, ancora


def _pousar(alvo: dict[str, Any], folha: str, bruto: str) -> None:
    """Grava a folha no dicionario que a hospeda, lendo a forma do nome."""
    if achado := _FINAL_LISTA.match(folha):
        # Acumula: `tools[]` pode vir de um campo com virgulas OU de varias
        # caixas de selecao com o mesmo nome, e as duas formas sao HTML legitimo.
        anterior = alvo.get(achado["campo"])
        itens = list(anterior) if isinstance(anterior, list) else []
        itens.extend(_lista(bruto))
        alvo[achado["campo"]] = itens
        return

    if achado := _FINAL_JSON.match(folha):
        # Campo que carrega JSON digitado a mao (uma textarea de variaveis, um
        # lote de itens). JSON malformado NAO e engolido: fica como string e o
        # Pydantic recusa com a mensagem dele, que diz o campo e o motivo. Um
        # `except: pass` aqui mandaria o texto cru adiante e o erro apareceria
        # tres camadas depois, sem dizer que o usuario digitou JSON invalido.
        try:
            alvo[achado["campo"]] = json.loads(bruto)
        except json.JSONDecodeError:
            alvo[achado["campo"]] = bruto
        return

    if achado := _FINAL_CHAVE.match(folha):
        dicionario = alvo.setdefault(achado["campo"], {})
        if isinstance(dicionario, dict):
            dicionario[achado["chave"]] = _valor(bruto)
        return

    alvo[folha] = _valor(bruto)


def _nome_da_folha(folha: str) -> str:
    """Nome do campo de uma folha, descontando a forma (`config{}` -> `config`)."""
    for regex in (_FINAL_LISTA, _FINAL_JSON, _FINAL_CHAVE):
        if achado := regex.match(folha):
            return achado["campo"]
    return folha


def _acomodar(dados: dict[str, Any], chave: str, bruto: str) -> None:
    """Guarda um campo, entendendo as formas de nome descritas acima."""
    *caminho, folha = chave.split(".")
    alvo, ancora = _descer(dados, caminho, criando=True)
    if alvo is None:
        return
    if ancora is not None and _nome_da_folha(folha) == CHAVE_ANCORA:
        ancora.ancorada = True
    _pousar(alvo, folha, bruto)


def _esvaziar(dados: dict[str, Any], chave: str) -> None:
    """Remove um campo vazio, respeitando as mesmas formas de nome.

    Poda os niveis que ficaram vazios ao longo do caminho: um `binding.model` em
    branco nao pode deixar para tras um `{"binding": {}}` que o schema veria como
    "o usuario mandou um binding vazio" em vez de "o usuario nao mexeu nisso".
    """
    *caminho, folha = chave.split(".")
    alvo, ancora = _descer(dados, caminho, criando=False)
    if ancora is not None and _nome_da_folha(folha) == CHAVE_ANCORA:
        # Marca mesmo em branco: e exatamente assim que se apaga uma regra.
        ancora.ancorada = True
    if alvo is None:
        return
    alvo.pop(_nome_da_folha(folha), None)
    _podar(dados, caminho)


def _podar(dados: dict[str, Any], caminho: list[str]) -> None:
    """Descarta, de dentro para fora, os niveis do caminho que ficaram vazios."""
    for fim in range(len(caminho), 0, -1):
        pai, _ = _descer(dados, caminho[: fim - 1], criando=False)
        if pai is None:
            return
        segmento = caminho[fim - 1]
        if achado := _INDICE.match(segmento):
            lista = pai.get(achado["campo"])
            if isinstance(lista, _Indexada) and not lista.get(int(achado["indice"])):
                lista.pop(int(achado["indice"]), None)
                if not lista:
                    pai.pop(achado["campo"], None)
            continue
        if pai.get(segmento) == {}:
            pai.pop(segmento, None)


def _compactar(valor: Any) -> Any:
    """Transforma as listas indexadas em listas de verdade, na ordem do indice.

    Entrada sem `id` cai fora quando o formulario declarou `id` para a lista —
    ver `CHAVE_ANCORA`. O buraco de indice nao vira `null`: `rules[0]` e
    `rules[2]` viram uma lista de dois, porque quem apagou a regra do meio
    espera uma politica com duas regras, nao uma com um furo.
    """
    if isinstance(valor, _Indexada):
        entradas = [_compactar(entrada) for _, entrada in sorted(valor.items())]
        if valor.ancorada:
            entradas = [entrada for entrada in entradas if entrada.get(CHAVE_ANCORA)]
        return entradas
    if isinstance(valor, dict):
        return {chave: _compactar(item) for chave, item in valor.items()}
    return valor


def form_para_json(corpo: bytes) -> tuple[dict[str, Any], str | None]:
    """Traduz um corpo form-urlencoded em `(dados, metodo_pedido)`.

    Campo repetido: vence o ultimo, que e o idioma de caixa de selecao em HTML.
    Campo vazio e omitido: o default declarado no schema vale mais do que uma
    string vazia que o usuario nao digitou.
    """
    dados: dict[str, Any] = {}
    metodo: str | None = None
    for chave, bruto in parse_qsl(corpo.decode("utf-8"), keep_blank_values=True):
        if chave == CAMPO_METODO:
            metodo = bruto.strip().upper()
            continue
        if not bruto.strip():
            _esvaziar(dados, chave)
            continue
        _acomodar(dados, chave, bruto)
    return _compactar(dados), metodo


def _quer_html(cabecalhos: Headers) -> bool:
    """True quando quem pediu foi um navegador seguindo um formulario."""
    return "text/html" in cabecalhos.get("accept", "").lower()


def _volta_para(cabecalhos: Headers, aviso: str, *, selecionado: str | None = None) -> str:
    """Monta o destino do 303: a pagina de onde o formulario veio, com o aviso.

    Sem `Referer` o navegador nao disse de onde veio; o cockpit e o unico destino
    que sempre existe.
    """
    origem = cabecalhos.get("referer") or "/"
    partes = urlsplit(origem)
    descartar = {_PARAM_AVISO} | ({"sel"} if selecionado else set())
    consulta = [(c, v) for c, v in parse_qsl(partes.query) if c not in descartar]
    consulta.append((_PARAM_AVISO, aviso))
    if selecionado:
        consulta.append(("sel", selecionado))
    # Descarta esquema e host: redirecionar para o que o cliente mandou no
    # Referer seria um open redirect. So o caminho do proprio console sobrevive.
    return urlunsplit(("", "", partes.path or "/", urlencode(consulta), partes.fragment))


class ConsoleFormMiddleware:
    """Faz a API entender o formulario HTML que o console ja mandava.

    ASGI puro, e nao `BaseHTTPMiddleware`, porque precisa reescrever `method` e
    `headers` no proprio `scope` antes de qualquer validacao, e trocar o corpo
    que o app vai ler. `BaseHTTPMiddleware` nao alcanca o scope a tempo.
    """

    def __init__(self, app: ASGIApp, *, prefixo: str = "/api/") -> None:
        self.app = app
        self.prefixo = prefixo

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Traduz a requisicao quando ela e um formulario; senao sai da frente."""
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        cabecalhos = Headers(scope=scope)
        tipo = cabecalhos.get("content-type", "")
        do_console = scope["path"].startswith(self.prefixo)

        # Formulario multipart (upload de arquivo): o corpo NAO e traduzido — os
        # endpoints de importacao ja leem multipart nativamente. O que faltava era
        # o outro lado: sem isto o usuario terminava a importacao olhando o JSON
        # da resposta, com a URL parada em /api/v1/..., e o F5 reenviava o arquivo.
        if do_console and tipo.startswith(_MULTIPART_MIME) and _quer_html(cabecalhos):
            _marcar(scope)
            await self.app(scope, receive, _Redirecionador(send, cabecalhos=cabecalhos))
            return

        if not tipo.startswith(_FORM_MIME) or not do_console:
            await self.app(scope, receive, send)
            return

        corpo = await _ler_corpo(receive)
        if corpo is None:  # corpo grande demais: deixa o app recusar do jeito dele
            await self.app(scope, receive, send)
            return

        dados, pedido = form_para_json(corpo)
        novo_corpo = json.dumps(dados).encode("utf-8")

        novo_scope = dict(scope)
        _marcar(novo_scope)
        if pedido in _VERBOS_PERMITIDOS:
            novo_scope["method"] = pedido
        novos_cabecalhos = MutableHeaders(scope=novo_scope)
        novos_cabecalhos["content-type"] = "application/json"
        novos_cabecalhos["content-length"] = str(len(novo_corpo))

        _logger.debug(
            "console_form_traduzido",
            path=scope["path"],
            metodo=novo_scope["method"],
            campos=sorted(dados),
        )

        html = _quer_html(cabecalhos)
        origem = cabecalhos
        enviador = _Redirecionador(send, cabecalhos=origem) if html else send
        await self.app(novo_scope, _corpo_unico(novo_corpo), enviador)


def _marcar(scope: Scope) -> None:
    """Anota no `state` do scope que esta requisicao nasceu de um formulario.

    `scope["state"]` e o mesmo dicionario que vira `request.state`; o Starlette o
    cria por requisicao. Se ele nao existir (scope montado a mao num teste), a
    marca simplesmente nao acontece e o comportamento antigo vale.
    """
    estado = scope.get("state")
    if not isinstance(estado, dict):
        # O ASGI so obriga `state` quando o lifespan publicou algum; o Starlette
        # o cria sob demanda em `Request.state`. Criar aqui garante que a marca
        # chegue ao tratador de erro em qualquer servidor.
        estado = {}
        scope["state"] = estado
    estado[MARCA_CONSOLE] = True


async def _ler_corpo(receive: Receive) -> bytes | None:
    """Junta o corpo da requisicao; `None` se passar do limite."""
    partes: list[bytes] = []
    tamanho = 0
    while True:
        evento = await receive()
        if evento["type"] != "http.request":
            break
        pedaco: bytes = evento.get("body", b"")
        tamanho += len(pedaco)
        if tamanho > _TAMANHO_MAXIMO:
            return None
        partes.append(pedaco)
        if not evento.get("more_body", False):
            break
    return b"".join(partes)


def _corpo_unico(corpo: bytes) -> Receive:
    """`receive` que entrega o corpo reescrito uma vez e depois se cala."""
    entregue = False

    async def receive() -> Message:
        nonlocal entregue
        if entregue:
            return {"type": "http.disconnect"}
        entregue = True
        return {"type": "http.request", "body": corpo, "more_body": False}

    return receive


_TAMANHO_RESPOSTA: Final[int] = 256 * 1024
"""Teto do corpo de resposta que vale a pena juntar so para achar o `id`. Acima
disto o redirecionamento acontece sem `sel` — perder o realce e barato, segurar
um corpo grande na memoria nao e."""


class _Redirecionador:
    """Troca a resposta JSON de sucesso por um 303 de volta para a pagina.

    Padrao POST-Redirect-GET: sem ele o navegador ficaria parado numa tela de
    JSON depois de salvar, e o F5 reenviaria o formulario. So mexe em 2xx —
    erro continua indo para o tratador, que ja devolve a pagina de erro em HTML.

    O 303 leva `sel=<id>` do que acabou de ser gravado. Sem isso, salvar um item
    devolvia a lista na ordem alfabetica de sempre: com 27 prompts cadastrados, o
    que o usuario acabou de criar caia na SEGUNDA pagina e sumia da vista. A tela
    dizia "salvo" e nao mostrava o que foi salvo. Para achar o `id` e preciso ler
    o corpo da resposta, entao o desvio espera o corpo terminar em vez de
    disparar no `http.response.start`.
    """

    def __init__(self, send: Send, *, cabecalhos: Headers) -> None:
        self._send = send
        self._cabecalhos = cabecalhos
        self._desviando = False
        self._corpo: list[bytes] = []
        self._tamanho = 0

    async def __call__(self, evento: Message) -> None:
        """Segura a resposta de sucesso, junta o corpo e desvia no fim."""
        tipo = evento["type"]
        if tipo == "http.response.start":
            if 200 <= int(evento["status"]) < 300:
                self._desviando = True
                return
            await self._send(evento)
            return

        if not self._desviando:
            await self._send(evento)
            return

        if tipo == "http.response.body":
            pedaco: bytes = evento.get("body", b"")
            self._tamanho += len(pedaco)
            if self._tamanho <= _TAMANHO_RESPOSTA:
                self._corpo.append(pedaco)
            if evento.get("more_body", False):
                return
            await self._desviar()

    async def _desviar(self) -> None:
        """Emite o 303 para a pagina de origem, realcando o item gravado."""
        destino = _volta_para(self._cabecalhos, "salvo", selecionado=self._identificador())
        resposta = RedirectResponse(destino, status_code=303)
        await resposta({"type": "http"}, _corpo_unico(b""), self._send)  # type: ignore[call-arg]

    def _identificador(self) -> str | None:
        """`id` do recurso gravado, quando a resposta o traz."""
        if self._tamanho > _TAMANHO_RESPOSTA:
            return None
        try:
            corpo = json.loads(b"".join(self._corpo).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(corpo, dict):
            valor = corpo.get("id")
            if isinstance(valor, str) and valor:
                return valor
        return None


def resposta_de_redirecionamento(request: Request, aviso: str) -> Response:
    """303 de volta para a pagina de origem — util em testes e rotas proprias."""
    return RedirectResponse(_volta_para(request.headers, aviso), status_code=303)
