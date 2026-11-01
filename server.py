#!/usr/bin/env python3
"""
Cutix demo chatbot v0.9
Data-driven location routing + server-controlled lead qualification.
Standard library only.

Lead flow (all decided by the server, not the model):
  answering -> qualifying -> confirming -> (awaiting_name) -> routed
The model only answers product / company questions.
"""

import csv
import datetime
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
LOGS = ROOT / "logs"

FALLBACK = ("Sorry, I can't answer that right now. Please reach the Cutix team: "
            "+234 704 623 6426 (call or WhatsApp) or info@cutixplc.com.ng.")
MAIN_LINE = "+234 704 623 6426"
MAIN_EMAIL = "info@cutixplc.com.ng"

STAGES = ("answering", "qualifying", "confirming", "awaiting_name", "routed")
ACTIVE = ("qualifying", "confirming", "awaiting_name")


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


WHATSAPP_NUMBER = env("WHATSAPP_NUMBER", "2347046236426")


# ---------------------------------------------------------------------------
# CSV data
# ---------------------------------------------------------------------------
def _load_csv(name):
    path = DATA / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


BRANCHES = {(r.get("id") or "").strip(): r for r in _load_csv("branches.csv") if r.get("id")}
STATE_ROUTING = {(r.get("state") or "").strip(): r for r in _load_csv("state_routing.csv") if r.get("state")}

DEALERS = []
for _r in _load_csv("distributors.csv"):
    DEALERS.append({
        "state": (_r.get("state") or "").strip(),
        "place": (_r.get("place") or "").strip(),
        "dealer": (_r.get("dealer") or "").strip(),
        "address": (_r.get("address") or "").strip(),
        "phones": (_r.get("phones") or "").strip(),
    })

# place / alias / state-name index  (key -> row)
LOC_INDEX = {}
for _r in _load_csv("places.csv"):
    _place = (_r.get("place") or "").strip()
    if not _place:
        continue
    LOC_INDEX[_place.lower()] = _r
    for _a in (_r.get("aliases") or "").split(";"):
        _a = _a.strip().lower()
        if _a:
            LOC_INDEX[_a] = _r
for _s in STATE_ROUTING:
    LOC_INDEX.setdefault(_s.lower(), {"place": _s, "state": _s, "aliases": "", "branch_id": ""})

_LOC_KEYS = sorted(LOC_INDEX, key=len, reverse=True)
LOC_RE = (re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(k) for k in _LOC_KEYS) + r")(?![A-Za-z0-9])", re.I)
          if _LOC_KEYS else None)

# Short or word-like keys must follow a location preposition (or be the whole message)
_SAFE_SHORT = {"aba", "jos", "uyo", "fct", "edo", "oyo"}
RISKY_SHORT = {k for k in LOC_INDEX if len(k) <= 3 and k not in _SAFE_SHORT}

PREP_PRIORITY = {"in": 0, "at": 0, "to": 0, "near": 0, "around": 0, "within": 0, "inside": 0, "for": 1, "from": 2}
_PREP_TAIL = re.compile(r"\b(in|at|to|near|around|within|inside|for|from)\s+(?:the\s+)?(?:city\s+of\s+)?$", re.I)


# ---------------------------------------------------------------------------
# Knowledge (model context)
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

    if DEALERS:
        lines = ["state | place | dealer | address | phones"]
        for d in DEALERS:
            lines.append(" | ".join([d["state"], d["place"], d["dealer"], d["address"], d["phones"]]))
        parts.append("=== APPROVED DISTRIBUTORS AND DEALERS ===\n" + "\n".join(lines))

    for spec in sorted(DATA.glob("spec_*.csv")):
        with spec.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
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


SERVER_RULES = (
    "\n\nIMPORTANT RULES SET BY THE SERVER (they override anything above):\n"
    "- Quote requests and customer details are handled by the server, not by you. Never ask the customer "
    "for quantity, location or name, never mention a quote form, and never mention a WhatsApp button.\n"
    "- If the customer wants to order or get a quote, reply in one sentence: tap 'Request a quote' or tell me "
    "what you need and I'll take your details.\n"
    "- Answer only the product, dealer or company question asked. Keep replies short."
)


