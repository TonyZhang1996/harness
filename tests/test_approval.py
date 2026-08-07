from pathlib import Path

from ai_harness.approval import CommandApprover


def test_command_approval_modes():
    assert CommandApprover("auto")("pytest", Path(".")) is True
    assert CommandApprover("never")("pytest", Path(".")) is False
    assert CommandApprover("ask", prompt=lambda _message: "y")("pytest", Path(".")) is True
    assert CommandApprover("ask", prompt=lambda _message: "n")("pytest", Path(".")) is False
