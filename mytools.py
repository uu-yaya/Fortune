from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.tools import tool
from langchain_qdrant import Qdrant
from qdrant_client import QdrantClient
from langchain_core.output_parsers import JsonOutputParser
import requests
import json
import time
import re
from loguru import logger
from models import get_lc_ali_embeddings, get_lc_ali_model_client
import os
from pydantic import BaseModel, Field, ValidationError

from config import SERPAPI_API_KEY, VECTOR_COLLECTION_NAME, VECTOR_DB_PATH, YUANFENJU_API_KEY

if SERPAPI_API_KEY:
    os.environ["SERPAPI_API_KEY"] = SERPAPI_API_KEY


class WuxingScores(BaseModel):
    metal: int = 0
    wood: int = 0
    water: int = 0
    fire: int = 0
    earth: int = 0


class FortuneSignals(BaseModel):
    love: str = ""
    wealth: str = ""
    career: str = ""


class FortuneError(BaseModel):
    code: str = ""
    message: str = ""


class BaziToolOutput(BaseModel):
    topic: str = "daily"
    bazi: str = ""
    day_master: str = ""
    strength: str = "balanced"
    xiyongshen: str = ""
    jishen: str = ""
    wuxing_scores: WuxingScores = Field(default_factory=WuxingScores)
    fortune_signals: FortuneSignals = Field(default_factory=FortuneSignals)
    risk_points: list[str] = Field(default_factory=list)
    opportunity_points: list[str] = Field(default_factory=list)
    time_hints: list[str] = Field(default_factory=list)
    evidence_lines: list[str] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    source: str = "yuanfenju"
    error: FortuneError | None = None


def _infer_topic(query: str) -> str:
    q = str(query or "")
    if any(k in q for k in ["桃花", "姻缘", "感情", "恋爱"]):
        return "love"
    if any(k in q for k in ["财运", "财富", "收入", "金钱"]):
        return "wealth"
    if any(k in q for k in ["事业", "工作", "职场", "升职"]):
        return "career"
    if any(k in q for k in ["学业", "考试", "学习"]):
        return "study"
    return "daily"


def _to_int(value) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _normalize_strength(raw: str) -> str:
    text = str(raw or "")
    if "强" in text:
        return "strong"
    if "弱" in text:
        return "weak"
    return "balanced"


def _build_advice(topic: str, strength: str) -> list[str]:
    base = {
        "daily": [
            "今天先做一件最重要的小事，连续投入25分钟。",
            "把待办减到3项以内，先完成再扩展。",
            "晚上用3分钟复盘：什么最顺、什么该收敛。",
        ],
        "love": [
            "今天主动发出一次轻量关心，不求长聊，只求真诚。",
            "表达需求时用'我感受'句式，减少猜测和拉扯。",
            "关系不确定时先稳节奏，48小时内不做冲动决定。",
        ],
        "wealth": [
            "今天只做一项与收入直接相关的动作。",
            "先记账再消费，避免情绪性花销。",
            "对高风险决策设置24小时冷静期。",
        ],
        "career": [
            "优先推进一个可量化产出点，别同时开太多线。",
            "把关键结果写成3句汇报，提高被看见概率。",
            "遇到卡点先找一位能给反馈的人快速对齐。",
        ],
        "study": [
            "先完成一段25分钟专注学习，再休息5分钟。",
            "先攻克最难的一题或一节，建立正反馈。",
            "睡前做一次10分钟回顾，巩固当天关键点。",
        ],
    }
    advice = list(base.get(topic, base["daily"]))
    if strength == "strong":
        advice[0] = "状态可用，今天把最关键任务前置完成。"
    elif strength == "weak":
        advice[0] = "先稳住节奏，今天只设一个最小可完成目标。"
    return advice


def _to_lines(raw, limit: int = 4, max_len: int = 80) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[。\n；;]", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        clean = str(part or "").strip(" ，,。；;")
        if not clean:
            continue
        key = clean.replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:max_len])
        if len(out) >= limit:
            break
    return out


