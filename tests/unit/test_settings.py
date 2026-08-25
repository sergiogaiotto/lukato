"""Testes de unidade da configuracao tipada (SPEC-0000 secao 13).

`Settings` e a unica porta de entrada de parametrizacao do lukato, entao aqui se
verifica: leitura de variaveis aninhadas (`LUKATO_LLM__MODEL`), listas vindas do
ambiente em CSV ou JSON, segredo que nao vaza em `repr`/`str`/JSON, o fallback de
`effective_provider` para `echo`/`hashing`, e as tres recusas de configuracao
incoerente — pesos do AdWatch que nao somam 1, `review_threshold` maior ou igual
a `accept_threshold`, e producao com autenticacao ligada sobre um segredo fraco.

Nenhum teste le o `.env` da maquina: `Settings` e sempre construido com
`_env_file=None` e as variaveis `LUKATO_*` sao removidas pela fixture autouse da
suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from lukato.config.settings import (
    MIN_JWT_SECRET_CHARS,
    WEAK_JWT_SECRETS,
    AdWatchSettings,
    AppSettings,
    DatabaseSettings,
    EmbeddingSettings,
    FinOpsSettings,
    LLMSettings,
    ObservabilitySettings,
    SecuritySettings,
    Settings,
    get_settings,
    reset_settings_cache,
)

pytestmark = pytest.mark.unit

SEGREDO_FORTE = "b7f3c1a9e5d24f8091ac6b3e7d5f2a184c9e0b6d3f7a1c85"
"""Segredo de 48 caracteres, fora da lista de proibidos, aceito em producao."""


def _settings(**grupos: object) -> Settings:
    """Constroi `Settings` isolado do `.env` do repositorio."""
    return Settings(_env_file=None, **grupos)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Variaveis de ambiente aninhadas
# --------------------------------------------------------------------------- #
def test_variavel_aninhada_alimenta_o_grupo_correspondente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`LUKATO_LLM__MODEL` cai em `settings.llm.model` (prefixo + delimitador `__`)."""
    monkeypatch.setenv("LUKATO_LLM__MODEL", "modelo-do-ambiente")

    assert _settings().llm.model == "modelo-do-ambiente"


def test_variavel_aninhada_alcanca_todos_os_grupos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada grupo da SPEC-0000 secao 13 tem o seu proprio espaco de variaveis."""
    monkeypatch.setenv("LUKATO_APP__PORT", "9001")
    monkeypatch.setenv("LUKATO_DB__ECHO", "true")
    monkeypatch.setenv("LUKATO_GUARDRAILS__FAIL_OPEN", "true")
    monkeypatch.setenv("LUKATO_OBSERVABILITY__LOG_LEVEL", "debug")
    monkeypatch.setenv("LUKATO_ADWATCH__TOP_K_RERANK", "7")

    configuracao = _settings()

    assert configuracao.app.port == 9001
    assert configuracao.db.echo is True
    assert configuracao.guardrails.fail_open is True
    assert configuracao.observability.log_level == "DEBUG"
    assert configuracao.adwatch.top_k_rerank == 7


def test_nome_da_variavel_nao_diferencia_maiusculas(monkeypatch: pytest.MonkeyPatch) -> None:
    """`case_sensitive=False`: o nome em minusculas tambem e lido."""
    monkeypatch.setenv("lukato_llm__model", "minusculo")

    assert _settings().llm.model == "minusculo"


def test_ambiente_e_normalizado_para_minusculas(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LUKATO_APP__ENV=DEV` vira `dev`, para a comparacao com `PRODUCTION_ENVS` funcionar."""
    monkeypatch.setenv("LUKATO_APP__ENV", "  DEV  ")

    assert _settings().app.env == "dev"


def test_grupo_ignora_chave_desconhecida() -> None:
    """`extra="ignore"`: uma variavel a mais no ambiente nao derruba o boot."""
    assert AppSettings(chave_inexistente="x").name == "lukato"


