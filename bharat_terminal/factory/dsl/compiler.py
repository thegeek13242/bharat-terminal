"""The compiler — deterministic gatekeeper between an agent's idea and compute.

Nothing reaches the backtester without passing every check here. The checks
encode this project's verified data hazards (RESEARCH_FACTORY_SPEC_v2 §1.1,
§6.3): an event clock that trails dissemination by a median 0.9h (6.2h via the
NSE XBRL path), unadjusted closes, an empty point-in-time universe, a sector
label filled 18.8% of the time, and an event-analysis history that only starts
2026-05-10.

Pure functions over injected context — no DB, no network — so the whole thing
is unit-testable and reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from bharat_terminal.factory.dsl.hashing import spec_hash
from bharat_terminal.factory.dsl.models import (
    Benchmark,
    Direction,
    EntryRule,
    EventClock,
    HoldoutPolicy,
    Horizon,
    HypothesisSpec,
)
from bharat_terminal.factory.registry.features import (
    CONTROL_COVERAGE_FLOOR,
    FeatureSpec,
    get_feature,
    parse_ref,
)

# The sealed holdout boundary for Tier-H research (spec §7.1). Data on/after
# this date is reachable exactly once per hypothesis version, through the gate
# engine. Changing it is a human decision recorded in the ledger.
SEALED_FROM = date(2025, 9, 12)

# MID150BEES only starts here (STAGE0_DATA_REALITY_2026-08-15).
MID150BEES_START = date(2022, 5, 1)

MAX_SNAPSHOT_AGE_HOURS = 26.0

# Ops that only make sense on a numeric feature.
_NUMERIC_OPS = {
    "gt", "ge", "lt", "le",
    "decile_ge", "decile_le", "pctile_ge", "pctile_le",
    "zscore_ge", "zscore_le",
}
_CATEGORICAL_OPS = {"eq", "in", "changed_to"}

# Intraday-shaped entry rules: they need a timestamp, not a date.
_INTRADAY_RULES = {
    EntryRule.t_plus_minutes,
    EntryRule.preopen_auction,
    EntryRule.same_session_close_if_before,
}

_FNO_BASES = {"fno_lot_universe"}

# Event sources whose features feed the E-series pre-registration. Families
# built on these may not touch the embargoed Q2 window until the founder lifts
# it (spec §7.1).
EMBARGOED_SOURCES = {"earnings_surprises", "guidance_changes"}


@dataclass(frozen=True)
class CompileError:
    code: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(frozen=True)
class EventCounts:
    """What the snapshot actually contains for this spec, pre-holdout."""

    events: int
    cohorts: int
    symbols: int
    window_start: Optional[date] = None
    window_end: Optional[date] = None


@dataclass(frozen=True)
class Embargo:
    start: date
    end: date
    reason: str
    sources: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CompileContext:
    """Everything the compiler is allowed to know about the world."""

    dataset_version: str
    snapshot_age_hours: float
    source_max_dates: Dict[str, date] = field(default_factory=dict)
    # Integrity findings open against the snapshot, e.g.
    # {"clock_lags_dissemination"}.
    integrity_flags: frozenset = frozenset()
    # Findings severe enough to stop all new work.
    integrity_failures: frozenset = frozenset()
    sealed_from: date = SEALED_FROM
    embargoes: Tuple[Embargo, ...] = ()
    known_spec_hashes: Dict[str, str] = field(default_factory=dict)  # hash -> experiment_id
    family_trials_remaining: int = 10_000
    event_counter: Optional[Callable[[HypothesisSpec], EventCounts]] = None


@dataclass(frozen=True)
class ExperimentPlan:
    """The compiled, executable form. Authored by the compiler, not the agent."""

    spec_hash: str
    data_tier: str
    trial_count: int
    study_start: date
    holdout: str
    feature_versions: Dict[str, str]
    bias_flags: Tuple[str, ...]
    constraints: Tuple[str, ...]
    counts: Optional[EventCounts] = None


@dataclass(frozen=True)
class CompileResult:
    ok: bool
    errors: Tuple[CompileError, ...] = ()
    warnings: Tuple[str, ...] = ()
    plan: Optional[ExperimentPlan] = None
    duplicate_of: Optional[str] = None
    checks_deferred: Tuple[str, ...] = ()


def _resolve_features(spec: HypothesisSpec) -> Tuple[Dict[str, FeatureSpec], List[CompileError]]:
    resolved: Dict[str, FeatureSpec] = {}
    errors: List[CompileError] = []
    for ref in spec.feature_refs():
        name, version = parse_ref(ref)
        if version is None:
            errors.append(CompileError(
                "E001", "features",
                f"feature {ref!r} is unversioned — pin it as name@version so the run is reproducible",
            ))
            continue
        feat = get_feature(ref)
        if feat is None:
            errors.append(CompileError(
                "E001", "features", f"feature {ref!r} is not in the registry",
            ))
            continue
        resolved[ref] = feat
    return resolved, errors


def compile_spec(spec: HypothesisSpec, ctx: CompileContext) -> CompileResult:
    errors: List[CompileError] = []
    warnings: List[str] = []
    deferred: List[str] = []
    constraints: List[str] = []
    bias: List[str] = []

    # ── E015: never plan work on a stale or broken snapshot ──────────────
    if ctx.snapshot_age_hours > MAX_SNAPSHOT_AGE_HOURS:
        errors.append(CompileError(
            "E015", "dataset",
            f"snapshot {ctx.dataset_version} is {ctx.snapshot_age_hours:.1f}h old "
            f"(limit {MAX_SNAPSHOT_AGE_HOURS}h) — fail closed",
        ))
    if ctx.integrity_failures:
        errors.append(CompileError(
            "E015", "dataset",
            f"integrity failures open on {ctx.dataset_version}: {sorted(ctx.integrity_failures)}",
        ))

    # ── E001: resolve every feature reference ────────────────────────────
    features, ref_errors = _resolve_features(spec)
    errors.extend(ref_errors)

    clock = spec.event.clock
    rule = spec.entry.rule

    # ── E002: point-in-time validity of each feature for this clock ──────
    for ref, feat in features.items():
        if clock.value not in feat.valid_clocks:
            errors.append(CompileError(
                "E002", "event.clock",
                f"feature {ref!r} is not point-in-time on clock {clock.value!r} "
                f"(valid: {list(feat.valid_clocks)})",
            ))

    # ── E003: blocked clock/entry-rule pairs ─────────────────────────────
    if clock is EventClock.announcement_date and rule in _INTRADAY_RULES:
        errors.append(CompileError(
            "E003", "entry.rule",
            f"entry rule {rule.value!r} needs a timestamp, but clock "
            f"'announcement_date' is date-granularity, and announced_at trails first "
            f"dissemination by a median 0.9h (6.2h via nse_xbrl, measured 2026-09-12), "
            f"so it cannot time an intraday entry. Use clock 'exchange_first_seen'.",
        ))
    if clock is EventClock.announcement_date and spec.entry.delay_minutes > 0:
        errors.append(CompileError(
            "E003", "entry.delay_minutes",
            "a minute-level delay is meaningless on a date-granularity clock",
        ))
    if clock is EventClock.announcement_date and "clock_lags_dissemination" in ctx.integrity_flags:
        # The stored clock is LATER than dissemination, so a next-session entry
        # is conservative (it can lag, never peek). Record the bias; don't block.
        bias.append("ts_late")
        constraints.append("entry_timing_conservative")

    # ── E004: entry must not be able to execute before the information ───
    if rule is EntryRule.same_session_close_if_before:
        if spec.entry.before_time is None:
            errors.append(CompileError(
                "E004", "entry.before_time",
                "same_session_close_if_before requires before_time (IST cutoff)",
            ))
        if clock is not EventClock.exchange_first_seen:
            errors.append(CompileError(
                "E004", "entry.rule",
                "a same-session entry needs the exchange dissemination timestamp "
                "('exchange_first_seen'); on any other clock the close may precede the filing",
            ))

    # ── E005: horizon must match the entry rule and the available data ───
    max_hold = max(spec.exit_.holding_days)
    if spec.horizon is Horizon.intraday:
        if rule not in _INTRADAY_RULES:
            errors.append(CompileError(
                "E005", "horizon",
                "intraday horizon requires an intraday entry rule",
            ))
        if max_hold > 1 or not spec.exit_.squareoff_intraday:
            errors.append(CompileError(
                "E005", "exit",
                "intraday horizon requires squareoff_intraday and holding_days <= 1",
            ))
        if any(f.data_tier == "H" for f in features.values()) and not any(
            f.data_tier == "F" for f in features.values()
        ):
            errors.append(CompileError(
                "E005", "horizon",
                "intraday research needs minute-level marks, which only exist forward "
                "(paper_signal_marks from 2026-08-13) — no historical intraday series exists",
            ))
    elif spec.horizon is Horizon.swing and max_hold > 20:
        errors.append(CompileError("E005", "horizon", "swing horizon caps holding_days at 20"))
    elif spec.horizon is Horizon.medium and max_hold < 20:
        errors.append(CompileError("E005", "horizon", "medium horizon needs holding_days >= 20"))

    # ── Data tier drives the whole validation regime ─────────────────────
    data_tier = "F" if any(f.data_tier == "F" for f in features.values()) else "H"
    study_start = max(
        [f.history_start for f in features.values()] or [date(2017, 11, 28)]
    )

    if data_tier == "F":
        if spec.validation.holdout is HoldoutPolicy.sealed_h_v1:
            warnings.append(
                "Tier F (feature history < 3y): a sealed historical holdout is not "
                "meaningful. Normalized to forward_only — validation is the SHADOW record."
            )
        holdout = HoldoutPolicy.forward_only.value
        constraints.append("shadow_required_before_paper")
        constraints.append("no_historical_llm_replay")
    else:
        holdout = HoldoutPolicy.sealed_h_v1.value
        constraints.append(f"in_sample_before:{ctx.sealed_from.isoformat()}")

    # Survivorship is unavoidable today: universe_membership is empty and only
    # 25 of 3,923 symbols ever stop pricing (verified).
    bias.append("survivorship")
    # Closes are unadjusted, so corporate-action windows are dropped in v1.
    constraints.append("exclude_ca_windows")

    # ── E006: enough events and cohorts to say anything ──────────────────
    counts: Optional[EventCounts] = None
    if ctx.event_counter is not None:
        counts = ctx.event_counter(spec)
        min_events = spec.statistics.min_events if data_tier == "H" else 100
        min_cohorts = spec.statistics.min_cohorts if data_tier == "H" else 4
        if counts.events < min_events:
            errors.append(CompileError(
                "E006", "statistics.min_events",
                f"{counts.events} events available pre-holdout, need {min_events}",
            ))
        if counts.cohorts < min_cohorts:
            errors.append(CompileError(
                "E006", "statistics.min_cohorts",
                f"{counts.cohorts} cohorts available, need {min_cohorts}",
            ))
    else:
        deferred.append("E006")

    # ── E007: controls must be measured and well populated ───────────────
    for control in spec.controls:
        ref = control if "@" in control else None
        feat = get_feature(ref) if ref else _latest_by_name(control)
        if feat is None:
            errors.append(CompileError(
                "E007", "controls", f"control {control!r} is not a registered feature",
            ))
            continue
        if not feat.control_eligible:
            errors.append(CompileError(
                "E007", "controls", f"feature {feat.ref!r} is not eligible as a control",
            ))
            continue
        if feat.coverage is None:
            errors.append(CompileError(
                "E007", "controls",
                f"control {feat.ref!r} has UNMEASURED coverage — measure it before "
                f"conditioning on it",
            ))
        elif feat.coverage < CONTROL_COVERAGE_FLOOR:
            errors.append(CompileError(
                "E007", "controls",
                f"control {feat.ref!r} coverage {feat.coverage:.1%} is below the "
                f"{CONTROL_COVERAGE_FLOOR:.0%} floor",
            ))

    # ── E008: operator/feature-kind compatibility ────────────────────────
    for i, filt in enumerate(spec.event.filters):
        feat = features.get(filt.feature)
        if feat is None:
            continue
        op = filt.op.value
        if op in _NUMERIC_OPS and feat.value_kind not in ("float", "int"):
            errors.append(CompileError(
                "E008", f"event.filters[{i}].op",
                f"operator {op!r} needs a numeric feature; {feat.ref!r} is {feat.value_kind}",
            ))
        if op in _CATEGORICAL_OPS and feat.value_kind == "float":
            errors.append(CompileError(
                "E008", f"event.filters[{i}].op",
                f"operator {op!r} on a float feature is ill-defined; use a threshold operator",
            ))
        if op in ("decile_ge", "decile_le"):
            try:
                dec = int(filt.value)
            except (TypeError, ValueError):
                dec = -1
            if not 1 <= dec <= 10:
                errors.append(CompileError(
                    "E008", f"event.filters[{i}].value", "decile must be an integer 1..10",
                ))
            if filt.reference is None:
                errors.append(CompileError(
                    "E008", f"event.filters[{i}].reference",
                    "ranking operators need an explicit reference window (e.g. trailing_365d)",
                ))

    # ── E009: explicit dates may not reach into sealed or embargoed data ─
    for i, filt in enumerate(spec.event.filters):
        d = _as_date(filt.value)
        if d is not None and d >= ctx.sealed_from and data_tier == "H":
            errors.append(CompileError(
                "E009", f"event.filters[{i}].value",
                f"{d.isoformat()} is inside the sealed holdout (from "
                f"{ctx.sealed_from.isoformat()}) — the gate engine owns that window",
            ))
    for emb in ctx.embargoes:
        if not emb.sources or spec.event.source.value in emb.sources:
            constraints.append(f"exclude_window:{emb.start.isoformat()}..{emb.end.isoformat()}")
            warnings.append(
                f"embargo active ({emb.reason}): events {emb.start}..{emb.end} are excluded "
                f"from every test and from the report"
            )

    # ── E011: shorts must be executable in this market ───────────────────
    if spec.signal.direction is Direction.short:
        if not spec.exit_.squareoff_intraday and spec.universe.base not in _FNO_BASES:
            errors.append(CompileError(
                "E011", "signal.direction",
                "overnight shorts require the F&O lot universe (base='fno_lot_universe'); "
                "cash shorts must set exit.squareoff_intraday",
            ))

    # ── E012: trials are a budgeted resource ─────────────────────────────
    trials = spec.sweep_points()
    if trials > ctx.family_trials_remaining:
        errors.append(CompileError(
            "E012", "sweep",
            f"{trials} trials requested, family has {ctx.family_trials_remaining} left",
        ))

    # ── E013: keep non-companies out of the universe ─────────────────────
    missing = {"fund_scheme", "suspended"} - set(spec.universe.exclude)
    if missing:
        errors.append(CompileError(
            "E013", "universe.exclude",
            f"must exclude {sorted(missing)} — 243 'companies' rows are MF schemes "
            f"and suspended names produce untradeable fills",
        ))

    # ── E014: the benchmark must cover the study window ──────────────────
    if spec.benchmark is Benchmark.mid150bees and study_start < MID150BEES_START:
        errors.append(CompileError(
            "E014", "benchmark",
            f"MID150BEES prices start {MID150BEES_START.isoformat()}, study starts "
            f"{study_start.isoformat()}",
        ))

    # ── E010: identical mechanics already exist ──────────────────────────
    shash = spec_hash(spec)
    duplicate_of = ctx.known_spec_hashes.get(shash)
    if duplicate_of is not None:
        errors.append(CompileError(
            "E010", "spec", f"identical mechanics already run as {duplicate_of}",
        ))

    if errors:
        return CompileResult(
            ok=False,
            errors=tuple(errors),
            warnings=tuple(warnings),
            duplicate_of=duplicate_of,
            checks_deferred=tuple(deferred),
        )

    plan = ExperimentPlan(
        spec_hash=shash,
        data_tier=data_tier,
        trial_count=trials,
        study_start=study_start,
        holdout=holdout,
        feature_versions={ref: f.version for ref, f in features.items()},
        bias_flags=tuple(dict.fromkeys(bias)),
        constraints=tuple(dict.fromkeys(constraints)),
        counts=counts,
    )
    return CompileResult(
        ok=True, warnings=tuple(warnings), plan=plan, checks_deferred=tuple(deferred),
    )


def _latest_by_name(name: str):
    from bharat_terminal.factory.registry.features import REGISTRY

    candidates = [f for ref, f in REGISTRY.items() if f.name == name]
    if not candidates:
        return None
    return sorted(candidates, key=lambda f: f.version)[-1]


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
