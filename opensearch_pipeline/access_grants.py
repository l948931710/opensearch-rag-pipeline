# -*- coding: utf-8 -*-
"""access_grants.py — Phase D 跨部门检索授权的【唯一聚合注入点】。

事实来源（authority）= `fuling_knowledge.kb_access_request`（status='approved'）。
`chunk_meta.allowed_depts`（RDS）与 HA3 `allowed_depts` 字段都只是其【物化投影】——
任何写/重建路径（普通 ingestion / 升版 / re-chunk / HA3 rebuild / 回填脚本）都必须经本模块
解析 allowed_depts 再写，否则文档一旦重建就丢授权（Phase D 约束 2：单一注入点）。

授权值 = 被授权检索本文档的【用户组码】集合（= 申请人 managed_owner_depts，组代码粒度），
经写组码白名单（kb_authz._valid_owner_depts = retriever._VALID_ACL_GROUPS）校验 + 去重 + 稳定
排序；未知/非白名单码 **fail-closed 丢弃** 并显式 warning（约束 6：不静默吞）。授权按逻辑 doc_id
生效、自动跟随 current version（约束 3：调用方按 current-version 关系定位 chunk，本模块只按 doc_id 聚合）。
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, List, Set, Tuple

from opensearch_pipeline.reindex_states import ChunkIndexStatus, DocVersionIndexStatus

logger = logging.getLogger(__name__)


class NodeAclAuthorityUnavailable(RuntimeError):
    """node-ACL 权威**不可达**（≠「查得到、结果是零授权」）。

    只在 `strict=True`（serving ENFORCE 路径）抛出。宽松路径（写路径）保持原有
    「读不到 ⇒ 按空集/legacy」的兼容行为不变。

    ⚠️ 存在的理由:把故障伪装成「正常的零授权」会让 serving 无法区分两件事 ——
    「这篇没人被授权」和「我读不到授权表」。前者应拒该篇,后者应拒**全部非 public**。
    调用方（`retriever._deny_revoked_cross_dept`）据此整体 fail-closed。
    """


# ── capability 探测的 positive-only 进程内缓存 ────────────────────────────────
# **只缓存 True**,按 (host, database) 分键。
#   · schema/060 是 additive migration ⇒ 「列已存在」是**单调事实**,缓存不会过期变假;
#   · 缓存 False 则会制造真实窗口:apply 之后、缓存失效之前,serving 仍把 node 文档
#     解析成 legacy —— 那正是本批要消灭的 stale-owner 模式混淆(codex 共识 2026-07-31)。
#     故 False 不缓存,apply 后**下一次请求即生效**(原 docstring 承诺的语义得以保留)。
#   · 异常同样不缓存;strict 下向上抛,宽松下退回 False。
# ⚠️ 已知后果:若有人**回滚** 060(删列),缓存仍报 True ⇒ acl_mode 查询 1054 ⇒ strict 下
#   异常上抛 ⇒ 全部非 public 命中被丢弃,直到进程重启。方向是 fail-closed 且报错响亮,
#   刻意不做自愈重试(回滚 additive 列不是计划内操作,静默自愈只会掩盖它)。
_NODE_SCHEMA_PRESENT: Set[Tuple[str, str]] = set()
_NODE_SCHEMA_LOCK = threading.Lock()


def _schema_cache_key() -> Tuple[str, str]:
    """(物理 host, 库名) —— 测试/staging/未来多目标进程之间绝不串值。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    return (str(getattr(cfg.rds, "host", "") or ""), str(getattr(cfg.rds, "database", "") or ""))


