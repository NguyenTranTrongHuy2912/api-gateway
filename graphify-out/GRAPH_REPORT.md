# Graph Report - api-gateway  (2026-10-30)

## Corpus Check
- 4 files · ~1,823 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 4 file(s) not represented in the graph (top: (none) 3, .proto 1)

## Summary
- 70 nodes · 98 edges · 11 communities (3 shown, 8 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `49c5b53e`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- chat_pb2_grpc.py
- ChatServiceServicer
- get_history
- send_message
- ChatServiceStub
- AuthServiceServicer
- AuthService
- register
- ChatService
- to_http_error

## God Nodes (most connected - your core abstractions)
1. `to_http_error()` - 8 edges
2. `send_message()` - 7 edges
3. `request_otp()` - 5 edges
4. `register()` - 5 edges
5. `login()` - 5 edges
6. `AuthServiceServicer` - 5 edges
7. `AuthService` - 5 edges
8. `lifespan()` - 4 edges
9. `get_history()` - 4 edges
10. `ChatServiceServicer` - 4 edges

## Surprising Connections (you probably didn't know these)
- `lifespan()` --calls--> `AuthServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py
- `lifespan()` --calls--> `ChatServiceStub`  [EXTRACTED]
  main.py → chat_pb2_grpc.py

## Import Cycles
- None detected.

## Communities (11 total, 8 thin omitted)

### Community 5 - "ChatServiceStub"
Cohesion: 0.20
Nodes (3): AuthServiceStub, ChatServiceStub, lifespan()

### Community 8 - "register"
Cohesion: 0.40
Nodes (4): LoginBody, register(), RegisterBody, SendMessageRequest

### Community 10 - "to_http_error"
Cohesion: 0.33
Nodes (4): login(), request_otp(), RequestOtpBody, to_http_error()

## Knowledge Gaps
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AuthServiceServicer` connect `AuthServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.193) - this node is a cross-community bridge._
- **Why does `ChatServiceServicer` connect `ChatServiceServicer` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.140) - this node is a cross-community bridge._
- **Why does `AuthService` connect `AuthService` to `chat_pb2_grpc.py`?**
  _High betweenness centrality (0.113) - this node is a cross-community bridge._