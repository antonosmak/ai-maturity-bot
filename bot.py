from __future__ import annotations
import base64, json, mimetypes, os, re, sqlite3, tempfile, textwrap, zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
import httpx
import matplotlib.pyplot as plt
import numpy as np
import xlsxwriter
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak

BASE_DIR=Path(__file__).resolve().parent
MATRIX=json.loads((BASE_DIR/'matrix.json').read_text(encoding='utf-8'))
DIMENSIONS=[]
for x in MATRIX:
    if x['dimension'] not in [d[0] for d in DIMENSIONS]: DIMENSIONS.append((x['dimension'],x['dimension_name']))
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); OPENAI_API_KEY=os.getenv('OPENAI_API_KEY','').strip(); OPENAI_MODEL=os.getenv('OPENAI_MODEL','').strip()
GDRIVE_UPLOAD_URL=os.getenv('GDRIVE_UPLOAD_URL','').strip(); GDRIVE_UPLOAD_SECRET=os.getenv('GDRIVE_UPLOAD_SECRET','').strip()
DB_PATH=Path(os.getenv('DB_PATH',str(BASE_DIR/'data'/'ai_maturity.db'))); POLL_TIMEOUT=int(os.getenv('POLL_TIMEOUT','30'))
if not TOKEN: raise SystemExit('Не задано TELEGRAM_BOT_TOKEN')
API=f'https://api.telegram.org/bot{TOKEN}'
SCALE={0:'Відсутність',1:'Початковий',2:'Фрагментарний',3:'Системний',4:'Інтегрований',5:'Трансформаційний'}
MODE_LABEL={'ai':'ШІ-аналіз','hybrid':'ШІ-аналіз з підкріпленням','manual':'Ручне оцінювання'}

def db():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c

def _col(c,t,n,decl):
    cols={r['name'] for r in c.execute(f'PRAGMA table_info({t})')}
    if n not in cols: c.execute(f'ALTER TABLE {t} ADD COLUMN {n} {decl}')

