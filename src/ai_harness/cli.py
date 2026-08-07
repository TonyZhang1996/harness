"""Command-line entry point for AI Harness."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .agent import AgentSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-harness",
        description="A safe, interactive local coding agent.",
    )
    parser.add_argument("task", nargs="?", help="Task for the harness to work on.")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--workspace", help="工作区目录；默认使用当前目录。")
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        dest="allowed_paths",
        help="额外允许访问的目录；可重复指定。",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=12,
        help="每条用户消息最多执行多少轮模型调用（默认：12）。",
    )
    parser.add_argument(
        "--approval",
        choices=["ask", "auto", "never"],
        default="ask",
        help="Shell 命令审批策略：询问、自动允许或始终拒绝。",
    )
    parser.add_argument(
        "--full-access",
        action="store_true",
        help="完全访问文件系统、敏感文件，并自动允许 Shell 命令。",
    )
    parser.add_argument("--model", help="覆盖 AI_HARNESS_MODEL。")
    parser.add_argument("--env-file", help="指定包含模型配置的 .env 文件。")
    parser.add_argument("--quiet-tools", action="store_true", help="隐藏工具进度。")
    parser.add_argument("--log-file", help="将工具事件以 JSONL 写入指定文件。")
    return parser


def _print_interactive_help() -> None:
    print(
        "交互命令：\n"
        "  /permissions              打开权限模式菜单\n"
        "  /permissions ask         请求批准（默认安全模式）\n"
        "  /permissions auto        替我审批（仍限制在授权目录）\n"
        "  /permissions full-access 完全访问权限\n"
        "  /permissions never       禁止执行需审批的操作\n"
        "  /ask /auto /full-access  快速切换权限模式\n"
        "  /clear                    清空当前对话上下文\n"
        "  /help                     显示这份帮助\n"
        "  /exit                     退出交互模式\n"
        "  Ctrl-D                    退出交互模式"
    )


PERMISSION_LABELS = {
    "ask": "请求批准",
    "auto": "替我审批",
    "never": "禁止执行",
    "full-access": "完全访问权限",
}


def _print_permission_status(session: AgentSession) -> None:
    mode = session.permission_mode
    label = PERMISSION_LABELS[mode]
    print(f"权限已切换：{label}（{mode}）")
    if mode == "full-access":
        print("⚠ 当前会话可访问整个文件系统和敏感文件，并会自动批准工具操作。")
    elif mode == "auto":
        print("文件访问仍限制在工作区和授权目录；工具操作将自动批准。")
    elif mode == "ask":
        print("文件访问限制在工作区和授权目录；敏感工具操作会逐次询问。")
    else:
        print("文件访问限制在工作区和授权目录；敏感工具操作将被拒绝。")


def _select_permission_mode(session: AgentSession) -> None:
    current = session.permission_mode
    print(
        f"当前权限：{PERMISSION_LABELS[current]}（{current}）\n"
        "  1. 请求批准      工作区内操作，敏感操作逐次询问\n"
        "  2. 替我审批      工作区内操作，敏感操作自动批准\n"
        "  3. 完全访问权限  整个文件系统、敏感文件、自动批准\n"
        "  4. 禁止执行      工作区内操作，拒绝敏感操作"
    )
    try:
        choice = input("选择 [1-4，回车取消]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消权限切换。")
        return
    selected = {"1": "ask", "2": "auto", "3": "full-access", "4": "never"}.get(choice)
    if not choice:
        print("已取消权限切换。")
        return
    if selected is None:
        print("无效选择，权限未改变。")
        return
    session.set_permission_mode(selected)
    _print_permission_status(session)


def _handle_permission_command(session: AgentSession, task: str) -> bool:
    shortcuts = {
        "/ask": "ask",
        "/safe": "ask",
        "/auto": "auto",
        "/never": "never",
        "/full": "full-access",
        "/full-access": "full-access",
    }
    if task in shortcuts:
        session.set_permission_mode(shortcuts[task])
        _print_permission_status(session)
        return True

    parts = task.split()
    if not parts or parts[0] not in {"/permissions", "/permission", "/approval"}:
        return False
    if len(parts) == 1:
        _select_permission_mode(session)
        return True
    if len(parts) > 2:
        print("用法：/permissions [ask|auto|never|full-access]")
        return True
    try:
        session.set_permission_mode(parts[1])
        _print_permission_status(session)
    except ValueError as exc:
        print(exc)
    return True


def _event_callback(quiet: bool = False, log_file: str | None = None):
    log_path = Path(log_file).expanduser().resolve() if log_file else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(kind: str, message: str) -> None:
        if not quiet:
            if kind == "tool_start":
                print(f"\n→ {message}")
            else:
                first_line = message.splitlines()[0] if message else "完成"
                print(f"  ✓ {first_line}")
        if log_path:
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "message": message,
            }
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return emit


def _create_session(
    *,
    max_turns: int,
    workspace: str | Path | None,
    allowed_paths: list[str] | None,
    approval_mode: str,
    full_access: bool,
    model_name: str | None,
    quiet_tools: bool,
    log_file: str | None,
) -> AgentSession:
    return AgentSession(
        max_turns=max_turns,
        workspace=workspace,
        allowed_paths=allowed_paths,
        approval_mode=approval_mode,
        full_access=full_access,
        model_name=model_name,
        event_callback=_event_callback(quiet_tools, log_file),
    )


def run_interactive(
    max_turns: int = 12,
    workspace: str | Path | None = None,
    allowed_paths: list[str] | None = None,
    approval_mode: str = "ask",
    full_access: bool = False,
    model_name: str | None = None,
    quiet_tools: bool = False,
    log_file: str | None = None,
) -> None:
    """Run a stateful conversational CLI session."""
    session = _create_session(
        max_turns=max_turns,
        workspace=workspace,
        allowed_paths=allowed_paths,
        approval_mode=approval_mode,
        full_access=full_access,
        model_name=model_name,
        quiet_tools=quiet_tools,
        log_file=log_file,
    )
    workspace_path = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
    print(f"AI Harness {__version__}（工作区：{workspace_path}）")
    effective_mode = getattr(
        session, "permission_mode", "full-access" if full_access else approval_mode
    )
    print(
        f"模型：{getattr(session, 'model_name', model_name or 'unknown')}；"
        f"权限：{effective_mode}"
    )
    if full_access:
        print("⚠ 完全访问模式：可访问整个文件系统、敏感文件并自动执行命令。")
    if allowed_paths:
        rendered = ", ".join(str(Path(path).expanduser().resolve()) for path in allowed_paths)
        print(f"额外授权目录：{rendered}")
    print("输入任务开始对话，输入 /help 查看命令。")

    while True:
        try:
            task = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return
        if not task:
            continue
        if task in {"/exit", "/quit"}:
            print("已退出。")
            return
        if task == "/help":
            _print_interactive_help()
            continue
        if task == "/clear":
            session.clear()
            print("对话上下文已清空。")
            continue
        if _handle_permission_command(session, task):
            continue
        try:
            answer = session.ask(task)
            print(f"\nHarness> {answer}")
        except Exception as exc:
            print(f"\n执行失败：{exc}")


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.env_file:
            os.environ["AI_HARNESS_ENV_FILE"] = str(
                Path(args.env_file).expanduser().resolve()
            )
        if args.task:
            session = _create_session(
                max_turns=args.max_turns,
                workspace=args.workspace,
                allowed_paths=args.allowed_paths,
                approval_mode=args.approval,
                full_access=args.full_access,
                model_name=args.model,
                quiet_tools=args.quiet_tools,
                log_file=args.log_file,
            )
            print(session.ask(args.task))
        else:
            run_interactive(
                max_turns=args.max_turns,
                workspace=args.workspace,
                allowed_paths=args.allowed_paths,
                approval_mode=args.approval,
                full_access=args.full_access,
                model_name=args.model,
                quiet_tools=args.quiet_tools,
                log_file=args.log_file,
            )
    except Exception as exc:
        build_parser().error(str(exc))


if __name__ == "__main__":
    main()
