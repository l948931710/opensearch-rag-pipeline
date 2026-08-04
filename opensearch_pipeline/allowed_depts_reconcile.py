# -*- coding: utf-8 -*-
"""allowed_depts_reconcile.py — Phase D 跨部门授权投影对账（RDS 侧自愈；HA3 推送交 stage-3）。

从 authority（`fuling_knowledge.kb_access_request` status='approved'）重算每篇文档应有的
allowed_depts（经 `access_grants` 单一注入点 resolve + gate 到 dept_internal），与 chunk_meta
现存投影 diff，drift → 写 `chunk_meta.allowed_depts` + 重置 `index_status='NOT_INDEXED'`
（= stage-3 outbox；下次 stage-3 drain 重解析 authority→重嵌 dense+sparse→cmd=add 推 HA3，
清空/收窄 MULTI_STRING）。**双向**：materialize（approved 且 dept_internal）+ retract（不再
approved、或已改判非 dept_internal 的残留 → 清 NULL）。

与 `spot_checker.reconcile_*` 同型：逐文档提交、**绝不抛**、2h PROCESSING 反抢锁、LIMIT、flag 关
→ no-op。**只写 RDS，绝不写 HA3**（HA3 由既有 stage-3 drain 携带）。

定位：decide 端点的同步 dirty-mark 给最佳延迟下限；本对账每次 stage-3 pre-drain 跑一遍，兜住
「端点漏标脏（优雅降级吞写）」「绕过端点直接改库的 authority」两类漂移——authority 永远是唯一
事实源，投影任何时刻可从它全量重算。
"""
import logging

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)

def _kb_db() -> str:
    """知识库库名（kb_access_request/chunk_meta 所在库）；经 RAG_RDS_DATABASE 配置（STAGING=_stg）。
    惰性读 config（不在 import 期），随 RAG_ENV 指向 staging/prod 库。"""
    return get_config().rds.database


_LIMIT = 200


