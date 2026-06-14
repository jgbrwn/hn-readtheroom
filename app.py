import os, re, sqlite3, threading
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs
import requests
from fasthtml.common import *
from starlette.responses import RedirectResponse
from monsterui.all import *
from llm_hacker_news import process_hn_comments

APP_TITLE = "Hacker News — Read The Room"
DB_PATH = os.getenv("DB_PATH", "readtheroom.db")
PROMPT_VERSION = "2025-06-13-v1"
HN_FIREBASE = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_ALGOLIA = "https://hn.algolia.com/api/v1/items/{id}"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
locks, locks_guard = {}, threading.Lock()

def openrouter_key():
    key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if key: return key.strip().strip('\x0b')
    if os.path.exists('.env'):
        txt = open('.env').read().strip().strip('\x0b')
        m = re.search(r'OPENROUTER(?:_API)?_KEY\s*[:=]\s*(\S+)', txt)
        if m: return m.group(1).strip().strip('\x0b')
    return None

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def nice_date(s): return datetime.fromisoformat(s).astimezone().strftime("%b %-d, %Y at %-I:%M %p")

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    return con

def init_db():
    with db() as con:
        con.executescript('''
        create table if not exists hn_items(
          hn_id integer primary key, title text not null, url text, hn_url text not null,
          type text, by text, score integer, descendants integer, fetched_at text not null);
        create table if not exists summaries(
          hn_id integer primary key references hn_items(hn_id), markdown text, model text,
          generated_at text, prompt_version text, comment_count integer, status text, error text);
        create table if not exists models(
          model_id text primary key, context_length integer, is_free integer, provider text, rank integer, last_seen_at text);
        create table if not exists kv(key text primary key, value text not null);
        ''')

def get_kv(con, key):
    r = con.execute("select value from kv where key=?", (key,)).fetchone()
    return r[0] if r else None

