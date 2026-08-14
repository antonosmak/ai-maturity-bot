from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import matplotlib.pyplot as plt
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
MATRIX = json.loads((BASE_DIR / "matrix.json").read_text(encoding="utf-8"))
DIMENSIONS = []
for item in MATRIX:
    if item["dimension"] not in [d[0] for d in DIMENSIONS]:
        DIMENSIONS.append((item["dimension"], item["dimension_name"]))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "").strip()
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "ai_maturity.db")))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "30"))

if not TOKEN:
    raise SystemExit("Не задано TELEGRAM_BOT_TOKEN. Див. .env.example / README.md")

API = f"https://api.telegram.org/bot{TOKEN}"

SCALE = {
    0: "Відсутність",
    1: "Початковий",
    2: "Фрагментарний",
    3: "Системний",
    4: "Інтегрований",
    5: "Трансформаційний",
}

DIM_RECOMMENDATIONS = {
    "D1": "Формалізувати стратегічні цілі застосування ШІ, визначити пріоритетні сценарії, ресурси та KPI публічної цінності.",
    "D2": "Уточнити правові підстави та внутрішні правила застосування ШІ, розподіл відповідальності, документування, захист даних і механізми оскарження.",
    "D3": "Інституціоналізувати AI Governance: визначити відповідального суб’єкта, порядок погодження AI-ініціатив, реєстр систем/сценаріїв та міжпідрозділову координацію.",
    "D4": "Провести інвентаризацію даних, підвищити їх якість, структурованість і метадані, забезпечити API/інтероперабельність та правила життєвого циклу даних.",
    "D5": "Підготувати ІТ-архітектуру, тестові середовища, журналювання, резервування, масштабування та безпечне виведення AI-компонентів з експлуатації.",
    "D6": "Розгорнути диференційоване навчання з AI literacy для керівників, предметних фахівців і технічного персоналу та практики критичної перевірки результатів ШІ.",
    "D7": "Запровадити ризик-орієнтоване оцінювання AI-систем, вимоги кібербезпеки, недискримінації, прозорості, моніторингу, аудиту та процедур припинення використання.",
    "D8": "Формалізувати людино-машинну взаємодію: розподіл функцій, Human-in-the-Loop, право втручання, людську перевірку та персоніфіковану відповідальність.",
}


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                organization TEXT,
                status TEXT NOT NULL DEFAULT 'await_org',
                current_index INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                validation_score INTEGER,
                validation_comment TEXT,
                recommendations TEXT
            );
            CREATE TABLE IF NOT EXISTS answers (
                assessment_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                score INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'respondent',
                evidence TEXT,
                rationale TEXT,
                confidence REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (assessment_id, code)
            );
            CREATE INDEX IF NOT EXISTS idx_assess_chat ON assessments(chat_id, id DESC);
            """
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def maturity_level(aimi: float) -> str:
    if aimi <= 20:
        return "I — Початковий"
    if aimi <= 40:
        return "II — Фрагментарний"
    if aimi <= 60:
        return "III — Системний"
    if aimi <= 80:
        return "IV — Інтегрований"
    return "V — Трансформаційний"


def calc_results(assessment_id: int) -> dict[str, Any]:
    with db() as c:
        rows = c.execute("SELECT code, score FROM answers WHERE assessment_id=?", (assessment_id,)).fetchall()
    scores = {r["code"]: int(r["score"]) for r in rows}
    dims: dict[str, float] = {}
    sums: dict[str, int] = {}
    counts: dict[str, int] = {}
    for dim, _name in DIMENSIONS:
        codes = [x["code"] for x in MATRIX if x["dimension"] == dim]
        vals = [scores[c] for c in codes if c in scores]
        sums[dim] = sum(vals)
        counts[dim] = len(vals)
        dims[dim] = (sum(vals) / (5 * len(codes)) * 100.0) if len(vals) == len(codes) else 0.0
    complete = len(scores) == len(MATRIX)
    aimi = sum(dims.values()) / 8 if complete else 0.0
    return {"dims": dims, "sums": sums, "counts": counts, "aimi": aimi, "complete": complete, "scores": scores}


def make_radar(assessment_id: int, organization: str) -> Path:
    r = calc_results(assessment_id)
    labels = [d[0] for d in DIMENSIONS]
    values = [r["dims"][d] for d in labels]
    vals = values + values[:1]
    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    angles += angles[:1]

    fig = plt.figure(figsize=(8, 8))
    ax = plt.subplot(111, polar=True)
    ax.plot(angles, vals, linewidth=2)
    ax.fill(angles, vals, alpha=0.15)
    ax.set_thetagrids(np.degrees(angles[:-1]), labels)
    ax.set_ylim(0, 100)
    ax.set_yticks([20, 40, 60, 80, 100])
    ax.set_title(f"Профіль AI-зрілості\n{organization}\nAIMI = {r['aimi']:.1f}%", pad=25)
    out = BASE_DIR / "exports" / f"radar_{assessment_id}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return out


def base_recommendations(assessment_id: int) -> str:
    r = calc_results(assessment_id)
    ranked = sorted(r["dims"].items(), key=lambda kv: kv[1])
    lines = ["ПРІОРИТЕТНІ РЕКОМЕНДАЦІЇ"]
    for n, (dim, value) in enumerate(ranked[:4], 1):
        lines.append(f"{n}. {dim} ({value:.1f}%): {DIM_RECOMMENDATIONS[dim]}")
    low_items = []
    for item in MATRIX:
        sc = r["scores"].get(item["code"])
        if sc is not None and sc <= 2:
            low_items.append((sc, item))
    low_items.sort(key=lambda x: (x[0], x[1]["code"]))
    if low_items:
        lines.append("\nКритичні/слабкі показники:")
        for sc, item in low_items[:10]:
            lines.append(f"• {item['code']} — {sc}/5: {item['criterion']}")
    return "\n".join(lines)


async def ai_recommendations(assessment_id: int, organization: str) -> str:
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        return base_recommendations(assessment_id)
    r = calc_results(assessment_id)
    low = []
    for item in MATRIX:
        sc = r["scores"].get(item["code"])
        if sc is not None and sc <= 3:
            low.append({"code": item["code"], "criterion": item["criterion"], "score": sc, "statement": item["statement"]})
    prompt = f"""
