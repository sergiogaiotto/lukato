"""Casos de uso de saude: liveness, readiness e detalhe por provedor (SPEC-0008 secao 5).

Tres perguntas distintas, tres respostas distintas — confundi-las e a causa
classica de um cluster que reinicia replicas saudaveis:

* **Liveness** (`GET /healthz`): "o processo esta vivo?". Constante, **sem** tocar
  em dependencia nenhuma. Se o banco cair e o liveness consultar o banco, o
  Kubernetes mata todas as replicas de uma aplicacao que estava perfeitamente
  capaz de servir o que nao depende do banco.
* **Readiness** (`GET /readyz`): "posso receber trafego?". Consulta
  :meth:`Container.health` e traduz o resultado em HTTP. Somente o **banco** e
  fatal: um provedor de LLM ou de embeddings degradado mantem o `200`, porque o
  lukato continua util offline (SPEC-0001 secao 6) — tirar a replica do balanceador
  por causa de um hub externo indisponivel nao devolveria o hub, so derrubaria o
  console junto.
* **Detalhe por provedor** (`GET /api/v1/health/providers`): alimenta a barra de
  status do console com o quadro completo — status ao vivo somado a configuracao
  efetiva. Nenhum segredo entra no relatorio: apenas o **fato** de haver
  credencial (`configured`), nunca o seu valor.

Nenhum dos tres levanta excecao: :meth:`Container.health` ja e blindado, e o que
sobrar e capturado aqui. Um relatorio de saude que falha e um diagnostico perdido
justamente no momento em que ele mais importa.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, TypeVar

from lukato.application.container import (
    STATUS_DEGRADED,
    STATUS_DOWN,
    STATUS_OK,
    Container,
)
from lukato.config import get_logger
from lukato.domain.types import Json, utcnow

__all__ = [
    "CRITICAL_COMPONENTS",
    "HTTP_OK",
    "HTTP_SERVICE_UNAVAILABLE",
    "LIVENESS_STATUS",
    "ComponentHealth",
    "GetLiveness",
    "GetProviderDetails",
    "GetReadiness",
    "LivenessReport",
    "ProviderInfo",
    "ProviderReport",
    "ReadinessReport",
]

_logger = get_logger(__name__)

_T = TypeVar("_T")

LIVENESS_STATUS: Final[str] = "alive"
"""Unica resposta possivel do liveness: o processo respondeu, logo esta vivo."""

HTTP_OK: Final[int] = 200
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

CRITICAL_COMPONENTS: Final[frozenset[str]] = frozenset({"database"})
"""Componentes cuja queda tira a replica do balanceador (SPEC-0008 secao 5).

