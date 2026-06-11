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
import asyncio
import logging
import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Gemini function calls carry no Anthropic-style tool_use id, but a multi-turn
# loop re-encodes the model turn (assistant_content) and must later correlate
# each tool_result back to its call. We synthesize a process-unique, monotonic
# id per Gemini function call so it round-trips through `messages` and lets
# `_gemini_translate` recover the function name for the function_response.
_GEMINI_CALL_SEQ = itertools.count(1)

# ── Provider selection ───────────────────────────────────────────────────────

# For Claude models: "vertex" (default) routes via Vertex AI; "anthropic" uses the
# legacy console API. Gemini models always go through Vertex.
CLAUDE_BACKEND = os.getenv("LLM_PROVIDER", "vertex").lower()

VERTEX_PROJECT = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or "bharat-terminal"
# Claude 4.x on Vertex is served through the "global" endpoint (host
# aiplatform.googleapis.com, no region prefix); the dated regional endpoints
# (us-east5, etc.) only carry older ids and 404 on claude-haiku-4-5 /
# claude-sonnet-4-6 / claude-opus-4-8. Gemini is broadly available regionally.
VERTEX_CLAUDE_REGION = os.getenv("VERTEX_CLAUDE_REGION", "global")
# Newer Gemini ids (e.g. gemini-3.5-flash, used by the signals stage) are only
# served on the "global" endpoint and 404 in us-central1/us-east5; global also
# serves the older gemini-2.5-* ids, so it is the safe universal default.
VERTEX_GEMINI_REGION = os.getenv("VERTEX_GEMINI_REGION", "global")

# Model used for Google Search grounding (web_search tool). A cheap thinking
# Gemini is fine here; the dossier model stays whatever DOSSIER_MODEL says.
GROUNDING_MODEL = os.getenv("GROUNDING_MODEL", "gemini-3.5-flash")

# Gemini emits structured function calls less token-efficiently than Claude — a
# complex tool schema can truncate to MALFORMED_FUNCTION_CALL at small budgets
# (e.g. Claude-Haiku fit extraction in 1024 tokens, flash-lite needs ~4k). Floor
# the output budget when tools are present so forced tool calls aren't truncated.
GEMINI_TOOL_MIN_OUTPUT_TOKENS = int(os.getenv("GEMINI_TOOL_MIN_OUTPUT_TOKENS", "4096"))

# Smaller Gemini models (e.g. flash-lite) intermittently emit a
# MALFORMED_FUNCTION_CALL for complex tool schemas — retry, since it's flaky not
# deterministic (flash-lite ~75% valid → ~98% with 3 attempts).
GEMINI_TOOL_MAX_ATTEMPTS = int(os.getenv("GEMINI_TOOL_MAX_ATTEMPTS", "3"))


def _is_malformed_toolcall(resp) -> bool:
    try:
        fr = resp.candidates[0].finish_reason
        return getattr(fr, "name", str(fr)) == "MALFORMED_FUNCTION_CALL"
    except (AttributeError, IndexError, TypeError):
        return False


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
    # Gemini 3 thinking models stamp each part with an opaque thought_signature
    # that MUST be echoed back when the turn is replayed in history, or the API
    # rejects it with "missing a thought_signature". Carried through so the
    # provider-agnostic multi-turn loop can replay it. Always None for Anthropic.
    signature: Optional[bytes] = None


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

def _coerce_response_dict(raw: Any) -> Dict[str, Any]:
    """Gemini's function_response wants a dict ('struct'). Anthropic tool_result
    `content` is a JSON string (see dossier_agent), so parse it; wrap scalars/
    arrays/unparseable strings under a `result` key."""
    import json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {"result": raw}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": raw}


