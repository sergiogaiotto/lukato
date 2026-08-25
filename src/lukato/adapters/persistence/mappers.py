"""Conversao pura entre linhas ORM e modelos de dominio (SPEC-0011 §3.8).

Para cada agregado existe um par de funcoes:

* ``<agregado>_to_domain(row)`` reconstroi o modelo de dominio — **nunca** devolve
  objeto ORM, de modo que nenhum repositorio vaze a camada de persistencia;
* ``<agregado>_apply(row, entity)`` copia os campos do modelo para a linha, preserva
  o `id` ja atribuido, mantem o `created_at` original e renova o `updated_at`
  (na linha e no proprio modelo, para que o objeto devolvido pelo repositorio fique
  coerente com o que foi gravado).

Nenhuma funcao deste modulo toca a sessao: sao transformacoes puras, testaveis sem
banco. Colisoes de nome ja resolvidas no ORM continuam valendo aqui —
`meta` <-> coluna `metadata`, `position` <-> `index` do dominio e
`start_seconds`/`end_seconds` <-> `start`/`end`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from lukato.adapters.persistence.orm import (
    AdFingerprintRow,
    AgentRunRow,
    ApiKeyRow,
    BudgetRow,
    ChunkRow,
    CommercialRow,
    DetectionRow,
    DocumentRow,
    GuardrailPolicyRow,
    MediaAssetRow,
    ModuleRow,
    OcrTextRow,
    PromptRow,
    RunStepRow,
    SceneCutRow,
    TranscriptRow,
    UsageRecordRow,
    UserRow,
)
from lukato.domain.errors import ProviderError
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionEvidence,
    DetectionStatus,
    MediaAsset,
    MediaKind,
    OcrText,
    SceneCut,
    Transcript,
    TranscriptWord,
)
from lukato.domain.models.finops import Budget, BudgetPeriod, UsageRecord
from lukato.domain.models.guardrail import GuardrailPolicy, GuardrailRule, GuardrailStage
from lukato.domain.models.identity import ApiKey, Role, User
from lukato.domain.models.knowledge import Chunk, Document
from lukato.domain.models.module import ModuleBinding, ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.models.prompt import PromptRole, PromptTemplate
from lukato.domain.models.run import AgentRun, RunStatus, RunStep, StepKind, TokenUsage
from lukato.domain.types import Id, Json, utcnow

__all__ = [
    "api_key_apply",
    "api_key_to_domain",
    "budget_apply",
    "budget_to_domain",
    "chunk_apply",
    "chunk_to_domain",
    "commercial_apply",
    "commercial_to_domain",
    "detection_apply",
    "detection_to_domain",
    "document_apply",
    "document_to_domain",
    "fingerprint_apply",
    "fingerprint_to_domain",
    "guardrail_apply",
    "guardrail_to_domain",
    "media_apply",
    "media_to_domain",
    "module_apply",
    "module_to_domain",
    "ocr_apply",
    "ocr_to_domain",
    "prompt_apply",
    "prompt_to_domain",
    "run_apply",
    "run_step_apply",
    "run_step_to_domain",
    "run_to_domain",
    "scene_apply",
    "scene_to_domain",
    "transcript_apply",
    "transcript_to_domain",
    "usage_apply",
    "usage_to_domain",
    "user_apply",
    "user_to_domain",
]

_EnumT = TypeVar("_EnumT", bound=StrEnum)
_ModelT = TypeVar("_ModelT", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Helpers puros
# --------------------------------------------------------------------------- #


def _json_dict(value: Any) -> Json:
    """Normaliza um campo JSON de objeto: `None` e valores invalidos viram `{}`."""
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    """Normaliza um campo JSON de lista: `None` e valores invalidos viram `[]`."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _str_list(value: Any) -> list[str]:
    """Normaliza um campo JSON de lista de textos."""
    return [str(item) for item in _json_list(value)]


def _floats(value: Any) -> list[float] | None:
    """Normaliza um embedding lido do banco para `list[float]` (ou `None`)."""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def _aware(value: datetime | None) -> datetime:
    """Garante `datetime` timezone-aware em UTC (SQLite devolve valores ingenuos)."""
    if value is None:
        return utcnow()
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _aware_optional(value: datetime | None) -> datetime | None:
    """Versao anulavel de :func:`_aware`: preserva `None`."""
    return None if value is None else _aware(value)


