"""Safe local tools exposed to the coding agent."""

from __future__ import annotations

from pathlib import Path


WORKSPACE_ROOT = Path.cwd().resolve()


def _resolve_workspace_path(path: str) -> Path:
    """Resolve a path and ensure it stays inside the current workspace."""
    candidate = (WORKSPACE_ROOT / path).resolve()

    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise ValueError("路径必须位于当前工作区内") from exc

    return candidate


def read_file(path: str, max_chars: int = 20_000) -> str:
    """Read a UTF-8 text file from the current workspace."""
    file_path = _resolve_workspace_path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")

    content = file_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        return content[:max_chars] + "\n\n[文件内容已截断]"

    return content
