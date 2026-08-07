"""Stateful tool-calling loop for the coding agent."""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from .approval import CommandApprover
from .config import ModelConfig
from .model import create_client
from .tools import (
    _get_allowed_roots,
    _get_filesystem_roots,
    capture_photo,
    create_directory,
    create_file,
    delete_directory,
    delete_file,
    edit_file,
    git_diff,
    git_status,
    list_files,
    read_file,
    run_command,
    search_text,
    write_file,
)


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


PATH_PROPERTY = {
    "type": "string",
    "description": "Workspace-relative path or absolute path inside an explicitly allowed directory.",
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file.",
            "parameters": _object_schema(
                {
                    "path": PATH_PROPERTY,
                    "max_chars": {"type": "integer", "description": "Maximum characters."},
                },
                ["path"],
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Recursively list files and directories.",
            "parameters": _object_schema(
                {
                    "path": PATH_PROPERTY,
                    "max_entries": {"type": "integer", "description": "Maximum entries."},
                }
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search text across UTF-8 project files.",
            "parameters": _object_schema(
                {
                    "query": {"type": "string"},
                    "path": PATH_PROPERTY,
                    "max_results": {"type": "integer"},
                    "case_sensitive": {"type": "boolean"},
                },
                ["query"],
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a new UTF-8 file without overwriting an existing file.",
            "parameters": _object_schema(
                {"path": PATH_PROPERTY, "content": {"type": "string"}}, ["path"]
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or completely overwrite a UTF-8 file.",
            "parameters": _object_schema(
                {"path": PATH_PROPERTY, "content": {"type": "string"}},
                ["path", "content"],
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Safely replace exact text in an existing UTF-8 file.",
            "parameters": _object_schema(
                {
                    "path": PATH_PROPERTY,
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                ["path", "old_text", "new_text"],
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete one file, never a directory.",
            "parameters": _object_schema({"path": PATH_PROPERTY}, ["path"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory and missing parent directories.",
            "parameters": _object_schema({"path": PATH_PROPERTY}, ["path"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_directory",
            "description": "Delete an empty directory only.",
            "parameters": _object_schema({"path": PATH_PROPERTY}, ["path"]),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_photo",
            "description": "Capture one photo from the host camera and save it as JPEG or PNG.",
            "parameters": _object_schema(
                {
                    "path": PATH_PROPERTY,
                    "device": {
                        "type": "string",
                        "description": (
                            "Camera index or name. Windows accepts a DirectShow name, "
                            "Linux accepts a /dev/video path, and the default is 0."
                        ),
                    },
                    "warmup_seconds": {
                        "type": "number",
                        "description": "Camera warmup delay from 0 to 10 seconds.",
                    },
                },
                ["path"],
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command after applying the configured approval policy.",
            "parameters": _object_schema(
                {
                    "command": {"type": "string"},
                    "cwd": PATH_PROPERTY,
                    "timeout": {"type": "integer", "description": "Timeout in seconds, max 600."},
                    "max_chars": {"type": "integer"},
                },
                ["command"],
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Read concise Git status for the workspace.",
            "parameters": _object_schema({}),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Read unstaged or staged Git diff for the workspace.",
            "parameters": _object_schema(
                {"staged": {"type": "boolean"}, "max_chars": {"type": "integer"}}
            ),
        },
    },
]

TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "read_file": read_file,
    "list_files": list_files,
    "search_text": search_text,
    "create_file": create_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "delete_file": delete_file,
    "create_directory": create_directory,
    "delete_directory": delete_directory,
    "capture_photo": capture_photo,
    "run_command": run_command,
    "git_status": git_status,
    "git_diff": git_diff,
}

SYSTEM_PROMPT = """You are AI Harness, a careful local coding agent.
Inspect the project before changing it. Use tools instead of inventing file contents or command results.
Prefer targeted edits. After meaningful code changes, run the relevant tests or checks when command execution is approved.
Never claim a file changed, a command ran, or a test passed unless the corresponding tool succeeded.
Honor the active session permission mode described below. Do not claim an operation is unavailable before trying the appropriate tool when that mode permits it.
Do not expose API keys or secrets. Respond in the user's language and summarize concrete outcomes."""

EventCallback = Callable[[str, str], None]


def _handlers_for_workspace(
    workspace: str | Path | None,
    allowed_paths: Sequence[str | Path] | None = None,
    approval_callback: Callable[[str, Path], bool] | None = None,
    full_access: bool = False,
) -> dict[str, Callable[..., str]]:
    """Bind every tool to one workspace, extra roots, and approval policy."""
    authorized = list(allowed_paths or ())
    if full_access:
        for filesystem_root in _get_filesystem_roots():
            if filesystem_root not in authorized:
                authorized.append(filesystem_root)
    authorized_paths = tuple(authorized)
    _get_allowed_roots(workspace, authorized_paths)
    handlers: dict[str, Callable[..., str]] = {}
    for name, handler in TOOL_HANDLERS.items():
        kwargs: dict[str, Any] = {
            "workspace_root": workspace,
            "allowed_roots": authorized_paths,
        }
        if name in {"run_command", "capture_photo"}:
            kwargs["approval_callback"] = approval_callback
        if name in {
            "read_file",
            "search_text",
            "create_file",
            "write_file",
            "edit_file",
            "delete_file",
        }:
            kwargs["allow_sensitive"] = full_access
        handlers[name] = partial(handler, **kwargs)
    return handlers


def _assistant_message_to_dict(message: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"role": "assistant"}
    if message.content is not None:
        result["content"] = message.content
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]
    return result


def _execute_tool(
    name: str,
    arguments_json: str,
    handlers: dict[str, Callable[..., str]] | None = None,
) -> str:
    handler = (handlers or TOOL_HANDLERS).get(name)
    if handler is None:
        return f"工具不存在: {name}"
    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError as exc:
        return f"工具参数不是有效 JSON: {exc}"
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象"
    try:
        return str(handler(**arguments))
    except Exception as exc:
        return f"工具执行失败: {exc}"


def _safe_tool_summary(name: str, arguments_json: str) -> str:
    try:
        arguments = json.loads(arguments_json)
    except json.JSONDecodeError:
        return name
    safe = {
        key: value
        for key, value in arguments.items()
        if key not in {"content", "old_text", "new_text"}
    }
    return f"{name} {json.dumps(safe, ensure_ascii=False)}"


class AgentSession:
    """A stateful model/tool session for interactive conversations."""

    PERMISSION_ALIASES = {
        "ask": "ask",
        "request": "ask",
        "safe": "ask",
        "auto": "auto",
        "approve": "auto",
        "never": "never",
        "deny": "never",
        "full": "full-access",
        "full-access": "full-access",
        "full_access": "full-access",
    }

    def __init__(
        self,
        max_turns: int = 12,
        workspace: str | Path | None = None,
        allowed_paths: Sequence[str | Path] | None = None,
        approval_mode: str = "ask",
        full_access: bool = False,
        client: Any | None = None,
        model_name: str | None = None,
        event_callback: EventCallback | None = None,
        approver: Callable[[str, Path], bool] | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns 必须大于 0")
        if client is None:
            config = ModelConfig.from_env()
            client = create_client(config)
            model_name = model_name or config.model
        self.client = client
        self.model_name = model_name or os.getenv("AI_HARNESS_MODEL", "test-model")
        self.max_turns = max_turns
        self.event_callback = event_callback
        self.workspace = workspace
        self.allowed_paths = tuple(allowed_paths or ())
        self.full_access = full_access
        self.approval_mode = "auto" if full_access else approval_mode
        self.approver = approver or CommandApprover(self.approval_mode)
        self._rebuild_tool_handlers()
        self.messages: list[dict[str, Any]] = []
        self.clear()

    @property
    def permission_mode(self) -> str:
        """Return the active user-facing permission mode."""
        return "full-access" if self.full_access else self.approval_mode

    def _rebuild_tool_handlers(self) -> None:
        self.tool_handlers = _handlers_for_workspace(
            self.workspace,
            self.allowed_paths,
            self.approver,
            full_access=self.full_access,
        )

    def set_permission_mode(self, mode: str) -> str:
        """Switch file boundaries and action approval policy for this session."""
        requested = mode.strip().lower()
        normalized = self.PERMISSION_ALIASES.get(requested)
        if normalized is None:
            choices = "ask、auto、never、full-access"
            raise ValueError(f"无效权限模式: {mode}；可选值：{choices}")

        self.full_access = normalized == "full-access"
        self.approval_mode = "auto" if self.full_access else normalized
        self.approver = CommandApprover(self.approval_mode)
        self._rebuild_tool_handlers()
        if self.messages:
            self.messages[0]["content"] = self._system_prompt()
        return self.permission_mode

    def _system_prompt(self) -> str:
        system_name = platform.system() or os.name
        native_shell = "PowerShell" if system_name == "Windows" else (
            "zsh" if system_name == "Darwin" else "bash/sh"
        )
        if self.full_access:
            permission_context = (
                "Active permission mode: full-access. Tools may access the entire local "
                "filesystem, including paths outside the workspace and sensitive files. "
                "Tool actions are automatically approved."
            )
        else:
            approval_context = {
                "ask": "Potentially sensitive tool actions require user approval.",
                "auto": "Potentially sensitive tool actions are automatically approved.",
                "never": "Potentially sensitive tool actions are denied.",
            }[self.approval_mode]
            permission_context = (
                "Active permission mode: "
                f"{self.approval_mode}. File access is limited to the workspace and "
                f"explicitly authorized directories. {approval_context}"
            )
        platform_context = (
            f"Host operating system: {system_name}. Native command shell: {native_shell}. "
            "Generate commands and paths that are valid for this operating system."
        )
        return f"{SYSTEM_PROMPT}\n\n{platform_context}\n{permission_context}"

    def _emit(self, kind: str, message: str) -> None:
        if self.event_callback:
            self.event_callback(kind, message)

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": self._system_prompt()}]

    def ask(self, task: str) -> str:
        if not task.strip():
            return ""
        self.messages.append({"role": "user", "content": task})

        for _ in range(self.max_turns):
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=self.messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            self.messages.append(_assistant_message_to_dict(message))
            if not tool_calls:
                return message.content or ""

            for tool_call in tool_calls:
                summary = _safe_tool_summary(
                    tool_call.function.name, tool_call.function.arguments
                )
                self._emit("tool_start", summary)
                result = _execute_tool(
                    tool_call.function.name,
                    tool_call.function.arguments,
                    self.tool_handlers,
                )
                self._emit("tool_result", result[:500])
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )
        raise RuntimeError("Agent 达到最大循环次数，任务未完成")


def run_agent(
    task: str,
    max_turns: int = 12,
    workspace: str | Path | None = None,
    allowed_paths: Sequence[str | Path] | None = None,
    approval_mode: str = "ask",
    full_access: bool = False,
) -> str:
    return AgentSession(
        max_turns=max_turns,
        workspace=workspace,
        allowed_paths=allowed_paths,
        approval_mode=approval_mode,
        full_access=full_access,
    ).ask(task)
