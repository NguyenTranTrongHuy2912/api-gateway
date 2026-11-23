import logging
import re
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
import grpc

import chat_pb2
from auth import get_current_admin, set_user_role, revoke_user_tokens

log = logging.getLogger("gateway.admin")
router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])

GRPC_TIMEOUT = 5.0

class SetUserRoleBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    role: str = Field(min_length=4, max_length=10)


class SetUserStatusBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    is_active: bool


class AdminRequestOtpBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: str = Field(min_length=3, max_length=254)


class CreateUserAdminBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=64)
    display_name: str = Field(min_length=1, max_length=50)
    role: str = Field(default="USER", max_length=10)
    otp_code: str = Field(min_length=6, max_length=6)


@router.get("/stats")
async def get_admin_stats(
    request: Request,
    admin_id: str = Depends(get_current_admin),
):
    total_users = 0
    total_admins = 0
    total_active_users = 0
    total_rooms = 0
    total_messages = 0

    # 1. Ủy quyền trực tiếp qua Java gRPC Backend để lấy thống kê tập trung
    try:
        resp = await request.app.state.auth_stub.GetSystemStats(
            chat_pb2.GetSystemStatsRequest(), timeout=GRPC_TIMEOUT
        )
        total_users = resp.total_users
        total_admins = resp.total_admins
        total_active_users = resp.total_active_users
        total_rooms = resp.total_rooms
        total_messages = resp.total_messages
    except Exception as grpc_err:
        log.warning("gRPC GetSystemStats failed, fallback to Mongo: %s", grpc_err)
        mongo_db = getattr(request.app.state, "mongo_db", None)
        if mongo_db is not None:
            try:
                total_users = mongo_db["users"].count_documents({})
                total_admins = mongo_db["users"].count_documents({"role": "ADMIN"})
                total_active_users = mongo_db["users"].count_documents({"is_active": {"$ne": False}})
                total_rooms = mongo_db["rooms"].count_documents({})
                total_messages = mongo_db["messages"].count_documents({})
            except Exception as e:
                log.error("Mongo stats fallback error: %s", e)

    return {
        "total_users": total_users,
        "total_admins": total_admins,
        "total_active_users": total_active_users,
        "total_rooms": total_rooms,
        "total_messages": total_messages,
    }


@router.get("/users")
async def get_admin_users(
    request: Request,
    admin_id: str = Depends(get_current_admin),
    q: Optional[str] = Query(None),
):
    mongo_db = getattr(request.app.state, "mongo_db", None)
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

    try:
        cursor = mongo_db["users"].find(query).sort("created_at", -1).limit(100)
        users = []
        for doc in cursor:
            users.append({
                "user_id": doc["_id"],
                "account": doc.get("account", ""),
                "display_name": doc.get("display_name", ""),
                "role": doc.get("role", "USER"),
                "is_active": doc.get("is_active", True),
                "avatar_url": doc.get("avatar_url", ""),
                "created_at": doc.get("created_at", 0),
            })
        return {"users": users}
    except Exception as e:
        log.error("Failed to query users from mongo: %s", e)
        raise HTTPException(status_code=500, detail="Không thể truy vấn danh sách người dùng.")


@router.patch("/users/{target_user_id}/role")
async def update_user_role_admin(
    target_user_id: str,
    body: SetUserRoleBody,
    admin_id: str = Depends(get_current_admin),
):
    if body.role != "ADMIN":
        raise HTTPException(
            status_code=400,
            detail="Vì lý do an toàn và bảo mật hệ thống, tài khoản Quản trị viên (ADMIN) không thể bị hạ cấp xuống USER."
        )

    set_user_role(target_user_id, "ADMIN")
    return {
        "ok": True,
        "user_id": target_user_id,
        "role": "ADMIN",
        "message": "Nâng cấp quyền Quản trị viên thành công.",
    }


@router.patch("/users/{target_user_id}/status")
async def update_user_status_admin(
    target_user_id: str,
    body: SetUserStatusBody,
    request: Request,
    admin_id: str = Depends(get_current_admin),
):
    if target_user_id == admin_id and not body.is_active:
        raise HTTPException(status_code=400, detail="Không thể tự khóa tài khoản của chính mình.")

    # 1. Ủy quyền qua Java gRPC Backend
    try:
        await request.app.state.auth_stub.SetUserActive(
            chat_pb2.SetUserActiveRequest(user_id=target_user_id, is_active=body.is_active),
            timeout=GRPC_TIMEOUT,
        )
    except Exception as grpc_err:
        log.warning("gRPC SetUserActive failed, fallback to Mongo: %s", grpc_err)
        mongo_db = getattr(request.app.state, "mongo_db", None)
        if mongo_db is not None:
            mongo_db["users"].update_one(
                {"_id": target_user_id},
                {"$set": {"is_active": body.is_active}},
            )

    if not body.is_active:
        revoke_user_tokens(target_user_id)

    return {
        "ok": True,
        "user_id": target_user_id,
        "is_active": body.is_active,
        "message": "Cập nhật trạng thái người dùng thành công.",
    }


@router.post("/request-otp")
async def admin_request_otp(
    payload: AdminRequestOtpBody,
    request: Request,
    admin_id: str = Depends(get_current_admin),
):
    account_clean = payload.account.strip().lower()
    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Email tài khoản không hợp lệ.")

    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(account=account_clean, purpose=chat_pb2.OTP_REGISTER),
            timeout=GRPC_TIMEOUT,
        )
        return {
            "expires_in_seconds": resp.expires_in_seconds,
            "resend_after_seconds": resp.resend_after_seconds,
        }
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details() or "Không gửi được OTP")


@router.post("/users")
async def create_user_admin(
    payload: CreateUserAdminBody,
    request: Request,
    admin_id: str = Depends(get_current_admin),
):
    account_clean = payload.account.lower().strip()
    otp_clean = payload.otp_code.strip()

    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Email tài khoản không hợp lệ.")
    if not payload.password or len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 6 ký tự.")
    if not otp_clean:
        raise HTTPException(status_code=400, detail="Vui lòng nhập mã xác thực OTP đã được gửi về email.")

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
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details() or "Tạo người dùng thất bại")

    user_id = resp.user_id
    role = payload.role.upper() if payload.role else "USER"
    if role in ("ADMIN", "USER"):
        set_user_role(user_id, role)

    return {
        "ok": True,
        "user_id": user_id,
        "account": account_clean,
        "display_name": payload.display_name.strip(),
        "role": role,
    }


@router.delete("/users/{target_user_id}")
async def delete_user_admin(
    target_user_id: str,
    request: Request,
    admin_id: str = Depends(get_current_admin),
):
    if target_user_id == admin_id:
        raise HTTPException(status_code=400, detail="Không thể xóa tài khoản của chính mình.")

    try:
        await request.app.state.auth_stub.SetUserActive(
            chat_pb2.SetUserActiveRequest(user_id=target_user_id, is_active=False),
            timeout=GRPC_TIMEOUT,
        )
    except Exception as e:
        log.warning("SetUserActive gRPC fallback: %s", e)
        mongo_db = getattr(request.app.state, "mongo_db", None)
        if mongo_db is not None:
            mongo_db["users"].update_one({"_id": target_user_id}, {"$set": {"is_active": False}})

    revoke_user_tokens(target_user_id)
    return {"ok": True, "message": "Đã vô hiệu hóa tài khoản thành công."}
