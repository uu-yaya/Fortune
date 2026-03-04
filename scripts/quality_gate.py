#!/usr/bin/env python3
import argparse
import os

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
    parser.add_argument("--max-template-signature-rate", type=float, default=0.20, help="模板签名率上限")
    parser.add_argument("--max-session-repeat-rate", type=float, default=0.15, help="同会话重复率上限")
    parser.add_argument("--max-blueprint-repeat-rate", type=float, default=0.70, help="蓝图重复率上限")
    parser.add_argument("--max-advice-repeat-rate", type=float, default=0.75, help="建议重复率上限")
    parser.add_argument("--min-unique-output-rate", type=float, default=0.45, help="输出唯一率下限")
    parser.add_argument("--max-pair-similarity", type=float, default=0.92, help="最大两两相似度上限")
    parser.add_argument("--min-direct-answer-hit-rate", type=float, default=0.95, help="决策直答命中率下限")
    parser.add_argument("--min-clarify-hit-rate", type=float, default=0.95, help="澄清命中率下限")
    parser.add_argument("--min-trend-window-hit-rate", type=float, default=0.90, help="趋势窗口命中率下限")
    parser.add_argument("--min-colloquial-window-hit-rate", type=float, default=0.90, help="口语时窗命中率下限")
    parser.add_argument("--min-temporal-consistency-hit-rate", type=float, default=0.99, help="时序一致性命中率下限")
    parser.add_argument("--max-weekday-mismatch-count", type=float, default=0.0, help="日期星期错配数上限")
    parser.add_argument("--min-observability-coverage", type=float, default=0.99, help="观测日志覆盖率下限")
    parser.add_argument("--max-time-guard-overwrite-rate", type=float, default=0.02, help="时间守卫覆盖业务回答率上限")
    parser.add_argument("--max-name-slot-pollution-rate", type=float, default=0.005, help="姓名槽位污染率上限")
    parser.add_argument("--min-name-write-high-confidence-rate", type=float, default=0.95, help="姓名高置信写入率下限")
    parser.add_argument("--max-long-horizon-shrink-rate", type=float, default=0.01, help="长周期收缩到三天比率上限")
    parser.add_argument("--max-fact-hallucination-rate", type=float, default=0.005, help="事实问答幻觉率上限")
    parser.add_argument("--strict", action="store_true", help="严格模式：样本不足也执行并判定失败")
    parser.add_argument(
        "--min-output-samples-for-diversity",
        type=int,
        default=12,
        help="多样性类指标启用的最小 output_total 样本数（默认12）",
    )
    parser.add_argument("--legacy-metrics", action="store_true", help="使用旧版模板指标门禁")
    return parser.parse_args()


