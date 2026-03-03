import re
from typing import Any


SCENARIOS = {
    "destined_portrait": "正缘写实画像",
    "destined_video": "正缘视频",
    "encounter_story_video": "正缘相遇剧情片段",
    "healing_sleep_video": "命理治愈视频",
    "general_image": "专属图片",
    "general_video": "专属视频",
}

MEDIA_ROUTES = {"chat", "media_create", "media_feedback", "media_followup", "media_clarify"}

GEN_ACTION_PATTERN = re.compile(
    r"(生成|制作|帮我做|帮我生成|给我来|画一?[个张幅]?|帮我画|出一?[个段条]?|做一?[个段条]?|做个|来个|"
    r"变成|做成|转成|画出来|可视化|配图|出图|出片)"
)
IMAGE_PATTERN = re.compile(
    r"(图|图片|图像|画像|照片|海报|壁纸|插画|长什么样|长相|样子|写真|头像|一幅画|一张画|画作|画面|配图|插图|绘图|封面图|卡面|视觉稿)"
)
VIDEO_PATTERN = re.compile(r"(视频|短片|片子|成片|剧情|片段|mv|动画|动图|动态影像)")
DESTINED_PATTERN = re.compile(r"(正缘|理想对象|另一半|未来对象|真命天子|真命天女)")
ENCOUNTER_PATTERN = re.compile(r"(相遇|邂逅|初见|剧情|故事|片段)")
HEALING_PATTERN = re.compile(r"(睡前|冥想|疗愈|治愈|助眠|好运|暗示语|舒缓)")
CONSULT_ONLY_PATTERN = re.compile(r"(怎么办|分析|建议|如何|为什么|原因|该不该|能不能在一起)")
WEDDING_PATTERN = re.compile(r"(结婚|婚礼|婚纱|誓词|交换戒指|婚宴)")
DUAL_RELATION_PATTERN = re.compile(
    r"((我和|我与|我们|我俩|两个人).*(正缘|理想对象|另一半|对象|伴侣|爱人))|((正缘|理想对象|另一半|对象|伴侣|爱人).*(和我|与我))"
)
USER_WITH_OTHER_PERSON_PATTERN = re.compile(
    r"(我和|我与|我跟|我同|我俩|两个人).{0,24}(他|她|ta|TA|对象|伴侣|爱人|另一半|女生|男生|朋友|同学|闺蜜|兄弟|家人|伴郎|伴娘|同事)"
)
MULTI_SCENE_PATTERN = re.compile(r"(多人|群像|亲友|伴郎|伴娘|家人|朋友|同学|婚宴|仪式|宾客|团队)")

BLOCKED_SEXUAL_PATTERN = re.compile(r"(露骨|成人视频|性爱|裸|色情|约炮)")
BLOCKED_ILLEGAL_PATTERN = re.compile(r"(违法|违禁|毒品|暴恐|诈骗)")
BLOCKED_MINOR_PATTERN = re.compile(r"(未成年|小学生|初中生|高中生)")
BLOCKED_REAL_PERSON_PATTERN = re.compile(r"(长得像|照着|仿照|复刻).*(明星|真人|某某|网红)")
FOLLOWUP_COMMAND_PATTERN = re.compile(
    r"(再来一个|同款再来|再做一个|换个风格|换一种风格|按这个再来|照这个再来|重做一版|再来一版|继续刚才那个风格|"
    r"按上一个来|同风格再来|再生成一?[个段张条]?|重新生成)"
)
DIRECT_CREATE_PREFIX_PATTERN = re.compile(r"^\s*(帮我|给我|请|麻烦|现在|立刻|再|重新)")
BARE_CREATE_COMMAND_PATTERN = re.compile(r"^\s*(生成|制作|画|做|出|来)\s*")
SOFT_CREATE_PATTERN = re.compile(
    r"(?:^|[，。！？\s])(?:请|麻烦|可以|能不能|能否)?\s*"
    r"(我想要(?!的)|我想看|我想做|想要(?!的)|能给我|可以给我|给我(?!的))"
    r"(?:\s*(来|整|做|出|生成|一?[个张幅段条]|一张|一个|一段|一条|图|视频))?"
)
CREATE_ACTION_PATTERN = re.compile(r"(生成(?!的)|制作(?!的)|做成|变成|转成|画出来|画成|配图|出图|出片|可视化|来一张|来一个)")
NEGATION_GUARD_PATTERN = re.compile(
    r"(不是要生成|不用生成|先别生成|不需要生成|只是聊聊|先聊聊|先讨论|先说说|不是让你生成|不要生成)"
)
DISCUSS_ONLY_PATTERN = re.compile(r"(聊聊|讨论|说说|怎么看|你觉得|评价|分析一下|讲讲)")
VISUAL_COMMENT_PATTERN = re.compile(r"(构图|质感|氛围|镜头|节奏|审美|细节|像电影|画面)")
STRUCTURE_TO_IMAGE_PATTERN = re.compile(r"(把|将).{0,30}(变成|做成|转成).{0,12}(图|图片|图像|画|海报|插画|配图)")
STRUCTURE_TO_VIDEO_PATTERN = re.compile(r"(把|将).{0,30}(变成|做成|转成).{0,12}(视频|短片|片段|动图)")
STRUCTURE_DRAW_OUT_PATTERN = re.compile(r"(把|将).{0,40}(画出来)")
STRUCTURE_ASSIGN_IMAGE_PATTERN = re.compile(r"(给).{0,40}(配图|配一张图)")
PAST_REFERENCE_PATTERN = re.compile(
    r"(你给我生成的|你刚给我生成的|帮我生成的|刚才生成的|上次生成的|这个视频|那个视频|这支视频|那支视频|这条视频|那条视频|"
    r"这张图|那张图|这个图像|那个图像|这张图片|那张图片|这幅画|那幅画|这个画面|那个画面|你做的那个)"
)
PRAISE_FEEDBACK_PATTERN = re.compile(r"(好喜欢|太美了|就是我的梦|绝了|好棒|太好了|好好看|好看)")


