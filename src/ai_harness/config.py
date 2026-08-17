"""Environment-backed configuration for AI Harness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_DEFAULT_MODEL = "deepseek-v4-flash"
OPENCODE_GO_PROVIDER_ALIASES = frozenset({"go", "opencode-go", "opencode_go"})

# These Go models expose the OpenAI-compatible Chat Completions protocol and
# therefore work with the current tool-calling client without another adapter.
OPENCODE_GO_CHAT_MODELS = (
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "hy3",
)


def is_opencode_go_provider(provider: str | None) -> bool:
    """Return whether a configured provider name refers to OpenCode Go."""
    return (provider or "").strip().lower() in OPENCODE_GO_PROVIDER_ALIASES


def find_env_file() -> Path | None:
    """Locate configuration consistently across editable and global installs."""
    explicit = os.getenv("AI_HARNESS_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser().resolve()

    candidates = [Path.cwd() / ".env", Path.home() / ".ai-harness" / ".env"]
    project_root = Path(__file__).resolve().parents[2]
    if (project_root / "pyproject.toml").is_file():
        candidates.append(project_root / ".env")
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


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
        env_file = find_env_file()
        if env_file:
            load_env_file(env_file)

        provider = os.getenv("AI_HARNESS_PROVIDER")
        go_requested = is_opencode_go_provider(provider)
        if not provider and os.getenv("OPENCODE_GO_API_KEY"):
            go_requested = True

        if go_requested:
            api_key = os.getenv("OPENCODE_GO_API_KEY") or os.getenv("AI_HARNESS_API_KEY")
        else:
            api_key = (
                os.getenv("AI_HARNESS_API_KEY")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
        if not api_key:
            raise RuntimeError(
                "请设置 AI_HARNESS_API_KEY、OPENCODE_GO_API_KEY、DEEPSEEK_API_KEY 或 OPENAI_API_KEY"
            )

        base_url = os.getenv("AI_HARNESS_BASE_URL")
        if base_url is None and go_requested:
            base_url = OPENCODE_GO_BASE_URL
        elif base_url is None and os.getenv("DEEPSEEK_API_KEY"):
            base_url = "https://api.deepseek.com"

        model = os.getenv("AI_HARNESS_MODEL")
        if not model:
            if go_requested:
                model = OPENCODE_GO_DEFAULT_MODEL
            elif base_url == "https://api.deepseek.com":
                model = "deepseek-v4-flash"
            else:
                model = ""
        if not model:
            raise RuntimeError("请设置 AI_HARNESS_MODEL")

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=_positive_float("AI_HARNESS_TIMEOUT", 60.0),
        )
