"""Testes de unidade dos adaptadores de LLM (SPEC-0000 secoes 7.1 e 14).

Dois adaptadores, duas responsabilidades bem diferentes:

* `EchoLLM` e o provedor efetivo offline. O que se prova aqui e o **determinismo**
  (mesma entrada, mesma saida, sem relogio e sem sorteio), o modo `[[JSON]]`, o
  `stream()` em fragmentos, a estimativa de consumo e o `health()` sempre verdadeiro.
* `OpenAICompatibleLLM` e a borda de rede. Nenhum teste deste arquivo abre socket:
  o cliente do SDK e substituido por um duble que levanta o erro que o teste quer
  ver traduzido. O que se prova e a **politica de retentativa** — repete o que e
  transitorio, falha na hora o que e definitivo — e a conversao para `LukatoError`.

A espera do backoff e zerada por `monkeypatch` nos limites do modulo: o teste
mede o numero de tentativas, nao a paciencia do `tenacity`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, RateLimitError

from lukato.adapters.llm import openai_compatible
from lukato.adapters.llm.echo import (
    DEFAULT_ECHO_MODEL,
    ECHO_PREFIX,
    ECHO_STREAM_CHUNK,
    JSON_MARKER,
    EchoLLM,
    estimate_usage,
)
from lukato.adapters.llm.factory import ECHO_REASONS, build_llm, build_llm_with_health
from lukato.adapters.llm.openai_compatible import (
    PLACEHOLDER_API_KEY,
    RETRY_ATTEMPTS,
    OpenAICompatibleLLM,
)
from lukato.config.settings import Settings
from lukato.domain.errors import ProviderError, RateLimitedError, ValidationError
from lukato.domain.ports.llm import ChatMessage, LLMPort

pytestmark = pytest.mark.unit

MENSAGENS = [ChatMessage.system("Voce e um agente."), ChatMessage.user("ola mundo")]
"""Conversa fixa usada em todos os testes de eco."""

URL_FALSA = "https://hub.invalido.teste/v1"
"""Endpoint inexistente: nenhum teste deste arquivo chega a resolve-lo."""


def _settings(**llm: Any) -> Settings:
    """`Settings` de teste sem `.env`, com o grupo de LLM ajustado pelo chamador."""
    base = {"provider": "openai_compatible", "base_url": URL_FALSA, "model": "modelo-hub"}
    return Settings(_env_file=None, llm={**base, **llm}, embedding={"provider": "hashing"})


# --------------------------------------------------------------------------- #
# Dubles do SDK
# --------------------------------------------------------------------------- #
class _CompletionsQueBrota:
    """`client.chat.completions` que levanta um erro programado e conta tentativas."""

    def __init__(self, erro: Exception) -> None:
        self._erro = erro
        self.tentativas = 0

    async def create(self, **kwargs: Any) -> Any:
        """Conta a tentativa e levanta o erro do teste."""
        self.tentativas += 1
        raise self._erro


class _ClienteFalso:
    """Duble minimo de `AsyncOpenAI`: so o que o adaptador realmente usa."""

    def __init__(self, erro: Exception) -> None:
        self.completions = _CompletionsQueBrota(erro)
        self.chat = type("_Chat", (), {"completions": self.completions})()
        self.models = type("_Models", (), {"list": self._list_models})()
        self.erro = erro

    async def _list_models(self, **kwargs: Any) -> Any:
        """`models.list` sempre falha: e o caminho do `health()` sem rede."""
        raise self.erro


def _adaptador_com_erro(erro: Exception, **llm: Any) -> OpenAICompatibleLLM:
    """Monta o adaptador com o cliente dublado que levanta `erro`."""
    return OpenAICompatibleLLM(_settings(**llm), client=_ClienteFalso(erro))  # type: ignore[arg-type]


def _requisicao() -> httpx.Request:
    """Requisicao httpx sintetica exigida pelos construtores de erro do SDK."""
    return httpx.Request("POST", f"{URL_FALSA}/chat/completions")


def _erro_de_status(status: int) -> APIStatusError:
    """`APIStatusError` com o status pedido (400 e o 4xx de contrato tipico)."""
    resposta = httpx.Response(status, request=_requisicao(), json={"error": "contrato"})
    return APIStatusError("recusado", response=resposta, body={"error": "contrato"})


def _erro_de_limite() -> RateLimitError:
    """`RateLimitError` 429 do SDK."""
    resposta = httpx.Response(429, request=_requisicao(), json={"error": "devagar"})
    return RateLimitError("limite", response=resposta, body={"error": "devagar"})


@pytest.fixture
def sem_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zera o backoff exponencial: o teste conta tentativas, nao segundos."""
    monkeypatch.setattr(openai_compatible, "RETRY_WAIT_MIN", 0.0)
    monkeypatch.setattr(openai_compatible, "RETRY_WAIT_MAX", 0.0)


