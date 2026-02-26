import os
import re
import secrets
import traceback
import uuid
import json
import hashlib
import hmac
from datetime import datetime
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pymysql
import redis
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
    SMS_DEBUG_CODE_ENABLED,
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


def _get_profile_by_user_id(user_id: int) -> dict[str, str]:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, birth_date, birth_time FROM user_profile WHERE user_id = %s LIMIT 1",
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
    return {"name": row.get("name") or "", "birthdate": birthdate, "birthtime": birthtime}


def _merge_profile_to_db(user_id: int, current: dict[str, str]) -> dict[str, str]:
    profile = _get_profile_by_user_id(user_id)
    merged = profile.copy()
    changed = False
    if current.get("name") and not merged.get("name"):
        merged["name"] = current["name"]
        changed = True
    if current.get("birthdate") and not merged.get("birthdate"):
        merged["birthdate"] = current["birthdate"]
        changed = True
    if current.get("birthtime") and not merged.get("birthtime"):
        merged["birthtime"] = current["birthtime"]
        changed = True
    if changed:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_profile
                    SET name = %s,
                        birth_date = %s,
                        birth_time = %s,
                        updated_at = NOW()
                    WHERE user_id = %s
                    """,
                    (
                        merged.get("name") or None,
                        merged.get("birthdate") or None,
                        merged.get("birthtime") or None,
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
V2_FLAG_DEFAULTS = {
    "intent_v2": True,
    "clarify_v2": True,
    "window_v2": True,
    "render_v2": True,
    "quality_gate_v2": True,
}
V2_FLAG_ENV_KEYS = {
    "intent_v2": "INTENT_V2",
    "clarify_v2": "CLARIFY_V2",
    "window_v2": "WINDOW_V2",
    "render_v2": "RENDER_V2",
    "quality_gate_v2": "QUALITY_GATE_V2",
}


def _to_bool(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def get_v2_flags() -> dict[str, bool]:
    flags = dict(V2_FLAG_DEFAULTS)
    # 环境变量兜底（用于本地/容器静态配置）
    for name, env_key in V2_FLAG_ENV_KEYS.items():
        if env_key in os.environ:
            flags[name] = _to_bool(os.getenv(env_key), flags[name])
    # Redis 动态覆盖（用于灰度/回滚）
    try:
        raw = _REDIS_CLIENT.hgetall(V2_FLAG_REDIS_KEY) or {}
        for name in V2_FLAG_NAMES:
            if name in raw:
                flags[name] = _to_bool(raw.get(name), flags[name])
    except Exception:
        pass
    return flags


def apply_v2_flag_policy(flags: dict[str, bool]) -> tuple[dict[str, bool], str]:
    effective = {k: bool(flags.get(k, False)) for k in V2_FLAG_NAMES}
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
        "flag_snapshot": {k: bool(flag_snapshot.get(k, False)) for k in V2_FLAG_NAMES},
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


def _has_profile_echo(text: str, profile: dict | None = None) -> bool:
    out = str(text or "")
    p = profile or {}
    if not out:
        return False
    name = str(p.get("name") or "").strip()
    birthdate = str(p.get("birthdate") or "").strip()
    birthtime = str(p.get("birthtime") or "").strip()
    if name and name in out:
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
):
    text = str(output or "")
    if not text:
        return

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

    _metric_incr("profile_echo_total")
    if _has_profile_echo(text, profile):
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
        for k, v in row.items():
            if k == "date":
                continue
            totals[k] = totals.get(k, 0) + _safe_int(v)

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


RELATIVE_WINDOW_PATTERN = re.compile(r"(本周|这周|下周|最近一周|这一周|近几天|这几天|哪几天|哪天|最近两天|这两天|本月|这个月)")


def _cn_day(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日（{_weekday_cn_from_date(dt.year, dt.month, dt.day)}）"


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
    if re.search(r"(下周)", q):
        start = (now - timedelta(days=now.weekday())) + timedelta(days=7)
        end = start + timedelta(days=6)
        label = "next_week"
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

    days = _enumerate_days(start, end, limit=14)
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
    r"(今天|现在|当前|日期|几号|星期|周几|近几天|这几天|本周|这周|下周|本月|这个月|时间窗口|哪天|哪几天|刚才|你说错|纠正|纠错|气场)"
)
NEAR_DAYS_QUERY_PATTERN = re.compile(r"(近几天|这几天|哪几天|哪天|最近三天|最近几天)")
DATE_WEEKDAY_PATTERN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日[，,\s]*((?:星期|周)[一二三四五六日天])")
DATE_FULL_PATTERN = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
DATE_SHORT_PATTERN = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")
YEAR_PATTERN = re.compile(r"(20\d{2})年")


def is_time_sensitive_query(query: str) -> bool:
    return bool(TIME_SENSITIVE_QUERY_PATTERN.search(str(query or "")))


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
    if window_meta and RELATIVE_WINDOW_PATTERN.search(q):
        window_text = str(window_meta.get("window_text") or "").strip()
        if window_text:
            return (
                f"呀哈～我先把时间对齐：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。\n"
                f"你问的时间窗口按这个范围计算：{window_text}。"
            )
    if NEAR_DAYS_QUERY_PATTERN.search(q) and near_days:
        window_text = "、".join(
            [f"{d.get('date_cn')}（{d.get('weekday_cn')}）" for d in near_days if d.get("date_cn")]
        )
        return (
            f"呀哈～我先把时间对齐：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。\n"
            f"你问的“近几天”按这个窗口计算：{window_text}。"
        )
    return f"呀哈～先把时间对齐：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。"


def validate_time_consistency(text: str, query: str, time_anchor: dict, window_meta: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    if not is_time_sensitive_query(q) and not is_bazi_fortune_query(q):
        return out
    _metric_incr("temporal_consistency_total")

    allowed_dates = set()
    for d in (time_anchor.get("near_days") or []):
        date_str = str(d.get("date") or "").strip()
        if date_str:
            allowed_dates.add(date_str)
    if str(time_anchor.get("today_date") or "").strip():
        allowed_dates.add(str(time_anchor.get("today_date")))
    if isinstance(window_meta, dict):
        for d in (window_meta.get("days") or []):
            date_str = str((d or {}).get("date") or "").strip()
            if date_str:
                allowed_dates.add(date_str)

    # 校验“日期-星期”匹配
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

    # 对“相对时间窗口”问题，输出日期必须落在允许窗口内
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

        # 近几天场景若出现明显越界年份，直接矫正
        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(out)}
        if years and any(str(y) not in {x[:4] for x in allowed_dates} for y in years):
            _metric_incr("time_validation_fail_total")
            _metric_incr("time_validation_autofix_total")
            _metric_incr("temporal_consistency_fail")
            return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

    _metric_incr("temporal_consistency_hit")
    return out


def get_fast_reply(query: str, time_anchor: dict | None = None) -> str | None:
    raw_text = (query or "").strip()
    text = raw_text.lower()
    if not raw_text:
        return None

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


def _is_valid_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    # 过滤明显非姓名内容，避免把“我是谁”“我叫什么”误识别为名字
    invalid_tokens = {"谁", "谁呀", "谁啊", "什么", "啥", "名字", "姓名", "自己"}
    if n in invalid_tokens:
        return False
    if re.search(r"(谁|什么|吗|呢|呀|啊|\?|？)", n):
        return False
    return True


def extract_profile_from_query(query: str) -> dict[str, str]:
    source = (query or "").strip()
    profile: dict[str, str] = {}
    name_match = re.search(r"(?:我叫|名字是|姓名是|我是)\s*([^\s，。！？,.]{1,16})", source)
    if name_match and _is_valid_name(name_match.group(1)):
        profile["name"] = name_match.group(1).strip()
    birthdate = _normalize_birthdate(source)
    if birthdate:
        profile["birthdate"] = birthdate
    birthtime = _normalize_birthtime(source)
    if birthtime:
        profile["birthtime"] = birthtime
    return profile


def merge_session_profile(session_id: str, current: dict[str, str]) -> dict[str, str]:
    # session_id 在当前实现中绑定用户 uuid
    user = _get_user_by_uuid(session_id)
    if not user:
        return {"name": "", "birthdate": "", "birthtime": ""}
    return _merge_profile_to_db(int(user["id"]), current)


def build_profile_context(profile: dict[str, str]) -> str:
    if not profile:
        return "暂无用户资料。"
    parts = []
    if profile.get("name"):
        parts.append(f"姓名：{profile['name']}")
    if profile.get("birthdate"):
        parts.append(f"出生日期：{profile['birthdate']}")
    if profile.get("birthtime"):
        parts.append(f"出生时间：{profile['birthtime']}")
    if not parts:
        return "暂无用户资料。"
    profile_line = "；".join(parts)
    return f"{profile_line}。可据此推断，但不要逐字回显个人信息。"


def extract_profile_from_history(chat_message_history) -> dict[str, str]:
    profile: dict[str, str] = {}
    try:
        messages = getattr(chat_message_history, "messages", []) or []
        for msg in messages:
            content = getattr(msg, "content", "")
            if not isinstance(content, str):
                continue
            piece = extract_profile_from_query(content)
            if piece.get("name") and not profile.get("name"):
                profile["name"] = piece["name"]
            if piece.get("birthdate") and not profile.get("birthdate"):
                profile["birthdate"] = piece["birthdate"]
            if piece.get("birthtime") and not profile.get("birthtime"):
                profile["birthtime"] = piece["birthtime"]
            if profile.get("name") and profile.get("birthdate") and profile.get("birthtime"):
                break
    except Exception:
        return profile
    return profile


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
    r"(算命|八字|流年|运势|桃花|姻缘|感情运|财运|事业运|学业运|贵人运|命盘|命理|紫微|测算|提运|气场|顺不顺|更顺)"
)
DIVINATION_QUERY_PATTERN = re.compile(r"(占卜|摇卦|抽签|起卦|卦象|卦)")
FORTUNE_SCENE_PATTERN = re.compile(r"(今天|本周|这周|本月|最近|这段时间|现在|未来)")
FORTUNE_DECISION_PATTERN = re.compile(
    r"(开源|守财|扩收入|控支出|先.*还是|二选一|更适合|哪个更|优先|先守后开|守中带开|"
    r"最旺.*方向|行动方向|避免.*决策|换岗.*积累|适合.*换岗|先积累|该不该换岗|更容易提运|提运.*领域)"
)
FORTUNE_COLLOQUIAL_PATTERN = re.compile(r"(气场|更顺|顺不顺|哪几天|哪天|近几天|这几天)")
FORTUNE_TREND_PATTERN = re.compile(r"(本周|这周|最近一周|这一周|走势|趋势|节奏|上半段|下半段)")
FORTUNE_ACTION_PATTERN = re.compile(r"(先做什么|第一步|怎么安排|如何安排|怎么排更稳|怎么做|怎么行动)")
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
ZODIAC_KEYWORD_PATTERN = re.compile(
    r"(星座|流年|年运|月运|周运|运势|桃花|感情|财运|事业|学业|贵人|配对|合盘|复合|水逆)"
)
ZODIAC_FORBIDDEN_BAZI_TERMS = re.compile(r"(八字|四柱|日主|喜用|忌神|五行|地支|天干)")


def _extract_zodiac_sign(query: str) -> str:
    q = str(query or "")
    for canonical, aliases in ZODIAC_SIGN_ALIASES.items():
        for alias in aliases:
            if alias and alias in q:
                return canonical
    return ""


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


def is_zodiac_query(query: str) -> bool:
    q = str(query or "")
    sign = _extract_zodiac_sign(q)
    if not sign:
        return False
    return bool(ZODIAC_KEYWORD_PATTERN.search(q))


def is_zodiac_intent_query(query: str) -> bool:
    q = str(query or "")
    if is_zodiac_query(q):
        return True
    return ("星座" in q) and bool(ZODIAC_KEYWORD_PATTERN.search(q))


def _zodiac_default_reply(sign: str, year: int, topic: str) -> str:
    topic_cn = _topic_cn(topic)
    return (
        f"呀哈～先给你结论：{year}年{sign}{topic_cn}是“先稳后发”的节奏。\n"
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
        "【本周立即执行一步】\n"
        "今晚把下周最重要的一件事写进日程，并锁定第一段执行时间。\n"
        "参考强度：中高（星座解读偏趋势参考）"
    )


def route_zodiac_pipeline(query: str, allow_clarify: bool = True) -> tuple[str | None, dict | None]:
    q = str(query or "").strip()
    if not q:
        return None, None
    if not is_zodiac_intent_query(q):
        return None, None

    sign = _extract_zodiac_sign(q)
    if not sign:
        if not allow_clarify:
            return None, None
        return (
            "呀哈～你想看星座运势我收到了。先告诉我你的星座（例如白羊座/天蝎座），我再给你本周重点和行动建议。",
            {"topic": "zodiac", "source": "zodiac_clarify", "question_type": "clarify"},
        )
    year = _extract_query_year(q)
    topic = detect_fortune_topic(q)
    topic_cn = _topic_cn(topic)
    prompt = ChatPromptTemplate.from_template(
        """你是专业占星顾问。请输出 {year}年{sign} 的{topic_cn}解读，要求：
