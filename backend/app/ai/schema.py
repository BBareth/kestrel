"""Strict JSON schema for AI analysis responses.

The same schema is (a) sent to OpenAI as a strict Structured Output format and
(b) re-validated locally with ``jsonschema`` — the model's output is never
trusted just because the API claims it conforms.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from pydantic import BaseModel, Field

AI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision",
        "confidence",
        "market_regime",
        "reasoning",
        "risk_flags",
        "invalidation",
        "recommended_action",
    ],
    "properties": {
        "decision": {"type": "string", "enum": ["LONG", "SHORT", "NO_TRADE"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "market_regime": {
            "type": "string",
            "enum": ["trending_up", "trending_down", "ranging", "high_volatility", "quiet", "uncertain"],
        },
        "reasoning": {"type": "string", "minLength": 1, "maxLength": 1200},
        "risk_flags": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 10},
        "invalidation": {"type": "string", "maxLength": 400},
        "recommended_action": {
            "type": "string",
            "enum": ["ENTER", "SKIP", "HOLD", "TIGHTEN_STOP", "CLOSE", "WAIT"],
        },
    },
}

_validator = jsonschema.Draft202012Validator(AI_RESPONSE_SCHEMA)


class AIResponse(BaseModel):
    decision: str
    confidence: int = Field(ge=0, le=100)
    market_regime: str
    reasoning: str
    risk_flags: list[str]
    invalidation: str
    recommended_action: str


class AIValidationError(ValueError):
    pass


def validate_response(data: Any) -> AIResponse:
    errors = sorted(_validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        msgs = "; ".join(f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors[:5])
        raise AIValidationError(f"AI response failed schema validation: {msgs}")
    return AIResponse.model_validate(data)
