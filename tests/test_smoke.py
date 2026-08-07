from ai_harness import __version__
from ai_harness.cli import build_parser


def test_package_has_version():
    assert __version__ == "0.2.0"


def test_parser_accepts_task():
    args = build_parser().parse_args(["inspect this project"])
    assert args.task == "inspect this project"


def test_parser_accepts_full_access():
    args = build_parser().parse_args(["--full-access"])
    assert args.full_access is True