# --------------------------------------------------------------------------- #
# EchoLLM
# --------------------------------------------------------------------------- #
async def test_echo_e_deterministico_para_a_mesma_entrada() -> None:
    llm = EchoLLM()

    primeira = await llm.chat(MENSAGENS)
    segunda = await llm.chat(MENSAGENS)

    assert primeira.content == segunda.content == f"{ECHO_PREFIX}ola mundo"
    assert primeira.usage == segunda.usage
    assert primeira.model == DEFAULT_ECHO_MODEL


async def test_echo_ecoa_a_ultima_mensagem_do_usuario_e_ignora_o_system() -> None:
    llm = EchoLLM()

    resposta = await llm.chat(
        [ChatMessage.system("regras"), ChatMessage.user("primeira"), ChatMessage.user("segunda")]
    )

    assert resposta.content == f"{ECHO_PREFIX}segunda"


async def test_echo_devolve_literalmente_o_que_vem_depois_do_marcador_json() -> None:
    payload = '{"resposta": "conteudo controlado"}'
    llm = EchoLLM()

    resposta = await llm.chat([ChatMessage.user(f"{JSON_MARKER} {payload}")])

    assert resposta.content == payload
    assert json.loads(resposta.content) == {"resposta": "conteudo controlado"}
    assert resposta.raw["echo_mode"] == "marker"
    assert resposta.raw["json_valid"] is True


async def test_echo_marcador_json_tambem_entrega_json_quebrado_de_proposito() -> None:
    llm = EchoLLM()

    resposta = await llm.chat([ChatMessage.user(f"{JSON_MARKER} {{quebrado")])

    assert resposta.content == "{quebrado"
    assert resposta.raw["json_valid"] is False, (
        "e o que permite exercitar o guardrail de saida por schema com JSON invalido"
    )


async def test_echo_com_response_format_json_object_devolve_json_valido() -> None:
    llm = EchoLLM()

    resposta = await llm.chat(MENSAGENS, response_format={"type": "json_object"})

    assert json.loads(resposta.content), "o modo JSON precisa devolver um objeto desserializavel"
    assert resposta.raw["echo_mode"] == "json"


async def test_echo_com_json_schema_preenche_as_chaves_obrigatorias() -> None:
    formato = {
        "type": "json_schema",
        "json_schema": {
            "schema": {
                "type": "object",
                "required": ["resposta", "confianca"],
                "properties": {
                    "resposta": {"type": "string"},
                    "confianca": {"type": "number"},
                },
            }
        },
    }

    resposta = await EchoLLM().chat(MENSAGENS, response_format=formato)

    assert json.loads(resposta.content) == {"resposta": "", "confianca": 0.0}


async def test_echo_stream_produz_fragmentos_que_recompoem_a_resposta() -> None:
    llm = EchoLLM()
    texto = "x" * (ECHO_STREAM_CHUNK * 2 + 5)

    fragmentos = [pedaco async for pedaco in llm.stream([ChatMessage.user(texto)])]

    assert len(fragmentos) > 1, "o stream tem de emitir mais de um fragmento"
    assert all(len(pedaco) <= ECHO_STREAM_CHUNK for pedaco in fragmentos)
    assert "".join(fragmentos) == f"{ECHO_PREFIX}{texto}"


async def test_echo_stream_devolve_o_mesmo_conteudo_do_chat() -> None:
    llm = EchoLLM()

    completo = (await llm.chat(MENSAGENS)).content
    transmitido = "".join([pedaco async for pedaco in llm.stream(MENSAGENS)])

    assert transmitido == completo


async def test_echo_estima_o_consumo_por_quatro_caracteres_por_token() -> None:
    llm = EchoLLM()

    resposta = await llm.chat(MENSAGENS)

    prompt_texto = "".join(mensagem.content for mensagem in MENSAGENS)
    assert resposta.usage == estimate_usage(prompt_texto, resposta.content)
    assert resposta.usage.prompt_tokens == len(prompt_texto) // 4
    assert resposta.usage.total_tokens == (
        resposta.usage.prompt_tokens + resposta.usage.completion_tokens
    )
    assert resposta.raw["usage_estimated"] is True


async def test_echo_respeita_max_tokens_e_reporta_finish_reason_length() -> None:
    llm = EchoLLM()

    resposta = await llm.chat([ChatMessage.user("a" * 200)], max_tokens=5)

    assert len(resposta.content) == 20, "5 tokens x 4 caracteres por token"
    assert resposta.finish_reason == "length"


