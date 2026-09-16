"""A login role for the gate: the only thing that may record a verdict.

WHY THIS EXISTS
Migration 043 made `factory_gate` the sole role able to INSERT
factory.gate_decisions and INSERT/UPDATE factory.hypothesis_status, and 044
gave the tools API a login role that is deliberately REVOKEd on both. That
left a gap nobody could cross: verified 2026-09-16,

    factory_gate login members  ->  0

so nothing in the system could write a verdict. The promotion ladder was
physically inert - an agent could propose and a backtester could run forever
and no gate_decision could ever exist. The two hypotheses the Scientist wrote
on 2026-09-16 sat un-judged for exactly this reason.

WHAT THIS DOES NOT DO
It does not give the gate role to an agent. `factory_api` (the tools API, and
therefore every agent) stays REVOKEd on gate_decisions and hypothesis_status.
The orchestrator runs the gate; agents never touch it. That split is the
difference between "the agent proposes and a deterministic system decides" and
"the agent decides", and it is the whole premise of the factory.

    factory_api       proposes, reads, writes research      NOT verdicts
    factory_api_gate  records verdicts and holdout access   NOT research
    factory_api_ro    reads production for paper feedback   writes nothing

Three roles, three jobs, none of them able to do another's.

PASSWORDS ARE NOT SET HERE
Same principle as 043 and 044: passwords never live in a migration, a
repository, or a log. The role is created able to log in but WITHOUT one, so
under this cluster's scram-sha-256 host auth it cannot authenticate from any
container until an operator sets one. Fail-closed by design.

POST-APPLY (operator, once)
Use psql's \\password, which hashes client-side and sends only the hash, so the
plaintext never reaches shell history or the server log:

    docker exec -it bharat-postgres psql -U bharat -d bharat_terminal
    \\password factory_api_gate

Then add the DSN to .env as FACTORY_GATE_DB_URL. It must NOT be the same
connection string the tools API uses - the entire point is that the process
running the gate is a different principal from the process serving agents.

VERIFY AFTER APPLYING
    -- membership exists, and the role is not a superuser
    SELECT rolname, rolsuper, rolcanlogin, rolconnlimit
      FROM pg_roles WHERE rolname LIKE 'factory_api%';

The acceptance test is a pair, and both halves matter:
  * as factory_api_gate,  INSERT INTO factory.gate_decisions  must SUCCEED
  * as factory_api,       the same INSERT must still be DENIED

The second is the one worth running twice. A migration that grants the gate
role and quietly widens the tools role would look identical from the first
test alone.

NOT APPLIED automatically. Applying this needs founder approval.

Revision ID: 045
Revises: 044
Create Date: 2026-09-16
"""
from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None

# The gate runs in one orchestrator process, not a pool of workers, so it needs
# far fewer connections than the tools API. This host has a documented history
# of connection exhaustion; a new consumer gets a bounded share.
GATE_CONN_LIMIT = 4


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_api_gate') THEN
                CREATE ROLE factory_api_gate LOGIN
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                    CONNECTION LIMIT {GATE_CONN_LIMIT};
            END IF;
        END $$;
        """
    )
    op.execute("GRANT factory_gate TO factory_api_gate")

    # Deliberately NOT granted factory_writer. The gate records decisions; it
    # does not author research. Keeping the two disjoint means a bug in the
    # gate path cannot write a hypothesis, and a bug in the research path
    # cannot write a verdict.


def downgrade() -> None:
    op.execute("REVOKE factory_gate FROM factory_api_gate")
    # The role is intentionally NOT dropped, matching 043 and 044: DROP ROLE
    # fails if the role owns objects or holds grants elsewhere, and a
    # half-finished downgrade is worse than a leftover role that can no longer
    # reach anything.
