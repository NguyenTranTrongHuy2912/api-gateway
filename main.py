from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import grpc
import time
import uuid

import chat_pb2
import chat_pb2_grpc

app = FastAPI(
    title="FastAPI gRPC Gateway",
    description="API Gateway làm cầu nối giữa Mobile Client và Java gRPC Backend"
)

# Địa chỉ và Port của Java gRPC Server
JAVA_GRPC_HOST = "localhost"
JAVA_GRPC_PORT = 50051

# Schema nhận dữ liệu JSON từ Client (React Native / Postman)
class SendMessageRequest(BaseModel):
    room_id: str
    sender_id: str
    content: str

@app.get("/")
async def root():
    return {
        "system": "FastAPI gRPC Gateway",
        "status": "Running",
        "docs_url": "http://127.0.0.1:8000/docs"
    }

@app.post("/api/v1/chat/send")
async def send_message(payload: SendMessageRequest):
    # 1. Khởi tạo kênh kết nối gRPC bất đồng bộ tới Java Server
    async with grpc.aio.insecure_channel(f"{JAVA_GRPC_HOST}:{JAVA_GRPC_PORT}") as channel:
        stub = chat_pb2_grpc.ChatServiceStub(channel)
        
        # 2. Đóng gói dữ liệu JSON thành Protobuf Request
        grpc_request = chat_pb2.ChatMessage(
            message_id=str(uuid.uuid4()),
            room_id=payload.room_id,
            sender_id=payload.sender_id,
            content=payload.content,
            timestamp=int(time.time() * 1000)
        )
        
        try:
            # 3. Gọi hàm SendMessage sang Java Backend qua gRPC
            grpc_response = await stub.SendMessage(grpc_request)
            
            # 4. Trả về kết quả JSON cho Client
            return {
                "message_id": grpc_response.message_id,
                "is_success": grpc_response.is_success,
                "status_message": grpc_response.status_message,
                "timestamp": grpc_response.timestamp
            }
        except grpc.RpcError as e:
            raise HTTPException(
                status_code=500, 
                detail=f"Không thể kết nối tới Java gRPC Backend: {e.details()}"
            )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)