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


def test_every_session_exposes_the_persistent_browser_search_tool():
    first = AgentSession(client=FakeClient(), model_name="test-model")
    second = AgentSession(client=FakeClient(), model_name="test-model")

    assert "browser_search" in first.tool_handlers
    assert "browser_search" in second.tool_handlers
    assert any(
        item["function"]["name"] == "browser_search"
        for item in agent_module.TOOL_DEFINITIONS
    )
    assert "MUST call browser_search" in first.messages[0]["content"]


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


def test_session_can_switch_model_without_discarding_history():
    client = FakeClient()
    session = AgentSession(client=client, model_name="deepseek-v4-flash")

    assert session.ask("保留这段历史") == "收到"
    session.set_model_name("glm-5.3")

    assert session.model_name == "glm-5.3"
    assert session.vision_router.text_model == "glm-5.3"
    assert session.messages[1]["content"] == "保留这段历史"
    assert session.ask("使用新模型继续") == "收到"
    assert client.chat.completions.calls[-1]["model"] == "glm-5.3"


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


def test_agent_applies_live_direction_change_after_blocked_model_call():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class SteerableCompletions:
        def create(self, **kwargs):
            calls.append({**kwargs, "messages": list(kwargs["messages"])})
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=2)
                content = "原计划结果"
            else:
                content = "按新方向完成"
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content, tool_calls=None)
                    )
                ]
            )

    events = []
    session = AgentSession(
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=SteerableCompletions())
        ),
        model_name="test-model",
        event_callback=lambda kind, message: events.append((kind, message)),
    )
    result = []

    worker = threading.Thread(
        target=lambda: result.append(session.ask("先按原计划处理")),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=1)
    assert session.request_direction_change("改为只检查测试，不要继续原计划") == 1
    release.set()
    worker.join(timeout=3)

    assert result == ["按新方向完成"]
    assert len(calls) == 2
    steering_message = calls[1]["messages"][-1]
    assert steering_message["role"] == "user"
    assert "改为只检查测试" in steering_message["content"]
    assert ("direction_applied", "改为只检查测试，不要继续原计划") in events


def test_live_direction_change_skips_pending_tool_calls_without_breaking_history(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    tool_call = SimpleNamespace(
        id="steer-call",
        function=SimpleNamespace(
            name="create_file",
            arguments='{"path":"must-not-exist.txt","content":"old plan"}',
        ),
    )

    class ToolSteerCompletions:
        def create(self, **kwargs):
            calls.append({**kwargs, "messages": list(kwargs["messages"])})
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=2)
                message = SimpleNamespace(content=None, tool_calls=[tool_call])
            else:
                message = SimpleNamespace(content="已改按新方向完成", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    session = AgentSession(
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=ToolSteerCompletions())
        ),
        model_name="test-model",
        workspace=tmp_path,
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(session.ask("先创建文件")),
        daemon=True,
    )
    worker.start()
    assert entered.wait(timeout=1)
    session.steer("改为只说明方案，不要创建文件")
    release.set()
    worker.join(timeout=3)

    assert result == ["已改按新方向完成"]
    assert not (tmp_path / "must-not-exist.txt").exists()
    second_messages = calls[1]["messages"]
    assert any(
        item.get("role") == "tool"
        and item.get("tool_call_id") == "steer-call"
        and "停止" in item.get("content", "")
        for item in second_messages
    )
    assert "改为只说明方案" in second_messages[-1]["content"]


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
    assert [kind for kind, _message in events] == [
        "think",
        "tool_start",
        "tool_result",
    ]


