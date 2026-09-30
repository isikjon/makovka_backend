"""loyalty tiers, states and purchases

Revision ID: 27d40401e0ae
Revises: 632a205f9975
Create Date: 2026-09-30 15:26:36.000000

"""
import json
import uuid
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "27d40401e0ae"
down_revision: Union[str, None] = "632a205f9975"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BIRTHDAY_PERK = "Бесплатный десерт на День рождения"

_SEED_TIERS = (
    ("bronze", "Бронза", "0", "10", "0", []),
    ("silver", "Серебро", "5000", "15", "5", [_BIRTHDAY_PERK]),
    ("gold", "Золото", "10000", "17", "7", [_BIRTHDAY_PERK]),
    ("platinum", "Платина", "15000", "19", "9", [_BIRTHDAY_PERK]),
    ("diamond", "Бриллиант", "20000", "21", "11", [_BIRTHDAY_PERK]),
)


def _money_column(name: str) -> sa.Column:
    return sa.Column(name, sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False)


def upgrade() -> None:
    loyalty_tiers = op.create_table(
        "loyalty_tiers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("min_spend", sa.Numeric(12, 2), nullable=False),
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("cashback_percent", sa.Numeric(5, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "perks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("iiko_category_id", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.execute(
        loyalty_tiers.insert().values(
            [
                {
                    "id": uuid.uuid4(),
                    "code": code,
                    "name": name,
                    "min_spend": Decimal(min_spend),
                    "discount_percent": Decimal(discount_percent),
                    "cashback_percent": Decimal(cashback_percent),
                    "perks": sa.cast(
                        sa.literal(json.dumps(perks, ensure_ascii=False), sa.Text()), postgresql.JSONB
                    ),
                    "iiko_category_id": None,
                    "sort_order": index,
                    "is_active": True,
                }
                for index, (code, name, min_spend, discount_percent, cashback_percent, perks) in enumerate(
                    _SEED_TIERS
                )
            ]
        )
    )

    op.create_table(
        "loyalty_states",
        sa.Column("user_id", sa.UUID(), nullable=False),
        _money_column("total_spend"),
        _money_column("bonus_balance"),
        _money_column("total_savings"),
        sa.Column("coffee_collected", sa.Integer(), nullable=True),
        sa.Column("tier_code", sa.String(length=32), nullable=True),
        sa.Column("synced_category_id", sa.String(length=64), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "purchases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("pos_order_id", sa.String(length=64), nullable=False),
        sa.Column("order_number", sa.Integer(), nullable=True),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        _money_column("order_sum"),
        _money_column("paid_sum"),
        _money_column("discount_sum"),
        _money_column("bonus_accrued"),
        _money_column("bonus_spent"),
        sa.Column("terminal_group_id", sa.String(length=64), nullable=True),
        sa.Column("bakery", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "pos_order_id", name="uq_purchases_user_pos_order"),
    )
    op.create_index(op.f("ix_purchases_user_id"), "purchases", ["user_id"], unique=False)
    op.create_index("ix_purchases_user_ordered_at", "purchases", ["user_id", "ordered_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_purchases_user_ordered_at", table_name="purchases")
    op.drop_index(op.f("ix_purchases_user_id"), table_name="purchases")
    op.drop_table("purchases")
    op.drop_table("loyalty_states")
    op.drop_table("loyalty_tiers")
