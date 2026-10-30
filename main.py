import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import grpc
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

import chat_pb2
import chat_pb2_grpc
from auth import create_token, get_current_user

log = logging.getLogger("gateway")

# Backend address and per-call timeout, configurable without touching code
JAVA_GRPC_TARGET = os.getenv("JAVA_GRPC_TARGET", "localhost:50051")
GRPC_TIMEOUT = float(os.getenv("GRPC_TIMEOUT", "15.0"))  # seconds

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


def to_http_error(e: grpc.RpcError, expose_details: bool = False) -> HTTPException:
    code = e.code()
    status = HTTP_BY_GRPC.get(code, 500)
    log.warning("gRPC call failed: %s - %s", code, e.details())

    if status == 400:
        detail = e.details() or "Invalid request"
    elif expose_details and status in (401, 409, 429):
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
    content: str = Field(min_length=1, max_length=4000)
    # Optional: client-generated id. If omitted, the gateway generates a uuid.
    message_id: Optional[str] = Field(default=None, min_length=1, max_length=100)


class RequestOtpBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    account: str = Field(min_length=1, max_length=254)  # email


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


@app.get("/")
async def root():
    return {
        "system": "FastAPI gRPC Gateway",
        "status": "Running",
        "docs_url": "/docs",
    }


@app.post("/api/v1/auth/request-otp")
async def request_otp(payload: RequestOtpBody, request: Request):
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
async def login(payload: LoginBody, request: Request):
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
    )
    try:
        resp = await request.app.state.stub.SendMessage(grpc_request, timeout=GRPC_TIMEOUT)
    except grpc.RpcError as e:
        raise to_http_error(e)

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
    )
    try:
        resp = await request.app.state.stub.GetMessageHistory(grpc_request, timeout=GRPC_TIMEOUT)
    except grpc.RpcError as e:
        raise to_http_error(e)

    return {
        "messages": [
            {
                "message_id": m.message_id,
                "room_id": m.room_id,
                "sender_id": m.sender_id,
                "content": m.content,
                "timestamp": m.timestamp,
            }
            for m in resp.messages
        ],
        "has_more": resp.has_more,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)