"""Testes de unidade das funcoes puras de normalizacao de texto (SPEC-0000 secao 8).

`text_normalizer` e a base compartilhada dos avaliadores de guardrail (SPEC-0003)
e do motor de matching do AdWatch (SPEC-0010). Como tudo aqui e memoizado com
`lru_cache`, os testes tambem cobrem a propriedade que a memoizacao promete: a
mesma entrada devolve sempre o mesmo resultado, e limpar o cache nao muda nada.
"""

from __future__ import annotations

import pytest

from lukato.domain.errors import ValidationError
from lukato.domain.services.text_normalizer import (
    char_ngrams,
    clear_caches,
    jaccard,
    lcs_length,
    lcs_ratio,
    ngrams,
    normalize,
    strip_accents,
    tokenize,
    truncate_words,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# strip_accents
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Ação", "Acao"),
        ("coração", "coracao"),
        ("Não é você", "Nao e voce"),
        ("ÜBER", "UBER"),
        ("sem acento", "sem acento"),
    ],
)
def test_strip_accents_remove_diacriticos_preservando_a_caixa(entrada: str, esperado: str) -> None:
    """A decomposicao NFKD tira o acento e mantem letra, caixa e espacos."""
    assert strip_accents(entrada) == esperado


def test_strip_accents_preserva_pontuacao_e_digitos() -> None:
    """Remover acento nao e limpar texto: so os diacriticos saem."""
    assert strip_accents("R$ 1.234,50 — ação!") == "R$ 1.234,50 — acao!"


