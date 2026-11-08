# Graph Report - api-gateway  (2026-11-08)

## Corpus Check
- 9 files · ~8,931 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 3, .example 1, .proto 1)

## Summary
- 237 nodes · 432 edges · 18 communities (12 shown, 6 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `cbda3512`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- main.py
- chat_pb2_grpc.py
- ChatServiceServicer
- to_http_error
- vector_db.py
- auth.py
- AuthServiceServicer
- FastAPI gRPC Gateway & BFF
- send_message
- Request
- BaseModel
- post
- set_user_role
- ai_chat
- rate_limit_handler
- rag_chat
- semantic_search
- change_password_logged_in

## God Nodes (most connected - your core abstractions)
1. `to_http_error()` - 23 edges
2. `ChatServiceServicer` - 13 edges
3. `ChatService` - 13 edges
4. `MessageVectorDB` - 10 edges
5. `send_message()` - 9 edges
6. `AuthServiceServicer` - 8 edges
7. `AuthService` - 8 edges
8. `login()` - 8 edges
9. `sign_upload()` - 8 edges
10. `generate_embedding()` - 8 edges

## Surprising Connections (you probably didn't know these)
- `ai_chat()` --calls--> `generate_ai_reply()`  [EXTRACTED]
  main.py → ai_service.py
- `ai_summarize_chat()` --calls--> `summarize_conversation()`  [EXTRACTED]
  main.py → ai_service.py
- `change_password_logged_in()` --calls--> `revoke_user_tokens()`  [EXTRACTED]
  main.py → auth.py
- `login()` --calls--> `create_token()`  [EXTRACTED]
  main.py → auth.py
- `register()` --calls--> `create_token()`  [EXTRACTED]
  main.py → auth.py

## Import Cycles
- None detected.

## Communities (18 total, 6 thin omitted)

### Community 0 - "main.py"
Cohesion: 0.15
Nodes (6): get_user_role(), get_my_profile(), login(), LoginBody, register(), RegisterBody

### Community 1 - "chat_pb2_grpc.py"
Cohesion: 0.05
Nodes (7): add_AuthServiceServicer_to_server(), add_ChatServiceServicer_to_server(), AuthService, AuthServiceStub, ChatService, ChatServiceStub, lifespan()

### Community 3 - "to_http_error"
Cohesion: 0.25
Nodes (6): ai_summarize_chat(), AiSummarizeBody, create_room(), list_rooms(), room_to_dict(), to_http_error()

### Community 4 - "vector_db.py"
Cohesion: 0.10
Nodes (7): call_gemini_api(), generate_ai_reply(), summarize_conversation(), calculate_cosine_similarity(), generate_embedding(), generate_local_embedding(), MessageVectorDB

### Community 5 - "auth.py"
Cohesion: 0.14
Nodes (7): create_token(), decode_token(), get_current_admin(), get_current_user(), revoke_user_tokens(), broadcast_ws_event(), chat_websocket()

### Community 7 - "FastAPI gRPC Gateway & BFF"
Cohesion: 0.15
Nodes (12): ⚙️ Configuration, Endpoints, Error Mapping, FastAPI gRPC Gateway & BFF, `GET /api/v1/chat/history/{room_id}`, 🚀 Getting Started, Installation & Setup, 🧪 Interactive API Documentation (+4 more)

### Community 8 - "send_message"
Cohesion: 0.15
Nodes (8): FriendRequestBody, is_own_image_url(), notify_new_message(), send_expo_push(), send_friend_request(), send_message(), SendMessageRequest, sign_upload()

### Community 9 - "Request"
Cohesion: 0.27
Nodes (8): get_admin_stats(), get_admin_users(), get_history(), get_online_users(), list_friend_requests(), list_friends(), root(), search_users()

### Community 10 - "BaseModel"
Cohesion: 0.22
Nodes (7): CreateRoomBody, mark_room_read(), MarkReadBody, respond_friend_request(), RespondFriendRequestBody, update_profile(), UpdateProfileBody

### Community 11 - "post"
Cohesion: 0.31
Nodes (7): forgot_password(), register_push_token(), RegisterPushTokenBody, request_otp(), RequestOtpBody, reset_password(), ResetPasswordBody

### Community 12 - "set_user_role"
Cohesion: 0.29
Nodes (5): set_user_role(), SetUserRoleBody, SetUserStatusBody, update_user_role_admin(), update_user_status_admin()

### Community 13 - "ai_chat"
Cohesion: 0.40
Nodes (3): ai_chat(), AiChatBody, normalize_vn()

## Knowledge Gaps
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChatServiceServicer` connect `ChatServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.167) - this node is a cross-community bridge._
- **Should `chat_pb2_grpc.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05384615384615385 - nodes in this community are weakly interconnected._
- **Why does `AuthServiceServicer` connect `AuthServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Should `ChatServiceServicer` be split into smaller, more focused modules?**
  _Cohesion score 0.08695652173913043 - nodes in this community are weakly interconnected._
- **Should `vector_db.py` be split into smaller, more focused modules?**
  _Cohesion score 0.09523809523809523 - nodes in this community are weakly interconnected._
- **Should `auth.py` be split into smaller, more focused modules?**
  _Cohesion score 0.1368421052631579 - nodes in this community are weakly interconnected._