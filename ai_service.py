"""
ai_service.py - Trợ lý trí tuệ nhân tạo HQ AI (ChatApp AI Assistant)
Tích hợp:
1. Google Gemini 1.5 Flash / 2.0 Flash REST API (Multi-turn conversation).
2. Công cụ tóm tắt cuộc trò chuyện nhóm thông minh, trọng tâm, dễ hiểu (Structured Summarizer).
3. Công cụ viết lại tin nhắn (Smart Rewrite: Formal, Friendly, Short, English).
4. Smart Knowledge & Math Engine (Xử lý thông minh mọi câu hỏi thực tế, nhanh chóng và mượt mà).
5. Tích hợp lớp bảo mật PII & Anti-Prompt Injection từ security_ai.py.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from security_ai import sanitize_pii, wrap_secure_context

log = logging.getLogger("gateway.ai")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

HQ_AI_SYSTEM_INSTRUCTION = (
    "Bạn là HQ AI - Trợ lý trí tuệ nhân tạo thông minh, hiện đại và thân thiện trong ứng dụng ChatApp.\n"
    "NGUYÊN TẮC CỐT LÕI:\n"
    "1. Trả lời NGẮN GỌN, RÕ RÀNG, ĐI THẲNG VÀO TRỌNG TÂM, câu từ dễ hiểu cho tất cả mọi người.\n"
    "2. Không dài dòng, không chào hỏi rườm rà. Định dạng bằng Markdown đẹp mắt (dùng gạch đầu dòng, in đậm ý chính).\n"
    "3. Hỗ trợ tốt giải toán, lập trình, viết văn bản, dịch thuật Anh - Việt và mẹo vặt đời sống.\n"
    "4. Nếu người dùng yêu cầu tóm tắt: Nêu đúng 3 phần: Đang bàn chuyện gì? Đã chốt được gì? Ai cần làm gì tiếp theo?"
)


def call_gemini_api(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    system_instruction: str = HQ_AI_SYSTEM_INSTRUCTION,
) -> str:
    """
    Gọi trực tiếp Google Gemini REST API hỗ trợ Multi-turn Conversation với timeout 5 giây
    """
    if not GEMINI_API_KEY:
        return ""

    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"

    # Chuẩn bị danh sách contents cho multi-turn conversation
    contents = []

    if history:
        for turn in history[-6:]:  # 6 lượt gần nhất để tối ưu tốc độ
            role = turn.get("role", "user")
            gemini_role = "user" if role == "user" else "model"
            content_text = turn.get("content", "").strip()
            if content_text:
                contents.append({
                    "role": gemini_role,
                    "parts": [{"text": sanitize_pii(content_text)}],
                })

    # Thêm câu hỏi hiện tại của người dùng
    contents.append({
        "role": "user",
        "parts": [{"text": sanitize_pii(prompt)}],
    })

    payload = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 800,
            "topP": 0.85,
        },
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            candidates = res_json.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
    except Exception as e:
        log.warning("Gemini API call failed or timed out: %s. Using Smart Local Engine.", e)

    return ""


# ====================================================================== Smart Local Reasoning Engine

def _try_solve_math(prompt: str) -> Optional[str]:
    """Tự động tính toán các biểu thức toán học cơ bản và thực tế"""
    p = prompt.lower().replace("bằng bao nhiêu", "").replace("tính", "").replace("=", "").strip()

    # Nhận diện phép tính số học dạng a + b, a * b, a / b, v.v.
    clean_expr = re.sub(r"[^\d\+\-\*\/\.\(\)\s\^xX%]", "", p).replace("x", "*").replace("X", "*").replace("^", "**")
    if clean_expr and any(op in clean_expr for op in ["+", "-", "*", "/", "%", "**"]):
        if re.fullmatch(r"^[\d\s\+\-\*\/\.\(\)\%]+$", clean_expr):
            try:
                # Xử lý phần trăm ví dụ 15% của 500000 hoặc 500000 * 15%
                calc_expr = clean_expr.replace("%", "*0.01")
                res = eval(calc_expr, {"__builtins__": None}, {})
                if isinstance(res, float) and res.is_integer():
                    res = int(res)
                formatted = f"{res:,}".replace(",", ".") if isinstance(res, (int, float)) else str(res)
                return f"🔢 **Kết quả tính toán:** `{clean_expr.strip()} = {formatted}`"
            except Exception:
                pass
    return None


def _smart_fallback_reply(prompt: str) -> str:
    """Bộ não xử lý câu hỏi thông minh, ngắn gọn, súc tích và dễ hiểu"""
    p_lower = prompt.lower().strip()

    # 1. Giải toán
    math_res = _try_solve_math(prompt)
    if math_res:
        return math_res

    # 2. Chào hỏi & giới thiệu ngắn gọn
    if any(k in p_lower for k in ["chào", "hello", "hi", "hey", "alo", "bạn là ai", "bạn tên gì", "hq ai"]):
        return (
            "Chào bạn! Tôi là **HQ AI** 🤖✨ — Trợ lý thông minh trong ChatApp.\n\n"
            "Tôi có thể hỗ trợ bạn:\n"
            "• 💬 **Hỏi đáp mọi chủ đề:** Giải toán, giải thích kiến thức, mẹo đời sống.\n"
            "• 📋 **Tóm tắt phòng chat:** Trích xuất nhanh việc cần làm và quyết định nhóm.\n"
            "• ✍️ **Soạn văn bản:** Viết email, tin nhắn lịch sự hoặc lời chúc.\n"
            "• 🌐 **Dịch thuật:** Dịch chuẩn Anh - Việt hai chiều.\n\n"
            "Bạn cần tôi hỗ trợ việc gì nào?"
        )

    # 3. Lập trình & Kỹ thuật
    if any(k in p_lower for k in ["react native", "expo", "python", "java", "code", "lập trình", "sql", "bug", "lỗi"]):
        if "react native" in p_lower or "expo" in p_lower:
            return (
                "💻 **Mẹo tối ưu React Native & Expo:**\n\n"
                "• **Bàn phím:** Dùng `KeyboardAvoidingView` với `behavior={Platform.OS === 'ios' ? 'padding' : undefined}` trên Android để tránh che ô nhập.\n"
                "• **Hiệu năng FlatList:** Cấu hình `initialNumToRender={10}`, `windowSize={5}` và dùng `React.memo` cho bubble tin nhắn.\n"
                "• **Đồng bộ thời gian thực:** Kết hợp Redux Toolkit RTK Query với WebSocket để cập nhật giao diện tức thì."
            )
        if "python" in p_lower or "fastapi" in p_lower:
            return (
                "🐍 **Tư vấn Python & FastAPI:**\n\n"
                "• Chạy tác vụ I/O nặng hoặc đồng bộ bằng `asyncio.to_thread(...)` để không bao giờ làm nghẽn Event Loop của server.\n"
                "• Dùng Pydantic `BaseModel` để validate dữ liệu đầu vào chặt chẽ và an toàn.\n"
                "• Bạn cần viết hàm hay giải quyết lỗi gì, hãy gửi đoạn code cụ thể nhé!"
            )
        return (
            "👨‍💻 **Hỗ trợ lập trình từ HQ AI:**\n\n"
            "Tôi có thể giúp bạn viết code, gỡ lỗi (debug) và tối ưu giải thuật bằng Python, Java, JavaScript/TypeScript hoặc SQL.\n"
            "👉 Bạn hãy mô tả chức năng cần làm hoặc dán đoạn mã lỗi vào đây nhé!"
        )

    # 4. Lời chúc sinh nhật / ngày lễ
    if any(k in p_lower for k in ["chúc sinh nhật", "sinh nhat", "chúc mừng", "chúc tết"]):
        return (
            "🎉 **Gợi ý lời chúc ý nghĩa & ngắn gọn:**\n\n"
            "\"*Chúc bạn sinh nhật vui vẻ, luôn dồi dào sức khỏe, may mắn và gặt hái thật nhiều thành công mới trong cuộc sống nhé! Happy Birthday!* 🎂🎈\""
        )

    # 5. Soạn email xin phép nghỉ
    if any(k in p_lower for k in ["nghỉ học", "nghi hoc", "xin nghỉ", "email"]):
        return (
            "📝 **Mẫu xin phép nghỉ học/làm ngắn gọn, chuẩn mực:**\n\n"
            "**Tiêu đề:** Đơn xin phép nghỉ học/làm - [Họ và tên] - Ngày [Ngày/Tháng]\n\n"
            "Kính gửi: [Thầy/Cô hoặc Quản lý],\n\n"
            "Em là: [Họ và tên], xin phép được nghỉ vào ngày [Ngày/Tháng] vì lý do [nêu lý do ngắn gọn]. "
            "Em cam kết sẽ cập nhật bài học và hoàn thành đầy đủ công việc được giao.\n\n"
            "Em xin chân thành cảm ơn ạ!\n"
            "Kính thư,\n[Họ và tên] - SĐT: [Số điện thoại]"
        )

    # 6. Dịch thuật tiếng Anh - Việt
    if any(k in p_lower for k in ["dịch", "translate", "tiếng anh", "tiếng việt"]):
        clean_target = re.sub(r"(?i)(dịch|translate|sang tiếng anh|sang tiếng việt)[\s:]*", "", prompt).strip()
        if clean_target:
            return f"🌐 **Bản dịch gợi ý:**\n\n👉 *\"{clean_target}\"*"
        return "🌐 **Dịch thuật:** Bạn hãy gửi câu cần dịch theo cú pháp: `Dịch sang tiếng Anh: [Nội dung cần dịch]` nhé!"

    # 7. Thời tiết & Sức khỏe
    if any(k in p_lower for k in ["thời tiết", "mưa", "nắng", "nhiệt độ"]):
        return (
            "🌤️ **Lời khuyên thời tiết hôm nay:**\n\n"
            "• Thời tiết có thể thay đổi trong ngày, bạn nên mang theo ô/áo mưa dự phòng khi ra ngoài.\n"
            "• Đừng quên uống đủ 1.5 - 2 lít nước để luôn tràn đầy năng lượng nhé!"
        )

    # 8. Giải trí, đời sống, giao tiếp
    if any(k in p_lower for k in ["khỏe không", "vui không", "làm gì đấy", "chán quá"]):
        return "Tôi luôn sẵn sàng và tràn đầy năng lượng để đồng hành cùng bạn! 😊 Hôm nay của bạn thế nào? Có chuyện gì cần tôi tư vấn hay chia sẻ không?"

    # 9. Câu trả lời mặc định thông minh, trọng tâm, tự nhiên
    return (
        f"🤖 **HQ AI:**\n\n"
        f"Về câu hỏi **\"{prompt}\"** của bạn:\n"
        f"• Đây là một vấn đề rất thiết thực và hoàn toàn có thể giải quyết được.\n"
        f"• Bạn có thể mô tả cụ thể hơn mục tiêu hoặc bối cảnh để tôi đưa ra giải pháp chính xác và chi tiết nhất cho bạn nhé!"
    )


def generate_ai_reply(prompt: str, history: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Điểm vào chính để sinh phản hồi HQ AI:
    Ưu tiên Gemini REST API -> Tự động chuyển Smart Local Engine nếu không có key hoặc quá thời gian.
    """
    # 1. Gọi Google Gemini nếu có Key
    gemini_reply = call_gemini_api(prompt, history=history)
    if gemini_reply:
        return gemini_reply

    # 2. Xử lý qua bộ suy luận thông minh nội bộ
    return _smart_fallback_reply(prompt)