def test_agent_uses_provider_visible_reasoning_for_think_event(tmp_path):
    tool_call = SimpleNamespace(
        id="call-reasoning",
        function=SimpleNamespace(name="git_status", arguments="{}"),
    )
    responses = iter(
        [
            SimpleNamespace(
                content=None,
                reasoning_content="先检查工作区状态，再决定是否需要修改文件。",
                tool_calls=[tool_call],
            ),
            SimpleNamespace(content="完成", tool_calls=None),
        ]
    )

    class ToolCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=next(responses))]
            )

    events = []
    session = AgentSession(
        client=SimpleNamespace(chat=SimpleNamespace(completions=ToolCompletions())),
        model_name="test-model",
        workspace=tmp_path,
        event_callback=lambda kind, message: events.append((kind, message)),
    )

    assert session.ask("检查状态") == "完成"
    assert events[0] == ("think", "先检查工作区状态，再决定是否需要修改文件。")


def test_agent_extracts_tagged_thinking_without_leaking_tags(tmp_path):
    responses = iter(
        [
            SimpleNamespace(
                content="<think>先整理目录信息</think>最终答案",
                tool_calls=None,
            )
        ]
    )

    class Completions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=next(responses))]
            )

    events = []
    session = AgentSession(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        model_name="test-model",
        workspace=tmp_path,
        event_callback=lambda kind, message: events.append((kind, message)),
    )

    assert session.ask("回答") == "最终答案"
    assert events == [("think", "先整理目录信息")]


def test_agent_stops_repeated_browser_search_failures(tmp_path):
    responses = iter(
        [
            SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        id="browser-call-1",
                        function=SimpleNamespace(
                            name="browser_search",
                            arguments='{"query":"测试"}',
                        ),
                    )
                ],
            ),
            SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        id="browser-call-2",
                        function=SimpleNamespace(
                            name="browser_search",
                            arguments='{"query":"测试"}',
                        ),
                    )
                ],
            ),
        ]
    )
    calls = 0

    class RepeatingBrowserCompletions:
        def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(message=next(responses))])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=RepeatingBrowserCompletions())
    )
    session = AgentSession(client=client, workspace=tmp_path)
    session.tool_handlers["browser_search"] = lambda **_kwargs: (
        "浏览器搜索不可用：运行 AI Harness 的 Python 环境未安装 Playwright。"
    )

    with pytest.raises(RuntimeError, match="连续失败两次"):
        session.ask("搜索测试")

    assert calls == 2
    assert "系统已停止重复调用" in session.messages[-1]["content"]


def test_agent_limits_successful_browser_searches_per_turn(tmp_path):
    responses = [
        SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    id=f"browser-call-{index}",
                    function=SimpleNamespace(
                        name="browser_search",
                        arguments=f'{{"query":"测试 {index}"}}',
                    ),
                )
            ],
        )
        for index in range(1, 5)
    ]
    responses.append(SimpleNamespace(content="已根据搜索结果回答。", tool_calls=None))
    browser_calls = []

    class RepeatingBrowserCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=responses.pop(0))])

    session = AgentSession(
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=RepeatingBrowserCompletions())
        ),
        workspace=tmp_path,
    )
    session.tool_handlers["browser_search"] = (
        lambda **kwargs: browser_calls.append(kwargs) or "搜索结果"
    )

    assert session.ask("搜索测试") == "已根据搜索结果回答。"
    assert len(browser_calls) == session.MAX_BROWSER_SEARCH_CALLS_PER_TURN
    assert "达到 3 次上限" in session.messages[-2]["content"]


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


def test_gui_mousewheel_normalizes_high_resolution_windows_events(monkeypatch):
    import ai_harness.gui as gui_module

    monkeypatch.setattr(gui_module.platform, "system", lambda: "Windows")
    assert gui_module._mousewheel_units(SimpleNamespace(delta=60, num=None)) == -1
    assert gui_module._mousewheel_units(SimpleNamespace(delta=240, num=None)) == -2


def test_gui_decodes_tk9_touchpad_scroll_delta():
    import ai_harness.gui as gui_module

    assert gui_module._touchpad_scroll_units(SimpleNamespace(delta=12)) == pytest.approx(-0.25)
    assert gui_module._touchpad_scroll_units(SimpleNamespace(delta=0xFFF4)) == pytest.approx(0.25)


