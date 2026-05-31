"""
Core LLM wrapper: normalized request → provider → Anthropic-shaped response.

Providers:
  - anthropic-vertex : Claude models via Vertex AI (AnthropicVertex), GCP ADC auth.
  - anthropic        : Claude models via the legacy console API (api_key).
  - gemini-vertex    : Gemini models via Vertex AI (google-genai).

Design goals:
  - Drop-in for `client.messages.create(...)`: same kwargs (model, max_tokens,
    system, messages, tools, tool_choice, temperature) and an Anthropic-shaped
    response object, so call sites need only swap the construct+create lines.
  - Per-stage model selection stays env-driven (e.g. SIGNALS_MODEL=gemini-3.5-flash,
    DOSSIER_MODEL=claude-opus-4-8); the wrapper picks the provider from the model id.
"""
import os
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Provider selection ───────────────────────────────────────────────────────

# For Claude models: "vertex" (default) routes via Vertex AI; "anthropic" uses the
# legacy console API. Gemini models always go through Vertex.
CLAUDE_BACKEND = os.getenv("LLM_PROVIDER", "vertex").lower()

VERTEX_PROJECT = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or "bharat-terminal"
# Claude on Vertex is region-bound (not in asia-south1); Gemini is broadly available.
VERTEX_CLAUDE_REGION = os.getenv("VERTEX_CLAUDE_REGION", "us-east5")
VERTEX_GEMINI_REGION = os.getenv("VERTEX_GEMINI_REGION", "us-central1")


def provider_for_model(model: str) -> str:
    """Return the provider key for a model id."""
    m = (model or "").lower()
    if m.startswith("gemini"):
        return "gemini-vertex"
    if m.startswith("claude"):
        return "anthropic" if CLAUDE_BACKEND == "anthropic" else "anthropic-vertex"
    raise ValueError(f"Unrecognized model id (no provider mapping): {model!r}")


def to_vertex_claude_id(model: str) -> str:
    """
    Map a console Claude id to its Vertex publisher id.
    'claude-haiku-4-5-20251001' -> 'claude-haiku-4-5@20251001'
    'claude-sonnet-4-6'         -> 'claude-sonnet-4-6'  (passthrough; set @version via env if needed)
    An explicit override may be supplied via VERTEX_MODEL_<UPPER_SNAKE> env.
    """
    override = os.getenv("VERTEX_MODEL_" + re.sub(r"[^A-Za-z0-9]", "_", model).upper())
    if override:
        return override
    m = re.match(r"^(claude-[a-z]+-\d+-\d+)-(\d{8})$", model)
    if m:
        return f"{m.group(1)}@{m.group(2)}"
    return model


# ── Normalized response (Anthropic-shaped) ───────────────────────────────────

