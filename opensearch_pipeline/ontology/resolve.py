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

import hashlib
import hmac
import json
import logging
import math
import os
import threading
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
    "auto_eligible",
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
_ACK_MANIFEST_REQUIRED = ("op", "date", "docset", "gt_summary", "signer",
                          "source_sha256", "environment")
_SHA256_HEX_LEN = 64


def auto_activation_enabled(source_fingerprint: Optional[str] = None) -> bool:
    """P0-07 auto 硬关（默认候选-only）+ P1-13 签名 manifest 验签 + **输入/环境绑定**
    （2026-07-11 重审计 §2）。

    旧闸只校验 op、当天日期、hash 长度≥8——「有人设了个环境变量」不构成质量门（外评
    P1-13：任何能设 env 的人可随手伪造）。现在 ack 是**持密钥签发**的：

      `RAG_ONTOLOGY_AUTO_ACK=<manifest_path>:<hmac_sha256_hex>`

    manifest 为 JSON 文件，必填 op / date(YYYY-MM-DD，当日有效) / docset(数据集描述) /
    gt_summary(GT/backtest 结果摘要) / signer / **source_sha256**(快照文件 sha256) /
    **environment**(目标环境名)；签名 = HMAC-SHA256(密钥, manifest 原始字节)，密钥走
    `RAG_ONTOLOGY_ACK_HMAC_KEY`（**密钥与 token 只能 Sam 设**，对齐
    RAG_ALLOW_UNFROZEN_RECHUNK 的 date-bound + docset-bound 纪律）。

    绑定语义（重审计 §2「manifest 未绑输入」：此前 HMAC 只覆盖 manifest 自身，同一
    manifest 可复用于任意 CSV/任意环境）：
    - source_sha256 必须等于**本轮实际读取的快照文件** sha256（source_fingerprint 由
      seeding/backfill 从 CsvSnapshotSource 实算传入；调用方给不出指纹 → auto 恒关）；
    - environment 必须等于当前 get_config().environment（跨环境复用同一签发即拒）。
    无密钥/无 manifest/签名不符/字段缺失/非当日/指纹不符/环境不符 → 一律拒绝，
    auto 保持关闭（候选-only 默认不变）。"""
    raw = os.environ.get("RAG_ONTOLOGY_AUTO_ACK", "").strip()
    if not raw:
        return False
    key = os.environ.get("RAG_ONTOLOGY_ACK_HMAC_KEY", "").strip()
    if not key:
        logger.warning("RAG_ONTOLOGY_AUTO_ACK 已设但 RAG_ONTOLOGY_ACK_HMAC_KEY 缺失——"
                       "无密钥即无合法签发，auto 保持关闭")
        return False
    path, sep, sig = raw.rpartition(":")
    if not sep or not path or len(sig) != 64:   # HMAC-SHA256 hex 恒 64 位
        logger.warning("RAG_ONTOLOGY_AUTO_ACK 格式非法（应为 <manifest_path>:"
                       "<hmac_sha256_hex>），auto 保持关闭")
        return False
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        logger.warning("ack manifest 不可读：%s——auto 保持关闭", path)
        return False
    expect = hmac.new(key.encode("utf-8"), blob, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig.lower()):
        logger.warning("ack manifest 签名不符（manifest 被改动或非本密钥签发），auto 保持关闭")
        return False
    try:
        doc = json.loads(blob.decode("utf-8"))
    except Exception:   # noqa: BLE001
        logger.warning("ack manifest 非合法 JSON，auto 保持关闭")
        return False
    if not isinstance(doc, dict):
        logger.warning("ack manifest 须为 JSON 对象，auto 保持关闭")
        return False
    missing = [k for k in _ACK_MANIFEST_REQUIRED if not str(doc.get(k) or "").strip()]
    if missing:
        logger.warning("ack manifest 缺必填字段 %s，auto 保持关闭", missing)
        return False
    from datetime import date
    if str(doc["date"]).strip() != date.today().isoformat():
        logger.warning("ack manifest 非当日（date=%s）——date-bound 纪律，auto 保持关闭",
                       doc["date"])
        return False
    # 环境绑定（重审计 §2）：staging 签发的 manifest 拿到 prod 复用即拒（fail-closed：
    # 环境读不出也拒——无法证明绑定成立就不放行）。
    try:
        from opensearch_pipeline.config import get_config
        cur_env = str(get_config().environment or "").strip()
    except Exception:   # noqa: BLE001
        cur_env = ""
    if not cur_env or str(doc["environment"]).strip() != cur_env:
        logger.warning("ack manifest 环境不符（manifest=%s，当前=%s）——auto 保持关闭",
                       doc.get("environment"), cur_env or "<读取失败>")
        return False
    # 输入绑定（重审计 §2）：manifest.source_sha256 必须与本轮实读快照文件的 sha256
    # 完全一致。调用方给不出指纹（非文件源/读失败）→ 无从证明绑定 → auto 恒关。
    man_src = str(doc["source_sha256"]).strip().lower()
    if len(man_src) != _SHA256_HEX_LEN or any(c not in "0123456789abcdef" for c in man_src):
        logger.warning("ack manifest source_sha256 非法（须 64 位 hex），auto 保持关闭")
        return False
    fp = (source_fingerprint or "").strip().lower()
    if not fp:
        logger.warning("调用方未提供快照指纹（source_fingerprint）——manifest 输入绑定"
                       "无从验证，auto 保持关闭")
        return False
    if man_src != fp:
        logger.warning("ack manifest source_sha256 与实读快照不符（manifest=%s…，实际=%s…）"
                       "——同一 manifest 不得复用于不同数据集，auto 保持关闭",
                       man_src[:12], fp[:12])
        return False
    return True


