"""Testes de unidade do compositor da trinca do modulo (SPEC-0003 secao 1).

`ModuleComposer` transforma o `ModuleBinding` (que so guarda identificadores) no
`ComposedPipeline` que o caso de uso `InvokeModule` executa: guardrail de entrada
-> system prompt -> guardrail de saida. Os repositorios chegam por injecao, entao
aqui usamos os dubles em memoria de `tests.fakes` — nada de banco.

Duas garantias sao o coracao do arquivo: um identificador vinculado que nao
existe precisa dizer **qual campo do binding** quebrou, e uma politica vinculada
no estagio errado precisa ser recusada antes de qualquer chamada de LLM.
"""

from __future__ import annotations

import pytest

from lukato.domain.errors import NotFoundError, ValidationError
from lukato.domain.models.guardrail import GuardrailStage
from lukato.domain.models.module import ModuleDefinition
from lukato.domain.models.prompt import PromptTemplate
from lukato.domain.services.module_composer import ComposedPipeline, ModuleComposer
from tests.factories import id_de, make_binding, make_module, make_policy, make_prompt
from tests.fakes import FakeGuardrailRepository, FakePromptRepository

pytestmark = pytest.mark.unit

MODELO_PADRAO = "modelo-do-composer"
TEMPERATURA_PADRAO = 0.2
MAX_TOKENS_PADRAO = 2048


def _composer() -> ModuleComposer:
    """Compositor com os defaults conhecidos usados em todas as asercoes."""
    return ModuleComposer(
        default_model=MODELO_PADRAO,
        default_temperature=TEMPERATURA_PADRAO,
        default_max_tokens=MAX_TOKENS_PADRAO,
    )


async def _compor(
    definicao: ModuleDefinition,
    *,
    prompts: FakePromptRepository | None = None,
    guardrails: FakeGuardrailRepository | None = None,
) -> ComposedPipeline:
    """Compoe a definicao com repositorios vazios por padrao."""
    return await _composer().compose(
        definicao,
        prompts=prompts or FakePromptRepository(),
        guardrails=guardrails or FakeGuardrailRepository(),
    )


# --------------------------------------------------------------------------- #
# Binding vazio: defaults do composer
# --------------------------------------------------------------------------- #
async def test_binding_vazio_resolve_a_trinca_como_ausente() -> None:
    """Modulo sem politica e sem prompt e legitimo (SPEC-0003 criterio 1)."""
    pipeline = await _compor(make_module(binding=make_binding()))

    assert pipeline.input_policy is None
    assert pipeline.output_policy is None
    assert pipeline.prompt is None


async def test_binding_vazio_usa_o_modelo_a_temperatura_e_o_max_tokens_padrao() -> None:
    """Sem sobrescrita no binding, valem os defaults vindos de `Settings`."""
    pipeline = await _compor(make_module(binding=make_binding()))

    assert pipeline.model == MODELO_PADRAO
    assert pipeline.temperature == TEMPERATURA_PADRAO
    assert pipeline.max_tokens == MAX_TOKENS_PADRAO


async def test_binding_vazio_mantem_timeout_padrao_e_lista_de_ferramentas_vazia() -> None:
    """O `timeout_seconds` tem default no proprio binding; `tools` nasce vazio."""
    pipeline = await _compor(make_module(binding=make_binding()))

    assert pipeline.timeout_seconds == 60.0
    assert pipeline.tools == []


async def test_binding_preenchido_sobrescreve_cada_default() -> None:
    """O que o binding declara vence o default do composer, campo a campo."""
    binding = make_binding(
        model="modelo-do-modulo",
        temperature=1.5,
        max_tokens=256,
        timeout_seconds=12.5,
        tools=["busca", "calculadora"],
    )

    pipeline = await _compor(make_module(binding=binding))

    assert pipeline.model == "modelo-do-modulo"
    assert pipeline.temperature == 1.5
    assert pipeline.max_tokens == 256
    assert pipeline.timeout_seconds == 12.5
    assert pipeline.tools == ["busca", "calculadora"]


async def test_temperatura_zero_do_binding_nao_e_confundida_com_ausencia() -> None:
    """`0.0` e um valor deliberado (saida deterministica), nao "nao informado"."""
    pipeline = await _compor(make_module(binding=make_binding(temperature=0.0)))

    assert pipeline.temperature == 0.0


async def test_lista_de_ferramentas_do_pipeline_e_uma_copia_do_binding() -> None:
    """Mutar o pipeline nao pode alterar a definicao persistida do modulo."""
    definicao = make_module(binding=make_binding(tools=["busca"]))

    pipeline = await _compor(definicao)
    pipeline.tools.append("intruso")

    assert definicao.binding.tools == ["busca"]


async def test_pipeline_carrega_a_definicao_do_modulo() -> None:
    """O `InvokeModule` precisa da definicao junto com a trinca resolvida."""
    definicao = make_module(slug="assistente")

    pipeline = await _compor(definicao)

    assert pipeline.definition is definicao


