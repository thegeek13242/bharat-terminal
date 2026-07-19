"""F12 research chat — INTERNAL-ONLY conversational research (dogfood).

Mounted exclusively behind require_internal_user (INTERNAL_RESEARCH_EMAILS).
Do not widen to customers until the dogfood period has measured groundedness
and the RA posture is counsel-reviewed (wish-board F12 sequencing).

Every answer is audit-logged append-only to research_chat_log with its full
tool trace — an answer either has an evidence trail or it doesn't ship.
"""
import json
import logging
from typing import List, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from bharat_terminal.api.auth import require_internal_user
from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research-chat", tags=["research-chat"])

MAX_HISTORY = 24
MAX_MESSAGE_CHARS = 4000


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


@router.post("/ask")
async def ask(
    messages: List[ChatMessage] = Body(..., embed=True),
    user: dict = Depends(require_internal_user),
):
    if not messages or messages[-1].role != "user":
        raise HTTPException(status_code=422, detail="last_message_must_be_user")
    history = [m.model_dump() for m in messages[-MAX_HISTORY:]]

    from bharat_terminal.research.chat_engine import ask as engine_ask

    try:
        out = await engine_ask(history)
    except Exception as e:
        logger.error(f"[research-chat] engine error: {e}", exc_info=True)
        out = {"answer": "", "citations": [], "status": "model_error",
               "model": None, "turns": 0, "tool_trace": [],
               "usage": {}, "latency_ms": 0.0, "invalid_refs_stripped": 0}

    # Append-only audit — log failures too; never let logging break the reply.
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            await session.execute(text("""
                INSERT INTO research_chat_log
                    (user_email, question, history_len, answer, status,
                     citations, tool_trace, model, latency_ms, turns,
                     input_tokens, output_tokens, thinking_tokens,
                     web_search_calls)
                VALUES (:email, :q, :hlen, :a, :status, :cites, :trace,
                        :model, :lat, :turns, :tin, :tout, :tthink, :web)
            """), {
                "email": user.get("email") or "?",
                "q": history[-1]["content"],
                "hlen": len(history) - 1,
                "a": out["answer"],
                "status": out["status"],
                "cites": json.dumps(out["citations"]),
                "trace": json.dumps(out["tool_trace"]),
                "model": out["model"],
                "lat": out["latency_ms"],
                "turns": out["turns"],
                "tin": out["usage"].get("input_tokens"),
                "tout": out["usage"].get("output_tokens"),
                "tthink": out["usage"].get("thinking_tokens"),
                "web": out["usage"].get("web_search_calls"),
            })
            await session.commit()
    except Exception as e:
        logger.error(f"[research-chat] audit log failed: {e}")

    if out["status"] == "model_error":
        raise HTTPException(status_code=503, detail="chat_unavailable")
    return {k: out[k] for k in
            ("answer", "citations", "status", "model", "turns", "latency_ms")}
