# -*- coding: utf-8 -*-
"""
registry.py — ToolRegistry（v2 报告 §4 模块 C）

双通道（Qwen-Agent 模式）：**实例注入优先**（register 进程内可执行工具）+ **DB 元数据兜底/治理**
（tool_registry 表，schema/022）。进程内只是缓存；治理事实在 DB。

- resolve(name) → 可执行 EnterpriseTool（executor 的 adjudicator 用它拿工具执行）；
- kill switch：disable(name) 全局停用某工具（报告 §I / P3 写回必备，agent_admin 权限）；
- list_specs(ctx) → 对模型可见的工具契约（ctx 角色可见性过滤留待 Policy，先返回 active）；
- drift_check(db_rows)：启动时代码内声明 vs tool_registry 表比对 → 漂移告警。

框架模块，**不 import 任何业务工具**；内置工具在 agent_tools 侧经 build_default_registry 注册
（依赖方向 agent_tools → agent_runtime）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from opensearch_pipeline.agent_runtime.tool import EnterpriseTool, ToolSpec


class ToolNotFound(KeyError):
    """请求的工具名/版本未注册。"""


class ToolDisabled(RuntimeError):
    """工具被 kill switch 停用（agent_admin 一键停用）。"""


class ToolRegistry:
    def __init__(self) -> None:
        self._latest: Dict[str, EnterpriseTool] = {}                 # name → 最新版
        self._versioned: Dict[Tuple[str, str], EnterpriseTool] = {}  # (name,version) → tool
        self._disabled: set = set()                                  # kill switch（name）

    # ── 注册 / 解析 ─────────────────────────────────────────────
    def register(self, tool: EnterpriseTool) -> None:
        spec = tool.spec
        self._latest[spec.name] = tool
        self._versioned[(spec.name, spec.version)] = tool

    def get(self, name: str, version: Optional[str] = None) -> Optional[EnterpriseTool]:
        if version is not None:
            return self._versioned.get((name, version))
        return self._latest.get(name)

    def resolve(self, name: str, version: Optional[str] = None) -> EnterpriseTool:
        """拿可执行工具。未注册→ToolNotFound；被 kill switch 停用→ToolDisabled。

        授权（该 ctx 能否调）由 PolicyEngine 裁决，不在此——registry 只负责名→工具解析。
        """
        if name in self._disabled:
            raise ToolDisabled(name)
        tool = self.get(name, version)
        if tool is None:
            raise ToolNotFound(name)
        return tool

    # ── kill switch ─────────────────────────────────────────────
    def disable(self, name: str) -> None:
        self._disabled.add(name)

    def enable(self, name: str) -> None:
        self._disabled.discard(name)

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    # ── 列举 / 治理 ─────────────────────────────────────────────
    def list_specs(self, ctx: Any = None) -> List[ToolSpec]:
        """对模型可见的工具契约（active、非 deprecated、非 disabled）。

        ctx 角色可见性过滤留待 PolicyEngine（可见集 = 该 ctx 可能被 ALLOW 的工具），本步先返回全集。
        """
        return [t.spec for t in self._latest.values()
                if not t.spec.deprecated and t.spec.name not in self._disabled]

    def to_registry_rows(self, registered_by: str = "platform") -> List[Dict[str, Any]]:
        """代码内声明 → tool_registry 表行（治理同步用；实际写库走 admin API）。"""
        import json
        rows = []
        for (name, version), tool in self._versioned.items():
            spec = tool.spec
            status = "disabled" if name in self._disabled else ("deprecated" if spec.deprecated else "active")
            rows.append({
                "tool_name": name, "version": version,
                "spec_json": json.dumps(_spec_to_dict(spec), ensure_ascii=False),
                "risk_level": spec.risk_level.value, "permission_scope": spec.permission_scope,
                "owner_team": spec.owner_team or registered_by, "status": status,
                "registered_by": registered_by,
            })
        return rows

    def drift_check(self, db_rows: List[Dict[str, Any]]) -> List[str]:
        """启动时代码声明 vs tool_registry 表比对，返回漂移告警（报告 §4⑥）。"""
        code_keys = set(self._versioned.keys())
        db_keys = {(r.get("tool_name"), r.get("version")) for r in db_rows}
        warnings = []
        for k in sorted(code_keys - db_keys):
            warnings.append(f"代码已注册但 DB 无记录: {k[0]}@{k[1]}")
        for k in sorted(db_keys - code_keys):
            warnings.append(f"DB 有记录但代码未注册: {k[0]}@{k[1]}")
        return warnings


def _spec_to_dict(spec: ToolSpec) -> Dict[str, Any]:
    from dataclasses import asdict
    d = asdict(spec)
    d["risk_level"] = spec.risk_level.value   # StrEnum → 值
    return d
