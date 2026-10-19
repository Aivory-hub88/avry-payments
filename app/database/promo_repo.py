"""
Promo code storage and usage counting.

A redemption is an order in `billing.payment_orders` carrying the code, so usage
is always derived from the ledger rather than kept as a counter that could drift.

What counts as "used":

* `paid` / granted / `awaiting_verification` — the code is spent.
* `pending` inside the payment expiry window — held for that customer, so two
  people cannot both start checkout on the last redemption. It is released
  automatically when the window passes, with no cleanup job.

A customer's own pending orders never count against them: closing the payment
sheet and trying again must not lock them out of their code.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.database import pg

_COLUMNS = (
    "code, discount_type, discount_value, max_redemptions, per_user_limit, "
    "valid_from, expires_at, products, active, campaign, note, created_by, "
    "created_at, updated_at, affiliate_name, affiliate_company, max_customers, "
    "affiliate_partner_id, created_by_email"
)
_N_COLUMNS = 19

# An order the customer actually paid for (or that was granted manually).
_PAID = "(o.status = 'paid' OR o.granted_at IS NOT NULL)"


def _row(r: Tuple) -> Dict[str, Any]:
    def iso(v):
        return v.isoformat() if v else None

    return {
        "code": r[0],
        "discount_type": r[1],
        "discount_value": float(r[2]),
        "max_redemptions": r[3],
        "per_user_limit": r[4],
        "valid_from": iso(r[5]),
        "expires_at": iso(r[6]),
        "products": list(r[7]) if r[7] else None,
        "active": bool(r[8]),
        "campaign": r[9],
        "note": r[10],
        "created_by": r[11],
        "created_at": iso(r[12]),
        "updated_at": iso(r[13]),
        "affiliate_name": r[14],
        "affiliate_company": r[15],
        "max_customers": r[16],
        "affiliate_partner_id": r[17],
        "created_by_email": r[18],
    }


_USED_SQL = """
    SELECT
      count(*) FILTER (
          WHERE status = 'paid' OR granted_at IS NOT NULL
             OR status = 'awaiting_verification'
      ) AS spent,
      count(DISTINCT user_id) FILTER (
          WHERE status = 'pending'
            AND granted_at IS NULL
            AND created_at > now() - make_interval(hours => %s)
            AND user_id <> %s
      ) AS held_by_others,
      count(*) FILTER (
          WHERE user_id = %s AND (
              status = 'paid' OR granted_at IS NOT NULL
              OR status = 'awaiting_verification'
          )
      ) AS spent_by_user,
      -- Customer slots: a customer holds one once they have spent the code
      -- or have a checkout with it open inside the payment window.
      count(DISTINCT user_id) FILTER (
          WHERE user_id <> %s AND (
              status = 'paid' OR granted_at IS NOT NULL
              OR status = 'awaiting_verification'
              OR (status = 'pending'
                  AND created_at > now() - make_interval(hours => %s))
          )
      ) AS other_customers,
      count(*) FILTER (
          WHERE user_id = %s AND (
              status = 'paid' OR granted_at IS NOT NULL
              OR status = 'awaiting_verification'
              OR (status = 'pending'
                  AND created_at > now() - make_interval(hours => %s))
          )
      ) AS caller_orders
    FROM billing.payment_orders
    WHERE promo_code = %s
"""


def usage_in_cursor(cur, code: str, user_id: str) -> Dict[str, int]:
    """Redemption counts for `code`, from the caller's own cursor/transaction."""
    hours = settings.payment_expiry_hours
    cur.execute(
        _USED_SQL,
        (hours, user_id, user_id, user_id, hours, user_id, hours, code),
    )
    spent, held, by_user, other_customers, caller_orders = cur.fetchone()
    return {
        "total_used": int(spent) + int(held),
        "spent": int(spent),
        "used_by_user": int(by_user),
        "other_customers": int(other_customers),
        # Already one of the code's customers, so no new slot is needed.
        "caller_is_customer": int(caller_orders) > 0,
    }


def get(code: str) -> Optional[Dict[str, Any]]:
    with pg.cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM billing.promo_codes WHERE code = %s", (code,))
        row = cur.fetchone()
    return _row(row) if row else None


