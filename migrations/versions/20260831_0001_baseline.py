"""Establish the Alembic revision baseline without changing application schema.

Revision ID: 20260831_0001
Revises: None
Create Date: 2026-08-31
"""

from typing import Sequence, Union


revision: str = "20260831_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