def test_strip_accents_de_texto_vazio_devolve_vazio() -> None:
    """Entrada vazia nao e caso especial."""
    assert strip_accents("") == ""


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("Ação, R$ 10!", "acao r 10"),
        ("  espacos    demais  ", "espacos demais"),
        ("O MELHOR Plano da Claro", "o melhor plano da claro"),
        ("linha\nquebrada\tpor\ttab", "linha quebrada por tab"),
        ("e-mail@exemplo.com", "e mail exemplo com"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_normalize_reduz_o_texto_a_alfanumericos_minusculos(entrada: str, esperado: str) -> None:
    """Minusculas, sem acento, sem pontuacao e com espacos colapsados."""
    assert normalize(entrada) == esperado


def test_normalize_e_idempotente() -> None:
    """Normalizar duas vezes nao muda o resultado — o texto ja esta na forma canonica."""
    uma_vez = normalize("Ação, R$ 10!")

    assert normalize(uma_vez) == uma_vez


def test_normalize_aproxima_grafias_que_diferem_so_por_acento_e_caixa() -> None:
    """E essa equivalencia que faz a comparacao lexica do AdWatch funcionar."""
    assert normalize("PROMOÇÃO Claro!") == normalize("promocao claro")


def test_normalize_preserva_digitos() -> None:
    """Numeros carregam sentido no comercial (preco, plano) e nao podem sumir."""
    assert normalize("Plano 50GB por R$ 79,90") == "plano 50gb por r 79 90"


# --------------------------------------------------------------------------- #
# tokenize
# --------------------------------------------------------------------------- #
def test_tokenize_divide_o_texto_normalizado_em_palavras() -> None:
    """Os tokens saem ja normalizados, prontos para comparacao."""
    assert tokenize("  Olá,  MUNDO! ") == ["ola", "mundo"]


def test_tokenize_de_texto_sem_alfanumerico_devolve_lista_vazia() -> None:
    """Pontuacao pura nao gera token."""
    assert tokenize("--- !!! ---") == []


def test_tokenize_devolve_lista_nova_a_cada_chamada() -> None:
    """A memoizacao guarda uma tupla; mutar o retorno nao contamina o cache."""
    primeira = tokenize("um dois")
    primeira.append("tres")

    assert tokenize("um dois") == ["um", "dois"]


# --------------------------------------------------------------------------- #
# ngrams e char_ngrams
# --------------------------------------------------------------------------- #
def test_ngrams_gera_as_janelas_contiguas_de_tokens() -> None:
    """Bigramas de `a b c` sao `(a,b)` e `(b,c)`."""
    assert ngrams(["a", "b", "c"], 2) == [("a", "b"), ("b", "c")]


def test_ngrams_de_tamanho_um_devolve_cada_token_isolado() -> None:
    """`n=1` degenera para a propria sequencia, em tuplas."""
    assert ngrams(["a", "b"], 1) == [("a",), ("b",)]


def test_ngrams_com_sequencia_menor_que_n_devolve_lista_vazia() -> None:
    """Nao ha janela possivel — e um resultado vazio, nao um erro."""
    assert ngrams(["a", "b"], 3) == []
    assert ngrams([], 1) == []


@pytest.mark.parametrize("n", [0, -1])
def test_ngrams_recusa_tamanho_menor_que_um(n: int) -> None:
    """`n < 1` e erro de programacao e vira `ValidationError` do dominio."""
    with pytest.raises(ValidationError) as excecao:
        ngrams(["a"], n)

    assert excecao.value.details["n"] == n


def test_char_ngrams_opera_sobre_o_texto_normalizado() -> None:
    """A normalizacao acontece antes: `Açã` e `aca` produzem os mesmos n-gramas."""
    assert char_ngrams("Açã", 2) == char_ngrams("aca", 2) == ["ac", "ca"]


def test_char_ngrams_com_texto_menor_que_n_devolve_lista_vazia() -> None:
    """Sem caracteres suficientes nao existe janela."""
    assert char_ngrams("ab", 5) == []


@pytest.mark.parametrize("n", [0, -3])
def test_char_ngrams_recusa_tamanho_menor_que_um(n: int) -> None:
    """Mesma regra dos n-gramas de token."""
    with pytest.raises(ValidationError):
        char_ngrams("abc", n)


def test_char_ngrams_devolve_lista_nova_a_cada_chamada() -> None:
    """Mutar o retorno nao pode corromper a memoizacao."""
    primeira = char_ngrams("abc", 2)
    primeira.clear()

    assert char_ngrams("abc", 2) == ["ab", "bc"]


# --------------------------------------------------------------------------- #
# jaccard
# --------------------------------------------------------------------------- #
def test_jaccard_de_conjuntos_identicos_e_um() -> None:
    """Sobreposicao total e similaridade maxima."""
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_calcula_intersecao_sobre_uniao() -> None:
    """`{a} / {a,b}` = 0.5, a conta classica."""
    assert jaccard({"a"}, {"a", "b"}) == 0.5


def test_jaccard_sem_intersecao_e_zero() -> None:
    """Conjuntos disjuntos nao tem similaridade."""
    assert jaccard({"a"}, {"b"}) == 0.0


@pytest.mark.parametrize(("esquerda", "direita"), [(set(), set()), ({"a"}, set()), (set(), {"a"})])
def test_jaccard_com_conjunto_vazio_e_zero(esquerda: set[str], direita: set[str]) -> None:
    """Ausencia de evidencia nunca pode virar similaridade maxima no matching."""
    assert jaccard(esquerda, direita) == 0.0


def test_jaccard_e_simetrico() -> None:
    """A ordem dos argumentos nao muda a similaridade."""
    assert jaccard({"a", "b"}, {"b", "c"}) == jaccard({"b", "c"}, {"a", "b"})


# --------------------------------------------------------------------------- #
# lcs_length e lcs_ratio
# --------------------------------------------------------------------------- #
def test_lcs_length_conta_a_maior_subsequencia_comum() -> None:
    """`a b c` e `a c` compartilham `a c`, de comprimento 2."""
    assert lcs_length(["a", "b", "c"], ["a", "c"]) == 2


def test_lcs_length_respeita_a_ordem_das_sequencias() -> None:
    """Os mesmos tokens em ordem invertida nao formam subsequencia longa."""
    assert lcs_length(["a", "b"], ["b", "a"]) == 1


@pytest.mark.parametrize(("esquerda", "direita"), [([], ["a"]), (["a"], []), ([], [])])
def test_lcs_length_com_sequencia_vazia_e_zero(esquerda: list[str], direita: list[str]) -> None:
    """Sem tokens nao ha subsequencia comum."""
    assert lcs_length(esquerda, direita) == 0


def test_lcs_ratio_e_a_fracao_da_esquerda_encontrada_na_direita() -> None:
    """2 dos 3 tokens esperados apareceram na ordem: 2/3."""
    assert lcs_ratio(["a", "b", "c"], ["a", "c"]) == pytest.approx(2 / 3)


def test_lcs_ratio_de_sequencia_esperada_vazia_e_zero() -> None:
    """Sem texto esperado nao existe cobertura a medir (e nao ha divisao por zero)."""
    assert lcs_ratio([], ["a", "b"]) == 0.0


def test_lcs_ratio_de_cobertura_total_e_um() -> None:
    """Toda a fala esperada apareceu, na ordem."""
    assert lcs_ratio(["a", "b"], ["x", "a", "y", "b"]) == 1.0


# --------------------------------------------------------------------------- #
# truncate_words
# --------------------------------------------------------------------------- #
def test_truncate_words_devolve_o_texto_intacto_quando_cabe_no_limite() -> None:
    """Sem excesso nao ha corte."""
    assert truncate_words("abc def", 100) == "abc def"


def test_truncate_words_corta_na_fronteira_de_palavra() -> None:
    """`abc def ghi` em 9 caracteres perde a palavra parcial, nao o meio dela."""
    assert truncate_words("abc def ghi", 9) == "abc def"


def test_truncate_words_corta_no_espaco_sem_deixar_sobra() -> None:
    """Quando o limite cai exatamente no espaco, nada de palavra parcial sobra."""
    assert truncate_words("abc def ghi", 7) == "abc def"


def test_truncate_words_corta_no_caractere_quando_a_primeira_palavra_ja_excede() -> None:
    """Sem fronteira util, cortar no caractere e melhor que devolver texto vazio."""
    assert truncate_words("abcdefgh", 3) == "abc"


@pytest.mark.parametrize("limite", [0, -5])
def test_truncate_words_com_limite_nao_positivo_devolve_vazio(limite: int) -> None:
    """Limite zero ou negativo significa "nada cabe"."""
    assert truncate_words("qualquer texto", limite) == ""


# --------------------------------------------------------------------------- #
# Determinismo e memoizacao
# --------------------------------------------------------------------------- #
def test_normalizacao_e_deterministica_entre_chamadas() -> None:
    """A mesma entrada produz a mesma saida — requisito da SPEC-0003 secao 2."""
    texto = "Promoção IMPERDÍVEL da Claro!"

    assert normalize(texto) == normalize(texto)
    assert tokenize(texto) == tokenize(texto)


def test_clear_caches_nao_muda_o_resultado_das_funcoes() -> None:
    """Limpar a memoizacao e transparente: muda o custo, nunca o valor."""
    texto = "Promoção IMPERDÍVEL da Claro!"
    antes = (strip_accents(texto), normalize(texto), tokenize(texto), char_ngrams(texto, 3))

    clear_caches()

    assert (strip_accents(texto), normalize(texto), tokenize(texto), char_ngrams(texto, 3)) == antes
