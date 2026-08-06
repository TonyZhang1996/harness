"""DeepSeek model client for AI Harness."""

from __future__ import annotations

import os

from openai import OpenAI


def create_client() -> OpenAI:
    """Create a DeepSeek client using the current environment settings."""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置 DEEPSEEK_API_KEY")

    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )


def get_model_name() -> str:
    """Return the configured DeepSeek model name."""
    return os.getenv("AI_HARNESS_MODEL", "deepseek-v4-flash")


def ask_model(task: str) -> str:
    """Send a task to DeepSeek and return the final text response."""
    response = create_client().chat.completions.create(
        model=get_model_name(),
        messages=[
            {
                "role": "system",
                "content": "You are a helpful coding assistant.",
            },
            {"role": "user", "content": task},
        ],
    )

    return response.choices[0].message.content or ""
