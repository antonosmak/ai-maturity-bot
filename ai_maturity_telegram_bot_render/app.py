from __future__ import annotations

import asyncio
import os
from flask import Flask, jsonify, request
import httpx

import bot

app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

bot.init_db()


def run_async(coro):
    return asyncio.run(coro)


@app.get("/")
def index():
    return jsonify({
        "service": "AI Maturity Telegram Bot",
        "status": "ok",
        "mode": "webhook",
    })


@app.get("/health")
def health():
    return "OK", 200


@app.post("/telegram/webhook")
def telegram_webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            return "forbidden", 403
    update = request.get_json(silent=True) or {}
    try:
        run_async(bot.process_update(update))
    except Exception as exc:
        print("Webhook update error:", repr(exc), flush=True)
        return "error", 500
    return "ok", 200


@app.post("/admin/set-webhook")
def set_webhook():
    """Protected by WEBHOOK_SECRET header; useful for manual reset."""
    if not WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "TELEGRAM_WEBHOOK_SECRET is not configured"}), 400
    supplied = request.headers.get("X-Admin-Secret", "")
    if supplied != WEBHOOK_SECRET:
        return "forbidden", 403
    if not RENDER_EXTERNAL_URL:
        return jsonify({"ok": False, "error": "RENDER_EXTERNAL_URL is missing"}), 400
    result = register_webhook(RENDER_EXTERNAL_URL)
    return jsonify(result)


def register_webhook(base_url: str) -> dict:
    url = f"{base_url}/telegram/webhook"
    payload = {
        "url": url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{bot.API}/setWebhook", json=payload)
        r.raise_for_status()
        data = r.json()
    print("Telegram webhook:", data, flush=True)
    return data


if RENDER_EXTERNAL_URL:
    try:
        register_webhook(RENDER_EXTERNAL_URL)
    except Exception as exc:
        print("Webhook registration warning:", repr(exc), flush=True)
