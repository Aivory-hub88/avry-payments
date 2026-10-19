"""
Affiliate funnel queries against a real Postgres.

Skipped unless PROMO_TEST_DATABASE_URL points at a throwaway database: the
schema is created there and the billing tables are truncated.
"""
import os

import pytest

DB = os.getenv("PROMO_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="PROMO_TEST_DATABASE_URL not set")


@pytest.fixture()
def repo(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    from app.database import pg, promo_repo

    with pg.cursor() as cur:
        cur.execute(
            "TRUNCATE billing.promo_codes, billing.promo_code_events, billing.payment_orders"
        )
    return promo_repo, pg


def _order(cur, n, user, code, status, amount=80.0, discount=19.0, email=None):
    cur.execute(
        """
        INSERT INTO billing.payment_orders
            (order_id, payment_id, user_id, product, amount_usd, status,
             customer_email, promo_code, discount_usd, list_amount_usd)
        VALUES (%s, %s, %s, 'business', %s, %s, %s, %s, %s, 99)
        """,
        (f"o{n}", f"p{n}", user, amount, status, email, code, discount),
    )


def test_funnel_per_code_affiliate_and_leads(repo):
    promo_repo, pg = repo
    base = {"discount_type": "percent", "discount_value": 20, "per_user_limit": 1}
    promo_repo.insert_many([
        {**base, "code": "NUSANTARA-AAAA", "affiliate_company": "PT Nusantara", "affiliate_name": "Budi"},
        {**base, "code": "NUSANTARA-BBBB", "affiliate_company": "PT Nusantara", "affiliate_name": "Sari"},
        {**base, "code": "PLAIN-CCCC"},
    ])
    # Code A: u1 applied only; u2 applied + abandoned checkout; u3 applied + paid;
    # u4 paid without the preview step.
    for user in ("u1", "u2", "u3", "u2"):
        promo_repo.record_apply("NUSANTARA-AAAA", user, f"{user}@x.id", "business", True, None)
    promo_repo.record_apply("NOPE-ZZZZ", "u9", None, "business", False, "not_found")
    with pg.cursor() as cur:
        _order(cur, 1, "u2", "NUSANTARA-AAAA", "pending")
        _order(cur, 2, "u3", "NUSANTARA-AAAA", "paid", email="u3@x.id")
        _order(cur, 3, "u4", "NUSANTARA-AAAA", "paid", email="u4@x.id")
        _order(cur, 4, "u5", "NUSANTARA-BBBB", "paid")

    rows, total = promo_repo.list_codes()
    assert total == 3
    a = next(r for r in rows if r["code"] == "NUSANTARA-AAAA")
    assert a["affiliate_company"] == "PT Nusantara"
    assert (a["applied_users"], a["checkout_users"], a["paid_users"]) == (4, 3, 2)
    assert a["revenue_usd"] == 160.0 and a["discount_given_usd"] == 38.0
    assert a["conversion_rate"] == 0.5
    plain = next(r for r in rows if r["code"] == "PLAIN-CCCC")
    assert plain["applied_users"] == 0 and plain["conversion_rate"] == 0.0

    filtered, n = promo_repo.list_codes(affiliate="PT Nusantara")
    assert n == 2 and {r["code"] for r in filtered} == {"NUSANTARA-AAAA", "NUSANTARA-BBBB"}
    assert promo_repo.list_codes(search="sari")[1] == 1

    (aff,) = promo_repo.affiliates()
    assert aff["affiliate"] == "PT Nusantara" and aff["codes"] == 2
    assert aff["affiliate_names"] == "Budi, Sari"
    assert (aff["applied_users"], aff["checkout_users"], aff["paid_users"]) == (5, 4, 3)
    assert aff["revenue_usd"] == 240.0

    leads = promo_repo.leads("NUSANTARA-AAAA")
    stages = {l["user_id"]: l["stage"] for l in leads}
    assert stages == {"u1": "applied", "u2": "checkout", "u3": "paid", "u4": "paid"}
    assert [l["stage"] for l in leads][:2] == ["paid", "paid"]
    assert next(l for l in leads if l["user_id"] == "u1")["email"] == "u1@x.id"

    # Unknown codes are never recorded.
    with pg.cursor(commit=False) as cur:
        cur.execute("SELECT count(*) FROM billing.promo_code_events WHERE code = 'NOPE-ZZZZ'")
        assert cur.fetchone()[0] == 0


def _client(caller):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth import require_auth
    from app.routes.payment import router as payment_router
    from app.routes.promo import router as promo_router

    app = FastAPI()
    app.include_router(payment_router)
    app.include_router(promo_router)
    app.dependency_overrides[require_auth] = lambda: caller
    return TestClient(app)


def test_generate_prefixes_from_affiliate_and_apply_is_tracked(repo, monkeypatch):
    promo_repo, _ = repo
    admin = _client({"user_id": "adm", "account_type": "superadmin"})
    r = admin.post("/api/v1/payments/promo/admin/generate", json={
        "discount_type": "percent", "discount_value": 20, "count": 3,
        "affiliate_name": "Budi", "affiliate_company": "PT Distributor Nusantara",
    })
    assert r.status_code == 200, r.text
    codes = r.json()["codes"]
    assert len(codes) == 3 and all(c.startswith("DISTRIBUTORN-") for c in codes)
    assert promo_repo.get(codes[0])["affiliate_company"] == "PT Distributor Nusantara"

    # An explicit prefix still wins over the derived one.
    r = admin.post("/api/v1/payments/promo/admin/generate", json={
        "discount_type": "percent", "discount_value": 20, "count": 2,
        "prefix": "EXPO", "affiliate_company": "PT Distributor Nusantara",
    })
    assert all(c.startswith("EXPO-") for c in r.json()["codes"])

    from app.services import fx

    async def fresh():
        return None

    monkeypatch.setattr(fx, "ensure_fresh", fresh)
    monkeypatch.setattr("app.services.pricing.usd_to_idr", lambda usd: int(round(usd * 16000)))
    customer = _client({"user_id": "cust1", "email": "cust1@x.id", "account_type": "free"})
    r = customer.post("/api/v1/payments/promo/validate", json={"code": codes[0].lower(), "product": "business"})
    assert r.json()["valid"] is True

    leads = admin.get(f"/api/v1/payments/promo/admin/{codes[0]}/leads").json()["leads"]
    assert leads == [{**leads[0], "user_id": "cust1", "email": "cust1@x.id", "stage": "applied"}]
    aff = admin.get("/api/v1/payments/promo/admin/affiliates").json()["affiliates"]
    assert aff[0]["affiliate"] == "PT Distributor Nusantara" and aff[0]["applied_users"] == 1
    assert admin.get("/api/v1/payments/promo/admin/NOPE-1234/leads").status_code == 404


def test_customer_limit_is_enforced_when_the_order_is_created(repo):
    promo_repo, pg = repo
    from app.database import payment_repo

    promo_repo.insert_many([{
        "code": "TEAM-2", "discount_type": "percent", "discount_value": 20,
        "per_user_limit": 2, "max_customers": 2,
    }])

    def order(n, user):
        return payment_repo.create({
            "order_id": f"t{n}", "payment_id": f"tp{n}", "user_id": user,
            "product": "business", "amount": 79.2, "amount_idr": 1267200,
            "usd_idr_rate": 16000, "status": "pending", "payment_method": "midtrans",
            "midtrans_transaction_id": None, "customer_email": f"{user}@x.id",
            "is_mock": False, "promo_code": "TEAM-2", "discount_usd": 19.8,
            "list_amount_usd": 99,
        })

    order(1, "a")
    order(2, "b")               # second customer takes the last slot
    with pytest.raises(payment_repo.PromoUnavailable) as e:
        order(3, "c")           # a third customer is refused
    assert e.value.reason == "exhausted"
    order(4, "a")               # an existing customer may use it again

    rows, _ = promo_repo.list_codes(search="TEAM-2")
    assert rows[0]["max_customers"] == 2
    assert promo_repo.usage("TEAM-2", "c")["other_customers"] == 2


def test_retyped_affiliate_joins_the_existing_one_and_creator_is_recorded(repo):
    promo_repo, _ = repo
    admin = _client({"user_id": "adm1", "email": "ops@aivory.id", "account_type": "superadmin"})
    gen = "/api/v1/payments/promo/admin/generate"
    first = admin.post(gen, json={
        "discount_type": "percent", "discount_value": 10,
        "affiliate_company": "PT Distributor Nusantara", "affiliate_partner_id": "user_partner_1",
    }).json()
    second = admin.post(gen, json={
        "discount_type": "percent", "discount_value": 10, "count": 2,
        "affiliate_company": "distributor  nusantara tbk",
    }).json()
    other = admin.post(gen, json={
        "discount_type": "percent", "discount_value": 10, "affiliate_company": "PT Distributor Jaya",
    }).json()

    assert second["affiliate_company"] == "PT Distributor Nusantara"
    assert all(c.startswith("DISTRIBUTORN-") for c in second["codes"])
    assert other["affiliate_company"] == "PT Distributor Jaya"

    row = promo_repo.get(first["codes"][0])
    assert row["affiliate_partner_id"] == "user_partner_1"
    assert row["created_by"] == "adm1" and row["created_by_email"] == "ops@aivory.id"

    by_name = {a["affiliate"]: a for a in promo_repo.affiliates()}
    assert set(by_name) == {"PT Distributor Nusantara", "PT Distributor Jaya"}
    assert by_name["PT Distributor Nusantara"]["codes"] == 3
    assert by_name["PT Distributor Nusantara"]["affiliate_partner_id"] == "user_partner_1"


def test_settled_promo_order_notifies_admins_with_customer_counts(repo, monkeypatch):
    import asyncio

    promo_repo, pg = repo
    from app.services import notifications

    with pg.cursor() as cur:
        cur.execute("TRUNCATE billing.notifications")
    promo_repo.insert_many([
        {"code": "NUSA-1", "discount_type": "percent", "discount_value": 20,
         "per_user_limit": 1, "max_customers": 20, "affiliate_company": "PT Nusa & Co"},
        {"code": "NUSA-2", "discount_type": "percent", "discount_value": 20,
         "per_user_limit": 1, "affiliate_company": "PT Nusa & Co"},
    ])
    with pg.cursor() as cur:
        _order(cur, 1, "u1", "NUSA-1", "paid", email="u1@x.id")
        _order(cur, 2, "u2", "NUSA-1", "pending")          # not a conversion yet
        _order(cur, 3, "u3", "NUSA-2", "paid", amount=50.0)

    sent = {"emails": [], "telegram": []}

    async def fake_email(to, subject, html, text):
        sent["emails"].append((subject, html, text))
        return True

    async def fake_telegram(text):
        sent["telegram"].append(text)
        return True

    async def no_receipt(*a, **k):
        return True

    monkeypatch.setattr(notifications, "_send_email", fake_email)
    monkeypatch.setattr(notifications, "_telegram_admin", fake_telegram)
    monkeypatch.setattr(notifications, "_send_receipt", no_receipt)

    record = {
        "order_id": "o1", "user_id": "u1", "product": "business", "amount": 80.0,
        "amount_idr": 1280000, "customer_email": "u1@x.id", "promo_code": "NUSA-1",
        "discount_usd": 19.0,
    }
    asyncio.run(notifications.payment_settled(record, "settled"))

    with pg.cursor(commit=False) as cur:
        cur.execute(
            "SELECT title, body, meta FROM billing.notifications "
            "WHERE audience = 'admin' AND type = 'promo_conversion'"
        )
        (title, body, meta), = cur.fetchall()
    assert title == "Promo conversion — NUSA-1 (PT Nusa & Co)"
    assert "1 of 20 customers (19 left)" in body
    assert "PT Nusa & Co: 2 paying customers, $130.00 across 2 codes" in body
    assert meta["promo_customers"] == 1 and meta["promo_max_customers"] == 20

    subject, html, text = sent["emails"][0]
    assert subject.endswith("· promo NUSA-1")
    assert "Code used by: 1 of 20 customers (19 left)" in text
    assert "PT Nusa &amp; Co" in html and "PT Nusa & Co" not in html
    assert "1 of 20 customers" in sent["telegram"][0] and "Nusa &amp; Co" in sent["telegram"][0]


def test_order_without_promo_sends_no_promo_notification(repo, monkeypatch):
    import asyncio

    _, pg = repo
    from app.services import notifications

    with pg.cursor() as cur:
        cur.execute("TRUNCATE billing.notifications")

    async def ok(*a, **k):
        return True

    for name in ("_send_email", "_telegram_admin", "_send_receipt"):
        monkeypatch.setattr(notifications, name, ok)
    asyncio.run(notifications.payment_settled(
        {"order_id": "o9", "user_id": "u9", "product": "business", "amount": 99.0}, "settled"
    ))
    with pg.cursor(commit=False) as cur:
        cur.execute("SELECT count(*) FROM billing.notifications WHERE type = 'promo_conversion'")
        assert cur.fetchone()[0] == 0


def test_partner_gets_conversion_email_without_buyer_identity(repo, monkeypatch):
    import asyncio

    promo_repo, pg = repo
    from app.services import notifications

    with pg.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS identity")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS identity.users (id TEXT PRIMARY KEY, email TEXT, "
            "account_type TEXT, is_active BOOLEAN, company_name TEXT)"
        )
        cur.execute("TRUNCATE identity.users, billing.notifications")
        cur.execute(
            "INSERT INTO identity.users VALUES "
            "('p1','ops@nusa.co.id','partner',true,'PT Nusa'),"
            "('p2','Sales@Nusa.co.id','partner',true,'pt nusa'),"      # second login, same distributor
            "('p3','old@nusa.co.id','partner',false,'PT Nusa'),"       # suspended
            "('p4','x@other.id','partner',true,'PT Other'),"           # different distributor
            "('c1','buyer@customer.id','paid',true,'PT Nusa')"         # not a partner
        )
    promo_repo.insert_many([
        {"code": "NUSA-P", "discount_type": "percent", "discount_value": 10, "per_user_limit": 1,
         "max_customers": 10, "affiliate_company": "PT Nusa", "affiliate_partner_id": "p1"},
        {"code": "PLAIN-X", "discount_type": "percent", "discount_value": 10, "per_user_limit": 1},
    ])
    with pg.cursor() as cur:
        _order(cur, 1, "c1", "NUSA-P", "paid", email="buyer@customer.id")

    sent = []

    async def fake_email(to, subject, html, text):
        sent.append((to, subject, html, text))
        return True

    async def ok(*a, **k):
        return True

    monkeypatch.setattr(notifications, "_send_email", fake_email)
    monkeypatch.setattr(notifications, "_telegram_admin", ok)
    monkeypatch.setattr(notifications, "_send_receipt", ok)

    asyncio.run(notifications.payment_settled({
        "order_id": "o1", "user_id": "c1", "product": "business", "amount": 89.1,
        "customer_email": "buyer@customer.id", "promo_code": "NUSA-P", "discount_usd": 9.9,
    }, "settled"))

    partner = [s for s in sent if s[1] == "Your Aivory code NUSA-P was just used"]
    assert sorted(s[0] for s in partner) == ["ops@nusa.co.id", "sales@nusa.co.id"]
    to, subject, html, text = partner[0]
    assert "1 of 10 customers (9 left)" in text
    assert "Across all 1 of your codes: 1 paying customer." in text
    assert "buyer@customer.id" not in text and "buyer@customer.id" not in html
    assert "c1" not in text

    # A code not linked to a partner emails no partner.
    sent.clear()
    with pg.cursor() as cur:
        _order(cur, 2, "c2", "PLAIN-X", "paid")
    asyncio.run(notifications.payment_settled({
        "order_id": "o2", "user_id": "c2", "product": "business", "amount": 89.1,
        "promo_code": "PLAIN-X",
    }, "settled"))
    assert not [s for s in sent if "was just used" in s[1]]


def test_confirm_returns_order_summary_for_the_success_page(repo, monkeypatch):
    _, pg = repo
    from app.services.payment_gateway import midtrans_service

    with pg.cursor() as cur:
        _order(cur, 1, "cust1", None, "pending", amount=0.2, discount=0)

    async def settled(order_id, expected_user_id=None):
        return {"success": True, "order_id": order_id, "status": "paid", "granted": True}

    monkeypatch.setattr(midtrans_service, "settle_order", settled)
    client = _client({"user_id": "cust1", "account_type": "paid"})
    body = client.post("/api/v1/payments/confirm", json={"order_id": "o1"}).json()
    assert body["granted"] is True and body["status"] == "paid"
    assert body["product"] == "business" and body["product_name"] == "Business Plan"
    assert body["amount_usd"] == 0.2

    async def pending(order_id, expected_user_id=None):
        return {"success": True, "order_id": order_id, "status": "pending", "granted": False}

    monkeypatch.setattr(midtrans_service, "settle_order", pending)
    body = client.post("/api/v1/payments/confirm", json={"order_id": "o1"}).json()
    assert body["granted"] is False and body["status"] == "pending"

    # Somebody else's order stays private.
    other = _client({"user_id": "cust2", "account_type": "paid"})
    assert other.post("/api/v1/payments/confirm", json={"order_id": "o1"}).status_code == 403


def test_finish_redirect_goes_to_the_marketing_success_page():
    from app.config import Settings

    assert Settings.model_fields["payment_finish_redirect_url"].default == "https://aivory.uk/checkout/success"
