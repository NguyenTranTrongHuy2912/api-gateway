import os
import time
from typing import Optional

import jwt
from fastapi import Header, HTTPException
from dotenv import load_dotenv

# Set before starting the gateway, e.g. PowerShell: $env:JWT_SECRET = "<long random string>"
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
}

# In-memory role store: user_id -> "ADMIN" | "USER"
USER_ROLES_STORE: dict[str, str] = {}


def set_user_role(user_id: str, role: str) -> None:
    """Cập nhật quyền của người dùng trong hệ thống (ADMIN hoặc USER)."""
    USER_ROLES_STORE[user_id] = "ADMIN" if role.upper() == "ADMIN" else "USER"


def get_user_role(user_id: str, account_email: str = "") -> str:
    """Lấy role của người dùng, tự động cấp quyền ADMIN nếu nằm trong danh sách admin cấu hình."""
    if user_id in USER_ROLES_STORE:
        return USER_ROLES_STORE[user_id]
    if account_email and account_email.lower().strip() in DEFAULT_ADMIN_EMAILS:
        USER_ROLES_STORE[user_id] = "ADMIN"
        return "ADMIN"
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
            raise HTTPException(status_code=401, detail="Phiên đăng nhập đã hết hạn.")

        # Kiểm tra quyền Admin từ token hoặc store
        actual_role = USER_ROLES_STORE.get(user_id, role)
        if actual_role != "ADMIN":
            raise HTTPException(
                status_code=403,
                detail="Quyền truy cập bị từ chối: Yêu cầu quyền Quản trị viên (Admin).",
            )
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def decode_token(token: str):
    """Decodes a JWT and returns user_id, or None if invalid/expired/revoked."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = payload.get("sub")
        token_iat = payload.get("iat", 0)

        revoked_at = REVOCATION_TIMESTAMPS.get(user_id, 0)
        if revoked_at > 0 and token_iat < revoked_at:
            return None

        return user_id
    except Exception:
        return None
