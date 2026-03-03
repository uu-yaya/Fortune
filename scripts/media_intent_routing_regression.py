#!/usr/bin/env python3
import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_intent import route_media_intent


@dataclass
class UnitCase:
    cid: str
    query: str
    expect_route: str
    expect_scenario: str = ""
    recent_scenario: str = ""
    allow_routes: tuple[str, ...] = ()


@dataclass
class ApiCase:
    cid: str
    query: str
    expect_message_type: str
    expect_no_task: bool = False
    expect_route_in: tuple[str, ...] = ("chat",)
    expect_source_in: tuple[str, ...] = ("rule", "llm")


def _rand_phone() -> str:
    return "13" + "".join(str(random.randint(0, 9)) for _ in range(9))


def _safe_json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def unit_cases() -> list[UnitCase]:
    return [
        UnitCase("U-001", "你给我生成的婚礼视频就是我的梦", "media_feedback"),
        UnitCase("U-002", "刚才生成的视频太美了", "media_feedback"),
        UnitCase("U-003", "那个视频绝了", "media_feedback"),
        UnitCase("U-004", "这张图太好看了", "media_feedback"),
        UnitCase("U-005", "帮我生成一个正缘画像", "media_create", "destined_portrait"),
        UnitCase("U-006", "给我生成一个正缘视频", "media_create", "destined_video"),
        UnitCase("U-007", "请生成我和正缘结婚的视频", "media_create", "encounter_story_video"),
        UnitCase("U-008", "帮我做一个睡前治愈视频", "media_create", "healing_sleep_video"),
        UnitCase("U-009", "来一个小猫视频", "media_create", "general_video"),
        UnitCase("U-010", "做一个咖啡店人像图", "media_create", "general_image"),
        UnitCase("U-011", "我想让你生成一个婚礼视频", "chat"),
        UnitCase("U-012", "我想看一个正缘画像", "media_create", "destined_portrait"),
        UnitCase("U-013", "那个视频你觉得怎么样", "chat"),
        UnitCase("U-014", "这个图像的构图很温柔", "media_feedback"),
        UnitCase("U-015", "上次生成的那张图我好喜欢", "media_feedback"),
        UnitCase("U-016", "再来一个", "media_clarify"),
        UnitCase("U-017", "同款再来", "media_clarify"),
        UnitCase("U-018", "换个风格", "media_clarify"),
        UnitCase("U-019", "再来一个", "media_followup", "destined_video", "destined_video"),
        UnitCase("U-020", "换个风格", "media_followup", "destined_portrait", "destined_portrait"),
        UnitCase("U-021", "同款再来", "media_followup", "encounter_story_video", "encounter_story_video"),
        UnitCase("U-022", "帮我分析一下感情", "chat"),
        UnitCase("U-023", "你记得我叫什么吗", "chat"),
        UnitCase("U-024", "帮我生成的那个视频真好看", "media_feedback"),
        UnitCase("U-025", "你给我生成的视频绝了，再来一个", "media_followup", "general_video", "general_video"),
        UnitCase("U-026", "给我生成露骨视频", "media_create", "general_video"),
        UnitCase("U-027", "请生成仿照某明星的画像", "media_create", "general_image"),
        UnitCase("U-028", "我和朋友多人婚礼剧情视频", "chat"),
        UnitCase("U-029", "帮我生成我和朋友多人婚礼剧情视频", "media_create", "encounter_story_video"),
        UnitCase("U-030", "给我来一个画像", "media_create", "general_image"),
        UnitCase("U-031", "帮我做个短片", "media_create", "general_video"),
        UnitCase("U-032", "能不能来个视频", "chat"),
        UnitCase("U-033", "我想要一个图", "media_create", "general_image"),
        UnitCase("U-034", "你刚给我生成的那支视频就是我的梦", "media_feedback"),
        UnitCase("U-035", "上次那张图就是我的梦", "media_feedback"),
        UnitCase("U-036", "请你帮我再生成一个正缘视频", "media_create", "destined_video"),
        UnitCase("U-037", "重新生成一条治愈视频", "media_create", "healing_sleep_video"),
        UnitCase("U-038", "帮我画一张我的正缘长相", "media_create", "destined_portrait"),
        UnitCase("U-039", "给我生成我和闺蜜的婚礼剧情视频", "media_create", "encounter_story_video"),
        UnitCase("U-040", "这个视频就是我的梦", "media_feedback"),
        UnitCase("U-041", "视频好看", "media_feedback"),
        UnitCase("U-042", "图片好看", "media_feedback"),
        UnitCase("U-043", "生成一张正缘画像", "media_create", "destined_portrait"),
        UnitCase("U-044", "生成正缘视频", "media_create", "destined_video"),
        # 纯自然表达 + 无命令词：误触发压力集（边界句）
        UnitCase("U-NC-001", "你给我生成的婚礼视频太戳我了", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-002", "上次那条正缘视频我循环看了好多遍", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-003", "这个视频的光影让我很安心", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-004", "这张图像很像我脑海里的感觉", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-005", "刚才那张图的氛围感真绝", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-006", "你做的那个片段让我想到未来", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-007", "婚礼剧情这个方向很对", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-008", "我和她的故事感好像被看见了", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-009", "这个画面就是我想要的关系状态", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-010", "那个视频我好喜欢", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-011", "这张图我好喜欢", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-012", "你的作品太美了", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-013", "我最近总会想到那支婚礼视频", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-014", "这个镜头语言挺高级的", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-015", "这个故事收尾好温柔", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-016", "那张图像细节真的很细", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-017", "视频节奏很舒服", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-018", "图像质感比我预期还好", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-019", "你刚给我的那条视频像电影", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-020", "上次的图片构图让我很有代入感", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-021", "我在想婚礼片段里两个人的眼神", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-022", "正缘画像这个概念我很吃", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-023", "治愈视频这个主题很适合我", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-024", "那条治愈视频陪我熬过了昨晚", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-025", "这个视频内容就是我的梦", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-026", "你给我的那张图很有故事感", "media_feedback", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-027", "我想聊聊刚才那个视频", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-028", "这类婚礼画面让我更有勇气", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-029", "那个片段让我觉得被温柔对待", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-NC-030", "图像和视频的整体审美都在线", "chat", allow_routes=("chat", "media_feedback")),
        UnitCase("U-V3-001", "把你这封情书变成一幅画", "media_create", "general_image"),
        UnitCase("U-V3-002", "把上面那段话画出来", "media_create", "general_image"),
        UnitCase("U-V3-003", "给这段文字配图", "media_create", "general_image"),
        UnitCase("U-V3-004", "我想要一个图", "media_create", "general_image"),
        UnitCase("U-V3-005", "我想看正缘画像", "media_create", "destined_portrait"),
        UnitCase("U-V3-006", "能给我一张图吗", "media_create", "general_image"),
        UnitCase("U-V3-007", "我不是要生成，只是想聊聊这个视频", "chat"),
        UnitCase("U-V3-008", "先别生成，聊聊这张图", "chat"),
        UnitCase("U-V3-009", "继续刚才那个风格再来一张", "media_followup", "general_image", "general_image"),
        UnitCase("U-V3-010", "按上一个视频节奏再来一版", "media_followup", "general_video", "general_video"),
        UnitCase("U-V3-011", "好看，再来一版", "media_followup", "general_image", "general_image"),
    ]


def api_cases() -> list[ApiCase]:
    return [
        ApiCase(
            cid="API-001",
            query="你给我生成的婚礼视频就是我的梦",
            expect_message_type="text",
            expect_no_task=True,
            expect_route_in=("media_feedback", "chat"),
            expect_source_in=("rule", "llm"),
        ),
        ApiCase(
            cid="API-002",
            query="那个视频很好看",
            expect_message_type="text",
            expect_no_task=True,
            expect_route_in=("chat", "media_feedback"),
            expect_source_in=("rule", "llm"),
        ),
        ApiCase(
            cid="API-003",
            query="帮我生成一个正缘写实画像",
            expect_message_type="media_pending",
            expect_no_task=False,
            expect_route_in=("media_create", "media_followup"),
            expect_source_in=("rule", "llm"),
        ),
        ApiCase(
            cid="API-004",
            query="帮我生成我和正缘结婚的视频",
            expect_message_type="media_pending",
            expect_no_task=False,
            expect_route_in=("media_create", "media_followup"),
            expect_source_in=("rule", "llm"),
        ),
        ApiCase(
            cid="API-005",
            query="把你这封情书变成一幅画",
            expect_message_type="media_pending",
            expect_no_task=False,
            expect_route_in=("media_create",),
            expect_source_in=("rule", "llm"),
        ),
        ApiCase(
            cid="API-006",
            query="我不是要生成，只是想聊聊这个视频",
            expect_message_type="text",
            expect_no_task=True,
            expect_route_in=("chat",),
            expect_source_in=("rule",),
        ),
    ]


def run_unit() -> dict[str, Any]:
    rows = []
    passed = 0
    confusion: dict[str, dict[str, int]] = {}
    errors: list[dict[str, Any]] = []
    all_cases = unit_cases()
    for c in all_cases:
        recent = {"scenario": c.recent_scenario, "task_id": "mock", "query": "mock", "status": "succeeded"} if c.recent_scenario else None
        got = route_media_intent(c.query, recent_media=recent)
        got_route = str(got.get("route") or "")
        confusion.setdefault(c.expect_route, {})
        confusion[c.expect_route][got_route] = int(confusion[c.expect_route].get(got_route) or 0) + 1
        if c.allow_routes:
            ok_route = got_route in set(c.allow_routes)
        else:
            ok_route = got_route == c.expect_route
        ok_scenario = True
        if c.allow_routes:
            ok_scenario = got_route not in {"media_create", "media_followup"}
        elif c.expect_scenario:
            ok_scenario = str(got.get("scenario") or "") == c.expect_scenario
        ok = bool(ok_route and ok_scenario)
        if ok:
            passed += 1
        else:
            errors.append(
                {
                    "cid": c.cid,
                    "query": c.query,
                    "expect_route": c.expect_route,
                    "got_route": got_route,
                    "expect_scenario": c.expect_scenario,
                    "got_scenario": str(got.get("scenario") or ""),
                    "reason_code": str(got.get("reason_code") or ""),
                    "create_score": int(got.get("create_score") or 0),
                    "feedback_score": int(got.get("feedback_score") or 0),
                    "negation_guard_hit": bool(got.get("negation_guard_hit") or False),
                }
            )
        rows.append(
            {
                "cid": c.cid,
                "query": c.query,
                "expect_route": c.expect_route,
                "allow_routes": list(c.allow_routes),
                "expect_scenario": c.expect_scenario,
                "got_route": got_route,
                "got_scenario": str(got.get("scenario") or ""),
                "reason_code": str(got.get("reason_code") or ""),
                "intent_source": str(got.get("decision_source") or ""),
                "create_score": int(got.get("create_score") or 0),
                "feedback_score": int(got.get("feedback_score") or 0),
                "ok": ok,
            }
        )
    return {
        "total": len(all_cases),
        "passed": passed,
        "failed": len(all_cases) - passed,
        "rows": rows,
        "confusion_matrix": confusion,
        "top_errors": errors[:20],
    }


def _auth_login(sess: requests.Session, base_url: str, timeout: int) -> dict[str, Any]:
    phone = _rand_phone()
    pwd = "abc12345"
    send = sess.post(f"{base_url}/auth/send_code", json={"phone": phone, "scene": "register"}, timeout=timeout)
    send_j = _safe_json(send)
    code = str(send_j.get("debug_code") or "")
    if send.status_code != 200 or len(code) != 6:
        return {"ok": False, "reason": "send_code_failed", "phone": phone, "send": send_j}
    verify = sess.post(
        f"{base_url}/auth/verify",
        json={"phone": phone, "code": code, "mode": "register", "password": pwd},
        timeout=timeout,
    )
    verify_j = _safe_json(verify)
    if verify.status_code != 200:
        return {"ok": False, "reason": "verify_failed", "phone": phone, "verify": verify_j}

    # 配齐必填资料 + 对象偏好，确保媒体流程可直接进入 pending。
    sess.post(f"{base_url}/chat", json={"query": "我叫回归甲，生日2001-01-01"}, timeout=timeout)
    sess.post(f"{base_url}/chat", json={"query": "我是男生"}, timeout=timeout)
    sess.post(f"{base_url}/chat", json={"query": "我喜欢女生"}, timeout=timeout)
    return {"ok": True, "phone": phone}


def run_api(base_url: str, timeout: int) -> dict[str, Any]:
    sess = requests.Session()
    login = _auth_login(sess, base_url, timeout)
    if not login.get("ok"):
        return {"total": 0, "passed": 0, "failed": 0, "rows": [], "fatal": login}

    rows = []
    passed = 0
    for c in api_cases():
        resp = sess.post(f"{base_url}/chat", json={"query": c.query}, timeout=timeout)
        data = _safe_json(resp)
        message_type = str(data.get("message_type") or "text")
        task_id = str(data.get("media_task_id") or "")
        route = str(((data.get("extra") or {}) if isinstance(data.get("extra"), dict) else {}).get("intent_route") or "")
        intent_source = str(((data.get("extra") or {}) if isinstance(data.get("extra"), dict) else {}).get("intent_source") or "")

        ok = resp.status_code == 200
        if c.expect_message_type == "media_pending":
            ok = ok and message_type in {"media_pending", "text"}
        else:
            ok = ok and message_type == c.expect_message_type
        if c.expect_no_task:
            ok = ok and (task_id == "")
        if c.expect_route_in:
            ok = ok and route in set(c.expect_route_in)
        if c.expect_source_in:
            ok = ok and intent_source in set(c.expect_source_in)

        # 允许正向媒体用例在缺 profile/偏好时走文本追问，但不能误触发失败。
        if c.expect_message_type == "media_pending" and message_type == "text":
            out = str(data.get("output") or "")
            ok = ok and (
                "帮我生成" in out
                or "我想要" in out
                or "女生" in out
                or "生日" in out
                or "名字" in out
                or "开工" in out
            )

        if ok:
            passed += 1
        rows.append(
            {
                "cid": c.cid,
                "query": c.query,
                "status": resp.status_code,
                "message_type": message_type,
                "media_task_id": task_id,
                "intent_route": route,
                "intent_source": intent_source,
                "ok": ok,
                "output": str(data.get("output") or "")[:140],
            }
        )
    return {"total": len(rows), "passed": passed, "failed": len(rows) - passed, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Media intent routing regression")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--unit-only", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    unit_result = run_unit()
    api_result = {"total": 0, "passed": 0, "failed": 0, "rows": []}
    if not args.unit_only:
        api_result = run_api(args.base_url.rstrip("/"), args.timeout)

    report = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "unit": unit_result,
        "api": api_result,
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {out_path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    unit_ok = unit_result["failed"] == 0
    api_ok = args.unit_only or api_result["failed"] == 0
    return 0 if (unit_ok and api_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
