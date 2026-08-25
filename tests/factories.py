"""Construtores de dados de teste do lukato (SPEC-0000 secao 6).

Tudo aqui e **deterministico**: nenhum `utcnow()`, nenhum `uuid4()` solto. Os
carimbos saem de :data:`AGORA` (uma data fixa) e os identificadores de
:func:`id_de`, um UUIDv5 derivado da chave natural do objeto. Dois testes que
constroem `make_module(slug="assistente")` obtem exatamente o mesmo `id`, e a
mesma suite rodada amanha produz os mesmos bytes.

Uso tipico::

    from tests.factories import make_module, make_transcript, momento

    modulo = make_module(slug="assistente", runtime="direct")
    transcricao = make_transcript([("o melhor plano da claro", 10.0, 14.0)])
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionCandidate,
    DetectionEvidence,
    DetectionStatus,
    MediaAsset,
    MediaKind,
    OcrText,
    SceneCut,
    Transcript,
    TranscriptWord,
)
from lukato.domain.models.finops import Budget, BudgetPeriod, ModelPrice, UsageRecord
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailPolicy,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailSeverity,
    GuardrailStage,
)
from lukato.domain.models.identity import ApiKey, Permission, Principal, Role, User
from lukato.domain.models.knowledge import Chunk, Document
from lukato.domain.models.module import ModuleBinding, ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.models.prompt import PromptRole, PromptTemplate
from lukato.domain.models.run import AgentRun, RunStatus, RunStep, StepKind, TokenUsage
from lukato.domain.types import DEFAULT_TENANT, Id

__all__ = [
    "AGORA",
    "TESTE_NAMESPACE",
    "id_de",
    "make_api_key",
    "make_binding",
    "make_budget",
    "make_candidate",
    "make_chunk",
    "make_commercial",
    "make_detection",
    "make_document",
    "make_evidence",
    "make_fingerprint",
    "make_media",
    "make_module",
    "make_ocr",
    "make_policy",
    "make_price",
    "make_principal",
    "make_prompt",
    "make_rule",
    "make_run",
    "make_scenes",
    "make_step",
    "make_transcript",
    "make_usage_record",
    "make_user",
    "momento",
]

AGORA: Final[datetime] = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
"""Instante fixo de referencia da suite; substitui `utcnow()` nos dados de teste."""

TESTE_NAMESPACE: Final[uuid.UUID] = uuid.uuid5(uuid.NAMESPACE_URL, "https://lukato/tests")
"""Namespace dos identificadores deterministicos gerados por :func:`id_de`."""


def id_de(*partes: object) -> Id:
    """Identificador deterministico (UUIDv5) derivado das partes informadas.

    `id_de("modulo", "assistente")` devolve sempre o mesmo UUID, em qualquer
    processo e em qualquer ordem de execucao dos testes.
    """
    return str(uuid.uuid5(TESTE_NAMESPACE, "|".join(str(parte) for parte in partes)))


def momento(segundos: float = 0.0) -> datetime:
    """Instante `AGORA + segundos`, para ordenar registros sem tocar o relogio real."""
    return AGORA + timedelta(seconds=segundos)


# --------------------------------------------------------------------------- #
# Registry de modulos, prompts e guardrails
# --------------------------------------------------------------------------- #
def make_binding(
    *,
    input_guardrail_id: Id | None = None,
    system_prompt_id: Id | None = None,
    output_guardrail_id: Id | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float = 60.0,
    tools: Sequence[str] = (),
) -> ModuleBinding:
    """Monta a trinca `guardrail de entrada -> system prompt -> guardrail de saida`."""
    return ModuleBinding(
        input_guardrail_id=input_guardrail_id,
        system_prompt_id=system_prompt_id,
        output_guardrail_id=output_guardrail_id,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        tools=list(tools),
    )


def make_module(
    slug: str = "modulo-teste",
    *,
    name: str | None = None,
    description: str = "Definicao de building block usada nos testes.",
    kind: ModuleKind = ModuleKind.AGENT,
    status: ModuleStatus = ModuleStatus.ACTIVE,
    runtime: str = "direct",
    binding: ModuleBinding | None = None,
    config: dict[str, Any] | None = None,
    tags: Sequence[str] = (),
    owner: str | None = "testes",
    version: str = "1.0.0",
    module_id: Id | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ModuleDefinition:
    """Cria uma `ModuleDefinition` completa e valida (id derivado do slug)."""
    return ModuleDefinition(
        id=module_id or id_de("modulo", slug),
        slug=slug,
        name=name or slug.replace("-", " ").capitalize(),
        description=description,
        kind=kind,
        status=status,
        runtime=runtime,
        binding=binding if binding is not None else make_binding(),
        config=dict(config or {}),
        tags=list(tags),
        owner=owner,
        version=version,
        created_at=created_at or AGORA,
        updated_at=updated_at or AGORA,
    )


def make_prompt(
    slug: str = "prompt-teste",
    *,
    name: str | None = None,
    description: str = "System prompt usado nos testes.",
    template: str = "Voce e o assistente de testes do lukato.",
    role: PromptRole = PromptRole.SYSTEM,
    variables: Sequence[str] | None = None,
    version: int = 1,
    is_active: bool = True,
    labels: Sequence[str] = ("teste",),
    prompt_id: Id | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> PromptTemplate:
    """Cria um `PromptTemplate`; `variables` sai do proprio template quando omitido."""
    return PromptTemplate(
        id=prompt_id or id_de("prompt", slug, version),
        slug=slug,
        name=name or slug.replace("-", " ").capitalize(),
        description=description,
        role=role,
        template=template,
        variables=list(variables) if variables is not None else [],
        version=version,
        is_active=is_active,
        labels=list(labels),
        created_at=created_at or AGORA,
        updated_at=updated_at or AGORA,
    )


def make_rule(
    rule_id: str = "regra-teste",
    *,
    kind: GuardrailRuleKind = GuardrailRuleKind.KEYWORD_BLOCK,
    action: GuardrailAction = GuardrailAction.BLOCK,
    severity: GuardrailSeverity = GuardrailSeverity.HIGH,
    config: dict[str, Any] | None = None,
    message: str = "Conteudo barrado pela regra de teste.",
    enabled: bool = True,
    order: int = 0,
) -> GuardrailRule:
    """Cria uma `GuardrailRule`; o padrao bloqueia a palavra `proibido`."""
    if config is None:
        config = {"keywords": ["proibido"]} if kind is GuardrailRuleKind.KEYWORD_BLOCK else {}
    return GuardrailRule(
        id=rule_id,
        kind=kind,
        action=action,
        severity=severity,
        config=dict(config),
        message=message,
        enabled=enabled,
        order=order,
    )


def make_policy(
    slug: str = "politica-teste",
    *,
    name: str | None = None,
    description: str = "Politica de guardrail usada nos testes.",
    stage: GuardrailStage = GuardrailStage.INPUT,
    rules: Iterable[GuardrailRule] | None = None,
    fail_open: bool = False,
    is_active: bool = True,
    policy_id: Id | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> GuardrailPolicy:
    """Cria uma `GuardrailPolicy` (por padrao com uma regra de palavra proibida)."""
    return GuardrailPolicy(
        id=policy_id or id_de("politica", slug),
        slug=slug,
        name=name or slug.replace("-", " ").capitalize(),
        description=description,
        stage=stage,
        rules=list(rules) if rules is not None else [make_rule()],
        fail_open=fail_open,
        is_active=is_active,
        created_at=created_at or AGORA,
        updated_at=updated_at or AGORA,
    )


# --------------------------------------------------------------------------- #
# Execucoes e FinOps
# --------------------------------------------------------------------------- #
def make_step(
    run_id: Id,
    *,
    index: int = 0,
    kind: StepKind = StepKind.LLM,
    name: str = "chamada-llm",
    status: RunStatus = RunStatus.SUCCEEDED,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    error: str | None = None,
    step_id: Id | None = None,
) -> RunStep:
    """Cria um `RunStep` ligado a execucao informada."""
    return RunStep(
        id=step_id or id_de("passo", run_id, index),
        run_id=run_id,
        index=index,
        kind=kind,
        name=name,
        status=status,
        input=dict(input or {}),
        output=dict(output or {}),
        usage=usage if usage is not None else TokenUsage(),
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        error=error,
        started_at=AGORA,
        finished_at=AGORA,
    )


def make_run(
    *,
    module_slug: str = "modulo-teste",
    module_id: Id | None = None,
    status: RunStatus = RunStatus.SUCCEEDED,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    steps: Sequence[RunStep] | None = None,
    usage: TokenUsage | None = None,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    trace_id: str | None = None,
    error: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
    actor: str | None = "testes",
    run_id: Id | None = None,
    created_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> AgentRun:
    """Cria um `AgentRun`; `steps` ja chega com `run_id` correto quando omitido."""
    resolved_id = run_id or id_de("execucao", module_slug, status.value)
    return AgentRun(
        id=resolved_id,
        module_id=module_id or id_de("modulo", module_slug),
        module_slug=module_slug,
        status=status,
        input=dict(input or {"text": "ola"}),
        output=dict(output or {"text": "[echo] ola"}),
        steps=list(steps) if steps is not None else [],
        usage=usage if usage is not None else TokenUsage.of(10, 5),
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        trace_id=trace_id,
        error=error,
        tenant_id=tenant_id,
        actor=actor,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
        finished_at=finished_at,
    )


def make_usage_record(
    *,
    module_slug: str = "modulo-teste",
    model: str = "qwen-latest",
    usage: TokenUsage | None = None,
    cost_usd: float = 0.01,
    run_id: Id | None = None,
    tenant_id: str = DEFAULT_TENANT,
    occurred_at: datetime | None = None,
    record_id: Id | None = None,
) -> UsageRecord:
    """Cria um `UsageRecord` de consumo faturavel."""
    return UsageRecord(
        id=record_id or id_de("consumo", module_slug, model, cost_usd),
        run_id=run_id,
        module_slug=module_slug,
        model=model,
        usage=usage if usage is not None else TokenUsage.of(1000, 500),
        cost_usd=cost_usd,
        tenant_id=tenant_id,
        occurred_at=occurred_at or AGORA,
    )


def make_budget(
    name: str = "orcamento-teste",
    *,
    scope: str = "global",
    limit_usd: float = 10.0,
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    alert_threshold: float = 0.8,
    hard_stop: bool = False,
    is_active: bool = True,
    budget_id: Id | None = None,
    created_at: datetime | None = None,
) -> Budget:
    """Cria um `Budget` de controle de custo."""
    return Budget(
        id=budget_id or id_de("orcamento", name, scope),
        name=name,
        scope=scope,
        limit_usd=limit_usd,
        period=period,
        alert_threshold=alert_threshold,
        hard_stop=hard_stop,
        is_active=is_active,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def make_price(
    model: str = "qwen-latest",
    *,
    input_usd_per_1k: float = 0.5,
    output_usd_per_1k: float = 1.5,
    currency: str = "USD",
) -> ModelPrice:
    """Cria um `ModelPrice` com valores redondos, faceis de conferir na mao."""
    return ModelPrice(
        model=model,
        input_usd_per_1k=input_usd_per_1k,
        output_usd_per_1k=output_usd_per_1k,
        currency=currency,
    )


# --------------------------------------------------------------------------- #
# Conhecimento
# --------------------------------------------------------------------------- #
def make_document(
    title: str = "Documento de teste",
    *,
    collection: str = "agente_evidence",
    content: str = "O lukato e um ecossistema modular de agentes de IA.",
    source: str = "teste",
    metadata: dict[str, Any] | None = None,
    checksum: str = "",
    document_id: Id | None = None,
    created_at: datetime | None = None,
) -> Document:
    """Cria um `Document` da base de conhecimento."""
    return Document(
        id=document_id or id_de("documento", collection, title),
        collection=collection,
        title=title,
        source=source,
        content=content,
        metadata=dict(metadata or {}),
        checksum=checksum,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def make_chunk(
    document_id: Id,
    *,
    index: int = 0,
    collection: str = "agente_evidence",
    content: str = "trecho de teste",
    metadata: dict[str, Any] | None = None,
    embedding: Sequence[float] | None = None,
    token_count: int = 0,
    chunk_id: Id | None = None,
) -> Chunk:
    """Cria um `Chunk` ligado ao documento informado."""
    return Chunk(
        id=chunk_id or id_de("chunk", document_id, index),
        document_id=document_id,
        collection=collection,
        index=index,
        content=content,
        metadata=dict(metadata or {}),
        embedding=list(embedding) if embedding is not None else None,
        token_count=token_count,
    )


# --------------------------------------------------------------------------- #
# Identidade
# --------------------------------------------------------------------------- #
def make_user(
    email: str = "operador@lukato.local",
    *,
    name: str | None = None,
    role: Role = Role.OPERATOR,
    password_hash: str = "$2b$04$hash-de-teste-sem-valor-real",
    is_active: bool = True,
    tenant_id: str = DEFAULT_TENANT,
    user_id: Id | None = None,
    created_at: datetime | None = None,
) -> User:
    """Cria um `User` (o hash e um marcador textual, nao uma senha derivada)."""
    return User(
        id=user_id or id_de("usuario", email),
        email=email,
        name=name or email.split("@", 1)[0].capitalize(),
        role=role,
        password_hash=password_hash,
        is_active=is_active,
        tenant_id=tenant_id,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def make_api_key(
    name: str = "chave-teste",
    *,
    prefix: str = "lkt_teste",
    hashed_secret: str = "$2b$04$segredo-de-teste-sem-valor-real",
    role: Role = Role.OPERATOR,
    tenant_id: str = DEFAULT_TENANT,
    is_active: bool = True,
    expires_at: datetime | None = None,
    last_used_at: datetime | None = None,
    api_key_id: Id | None = None,
    created_at: datetime | None = None,
) -> ApiKey:
    """Cria uma `ApiKey` (somente prefixo e hash, nunca o segredo em claro)."""
    return ApiKey(
        id=api_key_id or id_de("api-key", prefix),
        name=name,
        prefix=prefix,
        hashed_secret=hashed_secret,
        role=role,
        tenant_id=tenant_id,
        is_active=is_active,
        expires_at=expires_at,
        last_used_at=last_used_at,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def make_principal(
    *,
    subject: str = "testes",
    role: Role = Role.ROOT,
    tenant_id: str = DEFAULT_TENANT,
    kind: str = "user",
    permissions: Iterable[Permission] | None = None,
) -> Principal:
    """Cria um `Principal`; sem `permissions` recebe todas as do papel `ROOT`."""
    if permissions is None:
        return Principal(
            subject=subject,
            role=role,
            tenant_id=tenant_id,
            kind=kind,
            permissions=frozenset(Permission),
        )
    return Principal(
        subject=subject,
        role=role,
        tenant_id=tenant_id,
        kind=kind,
        permissions=frozenset(permissions),
    )


# --------------------------------------------------------------------------- #
# AdWatch
# --------------------------------------------------------------------------- #
def make_commercial(
    code: str = "COM_000001",
    *,
    campaign: str = "Campanha de teste",
    brand: str = "Claro",
    text: str = "o melhor plano da claro com internet ilimitada",
    duration_expected: float = 30.0,
    keywords: Sequence[str] = ("claro", "plano"),
    key_phrases: Sequence[str] = ("melhor plano da claro",),
    language: str = "pt-BR",
    is_active: bool = True,
    metadata: dict[str, Any] | None = None,
    commercial_id: Id | None = None,
    created_at: datetime | None = None,
) -> Commercial:
    """Cria um `Commercial` do catalogo; `code` e o codigo de negocio unico."""
    return Commercial(
        id=commercial_id or id_de("comercial", code),
        commercial_id=code,
        campaign=campaign,
        brand=brand,
        text=text,
        duration_expected=duration_expected,
        keywords=list(keywords),
        key_phrases=list(key_phrases),
        language=language,
        is_active=is_active,
        metadata=dict(metadata or {}),
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def make_fingerprint(
    commercial_id: Id,
    *,
    normalized_text: str = "o melhor plano da claro com internet ilimitada",
    token_set: Sequence[str] | None = None,
    keywords: Sequence[str] = ("claro", "plano"),
    key_phrases: Sequence[str] = ("melhor plano da claro",),
    embedding: Sequence[float] | None = None,
    duration: float = 30.0,
    expected_brand: str = "Claro",
    fingerprint_id: Id | None = None,
) -> AdFingerprint:
    """Cria uma `AdFingerprint`; `token_set` sai do texto normalizado quando omitido."""
    return AdFingerprint(
        id=fingerprint_id or id_de("assinatura", commercial_id),
        commercial_id=commercial_id,
        normalized_text=normalized_text,
        token_set=list(token_set) if token_set is not None else normalized_text.split(),
        keywords=list(keywords),
        key_phrases=list(key_phrases),
        embedding=list(embedding) if embedding is not None else None,
        duration=duration,
        expected_brand=expected_brand,
    )


def make_media(
    uri: str = "file:///midia/programa-teste.mp4",
    *,
    kind: MediaKind = MediaKind.VIDEO,
    duration_seconds: float = 120.0,
    fps: float = 25.0,
    title: str = "Programa de teste",
    status: str = "registered",
    metadata: dict[str, Any] | None = None,
    media_id: Id | None = None,
    created_at: datetime | None = None,
) -> MediaAsset:
    """Cria um `MediaAsset` registrado (sem tocar em arquivo nenhum)."""
    return MediaAsset(
        id=media_id or id_de("midia", uri),
        uri=uri,
        kind=kind,
        duration_seconds=duration_seconds,
        fps=fps,
        title=title,
        status=status,
        metadata=dict(metadata or {}),
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def _palavras_do_trecho(texto: str, inicio: float, fim: float) -> list[TranscriptWord]:
    """Distribui as palavras de `texto` uniformemente dentro de `[inicio, fim]`."""
    palavras = texto.split()
    if not palavras:
        return []
    duracao = max(float(fim) - float(inicio), 0.0)
    passo = duracao / len(palavras)
    resultado: list[TranscriptWord] = []
    for posicao, palavra in enumerate(palavras):
        comeco = float(inicio) + posicao * passo
        resultado.append(
            TranscriptWord(
                word=palavra,
                start=round(comeco, 6),
                end=round(comeco + passo, 6),
            )
        )
    return resultado


def make_transcript(
    words_spec: Sequence[tuple[str, float, float]],
    *,
    media_id: Id | None = None,
    language: str = "pt",
    source: str = "import",
    transcript_id: Id | None = None,
    created_at: datetime | None = None,
) -> Transcript:
    """Cria uma `Transcript` a partir de trechos `("texto", inicio, fim)`.

    Cada trecho e quebrado em palavras e os carimbos sao **distribuidos
    uniformemente** no intervalo: `("a b", 10.0, 12.0)` produz `a[10.0,11.0]` e
    `b[11.0,12.0]`. E dessa regularidade que dependem os testes de janelamento e
    de refinamento de fronteira do AdWatch (SPEC-0010).

    Trecho sem palavras e ignorado; trecho com `fim <= inicio` gera palavras de
    duracao zero ancoradas em `inicio`.
    """
    resolved_media = media_id or id_de("midia", "file:///midia/programa-teste.mp4")
    palavras: list[TranscriptWord] = []
    for texto, inicio, fim in words_spec:
        palavras.extend(_palavras_do_trecho(texto, inicio, fim))
    return Transcript(
        id=transcript_id or id_de("transcricao", resolved_media),
        media_id=resolved_media,
        language=language,
        words=palavras,
        source=source,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )


def make_scenes(bounds: Sequence[tuple[float, float]], *, kind: str = "cut") -> list[SceneCut]:
    """Cria a lista de `SceneCut` a partir dos pares `(inicio, fim)`, ja indexada."""
    return [
        SceneCut(index=posicao, start=float(inicio), end=float(fim), kind=kind)
        for posicao, (inicio, fim) in enumerate(bounds)
    ]


def make_ocr(
    spec: Sequence[tuple[str, float, float]], *, confidence: float = 0.9
) -> list[OcrText]:
    """Cria a lista de `OcrText` a partir de trechos `("texto", inicio, fim)`."""
    return [
        OcrText(text=texto, start=float(inicio), end=float(fim), confidence=confidence)
        for texto, inicio, fim in spec
    ]


def make_evidence(
    *,
    speech_match: float = 0.0,
    semantic_match: float = 0.0,
    ocr_match: float = 0.0,
    visual_match: float = 0.0,
    duration_match: float = 0.0,
    order_ok: bool = True,
    brand_detected: str | None = None,
    matched_text: str = "",
) -> DetectionEvidence:
    """Cria a `DetectionEvidence` por modalidade (todos os sinais zerados por padrao)."""
    return DetectionEvidence(
        speech_match=speech_match,
        semantic_match=semantic_match,
        ocr_match=ocr_match,
        visual_match=visual_match,
        duration_match=duration_match,
        order_ok=order_ok,
        brand_detected=brand_detected,
        matched_text=matched_text,
    )


def make_candidate(
    *,
    commercial_id: Id | None = None,
    commercial_code: str = "COM_000001",
    campaign: str = "Campanha de teste",
    start: float = 10.0,
    end: float = 40.0,
    score: float = 0.95,
    evidence: DetectionEvidence | None = None,
) -> DetectionCandidate:
    """Cria um `DetectionCandidate` (janela antes da decisao final)."""
    return DetectionCandidate(
        commercial_id=commercial_id or id_de("comercial", commercial_code),
        commercial_code=commercial_code,
        campaign=campaign,
        start=start,
        end=end,
        score=score,
        evidence=evidence if evidence is not None else make_evidence(),
    )


def make_detection(
    *,
    media_id: Id | None = None,
    commercial_id: Id | None = None,
    commercial_code: str = "COM_000001",
    campaign: str = "Campanha de teste",
    brand: str = "Claro",
    start: float = 10.0,
    end: float = 40.0,
    confidence: float = 0.95,
    status: DetectionStatus = DetectionStatus.ACCEPTED,
    evidence: DetectionEvidence | None = None,
    refined_by_scene: bool = False,
    verified_by_vlm: bool = False,
    detection_id: Id | None = None,
    created_at: datetime | None = None,
) -> Detection:
    """Cria uma `Detection` consolidada dentro de um ativo de midia."""
    resolved_media = media_id or id_de("midia", "file:///midia/programa-teste.mp4")
    return Detection(
        id=detection_id or id_de("deteccao", resolved_media, commercial_code, start),
        media_id=resolved_media,
        commercial_id=commercial_id or id_de("comercial", commercial_code),
        commercial_code=commercial_code,
        campaign=campaign,
        brand=brand,
        start=start,
        end=end,
        confidence=confidence,
        status=status,
        evidence=evidence if evidence is not None else make_evidence(),
        refined_by_scene=refined_by_scene,
        verified_by_vlm=verified_by_vlm,
        created_at=created_at or AGORA,
        updated_at=created_at or AGORA,
    )
