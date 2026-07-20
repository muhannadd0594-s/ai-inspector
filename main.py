"""
AI Product Inspector — WhatsApp bot MVP
=========================================
Receives a product photo + caption via WhatsApp (Twilio), sends it to
Gemini Vision for analysis, replies with a plain-Arabic report.

SETUP REQUIRED (you do these, not Claude — see README.md):
1. Twilio account + WhatsApp Sandbox (or approved Business number)
2. Google AI Studio API key (Gemini)
3. A public URL for the webhook (Railway / Render / a VPS — see README)

Run locally for testing:
    pip install -r requirements.txt
    export GEMINI_API_KEY="..."
    export TWILIO_AUTH_TOKEN="..."
    uvicorn main:app --reload
"""

import os
import json
import base64
import logging
import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from analysis_prompt import ANALYSIS_SYSTEM_PROMPT, build_user_prompt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-inspector")

app = FastAPI(title="AI Product Inspector")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent?key=" + GEMINI_API_KEY
)


def download_media(media_url: str, twilio_sid: str, twilio_token: str) -> bytes:
    """Twilio media URLs require basic auth with your account credentials."""
    resp = requests.get(media_url, auth=(twilio_sid, twilio_token), timeout=20)
    resp.raise_for_status()
    return resp.content


def analyze_image(image_bytes: bytes, seller_caption: str) -> dict:
    """Sends the image + prompt to Gemini Vision and parses the JSON result."""
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

    resp = requests.post(GEMINI_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
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


def format_whatsapp_reply(result: dict) -> str:
    """Turns the JSON analysis into a readable WhatsApp message in Arabic."""
    if result.get("image_quality") in ("poor", "unusable"):
        note = result.get("quality_note", "الصورة غير واضحة")
        return f"⚠️ {note}\nحاول ترسل صورة أوضح وبإضاءة أفضل."

    lines = ["📋 *تقرير الفحص الآلي*\n"]

    observations = result.get("observations", [])
    if observations:
        for obs in observations:
            icon = {"damage": "🔴", "discrepancy": "🟡", "inconsistency": "🟠", "note": "ℹ️"}.get(obs["type"], "•")
            lines.append(f"{icon} {obs['description']}")
    else:
        lines.append("✅ لم يلاحظ النظام أي مشاكل ظاهرة في الصورة.")

    claim_check = result.get("seller_claim_check")
    if claim_check == "contradicts":
        lines.append("\n⚠️ *تنبيه: الصورة قد تتعارض مع وصف البائع.*")

    lines.append(f"\n{result.get('summary_for_user', '')}")
    lines.append("\n_هذا تحليل آلي استرشادي غير ملزم. القرار النهائي يعود لك._")

    return "\n".join(lines)


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """Twilio calls this URL whenever a user sends a WhatsApp message."""
    form = await request.form()
    num_media = int(form.get("NumMedia", 0))
    caption = form.get("Body", "")

    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")

    if num_media == 0:
        reply = "أرسل صورة المنتج مع وصف مختصر (مثال: قميص أسود جديد بدون عيوب) عشان أقدر أحلله لك."
    else:
        media_url = form.get("MediaUrl0")
        try:
            image_bytes = download_media(media_url, twilio_sid, twilio_token)
            result = analyze_image(image_bytes, caption)
            reply = format_whatsapp_reply(result)
        except Exception as e:
            log.exception("Analysis failed")
            reply = "حدث خطأ أثناء التحليل. حاول مرة أخرى بعد قليل."

    twiml = f"<Response><Message>{reply}</Message></Response>"
    return Response(content=twiml, media_type="application/xml")


@app.get("/health")
async def health():
    return PlainTextResponse("ok")
