import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.loyalty.rules import (
    bakery_name,
    build_purchases,
    canonical_uuid,
    count_coffee_stamps,
    extract_category_ids,
    format_iiko_datetime,
    orphan_order_ids,
    parse_counters,
    parse_iiko_datetime,
    parse_terminal_groups,
    pick_wallet,
    pick_wallet_balance,
    plan_category_changes,
    resolve_bonus_wallet_id,
    resolve_tier,
    tier_spend_ranges,
    to_money,
)


@dataclass(frozen=True)
class Tier:
    code: str
    min_spend: Decimal


TIERS = [
    Tier("bronze", Decimal("0")),
    Tier("silver", Decimal("5000")),
    Tier("gold", Decimal("10000")),
    Tier("platinum", Decimal("15000")),
    Tier("diamond", Decimal("20000")),
]


def _codes(result):
    current, upcoming, remaining, progress = result
    return (
        current.code if current else None,
        upcoming.code if upcoming else None,
        remaining,
        progress,
    )


class TestResolveTier:
    def test_zero_spend_is_entry_tier(self):
        assert _codes(resolve_tier(TIERS, Decimal("0"))) == ("bronze", "silver", Decimal("5000"), 0.0)

    @pytest.mark.parametrize(
        ("spend", "current", "upcoming", "remaining"),
        [
            ("5000", "silver", "gold", Decimal("5000")),
            ("10000", "gold", "platinum", Decimal("5000")),
            ("15000", "platinum", "diamond", Decimal("5000")),
        ],
    )
    def test_exact_threshold_enters_tier(self, spend, current, upcoming, remaining):
        assert _codes(resolve_tier(TIERS, Decimal(spend))) == (current, upcoming, remaining, 0.0)

    def test_exact_top_threshold(self):
        assert _codes(resolve_tier(TIERS, Decimal("20000"))) == ("diamond", None, None, 1.0)

    def test_just_below_threshold_stays_in_lower_tier(self):
        current, upcoming, remaining, progress = resolve_tier(TIERS, Decimal("4999.99"))
        assert current.code == "bronze"
        assert upcoming.code == "silver"
        assert remaining == Decimal("0.01")
        assert 0.999 < progress < 1.0

    def test_progress_within_tier(self):
        assert _codes(resolve_tier(TIERS, Decimal("7500"))) == ("silver", "gold", Decimal("2500"), 0.5)

    def test_beyond_top_tier_has_full_progress(self):
        assert _codes(resolve_tier(TIERS, Decimal("1000000"))) == ("diamond", None, None, 1.0)

    def test_empty_tiers(self):
        assert resolve_tier([], Decimal("12345")) == (None, None, None, 1.0)

    def test_unsorted_input(self):
        shuffled = [TIERS[3], TIERS[0], TIERS[4], TIERS[2], TIERS[1]]
        assert _codes(resolve_tier(shuffled, Decimal("12500"))) == ("gold", "platinum", Decimal("2500"), 0.5)

    def test_spend_below_lowest_threshold(self):
        tiers = [Tier("entry", Decimal("100")), Tier("next", Decimal("200"))]
        assert _codes(resolve_tier(tiers, Decimal("40"))) == (None, "entry", Decimal("60"), 0.4)

    def test_single_tier_is_top(self):
        assert _codes(resolve_tier([TIERS[0]], Decimal("0"))) == ("bronze", None, None, 1.0)


_ROW_IDS = itertools.count(1)


def _row(type_name, pos_order_id="order-1", **extra):
    row = {
        "id": f"tx-{next(_ROW_IDS)}",
        "typeName": type_name,
        "posOrderId": pos_order_id,
        "orderNumber": 42,
        "terminalGroupId": "tg-1",
        "whenCreated": "2026-09-01 10:31:00.000",
        "whenCreatedOrder": "2026-09-01T10:30:55Z",
        "walletId": "bonus-wallet",
        "isIgnored": False,
        "sum": 0,
    }
    row.update(extra)
    return row


BAKERIES = {"tg-1": "Сходненская 35", "tg-2": "Ленина 1"}
BONUS_WALLET = "bonus-wallet"