def init_db():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS assessments(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,user_id INTEGER,username TEXT,organization TEXT,status TEXT NOT NULL DEFAULT 'await_mode',current_index INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,finished_at TEXT,validation_score INTEGER,validation_comment TEXT,recommendations TEXT); CREATE TABLE IF NOT EXISTS answers(assessment_id INTEGER NOT NULL,code TEXT NOT NULL,score INTEGER NOT NULL,source TEXT NOT NULL DEFAULT 'respondent',evidence TEXT,rationale TEXT,confidence REAL,created_at TEXT NOT NULL,PRIMARY KEY(assessment_id,code));''')
        _col(c,'assessments','mode','TEXT'); _col(c,'assessments','official_url','TEXT'); _col(c,'assessments','coverage','REAL'); _col(c,'assessments','ai_note','TEXT')

def now_iso(): return datetime.now(timezone.utc).isoformat()
def maturity_level(v):
    return 'I — Початковий' if v<=20 else 'II — Фрагментарний' if v<=40 else 'III — Системний' if v<=60 else 'IV — Інтегрований' if v<=80 else 'V — Трансформаційний'

def calc_results(aid):
    with db() as c: rows=c.execute('SELECT code,score,source FROM answers WHERE assessment_id=?',(aid,)).fetchall()
    scores={r['code']:int(r['score']) for r in rows}; dims={}; sums={}; counts={}; totals={}
    for d,_ in DIMENSIONS:
        codes=[x['code'] for x in MATRIX if x['dimension']==d]; vals=[scores[k] for k in codes if k in scores]
        counts[d]=len(vals); totals[d]=len(codes); sums[d]=sum(vals); dims[d]=(sum(vals)/(5*len(vals))*100) if vals else None
    n=len(scores); coverage=n/len(MATRIX)*100; vals=[v for v in dims.values() if v is not None]
    aimi=sum(scores.values())/(5*n)*100 if n else 0
    return {'scores':scores,'dims':dims,'sums':sums,'counts':counts,'totals':totals,'aimi':aimi,'coverage':coverage,'complete':n==len(MATRIX)}

async def tg(method,payload=None,files=None):
    async with httpx.AsyncClient(timeout=max(POLL_TIMEOUT+10,45)) as c:
        r=await c.post(
            f'{API}/{method}',
            data=(payload or {}) if files else None,
            json=None if files else (payload or {}),
            files=files
        )
        if r.status_code >= 400:
            body=r.text[:1500]
            print(f'Telegram API error: method={method} status={r.status_code} body={body}',flush=True)
            raise RuntimeError(f'Telegram {method}: HTTP {r.status_code}: {body}')
        j=r.json()
        if not j.get('ok',True):
            raise RuntimeError(j)
        return j.get('result')
async def send(chat,text,keyboard=None):
    p={'chat_id':chat,'text':text}
    if keyboard:
        p['reply_markup']={'inline_keyboard':keyboard}
    await tg('sendMessage',p)

async def send_start_welcome(chat,text):
    p={
        'chat_id':chat,
        'text':text,
        'reply_markup':{
            'keyboard':[[{'text':'▶️ Розпочати оцінювання'}]],
            'resize_keyboard':True,
            'one_time_keyboard':False
        }
    }
    await tg('sendMessage',p)

async def configure_telegram_ui():
    # Persistent Telegram command menu. It remains available even when the
    # message history is cleared, unlike a reply keyboard that only appears
    # after the bot has sent a message.
    commands=[
        {'command':'start','description':'Старт / головний екран'},
        {'command':'new','description':'Розпочати нове оцінювання'},
        {'command':'status','description':'Стан поточного оцінювання'},
        {'command':'log','description':'Журнал оцінювань'},
        {'command':'help','description':'Довідка'}
    ]
    try:
        await tg('setMyCommands',{'commands':commands})
        await tg('setChatMenuButton',{'menu_button':{'type':'commands'}})
        print('Telegram command menu configured',flush=True)
    except Exception as e:
        # The bot itself must still start even if Telegram temporarily rejects
        # a UI configuration call.
        print('Telegram UI configuration warning:',repr(e),flush=True)
async def send_document(chat,path,caption=''):
    with path.open('rb') as f: await tg('sendDocument',{'chat_id':str(chat),'caption':caption},{'document':(path.name,f,mimetypes.guess_type(path.name)[0] or 'application/octet-stream')})
async def send_photo(chat,path,caption=''):
    with path.open('rb') as f: await tg('sendPhoto',{'chat_id':str(chat),'caption':caption},{'photo':(path.name,f,'image/png')})

def mode_keyboard(aid): return [[{'text':'🤖 Повністю автоматичний аналіз','callback_data':f'mode:{aid}:ai'}],[{'text':'🤖+👤 Аналіз з підкріпленням','callback_data':f'mode:{aid}:hybrid'}],[{'text':'👤 Ручний режим','callback_data':f'mode:{aid}:manual'}]]
def score_keyboard(aid,idx): return [[{'text':str(s),'callback_data':f'score:{aid}:{idx}:{s}'} for s in range(6)]]
def validation_keyboard(aid): return [[{'text':str(s),'callback_data':f'valid:{aid}:{s}'} for s in range(1,6)]]

def repeat_keyboard(aid):
    return [[{'text':'🔄 Повторити оцінювання','callback_data':f'repeat:{aid}'}]]

def error_home_keyboard(aid):
    return [[{'text':'↩️ Повернутися на стартовий екран','callback_data':f'errorhome:{aid}'}]]

def normalize_official_url(text: str) -> str | None:
    """Нормалізує URL або домен офіційного сайту.

    Приймає https://example.gov.ua/, example.gov.ua, www.example.gov.ua
    та прибирає випадкові пробіли.
    """
    raw=(text or '').strip().replace(' ', '')
    if not raw:
        return None
    if not raw.startswith(('http://','https://')):
        raw='https://'+raw
    try:
        u=urlparse(raw)
    except Exception:
        return None
    host=(u.hostname or '').strip('.').lower()
    if not host or '.' not in host:
        return None
    # домен повинен мати принаймні дві непорожні частини; IDN теж допускається
    if any(not part for part in host.split('.')):
        return None
    return raw

async def start_assessment(chat,user):
    with db() as c:
        active=c.execute("SELECT id FROM assessments WHERE chat_id=? AND status NOT IN ('finished','cancelled') ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        if active: await send(chat,f"У вас уже є незавершене оцінювання №{active['id']}. /cancel — скасувати."); return
        cur=c.execute('INSERT INTO assessments(chat_id,user_id,username,status,created_at) VALUES(?,?,?,?,?)',(chat,user.get('id'),user.get('username'),'await_mode',now_iso())); aid=cur.lastrowid
    await send(chat,'Оберіть режим оцінювання AI-зрілості органу публічної влади:',mode_keyboard(aid))

async def ask_question(chat,aid,idx):
    x=MATRIX[idx]; await send(chat,f"{idx+1}/{len(MATRIX)}  {x['code']} — {x['criterion']}\n{x['dimension_name']}\n\n{x['statement']}\n\nОцініть 0–5:",score_keyboard(aid,idx))

def missing_indices(aid):
    r=calc_results(aid); return [i for i,x in enumerate(MATRIX) if x['code'] not in r['scores']]

async def crawl_official_site(url,max_pages=12,max_chars=70000):
    if not re.match(r'^https?://',url,re.I): url='https://'+url
    host=urlparse(url).netloc.lower().removeprefix('www.'); seen=set(); queue=[url]; docs=[]
    headers={'User-Agent':'Mozilla/5.0 AI-Maturity-Research-Bot/0.3'}
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers=headers) as c:
        while queue and len(seen)<max_pages and sum(len(x) for x in docs)<max_chars:
            u=queue.pop(0)
            if u in seen: continue
            seen.add(u)
            try:
                r=await c.get(u); ct=r.headers.get('content-type','')
                if r.status_code!=200 or 'text/html' not in ct: continue
                s=BeautifulSoup(r.text,'html.parser')
                for z in s(['script','style','noscript','svg']): z.decompose()
                text=' '.join(s.stripped_strings); docs.append(f'URL: {u}\n{text[:12000]}')
                for a in s.find_all('a',href=True):
                    v=urljoin(str(r.url),a['href']); p=urlparse(v)
                    if p.scheme in ('http','https') and p.netloc.lower().removeprefix('www.')==host and v not in seen:
                        label=(a.get_text(' ',strip=True)+' '+v).lower()
                        if any(k in label for k in ['цифр','digital','штуч','artificial','ai','дан','data','стратег','нормат','безпек','структур','положен','відкрит','реєстр','api']): queue.append(v.split('#')[0])
            except Exception: pass
    return '\n\n'.join(docs)[:max_chars],len(seen)

def _response_output_text(data):
    """Extract text from Responses API JSON without depending on the OpenAI SDK."""
    if isinstance(data.get('output_text'), str) and data.get('output_text').strip():
        return data['output_text'].strip()
    chunks=[]
    for item in data.get('output',[]) or []:
        if not isinstance(item,dict) or item.get('type')!='message':
            continue
        for c in item.get('content',[]) or []:
            if isinstance(c,dict) and c.get('type') in ('output_text','text') and c.get('text'):
                chunks.append(str(c['text']))
    return '\n'.join(chunks).strip()

def _json_from_model_text(s):
    """Accept a plain JSON object or JSON wrapped in a markdown fence."""
    s=(s or '').strip()
    if s.startswith('```'):
        s=re.sub(r'^```(?:json)?\s*','',s,flags=re.I)
        s=re.sub(r'\s*```$','',s)
    try:
        return json.loads(s)
    except Exception:
        m=re.search(r'\{.*\}',s,re.S)
        if not m:
            raise RuntimeError('ШІ не повернув структурований JSON-результат.')
        return json.loads(m.group(0))

async def ai_assess(aid,organization,url):
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        raise RuntimeError('Для ШІ-режиму потрібно задати OPENAI_API_KEY та OPENAI_MODEL у Render.')

    # Official-site crawling is useful evidence, but it is no longer a hard dependency.
    # If the site blocks Render or returns no parseable text, the analysis continues
    # with OpenAI Web Search over public sources.
    try:
        corpus,pages=await crawl_official_site(url)
    except Exception as e:
        print('Official-site crawl warning:',repr(e),flush=True)
        corpus,pages='',0

    criteria=[{
        'code':x['code'],
        'dimension':x.get('dimension'),
        'dimension_name':x.get('dimension_name'),
        'criterion':x['criterion'],
        'statement':x['statement']
    } for x in MATRIX]

    official_excerpt=(corpus[:45000] if corpus.strip() else
        '[Офіційний сайт не вдалося автоматично прочитати з Render. '
        'Не трактуй це як відсутність відповідних практик. Використай вебпошук, '
        'зокрема пошук сторінок офіційного домену та інших відкритих джерел.]')

    prompt=f"""
Ти — дослідницький модуль AI Maturity Bot для доказового оцінювання AI-зрілості
органу публічної влади.

ОРГАН: {organization}
ОФІЦІЙНИЙ САЙТ: {url}

МЕТОД:
1. Проведи вебпошук за повною назвою органу, скороченнями та тематикою кожного критерію.
2. Пріоритет доказів:
   A — офіційний сайт органу, його документи, рішення, звіти, відкриті дані;
   B — інші офіційні державні/муніципальні джерела, реєстри, Prozorro тощо;
   C — міжнародні організації, наукові та професійні аналітичні джерела;
   D — надійні ЗМІ та матеріали партнерів/постачальників.
3. Не роби висновок лише з відсутності інформації. Відсутність публічного доказу ≠ нульова зрілість.
4. Для кожного з 48 критеріїв постав score 0–5 ЛИШЕ коли є достатня доказова база.
   Якщо доказів недостатньо, неоднозначні або вони не стосуються саме цього органу — score=null.
5. Не домислюй внутрішні процеси, кадрові компетентності, політики, системи чи практики.
6. Якщо є суперечливі джерела — відобрази це у rationale і знизь confidence.
7. Для кожної визначеної оцінки наведи 1–3 конкретні URL-джерела.
8. Evidence має бути стислим фактичним описом того, що саме підтверджує оцінку.
9. Поверни ЛИШЕ JSON без markdown і без пояснень поза JSON.

ФОРМАТ:
{{
  "items": [
    {{
      "code": "D1.1",
      "score": 0,
      "evidence": "Стислий фактичний доказ",
      "rationale": "Чому цей доказ відповідає саме такому балу",
      "confidence": 0.0,
      "sources": [
        {{"title": "Назва джерела", "url": "https://..."}}
      ]
    }}
  ],
  "research_note": "Коротко: які типи джерел використано та які були обмеження"
}}

КРИТЕРІЇ:
{json.dumps(criteria,ensure_ascii=False)}

ДОДАТКОВИЙ КОРПУС, ЯКЩО ВДАЛОСЯ ПРОЧИТАТИ ОФІЦІЙНИЙ САЙТ:
{official_excerpt}
""".strip()

    headers={
        'Authorization':f'Bearer {OPENAI_API_KEY}',
        'Content-Type':'application/json'
    }
    payload={
        'model':OPENAI_MODEL,
        'tools':[{
            'type':'web_search',
            'external_web_access':True,
            'search_context_size':'high'
        }],
        'input':prompt,
        'max_output_tokens':14000
    }

    async with httpx.AsyncClient(timeout=300,follow_redirects=True) as c:
        resp=await c.post('https://api.openai.com/v1/responses',headers=headers,json=payload)
        if resp.status_code>=400:
            body=resp.text[:2500]
            rid=resp.headers.get('x-request-id','')
            print(f'OpenAI API error: status={resp.status_code} request_id={rid} body={body}',flush=True)
            raise RuntimeError(f'OpenAI API HTTP {resp.status_code}: {body}')
        data=resp.json()

    model_text=_response_output_text(data)
    obj=_json_from_model_text(model_text)
    allowed={x['code'] for x in MATRIX}
    saved=0

    with db() as c:
        for x in obj.get('items',[]) or []:
            code=x.get('code')
            score=x.get('score')
            if code not in allowed or not isinstance(score,int) or score not in range(6):
                continue

            sources=x.get('sources') or []
            src_lines=[]
            for s in sources[:3]:
                if isinstance(s,dict):
                    title=str(s.get('title') or '').strip()
                    surl=str(s.get('url') or '').strip()
                    if surl:
                        src_lines.append((title+' — ' if title else '')+surl)
                elif isinstance(s,str) and s.strip():
                    src_lines.append(s.strip())

            evidence=str(x.get('evidence') or '').strip()
            if src_lines:
                evidence=(evidence+'\nДжерела:\n'+'\n'.join(src_lines)).strip()

            conf=x.get('confidence')
            try:
                conf=float(conf) if conf is not None else None
                if conf is not None:
                    conf=max(0.0,min(1.0,conf))
            except Exception:
                conf=None

            c.execute(
                'INSERT OR REPLACE INTO answers'
                '(assessment_id,code,score,source,evidence,rationale,confidence,created_at) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (
                    aid,code,score,'ai',
                    evidence[:4000],
                    str(x.get('rationale') or '')[:1800],
                    conf,now_iso()
                )
            )
            saved+=1

        note=(
            f"Метод: ШІ-аналіз офіційного вебсайту та відкритих джерел. "
            f"Автоматично прочитано сторінок офіційного сайту: {pages}. "
            f"{str(obj.get('research_note') or '').strip()}"
        ).strip()
        c.execute(
            'UPDATE assessments SET coverage=?,ai_note=? WHERE id=?',
            (saved/len(MATRIX)*100,note[:4000],aid)
        )
    return saved,pages

async def run_ai_stage(chat,aid):
    with db() as c:
        a=c.execute('SELECT * FROM assessments WHERE id=?',(aid,)).fetchone()

    await send(
        chat,
        'Виконую ШІ-аналіз офіційного вебсайту та відкритих джерел. '
        'Якщо офіційний сайт технічно недоступний для автоматичного читання, '
        'оцінювання продовжиться за відкритими джерелами. Це може тривати кілька хвилин…'
    )
    try:
        saved,pages=await ai_assess(aid,a['organization'],a['official_url'])
    except Exception as e:
        # A failed AI run is closed so the user can immediately start again.
        with db() as c:
            c.execute("UPDATE assessments SET status='cancelled' WHERE id=?",(aid,))
        print(f'AI assessment error aid={aid}: {type(e).__name__}: {e}',flush=True)
        await send(
            chat,
            '⚠️ Під час автоматичного оцінювання сталася технічна помилка.\n\n'
            'Оцінювання не завершено. Спробуйте повторити пізніше або оберіть інший режим.\n\n'
            'Натисніть кнопку нижче, щоб повернутися на стартовий екран.',
            error_home_keyboard(aid)
        )
        return

    miss=missing_indices(aid)
    mode=a['mode']
    await send(
        chat,
        f'Автоматичний етап завершено. Визначено {saved} із 48 показників. '
        f'Не визначено: {len(miss)}.'
    )

    if mode=='hybrid' and miss:
        with db() as c:
            c.execute(
                "UPDATE assessments SET status='running',current_index=? WHERE id=?",
                (miss[0],aid)
            )
        await send(
            chat,
            'Переходимо до підкріплення: необхідно уточнити лише показники, '
            'які ШІ не зміг надійно визначити за сайтом та відкритими джерелами.'
        )
        await ask_question(chat,aid,miss[0])
    else:
        await finish_assessment(chat,aid)

def make_radar(aid,org):
    r=calc_results(aid); labels=[d[0] for d in DIMENSIONS]; values=[r['dims'][d] or 0 for d in labels]; vals=values+values[:1]; ang=np.linspace(0,2*np.pi,len(labels),endpoint=False).tolist()+[0]
    fig=plt.figure(figsize=(8,8)); ax=plt.subplot(111,polar=True); ax.plot(ang,vals,linewidth=2); ax.fill(ang,vals,alpha=.15); ax.set_thetagrids(np.degrees(ang[:-1]),labels); ax.set_ylim(0,100); ax.set_yticks([20,40,60,80,100]); ax.set_title(f'Профіль AI-зрілості\n{org}\nAIMI = {r["aimi"]:.1f}% | покриття {r["coverage"]:.1f}%',pad=25)
    out=BASE_DIR/'exports'/f'radar_{aid}.png'; out.parent.mkdir(parents=True,exist_ok=True); fig.savefig(out,dpi=180,bbox_inches='tight'); plt.close(fig); return out

async def finish_assessment(chat,aid):
    with db() as c: a=c.execute('SELECT * FROM assessments WHERE id=?',(aid,)).fetchone(); c.execute("UPDATE assessments SET status='await_validation',finished_at=?,coverage=? WHERE id=?",(now_iso(),calc_results(aid)['coverage'],aid))
    r=calc_results(aid); chart=make_radar(aid,a['organization'] or 'Орган'); await send_photo(chat,chart,'Профіль AI-зрілості')
    dims='\n'.join(f"{d}: {r['dims'][d]:.1f}%" if r['dims'][d] is not None else f'{d}: не визначено' for d,_ in DIMENSIONS)
    await send(chat,f"Оцінювання №{aid} завершено.\nОрган: {a['organization']}\nРежим: {MODE_LABEL.get(a['mode'],a['mode'])}\nAIMI: {r['aimi']:.1f}%\nПовнота: {r['coverage']:.1f}% ({len(r['scores'])}/48)\nРівень: {maturity_level(r['aimi'])}\n\n{dims}")
    await send(chat,'Наскільки отриманий профіль відповідає фактичному стану органу?\n1 — зовсім не відповідає; 5 — повністю відповідає.',validation_keyboard(aid))

def base_recommendations(aid):
    r=calc_results(aid); ranked=sorted([(d,v) for d,v in r['dims'].items() if v is not None],key=lambda x:x[1]); return 'ПРІОРИТЕТНІ НАПРЯМИ\n'+'\n'.join(f'{i}. {d}: {v:.1f}% — потребує пріоритетного опрацювання.' for i,(d,v) in enumerate(ranked[:4],1))

def _payload(aid):
    with db() as c: a=dict(c.execute('SELECT * FROM assessments WHERE id=?',(aid,)).fetchone()); rows=[dict(x) for x in c.execute('SELECT * FROM answers WHERE assessment_id=?',(aid,)).fetchall()]
    by={x['code']:x for x in rows}; ans=[]
    for m in MATRIX:
        x=by.get(m['code'],{}); ans.append({**m,**x,'score':x.get('score'),'source':x.get('source','ND')})
    return a,ans,calc_results(aid)

def _fonts():
    reg='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'; bold='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    if Path(reg).exists():
        if 'DejaVu' not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont('DejaVu',reg)); pdfmetrics.registerFont(TTFont('DejaVuBold',bold))
        return 'DejaVu','DejaVuBold'
    return 'Helvetica','Helvetica-Bold'

def export_pdf(aid):
    a,ans,r=_payload(aid); out=BASE_DIR/'exports'/f'AI_Maturity_Assessment_{aid}.pdf'; out.parent.mkdir(parents=True,exist_ok=True); font,bold=_fonts(); st=getSampleStyleSheet(); st.add(ParagraphStyle(name='U',parent=st['BodyText'],fontName=font,fontSize=8.5,leading=11)); st.add(ParagraphStyle(name='H',parent=st['Heading2'],fontName=bold,fontSize=13)); st.add(ParagraphStyle(name='T',parent=st['Title'],fontName=bold,fontSize=17,alignment=TA_CENTER))
    footer_text='AI Maturity Bot — дослідницький інформаційно-аналітичний інструмент для комплексного оцінювання AI-зрілості органів публічної влади. © Антон Осьмак · @AI_Maturity_Bot'
    def draw_footer(canvas,doc):
        canvas.saveState()
        canvas.setFont(font,6.4)
        width=A4[0]-24*mm
        # Footer in two compact lines so it remains readable and does not collide with content.
        line1='AI Maturity Bot — дослідницький інформаційно-аналітичний інструмент для комплексного оцінювання'
        line2='AI-зрілості органів публічної влади. © 2026, Антон Осьмак · @AI_Maturity_Bot · ai.maturity.bot@gmail.com'
        canvas.drawCentredString(A4[0]/2,8.0*mm,line1)
        canvas.drawCentredString(A4[0]/2,5.0*mm,line2)
        canvas.restoreState()
    doc=SimpleDocTemplate(str(out),pagesize=A4,rightMargin=12*mm,leftMargin=12*mm,topMargin=12*mm,bottomMargin=18*mm); story=[Paragraph('AI Maturity Assessment',st['T']),Paragraph('Звіт за результатами оцінювання AI-зрілості органу публічної влади',st['H'])]
    meta=[['Орган',a.get('organization') or ''],['Офіційний сайт',a.get('official_url') or '—'],['Режим оцінювання',MODE_LABEL.get(a.get('mode'),a.get('mode') or '—')],['AIMI',f"{r['aimi']:.1f}%"],['Повнота оцінювання',f"{r['coverage']:.1f}% ({len(r['scores'])}/48)"],['Рівень',maturity_level(r['aimi'])]]
    t=Table([[Paragraph(str(v),st['U']) for v in row] for row in meta],colWidths=[45*mm,135*mm]); t.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.4,colors.grey),('FONTNAME',(0,0),(-1,-1),font),('FONTNAME',(0,0),(0,-1),bold),('VALIGN',(0,0),(-1,-1),'TOP')])); story += [t,Spacer(1,5*mm)]
    if a.get('mode')=='ai': story.append(Paragraph('Примітка: це ШІ-аналіз на основі офіційного вебсайту органу та відкритих джерел із використанням вебпошуку. Показники, для яких не знайдено достатньої доказової інформації, позначено «не визначено».',st['U']))
    elif a.get('mode')=='hybrid': story.append(Paragraph('Примітка: це ШІ-аналіз з підкріпленням. Первинне оцінювання виконано ШІ на основі офіційного вебсайту органу та відкритих джерел із використанням вебпошуку; показники, для яких не знайдено достатньої доказової інформації, додатково уточнено респондентом.',st['U']))
    elif a.get('mode')=='manual': story.append(Paragraph('Примітка: оцінювання виконано в ручному режимі на основі відповідей респондента.',st['U']))
    story += [Image(str(make_radar(aid,a.get('organization') or 'Орган')),width=135*mm,height=135*mm),PageBreak(),Paragraph('Деталізація 48 показників',st['H'])]
    data=[['Код','Критерій','Бал','Джерело']]+[[x['code'],x['criterion'],str(x['score']) if x['score'] is not None else 'Не визначено',{'ai':'ШІ','respondent':'Респондент','ND':'Не визначено'}.get(x['source'],x['source'])] for x in ans]
    t=Table([[Paragraph(str(v),st['U']) for v in row] for row in data],colWidths=[16*mm,112*mm,25*mm,27*mm],repeatRows=1); t.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.3,colors.grey),('FONTNAME',(0,0),(-1,0),bold),('VALIGN',(0,0),(-1,-1),'TOP')])); story += [t,PageBreak(),Paragraph('Рекомендації',st['H']),Paragraph((a.get('recommendations') or base_recommendations(aid)).replace('\n','<br/>'),st['U'])]; doc.build(story,onFirstPage=draw_footer,onLaterPages=draw_footer); return out

def export_json(aid):
    a,ans,r=_payload(aid); out=BASE_DIR/'exports'/f'assessment_{aid}.json'; out.write_text(json.dumps({'assessment':a,'results':{k:v for k,v in r.items() if k!='scores'},'answers':ans},ensure_ascii=False,indent=2),encoding='utf-8'); return out

def export_xlsx(aid):
    a,ans,r=_payload(aid); out=BASE_DIR/'exports'/f'AI_Maturity_Assessment_{aid}.xlsx'; wb=xlsxwriter.Workbook(out); ws=wb.add_worksheet('Звіт'); de=wb.add_worksheet('48 показників'); h=wb.add_format({'bold':True,'border':1,'text_wrap':True}); tx=wb.add_format({'border':1,'text_wrap':True,'valign':'top'}); ws.write_row(0,0,['Орган','Режим','URL','AIMI %','Повнота %','Рівень'],h); ws.write_row(1,0,[a.get('organization'),MODE_LABEL.get(a.get('mode')),a.get('official_url'),r['aimi'],r['coverage'],maturity_level(r['aimi'])],tx); de.write_row(0,0,['Код','Вимір','Критерій','Твердження','Оцінка','Джерело','Evidence','Обґрунтування','Confidence'],h)
    for i,x in enumerate(ans,1): de.write_row(i,0,[x.get('code'),x.get('dimension_name'),x.get('criterion'),x.get('statement'),x.get('score'),x.get('source'),x.get('evidence'),x.get('rationale'),x.get('confidence')],tx)
    de.set_column(0,0,10); de.set_column(1,2,28); de.set_column(3,3,65); de.set_column(4,8,20); wb.close(); return out

def export_bundle(aid):
    files=[export_pdf(aid),export_xlsx(aid),export_json(aid)]; a,_,_=_payload(aid); files.append(make_radar(aid,a.get('organization') or 'Орган')); out=BASE_DIR/'exports'/f'AI_Maturity_Assessment_{aid}_bundle.zip';
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for p in files: z.write(p,arcname=p.name)
    return out

async def upload_to_google_drive(paths):
    if not GDRIVE_UPLOAD_URL or not GDRIVE_UPLOAD_SECRET: raise RuntimeError('Google Drive не налаштовано')
    uploaded=[]
    async with httpx.AsyncClient(timeout=120,follow_redirects=True) as c:
        for p in paths:
            payload={'secret':GDRIVE_UPLOAD_SECRET,'filename':p.name,'mime_type':mimetypes.guess_type(p.name)[0] or 'application/octet-stream','file_base64':base64.b64encode(p.read_bytes()).decode('ascii')}; r=await c.post(GDRIVE_UPLOAD_URL,json=payload); r.raise_for_status(); j=r.json();
            if not j.get('ok'): raise RuntimeError(j.get('error','Drive error'))
            uploaded.append(j)
    return uploaded

async def finalize_and_archive(aid,chat):
    pdf=export_pdf(aid); await send_document(chat,pdf,'Фінальний PDF-звіт AI-зрілості')
    if GDRIVE_UPLOAD_URL and GDRIVE_UPLOAD_SECRET:
        try: await upload_to_google_drive([pdf]); await send(chat,'Фінальний PDF-звіт сформовано. Копію результату збережено в журналі.',repeat_keyboard(aid))
        except Exception as e: print('Drive auto-upload error:',repr(e),flush=True); await send(chat,'Фінальний PDF-звіт сформовано, але копію не вдалося зберегти в журналі.',repeat_keyboard(aid))
    else: await send(chat,'Фінальний PDF-звіт сформовано.',repeat_keyboard(aid))

async def handle_message(msg):
    chat=msg['chat']['id']; user=msg.get('from',{}); text=(msg.get('text') or '').strip()
    if not text:return
    if text=='/start':
        welcome = (
            'Вітаємо в AI Maturity Bot!\n\n'
            'AI Maturity Bot — дослідницький інформаційно-аналітичний інструмент '
            'для комплексного оцінювання AI-зрілості органів публічної влади.\n\n'
            'Бот дає змогу оцінити готовність органу до використання штучного інтелекту '
            'за системою показників D1–D8, сформувати профіль AI-зрілості та отримати '
            'підсумковий аналітичний звіт. В автоматичних режимах аналіз здійснюється '
            'на основі офіційного вебсайту органу та відкритих джерел.\n\n'
            'Доступні три режими оцінювання:\n'
            '🤖 повністю автоматичний ШІ-аналіз;\n'
            '🤖+👤 ШІ-аналіз з підкріпленням;\n'
            '👤 ручне оцінювання.\n\n'
            'Для початку натисніть кнопку «Розпочати оцінювання».\n\n'
            '© 2026, Антон Осьмак'
        )
        await send_start_welcome(chat,welcome); return
    if text=='/help':
        await send(chat,'AI Maturity Bot — v0.3.9\n\n/new — нове оцінювання\n/status — стан\n/log — журнал\n/report [ID] — короткий звіт\n/pdf [ID] — PDF\n/xlsx [ID] — Excel\n/bundle [ID] — пакет\n/drive [ID] — архівувати пакет\n/export [ID] — JSON audit log\n/cancel — скасувати'); return
    if text in ('/new','▶️ Розпочати оцінювання'): await start_assessment(chat,user); return
    if text=='/cancel':
        with db() as c: a=c.execute("SELECT id FROM assessments WHERE chat_id=? AND status NOT IN ('finished','cancelled') ORDER BY id DESC LIMIT 1",(chat,)).fetchone();
        if a:
            with db() as c: c.execute("UPDATE assessments SET status='cancelled' WHERE id=?",(a['id'],))
        await send(chat,'Поточне оцінювання скасовано.' if a else 'Немає активного оцінювання.'); return
    if text=='/status':
        with db() as c:a=c.execute('SELECT * FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1',(chat,)).fetchone()
        if not a: await send(chat,'Оцінювань ще немає. /new'); return
        r=calc_results(a['id']); await send(chat,f"№{a['id']} | {a['organization'] or 'орган не задано'} | {MODE_LABEL.get(a['mode'],a['mode'] or 'режим не обрано')} | статус: {a['status']} | визначено: {len(r['scores'])}/48"); return
    if text.startswith('/report'):
        parts=text.split()
        with db() as c: row=c.execute('SELECT id FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1',(chat,)).fetchone()
        aid=int(parts[1]) if len(parts)>1 and parts[1].isdigit() else (row['id'] if row else None)
        if not aid: await send(chat,'Немає оцінювань.'); return
        with db() as c: a=c.execute('SELECT * FROM assessments WHERE id=? AND chat_id=?',(aid,chat)).fetchone()
        if not a: await send(chat,'Оцінювання не знайдено.'); return
        r=calc_results(aid); dims='\n'.join(f"{d}: {r['dims'][d]:.1f}%" if r['dims'][d] is not None else f'{d}: не визначено' for d,_ in DIMENSIONS)
        await send(chat,f"ЗВІТ №{aid}\nОрган: {a['organization'] or '—'}\nРежим: {MODE_LABEL.get(a['mode'],a['mode'] or '—')}\nAIMI: {r['aimi']:.1f}%\nПовнота: {r['coverage']:.1f}% ({len(r['scores'])}/48)\nРівень: {maturity_level(r['aimi'])}\n\n{dims}")
        return
    if text=='/log':
        with db() as c: rows=c.execute('SELECT * FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 10',(chat,)).fetchall()
        await send(chat,'Журнал порожній.' if not rows else 'Останні оцінювання:\n'+'\n'.join(f"№{a['id']} — {a['organization'] or 'без назви'} — {MODE_LABEL.get(a['mode'],a['mode'] or '—')} — {a['status']}" for a in rows)); return
    if text.startswith(('/pdf','/xlsx','/bundle','/drive','/export')):
        cmd=text.split()[0]; parts=text.split();
        with db() as c: row=c.execute('SELECT id FROM assessments WHERE chat_id=? ORDER BY id DESC LIMIT 1',(chat,)).fetchone()
        aid=int(parts[1]) if len(parts)>1 and parts[1].isdigit() else (row['id'] if row else None)
        if not aid: await send(chat,'Немає оцінювань.'); return
        try:
            if cmd=='/pdf': await send_document(chat,export_pdf(aid),'PDF-звіт')
            elif cmd=='/xlsx': await send_document(chat,export_xlsx(aid),'Excel-звіт')
            elif cmd=='/bundle': await send_document(chat,export_bundle(aid),'Повний пакет оцінювання')
            elif cmd=='/export': await send_document(chat,export_json(aid),'Audit log оцінювання')
            else:
                a,_,_=_payload(aid); paths=[export_pdf(aid),export_xlsx(aid),export_json(aid),make_radar(aid,a.get('organization') or 'Орган')]; await upload_to_google_drive(paths); await send(chat,'Пакет звіту збережено в журналі.')
        except Exception as e: await send(chat,f'Не вдалося виконати операцію: {type(e).__name__}: {e}')
        return
    with db() as c: a=c.execute("SELECT * FROM assessments WHERE chat_id=? AND status='await_org' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
    if a:
        with db() as c: c.execute("UPDATE assessments SET organization=?,status=? WHERE id=?",(text[:500],'running' if a['mode']=='manual' else 'await_url',a['id']))
        if a['mode']=='manual': await send(chat,f'Орган: {text}\nПочинаємо ручне оцінювання. Шкала 0–5.'); await ask_question(chat,a['id'],0)
        else: await send(chat,'Введіть адресу офіційного вебсайту органу (URL).')
        return
    # URL офіційного сайту. Додатковий fallback робить стан стійким до
    # рідкісних повторних webhook/update після redeploy: якщо є останнє AI/hybrid
    # оцінювання з назвою органу, але без URL, адресу все одно приймаємо.
    with db() as c:
        a=c.execute("SELECT * FROM assessments WHERE chat_id=? AND status='await_url' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        if not a:
            a=c.execute("""SELECT * FROM assessments
                         WHERE chat_id=? AND mode IN ('ai','hybrid')
                           AND organization IS NOT NULL
                           AND (official_url IS NULL OR official_url='')
                           AND status NOT IN ('finished','cancelled')
                         ORDER BY id DESC LIMIT 1""",(chat,)).fetchone()
    if a:
        url=normalize_official_url(text)
        if not url:
            await send(chat,'Введіть коректну адресу офіційного вебсайту, наприклад https://example.gov.ua або example.gov.ua')
            return
        with db() as c:
            c.execute("UPDATE assessments SET official_url=?,status='ai_processing' WHERE id=?",(url,a['id']))
        await send(chat,f'Офіційний сайт: {url}')
        await run_ai_stage(chat,a['id'])
        return
    with db() as c:a=c.execute("SELECT * FROM assessments WHERE chat_id=? AND status='await_validation_comment' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
    if a:
        with db() as c:c.execute("UPDATE assessments SET validation_comment=?,status='finished' WHERE id=?",(text[:2000],a['id']))
        await send(chat,'Коментар збережено. Формую фінальний PDF-звіт…'); await finalize_and_archive(a['id'],chat); return
    await send(chat,'Не розпізнав команду. /help')

async def handle_callback(cb):
    data=cb.get('data',''); chat=cb.get('message',{}).get('chat',{}).get('id');
    if not chat:return
    await tg('answerCallbackQuery',{'callback_query_id':cb['id']}); p=data.split(':')
    if p[0]=='errorhome' and len(p)==2:
        # Failed assessment is already cancelled; create a fresh start screen.
        await start_assessment(chat,cb.get('from',{}))
        return
    if p[0]=='repeat' and len(p)==2:
        # Нове незалежне оцінювання; попередній результат залишається в журналі.
        await start_assessment(chat,cb.get('from',{}))
        return
    if p[0]=='mode' and len(p)==3:
        aid=int(p[1]); mode=p[2]
        if mode not in MODE_LABEL:return
        with db() as c:a=c.execute('SELECT * FROM assessments WHERE id=? AND chat_id=?',(aid,chat)).fetchone(); c.execute("UPDATE assessments SET mode=?,status='await_org' WHERE id=?",(mode,aid)) if a and a['status']=='await_mode' else None
        if a and a['status']=='await_mode': await send(chat,f"Обрано: {MODE_LABEL[mode]}.\n\nНадішліть повну назву органу публічної влади.")
        return
    if p[0]=='score' and len(p)==4:
        aid,idx,score=map(int,p[1:]); x=MATRIX[idx]
        with db() as c:a=c.execute('SELECT * FROM assessments WHERE id=? AND chat_id=?',(aid,chat)).fetchone()
        if not a or a['status']!='running':return
        with db() as c:c.execute('INSERT OR REPLACE INTO answers(assessment_id,code,score,source,created_at) VALUES(?,?,?,?,?)',(aid,x['code'],score,'respondent',now_iso()))
        await send(chat,f"{x['code']}: {score}/5 — {SCALE[score]}")
        miss=missing_indices(aid)
        if miss: await ask_question(chat,aid,miss[0])
        else: await finish_assessment(chat,aid)
        return
    if p[0]=='valid' and len(p)==3:
        aid=int(p[1]); score=int(p[2]);
        with db() as c:c.execute("UPDATE assessments SET validation_score=?,status='await_validation_comment' WHERE id=? AND chat_id=?",(score,aid,chat))
        await send(chat,f'Відповідність: {score}/5.\nЗа бажанням надішліть короткий коментар. Якщо коментар не потрібен — надішліть «-».')

async def process_update(upd):
    if 'message' in upd: await handle_message(upd['message'])
    elif 'callback_query' in upd: await handle_callback(upd['callback_query'])

async def main():
    import asyncio
    init_db()
    await configure_telegram_ui()
    offset=0
    while True:
        try:
            ups=await tg('getUpdates',{'offset':offset,'timeout':POLL_TIMEOUT,'allowed_updates':['message','callback_query']})
            for u in ups:
                offset=max(offset,u['update_id']+1); await process_update(u)
        except Exception as e: print('Polling error:',repr(e)); await asyncio.sleep(3)
if __name__=='__main__':
    import asyncio; asyncio.run(main())