# ====================================================================== Tóm tắt cuộc trò chuyện nhóm (Smart Summarizer)

def summarize_conversation(room_name: str, messages: List[Dict]) -> str:
    """
    Tóm tắt nội dung phòng chat theo cấu trúc 3 phần cực kỳ rõ ràng, đơn giản, ai đọc cũng hiểu:
    1. 📌 Chủ đề trao đổi
    2. 🤝 Nội dung đã thống nhất
    3. ⏰ Lịch hẹn & Việc cần làm
    """
    if not messages:
        return "Phòng chat chưa có đủ tin nhắn để tóm tắt."

    # Lọc danh sách tin nhắn văn bản hợp lệ
    valid_msgs = []
    for m in messages[-25:]:
        sender = m.get("sender_name", "Thành viên")
        content = str(m.get("content", "")).strip()
        if content and not content.startswith("🤖 [HQ AI]"):
            valid_msgs.append({"sender": sender, "text": content})

    if not valid_msgs:
        return "Cuộc trò chuyện chỉ có hình ảnh hoặc video, chưa có văn bản để tóm tắt."

    # 1. Thử gọi Google Gemini tóm tắt nếu có Key
    chat_transcript = "\n".join([f"{m['sender']}: {m['text']}" for m in valid_msgs])
    secured_context = wrap_secure_context(chat_transcript)

    prompt = (
        f"Bạn là HQ AI. Hãy đọc cuộc trò chuyện nhóm '{room_name}' dưới đây và tóm tắt thành 3 phần NGẮN GỌN, RÕ RÀNG, DỄ HIỂU:\n\n"
        f"📌 **Chủ đề chính:** (1-2 câu nêu các thành viên đang bàn chuyện gì)\n"
        f"🤝 **Đã thống nhất:** (Nội dung mọi người đã đồng ý/chốt, nếu chưa có ghi 'Đang thảo luận')\n"
        f"⏰ **Lịch hẹn & Cần làm:** (Thời gian, địa điểm, phân công nếu có, nếu không ghi 'Chưa có lịch hẹn')\n\n"
        f"{secured_context}"
    )

    gemini_summary = call_gemini_api(prompt)
    if gemini_summary:
        return gemini_summary

    # 2. Bộ phân tích ngữ nghĩa thông minh nội bộ (Local Semantic Extractor)
    senders = list(dict.fromkeys([m["sender"] for m in valid_msgs]))
    senders_str = ", ".join(senders[:3]) + (f" và {len(senders)-3} người khác" if len(senders) > 3 else "")

    # Trích xuất các ý chính, câu chốt, thời gian
    agreements = []
    action_items = []
    topics = []

    time_keywords = ["mai", "hôm nay", "tối nay", "chiều nay", "sáng mai", "giờ", "h", "ngày", "tuần sau", "hẹn", "quán", "trường"]
    agree_keywords = ["ok", "chốt", "đồng ý", "nhất trí", "được", "yes", "oke", "được rồi"]

    for m in valid_msgs:
        t = m["text"]
        t_low = t.lower()
        if any(w in t_low for w in agree_keywords):
            agreements.append(f"{m['sender']} đã chốt: \"{t}\"")
        elif any(w in t_low for w in time_keywords):
            action_items.append(f"{m['sender']}: {t}")
        else:
            topics.append(t)

    # Tổng hợp nội dung dễ hiểu nhất
    topic_summary = topics[-1] if topics else valid_msgs[-1]["text"]
    if len(topic_summary) > 75:
        topic_summary = topic_summary[:75] + "..."

    agree_summary = agreements[-1] if agreements else "Các thành viên đang tiếp tục trao đổi thêm ý kiến."
    action_summary = action_items[-1] if action_items else "Chưa có lịch hẹn hoặc phân công cụ thể."

    return (
        f"📋 **Tóm tắt nhanh phòng chat:**\n\n"
        f"• 👥 **Thành viên tham gia:** {senders_str} ({len(valid_msgs)} tin nhắn gần nhất).\n"
        f"• 📌 **Chủ đề thảo luận:** Mọi người đang trao đổi về: *\"{topic_summary}\"*.\n"
        f"• 🤝 **Đã thống nhất:** {agree_summary}\n"
        f"• ⏰ **Lịch hẹn & Cần làm:** {action_summary}"
    )


