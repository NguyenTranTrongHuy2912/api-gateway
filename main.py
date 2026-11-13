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

from dotenv import load_dotenv

load_dotenv(override=True)

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
from ai_service import generate_ai_reply, summarize_conversation, rewrite_message_text
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
        # Đảm bảo tài khoản nguyenvanquoclt1@gmail.com có vai trò USER theo yêu cầu
        try:
            mongo_db["users"].update_many(
                {"account": "nguyenvanquoclt1@gmail.com"},
                {"$set": {"role": "USER"}}
            )
        except Exception:
            pass
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
    account_clean = payload.account.strip().lower()
    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Địa chỉ email không hợp lệ.")

    # 1. Kiểm tra email đã đăng ký chưa
    if mongo_db is not None:
        existing = mongo_db["users"].find_one({"account": account_clean})
        if existing:
            raise HTTPException(status_code=409, detail="Email này đã được đăng ký tài khoản. Vui lòng đăng nhập.")

    # 2. Thử gọi gRPC RequestOtp
    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(account=account_clean), timeout=GRPC_TIMEOUT
        )
        # Gửi thêm email thật qua Gmail SMTP để đảm bảo 100% người dùng nhận được mail
        try:
            generate_and_save_otp(mongo_db, account_clean, purpose="register")
        except Exception:
            pass
        return {
            "expires_in_seconds": resp.expires_in_seconds,
            "resend_after_seconds": resp.resend_after_seconds,
            "message": "Đã gửi mã xác thực OTP về email của bạn.",
        }
    except grpc.RpcError as e:
        # Nếu gRPC thất bại (ví dụ gRPC backend chưa bật SMTP), dùng trực tiếp Python SMTP
        log.warning("gRPC RequestOtp error: %s. Chuyển sang gửi trực tiếp qua Gateway SMTP", e)
        return generate_and_save_otp(mongo_db, account_clean, purpose="register")


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
    account_clean = payload.account.strip().lower()
    otp_clean = payload.otp_code.strip()

    # Xác thực OTP qua MongoDB nếu có lưu
    if mongo_db is not None:
        verify_otp_code(mongo_db, account_clean, otp_clean, purpose="register")

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
        role = get_user_role(resp.user_id, account_clean)
        return {
            "token": create_token(resp.user_id, role=role),
            "user_id": resp.user_id,
            "display_name": resp.display_name,
            "role": role,
        }
    except grpc.RpcError as e:
        # Nếu gRPC thất bại nhưng OTP đã được verify thành công ở trên, tạo trực tiếp trong Mongo
        if mongo_db is not None:
            existing = mongo_db["users"].find_one({"account": account_clean})
            if existing:
                raise HTTPException(status_code=409, detail="Email này đã được đăng ký.")
            new_id = str(uuid.uuid4())
            hashed_pw = hashlib.sha256(payload.password.encode("utf-8")).hexdigest()
            now_ms = int(time.time() * 1000)
            mongo_db["users"].insert_one({
                "_id": new_id,
                "account": account_clean,
                "password_hash": hashed_pw,
                "display_name": payload.display_name.strip() or account_clean.split("@")[0],
                "role": "USER",
                "is_active": True,
                "created_at": now_ms,
            })
            set_user_role(new_id, "USER")
            return {
                "token": create_token(new_id, role="USER"),
                "user_id": new_id,
                "display_name": payload.display_name.strip() or account_clean.split("@")[0],
                "role": "USER",
            }
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

    # 1. Bắt buộc kiểm tra mã OTP đã gửi về email
    if mongo_db is not None:
        verify_otp_code(mongo_db, account_clean, otp_clean, purpose="register")

        existing = mongo_db["users"].find_one({"account": account_clean})
        if existing:
            raise HTTPException(status_code=400, detail="Tài khoản email này đã tồn tại trong hệ thống.")

        new_id = str(uuid.uuid4())
        hashed_pw = hashlib.sha256(payload.password.encode("utf-8")).hexdigest()
        now_ms = int(time.time() * 1000)
        mongo_db["users"].insert_one({
            "_id": new_id,
            "account": account_clean,
            "password_hash": hashed_pw,
            "display_name": payload.display_name.strip() or account_clean.split("@")[0],
            "role": payload.role,
            "is_active": True,
            "created_at": now_ms,
        })
        set_user_role(new_id, payload.role)
        return {
            "ok": True,
            "user_id": new_id,
            "display_name": payload.display_name.strip() or account_clean.split("@")[0],
            "account": account_clean,
            "role": payload.role,
            "message": "Đã xác thực OTP và tạo tài khoản người dùng thành công!",
        }

    # 2. Hoặc fallback qua gRPC Register
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
    member_ids: list[str]


class UpdateRoomNameBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@app.post("/api/v1/rooms/{room_id}/leave")
async def leave_room(
    room_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Người dùng rời khỏi phòng chat nhóm - Cập nhật dữ liệu thật trong MongoDB Atlas."""
    if mongo_db is not None:
        room = mongo_db["rooms"].find_one({"_id": room_id})
        if not room:
            raise HTTPException(status_code=404, detail="Không tìm thấy phòng chat.")

        members = room.get("member_ids", [])
        if user_id not in members:
            return {"ok": True, "room_id": room_id, "message": "Bạn không còn trong phòng chat này."}

        # Xóa user_id khỏi danh sách thành viên
        mongo_db["rooms"].update_one(
            {"_id": room_id},
            {"$pull": {"member_ids": user_id}}
        )

        # Lấy tên hiển thị của người rời nhóm
        user_doc = mongo_db["users"].find_one({"_id": user_id})
        display_name = user_doc.get("display_name", "Một thành viên") if user_doc else "Một thành viên"

        # Thêm tin nhắn hệ thống thông báo người dùng rời nhóm
        sys_msg_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        mongo_db["messages"].insert_one({
            "_id": sys_msg_id,
            "room_id": room_id,
            "sender_id": "system",
            "content": f"{display_name} đã rời khỏi nhóm.",
            "timestamp": now_ms,
            "type": "TEXT",
        })

        # Cập nhật last_message của phòng
        mongo_db["rooms"].update_one(
            {"_id": room_id},
            {"$set": {
                "last_message_content": f"{display_name} đã rời khỏi nhóm.",
                "last_message_sender_id": "system",
                "last_message_timestamp": now_ms,
            }}
        )

        # Broadcast sự kiện WebSocket
        asyncio.create_task(broadcast_ws_event({
            "type": "ROOM_UPDATED",
            "room_id": room_id,
            "timestamp": now_ms,
        }))

        return {"ok": True, "room_id": room_id, "message": "Bạn đã rời khỏi nhóm thành công."}

    return {"ok": True, "room_id": room_id, "message": "Bạn đã rời nhóm."}


@app.post("/api/v1/rooms/{room_id}/members")
async def add_room_members(
    room_id: str,
    payload: AddRoomMembersBody,
    user_id: str = Depends(get_current_user),
):
    """Thêm thành viên vào phòng chat nhóm."""
    if mongo_db is not None:
        mongo_db["rooms"].update_one(
            {"_id": room_id},
            {"$addToSet": {"member_ids": {"$each": payload.member_ids}}}
        )
        return {"ok": True, "room_id": room_id, "added_count": len(payload.member_ids)}
    return {"ok": True, "room_id": room_id}


@app.patch("/api/v1/rooms/{room_id}/name")
async def update_room_name(
    room_id: str,
    payload: UpdateRoomNameBody,
    user_id: str = Depends(get_current_user),
):
    """Cập nhật tên nhóm chat."""
    clean_name = payload.name.strip()
    if mongo_db is not None:
        mongo_db["rooms"].update_one(
            {"_id": room_id},
            {"$set": {"name": clean_name}}
        )
        return {"ok": True, "room_id": room_id, "new_name": clean_name}
    return {"ok": True, "room_id": room_id, "new_name": clean_name}
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
                continue
            try:
                msg_data = json.loads(text)
                if isinstance(msg_data, dict) and msg_data.get("type", "").startswith("CALL_"):
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