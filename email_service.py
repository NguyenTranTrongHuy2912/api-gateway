import os
import time
import random
import hmac
import hashlib
import smtplib
import logging
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from fastapi import HTTPException

log = logging.getLogger("gateway.email")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "chatapp.support.noreply@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "mwrcjfejkbwyrwwd")
SMTP_FROM = os.getenv("SMTP_FROM", f"HQ ChatApp Security <{SMTP_USER}>")
OTP_SECRET = os.getenv("OTP_SECRET", "00204170e50fa7a68bdfd76260629b2ce95f55c023d2166138f679c85270193c").encode("utf-8")

TTL_SECONDS = 300  # 5 phút
RESEND_SECONDS = 60 # 60 giây
MAX_ATTEMPTS = 5


def hash_otp_code(key: str, code: str) -> str:
    """Băm mã OTP theo chuẩn HMAC-SHA256 tương thích 100% với OtpService Java Backend."""
    msg = f"{key}:{code}".encode("utf-8")
    return hmac.new(OTP_SECRET, msg, hashlib.sha256).hexdigest()


def send_smtp_email(to_email: str, subject: str, html_body: str) -> bool:
    """Gửi email HTML thật qua Gmail SMTP server."""
    if not SMTP_USER or not SMTP_PASSWORD:
        log.warning("SMTP chưa được cấu hình đầy đủ. Bỏ qua gửi email thực tế.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email

        part = MIMEText(html_body, "html", "utf-8")
        msg.attach(part)

        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=12)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())
        server.quit()
        log.info("Đã gửi email OTP thành công tới %s", to_email)
        return True
    except Exception as e:
        log.error("Gửi email SMTP tới %s thất bại: %s", to_email, e)
        return False


def build_otp_html(code: str, purpose: str = "register", display_name: str = "") -> str:
    """Tạo mẫu email HTML đẹp mắt, chuyên nghiệp chuẩn thương mại."""
    title_text = "Mã Xác Thực Đăng Ký Tài Khoản" if purpose == "register" else "Mã Đặt Lại Mật Khẩu"
    greeting = f"Xin chào <strong>{display_name}</strong>," if display_name else "Xin chào bạn,"
    desc_text = (
        "Bạn (hoặc Quản trị viên) vừa yêu cầu tạo tài khoản trên hệ thống <strong>HQ ChatApp</strong>."
        if purpose == "register"
        else "Bạn vừa yêu cầu đặt lại mật khẩu cho tài khoản của mình."
    )

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; background-color: #f4f6f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="table-layout: fixed;">
        <tr>
            <td align="center" style="padding: 40px 10px;">
                <table border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 520px; background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.06); border: 1px solid #e2e8f0;">
                    <!-- Header -->
                    <tr>
                        <td align="center" style="padding: 32px 24px 20px; background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%);">
                            <table border="0" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td align="center" style="width: 48px; height: 48px; background-color: rgba(255,255,255,0.2); border-radius: 12px;">
                                        <span style="font-size: 24px; color: #ffffff;">🔒</span>
                                    </td>
                                </tr>
                            </table>
                            <h1 style="color: #ffffff; font-size: 22px; font-weight: 800; margin: 16px 0 4px; letter-spacing: -0.3px;">HQ ChatApp Security</h1>
                            <p style="color: #bfdbfe; font-size: 13px; margin: 0; font-weight: 500;">Hệ thống xác thực tài khoản an toàn</p>
                        </td>
                    </tr>
                    <!-- Body Content -->
                    <tr>
                        <td style="padding: 32px 28px 24px;">
                            <p style="font-size: 15px; color: #1e293b; line-height: 24px; margin: 0 0 12px;">{greeting}</p>
                            <p style="font-size: 14px; color: #475569; line-height: 22px; margin: 0 0 24px;">
                                {desc_text} Dưới đây là mã xác thực OTP dùng một lần của bạn:
                            </p>
                            
                            <!-- OTP Box -->
                            <div style="background-color: #f8fafc; border: 2px dashed #cbd5e1; border-radius: 12px; padding: 20px; text-align: center; margin-bottom: 24px;">
                                <div style="font-size: 32px; font-weight: 800; letter-spacing: 8px; color: #2563EB; font-family: monospace;">
                                    {code}
                                </div>
                                <p style="font-size: 12px; color: #64748b; margin: 8px 0 0;">(Mã có hiệu lực trong vòng <strong>5 phút</strong>)</p>
                            </div>

                            <p style="font-size: 13px; color: #64748b; line-height: 20px; margin: 0 0 16px;">
                                ⚠️ <strong>Lưu ý bảo mật:</strong> Tuyệt đối không cung cấp mã xác thực này cho bất kỳ ai. Nhân viên hỗ trợ sẽ không bao giờ yêu cầu mã OTP của bạn.
                            </p>
                            <p style="font-size: 13px; color: #94a3b8; line-height: 20px; margin: 0;">
                                Nếu bạn không thực hiện yêu cầu này, vui lòng bỏ qua email này hoặc liên hệ hỗ trợ để khóa tài khoản kịp thời.
                            </p>
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td style="padding: 20px 28px; background-color: #f8fafc; border-top: 1px solid #f1f5f9; text-align: center;">
                            <p style="font-size: 12px; color: #94a3b8; margin: 0;">
                                © 2026 HQ ChatApp Messenger. Toàn quyền bảo lưu.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


