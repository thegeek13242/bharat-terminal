"""Accurate, grounded summaries of exchange-filing attachments.

For NSE/BSE filing items the headline is often boilerplate ("Intimation under
Regulation 30…") while the substance sits in the PDF attachment text that
ingestion merged into full_body. This stage produces a short extractive
summary shown on the terminal next to the filing.

Accuracy doctrine (CLAUDE.md: prefer parse-failure over wrong data):
  * The model is instructed to be strictly extractive and to copy every
    figure verbatim — no unit conversions, no computed growth rates.
  * A post-generation grounding gate verifies every numeric token in the
    summary appears in the source text. ANY miss rejects the whole summary.
  * Procedural filings with no substance produce no summary at all.
  * Every failure path returns None — no summary is always acceptable,
    a wrong summary never is.

Customer-visible text: descriptive only, no market view, no advice.
"""
import logging
import os
import re
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

SUMMARY_MODEL = os.getenv("FILING_SUMMARY_MODEL", "gemini-2.5-flash")
SUMMARY_BODY_CHARS = int(os.getenv("FILING_SUMMARY_BODY_CHARS", "120000"))
FILING_SOURCES = {"NSE_FILINGS", "BSE_FILINGS"}
# Below this the "attachment" is effectively empty — nothing to summarize.
MIN_BODY_CHARS = int(os.getenv("FILING_SUMMARY_MIN_BODY_CHARS", "200"))
MIN_SUMMARY_CHARS = 40
MAX_SUMMARY_CHARS = 700

SUMMARY_TOOL = {
    "name": "summarize_filing",
    "description": "Produce a strictly extractive summary of an exchange filing",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_substance": {
                "type": "boolean",
                "description": (
                    "false if the filing is purely procedural (AGM/record-date "
                    "intimation, trading-window closure, meeting schedule) with "
                    "no material content worth summarizing."
                ),
            },
            "summary": {
                "type": "string",
                "maxLength": 700,
                "description": "2-4 extractive sentences; empty if has_substance is false.",
            },
        },
        "required": ["has_substance", "summary"],
    },
}

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """Normalized numeric tokens: commas stripped; for decimals also add the
    trailing-zero-trimmed form so '5,700.00' matches '5700'."""
    out: set[str] = set()
    for m in _NUM_RE.findall(text or ""):
        n = m.replace(",", "")
        out.add(n)
        if "." in n:
            trimmed = n.rstrip("0").rstrip(".")
            if trimmed:
                out.add(trimmed)
    return out


def ungrounded_numbers(summary: str, source: str) -> set[str]:
    """Numeric tokens in the summary that do not appear in the source text.
    Non-empty result means the summary fails the accuracy gate."""
    src = _numbers(source)
    return {n for n in _numbers(summary) if n not in src}


# ── Currency gate ────────────────────────────────────────────────────────────
# PDF extraction from Indian annual reports frequently corrupts the rupee
# glyph: fonts that remapped '$' to draw '₹' extract as a literal '$'
# (observed: "final dividend of $ 25 per equity share of $10 each" for an
# NSE-listed company's rupee dividend). A bare '$'/'€'/'£' glyph in extracted
# filing text is therefore UNTRUSTWORTHY. A foreign-currency marker in the
# summary is allowed only when the source corroborates it in words
# ("USD", "US$", "dollar", …) — glyphs alone do not count. Rupee notation
# (₹/Rs./INR) is never blocked. Residual risk: a filing writing a genuine
# dollar amount only as a bare '$' loses its summary — fail-safe by design.
_FOREIGN_CURRENCY_CLASSES = {
    "dollar": (re.compile(r"\$|\bUSD\b|\bUS\$", re.I), re.compile(r"\bUSD\b|US\$|\bdollars?\b", re.I)),
    "euro": (re.compile(r"€|\bEUR\b", re.I), re.compile(r"\bEUR\b|\beuros?\b", re.I)),
    "pound": (re.compile(r"£|\bGBP\b", re.I), re.compile(r"\bGBP\b|\bpounds?\b|\bsterling\b", re.I)),
}


