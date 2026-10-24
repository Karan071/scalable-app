"""The nightly abandon-expired-carts Job (LLD-06 §2).

python -m app.cli abandon-expired
"""

import asyncio
import os
import sys

from sf_db import build_engine, build_session_factory, transaction

from . import service


async def abandon_expired() -> int:
    engine = build_engine(
        host=os.environ.get("SF_DB_HOST", "localhost"),
        port=int(os.environ.get("SF_DB_PORT", "5432")),
        database=os.environ.get("SF_DB_NAME", "scaleforge"),
        user=os.environ.get("SF_DB_USER", "svc_cart"),
        password=os.environ["SF_DB_PASSWORD"],
    )
    factory = build_session_factory(engine)
    async with transaction(factory) as session:
        count = await service.abandon_expired_carts(session)
    print(f"abandon-expired: marked {count} cart(s) abandoned", file=sys.stderr)
    await engine.dispose()
    return 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "abandon-expired":
        print("usage: python -m app.cli abandon-expired", file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(abandon_expired()))


if __name__ == "__main__":
    main()
