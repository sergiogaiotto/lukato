"""Testes de integracao do indice vetorial `PgVectorStore` (SPEC-0011 secao 7).

Em SQLite o adaptador roda no **modo varredura**: carrega os chunks da colecao e
calcula o cosseno com `numpy` em memoria. E exatamente o caminho que a suite offline
exercita aqui — o caminho nativo do pgvector (`ORDER BY embedding <=> :vec`) so existe
em PostgreSQL e nao e coberto por estes testes.

Os vetores sao **bases canonicas** (`e_i`: tudo zero, `1.0` na posicao `i`). Com eles o
cosseno vale exatamente `1.0` para o proprio vetor e `0.0` para qualquer outro, o que
torna os scores conferiveis na mao e a ordenacao livre de empate por ruido de ponto
flutuante.

Todo chunk depende de um documento real: `chunks.document_id` e uma chave estrangeira
com `ON DELETE CASCADE` e o `PRAGMA foreign_keys=ON` do `build_engine` esta ligado, de
modo que gravar chunk orfao seria recusado pelo banco.
"""

from __future__ import annotations

import pytest

from lukato.adapters.persistence.pgvector_store import PgVectorStore
from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.domain.errors import ValidationError
from lukato.domain.models.knowledge import SearchHit
from tests.factories import id_de, make_chunk, make_document

pytestmark = pytest.mark.integration

DIMENSAO = 1024
"""Dimensao declarada em `Settings.embedding.dimensions` na fixture `settings`."""

COLECAO = "evidencias"
OUTRA_COLECAO = "manuais"


def vetor(posicao: int) -> list[float]:
    """Base canonica `e_posicao` com `DIMENSAO` componentes (cosseno exato, sem ruido)."""
    valores = [0.0] * DIMENSAO
    valores[posicao] = 1.0
    return valores


async def grava_documento(
    uow_factory: UnitOfWorkFactoryImpl, *, titulo: str, colecao: str = COLECAO
) -> str:
    """Cria e **confirma** um documento; o indice vetorial usa sessao propria."""
    async with uow_factory() as unidade:
        documento = await unidade.documents.add(
            make_document(title=titulo, collection=colecao, document_id=id_de("documento", titulo))
        )
        await unidade.commit()
    return documento.id


