import os
import time
from typing import Optional

import jwt
from fastapi import Header, HTTPException
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
JWT_SECRET = os.environ.get("JWT_SECRET", "3277e7cd0687955793ee1c5b4980cb16c3d7284d9c5975411e541f89f8169199")
JWT_ALGO = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "3"))

# Bộ nhớ lưu mốc thời gian thu hồi token khi đổi mật khẩu: user_id -> timestamp (giây)
REVOCATION_TIMESTAMPS: dict[str, int] = {}

# Danh sách email mặc định có quyền Quản trị viên (System Admin)
DEFAULT_ADMIN_EMAILS = {
    "admin@chatapp.com",
    "admin@gmail.com",
    "quoc05@gmail.com",
    "admin@iuh.edu.vn",
    "nguyenvanquoc20055@gmail.com",
}

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


def get_user_role(user_id: str, account_email: str = "") -> str:
    """
    Lấy role của người dùng.
    Ưu tiên: Cache RAM -> Database MongoDB Atlas -> DEFAULT_ADMIN_EMAILS -> Default USER.
    """
    # 1. Kiểm tra cache RAM
    if user_id in USER_ROLES_STORE:
        return USER_ROLES_STORE[user_id]

    clean_email = account_email.lower().strip() if account_email else ""

    # 2. Kiểm tra trực tiếp từ MongoDB Atlas
    if _auth_mongo_db is not None and user_id:
        try:
            user_doc = _auth_mongo_db["users"].find_one({"_id": user_id}, {"role": 1, "account": 1})
            if user_doc:
                db_role = user_doc.get("role")
                db_account = (user_doc.get("account") or "").lower().strip()
                if not clean_email and db_account:
                    clean_email = db_account

                if db_role in ("ADMIN", "USER"):
                    USER_ROLES_STORE[user_id] = db_role
                    return db_role
        except Exception:
            pass

    # 3. Kiểm tra danh sách Admin mặc định
    if clean_email and clean_email in DEFAULT_ADMIN_EMAILS:
        USER_ROLES_STORE[user_id] = "ADMIN"
        return "ADMIN"

    # Mặc định là USER thông thường
    USER_ROLES_STORE[user_id] = "USER"
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

        # Kiểm tra token có bị vô hiệu hóa do đổi mật khẩu không
        revoked_at = REVOCATION_TIMESTAMPS.get(user_id, 0)
        if revoked_at > 0 and token_iat < revoked_at:
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

        # Kiểm tra thêm quyền thực tế trong Database/RAM
        real_role = get_user_role(user_id)
        if role != "ADMIN" and real_role != "ADMIN":
            raise HTTPException(
                status_code=403,
                detail="Bạn không có quyền Quản trị viên (ADMIN) để thực hiện thao tác này.",
            )

        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def decode_token(token: str) -> Optional[dict]:
    """Giải mã token không ném exception, trả về None nếu lỗi hoặc token bị thu hồi."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
        token_iat = payload.get("iat", 0)

        revoked_at = REVOCATION_TIMESTAMPS.get(user_id, 0)
        if revoked_at > 0 and token_iat < revoked_at:
            return None

        return payload
    except jwt.PyJWTError:
        return None
