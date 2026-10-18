"""Enforcer tests against an in-memory SQLite stand-in for the authz schema.

Builds the minimal subset of authz-svc's real schema (``casbin_rule``,
``domain_status``, ``policy_version``) that ``PolicyEnforcer`` reads, seeded
with the LLD-02 §3.1 example policy rows.
"""

import asyncio
from collections.abc import Iterator

import pytest
from casbin_sqlalchemy_adapter import Base as AdapterBase
from sf_authz.enforcer import PolicyEnforcer, default_model_path
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import StaticPool


@pytest.fixture
def sync_engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    AdapterBase.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE domain_status (domain TEXT PRIMARY KEY, status TEXT)"))
        conn.execute(text("CREATE TABLE policy_version (id INTEGER PRIMARY KEY, version INTEGER)"))
        conn.execute(text("INSERT INTO policy_version (id, version) VALUES (1, 1)"))
    yield engine
    engine.dispose()


def _seed(engine: Engine, rows: list[tuple[str, ...]]) -> None:
    with engine.begin() as conn:
        for row in rows:
            cols = ["ptype"] + [f"v{i}" for i in range(len(row) - 1)]
            placeholders = ", ".join(f":{c}" for c in cols)
            conn.execute(
                text(f"INSERT INTO casbin_rule ({', '.join(cols)}) VALUES ({placeholders})"),
                dict(zip(cols, row, strict=True)),
            )


def _enforcer(engine: Engine) -> PolicyEnforcer:
    enforcer = PolicyEnforcer(model_path=default_model_path(), sync_engine=engine)
    enforcer.load()
    return enforcer


def test_role_template_matches_any_org_domain(sync_engine: Engine) -> None:
    _seed(
        sync_engine,
        [
            ("p", "role:catalog_manager", "*", "product", "*", "allow"),
            ("g", "user:A", "role:catalog_manager", "org:O1"),
        ],
    )
    enforcer = _enforcer(sync_engine)

    assert enforcer.enforce("user:A", "org:O1", "product", "update") is True
    assert enforcer.enforce("user:A", "org:O2", "product", "update") is False


def test_platform_role_never_matches_org_domain(sync_engine: Engine) -> None:
    _seed(
        sync_engine,
        [
            ("p", "role:super_user", "platform", "*", "*", "allow"),
            ("g", "user:S", "role:super_user", "platform"),
        ],
    )
    enforcer = _enforcer(sync_engine)

    assert enforcer.enforce("user:S", "platform", "order", "read") is True
    assert enforcer.enforce("user:S", "org:O1", "order", "read") is False


def test_explicit_deny_overrides_allow(sync_engine: Engine) -> None:
    _seed(
        sync_engine,
        [
            ("p", "role:platform_operator", "platform", "reconciliation", "*", "allow"),
            ("p", "role:platform_operator", "platform", "identity_config", "*", "deny"),
            ("g", "user:P", "role:platform_operator", "platform"),
        ],
    )
    enforcer = _enforcer(sync_engine)

    assert enforcer.enforce("user:P", "platform", "reconciliation", "replay") is True
    assert enforcer.enforce("user:P", "platform", "identity_config", "update") is False


def test_reload_picks_up_new_g_row(sync_engine: Engine) -> None:
    _seed(sync_engine, [("p", "role:inventory_manager", "*", "stock", "adjust", "allow")])
    enforcer = _enforcer(sync_engine)
    assert enforcer.enforce("user:N", "org:O1", "stock", "adjust") is False

    _seed(sync_engine, [("g", "user:N", "role:inventory_manager", "org:O1")])
    before_version = enforcer.current_version
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE policy_version SET version = version + 1 WHERE id = 1"))
    asyncio.run(enforcer.reload())

    assert enforcer.current_version == before_version + 1
    assert enforcer.enforce("user:N", "org:O1", "stock", "adjust") is True


def test_domain_status_defaults_active_when_missing_row(sync_engine: Engine) -> None:
    enforcer = _enforcer(sync_engine)
    assert enforcer.domain_status("org:unknown") == "active"


def test_domain_status_reflects_seeded_row(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO domain_status (domain, status) VALUES ('org:O1', 'suspended')")
        )
    enforcer = _enforcer(sync_engine)
    assert enforcer.domain_status("org:O1") == "suspended"
