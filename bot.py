from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import textwrap
import zipfile
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import matplotlib.pyplot as plt
import numpy as np
import xlsxwriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether

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
GDRIVE_UPLOAD_URL = os.environ.get("GDRIVE_UPLOAD_URL", "").strip()
GDRIVE_UPLOAD_SECRET = os.environ.get("GDRIVE_UPLOAD_SECRET", "").strip()

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
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as f:
        await tg("sendDocument", {"chat_id": str(chat_id), "caption": caption}, {"document": (path.name, f, mime)})


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



def _assessment_payload(assessment_id: int) -> tuple[dict, list[dict], dict]:
    with db() as c:
        row = c.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
        if not row:
            raise ValueError("Assessment not found")
        a = dict(row)
        ans = [dict(x) for x in c.execute("SELECT * FROM answers WHERE assessment_id=? ORDER BY code", (assessment_id,)).fetchall()]
    r = calc_results(assessment_id)
    by_code = {x["code"]: x for x in ans}
    for item in MATRIX:
        x = by_code.get(item["code"], {})
        item_score = x.get("score")
        x["dimension"] = item["dimension"]
        x["dimension_name"] = item["dimension_name"]
        x["criterion"] = item["criterion"]
        x["statement"] = item["statement"]
        x["score"] = item_score
        by_code[item["code"]] = x
    ordered = [by_code[item["code"]] for item in MATRIX]
    return a, ordered, r


def export_xlsx(assessment_id: int) -> Path:
    a, answers, r = _assessment_payload(assessment_id)
    out = BASE_DIR / "exports" / f"AI_Maturity_Assessment_{assessment_id}.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = xlsxwriter.Workbook(out)
    ws = wb.add_worksheet("Звіт")
    detail = wb.add_worksheet("48 показників")
    scale = wb.add_worksheet("Шкала")
    fmt_title = wb.add_format({"bold": True, "font_size": 16, "align": "center", "valign": "vcenter"})
    fmt_h = wb.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
    fmt_txt = wb.add_format({"border": 1, "valign": "top", "text_wrap": True})
    fmt_num = wb.add_format({"border": 1, "align": "center", "num_format": "0.0%"})
    fmt_pct = wb.add_format({"border": 1, "align": "center", "num_format": "0.0"})
    fmt_score = wb.add_format({"border": 1, "align": "center"})
    fmt_kpi = wb.add_format({"bold": True, "font_size": 14, "align": "center", "border": 1, "bg_color": "#EEF5EA"})
    ws.merge_range("A1:F1", "AI Maturity Assessment — звіт", fmt_title)
    ws.write("A3", "Орган", fmt_h); ws.merge_range("B3:F3", a.get("organization") or "", fmt_txt)
    ws.write("A4", "Оцінювання №", fmt_h); ws.write("B4", assessment_id, fmt_txt)
    ws.write("C4", "Дата завершення", fmt_h); ws.write("D4", a.get("finished_at") or "", fmt_txt)
    ws.write("E4", "Верифікація", fmt_h); ws.write("F4", f"{a.get('validation_score') or '-'} / 5", fmt_txt)
    ws.write("A6", "AIMI", fmt_h); ws.write("B6", r["aimi"], fmt_kpi)
    ws.write("C6", "Рівень", fmt_h); ws.merge_range("D6:F6", maturity_level(r["aimi"]), fmt_kpi)
    ws.write_row("A8", ["Код", "Вимір", "Dᵢ, %", "Сума балів", "Макс. бал", "Пріоритет"], fmt_h)
    ranked = {d: i+1 for i,(d,_) in enumerate(sorted(r["dims"].items(), key=lambda kv: kv[1]))}
    for row,(d,name) in enumerate(DIMENSIONS, 8):
        ws.write(row,0,d,fmt_txt); ws.write(row,1,name,fmt_txt); ws.write(row,2,r["dims"][d],fmt_pct)
        ws.write(row,3,r["sums"][d],fmt_score); ws.write(row,4,30,fmt_score); ws.write(row,5,ranked[d],fmt_score)
    chart = wb.add_chart({"type":"radar", "subtype":"filled"})
    chart.add_series({"name":"AI-зрілість", "categories":"='Звіт'!$A$9:$A$16", "values":"='Звіт'!$C$9:$C$16"})
    chart.set_title({"name":"Профіль AI-зрілості"}); chart.set_legend({"none": True}); chart.set_size({"width": 620, "height": 380})
    ws.insert_chart("H3", chart)
    rec = a.get("recommendations") or base_recommendations(assessment_id)
    ws.write("A18", "Рекомендації", fmt_h); ws.merge_range("B18:F18", rec, fmt_txt)
    if a.get("validation_comment"):
        ws.write("A20", "Коментар верифікації", fmt_h); ws.merge_range("B20:F20", a["validation_comment"], fmt_txt)
    ws.set_column("A:A", 18); ws.set_column("B:B", 42); ws.set_column("C:F", 16); ws.set_row(17, 95)

    detail.write_row(0,0,["Код","Вимір","Критерій","Діагностичне твердження","Оцінка 0–5","Рівень","Джерело","Підстава / evidence","Обґрунтування","Confidence"],fmt_h)
    for i,x in enumerate(answers,1):
        score = x.get("score")
        vals=[x.get("code"),x.get("dimension_name"),x.get("criterion"),x.get("statement"),score,SCALE.get(score,""),x.get("source") or "",x.get("evidence") or "",x.get("rationale") or "",x.get("confidence")]
        for j,v in enumerate(vals): detail.write(i,j,v,fmt_score if j in (4,9) else fmt_txt)
    detail.freeze_panes(1,0); detail.autofilter(0,0,len(answers),9)
    detail.set_column(0,0,9); detail.set_column(1,2,28); detail.set_column(3,3,65); detail.set_column(4,5,13); detail.set_column(6,9,24)
    scale.write_row(0,0,["Бал","Рівень","Зміст"],fmt_h)
    descr={0:"Практика, спроможність або механізм відсутні",1:"Окремі неформальні дії або ініціативи без системного характеру",2:"Окремі елементи запроваджено лише в частині підрозділів або процесів",3:"Практика формалізована, застосовується регулярно й охоплює основні відповідні процеси",4:"Практика інтегрована в систему управління, забезпечена ресурсами, контролем і взаємодією",5:"Спроможність є невід’ємним компонентом управління, регулярно оцінюється та вдосконалюється"}
    for i in range(6): scale.write_row(i+1,0,[i,SCALE[i],descr[i]],fmt_txt)
    scale.set_column(0,0,8); scale.set_column(1,1,24); scale.set_column(2,2,80)
    wb.close()
    return out


