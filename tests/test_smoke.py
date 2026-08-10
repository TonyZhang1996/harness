from ai_harness import __version__
from ai_harness.cli import build_parser
from ai_harness.gui import HarnessGUI, launch_gui
import inspect


def test_package_has_version():
    assert __version__ == "0.4.0"


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


def test_project_order_can_be_changed_without_losing_records():
    gui = HarnessGUI.__new__(HarnessGUI)
    gui.projects = [
        {"name": "one", "path": "C:/one"},
        {"name": "two", "path": "C:/two"},
        {"name": "three", "path": "C:/three"},
    ]

    assert gui._move_project("C:/one", "C:/three") is True
    assert [item["name"] for item in gui.projects] == ["two", "three", "one"]
