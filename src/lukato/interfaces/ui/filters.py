"""Filtros e funcoes globais do Jinja2 usados pelo console web (SPEC-0009 secao 2).

Todo numero, valor monetario e data que aparece na tela passa por um filtro deste
modulo. A razao e simples: a formatacao pt-BR (virgula decimal, ponto de milhar,
"ha 3 minutos") e uma regra de apresentacao, e regra de apresentacao repetida em
vinte templates vira vinte formatacoes ligeiramente diferentes.

Nenhum filtro levanta excecao. Um template que recebe `None`, uma string vazia ou
um tipo inesperado precisa continuar renderizando: uma pagina inteira nao pode
cair porque um registro veio sem `finished_at`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlencode

from jinja2 import Environment, pass_eval_context
from jinja2.nodes import EvalContext
from markupsafe import Markup, escape
from starlette.requests import Request

__all__ = [
    "BADGE_CLASSES",
    "CURRENCY_PREFIX",
    "DEFAULT_BADGE_CLASS",
    "MONEY_DIGITS",
    "badge_class",
    "datetime_br",
    "duration",
    "initials",
    "json_pretty",
    "money",
    "nl2br",
    "number",
    "pct",
    "query_url",
    "register_filters",
    "timeago",
    "tokens",
    "truncate_mid",
]

CURRENCY_PREFIX: Final[str] = "US$"
"""Prefixo monetario do console; a moeda normativa do FinOps e o dolar."""

MONEY_DIGITS: Final[int] = 5
"""Casas decimais de custo: chamadas de LLM custam fracoes de centavo."""

ELLIPSIS: Final[str] = "…"
"""Reticencia tipografica usada em truncamentos e mascaras."""

DEFAULT_BADGE_CLASS: Final[str] = "lk-badge--neutral"
"""Variante do badge quando o estado nao e reconhecido."""

BADGE_CLASSES: Final[dict[str, str]] = {
    # execucoes
    "succeeded": "lk-badge--ok",
    "running": "lk-badge--info",
    "pending": "lk-badge--neutral",
    "failed": "lk-badge--danger",
    "blocked": "lk-badge--danger",
    "cancelled": "lk-badge--warn",
    # modulos
    "active": "lk-badge--ok",
    "draft": "lk-badge--neutral",
    "paused": "lk-badge--warn",
    "deprecated": "lk-badge--danger",
    # saude
    "ok": "lk-badge--ok",
    "degraded": "lk-badge--warn",
    "down": "lk-badge--danger",
    # guardrails
    "allow": "lk-badge--ok",
    "warn": "lk-badge--warn",
    "redact": "lk-badge--info",
    "block": "lk-badge--danger",
    # deteccoes do adwatch
    "accepted": "lk-badge--ok",
    "needs_review": "lk-badge--warn",
    "rejected": "lk-badge--danger",
    "confirmed": "lk-badge--ok",
    "dismissed": "lk-badge--neutral",
    # booleanos textuais
    "true": "lk-badge--ok",
    "false": "lk-badge--neutral",
    "sim": "lk-badge--ok",
    "nao": "lk-badge--neutral",
}
"""Mapa `estado -> variante` do badge, cobrindo os enums de dominio da SPEC-0000."""

_MINUTE: Final[int] = 60
_HOUR: Final[int] = 3600
_DAY: Final[int] = 86400
_MONTH: Final[int] = 2_592_000

_TOKEN_UNITS: Final[tuple[tuple[int, str], ...]] = (
    (1_000_000_000, "B"),
    (1_000_000, "M"),
    (1_000, "k"),
)
"""Escalas de abreviacao de contagem de tokens, da maior para a menor."""

_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "hashed_secret",
        "jwt_secret",
        "key_hash",
        "password",
        "password_hash",
        "secret",
        "secret_key",
        "token",
    }
)
"""Chaves que `json_pretty` sempre substitui por um marcador, nunca imprime."""

_REDACTED: Final[str] = "***"
"""Marcador escrito no lugar de um valor sensivel."""


# ---------------------------------------------------------------------------
# Numeros
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float:
    """Converte para `float` tolerando `None`, string e tipos exoticos."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else 0.0
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "").replace(",", ".")
        try:
            numeric = float(cleaned)
        except ValueError:
            return 0.0
        return numeric if math.isfinite(numeric) else 0.0
    return 0.0


