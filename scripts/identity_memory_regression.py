#!/usr/bin/env python3
import argparse
import random
import re

import requests


def pick_phone() -> str:
    return "13" + "".join(str(random.randint(0, 9)) for _ in range(9))


def post_json(session: requests.Session, url: str, payload: dict, timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description="身份记忆防污染回归脚本")
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

    failed = 0

    r1 = post_json(session, f"{base}/chat", {"query": "今年是多少年"}, timeout=args.timeout)
    if r1.status_code != 200:
        print(f"[FAIL] STEP-1 http={r1.status_code}")
        return 2

    r2 = post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout)
    out2 = str((r2.json() if r2.status_code == 200 else {}).get("output") or "")
    if r2.status_code != 200:
        failed += 1
        print(f"[FAIL] STEP-2 http={r2.status_code}")
    elif re.search(r"(你叫|你是)\s*[^\s，。！？,.]{2,12}", out2) and not re.search(r"(不知道|还没有|没记录|告诉我)", out2):
        failed += 1
        print(f"[FAIL] STEP-2 fabricated name | output={out2[:180]}")
    else:
        print("[PASS] STEP-2 no fabrication before evidence")

    r3 = post_json(session, f"{base}/chat", {"query": "我叫小雨"}, timeout=args.timeout)
    if r3.status_code != 200:
        failed += 1
        print(f"[FAIL] STEP-3 http={r3.status_code}")
    else:
        print("[PASS] STEP-3 set legal name")

    r4 = post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout)
    out4 = str((r4.json() if r4.status_code == 200 else {}).get("output") or "")
    if r4.status_code != 200:
        failed += 1
        print(f"[FAIL] STEP-4 http={r4.status_code}")
    elif "小雨" not in out4:
        failed += 1
        print(f"[FAIL] STEP-4 expected known name not found | output={out4[:180]}")
    else:
        print("[PASS] STEP-4 stable hit after evidence")

    r5 = post_json(session, f"{base}/chat", {"query": "你叫今年是多少年吗"}, timeout=args.timeout)
    if r5.status_code != 200:
        failed += 1
        print(f"[FAIL] STEP-5 http={r5.status_code}")

    r6 = post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout)
    out6 = str((r6.json() if r6.status_code == 200 else {}).get("output") or "")
    if r6.status_code != 200:
        failed += 1
        print(f"[FAIL] STEP-6 http={r6.status_code}")
    elif "小雨" not in out6:
        failed += 1
        print(f"[FAIL] STEP-6 name polluted by question sentence | output={out6[:180]}")
    else:
        print("[PASS] STEP-6 question sentence did not pollute stored name")

    if failed:
        print(f"[SUMMARY] failed={failed}")
        return 2
    print("[SUMMARY] pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
