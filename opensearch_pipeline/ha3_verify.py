# -*- coding: utf-8 -*-
"""HA3 **serving 可检索性**探针（self-query）—— 不是物理存在性判据。

Problem (cost a false-FAIL on 2026-06-20): the zero-vector range-scan
`client.query(vector=[0]*1024, filter="id>=lo AND id<hi")` is **non-deterministic
and incomplete** (G30). Right after a realtime push it can return 0 rows even though
every chunk is indexed and serving — so using it to confirm "is this doc in HA3?"
produces false negatives (and could wrongly trigger a rollback).

Method = **per-chunk self-query**: take each chunk's own text, run it through the real
serving path (`retrieve_and_enrich`, which uses a query vector + hybrid + the ACL
filter), and confirm the chunk returns *itself*. 命中 ⇒ 该 chunk 在真实服务路径下可被
自身内容召回（这正是本模块要证明的东西）。

⚠️ 定位更正（2026-07-22）：本模块此前自称 "Authoritative presence verification"，
**该说法已作废**。self-query 走 ANN + 融合 + ACL 过滤，未命中的原因可能是排序、阈值、
权限或 ANN 行为，**不足以否定物理存在**。物理存在性判定唯官方 `/vector-service/fetch`
（`clients.ha3_fetch_by_pks`）为准；发现未知 PK（orphan 方向）唯倒排枚举
（`clients.ha3_enumerate_bucket`）为准。本模块只回答「serving 能否召回它」。

Pure/injectable: `retrieve_fn` and the RDS cursor source are passed in, so this is
unit-testable without network and reusable by spot_checker / rebuild / 上线 verifies.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _active_chunks(conn, doc_id: str, kn: str = "fuling_knowledge") -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, chunk_id, chunk_text, chunk_type, owner_dept "
            f"FROM {kn}.chunk_meta WHERE doc_id=%s AND is_active=1 ORDER BY id",
            (doc_id,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(r if isinstance(r, dict) else
                   {"id": r[0], "chunk_id": r[1], "chunk_text": r[2], "chunk_type": r[3], "owner_dept": r[4]})
    return out


def _flatten(msg: str, limit: int = 200) -> str:
    """错误串压平成单行并截断（CR/LF 都要处理）；完整异常只进 logger。"""
    return msg.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")[:limit]


def _retrieve_fn_takes_acl_ctx(fn: Callable[..., Any]) -> bool:
    """retrieve_fn 能否接 `acl_ctx=`（显式形参或 **kwargs）。取不到签名 ⇒ 乐观直传。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD or p.name == "acl_ctx"
               for p in params.values())


def _node_acl_context(conn, doc_id: str):
    """按【权威 `acl_mode`】决定查询身份：node 文档 → 以其授权节点构造 `AclContext`。

    ⚠️ **绝不能用检索投影里的哨兵反推 mode**（2026-07-31 codex 评审的 BLOCKER）。
    初版实现只在 `chunk_meta.owner_dept` 已是哨兵时才查权威，于是漏掉最危险的一个窗口：

        RDS 权威已切 node，但检索投影**尚未收敛**（chunk 仍带旧的真实 owner）

    此时哨兵还没写下 ⇒ 探针把旧 owner 当组码去查 ⇒ 文档经 legacy owner 分支被找到 ⇒
    **报 ok=True**；而真正被节点授权的用户走 `d:`/`dx:` 根本召不回它。**假绿比误报更坏** ——
    本模块的全部价值就是不产生这种结论。故查询身份一律取自权威表，代价是 legacy 文档也多
    一次权威查询（本模块是运维探针、非热路径，这个代价可以接受）。

    返回该文档的权威 `DocAcl`。**判不出 mode 一律抛** `NodeAclAuthorityUnavailable`
    （缺 `document_meta` 行 / 未知 `acl_mode` / capability 探测失败 / 授权表读失败）——
    ⚠️ 必须传 `strict=True`：宽松路径会把 capability 异常改写成「未 apply ⇒ 全 legacy」，
    于是一篇 node 文档被当成 legacy、用陈旧 owner 查、经 legacy owner 分支找到，
    **报 ok=True 且 errors 为空**（codex 2026-07-31 实测复现）。`None` 一个值不能同时
    表示「合法 legacy」和「mode 判不出」。

    ⚠️ `conn` 须是 **tuple 游标**连接(serving 的 `db._get_db_conn()` 即是)。用
    `prod_access.get_prod_readonly_conn()` 核查时必须传 `dict_cursor=False` ——
    它默认 `DictCursor`,`resolve_doc_acl` 里的 `row[1]` 会 KeyError。
    """
    from opensearch_pipeline.access_grants import NodeAclAuthorityUnavailable, resolve_doc_acl
    with conn.cursor() as cur:
        acl = resolve_doc_acl([doc_id], cur, strict=True).get(doc_id)
    if acl is None:
        # strict 下缺行 / 未知 mode 都不返回该 doc ⇒ mode 判不出，绝不默认成 legacy
        raise NodeAclAuthorityUnavailable(
            f"doc={doc_id} 的权威 acl_mode 不可判（document_meta 缺行或 acl_mode 未知）")
    return acl


