#!/usr/bin/env python3
import argparse
import difflib
import random
import re
from dataclasses import dataclass

import requests


@dataclass
class Case:
    cid: str
    query: str
    expected: str  # missing_profile | fortune_detail | divination | general | clarify | colloquial | decision


def pick_phone() -> str:
    suffix = "".join(str(random.randint(0, 9)) for _ in range(9))
    return f"13{suffix}"


def post_json(session: requests.Session, url: str, payload: dict, timeout: int) -> requests.Response:
    return session.post(url, json=payload, timeout=timeout)


def contains_any(text: str, candidates: list[str], min_hit: int = 1) -> bool:
    hit = sum(1 for c in candidates if c in text)
    return hit >= min_hit


def has_pattern(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, str(text or ""), flags=re.IGNORECASE))


def has_explicit_window(text: str) -> bool:
    out = str(text or "")
    if not out:
        return False
    if re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", out):
        return True
    if re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", out):
        return True
    if re.search(r"(至|到|—|-)", out) and re.search(r"(周|星期|月|日)", out):
        return True
    return False


def first_sentence(text: str) -> str:
    head = re.split(r"[。！？!\n]", str(text or "").strip(), maxsplit=1)[0]
    return head.strip()


def normalize_for_similarity(text: str) -> str:
    out = str(text or "")
    out = re.sub(r"\s+", "", out)
    out = re.sub(r"[，,。.!！？?；;：:\"'（）()【】\[\]—\-~～]", "", out)
    return out[:320]


def max_pair_similarity(outputs: list[str]) -> float:
    if len(outputs) < 2:
        return 0.0
    max_sim = 0.0
    for i in range(len(outputs)):
        for j in range(i + 1, len(outputs)):
            sim = difflib.SequenceMatcher(None, outputs[i], outputs[j]).ratio()
            if sim > max_sim:
                max_sim = sim
    return round(max_sim, 4)


def asks_for_profile(text: str) -> bool:
    out = str(text or "")
    if not out.strip():
        return False
    if "资料补齐" in out:
        return True
    patterns = [
        r"(请|先)?告诉我.*(姓名|名字|出生|生日|出生年月日|出生日期|时辰|生辰)",
        r"(请|先)?提供.*(姓名|名字|出生|生日|出生年月日|出生日期|时辰|生辰)",
        r"(还需要|我还需要|需要你补充|请补充).*(姓名|名字|出生|生日|出生年月日|出生日期|时辰|生辰)",
    ]
    return any(has_pattern(out, p) for p in patterns)


def has_action_guidance(text: str) -> bool:
    return contains_any(
        str(text or ""),
        ["建议", "可执行", "行动", "第一步", "先", "宜", "不宜", "避免", "怎么做", "安排"],
        min_hit=1,
    )


def first_n_sentences(text: str, n: int = 2) -> str:
    out = str(text or "").strip()
    if not out:
        return ""
    parts = [p.strip() for p in re.split(r"[。！？!\n]+", out) if p.strip()]
    return "。".join(parts[: max(1, n)])


def assert_case(case: Case, output: str, profile_name: str, profile_birthdate: str) -> tuple[bool, str]:
    text = str(output or "")
    if case.expected == "missing_profile":
        if asks_for_profile(text):
            return True, "资料补齐提示命中"
        return False, "应提示补齐姓名或出生信息"

    if case.expected == "fortune_detail":
        if profile_name in text or profile_birthdate in text:
            return False, "发生用户资料原文回显"
        if asks_for_profile(text):
            return False, "已有资料后仍触发资料补齐"
        if len(text.strip()) < 40:
            return False, "命理解读过短"
        has_analysis_signal = contains_any(
            text,
            ["命理", "运势", "财运", "事业", "感情", "学业", "流年", "八字", "日主", "五行", "时间窗口", "近期", "本周", "下周"],
            min_hit=1,
        )
        if not has_analysis_signal:
            return False, "命理语义信号不足"
        if not has_action_guidance(text):
            return False, "缺少行动导向建议"
        return True, "命理解读语义完整"

    if case.expected == "clarify":
        has_sign_request = bool(re.search(r"(告诉我你的星座|先告诉我.*星座|你是哪个星座|什么星座)", text))
        return has_sign_request, "应进入星座缺信息澄清"

    if case.expected == "colloquial":
        if not has_explicit_window(text):
            return False, "口语时窗未命中（缺少明确时间窗口）"
        return True, "口语时窗命中"

    if case.expected == "decision":
        if asks_for_profile(text):
            return True, "决策问题进入资料补齐"
        head = first_n_sentences(text, n=2)
        if not head:
            return False, "决策回答为空"
        direct = has_pattern(
            head,
            r"(结论|优先|建议|更适合|更稳|更好|宜|不宜|可以|不该|不建议|应该|先.*再|守财|开源|控支出|继续|体面收尾|联系)",
        )
        if not direct:
            return False, "决策前两句未给出明确取舍"
        return True, "决策取舍命中"

    if case.expected == "divination":
        has_divination_signal = contains_any(text, ["卦", "摇卦", "抽签", "占卜", "签文"], min_hit=1) or contains_any(
            case.query, ["卦", "摇卦", "抽签", "占卜", "签"], min_hit=1
        )
        has_outcome_signal = contains_any(
            text,
            ["吉", "凶", "大吉", "小吉", "平", "宜", "不宜", "适合", "不适合", "结论", "建议", "不建议"],
            min_hit=1,
        )
        if not (has_divination_signal and has_outcome_signal):
            return False, "占卜语义不完整"
        return True, "占卜语义命中"

    if case.expected == "general":
        ok = len(text.strip()) > 0 and "Traceback" not in text
        return ok, "通用问答异常或空输出"

    return False, "未知用例类型"


