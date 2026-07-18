# -*- coding: utf-8 -*-
"""
answer_flow.py — 四条回答链路（/api/ask、/api/ask/stream、钉钉同步、钉钉流式）共用的
收尾簿记：qa_session_log 载荷组装 + 写历史策略 + NO_RESULT 文案。

设计约束（务必保持）：本模块只提供【纯函数与常量】，不做任何副作用调用。
log_qa_session / append_to_history 的实际调用留在各调用方模块内、经模块全局名解析 ——
现有 4 个测试文件约 50 处 patch("opensearch_pipeline.api.log_qa_session") /
patch("opensearch_pipeline.dingtalk_bot.…") 的 mock 接缝全部依赖这一点。
把副作用挪进本模块会让这些测试静默失去拦截对象。

背景：2026-06 评审确认 4 条链路的簿记尾部各自手写、已发生漂移
（/api/ask 落请求体 user_id 而非 uid、API 路径从不落 user_dept、
content_blocks_json 三缺一、NO_RESULT 文案三份两样、写历史条件四种）。
本模块是这些字段的单一事实来源。
"""

import json
import re
from typing import Any, Dict, List, Optional, Union

# 统一 NO_RESULT 文案（= 原 /api/ask 措辞；钉钉端回复时自行加 "🤷 " 渠道前缀）
NO_RESULT_MESSAGE = "抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。"

# 强拒答句式（serving 侧 canonical 判定）。与 eval_harness/matching.py 的
# _REFUSAL_STRONG 故意保持两份独立拷贝：serving 不得 import eval_harness，
# eval 侧度量口径也不随 serving 演进漂移。如修改请同步评审两处。
_REFUSAL_STRONG_PATTERN = re.compile(
    r"(抱歉[，,。]?\s*(当前|知识库|未|没有)|知识库中(未|没有)|未找到相关信息|"
    r"无法回答|没有找到相关|未能找到相关|未提供相关信息)"
)


def is_refusal_answer(text: Optional[str], max_chars: int = 110) -> bool:
    """回答是否为「知识库未命中」类拒答（纯函数）。

    两种命中：拒答句式开篇（前 30 字符内出现，长答也算拒答开场）；
    或全文很短（≤ max_chars）且含强拒答句式。语义对齐 eval_harness/matching.py
    的 hard_refusal。NO_RESULT_MESSAGE 与 LLM 规则 2 指示的
    「抱歉，当前知识库中未找到相关信息」都命中 —— /api/ask 用它统一两种
    "未找到" 形态（检索空 vs LLM 带弱来源拒答）置 no_result 标志。
    """
    if not text:
        return False
    a = text.strip()
    m = _REFUSAL_STRONG_PATTERN.search(a)
    if not m:
        return False
    return m.start() <= 30 or len(a) <= max_chars

# LLM 生成参数缺省值（API 请求可覆盖；钉钉端固定用缺省）
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.1


# ═══════════════════════════════════════════════════════════════
# 通用能力分级开放：话术常量 + 知识库失败统一抽象（纯函数，见
# docs/general_ability_opening_design.md；路由/分诊在 intent_router，
# 通用回答在 general_answerer —— 本模块保持零副作用、零重依赖）
# ═══════════════════════════════════════════════════════════════

# T0① 敏感硬红线（BLOCKED）
SENSITIVE_BLOCK_MESSAGE = (
    "抱歉，这个问题涉及敏感信息，我无法回答。"
    "如有薪酬、人事、法务等方面的疑问，请咨询对应部门或您的主管。"
)
# T0③ 实时公共信息（v1 恒拒，诚实说明能力边界）
REALTIME_BLOCK_MESSAGE = (
    "我目前没有实时信息来源，无法提供天气、汇率、新闻等实时内容。"
    "公司制度、流程与操作类问题随时可以问我。"
)
# 通用问题但通用层未开档/未放开
GENERAL_TIER_OFF_MESSAGE = (
    "这个问题不在公司知识库范围内，我目前主要解答公司制度、流程与操作类问题。"
)
# 通用 LLM 日配额用尽（rate_limiter general_quota）
GENERAL_QUOTA_MESSAGE = (
    "今日的通用问答额度已用完，明天可以继续；"
    "公司知识库相关的问题不受限制，随时可以问我。"
)


def suggest_titles(chunks: Optional[List[Dict[str, Any]]], limit: int = 2) -> List[str]:
    """从手头检索结果提取去重文档标题（引导式拒答"您是不是想问"用，零额外检索）。"""
    out: List[str] = []
    seen = set()
    for c in chunks or []:
        title = (c.get("title") or "").strip()
        if title and title not in seen:
            seen.add(title)
            out.append(title)
        if len(out) >= limit:
            break
    return out


