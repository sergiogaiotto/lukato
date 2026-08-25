#!/usr/bin/env python
"""Prova executavel do pipeline AdWatch (SPEC-0010).

    make install-dev && python scripts/prova_adwatch.py

Roda o funil inteiro — catalogo, fingerprints, janelas deslizantes, retrieval,
fusao de score, supressao de sobreposicao e refino de fronteira — **sem FFmpeg,
sem WhisperX, sem GPU e sem rede**, pelo caminho de importacao de transcricao.

O que a prova exige:

* o comercial presente no audio COM VARIACAO LEXICAL vira candidato, e o intervalo
  bate com o instante real depois do refino por cortes de cena;
* sem OCR e sem juiz multimodal — ambos indisponiveis offline — o candidato para
  em `needs_review` em vez de ser promovido: a SPEC-0010 3.6 proibe aceitar sem
  a evidencia visual, e o pipeline obedece;
* um comercial cujas palavras aparecem espalhadas FORA DE ORDEM nao e aceito —
  encontrar os termos nao basta, a ordem importa;
* um comercial ausente do audio nao aparece;
* cortes de cena aproximam as bordas da deteccao.
"""

from __future__ import annotations

import asyncio

from _prova_isolada import isolar_banco, limpar_banco

from lukato.adapters.embeddings.hashing import HashingEmbedder
from lukato.adapters.media.importers import SceneImporter, TranscriptImporter
from lukato.adapters.persistence.session import build_engine, build_sessionmaker, create_all
from lukato.adapters.persistence.uow import UnitOfWorkFactoryImpl
from lukato.config import get_settings, reset_settings_cache

CATALOGO = [
    {
        "commercial_id": "COM_000234",
        "campaign": "Claro Pos 100GB",
        "brand": "Claro",
        "text": "Na Claro voce tem muito mais internet para aproveitar tudo que gosta",
        "keywords": ["Claro", "internet"],
        "duration_expected": 30.0,
    },
    {
        "commercial_id": "COM_000235",
        "campaign": "Claro Flex",
        "brand": "Claro",
        "text": "Com Claro Flex voce controla tudo pelo aplicativo quando quiser",
        "keywords": ["Claro Flex", "aplicativo"],
        "duration_expected": 15.0,
    },
    {
        "commercial_id": "COM_000236",
        "campaign": "Plano Familia",
        "brand": "Claro",
        "text": "Conheca o novo plano Claro com 50 giga de internet e WhatsApp ilimitado",
        "keywords": ["50 giga", "WhatsApp"],
        "duration_expected": 30.0,
    },
]

# (texto falado, inicio, fim)
ROTEIRO = [
    ("bem vindos ao programa de hoje vamos falar sobre tecnologia e mercado", 0.0, 60.0),
    ("nosso convidado explica como as operadoras estao investindo em rede", 60.0, 120.0),
    # COM_000234 com variacao lexical proposital ("mais" em vez de "muito mais", "pra")
    ("na claro voce tem mais internet pra aproveitar tudo o que voce gosta", 120.0, 150.0),
    ("voltamos agora com a segunda parte da nossa conversa sobre conectividade", 150.0, 240.0),
    # COM_000236 com os termos presentes mas FORA DE ORDEM
    ("whatsapp foi citado antes internet depois e so no fim 50 giga com a claro", 240.0, 270.0),
    ("encerramos por aqui e ate a proxima semana com mais novidades", 270.0, 330.0),
]


def monta_palavras(roteiro: list[tuple[str, float, float]]) -> list[dict[str, object]]:
    """Distribui as palavras uniformemente no intervalo, como faria o WhisperX."""
    palavras: list[dict[str, object]] = []
    for texto, inicio, fim in roteiro:
        tokens = texto.split()
        passo = (fim - inicio) / max(len(tokens), 1)
        for i, token in enumerate(tokens):
            palavras.append(
                {"word": token, "start": inicio + i * passo, "end": inicio + (i + 1) * passo}
            )
    return palavras


