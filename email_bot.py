"""
AI Product Inspector — Email bot MVP (v2, simpler than WhatsApp)
===================================================================
Polls a Gmail inbox for new emails with image attachments, analyzes
them with Gemini Vision, and auto-replies with a report.

SETUP REQUIRED (you do these — see README.md):
1. A Gmail account dedicated to this bot (e.g. inspect@gmail.com)
2. An "App Password" for that Gmail account (NOT your normal password)
3. A Gemini API key

Run:
    pip install -r requirements.txt
    export GMAIL_ADDRESS="yourbot@gmail.com"
    export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
    export GEMINI_API_KEY="..."
    python email_bot.py
"""

import os
import time
import json
import base64
import logging
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.header import decode_header
import requests

from analysis_prompt import ANALYSIS_SYSTEM_PROMPT, build_user_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("email-inspector")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash-lite:generateContent?key=" + GEMINI_API_KEY
)


def analyze_image(image_bytes: bytes, seller_caption: str) -> dict:
    """Analyze image with exponential backoff retry on rate limit (429)."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [{
            "parts": [
                {"text": ANALYSIS_SYSTEM_PROMPT + "\n\n" + build_user_prompt(seller_caption)},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}},
            ]
        }],
        "generationConfig": {"temperature": 0.2},
    }
    
    # Retry with exponential backoff on 429 errors
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(GEMINI_URL, json=payload, timeout=30)
            resp.raise_for_status()
            raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            clean = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                return json.loads(clean)
            except json.JSONDecodeError:
                log.error("Failed to parse Gemini response: %s", raw_text)
                return {
                    "image_quality": "unusable",
                    "quality_note": "تعذر تحليل الصورة، حاول مرة أخرى بصورة أوضح",
                    "observations": [],
                    "seller_claim_check": "cannot_confirm",
                    "summary_for_user": "حدث خطأ تقني في التحليل. أعد إرسال الصورة من فضلك.",
                }
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  # Rate limit error
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 10  # 10s, 20s, 40s
                    log.warning("Rate limited (429). Retrying in %ds (attempt %d/%d)", wait_time, attempt + 1, max_retries)
                    time.sleep(wait_time)
                    continue
                else:
                    log.error("Rate limited after %d retries. Giving up.", max_retries)
                    return {
                        "image_quality": "unusable",
                        "quality_note": "النظام مشغول جداً حالياً. حاول لاحقاً من فضلك",
                        "observations": [],
                        "seller_claim_check": "cannot_confirm",
                        "summary_for_user": "خدمة Gemini مشغولة جداً. سيتم المحاولة في الدقائق القادمة.",
                    }
            else:
                raise


def format_report(result: dict) -> str:
    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "الصورة غير واضحة")
        return f"⚠️ {note}\nحاول ترسل صورة أوضح وبإضاءة أفضل."

    lines = ["تقرير الفحص الآلي\n" + "-" * 30]
    observations = result.get("observations", [])
    if observations:
        for obs in observations:
            icon = {"damage": "[تلف]", "discrepancy": "[تعارض]", "inconsistency": "[ملاحظة]", "note": "[معلومة]"}.get(obs["type"], "-")
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

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [to_address], msg.as_string())
    log.info("Replied to %s", to_address)


def decode_mime_words(s):
    if not s:
        return ""
    decoded = decode_header(s)
    return "".join(
        (t.decode(enc or "utf-8") if isinstance(t, bytes) else t) for t, enc in decoded
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

            sender = email.utils.parseaddr(msg.get("From"))[1]
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
            else:
                report = "لم أجد صورة مرفقة في رسالتك. أرسل الصورة مع وصف قصير للمنتج."

            send_reply(sender, subject, report)

        except Exception:
            log.exception("Failed processing email id=%s", eid)

    mail.logout()


def main():
    if not all([GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GEMINI_API_KEY]):
        raise SystemExit("Missing required environment variables. Check README.md")

    log.info("AI Product Inspector (email mode) starting. Polling every %ss", POLL_INTERVAL_SECONDS)
    while True:
        try:
            process_inbox()
        except Exception:
            log.exception("Error during inbox check")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

