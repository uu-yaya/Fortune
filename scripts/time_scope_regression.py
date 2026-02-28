#!/usr/bin/env python3
import argparse
import random
import re
from dataclasses import dataclass

import requests


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
    if re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", out) and re.search(r"(至|到|—|-)", out):
        return True
    return False


def has_shrink_words(text: str) -> bool:
    out = str(text or "")
    return bool(re.search(r"(这三天|最近三天|未来三天|2月27日到3月1日)", out))


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

    cases = [
        Case("TS-001", "我今天整体运势最该注意什么？", long_horizon=False),
        Case("TS-002", "我这周整体运势最该注意什么？", long_horizon=False),
        Case("TS-003", "我接下来一个月的主题是什么？", long_horizon=True),
        Case("TS-004", "分析一下我今年的运势", long_horizon=True, must_have=("2026",)),
        Case("TS-005", "今年下半年财运", long_horizon=True),
        Case("TS-006", "今年和明年财运对比", long_horizon=True, must_have=("2026", "2027")),
        Case("TS-007", "未来三年运势", long_horizon=True),
        Case("TS-008", "2027和2028财运对比", long_horizon=True, must_have=("2027", "2028")),
    ]

    failed = 0
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
        print(f"[PASS] {case.cid}")

    if failed:
        print(f"[SUMMARY] failed={failed} total={len(cases)}")
        return 2
    print(f"[SUMMARY] pass total={len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
