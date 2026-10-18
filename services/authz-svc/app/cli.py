"""One-time K8s Job CLI (LLD-02 §11.6).

    python -m app.cli bootstrap-super-user --user-id <uuid>

Refuses if a ``super_user`` assignment already exists. Runs with a
dedicated, short-lived credential, outside the normal escalation-guard
rules — by definition no super_user exists yet to have granted anything.
"""

import argparse
import asyncio
import os
import sys
from uuid import UUID

from sf_db import build_engine, build_session_factory, transaction

from . import repository


async def bootstrap_super_user(user_id: UUID) -> int:
    # Deliberately reads only the DB env vars (mirroring migrations/env.py),
    # not the full service Settings — this Job needs no OIDC/orgs-svc config,
    # only a dedicated, short-lived DB credential (LLD-02 §11.6).
    engine = build_engine(
        host=os.environ.get("SF_DB_HOST", "localhost"),
        port=int(os.environ.get("SF_DB_PORT", "5432")),
        database=os.environ.get("SF_DB_NAME", "scaleforge"),
        user=os.environ.get("SF_DB_USER", "svc_authz"),
        password=os.environ["SF_DB_PASSWORD"],
    )
    factory = build_session_factory(engine)
    try:
        async with transaction(factory) as session:
            existing = await repository.count_active_assignments_for_role(session, "super_user")
            if existing > 0:
                print("refusing: a super_user already exists", file=sys.stderr)
                return 1
            await repository.insert_role_assignment(
                session,
                user_id=user_id,
                role_id="super_user",
                domain="platform",
                org_id=None,
                membership_id=None,
                granted_by=user_id,
                reason="initial bootstrap",
            )
            await repository.insert_casbin_g_row(
                session, user_id=user_id, role_id="super_user", domain="platform"
            )
            await repository.bump_policy_version(session)
        print(f"bootstrapped super_user for {user_id}")
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = subparsers.add_parser("bootstrap-super-user")
    bootstrap_parser.add_argument("--user-id", type=UUID, required=True)
    args = parser.parse_args()

    if args.command == "bootstrap-super-user":
        sys.exit(asyncio.run(bootstrap_super_user(args.user_id)))


if __name__ == "__main__":
    main()