def build_guided_refusal(
    titles: Optional[List[str]] = None,
    rephrase: Optional[List[str]] = None,
) -> str:
    """升级版引导式拒答话术（RAG_GUIDED_REFUSAL）。两个变体开头都保持"抱歉，…知识库…"
    句式 —— 仍命中 _REFUSAL_STRONG_PATTERN，eval/看板的拒答口径自动兼容。"""
    if titles:
        lines = ["抱歉，知识库中暂时没有找到能直接回答这个问题的资料。", "您是不是想问："]
        lines += [f"· 《{t}》" for t in titles[:3]]
        lines.append(
            "可以换一种说法再试试（例如带上制度、流程或系统的名称）。"
            "如果确认知识库缺少这方面内容，欢迎通过「知识贡献」提交资料，帮助我们完善知识库。"
        )
        return "\n".join(lines)
    hint = f"（如：{rephrase[0]}）" if rephrase else ""
    return (
        "抱歉，当前知识库中未找到与您问题相关的信息。您可以：\n"
        f"· 换一种说法描述问题{hint}\n"
        "· 若知识库缺少该内容，欢迎通过「知识贡献」提交资料\n"
        "· 紧急事项请直接联系对应部门同事确认"
    )


# 知识库失败形态（统一抽象：四条链路的失败分支共用同一分类与决策，不再各自维护）。
# ACL 滤空与真零命中在服务侧不可区分，统一按 EMPTY_RETRIEVAL（话术不泄露权限信息）。
KB_FAILURE_EMPTY = "EMPTY_RETRIEVAL"
KB_FAILURE_REFUSAL = "LLM_REFUSAL"


def classify_kb_failure(
    chunks: Optional[List[Dict[str, Any]]],
    answer_text: Optional[str] = None,
) -> Optional[str]:
    """判定本次知识库回答是否失败及失败形态；成功回答 → None。"""
    if not chunks:
        return KB_FAILURE_EMPTY
    if answer_text is not None and is_refusal_answer(answer_text):
        return KB_FAILURE_REFUSAL
    return None


# 失败决策动作（build_failure_action 返回值的 action 字段）
ACTION_PASSTHROUGH = "passthrough"        # 维持今日行为（原拒答/NO_RESULT 文案原样）
ACTION_GUIDED_REFUSAL = "guided_refusal"  # 替换为 text 字段的引导话术
ACTION_GENERAL_LLM = "general_llm"        # 走 general_answerer（tier 字段给档位）
ACTION_SMALLTALK = "smalltalk"            # 走 canned 模板（调用方取 general_answerer）
ACTION_SENSITIVE_BLOCK = "sensitive_block"  # BLOCKED（risk_blocked=1）


