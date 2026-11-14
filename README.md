# api-gateway

A FastAPI gateway (backend-for-frontend) for the chat application. It sits between the React Native app and the Java gRPC backend: the app talks REST and WebSocket to this service, and this service calls the backend over gRPC. On top of the bridge it adds JWT authentication with roles, rate limiting, realtime events and call signaling over WebSocket, image and video upload signing for Cloudinary, push notifications, an admin API, and the HQ AI assistant (chat, summaries, rewriting, semantic search and RAG).

```text
React Native app  --REST / WebSocket-->  api-gateway  --gRPC-->  java-grpc-backend  -->  MongoDB
                                              |                                           ^
                                              +-------------------------------------------+
                                                 (direct reads and some writes, see below)
```

## Tech stack

- Python 3.12 (the Dockerfile uses `python:3.12-slim`)
- FastAPI 0.146 on Uvicorn 0.54, Pydantic 2.14
- `grpcio` 1.84 and `protobuf` 7.36, with one shared async channel opened at startup
- PyJWT 2.15 for tokens, `slowapi` 0.1.10 for rate limiting, `websockets` 17.4
- PyMongo 4.19 for the gateway's own database access
- `python-dotenv` for `.env`
- Google Gemini REST API (called with `urllib`, no SDK), Cloudinary (signing only), Expo Push API

## Project structure

```text
api-gateway/
├── proto/
│   └── chat.proto            # Copy of the backend's gRPC contract
├── chat_pb2.py               # Generated Protobuf code
├── chat_pb2_grpc.py          # Generated gRPC stubs (ChatService, AuthService)
├── main.py                   # FastAPI app: routes, WebSocket, calls, push, upload signing
├── auth.py                   # JWT creation and checks, roles, token revocation
├── email_service.py          # Gateway-side OTP generation, verification and SMTP mail
├── ai_service.py             # HQ AI: Gemini calls, fallback replies, summaries, rewriting
├── vector_db.py              # Small vector store for semantic search and RAG
├── security_ai.py            # Masks personal data before it reaches an AI prompt
├── start.sh                  # Starts the Java backend and the gateway in one container
├── Dockerfile                # Gateway-only image
├── requirements.txt
└── .env.example
```

`vector_store.json` is created in the working directory the first time a message is indexed.

## Getting started

You need Python 3.12 and a running Java gRPC backend (default `localhost:50051`). If you want to use MongoDB-backed features, the gateway also needs the same database the backend uses.

1. Clone the repository:
   ```bash
   git clone https://github.com/NguyenTranTrongHuy2912/api-gateway.git
   cd api-gateway
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate        # Windows PowerShell: .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Create `.env` from the template and fill it in (see Configuration, which lists variables the template does not have):
   ```bash
   cp .env.example .env
   ```

4. Regenerate the Protobuf code only when the backend's `chat.proto` changes. Copy the new file into `proto/` and run:
   ```bash
   python -m grpc_tools.protoc -I./proto --python_out=. --grpc_python_out=. proto/chat.proto
   ```

5. Start the gateway:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   `--host 0.0.0.0` lets a phone on the same network reach it. Interactive docs are at `http://127.0.0.1:8000/docs` (Swagger UI) and `/redoc`.

## Configuration

`main.py` calls `load_dotenv(override=True)`, so values in `.env` win over variables already set in the process environment. When you deploy with real environment variables, do not ship a `.env` file.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `JAVA_GRPC_TARGET` | No | `localhost:50051` | Backend address as `host:port`. A target ending in `:443` uses TLS |
| `GRPC_TIMEOUT` | No | `15.0` | Seconds to wait for each gRPC call before returning `504` |
| `JWT_SECRET` | Yes | none | Secret for signing tokens (HS256). Tokens cannot be created without it |
| `JWT_EXPIRE_DAYS` | No | `3` | Token lifetime in days |
| `MONGO_URI` | No | empty | MongoDB connection string. Without it the gateway runs, but admin endpoints return empty data and the direct-database features are skipped |
| `MONGO_DB_NAME` | No | `chat_db` | Database name, same as the backend |
| `OTP_SECRET` | Yes | none | Must equal the backend's `OTP_SECRET`. Read when `email_service.py` is imported, so the gateway will not start without it |
| `SMTP_HOST`, `SMTP_PORT` | Yes | none | Read at import time as well. `SMTP_PORT` must be a number, or startup fails |
| `SMTP_USER`, `SMTP_PASSWORD` | No | none | If either is empty the gateway skips sending its own OTP email. For Gmail use an App Password |
| `SMTP_FROM` | No | none | Sender address shown on OTP emails |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | For uploads | empty | Without all three, `POST /api/v1/uploads/sign` returns `503` |
| `CLOUDINARY_FOLDER` | No | `chatapp` | Folder that uploads must live in |
| `GEMINI_API_KEY` | No | empty | Enables Gemini replies and embeddings. `GOOGLE_API_KEY` is also accepted. Without a key the AI features use local fallbacks |
| `GEMINI_MODEL` | No | `gemini-2.5-flash` | Preferred model. If it fails, the code tries `gemini-2.5-flash`, `gemini-flash-latest` and `gemini-2.5-flash-lite` in turn |
| `PORT` | No | `8000` in Docker, `10000` in `start.sh` | Port the container listens on |

