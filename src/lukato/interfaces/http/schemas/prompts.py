"""Schemas do recurso `/api/v1/prompts`: biblioteca versionada de system prompts.

Um prompt e identificado pelo par `slug` + `version`. Alterar o texto **cria uma
nova versao**; os demais campos alteram a versao vigente. O preview renderiza sem
levantar erro: variavel faltando e informacao para o editor, nao falha.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from lukato.application.dto import UNSET, Maybe
from lukato.application.use_cases.prompts import PromptCreateInput, PromptUpdateInput
from lukato.domain.models.prompt import PromptRole, PromptTemplate
from lukato.domain.types import Id, Json
from lukato.interfaces.http.schemas.common import InSchema, OutSchema

__all__ = [
    "PromptCloneRequest",
    "PromptCreate",
    "PromptOut",
    "PromptPreviewRequest",
    "PromptPreviewResponse",
    "PromptUpdate",
]


class PromptCreate(InSchema):
    """Corpo de `POST /api/v1/prompts`: cria a primeira versao de um slug."""

    slug: str = Field(min_length=1, description="Identificador estavel do prompt.")
    name: str = Field(default="", description="Nome exibido no console.")
    description: str = Field(default="", description="Para que serve este prompt.")
    role: PromptRole = Field(default=PromptRole.SYSTEM, description="Papel da mensagem gerada.")
    template: str = Field(description="Texto com placeholders no formato `{{ variavel }}`.")
    variables: list[str] | None = Field(
        default=None, description="Variaveis declaradas; ausente deduz do proprio texto."
    )
    labels: list[str] = Field(default_factory=list, description="Etiquetas de organizacao.")
    is_active: bool = Field(default=True, description="Versao ativa da biblioteca.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "slug": "atendimento-base",
                "name": "Atendimento — base",
                "description": "Tom institucional para o canal {{ canal }}.",
                "role": "system",
                "template": "Voce atende clientes pelo canal {{ canal }}. Responda em {{ idioma }}.",
                "labels": ["suporte"],
                "is_active": True,
            }
        },
    )

    def to_input(self) -> PromptCreateInput:
        """Converte para o DTO do caso de uso `CreatePrompt`."""
        return PromptCreateInput(
            slug=self.slug,
            name=self.name,
            description=self.description,
            role=self.role,
            template=self.template,
            variables=list(self.variables) if self.variables is not None else None,
            labels=list(self.labels),
            is_active=self.is_active,
        )


class PromptUpdate(InSchema):
    """Corpo de `PUT /api/v1/prompts/{ref}`: texto novo gera versao nova."""

    name: str | None = None
    description: str | None = None
    role: PromptRole | None = None
    template: str | None = None
    variables: list[str] | None = None
    labels: list[str] | None = None
    is_active: bool | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "template": "Voce atende clientes pelo canal {{ canal }}. Seja objetivo.",
                "labels": ["suporte", "revisado"],
            }
        },
    )

    def to_input(self) -> PromptUpdateInput:
        """Converte para o DTO parcial, preservando o que nao foi enviado."""
        sent = self.model_fields_set

        def maybe(field: str, value: Any) -> Maybe[Any]:
            return value if field in sent else UNSET

        return PromptUpdateInput(
            name=maybe("name", self.name),
            description=maybe("description", self.description),
            role=maybe("role", self.role),
            template=maybe("template", self.template),
            variables=maybe(
                "variables", list(self.variables) if self.variables is not None else None
            ),
            labels=maybe("labels", list(self.labels) if self.labels is not None else None),
            is_active=maybe("is_active", self.is_active),
        )


class PromptOut(OutSchema):
    """Prompt devolvido pela API."""

    id: Id
    slug: str
    name: str
    description: str = ""
    role: PromptRole = PromptRole.SYSTEM
    template: str
    variables: list[str] = Field(default_factory=list)
    version: int = 1
    is_active: bool = True
    labels: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, prompt: PromptTemplate) -> PromptOut:
        """Converte a entidade de dominio."""
        return cls(
            id=prompt.id,
            slug=prompt.slug,
            name=prompt.name,
            description=prompt.description,
            role=prompt.role,
            template=prompt.template,
            variables=list(prompt.variables),
            version=prompt.version,
            is_active=prompt.is_active,
            labels=list(prompt.labels),
            created_at=prompt.created_at,
            updated_at=prompt.updated_at,
        )


class PromptPreviewRequest(InSchema):
    """Corpo de `POST /api/v1/prompts/{ref}/preview`.

    Com `template` preenchido, nada e lido do repositorio: e o modo de
    pre-visualizar um rascunho ainda nao salvo.
    """

    variables: Json = Field(default_factory=dict, description="Valores das variaveis do texto.")
    template: str | None = Field(
        default=None, description="Rascunho avulso a renderizar no lugar do prompt salvo."
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"variables": {"canal": "app", "idioma": "portugues"}}},
    )


class PromptPreviewResponse(OutSchema):
    """Resultado do preview: texto renderizado e o que ainda falta preencher."""

    slug: str = Field(description="Slug do prompt renderizado.")
    version: int = Field(default=1, description="Versao usada na renderizacao.")
    role: PromptRole = Field(default=PromptRole.SYSTEM, description="Papel da mensagem.")
    rendered: str = Field(description="Texto final; lacunas ficam como `{{ variavel }}`.")
    missing: list[str] = Field(default_factory=list, description="Variaveis nao informadas.")
    variables: list[str] = Field(default_factory=list, description="Variaveis exigidas pelo texto.")
    unused: list[str] = Field(default_factory=list, description="Valores enviados sem uso.")
    complete: bool = Field(default=False, description="True quando nada ficou faltando.")
    persisted: bool = Field(default=True, description="False quando renderizou um rascunho.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "slug": "atendimento-base",
                "version": 3,
                "role": "system",
                "rendered": "Voce atende clientes pelo canal app. Responda em {{ idioma }}.",
                "missing": ["idioma"],
                "variables": ["canal", "idioma"],
                "unused": [],
                "complete": False,
                "persisted": True,
            }
        }
    )

    @classmethod
    def from_result(cls, result: Json) -> PromptPreviewResponse:
        """Converte o mapa devolvido pelo caso de uso `PreviewPrompt`."""
        return cls(
            slug=str(result.get("slug", "")),
            version=int(result.get("version", 1)),
            role=PromptRole(result.get("role", PromptRole.SYSTEM.value)),
            rendered=str(result.get("rendered", "")),
            missing=[str(item) for item in result.get("missing") or []],
            variables=[str(item) for item in result.get("variables") or []],
            unused=[str(item) for item in result.get("unused") or []],
            complete=bool(result.get("complete", False)),
            persisted=bool(result.get("persisted", True)),
        )


class PromptCloneRequest(InSchema):
    """Corpo de `POST /api/v1/prompts/{ref}/clone`.

    Sem `target_slug` o clone vira a proxima versao do mesmo slug — a forma de
    retomar a edicao a partir de uma versao antiga.
    """

    target_slug: str | None = Field(default=None, description="Slug de destino do clone.")
    name: str | None = Field(default=None, description="Nome do clone; ausente herda a origem.")
    activate: bool = Field(default=True, description="Se o clone ja nasce ativo.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {"target_slug": "atendimento-experimental", "activate": False}
        },
    )
