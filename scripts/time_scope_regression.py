#!/usr/bin/env python3
import argparse
import random
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server


@dataclass
class Case:
    cid: str
    query: str
    long_horizon: bool = False
    must_have: tuple[str, ...] = ()


def pick_phone() -> str:
    suffix = "".join(str(random.randint(0, 9)) for _ in range(9))
    return f"13{suffix}"


def post_json(session: requests.Session, url: str, payload: dict, timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def has_explicit_window(text: str) -> bool:
    out = str(text or "")
    if re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", out):
        return True
    if re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", out):
        return True
    return False


def has_shrink_words(text: str) -> bool:
    out = str(text or "")
    return bool(re.search(r"(这三天|最近三天|未来三天|2月27日到3月1日)", out))


def has_stale_time_window_year(text: str, anchor_year: int) -> tuple[bool, list[int]]:
    out = str(text or "")
    stale_years: list[int] = []
    for m in re.finditer(r"(20\d{2})年", out):
        year = int(m.group(1))
        # 忽略明显的人物资料语境（出生年/生日），避免把 2001 误判为“旧时间窗口”
        ctx = out[max(0, m.start() - 12): m.end() + 12]
        if re.search(r"(出生|生于|生日|生在|命盘|年柱)", ctx):
            continue
        # 仅拦截接近当前年的“陈旧年份”，核心是抓到 2024/2025 这类回流
        if (anchor_year - 3) <= year < anchor_year:
            stale_years.append(year)
    return (len(stale_years) > 0), stale_years


def run_provider_timeout_contract_case() -> tuple[bool, str]:
    payload = {
        "topic": "daily",
        "strength": "balanced",
        "fortune_signals": {"love": "", "wealth": "", "career": "近期更适合先稳节奏。"},
        "advice": ["先稳住一个关键动作。", "把节奏拆成可执行的小步。"],
        "confidence": 0.2,
        "error": {
            "code": "FORTUNE_TIMEOUT",
            "message": "命理服务超时，请稍后重试",
            "provider": "yuanfenju",
            "provider_code": "YUANFENJU_TIMEOUT",
            "category": "timeout",
            "degraded": True,
        },
    }
    window_meta = {"window_text": "2026年3月7日至2026年3月9日", "label": "near_days"}
    output = server._build_fortune_provider_safe_fallback(
        payload,
        "daily",
        query="我今天整体运势最该注意什么？",
        question_type="default",
        time_anchor=server.build_time_anchor(),
        window_meta=window_meta,
        session_id="ts-provider-timeout",
    )
    output = server.validate_time_consistency(output, "我今天整体运势最该注意什么？", server.build_time_anchor(), window_meta=window_meta)
    ok = "时间窗口" in output and has_explicit_window(output) and "建议" in output
    return ok, output


def main() -> int:
    parser = argparse.ArgumentParser(description="时间范围回归脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    session = requests.Session()
    base = args.base_url.rstrip("/")
    phone = pick_phone()

    send = post_json(session, f"{base}/auth/send_code", {"phone": phone, "scene": "register"}, timeout=args.timeout)
    code = str((send.json() if send.status_code == 200 else {}).get("debug_code") or "")
    if not re.fullmatch(r"\d{6}", code):
        print(f"[FATAL] send_code failed: {send.status_code} {send.text[:200]}")
        return 1
    verify = post_json(
        session,
        f"{base}/auth/verify",
        {"phone": phone, "code": code, "mode": "register", "password": "abc12345"},
        timeout=args.timeout,
    )
    if verify.status_code != 200:
        print(f"[FATAL] verify failed: {verify.status_code} {verify.text[:200]}")
        return 1

    init = post_json(session, f"{base}/chat", {"query": "我叫时窗测试，2001-01-01出生"}, timeout=args.timeout)
    if init.status_code != 200:
        print(f"[FATAL] init profile failed: {init.status_code} {init.text[:200]}")
        return 1
    init_gender = post_json(session, f"{base}/chat", {"query": "我是男生"}, timeout=args.timeout)
    if init_gender.status_code != 200:
        print(f"[FATAL] init gender failed: {init_gender.status_code} {init_gender.text[:200]}")
        return 1

    cases = [
        Case("TS-001", "我今天整体运势最该注意什么？", long_horizon=False),
        Case("TS-002", "我这周整体运势最该注意什么？", long_horizon=False),
        Case("TS-003", "我接下来一个月的主题是什么？", long_horizon=True),
        Case("TS-004", "分析一下我今年的运势", long_horizon=True, must_have=("2026",)),
        Case("TS-005", "今年下半年财运", long_horizon=True),
        Case("TS-006", "今年和明年财运对比", long_horizon=True, must_have=("2026", "2027")),
        Case("TS-007", "未来三年运势", long_horizon=True),
        Case("TS-008", "2027和2028财运对比", long_horizon=True, must_have=("2027", "2028")),
        # Patch regressions: relative window queries should not fall back to stale years.
        Case("TS-PATCH-001", "我接下来一个月的主题是什么？", long_horizon=True),
        Case("TS-PATCH-002", "未来30天我该做什么？", long_horizon=True),
        Case("TS-PATCH-003", "接下来1个月我的财运重点是什么？", long_horizon=True),
        Case("TS-PATCH-004", "未来一个月我在工作上先做哪件事最稳？", long_horizon=True),
        Case("TS-PATCH-005", "接下来30天我在感情上该注意什么？", long_horizon=True),
    ]

    failed = 0
    anchor_year = datetime.now().year
    for case in cases:
        resp = post_json(session, f"{base}/chat", {"query": case.query}, timeout=args.timeout)
        if resp.status_code != 200:
            failed += 1
            print(f"[FAIL] {case.cid} HTTP={resp.status_code} body={resp.text[:160]}")
            continue
        output = str((resp.json() if "application/json" in resp.headers.get("content-type", "") else {}).get("output") or "")
        if not output:
            failed += 1
            print(f"[FAIL] {case.cid} empty output")
            continue
        if case.long_horizon and has_shrink_words(output):
            failed += 1
            print(f"[FAIL] {case.cid} long horizon shrank to 3 days | output={output[:180]}")
            continue
        if not case.long_horizon and not has_explicit_window(output):
            failed += 1
            print(f"[FAIL] {case.cid} missing explicit short window | output={output[:180]}")
            continue
        missing = [token for token in case.must_have if token not in output]
        if missing:
            failed += 1
            print(f"[FAIL] {case.cid} missing tokens={missing} | output={output[:180]}")
            continue
        if case.cid.startswith("TS-PATCH-"):
            bad, stale_years = has_stale_time_window_year(output, anchor_year)
            if bad:
                failed += 1
                print(
                    f"[FAIL] {case.cid} stale_year_detected years={stale_years} "
                    f"anchor_year={anchor_year} | output={output[:180]}"
                )
                continue
        print(f"[PASS] {case.cid}")

    provider_ok, provider_output = run_provider_timeout_contract_case()
    if provider_ok:
        print("[PASS] TS-PROV-001")
    else:
        failed += 1
        print(f"[FAIL] TS-PROV-001 provider timeout fallback missing contract | output={provider_output[:180]}")

    if failed:
        print(f"[SUMMARY] failed={failed} total={len(cases) + 1}")
        return 2
    print(f"[SUMMARY] pass total={len(cases) + 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
