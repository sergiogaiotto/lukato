"""Ferramentas auditaveis do runtime de agentes (SPEC-0004 secao 4).

Cada ferramenta declara nome, descricao e JSON Schema de argumentos, e executa de
forma assincrona. O registro nao guarda dependencia nenhuma: tudo o que a ferramenta
precisa (relogio, embeddings, indice vetorial, unidade de trabalho) chega no
`ToolContext` montado pelo chamador. Quando a dependencia nao esta presente a
ferramenta devolve ``{"error": "capacidade indisponivel"}`` em vez de levantar — o
agente segue raciocinando com a informacao de que aquele caminho esta fechado.

`calculator` avalia aritmetica com uma allowlist de nos da AST: nada de `eval`,
nada de nomes, nada de chamadas, e um teto de expoente que barra `9**9**9`.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from lukato.config import Settings, get_logger
from lukato.domain.errors import ConflictError, LukatoError, ModuleError, ValidationError
from lukato.domain.ports.embeddings import EmbeddingPort
from lukato.domain.ports.misc import ClockPort
from lukato.domain.ports.unit_of_work import UnitOfWorkFactory
from lukato.domain.ports.vector_store import VectorStorePort
from lukato.domain.types import DEFAULT_TENANT, Json, utcnow

__all__ = [
    "CAPABILITY_UNAVAILABLE",
    "DEFAULT_COLLECTION",
    "MAX_EXPONENT",
    "MAX_EXPRESSION_CHARS",
    "MAX_RESULT_MAGNITUDE",
    "MAX_TOOL_LIMIT",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolSpec",
    "build_tool_registry",
    "calculator",
    "commercial_lookup",
    "cost_lookup",
    "default_tool_specs",
    "knowledge_search",
    "now",
    "safe_arithmetic",
]

_logger = get_logger(__name__)

CAPABILITY_UNAVAILABLE: Final[str] = "capacidade indisponivel"
"""Valor de `error` devolvido quando falta a dependencia que a ferramenta exige."""

DEFAULT_COLLECTION: Final[str] = "agente_evidence"
"""Colecao usada pela busca semantica quando nem o contexto nem `Settings` indicam uma."""

MAX_TOOL_LIMIT: Final[int] = 20
"""Teto de itens devolvidos por `knowledge_search` e `commercial_lookup`."""

MAX_EXPRESSION_CHARS: Final[int] = 200
"""Comprimento maximo aceito pela calculadora (evita arvores absurdas)."""

MAX_EXPONENT: Final[float] = 64.0
"""Expoente maximo permitido: barra bombas de calculo como `9**9**9`."""

MAX_RESULT_MAGNITUDE: Final[float] = 1e100
"""Magnitude maxima de qualquer resultado intermediario da calculadora."""

MAX_SNIPPET_CHARS: Final[int] = 500
"""Recorte de texto devolvido em cada item de resultado (custo de contexto)."""

DEFAULT_LOOKUP_DAYS: Final[int] = 30
"""Janela padrao, em dias, consultada por `cost_lookup`."""

MAX_LOOKUP_DAYS: Final[int] = 366
"""Janela maxima, em dias, aceita por `cost_lookup`."""

ToolHandler = Callable[[Json, "ToolContext"], Awaitable[Json]]
"""Assinatura de execucao de uma ferramenta: `(argumentos, contexto) -> JSON`."""


@dataclass(slots=True)
class ToolContext:
    """Dependencias entregues as ferramentas em tempo de execucao.

    Tudo e opcional: uma ferramenta cuja dependencia falta devolve
    `{"error": "capacidade indisponivel"}` em vez de derrubar o run.
    """

    clock: ClockPort | None = None
    embeddings: EmbeddingPort | None = None
    vector_store: VectorStorePort | None = None
    uow_factory: UnitOfWorkFactory | None = None
    settings: Settings | None = None
    tenant_id: str = DEFAULT_TENANT
    module_slug: str = ""
    collection: str = ""
    extras: Json = field(default_factory=dict)

    def now(self) -> datetime:
        """Instante atual em UTC, vindo do `ClockPort` quando ele foi injetado."""
        moment = self.clock.now() if self.clock is not None else utcnow()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)

    def default_collection(self) -> str:
        """Colecao de conhecimento padrao: contexto, depois `Settings`, depois constante."""
        if self.collection:
            return self.collection
        if self.settings is not None:
            return self.settings.embedding.collection
        return DEFAULT_COLLECTION


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """Descricao completa de uma ferramenta: contrato + implementacao assincrona."""

    name: str
    description: str
    schema: Json
    handler: ToolHandler

    def describe(self) -> Json:
        """Contrato publico da ferramenta, pronto para ir ao prompt do agente."""
        return {"name": self.name, "description": self.description, "schema": self.schema}


def _clip(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    """Recorta o texto preservando o inicio e sinalizando o corte."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _unavailable(tool: str, missing: Sequence[str]) -> Json:
    """Resposta padrao de capacidade ausente, com o motivo no log estruturado."""
    _logger.warning("tool_capability_unavailable", tool=tool, missing=list(missing))
    return {"error": CAPABILITY_UNAVAILABLE}


