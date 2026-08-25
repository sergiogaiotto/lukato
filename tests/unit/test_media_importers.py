"""Testes de unidade do caminho offline do AdWatch (SPEC-0010 secao 3.1).

Sem FFmpeg, sem GPU e sem rede, a linha do tempo multimodal entra no sistema por
importacao JSON. Os tres importadores sao funcoes puras e sao o que este arquivo
mais exercita: os formatos aceitos de transcricao (lista simples, objeto com
`words`, saida do WhisperX com `segments[].words[]`), os sinonimos de campo, a
ordenacao por `start` e — o detalhe que faz diferenca em uma transcricao de dez
mil palavras — o **indice** do item problematico na mensagem de erro.

A segunda metade prova a promessa de degradacao: importar `lukato.adapters.media`
nunca puxa dependencia opcional, `MediaToolbox.capabilities()` reporta `False`
para tudo neste ambiente, e chamar uma capacidade ausente levanta
`UnsupportedCapability` com a instrucao de instalacao — nunca `ImportError`.
"""

from __future__ import annotations

from typing import Any

import pytest

from lukato.adapters.media import (
    CAPABILITY_HINTS,
    FFmpegMediaProbe,
    OcrImporter,
    PaddleOCRAdapter,
    PySceneDetectCuts,
    QwenVisionJudge,
    SceneImporter,
    TranscriptImporter,
    WhisperXASR,
    build_media_toolbox,
    capability_report,
)
from lukato.config.settings import Settings
from lukato.domain.errors import UnsupportedCapability, ValidationError
from lukato.domain.models.adwatch import Commercial
from lukato.domain.ports.media import MediaToolbox
from lukato.domain.types import new_id

pytestmark = pytest.mark.unit

MIDIA = "/tmp/nao-existe.mp4"
"""Caminho fictício: nenhum teste chega a abrir arquivo algum."""


@pytest.fixture
def settings_offline() -> Settings:
    """`Settings` sem credencial de LLM: o juiz multimodal fica indisponivel."""
    return Settings(
        _env_file=None,
        llm={"provider": "echo", "api_key": None},
        embedding={"provider": "hashing"},
    )


# --------------------------------------------------------------------------- #
# TranscriptImporter — os tres formatos aceitos
# --------------------------------------------------------------------------- #
async def test_transcricao_em_lista_simples_de_palavras() -> None:
    palavras = TranscriptImporter.parse(
        [
            {"word": "aproveite", "start": 12.0, "end": 12.4},
            {"word": "agora", "start": 12.4, "end": 12.8, "score": 0.87},
        ]
    )

    assert [palavra.word for palavra in palavras] == ["aproveite", "agora"]
    assert palavras[1].score == pytest.approx(0.87)


async def test_transcricao_em_objeto_com_a_chave_words() -> None:
    palavras = TranscriptImporter.parse({"words": [{"word": "oferta", "start": 1.0, "end": 1.5}]})

    assert len(palavras) == 1
    assert palavras[0].word == "oferta"


async def test_transcricao_no_formato_whisperx_com_segmentos_e_achatada() -> None:
    payload = {
        "segments": [
            {"text": "primeiro", "words": [{"word": "claro", "start": 0.0, "end": 0.5}]},
            {"text": "segundo", "words": [{"word": "movel", "start": 0.5, "end": 1.0}]},
        ]
    }

    palavras = TranscriptImporter.parse(payload)

    assert [palavra.word for palavra in palavras] == ["claro", "movel"]


async def test_transcricao_aceita_os_campos_alternativos_de_cada_atributo() -> None:
    payload = {
        "words": [
            {
                "text": "promocao",
                "start_time": 3.0,
                "end_time": 3.6,
                "probability": 0.42,
                "speaker": "SPEAKER_01",
            }
        ]
    }

    palavra = TranscriptImporter.parse(payload)[0]

    assert palavra.word == "promocao"
    assert palavra.start == pytest.approx(3.0)
    assert palavra.end == pytest.approx(3.6)
    assert palavra.score == pytest.approx(0.42)
    assert palavra.speaker == "SPEAKER_01"


async def test_transcricao_e_devolvida_ordenada_por_start() -> None:
    palavras = TranscriptImporter.parse(
        [
            {"word": "terceira", "start": 5.0, "end": 5.5},
            {"word": "primeira", "start": 1.0, "end": 1.5},
            {"word": "segunda", "start": 3.0, "end": 3.5},
        ]
    )

    assert [palavra.word for palavra in palavras] == ["primeira", "segunda", "terceira"]
    assert [palavra.start for palavra in palavras] == sorted(palavra.start for palavra in palavras)


async def test_transcricao_com_item_invalido_aponta_o_indice_na_mensagem() -> None:
    with pytest.raises(ValidationError) as capturado:
        TranscriptImporter.parse(
            [
                {"word": "ok", "start": 0.0, "end": 0.4},
                {"word": "ok", "start": 0.4, "end": 0.8},
                {"word": "sem-fim", "start": 0.8},
            ]
        )

    erro = capturado.value
    assert erro.details["index"] == 2, (
        f"o erro precisa nomear o item problematico; details={erro.details}"
    )
    assert "palavra 2" in str(erro)


