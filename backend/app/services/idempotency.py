import hashlib
import json
from typing import Any


def _json_default(value: Any) -> str:
    return str(value)


def make_idempotency_key(*parts: Any) -> str:
    normalized = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
