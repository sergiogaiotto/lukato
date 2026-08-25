"""Dubles em memoria das portas do lukato, para testes sem banco e sem rede.

O que mora aqui:

* os **doze repositorios** da SPEC-0000 secao 7.6 em memoria, com a mesma
  semantica dos adaptadores SQLAlchemy (`ConflictError` em chave duplicada,
  `NotFoundError` em `update`/`delete` de item inexistente, mesma ordenacao de
  `list`), mais :class:`FakeUnitOfWork` e :class:`FakeUnitOfWorkFactory`;
* :class:`FakeVectorStore`, indice vetorial por cosseno calculado em memoria;
* :class:`CountingLLM` (conta chamadas e guarda as mensagens recebidas — e o que
  prova que o guardrail de entrada barrou **antes** do provedor),
  :class:`FailingLLM` (levanta `ProviderError`) e :class:`SlowLLM` (lentidao
  simulada por contador de ciclos, sem `sleep` real).

Todo objeto guardado e uma **copia**: mutar o que saiu de um `get` nao muda o que
esta armazenado, exatamente como acontece com um banco de verdade.

A conformidade com as portas nao e promessa de docstring: o proprio modulo
executa, no fim, uma bateria de `isinstance` contra espelhos `runtime_checkable`
dos `Protocol` originais. Um metodo que sumir quebra o import da suite inteira.
"""

from __future__ import annotations

import asyncio
import builtins
import math
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from lukato.domain.errors import ConflictError, NotFoundError, ProviderError, ValidationError
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionStatus,
    MediaAsset,
    OcrText,
    SceneCut,
    Transcript,
)
from lukato.domain.models.finops import Budget, CostSummary, UsageRecord
from lukato.domain.models.guardrail import GuardrailPolicy, GuardrailStage
from lukato.domain.models.identity import ApiKey, User
from lukato.domain.models.knowledge import Chunk, Document, SearchHit
from lukato.domain.models.module import ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.models.run import AgentRun, RunStatus, RunStep, TokenUsage
from lukato.domain.ports.llm import ChatMessage, LLMPort, LLMResponse
from lukato.domain.ports.repositories import (
    ApiKeyRepository,
    BudgetRepository,
    CommercialRepository,
    DetectionRepository,
    DocumentRepository,
    GuardrailRepository,
    MediaRepository,
    ModuleRepository,
    PromptRepository,
    RunRepository,
    UsageRepository,
    UserRepository,
)
from lukato.domain.ports.unit_of_work import UnitOfWork
from lukato.domain.ports.vector_store import VectorStorePort
from lukato.domain.types import Id, Json

__all__ = [
    "COST_DIGITS",
    "CountingLLM",
    "FailingLLM",
    "FakeApiKeyRepository",
    "FakeBudgetRepository",
    "FakeCommercialRepository",
    "FakeDetectionRepository",
    "FakeDocumentRepository",
    "FakeGuardrailRepository",
    "FakeMediaRepository",
    "FakeModuleRepository",
    "FakePromptRepository",
    "FakeRunRepository",
    "FakeUnitOfWork",
    "FakeUnitOfWorkFactory",
    "FakeUsageRepository",
    "FakeUserRepository",
    "FakeVectorStore",
    "SlowLLM",
]

COST_DIGITS = 8
"""Casas decimais do arredondamento de custo, iguais as do `CostCalculator`."""

_ModelT = TypeVar("_ModelT")


def _copy(item: _ModelT) -> _ModelT:
    """Devolve uma copia profunda do modelo (o armazenamento nunca compartilha objeto)."""
    return item.model_copy(deep=True)  # type: ignore[attr-defined,no-any-return]


def _page(items: builtins.list[_ModelT], limit: int, offset: int) -> builtins.list[_ModelT]:
    """Aplica paginacao com os mesmos limites dos repositorios reais."""
    start = max(0, int(offset))
    return items[start : start + max(0, int(limit))]


def _hit(text: str | None, term: str | None) -> bool:
    """True quando `term` e vazio ou aparece em `text`, sem diferenciar caixa."""
    if not term:
        return True
    return term.lower() in (text or "").lower()


def _within(moment: datetime, since: datetime | None, until: datetime | None) -> bool:
    """True quando `moment` cai dentro do intervalo semiaberto informado."""
    if since is not None and moment < since:
        return False
    return not (until is not None and moment > until)


