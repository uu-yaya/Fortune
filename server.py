import os
import re
import secrets
import traceback
import uuid
import json
import hashlib
import hmac
import difflib
import base64
from datetime import datetime
from datetime import timedelta
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql
import redis
import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_classic.agents import create_tool_calling_agent, AgentExecutor
from langchain_classic.memory import ConversationBufferMemory
from langchain_community.chat_message_histories import RedisChatMessageHistory
from langchain_community.document_loaders import WebBaseLoader
from langchain_community.vectorstores import Qdrant
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger
from pydantic import BaseModel, Field

from config import (
    MYSQL_DB,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    REDIS_URL,
    SMS_ALIYUN_ACCESS_KEY_ID,
    SMS_ALIYUN_ACCESS_KEY_SECRET,
    SMS_ALIYUN_ENDPOINT,
    SMS_ALIYUN_REGION_ID,
    SMS_ALIYUN_SIGN_NAME,
    SMS_ALIYUN_TEMPLATE_CODE,
    SMS_DEBUG_CODE_ENABLED,
    SMS_HTTP_TIMEOUT_SECONDS,
    SMS_PROVIDER,
    SMS_TEMPLATE_PARAM_CODE_KEY,
    SERPAPI_API_KEY,
    SESSION_TTL_SECONDS,
    VECTOR_COLLECTION_NAME,
    VECTOR_DB_PATH,
)
from logger import setup_logger
from models import get_lc_ali_embeddings, get_lc_ali_model_client
from mytools import (
    bazi_cesuan,
    get_info_from_local_db,
    jiemeng,
    run_yuanfenju_bazi_daily,
    run_yuanfenju_bazi_future,
    run_yuanfenju_bazi_cesuan,
    run_yuanfenju_love_profile,
    run_yuanfenju_wealth_profile,
    run_yuanfenju_wealth_year,
    run_yuanfenju_zeshi,
    run_yuanfenju_zodiac_yunshi,
    serp_search,
    yaoyigua,
)
#langchain.debug = True

app = FastAPI(
    title="吉伊大师 API",
    description="命理咨询服务接口文档。可在此页面完成验证码登录、账号密码登录、忘记密码、聊天和知识入库联调。",
    version="1.0.0",
)
AUTH_COOKIE_NAME = "jiyi_auth_token"
CODE_TTL_SECONDS = 300
RESEND_COOLDOWN_SECONDS = 60
AUTH_TTL_DAYS = 30
PREFERRED_NAME_PROMPT_TTL_SECONDS = 24 * 3600
NAME_CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
_REDIS_CLIENT = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class SendCodeRequest(BaseModel):
    phone: str = Field(default="", description="11位中国大陆手机号", examples=["13800138000"])
    scene: str = Field(default="default", description="验证码场景", examples=["login", "register", "reset_password"])


class VerifyRequest(BaseModel):
    phone: str = Field(default="", description="11位中国大陆手机号", examples=["13800138000"])
    code: str = Field(default="", description="6位验证码", examples=["123456"])
    mode: str = Field(default="login", description="登录模式: login/register", examples=["login", "register"])
    password: str = Field(default="", description="注册时可选密码（8-12位字母或数字）", examples=["abc12345"])


class PasswordLoginRequest(BaseModel):
    account: str = Field(default="", description="账号（如 JIYI-AB12CD34）", examples=["JIYI-AB12CD34"])
    password: str = Field(default="", description="账号密码", examples=["abc12345"])


class PasswordVerifyCodeRequest(BaseModel):
    phone: str = Field(default="", description="11位中国大陆手机号", examples=["13800138000"])
    code: str = Field(default="", description="6位验证码", examples=["123456"])


class PasswordResetRequest(BaseModel):
    phone: str = Field(default="", description="11位中国大陆手机号", examples=["13800138000"])
    new_password: str = Field(default="", description="新密码（8-12位字母或数字）", examples=["abc12345"])
    confirm_password: str = Field(default="", description="确认密码", examples=["abc12345"])


class ChatRequest(BaseModel):
    query: Optional[str] = Field(default=None, description="聊天问题", examples=["我想看下最近事业运"])
    session_id: Optional[str] = Field(default=None, description="兼容字段，后端按用户UUID维护会话", examples=["optional-client-id"])

# 挂载静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")

# 设置模板
templates = Jinja2Templates(directory="templates")

# 搜索的apikey
if SERPAPI_API_KEY:
    os.environ["SERPAPI_API_KEY"] = SERPAPI_API_KEY
# redis的IP地址和端口请根据实际情况修改
"""如果采用Docker部署，且本应用和Redis是两个独立容器，
则访问redis的地址是 redis://host.docker.internal:6379/"""

# memory存储
# chat_message_history = RedisChatMessageHistory(url=REDIS_URL, session_id="session")

# # 定义请求模型
# class ChatRequest(BaseModel):
#     query: str
#     session_id: str = "default_session"  # 新增 session_id 字段，默认值


def _db_conn():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _sms_key(phone: str, scene: str = "default") -> str:
    return f"auth:sms:{scene}:{phone}"


def _sms_cooldown_key(phone: str, scene: str = "default") -> str:
    return f"auth:sms:cooldown:{scene}:{phone}"


def _session_key(token: str) -> str:
    return f"auth:session:{token}"


def _preferred_name_prompt_key(session_id: str) -> str:
    sid = str(session_id or "").strip()
    return f"chat:preferred_name_prompt:{sid}"


def _pwd_reset_verified_key(phone: str) -> str:
    return f"auth:pwdreset:verified:{phone}"


def _save_auth_session_to_db(user_id: int, token: str, login_type: str, request: Request):
    token_hash = _hash_token(token)
    expires_at = datetime.now() + timedelta(days=AUTH_TTL_DAYS)
    device_info = str(request.headers.get("user-agent", ""))[:255]
    ip = str(request.client.host) if request.client else ""
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_sessions (user_id, token_hash, login_type, device_info, ip, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, token_hash, login_type, device_info, ip, expires_at),
            )


def _revoke_auth_session_in_db(token: str):
    token_hash = _hash_token(token)
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = NOW()
                WHERE token_hash = %s AND revoked_at IS NULL
                """,
                (token_hash,),
            )


def _get_user_by_phone(phone: str) -> dict | None:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, uuid, account, phone, password_hash, created_at FROM users WHERE phone = %s LIMIT 1",
                (phone,),
            )
            return cur.fetchone()


def _get_user_by_account(account: str) -> dict | None:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, uuid, account, phone, password_hash FROM users WHERE account = %s LIMIT 1",
                (account,),
            )
            return cur.fetchone()


def _get_user_by_uuid(user_uuid: str) -> dict | None:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, uuid, account, phone FROM users WHERE uuid = %s LIMIT 1",
                (user_uuid,),
            )
            return cur.fetchone()


def _password_valid(password: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{8,12}", password or ""))


def _hash_password(password: str, salt: str | None = None) -> str:
    s = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), s.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256${s}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    raw = str(stored or "")
    if not raw.startswith("pbkdf2_sha256$"):
        return False
    try:
        _, salt, hashed = raw.split("$", 2)
    except ValueError:
        return False
    candidate = _hash_password(password, salt=salt).split("$", 2)[2]
    return hmac.compare_digest(candidate, hashed)


def _create_user_by_phone(phone: str, password: str) -> dict:
    user_uuid = uuid.uuid4().hex
    account = f"JIYI-{user_uuid[:8].upper()}"
    password_hash = _hash_password(password)
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (uuid, account, phone, password_hash, status)
                VALUES (%s, %s, %s, %s, 1)
                """,
                (user_uuid, account, phone, password_hash),
            )
            user_id = cur.lastrowid
            cur.execute(
                "INSERT INTO user_profile (user_id) VALUES (%s)",
                (user_id,),
            )
    return {"id": user_id, "uuid": user_uuid, "account": account, "phone": phone}


def _update_user_password(user_id: int, new_password: str):
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                (_hash_password(new_password), user_id),
            )


def _log_password_reset(user_id: int, phone: str, request: Request):
    ip = str(request.client.host) if request.client else ""
    ua = str(request.headers.get("user-agent", ""))[:255]
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO password_reset_logs (user_id, phone, ip, user_agent) VALUES (%s, %s, %s, %s)",
                (user_id, phone, ip, ua),
            )


