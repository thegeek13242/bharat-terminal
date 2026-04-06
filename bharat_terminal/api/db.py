"""
Async SQLAlchemy engine + session factory for the API gateway.
Uses the same DATABASE_URL as the KB service.
"""
import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal",
)

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def close_engine():
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None
        logger.info("DB engine disposed")
