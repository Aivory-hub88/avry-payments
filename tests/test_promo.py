"""Promo code rules: validation, discount maths and the charge floor."""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import pricing, promo

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
FRESH = {"total_used": 0, "spent": 0, "used_by_user": 0}


def _promo(**over):
    base = {
        "code": "SAVE20", "discount_type": "percent", "discount_value": 20.0,
        "max_redemptions": None, "per_user_limit": 1, "valid_from": None,
        "expires_at": None, "products": None, "active": True,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def fixed_rate(monkeypatch):
    monkeypatch.setattr(pricing, "usd_to_idr", lambda usd: int(round(usd * 16000)))


def test_normalize_uppercases_and_strips():
    assert promo.normalize_code("  launch-k7m2 ") == "LAUNCH-K7M2"
    assert promo.normalize_code("a b c") == "ABC"


@pytest.mark.parametrize("raw", ["", "ab", "-ABC", "HAS$IGN", "x" * 40, None])
def test_normalize_rejects_malformed(raw):
    with pytest.raises(promo.PromoError) as e:
        promo.normalize_code(raw)
    assert e.value.reason == "invalid_format"


def test_percent_and_fixed_discounts():
    assert promo.compute_discount_usd(100, "percent", 25) == 25.0
    assert promo.compute_discount_usd(39, "percent", 33.33) == 13.0
    assert promo.compute_discount_usd(39, "fixed_usd", 10) == 10.0


def test_fixed_discount_never_exceeds_price():
    assert promo.compute_discount_usd(5, "fixed_usd", 50) == 5.0


def test_evaluate_happy_path():
    assert promo.evaluate(_promo(), "operational", 39.0, FRESH, NOW) == 7.8


def test_evaluate_rejections():
    cases = [
        (_promo(active=False), FRESH, "inactive"),
        (_promo(valid_from=(NOW + timedelta(days=1)).isoformat()), FRESH, "not_started"),
        (_promo(expires_at=(NOW - timedelta(seconds=1)).isoformat()), FRESH, "expired"),
        (_promo(products=["business"]), FRESH, "not_applicable"),
        (_promo(max_redemptions=5), {"total_used": 5, "spent": 5, "used_by_user": 0}, "exhausted"),
        (_promo(per_user_limit=1), {"total_used": 1, "spent": 1, "used_by_user": 1}, "already_used"),
    ]
    for row, usage, reason in cases:
        with pytest.raises(promo.PromoError) as e:
            promo.evaluate(row, "operational", 39.0, usage, NOW)
        assert e.value.reason == reason


def test_already_used_wins_over_exhausted():
    usage = {"total_used": 1, "spent": 1, "used_by_user": 1}
    with pytest.raises(promo.PromoError) as e:
        promo.evaluate(_promo(max_redemptions=1), "operational", 39.0, usage, NOW)
    assert e.value.reason == "already_used"


def test_per_user_limit_allows_multiple_when_raised():
    row = _promo(per_user_limit=3)
    usage = {"total_used": 2, "spent": 2, "used_by_user": 2}
    assert promo.evaluate(row, "operational", 39.0, usage, NOW) > 0


def test_product_prefix_match():
    row = _promo(products=["credits_"])
    assert promo.evaluate(row, "credits_500", 38.0, FRESH, NOW) > 0
    with pytest.raises(promo.PromoError):
        promo.evaluate(row, "operational", 39.0, FRESH, NOW)


def test_wallet_topups_are_never_discountable():
    with pytest.raises(promo.PromoError) as e:
        promo.evaluate(_promo(), "wallet_topup_50", 50.0, FRESH, NOW)
    assert e.value.reason == "not_applicable"


def test_resolve_charge_applies_discount():
    usd, idr = pricing.resolve_charge("business", 9.9)
    assert usd == 89.1
    assert idr == 89.1 * 16000


def test_resolve_charge_enforces_gateway_floor():
    with pytest.raises(pricing.UnknownProduct):
        pricing.resolve_charge("credits_50", 4.99)  # $0.01 -> Rp 160


def test_resolve_charge_rejects_a_free_order():
    with pytest.raises(pricing.UnknownProduct):
        pricing.resolve_charge("credits_50", 5.0)


def test_generated_codes_are_valid_and_unambiguous():
    for _ in range(200):
        code = promo.generate_code("LAUNCH")
        assert promo.normalize_code(code) == code
        assert not set("01OIL") & set(code.split("-", 1)[1])


@pytest.mark.parametrize(
    "name,company,expected",
    [
        (None, "PT Distributor Nusantara", "DISTRIBUTORN"),
        ("Budi Santoso", None, "BUDISANTOSO"),
        ("Budi", "CV Maju Jaya", "MAJUJAYA"),
        ("Rina", "  ", "RINA"),
        (None, "PT", "PT"),          # only entity words: keep them rather than nothing
        ("x", None, ""),             # too short to identify anyone
        (None, None, ""),
    ],
)
def test_affiliate_prefix(name, company, expected):
    assert promo.affiliate_prefix(name, company) == expected


def test_affiliate_prefix_yields_valid_codes():
    prefix = promo.affiliate_prefix(None, "PT Sinar Mas Agro Resources & Technology Tbk")
    code = promo.generate_code(prefix)
    assert promo.normalize_code(code) == code
    assert code.startswith("SINARMASAGRO-")


def test_annual_operating_plan_prices():
    assert pricing.resolve_price_usd("operational_annual") == 240.0
    assert pricing.resolve_price_usd("business_annual") == 1188.0
    assert pricing.product_name("business_annual") == "Business Plan (Annual)"
    assert "operational_annual" in pricing.public_catalogue()


def test_promo_can_target_annual_plans_only():
    annual_only = _promo(products=["operational_annual", "business_annual"])
    assert promo.evaluate(annual_only, "business_annual", 1188.0, FRESH, NOW) == 237.6
    with pytest.raises(promo.PromoError):
        promo.evaluate(annual_only, "business", 99.0, FRESH, NOW)


def test_customer_limit_blocks_only_new_customers():
    capped = _promo(max_customers=2, per_user_limit=3)
    full_new = {"total_used": 4, "spent": 4, "used_by_user": 0,
                "other_customers": 2, "caller_is_customer": False}
    with pytest.raises(promo.PromoError) as e:
        promo.evaluate(capped, "business", 99.0, full_new, NOW)
    assert e.value.reason == "exhausted"

    # One of the two customers using it again is still within per_user_limit.
    returning = {"total_used": 4, "spent": 4, "used_by_user": 2,
                 "other_customers": 1, "caller_is_customer": True}
    assert promo.evaluate(capped, "business", 99.0, returning, NOW) == 19.8

    room = {"total_used": 1, "spent": 1, "used_by_user": 0,
            "other_customers": 1, "caller_is_customer": False}
    assert promo.evaluate(capped, "business", 99.0, room, NOW) == 19.8
