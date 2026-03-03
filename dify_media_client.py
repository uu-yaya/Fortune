import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


IMAGE_EXT_PATTERN = re.compile(r"\.(png|jpg|jpeg|webp|gif|bmp|svg)(\?.*)?$", re.IGNORECASE)
VIDEO_EXT_PATTERN = re.compile(r"\.(mp4|mov|webm|m3u8|avi|mkv)(\?.*)?$", re.IGNORECASE)
HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
PROVIDER_LIMIT_PATTERN = re.compile(
    r"(达到.*推断限制|安全体验模式|safe experience mode|quota|rate limit|余额不足|insufficient)",
    re.IGNORECASE,
)


def _normalize_status(raw_status: str) -> str:
    status = str(raw_status or "").strip().lower()
    if status in {"succeeded", "success", "completed", "finished", "done"}:
        return "succeeded"
    if status in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if status in {"timeout", "timed_out"}:
        return "timeout"
    if status in {"running", "pending", "queued", "processing", "starting"}:
        return "running"
    return "running"


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_run_ids(payload: dict[str, Any]) -> tuple[str, str]:
    run_id = ""
    task_id = ""
    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [
        payload.get("workflow_run_id"),
        data_obj.get("workflow_run_id"),
        payload.get("run_id"),
        data_obj.get("run_id"),
        payload.get("id"),
        data_obj.get("id"),
    ]
    for item in candidates:
        value = str(item or "").strip()
        if value:
            run_id = value
            break
    task_candidates = [
        payload.get("task_id"),
        (payload.get("data") or {}).get("task_id") if isinstance(payload.get("data"), dict) else None,
    ]
    for item in task_candidates:
        value = str(item or "").strip()
        if value:
            task_id = value
            break
    return run_id, task_id


def _infer_kind_from_url(url: str) -> str:
    val = str(url or "").strip().lower()
    if VIDEO_EXT_PATTERN.search(val):
        return "video"
    if IMAGE_EXT_PATTERN.search(val):
        return "image"
    return "unknown"


def _clean_candidate_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    # Handle malformed captures like: https://...](https://real-url
    if "](" in text:
        text = text.split("](")[-1]
    text = text.strip(" \t\r\n'\"<>)}],;，。")
    if not text.startswith(("http://", "https://")):
        return ""
    if "..." in text:
        # Usually indicates a truncated pseudo-link in model narration.
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    if parsed.netloc == "...":
        return ""
    return text


def _extract_urls_from_text(text: str) -> list[str]:
    source = str(text or "")
    if not source:
        return []
    candidates: list[str] = []
    for m in MARKDOWN_LINK_PATTERN.finditer(source):
        url = _clean_candidate_url(m.group(1))
        if url:
            candidates.append(url)
    for hit in HTTP_URL_PATTERN.findall(source):
        url = _clean_candidate_url(hit)
        if url:
            candidates.append(url)
    dedup: list[str] = []
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        dedup.append(url)
    return dedup


def _walk_media_values(node: Any, found: list[dict[str, Any]], limit: int = 12) -> None:
    if len(found) >= limit:
        return
    if isinstance(node, dict):
        # 优先处理常见字段结构
        url = str(node.get("url") or node.get("file_url") or node.get("image_url") or node.get("video_url") or "").strip()
        if url.startswith("http") or url.startswith("/"):
            raw_kind = str(node.get("kind") or node.get("type") or "").strip().lower()
            kind = raw_kind if raw_kind in {"image", "video"} else str(_infer_kind_from_url(url))
            found.append(
                {
                    "kind": kind if kind in {"image", "video"} else "unknown",
                    "url": url,
                    "cover_url": str(node.get("cover_url") or node.get("thumbnail_url") or ""),
                    "duration_sec": node.get("duration_sec"),
                    "prompt_used": str(node.get("prompt_used") or node.get("prompt") or ""),
                }
            )
        for value in node.values():
            _walk_media_values(value, found, limit=limit)
    elif isinstance(node, list):
        for item in node:
            _walk_media_values(item, found, limit=limit)
            if len(found) >= limit:
                break
    elif isinstance(node, str):
        for hit in _extract_urls_from_text(node):
            kind = _infer_kind_from_url(hit)
            if kind in {"image", "video"}:
                found.append(
                    {
                        "kind": kind,
                        "url": hit,
                        "cover_url": "",
                        "duration_sec": None,
                        "prompt_used": "",
                    }
                )
                if len(found) >= limit:
                    return


def _extract_media(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    _walk_media_values(payload, found, limit=12)
    seen = set()
    out = []
    for item in found:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"image", "video"}:
            kind = _infer_kind_from_url(url)
        if kind not in {"image", "video"}:
            continue
        out.append(
            {
                "kind": kind,
                "url": url,
                "cover_url": str(item.get("cover_url") or ""),
                "duration_sec": item.get("duration_sec"),
                "prompt_used": str(item.get("prompt_used") or ""),
            }
        )
    return out


