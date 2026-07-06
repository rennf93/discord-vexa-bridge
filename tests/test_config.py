"""Tests for summarizer.config — env -> typed Config, per-sink required-var enforcement."""

from pathlib import Path

import pytest

from summarizer import config


def _base_env():
    return {
        "SUMMARIZE_ENABLED": "true",
        "AI_MODEL": "anthropic/claude-sonnet-5",
        "AI_API_KEY": "sk-test",
        "VEXA_API_URL": "http://192.168.50.111:8056",
        "VEXA_API_KEY": "vexa-key",
        "SUMMARIZE_PLATFORMS": "discord,google_meet",
        "MIN_TRANSCRIPT_SECONDS": "30",
        "OBSIDIAN_ENABLED": "true",
        "OBSIDIAN_MCP_URL": "http://localhost:8765/mcp",
        "OBSIDIAN_MCP_TOKEN": "tok",
        "OBSIDIAN_NOTE_FOLDER": "Meetings",
        "INCLUDE_TRANSCRIPT": "true",
        "VEXA_NOTES_ENABLED": "false",
        "DRY_RUN": "false",
        "STATE_DIR": "/tmp/state",
    }


def test_load_config_parses_all_fields():
    cfg = config.load_config(_base_env())
    assert cfg.summarize_enabled is True
    assert cfg.ai_model == "anthropic/claude-sonnet-5"
    assert cfg.ai_api_key == "sk-test"
    assert cfg.vexa_api_url == "http://192.168.50.111:8056"
    assert cfg.vexa_api_key == "vexa-key"
    assert cfg.summarize_platforms == ["discord", "google_meet"]
    assert cfg.min_transcript_seconds == 30.0
    assert cfg.obsidian_enabled is True
    assert cfg.obsidian_mcp_url == "http://localhost:8765/mcp"
    assert cfg.obsidian_mcp_token == "tok"
    assert cfg.obsidian_note_folder == "Meetings"
    assert cfg.include_transcript is True
    assert cfg.vexa_notes_enabled is False
    assert cfg.dry_run is False
    assert cfg.state_dir == Path("/tmp/state")


def test_bool_parsing_accepts_common_truthy():
    env = _base_env()
    for val in ("true", "1", "yes", "TRUE", "Yes"):
        env["OBSIDIAN_ENABLED"] = val
        assert config.load_config(env).obsidian_enabled is True, val
    for val in ("false", "0", "no", "", "anything-else"):
        env["OBSIDIAN_ENABLED"] = val
        assert config.load_config(env).obsidian_enabled is False, val


def test_summarize_disabled_short_circuits_required_vars():
    """When the master switch is off, missing Vexa/Obsidian vars must NOT raise."""
    env = {"SUMMARIZE_ENABLED": "false"}
    cfg = config.load_config(env)
    assert cfg.summarize_enabled is False


def test_missing_vexa_url_raises():
    env = _base_env()
    del env["VEXA_API_URL"]
    with pytest.raises(config.ConfigError, match="VEXA_API_URL"):
        config.load_config(env)


def test_missing_vexa_key_raises():
    env = _base_env()
    del env["VEXA_API_KEY"]
    with pytest.raises(config.ConfigError, match="VEXA_API_KEY"):
        config.load_config(env)


def test_obsidian_enabled_requires_token():
    env = _base_env()
    del env["OBSIDIAN_MCP_TOKEN"]
    with pytest.raises(config.ConfigError, match="OBSIDIAN_MCP_TOKEN"):
        config.load_config(env)


def test_obsidian_disabled_does_not_require_token():
    env = _base_env()
    env["OBSIDIAN_ENABLED"] = "false"
    del env["OBSIDIAN_MCP_TOKEN"]
    cfg = config.load_config(env)  # must not raise
    assert cfg.obsidian_enabled is False


def test_defaults_when_optional_vars_missing():
    env = _base_env()
    del env["AI_MODEL"]
    del env["SUMMARIZE_PLATFORMS"]
    del env["MIN_TRANSCRIPT_SECONDS"]
    del env["OBSIDIAN_MCP_URL"]
    del env["OBSIDIAN_NOTE_FOLDER"]
    del env["INCLUDE_TRANSCRIPT"]
    del env["VEXA_NOTES_ENABLED"]
    del env["DRY_RUN"]
    del env["STATE_DIR"]
    cfg = config.load_config(env)
    assert cfg.ai_model == "anthropic/claude-sonnet-5"
    assert cfg.summarize_platforms == ["discord"]
    assert cfg.min_transcript_seconds == 30.0
    assert cfg.obsidian_mcp_url == "http://localhost:8765/mcp"
    assert cfg.obsidian_note_folder == "Meetings"
    assert cfg.include_transcript is True
    assert cfg.vexa_notes_enabled is False
    assert cfg.dry_run is False
    assert cfg.state_dir == Path.home() / ".local" / "share" / "vexa-summarizer"
