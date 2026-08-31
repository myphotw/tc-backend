"""Create transactional state and fail so rollback can be verified."""

from alembic import op
import sqlalchemy as sa


revision = "pgtest_0003"
down_revision = "pgtest_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_failed_revision_probe",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    op.execute("INSERT INTO test_failed_revision_probe (id) VALUES (1)")
    raise RuntimeError("intentional PostgreSQL integration migration failure")


def downgrade() -> None:
    op.drop_table("test_failed_revision_probe")
