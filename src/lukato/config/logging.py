"""Logging estruturado do lukato (structlog + logging da stdlib).

Um unico handler nomeado e instalado no logger raiz, de modo que a chamada de
`configure_logging` seja idempotente: reconfigurar troca o handler em vez de
empilhar duplicatas. Logs de bibliotecas (uvicorn, sqlalchemy, ...) passam pelo
mesmo pipeline via `ProcessorFormatter`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

__all__ = [
    "bind_request_context",
    "clear_request_context",
    "configure_logging",
    "get_logger",
]

_HANDLER_NAME: Final[str] = "lukato.console"
"""Nome do handler instalado no logger raiz (permite substituicao idempotente)."""

NOISY_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "openai", "urllib3")
"""Loggers de terceiros silenciados em WARNING."""

_DEFAULT_LEVEL: Final[int] = logging.INFO


def _resolve_level(level: str | int) -> int:
    """Converte `"INFO"`, `"info"` ou `"20"` no inteiro correspondente."""
    if isinstance(level, int):
        return level
    text = str(level).strip()
    if text.isdigit():
        return int(text)
    return logging.getLevelNamesMapping().get(text.upper(), _DEFAULT_LEVEL)


def _service_processor(service: str) -> Processor:
    """Cria o processador que carimba o nome do servico em cada evento."""

    def add_service(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def configure_logging(
    level: str = "INFO",
    json_logs: bool = False,
    *,
    service: str = "lukato",
) -> None:
    """Configura structlog e o logging da stdlib; chamar duas vezes e seguro."""
    resolved = _resolve_level(level)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _service_processor(service),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor
    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=_stream_supports_color())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)
    handler.setLevel(resolved)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(resolved, logging.WARNING))


def _stream_supports_color() -> bool:
    """Habilita cores apenas quando a saida e um terminal interativo."""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - stream fechado/substituido
        return False


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Devolve um logger estruturado ligado ao logging da stdlib."""
    return structlog.stdlib.get_logger(name)


def bind_request_context(request_id: str, **kw: Any) -> None:
    """Liga `request_id` (e extras) ao contexto assincrono da requisicao."""
    structlog.contextvars.bind_contextvars(request_id=request_id, **kw)


def clear_request_context() -> None:
    """Limpa todo o contexto de requisicao ligado por `bind_request_context`."""
    structlog.contextvars.clear_contextvars()