class TestBuildPurchases:
    def test_close_order_with_several_discount_rows(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", orderSum=1000, sum=850),
                _row("DiscountSum", sum=100),
                _row("DiscountSum", sum=50.5),
            ],
            BAKERIES,
        )
        assert purchases == [
            {
                "pos_order_id": "order-1",
                "order_number": 42,
                "ordered_at": datetime(2026, 9, 1, 10, 30, 55, tzinfo=timezone.utc),
                "order_sum": Decimal("1000.00"),
                "paid_sum": Decimal("850.00"),
                "discount_sum": Decimal("150.50"),
                "bonus_accrued": Decimal("0.00"),
                "bonus_spent": Decimal("0.00"),
                "terminal_group_id": "tg-1",
                "bakery": "Сходненская 35",
            }
        ]

    def test_ignored_rows_are_skipped(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", orderSum=500, sum=450),
                _row("DiscountSum", sum=50),
                _row("DiscountSum", sum=999, isIgnored=True),
                _row("CloseOrder", "order-2", orderSum=300, sum=300, isIgnored=True),
            ],
            BAKERIES,
        )
        assert [purchase["pos_order_id"] for purchase in purchases] == ["order-1"]
        assert purchases[0]["discount_sum"] == Decimal("50.00")

    def test_rows_without_pos_order_id_are_skipped(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", None, orderSum=100, sum=100),
                _row("CloseOrder", "", orderSum=100, sum=100),
                {"typeName": "CloseOrder", "orderSum": 100, "sum": 100},
            ],
            BAKERIES,
        )
        assert purchases == []

    def test_group_without_close_order_is_skipped(self):
        purchases = build_purchases(
            [_row("DiscountSum", "order-9", sum=10), _row("RefillWalletFromOrder", "order-9", sum=5)],
            BAKERIES,
        )
        assert purchases == []

    def test_wallet_refills_payments_and_cancellations(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", orderSum=1000, sum=900),
                _row("RefillWalletFromOrder", sum=30),
                _row("RefillWallet", sum=5),
                _row("CancelRefillWalletFromOrder", sum=-10),
                _row("PayFromWallet", sum=-40),
                _row("PayFromWallet", sum=60),
                _row("CancelPayFromWallet", sum=60),
            ],
            BAKERIES,
            bonus_wallet_id=BONUS_WALLET,
        )
        assert purchases[0]["bonus_accrued"] == Decimal("25.00")
        assert purchases[0]["bonus_spent"] == Decimal("40.00")

    def test_fully_cancelled_wallet_operations_never_go_negative(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", orderSum=100, sum=100),
                _row("CancelRefillWalletFromOrder", sum=15),
                _row("CancelPayFromWallet", sum=20),
            ],
            BAKERIES,
            bonus_wallet_id=BONUS_WALLET,
        )
        assert purchases[0]["bonus_accrued"] == Decimal("0.00")
        assert purchases[0]["bonus_spent"] == Decimal("0.00")

    def test_other_wallets_are_ignored_when_bonus_wallet_known(self):
        rows = [
            _row("CloseOrder", orderSum=100, sum=100),
            _row("RefillWalletFromOrder", sum=10),
            _row("RefillWalletFromOrder", sum=1, walletId="coffee-wallet"),
            _row("PayFromWallet", sum=3, walletId="COFFEE-WALLET"),
            _row("PayFromWallet", sum=2, walletId=None),
        ]
        purchases = build_purchases(rows, BAKERIES, bonus_wallet_id="BONUS-WALLET")
        assert purchases[0]["bonus_accrued"] == Decimal("10.00")
        assert purchases[0]["bonus_spent"] == Decimal("2.00")

    def test_wallet_rows_are_not_bonus_without_bonus_wallet(self):
        rows = [
            _row("CloseOrder", orderSum=100, sum=90),
            _row("DiscountSum", sum=10),
            _row("RefillWalletFromOrder", sum=1, walletId="coffee-wallet"),
            _row("PayFromWallet", sum=3, walletId="bonus-wallet"),
        ]
        for bonus_wallet_id in (None, ""):
            purchases = build_purchases(rows, BAKERIES, bonus_wallet_id=bonus_wallet_id)
            assert purchases[0]["discount_sum"] == Decimal("10.00")
            assert purchases[0]["bonus_accrued"] == Decimal("0.00")
            assert purchases[0]["bonus_spent"] == Decimal("0.00")

    def test_rows_repeated_across_pages_are_counted_once(self):
        close = _row("CloseOrder", orderSum=1000, sum=900)
        discount = _row("DiscountSum", sum=50)
        refill = _row("RefillWalletFromOrder", sum=30)
        spend = _row("PayFromWallet", sum=20)
        page_0 = [close, discount]
        page_1 = [dict(discount), refill, dict(refill), spend, dict(spend)]
        purchases = build_purchases(page_0 + page_1, BAKERIES, bonus_wallet_id=BONUS_WALLET)
        assert purchases[0]["discount_sum"] == Decimal("50.00")
        assert purchases[0]["bonus_accrued"] == Decimal("30.00")
        assert purchases[0]["bonus_spent"] == Decimal("20.00")

    def test_rows_without_id_are_not_deduplicated(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", orderSum=100, sum=80),
                _row("DiscountSum", sum=10, id=None),
                _row("DiscountSum", sum=10, id=None),
            ],
            BAKERIES,
        )
        assert purchases[0]["discount_sum"] == Decimal("20.00")

    def test_unknown_types_are_ignored(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", orderSum=200, sum=180),
                _row("SomethingNew", sum=1000),
                _row("DiscountSum", sum=20),
            ],
            BAKERIES,
        )
        assert purchases[0]["discount_sum"] == Decimal("20.00")
        assert purchases[0]["bonus_accrued"] == Decimal("0.00")

    def test_groups_several_orders_sorted_by_time(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", "late", orderSum=10, sum=10, whenCreatedOrder="2026-09-03T08:00:00Z"),
                _row("CloseOrder", "early", orderSum=20, sum=20, whenCreatedOrder="2026-09-02T08:00:00Z"),
                _row("DiscountSum", "late", sum=1),
            ],
            BAKERIES,
        )
        assert [purchase["pos_order_id"] for purchase in purchases] == ["early", "late"]
        assert purchases[1]["discount_sum"] == Decimal("1.00")

    def test_bakery_is_resolved_from_terminal_group(self):
        purchases = build_purchases(
            [
                _row("CloseOrder", "a", terminalGroupId="tg-2"),
                _row("CloseOrder", "b", terminalGroupId="unknown"),
                _row("CloseOrder", "c", terminalGroupId=None),
            ],
            BAKERIES,
        )
        by_order = {purchase["pos_order_id"]: purchase for purchase in purchases}
        assert by_order["a"]["bakery"] == "Ленина 1"
        assert by_order["b"]["bakery"] is None
        assert by_order["b"]["terminal_group_id"] == "unknown"
        assert by_order["c"]["bakery"] is None
        assert by_order["c"]["terminal_group_id"] is None

    def test_ordered_at_falls_back_to_when_created(self):
        purchases = build_purchases(
            [_row("CloseOrder", whenCreatedOrder=None, whenCreated="2026-09-05 12:00:00.123")],
            BAKERIES,
        )
        assert purchases[0]["ordered_at"] == datetime(2026, 9, 5, 12, 0, 0, 123000, tzinfo=timezone.utc)

    def test_order_without_any_timestamp_is_skipped(self):
        purchases = build_purchases(
            [_row("CloseOrder", whenCreatedOrder=None, whenCreated=None)],
            BAKERIES,
        )
        assert purchases == []

    def test_missing_order_number(self):
        purchases = build_purchases([_row("CloseOrder", orderNumber=None)], BAKERIES)
        assert purchases[0]["order_number"] is None

    def test_non_mapping_rows_are_ignored(self):
        purchases = build_purchases([None, "junk", _row("CloseOrder", orderSum=1, sum=1)], BAKERIES)
        assert len(purchases) == 1


