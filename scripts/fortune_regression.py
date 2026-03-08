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
    expected: str  # missing_profile | fortune_detail | divination | dream_detail | general | clarify | colloquial | decision | zodiac_detail
    phase: str = "post_seed"  # pre_seed | post_seed


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
    if "资料齐了我就" in out:
        return True
    patterns = [
        r"(请|先)?告诉我.*(姓名|名字|出生|生日|出生年月日|出生日期|时辰|生辰)",
        r"(请|先)?提供.*(姓名|名字|出生|生日|出生年月日|出生日期|时辰|生辰)",
        r"(还需要|我还需要|需要你补充|请补充).*(姓名|名字|出生|生日|出生年月日|出生日期|时辰|生辰)",
        r"(请|先)?告诉我.*(性别|男/女|男女)",
        r"(还差|缺).{0,12}(小资料|资料|信息).{0,12}(性别|男/女|男女)",
        r"(还需要|我还需要|需要你补充|请补充).{0,12}(性别|男/女|男女)",
    ]
    return any(has_pattern(out, p) for p in patterns)


def has_action_guidance(text: str) -> bool:
    return contains_any(
        str(text or ""),
        ["建议", "可执行", "行动", "第一步", "先", "宜", "不宜", "避免", "怎么做", "安排"],
        min_hit=1,
    )


def is_time_alignment_only(text: str) -> bool:
    out = str(text or "").strip()
    if not out:
        return True
    if "时间对齐" not in out and "时间窗口" not in out:
        return False
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    residual = []
    for line in lines:
        if re.search(r"(时间对齐|时间窗口|UTC|Asia/Shanghai|现在是|窗口按这个范围)", line):
            continue
        residual.append(line)
    return len("".join(residual)) < 18


def has_long_horizon_shrink(text: str) -> bool:
    out = str(text or "")
    return bool(re.search(r"(这三天|最近三天|未来三天|2月27日到3月1日)", out))


def _is_long_horizon_case(query: str) -> bool:
    q = str(query or "")
    return bool(re.search(r"(本月|这个月|今年|明年|后年|全年|年度|未来三年|[一二两三四五六七八九1-9]年内|对比)", q))


