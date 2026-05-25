"""
Async SQLAlchemy engine and session management for SQLite.

Uses aiosqlite for async access with WAL mode enabled for
concurrent read/write support across multiple connections.
"""

import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base
from ..paths import get_data_dir

logger = logging.getLogger(__name__)

# Database file location: ~/.magestic-ai/data.db
DATABASE_DIR = get_data_dir()
DATABASE_PATH = DATABASE_DIR / "data.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# Create the async engine
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    # Pool settings tuned for SQLite
    pool_pre_ping=True,
    connect_args={"check_same_thread": False},
)

# Async session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _enable_wal_mode(dbapi_connection, connection_record):
    """Enable WAL mode on every new SQLite connection.

    WAL (Write-Ahead Logging) mode allows concurrent readers and a
    single writer, which is essential for a web server handling
    multiple simultaneous requests.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


# Register the WAL mode listener on the sync engine
# (aiosqlite wraps a sync connection under the hood)
event.listen(engine.sync_engine, "connect", _enable_wal_mode)


async def init_db() -> None:
    """Initialize the database by creating all tables.

    Ensures the data directory exists and creates all tables defined
    in the ORM models. Safe to call multiple times -- existing tables
    are not recreated.
    """
    # Ensure directory exists
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initializing database at {DATABASE_PATH}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Lightweight column migrations. create_all() above creates missing
    # *tables* but never alters existing ones, so columns added after a DB was
    # first created must be backfilled here. Keep each check idempotent.
    async with engine.begin() as conn:
        res = await conn.execute(text("PRAGMA table_info(users)"))
        user_columns = {row[1] for row in res.fetchall()}
        if "status" not in user_columns:
            # DEFAULT 'active' so every pre-existing user keeps full access;
            # only brand-new registrations are set to 'pending' by register().
            await conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN status VARCHAR(20) "
                    "NOT NULL DEFAULT 'active'"
                )
            )
            logger.info("Migration: added users.status column (default 'active')")

    # Verify WAL mode is active
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        mode = result.scalar()
        logger.info(f"SQLite journal mode: {mode}")

    # Ensure a default user exists when auth is disabled
    from ..config import get_settings
    settings = get_settings()
    if settings.DISABLE_AUTH:
        from .models import User
        async with async_session_factory() as session:
            from sqlalchemy import select
            existing = await session.execute(
                select(User).where(User.id == "default")
            )
            if not existing.scalar_one_or_none():
                session.add(User(
                    id="default",
                    email="default@localhost",
                    name="Default User",
                    password_hash="disabled",
                    role="admin",
                    is_active=True,
                    status="active",
                ))
                await session.commit()
                logger.info("Created default user for auth-disabled mode")

    logger.info("Database initialization complete")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session.

    Usage in route handlers::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Item))
            return result.scalars().all()

    The session is automatically closed when the request finishes.
    Commits must be done explicitly within the route handler.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
