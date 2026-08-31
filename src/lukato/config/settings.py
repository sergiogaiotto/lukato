"""Configuracao tipada do lukato (SPEC-0000, secao 13).

Toda a parametrizacao da aplicacao vive aqui: um unico `Settings` composto por
grupos aninhados, alimentado por variaveis de ambiente com prefixo `LUKATO_` e
delimitador `__` (ex.: `LUKATO_LLM__MODEL` -> `settings.llm.model`).

Este modulo nao depende de nenhum outro pacote do projeto: `config` e a base da
pilha e nao pode importar `domain`, `application`, `adapters` ou `interfaces`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = [
    "DEFAULT_MODEL_PRICES",
    "AdWatchSettings",
    "AppSettings",
    "DatabaseSettings",
    "EmbeddingSettings",
    "FinOpsSettings",
    "GuardrailSettings",
    "LLMSettings",
    "ObservabilitySettings",
    "SecuritySettings",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]

# --------------------------------------------------------------------------- #
# Constantes normativas
# --------------------------------------------------------------------------- #

LLM_PROVIDERS: Final[frozenset[str]] = frozenset({"openai_compatible", "echo"})
"""Provedores de LLM aceitos: hub compativel com OpenAI ou eco offline."""

EMBEDDING_PROVIDERS: Final[frozenset[str]] = frozenset({"qwen", "hashing"})
"""Provedores de embedding aceitos: hub Qwen ou hashing deterministico offline."""

PRODUCTION_ENVS: Final[frozenset[str]] = frozenset({"prod", "production"})
"""Valores de `app.env` considerados producao."""

RECOMMENDED_DIMENSIONS: Final[frozenset[int]] = frozenset({256, 512, 768, 1024, 1536, 2048})
"""Dimensoes de embedding usuais; outros valores positivos sao aceitos."""

WEIGHT_SUM_TOLERANCE: Final[float] = 0.001
"""Tolerancia da soma dos pesos de fusao do AdWatch."""

DEFAULT_MODEL_PRICES: Final[dict[str, dict[str, float]]] = {
    "qwen-latest": {"input": 0.0, "output": 0.0},
    "openai/gpt-oss-20b": {"input": 0.0, "output": 0.0},
}
"""Tabela de precos padrao (USD por 1k tokens) dos modelos liberados."""

WEAK_JWT_SECRETS: Final[frozenset[str]] = frozenset(
    {"", "change-me", "troque-este-segredo-em-producao", "secret", "changeme", "lukato"}
)
"""Segredos de JWT proibidos quando a autenticacao roda em producao."""

MIN_JWT_SECRET_CHARS: Final[int] = 32
"""Tamanho minimo do segredo HS256 em producao (RFC 7518 secao 3.2).