class TestOrphanOrders:
    def test_groups_without_close_order(self):
        rows = [
            _row("CloseOrder", "closed"),
            _row("DiscountSum", "closed", sum=5),
            _row("RefillWalletFromOrder", "late-accrual", sum=12),
            _row("CancelRefillWalletFromOrder", "returned", sum=7),
            _row("CancelPayFromWallet", "returned", sum=3),
        ]
        assert orphan_order_ids(rows) == ["late-accrual", "returned"]

    def test_ignored_close_order_leaves_group_orphaned(self):
        rows = [_row("CloseOrder", "order-7", isIgnored=True), _row("DiscountSum", "order-7", sum=1)]
        assert orphan_order_ids(rows) == ["order-7"]

    def test_rows_without_order_are_not_orphans(self):
        assert orphan_order_ids([_row("RefillWallet", None, sum=1), None, "junk"]) == []

    def test_no_transactions(self):
        assert orphan_order_ids([]) == []


class TestTerminalGroups:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Сходненская 35 (Касса 1)", "Сходненская 35"),
            ("Сходненская 35 (касса 12)", "Сходненская 35"),
            ("Ленина 5  (КАССА 2) ", "Ленина 5"),
            ("Ленина 5", "Ленина 5"),
            ("Ленина 5 (угол)", "Ленина 5 (угол)"),
            ("", ""),
        ],
    )
    def test_bakery_name_strips_cash_register_suffix(self, raw, expected):
        assert bakery_name(raw) == expected

    def test_parse_terminal_groups(self):
        response = {
            "terminalGroups": [
                {
                    "organizationId": "org-1",
                    "items": [
                        {"id": "tg-1", "name": "Сходненская 35 (Касса 1)"},
                        {"id": "tg-2", "name": "Сходненская 35 (Касса 2)"},
                        {"name": "Без id"},
                    ],
                }
            ],
            "terminalGroupsInSleep": [{"items": [{"id": "tg-3", "name": "Тверская 7 (Касса 1)"}]}],
        }
        assert parse_terminal_groups(response) == {
            "tg-1": "Сходненская 35",
            "tg-2": "Сходненская 35",
            "tg-3": "Тверская 7",
        }

    def test_parse_terminal_groups_empty(self):
        assert parse_terminal_groups({}) == {}
        assert parse_terminal_groups({"terminalGroups": None}) == {}


