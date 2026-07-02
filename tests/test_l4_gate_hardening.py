# -*- coding: utf-8 -*-
"""L4-ingestion 闸固化（#F-mm8）：pdf 升 hard@0.78 + GT 缺席防静默。

(b) pdf 绑定闸 soft@0.70 → hard@0.78：D7 保 soft 的两个理由已消解（GT 3 docs/33
    chunks + Batch-1 修复后实测 0.8556，reports/p3_on_full）。
(c) 期望格式闸缺失防静默：旧行为下 GT 整体缺席（换机/CI 数据仓没挂载）或 l4 整层
    not-applicable 时绑定闸【无声消失】、--strict 照样放行。新语义：只要本 run 请求
    了 l4 层（results 含 "l4" 键），EVAL_L4_EXPECT_FMTS（默认 docx,xlsx,pdf）的闸
    必须存在，缺失 = not_executed → --strict FAIL。守卫特意放在 `if l4:` 块之外。
"""

import os as _os

# ⚠️ envboot 隔离（#F-mm13 排雷）：eval_harness.run_eval / layers.* 顶部 import envboot,
# boot() on-import 会把进程 env 强制成 live（RAG_SIMULATE=false + 公网 HA3 端点）——
# 测试进程内一旦泄漏,任何后续从 env 重建 config 的测试（test_config_loading/guards 族）
# 都会被 conftest PROD-GUARD 拒跑,且重建出的毒化单例会级联炸掉整个套件。
# 快照-导入-回滚：envboot 只 boot 一次（模块缓存）,env 写入立即撤销;本文件全部用
# mock 检索/生成,不需要 live env。
_env_snapshot = dict(_os.environ)
from eval_harness.report import build_gates  # noqa: E402
from eval_harness.run_eval import _strict_failures  # noqa: E402

for _k in set(_os.environ) - set(_env_snapshot):
    del _os.environ[_k]
_os.environ.update(_env_snapshot)
del _env_snapshot


def _r(ing=None, applicable=True):
    l4 = {"applicable": applicable}
    if ing is not None:
        l4["ingestion"] = {"deterministic": ing}
    return {"l4": l4}


def _clear_env(monkeypatch):
    monkeypatch.delenv("EVAL_L4_EXPECT_FMTS", raising=False)


# ═══════════════ (b) pdf hard@0.78 ═══════════════

def test_pdf_gate_is_hard_at_078(monkeypatch):
    _clear_env(monkeypatch)
    g = build_gates(_r({"binding_jaccard_pdf": 0.80, "binding_jaccard_docx": 0.99,
                        "binding_jaccard_xlsx": 0.90}))
    gate = g["binding pdf Jaccard (L4-ing)"]
    assert gate["pass"] is True and "hard" in gate["target"] and ">= 0.78" in gate["target"]


def test_pdf_gate_fails_below_078(monkeypatch):
    _clear_env(monkeypatch)
    g = build_gates(_r({"binding_jaccard_pdf": 0.75, "binding_jaccard_docx": 0.99,
                        "binding_jaccard_xlsx": 0.90}))
    gate = g["binding pdf Jaccard (L4-ing)"]
    assert gate["pass"] is False
    # hard 闸回退必须阻断 --strict（soft 时代 0.75 会放行）
    assert any("binding pdf" in f for f in _strict_failures(g, {}))


def test_pptx_stays_soft(monkeypatch):
    _clear_env(monkeypatch)
    g = build_gates(_r({"binding_jaccard_pptx": 0.5, "binding_jaccard_pdf": 0.9,
                        "binding_jaccard_docx": 0.99, "binding_jaccard_xlsx": 0.90}))
    assert "soft" in g["binding pptx Jaccard (L4-ing)"]["target"]


# ═══════════════ (c) GT 缺席防静默 ═══════════════

def test_missing_all_gt_emits_not_executed(monkeypatch):
    """l4 层跑了但 ingestion 支柱无数据 → 三个期望格式全部 not_executed 且阻断 strict。"""
    _clear_env(monkeypatch)
    g = build_gates(_r(ing=None))
    for fmt in ("docx", "xlsx", "pdf"):
        gate = g[f"binding {fmt} Jaccard (L4-ing)"]
        assert gate["pass"] is None and gate["na_reason"] == "not_executed"
    fails = _strict_failures(g, {})
    assert sum(1 for f in fails if "binding" in f and "not_executed" in f) == 3


def test_l4_not_applicable_still_guarded(monkeypatch):
    """l4 整层 applicable=False（正是旧行为整块跳过的场景）→ 守卫仍触发。"""
    _clear_env(monkeypatch)
    g = build_gates({"l4": {"applicable": False,
                            "note": "cases 里没有 expect_images case 且未提供 gt_files"}})
    assert g["binding pdf Jaccard (L4-ing)"]["na_reason"] == "not_executed"


def test_partial_gt_only_missing_fmts_flagged(monkeypatch):
    """只有 pdf 测到 → docx/xlsx not_executed，pdf 是真实闸不被覆盖。"""
    _clear_env(monkeypatch)
    g = build_gates(_r({"binding_jaccard_pdf": 0.85}))
    assert g["binding pdf Jaccard (L4-ing)"]["pass"] is True          # 真闸
    assert g["binding docx Jaccard (L4-ing)"]["na_reason"] == "not_executed"
    assert g["binding xlsx Jaccard (L4-ing)"]["na_reason"] == "not_executed"


def test_no_l4_layer_no_guard(monkeypatch):
    """本 run 没请求 l4 层（--layers l1,l2,l3）→ 不产闸、不误伤。"""
    _clear_env(monkeypatch)
    g = build_gates({"l1": {"ranking": {"recall@5": 0.9}}})
    assert not any("binding" in k for k in g)


def test_expect_fmts_env_shrink_and_disable(monkeypatch):
    monkeypatch.setenv("EVAL_L4_EXPECT_FMTS", "pdf")
    g = build_gates(_r(ing=None))
    assert "binding pdf Jaccard (L4-ing)" in g
    assert "binding docx Jaccard (L4-ing)" not in g
    monkeypatch.setenv("EVAL_L4_EXPECT_FMTS", "")
    g2 = build_gates(_r(ing=None))
    assert not any("binding" in k for k in g2)