def _to_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _sample_count(totals: dict, key: str) -> int:
    if not isinstance(totals, dict):
        return 0
    try:
        return int(float(totals.get(key, 0)))
    except Exception:
        return 0


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
    template_signature = _to_float(rates.get("template_signature_rate"))
    session_repeat = _to_float(rates.get("session_repeat_rate"))
    blueprint_repeat = _to_float(rates.get("blueprint_repeat_rate"))
    advice_repeat = _to_float(rates.get("advice_repeat_rate"))
    unique_output = _to_float(rates.get("unique_output_rate"))
    max_pair_similarity = _to_float(rates.get("max_pair_similarity"))
    direct_answer = _to_float(rates.get("direct_answer_hit_rate"))
    clarify_hit = _to_float(rates.get("clarify_hit_rate"))
    trend_window = _to_float(rates.get("trend_window_hit_rate"))
    colloquial_window = _to_float(rates.get("colloquial_window_hit_rate"))
    temporal_hit = _to_float(rates.get("temporal_consistency_hit_rate"))
    observability = _to_float(rates.get("observability_coverage"))
    time_guard_overwrite = _to_float(rates.get("time_guard_overwrite_rate"))
    name_slot_pollution = _to_float(rates.get("name_slot_pollution_rate"))
    name_write_high_conf = _to_float(rates.get("name_write_high_confidence_rate"))
    long_horizon_shrink = _to_float(rates.get("long_horizon_shrink_rate"))
    fact_hallucination = _to_float(rates.get("fact_hallucination_rate"))
    totals = data.get("totals") if isinstance(data, dict) else {}
    weekday_mismatch_count = _to_float((totals or {}).get("weekday_mismatch_count"))
    legacy_mode = args.legacy_metrics or str(os.getenv("QUALITY_GATE_LEGACY", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    checks: list[dict] = [
        {
            "name": "fortune_route_hit_rate",
            "actual": route_hit,
            "op": ">=",
            "target": args.min_route_hit_rate,
            "ok": route_hit >= args.min_route_hit_rate,
            "denominator_key": "fortune_intent_total",
        },
        {
            "name": "fortune_tool_success_rate",
            "actual": tool_success,
            "op": ">=",
            "target": args.min_tool_success_rate,
            "ok": tool_success >= args.min_tool_success_rate,
            "denominator_key": "fortune_tool_total",
        },
        {
            "name": "fortune_field_completeness_rate",
            "actual": field_complete,
            "op": ">=",
            "target": args.min_field_completeness_rate,
            "ok": field_complete >= args.min_field_completeness_rate,
            "denominator_key": "fortune_field_total",
        },
        {
            "name": "profile_echo_violation_rate",
            "actual": profile_echo,
            "op": "<=",
            "target": args.max_profile_echo_violation_rate,
            "ok": profile_echo <= args.max_profile_echo_violation_rate,
            "denominator_key": "profile_echo_total",
        },
        {
            "name": "direct_answer_hit_rate",
            "actual": direct_answer,
            "op": ">=",
            "target": args.min_direct_answer_hit_rate,
            "ok": direct_answer >= args.min_direct_answer_hit_rate,
            "denominator_key": "direct_answer_total",
        },
        {
            "name": "clarify_hit_rate",
            "actual": clarify_hit,
            "op": ">=",
            "target": args.min_clarify_hit_rate,
            "ok": clarify_hit >= args.min_clarify_hit_rate,
            "denominator_key": "clarify_total",
        },
        {
            "name": "trend_window_hit_rate",
            "actual": trend_window,
            "op": ">=",
            "target": args.min_trend_window_hit_rate,
            "ok": trend_window >= args.min_trend_window_hit_rate,
            "denominator_key": "trend_window_total",
        },
        {
            "name": "colloquial_window_hit_rate",
            "actual": colloquial_window,
            "op": ">=",
            "target": args.min_colloquial_window_hit_rate,
            "ok": colloquial_window >= args.min_colloquial_window_hit_rate,
            "denominator_key": "colloquial_window_total",
        },
        {
            "name": "temporal_consistency_hit_rate",
            "actual": temporal_hit,
            "op": ">=",
            "target": args.min_temporal_consistency_hit_rate,
            "ok": temporal_hit >= args.min_temporal_consistency_hit_rate,
            "denominator_key": "temporal_consistency_total",
        },
        {
            "name": "observability_coverage",
            "actual": observability,
            "op": ">=",
            "target": args.min_observability_coverage,
            "ok": observability >= args.min_observability_coverage,
            "denominator_key": "observability_total",
        },
        {
            "name": "time_guard_overwrite_rate",
            "actual": time_guard_overwrite,
            "op": "<=",
            "target": args.max_time_guard_overwrite_rate,
            "ok": time_guard_overwrite <= args.max_time_guard_overwrite_rate,
            "denominator_key": "time_guard_total",
        },
        {
            "name": "name_slot_pollution_rate",
            "actual": name_slot_pollution,
            "op": "<=",
            "target": args.max_name_slot_pollution_rate,
            "ok": name_slot_pollution <= args.max_name_slot_pollution_rate,
            "denominator_key": "name_slot_total",
        },
        {
            "name": "name_write_high_confidence_rate",
            "actual": name_write_high_conf,
            "op": ">=",
            "target": args.min_name_write_high_confidence_rate,
            "ok": name_write_high_conf >= args.min_name_write_high_confidence_rate,
            "denominator_key": "name_write_total",
        },
        {
            "name": "long_horizon_shrink_rate",
            "actual": long_horizon_shrink,
            "op": "<=",
            "target": args.max_long_horizon_shrink_rate,
            "ok": long_horizon_shrink <= args.max_long_horizon_shrink_rate,
            "denominator_key": "long_horizon_total",
        },
        {
            "name": "fact_hallucination_rate",
            "actual": fact_hallucination,
            "op": "<=",
            "target": args.max_fact_hallucination_rate,
            "ok": fact_hallucination <= args.max_fact_hallucination_rate,
            "denominator_key": "fact_check_total",
        },
        {
            "name": "weekday_mismatch_count",
            "actual": weekday_mismatch_count,
            "op": "<=",
            "target": args.max_weekday_mismatch_count,
            "ok": weekday_mismatch_count <= args.max_weekday_mismatch_count,
            "denominator_key": "temporal_consistency_total",
        },
    ]
    if legacy_mode:
        checks.extend(
            [
                {
                    "name": "template_repeat_rate",
                    "actual": template_repeat,
                    "op": "<=",
                    "target": args.max_template_repeat_rate,
                    "ok": template_repeat <= args.max_template_repeat_rate,
                    "denominator_key": "template_repeat_total",
                },
                {
                    "name": "template_signature_rate",
                    "actual": template_signature,
                    "op": "<=",
                    "target": args.max_template_signature_rate,
                    "ok": template_signature <= args.max_template_signature_rate,
                    "denominator_key": "template_signature_total",
                },
                {
                    "name": "session_repeat_rate",
                    "actual": session_repeat,
                    "op": "<=",
                    "target": args.max_session_repeat_rate,
                    "ok": session_repeat <= args.max_session_repeat_rate,
                    "denominator_key": "session_repeat_total",
                    "min_samples": max(2, int(args.min_output_samples_for_diversity)),
                },
            ]
        )
    else:
        checks.extend(
            [
                {
                    "name": "blueprint_repeat_rate",
                    "actual": blueprint_repeat,
                    "op": "<=",
                    "target": args.max_blueprint_repeat_rate,
                    "ok": blueprint_repeat <= args.max_blueprint_repeat_rate,
                    "denominator_key": "blueprint_total",
                    "min_samples": int(args.min_output_samples_for_diversity),
                },
                {
                    "name": "advice_repeat_rate",
                    "actual": advice_repeat,
                    "op": "<=",
                    "target": args.max_advice_repeat_rate,
                    "ok": advice_repeat <= args.max_advice_repeat_rate,
                    "denominator_key": "advice_total",
                    "min_samples": int(args.min_output_samples_for_diversity),
                },
                {
                    "name": "unique_output_rate",
                    "actual": unique_output,
                    "op": ">=",
                    "target": args.min_unique_output_rate,
                    "ok": unique_output >= args.min_unique_output_rate,
                    "denominator_key": "output_total",
                    "min_samples": int(args.min_output_samples_for_diversity),
                },
                {
                    "name": "max_pair_similarity",
                    "actual": max_pair_similarity,
                    "op": "<=",
                    "target": args.max_pair_similarity,
                    "ok": max_pair_similarity <= args.max_pair_similarity,
                    "denominator_key": "output_total",
                    "min_samples": max(2, int(args.min_output_samples_for_diversity)),
                },
            ]
        )

    failed = 0
    skipped = 0
    print("[METRICS] rates =", rates)
    print(f"[MODE] legacy_metrics={'ON' if legacy_mode else 'OFF'}")
    print(
        f"[MODE] strict={'ON' if args.strict else 'OFF'} "
        f"min_output_samples_for_diversity={int(args.min_output_samples_for_diversity)}"
    )

    for item in checks:
        name = str(item.get("name") or "")
        actual = _to_float(item.get("actual"))
        op = str(item.get("op") or "")
        target = _to_float(item.get("target"))
        ok = bool(item.get("ok"))
        denominator_key = str(item.get("denominator_key") or "")
        min_samples = int(item.get("min_samples") or 1)
        denominator_value = _sample_count(totals, denominator_key) if denominator_key else 0

        if (not args.strict) and denominator_key and denominator_value < max(1, min_samples):
            print(
                f"[SKIP] {name}: sample={denominator_value} from {denominator_key} "
                f"< min_required={max(1, min_samples)}"
            )
            skipped += 1
            continue

        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: actual={actual:.4f} {op} target={target:.4f}")
        if not ok:
            failed += 1

    if failed == 0 and skipped == len(checks):
        print("[SUMMARY] 质量门禁跳过（当前样本不足），按通过处理。")
        return 0

    if failed:
        print(f"[SUMMARY] 质量门禁未通过，失败项={failed} 跳过项={skipped}")
        return 2

    print(f"[SUMMARY] 质量门禁通过（跳过项={skipped}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