def build_failure_action(
    category: str,
    *,
    mode: str,
    guided_on: bool,
    titles: Optional[List[str]] = None,
    rephrase: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """失败分诊类别 → 处置决策（纯函数；config 快照经参数传入）。

    不变量 I2：category=enterprise 永远只有 guided_refusal/passthrough 两种出路，
    任何 mode 下都不产生 general_llm —— 企业相关未覆盖绝不让通用模型按常识补齐。
    返回 {"action", "text", "tier", "intent_type"}（不适用字段为 None/""）。
    """
    mode = (mode or "off").lower()
    if category == "sensitive":
        return {"action": ACTION_SENSITIVE_BLOCK, "text": SENSITIVE_BLOCK_MESSAGE,
                "tier": "", "intent_type": "refuse_sensitive"}
    if category == "smalltalk" and mode in ("smalltalk", "office", "full"):
        return {"action": ACTION_SMALLTALK, "text": "", "tier": "",
                "intent_type": "smalltalk"}
    if category == "office" and mode in ("office", "full"):
        return {"action": ACTION_GENERAL_LLM, "text": "", "tier": "office",
                "intent_type": "office"}
    if category == "general" and mode == "full":
        return {"action": ACTION_GENERAL_LLM, "text": "", "tier": "general",
                "intent_type": "general"}
    if category in ("office", "general", "smalltalk"):
        # 类别是通用但档位未放开：引导话术开着就给"范围说明"，否则维持现状
        if guided_on:
            return {"action": ACTION_GUIDED_REFUSAL, "text": GENERAL_TIER_OFF_MESSAGE,
                    "tier": "", "intent_type": "refuse_uncovered_general"}
        return {"action": ACTION_PASSTHROUGH, "text": "", "tier": "",
                "intent_type": "refuse_uncovered_general"}
    # enterprise（含一切 fail-closed 回落）：只有引导拒答/维持现状两种出路（I2）
    if guided_on:
        return {"action": ACTION_GUIDED_REFUSAL,
                "text": build_guided_refusal(titles=titles, rephrase=rephrase),
                "tier": "", "intent_type": "refuse_uncovered_enterprise"}
    return {"action": ACTION_PASSTHROUGH, "text": "", "tier": "",
            "intent_type": "refuse_uncovered_enterprise"}


def top_score_of(chunks: Optional[List[Dict[str, Any]]]) -> Optional[float]:
    """检索结果最高分；空/None → None。"""
    if not chunks:
        return None
    return max((c.get("score", 0) for c in chunks), default=None)


def gen_meta_to_json(gen_meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """gen_meta dict → 紧凑 JSON 串（纯函数，P2-20/21/22）。

    None/空 → None（NO_RESULT/错误路径没跑 LLM，没有 prompt 身份 —— 如实缺席）；
    不可序列化（测试 MagicMock config 等）→ default=str 兜底，仍失败则 None ——
    簿记序列化绝不允许炸回答链路。
    """
    if not gen_meta:
        return None
    try:
        return json.dumps(gen_meta, ensure_ascii=False, default=str)
    except Exception:
        return None


def should_append_history(answer_text: Optional[str], answer_status: str) -> bool:
    """统一写历史策略：非空且 SUCCESS / CLIENT_DISCONNECTED 的回答进入会话历史。

    出错时的部分回答不入史 —— 残句进上下文会污染后续轮次（钉钉流式一直如此，
    API 流式原先会把出错前的半截写进去，已统一）。

    CLIENT_DISCONNECTED（SSE 客户端中途断开）例外入史：collected frames = 用户
    实际看到的内容，follow-up 轮的语境应与用户所见一致（区别于出错残句 ——
    用户看到的是错误提示、不是半截回答）。该状态只改落库分桶，不改历史语义。
    """
    return bool(answer_text) and answer_status in ("SUCCESS", "CLIENT_DISCONNECTED")


def history_answer_text(answer_text: str) -> str:
    """统一入史文本策略（#F-mm5, RAG_HISTORY_STRIP_IMG_MARKERS 默认 OFF）。

    ON 时入史前剥掉 <<IMG:N>> 图片标记：历史里的标记会诱导 follow-up 轮 LLM 模仿，
    而 N 按【当前轮】image_map 解析——旧轮的 N 恰落在新轮任一带图 chunk 上即穿透
    referenced-only 的全部防线，附上无关噪声图（与 strip_doc_citations 防「文档N」
    编号模仿是同一根因的同款处理）。四个入史调用点（/api/ask、/api/ask/stream、
    钉钉流式/非流式）统一过本函数；qa_session_log 的 answer_text **不经此函数**
    （日志保真/溯源用原文）。回放侧兜底在 llm_generator._build_messages（覆盖存量
    已污染历史与客户端显式 req.history）。

    ⚠️ 调用方约束：只包裹入史实参，绝不提前改写 result["answer"] —— content_blocks
    构建依赖原始标记（/api/ask 的 blocks 在入史之后才构建）。
    """
    if not answer_text:
        return answer_text
    try:
        from opensearch_pipeline.config import get_config
        if not get_config().rag.history_strip_img_markers:
            return answer_text
        from opensearch_pipeline.content_blocks_builder import strip_image_markers
        return strip_image_markers(answer_text)
    except Exception:
        return answer_text   # fail-open：清洗失败不阻塞入史


def _clip(value: Optional[str], limit: int) -> Optional[str]:
    """审计标识列防御性截断（2026-07-17 ultra P1）：qa_session_log 各 VARCHAR 列在 MySQL
    strict（RDS 默认）下超长直接 1406，而 qa_logger 的兜底阶梯只吞不救——整行审计静默丢失，
    等于调用方可用超长 id 自选逃离溯源/反馈归属/缺口挖掘。截断保行优于丢行；
    合规值逐字节不变（载荷零漂移承诺不受影响）。"""
    return value if value is None or len(value) <= limit else value[:limit]


def build_qa_log_kwargs(
    *,
    session_id: str,
    message_id: str,
    question: str,
    user_id: str = "",
    user_name: Optional[str] = None,
    user_dept: Union[str, List[str], None] = None,
    answer_text: Optional[str] = None,
    chunks: Optional[List[Dict[str, Any]]] = None,
    cited_docs: Optional[List[Dict[str, Any]]] = None,
    latency_ms: int = 0,
    retrieval_latency_ms: Optional[int] = None,
    llm_latency_ms: Optional[int] = None,
    answer_status: str = "SUCCESS",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
    conversation_type: Optional[str] = None,
    content_blocks_json: Optional[str] = None,
    conversation_id: Optional[str] = None,
    gen_meta: Optional[Dict[str, Any]] = None,
    intent_type: Optional[str] = None,
    risk_level: Optional[str] = None,
    risk_blocked: bool = False,
    rewritten_query: Optional[str] = None,
) -> Dict[str, Any]:
    """qa_session_log 载荷的单一组装点。永远返回【全字段】（未知处显式 None，
    与 qa_logger.log_qa_session 的参数缺省一致）。

    chunks 语义：None = 检索未完成（命中数/分数全 None）；[] = NO_RESULT（命中数 0）。

    gen_meta 语义（P2-20/21/22，schema/018）：LLM 成功路径传 llm_generator 返回的
    result["gen_meta"]（prompt_sha/prompt_flags/temperature/top_p/model/retrieval 制度
    快照）；NO_RESULT / 错误路径传 None（没跑 LLM 就没有 prompt 身份，如实缺席优于伪造）。

    answer_status 词表（语料排查按此分桶）：
      SUCCESS   — 正常回答
      REFUSAL   — 拒答型（检索有候选但 LLM 按护栏拒答，is_refusal_answer 判定；
                  客户端同样显示"未找到"）→ 语料弱 / 检索未召回
      NO_RESULT — 检索为空 → 语料缺，补文档的直接信号
      LLM_ERROR — 生成异常（error_message 带 trace_id）
      CLIENT_DISCONNECTED — SSE 客户端中途断开，回答被截断（仅 /api/ask/stream）；
                  不参与 REFUSAL 翻转（截断恰止于拒答句式会假命中），照常入史
      BLOCKED   — 敏感硬红线拦截（通用能力分级开放 T0①；risk_blocked=1 +
                  risk_level='sensitive'，qa_rollup 已按 risk_blocked 归入拒答桶）
    注意：入史策略（should_append_history）按翻转前的原状态判定 —— REFUSAL 只改
    落库标签，不改变"拒答照旧入史"的既有行为。

    intent_type 词表（通用能力分级开放的新维度；NULL = 知识库默认路径 ——
    存量行为不写 'kb'，保证 flag off 时落库载荷逐字节不变）：
      smalltalk / office / general / refuse_sensitive / refuse_uncovered_enterprise /
      refuse_uncovered_general / refuse_realtime / refuse_quota
    企业相关未覆盖仍落 NO_RESULT/REFUSAL（contribution 缺口挖掘与 SLO 口径零漂移），
    只多 intent_type 标签。
    """
    return dict(
        # 标识列按 schema/001 列宽截断（session/message/user_id/user_name=128，user_dept=64）
        session_id=_clip(session_id, 128),
        message_id=_clip(message_id, 128),
        user_id=_clip(user_id, 128),
        user_name=_clip(user_name, 128),
        # 多组：list → CSV（qa_session_log.user_dept 仍 VARCHAR，无迁移）；str/None 原样。
        # 全组用户（总经办 15 组）CSV=104 字符同样超 VARCHAR(64)——不截断则 strict 模式整行丢
        user_dept=_clip(",".join(user_dept) if isinstance(user_dept, list) else user_dept, 64),
        query_text=question,
        answer_text=answer_text or None,
        retrieved_docs=chunks or None,
        cited_docs=cited_docs or None,
        latency_ms=latency_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        llm_latency_ms=llm_latency_ms,
        answer_status=answer_status,
        model_name=model_name,
        error_message=error_message,
        opensearch_hit_count=(len(chunks) if chunks is not None else None),
        top_score=top_score_of(chunks),
        conversation_type=conversation_type,
        content_blocks_json=content_blocks_json or None,
        conversation_id=_clip(conversation_id, 128),
        gen_meta_json=gen_meta_to_json(gen_meta),
        intent_type=intent_type,
        risk_level=risk_level,
        risk_blocked=risk_blocked,
        # 追问改写（RAG_FOLLOWUP_REWRITE，schema/050）：非空=检索实际用的独立问题；
        # None=未改写（flag 关/门控未命中/改写失败），qa_logger 不携带该列，载荷零漂移。
        rewritten_query=_clip(rewritten_query, 1000),
    )
