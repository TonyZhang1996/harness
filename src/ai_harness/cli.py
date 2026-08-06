"""Command-line entry point for AI Harness."""

from __future__ import annotations

import argparse

from . import __version__
from .agent import run_agent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-harness",
        description="A lightweight AI coding harness.",
    )
    parser.add_argument("task", nargs="?", help="Task for the harness to work on.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--workspace",
        help="工作区目录；默认使用当前目录。",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Agent 最多执行多少轮模型调用（默认：8）。",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.task:
        try:
            print(
                run_agent(
                    args.task,
                    max_turns=args.max_turns,
                    workspace=args.workspace,
                )
            )
        except Exception as exc:
            build_parser().error(str(exc))
    else:
        build_parser().print_help()


if __name__ == "__main__":
    main()