def _contains_media_object(query: str) -> tuple[bool, bool, bool]:
    q = str(query or "")
    has_image = bool(IMAGE_PATTERN.search(q))
    has_video = bool(VIDEO_PATTERN.search(q))
    return has_image, has_video, bool(has_image or has_video)


def _has_strong_create_command(query: str, media_like: bool) -> bool:
    q = str(query or "").strip()
    if not q or not media_like:
        return False
    if ("生成的" in q or "做的" in q) and not FOLLOWUP_COMMAND_PATTERN.search(q):
        return False
    if FOLLOWUP_COMMAND_PATTERN.search(q):
        return True
    if BARE_CREATE_COMMAND_PATTERN.search(q):
        return True
    if re.search(r"^\s*(来一个|做一个|来一张|做一张|来一段|做一段)", q):
        return True
    has_direct_prefix = bool(DIRECT_CREATE_PREFIX_PATTERN.search(q))
    has_gen_action = bool(GEN_ACTION_PATTERN.search(q))
    return bool(has_direct_prefix and has_gen_action)


def _has_structured_media_transform(query: str) -> tuple[bool, str]:
    q = str(query or "").strip()
    if not q:
        return False, ""
    if STRUCTURE_TO_VIDEO_PATTERN.search(q):
        return True, "video"
    if STRUCTURE_TO_IMAGE_PATTERN.search(q) or STRUCTURE_DRAW_OUT_PATTERN.search(q) or STRUCTURE_ASSIGN_IMAGE_PATTERN.search(q):
        return True, "image"
    return False, ""


def _has_explicit_create_action(query: str) -> bool:
    q = str(query or "").strip()
    if not q:
        return False
    if re.search(r"(生成的|做的|给我的|你给我生成的|刚才生成的|上次生成的)", q):
        return False
    return bool(CREATE_ACTION_PATTERN.search(q))


def _infer_media_scenario(query: str, *, has_image: bool, has_video: bool, media_like: bool) -> str:
    q = str(query or "").strip()
    if not q or not media_like:
        return ""
    has_destined = bool(DESTINED_PATTERN.search(q))
    has_healing = bool(HEALING_PATTERN.search(q))
    has_encounter = bool(ENCOUNTER_PATTERN.search(q))
    has_dual = bool(DUAL_RELATION_PATTERN.search(q))
    has_user_pair = bool(USER_WITH_OTHER_PERSON_PATTERN.search(q))
    has_wedding = bool(WEDDING_PATTERN.search(q))
    has_multi = bool(MULTI_SCENE_PATTERN.search(q))

    if has_healing and (has_video or "冥想" in q):
        return "healing_sleep_video"
    if (
        (has_encounter and has_video)
        or ("剧情" in q and ("对象" in q or has_user_pair))
        or (has_destined and has_video and (has_dual or has_wedding or has_multi or has_user_pair))
        or (has_video and (has_wedding or has_multi) and has_user_pair)
    ):
        return "encounter_story_video"
    if has_destined and (has_video or "动态" in q):
        return "destined_video"
    if has_destined and (has_image or "长什么样" in q):
        return "destined_portrait"
    if has_video:
        return "general_video"
    if has_image:
        return "general_image"
    if re.search(r"(视频|短片|片段|动图|成片)", q):
        return "general_video"
    if re.search(r"(画出来|一幅画|一张画|配图|插图|绘图|封面图|卡面|视觉稿)", q):
        return "general_image"
    return ""


