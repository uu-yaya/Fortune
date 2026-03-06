import os
import re
import secrets
import time
import traceback
import uuid
import json
import hashlib
import hmac
import difflib
import base64
import threading
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
    DIFY_API_KEY,
    DIFY_BASE_URL,
    DIFY_WORKFLOW_APP_ID,
    MEDIA_INTENT_LLM_FALLBACK,
    MEDIA_INTENT_NEGATION_GUARD,
    MEDIA_INTENT_ROUTER_V2,
    MEDIA_INTENT_ROUTER_V3,
    MEDIA_GEN_ENABLED,
    MEDIA_POLL_INTERVAL_SECONDS,
    MEDIA_TIMEOUT_SECONDS,
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
from dify_media_client import DifyMediaClient
from logger import setup_logger
from media_intent import build_media_prompt, check_media_safety, detect_media_intent, route_media_intent
from media_service import create_media_task, get_media_task, media_task_to_api, refresh_media_task, submit_media_task
from models import get_lc_ali_embeddings, get_lc_ali_model_client
from mytools import (
    bazi_cesuan,
    get_info_from_local_db,
    jiemeng,
    serp_search,
    yaoyigua,
)
from provider_runtime import provider_extra_meta
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
MEDIA_PREF_PROMPT_TTL_SECONDS = 30 * 60
NAME_CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
GENDER_VALUES = {"unknown", "male", "female"}
PARTNER_PREFERENCE_VALUES = {"unknown", "male", "female", "any"}
DIFY_PROMPT_MAX_CHARS = 256
MEDIA_SCENARIO_LABELS = {
    "destined_portrait": "正缘写实画像",
    "destined_video": "正缘视频",
    "encounter_story_video": "正缘相遇剧情片段",
    "healing_sleep_video": "命理治愈视频",
    "general_image": "专属图片",
    "general_video": "专属视频",
}
_REDIS_CLIENT = redis.Redis.from_url(REDIS_URL, decode_responses=True)
_DIFY_MEDIA_CLIENT = (
    DifyMediaClient(
        base_url=DIFY_BASE_URL,
        api_key=DIFY_API_KEY,
        workflow_app_id=DIFY_WORKFLOW_APP_ID or "",
        timeout_seconds=MEDIA_TIMEOUT_SECONDS,
    )
    if DIFY_API_KEY
    else None
)


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


class MediaTaskCreateRequest(BaseModel):
    query: Optional[str] = Field(default=None, description="媒体生成需求", examples=["帮我生成正缘写实画像"])
    scenario: Optional[str] = Field(
        default=None,
        description="可选：直接指定媒体场景",
        examples=[
            "destined_portrait",
            "destined_video",
            "encounter_story_video",
            "healing_sleep_video",
            "general_image",
            "general_video",
        ],
    )
    session_id: Optional[str] = Field(default=None, description="可选会话ID")

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


def _db_conn_txn():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        charset="utf8mb4",
        autocommit=False,
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


def _media_pref_prompt_context_key(session_id: str) -> str:
    sid = str(session_id or "").strip()
    return f"chat:media_pref_prompt_ctx:{sid}"


def _last_media_context_key(session_id: str) -> str:
    sid = str(session_id or "").strip()
    return f"chat:last_media_ctx:{sid}"


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


def _mask_phone_for_ui(phone: str) -> str:
    p = str(phone or "").strip()
    if re.fullmatch(r"1\d{10}", p):
        return f"{p[:3]}****{p[-4:]}"
    return ""


def _resolve_user_from_auth(auth: dict | None) -> tuple[dict, str, str]:
    auth_obj = auth if isinstance(auth, dict) else {}
    phone = str(auth_obj.get("phone") or "").strip()
    user_uuid = str(auth_obj.get("user_uuid") or "").strip()
    user = _get_user_by_phone(phone) if phone else None
    if not user and user_uuid:
        user = _get_user_by_uuid(user_uuid)
    user_obj = user or {}
    resolved_phone = str(user_obj.get("phone") or phone).strip()
    resolved_uuid = str(user_obj.get("uuid") or user_uuid).strip()
    return user_obj, resolved_phone, resolved_uuid


def _build_user_short_account(user: dict | None, phone: str = "", user_uuid: str = "") -> str:
    user_obj = user if isinstance(user, dict) else {}
    account = str(user_obj.get("account") or "").strip()
    if account:
        return account
    masked_phone = _mask_phone_for_ui(phone)
    if masked_phone:
        return masked_phone
    uuid_part = str(user_uuid or user_obj.get("uuid") or "").strip()
    if uuid_part:
        return f"JIYI-{uuid_part[:8].upper()}"
    return "JIYI-USER"


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


def _normalize_gender(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"male", "m", "man", "boy", "男", "男生", "男性", "男的"}:
        return "male"
    if raw in {"female", "f", "woman", "girl", "女", "女生", "女性", "女的"}:
        return "female"
    if raw in GENDER_VALUES:
        return raw
    return "unknown"


def _normalize_partner_gender_preference(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"female", "woman", "women", "girl", "女生", "女性", "女", "女的", "女孩子", "女孩"}:
        return "female"
    if raw in {"male", "man", "men", "boy", "男生", "男性", "男", "男的", "男孩子", "男孩"}:
        return "male"
    if raw in {"any", "all", "both", "不限", "都可以", "都行", "男女都可", "男女都行", "男女都可以"}:
        return "any"
    if raw in PARTNER_PREFERENCE_VALUES:
        return raw
    return "unknown"


def _dump_profile_json(
    preferred_name: str = "",
    name_confidence: str = "",
    preferred_name_confidence: str = "",
    gender: str = "",
    partner_gender_preference: str = "",
) -> str | None:
    payload: dict[str, str] = {}
    call_name = str(preferred_name or "").strip()
    if call_name:
        payload["preferred_name"] = call_name
    n_conf = _normalize_name_confidence(name_confidence)
    p_conf = _normalize_name_confidence(preferred_name_confidence)
    if n_conf != "none":
        payload["name_confidence"] = n_conf
    if p_conf != "none":
        payload["preferred_name_confidence"] = p_conf
    normalized_gender = _normalize_gender(gender)
    if normalized_gender in {"male", "female"}:
        payload["gender"] = normalized_gender
    normalized_partner_pref = _normalize_partner_gender_preference(partner_gender_preference)
    if normalized_partner_pref in {"male", "female", "any"}:
        payload["partner_gender_preference"] = normalized_partner_pref
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return None


def _get_profile_by_user_id(user_id: int) -> dict[str, str]:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            row = _fetch_profile_row(cur, user_id) or {}
    return _profile_from_db_row(row)


def _fetch_profile_row(cur, user_id: int, for_update: bool = False) -> dict:
    sql = "SELECT name, birth_date, birth_time, profile_json FROM user_profile WHERE user_id = %s LIMIT 1"
    if for_update:
        sql = f"{sql} FOR UPDATE"
    cur.execute(sql, (user_id,))
    row = cur.fetchone() or {}
    return row if isinstance(row, dict) else {}


def _profile_from_db_row(row: dict | None) -> dict[str, str]:
    row = row or {}
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
    preferred_name = str(ext.get("preferred_name") or "").strip()
    if preferred_name and not _is_valid_call_name(preferred_name):
        preferred_name = ""
    gender = _normalize_gender(str(ext.get("gender") or "unknown"))
    partner_gender_preference = _normalize_partner_gender_preference(str(ext.get("partner_gender_preference") or "unknown"))
    name_confidence = _normalize_name_confidence(str(ext.get("name_confidence") or ""))
    preferred_name_confidence = _normalize_name_confidence(str(ext.get("preferred_name_confidence") or ""))
    if (row.get("name") or "").strip() and name_confidence == "none":
        name_confidence = "high"
    if preferred_name and preferred_name_confidence == "none":
        preferred_name_confidence = "high"
    if not preferred_name:
        preferred_name_confidence = "none"
    return {
        "name": row.get("name") or "",
        "birthdate": birthdate,
        "birthtime": birthtime,
        "preferred_name": preferred_name,
        "gender": gender,
        "partner_gender_preference": partner_gender_preference,
        "name_confidence": name_confidence,
        "preferred_name_confidence": preferred_name_confidence,
    }


def _get_profile_by_user_id_for_update(cur, user_id: int) -> dict[str, str]:
    row = _fetch_profile_row(cur, user_id, for_update=True)
    if not row:
        cur.execute(
            "INSERT INTO user_profile (user_id) VALUES (%s) ON DUPLICATE KEY UPDATE user_id = user_id",
            (user_id,),
        )
        row = _fetch_profile_row(cur, user_id, for_update=True)
    return _profile_from_db_row(row)


def _merge_profile_payload(profile: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], bool]:
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
    incoming_gender = _normalize_gender(str(current.get("gender") or "unknown"))
    merged_gender = _normalize_gender(str(merged.get("gender") or "unknown"))
    if incoming_gender in {"male", "female"} and incoming_gender != merged_gender:
        merged["gender"] = incoming_gender
        changed = True
    incoming_partner_pref = _normalize_partner_gender_preference(str(current.get("partner_gender_preference") or "unknown"))
    merged_partner_pref = _normalize_partner_gender_preference(str(merged.get("partner_gender_preference") or "unknown"))
    if incoming_partner_pref in {"male", "female", "any"} and incoming_partner_pref != merged_partner_pref:
        merged["partner_gender_preference"] = incoming_partner_pref
        changed = True
    incoming_preferred_name = str(current.get("preferred_name") or "").strip()
    if incoming_preferred_name:
        if not _is_valid_call_name(incoming_preferred_name):
            _metric_incr("name_slot_pollution")
            incoming_preferred_name = ""
        else:
            _metric_incr("name_write_total")
        if incoming_preferred_name:
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
    return merged, changed


def _write_profile_row(cur, user_id: int, merged: dict[str, str]) -> None:
    profile_json = _dump_profile_json(
        preferred_name=str(merged.get("preferred_name") or "").strip(),
        name_confidence=str(merged.get("name_confidence") or "none"),
        preferred_name_confidence=str(merged.get("preferred_name_confidence") or "none"),
        gender=str(merged.get("gender") or "unknown"),
        partner_gender_preference=str(merged.get("partner_gender_preference") or "unknown"),
    )
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


def _merge_profile_to_db(user_id: int, current: dict[str, str]) -> dict[str, str]:
    retry_delays = (0.05, 0.1)
    for attempt in range(len(retry_delays) + 1):
        conn = None
        try:
            conn = _db_conn_txn()
            with conn.cursor() as cur:
                profile = _get_profile_by_user_id_for_update(cur, user_id)
                merged, changed = _merge_profile_payload(profile, current)
                if changed:
                    _write_profile_row(cur, user_id, merged)
            conn.commit()
            return merged
        except pymysql.MySQLError as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            mysql_errno = int(exc.args[0]) if getattr(exc, "args", None) else 0
            if mysql_errno == 1213:
                _metric_incr("profile_merge_deadlock_total")
            elif mysql_errno == 1205:
                _metric_incr("profile_merge_lock_timeout_total")
            if mysql_errno in {1205, 1213} and attempt < len(retry_delays):
                _metric_incr("profile_merge_retry_total")
                logger.warning(
                    "profile merge retry for user_id={} attempt={} mysql_errno={}",
                    user_id,
                    attempt + 1,
                    mysql_errno,
                )
                time.sleep(retry_delays[attempt])
                continue
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise RuntimeError(f"profile merge exhausted retries for user_id={user_id}")


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
    extra_meta: dict | None = None,
):
    event = {
        "route_path": str(route_path or "unknown"),
        "reason_code": str(reason_code or "none"),
        "flag_snapshot": {k: bool(flag_snapshot.get(k, False)) for k in V2_FLAG_NAMES},
        "domain_intent": str(domain_intent or "unknown"),
        "question_type": str(question_type or "default"),
    }
    if isinstance(extra_meta, dict):
        for key, value in extra_meta.items():
            event[str(key)] = value
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


def _recent_advice_signature_session_key(session_id: str) -> str:
    return f"jiyi:recent_advice:session:{session_id}"


def _quality_recent_advice_signature_key(day: datetime | None = None) -> str:
    return f"jiyi:quality:recent_advice:{_quality_day_tag(day)}"


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
    preferred_name = str(p.get("preferred_name") or "").strip()
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