async def test_transcricao_com_intervalo_invertido_e_recusada_com_o_indice() -> None:
    with pytest.raises(ValidationError) as capturado:
        TranscriptImporter.parse([{"word": "invertida", "start": 9.0, "end": 2.0}])

    assert capturado.value.details == {"index": 0, "start": 9.0, "end": 2.0}


async def test_transcricao_com_item_que_nao_e_objeto_e_recusada() -> None:
    with pytest.raises(ValidationError) as capturado:
        TranscriptImporter.parse([{"word": "ok", "start": 0.0, "end": 1.0}, "texto solto"])

    assert capturado.value.details["index"] == 1
    assert capturado.value.details["received_type"] == "str"


async def test_transcricao_sem_lista_reconhecida_explica_as_chaves_aceitas() -> None:
    with pytest.raises(ValidationError) as capturado:
        TranscriptImporter.parse({"resultado": []})

    assert "words" in capturado.value.details["expected"]


# --------------------------------------------------------------------------- #
# SceneImporter
# --------------------------------------------------------------------------- #
async def test_cenas_sao_ordenadas_por_start_e_reindexadas() -> None:
    cortes = SceneImporter.parse(
        {
            "scenes": [
                {"start": 30.0, "end": 45.0},
                {"start": 0.0, "end": 15.0, "kind": "fade"},
                {"start": 15.0, "end": 30.0},
            ]
        }
    )

    assert [corte.index for corte in cortes] == [0, 1, 2]
    assert [corte.start for corte in cortes] == [0.0, 15.0, 30.0]
    assert cortes[0].kind == "fade"


async def test_cenas_aceitam_o_formato_de_par_start_end() -> None:
    cortes = SceneImporter.parse([[0.0, 2.5], [2.5, 6.0]])

    assert [(corte.start, corte.end) for corte in cortes] == [(0.0, 2.5), (2.5, 6.0)]
    assert {corte.kind for corte in cortes} == {"cut"}


async def test_cena_com_kind_desconhecido_e_recusada_apontando_os_aceitos() -> None:
    with pytest.raises(ValidationError) as capturado:
        SceneImporter.parse([{"start": 0.0, "end": 1.0, "kind": "zoom"}])

    assert capturado.value.details["index"] == 0
    assert capturado.value.details["allowed"] == ["cut", "fade"]


async def test_cena_em_par_com_tamanho_errado_e_recusada() -> None:
    with pytest.raises(ValidationError) as capturado:
        SceneImporter.parse([[0.0, 1.0, 2.0]])

    assert capturado.value.details["index"] == 0
    assert capturado.value.details["length"] == 3


# --------------------------------------------------------------------------- #
# OcrImporter
# --------------------------------------------------------------------------- #
async def test_ocr_le_texto_intervalo_confianca_e_caixa_delimitadora() -> None:
    textos = OcrImporter.parse(
        {
            "ocr": [
                {
                    "text": "ASSINE JA",
                    "start": 8.0,
                    "end": 10.0,
                    "confidence": 0.93,
                    "bbox": [10, 20, 200, 60],
                }
            ]
        }
    )

    assert textos[0].text == "ASSINE JA"
    assert textos[0].bbox == (10, 20, 200, 60)
    assert textos[0].confidence == pytest.approx(0.93)


async def test_ocr_aceita_poligono_de_pontos_como_caixa() -> None:
    textos = OcrImporter.parse(
        [{"text": "OFERTA", "start": 1.0, "bbox": [[5, 8], [50, 8], [50, 30], [5, 30]]}]
    )

    assert textos[0].bbox == (5, 8, 50, 30)
    assert textos[0].end == pytest.approx(1.0), "sem `end`, o fim vale o proprio `start`"


async def test_ocr_e_ordenado_por_start() -> None:
    textos = OcrImporter.parse(
        [
            {"text": "segundo", "start": 9.0, "end": 10.0},
            {"text": "primeiro", "start": 2.0, "end": 3.0},
        ]
    )

    assert [texto.text for texto in textos] == ["primeiro", "segundo"]


async def test_ocr_apara_confianca_fora_da_faixa_de_zero_a_um() -> None:
    textos = OcrImporter.parse([{"text": "X", "start": 0.0, "end": 1.0, "confidence": 42.0}])

    assert textos[0].confidence == pytest.approx(1.0)


async def test_ocr_com_texto_vazio_e_recusado_apontando_o_campo() -> None:
    with pytest.raises(ValidationError) as capturado:
        OcrImporter.parse([{"text": "   ", "start": 0.0, "end": 1.0}])

    assert capturado.value.details == {"index": 0, "field": "text"}


