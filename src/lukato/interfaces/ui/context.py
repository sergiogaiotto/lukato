"""Contexto base de toda pagina do console (SPEC-0009 secoes 3, 4, 7 e 8).

Uma pagina do console e sempre a mesma moldura — topbar, sidebar, barra de status
e painel de contexto — com um miolo diferente. Este modulo produz essa moldura em
uma unica funcao, :func:`base_context`, para que nenhuma rota precise saber como
se monta a navegacao, de onde vem a saude dos provedores ou como se mascara um
segredo.

Tres decisoes valem ser ditas em voz alta:

1. **Nada aqui pode derrubar uma pagina.** Saude e custo sao enfeites de moldura:
   se o banco esta fora, a barra de status mostra "down" e a pagina continua
   renderizando. Toda consulta auxiliar e envolvida por
   :func:`_degrade`, que registra o erro e devolve o valor neutro.
2. **A saude e cacheada por poucos segundos.** `Container.health()` toca banco,
   LLM e embeddings; refazer essa sondagem a cada clique transformaria a barra de
   status em um gerador de latencia.
3. **Segredo nao chega ao template.** `settings_public` e montado campo a campo,
   com `SecretStr` passando obrigatoriamente por :func:`mask_secret` e URLs de
   banco por :func:`mask_url`. O template nunca recebe o objeto `Settings`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Final, TypeVar

from pydantic import SecretStr
from starlette.requests import Request

from lukato.application.container import (
    STATUS_DEGRADED,
    STATUS_DOWN,
    STATUS_OK,
    Container,
)
from lukato.application.use_cases.finops import CostFilter, GetCostSummary
from lukato.config import Settings, get_logger
from lukato.domain.errors import LukatoError
from lukato.domain.models.identity import Principal
from lukato.domain.types import Json, utcnow

__all__ = [
    "COST_WINDOW_HOURS",
    "ENTITY_BY_ROUTE",
    "HEALTH_TTL_SECONDS",
    "NAV_SECTIONS",
    "NOT_CONFIGURED",
    "SECTION_ADMIN",
    "SECTION_COCKPIT",
    "SECTION_CONFIG",
    "SECTION_FEATURE",
    "SECTION_MONITORING",
    "SERIES_SLOTS",
    "SUPPORTED_ENTITIES",
    "CostEntry",
    "CostView",
    "Crumb",
    "HealthIndicator",
    "HealthView",
    "NavItem",
    "NavSection",
    "SettingsItem",
    "SettingsSection",
    "base_context",
    "build_nav",
    "context_template_for",
    "crumbs",
    "entity_for_route",
    "load_entity",
    "mask_secret",
    "mask_url",
    "settings_public",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

NOT_CONFIGURED: Final[str] = "(nao configurado)"
"""Texto mostrado no lugar de um segredo que esta instalacao nao definiu."""

HIDDEN: Final[str] = "(oculto)"
"""Texto usado quando o segredo e curto demais para revelar qualquer parte."""

_MASK_MIN_LENGTH: Final[int] = 8
"""Abaixo deste tamanho, mostrar os quatro ultimos caracteres ja e vazar demais."""

_MASK_TAIL: Final[int] = 4
"""Quantidade de caracteres finais preservados na mascara (`sk-…1234`)."""

_MASK_PREFIX_MAX: Final[int] = 5
"""Tamanho maximo do prefixo tecnico preservado (`sk-`, `lk_`, `pk-`)."""

_ELLIPSIS: Final[str] = "…"

HEALTH_TTL_SECONDS: Final[float] = 10.0
"""Validade do retrato de saude usado pela barra de status."""

COST_WINDOW_HOURS: Final[int] = 24
"""Janela do resumo de custo mostrado na barra de status."""

SERIES_SLOTS: Final[int] = 8
"""Quantidade de cores de serie disponiveis para os pontos de custo por modulo."""

SECTION_COCKPIT: Final[str] = ""
SECTION_FEATURE: Final[str] = "FUNCIONALIDADE"
SECTION_CONFIG: Final[str] = "CONFIGURAÇÕES"
SECTION_MONITORING: Final[str] = "MONITORAMENTO"
SECTION_ADMIN: Final[str] = "ADMINISTRATIVO"


# ---------------------------------------------------------------------------
# Navegacao
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class NavItem:
    """Um link da sidebar: rotulo, rota, icone e posicao dentro da secao."""

    label: str
    route: str
    icon: str
    order: int = 100
    section: str = SECTION_FEATURE
    active: bool = False

    def matches(self, route: str) -> bool:
        """True quando `route` e este item ou uma pagina filha dele.

        `/modules` fica aceso em `/modules/adwatch`; a raiz `/` so acende em `/`,
        senao ela ficaria ativa em toda pagina do console.
        """
        current = route or "/"
        if self.route == "/":
            return current == "/"
        return current == self.route or current.startswith(f"{self.route}/")


@dataclass(slots=True)
class NavSection:
    """Um grupo rotulado de itens da sidebar; titulo vazio nao imprime cabecalho."""

    title: str
    items: list[NavItem] = field(default_factory=list)


NAV_SECTIONS: Final[tuple[NavSection, ...]] = (
    NavSection(
        SECTION_COCKPIT,
        [NavItem("Cockpit", "/", "home", order=10, section=SECTION_COCKPIT)],
    ),
    NavSection(
        SECTION_FEATURE,
        [
            NavItem("Módulos", "/modules", "blocks", order=10, section=SECTION_FEATURE),
            NavItem("Execuções", "/runs", "activity", order=20, section=SECTION_FEATURE),
            NavItem("Conhecimento", "/knowledge", "book", order=30, section=SECTION_FEATURE),
            NavItem("AdWatch", "/adwatch", "film", order=40, section=SECTION_FEATURE),
        ],
    ),
    NavSection(
        SECTION_CONFIG,
        [
            NavItem("Prompts", "/prompts", "message", order=10, section=SECTION_CONFIG),
            NavItem("Guardrails", "/guardrails", "shield", order=20, section=SECTION_CONFIG),
            NavItem("Registry", "/registry", "plug", order=30, section=SECTION_CONFIG),
        ],
    ),
    NavSection(
        SECTION_MONITORING,
        [
            NavItem("FinOps", "/finops", "coins", order=10, section=SECTION_MONITORING),
            NavItem(
                "Observabilidade",
                "/observability",
                "pulse",
                order=20,
                section=SECTION_MONITORING,
            ),
        ],
    ),
    NavSection(
        SECTION_ADMIN,
        [
            NavItem("Identidade", "/identity", "users", order=10, section=SECTION_ADMIN),
            NavItem("Configurações", "/settings", "sliders", order=20, section=SECTION_ADMIN),
        ],
    ),
)
"""Navegacao normativa do console; modulos registrados apenas **acrescentam** itens."""


def _module_nav_items(container: Container) -> list[NavItem]:
    """Itens de menu publicados pelos building blocks via `BaseModule.ui().nav`.

    Um descritor defeituoso nao pode apagar a sidebar inteira: a falha e
    registrada e o item simplesmente nao aparece.
    """
    collected: list[NavItem] = []
    try:
        descriptors = container.registry.describe()
    except Exception as exc:  # pragma: no cover - registry defeituoso
        _logger.warning("ui_nav_describe_failed", error=f"{type(exc).__name__}: {exc}")
        return collected

    for descriptor in descriptors:
        for entry in getattr(getattr(descriptor, "ui", None), "nav", []) or []:
            label = str(getattr(entry, "label", "") or "").strip()
            endpoint = str(getattr(entry, "endpoint", "") or "").strip()
            if not label or not endpoint:
                continue
            collected.append(
                NavItem(
                    label=label,
                    route=endpoint,
                    icon=str(getattr(entry, "icon", "") or "blocks"),
                    order=int(getattr(entry, "order", 100) or 100),
                    section=str(getattr(entry, "section", SECTION_FEATURE) or SECTION_FEATURE),
                )
            )
    return collected


def build_nav(container: Container, active: str) -> list[NavSection]:
    """Monta a sidebar: secoes normativas + itens dos modulos, com o item ativo marcado.

    Itens de modulo que repetem uma rota ja presente sao descartados — o menu
    canonico vence. Uma secao desconhecida vinda de um modulo entra no fim, com o
    titulo em caixa alta.
    """
    grouped: dict[str, list[NavItem]] = {}
    order: list[str] = []
    for section in NAV_SECTIONS:
        order.append(section.title)
        grouped[section.title] = [replace(item) for item in section.items]

    known_routes = {item.route for items in grouped.values() for item in items}
    for item in sorted(_module_nav_items(container), key=lambda entry: (entry.order, entry.label)):
        if item.route in known_routes:
            continue
        title = item.section if item.section in grouped else item.section.upper()
        if title not in grouped:
            grouped[title] = []
            order.append(title)
        grouped[title].append(item)
        known_routes.add(item.route)

    sections: list[NavSection] = []
    for title in order:
        items = sorted(grouped[title], key=lambda entry: (entry.order, entry.label))
        for item in items:
            item.active = item.matches(active)
        if items:
            sections.append(NavSection(title=title, items=items))
    return sections


# ---------------------------------------------------------------------------
# Trilha de navegacao
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Crumb:
    """Um degrau da trilha da topbar; sem `href` e o degrau atual."""

    label: str
    href: str | None = None


CrumbLike = Crumb | tuple[str, str | None] | tuple[str] | str
"""Formas aceitas por :func:`crumbs` para descrever um degrau."""


def crumbs(items: Iterable[CrumbLike] | None) -> list[Crumb]:
    """Normaliza a trilha recebida da rota em uma lista de :class:`Crumb`."""
    normalized: list[Crumb] = []
    for item in items or ():
        if isinstance(item, Crumb):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append(Crumb(item))
        else:
            label, *rest = item
            href = str(rest[0]) if rest and rest[0] else None
            normalized.append(Crumb(str(label), href))
    return normalized


# ---------------------------------------------------------------------------
# Saude
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HealthIndicator:
    """Um ponto colorido da barra de status."""

    key: str
    label: str
    status: str
    detail: str

    @property
    def is_ok(self) -> bool:
        """True quando o componente esta plenamente disponivel."""
        return self.status == STATUS_OK


@dataclass(frozen=True, slots=True)
class HealthView:
    """Saude da instalacao pronta para a tela.

    `indicators` e a sequencia curta da barra de status (guardrails, langfuse,
    otel, db); `components` traz o relatorio completo, usado pelo cockpit e pela
    pagina de configuracoes.
    """

    indicators: tuple[HealthIndicator, ...] = ()
    components: Mapping[str, HealthIndicator] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Pior estado entre os componentes: `ok`, `degraded` ou `down`."""
        states = {component.status for component in self.components.values()}
        if STATUS_DOWN in states:
            return STATUS_DOWN
        if STATUS_DEGRADED in states:
            return STATUS_DEGRADED
        return STATUS_OK

    def get(self, key: str) -> HealthIndicator:
        """Componente pelo nome, com um indicador neutro quando nao ha leitura."""
        return self.components.get(key, HealthIndicator(key, key, STATUS_DEGRADED, "sem leitura"))


