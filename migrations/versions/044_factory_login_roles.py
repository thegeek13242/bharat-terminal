"""Login roles for the factory tools API.

WHY THIS EXISTS
---------------
Migration 043 created three capability-separated group roles and carefully
revoked the dangerous grants:

    REVOKE INSERT ON factory.gate_decisions           FROM factory_writer
    REVOKE INSERT, UPDATE ON factory.hypothesis_status FROM factory_writer
    GRANT  INSERT ON factory.gate_decisions           TO   factory_gate

Verified on 2026-09-14, that separation protected nothing:

    pg_auth_members for factory_reader|factory_writer|factory_gate  ->  0 rows
    login roles in this database                                    ->  bharat
                                                                        (rolsuper = t)

All three are NOLOGIN and no login user was a member of any of them, so nothing
could connect as those roles or SET ROLE to them. Every process connected as
`bharat`, a superuser -- and superusers bypass every GRANT and REVOKE. The
promotion boundary existed in the migration text and nowhere else.

This migration creates the two login roles the tools API needs, so the grants in
043 start meaning something:

    factory_api      member of factory_writer  -> factory.* read + INSERT,
                                                  but NOT gate_decisions and
                                                  NOT hypothesis_status
    factory_api_ro   member of factory_reader  -> adds SELECT on the production
                                                  tables; used by exactly one
                                                  tool, get_paper_feedback

Both INHERIT (the default), so the tools API needs no SET ROLE per connection.

DELIBERATELY NOT CREATED
------------------------
A login member of `factory_gate`. Nothing writes gate decisions live yet --
`LedgerWriter` is dry-run by default and raises without an injected executor --
and the role that can promote should arrive with the workflow that needs it,
not sit unused ahead of it.

PASSWORDS ARE NOT SET HERE
--------------------------
043 states the principle and this migration keeps it: passwords never live in a
migration, a repository, or a log. Both roles are created able to log in but
WITHOUT a password.

Under this cluster's `pg_hba.conf`, host connections require `scram-sha-256`, so
until an operator sets a password these roles cannot authenticate from any
container -- fail-closed by design.

Be aware of the honest caveat: the cluster also `trust`s `local` and
`127.0.0.1`. Anything with a shell inside the postgres container can already
connect as ANY role without a password, superuser `bharat` included. That is
pre-existing cluster configuration which this migration does not change and does
not claim to fix; it means this boundary is only ever as strong as shell access
to that container.

POST-APPLY (operator, once, per environment)
--------------------------------------------
Use psql's \\password, which hashes client-side and sends only the hash -- so the
plaintext never reaches shell history, the server log, or this repository. Do
NOT use `ALTER ROLE ... PASSWORD '...'` on a command line.

    docker exec -it bharat-postgres psql -U bharat -d bharat_terminal
    \\password factory_api
    \\password factory_api_ro

Then store both in Secrets Manager (`factory/db-api-password`,
`factory/db-api-ro-password`) alongside `factory/tools-api-token`, and build the
two DSNs from there. They must NOT go in `.env` beside the production
credentials.

VERIFY AFTER APPLYING
---------------------
    -- membership exists
    SELECT r.rolname AS grp, m.rolname AS member
      FROM pg_auth_members am
      JOIN pg_roles r ON r.oid = am.roleid
      JOIN pg_roles m ON m.oid = am.member
     WHERE r.rolname LIKE 'factory_%';

    -- neither login role is a superuser
    SELECT rolname, rolsuper, rolcanlogin, rolconnlimit
      FROM pg_roles WHERE rolname LIKE 'factory_api%';

The acceptance test for the whole idea (design doc section 0, P4): connected as
factory_api, this must raise `permission denied`, not succeed --

    INSERT INTO factory.gate_decisions
        (hypothesis_id, version, from_status, to_status, gate_version,
         inputs, rule_results, decided_by)
    VALUES ('H-000001', 1, 'SIMULATED', 'ROBUST', 'v1', '{}', '[]', 'probe');

KNOWN BLOCKER BEFORE THIS CAN BE APPLIED
-----------------------------------------
`alembic current` inside bharat-kb reports "Can't locate revision identified by
'043'" while the alembic_version table holds '043'. The migration container
ships a stale copy of migrations/. Rebuild that image first, or this revision
cannot be reached either.

NOT APPLIED automatically. Applying this needs founder approval.

Revision ID: 044
Revises: 043
Create Date: 2026-09-14
"""
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

# Connection limits are deliberate. This host has a documented history of
# connection exhaustion (a weekly sweep leaked engines to 72/100 and collapsed
# the papertrade pool), so a new consumer gets a bounded share rather than an
# open tap.
API_CONN_LIMIT = 10
API_RO_CONN_LIMIT = 5


def upgrade() -> None:
    # Created without PASSWORD on purpose -- see the module docstring. Every
    # attribute is spelled out rather than left to defaults: in a migration whose
    # entire purpose is a privilege boundary, the reader should not have to know
    # what CREATE ROLE defaults to.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_api') THEN
                CREATE ROLE factory_api LOGIN
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                    CONNECTION LIMIT {API_CONN_LIMIT};
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factory_api_ro') THEN
                CREATE ROLE factory_api_ro LOGIN
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                    CONNECTION LIMIT {API_RO_CONN_LIMIT};
            END IF;
        END $$;
        """
    )

    # The whole point of the migration: make 043's group roles reachable.
    op.execute("GRANT factory_writer TO factory_api")
    op.execute("GRANT factory_reader TO factory_api_ro")

    # factory_api is NOT granted factory_reader. That is what keeps the ten
    # ledger-only tools structurally unable to read production tables, and
    # leaves get_paper_feedback as the single visibly-privileged route.
    #
    # Note on what is NOT attempted here: revoking schema `public` from these
    # roles would be theatre. USAGE on public comes from the PUBLIC pseudo-role,
    # and a per-role REVOKE does not remove it. The real protection is that
    # factory_writer holds no grants on any table in public, so factory_api can
    # enter the schema and read nothing in it.


def downgrade() -> None:
    op.execute("REVOKE factory_writer FROM factory_api")
    op.execute("REVOKE factory_reader FROM factory_api_ro")
    # Roles are intentionally NOT dropped, matching 043. DROP ROLE fails if the
    # role owns objects or holds grants in another database, and a half-finished
    # downgrade is worse than a leftover role that can no longer reach anything.
