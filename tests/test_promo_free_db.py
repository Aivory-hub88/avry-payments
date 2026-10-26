"""
100% promo codes, editing and regenerating codes, against a real Postgres.

Skipped unless PROMO_TEST_DATABASE_URL points at a throwaway database (the
billing tables there are truncated).
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

DB = os.getenv("PROMO_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="PROMO_TEST_DATABASE_URL not set")

ADMIN = {"user_id": "admin1", "email": "admin@aivory.id", "account_type": "superadmin"}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    from app.database import pg, promo_repo
    from app.services import entitlements, notifications

    with pg.cursor() as cur:
        cur.execute(
            "TRUNCATE billing.promo_codes, billing.promo_code_events, billing.payment_orders"
        )
    grants, notes = [], []

    async def fake_grant(**kw):
        grants.append(kw)
        return True, "granted"

    async def fake_notify(record, detail):
        notes.append(record)

    monkeypatch.setattr(entitlements, "apply_entitlement", fake_grant)
    monkeypatch.setattr(notifications, "payment_settled", fake_notify)
    promo_repo.insert_many([{
        "code": "FREE-TEST", "discount_type": "percent", "discount_value": 100,
        "per_user_limit": 1, "max_customers": 2,
    }])
    return promo_repo, grants, notes


def _charge(product, code, user):
    from app.routes import payment

    req = payment.ChargeRequest(product=product, channel="qris", promo_code=code)
    return asyncio.run(payment.charge_midtrans(req, {"user_id": user}))


def test_full_discount_grants_without_midtrans(env):
    promo_repo, grants, notes = env
    res = _charge("operational", "FREE-TEST", "u1")
    assert res.free and res.amount_idr == 0 and res.transaction_status == "settlement"
    assert grants[0]["product"] == "operational" and grants[0]["amount_usd"] > 0
    assert notes[0]["status"] == "paid" and notes[0]["amount_idr"] == 0

    # The code is spent for this customer, and the customer cap still holds.
    with pytest.raises(HTTPException) as e:
        _charge("operational", "FREE-TEST", "u1")
    assert e.value.detail["promo_error"] == "already_used"
    _charge("ai_snapshot", "FREE-TEST", "u2")
    with pytest.raises(HTTPException) as e:
        _charge("operational", "FREE-TEST", "u3")
    assert e.value.detail["promo_error"] == "exhausted"


def test_edit_dates_and_limits(env):
    from app.routes import promo as routes

    promo_repo, _, _ = env
    soon = datetime.now(timezone.utc) + timedelta(days=7)
    out = asyncio.run(routes.update_code(
        "free-test", routes.UpdateRequest(expires_at=soon, max_customers=10), ADMIN))
    assert out["max_customers"] == 10 and out["expires_at"].startswith(soon.date().isoformat())

    # Fields not sent are untouched; an explicit null clears the end date.
    out = asyncio.run(routes.update_code("FREE-TEST", routes.UpdateRequest(expires_at=None), ADMIN))
    assert out["expires_at"] is None and out["max_customers"] == 10

    with pytest.raises(HTTPException):
        asyncio.run(routes.update_code("FREE-TEST", routes.UpdateRequest(
            valid_from=soon, expires_at=soon - timedelta(days=1)), ADMIN))


def test_regenerate_copies_rules_and_resets_usage(env):
    from app.routes import promo as routes

    promo_repo, _, _ = env
    _charge("operational", "FREE-TEST", "u1")
    until = datetime.now(timezone.utc) + timedelta(days=3)
    out = asyncio.run(routes.regenerate(
        "FREE-TEST", routes.RegenerateRequest(expires_at=until), ADMIN))
    new = promo_repo.get(out["code"])
    assert out["code"].startswith("FREE-TEST-")
    assert (new["discount_value"], new["max_customers"], new["active"]) == (100.0, 2, True)
    assert promo_repo.get("FREE-TEST")["active"] is False

    # The customer who spent the old code can use the new one.
    assert _charge("operational", out["code"], "u1").free

    custom = asyncio.run(routes.regenerate(
        out["code"], routes.RegenerateRequest(code="aivory-test100", deactivate_old=False), ADMIN))
    assert custom["code"] == "AIVORY-TEST100" and promo_repo.get(out["code"])["active"]