def _extract_text_output(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    outputs = data_obj.get("outputs") if isinstance(data_obj.get("outputs"), dict) else {}
    text = str(outputs.get("text") or payload.get("answer") or payload.get("text") or "").strip()
    if text:
        return text
    output_obj = payload.get("output")
    if isinstance(output_obj, dict):
        return str(output_obj.get("text") or "").strip()
    return ""


def _extract_provider_limit_message(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if not PROVIDER_LIMIT_PATTERN.search(raw):
        return ""
    compact = re.sub(r"\s+", " ", raw)
    return compact[:180]


def _apply_provider_text_error(normalized: dict[str, Any], payload_data: dict[str, Any]) -> dict[str, Any]:
    current = dict(normalized or {})
    if current.get("status") != "succeeded" or current.get("media"):
        return current
    text_output = _extract_text_output(payload_data)
    limit_msg = _extract_provider_limit_message(text_output)
    if not limit_msg:
        return current
    current.update(
        {
            "ok": False,
            "status": "failed",
            "error_code": "DIFY_PROVIDER_LIMIT",
            "error_message": limit_msg,
        }
    )
    return current


@dataclass
class DifyMediaClient:
    base_url: str
    api_key: str
    workflow_app_id: str = ""
    timeout_seconds: int = 180

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _base_origin(self) -> str:
        val = str(self.base_url or "").strip()
        if not val:
            return ""
        parsed = urlparse(val)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    def _normalize_media_urls(self, media: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(media, list):
            return []
        base_origin = self._base_origin()
        out: list[dict[str, Any]] = []
        for item in media:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "").strip()
            raw_cover = str(item.get("cover_url") or "").strip()
            if raw_url.startswith("/"):
                raw_url = urljoin(base_origin or "", raw_url) if base_origin else ""
            if raw_cover.startswith("/"):
                raw_cover = urljoin(base_origin or "", raw_cover) if base_origin else ""
            if not raw_url.startswith("http"):
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in {"image", "video"}:
                kind = _infer_kind_from_url(raw_url)
            out.append(
                {
                    "kind": kind if kind in {"image", "video"} else "image",
                    "url": raw_url,
                    "cover_url": raw_cover,
                    "duration_sec": item.get("duration_sec"),
                    "prompt_used": str(item.get("prompt_used") or ""),
                }
            )
        return out

    def submit_workflow(self, *, scenario: str, prompt: str, user: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/workflows/run"
        payload_inputs = dict(inputs or {})
        payload_inputs.setdefault("scenario", str(scenario or ""))
        payload_inputs["prompt"] = str(payload_inputs.get("prompt") or prompt or "")
        if self.workflow_app_id:
            payload_inputs.setdefault("workflow_app_id", self.workflow_app_id)
        payload = {
            "inputs": payload_inputs,
            "response_mode": "blocking",
            "user": str(user or "anonymous"),
        }
        try:
            resp = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=(8, self.timeout_seconds),
            )
            if resp.status_code >= 400:
                error_payload = _safe_json(resp)
                message = str(error_payload.get("message") or error_payload.get("error") or resp.text[:200] or "Dify 请求失败")
                return {
                    "ok": False,
                    "status": "failed",
                    "workflow_run_id": "",
                    "task_id": "",
                    "media": [],
                    "raw": error_payload,
                    "error_code": f"DIFY_HTTP_{resp.status_code}",
                    "error_message": message,
                }

            payload_data = _safe_json(resp)
            normalized = self._normalize_payload(payload_data)
            return _apply_provider_text_error(normalized, payload_data)
        except requests.Timeout:
            return {
                "ok": False,
                "status": "timeout",
                "workflow_run_id": "",
                "task_id": "",
                "media": [],
                "raw": {},
                "error_code": "DIFY_TIMEOUT",
                "error_message": "调用 Dify 超时",
            }
        except Exception as e:
            return {
                "ok": False,
                "status": "failed",
                "workflow_run_id": "",
                "task_id": "",
                "media": [],
                "raw": {},
                "error_code": "DIFY_REQUEST_FAILED",
                "error_message": str(e),
            }

    def get_workflow_status(self, workflow_run_id: str, *, user: str = "") -> dict[str, Any]:
        run_id = str(workflow_run_id or "").strip()
        if not run_id:
            return {
                "ok": False,
                "status": "failed",
                "workflow_run_id": "",
                "task_id": "",
                "media": [],
                "raw": {},
                "error_code": "EMPTY_RUN_ID",
                "error_message": "workflow_run_id 为空",
            }
        url = f"{self.base_url.rstrip('/')}/workflows/run/{run_id}"
        params = {"user": str(user or "anonymous")}
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=(8, self.timeout_seconds))
        except requests.Timeout:
            return {
                "ok": False,
                "status": "timeout",
                "workflow_run_id": run_id,
                "task_id": "",
                "media": [],
                "raw": {},
                "error_code": "DIFY_TIMEOUT",
                "error_message": "查询 Dify 任务超时",
            }
        except Exception as e:
            return {
                "ok": False,
                "status": "failed",
                "workflow_run_id": run_id,
                "task_id": "",
                "media": [],
                "raw": {},
                "error_code": "DIFY_REQUEST_FAILED",
                "error_message": str(e),
            }

        payload = _safe_json(resp)
        if resp.status_code >= 400:
            message = str(payload.get("message") or payload.get("error") or resp.text[:200] or "Dify 查询失败")
            return {
                "ok": False,
                "status": "failed",
                "workflow_run_id": run_id,
                "task_id": "",
                "media": [],
                "raw": payload,
                "error_code": f"DIFY_HTTP_{resp.status_code}",
                "error_message": message,
            }
        normalized = self._normalize_payload(payload)
        normalized = _apply_provider_text_error(normalized, payload)
        if not normalized.get("workflow_run_id"):
            normalized["workflow_run_id"] = run_id
        return normalized

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id, task_id = _extract_run_ids(payload)
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_status = str(payload.get("status") or payload.get("workflow_status") or data_obj.get("status") or "")
        status = _normalize_status(raw_status)
        media = self._normalize_media_urls(_extract_media(payload))
        if media and status == "running":
            status = "succeeded"
        return {
            "ok": status in {"running", "succeeded"},
            "status": status,
            "workflow_run_id": run_id,
            "task_id": task_id,
            "media": media,
            "raw": payload if isinstance(payload, dict) else {},
            "error_code": "",
            "error_message": "",
        }