def _build_route_payload(
    *,
    route: str,
    scenario: str = "",
    confidence: str = "low",
    reason_code: str = "",
    blocked: bool = False,
    blocked_reason: str = "",
    media_like: bool = False,
    needs_llm: bool = False,
    intent_version: str = "v3",
    decision_source: str = "rule",
    create_score: int = 0,
    feedback_score: int = 0,
    negation_guard_hit: bool = False,
    conflict: bool = False,
) -> dict[str, Any]:
    route_name = str(route or "chat").strip().lower()
    if route_name not in MEDIA_ROUTES:
        route_name = "chat"
    scenario_name = str(scenario or "").strip()
    active_blocked = bool(blocked and route_name in {"media_create", "media_followup"})
    return {
        "route": route_name,
        "scenario": scenario_name,
        "scenario_label": SCENARIOS.get(scenario_name, scenario_name),
        "confidence": str(confidence or "low"),
        "reason_code": str(reason_code or "none"),
        "blocked": active_blocked,
        "blocked_reason": str(blocked_reason or "") if active_blocked else "",
        "media_like": bool(media_like),
        "needs_llm": bool(needs_llm),
        "intent_version": str(intent_version or "v3"),
        "decision_source": str(decision_source or "rule"),
        "create_score": int(max(0, int(create_score or 0))),
        "feedback_score": int(max(0, int(feedback_score or 0))),
        "negation_guard_hit": bool(negation_guard_hit),
        "conflict": bool(conflict),
    }


def _normalize_partner_gender_preference(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"female", "woman", "women", "girl", "女生", "女性", "女", "女的", "女孩子", "女孩"}:
        return "female"
    if raw in {"male", "man", "men", "boy", "男生", "男性", "男", "男的", "男孩子", "男孩"}:
        return "male"
    if raw in {"any", "all", "both", "不限", "都可以", "都行", "男女都可", "男女都行", "男女都可以"}:
        return "any"
    return "unknown"


def _normalize_gender(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"male", "man", "boy", "男", "男生", "男性"}:
        return "male"
    if raw in {"female", "woman", "girl", "女", "女生", "女性"}:
        return "female"
    return "unknown"


def _dual_subject_clause(scenario: str, query: str) -> str:
    q = str(query or "").strip()
    if str(scenario or "") not in {"destined_video", "encounter_story_video"}:
        return ""
    if not (DUAL_RELATION_PATTERN.search(q) or USER_WITH_OTHER_PERSON_PATTERN.search(q)):
        return ""
    if WEDDING_PATTERN.search(q):
        return (
            "必须双人同框：主角A为用户、主角B为用户指定对象（可为正缘或其他对象）。"
            "镜头至少包含：并肩入场、交换戒指或誓词互动、对视微笑收束。"
            "禁止全片只出现单人主体。"
        )
    return (
        "必须双人同框：主角A为用户、主角B为用户指定对象（可为正缘或其他对象）。"
        "镜头需出现两人互动（对视、牵手、并肩行走等），禁止全片只出现单人主体。"
    )


def _multi_scene_clause(query: str) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    if WEDDING_PATTERN.search(q) or MULTI_SCENE_PATTERN.search(q):
        return "可加入多人环境角色（如亲友/宾客）做背景衬托，但主叙事仍围绕“我与用户指定对象”的互动推进。"
    return ""


