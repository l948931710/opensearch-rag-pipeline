# -*- coding: utf-8 -*-
"""
seeding.py — 从源系统快照播种 canonical 对象与别名（P0 PR5；离线路径，S1 合法落库点之一）。
回填 worker（PR9，backfill.py）复用同一决策核心——差异只由两个开关表达，绝不第二套判定：

决策纪律（P0-P1 落地细化 §4.1 + 外评 S1/S6）：
- **已有 active 别名 → 跳过**（幂等：崩溃重跑/断点续跑天然安全）；跳过时若同 (ns,norm)
  还挂着 open case（历史积压/跨路径竞态遗留）→ **愈合闭环**（resolved by='auto'），
  驱动 resolution_coverage 单调增长（回填 M2 门指标；fail-open 不拖垮批）；
- **改模后缀命中基础码 → 入 case 恒人审**（修正④：改模判定 P0 全 HITL，置信 0.85 设计性
  够不着 auto 线，与 resolve._RULE_SUFFIX_CONF 对齐）；
- **同型同名（归一后 exact-title）→ 候选**：属性不冲突 conf=0.96（唯一时过 τ_high 可 auto，
  这就是"同物多货号"的愈合路径）；任一共有属性冲突 conf=0.80（压进 HITL 区间）；
- **auto 资格判定唯一入口 = resolve.may_auto_activate**（三禁共享，播种不得自铸第二套判定）；
- **无任何候选**：主数据语义（mint_new=True，播种默认）→ 铸新对象 + canonical 别名
  （标 confirmed_by='auto' 入抽检）——方向刻意保守：标题微差会多铸重复品（宁多建，
  steward 用 mark_duplicate 纠错），绝不静默合并；观测语义（mint_new=False，回填默认）
  → **不铸对象**，入 resolution_case 聚合观测（脏观测无铸对象权，防重复品繁殖）；
- **低置信/多候选一律入 case，不铸新对象**（防"U8 货号逐条建品"式重复品繁殖）。

dry-run（CLI 默认）：零写库，用批内账本（planned 对象/别名/case）仿真批内先后效应——
同批两条同名记录，dry 与真跑同样报「铸 1 + auto 别名 1」；愈合计数同账。
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

from opensearch_pipeline.ontology.normalize import normalize
from opensearch_pipeline.ontology.resolve import Candidate, TauTable, may_auto_activate
from opensearch_pipeline.ontology.store import DuplicateActiveIdentifier

logger = logging.getLogger(__name__)

__all__ = ["CsvSnapshotSource", "SeedRecord", "SeedReport", "U8SnapshotSource", "seed_snapshot"]

_REVISION_SUFFIXES = ("-M", "-N", "-W")
_SUFFIX_RULE_CONF = 0.85        # 与 resolve._RULE_SUFFIX_CONF 对齐：改模判定恒 HITL
_EXACT_TITLE_CONF = 0.96        # 同型同名且属性不冲突：唯一时 ≥ τ_high(0.95) 可 auto
_ATTR_CONFLICT_CONF = 0.80      # 同名但属性冲突：矛盾信号压进 HITL 区间
_BATCH_CASE = "__batch_case__"  # dry-run 批内 planned case 哨兵（无真 case_id 可引）


@dataclass
class SeedRecord:
    """一条快照记录（U8 产品档案/物料档案/模具清单的行投影）。attrs=golden 候选属性。"""

    namespace: str
    raw_code: str
    object_type: str
    title: str
    owner_dept: str
    attrs: Dict[str, str] = field(default_factory=dict)
    data_classification: str = "internal"


class CsvSnapshotSource:
    """CSV 快照源（fixtures/本地导出）。固定列 namespace/raw_code/object_type/title/
    owner_dept[/data_classification]，其余非空列一律进 attrs。"""

    _REQUIRED = ("namespace", "raw_code", "object_type", "title", "owner_dept")
    _FIXED = _REQUIRED + ("data_classification",)

    def __init__(self, path: str):
        self._path = path

    def iter_records(self) -> Iterator[SeedRecord]:
        with open(self._path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in self._REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"CSV 缺少必需列：{missing}（现有：{reader.fieldnames}）")
            for row in reader:
                if not (row.get("raw_code") or "").strip():
                    continue
                attrs = {k: v.strip() for k, v in row.items()
                         if k not in self._FIXED and v and v.strip()}
                yield SeedRecord(
                    namespace=row["namespace"].strip(),
                    raw_code=row["raw_code"].strip(),
                    object_type=row["object_type"].strip(),
                    title=row["title"].strip(),
                    owner_dept=row["owner_dept"].strip(),
                    attrs=attrs,
                    data_classification=(row.get("data_classification") or "internal").strip()
                    or "internal")


class U8SnapshotSource:
    """U8 T-1 附属只读库快照源——**契约桩**。

    列名/表清单/diff 语义（有无变更时间戳与操作类型）取决于 go/no-go ①（信息部判定），
    闭合前不实现：宁可显式 NotImplementedError，不虚构表结构。判定后在此填入
    只读 DSN + 表→SeedRecord 的列映射（经 prod_access 只读会话，绝不直连凭据）。
    """

    def __init__(self, *, table_hint: str = ""):
        self._table_hint = table_hint

    def iter_records(self) -> Iterator[SeedRecord]:
        raise NotImplementedError(
            "U8SnapshotSource 等 go/no-go ①（U8 T-1 可 diff 性判定）闭合后实现——"
            "见 docs/ontology_p0_plan_2026-07-10.md Phase 0；当前请用 CsvSnapshotSource")


@dataclass
class SeedReport:
    records: int = 0
    skipped_active: int = 0
    auto_aliased: int = 0
    minted: int = 0
    cases_opened: int = 0
    cases_healed: int = 0
    errors: int = 0
    dry_run: bool = True
    actions: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, **kw) -> None:
        self.actions.append(kw)

    def summary(self) -> str:
        mode = "DRY-RUN（零写库）" if self.dry_run else "已写库"
        return (f"[{mode}] 记录 {self.records}：跳过(已激活) {self.skipped_active} · "
                f"auto 别名 {self.auto_aliased} · 铸新对象 {self.minted} · "
                f"入工作台 case {self.cases_opened} · case 愈合 {self.cases_healed} · "
                f"错误 {self.errors}")


def _title_key(title: str) -> str:
    """标题聚类键：借 lab_sample 归一（去全部空白+全半角统一，保内容原样）。"""
    return normalize("lab_sample", title)


def _attrs_conflict(a: Dict[str, str], golden_json: Optional[str]) -> bool:
    """共有属性键取值不同 → 冲突（矛盾信号，压 HITL）。golden 解析失败按不冲突（信号缺失≠矛盾）。"""
    if not a or not golden_json:
        return False
    try:
        b = json.loads(golden_json) if isinstance(golden_json, str) else dict(golden_json)
    except Exception:   # noqa: BLE001
        return False
    for k, v in a.items():
        bv = b.get(k)
        if bv is not None and str(bv).strip() != str(v).strip():
            return True
    return False


class _Sink:
    """播种/回填写面 + 批内视图。dry-run 用 planned 账本仿真批内先后效应（零写库）；
    真跑直落 store（别名撞 uk → 视同已激活，幂等续跑）。愈合计数直接记进 report
    （愈合发生在 alias 落成/跳过两处，由 sink 就地判定，报表口径 dry 与真跑同账）。"""

    def __init__(self, store, dry_run: bool, report: "SeedReport", *,
                 evidence_source: str = "seeding"):
        self._store = store
        self.dry = dry_run
        self._report = report
        self._evidence_source = evidence_source
        self._planned_alias: Dict[tuple, str] = {}      # (ns, norm) → target_object_id
        self._planned_objects: List[Dict[str, Any]] = []
        self._planned_cases: set = set()                # (ns, norm)
        self._healed: set = set()                       # (ns, norm) 批内愈合去重
        self._n = 0

    # -- 批内读 ------------------------------------------------------------
    def is_active(self, ns: str, norm: str) -> bool:
        if (ns, norm) in self._planned_alias:
            return True
        return self._store.get_active_identifier(ns, norm) is not None

    def alias_target(self, ns: str, norm: str) -> Optional[str]:
        hit = self._planned_alias.get((ns, norm))
        if hit:
            return hit
        row = self._store.get_active_identifier(ns, norm)
        return row["target_object_id"] if row else None

    def title_matches(self, object_type: str, title: str) -> List[Dict[str, Any]]:
        key = _title_key(title)
        out = []
        for obj in self._store.find_objects(object_type, title_like=title.strip(), limit=50):
            if _title_key(obj["title"]) == key:
                full = self._store.get_object(obj["object_id"]) or obj
                out.append(full)
        for p in self._planned_objects:
            if p["object_type"] == object_type and _title_key(p["title"]) == key:
                out.append(p)
        return out

    # -- 写 ------------------------------------------------------------------
    def mint(self, r: SeedRecord) -> str:
        if self.dry:
            self._n += 1
            oid = f"DRY-{self._n:06d}"
            self._planned_objects.append({
                "object_id": oid, "object_type": r.object_type, "title": r.title,
                "golden_json": json.dumps(r.attrs, ensure_ascii=False), "status": "active"})
            return oid
        return self._store.mint_object(
            r.object_type, r.title, owner_dept=r.owner_dept, golden=r.attrs,
            data_classification=r.data_classification)["object_id"]

    def alias(self, ns: str, raw: str, norm: str, target: str, *, method: str,
              confidence: float, relation: str = "alias") -> bool:
        """True=落成；False=撞已激活（并发/重跑），调用方按跳过记账。
        落成时若同 (ns,norm) 有 open case → 顺手愈合（identifier 溯源挂 source_case_id）。"""
        case_id = self._open_case_id(ns, norm)
        if self.dry:
            self._planned_alias[(ns, norm)] = target
            if case_id:
                self._note_heal(ns, norm, case_id)
            return True
        try:
            identifier_id = self._store.insert_identifier(
                ns, raw, norm, target, method=method, relation=relation,
                confidence=confidence, confirmed_by="auto",
                source_case_id=None if case_id == _BATCH_CASE else case_id)
        except DuplicateActiveIdentifier:
            return False
        if case_id and case_id != _BATCH_CASE:
            self._close_case(ns, norm, case_id, identifier_id,
                             note=f"离线{self._evidence_source}自动确认落成，case 随之闭环")
        return True

    def heal_if_stale(self, ns: str, norm: str) -> None:
        """跳过(已激活)路径的对账愈合：active 别名已在而 case 还开着 → 闭环尸案。
        批内 planned 别名在 alias() 时已愈合过，这里只对账 store 实况。"""
        if (ns, norm) in self._planned_alias or (ns, norm) in self._healed:
            return
        case_id = self._open_case_id(ns, norm)
        if not case_id or case_id == _BATCH_CASE:
            return
        if self.dry:
            self._note_heal(ns, norm, case_id)
            return
        row = self._store.get_active_identifier(ns, norm)
        if row is None:   # 窄竞态：刚被处置——留给下轮对账
            return
        self._close_case(ns, norm, case_id, row["identifier_id"],
                         note=f"离线{self._evidence_source}对账：该编号已有 active 别名")

    # -- 愈合内部件 ----------------------------------------------------------
    def _open_case_id(self, ns: str, norm: str) -> Optional[str]:
        """dry 先看批内账本（真跑批内 case 已实际落库，直接查 store 拿真 id）。"""
        if self.dry and (ns, norm) in self._planned_cases:
            return _BATCH_CASE
        try:
            row = self._store.get_open_case(ns, norm)
        except Exception:   # noqa: BLE001 — 愈合是搭车动作，查不到不拦主流程
            logger.warning("open case 查询失败（fail-open）：%s %s", ns, norm, exc_info=True)
            return None
        return row["case_id"] if row else None

    def _close_case(self, ns: str, norm: str, case_id: str, identifier_id: str, *,
                    note: str) -> None:
        try:
            if self._store.resolve_case(case_id, identifier_id=identifier_id,
                                        by="auto", note=note):
                self._note_heal(ns, norm, case_id)
        except Exception:   # noqa: BLE001 — fail-open：闭不掉不影响别名本身
            logger.warning("case 愈合失败（fail-open）：%s", case_id, exc_info=True)

    def _note_heal(self, ns: str, norm: str, case_id: str) -> None:
        if (ns, norm) in self._healed:
            return
        self._healed.add((ns, norm))
        self._report.cases_healed += 1
        self._report.add(action="heal_case", namespace=ns, norm=norm,
                         case=None if case_id == _BATCH_CASE else case_id)

    def open_case(self, r: SeedRecord, norm: str, candidates: List[Candidate]) -> bool:
        """True=本批首次入 case（计数用；重复观测只聚合不重复计）。"""
        first = (r.namespace, norm) not in self._planned_cases
        self._planned_cases.add((r.namespace, norm))
        if self.dry:
            return first
        case_id = self._store.upsert_case(
            r.namespace, r.raw_code, norm, object_type_hint=r.object_type,
            evidence={"source": self._evidence_source, "title": r.title, "attrs": r.attrs})
        for c in candidates:
            if c.target_object_id.startswith("DRY-"):
                continue   # 理论不可达（真跑无 DRY id）；防御
            self._store.add_candidate(case_id, c.target_object_id, method=c.method,
                                      confidence=c.confidence, features=c.features)
        return first


def _decide(r: SeedRecord, sink: _Sink, tau: TauTable, report: SeedReport, *,
            mint_new: bool = True) -> None:
    norm = normalize(r.namespace, r.raw_code)
    if sink.is_active(r.namespace, norm):
        report.skipped_active += 1
        report.add(action="skip_active", namespace=r.namespace, norm=norm)
        sink.heal_if_stale(r.namespace, norm)
        return

    candidates: List[Candidate] = []
    # rule ①：改模后缀 → 基础码目标（恒人审）
    for suffix in _REVISION_SUFFIXES:
        if norm.endswith(suffix) and len(norm) > len(suffix):
            base_target = sink.alias_target(r.namespace, norm[: -len(suffix)])
            if base_target:
                candidates.append(Candidate(
                    target_object_id=base_target, method="rule",
                    confidence=_SUFFIX_RULE_CONF,
                    features={"rule": "strip_suffix", "suffix": suffix}))
    # rule ②：同型同名聚类
    for obj in sink.title_matches(r.object_type, r.title):
        conflict = _attrs_conflict(r.attrs, obj.get("golden_json"))
        candidates.append(Candidate(
            target_object_id=obj["object_id"], method="rule",
            confidence=_ATTR_CONFLICT_CONF if conflict else _EXACT_TITLE_CONF,
            features={"rule": "exact_title", "attr_conflict": conflict}))

    if candidates:
        winner = may_auto_activate(candidates, intent="read", namespace=r.namespace,
                                   tau_table=tau)
        if winner is not None:
            if sink.alias(r.namespace, r.raw_code, norm, winner.target_object_id,
                          method=winner.method, confidence=winner.confidence):
                report.auto_aliased += 1
                report.add(action="auto_alias", namespace=r.namespace, norm=norm,
                           target=winner.target_object_id, confidence=winner.confidence)
            else:
                report.skipped_active += 1
                report.add(action="skip_active", namespace=r.namespace, norm=norm)
                sink.heal_if_stale(r.namespace, norm)
            return
        if sink.open_case(r, norm, candidates):
            report.cases_opened += 1
        report.add(action="case", namespace=r.namespace, norm=norm,
                   candidates=len(candidates))
        return

    if not mint_new:   # 观测语义（回填默认）：无候选也不铸对象，只聚合观测供工作台裁决
        if sink.open_case(r, norm, []):
            report.cases_opened += 1
        report.add(action="case", namespace=r.namespace, norm=norm, candidates=0)
        return

    # 无候选 → 新物理对象：铸 canonical + 首别名
    oid = sink.mint(r)
    if sink.alias(r.namespace, r.raw_code, norm, oid, method="seed", confidence=1.0,
                  relation="canonical"):
        report.minted += 1
        report.add(action="mint", namespace=r.namespace, norm=norm, target=oid,
                   title=r.title)
    else:   # 极窄竞态：铸完对象别名被并发占——对象留着（无别名），入账错误供人查
        report.errors += 1
        report.add(action="mint_alias_conflict", namespace=r.namespace, norm=norm, target=oid)


def seed_snapshot(store, source: Any, *, dry_run: bool = True,
                  tau_table: Optional[TauTable] = None,
                  limit: Optional[int] = None, mint_new: bool = True,
                  evidence_source: str = "seeding") -> SeedReport:
    """跑一遍快照播种/回填。单条失败只记 errors 不拖垮批（可断点重跑，幂等）。
    mint_new=False = 观测语义（回填 mention 模式：无候选不铸对象，只入 case）。"""
    tau = tau_table or TauTable.from_env()
    report = SeedReport(dry_run=dry_run)
    sink = _Sink(store, dry_run, report, evidence_source=evidence_source)
    records: Iterable[SeedRecord] = source.iter_records()
    for r in records:
        if limit is not None and report.records >= limit:
            report.add(action="limit_reached", limit=limit)
            break
        report.records += 1
        try:
            _decide(r, sink, tau, report, mint_new=mint_new)
        except Exception as e:   # noqa: BLE001 — 单条脏数据不掀翻整批
            report.errors += 1
            report.add(action="error", namespace=r.namespace, raw=r.raw_code, error=str(e))
            logger.warning("播种单条失败（跳过继续）：%s %s", r.namespace, r.raw_code,
                           exc_info=True)
    return report
