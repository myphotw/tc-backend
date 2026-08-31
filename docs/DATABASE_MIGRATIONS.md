# TC-Backend Database Migrations

TC-Backend uses two deliberately separate schema-management paths during the
transition to explicit migrations.

## Ownership boundary

- `app/common/schema_sync.py` continues to manage legacy Table, Column, and
  Index objects that have no explicit owner marker.
- A Table, Column, or Index marked with
  `info=migration_managed_schema_info()` is owned by Alembic.
- A migration-managed Table passes ownership to its child Columns and Indexes.
- A missing Table that contains a migration-managed child is excluded from
  startup `create_all()`. This prevents startup DDL from bypassing ownership.
- Constraints are not covered by the current marker policy. New generated
  expressions, CHECK constraints, and PostgreSQL-specific DDL must therefore be
  written and reviewed manually in a migration revision.

Do not change ownership of an existing object without an explicit revision and
a reviewed deployment plan. Alembic must not generate drops or alterations for
bootstrap-managed legacy schema.

## Startup separation

Alembic never runs from FastAPI startup or from any worker. The existing
`initialize_database()` path remains limited to bootstrap-managed legacy
schema.

Online Alembic execution without a connection supplied by
`scripts/db_migrate.py` fails closed. Operators must not use raw online
`alembic upgrade` or `alembic stamp` commands.

## Baseline

The initial graph is:

```text
base -> 20260831_0001 (baseline, head)
```

The baseline `upgrade()` and `downgrade()` are both no-ops. It does not create,
drop, or alter application schema. The guarded CLI classifies a database before
it permits any baseline or upgrade operation:

- `EMPTY`: the `public` schema contains no tables. This is not a legacy
  baseline candidate and requires the future `bootstrap-empty` path.
- `LEGACY_UNVERSIONED`: TC-Backend application tables exist,
  `public.alembic_version` does not exist, and the complete baseline
  fingerprint matches.
- `VERSIONED`: `public.alembic_version` has the expected structure and exactly
  one known revision, and the application fingerprint still matches.
- `INVALID_AMBIGUOUS`: partial/unrecognized schema, malformed version table,
  zero or multiple revision rows, unknown revision, or any fingerprint
  mismatch. Mutating migration commands fail closed in this state.

Extra columns and unrelated indexes do not by themselves invalidate a
fingerprint. This permits expand-first evolution and avoids rejecting a known
schema because of an index-name difference. The fingerprint instead requires:

- the complete snapshot of application table names registered at the baseline;
- canonical identity, service-link, upload, metadata, and change-event columns;
- MemoryKeeper file-state identity;
- AstroJournal observation and persistent Plate Solve job identity;
- stable primary keys, critical uniqueness rules, column type/length,
  nullability, timezone semantics, and service-to-FileAsset foreign keys.

These checks identify the known TC-Backend production contract without trying
to reproduce every column or index. The immutable selection and its rationale
are defined in `migrations/baseline.py`.

## Operator CLI

The supported entry point is:

```text
python scripts/db_migrate.py <command>
```

Commands:

- `status`: show script head and applied database heads.
- `current`: show the current database revision through Alembic.
- `heads`: show the revision graph head without connecting to the database.
- `history`: show immutable revision history without connecting to the database.
- `preflight-baseline`: perform a read-only state and production fingerprint
  check. Only `LEGACY_UNVERSIONED` produces `BASELINE_READY`.
- `upgrade [revision]`: upgrade, defaulting to `head`.
- `stamp-baseline`: reacquire the migration lock, rerun the full state and
  fingerprint check, and stamp only `20260831_0001`.
- `verify`: verify the single head, baseline root, ownership boundary, and
  applied database head.

`upgrade` and `stamp-baseline` are mutating operations. They acquire the
TC-Backend PostgreSQL session advisory lock before invoking Alembic. Generic
`stamp <revision>` is deliberately not supported; arbitrary stamping can hide
missing migrations or falsely adopt an unknown production schema.

`stamp-baseline` never trusts a previously printed preflight result. It repeats
classification and fingerprint checks while holding the advisory lock, then
stamps only if the current state remains `LEGACY_UNVERSIONED`.