def _gemini_translate(system, messages, tools, tool_choice, max_tokens, temperature):
    """Translate Anthropic-style args to google-genai contents/config.

    Handles multi-turn tool loops: assistant `tool_use` blocks become Gemini
    function_call parts and user `tool_result` blocks become function_response
    parts. Gemini correlates a response to a call by function *name* (not by
    Anthropic's tool_use_id), so we first map id → name from the assistant turns.
    """
    from google.genai import types

    id_to_name: Dict[str, str] = {}
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("id"):
                    id_to_name[blk["id"]] = blk.get("name", "")

    contents = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        content = msg["content"]
        parts = []
        if isinstance(content, str):
            parts.append(types.Part.from_text(text=content))
        else:
            # Anthropic content-block lists: text / tool_use / tool_result.
            for blk in content:
                if isinstance(blk, str):
                    parts.append(types.Part.from_text(text=blk))
                    continue
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    part = types.Part.from_text(text=blk.get("text", ""))
                    sig = blk.get("_thought_signature")
                    if sig is not None:
                        part.thought_signature = sig
                    parts.append(part)
                elif btype == "tool_use":
                    part = types.Part.from_function_call(
                        name=blk.get("name", ""), args=blk.get("input") or {},
                    )
                    sig = blk.get("_thought_signature")
                    if sig is not None:
                        part.thought_signature = sig
                    parts.append(part)
                elif btype == "tool_result":
                    parts.append(types.Part.from_function_response(
                        name=id_to_name.get(blk.get("tool_use_id", ""), ""),
                        response=_coerce_response_dict(blk.get("content", "")),
                    ))
        if parts:
            contents.append(types.Content(role=role, parts=parts))

    # Floor the output budget for tool calls so a complex schema isn't truncated
    # into a MALFORMED_FUNCTION_CALL (Gemini is less token-efficient than Claude).
    out_tokens = max(max_tokens, GEMINI_TOOL_MIN_OUTPUT_TOKENS) if tools else max_tokens
    cfg: Dict[str, Any] = {"max_output_tokens": out_tokens}
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
        # parts can be None (e.g. a safety-blocked or thinking-only response);
        # `for x in None` would raise TypeError, so coalesce to an empty list.
        for part in (cand.content.parts or []):
            sig = getattr(part, "thought_signature", None)
            fc = getattr(part, "function_call", None)
            if fc:
                blocks.append(ContentBlock(
                    type="tool_use", name=fc.name, input=dict(fc.args or {}),
                    id=f"gemcall_{next(_GEMINI_CALL_SEQ)}", signature=sig,
                ))
            elif getattr(part, "text", None):
                blocks.append(ContentBlock(type="text", text=part.text, signature=sig))
    except (AttributeError, IndexError, TypeError):
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


# ── Provider-agnostic helpers ────────────────────────────────────────────────

def assistant_content(resp: LLMResponse) -> List[Dict[str, Any]]:
    """Re-encode a response as Anthropic-style assistant content blocks.

    A multi-turn tool loop must append the model's turn back into `messages`
    before sending the tool results. Using `resp.raw.content` is Anthropic-only
    (a Gemini raw response has no `.content`); rebuilding from the normalized
    `resp.content` keeps the loop provider-agnostic while preserving tool_use
    ids so the following tool_result blocks line up.
    """
    blocks: List[Dict[str, Any]] = []
    for b in resp.content:
        if b.type == "text" and b.text:
            block: Dict[str, Any] = {"type": "text", "text": b.text}
        elif b.type == "tool_use":
            block = {"type": "tool_use", "id": b.id,
                     "name": b.name, "input": b.input or {}}
        else:
            continue
        # Replay the Gemini thought_signature when present (absent for Anthropic,
        # so this key never reaches the Anthropic API's strict block schema).
        if b.signature is not None:
            block["_thought_signature"] = b.signature
        blocks.append(block)
    return blocks


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
    # Retain the client in a local: an inline `_gemini_client().models...` lets
    # the Client be GC'd mid-call in google-genai 1.x, raising
    # "Cannot send a request, as the client has been closed".
    client = _gemini_client()
    attempts = GEMINI_TOOL_MAX_ATTEMPTS if tools else 1
    for attempt in range(attempts):
        resp = client.models.generate_content(model=model, contents=contents, config=cfg)
        if not (tools and _is_malformed_toolcall(resp)) or attempt == attempts - 1:
            break
        logger.info("gemini MALFORMED_FUNCTION_CALL; retry %d/%d", attempt + 1, attempts - 1)
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
    # Retain the client in a local (see note in complete()): an inline
    # `_gemini_client().aio...` lets the Client be GC'd/closed mid-call.
    client = _gemini_client()
    attempts = GEMINI_TOOL_MAX_ATTEMPTS if tools else 1
    for attempt in range(attempts):
        resp = await client.aio.models.generate_content(
            model=model, contents=contents, config=cfg)
        if not (tools and _is_malformed_toolcall(resp)) or attempt == attempts - 1:
            break
        logger.info("gemini MALFORMED_FUNCTION_CALL; retry %d/%d", attempt + 1, attempts - 1)
    return _wrap_gemini_response(resp, model)


