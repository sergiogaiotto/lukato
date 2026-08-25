"""Testes de unidade do motor de guardrails (SPEC-0003 secao 2 e criterios de aceite).

O motor e dominio puro: os avaliadores chegam por injecao. Por isso os testes usam
avaliadores programaveis proprios — `AvaliadorEspiao` conta chamadas e guarda o
conteudo que recebeu — em vez dos avaliadores reais dos adaptadores. E o contador
que prova as duas garantias mais importantes da SPEC-0003:

* `BLOCK` **interrompe** a cadeia: a regra seguinte nao chega a ser avaliada;
* `REDACT` **encadeia**: a regra seguinte recebe o texto ja redigido.
"""

from __future__ import annotations

import pytest

from lukato.domain.errors import GuardrailViolation, UnsupportedCapability
from lukato.domain.models.guardrail import (
    GuardrailAction,
    GuardrailFinding,
    GuardrailPolicy,
    GuardrailRule,
    GuardrailRuleKind,
    GuardrailSeverity,
    GuardrailStage,
)
from lukato.domain.ports.guardrail import GuardrailPort
from lukato.domain.services.guardrail_engine import GuardrailEngine
from lukato.domain.types import Json
from tests.factories import make_policy, make_rule

pytestmark = pytest.mark.unit

TEXTO = "conteudo original do usuario"
"""Entrada usada em quase todos os testes; qualquer alteracao dela e visivel."""


