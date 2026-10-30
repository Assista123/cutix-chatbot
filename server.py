#!/usr/bin/env python3
"""Cutix demo chatbot server v0.4 — lead-generation restructure.
Standard library only: no installs needed.

Run:  python server.py   then open http://localhost:8000
Settings live in the .env file (copy .env.example to .env).

V2 changes:
- SQLite (WAL) primary logging + JSONL archival
- Server-side qualification state machine
- Lead fields (name, need, quantity, location) + routing outcome
- History window default trimmed to 4
- Gentle lead-gen prompt (v0.4)
- Groq/Llama stack noted for later (use Gemini free tier for demo)
"""
import csv
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
LOGS = ROOT / "logs"

FALLBACK = ("Sorry, I can't answer that right now. Please reach the Cutix team: "
            "+234 704 623 6426 (call or WhatsApp) or info@cutixplc.com.ng.")

# Qualification stages
STAGES = ("answering", "intent_detected", "qualifying", "lead_captured", "routed")


def load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def env(name, default=""):
    return os.environ.get(name, default)


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


# ----------------------------------------------------------------------------
# Knowledge: build the context that is placed inside the system prompt
# ----------------------------------------------------------------------------
META_COLS = {"product", "type_code", "voltage", "standard", "application",
             "conditions", "source", "verification", "flags"}


def build_context():
    parts = []
    kb = (DATA / "knowledge_base.md").read_text(encoding="utf-8")
    # Everything from section 13 onward is internal working notes, not for the bot.
    cut = kb.find("## 13. Pages still not captured")
    if cut != -1:
        kb = kb[:cut]
    parts.append("=== CUTIX KNOWLEDGE BASE ===\n" + kb.strip())

    dist = DATA / "distributors.csv"
    if dist.exists():
        rows = list(csv.DictReader(dist.open(encoding="utf-8")))
        lines = ["state | place | dealer | address | phones"]
        for r in rows:
            lines.append(" | ".join([r["state"], r["place"], r["dealer"], r["address"], r["phones"]]))
        parts.append("=== APPROVED DISTRIBUTORS AND DEALERS (from Cutix's website; confirm by phone) ===\n"
                     + "\n".join(lines))

    for spec in sorted(DATA.glob("spec_*.csv")):
        rows = list(csv.DictReader(spec.open(encoding="utf-8")))
        if not rows:
            continue
        r0 = rows[0]
        cols = [c for c in rows[0].keys() if c not in META_COLS]
        head = (f"TABLE: {r0['product']} ({r0['type_code']}, {r0['voltage']}); standard: {r0['standard']}; "
                f"use: {r0['application']}; conditions: {r0['conditions']}; source: {r0['source']}. "
                "Quote values only as 'per Cutix's catalogue' and add the electrician/engineer disclaimer.")
        lines = [head, " | ".join(cols)]
        for r in rows:
            lines.append(" | ".join(r[c] for c in cols))
        parts.append("=== CATALOGUE TABLE ===\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_system_prompt():
    tpl = (DATA / "system_prompt.md").read_text(encoding="utf-8")
    ctx = build_context()
    if "{{retrieved_chunks}}" in tpl:
        return tpl.replace("{{retrieved_chunks}}", ctx), ctx
    return tpl + "\n\n<context>\n" + ctx + "\n</context>", ctx


SYSTEM_PROMPT, CONTEXT_TEXT = build_system_prompt()

PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")


def norm_phone(s):
    d = re.sub(r"\D", "", s)
    if d.startswith("234") and len(d) == 13:
        d = "0" + d[3:]
    return d


ALLOWED_NUMBERS = {norm_phone(m) for m in PHONE_RE.findall(CONTEXT_TEXT)}
ALLOWED_EMAILS = {m.lower() for m in EMAIL_RE.findall(CONTEXT_TEXT)}


# ----------------------------------------------------------------------------
# Model providers (Groq via openai-compat path is ready for later; Gemini for demo)
# ----------------------------------------------------------------------------
class LLMError(Exception):
    pass


def http_json(url, payload, headers, timeout=45):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:500]}


