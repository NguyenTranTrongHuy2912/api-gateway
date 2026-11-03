# Graph Report - api-gateway  (2026-11-03)

## Corpus Check
- 5 files · ~2,709 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 3, .example 1, .proto 1)

## Summary
- 100 nodes · 165 edges · 8 communities (3 shown, 5 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `f67aec99`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- main.py
- AuthService
- ChatServiceServicer
- to_http_error
- auth.py
- ChatServiceStub
- AuthServiceServicer
- ChatService

## God Nodes (most connected - your core abstractions)
1. `to_http_error()` - 14 edges
2. `ChatServiceServicer` - 8 edges
3. `ChatService` - 8 edges
4. `send_message()` - 7 edges
5. `AuthServiceServicer` - 6 edges
6. `AuthService` - 6 edges
7. `register()` - 6 edges
8. `login()` - 6 edges
9. `create_room()` - 6 edges
10. `create_token()` - 6 edges

## Surprising Connections (you probably didn't know these)
- `register()` --calls--> `create_token()`  [EXTRACTED]
  main.py → auth.py
- `login()` --calls--> `create_token()`  [EXTRACTED]
  main.py → auth.py
- `lifespan()` --calls--> `ChatServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py
- `lifespan()` --calls--> `AuthServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py

## Import Cycles
- None detected.

## Communities (8 total, 5 thin omitted)

### Community 0 - "main.py"
Cohesion: 0.17
Nodes (8): CreateRoomBody, LoginBody, MarkReadBody, RegisterBody, ResetPasswordBody, root(), send_message(), SendMessageRequest

### Community 3 - "to_http_error"
Cohesion: 0.22
Nodes (13): create_room(), forgot_password(), get_history(), list_rooms(), login(), mark_room_read(), register(), request_otp() (+5 more)

### Community 5 - "ChatServiceStub"
Cohesion: 0.12
Nodes (5): add_AuthServiceServicer_to_server(), add_ChatServiceServicer_to_server(), AuthServiceStub, ChatServiceStub, lifespan()

## Knowledge Gaps
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChatServiceServicer` connect `ChatServiceServicer` to `ChatServiceStub`?**
  _High betweenness centrality (0.228) - this node is a cross-community bridge._
- **Should `ChatServiceStub` be split into smaller, more focused modules?**
  _Cohesion score 0.12280701754385964 - nodes in this community are weakly interconnected._
- **Why does `AuthServiceServicer` connect `AuthServiceServicer` to `ChatServiceStub`?**
  _High betweenness centrality (0.174) - this node is a cross-community bridge._
- **Why does `ChatService` connect `ChatService` to `ChatServiceStub`?**
  _High betweenness centrality (0.137) - this node is a cross-community bridge._