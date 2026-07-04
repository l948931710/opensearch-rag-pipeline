# -*- coding: utf-8 -*-
"""DAG 引擎通用跳过钩子（skip_on_truthy / skip_on_empty）回归测试。

背景：dag3_no_work 短路原先硬编码在引擎里（按 dag_id 魔法串 + node.func.__name__
判定），重命名节点函数 / 克隆 DAG id / 装饰器包裹都会静默解除跳过。泛化为
DAGNode(skip_on_truthy=...) 后，领域知识回到 dag_definitions 声明层。
本文件钉死：钩子语义本身 + DAG3 六个下游节点全部声明了该钩子 + 锁节点没有。
"""
from opensearch_pipeline.dag_engine import DAG, DAGNode, NodeStatus
from opensearch_pipeline.dag_definitions import build_dag3_chunk_to_opensearch


def _mk(calls, name):
    def _node(ctx):
        calls.append(name)
        return name
    _node.__name__ = f"node_{name}"
    return _node


def test_skip_on_truthy_skips_node_and_dependents_run_state():
    calls = []
    dag = DAG(dag_id="anydag", name="t")
    dag.add_node(DAGNode("a", "first", _mk(calls, "a")))
    dag.add_node(DAGNode("b", "second", _mk(calls, "b"),
                         depends_on=["a"], skip_on_truthy="no_work"))
    dag.add_node(DAGNode("c", "third", _mk(calls, "c"), depends_on=["b"]))
    dag.run({"no_work": True, "skip_reason": "nothing to do"})
    assert calls == ["a"]  # b 被跳过；c 依赖 SKIPPED 的 b，也跳过
    assert dag.nodes["b"].status is NodeStatus.SKIPPED
    assert "no_work" in (dag.nodes["b"].error or "")
    assert "nothing to do" in (dag.nodes["b"].error or "")
    assert dag.nodes["c"].status is NodeStatus.SKIPPED


def test_skip_on_truthy_falsy_value_runs_normally():
    calls = []
    dag = DAG(dag_id="anydag", name="t")
    dag.add_node(DAGNode("a", "first", _mk(calls, "a"), skip_on_truthy="no_work"))
    dag.run({"no_work": False})
    assert calls == ["a"]
    assert dag.nodes["a"].status is NodeStatus.SUCCESS


def test_dag3_declares_no_work_skip_on_all_downstream_nodes():
    """跳过声明与函数名/dag_id 解耦：改名/克隆不再静默解除（原硬编码的失效模式）。"""
    dag = build_dag3_chunk_to_opensearch()
    lock_node = dag.nodes["00"]
    assert lock_node.skip_on_truthy is None  # 锁节点必须始终执行（它负责置 dag3_no_work）
    for nid in ("01", "02", "03", "04", "04b", "05"):
        assert dag.nodes[nid].skip_on_truthy == "dag3_no_work", nid


def test_dag3_no_work_end_to_end_skips_downstream(monkeypatch):
    """整条 DAG3 在 no-work 下只跑锁节点（不触外部依赖：锁节点被替换为置位桩）。

    注意桩函数名故意 ≠ node_acquire_index_lock —— 原引擎硬编码按 __name__ 豁免锁
    节点，桩会被误跳；泛化后声明跟着节点定义走，桩照常执行。"""
    dag = build_dag3_chunk_to_opensearch()

    def _stub_lock(ctx):
        ctx["dag3_no_work"] = True
        ctx["skip_reason"] = "stub: no locks acquired"
        return {"locked": 0}

    dag.nodes["00"].func = _stub_lock
    result_ctx = dag.run({"valid_chunks": [], "simulate_db": True,
                              "simulate_opensearch": True})
    assert result_ctx["dag3_no_work"] is True
    assert dag.nodes["00"].status is NodeStatus.SUCCESS
    for nid in ("01", "02", "03", "04", "04b", "05"):
        assert dag.nodes[nid].status is NodeStatus.SKIPPED, nid