def _empty_bazi_output(topic: str, code: str, message: str) -> dict:
    model = BaziToolOutput(
        topic=topic,
        advice=_build_advice(topic, "balanced"),
        confidence=0.2,
        error=FortuneError(code=code, message=message),
    )
    return model.model_dump()


def _build_confidence(model: BaziToolOutput) -> float:
    score = 0
    score += 1 if model.bazi else 0
    score += 1 if model.day_master else 0
    score += 1 if model.xiyongshen else 0
    score += 1 if any(
        [
            model.wuxing_scores.metal,
            model.wuxing_scores.wood,
            model.wuxing_scores.water,
            model.wuxing_scores.fire,
            model.wuxing_scores.earth,
        ]
    ) else 0
    score += 1 if any([model.fortune_signals.love, model.fortune_signals.wealth, model.fortune_signals.career]) else 0
    return round(min(1.0, 0.2 + score * 0.16), 2)


def _parse_bazi_payload(payload: dict, topic: str) -> BaziToolOutput:
    data = payload.get("data", {}) or {}
    bazi_info = data.get("bazi_info", {}) or {}
    xiyongshen_info = data.get("xiyongshen", {}) or {}
    caiyun = data.get("caiyun", {}) or {}
    caiyun_desc = caiyun.get("sanshishu_caiyun", {}) if isinstance(caiyun, dict) else {}
    yinyuan = data.get("yinyuan", {}) or {}
    mingyun = data.get("mingyun", {}) or {}
    taohua = data.get("taohua", {}) or {}

    wuxing_scores = WuxingScores(
        metal=_to_int(xiyongshen_info.get("jin_score") or xiyongshen_info.get("jin_number")),
        wood=_to_int(xiyongshen_info.get("mu_score") or xiyongshen_info.get("mu_number")),
        water=_to_int(xiyongshen_info.get("shui_score") or xiyongshen_info.get("shui_number")),
        fire=_to_int(xiyongshen_info.get("huo_score") or xiyongshen_info.get("huo_number")),
        earth=_to_int(xiyongshen_info.get("tu_score") or xiyongshen_info.get("tu_number")),
    )

    signals = FortuneSignals(
        love=str(yinyuan.get("sanshishu_yinyuan") or "")[:120],
        wealth=str((caiyun_desc or {}).get("simple_desc") or "")[:60],
        career=str(mingyun.get("sanshishu_mingyun") or "")[:120],
    )

    opportunity_points = _to_lines(
        "；".join(
            [
                str((caiyun_desc or {}).get("simple_desc") or ""),
                str(yinyuan.get("sanshishu_yinyuan") or ""),
                str(mingyun.get("sanshishu_mingyun") or ""),
            ]
        )
    )
    risk_points = _to_lines(
        "；".join(
            [
                str((caiyun_desc or {}).get("risk_desc") or ""),
                str(taohua.get("risk_tip") or ""),
            ]
        )
    )
    time_hints = _to_lines(
        "；".join(
            [
                str(caiyun.get("time_hint") or ""),
                str(mingyun.get("time_hint") or ""),
            ]
        )
    )
    evidence_lines = _to_lines(
        "；".join(
            [
                str(bazi_info.get("bazi") or ""),
                str(xiyongshen_info.get("xiyongshen") or ""),
                str(xiyongshen_info.get("jishen") or ""),
            ]
        )
    )

    model = BaziToolOutput(
        topic=topic,
        bazi=str(bazi_info.get("bazi") or ""),
        day_master=str(
            bazi_info.get("riyuan")
            or xiyongshen_info.get("rizhu_tiangan")
            or ""
        ),
        strength=_normalize_strength(str(xiyongshen_info.get("qiangruo") or "")),
        xiyongshen=str(xiyongshen_info.get("xiyongshen") or ""),
        jishen=str(xiyongshen_info.get("jishen") or ""),
        wuxing_scores=wuxing_scores,
        fortune_signals=signals,
        risk_points=risk_points,
        opportunity_points=opportunity_points,
        time_hints=time_hints,
        evidence_lines=evidence_lines,
        advice=_build_advice(topic, _normalize_strength(str(xiyongshen_info.get("qiangruo") or ""))),
    )
    model.confidence = _build_confidence(model)
    return model