def may_auto_activate(candidates: Sequence[Candidate], *, intent: str, namespace: str,
                      tau_table: Optional[TauTable] = None,
                      source_fingerprint: Optional[str] = None) -> Optional[Candidate]:
    """闸 0（P0-07 硬关 + 输入/环境绑定）+ 三禁 + 唯一性：auto ack 缺失/绑定不符恒否
    （候选-only 默认）· write 意图恒否 · embedding 恒否 · 多候选（不同目标且 ≥ 各自
    τ_low）恒否；Top 候选 ≥ τ_high 且非 embedding 且目标唯一 → 返回该候选（调用方
    落库时标 confirmed_by='auto'；抽检复核闭环未建——批次3b）。仅播种/回填等离线路径调用；τ 走
    strict 校验（非法配置 raise 阻断写 worker，绝不回落可 auto 的默认值）。
    source_fingerprint=本轮实读快照的 sha256（seeding.source_fingerprint 实算）——
    manifest 绑定校验用；给不出即 auto 恒关（重审计 §2）。"""
    if not auto_activation_enabled(source_fingerprint=source_fingerprint):
        return None
    return auto_eligible(candidates, intent=intent, namespace=namespace,
                         tau_table=tau_table)


def auto_eligible(candidates: Sequence[Candidate], *, intent: str, namespace: str,
                  tau_table: Optional[TauTable] = None) -> Optional[Candidate]:
    """三禁+唯一性判定的**纯函数部分**（不含 ack/绑定闸）——PR14 backtest 用它离线评估
    「若放行 auto 会自动铸什么」（would-auto），**绝不落库**。任何生产落库路径必须走
    may_auto_activate（签名 manifest + 输入/环境绑定闸在那一层）——本函数不是第二个
    auto 入口，是同一判定的可测切面。"""
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
# in-flight 单飞锁（重审计 §4）：并发 resolve 同时冷启动时只有一个线程去批量补缺，
# 其余等锁后直接吃热缓存——不重复付 DashScope 调用费
_TITLE_VEC_LOCK = threading.Lock()


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
        self._embedder_is_default = False   # 默认 embedder 才走批量+持久缓存（_ensure_title_vecs）

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
            # 重审计 §4：批量 warmup（持久缓存 → native 批量 API）——此前每个标题走
            # get_query_embedding 单发一次 HTTP，冷启动上界 1+非机密对象数（≤801 次）。
            self._ensure_title_vecs(embedder, [o.get("title") or "" for o in objs])
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
        """逐条兜底路径（warmup 未覆盖/批量失败的标题仍逐条 embed，行为向后兼容）。"""
        if title in self._title_vecs:
            return self._title_vecs[title]
        try:
            vec = embedder(title)
        except Exception:   # noqa: BLE001
            return None
        if vec:
            self._store_title_vec(title, vec)
        return vec

    def _store_title_vec(self, title: str, vec: Sequence[float]) -> None:
        if len(self._title_vecs) >= _TITLE_CACHE_CAP:
            self._title_vecs.pop(next(iter(self._title_vecs)))
        self._title_vecs[title] = vec

    def _ensure_title_vecs(self, embedder, titles: List[str]) -> None:
        """批量 warmup（重审计 §4「~801 次冷启动逐条 HTTP」）：持久缓存点查 → miss 走
        embed_texts_native **批量** API（摄取侧同款；逐对象单发是实现缺口非 API 限制）。
        仅默认 embedder 生效（注入 embedder 是单文本契约、模型/维度未知，持久键无意义
        ——逐条路径照旧）。_TITLE_VEC_LOCK 单飞：并发 resolve 不重复付费。纯优化路径：
        任何失败只留 miss 给 _title_vec 逐条兜底，绝不改变候选结果。"""
        if not self._embedder_is_default or embedder is None:
            return
        want = [t for t in dict.fromkeys(titles) if t and t not in self._title_vecs]
        if not want:
            return
        with _TITLE_VEC_LOCK:
            want = [t for t in want if t not in self._title_vecs]
            if not want:
                return
            try:
                from opensearch_pipeline.config import get_config
                cfg = get_config()
                model, dim = cfg.embedding.model, cfg.embedding.dimension

                def _key(t: str) -> str:
                    # 与摄取缓存同一键契约 md5(f"{model}_{dim}_{text}")：同模型同维度的
                    # 同文本向量完全同值（native API 无 query/document 不对称）
                    return hashlib.md5(f"{model}_{dim}_{t}".encode("utf-8")).hexdigest()

                cache = self._persistent_cache()
                if cache is not None:
                    hits = cache.get_many([_key(t) for t in want])
                    for t in want:
                        v = hits.get(_key(t))
                        if isinstance(v, list) and v:
                            self._store_title_vec(t, v)
                    want = [t for t in want if t not in self._title_vecs]
                    if not want:
                        return
                from opensearch_pipeline.embedding_client import embed_texts_native
                bs = max(1, int(getattr(cfg.embedding, "batch_size", 10) or 10))
                fresh: Dict[str, List[float]] = {}
                for i in range(0, len(want), bs):
                    batch = want[i:i + bs]
                    try:
                        res = embed_texts_native(
                            batch, api_key=cfg.embedding.api_key, model=model,
                            dimension=dim, api_base_url=cfg.embedding.api_base_url,
                            sparse_fallback=False, label="ontology title embedding")
                    except Exception:   # noqa: BLE001 — 单批失败留给逐条兜底
                        logger.warning("title 批量 embedding 失败（该批走逐条兜底）",
                                       exc_info=True)
                        continue
                    for t, r in zip(batch, res):
                        if r and r[0]:
                            self._store_title_vec(t, r[0])
                            fresh[_key(t)] = list(r[0])
                if cache is not None and fresh:
                    try:
                        cache.put_many(fresh)
                    except Exception:   # noqa: BLE001 — 持久层是 advisory
                        pass
            except Exception:   # noqa: BLE001 — warmup 整体 fail-open
                logger.warning("title 向量批量 warmup 失败（回退逐条路径）", exc_info=True)

    @staticmethod
    def _persistent_cache():
        """持久层复用摄取 embedding 缓存（SqliteKVStore WAL + 进程单例）：跨进程/跨运行
        复用已算向量。打不开 → None（退化纯进程内 dict，graceful degradation 惯例）。"""
        try:
            from opensearch_pipeline.embedding_cache import get_embedding_cache
            return get_embedding_cache()
        except Exception:   # noqa: BLE001
            return None

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
            self._embedder_is_default = self._embedder is not None
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
