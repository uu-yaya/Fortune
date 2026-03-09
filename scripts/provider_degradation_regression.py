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
import mytools
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
        "gender": "",
        "partner_gender_preference": "unknown",
        "name_confidence": "none",
        "preferred_name_confidence": "none",
    }


def _complete_profile() -> dict[str, str]:
    profile = _blank_profile()
    profile.update({"name": "时窗测试", "birthdate": "2001-01-01", "gender": "女"})
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


def _fortune_success_payload(source: str, topic: str = "daily") -> dict[str, Any]:
    return {
        "topic": topic,
        "strength": "balanced",
        "fortune_signals": {
            "love": "关系节奏宜稳。",
            "wealth": "财务动作适合先收后放。",
            "career": "先推进最核心的一件事。",
        },
        "risk_points": ["避免同时开太多线。"],
        "opportunity_points": ["把最重要的一步提前。"],
        "time_hints": ["这段时间先稳后发。"],
        "evidence_lines": ["当前盘面更强调稳节奏。"],
        "advice": ["先做一件最重要的小事。", "把待办压到 3 项以内。"],
        "confidence": 0.72,
        "source": source,
        "provider_id": source,
        "provider_calls": 1,
        "quota_state": "healthy",
        "error": None,
    }


def _full_flags() -> dict[str, bool]:
    return dict(server.FEATURE_FLAG_DEFAULTS)


def case_general_profile_context_trimmed() -> dict[str, Any]:
    profile = {
        "name": "测试甲",
        "preferred_name": "周周",
        "birthdate": "2002-03-14",
        "birthtime": "07:15",
    }
    general_context = server.build_profile_context(
        profile,
        domain_intent="general",
        question_type="default",
        user_query="我最近焦虑，怎么调节睡眠？",
    )
    fortune_context = server.build_profile_context(
        profile,
        domain_intent="fortune",
        question_type="trend",
        user_query="分析一下我今年的运势",
    )
    assert f"出生日期：{profile['birthdate']}" not in general_context, general_context
    assert f"出生时间：{profile['birthtime']}" not in general_context, general_context
    assert "不要根据出生日期" in general_context, general_context
    assert "出生日期" in fortune_context and "出生时间" in fortune_context, fortune_context
    return {"general_context": general_context, "fortune_context": fortune_context}


def case_general_topic_shift_note() -> dict[str, Any]:
    class _Msg:
        def __init__(self, type_: str, content: str):
            self.type = type_
            self.content = content

    class _History:
        def __init__(self, messages):
            self.messages = messages

    shifted = _History([
        _Msg("human", "我今天适合吃什么"),
        _Msg("ai", "你今天想吃暖暖的还是清爽的呀？"),
    ])
    note = server.build_general_topic_shift_note("我最近工作压力很大，怎么缓一缓", shifted)
    assert "明显换了话题" in note, note
    assert "上一轮用户问题是：我今天适合吃什么" in note, note
    assert "旧话题只当弱参考" in note, note

    continued = _History([
        _Msg("human", "我今天适合吃什么"),
        _Msg("ai", "你今天想吃暖暖的还是清爽的呀？"),
    ])
    same_topic_note = server.build_general_topic_shift_note("我想吃点清爽的，有什么推荐", continued)
    assert same_topic_note == "", same_topic_note
    return {"shift_note": note}