`.env.example` currently lists only the Mongo, gRPC, JWT, Cloudinary and Gemini variables. The `OTP_SECRET` and `SMTP_*` rows above are needed by the code but are missing from the template.

## Authentication

Every endpoint under `/api/v1` except the `auth` group needs the header `Authorization: Bearer <token>`. Tokens come from `login` or `register` and carry the user id (`sub`), the role (`ADMIN` or `USER`) and the issue time.

- Changing a password through `POST /api/v1/users/change-password` revokes all tokens issued before that moment for that user.
- Admin endpoints use the `get_current_admin` dependency and return `403` unless the token or the stored role is `ADMIN`.
- A role is looked up in this order: in-memory cache, the `users` collection, a built-in list of default admin emails, then `USER`.
- The WebSocket takes the token as a query parameter instead of a header.

## REST API

Rate limits are per client IP and return `429` with a Vietnamese message when exceeded.

### Auth (no token)

| Method and path | Limit | Description |
|---|---|---|
| `GET /` | | Status check |
| `POST /api/v1/auth/request-otp` | 5/min | Sends a registration OTP. Returns `409` if the email already has an account |
| `POST /api/v1/auth/forgot-password` | 5/min | Sends a password-reset OTP through `AuthService.RequestOtp` |
| `POST /api/v1/auth/reset-password` | 5/min | Sets a new password with `account`, `otp_code` and `new_password` |
| `POST /api/v1/auth/register` | | Creates an account. Returns `token`, `user_id`, `display_name`, `role` |
| `POST /api/v1/auth/login` | 10/min | Returns `token`, `user_id`, `display_name`, `role` |

### Users and profile

| Method and path | Description |
|---|---|
| `GET /api/v1/users/me` | Returns `user_id`, `role` and `is_admin` |
| `GET /api/v1/users/search?q=&limit=` | Searches users through the backend. `limit` is 1 to 20, default 10. Ordinary users never see admin accounts in the results |
| `GET /api/v1/users/suggestions?limit=` | Suggests people to add, with the same admin filtering |
| `GET /api/v1/users/online` | Users currently connected over WebSocket |
| `POST /api/v1/users/push-token` | Stores the caller's Expo push token |
| `PUT /api/v1/users/profile` | Updates `display_name` and `avatar_url` |
| `POST /api/v1/users/change-password` | Needs `current_password` and `new_password` (6 to 64 characters) |

### Rooms, messages and uploads

| Method and path | Description |
|---|---|
| `GET /api/v1/rooms` | Rooms of the caller, with last message and `unread_count` |
| `POST /api/v1/rooms` | Body: `type` (`DIRECT` or `GROUP`), `name` (groups, up to 50), `member_ids` (1 to 99 ids). The creator comes from the token |
| `POST /api/v1/rooms/{room_id}/leave` | Leave a group |
| `POST /api/v1/rooms/{room_id}/members` | Add `member_ids` to a group |
| `PATCH /api/v1/rooms/{room_id}/name` | Rename a group (1 to 50 characters) |
| `POST /api/v1/rooms/{room_id}/read` | Mark as read. `read_up_to_timestamp` of 0 means everything so far |
| `POST /api/v1/chat/send` | Send a message, described below |
| `GET /api/v1/chat/history/{room_id}` | Newest first. `limit` 1 to 100 (default 50), `before_timestamp` for older pages. Returns `messages` and `has_more` |
| `POST /api/v1/uploads/sign?resource_type=image\|video` | 20/min. Returns a short-lived Cloudinary signature |

`POST /api/v1/chat/send` takes:

