#!/usr/bin/env python3
"""WashTrack backend server — robust, zero-dependency Python REST API and static server.
Serves frontend assets, handles authentication, orders, token tracking, complaints, OTP verification, and re-wash lifecycle.
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import smtplib
import ssl
import sys
import threading
import time
import traceback
from datetime import datetime
from email.message import EmailMessage
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# Ensure proper MIME types for web assets on Windows
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("application/json", ".json")

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
EMAIL_CONFIG_FILE = ROOT / "email_config.json"
HOST = os.environ.get("HOST", "0.0.0.0")

# Support custom port via CLI argument or environment variable (default 5000)
try:
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 5000))
except (ValueError, IndexError):
    PORT = 5000

STATUS_STEPS = ["Submitted", "In Wash", "Ready for Pickup", "Delivered"]
LOCK = threading.Lock()
# In-memory store for email verification codes for credential changes
# Key: studentRegNo or collegeEmail -> {"code": "123456", "email": "...", "expires_at": float}
EMAIL_OTP_STORE: dict[str, dict] = {}


def load_email_config() -> dict:
    if EMAIL_CONFIG_FILE.exists():
        try:
            with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "enabled": False,
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", 587)),
        "sender_email": os.environ.get("SMTP_USER", os.environ.get("SENDER_EMAIL", "")),
        "sender_password": os.environ.get("SMTP_PASS", os.environ.get("SENDER_PASSWORD", "")),
        "use_tls": True,
    }


def save_email_config(cfg: dict) -> None:
    with open(EMAIL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def send_order_notification_email(order: dict, student: dict, event: str) -> tuple[bool, str]:
    """Send a notification email when an order is submitted or ready for pickup.

    event: "submitted" | "ready_for_pickup"
    """
    college_email = (
        student.get("collegeEmail")
        or student.get("email")
        or f"{student.get('regNo', 'student').lower()}@campus.edu"
    ).strip().lower()

    student_name = student.get("name", "Student")
    token = order.get("token", "N/A")
    clothes = order.get("clothesDetails", "N/A")
    qty = order.get("totalQuantity", 0)
    hostel = order.get("hostelBlock", "N/A")
    room = order.get("roomNo", "N/A")
    submitted_at = order.get("submissionDateTime", "N/A")
    ironing = "Yes" if order.get("ironingRequested") else "No"

    if event == "submitted":
        subject = f"WashTrack – Laundry Ticket {token} Confirmed"
        heading = "Your Laundry Has Been Submitted!"
        color = "#4f46e5"
        icon = "🧺"
        body_text = (
            f"Hello {student_name},\n\n"
            f"Your laundry has been successfully submitted.\n\n"
            f"Token     : {token}\n"
            f"Items     : {clothes}\n"
            f"Quantity  : {qty}\n"
            f"Ironing   : {ironing}\n"
            f"Hostel    : {hostel}, Room {room}\n"
            f"Submitted : {submitted_at}\n\n"
            f"We will notify you again when your clothes are Ready for Pickup.\n\n"
            f"WashTrack Campus Laundry Team"
        )
        status_badge = "Submitted"
        badge_color = "#6366f1"
        extra_note = "We'll send you another email when your clothes are ready for pickup."
        detail_rows = f"""
            <tr><td style="color:#64748b;padding:4px 0;">Token</td><td style="font-weight:600;padding:4px 0;">{token}</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Items</td><td style="padding:4px 0;">{clothes}</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Quantity</td><td style="padding:4px 0;">{qty} item(s)</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Ironing</td><td style="padding:4px 0;">{ironing}</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Hostel / Room</td><td style="padding:4px 0;">{hostel} / Room {room}</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Submitted At</td><td style="padding:4px 0;">{submitted_at}</td></tr>
        """
    else:  # ready_for_pickup
        subject = f"WashTrack – Your Clothes Are Ready! 🎉 ({token})"
        heading = "Your Laundry is Ready for Pickup!"
        color = "#16a34a"
        icon = "✅"
        body_text = (
            f"Hello {student_name},\n\n"
            f"Great news! Your laundry is ready for pickup.\n\n"
            f"Token     : {token}\n"
            f"Items     : {clothes}\n"
            f"Quantity  : {qty}\n"
            f"Hostel    : {hostel}, Room {room}\n\n"
            f"Please collect your clothes from the laundry counter. "
            f"You will need your token number for handover.\n\n"
            f"WashTrack Campus Laundry Team"
        )
        status_badge = "Ready for Pickup"
        badge_color = "#16a34a"
        extra_note = "Please bring your token number to the laundry counter to collect your clothes."
        detail_rows = f"""
            <tr><td style="color:#64748b;padding:4px 0;">Token</td><td style="font-weight:600;padding:4px 0;">{token}</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Items</td><td style="padding:4px 0;">{clothes}</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Quantity</td><td style="padding:4px 0;">{qty} item(s)</td></tr>
            <tr><td style="color:#64748b;padding:4px 0;">Hostel / Room</td><td style="padding:4px 0;">{hostel} / Room {room}</td></tr>
        """

    html_content = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f8fafc;padding:20px;color:#1e293b;margin:0;">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;box-shadow:0 4px 6px rgba(0,0,0,0.05);">
    <div style="background:linear-gradient(135deg,{color} 0%,#3b82f6 100%);padding:24px;text-align:center;color:white;">
      <div style="font-size:36px;margin-bottom:8px;">{icon}</div>
      <h1 style="margin:0;font-size:20px;font-weight:bold;">{heading}</h1>
      <p style="margin:4px 0 0;font-size:13px;opacity:0.9;">WashTrack Campus Laundry Management</p>
    </div>
    <div style="padding:24px;">
      <p style="font-size:15px;margin-top:0;">Hello <strong>{student_name}</strong>,</p>
      <div style="text-align:center;margin:16px 0;">
        <span style="display:inline-block;background:{badge_color};color:white;border-radius:999px;padding:6px 18px;font-size:13px;font-weight:600;">
          Status: {status_badge}
        </span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0;">
        {detail_rows}
      </table>
      <div style="background:#f1f5f9;border-radius:10px;padding:14px;border-left:4px solid {color};margin:16px 0;">
        <p style="margin:0;font-size:13px;color:#475569;">{extra_note}</p>
      </div>
      <hr style="border:0;border-top:1px solid #e2e8f0;margin:20px 0;">
      <p style="font-size:11px;color:#94a3b8;text-align:center;margin:0;">
        This is an automated message from WashTrack. Please do not reply to this email.
      </p>
    </div>
  </div>
</body>
</html>"""

    config = load_email_config()
    sender = (config.get("sender_email") or "").strip()
    pwd = (config.get("sender_password") or "").strip()
    if not sender or not pwd or not config.get("enabled"):
        print(f"[WashTrack Email] SMTP not configured – skipping notification for {event} ({token})")
        return False, "SMTP not configured"

    from email.message import EmailMessage as _EM
    import smtplib as _smtp, ssl as _ssl
    msg = _EM()
    msg["Subject"] = subject
    msg["From"] = f"WashTrack Laundry <{sender}>"
    msg["To"] = college_email
    msg.set_content(body_text)
    msg.add_alternative(html_content, subtype="html")
    try:
        host = config.get("smtp_host", "smtp.gmail.com")
        port = int(config.get("smtp_port", 587))
        ctx = _ssl.create_default_context()
        if port == 465:
            with _smtp.SMTP_SSL(host, port, context=ctx, timeout=12) as srv:
                srv.login(sender, pwd)
                srv.send_message(msg)
        else:
            with _smtp.SMTP(host, port, timeout=12) as srv:
                srv.ehlo()
                if config.get("use_tls", True):
                    srv.starttls(context=ctx)
                    srv.ehlo()
                srv.login(sender, pwd)
                srv.send_message(msg)
        print(f"[WashTrack Email] ✓ Notification sent to {college_email} [{event}]")
        return True, "sent"
    except Exception as exc:
        print(f"[WashTrack Email] ✗ Failed to send notification [{event}]: {exc}")
        return False, str(exc)


