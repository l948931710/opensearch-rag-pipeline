#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_baseline_freshness.py — 成绩单新鲜度门（RB-05）。

问题（RB-05）：CI 绿 ≠ 成绩单有效。冻结基线绑定的金集 SHA / 模型 / prompt 一旦与
当前代码漂移，release-gate 要到部署前手工跑时才 FATAL，Agent 基线甚至无人发现。
本脚本把「基线过期」变成 CI 可见红灯：秒级、零网络、零 API 费用，任一 FAIL exit 1。

检查项：
  RAG 侧（eval_harness/goldset/baseline.json，main 与分支都有）
    - eval_set_sha  == sha256(当前金集)[:16]（金集默认值须与 deploy/eval_release_gate.sh
      的 GOLDSET 默认保持同源——两处都认 RAG_EVAL_GOLDSET，回退 golden_full.json）
    - llm_model / embedding_model / reranker_models == 当前 config 解析出的默认
      （不硬编码模型名：config 换默认，本检查自动跟随）
    - .env.example 的 RAG_LLM_MODEL == config 默认（文档漂移防复发）
    - advisory：frozen_at 距今 > 60 天 → WARN（时间不是 regime，不 FAIL）
  Agent 侧（eval_harness/agent/baseline.json，仅存在时检查——main 上自动跳过）
    - regime 指纹缺失（旧格式）→ FAIL（指向 refreeze）
    - cases_sha16 / prompt_sha16 / model 三指纹复算比对

修复指令统一指向：docs/eval_release_gate.md（RAG refreeze）与
python -m eval_harness.agent.runner --freeze-baseline <report>（Agent refreeze）。

耦合注记：模型默认值经 load_config() 工厂解析，须有 DashScope key 才走 Qwen 分支——
CI 无真 key 时注入 dummy（DASHSCOPE_API_KEY=freshness-dummy）。若 config 未来对 key
做真实性校验，此处需同步调整。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAG_BASELINE = REPO / "eval_harness" / "goldset" / "baseline.json"
AGENT_BASELINE = REPO / "eval_harness" / "agent" / "baseline.json"
ENV_EXAMPLE = REPO / ".env.example"

RAG_REFREEZE_HINT = ("refreeze 流程见 docs/eval_release_gate.md（live 全量跑 → 人工裁决 → "
                     "make eval-baseline-freeze RESULTS=<rundir>/report.json）")
AGENT_REFREEZE_HINT = ("refreeze：make agent-eval → 人工裁决 → "
                       "python -m eval_harness.agent.runner --freeze-baseline <report.json>")

_results: list = []   # (name, ok: bool|None, detail)  None=WARN


def _check(name: str, ok, detail: str = "") -> None:
    _results.append((name, ok, detail))


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _load_cfg():
    """当前默认模型的唯一权威=load_config() 工厂（读 config.py 的 factory，勿信 dataclass 默认）。"""
    os.environ.setdefault("RAG_SIMULATE", "true")
    os.environ.setdefault("DASHSCOPE_API_KEY", "freshness-dummy")   # 见文件头「耦合注记」
    from opensearch_pipeline.config import load_config
    return load_config()