def _to_int(value: Any) -> int:
    """Converte para `int` sem levantar, arredondando para o inteiro mais proximo."""
    return round(_to_float(value))


def _group(digits: str) -> str:
    """Agrupa a parte inteira de tres em tres com ponto (padrao pt-BR)."""
    negative = digits.startswith("-")
    body = digits.lstrip("-")
    chunks: list[str] = []
    while len(body) > 3:
        chunks.insert(0, body[-3:])
        body = body[:-3]
    chunks.insert(0, body)
    joined = ".".join(chunks)
    return f"-{joined}" if negative else joined


def number(value: Any, digits: int = 0) -> str:
    """Formata um numero em pt-BR: ponto de milhar e virgula decimal.

    `number(1234.5, 2)` devolve `1.234,50`.
    """
    places = max(0, int(digits))
    formatted = f"{_to_float(value):.{places}f}"
    whole, _, fraction = formatted.partition(".")
    grouped = _group(whole)
    return f"{grouped},{fraction}" if fraction else grouped


def money(value: Any, digits: int = MONEY_DIGITS) -> str:
    """Formata um custo em dolar no padrao pt-BR: `US$ 0,00042`."""
    return f"{CURRENCY_PREFIX} {number(value, digits)}"


def tokens(value: Any) -> str:
    """Abrevia uma contagem de tokens: `847`, `1,2k`, `3,4M`, `1,1B`."""
    total = _to_int(value)
    sign = "-" if total < 0 else ""
    magnitude = abs(total)
    for scale, suffix in _TOKEN_UNITS:
        if magnitude >= scale:
            reduced = magnitude / scale
            places = 0 if reduced >= 100 else 1
            return f"{sign}{number(reduced, places)}{suffix}"
    return f"{sign}{_group(str(magnitude))}"


def pct(value: Any, digits: int = 1, scale: float = 100.0) -> str:
    """Formata uma fracao como percentual: `pct(0.427)` devolve `42,7%`.

    Valores ja expressos em percentual entram com `scale=1`.
    """
    return f"{number(_to_float(value) * _to_float(scale or 1.0), digits)}%"


# ---------------------------------------------------------------------------
# Tempo
# ---------------------------------------------------------------------------
def duration(value: Any) -> str:
    """Formata segundos como `HH:MM:SS.d` — o relogio de midia do AdWatch.

    Formato normativo da SPEC-0009: `duration(5025.6)` devolve `01:23:45.6`. O
    separador decimal aqui e o **ponto**, e nao a virgula do resto do console,
    porque timecode e notacao tecnica, lida do mesmo jeito em qualquer idioma.
    Valores negativos viram `00:00:00.0`.
    """
    total = max(0.0, _to_float(value))
    hours, remainder = divmod(total, float(_HOUR))
    minutes, seconds = divmod(remainder, 60.0)
    whole = int(seconds)
    tenths = round((seconds - whole) * 10)
    if tenths == 10:  # arredondamento que estoura o segundo
        whole += 1
        tenths = 0
        if whole == 60:
            whole = 0
            minutes += 1
            if minutes == 60:
                minutes = 0.0
                hours += 1
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d}.{tenths}"


def _as_datetime(value: Any) -> datetime | None:
    """Interpreta datas vindas do dominio, de ISO-8601 ou de epoch; `None` se nao der."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def datetime_br(value: Any, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Formata uma data no padrao brasileiro; vazio quando nao ha data."""
    moment = _as_datetime(value)
    return "" if moment is None else moment.strftime(fmt)