def build_system_prompt():
    tpl_path = DATA / "system_prompt.md"
    tpl = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else ""
    ctx = build_context()
    if "{{retrieved_chunks}}" in tpl:
        return tpl.replace("{{retrieved_chunks}}", ctx) + SERVER_RULES, ctx
    return tpl + "\n\n<context>\n" + ctx + "\n</context>" + SERVER_RULES, ctx


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
# Location resolution
# ---------------------------------------------------------------------------
def _location_info(row):
    state = (row.get("state") or "").strip()
    ro = STATE_ROUTING.get(state, {})
    nearest_id = (ro.get("nearest_branch_id") or "").strip()
    second_id = (ro.get("second_branch_id") or "").strip()
    bid = (row.get("branch_id") or "").strip() or nearest_id
    if bid not in BRANCHES:
        bid = nearest_id if nearest_id in BRANCHES else ""
    alt_id = second_id if bid == nearest_id else nearest_id
    if alt_id == bid or alt_id not in BRANCHES:
        alt_id = ""

    same_state = [d for d in DEALERS if d["state"].lower() == state.lower()]
    if not same_state:
        zone = ro.get("zone", "")
        zone_states = {s for s, r in STATE_ROUTING.items() if r.get("zone") == zone}
        same_state = [d for d in DEALERS if d["state"] in zone_states]

    return {
        "raw": (row.get("place") or state).strip(),
        "state": state,
        "zone": ro.get("zone", ""),
        "branch_id": bid,
        "branch_label": BRANCHES.get(bid, {}).get("branch", ""),
        "second_label": BRANCHES.get(alt_id, {}).get("branch", ""),
        "dealers": same_state[:3],
        "far": (ro.get("far") or "").strip() == "1",
    }


def resolve_location(text):
    """Whole-word match against places, aliases and state names (longest first)."""
    if not LOC_RE or not text:
        return None
    t = text.strip()
    t_low = t.lower().strip(" .!?,")
    best = None
    for m in LOC_RE.finditer(t):
        key = m.group(0).lower()
        pm = _PREP_TAIL.search(t[max(0, m.start() - 30):m.start()])
        prio = PREP_PRIORITY[pm.group(1).lower()] if pm else 3
        if key in RISKY_SHORT and prio == 3 and t_low != key:
            continue
        cand = (prio, m.start(), key)
        if best is None or cand < best:
            best = cand
    if not best:
        return None
    return _location_info(LOC_INDEX[best[2]])


def branch_contact(branch_id):
    b = BRANCHES.get(branch_id) or {}
    if (b.get("verified") or "").strip() == "1" and b.get("phones"):
        return b["phones"]
    return ""


def build_locator_reply(info):
    if not info:
        return ("I couldn't match that place to our list. Tell me which state it is in, or contact the Cutix "
                f"team on {MAIN_LINE} or {MAIN_EMAIL}.")
    lines = []
    branch = info["branch_label"]
    dealers = info.get("dealers") or []

    def dealer_lines():
        out = ["Approved dealers nearby:"]
        for d in dealers:
            bit = f"- {d['dealer']}"
            if d["place"]:
                bit += f" ({d['place']})"
            if d["phones"]:
                bit += f" - {d['phones']}"
            out.append(bit)
        return out

    if info.get("far") and dealers:
        lines += dealer_lines()
        lines.append("")
    if branch:
        line = f"Nearest Cutix branch for {info['raw']}: {branch}"
        if info.get("second_label"):
            line += f" (alternative: {info['second_label']})"
        phones = branch_contact(info["branch_id"])
        if phones:
            line += f" - {phones}"
        lines.append(line)
    if dealers and not (info.get("far") and dealers):
        lines += dealer_lines()
    if branch and not branch_contact(info["branch_id"]):
        lines.append(f"For branch contact details, call {MAIN_LINE} or email {MAIN_EMAIL}.")
    if not lines:
        lines.append(f"Please contact the Cutix team on {MAIN_LINE} or {MAIN_EMAIL}.")
    return "\n".join(lines).strip()


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
            f"Cutix team: {MAIN_LINE} or {MAIN_EMAIL}.")


PROVIDERS = {"gemini": call_gemini, "openai": call_openai_compat, "mock": call_mock}


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


def _save_usage(u):
    tmp = USAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(u), encoding="utf-8")
    os.replace(tmp, USAGE_FILE)


def check_and_reserve(session, ip_hash):
    per_session = env_int("MAX_MSGS_PER_SESSION", 20)
    per_ip = env_int("MAX_MSGS_PER_IP", 40)
    daily = env_int("DAILY_CAP", 300)
    with _use_lock:
        u = _load_usage()
        if u["total"] >= daily:
            return False, f"The demo has reached its daily limit. Please try again tomorrow or contact Cutix on {MAIN_LINE}.", 0
        s = u["sessions"].get(session, 0)
        i = u["ips"].get(ip_hash, 0)
        if s >= per_session or i >= per_ip:
            return False, f"You've reached the message limit for this demo. Contact the Cutix team on {MAIN_LINE}.", 0
        u["total"] += 1
        u["sessions"][session] = s + 1
        u["ips"][ip_hash] = i + 1
        _save_usage(u)
        return True, "", per_session - (s + 1)


def refund(session, ip_hash):
    with _use_lock:
        u = _load_usage()
        u["total"] = max(0, u.get("total", 0) - 1)
        if session in u["sessions"]:
            u["sessions"][session] = max(0, u["sessions"][session] - 1)
        if ip_hash in u["ips"]:
            u["ips"][ip_hash] = max(0, u["ips"][ip_hash] - 1)
        _save_usage(u)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = LOGS / "chats.db"
_db_lock = threading.Lock()