def check_rag(cfg) -> None:
    if not RAG_BASELINE.exists():
        _check("rag.baseline_exists", False, f"无冻结基线 {RAG_BASELINE}；{RAG_REFREEZE_HINT}")
        return
    base = json.loads(RAG_BASELINE.read_text(encoding="utf-8"))
    regime = base.get("regime") or {}

    goldset = Path(os.environ.get("RAG_EVAL_GOLDSET")
                   or REPO / "eval_harness" / "goldset" / "golden_full.json")
    if goldset.exists():
        cur, frozen = _sha16(goldset), regime.get("eval_set_sha")
        _check("rag.eval_set_sha", cur == frozen,
               f"金集 {goldset.name} 现 sha={cur} vs 冻结 {frozen}——金集已变，成绩单失效；"
               f"{RAG_REFREEZE_HINT}")
    else:
        _check("rag.goldset_exists", False, f"金集缺失 {goldset}")

    av = getattr(cfg, "alibaba_vector", None)
    cur_llm = cfg.llm.model
    _check("rag.llm_model", regime.get("llm_model") == cur_llm,
           f"冻结基线 llm_model={regime.get('llm_model')} vs 当前默认 {cur_llm}——"
           f"拿旧模型的成绩单为新模型背书；{RAG_REFREEZE_HINT}")
    _check("rag.embedding_model", regime.get("embedding_model") == cfg.embedding.model,
           f"冻结 {regime.get('embedding_model')} vs 当前 {cfg.embedding.model}；{RAG_REFREEZE_HINT}")
    if regime.get("rerank_enable") and av is not None:
        cur_rr = f"{getattr(av, 'rerank_text_model', None)}/{getattr(av, 'rerank_vl_model', None)}"
        _check("rag.reranker_models", regime.get("reranker_models") == cur_rr,
               f"冻结 {regime.get('reranker_models')} vs 当前 {cur_rr}；{RAG_REFREEZE_HINT}")
    # VLM 指纹哨兵(2026-07-20 P2)：visual_summary 是 l4ing 绑定分的输入,VLM 换代/缓存
    # 版本提升 = caption 全体重掷,L4 分数不可与冻结值直接比较(0.8917→0.7167 曾烧一晚
    # 二分)。仅当冻结基线已带指纹时比较（老基线宽容,与 baseline._LENIENT_REGIME_KEYS 同款）。
    if regime.get("vlm_model") is not None:
        ocr_cfg = getattr(cfg, "ocr", None)
        cur_vlm = (getattr(ocr_cfg, "vlm_model", None)
                   or getattr(ocr_cfg, "model", None)) if ocr_cfg is not None else None
        _check("rag.vlm_model", regime.get("vlm_model") == cur_vlm,
               f"冻结 vlm_model={regime.get('vlm_model')} vs 当前 {cur_vlm}——VLM 换代后 "
               f"L4 图文绑定分不可比；{RAG_REFREEZE_HINT}")
    if regime.get("vlm_cache_version") is not None:
        cur_ns = os.environ.get("RAG_VLM_CACHE_VERSION", "2").strip() or "bare"
        _check("rag.vlm_cache_version", regime.get("vlm_cache_version") == cur_ns,
               f"冻结 vlm_cache_version={regime.get('vlm_cache_version')} vs 当前 {cur_ns}——"
               f"缓存版本提升=存量 caption 整体重掷,L4 分数不可比；{RAG_REFREEZE_HINT}")
    # L4-GT 指纹(2026-07-20):GT 在 repo 外数据仓,重标会静默移动 l4ing.*。仅当冻结基线
    # 已带指纹时比较;本机数据仓不可达(CI)→ advisory 跳过,不误红。
    if regime.get("l4_gt_sha") is not None:
        try:
            from eval_harness.run_eval import _l4_gt_sha
            cur_gt = _l4_gt_sha()
        except Exception:
            cur_gt = None
        _check("rag.l4_gt_sha",
               (None if cur_gt is None else regime.get("l4_gt_sha") == cur_gt),
               ("L4 GT 数据仓不可达,跳过比对(advisory)" if cur_gt is None else
                f"冻结 l4_gt_sha={regime.get('l4_gt_sha')} vs 当前 {cur_gt}——L4 绑定 GT "
                f"已重标,l4ing.* 不可与冻结值直接比较；{RAG_REFREEZE_HINT}"))

    # 文档漂移防复发（RB-05 D 层）：.env.example 示例模型必须跟 config 默认走
    if ENV_EXAMPLE.exists():
        example = next((ln.split("=", 1)[1].strip()
                        for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
                        if ln.startswith("RAG_LLM_MODEL=")), None)
        _check("docs.env_example_llm", example == cur_llm,
               f".env.example RAG_LLM_MODEL={example} vs config 默认 {cur_llm}——示例配置过期")

    frozen_at = str(base.get("frozen_at") or "")
    try:
        age_d = (datetime.now(timezone.utc)
                 - datetime.strptime(frozen_at, "%Y%m%d_%H%M%S").replace(
                     tzinfo=timezone.utc)).days
        _check("rag.baseline_age", None if age_d > 60 else True,
               f"基线冻结于 {frozen_at}（{age_d} 天前）——advisory，考虑例行 refreeze")
    except ValueError:
        _check("rag.baseline_age", None, f"frozen_at 无法解析：{frozen_at!r}")


def check_agent() -> None:
    if not AGENT_BASELINE.exists():   # main 无 agent 代码——静默跳过
        return
    base = json.loads(AGENT_BASELINE.read_text(encoding="utf-8"))
    regime = base.get("regime") or {}
    if not regime:
        _check("agent.regime_present", False,
               f"agent 基线无 regime 指纹（旧格式，冻结时未记录模型/金集/prompt）；{AGENT_REFREEZE_HINT}")
        return

    cases = AGENT_BASELINE.parent / str(regime.get("cases_file") or "agent_cases.json")
    if cases.exists():
        cur = _sha16(cases)
        _check("agent.cases_sha16", cur == regime.get("cases_sha16"),
               f"金集 {cases.name} 现 sha={cur} vs 冻结 {regime.get('cases_sha16')}；{AGENT_REFREEZE_HINT}")
    else:
        _check("agent.cases_exists", False, f"基线所记金集缺失：{cases}")

    try:
        os.environ["RAG_ONTOLOGY_TOOLS_ENABLE"] = (
            "true" if regime.get("ontology_flag_on") else "false")
        from opensearch_pipeline.routes.agent import _agent_system_prompt
        cur_p = hashlib.sha256(_agent_system_prompt().encode("utf-8")).hexdigest()[:16]
        _check("agent.prompt_sha16", cur_p == regime.get("prompt_sha16"),
               f"系统提示词现 sha={cur_p} vs 冻结 {regime.get('prompt_sha16')}——prompt 已变，"
               f"行为基线不可比；{AGENT_REFREEZE_HINT}")
    except Exception as e:   # noqa: BLE001
        _check("agent.prompt_sha16", False, f"prompt 指纹复算失败：{type(e).__name__}: {e}")

    try:
        from opensearch_pipeline.agent_runtime.model_gateway import _DEFAULT_ROUTES
        cur_m = "→".join(m for _, m in _DEFAULT_ROUTES.get(str(regime.get("tier")), ()))
        _check("agent.model", cur_m == regime.get("model"),
               f"tier={regime.get('tier')} 路由现 {cur_m} vs 冻结 {regime.get('model')}；"
               f"{AGENT_REFREEZE_HINT}")
    except Exception as e:   # noqa: BLE001
        _check("agent.model", False, f"模型路由复算失败：{type(e).__name__}: {e}")


def main() -> int:
    try:
        cfg = _load_cfg()
    except Exception as e:   # noqa: BLE001
        print(f"FAIL config: load_config() 失败——{type(e).__name__}: {e}")
        return 1
    check_rag(cfg)
    check_agent()

    failed = False
    for name, ok, detail in _results:
        if ok is True:
            print(f"PASS {name}")
        elif ok is None:
            print(f"WARN {name}: {detail}")
        else:
            failed = True
            print(f"FAIL {name}: {detail}")
    if failed:
        print("\n⛔ 成绩单已过期（RB-05）：以上 FAIL 项的成绩不再证明当前代码/模型的质量，"
              "按各项提示 refreeze 后此门自动转绿。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