# --------------------------------------------------------------------------- #
# Gravacao e busca
# --------------------------------------------------------------------------- #
async def test_upsert_seguido_de_search_recupera_o_chunk_correto(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    gravados = await vector_store.upsert(
        COLECAO,
        [
            make_chunk(
                documento,
                index=0,
                collection=COLECAO,
                content="primeiro trecho",
                embedding=vetor(0),
                chunk_id=id_de("chunk", "0"),
            ),
            make_chunk(
                documento,
                index=1,
                collection=COLECAO,
                content="segundo trecho",
                embedding=vetor(1),
                chunk_id=id_de("chunk", "1"),
            ),
        ],
    )
    assert gravados == 2

    achados = await vector_store.search(COLECAO, vetor(1), limit=2)

    assert isinstance(achados[0], SearchHit)
    assert achados[0].chunk_id == id_de("chunk", "1"), "o vizinho mais proximo vem primeiro"
    assert achados[0].content == "segundo trecho"
    assert achados[0].score == pytest.approx(1.0), "cosseno de um vetor com ele mesmo e 1"
    assert achados[1].score == pytest.approx(0.0), "bases canonicas distintas sao ortogonais"


async def test_search_respeita_o_limite_de_resultados(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    await vector_store.upsert(
        COLECAO,
        [
            make_chunk(
                documento,
                index=posicao,
                collection=COLECAO,
                embedding=vetor(posicao),
                chunk_id=id_de("chunk", posicao),
            )
            for posicao in range(5)
        ],
    )

    achados = await vector_store.search(COLECAO, vetor(0), limit=2)

    assert len(achados) == 2


async def test_upsert_do_mesmo_chunk_atualiza_em_vez_de_duplicar(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    original = make_chunk(
        documento,
        index=0,
        collection=COLECAO,
        content="texto original",
        embedding=vetor(0),
        chunk_id=id_de("chunk", "unico"),
    )
    await vector_store.upsert(COLECAO, [original])

    await vector_store.upsert(COLECAO, [original.model_copy(update={"content": "texto revisado"})])

    achados = await vector_store.search(COLECAO, vetor(0), limit=10)
    assert len(achados) == 1, "o chunk e identificado pelo id: upsert substitui, nao acumula"
    assert achados[0].content == "texto revisado"


async def test_search_ignora_chunks_de_outra_colecao(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    await vector_store.upsert(
        COLECAO,
        [make_chunk(documento, index=0, embedding=vetor(0), chunk_id=id_de("chunk", "dentro"))],
    )
    await vector_store.upsert(
        OUTRA_COLECAO,
        [make_chunk(documento, index=1, embedding=vetor(0), chunk_id=id_de("chunk", "fora"))],
    )

    achados = await vector_store.search(COLECAO, vetor(0), limit=10)

    assert [hit.chunk_id for hit in achados] == [id_de("chunk", "dentro")]


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #
async def test_search_filtra_por_document_id(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    primeiro = await grava_documento(uow_factory, titulo="manual")
    segundo = await grava_documento(uow_factory, titulo="politica")
    await vector_store.upsert(
        COLECAO,
        [
            make_chunk(primeiro, index=0, embedding=vetor(0), chunk_id=id_de("chunk", "p")),
            make_chunk(segundo, index=0, embedding=vetor(0), chunk_id=id_de("chunk", "s")),
        ],
    )

    achados = await vector_store.search(
        COLECAO, vetor(0), limit=10, filters={"document_id": segundo}
    )

    assert [hit.chunk_id for hit in achados] == [id_de("chunk", "s")]
    assert achados[0].document_id == segundo


async def test_search_filtra_por_chave_de_metadata(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    await vector_store.upsert(
        COLECAO,
        [
            make_chunk(
                documento,
                index=0,
                embedding=vetor(0),
                metadata={"fonte": "manual", "pagina": 1},
                chunk_id=id_de("chunk", "manual"),
            ),
            make_chunk(
                documento,
                index=1,
                embedding=vetor(0),
                metadata={"fonte": "automatico", "pagina": 2},
                chunk_id=id_de("chunk", "automatico"),
            ),
        ],
    )

    achados = await vector_store.search(COLECAO, vetor(0), limit=10, filters={"fonte": "manual"})

    assert [hit.chunk_id for hit in achados] == [id_de("chunk", "manual")]
    assert achados[0].metadata["pagina"] == 1


async def test_search_combina_filtro_de_coluna_e_de_metadata(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    primeiro = await grava_documento(uow_factory, titulo="manual")
    segundo = await grava_documento(uow_factory, titulo="politica")
    await vector_store.upsert(
        COLECAO,
        [
            make_chunk(
                primeiro,
                index=0,
                embedding=vetor(0),
                metadata={"fonte": "manual"},
                chunk_id=id_de("chunk", "pm"),
            ),
            make_chunk(
                segundo,
                index=0,
                embedding=vetor(0),
                metadata={"fonte": "manual"},
                chunk_id=id_de("chunk", "sm"),
            ),
            make_chunk(
                segundo,
                index=1,
                embedding=vetor(0),
                metadata={"fonte": "automatico"},
                chunk_id=id_de("chunk", "sa"),
            ),
        ],
    )

    achados = await vector_store.search(
        COLECAO, vetor(0), limit=10, filters={"document_id": segundo, "fonte": "manual"}
    )

    assert [hit.chunk_id for hit in achados] == [id_de("chunk", "sm")]


# --------------------------------------------------------------------------- #
# Remocao e inventario
# --------------------------------------------------------------------------- #
async def test_delete_remove_apenas_os_chunks_do_documento_informado(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    primeiro = await grava_documento(uow_factory, titulo="manual")
    segundo = await grava_documento(uow_factory, titulo="politica")
    await vector_store.upsert(
        COLECAO,
        [
            make_chunk(primeiro, index=0, embedding=vetor(0), chunk_id=id_de("chunk", "p0")),
            make_chunk(primeiro, index=1, embedding=vetor(1), chunk_id=id_de("chunk", "p1")),
            make_chunk(segundo, index=0, embedding=vetor(2), chunk_id=id_de("chunk", "s0")),
        ],
    )

    removidos = await vector_store.delete(COLECAO, document_id=primeiro)

    assert removidos == 2
    restantes = await vector_store.search(COLECAO, vetor(2), limit=10)
    assert [hit.chunk_id for hit in restantes] == [id_de("chunk", "s0")]


async def test_delete_sem_documento_esvazia_a_colecao_inteira(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    await vector_store.upsert(
        COLECAO,
        [
            make_chunk(documento, index=0, embedding=vetor(0), chunk_id=id_de("chunk", "a")),
            make_chunk(documento, index=1, embedding=vetor(1), chunk_id=id_de("chunk", "b")),
        ],
    )
    await vector_store.upsert(
        OUTRA_COLECAO,
        [make_chunk(documento, index=2, embedding=vetor(2), chunk_id=id_de("chunk", "c"))],
    )

    removidos = await vector_store.delete(COLECAO)

    assert removidos == 2
    assert await vector_store.search(COLECAO, vetor(0), limit=10) == []
    assert await vector_store.collections() == [OUTRA_COLECAO], (
        "apagar uma colecao nao pode tocar nas demais"
    )


async def test_collections_lista_as_colecoes_distintas_em_ordem_alfabetica(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    assert await vector_store.collections() == []

    await vector_store.upsert(
        OUTRA_COLECAO,
        [make_chunk(documento, index=0, embedding=vetor(0), chunk_id=id_de("chunk", "m"))],
    )
    await vector_store.upsert(
        COLECAO,
        [make_chunk(documento, index=1, embedding=vetor(1), chunk_id=id_de("chunk", "e"))],
    )

    assert await vector_store.collections() == [COLECAO, OUTRA_COLECAO]


# --------------------------------------------------------------------------- #
# Dimensao
# --------------------------------------------------------------------------- #
async def test_upsert_recusa_embedding_com_dimensao_divergente(
    vector_store: PgVectorStore, uow_factory: UnitOfWorkFactoryImpl
) -> None:
    documento = await grava_documento(uow_factory, titulo="manual")
    curto = make_chunk(
        documento, index=0, collection=COLECAO, embedding=[0.1, 0.2, 0.3], chunk_id=id_de("c", "x")
    )

    with pytest.raises(ValidationError) as erro:
        await vector_store.upsert(COLECAO, [curto])

    assert erro.value.details["received"] == 3
    assert erro.value.details["expected"] == DIMENSAO
    assert await vector_store.collections() == [], "nada pode ter sido gravado"


async def test_search_recusa_vetor_de_consulta_com_dimensao_divergente(
    vector_store: PgVectorStore,
) -> None:
    with pytest.raises(ValidationError) as erro:
        await vector_store.search(COLECAO, [1.0, 0.0], limit=1)

    assert erro.value.details["expected"] == DIMENSAO


async def test_construtor_recusa_dimensao_nao_positiva(session_factory) -> None:
    with pytest.raises(ValidationError):
        PgVectorStore(session_factory, dimensions=0)