Uma lista de valores proibidos nao basta: `JWT_SECRET=abc` nao esta nela e mesmo
assim e uma chave HMAC de 3 bytes, quebravel por forca bruta em segundos. O PyJWT
inclusive emite `InsecureKeyLengthWarning` abaixo de 32 bytes — em producao isso
vira erro de configuracao, nao aviso.
"""

_GROUP_CONFIG: Final[ConfigDict] = ConfigDict(extra="ignore", validate_assignment=True)


# --------------------------------------------------------------------------- #
# Utilidades internas
# --------------------------------------------------------------------------- #


def _as_string_list(value: Any) -> Any:
    """Aceita lista JSON (`["a","b"]`) ou CSV (`a, b`) vinda do ambiente."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0] in "[{":
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


def _blank_secret_to_none(value: Any) -> Any:
    """Converte segredo vazio (`LUKATO_LLM__API_KEY=`) em `None`."""
    if isinstance(value, SecretStr):
        return None if not value.get_secret_value().strip() else value
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _secret_text(secret: SecretStr | None) -> str:
    """Devolve o texto do segredo (sem espacos) ou string vazia."""
    return secret.get_secret_value().strip() if secret is not None else ""


# --------------------------------------------------------------------------- #
# Grupos de configuracao
# --------------------------------------------------------------------------- #


class AppSettings(BaseModel):
    """Identidade e binding HTTP da aplicacao (`LUKATO_APP__*`)."""

    model_config = _GROUP_CONFIG

    name: str = "lukato"
    version: str = "1.0.0"
    env: str = "dev"
    debug: bool = False
    host: str = "0.0.0.0"  # noqa: S104 - bind explicito para execucao em container
    port: int = Field(default=8000, ge=1, le=65535)
    root_path: str = ""
    workers: int = Field(default=1, ge=1)
    docs_assets_base: str = "https://cdn.jsdelivr.net/npm"
    """Origem dos bundles do Swagger UI e do ReDoc.

    O padrao e o CDN publico, que serve a maquina de quem so quer abrir
    `/api/docs` e ler o contrato. Instalacao sem saida para a internet — o caso
    de um cluster corporativo fechado — aponta isto para o espelho interno:

        LUKATO_APP__DOCS_ASSETS_BASE=https://npm.interno.exemplo/npm

    A CSP dessas duas rotas libera exatamente esta origem, e nenhuma outra. Sem
    isto, a pagina responde 200 e fica em branco: a propria resposta proibiria o
    script que ela manda o navegador carregar.
    """

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, value: Any) -> Any:
        """Normaliza o ambiente para minusculas (`PROD` -> `prod`)."""
        return value.strip().lower() if isinstance(value, str) else value


class DatabaseSettings(BaseModel):
    """PostgreSQL + pgvector com fallback SQLite (`LUKATO_DB__*`)."""

    model_config = _GROUP_CONFIG

    url: str = "postgresql+asyncpg://lukato:lukato@localhost:5432/lukato"
    fallback_url: str = "sqlite+aiosqlite:///./lukato.db"
    auto_fallback: bool = True
    echo: bool = False
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    create_all: bool = True

    @property
    def is_sqlite(self) -> bool:
        """Indica se a URL principal aponta para SQLite."""
        return self.url.startswith("sqlite")


class LLMSettings(BaseModel):
    """Hub de LLM compativel com OpenAI (`LUKATO_LLM__*`)."""

    model_config = _GROUP_CONFIG

    provider: str = "openai_compatible"
    # Sem "-lab": o endereco com sufixo existe e responde (401), o que fez uma
    # instalacao com chave valida perseguir o proprio modelo por tres rodadas de
    # diagnostico — a chave estava certa, o host nao. Confirmado em uso real:
    # `qwen-latest` responde neste endereco com a chave do documento de entrega.
    base_url: str = "https://hub-gpus.usto.re/v1"
    api_key: SecretStr | None = None
    model: str = "qwen-latest"
    fallback_model: str = "openai/gpt-oss-20b"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1)
    timeout: float = Field(default=60.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: Any) -> Any:
        """Aceita apenas `openai_compatible` ou `echo`."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in LLM_PROVIDERS:
                raise ValueError(
                    f"provider de LLM invalido: {value!r}; use um de {sorted(LLM_PROVIDERS)}"
                )
            return normalized
        return value

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: Any) -> Any:
        """Trata chave em branco como ausente."""
        return _blank_secret_to_none(value)

    @property
    def api_key_value(self) -> str:
        """Texto da API key ou string vazia quando nao configurada."""
        return _secret_text(self.api_key)

    @property
    def effective_provider(self) -> str:
        """Provedor realmente utilizavel: cai para `echo` sem credencial."""
        if self.provider == "openai_compatible" and not self.api_key_value:
            return "echo"
        return self.provider


class EmbeddingSettings(BaseModel):
    """Servico de embeddings Qwen com fallback hashing (`LUKATO_EMBEDDING__*`)."""

    model_config = _GROUP_CONFIG

    provider: str = "qwen"
    base_url: str = "https://hub-gpus.claro.com.br/embed06b/v1"
    api_key: SecretStr | None = None
    model: str = "Qwen/Qwen3-Embedding-0.6B"
    dimensions: int = 1024
    batch_size: int = Field(default=32, ge=1)
    collection: str = "agente_evidence"

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: Any) -> Any:
        """Aceita apenas `qwen` ou `hashing`."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in EMBEDDING_PROVIDERS:
                raise ValueError(
                    f"provider de embedding invalido: {value!r}; "
                    f"use um de {sorted(EMBEDDING_PROVIDERS)}"
                )
            return normalized
        return value

    @field_validator("api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: Any) -> Any:
        """Trata chave em branco como ausente."""
        return _blank_secret_to_none(value)

    @field_validator("dimensions")
    @classmethod
    def _validate_dimensions(cls, value: int) -> int:
        """Exige dimensao usual do catalogo ou qualquer inteiro positivo."""
        if value in RECOMMENDED_DIMENSIONS or value > 0:
            return value
        raise ValueError(
            f"dimensions deve ser positiva (preferencialmente {sorted(RECOMMENDED_DIMENSIONS)}), "
            f"recebido {value!r}"
        )

    @property
    def api_key_value(self) -> str:
        """Texto da API key ou string vazia quando nao configurada."""
        return _secret_text(self.api_key)

    @property
    def effective_provider(self) -> str:
        """Provedor realmente utilizavel: cai para `hashing` sem endpoint."""
        if self.provider == "qwen" and not self.base_url.strip():
            return "hashing"
        return self.provider


class GuardrailSettings(BaseModel):
    """Limites globais dos guardrails (`LUKATO_GUARDRAILS__*`)."""

    model_config = _GROUP_CONFIG

    enabled: bool = True
    fail_open: bool = False
    redaction_token: str = "[REDIGIDO]"  # noqa: S105 - marcador publico, nao e segredo
    max_input_chars: int = Field(default=32000, ge=1)
    max_output_chars: int = Field(default=32000, ge=1)


class ObservabilitySettings(BaseModel):
    """Langfuse, logging e metricas (`LUKATO_OBSERVABILITY__*`)."""

    model_config = _GROUP_CONFIG

    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    log_level: str = "INFO"
    log_json: bool = False
    metrics_enabled: bool = True

    @field_validator("langfuse_public_key", "langfuse_secret_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: Any) -> Any:
        """Trata credencial em branco como ausente."""
        return _blank_secret_to_none(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> Any:
        """Normaliza o nivel de log para maiusculas."""
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def langfuse_configured(self) -> bool:
        """Indica se ha credenciais suficientes para o tracer real."""
        return bool(
            self.langfuse_enabled
            and _secret_text(self.langfuse_public_key)
            and _secret_text(self.langfuse_secret_key)
        )


class SecuritySettings(BaseModel):
    """Autenticacao, JWT, API keys e CORS (`LUKATO_SECURITY__*`)."""

    model_config = _GROUP_CONFIG

    auth_enabled: bool = False
    jwt_secret: SecretStr = SecretStr("change-me")
    jwt_algorithm: str = "HS256"
    jwt_expires_seconds: int = Field(default=3600, ge=1)
    api_key_header: str = "X-API-Key"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> Any:
        """Aceita lista JSON (`["*"]`) ou CSV (`https://a, https://b`)."""
        return _as_string_list(value)

    @property
    def jwt_secret_value(self) -> str:
        """Texto do segredo de assinatura do JWT."""
        return self.jwt_secret.get_secret_value()


class FinOpsSettings(BaseModel):
    """Precificacao e controle de custo (`LUKATO_FINOPS__*`)."""

    model_config = _GROUP_CONFIG

    enabled: bool = True
    currency: str = "USD"
    prices: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            model: dict(price) for model, price in DEFAULT_MODEL_PRICES.items()
        }
    )
    default_input_usd_per_1k: float = Field(default=0.0, ge=0.0)
    default_output_usd_per_1k: float = Field(default=0.0, ge=0.0)

    def price_for(self, model: str) -> tuple[float, float]:
        """Devolve `(input_usd_per_1k, output_usd_per_1k)` do modelo informado."""
        entry = self.prices.get(model) or DEFAULT_MODEL_PRICES.get(model)
        if entry is None:
            return (self.default_input_usd_per_1k, self.default_output_usd_per_1k)
        return (
            float(entry.get("input", self.default_input_usd_per_1k)),
            float(entry.get("output", self.default_output_usd_per_1k)),
        )


class AdWatchSettings(BaseModel):
    """Janelamento, pesos de fusao e limiares do AdWatch (`LUKATO_ADWATCH__*`)."""

    model_config = _GROUP_CONFIG

    window_sizes: Annotated[list[float], NoDecode] = Field(
        default_factory=lambda: [15.0, 30.0, 60.0]
    )
    window_stride: float = Field(default=5.0, gt=0.0)
    weight_lexical: float = Field(default=0.40, ge=0.0, le=1.0)
    weight_semantic: float = Field(default=0.25, ge=0.0, le=1.0)
    weight_ocr: float = Field(default=0.15, ge=0.0, le=1.0)
    weight_visual: float = Field(default=0.15, ge=0.0, le=1.0)
    weight_duration: float = Field(default=0.05, ge=0.0, le=1.0)
    accept_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    top_k_retrieval: int = Field(default=10, ge=1)
    top_k_rerank: int = Field(default=3, ge=1)
    workdir: str = "./var/adwatch"
    # Teto de `POST /media/upload`, em MiB. O upload grava em `<workdir>/uploads`,
    # entao o limite real e o disco do volume — este numero so impede que um
    # arquivo errado (uma imagem de disco, um zip) ocupe o volume inteiro.
    upload_max_mb: int = Field(default=2048, ge=1)

    @field_validator("window_sizes", mode="before")
    @classmethod
    def _parse_windows(cls, value: Any) -> Any:
        """Aceita lista JSON (`[15.0,30.0]`) ou CSV (`15,30`)."""
        return _as_string_list(value)

    @model_validator(mode="after")
    def _check_weights_and_thresholds(self) -> AdWatchSettings:
        """Garante soma dos pesos igual a 1.0 e limiares coerentes."""
        total = sum(self.weights().values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"a soma dos pesos do AdWatch deve ser 1.0 +/- {WEIGHT_SUM_TOLERANCE}, "
                f"obtido {total:.6f}"
            )
        if self.review_threshold >= self.accept_threshold:
            raise ValueError(
                f"review_threshold ({self.review_threshold}) deve ser menor que "
                f"accept_threshold ({self.accept_threshold})"
            )
        return self

    def weights(self) -> dict[str, float]:
        """Pesos de fusao por sinal, na ordem normativa da SPEC-0010."""
        return {
            "lexical": self.weight_lexical,
            "semantic": self.weight_semantic,
            "ocr": self.weight_ocr,
            "visual": self.weight_visual,
            "duration": self.weight_duration,
        }


# --------------------------------------------------------------------------- #
# Raiz
# --------------------------------------------------------------------------- #


class Settings(BaseSettings):
    """Configuracao raiz do lukato, montada a partir do ambiente e do `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="LUKATO_",
        env_nested_delimiter="__",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    guardrails: GuardrailSettings = Field(default_factory=GuardrailSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    finops: FinOpsSettings = Field(default_factory=FinOpsSettings)
    adwatch: AdWatchSettings = Field(default_factory=AdWatchSettings)

    @model_validator(mode="after")
    def _check_production_secrets(self) -> Settings:
        """Exige autenticacao ligada e segredo de JWT forte em producao (SPEC-0006)."""
        if not self.is_production:
            return self

        if not self.security.auth_enabled:
            raise ValueError(
                "LUKATO_SECURITY__AUTH_ENABLED=false em producao expoe a API inteira "
                "como root anonimo: toda rota passaria a responder sem credencial. "
                "Ligue a autenticacao (LUKATO_SECURITY__AUTH_ENABLED=true) ou nao "
                "declare este ambiente como producao (LUKATO_APP__ENV)"
            )

        if not self.guardrails.enabled:
            raise ValueError(
                "LUKATO_GUARDRAILS__ENABLED=false em producao desliga a trinca "
                "guardrail de entrada -> system prompt -> guardrail de saida em TODOS "
                "os modulos: PII, segredos e prompt injection passariam direto para o "
                "provedor. E uma chave geral de emergencia para diagnostico local. "
                "Para afrouxar um modulo especifico, troque a politica dele em vez de "
                "desligar a plataforma inteira"
            )

        secret = self.security.jwt_secret_value.strip()
        remedio = "gere um segredo forte com: openssl rand -hex 32"

        if secret in WEAK_JWT_SECRETS:
            raise ValueError(
                f"LUKATO_SECURITY__JWT_SECRET esta com um valor conhecido/padrao e nao "
                f"pode ir para producao com autenticacao habilitada; {remedio}"
            )
        if len(secret) < MIN_JWT_SECRET_CHARS:
            raise ValueError(
                f"LUKATO_SECURITY__JWT_SECRET tem {len(secret)} caracteres; em producao "
                f"o minimo e {MIN_JWT_SECRET_CHARS} (RFC 7518 secao 3.2 para HS256). "
                f"Uma chave HMAC curta e quebravel por forca bruta; {remedio}"
            )
        return self

    @property
    def is_production(self) -> bool:
        """Indica se o ambiente configurado e de producao."""
        return self.app.env in PRODUCTION_ENVS

    @property
    def llm_configured(self) -> bool:
        """Indica se ha credencial para falar com o hub de LLM."""
        return bool(self.llm.api_key) and self.llm.provider == "openai_compatible"

    def database_url(self, prefer_fallback: bool = False) -> str:
        """URL de banco a usar: principal ou, sob demanda/ausencia, o fallback."""
        if prefer_fallback and self.db.fallback_url:
            return self.db.fallback_url
        if not self.db.url.strip():
            return self.db.fallback_url
        return self.db.url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devolve a instancia unica de `Settings` (memoizada no processo)."""
    return Settings()


def reset_settings_cache() -> None:
    """Limpa a memoizacao de `get_settings` (usado em testes)."""
    get_settings.cache_clear()
