"""Juiz multimodal Qwen no fim do funil do AdWatch (SPEC-0010 secao 4).

O modelo multimodal e caro e fica **no fim** do pipeline, nunca no comeco: e chamado
somente na faixa de revisao (`review_threshold <= S < accept_threshold`), depois que
o alinhamento textual ja fez o trabalho pesado.

Contrato de saida: JSON estrito. A SPEC e explicita — **resposta que nao for JSON
valido nao promove o candidato**. Por isso o caminho de erro devolve
`{"commercial_detected": False, "parse_error": True, "visual_match": None, ...}`:
`visual_match` nulo significa "sinal ausente, ignore na fusao", e nao "sinal zero",
que puniria injustamente o candidato. Quem consome deve checar `parse_error` antes de
usar qualquer numero.

Neste ambiente o hub da Claro nao responde, entao a falha de rede e o caminho normal:
ela vira o mesmo veredito neutro, com `error` preenchido, sem excecao vazando e sem
travar a deteccao.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Final

from lukato.config import Settings, get_logger
from lukato.domain.errors import LukatoError, UnsupportedCapability, ValidationError
from lukato.domain.models.adwatch import Commercial
from lukato.domain.ports.llm import ChatMessage, LLMPort
from lukato.domain.ports.media import MediaProbePort
from lukato.domain.types import Json

__all__ = [
    "CLIP_PREFIX",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "JUDGE_SYSTEM_PROMPT",
    "MAX_EXCERPT_CHARS",
    "QwenVisionJudge",
    "format_timecode",
]

_logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
"""Teto da chamada ao juiz: alem disso o veredito neutro sai mais barato que esperar."""

DEFAULT_MAX_TOKENS: Final[int] = 512
"""O veredito e um JSON curto; nao ha razao para pagar mais tokens de saida."""

MAX_EXCERPT_CHARS: Final[int] = 4000
"""Recorte do trecho de transcricao enviado no prompt."""

CLIP_PREFIX: Final[str] = "judge"
"""Prefixo dos arquivos de recorte gravados em `Settings.adwatch.workdir`."""

JUDGE_SYSTEM_PROMPT: Final[str] = (
    "Voce e um auditor de veiculacao publicitaria. Recebe o TEXTO CONHECIDO de um "
    "comercial catalogado e um TRECHO de midia (intervalo de tempo mais a transcricao "
    "correspondente) e decide se aquele trecho e, de fato, a veiculacao daquele "
    "comercial. Seja conservador: na duvida, responda commercial_detected=false. "
    "Responda EXCLUSIVAMENTE com um unico objeto JSON valido, sem markdown, sem "
    "cercas de codigo e sem texto antes ou depois, exatamente neste formato: "
    '{"commercial_detected": true, "commercial_id": "COM_000234", "confidence": 0.96, '
    '"start": "01:21:33.4", "end": "01:22:03.1", "evidence": {"speech_match": 0.94, '
    '"visual_match": 0.97, "ocr_match": 0.88, "brand_detected": "Claro"}}'
)
"""Prompt de julgamento normativo da SPEC-0010 secao 4."""

_JSON_OBJECT: Final[Json] = {"type": "json_object"}
_FENCE = re.compile(r"^```[A-Za-z0-9_+\-]*\s*|\s*```$")
_MAX_RAW_CHARS: Final[int] = 800
_SECONDS_PER_MINUTE: Final[int] = 60
_MINUTES_PER_HOUR: Final[int] = 60
_EVIDENCE_KEYS: Final[tuple[str, ...]] = ("speech_match", "visual_match", "ocr_match")


class QwenVisionJudge:
    """Implementa `VisionJudgePort` chamando o hub Qwen atraves do `LLMPort`."""

    def __init__(
        self,
        llm: LLMPort | None,
        settings: Settings,
        *,
        probe: MediaProbePort | None = None,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Guarda o provedor de LLM, a configuracao e o recortador opcional de midia."""
        self._llm = llm
        self._settings = settings
        self._probe = probe
        self._model = model
        self._timeout = max(1.0, float(timeout_seconds))

    @property
    def available(self) -> bool:
        """True quando ha provedor de LLM com `base_url` e `api_key` configurados."""
        if self._llm is None:
            return False
        llm_settings = self._settings.llm
        return bool(llm_settings.base_url.strip() and llm_settings.api_key_value)

    @property
    def model(self) -> str:
        """Modelo usado no julgamento (o do adaptador ou o padrao configurado)."""
        return self._model or self._settings.llm.model

    async def verify(
        self,
        *,
        media_uri: str,
        start: float,
        end: float,
        commercial: Commercial,
        transcript_excerpt: str,
    ) -> Json:
        """Julga se `[start, end]` de `media_uri` e a veiculacao de `commercial`."""
        source = media_uri.strip() if media_uri else ""
        if not source:
            raise ValidationError(
                "caminho de midia vazio em verify",
                details={"capability": "vision"},
            )
        begin = max(0.0, float(start))
        finish = float(end)
        if finish < begin:
            raise ValidationError(
                f"intervalo invalido para julgamento: exige start <= end, recebido "
                f"start={begin} end={finish}",
                details={"capability": "vision", "start": begin, "end": finish},
            )
        if not self.available:
            raise UnsupportedCapability(
                "juiz multimodal indisponivel: configure LUKATO_LLM__BASE_URL e "
                "LUKATO_LLM__API_KEY para habilitar a verificacao visual",
                details={
                    "capability": "vision",
                    "base_url": self._settings.llm.base_url,
                    "has_api_key": bool(self._settings.llm.api_key_value),
                },
            )

        clip_path = await self._clip(source, begin, finish, commercial)
        prompt = _build_prompt(
            commercial=commercial,
            start=begin,
            end=finish,
            transcript_excerpt=transcript_excerpt,
            clip_path=clip_path,
            media_uri=source,
        )
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._ask(prompt),
                timeout=self._timeout,
            )
        except TimeoutError:
            return _unverified(
                reason="timeout",
                message=f"o juiz multimodal nao respondeu em {self._timeout:.0f}s",
                commercial=commercial,
                clip_path=clip_path,
                model=self.model,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except LukatoError as exc:
            _logger.warning(
                "vision_judge_provider_error",
                error=type(exc).__name__,
                code=exc.code,
                commercial=commercial.commercial_id,
            )
            return _unverified(
                reason="provider_error",
                message=exc.message,
                commercial=commercial,
                clip_path=clip_path,
                model=self.model,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        parsed = _parse_verdict(response.content)
        if parsed is None:
            _logger.warning(
                "vision_judge_invalid_json",
                commercial=commercial.commercial_id,
                model=response.model,
                preview=response.content[:200],
            )
            return _unverified(
                reason="invalid_json",
                message="o juiz multimodal nao devolveu JSON valido",
                commercial=commercial,
                clip_path=clip_path,
                model=response.model,
                latency_ms=latency_ms,
                raw=response.content,
            )
        verdict = _normalize(parsed, commercial=commercial)
        verdict.update(
            {
                "parse_error": False,
                "model": response.model,
                "latency_ms": round(latency_ms, 3),
                "clip_path": clip_path,
            }
        )
        _logger.info(
            "vision_judge_verdict",
            commercial=commercial.commercial_id,
            detected=verdict["commercial_detected"],
            confidence=verdict["confidence"],
            model=response.model,
        )
        return verdict

    async def _ask(self, prompt: str) -> Any:
        """Chama o LLM pedindo objeto JSON e temperatura zero (veredito estavel)."""
        llm = self._llm
        if llm is None:
            raise UnsupportedCapability(
                "juiz multimodal sem provedor de LLM injetado",
                details={"capability": "vision"},
            )
        return await llm.chat(
            [ChatMessage.system(JUDGE_SYSTEM_PROMPT), ChatMessage.user(prompt)],
            model=self._model,
            temperature=0.0,
            max_tokens=DEFAULT_MAX_TOKENS,
            response_format=_JSON_OBJECT,
            metadata={"capability": "vision", "stage": "adwatch_judge"},
        )

    async def _clip(
        self, media_uri: str, start: float, end: float, commercial: Commercial
    ) -> str | None:
        """Recorta o trecho quando ha `MediaProbePort`; falha aqui nao aborta o juiz."""
        if self._probe is None or not self._probe.available or end <= start:
            return None
        workdir = self._settings.adwatch.workdir.rstrip("/") or "."
        target = (
            f"{workdir}/{CLIP_PREFIX}_{commercial.commercial_id}_"
            f"{int(start * 1000)}_{int(end * 1000)}.mp4"
        )
        try:
            return await self._probe.cut(media_uri, start, end, target)
        except LukatoError as exc:
            _logger.warning(
                "vision_judge_clip_failed",
                error=type(exc).__name__,
                code=exc.code,
                uri=media_uri,
                start=start,
                end=end,
            )
            return None


# --------------------------------------------------------------------------- #
# Prompt e parsing
# --------------------------------------------------------------------------- #


def _build_prompt(
    *,
    commercial: Commercial,
    start: float,
    end: float,
    transcript_excerpt: str,
    clip_path: str | None,
    media_uri: str,
) -> str:
    """Monta a mensagem de usuario com o comercial esperado e o trecho observado."""
    excerpt = (transcript_excerpt or "").strip()[:MAX_EXCERPT_CHARS]
    keywords = ", ".join(commercial.keywords) if commercial.keywords else "(nenhuma)"
    phrases = " | ".join(commercial.key_phrases) if commercial.key_phrases else "(nenhuma)"
    lines = [
        "COMERCIAL ESPERADO",
        f"commercial_id: {commercial.commercial_id}",
        f"marca: {commercial.brand}",
        f"campanha: {commercial.campaign}",
        f"duracao esperada: {commercial.duration_expected:.1f}s",
        f"palavras-chave: {keywords}",
        f"frases-chave: {phrases}",
        "texto conhecido:",
        commercial.text.strip(),
        "",
        "TRECHO OBSERVADO",
        f"arquivo: {clip_path or media_uri}",
        f"inicio: {format_timecode(start)}",
        f"fim: {format_timecode(end)}",
        f"duracao observada: {max(0.0, end - start):.1f}s",
        "transcricao do trecho:",
        excerpt or "(sem transcricao disponivel)",
        "",
        "Responda apenas com o objeto JSON do formato especificado.",
    ]
    return "\n".join(lines)


def format_timecode(seconds: float) -> str:
    """Formata segundos como `HH:MM:SS.s`, o formato exigido no JSON de saida."""
    total = max(0.0, float(seconds))
    hours = int(total // (_SECONDS_PER_MINUTE * _MINUTES_PER_HOUR))
    minutes = int((total // _SECONDS_PER_MINUTE) % _MINUTES_PER_HOUR)
    rest = total - hours * _SECONDS_PER_MINUTE * _MINUTES_PER_HOUR - minutes * _SECONDS_PER_MINUTE
    return f"{hours:02d}:{minutes:02d}:{rest:04.1f}"


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """Extrai o objeto JSON do veredito; `None` quando a resposta nao e utilizavel."""
    payload = (text or "").strip()
    if not payload:
        return None
    if payload.startswith("```"):
        payload = _FENCE.sub("", payload).strip()
    if not payload.startswith("{"):
        opening = payload.find("{")
        closing = payload.rfind("}")
        if opening < 0 or closing <= opening:
            return None
        payload = payload[opening : closing + 1]
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or "commercial_detected" not in parsed:
        return None
    if not isinstance(parsed.get("commercial_detected"), bool):
        return None
    return parsed


def _normalize(parsed: dict[str, Any], *, commercial: Commercial) -> Json:
    """Normaliza o veredito do modelo no dicionario estavel devolvido pela porta."""
    detected = bool(parsed.get("commercial_detected"))
    confidence = _ratio(parsed.get("confidence"), default=1.0 if detected else 0.0)
    raw_evidence = parsed.get("evidence")
    evidence_in = raw_evidence if isinstance(raw_evidence, dict) else {}
    evidence: Json = {key: _ratio(evidence_in.get(key), default=0.0) for key in _EVIDENCE_KEYS}
    brand = evidence_in.get("brand_detected")
    evidence["brand_detected"] = str(brand).strip() if isinstance(brand, str) and brand else None
    visual = _ratio(evidence_in.get("visual_match"), default=confidence if detected else 0.0)
    evidence["visual_match"] = visual
    return {
        "commercial_detected": detected,
        "commercial_id": str(parsed.get("commercial_id") or commercial.commercial_id),
        "confidence": confidence,
        "start": _timecode_field(parsed.get("start")),
        "end": _timecode_field(parsed.get("end")),
        "evidence": evidence,
        "visual_match": visual,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def _unverified(
    *,
    reason: str,
    message: str,
    commercial: Commercial,
    clip_path: str | None,
    model: str,
    latency_ms: float,
    raw: str | None = None,
) -> Json:
    """Veredito neutro: sem promocao do candidato e com `visual_match` ausente.

    `visual_match=None` e deliberado. A SPEC-0010 manda **nao considerar** o sinal
    visual quando o juiz falha; devolver `0.0` seria considerar o sinal e derrubar o
    score de um candidato que talvez fosse legitimo.
    """
    verdict: Json = {
        "commercial_detected": False,
        "parse_error": True,
        "commercial_id": commercial.commercial_id,
        "confidence": 0.0,
        "visual_match": None,
        "evidence": {
            "speech_match": 0.0,
            "visual_match": None,
            "ocr_match": 0.0,
            "brand_detected": None,
        },
        "reason": reason,
        "error": message,
        "model": model,
        "latency_ms": round(latency_ms, 3),
        "clip_path": clip_path,
    }
    if raw is not None:
        verdict["raw"] = raw[:_MAX_RAW_CHARS]
    return verdict


def _ratio(value: Any, *, default: float) -> float:
    """Converte para `[0.0, 1.0]`, caindo no padrao quando o valor nao serve."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return min(1.0, max(0.0, number))


def _timecode_field(value: Any) -> str | None:
    """Aceita `"01:21:33.4"` ou segundos numericos e devolve sempre o texto formatado."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_timecode(float(value))
    return None
