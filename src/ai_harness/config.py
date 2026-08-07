"""Environment-backed configuration for AI Harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: str | Path, *, override: bool = False) -> None:
    """Load simple KEY=VALUE entries without executing the file as shell code."""
    env_path = Path(path).expanduser().resolve()
    if not env_path.is_file():
        return
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{env_path}:{line_number} 不是有效的 KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"{env_path}:{line_number} 包含无效环境变量名")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for an OpenAI-compatible chat-completions endpoint."""

    api_key: str
    model: str
    base_url: str | None
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "ModelConfig":
        env_file = os.getenv("AI_HARNESS_ENV_FILE")
        if env_file:
            load_env_file(env_file)
        api_key = (
            os.getenv("AI_HARNESS_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not api_key:
            raise RuntimeError(
                "请设置 AI_HARNESS_API_KEY、DEEPSEEK_API_KEY 或 OPENAI_API_KEY"
            )

        base_url = os.getenv("AI_HARNESS_BASE_URL")
        if base_url is None and os.getenv("DEEPSEEK_API_KEY"):
            base_url = "https://api.deepseek.com"

        model = os.getenv("AI_HARNESS_MODEL")
        if not model:
            model = "deepseek-chat" if base_url == "https://api.deepseek.com" else ""
        if not model:
            raise RuntimeError("请设置 AI_HARNESS_MODEL")

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=_positive_float("AI_HARNESS_TIMEOUT", 60.0),
        )
