#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
import profile_concurrency_regression as profile_concurrency
import provider_degradation_regression as provider_regression


def _blank_profile() -> dict[str, str]:
    return {
        "name": "",
        "birthdate": "",
        "birthtime": "",
        "preferred_name": "",
        "gender": "unknown",
        "partner_gender_preference": "unknown",
        "name_confidence": "none",
        "preferred_name_confidence": "none",
    }


def _safe_json(resp) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _FakeRequest:
    def __init__(self, token: str):
        self.cookies = {server.AUTH_COOKIE_NAME: token}


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


def case_media_session_bound_to_auth_user() -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_get_auth(token: str) -> dict[str, str] | None:
        if token == "token-attacker":
            return {"phone": "13800000001", "user_uuid": "attacker-uuid"}
        return None

    def fake_get_user(phone: str) -> dict[str, Any] | None:
        if phone == "13800000001":
            return {"id": 101, "uuid": "attacker-uuid", "phone": phone, "account": "JIYI-ATTACK"}
        return None

    def fake_merge(session_id: str, current: dict[str, str]) -> dict[str, str]:
        captured["session_id"] = session_id
        captured["current"] = dict(current or {})
        return _blank_profile()

    def fake_submit(**kwargs) -> tuple[None, str]:
        _ = kwargs
        return None, "media_unavailable"

    patches = {
        "_get_auth_session": fake_get_auth,
        "_get_user_by_phone": fake_get_user,
        "merge_session_profile": fake_merge,
        "_create_and_submit_media_task": fake_submit,
    }
    with _patch_attrs(server, patches):
        req = _FakeRequest("token-attacker")
        payload = server.MediaTaskCreateRequest(
            query="帮我生成一张图片，我叫攻击者，出生于1999-02-03，我是男生",
            scenario="general_image",
            session_id="victim-uuid",
        )
        resp_obj = asyncio.run(server.create_media_task_api(req, payload))
        status, data = _as_status_data(resp_obj)

    assert status == 503, f"expected 503, got {status}, body={data}"
    assert captured.get("session_id") == "attacker-uuid", f"session bound mismatch: {captured}"
    assert captured.get("session_id") != "victim-uuid", f"trusted client session_id unexpectedly: {captured}"
    return {
        "status_code": status,
        "session_used_for_merge": captured.get("session_id"),
        "payload_session_id": "victim-uuid",
    }


def case_media_profile_check_only_for_required_scenarios() -> dict[str, Any]:
    def fake_get_auth(token: str) -> dict[str, str] | None:
        if token == "token-u":
            return {"phone": "13800000002", "user_uuid": "user-uuid-2"}
        return None

    def fake_get_user(phone: str) -> dict[str, Any] | None:
        if phone == "13800000002":
            return {"id": 102, "uuid": "user-uuid-2", "phone": phone, "account": "JIYI-U2"}
        return None

    def fake_merge(session_id: str, current: dict[str, str]) -> dict[str, str]:
        _ = (session_id, current)
        return _blank_profile()

    def fake_missing(profile: dict[str, str]) -> list[str]:
        _ = profile
        return ["name", "birthdate", "gender"]

    def fake_submit(**kwargs) -> tuple[None, str]:
        _ = kwargs
        return None, "media_unavailable"

    patches = {
        "_get_auth_session": fake_get_auth,
        "_get_user_by_phone": fake_get_user,
        "merge_session_profile": fake_merge,
        "_missing_profile_fields_for_fortune": fake_missing,
        "_create_and_submit_media_task": fake_submit,
    }
    with _patch_attrs(server, patches):
        req = _FakeRequest("token-u")
        general_obj = asyncio.run(
            server.create_media_task_api(
                req, server.MediaTaskCreateRequest(query="帮我生成一张治愈海报", scenario="general_image")
            )
        )
        general_status, general_data = _as_status_data(general_obj)
        destined_obj = asyncio.run(
            server.create_media_task_api(
                req, server.MediaTaskCreateRequest(query="帮我生成正缘写实画像", scenario="destined_portrait")
            )
        )
        destined_status, destined_data = _as_status_data(destined_obj)

    assert general_status == 503, f"general_image should bypass profile-400, got {general_status} {general_data}"
    assert destined_status == 400, f"destined_portrait should enforce profile, got {destined_status} {destined_data}"
    assert "missing_fields" in destined_data and "gender" in (destined_data.get("missing_fields") or []), destined_data
    return {
        "general_image_status": general_status,
        "general_image_body": general_data,
        "destined_portrait_status": destined_status,
        "destined_portrait_body": destined_data,
    }