async def main() -> None:
    reset_settings_cache()
    settings = get_settings()
    engine = build_engine(settings)
    await create_all(engine, vector_dim=settings.embedding.dimensions)
    session_factory = build_sessionmaker(engine)
    uow_factory = UnitOfWorkFactoryImpl(session_factory, vector_dim=settings.embedding.dimensions)
    embedder = HashingEmbedder(settings)

    from lukato.application.use_cases.adwatch import (
        CommercialInput,
        CreateCommercial,
        DetectCommercials,
        ImportScenes,
        ImportTranscript,
        MediaInput,
        RegisterMedia,
    )
    from lukato.domain.models.identity import Principal

    container = _container(settings, uow_factory, embedder)
    root = Principal.anonymous_root()

    print("=== 1. catalogo (CRUD) ===")
    criar = CreateCommercial(container)
    for item in CATALOGO:
        com = await criar.execute(CommercialInput(**item), root)
        print(f"  {com.commercial_id}  {com.campaign:<18} duracao={com.duration_expected:g}s")

    print("=== 2. midia + transcricao importada (sem FFmpeg, sem GPU) ===")
    midia = await RegisterMedia(container).execute(
        MediaInput(uri="file:///programa.mp4", title="Programa de TV"), root
    )
    palavras = TranscriptImporter.parse(monta_palavras(ROTEIRO))
    await ImportTranscript(container).execute(midia.id, palavras, root)
    print(f"  {len(palavras)} palavras, {palavras[-1].end:.0f}s de transcricao")

    print("=== 3. deteccao ===")
    relatorio = await DetectCommercials(container).execute(midia.id, principal=root)
    _mostra(relatorio)

    print("=== 4. com cortes de cena (refino de fronteira) ===")
    await ImportScenes(container).execute(
        midia.id,
        SceneImporter.parse(
            [
                {"index": 0, "start": 0.0, "end": 119.5},
                {"index": 1, "start": 119.5, "end": 150.5},
                {"index": 2, "start": 150.5, "end": 330.0},
            ]
        ),
        root,
    )
    relatorio2 = await DetectCommercials(container).execute(midia.id, principal=root)
    _mostra(relatorio2)

    print("=== 5. veredito ===")
    por_codigo = {d.commercial_code: d for d in relatorio2.detections}
    alvo = por_codigo.get("COM_000234")
    aceitos_ou_revisao = {c for c, d in por_codigo.items() if d.status.value != "rejected"}

    # O comercial presente DEVE aparecer como candidato com as bordas certas.
    ok_presente = alvo is not None
    erro = abs(alvo.start - 119.5) + abs(alvo.end - 150.5) if alvo else float("inf")

    # Sem OCR e sem juiz multimodal (ambos indisponiveis offline), a SPEC-0010 3.6
    # exige needs_review na faixa 0.60-0.90 — promover sem evidencia visual seria
    # o comportamento ERRADO.
    ok_classificacao = alvo is not None and alvo.status.value == "needs_review"
    ok_fora_de_ordem = "COM_000236" not in aceitos_ou_revisao
    ok_ausente = "COM_000235" not in aceitos_ou_revisao

    linhas = [
        ("comercial presente vira candidato", ok_presente),
        ("bordas encaixadas nos cortes de cena", erro < 0.01),
        ("classificado needs_review (sem OCR/VLM)", ok_classificacao),
        ("comercial fora de ordem descartado", ok_fora_de_ordem),
        ("comercial ausente do audio descartado", ok_ausente),
    ]
    for rotulo, ok in linhas:
        print(f"  {rotulo:.<44} {'OK' if ok else 'FALHOU'}")
    print(f"  erro de fronteira apos refino .............. {erro:.2f}s")
    if alvo is not None:
        ev = alvo.evidence
        print(
            f"\n  Decomposicao do score {alvo.confidence:.3f}:\n"
            f"    fala x0.40   = {ev.speech_match:.2f} -> {0.40 * ev.speech_match:.3f}\n"
            f"    semantico x0.25 = {ev.semantic_match:.2f} -> {0.25 * ev.semantic_match:.3f}\n"
            f"    ocr x0.15    = {ev.ocr_match:.2f} -> {0.15 * ev.ocr_match:.3f}   "
            f"(sem OCR offline: e isto que segura abaixo de 0.90)\n"
            f"    visual x0.15 = {ev.visual_match:.2f} -> {0.15 * ev.visual_match:.3f}\n"
            f"    duracao x0.05 = {ev.duration_match:.2f} -> {0.05 * ev.duration_match:.3f}"
        )
    print(
        "\n  Leitura: o funil textual localizou o comercial e cravou as bordas nos\n"
        "  cortes de cena. Sem OCR e sem juiz multimodal ele PARA em needs_review\n"
        "  em vez de afirmar — que e exatamente o que a SPEC-0010 3.6 manda fazer."
    )
    todas = all(ok for _, ok in linhas) and erro < 0.01
    print(f"\n  RESULTADO: {'todas as asercoes passaram' if todas else 'HA ASERCAO FALHANDO'}")

    await engine.dispose()


