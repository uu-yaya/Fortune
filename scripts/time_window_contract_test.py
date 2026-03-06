#!/usr/bin/env python3
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server


def test_year_window_text_contains_year() -> None:
    anchor = server.build_time_anchor()
    meta = server.date_window_resolver("分析一下我今年的运势", anchor)
    text = str(meta.get("window_text") or "")
    assert re.search(r"20\d{2}年", text), f"window_text should contain year, got: {text}"


def test_sanitize_keeps_dates_when_window_allowed() -> None:
    src = "这段建议按3月5日至3月7日执行。"
    out = server._sanitize_time_tokens_before_validation(
        src,
        "我今天整体运势最该注意什么？",
        {"label": "near_days", "window_text": "3月5日至3月7日"},
    )
    assert "3月5日" in out and "3月7日" in out, f"dates should be preserved when window is allowed, got: {out}"
    assert "这段时间" not in out, f"dates should not be normalized to generic text, got: {out}"


def test_contract_appends_window_and_expected_year() -> None:
    anchor = server.build_time_anchor()
    meta = server.date_window_resolver("分析一下我今年的运势", anchor)
    out = server._ensure_time_window_contract("结论：整体稳中有进。", "分析一下我今年的运势", anchor, meta)
    expected_year = server._expected_year_from_query("分析一下我今年的运势", anchor)
    assert "时间窗口：" in out, f"window line should be appended, got: {out}"
    assert expected_year is not None and str(expected_year) in out, f"expected year should be present, got: {out}"


def main() -> int:
    tests = [
        ("year_window_text_contains_year", test_year_window_text_contains_year),
        ("sanitize_keeps_dates_when_window_allowed", test_sanitize_keeps_dates_when_window_allowed),
        ("contract_appends_window_and_expected_year", test_contract_appends_window_and_expected_year),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {name}: {e}")
    if failed:
        print(f"[SUMMARY] failed={failed} total={len(tests)}")
        return 2
    print(f"[SUMMARY] pass total={len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