# --------------------------------------------------------------------------- #
# Registry de modulos, prompts e guardrails
# --------------------------------------------------------------------------- #
class FakeModuleRepository:
    """`ModuleRepository` em memoria, ordenado por slug."""

    def __init__(self) -> None:
        self.items: dict[Id, ModuleDefinition] = {}

    async def add(self, module: ModuleDefinition) -> ModuleDefinition:
        """Insere a definicao; slug duplicado gera `ConflictError`."""
        if any(item.slug == module.slug for item in self.items.values()):
            raise ConflictError(
                f"ja existe um modulo com o slug '{module.slug}'", details={"slug": module.slug}
            )
        self.items[module.id] = _copy(module)
        return _copy(module)

    async def get(self, module_id: Id) -> ModuleDefinition | None:
        """Busca por identificador."""
        found = self.items.get(module_id)
        return None if found is None else _copy(found)

    async def get_by_slug(self, slug: str) -> ModuleDefinition | None:
        """Busca pelo slug unico do modulo."""
        for item in self.items.values():
            if item.slug == slug:
                return _copy(item)
        return None

    def _filtered(
        self,
        *,
        kind: ModuleKind | None,
        status: ModuleStatus | None,
        search: str | None,
        tags: Sequence[str] | None,
    ) -> builtins.list[ModuleDefinition]:
        """Aplica os filtros publicos e devolve a lista ordenada por slug."""
        selected = [
            item
            for item in self.items.values()
            if (kind is None or item.kind == kind)
            and (status is None or item.status == status)
            and (
                not search
                or _hit(item.slug, search)
                or _hit(item.name, search)
                or _hit(item.description, search)
            )
            and all(tag in item.tags for tag in tags or ())
        ]
        return sorted(selected, key=lambda item: item.slug)

    async def list(
        self,
        *,
        kind: ModuleKind | None = None,
        status: ModuleStatus | None = None,
        search: str | None = None,
        tags: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[ModuleDefinition]:
        """Lista definicoes por slug, aplicando filtros e paginacao."""
        found = self._filtered(kind=kind, status=status, search=search, tags=tags)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta definicoes com os mesmos filtros aceitos por `list`."""
        return len(
            self._filtered(
                kind=filters.get("kind"),
                status=filters.get("status"),
                search=filters.get("search"),
                tags=filters.get("tags"),
            )
        )

    async def update(self, module: ModuleDefinition) -> ModuleDefinition:
        """Grava a definicao existente; ausente gera `NotFoundError`."""
        if module.id not in self.items:
            raise NotFoundError(f"modulo '{module.id}' nao encontrado", details={"id": module.id})
        clash = any(
            item.slug == module.slug and item.id != module.id for item in self.items.values()
        )
        if clash:
            raise ConflictError(
                f"ja existe um modulo com o slug '{module.slug}'", details={"slug": module.slug}
            )
        self.items[module.id] = _copy(module)
        return _copy(module)

    async def delete(self, module_id: Id) -> None:
        """Remove a definicao; ausente gera `NotFoundError`."""
        if module_id not in self.items:
            raise NotFoundError(f"modulo '{module_id}' nao encontrado", details={"id": module_id})
        del self.items[module_id]


class FakePromptRepository:
    """`PromptRepository` em memoria; `get_by_slug` devolve a versao ativa mais alta."""

    def __init__(self) -> None:
        self.items: dict[Id, PromptTemplate] = {}

    async def add(self, prompt: PromptTemplate) -> PromptTemplate:
        """Insere uma versao; par `slug`+`version` duplicado gera `ConflictError`."""
        duplicated = any(
            item.slug == prompt.slug and item.version == prompt.version
            for item in self.items.values()
        )
        if duplicated:
            raise ConflictError(
                f"ja existe o prompt '{prompt.slug}' na versao {prompt.version}",
                details={"slug": prompt.slug, "version": prompt.version},
            )
        self.items[prompt.id] = _copy(prompt)
        return _copy(prompt)

    async def get(self, prompt_id: Id) -> PromptTemplate | None:
        """Busca por identificador."""
        found = self.items.get(prompt_id)
        return None if found is None else _copy(found)

    async def get_by_slug(self, slug: str) -> PromptTemplate | None:
        """Devolve a versao ativa mais recente do slug."""
        candidates = [item for item in self.items.values() if item.slug == slug and item.is_active]
        if not candidates:
            return None
        return _copy(max(candidates, key=lambda item: item.version))

    def _filtered(
        self, *, search: str | None, is_active: bool | None
    ) -> builtins.list[PromptTemplate]:
        """Aplica os filtros publicos, ordenando por slug e versao decrescente."""
        selected = [
            item
            for item in self.items.values()
            if (is_active is None or item.is_active is is_active)
            and (not search or _hit(item.slug, search) or _hit(item.name, search))
        ]
        return sorted(selected, key=lambda item: (item.slug, -item.version))

    async def list(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[PromptTemplate]:
        """Lista prompts aplicando filtros e paginacao."""
        found = self._filtered(search=search, is_active=is_active)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta prompts com os mesmos filtros aceitos por `list`."""
        return len(self._filtered(search=filters.get("search"), is_active=filters.get("is_active")))

    async def update(self, prompt: PromptTemplate) -> PromptTemplate:
        """Grava a versao existente; ausente gera `NotFoundError`."""
        if prompt.id not in self.items:
            raise NotFoundError(f"prompt '{prompt.id}' nao encontrado", details={"id": prompt.id})
        self.items[prompt.id] = _copy(prompt)
        return _copy(prompt)

    async def delete(self, prompt_id: Id) -> None:
        """Remove uma versao; ausente gera `NotFoundError`."""
        if prompt_id not in self.items:
            raise NotFoundError(f"prompt '{prompt_id}' nao encontrado", details={"id": prompt_id})
        del self.items[prompt_id]

    async def list_versions(self, slug: str) -> builtins.list[PromptTemplate]:
        """Lista todas as versoes do slug, da mais recente para a mais antiga."""
        found = [item for item in self.items.values() if item.slug == slug]
        return [_copy(item) for item in sorted(found, key=lambda item: -item.version)]


class FakeGuardrailRepository:
    """`GuardrailRepository` em memoria, ordenado por slug."""

    def __init__(self) -> None:
        self.items: dict[Id, GuardrailPolicy] = {}

    async def add(self, policy: GuardrailPolicy) -> GuardrailPolicy:
        """Insere a politica; slug duplicado gera `ConflictError`."""
        if any(item.slug == policy.slug for item in self.items.values()):
            raise ConflictError(
                f"ja existe uma politica com o slug '{policy.slug}'",
                details={"slug": policy.slug},
            )
        self.items[policy.id] = _copy(policy)
        return _copy(policy)

    async def get(self, policy_id: Id) -> GuardrailPolicy | None:
        """Busca por identificador."""
        found = self.items.get(policy_id)
        return None if found is None else _copy(found)

    async def get_by_slug(self, slug: str) -> GuardrailPolicy | None:
        """Busca pelo slug unico da politica."""
        for item in self.items.values():
            if item.slug == slug:
                return _copy(item)
        return None

    def _filtered(
        self, *, stage: GuardrailStage | None, is_active: bool | None, search: str | None
    ) -> builtins.list[GuardrailPolicy]:
        """Aplica os filtros publicos e ordena por slug."""
        selected = [
            item
            for item in self.items.values()
            if (stage is None or item.stage == stage)
            and (is_active is None or item.is_active is is_active)
            and (not search or _hit(item.slug, search) or _hit(item.name, search))
        ]
        return sorted(selected, key=lambda item: item.slug)

    async def list(
        self,
        *,
        stage: GuardrailStage | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[GuardrailPolicy]:
        """Lista politicas aplicando filtros e paginacao."""
        found = self._filtered(stage=stage, is_active=is_active, search=search)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta politicas com os mesmos filtros aceitos por `list`."""
        return len(
            self._filtered(
                stage=filters.get("stage"),
                is_active=filters.get("is_active"),
                search=filters.get("search"),
            )
        )

    async def update(self, policy: GuardrailPolicy) -> GuardrailPolicy:
        """Grava a politica existente; ausente gera `NotFoundError`."""
        if policy.id not in self.items:
            raise NotFoundError(f"politica '{policy.id}' nao encontrada", details={"id": policy.id})
        self.items[policy.id] = _copy(policy)
        return _copy(policy)

    async def delete(self, policy_id: Id) -> None:
        """Remove a politica; ausente gera `NotFoundError`."""
        if policy_id not in self.items:
            raise NotFoundError(f"politica '{policy_id}' nao encontrada", details={"id": policy_id})
        del self.items[policy_id]


# --------------------------------------------------------------------------- #
# Execucoes e FinOps
# --------------------------------------------------------------------------- #
class FakeRunRepository:
    """`RunRepository` em memoria, listado da execucao mais recente para a mais antiga."""

    def __init__(self) -> None:
        self.items: dict[Id, AgentRun] = {}
        self.steps: dict[Id, builtins.list[RunStep]] = {}

    async def add(self, run: AgentRun) -> AgentRun:
        """Insere a execucao; id repetido gera `ConflictError`."""
        if run.id in self.items:
            raise ConflictError(f"execucao '{run.id}' ja registrada", details={"id": run.id})
        self.items[run.id] = _copy(run)
        self.steps[run.id] = [_copy(step) for step in run.steps]
        return _copy(run)

    async def get(self, run_id: Id) -> AgentRun | None:
        """Busca a execucao com os passos ja carregados."""
        found = self.items.get(run_id)
        if found is None:
            return None
        restored = _copy(found)
        restored.steps = [_copy(step) for step in self.steps.get(run_id, [])]
        return restored

    def _filtered(
        self,
        *,
        module_slug: str | None,
        status: RunStatus | None,
        since: datetime | None,
        until: datetime | None,
        tenant_id: str | None,
    ) -> builtins.list[AgentRun]:
        """Aplica os filtros publicos e ordena do mais recente para o mais antigo."""
        selected = [
            item
            for item in self.items.values()
            if (module_slug is None or item.module_slug == module_slug)
            and (status is None or item.status == status)
            and (tenant_id is None or item.tenant_id == tenant_id)
            and _within(item.created_at, since, until)
        ]
        return sorted(selected, key=lambda item: (item.created_at, item.id), reverse=True)

    async def list(
        self,
        *,
        module_slug: str | None = None,
        status: RunStatus | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[AgentRun]:
        """Lista execucoes da mais recente para a mais antiga."""
        found = self._filtered(
            module_slug=module_slug, status=status, since=since, until=until, tenant_id=tenant_id
        )
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta execucoes com os mesmos filtros aceitos por `list`."""
        return len(
            self._filtered(
                module_slug=filters.get("module_slug"),
                status=filters.get("status"),
                since=filters.get("since"),
                until=filters.get("until"),
                tenant_id=filters.get("tenant_id"),
            )
        )

    async def update(self, run: AgentRun) -> AgentRun:
        """Grava o estado final da execucao; ausente gera `NotFoundError`."""
        if run.id not in self.items:
            raise NotFoundError(f"execucao '{run.id}' nao encontrada", details={"id": run.id})
        self.items[run.id] = _copy(run)
        if run.steps:
            self.steps[run.id] = [_copy(step) for step in run.steps]
        return _copy(run)

    async def add_step(self, step: RunStep) -> RunStep:
        """Anexa um passo a execucao; execucao ausente gera `NotFoundError`."""
        if step.run_id not in self.items:
            raise NotFoundError(
                f"execucao '{step.run_id}' nao encontrada", details={"id": step.run_id}
            )
        self.steps.setdefault(step.run_id, []).append(_copy(step))
        return _copy(step)

    async def list_steps(self, run_id: Id) -> builtins.list[RunStep]:
        """Lista os passos da execucao em ordem de indice."""
        found = sorted(self.steps.get(run_id, []), key=lambda step: step.index)
        return [_copy(step) for step in found]


class FakeUsageRepository:
    """`UsageRepository` em memoria com a mesma agregacao do adaptador SQL."""

    def __init__(self) -> None:
        self.items: builtins.list[UsageRecord] = []

    async def add(self, record: UsageRecord) -> UsageRecord:
        """Insere um registro de consumo."""
        self.items.append(_copy(record))
        return _copy(record)

    def _filtered(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        module_slug: str | None = None,
        model: str | None = None,
        tenant_id: str | None = None,
    ) -> builtins.list[UsageRecord]:
        """Aplica os filtros publicos e ordena do mais recente para o mais antigo."""
        selected = [
            item
            for item in self.items
            if (module_slug is None or item.module_slug == module_slug)
            and (model is None or item.model == model)
            and (tenant_id is None or item.tenant_id == tenant_id)
            and _within(item.occurred_at, since, until)
        ]
        return sorted(selected, key=lambda item: (item.occurred_at, item.id), reverse=True)

    async def list(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        module_slug: str | None = None,
        model: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> builtins.list[UsageRecord]:
        """Lista registros do mais recente para o mais antigo."""
        found = self._filtered(
            since=since, until=until, module_slug=module_slug, model=model, tenant_id=tenant_id
        )
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta registros com os mesmos filtros aceitos por `list`."""
        return len(self._filtered(**filters))

    async def summary(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        module_slug: str | None = None,
        tenant_id: str | None = None,
    ) -> CostSummary:
        """Agrega custo e tokens por modulo e por modelo.

        `runs` conta execucoes distintas; registro sem `run_id` conta como uma
        execucao propria, igual ao `CostCalculator.summarize`.
        """
        found = self._filtered(
            since=since, until=until, module_slug=module_slug, tenant_id=tenant_id
        )
        by_module: dict[str, float] = {}
        by_model: dict[str, float] = {}
        runs: set[Id] = set()
        orphans = 0
        total_usd = 0.0
        total_tokens = 0
        for item in found:
            total_usd += item.cost_usd
            total_tokens += item.usage.total_tokens
            by_module[item.module_slug] = by_module.get(item.module_slug, 0.0) + item.cost_usd
            by_model[item.model] = by_model.get(item.model, 0.0) + item.cost_usd
            if item.run_id is None:
                orphans += 1
            else:
                runs.add(item.run_id)
        return CostSummary(
            total_usd=round(total_usd, COST_DIGITS),
            total_tokens=total_tokens,
            runs=len(runs) + orphans,
            by_module={key: round(value, COST_DIGITS) for key, value in by_module.items()},
            by_model={key: round(value, COST_DIGITS) for key, value in by_model.items()},
        )

    async def total_since(self, since: datetime, *, scope: str = "global") -> float:
        """Custo total em USD desde o instante, no escopo informado."""
        if scope == "global":
            selected = self._filtered(since=since)
        elif scope.startswith("module:"):
            selected = self._filtered(since=since, module_slug=scope[len("module:") :])
        elif scope.startswith("tenant:"):
            selected = self._filtered(since=since, tenant_id=scope[len("tenant:") :])
        else:
            raise ValidationError(
                f"escopo de custo invalido: {scope!r}",
                details={"scope": scope, "supported": ["global", "module:<slug>", "tenant:<id>"]},
            )
        return round(sum(item.cost_usd for item in selected), COST_DIGITS)


class FakeBudgetRepository:
    """`BudgetRepository` em memoria, do mais recente para o mais antigo."""

    def __init__(self) -> None:
        self.items: dict[Id, Budget] = {}

    async def add(self, budget: Budget) -> Budget:
        """Insere o orcamento; id repetido gera `ConflictError`."""
        if budget.id in self.items:
            raise ConflictError(f"orcamento '{budget.id}' ja existe", details={"id": budget.id})
        self.items[budget.id] = _copy(budget)
        return _copy(budget)

    async def get(self, budget_id: Id) -> Budget | None:
        """Busca por identificador."""
        found = self.items.get(budget_id)
        return None if found is None else _copy(found)

    async def list(
        self, *, scope: str | None = None, is_active: bool | None = None
    ) -> builtins.list[Budget]:
        """Lista orcamentos aplicando os filtros informados."""
        selected = [
            item
            for item in self.items.values()
            if (scope is None or item.scope == scope)
            and (is_active is None or item.is_active is is_active)
        ]
        ordered = sorted(selected, key=lambda item: (item.created_at, item.id), reverse=True)
        return [_copy(item) for item in ordered]

    async def update(self, budget: Budget) -> Budget:
        """Grava o orcamento existente; ausente gera `NotFoundError`."""
        if budget.id not in self.items:
            raise NotFoundError(
                f"orcamento '{budget.id}' nao encontrado", details={"id": budget.id}
            )
        self.items[budget.id] = _copy(budget)
        return _copy(budget)

    async def delete(self, budget_id: Id) -> None:
        """Remove o orcamento; ausente gera `NotFoundError`."""
        if budget_id not in self.items:
            raise NotFoundError(
                f"orcamento '{budget_id}' nao encontrado", details={"id": budget_id}
            )
        del self.items[budget_id]


# --------------------------------------------------------------------------- #
# Conhecimento
# --------------------------------------------------------------------------- #
class FakeDocumentRepository:
    """`DocumentRepository` em memoria, com os chunks presos ao documento."""

    def __init__(self) -> None:
        self.items: dict[Id, Document] = {}
        self.chunks: dict[Id, builtins.list[Chunk]] = {}

    async def add(self, document: Document) -> Document:
        """Insere o documento; id repetido gera `ConflictError`."""
        if document.id in self.items:
            raise ConflictError(f"documento '{document.id}' ja existe", details={"id": document.id})
        self.items[document.id] = _copy(document)
        return _copy(document)

    async def get(self, document_id: Id) -> Document | None:
        """Busca por identificador."""
        found = self.items.get(document_id)
        return None if found is None else _copy(found)

    def _filtered(self, *, collection: str | None, search: str | None) -> builtins.list[Document]:
        """Aplica os filtros publicos e ordena do mais recente para o mais antigo."""
        selected = [
            item
            for item in self.items.values()
            if (collection is None or item.collection == collection)
            and (not search or _hit(item.title, search) or _hit(item.content, search))
        ]
        return sorted(selected, key=lambda item: (item.created_at, item.id), reverse=True)

    async def list(
        self,
        *,
        collection: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Document]:
        """Lista documentos aplicando filtros e paginacao."""
        found = self._filtered(collection=collection, search=search)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta documentos com os mesmos filtros aceitos por `list`."""
        return len(
            self._filtered(collection=filters.get("collection"), search=filters.get("search"))
        )

    async def update(self, document: Document) -> Document:
        """Grava o documento existente; ausente gera `NotFoundError`."""
        if document.id not in self.items:
            raise NotFoundError(
                f"documento '{document.id}' nao encontrado", details={"id": document.id}
            )
        self.items[document.id] = _copy(document)
        return _copy(document)

    async def delete(self, document_id: Id) -> None:
        """Remove o documento e, em cascata, seus chunks."""
        if document_id not in self.items:
            raise NotFoundError(
                f"documento '{document_id}' nao encontrado", details={"id": document_id}
            )
        del self.items[document_id]
        self.chunks.pop(document_id, None)

    async def add_chunks(self, chunks: Sequence[Chunk]) -> int:
        """Insere os chunks informados; devolve quantos foram gravados."""
        for chunk in chunks:
            self.chunks.setdefault(chunk.document_id, []).append(_copy(chunk))
        return len(chunks)

    async def list_chunks(self, document_id: Id) -> builtins.list[Chunk]:
        """Lista os chunks do documento em ordem de indice."""
        found = sorted(self.chunks.get(document_id, []), key=lambda chunk: chunk.index)
        return [_copy(chunk) for chunk in found]

    async def delete_chunks(self, document_id: Id) -> int:
        """Remove os chunks do documento; devolve quantos foram apagados."""
        removed = self.chunks.pop(document_id, [])
        return len(removed)

    async def collections(self) -> builtins.list[str]:
        """Lista as colecoes distintas existentes, em ordem alfabetica."""
        return sorted({item.collection for item in self.items.values()})


# --------------------------------------------------------------------------- #
# Identidade
# --------------------------------------------------------------------------- #
class FakeUserRepository:
    """`UserRepository` em memoria; e-mail e unico."""

    def __init__(self) -> None:
        self.items: dict[Id, User] = {}

    async def add(self, user: User) -> User:
        """Insere o usuario; e-mail duplicado gera `ConflictError`."""
        if any(item.email == user.email for item in self.items.values()):
            raise ConflictError(
                f"ja existe um usuario com o e-mail '{user.email}'", details={"email": user.email}
            )
        self.items[user.id] = _copy(user)
        return _copy(user)

    async def get(self, user_id: Id) -> User | None:
        """Busca por identificador."""
        found = self.items.get(user_id)
        return None if found is None else _copy(found)

    async def get_by_email(self, email: str) -> User | None:
        """Busca pelo e-mail unico (comparacao sem diferenciar caixa)."""
        for item in self.items.values():
            if item.email.lower() == email.lower():
                return _copy(item)
        return None

    async def list(self, *, limit: int = 50, offset: int = 0) -> builtins.list[User]:
        """Lista usuarios do mais recente para o mais antigo."""
        ordered = sorted(
            self.items.values(), key=lambda item: (item.created_at, item.id), reverse=True
        )
        return [_copy(item) for item in _page(ordered, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta usuarios (os filtros seguem os de `list`, hoje apenas paginacao)."""
        return len(self.items)

    async def update(self, user: User) -> User:
        """Grava o usuario existente; ausente gera `NotFoundError`."""
        if user.id not in self.items:
            raise NotFoundError(f"usuario '{user.id}' nao encontrado", details={"id": user.id})
        self.items[user.id] = _copy(user)
        return _copy(user)

    async def delete(self, user_id: Id) -> None:
        """Remove o usuario; ausente gera `NotFoundError`."""
        if user_id not in self.items:
            raise NotFoundError(f"usuario '{user_id}' nao encontrado", details={"id": user_id})
        del self.items[user_id]


class FakeApiKeyRepository:
    """`ApiKeyRepository` em memoria; o prefixo e unico."""

    def __init__(self) -> None:
        self.items: dict[Id, ApiKey] = {}

    async def add(self, api_key: ApiKey) -> ApiKey:
        """Insere a chave; prefixo duplicado gera `ConflictError`."""
        if any(item.prefix == api_key.prefix for item in self.items.values()):
            raise ConflictError(
                f"ja existe uma chave com o prefixo '{api_key.prefix}'",
                details={"prefix": api_key.prefix},
            )
        self.items[api_key.id] = _copy(api_key)
        return _copy(api_key)

    async def get(self, api_key_id: Id) -> ApiKey | None:
        """Busca por identificador."""
        found = self.items.get(api_key_id)
        return None if found is None else _copy(found)

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        """Busca pelo prefixo apresentado na requisicao."""
        for item in self.items.values():
            if item.prefix == prefix:
                return _copy(item)
        return None

    async def list(
        self, *, is_active: bool | None = None, limit: int = 50, offset: int = 0
    ) -> builtins.list[ApiKey]:
        """Lista chaves da mais recente para a mais antiga."""
        selected = [
            item for item in self.items.values() if is_active is None or item.is_active is is_active
        ]
        ordered = sorted(selected, key=lambda item: (item.created_at, item.id), reverse=True)
        return [_copy(item) for item in _page(ordered, limit, offset)]

    async def update(self, api_key: ApiKey) -> ApiKey:
        """Grava a chave existente; ausente gera `NotFoundError`."""
        if api_key.id not in self.items:
            raise NotFoundError(f"chave '{api_key.id}' nao encontrada", details={"id": api_key.id})
        self.items[api_key.id] = _copy(api_key)
        return _copy(api_key)

    async def delete(self, api_key_id: Id) -> None:
        """Remove a chave; ausente gera `NotFoundError`."""
        if api_key_id not in self.items:
            raise NotFoundError(f"chave '{api_key_id}' nao encontrada", details={"id": api_key_id})
        del self.items[api_key_id]

    async def touch(self, api_key_id: Id, when: datetime) -> None:
        """Registra o instante do ultimo uso da chave."""
        item = self.items.get(api_key_id)
        if item is None:
            raise NotFoundError(f"chave '{api_key_id}' nao encontrada", details={"id": api_key_id})
        item.last_used_at = when


# --------------------------------------------------------------------------- #
# AdWatch
# --------------------------------------------------------------------------- #
class FakeCommercialRepository:
    """`CommercialRepository` em memoria, ordenado pelo codigo de negocio."""

    def __init__(self) -> None:
        self.items: dict[Id, Commercial] = {}
        self.fingerprints: dict[Id, AdFingerprint] = {}

    async def add(self, commercial: Commercial) -> Commercial:
        """Insere o comercial; codigo de negocio duplicado gera `ConflictError`."""
        code = commercial.commercial_id
        if any(item.commercial_id == code for item in self.items.values()):
            raise ConflictError(
                f"ja existe um comercial com o codigo '{code}'", details={"commercial_id": code}
            )
        self.items[commercial.id] = _copy(commercial)
        return _copy(commercial)

    async def get(self, commercial_id: Id) -> Commercial | None:
        """Busca por identificador interno."""
        found = self.items.get(commercial_id)
        return None if found is None else _copy(found)

    async def get_by_code(self, code: str) -> Commercial | None:
        """Busca pelo codigo de negocio."""
        for item in self.items.values():
            if item.commercial_id == code:
                return _copy(item)
        return None

    def _filtered(
        self,
        *,
        search: str | None,
        brand: str | None,
        campaign: str | None,
        is_active: bool | None,
    ) -> builtins.list[Commercial]:
        """Aplica os filtros publicos e ordena pelo codigo de negocio."""
        selected = [
            item
            for item in self.items.values()
            if (brand is None or item.brand == brand)
            and (campaign is None or item.campaign == campaign)
            and (is_active is None or item.is_active is is_active)
            and (
                not search
                or _hit(item.commercial_id, search)
                or _hit(item.campaign, search)
                or _hit(item.brand, search)
                or _hit(item.text, search)
            )
        ]
        return sorted(selected, key=lambda item: (item.commercial_id, item.id))

    async def list(
        self,
        *,
        search: str | None = None,
        brand: str | None = None,
        campaign: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Commercial]:
        """Lista comerciais aplicando filtros e paginacao."""
        found = self._filtered(search=search, brand=brand, campaign=campaign, is_active=is_active)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta comerciais com os mesmos filtros aceitos por `list`."""
        return len(
            self._filtered(
                search=filters.get("search"),
                brand=filters.get("brand"),
                campaign=filters.get("campaign"),
                is_active=filters.get("is_active"),
            )
        )

    async def update(self, commercial: Commercial) -> Commercial:
        """Grava o comercial existente; ausente gera `NotFoundError`."""
        if commercial.id not in self.items:
            raise NotFoundError(
                f"comercial '{commercial.id}' nao encontrado", details={"id": commercial.id}
            )
        self.items[commercial.id] = _copy(commercial)
        return _copy(commercial)

    async def delete(self, commercial_id: Id) -> None:
        """Remove o comercial e, em cascata, sua assinatura."""
        if commercial_id not in self.items:
            raise NotFoundError(
                f"comercial '{commercial_id}' nao encontrado", details={"id": commercial_id}
            )
        del self.items[commercial_id]
        self.fingerprints.pop(commercial_id, None)

    async def all_active(self) -> builtins.list[Commercial]:
        """Devolve todos os comerciais ativos, ordenados pelo codigo."""
        return await self.list(is_active=True, limit=len(self.items) or 1)

    async def upsert_fingerprint(self, fp: AdFingerprint) -> AdFingerprint:
        """Grava a assinatura do comercial, substituindo a anterior."""
        self.fingerprints[fp.commercial_id] = _copy(fp)
        return _copy(fp)

    async def get_fingerprint(self, commercial_id: Id) -> AdFingerprint | None:
        """Busca a assinatura de um comercial."""
        found = self.fingerprints.get(commercial_id)
        return None if found is None else _copy(found)

    async def list_fingerprints(self) -> builtins.list[AdFingerprint]:
        """Lista todas as assinaturas disponiveis, por comercial."""
        ordered = sorted(self.fingerprints.values(), key=lambda item: item.commercial_id)
        return [_copy(item) for item in ordered]


class FakeMediaRepository:
    """`MediaRepository` em memoria com transcricao, cenas e OCR por ativo."""

    def __init__(self) -> None:
        self.items: dict[Id, MediaAsset] = {}
        self.transcripts: dict[Id, Transcript] = {}
        self.scenes: dict[Id, builtins.list[SceneCut]] = {}
        self.ocr: dict[Id, builtins.list[OcrText]] = {}

    async def add(self, asset: MediaAsset) -> MediaAsset:
        """Registra o ativo de midia; id repetido gera `ConflictError`."""
        if asset.id in self.items:
            raise ConflictError(f"midia '{asset.id}' ja registrada", details={"id": asset.id})
        self.items[asset.id] = _copy(asset)
        return _copy(asset)

    async def get(self, media_id: Id) -> MediaAsset | None:
        """Busca por identificador."""
        found = self.items.get(media_id)
        return None if found is None else _copy(found)

    def _filtered(self, *, status: str | None, search: str | None) -> builtins.list[MediaAsset]:
        """Aplica os filtros publicos e ordena do mais recente para o mais antigo."""
        selected = [
            item
            for item in self.items.values()
            if (status is None or item.status == status)
            and (not search or _hit(item.uri, search) or _hit(item.title, search))
        ]
        return sorted(selected, key=lambda item: (item.created_at, item.id), reverse=True)

    async def list(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[MediaAsset]:
        """Lista ativos aplicando filtros e paginacao."""
        found = self._filtered(status=status, search=search)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta ativos com os mesmos filtros aceitos por `list`."""
        return len(self._filtered(status=filters.get("status"), search=filters.get("search")))

    async def update(self, asset: MediaAsset) -> MediaAsset:
        """Grava o ativo existente; ausente gera `NotFoundError`."""
        if asset.id not in self.items:
            raise NotFoundError(f"midia '{asset.id}' nao encontrada", details={"id": asset.id})
        self.items[asset.id] = _copy(asset)
        return _copy(asset)

    async def delete(self, media_id: Id) -> None:
        """Remove o ativo e, em cascata, transcricao, cenas e OCR."""
        if media_id not in self.items:
            raise NotFoundError(f"midia '{media_id}' nao encontrada", details={"id": media_id})
        del self.items[media_id]
        self.transcripts.pop(media_id, None)
        self.scenes.pop(media_id, None)
        self.ocr.pop(media_id, None)

    async def save_transcript(self, transcript: Transcript) -> Transcript:
        """Grava a transcricao do ativo, substituindo a anterior."""
        self.transcripts[transcript.media_id] = _copy(transcript)
        return _copy(transcript)

    async def get_transcript(self, media_id: Id) -> Transcript | None:
        """Busca a transcricao do ativo."""
        found = self.transcripts.get(media_id)
        return None if found is None else _copy(found)

    async def save_scenes(self, media_id: Id, scenes: Sequence[SceneCut]) -> int:
        """Substitui os cortes de cena do ativo; devolve quantos foram gravados."""
        self.scenes[media_id] = [_copy(scene) for scene in scenes]
        return len(scenes)

    async def list_scenes(self, media_id: Id) -> builtins.list[SceneCut]:
        """Lista os cortes de cena em ordem temporal."""
        found = sorted(self.scenes.get(media_id, []), key=lambda cut: (cut.index, cut.start))
        return [_copy(cut) for cut in found]

    async def save_ocr(self, media_id: Id, texts: Sequence[OcrText]) -> int:
        """Substitui os textos de OCR do ativo; devolve quantos foram gravados."""
        self.ocr[media_id] = [_copy(text) for text in texts]
        return len(texts)

    async def list_ocr(self, media_id: Id) -> builtins.list[OcrText]:
        """Lista os textos de OCR em ordem temporal."""
        found = sorted(self.ocr.get(media_id, []), key=lambda text: (text.start, text.end))
        return [_copy(text) for text in found]


class FakeDetectionRepository:
    """`DetectionRepository` em memoria, ordenado pela linha do tempo."""

    def __init__(self) -> None:
        self.items: dict[Id, Detection] = {}

    async def add(self, detection: Detection) -> Detection:
        """Insere uma deteccao; id repetido gera `ConflictError`."""
        if detection.id in self.items:
            raise ConflictError(
                f"deteccao '{detection.id}' ja registrada", details={"id": detection.id}
            )
        self.items[detection.id] = _copy(detection)
        return _copy(detection)

    async def add_many(self, detections: Sequence[Detection]) -> builtins.list[Detection]:
        """Insere varias deteccoes de uma vez, preservando a ordem."""
        return [await self.add(detection) for detection in detections]

    async def get(self, detection_id: Id) -> Detection | None:
        """Busca por identificador."""
        found = self.items.get(detection_id)
        return None if found is None else _copy(found)

    def _filtered(
        self,
        *,
        media_id: Id | None,
        commercial_id: Id | None,
        status: DetectionStatus | None,
    ) -> builtins.list[Detection]:
        """Aplica os filtros publicos e ordena por inicio, fim e id."""
        selected = [
            item
            for item in self.items.values()
            if (media_id is None or item.media_id == media_id)
            and (commercial_id is None or item.commercial_id == commercial_id)
            and (status is None or item.status == status)
        ]
        return sorted(selected, key=lambda item: (item.start, item.end, item.id))

    async def list(
        self,
        *,
        media_id: Id | None = None,
        commercial_id: Id | None = None,
        status: DetectionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Detection]:
        """Lista deteccoes na ordem da linha do tempo, sempre paginadas."""
        found = self._filtered(media_id=media_id, commercial_id=commercial_id, status=status)
        return [_copy(item) for item in _page(found, limit, offset)]

    async def count(self, **filters: Any) -> int:
        """Conta deteccoes com os mesmos filtros aceitos por `list`."""
        return len(
            self._filtered(
                media_id=filters.get("media_id"),
                commercial_id=filters.get("commercial_id"),
                status=filters.get("status"),
            )
        )

    async def update(self, detection: Detection) -> Detection:
        """Grava a deteccao existente; ausente gera `NotFoundError`."""
        if detection.id not in self.items:
            raise NotFoundError(
                f"deteccao '{detection.id}' nao encontrada", details={"id": detection.id}
            )
        self.items[detection.id] = _copy(detection)
        return _copy(detection)

    async def delete(self, detection_id: Id) -> None:
        """Remove a deteccao; ausente gera `NotFoundError`."""
        if detection_id not in self.items:
            raise NotFoundError(
                f"deteccao '{detection_id}' nao encontrada", details={"id": detection_id}
            )
        del self.items[detection_id]

    async def delete_by_media(self, media_id: Id) -> int:
        """Remove todas as deteccoes do ativo; devolve quantas foram apagadas."""
        alvos = [key for key, item in self.items.items() if item.media_id == media_id]
        for key in alvos:
            del self.items[key]
        return len(alvos)


# --------------------------------------------------------------------------- #
# Unidade de trabalho
# --------------------------------------------------------------------------- #
class FakeUnitOfWork:
    """`UnitOfWork` em memoria: os doze repositorios sem transacao de verdade.

    As escritas sao imediatas (nao ha o que descarregar), entao `commit()` e
    `rollback()` apenas **contam** as chamadas. E de proposito: os testes de caso
    de uso que usam este duble provam *que* o caso de uso confirma a transacao,
    nao o comportamento transacional do SQLAlchemy — esse tem os seus proprios
    testes de integracao contra o SQLite.
    """

    def __init__(self, repositories: dict[str, Any] | None = None) -> None:
        pronto = repositories if repositories is not None else build_fake_repositories()
        self.modules: FakeModuleRepository = pronto["modules"]
        self.prompts: FakePromptRepository = pronto["prompts"]
        self.guardrails: FakeGuardrailRepository = pronto["guardrails"]
        self.runs: FakeRunRepository = pronto["runs"]
        self.usage: FakeUsageRepository = pronto["usage"]
        self.budgets: FakeBudgetRepository = pronto["budgets"]
        self.documents: FakeDocumentRepository = pronto["documents"]
        self.users: FakeUserRepository = pronto["users"]
        self.api_keys: FakeApiKeyRepository = pronto["api_keys"]
        self.commercials: FakeCommercialRepository = pronto["commercials"]
        self.media: FakeMediaRepository = pronto["media"]
        self.detections: FakeDetectionRepository = pronto["detections"]
        self.commits = 0
        self.rollbacks = 0
        self.entered = 0
        self.closed = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        """Abre o contexto (nao ha sessao para abrir)."""
        self.entered += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Desfaz em caso de excecao e sempre encerra o contexto."""
        if exc and exc[0] is not None:
            await self.rollback()
        self.closed += 1

    async def commit(self) -> None:
        """Conta a confirmacao da transacao."""
        self.commits += 1

    async def rollback(self) -> None:
        """Conta o descarte da transacao."""
        self.rollbacks += 1


def build_fake_repositories() -> dict[str, Any]:
    """Cria um conjunto novo dos doze repositorios em memoria."""
    return {
        "modules": FakeModuleRepository(),
        "prompts": FakePromptRepository(),
        "guardrails": FakeGuardrailRepository(),
        "runs": FakeRunRepository(),
        "usage": FakeUsageRepository(),
        "budgets": FakeBudgetRepository(),
        "documents": FakeDocumentRepository(),
        "users": FakeUserRepository(),
        "api_keys": FakeApiKeyRepository(),
        "commercials": FakeCommercialRepository(),
        "media": FakeMediaRepository(),
        "detections": FakeDetectionRepository(),
    }


class FakeUnitOfWorkFactory:
    """`UnitOfWorkFactory` que entrega unidades sobre o **mesmo** armazenamento.

    Duas chamadas devolvem unidades diferentes que enxergam os mesmos dados — e o
    que um banco faz, e o que os casos de uso esperam quando abrem uma transacao
    por operacao.
    """

    def __init__(self) -> None:
        self.repositories = build_fake_repositories()
        self.created: list[FakeUnitOfWork] = []

    def __call__(self) -> FakeUnitOfWork:
        """Cria uma nova unidade de trabalho sobre o armazenamento compartilhado."""
        uow = FakeUnitOfWork(self.repositories)
        self.created.append(uow)
        return uow

    @property
    def commits(self) -> int:
        """Total de `commit()` em todas as unidades ja criadas."""
        return sum(uow.commits for uow in self.created)

    @property
    def rollbacks(self) -> int:
        """Total de `rollback()` em todas as unidades ja criadas."""
        return sum(uow.rollbacks for uow in self.created)


# --------------------------------------------------------------------------- #
# Indice vetorial
# --------------------------------------------------------------------------- #
def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Similaridade de cosseno entre dois vetores; vetor nulo devolve 0.0."""
    if not left or not right or len(left) != len(right):
        return 0.0
    produto = sum(a * b for a, b in zip(left, right, strict=True))
    norma = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return 0.0 if norma == 0.0 else produto / norma


class FakeVectorStore:
    """`VectorStorePort` em memoria, com similaridade de cosseno exata."""

    def __init__(self) -> None:
        self.items: dict[str, dict[Id, Chunk]] = {}

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        """Grava ou atualiza os chunks; devolve quantos foram afetados."""
        bucket = self.items.setdefault(collection, {})
        for chunk in chunks:
            bucket[chunk.id] = _copy(chunk)
        return len(chunks)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int = 10,
        filters: Json | None = None,
    ) -> builtins.list[SearchHit]:
        """Busca por cosseno, do maior para o menor score."""
        bucket = self.items.get(collection, {})
        scored: builtins.list[tuple[float, Chunk]] = []
        for chunk in bucket.values():
            if filters and any(chunk.metadata.get(key) != value for key, value in filters.items()):
                continue
            scored.append((_cosine(vector, chunk.embedding or []), chunk))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [
            SearchHit(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                collection=collection,
                content=chunk.content,
                score=score,
                metadata=dict(chunk.metadata),
            )
            for score, chunk in scored[: max(0, limit)]
        ]

    async def delete(self, collection: str, *, document_id: Id | None = None) -> int:
        """Remove a colecao inteira ou apenas os chunks de um documento."""
        bucket = self.items.get(collection)
        if bucket is None:
            return 0
        if document_id is None:
            self.items.pop(collection, None)
            return len(bucket)
        alvos = [key for key, chunk in bucket.items() if chunk.document_id == document_id]
        for key in alvos:
            del bucket[key]
        return len(alvos)

    async def collections(self) -> builtins.list[str]:
        """Lista as colecoes existentes, em ordem alfabetica."""
        return sorted(self.items)


# --------------------------------------------------------------------------- #
# Provedores de LLM
# --------------------------------------------------------------------------- #
class CountingLLM:
    """`LLMPort` espiao: conta chamadas e guarda **tudo** o que recebeu.

    E o duble que prova a ordem da trinca: se o guardrail de entrada bloqueou, o
    provedor nao pode ter sido chamado, e `llm.calls == 0` diz isso sem ambiguidade.

    Com `responses=[...]` as respostas sao roteirizadas na ordem; esgotada a lista
    (ou sem ela), ecoa a ultima mensagem do usuario com `prefix`.
    """

    def __init__(
        self,
        *,
        responses: Sequence[str] | None = None,
        model: str = "contador",
        prefix: str = "[echo] ",
    ) -> None:
        self._model = model
        self._prefix = prefix
        self._responses = list(responses or [])
        self.calls = 0
        self.stream_calls = 0
        self.messages: list[list[ChatMessage]] = []
        self.kwargs: list[dict[str, Any]] = []

    @property
    def default_model(self) -> str:
        """Modelo reportado quando a chamada nao informa um explicitamente."""
        return self._model

    @property
    def last_messages(self) -> list[ChatMessage]:
        """Mensagens da ultima chamada; lista vazia quando nunca foi chamado."""
        return self.messages[-1] if self.messages else []

    @property
    def last_system_prompt(self) -> str:
        """Conteudo da primeira mensagem `system` da ultima chamada."""
        for message in self.last_messages:
            if message.role == "system":
                return message.content
        return ""

    @property
    def last_user_text(self) -> str:
        """Conteudo da ultima mensagem `user` da ultima chamada."""
        for message in reversed(self.last_messages):
            if message.role == "user":
                return message.content
        return ""

    def reset(self) -> None:
        """Zera contadores e historico, mantendo as respostas roteirizadas."""
        self.calls = 0
        self.stream_calls = 0
        self.messages.clear()
        self.kwargs.clear()

    def _answer(self, messages: Sequence[ChatMessage]) -> str:
        """Resolve o texto da resposta: roteiro primeiro, eco depois."""
        if self._responses:
            return self._responses.pop(0)
        for message in reversed(messages):
            if message.role == "user":
                return f"{self._prefix}{message.content}"
        return self._prefix.strip()

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
        response_format: Json | None = None,
        metadata: Json | None = None,
    ) -> LLMResponse:
        """Registra a chamada e devolve a resposta deterministica."""
        self.calls += 1
        self.messages.append([message.model_copy(deep=True) for message in messages])
        self.kwargs.append(
            {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stop": list(stop) if stop else None,
                "response_format": response_format,
                "metadata": metadata,
            }
        )
        content = self._answer(messages)
        prompt_text = "".join(message.content for message in messages)
        return LLMResponse(
            content=content,
            model=model or self._model,
            usage=TokenUsage.of(len(prompt_text) // 4, len(content) // 4),
            finish_reason="stop",
            raw={"provider": "counting", "call": self.calls},
            latency_ms=0.0,
        )

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """Emite a resposta de `chat()` em fragmentos de 24 caracteres."""
        self.stream_calls += 1
        resposta = await self.chat(messages, **kwargs)
        for inicio in range(0, len(resposta.content), 24):
            yield resposta.content[inicio : inicio + 24]

    async def list_models(self) -> builtins.list[str]:
        """Catalogo do provedor espiao: apenas o proprio modelo."""
        return [self._model]

    async def health(self) -> bool:
        """O espiao esta sempre saudavel."""
        return True


class FailingLLM:
    """`LLMPort` que sempre falha com `ProviderError` (SPEC-0000 secao 14).

    Serve para provar que a falha do provedor vira `502 provider_error` no
    envelope de erro e que o `AgentRun` termina em `FAILED` — sem depender de uma
    indisponibilidade de rede real.
    """

    def __init__(self, *, model: str = "falho", message: str = "provedor indisponivel") -> None:
        self._model = model
        self._message = message
        self.calls = 0

    @property
    def default_model(self) -> str:
        """Modelo reportado pelo provedor falho."""
        return self._model

    def _boom(self) -> ProviderError:
        """Monta o erro de provedor com o motivo configurado."""
        return ProviderError(self._message, details={"model": self._model})

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
        response_format: Json | None = None,
        metadata: Json | None = None,
    ) -> LLMResponse:
        """Conta a tentativa e levanta `ProviderError`."""
        self.calls += 1
        raise self._boom()

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """Levanta `ProviderError` no primeiro fragmento pedido."""
        self.calls += 1
        raise self._boom()
        yield ""  # pragma: no cover - mantem a funcao como gerador assincrono

    async def list_models(self) -> builtins.list[str]:
        """Levanta `ProviderError`: nao ha catalogo sem provedor."""
        raise self._boom()

    async def health(self) -> bool:
        """Provedor falho nunca esta saudavel."""
        return False


class SlowLLM:
    """`LLMPort` lento **sem** `sleep` real: a lentidao e contada, nao esperada.

    Cada chamada cede o controle ao laco de eventos `ticks` vezes (`asyncio.sleep(0)`,
    que nao espera relogio nenhum) e acumula `delay_seconds` em
    :attr:`elapsed_seconds`, que e o tempo *ficticio* reportado em
    `LLMResponse.latency_ms`. Assim um teste de concorrencia ou de latencia roda em
    microssegundos e da sempre o mesmo resultado.

    Com `raises_timeout=True` a chamada levanta `TimeoutError` depois dos ciclos,
    para exercitar o tratamento de estouro de prazo sem esperar de verdade.
    """

    def __init__(
        self,
        *,
        ticks: int = 3,
        delay_seconds: float = 1.0,
        model: str = "lento",
        prefix: str = "[echo] ",
        raises_timeout: bool = False,
    ) -> None:
        self._ticks = max(0, int(ticks))
        self._delay = float(delay_seconds)
        self._model = model
        self._prefix = prefix
        self._raises_timeout = raises_timeout
        self.calls = 0
        self.elapsed_seconds = 0.0

    @property
    def default_model(self) -> str:
        """Modelo reportado pelo provedor lento."""
        return self._model

    @property
    def ticks(self) -> int:
        """Quantidade de ciclos cedidos ao laco de eventos por chamada."""
        return self._ticks

    async def _burn(self) -> None:
        """Consome os ciclos configurados sem esperar o relogio."""
        for _ in range(self._ticks):
            await asyncio.sleep(0)
        self.elapsed_seconds += self._delay
        if self._raises_timeout:
            raise TimeoutError(f"provedor '{self._model}' estourou o prazo (simulado)")

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
        response_format: Json | None = None,
        metadata: Json | None = None,
    ) -> LLMResponse:
        """Queima os ciclos configurados e devolve o eco com latencia ficticia."""
        self.calls += 1
        await self._burn()
        texto = ""
        for message in reversed(messages):
            if message.role == "user":
                texto = message.content
                break
        content = f"{self._prefix}{texto}"
        return LLMResponse(
            content=content,
            model=model or self._model,
            usage=TokenUsage.of(len(texto) // 4, len(content) // 4),
            finish_reason="stop",
            raw={"provider": "slow", "simulated_delay_seconds": self._delay},
            latency_ms=self._delay * 1000.0,
        )

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[str]:
        """Emite a resposta em fragmentos, um ciclo cedido por fragmento."""
        resposta = await self.chat(messages, **kwargs)
        for inicio in range(0, len(resposta.content), 24):
            await asyncio.sleep(0)
            yield resposta.content[inicio : inicio + 24]

    async def list_models(self) -> builtins.list[str]:
        """Catalogo do provedor lento: apenas o proprio modelo."""
        return [self._model]

    async def health(self) -> bool:
        """O provedor lento responde, so demora (ficticiamente)."""
        return True


# --------------------------------------------------------------------------- #
# Conformidade com as portas (verificada no import)
# --------------------------------------------------------------------------- #
@runtime_checkable
class _ModuleRepositoryRT(ModuleRepository, Protocol): ...


@runtime_checkable
class _PromptRepositoryRT(PromptRepository, Protocol): ...


@runtime_checkable
class _GuardrailRepositoryRT(GuardrailRepository, Protocol): ...


@runtime_checkable
class _RunRepositoryRT(RunRepository, Protocol): ...


@runtime_checkable
class _UsageRepositoryRT(UsageRepository, Protocol): ...


@runtime_checkable
class _BudgetRepositoryRT(BudgetRepository, Protocol): ...


@runtime_checkable
class _DocumentRepositoryRT(DocumentRepository, Protocol): ...


@runtime_checkable
class _UserRepositoryRT(UserRepository, Protocol): ...


@runtime_checkable
class _ApiKeyRepositoryRT(ApiKeyRepository, Protocol): ...


@runtime_checkable
class _CommercialRepositoryRT(CommercialRepository, Protocol): ...


@runtime_checkable
class _MediaRepositoryRT(MediaRepository, Protocol): ...


@runtime_checkable
class _DetectionRepositoryRT(DetectionRepository, Protocol): ...


@runtime_checkable
class _UnitOfWorkRT(UnitOfWork, Protocol): ...


@runtime_checkable
class _VectorStoreRT(VectorStorePort, Protocol): ...


PORT_CONFORMANCE: tuple[tuple[str, Any, Any], ...] = (
    ("modules", FakeModuleRepository(), _ModuleRepositoryRT),
    ("prompts", FakePromptRepository(), _PromptRepositoryRT),
    ("guardrails", FakeGuardrailRepository(), _GuardrailRepositoryRT),
    ("runs", FakeRunRepository(), _RunRepositoryRT),
    ("usage", FakeUsageRepository(), _UsageRepositoryRT),
    ("budgets", FakeBudgetRepository(), _BudgetRepositoryRT),
    ("documents", FakeDocumentRepository(), _DocumentRepositoryRT),
    ("users", FakeUserRepository(), _UserRepositoryRT),
    ("api_keys", FakeApiKeyRepository(), _ApiKeyRepositoryRT),
    ("commercials", FakeCommercialRepository(), _CommercialRepositoryRT),
    ("media", FakeMediaRepository(), _MediaRepositoryRT),
    ("detections", FakeDetectionRepository(), _DetectionRepositoryRT),
    ("unit_of_work", FakeUnitOfWork(), _UnitOfWorkRT),
    ("vector_store", FakeVectorStore(), _VectorStoreRT),
)
"""Pares `(nome, duble, espelho runtime_checkable da porta)` conferidos no import."""


def _assert_conformance(pares: Iterable[tuple[str, Any, Any]]) -> None:
    """Confere `isinstance` de cada duble contra a sua porta; falha alto no import."""
    for nome, duble, porta in pares:
        if not isinstance(duble, porta):
            raise TypeError(
                f"o duble '{nome}' ({type(duble).__name__}) nao satisfaz a porta "
                f"{porta.__name__}: algum metodo do Protocol esta faltando"
            )


_assert_conformance(PORT_CONFORMANCE)

LLM_CONFORMANCE: tuple[tuple[str, Any, Any], ...] = (
    ("counting_llm", CountingLLM(), LLMPort),
    ("failing_llm", FailingLLM(), LLMPort),
    ("slow_llm", SlowLLM(), LLMPort),
)
"""Provedores de LLM dublados conferidos contra `LLMPort` (ja `runtime_checkable`)."""

_assert_conformance(LLM_CONFORMANCE)
