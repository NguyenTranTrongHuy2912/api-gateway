# FastAPI gRPC Gateway & BFF

A lightweight Backend-For-Frontend (BFF) API Gateway built with **FastAPI** (Python 3.13) to bridge RESTful HTTP clients and the internal **Java gRPC Core Backend**.

---

## 🛠️ Tech Stack & Architecture

- **Framework:** FastAPI (Asynchronous Python)
- **ASGI Server:** Uvicorn
- **gRPC Client:** `grpcio` & `grpcio-tools` (one shared async channel, created at startup)
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
- Running instance of the **Java gRPC Server** (default `localhost:50051`)

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
   Copy the latest `chat.proto` from the Java backend into `proto/`, then run:
   ```bash
   python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/chat.proto
   ```

5. **Start the FastAPI Gateway:**
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   `--host 0.0.0.0` is required for a physical phone on the same network to reach the gateway. `python main.py` only listens on `127.0.0.1` (this machine) and is meant for local development.

---

## ⚙️ Configuration

Set these environment variables to change behavior without editing code:

| Variable | Default | Description |
|----------|---------|-------------|
| `JAVA_GRPC_TARGET` | `localhost:50051` | Address of the Java gRPC backend (`host:port`) |
| `GRPC_TIMEOUT` | `5` | Max seconds to wait for each gRPC call before returning `504` |

Example (macOS / Linux):
```bash
JAVA_GRPC_TARGET=192.168.1.10:50051 GRPC_TIMEOUT=3 uvicorn main:app --host 0.0.0.0 --port 8000
```

Example (Windows PowerShell):
```powershell
$env:JAVA_GRPC_TARGET="192.168.1.10:50051"; $env:GRPC_TIMEOUT="3"; uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 🧪 Interactive API Documentation

Once the server is running, visit:

- **Swagger UI:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

### Endpoints

#### `POST /api/v1/chat/send`

- **Request Body:**
  ```json
  {
    "room_id": "room_101",
    "sender_id": "user_fit_iuh",
    "content": "Hello from FastAPI Gateway!",
    "message_id": "optional-client-generated-id"
  }
  ```
  - `room_id`, `sender_id`: 1-100 characters.
  - `content`: 1-4000 characters.
  - `message_id` (optional): if omitted, the gateway generates a UUID. Resending the same `message_id` is safe (idempotent), which makes client-side retry possible.

- **Response (`200 OK`):**
  ```json
  {
    "message_id": "550e8400-e29b-41d4-a716-446655440000",
    "is_success": true,
    "status_message": "Message saved",
    "timestamp": 1700000000000
  }
  ```

#### `GET /api/v1/chat/history/{room_id}`

- **Query parameters:**
  - `limit` (default `50`, 1-100)
  - `before_timestamp` (default `0`): return only messages older than this timestamp, for pagination.

- **Response (`200 OK`):** messages are ordered newest first.
  ```json
  {
    "messages": [
      {
        "message_id": "550e8400-e29b-41d4-a716-446655440000",
        "room_id": "room_101",
        "sender_id": "user_fit_iuh",
        "content": "Hello from FastAPI Gateway!",
        "timestamp": 1700000000000
      }
    ],
    "has_more": false
  }
  ```

### Error Mapping

| HTTP | Cause |
|------|-------|
| `400` | Backend rejected the data (gRPC `INVALID_ARGUMENT`) |
| `422` | Request failed gateway validation (empty or too long fields); no gRPC call is made |
| `503` | Java backend is down or unreachable (gRPC `UNAVAILABLE`) |
| `504` | Backend did not answer within `GRPC_TIMEOUT` (gRPC `DEADLINE_EXCEEDED`) |
| `500` | Any other backend error (details are logged, not returned) |