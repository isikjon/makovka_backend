import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from starlette.datastructures import FormData

from app.admin.forms import (
    form_snapshot,
    format_decimal,
    format_moscow,
    model_snapshot,
    parse_bakery,
    parse_novelty,
    parse_phone_list,
    parse_promotion,
    parse_tier,
    tier_conflicts,
)
from app.admin.queries import mask_card, mask_phone, reorder, search_digits

TIERS = ["bronze", "silver", "gold"]
CATEGORY = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _form(**fields) -> FormData:
    items = []
    for key, value in fields.items():
        for entry in value if isinstance(value, list) else [value]:
            items.append((key, entry))
    return FormData(items)


def _promotion_form(**fields) -> FormData:
    base = {"kind": "generic", "title": "Акция", "audience": "all", "is_active": "on"}
    return _form(**{**base, **fields})


class TestPhones:
    def test_normalizes_dedupes_and_reports_invalid(self):
        valid, invalid = parse_phone_list("+7 999 000-00-01\n89990000001, 9990000002;\n\n  abc ; 12345")
        assert valid == ["+79990000001", "+79990000002"]
        assert invalid == ["abc", "12345"]

    def test_empty(self):
        assert parse_phone_list("") == ([], [])


class TestPromotionForm:
    def test_minimal(self):
        values, errors = parse_promotion(_promotion_form(), TIERS)
        assert errors == {}
        assert values["title"] == "Акция"
        assert values["audience"] == "all"
        assert values["audience_phones"] == [] and values["audience_tiers"] == []
        assert values["is_active"] is True and values["show_on_home"] is False
        assert values["price"] is None and values["starts_at"] is None

    def test_phone_audience_is_normalized(self):
        values, errors = parse_promotion(
            _promotion_form(audience="phones", audience_phones="8 (999) 000-00-01\n+79990000002"), TIERS
        )
        assert errors == {}
        assert values["audience_phones"] == ["+79990000001", "+79990000002"]

    def test_invalid_phones_are_rejected_with_their_values(self):
        _, errors = parse_promotion(_promotion_form(audience="phones", audience_phones="+79990000001\n555-12"), TIERS)
        assert errors == {"audience_phones": "Неверные номера: 555-12"}

    def test_phone_audience_needs_numbers(self):
        _, errors = parse_promotion(_promotion_form(audience="phones", audience_phones=" \n "), TIERS)
        assert "audience_phones" in errors

    def test_phones_ignored_for_other_audiences(self):
        values, errors = parse_promotion(_promotion_form(audience="all", audience_phones="garbage"), TIERS)
        assert errors == {}
        assert values["audience_phones"] == []

    def test_tier_audience(self):
        values, errors = parse_promotion(_promotion_form(audience="tiers", audience_tiers=["gold", "silver"]), TIERS)
        assert errors == {}
        assert values["audience_tiers"] == ["gold", "silver"]

    def test_unknown_or_missing_tiers(self):
        _, errors = parse_promotion(_promotion_form(audience="tiers", audience_tiers=["vip"]), TIERS)
        assert errors == {"audience_tiers": "Неизвестные уровни: vip"}
        _, errors = parse_promotion(_promotion_form(audience="tiers"), TIERS)
        assert "audience_tiers" in errors

    def test_moscow_dates_are_stored_in_utc(self):
        values, errors = parse_promotion(
            _promotion_form(starts_at="2026-10-01T09:00", ends_at="2026-10-02T00:00"), TIERS
        )
        assert errors == {}
        assert values["starts_at"] == datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
        assert values["ends_at"] == datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
        assert format_moscow(values["starts_at"]) == "2026-10-01T09:00"

    def test_end_must_follow_start(self):
        _, errors = parse_promotion(_promotion_form(starts_at="2026-10-01T09:00", ends_at="2026-10-01T09:00"), TIERS)
        assert "ends_at" in errors

    def test_prices(self):
        values, errors = parse_promotion(_promotion_form(price="299", old_price="1 420,50"), TIERS)
        assert errors == {}
        assert values["price"] == Decimal("299.00")
        assert values["old_price"] == Decimal("1420.50")
        _, errors = parse_promotion(_promotion_form(price="-1", old_price="abc"), TIERS)
        assert set(errors) == {"price", "old_price"}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", ""),
            ("title", "x" * 121),
            ("subtitle", "x" * 201),
            ("description", "x" * 1001),
            ("badge", "x" * 21),
            ("time_text", "x" * 61),
            ("kind", "lottery"),
            ("audience", "everyone"),
            ("starts_at", "tomorrow"),
            ("sort_order", "1.5"),
        ],
    )
    def test_field_validation(self, field, value):
        _, errors = parse_promotion(_promotion_form(**{field: value}), TIERS)
        assert field in errors