def _route_media_intent_v2(query: str, *, recent_media: dict[str, Any] | None = None) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        return _build_route_payload(route="chat", confidence="low", reason_code="empty_query", intent_version="v2")
    if CONSULT_ONLY_PATTERN.search(q) and not (IMAGE_PATTERN.search(q) or VIDEO_PATTERN.search(q)):
        return _build_route_payload(route="chat", confidence="low", reason_code="consult_only", intent_version="v2")

    has_image, has_video, media_like = _contains_media_object(q)
    has_followup_cmd = bool(FOLLOWUP_COMMAND_PATTERN.search(q))
    has_strong_create = _has_strong_create_command(q, media_like=media_like)
    has_past_reference = bool(PAST_REFERENCE_PATTERN.search(q))
    has_praise_feedback = bool(PRAISE_FEEDBACK_PATTERN.search(q))
    recent = recent_media if isinstance(recent_media, dict) else {}
    recent_scenario = str(recent.get("scenario") or "").strip()
    has_recent_media = bool(recent_scenario)

    blocked, reason = check_media_safety(q)
    scenario = _infer_media_scenario(q, has_image=has_image, has_video=has_video, media_like=media_like)

    if has_followup_cmd:
        if has_recent_media:
            return _build_route_payload(
                route="media_followup",
                scenario=recent_scenario or scenario,
                confidence="high",
                reason_code="followup_with_context",
                blocked=blocked,
                blocked_reason=reason,
                media_like=media_like,
                intent_version="v2",
            )
        if media_like:
            return _build_route_payload(
                route="media_create",
                scenario=scenario,
                confidence="high",
                reason_code="followup_with_explicit_media_no_context",
                blocked=blocked,
                blocked_reason=reason,
                media_like=True,
                intent_version="v2",
            )
        return _build_route_payload(
            route="media_clarify",
            scenario="",
            confidence="medium",
            reason_code="followup_without_context",
            media_like=media_like,
            intent_version="v2",
        )

    if has_strong_create and media_like:
        return _build_route_payload(
            route="media_create",
            scenario=scenario,
            confidence="high",
            reason_code="strong_command_with_media",
            blocked=blocked,
            blocked_reason=reason,
            media_like=True,
            intent_version="v2",
        )

    if has_past_reference and not has_strong_create:
        return _build_route_payload(
            route="media_feedback",
            confidence="high",
            reason_code="past_reference_no_command",
            media_like=media_like,
            intent_version="v2",
        )

    if has_praise_feedback and media_like and not has_strong_create:
        return _build_route_payload(
            route="media_feedback",
            confidence="high",
            reason_code="praise_without_command",
            media_like=True,
            intent_version="v2",
        )

    if media_like:
        return _build_route_payload(
            route="chat",
            scenario="",
            confidence="medium",
            reason_code="media_mention_no_command",
            media_like=True,
            needs_llm=True,
            intent_version="v2",
        )

    return _build_route_payload(
        route="chat",
        scenario="",
        confidence="low",
        reason_code="non_media_query",
        intent_version="v2",
    )


