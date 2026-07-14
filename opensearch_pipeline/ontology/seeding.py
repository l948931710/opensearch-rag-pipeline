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
import hashlib
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
        self._fingerprint: Optional[str] = None

    def fingerprint(self) -> str:
        """快照文件 sha256（缓存）——manifest 输入绑定（重审计 §2）的事实指纹。"""
        if self._fingerprint is None:
            h = hashlib.sha256()
            with open(self._path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            self._fingerprint = h.hexdigest()
        return self._fingerprint

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


_TITLE_MATCH_LIMIT = 50    # 同名聚类召回每臂上限（打满即 WARNING：截断可观测）


def _title_key(title: str) -> str:
    """标题聚类键——委托 normalize.title_key（C2：权威实现挪到 normalize.py，
    与 store 落列 / backfill 脚本三处同源）。"""
    from opensearch_pipeline.ontology.normalize import title_key
    return title_key(title)


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
        # P1-9：裸写原语的调用路径声明（store._check_caller）——播种/回填共用本核心，
        # 按证据源标注真实路径；未知来源一律按 seeding 申报（仍是 S1 合法离线路径）
        self._caller = "backfill" if evidence_source == "backfill" else "seeding"
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
        """C2 归一化感知召回（复核批次6）。主臂=normalized_title 等值（038 列，
        LIKE 对空白/全半角变体天然漏召回=false-mint 根源）；辅臂=legacy LIKE +
        Python 归一过滤，兜住 038 backfill 前的存量 NULL 行——辅臂命中而主臂 miss
        即 backfill 缺口，WARNING 可观测（回填完成后辅臂恒为主臂子集，零噪声）。
        任一臂打满 _TITLE_MATCH_LIMIT → WARNING（被截断的同名对象=漏掉的合并候选）。"""
        key = _title_key(title)
        seen: Dict[str, Dict[str, Any]] = {}
        try:
            norm_hits = self._store.find_objects(object_type, norm_title=key,
                                                 limit=_TITLE_MATCH_LIMIT)
        except TypeError:   # 旧 store 桩无 norm_title 参数：整程退回辅臂
            norm_hits = []
        if len(norm_hits) >= _TITLE_MATCH_LIMIT:
            logger.warning("title_matches 归一臂打满 LIMIT=%d（type=%s key=%r）——超出部分被"
                           "截断，可能漏合并候选", _TITLE_MATCH_LIMIT, object_type, key)
        for obj in norm_hits:
            seen[obj["object_id"]] = obj
        legacy = self._store.find_objects(object_type, title_like=title.strip(),
                                          limit=_TITLE_MATCH_LIMIT)
        if len(legacy) >= _TITLE_MATCH_LIMIT:
            logger.warning("title_matches LIKE 辅臂打满 LIMIT=%d（type=%s title=%r）——超出"
                           "部分被截断", _TITLE_MATCH_LIMIT, object_type, title)
        gap = [o for o in legacy
               if _title_key(o["title"]) == key and o["object_id"] not in seen]
        if gap and norm_hits is not None:
            logger.warning("title_matches 辅臂命中 %d 行归一臂 miss（type=%s key=%r）——"
                           "038 backfill 缺口，请跑 scripts/backfill_normalized_title.py",
                           len(gap), object_type, key)
        for obj in gap:
            seen[obj["object_id"]] = obj
        out = []
        for oid, obj in seen.items():
            full = self._store.get_object(oid) or obj
            out.append(full)
        for p in self._planned_objects:
            if p["object_type"] == object_type and _title_key(p["title"]) == key:
                out.append(p)
        return out

    # -- 写 ------------------------------------------------------------------
    def _provenance(self, r: SeedRecord) -> Optional[Dict[str, Any]]:
        """PR-H P1：golden 属性值级溯源——没有它 golden 会悄悄变成"业务事实副本"。
        每个播种属性记 {来源系统, 源键, as-of, 记录方}；答案侧据此回传"数据从哪来、多新鲜"。"""
        if not r.attrs:
            return None
        from datetime import datetime, timezone
        stamp = {"sor_system": f"snapshot:{self._evidence_source}",
                 "source_key": r.raw_code,
                 "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "recorded_by": self._evidence_source}
        return {attr: dict(stamp) for attr in r.attrs}

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
            provenance=self._provenance(r),
            data_classification=r.data_classification,
            _caller=self._caller)["object_id"]

    def mint_with_alias(self, r: SeedRecord, ns: str, raw: str, norm: str):
        """铸对象 + 首别名 + （若有）闭 open case **一个事务**（PR-C，P0-05）：
        别名撞 uk → store 整体回滚（无孤儿对象），返回 (None, False) 由调用方按
        已激活跳过记账。dry-run 记 planned 账本仿真同一语义。返回 (object_id, ok)。"""
        case_id = self._open_case_id(ns, norm)
        if self.dry:
            oid = self.mint(r)
            self._planned_alias[(ns, norm)] = oid
            if case_id:
                self._note_heal(ns, norm, case_id)
            return oid, True
        try:
            res = self._store.mint_object_with_alias(
                r.object_type, r.title, owner_dept=r.owner_dept, golden=r.attrs,
                provenance=self._provenance(r),
                data_classification=r.data_classification,
                namespace=ns, raw_value=raw, norm_value=norm,
                method="seed", relation="canonical", confidence=1.0, confirmed_by="auto",
                close_open_case=True,
                case_note=f"离线{self._evidence_source}铸对象落成，case 随之闭环")
        except DuplicateActiveIdentifier:
            return None, False
        if res.get("closed_case_id"):
            self._healed.add((ns, norm))
            self._report.cases_healed += 1
        return res["object_id"], True

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
                source_case_id=None if case_id == _BATCH_CASE else case_id,
                _caller=self._caller)
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


