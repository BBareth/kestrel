"""Structured JSON logging with secret redaction.

Every record passes through ``RedactingFilter`` which scrubs the literal values of
all configured secrets plus common credential patterns (Binance signatures, API-key
headers, OpenAI keys, bearer tokens). Redaction happens on the fully formatted
message *and* on the structured ``extra`` fields.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_PATTERNS = [
    re.compile(r"(signature=)[0-9a-fA-F]{16,}"),
    re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]{16,}", re.I),
    re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{16,}", re.I),
    re.compile(r"(listenKey=)[A-Za-z0-9]{16,}"),
    re.compile(r"(password['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.I),
]

_STD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = sorted({s for s in (secrets or []) if s}, key=len, reverse=True)

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "[REDACTED]")
        for p in _PATTERNS:
            text = p.sub(lambda m: m.group(1) + "[REDACTED]", text)
        return text

    def _redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self._redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._redact_obj(v) for v in obj]
        return obj

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            msg = str(record.msg)
        record.msg = self.redact(msg)
        record.args = None
        for k, v in list(record.__dict__.items()):
            if k not in _STD_ATTRS:
                record.__dict__[k] = self._redact_obj(v)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


_filter = RedactingFilter()


def setup_logging(level: str = "INFO", secrets: list[str] | None = None, service: str = "kestrel") -> None:
    _filter.set_secrets(secrets or [])
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_filter)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Quiet noisy libraries; their errors still surface.
    for name in ("httpx", "httpcore", "websockets", "asyncio", "urllib3", "openai", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("kestrel").info("logging configured", extra={"service": service})


def redact(text: str) -> str:
    return _filter.redact(text)