Somente o banco: sem ele nao ha registro de execucao, catalogo nem identidade.
LLM, embeddings e tracer degradam sem impedir que a plataforma sirva trafego.
"""

_UNKNOWN: Final[str] = "desconhecido"


_NO_RUNTIMES: Final[list[str]] = []
"""Padrao tipado de `runtimes`: um `[]` literal inferiria `list[Never]`."""


def _safe(read: Callable[[], _T], fallback: _T) -> _T:
    """Le uma propriedade de porta sem deixar o relatorio de saude levantar."""
    try:
        return read()
    except Exception:  # pragma: no cover - adaptador defeituoso
        return fallback


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Saude de um componente: `ok`, `degraded` ou `down`, com o detalhe legivel."""

    name: str
    status: str = STATUS_OK
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True quando o componente respondeu plenamente."""
        return self.status == STATUS_OK

    @property
    def down(self) -> bool:
        """True quando o componente esta fora do ar."""
        return self.status == STATUS_DOWN

    @property
    def degraded(self) -> bool:
        """True quando o componente responde, mas sem toda a capacidade."""
        return self.status == STATUS_DEGRADED

    def to_dict(self) -> Json:
        """Serializa o componente para o corpo da resposta."""
        return {"status": self.status, "detail": self.detail}

    @classmethod
    def of(cls, name: str, payload: Mapping[str, str]) -> ComponentHealth:
        """Constroi o componente a partir do mapa devolvido por `Container.health`."""
        status = str(payload.get("status", STATUS_DEGRADED))
        if status not in {STATUS_OK, STATUS_DEGRADED, STATUS_DOWN}:
            status = STATUS_DEGRADED
        return cls(name=name, status=status, detail=str(payload.get("detail", "")))


@dataclass(frozen=True, slots=True)
class LivenessReport:
    """Resposta constante do liveness — nenhuma dependencia foi consultada."""

    service: str
    version: str
    status: str = LIVENESS_STATUS

    @property
    def http_status(self) -> int:
        """Liveness que responde e, por definicao, `200`."""
        return HTTP_OK

    def to_dict(self) -> Json:
        """Serializa a resposta de `GET /healthz`."""
        return {"status": self.status, "service": self.service, "version": self.version}


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Prontidao agregada da instalacao, componente a componente."""

    status: str
    components: dict[str, ComponentHealth] = field(default_factory=dict)
    version: str = ""
    service: str = ""
    environment: str = ""

    @property
    def ready(self) -> bool:
        """True quando nenhum componente critico esta fora do ar."""
        return self.status != STATUS_DOWN

    @property
    def http_status(self) -> int:
        """`503` somente com componente critico fora do ar; degradacao mantem `200`."""
        return HTTP_OK if self.ready else HTTP_SERVICE_UNAVAILABLE

    @property
    def failing(self) -> list[str]:
        """Nomes dos componentes que nao estao plenamente `ok`, em ordem."""
        return sorted(name for name, item in self.components.items() if not item.ok)

    def to_dict(self) -> Json:
        """Serializa a resposta de `GET /readyz`."""
        return {
            "status": self.status,
            "components": {name: item.to_dict() for name, item in self.components.items()},
            "version": self.version,
            "service": self.service,
            "environment": self.environment,
        }

    @classmethod
    def from_components(
        cls,
        components: Mapping[str, ComponentHealth],
        *,
        version: str = "",
        service: str = "",
        environment: str = "",
    ) -> ReadinessReport:
        """Deriva o status global das partes.

        `down` quando um componente **critico** caiu; `degraded` quando qualquer
        componente nao esta `ok` (inclusive um nao critico fora do ar); `ok` so
        com todos plenos.
        """
        items = dict(components)
        critical_down = any(
            item.down for name, item in items.items() if name in CRITICAL_COMPONENTS
        )
        if critical_down:
            status = STATUS_DOWN
        elif all(item.ok for item in items.values()):
            status = STATUS_OK
        else:
            status = STATUS_DEGRADED
        return cls(
            status=status,
            components=items,
            version=version,
            service=service,
            environment=environment,
        )


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Retrato de um provedor: status ao vivo somado a configuracao efetiva."""

    name: str
    kind: str
    status: str = STATUS_OK
    detail: str = ""
    configured: bool = False
    info: Json = field(default_factory=dict)

    def to_dict(self) -> Json:
        """Serializa o provedor; `info` nunca carrega credencial."""
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "configured": self.configured,
            "info": dict(self.info),
        }


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """Quadro completo de provedores para o console (SPEC-0008 secao 5)."""

    providers: list[ProviderInfo] = field(default_factory=list)
    status: str = STATUS_OK
    version: str = ""
    checked_at: str = ""

    def to_dict(self) -> Json:
        """Serializa a resposta de `GET /api/v1/health/providers`."""
        return {
            "status": self.status,
            "version": self.version,
            "checked_at": self.checked_at,
            "providers": [provider.to_dict() for provider in self.providers],
        }


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _HealthUseCase:
    """Base dos casos de uso de saude: guarda o `Container` injetado."""

    __slots__ = ("_container",)

    def __init__(self, container: Container) -> None:
        self._container = container

    @property
    def container(self) -> Container:
        """Container de dependencias desta aplicacao."""
        return self._container

    async def _components(self) -> dict[str, ComponentHealth]:
        """Le `Container.health` e converte cada entrada em :class:`ComponentHealth`.

        `Container.health` ja promete nunca levantar; o `except` aqui e a segunda
        linha de defesa para que uma sonda quebrada nunca transforme `/readyz` em
        um `500` — o que faria o cluster ler "aplicacao com defeito" quando o
        defeito e do proprio termometro.
        """
        try:
            raw = await self._container.health()
        except Exception as exc:  # pragma: no cover - container defeituoso
            _logger.error("health_probe_failed", error=f"{type(exc).__name__}: {exc}")
            return {
                "database": ComponentHealth(
                    name="database",
                    status=STATUS_DOWN,
                    detail=f"sonda de saude indisponivel: {type(exc).__name__}",
                )
            }
        return {name: ComponentHealth.of(name, payload) for name, payload in raw.items()}


# ---------------------------------------------------------------------------
# Casos de uso
# ---------------------------------------------------------------------------
class GetLiveness(_HealthUseCase):
    """Responde ao liveness probe sem tocar em nenhuma dependencia."""

    async def execute(self) -> LivenessReport:
        """Devolve a resposta constante do processo.

        E `async` apenas para manter a assinatura uniforme dos casos de uso: nao
        ha I/O aqui, e nao pode haver — o contrato do `/healthz` e responder
        mesmo com banco, hub de LLM e tracer todos fora do ar.
        """
        app = self._container.settings.app
        return LivenessReport(service=app.name, version=app.version)


class GetReadiness(_HealthUseCase):
    """Responde ao readiness probe agregando a saude de cada componente."""

    async def execute(self) -> ReadinessReport:
        """Devolve o relatorio; `http_status` ja traz o codigo a ser respondido.

        Banco fora do ar -> `down` -> `503`. Provedor degradado -> `degraded` ->
        `200` com o detalhe visivel: a plataforma continua servindo com LLM
        `echo`, embeddings `hashing` e tracer no-op.
        """
        app = self._container.settings.app
        report = ReadinessReport.from_components(
            await self._components(),
            version=app.version,
            service=app.name,
            environment=app.env,
        )
        if not report.ready:
            _logger.error("readiness_down", components=report.failing)
        elif report.status != STATUS_OK:
            _logger.info("readiness_degraded", components=report.failing)
        return report


class GetProviderDetails(_HealthUseCase):
    """Detalha cada provedor externo para a barra de status do console."""

    async def execute(self) -> ProviderReport:
        """Reune status ao vivo e configuracao efetiva, sem expor segredo algum.

        `configured` diz apenas **se** ha credencial; a chave em si vive em
        `SecretStr` e nunca chega ate aqui.
        """
        settings = self._container.settings
        components = await self._components()
        providers = [
            self._database(components),
            self._llm(components),
            self._embeddings(components),
            self._vector_store(components),
            self._tracer(components),
            self._registry(components),
        ]
        report = ReadinessReport.from_components(components)
        return ProviderReport(
            providers=providers,
            status=report.status,
            version=settings.app.version,
            checked_at=utcnow().isoformat(),
        )

    @staticmethod
    def _component(components: Mapping[str, ComponentHealth], name: str) -> ComponentHealth:
        """Recupera o componente pelo nome, assumindo degradacao quando ausente."""
        return components.get(name) or ComponentHealth(
            name=name, status=STATUS_DEGRADED, detail="componente nao sondado"
        )

    def _database(self, components: Mapping[str, ComponentHealth]) -> ProviderInfo:
        """Descreve o banco sem revelar credencial da URL de conexao."""
        db = self._container.settings.db
        health = self._component(components, "database")
        return ProviderInfo(
            name="database",
            kind="persistence",
            status=health.status,
            detail=health.detail,
            configured=True,
            info={
                "dialect": db.url.split("://", 1)[0] if "://" in db.url else _UNKNOWN,
                "sqlite": db.is_sqlite,
                "auto_fallback": db.auto_fallback,
            },
        )

    def _llm(self, components: Mapping[str, ComponentHealth]) -> ProviderInfo:
        """Descreve o hub de LLM e o modelo realmente em uso."""
        llm = self._container.settings.llm
        health = self._component(components, "llm")
        return ProviderInfo(
            name="llm",
            kind="generation",
            status=health.status,
            detail=health.detail,
            configured=bool(llm.api_key_value),
            info={
                "provider": llm.provider,
                "effective_provider": llm.effective_provider,
                "base_url": llm.base_url,
                "model": _safe(lambda: self._container.llm.default_model, llm.model),
                "fallback_model": llm.fallback_model,
            },
        )

    def _embeddings(self, components: Mapping[str, ComponentHealth]) -> ProviderInfo:
        """Descreve o servico de embeddings e a dimensao dos vetores."""
        embedding = self._container.settings.embedding
        health = self._component(components, "embeddings")
        return ProviderInfo(
            name="embeddings",
            kind="embedding",
            status=health.status,
            detail=health.detail,
            configured=bool(embedding.api_key_value),
            info={
                "provider": embedding.provider,
                "effective_provider": embedding.effective_provider,
                "base_url": embedding.base_url,
                "model": _safe(lambda: self._container.embeddings.model, embedding.model),
                "dimensions": _safe(
                    lambda: self._container.embeddings.dimensions, embedding.dimensions
                ),
            },
        )

    def _vector_store(self, components: Mapping[str, ComponentHealth]) -> ProviderInfo:
        """Descreve o armazenamento vetorial, que acompanha a saude do banco."""
        embedding = self._container.settings.embedding
        health = self._component(components, "database")
        return ProviderInfo(
            name="vector_store",
            kind="retrieval",
            status=health.status,
            detail=health.detail,
            configured=True,
            info={
                "backend": type(self._container.vector_store).__name__,
                "collection": embedding.collection,
            },
        )

    def _tracer(self, components: Mapping[str, ComponentHealth]) -> ProviderInfo:
        """Descreve o tracer; sem credencial Langfuse ele degrada para no-op."""
        observability = self._container.settings.observability
        health = self._component(components, "tracer")
        return ProviderInfo(
            name="tracer",
            kind="observability",
            status=health.status,
            detail=health.detail,
            configured=observability.langfuse_configured,
            info={
                "backend": type(self._container.tracer).__name__,
                "enabled": _safe(lambda: bool(self._container.tracer.enabled), False),
                "host": observability.langfuse_host,
            },
        )

    def _registry(self, components: Mapping[str, ComponentHealth]) -> ProviderInfo:
        """Descreve o registry de building blocks e os runtimes disponiveis."""
        health = self._component(components, "registry")
        return ProviderInfo(
            name="registry",
            kind="modules",
            status=health.status,
            detail=health.detail,
            configured=True,
            info={
                "modules": _safe(lambda: len(self._container.registry), 0),
                "runtimes": _safe(lambda: self._container.runtimes, _NO_RUNTIMES),
            },
        )
