# -*- coding: utf-8 -*-
"""gates — 压测通过判据：既定 SLO（qa_rollup.evaluate_slos 权威）+ DRAFT agent 门 G1–G9。

DRAFT 门未批准（ratification 提案见设计文档 §4）；报告里 draft=True 标注，
FAIL 不阻断 matrix 运行（exit code 由 runner 汇总：任何非 draft 门 FAIL → 2）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GateResult:
    gate: str
    description: str
    threshold: str
    value: str
    passed: Optional[bool]          # None = skipped（如 DB 不可用）
    draft: bool = False
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "description": self.description,
                "threshold": self.threshold, "value": self.value,
                "passed": self.passed, "draft": self.draft, "note": self.note}


@dataclass
class ScenarioResult:
    scenario: str
    title: str
    started_at: str
    duration_s: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    gates: List[GateResult] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def hard_fail(self) -> bool:
        return any(g.passed is False and not g.draft for g in self.gates)

    def as_dict(self) -> Dict[str, Any]:
        return {"scenario": self.scenario, "title": self.title,
                "started_at": self.started_at, "duration_s": round(self.duration_s, 1),
                "metrics": self.metrics, "gates": [g.as_dict() for g in self.gates],
                "findings": self.findings, "notes": self.notes}


def gate(name: str, desc: str, threshold: str, value: Any, passed: Optional[bool],
         draft: bool = False, note: str = "") -> GateResult:
    return GateResult(gate=name, description=desc, threshold=threshold,
                      value=str(value), passed=passed, draft=draft, note=note)


def pctl(values: List[float], p: int) -> Optional[float]:
    """百分位（复用 eval_harness.metrics.percentiles 的 nearest-rank 语义）。"""
    from eval_harness.metrics import percentiles
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return percentiles(xs, ps=(p,))[f"p{p}"]
