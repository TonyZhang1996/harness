from ai_harness import __version__
from ai_harness.cli import build_parser
from ai_harness.gui import (
    ATTACHMENT_PREVIEW_SIZE,
    CHAT_IMAGE_MAX_SIZE,
    HarnessGUI,
    _make_attachment_preview,
    _load_attachment_image,
    _model_catalog_url,
    _parse_model_catalog,
    _compact_process_preview,
    launch_gui,
)
import inspect


def test_package_has_version():
    assert __version__ == "0.6.0"


def test_parser_accepts_task():
    args = build_parser().parse_args(["inspect this project"])
    assert args.task == "inspect this project"


def test_parser_accepts_full_access():
    args = build_parser().parse_args(["--full-access"])
    assert args.full_access is True


def test_parser_accepts_explicit_env_file():
    args = build_parser().parse_args(["--env-file", "config.env"])
    assert args.env_file == "config.env"


def test_parser_accepts_gui_mode():
    args = build_parser().parse_args(["--gui", "--workspace", "project"])
    assert args.gui is True
    assert args.workspace == "project"
    assert args.approval is None


def test_gui_defaults_to_auto_review():
    gui_default = inspect.signature(HarnessGUI.__init__).parameters["approval_mode"].default
    launch_default = inspect.signature(launch_gui).parameters["approval_mode"].default

    assert gui_default == "auto"
    assert launch_default == "auto"


def test_approval_dialog_is_promoted_to_foreground_on_windows(monkeypatch):
    import ai_harness.gui as gui_module

    monkeypatch.setattr(gui_module.platform, "system", lambda: "Windows")

    class FakeDialog:
        def __init__(self):
            self.calls = []

        def update_idletasks(self):
            self.calls.append(("update_idletasks",))

        def deiconify(self):
            self.calls.append(("deiconify",))

        def attributes(self, *args):
            self.calls.append(("attributes", *args))

        def lift(self):
            self.calls.append(("lift",))

        def focus_force(self):
            self.calls.append(("focus_force",))

        def grab_set(self):
            self.calls.append(("grab_set",))

        def after(self, *args):
            self.calls.append(("after", *args))

    gui = HarnessGUI.__new__(HarnessGUI)
    dialog = FakeDialog()
    gui._focus_approval_dialog(dialog)

    names = [call[0] for call in dialog.calls]
    assert "deiconify" in names
    assert "lift" in names
    assert "focus_force" in names
    assert "grab_set" in names
    assert ("attributes", "-topmost", True) in dialog.calls


def test_model_catalog_url_uses_provider_base_url():
    assert _model_catalog_url("https://opencode.ai/zen/go/v1") == (
        "https://opencode.ai/zen/go/v1/models"
    )
    assert _model_catalog_url(
        "https://opencode.ai/zen/go/v1/chat/completions"
    ) == "https://opencode.ai/zen/go/v1/models"


def test_model_catalog_parser_extracts_unique_ids():
    assert _parse_model_catalog(
        {
            "data": [
                {"id": "kimi-k3"},
                {"id": "deepseek-v4-flash"},
                {"id": "kimi-k3"},
            ]
        }
    ) == ["kimi-k3", "deepseek-v4-flash"]


def test_gui_attachment_preview_is_fixed_size(tmp_path):
    from PIL import Image

    image_path = tmp_path / "clipboard-example.png"
    Image.new("RGB", (240, 120), color="#e66b6b").save(image_path)

    preview = _make_attachment_preview(image_path)

    assert preview is not None
    assert preview.size == ATTACHMENT_PREVIEW_SIZE
    assert preview.getpixel((48, 36)) == (230, 107, 107)


def test_gui_attachment_preview_rejects_non_image_file(tmp_path):
    path = tmp_path / "not-an-image.png"
    path.write_text("not an image", encoding="utf-8")

    assert _make_attachment_preview(path) is None


def test_gui_chat_image_preview_preserves_original_aspect_ratio(tmp_path):
    from PIL import Image

    image_path = tmp_path / "chat-image.png"
    Image.new("RGB", (960, 480), color="#6b9ee6").save(image_path)

    image = _load_attachment_image(image_path, CHAT_IMAGE_MAX_SIZE)

    assert image is not None
    assert image.size == (480, 240)


def test_gui_user_prompt_prefers_raw_text_and_supports_old_history():
    assert HarnessGUI._prompt_text_from_item(
        {
            "prompt": "请搜索图片并展示结果",
            "body": "请搜索图片并展示结果\n\n附件：reference.png",
        }
    ) == "请搜索图片并展示结果"
    assert HarnessGUI._prompt_text_from_item(
        {"body": "旧提示词\n\n附件：reference.png"}
    ) == "旧提示词"


def test_gui_prompt_time_format_is_compact_and_tolerates_invalid_values():
    assert HarnessGUI._format_prompt_time("2026-08-18T13:14:00") == "13:14"
    assert HarnessGUI._format_prompt_time("not-a-timestamp") == ""
    assert HarnessGUI._format_prompt_time(None) == ""


def test_gui_process_rows_use_one_line_previews_and_keep_only_public_kinds():
    assert _compact_process_preview("命令：\nGet-ChildItem\n结果：\nok") == "命令："
    assert _compact_process_preview("x" * 150).endswith("…")
    assert HarnessGUI._process_title_and_body(
        'browser_search {"query": "刘德华 图片 高清", "engine": "baidu"}'
    ) == ("Search", "查询：刘德华 图片 高清")
    assert HarnessGUI._process_title_and_body(
        'run_command {"command": "Get-ChildItem", "cwd": "."}'
    ) == ("Pwsh", "命令：\nGet-ChildItem")
    assert HarnessGUI._process_title_and_body('read_file {"path": "README.md"}') is None
    assert HarnessGUI._should_render_history_item({"role": "tool", "title": "Think"})
    assert HarnessGUI._should_render_history_item({"role": "tool", "title": "Search"})
    assert HarnessGUI._should_render_history_item({"role": "tool", "title": "Pwsh"})
    assert not HarnessGUI._should_render_history_item(
        {"role": "tool", "title": "工具结果"}
    )


def test_project_order_can_be_changed_without_losing_records():
    gui = HarnessGUI.__new__(HarnessGUI)
    gui.projects = [
        {"name": "one", "path": "C:/one"},
        {"name": "two", "path": "C:/two"},
        {"name": "three", "path": "C:/three"},
    ]

    assert gui._move_project("C:/one", "C:/three") is True
    assert [item["name"] for item in gui.projects] == ["two", "three", "one"]
