# -*- coding: utf-8 -*-
"""
retriever.py — 检索模块

封装 DashScope Embedding + OpenSearch HA3 向量检索，为 RAG 问答提供上下文。
"""

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union

from opensearch_pipeline.config import get_config

# HA3 响应解析与 serving 默认输出字段清单已上移至 clients.py（HA3 客户端层，serving 与
# 批处理 parity/对账共用，消除批处理反向依赖 serving 私有名）。此处以旧下划线名 re-export
# 同一对象：既有 tests 的 import 与 monkeypatch 席位（`opensearch_pipeline.retriever
# ._parse_ha3_response` / `_DEFAULT_OUTPUT_FIELDS`）全部保留；绑定恒等由
# tests/test_ha3_client_coupling.py 看住，将来再分叉会立刻红。
from opensearch_pipeline.clients import (
    HA3_DEFAULT_OUTPUT_FIELDS as _DEFAULT_OUTPUT_FIELDS,
    parse_ha3_response as _parse_ha3_response,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 0. Step Card Query Intent Classifier
# ═══════════════════════════════════════════════════════════════

_STEP_INTENT_PATTERNS = [
    # 匹配顺序：窄 → 宽（先精确匹配，避免宽泛模式抢占）
    (
        "specific_step",
        re.compile(
            r"第几步|第\s*\d+\s*步|步骤\s*\d+|下一步|上一步",
        ),
    ),
    (
        "locate_field",
        re.compile(
            r"哪里|在哪|怎么填|填写|按钮|字段|位置|菜单|选项|入口",
        ),
    ),
    (
        "full_procedure",
        re.compile(
            r"如何|流程|怎么操作|怎么做|怎么用|办理|整个|完整|全部步骤|所有步骤",
        ),
    ),
]


def _classify_step_query_intent(query: str) -> str:
    """根据关键词将用户查询分类为 Step Card 检索意图。

    分类结果：
      - ``full_procedure``  — 用户想要完整流程（怎么、如何、流程 …）
      - ``locate_field``    — 用户想定位某个 UI 元素（哪里、在哪、按钮 …）
      - ``specific_step``   — 用户问特定步骤（第N步、下一步 …）
      - ``general``         — 默认兜底

    Returns:
        意图字符串
    """
    for intent, pattern in _STEP_INTENT_PATTERNS:
        if pattern.search(query):
            return intent
    return "general"


# ═══════════════════════════════════════════════════════════════
# 1. Query Embedding
# ═══════════════════════════════════════════════════════════════

# 小 LRU（性能第一梯队 #7）：重复问题（FAQ 快捷栏 / 示例问题 / 多轮同题）免一次
# DashScope 往返（~百 ms）。同输入 → 同 embedding，确定性无正确性风险；失败不缓存。
# RAG_QUERY_EMBED_CACHE_SIZE=0 关闭。conftest 每测清空（_query_embed_cache_clear）。
_query_embed_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_query_embed_cache_lock = threading.Lock()


def _query_embed_cache_size() -> int:
    try:
        return int(os.environ.get("RAG_QUERY_EMBED_CACHE_SIZE", "128"))
    except ValueError:
        return 128


def _query_embed_cache_clear() -> None:
    with _query_embed_cache_lock:
        _query_embed_cache.clear()


def get_query_embedding(
    query: str,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    dimension: Optional[int] = None,
) -> Tuple[List[float], List[int], List[float]]:
    """
    调用 DashScope **native API** 获取 query 的 dense + sparse embedding。

    必须使用 native API 而非 compatible-mode，因为 compatible-mode 不返回 sparse embedding，
    会导致混合检索（dense + sparse）退化为纯 dense 检索，严重影响召回质量。

    Returns:
        (dense_vector, sparse_indices, sparse_values)
    """
    config = get_config()

    resolved_model = model or config.embedding.model
    resolved_dim = dimension or config.embedding.dimension
    cache_size = _query_embed_cache_size()
    cache_key = (query, resolved_model, resolved_dim)
    if cache_size > 0:
        with _query_embed_cache_lock:
            hit = _query_embed_cache.get(cache_key)
            if hit is not None:
                _query_embed_cache.move_to_end(cache_key)
                d, si, sv = hit
                # 返回浅拷贝：命中方若原地改动向量（当前无此调用方）不污染缓存
                return list(d), list(si), list(sv)

    # 与入库侧共用加固实现（URL 去重 + 429/5xx 重试 + 退避）。查询侧 sparse_fallback=False：
    # 空 sparse 表示该查询不参与 sparse 匹配，比塞入 [0]/[0.001] 假项更准确。
    from opensearch_pipeline.embedding_client import embed_texts_native

    # F#59：查询侧独立超时/重试预算。原先沿用入库侧 30s×(2+1) 预算，DashScope 挂起时
    # 每个 /api/ask{,/stream} 的第一步最坏串行阻塞 ~93s（还占死一个线程池线程）。查询侧
    # 收紧为 RAG_QUERY_EMBED_TIMEOUT_S（默认 8s）× RAG_QUERY_EMBED_RETRIES（默认 1，即
    # 最多 2 次尝试）；摄取侧 embedding（pipeline_nodes/embedding_client 调用点）不动。
    try:
        _q_timeout = float(os.environ.get("RAG_QUERY_EMBED_TIMEOUT_S", "8"))
    except ValueError:
        _q_timeout = 8.0
    try:
        _q_retries = int(os.environ.get("RAG_QUERY_EMBED_RETRIES", "1"))
    except ValueError:
        _q_retries = 1

    results = embed_texts_native(
        [query],
        api_key=api_key or config.embedding.api_key,
        model=resolved_model,
        dimension=resolved_dim,
        api_base_url=config.embedding.api_base_url,
        max_retries=_q_retries,
        request_timeout=_q_timeout,
        sparse_fallback=False,
        label="query embedding",
    )
    r = results[0] if results else None
    if r is None:
        raise RuntimeError("DashScope 未返回 query embedding")
    dense, sparse_indices, sparse_values = r

    if cache_size > 0:
        with _query_embed_cache_lock:
            _query_embed_cache[cache_key] = (list(dense), list(sparse_indices), list(sparse_values))
            _query_embed_cache.move_to_end(cache_key)
            while len(_query_embed_cache) > cache_size:
                _query_embed_cache.popitem(last=False)

    logger.debug(
        "Embedding generated: dense=%d dims, sparse=%d nonzero",
        len(dense), len(sparse_indices),
    )
    return dense, sparse_indices, sparse_values


# ── P2-4：查询嵌入降级（DashScope 嵌入中断 ≠ 全部检索硬失败）─────────────────
# RAG_DEGRADED_BM25_ENABLE（默认 true）：查询嵌入失败（超时 / HTTP 错 / 无 key）时
# 不再向上抛，改用【零向量占位 + 纯 BM25 文本检索】继续查 HA3 —— 权限过滤
# （_build_permission_filter，ACL 安全边界）在降级路径【原样保留】，绝不因降级放宽。
# 设 =false 恢复历史行为：嵌入异常原样上抛（/api/ask → 500）。


class _DegradedEmbedding(tuple):
    """标记「降级查询嵌入」的三元组子类（(dense, sparse_idx, sparse_val) 解构完全兼容）。

    dense=零向量仅作 HA3 payload 占位（HA3 knn/query 必须带 vector 字段，[0]*dim 合法，
    见 [[hr-batch-pii-screenshot-quarantine]] 的 id 枚举先例），不参与实际排序——降级时
    knn 路权重清零、只按 BM25 text 路排序。``degraded`` 属性供 search_chunks 识别。
    """

    degraded = True


def _degraded_bm25_enabled() -> bool:
    """RAG_DEGRADED_BM25_ENABLE 开关（默认开；与 config._env_bool 同词表）。"""
    val = os.environ.get("RAG_DEGRADED_BM25_ENABLE", "").strip().lower()
    return val not in ("false", "0", "no")


# 降级告警限流（免刷屏）：持续故障下每 60s 最多一条 ERROR，其余降 DEBUG。
_degraded_log_state = {"ts": 0.0}
_degraded_log_lock = threading.Lock()
_DEGRADED_LOG_INTERVAL_S = 60.0


def _log_degraded_embedding(exc: BaseException) -> None:
    now = time.monotonic()
    with _degraded_log_lock:
        emit = now - _degraded_log_state["ts"] >= _DEGRADED_LOG_INTERVAL_S
        if emit:
            _degraded_log_state["ts"] = now
    if emit:
        logger.error(
            "查询嵌入失败，降级为纯 BM25 文本检索（结果带 degraded_retrieval 标记、"
            "相关度分级失效；RAG_DEGRADED_BM25_ENABLE=false 可关闭降级）: %s", exc,
        )
    else:
        logger.debug("查询嵌入失败（降级路径，ERROR 已限流省略）: %s", exc)


def _get_query_embedding_or_degraded(
    query: str,
) -> Tuple[List[float], List[int], List[float]]:
    """查询嵌入的可降级封装：失败时返回 _DegradedEmbedding（零向量 + 空 sparse）。

    优雅降级铁律：嵌入这类辅助上游故障不得放大为整条检索链路失败——BM25 文本路
    不依赖向量，仍可给出可用（但分级失效）的结果。flag 关闭时保持原 raise 语义。
    经模块全局名调用 get_query_embedding（tests 对 retriever.get_query_embedding 的
    monkeypatch 席位不受影响）。
    """
    try:
        return get_query_embedding(query)
    except Exception as e:  # noqa: BLE001 — 降级路径需覆盖超时/HTTP/配置各类异常
        if not _degraded_bm25_enabled():
            raise
        _log_degraded_embedding(e)
        try:
            dim = int(get_config().embedding.dimension or 1024)
        except Exception:  # noqa: BLE001 — 配置不可用也不阻断降级
            dim = 1024
        return _DegradedEmbedding(([0.0] * dim, [], []))


def _mark_degraded_results(results: List[Dict[str, Any]]) -> None:
    """P2-4：给降级检索结果打标（原地修改）。

    - ``degraded_retrieval=True``：让上游（api / llm_generator / 前端）可见本次命中
      来自降级检索。
    - 分级失效处理：score_level 的 高/中/低 阈值（7.7/5.8）按 weighted 融合分标定，
      降级分＝纯 BM25 分，量纲不可比——不伪造校准分：引擎原始分【保真】挪到
      ``degraded_raw_score``，``score`` 置 0.0 → llm_generator.score_level 恒判「低」、
      is_low_confidence_band 恒 True（降级结果本就应触发低置信提示）。下游顺序不受
      影响：expand 的组间排序 / _select_with_doc_cap 对全 0 分稳定（保持命中顺序）。
    """
    for r in results:
        if not isinstance(r, dict):
            continue
        r["degraded_retrieval"] = True
        r["degraded_raw_score"] = r.get("score", 0)
        r["score"] = 0.0


# ═══════════════════════════════════════════════════════════════
# 2. HA3 Vector Search
# ═══════════════════════════════════════════════════════════════

_ha3_client = None


def _get_ha3_client():
    """懒初始化 HA3 客户端（单例）。"""
    global _ha3_client
    if _ha3_client is not None:
        return _ha3_client

    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config

    cfg = get_config().alibaba_vector
    if not cfg.endpoint:
        raise RuntimeError("HA3 endpoint 未配置，无法进行向量检索")

    clean_endpoint = cfg.endpoint.replace("http://", "").replace("https://", "")

    ha3_config = Config(
        endpoint=clean_endpoint,
        instance_id=cfg.instance_id,
        access_user_name=cfg.access_user_name,
        access_pass_word=cfg.access_pass_word,
    )
    _ha3_client = Client(ha3_config)
    logger.info("HA3 client initialized: endpoint=%s", clean_endpoint)
    return _ha3_client


# （_parse_ha3_response 见文件头部：re-export 自 clients.parse_ha3_response）


def _escape_ha3_query(text: str) -> str:
    """转义 HA3 queryString 中的特殊字符，防止查询语法注入。

    HA3 query 语法中单引号 ' 用于包裹查询词，用户输入的单引号会
    破坏语法结构。反斜杠 \\ 和双引号 " 也需转义。
    """
    # 移除单引号（HA3 不支持引号内转义，只能剥离）
    text = text.replace("'", " ")
    # 转义反斜杠和双引号
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text.strip()


def _sanitize_ha3_filter_value(value: str) -> str:
    """清理 HA3 filter 表达式中的字段值，防止过滤条件注入。

    HA3 filter 中双引号用于包裹值，攻击者可通过注入双引号闭合值边界
    并追加额外过滤条件（如绕过 permission_level 限制）。

    策略：仅保留字母、数字、下划线、连字符、中文字符，剥离所有其他字符。
    这比转义更安全，因为 HA3 filter 语法中引号内的转义行为未明确文档化。
    """
    import re
    # 白名单：部门代码通常是字母数字+下划线+连字符+中文
    return re.sub(r'[^\w\-\u4e00-\u9fff]', '', value)


# （_DEFAULT_OUTPUT_FIELDS 见文件头部：re-export 自 clients.HA3_DEFAULT_OUTPUT_FIELDS）


# 合法 ACL 权限组白名单（单一来源；H2 防御纵深）。
# ⚠️ 语义：这些代码承载的是"ACL 权限组"，不是组织部门——一个组织部门可映射到多个组，
# 映射见 dingtalk_identity._DEPT_NAME_TO_GROUPS。字段名沿用历史的 dept/owner_dept/user_dept。
# 2026-07-03 扩容 5 组（用户拍板，映射见 dept_ancestry 锚表）：overseas=海外中心（自有组，
# 叠加 production 可读）、audit=审计、legal=法务、engineering=工程、corn_eco=玉米环保。
# 新组当日无 chunk：检索侧零影响，直到有文档以该 owner_dept 入库。
_VALID_ACL_GROUPS = frozenset({
    "finance", "it", "marketing", "production",
    "pmc", "admin", "hr", "rd", "quality", "supply",
    "overseas", "audit", "legal", "engineering", "corn_eco",
})

# ── Owner taxonomy (resource-side) vs user-facing ACL groups ──────────────────
# A chunk's owner_dept is the CONTENT owner and KEEPS subline granularity
# (e.g. production_mold / production_paper_cup / production_thermoforming) — it is
# never rewritten to the umbrella. _VALID_ACL_GROUPS above are the USER-facing
# groups. The two are deliberately separate.
#
# 'production' is an UMBRELLA group: a user holding it may read dept_internal
# content owned by 'production' OR any approved production subline. Every other
# group maps to EXACTLY itself (exact-match, unchanged). The mapping is a
# taxonomy-driven EXPLICIT allow-list (NOT an open startswith): a production-like
# owner not listed here is NOT granted (fail-closed) and is surfaced by
# audit_production_owner_taxonomy(). Add a new subline here (single source of truth).
# Only APPROVED + real owners. Unapproved production_* owners (incl. the
# production_papercup double-spelling) are deliberately excluded → they fail closed
# and surface via audit_production_owner_taxonomy() until explicitly approved + added
# here. Mirrors the approved OSS raw/production_*/ directory taxonomy: injection and
# straw were approved 2026-07-16 (both dirs exist in OSS with registered docs; without
# this entry any future dept_internal doc ingested from them would silently vanish
# from production users' retrieval — the fail-closed trap found in the 07-16
# subline exploration, docs/production_subline_exploration_2026-07-16_DRAFT.md).
_PRODUCTION_UMBRELLA_OWNERS = frozenset({
    "production",                 # the umbrella owner itself (exact)
    "production_injection",
    "production_mold",
    "production_paper_cup",
    "production_straw",
    "production_thermoforming",
    # 2026-07-20 Sam 拍板开通(07-16 全景缓办项;目录随首篇上传自动落位,不预建 OSS):
    "production_blown_film",      # 吹膜车间(172 人)
    "production_carton",          # 纸箱车间(169 人)
    "production_pulp_molding",    # 纸浆模塑事业部(92 人)
    # 同批拍板【不】开通的(记录在案,防止后人当遗漏):包装车间→归伞值 production
    # (横跨产品线);三级部门(车间下属机修/班组长/料房等)一律归伞;海外产线归属
    # 口径仍未拍板(overseas 用户组已可叠读 production)。
})
# user-facing group -> owner_dept set it grants. Absent group => exact {group}.
#
# Production+Marketing shared-access policy (2026-06-21): production-family dept_internal
# docs must be readable by BOTH the 'production' umbrella AND the 'marketing' group.
# owner_dept stays the REAL subline (never normalized to production, never rewritten to
# marketing, never duplicated). Access is granted SUBJECT-side: 'marketing' is expanded to
# also cover the production-family owners. This expresses effective_access_groups=
# ["production","marketing"] for production-family content under the existing owner_dept-
# equality filter — valid because the policy is UNIFORM (every production-family doc shares
# the same access set). It is asymmetric by design: marketing → can read production-family +
# its own marketing; production → reads production-family only (NOT marketing docs), per 权限单.
# (Per-document access variation would instead require a resource-side access_groups field.)
_DEPT_OWNER_EXPANSION = {
    "production": _PRODUCTION_UMBRELLA_OWNERS,
    "marketing": frozenset({"marketing"}) | _PRODUCTION_UMBRELLA_OWNERS,
}


def _expand_groups_to_owners(groups: List[str]) -> List[str]:
    """Map normalized user ACL groups → the owner_dept values they may retrieve.

    'production' umbrella expands to all approved production* sublines; 'marketing' expands
    to itself + the production family (Production+Marketing shared-access policy). Every
    OTHER group maps to exactly itself (exact-match — unchanged for non-production depts).
    Returns a sorted, de-duped list. Inputs are already sanitized + whitelisted by
    _normalize_acl_groups and outputs are taxonomy constants, so the result is injection-safe.
    """
    owners = set()
    for g in groups:
        owners |= set(_DEPT_OWNER_EXPANSION.get(g, (g,)))
    return sorted(owners)


def groups_that_can_read_owner(owner_dept: str) -> List[str]:
    """`_expand_groups_to_owners` 的**反向**：owner_dept → 能检索到它的【用户组码】集合。

    为什么必须有这个函数（2026-07-31，codex 实测）：**owner 值域 ≠ 组码值域**。
    `production_mold` 是合法 owner，但不是用户组码 —— 把它当组码送进过滤器只会得到
    public-only（组码白名单会丢掉它），能读该 owner 的组码其实是 `production`/`marketing`。
    任何"按文档 owner 反推应当能看到它的人"的场景（如 `ha3_verify` 的自查身份）都必须走
    这里，**绝不在调用方复制一份 production/marketing taxonomy**。

    返回排序去重列表；owner 为空或无人可读 ⇒ 空列表（调用方按 fail-closed 处置）。
    """
    owner_dept = (owner_dept or "").strip()      # 自行规范化，不指望调用方记得
    if not owner_dept:
        return []
    out = {g for g, owners in _DEPT_OWNER_EXPANSION.items() if owner_dept in owners}
    if owner_dept in _VALID_ACL_GROUPS:
        # 未进展开表的部门：组码就是 owner 自身（精确匹配）。**必须过白名单** ——
        # 否则 production_mold 这类"只是 owner"的值会被当成组码传出去，等于查了个寂寞。
        out.add(owner_dept)
    return sorted(out)


def audit_production_owner_taxonomy(active_owner_depts) -> List[str]:
    """Surface production-like owner_dept values present in data but NOT in the umbrella
    taxonomy. Such owners are invisible to 'production' users (fail-closed) until added
    to _PRODUCTION_UMBRELLA_OWNERS — this never auto-includes them. Read-only; logs a
    warning and returns the suspicious set (also catches malformed 'productionx' shapes).
    """
    known = _PRODUCTION_UMBRELLA_OWNERS
    suspicious = sorted({
        o for o in (active_owner_depts or [])
        if o and o not in known and str(o).startswith("production")
    })
    if suspicious:
        logger.warning(
            "Unrecognized production-like owner_dept NOT in umbrella taxonomy "
            "(fail-closed: invisible to 'production' users; add to _PRODUCTION_UMBRELLA_OWNERS "
            "if legitimate): %s", suspicious,
        )
    return suspicious


def _normalize_acl_groups(user_dept: Union[str, List[str], None]) -> List[str]:
    """把任意形态的部门/组入参归一为干净、去重、白名单内的 ACL 组列表（单一归一点）。

    每个元素先 _sanitize_ha3_filter_value 净化，再过 _VALID_ACL_GROUPS 白名单。
    fail-closed：空 / None / 全空白 / 全非法 → []（→ 仅 public 可见，绝不 fail-open）。
    接受形态：单字符串、逗号分隔字符串、列表（列表元素本身也可含逗号）。
    """
    if not user_dept:
        return []
    raw: List[str] = []
    if isinstance(user_dept, str):
        raw = user_dept.split(",")
    else:
        for item in user_dept:
            if item is None:
                continue
            raw.extend(str(item).split(","))
    out: List[str] = []
    seen = set()
    for d in raw:
        code = _sanitize_ha3_filter_value(d.strip())
        if code and code in _VALID_ACL_GROUPS and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _node_acl_flags() -> Tuple[bool, bool]:
    """(grant, enforce)。启动/首用即校验姿态:GRANT=true && ENFORCE=false 非法。"""
    from opensearch_pipeline.acl_policy import assert_flag_posture
    cfg = get_config().rag
    grant, enforce = bool(cfg.node_acl_grant), bool(cfg.node_acl_enforce)
    assert_flag_posture(grant_enabled=grant, enforce_enabled=enforce)
    return grant, enforce


def _build_permission_filter(user_dept: Union[str, List[str], None], *, acl_ctx=None) -> str:
    """构建 HA3 权限过滤表达式（安全边界，单一实现）。

    放行 public；对用户所属的每个 ACL 组额外放行该组的 dept_internal。入参经
    _normalize_acl_groups（净化 + 白名单 + 去重）后才进表达式；为空 → 仅 public（fail-closed）。
    所有调用点（search_chunks / cosurface_doc_images / 本地回退）共用，权限规则只有一处。
    完整括号包裹每个子句，避免 HA3 对 AND/OR 优先级的歧义。
    'production' 伞组经 _expand_groups_to_owners 展开为各 production* 子线 owner（其余组精确匹配）。
    """
    groups = _normalize_acl_groups(user_dept)
    # node-ACL 分支【必须先于空-groups 早退算出来】（2026-07-31，codex 共识）：
    # 组码通道与节点通道是**两条独立通道** —— `acl_policy.can_read_doc` 的 node 分支
    # 压根不读 `ctx.groups`（只看 node_channel_ok / org_wide_reader / 祖先链∩授权节点）。
    # 原实现在 groups 为空时直接返回 public-only，导致**过滤器比权威判定更严**：
    # 无组码映射的员工（现网部门映射缺口 26/131）即便被节点授权也只看得到 public。
    # 修正后语义只在 (groups 空) ∧ (acl_ctx 非空) ∧ (GRANT=on) ∧ (节点项非空) 时变化，
    # 且该集合正是 can_read_doc 已经放行的集合 ⇒ 是**对齐**，不是放宽。
    node_extra = _node_filter_suffix(acl_ctx)
    if not groups:
        return '(permission_level="public")' + node_extra
    # groups 已净化+白名单；owners 为 taxonomy 常量（伞组展开），字符串拼接无注入风险
    owners = _expand_groups_to_owners(groups)
    dept_clause = " OR ".join('owner_dept="' + o + '"' for o in owners)
    base = (
        '(permission_level="public")'
        ' OR (permission_level="dept_internal" AND (' + dept_clause + '))'
    )
    # Phase D（RAG_ALLOWED_DEPTS_ACL，默认关）：跨部门检索授权——文档 allowed_depts 含调用者
    # 任一【组码】（用 groups 本身，非 _expand_groups_to_owners 的 owner 展开；allowed_depts 存
    # 组码、按组匹配）即放行该 dept_internal 文档。与 dept_internal AND 绑定 → public/restricted
    # 不受影响、restricted 永不放行；allowed_depts 仅授权文档有值 → 零越权扩散；组码已净化+白名单，
    # 无注入。仅在 base 末尾整体括号化追加一个 OR 分支，不改既有任一子句 → flag 关时返回串与历史
    # 逐字节一致。HA3 多值字段 `allowed_depts="g"` = 数组成员匹配（Phase D Step 0 实证）。
    if get_config().rag.allowed_depts_acl:
        allowed_clause = " OR ".join('allowed_depts="' + g + '"' for g in groups)
        base = base + ' OR (permission_level="dept_internal" AND (' + allowed_clause + '))'
    return base + node_extra


def _node_filter_suffix(acl_ctx) -> str:
    """node-ACL 追加分支（RAG_NODE_ACL_GRANT，默认关）→ 待拼在过滤表达式末尾的后缀。

    `acl_ctx=None` ⇒ 恒返回 `""` ⇒ `_build_permission_filter` 的两条返回路径都与历史
    **逐字节一致**（全部 legacy 调用点、flag 关时的全部调用点都落在这里）。

    ⚠️ 节点值由 acl_policy 自行严格拼接，**绝不流经 _sanitize_ha3_filter_value**
      （该净化器会删掉冒号，d:123 会被打成 d123）。值域只含 d:/dx: + 正整数，无注入面。
    org_wide_reader（总经办 `*`）：单靠事后 can_read_doc 放行不够——HA3 主过滤器压根
      不会把 node 文档召回来（其 owner 是哨兵、祖先链也不含被勾节点），事后判定轮不到
      执行。故给该角色一条候选分支：直接放行全部 dept_internal，召回后统一走权威复核。
      ⚠️ 该分支**必须与 can_read_doc 同样先过 node_channel_ok**（acl_policy.py:283 在
      org_wide_reader 之前判它）：组织快照不可信时连节点语义都不成立，此时放出「全部
      dept_internal」候选只会把判定压力全推给事后复核（2026-07-31 codex 评审）。
    """
    if acl_ctx is None:
        return ""
    grant, _enforce = _node_acl_flags()
    if not grant:
        return ""
    if not getattr(acl_ctx, "node_channel_ok", False):
        return ""                       # 祖先链不可信 ⇒ 无节点通道（与 can_read_doc 同序）
    if getattr(acl_ctx, "org_wide_reader", False):
        return ' OR (permission_level="dept_internal")'
    from opensearch_pipeline.acl_policy import node_filter_terms
    terms = node_filter_terms(acl_ctx)
    if not terms:
        return ""
    node_clause = " OR ".join('allowed_depts="' + t + '"' for t in terms)
    return ' OR (permission_level="dept_internal" AND (' + node_clause + '))'


# ── E#39：查询侧授权复核的 (doc_id → approved 组码集) 进程内 TTL 缓存 ──────────
# 默认 RAG_ACL_DENY_CACHE_TTL_S=0 = 关闭（每次都查权威表，与历史行为一致）——该复核是
# 机密性 fail-closed 兜底，缓存必须保守启用。--workers 1 单进程部署下天然一致；
# routes/kb_access 的 approve/reject/revoke 提交成功后调 invalidate_deny_cache 主动失效，
# 保住「撤销即时生效」语义（跨进程写方如 allowed_depts_reconcile 由 TTL 兜底）。
_deny_cache: "OrderedDict[str, Tuple[float, frozenset]]" = OrderedDict()
_deny_cache_lock = threading.Lock()
_DENY_CACHE_MAX = 2048   # 容量上限（防 doc_id 长尾无界增长；TTL=0 时缓存恒空）


def _deny_cache_ttl_s() -> float:
    try:
        return float(os.environ.get("RAG_ACL_DENY_CACHE_TTL_S", "0"))
    except ValueError:
        return 0.0


def invalidate_deny_cache(doc_id: Optional[str] = None) -> None:
    """失效查询侧授权复核缓存：doc_id=None 清空全部。授权投影变更端点提交成功后调用。"""
    with _deny_cache_lock:
        if doc_id is None:
            _deny_cache.clear()
        else:
            _deny_cache.pop(doc_id, None)


def _deny_revoked_node_hits(results, acl_ctx, cand_idx, owner_set):
    """node-ACL 的查询侧复核：对【全部非 public 命中】按当前权威 + 真值表判定（mode-dispatch）。

    ⚠️ 为什么候选是「全部非 public」而不是「跨部门」（2026-07-31 codex 共识，实测复现）：
    一篇 RDS 权威已切 node 的文档，在投影未收敛期 HA3 里可能**还是旧的真实 owner**。
    若该 owner 恰好等于调用者的组码，旧口径（owner ∉ 自有集才算候选）就**不会复核它**，
    于是 `can_read_doc` 根本没机会执行 —— 设计稿 §8 承诺的「关 GRANT ⇒ node 文档非 public
    命中全部被拒 = 真 public-only」当场落空。实测：
        GRANT=false + 陈旧 owner 'hr' + 调用者组码 'hr' ⇒ 文档仍被投放。

    ⚠️ 过渡期**不能靠哨兵识别 node 命中**：`acl_mode` 不进 HA3，未收敛投影里哨兵尚未写下
    ⇒ 必须对全部候选**批量查当前 acl_mode**（与权威解析一次往返合并，不额外加 DB 往返）。

    **mode-dispatch（刻意不对全部候选无差别调 can_read_doc）**：
      · mode 不可判（缺行 / 未知 mode / 权威不可达）⇒ 丢弃
      · node    ⇒ 恒走 `can_read_doc`（这正是本函数存在的理由）
      · legacy 且 owner ∈ 调用者自有集 ⇒ **保留**，维持旧 same-owner 语义
      · legacy 且跨部门          ⇒ 走 `can_read_doc`（等价于既有 legacy 授权复核）
    为什么 legacy same-owner 不一并复核：那会新增「owner 漂移即拒 / permission 暂时不一致
    即拒」等本批次不需要的 legacy 行为扩面，而 same-owner 本就不是可撤销的组码 grant。

    返回需要丢弃的下标集合。权威不可达 ⇒ 丢弃**全部**候选（fail-closed，机密性优先）。
    """
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, can_read_doc
    grant, enforce = _node_acl_flags()
    doc_ids = {results[i].get("doc_id") for i in cand_idx if results[i].get("doc_id")}
    if not doc_ids:
        # 候选都没有 doc_id ⇒ 无从按权威判定 ⇒ fail-closed 全丢（不是「没候选」）
        return set(cand_idx)
    try:
        from opensearch_pipeline.access_grants import resolve_doc_acl
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # strict=True：capability/节点权威读失败一律上抛，缺行/未知 mode 不返回该 doc。
                # 绝不能让「读不到」退化成「按 legacy 放行」——那正是 stale-owner 窗口。
                acls = resolve_doc_acl(doc_ids, cur, strict=True)
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 — 权威不可达 ⇒ fail-closed 丢弃全部非 public 候选
        logger.warning("node-ACL 查询侧复核失败,fail-closed 丢弃 %d 条非 public 命中: %s",
                       len(cand_idx), e)
        return set(cand_idx)
    drop = set()
    for i in cand_idx:
        acl = acls.get(results[i].get("doc_id"))
        if acl is None:                       # 缺行 / 未知 mode（strict 下不返回）
            drop.add(i)
            continue
        if (getattr(acl, "mode", "") or "").strip().lower() == ACL_MODE_NODE:
            if not can_read_doc(acl_ctx, acl, grant_enabled=grant, enforce_enabled=enforce):
                drop.add(i)
            continue
        # ── 确认 legacy ──
        if (results[i].get("owner_dept") or "") in owner_set:
            continue                          # same-owner:保持旧语义，不新增拒绝面
        if not can_read_doc(acl_ctx, acl, grant_enabled=grant, enforce_enabled=enforce):
            drop.add(i)
    if drop:
        logger.info("node-ACL 查询侧拒绝:丢弃 %d 条无在册授权的命中", len(drop))
    return drop


def _deny_revoked_cross_dept(results, user_dept, *, acl_ctx=None):
    """查询侧拒绝（Phase D 读侧 fail-closed 复核）——撤销即时生效，不等 HA3 投影收回。

    HA3 的 allowed_depts 过滤依赖【投影】（chunk_meta→HA3，由 stage-3 drain 物化）。撤销跨部门授权后
    投影可能滞后（drain 未跑），残留授权会让被撤销部门仍检索到该文档。本函数对【跨部门命中】——
    permission_level='dept_internal' 且 owner_dept 不在调用者自有 owner 集（这类只可能经 allowed_depts
    分支进来）——按【权威表】kb_access_request(status='approved') 再核一次：无在册 approved 授权 → 丢弃。

    fail-closed：权威查询异常 → 丢弃【全部】跨部门命中（拒绝），保留同部门/public 命中（常见路径不受影响）。
    flag 关 / 无结果 / 无跨部门命中 → 原样返回（零开销，不建连）。与 _build_permission_filter 的 allowed_depts
    分支配套：投影是快路径，本复核是 fail-closed 兜底，二者口径一致（按【组码】匹配授权）。
    E#39：RAG_ACL_DENY_CACHE_TTL_S>0 时启用 (doc_id → approved 组码集) 进程内 TTL 缓存
    （默认 0=关闭），decide 端点提交后主动失效（invalidate_deny_cache）。
    """
    if not results:
        return results
    norm = _normalize_acl_groups(user_dept)
    # ⚠️ node-ACL 分支【必须先于 legacy flag 判定】(2026-07-31 codex 共识)：
    # 原实现把整段复核挂在 `RAG_ALLOWED_DEPTS_ACL` 之下（代码默认 false），于是
    # node ENFORCE 这条**设计上常开**的闸门会随一个 legacy 开关一起被关掉 ——
    # 设计稿 §8 明确要求 ENFORCE 不得随正向开关关闭。
    if acl_ctx is not None:
        # 候选 = **全部非 public 命中**（不要求 owner 非空、不排除 owner 属自有集）。
        # 理由见 `_deny_revoked_node_hits` 的 docstring：陈旧真实 owner 恰等于调用者组码时，
        # 旧的「跨部门」口径会整条漏判。判定本身仍按 mode 分派，legacy same-owner 语义不变。
        _owners = set(_expand_groups_to_owners(norm))
        _cand = [i for i, r in enumerate(results) if r.get("permission_level") != "public"]
        if not _cand:
            return results
        _drop = _deny_revoked_node_hits(results, acl_ctx, _cand, _owners)
        return [r for i, r in enumerate(results) if i not in _drop]
    if not get_config().rag.allowed_depts_acl:
        return results
    groups = set(norm)
    owner_set = set(_expand_groups_to_owners(norm))
    # P2-02：从「仅 dept_internal」放宽到「任何非 public」——这样字段漂移→restricted 兜底的
    # owner-不匹配命中也会走下面的 fail-closed 授权复核（查不到授权即丢弃），而非被当 public 放行。
    # 真实 restricted 本就被引擎端过滤不返回，故此放宽只作用于字段漂移命中，正常 dept_internal 语义不变。
    cross_idx = [
        i for i, r in enumerate(results)
        if r.get("permission_level") != "public"
        and r.get("owner_dept") and r.get("owner_dept") not in owner_set
    ]
    if not cross_idx:
        return results
    cross_doc_ids = {results[i].get("doc_id") for i in cross_idx if results[i].get("doc_id")}
    # E#39：TTL>0 时先查进程内缓存，仅对 miss 的 doc 查权威表（全命中则完全不建连）；
    # TTL=0（默认）跳过缓存、每次查库——与历史行为一致。DB 失败沿用现有 fail-closed 语义。
    ttl = _deny_cache_ttl_s()
    authorized: Dict[str, frozenset] = {}
    missing = set(cross_doc_ids)
    if ttl > 0 and missing:
        now = time.monotonic()
        with _deny_cache_lock:
            for d in list(missing):
                hit = _deny_cache.get(d)
                if hit is not None and now - hit[0] < ttl:
                    authorized[d] = hit[1]
                    missing.discard(d)
    if missing:
        try:
            from opensearch_pipeline.db import _get_db_conn
            from opensearch_pipeline.access_grants import resolve_allowed_depts
            conn = _get_db_conn()
            try:
                with conn.cursor() as cur:
                    fetched = resolve_allowed_depts(missing, cur)   # {doc_id: [approved 组码]}
            finally:
                conn.close()
        except Exception as e:   # noqa: BLE001 — 权威不可达 → fail-closed 丢弃全部跨部门命中（机密性优先）
            logger.warning("查询侧授权复核失败，fail-closed 丢弃 %d 条跨部门命中: %s", len(cross_idx), e)
            drop = set(cross_idx)
            return [r for i, r in enumerate(results) if i not in drop]
        for d in missing:
            grants = frozenset(fetched.get(d, ()))   # 负结果（无授权）同样缓存，撤销后由失效/TTL 收敛
            authorized[d] = grants
            if ttl > 0:
                with _deny_cache_lock:
                    _deny_cache[d] = (time.monotonic(), grants)
                    _deny_cache.move_to_end(d)
                    while len(_deny_cache) > _DENY_CACHE_MAX:
                        _deny_cache.popitem(last=False)
    drop = {
        i for i in cross_idx
        if not (groups & set(authorized.get(results[i].get("doc_id"), ())))
    }
    if drop:
        logger.info("查询侧拒绝：丢弃 %d 条已撤销/无在册授权的跨部门命中", len(drop))
    return [r for i, r in enumerate(results) if i not in drop]


def _acl_fail_closed() -> bool:
    """P0-04（报告1）ACL 严格模式总开关。默认 **off** = 现网行为不变（权威不可用时
    fail-open 保留原结果）。开启后主命中 RDS 复核不可用时只保留 public（公司公开）命中，
    绝不把 HA3 投影当权威投放受限内容——RDS 抖动期会牺牲部门内容可用性换安全，
    故默认 off、须显式灰度（报告称此项可 P1、需业务签字）。"""
    return os.environ.get("RAG_ACL_FAIL_CLOSED", "").strip().lower() in ("1", "true", "yes", "on")


def _keep_public_only_if_strict(results, reason: str):
    """严格模式下权威复核不可用 → 只留 public 命中（报告：至少丢弃非公司公开内容）；
    非严格（默认）→ 原样保留（历史 fail-open）。"""
    if not _acl_fail_closed():
        return results
    kept = [r for r in results if str(r.get("permission_level") or "") == "public"]
    if len(kept) != len(results):
        logger.warning("ACL fail-closed（%s）：权威复核不可用，丢弃 %d 非 public 主命中，仅留 %d public",
                       reason, len(results) - len(kept), len(kept))
    return kept


def _revalidate_main_hits(results, *, strict_on_unavailable: bool = False):
    """主命中 RDS 复核（盲区审计 P3-1）——权限执行不对称的补齐。

    strict_on_unavailable（2026-08-03，cosurface 补图用）：**权威复核没跑成**时（RDS 不可达 /
    返回空集 / 两个轴都没有键）直接返回空集，而不是 fail-open 保留。
    ⚠️ 2026-08-06 补齐两处**逐行**漏网（此前 strict 只在"整批"层面生效）：
      · 混合批里单条无键的行不再无条件保留（原 `[KEYLESS, KEYED]` → 两条全过）；
      · 缺 HA3`id` 的行不再跳过物理 PK 轴（缺字段本就是"未复核"的一种）。
    仅对"可选增强"路径开启：
    主命中 fail-open 的理由是"不把 RDS 故障放大成全站无答案"，但**补图失败的代价只是没有配图，
    正文答案不受影响** —— 那里没有任何理由拿未复核的图去赌。
    ⚠️ 只作用于"复核**没跑成**"，不作用于"复核**按配置不跑**"（`main_hit_revalidate` 关、
    `simulate_db`）—— 后者是运维/测试的显式选择，不该让补图比主命中更严。

    邻居拼接/step 扩展路径一直按 RDS 复核（is_active=1 + _same_permission），而**主 HA3
    命中**此前直接投放：任何 RDS→HA3 投影延迟（管理员收紧 public→dept_internal、下线
    置 is_active=0、版本停用后 HA3 删除滞后）窗口内，旧值按旧 ACL 被逐字投放给 LLM。
    本函数按权威表 chunk_meta 复核每个主命中，丢弃：
      - is_active=0（已停用：retire / 旧版本 / PENDING_DELETE 延迟）
      - permission_level / owner_dept 与 HA3 投影不一致（投影陈旧 → 不投放，等 drain 收敛）
    有意**不**比对 allowed_depts：新增跨部门授权先落 RDS 后投影，比对会把授权变更放大成
    全 doc 一天不可检索；撤销方向已由 _deny_revoked_cross_dept 按权威表 fail-closed 兜底。

    失败语义：权威表不可达 / 返回整体为空（几乎必然是连错库/桩连接而非全部命中同时失效）
    → 保留原结果并告警。HA3 服务端过滤是第一道边界，本检查只是投影延迟的防御纵深，
    不把 RDS 故障放大为全站无答案（与本模块 stitch/expand 的 fail-open 风格一致）。

    B7（2026-07-25）追加**物理 PK 轴**：同版本重切（node_write_chunk_meta 按
    (doc_id, version_no) 全量 DELETE→INSERT）会给同一 chunk_id 重新分配 chunk_meta.id
    ——也就是 HA3 的主键。旧 PK 就此成为孤儿：它既不在 deactivate 的覆盖面（那按
    version_no < N 选行，同版本重切时旧行连 id 都不在 RDS 了），也不在按 version 删的
    PENDING_DELETE 里。孤儿行的 chunk_id / is_active / permission 全都对得上，只按
    chunk_id 复核会**原样放行**，于是被修文档的旧文本可能被当作现行内容投给 LLM。
    改为 (chunk_id, HA3 返回的 id) 双轴比对，不一致即丢弃。
    """
    cfg = get_config()
    if not cfg.rag.main_hit_revalidate or not results or cfg.simulate_db:
        return results
    # 两个轴**分开收集**：此前 `r.get("chunk_id") or r.get("id")` 把两者压进同一个列表再按
    # chunk_id 查，于是"chunk_id 为空、只有 HA3 id"的历史命中永远查不到（所谓 id 兜底是假的）。
    cid_axis = sorted({str(r.get("chunk_id")) for r in results if r.get("chunk_id")})
    pk_axis = sorted({str(r.get("id")) for r in results
                      if not r.get("chunk_id") and r.get("id") not in (None, "")})
    if not cid_axis and not pk_axis:
        return [] if strict_on_unavailable else results
    try:
        from opensearch_pipeline.db import _get_db_conn   # 惰性：tests monkeypatch db._get_db_conn
        conn = _get_db_conn()
        try:
            rows = []
            with conn.cursor() as cur:
                if cid_axis:
                    ph = ",".join(["%s"] * len(cid_axis))
                    cur.execute(
                        "SELECT chunk_id, is_active, permission_level, owner_dept, id"
                        f" FROM chunk_meta WHERE chunk_id IN ({ph})", tuple(cid_axis))
                    rows.extend(cur.fetchall() or [])
                if pk_axis:
                    # chunk_id 为空的历史行：按物理 PK 兜底查，否则上面那条查不到它们
                    ph2 = ",".join(["%s"] * len(pk_axis))
                    cur.execute(
                        "SELECT chunk_id, is_active, permission_level, owner_dept, id"
                        f" FROM chunk_meta WHERE id IN ({ph2})", tuple(pk_axis))
                    rows.extend(cur.fetchall() or [])
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 — 权威不可达 → 默认保留（HA3 是第一道边界）；strict 只留 public
        logger.warning("主命中 RDS 复核失败（HA3 结果）: %s", e)
        if strict_on_unavailable:
            return []
        return _keep_public_only_if_strict(results, "RDS 不可达")
    if not rows:
        logger.warning("主命中 RDS 复核返回空集（%d 个键全部未知）——按权威不可用处理",
                       len(cid_axis) + len(pk_axis))
        if strict_on_unavailable:
            return []
        return _keep_public_only_if_strict(results, "复核返回空集")
    # r[4]=chunk_meta.id；对不含该列的行形态（旧桩/降级查询）取 None，下面据此跳过 PK 比对
    def _row(r):
        return (r[1], r[2], r[3], r[4] if len(r) > 4 else None)

    meta = {str(r[0]): _row(r) for r in rows if r[0]}
    meta_by_pk = {str(r[4]): _row(r) for r in rows if len(r) > 4 and r[4] is not None}
    kept, dropped = [], 0
    for r in results:
        cid = str(r.get("chunk_id") or "")
        row = meta.get(cid) if cid else meta_by_pk.get(str(r.get("id") or ""))
        if row is None and not cid and not r.get("id"):
            # 两个轴都没有键 → 无从复核。
            # 🔴 strict 档下**逐行**也要 fail-closed（2026-08-06 修）：此前只有「整批无键」
            # 走 strict 返空集，混合批里单条无键的行照样 `kept.append` 无条件保留 ——
            # 实测 `[KEYLESS(A), KEYED(B)]` → `['A','B']`，strict 对它完全失效。
            if strict_on_unavailable:
                dropped += 1
                continue
            kept.append(r)     # 非 strict：沿用既有 fail-open
            continue
        if row is None:
            dropped += 1       # RDS 已无此 chunk（purge/删除），HA3 残留
            continue
        active, perm, owner, rds_pk = row
        if not active:
            dropped += 1
            continue
        if (str(perm or "") != str(r.get("permission_level") or "")
                or str(owner or "") != str(r.get("owner_dept") or "")):
            dropped += 1       # 投影陈旧：ACL 轴不一致，不投放
            continue
        # B7：物理 PK 轴。HA3 返回的 id 与 RDS 现行 chunk_meta.id 不一致 ⇒ 同版本重切留下的
        # 孤儿行（旧文本），丢弃。**HA3 id 缺失时只跳过这一项比对**、不丢弃命中 ——
        # 历史行没有该字段，因缺字段整条丢会把正常内容一起打掉。
        _ha3_pk = r.get("id")
        if _ha3_pk in (None, "") and strict_on_unavailable:
            # 🔴 strict 档下缺 HA3 id **不再跳过** PK 轴（2026-08-06 修）：跳过等于对
            # 「同版本重切的孤儿 PK」这条轴完全失明，而 strict 的语义就是「补图失败只是
            # 没配图，绝不拿未复核的图去赌」——缺字段正是"未复核"的一种。
            dropped += 1
            continue
        if (_ha3_pk not in (None, "") and rds_pk is not None
                and str(_ha3_pk) != str(rds_pk)):
            dropped += 1
            continue
        kept.append(r)
    if dropped:
        logger.info("主命中 RDS 复核：丢弃 %d/%d 条陈旧/已停用命中", dropped, len(results))
    return kept


# ── 图片「当前版本」不变量（2026-08-06，codex 四轮 REVISE 收敛）────────────────────────
# 权威值 = **该文档最高的「完整 INDEXED」active 版本**（见 `_resolve_serving_versions`）。
#
# ⚠️ 这一条**推翻了**原定的 `document_meta.current_version_no`（2026-08-06 codex 补评审）：
#   · `routes/kb_console.py:3072` 在**上传登记的同一个事务里**就把指针推到 N+1 —— 此时摄取
#     （DAG1-3）一步都没跑；而按 CLAUDE.md 的安全不变量，旧版 chunk 要服务到新版索引成功为止。
#     ⇒ 拿指针当权威，会让「待审批 / DAG 处理中 / 索引失败」的文档**正文照常、图片全没**，
#     可能持续数天。仓内 `asset_set_diff.py:185` 早就写明「上一成功服务版本**不是**
#     current_version_no 指针」。
#   · 退而求其次的 `MAX(version_no) WHERE is_active=1` 同样不成立：新 chunk 在 **DAG2** 就以
#     `is_active=1 + index_status=NOT_INDEXED` 落库（`chunker.py:154`），DAG3 才推 HA3 ——
#     那只是把同一个回归从「上传后」挪到「DAG2 后」。
#   · 采用的口径与本仓既有惯用法同源：`spot_checker.py:585` 的 `reconcile_stranded_versions`
#     正是「max_active_ver **且** 该版本全部 INDEXED」；`pipeline_nodes.py:7462` 的停用闸
#     判据也是「该版本无 active 且非 INDEXED 的行」。
#
# 为什么必须是**独立于 4c 的**一道门（v1 方案被 codex 打掉的正是这点）：
# `_revalidate_main_hits` 在任何 SQL 之前就早退（`not cfg.rag.main_hit_revalidate` /
# `cfg.simulate_db`）—— 把版本门塞进 4c，等于让一个**性能开关**能关掉一道**安全门**。
#
# 为什么不能只看 `chunk_type == "image"`（v1 的第二个洞）：`llm_generator.py` 的
# `text_chunk/clause_chunk/ocr_chunk/visual_knowledge` 分支只要带 `image_refs` 或
# `source_image` 同样注入 `<<IMG:N>>` 出图。谓词取并集，见 `_row_carries_image`。

# 整条 chunk 就是图（其 chunk_text 本身即图 caption 派生）⇒ 版本陈旧时**丢行**。
# 其余类型带图 ⇒ **剥图保正文**：正文是它自己那个版本的正文，不该被图连坐。
_IMAGE_ONLY_CHUNK_TYPES = ("image", "visual_knowledge")


def _row_carries_image(c: Dict[str, Any], rds_chunk_type: Optional[str] = None) -> bool:
    """本行是否可能出图（版本门的作用域谓词——并集）。

    两类进作用域：
      · **类型本身即图**（`image`/`visual_knowledge`）—— 即便投影里 `source_image` 为空
        也要进：那种行的 `chunk_text`/`visual_summary` 本身就是旧版内容；
      · **当前已带出图载荷**的任意类型 —— `llm_generator` 的
        `text_chunk/clause_chunk/ocr_chunk/step_card` 分支只要有 `image_refs` 或
        `source_image` 就注入 `<<IMG:N>>`，只看 `chunk_type=='image'` 会全部漏掉。

    ⚠️ 有意**不**把「可能在下游装载图的类型」（无 refs 的 step_card）纳入：
      · 那会让**几乎每一批**检索都多一次 RDS 查表（`text_chunk` 无处不在），而对
        无载荷的行本函数什么也做不了 —— 纯粹的成本；
      · 下游那两个装载面各自已经堵住：`_probe_pool_image_refs` 先经
        `_resolve_serving_versions` 裁决，再按 `(doc_id, version_no)` 取 refs
        （根本不装非 serving 版本的 refs），expand 之后有消费点③重新过门。

    `image_refs` 走 `_normalize_image_refs` 而非裸 truthiness —— `'[]'` / `'null'`
    都是真值字符串（`_row_has_images` 同款教训）。
    """
    if any(r.get("oss_key") for r in _normalize_image_refs(c.get("image_refs"))):
        return True
    if c.get("source_image") or c.get("oss_key"):
        return True
    return (str(c.get("chunk_type") or "") in _IMAGE_ONLY_CHUNK_TYPES
            or str(rds_chunk_type or "") in _IMAGE_ONLY_CHUNK_TYPES)


def _strip_images(c: Dict[str, Any]) -> Dict[str, Any]:
    """剥掉一行的出图载荷，保留正文（返回副本，绝不就地改调用方的 dict）。

    三个键都要清：`image_refs`（step/vk/probe 装载的）、`source_image`（HA3 直接回的
    首图）、`oss_key`（部分行形态直接带）。少清一个，`llm_generator` 那条
    `(image_refs) or source_image` 的 or 链就会把图放回去。
    """
    out = dict(c)
    out["image_refs"] = []
    out["source_image"] = ""
    out["oss_key"] = ""
    out["_stale_image_stripped"] = True
    return out


def _resolve_serving_versions(cur, doc_ids: List[str]) -> Dict[str, Optional[int]]:
    """每个 doc「当前实际在服务的版本」——版本门与 probe **共用的唯一权威解析**。

    三态裁决（codex 2026-08-06 收窄，宽 fallback 已被证伪，见下）：
      · 存在「完整 INDEXED」的 active 版本 ⇒ 取其中最高者（`complete_max`）；
      · 无完整版本、但**只有一个** active 版本 ⇒ 取它（`single_active_fallback`）；
      · 无完整版本、且有**多个** active 版本 ⇒ **None = 无从裁决 ⇒ fail-closed**；
      · 无 active 行（整篇退役）⇒ None ⇒ fail-closed。

    「完整 INDEXED」= 该 (doc, version) 下**没有** active 且 `index_status != 'INDEXED'` 的行。
    与本仓既有判据同源：`pipeline_nodes.py` 停用闸、`spot_checker.reconcile_stranded_versions`。

    🔴 为什么单版本 fallback 必须存在：ACL 重投影 / 标题分类投影 / 可见性变更都会把
    **正在服务的那一版**的 active chunk 标成 `NOT_INDEXED`（`access_grants.py:507`、
    `routes/kb_console.py:3782`）—— 「无完整版本」因此是**常态**而非脏数据；不给 fallback，
    每次改个权限就会让该文档的图集体消失。

    🔴 为什么 fallback 不能放宽成「无完整版本就取 MAX(active)」（我提过，codex 举反例否掉）：
    可达状态是 vN 正在服务但被投影标脏、同时 vN+1 在 DAG3 **部分**推送成功
    （图片 chunk 已 INDEXED 进了 HA3、兄弟 chunk FAILED）。两版都不完整，宽 fallback 会选中
    vN+1 —— 而系统正因为 vN+1 不完整才**刻意保留** vN。那是**错版本投放**，比少图严重。
    多 active 版本且无完整版本 = 真歧义，只能 fail-closed。
    """
    if not doc_ids:
        return {}
    ph = ",".join(["%s"] * len(doc_ids))
    # 单查询同时取三个量（内层按版本聚合完整性，外层按 doc 归并）。分成两条聚合会重复扫同一批
    # active 行，且在隔离级别变化时可能落在两个快照上。
    cur.execute(
        "SELECT doc_id,"
        "       MAX(CASE WHEN nonindexed = 0 THEN version_no END) AS complete_max,"
        "       MAX(version_no) AS active_max,"
        "       COUNT(*) AS active_version_count"
        "  FROM (SELECT doc_id, version_no,"
        "               SUM(CASE WHEN index_status = 'INDEXED' THEN 0 ELSE 1 END) AS nonindexed"
        "          FROM chunk_meta"
        f"        WHERE doc_id IN ({ph}) AND is_active = 1"
        "         GROUP BY doc_id, version_no) v"
        " GROUP BY doc_id",
        tuple(doc_ids))
    out: Dict[str, Optional[int]] = {}
    for row in (cur.fetchall() or []):
        # ⚠️ 行形态**两种都要吃**：本 helper 被两处调用，而它们的游标类型不同 ——
        # 版本门用默认元组游标，`_probe_pool_image_refs` 用 `pymysql.cursors.DictCursor`。
        # 直接按四元组解包 dict 会解出**键名**，`int("complete_max")` 抛 ValueError，
        # 再被 probe 的 fail-open 吞掉 ⇒ 生产上 probe **永远装不上图**（功能静默失效、
        # 测试却全绿，因为桩的 `cursor(*a, **k)` 忽略了 cursor 类）。codex 2026-08-06 实证。
        if isinstance(row, dict):
            doc_id = row.get("doc_id")
            complete_max = row.get("complete_max")
            active_max = row.get("active_max")
            n_ver = row.get("active_version_count")
        else:
            doc_id, complete_max, active_max, n_ver = row
        if complete_max is not None:
            out[str(doc_id)] = int(complete_max)
        elif int(n_ver or 0) == 1:
            # 唯一 active 版本被投影标脏 ⇒ 它就是在服务的那一版，无歧义
            out[str(doc_id)] = int(active_max)
        else:
            out[str(doc_id)] = None      # 多版本且都不完整 ⇒ 歧义 ⇒ 不裁决
    return out


def _deny_stale_version_images(
    rows: List[Dict[str, Any]], *, source: str = "",
) -> List[Dict[str, Any]]:
    """版本门：非**当前版本**的图一律不得进入下游（不论渲染、还是外发第三方重排）。

    缺陷（2026-08-06 codex 补评审，三条均已独立复现）：
      1. 🔴 4c 的 SQL 只取 `chunk_id/is_active/permission_level/owner_dept/id`——**无版本列**。
         双活版本窗口里旧版图 `is_active=1` 干净通过；而 `cosurface_doc_images` 遇到
         `any(chunk_type=='image')` **直接短路**，cosurface 内那段版本 fail-closed 根本
         没机会跑 ⇒ 主命中的旧版本图被直接投放。
      2. 🔴 `_probe_pool_image_refs` 在 **rerank 之前**从 RDS 附 `image_refs`，
         `reranker._img_key` 取它、签名后进 `docs[].image_url` ⇒ **旧版本图会先被外发给
         DashScope 的 qwen3-vl-rerank**。那不是显示问题，是数据流出第三方。
         （缓解：`rerank_enable`/`rerank_img_probe` 现网默认均 False ⇒ 本门是「开 rerank
         之前必须修好」的前置条件，不是正在流血。）

    逐行五类处置（**不是**调用级参数 —— codex 明确否掉了 `drop_refs_only` 当调用级开关：
    同一批里 image 行与带图正文行的正确处置本就不同）：
      · `image` / `visual_knowledge` 陈旧 ⇒ 丢行；
      · 其余类型陈旧 ⇒ 剥图保正文；
      · 无键（`chunk_id`/`id` 都没有）⇒ fail-closed（缺陷 2 的同族：无从复核不等于放行）；
      · RDS 查不到该行 / 该 doc 解析不出 serving 版本 ⇒ fail-closed；
      · 等于 serving 版本 ⇒ 原样。

    ⚠️ 版本取 **RDS 的 `chunk_meta.version_no`**，不取 HA3 返回的 `version_no`：后者是被
    复核的对象，拿它当依据等于让投影自证。（HA3 确实回真值——2026-08-04 prod-ro 实证
    4699/4699 非零——但那是「可用作旁证」，不是「可用作权威」。）

    SIM 两档（codex 第二轮点名，不可合并）：
      · 全模拟（`simulate_db` 且 `simulate_opensearch`）⇒ no-op：没有真数据面，门无意义；
      · **半模拟**（`simulate_db` + 真 HA3）⇒ **fail-closed**：
        `_validate_environment_target_consistency` 在 `simulate_db` 时**跳过 RDS 目标校验**
        ⇒ 这一档完全没有守卫，不能 warning 后放行。
    """
    if not rows:
        return rows
    scoped = [i for i, c in enumerate(rows) if _row_carries_image(c)]
    if not scoped:
        return rows           # 全批不出图 → 零 DB 往返

    cfg = get_config()
    if cfg.simulate_db:
        if cfg.simulate_opensearch:
            return rows       # 全模拟：无真数据面
        # 半模拟（假 DB + 真 HA3）：无守卫档，fail-closed
        logger.warning("版本门%s：半模拟档（simulate_db + 真 HA3）无权威可查 —— fail-closed",
                       f"（{source}）" if source else "")
        return _apply_stale_disposition(rows, scoped, {}, source=source, unverifiable=True)

    cid_axis = sorted({str(rows[i].get("chunk_id")) for i in scoped if rows[i].get("chunk_id")})
    pk_axis = sorted({str(rows[i].get("id")) for i in scoped
                      if not rows[i].get("chunk_id") and rows[i].get("id") not in (None, "")})
    verdicts: Dict[str, Tuple[bool, Optional[str]]] = {}
    try:
        from opensearch_pipeline.db import _get_db_conn   # 惰性：tests monkeypatch db._get_db_conn
        conn = _get_db_conn()
        try:
            fetched = []
            with conn.cursor() as cur:
                _sel = ("SELECT cm.chunk_id, cm.id, cm.chunk_type, cm.version_no, cm.doc_id"
                        " FROM chunk_meta cm")
                # 两条查询轴必须**都**查（4c 的老教训：把两者压进同一个列表按 chunk_id 查，
                # 「只有 HA3 id」的历史行永远查不到，所谓 id 兜底是假的）。
                if cid_axis:
                    ph = ",".join(["%s"] * len(cid_axis))
                    cur.execute(f"{_sel} WHERE cm.chunk_id IN ({ph})", tuple(cid_axis))
                    fetched.extend(cur.fetchall() or [])
                if pk_axis:
                    ph2 = ",".join(["%s"] * len(pk_axis))
                    cur.execute(f"{_sel} WHERE cm.id IN ({ph2})", tuple(pk_axis))
                    fetched.extend(cur.fetchall() or [])
                serving = _resolve_serving_versions(
                    cur, sorted({str(r[4]) for r in fetched if r[4] is not None}))
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 — 权威不可达 ⇒ fail-closed（丢图不丢正文）
        logger.warning("版本门%s：权威表不可达 —— fail-closed（%s）",
                       f"（{source}）" if source else "", e)
        return _apply_stale_disposition(rows, scoped, {}, source=source, unverifiable=True)

    for r in fetched:
        want = serving.get(str(r[4])) if r[4] is not None else None
        ok = want is not None and str(r[3]) == str(want)
        rct = str(r[2] or "")
        if r[0]:
            verdicts[f"cid:{r[0]}"] = (ok, rct)
        if r[1] is not None:
            verdicts[f"pk:{r[1]}"] = (ok, rct)
    return _apply_stale_disposition(rows, scoped, verdicts, source=source)


def _apply_stale_disposition(
    rows: List[Dict[str, Any]],
    scoped: List[int],
    verdicts: Dict[str, Tuple[bool, Optional[str]]],
    *,
    source: str = "",
    unverifiable: bool = False,
) -> List[Dict[str, Any]]:
    """把版本裁决落到每一行（`_deny_stale_version_images` 的处置半边，便于单测直驱）。

    `unverifiable=True`（半模拟 / RDS 不可达）⇒ 作用域内全部按陈旧处置。
    """
    scoped_set = set(scoped)
    out: List[Dict[str, Any]] = []
    dropped = stripped = 0
    for i, c in enumerate(rows):
        if i not in scoped_set:
            out.append(c)
            continue
        cid, pk = c.get("chunk_id"), c.get("id")
        if unverifiable:
            ok, rct = False, None
        elif not cid and pk in (None, ""):
            ok, rct = False, None      # 两个轴都没有键 ⇒ 无从复核 ⇒ 不放行
        else:
            key = f"cid:{cid}" if cid else f"pk:{pk}"
            ok, rct = verdicts.get(key, (False, None))
        if ok:
            out.append(c)
            continue
        # 类型判定取 HA3 侧与 RDS 侧的**并集**：HA3 投影可能陈旧到连 chunk_type 都不对。
        if (str(c.get("chunk_type") or "") in _IMAGE_ONLY_CHUNK_TYPES
                or str(rct or "") in _IMAGE_ONLY_CHUNK_TYPES):
            dropped += 1
            continue
        out.append(_strip_images(c))
        stripped += 1
    if dropped or stripped:
        logger.info("版本门%s：丢弃 %d 条纯图行、剥图保正文 %d 条（非当前版本或无从复核）",
                    f"（{source}）" if source else "", dropped, stripped)
    return out


def _search_chunks_opensearch(
    query: str,
    dense: List[float],
    top_k: int,
    user_dept: Union[str, List[str], None] = None,
    degraded: bool = False,
    acl_ctx=None,
) -> List[Dict[str, Any]]:
    """本地开发回退检索：标准 OpenSearch dense kNN(0.7) + BM25(0.3)。

    仅当 HA3 endpoint 未配置且 opensearch.host 已配置时由 search_chunks 调用
    （生产配置 HA3，本分支不可达）。返回与 _parse_ha3_response 同形的 chunk 字典，
    权限语义与 HA3 过滤一致（public 或 dept_internal+本部门）；
    封面降权逻辑与 HA3 路径保持同款。
    degraded=True（P2-4 嵌入降级）：dense 是零向量占位 → 去掉 kNN 子句只留 BM25 match
    （零向量在部分 kNN 引擎下直接报错），权限 filter 原样保留，结果打 degraded 标。
    """
    from opensearchpy import OpenSearch

    cfg = get_config().opensearch
    client = OpenSearch(
        hosts=[{"host": cfg.host, "port": cfg.port}],
        http_auth=(cfg.auth_user, cfg.auth_password) if cfg.auth_user else None,
        use_ssl=cfg.use_ssl, verify_certs=cfg.verify_certs,
        timeout=30,
    )

    # public 子句统一用 permission_level（与下方 dept/allowed_depts 分支及 HA3
    # _build_permission_filter 同字段）——此前用 kb_type 造成本地回退路径字段漂移（field-drift）。
    perm_should = [{"term": {"permission_level": "public"}}]
    groups = _normalize_acl_groups(user_dept)  # 多组：term→terms（净化+白名单后的组列表）
    if groups:
        perm_should.append({"bool": {"must": [
            {"term": {"permission_level": "dept_internal"}},
            # 'production' 伞组展开为各 production* 子线 owner（与 HA3 _build_permission_filter 同源）
            {"terms": {"owner_dept": _expand_groups_to_owners(groups)}},
        ]}})
        # Phase D（默认关）：allowed_depts 含调用者组码（非 owner 展开）→ 放行该 dept_internal 文档。
        # 本地回退路径，与 HA3 _build_permission_filter 的 allowed_depts 分支同义（restricted 仍排除）。
        if get_config().rag.allowed_depts_acl:
            perm_should.append({"bool": {"must": [
                {"term": {"permission_level": "dept_internal"}},
                {"terms": {"allowed_depts": groups}},
            ]}})
    # node-ACL（2026-07-31）：节点分支与组码**互相独立** —— 与 HA3 侧 `_node_filter_suffix`
    # 同构地放在 `if groups:` **之外**。无组码映射的员工（现网部门映射缺口 26/131）拿到节点
    # 授权后，在本地回退链上同样必须可见；写侧 `to_opensearch_doc` 已输出 allowed_depts。
    if acl_ctx is not None:
        _grant, _enforce = _node_acl_flags()
        if _grant and getattr(acl_ctx, "node_channel_ok", False):
            if getattr(acl_ctx, "org_wide_reader", False):
                perm_should.append({"term": {"permission_level": "dept_internal"}})
            else:
                from opensearch_pipeline.acl_policy import node_filter_terms
                _terms = node_filter_terms(acl_ctx)
                if _terms:
                    perm_should.append({"bool": {"must": [
                        {"term": {"permission_level": "dept_internal"}},
                        {"terms": {"allowed_depts": _terms}},
                    ]}})

    fetch_k = max(top_k * 2, top_k + 5)
    # P2-4 降级：零向量不进 kNN（部分引擎对零范数向量报错），纯 BM25 match 排序
    if degraded:
        rank_should = [{"match": {"chunk_text": {"query": query, "boost": 1.0}}}]
    else:
        rank_should = [
            {"knn": {"chunk_vector": {"vector": dense, "k": fetch_k, "boost": 0.7}}},
            {"match": {"chunk_text": {"query": query, "boost": 0.3}}},
        ]
    body = {
        "size": fetch_k,
        # 🔴 `version_no` 是**必取**字段（2026-08-04 补）：写侧 `Chunk.to_opensearch_doc`
        #    （chunker.py:205）一直在写它，是这里的**读侧**漏了 ⇒ 下游拿到 0，而
        #    `stitch_neighbor_chunks`（:1469，`16eb40b` 2026-06-28 起）用它建
        #    `WHERE version_no=%s` ⇒ **本回退路径的邻居拼接自那天起一直是静默全空**
        #    （fail-open 风格让它不报错，所以没人发现）。补图的版本轴同样依赖它。
        "_source": ["chunk_id", "id", "doc_id", "chunk_text", "chunk_type", "title",
                    "section_title", "chunk_index", "page_num", "kb_type",
                    "permission_level", "owner_dept", "category_l1",
                    "source_image", "visual_summary", "version_no"],
        "query": {"bool": {
            "should": rank_should,
            "filter": [{"bool": {"should": perm_should, "minimum_should_match": 1}}],
        }},
    }
    resp = client.search(index=cfg.index_name, body=body)

    parsed = []
    for hit in resp["hits"]["hits"]:
        src = hit.get("_source", {})
        # _source 字段可能显式为 null（与缺失不同），统一空值兜底
        parsed.append({
            "chunk_id": (src.get("chunk_id") or hit.get("_id") or ""),
            "id": str(src.get("id") or hit.get("_id") or ""),
            "chunk_text": src.get("chunk_text") or "",
            "title": src.get("title") or "",
            "section_title": src.get("section_title") or "",
            "doc_id": src.get("doc_id") or "",
            # 与 HA3 路径（clients.parse_ha3_response）同键同缺省，供邻居拼接的版本约束
            # 与补图版本轴消费；缺失退 0（= 不拼别版本、不补图，fail-closed 方向）。
            "version_no": src.get("version_no") or 0,
            "category_l1": src.get("category_l1") or "",
            "chunk_index": src.get("chunk_index") or 0,
            "page_num": src.get("page_num") or 0,
            "kb_type": src.get("kb_type") or "public",
            "permission_level": src.get("permission_level") or "restricted",   # P2-02：缺失 fail-closed
            "owner_dept": src.get("owner_dept") or "",
            "chunk_type": src.get("chunk_type") or "",
            "source_image": src.get("source_image") or "",
            "visual_summary": src.get("visual_summary") or "",
            "score": hit.get("_score", 0),
        })
    if degraded:
        _mark_degraded_results(parsed)   # P2-4：打标 + 分级失效处理（见 helper 注释）

    # 封面降权（与 HA3 路径同款）
    content_results, cover_results = [], []
    for r in parsed:
        if r.get("chunk_type") in ("image", "step_card", "procedure_parent", "visual_knowledge"):
            content_results.append(r)
        elif not r.get("section_title") and len(r.get("chunk_text", "")) < 200:
            r["_is_cover"] = True
            cover_results.append(r)
        else:
            content_results.append(r)
    results = (content_results + cover_results)[:top_k]
    logger.info("OpenSearch fallback search: query=%r, results=%d (content=%d, cover=%d)",
                query[:30], len(results), len(content_results), len(cover_results))
    return results


def _client_fusion_search(
    *,
    query: str,
    dense: List[float],
    sparse_idx: List[int],
    sparse_val: List[float],
    filter_expr: Optional[str],
    top_k: int,
    output_fields: List[str],
    client,
    cfg,
) -> Optional[List[Dict[str, Any]]]:
    """三路客户端融合检索（RAG_HA3_CLIENT_FUSION，**默认开**——config.py 默认 True
    随包生效，=false 为逃生舱；本 docstring 曾写"默认关"系 2026-07-13 灰度期旧话）。

    /search 不支持 sparse 参数（522 盲行事故根因，docs/ha3_sparse_rootcause_and_ab_2026-07-13.md）
    之后「既救盲行又保 sparse」的正确路径（金集 A/B 判决 w3_s10，
    docs/ha3_client_fusion_3way_ab_2026-07-13.md：recall@1 +3.6pp vs 去 sparse 默认，盲行 5/5 rank1）：

      D 臂  /query 纯 dense       —— 主臂：无 sparse 倒排项的行（实时推送批）在此可达（救盲行）
      S 臂  /query dense+sparse   —— sparse 的官方支持路径，仅作加分信号；无 sparse 行在此臂
                                     缺席=拿不到加分，而非像 /search 那样被整行排除
      B 臂  /search 纯 BM25       —— knn 权重清零（P2-4 降级同款 payload），文本兜底

    三臂并行请求，各臂分数 min-max 归一后加权求和（默认 0.7/0.1/0.3，缺席不罚分——
    RRF 在金集上判死：按名次投票缺席=少一臂票，结构性惩罚无 sparse 行）。
    对外 score 恢复为服务端加权可比分（knn_weight*dense_IP + text_weight*BM25_raw），
    保住 7.7/5.8 高/中/低档位与低置信护栏的标定语义；融合名次分存 _fused_score 仅供诊断。

    降级语义（与仓库 fail-open 惯例一致）：S/B 辅臂失败按空臂继续；D 主臂异常返回 None，
    调用方回落服务端混合检索。
    """
    from alibabacloud_ha3engine_vector.models import (
        QueryRequest, RankQuery, SearchRequest, SparseData, TextQuery,
    )

    pool = cfg.client_fusion_pool

    def _arm_dense():
        return client.query(QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            top_k=pool,
            include_vector=False,
            output_fields=output_fields,
            filter=filter_expr,
            order="DESC",
        ))

    def _arm_sparse():
        return client.query(QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            sparse_data=SparseData(
                count=[len(sparse_idx)], indices=sparse_idx, values=sparse_val),
            top_k=pool,
            include_vector=False,
            output_fields=output_fields,
            filter=filter_expr,
            order="DESC",
        ))

    def _arm_bm25():
        knn_query = QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            top_k=pool,
            include_vector=False,
            filter=filter_expr,
        )
        knn_query.weight = 0.0
        text_query = TextQuery(
            query_string=f"{cfg.text_search_field}:'{_escape_ha3_query(query)}'",
            query_params={"default_op": "OR"},
            filter=filter_expr,
        )
        text_query.weight = 1.0
        return client.search(SearchRequest(
            table_name=cfg.table_name,
            knn=knn_query,
            text=text_query,
            rank=RankQuery(),
            size=pool,
            order="DESC",
            output_fields=output_fields,
        ))

    # D3（2026-08-06）：三臂改用**进程级** fusion 池。默认 max_workers = AnyIO 令牌 × 3
    # ⇒ 当前请求上限下不会排队，并发形态与改动前等价，只是线程被复用而非每请求新建。
    # ⚠️ 绝不能对共享池 `shutdown`（会毒化整个进程），故这里没有 `with`。
    # ⚠️ 饱和用 `submit_or_none` 而不是抛异常：下面那个 try 只包 `fut.result()`、**不包提交**，
    #    靠异常会直接崩掉整条查询而不是走单臂降级（codex 2026-08-06 BLOCKER）。
    from opensearch_pipeline import serving_pools as _sp

    arms: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    _subs = [("dense", _arm_dense), ("bm25", _arm_bm25)]
    if sparse_idx:
        _subs.append(("sparse", _arm_sparse))
    futures = {}
    for _name, _fn in _subs:
        _f = _sp.submit_or_none(_sp.FUSION, _fn)
        if _f is None:
            arms[_name] = None       # 饱和 = 该臂缺席，与单臂失败**同一条**降级路径
            logger.warning("Client fusion arm '%s' 因池饱和跳过（降级为少臂融合）", _name)
        else:
            futures[_name] = _f
    for name, fut in futures.items():
        try:
            arms[name] = _parse_ha3_response(fut.result())
        except Exception as e:  # 单臂失败不破坏答案（graceful degradation）
            arms[name] = None
            logger.warning("Client fusion arm '%s' failed: %s", name, e)

    if arms.get("dense") is None:
        # 主臂异常（非空结果）：融合失去救盲行意义，回落服务端混合（fail-open）
        logger.warning("Client fusion dense arm unavailable; falling back to server hybrid")
        return None

    weights = {
        "dense": cfg.client_fusion_dense_weight,
        "sparse": cfg.client_fusion_sparse_weight,
        "bm25": cfg.client_fusion_text_weight,
    }
    fused: Dict[str, float] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    raw_dense: Dict[str, float] = {}
    raw_bm25: Dict[str, float] = {}
    for name, hits in arms.items():
        if not hits:
            continue
        vals = [float(h.get("score") or 0.0) for h in hits]
        lo, hi = min(vals), max(vals)
        rng = hi - lo
        w = weights[name]
        for h, v in zip(hits, vals):
            pk = h.get("id") or h.get("chunk_id")
            if not pk:
                continue
            norm = (v - lo) / rng if rng > 0 else 1.0
            fused[pk] = fused.get(pk, 0.0) + w * norm
            meta.setdefault(pk, h)
            if name == "dense":
                raw_dense[pk] = v
            elif name == "bm25":
                raw_bm25[pk] = v

    ordered = sorted(fused, key=lambda p: -fused[p])[:top_k]
    results = []
    for pk in ordered:
        h = dict(meta[pk])
        h["_fused_score"] = round(fused[pk], 6)
        h["score"] = (cfg.knn_weight * raw_dense.get(pk, 0.0)
                      + cfg.text_weight * raw_bm25.get(pk, 0.0))
        results.append(h)

    logger.info(
        "Client 3-way fusion: dense=%s sparse=%s bm25=%s -> fused=%d, top_k=%d",
        len(arms.get("dense") or []),
        "off" if "sparse" not in futures else len(arms.get("sparse") or []),
        len(arms.get("bm25") or []) if arms.get("bm25") is not None else "FAIL",
        len(fused), len(results),
    )
    return results