- `room_id` (1 to 100 characters) and `type` (`TEXT`, `IMAGE` or `VIDEO`, default `TEXT`).
- `content`, up to 4000 characters, required for `TEXT`.
- `message_id`, optional. If missing the gateway generates a UUID. Resending the same id is safe.
- For `IMAGE` and `VIDEO`: `media_url` (up to 500 characters), `media_width` and `media_height` (0 to 20000), `media_duration_ms`.

The sender is always the user in the token. There is no `sender_id` field. A `media_url` is accepted only if it points into this project's Cloudinary account and folder, in the form `https://res.cloudinary.com/<cloud>/image|video/upload/[v123/]<folder>/<id>.<ext>`.

Upload flow: call `POST /api/v1/uploads/sign`, upload the file straight to the returned `upload_url` with the returned `api_key`, `timestamp`, `public_id`, `transformation` and `signature`, then send a message with the resulting URL. Images are signed with the incoming transformation `c_limit,w_1600,h_1600`.

### Friends

| Method and path | Description |
|---|---|
| `POST /api/v1/friends/request` | Body: `receiver_id` |
| `POST /api/v1/friends/respond` | Body: `request_id`, `accept` |
| `GET /api/v1/friends` | Friend list |
| `GET /api/v1/friends/requests` | Pending requests received |

### AI

| Method and path | Description |
|---|---|
| `POST /api/v1/ai/chat` | Body: `prompt` (up to 4000), optional `history` of `{role, content}` items. Questions about the user's own chats are answered from their rooms and messages |
| `POST /api/v1/ai/rewrite` | Body: `text` (up to 2000) and `tone`: `formal`, `friendly`, `short` or `english` |
| `POST /api/v1/ai/summarize` | Body: `room_id`, `limit` (default 30). Summarizes recent messages in three parts |
| `POST /api/v1/ai/semantic-search` | Body: `query` (up to 500), `room_id`, `top_k` (default 5) |
| `POST /api/v1/ai/rag-chat` | Body: `question` (up to 1000), `room_id`. Answers from the room's messages and returns the matches used |

Details of how these work are in the AI section below.

### Admin (token with role ADMIN)

| Method and path | Description |
|---|---|
| `GET /api/v1/admin/stats` | Counts of users, admins, active users, rooms, messages and indexed vectors |
| `GET /api/v1/admin/users?q=` | Up to 50 users, newest first, optional search on email or name |
| `PATCH /api/v1/admin/users/{id}/role` | Body: `role` is `ADMIN` or `USER` |
| `PATCH /api/v1/admin/users/{id}/status` | Body: `is_active` |
| `POST /api/v1/admin/request-otp` | Sends an OTP to the email of a user about to be created |
| `POST /api/v1/admin/users` | Creates a user. Needs `account`, `password` (6+ characters), `display_name`, `role`, and the OTP sent to that email |
| `DELETE /api/v1/admin/users/{id}` | Deletes the user document. You cannot delete yourself |

## WebSocket

Connect to `/api/v1/ws?token=<JWT>`. An invalid or missing token closes the socket with code 1008.

Messages from the server:

| `type` | When |
|---|---|
| `INITIAL_PRESENCE` | Right after connecting: `online_user_ids` |
| `USER_PRESENCE` | A user comes online or goes offline, with `is_online`, `timestamp` and `last_seen` on disconnect |
| `NEW_MESSAGE`, `ROOM_UPDATED`, `READ_RECEIPT`, `FRIEND_REQUEST`, `FRIEND_ACCEPTED` | Forwarded from the backend's `SubscribeEvents` stream. `NEW_MESSAGE` carries a `message` object |
| `CALL_OFFER`, `CALL_ANSWER`, `CALL_HANGUP`, `CALL_BUSY`, `ICE_CANDIDATE`, `CALL_UNAVAILABLE` | Call signaling relayed between two users |

From the client: send the text `ping` to get `pong`, or a JSON object whose `type` starts with `CALL_` for call signaling. The gateway keeps one backend stream per socket. If that stream ends (backend restart, database error) the gateway closes the socket with code 1011 so the app reconnects and subscribes again. Missed or declined calls are written into the room as a text message.

## Push notifications

The app registers an Expo push token with `POST /api/v1/users/push-token` (it must start with `ExponentPushToken`). After a message is sent, the gateway looks up the sender's rooms through gRPC and sends a push through `exp.host` to the other members of that room.

## AI features

