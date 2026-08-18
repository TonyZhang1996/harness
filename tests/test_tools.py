import os
import json
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_harness.tools as tools_module
from ai_harness.agent import _execute_tool, _handlers_for_workspace
from ai_harness.tools import (
    browser_search,
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


def test_browser_search_uses_headless_chromium_and_public_search_url(
    tmp_path: Path, monkeypatch
):
    calls = []

    for environment_name in tools_module.BROWSER_PROXY_ENV_NAMES:
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:6268")

    class FakePage:
        url = "https://www.baidu.com/s?wd=%E6%B5%8B%E8%AF%95"

        def goto(self, url, **kwargs):
            calls.append(("goto", url, kwargs))

        def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

        def title(self):
            return "测试_百度搜索"

        def inner_text(self, selector):
            assert selector == "body"
            return "搜索结果正文"

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        def new_context(self, **kwargs):
            calls.append(("context", kwargs))
            return FakeContext()

        def close(self):
            calls.append(("close",))

    class FakeChromium:
        def launch(self, **kwargs):
            calls.append(("launch", kwargs))
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class PlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    approvals = []
    monkeypatch.setattr(tools_module, "_sync_playwright", lambda: PlaywrightContext())
    result = browser_search(
        "测试关键词",
        workspace_root=tmp_path,
        approval_callback=lambda action, cwd: approvals.append((action, cwd)) or True,
    )

    assert "搜索结果正文" in result
    assert approvals[0][1] == tmp_path.resolve()
    assert calls[0] == (
        "launch",
        {"headless": True, "proxy": {"server": "http://127.0.0.1:6268"}},
    )
    assert calls[1][0] == "context"
    assert calls[1][1]["ignore_https_errors"] is True
    assert "wd=%E6%B5%8B%E8%AF%95%E5%85%B3%E9%94%AE%E8%AF%8D" in calls[2][1]
    assert calls[-1] == ("close",)


def test_browser_search_falls_back_to_baidu_after_bing_network_failure(
    tmp_path: Path, monkeypatch
):
    for environment_name in tools_module.BROWSER_PROXY_ENV_NAMES:
        monkeypatch.delenv(environment_name, raising=False)

    attempts = []

    def fake_search_once(
        sync_playwright, query, engine, max_chars, timeout, proxy, image_search
    ):
        attempts.append((engine, timeout, proxy, image_search))
        if engine == "bing":
            raise RuntimeError("Page.goto: Timeout 5000ms exceeded")
        return "搜索引擎: baidu\n查询: 测试关键词\n----\n搜索结果正文"

    monkeypatch.setattr(tools_module, "_sync_playwright", lambda: object())
    monkeypatch.setattr(tools_module, "_browser_search_once", fake_search_once)

    result = browser_search(
        "测试关键词",
        engine="bing",
        timeout=10,
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: True,
    )

    assert "搜索引擎: baidu" in result
    assert "已自动回退到 baidu" in result
    assert [attempt[0] for attempt in attempts] == ["bing", "baidu"]
    assert all(attempt[2] is None for attempt in attempts)
    assert all(attempt[3] is False for attempt in attempts)


def test_browser_image_search_extracts_public_image_urls_only():
    class FakePage:
        def evaluate(self, _script):
            return [
                {"dataImgUrl": "https://cdn.example.test/portrait.jpg"},
                {"src": "data:image/png;base64,not-a-remote-image"},
                {"dataSrc": "//cdn.example.test/wallpaper.webp"},
                {"src": "javascript:alert(1)"},
                {"dataOriginal": "https://cdn.example.test/portrait.jpg"},
            ]

    assert tools_module._extract_browser_image_urls(FakePage()) == [
        "https://cdn.example.test/portrait.jpg",
        "https://cdn.example.test/wallpaper.webp",
    ]


def test_browser_search_auto_detects_image_queries(tmp_path: Path, monkeypatch):
    for environment_name in tools_module.BROWSER_PROXY_ENV_NAMES:
        monkeypatch.delenv(environment_name, raising=False)
    calls = []

    def fake_search_once(
        sync_playwright, query, engine, max_chars, timeout, proxy, image_search
    ):
        calls.append(image_search)
        return "搜索引擎: baidu\n图片预览"

    monkeypatch.setattr(tools_module, "_sync_playwright", lambda: object())
    monkeypatch.setattr(tools_module, "_browser_search_once", fake_search_once)

    result = browser_search(
        "刘德华照片",
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: True,
    )

    assert "图片预览" in result
    assert calls == [True]


def test_browser_search_requires_approval(tmp_path: Path):
    result = browser_search(
        "不应执行",
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: False,
    )

    assert result == "浏览器搜索被用户或审批策略拒绝"


def test_playwright_is_loaded_lazily_after_runtime_install(monkeypatch):
    fake_sync_playwright = lambda: None

    monkeypatch.setattr(tools_module, "_sync_playwright", None)
    monkeypatch.setattr(tools_module.importlib, "invalidate_caches", lambda: None)
    monkeypatch.setattr(
        tools_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(sync_playwright=fake_sync_playwright),
    )

    assert tools_module._load_sync_playwright() is fake_sync_playwright
    assert tools_module._sync_playwright is fake_sync_playwright


def test_frozen_build_uses_bundled_playwright_browsers(tmp_path: Path, monkeypatch):
    bundle_root = tmp_path / "bundle"
    browsers_root = bundle_root / "playwright-browsers"
    browsers_root.mkdir(parents=True)
    monkeypatch.setattr(tools_module.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    assert tools_module._configure_bundled_playwright_browsers() == browsers_root
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers_root)


def test_frozen_browser_search_reports_missing_bundled_browser(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(tools_module.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr(tools_module, "_sync_playwright", None)
    monkeypatch.setattr(
        tools_module.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("not installed")),
    )

    result = browser_search(
        "缺少发行包浏览器",
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: True,
    )

    assert "发行包未包含" in result
    assert "不要尝试" in result


def test_browser_search_reports_the_running_interpreter_when_playwright_is_missing(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(tools_module, "_sync_playwright", None)
    monkeypatch.setattr(
        tools_module.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("not installed")),
    )

    result = browser_search(
        "缺少依赖",
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: True,
    )

    assert sys.executable in result
    assert tools_module._playwright_command("pip", "install", "playwright") in result
    assert tools_module._playwright_command("playwright", "install", "chromium") in result
    assert "不要改用其他 Python 环境" in result


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
    command = _python_command("print('approved')")
    denied = run_command(command, workspace_root=tmp_path)
    assert "拒绝" in denied

    approved = run_command(
        command,
        workspace_root=tmp_path,
        approval_callback=lambda _command, _cwd: True,
    )
    assert "退出码: 0" in approved
    assert "approved" in approved


def test_run_command_does_not_forward_api_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    command = _python_command(
        "import os,sys; value=os.getenv('DEEPSEEK_API_KEY',''); "
        "print(value); sys.exit(0 if value else 1)"
    )
    result = run_command(
        command,
        workspace_root=tmp_path,
        approval_callback=lambda _command, _cwd: True,
    )
    assert "must-not-leak" not in result
    assert "退出码: 1" in result


def test_run_command_reports_progress_while_silent(tmp_path: Path):
    progress: list[str] = []
    command = _python_command("import time; time.sleep(1.3)")

    result = run_command(
        command,
        workspace_root=tmp_path,
        timeout=5,
        approval_callback=lambda _command, _cwd: True,
        progress_callback=progress.append,
    )

    assert "退出码: 0" in result
    assert any("运行中" in message and "没有输出" in message for message in progress)


def test_mutation_through_symlink_is_rejected(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")

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
        "read_file", json.dumps({"path": str(outside / ".env")}), handlers
    )
    write_result = _execute_tool(
        "write_file",
        json.dumps({"path": str(outside / "created.txt"), "content": "ok"}),
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
    monkeypatch.setattr(tools_module.platform, "system", lambda: "Darwin")
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


def _python_command(source: str) -> str:
    arguments = [sys.executable, "-c", source]
    if platform.system() == "Windows":
        return "& " + subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


@pytest.mark.parametrize(
    ("system_name", "expected_backend", "expected_input"),
    [
        ("Darwin", "avfoundation", "0:none"),
        ("Linux", "v4l2", "/dev/video0"),
        ("Windows", "dshow", "video=Integrated Camera"),
    ],
)
def test_capture_photo_uses_native_camera_backend(
    tmp_path: Path,
    monkeypatch,
    system_name: str,
    expected_backend: str,
    expected_input: str,
):
    calls = []
    monkeypatch.setattr(tools_module.platform, "system", lambda: system_name)
    monkeypatch.setattr(tools_module.shutil, "which", lambda _name: "ffmpeg")

    def fake_run(command, **_kwargs):
        calls.append(command)
        if "-list_devices" in command:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr='[dshow] "Integrated Camera" (video)\n',
            )
        Path(command[-1]).write_bytes(b"photo" * 100)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tools_module.subprocess, "run", fake_run)
    result = capture_photo(
        "native.jpg",
        workspace_root=tmp_path,
        approval_callback=lambda _action, _cwd: True,
    )

    capture_command = calls[-1]
    assert expected_backend in capture_command
    assert expected_input in capture_command
    assert "已拍照并保存" in result


def test_run_command_uses_powershell_on_windows(tmp_path: Path, monkeypatch):
    recorded = []
    recorded_kwargs = []
    monkeypatch.setattr(tools_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        tools_module.shutil,
        "which",
        lambda name: "C:\\Program Files\\PowerShell\\7\\pwsh.exe"
        if name == "pwsh"
        else None,
    )

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return "ok\n", ""

        def terminate(self):
            pass

        def kill(self):
            pass

    def fake_popen(command, **kwargs):
        recorded.append(command)
        recorded_kwargs.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr(tools_module.subprocess, "Popen", fake_popen)
    result = run_command(
        "Write-Output ok",
        workspace_root=tmp_path,
        approval_callback=lambda _command, _cwd: True,
    )

    assert recorded[0][0].endswith("pwsh.exe")
    assert "-NoProfile" in recorded[0]
    assert recorded_kwargs[0]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert recorded_kwargs[0]["stdin"] is subprocess.DEVNULL
    if hasattr(subprocess, "STARTUPINFO"):
        assert recorded_kwargs[0]["startupinfo"].wShowWindow == subprocess.SW_HIDE
    else:
        assert "startupinfo" not in recorded_kwargs[0]
    assert "退出码: 0" in result
