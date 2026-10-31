#!/usr/bin/env python3
"""
Cutix demo chatbot – rewritten lead-generation version
Standard library only.
"""

import csv
import datetime
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
LOGS = ROOT / "logs"

FALLBACK = ("Sorry, I can't answer that right now. Please reach the Cutix team: "
            "+234 704 623 6426 (call or WhatsApp) or info@cutixplc.com.ng.")

# Stages
STAGES = ("answering", "intent_detected", "qualifying", "awaiting_name", "lead_captured", "routed")


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


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------
META_COLS = {"product", "type_code", "voltage", "standard", "application",
             "conditions", "source", "verification", "flags"}


def build_context():
    parts = []
    kb_path = DATA / "knowledge_base.md"
    if kb_path.exists():
        kb = kb_path.read_text(encoding="utf-8")
        cut = kb.find("## 13. Pages still not captured")
        if cut != -1:
            kb = kb[:cut]
        parts.append("=== CUTIX KNOWLEDGE BASE ===\n" + kb.strip())

    dist = DATA / "distributors.csv"
    if dist.exists():
        rows = list(csv.DictReader(dist.open(encoding="utf-8")))
        lines = ["state | place | dealer | address | phones"]
        for r in rows:
            lines.append(" | ".join([r.get("state", ""), r.get("place", ""),
                                     r.get("dealer", ""), r.get("address", ""),
                                     r.get("phones", "")]))
        parts.append("=== APPROVED DISTRIBUTORS AND DEALERS ===\n" + "\n".join(lines))

    for spec in sorted(DATA.glob("spec_*.csv")):
        rows = list(csv.DictReader(spec.open(encoding="utf-8")))
        if not rows:
            continue
        r0 = rows[0]
        cols = [c for c in rows[0].keys() if c not in META_COLS]
        head = (f"TABLE: {r0.get('product','')} ({r0.get('type_code','')}, {r0.get('voltage','')}); "
                f"standard: {r0.get('standard','')}; use: {r0.get('application','')}. "
                "Quote values only as 'per Cutix's catalogue'.")
        lines = [head, " | ".join(cols)]
        for r in rows:
            lines.append(" | ".join(r.get(c, "") for c in cols))
        parts.append("=== CATALOGUE TABLE ===\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_system_prompt():
    tpl_path = DATA / "system_prompt.md"
    tpl = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else ""
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


# ---------------------------------------------------------------------------
# Model providers
# ---------------------------------------------------------------------------
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
        raise LLMError("GEMINI_API_KEY is empty")
    model = env("GEMINI_MODEL", "gemini-2.0-flash")
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
        raise LLMError("Gemini returned no candidates")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise LLMError("Gemini empty reply")
    return text.strip()


def call_openai_compat(system, msgs):
    key = env("OPENAI_API_KEY")
    if not key:
        raise LLMError("OPENAI_API_KEY is empty")
    base = env("OPENAI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    payload = {"model": env("OPENAI_MODEL", "deepseek-chat"), "temperature": 0.2, "max_tokens": 700,
               "messages": [{"role": "system", "content": system}] + msgs}
    status, j = http_json(f"{base}/chat/completions", payload, {"Authorization": "Bearer " + key})
    if status != 200:
        raise LLMError(f"OpenAI-compatible HTTP {status}: {str(j)[:400]}")
    try:
        return j["choices"][0]["message"]["content"].strip()
    except Exception:
        raise LLMError("Unexpected reply shape")


def call_mock(system, msgs):
    q = msgs[-1]["content"]
    return (f"[Demo mode] You asked: \"{q[:120]}\". "
            "Add a real key in the environment. "
            "Cutix team: +234 704 623 6426 or info@cutixplc.com.ng.")


PROVIDERS = {
    "gemini": call_gemini,
    "openai": call_openai_compat,
    "mock": call_mock,
}


# ---------------------------------------------------------------------------
# Throttling & usage
# ---------------------------------------------------------------------------
_rl_lock = threading.Lock()
_calls = []


def throttle(max_wait=12):
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


def check_and_reserve(session, ip_hash):
    """Reserve a slot. Returns (ok, message, remaining). Caller must refund on failure."""
    per_session = env_int("MAX_MSGS_PER_SESSION", 20)
    per_ip = env_int("MAX_MSGS_PER_IP", 40)
    daily = env_int("DAILY_CAP", 300)
    with _use_lock:
        u = _load_usage()
        if u["total"] >= daily:
            return False, "The demo has reached its daily limit. Please try again tomorrow or contact Cutix on +234 704 623 6426.", 0
        s = u["sessions"].get(session, 0)
        i = u["ips"].get(ip_hash, 0)
        if s >= per_session or i >= per_ip:
            return False, "You've reached the message limit for this demo. Contact the Cutix team on +234 704 623 6426.", 0
        u["total"] += 1
        u["sessions"][session] = s + 1
        u["ips"][ip_hash] = i + 1
        USAGE_FILE.write_text(json.dumps(u), encoding="utf-8")
        return True, "", per_session - (s + 1)


def refund(session, ip_hash):
    with _use_lock:
        u = _load_usage()
        u["total"] = max(0, u.get("total", 0) - 1)
        if session in u["sessions"]:
            u["sessions"][session] = max(0, u["sessions"][session] - 1)
        if ip_hash in u["ips"]:
            u["ips"][ip_hash] = max(0, u["ips"][ip_hash] - 1)
        USAGE_FILE.write_text(json.dumps(u), encoding="utf-8")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
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
                message_count INTEGER NOT NULL DEFAULT 0,
                whatsapp_tapped INTEGER NOT NULL DEFAULT 0
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
    ttl_min = env_int("SESSION_TTL_MINUTES", 45)
    with _db_lock:
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (session_id,)).fetchone()
            if row:
                last = datetime.datetime.fromisoformat(row["last_active"])
                age = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds() / 60
                if age > ttl_min:
                    # expired – start fresh
                    conn.execute("DELETE FROM turns WHERE conversation_id = ?", (session_id,))
                    conn.execute("DELETE FROM conversations WHERE id = ?", (session_id,))
                    conn.commit()
                else:
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
                "lead_location": None, "routed_to": None, "message_count": 0,
                "whatsapp_tapped": 0
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


