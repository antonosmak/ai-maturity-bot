# AI Maturity Telegram Bot — v0.2.2

Прототип Telegram-бота для оцінювання AI-зрілості органу публічної влади за матрицею D1–D8 (48 діагностичних тверджень).

## Що працює

- 48-позиційне оцінювання за шкалою 0–5;
- автоматичний розрахунок D1–D8 та AIMI;
- визначення рівня AI-зрілості;
- радарна діаграма;
- фінальна верифікація результату респондентом;
- базові/AI-рекомендації;
- PDF, XLSX, JSON audit log та ZIP bundle;
- після фінального коментаря PDF автоматично надсилається респонденту в Telegram;
- копія фінального PDF автоматично архівується в Google Drive через Google Apps Script;
- `/drive [ID]` вручну архівує PDF + XLSX + JSON + Radar у Google Drive.

## Render Environment Variables

Обов'язкові:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`

Для Google Drive:

- `GDRIVE_UPLOAD_URL` — URL вебдодатка Apps Script, що закінчується `/exec`
- `GDRIVE_UPLOAD_SECRET` — той самий SECRET, що заданий у Apps Script

Опційні для AI-рекомендацій:

- `OPENAI_API_KEY`
- `OPENAI_MODEL`

## Команди

- `/new` — нове оцінювання
- `/status` — стан
- `/log` — журнал
- `/report [ID]` — короткий звіт
- `/pdf [ID]` — PDF
- `/xlsx [ID]` — XLSX
- `/bundle [ID]` — ZIP-пакет
- `/drive [ID]` — архівувати пакет у Google Drive
- `/recommend [ID]` — рекомендації
- `/export [ID]` — JSON audit log
- `/cancel` — скасувати

## Оновлення з v0.1/v0.2

Файли цього пакета можна завантажити в корінь GitHub-репозиторію з заміною однойменних файлів. Не завантажуйте `__pycache__`, локальні `data/*.db` або `exports/*`.
