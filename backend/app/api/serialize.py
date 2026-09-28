from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import inspect


def to_dict(obj: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    out: dict[str, Any] = {}
    for col in inspect(obj).mapper.column_attrs:
        k = col.key
        if k in exclude:
            continue
        v = getattr(obj, k)
        if isinstance(v, datetime):
            v = v.isoformat()
        out[k] = v
    return out