def build_cases() -> list[Case]:
    return [
        Case("FORTUNE-001", "给我算一下今日运势", "missing_profile"),
        Case("GENERAL-001", "我叫测试甲，2002-03-14出生。", "general"),
        Case("CLARIFY-001", "帮我看一下星座运势", "clarify"),
        Case("COLLOQUIAL-001", "我近哪几天气场更顺？", "colloquial"),
        Case("DECISION-001", "我这个月财运上该先开源还是先守财？", "decision"),
        Case("FORTUNE-002", "我今天财运如何？", "fortune_detail"),
        Case("FORTUNE-003", "帮我看看最近事业运", "fortune_detail"),
        Case("FORTUNE-004", "我这周感情运的关键点是什么？", "fortune_detail"),
        Case("FORTUNE-005", "最近学业运会不会拖后腿？", "fortune_detail"),
        Case("FORTUNE-006", "给我看下流年走势", "fortune_detail"),
        Case("FORTUNE-007", "今天运势里我适合冲刺还是稳住？", "fortune_detail"),
        Case("FORTUNE-008", "最近事业运里的贵人运怎么样？", "fortune_detail"),
        Case("FORTUNE-009", "最近财运里的风险点是什么？", "fortune_detail"),
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
    parser.add_argument("--min-unique-output-rate", type=float, default=0.45, help="最小输出唯一率")
    parser.add_argument("--max-pair-similarity", type=float, default=0.92, help="最大两两相似度")
    parser.add_argument("--max-first-sentence-repeat-rate", type=float, default=0.60, help="首句重复率上限")
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
    outputs: list[str] = []
    first_sentences: list[str] = []

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
        outputs.append(output)
        head = first_sentence(output)
        if head:
            first_sentences.append(head)
        ok, reason = assert_case(case, output, profile_name, profile_birthdate)
        if ok:
            passed += 1
            print(f"[PASS] {case.cid} {reason}")
        else:
            failed += 1
            print(f"[FAIL] {case.cid} {reason} | output={output[:220]}")

    normalized_outputs = []
    for item in outputs:
        normalized = normalize_for_similarity(item)
        if normalized:
            normalized_outputs.append(normalized)
    unique_output_rate = len(set(normalized_outputs)) / max(1, len(normalized_outputs))
    pair_similarity = max_pair_similarity(normalized_outputs)
    first_sentence_repeat_rate = 1.0 - (len(set(first_sentences)) / max(1, len(first_sentences)))
    anti_template_fail = False
    if unique_output_rate < args.min_unique_output_rate:
        anti_template_fail = True
        print(
            f"[FAIL] anti_template.unique_output_rate actual={unique_output_rate:.4f} < target={args.min_unique_output_rate:.4f}"
        )
    else:
        print(
            f"[PASS] anti_template.unique_output_rate actual={unique_output_rate:.4f} >= target={args.min_unique_output_rate:.4f}"
        )
    if pair_similarity > args.max_pair_similarity:
        anti_template_fail = True
        print(f"[FAIL] anti_template.max_pair_similarity actual={pair_similarity:.4f} > target={args.max_pair_similarity:.4f}")
    else:
        print(f"[PASS] anti_template.max_pair_similarity actual={pair_similarity:.4f} <= target={args.max_pair_similarity:.4f}")
    if first_sentence_repeat_rate > args.max_first_sentence_repeat_rate:
        anti_template_fail = True
        print(
            f"[FAIL] anti_template.first_sentence_repeat_rate actual={first_sentence_repeat_rate:.4f} > target={args.max_first_sentence_repeat_rate:.4f}"
        )
    else:
        print(
            f"[PASS] anti_template.first_sentence_repeat_rate actual={first_sentence_repeat_rate:.4f} <= target={args.max_first_sentence_repeat_rate:.4f}"
        )
    print(
        f"[SUMMARY] passed={passed} failed={failed} pass_rate={passed / max(1, len(cases)):.2%} "
        f"unique_output_rate={unique_output_rate:.2%} max_pair_similarity={pair_similarity:.4f} "
        f"first_sentence_repeat_rate={first_sentence_repeat_rate:.2%}"
    )

    try:
        m = session.get(f"{base}/quality/metrics", params={"days": 1}, timeout=args.timeout)
        if m.status_code == 200:
            print(f"[METRICS] {m.text}")
        else:
            print(f"[METRICS] 获取失败 HTTP={m.status_code} {m.text[:200]}")
    except Exception as e:
        print(f"[METRICS] 获取异常: {e}")

    return 0 if (failed == 0 and not anti_template_fail) else 2


if __name__ == "__main__":
    raise SystemExit(main())
