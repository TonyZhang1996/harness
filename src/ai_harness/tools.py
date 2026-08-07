"""Safe local tools exposed to the coding agent."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path


WORKSPACE_ROOT = Path.cwd().resolve()
DEFAULT_IGNORES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
PROTECTED_NAMES = {
    ".env",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
}
PROTECTED_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
SENSITIVE_ENV_KEYS = {
    "AI_HARNESS_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
}
ApprovalCallback = Callable[[str, Path], bool]


def _get_workspace_root(workspace_root: str | Path | None = None) -> Path:
    """Return a normalized workspace root."""
    root = Path(workspace_root).expanduser().resolve() if workspace_root else WORKSPACE_ROOT
    if not root.is_dir():
        raise ValueError(f"工作区不存在或不是目录: {root}")
    return root


def _get_allowed_roots(
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> tuple[Path, ...]:
    """Return the workspace plus explicitly authorized directories."""
    roots = [_get_workspace_root(workspace_root)]
    for allowed_root in allowed_roots or ():
        candidate = _get_workspace_root(allowed_root)
        if candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_workspace_path(
    path: str,
    workspace_root: str | Path | None = None,
    *,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_root: bool = False,
    reject_symlinks: bool = False,
) -> Path:
    """Resolve a path inside the workspace or an explicitly allowed directory."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("路径不能为空")

    roots = _get_allowed_roots(workspace_root, allowed_roots)
    requested = Path(path).expanduser()
    raw_candidate = requested if requested.is_absolute() else roots[0] / requested
    candidate = raw_candidate.resolve()
    matching_root = next(
        (root for root in roots if candidate == root or _is_within(candidate, root)),
        None,
    )
    if matching_root is None:
        raise ValueError("路径不在工作区或已授权目录内")
    if not allow_root and candidate == matching_root:
        raise ValueError("不能把授权目录根目录当作文件操作")

    if reject_symlinks:
        try:
            relative_path = raw_candidate.relative_to(matching_root)
        except ValueError as exc:
            raise ValueError("文件修改不允许经过符号链接路径") from exc
        current = matching_root
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise ValueError("文件修改不允许经过符号链接路径")

    return candidate


def _display_path(path: Path, workspace_root: str | Path | None) -> str:
    root = _get_workspace_root(workspace_root)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_protected_file(path: Path) -> bool:
    name = path.name.lower()
    if name in PROTECTED_NAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    return path.suffix.lower() in PROTECTED_SUFFIXES


def _reject_protected_file(path: Path) -> None:
    if _is_protected_file(path):
        raise PermissionError(f"受保护的敏感文件不能由 Agent 访问: {path.name}")


def read_file(
    path: str,
    max_chars: int = 20_000,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_sensitive: bool = False,
) -> str:
    """Read a UTF-8 text file."""
    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    file_path = _resolve_workspace_path(
        path, workspace_root, allowed_roots=allowed_roots
    )
    if not allow_sensitive:
        _reject_protected_file(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    content = file_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        return content[:max_chars] + "\n\n[文件内容已截断]"
    return content


def list_files(
    path: str = ".",
    max_entries: int = 300,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """List files recursively while skipping common generated directories."""
    if max_entries < 1:
        raise ValueError("max_entries 必须大于 0")
    directory = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        allow_root=True,
    )
    if not directory.is_dir():
        raise NotADirectoryError(f"目录不存在: {path}")

    entries: list[str] = []
    for candidate in sorted(directory.rglob("*")):
        relative = candidate.relative_to(directory)
        if any(part in DEFAULT_IGNORES for part in relative.parts):
            continue
        suffix = "/" if candidate.is_dir() else ""
        entries.append(f"{relative}{suffix}")
        if len(entries) >= max_entries:
            entries.append("[结果已截断]")
            break
    return "\n".join(entries) or "[目录为空]"


def search_text(
    query: str,
    path: str = ".",
    max_results: int = 100,
    case_sensitive: bool = False,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_sensitive: bool = False,
) -> str:
    """Search UTF-8 text files and return file, line, and matching text."""
    if not query:
        raise ValueError("搜索内容不能为空")
    if max_results < 1:
        raise ValueError("max_results 必须大于 0")
    target = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        allow_root=True,
    )
    candidates = [target] if target.is_file() else sorted(target.rglob("*"))
    needle = query if case_sensitive else query.casefold()
    matches: list[str] = []

    for candidate in candidates:
        if not candidate.is_file():
            continue
        if not allow_sensitive and _is_protected_file(candidate):
            continue
        try:
            relative = candidate.relative_to(target if target.is_dir() else target.parent)
        except ValueError:
            relative = candidate
        if any(part in DEFAULT_IGNORES for part in relative.parts):
            continue
        try:
            if candidate.stat().st_size > 2_000_000:
                continue
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.casefold()
            if needle in haystack:
                matches.append(f"{_display_path(candidate, workspace_root)}:{line_number}: {line}")
                if len(matches) >= max_results:
                    matches.append("[结果已截断]")
                    return "\n".join(matches)
    return "\n".join(matches) or "[未找到匹配内容]"


