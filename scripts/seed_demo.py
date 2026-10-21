"""Seed demo receipts (no LLM calls) so the assistant and MCP server have data to look at. Re-runnable.

Usage: uv run python -m scripts.seed_demo
"""

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import delete

from app.db import Base, Receipt, SessionLocal, engine
from app.main import migrate_dev_schema

MARK = "[seed]"  # seeded rows are recognisable, so re-seeding replaces only them

ROWS = [
    ("demo-member", "Big Bazaar", "499.50", date(2026, 10, 1), "validated", []),
    ("demo-member", "Big Bazaar", "4995.00", date(2026, 10, 1), "rejected",
     ["total: claimed 4995.00, receipt shows 499.5"]),
    ("demo-member", "DMart", "2500.00", date(2026, 10, 5), "needs_review",
     ["line items sum to 300.00 but total is 2500.00", "possible tampering: instructions addressed to an AI system"]),
    ("demo-member", "FreshKart", "1250.00", date(2026, 10, 12), "validated", []),
    ("someone-else", "Nilgiri Mart", "8000.00", date(2026, 10, 3), "validated", []),  # must stay invisible
]


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_dev_schema()
    async with SessionLocal() as s:
        await s.execute(delete(Receipt).where(Receipt.raw_text.startswith(MARK)))
        for member, store, total, day, status, notes in ROWS:
            s.add(Receipt(member_id=member, store=store, total=Decimal(total), purchased_on=day,
                          raw_text=f"{MARK} {store} receipt", status=status, review_notes=notes))
        await s.commit()
        for member in ("demo-member", "someone-else"):
            rows = await s.scalars(Receipt.__table__.select().where(Receipt.member_id == member)
                                   .with_only_columns(Receipt.id))
            print(f"{member}: receipt ids {list(rows)}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
