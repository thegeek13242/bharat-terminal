"""Export production tables to immutable, hashable snapshot files.

v1 writes **gzipped CSV using only the standard library**, so a snapshot can be
produced today from the existing images with no new dependency and no image
rebuild. DuckDB reads CSV natively, so the fast query path can arrive later
without changing the manifest, the integrity gates or any recorded hash.

Two properties matter more than speed here:

* **Determinism.** Rows are exported in an explicit ORDER BY and gzip is
  written with `mtime=0`, so the same data produces byte-identical files and
  therefore the same sha256. Without that, `result_hash` reproducibility (E6)
  would be untestable.
* **Read-only.** Every statement is a SELECT wrapped in COPY ... TO STDOUT.
  Nothing here can write to production.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from bharat_terminal.factory.snapshot.manifest import Manifest, TableSnapshot

# CLAUDE.md's sanctioned database access path.
DEFAULT_CONTAINER = "bharat-postgres"
DEFAULT_USER = "bharat"
DEFAULT_DB = "bharat_terminal"

GZIP_MTIME = 0          # deterministic gzip header
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class TableSpec:
    """One exported table: a read-only SELECT plus how to date it."""

    name: str
    sql: str                              # SELECT ..., no trailing semicolon
    clock_column: Optional[str] = None    # column used for the freshness gate
    note: str = ""

    def max_date_sql(self) -> Optional[str]:
        if not self.clock_column:
            return None
        return f"SELECT max({self.clock_column})::date FROM ({self.sql}) s"


# The Day-1 set: enough to run the incumbent PEAD recipe end to end.
# ORDER BY is part of the contract, not decoration - it makes the file
# deterministic.
SPECS_V1: Tuple[TableSpec, ...] = (
    TableSpec(
        name="price_points",
        sql=("SELECT symbol, date, open, high, low, close, volume "
             "FROM price_points WHERE close > 0 ORDER BY symbol, date"),
        clock_column="date",
        note="unadjusted closes; corporate-action windows are excluded downstream",
    ),
    TableSpec(
        name="earnings_surprises",
        # Every added column is joined POINT-IN-TIME. An expectation computed
        # after the announcement, or a shareholding filing not yet public, is
        # not evidence -- it is the answer leaking backwards. Each LATERAL
        # below therefore carries an explicit "<= announced_at" bound, and the
        # coverage cost of those bounds is real: expectation_surprise_gbm is
        # 100% of the expectations table but only 50.3% of EVENTS, and
        # guidance_change is 100% of its table but 15.8% of events (measured
        # 2026-09-17). The event-grain number is the one that decides whether
        # a study has enough sample.
        #
        # Values are exported RAW and ranked downstream by
        # backtest.ranking.trailing_deciles. Ranking in SQL would mean
        # NTILE over the whole history, which ranks every event against events
        # that had not happened yet.
        sql=("SELECT e.symbol, e.period_end, e.announced_at, e.sue_composite, "
             "e.sue_eps, e.sue_revenue, e.percentile, e.decile, e.liquidity_pass, "
             "e.median_traded_value_cr, e.study_version, "
             "c.macro_sector AS sector_bse, "
             "CASE WHEN x.expected_value IS NULL OR x.expected_value = 0 THEN NULL "
             "     ELSE (e.revenue_cr - x.expected_value) / abs(x.expected_value) "
             "END AS expectation_surprise_gbm, "
             "g.change_verdict AS guidance_change, "
             "CASE WHEN s.promoter_pct IS NULL OR sp.promoter_pct IS NULL THEN NULL "
             "     ELSE s.promoter_pct - sp.promoter_pct END AS shp_promoter_delta "
             "FROM earnings_surprises e "
             "LEFT JOIN companies c ON c.symbol = e.symbol "
             "LEFT JOIN LATERAL ("
             "  SELECT expected_value FROM expectations x "
             "   WHERE x.symbol = e.symbol AND x.period_end = e.period_end "
             "     AND x.metric = 'revenue' AND x.estimator = 'gbm' "
             "     AND x.inputs_max_ts <= e.announced_at "
             "   ORDER BY x.inputs_max_ts DESC LIMIT 1) x ON TRUE "
             "LEFT JOIN LATERAL ("
             "  SELECT change_verdict FROM transcript_guidance g "
             "   WHERE g.symbol = e.symbol AND g.announced_at <= e.announced_at "
             "   ORDER BY g.announced_at DESC LIMIT 1) g ON TRUE "
             # SEBI allows 21 days to file a shareholding pattern; 45 is margin.
             "LEFT JOIN LATERAL ("
             "  SELECT promoter_pct, quarter_end FROM shareholding_patterns s "
             "   WHERE s.symbol = e.symbol "
             "     AND s.quarter_end + INTERVAL '45 days' <= e.announced_at "
             "   ORDER BY s.quarter_end DESC LIMIT 1) s ON TRUE "
             "LEFT JOIN LATERAL ("
             "  SELECT promoter_pct FROM shareholding_patterns p "
             "   WHERE p.symbol = e.symbol AND p.quarter_end < s.quarter_end "
             "   ORDER BY p.quarter_end DESC LIMIT 1) sp ON TRUE "
             "ORDER BY e.symbol, e.period_end"),
        clock_column="announced_at",
        note="announced_at carries the IST-as-UTC offset on a subset of rows; "
             "added feature columns are PIT-joined and ranked downstream",
    ),
    TableSpec(
        name="corporate_actions",
        sql=("SELECT symbol, ex_date, action_type, value FROM corporate_actions "
             "WHERE ex_date > DATE '2000-01-01' ORDER BY symbol, ex_date"),
        clock_column="ex_date",
        note="one corrupt source row (year 0024) filtered by the date floor",
    ),
    TableSpec(
        name="companies",
        sql=("SELECT symbol, company_name, company_type, is_fund_scheme, sector_nse "
             "FROM companies ORDER BY symbol"),
        note="universe exclusions: fund schemes and suspended names",
    ),
    TableSpec(
        name="result_first_seen",
        sql=("SELECT n.id AS news_id, n.timestamp_utc, n.source, n.subcategory, "
             "n.symbols_mentioned::text AS symbols "
             "FROM news_items n WHERE n.category = 'RESULT' "
             "ORDER BY n.timestamp_utc, n.id"),
        clock_column="timestamp_utc",
        note="independent sighting used by the timestamp-offset integrity gate",
    ),
)


@dataclass(frozen=True)
class ExportResult:
    rows: int
    bytes: int
    sha256: str
    path: str


def psql_argv(
    sql: str,
    container: str = DEFAULT_CONTAINER,
    user: str = DEFAULT_USER,
    db: str = DEFAULT_DB,
    tuples_only: bool = False,
) -> List[str]:
    """argv for a read-only psql call inside the database container."""
    argv = ["docker", "exec", "-i", container, "psql", "-U", user, "-d", db,
            "-v", "ON_ERROR_STOP=1"]
    if tuples_only:
        argv += ["-At"]
    argv += ["-c", sql]
    return argv


def copy_sql(spec: TableSpec) -> str:
    """COPY (SELECT ...) TO STDOUT — a read; it cannot mutate anything."""
    return f"COPY ({spec.sql}) TO STDOUT WITH (FORMAT csv, HEADER true)"


def _finish(path: str, raw_rows: int) -> ExportResult:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return ExportResult(rows=max(0, raw_rows - 1), bytes=size, sha256=h.hexdigest(), path=path)


def write_csv_gz(path: str, header: Sequence[str], rows: Iterable[Sequence[object]]) -> ExportResult:
    """Write rows as deterministic gzipped CSV (used by tests and Python sources)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    # filename="" as well as mtime=0: GzipFile otherwise takes the name from the
    # file object and writes it into the header, which would make a snapshot's
    # hash depend on where it was written rather than on what it contains.
    with open(path, "wb") as raw, gzip.GzipFile(
        filename="", fileobj=raw, mode="wb", mtime=GZIP_MTIME
    ) as gz:
        gz.write((",".join(str(h) for h in header) + "\n").encode())
        n += 1
        for row in rows:
            gz.write((",".join("" if v is None else str(v) for v in row) + "\n").encode())
            n += 1
    return _finish(path, n)