def _legacy_audience(acl) -> List[str]:
    """legacy 文档的**权威可见受众** → 组码列表（自查身份就用它）。

    两个来源缺一不可（2026-07-31 codex 实测，只用 owner 会产生确定性假阴性）：
      · `owner_dept` 反查出的组码 —— ⚠️ **owner 值域 ≠ 组码值域**：`production_mold` 是
        合法 owner 但不是组码，能读它的是 `production`/`marketing`。反查走
        `retriever.groups_that_can_read_owner`（taxonomy 的单一来源，绝不在此复制）。
      · `DocAcl.groups` —— approved 的跨部门授权组码。owner 侧已撤、只剩跨部门 grant 的
        文档,只靠 owner 会被判缺,但它其实经 `allowed_depts` 分支可正常检索。
    """
    from opensearch_pipeline.retriever import groups_that_can_read_owner
    owner = (getattr(acl, "owner_dept", "") or "").strip()
    audience = set(groups_that_can_read_owner(owner))
    audience |= {g for g in (getattr(acl, "groups", ()) or ()) if g}
    return sorted(audience)


def _effective_identity(acl_ctx, user_dept):
    """→ (送去检索的 user_dept, 用于权威判定的 AclContext)，**两者组码口径必须同一份**。

    ⚠️ 组码要先过 `retriever._normalize_acl_groups`（净化 + 白名单 + 去重）再用。真实检索
    走的就是它，而权威判定若拿**原始**组码，两侧会对同一身份给出相反结论
    （codex 2026-07-31 实测）:
        `[" hr "]`  → 过滤器净化后放行；权威判定用原始值 ⇒ False   ← 假阴性
        `["evil"]`  → 过滤器白名单丢弃 ⇒ 仅 public；权威判定 ⇒ True ← 配合陈旧 public 投影即假绿
    """
    from opensearch_pipeline.retriever import _normalize_acl_groups
    if acl_ctx is not None:
        import dataclasses
        groups = tuple(_normalize_acl_groups(list(getattr(acl_ctx, "groups", ()) or [])))
        # 只换 groups，节点字段（祖先链/直属/通道可信/全库读）原样保留
        return list(groups), dataclasses.replace(acl_ctx, groups=groups)
    from opensearch_pipeline.acl_policy import AclContext
    groups = tuple(_normalize_acl_groups(list(user_dept or [])))
    return (list(groups) or None), AclContext(groups=groups)


