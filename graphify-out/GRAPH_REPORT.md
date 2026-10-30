# Graph Report - api-gateway  (2026-10-30)

## Corpus Check
- 5 files · ~1,937 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 5 file(s) not represented in the graph (top: (none) 3, .example 1, .proto 1)

## Summary
- 75 nodes · 109 edges · 11 communities (4 shown, 7 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `d64c0a40`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- chat_pb2_grpc.py
- ChatServiceServicer
- get_history
- auth.py
- ChatServiceStub
- AuthServiceServicer
- AuthService
- register
- register
- to_http_error

## God Nodes (most connected - your core abstractions)
1. `to_http_error()` - 8 edges
2. `send_message()` - 7 edges
3. `AuthServiceServicer` - 5 edges
4. `AuthService` - 5 edges
5. `login()` - 5 edges
6. `request_otp()` - 5 edges
7. `register()` - 5 edges
8. `ChatServiceServicer` - 4 edges
9. `AuthServiceStub` - 4 edges
10. `ChatServiceStub` - 4 edges

## Surprising Connections (you probably didn't know these)
- `lifespan()` --calls--> `AuthServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py
- `lifespan()` --calls--> `ChatServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py

## Import Cycles
- None detected.

## Communities (11 total, 7 thin omitted)

### Community 1 - "chat_pb2_grpc.py"
Cohesion: 0.18
Nodes (3): add_AuthServiceServicer_to_server(), add_ChatServiceServicer_to_server(), ChatService

### Community 5 - "ChatServiceStub"
Cohesion: 0.20
Nodes (3): AuthServiceStub, ChatServiceStub, lifespan()

### Community 8 - "register"
Cohesion: 0.40
Nodes (3): LoginBody, send_message(), SendMessageRequest

### Community 10 - "to_http_error"
Cohesion: 0.33
Nodes (4): login(), request_otp(), RequestOtpBody, to_http_error()

## Knowledge Gaps
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AuthServiceServicer` connect `AuthServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.180) - this node is a cross-community bridge._
- **Why does `ChatServiceServicer` connect `ChatServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.131) - this node is a cross-community bridge._
- **Why does `AuthService` connect `AuthService` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._