class TestOtherForms:
    def test_novelty(self):
        values, errors = parse_novelty(_form(title="  Круассан  ", sort_order="3"))
        assert errors == {}
        assert values == {"title": "Круассан", "sort_order": 3, "is_active": False}

    def test_bakery(self):
        values, errors = parse_bakery(
            _form(name="Сходненская", address="Сходненская улица, 35 к1", latitude="55,8583", longitude="37.4553")
        )
        assert errors == {}
        assert values["latitude"] == 55.8583
        assert values["metro"] is None

    @pytest.mark.parametrize(("latitude", "longitude"), [("91", "37"), ("55", "-181"), ("nan", "37"), ("", "37")])
    def test_bakery_coordinates(self, latitude, longitude):
        _, errors = parse_bakery(_form(name="x", address="y", latitude=latitude, longitude=longitude))
        assert {"latitude", "longitude"} & set(errors)

    def test_tier(self):
        values, errors = parse_tier(
            _form(
                name="Серебро",
                min_spend="5000",
                discount_percent="15",
                cashback_percent="5",
                perks="Десерт на ДР\n\n  Кофе  \n",
                iiko_category_id=CATEGORY,
                is_active="on",
            )
        )
        assert errors == {}
        assert values["perks"] == ["Десерт на ДР", "Кофе"]
        assert values["iiko_category_id"] == CATEGORY
        assert values["discount_percent"] == Decimal("15.00")

    def test_tier_manual_category_wins_and_empty_clears(self):
        other = "11111111-2222-3333-4444-555555555555"
        base = {"name": "x", "min_spend": "0", "discount_percent": "1", "cashback_percent": "0"}
        values, _ = parse_tier(_form(**base, iiko_category_id=CATEGORY, iiko_category_manual=other))
        assert values["iiko_category_id"] == other
        values, _ = parse_tier(_form(**base))
        assert values["iiko_category_id"] is None

    @pytest.mark.parametrize(
        "raw",
        [CATEGORY.upper(), CATEGORY.replace("-", ""), f"  {CATEGORY.upper().replace('-', '')}  "],
    )
    def test_tier_category_is_normalized_to_canonical_uuid(self, raw):
        base = {"name": "x", "min_spend": "0", "discount_percent": "1", "cashback_percent": "0"}
        values, errors = parse_tier(_form(**base, iiko_category_manual=raw))
        assert errors == {}
        assert values["iiko_category_id"] == CATEGORY

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("discount_percent", "101"),
            ("cashback_percent", "-1"),
            ("min_spend", ""),
            ("name", ""),
            ("iiko_category_manual", "bronze"),
            ("iiko_category_manual", "{" + CATEGORY + "}"),
            ("iiko_category_manual", CATEGORY[:-1]),
            ("perks", "\n".join(f"perk {index}" for index in range(21))),
        ],
    )
    def test_tier_validation(self, field, value):
        base = {"name": "x", "min_spend": "0", "discount_percent": "1", "cashback_percent": "0"}
        _, errors = parse_tier(_form(**{**base, field: value}))
        assert field in errors


@dataclass
class TierRow:
    id: uuid.UUID
    min_spend: Decimal
    iiko_category_id: str | None
    is_active: bool = True