@tool
def serp_search(query: str):
    """只有需要了解实时信息或不知道的事情的时候才会使用这个工具。"""
    if not SERPAPI_API_KEY:
        return "实时搜索暂不可用，请联系管理员配置 SERPAPI_API_KEY。"
    try:
        from langchain_community.utilities import SerpAPIWrapper
    except Exception as e:
        logger.error(f"SerpAPI 依赖不可用: {e}")
        return "实时搜索暂不可用（缺少 serpapi 依赖），请联系管理员安装后重试。"
    try:
        serp = SerpAPIWrapper()
        result = serp.run(query)
    except Exception as e:
        logger.error(f"SerpAPI 调用失败: {e}")
        return "实时搜索服务暂时不可用，请稍后再试。"
    logger.info(f"实时搜索结果: {result}")
    # 优化：将复杂对象转为友好字符串
    if isinstance(result, (list, dict)):
        # 只取前5个景点，格式化输出
        if isinstance(result, list) and len(result) > 0 and 'title' in result[0]:
            lines = [f"{i+1}. {item['title']}（{item.get('description','')}，評分：{item.get('rating','N/A')}）" for i, item in enumerate(result[:5])]
            return "\n".join(lines)
        return json.dumps(result, ensure_ascii=False)
    return str(result)


#对知识库的检索，本质就是个RAG
@tool
def get_info_from_local_db(query: str):
    """只有回答与办公室风水常识相关的问题的时候，会使用这个工具。"""
    client = Qdrant(
        QdrantClient(path=VECTOR_DB_PATH),
        VECTOR_COLLECTION_NAME,
        get_lc_ali_embeddings(),
    )

    retriever = client.as_retriever(search_type="mmr")
    result = retriever.get_relevant_documents(query)
    return result


