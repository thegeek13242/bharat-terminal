"""Gemini thought-signature plumbing.

Regression for the 2026-08-03 incident: kb-monitor's nightly dossier refresh
failed 100/100 with 400 "Function call is missing a thought_signature".
Root cause was NOT the wrapper logic — it was requirements/kb.txt pinning
pydantic==2.6.3, which forced pip to resolve google-genai 1.2.0, a version
with no Part.thought_signature field at all. Extraction silently yielded
None and replay silently dropped it, so every multi-turn tool loop run from
that image died on turn 2 while identical code worked elsewhere.

These tests pin both halves: the replay contract (signatures survive a
round-trip through assistant_content) and the capability guard that makes a
stale SDK loud instead of silent.
"""
from __future__ import annotations

import pytest

from bharat_terminal import llm
from bharat_terminal.llm import wrapper


def _resp(blocks):
    return wrapper.LLMResponse(content=blocks, usage=wrapper.LLMUsage(),
                               model="gemini-3.1-pro-preview",
                               provider="gemini-vertex", stop_reason="tool_use")


def test_signatures_survive_assistant_content_roundtrip():
    """Every block carrying a signature must still carry it after replay —
    Gemini rejects the NEXT turn otherwise."""
    blocks = [
        wrapper.ContentBlock(type="tool_use", name="get_kb_company_context",
                             input={"symbol": "ABB"}, id="gemcall_1",
                             signature=b"sig-a"),
        wrapper.ContentBlock(type="tool_use", name="get_financials",
                             input={"symbol": "ABB"}, id="gemcall_2",
                             signature=b"sig-b"),
        wrapper.ContentBlock(type="text", text="thinking", signature=b"sig-c"),
    ]
    out = llm.assistant_content(_resp(blocks))
    assert len(out) == 3
    assert [b["_thought_signature"] for b in out] == [b"sig-a", b"sig-b", b"sig-c"]
    # Ordering must be preserved: Gemini reports the offending part by POSITION.
    assert [b.get("name") for b in out][:2] == [
        "get_kb_company_context", "get_financials"]


def test_signature_only_block_is_not_dropped():
    """A hidden-thought part has no text but DOES carry a signature; dropping
    it is what produces 'missing a thought_signature' on the next turn."""
    out = llm.assistant_content(_resp([
        wrapper.ContentBlock(type="text", text="", signature=b"hidden"),
    ]))
    assert len(out) == 1 and out[0]["_thought_signature"] == b"hidden"


def test_anthropic_blocks_carry_no_signature_key():
    """Claude has no signatures; the key must never reach Anthropic's strict
    block schema."""
    out = llm.assistant_content(_resp([
        wrapper.ContentBlock(type="tool_use", name="t", input={}, id="x"),
    ]))
    assert "_thought_signature" not in out[0]


def test_capability_probe_reflects_installed_sdk(monkeypatch):
    wrapper._SIG_SUPPORT = None
    supported = wrapper._sdk_supports_thought_signatures()
    assert isinstance(supported, bool)
    # The test image must ship an SDK that can carry signatures — if this
    # fails, a requirements floor regressed (that IS the bug being pinned).
    assert supported, (
        "installed google-genai has no Part.thought_signature — Gemini 3 "
        "tool loops will fail on turn 2; check the google-genai>=1.21 floor "
        "in requirements/*.txt"
    )


def test_stale_sdk_warns_loudly_once(monkeypatch, caplog):
    """A stale SDK must produce an ERROR naming the fix, exactly once."""
    monkeypatch.setattr(wrapper, "_SIG_SUPPORT", False)
    monkeypatch.setattr(wrapper, "_SIG_WARNED", False)
    with caplog.at_level("ERROR"):
        wrapper._warn_if_signatures_unsupported("gemini-3.1-pro-preview", True)
        wrapper._warn_if_signatures_unsupported("gemini-3.1-pro-preview", True)
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errs) == 1, "must warn once per process, not per call"
    assert "thought_signature" in errs[0].getMessage()
    assert "1.21" in errs[0].getMessage(), "must name the remedy"


def test_no_warning_without_tools(monkeypatch, caplog):
    """Single-shot calls with no tools are unaffected — don't cry wolf."""
    monkeypatch.setattr(wrapper, "_SIG_SUPPORT", False)
    monkeypatch.setattr(wrapper, "_SIG_WARNED", False)
    with caplog.at_level("ERROR"):
        wrapper._warn_if_signatures_unsupported("gemini-3.1-pro-preview", False)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