def _require_str(args: Json, key: str, *, tool: str) -> str:
    """Le um argumento textual obrigatorio e nao vazio."""
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"A ferramenta '{tool}' exige o argumento '{key}' como texto nao vazio.",
            details={"tool": tool, "argument": key},
        )
    return value.strip()


def _optional_str(args: Json, key: str) -> str:
    """Le um argumento textual opcional, normalizado e sem espacos nas pontas."""
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _optional_int(
    args: Json,
    key: str,
    *,
    tool: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Le um argumento inteiro opcional, recusando valores fora da faixa."""
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"A ferramenta '{tool}' exige o argumento '{key}' como numero inteiro.",
            details={"tool": tool, "argument": key, "received": str(raw)[:80]},
        ) from exc
    if value < minimum or value > maximum:
        raise ValidationError(
            f"O argumento '{key}' de '{tool}' deve estar entre {minimum} e {maximum}.",
            details={"tool": tool, "argument": key, "received": value},
        )
    return value


# --------------------------------------------------------------------------- #
# calculator — avaliacao aritmetica por AST com allowlist explicita
# --------------------------------------------------------------------------- #

_BINARY_OPERATORS: Final[dict[type[ast.operator], Callable[[Any, Any], Any]]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
"""Operadores binarios permitidos (qualquer outro e recusado antes de avaliar)."""

_UNARY_OPERATORS: Final[dict[type[ast.unaryop], Callable[[Any], Any]]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
"""Operadores unarios permitidos."""

_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    *_BINARY_OPERATORS,
    *_UNARY_OPERATORS,
)
"""Allowlist de nos da AST: parenteses nao geram no, nomes e chamadas nao entram."""


def _reject_node(node: ast.AST, expression: str) -> ValidationError:
    """Monta o erro de sintaxe proibida com o no exato que causou a recusa."""
    return ValidationError(
        "A calculadora aceita apenas aritmetica com numeros, parenteses e os "
        "operadores + - * / // % **.",
        details={
            "tool": "calculator",
            "expression": _clip(expression, 120),
            "rejected_node": type(node).__name__,
        },
    )


def _numeric_constant(node: ast.Constant, expression: str) -> float | int:
    """Aceita apenas literais numericos reais (booleano e texto sao recusados)."""
    value = node.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _reject_node(node, expression)
    return value


def _guard_magnitude(value: Any, expression: str) -> float | int:
    """Recusa resultados fora da faixa suportada (inclui `inf` e `nan`)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError(
            "A calculadora produziu um valor que nao e numerico.",
            details={"tool": "calculator", "expression": _clip(expression, 120)},
        )
    if math.isnan(value) or abs(value) > MAX_RESULT_MAGNITUDE:
        raise ValidationError(
            "O resultado excede a magnitude suportada pela calculadora.",
            details={
                "tool": "calculator",
                "expression": _clip(expression, 120),
                "max_magnitude": MAX_RESULT_MAGNITUDE,
            },
        )
    return value


def _eval_node(node: ast.AST, expression: str) -> float | int:
    """Avalia recursivamente um no ja validado contra a allowlist."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, expression)
    if isinstance(node, ast.Constant):
        return _numeric_constant(node, expression)
    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPERATORS.get(type(node.op))
        if unary is None:
            raise _reject_node(node.op, expression)
        return _guard_magnitude(unary(_eval_node(node.operand, expression)), expression)
    if isinstance(node, ast.BinOp):
        binary = _BINARY_OPERATORS.get(type(node.op))
        if binary is None:
            raise _reject_node(node.op, expression)
        left = _eval_node(node.left, expression)
        right = _eval_node(node.right, expression)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ValidationError(
                f"A calculadora limita o expoente a {MAX_EXPONENT:.0f}.",
                details={
                    "tool": "calculator",
                    "expression": _clip(expression, 120),
                    "exponent": right,
                },
            )
        if isinstance(node.op, ast.Div | ast.FloorDiv | ast.Mod) and right == 0:
            raise ValidationError(
                "Divisao por zero na expressao informada.",
                details={"tool": "calculator", "expression": _clip(expression, 120)},
            )
        try:
            result = binary(left, right)
        except (ArithmeticError, ValueError) as exc:
            raise ValidationError(
                f"Nao foi possivel calcular a expressao: {exc}",
                details={"tool": "calculator", "expression": _clip(expression, 120)},
            ) from exc
        return _guard_magnitude(result, expression)
    raise _reject_node(node, expression)


def safe_arithmetic(expression: str) -> float | int:
    """Avalia uma expressao aritmetica com allowlist de AST — nunca usa `eval`.

    Qualquer no fora da allowlist (nome, chamada, atributo, indexacao, comprehension)
    resulta em `ValidationError` **antes** de qualquer avaliacao.
    """
    text = expression.strip()
    if not text:
        raise ValidationError(
            "A calculadora exige uma expressao nao vazia.",
            details={"tool": "calculator"},
        )
    if len(text) > MAX_EXPRESSION_CHARS:
        raise ValidationError(
            f"Expressao longa demais (maximo {MAX_EXPRESSION_CHARS} caracteres).",
            details={"tool": "calculator", "length": len(text)},
        )
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValidationError(
            "Expressao aritmetica invalida.",
            details={"tool": "calculator", "expression": _clip(text, 120), "detail": str(exc)},
        ) from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise _reject_node(node, text)
    return _eval_node(tree, text)


# --------------------------------------------------------------------------- #
# Implementacao das cinco ferramentas da SPEC-0004
# --------------------------------------------------------------------------- #


async def now(args: Json, ctx: ToolContext) -> Json:
    """Data e hora atuais em UTC, lidas do `ClockPort` para manter o teste determinista."""
    moment = ctx.now()
    return {
        "iso": moment.isoformat(),
        "date": moment.date().isoformat(),
        "time": moment.time().isoformat(timespec="seconds"),
        "timestamp": moment.timestamp(),
        "timezone": "UTC",
    }


async def calculator(args: Json, ctx: ToolContext) -> Json:
    """Aritmetica segura: soma, subtracao, produto, divisao, modulo e potencia."""
    expression = _require_str(args, "expression", tool="calculator")
    result = safe_arithmetic(expression)
    return {"expression": expression, "result": result}


async def knowledge_search(args: Json, ctx: ToolContext) -> Json:
    """Busca semantica na base de conhecimento (embeddings + indice vetorial)."""
    query = _require_str(args, "query", tool="knowledge_search")
    limit = _optional_int(
        args,
        "limit",
        tool="knowledge_search",
        default=5,
        minimum=1,
        maximum=MAX_TOOL_LIMIT,
    )
    embedder = ctx.embeddings
    store = ctx.vector_store
    missing = [
        name
        for name, dependency in (("embeddings", embedder), ("vector_store", store))
        if dependency is None
    ]
    if embedder is None or store is None:
        return _unavailable("knowledge_search", missing)
    collection = _optional_str(args, "collection") or ctx.default_collection()
    vector = await embedder.embed_one(query)
    hits = await store.search(collection, vector, limit=limit)
    return {
        "query": query,
        "collection": collection,
        "total": len(hits),
        "hits": [
            {
                "chunk_id": hit.chunk_id,
                "document_id": hit.document_id,
                "score": round(hit.score, 6),
                "content": _clip(hit.content),
                "metadata": hit.metadata,
            }
            for hit in hits
        ],
    }


async def cost_lookup(args: Json, ctx: ToolContext) -> Json:
    """Custo acumulado do modulo/tenant na janela pedida (padrao: 30 dias)."""
    days = _optional_int(
        args,
        "days",
        tool="cost_lookup",
        default=DEFAULT_LOOKUP_DAYS,
        minimum=1,
        maximum=MAX_LOOKUP_DAYS,
    )
    factory = ctx.uow_factory
    if factory is None:
        return _unavailable("cost_lookup", ["uow_factory"])
    module_slug = _optional_str(args, "module_slug") or ctx.module_slug
    tenant_id = _optional_str(args, "tenant_id") or ctx.tenant_id
    until = ctx.now()
    since = until - timedelta(days=days)
    async with factory() as uow:
        summary = await uow.usage.summary(
            since=since,
            until=until,
            module_slug=module_slug or None,
            tenant_id=tenant_id or None,
        )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "module_slug": module_slug,
        "tenant_id": tenant_id,
        "total_usd": round(summary.total_usd, 6),
        "total_tokens": summary.total_tokens,
        "runs": summary.runs,
        "by_module": summary.by_module,
        "by_model": summary.by_model,
    }


def _commercial_item(commercial: Any) -> Json:
    """Projeta o comercial nos campos uteis ao agente (sem despejar a entidade inteira)."""
    return {
        "id": commercial.id,
        "commercial_id": commercial.commercial_id,
        "campaign": commercial.campaign,
        "brand": commercial.brand,
        "duration_expected": commercial.duration_expected,
        "keywords": list(commercial.keywords),
        "key_phrases": list(commercial.key_phrases),
        "language": commercial.language,
        "is_active": commercial.is_active,
        "text": _clip(commercial.text),
    }


async def commercial_lookup(args: Json, ctx: ToolContext) -> Json:
    """Consulta o catalogo AdWatch por codigo de negocio ou por texto/marca/campanha."""
    limit = _optional_int(
        args,
        "limit",
        tool="commercial_lookup",
        default=5,
        minimum=1,
        maximum=MAX_TOOL_LIMIT,
    )
    factory = ctx.uow_factory
    if factory is None:
        return _unavailable("commercial_lookup", ["uow_factory"])
    code = _optional_str(args, "code")
    search = _optional_str(args, "query") or _optional_str(args, "search")
    brand = _optional_str(args, "brand")
    campaign = _optional_str(args, "campaign")
    async with factory() as uow:
        if code:
            found = await uow.commercials.get_by_code(code)
            items = [found] if found is not None else []
        else:
            items = await uow.commercials.list(
                search=search or None,
                brand=brand or None,
                campaign=campaign or None,
                limit=limit,
            )
    return {
        "code": code,
        "query": search,
        "brand": brand,
        "campaign": campaign,
        "total": len(items),
        "items": [_commercial_item(item) for item in items],
    }


_NOW_SCHEMA: Final[Json] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_CALCULATOR_SCHEMA: Final[Json] = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "Expressao aritmetica, ex: '(2 + 3) * 4.5'. Sem nomes nem funcoes.",
        }
    },
    "required": ["expression"],
    "additionalProperties": False,
}

_KNOWLEDGE_SCHEMA: Final[Json] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Pergunta ou trecho a buscar."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TOOL_LIMIT,
            "description": "Quantidade maxima de trechos devolvidos (padrao 5).",
        },
        "collection": {"type": "string", "description": "Colecao alvo (opcional)."},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_COST_SCHEMA: Final[Json] = {
    "type": "object",
    "properties": {
        "module_slug": {"type": "string", "description": "Modulo a consultar (opcional)."},
        "tenant_id": {"type": "string", "description": "Inquilino a consultar (opcional)."},
        "days": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LOOKUP_DAYS,
            "description": f"Janela em dias (padrao {DEFAULT_LOOKUP_DAYS}).",
        },
    },
    "additionalProperties": False,
}

_COMMERCIAL_SCHEMA: Final[Json] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "Codigo de negocio, ex 'COM_000234'."},
        "query": {"type": "string", "description": "Texto livre para busca no catalogo."},
        "brand": {"type": "string", "description": "Marca exata."},
        "campaign": {"type": "string", "description": "Campanha exata."},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_TOOL_LIMIT,
            "description": "Quantidade maxima de comerciais devolvidos (padrao 5).",
        },
    },
    "additionalProperties": False,
}


def default_tool_specs() -> list[ToolSpec]:
    """As cinco ferramentas normativas da SPEC-0004, na ordem da tabela."""
    return [
        ToolSpec(
            name="knowledge_search",
            description=(
                "Busca semantica na base de conhecimento do lukato. Use para recuperar "
                "trechos de documentos que fundamentem a resposta."
            ),
            schema=_KNOWLEDGE_SCHEMA,
            handler=knowledge_search,
        ),
        ToolSpec(
            name="cost_lookup",
            description=(
                "Custo acumulado em USD e tokens consumidos por modulo/tenant numa "
                "janela recente de dias."
            ),
            schema=_COST_SCHEMA,
            handler=cost_lookup,
        ),
        ToolSpec(
            name="commercial_lookup",
            description=(
                "Consulta o catalogo de comerciais do AdWatch por codigo de negocio, "
                "texto livre, marca ou campanha."
            ),
            schema=_COMMERCIAL_SCHEMA,
            handler=commercial_lookup,
        ),
        ToolSpec(
            name="now",
            description="Data e hora atuais em UTC.",
            schema=_NOW_SCHEMA,
            handler=now,
        ),
        ToolSpec(
            name="calculator",
            description=(
                "Calcula uma expressao aritmetica com + - * / // % ** e parenteses. "
                "Nao aceita nomes, funcoes nem chamadas."
            ),
            schema=_CALCULATOR_SCHEMA,
            handler=calculator,
        ),
    ]


_SCALAR_COERCIONS: Final[dict[str, Callable[[Any], Any]]] = {
    "integer": int,
    "number": float,
    "string": str,
}
"""Conversoes toleradas quando o LLM manda o tipo certo dentro de uma string."""


def _coerce(value: Any, declared: Any) -> Any:
    """Ajusta tipos triviais vindos do LLM (`"5"` -> `5`) sem mascarar erros reais."""
    kind = declared[0] if isinstance(declared, list) and declared else declared
    if not isinstance(kind, str) or kind == "boolean":
        return value
    converter = _SCALAR_COERCIONS.get(kind)
    if converter is None or isinstance(value, bool) or value is None:
        return value
    if kind == "string" and isinstance(value, str):
        return value
    if kind in {"integer", "number"} and isinstance(value, int | float):
        return converter(value)
    if isinstance(value, str):
        try:
            return converter(value.strip())
        except (TypeError, ValueError):
            return value
    return value


class ToolRegistry:
    """Registro nominal e auditavel das ferramentas disponiveis ao runtime."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec, *, override: bool = False) -> ToolSpec:
        """Registra a ferramenta; nome repetido sem `override` gera `ConflictError`."""
        if not spec.name or not spec.name.strip():
            raise ValidationError("Toda ferramenta precisa de um nome nao vazio.")
        key = spec.name.strip()
        if key in self._tools and not override:
            raise ConflictError(
                f"Ja existe uma ferramenta registrada com o nome '{key}'.",
                details={"tool": key},
            )
        self._tools[key] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        """Resolve a ferramenta pelo nome; inexistente gera `ValidationError`."""
        key = (name or "").strip()
        spec = self._tools.get(key)
        if spec is None:
            raise ValidationError(
                f"Ferramenta desconhecida: '{key}'.",
                details={"tool": key, "available": sorted(self._tools)},
            )
        return spec

    def all(self) -> list[ToolSpec]:
        """Todas as ferramentas registradas, em ordem alfabetica de nome."""
        return [self._tools[key] for key in sorted(self._tools)]

    def names(self) -> list[str]:
        """Nomes registrados, em ordem alfabetica."""
        return sorted(self._tools)

    def describe(self, names: Sequence[str] | None = None) -> list[Json]:
        """Contratos das ferramentas pedidas (ou de todas), prontos para o prompt."""
        if names is None:
            return [spec.describe() for spec in self.all()]
        return [self.get(name).describe() for name in names]

    def resolve(self, names: Sequence[str]) -> list[ToolSpec]:
        """Resolve uma lista de nomes preservando a ordem; nome invalido levanta."""
        return [self.get(name) for name in names]

    async def execute(self, name: str, args: Json | None, ctx: ToolContext) -> Json:
        """Executa a ferramenta e devolve o JSON de resultado.

        Argumentos desconhecidos sao descartados quando o schema os proibe: o LLM
        alucina chaves extras com frequencia e isso nao pode derrubar a execucao.
        Erros de biblioteca externa viram `ModuleError` (um `LukatoError`), para que
        o orquestrador os registre como observacao com step `ERROR`.
        """
        spec = self.get(name)
        payload = self._prepare_args(spec, args or {})
        try:
            result = await spec.handler(payload, ctx)
        except LukatoError:
            raise
        except Exception as exc:
            raise ModuleError(
                f"A ferramenta '{spec.name}' falhou: {type(exc).__name__}: {exc}",
                details={"tool": spec.name, "error": type(exc).__name__},
            ) from exc
        if not isinstance(result, dict):
            raise ModuleError(
                f"A ferramenta '{spec.name}' devolveu um resultado que nao e um objeto JSON.",
                details={"tool": spec.name, "type": type(result).__name__},
            )
        return result

    def _prepare_args(self, spec: ToolSpec, args: Mapping[str, Any]) -> Json:
        """Filtra chaves proibidas, converte escalares triviais e cobra obrigatorios."""
        properties = spec.schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        strict = spec.schema.get("additionalProperties") is False
        payload: Json = {}
        dropped: list[str] = []
        for key, value in args.items():
            declared = properties.get(key)
            if declared is None:
                if strict:
                    dropped.append(str(key))
                    continue
                payload[str(key)] = value
                continue
            payload[str(key)] = _coerce(value, declared.get("type"))
        if dropped:
            _logger.warning("tool_arguments_dropped", tool=spec.name, dropped=sorted(dropped))
        required = spec.schema.get("required")
        missing = [
            str(key)
            for key in (required if isinstance(required, list) else [])
            if str(key) not in payload
        ]
        if missing:
            raise ValidationError(
                f"Argumentos obrigatorios ausentes para '{spec.name}': {', '.join(missing)}.",
                details={"tool": spec.name, "missing": missing},
            )
        return payload

    def __contains__(self, name: object) -> bool:
        """True quando ha ferramenta registrada com esse nome."""
        return isinstance(name, str) and name.strip() in self._tools

    def __len__(self) -> int:
        """Quantidade de ferramentas registradas."""
        return len(self._tools)

    def __repr__(self) -> str:
        """Representacao curta com os nomes registrados."""
        return f"ToolRegistry({', '.join(self.names())})"


def build_tool_registry(*, extra: Iterable[ToolSpec] = ()) -> ToolRegistry:
    """Monta o registro com as cinco ferramentas normativas mais as extras do chamador."""
    registry = ToolRegistry(default_tool_specs())
    for spec in extra:
        registry.register(spec, override=True)
    return registry
