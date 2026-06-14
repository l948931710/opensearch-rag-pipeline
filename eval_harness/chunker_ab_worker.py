# -*- coding: utf-8 -*-
"""chunker_ab_worker.py — arm 子进程 worker(v2 #3)

Why subprocess:
    chunker_ab.py 在同进程内跑 2 arm 切 RAG_IMAGE_CONTENT_OVERRIDE 等 env vars
    不可靠 — `@lru_cache get_config()` 第一次调用后缓存配置, contextlib patch
    env 后 chunker 仍读旧 config. 子进程级 env 隔离是 standard practice.

工作流:
    1. parent 通过 stdin 喂 JSON 任务: {"op":"produce_chunks", "arm": "off", ...}
    2. worker 以 arm.env merge 到 os.environ, RAG_EVAL_MODE=1 强制设
    3. worker 调 _extract_and_chunk 产 chunks, dump pickle 到 disk
    4. stdout 返回 {"effective_env": {...}, "config_fingerprint": "...",
                   "chunks_path": "/tmp/...", "git_commit": "..."}

任务类型:
    - produce_chunks: 给 doc_paths 产 chunks (Tier 1/2 都用)
    - inject_index: 本地灌入 (Tier 2 ingest-only,调 local_chunker_ab_ingest.py)

调用方:
    eval_harness.chunker_ab.ChunkerAB.produce_chunks_via_worker
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent.parent
        ).decode().strip()
    except Exception:
        return "unknown"


def _compute_config_fingerprint() -> str:
    """计算当前进程 config 的 fingerprint(去掉 arm 变量后).

    用于 v3 #14 — 两 serving 启动后必须比对 fingerprint 一致(除 index_name + arm flag).
    """
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        # 抽稳定的子集(不含 arm.env 切换的字段)
        fp_items = {
            "embedding_model": getattr(cfg.embedding, "model", None),
            "embedding_dim": getattr(cfg.embedding, "dimension", None),
            "llm_model": getattr(cfg.llm, "model", None) if hasattr(cfg, "llm") else None,
            "rerank_enabled": os.environ.get("RAG_RERANK_ENABLE", ""),
            "rerank_pool_size": os.environ.get("RAG_RERANK_POOL_SIZE", ""),
            "top_k": getattr(cfg.rag, "default_top_k", None) if hasattr(cfg, "rag") else None,
        }
        return hashlib.sha256(json.dumps(fp_items, sort_keys=True).encode()).hexdigest()[:16]
    except Exception:
        return "config_fp_err"


def _run_produce_chunks(task: Dict[str, Any]) -> Dict[str, Any]:
    """跑 produce_chunks 任务.

    Task: {
        "op": "produce_chunks",
        "arm": "off",
        "doc_pool": [{"path": "...", "label": "...", "fmt": "..."}, ...],
    }
    """
    from eval_harness.binding.ingestion_binding import _extract_and_chunk

    arm_name = task["arm"]
    doc_pool = task["doc_pool"]

    chunks_by_doc: Dict[str, List[Any]] = {}
    errors: List[str] = []

    for entry in doc_pool:
        try:
            chunks = _extract_and_chunk(entry["label"], entry["fmt"], entry["path"])
            chunks_by_doc[entry["label"]] = chunks
        except Exception as e:
            errors.append(f"{entry['label']}: {type(e).__name__}: {e}")
            chunks_by_doc[entry["label"]] = []

    # Dump chunks 到 tempfile (pickle,避免 stdout 大对象)
    tmp = tempfile.NamedTemporaryFile(prefix=f"chunkab_chunks_{arm_name}_",
                                      suffix=".pkl", delete=False)
    pickle.dump(chunks_by_doc, tmp)
    tmp.close()

    return {
        "ok": True,
        "arm": arm_name,
        "chunks_path": tmp.name,
        "n_docs": len(chunks_by_doc),
        "total_chunks": sum(len(v) for v in chunks_by_doc.values()),
        "errors": errors,
    }


_OPS = {
    "produce_chunks": _run_produce_chunks,
}


def main():
    """Worker entry: 读 stdin JSON, 跑任务, 写 stdout JSON."""
    try:
        task = json.loads(sys.stdin.read())
        op = task.get("op")
        if op not in _OPS:
            raise ValueError(f"Unknown op: {op!r}, supported: {list(_OPS)}")

        # 强制评测模式
        os.environ["RAG_EVAL_MODE"] = "1"

        # 记 effective env (脱敏 — 不 dump 全 env,只 dump 评测相关 key)
        eval_env_keys = (
            "RAG_EVAL_MODE", "RAG_IMAGE_CONTENT_OVERRIDE", "RAG_ENV",
            "RAG_RERANK_ENABLE", "RAG_VLM_CONCURRENCY",
        )
        effective_env = {k: os.environ.get(k, "") for k in eval_env_keys}

        result = _OPS[op](task)
        result["effective_env"] = effective_env
        result["config_fingerprint"] = _compute_config_fingerprint()
        result["git_commit"] = _git_commit()

        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.exit(0)
    except Exception as e:
        sys.stdout.write(json.dumps({
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }) + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