def _enum(enum_cls: type[_EnumT], value: Any, *, field: str) -> _EnumT:
    """Converte o texto persistido no `StrEnum` do dominio."""
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ProviderError(
            f"valor invalido para {field}: {value!r}",
            details={"field": field, "value": str(value), "enum": enum_cls.__name__},
        ) from exc


def _submodel(model_cls: type[_ModelT], data: Any) -> _ModelT:
    """Reconstroi um sub-modelo a partir do JSON, ignorando chaves desconhecidas."""
    payload = {key: value for key, value in _json_dict(data).items() if key in model_cls.model_fields}
    try:
        return model_cls.model_validate(payload)
    except PydanticValidationError as exc:
        raise ProviderError(
            f"conteudo JSON incompativel com {model_cls.__name__}",
            details={"model": model_cls.__name__, "errors": exc.error_count()},
        ) from exc


def _submodels(model_cls: type[_ModelT], data: Any) -> list[_ModelT]:
    """Reconstroi uma lista de sub-modelos a partir de um campo JSON de lista."""
    return [_submodel(model_cls, item) for item in _json_list(data)]


def _dump(model: BaseModel) -> Json:
    """Serializa um sub-modelo em JSON puro (datas viram texto ISO)."""
    return model.model_dump(mode="json")


def _dump_all(models: Iterable[BaseModel]) -> list[Json]:
    """Serializa uma colecao de sub-modelos em JSON puro."""
    return [_dump(model) for model in models]


def _usage(row: Any) -> TokenUsage:
    """Remonta o `TokenUsage` a partir das tres colunas de contagem."""
    return TokenUsage(
        prompt_tokens=int(row.prompt_tokens or 0),
        completion_tokens=int(row.completion_tokens or 0),
        total_tokens=int(row.total_tokens or 0),
    )


def _apply_usage(row: Any, usage: TokenUsage) -> None:
    """Espalha o `TokenUsage` nas tres colunas de contagem."""
    row.prompt_tokens = usage.prompt_tokens
    row.completion_tokens = usage.completion_tokens
    row.total_tokens = usage.total_tokens


def _assign_id(row: Any, entity_id: Id) -> None:
    """Atribui o `id` do modelo apenas quando a linha ainda nao tem chave primaria."""
    if not getattr(row, "id", None):
        row.id = entity_id


def _stamp(row: Any, entity: Any) -> None:
    """Preserva `created_at` e renova `updated_at` na linha e no modelo."""
    if hasattr(row, "created_at"):
        created = getattr(entity, "created_at", None)
        if getattr(row, "created_at", None) is None and created is not None:
            row.created_at = created
    if hasattr(row, "updated_at"):
        now = utcnow()
        row.updated_at = now
        if hasattr(entity, "updated_at"):
            entity.updated_at = now


# --------------------------------------------------------------------------- #
# Registry de modulos
# --------------------------------------------------------------------------- #


