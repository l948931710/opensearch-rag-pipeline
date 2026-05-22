# -*- coding: utf-8 -*-
"""
dag_engine.py — 轻量 DAG 执行引擎（本地模拟用）

功能：
  - 节点定义：名称、函数、依赖、状态追踪
  - 拓扑排序执行
  - 节点间数据传递（共享 context dict）
  - 执行日志 + 耗时统计
  - ASCII 可视化

不依赖 DataWorks，可在本地 Python 直接运行。
"""

import time
import traceback
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class NodeStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DAGNode:
    """DAG 中的单个节点。"""

    def __init__(
        self,
        node_id: str,
        name: str,
        func: Callable,
        depends_on: Optional[List[str]] = None,
        description: str = "",
        skip_on_empty: Optional[str] = None,
    ):
        self.node_id = node_id
        self.name = name
        self.func = func
        self.depends_on = depends_on or []
        self.description = description
        self.skip_on_empty = skip_on_empty  # context key: 如果为空则跳过

        self.status = NodeStatus.PENDING
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.error: Optional[str] = None
        self.result: Any = None

    @property
    def duration_ms(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return round((self.end_time - self.start_time) * 1000, 1)
        return None


class DAG:
    """有向无环图执行器。"""

    def __init__(self, dag_id: str, name: str, description: str = ""):
        self.dag_id = dag_id
        self.name = name
        self.description = description
        self.nodes: Dict[str, DAGNode] = {}
        self.execution_order: List[str] = []
        self.context: Dict[str, Any] = {}  # 节点间共享上下文
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

    def add_node(self, node: DAGNode) -> "DAG":
        self.nodes[node.node_id] = node
        return self

    def _topological_sort(self) -> List[str]:
        """Kahn's algorithm 拓扑排序。"""
        in_degree = defaultdict(int)
        graph = defaultdict(list)

        for nid, node in self.nodes.items():
            if nid not in in_degree:
                in_degree[nid] = 0
            for dep in node.depends_on:
                graph[dep].append(nid)
                in_degree[nid] += 1

        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        queue.sort()  # 稳定排序
        result = []

        while queue:
            nid = queue.pop(0)
            result.append(nid)
            for child in sorted(graph[nid]):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(result) != len(self.nodes):
            missing = set(self.nodes.keys()) - set(result)
            raise ValueError(f"DAG has cycles or unresolved deps: {missing}")

        return result

    def run(self, initial_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """执行 DAG，返回最终 context。"""
        self.context = dict(initial_context or {})
        self.context["dag_id"] = self.dag_id
        self.execution_order = self._topological_sort()
        self.start_time = time.time()

        print(f"\n{'═' * 70}")
        print(f"  DAG: {self.name} ({self.dag_id})")
        print(f"  Nodes: {len(self.nodes)}  |  Started: {_now()}")
        print(f"{'═' * 70}\n")

        for node_id in self.execution_order:
            node = self.nodes[node_id]
            self._run_node(node)

            # 如果节点失败，跳过所有依赖它的后续节点
            if node.status == NodeStatus.FAILED:
                self._skip_dependents(node_id)

        self.end_time = time.time()
        total_ms = round((self.end_time - self.start_time) * 1000, 1)

        print(f"\n{'═' * 70}")
        print(f"  DAG Completed: {self.name}")
        print(f"  Total: {total_ms}ms  |  Finished: {_now()}")
        print(f"{'═' * 70}\n")

        self.print_summary()
        return self.context

    def _run_node(self, node: DAGNode):
        """执行单个节点。"""
        # 检查依赖状态
        for dep_id in node.depends_on:
            dep_node = self.nodes.get(dep_id)
            if dep_node and dep_node.status in (NodeStatus.FAILED, NodeStatus.SKIPPED):
                node.status = NodeStatus.SKIPPED
                node.error = f"dependency {dep_id} is {dep_node.status.value}"
                _log_node(node, "⏭️")
                return

        # 检查 skip_on_empty 条件
        if node.skip_on_empty:
            val = self.context.get(node.skip_on_empty)
            if not val:
                node.status = NodeStatus.SKIPPED
                node.error = f"context['{node.skip_on_empty}'] is empty"
                _log_node(node, "⏭️")
                return

        # Check dag3_no_work condition specifically for DAG 3 downstream nodes
        if (self.dag_id == "dag3_chunk_to_opensearch" and 
            self.context.get("dag3_no_work") and 
            node.func.__name__ != "node_acquire_index_lock"):
            node.status = NodeStatus.SKIPPED
            node.error = f"skipped because ctx['dag3_no_work'] is True. Reason: {self.context.get('skip_reason', 'No work')}"
            _log_node(node, "⏭️")
            return

        # 执行
        node.status = NodeStatus.RUNNING
        node.start_time = time.time()
        _log_node(node, "▶️")

        try:
            node.result = node.func(self.context)
            node.status = NodeStatus.SUCCESS
            node.end_time = time.time()
            _log_node(node, "✅", f"{node.duration_ms}ms")
        except Exception as e:
            node.status = NodeStatus.FAILED
            node.error = str(e)
            node.end_time = time.time()
            _log_node(node, "❌", f"{node.duration_ms}ms | {e}")
            # 打印完整堆栈用于调试
            print(f"    └─ Traceback:\n{''.join(traceback.format_exc())}")

    def _skip_dependents(self, failed_node_id: str):
        """将依赖失败节点的所有后续节点标记为 SKIPPED。"""
        for node_id in self.execution_order:
            node = self.nodes[node_id]
            if node.status == NodeStatus.PENDING and failed_node_id in node.depends_on:
                node.status = NodeStatus.SKIPPED
                node.error = f"upstream {failed_node_id} failed"

    def print_summary(self):
        """打印执行摘要表格。"""
        print(f"\n{'─' * 70}")
        print(f"  {'Node':<35} {'Status':<12} {'Duration':<12}")
        print(f"  {'─' * 33} {'─' * 10} {'─' * 10}")

        for node_id in self.execution_order:
            node = self.nodes[node_id]
            status_icon = {
                NodeStatus.SUCCESS: "✅",
                NodeStatus.FAILED: "❌",
                NodeStatus.SKIPPED: "⏭️",
                NodeStatus.PENDING: "⏳",
                NodeStatus.RUNNING: "▶️",
            }.get(node.status, "?")

            dur = f"{node.duration_ms}ms" if node.duration_ms else "—"
            label = f"{node.node_id}: {node.name}"
            if len(label) > 33:
                label = label[:30] + "..."
            print(f"  {status_icon} {label:<33} {node.status.value:<12} {dur:<12}")

        # 统计
        counts = defaultdict(int)
        for node in self.nodes.values():
            counts[node.status] += 1

        print(f"\n  Success: {counts[NodeStatus.SUCCESS]}  "
              f"Failed: {counts[NodeStatus.FAILED]}  "
              f"Skipped: {counts[NodeStatus.SKIPPED]}")
        print(f"{'─' * 70}\n")

    def print_dag_graph(self):
        """打印 ASCII DAG 结构图。"""
        print(f"\n  DAG Structure: {self.name}")
        print(f"  {'─' * 50}")

        order = self._topological_sort()
        for i, node_id in enumerate(order):
            node = self.nodes[node_id]
            prefix = "  └─" if i == len(order) - 1 else "  ├─"
            deps = f" (← {', '.join(node.depends_on)})" if node.depends_on else ""
            print(f"  {prefix} [{node.node_id}] {node.name}{deps}")

        print()


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log_node(node: DAGNode, icon: str, extra: str = ""):
    suffix = f" | {extra}" if extra else ""
    print(f"  {icon} [{node.node_id}] {node.name}{suffix}", flush=True)