def _register_pdf_fonts() -> tuple[str,str]:
    regular_candidates=["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
    bold_candidates=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]
    reg=next((x for x in regular_candidates if Path(x).exists()), None)
    bold=next((x for x in bold_candidates if Path(x).exists()), None)
    if reg:
        if "DejaVu" not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont("DejaVu",reg))
        if bold and "DejaVuBold" not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont("DejaVuBold",bold))
        return "DejaVu", "DejaVuBold" if bold else "DejaVu"
    return "Helvetica", "Helvetica-Bold"


def export_pdf(assessment_id: int) -> Path:
    a, answers, r = _assessment_payload(assessment_id)
    out = BASE_DIR / "exports" / f"AI_Maturity_Assessment_{assessment_id}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    font,bold = _register_pdf_fonts()
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="UABody", parent=styles["BodyText"], fontName=font, fontSize=9, leading=12, spaceAfter=4))
    styles.add(ParagraphStyle(name="UAHead", parent=styles["Heading2"], fontName=bold, fontSize=13, leading=16, spaceBefore=8, spaceAfter=7))
    styles.add(ParagraphStyle(name="UATitle", parent=styles["Title"], fontName=bold, fontSize=17, leading=21, alignment=TA_CENTER, spaceAfter=12))
    styles.add(ParagraphStyle(name="UASmall", parent=styles["BodyText"], fontName=font, fontSize=7.5, leading=9.5))
    doc=SimpleDocTemplate(str(out), pagesize=A4, rightMargin=14*mm,leftMargin=14*mm,topMargin=14*mm,bottomMargin=14*mm, title=f"AI Maturity Assessment #{assessment_id}")
    story=[]
    story += [Paragraph("AI Maturity Assessment", styles["UATitle"]), Paragraph(f"Звіт за результатами оцінювання AI-зрілості органу публічної влади", styles["UAHead"])]
    meta=[["Орган", a.get("organization") or ""],["Оцінювання", f"№ {assessment_id}"],["Дата завершення", a.get("finished_at") or ""],["AIMI", f"{r['aimi']:.1f}%"],["Рівень", maturity_level(r["aimi"])],["Відповідність результату", f"{a.get('validation_score') or '-'} / 5"]]
    t=Table([[Paragraph(str(c),styles["UABody"]) for c in row] for row in meta], colWidths=[45*mm,125*mm])
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#E8EEF5")),("FONTNAME",(0,0),(-1,-1),font),("FONTNAME",(0,0),(0,-1),bold),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5)]))
    story += [t, Spacer(1,7*mm)]
    chart=make_radar(assessment_id,a.get("organization") or "Орган")
    story += [Image(str(chart), width=140*mm, height=140*mm), Spacer(1,4*mm), Paragraph("Профіль AI-зрілості за вісьмома вимірами", styles["UABody"])]
    story += [Paragraph("Результати за вимірами", styles["UAHead"])]
    data=[["Код","Вимір","Dᵢ, %","Сума","Макс."]]
    for d,name in DIMENSIONS: data.append([d,name,f"{r['dims'][d]:.1f}",str(r['sums'][d]),"30"])
    t=Table([[Paragraph(str(c),styles["UASmall"]) for c in row] for row in data], colWidths=[13*mm,100*mm,19*mm,19*mm,19*mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.35,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#D9EAF7")),("FONTNAME",(0,0),(-1,0),bold),("ALIGN",(2,1),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [t, PageBreak(), Paragraph("Деталізація 48 діагностичних показників", styles["UAHead"])]
    ddata=[["Код","Критерій","Твердження","Бал"]]
    for x in answers: ddata.append([x["code"],x["criterion"],x["statement"],str(x.get("score") if x.get("score") is not None else "-")])
    t=Table([[Paragraph(str(c),styles["UASmall"]) for c in row] for row in ddata], colWidths=[14*mm,42*mm,105*mm,12*mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.25,colors.HexColor("#999999")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#D9EAF7")),("FONTNAME",(0,0),(-1,0),bold),("VALIGN",(0,0),(-1,-1),"TOP"),("ALIGN",(-1,1),(-1,-1),"CENTER")]))
    story += [t, PageBreak(), Paragraph("Рекомендації для підвищення AI-зрілості", styles["UAHead"])]
    rec=a.get("recommendations") or base_recommendations(assessment_id)
    for para in rec.split("\n"):
        if para.strip(): story.append(Paragraph(para.replace("&","&amp;"), styles["UABody"]))
    if a.get("validation_comment"):
        story += [Spacer(1,5*mm), Paragraph("Верифікація результату", styles["UAHead"]), Paragraph(f"Оцінка відповідності: {a.get('validation_score')}/5", styles["UABody"]), Paragraph(a["validation_comment"].replace("&","&amp;"), styles["UABody"])]
    story += [Spacer(1,6*mm), Paragraph("Примітка: інтегральний AIMI не повинен компенсувати критично низькі значення D2, D7 і D8 для сценаріїв із істотним впливом на права, свободи, обов’язки або юридично значущі рішення.", styles["UASmall"])]
    doc.build(story)
    return out


def export_bundle(assessment_id: int) -> Path:
    pdf=export_pdf(assessment_id); xlsx=export_xlsx(assessment_id); js=export_json(assessment_id)
    a,_,_= _assessment_payload(assessment_id)
    radar=make_radar(assessment_id,a.get("organization") or "Орган")
    out=BASE_DIR/"exports"/f"AI_Maturity_Assessment_{assessment_id}_bundle.zip"
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        for p in (pdf,xlsx,js,radar): z.write(p,arcname=p.name)
    return out


async def upload_to_google_drive(paths: list[Path]) -> list[dict]:
    """Upload files through the configured Google Apps Script web app.

    The Apps Script receives JSON with a shared secret, filename, MIME type and
    base64 payload, then writes the file into the configured Drive folder.
    """
    if not GDRIVE_UPLOAD_URL or not GDRIVE_UPLOAD_SECRET:
        raise RuntimeError("Google Drive шлюз не налаштовано: потрібні GDRIVE_UPLOAD_URL і GDRIVE_UPLOAD_SECRET")

    uploaded: list[dict] = []
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for p in paths:
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            payload = {
                "secret": GDRIVE_UPLOAD_SECRET,
                "filename": p.name,
                "mime_type": mime,
                "file_base64": base64.b64encode(p.read_bytes()).decode("ascii"),
            }
            resp = await client.post(GDRIVE_UPLOAD_URL, json=payload)
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception as exc:
                raise RuntimeError(f"Apps Script повернув не JSON: {resp.text[:300]}") from exc
            if not data.get("ok"):
                raise RuntimeError(data.get("error") or f"Помилка Google Drive для {p.name}")
            uploaded.append({
                "id": data.get("file_id", ""),
                "name": data.get("filename") or p.name,
                "webViewLink": data.get("url", ""),
            })
    return uploaded


async def finalize_and_archive(assessment_id: int, chat_id: int) -> None:
    """Create the final PDF, send it to Telegram and archive a copy to Drive."""
    with db() as c:
        a = c.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not a:
        return

    # Ensure the PDF always contains at least deterministic base recommendations.
    if not a["recommendations"]:
        rec = base_recommendations(assessment_id)
        with db() as c:
            c.execute("UPDATE assessments SET recommendations=? WHERE id=?", (rec, assessment_id))

    pdf = export_pdf(assessment_id)
    await send_document(chat_id, pdf, "Фінальний PDF-звіт AI-зрілості")

    if not GDRIVE_UPLOAD_URL or not GDRIVE_UPLOAD_SECRET:
        await send(chat_id, "PDF сформовано. Google Drive-архів не налаштовано.")
        return

    try:
        uploaded = await upload_to_google_drive([pdf])
        link = uploaded[0].get("webViewLink", "") if uploaded else ""
        msg = "Копію фінального PDF автоматично збережено в Google Drive."
        if link:
            msg += f"\n{link}"
        await send(chat_id, msg)
    except Exception as exc:
        print("Drive auto-upload error:", repr(exc), flush=True)
        await send(chat_id, f"PDF сформовано, але автоматичне архівування в Google Drive не вдалося: {type(exc).__name__}.")

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
            "/report [ID] — короткий звіт\n"
            "/pdf [ID] — PDF-звіт\n"
            "/xlsx [ID] — Excel-звіт\n"
            "/bundle [ID] — пакет PDF+XLSX+JSON+Radar\n"
            "/drive [ID] — архівувати пакет у Google Drive\n"
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
    if text.startswith(("/pdf", "/xlsx", "/bundle", "/drive")):
        cmd=text.split()[0]
        parts=text.split()
        if len(parts)>1 and parts[1].isdigit(): aid=int(parts[1])
        else:
            with db() as c: row=c.execute("SELECT id FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()
            if not row: await send(chat_id,"Немає оцінювань."); return
            aid=row["id"]
        with db() as c: a=c.execute("SELECT * FROM assessments WHERE id=? AND chat_id=?",(aid,chat_id)).fetchone()
        if not a: await send(chat_id,"Оцінювання не знайдено."); return
        r=calc_results(aid)
        if not r["complete"]: await send(chat_id,"Спочатку завершіть 48 оцінок."); return
        await send(chat_id,"Формую файл звіту…")
        try:
            if cmd=="/pdf": out=export_pdf(aid); await send_document(chat_id,out,"Повний PDF-звіт AI-зрілості")
            elif cmd=="/xlsx": out=export_xlsx(aid); await send_document(chat_id,out,"Excel-звіт AI-зрілості")
            elif cmd=="/bundle": out=export_bundle(aid); await send_document(chat_id,out,"Повний пакет оцінювання")
            else:
                pdf=export_pdf(aid); xlsx=export_xlsx(aid); js=export_json(aid); radar=make_radar(aid,a["organization"] or "Орган")
                uploaded=await upload_to_google_drive([pdf,xlsx,js,radar])
                lines=["Звіт архівовано в Google Drive:"]+[f"• {x.get('name')} — {x.get('webViewLink','') or x.get('id')}" for x in uploaded]
                await send(chat_id,"\n".join(lines))
        except Exception as e:
            await send(chat_id,f"Не вдалося сформувати/завантажити звіт: {type(e).__name__}: {e}")
        return

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
        await send(chat_id, "Коментар збережено. Формую фінальний PDF-звіт…")
        await finalize_and_archive(a["id"], chat_id)
        await send(chat_id, "Додатково доступні: /report, /pdf, /xlsx, /bundle, /drive, /recommend")
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