# --------------------------------------------------------------------------- #
# Trinca completa resolvida
# --------------------------------------------------------------------------- #
async def test_compose_resolve_as_duas_politicas_e_o_prompt() -> None:
    """O caminho feliz: entrada, prompt e saida vinculados e encontrados."""
    entrada = make_policy(slug="entrada-padrao", stage=GuardrailStage.INPUT)
    saida = make_policy(slug="saida-padrao", stage=GuardrailStage.OUTPUT)
    prompt = make_prompt(slug="system-assistente")
    guardrails = FakeGuardrailRepository()
    prompts = FakePromptRepository()
    await guardrails.add(entrada)
    await guardrails.add(saida)
    await prompts.add(prompt)
    definicao = make_module(
        binding=make_binding(
            input_guardrail_id=entrada.id,
            output_guardrail_id=saida.id,
            system_prompt_id=prompt.id,
        )
    )

    pipeline = await _compor(definicao, prompts=prompts, guardrails=guardrails)

    assert pipeline.input_policy is not None
    assert pipeline.input_policy.slug == "entrada-padrao"
    assert pipeline.output_policy is not None
    assert pipeline.output_policy.slug == "saida-padrao"
    assert pipeline.prompt is not None
    assert pipeline.prompt.slug == "system-assistente"


async def test_compose_aceita_apenas_o_guardrail_de_saida_vinculado() -> None:
    """A trinca e parametrizavel peca a peca: so a saida tambem e configuracao valida."""
    saida = make_policy(slug="saida-padrao", stage=GuardrailStage.OUTPUT)
    guardrails = FakeGuardrailRepository()
    await guardrails.add(saida)
    definicao = make_module(binding=make_binding(output_guardrail_id=saida.id))

    pipeline = await _compor(definicao, guardrails=guardrails)

    assert pipeline.input_policy is None
    assert pipeline.output_policy is not None
    assert pipeline.prompt is None


# --------------------------------------------------------------------------- #
# Identificador vinculado inexistente
# --------------------------------------------------------------------------- #
async def test_guardrail_de_entrada_inexistente_diz_qual_binding_quebrou() -> None:
    """O erro tem de nomear o campo, senao o operador nao sabe o que corrigir."""
    definicao = make_module(
        slug="assistente", binding=make_binding(input_guardrail_id=id_de("politica", "sumiu"))
    )

    with pytest.raises(NotFoundError) as excecao:
        await _compor(definicao)

    detalhes = excecao.value.details
    assert detalhes["binding_field"] == "input_guardrail_id"
    assert detalhes["module_slug"] == "assistente"
    assert detalhes["policy_id"] == id_de("politica", "sumiu")
    assert "binding.input_guardrail_id" in excecao.value.message
    assert excecao.value.http_status == 404


async def test_guardrail_de_saida_inexistente_aponta_o_campo_de_saida() -> None:
    """O mesmo erro precisa distinguir entrada de saida."""
    definicao = make_module(binding=make_binding(output_guardrail_id=id_de("politica", "sumiu")))

    with pytest.raises(NotFoundError) as excecao:
        await _compor(definicao)

    assert excecao.value.details["binding_field"] == "output_guardrail_id"


async def test_prompt_inexistente_aponta_o_campo_do_system_prompt() -> None:
    """Prompt vinculado e apagado deixa o modulo inconsistente e precisa falhar cedo."""
    definicao = make_module(
        slug="assistente", binding=make_binding(system_prompt_id=id_de("prompt", "sumiu"))
    )

    with pytest.raises(NotFoundError) as excecao:
        await _compor(definicao)

    detalhes = excecao.value.details
    assert detalhes["binding_field"] == "system_prompt_id"
    assert detalhes["prompt_id"] == id_de("prompt", "sumiu")
    assert "binding.system_prompt_id" in excecao.value.message


async def test_guardrail_de_entrada_e_verificado_antes_do_prompt() -> None:
    """Com dois vinculos quebrados, o erro relatado e o do primeiro passo da trinca."""
    definicao = make_module(
        binding=make_binding(
            input_guardrail_id=id_de("politica", "sumiu"),
            system_prompt_id=id_de("prompt", "sumiu"),
        )
    )

    with pytest.raises(NotFoundError) as excecao:
        await _compor(definicao)

    assert excecao.value.details["binding_field"] == "input_guardrail_id"


# --------------------------------------------------------------------------- #
# Politica no estagio errado
# --------------------------------------------------------------------------- #
async def test_politica_de_saida_vinculada_na_entrada_e_recusada() -> None:
    """Uma politica de saida no slot de entrada silenciaria o guardrail de entrada."""
    saida = make_policy(slug="saida-padrao", stage=GuardrailStage.OUTPUT)
    guardrails = FakeGuardrailRepository()
    await guardrails.add(saida)
    definicao = make_module(slug="assistente", binding=make_binding(input_guardrail_id=saida.id))

    with pytest.raises(ValidationError) as excecao:
        await _compor(definicao, guardrails=guardrails)

    detalhes = excecao.value.details
    assert detalhes["binding_field"] == "input_guardrail_id"
    assert detalhes["expected_stage"] == "input"
    assert detalhes["actual_stage"] == "output"
    assert detalhes["policy_slug"] == "saida-padrao"
    assert excecao.value.http_status == 422


