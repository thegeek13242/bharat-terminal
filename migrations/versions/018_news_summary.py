"""Grounded filing summaries on news_items.

news_items.summary holds the extractive, numerically-grounded summary of an
exchange-filing PDF attachment (analysis/filing_summary.py). NULL for
non-filing sources, for procedural filings, and whenever the accuracy gate
rejected the model output — a missing summary is by design never an error.

Revision ID: 018
Revises: 017
Create Date: 2026-07-03 00:00:00.000000
"""
from alembic import op


revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE news_items ADD COLUMN summary text")


def downgrade() -> None:
    op.execute("ALTER TABLE news_items DROP COLUMN IF EXISTS summary")