BONUS_PROGRAM = "cat-bonus-program"
DISCOUNT_25 = "cat-discount-25"
DISCOUNT_50 = "cat-discount-50"
SILVER = "cat-silver"
GOLD = "cat-gold"
PLATINUM = "cat-platinum"
TIER_CATEGORIES = [None, SILVER, GOLD, PLATINUM]


class TestPlanCategoryChanges:
    def test_moves_guest_to_new_tier(self):
        assert plan_category_changes(GOLD, [BONUS_PROGRAM, SILVER], TIER_CATEGORIES) == ([GOLD], [SILVER])

    def test_never_touches_non_tier_categories(self):
        to_add, to_remove = plan_category_changes(
            PLATINUM, [BONUS_PROGRAM, DISCOUNT_25, DISCOUNT_50, SILVER, GOLD], TIER_CATEGORIES
        )
        assert to_add == [PLATINUM]
        assert to_remove == [SILVER, GOLD]
        assert not {BONUS_PROGRAM, DISCOUNT_25, DISCOUNT_50} & set(to_add + to_remove)

    def test_already_in_target(self):
        assert plan_category_changes(GOLD, [BONUS_PROGRAM, GOLD], TIER_CATEGORIES) == ([], [])

    def test_already_in_target_with_leftover_tier(self):
        assert plan_category_changes(GOLD, [GOLD, SILVER, DISCOUNT_25], TIER_CATEGORIES) == ([], [SILVER])

    def test_guest_without_categories(self):
        assert plan_category_changes(SILVER, [], TIER_CATEGORIES) == ([SILVER], [])

    def test_no_target_removes_only_tier_categories(self):
        assert plan_category_changes(None, [BONUS_PROGRAM, SILVER, GOLD], TIER_CATEGORIES) == ([], [SILVER, GOLD])

    def test_only_non_tier_categories(self):
        assert plan_category_changes(None, [BONUS_PROGRAM, DISCOUNT_50], TIER_CATEGORIES) == ([], [])

    def test_ids_compared_case_insensitively(self):
        assert plan_category_changes("CAT-GOLD", [BONUS_PROGRAM, GOLD, "CAT-SILVER"], TIER_CATEGORIES) == (
            [],
            ["CAT-SILVER"],
        )

    def test_duplicates_and_blanks_in_guest_categories(self):
        assert plan_category_changes(GOLD, [SILVER, SILVER, "", BONUS_PROGRAM], TIER_CATEGORIES) == (
            [GOLD],
            [SILVER],
        )

    def test_extract_category_ids(self):
        customer = {
            "categories": [
                {"id": BONUS_PROGRAM, "name": "Бонусная программа", "isActive": True},
                {"id": SILVER, "name": "Серебро"},
                {"name": "Без id"},
                None,
            ]
        }
        assert extract_category_ids(customer) == [BONUS_PROGRAM, SILVER]
        assert extract_category_ids({}) == []
        assert extract_category_ids({"categories": None}) == []


