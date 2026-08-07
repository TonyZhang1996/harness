"""OpenAI-compatible model client for AI Harness."""

from __future__ import annotations

from openai import OpenAI

from .config import ModelConfig


def create_client(config: ModelConfig | None = None) -> OpenAI:
    """Create a client for an OpenAI-compatible endpoint."""
    settings = config or ModelConfig.from_env()
    kwargs = {
        "api_key": settings.api_key,
        "timeout": settings.timeout,
    }
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    return OpenAI(**kwargs)


def get_model_name(config: ModelConfig | None = None) -> str:
    """Return the configured model name."""
    return (config or ModelConfig.from_env()).model


def ask_model(task: str) -> str:
    """Send a task and return the final text response."""
    config = ModelConfig.from_env()
    response = create_client(config).chat.completions.create(
        model=config.model,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful coding assistant.",
            },
            {"role": "user", "content": task},
        ],
    )

    return response.choices[0].message.content or ""