def timeago(value: Any, *, reference: datetime | None = None) -> str:
    """Distancia legivel ate agora, em pt-BR: `agora`, `ha 5 min`, `ha 3 dias`.

    Datas futuras usam a forma "em ...". Alem de trinta dias, devolve a data
    absoluta, que informa mais do que "ha 14 meses".
    """
    moment = _as_datetime(value)
    if moment is None:
        return "—"
    now = reference or datetime.now(tz=UTC)
    delta = (now - moment).total_seconds()
    future = delta < 0
    seconds = abs(delta)

    if seconds < 45:
        return "agora"
    if seconds < _HOUR:
        amount, unit = int(seconds // _MINUTE), "min"
    elif seconds < _DAY:
        hours = int(seconds // _HOUR)
        amount, unit = hours, "hora" if hours == 1 else "horas"
    elif seconds < _MONTH:
        days = int(seconds // _DAY)
        amount, unit = days, "dia" if days == 1 else "dias"
    else:
        return datetime_br(moment, "%d/%m/%Y")
    return f"em {amount} {unit}" if future else f"ha {amount} {unit}"


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------
def badge_class(value: Any) -> str:
    """Variante CSS do badge para um estado de dominio (SPEC-0009 secao 6)."""
    if value is None:
        return DEFAULT_BADGE_CLASS
    raw = getattr(value, "value", value)
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return BADGE_CLASSES.get(key, DEFAULT_BADGE_CLASS)


def truncate_mid(value: Any, length: int = 32) -> str:
    """Encurta pelo meio preservando inicio e fim — ideal para identificadores.

    `truncate_mid("0198f2c1-...-9ab", 16)` devolve algo como `0198f2c…f9ab`.
    """
    text = "" if value is None else str(value)
    limit = max(5, int(length))
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = (keep + 1) // 2
    tail = keep - head
    return f"{text[:head]}{ELLIPSIS}{text[len(text) - tail :]}"


def _scrub(value: Any) -> Any:
    """Copia a estrutura substituindo valores de chaves sensiveis por `***`."""
    if isinstance(value, Mapping):
        return {
            str(key): (_REDACTED if str(key).lower() in _SENSITIVE_KEYS else _scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(item) for item in value]
    return value


def json_pretty(value: Any, indent: int = 2) -> str:
    """Serializa em JSON legivel (UTF-8, indentado) e sem campos sensiveis.

    Usado no bloco `<details><summary>JSON</summary>` do painel de contexto. A
    limpeza de chaves sensiveis e a ultima barreira antes da tela: nenhum hash de
    senha ou segredo de chave de API chega ao navegador por descuido de um
    template.
    """
    if value is None:
        return "{}"
    payload = value
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            payload = dump(mode="json")
        except TypeError:  # pragma: no cover - modelo sem modo json
            payload = dump()
    try:
        return json.dumps(
            _scrub(payload), indent=max(0, int(indent)), ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        return json.dumps(str(payload), ensure_ascii=False)


def initials(value: Any, size: int = 2) -> str:
    """Iniciais de um nome, para o avatar da topbar: `Ana Lima` vira `AL`."""
    text = "" if value is None else str(value).strip()
    if not text:
        return "?"
    words = [word for word in text.replace(".", " ").replace("_", " ").split() if word]
    if not words:
        return "?"
    limit = max(1, int(size))
    if len(words) == 1:
        return words[0][:limit].upper()
    picked = [words[0], words[-1]][:limit]
    return "".join(word[0] for word in picked).upper()


@pass_eval_context
def nl2br(eval_ctx: EvalContext, value: Any) -> str | Markup:
    """Converte quebras de linha em `<br>` **depois** de escapar o conteudo."""
    text = "" if value is None else str(value)
    if not eval_ctx.autoescape:
        return text.replace("\n", "<br>")
    return Markup("<br>").join(escape(line) for line in text.splitlines())


# ---------------------------------------------------------------------------
# Funcoes globais
# ---------------------------------------------------------------------------
def query_url(request: Request, **overrides: Any) -> str:
    """Reescreve a URL atual trocando apenas os parametros informados.

    Base da paginacao e dos filtros: `query_url(request, offset=50)` preserva
    busca, ordenacao e selecao ja aplicadas. Passar `None` remove o parametro.
    """
    params = dict(request.query_params)
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(getattr(value, "value", value))
    query = urlencode([(key, value) for key, value in sorted(params.items()) if key])
    path = request.url.path
    return f"{path}?{query}" if query else path


def register_filters(env: Environment) -> None:
    """Registra filtros e globais do console no ambiente Jinja informado."""
    env.filters.update(
        {
            "badge_class": badge_class,
            "datetime_br": datetime_br,
            "duration": duration,
            "initials": initials,
            "json_pretty": json_pretty,
            "money": money,
            "nl2br": nl2br,
            "number": number,
            "pct": pct,
            "timeago": timeago,
            "tokens": tokens,
            "truncate_mid": truncate_mid,
        }
    )
    env.globals.setdefault("query_url", query_url)