_COMPONENT_LABELS: Final[dict[str, str]] = {
    "database": "db",
    "registry": "registry",
    "llm": "llm",
    "embeddings": "embeddings",
    "tracer": "otel",
    "guardrails": "guardrails",
    "langfuse": "langfuse",
}
"""Rotulo curto de cada componente na barra de status."""

_STATUSBAR_ORDER: Final[tuple[str, ...]] = ("guardrails", "langfuse", "tracer", "database")
"""Ordem normativa dos indicadores a esquerda da barra de status."""

_HEALTH_CACHE: dict[int, tuple[float, Container, dict[str, dict[str, str]]]] = {}
"""Cache TTL do relatorio bruto de saude, por container.

A entrada guarda o proprio container junto do relatorio: a chave e `id()`, e um
container coletado pelo GC pode ceder o seu endereco a outro. Comparar a
identidade antes de servir o cache fecha essa porta — e a referencia forte
tambem impede a reciclagem enquanto a entrada existe."""

_HEALTH_CACHE_MAX: Final[int] = 8
"""Teto de containers memorizados (testes criam varios; producao usa um)."""

_HEALTH_LOCK: Final[asyncio.Lock] = asyncio.Lock()
"""Serializa a sondagem para que dez abas nao disparem dez `health()`."""


