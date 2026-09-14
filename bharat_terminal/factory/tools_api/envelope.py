"""Response envelope, truncation, and the untrusted-text marker.

Two ideas carry most of the weight here.

**Truncation is stated, never silent.** An agent reasoning over a clipped list
should know the list was clipped; otherwise "there are only 3 prior failures in
this family" becomes a confident wrong premise.

**Agent-authored text is data, not instruction.** Rows in `failures` and
`hypotheses` were written by other agents, so a read tool is an agent-to-agent
channel. Wrapping such text in a named field keeps it out of the narration
position, where a model is most likely to treat it as direction.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# A poisoned or merely verbose memo must not be able to crowd out a context
# window, so every free-text field and every list has a ceiling.
MAX_TEXT_CHARS = 2_000
MAX_ROWS = 50


def untrusted(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Wrap agent-authored free text so it cannot sit in narration position.

    Returns None for None so an absent field stays absent rather than becoming
    an empty quotation.
    """
    if text is None:
        return None
    s = str(text)
    clipped = len(s) > MAX_TEXT_CHARS
    return {
        "untrusted_text": s[:MAX_TEXT_CHARS],
        "truncated": clipped,
    }


def truncate(rows: Sequence[Any], cap: int = MAX_ROWS) -> Tuple[List[Any], bool]:
    """Return (rows, truncated). The flag is the point of the function."""
    out = list(rows[:cap])
    return out, len(rows) > cap


def _jsonable(value: Any) -> Any:
    """Dates and Decimals do not survive JSON. Convert rather than crash."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__float__") and type(value).__name__ == "Decimal":
        return float(value)
    return value


def envelope(
    tool: str,
    data: Any,
    *,
    dataset_version: Optional[str] = None,
    truncated: bool = False,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """The single response shape every read tool returns."""
    env: Dict[str, Any] = {
        "tool": tool,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "truncated": truncated,
        "data": _jsonable(data),
    }
    if dataset_version is not None:
        env["dataset_version"] = dataset_version
    if note is not None:
        env["note"] = note
    return env
