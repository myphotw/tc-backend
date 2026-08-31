"""Exercise autocommit_block and CREATE INDEX CONCURRENTLY."""

from alembic import op


revision = "pgtest_0002"
down_revision = "pgtest_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration_context = op.get_context()
    with migration_context.autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_test_revision_probe_phase "
            "ON test_revision_probe (phase)"
        )
    op.execute(
        "INSERT INTO test_revision_probe (phase, backend_pid) "
        "VALUES ('autocommit', pg_backend_pid())"
    )


def downgrade() -> None:
    migration_context = op.get_context()
    with migration_context.autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY ix_test_revision_probe_phase")