async def _raw_health(container: Container) -> dict[str, dict[str, str]]:
    """Le `Container.health()` com cache curto e sem jamais propagar excecao."""
    key = id(container)

    def fresh() -> dict[str, dict[str, str]] | None:
        """Entrada valida para este container e ainda dentro do TTL."""
        cached = _HEALTH_CACHE.get(key)
        if cached is None or cached[1] is not container:
            return None
        return cached[2] if time.monotonic() - cached[0] < HEALTH_TTL_SECONDS else None

    hit = fresh()
    if hit is not None:
        return hit

    async with _HEALTH_LOCK:
        hit = fresh()
        if hit is not None:
            return hit
        try:
            report = await container.health()
        except Exception as exc:  # pragma: no cover - sonda defeituosa
            _logger.warning("ui_health_failed", error=f"{type(exc).__name__}: {exc}")
            report = {}
        if len(_HEALTH_CACHE) >= _HEALTH_CACHE_MAX:
            _HEALTH_CACHE.clear()
        _HEALTH_CACHE[key] = (time.monotonic(), container, report)
        return report


def _indicator(key: str, entry: Mapping[str, str] | None) -> HealthIndicator:
    """Converte uma entrada do relatorio de saude em indicador de tela."""
    data = entry or {}
    return HealthIndicator(
        key=key,
        label=_COMPONENT_LABELS.get(key, key),
        status=str(data.get("status") or STATUS_DEGRADED),
        detail=str(data.get("detail") or "sem leitura"),
    )