@tool
def bazi_cesuan(query: str):
    """只有用户说要测试算八字或做八字排盘的时候才会使用这个工具,需要输入用户姓名和出生年月日时，
    如果缺少用户姓名和出生年月日时则不可用."""
    topic = _infer_topic(query)
    if YUANFENJU_API_KEY is None:
        return json.dumps(
            _empty_bazi_output(topic, "FORTUNE_API_KEY_MISSING", "未配置命理服务密钥"),
            ensure_ascii=False,
        )
    url = f"https://api.yuanfenju.com/index.php/v1/Bazi/cesuan"
    prompt = ChatPromptTemplate.from_template(
        """你是一个参数查询助手，根据用户输入内容找出相关的参数并按json格式返回。
        JSON字段如下： 
        -"api_key":"{api_key}", 
        - "name":"姓名", 
        - "sex":"性别，0表示男，1表示女，如果用户输入内容中未提供，则根据姓名判断", 
        - "type":"日历类型，0农历，1公历，默认1",
        - "year":"出生年份 例：1998", 
        - "month":"出生月份 例 8", - "day":"出生日期，例：8", - "hours":"出生小时 例 14", 
        - "minute":"0"，
        如果没有找到相关参数，则需要提醒用户告诉你这些内容，只返回数据结构，不要有其他的评论，用户输入:{query}""")
    parser = JsonOutputParser()
    prompt = prompt.partial(format_instructions=parser.get_format_instructions())
    logger.info(f"参数查询prompt: {prompt.messages}")
    try:
        chain = prompt | get_lc_ali_model_client(streaming=False) | parser
        data = chain.invoke({"query": query, "api_key": YUANFENJU_API_KEY})
    except Exception as e:
        logger.error(f"八字参数抽取失败: {e}")
        return json.dumps(
            _empty_bazi_output(topic, "FORTUNE_PARSE_FAILED", "参数抽取失败，请补充姓名和出生年月日时"),
            ensure_ascii=False,
        )

    logger.info(f"大模型返回参数抽取结果: {data}")
    timeout_seconds = 3
    max_retries = 2
    last_error = ("FORTUNE_UPSTREAM_HTTP", "命理服务暂时不可用")
    for attempt in range(max_retries + 1):
        try:
            result = requests.post(url, data=data, timeout=timeout_seconds)
            if result.status_code != 200:
                code = "FORTUNE_UPSTREAM_5XX" if result.status_code >= 500 else "FORTUNE_UPSTREAM_HTTP"
                last_error = (code, f"命理服务响应异常（HTTP {result.status_code}）")
                raise requests.RequestException(last_error[1])
            payload = result.json()
            logger.info(f"缘分居cesuan接口返回JSON: {payload}")
            if int(payload.get("errcode", 1)) != 0:
                msg = str(payload.get("errmsg") or "命理服务返回错误")
                return json.dumps(
                    _empty_bazi_output(topic, "FORTUNE_UPSTREAM_HTTP", msg),
                    ensure_ascii=False,
                )
            model = _parse_bazi_payload(payload, topic)
            try:
                validated = BaziToolOutput.model_validate(model.model_dump())
            except ValidationError as ve:
                logger.error(f"八字结构化校验失败: {ve}")
                return json.dumps(
                    _empty_bazi_output(topic, "FORTUNE_PARSE_FAILED", "命理结果解析失败"),
                    ensure_ascii=False,
                )
            return json.dumps(validated.model_dump(), ensure_ascii=False)
        except requests.Timeout:
            last_error = ("FORTUNE_TIMEOUT", "命理服务超时，请稍后重试")
        except (requests.RequestException, json.JSONDecodeError) as e:
            logger.warning(f"八字接口请求失败，attempt={attempt + 1}: {e}")
            if isinstance(e, json.JSONDecodeError):
                last_error = ("FORTUNE_PARSE_FAILED", "命理结果解析失败")
        if attempt < max_retries:
            time.sleep(0.4 * (2 ** attempt))

    return json.dumps(
        _empty_bazi_output(topic, last_error[0], last_error[1]),
        ensure_ascii=False,
    )

@tool
def yaoyigua():
    """只有用户想要占卜抽签的时候才会使用这个工具。"""
    api_key = YUANFENJU_API_KEY
    url = f"https://api.yuanfenju.com/index.php/v1/Zhanbu/meiri"
    result = requests.post(url, data={"api_key": api_key})
    logger.info(f"缘分居meiri接口返回: {result}")
    if result.status_code == 200:
        logger.info(f"缘分居meiri接口返回JSON: {result.json()}")
        return_string = json.loads(result.text)
        image = return_string["data"]["description"]
        logger.info(f"每日一占: {image}")
        return image
    else:
        return "技术错误，请告诉用户稍后再试。"

@tool
def jiemeng(query: str):
    """只有用户想要解梦的时候才会使用这个工具,需要输入用户梦境的内容，如果缺少用户梦境的内容则不可用。"""
    api_key = YUANFENJU_API_KEY
    url = f"https://api.yuanfenju.com/index.php/v1/Gongju/zhougong"
    LLM = get_lc_ali_model_client(streaming=False)
    prompt = PromptTemplate.from_template("根据内容提取1个关键词，只返回关键词，内容为:{topic}")
    prompt_value = prompt.invoke({"topic": query})
    keyword = LLM.invoke(prompt_value)
    logger.info(f"提取的关键词: {keyword}")
    result = requests.post(url, data={"api_key": api_key, "title_zhougong": keyword})
    if result.status_code == 200:
        logger.info(f"缘分居zhougong接口返回JSON: {result.json()}")
        returnstring = json.loads(result.text)
        return returnstring
    else:
        return "技术错误，请告诉用户稍后再试。"
