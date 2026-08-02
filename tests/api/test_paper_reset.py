"""Paper-ledger reset: dry-run makes zero writes; execute archives every
table with the batch stamp, clears the live tables, and audits — replacing
the undocumented manual wipes that left unstamped exit_reason='reset' rows."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from bharat_terminal.api.routes import admin

ADMIN = {"id": "1", "email": "founder@x.in", "role": "admin",
         "disabled_features": []}


def _count_responder(fakes, counts):
    def respond(sql, params):
        for table, n in counts.items():
            if f"FROM {table}" in sql:
                return fakes.Rows(scalar=n)
        raise AssertionError(sql)
    return respond


def _session(fakes, counts, archived_rowcounts=None):
    archived_rowcounts = archived_rowcounts or {}
    return fakes.FakeSession([
        ("INSERT INTO admin_audit_log", fakes.Rows()),
        ("INSERT INTO paper_trades_archive",
         fakes.Rows(rowcount=archived_rowcounts.get("paper_trades", 0))),
        ("INSERT INTO paper_equity_snapshots_archive",
         fakes.Rows(rowcount=archived_rowcounts.get("paper_equity_snapshots", 0))),
        ("INSERT INTO paper_signal_marks_archive",
         fakes.Rows(rowcount=archived_rowcounts.get("paper_signal_marks", 0))),
        ("DELETE FROM", fakes.Rows()),
        ("GROUP BY status",
         fakes.Rows(rows=[{"status": "OPEN", "n": 3},
                          {"status": "CLOSED", "n": 9}])),
        ("SELECT count(*)", _count_responder(fakes, counts)),
    ])


COUNTS = {"paper_trades": 12, "paper_equity_snapshots": 40,
          "paper_signal_marks": 7}


async def test_dry_run_reports_counts_and_writes_nothing(monkeypatch, fakes):
    s = _session(fakes, COUNTS)
    monkeypatch.setattr(admin, "get_session_factory",
                        lambda: fakes.fake_factory(s))

    out = await admin.paper_reset(
        admin.PaperResetBody(execute=False, reason=""), admin=ADMIN)

    assert out["dry_run"] is True
    assert out["counts"] == COUNTS
    assert out["paper_trades_by_status"] == {"OPEN": 3, "CLOSED": 9}
    assert not s.committed, "dry-run must not commit"
    writes = [sql for sql, _ in s.executed
              if sql.startswith(("INSERT", "DELETE", "UPDATE"))]
    assert writes == [], f"dry-run wrote: {writes}"


async def test_execute_requires_reason(monkeypatch, fakes):
    s = _session(fakes, COUNTS)
    monkeypatch.setattr(admin, "get_session_factory",
                        lambda: fakes.fake_factory(s))
    with pytest.raises(HTTPException) as e:
        await admin.paper_reset(
            admin.PaperResetBody(execute=True, reason="   "), admin=ADMIN)
    assert e.value.status_code == 422 and e.value.detail == "reason_required"
    assert not s.committed


async def test_execute_archives_deletes_and_audits(monkeypatch, fakes):
    s = _session(fakes, COUNTS, archived_rowcounts=COUNTS)
    monkeypatch.setattr(admin, "get_session_factory",
                        lambda: fakes.fake_factory(s))

    out = await admin.paper_reset(
        admin.PaperResetBody(execute=True, reason="pilot v2 restart"),
        admin=ADMIN)

    assert out["dry_run"] is False
    assert out["archived"] == COUNTS
    assert out["batch_id"].startswith("reset-") and "founder" in out["batch_id"]
    assert s.committed

    sqls = [sql for sql, _ in s.executed]
    for t in ("paper_trades", "paper_equity_snapshots", "paper_signal_marks"):
        insert_idx = next(i for i, q in enumerate(sqls)
                          if f"INSERT INTO {t}_archive" in q)
        delete_idx = next(i for i, q in enumerate(sqls)
                          if q.startswith(f"DELETE FROM {t}"))
        assert insert_idx < delete_idx, f"{t}: must archive BEFORE deleting"

    audit_sql, audit_params = next(
        (q, p) for q, p in s.executed if "admin_audit_log" in q)
    assert audit_params["action"] == "paper.reset"
    assert "pilot v2 restart" in audit_params["payload"]

    # Batch stamp params flow into every archive INSERT.
    for _, p in [(q, p) for q, p in s.executed if "_archive" in q]:
        assert p["batch_id"] == out["batch_id"]
        assert p["actor"] == "founder@x.in"