def stream_psql_csv_gz(path: str, argv: Sequence[str], timeout_s: int = 1800) -> ExportResult:
    """Stream a COPY ... TO STDOUT straight into a deterministic .csv.gz."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    with open(path, "wb") as raw, gzip.GzipFile(
        filename="", fileobj=raw, mode="wb", mtime=GZIP_MTIME
    ) as gz:
        proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
                gz.write(chunk)
                n += chunk.count(b"\n")
            _, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"psql export failed ({proc.returncode}): "
                               f"{(err or b'').decode()[:400]}")
    return _finish(path, n)


def query_scalar(sql: str, **kw) -> Optional[str]:
    """Single-value read (used for max dates)."""
    out = subprocess.run(psql_argv(sql, tuples_only=True, **kw),
                         capture_output=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"psql query failed: {out.stderr.decode()[:400]}")
    value = out.stdout.decode().strip()
    return value or None


def export_table(spec: TableSpec, out_dir: str, **kw) -> Tuple[TableSnapshot, ExportResult]:
    path = os.path.join(out_dir, f"{spec.name}.csv.gz")
    result = stream_psql_csv_gz(path, psql_argv(copy_sql(spec), **kw))
    max_date = None
    mds = spec.max_date_sql()
    if mds:
        max_date = query_scalar(mds, **kw)
    return TableSnapshot(
        table=spec.name, rows=result.rows, sha256=result.sha256,
        file=os.path.basename(path), max_date=max_date, bytes=result.bytes,
        note=spec.note,
    ), result


def export_snapshot(
    root: str,
    sealed_from: date,
    specs: Sequence[TableSpec] = SPECS_V1,
    sequence: int = 1,
    code_commit: Optional[str] = None,
    day: Optional[date] = None,
    embargoes: Optional[List[Dict[str, str]]] = None,
    **kw,
) -> Manifest:
    """Export every table and write the manifest. Returns the manifest."""
    from bharat_terminal.factory.snapshot.manifest import dataset_version_for

    day = day or datetime.now(timezone.utc).date()
    version = dataset_version_for(day, sequence)
    out_dir = os.path.join(root, version)
    os.makedirs(out_dir, exist_ok=True)

    tables: Dict[str, TableSnapshot] = {}
    for spec in specs:
        snap, _ = export_table(spec, out_dir, **kw)
        tables[spec.name] = snap

    manifest = Manifest(
        dataset_version=version,
        created_at=datetime.now(timezone.utc),
        sealed_from=sealed_from,
        tables=tables,
        embargoes=embargoes or [],
        code_commit=code_commit,
    )
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        fh.write(manifest.to_json())
    return manifest
