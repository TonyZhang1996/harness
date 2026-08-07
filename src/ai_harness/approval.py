"""Approval policies for potentially risky tool actions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class CommandApprover:
    """Approve, deny, or interactively ask before shell execution."""

    VALID_MODES = {"ask", "auto", "never"}

    def __init__(
        self,
        mode: str = "ask",
        prompt: Callable[[str], str] = input,
    ) -> None:
        if mode not in self.VALID_MODES:
            raise ValueError(f"无效审批模式: {mode}")
        self.mode = mode
        self.prompt = prompt

    def __call__(self, command: str, cwd: Path) -> bool:
        if self.mode == "auto":
            return True
        if self.mode == "never":
            return False
        answer = self.prompt(
            f"\n允许执行命令？\n  目录: {cwd}\n  命令: {command}\n输入 y 确认 [y/N]: "
        )
        return answer.strip().lower() in {"y", "yes"}
