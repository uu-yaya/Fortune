#!/usr/bin/env python3
import argparse
import random
import re
import sys
from dataclasses import dataclass

import requests


@dataclass
class Case:
    cid: str
    query: str
    expected: str  # missing_profile | fortune_detail | divination | general


def pick_phone() -> str:
    suffix = "".join(str(random.randint(0, 9)) for _ in range(9))
    return f"13{suffix}"


def post_json(session: requests.Session, url: str, payload: dict, timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def contains_any(text: str, candidates: list[str], min_hit: int = 1) -> bool:
    hit = sum(1 for c in candidates if c in text)
    return hit >= min_hit


def assert_case(case: Case, output: str, profile_name: str, profile_birthdate: str) -> tuple[bool, str]:
    text = str(output or "")
    if case.expected == "missing_profile":
        ok = "姓名" in text and "出生年月日" in text
        return ok, "应提示补齐姓名+出生年月日"

    if case.expected == "fortune_detail":
        required = ["命理依据：", "行动建议：", "参考置信度："]
        ok = contains_any(text, required, min_hit=3)
        if not ok:
            return False, "命理细节结构不完整（缺少依据/建议/置信度）"
        if profile_name in text or profile_birthdate in text:
            return False, "发生用户资料原文回显"
        return True, "命理结构完整"

    if case.expected == "divination":
        ok = "摇到一卦" in text and contains_any(text, ["凶吉：", "运势："], min_hit=1)
        return ok, "占卜结果结构不完整"

    if case.expected == "general":
        ok = len(text.strip()) > 0 and "Traceback" not in text
        return ok, "通用问答异常或空输出"

    return False, "未知用例类型"


def build_cases() -> list[Case]:
    return [
        Case("FORTUNE-001", "给我算一下今日运势", "missing_profile"),
        Case("GENERAL-001", "我叫测试甲，2002-03-14出生。", "general"),
        Case("FORTUNE-002", "我今天财运如何？", "fortune_detail"),
        Case("FORTUNE-003", "帮我看看最近事业运", "fortune_detail"),
        Case("FORTUNE-004", "我这周感情运的关键点是什么？", "fortune_detail"),
        Case("FORTUNE-005", "最近学业运会不会拖后腿？", "fortune_detail"),
        Case("FORTUNE-006", "给我看下流年走势", "fortune_detail"),
        Case("FORTUNE-007", "今天运势里我适合冲刺还是稳住？", "fortune_detail"),
        Case("FORTUNE-008", "我这个月财运上该先开源还是先守财？", "fortune_detail"),
        Case("FORTUNE-009", "最近事业运里的贵人运怎么样？", "fortune_detail"),
        Case("FORTUNE-010", "请按命理说下我最近一个月财运节奏", "fortune_detail"),
        Case("FORTUNE-011", "我这段时间最该避免什么决策？", "fortune_detail"),
        Case("FORTUNE-012", "现在是适合换岗还是先积累？", "fortune_detail"),
        Case("FORTUNE-013", "我的感情运是在回暖还是降温？", "fortune_detail"),
        Case("FORTUNE-014", "今天最旺的行动方向是什么？", "fortune_detail"),
        Case("FORTUNE-015", "我该在哪个领域更容易提运？", "fortune_detail"),
        Case("DIV-001", "请帮我摇一卦", "divination"),
        Case("DIV-002", "我想占卜一下今天适不适合谈合作", "divination"),
        Case("GENERAL-002", "我最近焦虑，怎么调节睡眠？", "general"),
        Case("GENERAL-003", "给我一个今天能执行的小目标", "general"),
        Case("GENERAL-004", "我和同事沟通总卡壳，怎么办？", "general"),
        Case("GENERAL-005", "如何减少自我怀疑？", "general"),
        Case("FORTUNE-016", "结合命理给我三条本周行动建议", "fortune_detail"),
        Case("FORTUNE-017", "我现在整体运势的风险点是什么？", "fortune_detail"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="吉伊命理专项回归脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="服务地址")
    parser.add_argument("--phone", default="", help="指定手机号；不传则自动生成")
    parser.add_argument("--password", default="abc12345", help="注册密码")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时秒数")
    parser.add_argument("--max-cases", type=int, default=24, help="最多执行的用例数")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    session = requests.Session()
    phone = args.phone.strip() or pick_phone()
    password = args.password.strip()

    send = post_json(
        session,
        f"{base}/auth/send_code",
        {"phone": phone, "scene": "register"},
        timeout=args.timeout,
    )
    if send.status_code != 200:
        print(f"[FATAL] send_code失败: {send.status_code} {send.text}")
        return 1
    try:
        code = str(send.json().get("debug_code") or "")
    except Exception:
        code = ""
    if not re.fullmatch(r"\d{6}", code):
        print(f"[FATAL] 未拿到debug_code: {send.text}")
        return 1

    verify = post_json(
        session,
        f"{base}/auth/verify",
        {"phone": phone, "code": code, "mode": "register", "password": password},
        timeout=args.timeout,
    )
    if verify.status_code != 200:
        print(f"[FATAL] 注册失败: {verify.status_code} {verify.text}")
        return 1

    profile_name = "测试甲"
    profile_birthdate = "2002-03-14"
    cases = build_cases()[: max(1, args.max_cases)]
    passed = 0
    failed = 0

    print(f"[INFO] base={base} phone={phone} total_cases={len(cases)}")
    for case in cases:
        try:
            resp = post_json(session, f"{base}/chat", {"query": case.query}, timeout=args.timeout)
        except Exception as e:
            failed += 1
            print(f"[FAIL] {case.cid} 请求异常: {e}")
            continue
        if resp.status_code != 200:
            failed += 1
            print(f"[FAIL] {case.cid} HTTP={resp.status_code} body={resp.text[:240]}")
            continue
        try:
            data = resp.json()
        except Exception:
            failed += 1
            print(f"[FAIL] {case.cid} 非JSON响应: {resp.text[:240]}")
            continue
        output = str(data.get("output") or "")
        ok, reason = assert_case(case, output, profile_name, profile_birthdate)
        if ok:
            passed += 1
            print(f"[PASS] {case.cid} {reason}")
        else:
            failed += 1
            print(f"[FAIL] {case.cid} {reason} | output={output[:220]}")

    print(f"[SUMMARY] passed={passed} failed={failed} pass_rate={passed / max(1, len(cases)):.2%}")

    try:
        m = session.get(f"{base}/quality/metrics", params={"days": 1}, timeout=args.timeout)
        if m.status_code == 200:
            print(f"[METRICS] {m.text}")
        else:
            print(f"[METRICS] 获取失败 HTTP={m.status_code} {m.text[:200]}")
    except Exception as e:
        print(f"[METRICS] 获取异常: {e}")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
