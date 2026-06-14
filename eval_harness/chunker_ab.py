# -*- coding: utf-8 -*-
"""chunker_ab.py — chunker A/B 评测框架核心(v3.1 Step C)

四层 Tier 架构(详 Plan ~/.claude/plans/a-b-rustling-hopper.md):

  Tier 0  BINDING_ONLY    — 复用 scripts/eval_image_binding_pdf.py(双跑 OFF/ON + 对比)
  Tier 1  QUICK_INJECT    — gold-anchored conditional generation(retrieval-free)
  Tier 2  FULL_REINDEX    — 双索引完整 e2e(本地 docker OS, run_manifest 不可变)
  Tier 3  STAGING         — 影子 HA3 / 影子 serving(可选,staging-only guard 保护)

本模块只提供"框架核心 + CLI 入口 + BINDING_ONLY 落地实现"。
QUICK_INJECT/FULL_REINDEX 是 scaffolding(API 占位,有清晰 NotImplementedError
指引下一步),给 Step E/F 实施时填实。

单一变量铁律: 两 arm 仅 `arm.env` + `arm.ctx_overrides` 差异。子进程 worker 隔离
env(避免 `@lru_cache get_config()` 缓存污染),config_fingerprint 跨 arm 校验。

CLI 示例:
    python -m eval_harness.chunker_ab \\
        --mode binding_only \\
        --arm 'off' --arm-env 'off:' \\
        --arm 'on'  --arm-env 'on:RAG_IMAGE_CONTENT_OVERRIDE=1' \\
        --gt-file ~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_analysis.json \\
        --docs-dir ~/Downloads/opensearch-rag-data/eval_samples/documents \\
        --out eval_harness/reports/chunker_ab_d8_tier0_$(date +%Y%m%d_%H%M)
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# ──────────────────────────── enums / dataclasses ────────────────────────────


class Mode(str, Enum):
    BINDING_ONLY = "binding_only"      # Tier 0
    QUICK_INJECT = "quick_inject"      # Tier 1
    FULL_REINDEX = "full_reindex"      # Tier 2(+staging via --staging)


@dataclass
class Arm:
    """单 arm 配置(off / on / ...)."""
    name: str
    env: Dict[str, str] = field(default_factory=dict)            # 进程 env override
    ctx_overrides: Dict[str, Any] = field(default_factory=dict)  # chunker ctx patch
    index_name: Optional[str] = None                              # Tier 2 OS index
    serving_base: Optional[str] = None                            # Tier 2 serving URL
    config_fingerprint: Optional[str] = None                      # worker 填
    effective_env: Dict[str, str] = field(default_factory=dict)   # worker 填


@dataclass
class SemanticChunkSig:
    """单 chunk 的语义签名(不含 chunk_id,容忍 image_ref 差异).

    用于 TopologyFingerprint 配对 — set 比较而非顺序比较.
    """
    chunk_type: str
    page_span: Optional[Tuple[int, int]]
    section_path: Tuple[str, ...]
    text_len: int
    image_ref_keys: Tuple[Tuple[str, ...], ...]   # set-like, hashable, 用于 D8 diff 统计

    def is_compatible(self, other: "SemanticChunkSig") -> Tuple[bool, str]:
        if self.chunk_type != other.chunk_type:
            return False, f"chunk_type {self.chunk_type} != {other.chunk_type}"
        if self.section_path != other.section_path:
            return False, "section_path mismatch"
        if self.page_span and other.page_span:
            ovl = _span_overlap(self.page_span, other.page_span)
            if ovl < 0.95:
                return False, f"page overlap {ovl:.2f} < 0.95"
        denom = max(self.text_len, 1)
        if abs(self.text_len - other.text_len) / denom > 0.20:
            return False, f"text_len {self.text_len} vs {other.text_len} > 20% delta"
        # image_ref_keys 差异允许(D8 改动目标)— 不校验
        return True, "ok"


@dataclass
class TopologyFingerprint:
    """doc 级 topology(v2 #6 + v3.1 #4): semantic key map 配对."""
    doc_id: str
    semantic_chunks: Dict[Tuple, SemanticChunkSig] = field(default_factory=dict)

    def is_pairable_with(self, other: "TopologyFingerprint") -> Tuple[bool, List[str]]:
        keys_self = set(self.semantic_chunks.keys())
        keys_other = set(other.semantic_chunks.keys())
        if keys_self != keys_other:
            missing = (keys_self - keys_other) | (keys_other - keys_self)
            return False, [f"semantic key mismatch: {list(missing)[:3]}"]
        failures: List[str] = []
        for k in keys_self:
            ok, reason = self.semantic_chunks[k].is_compatible(other.semantic_chunks[k])
            if not ok:
                failures.append(f"{k}: {reason}")
        return (len(failures) == 0), failures


@dataclass
class ComparisonReport:
    """Tier 0/1/2 通用对比报告."""
    mode: str
    arms: List[str]
    metrics: Dict[str, Dict[str, Any]]      # {arm: {metric: value}}
    deltas: Dict[str, Any]                   # ON-OFF 各维度 delta + CI
    win_tie_loss: Dict[str, Dict[str, int]]  # {metric: {win, tie, loss}}
    per_case: List[Dict[str, Any]]
    topology_check: Dict[str, Any]           # {pairable: bool, doc_failures: [...]}
    validity_notes: List[str]                # 效度边界声明
    meta: Dict[str, Any]

    def to_markdown(self) -> str:
        lines = [
            f"# chunker A/B report — mode={self.mode}",
            "",
            f"- arms: {', '.join(self.arms)}",
            f"- git_commit: {self.meta.get('git_commit', '?')[:12]}",
            f"- timestamp: {self.meta.get('timestamp', '?')}",
            f"- seed: {self.meta.get('seed', '?')}",
            "",
        ]
        if self.validity_notes:
            lines += ["## Validity notes", ""]
            for note in self.validity_notes:
                lines.append(f"- {note}")
            lines.append("")
        if self.topology_check:
            tc = self.topology_check
            mark = "✓ pairable" if tc.get("all_pairable") else "⚠️ NOT pairable"
            lines += ["## Topology check", "", f"- {mark}",
                      f"- docs total: {tc.get('n_docs', 0)} pairable: "
                      f"{tc.get('n_pairable', 0)} failed: {tc.get('n_failed', 0)}"]
            for fail in tc.get("doc_failures", [])[:10]:
                lines.append(f"  - {fail}")
            lines.append("")
        lines += ["## Metrics (per arm)", "", "| metric | " +
                  " | ".join(self.arms) + " | Δ(ON-OFF) |", "|" + "---|" *
                  (len(self.arms) + 2)]
        all_metrics = sorted({m for d in self.metrics.values() for m in d.keys()})
        for m in all_metrics:
            vals = [self.metrics.get(a, {}).get(m) for a in self.arms]
            delta = self.deltas.get(m)
            row = [m]
            for v in vals:
                row.append(f"{v:.4f}" if isinstance(v, float) else str(v))
            if isinstance(delta, dict):
                d = delta.get("delta")
                row.append(f"{d:+.4f}" if isinstance(d, float) else str(delta))
            else:
                row.append(f"{delta:+.4f}" if isinstance(delta, float) else "-")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        if self.win_tie_loss:
            lines += ["## Win/Tie/Loss (per metric)", ""]
            for metric, wtl in self.win_tie_loss.items():
                lines.append(f"- **{metric}**: win={wtl.get('win', 0)} "
                             f"tie={wtl.get('tie', 0)} loss={wtl.get('loss', 0)}")
            lines.append("")
        if self.per_case:
            lines += [f"## Per-case ({len(self.per_case)} rows)", "",
                      f"_dumped to per_case.json_"]
        return "\n".join(lines)

    def save(self, out_dir: Path) -> Path:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(self.to_markdown(), encoding="utf-8")
        (out_dir / "report.json").write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "per_case.json").write_text(
            json.dumps(self.per_case, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_dir / "report.md"


# ──────────────────────────── helpers ────────────────────────────


def _span_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    """两 [start, end] 区间的重叠率(短者长度为分母)."""
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    if hi < lo:
        return 0.0
    short = min(a[1] - a[0] + 1, b[1] - b[0] + 1)
    return (hi - lo + 1) / max(short, 1)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent.parent
        ).decode().strip()
    except Exception:
        return "unknown"


def _chunk_to_sig(c: Any) -> Tuple[Tuple, SemanticChunkSig]:
    """Chunk → (semantic_key, SemanticChunkSig).

    semantic_key 设计:
      (chunk_type, step_no, sub_no, section_no, sequence_no)
    顺序无关(后续放 dict 里),同 D8 改动允许 image_refs 差异.
    """
    if isinstance(c, dict):
        gv = lambda k, d=None: c.get(k, d)
        extra = c.get("extra") or {}
    else:
        gv = lambda k, d=None: getattr(c, k, d)
        extra = getattr(c, "extra", None) or {}
    chunk_type = gv("chunk_type") or "?"
    step_no = extra.get("step_no")
    sub_no = extra.get("sub_no")
    section_no = extra.get("section_no")
    seq = gv("seq_no") if gv("seq_no") is not None else gv("chunk_index")
    key = (chunk_type, step_no, sub_no, section_no, seq)

    pn = extra.get("page_num") or extra.get("page")
    span = (int(pn), int(pn)) if isinstance(pn, int) else None
    sec_path = tuple((extra.get("section_path") or []))
    text = gv("chunk_text") or ""
    img_refs = extra.get("image_refs") or []
    ref_keys: List[Tuple[str, ...]] = []
    for r in img_refs:
        if not isinstance(r, dict):
            continue
        rk = tuple(str(r.get(k, "")) for k in ("oss_key", "source_image",
                                                "image_index", "page_num",
                                                "anchor_row"))
        ref_keys.append(rk)
    sig = SemanticChunkSig(
        chunk_type=chunk_type,
        page_span=span,
        section_path=sec_path,
        text_len=len(text),
        image_ref_keys=tuple(sorted(ref_keys)),
    )
    return key, sig


def build_topology(doc_id: str, chunks: List[Any]) -> TopologyFingerprint:
    """从 chunks 列表构建 doc 级 TopologyFingerprint."""
    semantic: Dict[Tuple, SemanticChunkSig] = {}
    for i, c in enumerate(chunks):
        key, sig = _chunk_to_sig(c)
        # 同 semantic_key 撞车(理论不应该,但 fallback):后缀化避免吞失
        if key in semantic:
            key = key + (i,)
        semantic[key] = sig
    return TopologyFingerprint(doc_id=doc_id, semantic_chunks=semantic)


def check_topology_pairing(
    topo_by_arm: Dict[str, Dict[str, TopologyFingerprint]],
    arms: List[str],
) -> Dict[str, Any]:
    """两 arm topology 配对(doc 级 + 总览)."""
    assert len(arms) == 2, "check_topology_pairing 当前只支持双 arm"
    arm_a, arm_b = arms
    docs = sorted(set(topo_by_arm[arm_a]) & set(topo_by_arm[arm_b]))
    pairable_docs: List[str] = []
    failed: List[str] = []
    doc_failures: List[str] = []
    for d in docs:
        ok, fail = topo_by_arm[arm_a][d].is_pairable_with(topo_by_arm[arm_b][d])
        if ok:
            pairable_docs.append(d)
        else:
            failed.append(d)
            for r in fail[:3]:
                doc_failures.append(f"{d}: {r}")
    return {
        "all_pairable": len(failed) == 0,
        "n_docs": len(docs),
        "n_pairable": len(pairable_docs),
        "n_failed": len(failed),
        "pairable_docs": pairable_docs,
        "failed_docs": failed,
        "doc_failures": doc_failures,
        # arm 独有的 doc(未在另一 arm 出现)
        "only_in_a": sorted(set(topo_by_arm[arm_a]) - set(topo_by_arm[arm_b])),
        "only_in_b": sorted(set(topo_by_arm[arm_b]) - set(topo_by_arm[arm_a])),
    }


# ──────────────────────────── ChunkerAB core ────────────────────────────


class ChunkerAB:
    """A/B 评测协调器.

    BINDING_ONLY: shell 包装 eval_image_binding_pdf.py(子进程跑 OFF/ON)+ 对比
    QUICK_INJECT: Tier 1 conditional gen(Step E)
    FULL_REINDEX: Tier 2 双索引 + serving(Step F)
    """

    def __init__(
        self,
        *,
        mode: Mode,
        arms: List[Arm],
        out_dir: Path,
        seed: int = 20260614,
        validity_notes: Optional[List[str]] = None,
    ):
        self.mode = mode
        self.arms = arms
        self.out_dir = Path(out_dir)
        self.seed = seed
        self.validity_notes = list(validity_notes or [])
        if len(arms) != 2:
            raise ValueError("当前 chunker_ab.py 框架只支持双 arm (off / on)")

    # ── Tier 0: BINDING_ONLY ──

    def run_binding_only(
        self,
        *,
        gt_file: Optional[str],
        docs_dir: str,
    ) -> ComparisonReport:
        """Tier 0: 子进程跑两次 scripts/eval_image_binding_pdf.py(env 隔离).

        每次跑 dump 一个 binding_pdf_<ts>.json,parse 出 per_fmt.pdf.mean_jaccard
        + per_doc 逐题.对比 ON-OFF.
        """
        script = Path(__file__).parent.parent / "scripts" / "eval_image_binding_pdf.py"
        if not script.exists():
            raise FileNotFoundError(f"binding 脚本不存在: {script}")

        results: Dict[str, Dict[str, Any]] = {}
        run_dir = self.out_dir
        run_dir.mkdir(parents=True, exist_ok=True)

        for arm in self.arms:
            arm_out = run_dir / f"binding_{arm.name}.json"
            cmd = [sys.executable, str(script), "--docs-dir", docs_dir,
                   "--out", str(arm_out)]
            if gt_file:
                cmd += ["--gt-file", gt_file]
            env = {**os.environ, **arm.env, "RAG_EVAL_MODE": "1"}
            # 子进程级 env 隔离 — 避免 lru_cache get_config() 污染
            print(f"[BINDING_ONLY arm={arm.name}] running {script.name} ...")
            subprocess.run(cmd, env=env, check=True, cwd=script.parent.parent)
            with open(arm_out, encoding="utf-8") as f:
                results[arm.name] = json.load(f)

        return self._compare_binding(results)

    def _compare_binding(self, results: Dict[str, Dict[str, Any]]) -> ComparisonReport:
        """parse 两个 binding json + 出 ComparisonReport."""
        metrics: Dict[str, Dict[str, Any]] = {}
        per_case: List[Dict[str, Any]] = []
        for arm_name, r in results.items():
            determ = r.get("deterministic", {})
            pdf = determ.get("per_fmt", {}).get("pdf", {}) or {}
            metrics[arm_name] = {
                "mean_jaccard_pdf": pdf.get("mean_jaccard", float("nan")),
                "n_strong_chunks": pdf.get("n_strong_chunks", 0),
                "n_docs": pdf.get("n_docs", 0),
                "std_jaccard_pdf": pdf.get("std_jaccard"),
                "img_dup_p95": determ.get("img_dup_factor_p95"),
                "img_dup_max": determ.get("img_dup_factor_max"),
            }
        # per_case: 逐 (doc_label, gt_label) 配对
        a, b = [arm.name for arm in self.arms]
        per_doc_a = {d["label"]: d for d in results[a].get("per_doc", [])}
        per_doc_b = {d["label"]: d for d in results[b].get("per_doc", [])}
        common_docs = sorted(set(per_doc_a) & set(per_doc_b))
        for label in common_docs:
            da, db = per_doc_a[label], per_doc_b[label]
            pcs_a = {pc.get("gt_label"): pc for pc in da.get("per_chunk", [])}
            pcs_b = {pc.get("gt_label"): pc for pc in db.get("per_chunk", [])}
            for gt_label in sorted(set(pcs_a) & set(pcs_b)):
                pa, pb = pcs_a[gt_label], pcs_b[gt_label]
                if pa.get("weak") or pb.get("weak"):
                    continue
                ja, jb = pa.get("jaccard"), pb.get("jaccard")
                if ja is None or jb is None:
                    continue
                per_case.append({
                    "doc_id": label, "gt_label": gt_label,
                    f"jaccard_{a}": ja, f"jaccard_{b}": jb,
                    "delta": jb - ja,
                })
        # delta + win/tie/loss
        wins = sum(1 for r in per_case if r["delta"] > 0.001)
        losses = sum(1 for r in per_case if r["delta"] < -0.001)
        ties = len(per_case) - wins - losses
        m_a = metrics[a].get("mean_jaccard_pdf") or 0.0
        m_b = metrics[b].get("mean_jaccard_pdf") or 0.0
        deltas = {"mean_jaccard_pdf": {"delta": m_b - m_a}}
        win_tie_loss = {"jaccard_pdf": {"win": wins, "tie": ties, "loss": losses}}
        return ComparisonReport(
            mode=self.mode.value,
            arms=[a, b],
            metrics=metrics,
            deltas=deltas,
            win_tie_loss=win_tie_loss,
            per_case=per_case,
            topology_check={},   # BINDING_ONLY 不做 topology check(chunks 不暴露)
            validity_notes=self.validity_notes + [
                "Tier 0 BINDING_ONLY uses funnel image_index Jaccard (regression "
                "reference). Semantic anchor primary Jaccard (v3 #15) requires "
                "Step C+ anchor GT — see plan.md.",
            ],
            meta={
                "git_commit": _git_commit(),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "seed": self.seed,
                "arms": [asdict(arm) for arm in self.arms],
            },
        )

    # ── Tier 1: QUICK_INJECT(stub for Step E)──

    def run_quick_inject(self, *args, **kwargs):
        raise NotImplementedError(
            "Tier 1 QUICK_INJECT 实施位于 Step E(plan ~/.claude/plans/a-b-rustling-hopper.md)。\n"
            "依赖:\n"
            "  1. eval_harness/chunker_ab_worker.py(已建,produce_chunks)— 双 arm 产 chunks\n"
            "  2. gt_pdf_semantic_anchors.json(Step C+ 待补 5 PDF × ~6 anchor)\n"
            "  3. semantic anchor → arm chunks 解析(复用 _match_gt_chunk_to_produced)\n"
            "  4. 等量 budget 拼 context → LLM 生成 → judge L3\n"
            "建议先跑 BINDING_ONLY 与 anchor GT 补标 ,再回填 QUICK_INJECT."
        )

    # ── Tier 2: FULL_REINDEX(stub for Step F)──

    def run_full_reindex(self, *args, **kwargs):
        raise NotImplementedError(
            "Tier 2 FULL_REINDEX 实施位于 Step F(plan ~/.claude/plans/a-b-rustling-hopper.md)。\n"
            "依赖:\n"
            "  1. scratch/local_chunker_ab_ingest.py(待建 — 调 worker × 2 arm + 灌 docker OS)\n"
            "  2. run_manifest.RunManifest.create+save(已建)\n"
            "  3. 双 serving 启停 + /api/health/detail config fingerprint 比对\n"
            "  4. ABBA crossover 采集 + embedding cache(scratch/local_ab_eval.py 模板)\n"
            "  5. judge panel × 3 评委盲评\n"
            "建议先跑 BINDING_ONLY + QUICK_INJECT,通过后再上 FULL_REINDEX."
        )

    # ── 顶层 dispatch ──

    def run(self, **kwargs) -> ComparisonReport:
        if self.mode == Mode.BINDING_ONLY:
            return self.run_binding_only(**kwargs)
        if self.mode == Mode.QUICK_INJECT:
            return self.run_quick_inject(**kwargs)
        if self.mode == Mode.FULL_REINDEX:
            return self.run_full_reindex(**kwargs)
        raise ValueError(f"unknown mode: {self.mode}")


# ──────────────────────────── CLI ────────────────────────────


def _parse_arm_env(spec: str) -> Tuple[str, Dict[str, str]]:
    """`name:K1=V1,K2=V2` → (name, {K1: V1, K2: V2}).

    空 env 允许:`off:` → ('off', {}).
    """
    if ":" not in spec:
        raise ValueError(f"--arm-env 格式必须是 'name:K=V[,K=V]': {spec!r}")
    name, rest = spec.split(":", 1)
    env: Dict[str, str] = {}
    if rest.strip():
        for kv in rest.split(","):
            if "=" not in kv:
                raise ValueError(f"--arm-env 项必须是 K=V: {kv!r}")
            k, v = kv.split("=", 1)
            env[k.strip()] = v.strip()
    return name.strip(), env


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="chunker_ab",
        description="chunker A/B 评测框架(v3.1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--mode", required=True,
                    choices=[m.value for m in Mode],
                    help="binding_only(Tier 0)/ quick_inject(Tier 1)/ full_reindex(Tier 2)")
    ap.add_argument("--arm", action="append", required=True,
                    help="arm 名(可重复 — 必须 2 个,如 'off' 'on')")
    ap.add_argument("--arm-env", action="append", default=[],
                    help="arm env override: 'name:K1=V1,K2=V2'(可重复)。空 env: 'off:'")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--seed", type=int, default=20260614)
    # Tier 0 specific
    ap.add_argument("--gt-file", help="(binding_only)PDF GT 文件路径")
    ap.add_argument("--docs-dir", help="(binding_only)文档目录")
    # Tier 1/2 specific(Step E/F 实施时启用)
    ap.add_argument("--doc-dir", action="append", default=[],
                    help="(quick_inject/full_reindex)文档目录(可重复)")
    ap.add_argument("--goldset", help="(quick_inject/full_reindex)goldset 主文件")
    ap.add_argument("--goldset-supplementary", action="append", default=[],
                    help="(quick_inject/full_reindex)goldset 补充文件(可重复)")
    ap.add_argument("--stratify", help="分层抽样 spec: 'pdf:15,docx:10,xlsx:5,pptx:0'")
    ap.add_argument("--positive-only", action="store_true",
                    help="(quick_inject)Tier 1 只跑正例(负例无 anchor)")
    ap.add_argument("--layers", help="(quick_inject/full_reindex)L0/L1/L2/L3/L4")
    # Tier 2 specific
    ap.add_argument("--ingest-only", action="store_true")
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--bundle", action="store_true")
    ap.add_argument("--resume", help="(collect-only)run_id 用于校验 run_manifest")
    ap.add_argument("--run-id", help="(ingest-only)指定 run_id")
    ap.add_argument("--manifest-out", help="(ingest-only)run_manifest 落盘路径")
    ap.add_argument("--crossover", choices=["ABBA"], default="ABBA",
                    help="(collect-only)ABBA randomized crossover")
    ap.add_argument("--embedding-cache", help="(collect-only)embedding cache sqlite 路径")
    ap.add_argument("--staging", action="store_true",
                    help="Tier 3 — 启 staging-only guard(env_guard.assert_staging_eval_mode)")
    args = ap.parse_args(argv)

    if len(args.arm) != 2:
        ap.error("--arm 必须出现 2 次(双 arm,如 'off' 与 'on')")

    # 解析 arm-env(spec 比 --arm 多 → 报错)
    env_by_arm: Dict[str, Dict[str, str]] = {}
    for spec in args.arm_env:
        name, env = _parse_arm_env(spec)
        env_by_arm[name] = env
    arms: List[Arm] = []
    for name in args.arm:
        arms.append(Arm(name=name, env=env_by_arm.get(name, {})))

    mode = Mode(args.mode)
    out_dir = Path(args.out).expanduser()

    validity_notes: List[str] = []
    if mode == Mode.QUICK_INJECT and args.positive_only:
        validity_notes.append(
            "Tier 1 positive-only: 负例剥离(v3 #12)— Tier 1 是 retrieval-free conditional "
            "generation, 负例无 semantic anchor 强塞无意义. 拒答能力在 Tier 2 测.")
    if mode == Mode.BINDING_ONLY:
        if not args.gt_file or not args.docs_dir:
            print("⚠️  binding_only mode 通常需 --gt-file + --docs-dir;"
                  "未给则用 eval_image_binding_pdf.py 默认值(~/Downloads/...).")

    print(f"[chunker_ab] mode={mode.value} arms={[a.name for a in arms]} seed={args.seed}")
    print(f"[chunker_ab] arm.env:")
    for a in arms:
        print(f"  {a.name}: {a.env or '(empty)'}")

    runner = ChunkerAB(mode=mode, arms=arms, out_dir=out_dir,
                       seed=args.seed, validity_notes=validity_notes)

    if mode == Mode.BINDING_ONLY:
        gt_file = args.gt_file
        docs_dir = args.docs_dir or os.path.expanduser(
            "~/Downloads/opensearch-rag-data/eval_samples/documents")
        report = runner.run_binding_only(gt_file=gt_file, docs_dir=docs_dir)
    elif mode == Mode.QUICK_INJECT:
        # Step E 实施位 — 暂 raise 引导用户
        report = runner.run_quick_inject()
    elif mode == Mode.FULL_REINDEX:
        # Step F 实施位 — 暂 raise 引导用户
        report = runner.run_full_reindex()
    else:
        raise ValueError(f"unknown mode {mode}")

    saved = report.save(out_dir)
    print(f"\n✓ report saved to {saved}")
    print(report.to_markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
