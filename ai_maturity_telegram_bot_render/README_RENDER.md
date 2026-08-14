# AI Maturity Telegram Bot — Render edition

Ця версія адаптована для Render Web Service і Telegram webhook.

## Файли
- `app.py` — Flask endpoint для Telegram webhook та health check.
- `bot.py` — логіка оцінювання; локальний polling залишено для тестів.
- `matrix.json` — 48 показників D1.1–D8.6.
- `render.yaml` — Render Blueprint.
- `requirements.txt` — Python-залежності.

## Змінні середовища
Обов'язково:
- `TELEGRAM_BOT_TOKEN`

Автоматично/рекомендовано:
- `TELEGRAM_WEBHOOK_SECRET` — секрет Telegram webhook; `render.yaml` може згенерувати його.

Необов'язково:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

## Render
Start command:
`gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT app:app`

Health check:
`/health`

Після старту `app.py` використовує `RENDER_EXTERNAL_URL` і реєструє
`https://<service>.onrender.com/telegram/webhook` у Telegram через `setWebhook`.

## Важливо про дані
На Free Render локальна файлова система є тимчасовою. SQLite та створені локально звіти можуть бути втрачені під час redeploy/restart. Для апробації журнал треба перенести у зовнішню БД або архівувати результати у Google Drive.
