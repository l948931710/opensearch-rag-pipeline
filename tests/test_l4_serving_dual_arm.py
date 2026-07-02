# -*- coding: utf-8 -*-
"""L4-serving 双臂评测（#F-mm1b, 2026-07-01）结构回归。

主臂 cosurface_images=False = 生产主流量姿态（/api/ask 小程序 + 钉钉 bot），进闸；
对照臂 True = /api/ask/stream 姿态，落 arms.cosurface_true（trend-only）。
顶层 aggregate/per_query/judge_bundle_mm 键 = 主臂 —— report.py:l4.get('aggregate')
与 baseline extract_metrics 的 l4srv.* 读取路径契约不能动。
"""

from unittest.mock import patch

import os as _os

# ⚠️ envboot 隔离（#F-mm13 排雷）：eval_harness.run_eval / layers.* 顶部 import envboot,
# boot() on-import 会把进程 env 强制成 live（RAG_SIMULATE=false + 公网 HA3 端点）——
# 测试进程内一旦泄漏,任何后续从 env 重建 config 的测试（test_config_loading/guards 族）
# 都会被 conftest PROD-GUARD 拒跑,且重建出的毒化单例会级联炸掉整个套件。
# 快照-导入-回滚：envboot 只 boot 一次（模块缓存）,env 写入立即撤销;本文件全部用
# mock 检索/生成,不需要 live env。
_env_snapshot = dict(_os.environ)
from eval_harness.layers import l4_multimodal  # noqa: E402

for _k in set(_os.environ) - set(_env_snapshot):
    del _os.environ[_k]
_os.environ.update(_env_snapshot)
del _env_snapshot


def _cases():
    return [
        {"qid": "Q1", "query": "销售出库单怎么开？", "expect_images": True,
         "live_scorable": True, "query_breadth": "specific"},
        {"qid": "Q2", "query": "U8+成品仓库怎么操作？", "expect_images": True,
         "live_scorable": True, "query_breadth": "broad"},
    ]


def _fake_chunks(query, top_k=7, user_dept=None, cosurface_images=False, **kw):
    chunks = [{"chunk_type": "step_card", "doc_id": "D1", "title": "t",
               "image_refs": [{"oss_key": "a.png", "caption": "图"}]}]
    if cosurface_images:
        # 对照臂多补一个 image chunk（模拟 cosurface 生效）
        chunks.append({"chunk_type": "image", "doc_id": "D2",
                       "source_image": "b.png", "visual_summary": "补图", "title": "t2"})
    return chunks


def _fake_gen(query, chunks, pure_text=False):
    return {"answer": "第一步如图 <<IMG:1>> 所示。"}


def _run(monkeypatch, env_arm=None):
    if env_arm is None:
        monkeypatch.delenv("EVAL_L4_COSURFACE_ARM", raising=False)
    else:
        monkeypatch.setenv("EVAL_L4_COSURFACE_ARM", env_arm)
    calls = []

    def _spy_retrieve(query, top_k=7, user_dept=None, cosurface_images=False, **kw):
        calls.append(cosurface_images)
        return _fake_chunks(query, top_k, user_dept, cosurface_images, **kw)

    with patch("opensearch_pipeline.retriever.retrieve_and_enrich", _spy_retrieve), \
         patch("eval_harness.layers.l4_multimodal.generate_answer_nothink", _fake_gen):
        out = l4_multimodal.run(_cases())
    return out, calls


def test_main_arm_is_production_posture(monkeypatch):
    out, calls = _run(monkeypatch)
    assert out["applicable"] is True
    assert out["posture"] == "cosurface_images=False"
    # 主臂在前（False×2），对照臂在后（True×2）
    assert calls == [False, False, True, True]
    # 顶层键 = 主臂（report/baseline 路径契约）
    assert out["aggregate"]["n_answers"] == 2
    assert out["aggregate"]["answer_image_rate"] == 1.0
    assert len(out["judge_bundle_mm"]) == 2


def test_cosurface_arm_present_and_trend_only(monkeypatch):
    out, _ = _run(monkeypatch)
    arm = out["arms"]["cosurface_true"]
    assert arm["posture"] == "cosurface_images=True"
    assert arm["aggregate"]["n_answers"] == 2
    # 对照臂不产 judge bundle（评审只评主臂）
    assert "judge_bundle_mm" not in arm


def test_cosurface_arm_disabled_by_env(monkeypatch):
    out, calls = _run(monkeypatch, env_arm="false")
    assert out["arms"] == {}
    assert calls == [False, False]          # 只跑主臂，省一半 LLM 费用


def test_by_breadth_stratification(monkeypatch):
    out, _ = _run(monkeypatch)
    bb = out["by_breadth"]
    assert set(bb) == {"broad", "specific"}
    assert bb["broad"]["n"] == 1 and bb["specific"]["n"] == 1
    assert bb["broad"]["answer_image_rate"] == 1.0


def test_no_breadth_labels_no_stratification(monkeypatch):
    cases = [{"qid": "Q1", "query": "q", "expect_images": True, "live_scorable": True}]
    monkeypatch.delenv("EVAL_L4_COSURFACE_ARM", raising=False)
    with patch("opensearch_pipeline.retriever.retrieve_and_enrich", _fake_chunks), \
         patch("eval_harness.layers.l4_multimodal.generate_answer_nothink", _fake_gen):
        out = l4_multimodal.run(cases)
    assert out.get("by_breadth") is None