def _mostra(relatorio: object) -> None:
    for d in getattr(relatorio, "detections", []):
        ev = d.evidence
        print(
            f"  {d.commercial_code}  {d.start:7.1f}-{d.end:<7.1f} "
            f"conf={d.confidence:.3f}  {d.status.value:<12} "
            f"fala={ev.speech_match:.2f} sem={ev.semantic_match:.2f} "
            f"ordem={'sim' if ev.order_ok else 'nao'} cena={'sim' if d.refined_by_scene else 'nao'}"
        )
    if not getattr(relatorio, "detections", []):
        print("  (nenhuma deteccao)")


def _container(settings: object, uow_factory: object, embedder: object) -> object:
    """Container minimo: o AdWatch offline nao precisa de LLM nem de tracer real."""
    from lukato.adapters.guardrails.composite import build_default_evaluators
    from lukato.adapters.llm.echo import EchoLLM
    from lukato.adapters.media.factory import build_media_toolbox
    from lukato.adapters.observability.noop_tracer import NoopTracer
    from lukato.adapters.orchestrator.factory import build_orchestrators
    from lukato.adapters.orchestrator.tools import build_tool_registry
    from lukato.adapters.persistence.pgvector_store import PgVectorStore
    from lukato.adapters.security.hashing import BcryptHasher
    from lukato.adapters.security.tokens import JwtTokenService
    from lukato.application.container import Container
    from lukato.domain.services.cost_calculator import CostCalculator
    from lukato.domain.services.guardrail_engine import GuardrailEngine
    from lukato.domain.services.module_composer import ModuleComposer
    from lukato.modules.registry import registry

    llm = EchoLLM(settings)
    tools = build_tool_registry()
    registry.clear()
    registry.load_builtin()
    return Container(
        settings=settings,
        llm=llm,
        embeddings=embedder,
        vector_store=PgVectorStore(build_sessionmaker(build_engine(settings)), dimensions=1024),
        guardrails=GuardrailEngine(build_default_evaluators(llm=llm, settings=settings)),
        tracer=NoopTracer(),
        uow_factory=uow_factory,
        orchestrators=build_orchestrators(llm, settings=settings, tools=tools),
        registry=registry,
        cost_calculator=CostCalculator(),
        composer=ModuleComposer(
            default_model="echo", default_temperature=0.2, default_max_tokens=512
        ),
        hasher=BcryptHasher(rounds=4),
        tokens=JwtTokenService(settings),
        media=build_media_toolbox(settings, llm=llm),
        tools=tools,
    )


if __name__ == "__main__":
    # Banco descartavel: ver a nota em scripts/_prova_isolada.py.
    _tmp = isolar_banco()
    try:
        asyncio.run(main())
    finally:
        limpar_banco(_tmp)
