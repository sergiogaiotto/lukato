"""AdWatch de ponta a ponta pela API v1, sem rede, sem FFmpeg, sem GPU (SPEC-0010).

O cenario e um telejornal sintetico de **dez minutos** (`0` a `600` s) com dois
comerciais que realmente foram ao ar e tres que nao foram::

    ┌──────────────────────────────────────────────────────────────────────┐
    │ 0                120 ── 150                     400 ── 430       600 │
    │ jornal            COM_000001 (Claro)             COM_000002 (Vivo)   │
    └──────────────────────────────────────────────────────────────────────┘

* `COM_000001` (Claro) e falado com **variacao lexical**: o catalogo guarda
  *"Na Claro voce tem muito mais internet para aproveitar tudo que gosta"* e o
  locutor diz *"Na Claro voce tem mais internet pra aproveitar tudo o que voce
  gosta"* — o criterio de aceite 4 da secao 6 em forma executavel;
* `COM_000002` (Vivo) e falado como esta catalogado, e serve de controle;
* `COM_000003`, `COM_000004` e `COM_000005` **nao vao ao ar** e nao podem
  aparecer no relatorio.

O caminho exercitado e o da secao 3.1 que dispensa infraestrutura: `POST
/media/{id}/transcript` importa a linha do tempo de palavras pronta, e o funil
inteiro roda sobre ela. Nenhum adaptador multimodal esta instalado nesta suite —
`GET /capabilities` tem de dizer isso com todas as letras, inclusive o que
instalar para mudar de figura.

Os numeros do cenario sao deterministicos (`HashingEmbedder` e matching puro),
entao os testes afirmam faixas justas em vez de "maior que zero": um score que
mude de verdade quebra o teste, que e o ponto.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from httpx import AsyncClient

from lukato.domain.models.adwatch import DetectionStatus

pytestmark = pytest.mark.integration

API: Final[str] = "/api/v1/adwatch"
"""Prefixo do recurso AdWatch (SPEC-0000 secao 11)."""


# --------------------------------------------------------------------------- #
# O material do cenario
# --------------------------------------------------------------------------- #
FALA_DO_CLARO: Final[str] = "Na Claro voce tem mais internet pra aproveitar tudo o que voce gosta"
"""Como a peca da Claro foi dita no ar — variacao lexical do texto catalogado."""

FALA_DA_VIVO: Final[str] = "Vivo fibra chega na sua casa com velocidade de verdade"
"""A peca da Vivo foi dita exatamente como esta no catalogo (grupo de controle)."""

VEICULACOES: Final[dict[str, tuple[float, float]]] = {
    "COM_000001": (120.0, 150.0),
    "COM_000002": (400.0, 430.0),
}
"""Intervalos em que cada comercial de fato foi ao ar, em segundos."""

TOLERANCIA_DE_BORDA: Final[float] = 5.0
"""Erro maximo aceito em cada borda da deteccao, em segundos."""

PROGRAMA: Final[tuple[tuple[str, float, float], ...]] = (
    ("bom dia e bem vindos ao nosso jornal desta manha", 0.0, 40.0),
    ("hoje falamos da previsao do tempo e do transito nas principais avenidas", 40.0, 80.0),
    ("depois do intervalo teremos a entrevista com o tecnico da selecao", 80.0, 120.0),
    (FALA_DO_CLARO, 120.0, 150.0),
    ("voltamos ao estudio com a analise do nosso comentarista esportivo", 150.0, 200.0),
    ("a reportagem visitou o museu municipal reformado no fim do ano passado", 200.0, 260.0),
    ("os moradores do bairro pedem mais arborizacao nas calcadas do centro", 260.0, 330.0),
    ("agora um recado dos nossos anunciantes fiquem conosco no intervalo", 330.0, 400.0),
    (FALA_DA_VIVO, 400.0, 430.0),
    ("de volta ao estudio o boletim indica chuva no fim da tarde", 430.0, 500.0),
    ("no esporte o time local venceu por dois a zero na noite de ontem", 500.0, 560.0),
    ("obrigado por acompanhar e ate a proxima edicao do nosso telejornal", 560.0, 600.0),
)
"""Dez minutos de programa: dois blocos comerciais e nada mais que os imite."""

CATALOGO: Final[tuple[dict[str, Any], ...]] = (
    {
        "commercial_id": "COM_000001",
        "brand": "Claro",
        "campaign": "Claro Internet",
        "text": "Na Claro voce tem muito mais internet para aproveitar tudo que gosta",
        "duration_expected": 30.0,
    },
    {
        "commercial_id": "COM_000002",
        "brand": "Vivo",
        "campaign": "Vivo Fibra",
        "text": "Vivo fibra chega na sua casa com velocidade de verdade",
        "duration_expected": 30.0,
    },
    {
        "commercial_id": "COM_000003",
        "brand": "Banco Azul",
        "campaign": "Conta Digital",
        "text": "Abra sua conta digital no Banco Azul e peca o cartao sem anuidade",
        "duration_expected": 30.0,
    },
    {
        "commercial_id": "COM_000004",
        "brand": "Claro",
        "campaign": "Claro Prezao",
        "text": "Prezao com ligacoes a vontade e recarga programada todo mes",
        "duration_expected": 15.0,
    },
    {
        "commercial_id": "COM_000005",
        "brand": "Tim",
        "campaign": "Tim Black",
        "text": "Tim Black entrega roaming internacional sem custo adicional",
        "duration_expected": 20.0,
    },
)
"""Os cinco comerciais do catalogo; apenas os dois primeiros vao ao ar."""

AUSENTES_DO_AR: Final[frozenset[str]] = frozenset({"COM_000003", "COM_000004", "COM_000005"})
"""Codigos que nao podem aparecer em deteccao nenhuma."""

LETREIROS: Final[tuple[dict[str, Any], ...]] = (
    {"text": "Claro internet para aproveitar", "start": 122.0, "end": 148.0, "confidence": 0.95},
    {"text": "Vivo fibra velocidade de verdade", "start": 402.0, "end": 428.0, "confidence": 0.95},
)
"""Texto em tela de cada bloco comercial, usado no teste do sinal de OCR."""

CORTES_DE_CENA: Final[tuple[dict[str, Any], ...]] = (
    {"index": 0, "start": 0.0, "end": 119.5},
    {"index": 1, "start": 119.5, "end": 150.5},
    {"index": 2, "start": 150.5, "end": 399.5},
    {"index": 3, "start": 399.5, "end": 430.5},
    {"index": 4, "start": 430.5, "end": 600.0},
)
"""Cortes que delimitam os dois blocos comerciais no video."""

FRONTEIRAS_DE_CENA: Final[frozenset[float]] = frozenset(
    valor for corte in CORTES_DE_CENA for valor in (corte["start"], corte["end"])
)
"""Instantes de corte; o refino so pode encaixar as bordas em um destes."""

MAX_DESLOCAMENTO_DE_REFINO: Final[float] = 3.0
"""Teto do encaixe em corte de cena, em segundos (SPEC-0010 secao 3.8)."""

SINAIS_DA_EVIDENCIA: Final[tuple[str, ...]] = (
    "speech_match",
    "semantic_match",
    "ocr_match",
    "visual_match",
    "duration_match",
)
"""Os cinco sinais da fusao de score (SPEC-0010 secao 3.5)."""

LOTE: Final[tuple[dict[str, Any], ...]] = (
    {
        "commercial_id": "COM_000101",
        "brand": "Oi",
        "campaign": "Oi Fibra",
        "text": "Oi fibra instalada em ate quarenta e oito horas na sua residencia",
    },
    {
        "commercial_id": "COM_000102",
        "brand": "Sky",
        "campaign": "Sky Pre",
        "text": "Sky pre pago com recarga pelo aplicativo e canais abertos",
    },
)
"""Lote de importacao em massa, disjunto do catalogo criado item a item."""


def transcricao_whisperx() -> list[dict[str, Any]]:
    """Monta a transcricao no formato de lista do WhisperX, palavra a palavra.

    Cada trecho tem as suas palavras distribuidas uniformemente no intervalo, que
    e o que torna as fronteiras previsiveis: `("a b", 10, 12)` produz `a[10,11]` e
    `b[11,12]`.
    """
    palavras: list[dict[str, Any]] = []
    for texto, inicio, fim in PROGRAMA:
        tokens = texto.split()
        passo = (fim - inicio) / len(tokens)
        for posicao, token in enumerate(tokens):
            palavras.append(
                {
                    "word": token,
                    "start": round(inicio + posicao * passo, 3),
                    "end": round(inicio + (posicao + 1) * passo, 3),
                    "score": 0.99,
                }
            )
    return palavras


def por_codigo(deteccoes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Indexa as deteccoes do relatorio pelo codigo de negocio do comercial."""
    return {item["commercial_code"]: item for item in deteccoes}