def source_fingerprint(source: Any) -> Optional[str]:
    """duck-type 取快照源指纹（sha256 hex）。源不支持/读失败 → None（auto 恒关，
    fail-closed——绑定证明缺失就绝不放行 auto，候选/case 路径不受影响）。"""
    try:
        fp = getattr(source, "fingerprint", None)
        out = fp() if callable(fp) else None
        return str(out).strip().lower() if out else None
    except Exception:   # noqa: BLE001
        logger.warning("快照源指纹计算失败（auto 保持关闭）", exc_info=True)
        return None


def _generate_candidates(sink: "_Sink", r: SeedRecord, norm: str) -> List[Candidate]:
    """播种侧候选生成（_decide 与 simulate_seed_decision 共用——C2 复核批次6 抽取，
    保证回测仿真与真实播种走同一套规则，不出现「测的不是跑的」）。"""
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
    return candidates


def simulate_seed_decision(store, r: SeedRecord, *, tau_table: Optional[TauTable] = None,
                           source_fingerprint: Optional[str] = None) -> Dict[str, Any]:
    """C2（复核批次6）：播种路径 would-auto 仿真——纯读，零写库，供
    scripts/ontology_backtest.py 的 seed 臂调用。S9 硬闸只回测 resolver 路径；
    播种侧合并判定走 rule①（改模后缀）/rule②（同型同名聚类），两条路径的
    false-merge 面不重合（复核确认的覆盖缺口）。

    与真实播种的差异（有意）：auto 裁决用 auto_eligible（绕过 AUTO_ACK 环境闸）——
    回测回答「若开 auto 会怎样」，与 run_backtest 的 resolver 臂同口径。
    返回 {"action": skip_active|auto_alias|case|mint, "target", "confidence", "candidates"}。
    """
    from opensearch_pipeline.ontology.resolve import auto_eligible
    tau = tau_table or TauTable.from_env(strict=True)
    sink = _Sink(store, True, SeedReport())
    norm = normalize(r.namespace, r.raw_code)
    if sink.is_active(r.namespace, norm):
        row = store.get_active_identifier(r.namespace, norm) or {}
        return {"action": "skip_active", "target": row.get("target_object_id"),
                "confidence": None, "candidates": 0}
    candidates = _generate_candidates(sink, r, norm)
    winner = auto_eligible(candidates, intent="read", namespace=r.namespace, tau_table=tau)
    if winner is not None:
        return {"action": "auto_alias", "target": winner.target_object_id,
                "confidence": winner.confidence, "candidates": len(candidates)}
    if candidates:
        return {"action": "case", "target": None, "confidence": None,
                "candidates": len(candidates)}
    return {"action": "mint", "target": None, "confidence": None, "candidates": 0}


