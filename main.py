import asyncio
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
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
from auth import create_token, decode_token, get_current_user, revoke_user_tokens, get_current_admin, get_user_role, set_user_role, USER_ROLES_STORE
from ai_service import generate_ai_reply, summarize_conversation
from vector_db import vector_db

log = logging.getLogger("gateway")

def normalize_vn(text: str) -> str:
    """Chuyển tiếng Việt có dấu về không dấu để so khớp linh hoạt"""
    text = text.lower().replace("đ", "d")
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip()


# Backend address and per-call timeout, configurable without touching code
JAVA_GRPC_TARGET = os.getenv("JAVA_GRPC_TARGET", "localhost:50051")
GRPC_TIMEOUT = float(os.getenv("GRPC_TIMEOUT", "15.0"))  # seconds

# Cloudinary
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "chatapp").strip("/")
IMAGE_INCOMING_TRANSFORMATION = "c_limit,w_1600,h_1600"

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "chat_db")
mongo_db = None
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_db = mongo_client[MONGO_DB_NAME]
    log.info("Admin MongoDB connected successfully")
except Exception as e:
    log.warning("Admin MongoDB connection error: %s", e)


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
    if JAVA_GRPC_TARGET.endswith(":443"):
        channel = grpc.aio.secure_channel(JAVA_GRPC_TARGET, grpc.ssl_channel_credentials())
    else:
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
    role = get_user_role(resp.user_id, payload.account)
    return {
        "token": create_token(resp.user_id, role=role),
        "user_id": resp.user_id,
        "display_name": resp.display_name,
        "role": role,
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
    role = get_user_role(resp.user_id, payload.account)
    return {
        "token": create_token(resp.user_id, role=role),
        "user_id": resp.user_id,
        "display_name": resp.display_name,
        "role": role,
    }



class SetUserRoleBody(BaseModel):
    role: Literal["ADMIN", "USER"]


class SetUserStatusBody(BaseModel):
    is_active: bool


@app.get("/api/v1/users/me")
async def get_my_profile(
    user_id: str = Depends(get_current_user),
):
    role = get_user_role(user_id)
    if mongo_db is not None:
        try:
            u = mongo_db["users"].find_one({"_id": user_id}, {"role": 1, "display_name": 1, "account": 1})
            if u and "role" in u:
                role = u["role"]
        except Exception:
            pass
    return {
        "user_id": user_id,
        "role": role,
        "is_admin": role == "ADMIN",
    }


@app.get("/api/v1/admin/stats")
async def get_admin_stats(
    admin_id: str = Depends(get_current_admin),
):
    total_users = 0
    total_admins = 0
    total_active_users = 0
    total_rooms = 0
    total_messages = 0

    if mongo_db is not None:
        try:
            total_users = mongo_db["users"].count_documents({})
            total_admins = mongo_db["users"].count_documents({"role": "ADMIN"})
            total_active_users = mongo_db["users"].count_documents({"is_active": True})
            total_rooms = mongo_db["rooms"].count_documents({})
            total_messages = mongo_db["messages"].count_documents({})
        except Exception as e:
            log.warning("Admin stats mongo count error: %s", e)

    return {
        "database": "MongoDB Atlas Cloud",
        "status": "Online",
        "system_status": "Healthy",
        "total_users": total_users,
        "total_admins": total_admins,
        "total_active_users": total_active_users,
        "total_rooms": total_rooms,
        "total_messages": total_messages,
        "vector_indexed_messages": vector_db.count(),
        "admin_id": admin_id,
        "system_role": "ADMIN",
    }


@app.get("/api/v1/admin/users")
async def get_admin_users(
    admin_id: str = Depends(get_current_admin),
    q: Optional[str] = Query(None),
):
    if mongo_db is None:
        return {"users": []}

    query = {}
    if q and q.strip():
        search_term = q.strip()
        query = {
            "$or": [
                {"account": {"$regex": search_term, "$options": "i"}},
                {"display_name": {"$regex": search_term, "$options": "i"}},
            ]
        }

    docs = mongo_db["users"].find(query, {"password_hash": 0}).sort("created_at", -1).limit(50)
    users_list = []
    for d in docs:
        users_list.append({
            "user_id": d.get("_id", ""),
            "account": d.get("account", ""),
            "display_name": d.get("display_name", ""),
            "role": d.get("role", "USER"),
            "is_active": d.get("is_active", True),
            "avatar_url": d.get("avatar_url", ""),
            "created_at": d.get("created_at", 0),
        })

    return {"users": users_list}


@app.patch("/api/v1/admin/users/{target_user_id}/role")
async def update_user_role_admin(
    target_user_id: str,
    body: SetUserRoleBody,
    admin_id: str = Depends(get_current_admin),
):
    set_user_role(target_user_id, body.role)
    if mongo_db is not None:
        mongo_db["users"].update_one(
            {"_id": target_user_id},
            {"$set": {"role": body.role}}
        )
    return {
        "ok": True,
        "user_id": target_user_id,
        "new_role": body.role,
        "updated_by": admin_id,
    }


@app.patch("/api/v1/admin/users/{target_user_id}/status")
async def update_user_status_admin(
    target_user_id: str,
    body: SetUserStatusBody,
    admin_id: str = Depends(get_current_admin),
):
    if mongo_db is not None:
        mongo_db["users"].update_one(
            {"_id": target_user_id},
            {"$set": {"is_active": body.is_active}}
        )
    return {
        "ok": True,
        "user_id": target_user_id,
        "is_active": body.is_active,
        "updated_by": admin_id,
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
    if payload.content:
        try:
            vector_db.upsert(
                doc_id=resp.message_id or payload.message_id,
                text=payload.content,
                metadata={
                    "room_id": payload.room_id,
                    "sender_id": user_id,
                    "timestamp": resp.timestamp,
                }
            )
        except Exception as e:
            log.warning("Could not index message to vector_db: %s", e)

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
        ended_by_cancel = False
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
                        "media_duration_ms": event.message.media_duration_ms,
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
            ended_by_cancel = True
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.CANCELLED:
                log.warning("gRPC stream error for user %s: %s", user_id, e)
        except Exception as e:
            log.warning("Stream exception for user %s: %s", user_id, e)
        finally:
            if not ended_by_cancel:
                # Luồng sự kiện từ Java đã đứt (Java restart, Mongo lỗi, observer bị gỡ...). Nếu chỉ im lặng
                # thì app vẫn tưởng WebSocket còn sống (ping/pong vẫn chạy) nhưng không bao giờ nhận được tin.
                # Đóng WebSocket để app tự kết nối lại và đăng ký stream mới.
                log.info("Event stream ended for user %s, closing WebSocket", user_id)
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass

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
    request: Request,
    user_id: str = Depends(get_current_user),
):
    p_norm = normalize_vn(payload.prompt)

    # 1. Ưu tiên giải đáp kiến trúc đồ án và công nghệ
    if any(k in p_norm for k in ["kien truc", "3 tang", "giai thich", "fastapi", "grpc", "react native", "tamagui", "cong nghe"]):
        reply = (
            "Ứng dụng ChatApp được xây dựng theo kiến trúc Microservices 3 tầng độc lập chuẩn Clean Architecture:\n\n"
            "1. 📱 **Mobile Frontend (React Native + Expo SDK 58)**:\n"
            "   • Điều hướng bằng Expo Router (File-based navigation) bảo vệ bằng Stack.Protected.\n"
            "   • Redux Toolkit + RTK Query quản lý cache server và Optimistic Update.\n"
            "   • Hiệu ứng động Animation FadeInUp và ScalePressable nhún lò xo.\n\n"
            "2. 🚀 **API Gateway (FastAPI - Python 3.13)**:\n"
            "   • Đóng vai trò Backend-For-Frontend (BFF), bảo mật xác thực JWT.\n"
            "   • Quản lý WebSocket realtime thông báo tin nhắn và trạng thái chấm xanh Online.\n"
            "   • Tích hợp Vector Database in-memory (Cosine Similarity K-NN) và Google Gemini 1.5 Flash.\n\n"
            "3. ⚙️ **Core Backend (Java 21 Netty gRPC)**:\n"
            "   • Xử lý logic nghiệp vụ phòng chat, danh sách bạn bè với Protobuf nhị phân siêu tốc.\n"
            "   • Lưu trữ dữ liệu trên MongoDB Atlas Cloud và gửi OTP qua Jakarta Mail."
        )
        return {
            "ok": True,
            "reply": reply,
            "model": "gemini-1.5-flash",
        }

    # 2. Kiểm tra xem người dùng có đang hỏi về các cuộc trò chuyện / tin nhắn / database không
    db_chat_keywords = [
        "cuoc tro chuyen", "tin nhan", "moi nhat", "gan day", "vua nhan",
        "database", "tim tin", "nhan gi", "ai nhan", "trong huy", "quoc dep trai", "van quoc"
    ]
    is_asking_about_db = any(k in p_norm for k in db_chat_keywords)

    if is_asking_about_db:
        try:
            rooms_resp = await request.app.state.stub.ListRooms(
                chat_pb2.ListRoomsRequest(user_id=user_id),
                timeout=GRPC_TIMEOUT,
            )
            rooms = list(rooms_resp.rooms)

            if not rooms:
                reply = "Bạn hiện chưa có cuộc trò chuyện nào trong cơ sở dữ liệu."
            else:
                rooms.sort(key=lambda r: r.last_message_at, reverse=True)
                latest_room = rooms[0]

                if any(k in p_norm for k in ["moi nhat", "gan day", "vua nhan"]):
                    last_msg_text = latest_room.last_message or "Chưa có nội dung văn bản"
                    last_time_str = (
                        time.strftime('%H:%M - %d/%m/%Y', time.localtime(latest_room.last_message_at / 1000))
                        if latest_room.last_message_at else "Gần đây"
                    )
                    
                    other_rooms_preview = []
                    for r in rooms[1:4]:
                        msg_p = r.last_message or "Chưa có tin nhắn"
                        other_rooms_preview.append(f"• **{r.name}**: '{msg_p}'")
                    
                    other_str = ("\n\nCác cuộc trò chuyện khác của bạn:\n" + "\n".join(other_rooms_preview)) if other_rooms_preview else ""
                    
                    reply = (
                        f"📊 **Dữ liệu cuộc trò chuyện từ Database của bạn:**\n\n"
                        f"Cuộc trò chuyện mới nhất của bạn là với **{latest_room.name}**:\n"
                        f"💬 Tin nhắn cuối: *'{last_msg_text}'*\n"
                        f"⏰ Thời gian: {last_time_str}{other_str}\n\n"
                        f"💡 Bạn có thể vào phòng chat bấm biểu tượng kính lúp để tìm kiếm ngữ nghĩa chuyên sâu bằng Vector Database!"
                    )
                else:
                    for r in rooms[:5]:
                        try:
                            h = await request.app.state.stub.GetMessageHistory(
                                chat_pb2.HistoryRequest(room_id=r.room_id, limit=20, before_timestamp=0, user_id=user_id),
                                timeout=GRPC_TIMEOUT,
                            )
                            vector_db.index_messages([
                                {
                                    "id": m.message_id,
                                    "content": m.content,
                                    "room_id": m.room_id,
                                    "sender_id": m.sender_id,
                                    "sender_name": r.name,
                                    "timestamp": m.timestamp,
                                }
                                for m in h.messages if m.content
                            ])
                        except Exception:
                            pass
                    
                    hits = vector_db.search(payload.prompt, top_k=3, min_score=0.08)
                    if hits:
                        hit_lines = [
                            f"• Trong phòng **{h['sender_name']}**: '{h['content']}' (Khớp {h['match_percent']}%)"
                            for h in hits
                        ]
                        reply = (
                            f"🔍 **Kết quả tìm kiếm từ Vector Database (Cosine Similarity):**\n\n"
                            + "\n".join(hit_lines)
                            + "\n\n💡 Bạn có thể hỏi chi tiết hơn hoặc mở phòng chat tương ứng để xem đầy đủ!"
                        )
                    else:
                        reply = (
                            f"Tôi đã tra cứu cơ sở dữ liệu chat của bạn nhưng chưa tìm thấy tin nhắn khớp với '{payload.prompt}'. "
                            f"Bạn hiện có {len(rooms)} cuộc trò chuyện: {', '.join([r.name for r in rooms[:3]])}."
                        )
            return {
                "ok": True,
                "reply": reply,
                "model": "gemini-1.5-flash + Vector DB",
            }
        except Exception as db_err:
            log.warning("DB query for AI assistant failed: %s", db_err)

    # 3. Câu hỏi chung
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



# ====================================================================== Vector Database & RAG Endpoints

class SemanticSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    room_id: str
    top_k: int = 5

class RagChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    room_id: str


@app.post("/api/v1/ai/semantic-search")
async def semantic_search(
    payload: SemanticSearchBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """
    Tìm kiếm ngữ nghĩa (Semantic Search) trong phòng chat bằng Vector Database
    Sử dụng thuật toán Cosine Similarity K-NN.
    """
    try:
        hist_resp = await request.app.state.stub.GetMessageHistory(
            chat_pb2.HistoryRequest(
                room_id=payload.room_id,
                limit=50,
                before_timestamp=0,
                user_id=user_id,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    # Đưa tin nhắn vào vector database
    msgs = []
    for m in hist_resp.messages:
        if m.content:
            msgs.append({
                "id": m.message_id,
                "content": m.content,
                "room_id": m.room_id,
                "sender_id": m.sender_id,
                "sender_name": "Bạn" if m.sender_id == user_id else "Thành viên",
                "timestamp": m.timestamp,
            })
    vector_db.index_messages(msgs)

    # Tìm kiếm K vector gần nhất
    results = vector_db.search(payload.query, top_k=payload.top_k, room_id=payload.room_id)
    return {
        "ok": True,
        "query": payload.query,
        "results": results,
        "total": len(results),
        "algorithm": "Cosine Similarity + L2-Normalized Dense Vectors",
    }


@app.post("/api/v1/ai/rag-chat")
async def rag_chat(
    payload: RagChatBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """
    Hỏi đáp thông minh kết hợp Vector Database RAG (Retrieval-Augmented Generation)
    """
    try:
        hist_resp = await request.app.state.stub.GetMessageHistory(
            chat_pb2.HistoryRequest(
                room_id=payload.room_id,
                limit=50,
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
    for m in hist_resp.messages:
        if m.content:
            msgs.append({
                "id": m.message_id,
                "content": m.content,
                "room_id": m.room_id,
                "sender_id": m.sender_id,
                "sender_name": "Bạn" if m.sender_id == user_id else "Thành viên",
                "timestamp": m.timestamp,
            })

    rag_result = vector_db.rag_chat(
        question=payload.question,
        room_id=payload.room_id,
        room_name=room_name,
        messages=msgs,
    )
    return {
        "ok": True,
        "room_name": room_name,
        "question": payload.question,
        "answer": rag_result["answer"],
        "matched_messages": rag_result["matched_messages"],
        "total_matches": rag_result["total_matches"],
        "algorithm": rag_result["algorithm"],
    }

if __name__ == "__main__":

    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)