import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select

from app import extraction
from app.db import Receipt, SessionLocal
from app.deps import LLMDep, MemberDep, SessionDep, SettingsDep, require_api_key
from app.events.outbox import add_event
from app.events.schemas import ReceiptSubmitted
from app.events.topics import Topics
from app.llm import LLMClient, LLMError
from app.schemas import ExtractIn, ReceiptIn, ReceiptOut

log = logging.getLogger(__name__)

# Router-level dependency: every route here needs the API key (≈ DRF ViewSet permission_classes).
router = APIRouter(prefix="/receipts", tags=["receipts"], dependencies=[Depends(require_api_key)])


async def validate_receipt(receipt_id: int, llm: LLMClient) -> None:
    """Runs AFTER the response is sent, in the same process (moves to a Kafka consumer in Module 3).

    Takes only the id and opens its own session: it must not depend on the request's lifecycle, and in
    Module 3 it runs in a separate worker with no request at all (same reason Celery tasks take ids, not objects).
    """
    async with SessionLocal() as session:
        receipt = await session.get(Receipt, receipt_id)
        if receipt is None:
            return
        status, notes = await extraction.validate(
            receipt.raw_text, receipt.store, receipt.total, receipt.purchased_on, llm
        )
        receipt.status, receipt.review_notes = status, notes
        await session.commit()
    log.info("receipt %s -> %s %s", receipt_id, status, notes)


@router.post("", response_model=ReceiptOut, status_code=201)
async def create_receipt(payload: ReceiptIn, session: SessionDep, member: MemberDep, llm: LLMDep,
                         settings: SettingsDep, background: BackgroundTasks):
    receipt = Receipt(**payload.model_dump(), member_id=member)
    session.add(receipt)
    if settings.validation_mode == "kafka":
        await session.flush()  # assigns receipt.id inside the still-open transaction
        # Same transaction as the receipt: both rows commit together or not at all (transactional outbox).
        add_event(session, Topics(settings.kafka_topic_prefix).submitted, key=receipt.id,
                  event=ReceiptSubmitted(receipt_id=receipt.id, member_id=member))
    await session.commit()
    await session.refresh(receipt)
    if settings.validation_mode != "kafka":
        background.add_task(validate_receipt, receipt.id, llm)  # in-process: lost if the pod dies (gap #2)
    return receipt


@router.post("/extract", response_model=extraction.ExtractedReceipt)
async def extract_receipt(payload: ExtractIn, llm: LLMDep):
    """Playground: see exactly what the LLM pulls out of a receipt (no DB write)."""
    try:
        return await extraction.extract(payload.raw_text, llm)
    except LLMError as e:
        raise HTTPException(502, str(e))


@router.get("/{receipt_id}", response_model=ReceiptOut)
async def get_receipt(receipt_id: int, session: SessionDep, member: MemberDep):
    receipt = await session.get(Receipt, receipt_id)
    # Someone else's receipt → the SAME 404 as a missing one (a 403 would confirm it exists: an IDOR leak).
    if receipt is None or receipt.member_id != member:
        raise HTTPException(404, "Receipt not found")
    return receipt


@router.get("", response_model=list[ReceiptOut])
async def list_receipts(session: SessionDep, member: MemberDep, limit: int = 20, offset: int = 0):
    query = select(Receipt).where(Receipt.member_id == member).order_by(Receipt.id)
    result = await session.scalars(query.limit(min(limit, 100)).offset(offset))
    return result.all()