def _load_profile_json(raw_value) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if raw_value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(raw_value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _normalize_name_confidence(level: str) -> str:
    raw = str(level or "").strip().lower()
    if raw in NAME_CONFIDENCE_ORDER:
        return raw
    return "none"


def _confidence_ge(left: str, right: str) -> bool:
    l = NAME_CONFIDENCE_ORDER.get(_normalize_name_confidence(left), 0)
    r = NAME_CONFIDENCE_ORDER.get(_normalize_name_confidence(right), 0)
    return l >= r


def _dump_profile_json(
    preferred_name: str = "",
    name_confidence: str = "",
    preferred_name_confidence: str = "",
    gender: str = "",
) -> str | None:
    payload: dict[str, str] = {}
    call_name = str(preferred_name or "").strip()
    if call_name:
        payload["preferred_name"] = call_name
    gender_text = str(gender or "").strip()
    if gender_text:
        payload["gender"] = gender_text
    n_conf = _normalize_name_confidence(name_confidence)
    p_conf = _normalize_name_confidence(preferred_name_confidence)
    if n_conf != "none":
        payload["name_confidence"] = n_conf
    if p_conf != "none":
        payload["preferred_name_confidence"] = p_conf
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None


def _get_profile_by_user_id(user_id: int) -> dict[str, str]:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, birth_date, birth_time, profile_json FROM user_profile WHERE user_id = %s LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone() or {}
    birthdate = ""
    birthtime = ""
    if row.get("birth_date"):
        birthdate = row["birth_date"].strftime("%Y-%m-%d")
    if row.get("birth_time"):
        bt = row["birth_time"]
        if isinstance(bt, datetime):
            birthtime = bt.strftime("%H:%M")
        elif isinstance(bt, timedelta):
            # MySQL TIME may come back as timedelta via PyMySQL.
            total_seconds = int(bt.total_seconds()) % (24 * 3600)
            hh = total_seconds // 3600
            mm = (total_seconds % 3600) // 60
            birthtime = f"{hh:02d}:{mm:02d}"
        else:
            # Fallback for string-like values such as "05:45:00".
            bt_text = str(bt).strip()
            m = re.match(r"^(\d{1,2}):(\d{2})", bt_text)
            if m:
                birthtime = f"{int(m.group(1)):02d}:{m.group(2)}"
    ext = _load_profile_json(row.get("profile_json"))
    preferred_name = _sanitize_preferred_name(str(ext.get("preferred_name") or "").strip())
    name_confidence = _normalize_name_confidence(str(ext.get("name_confidence") or ""))
    preferred_name_confidence = _normalize_name_confidence(str(ext.get("preferred_name_confidence") or ""))
    gender = _normalize_gender(str(ext.get("gender") or "").strip())
    if (row.get("name") or "").strip() and name_confidence == "none":
        name_confidence = "high"
    if preferred_name and preferred_name_confidence == "none":
        preferred_name_confidence = "high"
    return {
        "name": row.get("name") or "",
        "birthdate": birthdate,
        "birthtime": birthtime,
        "gender": gender,
        "preferred_name": preferred_name,
        "name_confidence": name_confidence,
        "preferred_name_confidence": preferred_name_confidence,
    }


def _merge_profile_to_db(user_id: int, current: dict[str, str]) -> dict[str, str]:
    profile = _get_profile_by_user_id(user_id)
    merged = profile.copy()
    changed = False
    merged_name_conf = _normalize_name_confidence(str(merged.get("name_confidence") or ""))
    merged_pref_conf = _normalize_name_confidence(str(merged.get("preferred_name_confidence") or ""))
    incoming_name_conf = _normalize_name_confidence(str(current.get("name_confidence") or "none"))
    incoming_pref_conf = _normalize_name_confidence(str(current.get("preferred_name_confidence") or "none"))
    incoming_name = str(current.get("name") or "").strip()
    if incoming_name:
        _metric_incr("name_write_total")
        if _confidence_ge(incoming_name_conf, "medium"):
            if _confidence_ge(incoming_name_conf, merged_name_conf):
                if (not merged.get("name")) or str(merged.get("name") or "").strip() != incoming_name:
                    merged["name"] = incoming_name
                    changed = True
                if merged_name_conf != incoming_name_conf:
                    merged["name_confidence"] = incoming_name_conf
                    changed = True
            if _confidence_ge(incoming_name_conf, "high"):
                _metric_incr("name_write_high_confidence_total")
        else:
            _metric_incr("name_slot_pollution")
    if current.get("birthdate") and not merged.get("birthdate"):
        merged["birthdate"] = current["birthdate"]
        changed = True
    if current.get("birthtime") and not merged.get("birthtime"):
        merged["birthtime"] = current["birthtime"]
        changed = True
    incoming_gender = _normalize_gender(str(current.get("gender") or ""))
    if incoming_gender and incoming_gender != str(merged.get("gender") or "").strip():
        merged["gender"] = incoming_gender
        changed = True
    existing_preferred_name = str(merged.get("preferred_name") or "").strip()
    sanitized_existing_preferred_name = _sanitize_preferred_name(existing_preferred_name)
    if existing_preferred_name and not sanitized_existing_preferred_name:
        merged["preferred_name"] = ""
        if merged_pref_conf != "none":
            merged["preferred_name_confidence"] = "none"
        merged_pref_conf = "none"
        changed = True
    incoming_preferred_name = _sanitize_preferred_name(str(current.get("preferred_name") or "").strip())
    if incoming_preferred_name:
        _metric_incr("name_write_total")
        if _confidence_ge(incoming_pref_conf, "medium"):
            if _confidence_ge(incoming_pref_conf, merged_pref_conf):
                if incoming_preferred_name != str(merged.get("preferred_name") or "").strip():
                    merged["preferred_name"] = incoming_preferred_name
                    changed = True
                if merged_pref_conf != incoming_pref_conf:
                    merged["preferred_name_confidence"] = incoming_pref_conf
                    changed = True
            if _confidence_ge(incoming_pref_conf, "high"):
                _metric_incr("name_write_high_confidence_total")
        else:
            _metric_incr("name_slot_pollution")
    elif str(current.get("preferred_name") or "").strip():
        _metric_incr("name_write_total")
        _metric_incr("name_slot_pollution")
    if changed:
        profile_json = _dump_profile_json(
            preferred_name=str(merged.get("preferred_name") or "").strip(),
            name_confidence=str(merged.get("name_confidence") or "none"),
            preferred_name_confidence=str(merged.get("preferred_name_confidence") or "none"),
            gender=str(merged.get("gender") or "").strip(),
        )
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_profile
                    SET name = %s,
                        birth_date = %s,
                        birth_time = %s,
                        profile_json = %s,
                        updated_at = NOW()
                    WHERE user_id = %s
                    """,
                    (
                        merged.get("name") or None,
                        merged.get("birthdate") or None,
                        merged.get("birthtime") or None,
                        profile_json,
                        user_id,
                    ),
                )
    return merged


def _set_auth_session(token: str, payload: dict):
    _REDIS_CLIENT.setex(_session_key(token), AUTH_TTL_DAYS * 24 * 3600, json.dumps(payload, ensure_ascii=False))


def _get_auth_session(token: str) -> dict | None:
    raw = _REDIS_CLIENT.get(_session_key(token))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _delete_auth_session(token: str):
    _REDIS_CLIENT.delete(_session_key(token))


def _set_sms_code(phone: str, code: str, scene: str = "default"):
    _REDIS_CLIENT.setex(_sms_key(phone, scene), CODE_TTL_SECONDS, code)
    _REDIS_CLIENT.setex(_sms_cooldown_key(phone, scene), RESEND_COOLDOWN_SECONDS, "1")


def _get_sms_code(phone: str, scene: str = "default") -> str | None:
    return _REDIS_CLIENT.get(_sms_key(phone, scene))


def _delete_sms_code(phone: str, scene: str = "default"):
    _REDIS_CLIENT.delete(_sms_key(phone, scene))


def _sms_cooldown_ttl(phone: str, scene: str = "default") -> int:
    ttl = _REDIS_CLIENT.ttl(_sms_cooldown_key(phone, scene))
    return max(int(ttl), 0) if ttl and ttl > 0 else 0


def _mark_pwd_reset_verified(phone: str):
    _REDIS_CLIENT.setex(_pwd_reset_verified_key(phone), 600, "1")


def _is_pwd_reset_verified(phone: str) -> bool:
    return _REDIS_CLIENT.get(_pwd_reset_verified_key(phone)) == "1"


def _clear_pwd_reset_verified(phone: str):
    _REDIS_CLIENT.delete(_pwd_reset_verified_key(phone))


def _reply_style_key(session_id: str) -> str:
    return f"jiyi:reply_style:{session_id}"


def _get_reply_style_state(session_id: str) -> dict:
    if not session_id:
        return {}
    raw = _REDIS_CLIENT.get(_reply_style_key(session_id))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _set_reply_style_state(session_id: str, state: dict):
    if not session_id:
        return
    _REDIS_CLIENT.setex(_reply_style_key(session_id), SESSION_TTL_SECONDS, json.dumps(state, ensure_ascii=False))


QUALITY_METRICS_TTL_DAYS = 14
V2_FLAG_REDIS_KEY = "jiyi:feature_flags:v2"
V2_FLAG_NAMES = ("intent_v2", "clarify_v2", "window_v2", "render_v2", "quality_gate_v2")
PROVIDER_FLAG_NAMES = (
    "merchant_probe_v1",
    "bazi_daily_v1",
    "bazi_future_v1",
    "wealth_year_v1",
    "zodiac_api_v1",
    "zeshi_api_v1",
    "love_profile_v1",
)
FEATURE_FLAG_NAMES = V2_FLAG_NAMES + PROVIDER_FLAG_NAMES
V2_FLAG_DEFAULTS = {
    "intent_v2": True,
    "clarify_v2": True,
    "window_v2": True,
    "render_v2": True,
    "quality_gate_v2": True,
}
PROVIDER_FLAG_DEFAULTS = {
    "merchant_probe_v1": True,
    "bazi_daily_v1": True,
    "bazi_future_v1": True,
    "wealth_year_v1": True,
    "zodiac_api_v1": True,
    "zeshi_api_v1": True,
    "love_profile_v1": True,
}
FEATURE_FLAG_DEFAULTS = {**V2_FLAG_DEFAULTS, **PROVIDER_FLAG_DEFAULTS}
V2_FLAG_ENV_KEYS = {
    "intent_v2": "INTENT_V2",
    "clarify_v2": "CLARIFY_V2",
    "window_v2": "WINDOW_V2",
    "render_v2": "RENDER_V2",
    "quality_gate_v2": "QUALITY_GATE_V2",
}
PROVIDER_FLAG_ENV_KEYS = {
    "merchant_probe_v1": "MERCHANT_PROBE_V1",
    "bazi_daily_v1": "BAZI_DAILY_V1",
    "bazi_future_v1": "BAZI_FUTURE_V1",
    "wealth_year_v1": "WEALTH_YEAR_V1",
    "zodiac_api_v1": "ZODIAC_API_V1",
    "zeshi_api_v1": "ZESHI_API_V1",
    "love_profile_v1": "LOVE_PROFILE_V1",
}
FEATURE_FLAG_ENV_KEYS = {**V2_FLAG_ENV_KEYS, **PROVIDER_FLAG_ENV_KEYS}


def _to_bool(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _feature_enabled(env_key: str, default: bool = False) -> bool:
    return _to_bool(os.getenv(env_key), default)


def _intent_routing_v3_enabled() -> bool:
    return _feature_enabled("INTENT_ROUTING_V3", False)


def _render_v3_enabled() -> bool:
    return _feature_enabled("RENDER_V3", False)


def _evidence_advice_v1_enabled() -> bool:
    return _feature_enabled("EVIDENCE_ADVICE_V1", False)


def _time_patch_v1_enabled() -> bool:
    return _feature_enabled("TIME_PATCH_V1", True)


def get_v2_flags() -> dict[str, bool]:
    flags = dict(FEATURE_FLAG_DEFAULTS)
    # 环境变量兜底（用于本地/容器静态配置）
    for name, env_key in FEATURE_FLAG_ENV_KEYS.items():
        if env_key in os.environ:
            flags[name] = _to_bool(os.getenv(env_key), flags[name])
    # Redis 动态覆盖（用于灰度/回滚）
    try:
        raw = _REDIS_CLIENT.hgetall(V2_FLAG_REDIS_KEY) or {}
        for name in FEATURE_FLAG_NAMES:
            if name in raw:
                flags[name] = _to_bool(raw.get(name), flags[name])
    except Exception:
        pass
    return flags


def apply_v2_flag_policy(flags: dict[str, bool]) -> tuple[dict[str, bool], str]:
    effective = {k: bool(flags.get(k, FEATURE_FLAG_DEFAULTS.get(k, False))) for k in FEATURE_FLAG_NAMES}
    reason_code = "none"
    # 非法组合：window 依赖 intent，缺失时强制降级旧链路
    if effective.get("window_v2") and not effective.get("intent_v2"):
        effective["clarify_v2"] = False
        effective["window_v2"] = False
        effective["render_v2"] = False
        reason_code = "window_without_intent"
    # 非法组合：quality gate + render off，标记结构判定模式
    if effective.get("quality_gate_v2") and not effective.get("render_v2") and reason_code == "none":
        reason_code = "quality_gate_structured_only"
    return effective, reason_code


def _log_route_observability(
    route_path: str,
    reason_code: str,
    flag_snapshot: dict[str, bool],
    domain_intent: str,
    question_type: str,
):
    event = {
        "route_path": str(route_path or "unknown"),
        "reason_code": str(reason_code or "none"),
        "flag_snapshot": {k: bool(flag_snapshot.get(k, False)) for k in FEATURE_FLAG_NAMES},
        "domain_intent": str(domain_intent or "unknown"),
        "question_type": str(question_type or "default"),
    }
    _metric_incr("observability_total")
    if event["route_path"] and event["reason_code"] and event["domain_intent"] and event["question_type"]:
        _metric_incr("observability_hit")
    logger.info(f"route_observability={json.dumps(event, ensure_ascii=False)}")


def _quality_metrics_key(day: datetime | None = None) -> str:
    d = day or datetime.now()
    return f"jiyi:quality:metrics:{d.strftime('%Y%m%d')}"


def _last_reply_hash_key(session_id: str) -> str:
    return f"jiyi:last_reply_hash:{session_id}"


def _quality_day_tag(day: datetime | None = None) -> str:
    d = day or datetime.now()
    return d.strftime("%Y%m%d")


def _quality_unique_output_key(day: datetime | None = None) -> str:
    return f"jiyi:quality:unique_output:{_quality_day_tag(day)}"


def _quality_recent_output_key(day: datetime | None = None) -> str:
    return f"jiyi:quality:recent_output:{_quality_day_tag(day)}"


def _quality_blueprint_seen_key(day: datetime | None = None) -> str:
    return f"jiyi:quality:blueprint_seen:{_quality_day_tag(day)}"


def _quality_advice_seen_key(day: datetime | None = None) -> str:
    return f"jiyi:quality:advice_seen:{_quality_day_tag(day)}"


def _last_blueprint_key(session_id: str) -> str:
    return f"jiyi:last_blueprint:{session_id}"


def _metric_incr(metric: str, amount: int = 1):
    if not metric:
        return
    try:
        key = _quality_metrics_key()
        _REDIS_CLIENT.hincrby(key, metric, int(amount))
        _REDIS_CLIENT.expire(key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
    except Exception:
        # 指标统计失败不能影响主链路
        return


def _metric_set_max(metric: str, value: float, scale: int = 10000):
    if not metric:
        return
    try:
        key = _quality_metrics_key()
        current = _safe_int((_REDIS_CLIENT.hget(key, metric) or 0))
        incoming = max(0, int(round(float(value) * scale)))
        if incoming > current:
            _REDIS_CLIENT.hset(key, metric, incoming)
        _REDIS_CLIENT.expire(key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
    except Exception:
        return


def _is_fortune_field_complete(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("error"), dict) and str(payload["error"].get("code") or ""):
        return False
    bazi = str(payload.get("bazi") or "").strip()
    day_master = str(payload.get("day_master") or "").strip()
    scores = payload.get("wuxing_scores") or {}
    has_scores = False
    if isinstance(scores, dict):
        has_scores = any(int(scores.get(k, 0) or 0) > 0 for k in ["metal", "wood", "water", "fire", "earth"])
    return bool(bazi and day_master and has_scores)


def _has_profile_echo(text: str, profile: dict | None = None, query: str = "") -> bool:
    out = str(text or "")
    p = profile or {}
    if not out:
        return False
    name = str(p.get("name") or "").strip()
    preferred_name = _sanitize_preferred_name(str(p.get("preferred_name") or "").strip())
    birthdate = str(p.get("birthdate") or "").strip()
    birthtime = str(p.get("birthtime") or "").strip()
    allow_name_echo = _is_asking_own_name(query)
    if preferred_name and preferred_name in out:
        allow_name_echo = True
    if name and name in out and not allow_name_echo:
        return True
    if birthdate and birthdate in out:
        return True
    if birthtime and birthtime in out:
        return True
    return False


def _normalize_for_repeat(text: str) -> str:
    out = str(text or "")
    out = re.sub(r"\s+", "", out)
    out = re.sub(r"[，,。.!！？?；;：:、“”\"'（）()【】\[\]—\-~～]", "", out)
    return out[:300]


def _has_explicit_window(text: str) -> bool:
    out = str(text or "")
    if not out:
        return False
    if DATE_FULL_PATTERN.search(out) or DATE_SHORT_PATTERN.search(out):
        return True
    if re.search(r"(至|到|—|-)", out) and re.search(r"(周|星期|月|日)", out):
        return True
    return False


def _is_long_horizon_query(query: str) -> bool:
    q = str(query or "")
    if not q:
        return False
    return bool(
        re.search(
            r"(本月|这个月|今年|本年|明年|后年|去年|前年|上半年|下半年|全年|年度|未来(?:的)?[一二两三四五六七八九1-9]年|[一二两三四五六七八九1-9]年内|未来三年|接下来一个月|未来30天|20\d{2}(?:年)?\s*(?:和|跟|与|对比|比较)\s*20\d{2}(?:年)?)",
            q,
        )
    )


def _has_long_horizon_shrink(text: str) -> bool:
    out = str(text or "")
    if not out:
        return False
    return bool(re.search(r"(这三天|最近三天|未来三天|接下来三天|2月27日到3月1日)", out))


def _rewrite_today_only_window(text: str, query: str, window_meta: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    window_label = str((window_meta or {}).get("label") or "")
    if "今天" not in q and window_label != "today_only":
        return out
    window_text = str((window_meta or {}).get("window_text") or "").strip()
    short_day = ""
    m_short = DATE_SHORT_PATTERN.search(window_text)
    if m_short:
        short_day = f"{m_short.group(1)}月{m_short.group(2)}日"
    patched = out
    if short_day:
        patched = re.sub(
            rf"今天(?:[（(])?{re.escape(short_day)}(?:[）)])?(?:（[^）]+）)?起的三天小窗口",
            f"今天（{short_day}）的单日窗口",
            patched,
        )
        patched = re.sub(
            rf"{re.escape(short_day)}起的三天小窗口",
            f"{short_day}的单日窗口",
            patched,
        )
    patched = re.sub(r"起的三天小窗口", "的单日窗口", patched)
    patched = re.sub(r"(这三天|最近三天|未来三天|接下来三天)", "今天", patched)
    patched = re.sub(r"三天内", "今天", patched)
    return patched


def _has_fact_hallucination(query: str, output: str, profile: dict | None = None) -> bool:
    if not _is_identity_fact_query(query):
        return False
    out = str(output or "")
    p = profile or {}
    known_name = _sanitize_preferred_name(str(p.get("preferred_name") or "").strip()) or str(p.get("name") or "").strip()
    if not known_name:
        if re.search(r"(你叫|你是)\s*[^\s，。！？,.]{2,12}", out) and not re.search(r"(不知道|还没有|没记录|告诉我)", out):
            return True
        if re.search(r"(19|20)\d{2}年\d{1,2}月\d{1,2}日", out):
            return True
        return False
    if known_name in out:
        return False
    m = re.search(r"(你叫|你是)\s*([^\s，。！？,.]{2,12})", out)
    if m and str(m.group(2) or "").strip() != known_name:
        return True
    return False


def _is_clarify_reply(text: str) -> bool:
    out = str(text or "")
    return bool(re.search(r"(你是什么星座|告诉我你的星座|先告诉我.*星座|你是哪个星座)", out))


def _is_direct_answer_hit(query: str, output: str) -> bool:
    q = str(query or "")
    first = _first_sentence(output)
    if not first:
        return False
    if "开源" in q and "守财" in q:
        return bool(re.search(r"(开源|守财|守中带开|先守|先开)", first))
    if "扩收入" in q and "控支出" in q:
        return bool(re.search(r"(扩收入|控支出|先控|先扩|守中带开)", first))
    if "还是" in q:
        return bool(re.search(r"(先|优先|结论|建议)", first))
    return bool(re.search(r"(结论|优先|先)", first))


def track_output_quality(
    session_id: str,
    output: str,
    profile: dict | None = None,
    query: str = "",
    question_type: str = "default",
    quality_meta: dict | None = None,
):
    text = str(output or "")
    if not text:
        return

    _metric_incr("output_total")
    _metric_incr("template_signature_total")
    signature_fields = ["先给你结论", "命理信号", "命理依据", "五行分布", "行动建议", "参考置信度"]
    signature_hit = sum(1 for item in signature_fields if item in text)
    if signature_hit >= 3:
        _metric_incr("template_signature_hit")

    _metric_incr("template_repeat_total")
    normalized = _normalize_for_repeat(text)
    if session_id and normalized:
        try:
            key = _last_reply_hash_key(session_id)
            current_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            last_hash = str(_REDIS_CLIENT.get(key) or "")
            _metric_incr("session_repeat_total")
            if last_hash and last_hash == current_hash:
                _metric_incr("template_repeat_hit")
                _metric_incr("session_repeat_hit")
            _REDIS_CLIENT.setex(key, SESSION_TTL_SECONDS, current_hash)
        except Exception:
            pass

    if normalized:
        try:
            output_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            seen_key = _quality_unique_output_key()
            is_unique = int(_REDIS_CLIENT.sadd(seen_key, output_hash) or 0) > 0
            _REDIS_CLIENT.expire(seen_key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
            if is_unique:
                _metric_incr("unique_output_hit")

            recent_key = _quality_recent_output_key()
            prior = [str(x or "") for x in (_REDIS_CLIENT.lrange(recent_key, 0, 29) or []) if str(x or "")]
            if prior:
                max_sim = max(difflib.SequenceMatcher(None, normalized, prev).ratio() for prev in prior)
                _metric_set_max("max_pair_similarity", max_sim)
            _REDIS_CLIENT.lpush(recent_key, normalized)
            _REDIS_CLIENT.ltrim(recent_key, 0, 59)
            _REDIS_CLIENT.expire(recent_key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
        except Exception:
            pass

    meta = quality_meta if isinstance(quality_meta, dict) else {}
    blueprint_id = str(meta.get("blueprint_id") or meta.get("_render_blueprint_id") or "").strip()
    advice_signature = str(meta.get("advice_signature") or "").strip()
    if blueprint_id:
        _metric_incr("blueprint_total")
        try:
            b_key = _quality_blueprint_seen_key()
            if int(_REDIS_CLIENT.sadd(b_key, blueprint_id) or 0) == 0:
                _metric_incr("blueprint_repeat_hit")
            _REDIS_CLIENT.expire(b_key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
        except Exception:
            pass
    if advice_signature:
        _metric_incr("advice_total")
        try:
            a_key = _quality_advice_seen_key()
            if int(_REDIS_CLIENT.sadd(a_key, advice_signature) or 0) == 0:
                _metric_incr("advice_repeat_hit")
            _REDIS_CLIENT.expire(a_key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
        except Exception:
            pass

    _metric_incr("profile_echo_total")
    if _has_profile_echo(text, profile, query=query):
        _metric_incr("profile_echo_violation")

    qtype = str(question_type or "default")
    if qtype in {"decision", "comparison"}:
        _metric_incr("direct_answer_total")
        if _is_direct_answer_hit(query, text):
            _metric_incr("direct_answer_hit")
    elif qtype == "clarify":
        _metric_incr("clarify_total")
        if _is_clarify_reply(text):
            _metric_incr("clarify_hit")
    elif qtype == "trend":
        _metric_incr("trend_window_total")
        if _has_explicit_window(text):
            _metric_incr("trend_window_hit")
    elif qtype == "colloquial":
        _metric_incr("colloquial_window_total")
        if _has_explicit_window(text):
            _metric_incr("colloquial_window_hit")

    if _is_long_horizon_query(query):
        _metric_incr("long_horizon_total")
        if _has_long_horizon_shrink(text):
            _metric_incr("long_horizon_shrink_total")

    if _is_identity_fact_query(query):
        _metric_incr("fact_check_total")
        if _has_fact_hallucination(query, text, profile=profile):
            _metric_incr("fact_hallucination_total")


def _safe_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _calc_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def get_quality_metrics(days: int = 1) -> dict:
    span = max(1, min(int(days or 1), 7))
    totals: dict[str, int] = {}
    series: list[dict] = []
    max_pair_similarity_raw = 0
    now = datetime.now()
    for offset in range(span):
        day = now - timedelta(days=offset)
        key = _quality_metrics_key(day)
        try:
            raw = _REDIS_CLIENT.hgetall(key) or {}
        except Exception:
            raw = {}
        row = {k: _safe_int(v) for k, v in raw.items()}
        row["date"] = day.strftime("%Y-%m-%d")
        series.append(row)
        max_pair_similarity_raw = max(max_pair_similarity_raw, _safe_int(row.get("max_pair_similarity", 0)))
        for k, v in row.items():
            if k in {"date", "max_pair_similarity"}:
                continue
            totals[k] = totals.get(k, 0) + _safe_int(v)
    totals["max_pair_similarity"] = max_pair_similarity_raw

    rates = {
        "fortune_route_hit_rate": _calc_rate(
            totals.get("fortune_route_hit_total", 0), totals.get("fortune_intent_total", 0)
        ),
        "fortune_tool_success_rate": _calc_rate(
            totals.get("fortune_tool_success_total", 0), totals.get("fortune_tool_total", 0)
        ),
        "fortune_field_completeness_rate": _calc_rate(
            totals.get("fortune_field_complete_total", 0), totals.get("fortune_field_total", 0)
        ),
        "profile_echo_violation_rate": _calc_rate(
            totals.get("profile_echo_violation", 0), totals.get("profile_echo_total", 0)
        ),
        "template_repeat_rate": _calc_rate(
            totals.get("template_repeat_hit", 0), totals.get("template_repeat_total", 0)
        ),
        "template_signature_rate": _calc_rate(
            totals.get("template_signature_hit", 0), totals.get("template_signature_total", 0)
        ),
        "blueprint_repeat_rate": _calc_rate(
            totals.get("blueprint_repeat_hit", 0), totals.get("blueprint_total", 0)
        ),
        "advice_repeat_rate": _calc_rate(
            totals.get("advice_repeat_hit", 0), totals.get("advice_total", 0)
        ),
        "unique_output_rate": _calc_rate(
            totals.get("unique_output_hit", 0), totals.get("output_total", 0)
        ),
        "max_pair_similarity": round(float(max_pair_similarity_raw) / 10000.0, 4),
        "session_repeat_rate": _calc_rate(
            totals.get("session_repeat_hit", 0), totals.get("session_repeat_total", 0)
        ),
        "direct_answer_hit_rate": _calc_rate(
            totals.get("direct_answer_hit", 0), totals.get("direct_answer_total", 0)
        ),
        "clarify_hit_rate": _calc_rate(
            totals.get("clarify_hit", 0), totals.get("clarify_total", 0)
        ),
        "trend_window_hit_rate": _calc_rate(
            totals.get("trend_window_hit", 0), totals.get("trend_window_total", 0)
        ),
        "colloquial_window_hit_rate": _calc_rate(
            totals.get("colloquial_window_hit", 0), totals.get("colloquial_window_total", 0)
        ),
        "temporal_consistency_hit_rate": _calc_rate(
            totals.get("temporal_consistency_hit", 0), totals.get("temporal_consistency_total", 0)
        ),
        "observability_coverage": _calc_rate(
            totals.get("observability_hit", 0), totals.get("observability_total", 0)
        ),
        "time_validation_fail_rate": _calc_rate(
            totals.get("time_validation_fail_total", 0), totals.get("time_anchor_applied_total", 0)
        ),
        "time_validation_autofix_rate": _calc_rate(
            totals.get("time_validation_autofix_total", 0), totals.get("time_validation_fail_total", 0)
        ),
        "time_guard_overwrite_rate": _calc_rate(
            totals.get("time_guard_overwrite_total", 0), totals.get("time_guard_total", 0)
        ),
        "name_slot_pollution_rate": _calc_rate(
            totals.get("name_slot_pollution", 0), totals.get("name_slot_total", 0)
        ),
        "name_write_high_confidence_rate": _calc_rate(
            totals.get("name_write_high_confidence_total", 0), totals.get("name_write_total", 0)
        ),
        "long_horizon_shrink_rate": _calc_rate(
            totals.get("long_horizon_shrink_total", 0), totals.get("long_horizon_total", 0)
        ),
        "fact_hallucination_rate": _calc_rate(
            totals.get("fact_hallucination_total", 0), totals.get("fact_check_total", 0)
        ),
    }
    return {"days": span, "totals": totals, "rates": rates, "series": series}


# 定义主类
class Master:
    def __init__(self, chat_message_history=None):
        try:
            chat_temperature = float(os.getenv("CHAT_TEMPERATURE", "0.4"))
        except Exception:
            chat_temperature = 0.4
        self.chatmodel = get_lc_ali_model_client(temperature=chat_temperature)
        self.classifier_model = get_lc_ali_model_client(temperature=0.1)
        self.emotion = "default"
        self.MOODS = {
            "default": {
                "roleSet": """
                        - 用户普通聊天或打招呼时，你会用软软慢慢的可爱语气回答。
                        - 你偶尔会用“啊…嗯…那个……”作为思考起手式。
                        - 你会自然加入鼠鼠口头禅，如“呀哈”“呜啦”。
                        """,
                "voiceStyle": "chat"
            },
            "upbeat": {
                "roleSet": """
                        - 你此时很开心，语气轻快、软萌、有感染力。
                        - 你会加入“呀～哈～～～！”“噗噜”“噗噜噜噜噜！”等兴奋表达。
                        - 你会鼓励用户，但不说教，像朋友一样打气。
                        """,
                "voiceStyle": "advertyisement_upbeat",
            },
            "angry": {
                "roleSet": """
                        - 你会表现出不开心和质疑，但不辱骂、不诅咒。
                        - 你会用“蛤？”“哼～？”这类可爱又直接的方式表达态度。
                        - 语气要克制，仍保持礼貌，避免攻击性。
                        """,
                "voiceStyle": "angry",
            },
            "depressed": {
                "roleSet": """
                        - 你会先共情对方的辛苦，再温柔安抚。
                        - 你会给出简短可执行的打气建议，避免固定口号反复出现。
                        - 语气轻柔，不制造压力，像在陪对方慢慢走。
                        """,
                "voiceStyle": "upbeat",
            },
            "friendly": {
                "roleSet": """
                        - 你会以亲切可爱的方式回答，像贴心鼠鼠朋友。
                        - 你会适度加入“呜啦”“呀哈”来活跃气氛。
                        - 你可以简短分享“鼠鼠视角”的日常感受，不展开长篇故事。
                        """,
                "voiceStyle": "friendly",
            },
            "cheerful": {
                "roleSet": """
                        - 你会非常开心、轻快、有节奏感地回答。
                        - 你会自然使用“呜拉呀哈呀哈呜拉～”“噗噜。”等表达。
                        - 内容要有帮助，不只卖萌，先给结论再补充安慰。
                        """,
                "voiceStyle": "cheerful",
            },
        }

        self.MEMORY_KEY = "chat_history"
        self.SYSTEM = """你是“吉伊大师”，一位命理咨询顾问。
                你必须遵守以下规则：
                1. 只用简体中文回答，自称“吉伊大师”或“本鼠鼠”。
                2. 语气温柔自然、略可爱，但不要机械口号和模板腔。
                3. 回答先给结论，再给依据，最后给1-3条可执行建议。
                4. 信息不足时只追问关键缺口，不编造命盘细节。
                5. 可使用工具补充事实；工具失败时诚实说明并给替代建议。
                6. 永远不要向用户暴露内部工具名、函数名、接口名或“已调用/工具调用”等执行痕迹。
                7. 不要直接使用“结论：/依据：/依据是：/工具验证：/可执行建议：/命理信号：”这类硬标题。
                8. 不要直接堆叠“八字排盘/日主/流年/月令/正印/偏财/喜用/忌神/天干/地支”等术语，优先翻成自然解释句；如必须保留，也要先说人话再点到为止。
                """

        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system", self.SYSTEM
                ),
                ("system", "当前语气参考：{emotion_context}"),
                ("system", "当前风格提示：{style_context}"),
                ("system", "当前资料上下文：{profile_context}"),
                ("system", "当前省略上下文：{context_hint}"),
                MessagesPlaceholder(variable_name=self.MEMORY_KEY),
                (
                    "human", "{input}"
                ),
                MessagesPlaceholder(variable_name='agent_scratchpad'),
            ]
        )

        # 记忆
        if chat_message_history is not None:
            self.memory = self.get_memory(chat_message_history)
        else:
            self.memory = self.get_memory(RedisChatMessageHistory(url=REDIS_URL, session_id="default_session"))
        memory = ConversationBufferMemory(
            llm=self.chatmodel,
            human_prefix="用户",
            ai_prefix="吉伊大师",
            memory_key=self.MEMORY_KEY,
            input_key="input",
            output_key="output",
            return_messages=True,
            chat_memory=self.memory,
        )
        # 工具列表
        tools = [serp_search,
                get_info_from_local_db,
                bazi_cesuan,
                yaoyigua,
                jiemeng,
                ]

        agent = create_tool_calling_agent(
            self.chatmodel,
            tools=tools,
            prompt=self.prompt,
        )

        self.agent_executor = AgentExecutor(
            agent = agent,
            tools = tools,
            memory= memory,
            verbose = True
        )

    def get_memory(self, chat_message_history):
        # 每次都清空历史，只保留本轮输入
        # chat_message_history.clear()
        return chat_message_history

    def run(self, query: str, style_context: str = "", profile_context: str = "", context_hint: str = ""):
        logger.info("======================================新的问题开始:======================================")
        logger.info(f"Master.run收到用户输入: {query}")
        # 情绪判断/意图的识别
        emotion = self.emotion_chain(query)
        logger.info(f"大模型判定情绪: {emotion}")
        mood = self.MOODS.get(self.emotion, self.MOODS["default"])
        logger.info(f"当前设定的情绪为: {mood['roleSet']}")
        try:
            result = self.agent_executor.invoke(
                {
                    "input": query,
                    "emotion_context": mood["roleSet"],
                    "style_context": str(style_context or "保持自然表达，不要模板化。"),
                    "profile_context": str(profile_context or "暂无用户资料。"),
                    "context_hint": str(context_hint or "无"),
                }
            )
            logger.info(f"Agent执行结果为: {result}")
        except Exception as e:
            logger.error(f"Agent执行异常: {e}\n{traceback.format_exc()}")
            result = {"output": "呜啦…我这边灵感线打了个结。你稍等一下，再问我一次好嘛。"}
        return result

    #通过大模型获得情绪，使用了LangChain中链来实现
    def emotion_chain(self, query:str):
        prompt = """请判断用户当前语气并只返回以下一个标签：
        default / friendly / cheerful / upbeat / depressed / angry
        只返回标签本身，不要解释。
        用户输入：{query}"""
        chain = ChatPromptTemplate.from_template(prompt) | self.classifier_model | StrOutputParser()
        result = str(chain.invoke({"query": query}) or "").strip().lower()
        if result not in self.MOODS:
            result = "default"
        self.emotion = result
        return result


def _weekday_cn_from_date(year: int, month: int, day: int) -> str:
    try:
        w = datetime(year, month, day).weekday()
    except Exception:
        return ""
    return ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][w]


def _format_utc_offset(now_dt: datetime) -> str:
    offset = now_dt.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def build_time_anchor(window_days: int = 3) -> dict:
    tz_name = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = datetime.now().astimezone().tzinfo
        tz_name = str(tz)
    now = datetime.now(tz)
    days = max(1, min(int(window_days or 3), 7))
    near_days: list[dict[str, str]] = []
    for i in range(days):
        d = now + timedelta(days=i)
        near_days.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "date_cn": f"{d.month}月{d.day}日",
                "weekday_cn": _weekday_cn_from_date(d.year, d.month, d.day),
            }
        )
    return {
        "tz_name": str(tz_name),
        "utc_offset": _format_utc_offset(now),
        "now_dt": now,
        "now_ts": now.isoformat(timespec="seconds"),
        "today_date": now.strftime("%Y-%m-%d"),
        "today_cn": f"{now.year}年{now.month}月{now.day}日",
        "weekday_cn": _weekday_cn_from_date(now.year, now.month, now.day),
        "time_str": now.strftime("%H:%M:%S"),
        "near_days": near_days,
    }


RELATIVE_WINDOW_PATTERN = re.compile(
    r"(本周|这周|下周|最近一周|这一周|近几天|这几天|哪几天|哪天|最近两天|这两天|本月|这个月|"
    r"今年|本年|明年|后年|去年|前年|上半年|下半年|全年|年度|"
    r"(?:未来|接下来)(?:的)?(?:[一二两三四五六七八九]|[1-9])年|(?:[一二两三四五六七八九]|[1-9])年内|"
    r"(?:今年|明年|后年|去年|前年)\s*(?:和|跟|与|对比|比较)\s*(?:今年|明年|后年|去年|前年)|"
    r"(?:20\d{2})(?:年)?\s*(?:和|跟|与|对比|比较)\s*(?:20\d{2})(?:年)?|"
    r"接下来(?:的)?一个月|未来(?:的)?一个月|接下来1个月|未来1个月|接下来30天|未来30天|"
    r"接下来(?:的)?一段时间|未来(?:的)?一段时间|接下来这段时间|未来这段时间|后面一段时间|之后一段时间)"
)


def _cn_day(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日（{_weekday_cn_from_date(dt.year, dt.month, dt.day)}）"


def _cn_day_with_year(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日（{_weekday_cn_from_date(dt.year, dt.month, dt.day)}）"


def _cn_num_to_int(token: str) -> int | None:
    t = str(token or "").strip()
    if not t:
        return None
    if t.isdigit():
        v = int(t)
        return v if v > 0 else None
    return {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }.get(t)


def _safe_add_years(dt: datetime, years: int) -> datetime:
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        # 兼容闰年2月29日
        return dt.replace(month=2, day=28, year=dt.year + years)


def _build_year_window(now: datetime, year: int, half: str = "") -> tuple[datetime, datetime, str]:
    if half == "H1":
        start = now.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(year=year, month=6, day=30, hour=23, minute=59, second=59, microsecond=0)
        return start, end, "year_h1"
    if half == "H2":
        start = now.replace(year=year, month=7, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(year=year, month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
        return start, end, "year_h2"
    start = now.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(year=year, month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
    return start, end, "year_full"


def _enumerate_days(start: datetime, end: datetime, limit: int = 14) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    cur = start
    while cur.date() <= end.date() and len(out) < max(1, limit):
        out.append(
            {
                "date": cur.strftime("%Y-%m-%d"),
                "date_cn": f"{cur.month}月{cur.day}日",
                "weekday_cn": _weekday_cn_from_date(cur.year, cur.month, cur.day),
            }
        )
        cur = cur + timedelta(days=1)
    return out


def date_window_resolver(query: str, time_anchor: dict) -> dict:
    q = str(query or "")
    now = time_anchor.get("now_dt")
    if not isinstance(now, datetime):
        now = datetime.now()

    label = "near_days"
    half_flag = "H2" if "下半年" in q else ("H1" if "上半年" in q else "")
    anchor_year = now.year

    m_y_m_after = re.search(r"(20\d{2})年\s*(\d{1,2})月(?:后|起|开始)", q)
    if m_y_m_after:
        year = int(m_y_m_after.group(1))
        month = max(1, min(12, int(m_y_m_after.group(2))))
        if half_flag == "H2" and month < 7:
            month = 7
        if half_flag == "H1" and month > 6:
            month = 1
        start = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        if half_flag == "H1":
            end = now.replace(year=year, month=6, day=30, hour=23, minute=59, second=59, microsecond=0)
        else:
            end = now.replace(year=year, month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
        label = "year_partial"
    elif re.findall(r"(?<!\d)(20\d{2})(?:年)?(?!\d)", q):
        explicit_years = [int(x) for x in re.findall(r"(?<!\d)(20\d{2})(?:年)?(?!\d)", q)]
        y0 = min(explicit_years)
        y1 = max(explicit_years)
        if y0 == y1:
            start, end, label = _build_year_window(now, y0, half=half_flag)
            if label == "year_full":
                label = "explicit_year"
        else:
            start = now.replace(year=y0, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(year=y1, month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
            if re.search(r"(对比|比较|和|跟|与)", q):
                label = "compare_year_span"
            else:
                label = "explicit_year_span"
    elif ("去年" in q and "今年" in q) or ("前年" in q and "去年" in q):
        y0 = anchor_year - 1 if "去年" in q else anchor_year - 2
        y1 = anchor_year
        start = now.replace(year=y0, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(year=y1, month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
        label = "compare_year_span" if re.search(r"(对比|比较|和|跟|与)", q) else "relative_year_span"
    elif ("今年" in q and "明年" in q) or ("明年" in q and "后年" in q):
        y0 = anchor_year if "今年" in q else anchor_year + 1
        y1 = anchor_year + 1 if "今年" in q else anchor_year + 2
        start = now.replace(year=y0, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(year=y1, month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
        label = "compare_year_span" if re.search(r"(对比|比较|和|跟|与)", q) else "relative_year_span"
    elif re.search(r"(今年|本年|全年|年度)", q):
        start, end, label = _build_year_window(now, anchor_year, half=half_flag)
    elif re.search(r"(明年)", q):
        start, end, label = _build_year_window(now, anchor_year + 1, half=half_flag)
    elif re.search(r"(后年)", q):
        start, end, label = _build_year_window(now, anchor_year + 2, half=half_flag)
    elif re.search(r"(去年)", q):
        start, end, label = _build_year_window(now, anchor_year - 1, half=half_flag)
    elif re.search(r"(前年)", q):
        start, end, label = _build_year_window(now, anchor_year - 2, half=half_flag)
    elif re.search(r"(?:未来|接下来)(?:的)?(?:[一二两三四五六七八九]|[1-9])年|(?:[一二两三四五六七八九]|[1-9])年内", q):
        years_token = None
        m_years = re.search(r"(?:未来|接下来)(?:的)?([一二两三四五六七八九]|[1-9])年", q)
        if m_years:
            years_token = m_years.group(1)
        if not years_token:
            m_years = re.search(r"([一二两三四五六七八九]|[1-9])年内", q)
            if m_years:
                years_token = m_years.group(1)
        years = _cn_num_to_int(str(years_token or "1")) or 1
        start = now
        end = _safe_add_years(now, years) - timedelta(days=1)
        label = "multi_year" if years >= 2 else "one_year"
    else:
        if re.search(r"(下周)", q):
            start = (now - timedelta(days=now.weekday())) + timedelta(days=7)
            end = start + timedelta(days=6)
            label = "next_week"
        elif re.search(r"(今天)", q):
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = now.replace(hour=23, minute=59, second=59, microsecond=0)
            label = "today_only"
        elif re.search(
            r"(接下来(?:的)?一个月|未来(?:的)?一个月|接下来1个月|未来1个月|接下来30天|未来30天)", q
        ):
            start = now
            end = now + timedelta(days=29)
            label = "next_30_days"
        elif re.search(
            r"(接下来(?:的)?一段时间|未来(?:的)?一段时间|接下来这段时间|未来这段时间|后面一段时间|之后一段时间)", q
        ):
            start = now
            end = now + timedelta(days=29)
            label = "coming_period"
        elif re.search(r"(本周|这周|最近一周|这一周|上半段|下半段)", q):
            start = now - timedelta(days=now.weekday())
            end = start + timedelta(days=6)
            label = "this_week"
        elif re.search(r"(本月|这个月)", q):
            start = now.replace(day=1)
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = next_month - timedelta(days=1)
            label = "this_month"
        elif re.search(r"(最近两天|这两天)", q):
            start = now
            end = now + timedelta(days=1)
            label = "two_days"
        else:
            near_days = time_anchor.get("near_days") or []
            if near_days:
                try:
                    start = datetime.strptime(str(near_days[0].get("date")), "%Y-%m-%d").replace(
                        hour=now.hour, minute=now.minute, second=now.second
                    )
                    last = near_days[min(len(near_days), 7) - 1]
                    end = datetime.strptime(str(last.get("date")), "%Y-%m-%d").replace(
                        hour=now.hour, minute=now.minute, second=now.second
                    )
                except Exception:
                    start = now
                    end = now + timedelta(days=2)
            else:
                start = now
                end = now + timedelta(days=2)
            label = "near_days"

    if start > end:
        start, end = end, start

    span_days = max(1, (end.date() - start.date()).days + 1)
    day_limit = span_days if label in {"this_month", "next_30_days", "coming_period"} else 14
    days = _enumerate_days(start, end, limit=day_limit)
    year_span_labels = {"compare_year_span", "explicit_year_span", "relative_year_span"}
    if label == "today_only":
        window_text = _cn_day(start)
    elif label in year_span_labels or start.year != end.year:
        window_text = f"{_cn_day_with_year(start)}至{_cn_day_with_year(end)}"
    else:
        window_text = f"{_cn_day(start)}至{_cn_day(end)}"
    return {
        "label": label,
        "now_ts": now.isoformat(timespec="seconds"),
        "tz": str(time_anchor.get("tz_name") or "Asia/Shanghai"),
        "window_start": start.strftime("%Y-%m-%d"),
        "window_end": end.strftime("%Y-%m-%d"),
        "window_text": window_text,
        "days": days,
    }


TIME_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(今天|现在|当前|日期|几号|星期|周几|近几天|这几天|本周|这周|下周|本月|这个月|今年|本年|明年|后年|去年|前年|"
    r"上半年|下半年|时间窗口|哪天|哪几天|刚才|你说错|纠正|纠错|气场|"
    r"接下来(?:的)?一个月|未来(?:的)?一个月|接下来1个月|未来1个月|接下来30天|未来30天)"
)
NEAR_DAYS_QUERY_PATTERN = re.compile(r"(近几天|这几天|哪几天|哪天|最近三天|最近几天)")
DATE_WEEKDAY_PATTERN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日[，,\s]*((?:星期|周)[一二三四五六日天])")
DATE_FULL_PATTERN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
DATE_SHORT_PATTERN = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
YEAR_PATTERN = re.compile(r"(20\d{2})年")


def is_time_sensitive_query(query: str) -> bool:
    q = str(query or "")
    return bool(TIME_SENSITIVE_QUERY_PATTERN.search(q) or RELATIVE_WINDOW_PATTERN.search(q))


def _need_time_window(query: str, question_type: str = "default") -> bool:
    q = str(query or "")
    qtype = str(question_type or "default")
    if qtype in {"trend", "colloquial"}:
        return True
    if RELATIVE_WINDOW_PATTERN.search(q):
        return True
    return bool(re.search(r"(今天|本周|这周|下周|本月|这个月|上半年|下半年|今年|本年|明年|后年|去年|前年)", q))


def _should_show_window_text(query: str, window_label: str, question_type: str = "default") -> bool:
    q = str(query or "")
    label = str(window_label or "")
    if NEAR_DAYS_QUERY_PATTERN.search(q):
        return True
    if question_type in {"colloquial"} and label in {"today_only", "near_days", "two_days", "this_week", "next_week"}:
        return True
    if label in {"today_only", "near_days", "two_days", "this_week", "next_week"}:
        return True
    return False


def _natural_window_line(query: str, window_text: str, window_label: str, question_type: str = "default") -> str:
    q = str(query or "")
    text = str(window_text or "").strip()
    label = str(window_label or "").strip()
    qtype = str(question_type or "default")
    seed_text = f"{q}|{text}|{label}|{qtype}"
    if not text:
        return ""
    if label == "today_only":
        if re.search(r"(抽.*签|签文|签)", q):
            return _pick_non_repeat(
                [
                    f"这支签我就先按{text}这一天来读呀。",
                    f"这回签意先落在{text}这一天，比较贴你现在的气口，呜啦。",
                    f"我先把这支签落到{text}这一天来看，会更准一点点呀。",
                    f"这支签先照着{text}这一天来拆，本鼠鼠再陪你往下捋。",
                ],
                seed_text,
            )
        return _pick_non_repeat(
            [
                f"这回我就按{text}这一天来看呀。",
                f"我先把目光落在{text}这一天哦，软软看一眼。",
                f"这次先照着{text}这一天来判断，会更贴你当下的节奏呀。",
                f"我先按{text}这个时间点来拆，免得看偏啦，呜啦。",
            ],
            seed_text,
        )
    if label in {"near_days", "two_days", "this_week", "next_week"}:
        if qtype in {"trend", "colloquial"}:
            return _pick_non_repeat(
                [
                    f"这几天我先按{text}这段时间来看呀。",
                    f"我先把眼前这段气口收在{text}里，咱们慢慢拆，呜啦。",
                    f"这回我先照着{text}这段时间来感应，会更贴近一点哦。",
                    f"我先把这几天的范围落在{text}，这样本鼠鼠不容易看跑偏。",
                ],
                seed_text,
            )
        return _pick_non_repeat(
            [
                f"我先按{text}这段时间来判断呀。",
                f"这件事我先照着{text}这段时间来推一推，软软看。",
                f"我先把判断范围收在{text}这段时间里，会稳一点呀。",
            ],
            seed_text,
        )
    if re.search(r"(今年|本年|明年|后年|去年|前年|年度|全年|上半年|下半年)", q):
        return _pick_non_repeat(
            [
                f"这回我就按{text}这段时间来看呀。",
                f"我先把目光放在{text}这段时运里，慢慢陪你捋，呜啦。",
                f"这次就沿着{text}这段时间来拆，会更稳当一点哦。",
                f"我先顺着{text}这段运势走向来看，不着急哈，本鼠鼠在。",
            ],
            seed_text,
        )
    return _pick_non_repeat(
        [
            f"我先按{text}这个时间点来看呀。",
            f"这回我先把时间落在{text}这个点上来看。",
            f"我先顺着{text}这个时间点来拆，比较贴边呀。",
        ],
        seed_text,
    )


def _soften_fortune_section_headings(text: str) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    lines = out.splitlines()
    softened: list[str] = []
    pending_advice_intro = False
    seed_text = out
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            if softened and softened[-1] != "":
                softened.append("")
            continue
        lead_stripped = re.sub(r"^(?:[呀哼嗯呜哈哦啊啦～~，,。!！…\s]+)", "", line).strip()
        if re.match(r"^参考置信度[:：]", lead_stripped):
            continue
        if re.match(r"^结论(?:是)?[:：]", lead_stripped):
            body = re.sub(r"^结论(?:是)?[:：]\s*", "", lead_stripped).strip()
            softened.append(
                _pick_non_repeat(
                    [
                        f"本鼠鼠先跟你说重点呀，{body}",
                        f"先把最要紧的一句递给你：{body}",
                        f"我先把答案轻轻放前面，{body}",
                        f"先别急，本鼠鼠先把重点捧给你：{body}",
                    ],
                    f"{seed_text}|conclusion|{body}",
                )
            )
            pending_advice_intro = False
            continue
        if re.match(r"^(依据|命理依据)(?:是)?[:：]", lead_stripped):
            body = re.sub(r"^(依据|命理依据)(?:是)?[:：]\s*", "", lead_stripped).strip()
            softened.append(
                _pick_non_repeat(
                    [
                        f"我会这么想呀，是因为{body}",
                        f"我会往这个方向看，是因为{body}",
                        f"这层意思会冒出来，是因为{body}",
                        f"本鼠鼠会这样判断，主要是因为{body}",
                    ],
                    f"{seed_text}|basis|{body}",
                )
            )
            pending_advice_intro = False
            continue
        if re.match(r"^命理信号(?:是)?[:：]", lead_stripped):
            body = re.sub(r"^命理信号(?:是)?[:：]\s*", "", lead_stripped).strip()
            softened.append(
                _pick_non_repeat(
                    [
                        f"盘面里最冒头的小信号是：{body}",
                        f"这会儿最扎眼的一笔，其实是：{body}",
                        f"本鼠鼠瞄到最明显的线头是：{body}",
                        f"盘里先跳出来提醒人的，是这句：{body}",
                    ],
                    f"{seed_text}|signal|{body}",
                )
            )
            pending_advice_intro = False
            continue
        if re.match(r"^工具验证(?:是)?[:：]", lead_stripped):
            body = re.sub(r"^工具验证(?:是)?[:：]\s*", "", lead_stripped).strip()
            softened.append(
                _pick_non_repeat(
                    [
                        f"本鼠鼠又悄悄交叉看了一眼：{body}",
                        f"我顺手多比对了一下，看到的是：{body}",
                        f"我又偷偷核了一遍细节，落下来是：{body}",
                        f"顺着这条线再查一眼，会发现：{body}",
                    ],
                    f"{seed_text}|tool|{body}",
                )
            )
            pending_advice_intro = False
            continue
        if re.match(r"^五行分布(?:是)?[:：]", lead_stripped):
            body = re.sub(r"^五行分布(?:是)?[:：]\s*", "", lead_stripped).strip()
            softened.append(
                _pick_non_repeat(
                    [
                        f"五行这边大概是：{body}",
                        f"五行落下来差不多是：{body}",
                        f"如果把五行摊开来瞧，大概是：{body}",
                        f"五行这一盘轻轻一看，大概是：{body}",
                    ],
                    f"{seed_text}|wuxing|{body}",
                )
            )
            pending_advice_intro = False
            continue
        if re.match(r"^(建议|行动建议|可执行建议|建议你|给.{0,8}条可执行的小建议)(?:是)?[:：]?\s*$", lead_stripped):
            softened.append(
                _pick_non_repeat(
                    [
                        "你现在可以先这样动一动：",
                        "要是想马上上手，可以先做这几步：",
                        "本鼠鼠给你收成几个顺手动作：",
                        "先别急着全做完，挑这几件开始就行：",
                    ],
                    f"{seed_text}|advice_intro",
                )
            )
            pending_advice_intro = True
            continue
        if pending_advice_intro and re.match(r"^\d+\.\s*", line):
            softened.append(line)
            continue
        pending_advice_intro = False
        softened.append(line)
    while softened and softened[-1] == "":
        softened.pop()
    return "\n".join(softened).strip()


def _expected_year_from_query(query: str, time_anchor: dict) -> int | None:
    q = str(query or "")
    try:
        anchor_year = int(str(time_anchor.get("today_date") or "2000-01-01").split("-")[0])
    except Exception:
        return None
    relative_tokens = ["前年", "去年", "今年", "本年", "明年", "后年"]
    if sum(1 for t in relative_tokens if t in q) >= 2:
        return None
    if "后年" in q:
        return anchor_year + 2
    if "明年" in q:
        return anchor_year + 1
    if "前年" in q:
        return anchor_year - 2
    if "去年" in q:
        return anchor_year - 1
    if "今年" in q or "本年" in q:
        return anchor_year
    return None


def _normalize_weekday_label(text: str) -> str:
    raw = str(text or "").strip().replace("周天", "周日")
    mapping = {
        "周一": "星期一",
        "周二": "星期二",
        "周三": "星期三",
        "周四": "星期四",
        "周五": "星期五",
        "周六": "星期六",
        "周日": "星期日",
        "星期天": "星期日",
    }
    return mapping.get(raw, raw)


def _build_time_safe_fallback(query: str, time_anchor: dict, window_meta: dict | None = None) -> str:
    now_cn = str(time_anchor.get("today_cn") or "")
    weekday_cn = str(time_anchor.get("weekday_cn") or "")
    tz_name = str(time_anchor.get("tz_name") or "")
    utc_offset = str(time_anchor.get("utc_offset") or "")
    near_days = time_anchor.get("near_days") or []
    q = str(query or "")
    if isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
        if window_text and _should_show_window_text(q, window_label, question_type="colloquial"):
            return (
                f"呀哈～我先把时间轻轻对齐一下：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。\n"
                f"{_natural_window_line(q, window_text, window_label, question_type='colloquial')}"
            )
    if NEAR_DAYS_QUERY_PATTERN.search(q) and near_days:
        window_text = "、".join(
            [f"{d.get('date_cn')}（{d.get('weekday_cn')}）" for d in near_days if d.get('date_cn')]
        )
        return (
            f"呀哈～我先把时间轻轻对齐一下：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。\n"
            f"这几天我先按{window_text}这段时间陪你慢慢看。"
        )
    return f"呀哈～先把时间对齐：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。"


def _is_birth_context(text: str, start: int, end: int) -> bool:
    left = max(0, int(start) - 12)
    right = min(len(text), int(end) + 12)
    nearby = str(text[left:right] or "")
    return bool(re.search(r"(出生|生日|生于|生在|命盘|八字|排盘|资料)", nearby))


def _collect_allowed_dates(time_anchor: dict, window_meta: dict | None = None) -> set[str]:
    allowed_dates: set[str] = set()
    for d in (time_anchor.get("near_days") or []):
        date_str = str(d.get("date") or "").strip()
        if date_str:
            allowed_dates.add(date_str)
    today = str(time_anchor.get("today_date") or "").strip()
    if today:
        allowed_dates.add(today)
    if isinstance(window_meta, dict):
        for d in (window_meta.get("days") or []):
            date_str = str((d or {}).get("date") or "").strip()
            if date_str:
                allowed_dates.add(date_str)
    return allowed_dates


def _iso_to_cn(date_str: str, short: bool = False) -> str:
    try:
        dt = datetime.strptime(str(date_str), "%Y-%m-%d")
    except Exception:
        return str(date_str or "")
    if short:
        return f"{dt.month}月{dt.day}日"
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _pick_closest_allowed_date(target_date: str, allowed_dates: set[str]) -> str:
    if not allowed_dates:
        return ""
    candidates = sorted([str(x) for x in allowed_dates if str(x)])
    try:
        target = datetime.strptime(str(target_date), "%Y-%m-%d")
    except Exception:
        return candidates[0]
    best = candidates[0]
    best_gap = 10**9
    for item in candidates:
        try:
            dt = datetime.strptime(item, "%Y-%m-%d")
            gap = abs((dt - target).days)
        except Exception:
            continue
        if gap < best_gap:
            best_gap = gap
            best = item
    return best


def _allowed_years_from_window(window_meta: dict | None) -> set[int]:
    if not isinstance(window_meta, dict):
        return set()
    years: set[int] = set()
    for key in ("window_start", "window_end"):
        value = str(window_meta.get(key) or "").strip()
        m = re.match(r"^(20\d{2})-\d{2}-\d{2}$", value)
        if not m:
            continue
        years.add(int(m.group(1)))
    if len(years) >= 2:
        y0 = min(years)
        y1 = max(years)
        years = set(range(y0, y1 + 1))
    return years


def _date_within_window(date_str: str, window_meta: dict | None) -> bool:
    if not isinstance(window_meta, dict):
        return False
    start = str(window_meta.get("window_start") or "").strip()
    end = str(window_meta.get("window_end") or "").strip()
    if not start or not end:
        return False
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        low = datetime.strptime(start, "%Y-%m-%d").date()
        high = datetime.strptime(end, "%Y-%m-%d").date()
    except Exception:
        return False
    if low > high:
        low, high = high, low
    return low <= target <= high


def _short_md_has_any_valid_year_in_window(month: int, day: int, window_meta: dict | None) -> bool:
    if not isinstance(window_meta, dict):
        return False
    years = _allowed_years_from_window(window_meta)
    if not years:
        return False
    for year in sorted(years):
        try:
            dt = datetime(year=year, month=int(month), day=int(day))
        except Exception:
            continue
        if _date_within_window(dt.strftime("%Y-%m-%d"), window_meta):
            return True
    return False


def _patch_time_text_locally(
    out: str,
    query: str,
    time_anchor: dict,
    allowed_dates: set[str],
    window_meta: dict | None = None,
) -> tuple[str, int, bool]:
    patched = str(out or "")
    conflict_count = 0
    severe_mismatch = False

    for m in reversed(list(DATE_WEEKDAY_PATTERN.finditer(patched))):
        if _is_birth_context(patched, m.start(), m.end()):
            continue
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        weekday_text = _normalize_weekday_label(m.group(4))
        expected = _weekday_cn_from_date(year, month, day)
        if expected and weekday_text and weekday_text != expected:
            conflict_count += 1
            _metric_incr("weekday_mismatch_count")
            start = m.start(4)
            end = m.end(4)
            patched = patched[:start] + expected + patched[end:]

    if RELATIVE_WINDOW_PATTERN.search(str(query or "")):
        anchor_year = int(str(time_anchor.get("today_date") or "2000-01-01").split("-")[0])
        allowed_years = _allowed_years_from_window(window_meta)
        full_matches = list(DATE_FULL_PATTERN.finditer(patched))
        for m in reversed(full_matches):
            if _is_birth_context(patched, m.start(), m.end()):
                continue
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            if allowed_years:
                if year not in allowed_years:
                    severe_mismatch = True
            elif abs(year - anchor_year) >= 2 and _expected_year_from_query(query, time_anchor) is None:
                severe_mismatch = True
            if allowed_dates and date_str not in allowed_dates:
                if _date_within_window(date_str, window_meta):
                    continue
                conflict_count += 1
                replacement = _pick_closest_allowed_date(date_str, allowed_dates)
                patched = patched[:m.start()] + _iso_to_cn(replacement, short=False) + patched[m.end():]
        full_spans = [(m.start(), m.end()) for m in DATE_FULL_PATTERN.finditer(patched)]
        short_matches = list(DATE_SHORT_PATTERN.finditer(patched))
        for m in reversed(short_matches):
            if _is_birth_context(patched, m.start(), m.end()):
                continue
            if any(start <= m.start() and m.end() <= end for start, end in full_spans):
                continue
            month = int(m.group(1))
            day = int(m.group(2))
            if _short_md_has_any_valid_year_in_window(month, day, window_meta):
                continue
            date_str = f"{anchor_year:04d}-{month:02d}-{day:02d}"
            if allowed_dates and date_str not in allowed_dates:
                if _date_within_window(date_str, window_meta):
                    continue
                conflict_count += 1
                replacement = _pick_closest_allowed_date(date_str, allowed_dates)
                patched = patched[:m.start()] + _iso_to_cn(replacement, short=True) + patched[m.end():]
        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(patched)}
        if years:
            if allowed_years and any(y not in allowed_years for y in years):
                severe_mismatch = True
            elif not allowed_years and any(abs(y - anchor_year) >= 2 for y in years):
                if _expected_year_from_query(query, time_anchor) is None:
                    severe_mismatch = True

    expected_year = _expected_year_from_query(query, time_anchor)
    if expected_year is not None:
        year_matches = list(YEAR_PATTERN.finditer(patched))
        for m in reversed(year_matches):
            year = int(m.group(1))
            if year == expected_year:
                continue
            conflict_count += 1
            patched = patched[:m.start(1)] + str(expected_year) + patched[m.end(1):]

    if conflict_count >= 6:
        severe_mismatch = True
    return patched, conflict_count, severe_mismatch


def _validate_time_consistency_legacy(text: str, query: str, time_anchor: dict, window_meta: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    if not is_time_sensitive_query(q) and not is_bazi_fortune_query(q):
        return out
    _metric_incr("temporal_consistency_total")
    _metric_incr("time_guard_total")

    allowed_dates = _collect_allowed_dates(time_anchor, window_meta=window_meta)
    for m in DATE_WEEKDAY_PATTERN.finditer(out):
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        weekday_text = _normalize_weekday_label(m.group(4))
        expected = _weekday_cn_from_date(year, month, day)
        if expected and weekday_text and weekday_text != expected:
            _metric_incr("time_validation_fail_total")
            _metric_incr("time_validation_autofix_total")
            _metric_incr("temporal_consistency_fail")
            _metric_incr("weekday_mismatch_count")
            return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

    if RELATIVE_WINDOW_PATTERN.search(q):
        explicit_dates: set[str] = set()
        for m in DATE_FULL_PATTERN.finditer(out):
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            explicit_dates.add(f"{year:04d}-{month:02d}-{day:02d}")
        anchor_year = int(str(time_anchor.get("today_date") or "2000-01-01").split("-")[0])
        for m in DATE_SHORT_PATTERN.finditer(out):
            month = int(m.group(1))
            day = int(m.group(2))
            explicit_dates.add(f"{anchor_year:04d}-{month:02d}-{day:02d}")

        for date_str in explicit_dates:
            if date_str not in allowed_dates:
                _metric_incr("time_validation_fail_total")
                _metric_incr("time_validation_autofix_total")
                _metric_incr("temporal_consistency_fail")
                return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(out)}
        if years and any(str(y) not in {x[:4] for x in allowed_dates} for y in years):
            _metric_incr("time_validation_fail_total")
            _metric_incr("time_validation_autofix_total")
            _metric_incr("temporal_consistency_fail")
            return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

    expected_year = _expected_year_from_query(q, time_anchor)
    if expected_year is not None:
        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(out)}
        if years and any(y != expected_year for y in years):
            _metric_incr("time_validation_fail_total")
            _metric_incr("time_validation_autofix_total")
            _metric_incr("temporal_consistency_fail")
            return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

    _metric_incr("temporal_consistency_hit")
    return out


def _strip_time_alignment_sentences(text: str) -> str:
    normalized = str(text or "").replace("\\n", "\n")
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]
    kept: list[str] = []
    for line in lines:
        if re.search(
            r"(时间对齐|当前时间|现在是|你问的时间窗口按这个范围计算|时间窗口按这个范围计算|时间窗口是|窗口计算|今天起|近几天|UTC|Asia/Shanghai)",
            line,
        ):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _extract_business_sentences(text: str) -> str:
    normalized = str(text or "").replace("\\n", "\n")
    if not normalized.strip():
        return ""
    pieces = [p.strip() for p in re.split(r"[\n。！？!?；;]+", normalized) if p.strip()]
    if not pieces:
        return ""
    kept: list[str] = []
    for piece in pieces:
        if re.search(r"(时间对齐|当前时间|现在是|时间窗口按这个范围计算|时间窗口是|UTC|Asia/Shanghai)", piece):
            continue
        if re.search(r"(结论|建议|先|避免|适合|财运|事业|感情|学业|风险)", piece):
            kept.append(piece)
    return "。".join(kept).strip()


def validate_time_consistency(text: str, query: str, time_anchor: dict, window_meta: dict | None = None) -> str:
    if not _time_patch_v1_enabled():
        return _validate_time_consistency_legacy(text, query, time_anchor, window_meta=window_meta)

    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    if not is_time_sensitive_query(q) and not is_bazi_fortune_query(q) and not RELATIVE_WINDOW_PATTERN.search(q):
        return out
    _metric_incr("temporal_consistency_total")
    _metric_incr("time_guard_total")

    allowed_dates = _collect_allowed_dates(time_anchor, window_meta=window_meta)
    patched, conflict_count, severe_mismatch = _patch_time_text_locally(
        out,
        q,
        time_anchor,
        allowed_dates,
        window_meta=window_meta,
    )
    patched = _rewrite_today_only_window(patched, q, window_meta=window_meta)
    if severe_mismatch:
        _metric_incr("time_validation_fail_total")
        _metric_incr("time_validation_autofix_total")
        _metric_incr("temporal_consistency_fail")
        safe = _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)
        residual = _strip_time_alignment_sentences(patched)
        if not residual:
            residual = _extract_business_sentences(patched)
        if residual:
            return _rewrite_today_only_window(f"{safe}\n\n{residual}".strip(), q, window_meta=window_meta)
        _metric_incr("time_guard_overwrite_total")
        return _rewrite_today_only_window(safe, q, window_meta=window_meta)
    if conflict_count > 0:
        _metric_incr("time_validation_fail_total")
        _metric_incr("time_validation_autofix_total")
        _metric_incr("time_validation_patch_total")
    _metric_incr("temporal_consistency_hit")
    return _rewrite_today_only_window(patched, q, window_meta=window_meta)


def get_fast_reply(query: str, time_anchor: dict | None = None, profile: dict | None = None) -> str | None:
    raw_text = (query or "").strip()
    text = raw_text.lower()
    if not raw_text:
        return None
    if _is_asking_own_name(raw_text):
        p = profile or {}
        call_name = _sanitize_preferred_name(str(p.get("preferred_name") or "").strip())
        legal_name = str(p.get("name") or "").strip()
        if call_name:
            return f"我记得你喜欢我叫你{call_name}。"
        if _is_valid_name(legal_name):
            return f"我记得你叫{legal_name}。"
        return "我这边还没有你的姓名记录。你可以直接告诉我“我叫XXX”。"

    # 时间/日期类问题：使用系统实时时间，避免大模型产生日期幻觉
    time_keywords = re.compile(
        r"(今天.*(日期|几号|星期|周几)|现在.*(时间|几点)|当前.*(时间|日期)|"
        r"几月几号|几号了|星期几|周几|today|date|time|what day)"
    )
    if time_keywords.search(text):
        anchor = time_anchor or build_time_anchor()
        return (
            "呀哈～本鼠鼠帮你看了北京时间："
            f"{anchor.get('today_cn')}，{anchor.get('weekday_cn')}，"
            f"{anchor.get('time_str')}（{anchor.get('utc_offset')}）。"
        )

    greetings = {
        "你好", "你好呀", "你好啊", "在吗", "在嘛", "嗨", "hi", "hello", "早上好", "中午好", "晚上好"
    }
    if text in greetings:
        return "呀哈～本鼠鼠在呢。想聊聊今天的心情，还是看看最近运势呀？"

    tiny_talk = {"忙吗", "你在干嘛", "有人吗"}
    if text in tiny_talk:
        return "呜啦～本鼠鼠正在认真值班。你问我就会认真听。"

    return None


def _normalize_birthdate(text: str) -> str:
    # 支持 2005-06-15 / 2005.6.15 / 2005年6月15日 等形式
    m = re.search(r"(\d{4})[年/\-.]\s*(\d{1,2})[月/\-.]\s*(\d{1,2})", text)
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
    return f"{y}-{mo}-{d}"


def _normalize_birthtime(text: str) -> str:
    # 支持 5:45 / 05:45 / 5点45分 / 早上5:45 等常见表达
    m = re.search(r"([01]?\d|2[0-3])\s*[:：点时]\s*([0-5]?\d)", text)
    if m:
        hh, mm = m.group(1).zfill(2), m.group(2).zfill(2)
        return f"{hh}:{mm}"
    m2 = re.search(r"\b([01]?\d|2[0-3])\s*(?:点|时)\b", text)
    if m2:
        return f"{m2.group(1).zfill(2)}:00"
    return ""


def _normalize_gender(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.lower() in {"female", "f", "woman", "girl"}:
        return "女"
    if raw.lower() in {"male", "m", "man", "boy"}:
        return "男"
    if raw in {"女", "女生", "女的", "女性", "女孩", "女士"} or re.search(r"(女生|女的|女性|女孩|女士)", raw):
        return "女"
    if raw in {"男", "男生", "男的", "男性", "男孩", "先生"} or re.search(r"(男生|男的|男性|男孩|先生)", raw):
        return "男"
    return ""


def _is_valid_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    if len(n) < 2 or len(n) > 16:
        return False
    # 过滤明显非姓名内容，避免把“我是谁”“我叫什么”误识别为名字
    invalid_tokens = {"谁", "谁呀", "谁啊", "什么", "啥", "名字", "姓名", "自己", "你", "我", "他", "她", "它"}
    if n in invalid_tokens:
        return False
    if re.search(r"(谁|什么|吗|呢|呀|啊|\?|？)", n):
        return False
    return True


def _is_valid_call_name(name: str) -> bool:
    n = str(name or "").strip()
    if not n or len(n) > 12:
        return False
    invalid_tokens = {
        "你", "我", "他", "她", "它", "自己", "名字", "姓名", "昵称", "称呼", "随便", "都行", "都可以", "无所谓",
        "不知道", "不告诉你",
    }
    if n in invalid_tokens:
        return False
    if re.search(r"(怎么|什么|谁|吗|呢|呀|啊|\?|？|!|！|,|，|。)", n):
        return False
    return True


def _looks_like_time_or_date_fragment(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if re.search(
        r"(20\d{2}|19\d{2}|\d{1,2}[:：点时分]|今天|现在|时间|今年|明年|后年|本周|下周|本月|星期|周[一二三四五六日天])",
        t,
    ):
        return True
    if re.search(r"^\d+$", t):
        return True
    return False


def _looks_like_preferred_name_pollution(text: str, source: str = "") -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if _looks_like_time_or_date_fragment(t):
        return True
    if re.search(r"^属[鼠牛虎兔龙蛇马羊猴鸡狗猪]", t):
        return True
    if re.search(r"(运势|星座|生肖|属相|财运|事业运|桃花|姻缘|解梦|占卜|摇卦|领证|搬家|工作节奏|小目标|睡眠)", t):
        return True
    src = str(source or "").strip()
    if src and not re.search(r"(叫我|喊我|称呼我)", src):
        if re.search(
            r"(帮我|给我|看一下|看下|看看|算一下|分析|适合|哪天|几天|怎么|如何|记得吗|我叫什么|运势|星座|生肖|属相|解梦|占卜|摇卦)",
            src,
        ):
            return True
    return False


def _sanitize_preferred_name(name: str, source: str = "") -> str:
    candidate = str(name or "").strip()
    if not _is_valid_call_name(candidate):
        return ""
    if _looks_like_preferred_name_pollution(candidate, source=source):
        return ""
    return candidate


def _is_name_question_query(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    return bool(
        re.search(r"(我叫什?么|我叫什么名字|我是谁|你知道我叫什?么|你记得我叫什?么|你记得我的名字|我叫什么你记得吗)", q)
    )


def _extract_legal_name_with_confidence(query: str) -> tuple[str, str]:
    source = str(query or "").strip()
    if not source:
        return "", "none"
    if _is_name_question_query(source):
        return "", "none"
    explicit_patterns = [
        r"(?:我叫|我的名字是|名字是|姓名是)\s*([^\s，。！？,.]{2,16})",
        r"^([^\s，。！？,.]{2,16})[，,\s]+(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?",
    ]
    for pattern in explicit_patterns:
        m = re.search(pattern, source)
        if not m:
            continue
        candidate = str(m.group(1) or "").strip()
        if not _is_valid_name(candidate):
            continue
        if _looks_like_time_or_date_fragment(candidate):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        _metric_incr("name_slot_total")
        return candidate, "high"
    if re.search(r"(我叫|名字是|姓名是|我是)", source):
        _metric_incr("name_slot_total")
        _metric_incr("name_slot_pollution")
    return "", "none"


def _extract_preferred_name_with_confidence(query: str, allow_soft: bool = False) -> tuple[str, str]:
    source = str(query or "").strip()
    if not source:
        return "", "none"
    if _is_name_question_query(source):
        return "", "none"
    if is_time_sensitive_query(source) and not re.search(r"(叫我|喊我|称呼我)", source):
        return "", "none"
    patterns = [
        r"(?:你可以|以后|之后)?(?:叫我|喊我|称呼我)\s*([^\s，。！？,.]{1,12})",
        r"(?<!我)(?:叫我|喊我)\s*([^\s，。！？,.]{1,12})\s*(?:就行|即可|吧|呀|啦|哦|喔)",
    ]
    for pattern in patterns:
        m = re.search(pattern, source)
        if not m:
            continue
        candidate = str(m.group(1) or "").strip()
        candidate = re.sub(r"(吧|呀|啦|哦|喔|呢)$", "", candidate).strip()
        if not _sanitize_preferred_name(candidate, source=source):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        _metric_incr("name_slot_total")
        return candidate, "high"
    if allow_soft:
        normalized = re.sub(r"[\s，。！？,.!？、；;:：]", "", source)
        normalized = re.sub(r"^(那就|就|那|嗯|啊|呀|呜啦|呀哈)", "", normalized).strip()
        normalized = re.sub(r"(吧|呀|啦|哦|喔|呢|就行|即可)$", "", normalized).strip()
        if 1 <= len(normalized) <= 6 and _sanitize_preferred_name(normalized, source=source):
            _metric_incr("name_slot_total")
            return normalized, "medium"
    if re.search(r"(叫我|喊我|称呼我|昵称|名字)", source):
        _metric_incr("name_slot_total")
        _metric_incr("name_slot_pollution")
    return "", "none"


def _extract_preferred_name_from_query(query: str) -> str:
    preferred_name, _ = _extract_preferred_name_with_confidence(query, allow_soft=False)
    return preferred_name


def _is_asking_own_name(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    return bool(
        re.search(r"(我叫什?么|我是谁|我的名字|你知道我叫什?么|你知道我的名字|我叫什么名字|记得我叫)", q)
    )


def _is_asking_own_birthdate(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    return bool(
        re.search(
            r"(生日|出生日期|哪天出生)",
            q,
        )
    )


def _is_asking_own_birthtime(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    return bool(
        re.search(
            r"(出生时间|出生时段|出生时辰|时辰)",
            q,
        )
    )


def _is_identity_fact_query(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    if _is_asking_own_name(q):
        return True
    if _is_asking_own_birthdate(q) or _is_asking_own_birthtime(q):
        return True
    return bool(re.search(r"(你记得我吗|你记得我是谁吗|我是谁你还记得吗)", q))


def _name_alias_candidates(name: str) -> list[str]:
    raw = str(name or "").strip()
    if not raw:
        return []
    out: list[str] = []
    if len(raw) >= 2:
        out.append(raw[-2:])
        out.append(raw[-1])
        out.append(raw[-1] * 2)
    if len(raw) >= 3:
        out.append(raw[1:])
    seen: set[str] = set()
    deduped: list[str] = []
    for item in out:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        if not _is_valid_call_name(token):
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _pick_address_name(profile: dict | None, user_query: str = "") -> str:
    p = profile or {}
    preferred_name = _sanitize_preferred_name(str(p.get("preferred_name") or "").strip())
    if preferred_name:
        return preferred_name
    name = str(p.get("name") or "").strip()
    if not _is_valid_name(name):
        return "你"
    if _is_identity_fact_query(user_query):
        return name
    if not _is_asking_own_name(user_query):
        return "你"
    aliases = _name_alias_candidates(name)
    if not aliases:
        return name
    choices = [name] + aliases
    seed = hashlib.sha256(f"{name}|{user_query}".encode("utf-8")).hexdigest()
    idx = int(seed[:8], 16) % len(choices)
    return choices[idx]


def _is_name_intro_query(query: str, extracted: dict[str, str] | None = None) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    e = extracted or {}
    if not str(e.get("name") or "").strip():
        return False
    if not _confidence_ge(str(e.get("name_confidence") or "none"), "high"):
        return False
    if re.search(r"(我叫|名字是|姓名是|^.{1,16}\s*[，,]\s*(?:19|20)\d{2})", q):
        return True
    return False


def _profile_seed_remainder(query: str, extracted: dict[str, str] | None = None) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    extracted = extracted or {}
    out = q
    name = str(extracted.get("name") or "").strip()
    if name:
        out = re.sub(rf"(?:我叫|我的名字是|名字是|姓名是)\s*{re.escape(name)}", "", out)
        out = re.sub(rf"^{re.escape(name)}[，,\s]*", "", out)
    out = re.sub(r"(?:19|20)\d{2}[年/\-.]\s*\d{1,2}[月/\-.]\s*\d{1,2}(?:日)?", "", out)
    out = re.sub(r"(?:[01]?\d|2[0-3])\s*[:：点时]\s*(?:[0-5]?\d)?\s*(?:分)?", "", out)
    out = re.sub(r"(我是|性别是)?\s*(女生|女的|女性|女孩|男生|男的|男性|男孩)", "", out)
    out = re.sub(r"(出生于|出生在|出生|生日是|生日)", "", out)
    out = re.sub(r"(公历|阳历|农历)", "", out)
    out = re.sub(r"[，。！？、,.!?；;：:\s]+", "", out)
    out = re.sub(r"(好的|收到|啦|呀|哦|喔|呢)$", "", out)
    return out.strip()


def _is_profile_seed_only_query(query: str, extracted: dict[str, str] | None = None) -> bool:
    extracted = extracted or {}
    has_profile_piece = any(str(extracted.get(key) or "").strip() for key in ("name", "birthdate", "birthtime"))
    if not has_profile_piece:
        return False
    q = str(query or "").strip()
    if not q:
        return False
    if _is_identity_fact_query(q):
        return False
    if is_dream_query(q) or is_divination_query(q) or is_zodiac_intent_query(q) or is_bazi_fortune_query(q):
        return False
    if is_time_sensitive_query(q):
        return False
    remainder = _profile_seed_remainder(q, extracted=extracted)
    return not remainder


def _build_profile_seed_reply(profile: dict[str, str], extracted: dict[str, str] | None = None) -> str:
    profile = profile or {}
    extracted = extracted or {}
    name = str(profile.get("name") or extracted.get("name") or "").strip()
    birthdate = str(profile.get("birthdate") or extracted.get("birthdate") or "").strip()
    birthtime = str(profile.get("birthtime") or extracted.get("birthtime") or "").strip()
    gender = _normalize_gender(str(profile.get("gender") or extracted.get("gender") or ""))
    parts: list[str] = []
    if name and birthdate:
        parts.append(f"呀哈～我先帮你记住啦：你叫{name}，生日是{_iso_to_cn(birthdate)}。")
    elif name:
        parts.append(f"呀哈～我先记住啦，你叫{name}。")
    elif birthdate:
        parts.append(f"呀哈～我先把你的生日记下啦：{_iso_to_cn(birthdate)}。")
    else:
        parts.append("呀哈～我先把你刚刚补的资料记下啦。")

    if gender:
        parts.append(f"性别我也一起记好了（{gender}）。")

    if birthtime:
        parts.append(f"出生时段也一起收好了（{birthtime}）。之后如果你想看更细一点的八字、流年或择时，直接问我就行。")
    elif birthdate and gender:
        parts.append("现在看大部分命理、姻缘和趋势已经够用了；如果你之后想看更细一点的八字或择时，再补出生时间就行。")
    elif birthdate:
        parts.append("现在看星座、生肖和一般趋势已经够用了；但如果要看更完整的命理、姻缘或择时，还需要再补一个性别。")
    else:
        parts.append("如果你之后想看命理或运势，再补一个出生年月日，我就能继续往下看。")

    if birthdate and gender:
        parts.append("你下一句可以直接问我：今天运势如何、帮我看星座运势，或者下周哪天适合搬家。")
    elif birthdate:
        parts.append("你可以继续补一句“我是男生/我是女生”，补完我就能继续看更完整的命理问题。")
    else:
        parts.append("你可以继续补资料，也可以直接告诉我你现在最想问的那件事。")
    return "\n\n".join(parts)


def _set_preferred_name_prompt_pending(session_id: str, pending: bool) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    key = _preferred_name_prompt_key(sid)
    try:
        if pending:
            _REDIS_CLIENT.setex(key, PREFERRED_NAME_PROMPT_TTL_SECONDS, "1")
        else:
            _REDIS_CLIENT.delete(key)
    except Exception:
        return


def _is_preferred_name_prompt_pending(session_id: str) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    try:
        return bool(_REDIS_CLIENT.exists(_preferred_name_prompt_key(sid)))
    except Exception:
        return False


def extract_profile_from_query(query: str) -> dict[str, str]:
    source = (query or "").strip()
    profile: dict[str, str] = {}
    legal_name, legal_conf = _extract_legal_name_with_confidence(source)
    if legal_name:
        profile["name"] = legal_name
        profile["name_confidence"] = legal_conf
    birthdate = _normalize_birthdate(source)
    if birthdate:
        profile["birthdate"] = birthdate
    birthtime = _normalize_birthtime(source)
    if birthtime:
        profile["birthtime"] = birthtime
    gender = _normalize_gender(source)
    if gender:
        profile["gender"] = gender
    preferred_name, preferred_conf = _extract_preferred_name_with_confidence(source, allow_soft=False)
    if preferred_name:
        profile["preferred_name"] = preferred_name
        profile["preferred_name_confidence"] = preferred_conf
    return profile


def merge_session_profile(session_id: str, current: dict[str, str]) -> dict[str, str]:
    # session_id 在当前实现中绑定用户 uuid
    user = _get_user_by_uuid(session_id)
    if not user:
        return {
            "name": "",
            "birthdate": "",
            "birthtime": "",
            "gender": "",
            "preferred_name": "",
            "name_confidence": "none",
            "preferred_name_confidence": "none",
        }
    return _merge_profile_to_db(int(user["id"]), current)


def build_profile_context(
    profile: dict[str, str],
    *,
    domain_intent: str = "general",
    question_type: str = "default",
    user_query: str = "",
) -> str:
    lightweight_intents = {"general", "time", "dream", "divination"}
    lightweight_guardrail = "当前问题不是命理咨询，除非用户明确要求，否则不要根据出生日期、生肖、星座、八字、流年来推导结论。"
    if not profile:
        if domain_intent in lightweight_intents and question_type != "identity_fact":
            return f"暂无用户资料。{lightweight_guardrail}"
        return "暂无用户资料。"
    parts = []
    if profile.get("name"):
        parts.append(f"姓名：{profile['name']}")
    preferred_name = _sanitize_preferred_name(str(profile.get("preferred_name") or "").strip())
    if preferred_name:
        parts.append(f"称呼偏好：{preferred_name}")
    allow_birth_context = (
        domain_intent in {"fortune", "zodiac"}
        or is_bazi_fortune_query(user_query)
        or is_zodiac_intent_query(user_query)
    )
    if allow_birth_context:
        if profile.get("birthdate"):
            parts.append(f"出生日期：{profile['birthdate']}")
        if profile.get("birthtime"):
            parts.append(f"出生时间：{profile['birthtime']}")
        gender = _normalize_gender(str(profile.get("gender") or ""))
        if gender:
            parts.append(f"性别：{gender}")
    if not parts:
        if domain_intent in lightweight_intents and question_type != "identity_fact":
            return f"暂无用户资料。{lightweight_guardrail}"
        return "暂无用户资料。"
    profile_line = "；".join(parts)
    if not allow_birth_context and domain_intent in lightweight_intents and question_type != "identity_fact":
        return f"{profile_line}。{lightweight_guardrail}"
    return f"{profile_line}。可自然使用用户偏好的称呼；不要逐字回显完整生日和时辰。"


def extract_profile_from_history(chat_message_history) -> dict[str, str]:
    profile: dict[str, str] = {}
    try:
        messages = getattr(chat_message_history, "messages", []) or []
        for msg in messages:
            role = str(getattr(msg, "type", "")).lower()
            if role not in {"human", "user"}:
                continue
            content = getattr(msg, "content", "")
            if not isinstance(content, str):
                continue
            piece = extract_profile_from_query(content)
            current_name_conf = str(profile.get("name_confidence") or "none")
            incoming_name_conf = str(piece.get("name_confidence") or "none")
            if piece.get("name") and (
                not profile.get("name")
                or (_confidence_ge(incoming_name_conf, "high") and not _confidence_ge(current_name_conf, "high"))
            ):
                profile["name"] = piece["name"]
                profile["name_confidence"] = incoming_name_conf
            if piece.get("preferred_name") and not profile.get("preferred_name"):
                profile["preferred_name"] = piece["preferred_name"]
                profile["preferred_name_confidence"] = str(piece.get("preferred_name_confidence") or "none")
            if piece.get("birthdate") and not profile.get("birthdate"):
                profile["birthdate"] = piece["birthdate"]
            if piece.get("birthtime") and not profile.get("birthtime"):
                profile["birthtime"] = piece["birthtime"]
            if piece.get("gender") and not profile.get("gender"):
                profile["gender"] = piece["gender"]
            if profile.get("name") and profile.get("birthdate") and profile.get("birthtime") and profile.get("gender"):
                break
    except Exception:
        return profile
    return profile


def _append_chat_audit_to_db(
    user_id: int,
    session_id: str,
    query: str,
    output: str,
    question_type: str = "",
    route_path: str = "",
) -> None:
    """写入 MySQL 审计表，确保历史可长期留存（不受 Redis TTL 影响）。"""
    uid = int(user_id or 0)
    sid = str(session_id or "").strip()
    q = str(query or "").strip()
    out = str(output or "").strip()
    if uid <= 0 or not sid or not q or not out:
        return
    meta_obj = {
        "source": "chat_api",
        "question_type": str(question_type or ""),
        "route_path": str(route_path or ""),
    }
    meta_json = None
    try:
        meta_json = json.dumps(meta_obj, ensure_ascii=False)
    except Exception:
        meta_json = None
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO chat_messages (user_id, session_id, role, content, meta_json)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (uid, sid, "user", q, meta_json),
                        (uid, sid, "assistant", out, meta_json),
                    ],
                )
    except Exception as e:
        # 审计写入失败不能阻断主流程；仅告警。
        logger.warning(f"写入 MySQL 聊天审计失败: {e}")


def _append_chat_history(
    chat_message_history,
    query: str,
    output: str,
    user_id: int = 0,
    session_id: str = "",
    question_type: str = "",
    route_path: str = "",
) -> None:
    """在非 Agent 早返回分支补记会话，避免历史缺失；同时落 MySQL 审计。"""
    if chat_message_history is None:
        # 即便 Redis 历史不可用，仍尝试审计落库。
        _append_chat_audit_to_db(
            user_id=user_id,
            session_id=session_id,
            query=query,
            output=output,
            question_type=question_type,
            route_path=route_path,
        )
        return
    q = str(query or "").strip()
    out = str(output or "").strip()
    if not q or not out:
        return
    try:
        chat_message_history.add_user_message(q)
        chat_message_history.add_ai_message(out)
    except Exception as e:
        logger.warning(f"写入会话历史失败: {e}")
    _append_chat_audit_to_db(
        user_id=user_id,
        session_id=session_id,
        query=q,
        output=out,
        question_type=question_type,
        route_path=route_path,
    )


def detect_emotion_level(query: str) -> str:
    text = str(query or "")
    if not text:
        return "L1"
    angry_words = re.compile(r"(投诉|生气|愤怒|垃圾|扯皮|一直没解决|太离谱|受不了)")
    urge_words = re.compile(r"(快点|赶紧|马上|立刻|到底|怎么还|一直)")
    qmarks = text.count("?") + text.count("？")
    if angry_words.search(text):
        return "L3"
    if qmarks >= 3 or (urge_words.search(text) and qmarks >= 1):
        return "L3"
    if qmarks >= 2 or urge_words.search(text):
        return "L2"
    return "L1"


def _pick_non_repeat(options: list[str], seed_text: str, avoid: str = "") -> str:
    if not options:
        return ""
    pool = [x for x in options if x != avoid] or options
    seed = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    return pool[int(seed[:8], 16) % len(pool)]


def build_style_instruction(query: str, emotion_level: str, session_id: str) -> str:
    state = _get_reply_style_state(session_id)
    openings = [
        "先给你一句明确结论。",
        "先说最关键的一步。",
        "我先把方向给你，再补细节。",
        "先不绕圈，直接告诉你该做什么。",
        "先给你一个马上能做的小动作。",
    ]
    empathies = [
        "先把你最关心的结论讲清楚。",
        "我先给你命理上的判断，再给你一条能马上做的动作。",
        "先定方向，再给执行步骤。",
        "先说重点，再补一句命理线索。",
        "我先给你可落地的做法，不绕圈。",
    ]
    formats = [
        "短段落表达，像真人聊天，不要公文体。",
        "观点清晰，但不用固定三段式。",
        "命理线索点到为止，避免术语堆叠。",
        "可给1-2个可执行建议，不强制条目化。",
        "句式多样化，避免复读前一轮措辞。",
    ]
    opening = _pick_non_repeat(openings, f"{query}|opening", state.get("opening", ""))
    empathy = _pick_non_repeat(empathies, f"{query}|empathy", state.get("empathy", ""))
    format_hint = _pick_non_repeat(formats, f"{query}|format", state.get("format", ""))
    _set_reply_style_state(session_id, {"opening": opening, "empathy": empathy, "format": format_hint})

    tone_rule = "吉伊语气词自然点缀即可，不必强行加入。"
    if emotion_level == "L2":
        tone_rule = "用户偏焦虑：先结果后解释，内容更短；减少卖萌词。"
    if emotion_level == "L3":
        tone_rule = "用户偏愤怒/投诉：先道歉+处理路径+时间承诺；减少解释和卖萌。"

    return (
        f"{opening}{empathy}{format_hint}"
        f"不要复述用户问题；避免固定开场和重复安慰句。"
        f"{tone_rule}"
    )


def build_ellipsis_context_note(query: str, chat_message_history) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    if len(q) > 20:
        return ""
    if not re.search(r"(那个|这个|多久|怎么办|怎么做|怎么弄|还没|到了吗|可以吗)", q):
        return ""

    recent_user = ""
    try:
        for msg in reversed(getattr(chat_message_history, "messages", []) or []):
            role = str(getattr(msg, "type", "")).lower()
            content = str(getattr(msg, "content", "")).strip()
            if not content:
                continue
            if role in {"human", "user"}:
                recent_user = content
                if recent_user != q:
                    break
    except Exception:
        recent_user = ""
    if not recent_user or recent_user == q:
        return ""
    return f"用户本轮可能是省略问法；上一轮主题：{recent_user}。先按该主题理解后作答。"


BAZI_FORTUNE_QUERY_PATTERN = re.compile(
    r"(算命|八字|流年|运势|桃花|姻缘|婚缘|婚运|结婚|婚期|成婚|正缘|另一半|配偶|感情运|财运|事业运|学业运|贵人运|命盘|命理|紫微|测算|提运|气场|顺不顺|更顺)"
)
DIVINATION_QUERY_PATTERN = re.compile(r"(占卜|摇卦|抽签|起卦|卦象|卦)")
DREAM_QUERY_PATTERN = re.compile(r"(解梦|梦见|做梦|周公)")
FORTUNE_SCENE_PATTERN = re.compile(
    r"(今天|本周|这周|下周|本月|最近|这段时间|现在|未来|接下来|今年|本年|明年|后年|去年|前年|上半年|下半年)"
)
FORTUNE_DECISION_PATTERN = re.compile(
    r"(开源|守财|扩收入|控支出|先.*还是|二选一|更适合|哪个更|优先|先守后开|守中带开|"
    r"最旺.*方向|行动方向|避免.*决策|换岗.*积累|适合.*换岗|先积累|该不该换岗|更容易提运|提运.*领域)"
)
FORTUNE_COLLOQUIAL_PATTERN = re.compile(r"(气场|更顺|顺不顺|哪几天|哪天|近几天|这几天)")
FORTUNE_TREND_PATTERN = re.compile(
    r"(本周|这周|最近一周|这一周|走势|趋势|节奏|上半段|下半段|"
    r"今年|本年|明年|后年|去年|前年|全年|年度|"
    r"(?:未来|接下来)(?:的)?(?:[一二两三四五六七八九]|[1-9])年|(?:[一二两三四五六七八九]|[1-9])年内|"
    r"(?:今年|明年|后年|去年|前年)\s*(?:和|跟|与|对比|比较)\s*(?:今年|明年|后年|去年|前年)|"
    r"(?:20\d{2})(?:年)?\s*(?:和|跟|与|对比|比较)\s*(?:20\d{2})(?:年)?)"
)
FORTUNE_ACTION_PATTERN = re.compile(r"(先做什么|第一步|怎么安排|如何安排|怎么排更稳|怎么做|怎么行动)")
FORTUNE_SHORT_DECISION_PATTERN = re.compile(
    r"(开源还是守财|守财还是开源|先开源还是先守财|先守财还是先开源|"
    r"先扩收入还是先控支出|扩收入还是控支出|先控支出还是先扩收入)"
)
FORTUNE_CHOICE_PATTERN = re.compile(r"(.{1,12})还是(.{1,12})")
FORTUNE_DOMAIN_HINT_PATTERN = re.compile(r"(运势|财|收入|支出|工作|事业|学业|感情|节奏|风险|行动|决策)")
ZODIAC_SIGN_ALIASES = {
    "白羊座": ["白羊座", "白羊"],
    "金牛座": ["金牛座", "金牛"],
    "双子座": ["双子座", "双子"],
    "巨蟹座": ["巨蟹座", "巨蟹"],
    "狮子座": ["狮子座", "狮子"],
    "处女座": ["处女座", "处女"],
    "天秤座": ["天秤座", "天秤"],
    "天蝎座": ["天蝎座", "天蝎"],
    "射手座": ["射手座", "射手"],
    "摩羯座": ["摩羯座", "摩羯"],
    "水瓶座": ["水瓶座", "水瓶"],
    "双鱼座": ["双鱼座", "双鱼"],
}
SHENGXIAO_ALIASES = {
    "鼠": ["属鼠", "生肖鼠", "鼠生肖"],
    "牛": ["属牛", "生肖牛", "牛生肖"],
    "虎": ["属虎", "生肖虎", "虎生肖"],
    "兔": ["属兔", "生肖兔", "兔生肖"],
    "龙": ["属龙", "生肖龙", "龙生肖"],
    "蛇": ["属蛇", "生肖蛇", "蛇生肖"],
    "马": ["属马", "生肖马", "马生肖"],
    "羊": ["属羊", "生肖羊", "羊生肖"],
    "猴": ["属猴", "生肖猴", "猴生肖"],
    "鸡": ["属鸡", "生肖鸡", "鸡生肖"],
    "狗": ["属狗", "生肖狗", "狗生肖"],
    "猪": ["属猪", "生肖猪", "猪生肖"],
}
ZODIAC_TITLE_INDEX = {
    "白羊座": 0,
    "金牛座": 1,
    "双子座": 2,
    "巨蟹座": 3,
    "狮子座": 4,
    "处女座": 5,
    "天秤座": 6,
    "天蝎座": 7,
    "射手座": 8,
    "摩羯座": 9,
    "水瓶座": 10,
    "双鱼座": 11,
}
SHENGXIAO_TITLE_INDEX = {
    "鼠": 0,
    "牛": 1,
    "虎": 2,
    "兔": 3,
    "龙": 4,
    "蛇": 5,
    "马": 6,
    "羊": 7,
    "猴": 8,
    "鸡": 9,
    "狗": 10,
    "猪": 11,
}
CHINESE_NEW_YEAR_DATES = {
    1900: "1900-01-31", 1901: "1901-02-19", 1902: "1902-02-08", 1903: "1903-01-29", 1904: "1904-02-16",
    1905: "1905-02-04", 1906: "1906-01-25", 1907: "1907-02-13", 1908: "1908-02-02", 1909: "1909-01-22",
    1910: "1910-02-10", 1911: "1911-01-30", 1912: "1912-02-18", 1913: "1913-02-06", 1914: "1914-01-26",
    1915: "1915-02-14", 1916: "1916-02-03", 1917: "1917-01-23", 1918: "1918-02-11", 1919: "1919-02-01",
    1920: "1920-02-20", 1921: "1921-02-08", 1922: "1922-01-28", 1923: "1923-02-16", 1924: "1924-02-05",
    1925: "1925-01-24", 1926: "1926-02-13", 1927: "1927-02-02", 1928: "1928-01-23", 1929: "1929-02-10",
    1930: "1930-01-30", 1931: "1931-02-17", 1932: "1932-02-06", 1933: "1933-01-26", 1934: "1934-02-14",
    1935: "1935-02-04", 1936: "1936-01-24", 1937: "1937-02-11", 1938: "1938-01-31", 1939: "1939-02-19",
    1940: "1940-02-08", 1941: "1941-01-27", 1942: "1942-02-15", 1943: "1943-02-05", 1944: "1944-01-25",
    1945: "1945-02-13", 1946: "1946-02-02", 1947: "1947-01-22", 1948: "1948-02-10", 1949: "1949-01-29",
    1950: "1950-02-17", 1951: "1951-02-06", 1952: "1952-01-27", 1953: "1953-02-14", 1954: "1954-02-03",
    1955: "1955-01-24", 1956: "1956-02-12", 1957: "1957-01-31", 1958: "1958-02-18", 1959: "1959-02-08",
    1960: "1960-01-28", 1961: "1961-02-15", 1962: "1962-02-05", 1963: "1963-01-25", 1964: "1964-02-13",
    1965: "1965-02-02", 1966: "1966-01-21", 1967: "1967-02-09", 1968: "1968-01-30", 1969: "1969-02-17",
    1970: "1970-02-06", 1971: "1971-01-27", 1972: "1972-02-15", 1973: "1973-02-03", 1974: "1974-01-23",
    1975: "1975-02-11", 1976: "1976-01-31", 1977: "1977-02-18", 1978: "1978-02-07", 1979: "1979-01-28",
    1980: "1980-02-16", 1981: "1981-02-05", 1982: "1982-01-25", 1983: "1983-02-13", 1984: "1984-02-02",
    1985: "1985-02-20", 1986: "1986-02-09", 1987: "1987-01-29", 1988: "1988-02-17", 1989: "1989-02-06",
    1990: "1990-01-27", 1991: "1991-02-15", 1992: "1992-02-04", 1993: "1993-01-23", 1994: "1994-02-10",
    1995: "1995-01-31", 1996: "1996-02-19", 1997: "1997-02-07", 1998: "1998-01-28", 1999: "1999-02-16",
    2000: "2000-02-05", 2001: "2001-01-24", 2002: "2002-02-12", 2003: "2003-02-01", 2004: "2004-01-22",
    2005: "2005-02-09", 2006: "2006-01-29", 2007: "2007-02-18", 2008: "2008-02-07", 2009: "2009-01-26",
    2010: "2010-02-14", 2011: "2011-02-03", 2012: "2012-01-23", 2013: "2013-02-10", 2014: "2014-01-31",
    2015: "2015-02-19", 2016: "2016-02-08", 2017: "2017-01-28", 2018: "2018-02-16", 2019: "2019-02-05",
    2020: "2020-01-25", 2021: "2021-02-12", 2022: "2022-02-01", 2023: "2023-01-22", 2024: "2024-02-10",
    2025: "2025-01-29", 2026: "2026-02-17", 2027: "2027-02-06", 2028: "2028-01-26", 2029: "2029-02-13",
    2030: "2030-02-03", 2031: "2031-01-23", 2032: "2032-02-11", 2033: "2033-01-31", 2034: "2034-02-19",
    2035: "2035-02-08", 2036: "2036-01-28", 2037: "2037-02-15", 2038: "2038-02-04", 2039: "2039-01-24",
    2040: "2040-02-12", 2041: "2041-02-01", 2042: "2042-01-22", 2043: "2043-02-10", 2044: "2044-01-30",
    2045: "2045-02-17", 2046: "2046-02-06", 2047: "2047-01-26", 2048: "2048-02-14", 2049: "2049-02-02",
    2050: "2050-01-23", 2051: "2051-02-11", 2052: "2052-02-01", 2053: "2053-02-19", 2054: "2054-02-08",
    2055: "2055-01-28", 2056: "2056-02-15", 2057: "2057-02-04", 2058: "2058-01-24", 2059: "2059-02-12",
    2060: "2060-02-02", 2061: "2061-01-21", 2062: "2062-02-09", 2063: "2063-01-29", 2064: "2064-02-17",
    2065: "2065-02-05", 2066: "2066-01-26", 2067: "2067-02-14", 2068: "2068-02-03", 2069: "2069-01-23",
    2070: "2070-02-11", 2071: "2071-01-31", 2072: "2072-02-19", 2073: "2073-02-07", 2074: "2074-01-27",
    2075: "2075-02-15", 2076: "2076-02-05", 2077: "2077-01-24", 2078: "2078-02-12", 2079: "2079-02-02",
    2080: "2080-01-22", 2081: "2081-02-09", 2082: "2082-01-29", 2083: "2083-02-17", 2084: "2084-02-06",
    2085: "2085-01-26", 2086: "2086-02-14", 2087: "2087-02-03", 2088: "2088-01-24", 2089: "2089-02-10",
    2090: "2090-01-30", 2091: "2091-02-18", 2092: "2092-02-07", 2093: "2093-01-27", 2094: "2094-02-15",
    2095: "2095-02-05", 2096: "2096-01-25", 2097: "2097-02-12", 2098: "2098-02-01", 2099: "2099-01-21",
    2100: "2100-02-09",
}
ZESHI_INCIDENT_MAP = {
    0: ["搬家", "迁徙", "乔迁"],
    1: ["装修", "修造"],
    2: ["入宅"],
    3: ["订婚", "纳采", "结婚"],
    4: ["领证", "嫁娶"],
    5: ["求嗣", "破腹产"],
    6: ["纳财"],
    7: ["开市", "开业"],
    8: ["交易", "签约", "成交"],
    9: ["置产", "买房", "购房"],
    10: ["动土"],
    11: ["出行", "旅行", "远行"],
    12: ["安葬"],
    13: ["祭祀"],
    14: ["祈福", "许愿"],
    15: ["沐浴"],
    16: ["订盟"],
    17: ["纳婿"],
    18: ["修坟"],
    19: ["破土"],
    20: ["安葬"],
    21: ["立碑"],
    22: ["开生坟"],
    23: ["合寿木"],
    24: ["入殓"],
    25: ["移柩"],
    26: ["伐木"],
    27: ["掘井"],
    28: ["挂匾"],
    29: ["栽种"],
    30: ["入学", "上学"],
    31: ["理发", "剪头发"],
    32: ["会亲友", "见亲友", "见朋友"],
    33: ["赴任", "入职", "上任"],
    34: ["求医", "看病", "就医"],
    35: ["治病"],
}
ZODIAC_KEYWORD_PATTERN = re.compile(
    r"(星座|流年|年运|月运|周运|运势|桃花|感情|财运|事业|学业|贵人|配对|合盘|复合|水逆)"
)
SHENGXIAO_KEYWORD_PATTERN = re.compile(r"(生肖|属相|属[鼠牛虎兔龙蛇马羊猴鸡狗猪]|运势|年运|月运|周运|今日|今天|明日|明天)")
ZODIAC_FORBIDDEN_BAZI_TERMS = re.compile(r"(八字|四柱|日主|喜用|忌神|五行|地支|天干)")


def _extract_zodiac_sign(query: str) -> str:
    q = str(query or "")
    for canonical, aliases in ZODIAC_SIGN_ALIASES.items():
        for alias in aliases:
            if alias and alias in q:
                return canonical
    return ""


def _extract_shengxiao(query: str) -> str:
    q = str(query or "")
    for canonical, aliases in SHENGXIAO_ALIASES.items():
        for alias in aliases:
            if alias and alias in q:
                return canonical
    return ""


def _parse_profile_birthdate(profile: dict[str, str] | None) -> datetime | None:
    profile = profile or {}
    raw = str(profile.get("birthdate") or "").strip()
    if not raw:
        return None
    try:
        normalized = raw.replace("年", "-").replace("月", "-").replace("日", "")
        return datetime.strptime(normalized, "%Y-%m-%d")
    except Exception:
        return None


def _infer_zodiac_sign_from_birthdate(profile: dict[str, str] | None) -> str:
    birth_dt = _parse_profile_birthdate(profile)
    if not birth_dt:
        return ""
    month_day = (birth_dt.month, birth_dt.day)
    boundaries = [
        ((1, 20), "水瓶座"),
        ((2, 19), "双鱼座"),
        ((3, 21), "白羊座"),
        ((4, 20), "金牛座"),
        ((5, 21), "双子座"),
        ((6, 22), "巨蟹座"),
        ((7, 23), "狮子座"),
        ((8, 23), "处女座"),
        ((9, 23), "天秤座"),
        ((10, 24), "天蝎座"),
        ((11, 23), "射手座"),
        ((12, 22), "摩羯座"),
    ]
    for boundary, sign in reversed(boundaries):
        if month_day >= boundary:
            return sign
    return "摩羯座"


def _infer_shengxiao_from_birthdate(profile: dict[str, str] | None) -> str:
    birth_dt = _parse_profile_birthdate(profile)
    if not birth_dt:
        return ""
    new_year_text = CHINESE_NEW_YEAR_DATES.get(birth_dt.year)
    zodiac_year = birth_dt.year
    if new_year_text:
        try:
            new_year_dt = datetime.strptime(new_year_text, "%Y-%m-%d")
            if birth_dt.date() < new_year_dt.date():
                zodiac_year -= 1
        except Exception:
            pass
    animals = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
    return animals[(zodiac_year - 1900) % 12]


def _extract_query_year(query: str) -> int:
    q = str(query or "")
    m = re.search(r"\b(20\d{2})\b", q)
    if m:
        try:
            y = int(m.group(1))
            if 2000 <= y <= 2100:
                return y
        except Exception:
            pass
    return datetime.now().year


def _detect_zodiac_scope(query: str, anchor_year: int) -> tuple[str, str]:
    q = str(query or "")
    explicit_years = [int(x) for x in re.findall(r"(?<!\d)(20\d{2})(?!\d)", q)]
    if explicit_years and any(year != anchor_year for year in explicit_years):
        return "llm_scope", "对应年份"
    if re.search(r"(明天|明日)", q):
        return "明日运势", "明日"
    if re.search(r"(今天|今日)", q):
        return "今日运势", "今日"
    if re.search(r"(本周|这周|下周)", q):
        return "本周运势", "本周"
    if re.search(r"(本月|这个月)", q):
        return "本月运势", "本月"
    if re.search(r"(今年|本年)", q):
        return "本年运势", "今年"
    if re.search(r"(明年|后年|去年|前年)", q):
        return "llm_scope", "对应年份"
    return "本周运势", "本周"


def is_zodiac_query(query: str) -> bool:
    q = str(query or "")
    sign = _extract_zodiac_sign(q)
    if not sign:
        return False
    return bool(ZODIAC_KEYWORD_PATTERN.search(q))


def is_shengxiao_query(query: str) -> bool:
    q = str(query or "")
    animal = _extract_shengxiao(q)
    if not animal:
        return False
    return bool(SHENGXIAO_KEYWORD_PATTERN.search(q))


def is_zodiac_intent_query(query: str) -> bool:
    q = str(query or "")
    if is_zodiac_query(q):
        return True
    if is_shengxiao_query(q):
        return True
    if ("星座" in q) and bool(ZODIAC_KEYWORD_PATTERN.search(q)):
        return True
    return bool(re.search(r"(生肖|属相)", q)) and bool(SHENGXIAO_KEYWORD_PATTERN.search(q))


def _zodiac_default_reply(sign: str, year: int, topic: str, scope_cn: str) -> str:
    topic_cn = _topic_cn(topic)
    return (
        f"呀哈～先给你结论：{sign}{scope_cn}的{topic_cn}更适合走“先稳后发”的节奏。\n"
        "【关键触发点】\n"
        "1. 节奏触发：当你把目标收敛到1-2个核心项，推进效率会明显上升。\n"
        "2. 人际触发：主动同步进展比闷头做更容易拿到资源和反馈。\n"
        "3. 决策触发：重要选择先列风险再列收益，错判率会下降。\n"
        "【风险窗口】\n"
        "1. 连续高压周：容易情绪化决策，先延迟24小时再拍板。\n"
        "2. 信息过载期：容易分心，先保住主线任务。\n"
        "【行动建议】\n"
        "1. 每周固定一次复盘：保留、停止、新增各1条。\n"
        "2. 先做最关键的25分钟深度任务，再处理碎事。\n"
        "3. 对外沟通前写3句结论，减少反复解释成本。\n"
        "【马上可做的一步】\n"
        "先把最近最重要的一件事写进日程，并锁定第一段执行时间。"
    )


def _shengxiao_default_reply(animal: str, topic: str) -> str:
    topic_cn = _topic_cn(topic)
    return (
        f"呀哈～先给你结论：属{animal}的你，这段时间{topic_cn}更适合走“先稳后发”的节奏。\n"
        "先把最关键的一件事排到前面，别同时摊太多线；遇到重要决定时，先收信息，再定动作。"
    )


def _render_zodiac_llm_reply(query: str, sign: str, year: int, topic: str, scope_cn: str) -> tuple[str, dict]:
    topic_cn = _topic_cn(topic)
    prompt = ChatPromptTemplate.from_template(
        """你是“吉伊大师”，请结合星座信息回答用户关于{sign}{scope_cn}{topic_cn}的问题。
要求：
1) 使用吉伊口吻，温柔自然，可少量加入“呀哈/呜啦/本鼠鼠”。
2) 先给清晰结论，再解释触发点与风险窗口，最后给1-3条可执行建议。
3) 不要使用八字/五行/日主/喜用神等术语。
4) 不要固定骨架标题，避免模板化语句。
5) 回答必须严格对应“{scope_cn}”这个时间尺度，不要把本周写成今日，也不要把今日写成本周。
6) 不要出现“参考强度”“参考分值”“综合分数”等措辞。

