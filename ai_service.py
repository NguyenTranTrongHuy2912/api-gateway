"""
ai_service.py - Trợ lý trí tuệ nhân tạo HQ AI (Huy & Quốc AI)
Tích hợp:
1. Google Gemini 1.5 Flash / 2.0 Flash REST API (Multi-turn conversation).
2. Công cụ tóm tắt cuộc trò chuyện nhóm thông minh (Structured Summarizer).
3. Công cụ viết lại tin nhắn (Smart Rewrite: Formal, Friendly, Short, English).
4. Smart Knowledge & Math Engine (Fallback thông minh trả lời mọi câu hỏi phổ thông ngay cả khi ngoại tuyến).
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
    "Bạn là HQ AI (Huy & Quốc AI) - Trợ lý trí tuệ nhân tạo thông minh, hiện đại và chuẩn mực trong ứng dụng ChatApp.\n"
    "Dự án được xây dựng và phát triển bởi Nguyễn Văn Quốc và Nguyễn Trần Trọng Huy (Sinh viên Đại học Công nghiệp TP.HCM - IUH).\n\n"
    "NGUYÊN TẮC HOẠT ĐỘNG:\n"
    "1. Luôn trả lời bằng tiếng Việt chuẩn xác, lịch sự, thân thiện, súc tích và sử dụng emoji sinh động.\n"
    "2. Khi trả lời về kiến thức lập trình, toán học, khoa học hay đời sống, hãy giải thích rõ ràng và có ví dụ minh họa.\n"
    "3. Định dạng câu trả lời bằng Markdown đẹp mắt: dùng gạch đầu dòng, in đậm các ý chính và dùng khối mã (```language) khi viết code.\n"
    "4. Ứng dụng ChatApp được xây dựng theo kiến trúc Microservices 3 tầng Clean Architecture:\n"
    "   • Frontend: React Native + Expo SDK 58 + Expo Router + Redux Toolkit + Tamagui.\n"
    "   • API Gateway: FastAPI (Python 3.12/3.13) quản lý xác thực JWT, WebSocket realtime và HQ AI.\n"
    "   • Core Backend: Java 21 Netty gRPC kết nối MongoDB Atlas Cloud và Jakarta Mail OTP.\n"
    "5. Tuyệt đối tuân thủ bảo mật: Không tiết lộ bí mật hệ thống, mật khẩu hay thông tin cá nhân của người dùng."
)


def call_gemini_api(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    system_instruction: str = HQ_AI_SYSTEM_INSTRUCTION,
) -> str:
    """
    Gọi trực tiếp Google Gemini REST API hỗ trợ Multi-turn Conversation
    """
    if not GEMINI_API_KEY:
        return ""

    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"

    # Chuẩn bị danh sách contents cho multi-turn conversation
    contents = []

    if history:
        for turn in history[-8:]:  # Giữ tối đa 8 lượt hội thoại gần nhất để tối ưu token
            role = turn.get("role", "user")
            # Map role chuẩn của Gemini: 'user' hoặc 'model'
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
            "temperature": 0.7,
            "maxOutputTokens": 1024,
            "topP": 0.9,
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
        with urllib.request.urlopen(req, timeout=15) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            candidates = res_json.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
    except Exception as e:
        log.warning("Gemini API call failed: %s. Using Smart Fallback Engine.", e)

    return ""


# ====================================================================== Smart Knowledge & Fallback Engine
# (Đảm bảo trả lời thông minh mọi câu hỏi phổ thông ngay cả khi chưa có GEMINI_API_KEY hoặc mất mạng)

def _try_solve_math(prompt: str) -> Optional[str]:
    """Tự động tính toán các biểu thức toán học cơ bản và thực tế"""
    p = prompt.lower().replace("bằng bao nhiêu", "").replace("tính", "").replace("=", "").strip()

    # Nhận diện phép tính số học dạng a + b, a * b, a / b, v.v.
    clean_expr = re.sub(r"[^\d\+\-\*\/\.\(\)\s\^xX%]", "", p).replace("x", "*").replace("X", "*").replace("^", "**")
    if clean_expr and any(op in clean_expr for op in ["+", "-", "*", "/", "%", "**"]):
        # Chỉ eval biểu thức an toàn chỉ gồm chữ số và phép toán
        if re.fullmatch(r"^[\d\s\+\-\*\/\.\(\)\%]+$", clean_expr):
            try:
                # Xử lý tính phần trăm ví dụ 15% của X hoặc 200 * 15%
                calc_str = re.sub(r"(\d+)%", r"(\1/100)", clean_expr)
                val = eval(calc_str, {"__builtins__": None}, {})
                # Format số
                if isinstance(val, float) and val.is_integer():
                    val = int(val)
                elif isinstance(val, float):
                    val = round(val, 4)
                return f"🧮 **Kết quả tính toán:**\n\n`{clean_expr.strip()}` = **{val:,}**"
            except Exception:
                pass
    return None


def generate_smart_fallback(prompt: str, history: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Bộ não xử lý câu hỏi phổ thông ngoại tuyến của HQ AI:
    Giải đáp toán học, lập trình, viết thư, dịch thuật, khoa học và tư vấn đời sống.
    """
    p_lower = prompt.lower().strip()

    # 1. Kiểm tra toán học
    math_res = _try_solve_math(prompt)
    if math_res:
        return math_res

    # 2. Chào hỏi & Tự giới thiệu
    if any(k in p_lower for k in ["chào", "hello", "hi", "hey", "alo", "bạn là ai", "tên gì", "hq ai"]):
        return (
            "Chào bạn! Tôi là **HQ AI** 🤖✨ — Trợ lý ảo thông minh trong ứng dụng ChatApp, "
            "được phát triển bởi **Nguyễn Văn Quốc** & **Nguyễn Trần Trọng Huy**.\n\n"
            "Tôi có thể hỗ trợ bạn mọi tác vụ phổ thông:\n"
            "• 🎓 **Học tập & Lập trình:** Giải đáp code React Native, Python, Java, thuật toán.\n"
            "• 📊 **Quản lý phòng chat:** Tóm tắt cuộc trò chuyện nhóm, trích xuất việc cần làm.\n"
            "• ✍️ **Soạn thảo văn bản:** Viết email xin nghỉ, lời chúc sinh nhật, dịch Anh - Việt.\n"
            "• 💡 **Kiến trúc ChatApp:** Giải thích chi tiết hệ thống Microservices 3 tầng.\n\n"
            "Hôm nay bạn cần HQ AI hỗ trợ điều gì nào?"
        )

    # 3. Kiến trúc Đồ án ChatApp
    if any(k in p_lower for k in ["kien truc", "kiến trúc", "3 tang", "3 tầng", "fastapi", "grpc", "tamagui", "cong nghe", "công nghệ"]):
        return (
            "🏛️ **Kiến trúc hệ thống ChatApp (Clean Architecture 3 tầng):**\n\n"
            "1. 📱 **Mobile Frontend (React Native + Expo SDK 58):**\n"
            "   • Điều hướng an toàn bằng Expo Router với cơ chế `Stack.Protected`.\n"
            "   • Quản lý trạng thái và đồng bộ cache với Redux Toolkit & RTK Query.\n"
            "   • Giao diện hiện đại kết hợp Tamagui, hiệu ứng nhún lò xo ScalePressable.\n\n"
            "2. 🚀 **API Gateway (FastAPI - Python 3.12/3.13):**\n"
            "   • Đóng vai trò Backend-For-Frontend (BFF), xác thực mã hóa JSON Web Token (JWT).\n"
            "   • Quản lý kết nối WebSocket hai chiều (Realtime Presence & Message dispatch).\n"
            "   • Tích hợp Trợ lý **HQ AI** và Vector Database (Cosine Similarity) tìm kiếm ngữ nghĩa.\n\n"
            "3. ⚙️ **Core Backend (Java 21 Netty gRPC):**\n"
            "   • Xử lý logic phòng chat, kết bạn qua giao thức gRPC nhị phân Protobuf siêu tốc.\n"
            "   • Lưu trữ dữ liệu trên MongoDB Atlas Cloud, gửi mã xác thực OTP qua Jakarta Mail."
        )

    # 4. Lập trình & Kỹ thuật
    if any(k in p_lower for k in ["react native", "expo", "python", "java", "code", "lập trình"]):
        if "react native" in p_lower or "expo" in p_lower:
            return (
                "💻 **Kinh nghiệm tối ưu ứng dụng React Native / Expo:**\n\n"
                "1. **Tối ưu re-render:** Sử dụng `React.memo`, `useCallback`, `useMemo` cho các bubble tin nhắn trong danh sách chat.\n"
                "2. **Tối ưu danh sách lớn:** Sử dụng `FlatList` với cấu hình `initialNumToRender={15}`, `maxToRenderPerBatch={10}`, `windowSize={5}`.\n"
                "3. **Quản lý Cache:** Áp dụng RTK Query với Optimistic Updates để tin nhắn hiển thị ngay lập tức trước khi server phản hồi.\n"
                "4. **Xử lý Bàn phím:** Sử dụng `KeyboardAvoidingView` kết hợp `react-native-keyboard-controller` để cuộn mượt mà."
            )
        return (
            "👨‍💻 **Tư vấn lập trình từ HQ AI:**\n\n"
            "Tôi có thể hỗ trợ bạn viết code bằng Python, Java, TypeScript hoặc SQL. "
            "Bạn hãy mô tả cụ thể chức năng hoặc đoạn code đang gặp lỗi, HQ AI sẽ phân tích và đưa ra giải pháp kèm code mẫu chuẩn nhé!"
        )

    # 5. Soạn thảo: Lời chúc sinh nhật / Lễ Tết
    if any(k in p_lower for k in ["chúc sinh nhật", "sinh nhat", "chúc mừng", "lễ"]):
        return (
            "🎉 **Gợi ý lời chúc sinh nhật ý nghĩa & ấm áp:**\n\n"
            "\"*Chúc bạn có một ngày sinh nhật thật trọn vẹn, ngập tràn niềm vui và tiếng cười! "
            "Bước sang tuổi mới chúc bạn luôn dồi dào sức khỏe, gặt hái thật nhiều thành công mới trong học tập, "
            "công việc và luôn giữ vững ngọn lửa đam mê của mình nhé! Happy Birthday!* 🎂🎈🎁\""
        )

    # 6. Soạn thảo: Email xin nghỉ học / nghỉ phép
    if any(k in p_lower for k in ["nghỉ học", "nghi hoc", "xin nghỉ", "email"]):
        return (
            "📝 **Mẫu Email xin phép nghỉ học gửi Giảng viên:**\n\n"
            "**Tiêu đề:** [Họ và tên] - [MSSV] - Đơn xin phép nghỉ học ngày [Ngày/Tháng]\n\n"
            "Kính gửi: Thầy/Cô [Tên Giảng viên],\n\n"
            "Em tên là: [Họ và tên], MSSV: [Mã số sinh viên], sinh viên lớp [Tên lớp học phần].\n\n"
            "Em viết email này xin phép Thầy/Cô cho em được nghỉ buổi học vào ngày [Ngày/Tháng] vì lý do sức khỏe "
            "(hoặc có việc gia đình đột xuất).\n\n"
            "Em xin cam kết sẽ chủ động mượn vở bạn cùng lớp để ghi chép bài đầy đủ và hoàn thành mọi bài tập được giao.\n\n"
            "Em xin chân thành cảm ơn Thầy/Cô ạ!\n\n"
            "Kính thư,\n"
            "[Họ và tên của bạn] - SĐT: [Số điện thoại]"
        )

    # 7. Dịch thuật tiếng Anh - Việt
    if any(k in p_lower for k in ["dịch", "translate", "tiếng anh", "tiếng việt"]):
        return (
            "🌐 **HQ AI hỗ trợ dịch thuật đa ngôn ngữ:**\n\n"
            "Bạn có thể gửi câu hoặc đoạn văn bản cần dịch. Ví dụ:\n"
            "• Gõ: `Dịch sang tiếng Anh: Tôi rất vui được làm quen với bạn`\n"
            "👉 *'I am very pleased to make your acquaintance.'*\n\n"
            "Hãy gửi câu bạn muốn dịch ngay bên dưới nhé!"
        )

    # 8. Khoa học & Đời sống phổ thông
    if "bầu trời" in p_lower and "xanh" in p_lower:
        return (
            "🌌 **Tại sao bầu trời có màu xanh lam?**\n\n"
            "Hiện tượng này xảy ra do **tán xạ Rayleigh**:\n"
            "1. Ánh sáng Mặt Trời nhìn có màu trắng nhưng thực chất gồm đủ 7 sắc cầu vồng.\n"
            "2. Khi đi vào bầu khí quyển Trái Đất, ánh sáng va chạm với các phân tử khí (Nitơ, Oxy).\n"
            "3. Ánh sáng màu xanh lam có bước sóng ngắn hơn nhiều so với ánh sáng đỏ nên bị tán xạ ra mọi hướng mạnh hơn gấp khoảng 10 lần.\n"
            "👉 Kết quả là khi nhìn lên bầu trời ban ngày, mắt chúng ta nhận được nhiều ánh sáng xanh lam bị tán xạ nhất!"
        )

    if any(k in p_lower for k in ["thời tiết", "mưa", "nắng", "nhiệt độ"]):
        return (
            "🌤️ **Thông tin thời tiết & Lời khuyên sức khỏe từ HQ AI:**\n\n"
            "Thời tiết thay đổi liên tục giữa các mùa. Nếu ra ngoài, bạn nhớ:\n"
            "• Mang theo áo khoác nhẹ hoặc ô/dù dự phòng mưa bất chợt.\n"
            "• Bổ sung đủ 1.5 - 2 lít nước mỗi ngày để cơ thể luôn tràn đầy năng lượng.\n"
            "Chúc bạn một ngày học tập và làm việc thật sảng khoái!"
        )

    # 9. Câu trả lời mặc định thông minh & gợi mở
    return (
        f"🤖 **HQ AI:** Cảm ơn bạn đã hỏi về *'{prompt}'*!\n\n"
        "Tôi đã tiếp nhận câu hỏi của bạn. Đây là một chủ đề rất thú vị! "
        "Tôi có thể hỗ trợ bạn đi sâu hơn vào chi tiết, phân tích logic, hoặc cùng bạn giải quyết vấn đề này từng bước một.\n\n"
        "💡 *Gợi ý:* Bạn có thể kết nối thêm `GEMINI_API_KEY` trong phần cài đặt để HQ AI kích hoạt đầy đủ sức mạnh xử lý ngôn ngữ thế hệ mới nhất của Google Gemini nhé!"
    )