`upgrade` accepts only a valid `VERSIONED` database whose applied revision is
known to the local, single-root revision graph. It refuses `EMPTY`,
`LEGACY_UNVERSIONED`, and `INVALID_AMBIGUOUS`, so upgrade cannot implicitly
adopt an unversioned database as the no-op baseline.

Database credentials come from the existing application settings. They are not
stored in `alembic.ini`, and operator-facing errors mask configured passwords
and URL-shaped credentials.

## Advisory lock policy

- The runner uses a fixed TC-Backend BIGINT lock key.
- It calls `pg_try_advisory_lock` and fails immediately if another migration
  session owns the key.
- Alembic receives the same SQLAlchemy connection that owns the session lock.
- Per-revision commits and future AUTOCOMMIT blocks do not release the lock.
- The runner calls `pg_advisory_unlock` in `finally`.
- Closing the connection provides a final PostgreSQL cleanup guarantee.

Migration execution must never be added to Compose service startup,
`initialize_database()`, API startup, or worker startup.

## Transaction and rollback policy

- Use one transaction per ordinary revision.
- Prefer additive, nullable, expand-first changes.
- Prefer roll-forward recovery in production.
- Do not combine a large data backfill with schema DDL in one transaction.
- Keep old application versions compatible with newly expanded schema until
  the read switch is complete.
- Take and verify a backup/PITR recovery point before production migration.

## Backfill policy

Large backfills will use a separate, versioned, resumable runner in a later
phase. They must use short chunks and checkpoints. A backfill must not invoke
high-level application mutation services if doing so increments metadata
revision, writes metadata history, or floods `common_change_events`.

No backfill runner is included in phase 1.

## PostgreSQL concurrent indexes

`CREATE INDEX CONCURRENTLY` must be isolated in its own future revision and run
outside a transaction using Alembic's AUTOCOMMIT support. Do not mix it with
other DDL or data updates. A failed run must inspect and recover any invalid
index before retrying.

No concurrent-index revision is included in phase 1.

## Fresh empty database policy

Once a model Table contains a migration-managed child, startup `schema_sync`
may exclude the whole missing Table. A future empty-database command is
therefore required with this invariant:

```text
confirm completely empty database
  -> acquire session advisory lock
  -> full Base.metadata.create_all()
  -> verify schema
  -> stamp Alembic head
```

That command must fail closed if any application Table already exists. It must
never be called by startup code. Baseline stamping and empty-database bootstrap
are intentionally separate: a legacy database must preserve existing data and
prove its known schema, while an empty database must construct and verify the
entire current schema. This phase classifies `EMPTY` but does not implement or
authorize `bootstrap-empty`.

## Baseline operator procedure

For a legacy production database, the future approved procedure is:

```text
preflight-baseline
  -> review BASELINE_READY and every reported check
  -> acquire/verify backup and maintenance approval
  -> stamp-baseline (performs the same checks again under advisory lock)
  -> verify
```

If any fingerprint mismatch is reported, stop. Do not use raw Alembic,
manually insert `alembic_version`, weaken the fingerprint, or stamp an arbitrary
revision merely to continue deployment. Investigate whether the database is
partial, drifted, from a different release, or requires an explicit repair
plan.

## Production preflight checklist

Before a future production migration, the operator must confirm:

1. Correct host, database, user, and PostgreSQL version without printing the
   password or full connection URL.
2. Backup/PITR recovery point and available disk space.
3. Exactly one revision head.
4. Current database revision and pending revisions.
5. `preflight-baseline` reports `LEGACY_UNVERSIONED`, fingerprint `MATCH`, and
   `BASELINE_READY` for a baseline adoption, or the DB is already validly
   `VERSIONED` for an upgrade.
6. No other migration session owns the advisory lock.
7. Expected table locks, statement timeout, and maintenance window.
8. Compatibility of the old API/worker images with the expanded schema.
9. Post-migration verification and roll-forward recovery procedure.

## Phase 1 limitations

- No date schema migration exists.
- The baseline fingerprint is implemented but has not been validated against a
  disposable PostgreSQL 16 clone or an approved production schema snapshot.
- No backfill runner exists.
- No `bootstrap-empty` command exists.
- No PostgreSQL integration test has been executed by this change.
- Autogenerate is not an operator command. The environment only contains a
  fail-closed ownership filter for future reviewed use.
