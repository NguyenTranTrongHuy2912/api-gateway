import logging
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import grpc

import chat_pb2
from auth import get_current_user

log = logging.getLogger("gateway.rooms")
router = APIRouter(prefix="/api/v1/rooms", tags=["Rooms"])

GRPC_TIMEOUT = 5.0

ROOM_TYPE_TO_API = {
    chat_pb2.ROOM_TYPE_DIRECT: "DIRECT",
    chat_pb2.ROOM_TYPE_GROUP: "GROUP",
}
ROOM_TYPE_FROM_API = {
    "DIRECT": chat_pb2.ROOM_TYPE_DIRECT,
    "GROUP": chat_pb2.ROOM_TYPE_GROUP,
}

class CreateRoomBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    type: str = Field(pattern="^(DIRECT|GROUP)$")
    name: str = Field(default="", max_length=50)
    member_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=99)


class AddRoomMembersBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    member_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=99)


class UpdateRoomNameBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=50)


class MarkReadBody(BaseModel):
    read_up_to_timestamp: int = Field(default=0, ge=0)


def to_http_error(e: grpc.RpcError) -> HTTPException:
    code = e.code()
    mapping = {
        grpc.StatusCode.INVALID_ARGUMENT: 400,
        grpc.StatusCode.UNAUTHENTICATED: 401,
        grpc.StatusCode.PERMISSION_DENIED: 403,
        grpc.StatusCode.NOT_FOUND: 404,
        grpc.StatusCode.ALREADY_EXISTS: 409,
        grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
        grpc.StatusCode.UNAVAILABLE: 503,
        grpc.StatusCode.DEADLINE_EXCEEDED: 504,
    }
    status = mapping.get(code, 500)
    return HTTPException(status_code=status, detail=e.details() or "Lỗi xử lý phòng chat")


@router.get("")
async def list_rooms(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.ListRooms(
            chat_pb2.ListRoomsRequest(user_id=user_id), timeout=GRPC_TIMEOUT
        )
    except grpc.RpcError as e:
        raise to_http_error(e)

    # Delegate formatting to helper from main
    from main import room_to_dict
    return {"rooms": [room_to_dict(r) for r in resp.rooms]}


@router.post("")
async def create_room(
    payload: CreateRoomBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    from main import find_hidden_ids, room_to_dict
    if find_hidden_ids(payload.member_ids, hide_admins=False):
        raise HTTPException(status_code=400, detail="Có người dùng không khả dụng.")

    try:
        resp = await request.app.state.stub.CreateRoom(
            chat_pb2.CreateRoomRequest(
                creator_id=user_id,
                type=ROOM_TYPE_FROM_API[payload.type],
                name=payload.name,
                member_ids=payload.member_ids,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e)

    return room_to_dict(resp)


@router.post("/{room_id}/leave")
async def leave_room(
    room_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.LeaveRoom(
            chat_pb2.LeaveRoomRequest(user_id=user_id, room_id=room_id),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e)
    return {"ok": resp.is_success, "room_id": room_id, "message": resp.status_message}


@router.post("/{room_id}/members")
async def add_room_members(
    room_id: str,
    payload: AddRoomMembersBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    from main import find_hidden_ids, room_to_dict
    if find_hidden_ids(payload.member_ids, hide_admins=False):
        raise HTTPException(status_code=400, detail="Có người dùng không khả dụng.")
    try:
        resp = await request.app.state.stub.AddRoomMembers(
            chat_pb2.AddRoomMembersRequest(
                user_id=user_id,
                room_id=room_id,
                member_ids=payload.member_ids,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e)
    return {
        "ok": True,
        "room_id": room_id,
        "added_count": resp.added_count,
        "room": room_to_dict(resp.room),
    }


@router.patch("/{room_id}/name")
async def update_room_name(
    room_id: str,
    payload: UpdateRoomNameBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    from main import room_to_dict
    try:
        resp = await request.app.state.stub.UpdateRoomName(
            chat_pb2.UpdateRoomNameRequest(user_id=user_id, room_id=room_id, name=payload.name),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e)
    return {
        "ok": True,
        "room_id": room_id,
        "new_name": resp.room.name,
        "room": room_to_dict(resp.room),
    }


@router.post("/{room_id}/read")
async def mark_room_read(
    room_id: str,
    request: Request,
    payload: Optional[MarkReadBody] = None,
    user_id: str = Depends(get_current_user),
):
    try:
        await request.app.state.stub.MarkRoomRead(
            chat_pb2.MarkRoomReadRequest(
                user_id=user_id,
                room_id=room_id,
                read_up_to_timestamp=payload.read_up_to_timestamp if payload else 0,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e)

    return {"ok": True}
