#!/usr/bin/env python3
"""
One-off backfill: JSON payment files -> billing.payment_orders.

Run inside the avry-payments container:

    python scripts/migrate_ledger.py --dry-run   # report only, change nothing
    python scripts/migrate_ledger.py             # import
    python scripts/migrate_ledger.py --verify    # compare both stores

Idempotent: re-running imports nothing new, because the ledger is keyed on
order_id and the insert is ON CONFLICT DO NOTHING. That makes it safe to run
again if the first attempt is interrupted.

The JSON files are never deleted — verify first, then archive them by hand.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--verify", action="store_true", help="compare the two stores")
    args = parser.parse_args()

    from app.database import payment_repo, pg
    from app.database.db_service import DatabaseService

    if not pg.is_configured():
        print("DATABASE_URL is not set — nothing to migrate into.")
        return 1

    legacy = DatabaseService().load_all_json("payments")
    print(f"JSON store:     {len(legacy)} record(s)")

    rows, total = payment_repo.query(limit=1)
    print(f"Postgres ledger: {total} row(s) before")

    if args.verify:
        all_rows, _ = payment_repo.query(limit=10_000)
        in_pg = {r["order_id"] for r in all_rows}
        in_json = {r.get("order_id") for r in legacy if r.get("order_id")}
        missing = in_json - in_pg
        extra = in_pg - in_json
        print(f"in JSON only:    {len(missing)}")
        for order_id in sorted(missing)[:20]:
            print(f"  - {order_id}")
        print(f"in Postgres only: {len(extra)} (expected: anything created since cutover)")
        return 0 if not missing else 2

    unusable = [r for r in legacy if not r.get("order_id") or not r.get("payment_id")]
    if unusable:
        print(f"{len(unusable)} record(s) lack an order_id/payment_id and will be skipped")

    if args.dry_run:
        mock = sum(1 for r in legacy if r.get("is_mock"))
        print(f"would import up to {len(legacy) - len(unusable)} record(s) "
              f"({mock} flagged is_mock)")
        return 0

    result = payment_repo.import_legacy(legacy)
    print(f"imported: {result['imported']}   skipped (already present/unusable): {result['skipped']}")

    _, after = payment_repo.query(limit=1)
    print(f"Postgres ledger: {after} row(s) after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