def distancia_ao_corte(instante: float) -> float:
    """Distancia, em segundos, do instante ao corte de cena mais proximo."""
    return min(abs(instante - fronteira) for fronteira in FRONTEIRAS_DE_CENA)


# --------------------------------------------------------------------------- #
# Fixtures do cenario
# --------------------------------------------------------------------------- #
@pytest.fixture
async def catalogo(client: AsyncClient) -> dict[str, dict[str, Any]]:
    """Cria os cinco comerciais pela API e devolve o corpo de cada um, por codigo."""
    criados: dict[str, dict[str, Any]] = {}
    for item in CATALOGO:
        resposta = await client.post(f"{API}/commercials", json=item)
        assert resposta.status_code == 201, resposta.text
        criados[item["commercial_id"]] = resposta.json()
    return criados


@pytest.fixture
async def midia(client: AsyncClient) -> str:
    """Registra o ativo de midia do telejornal e devolve o seu identificador."""
    resposta = await client.post(
        f"{API}/media",
        json={
            "uri": "file:///acervo/telejornal-2026-01-15.mp4",
            "title": "Telejornal da manha",
            "duration_seconds": 600.0,
            "fps": 25.0,
        },
    )
    assert resposta.status_code == 201, resposta.text
    return str(resposta.json()["id"])