def case_dream_keyword_cleanup() -> dict[str, Any]:
    class _FakeLLM:
        def __init__(self, outputs: list[str]):
            self.outputs = list(outputs)

        def invoke(self, _prompt):
            if not self.outputs:
                return ""
            return self.outputs.pop(0)

    direct = mytools._extract_dream_keyword_local("梦见蛇是什么意思")
    with_parents = mytools._extract_dream_keyword("给我解梦，我梦到了爸爸妈妈", llm=_FakeLLM(["父母"]))
    with_leading_and = mytools._extract_dream_keyword("我梦到了和檀健次进行人类交配活动，给我解梦", llm=_FakeLLM(["性爱"]))
    last_night = mytools._extract_dream_keyword("昨晚梦到了前男友结婚", llm=_FakeLLM(["结婚"]))
    amusement_park = mytools._extract_dream_keyword("我梦到了去游乐园，给我解梦", llm=_FakeLLM(["游乐园"]))
    fallback_local = mytools._extract_dream_keyword("梦见蛇是什么意思", llm=_FakeLLM([""]))
    normalized_label = mytools._normalize_zhougong_keyword("关键词：蛇", fallback_query="梦见蛇是什么意思")
    normalized_aimessage = mytools._normalize_zhougong_keyword(
        "content='蛇' additional_kwargs={} response_metadata={}",
        fallback_query="梦见蛇是什么意思",
    )
    assert direct == "蛇", direct
    assert with_parents == "父母", with_parents
    assert with_leading_and == "性爱", with_leading_and
    assert last_night == "结婚", last_night
    assert amusement_park == "游乐园", amusement_park
    assert fallback_local == "蛇", fallback_local
    assert normalized_label == "蛇", normalized_label
    assert normalized_aimessage == "蛇", normalized_aimessage
    return {
        "direct": direct,
        "with_parents": with_parents,
        "with_leading_and": with_leading_and,
        "last_night": last_night,
        "amusement_park": amusement_park,
        "fallback_local": fallback_local,
        "normalized_label": normalized_label,
        "normalized_aimessage": normalized_aimessage,
    }


def case_profile_seed_reply() -> dict[str, Any]:
    extracted = {"name": "测试甲", "birthdate": "2002-03-14"}
    assert server._is_profile_seed_only_query("我叫测试甲，2002-03-14出生，我是女生。", extracted), extracted
    assert not server._is_profile_seed_only_query("我叫测试甲，2002-03-14出生，我是女生。帮我看今天运势", extracted), extracted
    reply = server._build_profile_seed_reply(
        {"name": "测试甲", "birthdate": "2002-03-14", "birthtime": "", "preferred_name": ""},
        extracted,
    )
    assert "我先帮你记住啦" in reply, reply
    assert "星座、生肖和一般趋势已经够用了" in reply, reply
    assert "希望我怎么称呼你" not in reply and "具体时间" not in reply, reply
    return {"reply": reply}


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
        "get_v2_flags": _full_flags,
        "apply_v2_flag_policy": lambda _raw: (_full_flags(), "none"),
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
        "route_dream_pipeline": lambda _q: (None, None),
        "route_zodiac_pipeline": lambda _q, allow_clarify=False, flags=None, profile=None: (None, None),
        "route_fortune_pipeline": fake_route,
        "_append_chat_history": lambda *args, **kwargs: None,
        "_log_route_observability": lambda *args, **kwargs: None,
        "track_output_quality": lambda *args, **kwargs: None,
    }
    with _patch_attrs(server, patches):
        resp_obj = asyncio.run(server.chat(_FakeRequest("token-fortune"), server.ChatRequest(query="我今天整体运势最该注意什么？")))
        status, data = _as_status_data(resp_obj)
    assert status == 200, data
    output = str(data.get("output") or "")
    assert "FORTUNE_TIMEOUT" in output and "稳妥方向" in output, output
    return {"status_code": status, "output_preview": output[:120]}


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
    assert "FORTUNE_PARSE_FAILED" in text and "稳妥方向" in text, text
    return {"output_preview": text[:160]}


def case_bazi_daily_route_hit() -> dict[str, Any]:
    called = {"daily": 0, "fallback": 0}

    def fake_daily(*args, **kwargs):
        called["daily"] += 1
        return _fortune_success_payload("yuanfenju_bazi_yunshi", topic="daily")

    def fail_if_fallback(*args, **kwargs):
        called["fallback"] += 1
        raise AssertionError("run_yuanfenju_bazi_cesuan should not be used for daily provider hit")

    with _patch_attrs(server, {"run_yuanfenju_bazi_daily": fake_daily, "run_yuanfenju_bazi_cesuan": fail_if_fallback}):
        reply, payload = server.route_fortune_pipeline(
            "今天运势如何",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="trend",
            session_id="daily-hit",
        )
    assert reply and isinstance(payload, dict), (reply, payload)
    assert str(payload.get("provider_id") or "") == "yuanfenju_bazi_yunshi", payload
    assert str(payload.get("route_reason_code") or "") == "bazi_daily_hit", payload
    return {"calls": called, "provider_id": payload.get("provider_id")}


