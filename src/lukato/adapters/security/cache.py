"""Cache em memoria com TTL e limitador de taxa por janela deslizante (SPEC-0006 secao 4).

`InMemoryCache` implementa `CachePort` para o processo corrente: sem Redis, sem rede,
sem dependencia opcional. Serve o `RateLimitMiddleware` e respostas quentes de leitura.
Como o estado vive no processo, **nao** e compartilhado entre replicas — em Kubernetes
com varias replicas o limite efetivo e `limit x replicas`; trocar por um cache
distribuido e apenas implementar a mesma porta.

Todo acesso passa por um `asyncio.Lock`: `allow()` faz leitura-modificacao-escrita e,
sem exclusao mutua, duas corrotinas concorrentes leriam a mesma contagem e ambas
passariam do limite.

Expiracao e preguicosa (na leitura) com uma varredura amortizada na escrita, e ha teto
de entradas com despejo FIFO: um cache sem teto alimentado por chaves de requisicao e
um vazamento de memoria lento.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from lukato.domain.errors import ValidationError

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_NAMESPACE",
    "InMemoryCache",
    "SlidingWindowRateLimiter",
]

DEFAULT_MAX_ENTRIES: Final[int] = 10_000
"""Teto de chaves vivas; ao estourar, a entrada mais antiga e despejada."""

DEFAULT_NAMESPACE: Final[str] = "ratelimit"
"""Prefixo das chaves do limitador, para nao colidir com o cache de aplicacao."""

_PURGE_EVERY: Final[int] = 64
"""Escritas entre duas varreduras completas de entradas expiradas."""


@dataclass(slots=True)
class _Entry:
    """Valor guardado com o instante de expiracao (monotonico, em segundos)."""

    value: Any
    expires_at: float | None

    def is_expired(self, now: float) -> bool:
        """True quando a entrada ja passou do seu tempo de vida."""
        return self.expires_at is not None and now >= self.expires_at


class InMemoryCache:
    """Implementa `CachePort` em memoria, com TTL e seguranca para concorrencia."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configura o teto de entradas e a fonte de tempo (monotonica por padrao)."""
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._writes_since_purge = 0

    @property
    def max_entries(self) -> int:
        """Numero maximo de chaves mantidas simultaneamente."""
        return self._max_entries

    async def get(self, key: str) -> Any | None:
        """Devolve o valor vivo da chave, ou `None` se ausente ou expirado."""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.is_expired(self._clock()):
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        """Grava o valor; `ttl_seconds` nulo ou nao positivo significa sem expiracao."""
        async with self._lock:
            now = self._clock()
            expires_at = now + float(ttl_seconds) if ttl_seconds and ttl_seconds > 0 else None
            self._entries[key] = _Entry(value=value, expires_at=expires_at)
            self._entries.move_to_end(key)
            self._writes_since_purge += 1
            if self._writes_since_purge >= _PURGE_EVERY:
                self._purge_expired(now)
                self._writes_since_purge = 0
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def delete(self, key: str) -> None:
        """Remove a chave, se existir."""
        async with self._lock:
            self._entries.pop(key, None)

    async def clear(self) -> None:
        """Esvazia o cache inteiro."""
        async with self._lock:
            self._entries.clear()
            self._writes_since_purge = 0

    async def size(self) -> int:
        """Numero de entradas ainda vivas (expiradas sao descartadas na contagem)."""
        async with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)

    def _purge_expired(self, now: float) -> None:
        """Descarta todas as entradas vencidas (chamado sob o lock)."""
        expired = [key for key, entry in self._entries.items() if entry.is_expired(now)]
        for key in expired:
            del self._entries[key]


class SlidingWindowRateLimiter:
    """Limitador por janela deslizante sobre qualquer `CachePort`.

    Guarda, por chave, os instantes das chamadas aceitas dentro da janela. Diferente
    do balde fixo, nao existe a virada de janela que deixaria passar `2 x limit`
    chamadas na fronteira entre dois periodos.
    """

    def __init__(
        self,
        cache: InMemoryCache | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Usa o cache informado (ou cria um proprio em memoria) e o relogio dado."""
        self._cache = cache if cache is not None else InMemoryCache(clock=clock)
        self._namespace = namespace.strip() or DEFAULT_NAMESPACE
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def cache(self) -> InMemoryCache:
        """Cache subjacente, exposto para inspecao e limpeza em testes."""
        return self._cache

    def _key(self, key: str) -> str:
        """Monta a chave namespaced usada no cache."""
        return f"{self._namespace}:{key}"

    async def allow(self, key: str, limit: int, window: float) -> tuple[bool, int]:
        """Registra uma chamada e devolve `(permitida, restantes)` na janela atual.

        A chamada de numero `limit + 1` dentro da mesma janela devolve
        `(False, 0)` e **nao** e contabilizada, para que a rajada bloqueada nao
        estenda indefinidamente a punicao.
        """
        span = float(window)
        if span <= 0:
            raise ValidationError(
                f"janela do rate limiter deve ser positiva, recebido {window!r}",
                details={"window": window},
            )
        quota = max(0, int(limit))
        cache_key = self._key(key)
        async with self._lock:
            now = self._clock()
            floor = now - span
            recorded = await self._cache.get(cache_key)
            hits = [stamp for stamp in _as_timestamps(recorded) if stamp > floor]
            if len(hits) >= quota:
                if hits:
                    await self._cache.set(cache_key, list(hits), ttl_seconds=span)
                return False, 0
            hits.append(now)
            await self._cache.set(cache_key, list(hits), ttl_seconds=span)
            return True, quota - len(hits)

    async def remaining(self, key: str, limit: int, window: float) -> int:
        """Quantas chamadas ainda cabem na janela, sem consumir nenhuma."""
        span = max(float(window), 0.0)
        quota = max(0, int(limit))
        async with self._lock:
            floor = self._clock() - span
            recorded = await self._cache.get(self._key(key))
            hits = [stamp for stamp in _as_timestamps(recorded) if stamp > floor]
            return max(0, quota - len(hits))

    async def reset(self, key: str) -> None:
        """Zera o historico da chave (usado em testes e no desbloqueio manual)."""
        async with self._lock:
            await self._cache.delete(self._key(key))


def _as_timestamps(value: Any) -> list[float]:
    """Le a lista de instantes gravada no cache, ignorando lixo de outro produtor."""
    if not isinstance(value, list):
        return []
    stamps: list[float] = []
    for item in value:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            stamps.append(float(item))
    return stamps