# ── Google Search grounding ──────────────────────────────────────────────────

def _extract_sources(resp) -> List[str]:
    """Pull grounding source URLs from a Gemini grounded response."""
    urls: List[str] = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None)
            if uri and uri not in urls:
                urls.append(uri)
    except (AttributeError, IndexError, TypeError):
        pass
    return urls


def _is_rate_limited(e: Exception) -> bool:
    """True if an exception is a Vertex 429 / RESOURCE_EXHAUSTED rate limit."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code == 429:
        return True
    s = str(e).upper()
    return "RESOURCE_EXHAUSTED" in s or "429" in s


# Google-Search grounding has its own (low) per-minute Vertex quota, separate
# from token quotas. Under concurrency the dossier agent's web_search bursts can
# exceed it; back off and retry rather than failing the call.
GROUNDING_MAX_RETRIES = int(os.getenv("GROUNDING_MAX_RETRIES", "4"))


async def grounded_search(
    query: str, *, model: Optional[str] = None, max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Google-Search-grounded answer for `query` via Gemini on Vertex.

    Backs the dossier agent's `web_search` *function* tool. Google Search
    grounding cannot be mixed with function declarations in one request, so this
    is a standalone grounded call with NO tools other than `google_search`.
    Returns ``{"text": str, "sources": [url, ...]}``. On a persistent rate limit
    it degrades gracefully to an ``error`` dict so the agent proceeds without it
    rather than crashing.
    """
    from google.genai import types

    model = model or GROUNDING_MODEL
    cfg = types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=query)])]

    for attempt in range(GROUNDING_MAX_RETRIES):
        client = _gemini_client()  # retained local; see complete() note
        try:
            resp = await client.aio.models.generate_content(
                model=model, contents=contents, config=cfg)
            return {"text": _wrap_gemini_response(resp, model).text,
                    "sources": _extract_sources(resp)}
        except Exception as e:
            if not _is_rate_limited(e):
                raise
            if attempt == GROUNDING_MAX_RETRIES - 1:
                logger.warning(
                    "grounded_search rate-limited (RESOURCE_EXHAUSTED) after "
                    "%d attempts; degrading gracefully: %s",
                    GROUNDING_MAX_RETRIES, str(e)[:200])
                return {"text": "", "sources": [],
                        "error": "web_search temporarily rate-limited "
                                 "(RESOURCE_EXHAUSTED) — proceed using DB-grounded "
                                 "data; do not retry this query immediately."}
            delay = 2.0 * (2 ** attempt)  # deterministic backoff: 2s, 4s, 8s
            logger.info("grounded_search 429; backing off %.0fs (attempt %d/%d)",
                        delay, attempt + 1, GROUNDING_MAX_RETRIES)
            await asyncio.sleep(delay)
