#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dify_media_client
import media_service
import provider_runtime
import server


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value


class _FakeRequest:
    def __init__(self, token: str):
        self.cookies = {server.AUTH_COOKIE_NAME: token}


class _DummyHistory:
    def __init__(self, *args, **kwargs):
        _ = (args, kwargs)
        self.messages = []

    def add_user_message(self, _msg: str) -> None:
        return

    def add_ai_message(self, _msg: str) -> None:
        return


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


@contextmanager
def _patch_attrs(module, patches: dict[str, Any]):
    original: dict[str, Any] = {}
    for key, val in patches.items():
        original[key] = getattr(module, key)
        setattr(module, key, val)
    try:
        yield
    finally:
        for key, val in original.items():
            setattr(module, key, val)


def _as_status_data(resp_obj: Any) -> tuple[int, dict[str, Any]]:
    if isinstance(resp_obj, JSONResponse):
        status = int(resp_obj.status_code or 200)
        try:
            body = json.loads((resp_obj.body or b"{}").decode("utf-8"))
            return status, body if isinstance(body, dict) else {}
        except Exception:
            return status, {}
    if isinstance(resp_obj, dict):
        return 200, resp_obj
    return 200, {}


def _blank_profile() -> dict[str, str]:
    return {
        "name": "",
        "birthdate": "",
        "birthtime": "",
        "preferred_name": "",
        "gender": "male",
        "partner_gender_preference": "unknown",
        "name_confidence": "none",
        "preferred_name_confidence": "none",
    }


def _complete_profile() -> dict[str, str]:
    profile = _blank_profile()
    profile.update({"name": "时窗测试", "birthdate": "2001-01-01"})
    return profile


def _fortune_payload(provider_code: str, code: str = "FORTUNE_TIMEOUT", category: str = "timeout") -> dict[str, Any]:
    return {
        "topic": "daily",
        "strength": "balanced",
        "fortune_signals": {"love": "", "wealth": "", "career": "近期节奏宜稳，不宜冒进。"},
        "risk_points": ["避免一口气接太多事。"],
        "opportunity_points": ["先把一个关键动作做扎实。"],
        "time_hints": ["适合先稳住这几天节奏。"],
        "evidence_lines": ["当前按保守窗口解读。"],
        "advice": ["先做一件最重要的小事。", "把待办减到 3 项以内。"],
        "confidence": 0.2,
        "question_type": "default",
        "window_text": "2026年3月7日至2026年3月9日",
        "window_label": "near_days",
        "error": {
            "code": code,
            "message": "命理服务暂时不可用",
            "provider": "yuanfenju",
            "provider_code": provider_code,
            "category": category,
            "degraded": True,
        },
    }


def case_fortune_timeout_chat_fallback() -> dict[str, Any]:
    payload = _fortune_payload("YUANFENJU_TIMEOUT")

    def fake_route(*args, **kwargs):
        reply = server.render_user_fortune_reply_v2(
            payload,
            "daily",
            query="我今天整体运势最该注意什么？",
            question_type="default",
            window_meta={"window_text": payload["window_text"], "label": payload["window_label"]},
            session_id="fortune-user-uuid",
        )
        return reply, dict(payload)

    patches = {
        "_get_auth_session": lambda token: {"phone": "13800000011", "user_uuid": "fortune-user-uuid"} if token == "token-fortune" else None,
        "_get_user_by_phone": lambda phone: {"id": 201, "uuid": "fortune-user-uuid", "phone": phone} if phone == "13800000011" else None,
        "build_time_anchor": lambda: {
            "today_cn": "2026年3月7日",
            "weekday_cn": "星期六",
            "tz_name": "Asia/Shanghai",
            "utc_offset": "UTC+08:00",
            "near_days": [{"date_cn": "3月7日", "weekday_cn": "星期六"}],
        },
        "get_v2_flags": lambda: dict(server.V2_FLAG_DEFAULTS),
        "apply_v2_flag_policy": lambda _raw: (dict(server.V2_FLAG_DEFAULTS), "none"),
        "_render_v3_enabled": lambda: True,
        "detect_domain_intent": lambda _q: "fortune",
        "detect_question_type": lambda _q: "default",
        "_need_time_window": lambda _q, question_type="default": True,
        "date_window_resolver": lambda _q, _anchor: {"window_text": payload["window_text"], "label": payload["window_label"]},
        "RedisChatMessageHistory": _DummyHistory,
        "extract_profile_from_history": lambda _history: _complete_profile(),
        "merge_session_profile": lambda _sid, _current: _complete_profile(),
        "_is_preferred_name_prompt_pending": lambda _sid: False,
        "extract_profile_from_query": lambda _query: {},
        "detect_media_intent": lambda _query: {"hit": False},
        "route_dream_pipeline": lambda _q: (None, None),
        "route_zodiac_pipeline": lambda _q, allow_clarify=False: (None, None),
        "route_fortune_pipeline": fake_route,
        "_append_chat_history": lambda *args, **kwargs: None,
        "_log_route_observability": lambda *args, **kwargs: None,
        "track_output_quality": lambda *args, **kwargs: None,
    }
    with _patch_attrs(server, patches):
        resp_obj = asyncio.run(server.chat(_FakeRequest("token-fortune"), server.ChatRequest(query="我今天整体运势最该注意什么？")))
        status, data = _as_status_data(resp_obj)
    assert status == 200, data
    assert bool(((data.get("extra") or {}).get("provider_fallback"))), data
    assert str(((data.get("extra") or {}).get("provider_error_code") or "")) == "YUANFENJU_TIMEOUT", data
    output = str(data.get("output") or "")
    assert "时间窗口" in output and "建议" in output and "依据" in output, output
    return {"status_code": status, "extra": data.get("extra"), "output_preview": output[:120]}


