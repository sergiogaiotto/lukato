"""Testes de unidade do motor de matching temporal do AdWatch (SPEC-0010 secao 3).

Tudo aqui e dominio puro: `lukato.domain.services.matching` nao faz I/O, nao abre
socket e nao le relogio. Os testes exercitam cada peca do funil da SPEC-0010
isoladamente, com dados fixos de :mod:`tests.factories`:

    janelas deslizantes -> sinais (lexico / semantico / ordem) -> fusao e decisao
    -> supressao de sobreposicao -> refino de fronteira

Dois pontos merecem destaque porque protegem contra regressao silenciosa:

* **Fallback lexico** — `LexicalMatcher` usa `rapidfuzz` quando existe e
  `difflib` quando nao existe. Os valores absolutos mudam entre os dois
  backends, mas a **ordenacao relativa** nao pode mudar: e ela que decide qual
  comercial ganha o rerank. O teste correspondente remove `rapidfuzz` de
  `sys.modules` e compara as duas ordenacoes.
* **Ausencia de juiz multimodal** — sem `VisionJudgePort`, `visual_match` herda
  `speech_match`. Nunca `1.0` inventado (SPEC-0010 secao 3.5).
"""

from __future__ import annotations

import math
import sys
from typing import Final

import pytest

from lukato.domain.errors import ValidationError
from lukato.domain.models.adwatch import DetectionCandidate, DetectionStatus
from lukato.domain.services.matching import (
    DEFAULT_ACCEPT_THRESHOLD,
    DEFAULT_IOU_THRESHOLD,
    DEFAULT_MAX_SHIFT,
    DEFAULT_ORDER_PENALTY,
    DEFAULT_REVIEW_THRESHOLD,
    DEFAULT_WINDOW_SIZES,
    DEFAULT_WINDOW_STRIDE,
    WEIGHT_SUM_TOLERANCE,
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
from tests.factories import (
    id_de,
    make_evidence,
    make_fingerprint,
    make_ocr,
    make_scenes,
    make_transcript,
)

pytestmark = pytest.mark.unit

# --------------------------------------------------------------------------- #
# Dados fixos
# --------------------------------------------------------------------------- #
SESSENTA_PALAVRAS: Final[str] = " ".join(f"palavra{indice:02d}" for indice in range(1, 61))
"""Sessenta palavras distintas; distribuidas em `[0, 60]` dao exatamente 1 s cada."""

TEXTO_DO_COMERCIAL: Final[str] = (
    "Na Claro você tem muito mais internet para aproveitar tudo que gosta"
)
"""Enunciado do comercial usado como referencia (SPEC-0010 secao 6, criterio 4)."""

VARIACAO_FALADA: Final[str] = "Na Claro você tem mais internet pra aproveitar tudo o que você gosta"
"""A mesma peca dita pelo locutor, com a variacao lexical real do ar."""

PARAFRASE_DISTANTE: Final[str] = "a claro oferece internet de sobra para quem gosta"
"""Fala da mesma marca, mas nao e o comercial: tem de ficar no meio da ordenacao."""

TEXTO_ALHEIO: Final[str] = "O time venceu o campeonato no ultimo minuto com um gol de falta"
"""Trecho do programa que nao tem nada a ver com o comercial."""

TEXTO_ALHEIO_DISTANTE: Final[str] = "receita de bolo de cenoura com cobertura de chocolate no forno"
"""Outro trecho alheio, ainda mais distante — usado para checar a ordenacao."""

ID_COMERCIAL: Final[str] = id_de("comercial", "COM_000001")
"""Identificador estavel do comercial de referencia."""


def transcricao_regular():
    """Transcricao de 60 palavras em `[0, 60]` — uma palavra por segundo."""
    return make_transcript([(SESSENTA_PALAVRAS, 0.0, 60.0)])


def fingerprint_de_referencia(**overrides):
    """`AdFingerprint` do comercial de referencia, com ajustes pontuais."""
    padrao = {
        "normalized_text": "na claro voce tem muito mais internet para aproveitar tudo que gosta",
        "keywords": ("claro", "internet", "aproveitar", "gosta"),
        "key_phrases": ("mais internet para aproveitar",),
        "duration": 30.0,
        "expected_brand": "Claro",
    }
    padrao.update(overrides)
    return make_fingerprint(ID_COMERCIAL, **padrao)


def candidato(code: str, start: float, end: float, score: float) -> DetectionCandidate:
    """Candidato minimo para os testes de supressao temporal."""
    return DetectionCandidate(
        commercial_id=id_de("comercial", code),
        commercial_code=code,
        start=start,
        end=end,
        score=score,
    )


@pytest.fixture
def matcher_sem_rapidfuzz(monkeypatch: pytest.MonkeyPatch) -> LexicalMatcher:
    """`LexicalMatcher` construido com `rapidfuzz` indisponivel (backend `difflib`).

    `sys.modules["rapidfuzz"] = None` faz o `import` de dentro do construtor
    levantar `ImportError`, que e exatamente o caminho de uma instalacao sem a
    biblioteca opcional. O `monkeypatch` restaura o `sys.modules` no teardown.
    """
    monkeypatch.setitem(sys.modules, "rapidfuzz", None)
    return LexicalMatcher()


# --------------------------------------------------------------------------- #
# SlidingWindowBuilder (SPEC-0010 secao 3.3)
# --------------------------------------------------------------------------- #
def test_tres_tamanhos_com_passo_cinco_geram_doze_janelas_por_tamanho() -> None:
    """`[15, 30, 60]` com passo 5 sobre 60 s produz 12 inicios e 36 janelas."""
    janelas = SlidingWindowBuilder().build(transcricao_regular())

    inicios = sorted({janela.start for janela in janelas})
    duracoes = sorted(round(janela.duration, 3) for janela in janelas)

    assert inicios == [float(passo * 5) for passo in range(12)], (
        f"os inicios deveriam andar de {DEFAULT_WINDOW_STRIDE} em "
        f"{DEFAULT_WINDOW_STRIDE} s de 0 a 55, obtido {inicios}"
    )
    assert len(janelas) == 36, f"12 inicios x 3 tamanhos = 36 janelas, obtido {len(janelas)}"
    for tamanho in DEFAULT_WINDOW_SIZES:
        assert duracoes.count(tamanho) == 12, (
            f"cada tamanho da SPEC deveria render 12 janelas; {tamanho} rendeu "
            f"{duracoes.count(tamanho)}"
        )


def test_toda_palavra_da_transcricao_cai_em_pelo_menos_uma_janela() -> None:
    """Cobertura: nenhuma palavra transcrita fica fora de todas as janelas."""
    transcricao = transcricao_regular()
    janelas = SlidingWindowBuilder().build(transcricao)

    descobertas = [
        palavra.word
        for palavra in transcricao.words
        if not any(
            janela.start <= palavra.end and palavra.start <= janela.end for janela in janelas
        )
    ]

    assert descobertas == [], f"palavras fora de qualquer janela: {descobertas}"


def test_janelas_saem_ordenadas_por_inicio_e_sem_intervalo_repetido() -> None:
    """A saida e estavel: ordenada por `(start, end)` e sem par repetido."""
    janelas = SlidingWindowBuilder().build(transcricao_regular())

    chaves = [(janela.start, janela.end) for janela in janelas]

    assert chaves == sorted(chaves), "as janelas deveriam sair ordenadas por (start, end)"
    assert len(chaves) == len(set(chaves)), "nenhuma janela pode repetir o par (start, end)"


def test_tamanho_de_janela_repetido_e_deduplicado_no_construtor() -> None:
    """`[15, 15.0, 30, 15]` colapsa em `(15.0, 30.0)` — nada de janela em dobro."""
    construtor = SlidingWindowBuilder(sizes=[15.0, 15, 30.0, 15.0])

    janelas = construtor.build(transcricao_regular())

    assert construtor.sizes == (15.0, 30.0), (
        f"tamanhos repetidos deveriam colapsar, obtido {construtor.sizes}"
    )
    assert len(janelas) == 24, f"12 inicios x 2 tamanhos = 24 janelas, obtido {len(janelas)}"


def test_janela_com_menos_palavras_que_o_minimo_e_descartada() -> None:
    """A ultima janela de `[15]` pega so 6 palavras; com `min_words=7` ela cai."""
    transcricao = transcricao_regular()

    com_minimo_baixo = SlidingWindowBuilder(sizes=[15.0], stride=5.0, min_words=6).build(
        transcricao
    )
    com_minimo_alto = SlidingWindowBuilder(sizes=[15.0], stride=5.0, min_words=7).build(transcricao)

    assert len(com_minimo_baixo) == 12
    assert len(com_minimo_alto) == 11, (
        "a janela [55, 70] tem 6 palavras e deveria ser descartada por min_words=7"
    )
    assert com_minimo_alto[-1].start == 50.0


def test_transcricao_vazia_nao_gera_janela_nenhuma() -> None:
    """Sem palavras nao ha linha do tempo — a lista sai vazia, sem levantar."""
    assert SlidingWindowBuilder().build(make_transcript([])) == []


def test_janela_carrega_apenas_o_ocr_do_proprio_intervalo() -> None:
    """O texto de tela entra na janela que o intersecta e so nela (secao 3.3)."""
    janelas = SlidingWindowBuilder(sizes=[15.0], stride=5.0).build(
        transcricao_regular(), ocr=make_ocr([("PROMOCAO", 10.0, 12.0)])
    )

    por_inicio = {janela.start: janela.ocr_text for janela in janelas}

    assert por_inicio[0.0] == "PROMOCAO", "a janela [0, 15] intersecta o OCR de [10, 12]"
    assert por_inicio[10.0] == "PROMOCAO"
    assert por_inicio[15.0] == "", "a janela [15, 30] nao intersecta o OCR e nao pode herda-lo"


@pytest.mark.parametrize(
    ("kwargs", "trecho"),
    [
        ({"sizes": []}, "tamanho de janela positivo"),
        ({"sizes": [0.0, -5.0]}, "tamanho de janela positivo"),
        ({"stride": 0.0}, "passo"),
        ({"stride": -1.0}, "passo"),
        ({"min_words": -1}, "min_words"),
    ],
)
def test_parametro_invalido_de_janelamento_e_recusado(kwargs: dict, trecho: str) -> None:
    """Janelamento sem tamanho positivo, sem passo ou com minimo negativo nao monta."""
    with pytest.raises(ValidationError) as erro:
        SlidingWindowBuilder(**kwargs)

    assert trecho in str(erro.value)


# --------------------------------------------------------------------------- #
# LexicalMatcher (SPEC-0010 secao 3.5, criterio de aceite 4)
# --------------------------------------------------------------------------- #
def test_variacao_lexical_do_enunciado_da_spec_pontua_acima_de_085() -> None:
    """A mesma peca dita com outras palavras continua sendo a mesma peca."""
    score = LexicalMatcher().score(TEXTO_DO_COMERCIAL, VARIACAO_FALADA)

    assert score > 0.85, (
        f"a variacao lexical do enunciado da SPEC deveria pontuar acima de 0.85, obtido {score:.4f}"
    )


def test_texto_alheio_fica_abaixo_de_05() -> None:
    """Trecho do programa que nao e o comercial nao pode competir com ele."""
    matcher = LexicalMatcher()

    for alheio in (TEXTO_ALHEIO, TEXTO_ALHEIO_DISTANTE):
        score = matcher.score(TEXTO_DO_COMERCIAL, alheio)
        assert score < 0.5, f"texto alheio deveria ficar abaixo de 0.5, obtido {score:.4f}"


def test_fallback_difflib_e_escolhido_quando_rapidfuzz_nao_esta_instalado(
    matcher_sem_rapidfuzz: LexicalMatcher,
) -> None:
    """Sem a biblioteca opcional o matcher se declara `difflib` e continua respondendo."""
    assert matcher_sem_rapidfuzz.backend == "difflib"
    assert matcher_sem_rapidfuzz.score(TEXTO_DO_COMERCIAL, VARIACAO_FALADA) > 0.85


def test_fallback_difflib_preserva_a_ordenacao_relativa_do_rapidfuzz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Os dois backends podem discordar do valor, nunca da ordem — e a ordem decide o rerank."""
    concorrentes = {
        "variacao": VARIACAO_FALADA,
        "parafrase": PARAFRASE_DISTANTE,
        "alheio": TEXTO_ALHEIO,
        "alheio_distante": TEXTO_ALHEIO_DISTANTE,
    }
    com_rapidfuzz = LexicalMatcher()
    if com_rapidfuzz.backend != "rapidfuzz":  # pragma: no cover - ambiente sem a lib opcional
        pytest.skip("rapidfuzz nao esta instalado: nao ha as duas ordenacoes para comparar")

    monkeypatch.setitem(sys.modules, "rapidfuzz", None)
    com_difflib = LexicalMatcher()

    def ordenacao(matcher: LexicalMatcher) -> list[str]:
        pontos = {
            nome: matcher.score(TEXTO_DO_COMERCIAL, texto) for nome, texto in concorrentes.items()
        }
        return sorted(pontos, key=lambda nome: (-pontos[nome], nome))

    ordem_rapidfuzz = ordenacao(com_rapidfuzz)
    ordem_difflib = ordenacao(com_difflib)

    assert com_difflib.backend == "difflib"
    assert ordem_rapidfuzz == ["variacao", "parafrase", "alheio", "alheio_distante"]
    assert ordem_difflib == ordem_rapidfuzz, (
        "trocar o backend lexico nao pode reordenar os candidatos: "
        f"rapidfuzz={ordem_rapidfuzz} difflib={ordem_difflib}"
    )


def test_texto_vazio_nao_produz_similaridade_lexica() -> None:
    """Ausencia de texto e ausencia de evidencia: `0.0`, nunca `1.0`."""
    matcher = LexicalMatcher()

    assert matcher.score("", TEXTO_DO_COMERCIAL) == 0.0
    assert matcher.score(TEXTO_DO_COMERCIAL, "   ") == 0.0


def test_keyword_presente_no_texto_vale_um_e_ausencia_de_keyword_vale_zero() -> None:
    """`best_keyword_score` e o sinal do OCR: contencao literal casa em cheio."""
    matcher = LexicalMatcher()

    assert matcher.best_keyword_score("assine CLARO internet hoje", ["claro"]) == 1.0
    assert matcher.best_keyword_score("assine claro internet hoje", []) == 0.0
    assert matcher.best_keyword_score("", ["claro"]) == 0.0


# --------------------------------------------------------------------------- #
# SemanticMatcher (SPEC-0010 secao 3.5)
# --------------------------------------------------------------------------- #
def test_cosseno_de_vetores_identicos_vale_um_e_similaridade_tambem() -> None:
    """Vetor contra ele mesmo: cosseno 1, similaridade 1."""
    vetor = [0.5, -0.25, 0.75, 1.0]

    assert SemanticMatcher.cosine(vetor, vetor) == pytest.approx(1.0)
    assert SemanticMatcher.similarity(vetor, vetor) == pytest.approx(1.0)


def test_vetores_ortogonais_tem_cosseno_zero_e_similaridade_meio() -> None:
    """O reescalonamento de `[-1, 1]` para `[0, 1]` poe a ortogonalidade em 0.5."""
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]

    assert SemanticMatcher.cosine(a, b) == pytest.approx(0.0)
    assert SemanticMatcher.similarity(a, b) == pytest.approx(0.5)


def test_vetores_opostos_tem_similaridade_zero() -> None:
    """Cosseno `-1` reescalado vira `0.0` — o piso do sinal semantico."""
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]

    assert SemanticMatcher.cosine(a, b) == pytest.approx(-1.0)
    assert SemanticMatcher.similarity(a, b) == pytest.approx(0.0)


def test_vetor_vazio_nulo_ou_de_outra_dimensao_devolve_cosseno_zero() -> None:
    """Sem vetor comparavel o cosseno e `0.0` (e a similaridade, o neutro 0.5)."""
    assert SemanticMatcher.cosine([], [1.0]) == 0.0
    assert SemanticMatcher.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert SemanticMatcher.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_rank_devolve_o_top_k_ordenado_por_similaridade_decrescente() -> None:
    """`rank` corta em `top_k` e entrega do mais parecido para o menos parecido."""
    consulta = [1.0, 0.0]
    catalogo = {
        "identico": [1.0, 0.0],
        "ortogonal": [0.0, 1.0],
        "oposto": [-1.0, 0.0],
        "quase": [0.9, 0.1],
    }

    ranking = SemanticMatcher().rank(consulta, catalogo, top_k=3)

    assert [chave for chave, _ in ranking] == ["identico", "quase", "ortogonal"], (
        f"ordenacao inesperada: {ranking}"
    )
    assert ranking[0][1] == pytest.approx(1.0)
    assert ranking[-1][1] == pytest.approx(0.5)


def test_rank_sem_candidatos_ou_com_top_k_nao_positivo_devolve_lista_vazia() -> None:
    """Casos degenerados nao levantam e nao inventam candidato."""
    matcher = SemanticMatcher()

    assert matcher.rank([1.0, 0.0], {}, top_k=3) == []
    assert matcher.rank([1.0, 0.0], {"a": [1.0, 0.0]}, top_k=0) == []


# --------------------------------------------------------------------------- #
# OrderMatcher (SPEC-0010 secao 3.5)
# --------------------------------------------------------------------------- #
ANCORAS: Final[tuple[str, ...]] = ("alfa beta", "gama delta")
"""Ancoras achatadas em quatro tokens na ordem esperada: alfa, beta, gama, delta."""


def test_ancoras_na_ordem_esperada_aprovam_a_ordem_temporal() -> None:
    """Tudo no lugar: razao maxima e `order_ok` verdadeiro."""
    razao, ordem_ok = OrderMatcher().evaluate("comeco alfa beta gama delta fim", ANCORAS)

    assert (razao, ordem_ok) == (1.0, True)


def test_ancoras_invertidas_reprovam_a_ordem_temporal() -> None:
    """Sequencia ao contrario: so uma ancora sobrevive a LCS, e a ordem cai."""
    razao, ordem_ok = OrderMatcher().evaluate("delta gama beta alfa", ANCORAS)

    assert razao == pytest.approx(0.25), f"LCS de 1 em 4 ancoras deveria dar 0.25, obtido {razao}"
    assert ordem_ok is False


def test_ancoras_parciais_fora_de_ordem_ficam_abaixo_do_limiar() -> None:
    """Tres ancoras presentes, uma trocada de lugar: 2/3 nao alcanca 0.7."""
    razao, ordem_ok = OrderMatcher().evaluate("alfa gama beta", ANCORAS)

    assert razao == pytest.approx(2 / 3)
    assert ordem_ok is False, "0.667 esta abaixo do limiar 0.7 e deveria reprovar"


def test_ancoras_parciais_em_ordem_aprovam_mesmo_faltando_ancora() -> None:
    """A ordem so julga o que aparece: duas de quatro, na ordem, aprovam."""
    assert OrderMatcher().evaluate("alfa beta apenas", ANCORAS) == (1.0, True)


def test_uma_unica_troca_entre_quatro_ancoras_ainda_aprova_a_ordem() -> None:
    """`0.75 >= 0.7`: a SPEC tolera um deslize, nao a inversao."""
    razao, ordem_ok = OrderMatcher().evaluate("alfa beta delta gama", ANCORAS)

    assert razao == pytest.approx(0.75)
    assert ordem_ok is True


def test_sem_ancoras_a_ordem_nada_afirma() -> None:
    """Comercial sem `key_phrases` nem keywords nao pode ser penalizado por ordem."""
    assert OrderMatcher().evaluate("qualquer coisa dita no ar", []) == (1.0, True)


def test_nenhuma_ancora_encontrada_no_texto_nao_reprova_a_ordem() -> None:
    """Ausencia total de ancoras e assunto do sinal lexico, nao do de ordem."""
    assert OrderMatcher().evaluate("nada disso aparece aqui", ANCORAS) == (1.0, True)


@pytest.mark.parametrize("limiar", [-0.1, 1.1])
def test_limiar_de_ordem_fora_de_zero_um_e_recusado(limiar: float) -> None:
    """`order_ratio` vive em `[0, 1]`; o limiar tambem."""
    with pytest.raises(ValidationError):
        OrderMatcher(limiar)


# --------------------------------------------------------------------------- #
# ScoreFusion (SPEC-0010 secao 3.6)
# --------------------------------------------------------------------------- #
def test_fusao_aplica_exatamente_a_formula_normativa_da_spec() -> None:
    """`S = 0.40*speech + 0.25*semantic + 0.15*ocr + 0.15*visual + 0.05*duration`."""
    evidencia = make_evidence(
        speech_match=0.8,
        semantic_match=0.6,
        ocr_match=0.4,
        visual_match=0.2,
        duration_match=1.0,
        order_ok=True,
    )

    score = ScoreFusion().fuse(evidencia)

    esperado = 0.40 * 0.8 + 0.25 * 0.6 + 0.15 * 0.4 + 0.15 * 0.2 + 0.05 * 1.0
    assert score == pytest.approx(esperado)
    assert score == pytest.approx(0.61), f"a formula da SPEC da 0.61, obtido {score:.6f}"


def test_pesos_padrao_da_fusao_sao_os_normativos_da_spec() -> None:
    """Os pesos default do servico de dominio sao os da tabela da secao 3.5."""
    assert ScoreFusion().weights() == {
        "lexical": 0.40,
        "semantic": 0.25,
        "ocr": 0.15,
        "visual": 0.15,
        "duration": 0.05,
    }


def test_ordem_reprovada_multiplica_o_score_pela_penalidade_de_085() -> None:
    """A penalidade de ordem e multiplicativa e pode rebaixar a decisao final."""
    comum = {
        "speech_match": 0.8,
        "semantic_match": 0.6,
        "ocr_match": 0.4,
        "visual_match": 0.2,
        "duration_match": 1.0,
    }
    fusao = ScoreFusion()

    com_ordem = fusao.fuse(make_evidence(**comum, order_ok=True))
    sem_ordem = fusao.fuse(make_evidence(**comum, order_ok=False))

    assert DEFAULT_ORDER_PENALTY == 0.85
    assert sem_ordem == pytest.approx(com_ordem * 0.85)
    assert sem_ordem == pytest.approx(0.5185)
    assert fusao.classify(com_ordem) is DetectionStatus.NEEDS_REVIEW
    assert fusao.classify(sem_ordem) is DetectionStatus.REJECTED, (
        "a penalidade de ordem deveria derrubar este candidato para REJECTED"
    )


def test_duracao_pontua_pela_distancia_relativa_a_duracao_esperada() -> None:
    """`1 - min(1, |dur_janela - esperada| / max(esperada, 1))` (secao 3.5)."""
    fusao = ScoreFusion()

    assert fusao.duration_score(30.0, 30.0) == pytest.approx(1.0)
    assert fusao.duration_score(15.0, 30.0) == pytest.approx(0.5)
    assert fusao.duration_score(60.0, 30.0) == pytest.approx(0.0)
    assert fusao.duration_score(120.0, 30.0) == pytest.approx(0.0), "o desvio satura em 1"


@pytest.mark.parametrize(
    ("score", "esperado"),
    [
        (1.0, DetectionStatus.ACCEPTED),
        (0.95, DetectionStatus.ACCEPTED),
        (0.75, DetectionStatus.NEEDS_REVIEW),
        (0.61, DetectionStatus.NEEDS_REVIEW),
        (0.30, DetectionStatus.REJECTED),
        (0.0, DetectionStatus.REJECTED),
    ],
)
def test_classify_separa_as_tres_faixas_da_spec(score: float, esperado: DetectionStatus) -> None:
    """Aceita, revisa e rejeita conforme a tabela da secao 3.6."""
    assert ScoreFusion().classify(score) is esperado


def test_classify_nas_bordas_exatas_de_090_e_060() -> None:
    """Os limiares sao inclusivos: `>= 0.90` aceita e `>= 0.60` revisa."""
    fusao = ScoreFusion()

    assert (DEFAULT_ACCEPT_THRESHOLD, DEFAULT_REVIEW_THRESHOLD) == (0.90, 0.60)
    assert fusao.classify(0.90) is DetectionStatus.ACCEPTED, "0.90 exato tem de ser ACCEPTED"
    assert fusao.classify(math.nextafter(0.90, 0.0)) is DetectionStatus.NEEDS_REVIEW, (
        "o menor float abaixo de 0.90 ainda nao e ACCEPTED"
    )
    assert fusao.classify(0.60) is DetectionStatus.NEEDS_REVIEW, "0.60 exato tem de ser revisado"
    assert fusao.classify(math.nextafter(0.60, 0.0)) is DetectionStatus.REJECTED, (
        "o menor float abaixo de 0.60 ja e REJECTED"
    )


def test_limiares_customizados_deslocam_as_faixas() -> None:
    """Os limiares vem de `Settings` e a classificacao acompanha (secao 3.6)."""
    fusao = ScoreFusion()

    assert fusao.classify(0.80, accept=0.75, review=0.50) is DetectionStatus.ACCEPTED
    assert fusao.classify(0.80, accept=0.95, review=0.50) is DetectionStatus.NEEDS_REVIEW


def test_pesos_que_nao_somam_um_sao_recusados() -> None:
    """A SPEC exige soma 1.0: qualquer outra combinacao nao monta o fusor."""
    with pytest.raises(ValidationError) as erro:
        ScoreFusion(
            weight_lexical=0.50,
            weight_semantic=0.25,
            weight_ocr=0.15,
            weight_visual=0.15,
            weight_duration=0.05,
        )

    assert "1.0" in str(erro.value)

    with pytest.raises(ValidationError):
        ScoreFusion(
            weight_lexical=0.10,
            weight_semantic=0.10,
            weight_ocr=0.10,
            weight_visual=0.10,
            weight_duration=0.10,
        )


def test_soma_dentro_da_tolerancia_e_aceita() -> None:
    """Ruido de ponto flutuante ate `WEIGHT_SUM_TOLERANCE` nao derruba o fusor."""
    fusao = ScoreFusion(
        weight_lexical=0.40 + WEIGHT_SUM_TOLERANCE / 2,
        weight_semantic=0.25,
        weight_ocr=0.15,
        weight_visual=0.15,
        weight_duration=0.05,
    )

    assert sum(fusao.weights().values()) == pytest.approx(1.0, abs=WEIGHT_SUM_TOLERANCE)


def test_peso_fora_do_intervalo_zero_um_e_recusado() -> None:
    """Peso negativo nao e peso — o erro aponta o campo."""
    with pytest.raises(ValidationError) as erro:
        ScoreFusion(
            weight_lexical=-0.10,
            weight_semantic=0.55,
            weight_ocr=0.20,
            weight_visual=0.20,
            weight_duration=0.15,
        )

    assert "[0, 1]" in str(erro.value)


def test_penalidade_de_ordem_fora_do_intervalo_e_recusada() -> None:
    """A penalidade tambem vive em `[0, 1]`."""
    with pytest.raises(ValidationError):
        ScoreFusion(order_penalty=1.5)


def test_review_maior_que_accept_e_recusado_na_classificacao() -> None:
    """Limiares invertidos tornariam a faixa de revisao vazia — e erro de configuracao."""
    with pytest.raises(ValidationError):
        ScoreFusion().classify(0.8, accept=0.60, review=0.90)


# --------------------------------------------------------------------------- #
# NonMaximumSuppression (SPEC-0010 secao 3.7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("a", "b", "esperado"),
    [
        ((0.0, 10.0), (0.0, 10.0), 1.0),
        ((0.0, 10.0), (5.0, 15.0), 1 / 3),
        ((0.0, 10.0), (10.0, 20.0), 0.0),
        ((0.0, 10.0), (20.0, 30.0), 0.0),
        ((100.0, 130.0), (105.0, 135.0), 25 / 35),
    ],
)
def test_iou_mede_intersecao_sobre_uniao(
    a: tuple[float, float], b: tuple[float, float], esperado: float
) -> None:
    """A metrica de sobreposicao temporal e a IoU classica."""
    assert NonMaximumSuppression.iou(a, b) == pytest.approx(esperado)


def test_mesmo_comercial_sobreposto_funde_mantendo_maior_score_e_uniao_do_intervalo() -> None:
    """Duas janelas da mesma veiculacao viram uma so (secao 3.7)."""
    fundidos = NonMaximumSuppression.suppress(
        [candidato("COM_000001", 100.0, 130.0, 0.80), candidato("COM_000001", 105.0, 135.0, 0.91)]
    )

    assert len(fundidos) == 1, f"IoU 0.714 > {DEFAULT_IOU_THRESHOLD} deveria fundir: {fundidos}"
    assert (fundidos[0].start, fundidos[0].end) == (100.0, 135.0), "o intervalo vira a uniao"
    assert fundidos[0].score == 0.91, "o score preservado e o do candidato mais forte"


def test_comerciais_diferentes_no_mesmo_intervalo_nao_sao_fundidos() -> None:
    """A supressao e por comercial: dois anunciantes no mesmo trecho continuam dois."""
    mantidos = NonMaximumSuppression.suppress(
        [candidato("COM_000001", 100.0, 135.0, 0.80), candidato("COM_000002", 100.0, 135.0, 0.91)]
    )

    assert {item.commercial_code for item in mantidos} == {"COM_000001", "COM_000002"}
    assert len(mantidos) == 2


def test_sobreposicao_abaixo_do_limiar_preserva_os_dois_candidatos() -> None:
    """IoU de 1/3 nao alcanca 0.5 — sao duas veiculacoes distintas."""
    mantidos = NonMaximumSuppression.suppress(
        [candidato("COM_000001", 0.0, 10.0, 0.9), candidato("COM_000001", 5.0, 15.0, 0.8)]
    )

    assert len(mantidos) == 2


def test_saida_da_supressao_sai_ordenada_por_inicio() -> None:
    """Independentemente da ordem de entrada, a saida e cronologica."""
    mantidos = NonMaximumSuppression.suppress(
        [
            candidato("COM_000001", 200.0, 230.0, 0.70),
            candidato("COM_000002", 10.0, 40.0, 0.99),
            candidato("COM_000001", 205.0, 235.0, 0.75),
        ]
    )

    assert [item.start for item in mantidos] == sorted(item.start for item in mantidos)
    assert [(item.commercial_code, item.start, item.end) for item in mantidos] == [
        ("COM_000002", 10.0, 40.0),
        ("COM_000001", 200.0, 235.0),
    ]


def test_supressao_sem_candidatos_devolve_lista_vazia() -> None:
    """Caso degenerado nao levanta."""
    assert NonMaximumSuppression.suppress([]) == []


@pytest.mark.parametrize("limiar", [-0.1, 1.5])
def test_limiar_de_iou_fora_de_zero_um_e_recusado(limiar: float) -> None:
    """IoU vive em `[0, 1]`; o limiar tambem."""
    with pytest.raises(ValidationError):
        NonMaximumSuppression.suppress(
            [candidato("COM_000001", 0.0, 10.0, 0.9)], iou_threshold=limiar
        )


# --------------------------------------------------------------------------- #
# BoundaryRefiner (SPEC-0010 secao 3.8)
# --------------------------------------------------------------------------- #
def test_fronteira_encaixa_no_corte_de_cena_dentro_do_deslocamento_maximo() -> None:
    """Cortes a 0.5 s de distancia estao dentro dos 3 s tolerados: encaixa e marca."""
    inicio, fim, refinado = BoundaryRefiner().refine(120.0, 150.0, make_scenes([(119.5, 150.5)]))

    assert DEFAULT_MAX_SHIFT == 3.0
    assert (inicio, fim) == (119.5, 150.5)
    assert refinado is True


def test_fronteira_nao_encaixa_em_corte_alem_do_deslocamento_maximo() -> None:
    """Cortes a 10 s de distancia sao outra cena: o intervalo original e preservado."""
    inicio, fim, refinado = BoundaryRefiner().refine(120.0, 150.0, make_scenes([(110.0, 160.0)]))

    assert (inicio, fim) == (120.0, 150.0), "nada dentro de 3 s deveria manter as fronteiras"
    assert refinado is False


def test_sem_cortes_de_cena_o_intervalo_volta_intacto_e_nao_marcado() -> None:
    """Sem deteccao de cena nao ha o que encaixar (secao 3.8)."""
    assert BoundaryRefiner().refine(120.0, 150.0, []) == (120.0, 150.0, False)


def test_deslocamento_maximo_configuravel_muda_o_encaixe() -> None:
    """`max_shift` e parametro: com 0.1 s de folga o corte a 0.5 s deixa de valer."""
    apertado = BoundaryRefiner(max_shift=0.1)

    assert apertado.refine(120.0, 150.0, make_scenes([(119.5, 150.5)])) == (120.0, 150.0, False)


def test_max_shift_negativo_e_recusado() -> None:
    """Deslocamento negativo nao tem significado temporal."""
    with pytest.raises(ValidationError):
        BoundaryRefiner(max_shift=-1.0)


# --------------------------------------------------------------------------- #
# CandidateBuilder (SPEC-0010 secao 3.5)
# --------------------------------------------------------------------------- #
def construtor_de_candidato() -> CandidateBuilder:
    """`CandidateBuilder` com os matchers padrao da SPEC."""
    return CandidateBuilder(
        lexical=LexicalMatcher(),
        semantic=SemanticMatcher(),
        order=OrderMatcher(),
        fusion=ScoreFusion(),
    )


def test_candidato_traz_a_evidencia_completa_por_modalidade() -> None:
    """Toda modalidade da tabela da secao 3.5 chega preenchida na evidencia."""
    fingerprint = fingerprint_de_referencia(embedding=[1.0, 0.0, 0.0, 0.0])
    janela = TextWindow(start=120.0, end=150.0, text=VARIACAO_FALADA, ocr_text="Claro internet 5G")

    score, evidencia = construtor_de_candidato().evaluate(
        janela, fingerprint, window_vec=[1.0, 0.0, 0.0, 0.0]
    )

    assert evidencia.speech_match > 0.85
    assert evidencia.semantic_match == pytest.approx(1.0), "vetores identicos casam em cheio"
    assert evidencia.ocr_match == pytest.approx(1.0), "a keyword 'claro' aparece na tela"
    assert evidencia.duration_match == pytest.approx(1.0), "janela de 30 s para peca de 30 s"
    assert evidencia.order_ok is True
    assert evidencia.brand_detected == "Claro"
    assert evidencia.matched_text == VARIACAO_FALADA
    assert score == pytest.approx(ScoreFusion().fuse(evidencia)), (
        "o score devolvido tem de ser exatamente a fusao da evidencia devolvida"
    )


def test_sem_juiz_visual_o_sinal_visual_herda_a_fala_e_nao_inventa_um() -> None:
    """Sem `VisionJudgePort`, `visual_match == speech_match` — nunca `1.0` (secao 3.5)."""
    fingerprint = fingerprint_de_referencia()
    janela = TextWindow(start=120.0, end=150.0, text=VARIACAO_FALADA)

    _, evidencia = construtor_de_candidato().evaluate(janela, fingerprint)

    assert evidencia.visual_match == pytest.approx(evidencia.speech_match)
    assert evidencia.visual_match != 1.0, (
        "a ausencia de juiz multimodal nao pode virar evidencia visual perfeita"
    )


def test_evidencia_distingue_proxy_de_fala_de_veredito_real_do_juiz() -> None:
    """Duas evidencias com o mesmo numero visual precisam ser distinguiveis (secao 3.5)."""
    fingerprint = fingerprint_de_referencia()
    janela = TextWindow(start=120.0, end=150.0, text=VARIACAO_FALADA)
    construtor = construtor_de_candidato()

    _, sem_juiz = construtor.evaluate(janela, fingerprint)
    _, com_juiz = construtor.evaluate(janela, fingerprint, visual_score=sem_juiz.speech_match)

    assert sem_juiz != com_juiz, (
        "a evidencia sem juiz multimodal e a evidencia com um veredito de mesmo valor "
        "saem identicas: o fato de nao ter havido juiz nao foi registrado em `evidence`"
    )


def test_veredito_do_juiz_visual_substitui_o_proxy_de_fala() -> None:
    """Quando o juiz responde, o `visual_match` e dele — nao mais o proxy."""
    fingerprint = fingerprint_de_referencia()
    janela = TextWindow(start=120.0, end=150.0, text=VARIACAO_FALADA)

    _, evidencia = construtor_de_candidato().evaluate(janela, fingerprint, visual_score=0.31)

    assert evidencia.visual_match == pytest.approx(0.31)
    assert evidencia.visual_match != evidencia.speech_match


def test_sem_ocr_o_sinal_de_tela_vale_zero() -> None:
    """Janela sem texto de tela nao ganha credito visual textual (secao 3.5)."""
    fingerprint = fingerprint_de_referencia()
    janela = TextWindow(start=120.0, end=150.0, text=VARIACAO_FALADA, ocr_text="   ")

    _, evidencia = construtor_de_candidato().evaluate(janela, fingerprint)

    assert evidencia.ocr_match == 0.0


def test_sem_embedding_o_sinal_semantico_vale_zero() -> None:
    """Sem embedder disponivel o funil degrada para `0.0`, nao para o neutro 0.5."""
    fingerprint = fingerprint_de_referencia(embedding=None)
    janela = TextWindow(start=120.0, end=150.0, text=VARIACAO_FALADA)

    _, evidencia = construtor_de_candidato().evaluate(janela, fingerprint, window_vec=[1.0, 0.0])

    assert evidencia.semantic_match == 0.0


def test_marca_ausente_da_fala_e_da_tela_nao_e_reportada() -> None:
    """`brand_detected` so afirma o que apareceu de fato."""
    fingerprint = fingerprint_de_referencia()
    janela = TextWindow(start=0.0, end=30.0, text="mais internet para aproveitar tudo que gosta")

    _, evidencia = construtor_de_candidato().evaluate(janela, fingerprint)

    assert evidencia.brand_detected is None
