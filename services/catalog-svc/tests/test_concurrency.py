"""Concurrency test against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-14 §5: "Concurrent product edits with the same
ETag -> one success, others 412." Exercised at the ``service`` layer
directly (not through HTTP), same style as inventory-svc's own
``test_concurrency.py`` -- the property under test is the optimistic-lock
``UPDATE ... WHERE version = :expected`` (LLD-05 §4), not routing.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app import repository, service
from app.db import build_catalog_engine, build_catalog_session_factory
from app.models import Product
from app.settings import Settings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    try:
        eng = build_catalog_engine(Settings(), pool_size=20, max_overflow=0, pool_timeout=30.0)
        async with eng.connect():
            pass
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_catalog_session_factory(engine)


async def _seed_product(session_factory: async_sessionmaker[AsyncSession], *, org_id: UUID) -> UUID:
    async with session_factory() as session:
        product = await service.create_product(
            session,
            org_id=org_id,
            actor_id=uuid4(),
            slug="concurrent-tee",
            name="Tee",
            description_md="",
            category_id=None,
        )
        await session.commit()
        return product.id


async def test_concurrent_product_edits_with_the_same_etag_one_succeeds_rest_412(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    org_id = uuid4()
    product_id = await _seed_product(session_factory, org_id=org_id)

    async def edit(name: str) -> Product | None:
        async with session_factory() as session:
            try:
                product = await service.update_product(
                    session,
                    product_id,
                    org_id=org_id,
                    actor_id=uuid4(),
                    expected_version=1,
                    name=name,
                    description_md=None,
                    category_id=None,
                    category_id_set=False,
                )
                await session.commit()
                return product
            except service.VersionConflictError:
                return None

    outcomes = await asyncio.gather(*[edit(f"Tee v{i}") for i in range(20)])

    succeeded = [o for o in outcomes if o is not None]
    assert len(succeeded) == 1

    async with session_factory() as session:
        current = await repository.get_product(session, product_id, org_id=org_id)
    assert current is not None
    assert current.version == 2
    assert current.name == succeeded[0].name
