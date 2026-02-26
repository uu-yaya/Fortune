#!/usr/bin/env python3
import argparse

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="命理质量门禁检查")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="服务地址")
    parser.add_argument("--days", type=int, default=1, help="统计最近N天，范围1-7")
    parser.add_argument("--timeout", type=int, default=30, help="请求超时秒数")
    parser.add_argument("--min-route-hit-rate", type=float, default=0.90, help="命理路由命中率下限")
    parser.add_argument("--min-tool-success-rate", type=float, default=0.90, help="命理工具成功率下限")
    parser.add_argument("--min-field-completeness-rate", type=float, default=0.85, help="命理字段完整率下限")
    parser.add_argument("--max-profile-echo-violation-rate", type=float, default=0.0, help="资料回显违规率上限")
    parser.add_argument("--max-template-repeat-rate", type=float, default=0.20, help="模板重复率上限")
    return parser.parse_args()


def _to_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def main() -> int:
    args = parse_args()
    base = args.base_url.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/quality/metrics",
            params={"days": max(1, min(args.days, 7))},
            timeout=args.timeout,
        )
    except Exception as e:
        print(f"[FATAL] 请求质量指标失败: {e}")
        return 1

    if resp.status_code != 200:
        print(f"[FATAL] 获取质量指标失败 HTTP={resp.status_code} body={resp.text[:240]}")
        return 1

    try:
        payload = resp.json()
    except Exception:
        print(f"[FATAL] 质量指标返回非JSON: {resp.text[:240]}")
        return 1

    data = payload.get("data") if isinstance(payload, dict) else None
    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict):
        print(f"[FATAL] 指标结构不正确: {payload}")
        return 1

    route_hit = _to_float(rates.get("fortune_route_hit_rate"))
    tool_success = _to_float(rates.get("fortune_tool_success_rate"))
    field_complete = _to_float(rates.get("fortune_field_completeness_rate"))
    profile_echo = _to_float(rates.get("profile_echo_violation_rate"))
    template_repeat = _to_float(rates.get("template_repeat_rate"))

    checks = [
        ("fortune_route_hit_rate", route_hit, ">=", args.min_route_hit_rate, route_hit >= args.min_route_hit_rate),
        ("fortune_tool_success_rate", tool_success, ">=", args.min_tool_success_rate, tool_success >= args.min_tool_success_rate),
        (
            "fortune_field_completeness_rate",
            field_complete,
            ">=",
            args.min_field_completeness_rate,
            field_complete >= args.min_field_completeness_rate,
        ),
        (
            "profile_echo_violation_rate",
            profile_echo,
            "<=",
            args.max_profile_echo_violation_rate,
            profile_echo <= args.max_profile_echo_violation_rate,
        ),
        (
            "template_repeat_rate",
            template_repeat,
            "<=",
            args.max_template_repeat_rate,
            template_repeat <= args.max_template_repeat_rate,
        ),
    ]

    failed = 0
    print("[METRICS] rates =", rates)
    for name, actual, op, target, ok in checks:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: actual={actual:.4f} {op} target={target:.4f}")
        if not ok:
            failed += 1

    if failed:
        print(f"[SUMMARY] 质量门禁未通过，失败项={failed}")
        return 2

    print("[SUMMARY] 质量门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
