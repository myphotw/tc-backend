# PostgreSQL Migration Integration Tests

This suite validates the Alembic framework and baseline safety against real
PostgreSQL 16 behavior. It is a production-baseline gate, not an ordinary unit
test suite.

## Destructive warning

Never point these tests at production, a production clone that must be
preserved, a shared development database, the NAS operational database, or the
application's default `.env` database. Every integration test executes:

```text
DROP SCHEMA IF EXISTS public CASCADE
CREATE SCHEMA public AUTHORIZATION CURRENT_USER
```

before and after the test. The suite does not create or drop the database
itself. The operator must provide a dedicated disposable PostgreSQL 16 database
that can be recreated without recovery.

## Fail-closed safety guard

The suite reads only `TC_POSTGRES_INTEGRATION_URL`. It never falls back to
`TEST_DATABASE_URL`, `DATABASE_URL`, or the application `POSTGRES_*` settings.
All of the following must pass before destructive fixture setup:

1. `TC_POSTGRES_INTEGRATION_URL` is explicitly present and uses PostgreSQL.
2. `TC_POSTGRES_INTEGRATION_CONFIRM` exactly equals
   `TC_BACKEND_DISPOSABLE_POSTGRESQL_16`.
3. The database is not `postgres`, `template0`, or `template1`.
4. Its name contains a separate `test`, `testing`, `integration`, `ci`, or `qa`
   token. This is only a secondary check, not the main authorization.
5. The connected server reports PostgreSQL major version 16 and is not in
   recovery.
6. The connected database name matches the URL and the role has database
   `CREATE` privilege.
7. The separate `tc_test_guard.authorization` table contains exactly one fixed
   authorization token.
8. A test-suite advisory lock is available, preventing two destructive runs
   against the same database.

If the URL is absent, explicitly selected integration tests are skipped. If a
URL is present but any other guard fails, the suite fails rather than skips.

## One-time disposable database authorization

After an operator creates a dedicated disposable database, connect to that
database and create the marker outside `public`:

```sql
CREATE SCHEMA tc_test_guard;
CREATE TABLE tc_test_guard.authorization (
    token text PRIMARY KEY
);
INSERT INTO tc_test_guard.authorization (token)
VALUES ('TC_BACKEND_POSTGRESQL_INTEGRATION_V1');
```

The test role must own, or be able to drop and recreate, `public`. Do not create
this marker in any operational database. The marker is intentionally outside
`public`, so per-test cleanup cannot manufacture or remove its own
authorization.

## Running in a safe environment

PowerShell example for a disposable PostgreSQL 16 database:

```powershell
$env:TC_POSTGRES_INTEGRATION_URL = 'postgresql://TEST_USER:TEST_PASSWORD@TEST_HOST:5432/tc_backend_integration_test'
$env:TC_POSTGRES_INTEGRATION_CONFIRM = 'TC_BACKEND_DISPOSABLE_POSTGRESQL_16'
python -m pytest -o "addopts=" -m postgresql_integration tests/integration/postgresql -v
```

`pytest.ini` excludes `postgresql_integration` by default. The explicit
`-o "addopts=" -m postgresql_integration` combination is required to select it.
The URL environment variable is still required even after explicit selection.

## Isolation and cleanup

- A session-level test-suite advisory lock serializes suites sharing one DB.
- An autouse fixture drops/recreates `public` before and after every test.
- The `tc_test_guard` schema remains untouched.
- Each test constructs its own empty, partial, malformed, or complete legacy
  fixture and does not depend on test order.
- CLI tests replace only `create_migration_engine()` with a NullPool engine for
  the guarded test URL. Production application settings are not applied.
- Interrupted runs may leave objects in `public`; the next authorized run
  clears them before its first test.

## Test-only Alembic graph

Transaction and AUTOCOMMIT behavior use this isolated graph:

```text
tests/integration/postgresql/alembic
  pgtest_0001 -> transactional probe and backend PID
  pgtest_0002 -> autocommit_block + CREATE INDEX CONCURRENTLY
  pgtest_0003 -> intentional transactional failure
```

It uses `public.test_alembic_version`, not `public.alembic_version`. It is never
loaded by production `alembic.ini`, `migrations/env.py`, API startup, workers,
or `scripts/db_migrate.py`. Test revisions must never be copied into
`migrations/versions`.

## Covered PostgreSQL behavior

- lock-owner and revision `pg_backend_pid()` equality;
- session advisory lock survival across revision commits and
  `autocommit_block()`;
- nonblocking `pg_try_advisory_lock` contention and post-unlock reacquisition;
- ordinary revision commit visibility;
- intentional revision failure rollback, version preservation, unlock, and
  primary error preservation;
- primary migration failure preservation when PostgreSQL reports that the
  final unlock no longer owns the lock, with unlock failure retained as a
  secondary diagnostic;
- `CREATE INDEX CONCURRENTLY` execution and valid-index reflection;
- no `alembic_version` or public catalog mutation from `status`, `current`,
  failed `verify`, or `preflight-baseline` on an empty DB;
- complete legacy preflight, baseline stamp, application schema preservation,
  VERSIONED classification, and verify;
- stamp/upgrade refusal for EMPTY, unversioned legacy, partial, fingerprint
  mismatch, malformed, multi-row, and unknown-version states;
- real Inspector handling of varchar length, integer/bigint, boolean,
  timestamptz/timestamp, nullability, PK, UNIQUE, and schema-qualified FK;
- wrapper masking during a real SQLAlchemy/psycopg2 authentication failure when
  password authentication is enabled.

## Connection invalidation

Automatic backend termination is intentionally not included. A reliable test
would require `pg_terminate_backend()` privilege and would deliberately sever
the lock-owning connection. That can be dangerous when privileges or target
selection are wrong.

Before production baseline approval, perform this as a separately reviewed
manual test only on a disposable database that passed the same URL, confirm,
PostgreSQL 16, marker, and suite-lock guards:

1. Record the lock owner's `pg_backend_pid()`.
2. Acquire the TC-Backend migration advisory lock.
3. From a separate administrative test session, terminate exactly that PID.
4. Verify the first connection is invalidated and cannot continue migration.
5. Verify a new connection can acquire the lock, proving PostgreSQL released
   the session-scoped lock.

Do not automate or run this procedure against NAS/production infrastructure.

## Secret-test scope and limitation

The suite substitutes a deliberately wrong password for the already-authorized
test URL, invokes the wrapper, captures stdout/stderr and pytest logging, and
checks the real password, wrong password, encoded/full URLs are absent. The
test skips when the URL has no password or the server accepts the wrong
password (for example, trust/peer authentication).

This verifies captured client-side output for the tested SQLAlchemy, psycopg2,
Alembic, and pytest configuration. It cannot prove that every future external
logger, CI collector, PostgreSQL server log, or uncaught third-party traceback
will mask credentials. Test URLs must therefore always contain disposable
credentials.

## Production baseline gate

Do not baseline an operating database until all automated PostgreSQL integration
tests pass on PostgreSQL 16 and the separately reviewed connection-invalidation
procedure is either completed or explicitly risk-accepted. Afterwards, inspect
the approved production schema fingerprint read-only before authorizing any
production `stamp-baseline` operation.
