"""Cache optionnel Redis avec repli mémoire (M11 préparation)."""
from __future__ import annotations

import json
import os
import time
from typing import Any

_REDIS = None
_MEM: dict[str, tuple[Any, float | None]] = {}
_REDIS_URL = os.getenv("BAZO_REDIS_URL", "").strip()


def _client():
    global _REDIS
    if _REDIS is False:
        return None
    if _REDIS is not None:
        return _REDIS
    if not _REDIS_URL:
        _REDIS = False
        return None
    try:
        import redis
        _REDIS = redis.from_url(_REDIS_URL, decode_responses=True)
        _REDIS.ping()
        return _REDIS
    except Exception:
        _REDIS = False
        return None


def cache_get(key: str) -> Any | None:
    r = _client()
    if r:
        val = r.get(key)
        return json.loads(val) if val else None
    item = _MEM.get(key)
    if not item:
        return None
    val, exp = item
    if exp and time.time() > exp:
        _MEM.pop(key, None)
        return None
    return val


def cache_set(key: str, value: Any, ttl_sec: int = 300) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    r = _client()
    if r:
        r.setex(key, ttl_sec, payload)
        return
    _MEM[key] = (value, time.time() + ttl_sec if ttl_sec else None)


def cache_delete(key: str) -> None:
    r = _client()
    if r:
        r.delete(key)
    _MEM.pop(key, None)


def cache_status() -> dict[str, Any]:
    r = _client()
    if r:
        try:
            r.ping()
            return {"backend": "redis", "ok": True}
        except Exception:
            return {"backend": "redis", "ok": False}
    return {"backend": "memory", "ok": True}
