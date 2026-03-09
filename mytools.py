from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.tools import tool
from langchain_qdrant import Qdrant
from qdrant_client import QdrantClient
from langchain_core.output_parsers import JsonOutputParser
import requests
import json
import time
import re
import hashlib
from datetime import datetime
from loguru import logger
from models import get_lc_ali_embeddings, get_lc_ali_model_client
import os
from pydantic import BaseModel, Field, ValidationError
import redis

from config import SERPAPI_API_KEY, VECTOR_COLLECTION_NAME, VECTOR_DB_PATH, YUANFENJU_API_KEY, REDIS_URL
from provider_runtime import (
    build_provider_failure,
    provider_record_failure,
    provider_record_success,
    provider_should_short_circuit,
)

if SERPAPI_API_KEY:
    os.environ["SERPAPI_API_KEY"] = SERPAPI_API_KEY


class WuxingScores(BaseModel):
    metal: int = 0
    wood: int = 0
    water: int = 0
    fire: int = 0
    earth: int = 0


class FortuneSignals(BaseModel):
    love: str = ""
    wealth: str = ""
    career: str = ""


class FortuneError(BaseModel):
    code: str = ""
    message: str = ""
    provider: str = ""
    provider_code: str = ""
    category: str = ""
    degraded: bool = False


YUANFENJU_PROVIDER_LIMIT_PATTERN = re.compile(r"(quota|余额不足|剩余可调用次数不足|insufficient|limit)", re.IGNORECASE)
YUANFENJU_PROVIDER_AUTH_PATTERN = re.compile(r"(api[_ -]?key|密钥|无效|非法|不存在|未授权|权限|auth|401|403)", re.IGNORECASE)
YUANFENJU_PROVIDER_EXPIRE_PATTERN = re.compile(r"(过期|到期|expired)", re.IGNORECASE)
YUANFENJU_API_BASE = "https://api.yuanfenju.com/index.php/v1"
YUANFENJU_DEFAULT_LANG = "zh-cn"
YUANFENJU_MERCHANT_PROBE_TTL_SECONDS = 300
YUANFENJU_DEFAULT_TIMEOUT_SECONDS = 3
YUANFENJU_DEFAULT_RETRIES = 2
_REDIS_CLIENT = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class BaziToolOutput(BaseModel):
    topic: str = "daily"
    bazi: str = ""
    day_master: str = ""
    strength: str = "balanced"
    xiyongshen: str = ""
    jishen: str = ""
    wuxing_scores: WuxingScores = Field(default_factory=WuxingScores)
    fortune_signals: FortuneSignals = Field(default_factory=FortuneSignals)
    risk_points: list[str] = Field(default_factory=list)
    opportunity_points: list[str] = Field(default_factory=list)
    time_hints: list[str] = Field(default_factory=list)
    evidence_lines: list[str] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    source: str = "yuanfenju"
    error: FortuneError | None = None


def _infer_topic(query: str) -> str:
    q = str(query or "")
    if any(k in q for k in ["桃花", "姻缘", "感情", "恋爱"]):
        return "love"
    if any(k in q for k in ["财运", "财富", "收入", "金钱"]):
        return "wealth"
    if any(k in q for k in ["事业", "工作", "职场", "升职"]):
        return "career"
    if any(k in q for k in ["学业", "考试", "学习"]):
        return "study"
    return "daily"


def _to_int(value) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _normalize_strength(raw: str) -> str:
    text = str(raw or "")
    if "强" in text:
        return "strong"
    if "弱" in text:
        return "weak"
    return "balanced"


def _build_advice(topic: str, strength: str) -> list[str]:
    base = {
        "daily": [
            "今天先做一件最重要的小事，连续投入25分钟。",
            "把待办减到3项以内，先完成再扩展。",
            "晚上用3分钟复盘：什么最顺、什么该收敛。",
        ],
        "love": [
            "今天主动发出一次轻量关心，不求长聊，只求真诚。",
            "表达需求时用'我感受'句式，减少猜测和拉扯。",
            "关系不确定时先稳节奏，48小时内不做冲动决定。",
        ],
        "wealth": [
            "今天只做一项与收入直接相关的动作。",
            "先记账再消费，避免情绪性花销。",
            "对高风险决策设置24小时冷静期。",
        ],
        "career": [
            "优先推进一个可量化产出点，别同时开太多线。",
            "把关键结果写成3句汇报，提高被看见概率。",
            "遇到卡点先找一位能给反馈的人快速对齐。",
        ],
        "study": [
            "先完成一段25分钟专注学习，再休息5分钟。",
            "先攻克最难的一题或一节，建立正反馈。",
            "睡前做一次10分钟回顾，巩固当天关键点。",
        ],
    }
    advice = list(base.get(topic, base["daily"]))
    if strength == "strong":
        advice[0] = "状态可用，今天把最关键任务前置完成。"
    elif strength == "weak":
        advice[0] = "先稳住节奏，今天只设一个最小可完成目标。"
    return advice


def _to_lines(raw, limit: int = 4, max_len: int = 80) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[。\n；;]", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = str(part or "").strip(" ，,。；;")
        if not clean:
            continue
        key = clean.replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:max_len])
        if len(out) >= limit:
            break
    return out


def _empty_bazi_output(
    topic: str,
    code: str,
    message: str,
    *,
    provider: str = "",
    provider_code: str = "",
    category: str = "",
    degraded: bool = False,
) -> dict:
    model = BaziToolOutput(
        topic=topic,
        advice=_build_advice(topic, "balanced"),
        confidence=0.2,
        error=FortuneError(
            code=code,
            message=message,
            provider=provider,
            provider_code=provider_code,
            category=category,
            degraded=degraded,
        ),
    )
    return model.model_dump()


def _fortune_failure_output(topic: str, code: str, message: str, failure: dict | None = None) -> str:
    failure = failure or {}
    return json.dumps(
        _empty_bazi_output(
            topic,
            code,
            message,
            provider=str(failure.get("provider") or ""),
            provider_code=str(failure.get("error_code") or ""),
            category=str(failure.get("category") or ""),
            degraded=bool(failure),
        ),
        ensure_ascii=False,
    )


def _attach_provider_meta(payload: dict, **meta) -> dict:
    out = dict(payload or {})
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        out[key] = value
    return out


