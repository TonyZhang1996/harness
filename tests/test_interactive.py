import threading
from types import SimpleNamespace

import pytest

import ai_harness.agent as agent_module
from ai_harness import cli
from ai_harness.agent import AgentPaused, AgentSession


class FakeCompletions:
    def __init__(self):
        self.calls = []
        self.content = "收到"

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        message = SimpleNamespace(content=self.content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


def test_agent_session_preserves_conversation_history():
    client = FakeClient()
    session = AgentSession(client=client)

    assert session.ask("第一句话") == "收到"
    assert session.ask("第二句话") == "收到"

    calls = client.chat.completions.calls
    assert calls[1]["messages"][1]["content"] == "第一句话"
    assert calls[1]["messages"][2]["content"] == "收到"
    assert calls[1]["messages"][3]["content"] == "第二句话"

    session.clear()
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "system"


def test_agent_inlines_text_attachment(tmp_path):
    attachment = tmp_path / "notes.txt"
    attachment.write_text("附件正文", encoding="utf-8")
    client = FakeClient()
    session = AgentSession(client=client)

    assert session.ask("请总结", attachments=[attachment]) == "收到"
    content = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "notes.txt" in content
    assert "附件正文" in content


def test_agent_generates_compact_session_title():
    client = FakeClient()
    client.chat.completions.content = "标题：审批按钮显示修复"
    session = AgentSession(client=client, model_name="test-model")

    title = session.generate_session_title(
        "审批窗口为什么没有按钮？",
        "已固定允许和拒绝按钮。",
    )

    assert title == "审批按钮显示修复"
    assert len(title) <= 11


def test_session_title_normalization_removes_wrapping_text():
    assert AgentSession._normalize_session_title("“标题：项目排序功能。”") == "项目排序功能"


def test_custom_gui_approver_survives_permission_switch(tmp_path):
    approvals = []

    def approver(command, cwd):
        approvals.append((command, cwd))
        return True

    session = AgentSession(
        client=FakeClient(),
        model_name="test-model",
        workspace=tmp_path,
        approver=approver,
    )

    assert session.approver is approver
    session.set_permission_mode("auto")
    assert session.approver("command", tmp_path) is True
    session.set_permission_mode("ask")
    assert session.approver is approver


def test_agent_turn_can_stop_and_resume():
    entered = threading.Event()
    release = threading.Event()
    call_count = 0

    class BlockingCompletions:
        def create(self, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                entered.set()
                release.wait(timeout=2)
            message = SimpleNamespace(content="继续完成", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=BlockingCompletions()))
    session = AgentSession(client=client, model_name="test-model")
    errors = []

    def run():
        try:
            session.ask("执行任务")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(timeout=1)
    session.request_stop()
    release.set()
    worker.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], AgentPaused)
    assert session.resume() == "继续完成"


def test_system_prompt_describes_windows_shell(monkeypatch):
    monkeypatch.setattr(agent_module.platform, "system", lambda: "Windows")
    session = AgentSession(client=FakeClient(), model_name="test-model")

    assert "Host operating system: Windows" in session.messages[0]["content"]
    assert "Native command shell: PowerShell" in session.messages[0]["content"]