def _authorized_by_authority(doc_id: str, acl, ctx) -> bool:
    """本次查询身份是否被**权威**授权读这篇文档（`acl_policy.can_read_doc` 的真值表）。

    这是"探针有没有验到东西"的前置条件：身份无权时，文档**被召回**恰恰说明检索投影比权威
    更宽，属于发现而不是通过。故它必须并进 `ok`（codex 2026-07-31）。

    权威读不到（`acl is None`）⇒ False（此时 `acl_mode` 也是 `unknown`，结论本就不可信）。
    ⚠️ 判定本身抛异常（flag 姿态非法、config 读失败）**向上抛** —— 由调用方记进 `errors`。
    在这里吞成 False 会把"探针故障"伪装成"身份无权"，正是 A3 要消灭的那类混淆。
    """
    if acl is None:
        return False
    from opensearch_pipeline.acl_policy import can_read_doc
    from opensearch_pipeline.retriever import _node_acl_flags
    grant, enforce = _node_acl_flags()
    return bool(can_read_doc(ctx, acl, grant_enabled=grant, enforce_enabled=enforce))


def _ctx_from_node_acl(doc_id: str, acl):
    """node 模式 `DocAcl` → 以其授权节点为读身份的 `AclContext`。"""
    from opensearch_pipeline.acl_policy import AclContext
    if not acl.node_ids and not acl.exact_node_ids:
        # 零授权节点 = 真的没人能看见(不是探针缺陷)。照常返回空身份让它判缺，但要留痕，
        # 否则运维会把「已被撤光授权」误读成「索引丢了」。
        logger.warning("doc=%s 是 node 模式但无任何有效节点授权 ⇒ 无人可见，self-query 必然判缺",
                       doc_id)
    return AclContext(
        groups=(),                                   # node 模式绝不带组码（模式互斥）
        ancestor_dept_ids=tuple(acl.node_ids),       # 子树授权 → 匹配 d:<id>
        direct_dept_ids=tuple(acl.exact_node_ids),   # 仅本节点授权 → 匹配 dx:<id>
        node_channel_ok=True,
    )


