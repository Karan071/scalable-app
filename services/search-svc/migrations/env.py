import asyncio
import os
from logging.config import fileConfig

from alembic import context
from app.models import Base
from sf_db import build_engine
from sqlalchemy import text
from sqlalchemy.engine import Connection

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    # lock_timeout bounds a migration against a long-held lock (LLD-09 §1).
    connection.execute(text("SET lock_timeout = '5s'"))
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = build_engine(
        host=os.environ.get("SF_DB_HOST", "localhost"),
        port=int(os.environ.get("SF_DB_PORT", "5432")),
        database=os.environ.get("SF_DB_NAME", "scaleforge"),
        user=os.environ.get("SF_DB_USER", "svc_search"),
        password=os.environ["SF_DB_PASSWORD"],
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


asyncio.run(run_async_migrations())
