# -*- coding: utf-8 -*-
"""
tool.py — EnterpriseTool 契约（v2 报告 §4/§5 模块 C）

工具 = ToolSpec（治理元数据 + JSON Schema 契约）+ run(ctx, args) -> ToolResult。
业务工具**绝不 import 任何框架类型**；权限数据面复用现有 ACL（工具 schema 里不出现
user_dept 等身份参数，由 ExecutionContext 注入）。

铁律映射：
- ToolSpec frozen → 注册后不可变（模型/调用方不可篡改风险等级/权限域）。
- 写型工具（LOW_WRITE/HIGH_WRITE）强制 idempotency=key_required（重放/重试不重复副作用）。
- input/output_schema 注册期即校验为合法 JSON Schema；入参执行前过 schema 校验层。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:  # 运行期不导入：context.py 由 Task 8 落地，注解经 __future__ 为字符串不求值
    from opensearch_pipeline.agent_runtime.context import ExecutionContext


class RiskLevel(str, Enum):
    """工具风险等级 —— 驱动 Policy 默认策略（HIGH_WRITE 一律 REQUIRE_APPROVAL）。"""

    READ_ONLY = "read_only"
    LOW_WRITE = "low_write"
    HIGH_WRITE = "high_write"


class ToolSpecError(ValueError):
    """ToolSpec 注册期校验失败（schema 非法 / 写型工具缺幂等键 / 名版为空）。"""


class ToolArgsError(ValueError):
    """工具入参不符合 input_schema（executor schema 校验层拒绝，先于 Policy）。"""


@dataclass(frozen=True)
class ToolSpec:
    """工具契约 + 治理元数据。构造即校验（无法造出非法 spec）。

    字段顺序与 v2 报告 §5 一致。data_classification 无默认（必填，驱动数据出境闸 D2）。
    """

    name: str
    version: str                                   # 语义化版本；运行记录固化 name@version
    description: str
    input_schema: Dict[str, Any]                   # JSON Schema（注册期校验合法性）
    output_schema: Dict[str, Any]
    risk_level: RiskLevel
    permission_scope: str                          # Policy 裁决键，如 "kb.search" / "u8.writeback"
    data_classification: Literal["public", "internal", "confidential"]
    timeout_s: float = 30.0
    max_retries: int = 0
    idempotency: Literal["none", "natural", "key_required"] = "none"   # 写型必须 key_required
    side_effects: bool = False
    approval_policy: Optional[str] = None          # None=按 risk_level 默认；"always"；"policy:<id>"
    owner_team: str = ""                            # N 模块治理：每工具必有 owner（注册期不强制，登记时补）
    concurrency_safe: bool = True                  # 驱动并行调度
    deprecated: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """注册期校验。抛 ToolSpecError。"""
        if not self.name or not self.version:
            raise ToolSpecError("ToolSpec.name/version 不能为空")
        for label, schema in (("input_schema", self.input_schema),
                              ("output_schema", self.output_schema)):
            if not isinstance(schema, dict):
                raise ToolSpecError(f"{label} 必须是 JSON Schema dict，收到 {type(schema).__name__}")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as e:
                raise ToolSpecError(f"{label} 不是合法 JSON Schema: {e.message}") from e
        # 写型工具的幂等键是重放/重试不重复副作用的兜底（报告 §5）
        if self.risk_level in (RiskLevel.LOW_WRITE, RiskLevel.HIGH_WRITE) \
                and self.idempotency != "key_required":
            raise ToolSpecError(
                f"写型工具 {self.name}（risk={self.risk_level.value}）必须 idempotency=key_required")

    def validate_args(self, args: Dict[str, Any]) -> None:
        """执行前入参校验（executor 中间件第一层，先于 Policy）。不符抛 ToolArgsError。

        input_schema 建议带 additionalProperties:false —— 这样请求体伪造的越权参数
        （如 user_dept，本应由 ExecutionContext 注入）会被直接拒绝。
        """
        try:
            Draft202012Validator(self.input_schema).validate(args)
        except ValidationError as e:
            raise ToolArgsError(f"工具 {self.name} 入参校验失败: {e.message}") from e

    @property
    def qualified_name(self) -> str:
        """运行记录固化用 name@version。"""
        return f"{self.name}@{self.version}"


class ContentBlock(BaseModel):
    """工具结果的一段内容（文本 / 表格 / 图片引用），回灌模型前可截断。"""

    type: Literal["text", "table", "image_ref"]
    text: Optional[str] = None
    table: Optional[Dict[str, Any]] = None         # {"columns": [...], "rows": [[...], ...]}
    image_ref: Optional[str] = None                # OSS key / URL

    @model_validator(mode="after")
    def _check_payload(self) -> "ContentBlock":
        payload = {"text": self.text, "table": self.table, "image_ref": self.image_ref}
        if payload[self.type] is None:
            raise ValueError(f"ContentBlock type={self.type} 需要对应字段非空")
        return self

    @classmethod
    def of_text(cls, text: str) -> "ContentBlock":
        return cls(type="text", text=text)

    @classmethod
    def of_table(cls, columns: List[str], rows: List[List[Any]]) -> "ContentBlock":
        return cls(type="table", table={"columns": columns, "rows": rows})

    @classmethod
    def of_image(cls, ref: str) -> "ContentBlock":
        return cls(type="image_ref", image_ref=ref)


class ToolResult(BaseModel):
    """工具执行结果（统一回执）。写型工具的 receipt 供对账。"""

    status: Literal["succeeded", "failed", "denied", "pending_approval"]
    content: List[ContentBlock] = Field(default_factory=list)
    receipt: Optional[Dict[str, Any]] = None       # 写型工具执行回执（对账用）
    error: Optional[str] = None
    # 进程内旁路载荷（如 knowledge_search 的检索 chunks，供 serving 层构建
    # sources/content_blocks 帧）。exclude=True：model_dump/digest/落库一律不带——
    # 只活在事件队列里，绝不进 tool_invocation/SSE 线协议。
    artifacts: Optional[Dict[str, Any]] = Field(default=None, exclude=True)

    @classmethod
    def ok(cls, content: Optional[List[ContentBlock]] = None,
           receipt: Optional[Dict[str, Any]] = None) -> "ToolResult":
        return cls(status="succeeded", content=content or [], receipt=receipt)

    @classmethod
    def text_ok(cls, text: str, receipt: Optional[Dict[str, Any]] = None) -> "ToolResult":
        return cls(status="succeeded", content=[ContentBlock.of_text(text)], receipt=receipt)

    @classmethod
    def fail(cls, error: str) -> "ToolResult":
        return cls(status="failed", error=error)

    @classmethod
    def denied(cls, reason: str) -> "ToolResult":
        return cls(status="denied", error=reason)

    @classmethod
    def pending(cls) -> "ToolResult":
        return cls(status="pending_approval")


@runtime_checkable
class EnterpriseTool(Protocol):
    """工具实现契约。业务工具实现它，但**不依赖任何 Agent 框架类型**。

    Loop 只见 ToolSpec 元数据与注入的结果；工具 run 收服务端构造的 ExecutionContext，
    数据面权限由工具内部走既有 ACL（如 knowledge_search 用 ctx.acl_groups 注入过滤）。
    """

    spec: ToolSpec

    def run(self, ctx: "ExecutionContext", args: Dict[str, Any],
            idempotency_key: Optional[str] = None) -> ToolResult:
        ...
