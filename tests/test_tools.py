from pathlib import Path

import pytest

from ai_harness.agent import _execute_tool, _handlers_for_workspace
from ai_harness.tools import create_file, delete_file, read_file, write_file


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