def case_chat_profile_gate_only_for_fortune_paths() -> dict[str, Any]:
    class DummyHistory:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)
            self.messages = []

        def add_user_message(self, _msg: str) -> None:
            return

        def add_ai_message(self, _msg: str) -> None:
            return

    def fake_get_auth(token: str) -> dict[str, str] | None:
        if token == "token-chat":
            return {"phone": "13800000003", "user_uuid": "chat-user-uuid"}
        return None

    def fake_get_user(phone: str) -> dict[str, Any] | None:
        if phone == "13800000003":
            return {"id": 103, "uuid": "chat-user-uuid", "phone": phone, "account": "JIYI-CHAT"}
        return None

    def fake_merge(session_id: str, current: dict[str, str]) -> dict[str, str]:
        _ = (session_id, current)
        return _blank_profile()

    def fake_domain_intent(query: str) -> str:
        return "fortune" if "运势" in str(query or "") else "general"

    def fake_fast_reply(query: str, time_anchor=None, profile=None) -> str:
        _ = (time_anchor, profile)
        if "你好" in str(query or ""):
            return "普通闲聊可用"
        return ""

    patches = {
        "_get_auth_session": fake_get_auth,
        "_get_user_by_phone": fake_get_user,
        "build_time_anchor": lambda: {
            "today_cn": "2026-03-03",
            "weekday_cn": "星期二",
            "tz_name": "Asia/Shanghai",
            "utc_offset": "+08:00",
            "near_days": [],
        },
        "get_v2_flags": lambda: dict(server.V2_FLAG_DEFAULTS),
        "apply_v2_flag_policy": lambda _raw: (dict(server.V2_FLAG_DEFAULTS), "none"),
        "detect_domain_intent": fake_domain_intent,
        "detect_question_type": lambda _q: "default",
        "_need_time_window": lambda _q, question_type="default": False,
        "RedisChatMessageHistory": DummyHistory,
        "extract_profile_from_history": lambda _history: _blank_profile(),
        "merge_session_profile": fake_merge,
        "_is_preferred_name_prompt_pending": lambda _sid: False,
        "extract_profile_from_query": lambda _query: {},
        "detect_media_intent": lambda _query: {"hit": False},
        "is_bazi_fortune_query": lambda q: "运势" in str(q or ""),
        "is_divination_query": lambda _q: False,
        "is_zodiac_intent_query": lambda _q: False,
        "get_fast_reply": fake_fast_reply,
        "_append_chat_history": lambda *args, **kwargs: None,
        "_log_route_observability": lambda *args, **kwargs: None,
        "track_output_quality": lambda *args, **kwargs: None,
    }
    with _patch_attrs(server, patches):
        req = _FakeRequest("token-chat")
        general_obj = asyncio.run(server.chat(req, server.ChatRequest(query="你好")))
        general_status, general_data = _as_status_data(general_obj)
        fortune_obj = asyncio.run(server.chat(req, server.ChatRequest(query="帮我看运势")))
        fortune_status, fortune_data = _as_status_data(fortune_obj)

    assert general_status == 200, general_data
    assert "普通闲聊可用" in str(general_data.get("output") or ""), general_data
    assert not bool(((general_data.get("extra") or {}).get("profile_required"))), general_data
    assert fortune_status == 200, fortune_data
    assert bool(((fortune_data.get("extra") or {}).get("profile_required"))), fortune_data
    return {
        "general_chat": general_data,
        "fortune_chat": fortune_data,
    }


