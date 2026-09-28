"""AI confirmation / analysis service: policy, budget, caching and audit.

The AI layer is an *analysis and veto* layer:
* In ``required`` mode a setup proceeds only if the model agrees with the
  direction at or above the confidence threshold.
* In ``advisory`` mode the analysis is recorded and shown but never gates.
* The model can never approve a trade the risk engine rejected — the engine only
  calls it for setups that already passed risk.
* Unavailability (errors, timeouts, invalid JSON, refusals) and budget exhaustion
  are handled by explicit, configurable policies.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.ai.provider import AIProvider, AIResult, AIUnavailable
from app.core.store import Store, day_start
from app.db.types import utcnow

log = logging.getLogger("kestrel.ai")


@dataclass
class AIGate:
    allowed: bool
    reason: str
    analysis_id: int | None = None
    decision: str | None = None
    confidence: float | None = None
    market_regime: str | None = None
    used_ai: bool = False
    degraded: bool = False  # AI unavailable/over budget and policy allowed deterministic fallback


def apply_policy(mode: str, direction: str, result: AIResult | None, error: str | None, over_budget: bool,
                 p: dict[str, Any]) -> AIGate:
    """Pure decision function (unit-tested exhaustively)."""
    if mode == "off":
        return AIGate(True, "AI confirmation off")
    if over_budget:
        if mode == "advisory" or p["ai_budget_policy"] == "deterministic":
            return AIGate(True, "AI budget exhausted — deterministic mode", degraded=True)
        return AIGate(False, "AI budget exhausted — policy: no trade", degraded=True)
    if result is None:
        if mode == "advisory" or p["ai_unavailable_policy"] == "deterministic":
            return AIGate(True, f"AI unavailable ({error}) — deterministic mode", degraded=True)
        return AIGate(False, f"AI unavailable ({error}) — policy: no trade", degraded=True)
    r = result.response
    base = dict(decision=r.decision, confidence=float(r.confidence), market_regime=r.market_regime, used_ai=True)
    if mode == "advisory":
        return AIGate(True, f"AI advisory: {r.decision} ({r.confidence})", **base)
    if r.decision != direction:
        return AIGate(False, f"AI disagrees: {r.decision} ({r.confidence})", **base)
    if r.confidence < p["ai_min_confidence"]:
        return AIGate(False, f"AI confidence {r.confidence} below {p['ai_min_confidence']:.0f}", **base)
    if r.recommended_action not in ("ENTER",):
        return AIGate(False, f"AI recommends {r.recommended_action}", **base)
    return AIGate(True, f"AI confirms {r.decision} ({r.confidence})", **base)


class AIService:
    def __init__(self, provider: AIProvider, store: Store):
        self.provider = provider
        self.store = store
        self.last_status: str = "unknown"
        self.last_error: str | None = None

    async def spend_today(self) -> tuple[float, int]:
        return await self.store.ai_spend_since(day_start(utcnow()))

    async def over_budget(self, p: dict[str, Any]) -> bool:
        spent, _ = await self.spend_today()
        return spent >= float(p["ai_daily_budget_usd"])

    async def _run(self, trigger: str, task: str, context: dict[str, Any], p: dict[str, Any],
                   cache_key: str | None = None, cache_ttl: timedelta = timedelta(minutes=10)
                   ) -> tuple[AIResult | None, int | None, str | None]:
        """Returns (result, analysis_id, error). Never raises."""
        if cache_key:
            cached = await self.store.cached_analysis(cache_key, cache_ttl)
            if cached is not None and cached.response:
                from app.ai.schema import validate_response

                try:
                    resp = validate_response(cached.response)
                    return AIResult(response=resp, raw=cached.response, model=cached.model), cached.id, None
                except Exception:  # noqa: BLE001 - stale/invalid cache entry: make a fresh call
                    log.debug("ignoring invalid cached AI analysis %s", cached.id)
        if not self.provider.available:
            self.last_status = "disabled"
            return None, None, "not configured"
        model = str(p["ai_model"])
        try:
            res = await self.provider.analyze(task, context, model, str(p["ai_reasoning_effort"]),
                                              float(p["ai_timeout_s"]))
        except AIUnavailable as e:
            usage = getattr(e, "usage", None)
            self.last_status = "error"
            self.last_error = str(e)[:300]
            a = await self.store.record_analysis(
                trigger=trigger, provider=self.provider.name, model=model, cache_key=cache_key,
                request=_summarize(context), response=None, valid=False, error=str(e)[:2000],
                input_tokens=getattr(usage, "input_tokens", 0), output_tokens=getattr(usage, "output_tokens", 0),
                cost_usd=getattr(usage, "cost_usd", 0.0), latency_ms=getattr(usage, "latency_ms", 0))
            await self.store.system_event("warning", "ai", "ai_unavailable", f"AI analysis failed: {e}")
            return None, a.id, str(e)[:200]
        except Exception as e:  # noqa: BLE001 - defensive: provider bug must not break trading
            self.last_status = "error"
            self.last_error = type(e).__name__
            log.exception("AI provider crashed")
            return None, None, f"provider error {type(e).__name__}"
        self.last_status = "ok"
        self.last_error = None
        r = res.response
        a = await self.store.record_analysis(
            trigger=trigger, provider=self.provider.name, model=res.model, cache_key=cache_key,
            request=_summarize(context), response=res.raw, valid=True, decision=r.decision,
            confidence=float(r.confidence), market_regime=r.market_regime, input_tokens=res.input_tokens,
            output_tokens=res.output_tokens, reasoning_tokens=res.reasoning_tokens, cost_usd=res.cost_usd,
            latency_ms=res.latency_ms)
        return res, a.id, None

    async def confirm_setup(self, context: dict[str, Any], direction: str, bar_time: int,
                            p: dict[str, Any]) -> AIGate:
        mode = p["ai_mode"]
        if mode == "off":
            return apply_policy(mode, direction, None, None, False, p)
        if await self.over_budget(p):
            return apply_policy(mode, direction, None, None, True, p)
        key = _key("setup", direction, bar_time, context.get("candidate", {}).get("level"))
        task = (f"Confirm or reject the {direction} setup proposed by the deterministic strategy. "
                f"Answer {direction} only if you agree it is a sound, well-timed setup; otherwise NO_TRADE.")
        res, aid, err = await self._run("setup", task, context, p, cache_key=key)
        gate = apply_policy(mode, direction, res, err, False, p)
        gate.analysis_id = aid
        return gate

    async def analyze(self, trigger: str, context: dict[str, Any], p: dict[str, Any], task: str
                      ) -> tuple[AIResult | None, int | None, str | None]:
        """Advisory analysis (regime change, periodic, position review, manual). Never gates trading."""
        if p["ai_mode"] == "off":
            return None, None, "AI off"
        if await self.over_budget(p):
            return None, None, "daily AI budget exhausted"
        return await self._run(trigger, task, context, p)


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:40]


def _summarize(context: dict[str, Any]) -> dict[str, Any]:
    """Store the full structured request (it contains no secrets) but cap its size."""
    raw = json.dumps(context, default=str)
    if len(raw) <= 60_000:
        return json.loads(raw)
    return {"truncated": True, "keys": list(context.keys())}