def _kb_db() -> str:
    """知识库库名（kb_access_request/chunk_meta/document_*/kb_acl_projection_outbox 所在库）；
    经 RAG_RDS_DATABASE 配置（STAGING=_stg）。惰性读 config（caller-cursor 路径也共用同一库名）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database


def resolve_allowed_depts(doc_ids: Iterable[str], cursor) -> Dict[str, List[str]]:
    """聚合给定 doc_id 集的 approved 跨部门检索授权 → {doc_id: [组码...]}。

    - 只返回【有】approved 授权的 doc（无授权的 doc 不在返回里 → 调用方按空 [] 处理）。
    - 组码经白名单 + 去重 + 稳定排序；非白名单码丢弃并 per-doc warning。
    - cursor：调用方提供的 `fuling_knowledge` 库游标（连接/事务由调用方掌控，本模块不建池）。
    """
    ids = [d for d in dict.fromkeys(doc_ids) if d]   # 去重保序
    if not ids:
        return {}
    from opensearch_pipeline.kb_authz import sanitize_owner_depts, _valid_owner_depts
    whitelist = _valid_owner_depts()
    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(
        f"SELECT doc_id, requester_depts FROM {_kb_db()}.kb_access_request "
        f"WHERE status='approved' AND doc_id IN ({placeholders})",
        tuple(ids),
    )
    raw: Dict[str, List[str]] = {}
    for row in cursor.fetchall():
        doc_id, rdepts = row[0], row[1]
        if not doc_id:
            continue
        raw.setdefault(doc_id, []).extend((rdepts or "").split(","))

    out: Dict[str, List[str]] = {}
    for doc_id, codes in raw.items():
        clean = sanitize_owner_depts(codes)                 # 白名单内、去重、有序（单一净化口径）
        dropped = sorted({
            c.strip() for c in codes
            if c.strip() and c.strip() not in whitelist
        })
        if dropped:
            logger.warning(
                "kb_access_request doc=%s 含非白名单授权组码（fail-closed 丢弃、不放行）: %s",
                doc_id, dropped,
            )
        if clean:
            out[doc_id] = clean
    return out


# ── node-ACL：结构化权威解析（两个权威源【分别】解析，只在投影边界编码）──────────
def _node_acl_columns_present(cursor, *, strict: bool = False) -> bool:
    """schema/060 是否已 apply（`document_meta.acl_mode` 存在）。

    未 apply → 全库恒 legacy，node 分支彻底惰化（与 049 的 1054 回退同型：代码可先部署，
    apply 是 user-gated）。

    缓存语义（2026-07-31，B-2 把本探测搬上了 serving 热路径）：
    **True 按 (host, database) 进程内缓存；False 与异常都不缓存** ——
    见 `_NODE_SCHEMA_PRESENT` 的说明。apply 后下一次请求即生效，与原「不缓存」承诺等价。

    strict=True（仅 serving ENFORCE 路径）：探测异常**向上抛**，不再伪装成「未 apply」。
    """
    key = _schema_cache_key()
    if key in _NODE_SCHEMA_PRESENT:
        return True
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='document_meta' AND COLUMN_NAME='acl_mode'",
            (_kb_db(),))
        row = cursor.fetchone()
        present = bool(row and row[0])
    except Exception as e:   # noqa: BLE001
        if strict:
            # serving:探不出 capability ⇒ 无法安全区分 node/legacy ⇒ 交给调用方整体 fail-closed
            raise NodeAclAuthorityUnavailable("node-ACL 列探测失败，无法判定 acl_mode 归属") from e
        logger.warning("node-ACL 列探测失败，按未 apply 处理（全库 legacy）: %s", e)
        return False
    if present:
        with _NODE_SCHEMA_LOCK:
            _NODE_SCHEMA_PRESENT.add(key)   # 并发首请求可能重复探测一次，无害
    return present


def resolve_acl_modes(doc_ids: Iterable[str], cursor) -> Dict[str, str]:
    """轻量批查 {doc_id: acl_mode}（写路径热路径用；060 未 apply / 查不到 → 全 legacy）。

    为什么单列一个函数:写路径(ingestion / 升版 / re-chunk / stage-3 reload)**每批都要**
    知道模式才能决定投影形态,但绝大多数批次 100% 是 legacy —— 只做一次 document_meta
    单列 SELECT 即可判定,避免为此把 `resolve_doc_acl` 的三次查询摊到每批。
    """
    from opensearch_pipeline.acl_policy import ACL_MODE_LEGACY, ACL_MODE_NODE
    ids = [d for d in dict.fromkeys(doc_ids) if d]
    if not ids:
        return {}
    out = {d: ACL_MODE_LEGACY for d in ids}
    if not _node_acl_columns_present(cursor):
        return out
    try:
        ph = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"SELECT doc_id, acl_mode FROM {_kb_db()}.document_meta WHERE doc_id IN ({ph})",
            tuple(ids))
        for doc_id, mode in cursor.fetchall():
            m = str(mode or "").strip().lower()
            out[doc_id] = m if m in (ACL_MODE_LEGACY, ACL_MODE_NODE) else ACL_MODE_LEGACY
    except Exception as e:   # noqa: BLE001 — 读不到 ⇒ 按 legacy(退回现状语义,绝不误升 node)
        logger.warning("resolve_acl_modes 失败，按 legacy 处理: %s", e)
    return out


def resolve_doc_acl(doc_ids: Iterable[str], cursor, *, strict: bool = False) -> Dict[str, "object"]:
    """聚合给定 doc 的【结构化 ACL 权威】→ {doc_id: acl_policy.DocAcl}。

    **strict=True 仅供 serving ENFORCE 路径**（`retriever._deny_revoked_cross_dept`）。
    写路径（ingestion / 升版 / re-chunk / materialize）一律用默认宽松语义 —— 那里的
    「部署先于 schema apply」兼容行为是刻意的，不能被本参数全局改掉。

    | 情形 | 宽松（默认，写路径） | strict（serving） |
    |---|---|---|
    | capability 探测异常 | 吞 ⇒ 全 legacy | **抛** `NodeAclAuthorityUnavailable` |
    | 探测成功、060 确未 apply | 全 legacy | 全 legacy（**相同** —— 合法部署态，不是故障） |
    | `document_meta` 缺行 | 合成默认 legacy DocAcl | **不返回该 doc**（调用方 `.get()` → None ⇒ 拒） |
    | 未知 `acl_mode` | 改写为 legacy | **不返回该 doc** ⇒ 拒 |
    | `kb_doc_node_grant` 读失败 | 按空集 | **抛** `NodeAclAuthorityUnavailable` |
    | 节点授权正常返回空集 | 零授权 DocAcl | 零授权 DocAcl（**相同** —— 查得到就不是故障） |

    为什么 strict 下「缺行/未知 mode」不能退回 legacy:退回 legacy 会让 `_legacy_readable`
    按 owner 放行 —— 而一篇 node 文档在投影未收敛期，HA3 里的 owner 可能正是调用者的组码。
    那恰好是本轮要堵的 stale-owner 窗口（codex 共识 2026-07-31）。

    ⚠️ **两个权威源分别解析、各自校验**，只在 RDS/HA3 投影边界才由
    `acl_policy.project_doc_acl` 编码成混合集合 —— 绝不能先把组码与 `d:`/`dx:` 混进一个
    列表再送 `sanitize_owner_depts`（组码白名单会把节点值静默丢光）。

      · kb_access_request(status='approved') → groups（过组码白名单，legacy 语义）
      · kb_doc_node_grant(revoked_at IS NULL) → node_ids / exact_node_ids（node 语义）
      · document_meta → acl_mode / permission_level / owner_dept（模式与敏感级权威）

    schema/060 未 apply ⇒ 恒返回 legacy 形态（node 字段为空），行为与现状逐字节一致。

    ⚠️ cursor 契约 = **tuple 游标**（与本模块其余函数一致；serving 路径 `db._get_db_conn()`
    实测返回 tuple）。**别传 `prod_access.get_prod_readonly_conn()` 的游标** —— 它默认
    `DictCursor`，行按列名取值，这里的 `row[1]` 会 KeyError（2026-07-29 核查脚本踩过）。
    需用 prod_access 核查时传 `dict_cursor=False`。
    """
    from opensearch_pipeline.acl_policy import (
        ACL_MODE_LEGACY, ACL_MODE_NODE, DocAcl, normalize_node_ids,
    )
    ids = [d for d in dict.fromkeys(doc_ids) if d]
    if not ids:
        return {}
    ph = ",".join(["%s"] * len(ids))
    has_node = _node_acl_columns_present(cursor, strict=strict)

    # 1. 文档自身（模式 / 敏感级 / 真实 owner）
    cols = "doc_id, permission_level, owner_dept" + (", acl_mode" if has_node else "")
    cursor.execute(f"SELECT {cols} FROM {_kb_db()}.document_meta WHERE doc_id IN ({ph})", tuple(ids))
    meta: Dict[str, tuple] = {}
    for row in cursor.fetchall():
        mode = (row[3] if has_node and len(row) > 3 else None) or ACL_MODE_LEGACY
        meta[row[0]] = ((row[1] or ""), (row[2] or ""), str(mode).strip().lower())

    # 2. legacy 权威：approved 跨部门组码（复用既有单一注入点，白名单/去重/告警口径不变）
    groups_by_doc = resolve_allowed_depts(ids, cursor)

    # 3. node 权威：active 节点授权，按 scope 分流
    nodes: Dict[str, List[int]] = {}
    exacts: Dict[str, List[int]] = {}
    if has_node:
        try:
            cursor.execute(
                f"SELECT doc_id, dept_id, scope FROM {_kb_db()}.kb_doc_node_grant "
                f"WHERE revoked_at IS NULL AND doc_id IN ({ph})", tuple(ids))
            for doc_id, dept_id, scope in cursor.fetchall():
                bucket = exacts if str(scope or "").strip().lower() == "exact" else nodes
                bucket.setdefault(doc_id, []).append(dept_id)
        except Exception as e:   # noqa: BLE001
            if strict:
                # 「读不到授权表」≠「这篇零授权」。serving 必须能区分,故整体 fail-closed。
                raise NodeAclAuthorityUnavailable("kb_doc_node_grant 读取失败") from e
            logger.warning("kb_doc_node_grant 读取失败，节点授权按空集处理（fail-closed）: %s", e)
            nodes, exacts = {}, {}

    out: Dict[str, object] = {}
    for doc_id in ids:
        if strict and doc_id not in meta:
            # 缺 document_meta 行 ⇒ mode 不可判 ⇒ 不返回(调用方按拒处置)。
            # 绝不合成 legacy DocAcl:那会让 _legacy_readable 按 owner 放行陈旧 node 命中。
            logger.warning("doc=%s 无 document_meta 行,strict 下不返回 ACL（调用方 fail-closed）", doc_id)
            continue
        perm, owner, mode = meta.get(doc_id, ("", "", ACL_MODE_LEGACY))
        if mode not in (ACL_MODE_LEGACY, ACL_MODE_NODE):
            if strict:
                logger.error("doc=%s 未知 acl_mode=%r,strict 下不返回 ACL（调用方 fail-closed）",
                             doc_id, mode)
                continue
            logger.warning("doc=%s 未知 acl_mode=%r ⇒ 按 legacy 解析", doc_id, mode)
            mode = ACL_MODE_LEGACY
        n_ids, n_ov = normalize_node_ids(nodes.get(doc_id, ()))
        e_ids, e_ov = normalize_node_ids(exacts.get(doc_id, ()))
        if n_ov or e_ov:
            # 权威表里的既存超限脏数据：取确定性安全子集（normalize 已按首次出现截序）
            # 并高优告警——不静默放行全部，也不整篇拒绝。
            logger.error("doc=%s 节点授权超上限（脏数据）⇒ 取安全子集并告警", doc_id)
        out[doc_id] = DocAcl(
            mode=mode, permission_level=perm, owner_dept=owner,
            groups=tuple(groups_by_doc.get(doc_id, ())),
            node_ids=tuple(n_ids), exact_node_ids=tuple(e_ids),
        )
    return out


def resolve_allowed_depts_one(doc_id: str, cursor) -> List[str]:
    """单文档便利：该 doc 的授权组码列表（无授权 → []）。"""
    if not doc_id:
        return []
    return resolve_allowed_depts([doc_id], cursor).get(doc_id, [])


def projection_rows_all_match(cursor, doc_id: str, version_no: int,
                             want_allowed: List[str], want_owner: str) -> bool:
    """**逐行**确认该版本全部 active chunk 的 `(owner_dept, allowed_depts)` 都等于期望值。

    C3′/062 certify-only 专用（codex 2026-08-03 BLOCKER）：**不能用
    `current_allowed_for_doc` 的并集口径来 certify** —— 并集会掩盖逐行不一致。
    反例：期望 `["finance"]`，一行是 `["finance"]`、另一行是 `NULL`，**并集仍等于期望**，
    据此盖章就把"一半 chunk 根本没投影"认证成了"已按权威投影"。

    与 diff 口径的分工：
      · `current_allowed_for_doc`（并集）→ 判**要不要重投影**（少计只会朝重投影自愈，安全）
      · 本函数（逐行全等）→ 判**能不能盖章**（宁可不盖，绝不误认证）
    坏 JSON / 任何一行不匹配 ⇒ 直接 False（fail-closed，交由正常 materialize 重投影）。
    """
    import json as _json
    _want = sorted(want_allowed or [])
    cursor.execute(
        f"SELECT owner_dept, allowed_depts FROM {_kb_db()}.chunk_meta "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (doc_id, version_no),
    )
    rows = cursor.fetchall()
    if not rows:
        return False                      # 无 active chunk：没有可认证的对象
    for owner, ad in rows:
        if (owner or "") != (want_owner or ""):
            return False
        if isinstance(ad, list):
            got = sorted(ad)
        elif not ad:
            got = []
        else:
            try:
                parsed = _json.loads(ad)
                got = sorted(parsed) if isinstance(parsed, list) else None
            except (ValueError, TypeError):
                return False              # 坏 JSON ⇒ 不认证
            if got is None:
                return False
        if got != _want:
            return False
    return True


def current_allowed_for_doc(cursor, doc_id: str, version_no: int) -> List[str]:
    """该 doc 指定版本 active chunk 现存的 allowed_depts 并集（去重、稳定排序）。

    用于 diff「应有 vs 现存」——decide 端点的同步标脏、allowed_depts_reconcile 对账、回填脚本
    共用同一口径（单一 diff 实现，避免三处漂移）。cursor 由调用方提供（连接/事务自掌）。
    """
    import json as _json
    cursor.execute(
        f"SELECT DISTINCT allowed_depts FROM {_kb_db()}.chunk_meta "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (doc_id, version_no),
    )
    vals = set()
    for (ad,) in cursor.fetchall():
        if not ad:
            continue
        if isinstance(ad, list):
            vals.update(ad)
            continue
        # 单行坏 JSON 不得 abort 整篇 doc 的 ACL 投影/对账（否则该文档授权永久卡死、无法收敛）：
        # 跳过坏行并告警；少计 current 只会让 reconcile 朝"重投影"自愈方向，绝不越权扩散。
        try:
            parsed = _json.loads(ad)
        except (ValueError, TypeError):
            logger.warning("current_allowed_for_doc: 跳过 doc=%s v=%s 的坏 allowed_depts JSON: %r",
                           doc_id, version_no, str(ad)[:80])
            continue
        vals.update(parsed or [])
    return sorted(vals)


def gate_by_permission(
    allowed: Dict[str, List[str]], permission_by_doc: Dict[str, str]
) -> Dict[str, List[str]]:
    """纵深防御守卫：只有【当前/将投影版本】permission_level=='dept_internal' 的文档保留 allowed_depts。

    - allowed: `resolve_allowed_depts` 的输出 {doc_id: [组码...]}。
    - permission_by_doc: {doc_id: permission_level}——调用方按【权威来源】提供：ingestion / 重推用
      chunk 自身的 permission_level（= 将写入的新版本，权威）；回填用当前 active chunk 的 permission_level。
    - 非 dept_internal（restricted / public / 未知 None）→ 丢弃 + warning。

    为何要这层（审计 Step 4 backstop a）：一篇文档在提交时是 dept_internal、获批授权后被改判为
    restricted（重传到 restricted 路径→新版本），若不守卫，approved 行会把 allowed_depts 物化到
    restricted chunk 上。消费侧（retriever）已把 allowed_depts OR 项 AND-bind 到
    permission_level='dept_internal'（故今天不泄露），本守卫在【写入源头】再加一层：restricted 文档
    绝不携带 allowed_depts，杜绝任何未来旁路 filter 因残留字段而泄露。被丢弃的 approved 文档在回填
    materialize 循环里会以空 want 清空（= 撤销其残留物化）。
    """
    out: Dict[str, List[str]] = {}
    for doc_id, groups in allowed.items():
        if permission_by_doc.get(doc_id) == "dept_internal":
            out[doc_id] = groups
        else:
            logger.warning(
                "allowed_depts 守卫：doc=%s permission_level=%r != dept_internal → 不物化（纵深防御）",
                doc_id, permission_by_doc.get(doc_id),
            )
    return out


def _current_owner_set_for_doc(cursor, doc_id: str, version_no: int) -> set:
    """该 doc 指定版本 active chunk 的**检索投影轴** owner_dept **集合**。

    ⚠️ 读的是 `chunk_meta.owner_dept`(检索投影轴),**不是** `document_meta.owner_dept`
    (归属/管理轴,node 模式下保持真实属主不变)。

    返回集合而非"首个值"(C3,2026-08-03):旧实现 `sorted(...)[0]` 在**混合 owner**
    (同版本部分 chunk 已投影、部分还挂旧 owner —— 上一轮写到一半失败/并发)时,只要排序
    首项恰好等于期望值就判一致 ⇒ 剩余错 owner 的 chunk 永久不被修正,旧 owner 组持续可读。
    调用方按 `owner_set == {expected}` 判定;空集 = 该版本无 active chunk(与 owner 漂移
    是两回事,不可混为一谈)。
    """
    cursor.execute(
        f"SELECT DISTINCT owner_dept FROM {_kb_db()}.chunk_meta "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (doc_id, version_no),
    )
    return {(r[0] or "") for r in cursor.fetchall()}


def materialize_doc_allowed_depts(cursor, doc_id: str, *, apply: bool = True) -> Dict[str, object]:
    """把单篇文档的 approved 授权【物化】到 chunk_meta.allowed_depts 投影——decide 端点与
    allowed_depts_reconcile 对账共用的【唯一写实现】（与上面 resolve/gate/current 读原语配套）。

    单一注入点，绝不重复手写。流程：
      1. 解析 current version，并施加 2h PROCESSING 反抢锁（与 stage-3 loader 同约定）——current
         version 正在 stage-3 跑（PROCESSING < 2h）→ 返回 skipped_locked、本轮不动，交对账下轮重对。
         **必须有这层**：否则在 stage-3 装载窗口内抢改 index_status，会被 stage-3 写回 INDEXED
         覆盖、而 HA3 仍是旧 ACL → chunk_meta 已等于 authority 致对账判 unchanged 不再重推的【自愈
         失败漂移】（Step 5 审计；decide 旧实现缺此守卫）。
      2. 从 authority 重解析该 doc 应有 allowed_depts，并按【该 version】permission_level **版本限定**
         gate 到 dept_internal（对账旧实现版本无关 GROUP_CONCAT → 双活混级误撤合法授权；此处统一）。
      3. 与现存投影 diff；变更则 UPDATE chunk_meta.allowed_depts + 标脏 index_status='NOT_INDEXED'
         （= stage-3 outbox，下次 drain 重推 HA3）。

    **不提交事务、不写 HA3**（连接/事务由调用方掌控；HA3 由 stage-3 drain 携带）。
    apply=False：只算 want/have/status、不写（对账只读预览）。

    Returns: {"status": "skipped"|"skipped_locked"|"unchanged"|"materialized"|"retracted",
              "reset_chunks": int, "version_no": int|None}
    """
    import json as _json
    if not doc_id:
        return {"status": "skipped", "reset_chunks": 0, "version_no": None}
    # 1. current version + 2h PROCESSING 反抢锁（与 stage-3 loader / 对账同约定）
    cursor.execute(
        f"SELECT dm.current_version_no FROM {_kb_db()}.document_meta dm "
        f"LEFT JOIN {_kb_db()}.document_version dv "
        "  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no "
        "WHERE dm.doc_id=%s AND (dv.index_status IS NULL "
        f"  OR dv.index_status!='{DocVersionIndexStatus.PROCESSING}' OR dv.updated_at < NOW() - INTERVAL 2 HOUR)",
        (doc_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {"status": "skipped_locked", "reset_chunks": 0, "version_no": None}
    ver = int(row[0] or 1)

    # C3′/062：**同一事务快照**内读权威 epoch，供下方两处 UPDATE stamp。
    # projection_complete 不变量（Sam 2026-08-03 拍板）：只有在
    #   ①062 已 apply（列存在）② acl_mode 判定确定（非 fail-loose 退化）
    # 两条同时成立时才 stamp；否则 acl_epoch 保持原值（NULL/旧值）⇒ 下轮仍判 dirty。
    # ⚠️ 刻意"照常投影、只是不 stamp" —— 不改既有 materializer 的投影行为，只约束盖章。
    _auth_epoch = None
    try:
        cursor.execute(
            f"SELECT acl_epoch FROM {_kb_db()}.document_meta WHERE doc_id=%s", (doc_id,))
        _r = cursor.fetchone()
        _auth_epoch = int(_r[0]) if _r and _r[0] is not None else None
    except Exception as e:   # noqa: BLE001 — 062 未 apply ⇒ 不 stamp（降级不阻断）
        if "1054" not in str(e) and "Unknown column" not in str(e):
            raise
        _auth_epoch = None
    try:
        _mode_certain = _node_acl_columns_present(cursor, strict=True) or True
    except Exception:   # noqa: BLE001 — 列探测失败 ⇒ 模式不确定 ⇒ 绝不 stamp
        _mode_certain = False
    _stamp = ", acl_epoch=%s" if (_auth_epoch is not None and _mode_certain) else ""

    # 2a. node-ACL:模式决定投影形态(唯一注入点 acl_policy.project_doc_acl)。
    #     ⚠️ 本函数是 ACL 变更的【唯一写实现】(decide 端点 + reconcile 共用),node 文档
    #     必须在这里把检索投影轴的 owner 一并改成哨兵 —— 只改 allowed_depts 而留着真实
    #     owner,legacy owner 分支照样放行,等于没收权(codex 评审 BLOCKER-1 的同一根因)。
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, project_doc_acl
    if resolve_acl_modes([doc_id], cursor).get(doc_id) == ACL_MODE_NODE:
        _acl = resolve_doc_acl([doc_id], cursor).get(doc_id)
        _owner, want = project_doc_acl(
            ACL_MODE_NODE, "", (),
            getattr(_acl, "node_ids", ()) if _acl else (),
            getattr(_acl, "exact_node_ids", ()) if _acl else ())
        have = current_allowed_for_doc(cursor, doc_id, ver)
        # 集合相等而非"首项相等"：混合 owner（部分 chunk 已投影、部分还挂旧 owner）必须判漂移
        _owner_set = _current_owner_set_for_doc(cursor, doc_id, ver)
        if sorted(want) == have and _owner_set == {_owner}:
            # C3′/062 certify-only（codex PROPOSED CHANGE 3）：值已正确但 epoch 落后/为 NULL 时，
            # **只盖章、不动 index_status** —— 否则「值对但 acl_epoch IS NULL」的文档永远盖不上章、
            # 永久 dirty（这是 72c9e22 落码时的缺口：此处在 stamp 之前就 return 了）。
            # ⚠️ 认证判据必须**逐行全等**，不能用上面 `have` 的并集口径（并集会掩盖
            #    「一行 ["finance"]、一行 NULL」这种一半没投影的情况）。
            if _stamp and projection_rows_all_match(cursor, doc_id, ver, want, _owner):
                cursor.execute(
                    f"UPDATE {_kb_db()}.chunk_meta SET acl_epoch=%s "
                    "WHERE doc_id=%s AND version_no=%s AND is_active=1",
                    (_auth_epoch, doc_id, ver),
                )
                if cursor.rowcount:
                    return {"status": "certified", "reset_chunks": 0, "version_no": ver}
            return {"status": "unchanged", "reset_chunks": 0, "version_no": ver}
        status = "materialized" if want else "retracted"
        if not apply:
            return {"status": status, "reset_chunks": 0, "version_no": ver}
        aj = _json.dumps(want, ensure_ascii=False) if want else None
        cursor.execute(
            f"UPDATE {_kb_db()}.chunk_meta SET allowed_depts=%s, owner_dept=%s"
            f"{_stamp}, index_status='{ChunkIndexStatus.NOT_INDEXED}' "
            "WHERE doc_id=%s AND version_no=%s AND is_active=1",
            ((aj, _owner, _auth_epoch, doc_id, ver) if _stamp else (aj, _owner, doc_id, ver)),
        )
        return {"status": status, "reset_chunks": cursor.rowcount, "version_no": ver}

    # 2. legacy:authority → 该版本 permission_level 版本限定 gate 到 dept_internal
    raw_want = resolve_allowed_depts_one(doc_id, cursor)
    cursor.execute(
        f"SELECT GROUP_CONCAT(DISTINCT permission_level) FROM {_kb_db()}.chunk_meta "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        (doc_id, ver),
    )
    prow = cursor.fetchone()
    gated = gate_by_permission(
        {doc_id: raw_want}, {doc_id: (prow[0] if prow else None)},
    ).get(doc_id, [])
    # 阶段 B（codex 评审 BLOCKER，2026-08-01）：legacy 分支同样走 project_doc_acl 投影
    # **owner 一并比较与写回**——此前只写 allowed_depts，node→legacy 迁移后 chunk 的
    # owner 永远留着哨兵，legacy owner 过滤召不回文档（access_grants.py 旧 :437-442）。
    # 对纯 legacy 存量这是无操作：chunk owner 本就是真实值 → owner 比较恒等，
    # unchanged 路径与改动前逐字节一致。
    cursor.execute(
        f"SELECT owner_dept FROM {_kb_db()}.document_meta WHERE doc_id=%s", (doc_id,))
    orow = cursor.fetchone()
    from opensearch_pipeline.acl_policy import ACL_MODE_LEGACY
    _owner, want = project_doc_acl(ACL_MODE_LEGACY, (orow[0] if orow else "") or "", gated)
    # 3. diff vs 现存投影（owner 与集合都比：owner 漂移也必须触发重投影）
    have = current_allowed_for_doc(cursor, doc_id, ver)
    _owner_set = _current_owner_set_for_doc(cursor, doc_id, ver)
    if sorted(want) == have and _owner_set == {_owner}:
        # C3′/062 certify-only（codex PROPOSED CHANGE 3）：值已正确但 epoch 落后/为 NULL 时，
        # **只盖章、不动 index_status** —— 否则「值对但 acl_epoch IS NULL」的文档永远盖不上章、
        # 永久 dirty（这是 72c9e22 落码时的缺口：此处在 stamp 之前就 return 了）。
        # ⚠️ 认证判据必须**逐行全等**，不能用上面 `have` 的并集口径（并集会掩盖
        #    「一行 ["finance"]、一行 NULL」这种一半没投影的情况）。
        if _stamp and projection_rows_all_match(cursor, doc_id, ver, want, _owner):
            cursor.execute(
                f"UPDATE {_kb_db()}.chunk_meta SET acl_epoch=%s "
                "WHERE doc_id=%s AND version_no=%s AND is_active=1",
                (_auth_epoch, doc_id, ver),
            )
            if cursor.rowcount:
                return {"status": "certified", "reset_chunks": 0, "version_no": ver}
        return {"status": "unchanged", "reset_chunks": 0, "version_no": ver}
    status = "materialized" if want else "retracted"
    if not apply:
        return {"status": status, "reset_chunks": 0, "version_no": ver}
    aj = _json.dumps(want, ensure_ascii=False) if want else None
    cursor.execute(
        f"UPDATE {_kb_db()}.chunk_meta SET allowed_depts=%s, owner_dept=%s"
        f"{_stamp}, index_status='{ChunkIndexStatus.NOT_INDEXED}' "
        "WHERE doc_id=%s AND version_no=%s AND is_active=1",
        ((aj, _owner, _auth_epoch, doc_id, ver) if _stamp else (aj, _owner, doc_id, ver)),
    )
    return {"status": status, "reset_chunks": cursor.rowcount, "version_no": ver}


# ── 投影 outbox：decide 同事务入队 + stage-3 幂等 drain（与全扫 reconcile 互补）──
def record_acl_projection_invalidation(cursor, doc_id: str, reason: str = "") -> None:
    """**ACL 投影失效的唯一登记入口**（C3′ / schema 062，2026-08-03，codex 五轮共识）。

    同事务做两件事：① `document_meta.acl_epoch += 1`（投影失效代次）② outbox 入队。
    任何改变本文档【有效投影输入】的写都必须调用它 —— 输入 = `project_doc_acl` 的产出
    `(owner_dept, allowed_depts)` 所依赖的一切：`acl_mode`、legacy `owner_dept`、
    node grant 生效集（授/撤/scope/dept）、access_request 的 **approved 态与 requester_depts**、
    以及**有效的每版本 `permission_level`**（它是 allowed_depts 的 gate 输入 —— 由 public 变
    dept_internal 会让期望的 allowed_depts 从空变成 approved groups）。

    ⚠️ **`permission_level` 是失效【输入】但不在 epoch 的认证【输出】范围**：epoch 相等
    不证明 dm/chunk 的 permission 副本已同步（那归 C9）。两者别混。
    ⚠️ **pending 态的 access_request INSERT 不调用本函数** —— 尚未改变有效授权。
    ⚠️ **绝不受 `RAG_ALLOWED_DEPTS_ACL` 门控**（Sam 2026-08-03 拍板）：flag 关闭期间发生的
      权威变更若不 bump，水位永久丢失，开 flag 后这批文档永远判 unchanged = 原样复现 C3。
    ⚠️ **不提交事务**（连接/事务由调用方掌控），与 `enqueue_acl_projection` 同约定。

    062 未 apply 的环境：`acl_epoch` 列不存在 ⇒ 1054，此时**只入队不 bump**（降级但不阻断，
    与 049/050 同型；apply 后自动恢复）。
    """
    if not doc_id:
        return
    try:
        cursor.execute(
            f"UPDATE {_kb_db()}.document_meta SET acl_epoch = acl_epoch + 1 WHERE doc_id=%s",
            (doc_id,),
        )
    except Exception as e:   # noqa: BLE001 — 仅 1054（062 未 apply）降级；其余照抛
        if "1054" not in str(e) and "Unknown column" not in str(e):
            raise
        logger.warning("acl_epoch bump 跳过（schema/062 未 apply）: doc=%s", doc_id)
    enqueue_acl_projection(cursor, doc_id, reason=reason)


def enqueue_acl_projection(cursor, doc_id: str, reason: str = "") -> None:
    """把一篇文档持久入队到 allowed_depts 投影 outbox（kb_acl_projection_outbox）。

    decide 端点在【改 kb_access_request.status 的同一事务】内调用——权威变更与投影意图原子提交：
    enqueue 失败 → 整笔回滚（绝不出现权威已改而无 outbox 行的撕裂；与「内联 materialize best-effort」
    不同，本入队是【持久保证】，刻意不吞异常）。一行一 doc（UNIQUE doc_id）：重复入队走 ON DUPLICATE
    复活（done_at=NULL, attempts=0），不留历史。**不提交事务**（连接/事务由调用方掌控）。
    """
    if not doc_id:
        return
    # 批次8（ultra schema/009:34）：复活时 generation+1——drain 的 done/attempts 标记按
    # (id, generation) CAS，mid-drain 落库的撤销（复活了同一行）不再被旧一轮的 done 标记
    # 擦掉。049 未 apply（1054 Unknown column）时回退旧语句（🔒 apply user-gated，
    # 代码可先部署；MySQL 语句级错误不终止调用方事务，同事务重试合法）。
    try:
        cursor.execute(
            f"INSERT INTO {_kb_db()}.kb_acl_projection_outbox (doc_id, reason) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE done_at=NULL, attempts=0, last_error=NULL, "
            "generation=generation+1, reason=VALUES(reason), updated_at=NOW()",
            (doc_id, (reason or "")[:64]),
        )
    except Exception as e:   # noqa: BLE001 — 仅 1054（列未 apply）回退；其余照抛（持久保证不吞）
        if "1054" not in str(e) and "Unknown column" not in str(e):
            raise
        cursor.execute(
            f"INSERT INTO {_kb_db()}.kb_acl_projection_outbox (doc_id, reason) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE done_at=NULL, attempts=0, last_error=NULL, "
            "reason=VALUES(reason), updated_at=NOW()",
            (doc_id, (reason or "")[:64]),
        )


def drain_acl_projection_outbox(commit: bool = True, limit: int = 200) -> dict:
    """Drain 投影 outbox：逐文档幂等 materialize（标脏 chunk_meta + index_status='NOT_INDEXED'），
    成功/unchanged → 标 done_at；skipped_locked/失败 → attempts++ 留待下轮。**绝不抛**（失败进 errors）。

    与 allowed_depts_reconcile 互补：outbox 是 decide 受影响 doc 的【定向必达】重试，reconcile 是
    authority↔投影漂移的【全扫兜底】。stage-3 pre-drain 调用（HA3 重推由其后的 drain 循环携带）。
    flag 关 → 直接返回 skipped（投影路径全程惰性）。commit=False 为只读预览（不写 outbox/不标脏）。
    """
    from opensearch_pipeline.config import get_config

    result = {"processed": 0, "done": 0, "locked": 0, "failed": 0, "skipped": False, "errors": []}
    if not get_config().rag.allowed_depts_acl:
        result["skipped"] = True
        return result

    from opensearch_pipeline.db import _get_db_conn

    try:
        conn = _get_db_conn()
    except Exception as e:   # noqa: BLE001
        result["errors"].append(f"DB connect failed: {e}")
        return result
    # 批次8（ultra schema/009:34）：SELECT 带走 generation，done/attempts 标记按
    # (id, generation) CAS——drain 物化与标记之间落库的撤销会复活同一行并 generation+1，
    # CAS 落空即视为「行已被更新代次接管」：不标 done（撤销意图保留，下轮按新代次重物化）。
    # 049 未 apply（1054）→ 回退旧无代次语义（🔒 apply user-gated，代码可先部署）。
    _gen_col = True
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"SELECT id, doc_id, generation FROM {_kb_db()}.kb_acl_projection_outbox "
                    "WHERE done_at IS NULL ORDER BY enqueued_at LIMIT %s",
                    (int(limit),),
                )
            except Exception as _ge:   # noqa: BLE001 — 仅列缺失回退
                if "1054" not in str(_ge) and "Unknown column" not in str(_ge):
                    raise
                _gen_col = False
                cur.execute(
                    f"SELECT id, doc_id, 0 FROM {_kb_db()}.kb_acl_projection_outbox "
                    "WHERE done_at IS NULL ORDER BY enqueued_at LIMIT %s",
                    (int(limit),),
                )
            rows = [(r[0], r[1], r[2]) for r in cur.fetchall() if r and r[1]]

        def _mark_where(row_id, gen):
            if _gen_col:
                return " WHERE id=%s AND generation=%s", (row_id, gen)
            return " WHERE id=%s", (row_id,)

        for row_id, doc_id, gen in rows:
            result["processed"] += 1
            try:
                with conn.cursor() as cur:
                    outcome = materialize_doc_allowed_depts(cur, doc_id, apply=commit)
                    if not commit:
                        conn.rollback()          # 预览：不改 outbox/不落标脏
                        continue
                    _w, _wp = _mark_where(row_id, gen)
                    if outcome["status"] == "skipped_locked":
                        # current version 正在 stage-3 跑 → 本轮跳过、下轮再 drain（留 done_at=NULL）
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_acl_projection_outbox "
                            "SET attempts=attempts+1, last_error='skipped_locked', updated_at=NOW()"
                            + _w, _wp,
                        )
                        result["locked"] += 1
                    else:
                        # unchanged / certified / materialized / retracted / skipped → 投影意图已落实 → 标 done
                        # （certified = 值本就正确、只补盖 epoch 章；意图同样已落实，刻意归此支）
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_acl_projection_outbox "
                            "SET done_at=NOW(), last_error=NULL, updated_at=NOW()" + _w, _wp,
                        )
                        if _gen_col and cur.rowcount == 0:
                            # mid-drain 复活（generation 已 +1）：不标 done，撤销意图保留待下轮
                            result["raced"] = result.get("raced", 0) + 1
                        else:
                            result["done"] += 1
                conn.commit()
            except Exception as e:   # noqa: BLE001 — 单文档失败不抛、记 errors、attempts++ 待重试
                result["errors"].append(f"{doc_id}: {e}")
                result["failed"] += 1
                try:
                    conn.rollback()
                    _w, _wp = _mark_where(row_id, gen)
                    with conn.cursor() as cur:
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_acl_projection_outbox "
                            "SET attempts=attempts+1, last_error=%s, updated_at=NOW()" + _w,
                            (str(e)[:512],) + _wp,
                        )
                    conn.commit()
                except Exception:   # noqa: BLE001 — 连记错误都失败 → 下轮 reconcile 兜底
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    finally:
        conn.close()
    return result


# ── 阶段 B：doc-meta 投影 outbox（schema/061 kb_doc_meta_projection_outbox）─────
# 为什么必须持久意图（codex 阶段 B blocker）：stage-3 的 loader 在 dag.run()（锁获取）
# **之前**把 title/category/chunk_text 读进内存（dataworks_orchestrator.py:454-605→:610），
# 推完又按 chunk_id 无条件回写 INDEXED（pipeline_nodes.py:8271-8288）——窗口内直接落的
# NOT_INDEXED 标记会被本轮吞掉。outbox 行 + generation CAS 让被吞的编辑下轮重投影。
def enqueue_meta_projection(cursor, doc_id: str, reason: str = "") -> None:
    """doc-meta 端点【同事务】入队（仅 title/category 变更需要；owner/可见集走 ACL outbox）。
    刻意不吞异常：061 未 apply 时抛 1146 → 端点整笔回滚——改标题/分类的能力以 061 为前置，
    半提交（元数据改了、投影意图丢了）比诚实失败更糟。**不提交事务**。"""
    if not doc_id:
        return
    cursor.execute(
        f"INSERT INTO {_kb_db()}.kb_doc_meta_projection_outbox (doc_id, reason) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE processed_at=NULL, attempts=0, last_error=NULL, "
        "generation=generation+1, reason=VALUES(reason), updated_at=NOW()",
        (doc_id, (reason or "")[:64]),
    )


def pre_drain_meta_projection(limit: int = 200) -> dict:
    """stage-3 在 loader **之前**调用：对每条未处理行同步 chunk_meta 的 category 列
    （stage-3 载荷的 category 取自 chunk_meta 而非 JOIN document_meta —— 只改 document_meta
    分类永远推不进 HA3）+ 把 active chunk 标 NOT_INDEXED（title 由 loader 的 JOIN 现读）。
    返回 {"claims": [(id, doc_id, generation)], ...}——调用方在本轮 DAG **成功推送后**
    把 claims 交给 complete_meta_projection 按 CAS 收尾；失败/中断则不收尾，下轮重投影
    （幂等：category 同步与标脏都可重放）。表不存在（061 未 apply）→ skipped。"""
    out = {"claims": [], "marked": 0, "skipped": False, "errors": []}
    from opensearch_pipeline.db import _get_db_conn
    try:
        conn = _get_db_conn()
    except Exception as e:   # noqa: BLE001
        out["errors"].append(f"DB connect failed: {e}")
        return out
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"SELECT id, doc_id, generation FROM {_kb_db()}.kb_doc_meta_projection_outbox "
                    "WHERE processed_at IS NULL ORDER BY enqueued_at ASC LIMIT %s", (limit,))
                rows = cur.fetchall() or []
            except Exception:   # noqa: BLE001 — 1146 表不存在 = 061 未 apply
                out["skipped"] = True
                return out
            # 🔴 claims/marked 必须**在 commit 成功之后**才对外可见（2026-08-03）。
            # 原实现在循环里直接写 out["claims"]，而 conn.commit() 在循环**外**：整批任一
            # 环节让 commit 失败 ⇒ 外层 except 捕获、函数**照常返回非空 claims**，而 chunk 的
            # NOT_INDEXED 标脏已随事务回滚。调用方（dataworks_orchestrator →
            # complete_meta_projection）按 claims 做 (id,generation) CAS 标 processed_at
            # ⇒ **那次改标题/分类的编辑永久丢失，且账面显示已处理**。
            # 先攒本地、commit 成功再对外——不改事务粒度（锁释放时机/重试粒度保持现状）。
            _pending_claims = []
            _pending_marked = 0
            for rid, doc_id, gen in rows:
                try:
                    cur.execute(
                        f"UPDATE {_kb_db()}.chunk_meta cm "
                        f"JOIN {_kb_db()}.document_meta dm ON dm.doc_id = cm.doc_id "
                        "SET cm.category_l1 = dm.category_l1, cm.category_l2 = dm.category_l2, "
                        f"    cm.index_status = '{ChunkIndexStatus.NOT_INDEXED}' "
                        "WHERE cm.doc_id = %s AND cm.is_active = 1",
                        (doc_id,))
                    _pending_marked += cur.rowcount
                    cur.execute(
                        f"UPDATE {_kb_db()}.kb_doc_meta_projection_outbox "
                        "SET attempts = attempts + 1, updated_at = NOW() WHERE id = %s", (rid,))
                    _pending_claims.append((int(rid), doc_id, int(gen or 0)))
                except Exception as e:   # noqa: BLE001 — 单行失败不拖垮整轮
                    out["errors"].append(f"{doc_id}: {e}")
                    try:
                        cur.execute(
                            f"UPDATE {_kb_db()}.kb_doc_meta_projection_outbox "
                            "SET attempts = attempts + 1, last_error = %s, updated_at = NOW() "
                            "WHERE id = %s", (str(e)[:512], rid))
                    except Exception:   # noqa: BLE001
                        pass
        conn.commit()
        # commit 成功才认账：失败时 claims 保持空 ⇒ 调用方不收尾 ⇒ 下轮重投影
        # （幂等：category 同步与标脏都可重放），正是本函数 docstring 承诺的语义。
        out["claims"] = _pending_claims
        out["marked"] = _pending_marked
    except Exception as e:   # noqa: BLE001
        try:
            conn.rollback()
        except Exception:   # noqa: BLE001
            pass
        out["errors"].append(str(e))
    finally:
        conn.close()
    return out


def complete_meta_projection(claims) -> dict:
    """本轮 DAG 成功推送后按 (id, generation) CAS 标 processed。CAS 落空 = 窗口中途该行被
    doc-meta 再次入队（generation 已 +1）——行保持待处理，下轮按新代次重投影（丢更新关死）。
    ⚠️ 只允许在**成功**路径调用（DAG 失败/中断不收尾，049 同款前提条款）。"""
    out = {"completed": 0, "superseded": 0, "errors": []}
    if not claims:
        return out
    from opensearch_pipeline.db import _get_db_conn
    try:
        conn = _get_db_conn()
    except Exception as e:   # noqa: BLE001
        out["errors"].append(f"DB connect failed: {e}")
        return out
    try:
        with conn.cursor() as cur:
            for rid, _doc_id, gen in claims:
                try:
                    cur.execute(
                        f"UPDATE {_kb_db()}.kb_doc_meta_projection_outbox "
                        "SET processed_at = NOW(), last_error = NULL, updated_at = NOW() "
                        "WHERE id = %s AND generation = %s AND processed_at IS NULL",
                        (rid, gen))
                    if cur.rowcount:
                        out["completed"] += 1
                    else:
                        out["superseded"] += 1
                except Exception as e:   # noqa: BLE001
                    out["errors"].append(f"#{rid}: {e}")
        conn.commit()
    except Exception as e:   # noqa: BLE001
        out["errors"].append(str(e))
    finally:
        conn.close()
    return out
