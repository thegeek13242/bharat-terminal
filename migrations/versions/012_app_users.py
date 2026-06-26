"""Add app_users table (invite-only auth allowlist + user record)

Alpha access is invite-only via Google OAuth. A row in this table IS the
invitation: a Google sign-in succeeds only if the verified email matches an
active row here. The same row doubles as the user record (google_sub, name,
picture, last_login_at) once the user has signed in at least once.

Revision ID: 012
Revises: 011
Create Date: 2026-06-26 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("google_sub", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("picture", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("invited_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ux_app_users_email", "app_users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_app_users_email", table_name="app_users")
    op.drop_table("app_users")
