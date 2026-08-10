"""Approval policies and model-backed review for sensitive tool actions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


ReviewDecision = Literal["allow", "deny", "ask"]

_NETWORK_PATTERN = (
    r"(?i)\b(invoke-webrequest|invoke-restmethod|curl\b|wget\b|ssh\b|scp\b|ftp\b|https?://)"
)


@dataclass(frozen=True)
class ReviewResult:
    """One narrowly scoped approval review result."""

    decision: ReviewDecision
    risk: str
    reason: str


class CommandApprover:
    """Approve, deny, or interactively ask before a sensitive action."""

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
            f"\n允许执行此操作？\n  目录: {cwd}\n  操作: {command}\n输入 y 确认 [y/N]: "
        )
        return answer.strip().lower() in {"y", "yes"}


_BLOCK_RULES: tuple[tuple[str, str], ...] = (
    (
        (
            r"(?is)(?=.*\b(?:invoke-webrequest|invoke-restmethod|curl|wget|ssh|scp|ftp)\b|.*https?://)"
            r"(?=.*(?:\.env\b|\.ssh[\\/]|id_rsa|login data|cookies?\b|credential|password|api[_-]?key|access[_-]?token|secret))"
        ),
        "疑似通过网络发送密钥、凭据或敏感数据",
    ),
    (
        r"(?i)(mimikatz|procdump\s+.*lsass|sekurlsa|sam\s+save|ntds\.dit)",
        "疑似读取或导出系统凭据",
    ),
    (
        r"(?i)(\.ssh[\\/]|id_rsa|login data|(?:^|[\\/])cookies?(?:\.|[\\/]|$)|get-childitem\s+env:|printenv\b|(?:^|[|;&]\s*)env(?:\s|$)|\$env:[^\s|;&]*(?:key|token|secret|password))",
        "疑似探测凭据、令牌或会话材料",
    ),
    (
        r"(?i)(set-mppreference\b.*disablerealtimemonitoring|netsh\b.*firewall\b.*(?:off|disable)|disable-windowsoptionalfeature.*defender)",
        "试图关闭安全防护",
    ),
    (
        r"(?i)(rm\s+-rf\s+(?:/|~)(?:\s|$)|remove-item\s+(?:[a-z]:\\|\\\\)[^|;]*-recurse[^|;]*-force|format(?:-volume)?\s+[a-z]:|diskpart\b)",
        "存在大范围或不可逆删除风险",
    ),
)

_MANUAL_RULES: tuple[tuple[str, str], ...] = (
    (r"(?i)^访问摄像头", "摄像头属于隐私设备"),
    (
        r"(?i)(\b(?:remove-item|del(?:ete)?|erase|rm|rmdir|truncate)\b|\bgit\s+(?:clean|reset\s+--hard)\b|\bformat-volume\b|(?:^|[|;&]\s*)format\s+[a-z]:)",
        "包含删除、覆盖或不可逆修改",
    ),
    (
        r"(?i)\b(pip|npm|pnpm|yarn|winget|choco|apt(?:-get)?|brew)\s+(?:install|uninstall|remove|update|upgrade)\b",
        "会安装、删除或升级软件",
    ),
    (
        r"(?i)\b(stop-process|taskkill|shutdown|restart-computer|stop-service|set-service|sc\.exe|schtasks|reg(?:\.exe)?\s+(?:add|delete)|set-itemproperty)\b",
        "会改变进程、服务、计划任务、注册表或系统状态",
    ),
    (
        r"(?i)\bgit\s+(?:push|commit|merge|rebase|checkout|switch|branch\s+-[dD])\b",
        "会改变 Git 历史、分支或远程状态",
    ),
)

_AUTO_ALLOW_RULES: tuple[tuple[str, str], ...] = (
    (_NETWORK_PATTERN, "普通网络访问按低风险自动允许"),
)


REVIEWER_PROMPT = """You are the independent approval reviewer for a local coding agent.
Review exactly one proposed action. Do not execute it and do not suggest a different command.