@dataclass
class ContentBlock:
    type: str                       # "text" | "tool_use"
    text: Optional[str] = None      # for type == "text"
    name: Optional[str] = None      # for type == "tool_use"
    input: Optional[Dict[str, Any]] = None  # for type == "tool_use"
    id: Optional[str] = None


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    content: List[ContentBlock] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    stop_reason: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    raw: Any = None

    @property
    def text(self) -> str:
        """Concatenated text blocks (convenience for plain-text calls)."""
        return "".join(b.text or "" for b in self.content if b.type == "text")

    def first_tool_use(self, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the input dict of the first tool_use block (optionally by name)."""
        for b in self.content:
            if b.type == "tool_use" and (name is None or b.name == name):
                return b.input
        return None


# ── Anthropic providers (Vertex + console) ───────────────────────────────────

def _anthropic_client(async_: bool, vertex: bool):
    import anthropic
    if vertex:
        cls = anthropic.AsyncAnthropicVertex if async_ else anthropic.AnthropicVertex
        return cls(project_id=VERTEX_PROJECT, region=VERTEX_CLAUDE_REGION)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    cls = anthropic.AsyncAnthropic if async_ else anthropic.Anthropic
    return cls(api_key=api_key)


def _anthropic_kwargs(model: str, vertex: bool, max_tokens: int, system, messages,
                      tools, tool_choice, temperature) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "model": to_vertex_claude_id(model) if vertex else model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


def _wrap_anthropic_response(resp, model: str, provider: str) -> LLMResponse:
    blocks: List[ContentBlock] = []
    for b in resp.content:
        btype = getattr(b, "type", None)
        if btype == "text":
            blocks.append(ContentBlock(type="text", text=getattr(b, "text", "")))
        elif btype == "tool_use":
            blocks.append(ContentBlock(
                type="tool_use", name=getattr(b, "name", None),
                input=getattr(b, "input", None), id=getattr(b, "id", None),
            ))
    usage = LLMUsage(
        input_tokens=getattr(resp.usage, "input_tokens", 0),
        output_tokens=getattr(resp.usage, "output_tokens", 0),
    )
    return LLMResponse(
        content=blocks, usage=usage,
        stop_reason=getattr(resp, "stop_reason", None),
        model=model, provider=provider, raw=resp,
    )


# ── Gemini provider (Vertex via google-genai) ────────────────────────────────

def _gemini_translate(system, messages, tools, tool_choice, max_tokens, temperature):
    """Translate Anthropic-style args to google-genai contents/config."""
    from google.genai import types

    contents = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        content = msg["content"]
        if isinstance(content, str):
            parts = [types.Part.from_text(text=content)]
        else:
            # Anthropic content-block lists: keep text blocks; tool_result → text.
            parts = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(types.Part.from_text(text=blk.get("text", "")))
                elif isinstance(blk, str):
                    parts.append(types.Part.from_text(text=blk))
        contents.append(types.Content(role=role, parts=parts))

    cfg: Dict[str, Any] = {"max_output_tokens": max_tokens}
    if temperature is not None:
        cfg["temperature"] = temperature
    if system is not None:
        cfg["system_instruction"] = system

    if tools:
        # Anthropic tool {name, description, input_schema} → Gemini FunctionDeclaration.
        decls = [
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("input_schema"),
            )
            for t in tools
        ]
        cfg["tools"] = [types.Tool(function_declarations=decls)]
        # Forced tool choice → FUNCTION_CALLING mode ANY (optionally restricted).
        if tool_choice and isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
            cfg["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY", allowed_function_names=[tool_choice["name"]],
                )
            )
    return contents, types.GenerateContentConfig(**cfg)


def _wrap_gemini_response(resp, model: str) -> LLMResponse:
    blocks: List[ContentBlock] = []
    try:
        cand = resp.candidates[0]
        for part in cand.content.parts:
            fc = getattr(part, "function_call", None)
            if fc:
                blocks.append(ContentBlock(
                    type="tool_use", name=fc.name, input=dict(fc.args or {}),
                ))
            elif getattr(part, "text", None):
                blocks.append(ContentBlock(type="text", text=part.text))
    except (AttributeError, IndexError):
        pass
    um = getattr(resp, "usage_metadata", None)
    usage = LLMUsage(
        input_tokens=getattr(um, "prompt_token_count", 0) or 0,
        output_tokens=getattr(um, "candidates_token_count", 0) or 0,
    )
    return LLMResponse(
        content=blocks, usage=usage, model=model,
        provider="gemini-vertex", raw=resp,
    )


def _gemini_client():
    from google import genai
    return genai.Client(
        vertexai=True, project=VERTEX_PROJECT, location=VERTEX_GEMINI_REGION,
    )


# ── Public entrypoints ───────────────────────────────────────────────────────

def complete(
    *, model: str, messages: List[Dict[str, Any]], max_tokens: int = 1024,
    system: Optional[Any] = None, tools: Optional[List[Dict]] = None,
    tool_choice: Optional[Dict] = None, temperature: Optional[float] = None,
) -> LLMResponse:
    """Synchronous completion. Routes to the provider for `model`."""
    provider = provider_for_model(model)
    if provider in ("anthropic-vertex", "anthropic"):
        vertex = provider == "anthropic-vertex"
        client = _anthropic_client(async_=False, vertex=vertex)
        kwargs = _anthropic_kwargs(model, vertex, max_tokens, system, messages,
                                   tools, tool_choice, temperature)
        return _wrap_anthropic_response(client.messages.create(**kwargs), model, provider)
    # gemini-vertex
    contents, cfg = _gemini_translate(system, messages, tools, tool_choice,
                                      max_tokens, temperature)
    resp = _gemini_client().models.generate_content(model=model, contents=contents, config=cfg)
    return _wrap_gemini_response(resp, model)


async def acomplete(
    *, model: str, messages: List[Dict[str, Any]], max_tokens: int = 1024,
    system: Optional[Any] = None, tools: Optional[List[Dict]] = None,
    tool_choice: Optional[Dict] = None, temperature: Optional[float] = None,
) -> LLMResponse:
    """Async completion. Routes to the provider for `model`."""
    provider = provider_for_model(model)
    if provider in ("anthropic-vertex", "anthropic"):
        vertex = provider == "anthropic-vertex"
        client = _anthropic_client(async_=True, vertex=vertex)
        kwargs = _anthropic_kwargs(model, vertex, max_tokens, system, messages,
                                   tools, tool_choice, temperature)
        return _wrap_anthropic_response(await client.messages.create(**kwargs), model, provider)
    # gemini-vertex
    contents, cfg = _gemini_translate(system, messages, tools, tool_choice,
                                      max_tokens, temperature)
    resp = await _gemini_client().aio.models.generate_content(
        model=model, contents=contents, config=cfg)
    return _wrap_gemini_response(resp, model)
