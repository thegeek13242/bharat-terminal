"""Render the report. Formatting only — every value comes from ReportFacts."""
from __future__ import annotations

from typing import List, Optional

from bharat_terminal.factory.report.facts import ReportFacts

SUBJECT_PREFIX = "[Quant Research] 24h Research Report"


def subject(facts: ReportFacts) -> str:
    return f"{SUBJECT_PREFIX} — {facts.report_date.isoformat()}"


def render_text(facts: ReportFacts, narrative: Optional[str] = None) -> str:
    L: List[str] = []
    sc = facts.scorecard

    L.append(subject(facts))
    L.append("")

    L.append("EXECUTIVE SUMMARY")
    L.append("")
    if facts.activity_low:
        L.append(f"Research activity was LOW: {sc.experiments_completed} experiment(s) "
                 f"completed in the last 24h.")
    if narrative:
        L.append(narrative)
    elif not facts.activity_low:
        L.append("(narrative unavailable — agent did not return prose)")
    L.append("")

    L.append("RESEARCH SCORECARD")
    L.append("")
    L.append(f"Hypotheses tested:            {sc.hypotheses_tested}")
    L.append(f"Experiments completed:        {sc.experiments_completed}")
    L.append(f"Hypotheses rejected:          {sc.rejected}")
    L.append(f"Hypotheses promoted:          {sc.promoted}")
    L.append(f"Entered shadow:               {sc.entered_shadow}")
    L.append(f"Entered paper:                {sc.entered_paper}")
    L.append(f"LLM spend (USD):              {sc.llm_usd}")
    L.append(f"Eval spend (USD):             {sc.eval_usd}")
    if sc.budget_remaining_usd is not None:
        L.append(f"Budget remaining (USD):       {sc.budget_remaining_usd}")
    L.append("")

    if facts.hypotheses:
        L.append("HYPOTHESES TESTED")
        L.append("")
        for h in facts.hypotheses:
            L.append(f"  {h.get('hypothesis_id', '?')} — {h.get('to_status', '?')}"
                     + (f" ({h.get('experiment_id')})" if h.get("experiment_id") else ""))
        L.append("")

    if facts.failures:
        L.append("WHAT FAILED")
        L.append("")
        for f in facts.failures:
            L.append(f"  {f.get('failure_id', '?')} — {f.get('reason', '')}")
            if f.get("lesson"):
                L.append(f"    Lesson: {f['lesson']}")
        L.append("")

    if facts.paper:
        L.append("PAPER TRADING")
        L.append("")
        for p in facts.paper:
            L.append(f"  {p.get('strategy_id', '?')} — {p.get('classification', 'n/a')}")
        L.append("")

    L.append("DATA INTEGRITY")
    L.append("")
    L.append(f"Dataset: {facts.dataset_version or 'none'}"
             + (f" (age {facts.snapshot_age_hours:.1f}h)"
                if facts.snapshot_age_hours is not None else ""))
    L.append(f"Flags: {', '.join(facts.integrity_flags) or 'none'}")
    L.append(f"Failures: {', '.join(facts.integrity_failures) or 'none'}")
    L.append("")

    L.append("SYSTEM HEALTH")
    L.append("")
    if facts.health:
        for k, v in sorted(facts.health.items()):
            L.append(f"  {k}: {v}")
    else:
        L.append("  (no health data recorded)")
    L.append("")

    if facts.action_required:
        L.append("ACTION REQUIRED")
        L.append("")
        for a in facts.actions:
            L.append(f"  - {a}")
    else:
        L.append("NO ACTION REQUIRED")
    L.append("")

    return "\n".join(L)
