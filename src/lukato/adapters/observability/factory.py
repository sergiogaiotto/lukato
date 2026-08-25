"""Escolha do tracer a partir de `Settings` (SPEC-0008 secao 3, ADR-0003).

A regra e simples e explicita: o `LangfuseTracer` so entra quando o Langfuse esta
habilitado **e** as duas chaves existem. Em qualquer outro caso volta o `NoopTracer`,
e o log INFO diz o motivo — quem le `/readyz`, o console ou o log sabe na hora se a
plataforma esta produzindo traces ou rodando cega.

A fabrica nunca abre conexao e nunca levanta: no pior caso a aplicacao sobe sem
telemetria, o que e um degrade aceitavel, ao contrario de um boot que falha.
"""

from __future__ import annotations

from typing import Final

from pydantic import SecretStr

from lukato.adapters.observability.langfuse_tracer import LangfuseTracer
from lukato.adapters.observability.noop_tracer import NoopTracer
from lukato.config import Settings, get_logger
from lukato.domain.ports.observability import TracerPort

__all__ = ["NOOP_REASONS", "build_tracer", "build_tracer_with_health"]

_logger = get_logger(__name__)

NOOP_REASONS: Final[dict[str, str]] = {
    "disabled": (
        "LUKATO_OBSERVABILITY__LANGFUSE_ENABLED=false: o tracing para o Langfuse foi "
        "desligado na configuracao, entao o tracer inerte assume no lugar"
    ),
    "missing_credentials": (
        "LUKATO_OBSERVABILITY__LANGFUSE_PUBLIC_KEY/SECRET_KEY ausentes: sem as duas "
        "chaves nao ha como autenticar no Langfuse, entao o tracer inerte assume"
    ),
    "auth_check_failed": (
        "Langfuse.auth_check() falhou no boot: o backend esta inalcancavel ou as chaves "
        "sao invalidas, entao o tracer inerte assume e /readyz reporta tracer degradado"
    ),
}
"""Motivos possiveis para a aplicacao rodar com o `NoopTracer`, prontos para log e UI."""


def _secret(value: SecretStr | None) -> str:
    """Extrai o texto de um `SecretStr` opcional, devolvendo string vazia quando ausente."""
    return value.get_secret_value() if value is not None else ""


def build_tracer(settings: Settings) -> TracerPort:
    """Constroi o tracer correspondente a configuracao efetiva."""
    observability = settings.observability
    if not observability.langfuse_enabled:
        _logger.info(
            "tracer_selected",
            adapter="noop",
            enabled=False,
            reason=NOOP_REASONS["disabled"],
        )
        return NoopTracer()
    if not observability.langfuse_configured:
        _logger.info(
            "tracer_selected",
            adapter="noop",
            enabled=False,
            host=observability.langfuse_host,
            reason=NOOP_REASONS["missing_credentials"],
        )
        return NoopTracer()
    tracer = LangfuseTracer(
        _secret(observability.langfuse_public_key),
        _secret(observability.langfuse_secret_key),
        observability.langfuse_host,
        environment=settings.app.env,
        release=settings.app.version,
    )
    _logger.info(
        "tracer_selected",
        adapter="langfuse",
        enabled=tracer.enabled,
        host=observability.langfuse_host,
        environment=settings.app.env,
        release=settings.app.version,
        reason="langfuse habilitado com as duas chaves presentes",
    )
    return tracer


async def build_tracer_with_health(settings: Settings) -> tuple[TracerPort, bool]:
    """Constroi o tracer e confirma as credenciais, degradando para `NoopTracer` se falhar.

    SPEC-0008 secao 3: um `auth_check()` que falha no boot vira WARNING e `NoopTracer`,
    e `/readyz` deve reportar o tracer como degradado — nunca derrubar a aplicacao.
    """
    tracer = build_tracer(settings)
    if not isinstance(tracer, LangfuseTracer):
        return tracer, False
    healthy = await tracer.health()
    if healthy:
        _logger.info("tracer_health_checked", adapter="langfuse", host=tracer.host, healthy=True)
        return tracer, True
    _logger.warning(
        "tracer_degraded",
        adapter="noop",
        host=tracer.host,
        reason=NOOP_REASONS["auth_check_failed"],
    )
    await tracer.aclose()
    return NoopTracer(), False
