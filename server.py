#!/usr/bin/env python3
"""
Cutix demo chatbot – lead-generation version with geo-markets & unit conversion
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
# Location / Geo-market
# ---------------------------------------------------------------------------
TOWN_TO_STATE = {
    # South-West
    "lagos": "Lagos", "ikeja": "Lagos", "surulere": "Lagos", "lekki": "Lagos",
    "yaba": "Lagos", "ajah": "Lagos", "apapa": "Lagos", "badagry": "Lagos",
    "ibadan": "Oyo", "abeokuta": "Ogun", "sango": "Ogun", "otof": "Ogun",
    "akure": "Ondo", "ado ekiti": "Ekiti", "osogbo": "Osun", "ile ife": "Osun",
    # South-East
    "onitsha": "Anambra", "awka": "Anambra", "nnewi": "Anambra",
    "enugu": "Enugu", "nsukka": "Enugu", "aba": "Abia", "umuahia": "Abia",
    "owerri": "Imo", "orlu": "Imo", "abakaliki": "Ebonyi",
    # South-South
    "port harcourt": "Rivers", "ph": "Rivers", "warri": "Delta", "asaba": "Delta",
    "benin": "Edo", "benin city": "Edo", "calabar": "Cross River", "uyo": "Akwa Ibom",
    "yenagoa": "Bayelsa",
    # North-Central
    "abuja": "FCT", "fct": "FCT", "garki": "FCT", "wuse": "FCT", "maitama": "FCT",
    "jos": "Plateau", "minna": "Niger", "ilorin": "Kwara", "lafia": "Nasarawa",
    "lokoja": "Kogi", "makurdi": "Benue",
    # North-West
    "kano": "Kano", "kaduna": "Kaduna", "zaria": "Kaduna", "katsina": "Katsina",
    "sokoto": "Sokoto", "birnin kebbi": "Kebbi", "gusau": "Zamfara", "dutse": "Jigawa",
    # North-East
    "maiduguri": "Borno", "yola": "Adamawa", "jalingo": "Taraba", "bauchi": "Bauchi",
    "gombe": "Gombe", "damaturu": "Yobe",
}

MARKET_GROUPS = {
    "South-West": ["Lagos", "Ogun", "Oyo", "Osun", "Ondo", "Ekiti"],
    "South-East": ["Anambra", "Enugu", "Imo", "Abia", "Ebonyi"],
    "South-South": ["Rivers", "Delta", "Bayelsa", "Edo", "Cross River", "Akwa Ibom"],
    "North-Central": ["FCT", "Niger", "Nasarawa", "Kogi", "Kwara", "Plateau", "Benue"],
    "North-West": ["Kano", "Kaduna", "Katsina", "Jigawa", "Kebbi", "Sokoto", "Zamfara"],
    "North-East": ["Borno", "Yobe", "Adamawa", "Taraba", "Gombe", "Bauchi"],
}

# Load dealers once
DEALERS = []
_dist_path = DATA / "distributors.csv"
if _dist_path.exists():
    for row in csv.DictReader(_dist_path.open(encoding="utf-8")):
        DEALERS.append({
            "state": (row.get("state") or "").strip(),
            "place": (row.get("place") or "").strip(),
            "dealer": (row.get("dealer") or "").strip(),
            "address": (row.get("address") or "").strip(),
            "phones": (row.get("phones") or "").strip(),
        })


def resolve_location(text):
    """Return structured location info with nearest dealers and real phones."""
    text_l = text.lower()
    found_state = None
    found_raw = None

    # whole-word town match
    for town, state in TOWN_TO_STATE.items():
        if re.search(rf"\b{re.escape(town)}\b", text_l):
            found_state = state
            found_raw = town.title() if town != "ph" else "Port Harcourt"
            break

    # direct state name
    if not found_state:
        for group, states in MARKET_GROUPS.items():
            for st in states:
                if re.search(rf"\b{re.escape(st.lower())}\b", text_l):
                    found_state = st
                    found_raw = st
                    break
            if found_state:
                break

    if not found_state:
        return None

    # find market group
    market = None
    for g, states in MARKET_GROUPS.items():
        if found_state in states:
            market = g
            break

    # dealers in same state first, then same market
    same_state = [d for d in DEALERS if d["state"].lower() == found_state.lower()]
    if not same_state and market:
        group_states = [s.lower() for s in MARKET_GROUPS.get(market, [])]
        same_state = [d for d in DEALERS if d["state"].lower() in group_states]

    phones = []
    for d in same_state[:3]:
        for p in re.findall(r"[\d\-+() ]{10,}", d["phones"]):
            np = norm_phone(p)
            if np and np not in phones and len(np) >= 10:
                phones.append(np)
                if len(phones) >= 3:
                    break
        if len(phones) >= 3:
            break

    return {
        "raw": found_raw or found_state,
        "state": found_state,
        "market": market,
        "dealers": same_state[:3],
        "phones": phones,
    }


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
                name_declined INTEGER NOT NULL DEFAULT 0,
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
        # migration for older DBs
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN name_declined INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN whatsapp_tapped INTEGER NOT NULL DEFAULT 0")
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
                "lead_name": None, "lead_need": None, "lead_quantity": None,
                "lead_location": None, "name_declined": 0,
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


# ---------------------------------------------------------------------------
# Lead extraction
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

SIZE_RE = re.compile(r"\b\d+(\.\d+)?\s*mm2?\b", re.I)

# quantity with unit (including imperial)
QUANTITY_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet|foot|yd|yards?|km|kilometers?|kilometres?|"
    r"rolls?|coils?|drums?|pieces?|pcs?|lengths?)\b",
    re.I
)

NAME_RE = re.compile(
    r"(?:my\s+name\s+is|i\s+am|i'?m|this\s+is|name['\s:]+)\s*([A-Za-z][A-Za-z\s'-]{1,40})",
    re.I
)


def convert_to_metres(num_str, unit):
    try:
        n = float(num_str.replace(",", ""))
    except ValueError:
        return None
    u = unit.lower()
    if u in ("m", "meter", "meters", "metre", "metres"):
        return n, f"{num_str} m"
    if u in ("ft", "feet", "foot"):
        return n * 0.3048, f"{num_str} ft (≈ {n * 0.3048:.0f} m)"
    if u in ("yd", "yard", "yards"):
        return n * 0.9144, f"{num_str} yd (≈ {n * 0.9144:.0f} m)"
    if u in ("km", "kilometer", "kilometers", "kilometre", "kilometres"):
        return n * 1000, f"{num_str} km (≈ {n * 1000:.0f} m)"
    # rolls / coils / drums – keep as-is, no conversion
    return None, f"{num_str} {unit}"


def extract_quantity(text):
    cleaned = SIZE_RE.sub(" ", text)
    matches = QUANTITY_RE.findall(cleaned)
    for num, unit in matches:
        digits = re.sub(r"\D", "", num)
        if len(digits) >= 10:  # phone-like
            continue
        metres, display = convert_to_metres(num, unit)
        return display
    return None


def extract_name(text, awaiting=False):
    m = NAME_RE.search(text)
    if m:
        name = m.group(1).strip()[:60]
        if (1 <= len(name.split()) <= 4 and
            not re.search(r"\d", name) and
            not any(w in name.lower() for w in ("need", "want", "looking", "order", "quote", "house", "shop"))):
            return name
    if awaiting:
        cleaned = text.strip()
        if cleaned.lower() in ("no", "skip", "later", "none", "n/a", "prefer not"):
            return "__declined__"
        if (1 <= len(cleaned.split()) <= 4 and
            not re.search(r"\d|\?", cleaned)):
            return cleaned[:60]
    return None


def extract_need(text, qualifying=False):
    # while already qualifying, accept size/cable type without intent words
    if qualifying:
        if SIZE_RE.search(text) or re.search(r"\b(xlpe|pvc|swa|armoured|single\s+core|multicore|hffr)\b", text, re.I):
            return text.strip()[:120]
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


def extract_lead_fields(text, current, awaiting_name=False, qualifying=False):
    updated = dict(current)

    name = extract_name(text, awaiting=awaiting_name)
    if name == "__declined__":
        updated["name_declined"] = 1
        updated["lead_name"] = None
    elif name:
        updated["lead_name"] = name
        updated["name_declined"] = 0

    qty = extract_quantity(text)
    if qty:
        updated["lead_quantity"] = qty

    loc_info = resolve_location(text)
    if loc_info:
        updated["lead_location"] = loc_info["raw"]
        updated["_loc_info"] = loc_info  # temporary for summary

    need = extract_need(text, qualifying=qualifying)
    if need:
        updated["lead_need"] = need

    return updated


def next_missing_question(fields):
    """Server-controlled question for the next missing field."""
    if not fields.get("lead_need"):
        return "What size and type of cable do you need?"
    if not fields.get("lead_quantity"):
        return "What length or quantity do you need?"
    if not fields.get("lead_location"):
        return "Which town or state is the project in?"
    if not fields.get("lead_name") and not fields.get("name_declined"):
        return "And your name, please?"
    return None


def advance_stage(conv, user_text, bot_reply, quote_request=False):
    stage = conv.get("stage") or "answering"
    was_routed = stage == "routed"

    fields = {
        "lead_name": conv.get("lead_name"),
        "lead_need": conv.get("lead_need"),
        "lead_quantity": conv.get("lead_quantity"),
        "lead_location": conv.get("lead_location"),
        "name_declined": conv.get("name_declined") or 0,
    }

    awaiting = stage == "awaiting_name"
    qualifying = stage in ("qualifying", "awaiting_name", "intent_detected")
    fields = extract_lead_fields(user_text, fields, awaiting_name=awaiting, qualifying=qualifying)

    is_locator = bool(LOCATOR_RE.search(user_text))
    has_intent = bool(INTENT_RE.search(user_text)) and not is_locator
    if quote_request:
        has_intent = True

    if stage == "answering" and (has_intent or quote_request):
        stage = "intent_detected"

    if stage in ("intent_detected", "qualifying", "awaiting_name"):
        stage = "qualifying"

    has_need = bool(fields.get("lead_need"))
    has_qty = bool(fields.get("lead_quantity"))
    has_loc = bool(fields.get("lead_location"))
    name_done = bool(fields.get("lead_name")) or bool(fields.get("name_declined"))

    if stage == "qualifying" and has_need and has_qty and has_loc:
        if not name_done:
            stage = "awaiting_name"
        else:
            stage = "lead_captured"

    if stage == "awaiting_name" and name_done:
        stage = "lead_captured"

    if stage == "lead_captured":
        stage = "routed"

    # Only start a new lead if we were already routed at the beginning of the turn
    if was_routed and has_intent and not quote_request:
        fields = {
            "lead_name": fields.get("lead_name"),
            "lead_need": fields.get("lead_need"),
            "lead_quantity": None,
            "lead_location": None,
            "name_declined": 0,
        }
        stage = "qualifying"

    routed_to = conv.get("routed_to")
    if stage in ("lead_captured", "routed") and not routed_to:
        routed_to = "whatsapp"

    qualified = 1 if stage in ("lead_captured", "routed") else 0

    result = {
        "stage": stage,
        "qualified": qualified,
        "lead_name": fields.get("lead_name"),
        "lead_need": fields.get("lead_need"),
        "lead_quantity": fields.get("lead_quantity"),
        "lead_location": fields.get("lead_location"),
        "name_declined": fields.get("name_declined") or 0,
        "routed_to": routed_to,
    }
    if "_loc_info" in fields:
        result["_loc_info"] = fields["_loc_info"]
    return result, qualified and not conv.get("qualified")


def build_lead_summary(conv, loc_info=None, additional=False):
    parts = []
    if conv.get("lead_name"):
        parts.append(f"Name: {conv['lead_name']}")
    elif conv.get("name_declined"):
        parts.append("Name: (declined)")
    if conv.get("lead_need"):
        parts.append(f"Need: {conv['lead_need']}")
    if conv.get("lead_quantity"):
        parts.append(f"Qty: {conv['lead_quantity']}")
    if conv.get("lead_location"):
        parts.append(f"Location: {conv['lead_location']}")

    if loc_info:
        parts.append(f"Route to: {loc_info['state']} ({loc_info.get('market') or 'nearest'})")
        if loc_info.get("phones"):
            parts.append("Local phones: " + ", ".join(loc_info["phones"][:2]))

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


def strip_handoff_language(text):
    """Remove any WhatsApp / hand-off wording the model may have added."""
    patterns = [
        r"(?i)tap\s+(the\s+)?(continue\s+on\s+)?whatsapp.*",
        r"(?i)click\s+(the\s+)?whatsapp.*",
        r"(?i)continue\s+on\s+whatsapp.*",
        r"(?i)reach\s+(them|us|the\s+team)\s+(directly|on\s+whatsapp).*",
        r"(?i)i.?ll\s+pass\s+this.*",
    ]
    for p in patterns:
        text = re.sub(p, "", text).strip()
    return text


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
        system += "\n\nCurrent lead state (use only for product answers, do not ask for missing fields or mention WhatsApp):\n" + json.dumps(lead_state)

    if paced and provider != "mock" and not throttle():
        return "I'm getting a lot of questions right now. Please try again in a few seconds.", "throttled"
    try:
        return fn(system, msgs), None
    except LLMError as e:
        return FALLBACK, str(e)
    except Exception as e:
        return FALLBACK, f"{type(e).__name__}: {e}"


def guard_invented_contacts(text):
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
    server_version = "CutixDemo/0.6"

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
            if not token or not hashlib.compare_digest(auth, f"Bearer {token}"):
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
            session = self.headers.get("X-Session-Id") or ""
            if not session:
                self._send(400, {"error": "missing session"})
                return
            conv = get_or_create_conversation(session)
            turns = get_recent_turns(session, limit=20)
            self._send(200, {
                "stage": conv["stage"],
                "turns": turns,
                "qualified": bool(conv["qualified"]),
            })
            return

        self._send(404, "Not found")

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/whatsapp-tapped":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            session = (body.get("session") or "").strip()
            if session:
                update_conversation(session, whatsapp_tapped=1)
            self._send(200, {"ok": True})
            return

        if path != "/chat":
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

        # Quote button – server asks first question, no model
        if quote_request:
            reply = "Tell me what you need and I'll start your quote. What size and type of cable are you looking for?"
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

        ok, err_msg, remaining = check_and_reserve(session, ip_hash)
        if not ok:
            self._send(429, {"error": err_msg})
            return

        history = get_recent_turns(session, limit=env_int("HISTORY_WINDOW", 6))
        stage = conv.get("stage") or "answering"
        qualifying = stage in ("qualifying", "awaiting_name", "intent_detected")

        # Server-controlled next question while qualifying
        if qualifying:
            # first extract what we can from this message
            temp_fields = extract_lead_fields(
                message,
                {
                    "lead_name": conv.get("lead_name"),
                    "lead_need": conv.get("lead_need"),
                    "lead_quantity": conv.get("lead_quantity"),
                    "lead_location": conv.get("lead_location"),
                    "name_declined": conv.get("name_declined") or 0,
                },
                awaiting_name=(stage == "awaiting_name"),
                qualifying=True,
            )
            q = next_missing_question(temp_fields)
            if q:
                # still missing something – ask it, optionally let model answer product part first
                t0 = time.time()
                lead_state = {
                    "name": temp_fields.get("lead_name"),
                    "need": temp_fields.get("lead_need"),
                    "quantity": temp_fields.get("lead_quantity"),
                    "location": temp_fields.get("lead_location"),
                    "stage": stage,
                }
                model_reply, error = answer(message, history=history, lead_state=lead_state)
                ms = int((time.time() - t0) * 1000)
                if error:
                    refund(session, ip_hash)
                model_reply = strip_handoff_language(model_reply or "")
                reply = (model_reply + "\n\n" + q).strip() if model_reply and not error else q
                updated, _ = advance_stage(conv, message, reply)
                update_conversation(session, **{k: v for k, v in updated.items() if not k.startswith("_")})
                log_turn(session, "user", message)
                log_turn(session, "assistant", reply, ms=ms, provider=env("PROVIDER"), error=error)
                self._send(200, {
                    "reply": reply,
                    "qualified": False,
                    "whatsapp_url": None,
                    "remaining": remaining,
                    "stage": updated["stage"],
                })
                return

        # Normal model call
        t0 = time.time()
        lead_state = {
            "name": conv.get("lead_name"),
            "need": conv.get("lead_need"),
            "quantity": conv.get("lead_quantity"),
            "location": conv.get("lead_location"),
            "stage": stage,
        }
        reply, error = answer(message, history=history, lead_state=lead_state)
        ms = int((time.time() - t0) * 1000)

        if error:
            refund(session, ip_hash)
            log_turn(session, "user", message)
            log_turn(session, "assistant", reply, ms=ms, provider=env("PROVIDER"), error=error)
            self._send(200, {"reply": reply, "qualified": False, "remaining": remaining})
            return

        reply, was_replaced = guard_invented_contacts(reply)
        if was_replaced:
            error = "invented_contact"

        reply = strip_handoff_language(reply)

        updated, newly = advance_stage(conv, message, reply)
        loc_info = updated.pop("_loc_info", None)
        update_conversation(session, **updated)

        log_turn(session, "user", message)
        log_turn(session, "assistant", reply, ms=ms, provider=env("PROVIDER"), error=error)

        whatsapp_url = None
        if updated["qualified"]:
            additional = bool(conv.get("whatsapp_tapped"))
            summary = build_lead_summary(updated, loc_info=loc_info, additional=additional)
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
