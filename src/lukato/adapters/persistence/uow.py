"""Unidade de trabalho SQLAlchemy: uma transacao sobre os doze repositorios (SPEC-0011 §6).

Contrato com o pacote `lukato.adapters.persistence.repositories`
----------------------------------------------------------------
Os repositorios sao resolvidos por **import tardio**, dentro de `__aenter__`: assim
este modulo (e todo o pacote de persistencia) continua importavel enquanto aquele
pacote ainda nao existe, e o boot da aplicacao nunca quebra por causa dele.

A resolucao aceita, nesta ordem:

1. uma funcao ``build_repositories(session, *, vector_dim)`` exportada pelo pacote,
   devolvendo um mapa ``{"modules": ..., "prompts": ..., ...}``;
2. as classes canonicas ``SqlAlchemy<Agregado>Repository`` exportadas pelo pacote
   (por exemplo `SqlAlchemyModuleRepository`), instanciadas com a `AsyncSession` e,
   quando o construtor aceitar, com `vector_dim`.

Faltando o pacote ou algum repositorio, levanta-se `ConfigurationError` com a lista
dos nomes procurados — nunca um `ImportError` cru.
"""

from __future__ import annotations

import inspect
from types import ModuleType
from typing import Any, Final, Self

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lukato.config import get_logger
from lukato.domain.errors import ConfigurationError, ConflictError, ProviderError
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

__all__ = ["REPOSITORY_ATTRS", "SqlAlchemyUnitOfWork", "UnitOfWorkFactoryImpl"]

_logger = get_logger(__name__)

_REPOSITORIES_MODULE: Final[str] = "lukato.adapters.persistence.repositories"
_FACTORY_NAME: Final[str] = "build_repositories"

REPOSITORY_ATTRS: Final[tuple[str, ...]] = (
    "modules",
    "prompts",
    "guardrails",
    "runs",
    "usage",
    "budgets",
    "documents",
    "users",
    "api_keys",
    "commercials",
    "media",
    "detections",
)
"""Nomes dos doze repositorios exigidos pela porta `UnitOfWork` (SPEC-0000 §7.7)."""

_CANDIDATES: Final[dict[str, tuple[str, ...]]] = {
    "modules": ("SqlAlchemyModuleRepository", "SQLAlchemyModuleRepository", "ModuleRepositoryImpl"),
    "prompts": ("SqlAlchemyPromptRepository", "SQLAlchemyPromptRepository", "PromptRepositoryImpl"),
    "guardrails": (
        "SqlAlchemyGuardrailRepository",
        "SQLAlchemyGuardrailRepository",
        "GuardrailRepositoryImpl",
    ),
    "runs": ("SqlAlchemyRunRepository", "SQLAlchemyRunRepository", "RunRepositoryImpl"),
    "usage": ("SqlAlchemyUsageRepository", "SQLAlchemyUsageRepository", "UsageRepositoryImpl"),
    "budgets": ("SqlAlchemyBudgetRepository", "SQLAlchemyBudgetRepository", "BudgetRepositoryImpl"),
    "documents": (
        "SqlAlchemyDocumentRepository",
        "SQLAlchemyDocumentRepository",
        "DocumentRepositoryImpl",
    ),
    "users": ("SqlAlchemyUserRepository", "SQLAlchemyUserRepository", "UserRepositoryImpl"),
    "api_keys": (
        "SqlAlchemyApiKeyRepository",
        "SQLAlchemyApiKeyRepository",
        "ApiKeyRepositoryImpl",
    ),
    "commercials": (
        "SqlAlchemyCommercialRepository",
        "SQLAlchemyCommercialRepository",
        "CommercialRepositoryImpl",
    ),
    "media": ("SqlAlchemyMediaRepository", "SQLAlchemyMediaRepository", "MediaRepositoryImpl"),
    "detections": (
        "SqlAlchemyDetectionRepository",
        "SQLAlchemyDetectionRepository",
        "DetectionRepositoryImpl",
    ),
}


def _load_repositories_module() -> ModuleType:
    """Importa o pacote de repositorios sob demanda, traduzindo a ausencia dele."""
    import importlib

    try:
        return importlib.import_module(_REPOSITORIES_MODULE)
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            f"pacote de repositorios indisponivel: {_REPOSITORIES_MODULE}",
            details={"module": _REPOSITORIES_MODULE, "error": str(exc)},
        ) from exc


def _instantiate(factory: Any, session: AsyncSession, vector_dim: int) -> Any:
    """Instancia o repositorio passando `vector_dim` apenas quando ele o aceita."""
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # builtins e objetos sem assinatura introspectavel
        parameters = {}
    if "vector_dim" in parameters:
        return factory(session, vector_dim=vector_dim)
    return factory(session)


