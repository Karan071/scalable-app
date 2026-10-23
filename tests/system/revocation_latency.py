"""Role-revocation latency (LLD-02 §8, §14; delivery plan M2 exit evidence).

    make test-revocation-latency            # 200 trials per scenario
    make test-revocation-latency TRIALS=20  # quick run

Per LLD-02 §14: "revoke -> poll the endpoint until 403; record the
distribution over 200 trials, watcher on and off". Each fixture is its own
org with an owner (A) and an org_admin member (B), built entirely through
the real APIs and event pipeline. Fixtures run concurrently; each runs its
share of trials sequentially. Latency is measured from *sending* the
triggering request to B's first 403, so it overstates by one request
round-trip.

Scenarios (target p99):

- direct (5 s): A revokes B's org_admin in authz-svc; B polls orgs-svc
  GET invitations. Measures authz commit -> redis -> orgs-svc's watcher.
- membership (5 s): A disables B's membership in orgs-svc; B polls authz-svc
  GET role-assignments. Measures orgs commit -> relay -> broker -> authz
  consumer -> authz-svc's watcher.
- watcher-off (15 s): as `direct`, with orgs-svc on an unreachable Redis, so
  only its 10 s polling fallback propagates the change.

`membership` polls authz-svc, not orgs-svc: orgs-svc checks membership in
its own DB and denies instantly, which would measure nothing about
propagation.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid7

import asyncpg
import httpx
from harness import (
    ENV,
    INVITATION_KEYS,
    REPO_ROOT,
    TEST_DB,
    FakeIdP,
    ServiceProcess,
    bootstrap_super_user,
    service_env,
    wait_until,
)
from sf_common import TokenCipher

ORGS_PORT, AUTHZ_PORT = 18101, 18102
TARGETS_S = {"direct": 5.0, "membership": 5.0, "watcher-off": 15.0}
TRIAL_TIMEOUT_S = 60.0

_cipher = TokenCipher.from_base64_keys(INVITATION_KEYS, "k1")


@dataclass
class Fixture:
    org_id: str
    a: UUID
    b: UUID
    b_membership_id: str


class Driver:
    def __init__(self, idp: FakeIdP, http: httpx.AsyncClient) -> None:
        self.idp = idp
        self.http = http
        self.orgs = f"http://127.0.0.1:{ORGS_PORT}"
        self.authz = f"http://127.0.0.1:{AUTHZ_PORT}"

    def _auth(self, uid: UUID) -> dict[str, str]:
        return {"authorization": f"Bearer {self.idp.user_token(uid, email=_email(uid))}"}

    async def call(self, method: str, url: str, uid: UUID, **kwargs: object) -> httpx.Response:
        headers = {**self._auth(uid), **kwargs.pop("headers", {})}  # type: ignore[dict-item]
        return await self.http.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]

    async def ok(self, method: str, url: str, uid: UUID, **kwargs: object) -> httpx.Response:
        resp = await self.call(method, url, uid, **kwargs)
        if resp.status_code != 200:
            raise RuntimeError(f"{method} {url} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    async def status(self, url: str, uid: UUID) -> int:
        return (await self.call("GET", url, uid)).status_code


def _email(uid: UUID) -> str:
    return f"rl-{uid}@example.com"


async def _invitation_token(org_id: str, email: str) -> str:
    conn = await asyncpg.connect(
        host="localhost", database=TEST_DB, user="svc_orgs", password=ENV["POSTGRES_APP_PASSWORD"]
    )
    try:
        row = await conn.fetchrow(
            "SELECT payload FROM orgs.outbox WHERE payload->>'type' = 'invitation.created' "
            "AND payload->'data'->>'org_id' = $1 AND payload->'data'->>'email' = $2 "
            "ORDER BY occurred_at DESC, id DESC LIMIT 1",
            org_id,
            email,
        )
    finally:
        await conn.close()
    data = json.loads(row["payload"])["data"]
    return _cipher.decrypt(data["token_enc"], aad=data["invitation_id"].encode())


async def build_fixture(d: Driver, admin: UUID) -> Fixture:
    a, b = uuid7(), uuid7()
    org = (
        await d.ok(
            "POST",
            f"{d.orgs}/v1/platform/orgs",
            admin,
            json={
                "slug": f"rl-{uuid4().hex[:16]}",
                "name": "Revocation Latency",
                "currency": "usd",
                "owner_email": _email(a),
            },
        )
    ).json()
    org_id = org["id"]
    invitations = f"{d.orgs}/v1/orgs/{org_id}/invitations"

    # A accepts the owner invitation; org_admin arrives via the real pipeline.
    token = await _invitation_token(org_id, _email(a))
    await d.ok("POST", f"{d.orgs}/v1/invitations:accept", a, json={"token": token})
    await wait_until(status_is(d, invitations, a, 200), timeout_s=TRIAL_TIMEOUT_S)

    await d.ok("POST", invitations, a, json={"email": _email(b), "roles_requested": ["org_admin"]})
    token = await _invitation_token(org_id, _email(b))
    await d.ok("POST", f"{d.orgs}/v1/invitations:accept", b, json={"token": token})
    await wait_until(status_is(d, invitations, b, 200), timeout_s=TRIAL_TIMEOUT_S)

    members = (await d.ok("GET", f"{d.orgs}/v1/orgs/{org_id}/members", a)).json()
    b_membership_id = next(m["id"] for m in members if m["user_id"] == str(b))
    return Fixture(org_id, a, b, b_membership_id)


def status_is(d: Driver, url: str, uid: UUID, expected: int):  # type: ignore[no-untyped-def]
    async def check() -> bool:
        return await d.status(url, uid) == expected

    return check


async def current_org_admin_assignment(d: Driver, f: Fixture) -> dict:
    # Fetched per trial: suspend/reactivate (membership trials) bump `version`.
    assignments = (
        await d.ok(
            "GET",
            f"{d.authz}/v1/orgs/{f.org_id}/role-assignments",
            f.a,
            params={"user_id": str(f.b)},
        )
    ).json()
    return next(x for x in assignments if x["role_id"] == "org_admin" and x["status"] == "active")


async def trial_direct(d: Driver, f: Fixture) -> float:
    invitations = f"{d.orgs}/v1/orgs/{f.org_id}/invitations"
    assignment = await current_org_admin_assignment(d, f)
    started = time.monotonic()
    await d.ok(
        "DELETE",
        f"{d.authz}/v1/orgs/{f.org_id}/role-assignments/{assignment['id']}",
        f.a,
        headers={"if-match": str(assignment["version"])},
    )
    await wait_until(status_is(d, invitations, f.b, 403), timeout_s=TRIAL_TIMEOUT_S)
    latency = time.monotonic() - started

    # Restore for the next trial (not measured).
    await d.ok(
        "POST",
        f"{d.authz}/v1/orgs/{f.org_id}/role-assignments",
        f.a,
        json={
            "user_id": str(f.b),
            "role_id": "org_admin",
            "membership_id": f.b_membership_id,
        },
    )
    await wait_until(status_is(d, invitations, f.b, 200), timeout_s=TRIAL_TIMEOUT_S)
    return latency


async def trial_membership(d: Driver, f: Fixture) -> float:
    member = f"{d.orgs}/v1/orgs/{f.org_id}/members/{f.b_membership_id}"
    observe = f"{d.authz}/v1/orgs/{f.org_id}/role-assignments"
    started = time.monotonic()
    await d.ok("POST", f"{member}:disable", f.a, json={"reason": "revocation latency trial"})
    await wait_until(status_is(d, observe, f.b, 403), timeout_s=TRIAL_TIMEOUT_S)
    latency = time.monotonic() - started

    await d.ok("POST", f"{member}:reactivate", f.a)
    await wait_until(status_is(d, observe, f.b, 200), timeout_s=TRIAL_TIMEOUT_S)
    return latency


async def run_trials(trial, d: Driver, fixtures: list[Fixture], total: int) -> list[float]:  # type: ignore[no-untyped-def]
    base, extra = divmod(total, len(fixtures))
    per_fixture = [base + (1 if i < extra else 0) for i in range(len(fixtures))]

    async def run(f: Fixture, n: int) -> list[float]:
        return [await trial(d, f) for _ in range(n)]

    results = await asyncio.gather(*(run(f, n) for f, n in zip(fixtures, per_fixture, strict=True)))
    return [x for r in results for x in r]


def _pct(samples: list[float], p: float) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[int(p) - 1]


def summarize(name: str, samples: list[float]) -> dict:
    return {
        "scenario": name,
        "trials": len(samples),
        "p50": statistics.median(samples),
        "p95": _pct(samples, 95),
        "p99": _pct(samples, 99),
        "max": max(samples),
        "target_p99": TARGETS_S[name],
        "pass": _pct(samples, 99) <= TARGETS_S[name],
    }


async def main(args: argparse.Namespace) -> int:
    idp = FakeIdP()
    admin = uuid7()
    bootstrap_super_user(admin)
    authz = ServiceProcess(
        "authz-svc",
        port=AUTHZ_PORT,
        env=service_env(
            "authz-svc",
            idp,
            SF_ORGS_SVC_BASE_URL=f"http://127.0.0.1:{ORGS_PORT}",
        ),
    )
    orgs = ServiceProcess("orgs-svc", port=ORGS_PORT, env=service_env("orgs-svc", idp))
    results: list[dict] = []
    try:
        await asyncio.gather(authz.start(), orgs.start())
        async with httpx.AsyncClient(timeout=30.0) as http:
            d = Driver(idp, http)
            print(f"building {args.concurrency} fixtures ...", flush=True)
            fixtures = await asyncio.gather(
                *(build_fixture(d, admin) for _ in range(args.concurrency))
            )

            for name, trial in (("direct", trial_direct), ("membership", trial_membership)):
                if name in args.scenarios:
                    print(f"{name}: {args.trials} trials ...", flush=True)
                    results.append(
                        summarize(name, await run_trials(trial, d, fixtures, args.trials))
                    )
                    print(f"  {results[-1]}", flush=True)

            if "watcher-off" in args.scenarios:
                # Restart only the enforcing service, with Redis unreachable:
                # its watcher falls back to the 10 s poll (LLD-02 §8).
                orgs.stop()
                orgs = ServiceProcess(
                    "orgs-svc",
                    port=ORGS_PORT,
                    label="orgs-svc-watcher-off",
                    env=service_env("orgs-svc", idp, SF_REDIS_CACHE_URL="redis://127.0.0.1:1/0"),
                )
                await orgs.start()
                print(f"watcher-off: {args.trials} trials ...", flush=True)
                results.append(
                    summarize(
                        "watcher-off", await run_trials(trial_direct, d, fixtures, args.trials)
                    )
                )
                print(f"  {results[-1]}", flush=True)
    finally:
        orgs.stop()
        authz.stop()
        idp.stop()

    write_report(args, results)
    return 0 if all(r["pass"] for r in results) else 1


def write_report(args: argparse.Namespace, results: list[dict]) -> None:
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    rows = "\n".join(
        f"| {r['scenario']} | {r['trials']} | {r['p50']:.3f} | {r['p95']:.3f} | {r['p99']:.3f} "
        f"| {r['max']:.3f} | {r['target_p99']:.0f} | {'pass' if r['pass'] else '**FAIL**'} |"
        for r in results
    )
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# Role-revocation latency — {datetime.now(UTC):%Y-%m-%d}

Generated by `tests/system/revocation_latency.py` (LLD-02 §8 targets, §14 method).

## Environment

Local Compose stack, single host. One authz-svc and one orgs-svc process
(uvicorn, no replicas), isolated test DB/vhost (`make test-env`), fake IdP.
Git `{sha}`{" (uncommitted changes)" if dirty else ""}. Not production-representative.

## Workload

{args.trials} trials per scenario across {args.concurrency} concurrent fixtures
(one org each). Latency = from sending the triggering request until the
member's first 403, polled every 25 ms.

## Results (seconds)

| Scenario | Trials | p50 | p95 | p99 | max | Target p99 | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
{rows}
""")
    print(f"report: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--scenarios", default="direct,membership,watcher-off")
    parser.add_argument(
        "--report",
        default=str(
            REPO_ROOT / "docs/reports/perf" / f"{datetime.now(UTC):%Y-%m-%d}-revocation-latency.md"
        ),
    )
    a = parser.parse_args()
    a.scenarios = set(a.scenarios.split(","))
    sys.exit(asyncio.run(main(a)))