def route_media_intent(
    query: str,
    *,
    recent_media: dict[str, Any] | None = None,
    router_version: str = "v3",
    negation_guard_enabled: bool = True,
) -> dict[str, Any]:
    if str(router_version or "v3").strip().lower() != "v3":
        return _route_media_intent_v2(query, recent_media=recent_media)

    q = str(query or "").strip()
    if not q:
        return _build_route_payload(route="chat", confidence="low", reason_code="empty_query")
    if CONSULT_ONLY_PATTERN.search(q) and not (IMAGE_PATTERN.search(q) or VIDEO_PATTERN.search(q)):
        return _build_route_payload(route="chat", confidence="low", reason_code="consult_only")

    has_image, has_video, media_like = _contains_media_object(q)
    has_followup_cmd = bool(FOLLOWUP_COMMAND_PATTERN.search(q))
    has_strong_create = _has_strong_create_command(q, media_like=media_like)
    has_soft_create = bool(SOFT_CREATE_PATTERN.search(q))
    has_create_action = _has_explicit_create_action(q)
    has_past_reference = bool(PAST_REFERENCE_PATTERN.search(q))
    has_praise_feedback = bool(PRAISE_FEEDBACK_PATTERN.search(q))
    has_discuss_only = bool(DISCUSS_ONLY_PATTERN.search(q))
    has_visual_comment = bool(VISUAL_COMMENT_PATTERN.search(q))
    has_structured_transform, transform_type = _has_structured_media_transform(q)
    recent = recent_media if isinstance(recent_media, dict) else {}
    recent_scenario = str(recent.get("scenario") or "").strip()
    recent_status = str(recent.get("status") or "").strip().lower()
    has_recent_media = bool(recent_scenario and recent_status in {"pending", "running", "succeeded"})
    negation_guard_hit = bool(negation_guard_enabled and NEGATION_GUARD_PATTERN.search(q))

    media_like_signal = bool(media_like or has_structured_transform)
    blocked, reason = check_media_safety(q)
    scenario = _infer_media_scenario(
        q,
        has_image=bool(has_image or transform_type == "image"),
        has_video=bool(has_video or transform_type == "video"),
        media_like=media_like_signal,
    )

    if negation_guard_hit:
        return _build_route_payload(
            route="chat",
            scenario="",
            confidence="high",
            reason_code="negation_guarded",
            media_like=media_like_signal,
            needs_llm=False,
            negation_guard_hit=True,
        )

    if has_followup_cmd:
        if has_recent_media:
            return _build_route_payload(
                route="media_followup",
                scenario=recent_scenario or scenario,
                confidence="high",
                reason_code="followup_with_context",
                blocked=blocked,
                blocked_reason=reason,
                media_like=media_like_signal,
                create_score=2,
            )
        if media_like_signal and (has_strong_create or has_soft_create or has_structured_transform or has_create_action or bool(scenario)):
            return _build_route_payload(
                route="media_create",
                scenario=scenario,
                confidence="high",
                reason_code="followup_with_explicit_media_no_context",
                blocked=blocked,
                blocked_reason=reason,
                media_like=media_like_signal,
                create_score=2,
            )
        return _build_route_payload(
            route="media_clarify",
            scenario="",
            confidence="medium",
            reason_code="followup_without_context",
            media_like=media_like_signal,
            needs_llm=False,
        )

    create_score = 0
    if has_structured_transform:
        create_score += 2
    if has_strong_create and media_like_signal:
        create_score += 2
    if has_soft_create and media_like_signal:
        create_score += 2
    elif has_create_action and media_like_signal:
        create_score += 1
    if has_past_reference and create_score > 0 and not has_followup_cmd:
        create_score -= 1

    feedback_score = 0
    if has_past_reference:
        feedback_score += 2
    if has_praise_feedback:
        feedback_score += 1
    if has_praise_feedback and media_like_signal:
        feedback_score += 1
    if has_visual_comment and (media_like_signal or has_past_reference):
        feedback_score += 1
    if has_discuss_only and not has_praise_feedback:
        feedback_score = max(0, feedback_score - 1)

    if has_discuss_only and has_past_reference and create_score < 2:
        return _build_route_payload(
            route="chat",
            confidence="medium",
            reason_code="media_discuss_only",
            media_like=media_like_signal,
            needs_llm=False,
            create_score=create_score,
            feedback_score=feedback_score,
        )

    has_conflict = bool(create_score >= 2 and feedback_score >= 2)
    if has_conflict:
        return _build_route_payload(
            route="chat",
            confidence="medium",
            reason_code="intent_conflict",
            media_like=media_like_signal,
            needs_llm=True,
            create_score=create_score,
            feedback_score=feedback_score,
            conflict=True,
        )

    if create_score >= 2 and media_like_signal:
        return _build_route_payload(
            route="media_create",
            scenario=scenario,
            confidence="high",
            reason_code="v3_scored_media_create",
            blocked=blocked,
            blocked_reason=reason,
            media_like=media_like_signal,
            create_score=create_score,
            feedback_score=feedback_score,
        )

    if feedback_score >= 2 and create_score < 2:
        return _build_route_payload(
            route="media_feedback",
            confidence="high",
            reason_code="v3_scored_media_feedback",
            media_like=media_like_signal,
            create_score=create_score,
            feedback_score=feedback_score,
        )

    if media_like_signal:
        return _build_route_payload(
            route="chat",
            scenario="",
            confidence="medium",
            reason_code="media_mention_no_command",
            media_like=True,
            needs_llm=True,
            create_score=create_score,
            feedback_score=feedback_score,
        )

    return _build_route_payload(
        route="chat",
        scenario="",
        confidence="low",
        reason_code="non_media_query",
        media_like=False,
        create_score=create_score,
        feedback_score=feedback_score,
    )


