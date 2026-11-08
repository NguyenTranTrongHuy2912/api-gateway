import hashlib
import json
import logging
import math
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger("gateway.vectordb")

DIM = 128
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()


def calculate_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """
    Thuật toán tính độ tương đồng Cosine giữa 2 vector:
    cos(theta) = (v1 . v2) / (||v1|| * ||v2||)
    """
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 < 1e-9 or norm2 < 1e-9:
        return 0.0
    return float(dot / (norm1 * norm2))


def generate_local_embedding(text: str) -> list[float]:
    """
    Thuật toán Vector Space Model (TF-IDF + Subword Hashing)
    Biến đổi chuỗi văn bản thành Dense Vector 128 chiều có chuẩn L2 = 1.0.
    """
    tokens = re.findall(r"\w+", text.lower())
    vec = [0.0] * DIM
    if not tokens:
        return vec

    for tok in tokens:
        # 1. Word-level hashing
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % DIM
        sign = 1.0 if (h >> 8) & 1 else -1.0
        vec[idx] += sign * 1.5

        # 2. Subword 3-gram hashing (giúp tìm kiếm từ đồng nghĩa, từ viết tắt, tiếng Việt không dấu)
        if len(tok) >= 3:
            for i in range(len(tok) - 2):
                tri = tok[i : i + 3]
                ht = int(hashlib.md5(tri.encode("utf-8")).hexdigest(), 16)
                it = ht % DIM
                st = 1.0 if (ht >> 8) & 1 else -1.0
                vec[it] += st * 0.8

    # Chuẩn hóa L2 Norm: ||v|| = 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 1e-9:
        vec = [float(x / norm) for x in vec]
    return vec