def verify_chunks_present(
    doc_id: str,
    *,
    conn,
    retrieve_fn: Callable[..., List[Dict[str, Any]]],
    kn: str = "fuling_knowledge",
    snippet: int = 160,
    top_k: int = 5,
    owner_dept_override: Optional[List[str]] = None,
    acl_ctx: Any = None,
) -> Dict[str, Any]:
    """Confirm every active chunk of `doc_id` is searchable in HA3 via self-query.

    Args:
      conn        : a DB connection (read-only is fine) yielding chunk_meta rows.
      retrieve_fn : callable(query:str, *, top_k:int, user_dept) -> list[result dict].
                    In prod pass `retriever.retrieve_and_enrich`. Each result should
                    carry "id" (str of chunk_meta.id) and/or "doc_id"+"chunk_id".
      owner_dept_override : legacy 模式下以哪些**组码**身份查询。默认身份 = **权威受众**
                    （`document_meta.owner_dept` 反查出的组码 ∪ approved 跨部门授权组码），
                    绝不取 chunk 投影里的 owner。入参与默认值都会过净化+白名单。
                    ⚠️ 权威为 node 模式时传它会**直接报错** —— 组码表达不了 `d:`/`dx:`
                    节点语义。与 `acl_ctx` **互斥**（同时传直接报错）。
      acl_ctx     : 显式读身份;查询时会同时按真实 serving 形态传 `user_dept=ctx.groups`。
                    省略且权威为 node ⇒ 自动按其节点授权构造。
                    ⚠️ 传了它**也不会**跳过文档 mode 的权威解析 —— 那是两件独立的事。

    Returns: {expected_ids, present_ids, missing_ids, present, total, served_ids,
              foreign_ids（本次自查中浮现的**他文档** id —— 跨文档/ACL 泄漏信号）,
              acl_mode, conclusive, identity_authorized, errors, error_count, ok, method}。

    ⚠️ **总判据只能读 `ok`**。它把四件事合起来了：
        全部命中 ∧ 无探针故障 ∧ mode 确定 ∧ **查询身份被权威授权**。
    只看 `missing_ids == []` 会把三类不可信结论当成通过：权威读不到、投影未收敛、以及
    **权威说不可读但陈旧投影仍放行**（那是"投影过宽"的发现，不是一次通过的验证）。

    ⚠️ **定位：existential positive probe，不是全受众 ACL parity 审计**。它只证明
    「**某一个**被权威授权的身份能召回每个 chunk」。故它查不出：多个 approved grant 里
    只有部分投影成功、owner 通道坏了但 grant 通道仍命中、以及"合法受众之外还有谁被过宽
    投影放行"。后者要的是逐受众的 parity 审计，不在本模块职责内。
    """
    chunks = _active_chunks(conn, doc_id, kn)
    expected = sorted(int(c["id"]) for c in chunks)

    # node-ACL(2026-07-31):**文档 mode 的解析与查询身份的选择彻底解耦** ——
    # mode 永远从权威解析（与是否传了 override / acl_ctx 无关），因为它是一个独立事实；
    # 用投影哨兵反推、或因为"运维显式指定了身份"就跳过解析，都会制造假绿
    # （codex 2026-07-31 两次实测复现）。
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, NODE_OWNER_SENTINEL
    sentinel_seen = any(c.get("owner_dept") == NODE_OWNER_SENTINEL for c in chunks)
    if owner_dept_override is not None and acl_ctx is not None:
        # 两者都在描述"以谁的身份查"，同时传语义歧义 ⇒ 互斥拒绝（此前 override 被静默忽略）。
        raise ValueError("owner_dept_override 与 acl_ctx 互斥 —— 二者都在指定查询身份，请只传一个。")
    errors: List[Dict[str, Any]] = []
    authority_mode = None
    authority_acl = None
    authority_audience: List[str] = []
    try:
        authority_acl = _node_acl_context(conn, doc_id)
        _acl = authority_acl
        authority_mode = (getattr(_acl, "mode", "") or "").strip().lower()
        authority_audience = _legacy_audience(_acl)
        if authority_mode == ACL_MODE_NODE and acl_ctx is None and owner_dept_override is None:
            acl_ctx = _ctx_from_node_acl(doc_id, _acl)
    except Exception as e:   # noqa: BLE001 — 权威判不了 mode ⇒ 本轮结论不可信
        logger.warning("doc=%s 权威 acl_mode 解析失败: %s", doc_id, e, exc_info=True)
        errors.append({
            "chunk_id": None,
            "error_type": type(e).__name__,
            "message": _flatten(str(e)),
        })
    if authority_mode == ACL_MODE_NODE and acl_ctx is None and owner_dept_override is not None:
        # 调用方用法错误 ⇒ **直接抛**（与下方 retrieve_fn 不收 acl_ctx 同处置），不降级成
        # 一条 error 后继续跑：override 是**组码**身份，表达不了 d:/dx: 节点语义。拿它去
        # "验证"一篇 node 文档，顶多验到"陈旧投影还能被旧 owner 找到"，与本模块要回答的
        # 「被授权的人能不能搜到它」无关 —— 而它恰好会**报绿**（codex 2026-07-31 实测）。
        raise ValueError(
            f"doc={doc_id} 权威为 node 模式,owner_dept_override 无法表达节点身份 —— "
            "请改传 acl_ctx,或去掉 override 让探针按权威自动构造。")
    if acl_ctx is not None and not _retrieve_fn_takes_acl_ctx(retrieve_fn):
        # 静默降级会直接产出「node 文档整篇缺失」的假结论 —— 那正是本次修复要消灭的误报。
        raise TypeError(
            "retrieve_fn 不接受 acl_ctx —— node 模式文档必须带节点身份查询，"
            "否则在场文档会被误报为缺失。请传 retriever.retrieve_and_enrich 或接受 **kwargs 的包装。")
    has_acl_ctx = acl_ctx is not None      # 是否该给 retrieve_fn 传 ctx（默认 legacy 路径不传）
    if authority_mode is None:
        mode = "unknown"            # 权威判不了 mode（errors 里有原因）
    elif authority_mode == ACL_MODE_NODE:
        mode = "node"
    elif sentinel_seen:
        # 权威说 legacy，投影里却是哨兵 —— 反向未收敛（刚回滚 node→legacy?）。
        # 不当作 node 处理（权威优先），但必须留痕：此时判缺同样不可直接当索引缺失处置。
        mode = "legacy?sentinel"
        logger.warning("doc=%s 权威为 legacy 但分块带 node 哨兵 owner ⇒ 投影未收敛，结论需人工判读",
                       doc_id)
    else:
        mode = "legacy"
    # 只有确定态才允许总绿灯。自动化消费者通常只读 `ok` —— 把"结论不可信"藏在 acl_mode 里
    # 等于没说（codex 2026-07-31）。
    conclusive = mode in ("legacy", "node")

    # ── 查询身份（**整篇一个**，一律取自权威，绝不逐块从投影取）─────────────────
    # ⚠️ 第三次栽在同一个根因上（codex 2026-07-31）：legacy 分支原本逐块用
    # `chunk_meta.owner_dept`。owner 变更后投影未收敛时（权威 finance / 投影仍 hr），
    # 探针会以**旧 owner** 身份去查、把文档找出来报 ok=True —— 而真正的 finance 用户
    # 拿到的过滤器是 `owner_dept="finance"`，根本召不回。投影是被验对象，不是身份来源。
    if acl_ctx is not None:
        # 与真实 serving 同形：API 同时传 user_dept 与 acl_ctx（api.py:989）。
        # 自动构造的 node ctx groups 为空 ⇒ 等价于 None，node seam 不受影响。
        ud = list(getattr(acl_ctx, "groups", ()) or [])
    elif owner_dept_override is not None:
        ud = owner_dept_override
    else:
        ud = authority_audience or None
    # 组码统一过净化+白名单，检索与权威判定共用同一份口径（见 `_effective_identity`）。
    # ⚠️ 规范化后的 `query_ctx` **必须同时送进检索**：真实 retriever 的事后 `can_read_doc`
    # 读的就是传入 ctx 的 groups，只在判定侧规范化会让两侧再次分叉（codex 2026-07-31 实测：
    # ctx.groups=(" hr ",) + 跨部门 grant ⇒ 判定 True 但检索侧仍拿原始值 ⇒ 假阴性）。
    ud, query_ctx = _effective_identity(acl_ctx, ud)
    extra = {"acl_ctx": query_ctx} if has_acl_ctx else {}
    # ⚠️ 「本次查询身份是否被**权威**授权读这篇文档」—— 必须按真值表算，不能只看"身份对象非空"
    # （codex 2026-07-31 实测三条反例）。核心场景是**投影漂移**：权威说不可读，而陈旧投影
    # 仍把文档放出来。此时"找到了"不是通过，是**投影过宽**的发现:
    #     权威 dept_internal 无 owner 无 grants + 陈旧 public 投影 → 曾经 ok=True
    #     权威 restricted            + 陈旧 dept_internal 投影      → 曾经 ok=True
    #     权威 node 且零节点授权     + 陈旧 public 投影             → 曾经 ok=True
    # 最后一条还与 `_ctx_from_node_acl` 自己"零授权=无人可见"的告警自相矛盾。
    decided = True
    try:
        identity_authorized = _authorized_by_authority(doc_id, authority_acl, query_ctx)
    except Exception as e:   # noqa: BLE001 — 判定本身故障 ≠ 身份无权，必须可辨（A3）
        logger.warning("doc=%s 权威授权判定失败: %s", doc_id, e, exc_info=True)
        errors.append({"chunk_id": None, "error_type": type(e).__name__,
                       "message": _flatten(str(e))})
        identity_authorized, decided = False, False
    # 只在**判定成功且结论是"无权"**时报"投影过宽"。权威读不到或判定抛异常时真实含义是
    # 「无法判定」，那两种情况已经各自进了 errors，再喊一句"投影过宽"是误导。
    if decided and authority_acl is not None and not identity_authorized:
        logger.warning("doc=%s 本次查询身份未被权威授权读它 ⇒ 若仍被召回，说明**投影过宽**，"
                       "而不是一次通过的验证", doc_id)

    present, served, foreign = set(), set(), set()
    for c in chunks:
        q = (c.get("chunk_text") or "")[:snippet]
        if not q.strip():
            continue
        try:
            results = retrieve_fn(q, top_k=top_k, user_dept=ud, **extra) or []
        except Exception as e:   # noqa: BLE001 — 单块失败不中断整篇验证，但必须留痕
            # ⚠️ 此前这里静默 `results = []`，于是 DB / 配置 / HA3 的**探针自身故障**
            # 全部伪装成「该 chunk 不可检索」。判缺与探针坏掉是两件事，必须可辨。
            logger.warning("self-query 失败 chunk_id=%s: %s", c.get("chunk_id"), e, exc_info=True)
            errors.append({
                "chunk_id": c.get("chunk_id"),
                "error_type": type(e).__name__,
                "message": _flatten(str(e)),                  # 压平+截断;全文只进 logger
            })
            results = []
        for r in results:
            rdoc = r.get("doc_id")
            rid = r.get("id")
            if rdoc == doc_id:
                try:
                    served.add(int(rid))
                except (TypeError, ValueError):
                    pass
                # ⚠️ `chunk_id` 相等【不足以】证明该 chunk 在 HA3(C2，2026-08-03)：生产
                # `expand_step_context` 会把命中行的兄弟/子步骤从 **RDS** 拉出，并把展开行的
                # `chunk_id` 覆写成兄弟的(`id/doc_id` 仍继承命中行)。于是一个 HA3 里根本不存在
                # 的 step_card，只要任一同家族兄弟被索引，就会"命中兄弟→展开出自身 RDS 行→判
                # present" —— 把【从 RDS 展开取回】误当【HA3 可召回】，正是本模块要消灭的假绿。
                # `id` 分支不受影响：展开行继承的 `id` 恰是【产生该展开的原始 HA3 命中】的 id，
                # 对该 id 而言是真证据，滤掉反而制造假红。
                # 缺 `_direct_hit` 键时按 `is_expanded` 兜底、再缺则视为 direct —— 覆盖
                # expand_step_context 异常/无 RDS 连接时"回退到原始结果"(全是裸 HA3 命中)的路径。
                _direct = r.get("_direct_hit", not r.get("is_expanded", False))
                if str(rid) == str(c["id"]) or (
                        _direct and r.get("chunk_id") == c.get("chunk_id")):
                    present.add(int(c["id"]))
            elif rid is not None:
                try:
                    foreign.add(int(rid))
                except (TypeError, ValueError):
                    pass
    missing = sorted(set(expected) - present)
    return {
        "doc_id": doc_id,
        "expected_ids": expected,
        "present_ids": sorted(present),
        "missing_ids": missing,
        "present": len(present),
        "total": len(expected),
        "served_ids": sorted(served),
        # 契约键（docstring 承诺）：自查中浮现的【他文档】id —— 跨文档/ACL 泄漏信号。
        # 此前漏返回，导致上线 verify 读 result['foreign_ids'] KeyError、泄漏检查形同虚设。
        "foreign_ids": sorted(foreign),
        # 判缺/报绿时的必读上下文（一律取自**权威**，不看投影哨兵）:
        #   legacy          权威 legacy 且投影一致
        #   node            权威 node ⇒ 已用其授权节点作查询身份
        #   legacy?sentinel 权威 legacy 但投影里还是哨兵（反向未收敛）⇒ 结论需人工判读
        #   unknown         权威没读成（errors 里有原因）⇒ 本轮结论不可信
        "acl_mode": mode,
        # mode 是否为确定态。False ⇒ 本轮不构成一次有效验证(`ok` 也一定为 False)。
        "conclusive": conclusive,
        # 本次查询身份是否被**权威**授权读它（can_read_doc 真值表）。False ⇒ 本轮不是一次
        # 有效验证:此时若文档仍被召回,说明**检索投影比权威更宽**（发现,不是通过）。
        "identity_authorized": identity_authorized,
        # 探针自身故障(≠ 不可检索)。有任何一条 ⇒ ok 恒 False:哪怕其余查询"恰好凑齐"了
        # 全部 chunk,这一轮也不构成一次有效验证,绝不报绿。
        "errors": errors,
        "error_count": len(errors),
        "ok": (bool(expected) and not missing and not errors
               and conclusive and identity_authorized),
        "method": "per-chunk self-query (G30-immune)",
    }