def case_bazi_future_route_hit() -> dict[str, Any]:
    called = {"future": 0, "fallback": 0}

    def fake_future(*args, **kwargs):
        called["future"] += 1
        assert int(kwargs.get("yunshi_year") or 0) == datetime.now().year + 1, kwargs
        return _fortune_success_payload("yuanfenju_bazi_weilai", topic="daily")

    def fail_if_fallback(*args, **kwargs):
        called["fallback"] += 1
        raise AssertionError("run_yuanfenju_bazi_cesuan should not be used for future provider hit")

    with _patch_attrs(server, {"run_yuanfenju_bazi_future": fake_future, "run_yuanfenju_bazi_cesuan": fail_if_fallback}):
        reply, payload = server.route_fortune_pipeline(
            "明年运势",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="trend",
            session_id="future-hit",
        )
    assert reply and isinstance(payload, dict), (reply, payload)
    assert str(payload.get("provider_id") or "") == "yuanfenju_bazi_weilai", payload
    assert str(payload.get("route_reason_code") or "") == "bazi_future_hit", payload
    return {"calls": called, "provider_id": payload.get("provider_id")}


def case_wealth_compare_route_hit() -> dict[str, Any]:
    called_years: list[int] = []

    def fake_wealth(*args, **kwargs):
        year = int(kwargs.get("liu_year") or 0)
        called_years.append(year)
        payload = _fortune_success_payload("yuanfenju_caiyunfenxi", topic="wealth")
        payload["fortune_signals"]["wealth"] = f"{year}年财运先稳后发。"
        payload["time_hints"] = [f"{year}年上半年先保守，下半年再发力。"]
        return payload

    def fail_if_fallback(*args, **kwargs):
        raise AssertionError("run_yuanfenju_bazi_cesuan should not be used for wealth compare hit")

    with _patch_attrs(server, {"run_yuanfenju_wealth_year": fake_wealth, "run_yuanfenju_bazi_cesuan": fail_if_fallback}):
        reply, payload = server.route_fortune_pipeline(
            "今年和明年财运对比",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="trend",
            session_id="wealth-compare-hit",
        )
    assert reply and isinstance(payload, dict), (reply, payload)
    assert payload.get("provider_id") == "yuanfenju_caiyunfenxi_compare", payload
    assert payload.get("question_type") == "comparison", payload
    assert payload.get("route_reason_code") == "wealth_year_compare_hit", payload
    assert len(called_years) == 2, called_years
    return {"called_years": called_years}


def case_wealth_profile_route_hit() -> dict[str, Any]:
    called = {"profile": 0, "fallback": 0}

    def fake_wealth_profile(*args, **kwargs):
        called["profile"] += 1
        payload = _fortune_success_payload("yuanfenju_yuce_caiyun", topic="wealth")
        payload["fortune_signals"]["wealth"] = "这段财运更适合先守住节奏，再慢慢放大动作。"
        payload["opportunity_points"] = ["先守住现金流，再挑一个最稳的开源点往前推。"]
        return payload

    def fail_if_fallback(*args, **kwargs):
        called["fallback"] += 1
        raise AssertionError("run_yuanfenju_bazi_cesuan should not be used for general wealth provider hit")

    with _patch_attrs(server, {"run_yuanfenju_wealth_profile": fake_wealth_profile, "run_yuanfenju_bazi_cesuan": fail_if_fallback}):
        reply, payload = server.route_fortune_pipeline(
            "我最近财运怎么样",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="trend",
            session_id="wealth-profile-hit",
        )
    assert reply and isinstance(payload, dict), (reply, payload)
    assert payload.get("provider_id") == "yuanfenju_yuce_caiyun", payload
    assert payload.get("route_reason_code") == "wealth_profile_hit", payload
    return {"calls": called, "provider_id": payload.get("provider_id")}