def generate_embedding(text: str) -> list[float]:
    """
    Tạo vector nhúng (embedding) cho văn bản.
    Tự động thử Google Gemini text-embedding-004; nếu không có key thì dùng thuật toán Vector Space Model cục bộ.
    """
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_API_KEY}"
            payload = {
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": text[:1000]}]},
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res = json.loads(response.read().decode("utf-8"))
                values = res.get("embedding", {}).get("values", [])
                if values:
                    # Trả về 128 chiều đầu tiên hoặc chuẩn hóa
                    step = max(1, len(values) // DIM)
                    compressed = [values[i * step] for i in range(DIM)]
                    norm = math.sqrt(sum(x * x for x in compressed))
                    return [float(x / norm) for x in compressed] if norm > 0 else compressed
        except Exception as e:
            log.warning("Gemini embedding failed: %s, using local vectorizer", e)

    return generate_local_embedding(text)


class MessageVectorDB:
    """
    Cơ sở dữ liệu Vector Database lưu trữ và tìm kiếm ngữ nghĩa tin nhắn (Vector Semantic Search).
    """

    def __init__(self, storage_path: str = "vector_store.json"):
        self.storage_path = storage_path
        self.documents: dict[str, dict] = {}
        self.load()

    def load(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.documents = json.load(f)
            except Exception as e:
                log.warning("Could not load vector store: %s", e)

    def save(self):
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.documents, f, ensure_ascii=False)
        except Exception as e:
            log.warning("Could not save vector store: %s", e)

    def count(self) -> int:
        return len(self.documents)

    def upsert(self, doc_id: str, text: str, metadata: dict) -> list[float]:
        """Index một tin nhắn vào Vector Database"""
        if not text.strip():
            return []
        vec = generate_embedding(text)
        self.documents[doc_id] = {
            "id": doc_id,
            "text": text,
            "vector": vec,
            "metadata": metadata,
        }
        self.save()
        return vec

    def index_messages(self, messages: list[dict]):
        """Index hàng loạt tin nhắn vào Vector Database"""
        changed = False
        for m in messages:
            msg_id = m.get("id") or m.get("message_id")
            content = m.get("content", "").strip()
            if not msg_id or not content:
                continue
            if msg_id not in self.documents:
                vec = generate_embedding(content)
                self.documents[msg_id] = {
                    "id": msg_id,
                    "text": content,
                    "vector": vec,
                    "metadata": m,
                }
                changed = True
        if changed:
            self.save()

    def search(
        self, query: str, top_k: int = 5, room_id: str = None, min_score: float = 0.05
    ) -> list[dict]:
        """
        Tìm kiếm ngữ nghĩa bằng thuật toán Cosine Similarity K-Nearest Neighbors (K-NN).
        """
        if not query.strip() or not self.documents:
            return []

        query_vec = generate_embedding(query)
        scored_results = []

        for doc in self.documents.values():
            meta = doc.get("metadata", {})
            if room_id and meta.get("room_id") != room_id:
                continue

            sim = calculate_cosine_similarity(query_vec, doc["vector"])
            if sim >= min_score:
                scored_results.append(
                    {
                        "message_id": doc["id"],
                        "content": doc["text"],
                        "sender_id": meta.get("sender_id", ""),
                        "sender_name": meta.get("sender_name", "Thành viên"),
                        "timestamp": meta.get("timestamp", 0),
                        "similarity_score": round(float(sim), 4),
                        "match_percent": int(max(0.0, min(1.0, (sim + 1.0) / 2.0)) * 100),
                    }
                )

        # Sắp xếp giảm dần theo điểm tương đồng cosine
        scored_results.sort(key=lambda x: x["similarity_score"], reverse=True)
        return scored_results[:top_k]

    def rag_chat(
        self, question: str, room_id: str, room_name: str = "", messages: list[dict] = None
    ) -> dict:
        """
        Retrieval-Augmented Generation (RAG):
        1. Tìm kiếm các tin nhắn liên quan nhất bằng Vector Similarity.
        2. Ghép vào Prompt làm Context cho AI.
        3. Sinh câu trả lời có bằng chứng xác thực từ lịch sử chat.
        """
        if messages:
            self.index_messages(messages)

        matched_docs = self.search(question, top_k=4, room_id=room_id, min_score=0.1)

        if not matched_docs:
            context_str = "Không tìm thấy tin nhắn liên quan trực tiếp trong phòng chat."
        else:
            context_str = "\n".join(
                [
                    f"- [{m['sender_name']}]: \"{m['content']}\" (Độ khớp: {m['similarity_score']})"
                    for m in matched_docs
                ]
            )

        rag_prompt = (
            f"Bạn là Trợ lý AI của ChatApp. Dưới đây là các tin nhắn được trích xuất từ Vector Database "
            f"của phòng chat '{room_name}':\n\n"
            f"[NGỮ CẢNH TỪ VECTOR DATABASE]:\n{context_str}\n\n"
            f"[CÂU HỎI CỦA NGƯỜI DÙNG]:\n{question}\n\n"
            f"Hãy trả lời câu hỏi trên dựa trên các tin nhắn trên. Nếu có thông tin, hãy trích dẫn rõ người nói. "
            f"Nếu không có trong ngữ cảnh, hãy nói rõ một cách lịch sự."
        )

        from ai_service import call_gemini_api

        ai_reply = call_gemini_api(rag_prompt)
        if not ai_reply:
            if matched_docs:
                top_hit = matched_docs[0]
                ai_reply = (
                    f"Dựa trên dữ liệu Vector Database tìm được trong phòng chat '{room_name}':\n\n"
                    f"📌 Tin nhắn liên quan nhất từ {top_hit['sender_name']}: \"{top_hit['content']}\" "
                    f"(Độ khớp cosine: {top_hit['similarity_score']})."
                )
            else:
                ai_reply = (
                    f"Vector Database đã quét toàn bộ tin nhắn trong phòng '{room_name}' "
                    f"nhưng chưa tìm thấy nội dung phù hợp với câu hỏi '{question}'."
                )

        return {
            "answer": ai_reply,
            "matched_messages": matched_docs,
            "total_matches": len(matched_docs),
            "algorithm": "Cosine Similarity + Dense Vector Space Model",
        }


# Global instance
vector_db = MessageVectorDB()
