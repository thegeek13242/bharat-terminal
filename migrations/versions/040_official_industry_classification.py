"""Official SEBI/AMFI industry classification on companies.

`companies.sector_nse` was filled for only 750 of 3,998 rows (18.8%) and
`industry_nse` for none. Verified 2026-08-27: those 750 came from NSE's
index constituent CSVs, which cover exactly 752 symbols — that source is
exhausted and can never reach the BSE-only tail (~1,631 companies), which
is precisely where the direction-signals thesis says the edge lives.

BSE's own company-page XHR (`ComHeadernew`) serves the full official
4-level tree per scrip for the entire BSE universe:
    Sector       -> macro_sector      e.g. "Commodities"
    IndustryNew  -> sector            e.g. "Construction Materials"
    IGroup       -> industry          e.g. "Cement & Cement Products"
    ISubGroup    -> basic_industry    e.g. "Cement & Cement Products"

Additive and reversible: every column is nullable, the legacy
sector_nse/industry_nse columns are untouched, and nothing reads the new
columns until the E-series peer read-across estimator does.

Revision ID: 040
Revises: 039
Create Date: 2026-08-27 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None

_COLS = [
    ("macro_sector", sa.String(length=80)),
    ("sector", sa.String(length=120)),
    ("industry", sa.String(length=160)),
    ("basic_industry", sa.String(length=160)),
    ("sector_source", sa.String(length=40)),
    ("sector_updated_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("companies", sa.Column(name, type_, nullable=True))
    # Peer read-across groups by industry; the dossier/E3 paths filter by
    # sector. Partial indexes keep them small while the backfill runs.
    # NAMED `_sebi_` deliberately: a legacy `ix_companies_sector` already
    # indexes the old sector_nse column, and the two taxonomies must stay
    # visibly distinct.
    op.create_index("ix_companies_sebi_industry", "companies", ["industry"],
                    postgresql_where=sa.text("industry IS NOT NULL"))
    op.create_index("ix_companies_sebi_sector", "companies", ["sector"],
                    postgresql_where=sa.text("sector IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_companies_sebi_sector", table_name="companies")
    op.drop_index("ix_companies_sebi_industry", table_name="companies")
    for name, _ in reversed(_COLS):
        op.drop_column("companies", name)