def case_fortune_quota_opens_breaker() -> dict[str, Any]:
    fake_redis = FakeRedis()
    failure = provider_runtime.build_provider_failure(
        provider="yuanfenju",
        operation="fortune_submit",
        category="quota",
        error_code="YUANFENJU_PROVIDER_LIMIT",
        error_message="余额不足",
        retryable=False,
    )
    provider_runtime.provider_record_failure(fake_redis, failure)
    state = provider_runtime.provider_should_short_circuit(fake_redis, "yuanfenju", "fortune_submit")
    assert bool(state.get("short_circuit")), state
    assert str(state.get("last_error_code") or "") == "YUANFENJU_PROVIDER_LIMIT", state
    return state


def case_fortune_invalid_response_fallback() -> dict[str, Any]:
    payload = _fortune_payload("YUANFENJU_INVALID_RESPONSE", code="FORTUNE_PARSE_FAILED", category="invalid_response")
    text = server._build_fortune_provider_safe_fallback(
        payload,
        "daily",
        query="分析一下我今年的运势",
        question_type="default",
        time_anchor=server.build_time_anchor(),
        window_meta={"window_text": "2026年1月1日至2026年12月31日", "label": "year_full"},
        session_id="fortune-invalid",
    )
    assert "结论" in text and "依据" in text and "建议" in text, text
    assert "2026年" in text, text
    return {"output_preview": text[:160]}


def case_media_provider_limit_fail_fast() -> dict[str, Any]:
    failed_task = {
        "task_id": "task-limit",
        "status": "failed",
        "scenario": "general_image",
        "error_code": "DIFY_PROVIDER_LIMIT",
        "error_message": "safe experience mode",
        "output_json": {},
    }
    patches = {
        "_get_auth_session": lambda token: {"phone": "13800000012", "user_uuid": "media-user-uuid"} if token == "token-media" else None,
        "_get_user_by_phone": lambda phone: {"id": 202, "uuid": "media-user-uuid", "phone": phone} if phone == "13800000012" else None,
        "merge_session_profile": lambda _sid, _current: _blank_profile(),
        "_create_and_submit_media_task": lambda **kwargs: (dict(failed_task), "DIFY_PROVIDER_LIMIT"),
    }
    with _patch_attrs(server, patches):
        resp_obj = asyncio.run(
            server.create_media_task_api(
                _FakeRequest("token-media"),
                server.MediaTaskCreateRequest(query="帮我生成一张海报", scenario="general_image"),
            )
        )
        status, data = _as_status_data(resp_obj)
    assert status == 503, data
    assert str(data.get("error_code") or "") == "MEDIA_PROVIDER_DEGRADED", data
    assert str(data.get("provider_error_code") or "") == "DIFY_PROVIDER_LIMIT", data
    return {"status_code": status, "body": data}