def test_interactive_mode_supports_commands(monkeypatch, capsys):
    class FakeSession:
        def __init__(self, **_kwargs):
            self.cleared = False
            self.permission_mode = "ask"

        def clear(self):
            self.cleared = True

        def ask(self, task):
            return f"回答：{task}"

        def set_permission_mode(self, mode):
            self.permission_mode = mode
            return mode

    monkeypatch.setattr(cli, "AgentSession", FakeSession)
    inputs = iter(["/help", "你好", "/clear", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    cli.run_interactive()
    output = capsys.readouterr().out

    assert "交互命令" in output
    assert "回答：你好" in output
    assert "对话上下文已清空" in output
    assert "已退出" in output


def test_agent_executes_tool_and_reports_events(tmp_path):
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="create_file",
            arguments='{"path": "created.txt", "content": "hello"}',
        ),
    )
    responses = iter(
        [
            SimpleNamespace(content=None, tool_calls=[tool_call]),
            SimpleNamespace(content="完成", tool_calls=None),
        ]
    )

    class ToolCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=next(responses))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=ToolCompletions()))
    events = []
    session = AgentSession(
        client=client,
        model_name="test-model",
        workspace=tmp_path,
        event_callback=lambda kind, message: events.append((kind, message)),
    )

    assert session.ask("创建文件") == "完成"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "hello"
    assert [kind for kind, _message in events] == ["tool_start", "tool_result"]


def test_full_access_forces_automatic_command_approval(tmp_path):
    session = AgentSession(
        client=FakeClient(),
        model_name="test-model",
        workspace=tmp_path,
        approval_mode="never",
        full_access=True,
    )

    assert session.full_access is True
    assert session.approval_mode == "auto"
    assert session.approver("printf ok", tmp_path) is True


def test_session_can_switch_permission_modes_and_rebind_file_tools(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    session = AgentSession(
        client=FakeClient(),
        model_name="test-model",
        workspace=workspace,
        approval_mode="never",
    )

    assert session.permission_mode == "never"
    with pytest.raises(ValueError):
        session.tool_handlers["read_file"](path=str(secret))

    assert session.set_permission_mode("full-access") == "full-access"
    assert session.tool_handlers["read_file"](path=str(secret)) == "TOKEN=secret"
    assert session.approver("printf ok", workspace) is True
    assert "entire local filesystem" in session.messages[0]["content"]

    assert session.set_permission_mode("ask") == "ask"
    assert session.full_access is False
    assert "limited to the workspace" in session.messages[0]["content"]
    with pytest.raises(ValueError):
        session.tool_handlers["read_file"](path=str(secret))


def test_interactive_permission_commands_switch_without_model_calls(monkeypatch, capsys):
    sessions = []

    class FakeSession:
        def __init__(self, **_kwargs):
            self.model_name = "test-model"
            self.permission_mode = "ask"
            sessions.append(self)

        def set_permission_mode(self, mode):
            self.permission_mode = mode
            return mode

        def clear(self):
            pass

        def ask(self, _task):
            raise AssertionError("斜杠命令不应发送给模型")

    monkeypatch.setattr(cli, "AgentSession", FakeSession)
    inputs = iter(["/permissions auto", "/full-access", "/safe", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    cli.run_interactive()
    output = capsys.readouterr().out

    assert "帮我批准（auto）" in output
    assert "完全访问权限（full-access）" in output
    assert "请求批准（ask）" in output
    assert "禁止执行（never）" not in output
    assert sessions[0].permission_mode == "ask"


def test_interactive_permission_menu(monkeypatch, capsys):
    class FakeSession:
        model_name = "test-model"
        permission_mode = "ask"

        def __init__(self, **_kwargs):
            pass

        def set_permission_mode(self, mode):
            self.permission_mode = mode
            return mode

        def clear(self):
            pass

        def ask(self, _task):
            raise AssertionError("菜单不应发送给模型")

    monkeypatch.setattr(cli, "AgentSession", FakeSession)
    inputs = iter(["/permissions", "3", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    cli.run_interactive()
    output = capsys.readouterr().out

    assert "当前权限：请求批准（ask）" in output
    assert "权限已切换：完全访问权限（full-access）" in output


def test_removed_never_permission_is_rejected_by_cli(monkeypatch, capsys):
    class FakeSession:
        permission_mode = "ask"

        def set_permission_mode(self, _mode):
            raise AssertionError("已删除的权限不应传给 Session")

    assert cli._handle_permission_command(FakeSession(), "/permissions never") is True
    assert "无效权限模式" in capsys.readouterr().out
