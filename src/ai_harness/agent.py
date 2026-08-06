"""Tool-calling loop for the coding agent."""

from __future__ import annotations

import json
from typing import Any, Callable

from .model import create_client, get_model_name
from .tools import read_file


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the current workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path, such as README.md",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum number of characters to return.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]

TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "read_file": read_file,
}


def _assistant_message_to_dict(message: Any) -> dict[str, Any]:
    """Convert an SDK message into the format needed for the next request."""
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


def _execute_tool(name: str, arguments_json: str) -> str:
    """Parse and execute one model-requested tool call."""
    handler = TOOL_HANDLERS.get(name)
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


def run_agent(task: str, max_turns: int = 8) -> str:
    """Run the model/tool loop and return the final assistant message."""
    client = create_client()
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a careful coding agent. "
                "Use read_file when you need to inspect a workspace file. "
                "Do not invent file contents. Respond in the user's language."
            ),
        },
        {"role": "user", "content": task},
    ]

    for _ in range(max_turns):
        response = client.chat.completions.create(
            model=get_model_name(),
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
        )
        message = response.choices[0].message
        tool_calls = message.tool_calls or []
        messages.append(_assistant_message_to_dict(message))

        if not tool_calls:
            return message.content or ""

        for tool_call in tool_calls:
            tool_result = _execute_tool(
                tool_call.function.name,
                tool_call.function.arguments,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                }
            )

    raise RuntimeError("Agent 达到最大循环次数，任务未完成")
