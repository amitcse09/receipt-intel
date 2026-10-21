"""Event payloads. Versioned and validated like any API contract — other teams' consumers depend on them.

Thin events: they carry ids + what happened, not the whole receipt. Consumers read current state from the
DB, so a replayed or late event can never overwrite newer data with stale data.
"""

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # for dedup / tracing
    version: int = 1  # bump on breaking changes; consumers can support several during a migration
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReceiptSubmitted(Event):
    type: str = "receipt.submitted"
    receipt_id: int
    member_id: str


class ReceiptValidated(Event):
    type: str = "receipt.validated"
    receipt_id: int
    member_id: str
    status: str
    review_notes: list[str]
