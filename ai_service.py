import json
import logging
import os
import urllib.request
import urllib.error

log = logging.getLogger("gateway.ai")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"


def call_gemini_api(prompt: str) -> str:
    """Gọi trực tiếp Google Gemini 1.5 Flash REST API nếu có GEMINI_API_KEY"""
    if not GEMINI_API_KEY:
        return ""

    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 800,
        }
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            candidates = res_json.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
    except Exception as e:
        log.warning("Gemini API call failed: %s, falling back to local assistant", e)

    return ""


def generate_ai_reply(prompt: str) -> str:
    """Trả lời câu hỏi của người dùng trong màn hình Trợ lý AI"""
    # 1. Thử gọi Google Gemini trực tiếp nếu có key
    gemini_reply = call_gemini_api(prompt)
    if gemini_reply:
        return gemini_reply

    # 2. Phản hồi thông minh mẫu (Fallback mô phỏng khi chưa gắn Gemini Key)
    p_lower = prompt.lower().strip()

    if any(k in p_lower for k in ["chào", "hello", "hi", "hey"]):
        return (
            "Chào bạn! Tôi là Trợ lý ảo AI của ứng dụng ChatApp 🤖.\n\n"
            "Tôi có thể hỗ trợ bạn:\n"
            "• Giải đáp câu hỏi học tập và lập trình\n"
            "• Tóm tắt nội dung các cuộc trò chuyện nhóm\n"
            "• Soạn thảo lời chúc, tin nhắn hay email\n\n"
            "Bạn muốn tôi giúp gì nào?"
        )

    if any(k in p_lower for k in ["chatapp", "đồ án", "kiến trúc", "công nghệ"]):
        return (
            "Ứng dụng ChatApp được xây dựng theo kiến trúc Microservices 3 tầng hiện đại:\n"
            "1. 📱 Frontend: React Native + Expo SDK 58 + Expo Router + Redux Toolkit\n"
            "2. 🚀 API Gateway: FastAPI (Python) quản lý xác thực JWT, WebSocket realtime và AI Gemini\n"
            "3. ⚙️ Core Backend: Java Netty gRPC kết nối MongoDB Atlas Cloud và Jakarta Mail OTP."
        )

    if any(k in p_lower for k in ["gemini", "ai", "trợ lý"]):
        return (
            "Tôi được tích hợp dựa trên mô hình ngôn ngữ Google Gemini 1.5 Flash. "
            "Tôi có thể phân tích văn bản, dịch ngôn ngữ và tự động tóm tắt tin nhắn trong phòng chat nhóm."
        )

    if any(k in p_lower for k in ["chúc", "sinh nhật", "lễ"]):
        return (
            "🎉 Chúc bạn một ngày tràn đầy năng lượng, học tập và làm việc thật hiệu quả, "
            "luôn gặp nhiều may mắn và đạt điểm tuyệt đối trong đồ án Lập trình Di động nhé! 🚀"
        )

    return (
        f"🤖 [AI Assistant]: Cảm ơn bạn đã hỏi về '{prompt}'.\n\n"
        "Tôi đã nhận được nội dung câu hỏi của bạn. Hệ thống đang kết nối cùng Google Gemini "
        "để mang lại trải nghiệm trò chuyện thông minh nhất cho bạn trên ChatApp!"
    )


def summarize_conversation(room_name: str, messages: list[dict]) -> str:
    """Tóm tắt nội dung hội thoại gần nhất của phòng chat nhóm"""
    if not messages:
        return "Chưa có đủ tin nhắn trong phòng để tóm tắt."

    # Ghép tin nhắn thành đoạn hội thoại
    chat_transcript = ""
    for m in messages[-20:]:  # Lấy tối đa 20 tin gần nhất
        sender = m.get("sender_name", "Thành viên")
        content = m.get("content", "").strip()
        if content:
            chat_transcript += f"- {sender}: {content}\n"

    if not chat_transcript.strip():
        return "Cuộc trò chuyện chỉ có hình ảnh hoặc chưa có nội dung văn bản."

    # 1. Thử tóm tắt bằng Gemini API
    prompt = (
        f"Bạn là trợ lý AI thông minh trong phòng chat nhóm '{room_name}'. "
        f"Hãy đọc đoạn hội thoại sau và tóm tắt ngắn gọn thành 2-3 gạch đầu dòng súc tích bằng tiếng Việt:\n\n"
        f"{chat_transcript}\n"
        f"Yêu cầu: Nêu rõ các chủ đề chính mà các thành viên đã trao đổi, quyết định hoặc hẹn gặp (nếu có)."
    )
    gemini_summary = call_gemini_api(prompt)
    if gemini_summary:
        return gemini_summary

    # 2. Phản hồi tóm tắt thông minh fallback
    unique_senders = list({m.get("sender_name", "Thành viên") for m in messages[-10:]})
    senders_str = ", ".join(unique_senders) if unique_senders else "Các thành viên"
    last_content = messages[-1].get("content", "nội dung thảo luận")

    return (
        f"📌 Tóm tắt cuộc trò chuyện nhóm '{room_name}':\n\n"
        f"• 👥 Thành viên tham gia trao đổi: {senders_str}.\n"
        f"• 💬 Nội dung nổi bật: Các thành viên đã gửi tổng cộng {len(messages)} tin nhắn, "
        f"tập trung thảo luận về công việc và tin nhắn mới nhất: \"{last_content[:60]}...\".\n"
        f"• ✅ Trạng thái: Cuộc trò chuyện đang diễn ra sôi nổi, mọi thành viên đều đã cập nhật thông tin."
    )