def case_media_poll_5xx_retry_then_success() -> dict[str, Any]:
    store = {
        "task_id": "task-poll",
        "user_id": 203,
        "session_id": "poll-user",
        "status": "running",
        "dify_run_id": "run-poll",
        "scenario": "general_video",
        "created_at": datetime.now(),
        "output_json": {},
        "error_code": "",
        "error_message": "",
    }

    def fake_get_media_task(_conn_factory, task_id: str, *, user_id: int = 0):
        if task_id != store["task_id"] or int(user_id or 0) != int(store["user_id"]):
            return None
        return dict(store)

    def fake_update(_conn_factory, task_id: str, **fields):
        assert task_id == store["task_id"], (task_id, store)
        store.update(fields)

    class FakeClient:
        def __init__(self):
            self.step = 0

        def get_workflow_status(self, workflow_run_id: str, *, user: str = "") -> dict[str, Any]:
            _ = (workflow_run_id, user)
            self.step += 1
            if self.step == 1:
                return {
                    "status": "failed",
                    "media": [],
                    "raw": {},
                    "error_code": "DIFY_HTTP_5XX",
                    "error_message": "server error",
                    "error_category": "http_5xx",
                }
            return {
                "status": "succeeded",
                "media": [{"kind": "video", "url": "https://example.com/demo.mp4"}],
                "raw": {},
                "error_code": "",
                "error_message": "",
                "error_category": "",
            }

    patches = {"get_media_task": fake_get_media_task, "_update_task_row": fake_update}
    with _patch_attrs(media_service, patches):
        client = FakeClient()
        first = media_service.refresh_media_task(None, client, task_id="task-poll", user_id=203, user_identity="poll-user", timeout_seconds=80)
        assert str((first or {}).get("status") or "") == "running", first
        second = media_service.refresh_media_task(None, client, task_id="task-poll", user_id=203, user_identity="poll-user", timeout_seconds=80)
        assert str((second or {}).get("status") or "") == "succeeded", second
    return {"first_status": first.get("status"), "second_status": second.get("status")}


def case_media_auth_failure_opens_breaker() -> dict[str, Any]:
    fake_redis = FakeRedis()
    called = {"count": 0}

    def fake_post(*args, **kwargs):
        called["count"] += 1
        return _FakeResponse(401, {"message": "Access token is invalid"}, text='{"message":"Access token is invalid"}')

    client = dify_media_client.DifyMediaClient(base_url="http://localhost/v1", api_key="app-invalid", workflow_app_id="wf-1", timeout_seconds=10)
    with _patch_attrs(provider_runtime, {"_REDIS": fake_redis}), _patch_attrs(dify_media_client.requests, {"post": fake_post}):
        result = client.submit_workflow(scenario="general_image", prompt="test", user="u1", inputs={"prompt": "test"})
        state = provider_runtime.provider_should_short_circuit(fake_redis, "dify", "media_submit")
    assert called["count"] == 1, called
    assert str(result.get("error_code") or "") == "DIFY_HTTP_401", result
    assert bool(state.get("short_circuit")), state
    return {"submit": result, "breaker": state}


def case_breaker_opens_after_three_timeouts() -> dict[str, Any]:
    fake_redis = FakeRedis()
    failure = provider_runtime.build_provider_failure(
        provider="dify",
        operation="media_poll",
        category="timeout",
        error_code="DIFY_TIMEOUT",
        error_message="timeout",
    )
    for _ in range(3):
        provider_runtime.provider_record_failure(fake_redis, failure)
    state = provider_runtime.provider_should_short_circuit(fake_redis, "dify", "media_poll")
    assert bool(state.get("short_circuit")), state
    return state


