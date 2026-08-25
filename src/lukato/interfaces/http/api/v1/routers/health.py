"""Rotas de saude e de metricas (SPEC-0008 secao 5).

Este modulo publica **dois** routers, porque as tres perguntas de saude nao vivem
no mesmo lugar:

* :data:`router` — montado sob `/api/v1/health`, e a visao do **console**:
  `live`, `ready` e o detalhe por provedor;
* :data:`root_router` — montado na **raiz** pelo composition root, e a visao do
  **orquestrador**: `/healthz`, `/readyz` e `/metrics`. Um probe de Kubernetes
  nao deve conhecer a versao da API: mudar `/api/v1` para `/api/v2` nao pode
  exigir editar um manifesto de deployment.

A separacao entre liveness e readiness e a regra que evita o incidente classico
de operacao:

* `/healthz` responde uma **constante** e nao toca em dependencia nenhuma. Se o
  banco cair e o liveness consultasse o banco, o Kubernetes mataria todas as
  replicas de uma aplicacao que continuava perfeitamente capaz de servir o que
  nao depende do banco.
* `/readyz` sonda banco, registry, LLM, embeddings e tracer. Somente o **banco**
  derruba a prontidao (`503`); provedor degradado mantem `200` com o detalhe
  visivel, porque a plataforma continua util offline (SPEC-0001 secao 6).

Nenhuma das rotas exige credencial: um probe nao carrega token, e o relatorio de
provedores e construido para nunca conter segredo — apenas o **fato** de haver
credencial configurada.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request, Response, status

from lukato import __version__
from lukato.adapters.observability.metrics import Metrics, get_metrics
from lukato.application.container import Container
from lukato.application.use_cases.health import (
    LIVENESS_STATUS,
    GetLiveness,
    GetProviderDetails,
    GetReadiness,
)
from lukato.interfaces.http.deps import ContainerDep
from lukato.interfaces.http.schemas.common import (
    LivenessResponse,
    ProvidersResponse,
    ReadinessResponse,
    error_responses,
)

__all__ = ["DEFAULT_SERVICE_NAME", "METRICS_MEDIA_TYPE", "root_router", "router"]

DEFAULT_SERVICE_NAME: Final[str] = "lukato"
"""Nome usado pelo liveness quando a aplicacao sobe sem composition root."""

METRICS_MEDIA_TYPE: Final[str] = "text/plain"
"""Tipo declarado no OpenAPI para `/metrics` (o corpo real traz a versao completa)."""

_READINESS_RESPONSES: Final[dict[int | str, dict[str, Any]]] = error_responses(503)
"""`/readyz` e `/health/ready` respondem `503` somente com o banco fora do ar."""

router = APIRouter(prefix="/health", tags=["sistema"])
"""Rotas de saude do console, sob `/api/v1/health`."""

root_router = APIRouter(tags=["sistema"])
"""Probes e metricas montados na raiz, fora do prefixo versionado."""


# ---------------------------------------------------------------------------
# Utilitarios
# ---------------------------------------------------------------------------
def _optional_container(request: Request) -> Container | None:
    """Le o `Container` de `app.state` sem nunca levantar.

    O liveness precisa responder mesmo em uma aplicacao montada sem composition
    root (um teste que instancia `FastAPI()` cru, por exemplo). Exigir o
    container aqui transformaria um defeito de composicao em `500` justamente na
    rota que o orquestrador usa para decidir se mata o processo.
    """
    container = getattr(request.app.state, "container", None)
    return container if isinstance(container, Container) else None


async def _liveness(request: Request) -> LivenessResponse:
    """Resposta constante do liveness, com ou sem composition root."""
    container = _optional_container(request)
    if container is None:
        return LivenessResponse(
            status=LIVENESS_STATUS, service=DEFAULT_SERVICE_NAME, version=__version__
        )
    return LivenessResponse.from_report(await GetLiveness(container).execute())


async def _readiness(container: Container, response: Response) -> ReadinessResponse:
    """Sonda a prontidao e carimba `200` ou `503` conforme o relatorio."""
    report = await GetReadiness(container).execute()
    response.status_code = report.http_status
    return ReadinessResponse.from_report(report)


def _metrics_of(request: Request) -> Metrics:
    """Registro de metricas da aplicacao (injetado em `app.state` ou o do processo)."""
    candidate = getattr(request.app.state, "metrics", None)
    return candidate if isinstance(candidate, Metrics) else get_metrics()


# ---------------------------------------------------------------------------
# `/api/v1/health` — visao do console
# ---------------------------------------------------------------------------
@router.get(
    "/live",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness da aplicacao",
    description=(
        "Responde uma constante enquanto o processo estiver vivo. **Nao** toca em "
        "banco, provedor de LLM, embeddings nem tracer: e a mesma resposta de "
        "`/healthz`, publicada tambem sob o prefixo versionado para o console."
    ),
)
async def liveness(request: Request) -> LivenessResponse:
    """Devolve `{"status": "alive", ...}` sem consultar dependencia alguma."""
    return await _liveness(request)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    responses=_READINESS_RESPONSES,
    summary="Prontidao da instalacao",
    description=(
        "Sonda banco, registry, LLM, embeddings e tracer e devolve a situacao de "
        "cada componente. Somente o **banco** fora do ar responde `503`: um "
        "provedor degradado mantem `200`, porque a plataforma continua util "
        "offline (LLM `echo`, embeddings `hashing`, tracer no-op)."
    ),
)
async def readiness(container: ContainerDep, response: Response) -> ReadinessResponse:
    """Devolve o relatorio de prontidao componente a componente."""
    return await _readiness(container, response)


@router.get(
    "/providers",
    response_model=ProvidersResponse,
    status_code=status.HTTP_200_OK,
    summary="Detalhe dos provedores externos",
    description=(
        "Quadro completo para a barra de status do console: status ao vivo somado "
        "a configuracao efetiva de banco, LLM, embeddings, armazenamento vetorial, "
        "tracer e registry. Nenhum segredo entra no relatorio — `configured` diz "
        "apenas **se** existe credencial, nunca qual e."
    ),
)
async def providers(container: ContainerDep) -> ProvidersResponse:
    """Devolve o retrato de cada provedor, com a configuracao publica de cada um."""
    return ProvidersResponse.from_report(await GetProviderDetails(container).execute())


# ---------------------------------------------------------------------------
# Raiz — visao do orquestrador
# ---------------------------------------------------------------------------
@root_router.get(
    "/healthz",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe (Kubernetes)",
    description=(
        "Probe de vida do processo. Resposta constante, **sem** tocar em nenhuma "
        "dependencia: enquanto o processo consegue responder, ele esta vivo. "
        "Montada na raiz de proposito, para que o manifesto do cluster nao dependa "
        "da versao da API."
    ),
)
async def healthz(request: Request) -> LivenessResponse:
    """Responde ao probe de liveness do orquestrador."""
    return await _liveness(request)


@root_router.get(
    "/readyz",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    responses=_READINESS_RESPONSES,
    summary="Readiness probe (Kubernetes)",
    description=(
        "Probe de prontidao. Devolve `503` **somente** quando o banco esta fora do "
        "ar; qualquer outra degradacao mantem `200` com o componente afetado "
        "descrito no corpo, para que uma queda do hub externo nao tire a replica "
        "do balanceador."
    ),
)
async def readyz(container: ContainerDep, response: Response) -> ReadinessResponse:
    """Responde ao probe de prontidao do orquestrador."""
    return await _readiness(container, response)


@root_router.get(
    "/metrics",
    response_class=Response,
    status_code=status.HTTP_200_OK,
    summary="Metricas Prometheus",
    description=(
        "Exposicao das nove metricas normativas da SPEC-0008 secao 4 no formato de "
        "texto do Prometheus. O label `path` guarda sempre o **template** da rota "
        "(`/api/v1/modules/{slug}`), nunca o valor concreto, para conter a "
        "cardinalidade das series."
    ),
    responses={
        200: {
            "description": "Exposicao no formato de texto do Prometheus",
            "content": {METRICS_MEDIA_TYPE: {"schema": {"type": "string"}}},
        }
    },
)
async def metrics(request: Request) -> Response:
    """Serializa o registro de metricas do processo."""
    content, media_type = _metrics_of(request).render()
    return Response(content=content, media_type=media_type)