def _rewrite_long_horizon_shrink(text: str, query: str, window_meta: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    window_label = str((window_meta or {}).get("label") or "")
    long_window_labels = {
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
    if not (_is_long_horizon_query(q) or window_label in long_window_labels):
        return out
    patched = out
    replacements = [
        (r"接下来这三天", "这个时间范围内"),
        (r"接下来三天", "这个时间范围内"),
        (r"未来三天", "这个时间范围内"),
        (r"最近三天", "这个时间范围内"),
        (r"这三天", "这个时间范围内"),
        (r"三天内", "这个阶段内"),
        (r"2月27日到3月1日", "这个时间范围内"),
    ]
    for pattern, repl in replacements:
        patched = re.sub(pattern, repl, patched)
    return patched


def _has_fact_hallucination(query: str, output: str, profile: dict | None = None) -> bool:
    if not _is_identity_fact_query(query):
        return False
    out = str(output or "")
    p = profile or {}
    known_name = str(p.get("preferred_name") or "").strip() or str(p.get("name") or "").strip()
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
    single_year_with_year_labels = {"year_full", "year_h1", "year_h2", "explicit_year", "year_partial", "one_year", "multi_year"}
    if label in year_span_labels or label in single_year_with_year_labels or start.year != end.year:
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
YEAR_SCOPE_QUERY_PATTERN = re.compile(r"(20\d{2}|今年|本年|明年|后年|去年|前年|全年|年度)")

SHORT_WINDOW_LABELS = {"near_days", "two_days", "this_week", "next_week"}
LONG_WINDOW_LABELS = {"next_30_days", "coming_period", "this_month"}
YEAR_WINDOW_LABELS = {
    "year_full",
    "year_h1",
    "year_h2",
    "explicit_year",
    "year_partial",
    "one_year",
    "multi_year",
    "compare_year_span",
    "relative_year_span",
    "explicit_year_span",
}


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
    if question_type in {"colloquial"} and label in SHORT_WINDOW_LABELS:
        return True
    if label in SHORT_WINDOW_LABELS:
        return True
    if label in LONG_WINDOW_LABELS and RELATIVE_WINDOW_PATTERN.search(q):
        return True
    if label in YEAR_WINDOW_LABELS and YEAR_SCOPE_QUERY_PATTERN.search(q):
        return True
    return False


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
    if NEAR_DAYS_QUERY_PATTERN.search(q) and near_days:
        window_text = "、".join(
            [f"{d.get('date_cn')}（{d.get('weekday_cn')}）" for d in near_days if d.get("date_cn")]
        )
        return (
            f"呀哈～我先把时间对齐：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。\n"
            f"你问的“近几天”按这个窗口计算：{window_text}。"
        )
    return f"呀哈～先把时间对齐：现在是{now_cn}，{weekday_cn}（{tz_name}，{utc_offset}）。"


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
) -> tuple[str, int, bool, dict[str, bool]]:
    patched = str(out or "")
    conflict_count = 0
    severe_mismatch = False
    reason_flags = {
        "weekday_mismatch": False,
        "window_overflow": False,
        "expected_year_mismatch": False,
        "year_out_of_window": False,
        "large_year_gap": False,
        "too_many_conflicts": False,
    }

    for m in reversed(list(DATE_WEEKDAY_PATTERN.finditer(patched))):
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        weekday_text = _normalize_weekday_label(m.group(4))
        expected = _weekday_cn_from_date(year, month, day)
        if expected and weekday_text and weekday_text != expected:
            conflict_count += 1
            reason_flags["weekday_mismatch"] = True
            _metric_incr("weekday_mismatch_count")
            start = m.start(4)
            end = m.end(4)
            patched = patched[:start] + expected + patched[end:]

    if RELATIVE_WINDOW_PATTERN.search(str(query or "")):
        anchor_year = int(str(time_anchor.get("today_date") or "2000-01-01").split("-")[0])
        allowed_years = _allowed_years_from_window(window_meta)
        full_matches = list(DATE_FULL_PATTERN.finditer(patched))
        for m in reversed(full_matches):
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
            if allowed_years:
                if year not in allowed_years:
                    reason_flags["year_out_of_window"] = True
                    conflict_count += 1
            elif abs(year - anchor_year) >= 2 and _expected_year_from_query(query, time_anchor) is None:
                reason_flags["large_year_gap"] = True
                conflict_count += 1
            if allowed_dates and date_str not in allowed_dates:
                if _date_within_window(date_str, window_meta):
                    continue
                conflict_count += 1
                reason_flags["window_overflow"] = True
                replacement = _pick_closest_allowed_date(date_str, allowed_dates)
                patched = patched[:m.start()] + _iso_to_cn(replacement, short=False) + patched[m.end():]
        full_spans = [(m.start(), m.end()) for m in DATE_FULL_PATTERN.finditer(patched)]
        short_matches = list(DATE_SHORT_PATTERN.finditer(patched))
        for m in reversed(short_matches):
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
                reason_flags["window_overflow"] = True
                replacement = _pick_closest_allowed_date(date_str, allowed_dates)
                patched = patched[:m.start()] + _iso_to_cn(replacement, short=True) + patched[m.end():]
        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(patched)}
        if years:
            if allowed_years and any(y not in allowed_years for y in years):
                reason_flags["year_out_of_window"] = True
                conflict_count += 1
            elif not allowed_years and any(abs(y - anchor_year) >= 2 for y in years):
                if _expected_year_from_query(query, time_anchor) is None:
                    reason_flags["large_year_gap"] = True
                    conflict_count += 1

    expected_year = _expected_year_from_query(query, time_anchor)
    if expected_year is not None:
        year_matches = list(YEAR_PATTERN.finditer(patched))
        for m in reversed(year_matches):
            year = int(m.group(1))
            if year == expected_year:
                continue
            conflict_count += 1
            reason_flags["expected_year_mismatch"] = True
            patched = patched[:m.start(1)] + str(expected_year) + patched[m.end(1):]

    if reason_flags["year_out_of_window"] and conflict_count >= 4:
        severe_mismatch = True
    if reason_flags["large_year_gap"] and conflict_count >= 3:
        severe_mismatch = True
    if conflict_count >= 6:
        reason_flags["too_many_conflicts"] = True
        severe_mismatch = True
    return patched, conflict_count, severe_mismatch, reason_flags


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
            _record_temporal_failure({"weekday_mismatch": True}, severe=True)
            _metric_incr("weekday_mismatch_count")
            _log_temporal_consistency_event(
                "legacy_weekday_mismatch",
                q,
                window_meta,
                conflict_count=1,
                severe_mismatch=True,
                reason_flags={"weekday_mismatch": True},
            )
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
                _record_temporal_failure({"window_overflow": True}, severe=True)
                _log_temporal_consistency_event(
                    "legacy_window_overflow",
                    q,
                    window_meta,
                    conflict_count=1,
                    severe_mismatch=True,
                    reason_flags={"window_overflow": True},
                )
                return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(out)}
        if years and any(str(y) not in {x[:4] for x in allowed_dates} for y in years):
            _record_temporal_failure({"window_overflow": True}, severe=True)
            _log_temporal_consistency_event(
                "legacy_year_outside_window",
                q,
                window_meta,
                conflict_count=1,
                severe_mismatch=True,
                reason_flags={"window_overflow": True},
            )
            return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

    expected_year = _expected_year_from_query(q, time_anchor)
    if expected_year is not None:
        years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(out)}
        if years and any(y != expected_year for y in years):
            _record_temporal_failure({"expected_year_mismatch": True}, severe=True)
            _log_temporal_consistency_event(
                "legacy_expected_year_mismatch",
                q,
                window_meta,
                conflict_count=1,
                severe_mismatch=True,
                reason_flags={"expected_year_mismatch": True},
            )
            return _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)

    _metric_incr("temporal_consistency_hit")
    _log_temporal_consistency_event(
        "legacy_pass",
        q,
        window_meta,
        conflict_count=0,
        severe_mismatch=False,
        reason_flags={},
    )
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


def _record_temporal_failure(reason_flags: dict[str, bool] | None = None, severe: bool = False):
    flags = reason_flags or {}
    _metric_incr("time_validation_fail_total")
    _metric_incr("time_validation_autofix_total")
    _metric_incr("temporal_consistency_fail")
    if severe:
        _metric_incr("temporal_fail_severe_total")
    if bool(flags.get("expected_year_mismatch")):
        _metric_incr("temporal_fail_expected_year_total")
    if bool(flags.get("window_overflow")):
        _metric_incr("temporal_fail_window_overflow_total")


def _log_temporal_consistency_event(
    branch: str,
    query: str,
    window_meta: dict | None,
    conflict_count: int,
    severe_mismatch: bool,
    reason_flags: dict[str, bool] | None = None,
):
    window_label = ""
    if isinstance(window_meta, dict):
        window_label = str(window_meta.get("label") or "")
    event = {
        "branch": str(branch or "none"),
        "question_type": detect_question_type(str(query or "")),
        "window_label": window_label or "none",
        "conflict_count": int(conflict_count or 0),
        "severe_mismatch": bool(severe_mismatch),
        "reason_flags": {k: bool(v) for k, v in (reason_flags or {}).items()},
    }
    logger.info(f"temporal_consistency={json.dumps(event, ensure_ascii=False)}")


def _sanitize_time_tokens_before_validation(text: str, query: str, window_meta: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    qtype = detect_question_type(q)
    window_label = str((window_meta or {}).get("label") or "")
    allow_window_text = _should_show_window_text(q, window_label, question_type=qtype)
    normalized = out.replace("\\n", "\n")

    # 非趋势/时窗问法，且不应展示窗口时，避免输出具体日期，降低时序一致性误伤。
    if (not allow_window_text) and qtype not in {"trend", "colloquial"} and not RELATIVE_WINDOW_PATTERN.search(q):
        normalized = re.sub(r"(20\d{2}年\d{1,2}月\d{1,2}日)", "这个时间范围", normalized)
        normalized = re.sub(r"(?<!\d)(\d{1,2})月(\d{1,2})日(?!\d)", "这段时间", normalized)

    # 不应展示窗口时，删除窗口对齐行，保留业务建议。
    if not allow_window_text:
        kept: list[str] = []
        for line in [ln.strip() for ln in normalized.splitlines() if ln.strip()]:
            if re.search(r"(时间上先对齐|时间窗口|窗口按这个范围计算|从\d{1,2}月\d{1,2}日到\d{1,2}月\d{1,2}日)", line):
                continue
            kept.append(line)
        if kept:
            normalized = "\n".join(kept).strip()
    return normalized.strip()


def _has_explicit_window_text(text: str) -> bool:
    out = str(text or "")
    if not out:
        return False
    if DATE_FULL_PATTERN.search(out):
        return True
    if DATE_SHORT_PATTERN.search(out) and re.search(r"(至|到|—|-)", out):
        return True
    return False


def _ensure_time_window_contract(text: str, query: str, time_anchor: dict, window_meta: dict | None = None) -> str:
    out = str(text or "").strip()
    if not out:
        return out
    q = str(query or "")
    qtype = detect_question_type(q)
    window_label = str((window_meta or {}).get("label") or "")
    window_text = str((window_meta or {}).get("window_text") or "").strip()
    allow_window_text = _should_show_window_text(q, window_label, question_type=qtype)

    patched = out
    if allow_window_text and window_text and not _has_explicit_window_text(patched):
        patched = f"{patched}\n时间窗口：{window_text}。".strip()

    expected_year = _expected_year_from_query(q, time_anchor)
    if expected_year is not None and not re.search(rf"(?<!\d){expected_year}(?!\d)", patched):
        patched = f"{patched}\n本次按{expected_year}年窗口解读。".strip()
    return patched


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
    out = _sanitize_time_tokens_before_validation(out, q, window_meta=window_meta)
    out = _rewrite_long_horizon_shrink(out, q, window_meta=window_meta)

    allowed_dates = _collect_allowed_dates(time_anchor, window_meta=window_meta)
    patched, conflict_count, severe_mismatch, reason_flags = _patch_time_text_locally(
        out,
        q,
        time_anchor,
        allowed_dates,
        window_meta=window_meta,
    )
    if severe_mismatch:
        _record_temporal_failure(reason_flags, severe=True)
        _log_temporal_consistency_event(
            "patch_severe_fallback",
            q,
            window_meta,
            conflict_count=conflict_count,
            severe_mismatch=True,
            reason_flags=reason_flags,
        )
        safe = _build_time_safe_fallback(q, time_anchor, window_meta=window_meta)
        residual = _strip_time_alignment_sentences(patched)
        if not residual:
            residual = _extract_business_sentences(patched)
        if residual:
            residual = _rewrite_long_horizon_shrink(residual, q, window_meta=window_meta)
            merged = f"{safe}\n\n{residual}".strip()
            return _ensure_time_window_contract(merged, q, time_anchor, window_meta=window_meta)
        _metric_incr("time_guard_overwrite_total")
        return _ensure_time_window_contract(safe, q, time_anchor, window_meta=window_meta)
    if conflict_count > 0:
        _metric_incr("time_validation_fail_total")
        _metric_incr("time_validation_autofix_total")
        _metric_incr("time_validation_patch_total")
        _log_temporal_consistency_event(
            "patch_autofix",
            q,
            window_meta,
            conflict_count=conflict_count,
            severe_mismatch=False,
            reason_flags=reason_flags,
        )
    _metric_incr("temporal_consistency_hit")
    if conflict_count == 0:
        _log_temporal_consistency_event(
            "patch_pass",
            q,
            window_meta,
            conflict_count=0,
            severe_mismatch=False,
            reason_flags=reason_flags,
        )
    patched = _rewrite_long_horizon_shrink(patched, q, window_meta=window_meta)
    return _ensure_time_window_contract(patched, q, time_anchor, window_meta=window_meta)


def get_fast_reply(query: str, time_anchor: dict | None = None, profile: dict | None = None) -> str | None:
    raw_text = (query or "").strip()
    text = raw_text.lower()
    if not raw_text:
        return None
    if _is_asking_own_name(raw_text):
        p = profile or {}
        call_name = str(p.get("preferred_name") or "").strip()
        legal_name = str(p.get("name") or "").strip()
        if _is_valid_call_name(call_name):
            return f"呀哈～本鼠鼠记得呀，我喜欢叫你{call_name}。"
        if _is_valid_name(legal_name):
            return f"呀哈～我记得你叫{legal_name}。"
        return "呜啦～我这边还没有你的姓名记录。你可以直接告诉我“我叫XXX”，我就会记住啦。"

    if _is_asking_own_gender(raw_text):
        p = profile or {}
        gender = _normalize_gender(str(p.get("gender") or "unknown"))
        if gender == "male":
            return "呀哈～你是男生，本鼠鼠记住啦。"
        if gender == "female":
            return "呀哈～你是女生，本鼠鼠记住啦。"
        return "呜啦～你还没告诉我性别呢。你可以说“我是男生”或“我是女生”，我会记住并用中性称呼兜底。"

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


def _extract_gender_with_confidence(query: str) -> tuple[str, str]:
    source = str(query or "").strip()
    if not source:
        return "unknown", "none"
    if re.search(r"[？?]", source):
        return "unknown", "none"
    if re.search(r"(不是|并非|不算)\s*(男|女)", source):
        return "unknown", "none"
    compact = re.sub(r"\s+", "", source)
    male_tokens = {"男", "男生", "男的", "男性"}
    female_tokens = {"女", "女生", "女的", "女性"}
    if compact in male_tokens:
        return "male", "high"
    if compact in female_tokens:
        return "female", "high"
    male_patterns = [
        r"(?:我是|本人是|本人|我的性别是|我的性别为|性别是|性别为|性别[:：])\s*(?:一)?(?:个)?\s*(男生|男的|男性|男)",
    ]
    female_patterns = [
        r"(?:我是|本人是|本人|我的性别是|我的性别为|性别是|性别为|性别[:：])\s*(?:一)?(?:个)?\s*(女生|女的|女性|女)",
    ]
    for pattern in male_patterns:
        if re.search(pattern, source):
            return "male", "high"
    for pattern in female_patterns:
        if re.search(pattern, source):
            return "female", "high"
    return "unknown", "none"


def _extract_partner_gender_preference_with_confidence(query: str, allow_bare_reply: bool = False) -> tuple[str, str]:
    source = str(query or "").strip()
    if not source:
        return "unknown", "none"
    if re.search(r"[？?]", source):
        return "unknown", "none"
    if re.search(r"(不是|并非|不喜欢)\s*(男生|女生|男性|女性|男的|女的)", source):
        return "unknown", "none"

    compact = re.sub(r"[\s，。！？,.!?；;:：]", "", source)
    if allow_bare_reply:
        if compact in {"女生", "女性", "女", "女的", "女孩子", "女孩"}:
            return "female", "high"
        if compact in {"男生", "男性", "男", "男的", "男孩子", "男孩"}:
            return "male", "high"
        if compact in {"不限", "都可以", "都行", "男女都可", "男女都行", "男女都可以"}:
            return "any", "high"
    if re.fullmatch(r"我喜欢(女生|女性|女孩子|女孩|女的)", compact):
        return "female", "high"
    if re.fullmatch(r"我喜欢(男生|男性|男孩子|男孩|男的)", compact):
        return "male", "high"
    if re.search(r"^我喜欢.*(女生|女性|女孩子|女孩|女的)", compact):
        return "female", "high"
    if re.search(r"^我喜欢.*(男生|男性|男孩子|男孩|男的)", compact):
        return "male", "high"
    if re.search(r"(我|本人).{0,2}(男女都可以|男女都行|都可以|都行|不限)", source):
        return "any", "high"

    romance_ctx = bool(re.search(r"(正缘|对象|另一半|恋爱|感情|取向|性取向|桃花)", source))
    if romance_ctx:
        if re.search(r"(喜欢|偏好|取向|希望|想要).{0,4}(女生|女性|女孩子|女孩|女的)", source):
            return "female", "high"
        if re.search(r"(喜欢|偏好|取向|希望|想要).{0,4}(男生|男性|男孩子|男孩|男的)", source):
            return "male", "high"
        if re.search(r"(都可以|都行|不限|男女都)", source):
            return "any", "high"

    return "unknown", "none"


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
        "学业", "事业", "感情", "财运", "运势", "工作", "学习",
    }
    if n in invalid_tokens:
        return False
    if re.search(r"(怎么|什么|谁|吗|呢|呀|啊|\?|？|!|！|,|，|。)", n):
        return False
    # 拦截明显语义短句/身份描述，避免把“我是男生”等写成昵称
    if re.search(r"(我是|我叫|叫我|性别|男生|女生|男性|女性|先生|小姐|女士|男士|不是|并非|不算)", n):
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
        candidate = re.sub(r"(吧|呀|啦|哦|喔|呢|吗|嘛|么|？|\?)$", "", candidate).strip()
        if not _is_valid_call_name(candidate):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        if _looks_like_time_or_date_fragment(candidate):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        _metric_incr("name_slot_total")
        return candidate, "high"
    if allow_soft:
        # 仅在“待确认称呼”场景下兜底接收简短昵称，避免把整句自我描述误写入别称
        if re.search(r"(我是|我是个|我是一个|本人|我属于|我这个)", source):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        normalized = re.sub(r"[\s，。！？,.!？、；;:：]", "", source)
        normalized = re.sub(r"^(那就|就|那|嗯|啊|呀|呜啦|呀哈)", "", normalized).strip()
        normalized = re.sub(r"(吧|呀|啦|哦|喔|呢|吗|嘛|么|就行|即可)$", "", normalized).strip()
        # 只接受“短且像称呼”的独立词，不接受“男生/很好的人/我是XX”这类描述句
        if re.search(r"(男生|女生|的人|一个人|很好|不错|普通|学生|老师|打工人|社畜|i人|e人)", normalized):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        if re.search(r"^(学业|事业|感情|财运|运势|工作|学习)$", normalized):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{1,6}", normalized):
            _metric_incr("name_slot_total")
            _metric_incr("name_slot_pollution")
            return "", "none"
        if 1 <= len(normalized) <= 6 and _is_valid_call_name(normalized) and not _looks_like_time_or_date_fragment(normalized):
            _metric_incr("name_slot_total")
            return normalized, "medium"
    if re.search(r"(叫我|喊我|称呼我|昵称|名字)", source):
        _metric_incr("name_slot_total")
        _metric_incr("name_slot_pollution")
    return "", "none"