def detect_media_intent(
    query: str,
    *,
    recent_media: dict[str, Any] | None = None,
    router_version: str = "v3",
    negation_guard_enabled: bool = True,
) -> dict[str, Any]:
    routed = route_media_intent(
        query,
        recent_media=recent_media,
        router_version=router_version,
        negation_guard_enabled=negation_guard_enabled,
    )
    route_name = str(routed.get("route") or "chat")
    hit = route_name in {"media_create", "media_followup"}
    return {
        "hit": hit,
        "scenario": str(routed.get("scenario") or "") if hit else "",
        "scenario_label": str(routed.get("scenario_label") or "") if hit else "",
        "blocked": bool(routed.get("blocked") or False),
        "blocked_reason": str(routed.get("blocked_reason") or ""),
        "route": route_name,
        "confidence": str(routed.get("confidence") or "low"),
        "reason_code": str(routed.get("reason_code") or "none"),
        "needs_llm": bool(routed.get("needs_llm") or False),
        "media_like": bool(routed.get("media_like") or False),
        "intent_version": str(routed.get("intent_version") or "v3"),
        "decision_source": str(routed.get("decision_source") or "rule"),
        "create_score": int(max(0, int(routed.get("create_score") or 0))),
        "feedback_score": int(max(0, int(routed.get("feedback_score") or 0))),
        "negation_guard_hit": bool(routed.get("negation_guard_hit") or False),
        "conflict": bool(routed.get("conflict") or False),
    }


def check_media_safety(query: str) -> tuple[bool, str]:
    q = str(query or "")
    if BLOCKED_SEXUAL_PATTERN.search(q):
        return True, "内容涉及露骨或不适宜生成"
    if BLOCKED_ILLEGAL_PATTERN.search(q):
        return True, "内容涉及违法风险"
    if BLOCKED_MINOR_PATTERN.search(q):
        return True, "内容涉及未成年人敏感场景"
    if BLOCKED_REAL_PERSON_PATTERN.search(q):
        return True, "不支持仿照真实人物肖像"
    return False, ""