def case_media_missing_prompt_mentions_gender() -> dict[str, Any]:
    msg = server.build_media_missing_reply(["gender"])
    assert "性别（男/女）" in msg, msg
    return {"message": msg}


def case_history_extract_keeps_gender_after_birthtime() -> dict[str, Any]:
    history = SimpleNamespace(
        messages=[
            SimpleNamespace(type="human", content="我叫林雨"),
            SimpleNamespace(type="human", content="我的生日是2001-08-15"),
            SimpleNamespace(type="human", content="出生时间10:30"),
            SimpleNamespace(type="human", content="我是男生"),
        ]
    )
    profile = server.extract_profile_from_history(history)
    assert str(profile.get("name") or "") == "林雨", profile
    assert str(profile.get("birthdate") or "") == "2001-08-15", profile
    assert str(profile.get("birthtime") or "") == "10:30", profile
    assert str(profile.get("gender") or "") == "male", profile
    return {"profile": profile}


def case_concurrent_partial_updates_preserve_fields() -> dict[str, Any]:
    phone = profile_concurrency._make_phone()
    user = profile_concurrency._ensure_test_user(phone)
    user_id = int(user.get("id") or 0)
    user_uuid = str(user.get("uuid") or "")
    detail = profile_concurrency.case_partial_updates_preserved(user_uuid, user_id, iterations=2)
    return {"phone": phone, **detail}


def case_concurrent_confidence_rule_preserved() -> dict[str, Any]:
    phone = profile_concurrency._make_phone()
    user = profile_concurrency._ensure_test_user(phone)
    user_id = int(user.get("id") or 0)
    user_uuid = str(user.get("uuid") or "")
    detail = profile_concurrency.case_confidence_rule_stable(user_uuid, user_id, iterations=2)
    return {"phone": phone, **detail}


def case_media_provider_degraded_response_stable() -> dict[str, Any]:
    return provider_regression.case_media_provider_limit_fail_fast()


def case_media_breaker_open_chat_stable() -> dict[str, Any]:
    return provider_regression.case_media_breaker_open_chat_reply()


def _run_case(case_id: str, title: str, fn) -> dict[str, Any]:
    try:
        detail = fn()
        return {"id": case_id, "title": title, "ok": True, "detail": detail}
    except AssertionError as e:
        return {"id": case_id, "title": title, "ok": False, "error": f"AssertionError: {e}"}
    except Exception as e:
        return {"id": case_id, "title": title, "ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="profile/media 回归测试（覆盖鉴权绑定与资料校验）")
    parser.add_argument("--out", default="", help="可选：将结果写入 JSON 文件")
    args = parser.parse_args()

    cases = [
        ("REG-001", "媒体任务会话绑定当前登录用户", case_media_session_bound_to_auth_user),
        ("REG-002", "媒体资料校验仅在需要资料的场景触发", case_media_profile_check_only_for_required_scenarios),
        ("REG-003", "聊天资料拦截只作用于命理路径", case_chat_profile_gate_only_for_fortune_paths),
        ("REG-004", "媒体缺失资料提示包含性别", case_media_missing_prompt_mentions_gender),
        ("REG-005", "历史资料提取不会漏掉后续性别", case_history_extract_keeps_gender_after_birthtime),
        ("REG-006", "并发 partial update 不丢字段", case_concurrent_partial_updates_preserve_fields),
        ("REG-007", "并发条件下昵称置信度规则稳定", case_concurrent_confidence_rule_preserved),
        ("REG-PROV-001", "media provider degraded 返回稳定错误结构", case_media_provider_degraded_response_stable),
        ("REG-PROV-002", "media breaker open 时 chat 返回稳定失败提示", case_media_breaker_open_chat_stable),
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
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
