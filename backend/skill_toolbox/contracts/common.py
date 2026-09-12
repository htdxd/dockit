"""领域 Service 的统一内部结果契约（实施计划 §5.1）。

所有 tools/ Service 返回 ``OperationResult``，失败时抛出带稳定 ``code`` 的
``ToolError``；这两个类型是 internal typed API，不依赖 LLM tool-call 数据结构。
LLM-facing 层（llm_tools/）负责把 OperationResult/ToolError 翻译成 tool 文本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal[
    "created", "validated", "cache_hit", "cache_miss", "failed", "skipped"
]


class ToolError(RuntimeError):
    """内部 Service 失败。code 是稳定错误码，message 是人类可读文本。

    retryable 为 True 时建议调用方重试同参数；为 False 时提示改变输入。
    suggestion 是唯一建议，禁止让调用方在多个猜测方案之间循环（计划 §8）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.suggestion = suggestion

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class ArtifactRef:
    """已注册产物。path 是 workspace 相对路径（可交付路径的规范化形式）。"""

    artifact_id: str
    path: str
    kind: str = "file"  # file | docx | pptx | pdf


@dataclass
class OperationResult:
    """所有 Service 返回的统一结构（计划 §5.1）。"""

    ok: bool
    artifact_id: str = ""
    status: Status = "created"
    warnings: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    next_action: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # ---- P2-3：版本化与多模态（计划 §6.1–6.3） ----
    # revision：本次结果对应的不可变版本号（无版本语义的 Service 保持 None）
    revision: int | None = None
    # preview_refs：workspace 相对路径的预览图（与 revision 绑定）
    preview_refs: list[str] = field(default_factory=list)
    # images：可选内联图像（media_type + base64_data），由 Runtime 转 ToolResult.images
    images: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def failed(
        cls,
        issue_code: str,
        message: str,
        *,
        retryable: bool = False,
        suggestion: str | None = None,
    ) -> "OperationResult":
        return cls(
            ok=False,
            status="failed",
            issues=[
                {
                    "code": issue_code,
                    "message": message,
                    "retryable": retryable,
                    "suggestion": suggestion,
                }
            ],
            next_action=suggestion or message,
        )

    @classmethod
    def from_exc(cls, exc: ToolError) -> "OperationResult":
        return cls.failed(
            exc.code,
            str(exc),
            retryable=exc.retryable,
            suggestion=exc.suggestion,
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "artifact_id": self.artifact_id,
            "status": self.status,
            "warnings": self.warnings,
            "issues": self.issues,
            "next_action": self.next_action,
            "data": self.data,
        }
        if self.revision is not None:
            payload["revision"] = self.revision
        if self.preview_refs:
            payload["preview_refs"] = self.preview_refs
        if self.images:
            # 内联图像体积大且对模型无信息量：只报数量与类型，图像走 ToolResult.images
            payload["images"] = [
                {"media_type": img.get("media_type", ""), "inline": True}
                for img in self.images
            ]
        return payload