def test_gui_mousewheel_coalesces_bursts_into_one_canvas_repaint():
    from ai_harness.gui import HarnessGUI

    class FakeRoot:
        def __init__(self):
            self.callbacks = []

        def after(self, delay, callback, widget):
            self.callbacks.append((delay, callback, widget))
            return "after-1"

    class FakeScrollable:
        def __init__(self):
            self.moves = []

        def __str__(self):
            return ".chat"

        def yview(self):
            return (0.2, 0.7)

        def winfo_height(self):
            return 800

        def yview_moveto(self, target):
            self.moves.append(target)

    gui = HarnessGUI.__new__(HarnessGUI)
    gui.root = FakeRoot()
    widget = FakeScrollable()

    gui._scroll_with_mousewheel(widget, -1)
    gui._scroll_with_mousewheel(widget, -1)

    assert len(gui.root.callbacks) == 1
    _, callback, callback_widget = gui.root.callbacks.pop()
    callback(callback_widget)

    assert widget.moves == [pytest.approx(0.14)]


def test_gui_mousewheel_routes_nested_widgets_to_their_scroll_container():
    from ai_harness.gui import HarnessGUI

    class FakeWidget:
        def __init__(self, path):
            self.path = path

        def __str__(self):
            return self.path

    gui = HarnessGUI.__new__(HarnessGUI)
    gui.root = SimpleNamespace()
    gui.chat_canvas = FakeWidget(".chat")
    gui.project_tree = FakeWidget(".sidebar.tree")
    calls = []
    gui._scroll_with_mousewheel = lambda widget, units: calls.append((widget, units))

    result = gui._on_mousewheel(
        SimpleNamespace(
            delta=1,
            num=None,
            widget=FakeWidget(".chat.message.body"),
        )
    )

    assert result == "break"
    assert calls == [(gui.chat_canvas, -1)]


def test_gui_mousewheel_uses_live_pointer_when_macos_targets_focused_widget():
    from ai_harness.gui import HarnessGUI

    class FakeWidget:
        def __init__(self, path):
            self.path = path

        def __str__(self):
            return self.path

    chat_child = FakeWidget(".chat.message.body")

    class FakeRoot:
        def winfo_containing(self, x, y):
            return chat_child if (x, y) == (700, 400) else None

        def winfo_pointerx(self):
            return 700

        def winfo_pointery(self):
            return 400

    gui = HarnessGUI.__new__(HarnessGUI)
    gui.root = FakeRoot()
    gui.chat_canvas = FakeWidget(".chat")
    gui.project_tree = FakeWidget(".sidebar.tree")

    target = gui._mousewheel_target(
        SimpleNamespace(
            widget=FakeWidget(".composer.prompt"),
            x_root=0,
            y_root=0,
        )
    )

    assert target is gui.chat_canvas


def test_gui_mousewheel_binds_each_concrete_widget_once():
    from ai_harness.gui import HarnessGUI, MOUSEWHEEL_SEQUENCES, TOUCHPAD_SCROLL_SEQUENCE

    class FakeWidget:
        def __init__(self):
            self.bindings = []

        def bind(self, sequence, callback, add=None):
            self.bindings.append((sequence, callback, add))

    gui = HarnessGUI.__new__(HarnessGUI)
    gui._mousewheel_sequences = [*MOUSEWHEEL_SEQUENCES, TOUCHPAD_SCROLL_SEQUENCE]
    widget = FakeWidget()

    gui._bind_mousewheel(widget)
    gui._bind_mousewheel(widget)

    assert [binding[0] for binding in widget.bindings] == [
        "<MouseWheel>",
        "<Button-4>",
        "<Button-5>",
        "<TouchpadScroll>",
    ]
    assert all(binding[2] == "+" for binding in widget.bindings)