def send_real_email(recipient: str, subject: str, code: str, student_name: str = "Student") -> tuple[bool, str]:
    config = load_email_config()
    sender = (config.get("sender_email") or "").strip()
    pwd = (config.get("sender_password") or "").strip()
    if not sender or not pwd:
        return False, "SMTP sender credentials not configured. Please enter your Gmail/SMTP credentials in Email Settings."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"WashTrack Laundry <{sender}>"
    msg["To"] = recipient

    plain_text = f"""Hello {student_name},

Your WashTrack security verification code is: {code}

This code will expire in 10 minutes. If you did not request this code, please ignore this email.

Best regards,
WashTrack Campus Laundry Team
"""
    html_content = f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; background-color: #f8fafc; padding: 20px; color: #1e293b; margin: 0;">
  <div style="max-width: 500px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
    <div style="background: linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%); padding: 24px; text-align: center; color: white;">
      <h1 style="margin: 0; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;">WashTrack Laundry Service</h1>
      <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">Campus Laundry Management System</p>
    </div>
    <div style="padding: 24px;">
      <p style="font-size: 15px; margin-top: 0; color: #334155;">Hello <strong>{student_name}</strong>,</p>
      <p style="font-size: 14px; color: #475569; line-height: 1.5;">You requested a verification code to authorize a password change for your WashTrack student portal account.</p>
      
      <div style="text-align: center; margin: 24px 0; background: #f1f5f9; border-radius: 12px; padding: 18px; border: 1px dashed #cbd5e1;">
        <span style="font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: bold; letter-spacing: 1px; display: block; margin-bottom: 6px;">Your 6-Digit Verification Code</span>
        <span style="font-size: 32px; font-weight: 800; letter-spacing: 6px; color: #4338ca; font-family: monospace;">{code}</span>
      </div>

      <p style="font-size: 12px; color: #64748b; line-height: 1.4;">This verification code is valid for <strong>10 minutes</strong>. Never share this code with anyone.</p>
      <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 20px 0;">
      <p style="font-size: 11px; color: #94a3b8; text-align: center; margin: 0;">If you didn't request this code, you can safely ignore this email.</p>
    </div>
  </div>