async def test_echo_corta_no_primeiro_marcador_de_parada() -> None:
    resposta = await EchoLLM().chat([ChatMessage.user("antes|depois")], stop=["|"])

    assert resposta.content == f"{ECHO_PREFIX}antes"


async def test_echo_esta_sempre_saudavel_e_publica_o_proprio_modelo() -> None:
    llm = EchoLLM()

    assert await llm.health() is True
    assert await llm.list_models() == [DEFAULT_ECHO_MODEL]
    assert isinstance(llm, LLMPort), "o eco precisa satisfazer a porta `LLMPort`"


# --------------------------------------------------------------------------- #
# Fabrica
# --------------------------------------------------------------------------- #
def test_factory_escolhe_o_eco_quando_nao_ha_chave_de_api() -> None:
    settings = _settings(api_key=None)

    adaptador = build_llm(settings)

    assert isinstance(adaptador, EchoLLM)
    assert settings.llm.effective_provider == "echo"


def test_factory_escolhe_o_eco_quando_o_provedor_e_pedido_explicitamente() -> None:
    adaptador = build_llm(_settings(provider="echo"))

    assert isinstance(adaptador, EchoLLM)
    assert ECHO_REASONS["echo"].startswith("LUKATO_LLM__PROVIDER=echo")


def test_factory_escolhe_o_adaptador_de_rede_quando_ha_chave() -> None:
    adaptador = build_llm(_settings(api_key="chave-de-teste"))

    assert isinstance(adaptador, OpenAICompatibleLLM)
    assert adaptador.base_url == URL_FALSA