Ти — аналітичний модуль методики оцінювання AI-зрілості органу публічної влади.
Орган: {organization}
AIMI: {r['aimi']:.2f}% ({maturity_level(r['aimi'])})
D1-D8: {json.dumps(r['dims'], ensure_ascii=False)}
Слабкі показники: {json.dumps(low, ensure_ascii=False)}

Сформуй українською мовою практичні рекомендації для підвищення AI-зрілості.
Вимоги:
- не змінюй і не переобчислюй оцінки;
- спирайся лише на наведені результати;
- розділи рекомендації на: першочергові (0–6 міс.), середньострокові (6–18 міс.), стратегічні (18+ міс.);
- для кожної рекомендації вкажи, які D/коди вона покращує;
- окремо познач критичні обмеження D2, D7, D8;
- не стверджуй, що певна внутрішня практика існує, якщо це не випливає з оцінок;
- обсяг до 700 слів.
""".strip()
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "input": prompt}
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"].strip()
    texts = []
    for out in data.get("output", []):
        for content in out.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                texts.append(content["text"])
    return "\n".join(texts).strip() or base_recommendations(assessment_id)


async def tg(method: str, payload: dict | None = None, files: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=max(POLL_TIMEOUT + 10, 45)) as client:
        if files:
            r = await client.post(f"{API}/{method}", data=payload or {}, files=files)
        else:
            r = await client.post(f"{API}/{method}", json=payload or {})
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data)
        return data.get("result")


async def send(chat_id: int, text: str, keyboard: list[list[dict]] | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    await tg("sendMessage", payload)


async def send_photo(chat_id: int, path: Path, caption: str = "") -> None:
    with path.open("rb") as f:
        await tg("sendPhoto", {"chat_id": str(chat_id), "caption": caption}, {"photo": (path.name, f, "image/png")})


async def send_document(chat_id: int, path: Path, caption: str = "") -> None:
    with path.open("rb") as f:
        await tg("sendDocument", {"chat_id": str(chat_id), "caption": caption}, {"document": (path.name, f, "application/json")})


def score_keyboard(assessment_id: int, idx: int) -> list[list[dict]]:
    return [[{"text": str(s), "callback_data": f"score:{assessment_id}:{idx}:{s}"} for s in range(6)]]


def validation_keyboard(assessment_id: int) -> list[list[dict]]:
    return [[{"text": str(s), "callback_data": f"valid:{assessment_id}:{s}"} for s in range(1, 6)]]


async def ask_question(chat_id: int, assessment_id: int, idx: int) -> None:
    item = MATRIX[idx]
    title = f"{idx+1}/{len(MATRIX)}  {item['code']} — {item['criterion']}\n{item['dimension_name']}"
    text = f"{title}\n\n{item['statement']}\n\nОцініть 0–5:"
    await send(chat_id, text, score_keyboard(assessment_id, idx))


async def start_assessment(chat_id: int, user: dict) -> None:
    with db() as c:
        active = c.execute("SELECT id FROM assessments WHERE chat_id=? AND status IN ('await_org','running') ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if active:
            await send(chat_id, f"У вас уже є незавершене оцінювання №{active['id']}. Надішліть назву органу або продовжуйте відповідати. /cancel — скасувати.")
            return
        cur = c.execute(
            "INSERT INTO assessments(chat_id,user_id,username,status,current_index,created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, user.get("id"), user.get("username"), "await_org", 0, now_iso()),
        )
        aid = cur.lastrowid
    await send(chat_id, f"Розпочато оцінювання №{aid}.\n\nНадішліть назву органу публічної влади, який оцінюється.")


async def finish_assessment(chat_id: int, assessment_id: int) -> None:
    with db() as c:
        a = c.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
        c.execute("UPDATE assessments SET status='await_validation', finished_at=? WHERE id=?", (now_iso(), assessment_id))
    r = calc_results(assessment_id)
    chart = make_radar(assessment_id, a["organization"] or "Орган")
    summary = [f"Оцінювання №{assessment_id} завершено.", f"Орган: {a['organization']}", f"AIMI: {r['aimi']:.1f}%", f"Рівень: {maturity_level(r['aimi'])}", ""]
    summary.extend(f"{d}: {r['dims'][d]:.1f}%" for d, _ in DIMENSIONS)
    await send_photo(chat_id, chart, "Профіль AI-зрілості")
    await send(chat_id, "\n".join(summary))
    await send(chat_id, "Наскільки отриманий профіль відповідає фактичному стану органу?\n1 — зовсім не відповідає; 5 — повністю відповідає.", validation_keyboard(assessment_id))


async def show_report(chat_id: int, assessment_id: int) -> None:
    with db() as c:
        a = c.execute("SELECT * FROM assessments WHERE id=? AND chat_id=?", (assessment_id, chat_id)).fetchone()
    if not a:
        await send(chat_id, "Оцінювання не знайдено.")
        return
    r = calc_results(assessment_id)
    if not r["complete"]:
        await send(chat_id, f"Оцінювання №{assessment_id} ще не завершено ({len(r['scores'])}/48 відповідей).")
        return
    chart = make_radar(assessment_id, a["organization"] or "Орган")
    text = [f"ЗВІТ №{assessment_id}", f"Орган: {a['organization']}", f"AIMI: {r['aimi']:.1f}% — {maturity_level(r['aimi'])}"]
    text.extend(f"{d}: {r['dims'][d]:.1f}%" for d, _ in DIMENSIONS)
    if a["validation_score"]:
        text.append(f"Відповідність за оцінкою респондента: {a['validation_score']}/5")
    await send_photo(chat_id, chart, "Профіль AI-зрілості")
    await send(chat_id, "\n".join(text))
    rec = a["recommendations"] or base_recommendations(assessment_id)
    await send(chat_id, rec[:4000])


def export_json(assessment_id: int) -> Path:
    with db() as c:
        a = dict(c.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone())
        ans = [dict(x) for x in c.execute("SELECT * FROM answers WHERE assessment_id=? ORDER BY code", (assessment_id,)).fetchall()]
    r = calc_results(assessment_id)
    data = {"assessment": a, "results": {k:v for k,v in r.items() if k != "scores"}, "answers": ans}
    out = BASE_DIR / "exports" / f"assessment_{assessment_id}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


async def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text in ("/start", "/help"):
        await send(chat_id,
            "AI Maturity Bot — прототип оцінювання готовності органу публічної влади.\n\n"
            "/new — нове оцінювання (48 показників)\n"
            "/status — стан поточного оцінювання\n"
            "/log — останні оцінювання\n"
            "/report [ID] — звіт\n"
            "/recommend [ID] — сформувати рекомендації\n"
            "/export [ID] — вивантажити audit log JSON\n"
            "/cancel — скасувати поточне оцінювання")
        return
    if text == "/new":
        await start_assessment(chat_id, user); return
    if text == "/cancel":
        with db() as c:
            row = c.execute("SELECT id FROM assessments WHERE chat_id=? AND status IN ('await_org','running') ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
            if row: c.execute("UPDATE assessments SET status='cancelled' WHERE id=?", (row["id"],))
        await send(chat_id, "Поточне оцінювання скасовано." if row else "Немає активного оцінювання.")
        return
    if text == "/status":
        with db() as c:
            a = c.execute("SELECT * FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if not a:
            await send(chat_id, "Оцінювань ще немає. /new")
        else:
            r = calc_results(a["id"])
            await send(chat_id, f"№{a['id']} | {a['organization'] or 'орган не задано'} | статус: {a['status']} | відповідей: {len(r['scores'])}/48")
        return
    if text == "/log":
        with db() as c:
            rows = c.execute("SELECT * FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 10", (chat_id,)).fetchall()
        if not rows:
            await send(chat_id, "Журнал порожній.")
        else:
            lines=["Останні оцінювання:"]
            for a in rows:
                r=calc_results(a["id"])
                val=f" | AIMI {r['aimi']:.1f}%" if r["complete"] else ""
                lines.append(f"№{a['id']} — {a['organization'] or 'без назви'} — {a['status']}{val}")
            await send(chat_id, "\n".join(lines))
        return
    if text.startswith("/report"):
        parts=text.split()
        if len(parts)>1 and parts[1].isdigit(): aid=int(parts[1])
        else:
            with db() as c: row=c.execute("SELECT id FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()
            if not row: await send(chat_id,"Немає оцінювань."); return
            aid=row["id"]
        await show_report(chat_id, aid); return
    if text.startswith("/export"):
        parts=text.split()
        if len(parts)>1 and parts[1].isdigit(): aid=int(parts[1])
        else:
            with db() as c: row=c.execute("SELECT id FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()
            if not row: await send(chat_id,"Немає оцінювань."); return
            aid=row["id"]
        with db() as c: own=c.execute("SELECT 1 FROM assessments WHERE id=? AND chat_id=?",(aid,chat_id)).fetchone()
        if not own: await send(chat_id,"Оцінювання не знайдено."); return
        out=export_json(aid)
        await send_document(chat_id,out,"Audit log оцінювання"); return
    if text.startswith("/recommend"):
        parts=text.split()
        if len(parts)>1 and parts[1].isdigit(): aid=int(parts[1])
        else:
            with db() as c: row=c.execute("SELECT id,organization FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()
            if not row: await send(chat_id,"Немає оцінювань."); return
            aid=row["id"]
        with db() as c: a=c.execute("SELECT * FROM assessments WHERE id=? AND chat_id=?",(aid,chat_id)).fetchone()
        if not a: await send(chat_id,"Оцінювання не знайдено."); return
        r=calc_results(aid)
        if not r["complete"]: await send(chat_id,"Спочатку завершіть 48 оцінок."); return
        await send(chat_id,"Формую рекомендації…")
        try:
            rec=await ai_recommendations(aid,a["organization"] or "Орган")
        except Exception as e:
            rec=base_recommendations(aid)+f"\n\nAI-модуль недоступний: {type(e).__name__}."
        with db() as c: c.execute("UPDATE assessments SET recommendations=? WHERE id=?",(rec,aid))
        for chunk in textwrap.wrap(rec, 3900, replace_whitespace=False, drop_whitespace=False):
            await send(chat_id,chunk)
        return

    # назва органу для щойно створеного оцінювання
    with db() as c:
        a = c.execute("SELECT * FROM assessments WHERE chat_id=? AND status='await_org' ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if a:
            c.execute("UPDATE assessments SET organization=?, status='running' WHERE id=?", (text[:500], a["id"]))
    if a:
        await send(chat_id, f"Орган: {text}\nПочинаємо оцінювання. Шкала 0–5.")
        await ask_question(chat_id, a["id"], 0)
        return

    # коментар до валідації
    with db() as c:
        a = c.execute("SELECT * FROM assessments WHERE chat_id=? AND status='await_validation_comment' ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if a:
            c.execute("UPDATE assessments SET validation_comment=?, status='finished' WHERE id=?", (text[:2000], a["id"]))
    if a:
        await send(chat_id, "Коментар збережено. /report або /recommend")
        return

    await send(chat_id, "Не розпізнав команду. /help")


async def handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    await tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
    parts = data.split(":")
    if parts[0] == "score" and len(parts) == 4:
        aid, idx, score = map(int, parts[1:])
        if idx < 0 or idx >= len(MATRIX) or score not in range(6): return
        item=MATRIX[idx]
        with db() as c:
            a=c.execute("SELECT * FROM assessments WHERE id=? AND chat_id=?",(aid,chat_id)).fetchone()
            if not a or a["status"]!='running': return
            c.execute("INSERT OR REPLACE INTO answers(assessment_id,code,score,source,created_at) VALUES(?,?,?,?,?)",(aid,item["code"],score,"respondent",now_iso()))
            c.execute("UPDATE assessments SET current_index=? WHERE id=?",(idx+1,aid))
        await send(chat_id, f"{item['code']}: {score}/5 — {SCALE[score]}")
        if idx+1 < len(MATRIX):
            await ask_question(chat_id,aid,idx+1)
        else:
            await finish_assessment(chat_id,aid)
        return
    if parts[0] == "valid" and len(parts)==3:
        aid=int(parts[1]); score=int(parts[2])
        if score not in range(1,6): return
        with db() as c:
            a=c.execute("SELECT * FROM assessments WHERE id=? AND chat_id=?",(aid,chat_id)).fetchone()
            if not a: return
            c.execute("UPDATE assessments SET validation_score=?, status='await_validation_comment' WHERE id=?",(score,aid))
        await send(chat_id, f"Відповідність: {score}/5.\nЗа бажанням надішліть короткий коментар, що саме оцінено неточно. Якщо коментар не потрібен — надішліть «-».")


async def process_update(upd: dict) -> None:
    if "message" in upd:
        await handle_message(upd["message"])
    elif "callback_query" in upd:
        await handle_callback(upd["callback_query"])


async def main() -> None:
    # Локальний режим long polling. На Render використовується app.py + webhook.
    init_db()
    print("AI Maturity Bot started in polling mode")
    offset = 0
    while True:
        try:
            updates = await tg("getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]})
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                try:
                    await process_update(upd)
                except Exception as e:
                    print("Update error:", repr(e))
        except Exception as e:
            print("Polling error:", repr(e))
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