def search_chunks(
    query: str,
    *,
    top_k: int = 5,
    min_score: float = 0.0,
    max_distance: float = 0.0,
    output_fields: Optional[List[str]] = None,
    user_dept: Union[str, List[str], None] = None,
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
    acl_ctx=None,
) -> List[Dict[str, Any]]:
    """
    端到端检索：query → embedding → HA3 search → 标准化结果。

    当 enable_hybrid=True（默认）时，使用 HA3 服务端三路混合检索：
      - kNN 路: Dense + Sparse 向量检索
      - Text 路: BM25 全文检索（基于 chunk_text 倒排索引）
      - 融合: RRF 或加权求和

    当 enable_hybrid=False 时，降级为纯向量检索（兼容旧行为）。

    Args:
        query: 用户查询文本
        top_k: 最终返回的结果数
        min_score: (保留兼容) 相似度下限，仅在 score 为 0-1 相似度时使用
        max_distance: (已弃用 no-op) InnerProduct 索引无「距离」语义，保留仅为签名兼容，不生效（F-20）
        output_fields: 自定义返回字段列表

    Returns:
        [{"chunk_text", "title", "section_title", "doc_id", "score", ...}]
    """
    config = get_config()
    cfg = config.alibaba_vector

    # 1. 生成 query embedding（retrieve_and_enrich 会预算一次并传入，避免重复嵌入）。
    # P2-4：自算嵌入失败时（RAG_DEGRADED_BM25_ENABLE 默认开）不 raise，拿到
    # _DegradedEmbedding（零向量占位）→ 下方切换纯 BM25 形态；传入的预算嵌入若本身
    # 是降级产物（retrieve_and_enrich 同一封装算出）同样经 degraded 属性识别。
    _emb = (query_embedding if query_embedding is not None
            else _get_query_embedding_or_degraded(query))
    degraded = bool(getattr(_emb, "degraded", False))
    dense, sparse_idx, sparse_val = _emb

    # 2. 构建 sparse data
    from alibabacloud_ha3engine_vector.models import QueryRequest, SparseData

    # 混合 /search 的 knn 臂是否携带 sparse_data。**默认 false（关）** —— 2026-07-13 定论：
    # /vector-service/search 不支持 sparse 参数（阿里工程师 + 官方 inverted-query 文档确认：
    # knn 仅 vector/topk/filter/weight）。引擎对该未支持参数做未文档化处理，把"无 sparse 倒排项
    # 的行"静默排除 —— 6-27 上一次全量之后经 API/Swift 实时推送的行（sparse 仅全量物化）因此
    # 成片失明（曾达 522 docs / 35% 语料）。251-q 金集 A/B 实证去 sparse 净提升召回（recall@5
    # 0.776→0.915），故关闭是止血即根治，且为全环境正确的安全默认。
    # 逃生舱：RAG_HA3_KNN_SPARSE_ENABLE=true 可重启（仅当阿里给出 /search 支持 sparse 的正式路径
    # 或改走 /query+客户端融合后才有意义）。详见 docs/ha3_sparse_rootcause_and_ab_2026-07-13.md。
    _knn_sparse_enable = os.environ.get("RAG_HA3_KNN_SPARSE_ENABLE", "false").lower() == "true"
    sparse_data = None
    if sparse_idx and _knn_sparse_enable:
        sparse_data = SparseData(
            count=[len(sparse_idx)],
            indices=sparse_idx,
            values=sparse_val,
        )

    _output_fields = output_fields or list(_DEFAULT_OUTPUT_FIELDS)

    # ── 权限过滤（安全边界，统一实现）──
    filter_expr = _build_permission_filter(user_dept, acl_ctx=acl_ctx)

    logger.info("Permission filter: user_dept=%s, filter=%s", user_dept, filter_expr)

    # 3. 构建请求并执行
    # 本地开发回退：HA3 未配置且本地 OpenSearch 可用时走标准 OpenSearch 检索
    # （dense kNN 0.7 + BM25 0.3，与线上 weighted 融合同权重；
    #  生产配置了 HA3 endpoint，此分支不可达 —— 2026-06-10 本地 E2E 引入）
    _full_cfg = get_config()
    if not _full_cfg.alibaba_vector.endpoint and getattr(_full_cfg.opensearch, "host", ""):
        # 本地 OS 回退**同样**过版本门：它是 return，不经下方 4b/4c/4d，只接主路等于
        # 「另一条执行路径恒不触发」（本仓 flag 传播的老教训）。
        return _deny_stale_version_images(_revalidate_main_hits(_deny_revoked_cross_dept(
            _search_chunks_opensearch(query, dense, top_k, user_dept, degraded=degraded,
                                      acl_ctx=acl_ctx),
            user_dept, acl_ctx=acl_ctx)), source="主命中/本地OS回退")

    client = _get_ha3_client()

    # 3a. 三路客户端融合（RAG_HA3_CLIENT_FUSION，默认开——config 默认 True 随包生效，
    # =false 为 kill switch；旧注释"默认关；生产开启走 SAE env"已过时）。
    # 注意：S 臂走 /query（sparse 官方支持路径），与上面 RAG_HA3_KNN_SPARSE_ENABLE
    # （只管 /search 的 knn 臂）互不相干——融合开启时 sparse 信号总是可用。
    # degraded（零向量嵌入）不走融合：D/S 两臂无意义，沿用纯 BM25 降级形态。
    results: Optional[List[Dict[str, Any]]] = None
    if cfg.client_fusion_enable and cfg.enable_hybrid and not degraded:
        results = _client_fusion_search(
            query=query, dense=dense, sparse_idx=sparse_idx, sparse_val=sparse_val,
            filter_expr=filter_expr, top_k=top_k, output_fields=_output_fields,
            client=client, cfg=cfg,
        )   # None = 主臂失败 → 回落下方服务端混合

    if results is None and (cfg.enable_hybrid or degraded):
        # ── 混合检索: Dense + Sparse + BM25 三路融合 ──
        # P2-4：degraded 时即便 enable_hybrid=False 也走本分支——纯向量路径没有 BM25
        # text 路可用，降级检索必须经 text 路（knn 路权重清零仅作 payload 占位）。
        from alibabacloud_ha3engine_vector.models import (
            SearchRequest, TextQuery, RankQuery,
        )

        # kNN 路（Dense + Sparse）— 复用 QueryRequest 模型
        knn_query = QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            sparse_data=sparse_data,
            top_k=cfg.hybrid_knn_top_k,
            include_vector=False,
            filter=filter_expr,
        )

        # BM25 Text 路
        escaped_query = _escape_ha3_query(query)
        text_query = TextQuery(
            query_string=f"{cfg.text_search_field}:'{escaped_query}'",
            query_params={"default_op": "OR"},
            filter=filter_expr,
        )

        # 融合策略
        if degraded:
            # P2-4 降级＝纯 BM25 形态（对既有 SDK payload 结构侵入最小）：knn 路权重清零、
            # text 路权重 1.0，等价于只按 chunk_text 倒排排序。不走 rrf——rrf 会把零向量
            # knn 路的伪名次融进排序。两路 filter（权限边界）均未改动。
            knn_query.weight = 0.0
            text_query.weight = 1.0
            rank = RankQuery()
        elif cfg.hybrid_fusion == "rrf":
            rank = RankQuery(rrf={"rankConstant": cfg.rrf_rank_constant})
        else:
            # 加权模式：通过 knn.weight 和 text.weight 控制
            knn_query.weight = cfg.knn_weight
            text_query.weight = cfg.text_weight
            rank = RankQuery()  # 空 rank = 默认加权策略

        request = SearchRequest(
            table_name=cfg.table_name,
            knn=knn_query,
            text=text_query,
            rank=rank,
            size=top_k,
            order="DESC",
            output_fields=_output_fields,
        )

        logger.info(
            "Hybrid search: fusion=%s, text_field=%s, knn_top_k=%d, size=%d, degraded=%s",
            cfg.hybrid_fusion, cfg.text_search_field, cfg.hybrid_knn_top_k, top_k, degraded,
        )
        resp = client.search(request)
        results = _parse_ha3_response(resp)
    elif results is None:
        # ── 纯向量检索（降级 / 兼容旧行为，RAG_HA3_ENABLE_HYBRID=false 才走）──
        request = QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            sparse_data=sparse_data,
            top_k=top_k,
            include_vector=False,
            output_fields=_output_fields,
            filter=filter_expr,
            order="DESC",  # F-20/G29: InnerProduct 越高越相似，缺 DESC 引擎按升序返回、最不相关排第一
        )
        logger.info("Vector-only search: top_k=%d", top_k)
        resp = client.query(request)
        results = _parse_ha3_response(resp)

    # 4. 结果后处理（三条路径——客户端融合/服务端混合/纯向量——在此汇合）
    if degraded:
        _mark_degraded_results(results)   # P2-4：打标 + 分级失效处理（见 helper 注释）

    # 4b. 查询侧拒绝（Phase D 读侧 fail-closed 复核）：撤销跨部门授权后即时生效，不等 HA3 投影收回。
    results = _deny_revoked_cross_dept(results, user_dept, acl_ctx=acl_ctx)

    # 4c. 主命中 RDS 复核（P3-1）：is_active/权限轴与权威表不一致的陈旧投影不投放。
    results = _revalidate_main_hits(results)

    # 4d. 图片版本门（消费点①）：4c **不看版本**，双活窗口里旧版本图 is_active=1 会干净通过。
    # 有意放在 4c 之后且**不受 main_hit_revalidate 门控**——那是性能开关，不该能关掉安全门。
    results = _deny_stale_version_images(results, source="主命中")

    # 5. （F-20）原 max_distance「距离上限」过滤已删除：HA3 索引是 InnerProduct（score 是相似度，
    # 越大越相关），不存在「距离」；旧代码 `score <= max_distance` 方向与内积相反，会把最相关结果全滤掉。
    # 该分支从无生产调用方传 max_distance（默认 0.0，恒不触发），故整段删除以消除反向陷阱。
    # max_distance 参数保留仅为签名兼容，现为 no-op（见 docstring）。

    # 6. 封面/元数据 chunk 降权
    # 短文本 + 无 section_title 的 chunk 通常是封面页或目录，
    # 包含文档标题导致 BM25 高分，但没有实质内容。
    # 策略：正文 chunk 优先排前面，封面 chunk 排后面（不丢弃，避免无结果）。
    # 注意：图片 chunk 天然短文本、无 section_title，但含有 visual_summary 语义信息，不应被降权。
    _COVER_MAX_LEN = 200  # 短于此且无 section_title 视为封面/元数据
    content_results = []
    cover_results = []
    for r in results:
        text = r.get("chunk_text", "")
        has_section = bool(r.get("section_title"))
        chunk_type = r.get("chunk_type", "")
        if chunk_type in ("image", "step_card", "procedure_parent", "visual_knowledge"):
            content_results.append(r)
        elif not has_section and len(text) < _COVER_MAX_LEN:
            r["_is_cover"] = True  # 供 _select_with_doc_cap 识别：封面只作最后回填
            cover_results.append(r)
        else:
            content_results.append(r)

    if cover_results:
        logger.info(
            "封面降权: %d 个封面 chunk 被移到末尾 (共 %d 结果)",
            len(cover_results), len(results),
        )
    results = content_results + cover_results

    logger.info("Search completed: query=%r, results=%d (content=%d, cover=%d), hybrid=%s",
                query[:50], len(results), len(content_results), len(cover_results), cfg.enable_hybrid)
    return results


