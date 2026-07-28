# -*- coding: utf-8 -*-
"""versions.py — single source of pipeline component versions + per-run provenance (Phase-1 L1).

These constants pin the *code revision* of each output-shaping stage so a stored chunk / run
can be traced to (and the re-index scope derived from) the exact producer. They are the keystone
the lineage (kb_audit_log / pipeline_run), per-chunk provenance (chunk_meta.extra_json +
embedding_version), determinism (chunk_set_hash / detector_version), and affected-doc-set diff all
hang off.

BUMP the relevant constant whenever you change that component's OUTPUT (not just refactor):
  - EXTRACTOR_VERSION:      extraction/* change that alters canonical text / blocks / assets
  - CHUNKER_VERSION:        chunking-stage OUTPUT change (chunk text / count / type,
                            含 pre-chunk 的 image_ref 注入——代码虽在 pipeline_nodes，
                            改的是 step_card.image_refs 与随之入 chunk_text 的图注)
  - DETECTOR_VERSION:       the routing/boundary detectors specifically
                            (_CLAUSE_RE / _STEP_DETECT_RE / _detect_heading_level / node_chunk_documents routing)
  - EMBEDDING_MODEL_VERSION: embedding model / dimension / endpoint change

Pure / read-only: no DB, no prod write, no config mutation. Zero behavior change when unread.
"""
from typing import Optional

from opensearch_pipeline.ingest_flags import (
    effective_funnel_policy_version,
    image_content_override_enabled,
    pdf_strip_stitch_enabled,
)

# ── component code-revision pins (bump on OUTPUT change; see module docstring) ──
EXTRACTOR_VERSION = "1.3.0"   # 2026-07-26：RAG_PDF_STRIP_STITCH 默认 ON（条带缝合改变 assets）
CHUNKER_VERSION = "1.3.0"   # 2026-07-25：RAG_IMAGE_CONTENT_OVERRIDE 默认翻 ON（D5=重复圈号 fail-closed）
DETECTOR_VERSION = "1.1.0"          # _CLAUSE_RE / _STEP_DETECT_RE / heading / routing detector revision
                                    # 1.1.0 = PDF-D3：heading 判定新增纯数字标注号 veto
# ⚠️ 手工常量仅作 embedding_regime_version() 的最后兜底（config 完全不可用时）。
# 盲区审计 P3-7：手工 pin 与运行时 RAG_EMBEDDING_MODEL/DIMENSION 脱钩——模型换代时
# 必然过期且无人发现。chunk_meta.embedding_version 的写值与 /api/version 均已改用
# 下面的派生指纹（仿 acl_policy_version 的"内容自变，杜绝忘记 bump"）。
EMBEDDING_MODEL_VERSION = "text-embedding-v4"


def embedding_regime_version() -> str:
    """嵌入制度指纹 `model@dimension`，从实时 config 解析（P3-7）。

    与 acl_policy_version 同哲学：从真值自动派生，模型/维度一变指纹即变，无需手动 bump。
    写入 chunk_meta.embedding_version 作行级溯源；重索引范围选择器（rebuild_from_rds
    --stale-embedding）按行级真值列 embedding_model/embedding_dimension 比对——不比对
    本指纹串，避免历史行（旧常量格式）被误判为陈旧而触发全语料重嵌入。
    任何异常回退手工常量（fail-open，与本模块其余 helper 一致）。"""
    try:
        from opensearch_pipeline.config import get_config
        emb = get_config().embedding
        return f"{emb.model}@{emb.dimension}"
    except Exception:
        return EMBEDDING_MODEL_VERSION