def _decide(r: SeedRecord, sink: _Sink, tau: TauTable, report: SeedReport, *,
            mint_new: bool = True, source_fp: Optional[str] = None) -> None:
    norm = normalize(r.namespace, r.raw_code)
    if sink.is_active(r.namespace, norm):
        report.skipped_active += 1
        report.add(action="skip_active", namespace=r.namespace, norm=norm)
        sink.heal_if_stale(r.namespace, norm)
        return

    candidates = _generate_candidates(sink, r, norm)

    if candidates:
        winner = may_auto_activate(candidates, intent="read", namespace=r.namespace,
                                   tau_table=tau, source_fingerprint=source_fp)
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

    # 无候选 → 新物理对象：铸 canonical + 首别名（同事务，P0-05——撞键整体回滚无孤儿）
    oid, ok = sink.mint_with_alias(r, r.namespace, r.raw_code, norm)
    if ok:
        report.minted += 1
        report.add(action="mint", namespace=r.namespace, norm=norm, target=oid,
                   title=r.title)
    else:   # 并发方先占了别名：视同已激活跳过（重跑幂等语义），零残留
        report.skipped_active += 1
        report.add(action="skip_active", namespace=r.namespace, norm=norm)
        sink.heal_if_stale(r.namespace, norm)


def seed_snapshot(store, source: Any, *, dry_run: bool = True,
                  tau_table: Optional[TauTable] = None,
                  limit: Optional[int] = None, mint_new: bool = True,
                  evidence_source: str = "seeding") -> SeedReport:
    """跑一遍快照播种/回填。单条失败只记 errors 不拖垮批（可断点重跑，幂等）。
    mint_new=False = 观测语义（回填 mention 模式：无候选不铸对象，只入 case）。

    population 分母纪律（P1-12）：coverage 的分母是「源里有多少记录」，与本批处理量
    无关——limit 只截断**处理**，不截断**计数**（此前 limit 批直接 break，把分母覆写成
    批大小 → active/批大小 覆盖率虚高）。代价是 limit 批也要把源迭代完（纯计数不判定
    不写库）；快照/CSV 源是本地顺序读，可接受。"""
    tau = tau_table or TauTable.from_env(strict=True)   # P0-07：写 worker 非法 τ 即断
    report = SeedReport(dry_run=dry_run)
    src_fp = source_fingerprint(source)   # manifest 输入绑定（重审计 §2）：一次实算全批复用
    sink = _Sink(store, dry_run, report, evidence_source=evidence_source)
    records: Iterable[SeedRecord] = source.iter_records()
    ns_counts: Dict[str, int] = {}                # PR-F/P1-12：分母=全量源计数（不吃 limit）
    limit_noted = False
    for r in records:
        ns_counts[r.namespace] = ns_counts.get(r.namespace, 0) + 1
        if limit is not None and report.records >= limit:
            if not limit_noted:
                report.add(action="limit_reached", limit=limit)
                limit_noted = True
            continue                              # 只计数不处理：分母仍走完全量源
        report.records += 1
        try:
            _decide(r, sink, tau, report, mint_new=mint_new, source_fp=src_fp)
        except Exception as e:   # noqa: BLE001 — 单条脏数据不掀翻整批
            report.errors += 1
            report.add(action="error", namespace=r.namespace, raw=r.raw_code, error=str(e))
            logger.warning("播种单条失败（跳过继续）：%s %s", r.namespace, r.raw_code,
                           exc_info=True)
    if not dry_run and ns_counts:
        try:   # 快照登记 fail-open：分母是指标不是事实，登记失败不掀翻批
            store.record_population_snapshot(ns_counts, source=evidence_source)
        except Exception:   # noqa: BLE001
            logger.warning("population 快照登记失败（coverage 回退 approx 口径）", exc_info=True)
    return report
