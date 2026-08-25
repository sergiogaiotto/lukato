"""Cenario de ponta a ponta do funil de scoring do AdWatch, todo em memoria.

Sem banco, sem rede, sem FFmpeg e sem GPU: o pipeline da SPEC-0010 secao 3 e
montado aqui com as pecas puras do dominio mais as funcoes puras de fingerprint
da camada de aplicacao e o `HashingEmbedder` (embeddings deterministicos)::

    transcricao importada -> janelas deslizantes -> filtro barato por keyword
      -> sinais (lexico / semantico / OCR / visual / duracao) -> fusao -> decisao
      -> supressao de sobreposicao -> refino de fronteira

**O cenario.** Cinco minutos de programa (`0` a `300` s) com um catalogo de tres
comerciais:

* `COM_000001` (Claro) **vai ao ar entre 120 s e 150 s**, dito com variacao
  lexical em relacao ao texto catalogado, com o letreiro "Claro internet" na tela;
* `COM_000002` (Vivo) **nunca vai ao ar**: suas palavras aparecem espalhadas
  pelo programa, em regioes distintas e **fora da ordem** do enunciado
  (`velocidade` aos 45 s, `casa` aos 70 s, `vivo` aos 175 s, `fibra`/`chega` aos
  205 s, `verdade` aos 235 s);
* `COM_000003` (Banco Azul) nao tem nenhuma palavra no ar — nem chega a virar
  candidato, barrado pelo filtro barato da secao 3.4.

Isto cobre os criterios de aceite 3, 4 e 5 da SPEC-0010 secao 6 na parte de
dominio puro. O criterio 2 (a rota `POST /media/{id}/detect`) e coberto pelos
testes de integracao da API.

**Por que o comercial 1 chega a `ACCEPTED`.** Sem OCR e sem juiz multimodal o
teto do score e `0.85` (o `visual_match` apenas herda o `speech_match`), abaixo
do `accept_threshold` de `0.90`. E o letreiro na tela que fecha a conta — como a
SPEC pretende: casamento puramente textual e forte, mas nao e prova.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Final

import pytest

from lukato.adapters.embeddings.hashing import HashingEmbedder
from lukato.application.use_cases.adwatch import fingerprint_draft
from lukato.config.settings import AdWatchSettings, Settings
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    DetectionCandidate,
    DetectionStatus,
)
from lukato.domain.services.matching import (
    BoundaryRefiner,
    CandidateBuilder,
    LexicalMatcher,
    NonMaximumSuppression,
    OrderMatcher,
    ScoreFusion,
    SemanticMatcher,
    SlidingWindowBuilder,
    TextWindow,
)
from lukato.domain.services.text_normalizer import normalize
from lukato.domain.types import Id
from tests.factories import make_commercial, make_ocr, make_scenes, make_transcript

pytestmark = pytest.mark.unit

# --------------------------------------------------------------------------- #
# O material do cenario
# --------------------------------------------------------------------------- #
VEICULACAO: Final[tuple[float, float]] = (120.0, 150.0)
"""Intervalo em que o comercial 1 realmente vai ao ar."""

TOLERANCIA_DE_BORDA: Final[float] = 5.0
"""Erro maximo aceito nas bordas da deteccao, em segundos."""

CORTES_DE_CENA: Final[tuple[tuple[float, float], ...]] = ((119.5, 150.5),)
"""Cortes de cena que delimitam o bloco comercial no video."""

FALA_DO_COMERCIAL_1: Final[str] = (
    "Na Claro você tem mais internet pra aproveitar tudo o que você gosta"
)
"""Como o locutor de fato disse a peca no ar — variacao lexical do texto catalogado."""

TRANSCRICAO: Final[tuple[tuple[str, float, float], ...]] = (
    ("bom dia e bem vindos ao nosso programa desta manha", 0.0, 20.0),
    ("hoje falamos sobre a previsao do tempo e sobre o transito na capital", 20.0, 45.0),
    ("a velocidade dos carros na avenida principal preocupa os moradores", 45.0, 70.0),
    ("nossa reportagem visitou uma casa reformada no centro da cidade", 70.0, 95.0),
    ("depois do intervalo teremos a entrevista com o tecnico do time", 95.0, 112.0),
    ("agora vamos aos nossos anunciantes fiquem conosco no intervalo comercial", 112.0, 120.0),
    (FALA_DO_COMERCIAL_1, 120.0, 150.0),
    ("voltamos ao estudio com a analise do nosso comentarista esportivo agora", 150.0, 158.0),
    ("a entrevista traz as noticias do fim de semana na regiao", 158.0, 175.0),
    ("o publico compareceu vivo e animado ao estadio na tarde de ontem", 175.0, 205.0),
    ("a fibra otica do novo cabo submarino chega ao litoral do estado", 205.0, 235.0),
    ("na verdade a expectativa e de mais investimento no proximo ano", 235.0, 265.0),
    ("obrigado por acompanhar e ate a proxima edicao do nosso jornal", 265.0, 300.0),
)
"""Cinco minutos de programa: o comercial 1 no meio, o comercial 2 em migalhas fora de ordem."""

CATALOGO: Final[tuple[dict[str, object], ...]] = (
    {
        "code": "COM_000001",
        "brand": "Claro",
        "campaign": "Claro Internet",
        "text": "Na Claro você tem muito mais internet para aproveitar tudo que gosta",
        "duration_expected": 30.0,
    },
    {
        "code": "COM_000002",
        "brand": "Vivo",
        "campaign": "Vivo Fibra",
        "text": "Vivo fibra chega na sua casa com velocidade de verdade",
        "duration_expected": 30.0,
    },
    {
        "code": "COM_000003",
        "brand": "Banco Azul",
        "campaign": "Conta Digital",
        "text": "Abra sua conta digital no Banco Azul e peca o cartao sem anuidade",
        "duration_expected": 15.0,
    },
)
"""Tres comerciais catalogados; so o primeiro esta no ar."""

PESOS_SO_LEXICO: Final[AdWatchSettings] = AdWatchSettings(
    weight_lexical=1.0,
    weight_semantic=0.0,
    weight_ocr=0.0,
    weight_visual=0.0,
    weight_duration=0.0,
)
"""Configuracao degenerada: o score passa a ser exatamente o `speech_match`."""

PESOS_SO_DURACAO: Final[AdWatchSettings] = AdWatchSettings(
    weight_lexical=0.0,
    weight_semantic=0.0,
    weight_ocr=0.0,
    weight_visual=0.0,
    weight_duration=1.0,
)
"""Configuracao degenerada: o score passa a ser exatamente o `duration_match`."""


# --------------------------------------------------------------------------- #
# Montagem do cenario (uma vez por sessao, deterministica)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Cenario:
    """Tudo o que o funil precisa, ja calculado: catalogo, janelas e vetores."""

    comerciais: tuple[Commercial, ...]
    fingerprints: dict[Id, AdFingerprint]
    janelas: tuple[TextWindow, ...]
    vetores: tuple[list[float], ...]

    def codigo(self, commercial_id: Id) -> str:
        """Codigo de negocio do comercial pelo identificador interno."""
        return next(item.commercial_id for item in self.comerciais if item.id == commercial_id)


@lru_cache(maxsize=1)
def cenario() -> Cenario:
    """Monta (uma unica vez) a transcricao, o catalogo, as janelas e os embeddings.

    Nada aqui depende de ordem de teste nem de relogio: `make_transcript` usa
    carimbos fixos e o `HashingEmbedder` e uma funcao pura do texto.
    """
    embedder = HashingEmbedder(dimensions=1024)
    transcricao = make_transcript(list(TRANSCRICAO))
    ocr = make_ocr([("Claro internet", 121.0, 149.0)])

    comerciais = tuple(
        make_commercial(
            str(item["code"]),
            brand=str(item["brand"]),
            campaign=str(item["campaign"]),
            text=str(item["text"]),
            duration_expected=float(item["duration_expected"]),  # type: ignore[arg-type]
            keywords=(),
            key_phrases=(),
        )
        for item in CATALOGO
    )
    fingerprints: dict[Id, AdFingerprint] = {}
    for comercial in comerciais:
        rascunho = fingerprint_draft(comercial)
        fingerprints[comercial.id] = rascunho.model_copy(
            update={"embedding": embedder.vector(rascunho.normalized_text)}
        )

    janelas = tuple(SlidingWindowBuilder().build(transcricao, ocr=ocr))
    vetores = tuple(embedder.vector(janela.text) for janela in janelas)
    return Cenario(
        comerciais=comerciais, fingerprints=fingerprints, janelas=janelas, vetores=vetores
    )


def fusao_de(config: AdWatchSettings) -> ScoreFusion:
    """`ScoreFusion` montado a partir de `Settings.adwatch` (SPEC-0010 secao 3.6)."""
    return ScoreFusion(
        weight_lexical=config.weight_lexical,
        weight_semantic=config.weight_semantic,
        weight_ocr=config.weight_ocr,
        weight_visual=config.weight_visual,
        weight_duration=config.weight_duration,
    )


def funil(config: AdWatchSettings | None = None) -> list[DetectionCandidate]:
    """Roda o funil de matching completo e devolve todos os candidatos pontuados.

    Reproduz a secao 3 da SPEC-0010 sem tocar em banco: filtro barato por
    keyword, avaliacao dos cinco sinais e fusao com os pesos de `Settings`.
    Nao ha juiz multimodal disponivel, entao `visual_match` herda `speech_match`.
    """
    cfg = config or AdWatchSettings()
    dados = cenario()
    fusion = fusao_de(cfg)
    builder = CandidateBuilder(
        lexical=LexicalMatcher(),
        semantic=SemanticMatcher(),
        order=OrderMatcher(),
        fusion=fusion,
    )
    candidatos: list[DetectionCandidate] = []
    for janela, vetor in zip(dados.janelas, dados.vetores, strict=True):
        procurado = f" {normalize(janela.text)} {normalize(janela.ocr_text)} "
        for comercial in dados.comerciais:
            fingerprint = dados.fingerprints[comercial.id]
            chaves = [normalize(palavra) for palavra in fingerprint.keywords]
            if chaves and not any(f" {chave} " in procurado for chave in chaves):
                continue
            score, evidencia = builder.evaluate(janela, fingerprint, window_vec=vetor)
            candidatos.append(
                DetectionCandidate(
                    commercial_id=comercial.id,
                    commercial_code=comercial.commercial_id,
                    campaign=comercial.campaign,
                    start=janela.start,
                    end=janela.end,
                    score=round(score, 6),
                    evidence=evidencia,
                )
            )
    return candidatos


def classificados(
    config: AdWatchSettings | None = None,
) -> dict[DetectionStatus, list[DetectionCandidate]]:
    """Candidatos do funil agrupados por `DetectionStatus`."""
    cfg = config or AdWatchSettings()
    fusion = fusao_de(cfg)
    grupos: dict[DetectionStatus, list[DetectionCandidate]] = {
        status: [] for status in DetectionStatus
    }
    for candidato in funil(cfg):
        status = fusion.classify(
            candidato.score, accept=cfg.accept_threshold, review=cfg.review_threshold
        )
        grupos[status].append(candidato)
    return grupos


def melhor(candidatos: list[DetectionCandidate], codigo: str) -> DetectionCandidate | None:
    """Candidato de maior score para um codigo de comercial, se houver algum."""
    do_comercial = [item for item in candidatos if item.commercial_code == codigo]
    if not do_comercial:
        return None
    return max(do_comercial, key=lambda item: (item.score, -item.start))


# --------------------------------------------------------------------------- #
# Criterio 4 — o comercial no ar e detectado apesar da variacao lexical
# --------------------------------------------------------------------------- #
def test_comercial_um_e_aceito_com_intervalo_dentro_de_cinco_segundos() -> None:
    """A peca dita com outras palavras entre 120 s e 150 s vira deteccao aceita."""
    aceitos = classificados()[DetectionStatus.ACCEPTED]
    campeao = melhor(aceitos, "COM_000001")

    assert campeao is not None, "o comercial 1 deveria produzir ao menos uma deteccao aceita"
    inicio_real, fim_real = VEICULACAO
    assert abs(campeao.start - inicio_real) <= TOLERANCIA_DE_BORDA, (
        f"inicio detectado em {campeao.start}s, esperado {inicio_real}s +/- {TOLERANCIA_DE_BORDA}s"
    )
    assert abs(campeao.end - fim_real) <= TOLERANCIA_DE_BORDA, (
        f"fim detectado em {campeao.end}s, esperado {fim_real}s +/- {TOLERANCIA_DE_BORDA}s"
    )
    assert campeao.score >= AdWatchSettings().accept_threshold


def test_evidencia_do_comercial_aceito_e_multimodal_e_nao_inventa_juiz_visual() -> None:
    """A deteccao aceita se sustenta em fala, semantica, tela e duracao — sem VLM."""
    campeao = melhor(classificados()[DetectionStatus.ACCEPTED], "COM_000001")

    assert campeao is not None
    evidencia = campeao.evidence
    assert evidencia.speech_match > 0.85, "a variacao lexical tem de casar acima de 0.85"
    assert evidencia.semantic_match > 0.85
    assert evidencia.ocr_match == pytest.approx(1.0), "o letreiro 'Claro internet' esta na tela"
    assert evidencia.duration_match == pytest.approx(1.0), "30 s de janela para peca de 30 s"
    assert evidencia.order_ok is True
    assert evidencia.brand_detected == "Claro"
    assert evidencia.visual_match == pytest.approx(evidencia.speech_match), (
        "sem juiz multimodal o sinal visual herda a fala; nunca 1.0 inventado"
    )


def test_nenhuma_janela_aceita_escapa_da_veiculacao_real() -> None:
    """Precisao: toda janela aceita cai dentro de `[120, 150]`, nada vaza para o programa."""
    aceitos = classificados()[DetectionStatus.ACCEPTED]
    inicio_real, fim_real = VEICULACAO

    fora = [
        (item.commercial_code, item.start, item.end)
        for item in aceitos
        if item.start < inicio_real or item.end > fim_real
    ]

    assert aceitos, "o cenario precisa produzir alguma deteccao aceita"
    assert fora == [], f"janelas aceitas fora da veiculacao real: {fora}"


def test_supressao_temporal_nao_espalha_a_veiculacao_pelo_programa() -> None:
    """Depois da NMS as sobras continuam ancoradas na veiculacao (SPEC-0010 secao 3.7)."""
    aceitos = classificados()[DetectionStatus.ACCEPTED]

    consolidados = NonMaximumSuppression.suppress(aceitos)

    assert {item.commercial_code for item in consolidados} == {"COM_000001"}
    assert min(item.start for item in consolidados) == VEICULACAO[0]
    assert max(item.end for item in consolidados) == VEICULACAO[1]
    assert [item.start for item in consolidados] == sorted(item.start for item in consolidados)


# --------------------------------------------------------------------------- #
# Criterio 5 — palavras fora de ordem nao viram deteccao aceita
# --------------------------------------------------------------------------- #
def test_comercial_dois_com_palavras_fora_de_ordem_nao_chega_a_aceito() -> None:
    """As palavras da peca da Vivo estao no ar, mas espalhadas: nenhuma deteccao aceita."""
    todos = funil()
    campeao = melhor(todos, "COM_000002")
    aceitos = classificados()[DetectionStatus.ACCEPTED]

    assert campeao is not None, "o filtro por keyword deixa passar candidatos do comercial 2"
    assert campeao.score < AdWatchSettings().accept_threshold, (
        f"o melhor candidato do comercial 2 marcou {campeao.score:.4f} e nao pode ser aceito"
    )
    assert all(item.commercial_code != "COM_000002" for item in aceitos)


def test_comercial_dois_e_rejeitado_e_nao_apenas_encaminhado_a_revisao() -> None:
    """Palavras soltas em regioes distintas ficam abaixo ate do limiar de revisao."""
    config = AdWatchSettings()
    campeao = melhor(funil(), "COM_000002")

    assert campeao is not None
    assert campeao.score < config.review_threshold, (
        f"o comercial 2 marcou {campeao.score:.4f}; abaixo de {config.review_threshold} "
        "ele e descartado sem gastar o juiz multimodal"
    )


def test_janela_que_junta_palavras_do_comercial_dois_fora_de_ordem_perde_a_ordem() -> None:
    """Onde varias ancoras coincidem na janela, o `OrderMatcher` reprova a sequencia."""
    fora_de_ordem = [
        item
        for item in funil()
        if item.commercial_code == "COM_000002" and not item.evidence.order_ok
    ]

    assert fora_de_ordem, (
        "alguma janela deveria juntar ancoras do comercial 2 na ordem errada e ser penalizada"
    )
    assert max(item.score for item in fora_de_ordem) < AdWatchSettings().accept_threshold


def test_comercial_ausente_do_ar_nem_chega_a_ser_candidato() -> None:
    """Filtro barato da secao 3.4: sem keyword na janela, o fingerprint nao e avaliado."""
    assert melhor(funil(), "COM_000003") is None


# --------------------------------------------------------------------------- #
# Refino de fronteira com cortes de cena (SPEC-0010 secao 3.8)
# --------------------------------------------------------------------------- #
def test_cortes_de_cena_aproximam_as_bordas_da_deteccao() -> None:
    """Com cortes em 119.5 s e 150.5 s o refino encaixa as bordas e reduz o erro a zero."""
    campeao = melhor(classificados()[DetectionStatus.ACCEPTED], "COM_000001")
    assert campeao is not None
    cortes = make_scenes(list(CORTES_DE_CENA))
    borda_inicial, borda_final = CORTES_DE_CENA[0]

    erro_antes = abs(campeao.start - borda_inicial) + abs(campeao.end - borda_final)
    inicio, fim, refinado = BoundaryRefiner().refine(campeao.start, campeao.end, cortes)
    erro_depois = abs(inicio - borda_inicial) + abs(fim - borda_final)

    assert refinado is True, "o refino deveria marcar a deteccao como encaixada em corte de cena"
    assert (inicio, fim) == (borda_inicial, borda_final)
    assert erro_depois < erro_antes, (
        f"o refino tinha de aproximar as bordas: erro {erro_antes:.2f}s -> {erro_depois:.2f}s"
    )
    assert erro_depois == pytest.approx(0.0)


def test_sem_cortes_de_cena_a_deteccao_mantem_as_bordas_da_janela() -> None:
    """Sem deteccao de cena o refino nao inventa fronteira nem marca a deteccao."""
    campeao = melhor(classificados()[DetectionStatus.ACCEPTED], "COM_000001")
    assert campeao is not None

    assert BoundaryRefiner().refine(campeao.start, campeao.end, []) == (
        campeao.start,
        campeao.end,
        False,
    )


# --------------------------------------------------------------------------- #
# Criterio 3 — pesos e limiares vem de Settings
# --------------------------------------------------------------------------- #
def test_pesos_e_limiares_do_settings_sao_os_normativos_da_spec(settings: Settings) -> None:
    """`Settings.adwatch` entrega exatamente os pesos e limiares da SPEC-0010."""
    config = settings.adwatch

    assert config.weights() == {
        "lexical": 0.40,
        "semantic": 0.25,
        "ocr": 0.15,
        "visual": 0.15,
        "duration": 0.05,
    }
    assert (config.accept_threshold, config.review_threshold) == (0.90, 0.60)
    assert config.window_sizes == [15.0, 30.0, 60.0]
    assert config.window_stride == 5.0
    assert fusao_de(config).weights() == config.weights(), (
        "a fusao tem de usar os pesos de Settings, nao uma copia interna"
    )


def test_peso_total_no_lexico_promove_o_comercial_dois_de_rejeitado_para_revisao() -> None:
    """Trocar os pesos muda a decisao de forma previsivel: `S` vira o proprio `speech_match`."""
    padrao = melhor(funil(), "COM_000002")
    so_lexico = melhor(funil(PESOS_SO_LEXICO), "COM_000002")
    assert padrao is not None and so_lexico is not None

    fusao_padrao = ScoreFusion()
    fusao_lexica = fusao_de(PESOS_SO_LEXICO)

    assert so_lexico.score == pytest.approx(so_lexico.evidence.speech_match, abs=1e-6), (
        "com peso 1.0 no lexico o score final e exatamente o sinal de fala"
    )
    assert fusao_padrao.classify(padrao.score) is DetectionStatus.REJECTED
    assert fusao_lexica.classify(so_lexico.score) is DetectionStatus.NEEDS_REVIEW, (
        f"apagar os demais sinais promove o comercial 2 de {padrao.score:.4f} para "
        f"{so_lexico.score:.4f} e o joga na faixa de revisao"
    )


def test_peso_total_na_duracao_destroi_a_precisao_e_aceita_o_comercial_errado() -> None:
    """Sem os pesos da SPEC a decisao passa a ser so o tamanho da janela — e erra."""
    so_duracao = funil(PESOS_SO_DURACAO)
    fusao = fusao_de(PESOS_SO_DURACAO)

    aceitos = {
        item.commercial_code
        for item in so_duracao
        if fusao.classify(item.score) is DetectionStatus.ACCEPTED
    }
    campeao = melhor(so_duracao, "COM_000002")

    assert campeao is not None
    assert campeao.score == pytest.approx(campeao.evidence.duration_match, abs=1e-6)
    assert "COM_000002" in aceitos, (
        "com o score reduzido a duracao, qualquer janela de 30 s vira deteccao aceita — "
        "e a precisao da SPEC depende justamente dos outros quatro pesos"
    )


def test_elevar_o_limiar_de_aceite_pelo_settings_manda_o_comercial_um_para_revisao() -> None:
    """Os limiares tambem vem de `Settings`: subir `accept_threshold` rebaixa a decisao."""
    exigente = AdWatchSettings(accept_threshold=0.98, review_threshold=0.60)
    campeao = melhor(funil(), "COM_000001")
    assert campeao is not None

    assert ScoreFusion().classify(campeao.score) is DetectionStatus.ACCEPTED
    assert (
        ScoreFusion().classify(
            campeao.score,
            accept=exigente.accept_threshold,
            review=exigente.review_threshold,
        )
        is DetectionStatus.NEEDS_REVIEW
    )


# --------------------------------------------------------------------------- #
# Diagnostico legivel (visivel com `pytest -s`)
# --------------------------------------------------------------------------- #
def test_placar_do_cenario_e_estavel_e_auditavel() -> None:
    """Imprime e confere o placar do funil — a foto que se olha quando algo regride."""
    dados = cenario()
    todos = funil()
    fusao = ScoreFusion()
    ordenados = sorted(todos, key=lambda item: -item.score)

    print(f"\njanelas={len(dados.janelas)} candidatos={len(todos)}")
    for item in ordenados[:6]:
        evidencia = item.evidence
        print(
            f"  {item.commercial_code} [{item.start:6.1f},{item.end:6.1f}] "
            f"S={item.score:.4f} fala={evidencia.speech_match:.3f} "
            f"sem={evidencia.semantic_match:.3f} ocr={evidencia.ocr_match:.3f} "
            f"vis={evidencia.visual_match:.3f} dur={evidencia.duration_match:.3f} "
            f"ordem={evidencia.order_ok} -> {fusao.classify(item.score).value}"
        )
    for codigo in ("COM_000001", "COM_000002", "COM_000003"):
        campeao = melhor(todos, codigo)
        resumo = "sem candidato" if campeao is None else f"{campeao.score:.4f}"
        print(f"  melhor {codigo}: {resumo}")

    assert len(dados.janelas) == 177, (
        f"o cenario de 5 min com [15, 30, 60] e passo 5 rende 177 janelas, "
        f"obtido {len(dados.janelas)}"
    )
    assert ordenados[0].commercial_code == "COM_000001"
    assert (ordenados[0].start, ordenados[0].end) == VEICULACAO
