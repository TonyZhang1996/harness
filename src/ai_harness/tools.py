"""Safe local file tools exposed to the coding agent."""

from __future__ import annotations

from pathlib import Path


WORKSPACE_ROOT = Path.cwd().resolve()


def _get_workspace_root(workspace_root: str | Path | None = None) -> Path:
    """Return a normalized workspace root."""
    root = Path(workspace_root).expanduser().resolve() if workspace_root else WORKSPACE_ROOT
    if not root.is_dir():
        raise ValueError(f"工作区不存在或不是目录: {root}")
    return root


def _resolve_workspace_path(
    path: str,
    workspace_root: str | Path | None = None,
    *,
    allow_root: bool = False,
    reject_symlinks: bool = False,
) -> Path:
    """Resolve a path and ensure it stays inside the selected workspace."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("路径不能为空")

    root = _get_workspace_root(workspace_root)
    raw_candidate = root / path

    if reject_symlinks:
        try:
            relative_path = raw_candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("路径必须位于当前工作区内") from exc

        current = root
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("文件修改不允许经过符号链接路径")

    candidate = raw_candidate.resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径必须位于当前工作区内") from exc

    if not allow_root and candidate == root:
        raise ValueError("不能把工作区根目录当作文件操作")

    return candidate


def read_file(
    path: str,
    max_chars: int = 20_000,
    workspace_root: str | Path | None = None,
) -> str:
    """Read a UTF-8 text file from the current workspace."""
    if max_chars < 0:
        raise ValueError("max_chars 不能小于 0")

    file_path = _resolve_workspace_path(path, workspace_root)

    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")

    content = file_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        return content[:max_chars] + "\n\n[文件内容已截断]"

    return content


def write_file(
    path: str,
    content: str,
    workspace_root: str | Path | None = None,
) -> str:
    """Create or overwrite a UTF-8 text file inside the workspace."""
    if not isinstance(content, str):
        raise ValueError("文件内容必须是字符串")

    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        reject_symlinks=True,
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"已写入文件: {path}"


def create_file(
    path: str,
    content: str = "",
    workspace_root: str | Path | None = None,
) -> str:
    """Create a new UTF-8 text file and refuse to overwrite an existing file."""
    if not isinstance(content, str):
        raise ValueError("文件内容必须是字符串")

    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        reject_symlinks=True,
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with file_path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise FileExistsError(f"文件已存在: {path}") from exc

    return f"已创建文件: {path}"


def delete_file(path: str, workspace_root: str | Path | None = None) -> str:
    """Delete one file inside the workspace; directories are never removed."""
    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        reject_symlinks=True,
    )

    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if file_path.is_dir():
        raise IsADirectoryError(f"只允许删除文件，不能删除目录: {path}")

    file_path.unlink()
    return f"已删除文件: {path}"