def log_turn(conversation_id, role, content, ms=None, provider=None, error=None):
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


def get_recent_turns(conversation_id, limit=6):
    with _db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT role, content FROM turns "
                "WHERE conversation_id = ? AND error IS NULL "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, limit)
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Lead extraction (improved)
# ---------------------------------------------------------------------------
INTENT_RE = re.compile(
    r"\b(i\s+need|i\s+want|looking\s+for|i\s+would\s+like|i'd\s+like|order|quote\s+for|"
    r"purchase|supply|for\s+my\s+(shop|house|project|site|building|office|warehouse)|"
    r"how\s+(do|can)\s+i\s+(order|get|buy)|request\s+a\s+quote|get\s+a\s+quote)\b",
    re.I
)

LOCATOR_RE = re.compile(
    r"\b(where\s+(can|do|to)\s+i\s+(buy|find|get)|nearest\s+(dealer|branch|distributor)|"
    r"dealers?\s+in|branches?\s+in|where\s+to\s+buy)\b",
    re.I
)

# Size patterns to strip before looking for quantity
SIZE_RE = re.compile(r"\b\d+(\.\d+)?\s*mm2?\b", re.I)

# Quantity must have a unit
QUANTITY_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(m|meters?|metres?|rolls?|coils?|drums?|pieces?|pcs?|lengths?)\b",
    re.I
)

KNOWN_LOCATIONS = {
    "lagos", "abuja", "port harcourt", "ph", "kano", "ibadan", "kaduna", "enugu",
    "onitsha", "aba", "jos", "benin", "warri", "calabar", "uyo", "owerri", "ilorin",
    "abeokuta", "akure", "asaba", "awka", "nnewi", "fct"
}

