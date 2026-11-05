import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

import grpc
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.responses import JSONResponse

import chat_pb2
import chat_pb2_grpc
from auth import create_token, decode_token, get_current_user, revoke_user_tokens
from ai_service import generate_ai_reply, summarize_conversation

log = logging.getLogger("gateway")

# Backend address and per-call timeout, configurable without touching code
JAVA_GRPC_TARGET = os.getenv("JAVA_GRPC_TARGET", "localhost:50051")
GRPC_TIMEOUT = float(os.getenv("GRPC_TIMEOUT", "15.0"))  # seconds

# Cloudinary (ảnh chat). API_SECRET chỉ nằm ở gateway, không bao giờ đưa vào app.
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "chatapp").strip("/")
# Cloudinary tự thu nhỏ ảnh khi nhận (áp dụng trước khi lưu), nên app không cần nén kích thước
IMAGE_INCOMING_TRANSFORMATION = "c_limit,w_1600,h_1600"


def is_own_image_url(url: str) -> bool:
    """Chỉ chấp nhận ảnh nằm trong đúng tài khoản + thư mục Cloudinary của mình."""
    if not CLOUDINARY_CLOUD_NAME or not url:
        return False
    pattern = (
        rf"^https://res\.cloudinary\.com/{re.escape(CLOUDINARY_CLOUD_NAME)}/(?:image|video)/upload/"
        rf"(?:v\d+/)?{re.escape(CLOUDINARY_FOLDER)}/[A-Za-z0-9_-]+\.[A-Za-z0-9]+$"
    )
    return re.match(pattern, url) is not None


# gRPC status code -> HTTP status code (anything not listed becomes 500)
HTTP_BY_GRPC = {
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.UNAUTHENTICATED: 401,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.ALREADY_EXISTS: 409,
    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
    grpc.StatusCode.UNAVAILABLE: 503,
    grpc.StatusCode.DEADLINE_EXCEEDED: 504,
}

DETAIL_BY_STATUS = {
    401: "Unauthenticated",
    403: "Forbidden",
    404: "Not found",
    409: "Conflict",
    429: "Too many requests",
    503: "Chat backend is unavailable",
    504: "Chat backend timed out",
}

# Proto enum <-> the short names used in the REST API
ROOM_TYPE_TO_API = {
    chat_pb2.ROOM_TYPE_DIRECT: "DIRECT",
    chat_pb2.ROOM_TYPE_GROUP: "GROUP",
}
ROOM_TYPE_FROM_API = {
    "DIRECT": chat_pb2.ROOM_TYPE_DIRECT,
    "GROUP": chat_pb2.ROOM_TYPE_GROUP,
}
MESSAGE_TYPE_TO_API = {
    chat_pb2.MESSAGE_TYPE_TEXT: "TEXT",
    chat_pb2.MESSAGE_TYPE_IMAGE: "IMAGE",
    chat_pb2.MESSAGE_TYPE_VIDEO: "VIDEO",
}


