#!/usr/bin/env python3
import argparse
import random
import re

import requests


def pick_phone() -> str:
    return "13" + "".join(str(random.randint(0, 9)) for _ in range(9))


def post_json(session: requests.Session, url: str, payload: dict, timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def output_of(resp: requests.Response) -> str:
    if resp.status_code != 200:
        return ""
    if "application/json" not in resp.headers.get("content-type", ""):
        return ""
    return str(resp.json().get("output") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="姓名三层约束回归测试")
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

    out0 = output_of(post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout))
    if not re.search(r"(不知道|还没有|没记录|告诉我)", out0):
        failed += 1
        print(f"[FAIL] LAYER-0 expected unknown-name prompt | output={out0[:180]}")
    else:
        print("[PASS] LAYER-0 unknown before evidence")

    post_json(session, f"{base}/chat", {"query": "我叫阿明"}, timeout=args.timeout)
    out1 = output_of(post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout))
    if "阿明" not in out1:
        failed += 1
        print(f"[FAIL] LAYER-1 high-confidence name not persisted | output={out1[:180]}")
    else:
        print("[PASS] LAYER-1 high-confidence write persisted")

    post_json(session, f"{base}/chat", {"query": "你可以叫我小明"}, timeout=args.timeout)
    out2 = output_of(post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout))
    if "小明" not in out2 and "阿明" not in out2:
        failed += 1
        print(f"[FAIL] LAYER-2 preferred name not reflected | output={out2[:180]}")
    else:
        print("[PASS] LAYER-2 preferred name reflected")

    post_json(session, f"{base}/chat", {"query": "叫我2026"}, timeout=args.timeout)
    out3 = output_of(post_json(session, f"{base}/chat", {"query": "我叫什么你记得吗"}, timeout=args.timeout))
    if "2026" in out3:
        failed += 1
        print(f"[FAIL] LAYER-3 low-confidence candidate polluted slot | output={out3[:180]}")
    else:
        print("[PASS] LAYER-3 low-confidence candidate blocked")

    if failed:
        print(f"[SUMMARY] failed={failed}")
        return 2
    print("[SUMMARY] pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