def write_file(
    path: str,
    content: str,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_sensitive: bool = False,
) -> str:
    """Create or overwrite a UTF-8 text file."""
    if not isinstance(content, str):
        raise ValueError("文件内容必须是字符串")
    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    if not allow_sensitive:
        _reject_protected_file(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"已写入文件: {_display_path(file_path, workspace_root)}"


def create_file(
    path: str,
    content: str = "",
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_sensitive: bool = False,
) -> str:
    """Create a new UTF-8 text file without overwriting."""
    if not isinstance(content, str):
        raise ValueError("文件内容必须是字符串")
    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    if not allow_sensitive:
        _reject_protected_file(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise FileExistsError(f"文件已存在: {path}") from exc
    return f"已创建文件: {_display_path(file_path, workspace_root)}"


def edit_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_sensitive: bool = False,
) -> str:
    """Replace exact text in a UTF-8 file."""
    if not old_text:
        raise ValueError("old_text 不能为空")
    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    if not allow_sensitive:
        _reject_protected_file(file_path)
    content = file_path.read_text(encoding="utf-8")
    count = content.count(old_text)
    if count == 0:
        raise ValueError("未找到要替换的文本")
    if count > 1 and not replace_all:
        raise ValueError(f"找到 {count} 处匹配；请提供更精确的文本或启用 replace_all")
    updated = content.replace(old_text, new_text, -1 if replace_all else 1)
    file_path.write_text(updated, encoding="utf-8")
    replacements = count if replace_all else 1
    return f"已修改文件: {_display_path(file_path, workspace_root)}（{replacements} 处）"


def delete_file(
    path: str,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_sensitive: bool = False,
) -> str:
    """Delete one file; directories are never removed by this tool."""
    file_path = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    if not allow_sensitive:
        _reject_protected_file(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if file_path.is_dir():
        raise IsADirectoryError(f"只允许删除文件，不能删除目录: {path}")
    file_path.unlink()
    return f"已删除文件: {_display_path(file_path, workspace_root)}"


def create_directory(
    path: str,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """Create a directory and missing parents."""
    directory = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    directory.mkdir(parents=True, exist_ok=True)
    return f"已创建目录: {_display_path(directory, workspace_root)}"


def delete_directory(
    path: str,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """Delete an empty directory only."""
    directory = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    if not directory.is_dir():
        raise NotADirectoryError(f"目录不存在: {path}")
    try:
        directory.rmdir()
    except OSError as exc:
        raise OSError(f"目录不是空目录，拒绝删除: {path}") from exc
    return f"已删除空目录: {_display_path(directory, workspace_root)}"


def _clean_subprocess_env() -> dict[str, str]:
    environment = os.environ.copy()
    for key in SENSITIVE_ENV_KEYS:
        environment.pop(key, None)
    return environment


def run_command(
    command: str,
    cwd: str = ".",
    timeout: int = 120,
    max_chars: int = 30_000,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    approval_callback: ApprovalCallback | None = None,
) -> str:
    """Run a shell command after approval, without forwarding model API keys."""
    if not command.strip():
        raise ValueError("命令不能为空")
    if timeout < 1 or timeout > 600:
        raise ValueError("timeout 必须在 1 到 600 秒之间")
    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    command_cwd = _resolve_workspace_path(
        cwd,
        workspace_root,
        allowed_roots=allowed_roots,
        allow_root=True,
    )
    if not command_cwd.is_dir():
        raise NotADirectoryError(f"命令目录不存在: {cwd}")
    if approval_callback is None or not approval_callback(command, command_cwd):
        return "命令执行被用户或审批策略拒绝"

    try:
        completed = subprocess.run(
            ["/bin/zsh", "-c", command],
            cwd=command_cwd,
            env=_clean_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return f"命令超时（{timeout}s）\n{output[:max_chars]}"

    output = completed.stdout + completed.stderr
    if len(output) > max_chars:
        output = output[:max_chars] + "\n[命令输出已截断]"
    rendered = shlex.join(["/bin/zsh", "-c", command])
    return f"命令: {rendered}\n退出码: {completed.returncode}\n{output}".rstrip()


def capture_photo(
    path: str,
    device: str = "0",
    warmup_seconds: float = 1.0,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    approval_callback: ApprovalCallback | None = None,
) -> str:
    """Capture one photo from a macOS camera using FFmpeg AVFoundation."""
    if os.uname().sysname != "Darwin":
        raise RuntimeError("capture_photo 当前仅支持 macOS")
    if warmup_seconds < 0 or warmup_seconds > 10:
        raise ValueError("warmup_seconds 必须在 0 到 10 秒之间")
    output_path = _resolve_workspace_path(
        path,
        workspace_root,
        allowed_roots=allowed_roots,
        reject_symlinks=True,
    )
    if output_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("照片路径必须使用 .jpg、.jpeg 或 .png 扩展名")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    approval_text = f"访问摄像头并拍照，保存到 {output_path}"
    if approval_callback is None or not approval_callback(
        approval_text, output_path.parent
    ):
        return "摄像头访问被用户或审批策略拒绝"

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法访问 macOS 摄像头")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "avfoundation",
        "-framerate",
        "30",
        "-i",
        f"{device}:none",
    ]
    if warmup_seconds > 0:
        command.extend(["-vf", f"select=gte(t\\,{warmup_seconds})"])
    command.extend(["-frames:v", "1", "-y", str(output_path)])

    try:
        completed = subprocess.run(
            command,
            cwd=output_path.parent,
            env=_clean_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=max(15, int(warmup_seconds) + 10),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("摄像头拍照超时，请检查系统相机权限") from exc

    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"摄像头拍照失败: {error[:2000]}")
    if not output_path.is_file() or output_path.stat().st_size < 100:
        raise RuntimeError("摄像头命令结束但没有生成有效照片")
    return (
        f"已拍照并保存: {_display_path(output_path, workspace_root)} "
        f"（{output_path.stat().st_size} bytes）"
    )


def git_status(
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """Return concise Git status for the workspace."""
    root = _resolve_workspace_path(
        ".", workspace_root, allowed_roots=allowed_roots, allow_root=True
    )
    completed = subprocess.run(
        ["git", "status", "--short", "--branch"],
        cwd=root,
        env=_clean_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return (completed.stdout + completed.stderr).strip() or "[无状态输出]"


def git_diff(
    staged: bool = False,
    max_chars: int = 30_000,
    workspace_root: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
) -> str:
    """Return the current Git diff without changing repository state."""
    root = _resolve_workspace_path(
        ".", workspace_root, allowed_roots=allowed_roots, allow_root=True
    )
    command = ["git", "diff"]
    if staged:
        command.append("--cached")
    completed = subprocess.run(
        command,
        cwd=root,
        env=_clean_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if len(output) > max_chars:
        output = output[:max_chars] + "\n[Git diff 已截断]"
    return output.strip() or "[无差异]"