def test_gui_output_autoscroll_settles_but_manual_wheel_cancels_it():
    from ai_harness.gui import HarnessGUI

    class FakeRoot:
        def __init__(self):
            self.idle = []
            self.delayed = []

        def after_idle(self, callback, *args):
            self.idle.append((callback, args))

        def after(self, delay, callback, *args):
            self.delayed.append((delay, callback, args))

    gui = HarnessGUI.__new__(HarnessGUI)
    gui.root = FakeRoot()
    gui.closing = False
    scrolls = []
    gui._scroll_chat_to_bottom = lambda: scrolls.append("bottom")

    gui._schedule_chat_to_bottom()
    callback, args = gui.root.idle.pop()
    callback(*args)

    assert scrolls == ["bottom"]
    assert len(gui.root.delayed) == 1

    gui._cancel_chat_autoscroll()
    _, callback, args = gui.root.delayed.pop()
    callback(*args)

    assert scrolls == ["bottom"]


def test_connection_change_defers_busy_session_rebuild():
    from ai_harness.gui import HarnessGUI

    live_session = object()
    busy_runtime = {
        "session": live_session,
        "busy": True,
        "rebuild_session_after_busy": False,
    }
    idle_runtime = {
        "session": object(),
        "busy": False,
        "rebuild_session_after_busy": True,
    }

    HarnessGUI._invalidate_sessions_for_connection_change(
        [busy_runtime, idle_runtime]
    )

    assert busy_runtime["session"] is live_session
    assert busy_runtime["rebuild_session_after_busy"] is True
    assert idle_runtime["session"] is None
    assert idle_runtime["rebuild_session_after_busy"] is False


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

    both_started = threading.Event()
    release_workers = threading.Event()
    started_tasks: list[str] = []
    started_lock = threading.Lock()

    class FakeSession:
        def __init__(self, **kwargs):
            self.messages = [{"role": "system", "content": "sys"}]
            self.stop_event = threading.Event()
            self.model_name = kwargs.get("model_name") or "test-model"
            self.event_callback = kwargs.get("event_callback")

        def ask(self, task, attachments=None):
            with started_lock:
                started_tasks.append(task)
                if len(started_tasks) == 2:
                    both_started.set()
            if not release_workers.wait(timeout=10):
                raise AssertionError("并发测试 worker 未被释放")
            if self.event_callback is not None:
                self.event_callback("tool_start", "执行工具")
                self.event_callback("tool_result", "工具结果")
            return f"回答:{task}"

        def resume(self):
            return "继续回答"

        def request_stop(self):
            self.stop_event.set()

        def repair_tool_call_history(self):
            return 0

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

        assert both_started.wait(timeout=10), "两个 Session 未能同时启动"

        # Both Sessions must be busy at the same time (true parallelism).
        assert gui._runtime(session_a_id)["busy"] is True
        assert gui._runtime(session_b_id)["busy"] is True

        release_workers.set()
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
        release_workers.set()
        root.destroy()