def case_zodiac_provider_hit() -> dict[str, Any]:
    def fake_zodiac(**kwargs):
        assert kwargs.get("entity_type") == 0, kwargs
        assert kwargs.get("scope_key") == "本周运势", kwargs
        return {
            "ok": True,
            "text": "呀哈～白羊座这周更适合先稳节奏，再把重点任务往前提。",
            "provider_id": "yuanfenju_zhanbu_yunshi",
            "provider_calls": 1,
            "quota_state": "healthy",
        }

    with _patch_attrs(server, {"run_yuanfenju_zodiac_yunshi": fake_zodiac}):
        reply, meta = server.route_zodiac_pipeline("白羊座本周运势", flags=_full_flags())
    assert "白羊座" in str(reply or ""), reply
    assert str((meta or {}).get("source") or "") == "yuanfenju_zhanbu_yunshi", meta
    return {"reply": str(reply or "")[:80], "meta": meta}


def case_zodiac_inferred_from_birthdate_hit() -> dict[str, Any]:
    def fake_zodiac(**kwargs):
        assert kwargs.get("entity_type") == 0, kwargs
        assert kwargs.get("label") == "双鱼座", kwargs
        assert kwargs.get("scope_key") == "本周运势", kwargs
        return {
            "ok": True,
            "text": "呀哈～双鱼座这周适合把注意力收回到最关键的目标上。",
            "provider_id": "yuanfenju_zhanbu_yunshi",
            "provider_calls": 1,
            "quota_state": "healthy",
        }

    profile = _blank_profile()
    profile.update({"birthdate": "2002-03-14"})
    with _patch_attrs(server, {"run_yuanfenju_zodiac_yunshi": fake_zodiac}):
        reply, meta = server.route_zodiac_pipeline("帮我看星座运势", allow_clarify=True, flags=_full_flags(), profile=profile)
    assert "双鱼座" in str(reply or ""), reply
    assert bool((meta or {}).get("inferred_from_profile")) is True, meta
    assert str((meta or {}).get("source") or "") == "yuanfenju_zhanbu_yunshi", meta
    return {"reply": str(reply or "")[:80], "meta": meta}


def case_shengxiao_provider_hit() -> dict[str, Any]:
    def fake_zodiac(**kwargs):
        assert kwargs.get("entity_type") == 1, kwargs
        assert kwargs.get("scope_key") == "今日运势", kwargs
        return {
            "ok": True,
            "text": "呀哈～属龙的你今天适合先把最重要的那件事推进一步。",
            "provider_id": "yuanfenju_zhanbu_yunshi",
            "provider_calls": 1,
            "quota_state": "healthy",
        }

    with _patch_attrs(server, {"run_yuanfenju_zodiac_yunshi": fake_zodiac}):
        reply, meta = server.route_zodiac_pipeline("属龙今日运势", flags=_full_flags())
    assert "属龙" in str(reply or ""), reply
    assert str((meta or {}).get("source") or "") == "yuanfenju_zhanbu_yunshi", meta
    return {"reply": str(reply or "")[:80], "meta": meta}


def case_shengxiao_inferred_from_birthdate_hit() -> dict[str, Any]:
    def fake_zodiac(**kwargs):
        assert kwargs.get("entity_type") == 1, kwargs
        assert kwargs.get("label") == "属马", kwargs
        assert kwargs.get("scope_key") == "本周运势", kwargs
        return {
            "ok": True,
            "text": "呀哈～属马的你这周适合先把最关键的一步落地。",
            "provider_id": "yuanfenju_zhanbu_yunshi",
            "provider_calls": 1,
            "quota_state": "healthy",
        }

    profile = _blank_profile()
    profile.update({"birthdate": "2002-03-14"})
    with _patch_attrs(server, {"run_yuanfenju_zodiac_yunshi": fake_zodiac}):
        reply, meta = server.route_zodiac_pipeline("帮我看生肖运势", allow_clarify=True, flags=_full_flags(), profile=profile)
    assert "属马" in str(reply or ""), reply
    assert bool((meta or {}).get("inferred_from_profile")) is True, meta
    assert str((meta or {}).get("source") or "") == "yuanfenju_zhanbu_yunshi", meta
    return {"reply": str(reply or "")[:80], "meta": meta}