def _build_repositories(session: AsyncSession, vector_dim: int) -> dict[str, Any]:
    """Resolve os doze repositorios concretos a partir do pacote de repositorios."""
    module = _load_repositories_module()

    factory = getattr(module, _FACTORY_NAME, None)
    if callable(factory):
        built = factory(session, vector_dim=vector_dim)
        missing = [name for name in REPOSITORY_ATTRS if name not in built]
        if missing:
            raise ConfigurationError(
                f"{_FACTORY_NAME}() nao devolveu todos os repositorios",
                details={"missing": missing},
            )
        return {name: built[name] for name in REPOSITORY_ATTRS}

    resolved: dict[str, Any] = {}
    for attribute, candidates in _CANDIDATES.items():
        cls = next((getattr(module, name) for name in candidates if hasattr(module, name)), None)
        if cls is None:
            raise ConfigurationError(
                f"repositorio '{attribute}' nao encontrado em {_REPOSITORIES_MODULE}",
                details={"attribute": attribute, "expected": list(candidates)},
            )
        resolved[attribute] = _instantiate(cls, session, vector_dim)
    return resolved


class SqlAlchemyUnitOfWork:
    """Transacao unica sobre os doze repositorios; implementa a porta `UnitOfWork`."""

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

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        vector_dim: int,
    ) -> None:
        """Guarda a fabrica de sessoes; nada e aberto ate entrar no contexto."""
        self._session_factory = session_factory
        self._vector_dim = vector_dim
        self._session: AsyncSession | None = None

    def __getattr__(self, name: str) -> Any:
        """Mensagem clara ao acessar um repositorio fora do contexto `async with`."""
        if name in REPOSITORY_ATTRS:
            raise ConfigurationError(
                f"repositorio '{name}' indisponivel: use a unidade de trabalho "
                "dentro de 'async with uow_factory() as uow:'"
            )
        raise AttributeError(name)

    @property
    def session(self) -> AsyncSession:
        """Sessao ativa da transacao; exige que o contexto esteja aberto."""
        if self._session is None:
            raise ConfigurationError(
                "unidade de trabalho fechada: abra com 'async with uow_factory() as uow:'"
            )
        return self._session

    @property
    def vector_dim(self) -> int:
        """Dimensionalidade de embedding usada pelos repositorios vetoriais."""
        return self._vector_dim

    async def __aenter__(self) -> Self:
        """Abre a `AsyncSession` e instancia os doze repositorios (import tardio)."""
        session = self._session_factory()
        self._session = session
        try:
            for attribute, repository in _build_repositories(session, self._vector_dim).items():
                object.__setattr__(self, attribute, repository)
        except BaseException:
            self._session = None
            await session.close()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Desfaz a transacao em caso de excecao e sempre encerra a sessao."""
        try:
            if exc and exc[0] is not None:
                await self.rollback()
        finally:
            await self._close()

    async def commit(self) -> None:
        """Confirma as escritas; traduz erros do driver para a hierarquia do dominio."""
        session = self.session
        try:
            await session.commit()
        except IntegrityError as exc:
            await self.rollback()
            raise ConflictError(
                "violacao de restricao de integridade ao confirmar a transacao",
                details={"error": str(exc.orig)},
            ) from exc
        except SQLAlchemyError as exc:
            await self.rollback()
            raise ProviderError(
                f"falha ao confirmar a transacao: {exc}",
                details={"error": type(exc).__name__},
            ) from exc

    async def rollback(self) -> None:
        """Descarta as escritas pendentes; sessao ja fechada e no-op."""
        if self._session is None:
            return
        try:
            await self._session.rollback()
        except SQLAlchemyError as exc:
            _logger.warning("unit_of_work_rollback_failed", error=str(exc))

    async def _close(self) -> None:
        """Encerra a sessao e solta as referencias aos repositorios."""
        session, self._session = self._session, None
        for attribute in REPOSITORY_ATTRS:
            self.__dict__.pop(attribute, None)
        if session is not None:
            try:
                await session.close()
            except SQLAlchemyError as exc:
                _logger.warning("unit_of_work_close_failed", error=str(exc))


class UnitOfWorkFactoryImpl:
    """Fabrica de unidades de trabalho; implementa a porta `UnitOfWorkFactory`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        vector_dim: int,
    ) -> None:
        """Guarda a fabrica de sessoes e a dimensionalidade dos embeddings."""
        self._session_factory = session_factory
        self._vector_dim = vector_dim

    def __call__(self) -> SqlAlchemyUnitOfWork:
        """Cria uma nova unidade de trabalho, ainda nao aberta."""
        return SqlAlchemyUnitOfWork(self._session_factory, vector_dim=self._vector_dim)

    @property
    def vector_dim(self) -> int:
        """Dimensionalidade de embedding propagada as unidades criadas."""
        return self._vector_dim