def _guardrail_indicator(settings: Settings) -> HealthIndicator:
    """Estado dos guardrails: desligado e degradacao consciente, nao falha."""
    guardrails = settings.guardrails
    if not guardrails.enabled:
        return HealthIndicator(
            "guardrails", "guardrails", STATUS_DEGRADED, "desligado nesta instalacao"
        )
    mode = "fail-open" if guardrails.fail_open else "fail-closed"
    return HealthIndicator("guardrails", "guardrails", STATUS_OK, f"ativo ({mode})")


def _langfuse_indicator(settings: Settings, tracer: HealthIndicator) -> HealthIndicator:
    """Estado do Langfuse, cruzando a configuracao com o tracer efetivamente ativo."""
    observability = settings.observability
    if not observability.langfuse_enabled:
        return HealthIndicator("langfuse", "langfuse", STATUS_DEGRADED, "desligado")
    if tracer.status == STATUS_OK:
        return HealthIndicator("langfuse", "langfuse", STATUS_OK, observability.langfuse_host)
    return HealthIndicator(
        "langfuse", "langfuse", STATUS_DOWN, "habilitado, mas o tracer nao subiu"
    )


async def _health_view(container: Container) -> HealthView:
    """Monta a visao de saude usada pela barra de status e pelo cockpit."""
    report = await _raw_health(container)
    components: dict[str, HealthIndicator] = {
        key: _indicator(key, report.get(key))
        for key in ("database", "registry", "llm", "embeddings", "tracer")
    }
    settings = container.settings
    components["guardrails"] = _guardrail_indicator(settings)
    components["langfuse"] = _langfuse_indicator(settings, components["tracer"])
    indicators = tuple(components[key] for key in _STATUSBAR_ORDER if key in components)
    return HealthView(indicators=indicators, components=components)


# ---------------------------------------------------------------------------
# Custo
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CostEntry:
    """Uma fatia do custo do periodo (por modulo ou por modelo)."""

    key: str
    label: str
    usd: float
    share: float = 0.0
    series: int = 1

    @property
    def series_class(self) -> str:
        """Classe CSS do ponto colorido desta fatia."""
        return f"lk-dot--s{self.series}"

    @property
    def width(self) -> str:
        """Largura da barra proporcional, pronta para `style="width: ..."`."""
        return f"{max(0.0, min(100.0, self.share * 100.0)):.1f}%"


@dataclass(frozen=True, slots=True)
class CostView:
    """Resumo de custo da janela corrente, pronto para a barra de status."""

    total_usd: float = 0.0
    total_tokens: int = 0
    runs: int = 0
    modules: tuple[CostEntry, ...] = ()
    models: tuple[CostEntry, ...] = ()
    window_hours: int = COST_WINDOW_HOURS
    available: bool = True

    @property
    def is_empty(self) -> bool:
        """True quando nao houve consumo algum na janela."""
        return self.runs == 0 and self.total_usd <= 0.0


def _cost_entries(amounts: Mapping[str, float], total: float) -> tuple[CostEntry, ...]:
    """Ordena as fatias da maior para a menor e calcula a participacao de cada uma."""
    ranked = sorted(amounts.items(), key=lambda pair: (-float(pair[1] or 0.0), pair[0]))
    reference = (
        total if total > 0 else max((float(value or 0.0) for _, value in ranked), default=0.0)
    )
    entries: list[CostEntry] = []
    for index, (key, value) in enumerate(ranked):
        usd = float(value or 0.0)
        entries.append(
            CostEntry(
                key=key,
                label=key,
                usd=usd,
                share=(usd / reference) if reference > 0 else 0.0,
                series=(index % SERIES_SLOTS) + 1,
            )
        )
    return tuple(entries)


