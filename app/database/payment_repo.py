"""
The payment ledger, in Postgres.

Replaces the JSON-file store for payment records. Two things this buys that files
could not:

* `lock_for_settlement()` takes a real row lock, so a Midtrans webhook racing the
  browser's confirm call cannot both pass the "already granted?" check. The old
  code guarded this with an in-process asyncio.Lock, which only held within a
  single worker — it could not survive a second container.
* Sorting, filtering and paging happen in the database, so the admin views and
  CSV exports stay usable as the ledger grows.

Records are dicts with the same keys the JSON store used, so callers and the
admin BFF mapping keep working unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.database import pg

logger = logging.getLogger(__name__)

# Columns selected for a full record, in a fixed order shared by every read.
_COLUMNS = (
    "order_id, payment_id, user_id, product, amount_usd, amount_idr, usd_idr_rate, "
    "status, payment_method, payment_type, transaction_status, fraud_status, "
    "midtrans_transaction_id, customer_email, is_mock, granted_at, grant_detail, "
    "grant_error, created_at, updated_at, invoice_number"
)

# Columns an admin may sort by. Whitelisted rather than interpolated, so a
# crafted `sort_by` can't reach the SQL.
SORTABLE = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "amount": "amount_usd",
    "amount_usd": "amount_usd",
    "amount_idr": "amount_idr",
    "status": "status",
    "product": "product",
    "user_id": "user_id",
    "email": "customer_email",
    "payment_method": "payment_method",
    "payment_type": "payment_type",
    "order_id": "order_id",
}


def _row_to_record(row: Tuple) -> Dict[str, Any]:
    """Map a selected row onto the record shape callers expect."""
    return {
        "order_id": row[0],
        "payment_id": row[1],
        "user_id": row[2],
        "product": row[3],
        "amount": float(row[4]) if row[4] is not None else 0.0,
        "amount_idr": int(row[5]) if row[5] is not None else None,
        "usd_idr_rate": int(row[6]) if row[6] is not None else None,
        "status": row[7],
        "payment_method": row[8],
        "payment_type": row[9],
        "transaction_status": row[10],
        "fraud_status": row[11],
        "transaction_id": row[12],
        "customer_email": row[13],
        "is_mock": bool(row[14]),
        "granted_at": row[15].isoformat() if row[15] else None,
        "grant_detail": row[16],
        "grant_error": row[17],
        "created_at": row[18].isoformat() if row[18] else None,
        "updated_at": row[19].isoformat() if row[19] else None,
        "invoice_number": row[20],
    }


def create(record: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a new pending order."""
    with pg.cursor() as cur:
        cur.execute(
            """
            INSERT INTO billing.payment_orders
                (order_id, payment_id, user_id, product, amount_usd, amount_idr,
                 usd_idr_rate, status, payment_method, midtrans_transaction_id,
                 customer_email, is_mock)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING """ + _COLUMNS,
            (
                record["order_id"],
                record["payment_id"],
                record["user_id"],
                record["product"],
                record["amount"],
                record["amount_idr"],
                record.get("usd_idr_rate"),
                record.get("status", "pending"),
                record.get("payment_method", "midtrans"),
                record.get("transaction_id"),
                record.get("customer_email"),
                bool(record.get("is_mock", False)),
            ),
        )
        return _row_to_record(cur.fetchone())


def find_by_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    """Look up one order, without locking."""
    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM billing.payment_orders WHERE order_id = %s",
            (order_id,),
        )
        row = cur.fetchone()
    return _row_to_record(row) if row else None


