"""Casbin enforcer wrapper: load/reload off a dedicated sync engine, atomic swap.

ADR-0004 §2/§4: the SQLAlchemy adapter is sync-only, so policy load/reload
runs on a dedicated ``authz_reader`` connection, off the request path, and
is swapped into place as one atomic reference assignment. ``enforce()``
itself is a pure in-memory Casbin call — no I/O, safe to call directly from
an async request (LLD-02 §6: "Casbin's in-memory evaluation is fast enough").
"""

import asyncio
import importlib.resources
import logging
import time
from dataclasses import dataclass

import casbin
from casbin_sqlalchemy_adapter import Adapter
from opentelemetry import trace
from sqlalchemy import Engine, create_engine, text

_tracer = trace.get_tracer(__name__)

#: Past this staleness, callers deny mutating/platform requests (LLD-02 §8).
STALE_WARN_S = 60.0
#: Past this staleness, callers fail readiness entirely (LLD-02 §8).
STALE_FAIL_S = 300.0

# casbin.Enforcer(..., enable_log=False) (our default) skips casbin's own
# logging setup entirely, so its loggers fall through to Python's defaults
# (propagate=True, no level set) and inherit whatever the service's root
# logger is configured to — which means every load/reload dumps the full
# model *and* the entire policy/role table at INFO through our own JSON
# handler (confirmed against live Postgres: ~70+ rows per reload). Casbin's
# own un-applied DEFAULT_LOGGING would set this logger to WARNING; just do
# that part ourselves rather than pulling in its whole logging config.
logging.getLogger("casbin.policy").setLevel(logging.WARNING)


def default_model_path() -> str:
    return str(importlib.resources.files("sf_authz").joinpath("model.conf"))


def build_sync_engine(*, host: str, port: int, database: str, user: str, password: str) -> Engine:
    """``authz_reader``-only engine, used exclusively for policy load/reload."""
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    return create_engine(url, pool_size=2, pool_pre_ping=True)


@dataclass(frozen=True)
class _Snapshot:
    enforcer: casbin.Enforcer
    domain_status: dict[str, str]
    version: int
    loaded_at: float


class PolicyEnforcer:
    """One instance per service process (ADR-0005: embedded enforcement).

    Thread-safe for reads via atomic snapshot swap: ``reload()`` builds a new
    snapshot off to the side and only then replaces the reference in-flight
    requests observe, so no request ever sees a half-built enforcer.
    """

    def __init__(self, *, model_path: str, sync_engine: Engine) -> None:
        self._model_path = model_path
        self._engine = sync_engine
        self._snapshot: _Snapshot | None = None
        # LLD-02 §8 last_confirmed_at: a reload *or* a poll that found the
        # loaded version still current. Staleness is measured from here.
        self._confirmed_at = 0.0

    def load(self) -> None:
        """Synchronous initial load. Call once at startup, before readiness."""
        self._snapshot = self._load_snapshot()
        self._confirmed_at = self._snapshot.loaded_at

    async def reload(self) -> int:
        """Build a fresh snapshot in a thread, then swap. Returns the new version."""
        snapshot = await asyncio.to_thread(self._load_snapshot)
        self._snapshot = snapshot
        self._confirmed_at = snapshot.loaded_at
        return snapshot.version

    def confirm_current(self, version: int) -> bool:
        """A poll found ``policy_version`` at ``version``. If that's the loaded
        version, this enforcer is provably current: reset staleness. Any
        other version is not a confirmation -- newer means a reload is due;
        older means the DB went backwards (e.g. a restore), so fail closed."""
        if version != self._require_snapshot().version:
            return False
        self._confirmed_at = time.monotonic()
        return True

    def _load_snapshot(self) -> _Snapshot:
        # ``create_all_models=False``: the ``casbin_rule`` table is owned by
        # authz-svc's own Alembic migration; the read-only ``authz_reader``
        # connection used here must never attempt DDL.
        adapter = Adapter(self._engine, create_all_models=False)
        enforcer = casbin.Enforcer(self._model_path, adapter)
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT domain, status FROM domain_status")).all()
            version = conn.execute(
                text("SELECT version FROM policy_version WHERE id = 1")
            ).scalar_one()
        return _Snapshot(
            enforcer=enforcer,
            domain_status={row[0]: row[1] for row in rows},
            version=version,
            loaded_at=time.monotonic(),
        )

    async def fetch_version_only(self) -> int:
        """Cheap polling check: just the version counter, no full reload."""

        def _fetch() -> int:
            with self._engine.connect() as conn:
                return conn.execute(
                    text("SELECT version FROM policy_version WHERE id = 1")
                ).scalar_one()

        return await asyncio.to_thread(_fetch)

    def enforce(self, sub: str, dom: str, obj: str, act: str) -> bool:
        # Manual span (LLD-10 §2): the ID attributes here aren't PII, and
        # trace attributes (unlike metric labels) are allowed to carry IDs.
        with _tracer.start_as_current_span(
            "authz.enforce",
            attributes={
                "authz.subject": sub,
                "authz.domain": dom,
                "authz.resource": obj,
                "authz.action": act,
            },
        ) as span:
            allowed = bool(self._require_snapshot().enforcer.enforce(sub, dom, obj, act))
            span.set_attribute("authz.allowed", allowed)
            return allowed

    def permissions_for(self, subject: str, domain: str) -> list[str]:
        """UI-hint list of ``"<resource>:<action>"`` strings (LLD-02 §12) — never
        trusted by the backend, which re-checks every mutation through
        ``enforce()``. Built by re-running ``enforce()`` over the catalog's
        known (resource, action) pairs rather than Casbin's
        ``get_implicit_permissions_for_user``, which filters by an exact
        domain match and so misses this model's wildcard ``p.dom == "*"``
        role templates.
        """
        snap = self._require_snapshot()
        candidates = {(row[2], row[3]) for row in snap.enforcer.get_policy() if len(row) >= 4}
        return sorted(
            f"{obj}:{act}"
            for obj, act in candidates
            if snap.enforcer.enforce(subject, domain, obj, act)
        )

    def domain_status(self, domain: str) -> str:
        """Missing row => ``"active"``.

        Intentional fail-open on *this* check only, until E2.8's event
        consumers keep ``domain_status`` fresh — Casbin's own default-deny
        permission check is unaffected by this.
        """
        return self._require_snapshot().domain_status.get(domain, "active")

    @property
    def current_version(self) -> int:
        return self._require_snapshot().version

    def staleness_s(self) -> float:
        """Seconds since this enforcer was last known current (LLD-02 §8)."""
        self._require_snapshot()
        return time.monotonic() - self._confirmed_at

    def _require_snapshot(self) -> _Snapshot:
        if self._snapshot is None:
            raise RuntimeError("PolicyEnforcer.load() was not called at startup")
        return self._snapshot


def build_enforcer(
    *,
    model_path: str | None = None,
    db_host: str,
    db_port: int,
    db_name: str,
    db_user: str,
    db_password: str,
) -> PolicyEnforcer:
    """One-call constructor for a service's ``lifespan``."""
    engine = build_sync_engine(
        host=db_host, port=db_port, database=db_name, user=db_user, password=db_password
    )
    return PolicyEnforcer(model_path=model_path or default_model_path(), sync_engine=engine)
