import json
import hashlib
import re
import uuid
from datetime import datetime
from typing import Any


MEDIA_STATUS_TERMINAL = {"succeeded", "failed", "timeout"}
SCENARIO_LABELS = {
    "destined_portrait": "正缘写实画像",
    "destined_video": "正缘视频",
    "encounter_story_video": "正缘剧情片段",
    "healing_sleep_video": "命理治愈视频",
    "general_image": "专属图片",
    "general_video": "专属视频",
}

_TABLE_READY = False


def _is_retryable_poll_failure(poll_status: str, error_code: str, provider_category: str = "") -> bool:
    status = str(poll_status or "").strip().lower()
    code = str(error_code or "").strip().upper()
    category = str(provider_category or "").strip().lower()
    if status not in {"failed", "timeout"}:
        return False
    if category in {"timeout", "network", "http_5xx"}:
        return True
    if category in {"quota", "auth", "invalid_response", "safety"}:
        return False
    if code in {"DIFY_TIMEOUT", "DIFY_REQUEST_FAILED"}:
        return True
    if code.startswith("DIFY_HTTP_"):
        try:
            http_status = int(code.split("_")[-1])
        except Exception:
            return False
        return http_status in {408, 425, 429} or http_status >= 500
    return False


def _json_dumps(data: Any) -> str | None:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return None


def _json_loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _extract_query_from_task(task: dict[str, Any]) -> str:
    if not isinstance(task, dict):
        return ""
    input_json = task.get("input_json")
    if isinstance(input_json, dict):
        return str(input_json.get("query") or "").strip()
    return ""


def _short_media_subject(query: str, scenario: str) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    text = re.sub(r"[，。！？,.!?：:；;]", " ", q)
    text = re.sub(r"\b\d+\s*(秒|s|sec|分钟|min)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(请|帮我|给我|我想|想要|想|麻烦|可以|能不能|可不可以|来个|做个|整一个|搞个|给个|生成|制作|做|画|出|一下|一张|一个|一段|一条|个|张|段|条)",
        " ",
        text,
    )
    media_words = r"(视频|短视频|短片|片段|动画|mv|图|图片|图像|画像|照片|海报|插画|壁纸)"
    text = re.sub(media_words, " ", text)
    text = re.sub(r"\s+", "", text).strip()
    if not text:
        return ""
    if len(text) > 10:
        text = text[:10]
    suffix = "视频" if str(scenario or "") == "general_video" else "图片"
    return f"{text}{suffix}"


def _scenario_label(scenario: str, task: dict[str, Any] | None = None) -> str:
    scenario_key = str(scenario or "")
    if scenario_key in {"general_image", "general_video"}:
        subject = _short_media_subject(_extract_query_from_task(task or {}), scenario_key)
        if subject:
            return subject
    return SCENARIO_LABELS.get(scenario_key, "媒体内容")


def _build_pending_output(label: str, task_id: str) -> str:
    templates = [
        "哼～我已经开始为你生成{label}啦，先把光影和气场调到位，通常几十秒就好。",
        "呀哈～{label}正在路上，我在悄悄打磨细节，给我一点点时间。",
        "呜啦～{label}已开工，本鼠鼠正在盯着渲染进度，马上回来给你看。",
        "噗噜～我在为你生成{label}，先把氛围和质感捏好，稍等一下下。",
    ]
    seed = hashlib.sha256(f"{task_id}|{label}".encode("utf-8")).hexdigest() if task_id else ""
    idx = int(seed[:8], 16) % len(templates) if seed else 0
    return templates[idx].format(label=label)


def ensure_media_tasks_table(conn_factory) -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    ddl = """
    CREATE TABLE IF NOT EXISTS media_tasks (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,
      task_id CHAR(32) NOT NULL UNIQUE,
      user_id BIGINT NOT NULL,
      session_id VARCHAR(64) NOT NULL,
      scenario VARCHAR(64) NOT NULL,
      status ENUM('pending', 'running', 'succeeded', 'failed', 'timeout') NOT NULL DEFAULT 'pending',
      dify_run_id VARCHAR(128) NULL,
      input_json JSON NULL,
      output_json JSON NULL,
      error_code VARCHAR(64) NULL,
      error_message VARCHAR(255) NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      finished_at DATETIME NULL,
      INDEX idx_media_user_created (user_id, created_at),
      INDEX idx_media_session_created (session_id, created_at),
      INDEX idx_media_status_created (status, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    _TABLE_READY = True


def create_media_task(
    conn_factory,
    *,
    user_id: int,
    session_id: str,
    scenario: str,
    query: str,
    prompt_bundle: dict[str, Any],
) -> dict[str, Any]:
    ensure_media_tasks_table(conn_factory)
    task_id = uuid.uuid4().hex
    input_json = _json_dumps(
        {
            "query": str(query or ""),
            "scenario": str(scenario or ""),
            "prompt_bundle": prompt_bundle or {},
        }
    )
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO media_tasks (task_id, user_id, session_id, scenario, status, input_json)
                VALUES (%s, %s, %s, %s, 'pending', %s)
                """,
                (task_id, int(user_id or 0), str(session_id or ""), str(scenario or ""), input_json),
            )
    return get_media_task(conn_factory, task_id, user_id=int(user_id or 0)) or {}