async def _cost_view(container: Container, principal: Principal) -> CostView:
    """Resumo de custo das ultimas horas; indisponibilidade vira `available=False`."""
    if not container.settings.finops.enabled:
        return CostView(available=False)
    since = utcnow() - timedelta(hours=COST_WINDOW_HOURS)
    summary = await GetCostSummary(container).execute(
        CostFilter(since=since, tenant_id=principal.tenant_id), principal
    )
    total = float(summary.total_usd)
    return CostView(
        total_usd=total,
        total_tokens=int(summary.total_tokens),
        runs=int(summary.runs),
        modules=_cost_entries(summary.by_module, total),
        models=_cost_entries(summary.by_model, total),
    )


# ---------------------------------------------------------------------------
# Configuracao publica (segredos mascarados)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SettingsItem:
    """Um par rotulo/valor da pagina de configuracoes; `value` ja e texto seguro."""

    label: str
    value: str
    secret: bool = False
    hint: str = ""


@dataclass(frozen=True, slots=True)
class SettingsSection:
    """Grupo de ajustes exibido como um cartao na pagina de configuracoes."""

    key: str
    title: str
    items: tuple[SettingsItem, ...] = ()


def _reveal(value: Any) -> str:
    """Extrai o texto de um `SecretStr` ou de uma string, sem levantar."""
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value() or ""
    return str(value)


def mask_secret(value: Any) -> str:
    """Mascara um segredo para exibicao: `sk-…1234` ou `(nao configurado)`.

    Regras (SPEC-0009 secao 10): o valor nunca aparece inteiro; preserva-se o
    prefixo tecnico (`sk-`, `lk_`, `pk-`) porque ele identifica o provedor sem
    revelar nada, e os quatro ultimos caracteres, que permitem conferir *qual*
    chave esta em uso. Segredos curtos demais para essa conta viram
    `(oculto)` — mostrar quatro de seis caracteres seria entregar o segredo.
    """
    raw = _reveal(value).strip()
    if not raw:
        return NOT_CONFIGURED
    if len(raw) < _MASK_MIN_LENGTH:
        return HIDDEN
    prefix = ""
    for separator in ("-", "_"):
        head, found, _ = raw.partition(separator)
        if found and len(head) < _MASK_PREFIX_MAX:
            prefix = f"{head}{separator}"
            break
    return f"{prefix}{_ELLIPSIS}{raw[-_MASK_TAIL:]}"


def mask_url(value: Any) -> str:
    """Remove a senha de uma URL de conexao: `postgresql://lukato:***@host/db`."""
    url = _reveal(value).strip()
    if not url:
        return NOT_CONFIGURED
    scheme, separator, remainder = url.partition("://")
    if not separator or "@" not in remainder:
        return url
    credentials, _, host = remainder.partition("@")
    user, has_password, _ = credentials.partition(":")
    if not has_password:
        return url
    return f"{scheme}://{user}:{'*' * 3}@{host}"


def _flag(value: bool) -> str:
    """Booleano em pt-BR para a tela de configuracoes."""
    return "sim" if value else "nao"


