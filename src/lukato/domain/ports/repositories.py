"""Portas de persistencia: um repositorio por agregado do dominio.

Todos os repositorios devolvem **modelos de dominio**, nunca objetos de ORM, e
nenhum deles conhece transacao: o controle fica com o `UnitOfWork`.

Os protocolos expoem um metodo chamado `list`, o que apaga o tipo embutido de
mesmo nome dentro do corpo da classe; por isso as anotacoes de retorno usam
`builtins.list`.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

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
from lukato.domain.models.knowledge import Chunk, Document
from lukato.domain.models.module import ModuleDefinition, ModuleKind, ModuleStatus
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.models.run import AgentRun, RunStatus, RunStep
from lukato.domain.types import Id

__all__ = [
    "ApiKeyRepository",
    "BudgetRepository",
    "CommercialRepository",
    "DetectionRepository",
    "DocumentRepository",
    "GuardrailRepository",
    "MediaRepository",
    "ModuleRepository",
    "PromptRepository",
    "RunRepository",
    "UsageRepository",
    "UserRepository",
]


class ModuleRepository(Protocol):
    """Catalogo persistido das definicoes de building blocks."""

    async def add(self, module: ModuleDefinition) -> ModuleDefinition:
        """Insere a definicao; slug duplicado gera `ConflictError`."""
        ...

    async def get(self, module_id: Id) -> ModuleDefinition | None:
        """Busca por identificador."""
        ...

    async def get_by_slug(self, slug: str) -> ModuleDefinition | None:
        """Busca pelo slug unico do modulo."""
        ...

    async def list(
        self,
        *,
        kind: ModuleKind | None = None,
        status: ModuleStatus | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[ModuleDefinition]:
        """Lista definicoes aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta definicoes com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, module: ModuleDefinition) -> ModuleDefinition:
        """Grava a definicao existente; ausente gera `NotFoundError`."""
        ...

    async def delete(self, module_id: Id) -> None:
        """Remove a definicao pelo identificador."""
        ...


class PromptRepository(Protocol):
    """Biblioteca de prompts versionados (`slug` + `version` sao unicos)."""

    async def add(self, prompt: PromptTemplate) -> PromptTemplate:
        """Insere uma versao de prompt."""
        ...

    async def get(self, prompt_id: Id) -> PromptTemplate | None:
        """Busca por identificador."""
        ...

    async def get_by_slug(self, slug: str) -> PromptTemplate | None:
        """Devolve a versao ativa mais recente do slug."""
        ...

    async def list(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[PromptTemplate]:
        """Lista prompts aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta prompts com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, prompt: PromptTemplate) -> PromptTemplate:
        """Grava a versao existente do prompt."""
        ...

    async def delete(self, prompt_id: Id) -> None:
        """Remove uma versao de prompt."""
        ...

    async def list_versions(self, slug: str) -> builtins.list[PromptTemplate]:
        """Lista todas as versoes do slug, da mais recente para a mais antiga."""
        ...


class GuardrailRepository(Protocol):
    """Politicas de guardrail de entrada e de saida."""

    async def add(self, policy: GuardrailPolicy) -> GuardrailPolicy:
        """Insere a politica; slug duplicado gera `ConflictError`."""
        ...

    async def get(self, policy_id: Id) -> GuardrailPolicy | None:
        """Busca por identificador."""
        ...

    async def get_by_slug(self, slug: str) -> GuardrailPolicy | None:
        """Busca pelo slug unico da politica."""
        ...

    async def list(
        self,
        *,
        stage: GuardrailStage | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[GuardrailPolicy]:
        """Lista politicas aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta politicas com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, policy: GuardrailPolicy) -> GuardrailPolicy:
        """Grava a politica existente."""
        ...

    async def delete(self, policy_id: Id) -> None:
        """Remove a politica pelo identificador."""
        ...


