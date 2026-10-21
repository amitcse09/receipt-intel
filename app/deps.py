from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.llm import LLMClient, get_llm

# Annotated aliases keep endpoint signatures short and reusable.
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
LLMDep = Annotated[LLMClient, Depends(get_llm)]


async def current_member(x_member_id: Annotated[str, Header(min_length=1, max_length=64)] = "demo-member") -> str:
    """WHO is calling. Demo only: a header with a default. In production this comes from a verified JWT
    (e.g. the `sub` claim) — never from the request body, and never from anything an LLM produced."""
    return x_member_id


MemberDep = Annotated[str, Depends(current_member)]


async def require_api_key(settings: SettingsDep, x_api_key: Annotated[str | None, Header()] = None) -> None:
    """≈ DRF permission_class. Dependencies can depend on other dependencies (settings here)."""
    if x_api_key != settings.api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