class TestParseCounters:
    def test_value_for_guest(self):
        response = {"counters": [{"guestId": "guest-1", "period": 0, "metric": 1, "value": 407.0}]}
        assert parse_counters(response, "guest-1") == Decimal("407.00")

    def test_guest_id_case_insensitive(self):
        response = {"counters": [{"guestId": "ABC-DEF", "period": 0, "metric": 1, "value": 12.5}]}
        assert parse_counters(response, "abc-def") == Decimal("12.50")

    def test_other_guest_is_ignored(self):
        response = {"counters": [{"guestId": "someone-else", "period": 0, "metric": 1, "value": 999}]}
        assert parse_counters(response, "guest-1") == Decimal("0")

    @pytest.mark.parametrize("response", [{}, {"counters": []}, {"counters": None}])
    def test_missing_counter_means_zero(self, response):
        assert parse_counters(response, "guest-1") == Decimal("0")

    def test_null_value_means_zero(self):
        response = {"counters": [{"guestId": "guest-1", "period": 0, "metric": 1, "value": None}]}
        assert parse_counters(response, "guest-1") == Decimal("0")


WALLETS = [
    {"id": "coffee-wallet", "name": "Кофе", "type": 4, "balance": 5.0},
    {"id": "c4a67eee-d7c2-11e9-80dd-d8d385655247", "name": "Программа через бонусы", "type": 1, "balance": 120.5},
]


class TestWallets:
    def test_pick_by_configured_id(self):
        assert pick_wallet_balance(WALLETS, "coffee-wallet") == Decimal("5.00")

    def test_configured_id_is_case_insensitive(self):
        assert pick_wallet_balance(WALLETS, "C4A67EEE-D7C2-11E9-80DD-D8D385655247") == Decimal("120.50")

    def test_configured_id_missing_does_not_fall_back(self):
        assert pick_wallet_balance(WALLETS, "unknown-wallet") == Decimal("0")

    def test_empty_id_falls_back_to_type(self):
        assert pick_wallet_balance(WALLETS, "") == Decimal("120.50")
        assert pick_wallet(WALLETS, "")["id"] == "c4a67eee-d7c2-11e9-80dd-d8d385655247"

    def test_type_given_as_string(self):
        assert pick_wallet_balance([{"id": "w", "type": "1", "balance": 7}], "") == Decimal("7.00")

    def test_no_wallet_of_fallback_type(self):
        assert pick_wallet_balance([WALLETS[0]], "") == Decimal("0")
        assert pick_wallet([WALLETS[0]], "") is None

    def test_fallback_disabled(self):
        assert pick_wallet(WALLETS, "", type_fallback=None) is None

    @pytest.mark.parametrize("wallets", [None, [], [None, "junk"]])
    def test_no_wallets(self, wallets):
        assert pick_wallet_balance(wallets, "") == Decimal("0")

    def test_null_balance(self):
        assert pick_wallet_balance([{"id": "w", "type": 1, "balance": None}], "") == Decimal("0")

    def test_type_fallback_skips_excluded_wallets(self):
        wallets = [
            {"id": "Coffee-Wallet", "type": 1, "balance": 4},
            {"id": "bonus", "type": 1, "balance": 90},
        ]
        assert pick_wallet(wallets, "", exclude_ids=("coffee-wallet", ""))["id"] == "bonus"
        assert pick_wallet(wallets[:1], "", exclude_ids=("coffee-wallet",)) is None

    def test_exclusion_does_not_apply_to_configured_id(self):
        assert pick_wallet(WALLETS, "coffee-wallet", exclude_ids=("coffee-wallet",))["id"] == "coffee-wallet"


