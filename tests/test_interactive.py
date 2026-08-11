import threading
import time
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


def test_agent_repairs_interrupted_tool_call_before_resume():
    tool_call = SimpleNamespace(
        id="call-interrupted",
        function=SimpleNamespace(name="run_command", arguments='{"command":"echo ok"}'),
    )
    captured = []

    class ResumeCompletions:
        def create(self, **kwargs):
            captured.append(kwargs["messages"])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="已恢复", tool_calls=None))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=ResumeCompletions()))
    session = AgentSession(client=client, model_name="test-model")
    session.messages.extend(
        [
            {"role": "user", "content": "执行任务"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {"name": "run_command", "arguments": "{}"},
                    }
                ],
            },
        ]
    )

    assert session.resume() == "已恢复"
    messages = captured[0]
    assert messages[-1]["role"] == "assistant"
    tool_index = next(i for i, item in enumerate(messages) if item.get("role") == "tool")
    assert messages[tool_index]["tool_call_id"] == tool_call.id
    assert "中断" in messages[tool_index]["content"]


def test_agent_retries_transient_model_transport_error(monkeypatch):
    calls = 0

    class FlakyCompletions:
        def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Error -3 while decompressing data: incorrect header check")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="重试成功", tool_calls=None))]
            )

    monkeypatch.setattr(agent_module.time, "sleep", lambda _seconds: None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=FlakyCompletions()))
    session = AgentSession(client=client, model_name="test-model")

    assert session.ask("测试网络重试") == "重试成功"
    assert calls == 2


def test_agent_sessions_run_concurrently_with_independent_history():
    import time

    class SlowCompletions:
        def __init__(self, content):
            self.content = content
            self.calls = []

        def create(self, **kwargs):
            self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
            time.sleep(0.05)
            message = SimpleNamespace(content=self.content, tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class SlowClient:
        def __init__(self, content):
            self.chat = SimpleNamespace(completions=SlowCompletions(content))

    first_client = SlowClient("第一个会话完成")
    second_client = SlowClient("第二个会话完成")
    session_a = AgentSession(client=first_client, model_name="test-model")
    session_b = AgentSession(client=second_client, model_name="test-model")

    results = {}

    def run_a():
        results["a"] = session_a.ask("任务A")

    def run_b():
        results["b"] = session_b.ask("任务B")

    threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results == {"a": "第一个会话完成", "b": "第二个会话完成"}
    assert "任务A" in first_client.chat.completions.calls[0]["messages"][1]["content"]
    assert "任务B" in second_client.chat.completions.calls[0]["messages"][1]["content"]
    assert session_a.messages[1]["content"] == "任务A"
    assert session_b.messages[1]["content"] == "任务B"
    assert len(session_a.messages) == 3
    assert len(session_b.messages) == 3


def test_gui_mousewheel_units_support_mac_and_x11(monkeypatch):
    import ai_harness.gui as gui_module

    monkeypatch.setattr(gui_module.platform, "system", lambda: "Darwin")
    assert gui_module._mousewheel_units(SimpleNamespace(delta=1, num=None)) == -1
    assert gui_module._mousewheel_units(SimpleNamespace(delta=-1, num=None)) == 1

    monkeypatch.setattr(gui_module.platform, "system", lambda: "Linux")
    assert gui_module._mousewheel_units(SimpleNamespace(delta=120, num=None)) == -1
    assert gui_module._mousewheel_units(SimpleNamespace(delta=-240, num=None)) == 2
    assert gui_module._mousewheel_units(SimpleNamespace(delta=0, num=4)) == -1
    assert gui_module._mousewheel_units(SimpleNamespace(delta=0, num=5)) == 1


def test_gui_runs_multiple_sessions_concurrently(monkeypatch, tmp_path):
    """Two Sessions can run agent turns in parallel with independent history."""
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception as exc:  # headless CI without an X server
        pytest.skip(f"无法初始化 Tk 界面：{exc}")

    import ai_harness.gui as gui_module
    from ai_harness.gui import HarnessGUI

    barrier = threading.Barrier(2)
    started_tasks: list[str] = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.messages = [{"role": "system", "content": "sys"}]
            self.stop_event = threading.Event()
            self.model_name = kwargs.get("model_name") or "test-model"
            self.event_callback = kwargs.get("event_callback")

        def ask(self, task, attachments=None):
            started_tasks.append(task)
            barrier.wait(timeout=5)
            if self.event_callback is not None:
                self.event_callback("tool_start", "执行工具")
                self.event_callback("tool_result", "工具结果")
            return f"回答:{task}"

        def resume(self):
            return "继续回答"

        def request_stop(self):
            self.stop_event.set()

        def set_permission_mode(self, mode):
            return mode

        def generate_session_title(self, question, answer, max_chars=11):
            return "并发标题"

    monkeypatch.setattr(gui_module, "AgentSession", FakeSession)

    try:
        gui = HarnessGUI(
            root,
            workspace=str(tmp_path),
            state_path=str(tmp_path / "gui-state.json"),
            config_path=str(tmp_path / "conn.env"),
        )
        gui.prompt.insert("1.0", "任务A")
        gui.send_message()
        session_a_id = gui.current_session_id

        gui.new_conversation()
        session_b_id = gui.current_session_id
        gui.prompt.insert("1.0", "任务B")
        gui.send_message()

        # Both Sessions must be busy at the same time (true parallelism).
        assert gui._runtime(session_a_id)["busy"] is True
        assert gui._runtime(session_b_id)["busy"] is True

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            gui._drain_events()
            if (
                not gui._runtime(session_a_id)["busy"]
                and not gui._runtime(session_b_id)["busy"]
            ):
                break
            time.sleep(0.02)
        gui._drain_events()

        assert not gui._runtime(session_a_id)["busy"]
        assert not gui._runtime(session_b_id)["busy"]

        record_a = gui._session_record(session_a_id)
        record_b = gui._session_record(session_b_id)
        assert record_a["title"] == "并发标题"
        assert record_a["items"][-1]["body"] == "回答:任务A"
        assert record_b["items"][-1]["body"] == "回答:任务B"
        tool_bodies = [
            item["body"] for item in record_a["items"] if item["role"] == "tool"
        ]
        assert "执行工具" in tool_bodies

        # Switching back to Session A renders only its own cards.
        gui._switch_session(session_a_id)
        bodies = [item["body"] for item in gui._current_record()["items"]]
        assert "回答:任务A" in bodies
    finally:
        root.destroy()


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