def build_media_prompt(
    *,
    scenario: str,
    query: str,
    profile: dict[str, str] | None = None,
    destiny_hint: str = "",
) -> dict[str, Any]:
    p = profile or {}
    preferred_name = str(p.get("preferred_name") or p.get("name") or "").strip()
    name_hint = f"用户称呼偏好：{preferred_name}。" if preferred_name else ""
    user_gender = _normalize_gender(str(p.get("gender") or "unknown"))
    user_gender_hint = ""
    if user_gender == "male":
        user_gender_hint = "用户性别：男性；治愈表达可偏沉静、稳定与松弛感。"
    elif user_gender == "female":
        user_gender_hint = "用户性别：女性；治愈表达可偏柔和、细腻与安全感。"
    else:
        user_gender_hint = "用户性别：未提供；请使用中性表达与中性人物设定。"
    destiny_text = str(destiny_hint or "").strip()
    love_destiny_clause = (
        f"桃花/正缘命理线索：{destiny_text}。请严格结合这条线索设计人物气质、互动氛围和场景细节。"
        if destiny_text
        else "桃花/正缘命理线索：未提供明确盘面，请按温柔稳定、长期关系导向来生成。"
    )
    healing_destiny_clause = (
        f"用户命理治愈线索：{destiny_text}。请据此匹配节奏、色调、场景元素和暗示语方向。"
        if destiny_text
        else "用户命理治愈线索：未提供明确盘面，请按低刺激、慢节奏、稳定安全感来生成。"
    )
    partner_pref = _normalize_partner_gender_preference(str(p.get("partner_gender_preference") or "unknown"))
    partner_hint = ""
    partner_negative_hint = ""
    if partner_pref == "female":
        partner_hint = "目标对象性别偏好：女性，请聚焦女性形象，不要出现男性主体。"
        partner_negative_hint = "男性形象、男性主体"
    elif partner_pref == "male":
        partner_hint = "目标对象性别偏好：男性，请聚焦男性形象，不要出现女性主体。"
        partner_negative_hint = "女性形象、女性主体"
    elif partner_pref == "any":
        partner_hint = "目标对象性别偏好：不限性别，请优先呈现气质契合度。"
    query_text = str(query or "").strip()
    if scenario == "destined_portrait":
        prompt = (
            f"请生成写实风格的“理想正缘人物画像”。{name_hint}"
            f"{partner_hint}"
            f"{love_destiny_clause}"
            f"{user_gender_hint}"
            "要求：真人摄影质感、自然光、细节清晰、不过度美颜、构图干净。"
            f"用户补充：{query_text}"
        )
        negative_prompt = "低清晰度、卡通、畸形五官、过度滤镜、露骨内容、未成年形象"
        if partner_negative_hint:
            negative_prompt = f"{negative_prompt}、{partner_negative_hint}"
        return {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style": "photorealistic",
            "type": "文生图",
            "duration_sec": 0,
            "scenario": scenario,
            "destiny_hint": destiny_text,
        }
    if scenario == "destined_video":
        dual_clause = _dual_subject_clause(scenario, query_text)
        prompt = (
            f"请生成“理想正缘人物”写实短视频镜头。{name_hint}"
            f"{partner_hint}"
            f"{love_destiny_clause}"
            f"{user_gender_hint}"
            f"{dual_clause}"
            "要求：电影级运镜、光影自然、人物动作克制，时长 8-12 秒。"
            f"用户补充：{query_text}"
        )
        negative_prompt = "卡通、低清、闪烁、露骨内容、未成年人敏感画面"
        if partner_negative_hint:
            negative_prompt = f"{negative_prompt}、{partner_negative_hint}"
        return {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style": "cinematic_realism",
            "type": "文生视频",
            "duration_sec": 10,
            "scenario": scenario,
            "destiny_hint": destiny_text,
        }
    if scenario == "encounter_story_video":
        dual_clause = _dual_subject_clause(scenario, query_text)
        multi_clause = _multi_scene_clause(query_text)
        story_target = "我与理想对象" if DESTINED_PATTERN.search(query_text) else "我与指定对象"
        prompt = (
            f"请生成“{story_target}”的轻剧情短视频。{name_hint}"
            f"{partner_hint}"
            f"{love_destiny_clause}"
            f"{user_gender_hint}"
            f"{dual_clause}"
            f"{multi_clause}"
            "结构：开场环境2秒-相遇瞬间4秒-温暖收束4秒。"
            "风格：日常写实、温柔氛围、不过度戏剧化。"
            f"用户补充：{query_text}"
        )
        negative_prompt = "狗血冲突、露骨亲密、未成年人画面、暴力惊悚"
        if partner_negative_hint:
            negative_prompt = f"{negative_prompt}、{partner_negative_hint}"
        return {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style": "warm_cinematic_story",
            "type": "文生视频",
            "duration_sec": 10,
            "scenario": scenario,
            "destiny_hint": destiny_text,
        }
    if scenario == "general_image":
        prompt = (
            "请根据用户描述生成高质量写实图像，优先尊重用户原始需求。"
            f"{name_hint}{user_gender_hint}"
            f"用户需求：{query_text}"
        )
        return {
            "prompt": prompt,
            "negative_prompt": "露骨内容、未成年人敏感内容、违法内容、仿照真实人物",
            "style": "general_visual",
            "type": "文生图",
            "duration_sec": 0,
            "scenario": "general_image",
        }
    if scenario == "general_video":
        prompt = (
            "请根据用户描述生成高质量写实短视频，优先尊重用户原始需求。"
            f"{name_hint}{user_gender_hint}"
            "要求：镜头连贯、光影自然、动作稳定，默认 8-12 秒。"
            f"用户需求：{query_text}"
        )
        return {
            "prompt": prompt,
            "negative_prompt": "露骨内容、未成年人敏感内容、违法内容、仿照真实人物、抖动闪烁",
            "style": "general_video",
            "type": "文生视频",
            "duration_sec": 10,
            "scenario": "general_video",
        }
    prompt = (
        "请生成让人治愈的命理能量视频，目标是缓解焦虑、稳定情绪、恢复内在秩序。"
        f"{name_hint}{user_gender_hint}{healing_destiny_clause}"
        "结构：自然场景过渡-呼吸放松引导-温暖暗示语-轻柔收束，时长 20-30 秒。"
        "风格：写实、柔光、慢节奏、低刺激，不要堆砌神秘符号。"
        f"用户补充：{query_text}"
    )
    return {
        "prompt": prompt,
        "negative_prompt": "高刺激画面、噪音、惊悚、露骨内容、未成年敏感内容",
        "style": "healing_soft_visual",
        "type": "文生视频",
        "duration_sec": 24,
        "scenario": "healing_sleep_video",
        "destiny_hint": destiny_text,
    }


def _none_intent() -> dict[str, Any]:
    return {
        "hit": False,
        "scenario": "",
        "scenario_label": "",
        "blocked": False,
        "blocked_reason": "",
    }
