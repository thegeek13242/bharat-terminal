"""Persist the exchange's own filing SUBCATEGORY.

BSE's announcement API returns SUBCATNAME alongside CATEGORYNAME (57
distinct pairs observed), but the adapter stored only the coarse category
and discarded the precise tag on every filing. The subcategory is exchange
ground truth for event classes we currently infer with keywords + an LLM
taxonomy, e.g.:

    Company Update :: Award of Order / Receipt of Order   (orderwin_long)
    Company Update :: Earnings Call Transcript
    Company Update :: Investor Presentation   <- the results-day decks that
        contaminated pre-results "business update" detection on 2026-08-27
    Company Update :: Credit Rating / Acquisition
    Result :: Financial Results

Additive and reversible: nullable, so an older ingestion image keeps
writing rows (NULL) without erroring. Historical rows stay NULL — the BSE
archive walker can backfill them later if it earns its keep.

Revision ID: 041
Revises: 040
Create Date: 2026-08-27 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_items",
                  sa.Column("subcategory", sa.String(length=120), nullable=True))
    op.create_index("ix_news_items_subcategory", "news_items", ["subcategory"],
                    postgresql_where=sa.text("subcategory IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_news_items_subcategory", table_name="news_items")
    op.drop_column("news_items", "subcategory")
