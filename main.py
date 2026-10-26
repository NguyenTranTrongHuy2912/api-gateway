import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import grpc
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

import chat_pb2
import chat_pb2_grpc

log = logging.getLogger("gateway")

# Backend address and per-call timeout, configurable without touching code
JAVA_GRPC_TARGET = os.getenv("JAVA_GRPC_TARGET", "localhost:50051")
GRPC_TIMEOUT = float(os.getenv("GRPC_TIMEOUT", "5"))  # seconds

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


def to_http_error(e: grpc.RpcError) -> HTTPException:
    code = e.code()
    status = HTTP_BY_GRPC.get(code, 500)
    log.warning("gRPC call failed: %s - %s", code, e.details())  # full detail only in the log

    if status == 400:
        detail = e.details() or "Invalid request"  # backend validation message is safe to show
    elif status == 503:
        detail = "Chat backend is unavailable"
    elif status == 504:
        detail = "Chat backend timed out"
    else:
        detail = "Internal gateway error"
    return HTTPException(status_code=status, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared channel for the whole app: no new connection handshake per request
    channel = grpc.aio.insecure_channel(JAVA_GRPC_TARGET)
    app.state.stub = chat_pb2_grpc.ChatServiceStub(channel)
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
    sender_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    # Optional: client-generated id. If omitted, the gateway generates a uuid.
    message_id: Optional[str] = Field(default=None, min_length=1, max_length=100)


@app.get("/")
async def root():
    return {
        "system": "FastAPI gRPC Gateway",
        "status": "Running",
        "docs_url": "/docs",
    }


@app.post("/api/v1/chat/send")
async def send_message(payload: SendMessageRequest, request: Request):
    grpc_request = chat_pb2.ChatMessage(
        message_id=payload.message_id or str(uuid.uuid4()),
        room_id=payload.room_id,
        sender_id=payload.sender_id,
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