def _prescreen_unchanged(cur, targets) -> set:
    """批量预筛出【确定无漂移】的 doc 子集（perf E#36，消除常态轮次逐 doc 4×SQL 的 N+1）。

    两条聚合查询：① resolve_allowed_depts 一条 IN 拉全部 approved grants（白名单净化同口径）；
    ② 一条 current-version 投影明细（allowed_depts / permission_level）。随后在内存按
    materialize_doc_allowed_depts 同一 diff 口径（版本限定 gate 到 dept_internal + 排序比较）
    复算。只有明细数据完整且 want==have 的 doc 才判 unchanged；任何数据缺失 / 行形状异常 /
    坏 JSON 一律【不判】——保守方向：宁可多跑 materialize 复核，绝不把真实漂移误判成 unchanged。
    本函数只读；写路径与 2h 反抢锁语义完全由单一注入点 helper 保持不变。

    ⚠️ **node 文档一律不预筛**（C3，2026-08-03）：预筛的 want 只来自 legacy 权威
    (`resolve_allowed_depts` 读 kb_access_request)，而 materialize 对 node 文档的 want 来自
    `kb_doc_node_grant` 投影的 `d:/dx:` 值、且要一并比 owner 投影轴。想在预筛里复刻这几维，
    等于把唯一写实现的判定逻辑抄第二份 —— 而"两份判定逻辑漂移"正是本条缺陷的根因。
    node 直接交给单一注入点复核；legacy 保留预筛（perf E#36 的 N+1 收益）。
    """
    import json as _json
    from opensearch_pipeline.access_grants import _node_acl_columns_present, resolve_allowed_depts
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE

    targets = list(targets)
    if not targets:
        return set()
    # ⚠️ 这里【不用】resolve_acl_modes：它对读失败会静默按 legacy 返回，而"读不出模式"与
    # "确实是 legacy"在预筛里后果完全不同——前者会让 node 文档被 legacy 口径误判 unchanged
    # 而永久跳过复核。故自行区分两种情形，读失败一律整体放弃预筛（全量交单一注入点复核）。
    if _node_acl_columns_present(cur):        # 060 未 apply ⇒ 不存在 node 文档 ⇒ 照常预筛
        try:
            _ph = ",".join(["%s"] * len(targets))
            cur.execute(
                f"SELECT doc_id, acl_mode FROM {_kb_db()}.document_meta "
                f"WHERE doc_id IN ({_ph})", tuple(targets))
            _modes = {r[0]: str(r[1] or "").strip().lower() for r in cur.fetchall()}
        except Exception as e:  # noqa: BLE001 — 模式读失败 ⇒ 保守全量复核，绝不按 legacy 蒙混
            logger.warning("预筛 acl_mode 批查失败，本轮放弃预筛、全量复核: %s", e)
            return set()
        targets = [d for d in targets if _modes.get(d) != ACL_MODE_NODE]
        if not targets:
            return set()
    grants = resolve_allowed_depts(targets, cur)         # ① 应有授权（一条 IN 查询）
    placeholders = ",".join(["%s"] * len(targets))
    # B2-②（2026-08-03）：**去掉 `cm.version_no=dm.current_version_no`**。
    # 旧口径只看 current 版本 ⇒「current 干净、旧 active 版 dirty」会被提前判 unchanged，
    # materialize 永不执行、旧版本带着错投影继续被检索（旧版本 chunk 仍 is_active=1 时可服务）。
    # 现在**全部 active 版本**都进预筛，任一版本不符即交逐 doc 复核。
    cur.execute(                                         # ② 现存投影 + 版本限定权限（一条查询）
        f"SELECT DISTINCT cm.doc_id, cm.version_no, cm.allowed_depts, cm.permission_level, "
        f"       cm.owner_dept, dm.owner_dept "
        f"FROM {_kb_db()}.chunk_meta cm "
        f"JOIN {_kb_db()}.document_meta dm ON dm.doc_id=cm.doc_id "
        f"WHERE cm.is_active=1 AND cm.doc_id IN ({placeholders})",
        tuple(targets),
    )
    rows = cur.fetchall()
    # B2-②：`have_rows[d]` 是**逐行**的集合列表，**不是并集**。
    # 并集是有损的：current=[a,b]、旧版=[] 时并集仍等于 want ⇒ 旧版的漂移被 current 盖住、
    # 判成 unchanged。与 certify 侧 `projection_rows_all_match()` 的逐行哲学统一：
    # 判「要不要复核」宁可宽（多跑一次 materialize 无害），绝不漏。
    have_rows: dict = {}
    have_map: dict = {}
    perm_map: dict = {}
    owner_map: dict = {}          # doc → chunk 投影轴 owner 集合（集合语义，见下）
    want_owner: dict = {}         # doc → 应有 owner（legacy = document_meta 真实 owner）
    bad: set = set()
    for row in rows:
        try:
            d, ad, perm = row[0], row[2], row[3]
            cm_owner, dm_owner = row[4], row[5]
        except Exception:  # noqa: BLE001 — 行形状异常（桩/驱动差异）→ 整体放弃预筛，全量复核
            return set()
        owner_map.setdefault(d, set()).add(cm_owner or "")
        want_owner[d] = dm_owner or ""
        perm_map.setdefault(d, set())
        if perm is not None:                     # 与 GROUP_CONCAT 同口径：NULL 权限不计入 gate 判定
            perm_map[d].add(perm)
        # ⚠️ NULL/空 allowed_depts **也要记一行空集**（旧实现 `continue` 直接跳过 ⇒ 该行
        # 对判定完全隐形：want 非空而某版本为 NULL 时照样判 unchanged，与并集是同族泄漏）。
        if not ad:
            have_rows.setdefault(d, []).append(set())
            continue
        if isinstance(ad, list):
            have_rows.setdefault(d, []).append(set(ad))
            have_map.setdefault(d, set()).update(ad)
            continue
        try:
            _parsed = set(_json.loads(ad) or [])
            have_rows.setdefault(d, []).append(_parsed)
            have_map.setdefault(d, set()).update(_parsed)
        except (ValueError, TypeError):
            bad.add(d)                           # 坏 JSON → 该 doc 不预筛，交 materialize 复核

    unchanged = set()
    for d in targets:
        if d not in perm_map or d in bad:        # 无 current-version 明细 → 保守交逐 doc 复核
            continue
        raw_want = grants.get(d, [])
        # 版本限定 gate（与 materialize 的 GROUP_CONCAT==‘dept_internal’ 等价）：
        # 唯一权限级且为 dept_internal 才保留 want，否则 want=[]。
        want = raw_want if perm_map[d] == {"dept_internal"} else []
        # B2-②：**每一行**都必须等于 want（逐版本严格），不再拿并集比。
        _rows_d = have_rows.get(d) or [set()]
        if any(sorted(_r) != sorted(want) for _r in _rows_d):
            continue
        # owner 投影轴（C3）：allowed_depts 一致但 chunk owner 与应有值不符也是漂移，
        # 此前预筛完全不看这一维 ⇒ stale-owner 文档被判 unchanged、materialize 永不执行。
        # 这一维读的是【投影结果】(chunk_meta.owner_dept vs document_meta.owner_dept)，
        # 不是重算权威，故不构成"判定逻辑抄第二份"。
        # 集合相等而非首项相等：混合 owner（写到一半/并发）必须判漂移，与 materialize 同口径。
        if owner_map.get(d, set()) != {want_owner.get(d, "")}:
            continue
        unchanged.add(d)
    return unchanged