# ═══════════════════════════════════════════════════════════════
# 4. Neighbor Stitching（邻居扩展）
# ═══════════════════════════════════════════════════════════════

def _same_permission(row: Dict[str, Any], center: Dict[str, Any]) -> bool:
    """H4 防御纵深：二次取回（邻居拼接 / step 扩展）的行必须与中心 chunk 同
    (permission_level, owner_dept)。同文档本应天然一致（权限按文档统一），万一不一致
    则丢弃——绝不把比"已通过权限的中心"更严的内容拼进答案上下文。统一边界，单一实现。
    """
    return (
        # P2-02：缺失一律按 restricted（fail-closed）——邻居权限未知时绝不拼进已通过权限的中心
        (row.get("permission_level") or "restricted") == (center.get("permission_level") or "restricted")
        and (row.get("owner_dept") or "") == center.get("owner_dept", "")
    )


# ── F#60：stitch/expand 连接共享 ──────────────────────────────────────────────
# retrieve_and_enrich 在 stitch→expand 两阶段外开一个【线程局部连接作用域】：两阶段中第一个
# 真正需要连库的函数自取连接并寄存于作用域，第二个直接复用，作用域关闭时由 retrieve_and_enrich
# 统一归还——两阶段共 4 次 RDS 查询只做一次池 checkout。刻意不改两函数的调用签名（既有测试以
# `lambda c, window=1` / `lambda c, q` 打桩这两个名字，call-site 传新 kwarg 会打爆桩），
# 独立/直接调用（作用域未开、conn=None）时自取自还，行为与历史一致。
_conn_scope = threading.local()


