"""Busca de frase exata sobre a linha do tempo de uma transcricao (SPEC-0010).

O motor de deteccao responde no nivel da veiculacao — *este comercial foi ao
ar?* Este servico responde a pergunta menor que a operacao faz todos os dias:
*onde exatamente esta frase foi dita?* A comparacao usa a mesma normalizacao do
matching (minusculas, sem acento, sem pontuacao), entao a fala transcrita como
"Na compra de um Motorola exclusivo, claro." casa com a busca digitada
`na compra de um motorola exclusivo claro`.

Nada aqui faz I/O nem depende de configuracao: palavras entram, ocorrencias com
`start`/`end` em segundos saem.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lukato.domain.models.adwatch import TranscriptWord
from lukato.domain.services.text_normalizer import tokenize

__all__ = [
    "PhraseOccurrence",
    "find_phrase",
]


@dataclass(frozen=True, slots=True)
class PhraseOccurrence:
    """Uma ocorrencia da frase buscada, localizada no tempo da midia.

    `first_word`/`last_word` apontam para indices de `Transcript.words` — e o
    que permite a quem apresenta destacar as palavras exatas do casamento.
    """

    start: float
    """Inicio da primeira palavra casada, em segundos."""

    end: float
    """Fim da ultima palavra casada, em segundos."""

    first_word: int
    """Indice da primeira palavra casada em `Transcript.words`."""

    last_word: int
    """Indice da ultima palavra casada em `Transcript.words`."""

    text: str
    """O trecho como foi falado, com a grafia original das palavras."""


def find_phrase(words: Sequence[TranscriptWord], query: str) -> list[PhraseOccurrence]:
    """Localiza toda ocorrencia da frase na linha do tempo, em ordem temporal.

    O casamento e por sequencia exata de tokens normalizados: maiusculas,
    acentos e pontuacao sao ignorados; a ordem e a contiguidade das palavras,
    nunca — e isso que separa *achar a frase* de *achar as palavras espalhadas*.
    Uma palavra da transcricao pode gerar mais de um token ("super-heroi" vira
    `super` + `heroi`), entao a varredura acontece sobre a lista achatada de
    tokens e os indices devolvidos apontam de volta para as palavras originais.
    Ocorrencias nao se sobrepoem: a busca recomeca depois de cada casamento.
    """
    needle = tokenize(query or "")
    if not needle or not words:
        return []
    flat: list[tuple[str, int]] = [
        (token, index) for index, word in enumerate(words) for token in tokenize(word.word)
    ]
    occurrences: list[PhraseOccurrence] = []
    size = len(needle)
    position = 0
    while position + size <= len(flat):
        window = flat[position : position + size]
        if [token for token, _ in window] != needle:
            position += 1
            continue
        first_index = window[0][1]
        last_index = window[-1][1]
        # Uma palavra repetitiva ("bla-bla-bla-bla") pode casar a mesma frase em
        # janelas de tokens diferentes que apontam para as MESMAS palavras; a
        # segunda ocorrencia seria identica a primeira e so poluiria a resposta.
        if occurrences and (occurrences[-1].first_word, occurrences[-1].last_word) == (
            first_index,
            last_index,
        ):
            position += size
            continue
        occurrences.append(
            PhraseOccurrence(
                start=words[first_index].start,
                end=words[last_index].end,
                first_word=first_index,
                last_word=last_index,
                text=" ".join(word.word.strip() for word in words[first_index : last_index + 1]),
            )
        )
        position += size
    return occurrences
