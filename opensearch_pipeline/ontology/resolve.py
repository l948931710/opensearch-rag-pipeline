# -*- coding: utf-8 -*-
"""
resolve.py — 身份消解服务（在线纯读；外评 S1/S6 的机制承载）。

**S1 读写彻底分离**：`OntologyResolver.resolve()` 只读两层（active 别名精确命中 →
候选生成），**永不落库、永不 auto-activate**——READ_ONLY 工具的风险分级契约由此成立。
持久化确认只在四条路径（播种 seeding / 回填 worker / 工作台决策 / 受治理 Action），
它们在离线侧调用本模块的 `may_auto_activate()` 判定 auto 资格（三禁单测锁死）。
在线 miss 也**不**入 resolution_case（那是写）——观测入队归 sem/回填层显式调用。

**S6 阈值分层机制**：τ 按 (namespace 前缀 × method) 查表，链式回退
(ns,method)→(ns,*)→(*,method)→全局默认(0.95/0.70)；全局与分层均 env 可调
（RAG_ONTOLOGY_TAU_HIGH/LOW/TAU_TABLE）。**分层数字随分层 ground-truth 积累再填**，
机制先就位。embedding 候选置信构造性封顶在 τ_high 之下 + may_auto_activate 拒
embedding —— 双保险（"黑色注塑叉子/勺子"式假高置信）。

候选生成（P0 两源；kie 候选源随 KIE 生产线 P2 接入，method 枚举已留位）：
- rule：剥改模后缀（-M/-N/-W）→ 基础码有 active 别名 → 疑似"同 Product 不同 Revision"。
  置信**设计性落在 HITL 区间**（改模判定 P0/P1 默认全人审——宁多建 Revision 不误合并）。
- embedding：品名相似（复用 retriever.get_query_embedding 的 dense 分量；SIM 下自动
  hash 向量）。候选池=ontology_object 标题（进程内小缓存）；池子大了换 HA3 子索引，
  P0 试点量级够用。
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from opensearch_pipeline.ontology.normalize import dispatch_key, normalize

logger = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "OntologyResolver",
    "ResolveResult",
    "Tau",
    "TauTable",
    "auto_activation_enabled",
    "may_auto_activate",
]

# 改模后缀（候选提示专用；normalize 铁律 1：绝不进归一键）
_REVISION_SUFFIXES = ("-M", "-N", "-W")
# rule(剥后缀) 候选的设计置信：落在默认 HITL 区间 [τ_low, τ_high)——改模判定 P0 全人审
_RULE_SUFFIX_CONF = 0.85
# embedding 候选相对 τ_high 的构造性缺口（置信 = min(sim, τ_high - 缺口)）
_EMBEDDING_HIGH_GAP = 1e-3
_DEFAULT_POOL_TYPES = ("product", "sku", "mold", "material")
_MAX_CANDIDATES = 5
_TITLE_CACHE_CAP = 4096


# ── τ 查表（S6）───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tau:
    high: float
    low: float


DEFAULT_TAU = Tau(high=0.95, low=0.70)


class TauTable:
    """(namespace 前缀 × method) → Tau，链式回退到全局默认。

    env：RAG_ONTOLOGY_TAU_HIGH / RAG_ONTOLOGY_TAU_LOW 调全局；
    RAG_ONTOLOGY_TAU_TABLE 调分层，JSON 形如
    {"customer|embedding": [0.97, 0.80], "*|rule": [0.93, 0.70]}（键=前缀|method，* 通配）。
    非法条目跳过并告警（fail-open 到默认）——阈值配置坏了不应让解析瘫掉。
    """

    @staticmethod
    def _validate(t: Tau, where: str = "全局") -> None:
        """P0-07：数值域 0 ≤ low ≤ high ≤ 1——负数/大于 1 的 typo 会把低置信规则
        候选放进 auto（外审动态探针已复现），必须在构造期拦死。"""
        if not (0.0 <= t.low <= t.high <= 1.0):
            raise ValueError(f"{where} τ 非法：须 0 ≤ low({t.low}) ≤ high({t.high}) ≤ 1")

    def __init__(self, default: Tau = DEFAULT_TAU,
                 layered: Optional[Dict[tuple, Tau]] = None):
        self._validate(default)
        for key, t in (layered or {}).items():
            self._validate(t, where=f"分层 {key}")
        self._default = default
        self._layered = dict(layered or {})

    @classmethod
    def from_env(cls, *, strict: bool = False) -> "TauTable":
        """strict=True（P0-07，离线写 worker——seeding/backfill/may_auto_activate）：
        任何非法 τ 配置 **raise 阻断**，绝不回落到可 auto 的默认值——配置坏了宁可
        停写，不能拿默认阈值继续 auto。strict=False（在线 resolve，纯读）保持
        fail-open 回默认：阈值只影响候选标注，配坏不该让查询瘫掉。"""
        try:
            high = float(os.environ.get("RAG_ONTOLOGY_TAU_HIGH", DEFAULT_TAU.high))
            low = float(os.environ.get("RAG_ONTOLOGY_TAU_LOW", DEFAULT_TAU.low))
            default = Tau(high=high, low=low)
            cls._validate(default)
        except Exception as e:   # noqa: BLE001
            if strict:
                raise ValueError(f"RAG_ONTOLOGY_TAU_HIGH/LOW 非法（strict 模式阻断写 worker）：{e}")
            logger.warning("RAG_ONTOLOGY_TAU_HIGH/LOW 非法，回落默认 %s", DEFAULT_TAU)
            default = DEFAULT_TAU
        layered: Dict[tuple, Tau] = {}
        raw = os.environ.get("RAG_ONTOLOGY_TAU_TABLE", "").strip()
        if raw:
            try:
                table = json.loads(raw)
            except Exception as e:   # noqa: BLE001
                if strict:
                    raise ValueError(f"RAG_ONTOLOGY_TAU_TABLE 非法 JSON（strict 模式阻断）：{e}")
                logger.warning("RAG_ONTOLOGY_TAU_TABLE 非法 JSON，整表忽略")
                table = {}
            for key, pair in table.items():
                try:
                    prefix, method = key.split("|", 1)
                    t = Tau(high=float(pair[0]), low=float(pair[1]))
                    cls._validate(t, where=f"分层 {key}")
                    layered[(prefix.strip(), method.strip())] = t
                except Exception as e:   # noqa: BLE001
                    if strict:
                        raise ValueError(
                            f"RAG_ONTOLOGY_TAU_TABLE 条目非法（strict 模式阻断）：{key!r}: {e}")
                    logger.warning("RAG_ONTOLOGY_TAU_TABLE 条目非法，跳过: %r", key)
        return cls(default=default, layered=layered)

    def lookup(self, namespace: str, method: str) -> Tau:
        prefix = dispatch_key(namespace)
        for key in ((prefix, method), (prefix, "*"), ("*", method)):
            hit = self._layered.get(key)
            if hit is not None:
                return hit
        return self._default


# ── 结果契约 ─────────────────────────────────────────────────────────────────
@dataclass
class Candidate:
    target_object_id: str
    method: str                       # rule | kie | embedding
    confidence: float
    target_revision: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)
    # 展示件（来自 ontology_object；密级掩码在工具层按 ctx 做，服务层不感知身份）
    canonical_ref: Optional[str] = None
    title: Optional[str] = None
    object_type: Optional[str] = None
    owner_dept: Optional[str] = None
    data_classification: Optional[str] = None


@dataclass
class ResolveResult:
    status: str                       # resolved | candidate | unresolved
    namespace: str
    raw_value: str
    norm_value: str
    intent: str
    confidence: float = 0.0
    method: Optional[str] = None      # resolved 时=exact
    requires_hitl: bool = False       # write 意图恒 True；read 下非 resolved 即 True
    object_id: Optional[str] = None
    target_revision: Optional[str] = None
    canonical_ref: Optional[str] = None
    title: Optional[str] = None
    object_type: Optional[str] = None
    owner_dept: Optional[str] = None
    data_classification: Optional[str] = None
    candidates: List[Candidate] = field(default_factory=list)


# ── 离线 auto 资格（S1：唯一允许把"自动"变成"落库"的判定点；在线绝不调用）─────────
def auto_activation_enabled() -> bool:
    """P0-07 auto 硬关（默认候选-only）：需当日有效的
    `RAG_ONTOLOGY_AUTO_ACK=<op>:<YYYY-MM-DD>:<gt_manifest_hash>`（**token 只能 Sam 设**，
    对齐 RAG_ALLOW_UNFROZEN_RECHUNK 的 date-bound 纪律）。GT 分层标定签字前该 token
    物理上无法合法构造（hash 由 PR14 backtest 工具按 GT manifest 打印）——机器化
    「签字前 auto 不可能开」。缺失/过期/畸形 → 一切候选（含 0.96 同型同名）进 case。"""
    raw = os.environ.get("RAG_ONTOLOGY_AUTO_ACK", "").strip()
    if not raw:
        return False
    parts = raw.split(":")
    if len(parts) != 3:
        logger.warning("RAG_ONTOLOGY_AUTO_ACK 格式非法（op:date:gt_hash），auto 保持关闭")
        return False
    op, date_s, gt_hash = (p.strip() for p in parts)
    from datetime import date
    if not op or date_s != date.today().isoformat():
        logger.warning("RAG_ONTOLOGY_AUTO_ACK 非当日/op 缺失，auto 保持关闭")
        return False
    if len(gt_hash) < 8:
        logger.warning("RAG_ONTOLOGY_AUTO_ACK gt_manifest_hash 过短，auto 保持关闭")
        return False
    return True


def may_auto_activate(candidates: Sequence[Candidate], *, intent: str, namespace: str,
                      tau_table: Optional[TauTable] = None) -> Optional[Candidate]:
    """闸 0（P0-07 硬关）+ 三禁 + 唯一性：auto ack 缺失恒否（候选-only 默认）·
    write 意图恒否 · embedding 恒否 · 多候选（不同目标且 ≥ 各自 τ_low）恒否；
    Top 候选 ≥ τ_high 且非 embedding 且目标唯一 → 返回该候选（调用方落库时标
    confirmed_by='auto' 并入抽检队列）。仅播种/回填等离线路径调用；τ 走 strict
    校验（非法配置 raise 阻断写 worker，绝不回落可 auto 的默认值）。"""
    if not auto_activation_enabled():
        return None
    if intent != "read":
        return None
    if not candidates:
        return None
    tau_table = tau_table or TauTable.from_env(strict=True)
    top = max(candidates, key=lambda c: c.confidence)
    if top.method == "embedding":
        return None
    if top.confidence < tau_table.lookup(namespace, top.method).high:
        return None
    for c in candidates:
        if c is top or c.target_object_id == top.target_object_id:
            continue   # 同目标多方法=互相印证，不算竞争
        if c.confidence >= tau_table.lookup(namespace, c.method).low:
            return None   # 存在竞争候选 → 人审
    return top


# ── 解析器 ───────────────────────────────────────────────────────────────────
_USE_DEFAULT_EMBEDDER = object()


class OntologyResolver:
    """在线消解（纯读）。store=OntologyStore 双后端任一；embedder 可注入
    （缺省=retriever.get_query_embedding 的 dense 分量，惰性加载失败则关闭 embedding 源；
    显式传 None=关闭）。"""

    def __init__(self, store, *, embedder: Any = _USE_DEFAULT_EMBEDDER,
                 tau_table: Optional[TauTable] = None, embed_pool_limit: int = 200):
        self._store = store
        self._embedder_spec = embedder
        self._embedder_resolved = False
        self._embedder: Optional[Callable[[str], Optional[Sequence[float]]]] = None
        self._tau = tau_table or TauTable.from_env()
        self._pool_limit = max(1, min(int(embed_pool_limit), 200))
        self._title_vecs: Dict[str, Sequence[float]] = {}

    # -- 主入口 ------------------------------------------------------------
    def resolve(self, namespace: str, raw: str, *, intent: str = "read",
                object_type_hint: Optional[str] = None) -> ResolveResult:
        if intent not in ("read", "write"):
            raise ValueError(f"intent 须为 read|write，得到 {intent!r}")
        norm = normalize(namespace, raw)

        hit = self._store.get_active_identifier(namespace, norm)
        if hit is not None:
            obj = self._store.get_object(hit["target_object_id"])
            # P0-03 生命周期闸（对齐 sem.py 的 active 校验）：目标缺失/retired/merged 的
            # active 别名是治理待处置态，绝不能返回 resolved 把退役身份当权威喂下游。
            # 纯读契约不变——不落库不建 case，fail-closed 为 unresolved 交人审。
            if obj is None or obj.get("status") != "active":
                return ResolveResult(
                    status="unresolved", namespace=namespace, raw_value=raw,
                    norm_value=norm, intent=intent, requires_hitl=True)
            return ResolveResult(
                status="resolved", namespace=namespace, raw_value=raw, norm_value=norm,
                intent=intent, confidence=1.0, method="exact",
                requires_hitl=(intent == "write"),   # 写意图精确命中也须人审确认
                object_id=hit["target_object_id"],
                target_revision=hit.get("target_revision"),
                canonical_ref=obj.get("canonical_ref"), title=obj.get("title"),
                object_type=obj.get("object_type"), owner_dept=obj.get("owner_dept"),
                data_classification=obj.get("data_classification"))

        candidates = self._rule_candidates(namespace, norm)
        candidates += self._embedding_candidates(namespace, raw, object_type_hint)
        # 同 (目标, 方法) 去重取大，按置信降序，截断
        best: Dict[tuple, Candidate] = {}
        for c in candidates:
            key = (c.target_object_id, c.method)
            if key not in best or c.confidence > best[key].confidence:
                best[key] = c
        ordered = sorted(best.values(), key=lambda c: -c.confidence)[:_MAX_CANDIDATES]

        has_viable = any(
            c.confidence >= self._tau.lookup(namespace, c.method).low for c in ordered)
        status = "candidate" if has_viable else "unresolved"
        return ResolveResult(
            status=status, namespace=namespace, raw_value=raw, norm_value=norm,
            intent=intent, requires_hitl=True,   # 非 resolved：喂任何下游写一律人审
            confidence=ordered[0].confidence if ordered else 0.0,
            candidates=ordered)

    # -- rule 源：剥改模后缀提示 ---------------------------------------------
    def _rule_candidates(self, namespace: str, norm: str) -> List[Candidate]:
        out: List[Candidate] = []
        for suffix in _REVISION_SUFFIXES:
            if not norm.endswith(suffix) or len(norm) <= len(suffix):
                continue
            base = norm[: -len(suffix)]
            base_hit = self._store.get_active_identifier(namespace, base)
            if base_hit is None:
                continue
            obj = self._store.get_object(base_hit["target_object_id"]) or {}
            if obj.get("status") != "active":   # P0-03：退役/合并目标不作候选
                continue
            out.append(Candidate(
                target_object_id=base_hit["target_object_id"], method="rule",
                confidence=_RULE_SUFFIX_CONF,
                features={"rule": "strip_suffix", "suffix": suffix, "base_norm": base,
                          "hint": "疑似同 Product 不同 Revision（改模判定默认人审）"},
                canonical_ref=obj.get("canonical_ref"), title=obj.get("title"),
                object_type=obj.get("object_type"), owner_dept=obj.get("owner_dept"),
                data_classification=obj.get("data_classification")))
        return out

    # -- embedding 源：品名相似 ----------------------------------------------
    def _embedding_candidates(self, namespace: str, raw: str,
                              object_type_hint: Optional[str]) -> List[Candidate]:
        embedder = self._get_embedder()
        if embedder is None:
            return []
        try:
            qv = embedder(raw.strip())
        except Exception:   # noqa: BLE001 — embedding 失败只降级为无该源候选
            logger.warning("query embedding 失败（跳过 embedding 候选源）", exc_info=True)
            return []
        if not qv:
            return []
        tau = self._tau.lookup(namespace, "embedding")
        cap = tau.high - _EMBEDDING_HIGH_GAP     # 构造性 < τ_high：embedding 永够不着 auto 线
        pool_types = (object_type_hint,) if object_type_hint else _DEFAULT_POOL_TYPES
        out: List[Candidate] = []
        for otype in pool_types:
            objs = self._store.find_objects(otype, limit=self._pool_limit)
            if len(objs) >= self._pool_limit:
                # P1「召回截断可观测」：池打满=有对象根本没进候选，静默截断读作"覆盖了全部"
                logger.warning("embedding 候选池 %s 打满上限 %d——超出部分未参与消解"
                               "（对象量级已超 P0 试点设计，考虑 HA3 子索引）",
                               otype, self._pool_limit)
            # P1「embedding 出域」：confidential 标题**默认不出域**——池子在调 provider
            # 之前先按密级过滤，机密品名/价目名绝不 POST 给外部 embedding 服务
            #（该类对象的消解只走 exact/rule/工作台人工，不因此丢正确性只丢便利）。
            objs = [o for o in objs if o.get("data_classification") != "confidential"]
            for obj in objs:
                tv = self._title_vec(embedder, obj["title"])
                if tv is None:
                    continue
                sim = max(0.0, _cosine(qv, tv))
                conf = min(sim, cap)
                if conf < tau.low:
                    continue
                out.append(Candidate(
                    target_object_id=obj["object_id"], method="embedding",
                    confidence=round(conf, 4), features={"similarity": round(sim, 4)},
                    canonical_ref=obj.get("canonical_ref"), title=obj.get("title"),
                    object_type=obj.get("object_type"), owner_dept=obj.get("owner_dept"),
                    data_classification=obj.get("data_classification")))
        return out

    def _title_vec(self, embedder, title: str) -> Optional[Sequence[float]]:
        if title in self._title_vecs:
            return self._title_vecs[title]
        try:
            vec = embedder(title)
        except Exception:   # noqa: BLE001
            return None
        if vec:
            if len(self._title_vecs) >= _TITLE_CACHE_CAP:
                self._title_vecs.pop(next(iter(self._title_vecs)))
            self._title_vecs[title] = vec
        return vec

    def _get_embedder(self):
        if self._embedder_resolved:
            return self._embedder
        self._embedder_resolved = True
        if self._embedder_spec is None:
            self._embedder = None
        elif self._embedder_spec is not _USE_DEFAULT_EMBEDDER:
            self._embedder = self._embedder_spec
        else:
            self._embedder = _default_embedder()
        return self._embedder


def _default_embedder() -> Optional[Callable[[str], Optional[Sequence[float]]]]:
    """缺省 embedder：retriever.get_query_embedding 的 dense 分量（SIM 下自动 hash 向量）。
    惰性 import；不可用→None（embedding 候选源整体关闭，其余源照常，fail-open）。"""
    try:
        from opensearch_pipeline.retriever import get_query_embedding
    except Exception:   # noqa: BLE001
        logger.warning("retriever.get_query_embedding 不可用，embedding 候选源关闭")
        return None

    def _embed(text: str) -> Optional[Sequence[float]]:
        dense, _indices, _values = get_query_embedding(text)
        return dense

    return _embed


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
