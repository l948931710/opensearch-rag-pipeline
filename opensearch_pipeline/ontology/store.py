# -*- coding: utf-8 -*-
"""
store.py — 本体表族（schema/027–030）的持久层：RDSOntologyStore + MemoryOntologyStore。

双后端同一契约（沿 session_store Memory/Redis 双后端惯例）：Memory 供单测/SIM 注入，
RDS 走 `db._get_db_conn()`（池 + GuardedDBConnection 写守卫——PROD-RO/非生产→生产写
在连接层被拦，本层不再自建环境判断）。

写入语义（外评 S1–S4 的持久层承载）：
- **本层只做存取与状态机，不做消解决策**——在线 resolve() 纯读；谁能调用写方法由上层
  （seeding/回填 worker/工作台 routes/受治理 Action）与审计负责（PR6 落 agent_audit_log）。
- 至多一行 active 别名（uk_ns_norm_active）→ 重复插入抛 DuplicateActiveIdentifier；
- 至多一个 open case（uk_ns_norm_open）→ upsert_case 命中即 seen_count+1（观测聚合）；
- 状态迁移一律 CAS（UPDATE … WHERE status=旧值，rowcount=0 即让位并发方）；
- golden_json 乐观锁（version CAS），冲突返回 False 由调用方重取。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from opensearch_pipeline.ontology.ids import format_ref, new_ulid
from opensearch_pipeline.ontology.normalize import title_key

logger = logging.getLogger(__name__)

__all__ = [
    "DuplicateActiveIdentifier",
    "LINK_TYPE_SPECS",
    "LinkCardinalityViolation",
    "MemoryOntologyStore",
    "RDSOntologyStore",
    "SEM_PROJECTIONS",
]

# ── PMC-1 语义投影契约（schema/030 视图列 = 内联回退列 = Memory 组装列，三方同形）────
# fields 即 spec.golden_json 的一级键（视图里 JSON_UNQUOTE(JSON_EXTRACT('$.<f>'))）。
SEM_PROJECTIONS: Dict[str, Dict[str, Any]] = {
    "packing": {"view": "sem_packing", "spec_type": "packing_spec",
                "link_type": "packing_spec_of_sku",
                "fields": ("box_type", "per_box", "outer_dim")},
    "stacking": {"view": "sem_stacking", "spec_type": "stacking_spec",
                 "link_type": "stacking_spec_of_sku",
                 "fields": ("stack_mode", "per_stack", "container_cbm")},
}
_SEM_PROBE_TTL_S = 300.0

_IDENTIFIER_END_STATES = ("rejected", "superseded")

# ── PR-G 服务层约束（与 027-029 的 CHECK/FK 同口径，双后端一致先行报错）────────────
# 关系类型白名单 + 端点/基数契约（P1-8）：每个 link_type 声明 src/dst 对象类型与 src 侧
# 基数。single = 同一 src 同型至多一条 active link（此前无此闸——一个 SKU 可挂两条
# active sku_of_product，sem 视图直接翻倍/串味）。DB 级兜底见 schema/033 生成列唯一键。
# 新增关系类型是受治理变更：走代码 PR 扩本表 + 029 注释；single 型还须同步扩 033 的
# IN 列表（见该文件头「扩展方式」），否则 DB 兜底缺位。
LINK_TYPE_SPECS: Dict[str, Dict[str, str]] = {
    # 一个 SKU 只属于一个产品（双 active=同 SKU 挂两个产品的语义错配）
    "sku_of_product":       {"src_type": "sku", "dst_type": "product",
                             "src_cardinality": "single"},
    # 一份箱规/香规只描述一个 SKU；同 SKU 可挂多份 spec（verified/draft 并存由 sem 挑行）
    "packing_spec_of_sku":  {"src_type": "packing_spec", "dst_type": "sku",
                             "src_cardinality": "single"},
    "stacking_spec_of_sku": {"src_type": "stacking_spec", "dst_type": "sku",
                             "src_cardinality": "single"},
    # 一副模具对应一个产品几何（家族模/共享模属型录外情形，真出现时走治理 PR 放宽）
    "mold_of_product":      {"src_type": "mold", "dst_type": "product",
                             "src_cardinality": "single"},
    # 同一牌号供多产品：multi
    "material_of_product":  {"src_type": "material", "dst_type": "product",
                             "src_cardinality": "multi"},
}
VALID_LINK_TYPES = frozenset(LINK_TYPE_SPECS)
_VALID_LIFECYCLE_STATES = ("draft", "verified")

# P1-9：泛型裸写（mint_object / insert_identifier / add_link）的调用方声明——只应经
# S1 四路径（播种/回填/工作台/受治理 Action）或测试进入；未声明即拒。这不是加密屏障，
# 是让每个绕过生命周期方法的写点显式、可 grep、可评审；复合原子写
# （mint_object_with_alias / confirm_case_with_identifier / insert_identifier_closing_case
# / repoint_identifier 等 S3 生命周期方法）是受祝福入口，不设此闸。
_GOVERNED_WRITE_CALLERS = frozenset({
    "seeding", "backfill", "steward_workbench", "governed_action",
    "store_internal",   # 仅 store 复合写内部委托（Memory mint_object_with_alias→mint_object）
    "test",
})


def _check_confidence(v: float) -> None:
    if not (0.0 <= float(v) <= 1.0):
        raise ValueError(f"confidence 越界：{v}（须 0..1——越界置信是 auto 判定的输入污染）")


def _check_link_type(t: str) -> None:
    if t not in VALID_LINK_TYPES:
        raise ValueError(f"未登记的 link_type {t!r}（合法：{sorted(VALID_LINK_TYPES)}；"
                         "新增关系类型是受治理变更，走代码 PR 扩 VALID_LINK_TYPES + 029 注释）")


def _check_lifecycle(state: str) -> None:
    if state not in _VALID_LIFECYCLE_STATES:
        raise ValueError(f"非法 lifecycle_state {state!r}（合法：{_VALID_LIFECYCLE_STATES}）")


def _check_owner_dept(dept: str) -> None:
    """owner_dept 必须是既有 ACL 组码（kb_authz 白名单单一来源）——sem/authz 的行过滤
    按 owner_dept ∈ acl 判定，私造组码=对象对所有人不可见的静默黑洞。

    P2 收口（2026-07-11 外评）：白名单加载失败 → **fail-closed 拒绝写入**。旧行为是
    告警放行——治理控制面的准入校验被"环境缺依赖"静默关掉，正是审计点名的 fail-open；
    校验做不了就不写，绝不带病放行。"""
    try:
        from opensearch_pipeline.kb_authz import _valid_owner_depts
        whitelist = _valid_owner_depts()
    except Exception as e:   # noqa: BLE001
        raise RuntimeError(
            f"owner_dept 白名单不可用（kb_authz 加载失败：{e}）——治理控制面 fail-closed，"
            "拒绝写入；请先修复运行环境（缺依赖/坏配置），不降级放行") from e
    if dept not in whitelist:
        raise ValueError(f"owner_dept {dept!r} 不在 ACL 组码白名单（私造组码会让对象"
                         "对所有人不可见；新组码走 user_role→白名单→灰度 的独立 PR）")


def _check_caller(caller: Optional[str], method: str) -> None:
    """P1-9：裸写原语须显式声明调用路径——bypass 生命周期的写点全部可 grep 可评审。"""
    if caller not in _GOVERNED_WRITE_CALLERS:
        raise PermissionError(
            f"{method} 是绕过生命周期的裸写原语（P1-9）——须显式 _caller ∈ "
            f"{sorted(_GOVERNED_WRITE_CALLERS - {'store_internal'})}，得到 {caller!r}；"
            "业务代码请改走 mint_object_with_alias / confirm_case_with_identifier / "
            "insert_identifier_closing_case 等原子生命周期方法")


def _check_link_endpoints(link_type: str, src: Optional[Dict[str, Any]],
                          dst: Optional[Dict[str, Any]]) -> None:
    """P1-8 端点校验：存在、active、类型匹配 LINK_TYPE_SPECS（双后端同文案，契约测试钉住）。"""
    spec = LINK_TYPE_SPECS[link_type]
    if src is None or dst is None:
        raise ValueError("link 端点对象不存在（FK 拦截）")
    for role, row, want in (("src", src, spec["src_type"]), ("dst", dst, spec["dst_type"])):
        if row.get("status") != "active":
            raise ValueError(f"link {role} 端点 {row.get('object_id')} 非 active"
                             f"（status={row.get('status')}）——退役/合并对象不得再建关系")
        if row.get("object_type") != want:
            raise ValueError(
                f"link_type {link_type!r} 要求 {role}={want}，得到 "
                f"{row.get('object_type')!r}（端点类型错配是 sem 投影串味的根源）")


def _check_golden_provenance(golden_attrs, provenance: Optional[Dict[str, Any]], *,
                             allow_missing: bool, where: str) -> None:
    """P1-10 fail-closed：golden 属性值必须随值级溯源（sor_system/as_of）落库，缺失即拒。
    allow_missing_provenance=True 是测试/显式声明无源数据的逃生口——生产调用方
    （seeding/backfill/工作台）不得使用（评审红线，grep 可查）。"""
    if allow_missing or not golden_attrs:
        return
    prov = provenance or {}
    missing = sorted(a for a in golden_attrs if not prov.get(a))
    if missing:
        raise ValueError(
            f"{where}: golden 属性 {missing} 缺值级 provenance——没有溯源的黄金值会悄悄"
            "变成业务事实副本（P1-10 fail-closed）；确属无源数据须显式 "
            "allow_missing_provenance=True")


class DuplicateActiveIdentifier(Exception):
    """同 (namespace, norm_value) 已有 active 别名——撞 uk_ns_norm_active。"""


class CommitAmbiguous(Exception):
    """受治理写事务的 **commit 段**异常——结果不可知（服务端可能已提交/在途/未提交），
    绝不可按「确定失败」分类（Majors α1/M3.1，codex 共识 2026-07-21）。

    工具层唯一可判凭据=与副作用同事务写入的操作台账（agent_tool_operation）三态：
    applied → 幂等成功收口；not_applied/unknown → 原样上抛由 executor 归 uncertain
    （053 对账器收敛）。uk_ns_norm_active 读回**不能**当凭据——identifier 行无
    operation_id 无法归属到本次操作，且慢提交窗口内「无行」≠未提交。"""


class LinkCardinalityViolation(Exception):
    """single 基数 link 已有指向其他对象的 active 行（P1-8）——先 retire 旧关系
    （S3 纠错）再建新，绝不静默双活。"""


def _dump(obj: Any) -> Optional[str]:
    return None if obj is None else json.dumps(obj, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# RDS 后端
# ══════════════════════════════════════════════════════════════════════════════
def _begin(conn) -> None:
    """写事务前显式 begin()：钉住池化 SteadyDB 连接，禁「断连/死锁 → 透明重连+单句重放」
    （dbutils tough_method 在 _transaction 位未置时的默认行为；1213/1205 也映射为
    OperationalError，一并命中）。不钉的实锤现场（2026-07-17，xdist 并行 ontology 测试）：
    mint 的 INSERT 与他 worker 清扫 DELETE 死锁，victim 整事务被服务端回滚（ref_seq 进位
    一并蒸发），SteadyDB 却在新连接重放该条 INSERT 并随 commit 落库 → 对象在、计数器回退，
    此后每次铸号都撞 uk_ref 1062 连环红，直到该行被清扫才解砖。begin() 后错误如实上抛走
    rollback，多语句事务原子性（P0-05/06 契约）才真正成立。同款纪律（批次1）：
    agent_runtime/run_store._begin、spot_checker、cost_breaker。桩连接无 begin 则跳过。"""
    fn = getattr(conn, "begin", None)
    if fn is not None:
        fn()


class RDSOntologyStore:
    """ontology_*（schema/027–029）的 RDS 实现。DB 访问沿 registry_store 惯例：
    `db._get_db_conn()`、`%s` 占位、f-string 库前缀、显式 commit/rollback/close。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    @staticmethod
    def _db() -> str:
        # PR-B（P0-02）：ontology 表族在独立库——fuling_ro 的库级 wildcard SELECT 只覆盖
        # fuling_operation，独立库不授即物理隔离（SHOW GRANTS 测试钉住）。
        from opensearch_pipeline.config import get_config
        return get_config().rds.ontology_database

    @staticmethod
    def _rows_to_dicts(cur) -> List[Dict[str, Any]]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    @staticmethod
    def _is_dup(exc: Exception) -> bool:
        return bool(getattr(exc, "args", None)) and exc.args[0] == 1062

    @staticmethod
    def _is_fk_violation(exc: Exception) -> bool:
        # 1452 = Cannot add or update a child row（FK 悬空目标，P0-03）
        return bool(getattr(exc, "args", None)) and exc.args[0] == 1452

    @staticmethod
    def _audit_in_tx(cur, audit: Optional[Dict[str, Any]]) -> None:
        """同事务合规审计行（PR-C，P0-06 fail-closed）：审计行与事实变更一个事务——
        写失败异常上抛 → 整笔回滚，**审计不可用时零副作用**。audit=None 跳过
        （离线播种等非治理路径；工作台/工具路径必传）。跨库同连接（agent_audit_log
        在 fuling_operation，本体表在 fuling_ontology，同实例单连接事务成立）。"""
        if not audit:
            return
        import uuid as _uuid

        from opensearch_pipeline.config import get_config
        op_db = get_config().rds.operation_database
        detail = audit.get("detail") or {}
        cur.execute(
            f"INSERT INTO {op_db}.agent_audit_log "
            "(audit_id, event_type, action, decision, user_id, detail_json, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,NOW(3))",
            (_uuid.uuid4().hex, audit.get("event_type", "ontology_workbench"),
             str(audit.get("action") or "-")[:96], str(audit.get("decision") or "-")[:32],
             str(audit.get("by") or "-")[:64],
             json.dumps(detail, ensure_ascii=False, default=str)))

    # ── 对象 ────────────────────────────────────────────────────────────────
    def mint_object(self, object_type: str, title: str, *, owner_dept: str,
                    golden: Optional[Dict[str, Any]] = None,
                    provenance: Optional[Dict[str, Any]] = None,
                    data_classification: str = "internal",
                    source_of_record: str = "ontology",
                    lifecycle_state: str = "draft",
                    allow_missing_provenance: bool = False,
                    _caller: Optional[str] = None) -> Dict[str, Any]:
        """铸 canonical 对象：ref_seq 原子取号 + INSERT，同一事务。
        object_type 未在 ontology_ref_seq 登记 → ValueError（登记类型码是治理动作，不静默造号）。
        P1-9：裸铸（无别名）须显式 _caller；业务路径请走 mint_object_with_alias。
        P1-10：golden 非空须随属性级 provenance（fail-closed）。"""
        _check_caller(_caller, "mint_object")
        _check_owner_dept(owner_dept)
        _check_lifecycle(lifecycle_state)
        _check_golden_provenance(set(golden or {}), provenance,
                                 allow_missing=allow_missing_provenance, where="mint_object")
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT type_code FROM {db}.ontology_ref_seq "
                            "WHERE object_type=%s", (object_type,))
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"object_type {object_type!r} 未在 ontology_ref_seq 登记类型码")
                type_code = row[0]
                cur.execute(f"UPDATE {db}.ontology_ref_seq "
                            "SET next_no=LAST_INSERT_ID(next_no+1) WHERE object_type=%s",
                            (object_type,))
                cur.execute("SELECT LAST_INSERT_ID()")
                seq_no = int(cur.fetchone()[0])
                object_id = new_ulid()
                canonical_ref = format_ref(type_code, seq_no)
                cur.execute(
                    f"INSERT INTO {db}.ontology_object "
                    "(object_id, object_type, canonical_ref, title, normalized_title, "
                    " golden_json, golden_provenance_json, lifecycle_state, "
                    " owner_dept, data_classification, source_of_record) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (object_id, object_type, canonical_ref, title, title_key(title),
                     _dump(golden or {}), _dump(provenance),
                     lifecycle_state, owner_dept, data_classification, source_of_record))
            conn.commit()
            return {"object_id": object_id, "object_type": object_type,
                    "canonical_ref": canonical_ref, "title": title}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_object(self, object_id: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_object WHERE object_id=%s", (object_id,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def find_objects(self, object_type: str, *, title_like: Optional[str] = None,
                     norm_title: Optional[str] = None,
                     status: str = "active", limit: int = 50) -> List[Dict[str, Any]]:
        """norm_title（C2 复核批次6）：normalized_title（038）等值召回——同名聚类的
        权威臂（LIKE 对空白/全半角变体天然漏召回）。存量行须先跑
        scripts/backfill_normalized_title.py，否则 NULL 行不参与等值匹配。"""
        db, conn = self._db(), self._conn()
        limit = max(1, min(int(limit), 200))
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT object_id, object_type, canonical_ref, title, lifecycle_state, "
                       f"owner_dept, data_classification, status FROM {db}.ontology_object "
                       "WHERE object_type=%s AND status=%s")
                params: List[Any] = [object_type, status]
                if title_like:
                    sql += " AND title LIKE %s"
                    params.append(f"%{title_like}%")
                if norm_title:
                    sql += " AND normalized_title=%s"
                    params.append(norm_title)
                cur.execute(sql + " ORDER BY canonical_ref LIMIT %s", (*params, limit))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def count_objects(self, object_type: str, *, title_like: Optional[str] = None,
                      status: str = "active") -> int:
        """find_objects 同谓词计数（P1「召回截断可观测」：搜索结果告知 total/truncated）。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT COUNT(*) FROM {db}.ontology_object "
                       "WHERE object_type=%s AND status=%s")
                params: List[Any] = [object_type, status]
                if title_like:
                    sql += " AND title LIKE %s"
                    params.append(f"%{title_like}%")
                cur.execute(sql, params)
                return int(cur.fetchone()[0])
        finally:
            conn.close()

    def search_objects_authorized(self, object_type: str, *, acl,
                                  bypass_acl: bool = False,
                                  title_like: Optional[str] = None,
                                  status: str = "active",
                                  limit: int = 50) -> List[Dict[str, Any]]:
        """find_objects 的**授权版**（P0-A②③）：对象级 ACL 谓词下推到 SQL——
        LIMIT 作用在授权集合上，不可见行既不出库也不挤占页位（旧"先 limit 再逐行
        filter"会被排前面的隐藏行饿死可见结果）。谓词与 authz.can_read_object
        单一来源（acl_read_predicate_sql 同模块互相印照）。"""
        from opensearch_pipeline.ontology.authz import acl_read_predicate_sql
        frag, acl_params = acl_read_predicate_sql(acl=acl, bypass_acl=bypass_acl)
        db, conn = self._db(), self._conn()
        limit = max(1, min(int(limit), 200))
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT object_id, object_type, canonical_ref, title, lifecycle_state, "
                       f"owner_dept, data_classification, status FROM {db}.ontology_object "
                       "WHERE object_type=%s AND status=%s")
                params: List[Any] = [object_type, status]
                if title_like:
                    sql += " AND title LIKE %s"
                    params.append(f"%{title_like}%")
                sql += f" AND {frag}"
                params.extend(acl_params)
                cur.execute(sql + " ORDER BY canonical_ref LIMIT %s", (*params, limit))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def search_objects_authorized_full(self, object_type: str, *, acl,
                                       bypass_acl: bool = False,
                                       title_like: Optional[str] = None,
                                       status: str = "active",
                                       limit: int = 50) -> List[Dict[str, Any]]:
        """search_objects_authorized 的**全列版**（perf 批次 A §4.2）：SELECT 追加
        version / golden_json / golden_provenance_json，供 CalcRule.from_object 直接
        消费——消除 packing_calc._select_rule 逐规则 get_object 的 +1。ACL 谓词与
        search_objects_authorized 同源（acl_read_predicate_sql），可见集逐字节一致
        （LIMIT 作用在授权集合上，post-ACL）。⚠️ golden_json 是重列，勿替换轻量
        list/search 路径。"""
        from opensearch_pipeline.ontology.authz import acl_read_predicate_sql
        frag, acl_params = acl_read_predicate_sql(acl=acl, bypass_acl=bypass_acl)
        db, conn = self._db(), self._conn()
        limit = max(1, min(int(limit), 200))
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT object_id, object_type, canonical_ref, title, lifecycle_state, "
                       f"owner_dept, data_classification, status, version, golden_json, "
                       f"golden_provenance_json FROM {db}.ontology_object "
                       "WHERE object_type=%s AND status=%s")
                params: List[Any] = [object_type, status]
                if title_like:
                    sql += " AND title LIKE %s"
                    params.append(f"%{title_like}%")
                sql += f" AND {frag}"
                params.extend(acl_params)
                cur.execute(sql + " ORDER BY canonical_ref LIMIT %s", (*params, limit))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def count_objects_authorized(self, object_type: str, *, acl,
                                 bypass_acl: bool = False,
                                 title_like: Optional[str] = None,
                                 status: str = "active") -> int:
        """count_objects 的授权版（P0-A②）：total/truncated 只计**可见集合**——
        未授权的全量计数会泄露不可见对象的存在与数量。谓词同 search_objects_authorized。"""
        from opensearch_pipeline.ontology.authz import acl_read_predicate_sql
        frag, acl_params = acl_read_predicate_sql(acl=acl, bypass_acl=bypass_acl)
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                sql = (f"SELECT COUNT(*) FROM {db}.ontology_object "
                       "WHERE object_type=%s AND status=%s")
                params: List[Any] = [object_type, status]
                if title_like:
                    sql += " AND title LIKE %s"
                    params.append(f"%{title_like}%")
                sql += f" AND {frag}"
                params.extend(acl_params)
                cur.execute(sql, params)
                return int(cur.fetchone()[0])
        finally:
            conn.close()

    def update_golden(self, object_id: str, golden: Dict[str, Any], *,
                      expected_version: int,
                      provenance: Optional[Dict[str, Any]] = None,
                      allow_missing_provenance: bool = False) -> bool:
        """乐观锁 CAS：版本不匹配返回 False（调用方重取重试）。

        provenance（PR-H → P1-10 收口）：**值变更 fail-closed 强制刷新溯源**——先读
        expected_version 时点的旧 golden 求 changed（新增/改值）与 removed 属性集：
        changed 必须由本次 provenance 覆盖（新值挂旧来源=溯源造假，此前允许）；removed
        属性的溯源行随值一并剪除（不留陈旧来源）。CAS 语义不变：UPDATE 仍带 version
        谓词，diff 只在 expected_version 快照上成立，竞态输家照旧返回 False。"""
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT golden_json FROM {db}.ontology_object "
                            "WHERE object_id=%s AND version=%s", (object_id, expected_version))
                row = cur.fetchone()
                if row is None:
                    return False
                try:
                    old = json.loads(row[0]) if row[0] else {}
                except Exception:   # noqa: BLE001 — 旧值坏 JSON：全部按变更对待（宁严勿漏）
                    old = {}
                new = dict(golden or {})
                changed = {k for k, v in new.items() if k not in old or old[k] != v}
                removed = set(old) - set(new)
                _check_golden_provenance(changed, provenance,
                                         allow_missing=allow_missing_provenance,
                                         where="update_golden")
                prov_patch = dict(provenance or {})
                for k in removed:      # JSON_MERGE_PATCH：null=删键（RFC 7396）
                    prov_patch[k] = None
                if prov_patch:
                    cur.execute(
                        f"UPDATE {db}.ontology_object SET golden_json=%s, "
                        "golden_provenance_json=JSON_MERGE_PATCH("
                        "COALESCE(golden_provenance_json, JSON_OBJECT()), %s), "
                        "version=version+1 WHERE object_id=%s AND version=%s",
                        (_dump(golden), _dump(prov_patch), object_id, expected_version))
                else:
                    cur.execute(
                        f"UPDATE {db}.ontology_object SET golden_json=%s, version=version+1 "
                        "WHERE object_id=%s AND version=%s",
                        (_dump(golden), object_id, expected_version))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def retire_object(self, object_id: str, *, audit: Optional[Dict[str, Any]] = None) -> bool:
        """S3 纠错（P0-03 收口）：**同一事务**内 对象 active→retired + 其全部 active 别名→'retired'
        + 其双向 active link→retired。退役后不留任何仍指向它的 active 引用（resolve/sem 双闸兜底）。
        audit 同事务落 agent_audit_log（P0-06 fail-closed）。"""
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {db}.ontology_object "
                            "WHERE object_id=%s FOR UPDATE", (object_id,))
                row = cur.fetchone()
                if row is None or row[0] != "active":
                    conn.rollback()
                    return False
                cur.execute(f"UPDATE {db}.ontology_object SET status='retired' "
                            "WHERE object_id=%s", (object_id,))
                cur.execute(f"UPDATE {db}.ontology_identifier SET status='retired' "
                            "WHERE target_object_id=%s AND status='active'", (object_id,))
                cur.execute(f"UPDATE {db}.ontology_link SET status='retired' "
                            "WHERE (src_object_id=%s OR dst_object_id=%s) AND status='active'",
                            (object_id, object_id))
                self._audit_in_tx(cur, audit)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_duplicate(self, object_id: str, merged_into: str, *,
                       by: Optional[str] = None,
                       audit: Optional[Dict[str, Any]] = None) -> bool:
        """S3 纠错（P0-03 收口）：**同一事务**内 source active→merged(+merged_into) +
        source 的全部 active 别名改指 target（supersede+插新 active 行，身份连续）+
        source 的 active link→retired（不搬运——target 自己的关系不受污染，全量传播=P2）。

        闸（均在锁内校验）：同 object_type（跨类型合并=语义脊柱污染，拒）；target 必须
        active（active 对象 merged_into 恒 NULL → 链长为 0，环构造性不可能）；自指拒。
        锁序按 object_id 排序取 FOR UPDATE，防对向并发死锁。"""
        if object_id == merged_into:
            raise ValueError("merged_into 不能指向自身")
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                first, second = sorted((object_id, merged_into))
                cur.execute(f"SELECT object_id, object_type, status FROM {db}.ontology_object "
                            "WHERE object_id IN (%s,%s) ORDER BY object_id FOR UPDATE",
                            (first, second))
                rows = {r[0]: {"object_type": r[1], "status": r[2]} for r in cur.fetchall()}
                src, dst = rows.get(object_id), rows.get(merged_into)
                if src is None or src["status"] != "active":
                    conn.rollback()
                    return False
                if dst is None or dst["status"] != "active":
                    raise ValueError(f"merge 目标 {merged_into} 不存在或非 active")
                if src["object_type"] != dst["object_type"]:
                    raise ValueError(
                        f"跨类型合并拒绝：{src['object_type']} → {dst['object_type']}")
                cur.execute(f"UPDATE {db}.ontology_object SET status='merged', merged_into=%s "
                            "WHERE object_id=%s", (merged_into, object_id))
                # source 的 active 别名同事务改指 target（旧行 supersede，新行 active）
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE target_object_id=%s AND status='active' FOR UPDATE",
                            (object_id,))
                for old in self._rows_to_dicts(cur):
                    new_id = new_ulid()
                    cur.execute(f"UPDATE {db}.ontology_identifier "
                                "SET status='superseded', superseded_by=%s "
                                "WHERE identifier_id=%s", (new_id, old["identifier_id"]))
                    cur.execute(
                        f"INSERT INTO {db}.ontology_identifier "
                        "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                        " target_revision, relation, resolution_method, confidence, status, "
                        " source_case_id, confirmed_by, confirmed_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',1.000,'active',%s,%s,NOW(3))",
                        (new_id, old["namespace"], old["raw_value"], old["norm_value"],
                         merged_into, old["target_revision"], old["relation"],
                         old["source_case_id"], by or old.get("confirmed_by")))
                cur.execute(f"UPDATE {db}.ontology_link SET status='retired' "
                            "WHERE (src_object_id=%s OR dst_object_id=%s) AND status='active'",
                            (object_id, object_id))
                self._audit_in_tx(cur, audit)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 别名（已确认映射；候选一律走 case，S2）──────────────────────────────
    def get_active_identifier(self, namespace: str, norm_value: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE namespace=%s AND norm_value=%s AND status='active'",
                            (namespace, norm_value))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def get_identifier(self, identifier_id: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE identifier_id=%s", (identifier_id,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def _lock_active_target(self, cur, target_object_id: str, *, what: str = "别名") -> None:
        """C1（复核批次3，写工具安全线）：别名写路径**事务内**锁定目标对象并断言 active。

        防「confirm/repoint/insert 与 mark_duplicate/retire 并发交错 → 别名指向
        merged/retired 对象」：FK 只保证存在性，不保证生命周期状态。FOR UPDATE 与
        mark_duplicate 同款模式——并发的 merge 持锁时本事务在此排队，锁到手后状态
        已是终值，非 active 即整体回滚。不存在/非 active 均抛 ValueError。"""
        db = self._db()
        cur.execute(f"SELECT status FROM {db}.ontology_object "
                    "WHERE object_id=%s FOR UPDATE", (target_object_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"target 对象 {target_object_id} 不存在（FK 拦截）")
        if row[0] != "active":
            raise ValueError(
                f"target 对象 {target_object_id} 非 active（{row[0]}），拒绝{what}写")

    def insert_identifier(self, namespace: str, raw_value: str, norm_value: str,
                          target_object_id: str, *, method: str, relation: str = "alias",
                          target_revision: Optional[str] = None, confidence: float = 1.0,
                          confirmed_by: Optional[str] = None,
                          source_case_id: Optional[str] = None,
                          approval_request_id: Optional[str] = None,
                          _caller: Optional[str] = None) -> str:
        """铸一条 active 别名（持久化确认——仅播种/回填/工作台/受治理 Action 四路径可达，S1）。
        P1-9：裸插（不闭 case、不带审计）须显式 _caller；治理路径请走
        confirm_case_with_identifier / insert_identifier_closing_case。"""
        _check_caller(_caller, "insert_identifier")
        _check_confidence(confidence)
        db, conn = self._db(), self._conn()
        _begin(conn)
        identifier_id = new_ulid()
        try:
            with conn.cursor() as cur:
                self._lock_active_target(cur, target_object_id)          # C1
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " target_revision, relation, resolution_method, confidence, status, "
                    " source_case_id, confirmed_by, approval_request_id, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,NOW(3))",
                    (identifier_id, namespace, raw_value, norm_value, target_object_id,
                     target_revision, relation, method, confidence,
                     source_case_id, confirmed_by, approval_request_id))
            conn.commit()
            return identifier_id
        except Exception as e:
            conn.rollback()
            if self._is_dup(e):
                raise DuplicateActiveIdentifier(f"{namespace}:{norm_value} 已有 active 别名")
            if self._is_fk_violation(e):
                raise ValueError(f"target 对象 {target_object_id} 不存在（FK 拦截）")
            raise
        finally:
            conn.close()

    def deactivate_identifier(self, identifier_id: str, *, status: str = "rejected",
                              audit: Optional[Dict[str, Any]] = None) -> bool:
        """S3：active→rejected|superseded（CAS）。误配纠正的第一动作。"""
        if status not in _IDENTIFIER_END_STATES:
            raise ValueError(f"非法终态 {status!r}（合法：{_IDENTIFIER_END_STATES}）")
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {db}.ontology_identifier SET status=%s "
                            "WHERE identifier_id=%s AND status='active'", (status, identifier_id))
                ok = cur.rowcount == 1
                if ok:
                    self._audit_in_tx(cur, audit)
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def repoint_identifier(self, identifier_id: str, new_target_object_id: str, *,
                           by: str, new_target_revision: Optional[str] = None,
                           audit: Optional[Dict[str, Any]] = None) -> str:
        """S3 原子改指：同一事务内 旧行 active→superseded(+superseded_by) + 插新 active 行。
        返回新行 identifier_id；旧行非 active → ValueError（先查明现状再纠错）。"""
        db, conn = self._db(), self._conn()
        _begin(conn)
        new_id = new_ulid()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_identifier "
                            "WHERE identifier_id=%s FOR UPDATE", (identifier_id,))
                rows = self._rows_to_dicts(cur)
                if not rows or rows[0]["status"] != "active":
                    raise ValueError(f"identifier {identifier_id} 不存在或非 active，无法改指")
                old = rows[0]
                self._lock_active_target(cur, new_target_object_id, what="改指")   # C1：新 target 须 active
                cur.execute(f"UPDATE {db}.ontology_identifier "
                            "SET status='superseded', superseded_by=%s "
                            "WHERE identifier_id=%s AND status='active'", (new_id, identifier_id))
                if cur.rowcount != 1:   # 并发方先动了手：让位
                    raise ValueError(f"identifier {identifier_id} 已被并发处置")
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " target_revision, relation, resolution_method, confidence, status, "
                    " source_case_id, confirmed_by, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',1.000,'active',%s,%s,NOW(3))",
                    (new_id, old["namespace"], old["raw_value"], old["norm_value"],
                     new_target_object_id, new_target_revision, old["relation"],
                     old["source_case_id"], by))
                self._audit_in_tx(cur, audit)
            conn.commit()
            return new_id
        except Exception as e:
            conn.rollback()
            if self._is_dup(e):   # 理论不可达（旧 active 刚被本事务 supersede）；防御留位
                raise DuplicateActiveIdentifier(
                    f"{identifier_id} 改指撞 active 唯一键（并发插入）")
            raise
        finally:
            conn.close()

    # ── 原子复合写（PR-C，P0-05：多步写一个事务，崩溃/竞态不留半状态）────────
    def mint_object_with_alias(self, object_type: str, title: str, *, owner_dept: str,
                               namespace: str, raw_value: str, norm_value: str,
                               golden: Optional[Dict[str, Any]] = None,
                               provenance: Optional[Dict[str, Any]] = None,
                               data_classification: str = "internal",
                               source_of_record: str = "ontology",
                               lifecycle_state: str = "draft",
                               method: str = "seed", relation: str = "canonical",
                               confidence: float = 1.0, confirmed_by: Optional[str] = None,
                               close_open_case: bool = True,
                               case_note: Optional[str] = None,
                               audit: Optional[Dict[str, Any]] = None,
                               allow_missing_provenance: bool = False) -> Dict[str, Any]:
        """铸对象 + 首别名（+顺手闭同 (ns,norm) 的 open case）**一个事务**。

        别名撞 uk → 整体回滚（对象不留），抛 DuplicateActiveIdentifier——P0-05 的
        「铸完对象别名被并发占 → 永久孤儿」由此根除。
        P1-10：golden 非空须随属性级 provenance（fail-closed，同 mint_object）。
        C1 说明：本方法的别名 target=同事务内刚 INSERT 的新对象（天然 active、外界
        不可见），无并发改状态窗口——五个别名写路径中唯一不需要 _lock_active_target 的。"""
        _check_owner_dept(owner_dept)
        _check_lifecycle(lifecycle_state)
        _check_confidence(confidence)
        _check_golden_provenance(set(golden or {}), provenance,
                                 allow_missing=allow_missing_provenance,
                                 where="mint_object_with_alias")
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT type_code FROM {db}.ontology_ref_seq "
                            "WHERE object_type=%s", (object_type,))
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"object_type {object_type!r} 未在 ontology_ref_seq 登记类型码")
                type_code = row[0]
                cur.execute(f"UPDATE {db}.ontology_ref_seq "
                            "SET next_no=LAST_INSERT_ID(next_no+1) WHERE object_type=%s",
                            (object_type,))
                cur.execute("SELECT LAST_INSERT_ID()")
                seq_no = int(cur.fetchone()[0])
                object_id = new_ulid()
                canonical_ref = format_ref(type_code, seq_no)
                cur.execute(
                    f"INSERT INTO {db}.ontology_object "
                    "(object_id, object_type, canonical_ref, title, normalized_title, "
                    " golden_json, golden_provenance_json, lifecycle_state, "
                    " owner_dept, data_classification, source_of_record) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (object_id, object_type, canonical_ref, title, title_key(title),
                     _dump(golden or {}), _dump(provenance),
                     lifecycle_state, owner_dept, data_classification, source_of_record))
                case_id = None
                if close_open_case:
                    cur.execute(f"SELECT case_id FROM {db}.ontology_resolution_case "
                                "WHERE namespace=%s AND norm_value=%s AND status='open' "
                                "FOR UPDATE", (namespace, norm_value))
                    hit = cur.fetchone()
                    case_id = hit[0] if hit else None
                identifier_id = new_ulid()
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " relation, resolution_method, confidence, status, source_case_id, "
                    " confirmed_by, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,NOW(3))",
                    (identifier_id, namespace, raw_value, norm_value, object_id,
                     relation, method, confidence, case_id, confirmed_by))
                if case_id:
                    cur.execute(
                        f"UPDATE {db}.ontology_resolution_case "
                        "SET status='resolved', resolved_identifier_id=%s, resolved_by=%s, "
                        "    resolved_at=NOW(3), resolution_note=%s "
                        "WHERE case_id=%s AND status='open'",
                        (identifier_id, confirmed_by or "auto",
                         case_note or "铸对象+首别名同事务闭环", case_id))
                self._audit_in_tx(cur, audit)
            conn.commit()
            return {"object_id": object_id, "object_type": object_type,
                    "canonical_ref": canonical_ref, "title": title,
                    "identifier_id": identifier_id, "closed_case_id": case_id}
        except Exception as e:
            conn.rollback()
            if self._is_dup(e):
                raise DuplicateActiveIdentifier(
                    f"{namespace}:{norm_value} 已有 active 别名（mint 整体回滚，无孤儿对象）")
            raise
        finally:
            conn.close()

    def confirm_case_with_identifier(self, case_id: str, *, target_object_id: str,
                                     by: str, note: Optional[str] = None,
                                     relation: str = "alias",
                                     target_revision: Optional[str] = None,
                                     method: str = "manual", confidence: float = 1.0,
                                     audit: Optional[Dict[str, Any]] = None) -> str:
        """工作台确认（P0-05）：case 锁定校验 open + 铸别名 + case→resolved + 审计
        **一个事务**——并发被抢在事务内即失败整体回滚，不再需要"补偿 deactivate"。

        case 不存在/非 open → ValueError；别名撞 uk → DuplicateActiveIdentifier。"""
        _check_confidence(confidence)
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_resolution_case "
                            "WHERE case_id=%s FOR UPDATE", (case_id,))
                rows = self._rows_to_dicts(cur)
                if not rows or rows[0]["status"] != "open":
                    raise ValueError(f"case {case_id} 不存在或非 open（已被并发处置）")
                case = rows[0]
                self._lock_active_target(cur, target_object_id, what="确认")   # C1
                identifier_id = new_ulid()
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " target_revision, relation, resolution_method, confidence, status, "
                    " source_case_id, confirmed_by, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,NOW(3))",
                    (identifier_id, case["namespace"], case["raw_value"], case["norm_value"],
                     target_object_id, target_revision, relation, method, confidence,
                     case_id, by))
                cur.execute(
                    f"UPDATE {db}.ontology_resolution_case "
                    "SET status='resolved', resolved_identifier_id=%s, resolved_by=%s, "
                    "    resolved_at=NOW(3), resolution_note=%s "
                    "WHERE case_id=%s AND status='open'",
                    (identifier_id, by, note, case_id))
                if cur.rowcount != 1:   # FOR UPDATE 后理论不可达；防御
                    raise ValueError(f"case {case_id} 状态竞态，已整体回滚")
                if audit:   # 审计 detail 补铸出的 identifier_id（同事务内已知）
                    audit = {**audit, "detail": {**(audit.get("detail") or {}),
                                                 "identifier_id": identifier_id}}
                self._audit_in_tx(cur, audit)
            conn.commit()
            return identifier_id
        except Exception as e:
            conn.rollback()
            if self._is_dup(e):
                raise DuplicateActiveIdentifier(
                    f"{case_id} 对应编号已有 active 别名（确认整体回滚）")
            if self._is_fk_violation(e):
                raise ValueError(f"target 对象 {target_object_id} 不存在（FK 拦截）")
            raise
        finally:
            conn.close()

    def insert_identifier_closing_case(self, namespace: str, raw_value: str, norm_value: str,
                                       target_object_id: str, *, method: str = "manual",
                                       relation: str = "alias",
                                       target_revision: Optional[str] = None,
                                       confidence: float = 1.0,
                                       confirmed_by: Optional[str] = None,
                                       approval_request_id: Optional[str] = None,
                                       note: Optional[str] = None,
                                       audit: Optional[Dict[str, Any]] = None,
                                       operation_writer=None):
        """受治理 Action 确认（P0-05/06）：铸别名 +（若有）闭同 (ns,norm) open case
        **一个事务**——正式 identifier 与治理 case 不再可分叉。返回
        (identifier_id, closed_case_id)。

        operation_writer（PR-3 Stage C 座缝，形态同 Stage B outbox_writer）：
        `(cur, identifier_id, closed_case_id) -> None`，在同一事务内把操作台账行
        （agent_tool_operation）随副作用一并提交——「台账行存在⇔别名已铸」从此可查证。
        它抛的 OperationAlreadyApplied（台账 PK fencing：另一执行已提交同操作）随
        rollback 原样上抛（args[0] 非 1062，_is_dup 不误译），由工具层按幂等成功收口。"""
        _check_confidence(confidence)
        db, conn = self._db(), self._conn()
        _begin(conn)
        _committing = False
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT case_id FROM {db}.ontology_resolution_case "
                            "WHERE namespace=%s AND norm_value=%s AND status='open' "
                            "FOR UPDATE", (namespace, norm_value))
                hit = cur.fetchone()
                case_id = hit[0] if hit else None
                # C1：锁序统一「case → object」（与 confirm_case_with_identifier 同序防死锁）
                self._lock_active_target(cur, target_object_id, what="受治理确认")
                identifier_id = new_ulid()
                cur.execute(
                    f"INSERT INTO {db}.ontology_identifier "
                    "(identifier_id, namespace, raw_value, norm_value, target_object_id, "
                    " target_revision, relation, resolution_method, confidence, status, "
                    " source_case_id, confirmed_by, approval_request_id, confirmed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,NOW(3))",
                    (identifier_id, namespace, raw_value, norm_value, target_object_id,
                     target_revision, relation, method, confidence, case_id,
                     confirmed_by, approval_request_id))
                if case_id:
                    cur.execute(
                        f"UPDATE {db}.ontology_resolution_case "
                        "SET status='resolved', resolved_identifier_id=%s, resolved_by=%s, "
                        "    resolved_at=NOW(3), resolution_note=%s "
                        "WHERE case_id=%s AND status='open'",
                        (identifier_id, confirmed_by or "-",
                         note or "经受治理动作确认", case_id))
                self._audit_in_tx(cur, audit)
                if operation_writer is not None:
                    operation_writer(cur, identifier_id, case_id)
            _committing = True
            conn.commit()
            return identifier_id, case_id
        except Exception as e:
            if _committing:
                # α1（M3.1）：commit 段异常=结果不可知。rollback 仅 best-effort（连接
                # 多半已坏，且若服务端已提交它也无可回滚），绝不掩盖原异常、绝不落进
                # 下方「确定失败」分类——上抛 CommitAmbiguous 由工具层按台账三态收口。
                try:
                    conn.rollback()
                except Exception:   # noqa: BLE001 — best-effort
                    pass
                raise CommitAmbiguous(
                    f"identifier 铸号事务 commit 结果不可知（{namespace}:{norm_value}）") from e
            conn.rollback()
            if self._is_dup(e):
                raise DuplicateActiveIdentifier(f"{namespace}:{norm_value} 已有 active 别名")
            if self._is_fk_violation(e):
                raise ValueError(f"target 对象 {target_object_id} 不存在（FK 拦截）")
            raise
        finally:
            conn.close()

    def list_identifiers_for_target(self, target_object_id: str,
                                    status: Optional[str] = None) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                sql = f"SELECT * FROM {db}.ontology_identifier WHERE target_object_id=%s"
                params: List[Any] = [target_object_id]
                if status:
                    sql += " AND status=%s"
                    params.append(status)
                cur.execute(sql + " ORDER BY first_seen_at", params)
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    # ── 关系（029 link）与 sem 投影（030；P0 PR10）────────────────────────────
    def add_link(self, src_object_id: str, dst_object_id: str, link_type: str, *,
                 attrs: Optional[Dict[str, Any]] = None,
                 _caller: Optional[str] = None) -> str:
        """建关系（src --type--> dst）。P1-8 语义校验（双后端同契约）：
        ① 端点存在且 active 且类型匹配 LINK_TYPE_SPECS（FOR UPDATE 锁端点行，与并发
           retire/merge 串行化）；
        ② single 基数：同 src 同型已有指向**其他** dst 的 active 行 → 抛
           LinkCardinalityViolation（DB 级兜底=schema/033 生成列唯一键 uk_link_active_single，
           服务层被绕过/并发窄窗时 1062 同样拦）。
        幂等：同 (src,dst,type) 已有行（含 retired）→ 返回现行 link_id，不复活不覆盖
        （retire/复活语义 P2 再议）。P1-9：须显式 _caller。"""
        _check_link_type(link_type)
        _check_caller(_caller, "add_link")
        if src_object_id == dst_object_id:
            raise ValueError("link 端点不得自指")
        spec = LINK_TYPE_SPECS[link_type]
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            link_id = new_ulid()
            dup = False
            with conn.cursor() as cur:
                cur.execute(f"SELECT object_id, object_type, status FROM {db}.ontology_object "
                            "WHERE object_id IN (%s,%s) ORDER BY object_id FOR UPDATE",
                            (src_object_id, dst_object_id))
                rows = {r[0]: {"object_id": r[0], "object_type": r[1], "status": r[2]}
                        for r in cur.fetchall()}
                _check_link_endpoints(link_type, rows.get(src_object_id),
                                      rows.get(dst_object_id))
                if spec["src_cardinality"] == "single":
                    cur.execute(f"SELECT link_id, dst_object_id FROM {db}.ontology_link "
                                "WHERE src_object_id=%s AND link_type=%s AND status='active' "
                                "FOR UPDATE", (src_object_id, link_type))
                    hit = cur.fetchone()
                    if hit is not None:
                        if hit[1] == dst_object_id:   # 同关系重建=幂等
                            conn.rollback()
                            return hit[0]
                        raise LinkCardinalityViolation(
                            f"{link_type} 为 single 基数：src {src_object_id} 已有 active "
                            f"关系指向 {hit[1]}——先 retire 旧关系再建新（S3 纠错），拒绝双活")
                try:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_link "
                        "(link_id, src_object_id, dst_object_id, link_type, attrs_json) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (link_id, src_object_id, dst_object_id, link_type, _dump(attrs)))
                except Exception as e:
                    if self._is_fk_violation(e):
                        raise ValueError("link 端点对象不存在（FK 拦截）")
                    if not self._is_dup(e):
                        raise
                    dup = True
            if not dup:
                conn.commit()
                return link_id
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT link_id FROM {db}.ontology_link "
                    "WHERE src_object_id=%s AND dst_object_id=%s AND link_type=%s",
                    (src_object_id, dst_object_id, link_type))
                row = cur.fetchone()
            if row is not None:
                return row[0]
            # 无同三元组行而 INSERT 仍 1062 ⇒ 撞的是 033 uk_link_active_single：
            # 并发方抢先占了同 (src,type) 的 active single 位——按基数违例上抛
            raise LinkCardinalityViolation(
                f"{link_type} 为 single 基数：src {src_object_id} 的 active 关系位被并发"
                "占用（DB 生成列唯一键拦截）——先查明现状再纠错")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_links(self, *, src_object_id: Optional[str] = None,
                   dst_object_id: Optional[str] = None, link_type: Optional[str] = None,
                   status: Optional[str] = "active", limit: int = 200) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                sql = f"SELECT * FROM {db}.ontology_link WHERE 1=1"
                params: List[Any] = []
                for col, val in (("src_object_id", src_object_id),
                                 ("dst_object_id", dst_object_id),
                                 ("link_type", link_type), ("status", status)):
                    if val is not None:
                        sql += f" AND {col}=%s"
                        params.append(val)
                cur.execute(sql + " ORDER BY created_at LIMIT %s",
                            (*params, max(1, min(int(limit), 500))))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def get_object_by_ref(self, canonical_ref: str) -> Optional[Dict[str, Any]]:
        """展示号（FLP-<码>-<序号>）查对象。uk 在 (object_type, canonical_ref)，
        但类型码内嵌于展示号 → 全局唯一，按 ref 单列等值即可。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_object "
                            "WHERE canonical_ref=%s LIMIT 1", (canonical_ref,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    # 探测缓存跨实例共享（qa_facts 式：flag/调用先于 030 apply 时优雅回退，不硬崩）
    _sem_probe: Dict[str, tuple] = {}
    _sem_probe_lock = threading.Lock()

    def _sem_view_ready(self, view: str) -> bool:
        now = time.time()
        with self._sem_probe_lock:
            hit = self._sem_probe.get(view)
            if hit and now - hit[0] < _SEM_PROBE_TTL_S:
                return hit[1]
        ok = True
        try:
            conn = self._conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM {self._db()}.{view} LIMIT 1")
                    cur.fetchall()
            finally:
                conn.close()
        except Exception:   # noqa: BLE001
            ok = False
            logger.warning("sem 视图 %s 探测失败（schema/030 未 apply？本窗口回退内联投影）", view)
        with self._sem_probe_lock:
            self._sem_probe[view] = (now, ok)
        return ok

    @staticmethod
    def _inline_fallback_allowed() -> bool:
        """PR-G（P1「sem 静默 inline fallback」收口）：内联回退**仅限本地 host**——
        共享 staging/prod 上视图缺失=schema drift 或授权失败，静默回退会把事故
        掩成"能查"，必须 fail-closed 阻断并把 030 未 apply 的事实暴露出来。
        判定用物理 host（与 apply_migration.classify_target 同纪律），不信自报标签。"""
        from opensearch_pipeline.config import _LOCAL_HOSTS, get_config
        return get_config().rds.host in _LOCAL_HOSTS

    def sem_spec_rows(self, sku_object_id: str, kind: str) -> List[Dict[str, Any]]:
        """PMC-1 投影行（kind ∈ SEM_PROJECTIONS）。视图在位走视图；未 apply/查询失败
        的内联同形 JOIN 回退**仅限本地开发**（共享环境 fail-closed 抛错——PR-G）。
        **本方法不做 ACL——行过滤是 sem.py 的职责。**"""
        proj = SEM_PROJECTIONS[kind]
        db = self._db()
        if self._sem_view_ready(proj["view"]):
            try:
                conn = self._conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT * FROM {db}.{proj['view']} WHERE sku_id=%s",
                                    (sku_object_id,))
                        return self._rows_to_dicts(cur)
                finally:
                    conn.close()
            except Exception:   # noqa: BLE001 — 视图刚被删等窄窗口：本地降级，共享环境下沉到闸
                logger.warning("sem 视图查询失败，尝试内联投影回退", exc_info=True)
        if not self._inline_fallback_allowed():
            logger.error("sem 视图 %s 不可用且目标非本地库——fail-closed 阻断"
                         "（schema/030 未 apply 或视图授权失败，须先修复，不静默回退）",
                         proj["view"])
            raise RuntimeError(
                f"sem 视图 {proj['view']} 不可用（共享环境禁止内联回退——"
                "请先 apply schema/030 或排查视图授权）")
        json_cols = ", ".join(
            f"JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.{f}')) AS {f}"
            for f in proj["fields"])
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT s.object_id AS sku_id, s.canonical_ref AS sku_ref, "
                    f"s.title AS sku_title, s.owner_dept AS sku_owner_dept, "
                    f"p.object_id AS product_id, p.canonical_ref AS product_ref, "
                    f"p.title AS product_name, "
                    f"p.owner_dept AS product_owner_dept, "
                    f"p.data_classification AS product_classification, "
                    f"spec.object_id AS spec_id, "
                    f"spec.canonical_ref AS spec_ref, {json_cols}, "
                    f"spec.golden_json AS spec_json, spec.lifecycle_state AS spec_state, "
                    f"spec.owner_dept AS owner_dept, "
                    f"spec.data_classification AS data_classification, "
                    f"spec.updated_at AS spec_updated_at "
                    f"FROM {db}.ontology_object s "
                    f"JOIN {db}.ontology_link lk ON lk.dst_object_id=s.object_id "
                    f"AND lk.link_type=%s AND lk.status='active' "
                    f"JOIN {db}.ontology_object spec ON spec.object_id=lk.src_object_id "
                    f"AND spec.object_type=%s AND spec.status='active' "
                    f"LEFT JOIN {db}.ontology_link lp ON lp.src_object_id=s.object_id "
                    f"AND lp.link_type='sku_of_product' AND lp.status='active' "
                    f"LEFT JOIN {db}.ontology_object p ON p.object_id=lp.dst_object_id "
                    f"AND p.status='active' "
                    f"WHERE s.object_type='sku' AND s.status='active' AND s.object_id=%s",
                    (proj["link_type"], proj["spec_type"], sku_object_id))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    # ── 消解 case / 候选（S2 承载层）────────────────────────────────────────
    def upsert_case(self, namespace: str, raw_value: str, norm_value: str, *,
                    object_type_hint: Optional[str] = None,
                    evidence: Optional[Dict[str, Any]] = None) -> str:
        """观测聚合：已有 open case → seen_count+1 + last_seen（evidence 保首次快照不覆盖）；
        无 → 新建。刻意不用 SELECT … FOR UPDATE（空行取间隙锁，并发首插会 1213 死锁）——
        乐观路径 + 撞 uk_ns_norm_open(1062) 回退聚合。"""
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT case_id FROM {db}.ontology_resolution_case "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                row = cur.fetchone()
                if row:
                    cur.execute(f"UPDATE {db}.ontology_resolution_case "
                                "SET seen_count=seen_count+1, last_seen_at=NOW(3) "
                                "WHERE case_id=%s AND status='open'", (row[0],))
                    if cur.rowcount == 1:
                        conn.commit()
                        return row[0]
                    # 窄竞态：查到后刚被处置 → 落到新建路径
                case_id = new_ulid()
                dup = False
                try:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_resolution_case "
                        "(case_id, namespace, raw_value, norm_value, object_type_hint, "
                        " status, evidence_json) VALUES (%s,%s,%s,%s,%s,'open',%s)",
                        (case_id, namespace, raw_value, norm_value, object_type_hint,
                         _dump(evidence)))
                except Exception as e:
                    if not self._is_dup(e):
                        raise
                    dup = True   # 并发首插输家：改走聚合
                if not dup:
                    conn.commit()
                    return case_id
            conn.rollback()
            _begin(conn)   # commit/rollback 复位 _transaction 位——第二轮聚合事务重新钉
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {db}.ontology_resolution_case "
                            "SET seen_count=seen_count+1, last_seen_at=NOW(3) "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                cur.execute(f"SELECT case_id FROM {db}.ontology_resolution_case "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                row2 = cur.fetchone()
            conn.commit()
            if row2 is None:   # 赢家已被处置：极窄窗口，交回调用方重试
                raise RuntimeError(f"case ({namespace},{norm_value}) 竞态后不可见，请重试")
            return row2[0]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_candidate(self, case_id: str, target_object_id: str, *, method: str,
                      confidence: float, target_revision: Optional[str] = None,
                      features: Optional[Dict[str, Any]] = None) -> str:
        """候选幂等：同 (case, target, method) 重复提出 → 置信取大、依据取新。返回现行 candidate_id。"""
        _check_confidence(confidence)
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.ontology_resolution_candidate "
                    "(candidate_id, case_id, target_object_id, target_revision, method, "
                    " confidence, features_json) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE "
                    "confidence=GREATEST(confidence, VALUES(confidence)), "
                    "features_json=COALESCE(VALUES(features_json), features_json)",
                    (new_ulid(), case_id, target_object_id, target_revision, method,
                     confidence, _dump(features)))
                cur.execute(
                    f"SELECT candidate_id FROM {db}.ontology_resolution_candidate "
                    "WHERE case_id=%s AND target_object_id=%s AND method=%s",
                    (case_id, target_object_id, method))
                candidate_id = cur.fetchone()[0]
            conn.commit()
            return candidate_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_resolution_case "
                            "WHERE case_id=%s", (case_id,))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def get_open_case(self, namespace: str, norm_value: str) -> Optional[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_resolution_case "
                            "WHERE namespace=%s AND norm_value=%s AND status='open'",
                            (namespace, norm_value))
                rows = self._rows_to_dicts(cur)
            return rows[0] if rows else None
        finally:
            conn.close()

    def list_open_cases(self, *, namespace: Optional[str] = None,
                        object_type_hint: Optional[str] = None, order: str = "freq",
                        limit: int = 50, offset: int = 0,
                        scope_filter: Optional[Dict[str, List[str]]] = None,
                        cursor: Optional[Dict[str, Any]] = None
                        ) -> List[Dict[str, Any]]:
        """工作台队列：order=freq（高频未解析优先）| recent。

        scope_filter（PR-B，P0-01 队列 SQL 层过滤）：None=不过滤（kb_admin）；
        dict{namespaces, namespace_prefixes, object_types}=按管辖 scope 并集收窄
        （跨部门行不出库）；三集合全空 → 恒空（fail-closed：什么都不管=什么都看不见）。

        cursor（PR-I，P2「单条 offset 队列」）：keyset 分页——offset 在边处置边翻页时
        会跳行/重行，大积压处置必须用游标。freq 序游标=(seen_count, last_seen_at,
        case_id)，recent 序=(last_seen_at, case_id)；case_id 终极去平局，排序键含
        case_id DESC 保证游标全序稳定。"""
        db, conn = self._db(), self._conn()
        limit = max(1, min(int(limit), 200))
        order_sql = ("seen_count DESC, last_seen_at DESC, case_id DESC" if order == "freq"
                     else "last_seen_at DESC, case_id DESC")
        try:
            with conn.cursor() as cur:
                sql = f"SELECT * FROM {db}.ontology_resolution_case WHERE status='open'"
                params: List[Any] = []
                if namespace:
                    sql += " AND namespace=%s"
                    params.append(namespace)
                if object_type_hint:
                    sql += " AND object_type_hint=%s"
                    params.append(object_type_hint)
                if cursor:
                    if order == "freq":
                        sql += (" AND (seen_count < %s OR (seen_count = %s AND last_seen_at < %s)"
                                " OR (seen_count = %s AND last_seen_at = %s AND case_id < %s))")
                        params.extend([cursor["seen_count"], cursor["seen_count"],
                                       cursor["last_seen_at"], cursor["seen_count"],
                                       cursor["last_seen_at"], cursor["case_id"]])
                    else:
                        sql += (" AND (last_seen_at < %s"
                                " OR (last_seen_at = %s AND case_id < %s))")
                        params.extend([cursor["last_seen_at"], cursor["last_seen_at"],
                                       cursor["case_id"]])
                if scope_filter is not None:
                    clauses: List[str] = []
                    nss = scope_filter.get("namespaces") or []
                    prefixes = scope_filter.get("namespace_prefixes") or []
                    otypes = scope_filter.get("object_types") or []
                    if nss:
                        clauses.append(
                            "namespace IN (" + ",".join(["%s"] * len(nss)) + ")")
                        params.extend(nss)
                    if prefixes:
                        clauses.append(
                            "SUBSTRING_INDEX(namespace, ':', 1) IN ("
                            + ",".join(["%s"] * len(prefixes)) + ")")
                        params.extend(prefixes)
                    if otypes:
                        clauses.append(
                            "object_type_hint IN (" + ",".join(["%s"] * len(otypes)) + ")")
                        params.extend(otypes)
                    if not clauses:
                        return []   # 管辖空集：SQL 都不必发
                    sql += " AND (" + " OR ".join(clauses) + ")"
                cur.execute(sql + f" ORDER BY {order_sql} LIMIT %s OFFSET %s",
                            (*params, limit, max(0, int(offset))))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def list_candidates(self, case_id: str) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_resolution_candidate "
                            "WHERE case_id=%s ORDER BY confidence DESC", (case_id,))
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def list_candidates_for_cases(
            self, case_ids: List[str]) -> Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        """批量取多 case 的候选 + 目标对象 6 字段切片（perf 批次 A §4.1）：单条 LEFT JOIN
        替代 workbench 逐 case list_candidates + 逐候选 get_object 的 N+1。

        返回 {case_id: [(cand_row, obj_slice), ...]}，每 case 内按 confidence DESC。
        · cand_row 与 list_candidates 逐字段一致（只剔除 JOIN 侧 _obj_* 列，不掺入对象列）。
        · obj_slice 仅 6 字段 {canonical_ref,title,object_type,status,owner_dept,
          data_classification}；目标悬空(o.object_id IS NULL)→ {}（复刻 `get_object() or {}`
          的 can_read_object({}) fail-closed 语义）。RDS FK 保证不悬空，仅 Memory 测试可造。
        空 case_ids → {}（绝不发 `IN ()`）。无候选的 case 不出现在返回 dict。"""
        ids = list(dict.fromkeys(case_ids or []))   # 去重保序；空 → 不查库
        if not ids:
            return {}
        db, conn = self._db(), self._conn()
        try:
            placeholders = ",".join(["%s"] * len(ids))
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.candidate_id, c.case_id, c.target_object_id, c.target_revision, "
                    "c.method, c.confidence, c.features_json, c.created_at, "
                    "o.object_id AS _obj_present, o.canonical_ref AS _obj_canonical_ref, "
                    "o.title AS _obj_title, o.object_type AS _obj_object_type, "
                    "o.status AS _obj_status, o.owner_dept AS _obj_owner_dept, "
                    "o.data_classification AS _obj_data_classification "
                    f"FROM {db}.ontology_resolution_candidate c "
                    f"LEFT JOIN {db}.ontology_object o ON o.object_id = c.target_object_id "
                    f"WHERE c.case_id IN ({placeholders}) "
                    "ORDER BY c.case_id, c.confidence DESC",
                    tuple(ids))
                rows = self._rows_to_dicts(cur)
        finally:
            conn.close()
        grouped: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
        for r in rows:
            cand = {k: v for k, v in r.items() if not k.startswith("_obj_")}
            obj: Dict[str, Any] = {} if r.get("_obj_present") is None else {
                "canonical_ref": r["_obj_canonical_ref"], "title": r["_obj_title"],
                "object_type": r["_obj_object_type"], "status": r["_obj_status"],
                "owner_dept": r["_obj_owner_dept"],
                "data_classification": r["_obj_data_classification"]}
            grouped.setdefault(r["case_id"], []).append((cand, obj))
        return grouped

    def resolve_case(self, case_id: str, *, identifier_id: str, by: str,
                     note: Optional[str] = None,
                     audit: Optional[Dict[str, Any]] = None) -> bool:
        return self._case_transition(case_id, to_status="resolved", by=by, note=note,
                                     identifier_id=identifier_id, audit=audit)

    def dismiss_case(self, case_id: str, *, by: str, note: str,
                     audit: Optional[Dict[str, Any]] = None) -> bool:
        """驳回/终止必须给理由（工作台契约）——空 note 直接 ValueError。"""
        if not (note or "").strip():
            raise ValueError("dismiss 必须给 resolution_note（处置理由）")
        return self._case_transition(case_id, to_status="dismissed", by=by, note=note,
                                     audit=audit)

    def _case_transition(self, case_id: str, *, to_status: str, by: str,
                         note: Optional[str], identifier_id: Optional[str] = None,
                         audit: Optional[Dict[str, Any]] = None) -> bool:
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.ontology_resolution_case "
                    "SET status=%s, resolved_identifier_id=%s, resolved_by=%s, "
                    "    resolved_at=NOW(3), resolution_note=%s "
                    "WHERE case_id=%s AND status='open'",
                    (to_status, identifier_id, by, note, case_id))
                ok = cur.rowcount == 1
                if ok:
                    self._audit_in_tx(cur, audit)
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 溯源目录 / stewardship（种子经 upsert，代码即声明）──────────────────
    def upsert_attribute_sources(self, rows: List[Dict[str, Any]]) -> int:
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_attribute_source "
                        "(object_type, attribute, sor_system, sync_mode, freshness, notes) "
                        "VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE sor_system=VALUES(sor_system), "
                        "sync_mode=VALUES(sync_mode), freshness=VALUES(freshness), "
                        "notes=VALUES(notes)",
                        (r["object_type"], r["attribute"], r["sor_system"], r["sync_mode"],
                         r.get("freshness"), r.get("notes")))
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_attribute_sources(self) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_attribute_source "
                            "ORDER BY object_type, attribute")
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def upsert_stewardship(self, rows: List[Dict[str, Any]]) -> int:
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        f"INSERT INTO {db}.ontology_stewardship "
                        "(scope_type, scope_key, steward_dept, backup_dept, notes) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE steward_dept=VALUES(steward_dept), "
                        "backup_dept=VALUES(backup_dept), notes=VALUES(notes)",
                        (r["scope_type"], r["scope_key"], r["steward_dept"],
                         r.get("backup_dept"), r.get("notes")))
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_stewardship(self) -> List[Dict[str, Any]]:
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {db}.ontology_stewardship "
                            "ORDER BY scope_type, scope_key")
                return self._rows_to_dicts(cur)
        finally:
            conn.close()

    def delete_stewardship(self, scope_type: str, scope_key: str) -> bool:
        """撤销一条 scope（PR-G desired-state：代码声明移除=显式撤权，不再永久残留）。"""
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {db}.ontology_stewardship "
                            "WHERE scope_type=%s AND scope_key=%s", (scope_type, scope_key))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 覆盖率 / 积压统计 ────────────────────────────────────────────────────
    def record_population_snapshot(self, namespace_counts: Dict[str, int], *,
                                   source: str = "seeding") -> int:
        """PR-F：源快照分母登记（seeding/backfill 真跑时按 namespace upsert）。"""
        db, conn = self._db(), self._conn()
        _begin(conn)
        try:
            with conn.cursor() as cur:
                for ns, n in namespace_counts.items():
                    cur.execute(
                        f"INSERT INTO {db}.ontology_source_population "
                        "(namespace, records, source, snapshot_at) VALUES (%s,%s,%s,NOW(3)) "
                        "ON DUPLICATE KEY UPDATE records=VALUES(records), "
                        "source=VALUES(source), snapshot_at=NOW(3)",
                        (ns, int(n), source[:64]))
            conn.commit()
            return len(namespace_counts)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def population_total(self) -> Optional[int]:
        """最近源快照的总记录数；无快照 → None（coverage 回退 approx 口径）。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*), COALESCE(SUM(records),0) "
                            f"FROM {db}.ontology_source_population")
                n_rows, total = cur.fetchone()
            return int(total) if int(n_rows or 0) else None
        except Exception:   # noqa: BLE001 — 表未建（旧库）→ 回退 approx，不硬崩
            return None
        finally:
            conn.close()

    def coverage(self, object_type: Optional[str] = None) -> Dict[str, Any]:
        """覆盖率双口径（PR-F）：有源快照 → population 分母（真实覆盖率）；
        无 → active/(active+open) 近似并明标 denominator='approx'（处置率≠覆盖率）。
        auto_active 单列 = auto 通道确认数（批次3a 口径诚实化：抽检复核数据模型未建，
        它不是「抽检队列规模」；manual_review_rate=1-auto/active 只是**非 auto 来源
        占比**、不是审核完成率——真实抽检闭环待批次3b）。"""
        db, conn = self._db(), self._conn()
        try:
            with conn.cursor() as cur:
                if object_type:
                    cur.execute(
                        f"SELECT COUNT(*), SUM(i.confirmed_by='auto') "
                        f"FROM {db}.ontology_identifier i "
                        f"JOIN {db}.ontology_object o ON o.object_id=i.target_object_id "
                        "WHERE i.status='active' AND o.object_type=%s", (object_type,))
                else:
                    cur.execute(
                        f"SELECT COUNT(*), SUM(confirmed_by='auto') "
                        f"FROM {db}.ontology_identifier WHERE status='active'")
                active, auto_active = cur.fetchone()
                active, auto_active = int(active or 0), int(auto_active or 0)
                sql = (f"SELECT status, COUNT(*) FROM {db}.ontology_resolution_case "
                       "WHERE 1=1")
                params: List[Any] = []
                if object_type:
                    sql += " AND object_type_hint=%s"
                    params.append(object_type)
                cur.execute(sql + " GROUP BY status", params)
                by_status = {row[0]: int(row[1]) for row in cur.fetchall()}
            open_n = by_status.get("open", 0)
        finally:
            conn.close()
        return self._coverage_payload(active, auto_active, open_n,
                                      by_status.get("resolved", 0),
                                      by_status.get("dismissed", 0),
                                      population=(None if object_type
                                                  else self.population_total()))

    @staticmethod
    def _coverage_payload(active, auto_active, open_n, resolved, dismissed, population):
        approx_denom = active + open_n
        out = {
            "active_identifiers": active,
            "auto_active": auto_active,
            "open_cases": open_n,
            "resolved_cases": resolved,
            "dismissed_cases": dismissed,
            "anomaly": None,   # P1-12：超分母异常显式标注，不再 min() 掩盖
        }
        if population:
            out["denominator"] = "population"
            out["population_records"] = population
            cov = active / population
            out["resolution_coverage"] = cov        # 不截顶：>1.0 原样透出 + anomaly
            if cov > 1.0:
                out["anomaly"] = "coverage_exceeds_population"
                logger.warning(
                    "coverage 超分母：active=%s > population=%s（快照陈旧/源收缩/重复激活）"
                    "——须重跑全量源快照校准分母；此前 min(1.0,·) 会把该异常掩成 100%%",
                    active, population)
        else:
            out["denominator"] = "approx"           # 处置率口径，UI 须明示非真实覆盖率
            out["population_records"] = None
            out["resolution_coverage"] = (active / approx_denom) if approx_denom else None
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Memory 后端（单测/SIM 注入；语义与 RDS 逐条对齐，由同一套契约测试钉住）
# ══════════════════════════════════════════════════════════════════════════════
class MemoryOntologyStore:
    _SEED_TYPES = (("product", "P"), ("sku", "S"), ("mold", "M"),
                   ("material", "MT"), ("calc_rule", "CR"),
                   ("packing_spec", "PS"), ("stacking_spec", "SS"))   # 后两类=030 登记

    def __init__(self):
        self._lock = threading.RLock()
        self._objects: Dict[str, Dict[str, Any]] = {}
        self._identifiers: Dict[str, Dict[str, Any]] = {}
        self._cases: Dict[str, Dict[str, Any]] = {}
        self._candidates: Dict[str, Dict[str, Any]] = {}
        self._links: Dict[str, Dict[str, Any]] = {}
        self._attr_sources: Dict[tuple, Dict[str, Any]] = {}
        self._stewardship: Dict[tuple, Dict[str, Any]] = {}
        self._seq: Dict[str, list] = {t: [code, 0] for t, code in self._SEED_TYPES}
        self._clock = 0   # 单调伪时钟：排序用（first/last_seen）
        # PR-C：同事务审计的 Memory 形态——audit_rows 收集、audit_hook 供测试注入失败
        # （hook 抛异常 = 模拟 agent_audit_log 不可写 → 方法必须零副作用地上抛）
        self.audit_rows: List[Dict[str, Any]] = []
        self.audit_hook = None
        self._population: Dict[str, Dict[str, Any]] = {}   # PR-F：源快照分母

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def _memory_audit(self, audit) -> None:
        """与 RDS._audit_in_tx 同语义：audit 写失败（hook 抛）→ 调用方在变更前上抛，
        零副作用。调用点约定：**先审计后变更**（锁内），等价于事务原子性。"""
        if not audit:
            return
        if self.audit_hook is not None:
            self.audit_hook(audit)
        self.audit_rows.append(dict(audit))

    # ── 对象 ────────────────────────────────────────────────────────────────
    def mint_object(self, object_type, title, *, owner_dept, golden=None, provenance=None,
                    data_classification="internal", source_of_record="ontology",
                    lifecycle_state="draft", allow_missing_provenance=False, _caller=None):
        _check_caller(_caller, "mint_object")
        _check_owner_dept(owner_dept)
        _check_lifecycle(lifecycle_state)
        _check_golden_provenance(set(golden or {}), provenance,
                                 allow_missing=allow_missing_provenance, where="mint_object")
        with self._lock:
            if object_type not in self._seq:
                raise ValueError(f"object_type {object_type!r} 未在 ontology_ref_seq 登记类型码")
            self._seq[object_type][1] += 1
            code, seq_no = self._seq[object_type]
            object_id = new_ulid()
            row = {"object_id": object_id, "object_type": object_type,
                   "canonical_ref": format_ref(code, seq_no), "title": title,
                   "normalized_title": title_key(title),
                   "golden_json": _dump(golden or {}),
                   "golden_provenance_json": _dump(provenance),
                   "lifecycle_state": lifecycle_state,
                   "owner_dept": owner_dept, "data_classification": data_classification,
                   "source_of_record": source_of_record, "version": 1,
                   "status": "active", "merged_into": None}
            self._objects[object_id] = row
            return {"object_id": object_id, "object_type": object_type,
                    "canonical_ref": row["canonical_ref"], "title": title}

    def get_object(self, object_id):
        with self._lock:
            row = self._objects.get(object_id)
            return dict(row) if row else None

    def find_objects(self, object_type, *, title_like=None, norm_title=None,
                     status="active", limit=50):
        with self._lock:
            out = [dict(o) for o in self._objects.values()
                   if o["object_type"] == object_type and o["status"] == status
                   and (title_like is None or title_like in o["title"])
                   and (norm_title is None
                        or (o.get("normalized_title") or title_key(o["title"])) == norm_title)]
            return sorted(out, key=lambda o: o["canonical_ref"])[:max(1, min(int(limit), 200))]

    def count_objects(self, object_type, *, title_like=None, status="active"):
        with self._lock:
            return sum(1 for o in self._objects.values()
                       if o["object_type"] == object_type and o["status"] == status
                       and (title_like is None or title_like in o["title"]))

    def _authorized_objects(self, object_type, *, acl, bypass_acl, title_like, status):
        """行级判定直接走 authz.can_read_object（与 RDS 的 SQL 谓词同一来源）。"""
        from opensearch_pipeline.ontology.authz import can_read_object
        return [dict(o) for o in self._objects.values()
                if o["object_type"] == object_type and o["status"] == status
                and (title_like is None or title_like in o["title"])
                and can_read_object(o, acl=acl, bypass_acl=bypass_acl)]

    def search_objects_authorized(self, object_type, *, acl, bypass_acl=False,
                                  title_like=None, status="active", limit=50):
        """P0-A②③（与 RDS 同契约）：先 ACL 过滤再排序截断——LIMIT 只作用授权集合。"""
        with self._lock:
            out = self._authorized_objects(object_type, acl=acl, bypass_acl=bypass_acl,
                                           title_like=title_like, status=status)
            return sorted(out, key=lambda o: o["canonical_ref"])[:max(1, min(int(limit), 200))]

    def search_objects_authorized_full(self, object_type, *, acl, bypass_acl=False,
                                       title_like=None, status="active", limit=50):
        """与 RDS search_objects_authorized_full 同契约（perf 批次 A §4.2）：Memory 行本就
        整行（含 golden_json/version/golden_provenance_json），复用同一授权过滤即可。"""
        with self._lock:
            out = self._authorized_objects(object_type, acl=acl, bypass_acl=bypass_acl,
                                           title_like=title_like, status=status)
            return sorted(out, key=lambda o: o["canonical_ref"])[:max(1, min(int(limit), 200))]

    def count_objects_authorized(self, object_type, *, acl, bypass_acl=False,
                                 title_like=None, status="active"):
        with self._lock:
            return len(self._authorized_objects(object_type, acl=acl, bypass_acl=bypass_acl,
                                                title_like=title_like, status=status))

    def update_golden(self, object_id, golden, *, expected_version, provenance=None,
                      allow_missing_provenance=False):
        """P1-10（与 RDS 同契约）：changed 属性强制随溯源、removed 属性剪除溯源行。"""
        with self._lock:
            row = self._objects.get(object_id)
            if not row or row["version"] != expected_version:
                return False
            try:
                old = json.loads(row.get("golden_json") or "{}")
            except Exception:   # noqa: BLE001
                old = {}
            new = dict(golden or {})
            changed = {k for k, v in new.items() if k not in old or old[k] != v}
            removed = set(old) - set(new)
            _check_golden_provenance(changed, provenance,
                                     allow_missing=allow_missing_provenance,
                                     where="update_golden")
            row["golden_json"] = _dump(golden)
            if provenance or removed:
                merged = json.loads(row.get("golden_provenance_json") or "{}")
                for k, v in (provenance or {}).items():   # RFC 7396 同语义：null=删键
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                for k in removed:
                    merged.pop(k, None)
                row["golden_provenance_json"] = _dump(merged)
            row["version"] += 1
            return True

    def retire_object(self, object_id, *, audit=None):
        """P0-03 级联（与 RDS 同契约）：对象→retired + active 别名→retired + 双向 link→retired。"""
        with self._lock:
            row = self._objects.get(object_id)
            if not row or row["status"] != "active":
                return False
            self._memory_audit(audit)
            row["status"] = "retired"
            for ident in self._identifiers.values():
                if ident["target_object_id"] == object_id and ident["status"] == "active":
                    ident["status"] = "retired"
            for lk in self._links.values():
                if lk["status"] == "active" and object_id in (
                        lk["src_object_id"], lk["dst_object_id"]):
                    lk["status"] = "retired"
            return True

    def mark_duplicate(self, object_id, merged_into, *, by=None, audit=None):
        """P0-03 级联（与 RDS 同契约）：同类型/目标 active/防自指闸 + source 别名改指 target
        + source link→retired。"""
        if object_id == merged_into:
            raise ValueError("merged_into 不能指向自身")
        with self._lock:
            src = self._objects.get(object_id)
            if not src or src["status"] != "active":
                return False
            dst = self._objects.get(merged_into)
            if not dst or dst["status"] != "active":
                raise ValueError(f"merge 目标 {merged_into} 不存在或非 active")
            if src["object_type"] != dst["object_type"]:
                raise ValueError(
                    f"跨类型合并拒绝：{src['object_type']} → {dst['object_type']}")
            self._memory_audit(audit)
            src["status"] = "merged"
            src["merged_into"] = merged_into
            for ident in list(self._identifiers.values()):
                if ident["target_object_id"] != object_id or ident["status"] != "active":
                    continue
                new_id = new_ulid()
                ident["status"] = "superseded"
                ident["superseded_by"] = new_id
                self._identifiers[new_id] = {
                    "identifier_id": new_id, "namespace": ident["namespace"],
                    "raw_value": ident["raw_value"], "norm_value": ident["norm_value"],
                    "target_object_id": merged_into,
                    "target_revision": ident["target_revision"],
                    "relation": ident["relation"], "resolution_method": "manual",
                    "confidence": 1.0, "status": "active", "superseded_by": None,
                    "source_case_id": ident["source_case_id"],
                    "confirmed_by": by or ident.get("confirmed_by"),
                    "approval_request_id": None, "first_seen_at": self._tick()}
            for lk in self._links.values():
                if lk["status"] == "active" and object_id in (
                        lk["src_object_id"], lk["dst_object_id"]):
                    lk["status"] = "retired"
            return True

    # ── 别名 ────────────────────────────────────────────────────────────────
    def get_active_identifier(self, namespace, norm_value):
        with self._lock:
            for row in self._identifiers.values():
                if (row["namespace"], row["norm_value"], row["status"]) == \
                        (namespace, norm_value, "active"):
                    return dict(row)
            return None

    def _assert_active_target(self, target_object_id, *, what="别名"):
        """C1（与 RDS _lock_active_target 同语义）：目标对象须存在且 active——
        memory 后端在全局锁内检查即等价于事务内锁定。"""
        obj = self._objects.get(target_object_id)
        if obj is None:
            raise ValueError(f"target 对象 {target_object_id} 不存在（FK 拦截）")
        if obj.get("status") != "active":
            raise ValueError(
                f"target 对象 {target_object_id} 非 active（{obj.get('status')}），拒绝{what}写")

    def insert_identifier(self, namespace, raw_value, norm_value, target_object_id, *,
                          method, relation="alias", target_revision=None, confidence=1.0,
                          confirmed_by=None, source_case_id=None, approval_request_id=None,
                          _caller=None):
        _check_caller(_caller, "insert_identifier")
        _check_confidence(confidence)
        with self._lock:
            if self.get_active_identifier(namespace, norm_value) is not None:
                raise DuplicateActiveIdentifier(f"{namespace}:{norm_value} 已有 active 别名")
            self._assert_active_target(target_object_id)   # C1（repoint 经内部调用同享）
            identifier_id = new_ulid()
            self._identifiers[identifier_id] = {
                "identifier_id": identifier_id, "namespace": namespace,
                "raw_value": raw_value, "norm_value": norm_value,
                "target_object_id": target_object_id, "target_revision": target_revision,
                "relation": relation, "resolution_method": method,
                "confidence": confidence, "status": "active", "superseded_by": None,
                "source_case_id": source_case_id, "confirmed_by": confirmed_by,
                "approval_request_id": approval_request_id, "first_seen_at": self._tick()}
            return identifier_id

    def get_identifier(self, identifier_id):
        with self._lock:
            row = self._identifiers.get(identifier_id)
            return dict(row) if row else None

    def deactivate_identifier(self, identifier_id, *, status="rejected", audit=None):
        if status not in _IDENTIFIER_END_STATES:
            raise ValueError(f"非法终态 {status!r}（合法：{_IDENTIFIER_END_STATES}）")
        with self._lock:
            row = self._identifiers.get(identifier_id)
            if not row or row["status"] != "active":
                return False
            self._memory_audit(audit)
            row["status"] = status
            return True

    def repoint_identifier(self, identifier_id, new_target_object_id, *, by,
                           new_target_revision=None, audit=None):
        with self._lock:
            row = self._identifiers.get(identifier_id)
            if not row or row["status"] != "active":
                raise ValueError(f"identifier {identifier_id} 不存在或非 active，无法改指")
            self._memory_audit(audit)
            row["status"] = "superseded"   # 同锁内先让位再插新行（对齐 RDS 同事务原子性）
            try:
                new_id = self.insert_identifier(
                    row["namespace"], row["raw_value"], row["norm_value"],
                    new_target_object_id, method="manual", relation=row["relation"],
                    target_revision=new_target_revision, confidence=1.0, confirmed_by=by,
                    source_case_id=row["source_case_id"], _caller="store_internal")
            except Exception:
                row["status"] = "active"   # 回滚让位
                raise
            row["superseded_by"] = new_id
            return new_id

    # ── 原子复合写（PR-C，与 RDS 同契约）────────────────────────────────────
    def mint_object_with_alias(self, object_type, title, *, owner_dept, namespace,
                               raw_value, norm_value, golden=None, provenance=None,
                               data_classification="internal",
                               source_of_record="ontology", lifecycle_state="draft",
                               method="seed", relation="canonical", confidence=1.0,
                               confirmed_by=None, close_open_case=True, case_note=None,
                               audit=None, allow_missing_provenance=False):
        _check_owner_dept(owner_dept)
        _check_lifecycle(lifecycle_state)
        _check_confidence(confidence)
        _check_golden_provenance(set(golden or {}), provenance,
                                 allow_missing=allow_missing_provenance,
                                 where="mint_object_with_alias")
        with self._lock:
            if self.get_active_identifier(namespace, norm_value) is not None:
                raise DuplicateActiveIdentifier(
                    f"{namespace}:{norm_value} 已有 active 别名（mint 整体回滚，无孤儿对象）")
            if object_type not in self._seq:
                raise ValueError(f"object_type {object_type!r} 未在 ontology_ref_seq 登记类型码")
            self._memory_audit(audit)
            obj = self.mint_object(object_type, title, owner_dept=owner_dept, golden=golden,
                                   provenance=provenance,
                                   data_classification=data_classification,
                                   source_of_record=source_of_record,
                                   lifecycle_state=lifecycle_state,
                                   allow_missing_provenance=allow_missing_provenance,
                                   _caller="store_internal")
            case = self.get_open_case(namespace, norm_value) if close_open_case else None
            identifier_id = new_ulid()
            self._identifiers[identifier_id] = {
                "identifier_id": identifier_id, "namespace": namespace,
                "raw_value": raw_value, "norm_value": norm_value,
                "target_object_id": obj["object_id"], "target_revision": None,
                "relation": relation, "resolution_method": method,
                "confidence": confidence, "status": "active", "superseded_by": None,
                "source_case_id": case["case_id"] if case else None,
                "confirmed_by": confirmed_by, "approval_request_id": None,
                "first_seen_at": self._tick()}
            closed = None
            if case:
                self._case_transition(case["case_id"], to_status="resolved",
                                      by=confirmed_by or "auto",
                                      note=case_note or "铸对象+首别名同事务闭环",
                                      identifier_id=identifier_id)
                closed = case["case_id"]
            return {**obj, "identifier_id": identifier_id, "closed_case_id": closed}

    def confirm_case_with_identifier(self, case_id, *, target_object_id, by, note=None,
                                     relation="alias", target_revision=None,
                                     method="manual", confidence=1.0, audit=None):
        _check_confidence(confidence)
        with self._lock:
            case = self._cases.get(case_id)
            if not case or case["status"] != "open":
                raise ValueError(f"case {case_id} 不存在或非 open（已被并发处置）")
            if self.get_active_identifier(case["namespace"], case["norm_value"]) is not None:
                raise DuplicateActiveIdentifier(
                    f"{case_id} 对应编号已有 active 别名（确认整体回滚）")
            self._assert_active_target(target_object_id, what="确认")   # C1
            identifier_id = new_ulid()
            if audit:
                audit = {**audit, "detail": {**(audit.get("detail") or {}),
                                             "identifier_id": identifier_id}}
            self._memory_audit(audit)
            self._identifiers[identifier_id] = {
                "identifier_id": identifier_id, "namespace": case["namespace"],
                "raw_value": case["raw_value"], "norm_value": case["norm_value"],
                "target_object_id": target_object_id, "target_revision": target_revision,
                "relation": relation, "resolution_method": method,
                "confidence": confidence, "status": "active", "superseded_by": None,
                "source_case_id": case_id, "confirmed_by": by,
                "approval_request_id": None, "first_seen_at": self._tick()}
            case.update(status="resolved", resolved_identifier_id=identifier_id,
                        resolved_by=by, resolution_note=note)
            return identifier_id

    def insert_identifier_closing_case(self, namespace, raw_value, norm_value,
                                       target_object_id, *, method="manual",
                                       relation="alias", target_revision=None,
                                       confidence=1.0, confirmed_by=None,
                                       approval_request_id=None, note=None, audit=None,
                                       operation_writer=None):
        _check_confidence(confidence)
        with self._lock:
            if self.get_active_identifier(namespace, norm_value) is not None:
                raise DuplicateActiveIdentifier(f"{namespace}:{norm_value} 已有 active 别名")
            self._assert_active_target(target_object_id, what="受治理确认")   # C1
            case = self.get_open_case(namespace, norm_value)
            self._memory_audit(audit)
            identifier_id = new_ulid()
            # PR-3 Stage C 座缝（与 RDS 同契约，cur 传 None）：内存后端无事务，fence
            # 语义靠「写台账先于 mutation」——writer 抛 OperationAlreadyApplied 时
            # identifier/case 均未动（_memory_audit 已记：测试后端已知微差，RDS 为准）。
            if operation_writer is not None:
                operation_writer(None, identifier_id, case["case_id"] if case else None)
            self._identifiers[identifier_id] = {
                "identifier_id": identifier_id, "namespace": namespace,
                "raw_value": raw_value, "norm_value": norm_value,
                "target_object_id": target_object_id, "target_revision": target_revision,
                "relation": relation, "resolution_method": method,
                "confidence": confidence, "status": "active", "superseded_by": None,
                "source_case_id": case["case_id"] if case else None,
                "confirmed_by": confirmed_by,
                "approval_request_id": approval_request_id,
                "first_seen_at": self._tick()}
            closed = None
            if case:
                self._case_transition(case["case_id"], to_status="resolved",
                                      by=confirmed_by or "-",
                                      note=note or "经受治理动作确认",
                                      identifier_id=identifier_id)
                closed = case["case_id"]
            return identifier_id, closed

    def list_identifiers_for_target(self, target_object_id, status=None):
        with self._lock:
            out = [dict(r) for r in self._identifiers.values()
                   if r["target_object_id"] == target_object_id
                   and (status is None or r["status"] == status)]
            return sorted(out, key=lambda r: r["first_seen_at"])

    # ── 关系 / sem 投影（与 RDS 同契约）──────────────────────────────────────
    def add_link(self, src_object_id, dst_object_id, link_type, *, attrs=None, _caller=None):
        """P1-8/P1-9（与 RDS 同契约）：端点 active+类型匹配 → single 基数闸 →
        同三元组幂等返回 → 插入。"""
        _check_link_type(link_type)
        _check_caller(_caller, "add_link")
        if src_object_id == dst_object_id:
            raise ValueError("link 端点不得自指")
        spec = LINK_TYPE_SPECS[link_type]
        with self._lock:
            _check_link_endpoints(link_type, self._objects.get(src_object_id),
                                  self._objects.get(dst_object_id))
            if spec["src_cardinality"] == "single":
                for row in self._links.values():
                    if (row["src_object_id"], row["link_type"], row["status"]) == \
                            (src_object_id, link_type, "active"):
                        if row["dst_object_id"] == dst_object_id:   # 同关系重建=幂等
                            return row["link_id"]
                        raise LinkCardinalityViolation(
                            f"{link_type} 为 single 基数：src {src_object_id} 已有 active "
                            f"关系指向 {row['dst_object_id']}——先 retire 旧关系再建新"
                            "（S3 纠错），拒绝双活")
            for row in self._links.values():
                if (row["src_object_id"], row["dst_object_id"], row["link_type"]) == \
                        (src_object_id, dst_object_id, link_type):
                    return row["link_id"]              # 含 retired 行：不复活不覆盖
            link_id = new_ulid()
            self._links[link_id] = {
                "link_id": link_id, "src_object_id": src_object_id,
                "dst_object_id": dst_object_id, "link_type": link_type,
                "attrs_json": _dump(attrs), "status": "active",
                "created_at": self._tick()}
            return link_id

    def list_links(self, *, src_object_id=None, dst_object_id=None, link_type=None,
                   status="active", limit=200):
        with self._lock:
            rows = [dict(r) for r in self._links.values()
                    if (src_object_id is None or r["src_object_id"] == src_object_id)
                    and (dst_object_id is None or r["dst_object_id"] == dst_object_id)
                    and (link_type is None or r["link_type"] == link_type)
                    and (status is None or r["status"] == status)]
            rows.sort(key=lambda r: r["created_at"])
            return rows[: max(1, min(int(limit), 500))]

    def get_object_by_ref(self, canonical_ref):
        with self._lock:
            for row in self._objects.values():
                if row["canonical_ref"] == canonical_ref:
                    return dict(row)
            return None

    def sem_spec_rows(self, sku_object_id, kind):
        proj = SEM_PROJECTIONS[kind]
        with self._lock:
            sku = self._objects.get(sku_object_id)
            if not sku or sku["object_type"] != "sku" or sku["status"] != "active":
                return []
            prod = None
            for lk in self._links.values():
                if (lk["src_object_id"] == sku_object_id and lk["status"] == "active"
                        and lk["link_type"] == "sku_of_product"):
                    cand = self._objects.get(lk["dst_object_id"])
                    if cand and cand["status"] == "active":
                        prod = cand
                        break
            rows = []
            for lk in sorted(self._links.values(), key=lambda r: r["created_at"]):
                if (lk["dst_object_id"] != sku_object_id or lk["status"] != "active"
                        or lk["link_type"] != proj["link_type"]):
                    continue
                sp = self._objects.get(lk["src_object_id"])
                if not sp or sp["object_type"] != proj["spec_type"] or sp["status"] != "active":
                    continue
                try:
                    golden = json.loads(sp["golden_json"] or "{}")
                except Exception:   # noqa: BLE001
                    golden = {}
                row = {"sku_id": sku_object_id, "sku_ref": sku["canonical_ref"],
                       "sku_title": sku["title"], "sku_owner_dept": sku["owner_dept"],
                       "product_id": prod["object_id"] if prod else None,
                       "product_ref": prod["canonical_ref"] if prod else None,
                       "product_name": prod["title"] if prod else None,
                       "product_owner_dept": prod["owner_dept"] if prod else None,
                       "product_classification": (prod["data_classification"]
                                                  if prod else None),
                       "spec_id": sp["object_id"], "spec_ref": sp["canonical_ref"]}
                for f in proj["fields"]:   # JSON_UNQUOTE 语义：值恒为字符串或 None
                    v = golden.get(f)
                    row[f] = None if v is None else str(v)
                row.update(spec_json=sp["golden_json"], spec_state=sp["lifecycle_state"],
                           owner_dept=sp["owner_dept"],
                           data_classification=sp["data_classification"],
                           spec_updated_at=None)
                rows.append(row)
            return rows

    # ── case / 候选 ─────────────────────────────────────────────────────────
    def upsert_case(self, namespace, raw_value, norm_value, *, object_type_hint=None,
                    evidence=None):
        with self._lock:
            for row in self._cases.values():
                if (row["namespace"], row["norm_value"], row["status"]) == \
                        (namespace, norm_value, "open"):
                    row["seen_count"] += 1
                    row["last_seen_at"] = self._tick()
                    return row["case_id"]
            case_id = new_ulid()
            now = self._tick()
            self._cases[case_id] = {
                "case_id": case_id, "namespace": namespace, "raw_value": raw_value,
                "norm_value": norm_value, "object_type_hint": object_type_hint,
                "status": "open", "evidence_json": _dump(evidence), "seen_count": 1,
                "first_seen_at": now, "last_seen_at": now, "resolved_identifier_id": None,
                "resolved_by": None, "resolution_note": None}
            return case_id

    def add_candidate(self, case_id, target_object_id, *, method, confidence,
                      target_revision=None, features=None):
        _check_confidence(confidence)
        with self._lock:
            for row in self._candidates.values():
                if (row["case_id"], row["target_object_id"], row["method"]) == \
                        (case_id, target_object_id, method):
                    row["confidence"] = max(row["confidence"], confidence)
                    if features is not None:
                        row["features_json"] = _dump(features)
                    return row["candidate_id"]
            candidate_id = new_ulid()
            self._candidates[candidate_id] = {
                "candidate_id": candidate_id, "case_id": case_id,
                "target_object_id": target_object_id, "target_revision": target_revision,
                "method": method, "confidence": confidence, "features_json": _dump(features)}
            return candidate_id

    def get_case(self, case_id):
        with self._lock:
            row = self._cases.get(case_id)
            return dict(row) if row else None

    def get_open_case(self, namespace, norm_value):
        with self._lock:
            for row in self._cases.values():
                if (row["namespace"], row["norm_value"], row["status"]) == \
                        (namespace, norm_value, "open"):
                    return dict(row)
            return None

    def list_open_cases(self, *, namespace=None, object_type_hint=None, order="freq",
                        limit=50, offset=0, scope_filter=None, cursor=None):
        def _in_scope(r):
            if scope_filter is None:
                return True
            nss = scope_filter.get("namespaces") or []
            prefixes = scope_filter.get("namespace_prefixes") or []
            otypes = scope_filter.get("object_types") or []
            return (r["namespace"] in nss
                    or r["namespace"].split(":", 1)[0] in prefixes
                    or (r["object_type_hint"] or "") in otypes)

        def _norm_ls(v):
            # 路由层游标经字符串编解码；Memory 的 last_seen_at 是 int 伪时钟 → 归一回 int
            try:
                return int(v)
            except (TypeError, ValueError):
                return v

        def _after_cursor(r):
            if not cursor:
                return True
            if order == "freq":
                mine = (r["seen_count"], r["last_seen_at"], r["case_id"])
                edge = (int(cursor["seen_count"]), _norm_ls(cursor["last_seen_at"]),
                        cursor["case_id"])
            else:
                mine = (r["last_seen_at"], r["case_id"])
                edge = (_norm_ls(cursor["last_seen_at"]), cursor["case_id"])
            return mine < edge   # DESC 序：游标之后 = 严格小于

        with self._lock:
            rows = [dict(r) for r in self._cases.values() if r["status"] == "open"
                    and (namespace is None or r["namespace"] == namespace)
                    and (object_type_hint is None or r["object_type_hint"] == object_type_hint)
                    and _in_scope(r) and _after_cursor(r)]
            key = (lambda r: (-r["seen_count"], -r["last_seen_at"],
                              tuple(-ord(c) for c in r["case_id"]))) if order == "freq" \
                else (lambda r: (-r["last_seen_at"],
                                 tuple(-ord(c) for c in r["case_id"])))
            rows.sort(key=key)
            lo = max(0, int(offset))
            return rows[lo:lo + max(1, min(int(limit), 200))]

    def list_candidates(self, case_id):
        with self._lock:
            rows = [dict(r) for r in self._candidates.values() if r["case_id"] == case_id]
            return sorted(rows, key=lambda r: -r["confidence"])

    def list_candidates_for_cases(self, case_ids):
        """与 RDS list_candidates_for_cases 同契约（perf 批次 A §4.1）：{case_id:
        [(cand_row, obj_slice), ...]}，每 case 内 confidence DESC。cand_row 复刻
        Memory list_candidates 形（无 created_at）；目标缺失 → obj_slice={}；空 → {}。"""
        wanted = set(case_ids or [])
        if not wanted:
            return {}
        grouped: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
        with self._lock:
            for r in self._candidates.values():
                cid = r["case_id"]
                if cid not in wanted:
                    continue
                obj = self._objects.get(r["target_object_id"])
                obj_slice = {} if obj is None else {
                    k: obj.get(k) for k in ("canonical_ref", "title", "object_type",
                                            "status", "owner_dept", "data_classification")}
                grouped.setdefault(cid, []).append((dict(r), obj_slice))
        for cid in grouped:
            grouped[cid].sort(key=lambda p: -p[0]["confidence"])
        return grouped

    def resolve_case(self, case_id, *, identifier_id, by, note=None, audit=None):
        return self._case_transition(case_id, to_status="resolved", by=by, note=note,
                                     identifier_id=identifier_id, audit=audit)

    def dismiss_case(self, case_id, *, by, note, audit=None):
        if not (note or "").strip():
            raise ValueError("dismiss 必须给 resolution_note（处置理由）")
        return self._case_transition(case_id, to_status="dismissed", by=by, note=note,
                                     audit=audit)

    def _case_transition(self, case_id, *, to_status, by, note, identifier_id=None,
                         audit=None):
        with self._lock:
            row = self._cases.get(case_id)
            if not row or row["status"] != "open":
                return False
            self._memory_audit(audit)
            row.update(status=to_status, resolved_identifier_id=identifier_id,
                       resolved_by=by, resolution_note=note)
            return True

    # ── 溯源目录 / stewardship ──────────────────────────────────────────────
    def upsert_attribute_sources(self, rows):
        with self._lock:
            for r in rows:
                self._attr_sources[(r["object_type"], r["attribute"])] = dict(r)
            return len(rows)

    def list_attribute_sources(self):
        with self._lock:
            return [dict(v) for k, v in sorted(self._attr_sources.items())]

    def upsert_stewardship(self, rows):
        with self._lock:
            for r in rows:
                self._stewardship[(r["scope_type"], r["scope_key"])] = dict(r)
            return len(rows)

    def list_stewardship(self):
        with self._lock:
            return [dict(v) for k, v in sorted(self._stewardship.items())]

    def delete_stewardship(self, scope_type, scope_key):
        with self._lock:
            return self._stewardship.pop((scope_type, scope_key), None) is not None

    # ── 统计 ────────────────────────────────────────────────────────────────
    def record_population_snapshot(self, namespace_counts, *, source="seeding"):
        with self._lock:
            for ns, n in namespace_counts.items():
                self._population[ns] = {"records": int(n), "source": source,
                                        "snapshot_at": self._tick()}
            return len(namespace_counts)

    def population_total(self):
        with self._lock:
            if not self._population:
                return None
            return sum(v["records"] for v in self._population.values())

    def coverage(self, object_type=None):
        with self._lock:
            def _type_of(target_id):
                obj = self._objects.get(target_id)
                return obj["object_type"] if obj else None

            act = [r for r in self._identifiers.values() if r["status"] == "active"
                   and (object_type is None or _type_of(r["target_object_id"]) == object_type)]
            cases = [r for r in self._cases.values()
                     if object_type is None or r["object_type_hint"] == object_type]
            open_n = sum(1 for r in cases if r["status"] == "open")
        return RDSOntologyStore._coverage_payload(
            len(act), sum(1 for r in act if r["confirmed_by"] == "auto"), open_n,
            sum(1 for r in cases if r["status"] == "resolved"),
            sum(1 for r in cases if r["status"] == "dismissed"),
            population=(None if object_type else self.population_total()))
