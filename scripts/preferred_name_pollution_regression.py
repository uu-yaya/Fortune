#!/usr/bin/env python3
import argparse
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


@dataclass
class Case:
    cid: str
    title: str
    setup_turns: list[str]
    expect_preferred: str
    expect_identity_contains: list[str]
    forbid_identity_contains: list[str]


def pick_phone() -> str:
    return "13" + "".join(str(random.randint(0, 9)) for _ in range(9))


def post_json(session: requests.Session, url: str, payload: dict[str, Any], timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def get_json(session: requests.Session, url: str, timeout: int) -> requests.Response:
    return session.get(url, timeout=timeout)


def safe_json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def contains_any(text: str, tokens: list[str]) -> bool:
    src = str(text or "")
    for token in tokens:
        if token and token in src:
            return True
    return False


def build_cases() -> list[Case]:
    return [
        Case(
            "PN-001",
            "显式昵称应写入",
            ["我叫刘芷华", "叫我华华"],
            "华华",
            ["华华", "刘芷华"],
            [],
        ),
        Case(
            "PN-002",
            "pending后“我是男生”不应污染",
            ["我叫林雨", "我是男生"],
            "",
            ["林雨"],
            ["男生"],
        ),
        Case(
            "PN-003",
            "pending后“我是很好的人”不应污染",
            ["我叫陈宁", "我是很好的人"],
            "",
            ["陈宁"],
            ["很好的人", "好人"],
        ),
        Case(
            "PN-004",
            "pending后偏题+短词不应误写昵称",
            ["我叫阿宁", "帮我看一下今天工作节奏", "小宁"],
            "",
            ["阿宁"],
            ["小宁"],
        ),
        Case(
            "PN-005",
            "显式无效昵称（数字）应拦截",
            ["我叫阿泽", "叫我2026"],
            "",
            ["阿泽"],
            ["2026"],
        ),
        Case(
            "PN-006",
            "显式英文昵称应写入",
            ["我叫李映葵", "以后叫我Ava"],
            "Ava",
            ["Ava", "李映葵"],
            [],
        ),
        Case(
            "PN-007",
            "pending后疑问句不应写入昵称",
            ["我叫周周", "可以叫我花花吗"],
            "",
            ["周周"],
            ["花花"],
        ),
        Case(
            "PN-008",
            "pending后短独立词可作为昵称",
            ["我叫苏苏", "酥酥"],
            "酥酥",
            ["酥酥", "苏苏"],
            [],
        ),
        Case(
            "PN-009",
            "显式“你可以叫我XX”应写入",
            ["我叫王木木", "你可以叫我木木"],
            "木木",
            ["木木", "王木木"],
            [],
        ),
        Case(
            "PN-010",
            "pending后身份描述不应污染",
            ["我叫赵青", "我是一个普通打工人"],
            "",
            ["赵青"],
            ["打工人", "普通打工人"],
        ),
    ]


def run_case(base_url: str, timeout: int, case: Case) -> dict[str, Any]:
    session = requests.Session()
    phone = pick_phone()
    password = "abc12345"

    logs: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    send = post_json(session, f"{base_url}/auth/send_code", {"phone": phone, "scene": "register"}, timeout=timeout)
    send_j = safe_json(send)
    logs.append({"api": "/auth/send_code", "input": {"phone": phone, "scene": "register"}, "status": send.status_code, "output": send_j})
    code = str(send_j.get("debug_code") or "")
    if send.status_code != 200 or not re.fullmatch(r"\d{6}", code):
        return {
            "case": case,
            "phone": phone,
            "ok": False,
            "fatal": f"send_code失败 status={send.status_code}",
            "logs": logs,
            "checks": checks,
        }

    verify = post_json(
        session,
        f"{base_url}/auth/verify",
        {"phone": phone, "code": code, "mode": "register", "password": password},
        timeout=timeout,
    )
    verify_j = safe_json(verify)
    logs.append(
        {
            "api": "/auth/verify",
            "input": {"phone": phone, "code": code, "mode": "register", "password": "***"},
            "status": verify.status_code,
            "output": verify_j,
        }
    )
    if verify.status_code != 200:
        return {
            "case": case,
            "phone": phone,
            "ok": False,
            "fatal": f"verify失败 status={verify.status_code}",
            "logs": logs,
            "checks": checks,
        }

    for idx, query in enumerate(case.setup_turns, start=1):
        chat = post_json(session, f"{base_url}/chat", {"query": query}, timeout=timeout)
        chat_j = safe_json(chat)
        logs.append({"api": "/chat", "step": idx, "input": {"query": query}, "status": chat.status_code, "output": chat_j})

    identity_query = "我叫什么你记得吗"
    identity = post_json(session, f"{base_url}/chat", {"query": identity_query}, timeout=timeout)
    identity_j = safe_json(identity)
    identity_output = str(identity_j.get("output") or "")
    logs.append(
        {
            "api": "/chat",
            "step": len(case.setup_turns) + 1,
            "input": {"query": identity_query},
            "status": identity.status_code,
            "output": identity_j,
        }
    )

    me = get_json(session, f"{base_url}/auth/me", timeout=timeout)
    me_j = safe_json(me)
    profile = me_j.get("profile") if isinstance(me_j.get("profile"), dict) else {}
    preferred_name = str(profile.get("preferred_name") or "")
    logs.append({"api": "/auth/me", "input": {}, "status": me.status_code, "output": me_j})

    must_have = contains_any(identity_output, case.expect_identity_contains)
    checks.append(
        {
            "name": "identity_should_contain_expected_name",
            "ok": must_have,
            "detail": f"expect_any={case.expect_identity_contains}",
        }
    )
    must_not = not contains_any(identity_output, case.forbid_identity_contains)
    checks.append(
        {
            "name": "identity_should_not_contain_forbidden_tokens",
            "ok": must_not,
            "detail": f"forbid={case.forbid_identity_contains}",
        }
    )
    if case.expect_preferred:
        pref_ok = preferred_name == case.expect_preferred
        checks.append(
            {
                "name": "preferred_name_should_equal_expected",
                "ok": pref_ok,
                "detail": f"expected={case.expect_preferred}, actual={preferred_name}",
            }
        )
    else:
        pref_ok = preferred_name == ""
        checks.append(
            {
                "name": "preferred_name_should_stay_empty",
                "ok": pref_ok,
                "detail": f"actual={preferred_name}",
            }
        )

    ok = all(bool(c.get("ok")) for c in checks)
    return {
        "case": case,
        "phone": phone,
        "ok": ok,
        "fatal": "",
        "logs": logs,
        "checks": checks,
    }


def render_markdown(base_url: str, results: list[dict[str, Any]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(results)
    passed = sum(1 for r in results if r.get("ok"))
    failed = total - passed
    lines: list[str] = []
    lines.append("# 33-preferred-name-污染专项回归报告-2026-03-02")
    lines.append("")
    lines.append(f"- 测试时间: {now}")
    lines.append(f"- 测试环境: `{base_url}`")
    lines.append(f"- 用例总数: `{total}`")
    lines.append(f"- 通过: `{passed}`")
    lines.append(f"- 失败: `{failed}`")
    lines.append("")
    for result in results:
        case: Case = result["case"]
        lines.append(f"## {case.cid} {case.title}")
        lines.append("")
        lines.append(f"- 手机号: `{result.get('phone')}`")
        lines.append(f"- 结果: `{'PASS' if result.get('ok') else 'FAIL'}`")
        if result.get("fatal"):
            lines.append(f"- FATAL: `{result.get('fatal')}`")
            lines.append("")
            continue
        lines.append("")
        lines.append("### 检查项")
        for check in result.get("checks") or []:
            icon = "PASS" if check.get("ok") else "FAIL"
            lines.append(f"- [{icon}] `{check.get('name')}` | {check.get('detail')}")
        lines.append("")
        lines.append("### 输入与输出原文")
        for item in result.get("logs") or []:
            lines.append(f"#### {item.get('api')} status={item.get('status')}")
            lines.append("输入:")
            lines.append("```json")
            lines.append(json.dumps(item.get("input") or {}, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("输出:")
            lines.append("```json")
            lines.append(json.dumps(item.get("output") or {}, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="preferred_name 污染专项回归")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=80)
    parser.add_argument("--out", default="docs/33-preferred-name-污染专项回归报告-2026-03-02.md")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    cases = build_cases()
    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"[RUN] {case.cid} {case.title}")
        result = run_case(base_url, args.timeout, case)
        status = "PASS" if result.get("ok") else "FAIL"
        print(f"[{status}] {case.cid}")
        results.append(result)

    md = render_markdown(base_url, results)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    failed = sum(1 for r in results if not r.get("ok"))
    print(f"[DONE] total={len(results)} failed={failed} report={out_path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