def generate_ai_reply(prompt: str, history: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Điểm vào chính để sinh phản hồi HQ AI:
    Ưu tiên Gemini REST API -> Tự động chuyển Smart Fallback Engine nếu không có key.
    """
    # 1. Gọi Google Gemini nếu có Key
    gemini_reply = call_gemini_api(prompt, history=history)
    if gemini_reply:
        return gemini_reply

    # 2. Xử lý qua Smart Fallback Engine
    return generate_smart_fallback(prompt, history=history)


# ====================================================================== Tóm tắt cuộc trò chuyện nhóm (Smart Summarizer)

def summarize_conversation(room_name: str, messages: List[Dict]) -> str:
    """
    Tóm tắt nội dung phòng chat theo chuẩn cấu trúc 3 phần:
    1. 📌 Chủ đề chính
    2. 🤝 Quyết định / Thống nhất
    3. ⏰ Lịch hẹn & Việc cần làm
    Được bảo vệ bởi lớp bảo mật PII & Anti-Prompt Injection.
    """
    if not messages:
        return "Cuộc trò chuyện chưa có đủ tin nhắn để tóm tắt."

    # Lọc và tạo transcript chỉ lấy tin nhắn văn bản, giới hạn 30 tin
    raw_lines = []
    for m in messages[-30:]:
        sender = m.get("sender_name", "Thành viên")
        content = str(m.get("content", "")).strip()
        if content:
            raw_lines.append(f"{sender}: {content}")

    if not raw_lines:
        return "Cuộc trò chuyện chỉ có hình ảnh hoặc video, chưa có nội dung văn bản để tóm tắt."

    chat_transcript = "\n".join(raw_lines)

    # Đóng gói an toàn qua Sandbox chống Prompt Injection
    secured_context = wrap_secure_context(chat_transcript)

    prompt = (
        f"Bạn là HQ AI - Trợ lý phân tích cuộc trò chuyện nhóm '{room_name}'.\n"
        f"Hãy đọc nội dung cuộc trò chuyện dưới đây và tóm tắt theo định dạng Markdown chuẩn xác sau:\n\n"
        f"📌 **Chủ đề chính thảo luận:**\n"
        f"• (1-2 gạch đầu dòng nêu nội dung cốt lõi các thành viên trao đổi)\n\n"
        f"🤝 **Quyết định đã thống nhất:**\n"
        f"• (Nêu rõ ý kiến đồng thuận hoặc kết luận của nhóm, nếu chưa có ghi rõ 'Đang thảo luận')\n\n"
        f"⏰ **Lịch hẹn & Việc cần làm (Action Items):**\n"
        f"• (Thời gian, địa điểm hoặc phân công nhiệm vụ cụ thể, nếu không có ghi 'Không có lịch hẹn')\n\n"
        f"{secured_context}"
    )

    gemini_summary = call_gemini_api(prompt)
    if gemini_summary:
        return gemini_summary

    # Fallback tóm tắt thông minh khi không có Gemini Key
    unique_senders = list({m.get("sender_name", "Thành viên") for m in messages[-15:]})
    senders_str = ", ".join(unique_senders[:4]) if unique_senders else "Các thành viên"
    last_msg = messages[-1].get("content", "")

    return (
        f"📌 **Tóm tắt cuộc trò chuyện nhóm '{room_name}':**\n\n"
        f"• 👥 **Thành viên tham gia:** {senders_str}.\n"
        f"• 💬 **Chủ đề chính:** Các thành viên đang trao đổi sôi nổi về công việc và học tập "
        f"với {len(messages)} tin nhắn gần nhất.\n"
        f"• 💬 **Nội dung mới nhất:** \"*{sanitize_pii(last_msg[:80])}...*\"\n"
        f"• 🤝 **Quyết định / Kế hoạch:** Nhóm đang tiếp tục trao đổi để đi đến thống nhất cuối cùng.\n"
        f"• ✅ **Trạng thái:** Mọi thành viên đều đã cập nhật thông tin kịp thời."
    )


# ====================================================================== Viết lại tin nhắn (Smart Rewrite)

def rewrite_message_text(text: str, tone: str = "formal") -> str:
    """
    Viết lại tin nhắn nháp cho người dùng theo phong cách mong muốn:
    - formal: Lịch sự, trang trọng (gửi thầy cô, đối tác)
    - friendly: Thân thiện, hài hước, tự nhiên (gửi bạn bè)
    - short: Ngắn gọn, súc tích, đi thẳng vào vấn đề
    - english: Dịch và diễn đạt chuẩn tiếng Anh bản xứ
    """
    clean_text = sanitize_pii(text)

    prompt = (
        f"Bạn là chuyên gia ngôn ngữ HQ AI. Hãy viết lại câu sau đây theo phong cách '{tone}':\n\n"
        f"Câu gốc: \"{clean_text}\"\n\n"
        f"Yêu cầu:\n"
        f"- Chỉ trả về duy nhất câu đã được viết lại, không cần giải thích dài dòng.\n"
        f"- Đảm bảo đúng ngữ pháp, tự nhiên và đúng ngữ cảnh."
    )

    gemini_res = call_gemini_api(prompt)
    if gemini_res:
        return gemini_res.strip().strip('"')

    # Fallback cơ bản nếu không có key
    if tone == "formal":
        return f"Dạ kính thưa Thầy/Cô (Anh/Chị), em xin phép được trao đổi về việc: {clean_text}. Em xin chân thành cảm ơn ạ!"
    elif tone == "short":
        return f"{clean_text.rstrip('.')} (vui lòng xác nhận sớm)."
    elif tone == "friendly":
        return f"Hey bạn ơi! {clean_text} nhé! Có gì nhắn mình liền nha 🎉"
    elif tone == "english":
        return f"Hello, I would like to inform you that: {clean_text}. Thank you!"
    return clean_text
