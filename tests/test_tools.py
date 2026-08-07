from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_harness.tools as tools_module
from ai_harness.agent import _execute_tool, _handlers_for_workspace
from ai_harness.tools import (
    capture_photo,
    create_directory,
    create_file,
    delete_directory,
    delete_file,
    edit_file,
    list_files,
    read_file,
    run_command,
    search_text,
    write_file,
)


def test_file_lifecycle(tmp_path: Path):
    assert create_file("nested/example.txt", "first", tmp_path) == "已创建文件: nested/example.txt"
    assert read_file("nested/example.txt", workspace_root=tmp_path) == "first"

    assert write_file("nested/example.txt", "updated", tmp_path) == "已写入文件: nested/example.txt"
    assert read_file("nested/example.txt", workspace_root=tmp_path) == "updated"

    assert delete_file("nested/example.txt", tmp_path) == "已删除文件: nested/example.txt"
    assert not (tmp_path / "nested/example.txt").exists()


def test_create_file_does_not_overwrite(tmp_path: Path):
    create_file("example.txt", "original", tmp_path)

    with pytest.raises(FileExistsError):
        create_file("example.txt", "replacement", tmp_path)

    assert (tmp_path / "example.txt").read_text(encoding="utf-8") == "original"


def test_file_mutations_stay_inside_workspace(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(ValueError):
        write_file("../outside.txt", "blocked", tmp_path)
    with pytest.raises(ValueError):
        delete_file("../outside.txt", tmp_path)

    assert not outside.exists()


def test_explicitly_allowed_directory_can_be_modified(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    write_file(
        str(allowed / "desktop.txt"),
        "authorized",
        workspace_root=tmp_path,
        allowed_roots=[allowed],
    )

    assert (allowed / "desktop.txt").read_text(encoding="utf-8") == "authorized"


def test_unapproved_directory_is_still_blocked(tmp_path: Path):
    allowed = tmp_path / "allowed"
    unapproved = tmp_path / "unapproved"
    allowed.mkdir()
    unapproved.mkdir()

    with pytest.raises(ValueError):
        write_file(
            str(unapproved / "blocked.txt"),
            "blocked",
            workspace_root=tmp_path / "allowed",
            allowed_roots=[allowed],
        )

    assert not (unapproved / "blocked.txt").exists()


def test_delete_file_never_deletes_directories(tmp_path: Path):
    (tmp_path / "folder").mkdir()

    with pytest.raises(IsADirectoryError):
        delete_file("folder", tmp_path)

    assert (tmp_path / "folder").is_dir()


def test_agent_file_tools_use_explicit_workspace(tmp_path: Path):
    handlers = _handlers_for_workspace(tmp_path)

    result = _execute_tool(
        "write_file",
        '{"path": "agent.txt", "content": "created by agent"}',
        handlers,
    )

    assert result == "已写入文件: agent.txt"
    assert (tmp_path / "agent.txt").read_text(encoding="utf-8") == "created by agent"


def test_list_search_and_exact_edit(tmp_path: Path):
    create_file("src/example.py", "name = 'old'\n", tmp_path)

    assert "src/example.py" in list_files(workspace_root=tmp_path)
    assert "src/example.py:1" in search_text("old", workspace_root=tmp_path)
    assert "1 处" in edit_file("src/example.py", "old", "new", workspace_root=tmp_path)
    assert read_file("src/example.py", workspace_root=tmp_path) == "name = 'new'\n"


def test_directory_lifecycle_only_deletes_empty_directory(tmp_path: Path):
    create_directory("empty/nested", tmp_path)
    assert (tmp_path / "empty/nested").is_dir()
    delete_directory("empty/nested", tmp_path)
    assert not (tmp_path / "empty/nested").exists()

    create_file("nonempty/file.txt", "x", tmp_path)
    with pytest.raises(OSError):
        delete_directory("nonempty", tmp_path)


def test_run_command_requires_approval_and_returns_exit_code(tmp_path: Path):
    denied = run_command("printf denied", workspace_root=tmp_path)
    assert "拒绝" in denied

    approved = run_command(
        "printf approved",
        workspace_root=tmp_path,
        approval_callback=lambda _command, _cwd: True,
    )
    assert "退出码: 0" in approved
    assert "approved" in approved


def test_run_command_does_not_forward_api_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    result = run_command(
        "printenv DEEPSEEK_API_KEY",
        workspace_root=tmp_path,
        approval_callback=lambda _command, _cwd: True,
    )
    assert "must-not-leak" not in result
    assert "退出码: 1" in result


def test_mutation_through_symlink_is_rejected(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="符号链接"):
        write_file(
            "linked/file.txt",
            "blocked",
            workspace_root=workspace,
            allowed_roots=[outside],
        )

    assert not (outside / "file.txt").exists()


def test_sensitive_files_are_protected(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=placeholder", encoding="utf-8")

    with pytest.raises(PermissionError, match="敏感文件"):
        read_file(".env", workspace_root=tmp_path)
    with pytest.raises(PermissionError, match="敏感文件"):
        write_file(".env", "changed", workspace_root=tmp_path)

    assert "SECRET=value" not in search_text("SECRET", workspace_root=tmp_path)
    assert read_file(".env.example", workspace_root=tmp_path) == "SECRET=placeholder"


def test_full_access_handlers_can_reach_outside_and_sensitive_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / ".env").write_text("SECRET=allowed", encoding="utf-8")
    handlers = _handlers_for_workspace(
        workspace,
        full_access=True,
        approval_callback=lambda _command, _cwd: True,
    )

    read_result = _execute_tool(
        "read_file",
        f'{{"path": "{outside / ".env"}"}}',
        handlers,
    )
    write_result = _execute_tool(
        "write_file",
        f'{{"path": "{outside / "created.txt"}", "content": "ok"}}',
        handlers,
    )

    assert read_result == "SECRET=allowed"
    assert "已写入文件" in write_result
    assert (outside / "created.txt").read_text(encoding="utf-8") == "ok"


def test_capture_photo_requires_approval(tmp_path: Path):
    result = capture_photo("photo.jpg", workspace_root=tmp_path)

    assert "拒绝" in result
    assert not (tmp_path / "photo.jpg").exists()


def test_capture_photo_verifies_generated_image(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: "/opt/homebrew/bin/ffmpeg")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"\xff\xd8" + b"photo" * 100)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)
    result = capture_photo(
        "photo.jpg",
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: True,
    )

    assert "已拍照并保存" in result
    assert (tmp_path / "photo.jpg").stat().st_size > 100
