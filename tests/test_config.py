import pytest

from ai_harness.config import ModelConfig, find_env_file, load_env_file


@pytest.fixture(autouse=True)
def disable_real_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_HARNESS_ENV_FILE", str(tmp_path / "missing.env"))


def test_deepseek_environment_defaults(monkeypatch):
    monkeypatch.delenv("AI_HARNESS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_HARNESS_BASE_URL", raising=False)
    monkeypatch.delenv("AI_HARNESS_MODEL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    config = ModelConfig.from_env()

    assert config.model == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"


def test_generic_endpoint_requires_model(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_HARNESS_MODEL", raising=False)
    monkeypatch.setenv("AI_HARNESS_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="AI_HARNESS_MODEL"):
        ModelConfig.from_env()


def test_env_file_is_parsed_without_shell_execution(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DEEPSEEK_API_KEY="file-key"\nAI_HARNESS_MODEL=custom-model\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AI_HARNESS_MODEL", raising=False)

    load_env_file(env_file)

    assert ModelConfig.from_env().api_key == "file-key"
    assert ModelConfig.from_env().model == "custom-model"


def test_env_file_discovery_prefers_explicit_path(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("AI_HARNESS_API_KEY=value", encoding="utf-8")
    monkeypatch.setenv("AI_HARNESS_ENV_FILE", str(env_file))

    assert find_env_file() == env_file.resolve()


def test_env_file_discovery_uses_current_workspace(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("AI_HARNESS_API_KEY=value", encoding="utf-8")
    monkeypatch.delenv("AI_HARNESS_ENV_FILE")
    monkeypatch.chdir(tmp_path)

    assert find_env_file() == env_file.resolve()
