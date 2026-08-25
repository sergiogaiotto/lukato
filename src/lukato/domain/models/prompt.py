"""Modelos de prompts versionados com renderizacao segura de placeholders."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, model_validator

from lukato.domain.errors import ValidationError
from lukato.domain.models.base import Entity
from lukato.domain.types import Json

__all__ = ["PromptRole", "PromptTemplate", "extract_variables"]

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def extract_variables(template: str) -> list[str]:
    """Lista, na ordem de aparicao e sem repeticoes, as variaveis `{{ var }}` do texto."""
    found: dict[str, None] = {}
    for match in _PLACEHOLDER.finditer(template):
        found.setdefault(match.group(1), None)
    return list(found)


class PromptRole(StrEnum):
    """Papel da mensagem gerada pelo template."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    DEVELOPER = "developer"


class PromptTemplate(Entity):
    """Template de prompt versionado; substituicao textual pura, sem exec/eval."""

    slug: str
    name: str
    description: str = ""
    role: PromptRole = PromptRole.SYSTEM
    template: str
    variables: list[str] = Field(default_factory=list)
    version: int = 1
    is_active: bool = True
    labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _autofill_variables(self) -> PromptTemplate:
        """Preenche `variables` a partir do template quando nao informado."""
        if not self.variables:
            self.variables = extract_variables(self.template)
        return self

    def render(self, variables: Json) -> str:
        """Substitui `{{ var }}` / `{{var}}` pelos valores informados.

        Levanta :class:`~lukato.domain.errors.ValidationError` com `details["missing"]`
        quando alguma variavel exigida pelo template nao foi fornecida.
        """
        required = extract_variables(self.template)
        missing = [name for name in required if name not in variables]
        if missing:
            raise ValidationError(
                f"Variaveis ausentes ao renderizar o prompt '{self.slug}': {', '.join(missing)}",
                details={"missing": missing, "slug": self.slug, "required": required},
            )
        return _PLACEHOLDER.sub(lambda match: str(variables[match.group(1)]), self.template)