- `ai/chat` and in-room replies use Gemini with the system prompt of the HQ AI assistant. If no key is set or every model fails, `ai_service.py` falls back to built-in answers, including an arithmetic and percentage solver.
- A message that contains `@hq ai`, `@hqai`, `@ai` or `@hq` (any case) triggers an AI reply in the same room, posted as the bot user `hq_ai_bot` with the prefix `[HQ AI]`.
- `security_ai.py` masks phone numbers, email addresses, OTP codes and password or token strings in chat text before it is placed in a prompt, and wraps the chat in a delimited block with an instruction to ignore commands found inside it.
- `semantic-search` and `rag-chat` first read the latest 50 messages of the room through `GetMessageHistory`, which also makes the backend check that the caller belongs to the room. Messages are indexed into the vector store, and results are ranked by cosine similarity over 128-dimension vectors. With a Gemini key the vectors come from `text-embedding-004` (reduced to 128 dimensions), otherwise from a local hashing vectorizer.
- Every text message sent through `POST /api/v1/chat/send` is also indexed. The index lives in `vector_store.json` and is loaded into memory at startup.

## What the gateway writes to MongoDB itself

Most operations go through gRPC, but the gateway also uses PyMongo directly:

| Collection | What the gateway does |
|---|---|
| `users` | Reads emails, roles and active flags. Writes `role` and `is_active` changes, creates and deletes users from the admin API, creates the `hq_ai_bot` user, and creates an account itself if `AuthService.Register` fails |
| `otp_codes` | Creates and verifies OTP records in the same format as the backend's `OtpService` |
| `messages` and `rooms` | Inserts AI replies and call-history messages, adds `hq_ai_bot` to a room's `member_ids`, and updates the room's last message. Reads the latest 30 messages for in-room AI replies |
| all of the above | `admin/stats` counts documents in `users`, `rooms` and `messages` |

## Error mapping

| gRPC status | HTTP |
|---|---|
| `INVALID_ARGUMENT` | 400, with the backend's message |
| `UNAUTHENTICATED` | 401 |
| `PERMISSION_DENIED` | 403 |
| `NOT_FOUND` | 404 |
| `ALREADY_EXISTS` | 409 |
| `RESOURCE_EXHAUSTED` | 429 |
| `UNAVAILABLE` | 503 |
| `DEADLINE_EXCEEDED` | 504 |
| anything else | 500, details only in the log |

Request bodies that fail Pydantic validation return 422 and never reach the backend. On most endpoints the backend's own message for 401, 403, 404, 409 and 429 is passed to the client, because the backend wrote those for end users. Other statuses get a short generic message.

## Deployment

The repository has two ways to run in a container:

- `Dockerfile` builds a gateway-only image and runs `uvicorn main:app` on `${PORT:-8000}` with `--proxy-headers`.
- `start.sh` is for a single container that holds both services. It starts the Java backend in the background with `PORT=50051`, `-Xmx256m` and the serial garbage collector (classpath `/app/java/classes:/app/java/lib/*`), waits up to 60 seconds for port 50051, exits if Java dies, sets `JAVA_GRPC_TARGET=localhost:50051`, and then runs the gateway on `${PORT:-10000}`. The Dockerfile in this repository does not install Java or copy the backend, so `start.sh` needs an image built for that purpose.

Presence, push tokens, active calls and token revocation are kept in the memory of one process, so run a single gateway instance.

## Known limitations

These come from reading the code and are worth fixing before a real deployment.

- Accounts created by the gateway directly in MongoDB (the `Register` fallback and `POST /api/v1/admin/users`) store an unsalted SHA-256 hash, while the Java backend checks passwords with bcrypt. Those accounts cannot log in through `AuthService.Login`.
- OTP logic exists twice, in `OtpService` (Java) and in `email_service.py`. Both must use the same `OTP_SECRET` and record format, and `Register` verifies the code in the gateway before the backend sees it.
- The gateway inserts messages and edits rooms directly for AI replies and call history. Those writes skip the backend's last-message logic for rooms and its `EVENT_NEW_MESSAGE` event, although the gateway pushes its own notifications afterwards.
- Only messages whose `type` starts with `CALL_` are handed to the call-signaling code, so a client message with `type` `ICE_CANDIDATE` is dropped even though the handler has a branch for it.
- Disabling an account blocks new logins but does not invalidate tokens that were already issued. A demoted admin keeps admin access until the token expires, because the token's role is trusted.
- Token revocation after a password change is stored in memory and is lost on restart.
- `DELETE /api/v1/admin/users/{id}` removes only the user document, not their rooms, messages or friendships.
- Default admin emails are hard-coded in `auth.py`, and CORS allows every origin with credentials.
- `vector_store.json` is rewritten in full after every indexed message and lives on local disk, which is ephemeral on most hosting platforms.