def clean_messages(msgs):
    out = [m for m in msgs if m.get("content")]
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def call_gemini(system, msgs):
    key = env("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is empty in .env")
    model = env("GEMINI_MODEL", "gemini-3.1-flash-lite")
    base = env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    contents = [{"role": "user" if m["role"] == "user" else "model",
                 "parts": [{"text": m["content"]}]} for m in msgs]
    gen = {"temperature": 0.2, "maxOutputTokens": 2048}
    extra = env("GEMINI_EXTRA_JSON")
    if extra:
        gen.update(json.loads(extra))
    payload = {"systemInstruction": {"parts": [{"text": system}]},
               "contents": contents, "generationConfig": gen}
    status, j = http_json(f"{base}/models/{model}:generateContent", payload, {"x-goog-api-key": key})
    if status != 200:
        raise LLMError(f"Gemini HTTP {status}: {str(j)[:400]}")
    cands = j.get("candidates") or []
    if not cands:
        raise LLMError("Gemini returned no candidates: " + str(j.get("promptFeedback"))[:300])
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise LLMError("Gemini empty reply, finishReason=" + str(cands[0].get("finishReason")))
    return text.strip()


def call_claude(system, msgs):
    key = env("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is empty in .env")
    payload = {"model": env("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": 700,
               "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
               "messages": msgs}
    status, j = http_json("https://api.anthropic.com/v1/messages", payload,
                          {"x-api-key": key, "anthropic-version": "2023-06-01"})
    if status != 200:
        raise LLMError(f"Claude HTTP {status}: {str(j)[:400]}")
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    if not text.strip():
        raise LLMError("Claude empty reply")
    return text.strip()


def call_openai_compat(system, msgs):
    """Works for DeepSeek, Groq (OpenAI-compatible), and other OpenAI-style APIs.
    For Groq later: OPENAI_BASE_URL=https://api.groq.com/openai/v1
                    OPENAI_MODEL=llama-3.1-8b-instant
    """
    key = env("OPENAI_API_KEY")
    if not key:
        raise LLMError("OPENAI_API_KEY is empty in .env")
    base = env("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    payload = {"model": env("OPENAI_MODEL", "deepseek-chat"), "temperature": 0.2, "max_tokens": 700,
               "messages": [{"role": "system", "content": system}] + msgs}
    status, j = http_json(f"{base}/chat/completions", payload, {"Authorization": "Bearer " + key})
    if status != 200:
        raise LLMError(f"OpenAI-compatible HTTP {status}: {str(j)[:400]}")
    try:
        return j["choices"][0]["message"]["content"].strip()
    except Exception:
        raise LLMError("Unexpected reply shape: " + str(j)[:300])


def call_mock(system, msgs):
    q = msgs[-1]["content"]
    return (f"[Demo mode: no AI model is connected.] You asked: \"{q[:120]}\". "
            "For real answers, add your model key in the .env file. "
            "Meanwhile, the Cutix team: +234 704 623 6426 or info@cutixplc.com.ng.")


PROVIDERS = {"gemini": call_gemini, "claude": call_claude, "openai": call_openai_compat, "mock": call_mock}


# ----------------------------------------------------------------------------
# Pacing and usage caps
# ----------------------------------------------------------------------------
_rl_lock = threading.Lock()
_calls = []


def throttle(max_wait=12):
    """Keep model calls under RPM_LIMIT per minute. Returns False if we had to give up."""
    rpm = env_int("RPM_LIMIT", 12)
    deadline = time.time() + max_wait
    while True:
        with _rl_lock:
            now = time.time()
            while _calls and now - _calls[0] > 60:
                _calls.pop(0)
            if len(_calls) < rpm:
                _calls.append(now)
                return True
            wait = 60 - (now - _calls[0])
        if time.time() + min(wait, 1) > deadline:
            return False
        time.sleep(min(wait, 1))


USAGE_FILE = ROOT / "usage.json"
_use_lock = threading.Lock()


def _today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _load_usage():
    try:
        u = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if u.get("date") == _today():
            return u
    except Exception:
        pass
    return {"date": _today(), "total": 0, "sessions": {}, "ips": {}}


def check_and_count(session, ip_hash):
    """Returns (ok, message, remaining_for_session)."""
    per_session = env_int("MAX_MSGS_PER_SESSION", 20)
    per_ip = env_int("MAX_MSGS_PER_IP", 40)
    daily = env_int("DAILY_CAP", 300)
    with _use_lock:
        u = _load_usage()
        if u["total"] >= daily:
            return False, "The demo has reached its daily limit. Please try again tomorrow, or contact the Cutix team on +234 704 623 6426.", 0
        s = u["sessions"].get(session, 0)
        i = u["ips"].get(ip_hash, 0)
        if s >= per_session or i >= per_ip:
            return False, "You've reached the message limit for this demo. For more help, contact the Cutix team on +234 704 623 6426 (call or WhatsApp).", 0
        u["total"] += 1
        u["sessions"][session] = s + 1
        u["ips"][ip_hash] = i + 1
        USAGE_FILE.write_text(json.dumps(u), encoding="utf-8")
        return True, "", per_session - (s + 1)


# ----------------------------------------------------------------------------
# SQLite logging + qualification state machine
# ----------------------------------------------------------------------------
DB_PATH = LOGS / "chats.db"
_db_lock = threading.Lock()


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def init_db():
    LOGS.mkdir(exist_ok=True)
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                last_active TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'answering',
                qualified INTEGER NOT NULL DEFAULT 0,
                lead_name TEXT,
                lead_need TEXT,
                lead_quantity TEXT,
                lead_location TEXT,
                routed_to TEXT,
                message_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ms INTEGER,
                provider TEXT,
                error TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );
            CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_conv_stage ON conversations(stage);
            CREATE INDEX IF NOT EXISTS idx_conv_qualified ON conversations(qualified);
        """)
        conn.commit()
        conn.close()


def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_or_create_conversation(session_id):
    now = _utcnow()
    with _db_lock:
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (session_id,)).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO conversations (id, started_at, last_active, stage) VALUES (?, ?, ?, ?)",
                (session_id, now, now, "answering")
            )
            conn.commit()
            return {
                "id": session_id, "started_at": now, "last_active": now,
                "stage": "answering", "qualified": 0,
                "lead_name": None, "lead_need": None, "lead_quantity": None,
                "lead_location": None, "routed_to": None, "message_count": 0
            }
        finally:
            conn.close()


def update_conversation(session_id, **fields):
    if not fields:
        return
    fields["last_active"] = _utcnow()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [session_id]
    with _db_lock:
        conn = get_db()
        try:
            conn.execute(f"UPDATE conversations SET {cols} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()


def log_turn_db(conversation_id, role, content, ms=None, provider=None, error=None):
    with _db_lock:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO turns (conversation_id, role, content, created_at, ms, provider, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, role, content, _utcnow(), ms, provider, error)
            )
            conn.execute(
                "UPDATE conversations SET message_count = message_count + 1, last_active = ? WHERE id = ?",
                (_utcnow(), conversation_id)
            )
            conn.commit()
        finally:
            conn.close()


def log_turn_jsonl(entry):
    """Archival JSONL (kept for easy debugging and the original design)."""
    LOGS.mkdir(exist_ok=True)
    with (LOGS / "chats.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# Simple heuristics for stage progression and lead extraction
# Intent must be a real buying signal, not pure "where can I buy / find dealers"
INTENT_RE = re.compile(
    r"\b(i\s+need|i\s+want|looking\s+for|i\s+would\s+like|order|quote\s+for|purchase|supply|"
    r"for\s+my\s+(shop|house|project|site|building|office|warehouse)|"
    r"how\s+(do|can)\s+i\s+(order|get|buy)\b)",
    re.I
)
# Pure locator questions should not start qualification
LOCATOR_RE = re.compile(
    r"\b(where\s+(can|do|to)\s+i\s+(buy|find|get)|nearest\s+(dealer|branch|distributor)|"
    r"dealers?\s+in|branches?\s+in|where\s+to\s+buy)\b",
    re.I
)
QUANTITY_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(m|meters?|metres?|rolls?|coils?|drums?|pieces?|pcs?|lengths?)?",
    re.I
)
LOCATION_HINTS = re.compile(
    r"\b(in|at|from|around|near)\s+([A-Za-z][A-Za-z\s-]{2,30})|"
    r"\b(Lagos|Abuja|Port\s*Harcourt|PH|Kano|Ibadan|Kaduna|Enugu|Onitsha|Aba|Jos|"
    r"Benin|Warri|Calabar|Uyo|Owerri|Ilorin|Abeokuta|Akure|Asaba|Awka|Nnewi|"
    r"Abia|Adamawa|Akwa\s*Ibom|Anambra|Bauchi|Bayelsa|Benue|Borno|Cross\s*River|"
    r"Delta|Ebonyi|Edo|Ekiti|Gombe|Imo|Jigawa|Kaduna|Kano|Katsina|Kebbi|Kogi|"
    r"Kwara|Lagos|Nasarawa|Niger|Ogun|Ondo|Osun|Oyo|Plateau|Rivers|Sokoto|"
    r"Taraba|Yobe|Zamfara|FCT)\b",
    re.I
)
NAME_RE = re.compile(
    r"(?:my\s+name\s+is|i\s+am|i'?m|this\s+is|name['\s:]+)\s*([A-Za-z][A-Za-z\s'-]{1,40})",
    re.I
)
HANDOFF_RE = re.compile(
    r"(got\s+it|i.?ll\s+pass|pass\s+this|hand\s*off|reach\s+them\s+directly|"
    r"whatsapp.*704\s*623\s*6426|quote\s+form|speak\s+with\s+the\s+cutix\s+team)",
    re.I
)


def extract_lead_fields(text, current):
    """Update lead fields from user text using simple patterns. Never overwrite with empty."""
    updated = dict(current)
    # Name
    m = NAME_RE.search(text)
    if m and not updated.get("lead_name"):
        name = m.group(1).strip()[:60]
        if len(name) > 1 and not any(w in name.lower() for w in ("need", "want", "looking", "order")):
            updated["lead_name"] = name
    # Quantity
    m = QUANTITY_RE.search(text)
    if m and not updated.get("lead_quantity"):
        qty = m.group(0).strip()[:40]
        # Avoid tiny numbers that are just cable sizes (e.g. 2.5mm)
        if not re.match(r"^\d+(\.\d+)?\s*mm", qty, re.I):
            updated["lead_quantity"] = qty
    # Location
    m = LOCATION_HINTS.search(text)
    if m and not updated.get("lead_location"):
        loc = (m.group(2) or m.group(0)).strip()[:60]
        loc = re.sub(r"^(in|at|from|around|near)\s+", "", loc, flags=re.I).strip()
        if len(loc) > 2:
            updated["lead_location"] = loc
    # Need: only from clear buying-intent phrasing, never from pure locator questions
    if not updated.get("lead_need") and INTENT_RE.search(text) and not LOCATOR_RE.search(text):
        need_m = re.search(
            r"(?:i\s+need|i\s+want|looking\s+for|i\s+would\s+like|order|quote\s+for|supply)\s+(.{5,80})",
            text, re.I
        )
        if need_m:
            updated["lead_need"] = need_m.group(1).strip()[:120]
        else:
            # Fallback: take a short clean slice only if it looks like a product request
            cleaned = re.sub(r"\b(where|can|i|buy|find|dealer|branch)\b", "", text, flags=re.I).strip()
            if len(cleaned) > 8:
                updated["lead_need"] = cleaned[:120]
    return updated


def advance_stage(conv, user_text, bot_reply):
    """Progress the qualification state machine.

    Rules (V2 tightened):
    - Pure locator questions ("where can I buy…") never start qualification.
    - Intent moves us into qualifying.
    - A lead is only qualified when ALL THREE are present: Need + Quantity + Location.
    - Name is preferred but not required for the qualified flag.
    - Bot hand-off language only moves to 'routed' after the three fields exist.
    """
    stage = conv.get("stage") or "answering"
    fields = {
        "lead_name": conv.get("lead_name"),
        "lead_need": conv.get("lead_need"),
        "lead_quantity": conv.get("lead_quantity"),
        "lead_location": conv.get("lead_location"),
    }
    fields = extract_lead_fields(user_text, fields)
    newly_qualified = False

    is_locator = bool(LOCATOR_RE.search(user_text))
    has_buying_intent = bool(INTENT_RE.search(user_text)) and not is_locator

    # Stay in answering for pure locator questions
    if stage == "answering" and is_locator and not has_buying_intent:
        pass  # remain answering
    elif stage == "answering" and has_buying_intent:
        stage = "intent_detected"

    if stage in ("intent_detected", "qualifying"):
        stage = "qualifying"

    has_need = bool(fields.get("lead_need"))
    has_qty = bool(fields.get("lead_quantity"))
    has_loc = bool(fields.get("lead_location"))
    all_three = has_need and has_qty and has_loc

    if stage == "qualifying" and all_three:
        stage = "lead_captured"
        newly_qualified = True

    # Only allow routed after we actually have the three fields
    if stage == "lead_captured":
        if HANDOFF_RE.search(bot_reply or "") or all_three:
            stage = "routed"

    routed_to = conv.get("routed_to")
    if stage in ("lead_captured", "routed") and not routed_to:
        routed_to = "whatsapp"

    qualified = 1 if stage in ("lead_captured", "routed") else 0

    updated = {
        "stage": stage,
        "qualified": qualified,
        "lead_name": fields.get("lead_name"),
        "lead_need": fields.get("lead_need"),
        "lead_quantity": fields.get("lead_quantity"),
        "lead_location": fields.get("lead_location"),
        "routed_to": routed_to,
    }
    return updated, newly_qualified or (qualified and not conv.get("qualified"))


def build_lead_summary(conv):
    parts = []
    if conv.get("lead_name"):
        parts.append(f"Name: {conv['lead_name']}")
    if conv.get("lead_need"):
        parts.append(f"Need: {conv['lead_need']}")
    if conv.get("lead_quantity"):
        parts.append(f"Qty: {conv['lead_quantity']}")
    if conv.get("lead_location"):
        parts.append(f"Location: {conv['lead_location']}")
    return " | ".join(parts) if parts else ""


def build_whatsapp_url(summary=""):
    base = "https://wa.me/2347046236426"
    if summary:
        text = "Hello Cutix, I was speaking with the website assistant.\n\n" + summary
        return base + "?text=" + urllib.request.quote(text)
    return base + "?text=" + urllib.request.quote("Hello Cutix")


# ----------------------------------------------------------------------------
# Answering
# ----------------------------------------------------------------------------
def answer(message, history=None, paced=True):
    """Ask the model. Returns (reply_text, error_text_or_None)."""
    provider = env("PROVIDER", "mock").lower()
    fn = PROVIDERS.get(provider)
    if not fn:
        return FALLBACK, f"Unknown PROVIDER '{provider}'"
    hist_window = env_int("HISTORY_WINDOW", 4)
    msgs = []
    for h in (history or [])[-hist_window:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            msgs.append({"role": h["role"], "content": h["content"][:1500]})
    msgs.append({"role": "user", "content": message})
    msgs = clean_messages(msgs)
    if paced and provider != "mock" and not throttle():
        return "I'm getting a lot of questions right now. Please try again in a few seconds.", "throttled"
    try:
        return fn(SYSTEM_PROMPT, msgs), None
    except LLMError as e:
        return FALLBACK, str(e)
    except Exception as e:  # network errors, bad JSON, etc.
        return FALLBACK, f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CutixDemo/0.4"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _client_hash(self):
        ip = self.client_address[0]
        if env("TRUST_PROXY") == "1":
            ip = self.headers.get("X-Forwarded-For", ip).split(",")[0].strip()
        return hashlib.sha256(("cutix-salt" + ip).encode()).hexdigest()[:16]

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/config.json":
            return self._send(200, {
                "accessCodeRequired": bool(env("ACCESS_CODE")),
                "poweredBy": env("POWERED_BY"),
                "contactEmail": env("CONTACT_EMAIL", "Theassistagroup@gmail.com"),
                "whatsapp": "https://wa.me/2347046236426?text=" + urllib.request.quote("Hello Cutix"),
                "quoteUrl": "https://forms.cutixplc.com.ng/sprt9u",
                "maxPerSession": env_int("MAX_MSGS_PER_SESSION", 20),
            })
        if path == "/health":
            return self._send(200, {
                "ok": True,
                "provider": env("PROVIDER", "mock"),
                "version": "0.4",
                "db": str(DB_PATH.exists()),
            })
        if path == "/stats":
            # Simple internal stats for the demo dashboard / review
            with _db_lock:
                conn = get_db()
                try:
                    total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                    qualified = conn.execute("SELECT COUNT(*) FROM conversations WHERE qualified = 1").fetchone()[0]
                    by_stage = dict(conn.execute(
                        "SELECT stage, COUNT(*) FROM conversations GROUP BY stage"
                    ).fetchall())
                    recent = [dict(r) for r in conn.execute(
                        "SELECT id, stage, qualified, lead_name, lead_need, lead_quantity, "
                        "lead_location, routed_to, message_count, started_at "
                        "FROM conversations ORDER BY last_active DESC LIMIT 20"
                    ).fetchall()]
                finally:
                    conn.close()
            return self._send(200, {
                "conversations": total,
                "qualified_leads": qualified,
                "qualification_rate": round(100 * qualified / total, 1) if total else 0,
                "by_stage": by_stage,
                "recent": recent,
            })
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            f = (STATIC / name).resolve()
            if STATIC in f.parents and f.is_file():
                ctype = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml",
                         "css": "text/css", "js": "text/javascript"}.get(f.suffix[1:], "application/octet-stream")
                return self._send(200, f.read_bytes(), ctype)
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/chat":
            return self._send(404, {"error": "Not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 20000:
                return self._send(413, {"error": "Message too large."})
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "Bad request."})

        code = env("ACCESS_CODE")
        if code and str(body.get("code", "")) != code:
            return self._send(401, {"error": "Access code needed."})

        message = str(body.get("message", "")).strip()[:600]
        if not message:
            return self._send(400, {"error": "Please type a question."})
        session = re.sub(r"[^\w-]", "", str(body.get("session", "anon")))[:64] or "anon"
        history = body.get("history") if isinstance(body.get("history"), list) else []

        ok, msg, remaining = check_and_count(session, self._client_hash())
        if not ok:
            return self._send(429, {"error": msg})

        # --- Lead-gen state machine ---
        conv = get_or_create_conversation(session)
        log_turn_db(session, "user", message)

        t0 = time.time()
        reply, err = answer(message, history)
        ms = int((time.time() - t0) * 1000)

        log_turn_db(session, "assistant", reply, ms=ms, provider=env("PROVIDER", "mock"), error=err)

        # Advance stage & extract lead fields
        updated, newly_qualified = advance_stage(conv, message, reply)
        update_conversation(session, **updated)
        conv.update(updated)

        summary = build_lead_summary(conv)
        wa_url = build_whatsapp_url(summary) if conv.get("qualified") else build_whatsapp_url()

        # Archival JSONL (enriched)
        log_turn_jsonl({
            "ts": _utcnow(),
            "session": session,
            "q": message,
            "a": reply,
            "ms": ms,
            "provider": env("PROVIDER", "mock"),
            "error": err,
            "stage": conv.get("stage"),
            "qualified": bool(conv.get("qualified")),
            "lead_name": conv.get("lead_name"),
            "lead_need": conv.get("lead_need"),
            "lead_quantity": conv.get("lead_quantity"),
            "lead_location": conv.get("lead_location"),
            "routed_to": conv.get("routed_to"),
        })

        if err:
            print(f"[model error] {err}", file=sys.stderr)

        resp = {
            "reply": reply,
            "remaining": remaining,
            "stage": conv.get("stage"),
            "qualified": bool(conv.get("qualified")),
            "lead_summary": summary or None,
            "routed_to": conv.get("routed_to"),
            "whatsapp_url": wa_url,
        }
        return self._send(200, resp)


def main():
    init_db()
    host = env("HOST", "127.0.0.1")
    port = env_int("PORT", 8000)
    provider = env("PROVIDER", "mock")
    print(f"Cutix demo bot v0.4 (lead-gen)  |  model provider: {provider}  |  context ~{len(CONTEXT_TEXT)//4} tokens")
    print(f"SQLite: {DB_PATH}  |  history window: {env_int('HISTORY_WINDOW', 4)}")
    if provider == "mock":
        print("NOTE: running in demo mode (no AI model). Set PROVIDER=gemini and a key in .env.")
    print("NOTE: Groq/Llama stack is prepared (via openai-compat) but deferred until paid plan.")
    print(f"Open http://localhost:{port}   (Ctrl+C to stop)")
    print(f"Stats: http://localhost:{port}/stats")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
