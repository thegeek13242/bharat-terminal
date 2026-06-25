"""
Provider-agnostic LLM wrapper for Bharat Terminal.

Lets any stage switch models (Claude on Vertex, Gemini on Vertex, or the legacy
Anthropic console API) by changing a single env var, without touching the stage's
prompt-building or response-parsing code.

The wrapper's response duck-types the Anthropic Messages response — `.content` is a
list of blocks with `.type` / `.text` / `.name` / `.input`, and `.usage` exposes
`.input_tokens` / `.output_tokens` — so existing call sites that iterate
`response.content` for `tool_use` blocks keep working as-is.

Routing is by model id:
  - "claude-*"  → Anthropic (Vertex by default; console API if LLM_PROVIDER=anthropic)
  - "gemini-*"  → Gemini on Vertex
"""
from bharat_terminal.llm.wrapper import (
    LLMResponse,
    LLMUsage,
    ContentBlock,
    complete,
    acomplete,
    provider_for_model,
    assistant_content,
    grounded_search,
)

__all__ = [
    "LLMResponse",
    "LLMUsage",
    "ContentBlock",
    "complete",
    "acomplete",
    "provider_for_model",
    "assistant_content",
    "grounded_search",
]