def _is_identity_query(query: str) -> bool:
    q = str(query or "")
    return bool(re.search(r"(我叫什么|我是谁|记得我叫|你记得我吗)", q))


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
        if is_time_alignment_only(text):
            return False, "命理回答被时间对齐模板覆盖"
        if len(text.strip()) < 40:
            return False, "命理解读过短"
        if case.cid == "LOVE-001":
            if has_pattern(text, r"(最近三天|接下来这三天|3月8日到3月10日|48小时)"):
                return False, "姻缘趋势被错误收缩成短时窗"
            if not has_action_guidance(text):
                return False, "姻缘趋势缺少行动建议"
            return True, "姻缘趋势时间尺度正常"
        if case.cid == "LOVE-002":
            if not has_pattern(text, r"(男生|女生|他|她)"):
                return False, "正缘画像未按性别给出明确画像"
            if not contains_any(text, ["外在样子", "家庭", "事业财运", "缘分线索", "相处", "运势"], min_hit=3):
                return False, "正缘画像缺少完整画像维度"
            if not has_action_guidance(text):
                return False, "正缘画像缺少行动建议"
            return True, "正缘画像已覆盖完整字段"
        has_analysis_signal = contains_any(
            text,
            ["命理", "运势", "财运", "事业", "感情", "学业", "流年", "八字", "日主", "五行", "时间窗口", "近期", "本周", "下周"],
            min_hit=1,
        )
        if not has_analysis_signal:
            return False, "命理语义信号不足"
        if not has_action_guidance(text):
            return False, "缺少行动导向建议"
        if _is_long_horizon_case(case.query) and has_long_horizon_shrink(text):
            return False, "长周期问题被收缩为三天窗口"
        return True, "命理解读语义完整"

    if case.expected == "clarify":
        has_clarify_request = bool(re.search(r"(告诉我|先告诉我|直接告诉我|请补充).*(出生年月日|出生日期|星座|生肖|属相)", text))
        return has_clarify_request, "应进入资料缺失澄清"

    if case.expected == "colloquial":
        if not has_explicit_window(text):
            return False, "口语时窗未命中（缺少明确时间窗口）"
        if re.search(r"(领证|搬家)", str(case.query or "")) and "更适合" in text and "：凶；" in text:
            return False, "推荐日期中混入凶日"
        if re.search(r"(领证|搬家)", str(case.query or "")):
            if "宜：" in text or "忌：" in text:
                return False, "择时回答仍在原样回显宜忌词表"
            if not re.search(r"(更推荐|理由|适合把|不建议同天|越简单越顺|把重点放在)", text):
                return False, "择时回答缺少用户可理解的建议和理由"
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

    if case.expected == "dream_detail":
        if len(text.strip()) < 20:
            return False, "解梦回答过短"
        if has_pattern(text, r"(工具没收到有效信息|输入格式没对上|线索有点散|没能跑出具体分析)"):
            return False, "解梦仍落在工具失败降级文案"
        has_dream_signal = contains_any(text, ["梦", "寓意", "象征", "提示", "情绪"], min_hit=1)
        if not has_dream_signal:
            return False, "解梦语义不完整"
        return True, "解梦语义命中"

    if case.expected == "zodiac_detail":
        if asks_for_profile(text):
            return False, "已有生日资料后仍触发星座/生肖澄清"
        if len(text.strip()) < 20:
            return False, "星座/生肖回答过短"
        if "可参考这些信号" in text:
            return False, "星座/生肖回答仍是字段平铺"
        if "参考强度" in text or "参考分值" in text:
            return False, "星座/生肖回答仍包含参考强度或参考分值"
        if has_pattern(text, r"(八字|四柱|日主|喜用|忌神|五行|地支|天干)"):
            return False, "星座/生肖回答混入八字术语"
        if not contains_any(text, ["感情：", "事业：", "财运：", "状态：", "幸运提示：", "行动建议："], min_hit=1):
            return False, "星座/生肖回答缺少结构化重点"
        if "本周" in case.query and "本周" not in text and "这周" not in text:
            return False, "本周问法未按本周时间尺度作答"
        if ("今日" in case.query or "今天" in case.query) and "今日" not in text and "今天" not in text:
            return False, "今日问法未按今日时间尺度作答"
        if case.query in {"帮我看一下星座运势", "帮我看生肖运势"} and ("今日" in text and "本周" not in text and "这周" not in text):
            return False, "泛运势问法仍默认收缩到今日"
        has_zodiac_signal = bool(
            re.search(
                r"(白羊座|金牛座|双子座|巨蟹座|狮子座|处女座|天秤座|天蝎座|射手座|摩羯座|水瓶座|双鱼座|"
                r"属[鼠牛虎兔龙蛇马羊猴鸡狗猪])",
                text,
            )
        )
        if not has_zodiac_signal:
            return False, "星座/生肖标识缺失"
        return True, "星座/生肖接口命中"

    if case.expected == "general":
        if _is_identity_query(case.query):
            if re.search(r"(200\d|201\d|202\d)年", text) and "生日" not in text:
                return False, "身份问答出现疑似编造出生细节"
            if profile_name and profile_name not in text:
                return False, "身份问答未返回已知姓名"
            if profile_name and f"{profile_name}叫{profile_name}" in text:
                return False, "身份问答仍是生硬回显句式"
        else:
            if has_pattern(
                text,
                r"(八字|日主|五行|流年|天干|地支|喜用|忌神|四柱|命盘|属[鼠牛虎兔龙蛇马羊猴鸡狗猪]|"
                r"白羊座|金牛座|双子座|巨蟹座|狮子座|处女座|天秤座|天蝎座|射手座|摩羯座|水瓶座|双鱼座)",
            ):
                return False, "通用问答混入命理推演"
        ok = len(text.strip()) > 0 and "Traceback" not in text
        return ok, "通用问答异常或空输出"

    return False, "未知用例类型"


