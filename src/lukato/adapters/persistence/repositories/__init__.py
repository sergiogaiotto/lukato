"""Repositorios SQLAlchemy do lukato: um arquivo por agregado (SPEC-0011 secao 2).

Este pacote reexporta as **doze** implementacoes concretas exigidas pela porta
:class:`~lukato.domain.ports.unit_of_work.UnitOfWork`. E por estes nomes que
`SqlAlchemyUnitOfWork.__aenter__` resolve os repositorios, via import tardio.

Todas as classes compartilham o mesmo contrato de construcao —
``__init__(self, session: AsyncSession)`` — nao conhecem transacao (o commit pertence
a unidade de trabalho) e devolvem exclusivamente modelos de dominio, nunca linhas ORM.
"""

from __future__ import annotations

from lukato.adapters.persistence.repositories.api_keys import SqlAlchemyApiKeyRepository
from lukato.adapters.persistence.repositories.budgets import SqlAlchemyBudgetRepository
from lukato.adapters.persistence.repositories.commercials import SqlAlchemyCommercialRepository
from lukato.adapters.persistence.repositories.detections import SqlAlchemyDetectionRepository
from lukato.adapters.persistence.repositories.documents import SqlAlchemyDocumentRepository
from lukato.adapters.persistence.repositories.guardrails import SqlAlchemyGuardrailRepository
from lukato.adapters.persistence.repositories.media import SqlAlchemyMediaRepository
from lukato.adapters.persistence.repositories.modules import SqlAlchemyModuleRepository
from lukato.adapters.persistence.repositories.prompts import SqlAlchemyPromptRepository
from lukato.adapters.persistence.repositories.runs import SqlAlchemyRunRepository
from lukato.adapters.persistence.repositories.usage import SqlAlchemyUsageRepository
from lukato.adapters.persistence.repositories.users import SqlAlchemyUserRepository

__all__ = [
    "SqlAlchemyApiKeyRepository",
    "SqlAlchemyBudgetRepository",
    "SqlAlchemyCommercialRepository",
    "SqlAlchemyDetectionRepository",
    "SqlAlchemyDocumentRepository",
    "SqlAlchemyGuardrailRepository",
    "SqlAlchemyMediaRepository",
    "SqlAlchemyModuleRepository",
    "SqlAlchemyPromptRepository",
    "SqlAlchemyRunRepository",
    "SqlAlchemyUsageRepository",
    "SqlAlchemyUserRepository",
]