class SettlementLock:
    """
    A held row lock on one order, for the duration of a settlement attempt.

    Used as an async-friendly context manager around the whole verify-then-grant
    sequence. Because the lock lives in Postgres, concurrent settlement attempts
    serialise even across separate containers — which is what makes granting
    exactly once actually true rather than merely likely.
    """

    def __init__(self, order_id: str):
        self.order_id = order_id
        self._conn = None
        self.record: Optional[Dict[str, Any]] = None

    def __enter__(self) -> "SettlementLock":
        self._conn = pg.connect()
        pg.ensure_schema(self._conn)
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT {_COLUMNS} FROM billing.payment_orders "
            "WHERE order_id = %s FOR UPDATE",
            (self.order_id,),
        )
        row = cur.fetchone()
        cur.close()
        self.record = _row_to_record(row) if row else None
        return self

    def update(self, **fields: Any) -> None:
        """Apply column updates inside the held lock."""
        if not fields:
            return
        allowed = {
            "status", "payment_type", "transaction_status", "fraud_status",
            "midtrans_transaction_id", "granted_at", "grant_detail",
            "grant_error", "gateway_response", "invoice_number",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"not an updatable column: {key}")
            sets.append(f"{key} = %s")
            values.append(
                json.dumps(value, ensure_ascii=False)
                if key in ("grant_detail", "gateway_response") and value is not None
                else value
            )
        sets.append("updated_at = now()")
        values.append(self.order_id)
        cur = self._conn.cursor()
        cur.execute(
            f"UPDATE billing.payment_orders SET {', '.join(sets)} WHERE order_id = %s",
            values,
        )
        cur.close()
        if self.record:
            self.record.update(
                {k: (v.isoformat() if isinstance(v, datetime) else v)
                 for k, v in fields.items()}
            )

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


def mark_granted(lock: SettlementLock, detail: str) -> None:
    """Record that the entitlement for this order has been applied."""
    lock.update(
        status="paid",
        granted_at=datetime.now(timezone.utc),
        grant_detail={"detail": detail},
    )


def assign_invoice_number(lock: "SettlementLock") -> Optional[str]:
    """
    Give a settling order its invoice number, once.

    Drawn from a Postgres sequence inside the settlement transaction, so numbers
    are unique and gap-free across concurrent settlements. Called only on the
    settle path: an abandoned checkout never consumes a number.
    """
    if lock.record and lock.record.get("invoice_number"):
        return lock.record["invoice_number"]

    cur = lock._conn.cursor()
    cur.execute(
        "SELECT 'INV-' || to_char(now(), 'YYYY') || '-' "
        "|| lpad(nextval('billing.invoice_seq')::text, 6, '0')"
    )
    number = cur.fetchone()[0]
    cur.close()
    lock.update(invoice_number=number)
    return number