def to_http_error(e: grpc.RpcError, expose_details: bool = False) -> HTTPException:
    code = e.code()
    status = HTTP_BY_GRPC.get(code, 500)
    log.warning("gRPC call failed: %s - %s", code, e.details())

    if status == 400:
        detail = e.details() or "Invalid request"
    elif expose_details and status in (401, 404, 409, 429):
        # Auth endpoints only: the backend wrote these messages for end users
        detail = e.details() or DETAIL_BY_STATUS.get(status, "Error")
    else:
        detail = DETAIL_BY_STATUS.get(status, "Internal gateway error")
    return HTTPException(status_code=status, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared channel for the whole app: no new connection handshake per request
    channel = grpc.aio.insecure_channel(JAVA_GRPC_TARGET)
    app.state.stub = chat_pb2_grpc.ChatServiceStub(channel)
    app.state.auth_stub = chat_pb2_grpc.AuthServiceStub(channel)
    try:
        yield
    finally:
        await channel.close()


app = FastAPI(
    title="FastAPI gRPC Gateway",
    description="API Gateway bridging the mobile client and the Java gRPC backend",
    lifespan=lifespan,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Bạn gửi quá nhiều yêu cầu. Vui lòng thử lại sau 1 phút."},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    room_id: str = Field(min_length=1, max_length=100)
    type: Literal["TEXT", "IMAGE", "VIDEO"] = "TEXT"
    # TEXT: bắt buộc. IMAGE/VIDEO: để trống.
    content: str = Field(default="", max_length=4000)
    # Optional: client-generated id. If omitted, the gateway generates a uuid.
    message_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    # Dùng cho IMAGE hoặc VIDEO
    media_url: Optional[str] = Field(default=None, max_length=500)
    media_width: int = Field(default=0, ge=0, le=20000)
    media_height: int = Field(default=0, ge=0, le=20000)
    media_duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_by_type(self):
        if self.type == "TEXT":
            if not self.content:
                raise ValueError("content must not be empty")
        elif not is_own_image_url(self.media_url or ""):
            raise ValueError("media_url is not a valid uploaded image")
        return self


class RequestOtpBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    account: str = Field(min_length=1, max_length=254)  # email


class ResetPasswordBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    account: str = Field(min_length=1, max_length=254)  # email
    otp_code: str = Field(min_length=1, max_length=10)
    new_password: str = Field(min_length=6, max_length=64)


class RegisterBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    account: str = Field(min_length=1, max_length=254)  # email
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=50)
    otp_code: str = Field(min_length=1, max_length=10)


class LoginBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    account: str = Field(min_length=1, max_length=254)  # email
    password: str = Field(min_length=1, max_length=128)


class CreateRoomBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    type: Literal["DIRECT", "GROUP"]
    # Only used for GROUP; the backend validates length and required-ness
    name: str = Field(default="", max_length=50)
    # Other people in the room; the creator (from the token) is added by the backend
    member_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=99
    )


class MarkReadBody(BaseModel):
    # 0 = mark everything currently in the room as read
    read_up_to_timestamp: int = Field(default=0, ge=0)


class FriendRequestBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    receiver_id: str = Field(min_length=1, max_length=100)


class RespondFriendRequestBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    request_id: str = Field(min_length=1, max_length=100)
    accept: bool


class UpdateProfileBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    display_name: Optional[str] = Field(default=None, max_length=50)
    avatar_url: Optional[str] = Field(default=None, max_length=500)


class ChangePasswordLoggedInBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=64)


class RegisterPushTokenBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    push_token: str = Field(min_length=1, max_length=200)


def room_to_dict(room: chat_pb2.Room) -> dict:
    return {
        "room_id": room.room_id,
        "type": ROOM_TYPE_TO_API.get(room.type, "UNKNOWN"),
        "name": room.name,
        "member_ids": list(room.member_ids),
        "members": [
            {"user_id": u.user_id, "display_name": u.display_name} for u in room.members
        ],
        # None when the room has no messages yet
        "last_message": (
            {
                "content": room.last_message_content,
                "sender_id": room.last_message_sender_id,
                "timestamp": room.last_message_timestamp,
            }
            if room.last_message_timestamp > 0
            else None
        ),
        "unread_count": room.unread_count,
    }


@app.get("/")
async def root():
    return {
        "system": "FastAPI gRPC Gateway",
        "status": "Running",
        "docs_url": "/docs",
    }


# ------------------------------------------------------------------- Auth


@app.post("/api/v1/auth/request-otp")
@limiter.limit("5/minute")
async def request_otp(request: Request, payload: RequestOtpBody):
    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(account=payload.account), timeout=GRPC_TIMEOUT
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "expires_in_seconds": resp.expires_in_seconds,
        "resend_after_seconds": resp.resend_after_seconds,
    }


