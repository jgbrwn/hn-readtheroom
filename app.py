import os, re, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs
import requests
from fasthtml.common import *
from starlette.responses import RedirectResponse, PlainTextResponse
from monsterui.all import *
from llm_hacker_news import process_hn_comments

APP_TITLE = "Hacker News — Read The Room"
DB_PATH = os.getenv("DB_PATH", "readtheroom.db")
PROMPT_VERSION = "v2"
HN_FIREBASE = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_ALGOLIA = "https://hn.algolia.com/api/v1/items/{id}"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
GEN_POOL = ThreadPoolExecutor(max_workers=int(os.getenv("GEN_WORKERS", "2")))
locks, locks_guard, submitted = {}, threading.Lock(), set()

# ---------- config / db ----------
def openrouter_key():
    key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if key: return key.strip().strip('\x0b')
    if os.path.exists('.env'):
        txt = open('.env').read().strip().strip('\x0b')
        m = re.search(r'OPENROUTER(?:_API)?_KEY\s*[:=]\s*(\S+)', txt)
        if m: return m.group(1).strip().strip('\x0b')
    return None

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def nice_date(s):
    dt = datetime.fromisoformat(s).astimezone()
    return dt.strftime("%b %-d, %Y at %-I:%M %p %Z (%z)")

def display_prompt_version(v):
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}-(v\d+)", v or "")
    return m.group(1) if m else (v or "unknown")

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
        create table if not exists model_failures(
          model_id text primary key, failures integer not null default 0, last_error text, last_failed_at text);
        create table if not exists rate_limits(
          key text not null, bucket text not null, count integer not null default 0, primary key(key,bucket));
        create table if not exists kv(key text primary key, value text not null);
        ''')

def get_kv(con, key):
    r = con.execute("select value from kv where key=?", (key,)).fetchone()
    return r[0] if r else None

def set_kv(con, key, value):
    con.execute("insert into kv(key,value) values(?,?) on conflict(key) do update set value=excluded.value", (key, value))

# ---------- validation / rate limiting ----------
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

def client_key(req=None):
    if not req: return "local"
    fwd = req.headers.get("x-forwarded-for") if hasattr(req, "headers") else None
    return (fwd.split(",")[0].strip() if fwd else getattr(getattr(req, "client", None), "host", "local")) or "local"

def rate_limit(key, limit=12):
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    with db() as con:
        con.execute("delete from rate_limits where bucket < ?", ((datetime.now(timezone.utc)-timedelta(hours=2)).strftime("%Y%m%d%H"),))
        con.execute("insert into rate_limits(key,bucket,count) values(?,?,1) on conflict(key,bucket) do update set count=count+1", (key, bucket))
        count = con.execute("select count from rate_limits where key=? and bucket=?", (key, bucket)).fetchone()[0]
    if count > limit: raise ValueError("Rate limit reached. Please try again later.")

def fetch_json(url, timeout=15):
    r = requests.get(url, timeout=timeout, headers={"User-Agent":"hn-readtheroom/1.0"})
    r.raise_for_status()
    return r.json()

def validate_hn_item(hn_id):
    data = fetch_json(HN_FIREBASE.format(id=hn_id))
    if not data or data.get("deleted") or data.get("dead"): raise ValueError("This does not appear to be a valid public Hacker News item.")
    title = data.get("title") or re.sub("<[^>]+>", "", data.get("text") or "Hacker News item")[:80]
    meta = dict(hn_id=hn_id, title=title, url=data.get("url"), hn_url=f"https://news.ycombinator.com/item?id={hn_id}",
                type=data.get("type"), by=data.get("by"), score=data.get("score"), descendants=data.get("descendants", 0), fetched_at=now_iso())
    with db() as con:
        con.execute('''insert into hn_items(hn_id,title,url,hn_url,type,by,score,descendants,fetched_at)
        values(:hn_id,:title,:url,:hn_url,:type,:by,:score,:descendants,:fetched_at)
        on conflict(hn_id) do update set title=excluded.title,url=excluded.url,type=excluded.type,by=excluded.by,score=excluded.score,descendants=excluded.descendants,fetched_at=excluded.fetched_at''', meta)
    return meta

def item_row(hn_id):
    with db() as con:
        return con.execute("select * from hn_items where hn_id=?", (hn_id,)).fetchone()

def summary_row(hn_id):
    with db() as con:
        return con.execute("select s.*, h.title, h.url, h.hn_url, h.score, h.descendants, h.by from summaries s join hn_items h using(hn_id) where hn_id=?", (hn_id,)).fetchone()

def cached_summary(hn_id):
    r = summary_row(hn_id)
    return r if r and r["status"] == "done" and r["markdown"] else None

# ---------- OpenRouter model discovery ----------
def model_is_suitable(m):
    arch = m.get("architecture") or {}; top = m.get("top_provider") or {}
    if arch.get("modality") != "text->text" or arch.get("output_modalities") != ["text"]: return False
    ctx = min(int(m.get("context_length") or 0), int(top.get("context_length") or m.get("context_length") or 0))
    if ctx < 1_000_000: return False
    p = m.get("pricing") or {}; vals = [p.get(k) for k in ("prompt", "completion") if p.get(k) is not None]
    return bool(vals) and all(float(v) == 0 for v in vals)

def refresh_models(force=False):
    with db() as con: last = get_kv(con, "models_refreshed_at")
    if not force and last:
        try:
            if datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(days=1): return
        except Exception: pass
    try:
        data = fetch_json(OPENROUTER_MODELS, timeout=30).get("data", [])
        rows = []
        for m in data:
            top = m.get("top_provider") or {}; mid = m.get("id")
            ctx = min(int(m.get("context_length") or 0), int(top.get("context_length") or m.get("context_length") or 0))
            if mid and model_is_suitable(m): rows.append((mid, ctx, 1, m.get("owned_by") or "", now_iso()))
        rows.sort(key=lambda r: (-r[1], r[0]))
        with db() as con:
            con.execute("delete from models")
            for rank, r in enumerate(rows, 1):
                con.execute("insert into models(model_id,context_length,is_free,provider,rank,last_seen_at) values(?,?,?,?,?,?)", (r[0],r[1],r[2],r[3],rank,r[4]))
            set_kv(con, "models_refreshed_at", now_iso())
    except Exception as e: print("model refresh failed", e)

def eligible_models():
    refresh_models(False)
    with db() as con:
        rows = con.execute('''select m.model_id from models m left join model_failures f using(model_id)
            where m.is_free=1 and m.context_length>=1000000
            order by case when coalesce(f.failures,0)>=3 and f.last_failed_at > ? then 1 else 0 end, coalesce(f.failures,0), m.rank''',
            ((datetime.now(timezone.utc)-timedelta(hours=6)).isoformat(timespec="seconds"),)).fetchall()
    return [r[0] for r in rows] or ["openrouter/owl-alpha", "nvidia/nemotron-3-ultra-550b-a55b:free"]

def record_model_failure(model, err):
    with db() as con:
        con.execute("insert into model_failures(model_id,failures,last_error,last_failed_at) values(?,1,?,?) on conflict(model_id) do update set failures=failures+1,last_error=excluded.last_error,last_failed_at=excluded.last_failed_at", (model, str(err)[:1000], now_iso()))

def record_model_success(model):
    with db() as con: con.execute("delete from model_failures where model_id=?", (model,))

def model_refresher():
    while True:
        time.sleep(3600)
        refresh_models(False)

# ---------- generation ----------
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
    r = requests.post(OPENROUTER_CHAT, timeout=180, headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json", "HTTP-Referer":"https://hn-readtheroom.exe.xyz/", "X-Title":"HN Read The Room"}, json={"model": model, "messages":[{"role":"user","content":prompt}], "temperature":0.25})
    if r.status_code >= 400: raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]["content"].strip()

def mark_processing(hn_id):
    with db() as con:
        con.execute("insert into summaries(hn_id,status,error) values(?,'processing',null) on conflict(hn_id) do update set status='processing',error=null", (hn_id,))

def generate_summary(hn_id, force=False):
    if not force:
        cached = cached_summary(hn_id)
        if cached: return cached
    with locks_guard: lock = locks.setdefault(hn_id, threading.Lock())
    with lock:
        if not force:
            cached = cached_summary(hn_id)
            if cached: return cached
        meta = validate_hn_item(hn_id); mark_processing(hn_id)
        comments, count = hn_thread(hn_id)
        if count == 0 and (meta.get("descendants") or 0) == 0: raise ValueError("This HN item is valid, but it does not appear to have comments yet.")
        errors = []
        for model in eligible_models():
            try:
                md = call_openrouter(model, prompt_for(meta, comments)); gen = now_iso(); record_model_success(model)
                with db() as con:
                    con.execute('''insert into summaries(hn_id,markdown,model,generated_at,prompt_version,comment_count,status,error)
                    values(?,?,?,?,?,?,'done',null)
                    on conflict(hn_id) do update set markdown=excluded.markdown,model=excluded.model,generated_at=excluded.generated_at,prompt_version=excluded.prompt_version,comment_count=excluded.comment_count,status='done',error=null''', (hn_id, md, model, gen, PROMPT_VERSION, count))
                return cached_summary(hn_id)
            except Exception as e:
                record_model_failure(model, e); errors.append(f"{model}: {e}")
        with db() as con: con.execute("insert into summaries(hn_id,status,error) values(?,'error',?) on conflict(hn_id) do update set status='error',error=excluded.error", (hn_id, "\n".join(errors)))
        raise RuntimeError("All eligible OpenRouter models failed. " + (errors[-1] if errors else ""))

def generation_task(hn_id, force=False):
    try: generate_summary(hn_id, force=force)
    except Exception as e:
        with db() as con: con.execute("insert into summaries(hn_id,status,error) values(?,'error',?) on conflict(hn_id) do update set status='error',error=excluded.error", (hn_id, str(e)))
    finally:
        with locks_guard: submitted.discard(hn_id)

def ensure_generation(hn_id, force=False):
    if force: mark_processing(hn_id)
    if not force and cached_summary(hn_id): return
    with locks_guard:
        if hn_id in submitted: return
        submitted.add(hn_id)
    GEN_POOL.submit(generation_task, hn_id, force)

# ---------- UI ----------
CUSTOM_CSS = Style("""
html, body { background:#f8fafc !important; color:#0f172a !important; color-scheme: light !important; }
.dark body, .uk-section, .uk-container { background:#f8fafc !important; color:#0f172a !important; }
h1,h2,h3,h4,p,article,li,label,.uk-article,.uk-card { color:#0f172a !important; }
.text-slate-600 { color:#475569 !important; } .text-slate-500 { color:#64748b !important; }
.uk-card { background:rgba(255,255,255,.94) !important; border:1px solid #e2e8f0 !important; box-shadow:0 12px 30px rgba(15,23,42,.05) !important; }
.uk-input, input { background:#fff !important; color:#0f172a !important; border-color:#cbd5e1 !important; }
.uk-input::placeholder, input::placeholder { color:#94a3b8 !important; opacity:1 !important; }
.uk-btn-primary { background:#0f172a !important; color:#fff !important; border-color:#0f172a !important; }
.uk-btn-default, .uk-btn-secondary { background:#fff !important; color:#0f172a !important; border-color:#cbd5e1 !important; }
a { color:inherit; }
.prose, .prose p, .prose li { color:#1e293b !important; }
.prose h1, .prose h2, .prose h3 { color:#0f172a !important; }
.stat-card { background:#fff !important; border:1px solid #e2e8f0 !important; }
@media (max-width: 640px) {
  h1, .uk-article-title { font-size: 2.6rem !important; line-height: 1.05 !important; }
  .text-5xl, .sm\\:text-6xl { font-size: 3.4rem !important; line-height: .95 !important; }
  .text-xl { font-size: 1.25rem !important; line-height: 1.55 !important; }
  .uk-card-body { padding: 1.35rem !important; }
  .prose h2 { font-size: 2rem !important; line-height: 1.15 !important; }
  .prose p, .prose li { font-size: 1.18rem !important; line-height: 1.65 !important; }
}
""")

def footer():
    return Footer("Built with FastHTML + MonsterUI + SQLite", cls="text-center text-sm text-slate-500 mt-14 pb-8")

def page(*content): return Title(APP_TITLE), Container(Div(*content, footer(), cls="max-w-4xl mx-auto py-10 px-4"))
def error_card(msg): return Card(P(str(msg), cls="text-red-900"), header=H3("Couldn’t read the room", cls="text-red-700"), cls="border border-red-200 bg-red-50 shadow-sm")
def stat(label, value): return Div(P(label, cls="text-xs uppercase tracking-widest text-slate-500"), P(value if value is not None else "—", cls="text-lg font-medium"), cls="stat-card rounded-xl p-4")

def loading_card(hn_id, title="Reading the room"):
    return Card(Div(Div(cls="h-2 w-2 rounded-full bg-slate-900 animate-ping"), P("Fetching comments, choosing a long-context free model, and writing a cached sentiment brief…", cls="text-slate-600"), cls="flex items-center gap-4"), Div("This usually takes 20–90 seconds for a new item.", cls="text-sm text-slate-500 mt-4"), hx_get=f"/status?id={hn_id}", hx_trigger="load delay:2s, every 4s", hx_swap="outerHTML", header=H2(title, cls="text-2xl font-medium"), cls="shadow-sm")

def summary_view(row):
    meta = [stat("HN score", row["score"]), stat("comments", row["comment_count"] or row["descendants"]), stat("posted by", row["by"])]
    links = [A("Open on HN ↗", href=row['hn_url'], target="_blank", rel="noopener", cls="uk-btn uk-btn-primary")]
    if row["url"]: links.append(A("Original article ↗", href=row["url"], target="_blank", rel="noopener", cls="uk-btn uk-btn-default"))
    return Div(
        Div(A("← Analyze another", href="/", cls="uk-btn uk-btn-default"), Form(Input(type="hidden", name="id", value=str(row['hn_id'])), Button("Regenerate", cls=(ButtonT.secondary,)), method="post", action="/regenerate", cls="inline"), cls="flex justify-between gap-3 mb-8"),
        Article(ArticleTitle(A(row['title'], href=row['hn_url'], target="_blank", rel="noopener", cls="hover:underline")), ArticleMeta(f"generated by {row['model']} on {nice_date(row['generated_at'])} · prompt {display_prompt_version(row['prompt_version'])}"), Div(*meta, cls="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-6 mb-6"), Div(*links, cls="flex flex-wrap gap-3 mb-8"), Div(render_md(row['markdown']), cls="mt-8 prose prose-slate max-w-none leading-8")))

app, rt = fast_app(hdrs=(Script("localStorage.setItem('__FRANKEN__', JSON.stringify({mode:'light'})); document.documentElement.classList.remove('dark');"), *Theme.slate.headers(), CUSTOM_CSS), title=APP_TITLE, live=False)

@rt('/')
def get():
    return page(
        Div(P("Hacker News", cls="tracking-[0.35em] uppercase text-sm text-slate-500"), H1("Read The Room", cls="text-5xl sm:text-6xl font-semibold tracking-tight mt-3"), P("Paste a Hacker News item ID or URL. We’ll distill the comments into a calm, cached sentiment brief — what people agree on, where they push back, and the jokes underneath.", cls="text-xl text-slate-600 mt-5 max-w-3xl mx-auto"), cls="text-center mb-10"),
        Card(Form(Label("HN item ID or URL", For="q", cls="font-medium"), Input(name="q", id="q", placeholder="43875136 or https://news.ycombinator.com/item?id=43875136", required=True, cls="uk-input text-lg"), Button("Read the room", cls=(ButtonT.primary, "mt-3 w-full")), Div(id="form-status", cls="text-sm text-slate-500 mt-2"), method="post", action="/analyze", hx_post="/analyze", hx_target="#form-status", hx_swap="innerHTML", hx_indicator="#form-status"), header=H2("Analyze a discussion", cls="text-2xl font-medium"), cls="shadow-sm bg-white/90"))

@rt('/analyze')
def post(q: str, request: Request):
    try:
        rate_limit(client_key(request), 60); hn_id = parse_hn_input(q); validate_hn_item(hn_id)
    except Exception as e: return error_card(e)
    return Div("Validated. Opening discussion…", Script(f"window.location='/item?id={hn_id}'"), cls="text-slate-600")

@rt('/item')
def get(id: str = ""):
    try:
        hn_id = parse_hn_input(id)
        row = cached_summary(hn_id)
        if row: return page(summary_view(row))
        validate_hn_item(hn_id); ensure_generation(hn_id)
        item = item_row(hn_id)
        return page(A("← Analyze another", href="/", cls="uk-btn uk-btn-default mb-6"), H1(item['title'], cls="text-4xl font-semibold tracking-tight mb-3"), P("A fresh summary is being generated and will be cached for the next visit.", cls="text-slate-600 mb-6"), loading_card(hn_id))
    except Exception as e:
        return page(error_card(e), A("Back home", href="/", cls="uk-btn uk-btn-default mt-5"))

@rt('/status')
def get(id: str = ""):
    try: hn_id = parse_hn_input(id)
    except Exception as e: return error_card(e)
    row = summary_row(hn_id)
    if row and row["status"] == "done": return Div(Script("window.location.reload()"), P("Done. Loading summary…", cls="text-slate-600"))
    if row and row["status"] == "error": return error_card(row["error"] or "Generation failed.")
    ensure_generation(hn_id)
    return loading_card(hn_id, "Still reading the room")

@rt('/regenerate')
def post(id: str, request: Request):
    try:
        rate_limit(client_key(request), 6); hn_id = parse_hn_input(id); validate_hn_item(hn_id); ensure_generation(hn_id, force=True)
        return RedirectResponse(f"/item?id={hn_id}", status_code=303)
    except Exception as e:
        return page(error_card(e), A("Back", href=f"/item?id={id}", cls="uk-btn uk-btn-default mt-5"))

@rt('/healthz')
def get():
    try:
        with db() as con: con.execute("select 1").fetchone()
        return PlainTextResponse("ok")
    except Exception as e: return PlainTextResponse(f"error: {e}", status_code=500)

init_db(); refresh_models(False)
threading.Thread(target=model_refresher, daemon=True).start()
if __name__ == '__main__': serve(host='0.0.0.0', port=int(os.getenv('PORT', '8000')), reload=False)
