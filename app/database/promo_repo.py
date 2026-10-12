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
    "created_at, updated_at"
)


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
      ) AS spent_by_user
    FROM billing.payment_orders
    WHERE promo_code = %s
"""


def usage_in_cursor(cur, code: str, user_id: str) -> Dict[str, int]:
    """Redemption counts for `code`, from the caller's own cursor/transaction."""
    cur.execute(
        _USED_SQL,
        (settings.payment_expiry_hours, user_id, user_id, code),
    )
    spent, held, by_user = cur.fetchone()
    return {
        "total_used": int(spent) + int(held),
        "spent": int(spent),
        "used_by_user": int(by_user),
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
                     campaign, note, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (code) DO NOTHING
                RETURNING code
                """,
                (
                    r["code"], r["discount_type"], r["discount_value"],
                    r.get("max_redemptions"), r.get("per_user_limit", 1),
                    r.get("valid_from"), r.get("expires_at"), r.get("products"),
                    r.get("active", True), r.get("campaign"), r.get("note"),
                    r.get("created_by"),
                ),
            )
            if cur.fetchone():
                created.append(r["code"])
    return created


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
        where.append("(c.code ILIKE %s OR c.note ILIKE %s OR c.campaign ILIKE %s)")
        params.extend([f"%{search}%"] * 3)
    if campaign:
        where.append("c.campaign = %s")
        params.append(campaign)

    redeemed = (
        "(SELECT count(*) FROM billing.payment_orders o WHERE o.promo_code = c.code "
        "AND (o.status = 'paid' OR o.granted_at IS NOT NULL "
        "OR o.status = 'awaiting_verification'))"
    )
    if status == "active":
        where.append(
            "c.active AND (c.expires_at IS NULL OR c.expires_at > now()) "
            f"AND (c.max_redemptions IS NULL OR {redeemed} < c.max_redemptions)"
        )
    elif status == "inactive":
        where.append("NOT c.active")
    elif status == "expired":
        where.append("c.expires_at IS NOT NULL AND c.expires_at <= now()")
    elif status == "exhausted":
        where.append(f"c.max_redemptions IS NOT NULL AND {redeemed} >= c.max_redemptions")

    clause = " AND ".join(where)
    cols = ", ".join(f"c.{c.strip()}" for c in _COLUMNS.split(","))
    with pg.cursor(commit=False) as cur:
        cur.execute(f"SELECT count(*) FROM billing.promo_codes c WHERE {clause}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"""
            SELECT {cols}, {redeemed} AS redeemed,
                   COALESCE((SELECT sum(o.discount_usd) FROM billing.payment_orders o
                             WHERE o.promo_code = c.code
                               AND (o.status = 'paid' OR o.granted_at IS NOT NULL)), 0)
                       AS discount_given_usd
            FROM billing.promo_codes c
            WHERE {clause}
            ORDER BY c.created_at DESC, c.code
            LIMIT %s OFFSET %s
            """,
            params + [max(1, min(limit, 500)), max(0, offset)],
        )
        out = []
        for r in cur.fetchall():
            d = _row(r[:14])
            d["redeemed"] = int(r[14])
            d["discount_given_usd"] = float(r[15])
            out.append(d)
    return out, total


def campaigns() -> List[str]:
    with pg.cursor(commit=False) as cur:
        cur.execute(
            "SELECT DISTINCT campaign FROM billing.promo_codes "
            "WHERE campaign IS NOT NULL ORDER BY campaign"
        )
        return [r[0] for r in cur.fetchall()]
