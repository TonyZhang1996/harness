from types import SimpleNamespace

from ai_harness.agent import (
    AgentSession,
    _remove_visible_reasoning_blocks,
    _split_assistant_response,
)


class OneShotCompletions:
    def __init__(self, message):
        self.message = message
        self.calls = []

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


def _session_for(message, events=None):
    completions = OneShotCompletions(message)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    session = AgentSession(
        client=client,
        model_name="test-model",
        event_callback=(
            (lambda kind, message: events.append((kind, message)))
            if events is not None
            else None
        ),
    )
    return session, completions


def test_provider_reasoning_field_is_emitted_separately_and_not_saved():
    events = []
    message = SimpleNamespace(
        content="这是给用户的最终答案。",
        reasoning_content="这里是供应商返回的内部思考。",
        tool_calls=None,
    )
    session, _completions = _session_for(message, events)

    assert session.ask("请处理这个问题") == "这是给用户的最终答案。"
    assert events == [("think", "这里是供应商返回的内部思考。")]
    assert session.messages[-1]["content"] == "这是给用户的最终答案。"
    assert "内部思考" not in session.messages[-1]["content"]


def test_tagged_reasoning_is_removed_from_answer_and_kept_as_think():
    events = []
    message = SimpleNamespace(
        content="<think>先检查上下文，再给出结论。</think>\n\n最终结果：已完成。",
        tool_calls=None,
    )
    session, _completions = _session_for(message, events)

    assert session.ask("继续") == "最终结果：已完成。"
    assert events == [("think", "先检查上下文，再给出结论。")]
    assert "<think>" not in session.messages[-1]["content"]


def test_explicit_final_marker_removes_leading_narration_conservatively():
    content = (
        "我先分析当前情况。\n\n"
        "让我给出最终回答。\n\n"
        "中间过程不应进入正式回答。\n\n"
        "给出结论。\n\n"
        "## 结论\n已完成检查，未发现问题。"
    )

    assert _remove_visible_reasoning_blocks(content) == "## 结论\n已完成检查，未发现问题。"


def test_plain_answer_is_not_trimmed_without_an_explicit_boundary():
    content = "结论：当前服务正常。\n建议继续观察。"

    assert _remove_visible_reasoning_blocks(content) == content


def test_loaded_assistant_history_is_sanitized_before_a_new_turn():
    message = SimpleNamespace(content="恢复后的回答", tool_calls=None)
    session, _completions = _session_for(message)
    session.messages.append(
        {
            "role": "assistant",
            "content": "我先分析。\n\n给出结论。\n\n恢复后的正式回答。",
        }
    )

    assert session.repair_tool_call_history() == 0
    assert session.messages[-1]["content"] == "恢复后的正式回答。"


def test_tool_call_preamble_is_not_carried_into_follow_up_context(tmp_path):
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="unknown-tool", arguments="{}"),
    )

    class TwoStepCompletions(OneShotCompletions):
        def __init__(self):
            super().__init__(None)
            self.responses = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="我正在分析并准备调用工具。",
                                tool_calls=[tool_call],
                            )
                        )
                    ]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="工具调用已完成，最终结果如下。",
                                tool_calls=None,
                            )
                        )
                    ]
                ),
            ]

        def create(self, **kwargs):
            self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
            return self.responses.pop(0)

    completions = TwoStepCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    session = AgentSession(client=client, model_name="test-model", workspace=tmp_path)

    assert session.ask("请处理") == "工具调用已完成，最终结果如下。"
    first_assistant = next(
        item
        for item in session.messages
        if item.get("role") == "assistant" and item.get("tool_calls")
    )
    assert "content" not in first_assistant
    assert "我正在分析" not in str(completions.calls[1]["messages"])


def test_split_assistant_response_handles_provider_field_and_content():
    thinking, answer = _split_assistant_response(
        {
            "reasoning_content": "provider reasoning",
            "content": "final answer",
        }
    )

    assert thinking == "provider reasoning"
    assert answer == "final answer"
