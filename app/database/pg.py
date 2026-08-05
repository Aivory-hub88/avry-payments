"""
Postgres access for avry-payments.

The service historically persisted everything as JSON files (see db_service).
Money records and the FX audit trail need real transactions, row locking and
queryability, so those live here instead.

Follows the same shape as avry-backend's credit_service: synchronous psycopg2 per
call, because the production entrypoint doesn't run an async pool lifespan. Calls
are short and infrequent relative to request volume, so the simplicity is worth
more than pooling here.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_schema_lock = threading.Lock()
_schema_ready = False

_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS billing;

-- Every FX poll, kept as an audit trail and as the series behind the admin graph.
-- `market_rate` is what the provider published; `billing_rate` is what we
-- actually charge (market + margin). Storing both means a past charge can always
-- be explained.
CREATE TABLE IF NOT EXISTS billing.fx_rates (
    id                  BIGSERIAL PRIMARY KEY,
    base_code           TEXT        NOT NULL DEFAULT 'USD',
    quote_code          TEXT        NOT NULL DEFAULT 'IDR',
    market_rate         NUMERIC(14,6) NOT NULL,
    billing_rate        INTEGER     NOT NULL,
    margin_percent      NUMERIC(6,3) NOT NULL,
    provider            TEXT,
    provider_updated_at TIMESTAMPTZ,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fx_rates_fetched_idx
    ON billing.fx_rates (quote_code, fetched_at DESC);

-- The payment ledger. Keyed on the Midtrans order id, which is the natural
-- idempotency key: one order can only ever be settled once.
CREATE TABLE IF NOT EXISTS billing.payment_orders (
    order_id                TEXT PRIMARY KEY,
    payment_id              TEXT NOT NULL UNIQUE,
    user_id                 TEXT NOT NULL,
    product                 TEXT NOT NULL,
    amount_usd              NUMERIC(12,2) NOT NULL CHECK (amount_usd > 0),
    -- NULL means "not known" (legacy rows imported from the JSON store);
    -- a present value must still be positive.
    amount_idr              BIGINT        CHECK (amount_idr IS NULL OR amount_idr > 0),
    usd_idr_rate            INTEGER,
    status                  TEXT NOT NULL DEFAULT 'pending',
    payment_method          TEXT NOT NULL DEFAULT 'midtrans',
    payment_type            TEXT,
    transaction_status      TEXT,
    fraud_status            TEXT,
    midtrans_transaction_id TEXT,
    customer_email          TEXT,
    is_mock                 BOOLEAN NOT NULL DEFAULT false,
    granted_at              TIMESTAMPTZ,
    grant_detail            JSONB,
    grant_error             TEXT,
    gateway_response        JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payment_orders_user_idx    ON billing.payment_orders (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS payment_orders_status_idx  ON billing.payment_orders (status);
CREATE INDEX IF NOT EXISTS payment_orders_created_idx ON billing.payment_orders (created_at DESC);
CREATE INDEX IF NOT EXISTS payment_orders_product_idx ON billing.payment_orders (product);

-- In-app notifications for both end users and admins.
CREATE TABLE IF NOT EXISTS billing.notifications (
    id         BIGSERIAL PRIMARY KEY,
    audience   TEXT NOT NULL,              -- 'user' | 'admin'
    user_id    TEXT,                       -- NULL for admin-wide notices
    type       TEXT NOT NULL,              -- e.g. 'payment_settled'
    title      TEXT NOT NULL,
    body       TEXT,
    meta       JSONB,
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notifications_user_idx
    ON billing.notifications (audience, user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS notifications_unread_idx
    ON billing.notifications (audience, read_at, created_at DESC);

-- Human-facing invoice numbers, assigned only when an order actually settles:
-- an abandoned checkout must not consume a number, or the sequence develops
-- gaps that look like missing invoices to an accountant.
CREATE SEQUENCE IF NOT EXISTS billing.invoice_seq;
ALTER TABLE billing.payment_orders
    ADD COLUMN IF NOT EXISTS invoice_number TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS payment_orders_invoice_number_idx
    ON billing.payment_orders (invoice_number)
    WHERE invoice_number IS NOT NULL;
"""


def dsn() -> str:
    """The Postgres DSN, or raise if the service isn't configured for it."""
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL not set — Postgres-backed features unavailable")
    return value


def is_configured() -> bool:
    """Whether Postgres is available at all (lets callers degrade gracefully)."""
    return bool(os.getenv("DATABASE_URL"))


def connect():
    """Open a short-lived connection."""
    import psycopg2

    return psycopg2.connect(dsn(), connect_timeout=5)


def ensure_schema(conn=None) -> None:
    """
    Create the billing tables if they don't exist. Runs at most once per process.

    Idempotent and safe to call concurrently — the guard only avoids redundant
    round trips, and every statement is IF NOT EXISTS regardless.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        own = conn is None
        c = connect() if own else conn
        try:
            with c.cursor() as cur:
                cur.execute(_SCHEMA_SQL)
            c.commit()
            _schema_ready = True
            logger.info("billing schema ready")
        finally:
            if own:
                c.close()


@contextmanager
def cursor(commit: bool = True):
    """
    Yield a cursor on a fresh connection, committing on clean exit.

    Rolls back on any exception so a partially-applied money change can never be
    left behind.
    """
    conn = connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
