"""PENDING status for off-session paper signals.

Most result filings land after 15:30 IST, when there is no live price to
fill against — filling at the pre-news stale LTP would fabricate an
impossible entry. Signals arriving off-session now queue as PENDING
(qty=0, no entry) and fill at the first live LTP after the next open.

Revision ID: 031
Revises: 030
Create Date: 2026-07-28 00:00:00.000000
"""
from alembic import op


revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE paper_trades DROP CONSTRAINT paper_trades_status_check")
    op.execute(
        "ALTER TABLE paper_trades ADD CONSTRAINT paper_trades_status_check "
        "CHECK (status IN ('PENDING', 'OPEN', 'CLOSED', 'REJECTED'))")


def downgrade() -> None:
    # Fails if PENDING rows exist — resolve them first; never drop data.
    op.execute(
        "ALTER TABLE paper_trades DROP CONSTRAINT paper_trades_status_check")
    op.execute(
        "ALTER TABLE paper_trades ADD CONSTRAINT paper_trades_status_check "
        "CHECK (status IN ('OPEN', 'CLOSED', 'REJECTED'))")