def generate_and_save_otp(mongo_db, email: str, purpose: str = "register", display_name: str = "") -> dict:
    """
    Sinh mã OTP, băm HMAC-SHA256, lưu vào MongoDB và gửi email thật.
    Đồng bộ trực tiếp với schema otp_codes của Java Backend.
    """
    clean_email = email.strip().lower()
    key = f"{purpose}:{clean_email}"
    now_ms = int(time.time() * 1000)

    if mongo_db is not None:
        old_doc = mongo_db["otp_codes"].find_one({"_id": key})
        if old_doc:
            last_sent = old_doc.get("last_sent_at", 0)
            if now_ms - last_sent < RESEND_SECONDS * 1000:
                wait_sec = RESEND_SECONDS - (now_ms - last_sent) // 1000
                raise HTTPException(
                    status_code=429,
                    detail=f"Vui lòng đợi {wait_sec} giây trước khi yêu cầu gửi lại mã OTP."
                )

    code = f"{random.randint(100000, 999999):06d}"
    code_hash = hash_otp_code(key, code)
    expires_dt = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)

    if mongo_db is not None:
        mongo_db["otp_codes"].replace_one(
            {"_id": key},
            {
                "_id": key,
                "code_hash": code_hash,
                "attempts": 0,
                "last_sent_at": now_ms,
                "expires_at": expires_dt,
            },
            upsert=True,
        )

    # Gửi email thật qua SMTP
    subject = "HQ ChatApp - Mã xác thực đăng ký tài khoản" if purpose == "register" else "HQ ChatApp - Đặt lại mật khẩu"
    html_content = build_otp_html(code, purpose, display_name)
    send_smtp_email(clean_email, subject, html_content)

    return {
        "expires_in_seconds": TTL_SECONDS,
        "resend_after_seconds": RESEND_SECONDS,
        "email": clean_email,
        "message": f"Mã xác thực OTP đã được gửi đến {clean_email}. Vui lòng kiểm tra hộp thư đến hoặc mục thư rác (Spam).",
    }


def verify_otp_code(mongo_db, email: str, code: str, purpose: str = "register") -> bool:
    """Xác thực mã OTP từ MongoDB. Đúng thì xóa mã ngay lập tức (dùng 1 lần)."""
    clean_email = email.strip().lower()
    clean_code = code.strip()
    key = f"{purpose}:{clean_email}"

    if mongo_db is None:
        return True

    doc = mongo_db["otp_codes"].find_one_and_update(
        {"_id": key},
        {"$inc": {"attempts": 1}},
        return_document=True,
    )

    now_utc = datetime.now(timezone.utc)
    if not doc:
        raise HTTPException(
            status_code=400,
            detail="Mã xác thực OTP không tồn tại hoặc đã hết hạn. Vui lòng yêu cầu mã mới."
        )

    exp_at = doc.get("expires_at")
    if exp_at:
        # Chuyển về UTC nếu chưa có tz
        if exp_at.tzinfo is None:
            exp_at = exp_at.replace(tzinfo=timezone.utc)
        if exp_at < now_utc:
            mongo_db["otp_codes"].delete_one({"_id": key})
            raise HTTPException(
                status_code=400,
                detail="Mã xác thực OTP đã hết hạn (quá 5 phút). Vui lòng yêu cầu mã mới."
            )

    if doc.get("attempts", 0) > MAX_ATTEMPTS:
        mongo_db["otp_codes"].delete_one({"_id": key})
        raise HTTPException(
            status_code=400,
            detail="Bạn đã nhập sai mã OTP quá 5 lần. Mã đã bị hủy để đảm bảo an toàn, vui lòng yêu cầu mã mới."
        )

    expected_hash = hash_otp_code(key, clean_code)
    if doc.get("code_hash") != expected_hash:
        raise HTTPException(
            status_code=400,
            detail="Mã xác thực OTP không chính xác. Vui lòng kiểm tra lại email của bạn."
        )

    # Đúng mã -> Xóa để dùng một lần duy nhất
    mongo_db["otp_codes"].delete_one({"_id": key})
    return True
