"""Testes de unidade da hierarquia de erros do dominio (SPEC-0000 secao 5).

`code` e `http_status` sao contrato publico: o handler HTTP monta
`{"error": {"code", "message", "details"}}` a partir deles e a SPEC-0000 fixa a
tabela inteira. Aqui a tabela normativa vira dado de teste, e o mapa
`ERROR_BY_CODE` e conferido contra todas as subclasses realmente declaradas —
uma excecao nova que esqueca o `code` quebra este arquivo, nao a producao.
"""

from __future__ import annotations

import pytest

from lukato.domain.errors import (
    ERROR_BY_CODE,
    BudgetExceededError,
    ConfigurationError,
    ConflictError,
    ForbiddenError,
    GuardrailViolation,
    LukatoError,
    ModuleError,
    ModuleNotFound,
    NotFoundError,
    ProviderError,
    RateLimitedError,
    UnauthorizedError,
    UnsupportedCapability,
    ValidationError,
    error_for_code,
)

pytestmark = pytest.mark.unit

TABELA_NORMATIVA: list[tuple[type[LukatoError], str, int]] = [
    (LukatoError, "lukato_error", 500),
    (ValidationError, "validation_error", 422),
    (NotFoundError, "not_found", 404),
    (ConflictError, "conflict", 409),
    (UnauthorizedError, "unauthorized", 401),
    (ForbiddenError, "forbidden", 403),
    (GuardrailViolation, "guardrail_violation", 422),
    (BudgetExceededError, "budget_exceeded", 402),
    (ProviderError, "provider_error", 502),
    (RateLimitedError, "rate_limited", 429),
    (ModuleError, "module_error", 500),
    (ModuleNotFound, "module_not_found", 404),
    (ConfigurationError, "configuration_error", 500),
    (UnsupportedCapability, "unsupported_capability", 501),
]
"""Tabela da SPEC-0000 secao 5, transcrita literalmente."""


def _todas_as_subclasses(base: type[LukatoError]) -> set[type[LukatoError]]:
    """Percorre recursivamente as subclasses declaradas de um erro."""
    encontradas: set[type[LukatoError]] = set()
    for subclasse in base.__subclasses__():
        encontradas.add(subclasse)
        encontradas |= _todas_as_subclasses(subclasse)
    return encontradas


# --------------------------------------------------------------------------- #
# Tabela de codigos e status
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("classe", "codigo", "status"), TABELA_NORMATIVA, ids=lambda item: getattr(item, "__name__", "")
)
def test_erro_expoe_o_code_e_o_http_status_da_spec(
    classe: type[LukatoError], codigo: str, status: int
) -> None:
    """Cada excecao carrega exatamente o par `(code, http_status)` da SPEC-0000."""
    assert classe.code == codigo
    assert classe.http_status == status


@pytest.mark.parametrize(
    ("classe", "codigo", "status"), TABELA_NORMATIVA, ids=lambda item: getattr(item, "__name__", "")
)
def test_code_e_http_status_tambem_valem_na_instancia(
    classe: type[LukatoError], codigo: str, status: int
) -> None:
    """Sao atributos de classe, entao a instancia levantada tambem os expoe."""
    erro = classe("qualquer mensagem")

    assert erro.code == codigo
    assert erro.http_status == status


def test_toda_excecao_de_dominio_herda_de_lukato_error() -> None:
    """Uma hierarquia unica permite `except LukatoError` no handler HTTP."""
    for classe, _, _ in TABELA_NORMATIVA:
        assert issubclass(classe, LukatoError)
        assert issubclass(classe, Exception)


def test_module_not_found_e_um_not_found() -> None:
    """`ModuleNotFound` herda de `NotFoundError` (SPEC-0000 secao 5)."""
    assert issubclass(ModuleNotFound, NotFoundError)
    assert ModuleNotFound("modulo 'x' nao registrado").http_status == 404


# --------------------------------------------------------------------------- #
# Mensagem, detalhes e to_dict
# --------------------------------------------------------------------------- #
def test_mensagem_fica_disponivel_no_atributo_e_no_str() -> None:
    """A mensagem legivel e acessivel por `.message` e por `str(erro)`."""
    erro = ValidationError("slug invalido")

    assert erro.message == "slug invalido"
    assert str(erro) == "slug invalido"


def test_details_e_um_dicionario_vazio_quando_nao_informado() -> None:
    """Consumidores podem indexar `details` sem checar `None`."""
    assert LukatoError("falhou").details == {}


def test_details_preserva_o_conteudo_informado() -> None:
    """Os detalhes estruturados chegam intactos ao envelope de erro."""
    erro = NotFoundError("sumiu", details={"id": "abc", "tipo": "modulo"})

    assert erro.details == {"id": "abc", "tipo": "modulo"}


def test_details_nao_compartilha_o_dicionario_do_chamador() -> None:
    """O erro copia os detalhes: mutar a origem depois nao muda o erro."""
    origem = {"id": "abc"}
    erro = NotFoundError("sumiu", details=origem)

    origem["id"] = "MUDOU"

    assert erro.details == {"id": "abc"}


def test_to_dict_usa_o_formato_do_envelope_de_erro_da_api() -> None:
    """`to_dict` devolve exatamente `{"code", "message", "details"}`."""
    erro = ConflictError("slug ja usado", details={"slug": "assistente"})

    assert erro.to_dict() == {
        "code": "conflict",
        "message": "slug ja usado",
        "details": {"slug": "assistente"},
    }


