"""
AI Product Inspector — Email bot MVP (v2 - OpenRouter)
=======================================================
Polls a Gmail inbox for new emails with image attachments, analyzes
them with OpenRouter Vision, and auto-replies with a report.

SETUP REQUIRED:
1. Gmail account + App Password
2. OpenRouter API key (sk-or-...)
3. Set env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, OPENROUTER_API_KEY
"""

import os
import time
import json
import base64
import logging
import socket
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.header import decode_header
import requests

# --- Fix for Railway/Docker environments where IPv6 routes exist but are
# unreachable, causing "OSError: [Errno 101] Network is unreachable" on
# smtplib/imaplib connections. Force all socket lookups to IPv4 only. ---
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(*args, **kwargs):
    responses = _orig_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET] or responses
socket.getaddrinfo = _ipv4_only_getaddrinfo

from analysis_prompt import ANALYSIS_SYSTEM_PROMPT, build_user_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("email-inspector")

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def analyze_image(image_bytes: bytes, seller_caption: str) -> dict:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "model": "google/gemini-2.5-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ANALYSIS_SYSTEM_PROMPT + "\n\n" + build_user_prompt(seller_caption)},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                ],
            }
        ],
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    raw_text = resp.json()["choices"][0]["message"]["content"]
    clean = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.error("Failed to parse response: %s", raw_text)
        return {
            "image_quality": "unusable",
            "quality_note": "تعذر تحليل الصورة، حاول مرة أخرى بصورة أوضح",
            "observations": [],
            "seller_claim_check": "cannot_confirm",
            "summary_for_user": "حدث خطأ تقني في التحليل. أعد إرسال الصورة من فضلك.",
        }


def format_report(result: dict) -> str:
    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "الصورة غير واضحة")
        return f"⚠️ {note}\nحاول ترسل صورة أوضح وبإضاءة أفضل."

    lines = ["تقرير الفحص الآلي\n" + "-" * 30]
    observations = result.get("observations", [])
    if observations:
        for obs in observations:
            icon = {
                "damage": "[تلف]",
                "discrepancy": "[تعارض]",
                "inconsistency": "[ملاحظة]",
                "note": "[معلومة]"
            }.get(obs["type"], "-")
            lines.append(f"{icon} {obs['description']}")
    else:
        lines.append("لم يلاحظ النظام أي مشاكل ظاهرة في الصورة.")

    if result.get("seller_claim_check") == "contradicts":
        lines.append("\n⚠️ تنبيه: الصورة قد تتعارض مع الوصف المرفق.")

    lines.append(f"\n{result.get('summary_for_user', '')}")
    lines.append("\n---\nهذا تحليل آلي استرشادي غير ملزم. القرار النهائي يعود لك.")
    return "\n".join(lines)


def send_reply(to_address: str, subject: str, body: str):
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"Re: {subject}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_address

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to_address], msg.as_string())
    log.info("Replied to %s", to_address)


def decode_mime_words(s):
    if not s:
        return ""
    decoded = decode_header(s)
    return "".join(
        (t.decode(enc or "utf-8") if isinstance(t, bytes) else t)
        for t, enc in decoded
    )


def process_inbox():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    status, data = mail.search(None, "UNSEEN")
    if status != "OK":
        mail.logout()
        return

    ids = data[0].split()
    log.info("Found %d unread email(s)", len(ids))

    for eid in ids:
        try:
            _, msg_data = mail.fetch(eid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            sender  = email.utils.parseaddr(msg.get("From"))[1]
            subject = decode_mime_words(msg.get("Subject"))
            caption = ""
            image_bytes = None

            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not part.get_filename():
                    payload = part.get_payload(decode=True)
                    if payload:
                        caption = payload.decode(errors="ignore").strip()
                elif content_type.startswith("image/"):
                    image_bytes = part.get_payload(decode=True)

            if image_bytes:
                result = analyze_image(image_bytes, caption)
                report = format_report(result)
                log.info("Analysis done for %s", sender)
            else:
                report = "لم أجد صورة مرفقة في رسالتك. أرسل الصورة مع وصف قصير للمنتج."

            send_reply(sender, subject, report)

        except Exception:
            log.exception("Failed processing email id=%s", eid)

    mail.logout()


def main():
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, OPENROUTER_API_KEY]):
        raise SystemExit("Missing required environment variables: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, OPENROUTER_API_KEY")

    log.info("AI Product Inspector (email mode) starting. Polling every %ss", POLL_INTERVAL_SECONDS)
    while True:
        try:
            process_inbox()
        except Exception:
            log.exception("Error during inbox check")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