def reconcile_allowed_depts(commit: bool = True) -> dict:
    """全量对账 approved authority → chunk_meta.allowed_depts 投影。

    commit=False 为只读预览（统计 drift，不写）。Returns 统计 dict，**绝不抛**（失败进 errors）。
    flag 关 → 直接返回 skipped（投影路径全程惰性，零写）。
    """
    result = {"approved": 0, "materialized": 0, "retracted": 0, "unchanged": 0, "certified": 0,
              "reset_chunks": 0, "capped": False, "skipped": False, "errors": []}
    if not get_config().rag.allowed_depts_acl:
        result["skipped"] = True
        return result

    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cur:
            # 1. authority → approved doc_ids（统计 + 候选并集；逐文档物化经单一注入点 helper）
            cur.execute(f"SELECT DISTINCT doc_id FROM {_kb_db()}.kb_access_request WHERE status='approved'")
            approved = [r[0] for r in cur.fetchall() if r and r[0]]
            result["approved"] = len(approved)
            # 2. 候选 = approved ∪ 仍有残留 allowed_depts 的文档（后者是 retract 候选）
            cur.execute(f"SELECT DISTINCT doc_id FROM {_kb_db()}.chunk_meta "
                        f"WHERE is_active=1 AND allowed_depts IS NOT NULL")
            have_ad = {r[0] for r in cur.fetchall() if r and r[0]}
            # 2b. node-ACL 权威（2026-07-31，设计稿 §4「候选集纳入 kb_doc_node_grant」）：
            #     approved ∪ have_ad **兜不住"从未投影过的 node 文档"** —— 它既没有
            #     kb_access_request 行（那是 legacy 权威），chunk_meta.allowed_depts 也还是
            #     NULL（decide 端点漏入队 / outbox 没 drain / 直接改库授权），于是全扫这条
            #     最后防线正好跳过它 ⇒ 管理员在管理台勾了组织节点，文档却永远搜不到。
            #     已投影过的 node 文档本来就会经 have_ad 进来，这里补的是"零投影"那一类。
            #     ⚠️ 060 未 apply 时表不存在 —— 与 org_sync 同款处置：跳过，不阻断整轮对账。
            try:
                cur.execute(f"SELECT DISTINCT doc_id FROM {_kb_db()}.kb_doc_node_grant "
                            "WHERE revoked_at IS NULL")
                node_granted = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:   # noqa: BLE001 — schema/060 未 apply
                logger.debug("kb_doc_node_grant 候选跳过（表不存在?）: %s", e)
                node_granted = set()
            # 2c. C3（2026-08-03）：**零 active grant** 的 node 文档三源全不命中 —— 它没有
            #     kb_access_request 行、allowed_depts 是 NULL、kb_doc_node_grant 里也没有
            #     未撤销行（从未授权 / 授权已全部撤销）。但 project_doc_acl 对 node 模式
            #     **无论节点集是否为空**都要求 owner 投影为哨兵；漏掉它 ⇒ chunk 仍挂旧真实
            #     owner ⇒ legacy owner 分支持续放行旧 owner 组 = stale-owner 越权【永久态】,
            #     而本对账正是最后一道自愈防线。
            #     INNER JOIN 到 current_version 的 active chunk：结构上排除"current 无 chunk"
            #     的文档（否则 owner 期望哨兵、实得空集，每轮判漂移、零行写、白占 _LIMIT 预算）。
            #     `<=>` 是 null-safe 比较：`<> 哨兵` 在三值逻辑下会漏掉 owner_dept IS NULL。
            #     非 current 的旧服务版本不在此列 —— 那是 materializer 版本轴问题（C3′，另立）。
            try:
                from opensearch_pipeline.acl_policy import NODE_OWNER_SENTINEL
                cur.execute(
                    f"SELECT DISTINCT dm.doc_id FROM {_kb_db()}.document_meta dm "
                    f"JOIN {_kb_db()}.chunk_meta cm "
                    "  ON cm.doc_id=dm.doc_id AND cm.version_no=dm.current_version_no "
                    " AND cm.is_active=1 "
                    "WHERE dm.acl_mode='node' AND NOT (cm.owner_dept <=> %s)",
                    (NODE_OWNER_SENTINEL,),
                )
                node_stale_owner = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:   # noqa: BLE001 — schema/060 未 apply（无 acl_mode 列）
                logger.debug("node stale-owner 候选跳过（060 未 apply?）: %s", e)
                node_stale_owner = set()
            # ── B2-① epoch 候选源（schema/062）——**sweep 的前提** ──────────────────
            # 此前候选只有「有 approved 授权 / 已有投影 / node 授权 / node stale-owner」四路，
            # 全是**从别处推出来**的。062 落了两列却**零消费**：
            #   · `chunk_meta.acl_epoch IS NULL` = **从未投影过** —— 恰恰是 C3′ 要修的那类
            #     （diff 在无上次结果时恒为空 ⇒ 永远判 unchanged），四路候选一条都覆盖不到；
            #   · `chunk_meta.acl_epoch < document_meta.acl_epoch` = 投影**落后于失效代次**。
            # 逐版本判（不限 current）：非 current 的旧 active 版同样要收敛。
            # capability 探测：062 未 apply 的环境降级为空集，绝不 1054 打挂整轮对账。
            epoch_dirty: set = set()
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chunk_meta' "
                    "AND COLUMN_NAME='acl_epoch'")
                _r = cur.fetchone()
                if _r and _r[0]:
                    cur.execute(
                        f"SELECT DISTINCT cm.doc_id FROM {_kb_db()}.chunk_meta cm "
                        f"JOIN {_kb_db()}.document_meta dm ON dm.doc_id=cm.doc_id "
                        "WHERE cm.is_active=1 "
                        "  AND (cm.acl_epoch IS NULL OR cm.acl_epoch < dm.acl_epoch)")
                    epoch_dirty = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception as e:   # noqa: BLE001 — 062 未 apply / 列缺失
                logger.debug("epoch 候选跳过（062 未 apply?）: %s", e)
                epoch_dirty = set()

            # ── B2-③ 不变量破坏：chunk.acl_epoch **不可能**大于 dm.acl_epoch ────────────
            # chunk 的章是从 doc 水位盖下来的，而 doc 水位单调只增 ⇒ 出现 `>` 只可能是
            # 手工改库 / 回滚 / 迁移错序 / 并发写乱序。这类文档**绝不可**被判 unchanged
            # （它的 epoch 会让后续 sweep 认为"已投影且更新"，永久掩盖真实漂移）。
            # 处置：大声报错 + **强制排除出预筛跳过集**，让它每轮都走完整 materialize 复核。
            invariant_bad: set = set()
            try:
                cur.execute(
                    f"SELECT DISTINCT cm.doc_id FROM {_kb_db()}.chunk_meta cm "
                    f"JOIN {_kb_db()}.document_meta dm ON dm.doc_id=cm.doc_id "
                    "WHERE cm.is_active=1 AND cm.acl_epoch IS NOT NULL "
                    "  AND cm.acl_epoch > dm.acl_epoch")
                invariant_bad = {r[0] for r in cur.fetchall() if r and r[0]}
            except Exception:   # noqa: BLE001 — 062 未 apply 时无从判，跳过
                invariant_bad = set()
            if invariant_bad:
                _msg = (f"🔴 ACL epoch 不变量破坏：{len(invariant_bad)} 篇文档的 chunk.acl_epoch "
                        f"> document_meta.acl_epoch（章不可能新过水位）。样本："
                        f"{sorted(invariant_bad)[:5]}。这些文档已强制排除出预筛跳过集、每轮全量复核；"
                        "请排查手工改库/迁移错序/回滚。")
                logger.error(_msg)
                result["errors"].append(_msg)
                result["invariant_violations"] = len(invariant_bad)

            # 全量扫描候选，但按【实际漂移写】数封顶（_LIMIT）——unchanged 文档只读不占写预算，故高位
            # 漂移文档绝不会被一致文档挤出（旧实现 sorted(...)[:_LIMIT] 固定切片会饿死高位漂移；Step 5
            # 审计）。漂移文档本轮处理后下轮即变 unchanged，预算自然腾给后续漂移 → 自清、最终全覆盖。
            targets = sorted(set(approved) | have_ad | node_granted | node_stale_owner
                             | epoch_dirty | invariant_bad)

            # perf E#36：批量预筛（2 条聚合查询）先在内存 diff 出确定无漂移的 doc，跳过其
            # 逐 doc 4×SQL 复核；漂移/存疑子集仍走单一注入点 materialize 复核+写（锁/gate/
            # 写语义不变）。预筛异常 → 退回全量逐 doc 复核（现状行为，graceful degradation）。
            prescreen_unchanged: set = set()
            try:
                prescreen_unchanged = _prescreen_unchanged(cur, targets)
            except Exception as pe:  # noqa: BLE001 — 预筛失败绝不影响对账本体
                logger.warning("allowed_depts 预筛失败，退回全量逐 doc 复核: %s", pe)
            # B2-③：不变量破坏的文档**永不**享受预筛跳过（其 epoch 不可信）。
            prescreen_unchanged -= invariant_bad
            result["unchanged"] += len(prescreen_unchanged)

            for doc_id in targets:
                if doc_id in prescreen_unchanged:
                    continue                             # 预筛判定无漂移 → 零逐 doc SQL
                if result["materialized"] + result["retracted"] >= _LIMIT:
                    result["capped"] = True
                    logger.info("allowed_depts reconcile 单轮写达上限 _LIMIT=%d，剩余漂移下轮续（自清不饿死）",
                                _LIMIT)
                    break
                try:
                    # helper：current version + 2h PROCESSING 反抢锁 → 版本限定 gate → diff → 写标脏。
                    # 不提交、不写 HA3；apply=commit 支持只读预览（commit=False 只统计漂移不写）。
                    outcome = materialize_doc_allowed_depts(cur, doc_id, apply=commit)
                    status = outcome["status"]
                    if status == "unchanged":
                        result["unchanged"] += 1
                    elif status == "certified":
                        # C3′/062：值本就正确、只补盖 epoch 章（不动 index_status、不重推 HA3）。
                        # ⚠️ **必须单独提交** —— 本函数只在 materialized/retracted 分支 commit，
                        # 漏掉 certified 会让刚写的 epoch 要么丢、要么被下一篇的 commit 意外带上。
                        result["certified"] = result.get("certified", 0) + 1
                        if commit:
                            conn.commit()
                    elif status in ("materialized", "retracted"):
                        result[status] += 1
                        result["reset_chunks"] += outcome["reset_chunks"]
                        if commit:
                            conn.commit()                # 逐文档提交（单文档失败不连累其余）
                    # skipped / skipped_locked：current version 正在 stage-3 跑 → 本轮跳过、下轮再对
                except Exception as e:                   # noqa: BLE001 — 单文档失败不抛、记 errors
                    result["errors"].append(f"{doc_id}: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    except Exception as e:                               # noqa: BLE001 — 顶层亦绝不抛
        result["errors"].append(f"reconcile failed: {e}")
    finally:
        conn.close()
    return result
