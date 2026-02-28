#!/usr/bin/env python3
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server


def _assert_true(name: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    if ok:
        return True, f"[PASS] {name}"
    return False, f"[FAIL] {name} {detail}".strip()


def main() -> int:
    anchor = server.build_time_anchor()
    failed = 0
    results: list[str] = []

    # A1: multi_year window + short-date fix should not rewrite 2/27 -> 2/28
    q_a1 = "未来三年运势"
    w_a1 = server.date_window_resolver(q_a1, anchor)
    raw_a1 = (
        "呀哈～我先把时间对齐：现在是2026年2月28日，星期六（Asia/Shanghai，UTC+08:00）。\\n"
        "你问的时间窗口按这个范围计算：2月28日（星期六）至2月27日（星期二）。"
    )
    out_a1 = server.validate_time_consistency(raw_a1, q_a1, anchor, window_meta=w_a1)
    ok, line = _assert_true(
        "A1.multi_year_window",
        w_a1.get("label") == "multi_year"
        and w_a1.get("window_start") == "2026-02-28"
        and w_a1.get("window_end") == "2029-02-27",
        detail=str(w_a1),
    )
    results.append(line)
    failed += 0 if ok else 1
    ok, line = _assert_true("A1.short_date_not_rewritten", "至2月27日" in out_a1 and "至2月28日（星期二）" not in out_a1, out_a1)
    results.append(line)
    failed += 0 if ok else 1

    # A2: another multi-year query should not produce wrong rewritten end date
    q_a2 = "未来三年财运对比"
    w_a2 = server.date_window_resolver(q_a2, anchor)
    out_a2 = server.validate_time_consistency(raw_a1, q_a2, anchor, window_meta=w_a2)
    ok, line = _assert_true("A2.no_2_28_tuesday_artifact", "至2月28日（星期二）" not in out_a2, out_a2)
    results.append(line)
    failed += 0 if ok else 1

    # B3: severe mismatch keeps business content
    q_b3 = "分析一下我今年的运势"
    w_b3 = server.date_window_resolver(q_b3, anchor)
    raw_b3 = (
        "现在是2035年1月1日，星期一。\\n"
        "时间窗口是2035年1月1日到2035年1月3日。\\n"
        "结论：今年先稳后进，重点管住冲动支出。\\n"
        "建议：把预算上限写进每周计划。"
    )
    out_b3 = server.validate_time_consistency(raw_b3, q_b3, anchor, window_meta=w_b3)
    ok, line = _assert_true(
        "B3.severe_keeps_business",
        ("时间窗口按这个范围计算" in out_b3) and bool(re.search(r"(结论|建议|先|避免|适合|风险)", out_b3)),
        out_b3,
    )
    results.append(line)
    failed += 0 if ok else 1

    # B4: pure-time severe fallback is still allowed
    raw_b4 = "现在是2035年1月1日，星期一。\\n时间窗口是2035年1月1日到2035年1月3日。"
    out_b4 = server.validate_time_consistency(raw_b4, q_b3, anchor, window_meta=w_b3)
    ok, line = _assert_true("B4.pure_time_fallback_allowed", "时间窗口按这个范围计算" in out_b4, out_b4)
    results.append(line)
    failed += 0 if ok else 1

    # B5: literal \\n mixed text still preserves business
    raw_b5 = "现在是2035年1月1日，星期一。\\n时间窗口是2035年1月1日到2035年1月3日。\\n建议：先稳住节奏。"
    out_b5 = server.validate_time_consistency(raw_b5, q_b3, anchor, window_meta=w_b3)
    ok, line = _assert_true("B5.literal_newline_preserve_business", "建议：先稳住节奏" in out_b5 or "建议先稳住节奏" in out_b5, out_b5)
    results.append(line)
    failed += 0 if ok else 1

    # C6: compare-year text contains both years
    q_c6 = "今年和明年财运对比"
    w_c6 = server.date_window_resolver(q_c6, anchor)
    ok, line = _assert_true("C6.window_text_has_2026_2027", "2026年" in w_c6.get("window_text", "") and "2027年" in w_c6.get("window_text", ""), str(w_c6))
    results.append(line)
    failed += 0 if ok else 1

    # C7: explicit compare years text contains both years
    q_c7 = "2027和2028财运对比"
    w_c7 = server.date_window_resolver(q_c7, anchor)
    ok, line = _assert_true("C7.window_text_has_2027_2028", "2027年" in w_c7.get("window_text", "") and "2028年" in w_c7.get("window_text", ""), str(w_c7))
    results.append(line)
    failed += 0 if ok else 1

    # C8: non-cross-year remains short style
    q_c8 = "分析一下我今年的运势"
    w_c8 = server.date_window_resolver(q_c8, anchor)
    ok, line = _assert_true("C8.non_cross_year_short_style", "2026年1月1日" not in w_c8.get("window_text", ""), str(w_c8))
    results.append(line)
    failed += 0 if ok else 1

    # D9: short-window behavior unaffected
    q_d9 = "我今天整体运势最该注意什么"
    w_d9 = server.date_window_resolver(q_d9, anchor)
    ok, line = _assert_true("D9.short_window_intact", w_d9.get("label") in {"near_days", "two_days", "this_week"}, str(w_d9))
    results.append(line)
    failed += 0 if ok else 1

    for row in results:
        print(row)
    if failed:
        print(f"[SUMMARY] failed={failed} total={len(results)}")
        return 2
    print(f"[SUMMARY] pass total={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
