import logging
from typing import Literal, Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from auth import get_current_user

log = logging.getLogger("gateway.ai")
router = APIRouter(prefix="/api/v1/ai", tags=["AI"])

class AiChatHistoryItem(BaseModel):
    role: str = "user"
    content: str = Field(min_length=1, max_length=4000)


class AiChatBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    history: Optional[list[AiChatHistoryItem]] = None


class AiSummarizeBody(BaseModel):
    room_id: str
    limit: int = 30


class AiRewriteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    tone: Literal["formal", "friendly", "short", "english"] = "formal"


class AiTranslateBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    target_lang: str = Field(default="vi", max_length=10)


@router.post("/rewrite")
async def ai_rewrite(
    payload: AiRewriteBody,
    user_id: str = Depends(get_current_user),
):
    from main import rewrite_with_ai
    result = rewrite_with_ai(payload.text, payload.tone)
    return {"ok": True, "result": result, "tone": payload.tone}


@router.post("/translate")
async def ai_translate(
    payload: AiTranslateBody,
    user_id: str = Depends(get_current_user),
):
    from main import translate_with_ai
    translated = translate_with_ai(payload.text, payload.target_lang)
    return {
        "ok": True,
        "original": payload.text,
        "translated": translated,
        "target_lang": payload.target_lang,
    }


@router.post("/chat")
async def ai_chat_endpoint(
    payload: AiChatBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    from main import ai_chat
    return await ai_chat(payload=payload, request=request, user_id=user_id)


@router.post("/summarize")
async def ai_summarize_endpoint(
    payload: AiSummarizeBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    from main import ai_summarize_room
    return await ai_summarize_room(payload=payload, request=request, user_id=user_id)