def query(
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    product: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_mock: bool = True,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Filtered, sorted, paged ledger read. Returns (rows, total_matching).

    `total` is the count before paging, so the UI can show "page 2 of N" without
    fetching everything.
    """
    where: List[str] = ["1=1"]
    params: List[Any] = []

    if user_id:
        where.append("user_id = %s")
        params.append(user_id)
    if status and status != "all":
        where.append("status = %s")
        params.append(status)
    if product and product != "all":
        # Prefix match so "credits" selects every credit pack.
        where.append("(product = %s OR product LIKE %s)")
        params.extend([product, f"{product}_%"])
    if search:
        where.append(
            "(order_id ILIKE %s OR payment_id ILIKE %s OR user_id ILIKE %s "
            "OR customer_email ILIKE %s OR product ILIKE %s)"
        )
        params.extend([f"%{search}%"] * 5)
    if date_from:
        where.append("created_at >= %s")
        params.append(date_from)
    if date_to:
        # Inclusive of the whole end day.
        where.append("created_at < (%s::date + 1)")
        params.append(date_to)
    if not include_mock:
        where.append("is_mock = false")

    clause = " AND ".join(where)
    column = SORTABLE.get(sort_by, "created_at")
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    with pg.cursor(commit=False) as cur:
        cur.execute(
            f"SELECT count(*) FROM billing.payment_orders WHERE {clause}", params
        )
        total = int(cur.fetchone()[0])

        cur.execute(
            f"SELECT {_COLUMNS} FROM billing.payment_orders WHERE {clause} "
            f"ORDER BY {column} {direction} NULLS LAST, order_id "
            "LIMIT %s OFFSET %s",
            params + [max(1, min(limit, 1000)), max(0, offset)],
        )
        rows = [_row_to_record(r) for r in cur.fetchall()]

    return rows, total


def summary(include_mock: bool = False) -> Dict[str, Any]:
    """Headline totals for the admin dashboard cards."""
    with pg.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
              count(*)                                             AS orders,
              count(*) FILTER (WHERE status = 'paid')              AS paid,
              count(*) FILTER (WHERE status = 'pending')           AS pending,
              -- Manual payments waiting on an admin are a queue, not a fault, so
              -- they are counted separately instead of inflating "problem".
              count(*) FILTER (WHERE status = 'awaiting_verification')
                                                                   AS awaiting_verification,
              count(*) FILTER (WHERE status NOT IN
                  ('paid','pending','awaiting_verification'))      AS problem,
              COALESCE(sum(amount_usd) FILTER (WHERE status = 'paid'), 0) AS revenue_usd,
              -- Only sums rows whose IDR amount is known, so imported
              -- legacy records can't understate revenue.
              COALESCE(sum(amount_idr) FILTER (
                  WHERE status = 'paid' AND amount_idr IS NOT NULL), 0) AS revenue_idr,
              count(*) FILTER (WHERE status = 'paid' AND amount_idr IS NULL)
                                                                   AS paid_without_idr
            FROM billing.payment_orders
            WHERE (%s OR is_mock = false)
            """,
            (include_mock,),
        )
        r = cur.fetchone()
    return {
        "orders": int(r[0]),
        "paid": int(r[1]),
        "pending": int(r[2]),
        "awaiting_verification": int(r[3]),
        "problem": int(r[4]),
        "revenue_usd": float(r[5]),
        "revenue_idr": int(r[6]),
        # Surfaced rather than hidden: revenue_idr excludes these, so the UI can
        # say so instead of quietly reporting a low number.
        "paid_without_idr": int(r[7]),
    }


def import_legacy(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Backfill records from the old JSON store. Idempotent on order_id.

    Legacy rows predate several columns, so missing values are filled with
    conservative defaults rather than guesses: no IDR amount is invented, and a
    record already marked paid keeps a granted_at so it is never re-granted.
    """
    imported = skipped = 0
    with pg.cursor() as cur:
        for rec in records:
            order_id = rec.get("order_id")
            payment_id = rec.get("payment_id")
            if not order_id or not payment_id:
                skipped += 1
                continue
            amount = rec.get("amount") or 0
            if amount <= 0:
                skipped += 1
                continue
            status = rec.get("status") or "pending"
            # Anything already settled must carry granted_at, or a later webhook
            # for that order would grant the entitlement a second time.
            granted_at = rec.get("granted_at") or (
                rec.get("updated_at") if status in ("paid", "completed") else None
            )
            cur.execute(
                """
                INSERT INTO billing.payment_orders
                    (order_id, payment_id, user_id, product, amount_usd, amount_idr,
                     status, payment_method, midtrans_transaction_id, is_mock,
                     granted_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s::timestamptz, now()),
                        COALESCE(%s::timestamptz, now()))
                ON CONFLICT (order_id) DO NOTHING
                """,
                (
                    order_id,
                    payment_id,
                    rec.get("user_id") or "unknown",
                    rec.get("product") or "unknown",
                    amount,
                    rec.get("amount_idr"),        # NULL when historically unknown
                    "paid" if status == "completed" else status,
                    rec.get("payment_method") or "midtrans",
                    rec.get("transaction_id"),
                    bool(rec.get("is_mock", False)),
                    granted_at,
                    rec.get("created_at"),
                    rec.get("updated_at"),
                ),
            )
            if cur.rowcount:
                imported += 1
            else:
                skipped += 1
    return {"imported": imported, "skipped": skipped}
