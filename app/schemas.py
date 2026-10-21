from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ReceiptBase(BaseModel):
    store: str = Field(min_length=2, max_length=120)
    total: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    purchased_on: date


class ReceiptIn(ReceiptBase):
    raw_text: str = Field(min_length=10, max_length=20_000, description="OCR text of the receipt")


class ReceiptOut(ReceiptBase):
    model_config = ConfigDict(from_attributes=True)  # ≈ DRF ModelSerializer: read from ORM objects

    id: int
    status: str
    review_notes: list[str] = []


class ExtractIn(BaseModel):
    raw_text: str = Field(min_length=10, max_length=20_000)