def usage(code: str, user_id: str) -> Dict[str, int]:
    with pg.cursor(commit=False) as cur:
        return usage_in_cursor(cur, code, user_id)


def insert_many(rows: List[Dict[str, Any]]) -> List[str]:
    """
    Insert codes, skipping any that already exist. Returns the codes created.

    The caller generates codes randomly, so a collision is possible but rare;
    skipping keeps a bulk run from failing on one unlucky draw.
    """
    created: List[str] = []
    with pg.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO billing.promo_codes
                    (code, discount_type, discount_value, max_redemptions,
                     per_user_limit, valid_from, expires_at, products, active,
                     campaign, note, created_by, affiliate_name, affiliate_company,
                     max_customers, affiliate_partner_id, created_by_email)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (code) DO NOTHING
                RETURNING code
                """,
                (
                    r["code"], r["discount_type"], r["discount_value"],
                    r.get("max_redemptions"), r.get("per_user_limit", 1),
                    r.get("valid_from"), r.get("expires_at"), r.get("products"),
                    r.get("active", True), r.get("campaign"), r.get("note"),
                    r.get("created_by"), r.get("affiliate_name"),
                    r.get("affiliate_company"), r.get("max_customers"),
                    r.get("affiliate_partner_id"), r.get("created_by_email"),
                ),
            )
            if cur.fetchone():
                created.append(r["code"])
    return created


def canonical_affiliate(column: str, value: Optional[str]) -> Optional[str]:
    """
    The spelling already on record for the affiliate `value` names, else
    `value` itself. Matching uses promo.affiliate_key, so a retyped name with
    different case, spacing or "PT"/"Tbk" joins the existing affiliate rather
    than splitting its numbers. The oldest spelling wins.
    """
    if not value:
        return value
    if column not in ("affiliate_company", "affiliate_name"):
        raise ValueError(column)
    from app.services.promo import affiliate_key

    key = affiliate_key(value)
    if not key:
        return value
    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"SELECT {column} FROM billing.promo_codes WHERE {column} IS NOT NULL "
            f"GROUP BY {column} ORDER BY min(created_at)"
        )
        for (existing,) in cur.fetchall():
            if affiliate_key(existing) == key:
                return existing
    return value


def set_active(code: str, active: bool) -> bool:
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE billing.promo_codes SET active = %s, updated_at = now() "
            "WHERE code = %s RETURNING code",
            (active, code),
        )
        return cur.fetchone() is not None


def list_codes(
    search: Optional[str] = None,
    campaign: Optional[str] = None,
    status: Optional[str] = None,
    affiliate: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Codes with their redemption counts, newest first.

    `status` is one of active | inactive | expired | exhausted; the derived
    states are evaluated here so the admin filter and the validation rules
    cannot disagree.
    """
    where: List[str] = ["1=1"]
    params: List[Any] = []
    if search:
        where.append(
            "(c.code ILIKE %s OR c.note ILIKE %s OR c.campaign ILIKE %s "
            "OR c.affiliate_name ILIKE %s OR c.affiliate_company ILIKE %s)"
        )
        params.extend([f"%{search}%"] * 5)
    if campaign:
        where.append("c.campaign = %s")
        params.append(campaign)
    if affiliate:
        where.append("COALESCE(c.affiliate_company, c.affiliate_name) = %s")
        params.append(affiliate)

    redeemed = (
        "(SELECT count(*) FROM billing.payment_orders o WHERE o.promo_code = c.code "
        "AND (o.status = 'paid' OR o.granted_at IS NOT NULL "
        "OR o.status = 'awaiting_verification'))"
    )
    customers = (
        "(SELECT count(DISTINCT o.user_id) FROM billing.payment_orders o "
        "WHERE o.promo_code = c.code AND (o.status = 'paid' OR o.granted_at IS NOT NULL "
        "OR o.status = 'awaiting_verification'))"
    )
    full = (
        f"((c.max_redemptions IS NOT NULL AND {redeemed} >= c.max_redemptions) "
        f"OR (c.max_customers IS NOT NULL AND {customers} >= c.max_customers))"
    )
    if status == "active":
        where.append(
            "c.active AND (c.expires_at IS NULL OR c.expires_at > now()) "
            f"AND NOT {full}"
        )
    elif status == "inactive":
        where.append("NOT c.active")
    elif status == "expired":
        where.append("c.expires_at IS NOT NULL AND c.expires_at <= now()")
    elif status == "exhausted":
        where.append(full)

    clause = " AND ".join(where)
    cols = ", ".join(f"c.{c.strip()}" for c in _COLUMNS.split(","))
    with pg.cursor(commit=False) as cur:
        cur.execute(f"SELECT count(*) FROM billing.promo_codes c WHERE {clause}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT {cols}, {redeemed} AS redeemed, {customers} AS customers_used,
                   COALESCE(o.discount_given_usd, 0), COALESCE(o.revenue_usd, 0),
                   COALESCE(e.applied_users, 0), COALESCE(o.checkout_users, 0),
                   COALESCE(o.paid_users, 0)
            FROM billing.promo_codes c
            LEFT JOIN LATERAL ({_ORDER_STATS} WHERE o.promo_code = c.code) o ON true
            LEFT JOIN LATERAL ({_LEAD_STATS}) e ON true
            WHERE {clause}
            ORDER BY c.created_at DESC, c.code
            LIMIT %s OFFSET %s
            """,
            params + [max(1, min(limit, 500)), max(0, offset)],
        )
        out = []
        for r in cur.fetchall():
            d = _row(r[:_N_COLUMNS])
            d["redeemed"] = int(r[_N_COLUMNS])
            d["customers_used"] = int(r[_N_COLUMNS + 1])
            d.update(_funnel(r[_N_COLUMNS + 2:]))
            out.append(d)
    return out, total


# Funnel per code. "Applied" counts every customer who used the code at all:
# those who entered it at checkout, plus any who went straight to an order
# without the preview, so it is never below checkout.
_ORDER_STATS = f"""
    SELECT sum(o.discount_usd) FILTER (WHERE {_PAID}) AS discount_given_usd,
           sum(o.amount_usd)   FILTER (WHERE {_PAID}) AS revenue_usd,
           count(DISTINCT o.user_id)                  AS checkout_users,
           count(DISTINCT o.user_id) FILTER (WHERE {_PAID}) AS paid_users
    FROM billing.payment_orders o
"""
_LEAD_STATS = """
    SELECT count(*) AS applied_users FROM (
        SELECT e.user_id FROM billing.promo_code_events e WHERE e.code = c.code
        UNION
        SELECT o2.user_id FROM billing.payment_orders o2 WHERE o2.promo_code = c.code
    ) leads
"""


def _funnel(values) -> Dict[str, Any]:
    discount, revenue, applied, checkout, paid = values
    applied = int(applied)
    return {
        "discount_given_usd": float(discount),
        "revenue_usd": float(revenue),
        "applied_users": applied,
        "checkout_users": int(checkout),
        "paid_users": int(paid),
        "conversion_rate": round(int(paid) / applied, 4) if applied else 0.0,
    }


def record_apply(
    code: str, user_id: str, email: Optional[str], product: str,
    valid: bool, reason: Optional[str],
) -> None:
    """Log that a customer applied an existing code. Never raises: tracking must
    not be able to break checkout."""
    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO billing.promo_code_events
                    (code, user_id, email, product, valid, reason)
                SELECT %s, %s, %s, %s, %s, %s
                WHERE EXISTS (SELECT 1 FROM billing.promo_codes WHERE code = %s)
                """,
                (code, user_id, email, product, valid, reason, code),
            )
    except Exception:  # pragma: no cover - best effort
        import logging

        logging.getLogger(__name__).warning("Could not record promo apply for %s", code, exc_info=True)