def case_zodiac_clarify_still_hits() -> dict[str, Any]:
    reply, meta = server.route_zodiac_pipeline("帮我看星座运势", allow_clarify=True, flags=_full_flags())
    assert "出生年月日" in str(reply or ""), reply
    assert str((meta or {}).get("source") or "") == "zodiac_clarify", meta
    return {"reply": reply, "meta": meta}


def case_zeshi_route_hit() -> dict[str, Any]:
    called = {"zeshi": 0}

    def fake_zeshi(**kwargs):
        called["zeshi"] += 1
        assert int(kwargs.get("incident") or -1) == 4, kwargs
        assert int(kwargs.get("future") or -1) == 1, kwargs
        assert str(kwargs.get("window_start") or "") and str(kwargs.get("window_end") or ""), kwargs
        return {
            "ok": True,
            "text": "呀哈～关于“领证”，接下来这几天里 3月12日 和 3月14日 更顺一点。",
            "provider_id": "yuanfenju_gongju_zeshi",
            "provider_calls": 1,
            "quota_state": "healthy",
        }

    with _patch_attrs(server, {"run_yuanfenju_zeshi": fake_zeshi}):
        reply, meta = server.route_fortune_pipeline(
            "下周哪天适合领证",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="colloquial",
            session_id="zeshi-hit",
        )
    assert "领证" in str(reply or ""), reply
    assert str((meta or {}).get("source") or "") == "yuanfenju_gongju_zeshi", meta
    assert int((meta or {}).get("zeshi_future_code") or -1) == 1, meta
    return {"calls": called, "meta": meta}


def case_zeshi_abstract_not_hit() -> dict[str, Any]:
    called = {"zeshi": 0, "fallback": 0}

    def fail_zeshi(**kwargs):
        called["zeshi"] += 1
        raise AssertionError("run_yuanfenju_zeshi should not be called for abstract window queries")

    def fake_fallback(*args, **kwargs):
        called["fallback"] += 1
        return _fortune_success_payload("yuanfenju_bazi_cesuan", topic="daily")

    with _patch_attrs(server, {"run_yuanfenju_zeshi": fail_zeshi, "run_yuanfenju_bazi_cesuan": fake_fallback}):
        reply, payload = server.route_fortune_pipeline(
            "近哪几天气场更顺",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="colloquial",
            session_id="zeshi-abstract",
        )
    assert reply and isinstance(payload, dict), (reply, payload)
    assert payload.get("provider_id") == "yuanfenju_bazi_cesuan", payload
    assert called["zeshi"] == 0, called
    return {"calls": called, "provider_id": payload.get("provider_id")}


def case_love_profile_routes() -> dict[str, Any]:
    called: list[str] = []

    def fake_love(*args, **kwargs):
        variant = str(kwargs.get("variant") or "")
        called.append(variant)
        if variant == "zhengyuan":
            source = "yuanfenju_zhengyuan"
        elif variant == "jiehun":
            source = "yuanfenju_jiehun"
        else:
            source = "yuanfenju_yinyuan"
        return _fortune_success_payload(source, topic="love")

    with _patch_attrs(server, {"run_yuanfenju_love_profile": fake_love}):
        _, payload_a = server.route_fortune_pipeline(
            "我的姻缘趋势",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="trend",
            session_id="love-yinyuan",
        )
        _, payload_b = server.route_fortune_pipeline(
            "我的正缘画像是什么样",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="default",
            session_id="love-zhengyuan",
        )
        _, payload_b2 = server.route_fortune_pipeline(
            "我的正缘",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="default",
            session_id="love-zhengyuan-plain",
        )
        _, payload_c = server.route_fortune_pipeline(
            "我什么时候适合结婚",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=_full_flags(),
            question_type="default",
            session_id="love-jiehun",
        )
    assert called == ["yinyuan", "zhengyuan", "zhengyuan", "jiehun"], called
    assert str((payload_a or {}).get("provider_id") or "") == "yuanfenju_yinyuan", payload_a
    assert str((payload_b or {}).get("provider_id") or "") == "yuanfenju_zhengyuan", payload_b
    assert str((payload_b2 or {}).get("provider_id") or "") == "yuanfenju_zhengyuan", payload_b2
    assert str((payload_c or {}).get("provider_id") or "") == "yuanfenju_jiehun", payload_c
    return {"called": called}


