"""
Research-runner CLI.

Three operations:

  python -m bharat_terminal.research.main seed
      → upserts triggers, macro_events, event_degrees, event_sector_edges,
        event_transmissions, universe_membership rows from the static
        seed files (events_seed.py + universe.py). Idempotent.

  python -m bharat_terminal.research.main run
      → runs the research agent across the (universe × triggers) grid.
        Skips pairs that already have a fresh agent_research row in
        trigger_betas (within 90 days).

  python -m bharat_terminal.research.main run --symbols=RELIANCE,INDIGO --triggers=CRUDEOIL,USDINR
      → targeted single-symbol research.

The runner is intentionally a one-shot batch — invoke from cron / workflow
rather than running as a long-lived service. Cost per pair is ~$0.05-0.20
depending on filing length, so the full NIFTY 50+BankNifty × commodities
grid (~60 × 7) is a few dollars per refresh cycle.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

import anthropic

from bharat_terminal.research import db, agent
from bharat_terminal.research.events_seed import (
    EVENT_DEGREES,
    EVENT_SECTOR_EDGES,
    EVENT_TRANSMISSIONS,
    MACRO_EVENTS,
)
from bharat_terminal.research.universe import (
    BANKNIFTY,
    NIFTY50,
    RESEARCH_UNIVERSE,
    TRIGGERS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


DEFAULT_TRIGGERS_TO_RESEARCH = (
    # Continuous commodity / FX / rate triggers — the ones where filings
    # actually disclose enough cost-structure detail for the agent to ground
    # a beta in. Discrete event triggers (FOMC, RBI MPC) need a different
    # methodology (event-study regression, not cost-structure).
    "CRUDEOIL", "BRENT", "GOLD", "SILVER", "COPPER",
    "ALUMINIUM", "NATURALGAS", "USDINR",
)


# ── seed ────────────────────────────────────────────────────────────────

async def _seed() -> None:
    n_triggers = await db.upsert_triggers([dict(t) for t in TRIGGERS])
    logger.info(f"Seeded triggers: {n_triggers}")

    macro_events = [
        {
            "id": eid,
            "label": label,
            "trigger_id": trig,
            "trigger_spec": spec,
            "propagation_chain": chain,
            "notes": notes,
        }
        for (eid, label, trig, spec, chain, notes) in MACRO_EVENTS
    ]
    logger.info(f"Seeded macro_events: {await db.upsert_macro_events(macro_events)}")

    degrees = [
        {"event_id": eid, "degree": deg, "label": label, "transmission_channel": tc}
        for (eid, deg, label, tc) in EVENT_DEGREES
    ]
    logger.info(f"Seeded event_degrees: {await db.upsert_event_degrees(degrees)}")

    edges = [
        {
            "event_id": eid,
            "degree": deg,
            "sector": sec,
            "direction": direction,
            "magnitude_prior": mag,
            "lag_text": lag_text,
            "lag_min_days": lag_min,
            "lag_max_days": lag_max,
            "mechanism": mech,
        }
        for (eid, deg, sec, direction, mag, lag_text, lag_min, lag_max, mech)
        in EVENT_SECTOR_EDGES
    ]
    logger.info(f"Seeded event_sector_edges: {await db.upsert_event_sector_edges(edges)}")

    trans = [
        {
            "event_id": eid,
            "from_degree": fd,
            "to_degree": td,
            "gating_trigger_id": tg,
            "confirms_threshold": ct,
            "refutes_threshold": rt,
            "description": desc,
        }
        for (eid, fd, td, tg, ct, rt, desc) in EVENT_TRANSMISSIONS
    ]
    logger.info(f"Seeded event_transmissions: {await db.upsert_event_transmissions(trans)}")

    logger.info(
        f"Seeded universe NIFTY50: {await db.upsert_universe('NIFTY50', list(NIFTY50))}"
    )
    logger.info(
        f"Seeded universe BANKNIFTY: {await db.upsert_universe('BANKNIFTY', list(BANKNIFTY))}"
    )


# ── run ─────────────────────────────────────────────────────────────────

async def _build_target_pairs(
    symbols: List[str], triggers: List[str], force: bool, freshness_days: int
) -> List[Tuple[str, str]]:
    """Build the (symbol, trigger) pairs that need refreshing."""
    pairs: List[Tuple[str, str]] = []
    threshold = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    for symbol in symbols:
        for trigger_id in triggers:
            if not force:
                existing = await db.get_best_trigger_beta(symbol, trigger_id)
                if (
                    existing
                    and existing.get("derivation_method") == "agent_research"
                    and existing.get("calibrated_at")
                    and existing["calibrated_at"] >= threshold
                ):
                    continue
            pairs.append((symbol, trigger_id))
    return pairs


async def _run(
    symbols: List[str],
    triggers: List[str],
    *,
    concurrency: int = 4,
    force: bool = False,
    freshness_days: int = 90,
) -> None:
    pairs = await _build_target_pairs(symbols, triggers, force, freshness_days)
    if not pairs:
        logger.info("Nothing to research — all pairs are fresh.")
        return

    logger.info(
        f"Starting agent research over {len(pairs)} pairs "
        f"({len(symbols)} symbols × {len(triggers)} triggers, concurrency={concurrency})"
    )

    # Build a label lookup so the agent can be told the trigger's human name.
    trigger_label = {t["id"]: t["label"] for t in TRIGGERS}

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

    sem = asyncio.Semaphore(concurrency)
    successes = 0
    failures = 0

    async def one(symbol: str, trigger_id: str) -> None:
        nonlocal successes, failures
        async with sem:
            try:
                result = await agent.research_pair(
                    symbol, trigger_id,
                    trigger_label.get(trigger_id, trigger_id),
                    client=client,
                )
                if result:
                    successes += 1
                else:
                    failures += 1
            except Exception as e:
                failures += 1
                logger.error(f"[RESEARCH] {symbol}/{trigger_id}: {e}", exc_info=True)

    await asyncio.gather(*(one(s, t) for s, t in pairs))
    logger.info(f"Research complete: {successes} ok, {failures} failed of {len(pairs)} pairs")


# ── CLI ─────────────────────────────────────────────────────────────────

def _parse_csv(s: str | None, default: List[str]) -> List[str]:
    if not s:
        return default
    return [x.strip().upper() for x in s.split(",") if x.strip()]


async def _amain(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="bharat_terminal.research.main")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="Seed triggers + macro_events + universe")

    p_run = sub.add_parser("run", help="Run agent research over the (universe × triggers) grid")
    p_run.add_argument("--symbols", help="Comma-separated; default = NIFTY50 ∪ BankNifty")
    p_run.add_argument("--triggers", help=f"Comma-separated; default = {','.join(DEFAULT_TRIGGERS_TO_RESEARCH)}")
    p_run.add_argument("--concurrency", type=int, default=4)
    p_run.add_argument("--force", action="store_true",
                       help="Re-research even if a fresh row exists.")
    p_run.add_argument("--freshness-days", type=int, default=90,
                       help="Skip pairs newer than this many days (unless --force).")

    args = ap.parse_args(argv)

    if args.cmd == "seed":
        await _seed()
        return 0

    if args.cmd == "run":
        symbols = _parse_csv(args.symbols, list(RESEARCH_UNIVERSE))
        triggers = _parse_csv(args.triggers, list(DEFAULT_TRIGGERS_TO_RESEARCH))
        await _run(
            symbols, triggers,
            concurrency=args.concurrency,
            force=args.force,
            freshness_days=args.freshness_days,
        )
        return 0

    ap.print_help()
    return 1


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
