import os
import time
from typing import Optional

import jwt
from fastapi import Header, HTTPException
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(override=False)
JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_ALGO = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "3"))

if not JWT_SECRET:
    raise RuntimeError("Biến môi trường JWT_SECRET chưa được cấu hình. Hệ thống từ chối khởi động để bảo vệ an toàn.")

# Bộ nhớ lưu mốc thời gian thu hồi token khi đổi mật khẩu: user_id -> timestamp (giây)
REVOCATION_TIMESTAMPS: dict[str, int] = {}

# Cache in-memory để tăng tốc đọc: user_id -> "ADMIN" | "USER"
USER_ROLES_STORE: dict[str, str] = {}

# Kết nối MongoDB Atlas để lưu trữ Role vĩnh viễn không bị mất khi restart server
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "chat_db")
_auth_mongo_db = None
if MONGO_URI:
    try:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
        _auth_mongo_db = _client[MONGO_DB_NAME]
    except Exception:
        pass


def set_user_role(user_id: str, role: str) -> None:
    """Cập nhật quyền của người dùng trong hệ thống và đồng bộ vĩnh viễn vào MongoDB Atlas."""
    norm_role = "ADMIN" if role.upper() == "ADMIN" else "USER"
    USER_ROLES_STORE[user_id] = norm_role

    # Lưu vĩnh viễn vào MongoDB Atlas
    if _auth_mongo_db is not None:
        try:
            _auth_mongo_db["users"].update_one(
                {"_id": user_id},
                {"$set": {"role": norm_role}}
            )
        except Exception:
            pass


def get_user_role(user_id: str) -> str:
    """Role do Java quyết định lúc đăng ký (ADMIN_EMAILS) và lưu trong users.role.
    Thứ tự: cache RAM -> MongoDB -> mặc định USER (mặc định không được cache)."""
    if user_id in USER_ROLES_STORE:
        return USER_ROLES_STORE[user_id]

    if _auth_mongo_db is not None and user_id:
        try:
            doc = _auth_mongo_db["users"].find_one({"_id": user_id}, {"role": 1})
            if doc and doc.get("role") in ("ADMIN", "USER"):
                USER_ROLES_STORE[user_id] = doc["role"]
                return doc["role"]
        except Exception:
            pass

    return "USER"


def revoke_user_tokens(user_id: str, timestamp: int | None = None) -> None:
    """Đánh dấu thu hồi mọi token được cấp trước thời điểm này đối với user."""
    REVOCATION_TIMESTAMPS[user_id] = timestamp or int(time.time())


def create_token(user_id: str, role: str = "USER") -> str:
    """Tạo JWT Token có đính kèm Role (ADMIN / USER) phục vụ phân quyền RBAC."""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "role": role,
        "iat": now,
        "exp": now + JWT_EXPIRE_DAYS * 86400,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def get_current_user(authorization: str = Header(default="")) -> str:
    """FastAPI dependency: returns the user_id stored in the Bearer token."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
        token_iat = payload.get("iat", 0)

        # Kiểm tra token có bị vô hiệu hóa do đổi mật khẩu không (bộ nhớ tạm RAM)
        revoked_at = REVOCATION_TIMESTAMPS.get(user_id, 0)
        if revoked_at > 0 and token_iat < revoked_at:
            raise HTTPException(
                status_code=401,
                detail="Phiên đăng nhập đã hết hạn do mật khẩu đã thay đổi. Vui lòng đăng nhập lại.",
            )

        # Kiểm tra trạng thái tài khoản và mốc đổi mật khẩu lưu trữ trong Database (P1-1 & P1-2)
        if _auth_mongo_db is not None:
            user_doc = _auth_mongo_db["users"].find_one(
                {"_id": user_id},
                {"is_active": 1, "password_changed_at": 1}
            )
            if user_doc:
                if user_doc.get("is_active") is False:
                    raise HTTPException(status_code=403, detail="Tài khoản của bạn đã bị khóa bởi Quản trị viên.")
                pwd_changed = user_doc.get("password_changed_at")
                if pwd_changed:
                    changed_sec = int(pwd_changed / 1000 if pwd_changed > 1e11 else pwd_changed)
                    if token_iat < changed_sec:
                        raise HTTPException(
                            status_code=401,
                            detail="Phiên đăng nhập đã hết hạn do mật khẩu đã thay đổi. Vui lòng đăng nhập lại.",
                        )

        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_admin(authorization: str = Header(default="")) -> str:
    """FastAPI dependency: Chỉ cho phép người dùng có Role = ADMIN thực hiện."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
        role = payload.get("role", "USER")
        token_iat = payload.get("iat", 0)

        revoked_at = REVOCATION_TIMESTAMPS.get(user_id, 0)
        if revoked_at > 0 and token_iat < revoked_at:
            raise HTTPException(
                status_code=401,
                detail="Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.",
            )

        # Kiểm tra quyền thực tế trong Database/RAM và trạng thái tài khoản (P1-1 & P1-2)
        real_role = get_user_role(user_id)
        if role != "ADMIN" or real_role != "ADMIN":
            raise HTTPException(
                status_code=403,
                detail="Bạn không có quyền Quản trị viên (ADMIN) để thực hiện thao tác này.",
            )

        if _auth_mongo_db is not None:
            user_doc = _auth_mongo_db["users"].find_one(
                {"_id": user_id},
                {"is_active": 1, "password_changed_at": 1, "role": 1}
            )
            if user_doc:
                if user_doc.get("is_active") is False:
                    raise HTTPException(status_code=403, detail="Tài khoản Quản trị viên đã bị khóa.")
                if user_doc.get("role") != "ADMIN":
                    raise HTTPException(status_code=403, detail="Tài khoản đã bị thu hồi quyền Quản trị viên.")
                pwd_changed = user_doc.get("password_changed_at")
                if pwd_changed:
                    changed_sec = int(pwd_changed / 1000 if pwd_changed > 1e11 else pwd_changed)
                    if token_iat < changed_sec:
                        raise HTTPException(
                            status_code=401,
                            detail="Phiên đăng nhập đã hết hạn do mật khẩu đã thay đổi. Vui lòng đăng nhập lại.",
                        )

        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def decode_token(token: str) -> Optional[str]:
    """Giải mã token không ném exception, trả về user_id (str) hoặc None nếu lỗi / bị thu hồi / bị khóa."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
        if not user_id:
            return None
        token_iat = payload.get("iat", 0)

        # Kiểm tra token có bị thu hồi do đổi mật khẩu (RAM)
        revoked_at = REVOCATION_TIMESTAMPS.get(user_id, 0)
        if revoked_at > 0 and token_iat < revoked_at:
            return None

        # Kiểm tra tài khoản có bị khóa hoặc đổi mật khẩu (Database)
        if _auth_mongo_db is not None:
            user_doc = _auth_mongo_db["users"].find_one(
                {"_id": user_id},
                {"is_active": 1, "password_changed_at": 1}
            )
            if user_doc:
                if user_doc.get("is_active") is False:
                    return None
                pwd_changed = user_doc.get("password_changed_at")
                if pwd_changed:
                    changed_sec = int(pwd_changed / 1000 if pwd_changed > 1e11 else pwd_changed)
                    if token_iat < changed_sec:
                        return None

        return str(user_id)
    except jwt.PyJWTError:
        return None