def _parse_birthdate(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if not text:
        return 0, 0, 0
    m = re.search(r"(?P<y>\d{4})[-/年](?P<m>\d{1,2})[-/月](?P<d>\d{1,2})", text)
    if not m:
        return 0, 0, 0
    return _to_int(m.group("y")), _to_int(m.group("m")), _to_int(m.group("d"))


def _parse_birthtime(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    if not text:
        return 0, 0
    m = re.search(r"(?P<h>\d{1,2})(?::|点|时)(?P<m>\d{1,2})?", text)
    if m:
        return _to_int(m.group("h")), _to_int(m.group("m") or 0)
    if text.isdigit():
        return _to_int(text), 0
    return 0, 0


def _infer_sex_from_text(profile: dict | None, query: str) -> int | None:
    profile = profile or {}
    gender = str(profile.get("gender") or "").strip().lower()
    q = str(query or "")
    if gender in {"male", "man", "boy", "m", "0", "男", "男生"}:
        return 0
    if gender in {"female", "woman", "girl", "f", "1", "女", "女生"}:
        return 1
    if re.search(r"(我是男|男生|男的|男性)", q):
        return 0
    if re.search(r"(我是女|女生|女的|女性)", q):
        return 1
    return None


def _build_profile_hint(query: str, profile: dict | None = None) -> str:
    profile = profile or {}
    parts = []
    if str(profile.get("name") or "").strip():
        parts.append(f"姓名：{str(profile.get('name')).strip()}")
    if str(profile.get("birthdate") or "").strip():
        parts.append(f"出生日期：{str(profile.get('birthdate')).strip()}")
    if str(profile.get("birthtime") or "").strip():
        parts.append(f"出生时间：{str(profile.get('birthtime')).strip()}")
    if str(profile.get("gender") or "").strip():
        parts.append(f"性别：{str(profile.get('gender')).strip()}")
    prefix = "；".join(parts)
    if prefix:
        return f"已知资料：{prefix}；用户问题：{str(query or '').strip()}"
    return str(query or "").strip()


def _extract_yuanfenju_birth_params(query: str, profile: dict | None = None) -> dict:
    profile = profile or {}
    params: dict[str, int | str] = {
        "api_key": str(YUANFENJU_API_KEY or "").strip(),
        "type": 1,
    }
    name = str(profile.get("name") or "").strip()
    if name:
        params["name"] = name
    year, month, day = _parse_birthdate(str(profile.get("birthdate") or ""))
    if year and month and day:
        params["year"] = year
        params["month"] = month
        params["day"] = day
    hour, minute = _parse_birthtime(str(profile.get("birthtime") or ""))
    params["hours"] = hour
    params["minute"] = minute
    sex_val = _infer_sex_from_text(profile, query)
    if sex_val is not None:
        params["sex"] = sex_val

    missing = [field for field in ("name", "sex", "year", "month", "day") if field not in params]
    if not missing:
        return {"ok": True, "params": params}

    prompt = ChatPromptTemplate.from_template(
        """你是一个参数查询助手，根据用户输入内容找出相关的参数并按json格式返回。
JSON字段如下：
- "api_key":"{api_key}",
- "name":"姓名",
- "sex":"性别，0表示男，1表示女，如果用户输入内容中未提供，则根据姓名判断",
- "type":"日历类型，0农历，1公历，默认1",
- "year":"出生年份 例：1998",
- "month":"出生月份 例：8",
- "day":"出生日期 例：8",
- "hours":"出生小时 例：14",
- "minute":"出生分钟，未知传0"
如果没有找到相关参数，则需要提醒用户告诉你这些内容，只返回JSON，不要有其他评论。用户输入：{query}"""
    )
    parser = JsonOutputParser()
    prompt = prompt.partial(format_instructions=parser.get_format_instructions())
    enriched_query = _build_profile_hint(query, profile=profile)
    try:
        chain = prompt | get_lc_ali_model_client(streaming=False) | parser
        extracted = chain.invoke({"query": enriched_query, "api_key": YUANFENJU_API_KEY})
    except Exception as e:
        logger.error(f"缘分居参数抽取失败: {e}")
        return {"ok": False, "message": "参数抽取失败，请补充姓名和出生年月日时"}

    logger.info(f"缘分居参数抽取结果: {extracted}")
    if isinstance(extracted, dict):
        for key in ("name", "sex", "type", "year", "month", "day", "hours", "minute"):
            value = extracted.get(key)
            if value in {None, ""}:
                continue
            if key in {"sex", "type", "year", "month", "day", "hours", "minute"}:
                params[key] = _to_int(value)
            else:
                params[key] = str(value).strip()

    missing = [field for field in ("name", "sex", "year", "month", "day") if field not in params or str(params.get(field)).strip() == ""]
    if missing:
        return {"ok": False, "message": "参数抽取失败，请补充姓名和出生年月日时"}
    params["hours"] = _to_int(params.get("hours") or 0)
    params["minute"] = _to_int(params.get("minute") or 0)
    params["type"] = _to_int(params.get("type") or 1) or 1
    return {"ok": True, "params": params}


def _merchant_probe_cache_key(api_key: str) -> str:
    hashed = hashlib.sha1(str(api_key or "").encode("utf-8")).hexdigest()[:16]
    return f"yuanfenju:merchant_probe:{hashed}"


def _load_merchant_probe_cache(api_key: str) -> dict | None:
    if not api_key:
        return None
    try:
        raw = _REDIS_CLIENT.get(_merchant_probe_cache_key(api_key))
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _save_merchant_probe_cache(api_key: str, payload: dict) -> None:
    if not api_key:
        return
    try:
        _REDIS_CLIENT.setex(
            _merchant_probe_cache_key(api_key),
            YUANFENJU_MERCHANT_PROBE_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        return


def _parse_probe_expire_state(expire_at: str) -> bool:
    text = str(expire_at or "").strip()
    if not text or text == "--":
        return False
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return dt < datetime.now()


def yuanfenju_query_merchant(*, force_refresh: bool = False) -> dict:
    api_key = str(YUANFENJU_API_KEY or "").strip()
    if not api_key:
        return {"ok": False, "state": "auth_invalid", "message": "未配置命理服务密钥"}
    if not force_refresh:
        cached = _load_merchant_probe_cache(api_key)
        if cached:
            return cached

    url = f"{YUANFENJU_API_BASE}/Free/querymerchant"
    probe: dict = {
        "ok": False,
        "state": "probe_failed",
        "message": "额度探针暂不可用",
        "merchant_type": "",
        "merchant_expire_time": "",
        "merchant_remaining_call_times": "",
    }
    try:
        response = requests.post(url, data={"api_key": api_key}, timeout=2)
        if response.status_code != 200:
            probe["message"] = f"额度探针响应异常（HTTP {response.status_code}）"
            return probe
        payload = response.json()
    except Exception as e:
        probe["message"] = str(e) or probe["message"]
        return probe

    if int(payload.get("errcode", 1)) != 0:
        msg = str(payload.get("errmsg") or "额度探针请求失败")
        state = "auth_invalid" if YUANFENJU_PROVIDER_AUTH_PATTERN.search(msg) else "probe_failed"
        probe.update({"message": msg, "state": state})
        _save_merchant_probe_cache(api_key, probe)
        return probe

    data = payload.get("data", {}) or {}
    remaining = str(data.get("merchant_remaining_call_times") or "").strip()
    expire_at = str(data.get("merchant_expire_time") or "").strip()
    state = "healthy"
    if remaining not in {"", "--"} and _to_int(remaining) <= 0:
        state = "quota_exhausted"
    elif _parse_probe_expire_state(expire_at):
        state = "member_expired"
    probe = {
        "ok": True,
        "state": state,
        "message": str(payload.get("errmsg") or "请求成功"),
        "merchant_type": str(data.get("merchant_type") or ""),
        "merchant_expire_time": expire_at,
        "merchant_remaining_call_times": remaining,
    }
    _save_merchant_probe_cache(api_key, probe)
    return probe


def yuanfenju_query_times() -> dict:
    api_key = str(YUANFENJU_API_KEY or "").strip()
    if not api_key:
        return {"ok": False, "message": "未配置命理服务密钥"}
    url = f"{YUANFENJU_API_BASE}/Free/querytimes"
    try:
        response = requests.post(url, data={"api_key": api_key}, timeout=2)
        if response.status_code != 200:
            return {"ok": False, "message": f"调用次数探针响应异常（HTTP {response.status_code}）"}
        payload = response.json()
    except Exception as e:
        return {"ok": False, "message": str(e) or "调用次数探针暂不可用"}
    if int(payload.get("errcode", 1)) != 0:
        return {"ok": False, "message": str(payload.get("errmsg") or "调用次数探针请求失败")}
    data = payload.get("data", {}) or {}
    return {
        "ok": True,
        "call_times": _to_int(data.get("call_times") or 0),
        "expire_time": _to_int(data.get("expire_time") or 0),
        "expire_time_message": str(data.get("expire_time_message") or ""),
    }


def _failure_from_probe(failure: dict, probe: dict | None = None) -> tuple[dict, str]:
    probe = probe or {}
    quota_state = str(probe.get("state") or "")
    message = str(failure.get("error_message") or "")
    if quota_state == "quota_exhausted" or YUANFENJU_PROVIDER_LIMIT_PATTERN.search(message):
        return (
            build_provider_failure(
                provider=str(failure.get("provider") or "yuanfenju"),
                operation=str(failure.get("operation") or ""),
                category="quota",
                error_code="YUANFENJU_PROVIDER_LIMIT",
                error_message=message or "命理服务额度已用尽",
                http_status=_to_int(failure.get("http_status") or 0),
                raw_error=str(failure.get("raw_error") or ""),
                retryable=False,
            ),
            "quota_exhausted",
        )
    if quota_state == "member_expired" or YUANFENJU_PROVIDER_EXPIRE_PATTERN.search(message):
        return (
            build_provider_failure(
                provider=str(failure.get("provider") or "yuanfenju"),
                operation=str(failure.get("operation") or ""),
                category="auth",
                error_code="YUANFENJU_MEMBER_EXPIRED",
                error_message=message or "命理服务会员已过期",
                http_status=_to_int(failure.get("http_status") or 0),
                raw_error=str(failure.get("raw_error") or ""),
                retryable=False,
            ),
            "member_expired",
        )
    if quota_state == "auth_invalid" or YUANFENJU_PROVIDER_AUTH_PATTERN.search(message):
        return (
            build_provider_failure(
                provider=str(failure.get("provider") or "yuanfenju"),
                operation=str(failure.get("operation") or ""),
                category="auth",
                error_code="YUANFENJU_AUTH_INVALID",
                error_message=message or "命理服务密钥异常",
                http_status=_to_int(failure.get("http_status") or 0),
                raw_error=str(failure.get("raw_error") or ""),
                retryable=False,
            ),
            "auth_invalid",
        )
    return failure, quota_state or "unknown"


def _outward_failure_code(failure: dict, quota_state: str = "") -> tuple[str, str]:
    state = str(quota_state or "")
    if state == "quota_exhausted":
        return "FORTUNE_UPSTREAM_QUOTA", "命理服务额度已用尽，请稍后再试"
    if state == "member_expired":
        return "FORTUNE_UPSTREAM_EXPIRED", "命理服务会员已过期，请稍后再试"
    if state == "auth_invalid":
        return "FORTUNE_UPSTREAM_AUTH", "命理服务密钥异常，请联系管理员"
    category = str(failure.get("category") or "")
    mapping = {
        "timeout": ("FORTUNE_TIMEOUT", "命理服务超时，请稍后重试"),
        "http_5xx": ("FORTUNE_UPSTREAM_5XX", "命理服务响应异常，请稍后重试"),
        "http_4xx": ("FORTUNE_UPSTREAM_HTTP", "命理服务暂时不可用"),
        "quota": ("FORTUNE_UPSTREAM_QUOTA", "命理服务额度已用尽，请稍后再试"),
        "auth": ("FORTUNE_UPSTREAM_AUTH", "命理服务密钥异常，请联系管理员"),
        "invalid_response": ("FORTUNE_PARSE_FAILED", "命理结果解析失败"),
        "network": ("FORTUNE_UPSTREAM_HTTP", "命理服务暂时不可用"),
        "unknown": ("FORTUNE_UPSTREAM_HTTP", "命理服务暂时不可用"),
    }
    code, default_message = mapping.get(category, ("FORTUNE_UPSTREAM_HTTP", "命理服务暂时不可用"))
    return code, str(failure.get("error_message") or default_message)


def _build_failure_payload(
    topic: str,
    failure: dict,
    *,
    source: str,
    provider_calls: int,
    quota_state: str = "",
    provider_fallback_reason: str = "",
) -> dict:
    code, message = _outward_failure_code(failure, quota_state=quota_state)
    payload = _empty_bazi_output(
        topic,
        code,
        message,
        provider=str(failure.get("provider") or ""),
        provider_code=str(failure.get("error_code") or ""),
        category=str(failure.get("category") or ""),
        degraded=True,
    )
    payload["source"] = source
    return _attach_provider_meta(
        payload,
        provider_id=source,
        provider_calls=provider_calls,
        provider_fallback_reason=provider_fallback_reason or str(failure.get("category") or "upstream_error"),
        upstream_errmsg=str(failure.get("error_message") or ""),
        quota_state=quota_state or "unknown",
    )


def _dedupe_lines(lines: list[str], *, limit: int = 4, max_len: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in lines:
        clean = str(item or "").strip(" ，,。；;")
        if not clean:
            continue
        key = re.sub(r"\s+", "", clean)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:max_len])
        if len(out) >= limit:
            break
    return out


def _flatten_text_values(node, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            out.extend(_flatten_text_values(value, next_prefix))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            out.extend(_flatten_text_values(value, f"{prefix}[{idx}]"))
    else:
        text = str(node or "").strip()
        if text:
            out.append((prefix, text))
    return out


def _pick_texts_by_keywords(flattened: list[tuple[str, str]], keywords: list[str], *, limit: int = 3) -> list[str]:
    hits: list[str] = []
    for path, text in flattened:
        key = f"{path}|{text}"
        if any(keyword in key for keyword in keywords):
            hits.append(text)
    return _dedupe_lines(hits, limit=limit)


def _zodiac_topic_cn(topic: str) -> str:
    mapping = {
        "love": "感情运",
        "wealth": "财运",
        "career": "事业运",
        "study": "学业运",
    }
    return mapping.get(str(topic or ""), "整体运势")


def _zodiac_pick_by_path(
    flattened: list[tuple[str, str]],
    path_keywords: list[str],
    *,
    limit: int = 1,
    max_len: int = 90,
    allow_numeric: bool = True,
) -> list[str]:
    hits: list[str] = []
    for path, text in flattened:
        path_text = str(path or "")
        path_lower = path_text.lower()
        if any(keyword.lower() in path_lower for keyword in path_keywords):
            clean = str(text or "").strip(" ，,。；;")
            if not allow_numeric and re.fullmatch(r"\d+(?:\.\d+)?", clean):
                continue
            if clean:
                hits.append(clean[:max_len])
    return _dedupe_lines(hits, limit=limit, max_len=max_len)


def _zodiac_pick_summary(flattened: list[tuple[str, str]], label: str, topic: str, scope_cn: str) -> str:
    summary_candidates: list[str] = []
    for path, text in flattened:
        path_text = str(path or "")
        clean = str(text or "").strip(" ，,。；;")
        if not clean or re.fullmatch(r"(吉|凶|平|中高|中低|\d+)", clean):
            continue
        if re.search(r"(速配|提防|幸运|分数|心情|交际|爱情运势|事业运势|财富运势|健康运势)", path_text):
            continue
        if re.search(r"(今明运势|本周运势|本月运势|本年运势|今日运势|整体运势|综合运势)", path_text):
            summary_candidates.append(clean[:88])
    summary_candidates = _dedupe_lines(summary_candidates, limit=3, max_len=88)
    for item in summary_candidates:
        if item and item != label:
            return f"{label}{scope_cn}的{_zodiac_topic_cn(topic)}重点是：{item}"
    topic_cn = _zodiac_topic_cn(topic)
    return f"{label}{scope_cn}的{topic_cn}更适合先稳住节奏，再把最重要的那件事往前推。"


def _trim_cn_text(text: str, *, max_sentences: int = 2, max_len: int = 100) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    sentences = [seg.strip() for seg in re.split(r"(?<=[。！？!?])", raw) if seg.strip()]
    picked: list[str] = []
    total_len = 0
    for seg in sentences:
        next_len = total_len + len(seg)
        if picked and (len(picked) >= max_sentences or next_len > max_len):
            break
        picked.append(seg)
        total_len = next_len
        if len(picked) >= max_sentences or total_len >= max_len:
            break
    if picked:
        return "".join(picked).strip()
    clean = raw[:max_len].rstrip(" ，,；;:")
    if re.search(r"[。！？!?]$", clean):
        return clean
    clause = re.split(r"[，,；;：:]", clean)[0].strip()
    return f"{clause}。" if clause else ""


def _normalize_zodiac_scope_text(text: str, scope_cn: str) -> str:
    clean = _trim_cn_text(text)
    if not clean:
        return ""
    if scope_cn == "明日":
        clean = re.sub(r"^今日", "明日", clean)
    return clean


def _zodiac_scope_cn(scope_key: str) -> str:
    mapping = {
        "今日运势": "今日",
        "明日运势": "明日",
        "本周运势": "本周",
        "本月运势": "本月",
        "本年运势": "今年",
    }
    return mapping.get(str(scope_key or ""), "这段时间")


def _zodiac_select_scope_data(payload: dict, scope_key: str) -> tuple[dict, str]:
    data = payload.get("data", {}) or {}
    if not isinstance(data, dict):
        return {}, "这段时间"
    selected = data.get(scope_key)
    if isinstance(selected, dict) and selected:
        return selected, _zodiac_scope_cn(scope_key)
    for fallback_key in ["本周运势", "今日运势", "本月运势", "本年运势", "明日运势"]:
        fallback = data.get(fallback_key)
        if isinstance(fallback, dict) and fallback:
            return fallback, _zodiac_scope_cn(fallback_key)
    for key, value in data.items():
        if isinstance(value, dict) and value:
            return value, _zodiac_scope_cn(str(key))
    return {}, "这段时间"


def _zodiac_pick_summary_from_scope(scope_data: dict, label: str, topic: str, scope_cn: str) -> str:
    for key in ["今明运势", "本周运势", "本月运势", "本年运势", "整体运势", "综合运势"]:
        if str(scope_data.get(key) or "").strip():
            summary = _normalize_zodiac_scope_text(str(scope_data.get(key) or ""), scope_cn)
            if summary:
                return f"{label}{scope_cn}的{_zodiac_topic_cn(topic)}重点是：{summary}"
    flattened = _flatten_text_values(scope_data)
    return _zodiac_pick_summary(flattened, label=label, topic=topic, scope_cn=scope_cn)


def _zodiac_pick_section(flattened: list[tuple[str, str]], path_keywords: list[str], fallback_keywords: list[str]) -> str:
    direct = _zodiac_pick_by_path(flattened, path_keywords, limit=1, max_len=88, allow_numeric=False)
    if direct:
        return direct[0]
    for path, text in flattened:
        key = f"{path}|{text}"
        if any(keyword in key for keyword in fallback_keywords):
            clean = str(text or "").strip(" ，,。；;")
            if not clean or re.fullmatch(r"\d+(?:\.\d+)?", clean):
                continue
            if len(clean) < 6:
                continue
            return clean[:88]
    return ""


def _zodiac_pick_lucky_bundle(flattened: list[tuple[str, str]]) -> str:
    bits: list[str] = []
    color = _zodiac_pick_by_path(flattened, ["color", "颜色"], limit=1, max_len=20)
    number = _zodiac_pick_by_path(flattened, ["number", "num", "数字"], limit=1, max_len=12)
    stone = _zodiac_pick_by_path(flattened, ["stone", "gem", "宝石"], limit=1, max_len=20)
    match = _zodiac_pick_by_path(flattened, ["match", "pair", "compatible", "friend", "速配", "契合"], limit=1, max_len=20)
    caution = _zodiac_pick_by_path(flattened, ["avoid", "beware", "warning", "提防", "留心"], limit=1, max_len=20)
    if color:
        bits.append(f"幸运色 {color[0]}")
    if number:
        bits.append(f"幸运数字 {number[0]}")
    if stone:
        bits.append(f"幸运物 {stone[0]}")
    if match:
        bits.append(f"适合协作 {match[0]}")
    if caution:
        bits.append(f"相处上多留心 {caution[0]}")
    return "；".join(bits[:4])


def _zodiac_pick_action(flattened: list[tuple[str, str]], topic: str) -> str:
    tip = _zodiac_pick_section(
        flattened,
        ["tip", "advice", "suggest", "notice", "remind", "建议", "提醒"],
        ["建议", "提醒", "注意", "宜：", "忌："],
    )
    if tip:
        first_sentence = _trim_cn_text(tip, max_sentences=1, max_len=72)
        return first_sentence or _trim_cn_text(tip, max_sentences=1, max_len=72)
    fallback = {
        "love": "别急着把关系推进太快，先把真实感受说清楚。",
        "wealth": "先守住节奏和预算，再考虑放大动作。",
        "career": "先把优先级收回来，最重要的一件事先做完。",
        "study": "先把最容易卡住的那一小段攻下来，再扩展。",
    }
    return fallback.get(str(topic or ""), "先把重心收回来，一次只推进一件最重要的事。")


def _zodiac_build_action(topic: str, summary: str, love: str, career: str, wealth: str, health: str) -> str:
    pool = " ".join([summary, love, career, wealth, health])
    if topic == "love":
        return "先把回应和沟通节奏稳住，比急着推进关系更有效。"
    if topic == "wealth":
        return "先把支出和风险收住，再决定要不要加大投入。"
    if topic == "career":
        return "把最关键的一件事先推进，别被零碎事务分走注意力。"
    if re.search(r"(谨慎|波折|变数|风险|受阻|压力)", pool):
        return "这段时间先稳住节奏，重要安排多确认一步再推进。"
    if re.search(r"(机遇|好运|顺利|突破|上升|机会)", pool):
        return "把最重要的一件事往前推，顺势做减法会更容易出结果。"
    return "先稳住节奏，把重心收回到最重要的一件事上。"


def _zodiac_line_intro(kind: str) -> str:
    mapping = {
        "love": "感情这条线呀",
        "career": "事业这边呢",
        "wealth": "财运这块的话",
        "health": "状态上要留意的是",
    }
    return mapping.get(kind, "这块来看")


def _zodiac_humanize_section(kind: str, text: str) -> str:
    clean = _trim_cn_text(text, max_sentences=2, max_len=120)
    if not clean:
        return ""
    clean = clean.strip()
    clean = re.sub(r"^(今日|本周|本月|今年)的?", "", clean).strip()
    clean = clean.lstrip("，,；;：:")
    if not clean:
        return ""
    if not re.search(r"[。！？!?]$", clean):
        clean = f"{clean}。"
    return f"{_zodiac_line_intro(kind)}，{clean}"


def _zodiac_build_story_block(love: str, career: str, wealth: str, health: str) -> str:
    lines = [
        _zodiac_humanize_section("love", love),
        _zodiac_humanize_section("career", career),
        _zodiac_humanize_section("wealth", wealth),
        _zodiac_humanize_section("health", health),
    ]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    if len(lines) <= 2:
        return "\n".join(lines)
    return "\n".join(lines[:3] + ["最后再提醒你一下，" + lines[3]])


def _build_confidence(model: BaziToolOutput) -> float:
    score = 0
    score += 1 if model.bazi else 0
    score += 1 if model.day_master else 0
    score += 1 if model.xiyongshen else 0
    score += 1 if any(
        [
            model.wuxing_scores.metal,
            model.wuxing_scores.wood,
            model.wuxing_scores.water,
            model.wuxing_scores.fire,
            model.wuxing_scores.earth,
        ]
    ) else 0
    score += 1 if any([model.fortune_signals.love, model.fortune_signals.wealth, model.fortune_signals.career]) else 0
    return round(min(1.0, 0.2 + score * 0.16), 2)


def _parse_bazi_payload(payload: dict, topic: str) -> BaziToolOutput:
    data = payload.get("data", {}) or {}
    base_info = data.get("base_info", {}) or {}
    bazi_info = data.get("bazi_info", {}) or {}
    xiyongshen_info = data.get("xiyongshen", {}) or base_info.get("xiyongshen", {}) or {}
    caiyun = data.get("caiyun", {}) or {}
    caiyun_desc = caiyun.get("sanshishu_caiyun", {}) if isinstance(caiyun, dict) else {}
    yinyuan = data.get("yinyuan", {}) or {}
    mingyun = data.get("mingyun", {}) or {}
    taohua = data.get("taohua", {}) or {}
    yunshi_info = data.get("yunshi_info", {}) or {}

    wuxing_scores = WuxingScores(
        metal=_to_int(xiyongshen_info.get("jin_score") or xiyongshen_info.get("jin_number")),
        wood=_to_int(xiyongshen_info.get("mu_score") or xiyongshen_info.get("mu_number")),
        water=_to_int(xiyongshen_info.get("shui_score") or xiyongshen_info.get("shui_number")),
        fire=_to_int(xiyongshen_info.get("huo_score") or xiyongshen_info.get("huo_number")),
        earth=_to_int(xiyongshen_info.get("tu_score") or xiyongshen_info.get("tu_number")),
    )

    signals = FortuneSignals(
        love=str(yinyuan.get("sanshishu_yinyuan") or yunshi_info.get("love_description") or "")[:120],
        wealth=str((caiyun_desc or {}).get("simple_desc") or yunshi_info.get("wealth_description") or "")[:60],
        career=str(mingyun.get("sanshishu_mingyun") or yunshi_info.get("cause_description") or "")[:120],
    )

    opportunity_points = _to_lines(
        "；".join(
            [
                str((caiyun_desc or {}).get("simple_desc") or ""),
                str(yinyuan.get("sanshishu_yinyuan") or ""),
                str(mingyun.get("sanshishu_mingyun") or ""),
                str(yunshi_info.get("jixiong_today") or ""),
                str(yunshi_info.get("lucky_yi") or ""),
            ]
        )
    )
    risk_points = _to_lines(
        "；".join(
            [
                str((caiyun_desc or {}).get("risk_desc") or ""),
                str(taohua.get("risk_tip") or ""),
                str(yunshi_info.get("lucky_ji") or ""),
            ]
        )
    )
    time_hints = _to_lines(
        "；".join(
            [
                str(caiyun.get("time_hint") or ""),
                str(mingyun.get("time_hint") or ""),
                str(yunshi_info.get("lucky_directions") or ""),
                str(yunshi_info.get("lucky_color") or ""),
                str(yunshi_info.get("lucky_number") or ""),
            ]
        )
    )
    evidence_lines = _to_lines(
        "；".join(
            [
                str(bazi_info.get("bazi") or base_info.get("bazi_info", {}).get("bazi") or ""),
                str(xiyongshen_info.get("xiyongshen") or ""),
                str(xiyongshen_info.get("jishen") or ""),
                str(yunshi_info.get("health_description") or ""),
            ]
        )
    )

    model = BaziToolOutput(
        topic=topic,
        bazi=str(bazi_info.get("bazi") or base_info.get("bazi_info", {}).get("bazi") or ""),
        day_master=str(
            bazi_info.get("riyuan")
            or xiyongshen_info.get("rizhu_tiangan")
            or ""
        ),
        strength=_normalize_strength(str(xiyongshen_info.get("qiangruo") or "")),
        xiyongshen=str(xiyongshen_info.get("xiyongshen") or ""),
        jishen=str(xiyongshen_info.get("jishen") or ""),
        wuxing_scores=wuxing_scores,
        fortune_signals=signals,
        risk_points=risk_points,
        opportunity_points=opportunity_points,
        time_hints=time_hints,
        evidence_lines=evidence_lines,
        advice=_build_advice(topic, _normalize_strength(str(xiyongshen_info.get("qiangruo") or ""))),
    )
    model.confidence = _build_confidence(model)
    return model


def _parse_generic_fortune_payload(payload: dict, topic: str, source: str) -> dict:
    try:
        model = _parse_bazi_payload(payload, topic)
    except Exception:
        model = BaziToolOutput(topic=topic, advice=_build_advice(topic, "balanced"), confidence=0.35)

    data = payload.get("data", {}) or {}
    flattened = _flatten_text_values(data)
    generic_lines = _dedupe_lines([text for _, text in flattened], limit=10, max_len=140)

    if not any(model.fortune_signals.model_dump().values()):
        topic_keywords = {
            "love": ["姻缘", "感情", "桃花", "恋爱", "另一半", "配偶", "正缘"],
            "wealth": ["财", "收入", "开支", "守财", "开源", "财富"],
            "career": ["事业", "工作", "职场", "升迁"],
            "study": ["学业", "考试", "学习"],
            "daily": ["运势", "综合", "趋势"],
        }
        picked = _pick_texts_by_keywords(flattened, topic_keywords.get(topic, topic_keywords["daily"]), limit=2)
        fallback_signal = (picked or generic_lines[:2])[:2]
        if topic == "love":
            model.fortune_signals.love = "；".join(fallback_signal)[:120]
        elif topic == "wealth":
            model.fortune_signals.wealth = "；".join(fallback_signal)[:120]
        else:
            model.fortune_signals.career = "；".join(fallback_signal)[:120]

    if not model.opportunity_points:
        model.opportunity_points = _pick_texts_by_keywords(flattened, ["宜", "机会", "适合", "有利", "顺"], limit=3) or generic_lines[:3]
    if not model.risk_points:
        model.risk_points = _pick_texts_by_keywords(flattened, ["忌", "风险", "避免", "不宜", "注意"], limit=3)
    if not model.time_hints:
        model.time_hints = _pick_texts_by_keywords(flattened, ["时间", "月份", "今年", "明年", "阶段", "窗口"], limit=3)
    if not model.evidence_lines:
        model.evidence_lines = generic_lines[:3]
    advice_hits = _pick_texts_by_keywords(flattened, ["建议", "行动", "宜", "不宜", "避免", "优先"], limit=3)
    if advice_hits:
        model.advice = advice_hits
    if model.confidence <= 0.2:
        model.confidence = 0.58 if generic_lines else 0.35
    model.source = source
    return model.model_dump()


def _call_yuanfenju_endpoint(
    endpoint: str,
    *,
    data: dict,
    topic: str,
    operation: str,
    provider_id: str,
    max_retries: int = YUANFENJU_DEFAULT_RETRIES,
    timeout_seconds: int = YUANFENJU_DEFAULT_TIMEOUT_SECONDS,
    enable_merchant_probe: bool = True,
) -> dict:
    breaker = provider_should_short_circuit(None, "yuanfenju", operation)
    if breaker.get("short_circuit"):
        failure = build_provider_failure(
            provider="yuanfenju",
            operation=operation,
            category=str(breaker.get("last_category") or "unknown"),
            error_code="YUANFENJU_BREAKER_OPEN",
            error_message="命理服务暂时降级中，请稍后重试",
            retryable=False,
            breaker_ttl_seconds=max(60, int((breaker.get("open_until") or 0) - time.time())),
            raw_error=str(breaker),
        )
        return {
            "ok": False,
            "failure": failure,
            "provider_calls": 0,
            "quota_state": "breaker_open",
            "payload": _build_failure_payload(
                topic,
                failure,
                source=provider_id,
                provider_calls=0,
                quota_state="breaker_open",
                provider_fallback_reason="breaker_open",
            ),
        }

    url = f"{YUANFENJU_API_BASE}/{endpoint}"
    provider_calls = 0
    last_failure = build_provider_failure(
        provider="yuanfenju",
        operation=operation,
        category="unknown",
        error_code="YUANFENJU_HTTP_4XX",
        error_message="命理服务暂时不可用",
    )
    last_quota_state = "unknown"
    for attempt in range(max_retries + 1):
        try:
            provider_calls += 1
            result = requests.post(url, data=data, timeout=timeout_seconds)
            if result.status_code != 200:
                category = "http_5xx" if result.status_code >= 500 else "http_4xx"
                last_failure = build_provider_failure(
                    provider="yuanfenju",
                    operation=operation,
                    category=category,
                    error_code="YUANFENJU_HTTP_5XX" if result.status_code >= 500 else "YUANFENJU_HTTP_4XX",
                    error_message=f"命理服务响应异常（HTTP {result.status_code}）",
                    http_status=result.status_code,
                    raw_error=result.text[:240],
                )
                if enable_merchant_probe and result.status_code < 500:
                    probe = yuanfenju_query_merchant(force_refresh=False)
                    last_failure, last_quota_state = _failure_from_probe(last_failure, probe)
                raise requests.RequestException(last_failure["error_message"])
            payload = result.json()
            logger.info(f"缘分居接口 {endpoint} 返回JSON: {payload}")
            if int(payload.get("errcode", 1)) != 0:
                msg = str(payload.get("errmsg") or "命理服务返回错误")
                last_failure = build_provider_failure(
                    provider="yuanfenju",
                    operation=operation,
                    category="http_4xx",
                    error_code="YUANFENJU_HTTP_4XX",
                    error_message=msg,
                    http_status=200,
                    raw_error=json.dumps(payload, ensure_ascii=False)[:240],
                )
                if enable_merchant_probe:
                    probe = yuanfenju_query_merchant(force_refresh=False)
                    last_failure, last_quota_state = _failure_from_probe(last_failure, probe)
                provider_record_failure(None, last_failure)
                return {
                    "ok": False,
                    "failure": last_failure,
                    "provider_calls": provider_calls,
                    "quota_state": last_quota_state,
                    "payload": _build_failure_payload(
                        topic,
                        last_failure,
                        source=provider_id,
                        provider_calls=provider_calls,
                        quota_state=last_quota_state,
                    ),
                }
            provider_record_success(None, "yuanfenju", operation)
            return {
                "ok": True,
                "payload": payload,
                "provider_calls": provider_calls,
                "quota_state": "healthy",
            }
        except requests.Timeout:
            last_failure = build_provider_failure(
                provider="yuanfenju",
                operation=operation,
                category="timeout",
                error_code="YUANFENJU_TIMEOUT",
                error_message="命理服务超时，请稍后重试",
            )
        except (requests.RequestException, json.JSONDecodeError) as e:
            logger.warning(f"缘分居接口请求失败 endpoint={endpoint} attempt={attempt + 1}: {e}")
            if isinstance(e, json.JSONDecodeError):
                last_failure = build_provider_failure(
                    provider="yuanfenju",
                    operation=operation,
                    category="invalid_response",
                    error_code="YUANFENJU_INVALID_RESPONSE",
                    error_message="命理结果解析失败",
                    raw_error=str(e),
                )
            elif last_failure.get("category") == "unknown":
                last_failure = build_provider_failure(
                    provider="yuanfenju",
                    operation=operation,
                    category="network",
                    error_code="YUANFENJU_NETWORK",
                    error_message="命理服务暂时不可用",
                    raw_error=str(e),
                )
        if attempt < max_retries:
            time.sleep(0.4 * (2 ** attempt))

    provider_record_failure(None, last_failure)
    return {
        "ok": False,
        "failure": last_failure,
        "provider_calls": provider_calls,
        "quota_state": last_quota_state,
        "payload": _build_failure_payload(
            topic,
            last_failure,
            source=provider_id,
            provider_calls=provider_calls,
            quota_state=last_quota_state,
        ),
    }


def _finalize_birth_based_payload(result: dict, *, topic: str, source: str) -> dict:
    if not result.get("ok"):
        return result.get("payload") or _build_failure_payload(
            topic,
            result.get("failure") or {},
            source=source,
            provider_calls=_to_int(result.get("provider_calls") or 0),
            quota_state=str(result.get("quota_state") or "unknown"),
        )
    parsed = _parse_generic_fortune_payload(result.get("payload") or {}, topic, source)
    try:
        validated = BaziToolOutput.model_validate(parsed).model_dump()
    except ValidationError as ve:
        failure = build_provider_failure(
            provider="yuanfenju",
            operation=source,
            category="invalid_response",
            error_code="YUANFENJU_INVALID_RESPONSE",
            error_message="命理结果解析失败",
            raw_error=str(ve),
        )
        provider_record_failure(None, failure)
        return _build_failure_payload(
            topic,
            failure,
            source=source,
            provider_calls=_to_int(result.get("provider_calls") or 0),
            quota_state=str(result.get("quota_state") or "unknown"),
            provider_fallback_reason="invalid_response",
        )
    validated["source"] = source
    return _attach_provider_meta(
        validated,
        provider_id=source,
        provider_calls=_to_int(result.get("provider_calls") or 0),
        provider_fallback_reason="",
        upstream_errmsg="",
        quota_state=str(result.get("quota_state") or "healthy"),
    )


def _extract_zhengyuan_profile_bundle(raw_payload: dict) -> dict:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    data = payload.get("data") or {}
    zhengyuan_info = data.get("zhengyuan_info") or {}
    if not isinstance(zhengyuan_info, dict):
        return {}
    huaxiang = zhengyuan_info.get("huaxiang") or {}
    tezhi = zhengyuan_info.get("tezhi") or {}
    zhiyin = zhengyuan_info.get("zhiyin") or {}
    bundle = {
        "huaxiang": {
            "face_shape": str((huaxiang or {}).get("face_shape") or "").strip(),
            "eyebrow_shape": str((huaxiang or {}).get("eyebrow_shape") or "").strip(),
            "eye_shape": str((huaxiang or {}).get("eye_shape") or "").strip(),
            "mouth_shape": str((huaxiang or {}).get("mouth_shape") or "").strip(),
            "nose_shape": str((huaxiang or {}).get("nose_shape") or "").strip(),
            "body_shape": str((huaxiang or {}).get("body_shape") or "").strip(),
            "profile_image": str((huaxiang or {}).get("profile_image") or "").strip(),
        },
        "tezhi": {
            "romantic_personality": str((tezhi or {}).get("romantic_personality") or "").strip(),
            "family_background": str((tezhi or {}).get("family_background") or "").strip(),
            "career_wealth": str((tezhi or {}).get("career_wealth") or "").strip(),
            "marital_happiness": str((tezhi or {}).get("marital_happiness") or "").strip(),
        },
        "zhiyin": {
            "love_location": str((zhiyin or {}).get("love_location") or "").strip(),
            "meeting_method": str((zhiyin or {}).get("meeting_method") or "").strip(),
            "interaction_model": str((zhiyin or {}).get("interaction_model") or "").strip(),
            "love_advice": str((zhiyin or {}).get("love_advice") or "").strip(),
        },
        "yunshi": str(zhengyuan_info.get("yunshi") or "").strip(),
    }
    return bundle


def _extract_jiehun_prediction_bundle(raw_payload: dict) -> dict:
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    data = payload.get("data") or {}
    guigu = data.get("guigu") or {}
    description = (guigu.get("description") or {}) if isinstance(guigu, dict) else {}
    return {
        "star_name": str((description or {}).get("鬼谷星") or "").strip(),
        "star_desc": str((description or {}).get("星描述") or "").strip(),
        "romantic_personality": str((description or {}).get("恋爱性格") or "").strip(),
        "destined_partner": str((description or {}).get("宿命恋人") or "").strip(),
        "peak_love_ages": str((description or {}).get("桃花运盛龄") or "").strip(),
        "gap_ages": str((description or {}).get("情感空窗龄") or "").strip(),
        "love_desc": str((description or {}).get("情感描述") or "").strip(),
    }


def run_yuanfenju_bazi_cesuan(
    query: str,
    *,
    profile: dict | None = None,
    topic: str | None = None,
    enable_merchant_probe: bool = True,
) -> dict:
    resolved_topic = str(topic or _infer_topic(query or ""))
    if YUANFENJU_API_KEY is None:
        return _empty_bazi_output(resolved_topic, "FORTUNE_API_KEY_MISSING", "未配置命理服务密钥")
    extracted = _extract_yuanfenju_birth_params(query, profile=profile)
    if not extracted.get("ok"):
        return _empty_bazi_output(
            resolved_topic,
            "FORTUNE_PARAM_EXTRACT_FAILED",
            str(extracted.get("message") or "参数抽取失败，请补充姓名和出生年月日时"),
        )
    params = dict(extracted.get("params") or {})
    params["lang"] = YUANFENJU_DEFAULT_LANG
    params["factor"] = 1
    result = _call_yuanfenju_endpoint(
        "Bazi/cesuan",
        data=params,
        topic=resolved_topic,
        operation="fortune_submit",
        provider_id="yuanfenju_bazi_cesuan",
        enable_merchant_probe=enable_merchant_probe,
    )
    return _finalize_birth_based_payload(result, topic=resolved_topic, source="yuanfenju_bazi_cesuan")


def run_yuanfenju_bazi_daily(
    query: str,
    *,
    profile: dict | None = None,
    topic: str = "daily",
    enable_merchant_probe: bool = True,
) -> dict:
    extracted = _extract_yuanfenju_birth_params(query, profile=profile)
    if not extracted.get("ok"):
        return _empty_bazi_output(topic, "FORTUNE_PARAM_EXTRACT_FAILED", str(extracted.get("message") or "参数抽取失败"))
    params = dict(extracted.get("params") or {})
    params["lang"] = YUANFENJU_DEFAULT_LANG
    result = _call_yuanfenju_endpoint(
        "Bazi/yunshi",
        data=params,
        topic=topic,
        operation="fortune_daily",
        provider_id="yuanfenju_bazi_yunshi",
        enable_merchant_probe=enable_merchant_probe,
    )
    return _finalize_birth_based_payload(result, topic=topic, source="yuanfenju_bazi_yunshi")


def run_yuanfenju_bazi_future(
    query: str,
    *,
    profile: dict | None = None,
    yunshi_year: int,
    topic: str = "daily",
    enable_merchant_probe: bool = True,
) -> dict:
    extracted = _extract_yuanfenju_birth_params(query, profile=profile)
    if not extracted.get("ok"):
        return _empty_bazi_output(topic, "FORTUNE_PARAM_EXTRACT_FAILED", str(extracted.get("message") or "参数抽取失败"))
    params = dict(extracted.get("params") or {})
    params.update({"lang": YUANFENJU_DEFAULT_LANG, "yunshi_year": _to_int(yunshi_year)})
    result = _call_yuanfenju_endpoint(
        "Bazi/weilai",
        data=params,
        topic=topic,
        operation="fortune_future",
        provider_id="yuanfenju_bazi_weilai",
        enable_merchant_probe=enable_merchant_probe,
    )
    return _finalize_birth_based_payload(result, topic=topic, source="yuanfenju_bazi_weilai")


def run_yuanfenju_wealth_year(
    query: str,
    *,
    profile: dict | None = None,
    liu_year: int,
    enable_merchant_probe: bool = True,
) -> dict:
    extracted = _extract_yuanfenju_birth_params(query, profile=profile)
    if not extracted.get("ok"):
        return _empty_bazi_output("wealth", "FORTUNE_PARAM_EXTRACT_FAILED", str(extracted.get("message") or "参数抽取失败"))
    params = dict(extracted.get("params") or {})
    params.update({"lang": YUANFENJU_DEFAULT_LANG, "liu_year": _to_int(liu_year)})
    result = _call_yuanfenju_endpoint(
        "Bazi/caiyunfenxi",
        data=params,
        topic="wealth",
        operation="wealth_year",
        provider_id="yuanfenju_caiyunfenxi",
        enable_merchant_probe=enable_merchant_probe,
    )
    return _finalize_birth_based_payload(result, topic="wealth", source="yuanfenju_caiyunfenxi")


def run_yuanfenju_wealth_profile(
    query: str,
    *,
    profile: dict | None = None,
    enable_merchant_probe: bool = True,
) -> dict:
    extracted = _extract_yuanfenju_birth_params(query, profile=profile)
    if not extracted.get("ok"):
        return _empty_bazi_output("wealth", "FORTUNE_PARAM_EXTRACT_FAILED", str(extracted.get("message") or "参数抽取失败"))
    params = dict(extracted.get("params") or {})
    params.update({"lang": YUANFENJU_DEFAULT_LANG, "factor": 1})
    result = _call_yuanfenju_endpoint(
        "Yuce/caiyun",
        data=params,
        topic="wealth",
        operation="wealth_profile",
        provider_id="yuanfenju_yuce_caiyun",
        enable_merchant_probe=enable_merchant_probe,
    )
    return _finalize_birth_based_payload(result, topic="wealth", source="yuanfenju_yuce_caiyun")


def run_yuanfenju_love_profile(
    query: str,
    *,
    profile: dict | None = None,
    variant: str = "yinyuan",
    enable_merchant_probe: bool = True,
) -> dict:
    extracted = _extract_yuanfenju_birth_params(query, profile=profile)
    if not extracted.get("ok"):
        return _empty_bazi_output("love", "FORTUNE_PARAM_EXTRACT_FAILED", str(extracted.get("message") or "参数抽取失败"))
    params = dict(extracted.get("params") or {})
    params["lang"] = YUANFENJU_DEFAULT_LANG
    if variant == "yinyuan":
        params["factor"] = 1
    if variant == "zhengyuan":
        endpoint = "Yuce/zhengyuan"
        provider_id = "yuanfenju_zhengyuan"
        operation = "love_profile_zhengyuan"
    elif variant == "jiehun":
        endpoint = "Yuce/jiehun"
        provider_id = "yuanfenju_jiehun"
        operation = "love_profile_jiehun"
    else:
        endpoint = "Yuce/yinyuan"
        provider_id = "yuanfenju_yinyuan"
        operation = "love_profile_yinyuan"
    result = _call_yuanfenju_endpoint(
        endpoint,
        data=params,
        topic="love",
        operation=operation,
        provider_id=provider_id,
        enable_merchant_probe=enable_merchant_probe,
    )
    finalized = _finalize_birth_based_payload(result, topic="love", source=provider_id)
    if variant == "zhengyuan" and result.get("ok"):
        bundle = _extract_zhengyuan_profile_bundle(result.get("payload") or {})
        if bundle:
            finalized["zhengyuan_profile"] = bundle
            portrait = str(((bundle.get("huaxiang") or {}).get("profile_image")) or "").strip()
            if portrait:
                finalized["partner_portrait_image"] = portrait
    if variant == "jiehun" and result.get("ok"):
        bundle = _extract_jiehun_prediction_bundle(result.get("payload") or {})
        if bundle:
            finalized["jiehun_profile"] = bundle
    return finalized


def _render_zodiac_text(payload: dict, *, label: str, topic: str, scope_key: str) -> str:
    scope_data, scope_cn = _zodiac_select_scope_data(payload, scope_key)
    flattened = _flatten_text_values(scope_data)
    summary = _zodiac_pick_summary_from_scope(scope_data, label=label, topic=topic, scope_cn=scope_cn)
    love = _normalize_zodiac_scope_text(
        _zodiac_pick_section(flattened, ["love", "emotion", "affection", "爱情运势", "感情运势"], ["爱情运势", "爱情", "感情", "桃花"]),
        scope_cn,
    )
    career = _normalize_zodiac_scope_text(
        _zodiac_pick_section(flattened, ["career", "cause", "work", "事业运势", "工作运势"], ["事业运势", "事业", "工作", "职场"]),
        scope_cn,
    )
    wealth = _normalize_zodiac_scope_text(
        _zodiac_pick_section(flattened, ["wealth", "money", "fortune", "财富运势", "财运运势"], ["财富运势", "财运", "财富", "收入"]),
        scope_cn,
    )
    health = _normalize_zodiac_scope_text(
        _zodiac_pick_section(flattened, ["health", "body", "健康运势"], ["健康运势", "健康", "状态"]),
        scope_cn,
    )
    lucky_bundle = _zodiac_pick_lucky_bundle(flattened)
    action = _zodiac_build_action(topic, summary, love, career, wealth, health)
    story_block = _zodiac_build_story_block(love, career, wealth, health)

    parts = [f"呀哈～本鼠鼠先替你翻了翻{label}{scope_cn}的小册子：{summary}"]
    if story_block:
        parts.append(f"如果把这段运势慢慢拆开看，大致是这样：\n{story_block}")
    if lucky_bundle:
        parts.append(f"小幸运我也替你捎来了：{lucky_bundle}。")
    parts.append(f"吉伊的小建议是：{action}")
    return "\n".join([part for part in parts if part])


def run_yuanfenju_zodiac_yunshi(
    *,
    label: str,
    title_yunshi: int,
    entity_type: int,
    topic: str,
    scope_key: str,
    enable_merchant_probe: bool = True,
) -> dict:
    if YUANFENJU_API_KEY is None:
        failure = build_provider_failure(
            provider="yuanfenju",
            operation="zodiac_yunshi",
            category="auth",
            error_code="YUANFENJU_AUTH_INVALID",
            error_message="未配置命理服务密钥",
            retryable=False,
        )
        return {"ok": False, "failure": failure, "provider_calls": 0, "quota_state": "auth_invalid"}
    result = _call_yuanfenju_endpoint(
        "Zhanbu/yunshi",
        data={
            "api_key": YUANFENJU_API_KEY,
            "type": _to_int(entity_type),
            "title_yunshi": _to_int(title_yunshi),
            "lang": YUANFENJU_DEFAULT_LANG,
        },
        topic=topic,
        operation="zodiac_yunshi",
        provider_id="yuanfenju_zhanbu_yunshi",
        max_retries=1,
        timeout_seconds=2,
        enable_merchant_probe=enable_merchant_probe,
    )
    if not result.get("ok"):
        return result
    text = _render_zodiac_text(result.get("payload") or {}, label=label, topic=topic, scope_key=scope_key)
    result["text"] = text
    result["provider_id"] = "yuanfenju_zhanbu_yunshi"
    return result


def _parse_date_bound(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _parse_zeshi_detail_date(raw: str, *, default_year: int = 0) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    full_match = re.search(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", text)
    if full_match:
        y, m, d = full_match.groups()
        try:
            return datetime(int(y), int(m), int(d))
        except Exception:
            return None
    short_match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if short_match and default_year:
        m, d = short_match.groups()
        try:
            return datetime(default_year, int(m), int(d))
        except Exception:
            return None
    return None


def _filter_zeshi_details_to_window(details: list[dict], *, window_start: str = "", window_end: str = "") -> list[dict]:
    if not details:
        return []
    start_dt = _parse_date_bound(window_start)
    end_dt = _parse_date_bound(window_end)
    if not start_dt or not end_dt:
        return details
    default_year = start_dt.year
    filtered: list[dict] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        date_text = str(item.get("day") or item.get("yangli") or item.get("date") or "").strip()
        detail_dt = _parse_zeshi_detail_date(date_text, default_year=default_year)
        if detail_dt and start_dt.date() <= detail_dt.date() <= end_dt.date():
            filtered.append(item)
    return filtered


def _expand_zeshi_incident_keywords(incident_label: str) -> list[str]:
    label = str(incident_label or "").strip()
    table = {
        "搬家": ["搬家", "迁徙", "乔迁", "移徙", "入宅"],
        "迁徙": ["搬家", "迁徙", "乔迁", "移徙", "入宅"],
        "乔迁": ["搬家", "迁徙", "乔迁", "移徙", "入宅"],
        "入宅": ["搬家", "迁徙", "乔迁", "移徙", "入宅"],
        "领证": ["领证", "嫁娶", "结婚", "纳婿"],
        "嫁娶": ["领证", "嫁娶", "结婚", "纳婿"],
        "结婚": ["领证", "嫁娶", "结婚", "纳婿"],
        "订婚": ["订婚", "纳采", "订盟"],
        "出行": ["出行", "旅行", "远行"],
        "开市": ["开市", "开业", "交易"],
        "开业": ["开市", "开业", "交易"],
        "求医": ["求医", "就医", "看病", "治病"],
        "看病": ["求医", "就医", "看病", "治病"],
    }
    keywords = table.get(label, [label] if label else [])
    deduped: list[str] = []
    seen: set[str] = set()
    for item in keywords:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _zeshi_contains_any(text: str, keywords: list[str]) -> bool:
    src = str(text or "").strip()
    if not src:
        return False
    return any(keyword and keyword in src for keyword in keywords)


def _is_zeshi_positive_level(level: str) -> bool:
    raw = str(level or "").strip()
    if not raw:
        return False
    if "凶" in raw or "黑道" in raw:
        return False
    return "吉" in raw or "黄道" in raw


def _split_zeshi_terms(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\s、，,；;]+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = str(part or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _format_zeshi_date_label(raw: str, *, default_year: int = 0) -> str:
    dt = _parse_zeshi_detail_date(raw, default_year=default_year)
    if not dt:
        return str(raw or "").strip()
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
    return f"{dt.month}月{dt.day}日（{weekday}）"


def _normalize_zeshi_incident_label(label: str) -> str:
    src = str(label or "").strip()
    alias_map = {
        "迁徙": "搬家",
        "乔迁": "搬家",
        "入宅": "搬家",
        "嫁娶": "领证",
        "结婚": "领证",
        "纳婿": "领证",
        "纳采": "订婚",
        "开业": "开市",
        "就医": "求医",
        "看病": "求医",
        "治病": "求医",
    }
    return alias_map.get(src, src)


def _zeshi_render_terms(label: str, terms: list[str]) -> list[str]:
    normalized = _normalize_zeshi_incident_label(label)
    preferred_map = {
        "领证": ["嫁娶", "纳婿", "订盟"],
        "搬家": ["移徙", "入宅"],
        "订婚": ["订盟", "纳采", "嫁娶"],
        "出行": ["出行", "远行"],
        "开市": ["开市", "交易", "立券"],
        "求医": ["求医", "治病"],
    }
    preferred = preferred_map.get(normalized, [])
    matched = [term for term in preferred if term in terms]
    if matched:
        return matched
    return terms[:2]


def _build_zeshi_reason(label: str, yi_terms: list[str], level: str) -> str:
    normalized = _normalize_zeshi_incident_label(label)
    level_reason = "这天整体偏吉" if _is_zeshi_positive_level(level) else "这天虽然事项上能做，但整体不算特别稳"
    matched = _zeshi_render_terms(normalized, yi_terms)
    term_text = "、".join(matched)
    reason_map = {
        "领证": f"{level_reason}，黄历事项里明确带了“{term_text or '嫁娶'}”，适合把领证流程走得简洁一点",
        "搬家": f"{level_reason}，黄历事项里明确带了“{term_text or '移徙、入宅'}”，适合以搬入、安顿新家为主",
        "订婚": f"{level_reason}，黄历事项里带了“{term_text or '订盟'}”，更适合先把订婚流程定下来",
        "出行": f"{level_reason}，黄历事项里带了“{term_text or '出行'}”，更适合把出发和路上节奏安排得从容些",
        "开市": f"{level_reason}，黄历事项里带了“{term_text or '开市'}”，适合把开业、开张或签单放在当天重点推进",
        "求医": f"{level_reason}，黄历事项里带了“{term_text or '求医'}”，适合把就诊、复查这类正事优先办掉",
    }
    if normalized in reason_map:
        return reason_map[normalized]
    if term_text:
        return f"{level_reason}，黄历事项里也带了“{term_text}”，适合把“{label}”这件事作为当天重点"
    return f"{level_reason}，适合把“{label}”这件事安排得简单、专注一些"


def _build_zeshi_caution(label: str, ji_terms: list[str]) -> str:
    normalized = _normalize_zeshi_incident_label(label)
    if not ji_terms:
        return ""
    if normalized == "领证":
        noisy_terms = [term for term in ji_terms if term in {"入宅", "置产", "作灶", "作梁", "破土", "安葬", "开生坟"}]
        if noisy_terms:
            return f"当天不建议再顺手塞进“{'、'.join(noisy_terms[:2])}”这类杂事，把重点放在领证本身会更顺。"
        return "当天尽量别把流程排得太满，领证之外的杂事能拆开就拆开。"
    if normalized == "搬家":
        build_terms = [term for term in ji_terms if term in {"伐木", "作梁", "作灶", "开光", "安葬", "破土", "置产"}]
        if build_terms:
            return f"但不建议同天再叠加“{'、'.join(build_terms[:2])}”这类施工或重事务，越简单越顺。"
        return "搬家当天更适合专心搬入和安顿，不建议再叠加太多重事务。"
    if normalized == "出行":
        return "行程尽量留充足缓冲，不建议把赶路、办事、应酬全压在同一天。"
    if normalized == "开市":
        return "当天更适合把开张和签单放在前面，其他重安排尽量后置。"
    if normalized == "求医":
        return "当天以看诊和休息为主，不建议再穿插太耗神的安排。"
    return "当天把重点收拢到这一件事上，会比什么都一起办更顺。"


def _render_zeshi_recommendation_line(item: dict, *, incident_label: str, default_year: int = 0) -> str:
    date_text = str(item.get("day") or item.get("yangli") or item.get("date") or "").strip()
    yi_terms = _split_zeshi_terms(item.get("yi") or item.get("suitable_incident") or "")
    ji_terms = _split_zeshi_terms(item.get("ji") or item.get("taboo_incident") or "")
    level = str(item.get("jixiong") or item.get("huanghei") or "").strip()
    date_label = _format_zeshi_date_label(date_text, default_year=default_year)
    reason = _build_zeshi_reason(incident_label, yi_terms, level)
    caution = _build_zeshi_caution(incident_label, ji_terms)
    if caution:
        return f"{date_label}更推荐：{reason}。{caution}"
    return f"{date_label}更推荐：{reason}。"


def _render_zeshi_text(payload: dict, *, incident_label: str, window_start: str = "", window_end: str = "") -> str:
    data = payload.get("data", {}) or {}
    summary = str((data.get("base_info") or {}).get("summarize") or "").strip()
    details = (data.get("detail_info") or []) if isinstance(data.get("detail_info"), list) else []
    filtered_details = _filter_zeshi_details_to_window(details, window_start=window_start, window_end=window_end)
    keywords = _expand_zeshi_incident_keywords(incident_label)
    preferred_items: list[dict] = []
    caution_items: list[dict] = []
    default_year = _parse_date_bound(window_start).year if _parse_date_bound(window_start) else 0
    for item in filtered_details:
        if not isinstance(item, dict):
            continue
        date_text = str(item.get("day") or item.get("yangli") or item.get("date") or "").strip()
        yi = str(item.get("yi") or item.get("suitable_incident") or "").strip()
        ji = str(item.get("ji") or item.get("taboo_incident") or "").strip()
        level = str(item.get("jixiong") or item.get("huanghei") or "").strip()
        if not date_text or not (yi or ji or level):
            continue
        yi_matches = _zeshi_contains_any(yi, keywords)
        ji_conflict = _zeshi_contains_any(ji, keywords)
        if _is_zeshi_positive_level(level) and (yi_matches or not keywords) and not ji_conflict:
            preferred_items.append(item)
            continue
        if yi_matches and not ji_conflict:
            caution_items.append(item)
    if window_start and window_end:
        if preferred_items:
            summary = f"我按你关心的时间段（{window_start} 至 {window_end}）筛了一遍，更适合“{incident_label}”的日子主要在下面这几天。"
        elif caution_items:
            summary = (
                f"我按你关心的时间段（{window_start} 至 {window_end}）筛了一遍，"
                f"这段时间里暂时没有特别稳妥的“{incident_label}”吉日；下面这些日子虽然事项相合，但整体不算稳。"
            )
        else:
            summary = f"我按你关心的时间段（{window_start} 至 {window_end}）筛了一遍，这段时间里没有特别突出的“{incident_label}”吉日。"
    elif not summary:
        summary = f"接下来几天里，和“{incident_label}”最相关的日子已经帮你筛出来了。"
    if preferred_items:
        body = "\n".join(
            _render_zeshi_recommendation_line(item, incident_label=incident_label, default_year=default_year)
            for item in preferred_items[:3]
        )
    elif caution_items:
        body = "\n".join(
            _render_zeshi_recommendation_line(item, incident_label=incident_label, default_year=default_year)
            for item in caution_items[:3]
        )
    elif window_start and window_end:
        body = f"你关心的时间段（{window_start} 至 {window_end}）里，暂时没筛到特别突出的日子，建议优先选行程更从容、冲突更少的日期。"
    else:
        body = "这次择时结果暂时不够完整，建议优先选气场更稳、杂事更少的日期。"
    return f"呀哈～先给你结论：关于“{incident_label}”，{summary}\n{body}"


def run_yuanfenju_zeshi(
    *,
    future: int,
    incident: int,
    incident_label: str,
    window_start: str = "",
    window_end: str = "",
    enable_merchant_probe: bool = True,
) -> dict:
    if YUANFENJU_API_KEY is None:
        failure = build_provider_failure(
            provider="yuanfenju",
            operation="zeshi",
            category="auth",
            error_code="YUANFENJU_AUTH_INVALID",
            error_message="未配置命理服务密钥",
            retryable=False,
        )
        return {"ok": False, "failure": failure, "provider_calls": 0, "quota_state": "auth_invalid"}
    result = _call_yuanfenju_endpoint(
        "Gongju/zeshi",
        data={
            "api_key": YUANFENJU_API_KEY,
            "future": max(0, min(3, _to_int(future))),
            "incident": _to_int(incident),
            "lang": YUANFENJU_DEFAULT_LANG,
        },
        topic="daily",
        operation="zeshi",
        provider_id="yuanfenju_gongju_zeshi",
        max_retries=1,
        timeout_seconds=2,
        enable_merchant_probe=enable_merchant_probe,
    )
    if not result.get("ok"):
        return result
    result["text"] = _render_zeshi_text(
        result.get("payload") or {},
        incident_label=incident_label,
        window_start=window_start,
        window_end=window_end,
    )
    result["provider_id"] = "yuanfenju_gongju_zeshi"
    return result


@tool
def serp_search(query: str):
    """只有需要了解实时信息或不知道的事情的时候才会使用这个工具。"""
    if not SERPAPI_API_KEY:
        return "实时搜索暂不可用，请联系管理员配置 SERPAPI_API_KEY。"
    try:
        from langchain_community.utilities import SerpAPIWrapper
    except Exception as e:
        logger.error(f"SerpAPI 依赖不可用: {e}")
        return "实时搜索暂不可用（缺少 serpapi 依赖），请联系管理员安装后重试。"
    try:
        serp = SerpAPIWrapper()
        result = serp.run(query)
    except Exception as e:
        logger.error(f"SerpAPI 调用失败: {e}")
        return "实时搜索服务暂时不可用，请稍后再试。"
    logger.info(f"实时搜索结果: {result}")
    # 优化：将复杂对象转为友好字符串
    if isinstance(result, (list, dict)):
        # 只取前5个景点，格式化输出
        if isinstance(result, list) and len(result) > 0 and 'title' in result[0]:
            lines = [f"{i+1}. {item['title']}（{item.get('description','')}，評分：{item.get('rating','N/A')}）" for i, item in enumerate(result[:5])]
            return "\n".join(lines)
        return json.dumps(result, ensure_ascii=False)
    return str(result)


#对知识库的检索，本质就是个RAG
@tool
def get_info_from_local_db(query: str):
    """只有回答与办公室风水常识相关的问题的时候，会使用这个工具。"""
    client = Qdrant(
        QdrantClient(path=VECTOR_DB_PATH),
        VECTOR_COLLECTION_NAME,
        get_lc_ali_embeddings(),
    )

    retriever = client.as_retriever(search_type="mmr")
    result = retriever.get_relevant_documents(query)
    return result


@tool
def bazi_cesuan(query: str):
    """只有用户说要测试算八字或做八字排盘的时候才会使用这个工具,需要输入用户姓名和出生年月日时，
    如果缺少用户姓名和出生年月日时则不可用."""
    return json.dumps(run_yuanfenju_bazi_cesuan(query), ensure_ascii=False)

@tool
def yaoyigua():
    """只有用户想要占卜抽签的时候才会使用这个工具。"""
    api_key = YUANFENJU_API_KEY
    url = f"https://api.yuanfenju.com/index.php/v1/Zhanbu/meiri"
    result = requests.post(url, data={"api_key": api_key})
    logger.info(f"缘分居meiri接口返回: {result}")
    if result.status_code == 200:
        logger.info(f"缘分居meiri接口返回JSON: {result.json()}")
        return_string = json.loads(result.text)
        image = return_string["data"]["description"]
        logger.info(f"每日一占: {image}")
        return image
    else:
        return "技术错误，请告诉用户稍后再试。"


def _coerce_llm_text(value) -> str:
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                clean = item.strip()
                if clean:
                    parts.append(clean)
                continue
            if isinstance(item, dict):
                clean = str(item.get("text") or item.get("content") or "").strip()
                if clean:
                    parts.append(clean)
        if parts:
            return "".join(parts)
    return str(value or "")


def _extract_dream_keyword_local(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return ""
    normalized = text
    normalized = re.sub(r"^(?:请|麻烦|拜托)?(?:帮我|给我)?解梦[，,:：\s]*", "", normalized)
    normalized = re.sub(r"^(?:请|麻烦|拜托)?(?:帮我|给我)?(?:看下|看看)?梦境?[，,:：\s]*", "", normalized)
    normalized = re.sub(r"^(?:昨晚|昨天|夜里|半夜|刚刚|最近|前几天|这两天|今天)?\s*", "", normalized)
    normalized = re.sub(r"[“”\"'`‘’]", "", normalized).strip()

    match = re.search(
        r"(?:我?(?:又)?(?:做梦)?梦见了?|我?(?:又)?(?:做梦)?梦到了?|梦到了?|梦见了?|做梦梦到了?|做梦梦见了?|梦里)(.+?)(?:是啥意思|是什么意思|什么预兆|预示着什么|意味着什么|怎么回事|好不好|代表什么|给我解梦|帮我解梦|呢|吗|[，。！？?]|$)",
        normalized,
    )
    candidate = str(match.group(1) or "").strip() if match else normalized
    candidate = re.sub(r"^(?:我|自己|我们|有人|一个人|一个|一只|一条|一头|一群|好多|很多|一堆)\s*", "", candidate).strip()
    candidate = re.sub(r"^(?:和|跟|与)\s*", "", candidate).strip()
    candidate = re.sub(r"(?:是什么意思|什么预兆|预示着什么|意味着什么|怎么回事|好不好|代表什么)$", "", candidate).strip()
    candidate = re.sub(r"[，。！？；;：:\s]+", "", candidate)
    if not candidate:
        return ""

    semantic_patterns = [
        (r"(爸爸妈妈|爸妈|父母)", "父母"),
        (r"(爷爷奶奶|祖父母)", "祖父母"),
        (r"(怀孕|生孩子|分娩)", "怀孕"),
        (r"(结婚|婚礼|成亲|嫁娶)", "结婚"),
        (r"(吵架|争吵|打架|冲突)", "吵架"),
        (r"(亲嘴|接吻|亲吻)", "接吻"),
        (r"(做爱|性爱|上床|发生关系|性行为|人类繁殖活动)", "性爱"),
        (r"(蛇|蟒蛇|毒蛇)", "蛇"),
        (r"(狗|小狗|大狗)", "狗"),
        (r"(猫|小猫)", "猫"),
        (r"(牙齿|掉牙|掉牙齿)", "掉牙"),
        (r"(水|大水|洪水|海水)", "水"),
        (r"(火|着火|火灾)", "火"),
        (r"(死了|死亡|去世)", "死亡"),
    ]
    for pattern, keyword in semantic_patterns:
        if re.search(pattern, candidate):
            return keyword

    pieces = [piece for piece in re.split(r"(?:然后|后来|结果|突然|忽然|正在|在|被|把|又|还|并且|而且|的时候|之后)", candidate) if piece]
    normalized_pieces: list[str] = []
    for piece in pieces:
        clean = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]", "", piece).strip()
        clean = re.sub(r"^(?:和|跟|与)", "", clean).strip()
        if clean:
            normalized_pieces.append(clean)

    for piece in normalized_pieces:
        if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{1,8}", piece):
            return piece

    compact = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]", "", candidate).strip()
    if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{1,8}", compact):
        return compact
    return ""


def _normalize_zhougong_keyword(raw_keyword, *, fallback_query: str = "") -> str:
    text = _coerce_llm_text(raw_keyword).strip()
    if not text and fallback_query:
        return _extract_dream_keyword_local(fallback_query)
    content_match = re.search(r"content=(?:'|\")([^'\"]+)(?:'|\")", text)
    if content_match:
        text = str(content_match.group(1) or "").strip()
    text = text.replace("\\n", "\n")
    text = re.sub(r"```(?:text|json)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)
    text = re.sub(r"^(关键词|关键字|答案|提取结果|keyword)[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" \n\r\t'\"“”‘’`")
    text = re.sub(r"(梦见|梦到|做梦梦到|做梦见到)", "", text)
    text = re.sub(r"(是什么意思|什么预兆|预示着什么|意味着什么|怎么回事|好不好|代表什么)", "", text)
    pieces = [piece for piece in re.split(r"[\n，。！？、,;；\s]+", text) if piece]
    for piece in pieces:
        clean = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]", "", piece).strip()
        if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{1,8}", clean):
            return clean
    fallback = _extract_dream_keyword_local(fallback_query)
    return fallback


@tool
def jiemeng(query: str):
    """只有用户想要解梦的时候才会使用这个工具,需要输入用户梦境的内容，如果缺少用户梦境的内容则不可用。"""
    api_key = YUANFENJU_API_KEY
    url = f"https://api.yuanfenju.com/index.php/v1/Gongju/zhougong"
    keyword = _extract_dream_keyword_local(query)
    if not keyword:
        LLM = get_lc_ali_model_client(streaming=False)
        prompt = PromptTemplate.from_template(
            "你是解梦关键词提取器。请从梦境描述中提取最适合查询周公解梦接口的1个中文关键词。"
            "只返回关键词本身，不要解释，不要标点，不要引号。内容为:{topic}"
        )
        prompt_value = prompt.invoke({"topic": query})
        keyword = _normalize_zhougong_keyword(LLM.invoke(prompt_value), fallback_query=query)
    logger.info(f"提取的关键词: {keyword}")
    if not keyword:
        return {"errcode": 1, "errmsg": "梦境关键词提取失败", "data": {}}
    result = requests.post(url, data={"api_key": api_key, "title_zhougong": keyword}, timeout=3)
    if result.status_code == 200:
        try:
            returnstring = json.loads(result.text)
        except Exception:
            return {"errcode": 1, "errmsg": "解梦结果解析失败", "data": {}}
        logger.info(f"缘分居zhougong接口返回JSON: {returnstring}")
        return returnstring
    else:
        return "技术错误，请告诉用户稍后再试。"