class TestTierConflicts:
    SILVER = TierRow(uuid.uuid4(), Decimal("5000"), CATEGORY)
    GOLD = TierRow(uuid.uuid4(), Decimal("10000"), "11111111-2222-3333-4444-555555555555")

    def _values(self, **fields):
        values = {"min_spend": Decimal("5000"), "iiko_category_id": CATEGORY, "is_active": True}
        values.update(fields)
        return values

    def test_unchanged_tier_has_no_conflicts(self):
        assert tier_conflicts([self.SILVER, self.GOLD], self.SILVER, self._values()) == {}

    def test_duplicate_threshold_of_active_tier(self):
        errors = tier_conflicts([self.SILVER, self.GOLD], self.SILVER, self._values(min_spend=Decimal("10000")))
        assert set(errors) == {"min_spend"}

    def test_duplicate_threshold_is_fine_when_either_tier_is_inactive(self):
        inactive_gold = TierRow(self.GOLD.id, self.GOLD.min_spend, self.GOLD.iiko_category_id, is_active=False)
        values = self._values(min_spend=Decimal("10000"))
        assert tier_conflicts([self.SILVER, inactive_gold], self.SILVER, values) == {}
        assert tier_conflicts([self.SILVER, self.GOLD], self.SILVER, {**values, "is_active": False}) == {}

    def test_category_of_another_tier_in_any_spelling(self):
        legacy_gold = TierRow(self.GOLD.id, self.GOLD.min_spend, self.GOLD.iiko_category_id.upper().replace("-", ""))
        values = self._values(iiko_category_id=self.GOLD.iiko_category_id)
        assert set(tier_conflicts([self.SILVER, legacy_gold], self.SILVER, values)) == {"iiko_category_manual"}

    def test_tiers_without_category_never_conflict(self):
        bronze = TierRow(uuid.uuid4(), Decimal("0"), None)
        assert tier_conflicts([bronze, self.SILVER], self.SILVER, self._values(iiko_category_id=None)) == {}


class TestSnapshots:
    def test_model_snapshot(self):
        snapshot = model_snapshot(
            {
                "active": True,
                "hidden": False,
                "price": Decimal("299.00"),
                "fraction": Decimal("199.50"),
                "missing": None,
                "tiers": ["gold"],
                "order": 3,
            }
        )
        assert snapshot == {
            "active": "on",
            "hidden": "",
            "price": "299",
            "fraction": "199.50",
            "missing": "",
            "tiers": ["gold"],
            "order": "3",
        }

    def test_form_snapshot_keeps_raw_input(self):
        snapshot = form_snapshot(_form(title="<b>", audience_tiers=["gold", "silver"]), ["audience_tiers"])
        assert snapshot == {"title": "<b>", "audience_tiers": ["gold", "silver"]}
        assert form_snapshot(_form(title="x"), ["audience_tiers"])["audience_tiers"] == []

    def test_format_decimal(self):
        assert format_decimal(None) == ""
        assert format_decimal(Decimal("10.00")) == "10"


@dataclass
class Item:
    id: uuid.UUID
    sort_order: int


class TestQueriesHelpers:
    def test_reorder_swaps_and_renumbers(self):
        items = [Item(uuid.uuid4(), 10), Item(uuid.uuid4(), 10), Item(uuid.uuid4(), 30)]
        first, second, third = items
        assert reorder(items, third.id, -1) is True
        assert [first.sort_order, third.sort_order, second.sort_order] == [0, 1, 2]

    def test_reorder_edges_and_unknown(self):
        items = [Item(uuid.uuid4(), 0), Item(uuid.uuid4(), 1)]
        assert reorder(items, items[0].id, -1) is True
        assert [item.sort_order for item in items] == [0, 1]
        assert reorder(items, uuid.uuid4(), 1) is False

    def test_masks(self):
        assert mask_phone("+79991234567") == "+7••••••4567"
        assert mask_phone("123") == "•••"
        assert mask_card("1234567890123456") == "•••• 3456"
        assert mask_card(None) == "—"

    def test_search_digits(self):
        assert search_digits("+7 (999) 12-34") == "79991234"
        assert search_digits("%_' or 1=1") == "11"
        assert search_digits("1" * 20) == "1" * 10
        assert search_digits(None) == ""