def test_gui_queues_follow_up_until_current_session_turn_finishes(monkeypatch, tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception as exc:  # headless CI without an X server
        pytest.skip(f"无法初始化 Tk 界面：{exc}")

    import ai_harness.gui as gui_module
    from ai_harness.gui import HarnessGUI

    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    calls = []
    calls_lock = threading.Lock()

    class QueueSession:
        def __init__(self, **kwargs):
            self.messages = [{"role": "system", "content": "sys"}]
            self.stop_event = threading.Event()
            self.model_name = kwargs.get("model_name") or "test-model"
            self.event_callback = kwargs.get("event_callback")

        def ask(self, task, attachments=None):
            with calls_lock:
                calls.append(task)
                call_number = len(calls)
            self.messages.append({"role": "user", "content": task})
            if call_number == 1:
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("队列测试第一轮未被释放")
                answer = "第一轮完成"
            else:
                second_started.set()
                answer = f"回答:{task}"
            self.messages.append({"role": "assistant", "content": answer})
            return answer

        def resume(self):
            return "继续回答"

        def request_stop(self):
            self.stop_event.set()

        def repair_tool_call_history(self):
            return 0

        def set_permission_mode(self, mode):
            return mode

        def generate_session_title(self, question, answer, max_chars=11):
            return "队列测试"

    monkeypatch.setattr(gui_module, "AgentSession", QueueSession)

    try:
        gui = HarnessGUI(
            root,
            workspace=str(tmp_path),
            state_path=str(tmp_path / "gui-state.json"),
            config_path=str(tmp_path / "conn.env"),
        )
        gui.prompt.insert("1.0", "第一轮任务")
        gui.send_message()
        session_id = gui.current_session_id
        assert first_started.wait(timeout=2)

        gui.prompt.insert("1.0", "第一轮完成后继续检查")
        gui.queue_message()
        assert len(gui._runtime(session_id)["follow_up_queue"]) == 1

        release_first.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not second_started.is_set():
            gui._drain_events()
            root.update_idletasks()
            time.sleep(0.02)
        assert second_started.is_set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and gui._runtime(session_id)["busy"]:
            gui._drain_events()
            root.update_idletasks()
            time.sleep(0.02)
        gui._drain_events()

        assert calls == ["第一轮任务", "第一轮完成后继续检查"]
        assert not gui._runtime(session_id)["follow_up_queue"]
        assert any(
            item["body"] == "回答:第一轮完成后继续检查"
            for item in gui._session_record(session_id)["items"]
        )
    finally:
        release_first.set()
        root.destroy()


def test_gui_can_send_new_message_after_stopping_session(monkeypatch, tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception as exc:  # headless CI without an X server
        pytest.skip(f"无法初始化 Tk 界面：{exc}")

    import ai_harness.gui as gui_module
    from ai_harness.gui import HarnessGUI

    first_started = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()

    class StoppableSession:
        def __init__(self, **kwargs):
            self.messages = [{"role": "system", "content": "sys"}]
            self.stop_event = threading.Event()
            self.model_name = kwargs.get("model_name") or "test-model"
            self.event_callback = kwargs.get("event_callback")

        def ask(self, task, attachments=None):
            with calls_lock:
                calls.append(task)
            self.messages.append({"role": "user", "content": task})
            if task == "第一轮任务":
                first_started.set()
                while not self.stop_event.wait(0.01):
                    pass
                raise AgentPaused("运行已由用户停止")
            answer = f"回答:{task}"
            self.messages.append({"role": "assistant", "content": answer})
            return answer

        def resume(self):
            return "继续回答"

        def request_stop(self):
            self.stop_event.set()

        def repair_tool_call_history(self):
            return 0

        def set_permission_mode(self, mode):
            return mode

        def generate_session_title(self, question, answer, max_chars=11):
            return "停止后继续"

    monkeypatch.setattr(gui_module, "AgentSession", StoppableSession)

    try:
        gui = HarnessGUI(
            root,
            workspace=str(tmp_path),
            state_path=str(tmp_path / "gui-state.json"),
            config_path=str(tmp_path / "conn.env"),
        )
        gui.prompt.insert("1.0", "第一轮任务")
        gui.send_message()
        session_id = gui.current_session_id
        assert first_started.wait(timeout=2)

        gui.stop_running()
        # The stop request is cooperative: the text area becomes editable
        # immediately, while the send action waits for the paused event.
        assert gui.prompt.cget("state") == "normal"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            gui._drain_events()
            root.update_idletasks()
            runtime = gui._runtime(session_id)
            if runtime["paused"] and not runtime["stop_pending"]:
                break
            time.sleep(0.02)
        gui._drain_events()

        runtime = gui._runtime(session_id)
        assert runtime["paused"] is True
        assert runtime["stop_pending"] is False
        assert gui.prompt.cget("state") == "normal"
        assert gui.send_button.cget("text") == "发送"

        gui.prompt.insert("1.0", "第二轮任务")
        gui.send_message()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            gui._drain_events()
            root.update_idletasks()
            if calls == ["第一轮任务", "第二轮任务"] and not gui._runtime(session_id)["busy"]:
                break
            time.sleep(0.02)
        gui._drain_events()

        assert calls == ["第一轮任务", "第二轮任务"]
        assert not gui._runtime(session_id)["busy"]
        assert any(
            item["body"] == "回答:第二轮任务"
            for item in gui._session_record(session_id)["items"]
        )
    finally:
        root.destroy()


def test_gui_user_card_renders_and_restores_image_attachment(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception as exc:  # headless CI without an X server
        pytest.skip(f"无法初始化 Tk 界面：{exc}")

    from PIL import Image
    from ai_harness.gui import HarnessGUI

    image_path = tmp_path / "chat-image.png"
    Image.new("RGB", (320, 180), color="#6b9ee6").save(image_path)

    try:
        gui = HarnessGUI(
            root,
            workspace=str(tmp_path),
            state_path=str(tmp_path / "gui-state.json"),
            config_path=str(tmp_path / "conn.env"),
        )
        card = gui._add_card(
            "user",
            "你",
            "请描述这张图片",
            attachments=[image_path],
        )
        root.update_idletasks()

        assert card is not None
        assert card["image_count"] == 1
        assert len(gui._chat_image_references) == 1
        assert gui._current_record()["items"][-1]["attachments"] == [
            str(image_path.resolve())
        ]

        gui._render_current_session()
        root.update_idletasks()
        assert len(gui._chat_image_references) == 1

        legacy_path = gui.attachments_dir / "clipboard-legacy.png"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), color="#e66b6b").save(legacy_path)
        gui._current_record()["items"] = [
            {
                "role": "user",
                "title": "你",
                "body": f"旧消息\n\n附件：{legacy_path.name}",
            }
        ]
        gui._render_current_session()
        root.update_idletasks()
        assert len(gui._chat_image_references) == 1
    finally:
        root.destroy()


def test_gui_composer_context_controls_switch_workspace_model_and_permission(
    monkeypatch, tmp_path
):
    tkinter = pytest.importorskip("tkinter")
    from tkinter import ttk

    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception as exc:  # headless CI without an X server
        pytest.skip(f"无法初始化 Tk 界面：{exc}")

    import ai_harness.gui as gui_module
    from ai_harness.gui import HarnessGUI

    other_workspace = tmp_path / "other-project"
    other_workspace.mkdir()
    try:
        gui = HarnessGUI(
            root,
            workspace=str(tmp_path),
            state_path=str(tmp_path / "gui-state.json"),
            config_path=str(tmp_path / "conn.env"),
        )
        assert isinstance(gui.composer_project, ttk.Button)
        assert isinstance(gui.composer_model, ttk.Combobox)
        assert isinstance(gui.composer_permission, ttk.Combobox)

        monkeypatch.setattr(
            gui_module.filedialog,
            "askdirectory",
            lambda **_kwargs: str(other_workspace),
        )
        gui.composer_project.invoke()
        assert gui.workspace == other_workspace.resolve()

        gui.composer_model.set("glm-5.3")
        gui.change_model(object())
        assert gui.model_name == "glm-5.3"
        assert gui.composer_model.get() == "glm-5.3"

        gui.composer_permission.set("请求批准")
        gui.change_permission(object())
        assert gui.permission_mode == "ask"
    finally:
        root.destroy()


def test_gui_extracts_remote_search_image_markdown():
    from ai_harness.gui import _extract_remote_image_refs, _remove_remote_image_markdown

    body = (
        "刘德华搜索结果\n"
        "![刘德华头像](https://cdn.example.test/avatar.jpg)\n"
        "![壁纸](<https://cdn.example.test/wallpaper.webp>)"
    )

    assert _extract_remote_image_refs(body) == [
        ("刘德华头像", "https://cdn.example.test/avatar.jpg"),
        ("壁纸", "https://cdn.example.test/wallpaper.webp"),
    ]
    assert _remove_remote_image_markdown(body) == "刘德华搜索结果\n刘德华头像\n壁纸"


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
