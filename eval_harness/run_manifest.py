# -*- coding: utf-8 -*-
"""run_manifest.py — chunker A/B Tier 2 不可变 run manifest(v3 #13)

Why immutable manifest:
    Tier 2 把 ingest-only(灌入双索引)和 collect-only(query+judge)拆开方便排错,
    但拆开后存在 "ingest 跑完 → 偷偷改代码 → collect 跑 → 结果脏" 的污染风险.
    引入不可变 run_manifest:
      - ingest-only 跑完写 manifest(run_id, git_commit, config_hash, arm_indexes,
        chunk_artifact_paths, sample_manifest, seed, timestamp)
      - collect-only 必须 --resume <run_id> 加载同 manifest,跑前校验 git_commit
        与 当前 一致,索引仍存在,sample_manifest 哈希一致.
    任一校验失败 → fail-loud,不让脏数据出报告.

文件路径:
    eval_harness/reports/chunker_ab_<tier>_<run_id>/run_manifest.json
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ManifestError(RuntimeError):
    """run_manifest 校验失败."""


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent.parent
        ).decode().strip()
    except Exception:
        return "unknown"


def _hash_sample(sample_qids: List[str]) -> str:
    """sample qids 列表的 sha256(顺序敏感,避免重抽样未察觉)."""
    blob = "\n".join(sample_qids).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class RunManifest:
    """v3 #13 — Tier 2 不可变 manifest."""

    run_id: str
    tier: str                                  # 'tier2_local' / 'tier3_staging'
    timestamp_iso: str
    git_commit: str
    seed: int
    sample_qids: List[str]                     # 抽到的 qids 顺序固定
    sample_hash: str                           # qids 顺序 sha256(校验用)
    arm_indexes: Dict[str, str]                # {'off': 'locale2e_chunkab_off_...', 'on': '...'}
    arm_chunk_paths: Dict[str, str]            # {'off': '/path/chunks_off.pkl', 'on': '...'}
    arm_effective_env: Dict[str, Dict[str, str]] # {'off': {RAG_EVAL_MODE:1, ...}, 'on': {...}}
    arm_config_fingerprint: Dict[str, str]     # {'off': sha16, 'on': sha16}(必须一致除 arm flag)
    doc_pool_dirs: List[str]
    doc_pool_stats: Dict[str, int]             # {pdf:5, docx:51, ...}
    stratify_target: Dict[str, int]            # {pdf:15, docx:10, ...}
    out_dir: str

    @classmethod
    def create(cls, *,
               tier: str, sample_qids: List[str], arm_indexes: Dict[str, str],
               arm_chunk_paths: Dict[str, str],
               arm_effective_env: Dict[str, Dict[str, str]],
               arm_config_fingerprint: Dict[str, str],
               doc_pool_dirs: List[str], doc_pool_stats: Dict[str, int],
               stratify_target: Dict[str, int], out_dir: Path,
               run_id: Optional[str] = None, seed: int = 20260614) -> "RunManifest":
        if run_id is None:
            run_id = f"{tier}_{time.strftime('%Y%m%d_%H%M%S')}"
        return cls(
            run_id=run_id, tier=tier,
            timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%S"),
            git_commit=_git_commit(),
            seed=seed,
            sample_qids=list(sample_qids),
            sample_hash=_hash_sample(sample_qids),
            arm_indexes=dict(arm_indexes),
            arm_chunk_paths=dict(arm_chunk_paths),
            arm_effective_env={k: dict(v) for k, v in arm_effective_env.items()},
            arm_config_fingerprint=dict(arm_config_fingerprint),
            doc_pool_dirs=[str(p) for p in doc_pool_dirs],
            doc_pool_stats=dict(doc_pool_stats),
            stratify_target=dict(stratify_target),
            out_dir=str(out_dir),
        )

    def save(self, path: Optional[Path] = None) -> Path:
        if path is None:
            path = Path(self.out_dir) / "run_manifest.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> "RunManifest":
        data = json.loads(Path(path).read_text())
        return cls(**data)

    def verify_resume(self) -> None:
        """collect-only --resume 时校验.

        Raises ManifestError 任一项不匹配:
          1. 当前 git_commit 与 manifest 一致(ingest 后没偷偷改代码)
          2. arm_indexes 引用的索引在 OS 里仍存在
          3. sample_hash 与 sample_qids 一致(防 manifest 篡改)
          4. arm_config_fingerprint 双 arm 必须一致(单变量铁律)
        """
        # 1. git commit
        current = _git_commit()
        if current != self.git_commit:
            raise ManifestError(
                f"git_commit mismatch: manifest={self.git_commit[:12]} but "
                f"current={current[:12]}. ingest 之后代码改了 → 必须重跑 ingest.")
        # 2. sample hash
        recompute = _hash_sample(self.sample_qids)
        if recompute != self.sample_hash:
            raise ManifestError(
                f"sample_hash mismatch: stored={self.sample_hash} recomputed={recompute}."
                "manifest 已被篡改或 sample_qids 被改.")
        # 3. config fingerprint 单变量铁律
        fps = list(self.arm_config_fingerprint.values())
        if len(set(fps)) > 1:
            raise ManifestError(
                f"arm_config_fingerprint mismatch: {self.arm_config_fingerprint}. "
                f"两 arm 配置必须一致(除 RAG_IMAGE_CONTENT_OVERRIDE).")
        # 4. arm indexes 仍存在(只校验,不创建)— 留给 chunker_ab.py 实施时调用 OS API
        # (避免本模块 import opensearchpy 增加耦合)
