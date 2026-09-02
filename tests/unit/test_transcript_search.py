"""Busca de frase exata sobre a linha do tempo da transcricao (SPEC-0010).

O contrato que importa: a busca ignora maiusculas, acentos e pontuacao — porque
o ASR escreve "Motorola exclusivo, claro." e o operador digita "motorola
exclusivo claro" — mas nunca ignora a ordem nem a contiguidade das palavras,
porque e isso que separa *achar a frase* de *achar as palavras espalhadas*.
"""

from __future__ import annotations

import pytest

from lukato.domain.models.adwatch import TranscriptWord
from lukato.domain.services.transcript_search import find_phrase

pytestmark = pytest.mark.unit


def palavras(*termos: str, inicio: float = 0.0, passo: float = 0.5) -> list[TranscriptWord]:
    """Linha do tempo sintetica: cada termo dura `passo` segundos, sem folga."""
    return [
        TranscriptWord(word=termo, start=inicio + indice * passo, end=inicio + (indice + 1) * passo)
        for indice, termo in enumerate(termos)
    ]


FALA = palavras(
    "Também.", "Na", "compra", "de", "um", "Motorola", "exclusivo,", "claro.", "Aproveite."
)
"""O trecho real do comercial, com a pontuacao e a caixa que o ASR produz."""


def test_frase_e_encontrada_ignorando_caixa_acentos_e_pontuacao() -> None:
    """A busca em minusculas e sem virgulas casa com a fala pontuada do ASR."""
    ocorrencias = find_phrase(FALA, "na compra de um motorola exclusivo claro")

    assert len(ocorrencias) == 1
    unica = ocorrencias[0]
    assert unica.first_word == 1
    assert unica.last_word == 7
    assert unica.start == FALA[1].start
    assert unica.end == FALA[7].end
    assert unica.text == "Na compra de um Motorola exclusivo, claro."


def test_busca_com_acento_casa_com_fala_sem_acento_e_vice_versa() -> None:
    """`também` e `Tambem.` sao a mesma palavra depois da normalizacao."""
    assert len(find_phrase(FALA, "também")) == 1
    assert len(find_phrase(palavras("também"), "TAMBEM")) == 1


def test_palavra_com_hifen_gera_dois_tokens_e_ainda_casa() -> None:
    """ "super-herói" na transcricao casa com a busca `super heroi`."""
    fala = palavras("pai", "de", "super-herói?")

    ocorrencias = find_phrase(fala, "pai de super heroi")

    assert len(ocorrencias) == 1
    assert ocorrencias[0].first_word == 0
    assert ocorrencias[0].last_word == 2
    assert ocorrencias[0].end == fala[2].end


def test_ordem_das_palavras_nunca_e_ignorada() -> None:
    """As mesmas palavras fora de ordem nao sao a frase."""
    assert find_phrase(FALA, "compra na de um motorola") == []


def test_contiguidade_e_exigida() -> None:
    """Uma palavra estranha no meio da sequencia quebra o casamento."""
    fala = palavras("na", "compra", "urgente", "de", "um")

    assert find_phrase(fala, "na compra de um") == []


def test_multiplas_ocorrencias_saem_em_ordem_temporal() -> None:
    """A mesma frase dita duas vezes devolve as duas faixas, na ordem da fala."""
    fala = palavras("oferta", "especial", "hoje", "e", "amanhã", "oferta", "especial")

    ocorrencias = find_phrase(fala, "oferta especial")

    assert [(item.first_word, item.last_word) for item in ocorrencias] == [(0, 1), (5, 6)]
    assert ocorrencias[0].end <= ocorrencias[1].start


def test_ocorrencias_nao_se_sobrepoem() -> None:
    """Em `la la la`, a busca por `la la` casa uma vez — a sobra nao forma outra."""
    ocorrencias = find_phrase(palavras("la", "la", "la"), "la la")

    assert len(ocorrencias) == 1
    assert (ocorrencias[0].first_word, ocorrencias[0].last_word) == (0, 1)


def test_casamentos_dentro_da_mesma_palavra_nao_geram_duplicatas() -> None:
    """ "bla-bla-bla-bla" casa `bla bla` duas vezes nos tokens, mas e uma palavra so."""
    ocorrencias = find_phrase(palavras("blá-blá-blá-blá"), "bla bla")

    assert len(ocorrencias) == 1
    assert (ocorrencias[0].first_word, ocorrencias[0].last_word) == (0, 0)


def test_casamento_pode_comecar_no_meio_de_uma_palavra_composta() -> None:
    """Buscar `chuva azul` acha "guarda-chuva azul": a granularidade e a palavra.

    E o mesmo mecanismo que faz `super heroi` casar com "super-herói" — o texto
    devolvido inclui a palavra composta inteira porque o timestamp e dela.
    """
    fala = palavras("guarda-chuva", "azul")

    ocorrencias = find_phrase(fala, "chuva azul")

    assert len(ocorrencias) == 1
    assert (ocorrencias[0].first_word, ocorrencias[0].last_word) == (0, 1)
    assert ocorrencias[0].text == "guarda-chuva azul"


def test_busca_vazia_ou_so_pontuacao_devolve_nada() -> None:
    """Sem token util nao ha o que casar — e lista vazia, nunca erro."""
    assert find_phrase(FALA, "") == []
    assert find_phrase(FALA, "   ") == []
    assert find_phrase(FALA, "?!,") == []
    assert find_phrase([], "qualquer frase") == []


def test_frase_maior_que_a_transcricao_devolve_nada() -> None:
    """Uma agulha maior que o palheiro nao pode casar."""
    fala = palavras("na", "compra")

    assert find_phrase(fala, "na compra de um motorola") == []