def settings_public(settings: Settings) -> tuple[SettingsSection, ...]:
    """Retrato **publico** da configuracao: nenhum segredo em texto claro.

    Cada campo e escrito a mao de proposito. Serializar `Settings` inteiro e
    filtrar depois inverteria o onus: bastaria alguem acrescentar um campo
    sensivel para ele aparecer na tela sem que ninguem decidisse isso.
    """
    app = settings.app
    database = settings.db
    llm = settings.llm
    embedding = settings.embedding
    guardrails = settings.guardrails
    observability = settings.observability
    security = settings.security
    finops = settings.finops
    adwatch = settings.adwatch

    return (
        SettingsSection(
            "app",
            "Aplicação",
            (
                SettingsItem("Nome", app.name),
                SettingsItem("Versão", app.version),
                SettingsItem("Ambiente", app.env),
                SettingsItem("Depuração", _flag(app.debug)),
                SettingsItem("Raiz HTTP", app.root_path or "/"),
                SettingsItem("Porta", str(app.port)),
            ),
        ),
        SettingsSection(
            "db",
            "Banco de dados",
            (
                SettingsItem("URL", mask_url(database.url), secret=True, hint="senha removida"),
                SettingsItem("Fallback", mask_url(database.fallback_url), secret=True),
                SettingsItem("Fallback automático", _flag(database.auto_fallback)),
                SettingsItem("Cria schema no boot", _flag(database.create_all)),
                SettingsItem("Echo SQL", _flag(database.echo)),
            ),
        ),
        SettingsSection(
            "llm",
            "Provedor de LLM",
            (
                SettingsItem("Provedor", llm.provider),
                SettingsItem("Endpoint", llm.base_url),
                SettingsItem("Modelo", llm.model),
                SettingsItem("Modelo de reserva", llm.fallback_model),
                SettingsItem("Chave de API", mask_secret(llm.api_key), secret=True),
                SettingsItem("Temperatura", f"{llm.temperature:.2f}"),
                SettingsItem("Máximo de tokens", str(llm.max_tokens)),
                SettingsItem("Tempo limite", f"{llm.timeout:.0f}s"),
                SettingsItem("Tentativas", str(llm.max_retries)),
            ),
        ),
        SettingsSection(
            "embedding",
            "Embeddings",
            (
                SettingsItem("Provedor", embedding.provider),
                SettingsItem("Endpoint", embedding.base_url),
                SettingsItem("Modelo", embedding.model),
                SettingsItem("Chave de API", mask_secret(embedding.api_key), secret=True),
                SettingsItem("Dimensões", str(embedding.dimensions)),
                SettingsItem("Lote", str(embedding.batch_size)),
                SettingsItem("Coleção padrão", embedding.collection),
            ),
        ),
        SettingsSection(
            "guardrails",
            "Guardrails",
            (
                SettingsItem("Ativos", _flag(guardrails.enabled)),
                SettingsItem(
                    "Modo de falha",
                    "fail-open" if guardrails.fail_open else "fail-closed",
                    hint="fail-closed bloqueia quando o motor falha",
                ),
                SettingsItem("Marcador de redação", guardrails.redaction_token),
                SettingsItem("Máximo de entrada", f"{guardrails.max_input_chars} caracteres"),
                SettingsItem("Máximo de saída", f"{guardrails.max_output_chars} caracteres"),
            ),
        ),
        SettingsSection(
            "observability",
            "Observabilidade",
            (
                SettingsItem("Langfuse", _flag(observability.langfuse_enabled)),
                SettingsItem("Host do Langfuse", observability.langfuse_host),
                SettingsItem(
                    "Chave pública",
                    mask_secret(observability.langfuse_public_key),
                    secret=True,
                ),
                SettingsItem(
                    "Chave secreta",
                    mask_secret(observability.langfuse_secret_key),
                    secret=True,
                ),
                SettingsItem("Nível de log", observability.log_level),
                SettingsItem("Log em JSON", _flag(observability.log_json)),
                SettingsItem("Métricas", _flag(observability.metrics_enabled)),
            ),
        ),
        SettingsSection(
            "security",
            "Segurança",
            (
                SettingsItem("Autenticação", _flag(security.auth_enabled)),
                SettingsItem("Algoritmo JWT", security.jwt_algorithm),
                SettingsItem("Segredo JWT", mask_secret(security.jwt_secret), secret=True),
                SettingsItem("Validade do JWT", f"{security.jwt_expires_seconds}s"),
                SettingsItem("Cabeçalho de API key", security.api_key_header),
                SettingsItem("Origens CORS", ", ".join(security.cors_origins) or "(nenhuma)"),
            ),
        ),
        SettingsSection(
            "finops",
            "FinOps",
            (
                SettingsItem("Ativo", _flag(finops.enabled)),
                SettingsItem("Moeda", finops.currency),
                SettingsItem("Modelos precificados", str(len(finops.prices))),
                SettingsItem(
                    "Preço padrão de entrada",
                    f"{finops.default_input_usd_per_1k:.5f} / 1k tokens",
                ),
                SettingsItem(
                    "Preço padrão de saída",
                    f"{finops.default_output_usd_per_1k:.5f} / 1k tokens",
                ),
            ),
        ),
        SettingsSection(
            "adwatch",
            "AdWatch",
            (
                SettingsItem(
                    "Janelas",
                    ", ".join(f"{size:.0f}s" for size in adwatch.window_sizes) or "(nenhuma)",
                ),
                SettingsItem("Passo da janela", f"{adwatch.window_stride:.0f}s"),
                SettingsItem(
                    "Pesos",
                    (
                        f"léxico {adwatch.weight_lexical:.2f} · "
                        f"semântico {adwatch.weight_semantic:.2f} · "
                        f"ocr {adwatch.weight_ocr:.2f} · "
                        f"visual {adwatch.weight_visual:.2f} · "
                        f"duração {adwatch.weight_duration:.2f}"
                    ),
                ),
                SettingsItem("Limiar de aceite", f"{adwatch.accept_threshold:.2f}"),
                SettingsItem("Limiar de revisão", f"{adwatch.review_threshold:.2f}"),
                SettingsItem(
                    "Top-K",
                    f"recuperação {adwatch.top_k_retrieval} · rerank {adwatch.top_k_rerank}",
                ),
                SettingsItem("Diretório de trabalho", adwatch.workdir),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Painel de contexto
# ---------------------------------------------------------------------------
SUPPORTED_ENTITIES: Final[tuple[str, ...]] = (
    "module",
    "prompt",
    "guardrail",
    "run",
    "document",
    "commercial",
    "detection",
    "apikey",
    "user",
)
"""Entidades que o painel de contexto sabe detalhar (SPEC-0009 secao 7)."""

ENTITY_BY_ROUTE: Final[dict[str, str]] = {
    "/": "run",
    "/modules": "module",
    "/modules/{slug}": "module",
    "/prompts": "prompt",
    "/guardrails": "guardrail",
    "/runs": "run",
    "/runs/{run_id}": "run",
    "/knowledge": "document",
    "/finops": "run",
    "/observability": "run",
    "/registry": "module",
    "/adwatch": "commercial",
    "/adwatch/commercials": "commercial",
    "/adwatch/detections": "detection",
    "/identity": "user",
    "/settings": "module",
}
"""Entidade que `?sel=<id>` seleciona em cada pagina do console."""


def entity_for_route(route: str) -> str:
    """Entidade padrao do painel de contexto para a rota informada."""
    return ENTITY_BY_ROUTE.get(route, "module")


def context_template_for(entity: str) -> str:
    """Template do fragmento de contexto de uma entidade, com queda para o padrao."""
    key = (entity or "").strip().lower()
    return f"context/{key}.html" if key in SUPPORTED_ENTITIES else "context/default.html"


async def load_entity(
    container: Container, entity: str, entity_id: str, principal: Principal
) -> Any | None:
    """Carrega o item selecionado pelo painel de contexto, sempre por caso de uso.

    Devolve `None` quando a entidade nao existe, nao e suportada ou o principal
    nao pode ve-la: o painel entao mostra o estado padrao, e a pagina continua de
    pe. Nenhum repositorio e aberto aqui — a regra 7 da SPEC-0000 vale igual para
    o painel lateral e para a rota principal.
    """
    key = (entity or "").strip().lower()
    identifier = (entity_id or "").strip()
    if key not in SUPPORTED_ENTITIES or not identifier:
        return None

    loader = _ENTITY_LOADERS.get(key)
    if loader is None:  # pragma: no cover - mapa e exaustivo sobre SUPPORTED_ENTITIES
        return None
    try:
        return await loader(container, identifier, principal)
    except LukatoError as exc:
        _logger.info("ui_context_miss", entity=key, id=identifier, code=exc.code)
        return None
    except Exception as exc:  # pragma: no cover - falha inesperada de leitura
        _logger.warning(
            "ui_context_failed", entity=key, id=identifier, error=f"{type(exc).__name__}: {exc}"
        )
        return None


async def _load_module(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega uma definicao de modulo por id ou slug."""
    from lukato.application.use_cases.modules import GetModule

    return await GetModule(container).execute(identifier, principal)


async def _load_prompt(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega um prompt por id ou slug."""
    from lukato.application.use_cases.prompts import GetPrompt

    return await GetPrompt(container).execute(identifier, principal)


async def _load_guardrail(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega uma politica de guardrail por id ou slug."""
    from lukato.application.use_cases.guardrails import GetPolicy

    return await GetPolicy(container).execute(identifier, principal)


async def _load_run(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega uma execucao pelo identificador."""
    from lukato.application.use_cases.runs import GetRun

    return await GetRun(container).execute(identifier, principal)


async def _load_document(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega um documento da base de conhecimento."""
    from lukato.application.use_cases.knowledge import GetDocument

    return await GetDocument(container).execute(identifier, principal)


async def _load_commercial(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega um comercial do catalogo do AdWatch."""
    from lukato.application.use_cases.adwatch import GetCommercial

    return await GetCommercial(container).execute(identifier, principal)


async def _load_detection(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega uma deteccao do AdWatch."""
    from lukato.application.use_cases.adwatch import GetDetection

    return await GetDetection(container).execute(identifier, principal)


async def _load_user(container: Container, identifier: str, principal: Principal) -> Any:
    """Carrega um usuario por id ou e-mail."""
    from lukato.application.use_cases.identity import GetUser

    return await GetUser(container).execute(identifier, principal)


async def _load_api_key(container: Container, identifier: str, principal: Principal) -> Any | None:
    """Localiza uma chave de API na listagem publica (sem segredo e sem hash).

    A porta `ApiKeyRepository` nao expoe leitura por id; a listagem ja devolve a
    forma publica da chave, que e exatamente o que o painel pode mostrar.
    """
    from lukato.application.use_cases.identity import ApiKeyFilter, ListApiKeys

    page = await ListApiKeys(container).execute(ApiKeyFilter(limit=200), principal)
    return next((item for item in page.items if item.id == identifier), None)


_ENTITY_LOADERS: Final[dict[str, Callable[[Container, str, Principal], Awaitable[Any]]]] = {
    "module": _load_module,
    "prompt": _load_prompt,
    "guardrail": _load_guardrail,
    "run": _load_run,
    "document": _load_document,
    "commercial": _load_commercial,
    "detection": _load_detection,
    "apikey": _load_api_key,
    "user": _load_user,
}
"""Carregador de cada entidade suportada pelo painel de contexto."""


# ---------------------------------------------------------------------------
# Contexto base
# ---------------------------------------------------------------------------
async def _degrade(what: str, awaitable: Awaitable[_T], fallback: _T) -> _T:
    """Executa uma consulta auxiliar da moldura sem deixar a pagina cair."""
    try:
        return await awaitable
    except LukatoError as exc:
        _logger.info("ui_context_degraded", what=what, code=exc.code)
        return fallback
    except Exception as exc:
        _logger.warning("ui_context_degraded", what=what, error=f"{type(exc).__name__}: {exc}")
        return fallback


def _principal_of(request: Request) -> Principal:
    """Principal ja resolvido pela dependencia HTTP; root anonimo como ultima linha."""
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else Principal.anonymous_root()


def _request_id_of(request: Request) -> str:
    """Identificador de correlacao carimbado pelo middleware."""
    return str(getattr(request.state, "request_id", "") or "")


async def base_context(
    request: Request,
    container: Container,
    *,
    active: str,
    breadcrumb: Iterable[CrumbLike] | None = None,
    selected_id: str | None = None,
) -> Json:
    """Monta o dicionario base que **toda** pagina do console recebe.

    Chaves entregues (SPEC-0009 secao 8): `nav_sections`, `active_route`,
    `breadcrumb`, `principal`, `settings_public`, `health`, `cost_summary`,
    `version`, `request_id`, `selected_id`. Alem delas, o painel de contexto
    recebe `selected_entity`, `context_template` e `context_item`, resolvidos
    aqui para que `?sel=<id>` funcione **sem JavaScript**.
    """
    principal = _principal_of(request)
    settings = container.settings
    selection = (selected_id or "").strip() or None
    entity = entity_for_route(active)

    health, cost, item = await asyncio.gather(
        _degrade("health", _health_view(container), HealthView()),
        _degrade("cost", _cost_view(container, principal), CostView(available=False)),
        (load_entity(container, entity, selection, principal) if selection else _resolved(None)),
    )

    return {
        "nav_sections": build_nav(container, active),
        "active_route": active,
        "breadcrumb": crumbs(breadcrumb),
        "principal": principal,
        "settings_public": settings_public(settings),
        "health": health,
        "cost_summary": cost,
        "version": settings.app.version,
        "request_id": _request_id_of(request),
        "selected_id": selection,
        "selected_entity": entity,
        "context_template": context_template_for(entity),
        "context_item": item,
        "now": utcnow(),
        "auth_enabled": settings.security.auth_enabled,
        "app_name": settings.app.name,
    }


async def _resolved(value: _T) -> _T:
    """Embrulha um valor pronto em um awaitable, para compor com `asyncio.gather`."""
    return value
