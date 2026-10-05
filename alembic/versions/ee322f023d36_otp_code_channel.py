"""otp code delivery channel

Revision ID: ee322f023d36
Revises: 9d862365fd25
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ee322f023d36"
down_revision: Union[str, None] = "9d862365fd25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "otp_codes",
        sa.Column("channel", sa.String(length=10), server_default=sa.text("'sms'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("otp_codes", "channel")
