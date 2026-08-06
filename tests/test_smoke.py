from ai_harness import __version__
from ai_harness.cli import build_parser


def test_package_has_version():
    assert __version__ == "0.1.0"


def test_parser_accepts_task():
    args = build_parser().parse_args(["inspect this project"])
    assert args.task == "inspect this project"
