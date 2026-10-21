"""Cashback maths in plain code — the LLM explains, this computes.

These numbers mirror data/policies/cashback-rates.md. Two copies of a rule is a real trade-off: in production
both the policy text and these values would be generated from ONE source (a config table), so they can't drift.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Tier = Literal["silver", "gold", "platinum"]

TIER_RATE_PCT = {"silver": Decimal(1), "gold": Decimal(2), "platinum": Decimal(3)}
PARTNER_BONUS_PCT = Decimal(2)
PARTNER_STORES = {"nilgiri mart", "freshkart", "sahyadri electronics"}
CAP_PER_RECEIPT = Decimal("500.00")
MIN_RECEIPT = Decimal("200.00")


def calculate(amount: Decimal, tier: Tier, store: str) -> dict:
    if amount < MIN_RECEIPT:
        return {"eligible": False, "cashback": "0.00", "reason": f"receipts under ₹{MIN_RECEIPT} are not eligible"}
    partner = store.strip().casefold() in PARTNER_STORES
    rate = TIER_RATE_PCT[tier] + (PARTNER_BONUS_PCT if partner else 0)
    raw = (amount * rate / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    cashback = min(raw, CAP_PER_RECEIPT)
    steps = [f"{tier} tier rate {TIER_RATE_PCT[tier]}%"]
    if partner:
        steps.append(f"+ {PARTNER_BONUS_PCT}% partner bonus ({store})")
    steps.append(f"= {rate}% of ₹{amount} = ₹{raw}")
    if cashback < raw:
        steps.append(f"capped at ₹{CAP_PER_RECEIPT} per receipt")
    return {"eligible": True, "rate_pct": str(rate), "cashback": str(cashback), "capped": cashback < raw,
            "partner_store": partner, "breakdown": steps}
