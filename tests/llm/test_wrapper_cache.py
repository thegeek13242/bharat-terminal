"""Prompt-cache marker + client-reuse semantics (2026-08-13 latency work).

Unit-only: no network — client construction and kwargs assembly only.
Pins three behaviours the analysis hot path now relies on:

1. cache=True adds exactly one cache_control breakpoint at the end of the
   static prefix (last system block if present, else last tool), and NEVER
   mutates caller structures — stage tool schemas are module constants
   shared across calls; mutating one would leak the marker into every
   later call including cache=False ones.
2. cache=False requests are byte-identical to pre-change requests.
3. SDK clients are reused: same object per (provider) for sync callers,
   same object per event loop for async callers, never shared across
   loops (asyncio clients are loop-bound; cross-loop reuse breaks).
"""
import asyncio

import pytest

import bharat_terminal.llm.wrapper as w


TOOL = {"name": "t1", "description": "d", "input_schema": {"type": "object"}}


def test_cache_marks_last_tool_when_no_system():
    k = w._anthropic_kwargs(
        "claude-sonnet-4-6", "anthropic-bedrock", 100, None,
        [{"role": "user", "content": "x"}], [TOOL],
        {"type": "tool", "name": "t1"}, None, cache=True)
    assert k["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # The module-level constant must be untouched.
    assert "cache_control" not in TOOL


def test_cache_wraps_string_system_and_leaves_tools_alone():
    k = w._anthropic_kwargs(
        "claude-sonnet-4-6", "anthropic-bedrock", 100, "sys prompt",
        [{"role": "user", "content": "x"}], [TOOL], None, None, cache=True)
    assert k["system"] == [{"type": "text", "text": "sys prompt",
                           "cache_control": {"type": "ephemeral"}}]
    # System breakpoint already covers tools (render order tools→system).
    assert "cache_control" not in k["tools"][-1]


def test_cache_false_is_unchanged():
    k = w._anthropic_kwargs(
        "claude-sonnet-4-6", "anthropic-bedrock", 100, None,
        [{"role": "user", "content": "x"}], [TOOL], None, None, cache=False)
    assert "cache_control" not in k["tools"][-1]
    assert "system" not in k


def test_cache_marker_idempotent_on_existing_block():
    sys_blocks = [{"type": "text", "text": "s",
                   "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    k = w._anthropic_kwargs(
        "claude-sonnet-4-6", "anthropic-bedrock", 100, sys_blocks,
        [{"role": "user", "content": "x"}], None, None, None, cache=True)
    # Pre-existing marker (caller-controlled TTL) is preserved, not doubled.
    assert k["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_client_reuse_sync_and_per_loop(monkeypatch):
    monkeypatch.setattr(w, "CLAUDE_BACKEND", "anthropic")
    c1 = w._anthropic_client(False, "anthropic")
    c2 = w._anthropic_client(False, "anthropic")
    assert c1 is c2

    async def get():
        a1 = w._anthropic_client(True, "anthropic")
        a2 = w._anthropic_client(True, "anthropic")
        assert a1 is a2
        return a1

    first = asyncio.run(get())

    async def get_again():
        return w._anthropic_client(True, "anthropic")

    second = asyncio.run(get_again())
    assert first is not second, "async client must not be reused across loops"
