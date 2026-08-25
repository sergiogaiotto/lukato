"""Porta de unidade de trabalho: agrupa os repositorios numa unica transacao."""

from __future__ import annotations

from typing import Any, Protocol

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

__all__ = ["UnitOfWork", "UnitOfWorkFactory"]


class UnitOfWork(Protocol):
    """Transacao unica sobre os doze repositorios do dominio.

    Sair do contexto sem `commit()` desfaz tudo o que foi escrito.
    """

    modules: ModuleRepository
    prompts: PromptRepository
    guardrails: GuardrailRepository
    runs: RunRepository
    usage: UsageRepository
    budgets: BudgetRepository
    documents: DocumentRepository
    users: UserRepository
    api_keys: ApiKeyRepository
    commercials: CommercialRepository
    media: MediaRepository
    detections: DetectionRepository

    async def __aenter__(self) -> UnitOfWork:
        """Abre a sessao e instancia os repositorios."""
        ...

    async def __aexit__(self, *exc: Any) -> None:
        """Desfaz a transacao em caso de excecao e sempre encerra a sessao."""
        ...

    async def commit(self) -> None:
        """Confirma as escritas pendentes."""
        ...

    async def rollback(self) -> None:
        """Descarta as escritas pendentes."""
        ...


class UnitOfWorkFactory(Protocol):
    """Fabrica de unidades de trabalho, injetada nos casos de uso."""

    def __call__(self) -> UnitOfWork:
        """Cria uma nova unidade de trabalho, ainda nao aberta."""
        ...
