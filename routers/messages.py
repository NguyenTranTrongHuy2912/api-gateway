import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import grpc

import chat_pb2
from auth import get_current_user

log = logging.getLogger("gateway.messages")
router = APIRouter(prefix="/api/v1", tags=["Messages"])

GRPC_TIMEOUT = 5.0

class MessageReactionBody(BaseModel):
    emoji: str = Field(min_length=1, max_length=10)


class EditMessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class VotePollBody(BaseModel):
    option_id: str = Field(min_length=1, max_length=50)


def reactions_to_dict(entries) -> dict:
    return {e.emoji: list(e.user_ids) for e in entries}


@router.post("/rooms/{room_id}/messages/{message_id}/revoke")
async def revoke_message(
    room_id: str,
    message_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.RevokeMessage(
            chat_pb2.RevokeMessageRequest(user_id=user_id, room_id=room_id, message_id=message_id),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details() or "Lỗi thu hồi tin nhắn")

    if resp.already_revoked:
        return {"ok": True, "message_id": message_id, "message": "Tin nhắn đã được thu hồi trước đó."}
    return {"ok": True, "message_id": message_id, "content": resp.content}


@router.post("/rooms/{room_id}/messages/{message_id}/react")
async def react_message(
    room_id: str,
    message_id: str,
    body: MessageReactionBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.ReactMessage(
            chat_pb2.ReactMessageRequest(
                user_id=user_id, room_id=room_id, message_id=message_id, emoji=body.emoji.strip()
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details() or "Lỗi thả cảm xúc")

    return {"ok": True, "message_id": message_id, "reactions": reactions_to_dict(resp.reactions)}


@router.put("/rooms/{room_id}/messages/{message_id}")
async def edit_message(
    room_id: str,
    message_id: str,
    body: EditMessageBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.EditMessage(
            chat_pb2.EditMessageRequest(
                user_id=user_id, room_id=room_id, message_id=message_id, content=body.content
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details() or "Lỗi sửa tin nhắn")

    return {
        "ok": True,
        "message_id": message_id,
        "content": resp.content,
        "is_edited": True,
        "edited_at": resp.edited_at,
    }


@router.post("/chat/polls/{poll_id}/vote")
async def vote_poll(
    poll_id: str,
    body: VotePollBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.VotePoll(
            chat_pb2.VotePollRequest(
                user_id=user_id, poll_id=poll_id, option_id=body.option_id.strip()
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details() or "Lỗi bình chọn")

    from main import format_poll_response, poll_proto_to_dict
    return {
        "ok": True,
        "poll_id": poll_id,
        "poll": format_poll_response(poll_proto_to_dict(resp.poll)),
    }
