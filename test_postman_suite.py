import json
import os
import sys
import time
import urllib.error
import urllib.request

# Ensure we can import from local directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import create_token
from pymongo import MongoClient

BASE_URL = "http://127.0.0.1:8000"

# Generate test tokens
USER_ID = "test_user_unit_01"
OTHER_USER_ID = "test_user_unit_02"
ADMIN_USER_ID = "test_admin_unit_01"

user_token = create_token(USER_ID, role="USER")
other_token = create_token(OTHER_USER_ID, role="USER")
admin_token = create_token(ADMIN_USER_ID, role="ADMIN")

# Setup MongoDB test data if available
from dotenv import load_dotenv
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "chat_db")
mongo_db = None
if MONGO_URI:
    try:
        mc = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
        mongo_db = mc[MONGO_DB_NAME]
        # Seed test user & test admin roles with valid unique accounts
        mongo_db["users"].update_one(
            {"_id": ADMIN_USER_ID},
            {"$set": {"role": "ADMIN", "is_active": True, "full_name": "Test Admin Unit", "account": f"{ADMIN_USER_ID}@test.com"}},
            upsert=True
        )
        mongo_db["users"].update_one(
            {"_id": USER_ID},
            {"$set": {"role": "USER", "is_active": True, "full_name": "Test User Unit", "account": f"{USER_ID}@test.com"}},
            upsert=True
        )
    except Exception as e:
        print(f"[WARN] MongoDB not available for direct fixture injection: {e}")


def request(method, path, body=None, token=None):
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            content = resp.read().decode("utf-8")
            try:
                res_json = json.loads(content)
            except Exception:
                res_json = content
            return status, res_json
    except urllib.error.HTTPError as e:
        status = e.code
        err_content = e.read().decode("utf-8")
        try:
            res_json = json.loads(err_content)
        except Exception:
            res_json = err_content
        return status, res_json
    except Exception as e:
        return 0, str(e)


