"""The full-reindex Job (LLD-05 §9).

    python -m app.cli reindex --org-id <uuid>

Pages through catalog-svc's export endpoint and applies every snapshot
through the exact same upsert/tombstone logic the live event consumer
uses (``service.apply_product_snapshot``), then deletes rows for products
no longer in that export (§9 step 3). Safe to run alongside the live
consumer -- no table swap, and the ``indexed_at``-before-job-start guard on
the delete means a product that arrives mid-run is never mistaken for a
deleted one.

Reindexing *every* org (LLD-05 §9's "per org or all") isn't built: it needs
a way to enumerate every org id, which belongs to orgs-svc, not this CLI.
"""

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from uuid import UUID

import httpx
from sf_db import build_engine, build_session_factory, transaction

from . import repository, service
from .catalog_client import CatalogClient, ServiceTokenSource

_PAGE_LIMIT = 48


async def reindex(org_id: UUID) -> int:
    engine = build_engine(
        host=os.environ.get("SF_DB_HOST", "localhost"),
        port=int(os.environ.get("SF_DB_PORT", "5432")),
        database=os.environ.get("SF_DB_NAME", "scaleforge"),
        user=os.environ.get("SF_DB_USER", "svc_search"),
        password=os.environ["SF_DB_PASSWORD"],
    )
    factory = build_session_factory(engine)
    started_at = datetime.now(UTC)
    seen: set[UUID] = set()

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
        catalog = CatalogClient(
            http,
            base_url=os.environ["SF_CATALOG_SVC_BASE_URL"],
            token_source=ServiceTokenSource(
                http,
                token_endpoint=os.environ["SF_OIDC_TOKEN_ENDPOINT"],
                client_id=os.environ.get("SF_SVC_SEARCH_CLIENT_ID", "sf-svc-search"),
                client_secret=os.environ["SF_SVC_SEARCH_CLIENT_SECRET"],
            ),
        )
        cursor: str | None = None
        pages = 0
        while True:
            items, cursor = await catalog.export_page(str(org_id), cursor=cursor, limit=_PAGE_LIMIT)
            if not items:
                break
            async with transaction(factory) as session:
                for item in items:
                    seen.add(UUID(item["product_id"]))
                    await service.apply_product_snapshot(session, item)
            pages += 1
            if cursor is None:
                break
        print(
            f"reindex {org_id}: applied {len(seen)} products over {pages} page(s)", file=sys.stderr
        )

    async with transaction(factory) as session:
        deleted = await repository.delete_stale_after_reindex(
            session, org_id, seen_product_ids=seen, indexed_before=started_at
        )
    print(f"reindex {org_id}: removed {deleted} stale doc(s)", file=sys.stderr)
    await engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    reindex_parser = subparsers.add_parser("reindex")
    reindex_parser.add_argument("--org-id", type=UUID, required=True)
    args = parser.parse_args()

    if args.command == "reindex":
        sys.exit(asyncio.run(reindex(args.org_id)))


if __name__ == "__main__":
    main()