def module_to_domain(row: ModuleRow) -> ModuleDefinition:
    """Reconstroi a `ModuleDefinition` a partir da linha de `modules`."""
    return ModuleDefinition(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        kind=_enum(ModuleKind, row.kind, field="modules.kind"),
        status=_enum(ModuleStatus, row.status, field="modules.status"),
        runtime=row.runtime,
        binding=_submodel(ModuleBinding, row.binding),
        config=_json_dict(row.config),
        tags=_str_list(row.tags),
        owner=row.owner,
        version=row.version,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def module_apply(row: ModuleRow, entity: ModuleDefinition) -> None:
    """Copia a `ModuleDefinition` para a linha de `modules`."""
    _assign_id(row, entity.id)
    row.slug = entity.slug
    row.name = entity.name
    row.description = entity.description
    row.kind = entity.kind.value
    row.status = entity.status.value
    row.runtime = entity.runtime
    row.binding = _dump(entity.binding)
    row.config = dict(entity.config)
    row.tags = list(entity.tags)
    row.owner = entity.owner
    row.version = entity.version
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def prompt_to_domain(row: PromptRow) -> PromptTemplate:
    """Reconstroi o `PromptTemplate` a partir da linha de `prompts`."""
    return PromptTemplate(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        role=_enum(PromptRole, row.role, field="prompts.role"),
        template=row.template,
        variables=_str_list(row.variables),
        version=int(row.version),
        is_active=bool(row.is_active),
        labels=_str_list(row.labels),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def prompt_apply(row: PromptRow, entity: PromptTemplate) -> None:
    """Copia o `PromptTemplate` para a linha de `prompts`."""
    _assign_id(row, entity.id)
    row.slug = entity.slug
    row.name = entity.name
    row.description = entity.description
    row.role = entity.role.value
    row.template = entity.template
    row.variables = list(entity.variables)
    row.version = entity.version
    row.is_active = entity.is_active
    row.labels = list(entity.labels)
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #


def guardrail_to_domain(row: GuardrailPolicyRow) -> GuardrailPolicy:
    """Reconstroi a `GuardrailPolicy` a partir da linha de `guardrail_policies`."""
    return GuardrailPolicy(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        stage=_enum(GuardrailStage, row.stage, field="guardrail_policies.stage"),
        rules=_submodels(GuardrailRule, row.rules),
        fail_open=bool(row.fail_open),
        is_active=bool(row.is_active),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def guardrail_apply(row: GuardrailPolicyRow, entity: GuardrailPolicy) -> None:
    """Copia a `GuardrailPolicy` para a linha de `guardrail_policies`."""
    _assign_id(row, entity.id)
    row.slug = entity.slug
    row.name = entity.name
    row.description = entity.description
    row.stage = entity.stage.value
    row.rules = _dump_all(entity.rules)
    row.fail_open = entity.fail_open
    row.is_active = entity.is_active
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# Execucoes
# --------------------------------------------------------------------------- #


def run_to_domain(row: AgentRunRow, *, steps: Sequence[RunStepRow] | None = None) -> AgentRun:
    """Reconstroi o `AgentRun`; `steps` deve vir carregado pelo repositorio.

    Os passos nao sao lidos da relacao `row.steps` para nao disparar lazy loading
    dentro de um contexto assincrono: quem chama decide se ja os carregou.
    """
    return AgentRun(
        id=row.id,
        module_id=row.module_id,
        module_slug=row.module_slug,
        status=_enum(RunStatus, row.status, field="agent_runs.status"),
        input=_json_dict(row.input),
        output=_json_dict(row.output),
        steps=[run_step_to_domain(step) for step in (steps or ())],
        usage=_usage(row),
        cost_usd=float(row.cost_usd or 0.0),
        latency_ms=float(row.latency_ms or 0.0),
        trace_id=row.trace_id,
        error=row.error,
        tenant_id=row.tenant_id,
        actor=row.actor,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        finished_at=_aware_optional(row.finished_at),
    )


def run_apply(row: AgentRunRow, entity: AgentRun) -> None:
    """Copia o `AgentRun` para a linha de `agent_runs` (os passos vao em `run_steps`)."""
    _assign_id(row, entity.id)
    row.module_id = entity.module_id
    row.module_slug = entity.module_slug
    row.status = entity.status.value
    row.input = dict(entity.input)
    row.output = dict(entity.output)
    _apply_usage(row, entity.usage)
    row.cost_usd = entity.cost_usd
    row.latency_ms = entity.latency_ms
    row.trace_id = entity.trace_id
    row.error = entity.error
    row.tenant_id = entity.tenant_id
    row.actor = entity.actor
    row.finished_at = entity.finished_at
    _stamp(row, entity)


def run_step_to_domain(row: RunStepRow) -> RunStep:
    """Reconstroi o `RunStep` (`position` da linha volta como `index` do dominio)."""
    return RunStep(
        id=row.id,
        run_id=row.run_id,
        index=int(row.position),
        kind=_enum(StepKind, row.kind, field="run_steps.kind"),
        name=row.name,
        status=_enum(RunStatus, row.status, field="run_steps.status"),
        input=_json_dict(row.input),
        output=_json_dict(row.output),
        usage=_usage(row),
        cost_usd=float(row.cost_usd or 0.0),
        latency_ms=float(row.latency_ms or 0.0),
        error=row.error,
        started_at=_aware_optional(row.started_at),
        finished_at=_aware_optional(row.finished_at),
    )


def run_step_apply(row: RunStepRow, entity: RunStep) -> None:
    """Copia o `RunStep` para a linha de `run_steps` (`index` vira `position`)."""
    _assign_id(row, entity.id)
    row.run_id = entity.run_id
    row.position = entity.index
    row.kind = entity.kind.value
    row.name = entity.name
    row.status = entity.status.value
    row.input = dict(entity.input)
    row.output = dict(entity.output)
    _apply_usage(row, entity.usage)
    row.cost_usd = entity.cost_usd
    row.latency_ms = entity.latency_ms
    row.error = entity.error
    row.started_at = entity.started_at
    row.finished_at = entity.finished_at
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# FinOps
# --------------------------------------------------------------------------- #


def usage_to_domain(row: UsageRecordRow) -> UsageRecord:
    """Reconstroi o `UsageRecord` a partir da linha de `usage_records`."""
    return UsageRecord(
        id=row.id,
        run_id=row.run_id,
        module_slug=row.module_slug,
        model=row.model,
        usage=_usage(row),
        cost_usd=float(row.cost_usd or 0.0),
        tenant_id=row.tenant_id,
        occurred_at=_aware(row.occurred_at),
    )


def usage_apply(row: UsageRecordRow, entity: UsageRecord) -> None:
    """Copia o `UsageRecord` para a linha de `usage_records`."""
    _assign_id(row, entity.id)
    row.run_id = entity.run_id
    row.module_slug = entity.module_slug
    row.model = entity.model
    _apply_usage(row, entity.usage)
    row.cost_usd = entity.cost_usd
    row.tenant_id = entity.tenant_id
    row.occurred_at = entity.occurred_at
    _stamp(row, entity)


def budget_to_domain(row: BudgetRow) -> Budget:
    """Reconstroi o `Budget` a partir da linha de `budgets`."""
    return Budget(
        id=row.id,
        name=row.name,
        scope=row.scope,
        limit_usd=float(row.limit_usd or 0.0),
        period=_enum(BudgetPeriod, row.period, field="budgets.period"),
        alert_threshold=float(row.alert_threshold),
        hard_stop=bool(row.hard_stop),
        is_active=bool(row.is_active),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def budget_apply(row: BudgetRow, entity: Budget) -> None:
    """Copia o `Budget` para a linha de `budgets`."""
    _assign_id(row, entity.id)
    row.name = entity.name
    row.scope = entity.scope
    row.limit_usd = entity.limit_usd
    row.period = entity.period.value
    row.alert_threshold = entity.alert_threshold
    row.hard_stop = entity.hard_stop
    row.is_active = entity.is_active
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# Conhecimento
# --------------------------------------------------------------------------- #


def document_to_domain(row: DocumentRow) -> Document:
    """Reconstroi o `Document` (coluna `metadata` volta pelo atributo `meta`)."""
    return Document(
        id=row.id,
        collection=row.collection,
        title=row.title,
        source=row.source or "",
        content=row.content,
        metadata=_json_dict(row.meta),
        checksum=row.checksum or "",
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def document_apply(row: DocumentRow, entity: Document) -> None:
    """Copia o `Document` para a linha de `documents`."""
    _assign_id(row, entity.id)
    row.collection = entity.collection
    row.title = entity.title
    row.source = entity.source
    row.content = entity.content
    row.meta = dict(entity.metadata)
    row.checksum = entity.checksum
    _stamp(row, entity)


def chunk_to_domain(row: ChunkRow) -> Chunk:
    """Reconstroi o `Chunk` (`position` da linha volta como `index` do dominio)."""
    return Chunk(
        id=row.id,
        document_id=row.document_id,
        collection=row.collection,
        index=int(row.position),
        content=row.content,
        metadata=_json_dict(row.meta),
        embedding=_floats(row.embedding),
        token_count=int(row.token_count or 0),
    )


def chunk_apply(row: ChunkRow, entity: Chunk) -> None:
    """Copia o `Chunk` para a linha de `chunks` (`index` vira `position`)."""
    _assign_id(row, entity.id)
    row.document_id = entity.document_id
    row.collection = entity.collection
    row.position = entity.index
    row.content = entity.content
    row.meta = dict(entity.metadata)
    row.embedding = list(entity.embedding) if entity.embedding is not None else None
    row.token_count = entity.token_count
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# Identidade e acesso
# --------------------------------------------------------------------------- #


def user_to_domain(row: UserRow) -> User:
    """Reconstroi o `User` a partir da linha de `users`."""
    return User(
        id=row.id,
        email=row.email,
        name=row.name,
        role=_enum(Role, row.role, field="users.role"),
        password_hash=row.password_hash or "",
        is_active=bool(row.is_active),
        tenant_id=row.tenant_id,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def user_apply(row: UserRow, entity: User) -> None:
    """Copia o `User` para a linha de `users`."""
    _assign_id(row, entity.id)
    row.email = entity.email
    row.name = entity.name
    row.role = entity.role.value
    row.password_hash = entity.password_hash
    row.is_active = entity.is_active
    row.tenant_id = entity.tenant_id
    _stamp(row, entity)


def api_key_to_domain(row: ApiKeyRow) -> ApiKey:
    """Reconstroi a `ApiKey` a partir da linha de `api_keys`."""
    return ApiKey(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        hashed_secret=row.hashed_secret,
        role=_enum(Role, row.role, field="api_keys.role"),
        tenant_id=row.tenant_id,
        is_active=bool(row.is_active),
        expires_at=_aware_optional(row.expires_at),
        last_used_at=_aware_optional(row.last_used_at),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def api_key_apply(row: ApiKeyRow, entity: ApiKey) -> None:
    """Copia a `ApiKey` para a linha de `api_keys`."""
    _assign_id(row, entity.id)
    row.name = entity.name
    row.prefix = entity.prefix
    row.hashed_secret = entity.hashed_secret
    row.role = entity.role.value
    row.tenant_id = entity.tenant_id
    row.is_active = entity.is_active
    row.expires_at = entity.expires_at
    row.last_used_at = entity.last_used_at
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# AdWatch — catalogo
# --------------------------------------------------------------------------- #


def commercial_to_domain(row: CommercialRow) -> Commercial:
    """Reconstroi o `Commercial` (coluna `metadata` volta pelo atributo `meta`)."""
    return Commercial(
        id=row.id,
        commercial_id=row.commercial_id,
        campaign=row.campaign,
        brand=row.brand,
        text=row.text or "",
        duration_expected=float(row.duration_expected or 0.0),
        keywords=_str_list(row.keywords),
        key_phrases=_str_list(row.key_phrases),
        language=row.language,
        is_active=bool(row.is_active),
        metadata=_json_dict(row.meta),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def commercial_apply(row: CommercialRow, entity: Commercial) -> None:
    """Copia o `Commercial` para a linha de `commercials`."""
    _assign_id(row, entity.id)
    row.commercial_id = entity.commercial_id
    row.campaign = entity.campaign
    row.brand = entity.brand
    row.text = entity.text
    row.duration_expected = entity.duration_expected
    row.keywords = list(entity.keywords)
    row.key_phrases = list(entity.key_phrases)
    row.language = entity.language
    row.is_active = entity.is_active
    row.meta = dict(entity.metadata)
    _stamp(row, entity)


def fingerprint_to_domain(row: AdFingerprintRow) -> AdFingerprint:
    """Reconstroi o `AdFingerprint` a partir da linha de `ad_fingerprints`."""
    return AdFingerprint(
        id=row.id,
        commercial_id=row.commercial_id,
        normalized_text=row.normalized_text or "",
        token_set=_str_list(row.token_set),
        keywords=_str_list(row.keywords),
        key_phrases=_str_list(row.key_phrases),
        embedding=_floats(row.embedding),
        duration=float(row.duration or 0.0),
        expected_brand=row.expected_brand or "",
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def fingerprint_apply(row: AdFingerprintRow, entity: AdFingerprint) -> None:
    """Copia o `AdFingerprint` para a linha de `ad_fingerprints`."""
    _assign_id(row, entity.id)
    row.commercial_id = entity.commercial_id
    row.normalized_text = entity.normalized_text
    row.token_set = list(entity.token_set)
    row.keywords = list(entity.keywords)
    row.key_phrases = list(entity.key_phrases)
    row.embedding = list(entity.embedding) if entity.embedding is not None else None
    row.duration = entity.duration
    row.expected_brand = entity.expected_brand
    _stamp(row, entity)


# --------------------------------------------------------------------------- #
# AdWatch — midia e artefatos derivados
# --------------------------------------------------------------------------- #


def media_to_domain(row: MediaAssetRow) -> MediaAsset:
    """Reconstroi o `MediaAsset` (coluna `metadata` volta pelo atributo `meta`)."""
    return MediaAsset(
        id=row.id,
        uri=row.uri,
        kind=_enum(MediaKind, row.kind, field="media_assets.kind"),
        duration_seconds=float(row.duration_seconds or 0.0),
        fps=float(row.fps or 0.0),
        title=row.title or "",
        status=row.status,
        metadata=_json_dict(row.meta),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def media_apply(row: MediaAssetRow, entity: MediaAsset) -> None:
    """Copia o `MediaAsset` para a linha de `media_assets`."""
    _assign_id(row, entity.id)
    row.uri = entity.uri
    row.kind = entity.kind.value
    row.duration_seconds = entity.duration_seconds
    row.fps = entity.fps
    row.title = entity.title
    row.status = entity.status
    row.meta = dict(entity.metadata)
    _stamp(row, entity)


def transcript_to_domain(row: TranscriptRow) -> Transcript:
    """Reconstroi o `Transcript` a partir da linha de `transcripts`."""
    return Transcript(
        id=row.id,
        media_id=row.media_id,
        language=row.language,
        words=_submodels(TranscriptWord, row.words),
        source=row.source,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def transcript_apply(row: TranscriptRow, entity: Transcript) -> None:
    """Copia o `Transcript` para a linha de `transcripts`."""
    _assign_id(row, entity.id)
    row.media_id = entity.media_id
    row.language = entity.language
    row.words = _dump_all(entity.words)
    row.source = entity.source
    _stamp(row, entity)


def scene_to_domain(row: SceneCutRow) -> SceneCut:
    """Reconstroi o `SceneCut` (`position`/`start_seconds`/`end_seconds` -> `index`/`start`/`end`)."""
    return SceneCut(
        index=int(row.position),
        start=float(row.start_seconds or 0.0),
        end=float(row.end_seconds or 0.0),
        kind=row.kind,
    )


def scene_apply(row: SceneCutRow, entity: SceneCut, *, media_id: Id | None = None) -> None:
    """Copia o `SceneCut` para a linha de `scene_cuts`.

    `SceneCut` nao carrega `media_id` nem `id`: o repositorio informa o ativo via
    `media_id` e a chave primaria e atribuida pelo chamador quando ainda nao existe.
    """
    if media_id is not None:
        row.media_id = media_id
    row.position = entity.index
    row.start_seconds = entity.start
    row.end_seconds = entity.end
    row.kind = entity.kind


def ocr_to_domain(row: OcrTextRow) -> OcrText:
    """Reconstroi o `OcrText` (`start_seconds`/`end_seconds` -> `start`/`end`)."""
    bbox = _json_list(row.bbox) if row.bbox is not None else None
    return OcrText(
        text=row.text or "",
        start=float(row.start_seconds or 0.0),
        end=float(row.end_seconds or 0.0),
        confidence=float(row.confidence),
        bbox=tuple(int(value) for value in bbox) if bbox else None,  # type: ignore[arg-type]
    )


def ocr_apply(row: OcrTextRow, entity: OcrText, *, media_id: Id | None = None) -> None:
    """Copia o `OcrText` para a linha de `ocr_texts`.

    `OcrText` nao carrega `media_id` nem `id`: o repositorio informa o ativo via
    `media_id` e a chave primaria e atribuida pelo chamador quando ainda nao existe.
    """
    if media_id is not None:
        row.media_id = media_id
    row.text = entity.text
    row.start_seconds = entity.start
    row.end_seconds = entity.end
    row.confidence = entity.confidence
    row.bbox = list(entity.bbox) if entity.bbox is not None else None


def detection_to_domain(row: DetectionRow) -> Detection:
    """Reconstroi a `Detection` (`start_seconds`/`end_seconds` -> `start`/`end`)."""
    return Detection(
        id=row.id,
        media_id=row.media_id,
        commercial_id=row.commercial_id,
        commercial_code=row.commercial_code or "",
        campaign=row.campaign or "",
        brand=row.brand or "",
        start=float(row.start_seconds or 0.0),
        end=float(row.end_seconds or 0.0),
        confidence=float(row.confidence or 0.0),
        status=_enum(DetectionStatus, row.status, field="detections.status"),
        evidence=_submodel(DetectionEvidence, row.evidence),
        refined_by_scene=bool(row.refined_by_scene),
        verified_by_vlm=bool(row.verified_by_vlm),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def detection_apply(row: DetectionRow, entity: Detection) -> None:
    """Copia a `Detection` para a linha de `detections`."""
    _assign_id(row, entity.id)
    row.media_id = entity.media_id
    row.commercial_id = entity.commercial_id
    row.commercial_code = entity.commercial_code
    row.campaign = entity.campaign
    row.brand = entity.brand
    row.start_seconds = entity.start
    row.end_seconds = entity.end
    row.confidence = entity.confidence
    row.status = entity.status.value
    row.evidence = _dump(entity.evidence)
    row.refined_by_scene = entity.refined_by_scene
    row.verified_by_vlm = entity.verified_by_vlm
    _stamp(row, entity)