def affiliates() -> List[Dict[str, Any]]:
    """Lead conversion rolled up per affiliate (company, else person)."""
    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"""
            WITH c AS (
                SELECT code, COALESCE(affiliate_company, affiliate_name) AS affiliate,
                       affiliate_name, affiliate_company, affiliate_partner_id
                FROM billing.promo_codes
                WHERE COALESCE(affiliate_company, affiliate_name) IS NOT NULL
            )
            SELECT c.affiliate,
                   min(c.affiliate_company), string_agg(DISTINCT c.affiliate_name, ', '),
                   min(c.affiliate_partner_id),
                   count(DISTINCT c.code),
                   COALESCE((SELECT sum(o.discount_usd) FILTER (WHERE {_PAID})
                             FROM billing.payment_orders o
                             WHERE o.promo_code IN (SELECT code FROM c c2 WHERE c2.affiliate = c.affiliate)), 0),
                   COALESCE((SELECT sum(o.amount_usd) FILTER (WHERE {_PAID})
                             FROM billing.payment_orders o
                             WHERE o.promo_code IN (SELECT code FROM c c2 WHERE c2.affiliate = c.affiliate)), 0),
                   (SELECT count(DISTINCT u) FROM (
                        SELECT e.user_id AS u FROM billing.promo_code_events e
                         WHERE e.code IN (SELECT code FROM c c2 WHERE c2.affiliate = c.affiliate)
                        UNION
                        SELECT o.user_id FROM billing.payment_orders o
                         WHERE o.promo_code IN (SELECT code FROM c c2 WHERE c2.affiliate = c.affiliate)
                    ) leads),
                   (SELECT count(DISTINCT o.user_id) FROM billing.payment_orders o
                     WHERE o.promo_code IN (SELECT code FROM c c2 WHERE c2.affiliate = c.affiliate)),
                   (SELECT count(DISTINCT o.user_id) FILTER (WHERE {_PAID}) FROM billing.payment_orders o
                     WHERE o.promo_code IN (SELECT code FROM c c2 WHERE c2.affiliate = c.affiliate))
            FROM c
            GROUP BY c.affiliate
            ORDER BY c.affiliate
            """
        )
        out = []
        for r in cur.fetchall():
            d = {
                "affiliate": r[0],
                "affiliate_company": r[1],
                "affiliate_names": r[2],
                "affiliate_partner_id": r[3],
                "codes": int(r[4]),
            }
            d.update(_funnel(r[5:10]))
            out.append(d)
    return out