def acl_policy_version() -> str:
    """dept→ACL组 映射策略的【内容指纹】（短 hash）。覆盖全部 5 个映射常量：
    dingtalk_identity._DEPT_NAME_TO_GROUPS / _PRODUCTION_WORKSHOP_DEPTS、
    retriever._VALID_ACL_GROUPS / _PRODUCTION_UMBRELLA_OWNERS / _DEPT_OWNER_EXPANSION。

    任一映射改动 → 版本自动变（内容 hash，无需手动 bump，杜绝忘记的失败模式）。per-doc 授权本就
    审计（kb_audit_log），缺的是「org 级 dept→组映射改动」这一维——本版本号盖进 ACL 审计行即补上。
    惰性 import 避免 import 环；任何异常 → 'unknown'（绝不因版本计算失败影响审计/服务，fail-open）。"""
    import hashlib
    import json
    try:
        from opensearch_pipeline import dingtalk_identity as _di
        from opensearch_pipeline import retriever as _rt
        payload = json.dumps(
            {
                "dept_to_groups": _di._DEPT_NAME_TO_GROUPS,
                "workshop_depts": sorted(_di._PRODUCTION_WORKSHOP_DEPTS),
                "valid_groups": sorted(_rt._VALID_ACL_GROUPS),
                "umbrella_owners": sorted(_rt._PRODUCTION_UMBRELLA_OWNERS),
                "owner_expansion": {k: sorted(v) for k, v in _rt._DEPT_OWNER_EXPANSION.items()},
            },
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return "unknown"


def git_commit() -> str:
    """Best-effort short git SHA. RAG_GIT_SHA env wins (deploy packages have no .git);
    falls back to `git rev-parse` in the repo, then 'unknown'. Never raises."""
    import os
    sha = os.environ.get("RAG_GIT_SHA")
    if sha and sha.strip():
        return sha.strip()
    try:
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def build_run_provenance(stage: Optional[int] = None, bizdate: Optional[str] = None) -> dict:
    """Per-run provenance dict: code/model versions + git sha + bizdate.

    Resolved model NAMES come from the live config factory (get_config), matching what actually
    runs (not the dataclass defaults). Read-only; safe to call anywhere. Callers stash it as
    ctx['run_provenance']; downstream consumers (per-chunk provenance, kb_audit_log, pipeline_run,
    affected-doc-set diff) read from there. Zero behavior change when unread.
    """
    embedding_model = llm_model = None
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        embedding_model = getattr(getattr(cfg, "embedding", None), "model", None)
        llm_model = getattr(getattr(cfg, "llm", None), "model", None)
    except Exception:
        # provenance is auxiliary — never let a config hiccup break the run
        pass
    return {
        "git_commit": git_commit(),
        "stage": stage,
        "bizdate": bizdate,
        "extractor_version": EXTRACTOR_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "detector_version": DETECTOR_VERSION,
        # 图↔步骤绑定制度：存**有效布尔值**而非原始字符串（"" / "1" / "TRUE " 在审计里
        # 长得不一样却同义）。2026-07-25 起默认 True。
        # ⚠️ 该 key 目前只被 pipeline_nodes 的 chunk `_provenance` 白名单消费，
        #    **不进 pipeline_run**（逐列清单，加列需迁移 059）。
        "image_content_override": image_content_override_enabled(),
        # 漏斗弃图判据（选项 C，2026-07-26）。空串 = 历史判据。它决定了**哪些图能进
        # 知识库**，因此必须与 chunk 一起留痕：同一份文档在两套判据下产出的 chunk
        # 图集不同，事后没有这个标签就无法归因。同 image_content_override，
        # **不进 pipeline_run**（逐列清单，加列需迁移，属后续项）。
        "funnel_policy": effective_funnel_policy_version(),
        # 条带缝合改变 assets（EXTRACTOR_VERSION 1.3.0 只反映**默认**姿态）——
        # 显式关掉的 run 必须能与默认 ON 的 run 区分开（审查遗漏项 2026-07-26）。
        "pdf_strip_stitch": pdf_strip_stitch_enabled(),
        "embedding_model_version": EMBEDDING_MODEL_VERSION,
        "embedding_model": embedding_model,
        "llm_model": llm_model,
    }