@pytest.fixture
async def midia_transcrita(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]], midia: str
) -> str:
    """Importa a transcricao sintetica: e o que destrava o funil sem FFmpeg."""
    resposta = await client.post(f"{API}/media/{midia}/transcript", json=transcricao_whisperx())
    assert resposta.status_code == 201, resposta.text
    assert resposta.json()["imported"] > 0
    return midia


@pytest.fixture
async def relatorio(client: AsyncClient, midia_transcrita: str) -> dict[str, Any]:
    """Roda `POST /media/{id}/detect` sobre a transcricao e devolve o relatorio."""
    resposta = await client.post(f"{API}/media/{midia_transcrita}/detect", json={})
    assert resposta.status_code == 200, resposta.text
    return dict(resposta.json())


# --------------------------------------------------------------------------- #
# CRUD do catalogo (SPEC-0010 secao 6, criterio 1)
# --------------------------------------------------------------------------- #
async def test_criar_os_cinco_comerciais_gera_fingerprint_para_cada_um(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """Criar comercial e criar assinatura: sem ela a peca fica invisivel ao funil."""
    assert len(catalogo) == len(CATALOGO)

    for codigo in catalogo:
        detalhe = (await client.get(f"{API}/commercials/{codigo}")).json()
        assinatura = detalhe["fingerprint"]
        assert assinatura is not None, f"{codigo} ficou sem assinatura"
        assert assinatura["normalized_text"], f"{codigo} tem assinatura sem texto normalizado"
        assert assinatura["token_set"], f"{codigo} tem assinatura sem conjunto de tokens"
        assert assinatura["has_embedding"] is True, f"{codigo} nao recebeu embedding"


async def test_listar_comerciais_filtrando_por_marca_devolve_so_aquela_marca(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """`GET /commercials?brand=Claro` traz as duas pecas da Claro e mais nenhuma."""
    resposta = await client.get(f"{API}/commercials", params={"brand": "Claro"})

    assert resposta.status_code == 200
    pagina = resposta.json()
    codigos = sorted(item["commercial_id"] for item in pagina["items"])
    assert codigos == ["COM_000001", "COM_000004"], f"filtro por marca devolveu {codigos}"
    assert pagina["total"] == 2


async def test_obter_comercial_resolve_pelo_codigo_de_negocio_e_pelo_id(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """A UI carrega o id, o operador conhece o codigo: as duas chaves resolvem."""
    interno = catalogo["COM_000002"]["id"]

    por_id = await client.get(f"{API}/commercials/{interno}")
    por_negocio = await client.get(f"{API}/commercials/COM_000002")

    assert por_id.status_code == 200
    assert por_negocio.status_code == 200
    assert por_id.json()["commercial"]["id"] == por_negocio.json()["commercial"]["id"] == interno


async def test_atualizar_o_texto_regenera_o_fingerprint(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """Mudar `text` muda o que o matching procura — a assinatura tem de acompanhar."""
    interno = catalogo["COM_000005"]["id"]
    antes = (await client.get(f"{API}/commercials/{interno}")).json()["fingerprint"]
    novo_texto = "Tim Black agora com franquia dobrada e ligacoes ilimitadas"

    atualizado = await client.put(f"{API}/commercials/{interno}", json={"text": novo_texto})

    assert atualizado.status_code == 200, atualizado.text
    assert atualizado.json()["text"] == novo_texto
    depois = (await client.get(f"{API}/commercials/{interno}")).json()["fingerprint"]
    assert depois["normalized_text"] != antes["normalized_text"], (
        "o fingerprint continuou apontando para o texto antigo"
    )
    assert "franquia" in depois["normalized_text"]
    assert "roaming" not in depois["normalized_text"]
    assert depois["token_set"] != antes["token_set"]
    assert depois["id"] == antes["id"], "regerar a assinatura nao pode trocar a sua identidade"


async def test_atualizar_campo_que_nao_afeta_o_matching_preserva_o_fingerprint(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """Renomear a campanha nao e motivo para reconstruir a assinatura."""
    interno = catalogo["COM_000003"]["id"]
    antes = (await client.get(f"{API}/commercials/{interno}")).json()["fingerprint"]

    resposta = await client.put(f"{API}/commercials/{interno}", json={"campaign": "Conta Azul"})

    assert resposta.status_code == 200
    depois = (await client.get(f"{API}/commercials/{interno}")).json()["fingerprint"]
    assert depois["normalized_text"] == antes["normalized_text"]


async def test_apagar_comercial_o_remove_do_catalogo(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """`DELETE` remove a peca e o `GET` seguinte responde 404."""
    interno = catalogo["COM_000004"]["id"]

    apagado = await client.delete(f"{API}/commercials/{interno}")

    assert apagado.status_code == 204
    assert (await client.get(f"{API}/commercials/{interno}")).status_code == 404
    restantes = (await client.get(f"{API}/commercials")).json()
    assert restantes["total"] == len(CATALOGO) - 1


async def test_codigo_de_negocio_duplicado_responde_409(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """O codigo de negocio e unico: repeti-lo e conflito, nao um segundo cadastro."""
    resposta = await client.post(f"{API}/commercials", json=dict(CATALOGO[0]))

    assert resposta.status_code == 409
    assert resposta.json()["error"]["code"] == "conflict"


# --------------------------------------------------------------------------- #
# Importacao em lote
# --------------------------------------------------------------------------- #
async def test_importacao_em_lote_cria_todos_os_itens_do_array(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """`POST /commercials/bulk` aceita o array puro e devolve o que criou."""
    resposta = await client.post(f"{API}/commercials/bulk", json=list(LOTE))

    assert resposta.status_code == 200, resposta.text
    resultado = resposta.json()
    assert len(resultado["created"]) == len(LOTE)
    assert resultado["errors"] == []
    assert resultado["total"] == len(LOTE)
    pagina = (await client.get(f"{API}/commercials", params={"limit": 50})).json()
    assert pagina["total"] == len(CATALOGO) + len(LOTE)


async def test_importacao_em_lote_isola_o_codigo_ja_existente_em_skipped(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]]
) -> None:
    """Um item repetido nao derruba o lote: fica em `skipped`, o resto entra."""
    lote = [dict(CATALOGO[0]), dict(LOTE[0])]

    resultado = (await client.post(f"{API}/commercials/bulk", json=lote)).json()

    assert [item["commercial_id"] for item in resultado["created"]] == ["COM_000101"]
    assert len(resultado["skipped"]) == 1, resultado["skipped"]
    assert resultado["errors"] == []


# --------------------------------------------------------------------------- #
# Deteccao (SPEC-0010 secao 6, criterios 2, 4 e 5)
# --------------------------------------------------------------------------- #
async def test_deteccao_offline_encontra_exatamente_os_dois_comerciais_veiculados(
    relatorio: dict[str, Any],
) -> None:
    """Sem rede e sem FFmpeg, o funil devolve as duas veiculacoes e so elas."""
    encontrados = por_codigo(relatorio["detections"])

    assert sorted(encontrados) == sorted(VEICULACOES), (
        f"o relatorio trouxe {sorted(encontrados)} em vez de {sorted(VEICULACOES)}"
    )
    assert relatorio["persisted"] == 2
    assert relatorio["commercials"] == len(CATALOGO)
    assert relatorio["windows"] > 0 and relatorio["candidates"] > 0


async def test_intervalos_detectados_batem_com_a_veiculacao_real(
    relatorio: dict[str, Any],
) -> None:
    """Cada borda cai dentro de cinco segundos do instante real de veiculacao."""
    encontrados = por_codigo(relatorio["detections"])

    desvios = {
        codigo: (
            round(encontrados[codigo]["start"] - inicio, 3),
            round(encontrados[codigo]["end"] - fim, 3),
        )
        for codigo, (inicio, fim) in VEICULACOES.items()
    }
    fora = {
        codigo: par
        for codigo, par in desvios.items()
        if abs(par[0]) > TOLERANCIA_DE_BORDA or abs(par[1]) > TOLERANCIA_DE_BORDA
    }
    assert not fora, f"bordas fora da tolerancia de {TOLERANCIA_DE_BORDA}s: {fora}"


async def test_comercial_com_variacao_lexical_continua_sendo_detectado(
    relatorio: dict[str, Any],
) -> None:
    """A peca da Claro foi dita com outras palavras e ainda assim casou (criterio 4)."""
    claro = por_codigo(relatorio["detections"])["COM_000001"]

    assert FALA_DO_CLARO.split()[0].lower() in claro["evidence"]["matched_text"].lower()
    assert claro["evidence"]["speech_match"] < 1.0, (
        "o cenario perdeu a variacao lexical: a locucao ficou identica ao catalogo"
    )
    assert claro["evidence"]["speech_match"] >= 0.85, (
        f"a variacao lexical derrubou o casamento para {claro['evidence']['speech_match']}"
    )
    assert claro["confidence"] >= 0.60, "a peca com variacao lexical caiu abaixo da revisao"


async def test_evidencia_traz_os_cinco_sinais_da_fusao(relatorio: dict[str, Any]) -> None:
    """Toda deteccao carrega os cinco sinais mais a ordem e o trecho que casou."""
    for codigo, deteccao in por_codigo(relatorio["detections"]).items():
        evidencia = deteccao["evidence"]
        faltando = [sinal for sinal in SINAIS_DA_EVIDENCIA if sinal not in evidencia]
        assert not faltando, f"{codigo} veio sem os sinais {faltando}"
        assert evidencia["speech_match"] > 0.0, f"{codigo} sem sinal lexico"
        assert evidencia["semantic_match"] > 0.0, f"{codigo} sem sinal semantico"
        assert evidencia["duration_match"] > 0.0, f"{codigo} sem aderencia de duracao"
        assert evidencia["order_ok"] is True, f"{codigo} deveria respeitar a ordem das ancoras"
        assert evidencia["matched_text"], f"{codigo} sem trecho de transcricao casado"


async def test_sem_juiz_visual_o_sinal_visual_apenas_herda_o_lexico(
    relatorio: dict[str, Any],
) -> None:
    """Nao havendo VLM instalado, `visual_match` copia o lexico — nunca inventa 1.0."""
    assert relatorio["vision_available"] is False
    assert relatorio["vision_calls"] == 0

    for codigo, deteccao in por_codigo(relatorio["detections"]).items():
        evidencia = deteccao["evidence"]
        assert evidencia["visual_match"] == pytest.approx(evidencia["speech_match"]), (
            f"{codigo} recebeu veredito visual sem juiz visual instalado"
        )
        assert deteccao["verified_by_vlm"] is False


async def test_sem_ocr_importado_o_sinal_de_tela_fica_zerado_e_limita_o_score(
    relatorio: dict[str, Any],
) -> None:
    """Sem letreiro nao ha `ocr_match`, e o teto do score cai pelo peso perdido."""
    assert relatorio["ocr_texts"] == 0

    for codigo, deteccao in por_codigo(relatorio["detections"]).items():
        assert deteccao["evidence"]["ocr_match"] == 0.0, f"{codigo} inventou sinal de OCR"
        assert deteccao["status"] == DetectionStatus.NEEDS_REVIEW.value, (
            f"{codigo} foi aceito sem OCR e sem juiz visual, o que o teto nao permite"
        )


async def test_comercial_ausente_do_audio_nao_aparece_no_relatorio(
    relatorio: dict[str, Any],
) -> None:
    """As tres pecas que nao foram ao ar nao produzem deteccao (criterio 5)."""
    encontrados = set(por_codigo(relatorio["detections"]))

    intrusos = sorted(encontrados & AUSENTES_DO_AR)
    assert not intrusos, f"comerciais que nao foram ao ar apareceram: {intrusos}"


async def test_deteccoes_ficam_persistidas_e_consultaveis_pela_midia(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """O relatorio nao e efemero: as deteccoes ficam gravadas na midia."""
    pagina = (await client.get(f"{API}/media/{midia_transcrita}/detections")).json()

    assert pagina["total"] == 2
    assert sorted(item["commercial_code"] for item in pagina["items"]) == sorted(VEICULACOES)
    detalhe = (await client.get(f"{API}/detections/{pagina['items'][0]['id']}")).json()
    assert detalhe["evidence"]["matched_text"]


async def test_importar_letreiro_de_ocr_soma_o_quinto_sinal_e_promove_a_aceito(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """Com o texto em tela, `ocr_match` entra na conta e as duas pecas sao aceitas.

    E o comportamento que a SPEC-0010 secao 3.5 desenha: casamento textual forte
    nao e prova; o letreiro e o que fecha os `0.90`.
    """
    antes = por_codigo(relatorio["detections"])

    importado = await client.post(f"{API}/media/{midia_transcrita}/ocr", json=list(LETREIROS))
    assert importado.status_code == 201, importado.text
    novo = (await client.post(f"{API}/media/{midia_transcrita}/detect", json={})).json()

    depois = por_codigo(novo["detections"])
    assert novo["ocr_texts"] == len(LETREIROS)
    assert sorted(depois) == sorted(VEICULACOES)
    for codigo in VEICULACOES:
        assert depois[codigo]["evidence"]["ocr_match"] > 0.0, f"{codigo} nao usou o letreiro"
        assert depois[codigo]["confidence"] > antes[codigo]["confidence"], (
            f"{codigo} nao ganhou score com o sinal de OCR"
        )
        assert depois[codigo]["status"] == DetectionStatus.ACCEPTED.value


# --------------------------------------------------------------------------- #
# Refino de fronteira por cortes de cena (SPEC-0010 secao 3.8)
# --------------------------------------------------------------------------- #
async def test_cortes_de_cena_refinam_as_fronteiras_das_deteccoes(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """Importar os cortes e rodar de novo encaixa as bordas no corte mais proximo."""
    antes = por_codigo(relatorio["detections"])
    assert all(item["refined_by_scene"] is False for item in antes.values()), (
        "sem cortes importados nenhuma deteccao pode se dizer refinada por cena"
    )

    importado = await client.post(
        f"{API}/media/{midia_transcrita}/scenes", json=list(CORTES_DE_CENA)
    )
    assert importado.status_code == 201, importado.text
    novo = (await client.post(f"{API}/media/{midia_transcrita}/detect", json={})).json()

    depois = por_codigo(novo["detections"])
    assert novo["scene_cuts"] == len(CORTES_DE_CENA)
    for codigo in VEICULACOES:
        assert depois[codigo]["refined_by_scene"] is True, f"{codigo} nao foi refinado por cena"
        assert depois[codigo]["start"] in FRONTEIRAS_DE_CENA, (
            f"{codigo} comeca em {depois[codigo]['start']}, que nao e um corte"
        )
        assert depois[codigo]["end"] in FRONTEIRAS_DE_CENA, (
            f"{codigo} termina em {depois[codigo]['end']}, que nao e um corte"
        )


async def test_refino_por_cena_aproxima_as_bordas_dos_cortes(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """Cada borda fica **mais perto** do corte de cena do que estava antes do refino.

    A medida e a distancia ao corte mais proximo: antes do refino as bordas saem
    da primeira e da ultima palavra casada e ficam a alguns decimos do corte;
    depois, encaixam nele. O deslocamento nunca passa dos tres segundos que a
    SPEC-0010 secao 3.8 autoriza, e as bordas continuam dentro da tolerancia
    contra o instante real de veiculacao.
    """
    antes = por_codigo(relatorio["detections"])
    await client.post(f"{API}/media/{midia_transcrita}/scenes", json=list(CORTES_DE_CENA))
    depois = por_codigo(
        (await client.post(f"{API}/media/{midia_transcrita}/detect", json={})).json()["detections"]
    )

    for codigo, (inicio, fim) in VEICULACOES.items():
        for borda in ("start", "end"):
            longe = distancia_ao_corte(antes[codigo][borda])
            perto = distancia_ao_corte(depois[codigo][borda])
            assert perto < longe, (
                f"{codigo}.{borda} nao se aproximou do corte: {longe:.3f}s -> {perto:.3f}s"
            )
            assert perto == pytest.approx(0.0), f"{codigo}.{borda} nao encaixou em corte nenhum"
            deslocamento = abs(depois[codigo][borda] - antes[codigo][borda])
            assert deslocamento <= MAX_DESLOCAMENTO_DE_REFINO, (
                f"{codigo}.{borda} deslocou {deslocamento:.3f}s, acima do teto do refino"
            )
        assert abs(depois[codigo]["start"] - inicio) <= TOLERANCIA_DE_BORDA
        assert abs(depois[codigo]["end"] - fim) <= TOLERANCIA_DE_BORDA


async def test_reexecutar_a_deteccao_substitui_o_resultado_anterior(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """O funil e reanalise completa: nao acumula a mesma veiculacao duas vezes."""
    segundo = (await client.post(f"{API}/media/{midia_transcrita}/detect", json={})).json()

    assert segundo["replaced"] == relatorio["persisted"]
    pagina = (await client.get(f"{API}/media/{midia_transcrita}/detections")).json()
    assert pagina["total"] == 2, "a segunda execucao duplicou as deteccoes"


# --------------------------------------------------------------------------- #
# Revisao humana
# --------------------------------------------------------------------------- #
async def test_revisao_humana_muda_o_status_da_deteccao(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """`PATCH /detections/{id}` grava o veredito do revisor sobre o da maquina."""
    deteccao = por_codigo(relatorio["detections"])["COM_000001"]
    assert deteccao["status"] == DetectionStatus.NEEDS_REVIEW.value

    resposta = await client.patch(
        f"{API}/detections/{deteccao['id']}",
        json={
            "status": DetectionStatus.ACCEPTED.value,
            "notes": "conferido no video pelo analista",
        },
    )

    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["status"] == DetectionStatus.ACCEPTED.value
    relido = (await client.get(f"{API}/detections/{deteccao['id']}")).json()
    assert relido["status"] == DetectionStatus.ACCEPTED.value


async def test_revisao_humana_nao_reescreve_o_score_nem_a_evidencia(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """A revisao registra o julgamento humano; nao apaga o que a maquina mediu."""
    deteccao = por_codigo(relatorio["detections"])["COM_000002"]

    revisada = (
        await client.patch(
            f"{API}/detections/{deteccao['id']}",
            json={"status": DetectionStatus.REJECTED.value, "notes": "era uma chamada, nao a peca"},
        )
    ).json()

    assert revisada["confidence"] == pytest.approx(deteccao["confidence"])
    assert revisada["evidence"] == deteccao["evidence"]
    assert revisada["start"] == deteccao["start"] and revisada["end"] == deteccao["end"]


async def test_filtrar_deteccoes_por_status_isola_a_fila_de_revisao(
    client: AsyncClient, midia_transcrita: str, relatorio: dict[str, Any]
) -> None:
    """Depois de aceitar uma, a fila `needs_review` fica so com a outra."""
    aceita = por_codigo(relatorio["detections"])["COM_000001"]
    await client.patch(
        f"{API}/detections/{aceita['id']}", json={"status": DetectionStatus.ACCEPTED.value}
    )

    fila = (
        await client.get(f"{API}/detections", params={"status": DetectionStatus.NEEDS_REVIEW.value})
    ).json()

    assert [item["commercial_code"] for item in fila["items"]] == ["COM_000002"]


# --------------------------------------------------------------------------- #
# Capacidades e falhas instrutivas (SPEC-0010 secao 6, criterio 6)
# --------------------------------------------------------------------------- #
async def test_capabilities_reporta_tudo_indisponivel_nesta_instalacao(
    client: AsyncClient,
) -> None:
    """Sem FFmpeg, WhisperX, PaddleOCR, PySceneDetect e VLM, a API diz isso."""
    resposta = await client.get(f"{API}/capabilities")

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["capabilities"] == {
        "probe": False,
        "asr": False,
        "ocr": False,
        "scenes": False,
        "vision": False,
    }
    assert sorted(corpo["degraded"]) == ["asr", "ocr", "probe", "scenes", "vision"]
    assert corpo["can_ingest"] is False, "sem sondagem e sem ASR nao ha ingestao automatica"
    assert corpo["can_detect"] is True, "a deteccao continua possivel pelo caminho de importacao"


async def test_capabilities_diz_o_que_instalar_para_cada_capacidade_ausente(
    client: AsyncClient,
) -> None:
    """Reportar 'ausente' sem dizer o proximo passo deixaria o operador parado."""
    detalhes = (await client.get(f"{API}/capabilities")).json()["details"]

    assert [item["name"] for item in detalhes] == ["probe", "asr", "ocr", "scenes", "vision"]
    for item in detalhes:
        assert item["available"] is False
        assert item["adapter"] is None
        assert item["hint"].strip(), f"{item['name']} nao explica como ser habilitada"
        assert item["detail"].startswith("Indisponivel:"), item["detail"]
        assert item["hint"] in item["detail"]


async def test_capabilities_publica_os_pesos_e_limiares_normativos(
    client: AsyncClient,
) -> None:
    """Os pesos da fusao e os limiares vem de `Settings` e somam 1.0 (criterio 3)."""
    corpo = (await client.get(f"{API}/capabilities")).json()

    assert corpo["weights"] == {
        "lexical": 0.40,
        "semantic": 0.25,
        "ocr": 0.15,
        "visual": 0.15,
        "duration": 0.05,
    }
    assert sum(corpo["weights"].values()) == pytest.approx(1.0)
    assert corpo["thresholds"] == {"accept": 0.90, "review": 0.60}
    assert corpo["max_score_without"]["ocr"] == pytest.approx(0.85)


async def test_ingestao_sem_adaptador_algum_pula_todas_as_etapas_sem_derrubar(
    client: AsyncClient, midia: str
) -> None:
    """`POST /media/{id}/ingest` responde 200 com tudo em `skipped` (secao 3.1)."""
    resposta = await client.post(f"{API}/media/{midia}/ingest")

    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["completed"] == []
    assert corpo["failed"] == []
    assert sorted(corpo["skipped"]) == ["asr", "audio", "ocr", "probe", "scenes"]


async def test_detectar_sem_transcricao_responde_422_com_mensagem_instrutiva(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]], midia: str
) -> None:
    """Midia sem linha do tempo nao devolve relatorio vazio: devolve o proximo passo."""
    resposta = await client.post(f"{API}/media/{midia}/detect", json={})

    assert resposta.status_code == 422, resposta.text
    erro = resposta.json()["error"]
    assert erro["code"] == "validation_error"
    assert "transcricao" in erro["message"].lower()
    assert f"{API}/media/{midia}/transcript" in erro["message"], (
        "a mensagem precisa dizer qual rota chamar para destravar a deteccao"
    )
    assert erro["details"]["hint"] == "import_transcript"
    assert f"{API}/media/{midia}/ingest" in erro["details"]["endpoints"]


async def test_detectar_em_midia_inexistente_responde_404(client: AsyncClient) -> None:
    """Identificador desconhecido e 404, e nao um funil rodando no vazio."""
    resposta = await client.post(
        f"{API}/media/00000000-0000-0000-0000-000000000000/detect", json={}
    )

    assert resposta.status_code == 404
    assert resposta.json()["error"]["code"] == "not_found"


async def test_detalhe_da_midia_mostra_os_artefatos_que_destravam_a_deteccao(
    client: AsyncClient, midia_transcrita: str
) -> None:
    """`GET /media/{id}` conta o que existe antes de gastar processamento."""
    corpo = (await client.get(f"{API}/media/{midia_transcrita}")).json()

    assert corpo["artifacts"]["transcript"] is True
    assert corpo["artifacts"]["transcript_words"] == len(transcricao_whisperx())
    assert corpo["artifacts"]["transcript_source"] == "import"
    assert corpo["artifacts"]["scene_cuts"] == 0
    assert corpo["capabilities"] == dict.fromkeys(
        ("probe", "asr", "ocr", "scenes", "vision"), False
    )


async def test_reindex_reassina_o_catalogo_com_o_embedder_atual(
    client: AsyncClient, catalogo: dict[str, dict[str, Any]], container
) -> None:
    """`ReindexCommercials` reescreve TODAS as assinaturas com o embedder de agora.

    A sequencia que motivou o comando e realista: catalogo importado com a
    instalacao em modo offline (`HashingEmbedder`), rede do hub volta, operador
    troca o provedor — e a busca semantica passa a comparar vetores de espacos
    diferentes, devolvendo similaridades sem significado, em silencio. O
    reindex fecha essa janela; este teste prova que ele reescreve de verdade,
    trocando o embedder por um de OUTRA dimensao e conferindo que os vetores
    gravados acompanham.
    """
    from lukato.adapters.embeddings.hashing import HashingEmbedder
    from lukato.application.use_cases.adwatch import ReindexCommercials
    from lukato.domain.models.identity import Principal

    principal = Principal.anonymous_root()

    async with container.uow_factory() as uow:
        antes = await uow.commercials.list_fingerprints()
    dims_antes = {len(f.embedding) for f in antes if f.embedding}
    assert dims_antes, "o catalogo de fixture deveria ter assinaturas com embedding"

    outra_dimensao = 64
    assert outra_dimensao not in dims_antes
    original = container.embeddings
    object.__setattr__(container, "embeddings", HashingEmbedder(dimensions=outra_dimensao))
    try:
        placar = await ReindexCommercials(container).execute(principal)
    finally:
        object.__setattr__(container, "embeddings", original)

    assert placar["total"] == len(catalogo)
    assert placar["com_embedding"] == len(catalogo)
    assert placar["sem_embedding"] == 0

    async with container.uow_factory() as uow:
        depois = await uow.commercials.list_fingerprints()
    dims_depois = {len(f.embedding) for f in depois if f.embedding}
    assert dims_depois == {outra_dimensao}, (
        f"o reindex nao reescreveu os vetores: dimensoes gravadas {sorted(dims_depois)}"
    )


# ---------------------------------------------------------------------------
# Upload de midia (POST /media/upload)
# ---------------------------------------------------------------------------
async def test_upload_grava_a_copia_no_workdir_e_registra_o_ativo(
    client: AsyncClient, settings: Any
) -> None:
    """O caminho hospedado: o arquivo sobe, vira copia no volume e sai registrado."""
    import asyncio
    from pathlib import Path

    conteudo = b"\x00\x00\x00\x18ftypmp42" + b"\xab" * 4096
    resposta = await client.post(
        f"{API}/media/upload",
        files={"file": ("Torcida Multi (1).mp4", conteudo, "video/mp4")},
        data={"title": "Torcida Multi", "kind": "video"},
    )
    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()

    gravado = Path(corpo["uri"])
    esperado = Path(settings.adwatch.workdir) / "uploads"
    assert gravado.parent == esperado, corpo["uri"]
    dados_no_disco = await asyncio.to_thread(gravado.read_bytes)
    assert dados_no_disco == conteudo, "a copia no disco difere do que subiu"
    # O nome enviado e conteudo do cliente: parenteses e espacos nao sobrevivem,
    # e um prefixo aleatorio impede colisao entre uploads homonimos.
    assert gravado.name.endswith("-Torcida_Multi_1_.mp4"), gravado.name

    assert corpo["status"] == "registered"
    assert corpo["title"] == "Torcida Multi"
    assert corpo["metadata"]["upload"]["original_filename"] == "Torcida Multi (1).mp4"
    assert corpo["metadata"]["upload"]["size_bytes"] == len(conteudo)

    detalhe = await client.get(f"{API}/media/{corpo['id']}")
    assert detalhe.status_code == 200
    assert detalhe.json()["media"]["uri"] == corpo["uri"]


async def test_upload_sem_titulo_usa_o_nome_do_arquivo(client: AsyncClient) -> None:
    """Sem titulo informado, o basename saneado (sem extensao) da nome ao ativo."""
    resposta = await client.post(
        f"{API}/media/upload",
        files={"file": ("chamada.mp3", b"ID3\x04" + b"\x01" * 64, "audio/mpeg")},
        data={"kind": "audio"},
    )
    assert resposta.status_code == 201, resposta.text
    assert resposta.json()["title"] == "chamada"


async def test_upload_rejeita_extensao_que_nao_corresponde_a_natureza(
    client: AsyncClient, settings: Any
) -> None:
    """Extensao fora da lista da natureza responde 422 e nao deixa arquivo para tras."""
    from pathlib import Path

    casos = [
        ("payload.exe", "video"),
        ("video.mp4", "audio"),
        ("sem_extensao", "video"),
    ]
    for nome, natureza in casos:
        resposta = await client.post(
            f"{API}/media/upload",
            files={"file": (nome, b"\x00" * 32, "application/octet-stream")},
            data={"kind": natureza},
        )
        assert resposta.status_code == 422, f"{nome}/{natureza}: {resposta.text}"

    pasta = Path(settings.adwatch.workdir) / "uploads"
    assert not pasta.exists() or not any(pasta.iterdir()), (
        "upload rejeitado nao pode deixar arquivo no volume"
    )


async def test_upload_acima_do_teto_responde_413_e_apaga_o_parcial(
    client: AsyncClient, settings: Any
) -> None:
    """Passar de `upload_max_mb` derruba o envio com 413 e descarta o parcial."""
    from pathlib import Path

    settings.adwatch.upload_max_mb = 1
    try:
        resposta = await client.post(
            f"{API}/media/upload",
            files={"file": ("longa.mp4", b"\x00" * (1024 * 1024 + 1), "video/mp4")},
            data={"kind": "video"},
        )
    finally:
        settings.adwatch.upload_max_mb = 2048
    assert resposta.status_code == 413, resposta.text

    pasta = Path(settings.adwatch.workdir) / "uploads"
    assert not pasta.exists() or not any(pasta.iterdir()), (
        "o parcial de um upload estourado deveria ter sido apagado"
    )
