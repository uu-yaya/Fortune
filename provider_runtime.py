import json
import time
from typing import TypedDict

import redis

from config import REDIS_URL


PROVIDER_CATEGORIES = {
    "quota",
    "timeout",
    "http_4xx",
    "http_5xx",
    "auth",
    "network",
    "invalid_payload",
    "invalid_response",
    "safety",
    "unknown",
}
BREAKER_WINDOW_SECONDS = {
    "timeout": 120,
    "network": 120,
    "http_5xx": 120,
    "invalid_response": 300,
}
BREAKER_FAILURE_THRESHOLD = {
    "timeout": 3,
    "network": 3,
    "http_5xx": 3,
    "invalid_response": 2,
}
BREAKER_TTL_SECONDS = {
    "quota": 1800,
    "auth": 1800,
    "timeout": 300,
    "network": 300,
    "http_5xx": 300,
    "invalid_response": 300,
    "http_4xx": 300,
    "invalid_payload": 300,
    "safety": 300,
    "unknown": 300,
}
_REDIS = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _default_state() -> dict:
    return {
        "state": "closed",
        "fail_count": 0,
        "last_error_code": "",
        "last_category": "",
        "last_ts": 0,
        "open_until": 0,
    }


class ProviderFailure(TypedDict):
    provider: str
    operation: str
    category: str
    error_code: str
    error_message: str
    retryable: bool
    breaker_ttl_seconds: int
    http_status: int
    raw_error: str


def build_provider_failure(
    *,
    provider: str,
    operation: str,
    category: str,
    error_code: str,
    error_message: str,
    retryable: bool | None = None,
    breaker_ttl_seconds: int | None = None,
    http_status: int = 0,
    raw_error: str = "",
) -> ProviderFailure:
    normalized = category if category in PROVIDER_CATEGORIES else "unknown"
    retryable_val = retryable if retryable is not None else normalized in {"timeout", "network", "http_5xx"}
    ttl = int(breaker_ttl_seconds or provider_breaker_ttl({"category": normalized}))  # type: ignore[arg-type]
    return {
        "provider": str(provider or "").strip().lower(),
        "operation": str(operation or "").strip().lower(),
        "category": normalized,
        "error_code": str(error_code or "").strip().upper(),
        "error_message": str(error_message or "").strip(),
        "retryable": bool(retryable_val),
        "breaker_ttl_seconds": ttl,
        "http_status": int(http_status or 0),
        "raw_error": str(raw_error or "").strip(),
    }


def provider_health_key(provider: str, operation: str) -> str:
    return f"provider:health:{str(provider or '').strip().lower()}:{str(operation or '').strip().lower()}"


def provider_breaker_should_open(failure: ProviderFailure) -> bool:
    category = str(failure.get("category") or "unknown")
    return category in {"quota", "auth"}


def provider_breaker_ttl(failure: ProviderFailure | dict) -> int:
    category = str((failure or {}).get("category") or "unknown")
    return int(BREAKER_TTL_SECONDS.get(category, 300))


def _load_state(redis_client, provider: str, operation: str) -> dict:
    client = redis_client or _REDIS
    try:
        raw = client.get(provider_health_key(provider, operation))
    except Exception:
        return _default_state()
    if not raw:
        return _default_state()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return _default_state()


def provider_should_short_circuit(redis_client, provider: str, operation: str) -> dict:
    client = redis_client or _REDIS
    now = int(time.time())
    state = _load_state(client, provider, operation)
    open_until = int(state.get("open_until") or 0)
    if str(state.get("state") or "closed") == "open" and open_until > now:
        return {
            "short_circuit": True,
            "state": "open",
            "provider": str(provider or "").strip().lower(),
            "operation": str(operation or "").strip().lower(),
            "open_until": open_until,
            "last_error_code": str(state.get("last_error_code") or ""),
            "last_category": str(state.get("last_category") or ""),
        }
    if str(state.get("state") or "closed") == "open" and open_until <= now:
        state["state"] = "half_open"
        try:
            client.setex(provider_health_key(provider, operation), 300, json.dumps(state, ensure_ascii=False))
        except Exception:
            pass
        return {
            "short_circuit": False,
            "state": "half_open",
            "provider": str(provider or "").strip().lower(),
            "operation": str(operation or "").strip().lower(),
            "open_until": 0,
            "last_error_code": str(state.get("last_error_code") or ""),
            "last_category": str(state.get("last_category") or ""),
        }
    return {
        "short_circuit": False,
        "state": str(state.get("state") or "closed"),
        "provider": str(provider or "").strip().lower(),
        "operation": str(operation or "").strip().lower(),
        "open_until": open_until,
        "last_error_code": str(state.get("last_error_code") or ""),
        "last_category": str(state.get("last_category") or ""),
    }


def provider_record_failure(redis_client, failure: ProviderFailure) -> None:
    client = redis_client or _REDIS
    provider = str(failure.get("provider") or "")
    operation = str(failure.get("operation") or "")
    category = str(failure.get("category") or "unknown")
    now = int(time.time())
    state = _load_state(client, provider, operation)
    window_seconds = int(BREAKER_WINDOW_SECONDS.get(category, 0))
    threshold = int(BREAKER_FAILURE_THRESHOLD.get(category, 1))
    last_ts = int(state.get("last_ts") or 0)
    fail_count = int(state.get("fail_count") or 0)
    if provider_breaker_should_open(failure):
        fail_count = 1
        open_until = now + int(failure.get("breaker_ttl_seconds") or provider_breaker_ttl(failure))
        state = {
            "state": "open",
            "fail_count": fail_count,
            "last_error_code": str(failure.get("error_code") or ""),
            "last_category": category,
            "last_ts": now,
            "open_until": open_until,
        }
        try:
            client.setex(provider_health_key(provider, operation), max(60, open_until - now), json.dumps(state, ensure_ascii=False))
        except Exception:
            return
        return
    if window_seconds and last_ts and (now - last_ts) > window_seconds:
        fail_count = 0
    fail_count += 1
    state = {
        "state": "closed",
        "fail_count": fail_count,
        "last_error_code": str(failure.get("error_code") or ""),
        "last_category": category,
        "last_ts": now,
        "open_until": 0,
    }
    if fail_count >= threshold:
        ttl = int(failure.get("breaker_ttl_seconds") or provider_breaker_ttl(failure))
        state["state"] = "open"
        state["open_until"] = now + ttl
        try:
            client.setex(provider_health_key(provider, operation), max(60, ttl), json.dumps(state, ensure_ascii=False))
        except Exception:
            return
        return
    try:
        client.setex(provider_health_key(provider, operation), max(60, window_seconds or 300), json.dumps(state, ensure_ascii=False))
    except Exception:
        return


def provider_record_success(redis_client, provider: str, operation: str) -> None:
    client = redis_client or _REDIS
    state = {
        "state": "closed",
        "fail_count": 0,
        "last_error_code": "",
        "last_category": "",
        "last_ts": int(time.time()),
        "open_until": 0,
    }
    try:
        client.setex(provider_health_key(provider, operation), 300, json.dumps(state, ensure_ascii=False))
    except Exception:
        return


def provider_extra_meta(failure: ProviderFailure | None, degraded: bool) -> dict[str, str | bool]:
    if not failure:
        return {"provider_fallback": bool(degraded)}
    return {
        "provider": str(failure.get("provider") or ""),
        "provider_category": str(failure.get("category") or ""),
        "provider_error_code": str(failure.get("error_code") or ""),
        "provider_fallback": bool(degraded),
    }
