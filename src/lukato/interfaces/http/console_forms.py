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
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lukato.config import get_logger

__all__ = [
    "CAMPO_METODO",
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

_VERDADEIROS: Final[frozenset[str]] = frozenset({"true", "on", "1", "yes", "sim"})
_FALSOS: Final[frozenset[str]] = frozenset({"false", "off", "0", "no", "nao"})

_TAMANHO_MAXIMO: Final[int] = 1 * 1024 * 1024
"""1 MiB. Um formulario do console nao chega perto; o limite existe para o corpo
nao ser lido inteiro na memoria quando alguem manda outra coisa."""

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
            dados.pop(chave, None)
            continue
        dados[chave] = _valor(bruto)
    return dados, metodo


def _quer_html(cabecalhos: Headers) -> bool:
    """True quando quem pediu foi um navegador seguindo um formulario."""
    return "text/html" in cabecalhos.get("accept", "").lower()


def _volta_para(cabecalhos: Headers, aviso: str) -> str:
    """Monta o destino do 303: a pagina de onde o formulario veio, com o aviso.

    Sem `Referer` o navegador nao disse de onde veio; o cockpit e o unico destino
    que sempre existe.
    """
    origem = cabecalhos.get("referer") or "/"
    partes = urlsplit(origem)
    consulta = [(c, v) for c, v in parse_qsl(partes.query) if c != _PARAM_AVISO]
    consulta.append((_PARAM_AVISO, aviso))
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
        if not tipo.startswith(_FORM_MIME) or not scope["path"].startswith(self.prefixo):
            await self.app(scope, receive, send)
            return

        corpo = await _ler_corpo(receive)
        if corpo is None:  # corpo grande demais: deixa o app recusar do jeito dele
            await self.app(scope, receive, send)
            return

        dados, pedido = form_para_json(corpo)
        novo_corpo = json.dumps(dados).encode("utf-8")

        novo_scope = dict(scope)
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


class _Redirecionador:
    """Troca a resposta JSON de sucesso por um 303 de volta para a pagina.

    Padrao POST-Redirect-GET: sem ele o navegador ficaria parado numa tela de
    JSON depois de salvar, e o F5 reenviaria o formulario. So mexe em 2xx —
    erro continua indo para o tratador, que ja devolve a pagina de erro em HTML.
    """

    def __init__(self, send: Send, *, cabecalhos: Headers) -> None:
        self._send = send
        self._cabecalhos = cabecalhos
        self._desviando = False

    async def __call__(self, evento: Message) -> None:
        """Intercepta o inicio da resposta e decide se desvia."""
        if evento["type"] == "http.response.start":
            status = int(evento["status"])
            if 200 <= status < 300:
                self._desviando = True
                destino = _volta_para(self._cabecalhos, "salvo")
                resposta = RedirectResponse(destino, status_code=303)
                await resposta(  # type: ignore[call-arg]
                    {"type": "http"}, _corpo_unico(b""), self._send
                )
                return
        if self._desviando and evento["type"] in {"http.response.body", "http.response.start"}:
            return  # a resposta original ja foi substituida
        await self._send(evento)


def resposta_de_redirecionamento(request: Request, aviso: str) -> Response:
    """303 de volta para a pagina de origem — util em testes e rotas proprias."""
    return RedirectResponse(_volta_para(request.headers, aviso), status_code=303)