def get_media_task(conn_factory, task_id: str, *, user_id: int = 0) -> dict[str, Any] | None:
    ensure_media_tasks_table(conn_factory)
    task = str(task_id or "").strip()
    if not task:
        return None
    with conn_factory() as conn:
        with conn.cursor() as cur:
            if int(user_id or 0) > 0:
                cur.execute(
                    """
                    SELECT * FROM media_tasks
                    WHERE task_id = %s AND user_id = %s
                    LIMIT 1
                    """,
                    (task, int(user_id or 0)),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM media_tasks
                    WHERE task_id = %s
                    LIMIT 1
                    """,
                    (task,),
                )
            row = cur.fetchone()
    if not row:
        return None
    row["input_json"] = _json_loads(row.get("input_json"))
    row["output_json"] = _json_loads(row.get("output_json"))
    return row


def _update_task_row(conn_factory, task_id: str, **fields) -> None:
    if not fields:
        return
    pairs = []
    values = []
    for key, value in fields.items():
        pairs.append(f"{key} = %s")
        values.append(value)
    values.append(str(task_id or ""))
    sql = f"UPDATE media_tasks SET {', '.join(pairs)} WHERE task_id = %s"
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(values))


def submit_media_task(
    conn_factory,
    dify_client,
    *,
    task_id: str,
    scenario: str,
    prompt_bundle: dict[str, Any],
    user_identity: str,
) -> dict[str, Any]:
    submit = dify_client.submit_workflow(
        scenario=str(scenario or ""),
        prompt=str(prompt_bundle.get("prompt") or ""),
        user=str(user_identity or "anonymous"),
        inputs=prompt_bundle or {},
    )
    run_id = str(submit.get("workflow_run_id") or "").strip()
    status = str(submit.get("status") or "running")
    media = submit.get("media") if isinstance(submit.get("media"), list) else []
    raw = submit.get("raw") if isinstance(submit.get("raw"), dict) else {}
    error_code = str(submit.get("error_code") or "")
    error_message = str(submit.get("error_message") or "")
    error_category = str(submit.get("error_category") or "")
    now = datetime.now()

    if status == "succeeded":
        _update_task_row(
            conn_factory,
            task_id,
            status="succeeded",
            dify_run_id=run_id or None,
            output_json=_json_dumps({"media": media, "raw": raw}),
            error_code=None,
            error_message=None,
            finished_at=now,
        )
    elif status in {"failed", "timeout"}:
        _update_task_row(
            conn_factory,
            task_id,
            status=status,
            dify_run_id=run_id or None,
            output_json=_json_dumps({"media": media, "raw": raw}),
            error_code=error_code or ("DIFY_TIMEOUT" if status == "timeout" else "DIFY_FAILED"),
            error_message=error_message or ("生成超时" if status == "timeout" else "生成失败"),
            finished_at=now,
        )
    else:
        _update_task_row(
            conn_factory,
            task_id,
            status="running",
            dify_run_id=run_id or None,
            output_json=_json_dumps({"media": media, "raw": raw}) if media else None,
            error_code=None,
            error_message=None,
        )
    return get_media_task(conn_factory, task_id) or {}


def refresh_media_task(
    conn_factory,
    dify_client,
    *,
    task_id: str,
    user_id: int,
    user_identity: str,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    task = get_media_task(conn_factory, task_id, user_id=int(user_id or 0))
    if not task:
        return None
    status = str(task.get("status") or "")
    if status in MEDIA_STATUS_TERMINAL:
        return task

    created_at = task.get("created_at")
    now = datetime.now()
    if isinstance(created_at, datetime):
        elapsed = (now - created_at).total_seconds()
        if elapsed > max(15, int(timeout_seconds or 180)):
            _update_task_row(
                conn_factory,
                task_id,
                status="timeout",
                error_code="MEDIA_TASK_TIMEOUT",
                error_message="媒体生成超时，请稍后重试",
                finished_at=now,
            )
            return get_media_task(conn_factory, task_id, user_id=int(user_id or 0))

    run_id = str(task.get("dify_run_id") or "").strip()
    if not run_id:
        return task

    # Keep polling identity aligned with submit-time identity (stored on task).
    effective_user_identity = str(task.get("session_id") or user_identity or "anonymous")
    poll = dify_client.get_workflow_status(run_id, user=effective_user_identity)
    poll_status = str(poll.get("status") or "running")
    media = poll.get("media") if isinstance(poll.get("media"), list) else []
    raw = poll.get("raw") if isinstance(poll.get("raw"), dict) else {}
    error_code = str(poll.get("error_code") or "")
    error_message = str(poll.get("error_message") or "")
    error_category = str(poll.get("error_category") or "")

    if _is_retryable_poll_failure(poll_status, error_code, error_category):
        _update_task_row(
            conn_factory,
            task_id,
            status="running",
            output_json=_json_dumps({"media": media, "raw": raw}) if media else _json_dumps(raw) if raw else None,
            error_code=error_code or None,
            error_message=error_message or None,
        )
        return get_media_task(conn_factory, task_id, user_id=int(user_id or 0))

    if poll_status == "succeeded":
        _update_task_row(
            conn_factory,
            task_id,
            status="succeeded",
            output_json=_json_dumps({"media": media, "raw": raw}),
            error_code=None,
            error_message=None,
            finished_at=now,
        )
    elif poll_status in {"failed", "timeout"}:
        _update_task_row(
            conn_factory,
            task_id,
            status=poll_status,
            output_json=_json_dumps({"media": media, "raw": raw}),
            error_code=error_code or ("DIFY_TIMEOUT" if poll_status == "timeout" else "DIFY_FAILED"),
            error_message=error_message or ("生成超时" if poll_status == "timeout" else "生成失败"),
            finished_at=now,
        )
    else:
        _update_task_row(
            conn_factory,
            task_id,
            status="running",
            output_json=_json_dumps({"media": media, "raw": raw}) if media else _json_dumps(raw) if raw else None,
        )
    return get_media_task(conn_factory, task_id, user_id=int(user_id or 0))


def media_task_to_api(task: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(task, dict):
        return {
            "task_id": "",
            "status": "failed",
            "message_type": "media_failed",
            "output": "未找到媒体任务。",
            "media": [],
            "scenario": "",
            "error_code": "TASK_NOT_FOUND",
            "error_message": "任务不存在",
        }
    status = str(task.get("status") or "failed")
    output_json = task.get("output_json") if isinstance(task.get("output_json"), dict) else {}
    media = output_json.get("media") if isinstance(output_json.get("media"), list) else []
    scenario = str(task.get("scenario") or "")
    label = _scenario_label(scenario, task)

    if status == "succeeded":
        if media:
            output = f"{label}已生成完成，点开卡片就能查看。"
            message_type = "media_result"
        else:
            output = "呀哈～这次显示完成了，但我没收到可展示的图/视频结果。你再试一次，本鼠鼠继续帮你盯紧。"
            message_type = "media_failed"
    elif status in {"pending", "running"}:
        output = _build_pending_output(label, str(task.get("task_id") or ""))
        message_type = "media_pending"
    elif status == "timeout":
        output = "呜啦～这次生成有点慢，已经超时啦。你可以再试一次，我会继续帮你盯着进度。"
        message_type = "media_failed"
    else:
        err_code = str(task.get("error_code") or "")
        err_msg = str(task.get("error_message") or "")
        if err_code == "DIFY_PROVIDER_LIMIT":
            output = "呀哈～这次被上游模型的额度/安全模式拦住啦。先到 Dify 调整额度后再试，我这边已经帮你记好任务了。"
        elif err_code == "DIFY_BREAKER_OPEN":
            output = "呀哈～当前媒体生成服务正在降级保护中，请稍后再试。"
        elif "less than 256" in err_msg.lower():
            output = "噗噜。上游 Dify 的提示词长度上限（256）触发了，所以这次没生成出来。你先在 Dify 调整上限，再试就更稳。"
        else:
            output = "呀哈～这次图/视频还没生成成功。稍后重试一次，通常就能恢复。"
        message_type = "media_failed"

    error_code = str(task.get("error_code") or "")
    error_message = str(task.get("error_message") or "")
    if status == "succeeded" and not media:
        error_code = error_code or "MEDIA_EMPTY_RESULT"
        error_message = error_message or "任务完成但未解析到媒体结果"

    return {
        "task_id": str(task.get("task_id") or ""),
        "status": status,
        "message_type": message_type,
        "output": output,
        "media": media if isinstance(media, list) else [],
        "scenario": scenario,
        "error_code": error_code,
        "error_message": error_message,
        "provider": "dify" if error_code.startswith("DIFY_") else "",
        "provider_error_code": error_code if error_code.startswith("DIFY_") else "",
        "extra": {
            "dify_run_id": str(task.get("dify_run_id") or ""),
            "scenario_label": label,
        },
    }
