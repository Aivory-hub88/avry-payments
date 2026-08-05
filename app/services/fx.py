"""
USD -> IDR exchange rate for Midtrans billing.

Midtrans settles in IDR while the pricing page is authored in USD, so every
charge depends on a rate. Hardcoding one silently underprices every sale as the
market moves, so the live rate is fetched and a configurable margin (default 1%)
is added to absorb intra-day drift and card FX spread.

Reliability matters more than freshness here: a payment must never be built from
a missing or absurd rate. The fallback chain is

    fresh fetch  ->  last known good (cached on disk, survives restarts)
                 ->  settings.usd_idr_rate (static floor)

and a fetched rate is only accepted if it passes a sanity band, so a provider
returning 0, null, or a wildly wrong number can't reprice the catalogue.

`current_rate()` is sync and never blocks — it reads the cache. `ensure_fresh()`
is awaited on the request path so a payment always prices against the newest rate
we hold, and a background poller (`run_poller`) refreshes on a fixed interval
independently of traffic, so the rate is current even on a quiet day.

Every successful poll is appended to `billing.fx_rates`, which is both the audit
trail for "why was this order charged that much" and the series behind the admin
dashboard's rate graph.

Note on cadence: the default provider (open.er-api.com) publishes once per ~24h,
so polling more often than that returns the same value. Point `FX_RATE_URL` at an
intraday provider (e.g. Open Exchange Rates, whose free tier updates hourly) to
get real intra-day resolution — the response parser already accepts that shape,
so it's a config change only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# A rate outside this band is treated as a provider error rather than reality.
MIN_PLAUSIBLE_RATE = 10_000
MAX_PLAUSIBLE_RATE = 30_000

_CACHE_PATH = os.path.join(
    os.getenv("FX_CACHE_DIR", "data"), "fx_usd_idr.json"
)

_lock = asyncio.Lock()
_rate: Optional[int] = None          # margin already applied
_fetched_at: float = 0.0
_source: str = "static"


def _apply_margin(base_rate: float) -> int:
    """Add the configured margin to a raw market rate."""
    return int(round(base_rate * (1 + settings.fx_margin_percent / 100.0)))


def _load_cache() -> None:
    """Seed the in-process rate from the on-disk last-known-good value."""
    global _rate, _fetched_at, _source
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rate = int(data["rate"])
        if MIN_PLAUSIBLE_RATE <= rate <= MAX_PLAUSIBLE_RATE:
            _rate = rate
            _fetched_at = float(data.get("fetched_at", 0))
            _source = "cache"
            logger.info("FX seeded from cache: 1 USD = %s IDR", rate)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not read FX cache: %s", e)


def _save_cache(rate: int, base_rate: float) -> None:
    """Persist the last known good rate so a restart doesn't lose it."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(
                {"rate": rate, "base_rate": base_rate, "fetched_at": time.time()}, fh
            )
    except Exception as e:
        logger.warning("Could not write FX cache: %s", e)


_load_cache()


def current_rate() -> int:
    """
    The rate to bill at, margin included. Never raises, never blocks.

    Falls back to the static configured rate when nothing has been fetched yet.
    """
    if _rate is not None:
        return _rate
    return settings.usd_idr_rate


def rate_info() -> dict:
    """Diagnostics for /config and /health."""
    return {
        "usd_idr_rate": current_rate(),
        "source": _source,
        "margin_percent": settings.fx_margin_percent,
        "age_seconds": int(time.time() - _fetched_at) if _fetched_at else None,
    }


def _is_stale() -> bool:
    if _rate is None:
        return True
    return (time.time() - _fetched_at) > settings.fx_ttl_seconds


