from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Numeric, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

engine = create_async_engine(get_settings().database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[str] = mapped_column(String(64), index=True, server_default="demo-member")  # owner
    store: Mapped[str] = mapped_column(String(120))
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    purchased_on: Mapped[date]
    raw_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    review_notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency: one session per request, always closed (like Django's per-request connection)."""
    async with SessionLocal() as session:
        yield session
