# FastAPI gRPC Gateway & BFF

A lightweight Backend-For-Frontend (BFF) API Gateway built with **FastAPI** (Python 3.13) to bridge RESTful HTTP/WebSocket clients and the internal **Java gRPC Core Backend**.

---

## 🛠️ Tech Stack & Architecture

- **Framework:** FastAPI (Asynchronous Python)
- **ASGI Server:** Uvicorn
- **gRPC Client:** `grpcio` & `grpcio-tools`
- **Validation:** Pydantic v2
- **Documentation:** Interactive OpenAPI (Swagger UI)

---

## 📁 Project Structure

```text
api-gateway/
├── proto/
│   └── chat.proto        # Shared gRPC protobuf contract
├── chat_pb2.py           # Auto-generated Protobuf code
├── chat_pb2_grpc.py      # Auto-generated gRPC Stub code
├── main.py               # FastAPI application entry point
├── requirements.txt      # Python dependencies list
└── .gitignore            # Git ignore rules
```

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+** (Python 3.13 recommended)
- Running instance of **Java gRPC Server** on `localhost:50051`

### Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/NguyenTranTrongHuy2912/api-gateway.git
   cd api-gateway
   ```

2. **Set up Python Virtual Environment:**
   ```bash
   python -m venv venv
   ```
   - **Windows (PowerShell):** `.\venv\Scripts\Activate.ps1`
   - **macOS / Linux:** `source venv/bin/activate`

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Recompile Protobuf (Optional):**
   ```bash
   python -m grpc.tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/chat.proto
   ```

5. **Start the FastAPI Gateway:**
   ```bash
   python main.py
   ```

---

## 🧪 Interactive API Documentation

Once the server is running, visit:
- **Swagger UI:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

### Endpoint Example

**`POST /api/v1/chat/send`**
- **Request Body:**
  ```json
  {
    "room_id": "room_101",
    "sender_id": "user_fit_iuh",
    "content": "Hello from FastAPI Gateway!"
  }
  ```
- **Response (`200 OK`):**
  ```json
  {
    "message_id": "550e8400-e29b-41d4-a716-446655440000",
    "is_success": true,
    "status_message": "Java Backend & MongoDB Atlas đã lưu tin nhắn!",
    "timestamp": 1700000000000
  }
  ```