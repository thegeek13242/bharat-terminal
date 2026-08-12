"""
Core LLM wrapper: normalized request → provider → Anthropic-shaped response.

Providers:
  - anthropic-vertex : Claude models via Vertex AI (AnthropicVertex), GCP ADC auth.
  - anthropic-bedrock: Claude models via Amazon Bedrock (AnthropicBedrock /
                       AnthropicBedrockMantle), AWS IAM auth (instance role).
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
import time
import asyncio
import logging
import itertools
import threading
import collections
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

# For Claude models: "vertex" (default) routes via Vertex AI; "bedrock" via
# Amazon Bedrock (AWS IAM auth, no key); "anthropic" uses the legacy console
# API. Gemini models always go through Vertex.
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

# Backend for the agents' web_search function tool (grounded_search below):
#   gemini (default): Gemini google_search grounding on Vertex — needs GCP auth.
#   nova           : Amazon Nova web grounding on Bedrock — AWS-native billing,
#                    single call, citations from Amazon's own web index (data
#                    never leaves AWS infra per the Nova docs). US-CRIS only,
#                    so the call runs cross-region (default us-east-1); needs
#                    bedrock:InvokeTool on system-tool/amazon.nova_grounding.
GROUNDING_BACKEND = os.getenv("GROUNDING_BACKEND", "gemini").lower()
# Model used for Google Search grounding (web_search tool). A cheap thinking
# Gemini is fine here; the dossier model stays whatever DOSSIER_MODEL says.
GROUNDING_MODEL = os.getenv("GROUNDING_MODEL", "gemini-3.5-flash")
NOVA_GROUNDING_MODEL = os.getenv("NOVA_GROUNDING_MODEL", "us.amazon.nova-2-lite-v1:0")
NOVA_GROUNDING_REGION = os.getenv("NOVA_GROUNDING_REGION", "us-east-1")

# ── Amazon Bedrock (Claude via AWS) ──────────────────────────────────────────
# Two Bedrock endpoints serve Claude; both auth via the AWS credential chain
# (EC2 instance role in prod — no API key):
#   runtime (default): bedrock-runtime via AnthropicBedrock. From ap-south-1 the
#     Claude 4.6+/5 models are served ONLY through `global.*` cross-region
#     inference profiles (verified against the live catalog 2026-08-12); the
#     apac.* profile family stops at legacy Sonnet 4.
#   mantle : the Anthropic Messages API endpoint (AnthropicBedrockMantle,
#     bedrock-mantle.<region>.api.aws). Separate quota pool (no RPM caps,
#     cached reads free against input TPM) but needs bedrock-mantle:* IAM.
BEDROCK_REGION = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "ap-south-1"
BEDROCK_CLAUDE_ENDPOINT = os.getenv("BEDROCK_CLAUDE_ENDPOINT", "runtime").lower()
# Inference-profile prefix for the runtime endpoint. "global" is the only
# family serving Claude 4.6+/5 from ap-south-1; "" would use in-region ids.
BEDROCK_CLAUDE_PROFILE = os.getenv("BEDROCK_CLAUDE_PROFILE", "global")
# Mantle has its own region catalog: Claude 4.5+ is served from us-east-1 but
# NOT from ap-south-1 (404s, verified 2026-08-12) — so the mantle client gets
# its own region default instead of inheriting BEDROCK_REGION.
BEDROCK_MANTLE_REGION = os.getenv("BEDROCK_MANTLE_REGION", "us-east-1")

# ── Client-side RPM pacing for Bedrock (per-model sliding window) ────────────
# The account's applied Bedrock quotas are clamped to 10 requests/min per model
# (AWS default is 10,000; increase requested 2026-08-12, case open). Unpaced,
# market-hours bursts terminally 429 ~40% of Haiku calls even after the SDK's
# retries. The pacer makes calls QUEUE instead of FAIL: each process holds a
# per-model sliding one-minute window and delays a call until a slot frees.
# Accuracy-over-latency is standing policy — a late extraction beats a blank one.
#
# BEDROCK_RPM_LIMIT            per-process, per-model budget (0 = pacing off).
# BEDROCK_RPM_LIMIT_<SNAKE>    per-model override, e.g.
#                              BEDROCK_RPM_LIMIT_CLAUDE_SONNET_4_6=8.
# Budgets are set PER SERVICE in docker-compose so the fleet sums under the
# account quota. Retries the SDK issues inside one create() call bypass the
# pacer, so keep the fleet sum a notch below the quota. Remove/raise the
# budgets when the quota increase lands.
BEDROCK_RPM_LIMIT = int(os.getenv("BEDROCK_RPM_LIMIT", "0"))
# If a slot hasn't freed after this long, proceed anyway and let the SDK's
# retry ladder fight for it — pacing must never deadlock the pipeline.
BEDROCK_PACING_MAX_WAIT_S = float(os.getenv("BEDROCK_PACING_MAX_WAIT_S", "240"))

_pacer_lock = threading.Lock()
_pacer_windows: Dict[str, "collections.deque[float]"] = {}


def _bedrock_rpm_limit(model: str) -> int:
    override = os.getenv("BEDROCK_RPM_LIMIT_" + re.sub(r"[^A-Za-z0-9]", "_", model).upper())
    return int(override) if override else BEDROCK_RPM_LIMIT


def _pacer_reserve(model: str, limit: int) -> float:
    """Try to claim a slot in `model`'s one-minute window.

    Returns 0.0 if claimed, else seconds until the oldest slot expires.
    """
    now = time.monotonic()
    with _pacer_lock:
        window = _pacer_windows.setdefault(model, collections.deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) < limit:
            window.append(now)
            return 0.0
        return max(0.05, 60.0 - (now - window[0]))


def _bedrock_pace_sync(model: str) -> None:
    limit = _bedrock_rpm_limit(model)
    if limit <= 0:
        return
    deadline = time.monotonic() + BEDROCK_PACING_MAX_WAIT_S
    while True:
        wait = _pacer_reserve(model, limit)
        if wait == 0.0:
            return
        if time.monotonic() + wait > deadline:
            logger.warning("bedrock pacer: %s waited %ss without a slot — "
                           "proceeding unpaced", model, int(BEDROCK_PACING_MAX_WAIT_S))
            return
        time.sleep(wait)


async def _bedrock_pace_async(model: str) -> None:
    limit = _bedrock_rpm_limit(model)
    if limit <= 0:
        return
    deadline = time.monotonic() + BEDROCK_PACING_MAX_WAIT_S
    while True:
        wait = _pacer_reserve(model, limit)
        if wait == 0.0:
            return
        if time.monotonic() + wait > deadline:
            logger.warning("bedrock pacer: %s waited %ss without a slot — "
                           "proceeding unpaced", model, int(BEDROCK_PACING_MAX_WAIT_S))
            return
        await asyncio.sleep(wait)

# Gemini emits structured function calls less token-efficiently than Claude — a
# complex tool schema can truncate to MALFORMED_FUNCTION_CALL at small budgets
# (e.g. Claude-Haiku fit extraction in 1024 tokens, flash-lite needs ~4k). Floor
# the output budget when tools are present so forced tool calls aren't truncated.
GEMINI_TOOL_MIN_OUTPUT_TOKENS = int(os.getenv("GEMINI_TOOL_MIN_OUTPUT_TOKENS", "4096"))

# Smaller Gemini models (e.g. flash-lite) intermittently emit a
# MALFORMED_FUNCTION_CALL for complex tool schemas — retry, since it's flaky not
# deterministic (flash-lite ~75% valid → ~98% with 3 attempts).
GEMINI_TOOL_MAX_ATTEMPTS = int(os.getenv("GEMINI_TOOL_MAX_ATTEMPTS", "3"))

# ── Per-call timeout budget ──────────────────────────────────────────────────
# A single model call must NEVER hang forever: a stuck Vertex call froze the
# analysis consumer for ~7h and silently keeps draining credits. Every call is
# bounded two ways: (1) an HTTP-level timeout on the SDK client, and (2) an
# asyncio.wait_for backstop on the async path, in case the SDK timeout fails to
# fire. On timeout the call RAISES and is NOT retried — a hung call should fail
# fast (the pipeline skips that item) rather than double the spend.
LLM_CALL_TIMEOUT_S = float(os.getenv("LLM_CALL_TIMEOUT_S", "120"))
# The asyncio backstop sits just beyond the HTTP timeout so the SDK aborts the
# request cleanly first; wait_for only fires if the SDK somehow does not.
_LLM_WAIT_BACKSTOP_S = LLM_CALL_TIMEOUT_S + float(os.getenv("LLM_TIMEOUT_GRACE_S", "15"))


async def _await_budget(coro, *, model: str, what: str):
    """Await a model-call coroutine under a hard time budget.

    Guarantees the await returns (raising TimeoutError) even if the SDK's own
    HTTP timeout does not fire — so a stuck Vertex call can never block the
    caller indefinitely.
    """
    try:
        return await asyncio.wait_for(coro, timeout=_LLM_WAIT_BACKSTOP_S)
    except asyncio.TimeoutError:
        logger.error(
            "LLM call exceeded %.0fs budget (model=%s, %s) — aborted to avoid a "
            "stuck/credit-draining call", _LLM_WAIT_BACKSTOP_S, model, what)
        raise TimeoutError(
            f"LLM call to {model} exceeded {_LLM_WAIT_BACKSTOP_S:.0f}s budget ({what})")


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
        if CLAUDE_BACKEND == "anthropic":
            return "anthropic"
        if CLAUDE_BACKEND == "bedrock":
            return "anthropic-bedrock"
        return "anthropic-vertex"
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


def to_bedrock_claude_id(model: str) -> str:
    """
    Map a console Claude id to its Bedrock id (verified against the live
    ap-south-1 catalog, 2026-08-12).

    runtime endpoint (dated ids carry Bedrock's -v1:0 suffix; 4.6+/5 ids are bare):
      'claude-haiku-4-5-20251001' -> 'global.anthropic.claude-haiku-4-5-20251001-v1:0'
      'claude-sonnet-4-6'         -> 'global.anthropic.claude-sonnet-4-6'
      'claude-sonnet-5'           -> 'global.anthropic.claude-sonnet-5'
    mantle endpoint (bare undated ids, no profile prefix):
      'claude-haiku-4-5-20251001' -> 'anthropic.claude-haiku-4-5'
    An explicit override may be supplied via BEDROCK_MODEL_<UPPER_SNAKE> env;
    ids already carrying a provider/profile prefix pass through unchanged.
    """
    override = os.getenv("BEDROCK_MODEL_" + re.sub(r"[^A-Za-z0-9]", "_", model).upper())
    if override:
        return override
    if re.match(r"^(global|us|eu|apac|au|jp|anthropic)\.", model):
        return model
    dated = re.match(r"^(claude-[a-z]+-\d+(?:-\d+)?)-(\d{8})$", model)
    if BEDROCK_CLAUDE_ENDPOINT == "mantle":
        return f"anthropic.{dated.group(1)}" if dated else f"anthropic.{model}"
    base = f"anthropic.{dated.group(1)}-{dated.group(2)}-v1:0" if dated else f"anthropic.{model}"
    return f"{BEDROCK_CLAUDE_PROFILE}.{base}" if BEDROCK_CLAUDE_PROFILE else base


# Claude 5-family models reject sampling params (temperature/top_p/top_k) with
# a 400; the 4.x family still accepts temperature. Matches e.g. claude-opus-5,
# claude-sonnet-5, claude-fable-5 — not claude-haiku-4-5.
_CLAUDE5_RE = re.compile(r"^claude-[a-z]+-([5-9]|\d{2,})(?:$|[.-])")


def _is_claude5(model: str) -> bool:
    return bool(_CLAUDE5_RE.match((model or "").lower()))


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
    # Gemini thinking models bill thoughts as output but report them OUTSIDE
    # candidates_token_count. Left uncounted they are invisible spend — the
    # dossier agent's 20-turn loop reasons before every tool call. Claude folds
    # thinking into output_tokens, so this stays 0 on the Anthropic path.
    thinking_tokens: int = 0
    # Input tokens served from cache (billed at a discount). 0 when caching off.
    cached_tokens: int = 0

    @property
    def billed_output_tokens(self) -> int:
        """Output tokens actually billed (Gemini: candidates + thoughts)."""
        return self.output_tokens + self.thinking_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.billed_output_tokens


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


# ── Anthropic providers (Vertex + Bedrock + console) ─────────────────────────

def _anthropic_client(async_: bool, provider: str):
    import anthropic
    if provider == "anthropic-vertex":
        cls = anthropic.AsyncAnthropicVertex if async_ else anthropic.AnthropicVertex
        return cls(project_id=VERTEX_PROJECT, region=VERTEX_CLAUDE_REGION,
                   timeout=LLM_CALL_TIMEOUT_S)
    if provider == "anthropic-bedrock":
        # AWS credential chain (instance role in prod) — no key material here.
        # The SDK retries throttling (429) / 5xx with exponential backoff; the
        # account's 10 RPM default quota needs more headroom than the SDK's
        # default of 2 retries during market-hours bursts (raise via env or a
        # Service Quotas increase, whichever lands first).
        if BEDROCK_CLAUDE_ENDPOINT == "mantle":
            cls = (anthropic.AsyncAnthropicBedrockMantle if async_
                   else anthropic.AnthropicBedrockMantle)
            region = BEDROCK_MANTLE_REGION
        else:
            cls = anthropic.AsyncAnthropicBedrock if async_ else anthropic.AnthropicBedrock
            region = BEDROCK_REGION
        return cls(aws_region=region, timeout=LLM_CALL_TIMEOUT_S,
                   max_retries=int(os.getenv("BEDROCK_MAX_RETRIES", "5")))
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    cls = anthropic.AsyncAnthropic if async_ else anthropic.Anthropic
    return cls(api_key=api_key, timeout=LLM_CALL_TIMEOUT_S)


def _anthropic_kwargs(model: str, provider: str, max_tokens: int, system, messages,
                      tools, tool_choice, temperature) -> Dict[str, Any]:
    if provider == "anthropic-vertex":
        mapped = to_vertex_claude_id(model)
    elif provider == "anthropic-bedrock":
        mapped = to_bedrock_claude_id(model)
    else:
        mapped = model
    kwargs: Dict[str, Any] = {
        "model": mapped,
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
        if _is_claude5(model):
            # Claude 5 models 400 on sampling params. The 0.0/0.4 retry ladders
            # in transcript_* exist to escape Gemini flakes; drop the param
            # rather than fail the call, and log so the ladder can be retired.
            logger.warning("dropping temperature=%s for Claude 5 model %s "
                           "(sampling params rejected by the API)", temperature, model)
        else:
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
        cached_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
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


def _sdk_supports_thought_signatures() -> bool:
    """Does the installed google-genai expose Part.thought_signature?

    google-genai < ~1.21 has NO such field. On those versions every Gemini 3
    multi-turn tool loop silently breaks: extraction reads None, replay drops
    the value, and turn 2 dies with 400 "Function call is missing a
    thought_signature". Cached because it is a pure import-time property.

    Real incident (2026-08-03): requirements/kb.txt pinned pydantic==2.6.3,
    which forced pip to resolve google-genai 1.2.0 in the kb image only. The
    nightly dossier refresh — which runs in kb-monitor — failed 100/100 for
    weeks while the same code worked fine from other images. The version
    floors in requirements/*.txt are the fix; this check makes a regression
    LOUD instead of silent.
    """
    global _SIG_SUPPORT
    if _SIG_SUPPORT is None:
        try:
            from google.genai import types
            _SIG_SUPPORT = "thought_signature" in types.Part.model_fields
        except Exception:
            _SIG_SUPPORT = False
    return _SIG_SUPPORT


_SIG_SUPPORT: Optional[bool] = None
_SIG_WARNED = False


def _warn_if_signatures_unsupported(model: str, has_tools: bool) -> None:
    """Warn once per process when a tool loop is issued on an SDK that cannot
    carry thought signatures — the loop will fail on its SECOND turn."""
    global _SIG_WARNED
    if _SIG_WARNED or not has_tools or _sdk_supports_thought_signatures():
        return
    _SIG_WARNED = True
    try:
        import google.genai as _g
        ver = getattr(_g, "__version__", "unknown")
    except Exception:
        ver = "unknown"
    logger.error(
        "google-genai %s has NO Part.thought_signature; multi-turn tool loops "
        "on %s WILL fail on turn 2 with 400 'missing a thought_signature'. "
        "Rebuild this image against google-genai>=1.21 (see requirements/).",
        ver, model,
    )


def _gemini_translate(system, messages, tools, tool_choice, max_tokens, temperature,
                      _model: str = "gemini"):
    """Translate Anthropic-style args to google-genai contents/config.

    Handles multi-turn tool loops: assistant `tool_use` blocks become Gemini
    function_call parts and user `tool_result` blocks become function_response
    parts. Gemini correlates a response to a call by function *name* (not by
    Anthropic's tool_use_id), so we first map id → name from the assistant turns.
    """
    _warn_if_signatures_unsupported(_model, bool(tools))
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
                    sig = blk.get("_thought_signature")
                    if not blk.get("text") and sig is not None:
                        # Hidden-thought placeholder: replay signature-only,
                        # no empty text part (which Vertex may reject).
                        parts.append(types.Part(thought_signature=sig))
                        continue
                    part = types.Part.from_text(text=blk.get("text", ""))
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
            elif sig is not None:
                # Signature-only part: a hidden thought (no text, no call).
                # Gemini 3 REQUIRES this signature replayed in history — drop
                # it and the next turn 400s with "missing thought_signature".
                blocks.append(ContentBlock(type="text", text="", signature=sig))
    except (AttributeError, IndexError, TypeError):
        pass
    um = getattr(resp, "usage_metadata", None)
    usage = LLMUsage(
        input_tokens=getattr(um, "prompt_token_count", 0) or 0,
        output_tokens=getattr(um, "candidates_token_count", 0) or 0,
        thinking_tokens=getattr(um, "thoughts_token_count", 0) or 0,
        cached_tokens=getattr(um, "cached_content_token_count", 0) or 0,
    )
    return LLMResponse(
        content=blocks, usage=usage, model=model,
        provider="gemini-vertex", raw=resp,
    )


def _gemini_client():
    from google import genai
    from google.genai import types
    # HttpOptions.timeout is in milliseconds. Bounds every Vertex HTTP request so
    # a single generate_content can't hang indefinitely (sync path relies on this;
    # the async path additionally has the asyncio.wait_for backstop).
    return genai.Client(
        vertexai=True, project=VERTEX_PROJECT, location=VERTEX_GEMINI_REGION,
        http_options=types.HttpOptions(timeout=int(LLM_CALL_TIMEOUT_S * 1000)),
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
        if b.type == "text" and (b.text or b.signature is not None):
            # Keep empty-text blocks when they carry a thought_signature —
            # dropping them loses the signature Gemini 3 demands on replay.
            block: Dict[str, Any] = {"type": "text", "text": b.text or ""}
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
    if provider in ("anthropic-vertex", "anthropic-bedrock", "anthropic"):
        client = _anthropic_client(async_=False, provider=provider)
        kwargs = _anthropic_kwargs(model, provider, max_tokens, system, messages,
                                   tools, tool_choice, temperature)
        if provider == "anthropic-bedrock":
            _bedrock_pace_sync(model)
        return _wrap_anthropic_response(client.messages.create(**kwargs), model, provider)
    # gemini-vertex
    contents, cfg = _gemini_translate(system, messages, tools, tool_choice,
                                      max_tokens, temperature, _model=model)
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
    if provider in ("anthropic-vertex", "anthropic-bedrock", "anthropic"):
        client = _anthropic_client(async_=True, provider=provider)
        kwargs = _anthropic_kwargs(model, provider, max_tokens, system, messages,
                                   tools, tool_choice, temperature)
        if provider == "anthropic-bedrock":
            await _bedrock_pace_async(model)
        resp = await _await_budget(client.messages.create(**kwargs),
                                   model=model, what="anthropic.messages.create")
        return _wrap_anthropic_response(resp, model, provider)
    # gemini-vertex
    contents, cfg = _gemini_translate(system, messages, tools, tool_choice,
                                      max_tokens, temperature, _model=model)
    # Retain the client in a local (see note in complete()): an inline
    # `_gemini_client().aio...` lets the Client be GC'd/closed mid-call.
    client = _gemini_client()
    attempts = GEMINI_TOOL_MAX_ATTEMPTS if tools else 1
    for attempt in range(attempts):
        resp = await _await_budget(
            client.aio.models.generate_content(model=model, contents=contents, config=cfg),
            model=model, what="gemini.generate_content")
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
    """True if an exception is a rate limit — Vertex 429 / RESOURCE_EXHAUSTED
    or Bedrock ThrottlingException."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code == 429:
        return True
    s = str(e).upper()
    return "RESOURCE_EXHAUSTED" in s or "429" in s or "THROTTLING" in s


# Google-Search grounding has its own (low) per-minute Vertex quota, separate
# from token quotas. Under concurrency the dossier agent's web_search bursts can
# exceed it; back off and retry rather than failing the call.
GROUNDING_MAX_RETRIES = int(os.getenv("GROUNDING_MAX_RETRIES", "4"))


def _log_grounding_cost(model: str, usage: "LLMUsage", latency_ms: float) -> None:
    """Record ONE web_search grounding call in the shared LLM cost log.

    grounded_search is a SEPARATE Gemini call (default gemini-3.5-flash) whose
    tokens never flow back into the caller's loop accounting — so without this
    they are invisible spend, on top of Google Search's per-query grounding fee
    (billed per call, not per token; compute_cost does not yet price it — the
    web_search_calls=1 tag makes the calls countable so the fee can be applied
    once its rate is confirmed). Stage 'dossier_grounding' keeps these separate
    from the main 'dossier' pro-loop records so neither double-counts the other.
    """
    try:
        from bharat_terminal.analysis.llm_logger import (
            get_llm_logger, LLMCallRecord, compute_cost,
        )
        billed_out = (usage.output_tokens or 0) + (usage.thinking_tokens or 0)
        get_llm_logger().log(LLMCallRecord(
            call_id=f"grounding_{time.time_ns()}",
            model=model,
            stage="dossier_grounding",
            prompt_tokens=usage.input_tokens or 0,
            completion_tokens=billed_out,
            latency_ms=latency_ms,
            cost_usd=compute_cost(model, usage.input_tokens or 0, billed_out),
            success=True,
            thinking_tokens=usage.thinking_tokens or 0,
            cached_tokens=usage.cached_tokens or 0,
            web_search_calls=1,
            turns=1,
        ))
    except Exception as e:  # cost logging must never break grounding
        logger.warning("grounding cost-log emit failed: %s", str(e)[:200])


def _nova_grounding_call(query: str, max_tokens: int) -> Dict[str, Any]:
    """One synchronous Nova web-grounding call (Bedrock Converse, US CRIS).

    Returns {"text", "sources", "_usage"}. Raises on error — retry/degrade
    policy lives in grounded_search. boto3 arrives via the anthropic[bedrock]
    extra, so every image with the wrapper's Bedrock path can run this.
    """
    import boto3
    from botocore.config import Config
    client = boto3.client(
        "bedrock-runtime", region_name=NOVA_GROUNDING_REGION,
        config=Config(read_timeout=LLM_CALL_TIMEOUT_S, connect_timeout=10,
                      retries={"max_attempts": 0}),  # retry policy is ours, not botocore's
    )
    resp = client.converse(
        modelId=NOVA_GROUNDING_MODEL,
        messages=[{"role": "user", "content": [{"text": query}]}],
        toolConfig={"tools": [{"systemTool": {"name": "nova_grounding"}}]},
        inferenceConfig={"maxTokens": max_tokens},
    )
    text, sources = "", []
    for c in resp["output"]["message"]["content"]:
        if "text" in c:
            text += c["text"]
        cc = c.get("citationsContent")
        blocks = cc if isinstance(cc, list) else ([cc] if cc else [])
        for blk in blocks:
            for cit in (blk.get("citations") or []) if isinstance(blk, dict) else []:
                url = ((cit.get("location") or {}).get("web") or {}).get("url")
                if url and url not in sources:
                    sources.append(url)
    u = resp.get("usage", {})
    usage = LLMUsage(input_tokens=u.get("inputTokens", 0),
                     output_tokens=u.get("outputTokens", 0))
    return {"text": text, "sources": sources, "_usage": usage}


async def grounded_search(
    query: str, *, model: Optional[str] = None, max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Web-grounded answer for `query` — the backend behind the agents'
    `web_search` function tool, selected by GROUNDING_BACKEND (gemini | nova).

    Returns ``{"text": str, "sources": [url, ...]}``. On a persistent rate limit
    or timeout it degrades gracefully to an ``error`` dict so the agent proceeds
    on DB-grounded data rather than crashing. Both backends log one cost record
    per call as stage='dossier_grounding' with web_search_calls=1 (any per-query
    grounding fee is not in compute_cost — the counter makes it applicable later).
    """
    if GROUNDING_BACKEND == "nova":
        for attempt in range(GROUNDING_MAX_RETRIES):
            try:
                _g0 = time.monotonic()
                out = await _await_budget(
                    asyncio.to_thread(_nova_grounding_call, query, max_tokens),
                    model=NOVA_GROUNDING_MODEL, what="grounded_search[nova]")
                usage = out.pop("_usage")
                _log_grounding_cost(NOVA_GROUNDING_MODEL, usage,
                                    (time.monotonic() - _g0) * 1000)
                return out
            except TimeoutError as e:
                logger.warning("grounded_search[nova] timed out; degrading gracefully: %s",
                               str(e)[:200])
                return {"text": "", "sources": [],
                        "error": "web_search timed out — proceed using DB-grounded data; "
                                 "do not retry this query immediately."}
            except Exception as e:
                if not _is_rate_limited(e):
                    raise
                if attempt == GROUNDING_MAX_RETRIES - 1:
                    logger.warning("grounded_search[nova] throttled after %d attempts; "
                                   "degrading gracefully: %s", GROUNDING_MAX_RETRIES, str(e)[:200])
                    return {"text": "", "sources": [],
                            "error": "web_search temporarily rate-limited — proceed using "
                                     "DB-grounded data; do not retry this query immediately."}
                delay = 2.0 * (2 ** attempt)  # deterministic backoff: 2s, 4s, 8s
                logger.info("grounded_search[nova] throttled; backing off %.0fs (attempt %d/%d)",
                            delay, attempt + 1, GROUNDING_MAX_RETRIES)
                await asyncio.sleep(delay)

    # gemini backend: Google Search grounding on Vertex. Grounding cannot be
    # mixed with function declarations in one request, so this is a standalone
    # grounded call with NO tools other than `google_search`.
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
            _g0 = time.monotonic()
            resp = await _await_budget(
                client.aio.models.generate_content(model=model, contents=contents, config=cfg),
                model=model, what="grounded_search")
            wrapped = _wrap_gemini_response(resp, model)
            _log_grounding_cost(model, wrapped.usage, (time.monotonic() - _g0) * 1000)
            return {"text": wrapped.text, "sources": _extract_sources(resp)}
        except TimeoutError as e:
            # A stuck grounding call must not hang the agent or burn credits on a
            # retry — degrade gracefully so the dossier proceeds on DB-grounded data.
            logger.warning("grounded_search timed out; degrading gracefully: %s", str(e)[:200])
            return {"text": "", "sources": [],
                    "error": "web_search timed out — proceed using DB-grounded data; "
                             "do not retry this query immediately."}
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
