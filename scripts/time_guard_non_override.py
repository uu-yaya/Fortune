#!/usr/bin/env python3
import argparse
import random
import re

import requests


def pick_phone() -> str:
    return "13" + "".join(str(random.randint(0, 9)) for _ in range(9))


def post_json(session: requests.Session, url: str, payload: dict, timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def is_time_only_answer(text: str) -> bool:
    out = str(text or "").strip()
    if not out:
        return True
    if "时间对齐" not in out and "时间窗口" not in out:
        return False
    residual = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(r"(时间对齐|时间窗口|UTC|Asia/Shanghai|现在是|窗口按这个范围)", line):
            continue
        residual.append(line)
    return len("".join(residual)) < 20


def has_business_content(text: str) -> bool:
    return bool(re.search(r"(结论|建议|先|避免|适合|不宜|财运|事业运|运势|行动)", str(text or "")))


def main() -> int:
    parser = argparse.ArgumentParser(description="时间守卫不覆盖业务回答回归")
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

    init = post_json(session, f"{base}/chat", {"query": "我叫守卫测试，2003-08-15出生"}, timeout=args.timeout)
    if init.status_code != 200:
        print(f"[FATAL] init profile failed: {init.status_code} {init.text[:200]}")
        return 1

    cases = [
        "分析一下我今年的运势",
        "今年财运",
        "今年事业运",
    ]
    failed = 0
    for idx, query in enumerate(cases, start=1):
        resp = post_json(session, f"{base}/chat", {"query": query}, timeout=args.timeout)
        if resp.status_code != 200:
            failed += 1
            print(f"[FAIL] CASE-{idx} HTTP={resp.status_code} body={resp.text[:180]}")
            continue
        output = str((resp.json() if "application/json" in resp.headers.get("content-type", "") else {}).get("output") or "")
        if is_time_only_answer(output):
            failed += 1
            print(f"[FAIL] CASE-{idx} time-only output | output={output[:180]}")
            continue
        if not has_business_content(output):
            failed += 1
            print(f"[FAIL] CASE-{idx} missing business content | output={output[:180]}")
            continue
        print(f"[PASS] CASE-{idx}")

    if failed:
        print(f"[SUMMARY] failed={failed} total={len(cases)}")
        return 2
    print(f"[SUMMARY] pass total={len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