The normal boundary is the named workspace. Approval must never be treated as permission to
expose secrets, probe credentials, weaken security, establish persistence, or cause broad and
irreversible damage.

Return only one JSON object:
{"decision":"allow|deny|ask","risk":"low|medium|high","reason":"short Chinese reason"}

Decision rules:
- allow: clearly scoped, reversible or read-only, consistent with the user's request, and confined
  to the workspace.
- deny: credential or secret collection/exfiltration, broad security weakening, persistence without
  explicit need, or destructive action with significant irreversible risk.
- Treat ordinary network access as low risk and allow it when it does not send secrets, credentials,
  cookies, private data, or sensitive local files. Network access alone is not a reason to ask.
- ask: user intent is missing, scope is ambiguous, the action touches privacy devices or system
  state, installs software, deletes or overwrites material data, or safety cannot be established.
- Never infer that running in a workspace makes an arbitrary shell command sandboxed.
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from plain text or a fenced response."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("审查模型没有返回 JSON")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("审查结果不是 JSON 对象")
    return value


class AutoReviewApprover:
    """Route approval requests through a separate, narrowly prompted model call."""

    def __init__(
        self,
        client: Any,
        model_name: str,
        *,
        fallback_approver: Callable[[str, Path], bool],
        context_provider: Callable[[], str] | None = None,
        event_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.client = client
        self.model_name = model_name
        self.fallback_approver = fallback_approver
        self.context_provider = context_provider or (lambda: "")
        self.event_callback = event_callback
        self.last_result: ReviewResult | None = None

    def _emit(self, result: ReviewResult) -> None:
        self.last_result = result
        if self.event_callback is not None:
            label = {"allow": "自动允许", "deny": "自动拒绝", "ask": "需要确认"}[
                result.decision
            ]
            self.event_callback(
                "approval_review",
                f"{label} · 风险：{result.risk}\n{result.reason}",
            )

    @staticmethod
    def _local_policy(command: str) -> ReviewResult | None:
        for pattern, reason in _BLOCK_RULES:
            if re.search(pattern, command):
                return ReviewResult("deny", "high", reason)
        for pattern, reason in _MANUAL_RULES:
            if re.search(pattern, command):
                return ReviewResult("ask", "medium", reason)
        for pattern, reason in _AUTO_ALLOW_RULES:
            if re.search(pattern, command):
                return ReviewResult("allow", "low", reason)
        return None

    def review(self, command: str, cwd: Path) -> ReviewResult:
        """Return a policy result without executing the action."""
        local_result = self._local_policy(command)
        if local_result is not None:
            return local_result

        context = self.context_provider().strip()
        request = {
            "workspace_or_cwd": str(cwd),
            "proposed_action": command,
            "retained_context": context[-5000:],
        }
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": REVIEWER_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(request, ensure_ascii=False),
                    },
                ],
                temperature=0,
            )
            content = response.choices[0].message.content or ""
            payload = _extract_json_object(content)
            decision = str(payload.get("decision", "")).lower()
            risk = str(payload.get("risk", "medium")).lower()
            reason = str(payload.get("reason", "审查器未提供原因")).strip()
            if decision not in {"allow", "deny", "ask"}:
                raise ValueError("审查决定无效")
            if risk not in {"low", "medium", "high"}:
                risk = "medium"
            if decision == "allow" and risk != "low":
                decision = "ask"
                reason = f"审查器未能确认低风险：{reason}"
            return ReviewResult(decision, risk, reason[:300])  # type: ignore[arg-type]
        except Exception as exc:
            return ReviewResult("ask", "medium", f"自动审查不可用：{exc}")

    def __call__(self, command: str, cwd: Path) -> bool:
        result = self.review(command, cwd)
        self._emit(result)
        if result.decision == "allow":
            return True
        if result.decision == "deny":
            return False
        return self.fallback_approver(command, cwd)
