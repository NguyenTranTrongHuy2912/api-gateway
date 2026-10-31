# Graph Report - api-gateway  (2026-10-31)

## Corpus Check
- 5 files · ~2,542 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 3, .example 1, .proto 1)

## Summary
- 94 nodes · 150 edges · 11 communities (5 shown, 6 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `28b4c2bc`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- main.py
- chat_pb2_grpc.py
- ChatServiceServicer
- Request
- auth.py
- ChatServiceStub
- AuthServiceServicer
- create_room
- BaseModel
- to_http_error
- ChatService

## God Nodes (most connected - your core abstractions)
1. `to_http_error()` - 12 edges
2. `ChatService` - 8 edges
3. `ChatServiceServicer` - 8 edges
4. `send_message()` - 7 edges
5. `register()` - 6 edges
6. `login()` - 6 edges
7. `create_room()` - 6 edges
8. `create_token()` - 6 edges
9. `request_otp()` - 5 edges
10. `list_rooms()` - 5 edges

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

## Communities (11 total, 6 thin omitted)

### Community 1 - "chat_pb2_grpc.py"
Cohesion: 0.16
Nodes (3): add_AuthServiceServicer_to_server(), add_ChatServiceServicer_to_server(), AuthService

### Community 3 - "Request"
Cohesion: 0.32
Nodes (5): get_history(), list_rooms(), room_to_dict(), root(), search_users()

### Community 5 - "ChatServiceStub"
Cohesion: 0.20
Nodes (3): AuthServiceStub, ChatServiceStub, lifespan()

### Community 9 - "BaseModel"
Cohesion: 0.32
Nodes (6): mark_room_read(), MarkReadBody, register(), RegisterBody, request_otp(), RequestOtpBody

### Community 10 - "to_http_error"
Cohesion: 0.40
Nodes (3): login(), LoginBody, to_http_error()

## Knowledge Gaps
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChatServiceServicer` connect `ChatServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.241) - this node is a cross-community bridge._
- **Why does `ChatService` connect `ChatService` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.146) - this node is a cross-community bridge._
- **Why does `AuthServiceServicer` connect `AuthServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.145) - this node is a cross-community bridge._