# --------------------------------------------------------------------------- #
# Adaptadores multimodais: import seguro e degradacao
# --------------------------------------------------------------------------- #
def test_todos_os_adaptadores_de_midia_sao_instanciaveis_sem_dependencia_opcional(
    settings_offline: Settings,
) -> None:
    adaptadores = [
        FFmpegMediaProbe(),
        WhisperXASR(),
        PaddleOCRAdapter(),
        PySceneDetectCuts(),
        QwenVisionJudge(None, settings_offline),
    ]

    assert all(adaptador is not None for adaptador in adaptadores)
    assert all(adaptador.available is False for adaptador in adaptadores), (
        "neste ambiente offline nenhuma dependencia multimodal esta instalada"
    )


def test_media_toolbox_reporta_todas_as_capacidades_indisponiveis(
    settings_offline: Settings,
) -> None:
    toolbox = build_media_toolbox(settings_offline)

    capacidades = toolbox.capabilities()

    assert set(capacidades) == {"probe", "asr", "ocr", "scenes", "vision"}
    assert capacidades == dict.fromkeys(capacidades, False)


def test_capability_report_explica_como_habilitar_cada_capacidade_ausente(
    settings_offline: Settings,
) -> None:
    relatorio = capability_report(build_media_toolbox(settings_offline))

    for nome, detalhe in relatorio.items():
        assert detalhe["available"] is False
        assert detalhe["adapter"], f"a capacidade {nome} precisa nomear o adaptador"
        assert detalhe["hint"] == CAPABILITY_HINTS[nome]
        assert len(detalhe["hint"]) > 20, "a instrucao tem de ser util, nao um rotulo"


def test_media_toolbox_vazia_reporta_tudo_indisponivel() -> None:
    assert MediaToolbox().capabilities() == {
        "probe": False,
        "asr": False,
        "ocr": False,
        "scenes": False,
        "vision": False,
    }


def _pular_se_disponivel(adaptador: Any, nome: str) -> None:
    """Pula o teste quando a dependencia opcional existe nesta maquina."""
    if adaptador.available:
        pytest.skip(f"a capacidade '{nome}' esta instalada nesta maquina")


async def test_probe_indisponivel_levanta_unsupported_capability_com_instrucao() -> None:
    probe = FFmpegMediaProbe()
    _pular_se_disponivel(probe, "probe")

    with pytest.raises(UnsupportedCapability) as capturado:
        await probe.probe(MIDIA)

    erro = capturado.value
    assert erro.http_status == 501
    assert erro.details["capability"] == "probe"
    assert "pip install" in erro.details["hint"] or "apt" in erro.details["hint"]


async def test_asr_indisponivel_levanta_unsupported_capability_com_instrucao() -> None:
    asr = WhisperXASR()
    _pular_se_disponivel(asr, "asr")

    with pytest.raises(UnsupportedCapability) as capturado:
        await asr.transcribe("/tmp/audio.wav")

    assert capturado.value.details["capability"] == "asr"
    assert "requirements-media.txt" in capturado.value.details["hint"]


async def test_ocr_indisponivel_levanta_unsupported_capability_com_instrucao() -> None:
    ocr = PaddleOCRAdapter()
    _pular_se_disponivel(ocr, "ocr")

    with pytest.raises(UnsupportedCapability) as capturado:
        await ocr.extract(MIDIA, start=0.0, end=5.0)

    assert capturado.value.details["capability"] == "ocr"
    assert capturado.value.details["missing"], "o erro tem de listar os pacotes que faltam"


async def test_detector_de_cenas_indisponivel_levanta_unsupported_capability() -> None:
    cenas = PySceneDetectCuts()
    _pular_se_disponivel(cenas, "scenes")

    with pytest.raises(UnsupportedCapability) as capturado:
        await cenas.detect(MIDIA)

    assert capturado.value.details["capability"] == "scenes"
    assert capturado.value.details["package"] == "scenedetect"


async def test_juiz_multimodal_sem_credencial_levanta_unsupported_capability(
    settings_offline: Settings,
) -> None:
    juiz = QwenVisionJudge(None, settings_offline)
    comercial = Commercial(
        id=new_id(),
        commercial_id="COM_000234",
        campaign="Verao",
        brand="Claro",
        text="Aproveite o plano de verao.",
    )

    with pytest.raises(UnsupportedCapability) as capturado:
        await juiz.verify(
            media_uri=MIDIA,
            start=0.0,
            end=30.0,
            commercial=comercial,
            transcript_excerpt="aproveite o plano",
        )

    assert capturado.value.details["capability"] == "vision"
    assert capturado.value.details["has_api_key"] is False


async def test_adaptador_de_midia_valida_a_entrada_antes_de_falar_de_disponibilidade() -> None:
    with pytest.raises(ValidationError):
        await FFmpegMediaProbe().probe("   ")
    with pytest.raises(ValidationError):
        await WhisperXASR().transcribe("")
    with pytest.raises(ValidationError):
        await PySceneDetectCuts().detect("")
