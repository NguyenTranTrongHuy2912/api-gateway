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
from typing import Annotated, List, Literal, Optional

from dotenv import load_dotenv

load_dotenv(override=False)

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
from auth import create_token, decode_token, get_current_user, revoke_user_tokens, get_current_admin, get_user_role, set_user_role, USER_ROLES_STORE, DEFAULT_ADMIN_EMAILS
from ai_service import generate_ai_reply, summarize_conversation, rewrite_message_text, translate_message_text
from security_ai import sanitize_pii
from vector_db import vector_db
from email_service import generate_and_save_otp, verify_otp_code, send_smtp_email, build_otp_html

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

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "chat_db")
mongo_db = None
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_db = mongo_client[MONGO_DB_NAME]
        log.info("Admin MongoDB connected successfully")
    except Exception as e:
        log.warning("Admin MongoDB connection error: %s", e)


def is_own_image_url(url: str) -> bool:
    """Chỉ chấp nhận file media (ảnh, video, tài liệu raw) nằm trong đúng tài khoản + thư mục Cloudinary của mình."""
    if not CLOUDINARY_CLOUD_NAME or not url:
        return False
    pattern = (
        rf"^https://res\.cloudinary\.com/{re.escape(CLOUDINARY_CLOUD_NAME)}/(?:image|video|raw)/upload/"
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
    elif expose_details and status in (401, 403, 404, 409, 429):
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

ALLOWED_ORIGINS = [
    orig.strip()
    for orig in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if orig.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else ["*"],
    allow_credentials=bool(ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReplyToDto(BaseModel):
    message_id: str = Field(min_length=1, max_length=100)
    sender_name: str = Field(min_length=1, max_length=100)
    content: str = Field(default="", max_length=1000)


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    room_id: str = Field(min_length=1, max_length=100)
    type: Literal["TEXT", "IMAGE", "VIDEO", "FILE"] = "TEXT"
    # TEXT: bắt buộc. IMAGE/VIDEO/FILE: để trống hoặc kèm caption/tên file.
    content: str = Field(default="", max_length=4000)
    # Optional: client-generated id. If omitted, the gateway generates a uuid.
    message_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    # Dùng cho IMAGE, VIDEO hoặc FILE
    media_url: Optional[str] = Field(default=None, max_length=500)
    media_width: int = Field(default=0, ge=0, le=20000)
    media_height: int = Field(default=0, ge=0, le=20000)
    media_duration_ms: int = Field(default=0, ge=0)
    # Trả lời / Trích dẫn tin nhắn
    reply_to: Optional[ReplyToDto] = None
    # Gửi tài liệu (Document / File)
    file_name: Optional[str] = Field(default=None, max_length=255)
    file_size: Optional[int] = Field(default=0, ge=0)
    # Chuyển tiếp tin nhắn (Forward)
    is_forwarded: bool = False

    @model_validator(mode="after")
    def check_by_type(self):
        if self.type == "TEXT":
            if not self.content:
                raise ValueError("content must not be empty")
        elif not is_own_image_url(self.media_url or ""):
            raise ValueError("media_url is not a valid uploaded media")
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
    password: str = Field(min_length=6, max_length=64)
    display_name: str = Field(min_length=1, max_length=50)
    otp_code: str = Field(min_length=1, max_length=10)


class LoginBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    account: str = Field(min_length=1, max_length=254)  # email
    password: str = Field(min_length=1, max_length=64)


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
    d = {
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
        "pinned_message": None,
    }
    if mongo_db is not None:
        try:
            r_doc = mongo_db["rooms"].find_one({"_id": room.room_id}, {"pinned_message": 1})
            if r_doc and r_doc.get("pinned_message"):
                d["pinned_message"] = r_doc["pinned_message"]
        except Exception:
            pass
    return d


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
    account_clean = payload.account.strip().lower()
    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Địa chỉ email không hợp lệ.")

    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(account=account_clean), timeout=GRPC_TIMEOUT
        )
        return {
            "expires_in_seconds": resp.expires_in_seconds,
            "resend_after_seconds": resp.resend_after_seconds,
            "message": "Đã gửi mã xác thực OTP về email của bạn.",
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


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
@limiter.limit("5/minute")
async def register(request: Request, payload: RegisterBody):
    account_clean = payload.account.strip().lower()
    otp_clean = payload.otp_code.strip()

    try:
        resp = await request.app.state.auth_stub.Register(
            chat_pb2.RegisterRequest(
                account=account_clean,
                password=payload.password,
                display_name=payload.display_name.strip(),
                otp_code=otp_clean,
            ),
            timeout=GRPC_TIMEOUT,
        )
        role = getattr(resp, "role", None) or get_user_role(resp.user_id, account_clean)
        return {
            "token": create_token(resp.user_id, role=role),
            "user_id": resp.user_id,
            "display_name": resp.display_name,
            "role": role,
            "avatar_url": getattr(resp, "avatar_url", "") or "",
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


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
        search_term = re.escape(q.strip())
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
    if target_user_id == admin_id and body.role != "ADMIN":
        raise HTTPException(status_code=400, detail="Không thể tự hạ quyền Admin của chính mình.")

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
    if target_user_id == admin_id and not body.is_active:
        raise HTTPException(status_code=400, detail="Không thể tự khóa tài khoản của chính mình.")

    if mongo_db is not None:
        mongo_db["users"].update_one(
            {"_id": target_user_id},
            {"$set": {"is_active": body.is_active}}
        )

    # Nếu tài khoản bị khóa, lập tức thu hồi token và cắt đứt kết nối WebSocket
    if not body.is_active:
        revoke_user_tokens(target_user_id)
        if target_user_id in active_user_connections:
            for ws in list(active_user_connections[target_user_id]):
                try:
                    asyncio.create_task(ws.close(code=status.WS_1008_POLICY_VIOLATION))
                except Exception:
                    pass

    return {
        "ok": True,
        "user_id": target_user_id,
        "is_active": body.is_active,
        "updated_by": admin_id,
    }

class AdminRequestOtpBody(BaseModel):
    account: str
    display_name: Optional[str] = ""


@app.post("/api/v1/admin/request-otp")
async def admin_request_otp(
    payload: AdminRequestOtpBody,
    admin_id: str = Depends(get_current_admin),
):
    """Admin yêu cầu gửi mã xác thực OTP về chính email của người dùng mới sắp được tạo."""
    account_clean = payload.account.strip().lower()
    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Email tài khoản không hợp lệ.")

    if mongo_db is not None:
        existing = mongo_db["users"].find_one({"account": account_clean})
        if existing:
            raise HTTPException(status_code=409, detail="Email này đã tồn tại trên hệ thống. Không thể tạo mới.")

    return generate_and_save_otp(
        mongo_db,
        account_clean,
        purpose="register",
        display_name=payload.display_name or account_clean.split("@")[0],
    )


class CreateUserAdminBody(BaseModel):
    account: str
    password: str
    display_name: str
    role: Literal["ADMIN", "USER"] = "USER"
    otp_code: str = Field(min_length=1, max_length=10)


@app.post("/api/v1/admin/users")
async def create_user_admin(
    payload: CreateUserAdminBody,
    request: Request,
    admin_id: str = Depends(get_current_admin),
):
    """Admin tạo người dùng mới - BẮT BUỘC PHẢI XÁC THỰC MÃ OTP ĐÃ GỬI VỀ CHÍNH EMAIL ĐÓ."""
    account_clean = payload.account.lower().strip()
    otp_clean = payload.otp_code.strip()

    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Email tài khoản không hợp lệ.")
    if not payload.password or len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 6 ký tự.")
    if not otp_clean:
        raise HTTPException(status_code=400, detail="Vui lòng nhập mã xác thực OTP đã được gửi về email.")

    # Đăng ký qua gRPC Backend chuẩn mật khẩu BCrypt
    try:
        resp = await request.app.state.auth_stub.Register(
            chat_pb2.RegisterRequest(
                account=account_clean,
                password=payload.password,
                display_name=payload.display_name.strip(),
                otp_code=otp_clean,
            ),
            timeout=GRPC_TIMEOUT,
        )
        set_user_role(resp.user_id, payload.role)
        return {
            "ok": True,
            "user_id": resp.user_id,
            "display_name": resp.display_name,
            "account": account_clean,
            "role": payload.role,
            "message": "Đã xác thực OTP và tạo tài khoản người dùng thành công!",
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@app.delete("/api/v1/admin/users/{target_user_id}")
async def delete_user_admin(
    target_user_id: str,
    admin_id: str = Depends(get_current_admin),
):
    if target_user_id == admin_id:
        raise HTTPException(status_code=400, detail="Không thể xóa tài khoản của chính mình.")
    if mongo_db is not None:
        mongo_db["users"].delete_one({"_id": target_user_id})

    # Lập tức thu hồi token và cắt WebSocket của user bị xóa
    revoke_user_tokens(target_user_id)
    if target_user_id in active_user_connections:
        for ws in list(active_user_connections[target_user_id]):
            try:
                asyncio.create_task(ws.close(code=status.WS_1008_POLICY_VIOLATION))
            except Exception:
                pass

    return {"ok": True, "user_id": target_user_id, "message": "Đã xóa tài khoản thành công."}

# ------------------------------------------------------------------ Users


@app.get("/api/v1/users/search")
async def search_users(
    request: Request,
    q: str = Query(default="", max_length=100),
    limit: int = Query(10, ge=1, le=20),
    user_id: str = Depends(get_current_user),
):
    q_clean = q.strip()
    if len(q_clean) < 2:
        return {"users": []}

    try:
        resp = await request.app.state.stub.SearchUsers(
            chat_pb2.SearchUsersRequest(requester_id=user_id, query=q_clean, limit=limit),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    requester_role = get_user_role(user_id)
    filtered = []
    for u in resp.users:
        if u.user_id == user_id:
            continue
        u_email = (u.email or "").lower().strip()
        # Bảo mật: Người dùng thông thường KHÔNG ĐƯỢC nhìn thấy tài khoản của ADMIN
        if requester_role != "ADMIN":
            if u_email in DEFAULT_ADMIN_EMAILS or get_user_role(u.user_id, u_email) == "ADMIN":
                continue
            if mongo_db is not None:
                admin_check = mongo_db["users"].find_one({"_id": u.user_id, "role": "ADMIN"})
                if admin_check:
                    continue
        filtered.append({
            "user_id": u.user_id,
            "display_name": u.display_name,
            "email": u.email,
        })

    return {"users": filtered}


@app.get("/api/v1/users/suggestions")
async def get_user_suggestions(
    request: Request,
    limit: int = Query(10, ge=1, le=20),
    user_id: str = Depends(get_current_user),
):
    """Gợi ý bạn bè phù hợp. Tuyệt đối ẩn danh tính và tài khoản Admin khỏi người dùng thường."""
    requester_role = get_user_role(user_id)
    users_list = []

    # 1. Thử lấy từ MongoDB
    if mongo_db is not None:
        try:
            query = {"_id": {"$ne": user_id}, "is_active": {"$ne": False}}
            if requester_role != "ADMIN":
                query["role"] = {"$ne": "ADMIN"}
                query["account"] = {"$nin": list(DEFAULT_ADMIN_EMAILS)}

            docs = mongo_db["users"].find(query, {"password_hash": 0}).limit(limit)
            for d in docs:
                acc = d.get("account", "")
                if requester_role != "ADMIN" and acc.lower() in DEFAULT_ADMIN_EMAILS:
                    continue
                users_list.append({
                    "user_id": str(d.get("_id", "")),
                    "display_name": d.get("display_name", "") or acc.split("@")[0],
                    "email": acc,
                })
            if users_list:
                return {"users": users_list}
        except Exception as e:
            log.warning("User suggestions from mongo error: %s", e)

    # 2. Fallback: Dùng search gRPC
    try:
        resp = await request.app.state.stub.SearchUsers(
            chat_pb2.SearchUsersRequest(requester_id=user_id, query="user", limit=limit * 2),
            timeout=GRPC_TIMEOUT,
        )
        for u in resp.users:
            if u.user_id == user_id:
                continue
            u_email = (u.email or "").lower().strip()
            if requester_role != "ADMIN":
                if u_email in DEFAULT_ADMIN_EMAILS or get_user_role(u.user_id, u_email) == "ADMIN":
                    continue
            users_list.append({
                "user_id": u.user_id,
                "display_name": u.display_name,
                "email": u.email,
            })
            if len(users_list) >= limit:
                break
    except Exception:
        pass

    return {"users": users_list}


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




class AddRoomMembersBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    member_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=99
    )


class UpdateRoomNameBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Khớp giới hạn 50 ký tự của backend Java
    name: str = Field(min_length=1, max_length=50)


# Thao tác nhóm: mọi kiểm tra quyền và ghi dữ liệu nằm ở Java; gateway chỉ chuyển yêu cầu.


@app.post("/api/v1/rooms/{room_id}/leave")
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
        raise to_http_error(e, expose_details=True)
    return {"ok": resp.is_success, "room_id": room_id, "message": resp.status_message}


@app.post("/api/v1/rooms/{room_id}/members")
async def add_room_members(
    room_id: str,
    payload: AddRoomMembersBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.AddRoomMembers(
            chat_pb2.AddRoomMembersRequest(
                user_id=user_id,  # từ token, không nhận từ client
                room_id=room_id,
                member_ids=payload.member_ids,
            ),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "ok": True,
        "room_id": room_id,
        "added_count": resp.added_count,
        "room": room_to_dict(resp.room),
    }


@app.patch("/api/v1/rooms/{room_id}/name")
async def update_room_name(
    room_id: str,
    payload: UpdateRoomNameBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        resp = await request.app.state.stub.UpdateRoomName(
            chat_pb2.UpdateRoomNameRequest(user_id=user_id, room_id=room_id, name=payload.name),
            timeout=GRPC_TIMEOUT,
        )
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
    return {
        "ok": True,
        "room_id": room_id,
        "new_name": resp.room.name,
        "room": room_to_dict(resp.room),
    }


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


class MessageReactionBody(BaseModel):
    emoji: str = Field(min_length=1, max_length=10)


class PinMessageBody(BaseModel):
    message_id: str = Field(min_length=1, max_length=100)


class EditMessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class CreatePollBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    options: list[str]
    is_multiple_choice: bool = False
    is_anonymous: bool = False


class VotePollBody(BaseModel):
    option_id: str = Field(min_length=1, max_length=50)


def format_poll_response(poll: dict) -> dict:
    options = poll.get("options", [])
    total_votes = sum(len(opt.get("voter_ids", [])) for opt in options)
    unique_voters = set()
    for opt in options:
        unique_voters.update(opt.get("voter_ids", []))

    return {
        "id": poll["_id"],
        "room_id": poll.get("room_id", ""),
        "message_id": poll.get("message_id", ""),
        "creator_id": poll.get("creator_id", ""),
        "creator_name": poll.get("creator_name", "Thành viên"),
        "question": poll.get("question", ""),
        "options": [
            {
                "id": opt["id"],
                "text": opt["text"],
                "voter_ids": opt.get("voter_ids", []),
                "vote_count": len(opt.get("voter_ids", [])),
                "percentage": round((len(opt.get("voter_ids", [])) / total_votes * 100), 1) if total_votes > 0 else 0.0,
            }
            for opt in options
        ],
        "is_multiple_choice": bool(poll.get("is_multiple_choice", False)),
        "is_anonymous": bool(poll.get("is_anonymous", False)),
        "is_closed": bool(poll.get("is_closed", False)),
        "created_at": poll.get("created_at", 0),
        "total_votes": total_votes,
        "total_voters": len(unique_voters),
    }


@app.post("/api/v1/rooms/{room_id}/messages/{message_id}/revoke")
async def revoke_message(
    room_id: str,
    message_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Thu hồi tin nhắn 2 phía trong vòng 30 phút sau khi gửi."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    msg = mongo_db["messages"].find_one({"_id": message_id, "room_id": room_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn cần thu hồi.")

    if msg.get("sender_id") != user_id:
        raise HTTPException(status_code=403, detail="Bạn chỉ có thể thu hồi tin nhắn do chính mình gửi.")

    if msg.get("is_revoked"):
        return {"ok": True, "message_id": message_id, "message": "Tin nhắn đã được thu hồi trước đó."}

    msg_time = msg.get("timestamp", 0)
    now_ms = int(time.time() * 1000)
    # Giới hạn 30 phút (30 * 60 * 1000 ms)
    if (now_ms - msg_time) > 30 * 60 * 1000:
        raise HTTPException(
            status_code=400,
            detail="Đã quá 30 phút kể từ lúc gửi. Không thể thu hồi tin nhắn này nữa."
        )

    revoked_text = "Tin nhắn đã được thu hồi"
    mongo_db["messages"].update_one(
        {"_id": message_id},
        {"$set": {"content": revoked_text, "is_revoked": True, "revoked_at": now_ms}}
    )

    # Cập nhật last_message của room nếu đây là tin nhắn cuối cùng
    room = mongo_db["rooms"].find_one({"_id": room_id})
    if room and room.get("last_message_timestamp") == msg_time:
        mongo_db["rooms"].update_one(
            {"_id": room_id},
            {"$set": {"last_message_content": revoked_text}}
        )

    # Phát tín hiệu realtime qua WebSocket tới toàn bộ thành viên trong phòng
    asyncio.create_task(broadcast_ws_event({
        "type": "MESSAGE_REVOKED",
        "room_id": room_id,
        "message_id": message_id,
        "content": revoked_text,
        "sender_id": user_id,
        "timestamp": now_ms,
    }))

    return {"ok": True, "message_id": message_id, "content": revoked_text}


@app.post("/api/v1/rooms/{room_id}/messages/{message_id}/react")
async def react_message(
    room_id: str,
    message_id: str,
    body: MessageReactionBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Thả cảm xúc cho tin nhắn (👍, ❤️, 😂, 😮, 😢, 😡)"""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    msg = mongo_db["messages"].find_one({"_id": message_id, "room_id": room_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn.")

    reactions = msg.get("reactions") or {}
    emoji = body.emoji.strip()

    # Toggle reaction
    current_users = reactions.get(emoji, [])
    if user_id in current_users:
        current_users.remove(user_id)
        if not current_users:
            reactions.pop(emoji, None)
        else:
            reactions[emoji] = current_users
    else:
        # Gỡ user khỏi các emoji khác (mỗi user thả 1 cảm xúc chính)
        for k in list(reactions.keys()):
            if user_id in reactions[k]:
                reactions[k].remove(user_id)
                if not reactions[k]:
                    reactions.pop(k, None)
        if emoji not in reactions:
            reactions[emoji] = []
        reactions[emoji].append(user_id)

    mongo_db["messages"].update_one(
        {"_id": message_id},
        {"$set": {"reactions": reactions}}
    )

    # Phát tín hiệu realtime qua WebSocket
    asyncio.create_task(broadcast_ws_event({
        "type": "MESSAGE_REACTION",
        "room_id": room_id,
        "message_id": message_id,
        "reactions": reactions,
        "user_id": user_id,
        "emoji": emoji,
    }))

    return {"ok": True, "message_id": message_id, "reactions": reactions}


@app.put("/api/v1/rooms/{room_id}/messages/{message_id}")
async def edit_message(
    room_id: str,
    message_id: str,
    body: EditMessageBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Chỉnh sửa tin nhắn đã gửi trong vòng 30 phút."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    msg = mongo_db["messages"].find_one({"_id": message_id, "room_id": room_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn cần chỉnh sửa.")

    if msg.get("sender_id") != user_id:
        raise HTTPException(status_code=403, detail="Bạn chỉ có thể chỉnh sửa tin nhắn do chính mình gửi.")

    if msg.get("is_revoked"):
        raise HTTPException(status_code=400, detail="Không thể chỉnh sửa tin nhắn đã thu hồi.")

    if msg.get("type") and msg.get("type") not in ("TEXT", "text"):
        raise HTTPException(status_code=400, detail="Chỉ có thể chỉnh sửa tin nhắn văn bản.")

    msg_time = msg.get("timestamp", 0)
    now_ms = int(time.time() * 1000)
    if (now_ms - msg_time) > 30 * 60 * 1000:
        raise HTTPException(
            status_code=400,
            detail="Đã quá 30 phút kể từ lúc gửi. Không thể chỉnh sửa tin nhắn này nữa."
        )

    new_content = body.content.strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="Nội dung tin nhắn không được để trống.")

    mongo_db["messages"].update_one(
        {"_id": message_id},
        {"$set": {"content": new_content, "is_edited": True, "edited_at": now_ms}}
    )

    room = mongo_db["rooms"].find_one({"_id": room_id})
    if room and room.get("last_message_timestamp") == msg_time:
        mongo_db["rooms"].update_one(
            {"_id": room_id},
            {"$set": {"last_message_content": new_content}}
        )

    asyncio.create_task(broadcast_ws_event({
        "type": "MESSAGE_EDITED",
        "room_id": room_id,
        "message_id": message_id,
        "content": new_content,
        "is_edited": True,
        "edited_at": now_ms,
    }))

    return {
        "ok": True,
        "message_id": message_id,
        "content": new_content,
        "is_edited": True,
        "edited_at": now_ms,
    }


@app.post("/api/v1/rooms/{room_id}/polls")
async def create_poll(
    room_id: str,
    body: CreatePollBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Tạo cuộc bình chọn mới trong phòng chat."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    cleaned_options = [opt.strip() for opt in body.options if opt and opt.strip()]
    if len(cleaned_options) < 2:
        raise HTTPException(status_code=400, detail="Cuộc bình chọn cần ít nhất 2 lựa chọn.")
    if len(cleaned_options) > 10:
        raise HTTPException(status_code=400, detail="Tối đa 10 lựa chọn cho một cuộc bình chọn.")

    sender = mongo_db["users"].find_one({"_id": user_id})
    sender_name = sender.get("display_name", "Thành viên") if sender else "Thành viên"

    now_ms = int(time.time() * 1000)
    poll_id = f"poll_{uuid.uuid4().hex[:12]}"
    msg_id = f"msg_{uuid.uuid4().hex[:16]}"
    poll_content = f"📊 [Bình chọn] {body.question.strip()}"

    # Gửi qua gRPC backend để ghi nhận vào lịch sử phòng chat
    grpc_request = chat_pb2.ChatMessage(
        message_id=msg_id,
        room_id=room_id,
        sender_id=user_id,
        content=poll_content,
        timestamp=now_ms,
        type=chat_pb2.ChatMessage.TEXT,
    )
    try:
        await request.app.state.stub.SendMessage(grpc_request, timeout=GRPC_TIMEOUT)
    except grpc.RpcError as e:
        log.warning("gRPC SendMessage for poll warning: %s", e)

    poll_options = [
        {"id": f"opt_{i}", "text": opt, "voter_ids": []}
        for i, opt in enumerate(cleaned_options)
    ]
    poll_doc = {
        "_id": poll_id,
        "room_id": room_id,
        "message_id": msg_id,
        "creator_id": user_id,
        "creator_name": sender_name,
        "question": body.question.strip(),
        "options": poll_options,
        "is_multiple_choice": body.is_multiple_choice,
        "is_anonymous": body.is_anonymous,
        "is_closed": False,
        "created_at": now_ms,
    }
    mongo_db["polls"].insert_one(poll_doc)

    mongo_db["messages"].update_one(
        {"_id": msg_id},
        {"$set": {
            "room_id": room_id,
            "sender_id": user_id,
            "content": poll_content,
            "type": "POLL",
            "poll_id": poll_id,
            "timestamp": now_ms,
        }},
        upsert=True,
    )

    formatted = format_poll_response(poll_doc)

    asyncio.create_task(broadcast_ws_event({
        "type": "POLL_CREATED",
        "room_id": room_id,
        "poll_id": poll_id,
        "message_id": msg_id,
        "poll": formatted,
    }))

    return {"ok": True, "poll_id": poll_id, "message_id": msg_id, "poll": formatted}


@app.post("/api/v1/chat/polls/{poll_id}/vote")
async def vote_poll(
    poll_id: str,
    body: VotePollBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Bình chọn hoặc thay đổi bình chọn trong Poll."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    poll = mongo_db["polls"].find_one({"_id": poll_id})
    if not poll:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc bình chọn.")

    if poll.get("is_closed"):
        raise HTTPException(status_code=400, detail="Cuộc bình chọn này đã kết thúc.")

    target_opt_id = body.option_id.strip()
    options = poll.get("options", [])
    valid_opt_ids = {opt["id"] for opt in options}
    if target_opt_id not in valid_opt_ids:
        raise HTTPException(status_code=400, detail="Lựa chọn không hợp lệ.")

    is_multiple = poll.get("is_multiple_choice", False)

    for opt in options:
        voters = opt.get("voter_ids", [])
        if opt["id"] == target_opt_id:
            if user_id in voters:
                voters.remove(user_id)
            else:
                voters.append(user_id)
        else:
            if not is_multiple and user_id in voters:
                voters.remove(user_id)
        opt["voter_ids"] = voters

    mongo_db["polls"].update_one(
        {"_id": poll_id},
        {"$set": {"options": options}}
    )

    poll["options"] = options
    formatted = format_poll_response(poll)

    asyncio.create_task(broadcast_ws_event({
        "type": "POLL_UPDATED",
        "room_id": poll.get("room_id"),
        "poll_id": poll_id,
        "poll": formatted,
    }))

    return {"ok": True, "poll": formatted}


@app.get("/api/v1/chat/polls/{poll_id}")
async def get_poll(
    poll_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Lấy chi tiết và kết quả cuộc bình chọn."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    poll = mongo_db["polls"].find_one({"_id": poll_id})
    if not poll:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc bình chọn.")

    return {"ok": True, "poll": format_poll_response(poll)}


@app.post("/api/v1/rooms/{room_id}/pin")
async def pin_message(
    room_id: str,
    body: PinMessageBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Ghim tin nhắn quan trọng lên đầu phòng chat."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    msg = mongo_db["messages"].find_one({"_id": body.message_id, "room_id": room_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn cần ghim.")

    sender = mongo_db["users"].find_one({"_id": msg.get("sender_id")})
    sender_name = sender.get("display_name", "Thành viên") if sender else "Thành viên"

    pinned_data = {
        "id": body.message_id,
        "content": msg.get("content", ""),
        "sender_id": msg.get("sender_id", ""),
        "sender_name": sender_name,
        "timestamp": msg.get("timestamp", int(time.time() * 1000)),
        "pinned_by": user_id,
        "pinned_at": int(time.time() * 1000),
    }

    mongo_db["rooms"].update_one(
        {"_id": room_id},
        {"$set": {"pinned_message": pinned_data}}
    )

    asyncio.create_task(broadcast_ws_event({
        "type": "ROOM_PINNED_MESSAGE",
        "room_id": room_id,
        "pinned_message": pinned_data,
    }))

    return {"ok": True, "room_id": room_id, "pinned_message": pinned_data}


@app.delete("/api/v1/rooms/{room_id}/pin")
async def unpin_message(
    room_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Bỏ ghim tin nhắn của phòng chat."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    mongo_db["rooms"].update_one(
        {"_id": room_id},
        {"$unset": {"pinned_message": ""}}
    )

    asyncio.create_task(broadcast_ws_event({
        "type": "ROOM_PINNED_MESSAGE",
        "room_id": room_id,
        "pinned_message": None,
    }))

    return {"ok": True, "room_id": room_id, "pinned_message": None}


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

    if resource_type in ("video",):
        res_type = "video"
        transformation = ""
    elif resource_type in ("raw", "document", "file"):
        res_type = "raw"
        transformation = ""
    else:
        res_type = "image"
        transformation = IMAGE_INCOMING_TRANSFORMATION
    timestamp = int(time.time())
    public_id = f"{CLOUDINARY_FOLDER}/{uuid.uuid4().hex}"
    
    if res_type in ("video", "raw"):
        to_sign = f"public_id={public_id}&timestamp={timestamp}"
    else:
        to_sign = (
            f"public_id={public_id}&timestamp={timestamp}"
            f"&transformation={IMAGE_INCOMING_TRANSFORMATION}"
        )

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


async def handle_in_room_ai_mention(room_id: str, prompt: str, user_id: str, stub):
    """
    Xử lý triệu hồi @HQ AI trực tiếp trong phòng chat nhóm / chat 1-1.
    Tự động phân tích câu hỏi và gửi câu trả lời của HQ AI vào phòng cho cả nhóm cùng xem.
    Hoàn toàn không chặn Event Loop, ghi trực tiếp DB và phát WebSocket tức thời.
    """
    try:
        await asyncio.sleep(0.3)  # Đợi 300ms để tin nhắn của người dùng hiện trước
        clean_q = re.sub(r"(?i)@(hq\s*ai|hqai|ai|hq)[\s:,]?", "", prompt).strip()
        if not clean_q:
            clean_q = "Chào HQ AI"

        q_norm = normalize_vn(clean_q)
        if any(k in q_norm for k in ["tom tat", "tóm tắt", "tong hop", "tổng hợp"]):
            msgs = []
            if mongo_db is not None:
                docs = list(mongo_db["messages"].find({"room_id": room_id}).sort("timestamp", -1).limit(30))
                docs.reverse()
                msgs = [
                    {
                        "sender_id": d.get("sender_id"),
                        "sender_name": "Bạn" if d.get("sender_id") == user_id else ("HQ AI" if d.get("sender_id") == "hq_ai_bot" else "Thành viên"),
                        "content": d.get("content", ""),
                        "timestamp": d.get("timestamp", 0),
                    }
                    for d in docs
                    if d.get("content") and not str(d.get("content")).startswith("🤖 [HQ AI]")
                ]
            else:
                try:
                    hist_resp = await stub.GetMessageHistory(
                        chat_pb2.HistoryRequest(room_id=room_id, limit=25, before_timestamp=0, user_id=user_id),
                        timeout=3.0,
                    )
                    msgs = [
                        {
                            "sender_id": m.sender_id,
                            "sender_name": "Bạn" if m.sender_id == user_id else "Thành viên",
                            "content": m.content,
                            "timestamp": m.timestamp,
                        }
                        for m in reversed(hist_resp.messages)
                        if m.content and not m.content.startswith("🤖 [HQ AI]")
                    ]
                except Exception:
                    msgs = []
            reply_text = await asyncio.to_thread(summarize_conversation, "Phòng chat", msgs)
        else:
            reply_text = await asyncio.to_thread(generate_ai_reply, clean_q)

        now_ms = int(time.time() * 1000)
        bot_msg_id = str(uuid.uuid4())
        full_content = f"🤖 [HQ AI]: {reply_text}"

        if mongo_db is not None:
            # 1. Đảm bảo bot account tồn tại trong DB
            mongo_db["users"].update_one(
                {"_id": "hq_ai_bot"},
                {"$setOnInsert": {
                    "account": "hq.ai.bot@chatapp.system",
                    "display_name": "HQ AI 🤖",
                    "avatar_url": "https://api.dicebear.com/7.x/bottts/png?seed=HQAI",
                    "role": "USER",
                    "is_active": True,
                    "created_at": now_ms,
                }},
                upsert=True
            )
            # 2. Đảm bảo bot có trong thành viên phòng
            mongo_db["rooms"].update_one(
                {"_id": room_id},
                {"$addToSet": {"member_ids": "hq_ai_bot"}}
            )
            # 3. Lưu tin nhắn trực tiếp vào DB
            mongo_db["messages"].insert_one({
                "_id": bot_msg_id,
                "room_id": room_id,
                "sender_id": "hq_ai_bot",
                "content": full_content,
                "timestamp": now_ms,
                "type": "TEXT",
            })
            # 4. Cập nhật last_message cho phòng
            mongo_db["rooms"].update_one(
                {"_id": room_id},
                {"$set": {
                    "last_message": {
                        "content": full_content,
                        "sender_id": "hq_ai_bot",
                        "timestamp": now_ms,
                    },
                    "last_message_at": now_ms,
                }}
            )

        # 5. Phát WebSocket tức thì
        await notify_new_message(room_id, "hq_ai_bot", full_content, "TEXT", stub)
    except Exception as e:
        log.warning("handle_in_room_ai_mention failed: %s", e)


# ------------------------------------------------------------------- Chat


@app.post("/api/v1/chat/send")
async def send_message(
    payload: SendMessageRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    grpc_msg_type = (
        chat_pb2.MESSAGE_TYPE_IMAGE if payload.type == "IMAGE"
        else chat_pb2.MESSAGE_TYPE_VIDEO if payload.type == "VIDEO"
        else chat_pb2.MESSAGE_TYPE_TEXT
    )
    msg_content = payload.content
    if payload.type == "FILE" and not msg_content:
        msg_content = f"[Tài liệu] {payload.file_name or 'Tập tin'}"

    grpc_request = chat_pb2.ChatMessage(
        message_id=payload.message_id or str(uuid.uuid4()),
        room_id=payload.room_id,
        sender_id=user_id,  # from the token, never from the client
        content=msg_content,
        timestamp=int(time.time() * 1000),
        type=grpc_msg_type,
        media_url=payload.media_url or "",
        media_width=payload.media_width,
        media_height=payload.media_height,
        media_duration_ms=payload.media_duration_ms,
    )
    try:
        resp = await request.app.state.stub.SendMessage(grpc_request, timeout=GRPC_TIMEOUT)
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)

    # Lưu trữ thêm metadata an toàn vào MongoDB (reply_to, file info)
    if mongo_db is not None:
        try:
            up_fields = {}
            if payload.reply_to:
                up_fields["reply_to"] = payload.reply_to.model_dump()
            if payload.is_forwarded:
                up_fields["is_forwarded"] = True
            if payload.type == "FILE":
                up_fields["type"] = "FILE"
                up_fields["file_name"] = payload.file_name
                up_fields["file_size"] = payload.file_size
                up_fields["media_url"] = payload.media_url
            if up_fields:
                mongo_db["messages"].update_one(
                    {"_id": resp.message_id or grpc_request.message_id},
                    {"$set": up_fields}
                )
        except Exception as ex:
            log.warning("Could not persist extra fields to Mongo: %s", ex)

    asyncio.create_task(notify_new_message(
        room_id=payload.room_id,
        sender_id=user_id,
        content=msg_content,
        msg_type=payload.type,
        stub=request.app.state.stub,
    ))

    # Tự động triệu hồi HQ AI trong phòng nếu tin nhắn chứa tag @HQ AI
    content_lower = (payload.content or "").lower()
    if any(m in content_lower for m in ["@hq ai", "@hqai", "@ai", "@hq"]):
        asyncio.create_task(handle_in_room_ai_mention(
            room_id=payload.room_id,
            prompt=payload.content,
            user_id=user_id,
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


@app.post("/api/v1/chat/messages/{message_id}/star")
async def toggle_star_message(
    message_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Đánh dấu / Lưu trữ tin nhắn quan trọng (Saved / Starred Message)."""
    if mongo_db is None:
        raise HTTPException(status_code=503, detail="Database chưa sẵn sàng")

    existing = mongo_db["starred_messages"].find_one({"user_id": user_id, "message_id": message_id})
    if existing:
        mongo_db["starred_messages"].delete_one({"_id": existing["_id"]})
        return {"ok": True, "message_id": message_id, "starred": False}

    msg = mongo_db["messages"].find_one({"_id": message_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn cần lưu.")

    sender = mongo_db["users"].find_one({"_id": msg.get("sender_id")})
    sender_name = sender.get("display_name", "Thành viên") if sender else "Thành viên"

    now_ms = int(time.time() * 1000)
    starred_doc = {
        "_id": f"{user_id}_{message_id}",
        "user_id": user_id,
        "message_id": message_id,
        "room_id": msg.get("room_id", ""),
        "content": msg.get("content", ""),
        "sender_id": msg.get("sender_id", ""),
        "sender_name": sender_name,
        "type": msg.get("type", "TEXT"),
        "media_url": msg.get("media_url"),
        "file_name": msg.get("file_name"),
        "file_size": msg.get("file_size"),
        "timestamp": msg.get("timestamp", now_ms),
        "starred_at": now_ms,
    }
    mongo_db["starred_messages"].replace_one(
        {"_id": starred_doc["_id"]},
        starred_doc,
        upsert=True
    )
    return {"ok": True, "message_id": message_id, "starred": True}


@app.get("/api/v1/chat/starred")
async def get_starred_messages(
    request: Request,
    room_id: Optional[str] = Query(default=None),
    user_id: str = Depends(get_current_user),
):
    """Lấy danh sách các tin nhắn đã đánh dấu / lưu trữ của user."""
    if mongo_db is None:
        return {"messages": []}

    query = {"user_id": user_id}
    if room_id:
        query["room_id"] = room_id

    docs = list(mongo_db["starred_messages"].find(query).sort("starred_at", -1).limit(100))
    return {
        "messages": [
            {
                "message_id": d["message_id"],
                "room_id": d.get("room_id", ""),
                "sender_id": d.get("sender_id", ""),
                "sender_name": d.get("sender_name", "Thành viên"),
                "content": d.get("content", ""),
                "type": d.get("type", "TEXT"),
                "media_url": d.get("media_url"),
                "file_name": d.get("file_name"),
                "file_size": d.get("file_size"),
                "timestamp": d.get("timestamp", 0),
                "starred_at": d.get("starred_at", 0),
            }
            for d in docs
        ]
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

    msg_ids = [m.message_id for m in resp.messages]
    extra_map = {}
    starred_set = set()
    if mongo_db is not None and msg_ids:
        try:
            docs = mongo_db["messages"].find(
                {"_id": {"$in": msg_ids}},
                {"content": 1, "is_revoked": 1, "is_edited": 1, "edited_at": 1, "reactions": 1, "reply_to": 1, "type": 1, "file_name": 1, "file_size": 1, "media_url": 1, "is_forwarded": 1, "poll_id": 1}
            )
            for d in docs:
                extra_map[d["_id"]] = {
                    "content": d.get("content"),
                    "is_revoked": bool(d.get("is_revoked", False)),
                    "is_edited": bool(d.get("is_edited", False)),
                    "edited_at": d.get("edited_at"),
                    "reactions": d.get("reactions") or {},
                    "reply_to": d.get("reply_to"),
                    "type": d.get("type"),
                    "file_name": d.get("file_name"),
                    "file_size": d.get("file_size"),
                    "media_url": d.get("media_url"),
                    "is_forwarded": bool(d.get("is_forwarded", False)),
                    "poll_id": d.get("poll_id"),
                }
            starred_docs = mongo_db["starred_messages"].find(
                {"user_id": user_id, "message_id": {"$in": msg_ids}},
                {"message_id": 1}
            )
            starred_set = {s["message_id"] for s in starred_docs}
        except Exception:
            pass

    return {
        "messages": [
            {
                "message_id": m.message_id,
                "room_id": m.room_id,
                "sender_id": m.sender_id,
                "content": "Tin nhắn đã được thu hồi" if extra_map.get(m.message_id, {}).get("is_revoked") else (extra_map.get(m.message_id, {}).get("content") or m.content),
                "timestamp": m.timestamp,
                "type": extra_map.get(m.message_id, {}).get("type") or MESSAGE_TYPE_TO_API.get(m.type, "TEXT"),
                "media_url": extra_map.get(m.message_id, {}).get("media_url") or m.media_url,
                "media_width": m.media_width,
                "media_height": m.media_height,
                "media_duration_ms": m.media_duration_ms,
                "is_revoked": extra_map.get(m.message_id, {}).get("is_revoked", False),
                "is_edited": extra_map.get(m.message_id, {}).get("is_edited", False),
                "edited_at": extra_map.get(m.message_id, {}).get("edited_at"),
                "reactions": extra_map.get(m.message_id, {}).get("reactions", {}),
                "reply_to": extra_map.get(m.message_id, {}).get("reply_to"),
                "file_name": extra_map.get(m.message_id, {}).get("file_name"),
                "file_size": extra_map.get(m.message_id, {}).get("file_size"),
                "is_forwarded": extra_map.get(m.message_id, {}).get("is_forwarded", False),
                "is_starred": m.message_id in starred_set,
                "poll_id": extra_map.get(m.message_id, {}).get("poll_id"),
            }
            for m in resp.messages
        ],
        "has_more": resp.has_more,
    }


# ---------------------------------------------------------------- Presence & Push State
active_user_connections: dict[str, set[WebSocket]] = {}
user_last_seen: dict[str, int] = {}
user_push_tokens: dict[str, str] = {}
active_calls: dict[str, dict] = {}


async def send_ws_to_user(target_user_id: str, payload: dict) -> bool:
    """Gửi trực tiếp sự kiện WebSocket tới tất cả các kết nối của một người dùng"""
    connections = active_user_connections.get(target_user_id, set())
    if not connections:
        return False
    delivered = False
    for ws in list(connections):
        try:
            await ws.send_json(payload)
            delivered = True
        except Exception:
            pass
    return delivered


async def record_call_history_message(room_id: str, sender_id: str, content: str, stub):
    """Ghi lại lịch sử cuộc gọi vào MongoDB và phát WebSocket tới cả phòng"""
    now_ms = int(time.time() * 1000)
    call_msg_id = str(uuid.uuid4())
    if mongo_db is not None:
        try:
            mongo_db["messages"].insert_one({
                "_id": call_msg_id,
                "room_id": room_id,
                "sender_id": sender_id,
                "content": content,
                "timestamp": now_ms,
                "type": "TEXT",
            })
            mongo_db["rooms"].update_one(
                {"_id": room_id},
                {"$set": {
                    "last_message": {
                        "content": content,
                        "sender_id": sender_id,
                        "timestamp": now_ms,
                    },
                    "last_message_at": now_ms,
                }}
            )
        except Exception as e:
            log.warning("Could not record call message to Mongo: %s", e)

    # Phát sự kiện NEW_MESSAGE qua WebSocket
    await notify_new_message(room_id, sender_id, content, "TEXT", stub)


async def handle_call_signal(ws: WebSocket, sender_user_id: str, data: dict, stub):
    """Xử lý điều phối tín hiệu WebRTC (Signaling) và ghi nhận trạng thái cuộc gọi"""
    msg_type = data.get("type")
    call_id = data.get("call_id") or str(uuid.uuid4())
    receiver_id = data.get("receiver_id")
    room_id = data.get("room_id")
    call_type = data.get("call_type", "audio")

    if msg_type == "CALL_OFFER":
        is_online = receiver_id in active_user_connections and len(active_user_connections[receiver_id]) > 0
        if not is_online:
            await ws.send_json({
                "type": "CALL_UNAVAILABLE",
                "call_id": call_id,
                "receiver_id": receiver_id,
                "reason": "offline",
                "message": "Người dùng hiện không trực tuyến."
            })
            if room_id:
                call_label = "📞 Cuộc gọi thoại nhỡ" if call_type == "audio" else "📹 Cuộc gọi video nhỡ"
                await record_call_history_message(room_id, sender_user_id, f"❌ {call_label}", stub)
            return

        active_calls[call_id] = {
            "caller_id": sender_user_id,
            "receiver_id": receiver_id,
            "room_id": room_id,
            "call_type": call_type,
            "caller_name": data.get("caller_name", "Người dùng"),
            "caller_avatar": data.get("caller_avatar", ""),
            "status": "ringing",
            "created_at": time.time(),
            "start_time": None,
        }

        await send_ws_to_user(receiver_id, {
            "type": "CALL_OFFER",
            "call_id": call_id,
            "caller_id": sender_user_id,
            "caller_name": data.get("caller_name", "Người dùng"),
            "caller_avatar": data.get("caller_avatar", ""),
            "receiver_id": receiver_id,
            "room_id": room_id,
            "call_type": call_type,
            "sdp": data.get("sdp"),
            "timestamp": int(time.time() * 1000),
        })

    elif msg_type == "CALL_ANSWER":
        caller_id = data.get("caller_id")
        accepted = data.get("accepted", False)
        call_info = active_calls.get(call_id)

        if accepted:
            if call_info:
                call_info["status"] = "connected"
                call_info["start_time"] = time.time()
            await send_ws_to_user(caller_id, {
                "type": "CALL_ANSWER",
                "call_id": call_id,
                "accepted": True,
                "responder_id": sender_user_id,
                "sdp": data.get("sdp"),
                "timestamp": int(time.time() * 1000),
            })
        else:
            if call_info and call_info.get("room_id"):
                r_id = call_info["room_id"]
                c_type = call_info.get("call_type", "audio")
                call_label = "Cuộc gọi thoại bị từ chối" if c_type == "audio" else "Cuộc gọi video bị từ chối"
                await record_call_history_message(r_id, caller_id, f"🚫 {call_label}", stub)
            active_calls.pop(call_id, None)

            await send_ws_to_user(caller_id, {
                "type": "CALL_HANGUP",
                "call_id": call_id,
                "reason": "declined",
                "message": "Người nhận đã từ chối cuộc gọi."
            })

    elif msg_type == "ICE_CANDIDATE":
        target_id = data.get("target_id") or receiver_id
        if target_id:
            await send_ws_to_user(target_id, {
                "type": "ICE_CANDIDATE",
                "call_id": call_id,
                "sender_id": sender_user_id,
                "candidate": data.get("candidate"),
            })

    elif msg_type == "CALL_HANGUP":
        call_info = active_calls.pop(call_id, None)
        other_user_id = data.get("target_id")
        reason = data.get("reason", "ended")
        duration = data.get("duration_seconds", 0)

        if not other_user_id and call_info:
            other_user_id = call_info["receiver_id"] if sender_user_id == call_info["caller_id"] else call_info["caller_id"]

        if other_user_id:
            await send_ws_to_user(other_user_id, {
                "type": "CALL_HANGUP",
                "call_id": call_id,
                "reason": reason,
                "duration_seconds": duration,
                "message": "Cuộc gọi đã kết thúc."
            })

        curr_room_id = room_id or (call_info.get("room_id") if call_info else None)
        c_type = call_type or (call_info.get("call_type") if call_info else "audio")
        caller_id = (call_info.get("caller_id") if call_info else None) or sender_user_id

        if curr_room_id:
            if duration > 0:
                mins = duration // 60
                secs = duration % 60
                time_str = f"{mins:02d}:{secs:02d}"
                call_icon = "📞 Cuộc gọi thoại" if c_type == "audio" else "📹 Cuộc gọi video"
                content = f"{call_icon} ({time_str})"
                await record_call_history_message(curr_room_id, caller_id, content, stub)
            elif reason in ("canceled", "timeout", "offline"):
                call_icon = "📞 Cuộc gọi thoại nhỡ" if c_type == "audio" else "📹 Cuộc gọi video nhỡ"
                content = f"❌ {call_icon}"
                await record_call_history_message(curr_room_id, caller_id, content, stub)

    elif msg_type == "CALL_BUSY":
        target_id = data.get("target_id")
        if target_id:
            await send_ws_to_user(target_id, {
                "type": "CALL_BUSY",
                "call_id": call_id,
                "message": "Người dùng đang có cuộc gọi khác."
            })

    elif msg_type == "USER_NOTE_BROADCAST":
        asyncio.create_task(broadcast_ws_event({
            "type": "USER_NOTE_BROADCAST",
            "user_id": sender_user_id,
            "display_name": data.get("display_name", ""),
            "note": data.get("note", ""),
            "created_at": data.get("created_at"),
            "expires_at": data.get("expires_at"),
        }))



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
    """Gửi push notification qua Expo Push API (đọc token từ RAM và fallback từ MongoDB)"""
    if not target_user_ids:
        return

    token_set = set()
    missing_uids = []

    for uid in target_user_ids:
        t = user_push_tokens.get(uid)
        if t and t.startswith("ExponentPushToken"):
            token_set.add(t)
        else:
            missing_uids.append(uid)

    # Nếu RAM chưa có (do server vừa restart), lấy lại từ MongoDB
    if missing_uids and mongo_db is not None:
        try:
            docs = mongo_db["users"].find(
                {"_id": {"$in": missing_uids}, "push_token": {"$exists": True, "$ne": ""}},
                {"_id": 1, "push_token": 1},
            )
            for d in docs:
                t = d.get("push_token", "")
                if t.startswith("ExponentPushToken"):
                    user_push_tokens[d["_id"]] = t
                    token_set.add(t)
        except Exception as e:
            log.warning("Could not load push tokens from Mongo: %s", e)

    if not token_set:
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
        for token in token_set
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

        if sender_id == "hq_ai_bot":
            sender_name = "HQ AI 🤖"
        else:
            sender_member = next((m for m in room.members if m.user_id == sender_id), None)
            sender_name = (
                sender_member.display_name
                if sender_member and sender_member.display_name
                else ""
            )

        if room.type == chat_pb2.ROOM_TYPE_GROUP and room.name:
            title = f"{room.name} · {sender_name}" if sender_name else room.name
        else:
            title = sender_name or "Tin nhắn mới"

        body_text = re.sub(r"^🤖\s*\[HQ AI\]:\s*", "", content or "")
        if msg_type == "IMAGE":
            body_text = "Đã gửi một hình ảnh 📷"
        elif msg_type == "VIDEO":
            body_text = "Đã gửi một video 🎬"
        elif msg_type == "FILE":
            body_text = "Đã gửi một tài liệu 📎"

        target_ids = [uid for uid in room.member_ids if uid != sender_id and uid != "hq_ai_bot"]
        await send_expo_push(target_ids, title=title, body=body_text, data={"roomId": room_id, "type": "chat"})
    except Exception as e:
        log.warning("Error pushing new message notification: %s", e)


@app.get("/api/v1/users/online")
async def get_online_users(request: Request, user_id: str = Depends(get_current_user)):
    """Bảo mật quyền riêng tư: Chỉ trả về trạng thái của bạn bè đang Online, không để lộ tài khoản Admin hay người lạ."""
    try:
        resp = await request.app.state.stub.ListFriends(
            chat_pb2.ListFriendsReq(user_id=user_id),
            timeout=GRPC_TIMEOUT,
        )
        friend_ids = {f.user_id for f in resp.friends}
        return {"online_user_ids": [uid for uid in active_user_connections.keys() if uid in friend_ids]}
    except Exception:
        return {"online_user_ids": []}


@app.post("/api/v1/users/push-token")
async def register_push_token(
    payload: RegisterPushTokenBody,
    user_id: str = Depends(get_current_user),
):
    user_push_tokens[user_id] = payload.push_token
    if mongo_db is not None:
        try:
            mongo_db["users"].update_one(
                {"_id": user_id},
                {"$set": {"push_token": payload.push_token}},
            )
        except Exception as e:
            log.warning("Could not save push token to Mongo: %s", e)
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

    # Gửi danh sách online ban đầu cho client vừa kết nối (Bảo mật: chỉ gửi bạn bè đang online)
    friend_ids = set()
    try:
        f_resp = await websocket.app.state.stub.ListFriends(
            chat_pb2.ListFriendsReq(user_id=user_id), timeout=3.0
        )
        friend_ids = {f.user_id for f in f_resp.friends}
    except Exception:
        pass
    online_friends = [uid for uid in active_user_connections.keys() if uid in friend_ids]
    try:
        await websocket.send_json({
            "type": "INITIAL_PRESENCE",
            "online_user_ids": online_friends,
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
                    msg_obj = {
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
                    if mongo_db is not None:
                        try:
                            extra_doc = mongo_db["messages"].find_one(
                                {"_id": event.message.message_id},
                                {"content": 1, "reply_to": 1, "type": 1, "file_name": 1, "file_size": 1, "media_url": 1, "is_forwarded": 1, "is_edited": 1, "edited_at": 1, "poll_id": 1}
                            )
                            if extra_doc:
                                if extra_doc.get("content"):
                                    msg_obj["content"] = extra_doc["content"]
                                if extra_doc.get("reply_to"):
                                    msg_obj["reply_to"] = extra_doc["reply_to"]
                                if extra_doc.get("type"):
                                    msg_obj["type"] = extra_doc["type"]
                                if extra_doc.get("file_name"):
                                    msg_obj["file_name"] = extra_doc["file_name"]
                                    msg_obj["file_size"] = extra_doc.get("file_size", 0)
                                if extra_doc.get("media_url"):
                                    msg_obj["media_url"] = extra_doc["media_url"]
                                if extra_doc.get("is_forwarded"):
                                    msg_obj["is_forwarded"] = True
                                if extra_doc.get("is_edited"):
                                    msg_obj["is_edited"] = True
                                    msg_obj["edited_at"] = extra_doc.get("edited_at")
                                if extra_doc.get("poll_id"):
                                    msg_obj["poll_id"] = extra_doc["poll_id"]
                        except Exception:
                            pass
                    payload["message"] = msg_obj
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
                elif event.event_type == chat_pb2.EVENT_ROOM_UPDATED:
                    payload["room_id"] = event.room.room_id
                elif event.event_type == chat_pb2.EVENT_READ_RECEIPT:
                    payload["user_id"] = event.user_id
                    payload["room_id"] = event.room.room_id
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
                continue
            try:
                msg_data = json.loads(text)
                if isinstance(msg_data, dict) and (
                    msg_data.get("type", "").startswith("CALL_")
                    or msg_data.get("type") in ("ICE_CANDIDATE", "USER_NOTE_BROADCAST")
                ):
                    await handle_call_signal(websocket, user_id, msg_data, stub)
            except Exception as e:
                log.debug("WS incoming parse error: %s", e)
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
    # Bảo mật: Người dùng thường không thể gửi kết bạn đến tài khoản ADMIN
    requester_role = get_user_role(user_id)
    if requester_role != "ADMIN":
        target_role = get_user_role(payload.receiver_id)
        if target_role == "ADMIN":
            raise HTTPException(
                status_code=403,
                detail="Không thể gửi yêu cầu kết bạn đến tài khoản Quản trị viên hệ thống."
            )
        if mongo_db is not None:
            target_doc = mongo_db["users"].find_one({"_id": payload.receiver_id})
            if target_doc:
                if target_doc.get("role") == "ADMIN" or target_doc.get("account", "").lower() in DEFAULT_ADMIN_EMAILS:
                    raise HTTPException(
                        status_code=403,
                        detail="Không thể gửi yêu cầu kết bạn đến tài khoản Quản trị viên hệ thống."
                    )
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



# ====================================================================== AI Endpoints (HQ AI - Google Gemini)

class AiChatHistoryItem(BaseModel):
    role: str = "user"  # 'user' | 'model'
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

@app.post("/api/v1/ai/chat")
async def ai_chat(
    payload: AiChatBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    p_norm = normalize_vn(payload.prompt)

    # 1. Kiểm tra xem người dùng có đang hỏi cụ thể về dữ liệu tin nhắn / database không
    is_asking_about_db = (
        ("tin nhan" in p_norm and any(k in p_norm for k in ["moi nhat", "gan day", "vua nhan", "tim", "tra cuu"]))
        or ("cuoc tro chuyen" in p_norm and any(k in p_norm for k in ["moi nhat", "gan day", "vua nhan"]))
        or ("vector database" in p_norm or "tim kiem tin nhan" in p_norm)
    )

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

    # 3. Câu hỏi chung & Đa năng (Multi-turn Context)
    history_dicts = [item.model_dump() for item in payload.history] if payload.history else None
    reply = generate_ai_reply(payload.prompt, history=history_dicts)
    return {
        "ok": True,
        "reply": reply,
        "model": "HQ AI",
    }


@app.post("/api/v1/ai/translate")
async def ai_translate_text(
    payload: AiTranslateBody,
    user_id: str = Depends(get_current_user),
):
    """Dịch tức thì tin nhắn trong phòng chat bằng AI (Gemini)"""
    res = translate_message_text(payload.text, target_lang=payload.target_lang)
    return {
        "ok": True,
        "original_text": res.get("original_text", payload.text),
        "translated_text": res.get("translated_text", ""),
        "target_lang": res.get("target_lang", payload.target_lang),
    }


@app.post("/api/v1/ai/rewrite")
async def ai_rewrite_text(
    payload: AiRewriteBody,
    user_id: str = Depends(get_current_user),
):
    """Viết lại tin nhắn nháp (Formal, Friendly, Short, English)"""
    rewritten = rewrite_message_text(payload.text, tone=payload.tone)
    return {
        "ok": True,
        "original": payload.text,
        "rewritten": rewritten,
        "tone": payload.tone,
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