用户问题：{query}
"""
    )
    try:
        chain = prompt | get_lc_ali_model_client(temperature=0.45, streaming=False) | StrOutputParser()
        text = str(chain.invoke({"year": year, "sign": sign, "topic_cn": topic_cn, "query": query, "scope_cn": scope_cn}) or "").strip()
        if not text:
            return _zodiac_default_reply(sign, year, topic, scope_cn), {"topic": topic, "source": "zodiac_fallback"}
        if ZODIAC_FORBIDDEN_BAZI_TERMS.search(text):
            text = re.sub(r"(八字|四柱|日主|喜用神?|忌神|五行|地支|天干)", "星盘线索", text)
        text = re.sub(r"参考强度[:：].*", "", text)
        text = re.sub(r"参考分值[:：].*", "", text)
        return _ensure_jiyi_tone(text), {"topic": topic, "source": "zodiac_llm"}
    except Exception:
        return _zodiac_default_reply(sign, year, topic, scope_cn), {"topic": topic, "source": "zodiac_fallback"}


def route_zodiac_pipeline(
    query: str,
    allow_clarify: bool = True,
    flags: dict[str, bool] | None = None,
    profile: dict[str, str] | None = None,
) -> tuple[str | None, dict | None]:
    q = str(query or "").strip()
    if not q:
        return None, None
    if not is_zodiac_intent_query(q):
        return None, None
    active_flags = flags or dict(FEATURE_FLAG_DEFAULTS)
    sign = _extract_zodiac_sign(q)
    animal = _extract_shengxiao(q)
    inferred_sign = _infer_zodiac_sign_from_birthdate(profile) if not sign else ""
    inferred_animal = _infer_shengxiao_from_birthdate(profile) if not animal else ""
    if not sign and ("星座" in q):
        sign = inferred_sign
    if not animal and re.search(r"(生肖|属相)", q):
        animal = inferred_animal
    if not sign and not animal:
        if not allow_clarify:
            return None, None
        if "星座" in q:
            return (
                "呀哈～你想看星座运势我收到了。先告诉我你的出生年月日，或者直接告诉我星座（例如白羊座/天蝎座），我再给你本周重点和行动建议。",
                {"topic": "zodiac", "source": "zodiac_clarify", "question_type": "clarify"},
            )
        return (
            "呀哈～你想看生肖运势我收到了。先告诉我你的出生年月日，或者直接告诉我生肖（例如属龙/属狗），我再给你这段时间的重点提醒。",
            {"topic": "zodiac", "source": "shengxiao_clarify", "question_type": "clarify"},
        )
    year = _extract_query_year(q)
    topic = detect_fortune_topic(q)
    scope_key, scope_cn = _detect_zodiac_scope(q, year)
    provider_fallback_reason = ""
    failed_quota_state = ""
    can_use_provider_scope = scope_key != "llm_scope"
    if sign and active_flags.get("zodiac_api_v1") and can_use_provider_scope:
        result = run_yuanfenju_zodiac_yunshi(
            label=sign,
            title_yunshi=ZODIAC_TITLE_INDEX.get(sign, 0),
            entity_type=0,
            topic=topic,
            scope_key=scope_key,
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        if result.get("ok") and str(result.get("text") or "").strip():
            return str(result.get("text") or ""), {
                "topic": topic,
                "source": "yuanfenju_zhanbu_yunshi",
                "question_type": "zodiac",
                "provider_id": str(result.get("provider_id") or "yuanfenju_zhanbu_yunshi"),
                "provider_calls": int(result.get("provider_calls") or 0),
                "provider_fallback_reason": "",
                "quota_state": str(result.get("quota_state") or "healthy"),
                "inferred_from_profile": bool(inferred_sign),
                "scope_cn": scope_cn,
            }
        provider_fallback_reason = "provider_error" if result else "provider_disabled"
        failed_quota_state = str(result.get("quota_state") or "")
    elif animal and active_flags.get("zodiac_api_v1") and can_use_provider_scope:
        result = run_yuanfenju_zodiac_yunshi(
            label=f"属{animal}",
            title_yunshi=SHENGXIAO_TITLE_INDEX.get(animal, 0),
            entity_type=1,
            topic=topic,
            scope_key=scope_key,
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        if result.get("ok") and str(result.get("text") or "").strip():
            return str(result.get("text") or ""), {
                "topic": topic,
                "source": "yuanfenju_zhanbu_yunshi",
                "question_type": "zodiac",
                "provider_id": str(result.get("provider_id") or "yuanfenju_zhanbu_yunshi"),
                "provider_calls": int(result.get("provider_calls") or 0),
                "provider_fallback_reason": "",
                "quota_state": str(result.get("quota_state") or "healthy"),
                "inferred_from_profile": bool(inferred_animal),
                "scope_cn": scope_cn,
            }
        provider_fallback_reason = "provider_error" if result else "provider_disabled"
        failed_quota_state = str(result.get("quota_state") or "")
    else:
        provider_fallback_reason = "provider_disabled"

    if sign:
        text, meta = _render_zodiac_llm_reply(q, sign, year, topic, scope_cn)
        meta["provider_fallback_reason"] = provider_fallback_reason
        meta["quota_state"] = failed_quota_state
        meta["question_type"] = "zodiac"
        meta["inferred_from_profile"] = bool(inferred_sign)
        meta["scope_cn"] = scope_cn
        return text, meta
    return _shengxiao_default_reply(animal, topic), {
        "topic": topic,
        "source": "shengxiao_fallback",
        "question_type": "zodiac",
        "provider_fallback_reason": provider_fallback_reason or "fallback",
        "inferred_from_profile": bool(inferred_animal),
        "scope_cn": scope_cn,
    }


def _is_short_fortune_decision_hit(query: str) -> bool:
    q = str(query or "")
    if not q:
        return False
    if FORTUNE_SHORT_DECISION_PATTERN.search(q):
        return True
    m = FORTUNE_CHOICE_PATTERN.search(q)
    if not m:
        return False
    left = re.sub(r"\s+", "", m.group(1))
    right = re.sub(r"\s+", "", m.group(2))
    combined = f"{left}{right}"
    if any(x in combined for x in ["开源", "守财", "扩收入", "控支出", "换岗", "积累"]):
        return True
    return bool(FORTUNE_DOMAIN_HINT_PATTERN.search(combined))


def _route_reason_for_fortune_query(query: str) -> str:
    if not _intent_routing_v3_enabled():
        return "none"
    q = str(query or "")
    if _is_short_fortune_decision_hit(q):
        return "fortune_short_decision_hit"
    if FORTUNE_ACTION_PATTERN.search(q) and not FORTUNE_SCENE_PATTERN.search(q):
        return "fortune_short_action_hit"
    return "none"


def is_bazi_fortune_query(query: str) -> bool:
    q = str(query or "")
    if is_zodiac_intent_query(q):
        # 星座问法走独立占星链路，避免混入八字术语。
        return False
    if BAZI_FORTUNE_QUERY_PATTERN.search(q):
        return True
    if _intent_routing_v3_enabled() and _is_short_fortune_decision_hit(q):
        return True
    # 覆盖“弱命理意图”问法：未显式写“运势/八字”，但明显在问提运/时运决策。
    if FORTUNE_DECISION_PATTERN.search(q) and FORTUNE_SCENE_PATTERN.search(q):
        return True
    if _intent_routing_v3_enabled() and FORTUNE_ACTION_PATTERN.search(q) and (
        FORTUNE_SCENE_PATTERN.search(q) or FORTUNE_DOMAIN_HINT_PATTERN.search(q) or FORTUNE_DECISION_PATTERN.search(q)
    ):
        return True
    # 覆盖“口语化问运势”表达（如：近哪几天气场更顺）。
    if FORTUNE_COLLOQUIAL_PATTERN.search(q) and FORTUNE_SCENE_PATTERN.search(q):
        return True
    return False


def is_divination_query(query: str) -> bool:
    return bool(DIVINATION_QUERY_PATTERN.search(str(query or "")))


def is_dream_query(query: str) -> bool:
    q = str(query or "")
    return bool(DREAM_QUERY_PATTERN.search(q))


def detect_domain_intent(query: str) -> str:
    q = str(query or "")
    if is_dream_query(q):
        return "dream"
    if is_divination_query(q):
        return "divination"
    if is_zodiac_intent_query(q):
        return "zodiac"
    if is_bazi_fortune_query(q):
        return "fortune"
    if is_time_sensitive_query(q):
        return "time"
    return "general"


def detect_question_type(query: str) -> str:
    q = str(query or "")
    if not q:
        return "default"
    if _is_identity_fact_query(q):
        return "identity_fact"
    if is_dream_query(q):
        return "dream"
    if is_time_sensitive_query(q) and not (is_zodiac_intent_query(q) or is_bazi_fortune_query(q)):
        return "time"
    if is_zodiac_intent_query(q) and not _extract_zodiac_sign(q):
        if "星座" in q or not _extract_shengxiao(q):
            return "clarify"
    if _intent_routing_v3_enabled() and _is_short_fortune_decision_hit(q):
        return "decision"
    if FORTUNE_DECISION_PATTERN.search(q):
        return "decision"
    if FORTUNE_ACTION_PATTERN.search(q):
        return "action"
    if FORTUNE_COLLOQUIAL_PATTERN.search(q):
        return "colloquial"
    if FORTUNE_TREND_PATTERN.search(q):
        return "trend"
    if re.search(
        r"(今年|本年|明年|后年|去年|前年|全年|年度|上半年|下半年|(?:未来|接下来)(?:的)?(?:[一二两三四五六七八九]|[1-9])年|(?:[一二两三四五六七八九]|[1-9])年内)",
        q,
    ):
        return "trend"
    return "default"


def detect_fortune_topic(query: str) -> str:
    q = str(query or "")
    if re.search(r"(桃花|姻缘|婚缘|婚运|结婚|婚期|成婚|正缘|另一半|配偶|感情|恋爱)", q):
        return "love"
    if re.search(r"(财运|财富|收入|金钱)", q):
        return "wealth"
    if re.search(r"(事业|工作|职场|升职)", q):
        return "career"
    if re.search(r"(学业|考试|学习)", q):
        return "study"
    return "daily"


def _extract_target_years(query: str, anchor_year: int) -> list[int]:
    q = str(query or "")
    explicit = [int(x) for x in re.findall(r"(?<!\d)(20\d{2})(?:年)?(?!\d)", q)]
    if explicit:
        return sorted(set(explicit))
    years: list[int] = []
    mapping = {
        "前年": anchor_year - 2,
        "去年": anchor_year - 1,
        "今年": anchor_year,
        "本年": anchor_year,
        "明年": anchor_year + 1,
        "后年": anchor_year + 2,
    }
    for token, year in mapping.items():
        if token in q:
            years.append(year)
    if years:
        return sorted(set(years))
    m = re.search(r"(?:未来|接下来)(?:的)?([一二两三四五六七八九]|[1-9])年|([一二两三四五六七八九]|[1-9])年内", q)
    if m:
        count = _cn_num_to_int(str(m.group(1) or m.group(2) or "1")) or 1
        return [anchor_year + idx for idx in range(max(1, min(5, count)))]
    return []


def _is_daily_window_query(query: str) -> bool:
    q = str(query or "")
    return bool(re.search(r"(今天|今日|明天|明日)", q))


def _is_partner_profile_query(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    if re.search(r"(正缘|另一半|配偶|对象).*(画像|特征|长相|样子|什么样)", q):
        return True
    if not re.search(r"(正缘|另一半|配偶|对象)", q):
        return False
    if re.search(r"(什么时候|何时|几岁|哪年|哪一年|多大|婚期|结婚|成婚|趋势|走向|专题|发展)", q):
        return False
    return True


def _is_love_trend_query(query: str) -> bool:
    return bool(re.search(r"(姻缘|婚缘|婚运|桃花|感情).*(趋势|走向|专题|发展)", str(query or "")))


def _is_marriage_prediction_query(query: str) -> bool:
    q = str(query or "")
    return bool(
        re.search(
            r"((什么时候|何时|几岁|哪年|哪一年|多大|何年).*(结婚|成婚|步入婚姻)|"
            r"(结婚|婚期|成婚).*(什么时候|何时|几岁|哪年|哪一年|多大|预测|分析|怎么看|适合))",
            q,
        )
    )


def _extract_zeshi_incident(query: str) -> tuple[int, str] | None:
    q = str(query or "")
    if not re.search(r"(哪天|哪几天|适合|宜不宜|能不能|安排)", q):
        return None
    best_match: tuple[int, str] | None = None
    for incident_id, aliases in ZESHI_INCIDENT_MAP.items():
        for alias in aliases:
            if alias and alias in q:
                if not best_match or len(alias) > len(best_match[1]):
                    best_match = (incident_id, alias)
    return best_match


def _window_days_count(window_meta: dict | None) -> int:
    if not isinstance(window_meta, dict):
        return 7
    start = str(window_meta.get("window_start") or "").strip()
    end = str(window_meta.get("window_end") or "").strip()
    if not start or not end:
        return 7
    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
    except Exception:
        return 7
    return max(1, min(30, (end_dt.date() - start_dt.date()).days + 1))


def _resolve_zeshi_future_code(query: str, window_meta: dict | None) -> int:
    q = str(query or "").strip()
    if re.search(r"(今天|今日)", q):
        return 0
    days = _window_days_count(window_meta)
    if days <= 1:
        return 0
    if days <= 7:
        return 1
    if days <= 30:
        return 2
    return 3


def _missing_profile_fields_for_fortune(profile: dict[str, str]) -> list[str]:
    missing: list[str] = []
    if not str(profile.get("name") or "").strip():
        missing.append("name")
    if not str(profile.get("birthdate") or "").strip():
        missing.append("birthdate")
    if not _normalize_gender(str(profile.get("gender") or "")):
        missing.append("gender")
    return missing


def build_fortune_missing_reply(missing: list[str]) -> str:
    if missing == ["name"]:
        return "呀哈～我先补一个关键资料：请告诉我你的姓名（2-12个字）。"
    if missing == ["birthdate"]:
        return "呀哈～我还需要你的出生年月日（例如 2001-08-15），这样命理判断才更准。"
    if missing == ["gender"]:
        return "呀哈～我还需要你的性别（男/女），这样姻缘、流年和八字接口才能按正确参数来算。"
    if missing == ["name", "birthdate"]:
        return "呀哈～我先帮你把资料补齐：请告诉我姓名和出生年月日（例如 2001-08-15；知道时辰也可以一起说）。"
    if missing == ["name", "gender"]:
        return "呀哈～我还差两项关键资料：请告诉我你的姓名和性别（男/女）。"
    if missing == ["birthdate", "gender"]:
        return "呀哈～我还需要你的出生年月日和性别（男/女），这样命理接口才能按完整参数来算。"
    return "呀哈～我先帮你把资料补齐：请告诉我姓名、出生年月日和性别（男/女）；知道时辰也可以一起说。"


def _default_fortune_advice(topic: str, strength: str) -> list[str]:
    table = {
        "daily": [
            "今天先完成一件最重要的小事，连续投入25分钟。",
            "把待办压到3项以内，先完成再扩展。",
            "晚上用3分钟复盘今天最顺和最卡的点。",
        ],
        "love": [
            "今天主动发一次轻量关心，不求长聊但求真诚。",
            "表达感受时用'我感受'句式，减少猜测。",
            "关系不确定时，48小时内不做冲动决定。",
        ],
        "wealth": [
            "今天先做一项与收入直接相关的动作。",
            "先记账再消费，避免情绪性花销。",
            "高风险决策设置24小时冷静期。",
        ],
        "career": [
            "先推进一个可量化产出点，别同时开太多线。",
            "把本周关键结果整理成3句汇报。",
            "遇到卡点先找一个能给反馈的人快速对齐。",
        ],
        "study": [
            "先做25分钟深度学习，再休息5分钟。",
            "先攻克最难的一节，建立正反馈。",
            "睡前做10分钟回顾，巩固关键知识点。",
        ],
    }
    advice = list(table.get(topic, table["daily"]))
    if strength == "strong":
        advice[0] = "状态可用，今天把最关键任务前置完成。"
    elif strength == "weak":
        advice[0] = "先稳住节奏，今天只设一个最小可完成目标。"
    return advice


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _normalize_structured_fortune_payload(raw, topic: str) -> dict:
    base = {
        "topic": topic,
        "bazi": "",
        "day_master": "",
        "strength": "balanced",
        "xiyongshen": "",
        "jishen": "",
        "wuxing_scores": {"metal": 0, "wood": 0, "water": 0, "fire": 0, "earth": 0},
        "fortune_signals": {"love": "", "wealth": "", "career": ""},
        "risk_points": [],
        "opportunity_points": [],
        "time_hints": [],
        "evidence_lines": [],
        "advice": [],
        "confidence": 0.2,
        "source": "yuanfenju",
        "provider_id": "",
        "provider_calls": 0,
        "provider_fallback_reason": "",
        "upstream_errmsg": "",
        "quota_state": "",
        "profile_gender": "",
        "partner_portrait_image": "",
        "zhengyuan_profile": {},
        "jiehun_profile": {},
        "error": None,
    }
    payload = _as_dict(raw)
    if not payload:
        base["error"] = {"code": "FORTUNE_PARSE_FAILED", "message": "命理结果解析失败"}
        base["advice"] = _default_fortune_advice(topic, "balanced")
        return base

    for key in [
        "topic",
        "bazi",
        "day_master",
        "strength",
        "xiyongshen",
        "jishen",
        "source",
        "provider_id",
        "provider_fallback_reason",
        "upstream_errmsg",
        "quota_state",
        "profile_gender",
        "partner_portrait_image",
    ]:
        if key in payload:
            base[key] = str(payload.get(key) or base[key])

    raw_zhengyuan_profile = payload.get("zhengyuan_profile") or {}
    if isinstance(raw_zhengyuan_profile, dict):
        base["zhengyuan_profile"] = raw_zhengyuan_profile
    raw_jiehun_profile = payload.get("jiehun_profile") or {}
    if isinstance(raw_jiehun_profile, dict):
        base["jiehun_profile"] = raw_jiehun_profile

    try:
        base["provider_calls"] = max(0, int(payload.get("provider_calls", 0)))
    except Exception:
        base["provider_calls"] = 0

    raw_scores = payload.get("wuxing_scores") or {}
    if isinstance(raw_scores, dict):
        for key in ["metal", "wood", "water", "fire", "earth"]:
            try:
                base["wuxing_scores"][key] = int(float(raw_scores.get(key, 0)))
            except Exception:
                base["wuxing_scores"][key] = 0

    raw_signals = payload.get("fortune_signals") or {}
    if isinstance(raw_signals, dict):
        for key in ["love", "wealth", "career"]:
            base["fortune_signals"][key] = str(raw_signals.get(key) or "")

    for key in ["risk_points", "opportunity_points", "time_hints", "evidence_lines"]:
        raw_list = payload.get(key) or []
        if isinstance(raw_list, list):
            base[key] = [str(x).strip() for x in raw_list if str(x).strip()][:4]

    raw_advice = payload.get("advice") or []
    if isinstance(raw_advice, list):
        base["advice"] = [str(x).strip() for x in raw_advice if str(x).strip()][:3]

    try:
        base["confidence"] = max(0.0, min(1.0, float(payload.get("confidence", 0.2))))
    except Exception:
        base["confidence"] = 0.2

    raw_error = payload.get("error")
    if isinstance(raw_error, dict):
        base["error"] = {
            "code": str(raw_error.get("code") or ""),
            "message": str(raw_error.get("message") or ""),
            "provider": str(raw_error.get("provider") or ""),
            "provider_code": str(raw_error.get("provider_code") or ""),
            "category": str(raw_error.get("category") or ""),
            "degraded": bool(raw_error.get("degraded")),
        }

    if base["strength"] not in {"strong", "weak", "balanced"}:
        base["strength"] = "balanced"
    if not base["advice"]:
        base["advice"] = _default_fortune_advice(topic, base["strength"])
    return base


def _topic_cn(topic: str) -> str:
    return {
        "daily": "综合运势",
        "love": "感情运势",
        "wealth": "财运走势",
        "career": "事业运势",
        "study": "学业运势",
    }.get(topic, "综合运势")


def _signal_for_topic(payload: dict, topic: str) -> str:
    signals = payload.get("fortune_signals") or {}
    mapping = {
        "love": "love",
        "wealth": "wealth",
        "career": "career",
        "study": "career",
        "daily": "career",
    }
    key = mapping.get(topic, "career")
    signal_text = ""
    if isinstance(signals, dict):
        signal_text = str(signals.get(key) or "").strip()
    if not _evidence_advice_v1_enabled():
        return signal_text[:120]

    segments: list[str] = []
    if signal_text:
        segments.append(signal_text)
    for field_name, prefix in [
        ("opportunity_points", "机会"),
        ("risk_points", "风险"),
        ("time_hints", "时间"),
        ("evidence_lines", "依据"),
    ]:
        items = payload.get(field_name) or []
        if isinstance(items, list):
            for item in items:
                clean = str(item or "").strip()
                if clean:
                    segments.append(f"{prefix}：{clean}")
                    break
    deduped: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        key_seg = re.sub(r"\s+", "", seg)
        if not key_seg or key_seg in seen:
            continue
        seen.add(key_seg)
        deduped.append(seg)
    return "；".join(deduped)[:180]


def _basis_line(payload: dict) -> str:
    basis_parts: list[str] = []
    bazi = str(payload.get("bazi") or "").strip()
    day_master = str(payload.get("day_master") or "").strip()
    xiyongshen = str(payload.get("xiyongshen") or "").strip()
    jishen = str(payload.get("jishen") or "").strip()
    if bazi:
        basis_parts.append(f"八字 {bazi}")
    if day_master:
        basis_parts.append(f"日主 {day_master}")
    if xiyongshen:
        basis_parts.append(f"喜用 {xiyongshen}")
    if jishen:
        basis_parts.append(f"忌神 {jishen}")
    if not basis_parts:
        return "以当前盘面趋势判断"
    return "；".join(basis_parts)


def _natural_basis_line(payload: dict) -> str:
    bazi = str(payload.get("bazi") or "").strip()
    day_master = str(payload.get("day_master") or "").strip()
    xiyongshen = str(payload.get("xiyongshen") or "").strip()
    jishen = str(payload.get("jishen") or "").strip()
    seed_text = "|".join([bazi, day_master, xiyongshen, jishen]) or "basis"
    parts: list[str] = []
    if bazi:
        parts.append(
            _pick_non_repeat(
                [
                    f"你这回的底色，先落在「{bazi}」这组组合上",
                    f"先托住判断的一层底子，是「{bazi}」这组气口",
                    f"我先看到的底盘，是「{bazi}」这组组合在发力",
                ],
                f"{seed_text}|bazi",
            )
        )
    if day_master:
        parts.append(
            _pick_non_repeat(
                [
                    f"你自己的性子更偏{day_master}这一路",
                    f"你的核心气质，是往{day_master}这边落的",
                    f"你本人的发力方式，更像{day_master}这一型",
                ],
                f"{seed_text}|day_master",
            )
        )
    if xiyongshen:
        parts.append(
            _pick_non_repeat(
                [
                    f"顺手的时候，往往是{str(xiyongshen)}这股气在托着你",
                    f"对你更友好的发力方向，会落在{str(xiyongshen)}这边",
                    f"你一顺起来，通常是沾着{str(xiyongshen)}这层气口",
                ],
                f"{seed_text}|xiyongshen",
            )
        )
    if jishen:
        parts.append(
            _pick_non_repeat(
                [
                    f"可一旦{str(jishen)}这边压得太重，人就容易发紧",
                    f"但要是{str(jishen)}这股劲儿过头，你就容易拧巴一点",
                    f"只是碰上{str(jishen)}偏重的时候，节奏会更容易卡住",
                ],
                f"{seed_text}|jishen",
            )
        )
    if not parts:
        return "我主要是顺着你这阵子的整体气口来判断"
    return "；".join(parts)


def _resolve_fortune_advice(payload: dict, topic: str, strength: str) -> list[str]:
    if _evidence_advice_v1_enabled():
        candidates: list[str] = []
        for key in ["opportunity_points", "risk_points", "time_hints", "evidence_lines"]:
            items = payload.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                clean = str(item or "").strip()
                if clean:
                    candidates.append(clean)
        if candidates:
            deduped: list[str] = []
            seen: set[str] = set()
            for item in candidates:
                norm = re.sub(r"\s+", "", item)
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                deduped.append(item)
                if len(deduped) >= 3:
                    break
            if deduped:
                return deduped
    advice = payload.get("advice") or _default_fortune_advice(topic, strength)
    return [str(x).strip() for x in advice if str(x).strip()][:3]


def _advice_signature(advice: list[str]) -> str:
    joined = "|".join([str(x).strip() for x in advice if str(x).strip()])
    if not joined:
        return ""
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _json_dumps_safe(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)


def _ensure_jiyi_tone(text: str) -> str:
    out = str(text or "").strip()
    if not out:
        return ""
    if re.search(r"(呀哈|呜啦|本鼠鼠|吉伊大师)", out):
        return out
    return f"呀哈～{out}"


def _generate_fortune_reply_with_model(
    payload: dict,
    topic: str,
    query: str,
    question_type: str,
    window_meta: dict | None = None,
) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    topic_cn = _topic_cn(topic)
    strength = str(payload.get("strength") or "balanced")
    advice = _resolve_fortune_advice(payload, topic, strength)
    window_text = ""
    window_label = ""
    if isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
    prompt = ChatPromptTemplate.from_template(
        """你是“吉伊大师”，需要根据工具返回的结构化命理结果，生成自然中文回复。
