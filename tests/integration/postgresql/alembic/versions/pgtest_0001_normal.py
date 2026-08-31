"""Create a transactional probe and record the migration backend PID."""

from alembic import op
import sqlalchemy as sa


revision = "pgtest_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_revision_probe",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.String(50), nullable=False),
        sa.Column("backend_pid", sa.Integer(), nullable=False),
    )
    op.execute(
        "INSERT INTO test_revision_probe (phase, backend_pid) "
        "VALUES ('transactional', pg_backend_pid())"
    )


def downgrade() -> None:
    op.drop_table("test_revision_probe")