def _stitch_expand_conn(explicit):
    """解析 stitch/expand 应使用的 RDS 连接：显式入参 > 作用域寄存 > 自取。

    Returns:
        (conn, owns)：owns=True 表示调用方需自行归还；共享连接（显式传入或作用域寄存）
        由所有者（外层调用方 / retrieve_and_enrich 的作用域 finally）统一归还。
    """
    if explicit is not None:
        return explicit, False
    from opensearch_pipeline.db import _get_db_conn   # 惰性：tests monkeypatch db._get_db_conn

    if getattr(_conn_scope, "active", False):
        cached = getattr(_conn_scope, "conn", None)
        if cached is None:
            cached = _get_db_conn()
            _conn_scope.conn = cached   # 寄存：后一阶段复用，retrieve_and_enrich 统一归还
        return cached, False
    return _get_db_conn(), True


def stitch_neighbor_chunks(
    chunks: List[Dict[str, Any]],
    *,
    window: int = 1,
    conn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """
    对检索结果中的每个 chunk，从 RDS 查询 chunk_index ±window 的邻居并拼接。

    解决 chunk 边界切割导致信息不完整的问题：
      - 一个完整条款被切成两个 chunk，检索只命中了一半
      - SOP 流程步骤跨越 chunk 边界

    评测数据 (120 queries)：
      - Context Coverage: +3.1pp (88.8% → 91.8%)
      - Answer Completeness: +2.1pp (82.2% → 84.3%)
      - 退化率: 0% (无负面影响)

    实现细节：
      - 从 RDS chunk_meta 查询邻居（<10ms per query）
      - 按 (doc_id, version_no, chunk_index) 去重，不跨文档边界、不跨版本（双活版本窗口安全）
      - 同一个文档内的邻居 chunk 按 chunk_index 排序后拼接文本
      - 保留原始检索 chunk 的 score / metadata

    Args:
        chunks: search_chunks 返回的结果
        window: 向前/后扩展的 chunk 数量，默认 1（即 ±1）
        conn: F#60 可选共享 RDS 连接（调用方负责归还）；None 时自取（或复用
            retrieve_and_enrich 的连接作用域，与 expand_step_context 共享一次 checkout）

    Returns:
        扩展后的 chunks 列表，chunk_text 已包含邻居文本
    """
    if not chunks or window <= 0:
        return chunks

    try:
        # 单次批量查询所有命中 chunk 的 ±window 邻居（消除 N+1：原先每个 hit 一次 RDS 往返）。
        # 第一遍：分流 pass-through（无 doc_id / step·proc·visual 语义单元）与待拼接 chunk，
        # 用占位符保留输出顺序，并按 (doc_id, center_idx) 去重（重复中心整条丢弃，与原行为一致）。
        expanded: List[Optional[Dict[str, Any]]] = []
        seen_centers = set()
        pending = []          # (slot, chunk, doc_id, center_idx, center_ver)
        ranges = []           # (doc_id, center_ver, lo, hi)

        for chunk in chunks:
            doc_id = chunk.get("doc_id", "")
            center_idx = chunk.get("chunk_index", 0)
            chunk_type = chunk.get("chunk_type", "")

            if not doc_id or chunk_type in ("step_card", "procedure_parent", "visual_knowledge"):
                expanded.append(chunk)
                continue

            # 版本号：邻居必须与中心【同文档同版本】。chunk_meta 的 (doc_id, chunk_index) 跨版本
            # 不唯一——双活版本窗口（新版已 INDEXED、旧版尚未 deactivate；或部分失败长期双活）下
            # 两版本 chunk_index 重叠，不带 version_no 约束会把【别版本】文本拼进答案上下文。
            # HA3 多值字段可能回列表（与 chunk_type 同），防御性归一为 int，失败退 0（= 不拼别版本）。
            _cv = chunk.get("version_no", 0)
            if isinstance(_cv, (list, tuple)):
                _cv = _cv[0] if _cv else 0
            try:
                center_ver = int(_cv)
            except (TypeError, ValueError):
                center_ver = 0

            center_key = (doc_id, center_ver, center_idx)
            if center_key in seen_centers:
                continue  # 重复中心：丢弃（hit A 的邻居恰是 hit B 的中心）
            seen_centers.add(center_key)

            slot = len(expanded)
            expanded.append(None)  # 占位，批量查询后回填
            pending.append((slot, chunk, doc_id, center_idx, center_ver))
            ranges.append((doc_id, center_ver, center_idx - window, center_idx + window))

        # 只有存在待拼接 chunk 时才连库
        neighbors_by_doc: Dict[tuple, Dict[int, Dict[str, Any]]] = {}
        if pending:
            import pymysql.cursors

            # F#60：优先复用共享连接（显式入参 / retrieve_and_enrich 连接作用域），
            # 独立调用（conn=None 且无作用域）时自取自还——行为与历史一致。
            db_conn, _own_conn = _stitch_expand_conn(conn)
            try:
                cursor = db_conn.cursor(pymysql.cursors.DictCursor)
                try:
                    where_parts = []
                    params: List[Any] = []
                    for doc_id, ver, lo, hi in ranges:
                        where_parts.append("(doc_id = %s AND version_no = %s AND chunk_index BETWEEN %s AND %s)")
                        params.extend([doc_id, ver, lo, hi])
                    cursor.execute(
                        "SELECT doc_id, version_no, chunk_index, chunk_text, section_title, "
                        "       permission_level, owner_dept "
                        "FROM chunk_meta WHERE is_active = 1 AND (" + " OR ".join(where_parts) + ")",
                        tuple(params),
                    )
                    for row in cursor.fetchall():
                        # 按 (doc_id, version_no) 分桶 → 中心只取本版本邻居（防跨版本拼接）
                        neighbors_by_doc.setdefault((row["doc_id"], row["version_no"]), {})[row["chunk_index"]] = row
                finally:
                    cursor.close()
            finally:
                if _own_conn:
                    db_conn.close()

        # 第二遍：回填每个待拼接 chunk 的拼接文本（按 chunk_index 升序，含中心本身）
        for slot, chunk, doc_id, center_idx, center_ver in pending:
            doc_neighbors = neighbors_by_doc.get((doc_id, center_ver), {})
            lo, hi = center_idx - window, center_idx + window
            # H4 防御纵深：邻居必须与中心 chunk 同权限（同文档本应一致），否则丢弃
            neighbor_rows = [
                doc_neighbors[i] for i in sorted(doc_neighbors)
                if lo <= i <= hi and _same_permission(doc_neighbors[i], chunk)
            ]
            if neighbor_rows:
                stitched_text = "\n".join(nb["chunk_text"] or "" for nb in neighbor_rows)
            else:
                stitched_text = chunk.get("chunk_text", "")
            expanded_chunk = dict(chunk)
            expanded_chunk["chunk_text"] = stitched_text
            expanded_chunk["_stitched"] = True
            expanded_chunk["_stitch_window"] = window
            expanded_chunk["_neighbor_count"] = len(neighbor_rows)
            expanded[slot] = expanded_chunk

        logger.info(
            "邻居扩展完成: %d chunks → %d expanded (去重 %d), window=±%d, RDS 往返=%d",
            len(chunks), len(expanded), len(chunks) - len(expanded), window,
            1 if pending else 0,
        )
        return expanded

    except Exception as e:
        logger.warning("邻居扩展失败，回退到原始结果: %s", e, exc_info=True)
        return chunks


# ═══════════════════════════════════════════════════════════════
# 4.5 Step Card 上下文扩展
# ═══════════════════════════════════════════════════════════════

def _normalize_image_refs(image_refs_json) -> List[Dict[str, Any]]:
    """把 RDS image_refs_json 归一化为统一的 image_refs 列表（单一实现，消除三份漂移）。

    保留 CLAUDE.md 标注的载荷契约键 oss_key/source_image/visual_summary/ocr_text/caption/
    order/image_index + filename/anchor_row（SF-2：xlsx 同 anchor 多图的严格身份键，不可在
    RDS→serving 回路丢失），互相兜底（oss_key↔source_image）。下游 content_blocks_builder 读
    ``oss_key or source_image`` 与 ``caption or visual_summary or ocr_text``，因此键越全越好；
    原先三个分支各自只发部分键（两处丢 visual_summary、一处丢 ocr_text），导致 XLSX 绑定的
    图注（存在 visual_summary）渲染不出来。

    入参可为 JSON 字符串或已解析的 list。
    """
    raw: list = []
    if image_refs_json:
        if isinstance(image_refs_json, str):
            try:
                raw = json.loads(image_refs_json)
            except (json.JSONDecodeError, TypeError):
                raw = []
        elif isinstance(image_refs_json, list):
            raw = image_refs_json
    if not isinstance(raw, list):
        # JSON 列可能存了非数组值（'null'/'{}'/数字）：json.loads 成功但结果不可
        # enumerate → 此前会炸掉整个调用方（expand 整体回退）。归一为空列表。
        raw = []
    out: List[Dict[str, Any]] = []
    for idx, ref in enumerate(raw):
        if not isinstance(ref, dict):
            continue
        oss_key = ref.get("oss_key") or ref.get("source_image", "")
        out.append({
            "oss_key": oss_key,
            "source_image": ref.get("source_image") or oss_key,
            "visual_summary": ref.get("visual_summary", ""),
            "ocr_text": ref.get("ocr_text", ""),
            "caption": ref.get("caption", ""),
            "order": ref.get("order", idx),
            "image_index": ref.get("image_index", idx),
            # SF-2: preserve the xlsx same-anchor disambiguation contract keys (CLAUDE.md) across the
            # RDS→serving roundtrip — filename+anchor_row are the strict identity for multiple images
            # bound at the same row; dropping them here breaks the documented end-to-end contract.
            "filename": ref.get("filename", ""),
            "anchor_row": ref.get("anchor_row"),
        })
    return out


def _row_has_images(row: Dict[str, Any]) -> bool:
    """RDS 兄弟行是否携带可渲染图片（#F-mm4 带图保底的判定谓词）。

    ⚠️ 不能对 image_refs_json 做裸 truthiness 判断：'[]' / 'null' 都是真值字符串。
    统一走 _normalize_image_refs 解析，并要求至少一个 ref 带 oss_key（可渲染）。
    """
    refs = _normalize_image_refs(row.get("image_refs_json"))
    return any(r.get("oss_key") for r in refs)


def expand_step_context(
    chunks: List[Dict[str, Any]],
    query: str,
    *,
    max_steps: int = 8,
    max_images_total: int = 8,
    conn: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """对 step_card / procedure_parent 类型的检索结果进行上下文扩展。

    根据用户查询意图从 RDS 查询同一流程的兄弟步骤或子步骤，
    将语义完整的步骤序列组装后返回，让 LLM 能看到完整操作上下文。

    扩展策略（按意图）：
      - ``full_procedure``  — 包含全部兄弟步骤（上限 max_steps）
      - ``locate_field``    — 仅保留命中步骤本身（不扩展）
      - ``specific_step``   — 命中步骤 + 下一步
      - ``general``         — 命中步骤 ±1 邻居

    排序规则：
      按 parent_chunk_id 分组，组间按组内最高分降序，组内按 step_no 升序。

    Args:
        chunks: 上游检索 + 邻居拼接后的结果列表
        query: 用户原始查询文本
        max_steps: 单个流程最多展示的步骤数
        max_images_total: 全局图片引用上限（预留）
        conn: F#60 可选共享 RDS 连接（调用方负责归还）；None 时自取（或复用
            retrieve_and_enrich 的连接作用域，与 stitch_neighbor_chunks 共享一次 checkout）

    Returns:
        扩展、去重、重排后的 chunks 列表
    """
    if not chunks:
        return chunks

    intent = _classify_step_query_intent(query)
    logger.info("Step Card 意图分类: query=%r → intent=%s", query[:60], intent)

    # #F-mm4 图感知双修（两个独立 flag，默认关闭 = 行为与历史逐字节一致）：
    #   expand_image_keep（RAG_EXPAND_IMAGE_KEEP，K>0 生效）— 意图筛选后入选兄弟
    #   全部无图而家族里有带图 step_card 时，按步号最近补入最多 K 张带图兄弟；
    #   parent_child_as_stepcard（RAG_PARENT_CHILD_AS_STEPCARD）— parent 展开子卡
    #   chunk_type 归位 step_card，使其 image_refs 在生成/渲染两端可达。
    _cfg_rag = get_config().rag
    _img_keep = _cfg_rag.expand_image_keep
    _child_as_stepcard = _cfg_rag.parent_child_as_stepcard

    # 判断是否存在需要扩展的 chunk 类型，避免无意义的 RDS 连接
    # visual_knowledge：image_refs 仅落库 RDS（HA3 只回 source_image 首图），需按 chunk_id 补全多图。
    need_expand = any(
        c.get("chunk_type") in ("step_card", "procedure_parent", "visual_knowledge")
        for c in chunks
    )
    if not need_expand:
        return chunks

    db_conn = None
    _own_conn = False
    try:
        import pymysql.cursors

        # F#60：优先复用共享连接（显式入参 / retrieve_and_enrich 连接作用域），
        # 独立调用（conn=None 且无作用域）时自取自还——行为与历史一致。
        db_conn, _own_conn = _stitch_expand_conn(conn)
        cursor = db_conn.cursor(pymysql.cursors.DictCursor)
    except Exception as e:
        logger.warning("expand_step_context: 无法获取 RDS 连接，回退到原始结果: %s", e)
        if _own_conn and db_conn is not None:
            try:
                db_conn.close()
            except Exception:   # noqa: BLE001
                pass
        return chunks

    try:
        # ── A2: 批量预取，消除 N+1（原先每个 step_card 2 次、每个 visual_knowledge 1 次往返）──
        step_card_ids = [
            cid for cid in (
                (c.get("chunk_id") or c.get("id", ""))
                for c in chunks if c.get("chunk_type") == "step_card"
            ) if cid
        ]
        vk_ids = [
            cid for cid in (
                (c.get("chunk_id") or c.get("id", ""))
                for c in chunks
                if c.get("chunk_type") == "visual_knowledge" and not c.get("image_refs")
            ) if cid
        ]

        # 1) step_card 元数据：parent_chunk_id / step_no / extra_json / image_refs_json
        meta_by_id: Dict[str, Dict[str, Any]] = {}
        if step_card_ids:
            ph = ",".join(["%s"] * len(step_card_ids))
            cursor.execute(
                "SELECT chunk_id, parent_chunk_id, step_no, extra_json, image_refs_json "
                f"FROM chunk_meta WHERE chunk_id IN ({ph})",
                tuple(step_card_ids),
            )
            for row in cursor.fetchall():
                meta_by_id[row["chunk_id"]] = row

        # 2) 所有相关 parent 的兄弟/子步骤（一次取齐，按 parent 分组、组内按 step_no 升序）。
        # procedure_parent 命中也并入：其子步骤靠 RDS 的 parent_chunk_id 反查——
        # 旧实现读 chunk["extra_json"].child_chunk_ids，但 HA3 的 output_fields 不含
        # extra_json，child_ids 永远为空（死分支），子步骤及其图片从未被展开过。
        procedure_parent_ids = {
            cid for cid in (
                (c.get("chunk_id") or c.get("id", ""))
                for c in chunks if c.get("chunk_type") == "procedure_parent"
            ) if cid
        }
        parent_ids = sorted(
            {r["parent_chunk_id"] for r in meta_by_id.values() if r.get("parent_chunk_id")}
            | procedure_parent_ids
        )
        siblings_by_parent: Dict[str, List[Dict[str, Any]]] = {}
        if parent_ids:
            ph = ",".join(["%s"] * len(parent_ids))
            cursor.execute(
                "SELECT chunk_id, chunk_text, step_no, section_title, "
                "       extra_json, image_refs_json, parent_chunk_id, "
                "       permission_level, owner_dept "
                f"FROM chunk_meta WHERE parent_chunk_id IN ({ph}) AND is_active = 1 "
                # chunk_id 内嵌零填充 _cNNNN_ 序号 → 字典序 = 文档顺序；
                # 兜底 step_no 平局（N) 子步骤沿用主号、X.Y 步骤等）的展示顺序
                "ORDER BY step_no, chunk_id",
                tuple(parent_ids),
            )
            for row in cursor.fetchall():
                siblings_by_parent.setdefault(row["parent_chunk_id"], []).append(row)

        # 3) visual_knowledge 的 image_refs_json（一次取齐）
        vk_refs_by_id: Dict[str, Any] = {}
        if vk_ids:
            ph = ",".join(["%s"] * len(vk_ids))
            cursor.execute(
                f"SELECT chunk_id, image_refs_json FROM chunk_meta WHERE chunk_id IN ({ph})",
                tuple(vk_ids),
            )
            for row in cursor.fetchall():
                vk_refs_by_id[row["chunk_id"]] = row.get("image_refs_json")

        expanded_all: List[Dict[str, Any]] = []

        for chunk in chunks:
            ctype = chunk.get("chunk_type", "")

            # ── step_card：查 RDS 获取 parent_chunk_id / step_no，再展开兄弟 ──
            if ctype == "step_card":
                chunk_id = chunk.get("chunk_id") or chunk.get("id", "")
                original_score = chunk.get("score", 0)

                meta_row = meta_by_id.get(chunk_id)
                if not meta_row or not meta_row.get("parent_chunk_id"):
                    # C2：无 procedure_parent（如 XLSX procedure_image_guide）。RDS 里已绑定的
                    # image_refs（HA3 不返回）必须在此附上，否则该 step_card 的图片永远到不了答案。
                    if meta_row and not chunk.get("image_refs"):
                        refs = _normalize_image_refs(meta_row.get("image_refs_json"))
                        if refs:
                            chunk = dict(chunk)
                            chunk["image_refs"] = refs
                    expanded_all.append(chunk)
                    continue

                parent_id = meta_row["parent_chunk_id"]
                hit_step_no = meta_row.get("step_no") or 0
                # H4 防御纵深：只展开与命中 step_card 同权限的兄弟（同家族本应一致）
                siblings = [s for s in siblings_by_parent.get(parent_id, [])
                            if _same_permission(s, chunk)]

                # 按意图筛选
                if intent == "full_procedure":
                    selected = siblings[:max_steps]
                elif intent == "locate_field":
                    selected = [s for s in siblings if s["step_no"] == hit_step_no]
                elif intent == "specific_step":
                    selected = [
                        s for s in siblings
                        if s["step_no"] is not None
                        and hit_step_no <= s["step_no"] <= hit_step_no + 1
                    ]
                else:  # general
                    selected = [
                        s for s in siblings
                        if s["step_no"] is not None
                        and hit_step_no - 1 <= s["step_no"] <= hit_step_no + 1
                    ]

                # ── 公共不变量（F-21）：命中卡永不被意图筛选裁掉 ──────────────
                # full_procedure 的 siblings[:max_steps] 位置截断、以及 locate/specific/general
                # 的 step_no 区间筛选（命中卡自身 step_no 为 None / 平局 / 越界）都可能把命中
                # step_card 排除出 selected。命中卡是最佳匹配文本，一旦缺席 → 答案只讲前 N 步、
                # 对用户实际问的后段步骤无中生有或拒答。把防洪 cap 分支里 keep_ids={chunk_id} 的
                # 「命中永存」保证提升为所有意图分支共享：缺失则从（已过 _same_permission 的）
                # siblings 取回命中行置于队首（最终展示序由组内 step_no 重排决定，见下方 sort）。
                if chunk_id not in {s["chunk_id"] for s in selected}:
                    hit_self = next((s for s in siblings if s["chunk_id"] == chunk_id), None)
                    if hit_self is not None:
                        selected = [hit_self] + selected

                # ── 带图兄弟保底（RAG_EXPAND_IMAGE_KEEP，#F-mm4a）──────────
                # 意图窗/max_steps 裁剪零图感知：宽泛问题命中概述卡（step_no 大批
                # 平局）时真正带截图的操作步最易被切掉 → 答案恒无图。K>0 且入选
                # 兄弟全部无图而家族有带图行时，按步号最近补入最多 K 张。
                # locate_field 的设计语义是「仅保留命中步、不扩展」，不参与保底。
                # 判定只读预取行的 image_refs_json（零额外 RDS 查询）。
                img_keep_ids: set = set()
                if _img_keep > 0 and intent != "locate_field" and selected:
                    if not any(_row_has_images(s) for s in selected):
                        _sel_ids = {s["chunk_id"] for s in selected}
                        img_sibs = [
                            s for s in siblings
                            if s["chunk_id"] not in _sel_ids and _row_has_images(s)
                        ]
                        if img_sibs:
                            # 步号最近优先；平局按 chunk_id（零填充序号=文档序）保证确定性
                            img_sibs.sort(key=lambda s: (
                                abs((s.get("step_no") or 0) - hit_step_no), s["chunk_id"]))
                            extra_sibs = img_sibs[:_img_keep]
                            img_keep_ids = {s["chunk_id"] for s in extra_sibs}
                            selected = selected + extra_sibs
                            logger.info(
                                "Step 扩展带图保底: parent=%s 补入 %d 张带图兄弟 (K=%d, hit_step=%s)",
                                parent_id, len(extra_sibs), _img_keep, hit_step_no,
                            )

                # ── 超大家族防洪（RAG_STEP_EXPAND_FAMILY_CAP）──────────────
                # 意图筛选按 step_no 数值区间：正常 SOP（step_no 基本互异）选出 2-3 个；
                # 但超大手册的 step_no 大规模平局（如 41 个小节卡全是 step_no=0）会让
                # 区间退化成全家族（~15k 字），把真正命中的小节挤出 context 预算
                # （2026-06-11 J-r120_23 拒答根因）。超限时收缩为：命中卡 + 同
                # section_title 伙伴 + 文档序 ±2 窗口；≤ 上限的家族行为不变。
                _cap = get_config().rag.step_expand_family_cap
                if _cap > 0 and len(selected) > _cap:
                    hit_row = next(
                        (s for s in selected if s["chunk_id"] == chunk_id), None)
                    hit_section = (hit_row or {}).get("section_title") or ""
                    keep_ids = {chunk_id}
                    if hit_section:
                        keep_ids.update(
                            s["chunk_id"] for s in selected
                            if (s.get("section_title") or "") == hit_section)
                    if hit_row is not None:
                        hi = selected.index(hit_row)
                        keep_ids.update(
                            s["chunk_id"] for s in selected[max(0, hi - 2):hi + 3])
                    else:
                        keep_ids.update(s["chunk_id"] for s in selected[:_cap])
                    # #F-mm4a：带图保底行进保留集（否则收缩规则会立即吐掉刚补的图）
                    keep_ids.update(img_keep_ids)
                    # #F-mm4a：cap 分支自身的图感知——step_no 大规模平局时区间退化为
                    # 全家族（selected 本就含带图行，前面的保底不触发），而「命中 +
                    # 同 section + 文档序 ±2」的收缩规则零图感知，会把带图操作步全部
                    # 吐掉（宽泛问题恒零图的真实形态）。保留集无图而 selected 有时，
                    # 按步号最近补最多 K 张进保留集。
                    if _img_keep > 0 and intent != "locate_field":
                        _kept_rows = [s for s in selected if s["chunk_id"] in keep_ids]
                        if not any(_row_has_images(s) for s in _kept_rows):
                            _img_rows = [
                                s for s in selected
                                if s["chunk_id"] not in keep_ids and _row_has_images(s)
                            ]
                            _img_rows.sort(key=lambda s: (
                                abs((s.get("step_no") or 0) - hit_step_no), s["chunk_id"]))
                            for row in _img_rows[:_img_keep]:
                                keep_ids.add(row["chunk_id"])
                                img_keep_ids.add(row["chunk_id"])
                    trimmed = [s for s in selected if s["chunk_id"] in keep_ids]
                    logger.info(
                        "Step 扩展防洪: parent=%s 家族筛选 %d → %d (cap=%d, hit_section=%r)",
                        parent_id, len(selected), len(trimmed), _cap, hit_section,
                    )
                    selected = trimmed[:_cap]
                    # #F-mm4a：带图保底行必须在末端 [:_cap] 切片内存活——被挤出时
                    # 从尾部向前替换「非命中、非带图」的行（保底行数 ≤ K，替换有界）
                    if img_keep_ids:
                        _in_slice = {s["chunk_id"] for s in selected}
                        _evicted = [s for s in trimmed
                                    if s["chunk_id"] in img_keep_ids
                                    and s["chunk_id"] not in _in_slice]
                        for row in _evicted:
                            for i in range(len(selected) - 1, -1, -1):
                                sid = selected[i]["chunk_id"]
                                if sid != chunk_id and sid not in img_keep_ids:
                                    selected[i] = row
                                    break

                for sib in selected:
                    is_hit = (sib["chunk_id"] == chunk_id)
                    score = original_score if is_hit else original_score * 0.85

                    # 解析 extra_json
                    extra = {}
                    if sib.get("extra_json"):
                        try:
                            extra = json.loads(sib["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    expanded_chunk = dict(chunk)  # 继承原始 hit 的 metadata
                    expanded_chunk.update({
                        "chunk_id": sib["chunk_id"],
                        "chunk_text": sib.get("chunk_text", ""),
                        "step_no": sib.get("step_no"),
                        "section_title": sib.get("section_title", ""),
                        "parent_chunk_id": parent_id,
                        "score": score,
                        "image_refs": _normalize_image_refs(sib.get("image_refs_json")),
                        "annotation_map": extra.get("annotation_map", {}),
                        # 条款编号原文（4.1 / 3.2.4），展示层用它替代 ordinal step_no
                        "section_no": extra.get("section_no", ""),
                        "is_expanded": not is_hit,
                        "expanded_from": chunk_id if not is_hit else None,
                        "expansion_reason": "sibling_step" if not is_hit else None,
                    })
                    expanded_all.append(expanded_chunk)

            # ── procedure_parent：展开子步骤（按 RDS parent_chunk_id 反查，已随兄弟查询预取）──
            elif ctype == "procedure_parent":
                original_score = chunk.get("score", 0)
                parent_chunk_id = chunk.get("chunk_id") or chunk.get("id", "")

                # H4 防御纵深：只展开与命中 procedure_parent 同权限的子步骤
                children = [s for s in siblings_by_parent.get(parent_chunk_id, [])
                            if _same_permission(s, chunk)]
                total_children = len(children)

                if not children:
                    expanded_all.append(chunk)
                    continue

                # 截断并添加提示
                if total_children > max_steps:
                    parent_chunk = dict(chunk)
                    parent_chunk["chunk_text"] = (
                        chunk.get("chunk_text", "")
                        + f"\n（该流程共{total_children}步，以下展示前{max_steps}步）"
                    )
                    expanded_all.append(parent_chunk)
                    children = children[:max_steps]
                else:
                    expanded_all.append(chunk)

                for child in children:
                    child_extra = {}
                    if child.get("extra_json"):
                        try:
                            child_extra = json.loads(child["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    expanded_chunk = dict(chunk)
                    expanded_chunk.update({
                        "chunk_id": child["chunk_id"],
                        "chunk_text": child.get("chunk_text", ""),
                        "step_no": child.get("step_no"),
                        "section_title": child.get("section_title", ""),
                        "parent_chunk_id": parent_chunk_id,
                        "score": original_score * 0.8,
                        "image_refs": _normalize_image_refs(child.get("image_refs_json")),
                        "annotation_map": child_extra.get("annotation_map", {}),
                        "section_no": child_extra.get("section_no", ""),
                        "is_expanded": True,
                        "expanded_from": parent_chunk_id,
                        "expansion_reason": "parent_children",
                    })
                    if _child_as_stepcard:
                        # #F-mm4b：子卡归位 step_card（父卡本体保持 procedure_parent）。
                        # 否则子卡继承父类型：_format_context 只加 [流程概览] 不注
                        # <<IMG:N>>、content_blocks_builder 无该类型提图分支 —— 上面
                        # 从 RDS 装载的 image_refs 在生成/渲染两端 100% 不可达。
                        # 归位后自动进入现有 step_card 全链路，零下游代码改动。
                        expanded_chunk["chunk_type"] = "step_card"
                    expanded_all.append(expanded_chunk)

            # ── visual_knowledge：按 chunk_id 从 RDS 补全全部 image_refs（多图幻灯片）──
            elif ctype == "visual_knowledge":
                enriched = dict(chunk)
                chunk_id = chunk.get("chunk_id") or chunk.get("id", "")
                # HA3 只回 source_image（首图）；image_refs 不在索引里。仅当结果未带
                # image_refs 时用预取的 RDS 全量补齐，失败/无记录则保留 source_image 首图兜底。
                if chunk_id and not chunk.get("image_refs"):
                    refs = _normalize_image_refs(vk_refs_by_id.get(chunk_id))
                    refs = [r for r in refs if r["oss_key"]]  # 保留原 vk 语义：无图源的 ref 丢弃
                    if refs:
                        enriched["image_refs"] = refs
                expanded_all.append(enriched)

            else:
                # 非 step 类型，原样保留
                expanded_all.append(chunk)

    except Exception as e:
        logger.warning("expand_step_context 处理异常，回退到原始结果: %s", e, exc_info=True)
        return chunks
    finally:
        # F#60：游标为本函数私有必关；连接仅在自有（非共享/作用域）时归还——顺带修复
        # 原实现异常路径不归还连接的池泄漏（blocking 池下泄漏会饿死后续请求）。
        try:
            cursor.close()
        except Exception:   # noqa: BLE001
            pass
        if _own_conn:
            try:
                db_conn.close()
            except Exception:   # noqa: BLE001
                pass

    # ── 去重：相同 chunk_id 保留最高分 ──
    # ⚠️ direct 证据的合并与"选最高分 winner"是【两件事】，必须分开(C2，2026-08-03)：
    # 同一 chunk_id 既可能来自 HA3 直接命中，也可能是【别的】命中行展开出的 RDS 兄弟
    # (expand 时 `dict(chunk)` 继承原 hit 的 id、仅覆写 chunk_id)。若展开行分数更高，
    # winner 就是展开行，"该 chunk 曾被 HA3 直接命中"这个事实会随败者一起丢掉 ——
    # ha3_verify 据此判在场/缺失，丢了就会把【真实命中】判成 missing(假红)。
    # 故：无论保留哪一行，只要任一候选是 direct，winner 一律标 `_direct_hit=True`(OR 合并)。
    # 原始 HA3 行根本不带 `is_expanded` 键 ⇒ 缺键即 direct。
    def _is_direct(row: Dict[str, Any]) -> bool:
        return not row.get("is_expanded", False)

    seen: Dict[str, Dict[str, Any]] = {}
    direct_seen: Dict[str, bool] = {}
    for c in expanded_all:
        cid = c.get("chunk_id") or c.get("id", "")
        direct_seen[cid] = direct_seen.get(cid, False) or _is_direct(c)
        if cid in seen:
            if c.get("score", 0) > seen[cid].get("score", 0):
                seen[cid] = c
        else:
            seen[cid] = c
    deduped = []
    for cid, row in seen.items():
        row["_direct_hit"] = direct_seen.get(cid, False)
        deduped.append(row)

    # ── 排序：按 parent_chunk_id 分组 → 组间按最高分降序 → 组内按 step_no 升序 ──
    groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for c in deduped:
        gkey = c.get("parent_chunk_id")
        groups.setdefault(gkey, []).append(c)

    # 组内按 step_no 排序
    for members in groups.values():
        members.sort(key=lambda x: (x.get("step_no") or 0))

    # 组间按最高分降序
    sorted_groups = sorted(
        groups.values(),
        key=lambda grp: max(c.get("score", 0) for c in grp),
        reverse=True,
    )

    result: List[Dict[str, Any]] = []
    for grp in sorted_groups:
        result.extend(grp)

    logger.info(
        "Step Card 扩展完成: %d chunks → %d expanded (去重后 %d), intent=%s",
        len(chunks), len(expanded_all), len(result), intent,
    )
    return result


# ═══════════════════════════════════════════════════════════════
# 5. 统一检索入口
# ═══════════════════════════════════════════════════════════════

def _cosurface_top_doc_ids(chunks: List[Dict[str, Any]], max_docs: int = 3) -> List[str]:
    """cosurface 的 top 文档选择：按结果顺序（= 相关度）取前 max_docs 个去重 doc_id。

    单独成函使 F#53 预取与 cosurface_doc_images 共用同一实现——口径一漂移预取即失配。
    """
    doc_ids: List[str] = []
    for c in chunks:
        d = c.get("doc_id")
        if d and d not in doc_ids:
            doc_ids.append(d)
        if len(doc_ids) >= max_docs:
            break
    return doc_ids


def _fetch_cosurface_images(
    query: str,
    doc_ids: List[str],
    user_dept: Union[str, List[str], None],
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
    max_images: int = 3,
    acl_ctx=None,
) -> List[Dict[str, Any]]:
    """cosurface 的 HA3 补图查询（F#53 拆出：可在 stitch/expand 期间由 future 并发预取）。

    失败向上抛出，由调用方 fail-open（cosurface_doc_images 捕获后原样返回 chunks）。
    """
    cfg = get_config().alibaba_vector
    from alibabacloud_ha3engine_vector.models import QueryRequest, SparseData

    dense, sparse_idx, sparse_val = (
        query_embedding if query_embedding is not None else get_query_embedding(query)
    )
    sparse_data = (
        SparseData(count=[len(sparse_idx)], indices=sparse_idx, values=sparse_val)
        if sparse_idx else None
    )

    doc_clause = " OR ".join(
        f'doc_id="{_sanitize_ha3_filter_value(d)}"' for d in doc_ids
    )
    # 权限子句与 search_chunks 共用同一实现（安全边界单一来源）
    # ⚠️ 必须同样透传 acl_ctx（2026-07-31）：漏传时节点分支不会出现在这条过滤器里，
    # 于是 node 文档【正文可见但补图召不回】——用户看到答案却永远没有配图，而
    # step-card 的价值高度依赖配图。方向是 fail-closed（少召回、不越权），故此前
    # 定性为可用性缺陷；但 node-ACL 一旦放量它就是"图全没了"。
    perm = _build_permission_filter(user_dept, acl_ctx=acl_ctx)
    filter_expr = f'chunk_type="image" AND ({doc_clause}) AND ({perm})'

    req = QueryRequest(
        table_name=cfg.table_name,
        vector=dense,
        sparse_data=sparse_data,
        # 复核会丢掉候选（陈旧/孤儿/旧版本），×2 的余量在丢弃后常常一张都不剩 ⇒ 有界
        # 过取：×4 仍是十几行的小查询，但给"排第一的被丢、排第二的递补"留出空间。
        top_k=max_images * 4,
        include_vector=False,
        output_fields=list(_DEFAULT_OUTPUT_FIELDS),
        filter=filter_expr,
        order="DESC",  # G29: InnerProduct 越高越相似，缺 DESC 引擎按升序返回 → "每文档取首个"取到最不相关图
    )
    return _parse_ha3_response(_get_ha3_client().query(req))


def cosurface_doc_images(
    query: str,
    chunks: List[Dict[str, Any]],
    *,
    user_dept: Union[str, List[str], None] = None,
    max_docs: int = 3,
    max_images: int = 3,
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
    prefetched: Optional[Tuple[List[str], Any]] = None,
    acl_ctx=None,
) -> List[Dict[str, Any]]:
    """图片召回增强：为已检索高分文档补充其最相关的 image chunk。

    背景：图片以独立 ``chunk_type="image"`` chunk 存在，与正文 chunk 在同一向量排序中
    竞争。文本类 / 流程类查询往往让正文挤掉同文档的图片，导致答案缺图（即便文档其实有图）。

    做法：对 top 文档做一次按 ``doc_id + chunk_type="image"`` 过滤的 kNN 查询，取与 query
    最相关的图片，并**插入到其同文档正文 chunk 之后**（而非追加到末尾）—— 这样 ``<<IMG:N>>``
    提示不会被 ``_format_context`` 的 ``max_context_chars`` 截断，LLM 才能引用到正确序号，
    ``content_blocks_builder`` 才能绑定。``source_image`` 仅存于 HA3（不在 RDS chunk_meta），
    故必须走 HA3 查询。

    - 结果已含 image chunk（如可视化查询）→ 原样返回，不打扰既有多模态路径。
    - 任何异常都 fail-open 返回原 chunks，绝不影响回答（与本模块整体降级风格一致）。

    Args:
        query: 用户查询文本（用于按相关度挑图）
        chunks: 已检索 + 拼接后的 chunk 列表
        user_dept: 用户部门（沿用权限过滤）
        max_docs: 最多为前 N 个文档补图
        max_images: 最多补充的图片总数
        prefetched: F#53 可选预取结果 (doc_ids, Future)——retrieve_and_enrich 在 stitch/expand
            期间并发发起的同参 HA3 补图查询。仅当预取的 doc_ids 与本次（expand 后）选定的
            doc_ids 完全一致才采用（expand 的分组重排可能改变前 max_docs 个 doc 的顺序）；
            失配则回退本函数串行查询，语义与无预取时逐字节一致。

    Returns:
        在同文档正文之后插入了 image chunk 的新列表（原文本 chunk 顺序不变）。
    """
    if not chunks:
        return chunks
    # 已经有图片 chunk（可视化查询）→ 不重复补充
    if any(c.get("chunk_type") == "image" for c in chunks):
        return chunks

    # top 文档（按结果顺序 = 相关度）
    doc_ids = _cosurface_top_doc_ids(chunks, max_docs)
    if not doc_ids:
        return chunks

    try:
        img_results: Optional[List[Dict[str, Any]]] = None
        if prefetched is not None:
            pre_ids, fut = prefetched
            if pre_ids == doc_ids:
                # F#53 预取命中：doc 集与顺序一致 → 与串行查询同参数，结果等价。
                # 预取失败在此重放异常 → 与串行的单次尝试同一 fail-open 语义（不重试）。
                img_results = fut.result()
        if img_results is None:
            img_results = _fetch_cosurface_images(
                query, doc_ids, user_dept, query_embedding, max_images=max_images,
                acl_ctx=acl_ctx)
    except Exception as e:
        logger.warning("图片召回补充失败 (non-fatal): %s", e)
        return chunks

    # ── 补图必须走与主命中【同一条】权威复核链（附录B，2026-08-03）─────────────────
    # 此前 _fetch_cosurface_images 的结果从 HA3 直接进最终 chunks —— cosurface 是
    # retrieve_and_enrich 的最后一个检索步骤（其后只有 _attach_doc_dates 这种纯元数据），
    # 全程没有任何下游复核。"doc 已授权"**不能**替代"这条 image 行有效"：
    #   · 同版本重切留下的孤儿 PK（4c 的 B7 段专门治它——旧 PK 既不在 deactivate 的
    #     `version_no < N` 覆盖面，也不在按 version 删的 PENDING_DELETE 里）；
    #   · 主命中复核完成【之后】才发生的 retire / visibility 收紧 / spot 隔离（真 TOCTOU）；
    #   · 旧版本图片：本查询的 filter 不带 version_no，而 _cosurface_top_doc_ids 又把版本轴
    #     折叠掉了 ⇒ 稳态下当前版本正文也能配上旧版本的图。
    # 复核必须在 best_by_doc **之前**：否则排第一的失效图被丢后，同文档排第二的有效图
    # 没有递补机会（本来能出图的文档变成不出图）。
    _n_raw = len(img_results)
    # 版本轴（消费点④）：**fail-closed** —— 非当前版本、或无从复核，都不补这张图。
    #
    # 为什么这一轴不能省（4b/4c 都覆盖不到）：`_revalidate_main_hits` 只比
    # chunk_id / is_active / permission_level / owner_dept / id，**不看版本**；而双活版本
    # 窗口里旧版本 chunk 本就是 `is_active=1` ⇒ 旧版本的图能干干净净地通过 4c。
    #
    # 🔴 2026-08-06 改：权威值从「主结果里首个非零 version_no」换成**该 doc 最高的
    #    「完整 INDEXED」active 版本**（`_resolve_serving_versions`；中途提过的
    #    `document_meta.current_version_no` 与 `MAX(active)` 都已被证伪，理由见该函数
    #    docstring）。原 `_ver_by_doc` 已整段删除 ——
    #    它既不是权威值（拿被复核的 HA3 投影自证），胜出者**还取决于主结果的排序**：
    #    同一 doc 的多个命中版本不同时，谁排第一谁说了算，这不是任何人定义过的语义。
    #    该策略此前零契约守卫（改成「取最后一个版本」全绿），现由版本门的测试面钉住。
    #
    # 位置：必须在 `best_by_doc` **之前** —— 否则排第一的失效图被丢后，同文档排第二的
    # 有效图没有递补机会（本来能出图的文档变成不出图）。
    img_results = _deny_stale_version_images(img_results, source="cosurface 补图")
    # 4b：撤销跨部门授权即时生效（4c 有意不比对 allowed_depts，故这条不可省）
    img_results = _deny_revoked_cross_dept(img_results, user_dept, acl_ctx=acl_ctx)
    # 4c：is_active / ACL 轴 / 物理 PK 轴。strict——补图失败只是没配图，绝不拿未复核的图去赌
    img_results = _revalidate_main_hits(img_results, strict_on_unavailable=True)
    if len(img_results) != _n_raw:
        logger.info("cosurface 补图复核：%d → %d 条（丢弃陈旧/已停用/越权图片行）",
                    _n_raw, len(img_results))
    if not img_results:
        return chunks

    # 每个文档取最相关（首个）的有效图片
    best_by_doc: Dict[str, Dict[str, Any]] = {}
    for r in img_results:
        if r.get("chunk_type") != "image" or not r.get("source_image"):
            continue
        d = r.get("doc_id")
        if d and d not in best_by_doc:
            best_by_doc[d] = r
    if not best_by_doc:
        return chunks

    # 插入到同文档首个正文 chunk 之后；总量 ≤ max_images
    out: List[Dict[str, Any]] = []
    used_docs: set = set()
    for c in chunks:
        out.append(c)
        d = c.get("doc_id")
        if d in best_by_doc and d not in used_docs and len(used_docs) < max_images:
            out.append(best_by_doc[d])
            used_docs.add(d)

    if used_docs:
        logger.info("图片召回补充: 为 %d 个文档插入 image chunk（共 %d 候选文档）",
                    len(used_docs), len(doc_ids))
    return out


def _select_with_doc_cap(
    pool: List[Dict[str, Any]],
    top_k: int,
    cap: int,
) -> List[Dict[str, Any]]:
    """从（已按分排序的）候选池选 top_k，同一文档最多保留 cap 条；池有富余时回填。

    跨文档问题的失败形态之一：top-k 被单一最相似文档占满（重排池 recall@10≈0.99，
    第二目标文档挤不进 top-7）。按文档限额给次优文档让位；单文档问题几乎不受影响
    （top_k=7、cap=4 时仅当某文档独占 ≥5 席才改变结果，且被换入的是池内次优 chunk）。
    cap<=0 或池不大于 top_k 时为纯截断（与原行为一致）。
    """
    if cap <= 0 or len(pool) <= top_k:
        return pool[:top_k]
    out: List[Dict[str, Any]] = []
    counts: Dict[Any, int] = {}
    overflow: List[Dict[str, Any]] = []
    covers: List[Dict[str, Any]] = []
    for ch in pool:
        if len(out) >= top_k:
            break
        # 封面/目录 chunk（search_chunks 已降权标记）不得借限额让位"晋升"——
        # 它们只配作最后的回填，排在被限额挤出的正文 overflow 之后。
        if ch.get("_is_cover"):
            covers.append(ch)
            continue
        key = ch.get("doc_id") or ch.get("title") or ""
        if counts.get(key, 0) >= cap:
            overflow.append(ch)
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(ch)
    for ch in overflow + covers:  # 池内多样性不足 top_k 时按原序回填（正文先、封面后）
        if len(out) >= top_k:
            break
        out.append(ch)
    return out


def _probe_pool_image_refs(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """#F-mm10：候选池 step_card 的带图探测（rerank/tiebreak 之前）。

    重排发生在 expand_step_context 之前：step_card 此时无 image_refs（RDS 未拉）
    且 HA3 行无 source_image → reranker._img_key 恒 None，qwen3-vl-rerank 对带图
    step_card 结构性失明；rerank OFF 的近平局倾斜同样无信号可用。本函数对池内
    step_card 批量 IN 查 chunk_meta.image_refs_json（PK 索引；先经
    `_resolve_serving_versions` 裁决 serving 版本，故为**两条** SQL）并经
    _normalize_image_refs 附到 chunk 上——expand 的 C2/兄弟逻辑对已带 refs 的
    chunk 幂等（`if meta_row and not chunk.get("image_refs")`），不会重复装载。
    任何失败原样返回（fail-open，优雅降级铁律）。
    """
    ids = [
        cid for cid in (
            (c.get("chunk_id") or c.get("id", ""))
            for c in chunks
            if c.get("chunk_type") == "step_card" and not c.get("image_refs")
        ) if cid
    ]
    if not ids:
        return chunks
    refs_by_id: Dict[str, Any] = {}
    conn = None
    cursor = None
    try:
        import pymysql.cursors
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # 消费点②（图片版本门，2026-08-06）：本函数跑在 rerank **之前**，装上的 refs 会被
        # `reranker._img_key` 取走、签名后进 `docs[].image_url` ⇒ 旧版本图会先被外发给
        # DashScope 的 qwen3-vl-rerank。所以不能"先装上、下游再剥"，必须**根本不装**。
        # 权威解析与主门**共用** `_resolve_serving_versions`（两套 SQL 迟早漂移）。
        serving = _resolve_serving_versions(
            cursor, sorted({str(c.get("doc_id")) for c in chunks if c.get("doc_id")}))
        pairs = [(d, v) for d, v in serving.items() if v is not None]
        if not pairs:
            return chunks      # 一个 doc 都裁决不出 serving 版本 ⇒ 一张图都不装（fail-closed）
        ph = ",".join(["%s"] * len(ids))
        # version_no 是语义守卫（chunk_id 已唯一）；`is_active=1` 不可省。
        _vc = " OR ".join(["(cm.doc_id=%s AND cm.version_no=%s)"] * len(pairs))
        cursor.execute(
            f"SELECT cm.chunk_id, cm.image_refs_json FROM chunk_meta cm "
            f"WHERE cm.chunk_id IN ({ph}) AND cm.is_active = 1 AND ({_vc})",
            tuple(ids) + tuple(x for p in pairs for x in p),
        )
        for row in cursor.fetchall():
            refs_by_id[row["chunk_id"]] = row.get("image_refs_json")
    except Exception as e:
        logger.warning("候选池带图探测失败（忽略，fail-open）: %s", e)
        return chunks
    finally:
        # 异常路径也归还连接（同 expand_step_context 的 F#60 修法：
        # blocking=False 池下不归还会饿死后续请求）
        try:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
        except Exception:
            pass
    out: List[Dict[str, Any]] = []
    for c in chunks:
        cid = c.get("chunk_id") or c.get("id", "")
        if (c.get("chunk_type") == "step_card" and not c.get("image_refs")
                and cid in refs_by_id):
            refs = [r for r in _normalize_image_refs(refs_by_id[cid]) if r.get("oss_key")]
            if refs:
                c = dict(c)
                c["image_refs"] = refs
        out.append(c)
    return out


def _chunk_carries_image(c: Dict[str, Any]) -> bool:
    """#F-mm10b：候选是否携带可渲染图（探测后的 image_refs 或 image/vk 的 source_image）。"""
    if c.get("image_refs"):
        return True
    return bool(c.get("chunk_type") in ("image", "visual_knowledge") and c.get("source_image"))


def _image_tiebreak_reorder(chunks: List[Dict[str, Any]], eps: float) -> List[Dict[str, Any]]:
    """#F-mm10b：融合分近平局的带图倾斜（稳定相邻交换，只动分差 < eps 的相邻对）。

    只在「无图在前、带图紧随其后、分差 < eps」时交换——绝不跨越真实分差挪位，
    真实排序信号完整保留；确定性（无随机、有界循环）。eps 取错会用弱文本换图，
    须按 251 题金集标定（L1/L2 baseline merge --strict 证明文本召回无回退）。
    """
    chs = list(chunks)
    changed = True
    while changed:
        changed = False
        for i in range(len(chs) - 1):
            a, b = chs[i], chs[i + 1]
            if _chunk_carries_image(a) or not _chunk_carries_image(b):
                continue
            try:
                gap = float(a.get("score", 0) or 0) - float(b.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if gap < eps:
                chs[i], chs[i + 1] = b, a
                changed = True
    return chs


def _multi_query_search(
    query: str,
    sub_queries: List[str],
    *,
    fetch_k: int,
    top_k: int,
    user_dept: Union[str, List[str], None],
    rerank_enable: bool,
    multimodal: bool,
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
    primary_supplier: Optional[Any] = None,
    acl_ctx=None,
) -> List[Dict[str, Any]]:
    """多意图 fan-out：原查询 + 子查询并行检索（各自重排），轮转交错合并去重。

    轮转交错（原查询路优先）保证每路的 top-1/2 必进最终 top_k —— 跨文档问题的
    失败模式正是单查询 top-k 被一个文档占满，第二个目标文档挤不进上下文。
    单路失败只丢该路（fail-open）；但若所有路都失败（≥1 路异常且无任何结果），
    回退原查询单路检索且**不再捕获异常**——持续性故障必须像单查询路径一样向上
    传播为错误（500/LLM_ERROR），不能被吞成 NO_RESULT"知识库未找到"。

    F#52：primary_supplier（可选零参 callable）——retrieve_and_enrich 在分解 LLM 期间
    已并行预取原查询主路的 search_chunks 结果，传入后原查询路直接复用（不重复检索）；
    预取异常在该路重放 → 与该路自检索失败同语义（fail-open 丢该路）。重排/截断/合并
    逻辑不变。None 时原查询路自行检索（直接调用方 / 测试路径，与历史行为一致）。

    #F-mm10 已知缺口声明：RAG_RERANK_IMG_PROBE 已接入本路径（每路 rerank 前探测）；
    RAG_IMAGE_TIEBREAK 仅单查询路径生效——轮转交错合并的排序语义与相邻近平局
    交换不兼容（每路 top-1/2 保送优先级高于图倾斜），不在此实现。
    """
    queries = [query] + [q for q in sub_queries if q and q.strip() and q != query]

    def _one(idx_q):
        idx, q = idx_q
        try:
            if idx == 0 and primary_supplier is not None:
                chs = primary_supplier()   # F#52：主路结果已预取，直接复用
            else:
                chs = search_chunks(
                    q, top_k=fetch_k, user_dept=user_dept,
                    query_embedding=query_embedding if idx == 0 else None,
                    acl_ctx=acl_ctx,
                )
            if rerank_enable and chs:
                if get_config().rag.rerank_img_probe:
                    chs = _probe_pool_image_refs(chs)   # 每路重排前探测（#F-mm10a）
                from .reranker import rerank_chunks
                chs = rerank_chunks(q, chs, top_k=top_k, multimodal=multimodal)
            return chs[:top_k]
        except Exception as e:
            logger.warning("multi-query 子查询检索失败（忽略该路）: %r %s", q[:40], e)
            return None  # None=该路异常；[]=该路正常但无结果（语义不同，勿混）

    # D3：改用进程级 fanout 池。⚠️ **必须按原始 index 回填**——`ex.map` 保证 `lists` 与
    # queries 同序，而下面的交错合并依赖这个顺序；换成 as_completed 会静默打乱
    # （codex 2026-08-06 BLOCKER）。饱和的那一路回落**同步**执行（等价于该路不并行，
    # 不是该路消失），仍走 `_one` 自己的 except → None 语义。
    from opensearch_pipeline import serving_pools as _sp

    _idx_q = list(enumerate(queries))
    _futs = [(i, _sp.submit_or_none(_sp.FANOUT, _one, iq)) for i, iq in enumerate(_idx_q)]
    lists = [None] * len(_idx_q)
    for i, f in _futs:
        lists[i] = f.result() if f is not None else _one(_idx_q[i])

    n_errored = sum(1 for lst in lists if lst is None)
    lists = [lst or [] for lst in lists]
    if not any(lists):
        if not n_errored:
            return []  # 各路都正常且都空 = 真·无结果
        # 文档化回退：全部路由失败时按原查询单路重试，异常向上传播
        logger.warning("multi-query 全部 %d 路无结果（%d 路异常），回退原查询单路检索",
                       len(queries), n_errored)
        chs = search_chunks(query, top_k=fetch_k, user_dept=user_dept,
                            query_embedding=query_embedding, acl_ctx=acl_ctx)
        if rerank_enable and chs:
            if get_config().rag.rerank_img_probe:
                chs = _probe_pool_image_refs(chs)   # 回退单路同样探测（#F-mm10a）
            from .reranker import rerank_chunks
            chs = rerank_chunks(query, chs, top_k=None, multimodal=multimodal)
        return _select_with_doc_cap(chs, top_k, get_config().rag.doc_diversity_cap)

    merged: List[Dict[str, Any]] = []
    seen = set()
    for rank in range(max(len(lst) for lst in lists)):
        for lst in lists:
            if rank >= len(lst):
                continue
            ch = lst[rank]
            key = ch.get("chunk_id") or ch.get("id") or (ch.get("doc_id"), ch.get("chunk_index"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(ch)
    # 某路重排失败降级时会出现 rerank 分（0~1）与融合分（~0-10）混用——下游
    # expand 分组排序 / 相关度标签 / 低置信护栏都假定单一分制。混用时统一回退
    # 融合分（reranker 在 _fused_score 保留了原分，可无损还原）。
    if any("rerank_score" in c for c in merged) and any("rerank_score" not in c for c in merged):
        logger.warning("multi-query 混合分制（部分路由重排失败/降级），统一回退融合分")
        for c in merged:
            if "rerank_score" in c:
                c["score"] = c.pop("_fused_score", c["score"])
                c.pop("rerank_score", None)
    logger.info("multi-query fan-out: %d 路（含原查询，%d 路异常）→ 去重合并 %d → 取 top_k=%d",
                len(queries), n_errored, len(merged), top_k)
    return _select_with_doc_cap(merged, top_k, get_config().rag.doc_diversity_cap)


def retrieve_and_enrich(
    query: str,
    *,
    top_k: Optional[int] = None,
    user_dept: Union[str, List[str], None] = None,
    stitch_window: int = 1,
    cosurface_images: bool = False,
    acl_ctx=None,
) -> List[Dict[str, Any]]:
    """统一检索 + 后处理入口，供 API 和 DingTalk 共用。

    流程：
      1. search_chunks: 三路混合检索（Dense + Sparse + BM25）+ 封面降权
      2. stitch_neighbor_chunks: 邻居拼接解决 chunk 边界断裂
      3. expand_step_context: step card 上下文扩展
      4. cosurface_doc_images: 图片召回增强（仅多模态渲染路径 opt-in）

    参数选择依据（数据驱动）：
      - top_k=7 + window=1: 估算 context ~5,700 chars ≤ max_context_chars=6,000
      - 避免 top_k 过大导致 context 溢出后被 _format_context 截断浪费
      - window=1 已验证: CC +3.1pp, AC +2.1pp, 退化率 0%

    Args:
        query: 用户查询文本
        top_k: 检索返回的 chunk 数量；None 取 RAG_TOP_K（评测锁定默认 7）
        user_dept: 用户部门（用于权限过滤）
        stitch_window: 邻居拼接窗口大小（±N）
        cosurface_images: 是否为高分文档补充 image chunk（图文渲染路径传 True；
            纯文本路径 / 不展示图片的 /api/ask 保持 False，避免无谓的 HA3 查询）。
            另受全局开关 RAG_IMAGE_COSURFACE 控制。

    Returns:
        经过检索 + 邻居拼接（+ 可选图片召回）后的 chunks 列表
    """
    if top_k is None:
        top_k = get_config().rag.default_top_k  # RAG_TOP_K（此前写死 7，环境变量是哑的）
    # 路由式重排开启时：over-fetch rerank_pool 个候选 → 重排 → 取 top_k；否则直接取 top_k。
    _av = get_config().alibaba_vector
    _fetch_k = max(_av.rerank_pool, top_k) if _av.rerank_enable else top_k
    # #F-mm10b 近平局带图倾斜（rerank OFF 专用）：over-fetch 绑死在本分支内
    # （不做独立 fetch env——单独放大 fetch 会把超池直接漏给 stitch/expand/context），
    # 倾斜后显式截回 top_k。rerank ON 时倾斜让位于重排（互斥）。
    # #F-mm10c eps 按 251 金集在 weighted 融合分（~0-10）上标定；rrf 融合分尺度（~0.0x）下
    # 绝对阈值错配 → 几乎所有相邻带图对判为近平局被无差别前移、排序被摧毁，故仅 weighted 启用。
    _tiebreak = (not _av.rerank_enable) and get_config().rag.image_tiebreak \
        and _av.hybrid_fusion != "rrf"
    if _tiebreak:
        _fetch_k = max(_fetch_k, get_config().rag.image_tiebreak_pool)
    # query embedding 只算一次，传给 search_chunks 与 cosurface（后者原本会重复嵌入一次）。
    # P2-4：嵌入失败经 _get_query_embedding_or_degraded 降级（零向量+纯 BM25），
    # 不再让整条检索链路硬失败；flag 关闭时保持原 raise。
    _emb = _get_query_embedding_or_degraded(query)
    _emb_degraded = bool(getattr(_emb, "degraded", False))
    if _emb_degraded:
        # 降级分数统一置 0（分级失效）：近平局带图倾斜的「分差 < eps」判定失去意义
        # （所有相邻对都会被判平局、无差别交换），故降级本次直接关闭 tiebreak。
        _tiebreak = False
    # 多意图查询分解（RAG_MULTI_QUERY_MODE，默认 off；失败/不触发即走原单查询路径）。
    # F#52：mode 开启时，分解 LLM（decompose_timeout 最坏 8s）与原查询主路检索【并行】——
    # 主路结果两用：不触发分解 → 直接作为单查询路径的检索结果（零重复检索、零回归）；
    # 触发 → 经 primary_supplier 喂给 fan-out 的原查询路（交错合并逻辑不变）。
    # mode=off（默认）完全不建线程，与历史行为逐字节一致。
    _sub_queries: List[str] = []
    _primary_future = None
    if get_config().rag.multi_query_mode in ("auto", "llm"):
        from .query_decomposer import maybe_decompose
        from opensearch_pipeline import serving_pools as _sp

        # D3：改用进程级 prefetch 池。⚠️ **删掉了原先的 `_px.shutdown(wait=False)`** ——
        # 那在每请求私有池上是"让线程自行回收"的正确写法，对**共享池**则会把整个进程的池
        # 关掉（codex 2026-08-06 BLOCKER）。共享池的关闭点只有 api.py 的 lifespan 退出分支。
        # ⚠️ prefetch 与 fanout **必须是两个池**：`_one(0)` 会在 fanout worker 里等这个
        # primary future，同池就是自等待/死锁（codex 抓出的 P→P 环）。
        # lambda 体内按模块全局名解析 search_chunks（monkeypatch 兼容，不做 import 期快照）
        _primary_future = _sp.submit_or_none(
            _sp.PREFETCH,
            lambda: search_chunks(query, top_k=_fetch_k, user_dept=user_dept,
                                  query_embedding=_emb, acl_ctx=acl_ctx))
        _sub_queries = maybe_decompose(query)
    if _sub_queries:
        chunks = _multi_query_search(
            query, _sub_queries, fetch_k=_fetch_k, top_k=top_k, user_dept=user_dept,
            acl_ctx=acl_ctx,
            rerank_enable=_av.rerank_enable, multimodal=bool(cosurface_images),
            query_embedding=_emb,
            primary_supplier=(_primary_future.result if _primary_future is not None else None),
        )
    else:
        # F#52：主路结果直接用（预取的就是同参 search_chunks；异常经 .result() 原样上抛，
        # 与历史同步调用的传播语义一致）；mode=off 走原同步路径。
        chunks = (_primary_future.result() if _primary_future is not None
                  else search_chunks(query, top_k=_fetch_k, user_dept=user_dept,
                                     query_embedding=_emb, acl_ctx=acl_ctx))
        _cap = get_config().rag.doc_diversity_cap
        if _av.rerank_enable and chunks:
            # #F-mm10a：探测让 VL 重排看见 step_card 的绑定图（_img_key 取
            # refs[0].oss_key），VL 路由经既有 any(_img_key) 通路自动激活。
            # ⚠️ 路由切换意味着更多 query 走 qwen3-vl-rerank：0.9/0.8 档位标签按
            # 模型分布标定，开启前须 rerank_ab 复跑 + 阈值 sanity check。
            if get_config().rag.rerank_img_probe:
                chunks = _probe_pool_image_refs(chunks)
            from .reranker import rerank_chunks
            # multimodal 渲染路径（cosurface_images=True）用 VL 重排；纯文本/钉钉机器人用文本重排。
            # 文档限额开启时不在重排内截断，先拿全池重排序，再按 cap 选 top_k。
            chunks = rerank_chunks(query, chunks, top_k=None if _cap > 0 else top_k,
                                   multimodal=bool(cosurface_images))  # 失败自动降级为原始顺序
        elif _tiebreak and chunks:
            # #F-mm10b：探测 → 近平局带图前移 → 显式截回 top_k（over-fetch 收口，
            # 绝不把超池漏给 stitch/expand/context）。cap>0 时由 doc-cap 选择收口。
            chunks = _probe_pool_image_refs(chunks)
            chunks = _image_tiebreak_reorder(chunks, get_config().rag.image_tiebreak_eps)
            if _cap <= 0:
                chunks = chunks[:top_k]
        if _cap > 0:
            chunks = _select_with_doc_cap(chunks, top_k, _cap)
    # F#53：cosurface 的 HA3 补图查询与 stitch/expand 并行预取。doc_id【全集】不被 stitch/expand
    # 改变（两者只透传/继承命中 chunk 的 doc_id，不引入新 doc）；但 expand 的分组重排可能改变
    # 【前 max_docs 个去重 doc_id 的顺序】→ cosurface_doc_images 在合并时校验 doc_ids 严格一致
    # 才采用预取，失配则回退串行查询（插入位置/去重语义不变）。「已有 image chunk 则短路」的
    # 判定对 stitch/expand 不变（两者不增删 image 类型 chunk），可安全前置。
    # P2-4：降级时零向量无法按相关度挑图，cosurface 补图查询无意义 → 跳过（预取与串行
    # 两处一并跳过；与本模块 fail-open 风格一致：只降增强，不降正文结果）。
    _cos_pref = None
    if chunks and cosurface_images and get_config().rag.image_cosurface \
            and not _emb_degraded \
            and not any(c.get("chunk_type") == "image" for c in chunks):
        _pre_ids = _cosurface_top_doc_ids(chunks)
        if _pre_ids:
            try:
                from opensearch_pipeline import serving_pools as _sp

                # D3：改用进程级 prefetch 池；⚠️ **删掉了原先的 `_cx.shutdown(wait=False)`**
                # （对共享池调用会毒化整个进程）。饱和 ⇒ `_cos_pref=None` ⇒ 走下方既有的
                # 串行 cosurface 路径，与"预取起不来"完全同一条 fail-open 分支。
                # ⚠️ acl_ctx 必须与串行路径同参（2026-07-31）：预取命中时其结果被**直接采用**
                # （下方仅比对 doc_ids 是否一致，不重算），漏传就变成"预取时没图、串行时有图"
                # 这种随并发时序漂移的 node 文档配图缺失。
                _cf = _sp.submit_or_none(
                    _sp.PREFETCH, _fetch_cosurface_images, query, _pre_ids, user_dept, _emb,
                    acl_ctx=acl_ctx)
                _cos_pref = (_pre_ids, _cf) if _cf is not None else None
            except Exception as e:   # noqa: BLE001 — 预取起不来 → 回退串行（fail-open）
                logger.warning("cosurface 预取启动失败（回退串行查询）: %s", e)
                _cos_pref = None
    # F#60：stitch 与 expand 共享同一次连接池 checkout——开线程局部连接作用域（不改两函数
    # 的调用签名，测试桩兼容）：两阶段中第一个真正需要连库的自取并寄存，第二个复用，此处
    # 统一归还。两函数各自的 fail-open 语义不变；作用域内未连库则本块零开销。
    _scope_opened = False
    if chunks and not getattr(_conn_scope, "active", False):
        _conn_scope.active = True
        _conn_scope.conn = None
        _scope_opened = True
    try:
        if chunks and stitch_window > 0:
            chunks = stitch_neighbor_chunks(chunks, window=stitch_window)
        # Step Card 上下文扩展
        if chunks:
            chunks = expand_step_context(chunks, query)
    finally:
        if _scope_opened:
            _shared_conn = getattr(_conn_scope, "conn", None)
            _conn_scope.active = False
            _conn_scope.conn = None
            if _shared_conn is not None:
                try:
                    _shared_conn.close()   # 池化连接 close = 归还池
                except Exception:   # noqa: BLE001
                    pass
    # 消费点③（图片版本门）：**expand_step_context** 会从 RDS 新装 image_refs
    # （给 step_card / visual_knowledge / 展开出来的兄弟步骤）——那是独立于主命中的注入面，
    # 主命中过了门不代表这里装上的是 serving 版本。
    # ⚠️ 注意 stitch_neighbor_chunks **不**装图（它的 SELECT 只有 doc_id/version_no/
    # chunk_index/chunk_text/section_title/permission_level/owner_dept 七列）——本注释此前
    # 写成"stitch/expand 都会装"，会误导后续接线审计（codex 2026-08-06 点名）。
    # 放在连接作用域**之外**（与 _attach_doc_dates 同侧），不掺进 stitch/expand 那次 checkout。
    if chunks:
        chunks = _deny_stale_version_images(chunks, source="expand 后")
    # 图片召回增强（仅多模态渲染路径 opt-in；可经 RAG_IMAGE_COSURFACE 全局关闭；
    # P2-4 嵌入降级时跳过，理由见上方 _cos_pref 注释）
    if chunks and cosurface_images and get_config().rag.image_cosurface and not _emb_degraded:
        chunks = cosurface_doc_images(query, chunks, user_dept=user_dept, query_embedding=_emb,
                                      prefetched=_cos_pref, acl_ctx=acl_ctx)
    # P2-31（盲区审计）：附文档日期（版本落库日，RDS 现查、fail-open）——此前索引→上下文→
    # 来源整条链无任何时间信号，模型与用户都无法区分 2023 版与 2025 版 SOP。
    _attach_doc_dates(chunks)
    return chunks


def _attach_doc_dates(chunks: List[Dict[str, Any]]) -> None:
    """给命中 chunk 就地附 `doc_date`（当前版本 document_version.created_at 的日期部分）。

    P2-31 的 serving 侧半边：HA3 schema 加日期字段需整表重建（user-gated），先用 RDS 现查
    把日期送进 _chunk_header（模型可推理时效）与 _extract_sources（卡片可渲染）。版本落库日
    是现有最好的时效近似（真实生效日期语料里没有采集）；fail-open——查不到不附、绝不影响答案。"""
    try:
        if not chunks:
            return
        from opensearch_pipeline.config import get_config
        if get_config().simulate_db:
            return
        ids = sorted({c.get("doc_id") for c in chunks if c.get("doc_id")})
        if not ids:
            return
        from opensearch_pipeline.db import _get_db_conn   # 惰性：tests monkeypatch db._get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                ph = ",".join(["%s"] * len(ids))
                cur.execute(
                    "SELECT dv.doc_id, DATE(dv.created_at) FROM document_version dv"
                    " JOIN document_meta dm ON dm.doc_id = dv.doc_id"
                    "  AND dm.current_version_no = dv.version_no"
                    f" WHERE dv.doc_id IN ({ph})", tuple(ids))
                dates = {str(r[0]): str(r[1]) for r in (cur.fetchall() or []) if r and r[1]}
        finally:
            conn.close()
        for c in chunks:
            d = dates.get(str(c.get("doc_id") or ""))
            if d:
                c.setdefault("doc_date", d)
    except Exception:   # noqa: BLE001 — 辅助增强绝不破坏回答主链路
        logger.debug("doc_date 附加失败（fail-open）", exc_info=True)