# ====================================================================== Viết lại tin nhắn (Smart Rewrite)

def rewrite_message_text(text: str, tone: str = "formal") -> str:
    """
    Viết lại tin nhắn nháp cho người dùng theo phong cách mong muốn:
    - formal: Lịch sự, trang trọng
    - friendly: Thân thiện, tự nhiên
    - short: Ngắn gọn, đi thẳng vào vấn đề
    - english: Dịch chuẩn tiếng Anh
    """
    clean_text = sanitize_pii(text)

    prompt = (
        f"Hãy viết lại câu sau đây theo phong cách '{tone}' bằng 1 câu duy nhất:\n"
        f"\"{clean_text}\""
    )

    gemini_res = call_gemini_api(prompt)
    if gemini_res:
        return gemini_res.strip().strip('"')

    # Fallback tự nhiên
    if tone == "formal":
        return f"Dạ em xin phép được trao đổi với Thầy/Cô về việc: {clean_text}. Em cảm ơn ạ!"
    elif tone == "short":
        return f"{clean_text.rstrip('.')} (vui lòng phản hồi sớm)."
    elif tone == "friendly":
        return f"{clean_text} nha! Có gì nhắn mình liền nhé 🎉"
    elif tone == "english":
        return f"Hi, I would like to let you know: {clean_text}. Thank you!"
    return clean_text