# --------------------------------------------------------------------------- #
# Avaliadores programaveis
# --------------------------------------------------------------------------- #
class AvaliadorEspiao:
    """Avaliador de teste: registra cada chamada e devolve o que o teste mandar.

    `resultado` pode ser um `GuardrailFinding`, `None` (nada violado) ou uma
    funcao `(content, rule) -> GuardrailFinding | None` quando o resultado
    depende da regra. `erro`, quando informado, e levantado no lugar da resposta.
    """

    def __init__(
        self,
        kind: GuardrailRuleKind,
        resultado: object = None,
        *,
        erro: Exception | None = None,
    ) -> None:
        self.kind = kind
        self._resultado = resultado
        self._erro = erro
        self.conteudos: list[str] = []
        self.regras: list[str] = []
        self.contextos: list[Json] = []

    @property
    def chamadas(self) -> int:
        """Quantas vezes o motor pediu uma avaliacao a este avaliador."""
        return len(self.conteudos)

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Registra a chamada e devolve o resultado programado pelo teste."""
        self.conteudos.append(content)
        self.regras.append(rule.id)
        self.contextos.append(dict(context))
        if self._erro is not None:
            raise self._erro
        if callable(self._resultado):
            return self._resultado(content, rule)
        return self._resultado


class AvaliadorPesado:
    """Avaliador que consome tempo de CPU de forma deterministica (sem `sleep`)."""

    def __init__(self, kind: GuardrailRuleKind, *, ciclos: int = 400_000) -> None:
        self.kind = kind
        self._ciclos = ciclos

    async def evaluate(
        self, content: str, rule: GuardrailRule, context: Json
    ) -> GuardrailFinding | None:
        """Faz trabalho mensuravel e nao acusa nada."""
        total = 0
        for numero in range(self._ciclos):
            total += numero
        assert total >= 0
        return None


def _achado(rule: GuardrailRule, acao: GuardrailAction, *, evidencia: str = "") -> GuardrailFinding:
    """Monta o achado que um avaliador devolveria para a regra informada."""
    return GuardrailFinding(
        rule_id=rule.id,
        kind=rule.kind,
        action=acao,
        severity=GuardrailSeverity.HIGH,
        message=f"regra {rule.id} disparou",
        evidence=evidencia,
    )


def _regra(
    rule_id: str,
    kind: GuardrailRuleKind,
    *,
    action: GuardrailAction = GuardrailAction.BLOCK,
    order: int = 0,
    enabled: bool = True,
) -> GuardrailRule:
    """Atalho para uma regra sem configuracao (o comportamento vem do avaliador)."""
    return make_rule(rule_id, kind=kind, action=action, order=order, enabled=enabled, config={})


def _politica(
    *regras: GuardrailRule,
    stage: GuardrailStage = GuardrailStage.INPUT,
    fail_open: bool = False,
    is_active: bool = True,
) -> GuardrailPolicy:
    """Politica de teste com as regras informadas, sem depender do seed real."""
    return make_policy(rules=list(regras), stage=stage, fail_open=fail_open, is_active=is_active)


# --------------------------------------------------------------------------- #
# Politica ausente ou inativa: comportamento permissivo
# --------------------------------------------------------------------------- #
async def test_politica_none_deixa_o_conteudo_passar_intacto() -> None:
    """Estagio sem politica e escolha explicita, nunca erro (SPEC-0003 secao 1)."""
    motor = GuardrailEngine([])

    veredito = await motor.apply(TEXTO, None)

    assert veredito.allowed is True
    assert veredito.blocked is False
    assert veredito.content == TEXTO
    assert veredito.original_content == TEXTO
    assert veredito.findings == []
    assert veredito.policy_id is None


async def test_politica_none_nao_consulta_nenhum_avaliador() -> None:
    """Sem politica nao ha regra: nenhum avaliador e acionado."""
    espiao = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)
    motor = GuardrailEngine([espiao])

    await motor.apply(TEXTO, None)

    assert espiao.chamadas == 0


async def test_politica_none_usa_o_estagio_informado_no_contexto() -> None:
    """Sem politica, o estagio do veredito vem do contexto da chamada."""
    motor = GuardrailEngine([])

    veredito = await motor.apply(TEXTO, None, context={"stage": "output"})

    assert veredito.stage is GuardrailStage.OUTPUT


async def test_politica_none_sem_contexto_assume_estagio_de_entrada() -> None:
    """O padrao seguro e tratar o texto como entrada do usuario."""
    motor = GuardrailEngine([])

    assert (await motor.apply(TEXTO, None)).stage is GuardrailStage.INPUT


async def test_politica_inativa_e_tratada_como_permissiva() -> None:
    """`is_active=False` desliga a politica sem precisar desvincula-la do modulo."""
    espiao = AvaliadorEspiao(
        GuardrailRuleKind.KEYWORD_BLOCK,
        lambda conteudo, regra: _achado(regra, GuardrailAction.BLOCK),
    )
    politica = _politica(_regra("bloqueia", GuardrailRuleKind.KEYWORD_BLOCK), is_active=False)
    motor = GuardrailEngine([espiao])

    veredito = await motor.apply(TEXTO, politica)

    assert veredito.allowed is True
    assert espiao.chamadas == 0
    assert veredito.policy_id == politica.id


async def test_motor_desligado_deixa_tudo_passar() -> None:
    """`LUKATO_GUARDRAILS__ENABLED=false` e a chave geral de emergencia."""
    espiao = AvaliadorEspiao(
        GuardrailRuleKind.KEYWORD_BLOCK,
        lambda conteudo, regra: _achado(regra, GuardrailAction.BLOCK),
    )
    motor = GuardrailEngine([espiao], enabled=False)

    veredito = await motor.apply(TEXTO, _politica(_regra("b", GuardrailRuleKind.KEYWORD_BLOCK)))

    assert veredito.allowed is True
    assert veredito.content == TEXTO
    assert espiao.chamadas == 0


# --------------------------------------------------------------------------- #
# Ordem de avaliacao
# --------------------------------------------------------------------------- #
async def test_regras_sao_avaliadas_na_ordem_de_order_e_depois_de_id() -> None:
    """Determinismo exige criterio total: `(order, id)` (SPEC-0003 secao 2)."""
    espiao = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)
    politica = _politica(
        _regra("zulu", GuardrailRuleKind.KEYWORD_BLOCK, order=10),
        _regra("bravo", GuardrailRuleKind.KEYWORD_BLOCK, order=1),
        _regra("alfa", GuardrailRuleKind.KEYWORD_BLOCK, order=1),
        _regra("charlie", GuardrailRuleKind.KEYWORD_BLOCK, order=0),
    )
    motor = GuardrailEngine([espiao])

    await motor.apply(TEXTO, politica)

    assert espiao.regras == ["charlie", "alfa", "bravo", "zulu"], (
        "empate em `order` e desempatado pelo `id`, nunca pela ordem da lista"
    )


async def test_ordem_independe_da_ordem_de_declaracao_das_regras() -> None:
    """A mesma politica declarada ao contrario produz a mesma sequencia."""
    espiao_direto = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)
    espiao_invertido = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)
    regras = [
        _regra("a", GuardrailRuleKind.KEYWORD_BLOCK, order=0),
        _regra("b", GuardrailRuleKind.KEYWORD_BLOCK, order=1),
        _regra("c", GuardrailRuleKind.KEYWORD_BLOCK, order=2),
    ]

    await GuardrailEngine([espiao_direto]).apply(TEXTO, _politica(*regras))
    await GuardrailEngine([espiao_invertido]).apply(TEXTO, _politica(*reversed(regras)))

    assert espiao_direto.regras == espiao_invertido.regras == ["a", "b", "c"]


async def test_regra_desabilitada_nao_e_avaliada() -> None:
    """`enabled=False` tira a regra da cadeia sem remove-la da politica."""
    espiao = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)
    politica = _politica(
        _regra("ligada", GuardrailRuleKind.KEYWORD_BLOCK, order=0),
        _regra("desligada", GuardrailRuleKind.KEYWORD_BLOCK, order=1, enabled=False),
    )

    await GuardrailEngine([espiao]).apply(TEXTO, politica)

    assert espiao.regras == ["ligada"]


# --------------------------------------------------------------------------- #
# BLOCK interrompe a cadeia
# --------------------------------------------------------------------------- #
async def test_block_interrompe_a_cadeia_e_a_regra_seguinte_nao_e_avaliada() -> None:
    """Bloqueio e terminal: o contador da regra seguinte precisa ficar em zero."""
    bloqueia = AvaliadorEspiao(
        GuardrailRuleKind.KEYWORD_BLOCK,
        lambda conteudo, regra: _achado(regra, GuardrailAction.BLOCK),
    )
    depois = AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN)
    politica = _politica(
        _regra("bloqueia", GuardrailRuleKind.KEYWORD_BLOCK, order=0),
        _regra("depois", GuardrailRuleKind.SECRET_SCAN, order=1),
    )

    veredito = await GuardrailEngine([bloqueia, depois]).apply(TEXTO, politica)

    assert veredito.blocked is True
    assert bloqueia.chamadas == 1
    assert depois.chamadas == 0, "a regra seguinte a um BLOCK nao pode ser avaliada"


async def test_block_registra_apenas_os_achados_ate_o_bloqueio() -> None:
    """A trilha do veredito para no achado que bloqueou."""
    avisa = AvaliadorEspiao(
        GuardrailRuleKind.MIN_LENGTH,
        lambda conteudo, regra: _achado(regra, GuardrailAction.WARN),
    )
    bloqueia = AvaliadorEspiao(
        GuardrailRuleKind.KEYWORD_BLOCK,
        lambda conteudo, regra: _achado(regra, GuardrailAction.BLOCK),
    )
    nunca = AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN)
    politica = _politica(
        _regra("avisa", GuardrailRuleKind.MIN_LENGTH, order=0),
        _regra("bloqueia", GuardrailRuleKind.KEYWORD_BLOCK, order=1),
        _regra("nunca", GuardrailRuleKind.SECRET_SCAN, order=2),
    )

    veredito = await GuardrailEngine([avisa, bloqueia, nunca]).apply(TEXTO, politica)

    assert [achado.rule_id for achado in veredito.findings] == ["avisa", "bloqueia"]
    assert nunca.chamadas == 0


async def test_block_preserva_o_conteudo_original_no_veredito() -> None:
    """Mesmo bloqueado, o veredito guarda o texto que chegou, para auditoria."""
    bloqueia = AvaliadorEspiao(
        GuardrailRuleKind.KEYWORD_BLOCK,
        lambda conteudo, regra: _achado(regra, GuardrailAction.BLOCK),
    )
    politica = _politica(_regra("bloqueia", GuardrailRuleKind.KEYWORD_BLOCK))

    veredito = await GuardrailEngine([bloqueia]).apply(TEXTO, politica)

    assert veredito.original_content == TEXTO
    assert veredito.policy_id == politica.id
    assert veredito.stage is GuardrailStage.INPUT


# --------------------------------------------------------------------------- #
# REDACT encadeia, TRANSFORM substitui, WARN e ALLOW nao bloqueiam
# --------------------------------------------------------------------------- #
async def test_redact_entrega_o_texto_ja_redigido_a_regra_seguinte() -> None:
    """A cadeia opera sobre o conteudo corrente, nao sobre o original."""
    redige = AvaliadorEspiao(
        GuardrailRuleKind.PII_REDACT,
        lambda conteudo, regra: _achado(
            regra, GuardrailAction.REDACT, evidencia="CPF [REDIGIDO] do cliente"
        ),
    )
    seguinte = AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN)
    politica = _politica(
        _regra("cpf", GuardrailRuleKind.PII_REDACT, action=GuardrailAction.REDACT, order=0),
        _regra("segredos", GuardrailRuleKind.SECRET_SCAN, order=1),
    )

    veredito = await GuardrailEngine([redige, seguinte]).apply(
        "CPF 529.982.247-25 do cliente", politica
    )

    assert seguinte.conteudos == ["CPF [REDIGIDO] do cliente"], (
        "a regra seguinte precisa ver o texto ja redigido"
    )
    assert veredito.content == "CPF [REDIGIDO] do cliente"
    assert veredito.allowed is True
    assert veredito.modified is True


async def test_redacoes_sucessivas_se_acumulam() -> None:
    """Duas regras de redacao produzem um texto redigido duas vezes."""

    def primeira(conteudo: str, regra: GuardrailRule) -> GuardrailFinding:
        return _achado(regra, GuardrailAction.REDACT, evidencia=conteudo.replace("cpf", "[R1]"))

    def segunda(conteudo: str, regra: GuardrailRule) -> GuardrailFinding:
        return _achado(regra, GuardrailAction.REDACT, evidencia=conteudo.replace("email", "[R2]"))

    politica = _politica(
        _regra("cpf", GuardrailRuleKind.PII_REDACT, action=GuardrailAction.REDACT, order=0),
        _regra("email", GuardrailRuleKind.SECRET_SCAN, action=GuardrailAction.REDACT, order=1),
    )
    motor = GuardrailEngine(
        [
            AvaliadorEspiao(GuardrailRuleKind.PII_REDACT, primeira),
            AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN, segunda),
        ]
    )

    veredito = await motor.apply("cpf e email", politica)

    assert veredito.content == "[R1] e [R2]"
    assert veredito.original_content == "cpf e email"


async def test_redact_sem_evidencia_nao_apaga_o_conteudo() -> None:
    """Achado de redacao sem texto substituto e ignorado como transformacao."""
    redige = AvaliadorEspiao(
        GuardrailRuleKind.PII_REDACT,
        lambda conteudo, regra: _achado(regra, GuardrailAction.REDACT, evidencia=""),
    )
    politica = _politica(_regra("cpf", GuardrailRuleKind.PII_REDACT, action=GuardrailAction.REDACT))

    veredito = await GuardrailEngine([redige]).apply(TEXTO, politica)

    assert veredito.content == TEXTO
    assert veredito.modified is False


async def test_transform_substitui_o_conteudo_pela_evidencia() -> None:
    """`TRANSFORM` troca o texto inteiro (ex.: truncamento por `max_length`)."""
    trunca = AvaliadorEspiao(
        GuardrailRuleKind.MAX_LENGTH,
        lambda conteudo, regra: _achado(regra, GuardrailAction.TRANSFORM, evidencia=conteudo[:8]),
    )
    politica = _politica(
        _regra("tamanho", GuardrailRuleKind.MAX_LENGTH, action=GuardrailAction.TRANSFORM),
        stage=GuardrailStage.OUTPUT,
    )

    veredito = await GuardrailEngine([trunca]).apply(TEXTO, politica)

    assert veredito.content == "conteudo"
    assert veredito.allowed is True
    assert veredito.stage is GuardrailStage.OUTPUT


async def test_warn_registra_o_achado_sem_bloquear_nem_alterar_o_conteudo() -> None:
    """`WARN` e sinal de auditoria: a execucao continua e o texto passa igual."""
    avisa = AvaliadorEspiao(
        GuardrailRuleKind.LANGUAGE_ALLOW,
        lambda conteudo, regra: _achado(regra, GuardrailAction.WARN, evidencia="ignorado"),
    )
    seguinte = AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN)
    politica = _politica(
        _regra("idioma", GuardrailRuleKind.LANGUAGE_ALLOW, action=GuardrailAction.WARN, order=0),
        _regra("segredos", GuardrailRuleKind.SECRET_SCAN, order=1),
    )

    veredito = await GuardrailEngine([avisa, seguinte]).apply(TEXTO, politica)

    assert veredito.allowed is True
    assert veredito.content == TEXTO
    assert veredito.modified is False
    assert [achado.action for achado in veredito.findings] == [GuardrailAction.WARN]
    assert seguinte.chamadas == 1, "WARN nao pode interromper a cadeia"


async def test_allow_nao_altera_nada() -> None:
    """`ALLOW` registra o achado e segue: o conteudo e o veredito ficam intactos."""
    libera = AvaliadorEspiao(
        GuardrailRuleKind.REGEX_REQUIRE,
        lambda conteudo, regra: _achado(regra, GuardrailAction.ALLOW, evidencia="qualquer"),
    )
    politica = _politica(
        _regra("obrigatorio", GuardrailRuleKind.REGEX_REQUIRE, action=GuardrailAction.ALLOW)
    )

    veredito = await GuardrailEngine([libera]).apply(TEXTO, politica)

    assert veredito.allowed is True
    assert veredito.content == TEXTO
    assert len(veredito.findings) == 1


async def test_avaliador_que_nao_acusa_nada_nao_gera_achado() -> None:
    """`None` do avaliador significa "nada violado"."""
    silencioso = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK, None)
    politica = _politica(_regra("keyword", GuardrailRuleKind.KEYWORD_BLOCK))

    veredito = await GuardrailEngine([silencioso]).apply(TEXTO, politica)

    assert veredito.findings == []
    assert veredito.allowed is True


# --------------------------------------------------------------------------- #
# Avaliador ausente
# --------------------------------------------------------------------------- #
async def test_avaliador_ausente_com_fail_open_falso_levanta_unsupported_capability() -> None:
    """Sem avaliador e sem fail_open a politica nao pode ser cumprida: e erro."""
    politica = _politica(_regra("judge", GuardrailRuleKind.LLM_JUDGE))
    motor = GuardrailEngine([AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)], fail_open=False)

    with pytest.raises(UnsupportedCapability) as excecao:
        await motor.apply(TEXTO, politica)

    detalhes = excecao.value.details
    assert detalhes["rule_id"] == "judge"
    assert detalhes["kind"] == "llm_judge"
    assert detalhes["policy_id"] == politica.id
    assert detalhes["stage"] == "input"
    assert detalhes["registered_kinds"] == ["keyword_block"]
    assert excecao.value.http_status == 501


async def test_avaliador_ausente_com_fail_open_verdadeiro_vira_warn() -> None:
    """Com fail_open a lacuna e registrada como aviso e a cadeia continua."""
    seguinte = AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN)
    politica = _politica(
        _regra("judge", GuardrailRuleKind.LLM_JUDGE, order=0),
        _regra("segredos", GuardrailRuleKind.SECRET_SCAN, order=1),
    )
    motor = GuardrailEngine([seguinte], fail_open=True)

    veredito = await motor.apply(TEXTO, politica)

    assert veredito.allowed is True
    assert [achado.rule_id for achado in veredito.findings] == ["judge"]
    assert veredito.findings[0].action is GuardrailAction.WARN
    assert veredito.findings[0].kind is GuardrailRuleKind.LLM_JUDGE
    assert "nenhum avaliador registrado" in veredito.findings[0].message
    assert seguinte.chamadas == 1, "o aviso nao interrompe a avaliacao das regras seguintes"


async def test_fail_open_da_politica_vale_mesmo_com_o_motor_estrito() -> None:
    """`GuardrailPolicy.fail_open=True` afrouxa apenas aquela politica."""
    politica = _politica(_regra("judge", GuardrailRuleKind.LLM_JUDGE), fail_open=True)
    motor = GuardrailEngine([], fail_open=False)

    veredito = await motor.apply(TEXTO, politica)

    assert veredito.allowed is True
    assert veredito.findings[0].action is GuardrailAction.WARN


# --------------------------------------------------------------------------- #
# Excecao do avaliador
# --------------------------------------------------------------------------- #
async def test_excecao_do_avaliador_com_fail_open_falso_vira_guardrail_violation() -> None:
    """Falha interna de regra, sem fail_open, bloqueia a execucao com erro tipado."""
    quebrado = AvaliadorEspiao(GuardrailRuleKind.REGEX_BLOCK, erro=RuntimeError("regex explodiu"))
    politica = _politica(
        _regra("regex", GuardrailRuleKind.REGEX_BLOCK), stage=GuardrailStage.OUTPUT
    )
    motor = GuardrailEngine([quebrado], fail_open=False)

    with pytest.raises(GuardrailViolation) as excecao:
        await motor.apply(TEXTO, politica)

    assert excecao.value.rule_id == "regex"
    assert excecao.value.policy_id == politica.id
    assert excecao.value.stage == "output"
    assert excecao.value.details["cause"] == "RuntimeError"
    assert isinstance(excecao.value.__cause__, RuntimeError)


async def test_excecao_do_avaliador_com_fail_open_verdadeiro_vira_warn_e_segue() -> None:
    """SPEC-0003 criterio 5: fail_open converte falha interna em `WARN` sem bloquear."""
    quebrado = AvaliadorEspiao(GuardrailRuleKind.REGEX_BLOCK, erro=RuntimeError("regex explodiu"))
    seguinte = AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN)
    politica = _politica(
        _regra("regex", GuardrailRuleKind.REGEX_BLOCK, order=0),
        _regra("segredos", GuardrailRuleKind.SECRET_SCAN, order=1),
    )
    motor = GuardrailEngine([quebrado, seguinte], fail_open=True)

    veredito = await motor.apply(TEXTO, politica)

    assert veredito.allowed is True
    assert veredito.content == TEXTO
    assert veredito.findings[0].action is GuardrailAction.WARN
    assert "RuntimeError" in veredito.findings[0].message
    assert seguinte.chamadas == 1


async def test_excecao_do_avaliador_respeita_o_fail_open_da_politica() -> None:
    """A politica pode afrouxar sozinha, sem mexer na configuracao global."""
    quebrado = AvaliadorEspiao(GuardrailRuleKind.REGEX_BLOCK, erro=ValueError("boom"))
    politica = _politica(_regra("regex", GuardrailRuleKind.REGEX_BLOCK), fail_open=True)

    veredito = await GuardrailEngine([quebrado], fail_open=False).apply(TEXTO, politica)

    assert veredito.allowed is True
    assert veredito.findings[0].action is GuardrailAction.WARN


# --------------------------------------------------------------------------- #
# Contexto entregue aos avaliadores
# --------------------------------------------------------------------------- #
async def test_contexto_recebe_politica_estagio_e_marcador_de_redacao() -> None:
    """O avaliador precisa saber em que politica e estagio esta e como redigir."""
    espiao = AvaliadorEspiao(GuardrailRuleKind.PII_REDACT)
    politica = _politica(_regra("cpf", GuardrailRuleKind.PII_REDACT), stage=GuardrailStage.OUTPUT)
    motor = GuardrailEngine([espiao], redaction_token="<<oculto>>")

    await motor.apply(TEXTO, politica, context={"module_slug": "assistente"})

    contexto = espiao.contextos[0]
    assert contexto["policy_id"] == politica.id
    assert contexto["policy_slug"] == politica.slug
    assert contexto["stage"] == "output"
    assert contexto["redaction_token"] == "<<oculto>>"
    assert contexto["module_slug"] == "assistente", "o contexto do chamador e preservado"


async def test_contexto_do_chamador_pode_sobrescrever_o_marcador_de_redacao() -> None:
    """`setdefault` deixa o chamador escolher outro marcador quando precisa."""
    espiao = AvaliadorEspiao(GuardrailRuleKind.PII_REDACT)
    politica = _politica(_regra("cpf", GuardrailRuleKind.PII_REDACT))

    await GuardrailEngine([espiao]).apply(TEXTO, politica, context={"redaction_token": "***"})

    assert espiao.contextos[0]["redaction_token"] == "***"


# --------------------------------------------------------------------------- #
# Registro de avaliadores e conformidade com a porta
# --------------------------------------------------------------------------- #
def test_register_substitui_o_avaliador_do_mesmo_tipo() -> None:
    """Um tipo de regra tem exatamente um avaliador: o ultimo registrado vence."""
    motor = GuardrailEngine([AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)])
    substituto = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)

    motor.register(substituto)

    assert motor.kinds == frozenset({GuardrailRuleKind.KEYWORD_BLOCK})


async def test_avaliador_registrado_depois_da_construcao_passa_a_ser_usado() -> None:
    """`register` existe para o composition root completar o motor apos o boot."""
    motor = GuardrailEngine([])
    tardio = AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)
    motor.register(tardio)

    await motor.apply(TEXTO, _politica(_regra("k", GuardrailRuleKind.KEYWORD_BLOCK)))

    assert tardio.chamadas == 1


def test_motor_expoe_a_configuracao_recebida() -> None:
    """As propriedades publicas espelham o que o composition root injetou."""
    motor = GuardrailEngine([], redaction_token="<<x>>", fail_open=True, enabled=False)

    assert motor.redaction_token == "<<x>>"
    assert motor.fail_open is True
    assert motor.enabled is False


def test_motor_implementa_a_porta_de_guardrail() -> None:
    """`GuardrailEngine` e o que o `Container` injeta como `GuardrailPort`."""
    assert isinstance(GuardrailEngine([]), GuardrailPort)


# --------------------------------------------------------------------------- #
# latency_ms
# --------------------------------------------------------------------------- #
async def test_latency_ms_e_preenchido_e_nao_negativo() -> None:
    """Todo veredito carrega a latencia medida, inclusive o permissivo."""
    motor = GuardrailEngine([AvaliadorEspiao(GuardrailRuleKind.KEYWORD_BLOCK)])
    politica = _politica(_regra("k", GuardrailRuleKind.KEYWORD_BLOCK))

    com_politica = await motor.apply(TEXTO, politica)
    sem_politica = await motor.apply(TEXTO, None)

    assert isinstance(com_politica.latency_ms, float)
    assert com_politica.latency_ms >= 0.0
    assert sem_politica.latency_ms >= 0.0


async def test_latency_ms_mede_o_tempo_gasto_pelos_avaliadores() -> None:
    """Com um avaliador que gasta CPU de verdade, a latencia sai maior que zero."""
    politica = _politica(_regra("pesado", GuardrailRuleKind.TOPIC_BLOCK))
    motor = GuardrailEngine([AvaliadorPesado(GuardrailRuleKind.TOPIC_BLOCK)])

    veredito = await motor.apply(TEXTO, politica)

    assert veredito.latency_ms > 0.0, "a latencia nao pode ser uma constante zerada"


# --------------------------------------------------------------------------- #
# Determinismo
# --------------------------------------------------------------------------- #
async def test_mesma_entrada_e_mesma_politica_produzem_o_mesmo_veredito() -> None:
    """Determinismo obrigatorio da SPEC-0003 secao 2 (fora do `llm_judge`)."""

    def redige(conteudo: str, regra: GuardrailRule) -> GuardrailFinding:
        return _achado(regra, GuardrailAction.REDACT, evidencia=conteudo.upper())

    politica = _politica(
        _regra("um", GuardrailRuleKind.PII_REDACT, action=GuardrailAction.REDACT, order=0),
        _regra("dois", GuardrailRuleKind.SECRET_SCAN, order=1),
    )
    motor = GuardrailEngine(
        [
            AvaliadorEspiao(GuardrailRuleKind.PII_REDACT, redige),
            AvaliadorEspiao(GuardrailRuleKind.SECRET_SCAN),
        ]
    )

    primeiro = await motor.apply(TEXTO, politica)
    segundo = await motor.apply(TEXTO, politica)

    assert primeiro.model_dump(exclude={"latency_ms"}) == segundo.model_dump(exclude={"latency_ms"})