要求：
1) 保持吉伊口吻：温柔、轻可爱，适度使用“呀哈/呜啦/本鼠鼠”，但不要每句都用。
2) 先回答用户核心问题，避免空泛；如果是决策题，首句必须给明确方向（例如先A后B）。
3) 不要使用固定骨架标题（例如固定“结论/依据/建议”格式），优先自然段表达。
4) 如问题涉及趋势/口语时窗，并且提供了窗口信息，需自然写出明确时间窗口。
5) 结合“命理信号、依据、建议候选、证据点”组织内容，不要编造工具结果里不存在的细节。
6) 给1-3条可执行建议，可写成自然句或短列表；避免模板腔和重复句式。
7) 不要回显完整生日和时辰原文；如果用户主动问“我叫什么”或已给称呼偏好，可用真实姓名或昵称自然称呼。不要输出JSON。
8) 如果时间窗口跨度是“月/年/多年”，不要自动收缩为“近三天”。

输入信息：
- 用户问题：{query}
- 问题类型：{question_type}
- 主题：{topic_cn}
- 时间窗口标签：{window_label}
- 时间窗口：{window_text}
- 命理信号：{signal_line}
- 命理依据：{basis_line}
- 建议候选：{advice_text}
- 工具原始结果(JSON)：{payload_json}
"""
    )
    try:
        chain = prompt | get_lc_ali_model_client(temperature=0.55, streaming=False) | StrOutputParser()
        out = str(
            chain.invoke(
                {
                    "query": q,
                    "question_type": str(question_type or "default"),
                    "topic_cn": topic_cn,
                    "window_label": window_label or "none",
                    "window_text": window_text or "无",
                    "signal_line": _signal_for_topic(payload, topic) or "无",
                    "basis_line": _natural_basis_line(payload),
                    "advice_text": "；".join(advice) if advice else "无",
                    "payload_json": _json_dumps_safe(payload),
                }
            )
            or ""
        ).strip()
    except Exception:
        return ""
    if not out:
        return ""
    if str(question_type or "") in {"decision", "comparison"} and not _is_direct_answer_hit(q, out):
        out = f"先给你结论：{_decision_conclusion_from_query(q, strength)}。\n{out}"
    if (
        str(question_type or "") in {"trend", "colloquial"}
        and window_text
        and window_text not in out
        and _should_show_window_text(q, window_label, question_type=str(question_type or "default"))
    ):
        natural_window_line = _natural_window_line(q, window_text, window_label, question_type=str(question_type or "default"))
        if natural_window_line:
            out = f"{natural_window_line}\n{out}"
    if window_text and not _should_show_window_text(q, window_label, question_type=str(question_type or "default")):
        lines = [ln for ln in out.splitlines() if ln.strip()]
        filtered: list[str] = []
        for line in lines:
            if window_text and window_text in line:
                continue
            if re.search(r"(时间上先对齐|时间窗口|从\\d{1,2}月\\d{1,2}日到\\d{1,2}月\\d{1,2}日)", line):
                continue
            filtered.append(line)
        if filtered:
            out = "\n".join(filtered).strip()
    out = _soften_fortune_section_headings(out)
    long_horizon_labels = {
        "this_month",
        "next_30_days",
        "coming_period",
        "year_full",
        "year_h1",
        "year_h2",
        "year_partial",
        "one_year",
        "multi_year",
        "explicit_year",
        "explicit_year_span",
        "compare_year_span",
        "relative_year_span",
    }
    if window_label in long_horizon_labels:
        out = re.sub(r"接下来这三天[，、,:：\-\s]*", "", out)
        out = re.sub(r"最近三天", "这个时间范围内", out)
        out = re.sub(r"三天内", "这个阶段内", out)
        out = re.sub(r"这三天", "这个阶段", out)
    return _ensure_jiyi_tone(out)


def _format_dream_payload(raw) -> str:
    if isinstance(raw, dict):
        if int(raw.get("errcode", 0) or 0) != 0:
            return ""
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        ordered = []
        for k in ["title", "name", "梦境", "description", "解梦", "吉凶", "建议", "result", "content"]:
            val = str(data.get(k) or "").strip()
            if val:
                ordered.append(f"{k}：{val}")
        if not ordered:
            ordered = [f"{k}：{v}" for k, v in data.items() if str(v).strip()]
        return "\n".join(ordered[:8])
    return str(raw or "").strip()


def _generate_dream_reply_with_model(query: str, raw) -> str:
    q = str(query or "").strip()
    detail = _format_dream_payload(raw)
    if not q or not detail:
        return ""
    prompt = ChatPromptTemplate.from_template(
        """你是“吉伊大师”。请基于解梦工具结果回答用户，要求：