def set_kv(con, key, value):
    con.execute("insert into kv(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, value))

def parse_hn_input(raw):
    raw = (raw or "").strip()
    if not raw or len(raw) > 250: raise ValueError("Enter a Hacker News item ID or item URL.")
    if re.fullmatch(r"\d{1,12}", raw): return int(raw)
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or u.netloc.lower() not in ("news.ycombinator.com", "www.news.ycombinator.com") or u.path != "/item":
        raise ValueError("Only Hacker News item URLs like https://news.ycombinator.com/item?id=123 are accepted.")
    ids = parse_qs(u.query).get("id", [])
    if len(ids) != 1 or not re.fullmatch(r"\d{1,12}", ids[0]): raise ValueError("The Hacker News URL must contain one numeric id parameter.")
    return int(ids[0])

def fetch_json(url, timeout=15):
    r = requests.get(url, timeout=timeout, headers={"User-Agent":"hn-readtheroom/1.0"})
    r.raise_for_status()
    return r.json()

def validate_hn_item(hn_id):
    data = fetch_json(HN_FIREBASE.format(id=hn_id))
    if not data or data.get("deleted") or data.get("dead"): raise ValueError("This does not appear to be a valid public Hacker News item.")
    title = data.get("title") or (data.get("text") or "Hacker News item")[:80]
    meta = dict(hn_id=hn_id, title=title, url=data.get("url"), hn_url=f"https://news.ycombinator.com/item?id={hn_id}",
                type=data.get("type"), by=data.get("by"), score=data.get("score"), descendants=data.get("descendants", 0), fetched_at=now_iso())
    with db() as con:
        con.execute('''insert into hn_items(hn_id,title,url,hn_url,type,by,score,descendants,fetched_at)
        values(:hn_id,:title,:url,:hn_url,:type,:by,:score,:descendants,:fetched_at)
        on conflict(hn_id) do update set title=excluded.title,url=excluded.url,type=excluded.type,by=excluded.by,score=excluded.score,descendants=excluded.descendants,fetched_at=excluded.fetched_at''', meta)
    return meta

def cached_summary(hn_id):
    with db() as con:
        return con.execute("select s.*, h.title, h.hn_url from summaries s join hn_items h using(hn_id) where hn_id=? and status='done' and markdown is not null", (hn_id,)).fetchone()

def model_is_suitable(m):
    arch = m.get("architecture") or {}
    top = m.get("top_provider") or {}
    if arch.get("modality") != "text->text": return False
    if arch.get("output_modalities") != ["text"]: return False
    ctx = min(int(m.get("context_length") or 0), int(top.get("context_length") or m.get("context_length") or 0))
    if ctx < 1_000_000: return False
    p = m.get("pricing") or {}
    vals = [p.get(k) for k in ("prompt", "completion") if p.get(k) is not None]
    return bool(vals) and all(float(v) == 0 for v in vals)

def refresh_models(force=False):
    with db() as con:
        last = get_kv(con, "models_refreshed_at")
    if not force and last:
        try:
            if datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(days=1): return
        except Exception: pass
    try:
        data = fetch_json(OPENROUTER_MODELS, timeout=30).get("data", [])
        rows = []
        for m in data:
            top = m.get("top_provider") or {}
            ctx = min(int(m.get("context_length") or 0), int(top.get("context_length") or m.get("context_length") or 0)); mid = m.get("id")
            if mid and model_is_suitable(m): rows.append((mid, ctx, 1, m.get("owned_by") or "", 0, now_iso()))
        rows.sort(key=lambda r: (-r[1], r[0]))
        with db() as con:
            con.execute("delete from models")
            for rank, r in enumerate(rows, 1):
                con.execute("insert into models(model_id,context_length,is_free,provider,rank,last_seen_at) values(?,?,?,?,?,?)", (r[0],r[1],r[2],r[3],rank,r[5]))
            set_kv(con, "models_refreshed_at", now_iso())
    except Exception as e: print("model refresh failed", e)

def eligible_models():
    refresh_models(False)
    with db() as con: rows = con.execute("select model_id from models where is_free=1 and context_length>=1000000 order by rank").fetchall()
    return [r[0] for r in rows] or ["google/gemini-2.5-pro-exp-03-25:free", "google/gemini-2.0-flash-exp:free"]

def hn_thread(hn_id):
    data = fetch_json(HN_ALGOLIA.format(id=hn_id), timeout=30)
    text = process_hn_comments(data)
    count = len(re.findall(r"^\[[\d.]+\] ", text, re.M)) - 1
    return text, max(0, count)

def prompt_for(meta, comments):
    return f'''You are writing for a polished web app named "Hacker News — Read The Room".

Read the room of this Hacker News discussion. Summarize the sentiment and social texture of the comments, not just the article. Be specific, measured, and useful. Do not invent quotes. If there are few comments, say so.

HN item: {meta['title']}
HN URL: {meta['hn_url']}
Original URL: {meta.get('url') or 'none'}

Return clean Markdown only with these sections:

## The room in one sentence
## Overall mood
## What people agree on
## Points of tension
## Notable technical/practical objections
## Undercurrents, jokes, and skepticism
## Bottom line

Keep it concise but substantive. Avoid generic AI phrasing.

Discussion transcript follows in thread-path notation:

{comments}
'''

def call_openrouter(model, prompt):
    key = openrouter_key()
    if not key: raise RuntimeError("OPENROUTER_API_KEY is missing.")
    r = requests.post(OPENROUTER_CHAT, timeout=180, headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json", "HTTP-Referer":"https://hn-readtheroom.exe.xyz/", "X-Title": "HN Read The Room"}, json={"model": model, "messages":[{"role":"user","content":prompt}], "temperature":0.25})
    if r.status_code >= 400: raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]["content"].strip()

def generate_summary(hn_id):
    cached = cached_summary(hn_id)
    if cached: return cached
    with locks_guard: lock = locks.setdefault(hn_id, threading.Lock())
    with lock:
        cached = cached_summary(hn_id)
        if cached: return cached
        meta = validate_hn_item(hn_id)
        comments, count = hn_thread(hn_id)
        if count == 0 and (meta.get("descendants") or 0) == 0: raise ValueError("This HN item is valid, but it does not appear to have comments yet.")
        errors = []
        for model in eligible_models():
            try:
                md = call_openrouter(model, prompt_for(meta, comments)); gen = now_iso()
                with db() as con:
                    con.execute('''insert into summaries(hn_id,markdown,model,generated_at,prompt_version,comment_count,status,error)
                    values(?,?,?,?,?,?,'done',null)
                    on conflict(hn_id) do update set markdown=excluded.markdown,model=excluded.model,generated_at=excluded.generated_at,prompt_version=excluded.prompt_version,comment_count=excluded.comment_count,status='done',error=null''', (hn_id, md, model, gen, PROMPT_VERSION, count))
                return cached_summary(hn_id)
            except Exception as e: errors.append(f"{model}: {e}")
        with db() as con: con.execute("insert into summaries(hn_id,status,error) values(?,'error',?) on conflict(hn_id) do update set status='error',error=excluded.error", (hn_id, "\n".join(errors)))
        raise RuntimeError("All eligible OpenRouter models failed. " + (errors[-1] if errors else ""))

def page(*content): return Title(APP_TITLE), Container(Div(*content, cls="max-w-4xl mx-auto py-10 px-4"))
def error_card(msg): return Card(P(str(msg), cls="text-red-900"), header=H3("Couldn’t read the room", cls="text-red-700"), cls="border border-red-200 bg-red-50")

app, rt = fast_app(hdrs=Theme.slate.headers(), title=APP_TITLE, live=False)

@rt('/')
def get():
    return page(
        Div(P("Hacker News", cls="tracking-[0.3em] uppercase text-sm text-slate-500"), H1("Read The Room", cls="text-5xl font-semibold tracking-tight mt-3"), P("Paste a Hacker News item ID or URL. We’ll distill the comments into a clear, cached sentiment brief.", cls="text-xl text-slate-600 mt-4"), cls="text-center mb-10"),
        Card(Form(Label("HN item ID or URL", For="q", cls="font-medium"), Input(name="q", id="q", placeholder="43875136 or https://news.ycombinator.com/item?id=43875136", required=True, cls="uk-input text-lg"), Button("Read the room", cls=(ButtonT.primary, "mt-3 w-full")), Div(id="form-status", cls="text-sm text-slate-500"), method="post", action="/analyze", hx_post="/analyze", hx_target="#form-status", hx_swap="innerHTML", hx_indicator="#form-status"), header=H2("Analyze a discussion", cls="text-2xl font-medium"), cls="shadow-sm"),
        P("Summaries are generated once and cached. Inputs are strictly validated against Hacker News before analysis.", cls="text-center text-sm text-slate-500 mt-6"))

@rt('/analyze')
def post(q: str):
    try: hn_id = parse_hn_input(q)
    except Exception as e: return error_card(e)
    return Div("Validated. Opening discussion…", Script(f"window.location='/item?id={hn_id}'"), cls="text-slate-600")

@rt('/item')
def get(id: str = ""):
    try:
        hn_id = parse_hn_input(id)
        row = cached_summary(hn_id) or generate_summary(hn_id)
        return page(A("← Analyze another", href="/", cls="uk-btn uk-btn-default mb-6"), Article(ArticleTitle(A(row['title'], href=row['hn_url'], target="_blank", rel="noopener", cls="hover:underline")), ArticleMeta(f"generated by {row['model']} on {nice_date(row['generated_at'])}"), Div(render_md(row['markdown']), cls="mt-8 prose prose-slate max-w-none")))
    except Exception as e:
        return page(error_card(e), A("Back home", href="/", cls="uk-btn uk-btn-default mt-5"))

init_db(); refresh_models(False)
if __name__ == '__main__': serve(host='0.0.0.0', port=int(os.getenv('PORT', '8000')))