def test_to_dict_devolve_copia_dos_detalhes() -> None:
    """Mutar o dicionario devolvido nao contamina a excecao original."""
    erro = ValidationError("invalido", details={"campo": "slug"})

    saida = erro.to_dict()
    saida["details"]["campo"] = "OUTRO"

    assert erro.details == {"campo": "slug"}


def test_to_dict_de_erro_sem_detalhes_traz_dicionario_vazio() -> None:
    """A chave `details` existe sempre, mesmo vazia — o contrato nao muda de forma."""
    assert LukatoError("falhou").to_dict()["details"] == {}


def test_repr_mostra_classe_codigo_e_mensagem() -> None:
    """O `repr` e diagnostico de log: identifica a classe e o codigo."""
    assert repr(ProviderError("hub fora do ar")) == (
        "ProviderError(code='provider_error', message='hub fora do ar')"
    )


def test_erro_pode_ser_capturado_pela_classe_base() -> None:
    """`except LukatoError` pega qualquer erro do ecossistema."""
    with pytest.raises(LukatoError) as excecao:
        raise RateLimitedError("429 do provedor")

    assert excecao.value.code == "rate_limited"


# --------------------------------------------------------------------------- #
# GuardrailViolation (SPEC-0000 secao 5, SPEC-0003 secao 2)
# --------------------------------------------------------------------------- #
def test_guardrail_violation_carrega_policy_rule_e_stage_nos_details() -> None:
    """Os tres campos extras viajam em `details` para chegar ao JSON da API."""
    erro = GuardrailViolation(
        "conteudo bloqueado",
        policy_id="pol-1",
        rule_id="pii-cpf",
        stage="output",
    )

    assert erro.details["policy_id"] == "pol-1"
    assert erro.details["rule_id"] == "pii-cpf"
    assert erro.details["stage"] == "output"


def test_guardrail_violation_expoe_os_campos_extras_como_atributos() -> None:
    """O codigo Python le `erro.stage` sem precisar navegar em `details`."""
    erro = GuardrailViolation("bloqueado", policy_id="pol-1", rule_id="cpf", stage="input")

    assert (erro.policy_id, erro.rule_id, erro.stage) == ("pol-1", "cpf", "input")


def test_guardrail_violation_usa_estagio_de_entrada_por_padrao() -> None:
    """Sem `stage` explicito o bloqueio e de entrada — o caso mais comum."""
    erro = GuardrailViolation("bloqueado")

    assert erro.stage == "input"
    assert erro.details == {"policy_id": None, "rule_id": None, "stage": "input"}


def test_guardrail_violation_mantem_os_details_do_chamador_ao_lado_dos_extras() -> None:
    """Detalhes proprios da regra convivem com `policy_id`/`rule_id`/`stage`."""
    erro = GuardrailViolation(
        "bloqueado",
        details={"kind": "pii_redact", "evidence": "CPF"},
        policy_id="pol-1",
        rule_id="cpf",
        stage="input",
    )

    assert erro.details["kind"] == "pii_redact"
    assert erro.details["evidence"] == "CPF"
    assert erro.details["rule_id"] == "cpf"


def test_guardrail_violation_serializa_tudo_em_to_dict() -> None:
    """O envelope da API entrega o motivo do bloqueio pronto para o cliente."""
    erro = GuardrailViolation("bloqueado", policy_id="pol-1", rule_id="cpf", stage="output")

    assert erro.to_dict() == {
        "code": "guardrail_violation",
        "message": "bloqueado",
        "details": {"policy_id": "pol-1", "rule_id": "cpf", "stage": "output"},
    }


# --------------------------------------------------------------------------- #
# ERROR_BY_CODE e error_for_code
# --------------------------------------------------------------------------- #
def test_error_by_code_cobre_toda_a_tabela_normativa() -> None:
    """Todo par `(code, classe)` da SPEC-0000 esta no mapa de resolucao."""
    for classe, codigo, _ in TABELA_NORMATIVA:
        assert ERROR_BY_CODE[codigo] is classe, (
            f"'{codigo}' deveria resolver para {classe.__name__}"
        )


def test_error_by_code_nao_tem_codigo_alem_dos_declarados() -> None:
    """O mapa e exatamente a tabela: nada sobrando, nada faltando."""
    assert set(ERROR_BY_CODE) == {codigo for _, codigo, _ in TABELA_NORMATIVA}


def test_error_by_code_cobre_toda_subclasse_declarada_no_modulo() -> None:
    """Qualquer excecao nova de dominio precisa aparecer no mapa por `code`."""
    declaradas = _todas_as_subclasses(LukatoError) | {LukatoError}

    assert set(ERROR_BY_CODE.values()) == declaradas


def test_cada_subclasse_declara_um_code_proprio() -> None:
    """Duas excecoes com o mesmo `code` se apagariam no mapa."""
    declaradas = _todas_as_subclasses(LukatoError) | {LukatoError}
    codigos = [classe.code for classe in declaradas]

    assert len(codigos) == len(set(codigos)), f"codigos duplicados entre {sorted(codigos)}"


def test_error_for_code_resolve_um_codigo_conhecido() -> None:
    """A resolucao por codigo permite reconstruir o erro a partir do JSON."""
    assert error_for_code("budget_exceeded") is BudgetExceededError


def test_error_for_code_desconhecido_cai_no_erro_base() -> None:
    """Codigo desconhecido nao explode: degrada para `LukatoError`."""
    assert error_for_code("codigo-que-nao-existe") is LukatoError