def run_tests():
    print("=" * 70)
    print("🚀 BẮT ĐẦU CHẠY BỘ KIỂM THỬ POSTMAN TỰ ĐỘNG CHO API GATEWAY")
    print("=" * 70)

    results = []
    def check(name, condition, details=""):
        status = "PASSED" if condition else "FAILED"
        icon = "✅" if condition else "❌"
        print(f"{icon} [{status}] {name} {details}")
        results.append((name, condition))

    # 1. Health check & Root
    st, res = request("GET", "/")
    check("Case 01: Health Check Root Endpoint", st == 200 and res.get("status") == "Running")

    # 2. OpenAPI Schema Check
    st, res = request("GET", "/openapi.json")
    check("Case 02: OpenAPI Schema Spec", st == 200 and "paths" in res and len(res["paths"]) >= 40, f"({len(res.get('paths', {}))} paths)")

    # 3. Security: Missing Auth Token
    st, res = request("GET", "/api/v1/users/me")
    check("Case 03: Auth Guard - Missing Token Rejection", st == 401, f"(Status: {st})")

    # 4. Security: Invalid Bearer Token
    st, res = request("GET", "/api/v1/users/me", token="invalid_jwt_token_123")
    check("Case 04: Auth Guard - Invalid Token Rejection", st == 401, f"(Status: {st})")

    # 5. Security: Valid User Token Identity
    st, res = request("GET", "/api/v1/users/me", token=user_token)
    check("Case 05: User Identity Verification", st == 200 and res.get("user_id") == USER_ID and not res.get("is_admin"))

    # 6. Security: Admin Token Identity
    st, res = request("GET", "/api/v1/users/me", token=admin_token)
    check("Case 06: Admin Identity Verification", st == 200 and res.get("is_admin") is True)

    # 7. RBAC: User blocked from Admin API
    st, res = request("GET", "/api/v1/admin/stats", token=user_token)
    check("Case 07: RBAC - User Forbidden from Admin Stats", st == 403, f"(Status: {st})")

    # 8. RBAC: Admin allowed to Admin API
    st, res = request("GET", "/api/v1/admin/stats", token=admin_token)
    check("Case 08: RBAC - Admin Access to Admin Stats", st == 200 and "total_users" in res)

    # 9. AI: Translate Message (Vietnamese)
    st, res = request("POST", "/api/v1/ai/translate", body={"text": "Hello, how are you doing today?", "target_lang": "vi"}, token=user_token)
    check("Case 09: AI Translate English -> Vietnamese", st == 200 and bool(res.get("translated_text")), f"('{res.get('translated_text')}')")

    # 10. AI: Translate Validation (Empty text)
    st, res = request("POST", "/api/v1/ai/translate", body={"text": "", "target_lang": "vi"}, token=user_token)
    check("Case 10: AI Translate Validation - Rejects Empty Text", st == 422, f"(Status: {st})")

    # 11. AI: Rewrite Message (Formal)
    st, res = request("POST", "/api/v1/ai/rewrite", body={"text": "mai tui nghi hoc nha", "tone": "formal"}, token=user_token)
    check("Case 11: AI Rewrite Tone Formal", st == 200 and "rewritten" in res, f"('{res.get('rewritten')}')")

    # 12. AI: Rewrite Message (English)
    st, res = request("POST", "/api/v1/ai/rewrite", body={"text": "chieu nay hop 3h", "tone": "english"}, token=user_token)
    check("Case 12: AI Rewrite Tone English", st == 200 and "rewritten" in res, f"('{res.get('rewritten')}')")

    # 13. AI: HQ AI Chat Assistant Query
    st, res = request("POST", "/api/v1/ai/chat", body={"prompt": "1 + 1 bằng mấy?"}, token=user_token)
    check("Case 13: HQ AI Chat Basic Computation Query", st == 200 and "2" in res.get("reply", ""))

    # 14. Group Polls: Create Poll Validation (<2 options rejected)
    st, res = request("POST", "/api/v1/rooms/test_room_1/polls", body={"question": "Ăn gì?", "options": ["Cơm"]}, token=user_token)
    check("Case 14: Group Polls - Rejects Less than 2 Options", st == 400, f"(Status: {st})")

    # 15. Group Polls: Create Poll Success
    st, res = request("POST", "/api/v1/rooms/test_room_1/polls", body={
        "question": "Trưa nay ăn gì các bạn?",
        "options": ["Phở bò Hà Nội", "Cơm tấm sườn bì chả", "Bún chả"],
        "is_multiple_choice": False,
        "is_anonymous": False
    }, token=user_token)
    poll_id = res.get("poll_id") if st == 200 else None
    check("Case 15: Group Polls - Create Poll Success", st == 200 and poll_id is not None, f"(PollId: {poll_id})")

    if poll_id:
        # 16. Group Polls: Get Poll Details
        st, res = request("GET", f"/api/v1/chat/polls/{poll_id}", token=user_token)
        check("Case 16: Group Polls - Get Poll Details", st == 200 and res.get("poll", {}).get("total_votes") == 0)

        # 17. Group Polls: Vote for option "opt_0"
        st, res = request("POST", f"/api/v1/chat/polls/{poll_id}/vote", body={"option_id": "opt_0"}, token=user_token)
        voted_poll = res.get("poll", {})
        check("Case 17: Group Polls - Vote For Option (opt_0)", st == 200 and voted_poll.get("total_votes") == 1 and voted_poll["options"][0]["percentage"] == 100.0)

        # 18. Group Polls: Toggle Vote (Unvote)
        st, res = request("POST", f"/api/v1/chat/polls/{poll_id}/vote", body={"option_id": "opt_0"}, token=user_token)
        unvoted_poll = res.get("poll", {})
        check("Case 18: Group Polls - Toggle Vote to Unvote", st == 200 and unvoted_poll.get("total_votes") == 0 and unvoted_poll["options"][0]["percentage"] == 0.0)

    # 19. Message Editing: Seed a test message in Mongo
    test_msg_id = f"test_msg_{int(time.time()*1000)}"
    if mongo_db is not None:
        mongo_db["messages"].insert_one({
            "_id": test_msg_id,
            "room_id": "test_room_edit",
            "sender_id": USER_ID,
            "content": "Nội dung ban đầu trước khi sửa",
            "type": "TEXT",
            "timestamp": int(time.time() * 1000),
            "is_revoked": False,
        })

    # 20. Message Editing: Edit by Sender within 30 min
    st, res = request("PUT", f"/api/v1/rooms/test_room_edit/messages/{test_msg_id}", body={"content": "Nội dung đã được chỉnh sửa chuẩn xác!"}, token=user_token)
    check("Case 19: Message Editing - Edit By Sender Allowed", st == 200 and res.get("is_edited") is True)

    # 21. Message Editing: Security - Edit by other user forbidden
    st, res = request("PUT", f"/api/v1/rooms/test_room_edit/messages/{test_msg_id}", body={"content": "Hacker cố tình sửa"}, token=other_token)
    check("Case 20: Message Editing - Security - Other User Forbidden", st == 403, f"(Status: {st})")

    # 22. Starred / Saved Messages: Toggle Star
    st, res = request("POST", f"/api/v1/chat/messages/{test_msg_id}/star", token=user_token)
    check("Case 21: Starred Messages - Toggle Star (Saved)", st == 200 and res.get("starred") is True)

    # 23. Starred / Saved Messages: Get Starred List
    st, res = request("GET", "/api/v1/chat/starred", token=user_token)
    has_starred = any(m.get("message_id") == test_msg_id for m in res.get("messages", []))
    check("Case 22: Starred Messages - Retrieve Starred List", st == 200 and has_starred)

    # 24. Starred / Saved Messages: Toggle Unstar
    st, res = request("POST", f"/api/v1/chat/messages/{test_msg_id}/star", token=user_token)
    check("Case 23: Starred Messages - Toggle Unstar (Removed)", st == 200 and res.get("starred") is False)

    # Clean up test message & users
    if mongo_db is not None:
        mongo_db["messages"].delete_one({"_id": test_msg_id})
        mongo_db["starred_messages"].delete_one({"_id": f"{USER_ID}_{test_msg_id}"})
        mongo_db["users"].delete_one({"_id": ADMIN_USER_ID})
        mongo_db["users"].delete_one({"_id": USER_ID})
        if poll_id:
            mongo_db["polls"].delete_one({"_id": poll_id})

    # 25. Online Users list
    st, res = request("GET", "/api/v1/users/online", token=user_token)
    check("Case 24: User Presence - Online Users Endpoint", st == 200 and "online_user_ids" in res)

    print("=" * 70)
    passed_count = sum(1 for _, ok in results if ok)
    total_count = len(results)
    print(f"📊 TỔNG KẾT: {passed_count}/{total_count} CASES PASSED ({round(passed_count/total_count*100, 1)}%)")
    print("=" * 70)
    return passed_count == total_count

if __name__ == "__main__":
    run_tests()