def uncorroborated_currencies(summary: str, source: str) -> set[str]:
    """Foreign-currency classes used in the summary that the source does not
    corroborate in WORD form. Non-empty result fails the accuracy gate."""
    out = set()
    for name, (in_summary, word_form_in_source) in _FOREIGN_CURRENCY_CLASSES.items():
        if in_summary.search(summary) and not word_form_in_source.search(source):
            out.add(name)
    return out


def _build_prompt(headline: str, body: str) -> str:
    return f"""You are an equity desk analyst summarizing an exchange filing for a market terminal. Below is the filing headline and the full text of its attachment.

STRICT RULES:
- Extractive only: state ONLY facts written in the document. No inference, no
  interpretation, no market view, no advice.
- Copy every figure EXACTLY as it appears in the document — same units, same
  digits. Do NOT convert units, compute totals, growth rates, or differences.
  A figure you cannot quote verbatim must be left out.
- CURRENCY: the extracted text often corrupts the rupee symbol '₹' into '$'.
  Amounts in Indian exchange filings are rupee-denominated unless the document
  says USD/US$/dollar IN WORDS. Write rupee amounts as '₹' or 'Rs.'; never
  write '$' based on the symbol alone.
- 2-4 sentences: what kind of filing this is, the material facts (parties,
  amounts, dates, quantities), and any stated purpose or reason.
- If the document is purely procedural with no material content, set
  has_substance to false and leave the summary empty.

HEADLINE: {headline}

ATTACHMENT TEXT:
{body}"""


async def summarize_filing(headline: str, body: Optional[str], source: str,
                           news_id: str = "") -> Optional[str]:
    """Summarize a filing attachment. Returns None on any doubt — callers must
    treat None as 'no summary', never as an error. news_id is cost-attribution
    only (llm_call_log.context_id)."""
    if source not in FILING_SOURCES:
        return None
    text_body = (body or "").strip()[:SUMMARY_BODY_CHARS]
    if len(text_body) < MIN_BODY_CHARS:
        return None

    from bharat_terminal import llm
    from bharat_terminal.analysis.llm_logger import get_llm_logger, LLMCallRecord, compute_cost

    llm_logger = get_llm_logger()
    t0 = time.time()
    try:
        response = await llm.acomplete(
            model=SUMMARY_MODEL,
            max_tokens=1024,
            tools=[SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "summarize_filing"},
            messages=[{"role": "user", "content": _build_prompt(headline, text_body)}],
        )
        llm_logger.log(LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=SUMMARY_MODEL,
            stage="filing_summary",
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            latency_ms=(time.time() - t0) * 1000,
            cost_usd=compute_cost(SUMMARY_MODEL, response.usage.input_tokens, response.usage.output_tokens),
            success=True,
            context_id=news_id,
        ))

        tool_result = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "summarize_filing":
                tool_result = block.input
                break
        if not tool_result:
            return None
        if not tool_result.get("has_substance"):
            return None

        summary = (tool_result.get("summary") or "").strip()
        if not (MIN_SUMMARY_CHARS <= len(summary) <= MAX_SUMMARY_CHARS):
            logger.info(f"[filing_summary] rejected: length {len(summary)} outside bounds")
            return None

        # ── Accuracy gate: every number must exist verbatim in the source ──
        source_text = headline + "\n" + text_body
        missing = ungrounded_numbers(summary, source_text)
        if missing:
            logger.warning(
                f"[filing_summary] REJECTED ungrounded numbers {sorted(missing)[:5]} "
                f"for headline {headline[:60]!r}"
            )
            return None

        # ── Currency gate: foreign symbols need word-form corroboration ──
        bad_currencies = uncorroborated_currencies(summary, source_text)
        if bad_currencies:
            logger.warning(
                f"[filing_summary] REJECTED uncorroborated currency {sorted(bad_currencies)} "
                f"(likely a corrupted rupee glyph) for headline {headline[:60]!r}"
            )
            return None

        return summary

    except Exception as e:
        llm_logger.log(LLMCallRecord(
            call_id=str(uuid.uuid4()),
            model=SUMMARY_MODEL,
            stage="filing_summary",
            prompt_tokens=0, completion_tokens=0,
            latency_ms=(time.time() - t0) * 1000, cost_usd=0.0,
            success=False, error=str(e),
            context_id=news_id,
        ))
        logger.error(f"[filing_summary] error (no summary emitted): {e}")
        return None
