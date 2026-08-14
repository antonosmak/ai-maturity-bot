from __future__ import annotations

import asyncio
import os
import threading
from flask import Flask, jsonify, request
import httpx

import bot

app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

bot.init_db()


def run_async(coro):
    return asyncio.run(coro)


def run_update_background(update: dict):
    """Process Telegram update outside the HTTP request.
    This is required because free-tier Groq analysis may take several minutes.
    """
    def worker():
        try:
            asyncio.run(bot.process_update(update))
        except Exception as exc:
            print("Background update error:", bot._safe_log_text(repr(exc)), flush=True)

    threading.Thread(target=worker, daemon=True).start()


@app.get("/")
def index():
    return jsonify({
        "service": "AI Maturity Telegram Bot",
        "status": "ok",
        "mode": "webhook",
        "version": "0.4.7",
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
    # Acknowledge Telegram immediately; long AI work continues in background.
    run_update_background(update)
    return "ok", 200


@app.post("/admin/set-webhook")
def set_webhook():
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


def configure_bot_ui():
    try:
        asyncio.run(bot.configure_telegram_ui())
    except Exception as exc:
        print("Telegram UI startup warning:", bot._safe_log_text(repr(exc)), flush=True)


if RENDER_EXTERNAL_URL:
    try:
        register_webhook(RENDER_EXTERNAL_URL)
    except Exception as exc:
        print("Webhook registration warning:", bot._safe_log_text(repr(exc)), flush=True)

# Important: webhook deployment uses app.py, not bot.py's __main__.
# Therefore profile description/commands must be configured here.
configure_bot_ui()
