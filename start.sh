#!/bin/sh
# Chạy Java gRPC backend (nền) rồi chạy FastAPI gateway (chính).
# Render đặt PORT cho cổng public; Java phải dùng cổng riêng 50051 trong container.

# 1) Java backend: ép PORT=50051 CHỈ cho tiến trình Java
PORT=50051 java \
  -Xmx256m \
  -XX:+UseSerialGC \
  -cp "/app/java/classes:/app/java/lib/*" \
  iuh.fit.grpc.GrpcServerApp &
JAVA_PID=$!

# 2) Chờ Java mở cổng 50051 (tối đa ~60 giây)
i=0
while [ $i -lt 60 ]; do
  if python -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',50051)); s.close()" 2>/dev/null; then
    echo "Java gRPC backend da san sang."
    break
  fi
  if ! kill -0 "$JAVA_PID" 2>/dev/null; then
    echo "Java backend da thoat bat thuong, dung container."
    exit 1
  fi
  i=$((i + 1))
  sleep 1
done

# 3) Gateway gọi Java qua localhost trong cùng container
export JAVA_GRPC_TARGET="localhost:50051"

# exec để uvicorn nhận tín hiệu dừng từ Render
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}" --proxy-headers --forwarded-allow-ips='*'
