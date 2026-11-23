import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import grpc

import chat_pb2
from auth import create_token, get_current_user, get_user_role, revoke_user_tokens

log = logging.getLogger("gateway.auth")
router = APIRouter(tags=["Auth"])

# Error mapping and timeout from main gateway
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

def to_http_error(e: grpc.RpcError, expose_details: bool = True) -> HTTPException:
    code = e.code()
    status = HTTP_BY_GRPC.get(code, 500)
    detail = e.details() or DETAIL_BY_STATUS.get(status, "Error") if expose_details else "Error"
    return HTTPException(status_code=status, detail=detail)


class RequestOtpBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: str = Field(min_length=3, max_length=254)


class ResetPasswordBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: str = Field(min_length=3, max_length=254)
    otp_code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=6, max_length=64)


class RegisterBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=64)
    display_name: str = Field(min_length=1, max_length=50)
    otp_code: str = Field(min_length=6, max_length=6)


class LoginBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=64)


class UpdateProfileBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    display_name: Optional[str] = Field(default=None, max_length=50)
    avatar_url: Optional[str] = Field(default=None, max_length=500)


class ChangePasswordLoggedInBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    current_password: str = Field(min_length=1, max_length=64)
    new_password: str = Field(min_length=6, max_length=64)


@router.post("/api/v1/auth/request-otp")
async def request_otp_endpoint(request: Request, payload: RequestOtpBody):
    account_clean = payload.account.strip().lower()
    if not account_clean or "@" not in account_clean:
        raise HTTPException(status_code=400, detail="Địa chỉ email không hợp lệ.")
    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(account=account_clean), timeout=15.0
        )
        return {
            "expires_in_seconds": resp.expires_in_seconds,
            "resend_after_seconds": resp.resend_after_seconds,
            "message": "Đã gửi mã xác thực OTP về email của bạn.",
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@router.post("/api/v1/auth/forgot-password")
async def forgot_password_endpoint(request: Request, payload: RequestOtpBody):
    try:
        resp = await request.app.state.auth_stub.RequestOtp(
            chat_pb2.RequestOtpRequest(
                account=payload.account,
                purpose=chat_pb2.OTP_PURPOSE_RESET_PASSWORD,
            ),
            timeout=15.0,
        )
        return {
            "expires_in_seconds": resp.expires_in_seconds,
            "resend_after_seconds": resp.resend_after_seconds,
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@router.post("/api/v1/auth/reset-password")
async def reset_password_endpoint(request: Request, payload: ResetPasswordBody):
    try:
        await request.app.state.auth_stub.ResetPassword(
            chat_pb2.ResetPasswordRequest(
                account=payload.account,
                otp_code=payload.otp_code,
                new_password=payload.new_password,
            ),
            timeout=15.0,
        )
        return {"ok": True}
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@router.post("/api/v1/auth/register")
async def register_endpoint(request: Request, payload: RegisterBody):
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
            timeout=15.0,
        )
        role = getattr(resp, "role", "") or get_user_role(resp.user_id)
        return {
            "token": create_token(resp.user_id, role=role),
            "user_id": resp.user_id,
            "display_name": resp.display_name,
            "role": role,
            "avatar_url": getattr(resp, "avatar_url", "") or "",
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@router.post("/api/v1/auth/login")
async def login_endpoint(request: Request, payload: LoginBody):
    try:
        resp = await request.app.state.auth_stub.Login(
            chat_pb2.LoginRequest(account=payload.account, password=payload.password),
            timeout=15.0,
        )
        role = getattr(resp, "role", "") or get_user_role(resp.user_id)
        return {
            "token": create_token(resp.user_id, role=role),
            "user_id": resp.user_id,
            "display_name": resp.display_name,
            "role": role,
            "avatar_url": getattr(resp, "avatar_url", "") or "",
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@router.get("/api/v1/users/me")
async def get_my_profile_endpoint(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    role = get_user_role(user_id)
    return {
        "user_id": user_id,
        "role": role,
        "is_admin": role == "ADMIN",
    }


@router.put("/api/v1/users/profile")
async def update_profile_endpoint(
    payload: UpdateProfileBody,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    try:
        req = chat_pb2.UpdateProfileRequest(user_id=user_id)
        if payload.display_name is not None:
            req.display_name = payload.display_name.strip()
        if payload.avatar_url is not None:
            req.avatar_url = payload.avatar_url.strip()
        resp = await request.app.state.auth_stub.UpdateProfile(req, timeout=15.0)
        return {
            "ok": resp.is_success,
            "display_name": resp.display_name,
            "avatar_url": resp.avatar_url,
        }
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)


@router.post("/api/v1/users/change-password")
async def change_password_endpoint(
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
            timeout=15.0,
        )
        revoke_user_tokens(user_id)
        return {"ok": resp.is_success, "message": resp.status_message}
    except grpc.RpcError as e:
        raise to_http_error(e, expose_details=True)
