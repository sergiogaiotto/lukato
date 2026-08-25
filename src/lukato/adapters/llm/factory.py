"""Escolha do adaptador de LLM a partir de `Settings` (ADR-0003).

A decisao nasce de `settings.llm.effective_provider`, que ja rebaixa
`openai_compatible` para `echo` quando falta credencial. A fabrica nunca abre
conexao e nunca levanta por indisponibilidade de rede: no pior caso a aplicacao
sobe com o eco deterministico, e o log INFO diz exatamente por que.
"""

from __future__ import annotations

from typing import Final

from lukato.adapters.llm.echo import EchoLLM
from lukato.adapters.llm.openai_compatible import OpenAICompatibleLLM
from lukato.config import Settings, get_logger
from lukato.domain.ports.llm import LLMPort

__all__ = ["ECHO_REASONS", "build_llm", "build_llm_with_health"]

_logger = get_logger(__name__)

ECHO_REASONS: Final[dict[str, str]] = {
    "echo": (
        "LUKATO_LLM__PROVIDER=echo: o adaptador deterministico offline foi pedido "
        "explicitamente na configuracao"
    ),
    "missing_api_key": (
        "LUKATO_LLM__API_KEY ausente: sem credencial nao ha como falar com o hub, "
        "entao o adaptador deterministico offline assume no lugar"
    ),
}
"""Motivos possiveis para a aplicacao rodar com o `EchoLLM`, prontos para log e UI."""


def _echo_reason(settings: Settings) -> str:
    """Explica por que o eco foi escolhido, distinguindo pedido explicito de falta de chave."""
    if settings.llm.provider == "echo":
        return ECHO_REASONS["echo"]
    return ECHO_REASONS["missing_api_key"]


def build_llm(settings: Settings) -> LLMPort:
    """Constroi o adaptador de LLM correspondente ao provedor efetivo."""
    if settings.llm.effective_provider == "echo":
        reason = _echo_reason(settings)
        _logger.info(
            "llm_adapter_selected",
            adapter=EchoLLM.provider,
            configured_provider=settings.llm.provider,
            model=EchoLLM(settings).default_model,
            offline=True,
            reason=reason,
        )
        return EchoLLM(settings)
    adapter = OpenAICompatibleLLM(settings)
    _logger.info(
        "llm_adapter_selected",
        adapter=OpenAICompatibleLLM.provider,
        configured_provider=settings.llm.provider,
        model=adapter.default_model,
        base_url=adapter.base_url,
        offline=False,
        reason="credencial presente para o hub compativel com OpenAI",
    )
    return adapter


async def build_llm_with_health(settings: Settings) -> tuple[LLMPort, bool]:
    """Constroi o adaptador e ja devolve o resultado do `health()`.

    E `async` porque a verificacao e I/O (SPEC-0000 secao 14). Nenhuma excecao
    escapa: um adaptador que falhe na verificacao devolve `False` e a aplicacao
    segue subindo em modo degradado.
    """
    adapter = build_llm(settings)
    try:
        healthy = await adapter.health()
    except Exception as exc:
        _logger.warning(
            "llm_health_check_failed",
            adapter=type(adapter).__name__,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return adapter, False
    _logger.info("llm_health_checked", adapter=type(adapter).__name__, healthy=healthy)
    return adapter, healthy