LOCATION_RE = re.compile(
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


def extract_quantity(text):
    """Strip cable sizes first, then require a unit. Reject pure phone numbers."""
    cleaned = SIZE_RE.sub(" ", text)
    matches = QUANTITY_RE.findall(cleaned)
    for num, unit in matches:
        digits = re.sub(r"\D", "", num)
        if len(digits) >= 10:          # phone-like
            continue
        return f"{num}{unit}".strip()
    return None


def extract_location(text):
    m = LOCATION_RE.search(text)
    if not m:
        return None
    loc = (m.group(2) or m.group(0)).strip()
    loc = re.sub(r"^(in|at|from|around|near)\s+", "", loc, flags=re.I).strip()
    if len(loc) < 3:
        return None
    # prefer known places
    low = loc.lower()
    for known in KNOWN_LOCATIONS:
        if known in low:
            return known.title() if known != "ph" else "Port Harcourt"
    return loc[:60]


def extract_name(text, awaiting=False):
    m = NAME_RE.search(text)
    if m:
        name = m.group(1).strip()[:60]
        if len(name) > 1 and not any(w in name.lower() for w in ("need", "want", "looking", "order", "quote")):
            return name
    if awaiting:
        # short reply with no digits or question mark → treat as name
        cleaned = text.strip()
        if (len(cleaned.split()) <= 4 and
            not re.search(r"\d|\?", cleaned) and
            cleaned.lower() not in ("no", "skip", "later", "none", "n/a")):
            return cleaned[:60]
        if cleaned.lower() in ("no", "skip", "later", "none", "n/a"):
            return "__declined__"
    return None


def extract_need(text):
    if not INTENT_RE.search(text) or LOCATOR_RE.search(text):
        return None
    m = re.search(
        r"(?:i\s+need|i\s+want|looking\s+for|i\s+would\s+like|i'd\s+like|order|quote\s+for|supply)\s+(.{5,100})",
        text, re.I
    )
    if m:
        return m.group(1).strip()[:120]
    cleaned = re.sub(r"\b(where|can|i|buy|find|dealer|branch)\b", "", text, flags=re.I).strip()
    if len(cleaned) > 8:
        return cleaned[:120]
    return None


def extract_lead_fields(text, current, awaiting_name=False):
    """Latest-wins for every field."""
    updated = dict(current)

    name = extract_name(text, awaiting=awaiting_name)
    if name:
        updated["lead_name"] = None if name == "__declined__" else name

    qty = extract_quantity(text)
    if qty:
        updated["lead_quantity"] = qty

    loc = extract_location(text)
    if loc:
        updated["lead_location"] = loc

    need = extract_need(text)
    if need:
        updated["lead_need"] = need

    return updated


def advance_stage(conv, user_text, bot_reply, quote_request=False):
    stage = conv.get("stage") or "answering"
    fields = {
        "lead_name": conv.get("lead_name"),
        "lead_need": conv.get("lead_need"),
        "lead_quantity": conv.get("lead_quantity"),
        "lead_location": conv.get("lead_location"),
    }

    awaiting = stage == "awaiting_name"
    fields = extract_lead_fields(user_text, fields, awaiting_name=awaiting)

    is_locator = bool(LOCATOR_RE.search(user_text))
    has_intent = bool(INTENT_RE.search(user_text)) and not is_locator

    if quote_request:
        has_intent = True

    # Stage progression
    if stage == "answering" and (has_intent or quote_request):
        stage = "intent_detected"

    if stage in ("intent_detected", "qualifying", "awaiting_name"):
        stage = "qualifying"

    has_need = bool(fields.get("lead_need"))
    has_qty = bool(fields.get("lead_quantity"))
    has_loc = bool(fields.get("lead_location"))
    has_name = fields.get("lead_name") is not None   # even declined counts as “handled”

    if stage == "qualifying" and has_need and has_qty and has_loc:
        if not has_name:
            stage = "awaiting_name"
        else:
            stage = "lead_captured"

    if stage == "awaiting_name" and has_name:
        stage = "lead_captured"

    if stage == "lead_captured":
        stage = "routed"

    # After hand-off, a new clear buying intent starts a fresh lead
    if stage == "routed" and has_intent and not quote_request:
        # reset fields except name (keep name if we already have it)
        fields = {
            "lead_name": fields.get("lead_name"),
            "lead_need": fields.get("lead_need") if has_intent else None,
            "lead_quantity": None,
            "lead_location": None,
        }
        stage = "qualifying"

    routed_to = conv.get("routed_to")
    if stage in ("lead_captured", "routed") and not routed_to:
        routed_to = "whatsapp"

    qualified = 1 if stage in ("lead_captured", "routed") else 0

    return {
        "stage": stage,
        "qualified": qualified,
        "lead_name": fields.get("lead_name"),
        "lead_need": fields.get("lead_need"),
        "lead_quantity": fields.get("lead_quantity"),
        "lead_location": fields.get("lead_location"),
        "routed_to": routed_to,
    }, qualified and not conv.get("qualified")


def build_lead_summary(conv, additional=False):
    parts = []
    if conv.get("lead_name"):
        parts.append(f"Name: {conv['lead_name']}")
    if conv.get("lead_need"):
        parts.append(f"Need: {conv['lead_need']}")
    if conv.get("lead_quantity"):
        parts.append(f"Qty: {conv['lead_quantity']}")
    if conv.get("lead_location"):
        parts.append(f"Location: {conv['lead_location']}")
    body = "\n".join(parts) if parts else ""
    if additional and body:
        return "Additional to my earlier message:\n\n" + body
    return body


def build_whatsapp_url(summary=""):
    base = "https://wa.me/2347046236426"
    if summary:
        text = "Hello Cutix, I was speaking with the website assistant.\n\n" + summary
        return base + "?text=" + quote(text)
    return base + "?text=" + quote("Hello Cutix")


def build_confirmation(conv):
    """Server-built confirmation message."""
    bits = []
    if conv.get("lead_quantity"):
        bits.append(conv["lead_quantity"])
    if conv.get("lead_need"):
        bits.append(conv["lead_need"])
    if conv.get("lead_location"):
        bits.append("in " + conv["lead_location"])
    if not bits:
        return None
    msg = "Just to confirm: " + ", ".join(bits) + ". Is that right?"
    if not conv.get("lead_name"):
        msg += " And your name?"
    return msg


# ---------------------------------------------------------------------------
# Answer
# ---------------------------------------------------------------------------
def answer(message, history=None, paced=True, lead_state=None):
    provider = env("PROVIDER", "mock").lower()
    fn = PROVIDERS.get(provider)
    if not fn:
        return FALLBACK, f"Unknown PROVIDER '{provider}'"

    hist_window = env_int("HISTORY_WINDOW", 6)
    msgs = []
    for h in (history or [])[-hist_window:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            msgs.append({"role": h["role"], "content": h["content"][:1500]})
    msgs.append({"role": "user", "content": message})
    msgs = clean_messages(msgs)

    system = SYSTEM_PROMPT
    if lead_state:
        system += "\n\nCurrent lead state (use for confirmation and summary):\n" + json.dumps(lead_state)

    if paced and provider != "mock" and not throttle():
        return "I'm getting a lot of questions right now. Please try again in a few seconds.", "throttled"
    try:
        return fn(system, msgs), None
    except LLMError as e:
        return FALLBACK, str(e)
    except Exception as e:
        return FALLBACK, f"{type(e).__name__}: {e}"


def guard_invented_contacts(text):
    """Replace reply if it invents a phone or email not in the allowed set."""
    phones = PHONE_RE.findall(text)
    emails = EMAIL_RE.findall(text)
    for p in phones:
        if norm_phone(p) not in ALLOWED_NUMBERS:
            return FALLBACK, True
    for e in emails:
        if e.lower() not in ALLOWED_EMAILS:
            return FALLBACK, True
    return text, False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CutixDemo/0.5"

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

    def _ip_hash(self):
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        return hashlib.sha256(ip.encode()).hexdigest()[:16]

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            if f.exists() and f.is_file():
                data = f.read_bytes()
                ctype = "image/png" if f.suffix == ".png" else "application/octet-stream"
                self._send(200, data, ctype)
                return
            self._send(404, "Not found")
            return

        if path == "/config.json":
            cfg = {
                "whatsapp": "https://wa.me/2347046236426",
                "contactEmail": env("CONTACT_EMAIL", ""),
                "poweredBy": env("POWERED_BY", ""),
                "accessCodeRequired": bool(env("ACCESS_CODE")),
            }
            self._send(200, cfg)
            return

        if path == "/stats":
            token = env("ADMIN_TOKEN")
            auth = self.headers.get("Authorization", "")
            if not token or auth != f"Bearer {token}":
                self._send(401, {"error": "Unauthorized"})
                return
            with _db_lock:
                conn = get_db()
                try:
                    rows = conn.execute(
                        "SELECT id, stage, qualified, lead_name, lead_need, lead_quantity, "
                        "lead_location, message_count, started_at FROM conversations "
                        "ORDER BY last_active DESC LIMIT 100"
                    ).fetchall()
                    stats = []
                    for r in rows:
                        name = r["lead_name"] or ""
                        if name and " " in name:
                            name = name.split()[0] + " " + name.split()[-1][0] + "."
                        stats.append({
                            "id": r["id"][:8],
                            "stage": r["stage"],
                            "qualified": bool(r["qualified"]),
                            "name": name,
                            "need": (r["lead_need"] or "")[:40],
                            "qty": r["lead_quantity"],
                            "loc": r["lead_location"],
                            "msgs": r["message_count"],
                            "started": r["started_at"],
                        })
                    self._send(200, {"conversations": stats})
                finally:
                    conn.close()
            return

        if path == "/session":
            # restore chat for short-lived session
            session = self.headers.get("X-Session-Id") or ""
            if not session:
                self._send(400, {"error": "missing session"})
                return
            conv = get_or_create_conversation(session)
            turns = get_recent_turns(session, limit=20)
            self._send(200, {"stage": conv["stage"], "turns": turns, "qualified": bool(conv["qualified"])})
            return

        self._send(404, "Not found")

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "bad json"})
            return

        session = (body.get("session") or "").strip() or "anon"
        message = (body.get("message") or "").strip()[:600]
        quote_request = bool(body.get("quote_request"))
        code = (body.get("code") or "").strip()
        access = env("ACCESS_CODE")
        if access and code != access:
            self._send(401, {"error": "invalid code"})
            return

        if not message and not quote_request:
            self._send(400, {"error": "empty message"})
            return

        ip_hash = self._ip_hash()
        conv = get_or_create_conversation(session)

        # Quote button: server asks first question, no model call
        if quote_request:
            reply = "Tell me what you need and I'll start your quote. What size and length are you looking for?"
            log_turn(session, "user", "[quote_request]")
            log_turn(session, "assistant", reply)
            update_conversation(session, stage="qualifying")
            self._send(200, {
                "reply": reply,
                "qualified": False,
                "whatsapp_url": None,
                "remaining": None,
            })
            return

        # Reserve usage slot
        ok, err_msg, remaining = check_and_reserve(session, ip_hash)
        if not ok:
            self._send(429, {"error": err_msg})
            return

        # Server-side history
        history = get_recent_turns(session, limit=env_int("HISTORY_WINDOW", 6))

        t0 = time.time()
        lead_state = {
            "name": conv.get("lead_name"),
            "need": conv.get("lead_need"),
            "quantity": conv.get("lead_quantity"),
            "location": conv.get("lead_location"),
            "stage": conv.get("stage"),
        }
        reply, error = answer(message, history=history, lead_state=lead_state)
        ms = int((time.time() - t0) * 1000)

        if error:
            refund(session, ip_hash)
            log_turn(session, "user", message)
            log_turn(session, "assistant", reply, ms=ms, provider=env("PROVIDER"), error=error)
            self._send(200, {"reply": reply, "qualified": False, "remaining": remaining})
            return

        # Guard invented contacts
        reply, was_replaced = guard_invented_contacts(reply)
        if was_replaced:
            error = "invented_contact"

        # Advance stage
        updated, newly = advance_stage(conv, message, reply, quote_request=False)
        update_conversation(session, **updated)

        log_turn(session, "user", message)
        log_turn(session, "assistant", reply, ms=ms, provider=env("PROVIDER"), error=error)

        # Confirmation when we just got the three fields
        if updated["stage"] == "awaiting_name" and not conv.get("lead_name"):
            conf = build_confirmation(updated)
            if conf:
                reply = conf

        whatsapp_url = None
        if updated["qualified"]:
            additional = bool(conv.get("whatsapp_tapped"))
            summary = build_lead_summary(updated, additional=additional)
            whatsapp_url = build_whatsapp_url(summary)

        self._send(200, {
            "reply": reply,
            "qualified": bool(updated["qualified"]),
            "whatsapp_url": whatsapp_url,
            "remaining": remaining,
            "stage": updated["stage"],
        })

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Session-Id")
        self.end_headers()


def main():
    init_db()
    host = env("HOST", "0.0.0.0")
    port = env_int("PORT", 8000)
    print(f"Cutix chatbot listening on http://{host}:{port}")
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