def _extract_preferred_name_from_query(query: str) -> str:
    preferred_name, _ = _extract_preferred_name_with_confidence(query, allow_soft=False)
    return preferred_name


def _is_soft_preferred_name_reply(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    # 软提取只接受“短独立词”回复，避免把正常对话句子误识别为昵称
    if len(q) > 6:
        return False
    if re.search(r"[，。！？,.!?；;:：\s]", q):
        return False
    if re.search(r"(我|你|他|她|它|是|有|在|要|会|想|觉得|知道|告诉|因为|所以|但是|然后)", q):
        return False
    if re.search(r"(今天|明天|后天|工作|学习|感情|运势|视频|图片|生成|帮我|可以|怎么|为什么|吗|呢|呀|啊|吧)", q):
        return False
    if re.search(r"^(学业|事业|感情|财运|运势|工作|学习)$", q):
        return False
    if _looks_like_time_or_date_fragment(q):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{1,6}", q))


def _is_asking_own_name(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    return bool(
        re.search(r"(我叫什?么|我是谁|我的名字|你知道我叫什?么|你知道我的名字|我叫什么名字|记得我叫)", q)
    )


def _is_asking_own_gender(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    return bool(
        re.search(r"(我的性别|我是什么性别|我是男生还是女生|我是男的还是女的|我男还是女|你知道我的性别)", q)
    )


def _is_identity_fact_query(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    if _is_asking_own_name(q):
        return True
    if _is_asking_own_gender(q):
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
    preferred_name = str(p.get("preferred_name") or "").strip()
    if _is_valid_call_name(preferred_name):
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


def _set_media_pref_prompt_context(session_id: str, query: str, scenario: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    key = _media_pref_prompt_context_key(sid)
    payload = {
        "query": str(query or "").strip(),
        "scenario": str(scenario or "").strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _REDIS_CLIENT.setex(key, MEDIA_PREF_PROMPT_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
    except Exception:
        return


def _get_media_pref_prompt_context(session_id: str) -> dict[str, str] | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    key = _media_pref_prompt_context_key(sid)
    try:
        raw = _REDIS_CLIENT.get(key)
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        obj = json.loads(str(raw))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return {
        "query": str(obj.get("query") or "").strip(),
        "scenario": str(obj.get("scenario") or "").strip(),
        "created_at": str(obj.get("created_at") or "").strip(),
    }


def _clear_media_pref_prompt_context(session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    key = _media_pref_prompt_context_key(sid)
    try:
        _REDIS_CLIENT.delete(key)
    except Exception:
        return


def _set_last_media_context(session_id: str, *, task_id: str, scenario: str, query: str, status: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    payload = {
        "task_id": str(task_id or "").strip(),
        "scenario": str(scenario or "").strip(),
        "query": str(query or "").strip(),
        "status": str(status or "").strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _REDIS_CLIENT.setex(
            _last_media_context_key(sid),
            SESSION_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        return


def _get_last_media_context(session_id: str) -> dict[str, str] | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    try:
        raw = _REDIS_CLIENT.get(_last_media_context_key(sid))
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        obj = json.loads(str(raw))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return {
        "task_id": str(obj.get("task_id") or "").strip(),
        "scenario": str(obj.get("scenario") or "").strip(),
        "query": str(obj.get("query") or "").strip(),
        "status": str(obj.get("status") or "").strip(),
        "created_at": str(obj.get("created_at") or "").strip(),
    }


def _scenario_requires_partner_preference(scenario: str) -> bool:
    return str(scenario or "").strip() in {"destined_portrait", "destined_video", "encounter_story_video"}


def _scenario_requires_profile_for_media(scenario: str) -> bool:
    return str(scenario or "").strip() in {"destined_portrait", "destined_video", "encounter_story_video"}


def _partner_preference_cn(value: str) -> str:
    normalized = _normalize_partner_gender_preference(value)
    if normalized == "female":
        return "女生"
    if normalized == "male":
        return "男生"
    if normalized == "any":
        return "不限"
    return ""


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
    preferred_name, preferred_conf = _extract_preferred_name_with_confidence(source, allow_soft=False)
    if preferred_name:
        profile["preferred_name"] = preferred_name
        profile["preferred_name_confidence"] = preferred_conf
    gender, gender_conf = _extract_gender_with_confidence(source)
    if gender in {"male", "female"} and gender_conf == "high":
        profile["gender"] = gender
    partner_pref, partner_pref_conf = _extract_partner_gender_preference_with_confidence(source)
    if partner_pref in {"male", "female", "any"} and partner_pref_conf == "high":
        profile["partner_gender_preference"] = partner_pref
    return profile


def merge_session_profile(session_id: str, current: dict[str, str]) -> dict[str, str]:
    # session_id 在当前实现中绑定用户 uuid
    user = _get_user_by_uuid(session_id)
    if not user:
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
    return _merge_profile_to_db(int(user["id"]), current)


def build_profile_context(profile: dict[str, str]) -> str:
    if not profile:
        return "暂无用户资料。"
    parts = []
    if profile.get("name"):
        parts.append(f"姓名：{profile['name']}")
    if profile.get("preferred_name"):
        parts.append(f"称呼偏好：{profile['preferred_name']}")
    if profile.get("birthdate"):
        parts.append(f"出生日期：{profile['birthdate']}")
    if profile.get("birthtime"):
        parts.append(f"出生时间：{profile['birthtime']}")
    gender = _normalize_gender(str(profile.get("gender") or "unknown"))
    partner_pref = _normalize_partner_gender_preference(str(profile.get("partner_gender_preference") or "unknown"))
    if gender == "male":
        parts.append("性别：男")
    elif gender == "female":
        parts.append("性别：女")
    if partner_pref == "male":
        parts.append("情感对象偏好：男性")
    elif partner_pref == "female":
        parts.append("情感对象偏好：女性")
    elif partner_pref == "any":
        parts.append("情感对象偏好：不限性别")
    if not parts:
        return "暂无用户资料。未提供性别时，只能使用中性称呼（你/同学），不得猜测用户性别。"
    profile_line = "；".join(parts)
    if gender == "unknown":
        gender_rule = "未提供性别时，只能使用中性称呼（你/同学），不得猜测用户是小姐/先生。"
    else:
        gender_rule = "可参考已提供的性别信息，但不要臆测和扩展其他性别线索。"
    return f"{profile_line}。可自然使用用户偏好的称呼；{gender_rule}不要逐字回显完整生日和时辰。"


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
            if piece.get("gender") and _normalize_gender(str(profile.get("gender") or "unknown")) == "unknown":
                profile["gender"] = _normalize_gender(str(piece.get("gender") or "unknown"))
            if (
                piece.get("partner_gender_preference")
                and _normalize_partner_gender_preference(str(profile.get("partner_gender_preference") or "unknown")) == "unknown"
            ):
                profile["partner_gender_preference"] = _normalize_partner_gender_preference(
                    str(piece.get("partner_gender_preference") or "unknown")
                )
            # 不能仅凭姓名/生日/时辰提前结束扫描；性别可能在后续轮次才出现。
            if (
                profile.get("name")
                and profile.get("birthdate")
                and profile.get("birthtime")
                and _normalize_gender(str(profile.get("gender") or "unknown")) != "unknown"
            ):
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
    extra_meta: dict | None = None,
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
    if isinstance(extra_meta, dict):
        for k, v in extra_meta.items():
            meta_obj[str(k)] = v
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
    extra_meta: dict | None = None,
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
            extra_meta=extra_meta,
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
        extra_meta=extra_meta,
    )


def _media_ready() -> bool:
    return bool(MEDIA_GEN_ENABLED and _DIFY_MEDIA_CLIENT and DIFY_WORKFLOW_APP_ID)


def _build_media_task_response(session_id: str, task: dict | None) -> dict:
    payload = media_task_to_api(task or {})
    return {
        "session_id": str(session_id or ""),
        "output": str(payload.get("output") or ""),
        "message_type": str(payload.get("message_type") or "media_failed"),
        "media_task_id": str(payload.get("task_id") or ""),
        "media": payload.get("media") if isinstance(payload.get("media"), list) else [],
        "poll_interval_seconds": int(max(1, MEDIA_POLL_INTERVAL_SECONDS)),
        "extra": payload.get("extra") if isinstance(payload.get("extra"), dict) else {},
    }


def _provider_category_from_error_code(error_code: str) -> str:
    code = str(error_code or "").strip().upper()
    if not code:
        return ""
    if code in {"DIFY_PROVIDER_LIMIT", "DIFY_HTTP_429"}:
        return "quota"
    if code in {"DIFY_HTTP_401", "DIFY_HTTP_403"}:
        return "auth"
    if code == "DIFY_TIMEOUT":
        return "timeout"
    if code == "DIFY_REQUEST_FAILED":
        return "network"
    if code == "DIFY_HTTP_5XX":
        return "http_5xx"
    if code == "DIFY_INVALID_RESPONSE":
        return "invalid_response"
    if code == "DIFY_BREAKER_OPEN":
        return "unknown"
    return ""


def _media_provider_failure_meta(task: dict | None) -> dict:
    if not isinstance(task, dict):
        return {}
    payload = media_task_to_api(task)
    provider = str(payload.get("provider") or "").strip()
    provider_error_code = str(payload.get("provider_error_code") or "").strip()
    if not provider or not provider_error_code:
        return {}
    return {
        "provider": provider,
        "category": _provider_category_from_error_code(provider_error_code),
        "error_code": provider_error_code,
    }


def _default_media_destiny_hint(profile: dict[str, str], scenario: str) -> str:
    if scenario == "healing_sleep_video":
        return (
            "近期命理节奏以稳为先，适合低刺激、慢节奏、自然光和柔和色调的治愈表达；"
            "暗示语以安定、放松、恢复能量为主。"
        )
    partner_pref = _partner_preference_cn(str(profile.get("partner_gender_preference") or "unknown"))
    base = "桃花节奏偏慢热，正缘更看重真诚沟通、情绪稳定与长期陪伴。"
    if scenario == "encounter_story_video":
        base += "相遇场景宜放在日常但有仪式感的空间，比如书店、咖啡馆或傍晚街角。"
    if partner_pref:
        base += f"对象形象偏向{partner_pref}。"
    return base


def _build_media_destiny_hint(query: str, profile: dict[str, str], scenario: str) -> str:
    scenario_name = str(scenario or "").strip()
    if scenario_name not in {"destined_portrait", "destined_video", "encounter_story_video", "healing_sleep_video"}:
        return ""
    topic = "daily" if scenario_name == "healing_sleep_video" else "love"
    name = str(profile.get("name") or "").strip()
    birthdate = str(profile.get("birthdate") or "").strip()
    if not name or not birthdate:
        return _default_media_destiny_hint(profile, scenario_name)
    birthtime = str(profile.get("birthtime") or "").strip() or "未知"
    gender = _normalize_gender(str(profile.get("gender") or "unknown"))
    gender_cn = "男" if gender == "male" else "女" if gender == "female" else "未知"
    partner_pref_cn = _partner_preference_cn(str(profile.get("partner_gender_preference") or "unknown")) or "未说明"
    anchor = build_time_anchor()
    focus_clause = (
        "重点输出近期身心能量节律、适配的画面元素与节奏、以及治愈暗示语方向。"
        if scenario_name == "healing_sleep_video"
        else "重点输出桃花趋势、正缘气质、相遇场景线索。"
    )
    tool_query = (
        f"请按结构化JSON返回{topic}命理结果。"
        f"姓名：{name}；出生日期：{birthdate}；出生时间：{birthtime}；性别：{gender_cn}；"
        f"情感对象偏好：{partner_pref_cn}；用户媒体需求：{str(query or '').strip()}。"
        f"当前时间锚点：{anchor.get('today_cn')}（{anchor.get('weekday_cn')}，{anchor.get('tz_name')}，{anchor.get('utc_offset')}）。"
        f"{focus_clause}"
    )
    raw = ""
    try:
        raw = bazi_cesuan.invoke(tool_query)
    except Exception:
        try:
            raw = bazi_cesuan.run(tool_query)
        except Exception:
            raw = {}
    payload = _normalize_structured_fortune_payload(raw, topic)
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("code") or "").strip():
        return _default_media_destiny_hint(profile, scenario_name)
    parts: list[str] = []
    signal_topic = "love" if topic == "love" else "daily"
    signal = _signal_for_topic(payload, signal_topic)
    if signal:
        parts.append(signal)
    basis = _basis_line(payload)
    if basis:
        parts.append(f"命盘依据：{basis}")
    if scenario_name == "healing_sleep_video":
        strength = str(payload.get("strength") or "balanced")
        strength_text = {
            "strong": "能量较强，适合明亮通透且有呼吸感的治愈节奏",
            "weak": "能量偏弱，适合更慢、更柔和、包裹感更强的治愈节奏",
            "balanced": "能量平衡，适合稳定舒缓、自然过渡的治愈节奏",
        }.get(strength, "能量平衡，适合稳定舒缓、自然过渡的治愈节奏")
        parts.append(f"节奏建议：{strength_text}")
        advice = _resolve_fortune_advice(payload, "daily", strength)
        if advice:
            parts.append(f"疗愈重点：{str(advice[0]).strip()}")
    for key, prefix in [
        ("opportunity_points", "机会"),
        ("risk_points", "风险"),
        ("time_hints", "时间"),
    ]:
        values = payload.get(key) or []
        if isinstance(values, list):
            first = next((str(v).strip() for v in values if str(v).strip()), "")
            if first:
                parts.append(f"{prefix}：{first}")
    hint = "；".join(parts).strip("； ")
    if not hint:
        return _default_media_destiny_hint(profile, scenario_name)
    return hint[:220]


def _compress_prompt_with_model(prompt: str, max_chars: int = DIFY_PROMPT_MAX_CHARS) -> str:
    raw = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not raw:
        return ""
    if len(raw) <= max_chars:
        return raw
    tpl = ChatPromptTemplate.from_template(
        """你是“提示词压缩器”。
请把下面媒体生成提示词压缩到不超过 {max_chars} 个中文字符，必须保留原意与关键约束：
1) 主体对象与场景
2) 风格/镜头/光影氛围
3) 用户关键偏好（如性别偏好、命理线索）
4) 安全限制（如不露骨、不未成年人、不仿真人）

只输出压缩后的单行提示词，不要解释，不要加引号。
原提示词：{prompt}
"""
    )
    try:
        chain = tpl | get_lc_ali_model_client(temperature=0.1, streaming=False) | StrOutputParser()
        out = str(chain.invoke({"max_chars": int(max_chars), "prompt": raw}) or "").strip()
        out = re.sub(r"\s+", " ", out)
        out = out.strip("`\"' ")
        if not out:
            return raw[:max_chars]
        if len(out) > max_chars:
            out = out[:max_chars]
        return out
    except Exception as e:
        logger.warning(f"提示词压缩失败，使用截断兜底: {e}")
        return raw[:max_chars]


def _intent_extra_payload(intent_decision: dict | None) -> dict[str, str]:
    src = intent_decision if isinstance(intent_decision, dict) else {}
    route = str(src.get("route") or "chat").strip() or "chat"
    confidence = str(src.get("confidence") or "low").strip() or "low"
    reason_code = str(src.get("reason_code") or "none").strip() or "none"
    intent_version = str(src.get("intent_version") or "v3").strip() or "v3"
    decision_source = str(src.get("decision_source") or "rule").strip() or "rule"
    score_create = str(int(max(0, int(src.get("create_score") or 0))))
    score_feedback = str(int(max(0, int(src.get("feedback_score") or 0))))
    return {
        "intent_route": route,
        "intent_confidence": confidence,
        "intent_reason": reason_code,
        "intent_version": intent_version,
        "intent_source": decision_source,
        "intent_score_create": score_create,
        "intent_score_feedback": score_feedback,
    }


def _attach_intent_extra(response_data: dict, intent_decision: dict | None) -> dict:
    if not isinstance(response_data, dict):
        return response_data
    extra = response_data.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    extra.update(_intent_extra_payload(intent_decision))
    response_data["extra"] = extra
    return response_data


def _task_input_query(task: dict | None) -> str:
    data = task if isinstance(task, dict) else {}
    input_json = data.get("input_json")
    if not isinstance(input_json, dict):
        return ""
    return str(input_json.get("query") or "").strip()


def _fallback_media_scenario_by_query(query: str) -> str:
    q = str(query or "")
    if not q:
        return ""
    if re.search(r"(视频|短片|剧情|片段|mv|动画|片子|成片|动图)", q):
        return "general_video"
    if re.search(r"(图|图片|图像|画像|照片|海报|壁纸|插画|写真|头像|一幅画|一张画|画出来|配图|插图|绘图|封面图|卡面|视觉稿)", q):
        return "general_image"
    return ""


def _parse_json_object(raw_text: str) -> dict:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _classify_media_route_with_model(query: str, rule_probe: dict | None, recent_media: dict | None = None) -> dict:
    q = str(query or "").strip()
    if not q:
        return {}
    probe = rule_probe if isinstance(rule_probe, dict) else {}
    recent = recent_media if isinstance(recent_media, dict) else {}
    default_scenario = str(probe.get("scenario") or "").strip() or str(recent.get("scenario") or "").strip()
    prompt = ChatPromptTemplate.from_template(
        """你是“媒体意图路由器”。请判断下面这句是否在发起新的媒体生成任务。
只允许输出一个 JSON 对象，不要解释，不要 markdown：
{{
  "route": "media_create" | "media_feedback" | "media_followup" | "media_clarify" | "chat",
  "confidence": "high" | "medium" | "low",
  "reason_code": "简短英文下划线"
}}

判定原则：
1) 明确命令“帮我生成/再生成/重新生成/来一个”且有图或视频对象 => media_create
2) “再来一版/同风格再来/按上一个来”且有最近作品上下文 => media_followup
3) 跟做图相关但缺上下文 => media_clarify
4) 对“已生成内容”的评价、回顾、感叹 => media_feedback
5) 若用户明确说“不是要生成/先别生成/只是聊聊”，必须 route=chat
6) 不确定时优先 chat，不要误触发生成

用户输入：{query}
规则探测：{probe}
最近媒体上下文：{recent}
"""
    )
    try:
        chain = prompt | get_lc_ali_model_client(temperature=0.1, streaming=False) | StrOutputParser()
        raw = str(chain.invoke({"query": q, "probe": json.dumps(probe, ensure_ascii=False), "recent": json.dumps(recent, ensure_ascii=False)}) or "").strip()
        parsed = _parse_json_object(raw)
        route = str(parsed.get("route") or "").strip().lower()
        confidence = str(parsed.get("confidence") or "").strip().lower()
        reason_code = str(parsed.get("reason_code") or "").strip() or "llm_classifier"
        if route not in {"media_create", "media_feedback", "media_followup", "media_clarify", "chat"}:
            return {}
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        if bool(probe.get("negation_guard_hit")) and route == "media_create":
            route = "chat"
            reason_code = "llm_guarded_by_negation"
        blocked, blocked_reason = check_media_safety(q)
        scenario = default_scenario if route in {"media_create", "media_followup"} else ""
        if route in {"media_create", "media_followup"} and not scenario:
            scenario = _fallback_media_scenario_by_query(q)
        return {
            "route": route,
            "scenario": scenario,
            "scenario_label": MEDIA_SCENARIO_LABELS.get(scenario, scenario),
            "confidence": confidence,
            "reason_code": reason_code,
            "blocked": bool(blocked and route in {"media_create", "media_followup"}),
            "blocked_reason": str(blocked_reason or "") if bool(blocked and route in {"media_create", "media_followup"}) else "",
            "media_like": bool(probe.get("media_like") or False),
            "needs_llm": False,
            "intent_version": "v3",
            "decision_source": "llm",
            "create_score": int(max(0, int(probe.get("create_score") or 0))),
            "feedback_score": int(max(0, int(probe.get("feedback_score") or 0))),
            "negation_guard_hit": bool(probe.get("negation_guard_hit") or False),
            "conflict": False,
        }
    except Exception as e:
        logger.warning(f"媒体二阶判别失败，降级chat: {e}")
        return {}


def _resolve_media_intent(query: str, session_id: str) -> tuple[dict, dict]:
    recent_media = _get_last_media_context(session_id) or {}
    if not MEDIA_INTENT_ROUTER_V2:
        legacy = detect_media_intent(
            query,
            recent_media=recent_media,
            router_version="v2",
            negation_guard_enabled=False,
        )
        decision = {
            "route": "media_create" if bool(legacy.get("hit")) else "chat",
            "scenario": str(legacy.get("scenario") or ""),
            "scenario_label": str(legacy.get("scenario_label") or ""),
            "confidence": "high" if bool(legacy.get("hit")) else "low",
            "reason_code": "router_v2_disabled",
            "blocked": bool(legacy.get("blocked") or False),
            "blocked_reason": str(legacy.get("blocked_reason") or ""),
            "media_like": bool(legacy.get("hit") or False),
            "needs_llm": False,
            "intent_version": "v2",
            "decision_source": "rule",
            "create_score": int(max(0, int(legacy.get("create_score") or 0))),
            "feedback_score": int(max(0, int(legacy.get("feedback_score") or 0))),
            "negation_guard_hit": bool(legacy.get("negation_guard_hit") or False),
            "conflict": bool(legacy.get("conflict") or False),
        }
        _metric_incr(f"media_intent_route_total__{decision['route']}")
        return legacy, decision

    router_version = "v3" if MEDIA_INTENT_ROUTER_V3 else "v2"
    decision = route_media_intent(
        query,
        recent_media=recent_media,
        router_version=router_version,
        negation_guard_enabled=bool(MEDIA_INTENT_NEGATION_GUARD),
    )
    route_name = str(decision.get("route") or "chat")
    _metric_incr(f"media_intent_route_total__{route_name}")
    if bool(decision.get("negation_guard_hit") or False):
        _metric_incr("media_intent_negation_guard_total")
    if route_name == "media_followup":
        _metric_incr("media_intent_followup_total")
    if bool(decision.get("conflict") or False):
        _metric_incr("media_intent_conflict_total")

    needs_fallback = bool(
        MEDIA_INTENT_LLM_FALLBACK
        and str(decision.get("route") or "") == "chat"
        and bool(decision.get("media_like") or False)
        and (
            bool(decision.get("needs_llm"))
            or str(decision.get("reason_code") or "") == "intent_conflict"
        )
    )
    if needs_fallback:
        _metric_incr("media_intent_llm_fallback_total")
        llm_decision = _classify_media_route_with_model(query, decision, recent_media=recent_media)
        if llm_decision:
            decision = llm_decision
            route_name = str(decision.get("route") or "chat")
            _metric_incr(f"media_intent_route_total__{route_name}")
        else:
            _metric_incr("media_intent_force_chat_total")
            decision = {
                "route": "chat",
                "scenario": "",
                "scenario_label": "",
                "confidence": "low",
                "reason_code": "llm_fallback_failed_chat",
                "blocked": False,
                "blocked_reason": "",
                "media_like": bool(decision.get("media_like") or False),
                "needs_llm": False,
                "intent_version": str(decision.get("intent_version") or router_version),
                "decision_source": "rule",
                "create_score": int(max(0, int(decision.get("create_score") or 0))),
                "feedback_score": int(max(0, int(decision.get("feedback_score") or 0))),
                "negation_guard_hit": bool(decision.get("negation_guard_hit") or False),
                "conflict": bool(decision.get("conflict") or False),
            }

    route_name = str(decision.get("route") or "chat")
    hit = route_name in {"media_create", "media_followup"}
    scenario = str(decision.get("scenario") or "")
    if hit and not scenario:
        scenario = _fallback_media_scenario_by_query(query)
        decision["scenario"] = scenario
        decision["scenario_label"] = MEDIA_SCENARIO_LABELS.get(scenario, scenario)
    legacy = {
        "hit": hit and bool(scenario),
        "scenario": scenario if hit else "",
        "scenario_label": MEDIA_SCENARIO_LABELS.get(scenario, scenario) if hit else "",
        "blocked": bool(decision.get("blocked") or False),
        "blocked_reason": str(decision.get("blocked_reason") or ""),
        "route": route_name,
        "confidence": str(decision.get("confidence") or "low"),
        "reason_code": str(decision.get("reason_code") or "none"),
        "needs_llm": bool(decision.get("needs_llm") or False),
        "media_like": bool(decision.get("media_like") or False),
        "intent_version": str(decision.get("intent_version") or router_version),
        "decision_source": str(decision.get("decision_source") or "rule"),
        "create_score": int(max(0, int(decision.get("create_score") or 0))),
        "feedback_score": int(max(0, int(decision.get("feedback_score") or 0))),
        "negation_guard_hit": bool(decision.get("negation_guard_hit") or False),
        "conflict": bool(decision.get("conflict") or False),
    }
    return legacy, decision


def _create_and_submit_media_task(
    *,
    user_id: int,
    session_id: str,
    query: str,
    scenario: str,
    profile: dict[str, str],
    user_identity: str,
) -> tuple[dict | None, str]:
    if not _media_ready():
        return None, "MEDIA_DISABLED"
    prompt_bundle = build_media_prompt(
        scenario=scenario,
        query=query,
        profile=profile,
        destiny_hint="",
    )
    task = create_media_task(
        _db_conn,
        user_id=user_id,
        session_id=session_id,
        scenario=scenario,
        query=query,
        prompt_bundle=prompt_bundle,
    )
    task_id = str(task.get("task_id") or "")
    if not task_id:
        return None, "TASK_CREATE_FAILED"
    _set_last_media_context(
        session_id,
        task_id=task_id,
        scenario=str(scenario or ""),
        query=str(query or ""),
        status=str(task.get("status") or "pending"),
    )
    try:
        destiny_hint = _build_media_destiny_hint(
            str(query or ""),
            profile if isinstance(profile, dict) else {},
            str(scenario or ""),
        )
        prompt_bundle = build_media_prompt(
            scenario=str(scenario or ""),
            query=str(query or ""),
            profile=profile if isinstance(profile, dict) else {},
            destiny_hint=destiny_hint,
        )
        prompt_raw = str((prompt_bundle or {}).get("prompt") or "")
        if prompt_raw:
            prompt_bundle["prompt"] = _compress_prompt_with_model(prompt_raw, max_chars=DIFY_PROMPT_MAX_CHARS)
        task = submit_media_task(
            _db_conn,
            _DIFY_MEDIA_CLIENT,
            task_id=str(task_id or ""),
            scenario=str(scenario or ""),
            prompt_bundle=prompt_bundle if isinstance(prompt_bundle, dict) else {},
            user_identity=str(user_identity or ""),
        )
    except Exception as e:
        logger.error(f"提交媒体任务失败 task_id={task_id}: {e}\n{traceback.format_exc()}")
        _mark_media_task_failed(task_id, "MEDIA_SUBMIT_FAILED", "媒体任务提交失败，请稍后重试")
        task = get_media_task(_db_conn, task_id, user_id=int(user_id or 0))
        return task, "MEDIA_SUBMIT_FAILED"
    return task, str((task or {}).get("error_code") or "")


def _submit_media_task_background(
    *,
    task_id: str,
    query: str,
    scenario: str,
    profile: dict,
    user_identity: str,
) -> None:
    try:
        destiny_hint = _build_media_destiny_hint(str(query or ""), profile if isinstance(profile, dict) else {}, str(scenario or ""))
        prompt_bundle = build_media_prompt(
            scenario=str(scenario or ""),
            query=str(query or ""),
            profile=profile if isinstance(profile, dict) else {},
            destiny_hint=destiny_hint,
        )
        prompt_raw = str((prompt_bundle or {}).get("prompt") or "")
        if prompt_raw:
            prompt_bundle["prompt"] = _compress_prompt_with_model(prompt_raw, max_chars=DIFY_PROMPT_MAX_CHARS)
        submit_media_task(
            _db_conn,
            _DIFY_MEDIA_CLIENT,
            task_id=str(task_id or ""),
            scenario=str(scenario or ""),
            prompt_bundle=prompt_bundle if isinstance(prompt_bundle, dict) else {},
            user_identity=str(user_identity or ""),
        )
    except Exception as e:
        logger.error(f"后台提交媒体任务失败 task_id={task_id}: {e}\n{traceback.format_exc()}")
        _mark_media_task_failed(str(task_id or ""), "MEDIA_SUBMIT_FAILED", "媒体任务提交失败，请稍后重试")


def _mark_media_task_failed(task_id: str, error_code: str, error_message: str) -> None:
    tid = str(task_id or "").strip()
    if not tid:
        return
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE media_tasks
                    SET status = 'failed',
                        error_code = %s,
                        error_message = %s,
                        finished_at = NOW(),
                        updated_at = NOW()
                    WHERE task_id = %s AND status IN ('pending', 'running')
                    """,
                    (str(error_code or "MEDIA_SUBMIT_FAILED"), str(error_message or "媒体任务提交失败"), tid),
                )
    except Exception as e:
        logger.error(f"写入媒体失败状态异常 task_id={tid}: {e}")


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
DREAM_QUERY_PATTERN = re.compile(r"(解梦|梦见|做梦|周公)")
FORTUNE_SCENE_PATTERN = re.compile(
    r"(今天|本周|这周|本月|最近|这段时间|现在|未来|今年|本年|明年|后年|去年|前年|上半年|下半年)"
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
        """你是“吉伊大师”，请结合星座信息回答用户关于{year}年{sign}{topic_cn}的问题。
要求：
1) 使用吉伊口吻，温柔自然，可少量加入“呀哈/呜啦/本鼠鼠”。
2) 先给清晰结论，再解释触发点与风险窗口，最后给1-3条可执行建议。
3) 不要使用八字/五行/日主/喜用神等术语。
4) 不要固定骨架标题，避免模板化语句。
5) 结尾补一句“参考强度：中/中高/高（星座解读偏趋势参考）”之一。

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
        return _ensure_jiyi_tone(text), {"topic": topic, "source": "zodiac_llm"}
    except Exception:
        return _zodiac_default_reply(sign, year, topic), {"topic": topic, "source": "zodiac_fallback"}


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
    if _normalize_gender(str(profile.get("gender") or "unknown")) == "unknown":
        missing.append("gender")
    return missing


def build_fortune_missing_reply(missing: list[str]) -> str:
    if missing == ["name"]:
        return "呀哈～我先补一个关键资料：请告诉我你的姓名（2-12个字）。"
    if missing == ["birthdate"]:
        return "呀哈～我还需要你的出生年月日（例如 2001-08-15），这样命理判断才更准。"
    return "呀哈～我先帮你把资料补齐：请告诉我姓名和出生年月日（例如 2001-08-15；知道时辰也可以一起说）。"


def build_profile_gate_reply(missing: list[str]) -> str:
    miss = set(missing or [])
    if miss == {"name"}:
        return "呀哈～先让本鼠鼠认识你一下：请先告诉我你的姓名（2-12个字）喔。"
    if miss == {"birthdate"}:
        return "呀哈～还差一个小资料：请告诉我你的出生年月日（例如 2001-08-15）喔。"
    if miss == {"gender"}:
        return "呀哈～还差一个小资料：请告诉我你的性别（男/女）喔。"
    if miss == {"name", "birthdate"}:
        return "呀哈～开始前先给我姓名和出生年月日（例如 2001-08-15）吧，资料齐了我就认真帮你看～"
    if miss == {"name", "gender"}:
        return "呀哈～还差两项资料：请告诉我姓名（2-12个字）和性别（男/女）喔。"
    if miss == {"birthdate", "gender"}:
        return "呀哈～还差两项资料：请告诉我出生年月日（例如 2001-08-15）和性别（男/女）喔。"
    return "呀哈～开始前先给我姓名、出生年月日（例如 2001-08-15）和性别（男/女）吧，资料齐了我就认真帮你看～"


def build_profile_ready_transition(profile: dict[str, str] | None = None) -> str:
    p = profile or {}
    call_name = str(p.get("preferred_name") or p.get("name") or "").strip()
    head = f"呀哈～{call_name}，资料我都收好啦。" if call_name else "呀哈～资料我都收好啦。"
    return (
        f"{head}\n"
        "姓名、生日和性别已经对齐，本鼠鼠先把小星盘和节气线索摆正一下～\n"
        "你下一句直接告诉我想看的方向就行：比如“看事业”“看感情”“看今天运势”或“生成正缘画像”。"
    )


def build_media_missing_reply(missing: list[str]) -> str:
    miss = set(missing or [])
    if miss == {"name"}:
        return "呀哈～生成正缘画像/视频前，我还需要你的姓名（2-12个字）来对齐命理信息。"
    if miss == {"birthdate"}:
        return "呀哈～生成前还差一个关键资料：你的出生年月日（例如 2001-08-15）。"
    if miss == {"gender"}:
        return "呀哈～生成前还差一个关键资料：你的性别（男/女）。"
    if miss == {"name", "birthdate"}:
        return "呀哈～想生成正缘画像/视频的话，请先告诉我姓名和出生年月日（例如 2001-08-15）喔。"
    if miss == {"name", "gender"}:
        return "呀哈～想生成正缘画像/视频的话，请先告诉我姓名（2-12个字）和性别（男/女）喔。"
    if miss == {"birthdate", "gender"}:
        return "呀哈～想生成正缘画像/视频的话，请先告诉我出生年月日（例如 2001-08-15）和性别（男/女）喔。"
    return "呀哈～想生成正缘画像/视频的话，请先告诉我姓名、出生年月日（例如 2001-08-15）和性别（男/女）喔。"


def _default_fortune_advice(topic: str, strength: str) -> list[str]:
    table = {
        "daily": [
            "今天先完成一件最重要的小事，连续投入25分钟。",
            "把待办压到3项以内，先完成再扩展。",
            "晚上用3分钟复盘今天最顺和最卡的点。",
            "先把最容易拖延的任务切成两步，完成第一步就收获正反馈。",
            "对外沟通前先写三句结论，减少来回解释的成本。",
            "遇到分心时先离开屏幕2分钟，再回到当前最关键任务。",
            "今天只保留一个主目标，其他事项全部降级到备选。",
            "把睡前15分钟留给整理与复盘，帮助明天开局更稳。",
        ],
        "love": [
            "今天主动发一次轻量关心，不求长聊但求真诚。",
            "表达感受时用'我感受'句式，减少猜测。",
            "关系不确定时，48小时内不做冲动决定。",
            "把期待说成具体需求，别让对方靠猜来理解你。",
            "出现分歧先复述对方观点，再表达你的底线和诉求。",
            "高情绪时先暂停20分钟，等状态稳下来再回应。",
            "先稳定互动频率，再讨论关系定义，避免节奏失衡。",
            "先做一次真诚感谢，修复关系比争输赢更重要。",
        ],
        "wealth": [
            "今天先做一项与收入直接相关的动作。",
            "先记账再消费，避免情绪性花销。",
            "高风险决策设置24小时冷静期。",
            "把本周可变支出先封顶，再安排非必要消费。",
            "先清掉一项低价值开销，为现金流腾出缓冲区。",
            "收入增长目标拆成日动作，先保连续性再追高强度。",
            "遇到高收益承诺先核对风险条款，避免信息不对称。",
            "优先补齐应急金，再考虑激进配置。",
        ],
        "career": [
            "先推进一个可量化产出点，别同时开太多线。",
            "把本周关键结果整理成3句汇报。",
            "遇到卡点先找一个能给反馈的人快速对齐。",
            "先把任务标准写清楚，再动手可显著减少返工。",
            "优先解决高杠杆问题，别被低价值杂事拖住节奏。",
            "重要会议前先准备一页提纲，保证表达稳定有重点。",
            "今天先完成一个可交付版本，再逐步打磨细节。",
            "碰到争议先给备选方案，再表达偏好和理由。",
        ],
        "study": [
            "先做25分钟深度学习，再休息5分钟。",
            "先攻克最难的一节，建立正反馈。",
            "睡前做10分钟回顾，巩固关键知识点。",
            "先做一组限时练习，再复盘错因比盲目刷题更有效。",
            "把知识点讲给自己听一遍，检验理解是否完整。",
            "把高频错题单独成册，每天固定回看一次。",
            "先定今日最小达标线，完成后再加练。",
            "把复习节奏拆成“输入-输出-纠错”三步，避免只看不练。",
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


def _fortune_provider_failure(payload: dict | None) -> dict:
    error = (payload or {}).get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def _build_fortune_provider_safe_fallback(
    payload: dict,
    topic: str,
    query: str,
    question_type: str,
    time_anchor: dict,
    window_meta: dict | None,
    session_id: str = "",
) -> str:
    topic_cn = _topic_cn(topic)
    error = _fortune_provider_failure(payload)
    provider_code = str(error.get("provider_code") or error.get("code") or "FORTUNE_PROVIDER_DEGRADED")
    signal_line = str(error.get("message") or "命理服务暂时不可用，请先按保守策略处理。")
    window_line = ""
    if isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
        if window_text and _should_show_window_text(query, window_label, question_type=question_type):
            window_line = f"时间窗口：{window_text}。"
    advice = _resolve_fortune_advice(
        payload,
        topic,
        "balanced",
        query=str(query or ""),
        question_type=str(question_type or "default"),
        blueprint_id="provider_safe_fallback",
        session_id=session_id,
    )[:3] or _default_fortune_advice(topic, "balanced")[:3]
    basis = "上游命理盘面暂时不可用，本次按用户问题、时间范围与保守策略给出不依赖外部盘面的建议"
    return _render_fortune_with_blueprint(
        blueprint_id="provider_safe_fallback",
        conclusion_line=f"这次{topic_cn}先按保守策略解读（{provider_code}）",
        window_line=window_line,
        signal_line=signal_line,
        basis_line=basis,
        advice=advice,
    )

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


def _dedupe_text_items(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = str(item or "").strip()
        if not clean:
            continue
        norm = re.sub(r"\s+", "", clean)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        deduped.append(clean)
    return deduped


def _pick_advice_by_seed(candidates: list[str], seed: str, max_items: int = 3) -> list[str]:
    if not candidates:
        return []
    cap = max(1, min(int(max_items), len(candidates)))
    hashed = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    start = int(hashed[:8], 16) % len(candidates)
    step = (int(hashed[8:16], 16) % len(candidates)) or 1
    if len(candidates) > 1 and step % len(candidates) == 0:
        step = 1
    picked: list[str] = []
    idx = start
    guard = 0
    while len(picked) < cap and guard < len(candidates) * 2:
        choice = candidates[idx % len(candidates)]
        if choice not in picked:
            picked.append(choice)
        idx += step
        guard += 1
    return picked[:cap]


def _load_recent_advice_signatures(session_id: str) -> set[str]:
    signatures: set[str] = set()
    try:
        if session_id:
            s_key = _recent_advice_signature_session_key(session_id)
            session_items = _REDIS_CLIENT.lrange(s_key, 0, 7) or []
            signatures.update(str(x or "").strip() for x in session_items if str(x or "").strip())
        g_key = _quality_recent_advice_signature_key()
        global_items = _REDIS_CLIENT.lrange(g_key, 0, 31) or []
        signatures.update(str(x or "").strip() for x in global_items if str(x or "").strip())
    except Exception:
        return signatures
    return signatures


def _remember_advice_signature(signature: str, session_id: str):
    sign = str(signature or "").strip()
    if not sign:
        return
    try:
        g_key = _quality_recent_advice_signature_key()
        _REDIS_CLIENT.lpush(g_key, sign)
        _REDIS_CLIENT.ltrim(g_key, 0, 63)
        _REDIS_CLIENT.expire(g_key, QUALITY_METRICS_TTL_DAYS * 24 * 3600)
        if session_id:
            s_key = _recent_advice_signature_session_key(session_id)
            _REDIS_CLIENT.lpush(s_key, sign)
            _REDIS_CLIENT.ltrim(s_key, 0, 15)
            _REDIS_CLIENT.expire(s_key, SESSION_TTL_SECONDS)
    except Exception:
        return


def _resolve_fortune_advice(
    payload: dict,
    topic: str,
    strength: str,
    query: str = "",
    question_type: str = "default",
    blueprint_id: str = "",
    session_id: str = "",
) -> list[str]:
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
        evidence_pool = _dedupe_text_items(candidates)
    else:
        evidence_pool = []
    base_advice = payload.get("advice") or []
    base_pool = _dedupe_text_items([str(x).strip() for x in base_advice if str(x).strip()])
    default_pool = _dedupe_text_items(_default_fortune_advice(topic, strength))
    pool: list[str] = []
    for source in [evidence_pool, base_pool, default_pool]:
        for item in source:
            if item not in pool:
                pool.append(item)
    if not pool:
        return []
    seed_src = (
        f"{topic}|{strength}|{str(question_type or 'default')}|{str(blueprint_id or 'none')}|"
        f"{hashlib.sha256(str(query or '').encode('utf-8')).hexdigest()[:16]}"
    )
    max_items = min(3, len(pool))
    selected = _pick_advice_by_seed(pool, seed_src, max_items=max_items)
    recent_signatures = _load_recent_advice_signatures(session_id)
    if selected and recent_signatures and len(pool) > max_items:
        for offset in range(1, min(6, len(pool))):
            alt = _pick_advice_by_seed(pool, f"{seed_src}|alt{offset}", max_items=max_items)
            if not alt:
                continue
            alt_sign = _advice_signature(alt)
            if alt_sign and alt_sign not in recent_signatures:
                selected = alt
                break
    _remember_advice_signature(_advice_signature(selected), session_id=session_id)
    return selected


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
    blueprint_id: str = "default_concise",
    advice_override: list[str] | None = None,
) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    topic_cn = _topic_cn(topic)
    strength = str(payload.get("strength") or "balanced")
    advice = [str(x).strip() for x in (advice_override or []) if str(x).strip()]
    if not advice:
        advice = _resolve_fortune_advice(
            payload,
            topic,
            strength,
            query=q,
            question_type=str(question_type or "default"),
            blueprint_id=str(blueprint_id or ""),
            session_id="",
        )
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
9) 如果题目不是趋势/时窗问题，不要输出具体日期（如“2026年3月5日”“3月5日”）。
10) 若给定“时间窗口”，只能复述该窗口，不得自行新增窗口外日期。

输入信息：
- 用户问题：{query}
- 问题类型：{question_type}
- 主题：{topic_cn}
- 蓝图风格：{blueprint_style}
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
                    "blueprint_style": str(blueprint_id or "default_concise"),
                    "window_label": window_label or "none",
                    "window_text": window_text or "无",
                    "signal_line": _signal_for_topic(payload, topic) or "无",
                    "basis_line": _basis_line(payload),
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
        out = f"时间上先对齐：{window_text}。\n{out}"
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
        ordered = []
        for k in ["梦境", "解梦", "吉凶", "建议", "result", "content"]:
            val = str(raw.get(k) or "").strip()
            if val:
                ordered.append(f"{k}：{val}")
        if not ordered:
            ordered = [f"{k}：{v}" for k, v in raw.items() if str(v).strip()]
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
    return "\n".join(lines)


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

    if isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
        if window_text and _should_show_window_text(query, window_label, question_type=question_type):
            lines.append(f"时间窗口：{window_text}。")

    signal_line = _signal_for_topic(payload, topic)
    if signal_line:
        lines.append(f"命理信号：{signal_line}")

    lines.append(f"依据：{_basis_line(payload)}。")
    advice = _resolve_fortune_advice(payload, topic, strength)
    payload["advice_signature"] = _advice_signature(advice)
    if advice:
        lines.append("建议：")
        for idx, tip in enumerate(advice, start=1):
            lines.append(f"{idx}. {tip}")
    payload["blueprint_id"] = "legacy_v2"
    payload["_render_blueprint_id"] = "legacy_v2"
    return "\n".join(lines)


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
    session_id: str = "",
) -> str:
    strength = str(payload.get("strength") or "balanced")
    query_hash = hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]
    blueprint_id = _select_fortune_blueprint(question_type, session_id, query_hash)
    advice_for_sign = _resolve_fortune_advice(
        payload,
        topic,
        strength,
        query=str(query or ""),
        question_type=str(question_type or "default"),
        blueprint_id=blueprint_id,
        session_id=session_id,
    )
    payload["advice_signature"] = _advice_signature(advice_for_sign)
    payload["blueprint_id"] = blueprint_id
    payload["_render_blueprint_id"] = blueprint_id
    raw_error = payload.get("error")
    has_error = isinstance(raw_error, dict) and str(raw_error.get("code") or "")
    if not has_error:
        model_reply = _generate_fortune_reply_with_model(
            payload=payload,
            topic=topic,
            query=query,
            question_type=question_type,
            window_meta=window_meta,
            blueprint_id=blueprint_id,
            advice_override=advice_for_sign,
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
        if str(error.get("provider") or "") == "yuanfenju":
            return _build_fortune_provider_safe_fallback(
                payload,
                topic,
                query=query,
                question_type=question_type,
                time_anchor=build_time_anchor(),
                window_meta=window_meta,
                session_id=session_id,
            )
        code = str(error.get("code") or "")
        msg = str(error.get("message") or "命理链路暂时不可用")
        fallback_advice = _resolve_fortune_advice(
            payload,
            topic,
            "balanced",
            query=str(query or ""),
            question_type=str(question_type or "default"),
            blueprint_id=blueprint_id,
            session_id=session_id,
        )[:2] or _default_fortune_advice(topic, "balanced")[:2]
        payload["advice_signature"] = _advice_signature(fallback_advice)
        return _render_fortune_with_blueprint(
            blueprint_id=blueprint_id,
            conclusion_line=f"这次{topic_cn}盘面暂时没取全（{code}）",
            window_line="",
            signal_line=msg,
            basis_line="先按保守策略处理",
            advice=fallback_advice,
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
    if isinstance(window_meta, dict):
        window_text = str(window_meta.get("window_text") or "").strip()
        window_label = str(window_meta.get("label") or "").strip()
        if window_text and _should_show_window_text(query, window_label, question_type=question_type):
            window_line = f"时间窗口：{window_text}。"
    signal_line = _signal_for_topic(payload, topic)
    basis = _basis_line(payload)
    advice = advice_for_sign
    payload["advice_signature"] = _advice_signature(advice)
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
        if str(error.get("provider") or "") == "yuanfenju":
            return _build_fortune_provider_safe_fallback(
                payload,
                topic,
                query="",
                question_type=str(payload.get("question_type") or "default"),
                time_anchor=build_time_anchor(),
                window_meta={
                    "window_text": str(payload.get("window_text") or ""),
                    "label": str(payload.get("window_label") or ""),
                },
            )
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
    advice = _resolve_fortune_advice(payload, topic, str(payload.get("strength") or "balanced"))
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
    active_flags = flags or dict(V2_FLAG_DEFAULTS)
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
    name = str(profile.get("name") or "").strip()
    birthdate = str(profile.get("birthdate") or "").strip()
    birthtime = str(profile.get("birthtime") or "").strip()
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
        if window_label in {"near_days", "two_days", "this_week", "next_week"}:
            time_window_clause = f"若用户问“近几天/哪几天”，仅允许在此窗口判断：{window_text}。"
        else:
            time_window_clause = f"时间范围：{window_text}。回答不要收缩成“近三天”，要覆盖该范围。"
    tool_query = (
        f"请按结构化JSON返回{topic}命理结果。"
        f"姓名：{name}；出生日期：{birthdate}；出生时间：{birthtime or '未知'}；用户问题：{q}。"
        f"当前时间锚点：{anchor.get('today_cn')}（{anchor.get('weekday_cn')}，{anchor.get('tz_name')}，{anchor.get('utc_offset')}）。"
        f"{time_window_clause}"
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
    payload["route_reason_code"] = route_reason_code
    if isinstance(window_meta, dict):
        payload["window_start"] = str(window_meta.get("window_start") or "")
        payload["window_end"] = str(window_meta.get("window_end") or "")
        payload["window_text"] = str(window_meta.get("window_text") or "")
        payload["window_label"] = str(window_meta.get("label") or "")
    logger.info(
        "fortune_pipeline session_payload: "
        f"topic={payload.get('topic')} error={((payload.get('error') or {}).get('code') if isinstance(payload.get('error'), dict) else '')} "
        f"confidence={payload.get('confidence')} question_type={question_type} route_reason={route_reason_code}"
    )
    if active_flags.get("render_v2"):
        return (
            render_user_fortune_reply_v2(
                payload,
                topic,
                query=q,
                question_type=question_type,
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


def strip_profile_echo(text: str, profile: dict | None = None, user_query: str = "") -> str:
    out = str(text or "").strip()
    if not out:
        return out
    p = profile or {}
    name = str(p.get("name") or "").strip()
    preferred_name = str(p.get("preferred_name") or "").strip()
    gender = _normalize_gender(str(p.get("gender") or "unknown"))
    birthdate = str(p.get("birthdate") or "").strip()
    birthtime = str(p.get("birthtime") or "").strip()

    # 姓名处理：若用户有称呼偏好，优先替换成偏好；若在“我叫什么”场景，允许显示姓名/昵称；其余场景维持匿名“你”。
    replace_name = "你"
    if _is_valid_call_name(preferred_name):
        replace_name = preferred_name
    else:
        replace_name = _pick_address_name(p, user_query=user_query)
    if name and len(name) >= 2:
        # 先处理“姓名+敬称”，避免出现“你小姐/你先生”这类生硬替换。
        # 若当前采用匿名称呼“你”，句首敬称直接移除（如“刘芷华小姐，欢迎...” -> “欢迎...”）。
        honorific_pattern = rf"{re.escape(name)}\s*(?:小姐|先生|同学|老师|女士|男士)\s*[，,、]?"
        honorific_repl = "" if replace_name == "你" else f"{replace_name}，"
        out = re.sub(honorific_pattern, honorific_repl, out)
        out = out.replace(name, replace_name)
        out = re.sub(r"^[，,、\s]+", "", out)
    if gender == "unknown":
        # 未提供性别时，统一中性称呼，避免模型猜测“小姐/先生”等。
        out = re.sub(r"(小姐姐|小哥哥|小姐|先生|女士|男士)", "同学", out)
        out = re.sub(r"(同学){2,}", "同学", out)
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
    # 保留吉伊口头禅（呀哈/呜啦/噗噜等），仅清理其它异常“XX～”前缀。
    out = re.sub(
        r"^\s*(?!(?:呀哈|呜啦|噗噜|哼|呀～哈)[～~])[\u4e00-\u9fa5]{2,4}[～~][，,:：]?\s*",
        "",
        out,
        flags=re.MULTILINE,
    )
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
        out = strip_profile_echo(out.strip(), profile=profile, user_query=user_query)
        out = re.sub(r"(19|20)\d{2}年\d{1,2}月\d{1,2}日", "你的生日", out)
        out = re.sub(r"(清晨|凌晨|早上|上午|中午|下午|晚上)\s*\d{1,2}[:：点]\d{0,2}", "你的出生时段", out)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(lines) > 2:
            out = "\n".join(lines[:2])
        return out.strip()

    emotion_level = detect_emotion_level(user_query)
    out = diversify_fortune_opening(out.strip(), user_query=user_query)
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
    user, phone, user_uuid = _resolve_user_from_auth(auth)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user_phone": phone,
            "user_short_account": _build_user_short_account(user, phone=phone, user_uuid=user_uuid),
            "user_uuid": user_uuid,
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

    user, phone, user_uuid = _resolve_user_from_auth(auth)
    user_id = int(user["id"]) if user.get("id") else 0
    profile = _get_profile_by_user_id(user_id) if user_id else {
        "name": "",
        "birthdate": "",
        "preferred_name": "",
        "gender": "unknown",
        "partner_gender_preference": "unknown",
    }
    return {
        "ok": True,
        "user": {
            "phone": phone,
            "user_id": user_uuid,
            "short_account": _build_user_short_account(user, phone=phone, user_uuid=user_uuid),
        },
        "profile": {
            "name": str((profile or {}).get("name", "")),
            "preferred_name": str((profile or {}).get("preferred_name", "")),
            "birthdate": str((profile or {}).get("birthdate", "")),
            "gender": _normalize_gender(str((profile or {}).get("gender") or "unknown")),
            "partner_gender_preference": _normalize_partner_gender_preference(
                str((profile or {}).get("partner_gender_preference") or "unknown")
            ),
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
        "gender": "unknown",
        "partner_gender_preference": "unknown",
        "name_confidence": "none",
        "preferred_name_confidence": "none",
    }
    session_id = ""
    user_id = 0
    time_anchor = build_time_anchor()
    flag_snapshot = dict(V2_FLAG_DEFAULTS)
    flag_reason_code = "none"
    domain_intent = "general"
    question_type = "default"
    window_meta: dict | None = None
    intent_decision: dict = {"route": "chat", "confidence": "low", "reason_code": "init"}

    def _with_intent(payload_obj: dict) -> dict:
        return _attach_intent_extra(payload_obj, intent_decision)

    def _intent_meta(extra_meta: dict | None = None) -> dict:
        merged = dict(extra_meta or {})
        merged.update(_intent_extra_payload(intent_decision))
        return merged

    try:
        query = payload.query
        if not query:
            response_data["output"] = "呀哈～先告诉吉伊大师你想问什么吧。"
            return _with_intent(response_data)
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
        missing_profile_before = _missing_profile_fields_for_fortune(profile)
        pending_preferred_name_prompt = _is_preferred_name_prompt_pending(session_id)
        extracted = extract_profile_from_query(query)
        if pending_preferred_name_prompt and not str(extracted.get("preferred_name") or "").strip():
            if _is_soft_preferred_name_reply(query):
                pending_name, pending_conf = _extract_preferred_name_with_confidence(query, allow_soft=True)
                if pending_name:
                    extracted["preferred_name"] = pending_name
                    extracted["preferred_name_confidence"] = pending_conf
            else:
                # 用户未按昵称回复时，立即结束本次追问状态，避免后续普通对话被软提取污染
                _set_preferred_name_prompt_pending(session_id, False)
        profile = merge_session_profile(session_id, extracted)
        preferred_name_set_this_turn = bool(str(extracted.get("preferred_name") or "").strip())
        if preferred_name_set_this_turn:
            _set_preferred_name_prompt_pending(session_id, False)
        should_probe_preferred_name = _is_name_intro_query(query, extracted) and not str(profile.get("preferred_name") or "").strip()
        missing_profile = _missing_profile_fields_for_fortune(profile)
        media_intent_probe, intent_decision = _resolve_media_intent(query, session_id)
        profile_gate_for_fortune = bool(
            (not media_intent_probe.get("hit"))
            and (
                is_bazi_fortune_query(query)
                or is_divination_query(query)
                or is_zodiac_intent_query(query)
            )
        )
        profile_fields_updated = any(
            str(extracted.get(k) or "").strip() for k in ("name", "birthdate", "gender")
        )
        if missing_profile_before and not missing_profile and profile_fields_updated:
            _clear_media_pref_prompt_context(session_id)
            out = build_profile_ready_transition(profile)
            response_data = {
                "session_id": session_id,
                "output": out,
                "message_type": "text",
                "media": [],
                "extra": {
                    "profile_completed": True,
                },
            }
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type="profile",
                route_path="profile_completed",
                extra_meta=_intent_meta({
                    "profile_completed": True,
                }),
            )
            _log_route_observability(
                route_path="profile_completed",
                reason_code="profile_completed_transition",
                flag_snapshot=flag_snapshot,
                domain_intent=domain_intent,
                question_type="profile",
            )
            return _with_intent(response_data)
        # 资料缺失仅拦截命理/运势类对话；普通闲聊、问候、时间类不受影响。
        if missing_profile and profile_gate_for_fortune:
            _clear_media_pref_prompt_context(session_id)
            out = build_profile_gate_reply(missing_profile)
            response_data = {
                "session_id": session_id,
                "output": out,
                "message_type": "text",
                "media": [],
                "extra": {
                    "profile_required": True,
                    "missing_fields": missing_profile,
                },
            }
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type="profile",
                route_path="profile_required",
                extra_meta=_intent_meta({
                    "profile_required": True,
                    "missing_fields": ",".join(missing_profile),
                }),
            )
            _log_route_observability(
                route_path="profile_required",
                reason_code="profile_required",
                flag_snapshot=flag_snapshot,
                domain_intent=domain_intent,
                question_type="profile",
            )
            return _with_intent(response_data)
        resumed_media_ctx = None
        pending_media_pref_ctx = _get_media_pref_prompt_context(session_id)
        if pending_media_pref_ctx:
            pending_pref, pending_pref_conf = _extract_partner_gender_preference_with_confidence(
                query,
                allow_bare_reply=True,
            )
            if pending_pref in {"male", "female", "any"} and pending_pref_conf == "high":
                profile = merge_session_profile(session_id, {"partner_gender_preference": pending_pref})
                resumed_media_ctx = pending_media_pref_ctx
            # 若用户未按预期回复，结束本次追问，避免上下文污染。
            _clear_media_pref_prompt_context(session_id)

        def _postprocess_output(raw_output: str, qtype: str = question_type) -> str:
            out = sanitize_output(raw_output, user_query=query, profile=profile)
            chosen = str(profile.get("preferred_name") or "").strip()
            if preferred_name_set_this_turn and chosen and chosen not in out:
                out = f"好呀～那我就叫你{chosen}。\n\n{out}"
            out = maybe_append_preferred_name_probe(
                out,
                session_id=session_id,
                should_probe=should_probe_preferred_name,
            )
            out = _enforce_min_answer_contract(out, query=query, question_type=qtype)
            return out

        media_query_for_generation = query
        media_intent = media_intent_probe
        if resumed_media_ctx:
            resumed_query = str((resumed_media_ctx or {}).get("query") or "").strip()
            resumed_scenario = str((resumed_media_ctx or {}).get("scenario") or "").strip()
            if resumed_query and resumed_scenario:
                resumed_blocked, resumed_blocked_reason = check_media_safety(resumed_query)
                media_query_for_generation = resumed_query
                media_intent = {
                    "hit": True,
                    "scenario": resumed_scenario,
                    "scenario_label": MEDIA_SCENARIO_LABELS.get(resumed_scenario, resumed_scenario),
                    "blocked": resumed_blocked,
                    "blocked_reason": resumed_blocked_reason,
                }
                intent_decision = {
                    "route": "media_followup",
                    "scenario": resumed_scenario,
                    "scenario_label": MEDIA_SCENARIO_LABELS.get(resumed_scenario, resumed_scenario),
                    "confidence": "high",
                    "reason_code": "resume_after_preference",
                    "blocked": resumed_blocked,
                    "blocked_reason": resumed_blocked_reason,
                }
        if media_intent.get("hit"):
            _metric_incr("media_intent_total")
            media_scenario = str(media_intent.get("scenario") or "")
            if _scenario_requires_profile_for_media(media_scenario):
                missing_profile = _missing_profile_fields_for_fortune(profile)
                if missing_profile:
                    _clear_media_pref_prompt_context(session_id)
                    out = _postprocess_output(build_media_missing_reply(missing_profile), qtype="media")
                    response_data = {
                        "session_id": session_id,
                        "output": out,
                        "message_type": "text",
                        "media": [],
                        "extra": {
                            "media_profile_missing": True,
                            "missing_fields": missing_profile,
                            "media_scenario": media_scenario,
                        },
                    }
                    _append_chat_history(
                        chat_message_history,
                        query,
                        out,
                        user_id=user_id,
                        session_id=session_id,
                        question_type="media",
                        route_path="media_profile_missing",
                        extra_meta=_intent_meta({
                            "media_status": "missing_profile",
                            "media_scenario": media_scenario,
                            "missing_fields": ",".join(missing_profile),
                        }),
                    )
                    _log_route_observability(
                        route_path="media_profile_missing",
                        reason_code="media_profile_missing",
                        flag_snapshot=flag_snapshot,
                        domain_intent="media",
                        question_type="media",
                    )
                    return _with_intent(response_data)
            partner_pref = _normalize_partner_gender_preference(str(profile.get("partner_gender_preference") or "unknown"))
            if _scenario_requires_partner_preference(media_scenario) and partner_pref == "unknown":
                _set_media_pref_prompt_context(
                    session_id=session_id,
                    query=media_query_for_generation,
                    scenario=media_scenario,
                )
                out = _postprocess_output(
                    "呀哈～开工前先确认一下呀：你希望正缘形象偏向女生、男生，还是不限呢？你回我“女生 / 男生 / 不限”就行～",
                    qtype="media",
                )
                response_data = {
                    "session_id": session_id,
                    "output": out,
                    "message_type": "text",
                    "media": [],
                    "extra": {
                        "awaiting_partner_preference": True,
                        "media_scenario": media_scenario,
                        "choice_buttons": [
                            {"text": "女生", "send_text": "女生"},
                            {"text": "男生", "send_text": "男生"},
                            {"text": "不限", "send_text": "不限"},
                        ],
                    },
                }
                _append_chat_history(
                    chat_message_history,
                    query,
                    out,
                    user_id=user_id,
                    session_id=session_id,
                    question_type="media",
                    route_path="media_pref_prompt",
                    extra_meta=_intent_meta({
                        "media_status": "awaiting_preference",
                        "media_scenario": media_scenario,
                    }),
                )
                _log_route_observability(
                    route_path="media_pref_prompt",
                    reason_code="media_partner_preference_missing",
                    flag_snapshot=flag_snapshot,
                    domain_intent="media",
                    question_type="media",
                )
                return _with_intent(response_data)
            if bool(media_intent.get("blocked")):
                blocked_reason = str(media_intent.get("blocked_reason") or "内容不符合生成要求")
                out = _postprocess_output(
                    f"这条内容我不能直接生成媒体：{blocked_reason}。你可以换一个更安全的描述，我继续帮你生成。",
                    qtype="media",
                )
                response_data = {
                    "session_id": session_id,
                    "output": out,
                    "message_type": "text",
                    "media": [],
                    "extra": {"media_blocked": True, "blocked_reason": blocked_reason},
                }
                _append_chat_history(
                    chat_message_history,
                    query,
                    out,
                    user_id=user_id,
                    session_id=session_id,
                    question_type="media",
                    route_path="media_blocked",
                    extra_meta=_intent_meta({
                        "media_status": "blocked",
                        "media_scenario": media_scenario,
                        "blocked_reason": blocked_reason,
                    }),
                )
                _log_route_observability(
                    route_path="media_blocked",
                    reason_code="media_safety_blocked",
                    flag_snapshot=flag_snapshot,
                    domain_intent="media",
                    question_type="media",
                )
                return _with_intent(response_data)

            task, err_code = _create_and_submit_media_task(
                user_id=user_id,
                session_id=session_id,
                query=media_query_for_generation,
                scenario=media_scenario,
                profile=profile,
                user_identity=session_id,
            )
            if not task:
                _metric_incr("media_failed_total")
                degraded_meta = {}
                if str(err_code or "").startswith("DIFY_"):
                    degraded_meta = provider_extra_meta(
                        {
                            "provider": "dify",
                            "category": _provider_category_from_error_code(str(err_code or "")),
                            "error_code": str(err_code or ""),
                        },
                        degraded=True,
                    )
                out = _postprocess_output(
                    "文生图/视频功能暂未开启，请稍后再试，或先告诉我你想要的风格与场景，我先给你文案草图。",
                    qtype="media",
                )
                response_data = {
                    "session_id": session_id,
                    "output": out,
                    "message_type": "media_failed",
                    "media": [],
                    "extra": {"reason_code": err_code or "media_unavailable", **degraded_meta},
                }
                _append_chat_history(
                    chat_message_history,
                    query,
                    out,
                    user_id=user_id,
                    session_id=session_id,
                    question_type="media",
                    route_path="media_unavailable",
                    extra_meta=_intent_meta({
                        "media_status": "failed",
                        "media_scenario": media_scenario,
                        "media_error_code": err_code or "media_unavailable",
                        **degraded_meta,
                    }),
                )
                _log_route_observability(
                    route_path="media_unavailable",
                    reason_code=err_code or "media_unavailable",
                    flag_snapshot=flag_snapshot,
                    domain_intent="media",
                    question_type="media",
                    extra_meta=degraded_meta,
                )
                return _with_intent(response_data)

            task_api = _build_media_task_response(session_id, task)
            media_output = str(task_api.get("output") or "")
            if resumed_media_ctx:
                pref_cn = _partner_preference_cn(str(profile.get("partner_gender_preference") or "unknown"))
                if pref_cn:
                    media_output = f"呀哈～收到啦，这次按你偏好的{pref_cn}来生成。\n{media_output}"
            task_api["output"] = _postprocess_output(media_output, qtype="media")
            media_failure_meta = provider_extra_meta(_media_provider_failure_meta(task), degraded=True)
            if media_failure_meta.get("provider"):
                extra_payload = task_api.get("extra") if isinstance(task_api.get("extra"), dict) else {}
                extra_payload.update(media_failure_meta)
                if str(task.get("status") or "") == "failed":
                    extra_payload.setdefault("reason_code", "MEDIA_PROVIDER_DEGRADED")
                task_api["extra"] = extra_payload
            response_data = task_api
            task_status = str(task.get("status") or "")
            if task_status in {"pending", "running"}:
                _metric_incr("media_pending_total")
            elif task_status == "succeeded":
                _metric_incr("media_success_total")
            else:
                _metric_incr("media_failed_total")
            _append_chat_history(
                chat_message_history,
                query,
                str(response_data.get("output") or ""),
                user_id=user_id,
                session_id=session_id,
                question_type="media",
                route_path="media_pipeline",
                extra_meta=_intent_meta({
                    "media_status": task_status,
                    "media_scenario": media_scenario,
                    "media_task_id": str(task.get("task_id") or ""),
                    "media_message_type": str(response_data.get("message_type") or ""),
                    "media_resumed_after_preference": bool(resumed_media_ctx),
                    **media_failure_meta,
                }),
            )
            media_reason_code = f"media_{task_status or 'unknown'}"
            if media_failure_meta.get("provider") and task_status == "failed":
                media_reason_code = str(media_failure_meta.get("provider_error_code") or "MEDIA_PROVIDER_DEGRADED")
            _log_route_observability(
                route_path="media_pipeline",
                reason_code=media_reason_code,
                flag_snapshot=flag_snapshot,
                domain_intent="media",
                question_type="media",
                extra_meta=media_failure_meta,
            )
            return _with_intent(response_data)

        if str(intent_decision.get("route") or "") == "media_feedback":
            out = _postprocess_output(
                "呀哈～你这句更像是在聊刚才的作品反馈，我收到啦。要是你想再来一版，可以直接说“再来一版/同风格再来”，也可以说“我想要一张xx图”。",
                qtype="media",
            )
            response_data = {
                "session_id": session_id,
                "output": out,
                "message_type": "text",
                "media": [],
            }
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type="media",
                route_path="media_feedback_chat",
                extra_meta=_intent_meta({"media_status": "feedback"}),
            )
            _log_route_observability(
                route_path="media_feedback_chat",
                reason_code=str(intent_decision.get("reason_code") or "media_feedback"),
                flag_snapshot=flag_snapshot,
                domain_intent="media",
                question_type="media",
            )
            return _with_intent(response_data)

        if str(intent_decision.get("route") or "") == "media_clarify":
            out = _postprocess_output(
                "呜啦～我还没拿到可复用的上一条作品。你可以直接说“按上一个风格生成”或“我想要一张/一段 + 内容”，我就马上开工～",
                qtype="media",
            )
            response_data = {
                "session_id": session_id,
                "output": out,
                "message_type": "text",
                "media": [],
            }
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type="media",
                route_path="media_clarify_chat",
                extra_meta=_intent_meta({"media_status": "clarify"}),
            )
            _log_route_observability(
                route_path="media_clarify_chat",
                reason_code=str(intent_decision.get("reason_code") or "media_clarify"),
                flag_snapshot=flag_snapshot,
                domain_intent="media",
                question_type="media",
            )
            return _with_intent(response_data)

        if (
            str(intent_decision.get("route") or "") == "chat"
            and str(intent_decision.get("reason_code") or "") == "media_mention_no_command"
        ):
            out = _postprocess_output(
                "呀哈～如果你要我现在开生成，直接说“我想要一张/一段 + 内容”就行；如果你想聊刚才那条作品感受，我也在听呀～",
                qtype="media",
            )
            response_data = {
                "session_id": session_id,
                "output": out,
                "message_type": "text",
                "media": [],
            }
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type="media",
                route_path="media_chat_hint",
                extra_meta=_intent_meta({"media_status": "chat_hint"}),
            )
            _log_route_observability(
                route_path="media_chat_hint",
                reason_code="media_mention_no_command",
                flag_snapshot=flag_snapshot,
                domain_intent="media",
                question_type="media",
            )
            return _with_intent(response_data)

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
                extra_meta=_intent_meta(),
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
            return _with_intent(response_data)
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
                extra_meta=_intent_meta(),
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
            return _with_intent({
                "session_id": session_id,
                "output": out,
            })
        zodiac_reply, zodiac_meta = route_zodiac_pipeline(query, allow_clarify=bool(flags.get("clarify_v2")))
        if zodiac_reply is not None:
            if is_fortune_intent:
                _metric_incr("fortune_route_hit_total")
            z_qtype = str(((zodiac_meta or {}).get("question_type") if isinstance(zodiac_meta, dict) else "") or question_type)
            z_reason = flag_reason_code
            z_route = "zodiac_pipeline"
            if isinstance(zodiac_meta, dict) and str(zodiac_meta.get("source") or "") == "zodiac_clarify":
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
                extra_meta=_intent_meta(),
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
            return _with_intent({
                "session_id": session_id,
                "output": out,
            })
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
            fortune_failure = _fortune_provider_failure(fortune_payload)
            fortune_failure_meta = (
                {
                    "provider": str(fortune_failure.get("provider") or ""),
                    "category": str(fortune_failure.get("category") or ""),
                    "error_code": str(fortune_failure.get("provider_code") or fortune_failure.get("code") or ""),
                }
                if fortune_failure.get("provider")
                else None
            )
            fortune_meta = provider_extra_meta(fortune_failure_meta, degraded=bool(fortune_failure_meta))
            _append_chat_history(
                chat_message_history,
                query,
                out,
                user_id=user_id,
                session_id=session_id,
                question_type=qtype_for_metrics,
                route_path="fortune_provider_fallback" if fortune_failure.get("provider") else "fortune_pipeline",
                extra_meta=_intent_meta(fortune_meta),
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
            if fortune_failure.get("provider"):
                final_reason = str(fortune_failure.get("provider_code") or fortune_failure.get("code") or final_reason)
            _log_route_observability(
                route_path="fortune_provider_fallback" if fortune_failure.get("provider") else "fortune_pipeline",
                reason_code=final_reason,
                flag_snapshot=flag_snapshot,
                domain_intent="fortune",
                question_type=qtype_for_metrics,
                extra_meta=fortune_meta,
            )
            response_payload = {
                "session_id": session_id,
                "output": out,
            }
            if fortune_failure.get("provider"):
                response_payload["extra"] = fortune_meta
            return _with_intent(response_payload)
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
            extra_meta=_intent_meta(),
        )
        _log_route_observability(
            route_path="agent_fallback",
            reason_code=flag_reason_code,
            flag_snapshot=flag_snapshot,
            domain_intent=domain_intent,
            question_type=question_type,
        )
        return _with_intent(response_data)
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
                extra_meta=_intent_meta(),
            )
        _log_route_observability(
            route_path="error",
            reason_code="exception",
            flag_snapshot=flag_snapshot,
            domain_intent=domain_intent,
            question_type=question_type,
        )
        return _with_intent(response_data)


@app.post("/media/tasks", summary="创建媒体生成任务", tags=["Media"], responses={401: {"description": "未登录"}})
async def create_media_task_api(request: Request, payload: MediaTaskCreateRequest):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    auth = _get_auth_session(token or "")
    if not auth:
        return JSONResponse({"output": "请先登录后再继续聊天。"}, status_code=401)

    query = str(payload.query or "").strip()
    if not query:
        return JSONResponse({"ok": False, "message": "query 不能为空"}, status_code=400)

    phone = str(auth.get("phone", ""))
    user = _get_user_by_phone(phone) or {}
    if not user:
        return JSONResponse({"output": "登录信息异常，请重新登录后再试。"}, status_code=401)

    user_id = int(user.get("id") or 0)
    # 安全要求：会话ID必须绑定当前登录用户，不能信任客户端传入的 session_id。
    session_id = str(user.get("uuid") or f"phone_{phone}" or str(uuid.uuid4().hex))
    profile = merge_session_profile(
        session_id,
        extract_profile_from_query(query),
    )

    scenario = str(payload.scenario or "").strip()
    allowed_scenarios = {
        "destined_portrait",
        "destined_video",
        "encounter_story_video",
        "healing_sleep_video",
        "general_image",
        "general_video",
    }
    intent_decision: dict = {}
    if scenario:
        if scenario not in allowed_scenarios:
            return JSONResponse({"ok": False, "message": "scenario 不合法"}, status_code=400)
        blocked, blocked_reason = check_media_safety(query)
        media_intent = {"hit": True, "scenario": scenario, "blocked": blocked, "blocked_reason": blocked_reason}
        intent_decision = {
            "route": "media_create",
            "scenario": scenario,
            "confidence": "high",
            "reason_code": "api_explicit_scenario",
            "blocked": bool(blocked),
            "blocked_reason": str(blocked_reason or ""),
            "media_like": True,
            "needs_llm": False,
            "intent_version": "v3",
            "decision_source": "rule",
            "create_score": 2,
            "feedback_score": 0,
            "negation_guard_hit": False,
            "conflict": False,
        }
    else:
        media_intent, intent_decision = _resolve_media_intent(query, session_id)
    if not media_intent.get("hit"):
        return JSONResponse({"ok": False, "message": "当前输入未命中媒体生成意图"}, status_code=400)
    if bool(media_intent.get("blocked")):
        return JSONResponse(
            {
                "ok": False,
                "message": f"当前输入不支持生成：{str(media_intent.get('blocked_reason') or '内容不安全')}",
            },
            status_code=400,
        )

    media_scenario = str(media_intent.get("scenario") or "")
    if _scenario_requires_profile_for_media(media_scenario):
        missing_profile = _missing_profile_fields_for_fortune(profile)
        if missing_profile:
            return JSONResponse(
                {
                    "ok": False,
                    "message": build_media_missing_reply(missing_profile),
                    "missing_fields": missing_profile,
                },
                status_code=400,
            )
    task, err_code = _create_and_submit_media_task(
        user_id=user_id,
        session_id=session_id,
        query=query,
        scenario=media_scenario,
        profile=profile,
        user_identity=session_id,
    )
    if not task:
        degraded_payload = {}
        if str(err_code or "").startswith("DIFY_"):
            degraded_payload = {
                "provider": "dify",
                "provider_error_code": str(err_code or ""),
            }
        return JSONResponse(
            {
                "ok": False,
                "message": "当前媒体生成能力暂不可用，请稍后重试" if degraded_payload else "媒体能力不可用，请稍后重试",
                "error_code": "MEDIA_PROVIDER_DEGRADED" if degraded_payload else (err_code or "media_unavailable"),
                **degraded_payload,
            },
            status_code=503,
        )
    task_resp = _build_media_task_response(session_id, task)
    task_resp["status"] = str(task.get("status") or "")
    media_failure = _media_provider_failure_meta(task)
    if media_failure:
        return JSONResponse(
            {
                "ok": False,
                "message": "当前媒体生成能力暂不可用，请稍后重试",
                "error_code": "MEDIA_PROVIDER_DEGRADED",
                "provider": str(media_failure.get("provider") or ""),
                "provider_error_code": str(media_failure.get("error_code") or ""),
            },
            status_code=503,
        )
    extra = task_resp.get("extra") if isinstance(task_resp.get("extra"), dict) else {}
    extra.update(_intent_extra_payload(intent_decision))
    task_resp["extra"] = extra
    return task_resp


@app.get("/media/tasks/{task_id}", summary="查询媒体任务状态", tags=["Media"], responses={401: {"description": "未登录"}})
async def get_media_task_api(request: Request, task_id: str):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    auth = _get_auth_session(token or "")
    if not auth:
        return JSONResponse({"output": "请先登录后再继续聊天。"}, status_code=401)

    phone = str(auth.get("phone", ""))
    user = _get_user_by_phone(phone) or {}
    if not user:
        return JSONResponse({"output": "登录信息异常，请重新登录后再试。"}, status_code=401)

    user_id = int(user.get("id") or 0)
    task = get_media_task(_db_conn, str(task_id or ""), user_id=user_id)
    if not task:
        return JSONResponse({"ok": False, "message": "任务不存在"}, status_code=404)
    task_session_id = str(task.get("session_id") or "")
    if _DIFY_MEDIA_CLIENT:
        task = refresh_media_task(
            _db_conn,
            _DIFY_MEDIA_CLIENT,
            task_id=str(task_id or ""),
            user_id=user_id,
            user_identity=task_session_id,
            timeout_seconds=MEDIA_TIMEOUT_SECONDS,
        )
    if not task:
        return JSONResponse({"ok": False, "message": "任务不存在"}, status_code=404)
    _set_last_media_context(
        str(task.get("session_id") or task_session_id),
        task_id=str(task.get("task_id") or task_id),
        scenario=str(task.get("scenario") or ""),
        query=_task_input_query(task),
        status=str(task.get("status") or ""),
    )
    task_resp = _build_media_task_response(str(task.get("session_id") or task_session_id), task)
    task_resp["status"] = str(task.get("status") or "")
    task_resp["poll_interval_seconds"] = int(max(1, MEDIA_POLL_INTERVAL_SECONDS))
    return task_resp


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
