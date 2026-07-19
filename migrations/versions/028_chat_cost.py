"""Per-ask cost on the research-chat audit log (budget enforcement).

cost_usd is computed at ask time from the repo pricing table (thinking
tokens billed as output; web-search per-call fees not yet priced — see
analysis/llm_logger.MODEL_PRICING). The monthly per-user budget is enforced
by summing this column — the audit log doubles as the meter.

Revision ID: 028
Revises: 027
Create Date: 2026-07-19 00:00:00.000000
"""
from alembic import op


revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE research_chat_log ADD COLUMN cost_usd double precision"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE research_chat_log DROP COLUMN IF EXISTS cost_usd"
    )
