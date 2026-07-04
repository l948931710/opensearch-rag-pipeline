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
_VALID_ACL_GROUPS = frozenset({
    "finance", "it", "marketing", "production",
    "pmc", "admin", "hr", "rd", "quality", "supply",
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
# Only APPROVED + real (live-in-data) owners. Unapproved production_* owners (incl.
# the production_papercup double-spelling and not-yet-live production_injection) are
# deliberately excluded → they fail closed and surface via audit_production_owner_taxonomy()
# until explicitly approved + added here. Mirrors the live active-chunk owner set.
_PRODUCTION_UMBRELLA_OWNERS = frozenset({
    "production",                 # the umbrella owner itself (exact)
    "production_mold",
    "production_paper_cup",
    "production_thermoforming",
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


def _build_permission_filter(user_dept: Union[str, List[str], None]) -> str:
    """构建 HA3 权限过滤表达式（安全边界，单一实现）。

    放行 public；对用户所属的每个 ACL 组额外放行该组的 dept_internal。入参经
    _normalize_acl_groups（净化 + 白名单 + 去重）后才进表达式；为空 → 仅 public（fail-closed）。
    所有调用点（search_chunks / cosurface_doc_images / 本地回退）共用，权限规则只有一处。
    完整括号包裹每个子句，避免 HA3 对 AND/OR 优先级的歧义。
    'production' 伞组经 _expand_groups_to_owners 展开为各 production* 子线 owner（其余组精确匹配）。
    """
    groups = _normalize_acl_groups(user_dept)
    if not groups:
        return '(permission_level="public")'
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
    return base


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


def _deny_revoked_cross_dept(results, user_dept):
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
    if not get_config().rag.allowed_depts_acl or not results:
        return results
    norm = _normalize_acl_groups(user_dept)
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


def _search_chunks_opensearch(
    query: str,
    dense: List[float],
    top_k: int,
    user_dept: Union[str, List[str], None] = None,
) -> List[Dict[str, Any]]:
    """本地开发回退检索：标准 OpenSearch dense kNN(0.7) + BM25(0.3)。

    仅当 HA3 endpoint 未配置且 opensearch.host 已配置时由 search_chunks 调用
    （生产配置 HA3，本分支不可达）。返回与 _parse_ha3_response 同形的 chunk 字典，
    权限语义与 HA3 过滤一致（public 或 dept_internal+本部门）；
    封面降权逻辑与 HA3 路径保持同款。
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

    fetch_k = max(top_k * 2, top_k + 5)
    body = {
        "size": fetch_k,
        "_source": ["chunk_id", "id", "doc_id", "chunk_text", "chunk_type", "title",
                    "section_title", "chunk_index", "page_num", "kb_type",
                    "permission_level", "owner_dept", "category_l1",
                    "source_image", "visual_summary"],
        "query": {"bool": {
            "should": [
                {"knn": {"chunk_vector": {"vector": dense, "k": fetch_k, "boost": 0.7}}},
                {"match": {"chunk_text": {"query": query, "boost": 0.3}}},
            ],
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


def search_chunks(
    query: str,
    *,
    top_k: int = 5,
    min_score: float = 0.0,
    max_distance: float = 0.0,
    output_fields: Optional[List[str]] = None,
    user_dept: Union[str, List[str], None] = None,
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
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

    # 1. 生成 query embedding（retrieve_and_enrich 会预算一次并传入，避免重复嵌入）
    dense, sparse_idx, sparse_val = query_embedding if query_embedding is not None else get_query_embedding(query)

    # 2. 构建 sparse data
    from alibabacloud_ha3engine_vector.models import QueryRequest, SparseData

    sparse_data = None
    if sparse_idx:
        sparse_data = SparseData(
            count=[len(sparse_idx)],
            indices=sparse_idx,
            values=sparse_val,
        )

    _output_fields = output_fields or list(_DEFAULT_OUTPUT_FIELDS)

    # ── 权限过滤（安全边界，统一实现）──
    filter_expr = _build_permission_filter(user_dept)

    logger.info("Permission filter: user_dept=%s, filter=%s", user_dept, filter_expr)

    # 3. 构建请求并执行
    # 本地开发回退：HA3 未配置且本地 OpenSearch 可用时走标准 OpenSearch 检索
    # （dense kNN 0.7 + BM25 0.3，与线上 weighted 融合同权重；
    #  生产配置了 HA3 endpoint，此分支不可达 —— 2026-06-10 本地 E2E 引入）
    _full_cfg = get_config()
    if not _full_cfg.alibaba_vector.endpoint and getattr(_full_cfg.opensearch, "host", ""):
        return _deny_revoked_cross_dept(_search_chunks_opensearch(query, dense, top_k, user_dept), user_dept)

    client = _get_ha3_client()

    if cfg.enable_hybrid:
        # ── 混合检索: Dense + Sparse + BM25 三路融合 ──
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
        if cfg.hybrid_fusion == "rrf":
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
            "Hybrid search: fusion=%s, text_field=%s, knn_top_k=%d, size=%d",
            cfg.hybrid_fusion, cfg.text_search_field, cfg.hybrid_knn_top_k, top_k,
        )
        resp = client.search(request)
    else:
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

    # 4. 解析结果
    results = _parse_ha3_response(resp)

    # 4b. 查询侧拒绝（Phase D 读侧 fail-closed 复核）：撤销跨部门授权后即时生效，不等 HA3 投影收回。
    results = _deny_revoked_cross_dept(results, user_dept)

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
    seen: Dict[str, Dict[str, Any]] = {}
    for c in expanded_all:
        cid = c.get("chunk_id") or c.get("id", "")
        if cid in seen:
            if c.get("score", 0) > seen[cid].get("score", 0):
                seen[cid] = c
        else:
            seen[cid] = c
    deduped = list(seen.values())

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
    perm = _build_permission_filter(user_dept)
    filter_expr = f'chunk_type="image" AND ({doc_clause}) AND ({perm})'

    req = QueryRequest(
        table_name=cfg.table_name,
        vector=dense,
        sparse_data=sparse_data,
        top_k=max_images * 2,
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
                query, doc_ids, user_dept, query_embedding, max_images=max_images)
    except Exception as e:
        logger.warning("图片召回补充失败 (non-fatal): %s", e)
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
    step_card 批量 IN 查 chunk_meta.image_refs_json（PK 索引、一次往返）并经
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
    try:
        import pymysql.cursors
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        ph = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"SELECT chunk_id, image_refs_json FROM chunk_meta "
            f"WHERE chunk_id IN ({ph}) AND is_active = 1",
            tuple(ids),
        )
        for row in cursor.fetchall():
            refs_by_id[row["chunk_id"]] = row.get("image_refs_json")
        cursor.close()
        conn.close()
    except Exception as e:
        logger.warning("候选池带图探测失败（忽略，fail-open）: %s", e)
        return chunks
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

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as ex:
        lists = list(ex.map(_one, enumerate(queries)))

    n_errored = sum(1 for lst in lists if lst is None)
    lists = [lst or [] for lst in lists]
    if not any(lists):
        if not n_errored:
            return []  # 各路都正常且都空 = 真·无结果
        # 文档化回退：全部路由失败时按原查询单路重试，异常向上传播
        logger.warning("multi-query 全部 %d 路无结果（%d 路异常），回退原查询单路检索",
                       len(queries), n_errored)
        chs = search_chunks(query, top_k=fetch_k, user_dept=user_dept,
                            query_embedding=query_embedding)
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
    # query embedding 只算一次，传给 search_chunks 与 cosurface（后者原本会重复嵌入一次）
    _emb = get_query_embedding(query)
    # 多意图查询分解（RAG_MULTI_QUERY_MODE，默认 off；失败/不触发即走原单查询路径）。
    # F#52：mode 开启时，分解 LLM（decompose_timeout 最坏 8s）与原查询主路检索【并行】——
    # 主路结果两用：不触发分解 → 直接作为单查询路径的检索结果（零重复检索、零回归）；
    # 触发 → 经 primary_supplier 喂给 fan-out 的原查询路（交错合并逻辑不变）。
    # mode=off（默认）完全不建线程，与历史行为逐字节一致。
    _sub_queries: List[str] = []
    _primary_future = None
    if get_config().rag.multi_query_mode in ("auto", "llm"):
        from .query_decomposer import maybe_decompose
        from concurrent.futures import ThreadPoolExecutor

        _px = ThreadPoolExecutor(max_workers=1)
        try:
            # lambda 体内按模块全局名解析 search_chunks（monkeypatch 兼容，不做 import 期快照）
            _primary_future = _px.submit(
                lambda: search_chunks(query, top_k=_fetch_k, user_dept=user_dept,
                                      query_embedding=_emb))
        finally:
            _px.shutdown(wait=False)   # 不阻塞：future 照常完成，线程随后回收
        _sub_queries = maybe_decompose(query)
    if _sub_queries:
        chunks = _multi_query_search(
            query, _sub_queries, fetch_k=_fetch_k, top_k=top_k, user_dept=user_dept,
            rerank_enable=_av.rerank_enable, multimodal=bool(cosurface_images),
            query_embedding=_emb,
            primary_supplier=(_primary_future.result if _primary_future is not None else None),
        )
    else:
        # F#52：主路结果直接用（预取的就是同参 search_chunks；异常经 .result() 原样上抛，
        # 与历史同步调用的传播语义一致）；mode=off 走原同步路径。
        chunks = (_primary_future.result() if _primary_future is not None
                  else search_chunks(query, top_k=_fetch_k, user_dept=user_dept,
                                     query_embedding=_emb))
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
    _cos_pref = None
    if chunks and cosurface_images and get_config().rag.image_cosurface \
            and not any(c.get("chunk_type") == "image" for c in chunks):
        _pre_ids = _cosurface_top_doc_ids(chunks)
        if _pre_ids:
            try:
                from concurrent.futures import ThreadPoolExecutor

                _cx = ThreadPoolExecutor(max_workers=1)
                try:
                    _cos_pref = (_pre_ids, _cx.submit(
                        _fetch_cosurface_images, query, _pre_ids, user_dept, _emb))
                finally:
                    _cx.shutdown(wait=False)
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
    # 图片召回增强（仅多模态渲染路径 opt-in；可经 RAG_IMAGE_COSURFACE 全局关闭）
    if chunks and cosurface_images and get_config().rag.image_cosurface:
        chunks = cosurface_doc_images(query, chunks, user_dept=user_dept, query_embedding=_emb,
                                      prefetched=_cos_pref)
    return chunks
