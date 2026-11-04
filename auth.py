import os
import time

import jwt
from fastapi import Header, HTTPException
from dotenv import load_dotenv

# Set before starting the gateway, e.g. PowerShell: $env:JWT_SECRET = "<long random string>"
load_dotenv()
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "3"))

# Bộ nhớ lưu mốc thời gian thu hồi token khi đổi mật khẩu: user_id -> timestamp (giây)
REVOCATION_TIMESTAMPS: dict[str, int] = {}


def revoke_user_tokens(user_id: str, timestamp: int | None = None) -> None:
    """Đánh dấu thu hồi mọi token được cấp trước thời điểm này đối với user."""
    REVOCATION_TIMESTAMPS[user_id] = timestamp or int(time.time())


def create_token(user_id: str) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + JWT_EXPIRE_DAYS * 86400}
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