def case_breaker_open_skips_real_upstream_call() -> dict[str, Any]:
    fake_redis = FakeRedis()
    provider_runtime.provider_record_failure(
        fake_redis,
        provider_runtime.build_provider_failure(
            provider="dify",
            operation="media_submit",
            category="quota",
            error_code="DIFY_PROVIDER_LIMIT",
            error_message="quota exceeded",
            retryable=False,
        ),
    )
    called = {"count": 0}

    def fail_if_called(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("requests.post should not be called when breaker is open")

    client = dify_media_client.DifyMediaClient(base_url="http://localhost/v1", api_key="app-test", workflow_app_id="wf-1", timeout_seconds=10)
    with _patch_attrs(provider_runtime, {"_REDIS": fake_redis}), _patch_attrs(dify_media_client.requests, {"post": fail_if_called}):
        result = client.submit_workflow(scenario="general_image", prompt="test", user="u1", inputs={"prompt": "test"})
    assert str(result.get("error_code") or "") == "DIFY_BREAKER_OPEN", result
    assert called["count"] == 0, called
    return result


def case_media_breaker_open_chat_reply() -> dict[str, Any]:
    failed_task = {
        "task_id": "task-breaker",
        "status": "failed",
        "scenario": "general_image",
        "error_code": "DIFY_BREAKER_OPEN",
        "error_message": "媒体生成服务暂时不可用",
        "output_json": {},
    }
    patches = {
        "_get_auth_session": lambda token: {"phone": "13800000013", "user_uuid": "media-chat-user"} if token == "token-media-chat" else None,
        "_get_user_by_phone": lambda phone: {"id": 204, "uuid": "media-chat-user", "phone": phone} if phone == "13800000013" else None,
        "build_time_anchor": lambda: {
            "today_cn": "2026年3月7日",
            "weekday_cn": "星期六",
            "tz_name": "Asia/Shanghai",
            "utc_offset": "UTC+08:00",
            "near_days": [],
        },
        "get_v2_flags": lambda: dict(server.V2_FLAG_DEFAULTS),
        "apply_v2_flag_policy": lambda _raw: (dict(server.V2_FLAG_DEFAULTS), "none"),
        "detect_domain_intent": lambda _q: "media",
        "detect_question_type": lambda _q: "media",
        "_need_time_window": lambda _q, question_type="default": False,
        "RedisChatMessageHistory": _DummyHistory,
        "extract_profile_from_history": lambda _history: _blank_profile(),
        "merge_session_profile": lambda _sid, _current: _blank_profile(),
        "_is_preferred_name_prompt_pending": lambda _sid: False,
        "extract_profile_from_query": lambda _query: {},
        "detect_media_intent": lambda _query: {"hit": True, "scenario": "general_image", "blocked": False, "blocked_reason": ""},
        "_resolve_media_intent": lambda _query, _sid: (
            {"hit": True, "scenario": "general_image", "blocked": False, "blocked_reason": ""},
            {"route": "media_create", "reason_code": "mock_media_create", "confidence": "high", "media_like": True},
        ),
        "_create_and_submit_media_task": lambda **kwargs: (dict(failed_task), "DIFY_BREAKER_OPEN"),
        "_append_chat_history": lambda *args, **kwargs: None,
        "_log_route_observability": lambda *args, **kwargs: None,
        "track_output_quality": lambda *args, **kwargs: None,
    }
    with _patch_attrs(server, patches):
        resp_obj = asyncio.run(server.chat(_FakeRequest("token-media-chat"), server.ChatRequest(query="帮我生成一张城市夜景海报")))
        status, data = _as_status_data(resp_obj)
    assert status == 200, data
    assert str(data.get("message_type") or "") == "media_failed", data
    assert str(((data.get("extra") or {}).get("provider_error_code") or "")) == "DIFY_BREAKER_OPEN", data
    assert "降级保护" in str(data.get("output") or "") or "暂时不可用" in str(data.get("output") or ""), data
    return {"status_code": status, "extra": data.get("extra"), "output_preview": str(data.get("output") or "")[:120]}


def _run_case(case_id: str, title: str, fn) -> dict[str, Any]:
    try:
        detail = fn()
        return {"id": case_id, "title": title, "ok": True, "detail": detail}
    except AssertionError as e:
        return {"id": case_id, "title": title, "ok": False, "error": f"AssertionError: {e}"}
    except Exception as e:
        return {"id": case_id, "title": title, "ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="provider 降级与回退回归")
    parser.add_argument("--out", default="", help="可选：输出 JSON 报告")
    args = parser.parse_args()

    cases = [
        ("PROV-001", "命理 provider timeout 走安全回退", case_fortune_timeout_chat_fallback),
        ("PROV-002", "命理 quota 立即打开 breaker", case_fortune_quota_opens_breaker),
        ("PROV-003", "命理 invalid_response 仍输出结构化回退", case_fortune_invalid_response_fallback),
        ("PROV-004", "媒体 provider limit fail-fast", case_media_provider_limit_fail_fast),
        ("PROV-005", "媒体 poll 5xx 重试后可恢复成功", case_media_poll_5xx_retry_then_success),
        ("PROV-006", "媒体 auth failure 打开 breaker", case_media_auth_failure_opens_breaker),
        ("PROV-007", "连续 timeout/http_5xx 打开 breaker", case_breaker_opens_after_three_timeouts),
        ("PROV-008", "breaker open 后不再请求上游", case_breaker_open_skips_real_upstream_call),
        ("REG-PROV-002", "media breaker open 时 chat 返回稳定失败提示", case_media_breaker_open_chat_reply),
    ]

    results = [_run_case(cid, title, fn) for cid, title, fn in cases]
    summary = {
        "total": len(results),
        "passed": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "results": results,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