@app.post("/api/v1/auth/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(request: Request, payload: RequestOtpBody):
    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(
                account=payload.account,
                purpose=chat_pb2.OTP_PURPOSE_RESET_PASSWORD,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "expires_in_seconds": resp.expires_in_seconds,
        "resend_after_seconds": resp.resend_after_seconds,
    }


@app.post("/api/v1/auth/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, payload: ResetPasswordBody):
    try:
        await request.app.state.auth_stub.ResetPassword(
            chat_pb2.ResetPasswordRequest(
                account=payload.account,
                otp_code=payload.otp_code,
                new_password=payload.new_password,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {"ok": True}


@app.post("/api/v1/auth/register")
async def register(payload: RegisterBody, request: Request):
    try:
        resp = await request.app.state.auth_stub.Register(
            chat_pb2.RegisterRequest(
                account=payload.account,
                password=payload.password,
                display_name=payload.display_name,
                otp_code=payload.otp_code,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "token": create_token(resp.user_id),
        "user_id": resp.user_id,
        "display_name": resp.display_name,
    }


@app.post("/api/v1/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, payload: LoginBody):
    try:
        resp = await request.app.state.auth_stub.Login(
            chat_pb2.LoginRequest(account=payload.account, password=payload.password),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "token": create_token(resp.user_id),
        "user_id": resp.user_id,
        "display_name": resp.display_name,
    }


# ------------------------------------------------------------------ Users


@app.get("/api/v1/users/search")
async def search_users(
    request: Request,
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(10, ge=1, le=20),
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.SearchUsers(
            chat_pb2.SearchUsersRequest(requester_id=user_id, query=q, limit=limit),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    return {
        "users": [
            {"user_id": u.user_id, "display_name": u.display_name, "email": u.email}
            for u in resp.users
        ]
    }


# ------------------------------------------------------------------ Rooms


@app.get("/api/v1/rooms")
async def list_rooms(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.ListRooms(
            chat_pb2.ListRoomsRequest(user_id=user_id), timeout=GRPC_TIMEOUT
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    return {"rooms": [room_to_dict(r) for r in resp.rooms]}


@app.post("/api/v1/rooms")
async def create_room(
    payload: CreateRoomBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.CreateRoom(
            chat_pb2.CreateRoomRequest(
                creator_id=user_id,  # from the token, never from the client
                type=ROOM_TYPE_FROM_API[payload.type],
                name=payload.name,
                member_ids=payload.member_ids,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    return room_to_dict(resp)


@app.post("/api/v1/rooms/{room_id}/read")
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
        raise to_http_error(e, expose_details=True)

    return {"ok": True}


# ---------------------------------------------------------------- Uploads


@app.post("/api/v1/uploads/sign")
@limiter.limit("20/minute")
async def sign_upload(
    request: Request,
    resource_type: str = "image",
    user_id: str = Depends(get_current_user),
):
    """Cấp chữ ký ngắn hạn để app upload ảnh/video thẳng lên Cloudinary."""
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
        raise HTTPException(status_code=503, detail="Upload is not configured")

    res_type = "video" if resource_type == "video" else "image"
    timestamp = int(time.time())
    public_id = f"{CLOUDINARY_FOLDER}/{uuid.uuid4().hex}"
    
    if res_type == "video":
        to_sign = f"public_id={public_id}&timestamp={timestamp}"
        transformation = ""
    else:
        to_sign = (
            f"public_id={public_id}&timestamp={timestamp}"
            f"&transformation={IMAGE_INCOMING_TRANSFORMATION}"
        )
        transformation = IMAGE_INCOMING_TRANSFORMATION

    signature = hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()
    return {
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/{res_type}/upload",
        "api_key": CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "public_id": public_id,
        "transformation": transformation,
        "signature": signature,
        "resource_type": res_type,
    }


# ------------------------------------------------------------------- Chat


@app.post("/api/v1/chat/send")
async def send_message(
    payload: SendMessageRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    grpc_request = chat_pb2.ChatMessage(
        message_id=payload.message_id or str(uuid.uuid4()),
        room_id=payload.room_id,
        sender_id=user_id,  # from the token, never from the client
        content=payload.content,
        timestamp=int(time.time() * 1000),
        type=(
            chat_pb2.MESSAGE_TYPE_IMAGE if payload.type == "IMAGE"
            else chat_pb2.MESSAGE_TYPE_VIDEO if payload.type == "VIDEO"
            else chat_pb2.MESSAGE_TYPE_TEXT
        ),
        media_url=payload.media_url or "",
        media_width=payload.media_width,
        media_height=payload.media_height,
        media_duration_ms=payload.media_duration_ms,
    )
    try:
        resp = await request.app.state.stub.SendMessage(grpc_request, timeout=GRPC_TIMEOUT)
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    asyncio.create_task(notify_new_message(
        room_id=payload.room_id,
        sender_id=user_id,
        content=payload.content,
        msg_type=payload.type,
        stub=request.app.state.stub,
    ))

    return {
        "message_id": resp.message_id,
        "is_success": resp.is_success,
        "status_message": resp.status_message,
        "timestamp": resp.timestamp,
    }


@app.get("/api/v1/chat/history/{room_id}")
async def get_history(
    room_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    before_timestamp: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user),
):
    grpc_request = chat_pb2.HistoryRequest(
        room_id=room_id,
        limit=limit,
        before_timestamp=before_timestamp,
        user_id=user_id,  # backend checks this user belongs to the room
    )
    try:
        resp = await request.app.state.stub.GetMessageHistory(grpc_request, timeout=GRPC_TIMEOUT)
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    return {
        "messages": [
            {
                "message_id": m.message_id,
                "room_id": m.room_id,
                "sender_id": m.sender_id,
                "content": m.content,
                "timestamp": m.timestamp,
                "type": MESSAGE_TYPE_TO_API.get(m.type, "TEXT"),
                "media_url": m.media_url,
                "media_width": m.media_width,
                "media_height": m.media_height,
                "media_duration_ms": m.media_duration_ms,
            }
            for m in resp.messages
        ],
        "has_more": resp.has_more,
    }


# ---------------------------------------------------------------- Presence & Push State
active_user_connections: dict[str, set[WebSocket]] = {}
user_last_seen: dict[str, int] = {}
user_push_tokens: dict[str, str] = {}


async def broadcast_ws_event(payload: dict):
    """Gửi sự kiện realtime tới tất cả các WebSocket client đang kết nối"""
    all_sockets = []
    for sockets in list(active_user_connections.values()):
        all_sockets.extend(list(sockets))
    for ws in all_sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            pass


async def send_expo_push(target_user_ids: list[str], title: str, body: str, data: dict = None):
    """Gửi push notification qua Expo Push API"""
    tokens = [
        user_push_tokens[uid]
        for uid in target_user_ids
        if uid in user_push_tokens and user_push_tokens[uid].startswith("ExponentPushToken")
    ]
    if not tokens:
        return

    messages = [
        {
            "to": token,
            "sound": "default",
            "title": title,
            "body": body,
            "data": data or {},
            "priority": "high",
            "channelId": "chat-messages",
        }
        for token in tokens
    ]

    try:
        import urllib.request
        req = urllib.request.Request(
            "https://exp.host/--/api/v2/push/send",
            data=json.dumps(messages).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            method="POST",
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5).read())
    except Exception as e:
        log.warning("Failed to send Expo push notification: %s", e)


async def notify_new_message(room_id: str, sender_id: str, content: str, msg_type: str, stub):
    try:
        rooms_resp = await stub.ListRooms(chat_pb2.ListRoomsRequest(user_id=sender_id), timeout=GRPC_TIMEOUT)
        room = next((r for r in rooms_resp.rooms if r.room_id == room_id), None)
        if not room:
            return
        sender_member = next((m for m in room.members if m.user_id == sender_id), None)
        sender_name = sender_member.display_name if sender_member and sender_member.display_name else "Tin nhắn mới"

        title = room.name if (room.type == chat_pb2.ROOM_TYPE_GROUP and room.name) else sender_name
        body_text = content
        if msg_type == "IMAGE":
            body_text = "Đã gửi một hình ảnh 📷"
        elif msg_type == "VIDEO":
            body_text = "Đã gửi một video 🎬"

        target_ids = [uid for uid in room.member_ids if uid != sender_id]
        await send_expo_push(target_ids, title=title, body=body_text, data={"roomId": room_id, "type": "chat"})
    except Exception as e:
        log.warning("Error pushing new message notification: %s", e)


@app.get("/api/v1/users/online")
async def get_online_users(user_id: str = Depends(get_current_user)):
    return {"online_user_ids": list(active_user_connections.keys())}


@app.post("/api/v1/users/push-token")
async def register_push_token(
    payload: RegisterPushTokenBody,
    user_id: str = Depends(get_current_user),
):
    user_push_tokens[user_id] = payload.push_token
    log.info("Registered push token for user %s: %s", user_id, payload.push_token)
    return {"ok": True}


# ---------------------------------------------------------------- Realtime WebSocket


@app.websocket("/api/v1/ws")
async def chat_websocket(websocket: WebSocket, token: str = Query(default="")):
    user_id = decode_token(token)
    if not user_id:
        log.warning("WebSocket rejected: missing or invalid token")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    log.info("WebSocket client connected: user_id=%s", user_id)

    # Đăng ký active connection
    is_first = (user_id not in active_user_connections) or (len(active_user_connections[user_id]) == 0)
    if user_id not in active_user_connections:
        active_user_connections[user_id] = set()
    active_user_connections[user_id].add(websocket)

    # Gửi danh sách online ban đầu cho client vừa kết nối
    try:
        await websocket.send_json({
            "type": "INITIAL_PRESENCE",
            "online_user_ids": list(active_user_connections.keys()),
        })
    except Exception:
        pass

    if is_first:
        asyncio.create_task(broadcast_ws_event({
            "type": "USER_PRESENCE",
            "user_id": user_id,
            "is_online": True,
            "timestamp": int(time.time() * 1000),
        }))

    grpc_req = chat_pb2.StreamEventsRequest(user_id=user_id)
    stub = websocket.app.state.stub
    stream = stub.SubscribeEvents(grpc_req)

    async def forward_grpc_to_ws():
        try:
            async for event in stream:
                event_type_name = "UNKNOWN"
                if event.event_type == chat_pb2.EVENT_NEW_MESSAGE:
                    event_type_name = "NEW_MESSAGE"
                elif event.event_type == chat_pb2.EVENT_ROOM_UPDATED:
                    event_type_name = "ROOM_UPDATED"
                elif event.event_type == chat_pb2.EVENT_READ_RECEIPT:
                    event_type_name = "READ_RECEIPT"
                elif event.event_type == chat_pb2.EVENT_FRIEND_REQUEST:
                    event_type_name = "FRIEND_REQUEST"
                elif event.event_type == chat_pb2.EVENT_FRIEND_ACCEPTED:
                    event_type_name = "FRIEND_ACCEPTED"

                payload = {
                    "type": event_type_name,
                    "timestamp": event.timestamp,
                }
                if event.event_type == chat_pb2.EVENT_NEW_MESSAGE:
                    payload["message"] = {
                        "message_id": event.message.message_id,
                        "room_id": event.message.room_id,
                        "sender_id": event.message.sender_id,
                        "content": event.message.content,
                        "timestamp": event.message.timestamp,
                        "type": MESSAGE_TYPE_TO_API.get(event.message.type, "TEXT"),
                        "media_url": event.message.media_url,
                        "media_width": event.message.media_width,
                        "media_height": event.message.media_height,
                    }
                elif event.event_type == chat_pb2.EVENT_FRIEND_REQUEST:
                    payload["friend_request"] = {
                        "request_id": event.friend_request.request_id,
                        "sender_id": event.friend_request.sender_id,
                        "sender_name": event.friend_request.sender_name,
                        "sender_email": event.friend_request.sender_email,
                        "created_at": event.friend_request.created_at,
                    }
                elif event.event_type == chat_pb2.EVENT_FRIEND_ACCEPTED:
                    payload["user_id"] = event.user_id
                await websocket.send_json(payload)
        except asyncio.CancelledError:
            pass
        except grpc.RpcError as e:
            log.warning("gRPC stream error for user %s: %s", user_id, e)
        except Exception as e:
            log.debug("Stream exception for user %s: %s", user_id, e)

    forward_task = asyncio.create_task(forward_grpc_to_ws())

    try:
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        log.info("WebSocket disconnected: user_id=%s", user_id)
    except Exception as e:
        log.debug("WebSocket exception for user %s: %s", user_id, e)
    finally:
        forward_task.cancel()
        stream.cancel()
        if user_id in active_user_connections:
            active_user_connections[user_id].discard(websocket)
            if len(active_user_connections[user_id]) == 0:
                del active_user_connections[user_id]
                now_ms = int(time.time() * 1000)
                user_last_seen[user_id] = now_ms
                asyncio.create_task(broadcast_ws_event({
                    "type": "USER_PRESENCE",
                    "user_id": user_id,
                    "is_online": False,
                    "last_seen": now_ms,
                    "timestamp": now_ms,
                }))


# ---------------------------------------------------------------- Hệ thống kết bạn


@app.post("/api/v1/friends/request")
async def send_friend_request(
    payload: FriendRequestBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.SendFriendRequest(
            chat_pb2.SendFriendRequestReq(sender_id=user_id, receiver_id=payload.receiver_id),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    asyncio.create_task(send_expo_push(
        target_user_ids=[payload.receiver_id],
        title="Lời mời kết bạn mới",
        body=f"{resp.request.sender_name} đã gửi lời mời kết bạn cho bạn.",
        data={"type": "friend_request"},
    ))

    return {
        "ok": resp.is_success,
        "message": resp.status_message,
        "request": {
            "request_id": resp.request.request_id,
            "sender_id": resp.request.sender_id,
            "sender_name": resp.request.sender_name,
            "sender_email": resp.request.sender_email,
            "receiver_id": resp.request.receiver_id,
            "created_at": resp.request.created_at,
        },
    }


@app.post("/api/v1/friends/respond")
async def respond_friend_request(
    payload: RespondFriendRequestBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.RespondFriendRequest(
            chat_pb2.RespondFriendRequestReq(
                user_id=user_id, request_id=payload.request_id, accept=payload.accept
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {"ok": resp.is_success, "message": resp.status_message}


@app.get("/api/v1/friends")
async def list_friends(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.ListFriends(
            chat_pb2.ListFriendsReq(user_id=user_id),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "friends": [
            {
                "user_id": f.user_id,
                "display_name": f.display_name,
                "email": f.email,
                "avatar_url": f.avatar_url,
            }
            for f in resp.friends
        ]
    }


@app.get("/api/v1/friends/requests")
async def list_friend_requests(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.ListFriendRequests(
            chat_pb2.ListFriendRequestsReq(user_id=user_id),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "requests": [
            {
                "request_id": r.request_id,
                "sender_id": r.sender_id,
                "sender_name": r.sender_name,
                "sender_email": r.sender_email,
                "created_at": r.created_at,
            }
            for r in resp.requests
        ]
    }


# ---------------------------------------------------------------- Tab Cá nhân


@app.put("/api/v1/users/profile")
async def update_profile(
    payload: UpdateProfileBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.auth_stub.UpdateProfile(
            chat_pb2.UpdateProfileRequest(
                user_id=user_id,
                display_name=payload.display_name or "",
                avatar_url=payload.avatar_url or "",
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "ok": resp.is_success,
        "display_name": resp.display_name,
        "avatar_url": resp.avatar_url,
    }


@app.post("/api/v1/users/change-password")
async def change_password_logged_in(
    payload: ChangePasswordLoggedInBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.auth_stub.ChangePasswordLoggedIn(
            chat_pb2.ChangePasswordLoggedInRequest(
                user_id=user_id,
                current_password=payload.current_password,
                new_password=payload.new_password,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    revoke_user_tokens(user_id)
    return {"ok": resp.is_success, "message": resp.status_message}



# ====================================================================== AI Endpoints (Google Gemini)

class AiChatBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)

class AiSummarizeBody(BaseModel):
    room_id: str
    limit: int = 30

@app.post("/api/v1/ai/chat")
async def ai_chat(
    payload: AiChatBody,
    user_id: str = Depends(get_current_user),
):
    reply = generate_ai_reply(payload.prompt)
    return {
        "ok": True,
        "reply": reply,
        "model": "gemini-1.5-flash",
    }

@app.post("/api/v1/ai/summarize")
async def ai_summarize_chat(
    payload: AiSummarizeBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        hist_resp = await request.app.state.stub.GetMessageHistory(
            chat_pb2.HistoryRequest(
                room_id=payload.room_id,
                limit=payload.limit,
                before_timestamp=0,
                user_id=user_id,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    rooms_resp = await request.app.state.stub.ListRooms(
        chat_pb2.ListRoomsRequest(user_id=user_id),
        timeout=GRPC_TIMEOUT,
    )
    room_name = "Cuộc trò chuyện"
    for r in rooms_resp.rooms:
        if r.room_id == payload.room_id:
            room_name = r.name
            break

    msgs = []
    for m in reversed(hist_resp.messages):
        msgs.append({
            "sender_id": m.sender_id,
            "sender_name": "Bạn" if m.sender_id == user_id else "Thành viên",
            "content": m.content,
            "timestamp": m.timestamp,
        })

    summary_text = summarize_conversation(room_name, msgs)
    return {
        "ok": True,
        "room_name": room_name,
        "summary": summary_text,
        "total_messages": len(msgs),
    }


if __name__ == "__main__":

    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)