</body>
</html>
"""
    msg.set_content(plain_text)
    msg.add_alternative(html_content, subtype="html")

    try:
        host = config.get("smtp_host", "smtp.gmail.com")
        port = int(config.get("smtp_port", 587))
        context = ssl.create_default_context()

        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=12) as server:
                server.login(sender, pwd)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=12) as server:
                server.ehlo()
                if config.get("use_tls", True):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(sender, pwd)
                server.send_message(msg)
        return True, "Email sent successfully"
    except Exception as exc:
        traceback.print_exc()
        return False, f"SMTP Error: {str(exc)}"

SEED_STAFF = [
    {
        "email": "staff01@laundry.edu",
        "password": "password123",
        "uid": "STAFF01",
        "name": "Counter Staff 01",
    },
    {
        "email": "staff02@laundry.edu",
        "password": "password123",
        "uid": "STAFF02",
        "name": "Counter Staff 02",
    },
]


def now_stamp() -> str:
    return datetime.now().strftime("%d-%m-%Y %H:%M")


def generate_otp() -> str:
    return f"{random.randint(100000, 999999)}"


def load_db() -> dict:
    """Load JSON database safely. Never auto-seeds students, orders or complaints."""
    data = None
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[WashTrack] Warning: Failed to parse {DATA_FILE}: {e}. Starting fresh.")
            data = None

    if not isinstance(data, dict):
        data = {
            "tokenCounter": 100,
            "staff": [dict(s) for s in SEED_STAFF],
            "students": [],
            "orders": [],
            "complaints": [],
        }
        save_db(data)
        return data

    # Ensure required keys exist (but never overwrite with demo data)
    changed = False
    if "staff" not in data or not data["staff"]:
        data["staff"] = [dict(s) for s in SEED_STAFF]
        changed = True
    if "students" not in data:
        data["students"] = []
        changed = True
    if "orders" not in data:
        data["orders"] = []
        changed = True
    if "complaints" not in data:
        data["complaints"] = []
        changed = True
    if "tokenCounter" not in data or not isinstance(data["tokenCounter"], int):
        data["tokenCounter"] = 100
        changed = True

    # Ensure all existing students have a password and college email
    for s in data.get("students", []):
        if not s.get("password"):
            s["password"] = "password123"
            changed = True
        if not s.get("collegeEmail"):
            reg_clean = s.get("regNo", "student").lower()
            s["collegeEmail"] = f"{reg_clean}@campus.edu"
            changed = True

    if changed:
        save_db(data)

    return data


def save_db(data: dict) -> None:
    """Save database atomically via temporary file to prevent corruption."""
    tmp_path = ROOT / "data.json.tmp"
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp_path.replace(DATA_FILE)


def public_order(order: dict, include_otp: bool) -> dict:
    item = dict(order)
    if not include_otp:
        item.pop("handoverOtp", None)
    return item


def public_student(student: dict | None) -> dict | None:
    if not student:
        return None
    item = dict(student)
    item.pop("password", None)
    return item


def find_order(db: dict, token: str) -> dict | None:
    token_clean = (token or "").strip()
    for order in db.get("orders", []):
        if order.get("token") == token_clean:
            return order
    return None


def find_student(db: dict, identifier: str) -> dict | None:
    key = (identifier or "").strip().lower()
    if not key:
        return None
    for student in db.get("students", []):
        s_reg = student.get("regNo", "").strip().lower()
        s_cemail = student.get("collegeEmail", "").strip().lower()
        s_email = student.get("email", "").strip().lower()
        if key in (s_reg, s_cemail, s_email):
            return student
        # Also allow student's ID with campus email domain (e.g. 21bce0001@campus.edu)
        if s_reg and (key == f"{s_reg}@campus.edu" or key == f"{s_reg}@university.edu"):
            return student
    return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # Global CORS and caching headers
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, Accept")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        # Clean logging format
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def send_json(self, payload: dict | list, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # ----------------------------------------------------
    # GET Router
    # ----------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if not path.startswith("/api/"):
            return super().do_GET()

        try:
            if path == "/api/health":
                with LOCK:
                    db = load_db()
                    return self.send_json({
                        "ok": True,
                        "service": "washtrack",
                        "ordersCount": len(db.get("orders", [])),
                        "complaintsCount": len(db.get("complaints", [])),
                        "studentsCount": len(db.get("students", [])),
                    })

            if path == "/api/state":
                role = (query.get("role") or ["staff"])[0]
                reg_no = (query.get("regNo") or [""])[0].strip().upper()
                include_otp = (role == "student")
                with LOCK:
                    db = load_db()
                    orders = db.get("orders", [])
                    complaints = db.get("complaints", [])

                    if role == "student":
                        if reg_no:
                            orders = [o for o in orders if o.get("studentRegNo", "").upper() == reg_no]
                            complaints = [c for c in complaints if c.get("studentRegNo", "").upper() == reg_no]
                        else:
                            orders = []
                            complaints = []

                    return self.send_json({
                        "orders": [public_order(o, include_otp) for o in orders],
                        "complaints": complaints,
                        "tokenCounter": db.get("tokenCounter", 103),
                    })

            if path == "/api/settings/smtp":
                cfg = load_email_config()
                has_pwd = bool((cfg.get("sender_password") or "").strip())
                return self.send_json({
                    "enabled": cfg.get("enabled", False),
                    "configured": bool(cfg.get("sender_email") and has_pwd),
                    "sender_email": cfg.get("sender_email", ""),
                    "smtp_host": cfg.get("smtp_host", "smtp.gmail.com"),
                    "smtp_port": cfg.get("smtp_port", 587),
                    "use_tls": cfg.get("use_tls", True),
                })

            if path == "/api/meta/next-token":
                reg_no = (query.get("regNo") or ["21BCE0001"])[0].strip().upper() or "21BCE0001"
                with LOCK:
                    db = load_db()
                    student = find_student(db, reg_no)
                    counter = db.get("tokenCounter", 103)
                    return self.send_json({
                        "token": f"TK-{reg_no}-{counter}",
                        "tokenCounter": counter,
                        "student": public_student(student),
                    })

            # GET /api/orders/{token}
            parts = [unquote(p) for p in path.rstrip("/").split("/")]
            if len(parts) == 4 and parts[1] == "api" and parts[2] == "orders":
                token = parts[3]
                with LOCK:
                    db = load_db()
                    order = find_order(db, token)
                    if not order:
                        return self.send_json({"error": "Order not found"}, 404)
                    return self.send_json({"order": public_order(order, include_otp=True)})

            # GET /api/students/{regNo}
            if len(parts) == 4 and parts[1] == "api" and parts[2] == "students":
                reg_no = parts[3].upper()
                with LOCK:
                    db = load_db()
                    student = find_student(db, reg_no)
                    if not student:
                        return self.send_json({"error": "Student not found"}, 404)
                    return self.send_json({"student": public_student(student)})

            return self.send_json({"error": "API route not found"}, 404)

        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": f"Server error: {str(exc)}"}, 500)

    # ----------------------------------------------------
    # POST & PATCH Router
    # ----------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            body = self.read_json()
        except json.JSONDecodeError:
            return self.send_json({"error": "Invalid JSON body"}, 400)

        try:
            if path == "/api/auth/student":
                return self._auth_student(body)
            if path == "/api/auth/staff":
                return self._auth_staff(body)
            if path == "/api/auth/send-email-otp":
                return self._send_email_otp(body)
            if path == "/api/settings/smtp":
                return self._update_smtp_settings(body)
            if path == "/api/orders":
                return self._create_order(body)
            if path == "/api/complaints":
                return self._create_complaint(body)
            if path == "/api/reset":
                with LOCK:
                    data = seed_db()
                    save_db(data)
                return self.send_json({"ok": True, "message": "Database reset to initial demo seeds"})

            parts = [unquote(p) for p in path.split("/")]
            # /api/orders/{token}/check-otp | deliver | rewash | status
            if len(parts) >= 5 and parts[1] == "api" and parts[2] == "orders":
                token = parts[3]
                action = parts[4]
                if action == "check-otp":
                    return self._check_otp(token, body)
                if action == "deliver":
                    return self._deliver(token, body)
                if action == "rewash":
                    return self._rewash(token)
                if action == "status":
                    return self._advance_status(token, body)

            # /api/complaints/{complaintId}/resolve
            if len(parts) >= 5 and parts[1] == "api" and parts[2] == "complaints" and parts[4] == "resolve":
                return self._resolve_complaint(parts[3])

            return self.send_json({"error": "API endpoint not found"}, 404)

        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": f"Server error: {str(exc)}"}, 500)

    def do_PATCH(self):
        self.do_POST()

    # ----------------------------------------------------
    # Business Logic Handlers
    # ----------------------------------------------------
    def _send_email_otp(self, body: dict):
        identifier = (body.get("identifier") or body.get("regNo") or body.get("email") or "").strip()
        if not identifier:
            return self.send_json({"error": "Student ID or College Email is required to send verification code."}, 400)

        with LOCK:
            db = load_db()
            student = find_student(db, identifier)
            if not student:
                return self.send_json({"error": "No university student account found with this ID or Email."}, 404)

            college_email = (body.get("collegeEmail") or student.get("collegeEmail") or student.get("email") or f"{student['regNo'].lower()}@campus.edu").strip().lower()
            code = f"{random.randint(100000, 999999)}"
            expires_at = time.time() + 600  # 10 minutes

            otp_record = {
                "code": code,
                "email": college_email,
                "regNo": student["regNo"],
                "expires_at": expires_at,
                "created_at": time.time(),
            }
            EMAIL_OTP_STORE[student["regNo"].upper()] = otp_record
            EMAIL_OTP_STORE[college_email] = otp_record

            # Attempt real email delivery via SMTP
            sent_ok, send_msg = send_real_email(
                recipient=college_email,
                subject=f"WashTrack Verification Code: {code}",
                code=code,
                student_name=student.get("name", "Student"),
            )

            print(f"\n=======================================================")
            print(f" [CAMPUS EMAIL DISPATCH] WashTrack Identity Service")
            print(f" To: {college_email}")
            print(f" Student: {student.get('name')} ({student['regNo']})")
            print(f" Subject: WashTrack - Verification Code for Password Change")
            print(f" Code: {code} (Valid for 10 minutes)")
            print(f" Real SMTP Sent: {'YES' if sent_ok else 'NO (' + send_msg + ')'}")
            print(f"=======================================================\n")

            return self.send_json({
                "ok": True,
                "deliveredRealEmail": sent_ok,
                "smtpStatus": send_msg,
                "message": f"Verification code sent to {college_email}" if sent_ok else f"Code generated for {college_email} (SMTP not connected)",
                "email": college_email,
                "regNo": student["regNo"],
                "expiresIn": 600,
                "devCode": code,
            })

    def _update_smtp_settings(self, body: dict):
        sender = (body.get("sender_email") or "").strip()
        pwd = (body.get("sender_password") or "").strip()
        host = (body.get("smtp_host") or "smtp.gmail.com").strip()
        try:
            port = int(body.get("smtp_port") or 587)
        except (ValueError, TypeError):
            port = 587
        use_tls = bool(body.get("use_tls", True))

        if not sender:
            return self.send_json({"error": "Sender email is required"}, 400)

        cfg = load_email_config()
        if not pwd and cfg.get("sender_email") == sender and cfg.get("sender_password"):
            pwd = cfg["sender_password"]

        if not pwd:
            return self.send_json({"error": "Sender password or Google App Password is required"}, 400)

        new_cfg = {
            "enabled": True,
            "smtp_host": host,
            "smtp_port": port,
            "sender_email": sender,
            "sender_password": pwd,
            "use_tls": use_tls,
        }

        # Test connection if requested
        if body.get("testConnection"):
            test_recipient = (body.get("testRecipient") or sender).strip()
            save_email_config(new_cfg)
            code = f"{random.randint(100000, 999999)}"
            ok, msg = send_real_email(test_recipient, "WashTrack - SMTP Connection Test", code, "WashTrack Admin")
            if not ok:
                return self.send_json({"error": f"Failed to connect or send via SMTP server: {msg}"}, 400)
            return self.send_json({
                "ok": True,
                "message": f"SMTP verified! Test email successfully delivered to {test_recipient}."
            })

        save_email_config(new_cfg)
        return self.send_json({"ok": True, "message": "Email SMTP settings saved successfully!"})

    def _auth_student(self, body: dict):
        identifier = (body.get("regNo") or body.get("identifier") or "").strip()
        password = (body.get("password") or "").strip()
        action = (body.get("action") or "login").strip().lower()

        if action == "register":
            return self.send_json({
                "error": "Self-registration is disabled. Student accounts are pre-issued by University administration."
            }, 403)

        with LOCK:
            db = load_db()

            if action == "update_profile":
                current_reg = (body.get("regNo") or "").strip().upper()
                if not current_reg:
                    return self.send_json({"error": "Student registration ID is required"}, 400)

                existing = find_student(db, current_reg)
                if not existing:
                    return self.send_json({"error": "Student account not found"}, 404)

                # Update college email
                college_email = (body.get("collegeEmail") or "").strip().lower()
                if college_email:
                    existing["collegeEmail"] = college_email

                # Update Login ID if student requested changing it
                new_login_id = (body.get("newLoginId") or "").strip().upper()
                if new_login_id and new_login_id != existing.get("regNo", "").upper():
                    target = find_student(db, new_login_id)
                    if target and target.get("regNo", "").upper() != existing["regNo"].upper():
                        return self.send_json({"error": f"Login ID '{new_login_id}' is already taken by another student."}, 400)
                    old_reg = existing["regNo"]
                    existing["regNo"] = new_login_id
                    for order in db.get("orders", []):
                        if order.get("studentRegNo", "").upper() == old_reg.upper():
                            order["studentRegNo"] = new_login_id
                    for complaint in db.get("complaints", []):
                        if complaint.get("studentRegNo", "").upper() == old_reg.upper():
                            complaint["studentRegNo"] = new_login_id

                # Update password if provided - REQUIRES EMAIL AUTHENTICATION
                new_password = (body.get("newPassword") or "").strip()
                if new_password:
                    if len(new_password) < 6:
                        return self.send_json({"error": "New password must be at least 6 characters long"}, 400)
                    
                    email_otp = (body.get("emailOtp") or body.get("emailCode") or "").strip()
                    if not email_otp:
                        return self.send_json({
                            "error": "Email verification code is required to change your password. Please click 'Send Code to Email' to verify your college email."
                        }, 400)

                    record = EMAIL_OTP_STORE.get(existing["regNo"].upper()) or EMAIL_OTP_STORE.get(existing.get("collegeEmail", "").lower())
                    if not record:
                        return self.send_json({
                            "error": "No verification code requested or code has expired. Please click 'Send Code to Email'."
                        }, 400)

                    if time.time() > record["expires_at"]:
                        return self.send_json({
                            "error": "Email verification code has expired. Please request a new code."
                        }, 400)

                    if email_otp != record["code"]:
                        return self.send_json({
                            "error": "Incorrect email verification code. Please check your college inbox."
                        }, 400)

                    # Verified successfully -> update password and consume OTP
                    existing["password"] = new_password
                    EMAIL_OTP_STORE.pop(existing["regNo"].upper(), None)
                    EMAIL_OTP_STORE.pop(existing.get("collegeEmail", "").lower(), None)

                # Update other profile details
                if body.get("name"):
                    existing["name"] = body["name"].strip()
                if body.get("hostel"):
                    existing["hostel"] = body["hostel"]
                if body.get("room"):
                    existing["room"] = body["room"].strip()
                if body.get("phone"):
                    existing["phone"] = body["phone"].strip()

                save_db(db)
                return self.send_json({"student": public_student(existing)})

            elif action == "reset_password":
                # Forgot password / recovery via college email
                if not identifier:
                    return self.send_json({"error": "Student ID or College Email is required"}, 400)

                existing = find_student(db, identifier)
                if not existing:
                    return self.send_json({"error": "Student account not found"}, 404)

                email_otp = (body.get("emailOtp") or body.get("emailCode") or "").strip()
                new_password = (body.get("newPassword") or "").strip()

                if not email_otp:
                    return self.send_json({"error": "Email verification code is required."}, 400)
                if not new_password or len(new_password) < 6:
                    return self.send_json({"error": "New password must be at least 6 characters long."}, 400)

                record = EMAIL_OTP_STORE.get(existing["regNo"].upper()) or EMAIL_OTP_STORE.get(existing.get("collegeEmail", "").lower())
                if not record:
                    return self.send_json({"error": "No verification code requested or code has expired."}, 400)
                if time.time() > record["expires_at"]:
                    return self.send_json({"error": "Verification code has expired. Please request a new code."}, 400)
                if email_otp != record["code"]:
                    return self.send_json({"error": "Incorrect email verification code. Please check your college email."}, 400)

                existing["password"] = new_password
                save_db(db)
                EMAIL_OTP_STORE.pop(existing["regNo"].upper(), None)
                EMAIL_OTP_STORE.pop(existing.get("collegeEmail", "").lower(), None)
                return self.send_json({
                    "ok": True,
                    "message": "Password changed successfully! You may now log in with your new password.",
                    "student": public_student(existing)
                })

            else:  # default action == "login"
                if not identifier:
                    return self.send_json({"error": "Student ID or College Email is required"}, 400)
                if not password:
                    return self.send_json({"error": "Password is required to sign in"}, 400)

                existing = find_student(db, identifier)
                if not existing:
                    return self.send_json({
                        "error": "Account not found. Only pre-issued University Student IDs can access this portal."
                    }, 404)

                expected_pwd = existing.get("password") or "password123"
                if password != expected_pwd:
                    return self.send_json({"error": "Incorrect password. Please try again."}, 401)

                return self.send_json({"student": public_student(existing)})

    def _auth_staff(self, body: dict):
        identifier = (body.get("email") or body.get("uid") or body.get("identifier") or "").strip().lower()
        password = (body.get("password") or "").strip()
        if not identifier or not password:
            return self.send_json({"error": "Staff ID or Email, and password are required"}, 400)

        with LOCK:
            db = load_db()
            for staff in db.get("staff", []):
                s_email = staff.get("email", "").lower()
                s_uid = staff.get("uid", "").lower()
                if (identifier == s_email or identifier == s_uid) and staff.get("password") == password:
                    return self.send_json({
                        "staff": {
                            "email": staff["email"],
                            "uid": staff["uid"],
                            "name": staff.get("name") or staff["uid"],
                        }
                    })

        return self.send_json({"error": "Invalid staff credentials (demo: staff01@laundry.edu / password123)"}, 401)

    def _create_order(self, body: dict):
        reg = (body.get("studentRegNo") or "").strip().upper()
        clothes = (body.get("clothesDetails") or "").strip()
        try:
            qty = int(body.get("totalQuantity") or 0)
        except (ValueError, TypeError):
            qty = 0

        if not reg:
            return self.send_json({"error": "Student registration number is required"}, 400)
        if qty <= 0 or not clothes:
            return self.send_json({"error": "Select at least one clothing item"}, 400)

        with LOCK:
            db = load_db()
            student = find_student(db, reg)

            # Auto-register student if entering new intake
            if not student:
                student = {
                    "regNo": reg,
                    "name": (body.get("studentName") or "Student").strip() or "Student",
                    "hostel": body.get("hostelBlock") or "Block A (Men's)",
                    "room": (body.get("roomNo") or "302").strip() or "302",
                    "studentPhone": (body.get("studentPhone") or "+91 9876543210").strip() or "+91 9876543210",
                }
                db["students"].append(student)

            token_counter = db.get("tokenCounter", 103)
            token = f"TK-{reg}-{token_counter}"
            order = {
                "orderId": str(len(db.get("orders", [])) + 1),
                "studentRegNo": reg,
                "studentName": body.get("studentName") or student.get("name") or "Student",
                "hostelBlock": body.get("hostelBlock") or student.get("hostel") or "Block A (Men's)",
                "roomNo": body.get("roomNo") or student.get("room") or "302",
                "studentPhone": body.get("studentPhone") or student.get("phone") or "+91 9876543210",
                "staffUid": body.get("staffUid") or "STAFF01",
                "clothesDetails": clothes,
                "totalQuantity": qty,
                "submissionDateTime": now_stamp(),
                "status": "Submitted",
                "token": token,
                "handoverOtp": generate_otp(),
                "ironingRequested": bool(body.get("ironingRequested")),
            }
            db["orders"].insert(0, order)
            db["tokenCounter"] = token_counter + 1
            save_db(db)

        # Send submission confirmation email (non-blocking best-effort)
        try:
            send_order_notification_email(order, student, "submitted")
        except Exception:
            pass

        return self.send_json({
            "order": public_order(order, include_otp=False),
            "tokenCounter": db["tokenCounter"]
        })

    def _advance_status(self, token: str, body: dict):
        target = body.get("status")
        if target not in STATUS_STEPS:
            return self.send_json({"error": f"Invalid status '{target}'"}, 400)
        if target == "Delivered":
            return self.send_json({"error": "Delivered requires OTP handover verification"}, 400)

        with LOCK:
            db = load_db()
            order = find_order(db, token)
            if not order:
                return self.send_json({"error": f"Order with token '{token}' not found"}, 404)

            current = "Delivered" if order["status"] == "Completed" else order["status"]
            try:
                cur_i = STATUS_STEPS.index(current)
                tgt_i = STATUS_STEPS.index(target)
            except ValueError:
                cur_i = 0
                tgt_i = STATUS_STEPS.index(target)

            # Allow standard sequential progression or moving back to In Wash (for re-wash/fixes)
            if tgt_i != cur_i + 1 and not (target == "In Wash" and current in ("Delivered", "Ready for Pickup")):
                return self.send_json({"error": f"Cannot transition from '{current}' to '{target}'"}, 400)

            order["status"] = target
            if target == "Ready for Pickup" and not order.get("handoverOtp"):
                order["handoverOtp"] = generate_otp()

            save_db(db)

            # Send "ready for pickup" notification email
            if target == "Ready for Pickup":
                student = find_student(db, order.get("studentRegNo", ""))
                if student:
                    try:
                        send_order_notification_email(order, student, "ready_for_pickup")
                    except Exception:
                        pass

            return self.send_json({"order": public_order(order, include_otp=False)})

    def _check_otp(self, token: str, body: dict):
        otp = str(body.get("otp") or "").strip()
        with LOCK:
            db = load_db()
            order = find_order(db, token)
            if not order:
                return self.send_json({"error": "Order not found"}, 404)
            expected = str(order.get("handoverOtp") or "").strip()
            match = bool(expected and otp == expected and len(otp) == 6)
            return self.send_json({"match": match})

    def _deliver(self, token: str, body: dict):
        otp = str(body.get("otp") or "").strip()
        with LOCK:
            db = load_db()
            order = find_order(db, token)
            if not order:
                return self.send_json({"error": "Order not found"}, 404)

            status = "Delivered" if order["status"] == "Completed" else order["status"]
            if status != "Ready for Pickup":
                return self.send_json({"error": f"Order status is '{status}', not Ready for Pickup"}, 400)

            expected_otp = str(order.get("handoverOtp") or "").strip()
            if not expected_otp or otp != expected_otp:
                return self.send_json({"error": "Invalid 6-digit OTP code"}, 401)

            order["status"] = "Delivered"

            # Auto-resolve any pending re-wash requests for this token
            for complaint in db.get("complaints", []):
                if complaint.get("token") == token and complaint.get("category") == "Re-Wash Request":
                    complaint["status"] = "Resolved"

            save_db(db)
            return self.send_json({"order": public_order(order, include_otp=False)})

    def _rewash(self, token: str):
        with LOCK:
            db = load_db()
            order = find_order(db, token)
            if not order:
                return self.send_json({"error": "Order not found"}, 404)

            # Move order status back to In Wash for the laundry team with a fresh OTP
            order["status"] = "In Wash"
            order["handoverOtp"] = generate_otp()

            complaint = {
                "complaintId": f"C-{int(datetime.now().timestamp() * 1000)}",
                "studentRegNo": order["studentRegNo"],
                "token": token,
                "staffUid": order.get("staffUid") or "STAFF01",
                "category": "Re-Wash Request",
                "description": f"Student requested a re-wash for order {token}",
                "timestamp": now_stamp(),
                "photoUrl": None,
                "status": "Pending",
            }
            db["complaints"].insert(0, complaint)
            save_db(db)

        return self.send_json({"complaint": complaint, "order": public_order(order, include_otp=True)})

    def _create_complaint(self, body: dict):
        token = (body.get("token") or "").strip()
        desc = (body.get("description") or "").strip()
        if not token or not desc:
            return self.send_json({"error": "Token and description are required"}, 400)

        with LOCK:
            db = load_db()
            order = find_order(db, token)
            complaint = {
                "complaintId": f"C-{int(datetime.now().timestamp() * 1000)}",
                "studentRegNo": body.get("studentRegNo") or (order["studentRegNo"] if order else "UNKNOWN"),
                "token": token,
                "staffUid": body.get("staffUid") or (order.get("staffUid") if order else "STAFF01"),
                "category": body.get("category") or "Other",
                "description": desc,
                "timestamp": now_stamp(),
                "photoUrl": body.get("photoUrl"),
                "status": "Pending",
            }
            db["complaints"].insert(0, complaint)
            save_db(db)

        return self.send_json({"complaint": complaint})

    def _resolve_complaint(self, complaint_id: str):
        complaint_id = unquote(complaint_id).strip()
        with LOCK:
            db = load_db()
            for complaint in db.get("complaints", []):
                if complaint.get("complaintId") == complaint_id:
                    complaint["status"] = "Resolved"
                    save_db(db)
                    return self.send_json({"complaint": complaint})

        return self.send_json({"error": "Complaint not found"}, 404)


def main():
    with LOCK:
        db = load_db()
        print(f"[WashTrack] Database ready: {len(db.get('orders', []))} orders, "
              f"{len(db.get('students', []))} students, {len(db.get('complaints', []))} complaints.")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[WashTrack] Server running at http://localhost:{PORT} (also accessible on 0.0.0.0:{PORT})")
    print(f"[WashTrack] Staff demo credentials: staff01@laundry.edu / password123")
    print(f"[WashTrack] Student demo regNo: 21BCE0001 (Alex Smith) / 21BCE0002 (Rachel Green)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[WashTrack] Server stopped cleanly.")
        server.server_close()


if __name__ == "__main__":
    main()
