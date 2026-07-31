"""Sessao assincrona do SQLAlchemy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def criar_engine(url: str | None = None) -> AsyncEngine:
    destino = url or get_settings().database_url
    if not destino:
        raise ValueError("DATABASE_URL nao configurada")
    return create_async_engine(destino, pool_pre_ping=True, future=True)


def criar_fabrica(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def sessao(
    fabrica: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with fabrica() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