async def ensure_fresh() -> int:
    """
    Refresh the rate if it has aged past the TTL, then return it.

    A failed refresh is logged and swallowed: an old-but-plausible rate is far
    better than refusing to sell.
    """
    global _rate, _fetched_at, _source

    if not _is_stale():
        return current_rate()

    async with _lock:
        # Another coroutine may have refreshed while we waited.
        if not _is_stale():
            return current_rate()

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(settings.fx_rate_url)
                response.raise_for_status()
                payload = response.json()
        except Exception as e:
            logger.warning(
                "FX refresh failed (%s); continuing with %s IDR from %s",
                e, current_rate(), _source,
            )
            return current_rate()

        base = _extract_idr(payload)
        if base is None:
            logger.warning("FX response had no usable IDR rate; keeping %s", current_rate())
            return current_rate()

        candidate = _apply_margin(base)
        if not (MIN_PLAUSIBLE_RATE <= candidate <= MAX_PLAUSIBLE_RATE):
            logger.error(
                "FX rate %s outside plausible band; refusing it and keeping %s",
                candidate, current_rate(),
            )
            return current_rate()

        _rate = candidate
        _fetched_at = time.time()
        _source = "live"
        _save_cache(candidate, base)
        logger.info(
            "FX refreshed: 1 USD = %s IDR market, billing at %s (+%s%%)",
            base, candidate, settings.fx_margin_percent,
        )
        _record_history(base, candidate, payload)
        return candidate


def _provider_updated_at(payload: object) -> Optional[str]:
    """
    When the provider says it last published, if it tells us.

    Worth recording: it's the difference between "we polled 5 minutes ago" and
    "this number is 20 hours old", which is the honest measure of freshness.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("time_last_update_utc", "time_last_updated_utc", "timestamp", "date"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _record_history(market_rate: float, billing_rate: int, payload: object) -> None:
    """
    Append this observation to billing.fx_rates.

    Never raises: losing a history row must not fail a payment or the poller.
    """
    from app.database import pg

    if not pg.is_configured():
        return

    provider = None
    updated_at = _provider_updated_at(payload)
    if isinstance(payload, dict):
        provider = payload.get("provider") or payload.get("base_code")

    try:
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO billing.fx_rates
                    (base_code, quote_code, market_rate, billing_rate,
                     margin_percent, provider, provider_updated_at)
                VALUES ('USD', 'IDR', %s, %s, %s, %s, %s)
                """,
                (
                    market_rate,
                    billing_rate,
                    settings.fx_margin_percent,
                    str(provider)[:200] if provider else None,
                    # Providers disagree on timestamp format, so it's parsed
                    # tolerantly in Python and stored as NULL if unreadable.
                    _parse_provider_timestamp(updated_at),
                ),
            )
    except Exception as e:
        logger.warning("Could not record FX history: %s", e)


def _parse_provider_timestamp(value: Optional[str]):
    """Best-effort parse of a provider timestamp; None if it can't be read."""
    if not value:
        return None
    from datetime import datetime, timezone

    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",   # open.er-api.com
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


async def run_poller() -> None:
    """
    Refresh the rate on a fixed interval, regardless of whether anyone is paying.

    Runs for the lifetime of the process. Each tick is fully guarded: a provider
    outage logs and waits for the next interval rather than killing the task,
    because a dead poller would silently freeze pricing at the last known rate.
    """
    interval = max(300, settings.fx_poll_seconds)
    logger.info("FX poller started (every %ss)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            # Force a refresh on the poller's schedule rather than the TTL's.
            global _fetched_at
            _fetched_at = 0.0
            rate = await ensure_fresh()
            logger.info("FX poll complete: billing at %s IDR (%s)", rate, _source)
        except asyncio.CancelledError:
            logger.info("FX poller stopped")
            raise
        except Exception as e:
            logger.error("FX poll tick failed: %s", e)


def history(days: int = 30, limit: int = 5000) -> list[dict]:
    """Recorded rate observations, newest first, for the graph and CSV export."""
    from app.database import pg

    if not pg.is_configured():
        return []

    with pg.cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT fetched_at, market_rate, billing_rate, margin_percent,
                   provider, provider_updated_at
            FROM billing.fx_rates
            WHERE quote_code = 'IDR'
              AND fetched_at >= now() - make_interval(days => %s)
            ORDER BY fetched_at DESC
            LIMIT %s
            """,
            (days, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "fetched_at": r[0].isoformat(),
            "market_rate": float(r[1]),
            "billing_rate": int(r[2]),
            "margin_percent": float(r[3]),
            "provider": r[4],
            "provider_updated_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


def _extract_idr(payload: object) -> Optional[float]:
    """Pull the IDR rate out of a provider response, tolerating shape differences."""
    if not isinstance(payload, dict):
        return None
    # open.er-api.com and exchangerate.host both nest under "rates".
    rates = payload.get("rates") or payload.get("data") or payload.get("conversion_rates")
    if isinstance(rates, dict):
        value = rates.get("IDR") or rates.get("idr")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None