class RunRepository(Protocol):
    """Historico de execucoes de modulos e seus passos."""

    async def add(self, run: AgentRun) -> AgentRun:
        """Insere a execucao (normalmente ainda em `RUNNING`)."""
        ...

    async def get(self, run_id: Id) -> AgentRun | None:
        """Busca a execucao, com os passos ja carregados."""
        ...

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
        ...

    async def count(self, **filters: Any) -> int:
        """Conta execucoes com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, run: AgentRun) -> AgentRun:
        """Grava o estado final da execucao."""
        ...

    async def add_step(self, step: RunStep) -> RunStep:
        """Anexa um passo a execucao correspondente."""
        ...

    async def list_steps(self, run_id: Id) -> builtins.list[RunStep]:
        """Lista os passos da execucao em ordem de indice."""
        ...


class UsageRepository(Protocol):
    """Registros de consumo faturavel gerados pelas execucoes."""

    async def add(self, record: UsageRecord) -> UsageRecord:
        """Insere um registro de consumo."""
        ...

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
        """Lista registros de consumo do mais recente para o mais antigo."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta registros com os mesmos filtros aceitos por `list`."""
        ...

    async def summary(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        module_slug: str | None = None,
        tenant_id: str | None = None,
    ) -> CostSummary:
        """Agrega custo e tokens do periodo, por modulo e por modelo."""
        ...

    async def total_since(self, since: datetime, *, scope: str = "global") -> float:
        """Custo total em USD desde o instante, no escopo (`global`, `module:<slug>`, `tenant:<id>`)."""
        ...


class BudgetRepository(Protocol):
    """Orcamentos de custo por escopo."""

    async def add(self, budget: Budget) -> Budget:
        """Insere o orcamento."""
        ...

    async def get(self, budget_id: Id) -> Budget | None:
        """Busca por identificador."""
        ...

    async def list(
        self, *, scope: str | None = None, is_active: bool | None = None
    ) -> builtins.list[Budget]:
        """Lista orcamentos aplicando os filtros informados."""
        ...

    async def update(self, budget: Budget) -> Budget:
        """Grava o orcamento existente."""
        ...

    async def delete(self, budget_id: Id) -> None:
        """Remove o orcamento pelo identificador."""
        ...


class DocumentRepository(Protocol):
    """Documentos da base de conhecimento e seus chunks."""

    async def add(self, document: Document) -> Document:
        """Insere o documento."""
        ...

    async def get(self, document_id: Id) -> Document | None:
        """Busca por identificador."""
        ...

    async def list(
        self,
        *,
        collection: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Document]:
        """Lista documentos aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta documentos com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, document: Document) -> Document:
        """Grava o documento existente."""
        ...

    async def delete(self, document_id: Id) -> None:
        """Remove o documento e, em cascata, seus chunks."""
        ...

    async def add_chunks(self, chunks: Sequence[Chunk]) -> int:
        """Insere os chunks informados; devolve quantos foram gravados."""
        ...

    async def list_chunks(self, document_id: Id) -> builtins.list[Chunk]:
        """Lista os chunks do documento em ordem de indice."""
        ...

    async def delete_chunks(self, document_id: Id) -> int:
        """Remove os chunks do documento; devolve quantos foram apagados."""
        ...

    async def collections(self) -> builtins.list[str]:
        """Lista as colecoes distintas existentes."""
        ...


class UserRepository(Protocol):
    """Usuarios autenticaveis da plataforma."""

    async def add(self, user: User) -> User:
        """Insere o usuario; e-mail duplicado gera `ConflictError`."""
        ...

    async def get(self, user_id: Id) -> User | None:
        """Busca por identificador."""
        ...

    async def get_by_email(self, email: str) -> User | None:
        """Busca pelo e-mail unico."""
        ...

    async def list(self, *, limit: int = 50, offset: int = 0) -> builtins.list[User]:
        """Lista usuarios paginados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta usuarios com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, user: User) -> User:
        """Grava o usuario existente."""
        ...

    async def delete(self, user_id: Id) -> None:
        """Remove o usuario pelo identificador."""
        ...


class ApiKeyRepository(Protocol):
    """Chaves de API (apenas prefixo e hash do segredo sao persistidos)."""

    async def add(self, api_key: ApiKey) -> ApiKey:
        """Insere a chave; prefixo duplicado gera `ConflictError`."""
        ...

    async def get(self, api_key_id: Id) -> ApiKey | None:
        """Busca por identificador."""
        ...

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        """Busca pelo prefixo unico apresentado na requisicao."""
        ...

    async def list(
        self, *, is_active: bool | None = None, limit: int = 50, offset: int = 0
    ) -> builtins.list[ApiKey]:
        """Lista chaves aplicando os filtros informados."""
        ...

    async def update(self, api_key: ApiKey) -> ApiKey:
        """Grava a chave existente."""
        ...

    async def delete(self, api_key_id: Id) -> None:
        """Remove a chave pelo identificador."""
        ...

    async def touch(self, api_key_id: Id, when: datetime) -> None:
        """Registra o instante do ultimo uso da chave."""
        ...


