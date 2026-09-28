"""Replaceable AI analysis providers.

The trading engine only depends on ``AIProvider``. ``OpenAIProvider`` talks to the
Responses API with a strict JSON-schema output format; ``DisabledProvider`` is
used when no key is configured or AI is switched off. Any provider failure is
surfaced as ``AIUnavailable`` and handled by policy — it never crashes trading.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.ai.schema import AI_RESPONSE_SCHEMA, AIResponse, AIValidationError, validate_response

log = logging.getLogger("kestrel.ai")

# USD per 1M tokens (input, cached input, output). Reasoning tokens bill as output.
# Source: OpenAI pricing page, checked 2026-09-27. Unknown models fall back to the
# most expensive row so cost is over- rather than under-estimated.
PRICING: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
}
_FALLBACK_PRICE = (10.00, 1.00, 50.00)


def estimate_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    price = PRICING.get(model)
    if price is None:
        base = next((k for k in PRICING if model.startswith(k)), None)
        price = PRICING[base] if base else _FALLBACK_PRICE
    pin, pcached, pout = price
    uncached = max(0, input_tokens - cached_tokens)
    return (uncached * pin + cached_tokens * pcached + output_tokens * pout) / 1_000_000


class AIUnavailable(Exception):
    """Provider could not produce a valid analysis (network, timeout, refusal, invalid JSON)."""


@dataclass
class AIResult:
    response: AIResponse
    raw: dict[str, Any]
    model: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


@dataclass
class AIUsageError:
    message: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


SYSTEM_PROMPT = """You are a cautious risk analyst for a BTCUSDT perpetual futures trading system.
You do NOT place or control orders. A deterministic strategy and a risk engine have already
evaluated the market; you receive their structured output and act as an independent second
opinion. You cannot change position size, leverage, stops or limits.

Rules:
- Base your view only on the data provided. Do not invent news, prices or indicators.
- Nobody can predict Bitcoin reliably. Prefer NO_TRADE whenever the evidence is mixed,
  the setup is late, volatility is abnormal, or derivatives data shows crowding.
- For a setup confirmation: decision must be the candidate's direction to agree, or NO_TRADE.
  Never propose the opposite direction as a trade; say NO_TRADE instead.
- confidence is your probability-like conviction (0-100) that the setup's premise is sound,
  not a promise of profit.
- risk_flags: short concrete concerns (e.g. "4h trend bearish", "funding crowded long").
- invalidation: the price/condition that would prove the idea wrong.
- recommended_action: ENTER or SKIP for setups; HOLD, TIGHTEN_STOP or CLOSE for position
  reviews; WAIT for general market reads.
Respond only with the JSON object required by the schema."""


class AIProvider(ABC):
    name = "abstract"

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def analyze(self, task: str, context: dict[str, Any], model: str, reasoning_effort: str,
                      timeout_s: float) -> AIResult: ...

    async def ping(self) -> bool:
        return self.available


class DisabledProvider(AIProvider):
    name = "disabled"

    @property
    def available(self) -> bool:
        return False

    async def analyze(self, task: str, context: dict[str, Any], model: str, reasoning_effort: str,
                      timeout_s: float) -> AIResult:
        raise AIUnavailable("AI provider not configured")


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        from openai import AsyncOpenAI  # imported lazily so tests don't need network setup

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=1)
        self.last_error: str | None = None
        self.last_ok_at: float | None = None

    @property
    def available(self) -> bool:
        return True

    async def ping(self) -> bool:
        try:
            await asyncio.wait_for(self._client.models.list(), timeout=15)
            self.last_ok_at = time.time()
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = type(e).__name__
            return False

    async def analyze(self, task: str, context: dict[str, Any], model: str, reasoning_effort: str,
                      timeout_s: float) -> AIResult:
        t0 = time.monotonic()
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": SYSTEM_PROMPT,
            "input": [{"role": "user", "content": f"TASK: {task}\n\nDATA (JSON):\n{json.dumps(context, separators=(',', ':'), default=str)}"}],
            "text": {"format": {"type": "json_schema", "name": "trade_analysis", "schema": AI_RESPONSE_SCHEMA,
                                "strict": True}},
            "max_output_tokens": 4000,
            "store": False,
        }
        if reasoning_effort and reasoning_effort != "none" and not model.startswith("gpt-4"):
            kwargs["reasoning"] = {"effort": reasoning_effort}
        try:
            resp = await asyncio.wait_for(self._client.responses.create(**kwargs), timeout=timeout_s)
        except TimeoutError as e:
            self.last_error = "timeout"
            raise AIUnavailable(f"OpenAI request timed out after {timeout_s:.0f}s") from e
        except Exception as e:  # noqa: BLE001 - SDK raises many types; all mean "unavailable"
            self.last_error = type(e).__name__
            status = getattr(e, "status_code", None)
            raise AIUnavailable(f"OpenAI error: {type(e).__name__}{f' ({status})' if status else ''}") from e
        latency = int((time.monotonic() - t0) * 1000)

        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cached = int(getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0)
        reasoning = int(getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0)
        cost = estimate_cost(model, in_tok, cached, out_tok)

        refusal = None
        for item in getattr(resp, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", None) == "refusal":
                        refusal = getattr(c, "refusal", "refused")
        text = getattr(resp, "output_text", "") or ""
        raw: dict[str, Any] = {"output_text": text[:4000], "status": getattr(resp, "status", None)}
        if refusal:
            raise _Unavailable(f"model refused: {refusal}", in_tok, out_tok, cost, latency, raw)
        if getattr(resp, "status", "completed") not in ("completed", None):
            raise _Unavailable(f"response status {resp.status}", in_tok, out_tok, cost, latency, raw)
        try:
            data = json.loads(text)
            parsed = validate_response(data)
        except (json.JSONDecodeError, AIValidationError) as e:
            raise _Unavailable(f"invalid AI JSON: {e}", in_tok, out_tok, cost, latency, raw) from e
        self.last_ok_at = time.time()
        self.last_error = None
        return AIResult(response=parsed, raw=data, model=model, input_tokens=in_tok, cached_tokens=cached,
                        output_tokens=out_tok, reasoning_tokens=reasoning, cost_usd=cost, latency_ms=latency)


class _Unavailable(AIUnavailable):
    """AIUnavailable that still carries usage (tokens were spent)."""

    def __init__(self, msg: str, in_tok: int, out_tok: int, cost: float, latency: int, raw: dict[str, Any]):
        super().__init__(msg)
        self.usage = AIUsageError(msg, in_tok, out_tok, cost, latency, raw)


class StaticProvider(AIProvider):
    """Deterministic provider for tests/backtests: returns a fixed (or callable) response."""

    name = "static"

    def __init__(self, response: dict[str, Any] | Exception | None = None, fn=None) -> None:  # noqa: ANN001
        self._response = response
        self._fn = fn
        self.calls: list[dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return True

    async def analyze(self, task: str, context: dict[str, Any], model: str, reasoning_effort: str,
                      timeout_s: float) -> AIResult:
        self.calls.append({"task": task, "context": context})
        data = self._fn(task, context) if self._fn else self._response
        if isinstance(data, Exception):
            raise data
        try:
            parsed = validate_response(data)
        except AIValidationError as e:
            raise AIUnavailable(str(e)) from e
        return AIResult(response=parsed, raw=data, model=model, input_tokens=1000, output_tokens=200,
                        cost_usd=estimate_cost(model, 1000, 0, 200), latency_ms=5)