async def test_politica_de_entrada_vinculada_na_saida_e_recusada() -> None:
    """A verificacao vale nos dois sentidos."""
    entrada = make_policy(slug="entrada-padrao", stage=GuardrailStage.INPUT)
    guardrails = FakeGuardrailRepository()
    await guardrails.add(entrada)
    definicao = make_module(binding=make_binding(output_guardrail_id=entrada.id))

    with pytest.raises(ValidationError) as excecao:
        await _compor(definicao, guardrails=guardrails)

    assert excecao.value.details["expected_stage"] == "output"
    assert excecao.value.details["actual_stage"] == "input"


async def test_politica_inativa_no_estagio_certo_e_aceita_pelo_composer() -> None:
    """Desligar a politica e decisao do motor de guardrail, nao do compositor."""
    entrada = make_policy(slug="entrada-desligada", stage=GuardrailStage.INPUT, is_active=False)
    guardrails = FakeGuardrailRepository()
    await guardrails.add(entrada)
    definicao = make_module(binding=make_binding(input_guardrail_id=entrada.id))

    pipeline = await _compor(definicao, guardrails=guardrails)

    assert pipeline.input_policy is not None
    assert pipeline.input_policy.is_active is False


# --------------------------------------------------------------------------- #
# render_system_prompt
# --------------------------------------------------------------------------- #
async def test_render_system_prompt_sem_prompt_vinculado_devolve_texto_vazio() -> None:
    """Modulo sem system prompt roda mesmo assim, com prompt vazio."""
    pipeline = await _compor(make_module(binding=make_binding()))

    assert pipeline.render_system_prompt({"qualquer": "coisa"}) == ""


async def test_render_system_prompt_com_prompt_vinculado_substitui_as_variaveis() -> None:
    """O prompt resolvido e renderizado com as variaveis da requisicao."""
    prompt = make_prompt(
        slug="system-assistente", template="Voce atende a marca {{ marca }} em {{ idioma }}."
    )
    prompts = FakePromptRepository()
    await prompts.add(prompt)
    definicao = make_module(binding=make_binding(system_prompt_id=prompt.id))

    pipeline = await _compor(definicao, prompts=prompts)

    assert pipeline.render_system_prompt({"marca": "Claro", "idioma": "portugues"}) == (
        "Voce atende a marca Claro em portugues."
    )


async def test_render_system_prompt_sem_variaveis_no_template_ignora_o_dicionario() -> None:
    """Template estatico nao exige variavel nenhuma."""
    prompt = make_prompt(slug="estatico", template="Voce e objetivo.")
    prompts = FakePromptRepository()
    await prompts.add(prompt)
    definicao = make_module(binding=make_binding(system_prompt_id=prompt.id))

    pipeline = await _compor(definicao, prompts=prompts)

    assert pipeline.render_system_prompt({}) == "Voce e objetivo."


async def test_render_system_prompt_com_variavel_ausente_levanta_validation_error() -> None:
    """A falha vem de `PromptTemplate.render`, com `details['missing']` preenchido."""
    prompt = make_prompt(slug="com-variavel", template="Ola {{ nome }}")
    prompts = FakePromptRepository()
    await prompts.add(prompt)
    definicao = make_module(binding=make_binding(system_prompt_id=prompt.id))
    pipeline = await _compor(definicao, prompts=prompts)

    with pytest.raises(ValidationError) as excecao:
        pipeline.render_system_prompt({})

    assert excecao.value.details["missing"] == ["nome"]


# --------------------------------------------------------------------------- #
# Configuracao do proprio composer
# --------------------------------------------------------------------------- #
def test_composer_expoe_os_defaults_recebidos() -> None:
    """As propriedades publicas espelham o que o composition root injetou."""
    composer = _composer()

    assert composer.default_model == MODELO_PADRAO
    assert composer.default_temperature == TEMPERATURA_PADRAO
    assert composer.default_max_tokens == MAX_TOKENS_PADRAO


async def test_composer_nao_altera_a_definicao_recebida() -> None:
    """Compor e uma leitura: a `ModuleDefinition` sai como entrou."""
    definicao = make_module(binding=make_binding(model="modelo-do-modulo"))
    antes = definicao.model_dump_json()

    await _compor(definicao)

    assert definicao.model_dump_json() == antes


async def test_prompt_resolvido_e_o_objeto_completo_do_repositorio() -> None:
    """O pipeline entrega o `PromptTemplate`, nao apenas o texto do template."""
    prompt = make_prompt(slug="system-assistente", template="Ola {{ nome }}")
    prompts = FakePromptRepository()
    await prompts.add(prompt)
    definicao = make_module(binding=make_binding(system_prompt_id=prompt.id))

    pipeline = await _compor(definicao, prompts=prompts)

    assert isinstance(pipeline.prompt, PromptTemplate)
    assert pipeline.prompt.variables == ["nome"]
