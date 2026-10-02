"""admin panel, app content, referrals and runtime settings

Revision ID: 9d862365fd25
Revises: 27d40401e0ae
Create Date: 2026-09-30 18:00:00.000000

"""
import uuid
from decimal import Decimal
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9d862365fd25"
down_revision: Union[str, None] = "27d40401e0ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KIND_CHECK = "kind IN ('generic', 'coffee_stamps', 'referral', 'happy_hours', 'combo')"
_AUDIENCE_CHECK = "audience IN ('all', 'phones', 'tiers')"

_SEED_NOVELTIES = (
    ("Пицца цыпленок барбекю", "seed_pizza_bbq.png"),
    ("Печуша с ветчиной и сыром", "seed_pechusha_ham_cheese.png"),
    ("Круассан с шоколадом", "seed_croissant_chocolate.png"),
)

_PROMOTION_FIELDS = ("kind", "title", "subtitle", "description", "time_text", "price", "old_price")
_SEED_PROMOTIONS: tuple[dict[str, Any], ...] = (
    {
        "kind": "referral",
        "title": "Пригласи друга - получи бонус!",
        "subtitle": "За каждого друга - до 500 бонусов!",
    },
    {
        "kind": "coffee_stamps",
        "title": "Скидка 99% на каждый {n} кофе",
        "description": "Копите чашки в приложении:\nкаждый {n}-й кофе — за 1% стоимости!",
    },
    {
        "kind": "happy_hours",
        "title": "Скидка 10% в счастливые часы",
        "description": "Каждый день вечером — получите скидку на любимые лакомства",
        "time_text": "С 20:30 до 00:00",
    },
    {
        "kind": "combo",
        "title": "Комбо: Круассан + Американо",
        "subtitle": "Идеальное утреннее сочетание",
        "price": Decimal("299"),
        "old_price": Decimal("420"),
    },
)

_SEED_BAKERIES = (
    ("Сходненская", "Сходненская улица, 35 к1", "Метро Сходненская", "Открыто с 07:00 до 23:00", 55.8583, 37.4553),
    ("Химкинский бульвар", "Химкинский бульвар, 21", "Метро Сходненская", "Открыто с 08:00 до 23:00", 55.8917, 37.4295),
    ("Тушинская", "Тушинская улица, 24 ст15", "Метро Тушинская", "Открыто с 06:00 до 23:00", 55.8264, 37.4407),
    (
        "Маршала Василевского",
        "Улица Маршала Василевского, 17",
        "Метро Щукинская",
        "Открыто с 07:00 до 23:00",
        55.8103,
        37.4581,
    ),
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _listing_columns() -> list[sa.Column]:
    return [
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )

    novelties = op.create_table(
        "novelties",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("image", sa.String(length=64), nullable=True),
        *_listing_columns(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        novelties,
        [
            {"id": uuid.uuid4(), "title": title, "image": image, "sort_order": index, "is_active": True}
            for index, (title, image) in enumerate(_SEED_NOVELTIES)
        ],
    )

    promotions = op.create_table(
        "promotions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), server_default=sa.text("'generic'"), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("subtitle", sa.String(length=200), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("badge", sa.String(length=20), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("old_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("time_text", sa.String(length=60), nullable=True),
        sa.Column("image", sa.String(length=64), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("show_on_home", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_listing_columns(),
        sa.Column("audience", sa.String(length=10), server_default=sa.text("'all'"), nullable=False),
        sa.Column(
            "audience_phones",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "audience_tiers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(_KIND_CHECK, name="ck_promotions_kind"),
        sa.CheckConstraint(_AUDIENCE_CHECK, name="ck_promotions_audience"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        promotions,
        [
            {
                "id": uuid.uuid4(),
                **{field: promotion.get(field) for field in _PROMOTION_FIELDS},
                "show_on_home": True,
                "sort_order": index,
                "is_active": True,
                "audience": "all",
            }
            for index, promotion in enumerate(_SEED_PROMOTIONS)
        ],
    )

    bakeries = op.create_table(
        "bakeries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("metro", sa.String(length=120), nullable=True),
        sa.Column("hours_text", sa.String(length=120), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        *_listing_columns(),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        bakeries,
        [
            {
                "id": uuid.uuid4(),
                "name": name,
                "address": address,
                "metro": metro,
                "hours_text": hours_text,
                "latitude": latitude,
                "longitude": longitude,
                "sort_order": index,
                "is_active": True,
            }
            for index, (name, address, metro, hours_text, latitude, longitude) in enumerate(_SEED_BAKERIES)
        ],
    )

    op.add_column("users", sa.Column("referral_code", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("referred_by_id", sa.UUID(), nullable=True))
    op.add_column("users", sa.Column("referred_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_users_referral_code", "users", ["referral_code"])
    op.create_index(op.f("ix_users_referred_by_id"), "users", ["referred_by_id"], unique=False)
    op.create_foreign_key(
        "fk_users_referred_by_id_users", "users", "users", ["referred_by_id"], ["id"], ondelete="SET NULL"
    )

    op.create_table(
        "referral_rewards",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("referred_user_id", sa.UUID(), nullable=False),
        sa.Column("referrer_user_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("referrer_done", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("referred_done", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("referrer_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("referred_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["referred_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["referrer_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referred_user_id"),
    )
    op.create_index(
        op.f("ix_referral_rewards_referrer_user_id"), "referral_rewards", ["referrer_user_id"], unique=False
    )

    op.create_table(
        "admin_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("login", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("session_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("login"),
    )


def downgrade() -> None:
    op.drop_table("admin_users")
    op.drop_index(op.f("ix_referral_rewards_referrer_user_id"), table_name="referral_rewards")
    op.drop_table("referral_rewards")
    op.drop_constraint("fk_users_referred_by_id_users", "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_referred_by_id"), table_name="users")
    op.drop_constraint("uq_users_referral_code", "users", type_="unique")
    op.drop_column("users", "referred_at")
    op.drop_column("users", "referred_by_id")
    op.drop_column("users", "referral_code")
    op.drop_table("bakeries")
    op.drop_table("promotions")
    op.drop_table("novelties")
    op.drop_table("app_settings")
