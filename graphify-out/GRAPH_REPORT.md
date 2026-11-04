# Graph Report - api-gateway  (2026-11-04)

## Corpus Check
- 6 files · ~3,007 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 3, .example 1, .proto 1)

## Summary
- 108 nodes · 179 edges · 7 communities (4 shown, 3 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `44d8bbae`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- main.py
- chat_pb2_grpc.py
- ChatServiceServicer
- to_http_error
- ChatServiceStub
- AuthServiceServicer
- ChatService

## God Nodes (most connected - your core abstractions)
1. `to_http_error()` - 14 edges
2. `ChatService` - 8 edges
3. `ChatServiceServicer` - 8 edges
4. `send_message()` - 7 edges
5. `register()` - 6 edges
6. `login()` - 6 edges
7. `create_room()` - 6 edges
8. `sign_upload()` - 6 edges
9. `AuthService` - 6 edges
10. `AuthServiceServicer` - 6 edges

## Surprising Connections (you probably didn't know these)
- `register()` --calls--> `create_token()`  [EXTRACTED]
  main.py → auth.py
- `login()` --calls--> `create_token()`  [EXTRACTED]
  main.py → auth.py
- `lifespan()` --calls--> `AuthServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py
- `lifespan()` --calls--> `ChatServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py

## Import Cycles
- None detected.

## Communities (7 total, 3 thin omitted)

### Community 0 - "main.py"
Cohesion: 0.12
Nodes (6): create_token(), get_current_user(), is_own_image_url(), send_message(), SendMessageRequest, sign_upload()

### Community 1 - "chat_pb2_grpc.py"
Cohesion: 0.15
Nodes (3): add_AuthServiceServicer_to_server(), add_ChatServiceServicer_to_server(), AuthService

### Community 3 - "to_http_error"
Cohesion: 0.15
Nodes (19): create_room(), CreateRoomBody, forgot_password(), get_history(), list_rooms(), login(), LoginBody, mark_room_read() (+11 more)

### Community 5 - "ChatServiceStub"
Cohesion: 0.20
Nodes (3): AuthServiceStub, ChatServiceStub, lifespan()

## Knowledge Gaps
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChatServiceServicer` connect `ChatServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.212) - this node is a cross-community bridge._
- **Should `main.py` be split into smaller, more focused modules?**
  _Cohesion score 0.12307692307692308 - nodes in this community are weakly interconnected._
- **Why does `AuthServiceServicer` connect `AuthServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.161) - this node is a cross-community bridge._
- **Why does `ChatService` connect `ChatService` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.127) - this node is a cross-community bridge._