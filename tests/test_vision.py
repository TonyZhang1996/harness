from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from ai_harness.agent import AgentSession
from ai_harness.config import OPENCODE_GO_BASE_URL
from ai_harness.plugins.vision_router import VisionConfig, VisionModelUnavailable


class FakeModels:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    def list(self):
        self.calls += 1
        return SimpleNamespace(data=self.records)


class FakeVisionCompletions:
    def __init__(self, *, vision_error: Exception | None = None):
        self.calls = []
        self.vision_error = vision_error

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        is_vision_call = isinstance(kwargs["messages"][-1]["content"], list)
        if is_vision_call:
            if self.vision_error is not None:
                raise self.vision_error
            content = "图片中有一个红色方框，图片文字为：VISION-EVIDENCE。"
        else:
            content = "文本模型已根据图片证据完成推理。"
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, records, *, vision_error: Exception | None = None):
        self.models = FakeModels(records)
        completions = FakeVisionCompletions(vision_error=vision_error)
        self.completions = completions
        self.chat = SimpleNamespace(completions=completions)


def make_image(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (8, 8), color="red").save(path)
    return path


def test_image_turn_routes_vision_then_text_model(tmp_path):
    image = make_image(tmp_path)
    client = FakeClient(
        [
            SimpleNamespace(id="text-model"),
            SimpleNamespace(id="qwen-vl-max"),
        ]
    )
    session = AgentSession(
        client=client,
        model_name="text-model",
        vision_config=VisionConfig(),
    )

    assert session.ask("请分析图片", attachments=[image]) == "文本模型已根据图片证据完成推理。"

    calls = client.completions.calls
    assert len(calls) == 2
    assert calls[0]["model"] == "qwen-vl-max"
    vision_content = calls[0]["messages"][-1]["content"]
    assert isinstance(vision_content, list)
    assert any(item["type"] == "image_url" for item in vision_content)
    image_item = next(item for item in vision_content if item["type"] == "image_url")
    assert image_item["image_url"]["url"].startswith("data:image/png;base64,")

    assert calls[1]["model"] == "text-model"
    text_content = calls[1]["messages"][1]["content"]
    assert isinstance(text_content, str)
    assert "VISION-EVIDENCE" in text_content
    assert "图片感知结果" in text_content


def test_explicit_candidate_works_when_model_catalog_is_unavailable(tmp_path):
    image = make_image(tmp_path)

    class ClientWithoutModels:
        def __init__(self):
            completions = FakeVisionCompletions()
            self.chat = SimpleNamespace(completions=completions)
            self.completions = completions

    client = ClientWithoutModels()
    session = AgentSession(
        client=client,
        model_name="text-model",
        vision_config=VisionConfig(
            candidates=("configured-vision-model",),
        ),
    )

    assert session.ask("识别图片", attachments=[image]) == "文本模型已根据图片证据完成推理。"
    assert client.completions.calls[0]["model"] == "configured-vision-model"


def test_opencode_go_uses_confirmed_vision_allowlist(tmp_path):
    image = make_image(tmp_path)
    client = FakeClient(
        [
            SimpleNamespace(id="mimo-v2-omni"),
            SimpleNamespace(id="mimo-v2.5-pro"),
            SimpleNamespace(id="deepseek-v4-flash"),
            SimpleNamespace(id="mimo-v2.5"),
        ]
    )
    session = AgentSession(
        client=client,
        model_name="deepseek-v4-flash",
        vision_config=VisionConfig(base_url=OPENCODE_GO_BASE_URL),
    )

    assert session.ask("识别图片", attachments=[image]) == "文本模型已根据图片证据完成推理。"
    assert client.completions.calls[0]["model"] == "mimo-v2.5"


def test_missing_vision_model_stops_image_request_without_text_call(tmp_path):
    image = make_image(tmp_path)
    client = FakeClient([SimpleNamespace(id="text-model")])
    events = []
    session = AgentSession(
        client=client,
        model_name="text-model",
        vision_config=VisionConfig(),
        event_callback=lambda kind, message: events.append((kind, message)),
    )

    with pytest.raises(VisionModelUnavailable, match="未找到可用的多模态模型"):
        session.ask("请看图", attachments=[image])

    assert client.completions.calls == []
    assert any(kind == "vision_error" for kind, _message in events)


def test_vision_call_failure_does_not_fallback_to_ocr_or_text_model(tmp_path):
    image = make_image(tmp_path)
    client = FakeClient(
        [SimpleNamespace(id="vision-model")],
        vision_error=RuntimeError("vision endpoint unavailable"),
    )
    session = AgentSession(
        client=client,
        model_name="text-model",
        vision_config=VisionConfig(model="vision-model"),
    )

    with pytest.raises(RuntimeError, match="图片感知失败"):
        session.ask("请解释图片", attachments=[image])

    assert len(client.completions.calls) == 1
    assert isinstance(client.completions.calls[0]["messages"][-1]["content"], list)


def test_text_only_turn_does_not_discover_or_call_vision_model():
    client = FakeClient([SimpleNamespace(id="vision-model")])
    session = AgentSession(
        client=client,
        model_name="text-model",
        vision_config=VisionConfig(),
    )

    assert session.ask("普通文本问题") == "文本模型已根据图片证据完成推理。"
    assert client.models.calls == 0
    assert len(client.completions.calls) == 1
    assert isinstance(client.completions.calls[0]["messages"][-1]["content"], str)
