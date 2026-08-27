from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session


async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_session():
        yield session