def test_argumento_explicito_prevalece_sobre_o_ambiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O que o composition root passa no construtor vence a variavel de ambiente."""
    monkeypatch.setenv("LUKATO_LLM__MODEL", "do-ambiente")

    assert _settings(llm={"model": "do-codigo"}).llm.model == "do-codigo"


# --------------------------------------------------------------------------- #
# Listas em CSV e em JSON
# --------------------------------------------------------------------------- #
def test_cors_origins_aceita_csv_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    """CSV e o formato natural de quem edita um `.env` na mao."""
    monkeypatch.setenv("LUKATO_SECURITY__CORS_ORIGINS", "https://a.com, https://b.com")

    assert _settings().security.cors_origins == ["https://a.com", "https://b.com"]


def test_cors_origins_aceita_json_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON e o formato natural de quem gera o manifesto por template."""
    monkeypatch.setenv("LUKATO_SECURITY__CORS_ORIGINS", '["https://a.com","https://b.com"]')

    assert _settings().security.cors_origins == ["https://a.com", "https://b.com"]


def test_cors_origins_vazio_vira_lista_vazia() -> None:
    """`LUKATO_SECURITY__CORS_ORIGINS=` significa nenhuma origem, nao `[""]`."""
    assert SecuritySettings(cors_origins="   ").cors_origins == []


def test_cors_origins_csv_descarta_itens_em_branco() -> None:
    """Virgula sobrando no fim do CSV nao vira origem vazia."""
    assert SecuritySettings(cors_origins="https://a.com, ,https://b.com,").cors_origins == [
        "https://a.com",
        "https://b.com",
    ]


def test_window_sizes_aceita_csv_e_converte_para_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As janelas do AdWatch chegam como `15,30,60` e viram floats."""
    monkeypatch.setenv("LUKATO_ADWATCH__WINDOW_SIZES", "15,30,60")

    assert _settings().adwatch.window_sizes == [15.0, 30.0, 60.0]


def test_window_sizes_aceita_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mesma lista em JSON produz o mesmo resultado."""
    monkeypatch.setenv("LUKATO_ADWATCH__WINDOW_SIZES", "[15.0, 30.0, 60.0]")

    assert _settings().adwatch.window_sizes == [15.0, 30.0, 60.0]


