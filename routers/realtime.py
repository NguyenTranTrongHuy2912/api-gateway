import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from auth import get_current_user

log = logging.getLogger("gateway.realtime")
router = APIRouter(prefix="/api/v1", tags=["Realtime"])

class RegisterPushTokenBody(BaseModel):
    push_token: str = Field(min_length=10, max_length=500)


@router.post("/users/push-token")
async def register_push_token_endpoint(
    payload: RegisterPushTokenBody,
    user_id: str = Depends(get_current_user),
    request: Request = None,
):
    mongo_db = getattr(request.app.state, "mongo_db", None) if request else None
    if mongo_db is not None:
        try:
            mongo_db["users"].update_one(
                {"_id": user_id},
                {"$set": {"push_token": payload.push_token.strip()}},
                upsert=False,
            )
        except Exception as e:
            log.warning("Could not persist push_token: %s", e)
    return {"ok": True, "user_id": user_id}
