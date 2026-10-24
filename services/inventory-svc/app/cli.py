"""The nightly ledger-reconciliation job (LLD-07 §9).

python -m app.cli reconcile
"""

import asyncio
import os
import sys

from sf_db import build_engine, build_session_factory

from . import service


async def reconcile() -> int:
    engine = build_engine(
        host=os.environ.get("SF_DB_HOST", "localhost"),
        port=int(os.environ.get("SF_DB_PORT", "5432")),
        database=os.environ.get("SF_DB_NAME", "scaleforge"),
        user=os.environ.get("SF_DB_USER", "svc_inventory"),
        password=os.environ["SF_DB_PASSWORD"],
    )
    factory = build_session_factory(engine)
    mismatches = await service.run_reconciliation(factory)
    if mismatches:
        print(f"reconcile: {len(mismatches)} SKU(s) mismatched:", file=sys.stderr)
        for row in mismatches:
            print(f"  {row}", file=sys.stderr)
    else:
        print("reconcile: no mismatches", file=sys.stderr)
    await engine.dispose()
    return 1 if mismatches else 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "reconcile":
        print("usage: python -m app.cli reconcile", file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(reconcile()))


if __name__ == "__main__":
    main()