async def test_factory_com_health_nao_levanta_quando_o_provedor_esta_fora(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(api_key="chave-de-teste")
    monkeypatch.setattr(
        openai_compatible.OpenAICompatibleLLM,
        "health",
        _levanta_sempre,
    )

    adaptador, saudavel = await build_llm_with_health(settings)

    assert isinstance(adaptador, OpenAICompatibleLLM)
    assert saudavel is False, "a falha na verificacao degrada, nao derruba o boot"


async def _levanta_sempre(self: object) -> bool:
    """`health()` que estoura, para exercitar a rede de seguranca da fabrica."""
    raise RuntimeError("provedor inalcancavel")


# --------------------------------------------------------------------------- #
# OpenAICompatibleLLM — construcao e saude
# --------------------------------------------------------------------------- #
def test_openai_compativel_e_instanciavel_sem_rede() -> None:
    adaptador = OpenAICompatibleLLM(_settings(api_key="chave-de-teste"))

    assert adaptador.default_model == "modelo-hub"
    assert adaptador.base_url == URL_FALSA
    assert adaptador.provider == "openai_compatible"


def test_openai_compativel_usa_marcador_publico_quando_o_hub_nao_exige_chave() -> None:
    adaptador = OpenAICompatibleLLM(_settings(api_key=None))

    assert adaptador.client.api_key == PLACEHOLDER_API_KEY, (
        "o SDK recusa construir sem `api_key`; o marcador satisfaz a validacao"
    )


async def test_openai_compativel_health_devolve_false_sem_levantar() -> None:
    adaptador = _adaptador_com_erro(APIConnectionError(request=_requisicao()))

    assert await adaptador.health() is False


async def test_openai_compativel_recusa_chamada_sem_mensagens() -> None:
    adaptador = _adaptador_com_erro(_erro_de_status(400))

    with pytest.raises(ValidationError):
        await adaptador.chat([])


# --------------------------------------------------------------------------- #
# OpenAICompatibleLLM — traducao de erro e retentativa
# --------------------------------------------------------------------------- #
async def test_rate_limit_error_do_sdk_vira_rate_limited_error(sem_espera: None) -> None:
    adaptador = _adaptador_com_erro(_erro_de_limite())

    with pytest.raises(RateLimitedError) as capturado:
        await adaptador.chat(MENSAGENS)

    assert capturado.value.http_status == 429
    assert capturado.value.details["status"] == 429


async def test_erro_de_status_400_vira_provider_error_sem_retentativa(sem_espera: None) -> None:
    cliente = _ClienteFalso(_erro_de_status(400))
    adaptador = OpenAICompatibleLLM(_settings(), client=cliente)  # type: ignore[arg-type]

    with pytest.raises(ProviderError) as capturado:
        await adaptador.chat(MENSAGENS)

    assert cliente.completions.tentativas == 1, (
        "um 400 e erro de contrato: repetir so queima tempo e cota"
    )
    assert capturado.value.details["status"] == 400
    assert capturado.value.http_status == 502


async def test_erro_de_conexao_e_retentado_ate_o_limite_e_depois_vira_provider_error(
    sem_espera: None,
) -> None:
    cliente = _ClienteFalso(APIConnectionError(request=_requisicao()))
    adaptador = OpenAICompatibleLLM(_settings(), client=cliente)  # type: ignore[arg-type]

    with pytest.raises(ProviderError) as capturado:
        await adaptador.chat(MENSAGENS)

    assert cliente.completions.tentativas == RETRY_ATTEMPTS, (
        f"falha de conexao e transitoria: esperava {RETRY_ATTEMPTS} tentativas, "
        f"houve {cliente.completions.tentativas}"
    )
    assert capturado.value.details["cause"] == "APIConnectionError"


async def test_rate_limit_tambem_e_retentado_antes_de_desistir(sem_espera: None) -> None:
    cliente = _ClienteFalso(_erro_de_limite())
    adaptador = OpenAICompatibleLLM(_settings(), client=cliente)  # type: ignore[arg-type]

    with pytest.raises(RateLimitedError):
        await adaptador.chat(MENSAGENS)

    assert cliente.completions.tentativas == RETRY_ATTEMPTS


# --------------------------------------------------------------------------- #
# OpenAICompatibleLLM — traducao da resposta
# --------------------------------------------------------------------------- #
class _RespostaDoHub:
    """Resposta minima do SDK, com o formato que `_to_domain` sabe ler."""

    def __init__(self, conteudo: str, usage: object | None) -> None:
        mensagem = type("_Msg", (), {"content": conteudo})()
        escolha = type("_Choice", (), {"message": mensagem, "finish_reason": "stop"})()
        self.choices = [escolha]
        self.usage = usage
        self.model = "modelo-hub"
        self.id = "resp-1"


class _CompletionsQueResponde:
    """`client.chat.completions` que devolve uma resposta pronta e guarda o payload."""

    def __init__(self, resposta: _RespostaDoHub) -> None:
        self._resposta = resposta
        self.payloads: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _RespostaDoHub:
        """Registra o corpo montado pelo adaptador e devolve a resposta programada."""
        self.payloads.append(kwargs)
        return self._resposta


class _ClienteQueResponde:
    """Duble que devolve uma resposta pronta em vez de falhar."""

    def __init__(self, resposta: _RespostaDoHub) -> None:
        self.completions = _CompletionsQueResponde(resposta)
        self.chat = type("_Chat", (), {"completions": self.completions})()

    @property
    def payloads(self) -> list[dict[str, Any]]:
        """Corpos enviados ao SDK, na ordem das chamadas."""
        return self.completions.payloads


async def test_resposta_do_hub_com_usage_reportado_e_preservada() -> None:
    usage = type("_U", (), {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})()
    cliente = _ClienteQueResponde(_RespostaDoHub("resposta do hub", usage))
    adaptador = OpenAICompatibleLLM(_settings(), client=cliente)  # type: ignore[arg-type]

    resposta = await adaptador.chat(MENSAGENS)

    assert resposta.content == "resposta do hub"
    assert resposta.usage.total_tokens == 18
    assert resposta.raw["usage_estimated"] is False


async def test_resposta_sem_usage_cai_na_estimativa_do_eco() -> None:
    cliente = _ClienteQueResponde(_RespostaDoHub("resposta do hub", None))
    adaptador = OpenAICompatibleLLM(_settings(), client=cliente)  # type: ignore[arg-type]

    resposta = await adaptador.chat(MENSAGENS)

    prompt_texto = "".join(mensagem.content for mensagem in MENSAGENS)
    assert resposta.usage == estimate_usage(prompt_texto, "resposta do hub")
    assert resposta.raw["usage_estimated"] is True


async def test_resposta_sem_choices_vira_provider_error() -> None:
    vazia = _RespostaDoHub("", None)
    vazia.choices = []
    cliente = _ClienteQueResponde(vazia)
    adaptador = OpenAICompatibleLLM(_settings(), client=cliente)  # type: ignore[arg-type]

    with pytest.raises(ProviderError):
        await adaptador.chat(MENSAGENS)


async def test_payload_aplica_os_padroes_de_settings_e_os_parametros_da_chamada() -> None:
    cliente = _ClienteQueResponde(_RespostaDoHub("ok", None))
    adaptador = OpenAICompatibleLLM(
        _settings(temperature=0.7, max_tokens=128),
        client=cliente,  # type: ignore[arg-type]
    )

    await adaptador.chat(MENSAGENS, stop=["FIM"], response_format={"type": "json_object"})

    enviado = cliente.payloads[-1]
    assert enviado["model"] == "modelo-hub"
    assert enviado["temperature"] == 0.7
    assert enviado["max_tokens"] == 128
    assert enviado["stop"] == ["FIM"]
    assert enviado["response_format"] == {"type": "json_object"}
    assert [item["role"] for item in enviado["messages"]] == ["system", "user"]