def leads(code: str) -> List[Dict[str, Any]]:
    """
    Every customer who touched `code`, furthest funnel stage first: paid, then
    checkout started, then applied only. One row per customer.
    """
    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"""
            WITH touched AS (
                SELECT e.user_id, e.email, e.product, e.created_at,
                       NULL::numeric AS amount_usd, 'applied' AS stage, 1 AS rank
                FROM billing.promo_code_events e WHERE e.code = %s
                UNION ALL
                SELECT o.user_id, o.customer_email, o.product, o.created_at, o.amount_usd,
                       CASE WHEN {_PAID} THEN 'paid' ELSE 'checkout' END,
                       CASE WHEN {_PAID} THEN 3 ELSE 2 END
                FROM billing.payment_orders o WHERE o.promo_code = %s
            ),
            best AS (
                SELECT DISTINCT ON (user_id) user_id, stage, rank, product, amount_usd
                FROM touched ORDER BY user_id, rank DESC, created_at DESC
            )
            SELECT b.user_id,
                   (SELECT t.email FROM touched t WHERE t.user_id = b.user_id AND t.email IS NOT NULL
                     ORDER BY t.created_at DESC LIMIT 1),
                   b.stage, b.product, b.amount_usd,
                   (SELECT min(t.created_at) FROM touched t WHERE t.user_id = b.user_id),
                   (SELECT max(t.created_at) FROM touched t WHERE t.user_id = b.user_id)
            FROM best b
            ORDER BY b.rank DESC, 7 DESC
            """,
            (code, code),
        )
        return [
            {
                "user_id": r[0],
                "email": r[1],
                "stage": r[2],
                "product": r[3],
                "amount_usd": float(r[4]) if r[4] is not None else None,
                "first_seen_at": r[5].isoformat() if r[5] else None,
                "last_seen_at": r[6].isoformat() if r[6] else None,
            }
            for r in cur.fetchall()
        ]


def campaigns() -> List[str]:
    with pg.cursor(commit=False) as cur:
        cur.execute(
            "SELECT DISTINCT campaign FROM billing.promo_codes "
            "WHERE campaign IS NOT NULL ORDER BY campaign"
        )
        return [r[0] for r in cur.fetchall()]