class TestBonusWalletId:
    def test_configured_id_is_used_even_if_guest_lacks_wallet(self):
        assert resolve_bonus_wallet_id([WALLETS[0]], "configured-bonus", ("coffee-wallet",)) == "configured-bonus"

    def test_falls_back_to_bonus_type_wallet(self):
        assert resolve_bonus_wallet_id(WALLETS, "", ("coffee-wallet",)) == "c4a67eee-d7c2-11e9-80dd-d8d385655247"

    def test_fallback_never_picks_coffee_wallet(self):
        wallets = [{"id": "coffee-wallet", "type": 1, "balance": 3}]
        assert resolve_bonus_wallet_id(wallets, "", ("COFFEE-WALLET",)) is None

    def test_fallback_skips_every_known_coffee_wallet(self):
        wallets = [
            {"id": "env-coffee", "type": 1, "balance": 3},
            {"id": "admin-coffee", "type": 1, "balance": 2},
            {"id": "bonus", "type": 1, "balance": 90},
        ]
        assert resolve_bonus_wallet_id(wallets, "", ("ENV-COFFEE", "admin-coffee")) == "bonus"
        assert resolve_bonus_wallet_id(wallets, "", ("env-coffee", "")) == "admin-coffee"

    @pytest.mark.parametrize("wallets", [None, [], [{"type": 1, "balance": 5}], [WALLETS[0]]])
    def test_unidentified_bonus_wallet(self, wallets):
        assert resolve_bonus_wallet_id(wallets, "", ("",)) is None


class TestCanonicalUuid:
    @pytest.mark.parametrize(
        "raw",
        [
            "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            " 3FA85F64-5717-4562-B3FC-2C963F66AFA6 ",
            "3fa85f6457174562b3fc2c963f66afa6",
            "3fa85f64-57174562-b3fc2c963f66afa6",
        ],
    )
    def test_normalizes_case_and_dashes(self, raw):
        assert canonical_uuid(raw) == "3fa85f64-5717-4562-b3fc-2c963f66afa6"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not-a-uuid",
            "{3fa85f64-5717-4562-b3fc-2c963f66afa6}",
            "urn:uuid:3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "3fa85f64-5717-4562-b3fc-2c963f66afa",
            "3fa85f64--5717-4562-b3fc-2c963f66afa6",
        ],
    )
    def test_rejects_anything_else(self, raw):
        assert canonical_uuid(raw) is None


class TestTierSpendRanges:
    def test_ranges_match_resolve_tier(self):
        tiers = [Tier("bronze", Decimal(0)), Tier("silver", Decimal(5000)), Tier("gold", Decimal(10000))]
        assert tier_spend_ranges(tiers, ["silver", "gold"]) == [(Decimal(5000), Decimal(10000)), (Decimal(10000), None)]
        assert tier_spend_ranges(tiers, ["unknown"]) == []


class TestCoffeeStamps:
    def test_not_configured(self):
        assert count_coffee_stamps(WALLETS, "", 7) is None

    def test_counts_configured_wallet(self):
        assert count_coffee_stamps(WALLETS, "coffee-wallet", 7) == 5

    def test_capped_at_goal(self):
        assert count_coffee_stamps([{"id": "coffee", "balance": 12}], "coffee", 7) == 7

    def test_fraction_is_floored(self):
        assert count_coffee_stamps([{"id": "coffee", "balance": 3.9}], "coffee", 7) == 3

    def test_negative_is_zero(self):
        assert count_coffee_stamps([{"id": "coffee", "balance": -2}], "coffee", 7) == 0

    def test_missing_wallet_is_zero(self):
        assert count_coffee_stamps(WALLETS, "absent", 7) == 0


class TestConversions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, Decimal("0")),
            (True, Decimal("0")),
            ("abc", Decimal("0")),
            (float("nan"), Decimal("0")),
            (407.0, Decimal("407.00")),
            ("12.345", Decimal("12.34")),
            (5, Decimal("5.00")),
        ],
    )
    def test_to_money(self, value, expected):
        assert to_money(value) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-09-01T10:30:55Z", datetime(2026, 9, 1, 10, 30, 55, tzinfo=timezone.utc)),
            ("2026-09-01 10:30:55.123", datetime(2026, 9, 1, 10, 30, 55, 123000, tzinfo=timezone.utc)),
            ("2026-09-01T13:30:55+03:00", datetime(2026, 9, 1, 10, 30, 55, tzinfo=timezone.utc)),
            ("2026-09-01T10:30:55.1234567Z", datetime(2026, 9, 1, 10, 30, 55, 123456, tzinfo=timezone.utc)),
            ("not a date", None),
            ("", None),
            (None, None),
            (123, None),
        ],
    )
    def test_parse_iiko_datetime(self, raw, expected):
        assert parse_iiko_datetime(raw) == expected

    def test_format_iiko_datetime(self):
        moment = datetime(2026, 9, 1, 13, 5, 9, 987654, tzinfo=timezone.utc)
        assert format_iiko_datetime(moment) == "2026-09-01 13:05:09.000"
