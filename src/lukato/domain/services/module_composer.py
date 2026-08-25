"""Resolucao da trinca do modulo: guardrail de entrada -> system prompt -> guardrail de saida.

SPEC-0001 (secao 4) e SPEC-0003 (secao 1): nenhum modulo chama um LLM fora da
trinca, e a trinca vive em `ModuleDefinition.binding`. Este servico transforma o
binding (identificadores) em um `ComposedPipeline` (objetos ja resolvidos).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lukato.domain.errors import NotFoundError, ValidationError
from lukato.domain.models.guardrail import GuardrailPolicy, GuardrailStage
from lukato.domain.models.module import ModuleDefinition
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.ports.repositories import GuardrailRepository, PromptRepository
from lukato.domain.types import Id, Json

__all__ = ["ComposedPipeline", "ModuleComposer"]

_INPUT_FIELD = "input_guardrail_id"
_OUTPUT_FIELD = "output_guardrail_id"
_PROMPT_FIELD = "system_prompt_id"


@dataclass(slots=True)
class ComposedPipeline:
    """Trinca resolvida de um modulo, pronta para o caso de uso `InvokeModule`."""

    definition: ModuleDefinition
    input_policy: GuardrailPolicy | None
    output_policy: GuardrailPolicy | None
    prompt: PromptTemplate | None
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    tools: list[str] = field(default_factory=list)

    def render_system_prompt(self, variables: Json) -> str:
        """Renderiza o system prompt; devolve `""` quando o modulo nao vincula prompt.

        Variavel exigida pelo template e nao informada gera `ValidationError`
        (levantada por `PromptTemplate.render`).
        """
        if self.prompt is None:
            return ""
        return self.prompt.render(variables)


class ModuleComposer:
    """Monta o `ComposedPipeline` de uma `ModuleDefinition` consultando os repositorios."""

    def __init__(
        self, *, default_model: str, default_temperature: float, default_max_tokens: int
    ) -> None:
        self._default_model = default_model
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens

    @property
    def default_model(self) -> str:
        """Modelo usado quando o binding nao define um."""
        return self._default_model

    @property
    def default_temperature(self) -> float:
        """Temperatura usada quando o binding nao define uma."""
        return self._default_temperature

    @property
    def default_max_tokens(self) -> int:
        """Limite de tokens usado quando o binding nao define um."""
        return self._default_max_tokens

    async def compose(
        self,
        definition: ModuleDefinition,
        *,
        prompts: PromptRepository,
        guardrails: GuardrailRepository,
    ) -> ComposedPipeline:
        """Resolve politicas e prompt do binding, aplicando os defaults do composer.

        Identificador vinculado que nao existe gera `NotFoundError` nomeando o
        campo quebrado; politica encontrada no estagio errado gera `ValidationError`.
        """
        binding = definition.binding
        input_policy = await self._load_policy(
            guardrails, definition, binding.input_guardrail_id, GuardrailStage.INPUT, _INPUT_FIELD
        )
        output_policy = await self._load_policy(
            guardrails,
            definition,
            binding.output_guardrail_id,
            GuardrailStage.OUTPUT,
            _OUTPUT_FIELD,
        )
        prompt = await self._load_prompt(prompts, definition, binding.system_prompt_id)

        return ComposedPipeline(
            definition=definition,
            input_policy=input_policy,
            output_policy=output_policy,
            prompt=prompt,
            model=binding.model or self._default_model,
            temperature=(
                self._default_temperature if binding.temperature is None else binding.temperature
            ),
            max_tokens=(
                self._default_max_tokens if binding.max_tokens is None else binding.max_tokens
            ),
            timeout_seconds=binding.timeout_seconds,
            tools=list(binding.tools),
        )

    async def _load_policy(
        self,
        guardrails: GuardrailRepository,
        definition: ModuleDefinition,
        policy_id: Id | None,
        expected_stage: GuardrailStage,
        binding_field: str,
    ) -> GuardrailPolicy | None:
        """Carrega e valida uma politica vinculada; `None` significa estagio sem restricao."""
        if policy_id is None:
            return None
        policy = await guardrails.get(policy_id)
        if policy is None:
            raise NotFoundError(
                f"Politica de guardrail '{policy_id}' vinculada em "
                f"binding.{binding_field} do modulo '{definition.slug}' nao existe.",
                details={
                    "module_id": definition.id,
                    "module_slug": definition.slug,
                    "binding_field": binding_field,
                    "policy_id": policy_id,
                },
            )
        if policy.stage is not expected_stage:
            raise ValidationError(
                f"A politica '{policy.slug}' e do estagio '{policy.stage.value}', mas foi "
                f"vinculada em binding.{binding_field} do modulo '{definition.slug}', que "
                f"exige o estagio '{expected_stage.value}'.",
                details={
                    "module_id": definition.id,
                    "module_slug": definition.slug,
                    "binding_field": binding_field,
                    "policy_id": policy.id,
                    "policy_slug": policy.slug,
                    "expected_stage": expected_stage.value,
                    "actual_stage": policy.stage.value,
                },
            )
        return policy

    async def _load_prompt(
        self, prompts: PromptRepository, definition: ModuleDefinition, prompt_id: Id | None
    ) -> PromptTemplate | None:
        """Carrega o system prompt vinculado; `None` significa modulo sem prompt."""
        if prompt_id is None:
            return None
        prompt = await prompts.get(prompt_id)
        if prompt is None:
            raise NotFoundError(
                f"Prompt '{prompt_id}' vinculado em binding.{_PROMPT_FIELD} do modulo "
                f"'{definition.slug}' nao existe.",
                details={
                    "module_id": definition.id,
                    "module_slug": definition.slug,
                    "binding_field": _PROMPT_FIELD,
                    "prompt_id": prompt_id,
                },
            )
        return prompt
