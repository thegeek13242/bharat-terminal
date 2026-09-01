"""llm.wrapper.anthropic_beta_for — env-driven per-model beta header.

2026-08-22: the guidance sweep moved to Sonnet 4.5's 1M-context profile,
which Bedrock meters under a separate tokens-per-day quota. The opt-in is
an env var scoped to the job container so the live pipeline never sends
the header by accident.
"""
from bharat_terminal.llm.wrapper import _anthropic_kwargs, anthropic_beta_for

MSGS = [{"role": "user", "content": "x"}]
ENV = "BEDROCK_ANTHROPIC_BETA_CLAUDE_SONNET_4_5_20250929"


def test_no_env_no_header(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert anthropic_beta_for("claude-sonnet-4-5-20250929", "anthropic-bedrock") is None
    kw = _anthropic_kwargs("claude-sonnet-4-5-20250929", "anthropic-bedrock", 5,
                           None, MSGS, None, None, None)
    assert "extra_headers" not in kw


def test_env_sets_header_for_bedrock_only(monkeypatch):
    monkeypatch.setenv(ENV, "context-1m-2025-08-07")
    assert anthropic_beta_for("claude-sonnet-4-5-20250929", "anthropic-bedrock") \
        == "context-1m-2025-08-07"
    assert anthropic_beta_for("claude-sonnet-4-5-20250929", "anthropic") is None
    kw = _anthropic_kwargs("claude-sonnet-4-5-20250929", "anthropic-bedrock", 5,
                           None, MSGS, None, None, None)
    assert kw["extra_headers"] == {"anthropic-beta": "context-1m-2025-08-07"}


def test_other_models_unaffected(monkeypatch):
    monkeypatch.setenv(ENV, "context-1m-2025-08-07")
    assert anthropic_beta_for("claude-haiku-4-5-20251001", "anthropic-bedrock") is None