def case_provider_flag_off_fallback() -> dict[str, Any]:
    called = {"daily": 0, "fallback": 0}

    def fail_daily(*args, **kwargs):
        called["daily"] += 1
        raise AssertionError("run_yuanfenju_bazi_daily should not be called when flag is off")

    def fake_fallback(*args, **kwargs):
        called["fallback"] += 1
        return _fortune_success_payload("yuanfenju_bazi_cesuan", topic="daily")

    flags = _full_flags()
    flags["bazi_daily_v1"] = False
    with _patch_attrs(server, {"run_yuanfenju_bazi_daily": fail_daily, "run_yuanfenju_bazi_cesuan": fake_fallback}):
        _, payload = server.route_fortune_pipeline(
            "今天运势如何",
            _complete_profile(),
            time_anchor=server.build_time_anchor(),
            flags=flags,
            question_type="trend",
            session_id="flag-off",
        )
    assert payload.get("provider_id") == "yuanfenju_bazi_cesuan", payload
    assert called["daily"] == 0 and called["fallback"] == 1, called
    return {"calls": called}


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
        "get_v2_flags": _full_flags,
        "apply_v2_flag_policy": lambda _raw: (_full_flags(), "none"),
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
        ("PROV-004", "普通问答上下文不再注入命理资料", case_general_profile_context_trimmed),
        ("PROV-004A", "general 换话题时会降权旧历史", case_general_topic_shift_note),
        ("PROV-009", "今日运势命中 Bazi/yunshi", case_bazi_daily_route_hit),
        ("PROV-010", "年度运势命中 Bazi/weilai", case_bazi_future_route_hit),
        ("PROV-011", "财运对比命中多次 caiyunfenxi", case_wealth_compare_route_hit),
        ("PROV-011A", "通用财运命中 Yuce/caiyun", case_wealth_profile_route_hit),
        ("PROV-012", "星座运势命中 Zodiac provider", case_zodiac_provider_hit),
        ("PROV-013", "生肖运势命中 Zodiac provider", case_shengxiao_provider_hit),
        ("PROV-014", "有出生日期时可直接推断星座", case_zodiac_inferred_from_birthdate_hit),
        ("PROV-015", "有出生日期时可直接推断生肖", case_shengxiao_inferred_from_birthdate_hit),
        ("PROV-016", "缺出生信息时仍先澄清", case_zodiac_clarify_still_hits),
        ("PROV-017", "具体事项命中 zeshi", case_zeshi_route_hit),
        ("PROV-018", "抽象时间窗不误用 zeshi", case_zeshi_abstract_not_hit),
        ("PROV-019", "姻缘与正缘专题命中 love providers", case_love_profile_routes),
        ("PROV-020", "provider flag 关闭时回退旧链路", case_provider_flag_off_fallback),
        ("PROV-021", "解梦关键词提取会清洗成接口可用参数", case_dream_keyword_cleanup),
        ("PROV-022", "资料 seed 走确定性确认回复", case_profile_seed_reply),
        ("PROV-005", "媒体 poll 5xx 重试后可恢复成功", case_media_poll_5xx_retry_then_success),
        ("PROV-006", "媒体 auth failure 打开 breaker", case_media_auth_failure_opens_breaker),
        ("PROV-007", "连续 timeout/http_5xx 打开 breaker", case_breaker_opens_after_three_timeouts),
        ("PROV-008", "breaker open 后不再请求上游", case_breaker_open_skips_real_upstream_call),
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