LEAD_KEYS = ("lead_name", "lead_size", "lead_type", "lead_need", "lead_quantity",
             "lead_location", "lead_state", "lead_branch", "lead_second")
COUNTER_KEYS = ("pending_qty", "pending_loc", "loc_asks", "name_asks", "name_declined", "location_confirmed")
CONV_COLS = set(LEAD_KEYS + COUNTER_KEYS + ("stage", "qualified", "routed_to", "whatsapp_tapped",
                                            "message_count", "last_active"))


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
                lead_size TEXT,
                lead_type TEXT,
                lead_need TEXT,
                lead_quantity TEXT,
                lead_location TEXT,
                lead_state TEXT,
                lead_branch TEXT,
                lead_second TEXT,
                name_declined INTEGER NOT NULL DEFAULT 0,
                location_confirmed INTEGER NOT NULL DEFAULT 1,
                pending_qty TEXT,
                pending_loc TEXT,
                loc_asks INTEGER NOT NULL DEFAULT 0,
                name_asks INTEGER NOT NULL DEFAULT 0,
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
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id);
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                name TEXT,
                size TEXT,
                type TEXT,
                need TEXT,
                quantity TEXT,
                location TEXT,
                state TEXT,
                branch TEXT,
                location_confirmed INTEGER NOT NULL DEFAULT 1,
                tapped INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_leads_conv ON leads(conversation_id);
        """)
        # migrate older databases
        for col, typ in [
            ("lead_size", "TEXT"), ("lead_type", "TEXT"), ("lead_second", "TEXT"),
            ("pending_qty", "TEXT"), ("pending_loc", "TEXT"),
            ("loc_asks", "INTEGER NOT NULL DEFAULT 0"),
            ("name_asks", "INTEGER NOT NULL DEFAULT 0"),
            ("name_declined", "INTEGER NOT NULL DEFAULT 0"),
            ("location_confirmed", "INTEGER NOT NULL DEFAULT 1"),
            ("whatsapp_tapped", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {typ}")
            except Exception:
                pass
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
                "lead_name": None, "lead_size": None, "lead_type": None,
                "lead_need": None, "lead_quantity": None, "lead_location": None,
                "lead_state": None, "lead_branch": None, "lead_second": None,
                "name_declined": 0, "location_confirmed": 1,
                "pending_qty": None, "pending_loc": None,
                "loc_asks": 0, "name_asks": 0,
                "routed_to": None, "message_count": 0, "whatsapp_tapped": 0
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
                "AND content != '[quote_request]' "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, limit)
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        finally:
            conn.close()


def save_lead(conversation_id, state):
    with _db_lock:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO leads (conversation_id, created_at, name, size, type, need, quantity, "
                "location, state, branch, location_confirmed, tapped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (conversation_id, _utcnow(),
                 state.get("lead_name"), state.get("lead_size"), state.get("lead_type"),
                 state.get("lead_need"), state.get("lead_quantity"),
                 state.get("lead_location"), state.get("lead_state"),
                 state.get("lead_branch"), state.get("location_confirmed", 1))
            )
            conn.commit()
        finally:
            conn.close()


def get_leads_since(conversation_id, since):
    with _db_lock:
        conn = get_db()
        try:
            return conn.execute(
                "SELECT * FROM leads WHERE conversation_id = ? AND created_at >= ? ORDER BY id",
                (conversation_id, since)
            ).fetchall()
        finally:
            conn.close()


def mark_leads_tapped(conversation_id, since):
    with _db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE leads SET tapped = 1 WHERE conversation_id = ? AND created_at >= ? AND tapped = 0",
                (conversation_id, since)
            )
            conn.commit()
        finally:
            conn.close()


def recently_asked(conversation_id, prompt, n=2):
    """True if this prompt already appears in one of the last n assistant messages."""
    if not prompt:
        return False
    with _db_lock:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT content FROM turns WHERE conversation_id = ? AND role = 'assistant' "
                "ORDER BY id DESC LIMIT ?", (conversation_id, n)).fetchall()
        finally:
            conn.close()
    return any(prompt.strip() in r["content"] for r in rows)


def purge_old():
    days = env_int("LOG_RETENTION_DAYS", 90)
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    with _db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM turns WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM conversations WHERE last_active < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()


def _purge_loop():
    while True:
        time.sleep(86400)
        try:
            purge_old()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Lead extraction helpers
# ---------------------------------------------------------------------------
# Strong buying intent. The only kind allowed to start a NEW lead after a hand-off.
STRONG_START_RE = re.compile(
    r"\b(?:(?:i|we)\s+(?:also\s+)?(?:need|want)"
    r"|(?:(?:i|we)(?:'m|\s+am|'re|\s+are)?\s+)?(?:also\s+)?looking\s+for"
    r"|(?:i|we)\s+(?:also\s+)?would\s+like|(?:i|we)'d\s+(?:also\s+)?like"
    r"|another\s+quot(?:e|ation)|new\s+(?:enquiry|inquiry)"
    r"|(?:get|need|request|want|send)\s+(?:me\s+)?(?:a\s+)?quot(?:e|ation))\b", re.I)
# Broader signals, used only BEFORE the first lead exists.
BROAD_START_RE = re.compile(
    r"\b(?:place\s+(?:an\s+)?order|(?:want|like|wish)\s+to\s+(?:buy|order|purchase)"
    r"|how\s+(?:do|can)\s+i\s+(?:order|buy|get|purchase)|a\s+quot(?:e|ation))\b", re.I)
START_RE = re.compile(STRONG_START_RE.pattern + "|" + BROAD_START_RE.pattern, re.I)
# Phrases that look like intent but are really questions or requests for help.
NOT_START_RE = re.compile(
    r"\b(?:(?:i|we)\s+(?:also\s+)?(?:need|want)\s+(?:to\s+(?:know|understand|ask|find\s+out|check|confirm|"
    r"learn|see|talk|speak|call)|help|assistance|advice|info|information|clarification|"
    r"an?\s+(?:call|human|person|agent|explanation|answer)|your\s+(?:address|number|contact|help))"
    r"|(?:i|we)\s+would\s+like\s+to\s+(?:know|ask|understand|find\s+out)"
    r"|(?:i|we)'d\s+like\s+to\s+(?:know|ask|understand|find\s+out))\b", re.I)

LOCATOR_RE = re.compile(
    r"\b(where\s+(can|do|to)\s+i\s+(buy|find|get)|nearest\s+(dealer|branch|distributor|shop)|"
    r"closest\s+(shop|dealer|branch)|dealers?\s+in|branches?\s+in|where\s+to\s+buy|"
    r"do\s+you\s+have\s+(a\s+)?(dealer|branch|distributor))\b",
    re.I
)

SIZE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*mm2?\b", re.I)
TYPE_RE = re.compile(r"\b(xlpe|pvc|swa|armoured|non-?armoured|single\s+core|multicore|hffr|flexible|house\s+wiring)\b", re.I)

QUANTITY_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet|foot|yd|yards?|km|kilometers?|kilometres?|"
    r"rolls?|coils?|drums?|pieces?|pcs?|lengths?)\b",
    re.I
)

NAME_RE = re.compile(
    r"(?:my\s+name\s+is|i\s+am|i'?m|this\s+is|name['\s:]+)\s*([A-Za-z][A-Za-z\s'-]{1,40})",
    re.I
)

QUESTION_START_RE = re.compile(r"^(?:what|which|how|why|when|where|who|is|are|can|do|does|did|will|would|should)\b", re.I)

SIZE_UNSURE = "size not specified"


def convert_to_metres(num_str, unit):
    try:
        n = float(num_str.replace(",", ""))
    except ValueError:
        return None, f"{num_str} {unit}"
    u = unit.lower()
    if u in ("m", "meter", "meters", "metre", "metres"):
        return n, f"{num_str} m"
    if u in ("ft", "feet", "foot"):
        return n * 0.3048, f"{num_str} ft (≈ {n * 0.3048:.0f} m)"
    if u in ("yd", "yard", "yards"):
        return n * 0.9144, f"{num_str} yd (≈ {n * 0.9144:.0f} m)"
    if u in ("km", "kilometer", "kilometers", "kilometre", "kilometres"):
        return n * 1000, f"{num_str} km (≈ {n * 1000:.0f} m)"
    return None, f"{num_str} {unit}"


def extract_quantity(text):
    cleaned = SIZE_RE.sub(" ", text)
    matches = QUANTITY_RE.findall(cleaned)
    for num, unit in matches:
        digits = re.sub(r"\D", "", num)
        if len(digits) >= 10:
            continue
        _, display = convert_to_metres(num, unit)
        return display
    return None


def extract_size(text):
    m = SIZE_RE.search(text)
    return m.group(0) if m else None


def extract_type(text):
    m = TYPE_RE.search(text)
    return m.group(0) if m else None


def name_from_reply(text):
    m = NAME_RE.search(text)
    if m:
        name = m.group(1).strip()[:60]
        if (1 <= len(name.split()) <= 4 and
            not re.search(r"\d", name) and
            not any(w in name.lower() for w in ("need", "want", "looking", "order", "quote", "house", "shop"))):
            return name
    cleaned = text.strip()
    if cleaned.lower() in ("no", "skip", "later", "none", "n/a", "prefer not"):
        return "__declined__"
    if (1 <= len(cleaned.split()) <= 4 and not re.search(r"\d|\?", cleaned)):
        return cleaned[:60]
    return None


def is_question(text):
    return bool(QUESTION_START_RE.match(text.strip()) or text.strip().endswith("?"))


def is_lead_start(text, stage):
    """Broad signals before the first lead; only strong intent after a hand-off."""
    if NOT_START_RE.search(text):
        return False
    return bool((STRONG_START_RE if stage == "routed" else START_RE).search(text))


def weak_buying_words(text):
    return bool(re.search(r"\b(order|buy|purchase|supply|quote|price)\b", text, re.I))


def apply_extraction(f, text, allow_unknown=True):
    caps = []
    size = extract_size(text)
    if size:
        f["lead_size"] = size
        caps.append(size)
    typ = extract_type(text)
    if typ:
        f["lead_type"] = typ
        caps.append(typ)
    qty = extract_quantity(text)
    if qty:
        f["lead_quantity"] = qty
        caps.append(qty)
    loc = resolve_location(text)
    if loc:
        f["lead_location"] = loc["raw"]
        f["lead_state"] = loc["state"]
        f["lead_branch"] = loc["branch_label"]
        f["lead_second"] = loc.get("second_label")
        f["location_confirmed"] = 1
        caps.append(loc["raw"])
    elif allow_unknown and re.search(r"\b(?:in|at|from|around|near)\s+[A-Za-z]", text, re.I):
        m = re.search(r"\b(?:in|at|from|around|near)\s+([A-Za-z][A-Za-z\s\-]{2,40})", text, re.I)
        if m:
            f["lead_location"] = m.group(1).strip()
            f["location_confirmed"] = 0
            caps.append(f["lead_location"] + " (to confirm)")
    return caps


def prompt_for(conv):
    if not (conv.get("lead_size") or conv.get("lead_need")):
        return "What size and type of cable do you need?"
    if not conv.get("lead_quantity"):
        return "What length or quantity do you need?"
    if not conv.get("lead_location"):
        return "Which town or state is the project in?"
    if not conv.get("lead_name") and not conv.get("name_declined"):
        return "And your name, please?"
    return None


def build_confirmation(f):
    bits = []
    if f.get("lead_size"):
        bits.append(f["lead_size"])
    if f.get("lead_type"):
        bits.append(f["lead_type"])
    if f.get("lead_need") and not f.get("lead_size"):
        bits.append(f["lead_need"])
    if f.get("lead_quantity"):
        bits.append(f["lead_quantity"])
    if f.get("lead_location"):
        bits.append("in " + f["lead_location"])
    if not bits:
        return None
    msg = "Just to confirm: " + ", ".join(bits) + ". Is that right?"
    if not f.get("lead_name") and not f.get("name_declined"):
        msg += " And your name?"
    return msg


def build_handoff_message(f):
    name = f.get("lead_name") or "there"
    return (f"Thanks, {name}. Your details are ready. "
            "Tap **Continue on WhatsApp** below and the team will have everything.")


def name_needed(f):
    return not f.get("lead_name") and not f.get("name_declined")


def _snapshot(f):
    return tuple(f.get(k) for k in LEAD_KEYS)


def lead_context(state):
    return {k: state.get(k) for k in LEAD_KEYS if state.get(k)}


def lead_turn(conv, text, quote_request=False):
    """Server-controlled lead state machine. Returns (new_state, event)."""
    stage = conv.get("stage") or "answering"
    f = {k: conv.get(k) for k in list(LEAD_KEYS) + list(COUNTER_KEYS)}
    f["stage"] = stage
    event = None
    caps = []

    if stage == "answering" or stage == "routed":
        if not (quote_request or is_lead_start(text, stage)):
            return None, None
        for k in LEAD_KEYS:
            f[k] = None
        f["name_declined"] = 0
        f["location_confirmed"] = 1
        f["loc_asks"] = 0
        f["name_asks"] = 0
        stage = "qualifying"
        event = "reply"
        if quote_request:
            f["_prompt"] = "What size and type of cable are you looking for?"
        else:
            caps = apply_extraction(f, text, allow_unknown=True)
            if caps:
                f["_ack"] = "Noted: " + ", ".join(caps) + "."
            f["_prompt"] = prompt_for(f)

    elif stage == "qualifying":
        event = "reply"
        product_question = bool(QUESTION_START_RE.match(text) and not START_RE.search(text))
        caps = [] if product_question else apply_extraction(f, text, allow_unknown=True)
        if not product_question and (f.get("lead_size") or f.get("lead_need")) and f.get("lead_quantity") and f.get("lead_location"):
            stage = "confirming"
            f["_prompt"] = build_confirmation(f)
        else:
            f["_prompt"] = prompt_for(f)
        if caps:
            f["_ack"] = "Noted: " + ", ".join(caps) + "."

    elif stage == "confirming":
        event = "reply"
        product_question = bool(QUESTION_START_RE.match(text) and not START_RE.search(text))
        yes = bool(re.search(r"\b(yes|yeah|yep|correct|right|ok|okay|sure|fine)\b", text, re.I))
        no = bool(re.search(r"\b(no|nope|wrong|incorrect|change|actually)\b", text, re.I))
        got_name = False
        caps = [] if product_question else apply_extraction(f, text, allow_unknown=False)
        changed = bool(caps)
        if not product_question:
            nm = name_from_reply(text)
            if nm == "__declined__":
                f["name_declined"], got_name = 1, True
            elif nm:
                f["lead_name"], got_name = nm, True
        if changed:
            pass
        elif yes or got_name:
            if name_needed(f):
                stage = "awaiting_name"
            else:
                event = "handoff"
        elif no:
            f["_prompt"] = "No problem. What should I change: the size, the length or the location?"

    elif stage == "awaiting_name":
        before = _snapshot(f)
        product_question = bool(QUESTION_START_RE.match(text) and not START_RE.search(text))
        caps = [] if product_question else apply_extraction(f, text, allow_unknown=False)
        if _snapshot(f) != before:
            stage = "confirming"
        elif f.get("lead_name"):
            event = "handoff"
        else:
            nm = name_from_reply(text)
            if nm == "__declined__":
                f["name_declined"], event = 1, "handoff"
            elif nm:
                f["lead_name"], event = nm, "handoff"
            elif not is_question(text):      # a side question is not a failed name attempt
                f["name_asks"] = int(f.get("name_asks") or 0) + 1
                if f["name_asks"] >= 3:
                    f["name_declined"], event = 1, "handoff"

    if event == "handoff":
        stage = "routed"
        f["qualified"] = 1
        f["routed_to"] = f.get("lead_branch") or "head office"
    f["stage"] = stage
    if caps and stage != "confirming" and event != "handoff":
        f["_ack"] = "Noted: " + ", ".join(caps) + "."
    return f, event


# ---------------------------------------------------------------------------
# WhatsApp hand-off
# ---------------------------------------------------------------------------
def build_whatsapp_summary(conversation_id, since):
    rows = get_leads_since(conversation_id, since)
    unsent = [r for r in rows if not r["tapped"]]
    if not unsent:
        return None
    sent_before = any(r["tapped"] for r in rows)
    name = next((r["name"] for r in reversed(unsent) if r["name"]), None)
    lines = []
    if sent_before:
        lines.append("Additional to my earlier message:")
    lines.append(f"Name: {name}" if name else "Name: (not given)")

    def item_text(r):
        parts = [x for x in (r["size"] if r["size"] != SIZE_UNSURE else None, r["type"]) if x]
        text = " ".join(parts)
        if r["size"] == SIZE_UNSURE:
            text = (text or r["need"] or "cable") + f" ({SIZE_UNSURE})"
        elif not text:
            text = r["need"] or "cable"
        elif r["need"]:
            text += f" (use: {r['need']})"
        route = r["branch"] if (r["branch"] and r["location_confirmed"]) else "location to be confirmed by team"
        return text, r["quantity"] or "-", r["location"] or "-", route

    if len(unsent) == 1:
        need, qty, loc, route = item_text(unsent[0])
        lines += [f"Need: {need}", f"Qty: {qty}", f"Location: {loc}", f"Route to: {route}"]
    else:
        for i, r in enumerate(unsent, 1):
            need, qty, loc, route = item_text(r)
            lines.append(f"Item {i}: {need} | {qty} | {loc} | Route to: {route}")
    return "\n".join(lines)


def build_whatsapp_url(summary):
    base = f"https://wa.me/{WHATSAPP_NUMBER}"
    if summary:
        return base + "?text=" + quote("Hello Cutix, I was speaking with the website assistant.\n\n" + summary)
    return base + "?text=" + quote("Hello Cutix")


def strip_handoff_language(text):
    patterns = [
        r"(?i)tap\s+(the\s+)?(continue\s+on\s+)?whatsapp.*",
        r"(?i)click\s+(the\s+)?whatsapp.*",
        r"(?i)continue\s+on\s+whatsapp.*",
        r"(?i)i.?ll\s+pass\s+this.*",
    ]
    for p in patterns:
        text = re.sub(p, "", text).strip()
    return text


# ---------------------------------------------------------------------------
# Model answer
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
        shown = {k: v for k, v in lead_state.items() if v}
        if shown:
            system += ("\n\nCustomer's enquiry so far (context only; do not ask for missing details and do not "
                       "mention WhatsApp):\n" + json.dumps(shown))

    if paced and provider != "mock" and not throttle():
        return "I'm getting a lot of questions right now. Please try again in a few seconds.", "throttled"
    try:
        return fn(system, msgs), None
    except LLMError as e:
        return FALLBACK, str(e)
    except Exception as e:
        return FALLBACK, f"{type(e).__name__}: {e}"


def guard_invented_contacts(text):
    for p in PHONE_RE.findall(text):
        if norm_phone(p) not in ALLOWED_NUMBERS:
            return FALLBACK, True
    for e in EMAIL_RE.findall(text):
        if e.lower() not in ALLOWED_EMAILS:
            return FALLBACK, True
    return text, False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
SESSION_RE = re.compile(r"^[A-Za-z0-9_\-]{6,80}$")
STATIC_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
                ".webp": "image/webp", ".ico": "image/x-icon", ".css": "text/css", ".js": "application/javascript"}
MAX_BODY = 20000


def clean_text(s):
    s = (s or "") if isinstance(s, str) else ""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s).strip()[:600]


class Handler(BaseHTTPRequestHandler):
    server_version = "CutixDemo/0.9"

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

    def _client_ip(self):
        trust = env("TRUST_PROXY", "1" if env("RENDER") else "0") == "1"
        if trust:
            parts = [p.strip() for p in self.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            hops = max(1, env_int("PROXY_HOPS", 1))
            if parts:
                return parts[-hops] if len(parts) >= hops else parts[0]
        return self.client_address[0]

    def _ip_hash(self):
        return hashlib.sha256(self._client_ip().encode()).hexdigest()[:16]

    def do_GET(self):
        try:
            self._get()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._send(500, {"error": "server error"})
            except Exception:
                pass

    def do_POST(self):
        try:
            self._post()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._send(500, {"error": "server error"})
            except Exception:
                pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._send(413, {"error": "message too large"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, {"error": "bad json"})
            return None

    def _unsent_url(self, session, conv):
        summary = build_whatsapp_summary(session, conv["started_at"])
        return build_whatsapp_url(summary) if summary else None

    def _get(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
            f = (STATIC / rel).resolve()
            if STATIC.resolve() in f.parents and f.is_file() and f.suffix.lower() in STATIC_TYPES:
                self._send(200, f.read_bytes(), STATIC_TYPES[f.suffix.lower()])
                return
            self._send(404, "Not found")
            return

        if path == "/config.json":
            self._send(200, {
                "whatsapp": f"https://wa.me/{WHATSAPP_NUMBER}",
                "contactEmail": env("CONTACT_EMAIL", ""),
                "poweredBy": env("POWERED_BY", ""),
                "accessCodeRequired": bool(env("ACCESS_CODE")),
            })
            return

        if path == "/stats":
            token = env("ADMIN_TOKEN")
            auth = self.headers.get("Authorization", "")
            if not token or not hmac.compare_digest(auth.encode("utf-8"), f"Bearer {token}".encode("utf-8")):
                self._send(401, {"error": "Unauthorized"})
                return
            with _db_lock:
                conn = get_db()
                try:
                    convs = conn.execute(
                        "SELECT id, stage, qualified, message_count, started_at FROM conversations "
                        "ORDER BY last_active DESC LIMIT 100").fetchall()
                    leads = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 100").fetchall()
                    total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                    by_branch = conn.execute(
                        "SELECT COALESCE(branch, 'unconfirmed') AS b, COUNT(*) AS n FROM leads GROUP BY b").fetchall()
                finally:
                    conn.close()

            def mask(name):
                name = name or ""
                parts = name.split()
                return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name

            self._send(200, {
                "total_leads": total_leads,
                "leads_by_branch": {r["b"]: r["n"] for r in by_branch},
                "leads": [{"id": r["id"], "name": mask(r["name"]), "size": r["size"], "type": r["type"],
                           "qty": r["quantity"], "location": r["location"], "branch": r["branch"],
                           "tapped": bool(r["tapped"]), "created": r["created_at"]} for r in leads],
                "conversations": [{"id": r["id"][:8], "stage": r["stage"], "qualified": bool(r["qualified"]),
                                   "msgs": r["message_count"], "started": r["started_at"]} for r in convs],
            })
            return

        if path == "/session":
            session = self.headers.get("X-Session-Id") or ""
            if not SESSION_RE.match(session):
                self._send(400, {"error": "missing session"})
                return
            conv = get_or_create_conversation(session)
            url = self._unsent_url(session, conv)
            self._send(200, {"stage": conv["stage"], "turns": get_recent_turns(session, limit=20),
                             "qualified": bool(url), "whatsapp_url": url})
            return

        self._send(404, "Not found")

    def _post(self):
        path = self.path.split("?")[0]

        if path == "/whatsapp-tapped":
            body = self._read_json()
            if body is None:
                return
            session = (body.get("session") or "").strip()
            if SESSION_RE.match(session):
                conv = get_or_create_conversation(session)
                mark_leads_tapped(session, conv["started_at"])
            self._send(200, {"ok": True})
            return

        if path != "/chat":
            self._send(404, "Not found")
            return

        body = self._read_json()
        if body is None:
            return
        self._chat(body)

    def _reply(self, session, conv, reply, *, qualified=False, url=None, remaining=None, stage=None,
               user_text=None, ms=None, error=None):
        if user_text is not None:
            log_turn(session, "user", user_text)
        log_turn(session, "assistant", reply, ms=ms, provider=env("PROVIDER"), error=error)
        self._send(200, {"reply": reply, "qualified": qualified, "whatsapp_url": url if qualified else None,
                         "remaining": remaining, "stage": stage or conv.get("stage")})

    def _model_answer(self, session, ip_hash, message, lead_state=None):
        ok, err_msg, remaining = check_and_reserve(session, ip_hash)
        if not ok:
            return None, None, 0, err_msg
        history = get_recent_turns(session, limit=env_int("HISTORY_WINDOW", 6))
        t0 = time.time()
        text, error = answer(message, history=history, lead_state=lead_state)
        ms = int((time.time() - t0) * 1000)
        if error:
            refund(session, ip_hash)
            return text, error, remaining, None
        text, replaced = guard_invented_contacts(text)
        text = strip_handoff_language(text)
        return text, ("invented_contact" if replaced else None), (remaining, ms), None

    def _chat(self, body):
        session = (body.get("session") or "").strip()
        if not SESSION_RE.match(session):
            self._send(400, {"error": "missing or invalid session"})
            return
        message = clean_text(body.get("message"))
        quote_request = bool(body.get("quote_request"))
        access = env("ACCESS_CODE")
        if access and (body.get("code") or "").strip() != access:
            self._send(401, {"error": "invalid code"})
            return
        if not message and not quote_request:
            self._send(400, {"error": "empty message"})
            return

        ip_hash = self._ip_hash()
        conv = get_or_create_conversation(session)
        if (conv.get("message_count") or 0) >= env_int("MAX_TURNS_PER_SESSION", 200):
            self._send(429, {"error": "Too many messages in this chat. Please start a new chat."})
            return
        stage = conv.get("stage") or "answering"
        if stage == "intent_detected":
            stage = "qualifying"
        if stage == "lead_captured":
            stage = "routed"
        conv["stage"] = stage

        # 1. Request a quote button
        if quote_request:
            if stage in ACTIVE:
                reply = "Let's carry on with your quote.\n\n" + (prompt_for(conv) or "")
                self._reply(session, conv, reply.strip(), user_text="[quote_request]")
                return
            state, _ = lead_turn(conv, "", quote_request=True)
            update_conversation(session, **{k: v for k, v in state.items() if not k.startswith("_")})
            reply = ("Tell me what you need and I'll start your quote. What size and type of cable are you "
                     "looking for? If you're not sure, tell me what it's for.")
            self._reply(session, conv, reply, user_text="[quote_request]", stage="qualifying")
            return

        # 2. Pure "where can I buy" question
        if LOCATOR_RE.search(message) and not is_lead_start(message, stage):
            reply = build_locator_reply(resolve_location(message))
            if stage in ACTIVE:
                p = prompt_for(conv)
                if p and not recently_asked(session, p):
                    reply += "\n\n" + p
            self._reply(session, conv, reply.strip(), user_text=message)
            return

        # 3. Lead engine
        state, event = lead_turn(conv, message)
        if event:
            ack = state.pop("_ack", None)
            override = state.pop("_prompt", None)
            new_stage = state["stage"]
            update_conversation(session, **{k: v for k, v in state.items() if not k.startswith("_")})

            if event == "handoff":
                save_lead(session, state)
                reply = build_handoff_message(state)
                url = self._unsent_url(session, conv)
                self._reply(session, conv, reply, qualified=bool(url), url=url, stage=new_stage, user_text=message)
                return

            model_text, remaining = None, None
            if is_question(message):
                text, error, extra, blocked = self._model_answer(session, ip_hash, message, lead_state=lead_context(state))
                if not error and not blocked and text:
                    model_text = text
                    remaining = extra[0] if isinstance(extra, tuple) else None
            prompt = override or prompt_for(state)
            progress = bool(ack) or new_stage != stage or bool(override)
            if is_question(message) and not progress and recently_asked(session, prompt):
                prompt = None
            reply = "\n\n".join(x for x in (ack, model_text, prompt) if x)
            if not reply:
                reply = override or prompt_for(state) or ""
            self._reply(session, conv, reply, remaining=remaining, stage=new_stage, user_text=message)
            return

        # 4. Normal chat
        lead_ctx = lead_context(conv) if stage == "routed" else None
        text, error, extra, blocked = self._model_answer(session, ip_hash, message, lead_state=lead_ctx)
        if blocked:
            self._send(429, {"error": blocked})
            return
        if error and error != "invented_contact":
            log_turn(session, "user", message)
            log_turn(session, "assistant", text, provider=env("PROVIDER"), error=error)
            self._send(200, {"reply": text, "qualified": False, "whatsapp_url": None, "remaining": None})
            return
        remaining, ms = extra
        if stage == "routed" and weak_buying_words(message) and "Request a quote" not in text:
            text += "\n\nIf you'd like a new quote, tap Request a quote."
        url = self._unsent_url(session, conv) if stage == "routed" else None
        self._reply(session, conv, text, qualified=bool(url), url=url, remaining=remaining, ms=ms,
                    error=error, user_text=message)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Session-Id")
        self.end_headers()


def main():
    init_db()
    try:
        purge_old()
    except Exception:
        pass
    threading.Thread(target=_purge_loop, daemon=True).start()
    host = env("HOST", "0.0.0.0")
    port = env_int("PORT", 8000)
    print(f"Cutix chatbot listening on http://{host}:{port}")
    print(f"Loaded {len(BRANCHES)} branches, {len(STATE_ROUTING)} states, {len(LOC_INDEX)} place keys, "
          f"{len(DEALERS)} dealers")
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