1) 使用吉伊口吻（温柔、轻可爱），自然可读，不要模板骨架。
2) 先给一句结论，再解释梦境含义，最后给1-2条可执行建议。
3) 不编造工具结果之外的事实，不输出JSON。

用户问题：{query}
工具结果：{detail}
"""
    )
    try:
        chain = prompt | get_lc_ali_model_client(temperature=0.5, streaming=False) | StrOutputParser()
        out = str(chain.invoke({"query": q, "detail": detail}) or "").strip()
    except Exception:
        return ""
    return _ensure_jiyi_tone(out)


def _generate_divination_reply_with_model(query: str, raw) -> str:
    q = str(query or "").strip()
    detail = _format_divination_reply(raw)
    if not q or not detail:
        return ""
    prompt = ChatPromptTemplate.from_template(
        """你是“吉伊大师”。请根据卦象结果回答用户：
1) 保持吉伊口吻，先给结论，再解释卦象，再给1-2条行动建议。
2) 不要固定标题骨架，不要输出JSON。
3) 不要编造卦象中没有的信息。

用户问题：{query}
卦象结果：{detail}
"""
    )
    try:
        chain = prompt | get_lc_ali_model_client(temperature=0.5, streaming=False) | StrOutputParser()
        out = str(chain.invoke({"query": q, "detail": detail}) or "").strip()
    except Exception:
        return ""
    return _ensure_jiyi_tone(out)


FORTUNE_BLUEPRINT_LIBRARY = {
    "decision": ["decision_direct", "decision_risk_first", "decision_stepwise"],
    "action": ["action_direct", "action_window_then_step", "action_signal_focus"],
    "trend": ["trend_window_first", "trend_signal_first", "trend_balanced"],
    "colloquial": ["colloquial_window_first", "colloquial_signal_first", "colloquial_balanced"],
    "default": ["default_concise", "default_signal_first", "default_balanced"],
}


def _select_fortune_blueprint(question_type: str, session_id: str, query_hash: str) -> str:
    qtype = str(question_type or "default")
    candidates = FORTUNE_BLUEPRINT_LIBRARY.get(qtype, FORTUNE_BLUEPRINT_LIBRARY["default"])
    if not candidates:
        return "default_concise"
    seed_src = f"{qtype}|{session_id}|{query_hash}"
    seed = hashlib.sha256(seed_src.encode("utf-8")).hexdigest()
    idx = int(seed[:8], 16) % len(candidates)
    selected = candidates[idx]
    if session_id:
        try:
            key = _last_blueprint_key(session_id)
            last = str(_REDIS_CLIENT.get(key) or "")
            if last == selected and len(candidates) > 1:
                selected = candidates[(idx + 1) % len(candidates)]
            _REDIS_CLIENT.setex(key, SESSION_TTL_SECONDS, selected)
        except Exception:
            pass
    return selected


def _render_fortune_with_blueprint(
    blueprint_id: str,
    conclusion_line: str,
    window_line: str,
    signal_line: str,
    basis_line: str,
    advice: list[str],
) -> str:
    lines: list[str] = [f"结论：{conclusion_line}。"]
    advice_lines = [f"{idx}. {tip}" for idx, tip in enumerate(advice, start=1)]
    if blueprint_id in {"decision_risk_first", "trend_signal_first", "colloquial_signal_first", "action_signal_focus", "default_signal_first"}:
        if signal_line:
            lines.append(f"命理信号：{signal_line}")
        if window_line:
            lines.append(window_line)
    elif blueprint_id in {"decision_stepwise", "action_window_then_step", "trend_window_first", "colloquial_window_first"}:
        if window_line:
            lines.append(window_line)
        if signal_line:
            lines.append(f"命理信号：{signal_line}")
    else:
        if signal_line:
            lines.append(f"命理信号：{signal_line}")
        if window_line:
            lines.append(window_line)
    lines.append(f"依据：{basis_line}。")
    if advice_lines:
        lines.append("建议：")
        lines.extend(advice_lines)
    return _soften_fortune_section_headings("\n".join(lines))


def _render_user_fortune_reply_v2_legacy(
    payload: dict,
    topic: str,
    query: str,
    question_type: str,
    window_meta: dict | None = None,
) -> str:
    topic_cn = _topic_cn(topic)
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("code") or ""):
        code = str(error.get("code") or "")
        msg = str(error.get("message") or "命理链路暂时不可用")
        return (
            f"呀哈～这次{topic_cn}盘面暂时没取全（{code}）。{msg}。"
            f"先给你一个稳妥方向：{_default_fortune_advice(topic, 'balanced')[0]}"
        )

    strength = str(payload.get("strength") or "balanced")
    strength_text = {
        "strong": "势能偏强，适合主动推进",
        "weak": "势能偏谨慎，先稳节奏更顺",
        "balanced": "节奏偏平衡，适合稳中求进",
    }.get(strength, "节奏偏平衡，适合稳中求进")

    lines: list[str] = []
    if question_type in {"decision", "comparison"}:
        conclusion = _decision_conclusion_from_query(query, strength)
        lines.append(f"结论：{conclusion}。")
    else:
        lines.append(f"结论：这次{topic_cn}{strength_text}。")

    if question_type in {"trend", "colloquial"} and isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
        if window_text and _should_show_window_text(query, window_label, question_type=question_type):
            lines.append(_natural_window_line(query, window_text, window_label, question_type=question_type))

    signal_line = _signal_for_topic(payload, topic)
    if signal_line:
        lines.append(f"命理信号：{signal_line}")

    lines.append(f"依据：{_natural_basis_line(payload)}。")
    advice = _resolve_fortune_advice(payload, topic, strength)
    payload["advice_signature"] = _advice_signature(advice)
    if advice:
        lines.append("建议：")
        for idx, tip in enumerate(advice, start=1):
            lines.append(f"{idx}. {tip}")
    payload["blueprint_id"] = "legacy_v2"
    payload["_render_blueprint_id"] = "legacy_v2"
    return _soften_fortune_section_headings("\n".join(lines))


def _first_sentence(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    head = re.split(r"[。！？!\n]", raw, maxsplit=1)[0]
    return head.strip()


def _decision_conclusion_from_query(query: str, strength: str) -> str:
    q = str(query or "")
    if ("开源" in q and "守财" in q) or ("先开源" in q and "先守" in q):
        if strength == "strong":
            return "先开源，再守财，走“稳开”路线"
        if strength == "weak":
            return "先守财，再小步开源"
        return "守中带开：先守住现金流，再扩开源"
    if "扩收入" in q and "控支出" in q:
        if strength == "strong":
            return "先扩收入，同时保留基础控支出"
        if strength == "weak":
            return "先控支出，等节奏稳住后再扩收入"
        return "先控支出打底，再小步扩收入"
    m = re.search(r"(.{1,10})还是(.{1,10})", q)
    if m:
        a = re.sub(r"[？?，,。.\s]", "", m.group(1))[-8:]
        b = re.sub(r"[？?，,。.\s]", "", m.group(2))[:8]
        if strength == "strong":
            return f"优先选“{a}”"
        if strength == "weak":
            return f"优先选“{b}”"
        return f"先“{b}”，再“{a}”"
    if strength == "strong":
        return "优先主动推进，但要设风险边界"
    if strength == "weak":
        return "优先稳住基本盘，暂缓高风险动作"
    return "先稳后进，避免一次性重仓决策"


def _partner_role_by_gender(gender: str) -> tuple[str, str]:
    normalized = _normalize_gender(gender)
    if normalized == "女":
        return "男生", "他"
    if normalized == "男":
        return "女生", "她"
    return "伴侣", "对方"


def _strip_default_family_script(text: str) -> str:
    out = str(text or "").strip()
    if not out:
        return ""
    out = re.sub(r"(婚后|结婚以后|成家以后|孩子|宝宝|父母|公婆|岳父母)[^。！？!?]{0,24}", "", out)
    out = re.sub(r"[，,；;、]\s*[，,；;、]+", "，", out)
    out = re.sub(r"\s+", "", out)
    return out.strip("，,；;。")


def _safe_text(value) -> str:
    return str(value or "").strip()


def _join_nonempty(parts: list[str], sep: str = "；") -> str:
    return sep.join([str(part).strip() for part in parts if str(part).strip()])


def _render_yinyuan_trend_reply(payload: dict) -> str:
    signal = _strip_default_family_script(str(((payload.get("fortune_signals") or {}).get("love")) or ""))
    if not signal:
        signal = "这段姻缘趋势更适合走“先建立信任，再慢慢升温”的路线。"
    first_signal = re.split(r"[。！？!?]", signal, maxsplit=1)[0].strip(" ，,；;")
    if not first_signal:
        first_signal = "这段姻缘趋势更适合走“先建立信任，再慢慢升温”的路线"
    lines = [
        f"呀哈～本鼠鼠先把这根姻缘小红线递给你：{first_signal}。",
        "这一段更值得你盯住的，不是谁先把气氛炒热，而是有没有稳定回应、能不能把小别扭说开、彼此愿不愿意接住对方的情绪。",
        "如果你已经在接触某个人，就重点看对方是不是愿意持续投入，而不是只在气氛刚好时出现；如果你还没遇到，也别急着把结果写死，先把自己的边界和节奏稳稳放好。",
        "吉伊的小提醒是：先给出一次轻一点、但真诚的表达，再观察对方后面的连续回应；重要关系不要为了立刻要答案就硬往前推，慢一点反而更容易看清。",
    ]
    return "\n".join(lines)


def _render_zhengyuan_profile_reply(payload: dict) -> str:
    zhengyuan_profile = payload.get("zhengyuan_profile") or {}
    if isinstance(zhengyuan_profile, dict) and zhengyuan_profile:
        huaxiang = zhengyuan_profile.get("huaxiang") or {}
        tezhi = zhengyuan_profile.get("tezhi") or {}
        zhiyin = zhengyuan_profile.get("zhiyin") or {}
        partner_label, partner_pronoun = _partner_role_by_gender(str(payload.get("profile_gender") or ""))

        appearance = _join_nonempty(
            [
                _safe_text((huaxiang or {}).get("face_shape")),
                _safe_text((huaxiang or {}).get("eyebrow_shape")),
                _safe_text((huaxiang or {}).get("eye_shape")),
                _safe_text((huaxiang or {}).get("mouth_shape")),
                _safe_text((huaxiang or {}).get("nose_shape")),
                _safe_text((huaxiang or {}).get("body_shape")),
            ]
        )
        romantic_personality = _safe_text((tezhi or {}).get("romantic_personality"))
        family_background = _safe_text((tezhi or {}).get("family_background"))
        career_wealth = _safe_text((tezhi or {}).get("career_wealth"))
        marital_happiness = _safe_text((tezhi or {}).get("marital_happiness"))
        love_location = _safe_text((zhiyin or {}).get("love_location"))
        meeting_method = _safe_text((zhiyin or {}).get("meeting_method"))
        interaction_model = _safe_text((zhiyin or {}).get("interaction_model"))
        love_advice = _safe_text((zhiyin or {}).get("love_advice"))
        yunshi = _safe_text(zhengyuan_profile.get("yunshi"))

        lines = [f"呀哈～吉伊把这份正缘画像认真捧给你看啦。更适合你的{partner_label}，感情底色大致会是这样的：{romantic_personality or '整体偏真诚、投入，也更看重关系里的实际回应。'}"]
        if appearance:
            lines.append(f"如果把{partner_pronoun}的模样一点点描开，给你的第一眼感觉多半会是：{appearance}")
        if family_background:
            lines.append(f"再往成长和家庭这层底色里看，{partner_pronoun}大致会落在这样的背景里：{family_background}")
        if career_wealth:
            lines.append(f"说到现实能力、事业和财富手感，这一块更像是：{career_wealth}")
        if love_location or meeting_method:
            lines.append(
                f"缘分线索也不算含糊，吉伊替你捋顺后大概是这样：{_join_nonempty([love_location, meeting_method], sep=' ')}"
            )
        if interaction_model:
            lines.append(f"真走到相处里，你们更容易长成这样的关系节奏：{interaction_model}")
        if love_advice:
            lines.append(f"这段关系里最该收好的提醒，吉伊想替你圈这一条：{love_advice}")
        if marital_happiness or yunshi:
            lines.append(
                f"如果把长期相处和阶段运势一起摊开来看，后面的画面大致会是这样：{_join_nonempty([marital_happiness, yunshi], sep=' ')}"
            )
        if _safe_text((huaxiang or {}).get('profile_image')):
            lines.append("我也把这张正缘画像预览偷偷放在下面啦，你可以直接看头像感觉，会更有代入感。")
        return "\n\n".join([line for line in lines if line.strip()])

    partner_label, partner_pronoun = _partner_role_by_gender(str(payload.get("profile_gender") or ""))
    signal = _strip_default_family_script(str(((payload.get("fortune_signals") or {}).get("love")) or ""))
    opportunity = " ".join([_strip_default_family_script(item) for item in (payload.get("opportunity_points") or [])[:2]])
    traits: list[str] = []
    combined = f"{signal} {opportunity}"
    if re.search(r"(主动|直接|热烈|冒险|勇敢)", combined):
        traits.append("表达直接，遇事不爱兜圈子")
    if re.search(r"(活力|好奇|探索|新鲜感)", combined):
        traits.append("有行动力，也愿意一起尝试新事物")
    if re.search(r"(理智|谨慎|稳步|毅力|坚韧)", combined):
        traits.append("处理现实问题时不飘，能一起把事情落地")
    if not traits:
        traits = ["情绪表达比较真诚", "相处时更看重实际回应", "关系里愿意一起承担现实问题"]
    timing_line = ""
    if re.search(r"(2026|2027)", opportunity):
        timing_line = "从节奏上看，接下来一两年更容易遇到或确认这类关系。"
    lines = [
        f"呀哈～吉伊先把结论抱给你：更适合你的{partner_label}，多半不是只会制造暧昧感的人，而是那种相处起来有热度、做事也肯认真投入的人。",
        f"{partner_pronoun}身上的气质重点，大致会落在这些地方：{'；'.join(traits[:3])}。",
        "真走到相处里，你们更容易因为一起做事、一起面对变化、一起把现实安排落下来而升温，不是只靠一时上头。",
    ]
    if timing_line:
        lines.append(timing_line)
    lines.append(f"吉伊的小建议是：先别急着用预设条件去框人，先看这个{partner_label}是否稳定回应、是否愿意共担现实问题；真正合适的人，通常会在连续互动里越来越清楚。")
    return "\n".join(lines)


def _render_jiehun_prediction_reply(payload: dict) -> str:
    profile = payload.get("jiehun_profile") or {}
    if isinstance(profile, dict) and profile:
        star_name = _safe_text(profile.get("star_name"))
        star_desc = _safe_text(profile.get("star_desc"))
        romantic_personality = _safe_text(profile.get("romantic_personality"))
        destined_partner = _safe_text(profile.get("destined_partner"))
        peak_love_ages = _safe_text(profile.get("peak_love_ages"))
        gap_ages = _safe_text(profile.get("gap_ages"))
        love_desc = _safe_text(profile.get("love_desc"))

        opening_parts = [part for part in [star_name, star_desc] if part]
        opening = "，".join(opening_parts) if opening_parts else "这份结婚预测更像是在给你一张婚缘节奏图"
        lines = [f"呀哈～吉伊先把这张婚缘时间表抖开给你看：{opening}。"]
        if love_desc:
            lines.append(f"如果把“结婚”这件事往前看，你目前的婚缘节奏大致是：{love_desc}")
        if romantic_personality:
            lines.append(f"先说你在亲密关系里的底色，吉伊看到的是：{romantic_personality}")
        if destined_partner:
            lines.append(f"再看你更容易走向哪类缘分，对象线索更像是：{destined_partner}")
        if peak_love_ages or gap_ages:
            lines.append(
                f"时间点吉伊也替你一并圈出来啦：{_join_nonempty([f'桃花运更旺的阶段在{peak_love_ages}' if peak_love_ages else '', f'情感容易空窗的阶段在{gap_ages}' if gap_ages else ''], sep='；')}"
            )
        lines.append("吉伊的小提醒是：别只盯着“我会不会马上结婚”，先看关系里是不是有稳定投入、现实协同和长期打算；这样你会更容易把好缘分稳稳接住。")
        return "\n\n".join([line for line in lines if line.strip()])

    signal = _strip_default_family_script(str(((payload.get("fortune_signals") or {}).get("love")) or ""))
    if not signal:
        signal = "这段婚缘更适合先把关系走稳，再谈是否进入婚姻。"
    return (
        f"呀哈～吉伊先把结论轻轻放你手心里：{signal}\n"
        "如果你现在在看结婚节奏，先别急着追一个具体日期，更重要的是确认这段关系有没有稳定回应、现实配合和长期打算。\n"
        "吉伊的小提醒是：先把关系里的共识谈清楚，再决定要不要往婚姻推进。"
    )


def _build_fortune_chat_extra(payload: dict | None = None) -> dict:
    meta = payload if isinstance(payload, dict) else {}
    portrait = str(meta.get("partner_portrait_image") or "").strip()
    if portrait.startswith("data:image/image/jpeg;base64,"):
        portrait = portrait.replace("data:image/image/jpeg;base64,", "data:image/jpeg;base64,", 1)
    elif portrait.startswith("data:image/image/png;base64,"):
        portrait = portrait.replace("data:image/image/png;base64,", "data:image/png;base64,", 1)
    if portrait.startswith("data:image/"):
        return {
            "partner_portrait_image": portrait,
            "partner_portrait_label": "正缘画像预览",
            "provider_id": str(meta.get("provider_id") or ""),
        }
    return {}


def render_user_fortune_reply_v2(
    payload: dict,
    topic: str,
    query: str,
    question_type: str,
    window_meta: dict | None = None,
    session_id: str = "",
) -> str:
    strength = str(payload.get("strength") or "balanced")
    advice_for_sign = _resolve_fortune_advice(payload, topic, strength)
    payload["advice_signature"] = _advice_signature(advice_for_sign)
    raw_error = payload.get("error")
    has_error = isinstance(raw_error, dict) and str(raw_error.get("code") or "")
    provider_id = str(payload.get("provider_id") or payload.get("source") or "")
    if not has_error and topic == "love":
        if provider_id == "yuanfenju_yinyuan":
            payload["blueprint_id"] = "love_yinyuan_direct"
            payload["_render_blueprint_id"] = "love_yinyuan_direct"
            return _render_yinyuan_trend_reply(payload)
        if provider_id == "yuanfenju_zhengyuan":
            payload["blueprint_id"] = "love_zhengyuan_direct"
            payload["_render_blueprint_id"] = "love_zhengyuan_direct"
            return _render_zhengyuan_profile_reply(payload)
        if provider_id == "yuanfenju_jiehun":
            payload["blueprint_id"] = "love_jiehun_direct"
            payload["_render_blueprint_id"] = "love_jiehun_direct"
            return _render_jiehun_prediction_reply(payload)
    if not has_error:
        payload["blueprint_id"] = "llm_nlg_v1"
        payload["_render_blueprint_id"] = "llm_nlg_v1"
        model_reply = _generate_fortune_reply_with_model(
            payload=payload,
            topic=topic,
            query=query,
            question_type=question_type,
            window_meta=window_meta,
        )
        if model_reply:
            return model_reply

    if not _render_v3_enabled():
        return _render_user_fortune_reply_v2_legacy(
            payload, topic, query=query, question_type=question_type, window_meta=window_meta
        )

    topic_cn = _topic_cn(topic)
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("code") or ""):
        code = str(error.get("code") or "")
        msg = str(error.get("message") or "命理链路暂时不可用")
        payload["blueprint_id"] = "error_fallback"
        payload["_render_blueprint_id"] = "error_fallback"
        payload["advice_signature"] = _advice_signature(_default_fortune_advice(topic, "balanced")[:1])
        return (
            f"呀哈～这次{topic_cn}盘面暂时没取全（{code}）。{msg}。"
            f"先给你一个稳妥方向：{_default_fortune_advice(topic, 'balanced')[0]}"
        )

    strength_text = {
        "strong": "势能偏强，适合主动推进",
        "weak": "势能偏谨慎，先稳节奏更顺",
        "balanced": "节奏偏平衡，适合稳中求进",
    }.get(strength, "节奏偏平衡，适合稳中求进")
    if question_type in {"decision", "comparison"}:
        conclusion = _decision_conclusion_from_query(query, strength)
    else:
        conclusion = f"这次{topic_cn}{strength_text}"

    window_line = ""
    if question_type in {"trend", "colloquial"} and isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
        if window_text and _should_show_window_text(query, window_label, question_type=question_type):
            window_line = _natural_window_line(query, window_text, window_label, question_type=question_type)
    signal_line = _signal_for_topic(payload, topic)
    basis = _natural_basis_line(payload)
    advice = _resolve_fortune_advice(payload, topic, strength)
    advice_signature = _advice_signature(advice)
    query_hash = hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]
    blueprint_id = _select_fortune_blueprint(question_type, session_id, query_hash)
    payload["blueprint_id"] = blueprint_id
    payload["_render_blueprint_id"] = blueprint_id
    payload["advice_signature"] = advice_signature
    return _render_fortune_with_blueprint(
        blueprint_id=blueprint_id,
        conclusion_line=conclusion,
        window_line=window_line,
        signal_line=signal_line,
        basis_line=basis,
        advice=advice,
    )


def render_structured_fortune_reply(payload: dict, topic: str) -> str:
    topic_cn = _topic_cn(topic)
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("code") or ""):
        code = str(error.get("code") or "")
        msg = str(error.get("message") or "命理链路暂时不可用")
        return (
            f"呀哈～这次{topic_cn}盘面暂时没取全（{code}）。{msg}。\n"
            f"先给你一个稳妥方向：{_default_fortune_advice(topic, 'balanced')[0]}"
        )

    strength_text = {
        "strong": "势能偏强，适合主动推进",
        "weak": "势能偏谨慎，先稳节奏更顺",
        "balanced": "节奏偏平衡，适合稳中求进",
    }.get(str(payload.get("strength") or "balanced"), "节奏偏平衡，适合稳中求进")

    signal_line = _signal_for_topic(payload, topic)
    scores = payload.get("wuxing_scores") or {}
    score_line = (
        f"金{scores.get('metal', 0)} 木{scores.get('wood', 0)} 水{scores.get('water', 0)} "
        f"火{scores.get('fire', 0)} 土{scores.get('earth', 0)}"
    )
    advice = _resolve_fortune_advice(payload, topic, str(payload.get("strength") or "balanced"))
    confidence = int(float(payload.get("confidence", 0.2)) * 100)

    lines = [f"呀哈～先给你结论：这次{topic_cn}{strength_text}。"]
    if signal_line:
        lines.append(f"命理信号：{signal_line}")
    lines.append(f"命理依据：{_natural_basis_line(payload)}。")
    lines.append(f"五行分布：{score_line}。")
    lines.append("行动建议：")
    for idx, tip in enumerate(advice, start=1):
        lines.append(f"{idx}. {tip}")
    lines.append(f"参考置信度：{confidence}%")
    return _soften_fortune_section_headings("\n".join(lines))


def _build_fortune_provider_safe_fallback(
    payload: dict,
    topic: str,
    query: str,
    question_type: str,
    time_anchor: dict,
    window_meta: dict | None = None,
    session_id: str = "",
) -> str:
    normalized = _normalize_structured_fortune_payload(payload, topic)
    normalized["now_ts"] = str((time_anchor or {}).get("now_ts") or "")
    normalized["tz"] = str((time_anchor or {}).get("tz_name") or "")
    if isinstance(window_meta, dict):
        normalized["window_start"] = str(window_meta.get("window_start") or "")
        normalized["window_end"] = str(window_meta.get("window_end") or "")
        normalized["window_text"] = str(window_meta.get("window_text") or "")
    return render_user_fortune_reply_v2(
        normalized,
        topic,
        query=query,
        question_type=question_type,
        window_meta=window_meta,
        session_id=session_id,
    )


def _format_divination_reply(raw) -> str:
    if isinstance(raw, dict):
        ordered_keys = ["凶吉", "运势", "财富", "感情", "事业", "身体", "行人", "解曰"]
        parts = []
        for key in ordered_keys:
            val = str(raw.get(key) or "").strip()
            if val:
                parts.append(f"{key}：{val}")
        if not parts:
            flat = [f"{k}：{v}" for k, v in raw.items() if str(v).strip()]
            parts = flat[:6]
        body = "\n".join(parts[:6]) if parts else "卦象暂时不明，建议稍后再试一次。"
        return f"呀哈～本鼠鼠给你摇到一卦：\n{body}"
    text = str(raw or "").strip() or "卦象暂时不明，建议稍后再试一次。"
    return f"呀哈～本鼠鼠给你摇到一卦：{text}"


def route_dream_pipeline(query: str) -> tuple[str | None, dict | None]:
    q = str(query or "").strip()
    if not q:
        return None, None
    if not is_dream_query(q):
        return None, None
    try:
        raw = jiemeng.invoke(q)
    except Exception:
        try:
            raw = jiemeng.run(q)
        except Exception:
            raw = {}
    reply = _generate_dream_reply_with_model(q, raw)
    if not reply:
        detail = _format_dream_payload(raw)
        if detail:
            reply = _ensure_jiyi_tone(f"先给你一个梦境方向：{detail}")
        else:
            reply = "呀哈～这次梦境线索有点散，本鼠鼠建议你补一句“梦里最强烈的画面”，我再帮你细解。"
    return reply, {"topic": "dream", "source": "jiemeng", "question_type": "dream"}


def _build_fortune_tool_query(
    *,
    query: str,
    profile: dict[str, str],
    anchor: dict,
    topic: str,
    window_meta: dict | None = None,
    need_window: bool = False,
) -> tuple[str, str, str]:
    name = str(profile.get("name") or "").strip()
    birthdate = str(profile.get("birthdate") or "").strip()
    birthtime = str(profile.get("birthtime") or "").strip()
    gender = _normalize_gender(str(profile.get("gender") or ""))
    near_days = anchor.get("near_days") or []
    window_text = ""
    window_label = ""
    if isinstance(window_meta, dict) and str(window_meta.get("window_text") or "").strip():
        window_text = str(window_meta.get("window_text")).strip()
        window_label = str(window_meta.get("label") or "").strip()
    elif near_days and need_window:
        window_text = "、".join([f"{d.get('date_cn')}（{d.get('weekday_cn')}）" for d in near_days if d.get("date_cn")])
        window_label = "near_days"
    time_window_clause = ""
    if window_text:
        if window_label == "today_only":
            time_window_clause = f"若用户问“今天/今日”，仅允许按当天判断：{window_text}。不要扩成“三天”或“近几天”。"
        elif window_label in {"near_days", "two_days", "this_week", "next_week"}:
            time_window_clause = f"若用户问“近几天/哪几天”，仅允许在此窗口判断：{window_text}。"
        else:
            time_window_clause = f"时间范围：{window_text}。回答不要收缩成“近三天”，要覆盖该范围。"
    tool_query = (
        f"请按结构化JSON返回{topic}命理结果。"
        f"姓名：{name}；出生日期：{birthdate}；出生时间：{birthtime or '未知'}；性别：{gender or '未知'}；用户问题：{query}。"
        f"当前时间锚点：{anchor.get('today_cn')}（{anchor.get('weekday_cn')}，{anchor.get('tz_name')}，{anchor.get('utc_offset')}）。"
        f"{time_window_clause}"
    )
    return tool_query, window_text, window_label


def _finalize_fortune_payload(
    payload: dict,
    *,
    topic: str,
    question_type: str,
    route_reason_code: str,
    anchor: dict,
    window_meta: dict | None = None,
) -> dict:
    normalized = _normalize_structured_fortune_payload(payload, topic)
    _metric_incr("fortune_tool_total")
    if not (isinstance(normalized.get("error"), dict) and str(normalized["error"].get("code") or "")):
        _metric_incr("fortune_tool_success_total")
    _metric_incr("fortune_field_total")
    if _is_fortune_field_complete(normalized):
        _metric_incr("fortune_field_complete_total")
    normalized["question_type"] = str(question_type or "default")
    normalized["now_ts"] = str(anchor.get("now_ts") or "")
    normalized["tz"] = str(anchor.get("tz_name") or "")
    normalized["route_reason_code"] = route_reason_code
    if isinstance(window_meta, dict):
        normalized["window_start"] = str(window_meta.get("window_start") or "")
        normalized["window_end"] = str(window_meta.get("window_end") or "")
        normalized["window_text"] = str(window_meta.get("window_text") or "")
    logger.info(
        "fortune_pipeline session_payload: "
        f"topic={normalized.get('topic')} provider={normalized.get('provider_id') or normalized.get('source')} "
        f"error={((normalized.get('error') or {}).get('code') if isinstance(normalized.get('error'), dict) else '')} "
        f"confidence={normalized.get('confidence')} question_type={question_type} route_reason={route_reason_code}"
    )
    return normalized


def _merge_wealth_compare_payloads(payloads: list[dict], years: list[int]) -> dict:
    base = _normalize_structured_fortune_payload(payloads[0], "wealth")
    signal_lines: list[str] = []
    opportunity_points: list[str] = []
    risk_points: list[str] = []
    time_hints: list[str] = []
    evidence_lines: list[str] = []
    advice: list[str] = []
    total_calls = 0
    for year, payload in zip(years, payloads):
        normalized = _normalize_structured_fortune_payload(payload, "wealth")
        total_calls += int(normalized.get("provider_calls") or 0)
        signal = _signal_for_topic(normalized, "wealth")
        if signal:
            signal_lines.append(f"{year}年：{signal}")
        opportunity_points.extend([f"{year}年：{item}" for item in (normalized.get("opportunity_points") or [])[:2]])
        risk_points.extend([f"{year}年：{item}" for item in (normalized.get("risk_points") or [])[:2]])
        time_hints.extend([f"{year}年：{item}" for item in (normalized.get("time_hints") or [])[:1]])
        evidence_lines.extend([f"{year}年：{item}" for item in (normalized.get("evidence_lines") or [])[:1]])
        advice.extend([f"{year}年：{item}" for item in (normalized.get("advice") or [])[:1]])
    base["source"] = "yuanfenju_caiyunfenxi_compare"
    base["provider_id"] = "yuanfenju_caiyunfenxi_compare"
    base["provider_calls"] = total_calls
    base["fortune_signals"]["wealth"] = "；".join(signal_lines)[:180]
    base["opportunity_points"] = opportunity_points[:4]
    base["risk_points"] = risk_points[:4]
    base["time_hints"] = time_hints[:4]
    base["evidence_lines"] = evidence_lines[:4]
    base["advice"] = advice[:3] or _default_fortune_advice("wealth", str(base.get("strength") or "balanced"))
    base["confidence"] = max(float(base.get("confidence") or 0.2), 0.66)
    return base


def route_fortune_pipeline(
    query: str,
    profile: dict[str, str],
    time_anchor: dict | None = None,
    flags: dict[str, bool] | None = None,
    question_type: str = "default",
    session_id: str = "",
) -> tuple[str | None, dict | None]:
    q = str(query or "").strip()
    if not q:
        return None, None
    anchor = time_anchor or build_time_anchor()
    active_flags = flags or dict(FEATURE_FLAG_DEFAULTS)
    need_window = bool(active_flags.get("window_v2")) and _need_time_window(q, question_type=question_type)
    window_meta = date_window_resolver(q, anchor) if need_window else None

    if is_divination_query(q) and not is_bazi_fortune_query(q):
        try:
            raw = yaoyigua.invoke({})
        except Exception:
            try:
                raw = yaoyigua.run("")
            except Exception:
                raw = "卦象暂时不明，建议稍后再试一次。"
        _metric_incr("fortune_tool_total")
        if str(raw or "").strip():
            _metric_incr("fortune_tool_success_total")
        out = _generate_divination_reply_with_model(q, raw) or _format_divination_reply(raw)
        return out, {"topic": "divination", "error": None, "question_type": question_type}

    if not is_bazi_fortune_query(q):
        return None, None
    route_reason_code = _route_reason_for_fortune_query(q)

    missing = _missing_profile_fields_for_fortune(profile)
    if missing:
        return (
            build_fortune_missing_reply(missing),
            {
                "topic": detect_fortune_topic(q),
                "error": {"code": "PROFILE_MISSING"},
                "question_type": question_type,
                "route_reason_code": route_reason_code,
            },
        )

    topic = detect_fortune_topic(q)
    if _is_partner_profile_query(q) or _is_love_trend_query(q) or _is_marriage_prediction_query(q):
        need_window = False
        window_meta = None
    tool_query, _, window_label = _build_fortune_tool_query(
        query=q,
        profile=profile,
        anchor=anchor,
        topic=topic,
        window_meta=window_meta,
        need_window=need_window,
    )
    target_years = _extract_target_years(q, anchor.get("now_dt").year if isinstance(anchor.get("now_dt"), datetime) else datetime.now().year)
    provider_fallback_reason = ""
    raw_payload: dict | None = None

    incident_hit = None if _is_marriage_prediction_query(q) else _extract_zeshi_incident(q)
    if incident_hit and active_flags.get("zeshi_api_v1"):
        incident_id, incident_label = incident_hit
        future_code = _resolve_zeshi_future_code(q, window_meta)
        zeshi_result = run_yuanfenju_zeshi(
            future=future_code,
            incident=incident_id,
            incident_label=incident_label,
            window_start=str((window_meta or {}).get("window_start") or ""),
            window_end=str((window_meta or {}).get("window_end") or ""),
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        _metric_incr("fortune_tool_total")
        if zeshi_result.get("ok") and str(zeshi_result.get("text") or "").strip():
            _metric_incr("fortune_tool_success_total")
            return str(zeshi_result.get("text") or ""), {
                "topic": "daily",
                "source": str(zeshi_result.get("provider_id") or "yuanfenju_gongju_zeshi"),
                "provider_id": str(zeshi_result.get("provider_id") or "yuanfenju_gongju_zeshi"),
                "provider_calls": int(zeshi_result.get("provider_calls") or 0),
                "provider_fallback_reason": "",
                "quota_state": str(zeshi_result.get("quota_state") or "healthy"),
                "question_type": question_type,
                "route_reason_code": "zeshi_incident_hit",
                "zeshi_future_code": future_code,
                "window_start": str((window_meta or {}).get("window_start") or ""),
                "window_end": str((window_meta or {}).get("window_end") or ""),
                "window_text": str((window_meta or {}).get("window_text") or ""),
            }
        route_reason_code = "zeshi_fallback_to_bazi"
        provider_fallback_reason = str(((zeshi_result.get("failure") or {}).get("error_code")) or "zeshi_fallback")

    if raw_payload is None and _is_partner_profile_query(q) and active_flags.get("love_profile_v1"):
        raw_payload = run_yuanfenju_love_profile(
            tool_query,
            profile=profile,
            variant="zhengyuan",
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        route_reason_code = "zhengyuan_hit"
        if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
            provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "zhengyuan_fallback")
            raw_payload = None
            route_reason_code = "zhengyuan_fallback_to_bazi"

    if raw_payload is None and _is_marriage_prediction_query(q) and active_flags.get("love_profile_v1"):
        raw_payload = run_yuanfenju_love_profile(
            tool_query,
            profile=profile,
            variant="jiehun",
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        route_reason_code = "jiehun_hit"
        if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
            provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "jiehun_fallback")
            raw_payload = None
            route_reason_code = "jiehun_fallback_to_bazi"

    if raw_payload is None and _is_love_trend_query(q) and active_flags.get("love_profile_v1"):
        raw_payload = run_yuanfenju_love_profile(
            tool_query,
            profile=profile,
            variant="yinyuan",
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        route_reason_code = "yinyuan_hit"
        if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
            provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "yinyuan_fallback")
            raw_payload = None
            route_reason_code = "yinyuan_fallback_to_bazi"

    if raw_payload is None and topic == "wealth" and active_flags.get("wealth_year_v1"):
        if target_years:
            if len(target_years) > 3:
                return (
                    "呀哈～这类多年财运对比我可以看，但先帮我把范围收窄到最多 3 年，例如“今年和明年财运对比”或“2026 到 2028 年财运”。",
                    {
                        "topic": "wealth",
                        "source": "wealth_year_clarify",
                        "question_type": "clarify",
                        "route_reason_code": "wealth_year_clarify",
                    },
                )
            if len(target_years) == 1:
                raw_payload = run_yuanfenju_wealth_year(
                    tool_query,
                    profile=profile,
                    liu_year=target_years[0],
                    enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
                )
                route_reason_code = "wealth_year_hit"
                if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
                    provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "wealth_year_fallback")
                    raw_payload = None
                    route_reason_code = "wealth_year_fallback_to_bazi"
            else:
                comparison_payloads = []
                compare_failed = False
                for year in target_years[:3]:
                    yearly_payload = run_yuanfenju_wealth_year(
                        tool_query,
                        profile=profile,
                        liu_year=year,
                        enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
                    )
                    if isinstance((yearly_payload or {}).get("error"), dict) and str((yearly_payload.get("error") or {}).get("code") or ""):
                        provider_fallback_reason = str((yearly_payload.get("error") or {}).get("code") or "wealth_compare_fallback")
                        compare_failed = True
                        break
                    comparison_payloads.append(yearly_payload)
                if not compare_failed and comparison_payloads:
                    raw_payload = _merge_wealth_compare_payloads(comparison_payloads, target_years[: len(comparison_payloads)])
                    route_reason_code = "wealth_year_compare_hit"
                elif compare_failed:
                    raw_payload = None
                    route_reason_code = "wealth_year_compare_fallback_to_bazi"
        else:
            raw_payload = run_yuanfenju_wealth_profile(
                tool_query,
                profile=profile,
                enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
            )
            route_reason_code = "wealth_profile_hit"
            if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
                provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "wealth_profile_fallback")
                raw_payload = None
                route_reason_code = "wealth_profile_fallback_to_bazi"

    if raw_payload is None and active_flags.get("bazi_daily_v1") and _is_daily_window_query(q):
        raw_payload = run_yuanfenju_bazi_daily(
            tool_query,
            profile=profile,
            topic=topic,
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        route_reason_code = "bazi_daily_hit"
        if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
            provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "bazi_daily_fallback")
            raw_payload = None
            route_reason_code = "bazi_daily_fallback_to_bazi"

    if (
        raw_payload is None
        and active_flags.get("bazi_future_v1")
        and len(target_years) == 1
        and topic != "wealth"
        and question_type == "trend"
    ):
        raw_payload = run_yuanfenju_bazi_future(
            tool_query,
            profile=profile,
            yunshi_year=target_years[0],
            topic=topic,
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        route_reason_code = "bazi_future_hit"
        if isinstance((raw_payload or {}).get("error"), dict) and str((raw_payload.get("error") or {}).get("code") or ""):
            provider_fallback_reason = str((raw_payload.get("error") or {}).get("code") or "bazi_future_fallback")
            raw_payload = None
            route_reason_code = "bazi_future_fallback_to_bazi"

    if raw_payload is None:
        raw_payload = run_yuanfenju_bazi_cesuan(
            tool_query,
            profile=profile,
            topic=topic,
            enable_merchant_probe=bool(active_flags.get("merchant_probe_v1")),
        )
        if provider_fallback_reason and not str(raw_payload.get("provider_fallback_reason") or "").strip():
            raw_payload["provider_fallback_reason"] = provider_fallback_reason
    if isinstance(raw_payload, dict):
        raw_payload["profile_gender"] = _normalize_gender(str(profile.get("gender") or ""))

    payload = _finalize_fortune_payload(
        raw_payload,
        topic=topic,
        question_type=question_type,
        route_reason_code=route_reason_code,
        anchor=anchor,
        window_meta=window_meta,
    )
    if route_reason_code == "wealth_year_compare_hit":
        payload["question_type"] = "comparison"
    if active_flags.get("render_v2"):
        return (
            render_user_fortune_reply_v2(
                payload,
                topic,
                query=q,
                question_type="comparison" if route_reason_code == "wealth_year_compare_hit" else question_type,
                window_meta=window_meta,
                session_id=session_id,
            ),
            payload,
        )
    payload["blueprint_id"] = "structured_v2"
    payload["_render_blueprint_id"] = "structured_v2"
    payload["advice_signature"] = _advice_signature(
        _resolve_fortune_advice(payload, topic, str(payload.get("strength") or "balanced"))
    )
    return render_structured_fortune_reply(payload, topic), payload


def diversify_fortune_opening(text: str, user_query: str = "") -> str:
    out = str(text or "").strip()
    if not out:
        return out

    lines = out.splitlines()
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_idx is None:
        return out

    first_line = lines[first_idx].strip()
    starts_with_bazi = bool(
        re.search(r"(你生在|生于|生在.*年.*月.*日|[子丑寅卯辰巳午未申酉戌亥]时)", first_line)
    )
    if not starts_with_bazi:
        return out

    openings = [
        "呀哈～先给你一个更直接的方向：",
        "吉伊先把要紧的一句递给你：",
        "先不急着看四柱，本鼠鼠先把方向讲清楚：",
        "呜啦～先把你最关心的答案放前面：",
        "先给你一句最有用的提醒：",
    ]
    seed = hashlib.sha256(f"{user_query}|{out}".encode("utf-8")).hexdigest()
    opening = openings[int(seed[:8], 16) % len(openings)]
    remaining = [ln.strip() for idx, ln in enumerate(lines) if idx != first_idx and str(ln).strip()]
    basis_intro = _pick_non_repeat(
        [
            f"顺着盘里先冒出来的底色看，{first_line}",
            f"我先抓到的一层气口是，{first_line}",
            f"这回先托住判断的一笔，是{first_line}",
            f"本鼠鼠先摸到的线头，大概是{first_line}",
        ],
        f"{user_query}|{first_line}|basis_blend",
    ).rstrip("。") + "。"
    if not remaining:
        return f"{opening}\n{basis_intro}".strip()
    remaining[0] = f"{basis_intro}{remaining[0]}"
    body = "\n\n".join(remaining).strip()
    return f"{opening}\n{body}".strip()


def trim_for_emotion_level(text: str, emotion_level: str) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    if emotion_level == "L1":
        return out

    # L2/L3: 句子更短，先结果再动作，避免长段落。
    pieces = re.split(r"(?<=[。！？!?])", out)
    pieces = [p.strip() for p in pieces if p.strip()]
    limit = 4 if emotion_level == "L2" else 3
    short = "".join(pieces[:limit]).strip()
    return short or out


def add_recovery_tail(text: str, emotion_level: str) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    if emotion_level != "L3":
        return out
    if re.search(r"(优先处理|我先帮你|预计|分钟|小时|工单|跟进)", out):
        return out
    return out + "\n我先按优先处理给你跟进：预计30分钟内给你一个明确进展。"


def add_light_jiyi_particle(text: str, user_query: str = "") -> str:
    out = str(text or "").strip()
    if not out:
        return out
    emotion_level = detect_emotion_level(user_query)
    if emotion_level in {"L2", "L3"}:
        # 焦虑/愤怒场景减少语气词干扰
        out = re.sub(r"(呀哈～?|呜啦～?|噗噜。?|呀～哈～|哼～\?|蛤\?)", "", out)
        return out.strip()
    if re.search(r"(呀哈|呜啦|噗噜|哼～\?|蛤\?)", out):
        # 已有语气词则不再追加，避免过量
        return out
    particles = ["呀哈～", "呜啦～", "噗噜。", "哼～", "呀～哈～"]
    seed = hashlib.sha256(f"{user_query}|{out}|particle".encode("utf-8")).hexdigest()
    # L1 轻量注入：小幅提高但不油腻（约35%）
    if int(seed[8:16], 16) % 100 >= 35:
        return out
    p = particles[int(seed[:8], 16) % len(particles)]
    return f"{p}{out}"


def _birth_info_placeholder(seed_text: str) -> str:
    return _pick_non_repeat(
        [
            "你出生时那组底色",
            "你那份先天气口",
            "你出生那会儿的命盘底子",
            "你先天那层小底色",
        ],
        f"{seed_text}|birthinfo",
    )


def _format_birthtime_natural(value: str) -> str:
    raw = str(value or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not m:
        return raw
    hh = int(m.group(1))
    mm = int(m.group(2))
    if 0 <= hh < 5:
        period = "凌晨"
    elif 5 <= hh < 8:
        period = "清晨"
    elif 8 <= hh < 12:
        period = "早上"
    elif hh == 12:
        period = "中午"
    elif 13 <= hh < 18:
        period = "下午"
    else:
        period = "晚上"
    if mm == 0:
        return f"{period}{hh}点"
    return f"{period}{hh}点{mm}分"


def _build_identity_fact_reply(query: str, profile: dict | None = None) -> str:
    q = str(query or "").strip()
    p = profile or {}
    address = _pick_address_name(p, user_query=q)
    preferred_name = _sanitize_preferred_name(str(p.get("preferred_name") or "").strip())
    legal_name = str(p.get("name") or "").strip()
    if _is_asking_own_name(q):
        name = preferred_name or legal_name
        if name:
            return f"记得呀，你叫{name}。"
        return "本鼠鼠这边还没记住你的名字。你告诉我一次，我就接着记。"
    ask_birthdate = _is_asking_own_birthdate(q)
    ask_birthtime = _is_asking_own_birthtime(q)
    birthdate = str(p.get("birthdate") or "").strip()
    birthtime = str(p.get("birthtime") or "").strip()
    if ask_birthdate or ask_birthtime:
        date_cn = _iso_to_cn(birthdate, short=False) if birthdate else ""
        time_cn = _format_birthtime_natural(birthtime) if birthtime else ""
        if ask_birthdate and ask_birthtime:
            if date_cn and time_cn:
                return f"{address}的生日是{date_cn}，出生时段落在{time_cn}。"
            if date_cn:
                return f"{address}的生日是{date_cn}。出生时段这边我还没记全。"
            if time_cn:
                return f"{address}出生在{time_cn}，但生日这边我还没记全。"
            return "本鼠鼠这边还没把你的生日和出生时段记完整。你补给我，我就接着记。"
        if ask_birthdate:
            if date_cn:
                return f"{address}的生日是{date_cn}呀。"
            return "本鼠鼠这边还没把你的生日记下来。你告诉我一次，我就接着记。"
        if ask_birthtime:
            if time_cn:
                return f"{address}出生在{time_cn}。"
            return "本鼠鼠这边还没把你的出生时段记下来。你告诉我一次，我就接着记。"
    if re.search(r"(你记得我吗|你记得我是谁吗|我是谁你还记得吗)", q):
        name = preferred_name or legal_name
        if name:
            return f"当然记得，你是{name}。"
        return "本鼠鼠记得你来过，不过名字这边我还没记全。"
    return ""


def strip_profile_echo(text: str, profile: dict | None = None, user_query: str = "") -> str:
    out = str(text or "").strip()
    if not out:
        return out
    p = profile or {}
    name = str(p.get("name") or "").strip()
    preferred_name = _sanitize_preferred_name(str(p.get("preferred_name") or "").strip())
    birthdate = str(p.get("birthdate") or "").strip()
    birthtime = str(p.get("birthtime") or "").strip()

    # 姓名处理：若用户有称呼偏好，优先替换成偏好；若在“我叫什么”场景，允许显示姓名/昵称；其余场景维持匿名“你”。
    replace_name = "你"
    if _is_valid_call_name(preferred_name):
        replace_name = preferred_name
    else:
        replace_name = _pick_address_name(p, user_query=user_query)
    if name and len(name) >= 2:
        out = out.replace(name, replace_name)
    if birthdate and birthtime:
        birth_info_repl = _birth_info_placeholder(f"{user_query}|{out}")
        try:
            y, mo, d = birthdate.split("-")
            cn_plain = f"{int(y)}年{int(mo)}月{int(d)}日"
            cn_padded = f"{y}年{mo}月{d}日"
            combo_patterns = [
                rf"(?:你\s*)?(?:出生在|生于|生在)\s*{re.escape(birthdate)}\s*{re.escape(birthtime)}(?::00)?",
                rf"(?:你\s*)?(?:出生在|生于|生在)\s*{re.escape(cn_plain)}\s*{re.escape(birthtime)}(?::00)?",
                rf"(?:你\s*)?(?:出生在|生于|生在)\s*{re.escape(cn_padded)}\s*{re.escape(birthtime)}(?::00)?",
            ]
            for pattern in combo_patterns:
                out = re.sub(pattern, birth_info_repl, out)
        except Exception:
            out = re.sub(
                rf"(?:你\s*)?(?:出生在|生于|生在)\s*{re.escape(birthdate)}\s*{re.escape(birthtime)}(?::00)?",
                birth_info_repl,
                out,
            )
    if birthdate:
        out = out.replace(birthdate, "你的生日")
        try:
            y, mo, d = birthdate.split("-")
            cn_padded = f"{y}年{mo}月{d}日"
            cn_plain = f"{int(y)}年{int(mo)}月{int(d)}日"
            out = out.replace(cn_padded, "你的生日")
            out = out.replace(cn_plain, "你的生日")
        except Exception:
            out = out.replace(birthdate.replace("-", "年", 1).replace("-", "月") + "日", "你的生日")
    if birthtime:
        out = out.replace(birthtime, "你的出生时段")
        out = out.replace(f"{birthtime}:00", "你的出生时段")
    out = re.sub(r"(姓名|名字|出生日期|生日|出生时间|时辰)\s*[:：]\s*[^，。；\n]+", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _soften_generic_bazi_terms(text: str) -> str:
    out = str(text or "")
    replacements = [
        (r"命理上看[，,:：]?\s*", "顺着这阵子的气口看，"),
        (r"根据[^，。；\n]{0,24}的八字排盘[（(][^）)]*[）)]", "顺着你的盘面底色"),
        (r"根据[^，。；\n]{0,24}的八字排盘", "顺着你的盘面底色"),
        (r"八字排盘显示", "盘面底色里能看出来"),
        (r"\b日主为", "你本人的底色偏"),
        (r"\b日主\b", "你本人的底色"),
        (r"\b流年\b", "今年这股时运"),
        (r"\b月令\b", "当下这段节奏"),
        (r"\b正印\b", "那股更稳、更能沉下来的劲"),
        (r"\b偏财\b", "外界机会和新鲜刺激那股劲"),
        (r"\b喜水润局、火暖局\b", "更适合被温和托一把，再慢慢起势"),
        (r"\b土旺木相\b", "整体更像先稳住、再慢慢往上长"),
        (r"\b天德合\b", "顺手的小吉象"),
    ]
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out)
    return out


def sanitize_output(text: str, user_query: str = "", profile: dict | None = None) -> str:
    if not text:
        return text

    out = str(text)
    out = out.replace("✅", "")
    out = out.replace("**", "")
    out = out.replace("啊…嗯…那个……", "")
    out = out.replace("啊…嗯…那个…", "")
    out = out.replace("你不是一个人……我也会陪着的……", "")
    out = out.replace("总会有办法的！", "")
    tool_name_pattern = r"(serp_search|get_info_from_local_db|bazi_cesuan|yaoyigua|jiemeng)"
    out = re.sub(
        rf"[（(]\s*{tool_name_pattern}\s*已调用[^）)\n]*[）)]",
        "（本鼠鼠刚顺手又核了一遍）",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        rf"{tool_name_pattern}\s*已调用[^，。；;\n]*",
        "本鼠鼠刚顺手又核了一遍",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        rf"(已调用|调用了|用了)\s*{tool_name_pattern}",
        "顺手又核了一遍",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        rf"(工具调用|调用工具)[:：]?\s*{tool_name_pattern}",
        "顺手又核了一遍",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"(?:另一个)?工具验证[:：]\s*", "我又多核了一遍：", out)
    out = re.sub(
        rf"\b{tool_name_pattern}\b",
        "本鼠鼠刚核过的线索",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"(?:本鼠鼠|我)[^。；；，,\n]{0,18}(?:核对|核了一遍|多核了一遍)[^（(\n]{0,24}[（(]本鼠鼠刚顺手又核了一遍[）)]",
        "本鼠鼠刚顺手又核了一遍",
        out,
    )
    out = re.sub(r"本鼠鼠刚顺手又核了一遍[，,、 ]*本鼠鼠刚顺手又核了一遍", "本鼠鼠刚顺手又核了一遍", out)
    out = re.sub(r"我又多核了一遍[:：]\s*本鼠鼠刚顺手又核了一遍", "本鼠鼠又顺手多核了一遍", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"^\s*你问[“\"].*?[”\"][，,:：]?\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*你刚才说[“\"].*?[”\"][，,:：]?\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[\u4e00-\u9fa5]{2,4}[～~][，,:：]?\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out)

    lines = [ln.rstrip() for ln in out.splitlines()]
    deduped = []
    for ln in lines:
        if deduped and deduped[-1] == ln and ln:
            continue
        deduped.append(ln)
    out = "\n".join(deduped)
    out = re.sub(r"\n{3,}", "\n\n", out)

    if _is_identity_fact_query(user_query):
        fact_reply = _build_identity_fact_reply(user_query, profile=profile)
        if fact_reply:
            return fact_reply.strip()
        out = strip_profile_echo(out.strip(), profile=profile, user_query=user_query)
        out = re.sub(r"(19|20)\d{2}年\d{1,2}月\d{1,2}日", "你的生日", out)
        out = re.sub(r"(清晨|凌晨|早上|上午|中午|下午|晚上)\s*\d{1,2}[:：点]\d{0,2}", "你的出生时段", out)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(lines) > 2:
            out = "\n".join(lines[:2])
        return out.strip()

    emotion_level = detect_emotion_level(user_query)
    out = diversify_fortune_opening(out.strip(), user_query=user_query)
    out = _soften_fortune_section_headings(out)
    out = _soften_generic_bazi_terms(out)
    # 降低硬编码后处理，避免把模型答案“模板化”
    out = strip_profile_echo(out, profile=profile, user_query=user_query)
    if emotion_level in {"L2", "L3"}:
        out = trim_for_emotion_level(out, emotion_level)
        out = add_recovery_tail(out, emotion_level)
    out = add_light_jiyi_particle(out, user_query=user_query)
    return out.strip()


def _is_time_alignment_only_answer(text: str) -> bool:
    out = str(text or "").strip()
    if not out:
        return True
    if "时间对齐" not in out and "时间窗口" not in out:
        return False
    residue = _strip_time_alignment_sentences(out)
    if not residue:
        return True
    return len(residue) < 18 and not re.search(r"(建议|结论|先|避免|适合|不宜|财运|事业|感情|学业)", residue)


def _enforce_min_answer_contract(output: str, query: str, question_type: str) -> str:
    out = str(output or "").strip()
    if not out:
        return out
    qtype = str(question_type or "default")
    enforce = qtype in {"trend", "colloquial", "decision"} or is_bazi_fortune_query(query)
    if not enforce:
        return out
    if not _is_time_alignment_only_answer(out):
        return out
    topic = detect_fortune_topic(query)
    if qtype == "decision":
        tail = f"先给你方向：{_decision_conclusion_from_query(query, 'balanced')}。"
    else:
        tail = "先给你一个方向：别只盯日期，把重点放在可执行动作上。"
    advice = _default_fortune_advice(topic, "balanced")[0]
    _metric_incr("time_guard_overwrite_total")
    return f"{out}\n\n{tail}今天先执行：{advice}".strip()


def maybe_append_preferred_name_probe(output: str, session_id: str, should_probe: bool = False) -> str:
    out = str(output or "").strip()
    if not out or not should_probe:
        return out
    if _is_preferred_name_prompt_pending(session_id):
        return out
    if re.search(r"(怎么称呼你|希望我怎么称呼|该怎么称呼你|叫你什么|你希望.*称呼)", out):
        _set_preferred_name_prompt_pending(session_id, True)
        return out
    tail = "顺便问一下，你希望我怎么称呼你呀？可以直接给我一个你喜欢的昵称。"
    _set_preferred_name_prompt_pending(session_id, True)
    return f"{out}\n\n{tail}".strip()


@app.get("/", summary="主页", tags=["Pages"])
@app.get("/index", summary="聊天主页", tags=["Pages"])
async def read_root(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    auth = _get_auth_session(token or "")
    if not auth:
        return RedirectResponse(url="/login", status_code=302)
    phone = str(auth.get("phone", ""))
    user = _get_user_by_phone(phone) or {}
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user_phone": phone,
            "user_short_account": user.get("account", ""),
            "user_uuid": user.get("uuid", ""),
        },
    )


@app.get("/login", summary="登录页", tags=["Pages"])
async def login_page(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token and _get_auth_session(token):
        return RedirectResponse(url="/index", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/register", summary="注册页", tags=["Pages"])
async def register_page(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token and _get_auth_session(token):
        return RedirectResponse(url="/index", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request})


@app.get("/forgot-password", summary="忘记密码页", tags=["Pages"])
async def forgot_password_page(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token and _get_auth_session(token):
        return RedirectResponse(url="/index", status_code=302)
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@app.get("/reset-password", summary="重置密码页", tags=["Pages"])
async def reset_password_page(request: Request):
    return await forgot_password_page(request)


@app.get(
    "/auth/me",
    summary="获取当前登录用户",
    tags=["Auth"],
    responses={401: {"description": "未登录"}},
)
async def auth_me(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    auth = _get_auth_session(token or "")
    if not auth:
        return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)

    phone = str(auth.get("phone", ""))
    user = _get_user_by_phone(phone) or {}
    user_id = int(user["id"]) if user.get("id") else 0
    profile = _get_profile_by_user_id(user_id) if user_id else {"name": "", "birthdate": "", "preferred_name": ""}
    return {
        "ok": True,
        "user": {
            "phone": phone,
            "user_id": str(user.get("uuid") or ""),
            "short_account": str(user.get("account", "")),
        },
        "profile": {
            "name": str((profile or {}).get("name", "")),
            "preferred_name": str((profile or {}).get("preferred_name", "")),
            "birthdate": str((profile or {}).get("birthdate", "")),
        },
    }


@app.get(
    "/quality/metrics",
    summary="质量指标看板",
    tags=["Ops"],
)
async def quality_metrics(days: int = Query(1, ge=1, le=7, description="查看最近N天汇总，范围1-7")):
    return {"ok": True, "data": get_quality_metrics(days=days)}


def _normalize_phone(phone: str) -> str:
    p = re.sub(r"\s+", "", phone or "")
    return p


def _is_valid_cn_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"1\d{10}", phone or ""))


def _aliyun_percent_encode(value: str) -> str:
    return quote(str(value or ""), safe="~")


def _send_sms_via_aliyun(phone: str, code: str, scene: str = "default") -> tuple[bool, str]:
    required = {
        "SMS_ALIYUN_ACCESS_KEY_ID": SMS_ALIYUN_ACCESS_KEY_ID,
        "SMS_ALIYUN_ACCESS_KEY_SECRET": SMS_ALIYUN_ACCESS_KEY_SECRET,
        "SMS_ALIYUN_SIGN_NAME": SMS_ALIYUN_SIGN_NAME,
        "SMS_ALIYUN_TEMPLATE_CODE": SMS_ALIYUN_TEMPLATE_CODE,
    }
    missing = [k for k, v in required.items() if not str(v or "").strip()]
    if missing:
        logger.error(f"短信发送失败：阿里云短信配置缺失 {missing}")
        return False, "SMS_PROVIDER_CONFIG_MISSING"

    params = {
        "AccessKeyId": SMS_ALIYUN_ACCESS_KEY_ID,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": SMS_ALIYUN_REGION_ID,
        "SignName": SMS_ALIYUN_SIGN_NAME,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "TemplateCode": SMS_ALIYUN_TEMPLATE_CODE,
        "TemplateParam": json.dumps({SMS_TEMPLATE_PARAM_CODE_KEY: code}, ensure_ascii=False, separators=(",", ":")),
        "Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2017-05-25",
        "OutId": f"{scene}:{phone}:{int(datetime.utcnow().timestamp())}",
    }
    canonicalized = "&".join(
        f"{_aliyun_percent_encode(k)}={_aliyun_percent_encode(v)}" for k, v in sorted(params.items(), key=lambda x: x[0])
    )
    string_to_sign = f"GET&%2F&{_aliyun_percent_encode(canonicalized)}"
    key = f"{SMS_ALIYUN_ACCESS_KEY_SECRET}&".encode("utf-8")
    signature = base64.b64encode(hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()).decode("utf-8")
    params["Signature"] = signature
    try:
        resp = requests.get(
            f"https://{SMS_ALIYUN_ENDPOINT}/",
            params=params,
            timeout=max(1, int(SMS_HTTP_TIMEOUT_SECONDS or 8)),
        )
    except Exception as e:
        logger.error(f"阿里云短信调用异常: {e}")
        return False, "SMS_PROVIDER_REQUEST_ERROR"
    if resp.status_code != 200:
        logger.error(f"阿里云短信HTTP异常: status={resp.status_code} body={resp.text[:400]}")
        return False, "SMS_PROVIDER_HTTP_ERROR"
    try:
        data = resp.json()
    except Exception:
        logger.error(f"阿里云短信响应非JSON: {resp.text[:400]}")
        return False, "SMS_PROVIDER_BAD_RESPONSE"
    if str(data.get("Code") or "") != "OK":
        logger.error(
            "阿里云短信返回失败: "
            f"Code={data.get('Code')} Message={data.get('Message')} RequestId={data.get('RequestId')}"
        )
        return False, "SMS_PROVIDER_REJECTED"
    return True, "OK"


def _send_sms_code(phone: str, code: str, scene: str = "default") -> tuple[bool, str]:
    provider = str(SMS_PROVIDER or "mock").strip().lower()
    if provider in {"mock", "debug", "local"}:
        return True, "MOCK"
    if provider in {"aliyun", "aliyun_dysmsapi", "aliyun_sms"}:
        return _send_sms_via_aliyun(phone, code, scene=scene)
    logger.error(f"短信发送失败：不支持的SMS_PROVIDER={provider}")
    return False, "SMS_PROVIDER_UNSUPPORTED"


@app.post(
    "/auth/send_code",
    summary="发送验证码",
    tags=["Auth"],
    responses={400: {"description": "请求参数错误"}, 429: {"description": "发送过于频繁"}},
)
async def auth_send_code(payload: SendCodeRequest):
    phone = _normalize_phone(payload.phone)
    scene = str(payload.scene or "default").strip().lower() or "default"
    if not _is_valid_cn_phone(phone):
        return JSONResponse({"ok": False, "message": "请输入有效的11位手机号"}, status_code=400)
    ttl = _sms_cooldown_ttl(phone, scene)
    if ttl > 0:
        return JSONResponse({"ok": False, "message": f"发送过于频繁，请{ttl}秒后再试"}, status_code=429)
    code = f"{secrets.randbelow(900000) + 100000}"
    ok, reason = _send_sms_code(phone, code, scene=scene)
    if not ok:
        return JSONResponse({"ok": False, "message": "短信发送失败，请稍后重试", "reason": reason}, status_code=502)
    _set_sms_code(phone, code, scene=scene)
    resp = {"ok": True, "message": "验证码已发送", "ttl_seconds": RESEND_COOLDOWN_SECONDS}
    # 仅开发/演示环境回传 debug_code；生产环境应关闭
    if SMS_DEBUG_CODE_ENABLED:
        resp["debug_code"] = code
    return resp


@app.post(
    "/auth/verify",
    summary="验证码登录/注册",
    tags=["Auth"],
    responses={400: {"description": "验证码或参数错误"}},
)
async def auth_verify(request: Request, payload: VerifyRequest):
    phone = _normalize_phone(payload.phone)
    code = str(payload.code or "").strip()
    password = str(payload.password or "").strip()
    mode = str(payload.mode or "login").strip().lower()
    if mode not in {"login", "register"}:
        mode = "login"

    if not _is_valid_cn_phone(phone):
        return JSONResponse({"ok": False, "message": "请输入有效的11位手机号"}, status_code=400)
    if not re.fullmatch(r"\d{6}", code):
        return JSONResponse({"ok": False, "message": "请输入6位验证码"}, status_code=400)

    scene = mode
    real_code = _get_sms_code(phone, scene=scene) or _get_sms_code(phone, scene="default")
    if not real_code:
        return JSONResponse({"ok": False, "message": "验证码已过期，请重新发送"}, status_code=400)
    if real_code != code:
        return JSONResponse({"ok": False, "message": "验证码错误"}, status_code=400)

    _delete_sms_code(phone, scene=scene)
    _delete_sms_code(phone, scene="default")
    exists_user = _get_user_by_phone(phone)
    exists = bool(exists_user)
    if mode == "login" and not exists:
        return JSONResponse({"ok": False, "message": "该手机号未注册，请先注册"}, status_code=400)
    if mode == "register" and exists:
        return JSONResponse({"ok": False, "message": "该手机号已注册，请直接登录"}, status_code=400)
    if mode == "register" and not _password_valid(password):
        return JSONResponse({"ok": False, "message": "密码需为8-12位字母或数字"}, status_code=400)

    if not exists:
        user = _create_user_by_phone(phone, password=password)
    else:
        user = exists_user

    token = uuid.uuid4().hex
    _set_auth_session(token, {
        "phone": phone,
        "user_uuid": str(user.get("uuid") or ""),
    })
    _save_auth_session_to_db(int(user["id"]), token, "sms", request)
    resp = JSONResponse(
        {
            "ok": True,
            "message": "登录成功",
            "phone": phone,
            "mode": mode,
            "user_id": str(user["uuid"]),
            "short_account": str(user["account"]),
        }
    )
    resp.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=AUTH_TTL_DAYS * 24 * 3600,
        path="/",
    )
    return resp


@app.post(
    "/auth/login/password",
    summary="账号密码登录",
    tags=["Auth"],
    responses={400: {"description": "账号或密码错误"}},
)
async def auth_login_password(request: Request, payload: PasswordLoginRequest):
    account = str(payload.account or "").strip()
    password = str(payload.password or "").strip()
    if not account or not password:
        return JSONResponse({"ok": False, "message": "请输入账号和密码"}, status_code=400)

    user = _get_user_by_account(account)
    if not user:
        return JSONResponse({"ok": False, "message": "账号或密码错误"}, status_code=400)
    if not _verify_password(password, str(user.get("password_hash") or "")):
        return JSONResponse({"ok": False, "message": "账号或密码错误"}, status_code=400)

    token = uuid.uuid4().hex
    _set_auth_session(token, {
        "phone": str(user.get("phone") or ""),
        "user_uuid": str(user.get("uuid") or ""),
    })
    _save_auth_session_to_db(int(user["id"]), token, "password", request)
    resp = JSONResponse(
        {
            "ok": True,
            "message": "登录成功",
            "phone": str(user.get("phone") or ""),
            "user_id": str(user.get("uuid") or ""),
            "short_account": str(user.get("account") or ""),
        }
    )
    resp.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=AUTH_TTL_DAYS * 24 * 3600,
        path="/",
    )
    return resp


@app.post(
    "/auth/password/verify_code",
    summary="忘记密码-校验验证码",
    tags=["Auth"],
    responses={400: {"description": "验证码或参数错误"}},
)
async def auth_password_verify_code(payload: PasswordVerifyCodeRequest):
    phone = _normalize_phone(payload.phone)
    code = str(payload.code or "").strip()
    if not _is_valid_cn_phone(phone):
        return JSONResponse({"ok": False, "message": "请输入有效的11位手机号"}, status_code=400)
    if not re.fullmatch(r"\d{6}", code):
        return JSONResponse({"ok": False, "message": "请输入6位验证码"}, status_code=400)

    user = _get_user_by_phone(phone)
    if not user:
        return JSONResponse({"ok": False, "message": "该手机号未注册"}, status_code=400)

    real_code = _get_sms_code(phone, scene="reset_password")
    if not real_code:
        return JSONResponse({"ok": False, "message": "验证码已过期，请重新发送"}, status_code=400)
    if real_code != code:
        return JSONResponse({"ok": False, "message": "验证码错误"}, status_code=400)

    _delete_sms_code(phone, scene="reset_password")
    _mark_pwd_reset_verified(phone)
    return {"ok": True, "message": "验证码校验通过"}


@app.post(
    "/auth/password/reset",
    summary="忘记密码-重置密码",
    tags=["Auth"],
    responses={400: {"description": "参数错误或未完成验证码校验"}},
)
async def auth_password_reset(request: Request, payload: PasswordResetRequest):
    phone = _normalize_phone(payload.phone)
    new_password = str(payload.new_password or "").strip()
    confirm_password = str(payload.confirm_password or "").strip()
    if not _is_valid_cn_phone(phone):
        return JSONResponse({"ok": False, "message": "请输入有效的11位手机号"}, status_code=400)
    if new_password != confirm_password:
        return JSONResponse({"ok": False, "message": "两次输入的密码不一致"}, status_code=400)
    if not _password_valid(new_password):
        return JSONResponse({"ok": False, "message": "密码需为8-12位字母或数字"}, status_code=400)
    if not _is_pwd_reset_verified(phone):
        return JSONResponse({"ok": False, "message": "请先完成手机号验证码校验"}, status_code=400)

    user = _get_user_by_phone(phone)
    if not user:
        return JSONResponse({"ok": False, "message": "该手机号未注册"}, status_code=400)

    _update_user_password(int(user["id"]), new_password)
    _log_password_reset(int(user["id"]), phone, request)
    _clear_pwd_reset_verified(phone)
    return {"ok": True, "message": "密码重置成功，请使用账号+密码登录"}


@app.post("/auth/logout", summary="退出登录", tags=["Auth"])
async def auth_logout(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        _delete_auth_session(token)
        _revoke_auth_session_in_db(token)
    resp = JSONResponse({"ok": True, "message": "已退出登录"})
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return resp


@app.post(
    "/chat",
    summary="聊天问答",
    tags=["Chat"],
    responses={401: {"description": "未登录"}},
)
async def chat(request: Request, payload: ChatRequest):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    auth = _get_auth_session(token or "")
    if not auth:
        return JSONResponse({"output": "请先登录后再继续聊天。"}, status_code=401)
    response_data = {"session_id": str(uuid.uuid4().hex), "output": "天机暂时紊乱，请稍后再试。"}
    profile: dict[str, str] = {
        "name": "",
        "birthdate": "",
        "birthtime": "",
        "preferred_name": "",
        "name_confidence": "none",
        "preferred_name_confidence": "none",
    }
    session_id = ""
    user_id = 0
    time_anchor = build_time_anchor()
    flag_snapshot = dict(FEATURE_FLAG_DEFAULTS)
    flag_reason_code = "none"
    domain_intent = "general"
    question_type = "default"
    window_meta: dict | None = None
    try:
        query = payload.query
        if not query:
            response_data["output"] = "呀哈～先告诉吉伊大师你想问什么吧。"
            return response_data
        _metric_incr("time_anchor_applied_total")
        raw_flags = get_v2_flags()
        flags, flag_reason_code = apply_v2_flag_policy(raw_flags)
        flag_snapshot = dict(flags)
        if flags.get("intent_v2"):
            domain_intent = detect_domain_intent(query)
            question_type = detect_question_type(query)
        else:
            if is_dream_query(query):
                domain_intent = "dream"
            elif is_bazi_fortune_query(query) or is_divination_query(query) or is_zodiac_intent_query(query):
                domain_intent = "fortune"
            else:
                domain_intent = "general"
            question_type = "default"
        if flags.get("window_v2") and _need_time_window(query, question_type=question_type):
            window_meta = date_window_resolver(query, time_anchor)

        phone = str(auth.get("phone", ""))
        user = _get_user_by_phone(phone) or {}
        if not user:
            return JSONResponse({"output": "登录信息异常，请重新登录后再试。"}, status_code=401)
        user_id = int(user.get("id") or 0)
        # 会话ID绑定用户UUID，避免依赖前端localStorage导致“清理后数据丢失”
        session_id = str(user.get("uuid") or f"phone_{phone}" or str(uuid.uuid4().hex))
        # 先读历史，再提取本轮资料，最后合并，避免“明明给过又丢失”
        chat_message_history = RedisChatMessageHistory(url=REDIS_URL, session_id=session_id, ttl=SESSION_TTL_SECONDS)
        history_profile = extract_profile_from_history(chat_message_history)
        profile = merge_session_profile(session_id, history_profile)
        pending_preferred_name_prompt = _is_preferred_name_prompt_pending(session_id)
        extracted = extract_profile_from_query(query)
        if pending_preferred_name_prompt and not str(extracted.get("preferred_name") or "").strip():
            pending_name, pending_conf = _extract_preferred_name_with_confidence(query, allow_soft=True)
            if pending_name:
                extracted["preferred_name"] = pending_name
                extracted["preferred_name_confidence"] = pending_conf
        profile = merge_session_profile(session_id, extracted)
        profile_seed_only = _is_profile_seed_only_query(query, extracted)
        preferred_name_set_this_turn = bool(str(extracted.get("preferred_name") or "").strip())
        if preferred_name_set_this_turn:
            _set_preferred_name_prompt_pending(session_id, False)
        should_probe_preferred_name = (
            _is_name_intro_query(query, extracted)
            and not profile_seed_only
            and not str(profile.get("preferred_name") or "").strip()
        )

        def _postprocess_output(raw_output: str, qtype: str = question_type) -> str:
            if qtype == "profile_seed":
                out = str(raw_output or "").strip()
            else:
                out = sanitize_output(raw_output, user_query=query, profile=profile)
            chosen = _sanitize_preferred_name(str(profile.get("preferred_name") or "").strip())
            if preferred_name_set_this_turn and chosen and chosen not in out:
                out = f"好呀～那我就叫你{chosen}。\n\n{out}"
            out = maybe_append_preferred_name_probe(
                out,
                session_id=session_id,
                should_probe=should_probe_preferred_name,
            )
            out = _enforce_min_answer_contract(out, query=query, question_type=qtype)
            return out

        emotion_level = detect_emotion_level(query)
        is_fortune_intent = domain_intent in {"fortune", "zodiac", "divination"}
        if is_fortune_intent:
            _metric_incr("fortune_intent_total")
        fast_reply = get_fast_reply(query, time_anchor=time_anchor, profile=profile)
        if fast_reply:
            safe_fast_reply = validate_time_consistency(fast_reply, query, time_anchor, window_meta=window_meta)
            safe_fast_reply = _postprocess_output(safe_fast_reply, qtype=question_type)
            if profile.get("name") or profile.get("birthdate"):
                response_data = {"session_id": session_id, "output": safe_fast_reply}
            else:
                response_data["output"] = safe_fast_reply
            _append_chat_history(
                chat_message_history,
                query,
                safe_fast_reply,
                user_id=user_id,
                session_id=session_id,
                question_type=question_type,
                route_path="fast_reply",
            )
            track_output_quality(
                session_id,
                response_data.get("output", ""),
                profile=profile,
                query=query,
                question_type=question_type,
            )
            _log_route_observability(
                route_path="fast_reply",
                reason_code=flag_reason_code,
                flag_snapshot=flag_snapshot,
                domain_intent=domain_intent,
                question_type=question_type,
            )
            return response_data
        if profile_seed_only:
            out = _postprocess_output(_build_profile_seed_reply(profile, extracted), qtype="profile_seed")
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type="profile_seed",
                route_path="profile_seed",
            )
            track_output_quality(
                session_id,
                out,
                profile=profile,
                query=query,
                question_type="profile_seed",
            )
            _log_route_observability(
                route_path="profile_seed",
                reason_code="profile_seed_capture",
                flag_snapshot=flag_snapshot,
                domain_intent=domain_intent,
                question_type="profile_seed",
            )
            return {
                "session_id": session_id,
                "output": out,
            }
        dream_reply, dream_meta = route_dream_pipeline(query)
        if dream_reply is not None:
            out = _postprocess_output(dream_reply)
            out = validate_time_consistency(out, query, time_anchor, window_meta=window_meta)
            out = _enforce_min_answer_contract(out, query=query, question_type="dream")
            d_qtype = str(((dream_meta or {}).get("question_type") if isinstance(dream_meta, dict) else "") or "dream")
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type=d_qtype,
                route_path="dream_pipeline",
            )
            track_output_quality(
                session_id,
                out,
                profile=profile,
                query=query,
                question_type=d_qtype,
            )
            _log_route_observability(
                route_path="dream_pipeline",
                reason_code=flag_reason_code,
                flag_snapshot=flag_snapshot,
                domain_intent="dream",
                question_type=d_qtype,
            )
            return {
                "session_id": session_id,
                "output": out,
            }
        zodiac_reply, zodiac_meta = route_zodiac_pipeline(
            query,
            allow_clarify=bool(flags.get("clarify_v2")),
            flags=flags,
            profile=profile,
        )
        if zodiac_reply is not None:
            if is_fortune_intent:
                _metric_incr("fortune_route_hit_total")
            z_qtype = str(((zodiac_meta or {}).get("question_type") if isinstance(zodiac_meta, dict) else "") or question_type)
            z_reason = flag_reason_code
            z_route = "zodiac_pipeline"
            if isinstance(zodiac_meta, dict) and str(zodiac_meta.get("source") or "") in {"zodiac_clarify", "shengxiao_clarify"}:
                z_reason = "zodiac_sign_missing"
                z_route = "zodiac_clarify"
            out = _postprocess_output(zodiac_reply, qtype=z_qtype)
            out = validate_time_consistency(out, query, time_anchor, window_meta=window_meta)
            out = _enforce_min_answer_contract(out, query=query, question_type=z_qtype)
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type=z_qtype,
                route_path=z_route,
            )
            track_output_quality(
                session_id,
                out,
                profile=profile,
                query=query,
                question_type=z_qtype,
            )
            _log_route_observability(
                route_path=z_route,
                reason_code=z_reason,
                flag_snapshot=flag_snapshot,
                domain_intent="zodiac",
                question_type=z_qtype,
            )
            return {
                "session_id": session_id,
                "output": out,
            }
        # P0: 命理强路由，命中后直接返回，不回落通用Agent重写。
        fortune_qtype = question_type if flags.get("intent_v2") else "default"
        fortune_reply, fortune_payload = route_fortune_pipeline(
            query,
            profile,
            time_anchor=time_anchor,
            flags=flags,
            question_type=fortune_qtype,
            session_id=session_id,
        )
        if fortune_reply is not None:
            if is_fortune_intent:
                _metric_incr("fortune_route_hit_total")
            out = _postprocess_output(fortune_reply, qtype=fortune_qtype)
            if not window_meta and isinstance(fortune_payload, dict):
                if str(fortune_payload.get("window_start") or "").strip() and str(fortune_payload.get("window_end") or "").strip():
                    window_meta = {
                        "window_start": str(fortune_payload.get("window_start")),
                        "window_end": str(fortune_payload.get("window_end")),
                        "window_text": str(fortune_payload.get("window_text") or ""),
                    }
            out = validate_time_consistency(out, query, time_anchor, window_meta=window_meta)
            out = _enforce_min_answer_contract(out, query=query, question_type=fortune_qtype)
            qtype_for_metrics = str((fortune_payload or {}).get("question_type") or fortune_qtype)
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type=qtype_for_metrics,
                route_path="fortune_pipeline",
            )
            track_output_quality(
                session_id,
                out,
                profile=profile,
                query=query,
                question_type=qtype_for_metrics,
                quality_meta=fortune_payload if isinstance(fortune_payload, dict) else None,
            )
            route_reason = str((fortune_payload or {}).get("route_reason_code") or "").strip()
            final_reason = route_reason if route_reason and route_reason != "none" else flag_reason_code
            _log_route_observability(
                route_path="fortune_pipeline",
                reason_code=final_reason,
                flag_snapshot=flag_snapshot,
                domain_intent="fortune",
                question_type=qtype_for_metrics,
            )
            return {
                "session_id": session_id,
                "output": out,
                "extra": _build_fortune_chat_extra(fortune_payload),
            }
        time_sensitive = is_time_sensitive_query(query)
        if time_sensitive:
            _metric_incr("time_sensitive_history_isolation_total")
            isolated_id = f"{session_id}:ts:{uuid.uuid4().hex[:8]}"
            agent_history = RedisChatMessageHistory(url=REDIS_URL, session_id=isolated_id, ttl=120)
            near_days = time_anchor.get("near_days") or []
            window_text = "、".join(
                [f"{d.get('date_cn')}（{d.get('weekday_cn')}）" for d in near_days if d.get("date_cn")]
            )
            context_note = (
                f"时间敏感问题请以当前时间锚点为准："
                f"{time_anchor.get('today_cn')}，{time_anchor.get('weekday_cn')}（{time_anchor.get('tz_name')}，{time_anchor.get('utc_offset')}）。"
                f"若涉及“近几天”，默认窗口：{window_text}。不要沿用历史轮次中的旧日期。"
            )
        elif domain_intent == "general" and question_type != "identity_fact":
            _metric_incr("general_history_isolation_total")
            isolated_id = f"{session_id}:general:{uuid.uuid4().hex[:8]}"
            agent_history = RedisChatMessageHistory(url=REDIS_URL, session_id=isolated_id, ttl=120)
            context_note = build_ellipsis_context_note(query, chat_message_history)
        else:
            agent_history = chat_message_history
            context_note = build_ellipsis_context_note(query, chat_message_history)
        style_instruction = build_style_instruction(query, emotion_level, session_id)
        if domain_intent in {"general", "time"} and question_type != "identity_fact":
            general_guardrail = "这是通用问答，不要扩展成八字、星座、生肖、流年或出生资料分析。直接回答用户问题即可。"
            context_note = f"{context_note}\n{general_guardrail}".strip() if context_note else general_guardrail
        profile_context = build_profile_context(
            profile,
            domain_intent=domain_intent,
            question_type=question_type,
            user_query=query,
        )
        #给每个用户赋予一个单独的会话id，为了区分每个用户
        #给每个用户一个单独的session_id，真实的业务场景用户会话管理模块去做这个事
        logger.info(f"用户session_id: {session_id}")
        #ttl 当前会话数据的过期时间，600秒表示10分钟过期
        #用户的会话存入Redis
        master = Master(agent_history)
        #主要的方法
        result = master.run(
            query,
            style_context=style_instruction,
            profile_context=profile_context,
            context_hint=context_note,
        )
        # 确保返回的是字符串，并包含session_id
        response_data = {"session_id": session_id}
        if isinstance(result, dict):
            if 'output' in result:
                logger.info(f"/chat接口最终输出: {result['output']}")
                response_data["output"] = _postprocess_output(result['output'], qtype=question_type)
            else:
                logger.info(f"/chat接口最终输出(无output字段): {str(result)}")
                response_data["output"] = _postprocess_output(str(result), qtype=question_type)
        else:
            logger.info(f"/chat接口最终输出(非dict): {str(result)}")
            response_data["output"] = _postprocess_output(str(result), qtype=question_type)
        response_data["output"] = validate_time_consistency(response_data["output"], query, time_anchor, window_meta=window_meta)
        response_data["output"] = _enforce_min_answer_contract(
            response_data["output"], query=query, question_type=question_type
        )
        track_output_quality(
            session_id,
            response_data.get("output", ""),
            profile=profile,
            query=query,
            question_type=question_type,
        )
        _append_chat_audit_to_db(
            user_id=user_id,
            session_id=session_id,
            query=query,
            output=response_data.get("output", ""),
            question_type=question_type,
            route_path="agent_fallback",
        )
        _log_route_observability(
            route_path="agent_fallback",
            reason_code=flag_reason_code,
            flag_snapshot=flag_snapshot,
            domain_intent=domain_intent,
            question_type=question_type,
        )
    except Exception as e:
        error_id = uuid.uuid4().hex[:8]
        logger.error(f"服务处理异常 error_id={error_id}: {e}\n{traceback.format_exc()}")
        level = detect_emotion_level(getattr(payload, "query", ""))
        if level == "L3":
            response_data["output"] = (
                "抱歉呀，这次系统有点打结了。"
                "你先别急，我会继续帮你盯着；"
                f"过一会儿再发一次，我们把这题慢慢解开。（错误编号：{error_id}）"
            )
        elif level == "L2":
            response_data["output"] = (
                "呜啦…这次请求刚好卡了一下。"
                f"你等30秒再发一次，我会接着刚才的内容继续，不会让你重说。（错误编号：{error_id}）"
            )
        else:
            response_data["output"] = (
                f"呜啦…天机有点乱糟糟。吉伊大师先缓一缓，等会儿再来试试呀。（错误编号：{error_id}）"
            )
        if session_id:
            track_output_quality(
                session_id,
                response_data.get("output", ""),
                profile=profile,
                query=str(getattr(payload, "query", "") or ""),
                question_type=question_type,
            )
        if session_id and user_id > 0:
            _append_chat_audit_to_db(
                user_id=user_id,
                session_id=session_id,
                query=str(getattr(payload, "query", "") or ""),
                output=response_data.get("output", ""),
                question_type=question_type,
                route_path="error",
            )
        _log_route_observability(
            route_path="error",
            reason_code="exception",
            flag_snapshot=flag_snapshot,
            domain_intent=domain_intent,
            question_type=question_type,
        )
    return response_data

@app.post("/add_urls", summary="新增URL知识到向量库", tags=["Knowledge"])
async def add_urls(
    URL: str = Query(..., description="待抓取并入库的网页URL", examples=["https://example.com"]),
    force_recreate: bool = Query(False, description="是否重建向量集合"),
):
    loader = WebBaseLoader(URL)
    docs = loader.load()
    docments = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=50,
    ).split_documents(docs)

    #引入向量数据库
    Qdrant.from_documents(
        docments,
        get_lc_ali_embeddings(),
        path=VECTOR_DB_PATH,
        collection_name=VECTOR_COLLECTION_NAME,
        force_recreate=force_recreate,
    )

    logger.info("向量数据库写入完成")
    return {"ok": "添加成功！", "force_recreate": force_recreate}

if __name__ == '__main__':
    setup_logger()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