class CommercialRepository(Protocol):
    """Catalogo de comerciais do AdWatch e suas assinaturas de matching."""

    async def add(self, commercial: Commercial) -> Commercial:
        """Insere o comercial; codigo de negocio duplicado gera `ConflictError`."""
        ...

    async def get(self, commercial_id: Id) -> Commercial | None:
        """Busca por identificador interno."""
        ...

    async def get_by_code(self, code: str) -> Commercial | None:
        """Busca pelo codigo de negocio (`Commercial.commercial_id`)."""
        ...

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
        """Lista comerciais aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta comerciais com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, commercial: Commercial) -> Commercial:
        """Grava o comercial existente."""
        ...

    async def delete(self, commercial_id: Id) -> None:
        """Remove o comercial e, em cascata, sua assinatura."""
        ...

    async def all_active(self) -> builtins.list[Commercial]:
        """Devolve todos os comerciais ativos (usado para montar o indice de matching)."""
        ...

    async def upsert_fingerprint(self, fp: AdFingerprint) -> AdFingerprint:
        """Grava a assinatura do comercial, substituindo a anterior se existir."""
        ...

    async def get_fingerprint(self, commercial_id: Id) -> AdFingerprint | None:
        """Busca a assinatura de um comercial."""
        ...

    async def list_fingerprints(self) -> builtins.list[AdFingerprint]:
        """Lista todas as assinaturas disponiveis."""
        ...


class MediaRepository(Protocol):
    """Ativos de midia e os artefatos derivados da ingestao."""

    async def add(self, asset: MediaAsset) -> MediaAsset:
        """Registra o ativo de midia."""
        ...

    async def get(self, media_id: Id) -> MediaAsset | None:
        """Busca por identificador."""
        ...

    async def list(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[MediaAsset]:
        """Lista ativos aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta ativos com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, asset: MediaAsset) -> MediaAsset:
        """Grava o ativo existente (duracao, fps, status)."""
        ...

    async def delete(self, media_id: Id) -> None:
        """Remove o ativo e, em cascata, transcricao, cenas, OCR e deteccoes."""
        ...

    async def save_transcript(self, transcript: Transcript) -> Transcript:
        """Grava a transcricao do ativo, substituindo a anterior se existir."""
        ...

    async def get_transcript(self, media_id: Id) -> Transcript | None:
        """Busca a transcricao do ativo."""
        ...

    async def save_scenes(self, media_id: Id, scenes: Sequence[SceneCut]) -> int:
        """Substitui os cortes de cena do ativo; devolve quantos foram gravados."""
        ...

    async def list_scenes(self, media_id: Id) -> builtins.list[SceneCut]:
        """Lista os cortes de cena em ordem temporal."""
        ...

    async def save_ocr(self, media_id: Id, texts: Sequence[OcrText]) -> int:
        """Substitui os textos de OCR do ativo; devolve quantos foram gravados."""
        ...

    async def list_ocr(self, media_id: Id) -> builtins.list[OcrText]:
        """Lista os textos de OCR em ordem temporal."""
        ...


class DetectionRepository(Protocol):
    """Deteccoes consolidadas de comerciais dentro dos ativos de midia."""

    async def add(self, detection: Detection) -> Detection:
        """Insere uma deteccao."""
        ...

    async def add_many(self, detections: Sequence[Detection]) -> builtins.list[Detection]:
        """Insere varias deteccoes de uma vez, preservando a ordem."""
        ...

    async def get(self, detection_id: Id) -> Detection | None:
        """Busca por identificador."""
        ...

    async def list(
        self,
        *,
        media_id: Id | None = None,
        commercial_id: Id | None = None,
        status: DetectionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Detection]:
        """Lista deteccoes aplicando os filtros informados."""
        ...

    async def count(self, **filters: Any) -> int:
        """Conta deteccoes com os mesmos filtros aceitos por `list`."""
        ...

    async def update(self, detection: Detection) -> Detection:
        """Grava a deteccao existente (revisao humana ou veredito do VLM)."""
        ...

    async def delete(self, detection_id: Id) -> None:
        """Remove a deteccao pelo identificador."""
        ...

    async def delete_by_media(self, media_id: Id) -> int:
        """Remove todas as deteccoes do ativo; devolve quantas foram apagadas."""
        ...