def test_precos_do_finops_aceitam_json_aninhado(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tabela de precos e um mapa de mapas e so cabe em JSON."""
    monkeypatch.setenv("LUKATO_FINOPS__PRICES", '{"meu-modelo": {"input": 0.002, "output": 0.006}}')

    assert _settings().finops.price_for("meu-modelo") == (0.002, 0.006)


# --------------------------------------------------------------------------- #
# Segredos
# --------------------------------------------------------------------------- #
def test_segredo_do_llm_nao_aparece_no_repr_nem_no_str_nem_no_json() -> None:
    """`SecretStr` protege a chave em log, em traceback e no dump da configuracao."""
    configuracao = _settings(
        llm={"api_key": "sk-segredo-que-nao-pode-vazar"},
        security={"jwt_secret": "jwt-segredo-que-nao-pode-vazar"},
    )

    assert "sk-segredo-que-nao-pode-vazar" not in repr(configuracao)
    assert "sk-segredo-que-nao-pode-vazar" not in str(configuracao)
    assert "sk-segredo-que-nao-pode-vazar" not in configuracao.model_dump_json()
    assert "jwt-segredo-que-nao-pode-vazar" not in repr(configuracao)
    assert "jwt-segredo-que-nao-pode-vazar" not in configuracao.model_dump_json()


def test_segredo_continua_legivel_pela_propriedade_dedicada() -> None:
    """Esconder no `repr` nao pode impedir o adaptador de usar a credencial."""
    configuracao = _settings(llm={"api_key": "sk-abc"})

    assert configuracao.llm.api_key_value == "sk-abc"
    assert isinstance(configuracao.llm.api_key, SecretStr)


def test_repr_do_grupo_de_seguranca_tambem_mascara_o_jwt() -> None:
    """O grupo isolado precisa mascarar tanto quanto o `Settings` inteiro."""
    grupo = SecuritySettings(jwt_secret="segredo-isolado-que-nao-pode-vazar")

    assert "segredo-isolado-que-nao-pode-vazar" not in repr(grupo)
    assert grupo.jwt_secret_value == "segredo-isolado-que-nao-pode-vazar"


@pytest.mark.parametrize("branco", ["", "   "])
def test_credencial_em_branco_e_tratada_como_ausente(branco: str) -> None:
    """`LUKATO_LLM__API_KEY=` no `.env` significa "sem credencial", nao chave vazia."""
    assert LLMSettings(api_key=branco).api_key is None
    assert EmbeddingSettings(api_key=branco).api_key is None
    assert ObservabilitySettings(langfuse_public_key=branco).langfuse_public_key is None


# --------------------------------------------------------------------------- #
# effective_provider e degradacao offline
# --------------------------------------------------------------------------- #
def test_llm_sem_credencial_cai_para_echo() -> None:
    """Sem API key nao ha como falar com o hub: o provedor efetivo e o eco offline."""
    assert LLMSettings(provider="openai_compatible", api_key=None).effective_provider == "echo"


def test_llm_com_credencial_mantem_o_provedor_declarado() -> None:
    """Com credencial, o provedor efetivo e o hub compativel com OpenAI."""
    configuracao = LLMSettings(provider="openai_compatible", api_key="sk-abc")

    assert configuracao.effective_provider == "openai_compatible"


def test_llm_declarado_como_echo_permanece_echo() -> None:
    """Escolher `echo` explicitamente e uma decisao, nao um fallback."""
    assert LLMSettings(provider="echo", api_key="sk-abc").effective_provider == "echo"


def test_embedding_sem_endpoint_cai_para_hashing() -> None:
    """Sem `base_url` o servico Qwen e inalcancavel: vale o hashing deterministico."""
    assert EmbeddingSettings(provider="qwen", base_url="   ").effective_provider == "hashing"


def test_embedding_com_endpoint_mantem_qwen() -> None:
    """Com endpoint configurado o provedor efetivo continua sendo o Qwen."""
    assert EmbeddingSettings(provider="qwen").effective_provider == "qwen"


def test_embedding_declarado_como_hashing_permanece_hashing() -> None:
    """O modo offline e estavel: nada o promove de volta para a rede."""
    assert EmbeddingSettings(provider="hashing").effective_provider == "hashing"


@pytest.mark.parametrize("provedor", ["anthropic", "openai", "", "ECHO_"])
def test_provedor_de_llm_fora_do_catalogo_e_recusado(provedor: str) -> None:
    """Somente `openai_compatible` e `echo` sao aceitos (SPEC-0000 secao 13)."""
    with pytest.raises(PydanticValidationError):
        LLMSettings(provider=provedor)


@pytest.mark.parametrize("provedor", ["openai", "sentence-transformers", ""])
def test_provedor_de_embedding_fora_do_catalogo_e_recusado(provedor: str) -> None:
    """Somente `qwen` e `hashing` sao aceitos (SPEC-0000 secao 13)."""
    with pytest.raises(PydanticValidationError):
        EmbeddingSettings(provider=provedor)


def test_llm_configured_exige_credencial_e_provedor_de_rede() -> None:
    """`llm_configured` diz se ha como falar com o hub, nao se o eco funciona."""
    assert _settings(llm={"provider": "echo"}).llm_configured is False
    assert _settings(llm={"provider": "openai_compatible"}).llm_configured is False
    assert (
        _settings(llm={"provider": "openai_compatible", "api_key": "sk-abc"}).llm_configured is True
    )


# --------------------------------------------------------------------------- #
# AdWatch: pesos e limiares
# --------------------------------------------------------------------------- #
def test_pesos_padrao_do_adwatch_somam_um_e_seguem_a_spec() -> None:
    """`0.40/0.25/0.15/0.15/0.05` e a fusao normativa da SPEC-0000 secao 8."""
    pesos = AdWatchSettings().weights()

    assert pesos == {
        "lexical": 0.40,
        "semantic": 0.25,
        "ocr": 0.15,
        "visual": 0.15,
        "duration": 0.05,
    }
    assert sum(pesos.values()) == pytest.approx(1.0)


def test_pesos_que_nao_somam_um_sao_recusados() -> None:
    """Somar 1.10 desequilibra o score fundido e precisa falhar no boot."""
    with pytest.raises(PydanticValidationError) as excecao:
        AdWatchSettings(weight_lexical=0.50, weight_semantic=0.35)

    assert "soma dos pesos" in str(excecao.value)


def test_pesos_que_somam_menos_de_um_tambem_sao_recusados() -> None:
    """A verificacao e nos dois sentidos: 0.85 tambem e incoerente."""
    with pytest.raises(PydanticValidationError):
        AdWatchSettings(weight_lexical=0.25)


def test_pesos_dentro_da_tolerancia_sao_aceitos() -> None:
    """Arredondamento de configuracao nao pode derrubar a aplicacao."""
    configuracao = AdWatchSettings(weight_duration=0.0505, weight_ocr=0.1495)

    assert sum(configuracao.weights().values()) == pytest.approx(1.0, abs=1e-3)


def test_review_threshold_igual_ao_accept_threshold_e_recusado() -> None:
    """Com os limiares colados a faixa `NEEDS_REVIEW` some do pipeline."""
    with pytest.raises(PydanticValidationError) as excecao:
        AdWatchSettings(review_threshold=0.90, accept_threshold=0.90)

    assert "review_threshold" in str(excecao.value)


def test_review_threshold_maior_que_accept_threshold_e_recusado() -> None:
    """Limiar de revisao acima do de aceite inverteria a decisao da fusao."""
    with pytest.raises(PydanticValidationError):
        AdWatchSettings(review_threshold=0.95, accept_threshold=0.90)


def test_limiares_padrao_seguem_a_spec() -> None:
    """`>= 0.90` aceita e `>= 0.60` manda para revisao (SPEC-0000 secao 8)."""
    configuracao = AdWatchSettings()

    assert (configuracao.accept_threshold, configuracao.review_threshold) == (0.90, 0.60)


# --------------------------------------------------------------------------- #
# Producao: autenticacao, guardrails e segredo do JWT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fraco", sorted(WEAK_JWT_SECRETS))
def test_producao_com_auth_ligada_recusa_segredo_conhecido(fraco: str) -> None:
    """Os segredos de exemplo do repositorio nao podem assinar token em producao."""
    with pytest.raises(PydanticValidationError) as excecao:
        _settings(
            app={"env": "prod"},
            security={"auth_enabled": True, "jwt_secret": fraco},
        )

    assert "JWT_SECRET" in str(excecao.value)


def test_producao_com_auth_ligada_recusa_segredo_curto() -> None:
    """Chave HMAC menor que 32 caracteres e quebravel por forca bruta (RFC 7518)."""
    with pytest.raises(PydanticValidationError) as excecao:
        _settings(
            app={"env": "prod"},
            security={"auth_enabled": True, "jwt_secret": "a" * (MIN_JWT_SECRET_CHARS - 1)},
        )

    assert str(MIN_JWT_SECRET_CHARS) in str(excecao.value)


def test_producao_com_auth_ligada_e_segredo_forte_e_aceita() -> None:
    """O caminho feliz de producao precisa subir sem reclamacao."""
    configuracao = _settings(
        app={"env": "production"},
        security={"auth_enabled": True, "jwt_secret": SEGREDO_FORTE},
    )

    assert configuracao.is_production is True
    assert configuracao.security.auth_enabled is True


def test_producao_com_autenticacao_desligada_e_recusada() -> None:
    """Sem autenticacao a API inteira responderia como root anonimo."""
    with pytest.raises(PydanticValidationError) as excecao:
        _settings(
            app={"env": "prod"},
            security={"auth_enabled": False, "jwt_secret": SEGREDO_FORTE},
        )

    assert "AUTH_ENABLED" in str(excecao.value)


def test_producao_com_guardrails_desligados_e_recusada() -> None:
    """Desligar a trinca em producao entregaria PII e segredos ao provedor."""
    with pytest.raises(PydanticValidationError) as excecao:
        _settings(
            app={"env": "prod"},
            guardrails={"enabled": False},
            security={"auth_enabled": True, "jwt_secret": SEGREDO_FORTE},
        )

    assert "GUARDRAILS__ENABLED" in str(excecao.value)


def test_ambiente_de_desenvolvimento_tolera_o_segredo_padrao() -> None:
    """Em dev o `change-me` e conveniencia, nao risco: a verificacao nao dispara."""
    configuracao = _settings(app={"env": "dev"}, security={"auth_enabled": False})

    assert configuracao.is_production is False
    assert configuracao.security.jwt_secret_value == "change-me"


@pytest.mark.parametrize(("ambiente", "producao"), [("prod", True), ("production", True)])
def test_is_production_reconhece_os_dois_nomes_do_ambiente(ambiente: str, producao: bool) -> None:
    """`prod` e `production` sao o mesmo ambiente para efeito das verificacoes."""
    configuracao = _settings(
        app={"env": ambiente},
        security={"auth_enabled": True, "jwt_secret": SEGREDO_FORTE},
    )

    assert configuracao.is_production is producao


@pytest.mark.parametrize("ambiente", ["dev", "test", "staging", "homolog"])
def test_is_production_e_falso_nos_demais_ambientes(ambiente: str) -> None:
    """Qualquer outro nome de ambiente nao aciona as regras de producao."""
    assert _settings(app={"env": ambiente}).is_production is False


# --------------------------------------------------------------------------- #
# Banco, FinOps e memoizacao
# --------------------------------------------------------------------------- #
def test_database_url_prefere_a_principal_quando_preenchida() -> None:
    """Sem pedido explicito, a URL principal e a usada."""
    configuracao = _settings(db={"url": "postgresql+asyncpg://x/y", "fallback_url": "sqlite://"})

    assert configuracao.database_url() == "postgresql+asyncpg://x/y"


def test_database_url_usa_o_fallback_sob_demanda() -> None:
    """O composition root pede o fallback quando o PostgreSQL nao responde."""
    configuracao = _settings(
        db={"url": "postgresql+asyncpg://x/y", "fallback_url": "sqlite+aiosqlite:///./lukato.db"}
    )

    assert configuracao.database_url(prefer_fallback=True) == "sqlite+aiosqlite:///./lukato.db"


def test_database_url_vazia_cai_no_fallback() -> None:
    """URL principal em branco equivale a nao ter banco principal configurado."""
    configuracao = _settings(db={"url": "   ", "fallback_url": "sqlite+aiosqlite:///:memory:"})

    assert configuracao.database_url() == "sqlite+aiosqlite:///:memory:"


def test_is_sqlite_reconhece_a_url_de_desenvolvimento() -> None:
    """A flag orienta o adaptador a evitar recursos exclusivos do PostgreSQL."""
    assert DatabaseSettings(url="sqlite+aiosqlite:///./lukato.db").is_sqlite is True
    assert DatabaseSettings(url="postgresql+asyncpg://x/y").is_sqlite is False


def test_price_for_devolve_os_defaults_para_modelo_desconhecido() -> None:
    """Modelo fora da tabela usa o preco padrao configurado, nunca `None`."""
    configuracao = FinOpsSettings(
        prices={}, default_input_usd_per_1k=0.001, default_output_usd_per_1k=0.003
    )

    assert configuracao.price_for("modelo-desconhecido") == (0.001, 0.003)


def test_price_for_devolve_o_preco_cadastrado() -> None:
    """Preco explicito do modelo prevalece sobre o default."""
    configuracao = FinOpsSettings(prices={"m": {"input": 0.002, "output": 0.006}})

    assert configuracao.price_for("m") == (0.002, 0.006)


def test_langfuse_configured_exige_as_duas_chaves_e_a_flag() -> None:
    """O tracer real so entra em cena com flag e o par de credenciais."""
    assert ObservabilitySettings(langfuse_enabled=True).langfuse_configured is False
    assert (
        ObservabilitySettings(
            langfuse_enabled=True, langfuse_public_key="pk", langfuse_secret_key="sk"
        ).langfuse_configured
        is True
    )
    assert (
        ObservabilitySettings(
            langfuse_enabled=False, langfuse_public_key="pk", langfuse_secret_key="sk"
        ).langfuse_configured
        is False
    )


def test_get_settings_memoiza_a_instancia(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`get_settings()` e `lru_cache`: a mesma instancia serve o processo inteiro."""
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()

    assert get_settings() is get_settings()


def test_reset_settings_cache_permite_reler_o_ambiente(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Limpar a memoizacao e o que torna a configuracao testavel."""
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    primeira = get_settings()

    reset_settings_cache()
    monkeypatch.setenv("LUKATO_APP__NAME", "outro-nome")
    segunda = get_settings()

    assert primeira is not segunda
    assert segunda.app.name == "outro-nome"


def test_defaults_da_raiz_seguem_a_spec() -> None:
    """Sem nenhuma variavel de ambiente a aplicacao sobe offline e sem autenticacao."""
    configuracao = _settings()

    assert configuracao.app.name == "lukato"
    assert configuracao.app.version == "1.0.0"
    assert configuracao.app.env == "dev"
    assert configuracao.guardrails.enabled is True
    assert configuracao.guardrails.fail_open is False
    assert configuracao.guardrails.redaction_token == "[REDIGIDO]"
    assert configuracao.security.auth_enabled is False
    assert configuracao.db.auto_fallback is True
    assert configuracao.embedding.dimensions == 1024