def build_cases() -> list[Case]:
    return [
        Case("FORTUNE-001", "给我算一下今日运势", "missing_profile", phase="pre_seed"),
        Case("CLARIFY-001", "帮我看一下星座运势", "clarify", phase="pre_seed"),
        Case("CLARIFY-002", "帮我看生肖运势", "clarify", phase="pre_seed"),
        Case("TIME-001", "今年是多少年", "general"),
        Case("TREND-001", "分析一下我今年的运势", "fortune_detail"),
        Case("YEAR-001", "明年运势", "fortune_detail"),
        Case("WEALTH-001", "2027年财运如何", "fortune_detail"),
        Case("TREND-002", "今年和明年财运对比", "fortune_detail"),
        Case("TREND-003", "未来三年运势", "fortune_detail"),
        Case("ZODIAC-001", "帮我看一下星座运势", "zodiac_detail"),
        Case("ZODIAC-002", "白羊座本周运势", "zodiac_detail"),
        Case("SHENGXIAO-001", "帮我看生肖运势", "zodiac_detail"),
        Case("SHENGXIAO-002", "属龙今日运势", "zodiac_detail"),
        Case("COLLOQUIAL-001", "我近哪几天气场更顺？", "colloquial"),
        Case("ZESHI-001", "下周哪天适合领证", "colloquial"),
        Case("ZESHI-002", "下周哪天适合搬家", "colloquial"),
        Case("DECISION-001", "我这个月财运上该先开源还是先守财？", "decision"),
        Case("LOVE-001", "我的姻缘趋势", "fortune_detail"),
        Case("LOVE-002", "我的正缘画像是什么样", "fortune_detail"),
        Case("DIV-001", "请帮我摇一卦", "divination"),
        Case("DIV-002", "我想占卜一下今天适不适合谈合作", "divination"),
        Case("DREAM-001", "梦见蛇是什么意思", "dream_detail"),
        Case("FORTUNE-002", "帮我看看最近事业运", "fortune_detail"),
        Case("FORTUNE-003", "我今天财运如何？", "fortune_detail"),
        Case("GENERAL-001", "我最近焦虑，怎么调节睡眠？", "general"),
        Case("GENERAL-002", "给我一个今天能执行的小目标", "general"),
        Case("GENERAL-003", "我叫什么你记得吗", "general"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="吉伊命理专项回归脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="服务地址")
    parser.add_argument("--phone", default="", help="指定手机号；不传则自动生成")
    parser.add_argument("--password", default="abc12345", help="注册密码")
    parser.add_argument("--timeout", type=int, default=60, help="请求超时秒数")
    parser.add_argument("--max-cases", type=int, default=27, help="最多执行的用例数")
    parser.add_argument(
        "--seed-profile-query",
        default="我叫测试甲，2002-03-14出生，我是女生。",
        help="回归预热画像注入语句（不计入用例统计）",
    )
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
    all_cases = build_cases()[: max(1, args.max_cases)]
    pre_seed_cases = [case for case in all_cases if case.phase == "pre_seed"]
    post_seed_cases = [case for case in all_cases if case.phase != "pre_seed"]

    passed = 0
    failed = 0
    outputs: list[str] = []
    first_sentences: list[str] = []

    print(f"[INFO] base={base} phone={phone} total_cases={len(all_cases)}")

    def run_case(case: Case) -> None:
        nonlocal passed, failed
        try:
            resp = post_json(session, f"{base}/chat", {"query": case.query}, timeout=args.timeout)
        except Exception as e:
            failed += 1
            print(f"[FAIL] {case.cid} 请求异常: {e}")
            return
        if resp.status_code != 200:
            failed += 1
            print(f"[FAIL] {case.cid} HTTP={resp.status_code} body={resp.text[:240]}")
            return
        try:
            data = resp.json()
        except Exception:
            failed += 1
            print(f"[FAIL] {case.cid} 非JSON响应: {resp.text[:240]}")
            return
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

    for case in pre_seed_cases:
        run_case(case)

    if post_seed_cases:
        seed_query = str(args.seed_profile_query or "").strip()
        if not seed_query:
            print("[FATAL] seed_profile_query 为空")
            return 1
        try:
            seed_resp = post_json(session, f"{base}/chat", {"query": seed_query}, timeout=args.timeout)
        except Exception as e:
            print(f"[FATAL] 画像预热请求异常: {e}")
            return 1
        if seed_resp.status_code != 200:
            print(f"[FATAL] 画像预热失败 HTTP={seed_resp.status_code} body={seed_resp.text[:240]}")
            return 1
        try:
            seed_data = seed_resp.json()
        except Exception:
            print(f"[FATAL] 画像预热返回非JSON: {seed_resp.text[:240]}")
            return 1
        seed_output = str(seed_data.get("output") or "").strip()
        if not seed_output:
            print(f"[FATAL] 画像预热返回空输出: {seed_resp.text[:240]}")
            return 1
        print("[INFO] profile seed injected")

    for case in post_seed_cases:
        run_case(case)

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
        f"[SUMMARY] passed={passed} failed={failed} pass_rate={passed / max(1, len(all_cases)):.2%} "
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