1) 不要使用八字/五行/日主/喜用神等术语。
2) 先给一句结论，再给“触发点、风险窗口、行动建议、本周一步”。
3) 每条都要可执行，避免空泛鸡汤。
4) 结尾只能写“参考强度：中/中高/高（星座解读偏趋势参考）”之一。
5) 必须严格按以下格式输出：
【结论】
...
【关键触发点】
1. ...
2. ...
3. ...
【风险窗口】
1. ...
2. ...
【行动建议】
1. ...
2. ...
3. ...
【本周立即执行一步】
...
参考强度：...
用户问题：{query}
"""
    )
    try:
        chain = prompt | get_lc_ali_model_client(temperature=0.45, streaming=False) | StrOutputParser()
        text = str(chain.invoke({"year": year, "sign": sign, "topic_cn": topic_cn, "query": q}) or "").strip()
        if not text:
            return _zodiac_default_reply(sign, year, topic), {"topic": topic, "source": "zodiac_fallback"}
        if ZODIAC_FORBIDDEN_BAZI_TERMS.search(text):
            text = re.sub(r"(八字|四柱|日主|喜用神?|忌神|五行|地支|天干)", "星盘线索", text)
        if "参考强度：" not in text:
            text = text.rstrip() + "\n参考强度：中高（星座解读偏趋势参考）"
        return text, {"topic": topic, "source": "zodiac_llm"}
    except Exception:
        return _zodiac_default_reply(sign, year, topic), {"topic": topic, "source": "zodiac_fallback"}


def is_bazi_fortune_query(query: str) -> bool:
    q = str(query or "")
    if is_zodiac_intent_query(q):
        # 星座问法走独立占星链路，避免混入八字术语。
        return False
    if BAZI_FORTUNE_QUERY_PATTERN.search(q):
        return True
    # 覆盖“弱命理意图”问法：未显式写“运势/八字”，但明显在问提运/时运决策。
    if FORTUNE_DECISION_PATTERN.search(q) and FORTUNE_SCENE_PATTERN.search(q):
        return True
    # 覆盖“口语化问运势”表达（如：近哪几天气场更顺）。
    if FORTUNE_COLLOQUIAL_PATTERN.search(q) and FORTUNE_SCENE_PATTERN.search(q):
        return True
    return False


def is_divination_query(query: str) -> bool:
    return bool(DIVINATION_QUERY_PATTERN.search(str(query or "")))


def detect_domain_intent(query: str) -> str:
    q = str(query or "")
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
    if is_time_sensitive_query(q) and not (is_zodiac_intent_query(q) or is_bazi_fortune_query(q)):
        return "time"
    if is_zodiac_intent_query(q) and not _extract_zodiac_sign(q):
        return "clarify"
    if FORTUNE_DECISION_PATTERN.search(q):
        return "decision"
    if FORTUNE_COLLOQUIAL_PATTERN.search(q):
        return "colloquial"
    if FORTUNE_TREND_PATTERN.search(q):
        return "trend"
    if FORTUNE_ACTION_PATTERN.search(q):
        return "action"
    return "default"


def detect_fortune_topic(query: str) -> str:
    q = str(query or "")
    if re.search(r"(桃花|姻缘|感情|恋爱)", q):
        return "love"
    if re.search(r"(财运|财富|收入|金钱)", q):
        return "wealth"
    if re.search(r"(事业|工作|职场|升职)", q):
        return "career"
    if re.search(r"(学业|考试|学习)", q):
        return "study"
    return "daily"


def _missing_profile_fields_for_fortune(profile: dict[str, str]) -> list[str]:
    missing: list[str] = []
    if not str(profile.get("name") or "").strip():
        missing.append("name")
    if not str(profile.get("birthdate") or "").strip():
        missing.append("birthdate")
    return missing


def build_fortune_missing_reply(missing: list[str]) -> str:
    if missing == ["name"]:
        return "呀哈～我先补一个关键资料：请告诉我你的姓名（2-12个字）。"
    if missing == ["birthdate"]:
        return "呀哈～我还需要你的出生年月日（例如 2001-08-15），这样命理判断才更准。"
    return "呀哈～我先帮你把资料补齐：请告诉我姓名和出生年月日（例如 2001-08-15；知道时辰也可以一起说）。"


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
        "advice": [],
        "confidence": 0.2,
        "source": "yuanfenju",
        "error": None,
    }
    payload = _as_dict(raw)
    if not payload:
        base["error"] = {"code": "FORTUNE_PARSE_FAILED", "message": "命理结果解析失败"}
        base["advice"] = _default_fortune_advice(topic, "balanced")
        return base

    for key in ["topic", "bazi", "day_master", "strength", "xiyongshen", "jishen", "source"]:
        if key in payload:
            base[key] = str(payload.get(key) or base[key])

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
    if not isinstance(signals, dict):
        return ""
    mapping = {
        "love": "love",
        "wealth": "wealth",
        "career": "career",
        "study": "career",
        "daily": "career",
    }
    key = mapping.get(topic, "career")
    text = str(signals.get(key) or "").strip()
    return text[:120]


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


def render_user_fortune_reply_v2(
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
        if window_text:
            lines.append(f"时间窗口：{window_text}。")

    signal_line = _signal_for_topic(payload, topic)
    if signal_line:
        lines.append(f"命理信号：{signal_line}")

    basis_parts = []
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
    lines.append(f"依据：{'；'.join(basis_parts) if basis_parts else '以当前盘面趋势判断'}。")

    advice = payload.get("advice") or _default_fortune_advice(topic, strength)
    advice = [str(x).strip() for x in advice if str(x).strip()][:3]
    if advice:
        lines.append("建议：")
        for idx, tip in enumerate(advice, start=1):
            lines.append(f"{idx}. {tip}")
    return "\n".join(lines)


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
    bazi = str(payload.get("bazi") or "").strip()
    day_master = str(payload.get("day_master") or "").strip()
    xiyongshen = str(payload.get("xiyongshen") or "").strip()
    jishen = str(payload.get("jishen") or "").strip()
    scores = payload.get("wuxing_scores") or {}
    score_line = (
        f"金{scores.get('metal', 0)} 木{scores.get('wood', 0)} 水{scores.get('water', 0)} "
        f"火{scores.get('fire', 0)} 土{scores.get('earth', 0)}"
    )
    advice = payload.get("advice") or _default_fortune_advice(topic, str(payload.get("strength") or "balanced"))
    advice = [str(x).strip() for x in advice if str(x).strip()][:3]
    confidence = int(float(payload.get("confidence", 0.2)) * 100)

    lines = [f"呀哈～先给你结论：这次{topic_cn}{strength_text}。"]
    if signal_line:
        lines.append(f"命理信号：{signal_line}")
    basis_parts = []
    if bazi:
        basis_parts.append(f"八字 {bazi}")
    if day_master:
        basis_parts.append(f"日主 {day_master}")
    if xiyongshen:
        basis_parts.append(f"喜用 {xiyongshen}")
    if jishen:
        basis_parts.append(f"忌神 {jishen}")
    lines.append(f"命理依据：{'；'.join(basis_parts) if basis_parts else '以当前盘面趋势判断'}。")
    lines.append(f"五行分布：{score_line}。")
    lines.append("行动建议：")
    for idx, tip in enumerate(advice, start=1):
        lines.append(f"{idx}. {tip}")
    lines.append(f"参考置信度：{confidence}%")
    return "\n".join(lines)


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


def route_fortune_pipeline(
    query: str,
    profile: dict[str, str],
    time_anchor: dict | None = None,
    flags: dict[str, bool] | None = None,
    question_type: str = "default",
) -> tuple[str | None, dict | None]:
    q = str(query or "").strip()
    if not q:
        return None, None
    anchor = time_anchor or build_time_anchor()
    active_flags = flags or dict(V2_FLAG_DEFAULTS)
    window_meta = date_window_resolver(q, anchor) if active_flags.get("window_v2") else None

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
        return _format_divination_reply(raw), {"topic": "divination", "error": None, "question_type": question_type}

    if not is_bazi_fortune_query(q):
        return None, None

    missing = _missing_profile_fields_for_fortune(profile)
    if missing:
        return (
            build_fortune_missing_reply(missing),
            {"topic": detect_fortune_topic(q), "error": {"code": "PROFILE_MISSING"}, "question_type": question_type},
        )

    topic = detect_fortune_topic(q)
    name = str(profile.get("name") or "").strip()
    birthdate = str(profile.get("birthdate") or "").strip()
    birthtime = str(profile.get("birthtime") or "").strip()
    near_days = anchor.get("near_days") or []
    window_text = "、".join([f"{d.get('date_cn')}（{d.get('weekday_cn')}）" for d in near_days if d.get("date_cn")]) or "今天起未来3天"
    if isinstance(window_meta, dict) and str(window_meta.get("window_text") or "").strip():
        window_text = str(window_meta.get("window_text")).strip()
    tool_query = (
        f"请按结构化JSON返回{topic}命理结果。"
        f"姓名：{name}；出生日期：{birthdate}；出生时间：{birthtime or '未知'}；用户问题：{q}。"
        f"当前时间锚点：{anchor.get('today_cn')}（{anchor.get('weekday_cn')}，{anchor.get('tz_name')}，{anchor.get('utc_offset')}）。"
        f"若用户问“近几天/哪几天”，仅允许在此窗口判断：{window_text}。"
    )
    raw = ""
    try:
        raw = bazi_cesuan.invoke(tool_query)
    except Exception:
        try:
            raw = bazi_cesuan.run(tool_query)
        except Exception as e:
            logger.error(f"命理工具调用失败: {e}\n{traceback.format_exc()}")
            raw = {
                "topic": topic,
                "error": {"code": "FORTUNE_PARSE_FAILED", "message": "命理工具调用失败，请稍后重试"},
            }

    payload = _normalize_structured_fortune_payload(raw, topic)
    _metric_incr("fortune_tool_total")
    if not (isinstance(payload.get("error"), dict) and str(payload["error"].get("code") or "")):
        _metric_incr("fortune_tool_success_total")
    _metric_incr("fortune_field_total")
    if _is_fortune_field_complete(payload):
        _metric_incr("fortune_field_complete_total")
    payload["question_type"] = str(question_type or "default")
    payload["now_ts"] = str(anchor.get("now_ts") or "")
    payload["tz"] = str(anchor.get("tz_name") or "")
    if isinstance(window_meta, dict):
        payload["window_start"] = str(window_meta.get("window_start") or "")
        payload["window_end"] = str(window_meta.get("window_end") or "")
        payload["window_text"] = str(window_meta.get("window_text") or "")
    logger.info(
        "fortune_pipeline session_payload: "
        f"topic={payload.get('topic')} error={((payload.get('error') or {}).get('code') if isinstance(payload.get('error'), dict) else '')} "
        f"confidence={payload.get('confidence')} question_type={question_type}"
    )
    if active_flags.get("render_v2"):
        return render_user_fortune_reply_v2(payload, topic, query=q, question_type=question_type, window_meta=window_meta), payload
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
        "吉伊先说重点结论，再给你命盘线索：",
        "先不急着看四柱，本鼠鼠先把方向讲清楚：",
        "呜啦～先把你最关心的答案放前面：",
        "先给你一句最有用的提醒：",
    ]
    seed = hashlib.sha256(f"{user_query}|{out}".encode("utf-8")).hexdigest()
    opening = openings[int(seed[:8], 16) % len(openings)]

    remaining = lines[:first_idx] + lines[first_idx + 1:]
    body = "\n".join(remaining).strip()
    moved_basis = f"（命盘线索：{first_line}）"
    if body:
        return f"{opening}\n{body}\n\n{moved_basis}".strip()
    return f"{opening}\n{moved_basis}".strip()


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


def strip_profile_echo(text: str, profile: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    p = profile or {}
    name = str(p.get("name") or "").strip()
    birthdate = str(p.get("birthdate") or "").strip()
    birthtime = str(p.get("birthtime") or "").strip()

    # 去掉显式资料回显，避免重复用户个人信息
    if name and len(name) >= 2:
        out = out.replace(name, "你")
    if birthdate:
        out = out.replace(birthdate, "你的生日")
        out = out.replace(birthdate.replace("-", "年", 1).replace("-", "月") + "日", "你的生日")
    if birthtime:
        out = out.replace(birthtime, "你的出生时段")
        out = out.replace(f"{birthtime}:00", "你的出生时段")
    out = re.sub(r"(姓名|名字|出生日期|生日|出生时间|时辰)\s*[:：]\s*[^，。；\n]+", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


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

    emotion_level = detect_emotion_level(user_query)
    out = diversify_fortune_opening(out.strip(), user_query=user_query)
    # 降低硬编码后处理，避免把模型答案“模板化”
    out = strip_profile_echo(out, profile=profile)
    if emotion_level in {"L2", "L3"}:
        out = trim_for_emotion_level(out, emotion_level)
        out = add_recovery_tail(out, emotion_level)
    out = add_light_jiyi_particle(out, user_query=user_query)
    return out.strip()


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
    profile = _get_profile_by_user_id(user_id) if user_id else {"name": "", "birthdate": ""}
    return {
        "ok": True,
        "user": {
            "phone": phone,
            "user_id": str(user.get("uuid") or ""),
            "short_account": str(user.get("account", "")),
        },
        "profile": {
            "name": str((profile or {}).get("name", "")),
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
    profile: dict[str, str] = {"name": "", "birthdate": "", "birthtime": ""}
    session_id = ""
    time_anchor = build_time_anchor()
    flag_snapshot = dict(V2_FLAG_DEFAULTS)
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
            domain_intent = "fortune" if (is_bazi_fortune_query(query) or is_divination_query(query) or is_zodiac_intent_query(query)) else "general"
            question_type = "default"
        if flags.get("window_v2") and (
            question_type in {"trend", "colloquial"} or RELATIVE_WINDOW_PATTERN.search(str(query or ""))
        ):
            window_meta = date_window_resolver(query, time_anchor)

        phone = str(auth.get("phone", ""))
        user = _get_user_by_phone(phone) or {}
        if not user:
            return JSONResponse({"output": "登录信息异常，请重新登录后再试。"}, status_code=401)
        # 会话ID绑定用户UUID，避免依赖前端localStorage导致“清理后数据丢失”
        session_id = str(user.get("uuid") or f"phone_{phone}" or str(uuid.uuid4().hex))
        # 先读历史，再提取本轮资料，最后合并，避免“明明给过又丢失”
        chat_message_history = RedisChatMessageHistory(url=REDIS_URL, session_id=session_id, ttl=SESSION_TTL_SECONDS)
        history_profile = extract_profile_from_history(chat_message_history)
        profile = merge_session_profile(session_id, history_profile)
        extracted = extract_profile_from_query(query)
        profile = merge_session_profile(session_id, extracted)
        emotion_level = detect_emotion_level(query)
        is_fortune_intent = domain_intent in {"fortune", "zodiac", "divination"}
        if is_fortune_intent:
            _metric_incr("fortune_intent_total")
        fast_reply = get_fast_reply(query, time_anchor=time_anchor)
        if fast_reply:
            safe_fast_reply = validate_time_consistency(fast_reply, query, time_anchor, window_meta=window_meta)
            if profile.get("name") or profile.get("birthdate"):
                response_data = {"session_id": session_id, "output": safe_fast_reply}
            else:
                response_data["output"] = safe_fast_reply
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
        zodiac_reply, zodiac_meta = route_zodiac_pipeline(query, allow_clarify=bool(flags.get("clarify_v2")))
        if zodiac_reply is not None:
            if is_fortune_intent:
                _metric_incr("fortune_route_hit_total")
            out = sanitize_output(zodiac_reply, user_query=query, profile=profile)
            z_qtype = str(((zodiac_meta or {}).get("question_type") if isinstance(zodiac_meta, dict) else "") or question_type)
            out = validate_time_consistency(out, query, time_anchor, window_meta=window_meta)
            track_output_quality(
                session_id,
                out,
                profile=profile,
                query=query,
                question_type=z_qtype,
            )
            z_reason = flag_reason_code
            z_route = "zodiac_pipeline"
            if isinstance(zodiac_meta, dict) and str(zodiac_meta.get("source") or "") == "zodiac_clarify":
                z_reason = "zodiac_sign_missing"
                z_route = "zodiac_clarify"
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
        )
        if fortune_reply is not None:
            if is_fortune_intent:
                _metric_incr("fortune_route_hit_total")
            out = sanitize_output(fortune_reply, user_query=query, profile=profile)
            if not window_meta and isinstance(fortune_payload, dict):
                if str(fortune_payload.get("window_start") or "").strip() and str(fortune_payload.get("window_end") or "").strip():
                    window_meta = {
                        "window_start": str(fortune_payload.get("window_start")),
                        "window_end": str(fortune_payload.get("window_end")),
                        "window_text": str(fortune_payload.get("window_text") or ""),
                    }
            out = validate_time_consistency(out, query, time_anchor, window_meta=window_meta)
            qtype_for_metrics = str((fortune_payload or {}).get("question_type") or fortune_qtype)
            track_output_quality(
                session_id,
                out,
                profile=profile,
                query=query,
                question_type=qtype_for_metrics,
            )
            _log_route_observability(
                route_path="fortune_pipeline",
                reason_code=flag_reason_code,
                flag_snapshot=flag_snapshot,
                domain_intent="fortune",
                question_type=qtype_for_metrics,
            )
            return {
                "session_id": session_id,
                "output": out,
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
        else:
            agent_history = chat_message_history
            context_note = build_ellipsis_context_note(query, chat_message_history)
        style_instruction = build_style_instruction(query, emotion_level, session_id)
        profile_context = build_profile_context(profile)
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
                response_data["output"] = sanitize_output(result['output'], user_query=query, profile=profile)
            else:
                logger.info(f"/chat接口最终输出(无output字段): {str(result)}")
                response_data["output"] = sanitize_output(str(result), user_query=query, profile=profile)
        else:
            logger.info(f"/chat接口最终输出(非dict): {str(result)}")
            response_data["output"] = sanitize_output(str(result), user_query=query, profile=profile)
        response_data["output"] = validate_time_consistency(response_data["output"], query, time_anchor, window_meta=window_meta)
        track_output_quality(
            session_id,
            response_data.get("output", ""),
            profile=profile,
            query=query,
            question_type=question_type,
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
