"""Testes de unidade dos modelos de dominio (SPEC-0000 secao 6).

Cobre o comportamento normativo que os modelos carregam alem da declaracao de
campos: renderizacao de `PromptTemplate`, aritmetica de `TokenUsage`, as
propriedades derivadas de `GuardrailVerdict`, o RBAC de `Principal.can`, o
recorte temporal de `Transcript.window`, as faixas de validacao de
`ModuleBinding`/`Detection`, o formato do slug de modulo e a serializacao
ida-e-volta (`model_dump_json` / `model_validate_json`) de todos os agregados.

Tudo e deterministico: os objetos vem de `tests.factories`, cujos identificadores
sao UUIDv5 derivados da chave natural e cujos carimbos saem da data fixa `AGORA`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from lukato.domain.errors import ValidationError
from lukato.domain.models.adwatch import (
    AdFingerprint,
    Commercial,
    Detection,
    DetectionCandidate,
    DetectionEvidence,
    DetectionStatus,
    MediaAsset,
    OcrText,
    SceneCut,
    Transcript,
    TranscriptWord,
)
from lukato.domain.models.finops import Budget, CostSummary, ModelPrice, UsageRecord
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailFinding,
    GuardrailPolicy,
    GuardrailRuleKind,
    GuardrailSeverity,
    GuardrailStage,
    GuardrailVerdict,
)
from lukato.domain.models.identity import (
    ApiKey,
    Permission,
    Principal,
    Role,
    User,
    permissions_for,
)
from lukato.domain.models.knowledge import Chunk, Document, SearchHit
from lukato.domain.models.module import ModuleBinding, ModuleDefinition
from lukato.domain.models.prompt import PromptRole, PromptTemplate, extract_variables
from lukato.domain.models.run import AgentRun, RunStep, TokenUsage
from lukato.domain.types import DEFAULT_TENANT
from tests.factories import (
    AGORA,
    id_de,
    make_api_key,
    make_binding,
    make_budget,
    make_candidate,
    make_chunk,
    make_commercial,
    make_detection,
    make_document,
    make_evidence,
    make_fingerprint,
    make_media,
    make_module,
    make_ocr,
    make_policy,
    make_price,
    make_principal,
    make_prompt,
    make_run,
    make_scenes,
    make_step,
    make_transcript,
    make_usage_record,
    make_user,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# PromptTemplate.render (SPEC-0000 secao 6.2)
# --------------------------------------------------------------------------- #
def test_render_substitui_placeholders_com_e_sem_espaco_interno() -> None:
    """`{{ var }}` e `{{var}}` sao a mesma variavel e ambos sao substituidos."""
    prompt = make_prompt(template="Ola {{ nome }}, voce fala {{idioma}}.")

    assert prompt.render({"nome": "Sergio", "idioma": "portugues"}) == (
        "Ola Sergio, voce fala portugues."
    )


def test_render_converte_valores_nao_textuais_para_texto() -> None:
    """Numeros e booleanos viram texto sem exigir conversao do chamador."""
    prompt = make_prompt(template="limite={{ limite }} ativo={{ ativo }}")

    assert prompt.render({"limite": 42, "ativo": True}) == "limite=42 ativo=True"


def test_render_repete_a_mesma_variavel_em_todas_as_ocorrencias() -> None:
    """Uma variavel usada duas vezes e substituida nas duas posicoes."""
    prompt = make_prompt(template="{{ marca }} e sempre {{ marca }}")

    assert prompt.render({"marca": "lukato"}) == "lukato e sempre lukato"


def test_render_sem_variavel_ausente_lista_as_faltantes_em_details_missing() -> None:
    """Variavel exigida e nao informada vira `ValidationError` com `details['missing']`."""
    prompt = make_prompt(slug="saudacao", template="{{ nome }} de {{ cidade }} em {{ ano }}")

    with pytest.raises(ValidationError) as excecao:
        prompt.render({"cidade": "Sao Paulo"})

    detalhes = excecao.value.details
    assert detalhes["missing"] == ["nome", "ano"], (
        "details['missing'] deve listar apenas as variaveis faltantes, na ordem do template"
    )
    assert detalhes["slug"] == "saudacao"
    assert detalhes["required"] == ["nome", "cidade", "ano"]


def test_render_de_template_sem_variaveis_devolve_o_texto_intacto() -> None:
    """Template sem placeholder nao depende de variaveis e passa incolume."""
    prompt = make_prompt(template="Voce e um assistente objetivo.")

    assert prompt.variables == []
    assert prompt.render({}) == "Voce e um assistente objetivo."
    assert prompt.render({"sobrando": "ignorado"}) == "Voce e um assistente objetivo."


def test_render_nao_reexpande_valor_que_contem_chaves_duplas() -> None:
    """A substituicao e de passada unica: `{{ ... }}` vindo do valor fica literal."""
    prompt = make_prompt(template="Instrucao: {{ trecho }}")

    renderizado = prompt.render({"trecho": "{{ nome }}", "nome": "NAO DEVE APARECER"})

    assert renderizado == "Instrucao: {{ nome }}", (
        "o valor injetado nao pode ser reinterpretado como placeholder (injecao de template)"
    )


def test_variables_e_preenchido_a_partir_do_template_quando_omitido() -> None:
    """`variables` vazio e autopreenchido, sem repeticao e na ordem de aparicao."""
    prompt = PromptTemplate(
        slug="autofill",
        name="Autofill",
        template="{{ b }} {{ a }} {{ b }}",
    )

    assert prompt.variables == ["b", "a"]


def test_variables_informado_explicitamente_e_preservado() -> None:
    """Lista declarada pelo autor do prompt nao e sobrescrita pelo autofill."""
    prompt = PromptTemplate(
        slug="explicito",
        name="Explicito",
        template="{{ a }}",
        variables=["a", "documentada-mas-nao-usada"],
    )

    assert prompt.variables == ["a", "documentada-mas-nao-usada"]


def test_extract_variables_ignora_placeholder_com_nome_invalido() -> None:
    """Somente identificadores Python valem como variavel de template."""
    assert extract_variables("{{ ok }} {{ 1invalido }} {{ com-hifen }}") == ["ok"]


def test_prompt_com_papel_padrao_e_system() -> None:
    """O papel default do template e `system`, o usado pela trinca do modulo."""
    assert PromptTemplate(slug="p", name="P", template="x").role is PromptRole.SYSTEM


# --------------------------------------------------------------------------- #
# TokenUsage (SPEC-0000 secao 6.4)
# --------------------------------------------------------------------------- #
def test_token_usage_preenche_o_total_quando_nao_informado() -> None:
    """`total_tokens` ausente e calculado como prompt + completion."""
    uso = TokenUsage(prompt_tokens=1000, completion_tokens=500)

    assert uso.total_tokens == 1500


def test_token_usage_respeita_o_total_informado_pelo_provedor() -> None:
    """Total explicito do provedor nao e recalculado (pode incluir tokens de cache)."""
    uso = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=99)

    assert uso.total_tokens == 99


def test_token_usage_zerado_mantem_total_zero() -> None:
    """Consumo vazio continua zerado e nao vira `None` nem erro."""
    assert TokenUsage().total_tokens == 0


def test_token_usage_soma_campo_a_campo() -> None:
    """`__add__` soma prompt, completion e total separadamente."""
    soma = TokenUsage.of(10, 5) + TokenUsage.of(1, 2)

    assert (soma.prompt_tokens, soma.completion_tokens, soma.total_tokens) == (11, 7, 18)


def test_token_usage_soma_nao_muda_as_parcelas() -> None:
    """A soma devolve um novo objeto: os operandos permanecem intactos."""
    esquerda = TokenUsage.of(10, 5)
    direita = TokenUsage.of(1, 2)

    resultado = esquerda + direita

    assert esquerda == TokenUsage.of(10, 5)
    assert direita == TokenUsage.of(1, 2)
    assert resultado is not esquerda


def test_token_usage_of_soma_o_total_das_parcelas() -> None:
    """O construtor de conveniencia `of` nunca deixa o total inconsistente."""
    assert TokenUsage.of(3, 4).total_tokens == 7


# --------------------------------------------------------------------------- #
# GuardrailVerdict (SPEC-0000 secao 6.3)
# --------------------------------------------------------------------------- #
def _verdict(*, allowed: bool, content: str, original: str) -> GuardrailVerdict:
    """Monta um veredito minimo para exercitar as propriedades derivadas."""
    return GuardrailVerdict(
        allowed=allowed,
        stage=GuardrailStage.INPUT,
        content=content,
        original_content=original,
    )


def test_verdict_blocked_e_o_inverso_de_allowed() -> None:
    """`blocked` existe para o codigo ler a negativa sem inverter o booleano na mao."""
    assert _verdict(allowed=False, content="x", original="x").blocked is True
    assert _verdict(allowed=True, content="x", original="x").blocked is False


def test_verdict_modified_detecta_conteudo_redigido() -> None:
    """`modified` compara o conteudo final com o original."""
    redigido = _verdict(allowed=True, content="CPF [REDIGIDO]", original="CPF 529.982.247-25")

    assert redigido.modified is True


def test_verdict_modified_e_falso_quando_o_conteudo_passa_intacto() -> None:
    """Sem redacao nem transformacao, `modified` continua falso."""
    assert _verdict(allowed=True, content="igual", original="igual").modified is False


def test_verdict_permissivo_nasce_sem_findings_e_sem_politica() -> None:
    """O veredito permissivo padrao nao inventa achados nem `policy_id`."""
    veredito = _verdict(allowed=True, content="ola", original="ola")

    assert veredito.findings == []
    assert veredito.policy_id is None
    assert veredito.latency_ms == 0.0


# --------------------------------------------------------------------------- #
# Principal.can (SPEC-0000 secao 6.7)
# --------------------------------------------------------------------------- #
def _principal_do_papel(role: Role) -> Principal:
    """Principal com exatamente as permissoes que `ROLE_PERMISSIONS` da ao papel."""
    return Principal(subject=f"sujeito-{role.value}", role=role, permissions=permissions_for(role))


@pytest.mark.parametrize("papel", [Role.ROOT, Role.ADMIN])
def test_can_root_e_admin_podem_tudo(papel: Role) -> None:
    """ROOT e ADMIN recebem o conjunto completo de permissoes."""
    principal = _principal_do_papel(papel)

    assert all(principal.can(permissao) for permissao in Permission), (
        f"o papel {papel.value} deveria poder executar todas as permissoes"
    )


def test_can_operator_le_tudo_invoca_modulo_e_escreve_conhecimento() -> None:
    """OPERATOR opera: le tudo, invoca modulos e alimenta a base de conhecimento."""
    operador = _principal_do_papel(Role.OPERATOR)

    assert operador.can(Permission.MODULE_READ)
    assert operador.can(Permission.RUN_READ)
    assert operador.can(Permission.MODULE_INVOKE)
    assert operador.can(Permission.KNOWLEDGE_WRITE)


def test_can_operator_nao_escreve_modulo_prompt_guardrail_nem_finops() -> None:
    """OPERATOR nao configura a plataforma: escrita administrativa continua negada."""
    operador = _principal_do_papel(Role.OPERATOR)

    negadas = [
        Permission.MODULE_WRITE,
        Permission.PROMPT_WRITE,
        Permission.GUARDRAIL_WRITE,
        Permission.FINOPS_WRITE,
        Permission.ADMIN_ALL,
    ]
    assert not any(operador.can(permissao) for permissao in negadas), (
        "OPERATOR nao pode receber permissao de escrita administrativa"
    )


def test_can_viewer_somente_le() -> None:
    """VIEWER tem apenas as permissoes terminadas em `:read`."""
    leitor = _principal_do_papel(Role.VIEWER)

    permitidas = {permissao for permissao in Permission if leitor.can(permissao)}
    assert permitidas == {
        permissao for permissao in Permission if permissao.value.endswith(":read")
    }


def test_can_viewer_nao_invoca_modulo() -> None:
    """Invocar modulo custa dinheiro: VIEWER nao pode."""
    assert _principal_do_papel(Role.VIEWER).can(Permission.MODULE_INVOKE) is False


def test_can_reconhece_o_coringa_admin_all_sozinho() -> None:
    """`admin:*` concede qualquer permissao, mesmo sem ela estar no conjunto."""
    coringa = Principal(
        subject="coringa", role=Role.ADMIN, permissions=frozenset({Permission.ADMIN_ALL})
    )

    assert coringa.can(Permission.FINOPS_WRITE) is True


def test_can_de_principal_sem_permissao_nenhuma_e_sempre_falso() -> None:
    """Principal vazio nao ganha nada implicitamente pelo papel."""
    vazio = Principal(subject="ninguem", role=Role.ROOT, permissions=frozenset())

    assert not any(vazio.can(permissao) for permissao in Permission)


def test_anonymous_root_recebe_todas_as_permissoes_no_tenant_informado() -> None:
    """Com autenticacao desligada em dev, o principal anonimo age como root."""
    anonimo = Principal.anonymous_root(tenant_id="acme")

    assert anonimo.kind == "anonymous"
    assert anonimo.role is Role.ROOT
    assert anonimo.tenant_id == "acme"
    assert all(anonimo.can(permissao) for permissao in Permission)


# --------------------------------------------------------------------------- #
# Transcript.window (SPEC-0000 secao 6.8)
# --------------------------------------------------------------------------- #
def _transcricao_de_quatro_palavras() -> Transcript:
    """`a[0,1] b[1,2] c[2,3] d[3,4]` — carimbos regulares para conferir na mao."""
    return make_transcript([("a b c d", 0.0, 4.0)])


def test_window_seleciona_apenas_as_palavras_que_intersectam_a_janela() -> None:
    """A janela `[1.2, 2.8]` pega `b` e `c` e descarta `a` e `d`."""
    recorte = _transcricao_de_quatro_palavras().window(1.2, 2.8)

    assert [palavra.word for palavra in recorte.words] == ["b", "c"]


def test_window_inclui_a_palavra_que_apenas_encosta_na_borda() -> None:
    """Intersecao e inclusiva nas duas pontas: `a` termina em 1.0 e `c` comeca em 2.0."""
    recorte = _transcricao_de_quatro_palavras().window(1.0, 2.0)

    assert [palavra.word for palavra in recorte.words] == ["a", "b", "c"]


def test_window_fora_do_intervalo_devolve_transcricao_vazia() -> None:
    """Janela depois do fim da fala nao devolve palavra alguma nem levanta erro."""
    recorte = _transcricao_de_quatro_palavras().window(10.0, 20.0)

    assert recorte.words == []
    assert recorte.text == ""


def test_window_preserva_identidade_midia_idioma_e_origem() -> None:
    """O recorte continua sendo a mesma transcricao, apenas com menos palavras."""
    original = make_transcript([("a b c d", 0.0, 4.0)], language="pt", source="whisperx")

    recorte = original.window(0.0, 1.5)

    assert recorte.id == original.id
    assert recorte.media_id == original.media_id
    assert recorte.language == "pt"
    assert recorte.source == "whisperx"


def test_window_nao_altera_a_transcricao_original() -> None:
    """`window` e uma projecao: a lista de palavras de origem continua completa."""
    original = _transcricao_de_quatro_palavras()

    original.window(0.0, 0.5)

    assert len(original.words) == 4


def test_text_junta_as_palavras_por_espaco() -> None:
    """`text` reconstroi a fala corrida para os comparadores lexicais."""
    assert _transcricao_de_quatro_palavras().text == "a b c d"


# --------------------------------------------------------------------------- #
# Faixas de validacao: ModuleBinding e Detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("temperatura", [-0.1, 2.1])
def test_binding_recusa_temperatura_fora_de_0_a_2(temperatura: float) -> None:
    """`temperature` vive em `0.0..2.0` (SPEC-0000 secao 6.1)."""
    with pytest.raises(PydanticValidationError):
        ModuleBinding(temperature=temperatura)


@pytest.mark.parametrize("temperatura", [0.0, 1.0, 2.0])
def test_binding_aceita_temperatura_nas_bordas_e_no_meio(temperatura: float) -> None:
    """As bordas 0.0 e 2.0 sao validas."""
    assert ModuleBinding(temperature=temperatura).temperature == temperatura


@pytest.mark.parametrize("maximo", [0, -1, 8193])
def test_binding_recusa_max_tokens_fora_de_1_a_8192(maximo: int) -> None:
    """`max_tokens` vive em `1..8192` (SPEC-0000 secao 6.1)."""
    with pytest.raises(PydanticValidationError):
        ModuleBinding(max_tokens=maximo)


@pytest.mark.parametrize("maximo", [1, 8192])
def test_binding_aceita_max_tokens_nas_bordas(maximo: int) -> None:
    """As bordas 1 e 8192 sao validas."""
    assert ModuleBinding(max_tokens=maximo).max_tokens == maximo


def test_binding_vazio_nasce_sem_trinca_e_com_timeout_padrao() -> None:
    """Modulo sem politica nem prompt e legitimo; o timeout padrao e 60 s."""
    binding = ModuleBinding()

    assert binding.input_guardrail_id is None
    assert binding.system_prompt_id is None
    assert binding.output_guardrail_id is None
    assert binding.model is None
    assert binding.timeout_seconds == 60.0
    assert binding.tools == []


def test_binding_recusa_campo_desconhecido() -> None:
    """`extra="forbid"`: um campo digitado errado nao passa silenciosamente."""
    with pytest.raises(PydanticValidationError):
        ModuleBinding(temperatura=0.5)


@pytest.mark.parametrize("confianca", [-0.01, 1.01])
def test_detection_recusa_confianca_fora_de_0_a_1(confianca: float) -> None:
    """`confidence` e uma fracao: `0.0..1.0`."""
    with pytest.raises(PydanticValidationError):
        make_detection(confidence=confianca)


def test_detection_recusa_inicio_negativo() -> None:
    """Nao existe deteccao antes do inicio da midia."""
    with pytest.raises(PydanticValidationError):
        make_detection(start=-1.0, end=5.0)


def test_detection_recusa_intervalo_invertido() -> None:
    """`end < start` e intervalo impossivel e precisa falhar na construcao."""
    with pytest.raises(PydanticValidationError) as excecao:
        make_detection(start=40.0, end=10.0)

    assert "0 <= start <= end" in str(excecao.value)


def test_detection_aceita_intervalo_de_duracao_zero() -> None:
    """`start == end` e degenerado, porem valido (marcador pontual)."""
    assert make_detection(start=7.0, end=7.0).end == 7.0


@pytest.mark.parametrize("score", [-0.01, 1.5])
def test_candidate_recusa_score_fora_de_0_a_1(score: float) -> None:
    """O score fundido do candidato tambem e uma fracao."""
    with pytest.raises(PydanticValidationError):
        make_candidate(score=score)


def test_candidate_recusa_intervalo_invertido() -> None:
    """A janela candidata segue a mesma regra de intervalo da deteccao."""
    with pytest.raises(PydanticValidationError):
        make_candidate(start=40.0, end=10.0)


def test_budget_recusa_alert_threshold_fora_de_0_a_1() -> None:
    """O limiar de alerta do orcamento e uma fracao do limite."""
    with pytest.raises(PydanticValidationError):
        make_budget(alert_threshold=1.5)


# --------------------------------------------------------------------------- #
# Slug de modulo (SPEC-0000 secao 6.1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a",
        "Modulo",
        "modulo invalido",
        "modulo_invalido",
        "-comeca-com-hifen",
        "acentuacao-invalida-caoç",
        "m" * 64,
    ],
)
def test_module_definition_recusa_slug_invalido(slug: str) -> None:
    """O slug e `a-z0-9-`, comeca por alfanumerico e tem de 2 a 63 caracteres."""
    with pytest.raises(PydanticValidationError):
        ModuleDefinition(slug=slug, name="Qualquer")


@pytest.mark.parametrize("slug", ["ab", "modulo-teste", "adwatch2", "m" * 63])
def test_module_definition_aceita_slug_canonico(slug: str) -> None:
    """Slugs em minusculas com hifens e digitos sao aceitos."""
    assert ModuleDefinition(slug=slug, name="Qualquer").slug == slug


def test_module_definition_recusa_campo_desconhecido() -> None:
    """Entidade de dominio nao aceita campo extra (`extra="forbid"`)."""
    with pytest.raises(PydanticValidationError):
        ModuleDefinition(slug="modulo", name="Modulo", inexistente=1)


def test_module_definition_tem_defaults_da_spec() -> None:
    """Modulo novo nasce `agent`/`draft`/`langgraph` com binding vazio."""
    definicao = ModuleDefinition(slug="novo-modulo", name="Novo")

    assert definicao.kind.value == "agent"
    assert definicao.status.value == "draft"
    assert definicao.runtime == "langgraph"
    assert definicao.binding == ModuleBinding()
    assert definicao.version == "1.0.0"


def test_entity_touch_atualiza_updated_at_sem_mexer_no_created_at() -> None:
    """`touch()` carimba a alteracao preservando a data de criacao."""
    modulo = make_module()
    criado_em = modulo.created_at

    modulo.touch()

    assert modulo.created_at == criado_em
    assert modulo.updated_at > AGORA


# --------------------------------------------------------------------------- #
# Serializacao ida-e-volta de todos os agregados
# --------------------------------------------------------------------------- #
def _todos_os_agregados() -> list[BaseModel]:
    """Uma instancia povoada de cada agregado normativo da SPEC-0000 secao 6."""
    id_run = id_de("execucao", "serializacao")
    id_documento = id_de("documento", "serializacao")
    id_comercial = id_de("comercial", "COM_000001")
    return [
        make_binding(model="qwen-latest", temperature=0.7, max_tokens=256, tools=["busca"]),
        make_module(binding=make_binding(model="qwen-latest"), tags=["a", "b"]),
        make_prompt(template="Ola {{ nome }}"),
        make_policy(),
        make_run(steps=[make_step(id_run, usage=TokenUsage.of(10, 5))]),
        make_step(id_run),
        make_usage_record(),
        make_budget(),
        make_price(),
        CostSummary(
            total_usd=1.5, total_tokens=30, runs=2, by_module={"a": 1.0}, by_model={"m": 1.5}
        ),
        make_document(),
        make_chunk(id_documento, embedding=[0.1, 0.2]),
        SearchHit(
            chunk_id=id_de("chunk", id_documento, 0),
            document_id=id_documento,
            collection="agente_evidence",
            content="trecho",
            score=0.87,
            metadata={"origem": "teste"},
        ),
        make_user(),
        make_api_key(),
        make_principal(),
        make_commercial(),
        make_fingerprint(id_comercial, embedding=[0.3, 0.4]),
        make_media(),
        make_transcript([("o melhor plano", 1.0, 4.0)]),
        make_scenes([(0.0, 5.0), (5.0, 9.0)])[0],
        make_ocr([("claro", 1.0, 2.0)])[0],
        OcrText(text="claro", start=1.0, end=2.0, bbox=(1, 2, 3, 4)),
        make_evidence(speech_match=0.9, brand_detected="Claro", matched_text="plano"),
        make_candidate(),
        make_detection(),
        GuardrailFinding(
            rule_id="pii",
            kind=GuardrailRuleKind.PII_REDACT,
            action=GuardrailAction.REDACT,
            severity=GuardrailSeverity.HIGH,
            message="CPF redigido",
            evidence="CPF [REDIGIDO]",
            span=(4, 18),
        ),
        GuardrailVerdict(
            allowed=False,
            stage=GuardrailStage.OUTPUT,
            content="[bloqueado]",
            original_content="segredo sk-abc",
            findings=[],
            policy_id=id_de("politica", "saida-padrao"),
            latency_ms=3.25,
        ),
    ]


@pytest.mark.parametrize(
    "agregado", _todos_os_agregados(), ids=lambda item: type(item).__name__.lower()
)
def test_agregado_sobrevive_ao_ciclo_model_dump_json_model_validate_json(
    agregado: BaseModel,
) -> None:
    """Serializar e desserializar devolve um objeto igual ao original."""
    reconstruido = type(agregado).model_validate_json(agregado.model_dump_json())

    assert reconstruido == agregado, (
        f"{type(agregado).__name__} nao sobreviveu ao ciclo de serializacao JSON"
    )


def test_serializacao_preserva_o_tipo_dos_campos_derivados() -> None:
    """Tupla, frozenset e datetime voltam do JSON com o tipo declarado, nao como lista."""
    achado = GuardrailFinding(
        rule_id="pii",
        kind=GuardrailRuleKind.PII_REDACT,
        action=GuardrailAction.REDACT,
        severity=GuardrailSeverity.HIGH,
        message="m",
        span=(4, 18),
    )
    principal = make_principal(permissions=[Permission.MODULE_READ])
    modulo = make_module()

    achado_de_volta = GuardrailFinding.model_validate_json(achado.model_dump_json())
    principal_de_volta = Principal.model_validate_json(principal.model_dump_json())
    modulo_de_volta = ModuleDefinition.model_validate_json(modulo.model_dump_json())

    assert achado_de_volta.span == (4, 18)
    assert isinstance(achado_de_volta.span, tuple)
    assert principal_de_volta.permissions == frozenset({Permission.MODULE_READ})
    assert modulo_de_volta.created_at == AGORA


def test_serializacao_de_enums_usa_o_valor_textual_da_spec() -> None:
    """O JSON carrega os literais da SPEC (`agent`, `active`), nao o nome do membro."""
    dados: dict[str, Any] = make_module().model_dump(mode="json")

    assert dados["kind"] == "agent"
    assert dados["status"] == "active"


@pytest.mark.parametrize(
    "modelo",
    [
        ModuleDefinition,
        PromptTemplate,
        GuardrailPolicy,
        GuardrailVerdict,
        AgentRun,
        RunStep,
        TokenUsage,
        UsageRecord,
        Budget,
        ModelPrice,
        CostSummary,
        Document,
        Chunk,
        SearchHit,
        User,
        ApiKey,
        Principal,
        Commercial,
        AdFingerprint,
        MediaAsset,
        Transcript,
        TranscriptWord,
        SceneCut,
        OcrText,
        DetectionEvidence,
        DetectionCandidate,
        Detection,
    ],
)
def test_todo_modelo_de_dominio_proibe_campo_extra(modelo: type[BaseModel]) -> None:
    """SPEC-0000 secao 6: todo modelo usa `ConfigDict(extra="forbid")`."""
    assert modelo.model_config.get("extra") == "forbid", (
        f"{modelo.__name__} precisa recusar campos extras"
    )


def test_valores_padrao_de_tenant_seguem_a_constante_do_dominio() -> None:
    """`tenant_id` padrao vem de `DEFAULT_TENANT`, nunca de um literal solto."""
    assert make_run().tenant_id == DEFAULT_TENANT
    assert make_user().tenant_id == DEFAULT_TENANT
    assert make_usage_record().tenant_id == DEFAULT_TENANT
    assert Principal(subject="s", role=Role.VIEWER).tenant_id == DEFAULT_TENANT


def test_detection_status_cobre_os_tres_desfechos_da_fusao() -> None:
    """Os limiares da SPEC-0000 secao 8 tem exatamente tres desfechos."""
    assert {estado.value for estado in DetectionStatus} == {
        "accepted",
        "needs_review",
        "rejected",
    }
