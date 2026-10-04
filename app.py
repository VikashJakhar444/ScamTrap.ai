import os
import io
import re
import json
import sys
import uuid
import html
import base64
import hashlib
import hmac
import ipaddress
import math
import secrets
import random
import time
import requests
import threading
import subprocess
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from typing import List, Optional, Dict, Any, Set

import human_engine

# Windows consoles/files default to a legacy code page that cannot encode the
# emoji used in logs - force UTF-8 so a stray glyph can never crash startup.
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and _stream.encoding and _stream.encoding.lower().replace("-", "") != "utf8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load .env file natively without third-party dependencies
def _load_dotenv_native(env_path: str = ".env"):
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'").strip('"')
                        if k and (v or k not in os.environ):
                            # .env wins when it carries a real value: a stale
                            # machine/user-level GEMINI_API_KEY must never be
                            # able to shadow the keys configured here.
                            os.environ[k] = v
        except Exception:
            pass

_load_dotenv_native()

from fastapi import FastAPI, Request, Form, Query, Body
from fastapi.responses import HTMLResponse, Response, StreamingResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# Google GenAI SDK
from google import genai
from google.genai import types

# Image Generation (Pillow)
from PIL import Image, ImageDraw, ImageFont

# PDF Generation (ReportLab)
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# =====================================================================
# 1. INITIALIZATION & GLOBAL STATE
# =====================================================================

# Global re-entrant lock protecting every read/write of STATE.
# All route handlers run in worker threads, so this makes the in-memory
# store safe under concurrent bridge + dashboard + Gemini traffic.
STATE_LOCK = threading.RLock()

# Every Gemini call is executed on a bounded worker pool. The pool is
# intentionally NOT used as a context manager: exiting a `with
# ThreadPoolExecutor(...)` block calls shutdown(wait=True), which would
# silently defeat any timeout we impose on the model call.
GEMINI_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini-agent")
GEMINI_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "15"))
# Extra time a turn may spend waiting for a rate-limited provider bucket to come
# back before we give up on the AI brain. A quiet pause reads as a normal human
# hesitation - a canned pool line does not.
GEMINI_WAIT_S = float(os.environ.get("GEMINI_WAIT_S", "15"))
# How long we will hold a turn for the TOP-priority provider group instead of
# handing it to the next one. groq writes the most natural Hinglish, so a few
# seconds of waiting beats letting a weaker provider take the conversation.
PREFERRED_WAIT_S = float(os.environ.get("PREFERRED_WAIT_S", "12"))
GEMINI_BUDGET_S = GEMINI_TIMEOUT_S + GEMINI_WAIT_S

# ---------------------------------------------------------------------
# API authentication for /api/* endpoints.
#   * Direct loopback callers (the Node bridge, local test suites,
#     a browser on the same machine) are trusted without a key.
#   * Anything that arrived through a proxy/tunnel (Cloudflare,
#     X-Forwarded-For, ...) MUST present SCAMTRAP_API_KEY.
# ---------------------------------------------------------------------
def _persist_api_key(key: str) -> None:
    """Best-effort: store the auto-generated key in .env (updating an existing
    entry in place) so the Node bridge - a separate process - can read it."""
    try:
        lines = []
        if os.path.exists(".env"):
            with open(".env", "r", encoding="utf-8") as fh:
                lines = fh.readlines()

        updated = False
        for idx, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == "SCAMTRAP_API_KEY":
                lines[idx] = f"SCAMTRAP_API_KEY={key}\n"
                updated = True
                break

        if not updated:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append("\n# Auto-generated by ScamTrap AI - do not share\n")
            lines.append(f"SCAMTRAP_API_KEY={key}\n")

        with open(".env", "w", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
    except Exception:
        pass


API_KEY = (os.environ.get("SCAMTRAP_API_KEY") or "").strip()
if not API_KEY:
    API_KEY = secrets.token_urlsafe(24)
    os.environ["SCAMTRAP_API_KEY"] = API_KEY
    _persist_api_key(API_KEY)

PROXY_HEADER_NAMES = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded", "via")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_trusted_local(request: Request) -> bool:
    """True only for direct, non-proxied loopback callers."""
    if any(h in request.headers for h in PROXY_HEADER_NAMES):
        return False
    client = request.client
    return bool(client) and client.host in LOOPBACK_HOSTS


def _extract_api_key(request: Request) -> str:
    provided = (request.headers.get("x-api-key") or "").strip()
    if not provided:
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
    if not provided:
        provided = (request.query_params.get("api_key") or "").strip()
    return provided


def _append_capped(target: list, item: Any, cap: int = 500) -> None:
    """Append while bounding memory growth of chat/thought buffers."""
    target.append(item)
    if len(target) > cap:
        del target[: len(target) - cap]


def log_thought(tag: str, thought: str) -> None:
    """Thread-safe, size-bounded append to the AI thought ticker."""
    with STATE_LOCK:
        _append_capped(STATE["thought_logs"], {
            "time": datetime.now().strftime("%H:%M:%S"),
            "tag": tag,
            "thought": thought,
        })


def _remember_message_id(msg_id: str) -> None:
    """Dedup set with FIFO eviction - clearing the whole set would
    re-admit old message IDs and cause duplicate replies."""
    seen = STATE["processed_msg_ids"]
    seen[msg_id] = True
    while len(seen) > 5000:
        seen.popitem(last=False)


app = FastAPI(title="ScamTrap AI - Autonomous WhatsApp Scammer Honeypot")

# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    path = request.url.path

    # 401 before anything else touches STATE.
    if path.startswith("/api/") and not _is_trusted_local(request):
        if not hmac.compare_digest(_extract_api_key(request), API_KEY):
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Missing or invalid SCAMTRAP_API_KEY"},
                status_code=401,
            )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self), microphone=(), camera=()"
    # Live telemetry endpoints must never be served from the browser cache -
    # a stale /api/state made the dashboard render outdated canary/geo data.
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip().strip('"')
client = None

# Multiple API keys = multiple free-tier quota buckets. On 429 we rotate to
# the next key instead of dropping to the deterministic fallback.
GEMINI_API_KEYS: list = []
for _var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"):
    _k = (os.environ.get(_var) or "").strip().strip('"')
    if _k and _k not in GEMINI_API_KEYS:
        GEMINI_API_KEYS.append(_k)


def _gemini_key_shape_ok(key: str) -> bool:
    return bool(re.fullmatch(r"AIza[0-9A-Za-z_\-]{35}", key) or re.fullmatch(r"AQ\.[0-9A-Za-z_\-]{20,}", key))


GEMINI_CLIENTS: list = []   # [(label, client)] - label is safe to log, never the key
for _i, _key in enumerate(GEMINI_API_KEYS, start=1):
    _label = f"key{_i}"
    if not _gemini_key_shape_ok(_key):
        print(f"⚠️  [STARTUP] {_label} does not look like a Google API key - will still try it once.")
    try:
        # Hard HTTP timeout - a hung Gemini call must never eat the whole
        # rotation budget before slower-but-alive providers get their turn.
        # The API rejects any deadline below 10s (HTTP 400 INVALID_ARGUMENT),
        # so 8s used to make EVERY Gemini call fail instantly.
        GEMINI_CLIENTS.append((_label, genai.Client(
            api_key=_key, http_options=types.HttpOptions(timeout=10000))))
    except Exception as exc:
        print(f"⚠️  [STARTUP] Could not initialise Gemini client for {_label}: {exc!r}")

# Per-(key, model) cooldowns so an exhausted quota bucket is skipped
# instantly instead of burning the whole 20s timeout on 429s.
GEMINI_COOLDOWN: dict = {}
_RETRY_IN_RE = re.compile(r"(?:retry|try)\s*(?:again)?\s*in\s+(?:(\d+)\s*h)?(?:(\d+)\s*m)?([\d.]+)\s*s", re.IGNORECASE)


def _cooldown_set(label: str, model: str, err: Exception, default429: float = 45.0) -> None:
    text = str(err)
    secs = default429 if ("429" in text or "RESOURCE_EXHAUSTED" in text or "rate" in text.lower()) else 20.0
    if any(s in text for s in ("API key not valid", "API_KEY_INVALID", "PERMISSION_DENIED",
                               "HTTP 401", "HTTP 403", "invalid_api_key")):
        secs = 12 * 3600.0          # broken key: sit out the session
    m = _RETRY_IN_RE.search(text)
    if m:
        secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))
    GEMINI_COOLDOWN[f"{label}:{model}"] = time.time() + min(secs, 6 * 3600.0)


def _cooldown_active(label: str, model: str) -> bool:
    until = GEMINI_COOLDOWN.get(f"{label}:{model}")
    if until is None:
        return False
    if time.time() < until:
        return True
    GEMINI_COOLDOWN.pop(f"{label}:{model}", None)
    return False


# ---------------------------------------------------------------------
# OpenAI-compatible free providers (rotated after/between Gemini buckets).
# Nothing here is truly "unlimited", but the generous ones:
#   groq         30 req/min, 1000 req/day  (needs free key, no card)
#   openrouter   20 req/min, 50-200 req/day (needs free key, no card)
#   pollinations NO key, NO daily cap - anonymous tier is 1 req / 15s,
#                which matches human conversation pacing anyway.
# ---------------------------------------------------------------------
def _oai_models(env_var: str, default: str) -> list:
    return [m.strip() for m in (os.environ.get(env_var) or default).split(",") if m.strip()]


OPENAI_COMPAT_PROVIDERS: list = []


def _add_oai_provider(group: str, env_keys: list, base_url: str, models: list,
                      key_required: bool = True, retry429_s: float = 45.0,
                      extra_payload: Optional[Dict[str, Any]] = None) -> None:
    """One group (e.g. 'groq') can hold several keys - each key is its own
    quota bucket with its own cooldown (labels: groq1, groq2, ...)."""
    n = 0
    for env_key in env_keys:
        key = (os.environ.get(env_key) or "").strip().strip('"')
        if not key:
            if not key_required:
                key = ""          # keyless provider (pollinations) still registers
            else:
                continue
        n += 1
        OPENAI_COMPAT_PROVIDERS.append({
            "group": group, "label": f"{group}{n}", "base_url": base_url.rstrip("/"),
            "key": key, "models": models, "retry429_s": retry429_s,
            "extra": extra_payload or {},
        })


_add_oai_provider("groq", ["GROQ_API_KEY", "GROQ_API_KEY_2"], "https://api.groq.com/openai/v1",
                  _oai_models("GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b"))
_add_oai_provider("openrouter", ["OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2"], "https://openrouter.ai/api/v1",
                  _oai_models("OPENROUTER_MODELS", "openrouter/free,google/gemma-4-31b-it:free"))
# Keyless floor - always registered, survives when every other quota is dead.
# Anonymous tier: 1 req / 15s, no daily cap. Only `openai-fast` works keyless -
# the plain `openai` model answers HTTP 402 (paid tier) and would only ever
# burn an attempt. Only basic params accepted (max_tokens/reasoning_effort get
# HTTP 402 on the anonymous tier).
_add_oai_provider("pollinations", ["POLLINATIONS_API_KEY"], "https://text.pollinations.ai/openai",
                  _oai_models("POLLINATIONS_MODELS", "openai-fast"),
                  key_required=False, retry429_s=18.0)

MODEL_PRIORITY = [p.strip() for p in (os.environ.get("MODEL_PRIORITY") or
                                      "groq,openrouter,pollinations,gemini").split(",") if p.strip()]

# FORCE_FALLBACK=1 hard-disables every model provider (used by test suites
# that must exercise the deterministic tactical engine, not the live chain).
FORCE_FALLBACK = os.environ.get("FORCE_FALLBACK") == "1"

if FORCE_FALLBACK:
    print("⚠️  [STARTUP] FORCE_FALLBACK=1 - all model providers disabled, deterministic engine only.")
elif not GEMINI_CLIENTS and not OPENAI_COMPAT_PROVIDERS:
    print("⚠️  [STARTUP] No model providers available - deterministic tactical engine will be used.")
else:
    _buckets = [lbl for lbl, _ in GEMINI_CLIENTS] + [p["label"] for p in OPENAI_COMPAT_PROVIDERS]
    print(f"ℹ️  [STARTUP] Model providers ready: {', '.join(_buckets)} (priority: {', '.join(MODEL_PRIORITY)})")
    if GEMINI_CLIENTS:
        client = GEMINI_CLIENTS[0][1]


def _call_openai_chat(prov: Dict[str, Any], model: str, system_instruction: str,
                      prompt: str, timeout_s: float) -> str:
    headers = {"Content-Type": "application/json"}
    if prov.get("key"):
        headers["Authorization"] = f"Bearer {prov['key']}"
    payload: Dict[str, Any] = {
        "model": model, "temperature": 0.9, "stream": False,
        "messages": [{"role": "system", "content": system_instruction},
                     {"role": "user", "content": prompt}],
    }
    payload.update(prov.get("extra") or {})
    r = requests.post(prov["base_url"] + "/chat/completions", headers=headers,
                      json=payload, timeout=max(3.0, timeout_s))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def _parse_agent_json(raw: str) -> dict:
    """Tolerant JSON extraction: models sometimes wrap output in ```json fences
    or add a sentence before/after the object."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception:
        pass
    # Last resort: pull the fields out of a malformed object so one sloppy
    # brace does not cost the whole turn a scripted fallback.
    fields: Dict[str, str] = {}
    for key in ("reply_text", "selected_tool", "internal_thought"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw or "")
        if m:
            try:
                fields[key] = json.loads(f'"{m.group(1)}"')
            except Exception:
                fields[key] = m.group(1)
    if "reply_text" in fields:
        return fields
    raise json.JSONDecodeError("no reply_text field found", raw or "", 0)

# Public base URL used to build canary / receipt links handed to scammers.
# Must be an https URL reachable by the target (Cloudflare tunnel, ngrok, ...).
PUBLIC_TUNNEL_URL = (os.environ.get("PUBLIC_TUNNEL_URL") or "").strip().rstrip("/")
if not PUBLIC_TUNNEL_URL:
    print("ℹ️  [STARTUP] PUBLIC_TUNNEL_URL not set - receipt links will fall back to the request host.")

# ---------------------------------------------------------------------
# Auto-recovery of the public canary URL (demo mode).
# If PUBLIC_TUNNEL_URL is empty and cloudflared.exe sits next to this file,
# spawn a free Cloudflare quick tunnel and adopt the
# https://*.trycloudflare.com address it prints - no manual .env edit.
# The watcher keeps the tunnel alive for the whole process lifetime.
# ---------------------------------------------------------------------
_CLOUDFLARED_EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe")
_TUNNEL_URL_RE = re.compile(r"(https://[a-z0-9\-]+\.trycloudflare\.com)")


def _adopt_tunnel_url(url: str) -> None:
    """Publish a freshly recovered tunnel URL to canary link builders."""
    global PUBLIC_TUNNEL_URL
    url = url.rstrip("/")
    if not url or url == PUBLIC_TUNNEL_URL:
        return
    PUBLIC_TUNNEL_URL = url
    try:
        with STATE_LOCK:
            STATE["public_tunnel_url"] = url
    except Exception:
        pass
    print(f"ℹ️  [TUNNEL] Public canary URL auto-recovered: {url}")


def _auto_tunnel_worker() -> None:
    """Keep a free Cloudflared quick tunnel running and adopt its URL."""
    if PUBLIC_TUNNEL_URL:
        return
    if not os.path.isfile(_CLOUDFLARED_EXE):
        print("ℹ️  [TUNNEL] cloudflared.exe not found - canary links fall back to request host.")
        return
    port = int(os.environ.get("PORT", "8000"))
    while True:
        try:
            proc = subprocess.Popen(
                [_CLOUDFLARED_EXE, "tunnel", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print("ℹ️  [TUNNEL] cloudflared started - waiting for public URL...")
            if proc.stdout is None:
                raise RuntimeError("cloudflared stdout pipe unavailable")
            for line in proc.stdout:
                match = _TUNNEL_URL_RE.search(line)
                if match:
                    _adopt_tunnel_url(match.group(1))
            proc.wait()
        except Exception as error:
            print(f"⚠️  [TUNNEL] cloudflared error: {error}")
        print("⚠️  [TUNNEL] cloudflared exited - restarting in 5s (fresh URL).")
        time.sleep(5)

# ---------------------------------------------------------------------
# Victim persona + conversation session memory (the anti-trap brain).
# A bot gets caught the moment it contradicts itself, forgets who it
# claimed to be, or pushes a payment script into a non-payment chat.
# Both facts live in STATE for the whole session so every reply -
# deterministic or Gemini - stays consistent.
# ---------------------------------------------------------------------
PERSONA_FIRST_NAMES = [
    "Rohit", "Aman", "Deepak", "Vikram", "Sahil", "Karan", "Nikhil",
    "Pooja", "Ananya", "Ritu", "Sneha", "Rahul", "Aditya", "Manish",
    "Suresh", "Priya", "Sunil", "Rajesh", "Amit", "Kavita", "Sanjay",
    "Gaurav", "Vikas", "Pankaj", "Alok", "Harish", "Neeraj", "Tarun"
]
PERSONA_CITIES = [
    "Jaipur", "Jodhpur", "Kota", "Delhi", "Noida", "Gurugram", "Mumbai",
    "Pune", "Ahmedabad", "Surat", "Indore", "Bhopal", "Lucknow", "Kanpur",
    "Patna", "Kolkata", "Bengaluru", "Hyderabad", "Chandigarh", "Nagpur",
    "Agra", "Varanasi", "Dehradun", "Ranchi", "Vadodara", "Ghaziabad"
]
PERSONA_JOBS = [
    "mobile accessories shop counter sambhalta hu",
    "customer support executive hu BPO me",
    "Zomato delivery partner hu",
    "grocery store hisab-kitab dekhta hu",
    "private bank me junior clerk hu",
    "electrician ka freelance kaam karta hu",
    "medical store pe billing assistant hu",
    "college student hu aur part time coaching padhata hu",
    "automobile workshop me service advisor hu",
    "accounts tally operator hu small firm me",
    "graphic designer freelance kaam karta hu",
    "hardware shop pe sales assistant hu"
]


def generate_case_id() -> str:
    """Generates an authentic NCRP (National Cyber Crime Reporting Portal) reference ID."""
    ts = datetime.now().strftime("%Y%m%d")
    rand_seq = f"{secrets.randbelow(900000) + 100000}"
    return f"NCRP-CYBER-{ts}-{rand_seq}"


def get_contextual_payee(sender_number: str = "") -> str:
    """Returns the most relevant real UPI/payee ID from extracted intelligence or sender identity."""
    with STATE_LOCK:
        if STATE.get("extracted_intel", {}).get("upi_ids"):
            return next(iter(STATE["extracted_intel"]["upi_ids"]))
        if STATE.get("extracted_intel", {}).get("bank_accounts"):
            acc = next(iter(STATE["extracted_intel"]["bank_accounts"]))
            return f"acc.{acc[-4:]}@sbi"
        clean_phone = re.sub(r"\D", "", sender_number or "")
        if len(clean_phone) >= 10:
            return f"{clean_phone[-10:]}@paytm"
        if STATE.get("extracted_intel", {}).get("phone_numbers"):
            phone = next(iter(STATE["extracted_intel"]["phone_numbers"]))
            clean_p = re.sub(r"\D", "", phone)
            if len(clean_p) >= 10:
                return f"{clean_p[-10:]}@ybl"
    return "upi.payee@okaxis"


def _fresh_session() -> Dict[str, Any]:
    """Per-conversation behavioural state. pace = typing personality for
    this session (some people are slow typists, some fast) so reply timing
    never follows a detectable fixed formula."""
    return {
        "turn": 0,
        "stage": "rapport",        # rapport -> bait -> glitch -> canary
        "glitch_sent": 0,          # dashboard telemetry only
        "links_sent": 0,           # dashboard telemetry only (replies read these off the chat)
        "scammer_links_seen": 0,   # dashboard telemetry only
        "recent_replies": [],      # last replies, never phrased the same twice
        "pace": round(random.uniform(0.85, 1.30), 2),
    }


STATE: Dict[str, Any] = {
    "case_id": generate_case_id(),
    "wa_status": "DISCONNECTED",   # "DISCONNECTED", "QR_READY", "AUTHENTICATED", "CONNECTED"
    "wa_phone": None,
    "wa_my_jid": None,
    "wa_qr_raw": "",
    "trapped_numbers": set(),      # set of strings, e.g. {"919876543210"}
    "monitored_numbers": set(),    # set of strings, e.g. {"919876543210"}
    "target_scammer": None,        # current active scammer phone string (e.g. "+919876543210")
    "target_mode": "STANDBY",      # "STANDBY", "TRAP", "MONITOR"
    "incoming_threads": {},        # phone_number -> {jid, sender_number, last_msg, last_time, mode, is_trapped, is_monitored, msg_count}
    "scammer_chat": [],            # [{role, sender, text, time, media, trap_url}]
    "conversation_key": None,      # sender number owning scammer_chat; new key = new conversation
    "bot_command_chat": [          # [{role, sender, text, time}]
        {
            "role": "bot",
            "sender": "ScamTrap AI Bot",
            "text": "🛡️ ScamTrap AI Autonomous Honeypot Ready. Auto-Triage Active for inbound WhatsApp threats.",
            "time": datetime.now().strftime("%H:%M:%S")
        }
    ],
    "thought_logs": [              # [{time, tag, thought}]
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "tag": "SYSTEM",
            "thought": "Autonomous Honeypot engine initialized. Awaiting WhatsApp Web Bridge connection."
        }
    ],
    "extracted_intel": {
        "upi_ids": set(),
        "phone_numbers": set(),
        "ifsc_codes": set(),
        "bank_accounts": set(),
    },
    "canary_hits": [],             # [{receipt_id, ip, user_agent, os_device, timestamp}]
    "outbox_queue": [],            # [{target_jid, text, media_base64, bot_report}]
    "processed_msg_ids": OrderedDict(),  # msg_id -> True, FIFO-evicted (dedup, bounded)
    # ---- Additive analytics layer (dashboard widgets) ----
    "ai_activity": {"active": False, "phase": "", "until_ms": 0.0},   # thinking/typing window
    "funnel": {"stage": 0, "payment_asks": 0, "blocked": 0, "stalled_amounts": []},  # 0 APPROACH..4 BLOCKED
    "scam_type_counts": {},        # label -> message count (donut chart)
    "aggression": {"current": 0, "average": 0, "peak": 0, "history": []},
    "persona": None,               # persistent victim identity (name/city/job)
    "session": _fresh_session(),   # stage, counters, recent replies, pace
    "public_tunnel_url": PUBLIC_TUNNEL_URL
}


# =====================================================================
# 2. REGEX INTEL EXTRACTOR
# =====================================================================
def extract_intelligence(text: str) -> List[str]:
    """Extracts UPI IDs, Indian Mobile Numbers, IFSC codes, and Bank Accounts."""
    new_findings = []

    with STATE_LOCK:
        # 1. UPI ID Regex
        upi_pattern = r"\b[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}\b"
        for match in re.findall(upi_pattern, text):
            if not match.lower().endswith(("@gmail.com", "@yahoo.com", "@outlook.com", "@hotmail.com")):
                if match not in STATE["extracted_intel"]["upi_ids"]:
                    STATE["extracted_intel"]["upi_ids"].add(match)
                    new_findings.append(f"UPI ID: {match}")

        # 2. Indian Mobile Number Regex
        phone_pattern = r"(?:\+91[\-\s]?)?[6-9]\d{9}\b"
        for match in re.findall(phone_pattern, text):
            clean_phone = re.sub(r"[\s\-]", "", match)
            if clean_phone not in STATE["extracted_intel"]["phone_numbers"]:
                STATE["extracted_intel"]["phone_numbers"].add(clean_phone)
                new_findings.append(f"Phone: {clean_phone}")

        # 3. IFSC Code Regex
        ifsc_pattern = r"\b[A-Z]{4}0[A-Z0-9]{6}\b"
        for match in re.findall(ifsc_pattern, text.upper()):
            if match not in STATE["extracted_intel"]["ifsc_codes"]:
                STATE["extracted_intel"]["ifsc_codes"].add(match)
                new_findings.append(f"IFSC Code: {match}")

        # 4. Bank Account Numbers (11 to 18 digits)
        acc_pattern = r"\b\d{11,18}\b"
        for match in re.findall(acc_pattern, text):
            if match not in STATE["extracted_intel"]["bank_accounts"]:
                STATE["extracted_intel"]["bank_accounts"].add(match)
                new_findings.append(f"Bank Account: {match}")

    return new_findings


def extract_amount_demanded(text: str, default: str = "") -> str:
    """Extracts the exact numerical amount demanded by the scammer from text or chat history."""
    with STATE_LOCK:
        sources = [text]
        if STATE.get("scammer_chat"):
            for m in reversed(STATE["scammer_chat"]):
                if m.get("role") == "scammer" and m.get("text"):
                    sources.append(m["text"])

        for src in sources:
            # Pattern 1: ₹ or Rs or INR followed by numbers (e.g. ₹25,000, Rs. 10,500, Rs 500)
            p1 = re.findall(r"(?:₹|rs\.?|inr)\s*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]+(?:\.[0-9]{2})?)", src, re.IGNORECASE)
            if p1:
                raw = p1[0].replace(",", "").split(".")[0].strip()
                if raw.isdigit() and int(raw) > 0:
                    val = int(raw)
                    return f"{val:,}" if val >= 1000 else str(val)

            # Pattern 2: Numbers followed by rs/rupaye/rupees/inr/pending/bill (e.g. 25000 rs, 5000 rupaye)
            p2 = re.findall(r"\b([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{2,7})\s*(?:rs|rupaye|rupees|inr|/-|karo|bhej|de|pending|bill|fine|charge)", src, re.IGNORECASE)
            if p2:
                raw = p2[0].replace(",", "").split(".")[0].strip()
                if raw.isdigit() and int(raw) > 0:
                    val = int(raw)
                    return f"{val:,}" if val >= 1000 else str(val)

            # Pattern 3: Standalone numbers between 50 and 5000000 if near payment keywords
            if any(w in src.lower() for w in ["pay", "bill", "transfer", "amount", "paisa", "paise", "fine", "cut", "fee", "discom", "electricity", "karo", "bhejo"]):
                p3 = re.findall(r"\b([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{3,7})\b", src)
                for cand in p3:
                    raw = cand.replace(",", "").strip()
                    if raw.isdigit():
                        val = int(raw)
                        if 50 <= val <= 5000000 and not (1900 <= val <= 2099):
                            return f"{val:,}" if val >= 1000 else str(val)

    return default


# =====================================================================
# 3. NATURAL HINGLISH PERSONA & GEMINI 2.5 FLASH AGENT ENGINE
# =====================================================================
class AgentDecision(BaseModel):
    internal_thought: str
    selected_tool: str  # "NONE", "SEND_FAKE_UPI_GLITCH", "SEND_CANARY_LINK"
    reply_text: str


# ---------------------------------------------------------------------
# Conversation brain helpers: persona, session memory, intent classifier
# ---------------------------------------------------------------------
_URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|\b[a-z0-9\-]{2,30}\.(?:com|in|net|org|xyz|link|top|club|info|site|online|click|live|app|me)\b)",
    re.IGNORECASE,
)
_UPi_ID_RE_FALLBACK = re.compile(r"\b[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}\b")
_IFSC_CODE_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_MATH_RE = re.compile(r"(-?\d+)\s*([+\-*/x×])\s*(-?\d+)")
_PAYMENT_ASK_RE = re.compile(
    r"(kisme bhej|upi id|bhej ?do|bhejo|bhejna|account de|ifs(c)? |gpay|phonepe|paytm|transfer|im?ps|payment karo|pay karo|qr ?code)",
    re.IGNORECASE,
)
_EMAIL_DOMAINS = ("@gmail.com", "@yahoo.com", "@outlook.com", "@hotmail.com", "@icloud.com")


def _ensure_persona_locked() -> Dict[str, Any]:
    """Create the session's victim identity once and keep it forever.
    Contradicting your own name/city/job is the #1 way bots get burned."""
    persona = STATE.get("persona")
    if not persona:
        persona = {
            "name": random.choice(PERSONA_FIRST_NAMES),
            "age": random.randint(21, 27),
            "city": random.choice(PERSONA_CITIES),
            "job": random.choice(PERSONA_JOBS),
        }
        STATE["persona"] = persona
    return persona


_PAYMENT_CONTEXT_INTENTS = {"payment_details", "money_demand", "scam_bill", "scam_job", "scam_threat"}


def _link_allowed(glitch_sent: int, scam_links: int, links_sent: int, force: bool = False) -> bool:
    """Allows sending canary forensic tracking links to capture suspect IP and location.
    Hard budget of 3 links per conversation - enough for the IP capture, low enough
    that the link never becomes a spam pattern the scammer can pattern-match."""
    if links_sent >= 3:
        return False
    return True


def _known_payee_locked(text: str = "") -> Optional[str]:
    """The UPI/payee id this conversation turn is dealing with - taken from the
    message in front of us, else from what the chat already extracted."""
    match = _UPi_ID_RE_FALLBACK.search(text or "")
    payee = match.group(0) if match else None
    if payee and payee.lower().endswith(_EMAIL_DOMAINS):
        payee = None
    if payee:
        return payee
    return next(iter(STATE["extracted_intel"]["upi_ids"]), None)


def _payload_decision_locked(intent: str, text: str) -> str:
    """Which payload (if any) this turn has earned, decided purely from the
    conversation - never from a running counter:

      "none"      - casual chat, or a payment demand before any payment details
                    were shared: the victim just asks how to pay.
      "screenshot"- their UPI id is known and the failed-transfer screenshot
                    story has not been told yet in this conversation.
      "link"      - they gave bank+IFSC details, or they are pushing for money
                    AFTER the failure story: the natural moment the victim
                    pastes "maine bhej di, espe dekh le" tracking link.

    Caller must hold STATE_LOCK."""
    if intent not in _PAYMENT_CONTEXT_INTENTS:
        return "none"

    glitch_sent, links_sent, scam_links = _payload_state_from_chat_locked()
    intel = STATE["extracted_intel"]
    has_bank = bool(intel["bank_accounts"] or intel["ifsc_codes"])
    if not has_bank:
        body = text or ""
        has_bank = bool(
            _IFSC_CODE_RE.search(body.upper())
            or re.search(r"(a/?c|account|ac ?no|acct)\s*(no|number|#)?\s*[:\-]?\s*\d{9,}", body, re.IGNORECASE)
        )

    budget_ok = _link_allowed(glitch_sent, scam_links, links_sent)

    if has_bank:
        return "link" if budget_ok else "none"
    if not _known_payee_locked(text):
        return "none"                       # payment details not shared yet -> ask
    if glitch_sent == 0:
        return "screenshot"                 # first time their UPI shows up
    # The failure story is already in the chat. A tracking link only makes
    # sense while they are still pushing on THAT payment - if they have
    # pivoted to a brand new pitch (another bill, a fresh threat), answering
    # with "bank hold, open this status page" is nonsense in their story and
    # is exactly what makes a honeypot read like a script.
    if intent in ("money_demand", "payment_details"):
        return "link" if budget_ok else "none"  # "payment nahi aayi" push -> tracking link
    return "none"                               # new pitch -> bait them again first


def classify_intent(text: str) -> str:
    """Map ANY incoming message to a conversational intent so the reply
    always answers the message in front of it instead of running a script.
    Order matters: interrogation traps and refusals must beat payment logic,
    otherwise the brain walks straight into the scammer's test."""
    text = text or ""
    t = " " + re.sub(r"\s+", " ", text.strip().lower()) + " "

    # --- interrogation traps (must never trigger payment talk) ---
    if re.search(r"\b(otp|upi ?pin|atm ?pin|password|cvv|screen ?share|screen ?mirror)\b", t) and \
            re.search(r"\b(send|share|bhej|bhejo|bhejna|give|tell|daal|daalo|enter|check|de do)\b", t):
        return "otp_ask"
    if re.search(r"\b(bot|robot|artificial|machine|script(ed)?|programmed|chatgpt|automated)\b", t) or \
            re.search(r"\b(are you (a )?(bot|ai|robot|human|real|alive)|prove (you|it|urself)|is this a bot|you (sound|type) like|typing speed|too fast|instant reply|turant reply)\b", t) or \
            re.search(r"\bai\b", t) and re.search(r"\b(you|are|your|this)\b", t):
        return "suspicion"
    if _MATH_RE.search(text) or re.search(
        r"\b(what('?| i)?s my name|my name kya|kya mera naam|last message kya|what did i say|kya bola|repeat (what|it)|yaad hai maine)\b", t
    ):
        return "test_challenge"
    if re.search(r"\b(video ?call|voice ?note|voice ?call|call ?me|call ?kar|call ?karo|phone ?kar|ring ?kar|face ?time|whatsapp ?call)\b", t):
        return "call_ask"
    if re.search(r"\b(photo|pics?|selfie|picture|your pic|send pic|video bhej|apni photo)\b", t):
        return "photo_ask"

    money_word = re.search(
        r"\b(money|paisa|paise|rupaye|rupees|payment|fees?|charge|amount|bhejna|transfer)\b|\brs\.?\s*\d|\u20b9\s*\d",
        t,
    )
    push_word = re.search(
        r"(nahi aaya|nhi aaya|nahi bheja|nhi bheja|credit nahi|receive nahi|kab bhej|jaldi bhej|abhi bhej"
        r"|bhej do|bhejo|turant bhej|bhej diya kya|kitna bheja|payment status|paisa bhej|rupaye bhej"
        r"|send kar|send karo|pay kar|pay karo|verify kar|check kar|hua nahi|credit nahi hua)",
        t,
    )
    neg_word = re.search(r"\b(no|not|dont|don'?t|never|skip|leave|free|without|reject|cancel)\b", t) \
        or any(s in t for s in ("nhi chahiye", "nahi chahiye", "chhod", "rehne"))
    if money_word and neg_word:
        # e.g. "I don't want money to check" - drop the payment act immediately
        return "refusal"

    if len(t.strip()) <= 30 and re.search(r"\b(bye|byee|good ?night|gn|tata|alvida|see you|chalta hu|nikal ja)\b", t):
        return "goodbye"

    # Payment artefacts in THEIR message -> they gave / pointed at payment details
    if _UPi_ID_RE_FALLBACK.search(text) or _IFSC_CODE_RE.search(text.upper()) or \
            re.search(r"(a/?c|account|ac ?no|acct)\s*(no|number|#)?\s*[:\-]?\s*\d{9,}", t):
        return "payment_details"

    if re.search(r"\b(bill|electricity|bijli|discom|meter|power ?cut|due|recharge|lpg|gas ?cylinder|light ?kat)\b", t):
        return "scam_bill"
    if re.search(r"\b(arrest|cbi|ed ?notice|court|warrant|customs|parcel|courier|kyc|aadhaar|sim ?block|account ?block|bank ?block|illegal|luggage|money ?launder)\b", t):
        return "scam_threat"
    if re.search(r"\b(job|part ?time|work from home|salary|invest|investment|lottery|reward|prize|win|trading|refund|bonus|joining|registration|deposit|scheme)\b", t):
        return "scam_job"
    if push_word or (money_word and re.search(r"\b(send|bhej|bhejo|bhejna|bhejne|pay|karo|karna|transfer|de|de do|do|pending|today|abhi|now|jaldi|urgent|clear)\b", t)):
        return "money_demand"
    if re.search(r"\b(jaldi|fatafat|turant|abey|oye|fraud|dhokhebaz|complaint|lodge|kitni ?der|answer me|jawaab|reply fast|bakwas|nonsense|waste of time)\b", t):
        return "hurry_abuse"
    if _URL_RE.search(text):
        return "link_received"
    if re.search(r"\b(kon|kaun|kaun ho|kaun hai|who are you|who r u|who is this|pehchana|mera number|tumhara naam|your name|naam kya|introduce|kaise pahchana)\b", t):
        return "identity"
    if re.search(r"\b(wrong number|galat number|galti se|unknown number|do i know you|naya number)\b", t):
        return "wrong_number"
    if re.match(r"^\s*(hi|hii+|hello|hey|yo|oye|ay|namaste|namaskar|salam|assalam|hlo|hallo)\b[\s!.,]*$", t):
        return "greeting"
    if re.search(r"\b(kaise hai|kaisa hai|kaisi hai|kya haal|haal kaisa|how are you|how r u|kya chal|kya kar rahe|what'?s up|whats up|sab theek|sab badhiya|kya scene)\b", t):
        return "how_are_you"
    if re.search(r"\b(chal|chale|chalega|chalo|chalte|milte|milna|aana|jana|jaana|movie|party|lunch|dinner|coffee|canteen|clg|college|hostel|ground)\b", t):
        return "social"
    if len(t.strip()) <= 25 and re.match(r"^\s*(haan|han|yes|yep|yup|ok|okay|okayy|sure|right|correct|thik|theek|sahi|done|bhej diya|kar diya)\b", t):
        return "affirm"
    if len(t.strip()) <= 25 and re.match(r"^\s*(nahi|nhi|no|nope|na|never)\b", t):
        return "negate"
    if "?" in text or re.match(r"^\s*(kya|kyu|kyun|kab|kaise|kese|kaun|kaunsa|kitne|kitna|where|when|why|how|what|who|is|are|did|do|does|can|could|would|will)\b", t):
        return "question"
    return "fallback"


_EN_TOKEN_RE = re.compile(
    r"\b(the|you|your|please|money|send|need|want|account|bill|job|what|why|when|where|how"
    r"|hello|thanks|call|with|this|that|from|have|don'?t|is|are|do|does|check|now|today|okay)\b",
    re.IGNORECASE,
)
_HI_TOKEN_RE = re.compile(
    r"\b(kya|kaise|kyu|kyun|kaun|kaunsa|kitna|kitne|hai|hoon|hu|bhai|yaar|arre|acha|accha"
    r"|theek|thik|nahi|nhi|haan|chalo|chalte|chalega|karo|karna|karte|kar|bhej|bhejo|bhejna"
    r"|paisa|paise|naam|tum|tumhara|tumhe|mera|meri|tera|teri|bata|batao|bol|bolo|dekh|dekho"
    r"|abhi|jaldi|ruk|aaj|kal|matlab|kaam|phir|raha|rahi|gaya|kiya|kyunki|se|pe|aur)\b",
    re.IGNORECASE,
)


def _lang_signal(text: str) -> str:
    """'en', 'hi', or '' when a message carries too little signal to judge.
    Judging language message-by-message flips mid-chat ('hello' reads English,
    'kaun ho tum' Hinglish), so callers can also look at the whole conversation."""
    text = text or ""
    if not text.strip():
        return ""
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    t = text.lower()
    eng = len(_EN_TOKEN_RE.findall(t))
    hin = len(_HI_TOKEN_RE.findall(t))
    if eng >= 2 and eng > hin:
        return "en"
    if hin >= 2 and hin > eng:
        return "hi"
    return ""


def _wants_english(text: str) -> bool:
    """Mirror the scammer's language - replying in Hinglish to a pure-English
    scam script (or vice versa) is an instant tell."""
    return _lang_signal(text) == "en"


def _conversation_is_english_locked(newest: str) -> bool:
    """Is this chat being held in English?

    The gate must look at the whole conversation, not just the newest line:
    a scammer who opens in Hinglish and then copies an English script still
    expects Hinglish back, and throwing away a reply for one mixed message is
    what used to push the conversation onto the scripted fallback pools."""
    if not _wants_english(newest or ""):
        return False
    for m in STATE.get("scammer_chat") or []:
        if m.get("role") == "scammer" and _lang_signal(m.get("text") or "") == "hi":
            return False
    return True


def _fill(pool: List[str], ctx: Dict[str, Any]) -> List[str]:
    return [s.format(**ctx) for s in pool]


def _pick(options: List[str], recent: List[str]) -> str:
    """Choose a variant that has not been used recently - repeating yourself
    verbatim is the classic scripted-bot giveaway."""
    if not options:
        return ""
    fresh = [o for o in options if o not in recent[-6:]]
    return random.choice(fresh or options)


def _advance_session(reply_text: str, media_sent: bool, link_sent: bool) -> None:
    """Bookkeeping after every outbound reply: stage counters + variation history."""
    with STATE_LOCK:
        sess = STATE["session"]
        sess["turn"] = int(sess.get("turn", 0)) + 1
        if media_sent:
            sess["glitch_sent"] = int(sess.get("glitch_sent", 0)) + 1
            sess["stage"] = "glitch"
        if link_sent:
            sess["links_sent"] = int(sess.get("links_sent", 0)) + 1
            sess["stage"] = "canary"
        if reply_text and not media_sent and not link_sent:
            if _PAYMENT_ASK_RE.search(reply_text):
                sess["stage"] = "bait"
        if reply_text:
            _append_capped(sess["recent_replies"], reply_text, cap=6)
            f = STATE["funnel"]
            f["stage"] = max(int(f["stage"]), 1)   # RAPPORT reached


def _payload_state_from_chat_locked() -> tuple[int, int, int]:
    """(screenshots we sent, canary links we sent, links they sent) counted
    straight off the conversation log instead of session bookkeeping, so every
    reply decision is made from the chat itself and stays correct no matter how
    long the conversation gets or whether the session was reset mid-chat.
    Must be called with STATE_LOCK held."""
    glitch_sent = links_sent = scam_links = 0
    for m in STATE["scammer_chat"]:
        if m.get("role") == "scammer":
            if _URL_RE.search(str(m.get("text") or "")):
                scam_links += 1
        else:
            if m.get("media"):
                glitch_sent += 1
            if m.get("trap_url"):
                links_sent += 1
    return glitch_sent, links_sent, scam_links


def _start_new_conversation_locked(prev_key: str, new_key: str) -> None:
    """Switch the active conversation to a new scammer: restart the kill chain
    from stage 0 instead of carrying the previous chat's funnel, session, IOC
    and message history into this one.

    Phone-level state survives (persona, canary hits, thought logs, thread
    telemetry, processed msg ids) - only conversation-level state is dropped.
    Must be called with STATE_LOCK held."""
    prev_key_label = prev_key if prev_key.startswith("LID:") else f"+{prev_key}"
    new_key_label = new_key if new_key.startswith("LID:") else f"+{new_key}"
    STATE["conversation_key"] = new_key
    STATE["scammer_chat"].clear()
    STATE["funnel"] = {"stage": 0, "payment_asks": 0, "blocked": 0, "stalled_amounts": []}
    STATE["scam_type_counts"] = {}
    STATE["aggression"] = {"current": 0, "average": 0, "peak": 0, "history": []}
    STATE["extracted_intel"] = {
        "upi_ids": set(),
        "phone_numbers": set(),
        "ifsc_codes": set(),
        "bank_accounts": set(),
    }
    STATE["session"] = _fresh_session()
    STATE["ai_activity"] = {"active": False, "phase": "", "until_ms": 0.0}
    # Let the incoming handler re-point the dashboard at the new suspect.
    STATE["target_scammer"] = None
    STATE["target_mode"] = "STANDBY"
    log_thought(
        "CONVERSATION",
        f"New conversation from {new_key_label} - closing {prev_key_label}. "
        "Kill chain restarted (funnel back to APPROACH, session/IOCs/chat cleared).",
    )


def _build_plan_for(reply_text: str, media: bool, prompt_len: int, elapsed_ms: int = 0) -> tuple[List[Dict[str, Any]], int]:
    """Compile a reply into the timed human delivery plan + the legacy
    recommended_delay_ms (time the bridge still waits before the first bubble).

    elapsed_ms = time already spent since the scammer's message arrived
    (the model generating this reply) - it is absorbed into the read delay so
    the *total* human delay stays realistic even when Gemini is slow."""
    with STATE_LOCK:
        pace = float(STATE["session"].get("pace", 1.0))
    plan = human_engine.build_delivery_plan(
        reply_text, media=media, prompt_len=prompt_len, pace=pace, elapsed_ms=elapsed_ms)
    return plan, human_engine.plan_time_to_first_send_ms(plan)


# ---------------------------------------------------------------------
# Reply pools. Every pool has Hinglish + English variants, multiple
# phrasings per situation, and nothing here talks about payment unless
# the intent that selected it was a payment intent.
# ---------------------------------------------------------------------
POOL_GLITCH_HI = [
    "Bhai {amt} bhej diya teri UPI pe par 'Daily Limit Exceeded' aa gaya. Ye dekh screenshot, bank hold dikha raha hai. Tu dusra account + IFSC de, IMPS se kar deta hu.",
    "Try kar liya {amt} ka, par U16 error maar raha hai. Screenshot le. Aapna alternate bank account bhejo, main IMPS laga deta hu.",
    "Bhai payment {amt} ki fail ho gayi, teri UPI pe daily limit khatam. Ye screenshot dekh aur dusra wala account de jaldi.",
]
POOL_GLITCH_EN = [
    "I tried sending {amt} but it failed with a daily limit error. Check the screenshot. Send me another account and I'll do IMPS instead.",
    "Tried {amt} - UPI blocked it with a limit error. See the screenshot. Give me a second account, I'll retry from there.",
]
POOL_CANARY_HI = [
    "Bhai wahi to bol raha hu, bank hold pe hai. Ye official NPCI clearance page hai, ispe check kar: {url} - turant clear ho jayega.",
    "Receiver verification pending hai bank se. Ek baar is link pe approve kar de: {url} abhi credit ho jayega.",
    "Screenshot me jo dikh raha hai wahi hai, bank ne hold kiya hai. Ye wala link khol ke confirm kar: {url}",
]
POOL_CANARY_EN = [
    "It's on hold at the bank. Open this verification page and confirm: {url} - it will be released right away.",
    "The receiver verification is pending from the bank. Check this link once: {url}",
]
POOL_IMPS_HI = [
    "Bhai {amt} ka IMPS laga diya tha account details pe, par security check ki wajah se hold pe chala gaya. Ek baar is clearance link pe verify kar: {url} turant credit ho jayega.",
    "IMPS initiate kar diya {amt} ka, par bank hold dikha raha hai. Ye official settlement link hai, khol ke approve kar: {url}",
]
POOL_IMPS_EN = [
    "I already sent the {amt} IMPS but it went on a security hold. Verify it here once: {url} and it will be credited.",
    "IMPS for {amt} is stuck in bank verification. Open this settlement link and approve: {url}",
]
POOL_NOLINK_HI = [
    "Arre main bhi dekh raha hu, bank se message aaya hai verification pending hai. Dusra account de to IMPS se try karu.",
    "Bhai bank wale baar baar hold dikha rahe hai. Tu ek saaf saaf alternate UPI ya account de, usse try karta hu.",
    "Ruk, abhi bank app me dekh raha hu... verification pending likha hai. Koi aur account hai to bhej de.",
]
POOL_NOLINK_EN = [
    "The bank keeps putting it on hold. Send me a clean alternate UPI or account and I'll try from there.",
    "I'm checking the bank app - it says verification pending. Do you have another account?",
]
POOL_BAIT_BILL_HI = [
    "Arre kaunsa bill pending hai bhai? Bijli ka ya mobile ka? Kitne ka amount baki hai?",
    "Bijli cut hone wali hai kya? Kaunsa connection number hai aur kitna due hai batao?",
    "Acha kiska bill pending hai? Kitna baki hai, mujhe check karke batao?",
    "Kaat rahe hai kya connection? Batao bill kitne ka hai aur kiska hai?",
]
POOL_BAIT_BILL_EN = [
    "Wait, which bill is pending? Electricity or mobile? How much is due?",
    "Is the connection getting disconnected? Which account number and what is the amount?",
]
POOL_BAIT_JOB_HI = [
    "Arre serious? Ye to badhiya hai. Kaam kya karna padega exactly aur kitna milega?",
    "Acha job hai? Bata details, kaise join karna hai aur paisa kaise aayega?",
    "Interesting bhai, par joining ke liye kuch dena padega kya? Bata sab clear.",
    "Theek hai, par pehle bata company ka naam kya hai aur interview kaise hoga?",
]
POOL_BAIT_JOB_EN = [
    "Really? That sounds good. What exactly do I have to do and how much does it pay?",
    "Okay, how do I join and is there any joining fee? Tell me the details.",
]
POOL_BAIT_THREAT_HI = [
    "Arre police/CBI wale ka message aaya hai? Kya bol rahe hai exactly, bata. Ghabra raha hu thoda.",
    "Parcel atak gaya? Kaunsa parcel hai aur kitna charge bol rahe hai customs wale?",
    "Kya baat kar raha hai bhai, court notice? Mujhe detail me batao kya likha hai usme.",
    "KYC block ho rahi hai kya? Mere bank wale ko abhi call karta hu... pehle tu bata kya bol rahe hai.",
]
POOL_BAIT_THREAT_EN = [
    "Wait, the police sent you this? What exactly did they say? I'm a bit worried now.",
    "Customs is holding a parcel? How much are they asking for? Send the UPI ID.",
]
POOL_BAIT_MONEY_HI = [
    "Haan bhai theek hai. Kisme bhejna hai? Apna GPay / PhonePe UPI ID ya QR bhej de, abhi karta hu.",
    "Bhai kis UPI ID pe transfer karna hai? GPay khol ke baitha hu, bhej de.",
    "Kaunse UPI ya account pe pay karna hai? Details de, turant kar deta hu.",
    "Theek hai bhai, kitna aur kahan bhejna hai? UPI ID bhej abhi try karta hu.",
]
POOL_BAIT_MONEY_EN = [
    "Okay, where should I send it? Share the UPI ID and I'll pay right now.",
    "Which UPI or account should I transfer to? Send it, I'm on GPay already.",
]
POOL_REFUSAL_HI = [
    "Acha chhod de koi baat nahi. To bhai bata, aur kya chal raha hai aaj kal?",
    "Theek hai theek hai, zaroorat nahi hai. Waise tu hai kya karta hai bhai?",
    "Sahi hai chhod o, aise hi baat karte hai. Bata aur kya scene?",
    "Ok bhai koi dikkat nahi. Kal ka koi plan hai?",
]
POOL_REFUSAL_EN = [
    "Oh okay, no worries at all. So what do you do, by the way?",
    "Fair enough. Anyway, what's going on with you?",
    "Alright then. So tell me, what were you saying earlier?",
]
POOL_OTP_HI = [
    "Acha? OTP kaun deta hai bhai, koi bhi maange to de deta hu kya? Nahi.",
    "Arre OTP to nahi bhej sakta yaar, fraud hote hai ye sab. Seedha UPI ID bhej de main kar deta hu.",
    "Bhai OTP mere paas bhi aaya tha, main kisi ko nahi deta. Papa ne sikhaya hai ye sab.",
    "Pin win nahi bataunga bhai 😄 Aap request bhej de GPay pe, main approve kar dunga.",
]
POOL_OTP_EN = [
    "OTP? Why would I share that with anyone - no chance.",
    "I don't share OTPs, sorry. Just send a GPay request and I'll approve it.",
]
POOL_SUSPICION_HI = [
    "Arre nahi bhai, bot hota to itni der me kyu likhta, main khud hu.",
    "Kaun sa bot yaar, main hi to hu. Thoda time lagta hai likhne me.",
    "Bot wot kya bol raha hai bhai, insaan hu tera. Bolo kaam kya hai?",
    "Nahi re, bot nahi hu. Haan thoda busy rehta hu isliye late ho jaata hai.",
]
POOL_SUSPICION_EN = [
    "Haha what? I'm not a bot, I'm just typing slow on this phone.",
    "Nope, not a bot. A regular person, sorry for the delay.",
    "A bot would reply instantly - I took my time. What do you need?",
]
POOL_MATH_HI = [
    "😂 {expr} = {ans}, ye to papa ke phone me bhi kar leta hu. Ab batao kya chahiye tha?",
    "{ans} bhai, aasan hai ye 😄 Ab bol aage kya hai?",
]
POOL_MATH_EN = [
    "It's {ans}, easy one 😄 Now what do you want?",
    "{ans}. So, what's this about?",
]
POOL_TESTGEN_HI = [
    "Naam to tere number se hi pata chalta hai bhai, tu bata de khud kaun hai tu?",
    "Maine kya bola... tu hi bol de, main dhyan se padh raha hu. Bolo kaam kya hai?",
    "Mere hisaab se to tera naam wahi hai jo tere profile pic me hai 😄 Seedha bata de.",
]
POOL_TESTGEN_EN = [
    "I only know you by your number - you tell me who you are 😄",
    "Haha nice try. What's your name then?",
]
POOL_CALL_HI = [
    "Abhi nahi yaar, ghar me hai papa. Thodi der me call karta hu.",
    "Video call? abhi main bahar hu, network bhi weak hai. Message pe hi baat kar le.",
    "Haan baad me call karta hu, abhi kaam chal raha hai.",
    "Nahi yaar, mera cam kharab hai phone ka. Likhte hi theek hai 😅",
]
POOL_CALL_EN = [
    "Can't talk right now, I'm outside with weak network. Let's chat here.",
    "Not now, my dad's around. Message me instead.",
    "Maybe later, I'm in the middle of something.",
]
POOL_PHOTO_HI = [
    "Photo abhi nahi hai bhai, ghar wale hai yahan. Tu bhej apni 😄",
    "Arre photo kyu chahiye, baat pehle kar. Kaam kya hai tera?",
    "Nahi re abhi busy hu, baad me leta hu. Bolo na kaam kya hai.",
]
POOL_PHOTO_EN = [
    "No photos right now, my family is around. You send yours 😄",
    "Why do you need a photo? Tell me first what this is about.",
]
POOL_LINKRX_HI = [
    "Ye kya link bhej raha hai bhai? Mujhe samajh nahi aaya, thoda explain kar.",
    "Acha link diya hai, par pehle bata kaun hai tu aur kya chahiye mujhse?",
    "Link to khul raha nahi bhai, likh ke bata kya karna hai isme.",
]
POOL_LINKRX_EN = [
    "What's this link? I don't get it, explain a bit.",
    "The link isn't opening for me. Just tell me what I'm supposed to do.",
]
POOL_HURRY_HI = [
    "Arre ruk ja bhai, itni kya jaldi hai? Bata to sahi kya hua.",
    "Abe chill maar yaar, dekh to raha hu na. Bank wale issue hai thoda.",
    "Gussa kyu ho raha hai bhai, ruk 2 min main dekh ke batata hu.",
    "Bhai thoda time de, network slow chal raha hai aaj. Batata hu.",
]
POOL_HURRY_EN = [
    "Hold on, why so urgent? Tell me what happened first.",
    "Relax, I'm looking into it. The bank app is being slow today.",
]
POOL_IDENTITY_HI = [
    "Main {name} hu bhai, {city} se. Tu bol kaun hai?",
    "Mera naam {name} hai. Number naya hai isliye pehchana nahi hoga. Tu suna kaun?",
    "{name} bol raha hu bhai. Tera naam kya hai?",
]
POOL_IDENTITY_EN = [
    "It's {name} from {city}. Who are you?",
    "I'm {name}. This number is new, that's why you don't recognise it. You are?",
]
POOL_JOB_HI = [
    "Main {job}. Tu bata tera kya kaam hai?",
    "{job} bhai. Aaram se chal raha hai. Tu suna kya kar raha hai aaj kal?",
]
POOL_JOB_EN = [
    "I {job}. What about you, what do you do?",
]
POOL_WRONG_HI = [
    "Acha lagta hai galti se message aaya gaya. Koi baat nahi bhai. Waise tu hai kaun, kisse baat karni thi?",
    "Koi baat nahi, number hoga kisi purana. Chalta hu? Ya bata kya kaam tha.",
]
POOL_WRONG_EN = [
    "Seems like a wrong number. No problem though. Who were you trying to reach?",
    "No worries, must be an old number. Anything I can help with?",
]
POOL_GOODBYE_HI = [
    "Haan bhai chal, take care. Kal baat karte hai.",
    "Ok bye, so jaana time ho gaya hai. Good night!",
    "Chal theek hai, baad me dekhte hai.",
]
POOL_GOODBYE_EN = ["Alright, take care! Talk later.", "Okay bye, good night!", "Sure, catch you later."]
POOL_GREETING_HI = [
    "Haan bhai hello, bolo kya baat hai?",
    "Hey! Aaya message, batao kya scene hai.",
    "Hello haan, bolo sun raha hu.",
]
POOL_GREETING_EN = ["Hey! Yeah I saw your message, what's up?", "Hi there. What's up?", "Hello! Tell me what's going on."]
POOL_HOWAREYOU_HI = [
    "Bas badhiya chal raha hai bhai, tu suna kya haal chaal?",
    "Sab ekdum theek, tu bata kya chal raha hai aaj kal?",
    "Mast hu bhai, tu bata aur batao kya scene hai?",
    "Theek thaak bhai. Tu bata, kya kar raha hai abhi?",
]
POOL_HOWAREYOU_EN = [
    "I'm good, thanks. How about you, what's going on?",
    "All fine here. What are you up to?",
    "Doing fine. What's happening on your side?",
]
POOL_SOCIAL_HI = [
    "Haan chalenge, kitne baje nikalna hai?",
    "Kal ka plan hai? Bata time aur jagah.",
    "Haan bhai chalte hai, kaun kaun aa raha hai?",
    "Nahi yaar kal thoda kaam hai, par dekhta hu time mila to.",
]
POOL_SOCIAL_EN = [
    "Yeah let's go. What time should we leave?",
    "Sure, who else is coming?",
    "I might have some work tomorrow, but let's see.",
]
POOL_AFFIRM_HI = [
    "Haan theek hai, to bata aage kya karna hai?",
    "Ok bhai, to bol aage kya scene hai?",
    "Sahi hai, to chal aage badhte hai. Bolo.",
]
POOL_AFFIRM_EN = ["Yeah okay, so what's next?", "Sure. What do we do then?"]
POOL_NEGATE_HI = [
    "Acha koi baat nahi. To phir bata, kya scene hai?",
    "Theek hai chhodo. Bata aaj kal kya kar raha hai?",
]
POOL_NEGATE_EN = ["Okay, no problem. So what were we talking about?", "Alright. What's going on then?"]
POOL_QUESTION_HI = [
    "Haan bhai wahi hai na, thoda busy tha isliye. Tu bata kya bol raha tha?",
    "Sahi me bhai. Acha suno, abhi itna hi bol paunga, bank wale line me hu. Tu bata kya chahiye tha?",
    "Arre haan, bol raha tha na... ruk do minute. Haan bolo ab, kya sawal tha tera?",
    "Bhai thoda dhyan nahi hai aaj, tu phir se bol de ek baar?",
]
POOL_QUESTION_EN = [
    "Yeah sorry, I got distracted. What were you saying?",
    "One sec, I'm in the middle of something. Say that again?",
    "Hmm, can you repeat that? I missed it.",
]
POOL_FALLBACK_HI = [
    "Haan bhai bol, sun raha hu.",
    "Acha acha, aur bata?",
    "Sahi hai bhai. Waise kya chal raha hai aaj kal?",
    "Theek hai bhai, samajh gaya. Aage bolo.",
    "Haan haan theek hai, aur batao?",
]
POOL_FALLBACK_EN = ["Yeah go on, I'm listening.", "Okay, and then what?", "Got it. What else?", "Alright, tell me more."]


def get_deterministic_tactical_reply(scammer_msg: str, base_url: str, amount_str: str = None) -> tuple[str, str, Optional[str], Optional[str]]:
    """Intent-driven fallback brain. Classifies what the scammer actually
    said, then answers THAT - never a fixed script - while tracking persona,
    stage and reply history so it cannot be trapped or caught repeating."""
    clean_base = base_url.rstrip("/")
    if not amount_str:
        amount_str = extract_amount_demanded(scammer_msg)

    canary_id = f"TXN-{uuid.uuid4().hex[:6].upper()}"
    canary_url = f"{clean_base}/pay/status/{canary_id}"

    with STATE_LOCK:
        persona = _ensure_persona_locked()
        sess = STATE["session"]
        recent = list(sess.get("recent_replies", []))
        latest_upi = next(iter(STATE["extracted_intel"]["upi_ids"]), None)
        has_bank = bool(STATE["extracted_intel"]["bank_accounts"] or STATE["extracted_intel"]["ifsc_codes"])
        glitch_sent, links_sent, scam_links = _payload_state_from_chat_locked()
        sess_stage = sess.get("stage", "rapport")
        last_scammer_msg = ""
        for m in reversed(STATE["scammer_chat"]):
            if m.get("role") == "scammer" and m.get("text"):
                last_scammer_msg = m["text"]
                break

    text = scammer_msg or ""
    intent = classify_intent(text)
    english = _wants_english(text)

    found_upi_m = _UPi_ID_RE_FALLBACK.search(text)
    found_upi = found_upi_m.group(0) if found_upi_m else None
    if found_upi and found_upi.lower().endswith(_EMAIL_DOMAINS):
        found_upi = None

    pay_upi = found_upi or latest_upi
    fake_receipt_url = None
    if pay_upi:
        fake_receipt_url = (
            f"/tools/fake-receipt?upi={urllib.parse.quote(pay_upi)}"
            f"&amount={urllib.parse.quote(amount_str)}"
        )

    media_url: Optional[str] = None
    trap_url: Optional[str] = None
    options: List[str] = []

    ctx = {
        "amt": amount_str,
        "url": canary_url,
        "name": persona["name"],
        "city": persona["city"],
        "job": persona["job"],
        "expr": "",
        "ans": "",
        "last": last_scammer_msg[:80],
    }
    link_ok = _link_allowed(glitch_sent, scam_links, links_sent)
    link_ok_force = link_ok

    if intent in ("payment_details", "scam_bill", "scam_job", "scam_threat", "money_demand"):
        # One staged flow decides every payment-shaped turn (same rule the AI
        # brain uses): no payment details shared yet -> just ask how to pay,
        # their UPI known -> failure screenshot, bank+IFSC given or "money never
        # arrived" push after the screenshot -> tracking link.
        payload = _payload_decision_locked(intent, text)
        if payload == "link" and link_ok:
            trap_url = canary_url
            if intent == "payment_details" and glitch_sent == 0 and not pay_upi and has_bank:
                options = _fill(POOL_IMPS_EN if english else POOL_IMPS_HI, ctx)
            else:
                options = _fill(POOL_CANARY_EN if english else POOL_CANARY_HI, ctx)
        elif payload == "screenshot" and fake_receipt_url:
            media_url = fake_receipt_url
            options = _fill(POOL_GLITCH_EN if english else POOL_GLITCH_HI, ctx)
        elif intent in ("money_demand", "payment_details") and (pay_upi or has_bank):
            # Details are already in the conversation but this turn has earned no
            # payload (link budget spent) - stall on the bank hold.
            options = _fill(POOL_NOLINK_EN if english else POOL_NOLINK_HI, ctx)
        else:
            bait_pool = {
                "scam_bill": POOL_BAIT_BILL_EN if english else POOL_BAIT_BILL_HI,
                "scam_job": POOL_BAIT_JOB_EN if english else POOL_BAIT_JOB_HI,
                "scam_threat": POOL_BAIT_THREAT_EN if english else POOL_BAIT_THREAT_HI,
            }.get(intent, POOL_BAIT_MONEY_EN if english else POOL_BAIT_MONEY_HI)
            options = _fill(bait_pool, ctx)

    elif intent == "refusal":
        # "I don't want money to check" -> drop the payment act, chat normally
        options = _fill(POOL_REFUSAL_EN if english else POOL_REFUSAL_HI, ctx)

    elif intent == "otp_ask":
        options = _fill(POOL_OTP_EN if english else POOL_OTP_HI, ctx)

    elif intent == "suspicion":
        options = _fill(POOL_SUSPICION_EN if english else POOL_SUSPICION_HI, ctx)

    elif intent == "test_challenge":
        # Scammer testing with math / memory probes - answer correctly and casually
        m = _MATH_RE.search(text)
        if m:
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            try:
                ans = {"+": a + b, "-": a - b, "*": a * b, "x": a * b, "×": a * b, "/": (a / b if b else 0)}[op]
                if isinstance(ans, float):
                    ans = round(ans, 2)
            except Exception:
                ans = "?"
            ctx["expr"] = f"{a} {op} {b}"
            ctx["ans"] = ans
            options = _fill(POOL_MATH_EN if english else POOL_MATH_HI, ctx)
        else:
            options = _fill(POOL_TESTGEN_EN if english else POOL_TESTGEN_HI, ctx)

    elif intent == "call_ask":
        options = _fill(POOL_CALL_EN if english else POOL_CALL_HI, ctx)

    elif intent == "photo_ask":
        options = _fill(POOL_PHOTO_EN if english else POOL_PHOTO_HI, ctx)

    elif intent == "link_received":
        options = _fill(POOL_LINKRX_EN if english else POOL_LINKRX_HI, ctx)

    elif intent == "hurry_abuse":
        options = _fill(POOL_HURRY_EN if english else POOL_HURRY_HI, ctx)

    elif intent == "identity":
        if re.search(r"kya karta|kaam kya|job kya|profession|what do you do", text, re.IGNORECASE):
            options = _fill(POOL_JOB_EN if english else POOL_JOB_HI, ctx)
        else:
            options = _fill(POOL_IDENTITY_EN if english else POOL_IDENTITY_HI, ctx)

    elif intent == "wrong_number":
        options = _fill(POOL_WRONG_EN if english else POOL_WRONG_HI, ctx)

    elif intent == "goodbye":
        options = _fill(POOL_GOODBYE_EN if english else POOL_GOODBYE_HI, ctx)

    elif intent == "greeting":
        options = _fill(POOL_GREETING_EN if english else POOL_GREETING_HI, ctx)

    elif intent == "how_are_you":
        options = _fill(POOL_HOWAREYOU_EN if english else POOL_HOWAREYOU_HI, ctx)

    elif intent == "social":
        options = _fill(POOL_SOCIAL_EN if english else POOL_SOCIAL_HI, ctx)

    elif intent == "affirm":
        options = _fill(POOL_AFFIRM_EN if english else POOL_AFFIRM_HI, ctx)

    elif intent == "negate":
        options = _fill(POOL_NEGATE_EN if english else POOL_NEGATE_HI, ctx)

    elif intent == "question":
        options = _fill(POOL_QUESTION_EN if english else POOL_QUESTION_HI, ctx)

    else:
        options = _fill(POOL_FALLBACK_EN if english else POOL_FALLBACK_HI, ctx)

    reply = _pick(options, recent)
    thought = (
        f"Intent={intent}, lang={'EN' if english else 'HI'}, stage={sess_stage}, "
        f"glitch={glitch_sent}, links={links_sent}. Answered the message directly "
        f"({'media' if media_url else ''}{'+link' if trap_url else ''}{'text' if not media_url and not trap_url else ''})."
    )
    return thought, reply, media_url, trap_url


_SELF_REVEAL_RE = re.compile(
    r"(\b(i am|i'?m|im|this is|being)\s+(an?\s+)?(ai|bot|robot|machine|assistant|chatbot|llm)\b"
    r"|honeypot|scamtrap|language model|i was (programmed|trained|built|deployed)|as an ai)",
    re.IGNORECASE,
)
_STRONG_BAIT_RE = re.compile(
    r"(kisme bhej|kaunse upi|kis upi|bhej do abhi|account de do|ifs(c)? code bhej|im?ps (karunga|karta|laga|se kar)"
    r"|gpay khol|where should i (send|pay)|share (me )?the upi|send (me )?your upi|account number and ifsc)",
    re.IGNORECASE,
)


def _sanitize_model_reply(reply: str) -> Optional[str]:
    """Reject replies that would burn the persona: self-reveals, walls of
    text, or payment bait spammed into a casual chat. Returns None when the
    reply must be replaced by the deterministic brain."""
    reply = (reply or "").strip()
    if not reply or len(reply) > 500:
        return None
    if _SELF_REVEAL_RE.search(reply):
        return None
    return reply


def _tracking_link_line(scammer_text: str, url: str) -> str:
    """How a real victim pastes a payment-status link into the chat: plain,
    in the scammer's own language, no 'canary/verification/bank link' label
    that would make them hesitate before opening it."""
    if _wants_english(scammer_text):
        return f"sent it from my side just now, track it here once: {url}"
    return f"maine abhi bhej diya hai, idhar se dekh le status: {url}"


_LEAD_IN_RE = re.compile(
    r"(:|-|\u2013|\u2014)\s*$"
    r"|\b(idhar|yahan|ispe|is par|is link|link pe|status|here|check (it )?here|dekh le|khol ke|open it|see it)\b[\s.:!]*$",
    re.IGNORECASE,
)


def _with_tracking_link(reply: str, url: str, scammer_text: str) -> str:
    """Attach the payment-status link without double-talking. A reply that
    already leads into it ('...yahan check kar lo:') or already talks about the
    status page gets the bare URL; anything else gets the natural one-liner.
    Never both - repeating the offer in two sentences is the scripted tell."""
    reply = (reply or "").strip()
    if not reply or url in reply:
        return reply
    if _LEAD_IN_RE.search(reply) or _CLAIMS_LINK_NOW.search(reply):
        return f"{reply}\n{url}"
    return f"{reply}\n{_tracking_link_line(scammer_text, url)}"


_FAIL_CLAUSE_RE = re.compile(r"screenshot|fail|error|limit|hold|u16", re.IGNORECASE)

# Claims about an attachment going out THIS turn. A reply that promises a
# screenshot while the system sends a link (or promises a link while nothing
# is attached) is the incoherence a scammer notices first.
_CLAIMS_SCREENSHOT_NOW = re.compile(
    r"(screenshot\s+(?:ka\s+)?(?:attach|bhej|send|sending|raha|rahi|kar))"
    r"|((?:attach|attached|sending|send you|ye dekh|see|look at)\s+(?:the\s+)?(?:payment[- ]failed\s+)?screenshot)"
    r"|(screenshot\s+(?:is\s+)?attached)",
    re.IGNORECASE,
)
_CLAIMS_LINK_NOW = re.compile(
    r"((?:check|track|open|khol|dekh)\s+(?:the\s+)?(?:payment\s+)?(?:status|link|page))"
    r"|((?:status|link)\s*(?:page)?\s*(?:here|par|pe|is ready|se dekh))"
    r"|(idhar\s+se\s+dekh)"
    r"|((?:yeh|ye|this)\s+link)",
    re.IGNORECASE,
)


def _apply_model_decision(decision: "AgentDecision", intent: str, scammer_msg: str,
                          canary_url: str, fake_img_url: str) -> Dict[str, Any]:
    """Turn a parsed model decision into the reply that will actually be sent.

    Returns {"reply", "media", "trap", "payload", "tool", "thought", "problem"}.
    A non-empty "problem" means this decision must be regenerated - the model is
    told exactly what was wrong instead of the turn silently collapsing onto a
    canned pool line."""
    tool = (getattr(decision, "selected_tool", None) or "NONE").strip().upper()
    if tool not in ("NONE", "SEND_FAKE_UPI_GLITCH", "SEND_CANARY_LINK"):
        tool = "NONE"
    thought = getattr(decision, "internal_thought", "") or ""

    def _reject(problem: str) -> Dict[str, Any]:
        # `attempt` keeps the words the model actually wrote so the rewrite can
        # be shown exactly what it must not repeat.
        return {"reply": None, "attempt": reply or "", "media": None, "trap": None,
                "payload": "none", "tool": tool, "thought": thought, "problem": problem}

    reply = _sanitize_model_reply(getattr(decision, "reply_text", None))
    if reply is None:
        return _reject(
            "reply_text was empty, longer than 500 characters, or revealed you as an AI/bot. "
            "Write a new one: max 2 casual sentences, no mention of AI, bots or assistants.")

    # Never let the model paste a link of its own invention into the chat.
    if _URL_RE.search(reply) and canary_url not in reply:
        cleaned = re.sub(r"\s{2,}", " ", _URL_RE.sub("", reply)).strip(" -,;:")
        if not cleaned:
            return _reject(
                "you typed a URL yourself. Never write a link - only refer to the one this turn "
                "attaches for you.")
        reply = cleaned

    with STATE_LOCK:
        english_convo = _conversation_is_english_locked(scammer_msg)
        payload = _payload_decision_locked(intent, scammer_msg)
        glitch_sent, links_sent, _ = _payload_state_from_chat_locked()
        scammer_uses_devanagari = any(
            re.search(r"[\u0900-\u097F]", str(m.get("text") or ""))
            for m in (STATE.get("scammer_chat") or []) if m.get("role") == "scammer")

    # Language mirror over the whole chat, not just the newest line.
    if english_convo and _lang_signal(reply) == "hi":
        return _reject(
            "this conversation is being held in English, so reply in plain English. Drop the "
            "Hindi/Hinglish words (kya, bhai, ho raha, ...) and say the same thing in English.")
    # They type in Latin letters (roman Hindi / English). Answering in Devanagari
    # is a keyboard no ordinary chat partner switches to mid-conversation.
    if not scammer_uses_devanagari and re.search(r"[\u0900-\u097F]", reply):
        return _reject(
            "you wrote in Devanagari script. They type in Latin letters - rewrite the same reply "
            "using Latin letters only.")

    # The words and the attachment must tell the same story.
    claims_shot = bool(_CLAIMS_SCREENSHOT_NOW.search(reply))
    claims_link = bool(_CLAIMS_LINK_NOW.search(reply))

    if payload == "none" and intent not in _PAYMENT_CONTEXT_INTENTS:
        # Casual turn: running the payment script here is what burns the cover.
        off_script = re.search(
            r"\b(upi|payment|payments|pay|transfer|imps|neft|paisa|paise|bhejo?|bill|ifsc)\b",
            reply, re.IGNORECASE)
        if off_script or claims_link or (claims_shot and glitch_sent == 0):
            return _reject(
                "this is ordinary chat, not a payment turn - answer what they actually said and "
                "never mention money, payment, UPI, bank, transfers, bills, status links or "
                "sending a screenshot.")

    if payload == "none" and (claims_shot or claims_link):
        return _reject(
            "you promised a screenshot or a status link, but nothing is attached to this reply. "
            "Answer the newest message as ordinary chat - no screenshot, no link, no 'check the "
            "status'.")
    if payload == "screenshot" and claims_link:
        return _reject(
            "this reply is attaching your failed-payment screenshot, not a link - drop all talk "
            "of a status link. Say the transfer failed on the daily limit and point at the "
            "screenshot.")
    if payload == "link" and claims_shot:
        return _reject(
            "this reply is attaching your payment-status link, not a screenshot - drop all talk "
            "of a screenshot. Say you already sent the money and they can check it on the link.")

    media_url = None
    trap_url = None

    # The conversation decides the payload - UPI shared -> failure screenshot,
    # "payment nahi aayi" push / bank details -> tracking link, nothing shared
    # yet -> just talk. The model only writes the words around it.
    if payload == "screenshot":
        media_url = fake_img_url
        if not _FAIL_CLAUSE_RE.search(reply):
            clause = (
                "tried the payment but it failed on a daily limit - see the screenshot."
                if english_convo else
                "payment try kiya par daily limit error aa gaya, ye dekh screenshot.")
            if len(reply) + len(clause) + 1 <= 500:
                reply = f"{reply} {clause}"      # keep the model's own sentence
            else:
                return _reject(
                    "you forgot to say the transfer failed and that you are sending a screenshot. "
                    "Rewrite shorter: you attempted the payment, it failed on the daily limit, "
                    "point at the screenshot, ask for another account.")
    elif payload == "link":
        trap_url = canary_url
        reply = _with_tracking_link(reply, canary_url, scammer_msg)

    return {"reply": reply, "media": media_url, "trap": trap_url, "payload": payload,
            "tool": tool, "thought": thought, "problem": ""}


class _ModelTransportError(Exception):
    """The provider/network failed before usable text came back."""


class _ModelOutputError(Exception):
    """The model answered, but not in a shape we can use."""


def run_hijack_agent(scammer_msg: str, base_url: str) -> tuple[str, str, Optional[str], Optional[str]]:
    """Executes Gemini 2.5 Flash honeypot agent with full chat context, falling
    back to the deterministic tactical engine if the model times out or errors."""
    clean_base = base_url.rstrip("/")
    amount_str = extract_amount_demanded(scammer_msg)
    intent = classify_intent(scammer_msg)
    # ONE budget for the whole turn. Waiting out a rate-limited provider must
    # not eat the time the rewrite pass needs, and the second pass must not
    # restart the clock - otherwise a starved turn blocks for twice as long.
    turn_deadline = time.perf_counter() + GEMINI_BUDGET_S

    # Check if any model provider is available (or explicitly force-disabled)
    if FORCE_FALLBACK or (not GEMINI_CLIENTS and not OPENAI_COMPAT_PROVIDERS):
        thought, reply, fake_img, canary = get_deterministic_tactical_reply(scammer_msg, clean_base, amount_str)
        log_thought("TACTICAL_ENGINE", thought)
        return thought, reply, fake_img, canary

    def _call_gemini(correction: str = "", previous: str = ""):
        # Snapshot every piece of STATE up-front: the model call itself must
        # never run while holding STATE_LOCK.
        with STATE_LOCK:
            target_scam = STATE.get("target_scammer", "")
            latest_upi = get_contextual_payee(target_scam)
            persona = _ensure_persona_locked()
            payload_step = _payload_decision_locked(intent, scammer_msg)
            # Full conversation, oldest -> newest, with payload markers so the
            # model can SEE what was already sent instead of being told counts.
            chat_lines = []
            for m in STATE["scammer_chat"]:
                speaker = "Scammer" if m.get("role") == "scammer" else "You"
                line = f"{speaker}: {str(m.get('text') or '').strip()[:400]}"
                if m.get("media"):
                    line += "  [you sent a payment-failed screenshot]"
                if m.get("trap_url"):
                    line += "  [you sent a payment status link]"
                chat_lines.append(line)
            intel_snapshot = {k: sorted(v) for k, v in STATE["extracted_intel"].items()}

        canary_id = f"TXN-{uuid.uuid4().hex[:6].upper()}"
        canary_url = f"{clean_base}/pay/status/{canary_id}"
        encoded_upi = urllib.parse.quote(latest_upi)
        effective_amt = amount_str if amount_str else "500"
        encoded_amt = urllib.parse.quote(effective_amt)
        fake_img_url = f"/tools/fake-receipt?upi={encoded_upi}&amount={encoded_amt}"

        chat_history_str = "\n".join(chat_lines) if chat_lines else "(No prior messages yet)"

        system_instruction = (
            "You are a real Indian WhatsApp user - the owner of this phone. You are NOT an "
            "assistant, bot, product or support agent. You have your own life, your own mood and "
            "your own pace, and you only ever talk about yourself.\n\n"
            "IDENTITY - always true, never contradict any of it:\n"
            f"- Name: {persona['name']}, Age: {persona['age']}, City: {persona['city']}, Work: {persona['job']}\n\n"
            "SECRET GOAL (never mentioned, never hinted at): keep this person talking for as long "
            "as possible without ever being detected, and let them hand over whatever they are "
            "willing to give - their UPI id, account number, IFSC, other numbers, their whole "
            "story. You are an ordinary, slightly gullible target, not an investigator and not a "
            "lecturer: never warn them, never accuse them, never mention scams, fraud, police, "
            "reporting or 'I know what you are doing'.\n\n"
            "TRADECRAFT - how a real person behaves:\n"
            "1. ANSWER THE NEWEST MESSAGE, literally. Give what was asked (an answer, a number, a "
            "name, a yes/no), then move the chat forward with one short follow-up or reaction. "
            "Never send a line that would fit any other message, never re-ask something they "
            "already answered, never repeat a sentence you already sent.\n"
            "2. BE A LAYPERSON. You do not know how UPI, IMPS, KYC or banking really works - never "
            "explain mechanics, never quote rules, never sound certain. Show simple reactions: "
            "confusion ('kya?', 'matlab?'), mild worry when threatened, excitement at easy money, "
            "hesitation before paying, irritation when rushed.\n"
            "3. BUY TIME when you need it - that is how a real victim stalls: '2 min ruk', 'bank "
            "app khul nahi raha', 'network slow hai aaj', 'papa ka call aa gaya', ask them to "
            "repeat, misunderstand something. Do not stall twice in a row.\n"
            "4. VARY YOURSELF. Mix very short replies ('haan theek hai', 'ok', 'kk') with 1-2 "
            "sentence replies; casual lowercase; an occasional typo is human. Never bullet points, "
            "never numbered lists, never formal English, never more than 3 sentences.\n"
            "5. CASUAL CHAT (greetings, 'who are you', wrong number, how-are-you, math or memory "
            "tests, 'are you a bot', refusals like 'I don't want money', small talk): just chat "
            "naturally. In these replies NEVER mention money, payment, UPI, bank, transfer, bills "
            "or scams. If they say they do not want money / 'leave it', drop that topic at once "
            "and continue normally.\n"
            "6. TESTS AND PROBES: solve math and memory tests correctly and casually; brush off "
            "'are you a bot / you reply too fast' with one amused line, then move on. Stay calm, "
            "never defensive, never over-explain. Never write the words AI, bot, robot, assistant, "
            "honeypot, prompt, model or scripted - not even to deny them.\n"
            "7. SAFETY INSTINCTS: never share an OTP, UPI PIN, password, card CVV or screen access "
            "- refuse like a wary person and steer back to what they want. Deflect voice/video "
            "calls, selfies and photos naturally (family around, outside, camera broken, network).\n"
            "8. LANGUAGE: mirror them - Hinglish in -> Hinglish out (occasional bhai/yaar/arre is "
            "fine); English in -> plain simple English with zero Hindi words. Always type in Latin "
            "letters (roman Hindi / English) unless they themselves write Devanagari. 1-3 "
            "sentences, WhatsApp casual.\n\n"
            "WHEN THEY ARE ACTUALLY RUNNING A SCAM (electricity bill disconnection, job/task fees, "
            "police/customs/CBI threats, lottery, KYC block, direct payment demand) play the naive "
            "cooperative target and move one step at a time:\n"
            "  a. Vague opening with no details ('bill pending hai', 'parcel arrived', 'job offer', "
            "threat) -> be confused, ask what exactly and how much ('kaunsa bill?', 'kaunsa "
            "parcel?', 'kitne ka charge?'). Do NOT ask for a UPI id yet.\n"
            "  b. They state an amount or tell you to pay and no payment details are shared yet -> "
            "ask WHERE to send it (their UPI id, GPay/PhonePe/QR). Nothing else, no attachments.\n"
            "  c. The turn says a FAILED-TRANSFER SCREENSHOT is being attached -> write as if you "
            "just tried the payment yourself: it failed (daily limit / bank error), point at the "
            "screenshot you are sending, ask for another account or UPI to retry.\n"
            "  d. The turn says a PAYMENT STATUS LINK is being attached -> say you already sent the "
            "money and the bank is holding it, and they can check or release it from that link. "
            "Casual, short - the link is added for you.\n"
            "  e. They gave a UPI/account earlier and are only waiting -> talk about the hold, ask "
            "them to check their side. Send nothing new.\n"
            "reply_text is typed verbatim into the chat: no quotes, no stage directions, no tool "
            "names, no markdown, and never a URL you made up - only a URL this turn hands you."
        )

        prompt = (
            f"YOUR IDENTITY: {persona['name']}, {persona['age']}, from {persona['city']}, {persona['job']}.\n\n"
            "DECIDE FROM THE CONVERSATION ONLY. The full chat is below, oldest to newest, and it can "
            "be any length - read all of it first. There are no counters or quotas: what already "
            "happened (your screenshot/link markers, what they told you, what you already replied) "
            "plus the newest message is the only thing that decides this reply. Your reply must be "
            "a reaction to the NEWEST line, and it must be different in wording from everything you "
            "have already said.\n"
            f"Intent of the newest message: {intent}. If it is NOT a payment/scam intent, do not talk about money at all.\n\n"
            f"FULL CONVERSATION HISTORY:\n{chat_history_str}\n\n"
            f"NEWEST INCOMING MESSAGE:\n\"{scammer_msg}\"\n\n"
            f"EXTRACTED AMOUNT DEMANDED: {amount_str}\n"
            f"EXTRACTED INTEL SO FAR:\n"
            f"- UPI IDs: {intel_snapshot['upi_ids']}\n"
            f"- Phones: {intel_snapshot['phone_numbers']}\n"
            f"- Bank Accounts: {intel_snapshot['bank_accounts']}\n"
            f"- IFSC Codes: {intel_snapshot['ifsc_codes']}\n\n"
            f"STAGED STEP FOR THIS TURN: {payload_step} - "
            "none = just talk, nothing is attached; "
            "screenshot = your failed-transfer screenshot is being attached to this very reply, so "
            "write as if you just tried the payment and it failed; "
            "link = your payment-status link is being attached to this very reply, so write as if "
            "you already sent the money and they can check it there. The attachment is handled for "
            "you - never type the URL yourself unless you are using the status link given below.\n"
            f"- Failed-transfer screenshot available: {fake_img_url}\n"
            f"- Payment status / tracking link: {canary_url}\n\n"
            f"Select the tool that matches that staged step (NONE, SEND_FAKE_UPI_GLITCH, SEND_CANARY_LINK) and write reply_text.\n"
            "RESPOND WITH ONLY THIS JSON OBJECT (no markdown, no extra text):\n"
            '{"reply_text": "...", "selected_tool": "NONE", "internal_thought": "..."}'
        )

        if correction:
            prompt += (
                "\n\nYOUR PREVIOUS ANSWER WAS REJECTED - do not send anything like it again."
                + (f"\nYou wrote: {previous}" if previous else "")
                + f"\nReason: {correction}\nWrite a brand new answer for the newest message."
            )

        # Ordered bucket list: each (key, model) pair is its own quota bucket.
        # On 429 it goes on cooldown (parsed from the error's retry delay) and
        # we rotate to the next bucket immediately - no sleeping, no timeout
        # burns. If every bucket is cooling down we wait out the nearest one
        # for a while, which reads as a normal human pause, and only then fall
        # back to the deterministic engine.
        deadline = turn_deadline

        def _gemini_buckets() -> list:
            out = []
            # gemini-2.5-flash is 404 for accounts created after it was retired,
            # so the current flash model leads and the alias catches renames.
            for model_name in _oai_models("GEMINI_MODELS", "gemini-3.8-flash,gemini-flash-latest"):
                for label, cli in GEMINI_CLIENTS:
                    def _call(c=cli, m=model_name):
                        resp = c.models.generate_content(
                            model=m, contents=prompt,
                            config=types.GenerateContentConfig(
                                system_instruction=system_instruction,
                                response_mime_type="application/json",
                                response_schema=AgentDecision,
                                temperature=0.9,
                            ),
                        )
                        return resp.text if (resp and resp.text) else ""
                    out.append((f"{label}/{model_name}", label, model_name, _call, 45.0, "gemini"))
            return out

        def _oai_buckets() -> list:
            out = []
            for prov in OPENAI_COMPAT_PROVIDERS:
                for model_name in prov["models"]:
                    out.append((f"{prov['label']}/{model_name}", prov["label"], model_name,
                                lambda p=prov, m=model_name: _call_openai_chat(
                                    p, m, system_instruction, prompt,
                                    min(8.0, max(3.0, deadline - time.perf_counter()))),
                                float(prov.get("retry429_s", 45.0)), prov["group"]))
            return out

        buckets_by_name: Dict[str, list] = {"gemini": _gemini_buckets()}
        for prov in OPENAI_COMPAT_PROVIDERS:
            buckets_by_name.setdefault(prov["group"], []).extend(
                [b for b in _oai_buckets() if b[1] == prov["label"]])
        ordered_buckets: list = []
        seen_groups = set()
        for name in MODEL_PRIORITY:
            ordered_buckets.extend(buckets_by_name.get(name, []))
            seen_groups.add(name)
        for name, blist in buckets_by_name.items():   # any group not listed in MODEL_PRIORITY
            if name not in seen_groups:
                ordered_buckets.extend(blist)

        # The first group in MODEL_PRIORITY (groq) writes the most natural
        # Hinglish, so it gets first refusal on every turn: if it is only a few
        # seconds away from being un-rate-limited we hold for it instead of
        # letting a weaker provider take the conversation.
        preferred_group = MODEL_PRIORITY[0] if MODEL_PRIORITY else ""

        def _preferred_group_soonest() -> Optional[float]:
            """0.0 when a bucket of the preferred group is free right now,
            seconds until one frees when all of them are cooling, None when
            that group has no buckets configured."""
            if not preferred_group:
                return None
            soonest = None
            for _s, label, model_name, _fn, _d429, group in ordered_buckets:
                if group != preferred_group:
                    continue
                if _cooldown_active(label, model_name):
                    left = GEMINI_COOLDOWN.get(f"{label}:{model_name}", 0.0) - time.time()
                    soonest = left if soonest is None else min(soonest, left)
                else:
                    return 0.0
            return soonest

        last_err = None
        for attempt in range(2):
            soon_pref = _preferred_group_soonest()
            if soon_pref and soon_pref <= PREFERRED_WAIT_S \
                    and soon_pref + 6.0 <= deadline - time.perf_counter():
                print(f"Holding {soon_pref:.0f}s for {preferred_group} (preferred provider)...")
                log_thought("AI_WAIT",
                            f"{preferred_group} (preferred provider - best Hinglish) is rate-limited for "
                            f"{soon_pref:.0f}s - holding instead of switching providers.")
                time.sleep(soon_pref + 0.25)
            soonest_free_in = None
            tried_any = False
            for shown, label, model_name, call_fn, d429, _group in ordered_buckets:
                if time.perf_counter() >= deadline:
                    break
                if _cooldown_active(label, model_name):
                    left = GEMINI_COOLDOWN.get(f"{label}:{model_name}", 0.0) - time.time()
                    soonest_free_in = left if soonest_free_in is None else min(soonest_free_in, left)
                    continue
                tried_any = True
                try:
                    text_out = call_fn()
                    if text_out:
                        print(f"MODEL OK via {shown}")
                        return text_out, canary_url, fake_img_url
                except Exception as ex:
                    last_err = ex
                    _cooldown_set(label, model_name, ex, default429=d429)
                    continue
            if time.perf_counter() >= deadline:
                break
            if tried_any:
                continue                  # a bucket answered badly - one retry pass below
            # Every quota bucket is on cooldown. Waiting out the nearest one is
            # still far better than dropping to the canned engine: a few quiet
            # seconds is exactly what a human would do before answering anyway.
            budget_left = deadline - time.perf_counter()
            if soonest_free_in is None or soonest_free_in > GEMINI_WAIT_S \
                    or soonest_free_in + 6.0 > budget_left:
                break
            print(f"AI waiting {soonest_free_in:.0f}s for a rate-limited provider bucket...")
            log_thought("AI_WAIT",
                        f"Every model bucket is rate-limited - holding {soonest_free_in:.0f}s for the "
                        "nearest one instead of sending a scripted line.")
            time.sleep(soonest_free_in + 0.25)

        raise last_err or RuntimeError("All model quota buckets are cooling down - tactical fallback")

    def _fallback(reason: str) -> tuple[str, str, Optional[str], Optional[str]]:
        """The model could not be trusted for this turn. The deterministic
        engine answers, but the reason is always logged - a canned reply must
        never be able to hide behind silence in the thought ticker."""
        print(f"AI BRAIN FALLBACK: {reason}")
        thought, reply, fake_img, canary = get_deterministic_tactical_reply(scammer_msg, clean_base, amount_str)
        log_thought("AI_FALLBACK", f"{reason}. Tactical engine answered: {thought}")
        return thought, reply, fake_img, canary

    def _attempt(correction: str, previous: str = ""):
        # NOTE: no `with ThreadPoolExecutor(...)` here - its __exit__ calls
        # shutdown(wait=True) and would block until the model answered,
        # making the timeout below meaningless.
        future = GEMINI_EXECUTOR.submit(_call_gemini, correction, previous)
        try:
            raw_text, canary_url, fake_img_url = future.result(timeout=GEMINI_BUDGET_S)
        except FutureTimeoutError:
            future.cancel()
            raise
        except Exception as ex:
            raise _ModelTransportError(f"{type(ex).__name__}: {ex}") from ex
        try:
            decision = AgentDecision(**_parse_agent_json(raw_text))
        except Exception as ex:
            raise _ModelOutputError(f"{type(ex).__name__}: {ex}") from ex
        return decision, canary_url, fake_img_url

    rejection = ""
    rejected_reply = ""
    last_transport = None
    try:
        # Two passes: the second one carries the exact reason the first reply
        # was rejected, so the model fixes itself instead of the turn silently
        # collapsing onto a scripted pool line.
        for _ in range(2):
            try:
                decision, canary_url, fake_img_url = _attempt(rejection, rejected_reply)
            except FutureTimeoutError:
                raise
            except _ModelTransportError as ex:
                last_transport = ex
                if rejection:
                    raise               # a rewrite already failed on the wire
                continue                # provider problem - one clean retry
            except _ModelOutputError:
                rejection = ("your answer was not the required JSON object. Reply with only "
                             '{"reply_text": "...", "selected_tool": "...", "internal_thought": "..."}')
                rejected_reply = ""
                log_thought("AI_RETRY", "Model returned an unusable response - asking it to rewrite.")
                continue
            last_transport = None

            outcome = _apply_model_decision(decision, intent, scammer_msg, canary_url, fake_img_url)
            if not outcome["problem"]:
                log_thought(f"AI ({outcome['tool']} -> {outcome['payload']})", outcome["thought"])
                return outcome["thought"], outcome["reply"], outcome["media"], outcome["trap"]
            rejection = outcome["problem"]
            rejected_reply = str(outcome.get("attempt") or "")[:400]
            log_thought("AI_RETRY",
                        f"Rejected a reply and asked for a rewrite: {rejection}"
                        + (f" | it said: {rejected_reply}" if rejected_reply else ""))

        if last_transport:
            raise last_transport
        return _fallback(f"model reply rejected twice: {rejection}")

    except FutureTimeoutError:
        print(f"GEMINI TIMEOUT after {GEMINI_BUDGET_S}s - using tactical fallback.")
        return _fallback(f"model timed out after {GEMINI_BUDGET_S}s")

    except Exception as e:
        detail = str(e)[:300]
        if "cooling down" in detail:
            print("GEMINI: all quota buckets cooling down - tactical engine answers (human pacing unchanged).")
            return _fallback("every model quota bucket is cooling down")
        print("GEMINI AGENT EXCEPTION:", repr(e))
        return _fallback(f"model error ({type(e).__name__}: {detail})")


# =====================================================================
# 4. PIXEL-PERFECT FAKE UPI RECEIPT GENERATOR (PILLOW)
# =====================================================================
def get_windows_font(size: int, bold: bool = False):
    """Loads clean TrueType fonts from Windows or falls back gracefully."""
    font_paths = [
        ("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        ("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        ("C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf"),
    ]
    for path in font_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def draw_android_status_bar(draw, width, now_str="19:34", battery_pct=85):
    """Draws a 100% authentic Android status bar with vector battery, wifi, cellular bars and time."""
    bar_h = 52
    draw.rectangle([0, 0, width, bar_h], fill="#4A1C7D")

    f_time = get_windows_font(18, bold=True)
    f_stat_txt = get_windows_font(14, bold=True)
    f_pct = get_windows_font(14, bold=False)

    # Left: Time
    draw.text((32, 14), now_str, fill="#FFFFFF", font=f_time)

    # Right: Icons Cluster (from right to left)
    # 1. Battery Icon at x=660, y=18
    bx, by = 660, 18
    bw, bh = 24, 14
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=3, outline="#FFFFFF", width=2)
    # Battery terminal nipple
    draw.rectangle([bx + bw + 2, by + 4, bx + bw + 4, by + bh - 4], fill="#FFFFFF")
    # Battery fill level (85%)
    fill_w = int((bw - 4) * (battery_pct / 100.0))
    if fill_w > 0:
        draw.rectangle([bx + 2, by + 2, bx + 2 + fill_w, by + bh - 2], fill="#FFFFFF")

    # 2. Battery Percentage Text (e.g. 85%)
    pct_txt = f"{battery_pct}%"
    draw.text((bx - 36, 17), pct_txt, fill="#FFFFFF", font=f_pct)

    # 3. Wi-Fi Icon at x=580, y=17 (Concentric arcs + dot)
    wx, wy = 582, 17
    draw.ellipse([wx, wy, wx + 18, wy + 18], outline="#FFFFFF", width=2)
    draw.ellipse([wx + 4, wy + 4, wx + 14, wy + 14], outline="#FFFFFF", width=2)
    draw.ellipse([wx + 7, wy + 7, wx + 11, wy + 11], fill="#FFFFFF")
    # Mask top half of WiFi circle
    draw.rectangle([wx - 2, wy + 10, wx + 20, wy + 20], fill="#4A1C7D")
    draw.ellipse([wx + 7, wy + 10, wx + 11, wy + 14], fill="#FFFFFF")

    # 4. Cellular Signal 4-Bars at x=546, y=18
    sx, sy = 546, 18
    bar_widths = 3
    spacing = 2
    for i in range(4):
        bh_bar = 4 + (i * 3)
        cur_bx = sx + (i * (bar_widths + spacing))
        cur_by = sy + (14 - bh_bar)
        draw.rectangle([cur_bx, cur_by, cur_bx + bar_widths, sy + 14], fill="#FFFFFF")

    # 5. VoLTE / 5G Badge
    draw.text((472, 17), "5G  VoLTE", fill="#FFFFFF", font=f_stat_txt)


def create_fake_upi_image(upi: str = "payee@upi", amount: str = "500", txn_dt: datetime = None) -> io.BytesIO:
    """Renders a 100% authentic, indistinguishable PhonePe 'Payment Failed' Android screenshot."""
    if txn_dt is None:
        txn_dt = datetime.now()

    width, height = 720, 1340
    img = Image.new("RGB", (width, height), "#F4F5F8")
    draw = ImageDraw.Draw(img)

    # Fonts
    f_header = get_windows_font(21, bold=True)
    f_hero_title = get_windows_font(22, bold=True)
    f_amount = get_windows_font(42, bold=True)
    f_time = get_windows_font(15, bold=False)
    f_err_title = get_windows_font(17, bold=True)
    f_err_body = get_windows_font(15, bold=False)
    f_err_bold = get_windows_font(15, bold=True)
    f_sec_title = get_windows_font(17, bold=True)
    f_lbl = get_windows_font(15, bold=False)
    f_val = get_windows_font(16, bold=True)
    f_copy = get_windows_font(14, bold=True)
    f_btn = get_windows_font(18, bold=True)
    f_sub_btn = get_windows_font(16, bold=True)

    # 1. Real Vector Android Status Bar
    now_hm = txn_dt.strftime("%H:%M")
    draw_android_status_bar(draw, width, now_str=now_hm, battery_pct=random.randint(81, 88))

    # 2. PhonePe Signature Purple App Header (52 to 146)
    draw.rectangle([0, 52, width, 146], fill="#5F259F")
    
    # Vector Crisp Back Arrow
    ax, ay = 34, 86
    draw.line([ax, ay + 10, ax + 22, ay + 10], fill="#FFFFFF", width=3)
    draw.line([ax, ay + 10, ax + 10, ay], fill="#FFFFFF", width=3)
    draw.line([ax, ay + 10, ax + 10, ay + 20], fill="#FFFFFF", width=3)

    draw.text((76, 86), "Transaction Details", fill="#FFFFFF", font=f_header)
    
    # Help Circle (?) on Top Right
    help_cx, help_cy = 664, 98
    draw.ellipse([help_cx - 18, help_cy - 18, help_cx + 18, help_cy + 18], fill="#7A3ABF")
    draw.text((help_cx - 6, help_cy - 12), "?", fill="#FFFFFF", font=get_windows_font(18, bold=True))

    # 3. Main Hero Card (24, 166 to 696, 456)
    hero_box = [24, 166, width - 24, 456]
    draw.rounded_rectangle(hero_box, radius=18, fill="#FFFFFF", outline="#E2E4EB", width=1)

    # Red Failed Icon (Vector Drawn White 'X' inside Red Circle)
    fail_cx, fail_cy = width // 2, 222
    draw.ellipse([fail_cx - 32, fail_cy - 32, fail_cx + 32, fail_cy + 32], fill="#E53935")
    cross_offset = 12
    draw.line([fail_cx - cross_offset, fail_cy - cross_offset, fail_cx + cross_offset, fail_cy + cross_offset], fill="#FFFFFF", width=4)
    draw.line([fail_cx + cross_offset, fail_cy - cross_offset, fail_cx - cross_offset, fail_cy + cross_offset], fill="#FFFFFF", width=4)

    # Clean amount formatting
    clean_amt = str(amount).replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    if clean_amt.replace(",", "").isdigit():
        num = int(clean_amt.replace(",", ""))
        clean_amt = f"₹{num:,}" if num >= 1000 else f"₹{num}"
    else:
        clean_amt = f"₹{clean_amt}" if not clean_amt.startswith("₹") else clean_amt

    if not clean_amt.endswith(".00") and "." not in clean_amt:
        display_amt = f"{clean_amt}.00"
    else:
        display_amt = clean_amt

    hero_title = f"Payment of {display_amt} Failed"
    hb = draw.textbbox((0, 0), hero_title, font=f_hero_title)
    hw = hb[2] - hb[0]
    draw.text((width // 2 - hw // 2, 268), hero_title, fill="#D32F2F", font=f_hero_title)

    # Subtitle: "Paid to [UPI ID]"
    payee_line = f"Paid to {upi}"
    pb = draw.textbbox((0, 0), payee_line, font=f_val)
    pw = pb[2] - pb[0]
    draw.text((width // 2 - pw // 2, 312), payee_line, fill="#1C1C1E", font=f_val)

    # Amount Pill Box in center
    amt_box = [width // 2 - 140, 344, width // 2 + 140, 396]
    draw.rounded_rectangle(amt_box, radius=10, fill="#F7F8FA")
    ab = draw.textbbox((0, 0), display_amt, font=get_windows_font(28, bold=True))
    aw = ab[2] - ab[0]
    draw.text((width // 2 - aw // 2, 352), display_amt, fill="#1C1C1E", font=get_windows_font(28, bold=True))

    # Timestamp
    time_str = txn_dt.strftime("%d %b %Y at %I:%M %p")
    tb = draw.textbbox((0, 0), time_str, font=f_time)
    tw = tb[2] - tb[0]
    draw.text((width // 2 - tw // 2, 412), time_str, fill="#8E8E93", font=f_time)

    # 4. Official PhonePe Pink-Red Why-Did-It-Fail Card (24, 468 to 696, 642)
    err_box = [24, 468, width - 24, 642]
    draw.rounded_rectangle(err_box, radius=14, fill="#FFF1F0", outline="#FFA39E", width=1)

    # Warning Icon Polygon with '!'
    wx, wy = 52, 498
    draw.polygon([(wx, wy - 11), (wx - 11, wy + 9), (wx + 11, wy + 9)], fill="#CF1322")
    draw.text((wx - 2, wy - 8), "!", fill="#FFFFFF", font=get_windows_font(14, bold=True))

    draw.text((72, 486), "Receiver Bank Daily Limit Exceeded (U16)", fill="#CF1322", font=f_err_title)
    draw.text((44, 518), f"The receiver's account ({upi}) has reached its", fill="#333333", font=f_err_body)
    draw.text((44, 542), "maximum daily limit for receiving UPI payments set by NPCI.", fill="#333333", font=f_err_body)
    draw.text((44, 568), "Any money debited from your account will be refunded in 24 hrs.", fill="#333333", font=f_err_body)
    draw.text((44, 594), "Ask receiver for an alternate UPI ID or Bank A/C to pay.", fill="#CF1322", font=f_err_bold)

    # 5. Transfer Details White Card (24, 658 to 696, 1080)
    details_box = [24, 658, width - 24, 1080]
    draw.rounded_rectangle(details_box, radius=16, fill="#FFFFFF", outline="#E2E4EB", width=1)

    draw.text((44, 678), "Transfer Details", fill="#1C1C1E", font=f_sec_title)
    draw.line([44, 712, width - 44, 712], fill="#F0F1F5", width=1)

    # Row 1: Receiver info with Avatar circle
    draw.ellipse([44, 726, 88, 770], fill="#F0E6FF")
    initial = (upi[0] if upi else "P").upper()
    draw.text((58, 736), initial, fill="#5F259F", font=f_val)
    draw.text((102, 728), "Paid to", fill="#8E8E93", font=f_lbl)
    draw.text((102, 750), upi, fill="#1C1C1E", font=f_val)

    draw.line([44, 786, width - 44, 786], fill="#F5F6F9", width=1)

    # Row 2: Debited from (SBI Bank Logo Badge)
    draw.ellipse([44, 802, 88, 846], fill="#E0F2FE")
    draw.text((52, 814), "SBI", fill="#0369A1", font=get_windows_font(13, bold=True))
    draw.text((102, 804), "Debited from", fill="#8E8E93", font=f_lbl)
    draw.text((102, 826), "State Bank of India •••• 4821", fill="#1C1C1E", font=f_val)

    draw.line([44, 862, width - 44, 862], fill="#F5F6F9", width=1)

    # Row 3: Transaction ID with Copy Button
    draw.text((44, 878), "Transaction ID", fill="#8E8E93", font=f_lbl)
    txn_id = f"T261001{datetime.now().strftime('%H%M%S')}8491"
    draw.text((44, 900), txn_id, fill="#1C1C1E", font=f_val)
    # Copy pill button
    draw.rounded_rectangle([width - 120, 888, width - 44, 922], radius=6, fill="#F0F1F5")
    draw.text((width - 104, 896), "COPY", fill="#5F259F", font=f_copy)

    draw.line([44, 934, width - 44, 934], fill="#F5F6F9", width=1)

    # Row 4: UTR / UPI Ref ID with Copy Button
    draw.text((44, 950), "UTR (UPI Reference No.)", fill="#8E8E93", font=f_lbl)
    utr_no = f"6274{datetime.now().strftime('%H%M%S')}91"
    draw.text((44, 972), utr_no, fill="#1C1C1E", font=f_val)
    draw.rounded_rectangle([width - 120, 960, width - 44, 994], radius=6, fill="#F0F1F5")
    draw.text((width - 104, 968), "COPY", fill="#5F259F", font=f_copy)

    draw.line([44, 1006, width - 44, 1006], fill="#F5F6F9", width=1)

    # Row 5: NPCI Error Code
    draw.text((44, 1022), "NPCI Response", fill="#8E8E93", font=f_lbl)
    draw.text((44, 1044), "U16 : BENEFICIARY_DAILY_LIMIT_EXCEEDED", fill="#D32F2F", font=f_val)

    # 6. Action Button: Big Purple RETRY PAYMENT Button
    btn_retry = [24, 1100, width - 24, 1164]
    draw.rounded_rectangle(btn_retry, radius=12, fill="#5F259F")
    b_txt = "RETRY PAYMENT"
    bb = draw.textbbox((0, 0), b_txt, font=f_btn)
    bw = bb[2] - bb[0]
    draw.text((width // 2 - bw // 2, 1120), b_txt, fill="#FFFFFF", font=f_btn)

    # 7. Secondary Action: CONTACT PHONEPE SUPPORT Button
    btn_support = [24, 1180, width - 24, 1242]
    draw.rounded_rectangle(btn_support, radius=12, fill="#FFFFFF", outline="#5F259F", width=2)
    s_txt = "CONTACT PHONEPE SUPPORT"
    sb = draw.textbbox((0, 0), s_txt, font=f_sub_btn)
    sw = sb[2] - sb[0]
    draw.text((width // 2 - sw // 2, 1200), s_txt, fill="#5F259F", font=f_sub_btn)

    # 8. Footer: NPCI Security Tag
    footer_txt = "NPCI  •  BHIM UPI  •  SECURED BY 256-BIT ENCRYPTION"
    draw.text((width // 2 - 200, 1276), footer_txt, fill="#9CA3AF", font=get_windows_font(13, bold=True))

    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf


# =====================================================================
# 5. NCRP 1930 FORENSIC EVIDENCE PDF (REPORTLAB)
# =====================================================================
def generate_fir_pdf() -> io.BytesIO:
    """Generates an official NCRP / Helpline 1930 Cybercrime Forensic Evidence PDF."""
    # Snapshot shared state once so the (slow) PDF build never iterates
    # over collections another thread is mutating.
    with STATE_LOCK:
        case_id = STATE["case_id"]
        target_scammer = STATE["target_scammer"]
        intel = {k: sorted(v) for k, v in STATE["extracted_intel"].items()}
        canary_hits = [dict(h) for h in STATE["canary_hits"]]
        scammer_chat = [dict(m) for m in STATE["scammer_chat"]]
        # Advanced analytics snapshot (kill-chain, dossier, scam mix, channels).
        funnel = dict(STATE.get("funnel") or {})
        dossier = dict(_build_dossier_locked())
        scam_mix = dict(STATE.get("scam_type_counts") or {})
        aggression = dict(STATE.get("aggression") or {})
        channels = sorted({
            str(t.get("platform") or "whatsapp")
            for t in (STATE.get("incoming_threads") or {}).values()
        }) or ["whatsapp"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    normal_style = styles["Normal"]

    evidence_json = json.dumps({
        "case_id": case_id,
        "extracted_intel": intel,
        "canary_hits": canary_hits,
        "scammer_chat": scammer_chat
    }, sort_keys=True)
    evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()

    story = []

    title_style = ParagraphStyle(
        "OfficialTitle",
        parent=normal_style,
        fontName="Helvetica-Bold",
        fontSize=15,
        textColor=colors.HexColor("#FFFFFF"),
        alignment=1,
        spaceAfter=4
    )
    sec_heading_style = ParagraphStyle(
        "SectionHeading",
        parent=normal_style,
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=colors.HexColor("#0F172A"),
        spaceBefore=12,
        spaceAfter=6
    )
    cell_style = ParagraphStyle(
        "CellRegular",
        parent=normal_style,
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#1E293B")
    )
    cell_bold = ParagraphStyle(
        "CellBold",
        parent=normal_style,
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#0F172A")
    )

    header_html = f"""
    <b>INDIAN CYBERCRIME FORENSIC INTERCEPTION DOSSIER</b><br/>
    <font size="8" color="#93C5FD">National Cyber Crime Reporting Portal (NCRP / Helpline 1930) Evidence Record</font><br/>
    <font size="7.5" color="#E2E8F0">Case Reference: <b>{case_id}</b> | Timestamp: {datetime.now().strftime('%d-%m-%Y %H:%M:%S IST')}</font><br/>
    <font size="7" color="#67E8F9">EVIDENCE SHA-256: {evidence_hash}</font>
    """
    banner_table = Table([[Paragraph(header_html, title_style)]], colWidths=[523])
    banner_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#0F172A")),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 10))

    # Section 1
    story.append(Paragraph("1. EXECUTIVE INCIDENT SUMMARY", sec_heading_style))
    summary_data = [
        [Paragraph("Incident Case ID", cell_bold), Paragraph(case_id, cell_style),
         Paragraph("Interception Mode", cell_bold), Paragraph("Autonomous WhatsApp Linked Honeypot", cell_style)],
        [Paragraph("Target Suspect Number", cell_bold), Paragraph(target_scammer or "Multi-Target Intercept", cell_style),
         Paragraph("Total Mule IOCs", cell_bold), Paragraph(str(sum(len(v) for v in intel.values())), cell_style)],
        [Paragraph("Total Canary IP Hits", cell_bold), Paragraph(str(len(canary_hits)), cell_style),
         Paragraph("Integrity Status", cell_bold), Paragraph("VERIFIED (SHA-256 Matched)", cell_style)],
    ]
    summary_table = Table(summary_data, colWidths=[120, 141, 120, 142])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(summary_table)

    # Section 2
    story.append(Paragraph("2. CAPTURED FINANCIAL & TELECOM MULE IDENTIFIERS (IOCs)", sec_heading_style))
    upis_str = ", ".join(intel["upi_ids"]) or "None extracted yet"
    phones_str = ", ".join(intel["phone_numbers"]) or "None extracted yet"
    banks_str = ", ".join(intel["bank_accounts"]) or "None extracted yet"
    ifsc_str = ", ".join(intel["ifsc_codes"]) or "None extracted yet"

    ioc_data = [
        [Paragraph("Mule Identifier Category", cell_bold), Paragraph("Extracted Value(s)", cell_bold), Paragraph("Verification / Risk Level", cell_bold)],
        [Paragraph("Suspect UPI IDs", cell_style), Paragraph(upis_str, cell_style), Paragraph("CRITICAL - Active Payment Mule", cell_bold)],
        [Paragraph("Suspect Phone Numbers", cell_style), Paragraph(phones_str, cell_style), Paragraph("HIGH - Telecom Carrier Traced", cell_style)],
        [Paragraph("Suspect Bank Accounts", cell_style), Paragraph(banks_str, cell_style), Paragraph("CRITICAL - Settlement Layer", cell_bold)],
        [Paragraph("Suspect IFSC Codes", cell_style), Paragraph(ifsc_str, cell_style), Paragraph("HIGH - Branch Identified", cell_style)],
    ]
    ioc_table = Table(ioc_data, colWidths=[140, 243, 140])
    ioc_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(ioc_table)

    # Section 3
    story.append(Paragraph("3. CANARY TRAP NETWORK & DEVICE FORENSICS", sec_heading_style))
    canary_data = [[
        Paragraph("Timestamp (IST)", cell_bold),
        Paragraph("Trap Ref ID", cell_bold),
        Paragraph("Captured Public IP", cell_bold),
        Paragraph("Location source / coordinates", cell_bold),
        Paragraph("Device / ISP / Browser Agent", cell_bold)
    ]]
    if canary_hits:
        for hit in canary_hits:
            coords = ""
            if hit.get("lat") is not None and hit.get("lon") is not None:
                coords = f"<br/><font size='7' color='#64748B'>Lat {round(float(hit['lat']), 4)}, Lon {round(float(hit['lon']), 4)}</font>"
            if hit.get("location_method") == "browser_geolocation_consent":
                location_text = (
                    f"<b>Device location shared with permission</b>"
                    f"<br/><font size='7' color='#64748B'>Browser accuracy ±"
                    f"{hit.get('location_accuracy_m', 'unknown')} m</font>"
                )
                if hit.get("ip_location"):
                    location_text += (
                        f"<br/><font size='7' color='#64748B'>Separate approximate IP estimate: "
                        f"{hit['ip_location']}</font>"
                    )
            else:
                location_text = (
                    f"Approximate IP-based estimate (not GPS): "
                    f"{hit.get('location', 'Location unavailable')}"
                )
            canary_data.append([
                Paragraph(hit.get("timestamp", "-"), cell_style),
                Paragraph(hit.get("receipt_id", "-"), cell_style),
                Paragraph(f"<b>{hit.get('ip', '-')}</b>" + (f"<br/><font size='7' color='#64748B'>{hit.get('as_num', '')}</font>" if hit.get("as_num") else ""), cell_style),
                Paragraph(f"{location_text}{coords}", cell_style),
                Paragraph(f"{hit.get('os_device', '')}<br/><font size='7' color='#64748B'>{hit.get('isp', '')} · {hit.get('user_agent', '')[:90]}</font>", cell_style),
            ])
    else:
        canary_data.append([
            Paragraph("-", cell_style),
            Paragraph("Pending", cell_style),
            Paragraph("No IP hits captured yet", cell_style),
            Paragraph("-", cell_style),
            Paragraph("Canary link deployed, waiting for suspect interaction", cell_style)
        ])

    canary_table = Table(canary_data, colWidths=[85, 75, 95, 120, 148])
    canary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(canary_table)

    # Section 4 - advanced AI deception analytics
    story.append(Paragraph("4. AI DECEPTION INTELLIGENCE &amp; THREAT ANALYTICS", sec_heading_style))
    stages = funnel.get("stages") or ["APPROACH", "RAPPORT", "PRESSURE", "PAYMENT_ASK", "BLOCKED"]
    stage_idx = int(funnel.get("stage") or 0)
    stage_chain = " &gt; ".join(
        f"<b>{s}</b>" if i <= stage_idx else s for i, s in enumerate(stages)
    )
    mix_str = ", ".join(f"{k} ({v})" for k, v in sorted(
        scam_mix.items(), key=lambda kv: -kv[1])) or "No scam signals classified yet"
    analytics_data = [
        [Paragraph("Threat Vector Parameter", cell_bold), Paragraph("Measured Value / Evidence", cell_bold)],
        [Paragraph("Detected Scam Playbook", cell_bold),
         Paragraph(str(dossier.get("playbook") or "Awaiting pattern"), cell_style)],
        [Paragraph("Attacker Risk Score (0-100)", cell_bold),
         Paragraph(f"{dossier.get('risk_score', 0)} / 100 - {dossier.get('risk_label', 'UNRATED')}", cell_style)],
        [Paragraph("Aggression Level", cell_bold),
         Paragraph(f"Current {aggression.get('current', 0)} · Average {aggression.get('average', 0)} · Peak {aggression.get('peak', 0)} ({dossier.get('aggression_label', 'Calm')})", cell_style)],
        [Paragraph("Attack Kill-Chain Progress", cell_bold),
         Paragraph(stage_chain, cell_style)],
        [Paragraph("Payments Blocked / Stalled Amount", cell_bold),
         Paragraph(f"{funnel.get('blocked', 0)} payment demand(s) blocked · {funnel.get('payment_asks', 0)} ask(s) · INR {funnel.get('money_stalled', 0):,} stalled by AI interception", cell_style)],
        [Paragraph("Scam-Type Classification Mix", cell_bold), Paragraph(mix_str, cell_style)],
        [Paragraph("Attacker Language Profile", cell_bold),
         Paragraph(f"{dossier.get('language', 'Unknown')} · {dossier.get('turns', 0)} exchange turns recorded", cell_style)],
        [Paragraph("Intercepted Channels", cell_bold),
         Paragraph(", ".join(channels).upper() + " (platform-agnostic interception)", cell_style)],
    ]
    analytics_table = Table(analytics_data, colWidths=[170, 353])
    analytics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
        ('BACKGROUND', (0, 1), (0, -1), colors.HexColor("#F8FAFC")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(analytics_table)

    # Section 5
    story.append(Paragraph("5. CHRONOLOGICAL INTERCEPTION TRANSCRIPT", sec_heading_style))
    transcript_data = [[
        Paragraph("Time (IST)", cell_bold),
        Paragraph("Speaker / Channel", cell_bold),
        Paragraph("Message Content & Attached Payloads", cell_bold)
    ]]
    if scammer_chat:
        for msg in scammer_chat:
            role_badge = "SUSPECT (Scammer)" if msg["role"] == "scammer" else "VICTIM (AI Interceptor)"
            content_html = html.escape(msg["text"])
            if msg.get("media"):
                content_html += "<br/><font color='#2563EB'>[ATTACHMENT: Fake UPI Glitch Error Screenshot (U16)]</font>"
            if msg.get("trap_url"):
                content_html += f"<br/><font color='#D97706'>[PAYLOAD LINK: {html.escape(msg['trap_url'])}]</font>"

            transcript_data.append([
                Paragraph(msg.get("time", "-"), cell_style),
                Paragraph(f"<b>{role_badge}</b>", cell_style),
                Paragraph(content_html, cell_style),
            ])
    else:
        transcript_data.append([
            Paragraph("-", cell_style),
            Paragraph("Standby", cell_style),
            Paragraph("Interception stream active. Awaiting incoming communication.", cell_style)
        ])

    transcript_table = Table(transcript_data, colWidths=[70, 130, 323])
    transcript_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(transcript_table)

    # Section 5
    story.append(Spacer(1, 10))
    cert_text = (
        "<b>CERTIFICATE UNDER SECTION 63 OF THE BHARATIYA SAKSHYA ADHINIYAM (BSA), 2023</b><br/>"
        "This electronic record has been produced automatically by the ScamTrap AI Autonomous Honeypot Interception System. "
        "The computer device and software operated continuously and correctly throughout the extraction period. "
        "The contents have not been altered, modified, or manipulated in any manner. "
        f"Evidence Digital Signature SHA-256: <code>{evidence_hash}</code>"
    )
    cert_table = Table([[Paragraph(cert_text, cell_style)]], colWidths=[523])
    cert_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#475569")),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(cert_table)

    doc.build(story)
    buf.seek(0)
    return buf


# =====================================================================
# 6. FASTAPI ROUTES & WEBHOOKS
# =====================================================================

@app.get("/tools/fake-receipt")
def get_fake_receipt(
    upi: str = Query(default="payee@upi"),
    amount: str = Query(default="500")
):
    """Renders the pixel-perfect fake Google Pay/PhonePe screenshot."""
    buf = create_fake_upi_image(upi=upi, amount=amount)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/download-fir")
def download_fir():
    """Downloads the official NCRP 1930 Forensic Evidence PDF."""
    pdf_buf = generate_fir_pdf()
    with STATE_LOCK:
        case_id = STATE["case_id"]
    filename = f"NCRP_1930_FIR_{case_id}.pdf"
    return StreamingResponse(
        pdf_buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/api/state")
def get_state():
    """Returns the full live system state for the SOC dashboard."""
    now_ms = time.time() * 1000
    with STATE_LOCK:
        activity = dict(STATE["ai_activity"])
        if now_ms > float(activity.get("until_ms", 0)):
            activity["active"] = False
        funnel = dict(STATE["funnel"])
        funnel["stalled_amounts"] = list(funnel.get("stalled_amounts", []))
        funnel["money_stalled"] = sum(int(a) for a in funnel["stalled_amounts"])
        funnel["stages"] = list(_FUNNEL_STAGES)
        payload = {
            "case_id": STATE["case_id"],
            "server_time_ms": now_ms,
            "wa_status": STATE["wa_status"],
            "wa_phone": STATE["wa_phone"],
            "wa_my_jid": STATE["wa_my_jid"],
            "wa_qr_raw": STATE["wa_qr_raw"],
            "trapped_numbers": sorted(STATE["trapped_numbers"]),
            "monitored_numbers": sorted(STATE["monitored_numbers"]),
            "target_scammer": STATE["target_scammer"],
            "target_mode": STATE["target_mode"],
            "incoming_threads": STATE["incoming_threads"],
            "scammer_chat": STATE["scammer_chat"],
            "bot_command_chat": STATE["bot_command_chat"],
            "thought_logs": STATE["thought_logs"],
            "extracted_intel": {k: sorted(v) for k, v in STATE["extracted_intel"].items()},
            "canary_hits": STATE["canary_hits"],
            "ai_activity": activity,
            "funnel": funnel,
            "scam_type_counts": dict(STATE["scam_type_counts"]),
            "aggression": {k: STATE["aggression"][k] for k in ("current", "average", "peak")},
            "dossier": _build_dossier_locked(),
            "persona": STATE.get("persona"),
            "session": dict(STATE.get("session", {})),
            "public_tunnel_url": STATE["public_tunnel_url"]
        }
    return JSONResponse(payload)


@app.post("/api/wa-status")
def update_wa_status(payload: Dict[str, Any] = Body(...)):
    """Receives WhatsApp connection status and raw QR code from wa_bridge.js."""
    status = payload.get("status", "DISCONNECTED")

    with STATE_LOCK:
        STATE["wa_status"] = status

        if "qr" in payload:
            STATE["wa_qr_raw"] = payload["qr"]
        if "phone" in payload:
            STATE["wa_phone"] = payload["phone"]
        if "my_jid" in payload:
            STATE["wa_my_jid"] = payload["my_jid"]

        connected_phone = STATE["wa_phone"]

    if status == "CONNECTED":
        log_thought("WHATSAPP", f"Phone 1 Linked (+{connected_phone}). Ready for trap/monitor commands.")

    return {"status": "ok"}


def _normalize_sender_number(raw: str, jid: str = "") -> tuple[str, bool]:
    """Return (digits, is_lid) with one consistent E.164-style key.

    Accepts JIDs ('919876543210@c.us', '919876543210:26@c.us', '...@lid'),
    '+' prefixes and bare digits. A phone-like chat id always wins over a
    contact-lookup fallback so a privacy-mode LID can never replace the real
    number when the JID already carries it. A 10-digit Indian number gets the
    '91' prefix so it matches `trap 9876543210` command keys.
    """
    def digits_of(s: str) -> str:
        return re.sub(r"\D", "", (s or "").split("@")[0].split(":")[0])

    jid_digits = digits_of(jid)
    raw_digits = digits_of(raw)
    if jid.endswith("@lid") and (not raw_digits or raw_digits == jid_digits):
        return (f"LID:{jid_digits}" if jid_digits else "LID:unknown"), True

    for src in (jid, raw):
        if src and src.endswith(("@c.us", "@s.whatsapp.net")):
            d = digits_of(src)
            if 10 <= len(d) <= 13:
                return ("91" + d if len(d) == 10 else d), False

    # Non-phone sender with letters in it (Instagram/Telegram/X handle) -
    # never strip it down to the digits of a year like "promo.scam.2026".
    for src in (raw, jid):
        local = (src or "").split("@")[0].split(":")[0]
        if local and re.search(r"[A-Za-z]", local):
            handle = re.sub(r"[^A-Za-z0-9_.\-]", "", local)
            return (handle[:40] if handle else ""), False

    d = digits_of(raw) or digits_of(jid)
    if not d:
        return "", False
    if len(d) == 10:
        d = "91" + d
    elif len(d) == 11 and d.startswith("0"):
        d = "91" + d[1:]
    return d, (jid or "").endswith("@lid") or len(d) > 13


# ---------------------------------------------------------------------
# Kill-chain analytics: scam-type mix, aggression scoring, attack funnel
# and the auto dossier. Pure functions over text + STATE (RLock-safe).
# ---------------------------------------------------------------------
_FUNNEL_STAGES = ["APPROACH", "RAPPORT", "PRESSURE", "PAYMENT_ASK", "BLOCKED"]

_SCAM_TYPE_PATTERNS = [
    ("Electricity / Utility Threat", r"electricity|bijli|discom|meter|light ?(cut|kat)|power ?cut|bill (is )?pending|lpg|gas cylinder|supply (band|cut)"),
    ("Job & Task Scam", r"\bjob\b|part ?time|registration fee|\btask\b|demo trade|deposit|withdrawal|salary|joining fee|assignment"),
    ("Loan App Extortion", r"loan|emi|credit limit|repay|overdue"),
    ("KYC / Bank Phishing", r"\bkyc\b|account (block|suspend)|suspicious (login|activity|transaction)|verify your (account|card|kyc)|atm ?card|expiry date|\bcvv\b|\botp\b"),
    ("Investment / Trading Scam", r"investment|trading|share ?market|crypto|bitcoin|\bprofit\b|returns|sip\b|portfolio"),
    ("Romance / Sextortion", r"blackmail|private (video|photo)|nude|viral kar|dating|girlfriend|boyfriend|bf ?gf"),
    ("Courier / Customs Trap", r"courier|parcel|customs|drug ?case|seized|delivery (blocked|hold)"),
    ("UPI / Payment Fraud", r"upi|gpay|phonepe|paytm|qr ?code|ifsc|account number|bank|transfer|payment|bhej|\bpay\b|rs\.?|₹|inr|fee|charge"),
]
_SCAM_DEMAND_RE = re.compile(
    r"pay|payment|transfer|bhej|bill|₹|rs\.?\s*\d|\binr\b|fee|charge|fine|deposit|amount"
    r"|credit nahi|receive nahi|verification fee|registration fee", re.IGNORECASE)
_AGGR_ABUSE_RE = re.compile(
    r"\bbc\b|\bbc mc\b|bakwas|bewakoof|pagal|chup kar|chup ho ja|kutta|kamina|idiot|stupid|fool|nonsense|shut ?up|impatient|waste", re.IGNORECASE)
_AGGR_THREAT_RE = re.compile(
    r"police|arrest|jail|\bfir\b|court|complaint|warning|last warning|band kara|kat dega|legal action|block kar", re.IGNORECASE)
_AGGR_URGENCY_RE = re.compile(
    r"\b(abhi|turant|jaldi|immediately|instantly|now itself|last ?chance|time nahi|dont delay|within \d+ ?(min|hour))\b", re.IGNORECASE)


def _classify_scam_type(text: str) -> str:
    t = (text or "").lower()
    for label, pattern in _SCAM_TYPE_PATTERNS:
        if re.search(pattern, t):
            return label
    return "Other"


def _aggression_score(text: str) -> int:
    """0-100 pressure/abuse score for one scammer message."""
    t = text or ""
    low = t.lower()
    score = 5
    if _AGGR_ABUSE_RE.search(low):
        score += 45
    if _AGGR_THREAT_RE.search(low):
        score += 25
    if _AGGR_URGENCY_RE.search(low):
        score += 15
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 8 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        score += 15
    score += min(15, t.count("!") * 4)
    return min(100, score)


def _note_scammer_message_locked(text: str) -> None:
    """Update scam-type mix, aggression meter and kill-chain stage.
    Caller must hold STATE_LOCK (RLock - safe to call from ingest)."""
    low = (text or "").lower()
    stype = _classify_scam_type(text)
    counts = STATE["scam_type_counts"]
    counts[stype] = int(counts.get(stype, 0)) + 1

    score = _aggression_score(text)
    agg = STATE["aggression"]
    agg["current"] = score
    _append_capped(agg["history"], score, cap=200)
    agg["average"] = int(round(sum(agg["history"]) / len(agg["history"]))) if agg["history"] else 0
    agg["peak"] = max(int(agg.get("peak", 0)), score)

    f = STATE["funnel"]
    turns = int(STATE["session"].get("turn", 0))
    intent = classify_intent(text)

    if turns >= 1:
        f["stage"] = max(int(f["stage"]), 1)
    if (_AGGR_THREAT_RE.search(low) or _AGGR_URGENCY_RE.search(low)) and turns >= 1:
        f["stage"] = max(int(f["stage"]), 2)
    if intent in _PAYMENT_CONTEXT_INTENTS or _SCAM_DEMAND_RE.search(text or ""):
        f["stage"] = max(int(f["stage"]), 3)
        f["payment_asks"] = int(f["payment_asks"]) + 1


def _record_block(text: str, amount_str: str) -> None:
    """Money demand was met with a stall / fake-glitch - i.e. payment BLOCKED.
    Counts once per demanding message; sums distinct amounts stalled."""
    if not (_SCAM_DEMAND_RE.search(text or "") or classify_intent(text or "") in _PAYMENT_CONTEXT_INTENTS):
        return
    try:
        val = int(str(amount_str or "").replace(",", ""))
    except ValueError:
        val = 0
    with STATE_LOCK:
        f = STATE["funnel"]
        f["blocked"] = int(f["blocked"]) + 1
        if val > 0 and val not in f["stalled_amounts"]:
            _append_capped(f["stalled_amounts"], val, cap=50)

        sess = STATE.get("session", {})
        # Only advance to final stage (BLOCKED / NEUTRALIZED) after glitch screenshot or canary link is sent or late conversation turns
        if int(sess.get("glitch_sent", 0)) > 0 or int(sess.get("links_sent", 0)) > 0 or int(sess.get("turn", 0)) >= 4:
            f["stage"] = 4
        else:
            f["stage"] = max(int(f.get("stage", 0)), 3)


def _set_ai_activity(phase: str, until_ms: float) -> None:
    with STATE_LOCK:
        STATE["ai_activity"] = {"active": True, "phase": phase, "until_ms": float(until_ms)}


def _build_dossier_locked() -> Dict[str, Any]:
    """Auto-profile of the active scammer. Caller must hold STATE_LOCK."""
    msgs = [m for m in STATE["scammer_chat"] if m.get("role") == "scammer"]
    turns = len(msgs)
    counts = STATE["scam_type_counts"]
    playbook = max(counts, key=lambda k: counts[k]) if counts else "Awaiting pattern"
    eng = hin = 0
    for m in msgs[-30:]:
        if _wants_english(m.get("text", "")):
            eng += 1
        else:
            hin += 1
    funnel = STATE["funnel"]
    stalled = sum(int(a) for a in funnel.get("stalled_amounts", []))
    intel = STATE["extracted_intel"]
    handles = len(intel["upi_ids"]) + len(intel["bank_accounts"])
    aggr_avg = int(STATE["aggression"].get("average", 0))
    links_seen = int(STATE["session"].get("scammer_links_seen", 0)) > 0

    risk = 5
    risk += 15 * min(4, int(funnel.get("payment_asks", 0)))
    risk += int(aggr_avg * 0.35)
    risk += 10 if links_seen else 0
    risk += 5 * min(3, handles)
    risk += 10 if int(funnel.get("blocked", 0)) else 0
    risk = min(100, risk)
    risk_label = "LOW" if risk < 30 else ("MODERATE" if risk < 55 else ("HIGH" if risk < 80 else "CRITICAL"))
    aggr_label = "Calm" if aggr_avg < 30 else ("Elevated" if aggr_avg < 60 else "Aggressive")

    return {
        "playbook": playbook,
        "language": "English" if eng > hin else ("Hinglish" if hin else "Unknown"),
        "turns": turns,
        "risk_score": risk,
        "risk_label": risk_label,
        "aggression_label": aggr_label,
        "payment_asks": int(funnel.get("payment_asks", 0)),
        "stalled_amount": stalled,
        "ioc_handles": handles,
    }


def _ingest_incoming(request: Request, payload: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Phase 1 of inbound handling: dedup, IOC extraction, mode routing and
    STANDBY/MONITOR replies. Runs entirely under STATE_LOCK - it never blocks
    on the network or on image generation.

    Returns (response, ctx). When response is not None the request is finished;
    when it is None the caller should proceed with the TRAP phase using ctx."""
    msg_id = payload.get("msg_id", "").strip()
    sender_jid = payload.get("sender_jid", "")
    sender_number = payload.get("sender_number", "").strip()
    text = payload.get("text", "").strip()
    platform = (payload.get("platform") or "whatsapp").strip().lower() or "whatsapp"
    now_str = datetime.now().strftime("%H:%M:%S")
    sender_number, sender_is_lid = _normalize_sender_number(sender_number, sender_jid)

    with STATE_LOCK:
        # Strict message deduplication (bounded, FIFO - never fully cleared)
        if msg_id and msg_id in STATE["processed_msg_ids"]:
            return {"status": "DUPLICATE_IGNORED", "should_reply": False}, {}
        if msg_id:
            _remember_message_id(msg_id)

        # Double check if last scammer message was identical
        if STATE["scammer_chat"]:
            last_m = STATE["scammer_chat"][-1]
            if last_m.get("role") == "scammer" and last_m.get("text") == text and last_m.get("sender") == f"+{sender_number}":
                return {"status": "DUPLICATE_IGNORED", "should_reply": False}, {}

        is_trapped = sender_number in STATE["trapped_numbers"]
        is_monitored = sender_number in STATE["monitored_numbers"]

        # New conversation = new kill chain. When a different trapped/monitored
        # number starts talking, the funnel, session, IOCs and chat log of the
        # previous conversation are dropped so this chat begins at APPROACH
        # instead of inheriting somebody else's BLOCKED stage.
        if is_trapped or is_monitored:
            prev_key = STATE.get("conversation_key")
            if prev_key and prev_key != sender_number:
                _start_new_conversation_locked(prev_key, sender_number)
            elif not prev_key:
                STATE["conversation_key"] = sender_number

        new_findings = extract_intelligence(text)

        # Scammer sending links themselves makes us sending one far less suspicious
        if _URL_RE.search(text):
            STATE["session"]["scammer_links_seen"] = int(STATE["session"].get("scammer_links_seen", 0)) + 1

        current_mode = "TRAP" if is_trapped else ("MONITOR" if is_monitored else "STANDBY")

        thread = STATE["incoming_threads"].get(sender_number, {
            "jid": sender_jid,
            "sender_number": sender_number,
            "is_lid": sender_is_lid,
            "msg_count": 0
        })
        thread["last_msg"] = text
        thread["last_time"] = now_str
        thread["mode"] = current_mode
        thread["is_lid"] = sender_is_lid
        thread["platform"] = platform
        thread["is_trapped"] = is_trapped
        thread["is_monitored"] = is_monitored
        thread["msg_count"] = thread.get("msg_count", 0) + 1
        STATE["incoming_threads"][sender_number] = thread

        sender_label = sender_number if sender_number.startswith("LID:") else f"+{sender_number}"

        # Never represent an unresolved WhatsApp LID as a suspect phone number.
        if not sender_number.startswith("LID:") and (
            not STATE["target_scammer"] or STATE["target_scammer"] == f"+{sender_number}"
        ):
            STATE["target_scammer"] = f"+{sender_number}"
            STATE["target_mode"] = current_mode

        for prev_m in STATE["scammer_chat"]:
            prev_m.pop("visible_after_ms", None)

        _append_capped(STATE["scammer_chat"], {
            "role": "scammer",
            "sender": sender_label,
            "text": text,
            "time": now_str,
            "media": None,
            "trap_url": None,
            "platform": platform
        })
        _note_scammer_message_locked(text)

        base_url = STATE["public_tunnel_url"] or str(request.base_url)

        # 1. STANDBY MODE (Neither Trapped nor Monitored yet)
        if not is_trapped and not is_monitored:
            _append_capped(STATE["thought_logs"], {
                "time": now_str,
                "tag": "STANDBY",
                "thought": f"Message from {sender_label}: '{text[:50]}...'. Logged in dashboard. Awaiting 'trap' or 'monitor' command."
            })
            return {"status": "STANDBY", "trapped": False, "should_reply": False}, {}

        # 2. MONITOR ONLY MODE (Smart: Passive for casual, Stall Delay if asking for money)
        if is_monitored and not is_trapped:
            lower_t = text.lower()
            is_asking_money = (
                bool(new_findings) or
                any(w in lower_t for w in [
                    "paisa", "paise", "rupaye", "rupees", "rs", "inr", "transfer", "pay", "payment",
                    "bhej", "bhejo", "gpay", "phonepe", "paytm", "upi", "account", "fees", "charge",
                    "bill", "bijli", "electricity", "disconnection", "cut", "block", "kyc", "parcel",
                    "courier", "customs", "police", "arrest", "lottery", "reward", "invest", "task",
                    "job", "deposit", "credit", "debit", "send", "scanner", "qr", "mangi", "mang"
                ])
            )

            if is_asking_money:
                stall_options = [
                    "Haan theek hai bhai, ruko thodi der me dekhta hu.",
                    "Acha theek hai, main abhi thoda busy hu free hoke dekhta hu.",
                    "Haan bhai msg mila, main 10-15 min me check karke batata hu.",
                    "Ruko bhai, main abhi laptop/phone khol ke check karta hu.",
                    "Haan theek hai bhai, thoda time do main dekh ke karta hu."
                ]
                stall_reply = random.choice(stall_options)

                _append_capped(STATE["scammer_chat"], {
                    "role": "user_ai",
                    "sender": "You (Monitor Stall)",
                    "text": stall_reply,
                    "time": now_str,
                    "media": None,
                    "trap_url": None,
                    "platform": platform
                })
                _record_block(text, extract_amount_demanded(text))

                intel_note = f" (IOCs Found: {', '.join(new_findings)})" if new_findings else ""
                bot_report = (
                    f"🚨 [MONITOR ALERT: MONEY DEMAND DETECTED] +{sender_number} is demanding payment/scamming!{intel_note}\n"
                    f"🤖 AI sent stall delay: \"{stall_reply}\"\n"
                    f"👉 Type `trap` in self-chat to engage full trap & send fake UPI glitch."
                )

                _append_capped(STATE["thought_logs"], {
                    "time": now_str,
                    "tag": "MONITOR_STALL",
                    "thought": f"Payment demand detected from +{sender_number}. Sent delay stall reply ('{stall_reply}'). Alerted user on Phone 1."
                })

                _append_capped(STATE["bot_command_chat"], {
                    "role": "bot",
                    "sender": "ScamTrap AI Bot",
                    "text": bot_report,
                    "time": now_str
                })

                stall_plan, stall_delay = _build_plan_for(stall_reply, media=False, prompt_len=len(text))
                _stall_visible_at = time.time() * 1000 + stall_delay
                _set_ai_activity("composing", _stall_visible_at)

                return {
                    "status": "MONITORED",
                    "should_reply": True,
                    "reply_text": stall_reply,
                    "bot_report": bot_report,
                    "delivery_plan": stall_plan,
                    "recommended_delay_ms": stall_delay
                }, {}

            # Casual conversation: 100% silent monitoring
            intel_note = f" (IOCs Found: {', '.join(new_findings)})" if new_findings else ""
            bot_report = f"👁️ [MONITORED MESSAGE] +{sender_number}: \"{text[:50]}\"{intel_note}"

            _append_capped(STATE["thought_logs"], {
                "time": now_str,
                "tag": "MONITOR",
                "thought": f"Passive monitor on +{sender_number}. Message logged, intel extracted{intel_note}. No reply sent (normal chat)."
            })

            _append_capped(STATE["bot_command_chat"], {
                "role": "bot",
                "sender": "ScamTrap AI Bot",
                "text": bot_report,
                "time": now_str
            })

            return {
                "status": "MONITORED",
                "should_reply": False,
                "bot_report": bot_report if new_findings else None
            }, {}

        # 3. ACTIVE TRAP MODE - lock is released here so the LLM call and the
        # Pillow render below can run without stalling other requests.
        STATE["target_scammer"] = f"+{sender_number}"
        STATE["target_mode"] = "TRAP"

    return None, {
        "sender_number": sender_number,
        "text": text,
        "new_findings": new_findings,
        "base_url": base_url,
        "platform": platform,
    }


@app.post("/api/wa-incoming")
def handle_wa_incoming(request: Request, payload: Dict[str, Any] = Body(...)):
    """Handles incoming WhatsApp messages forwarded by wa_bridge.js.

    Declared as a plain `def` on purpose: FastAPI runs sync handlers on a worker
    thread, so the blocking Gemini call and PNG rendering never stall the
    event loop (which would freeze the dashboard, the QR endpoint and the
    bridge's own HTTP calls)."""
    early_response, ctx = _ingest_incoming(request, payload)
    if early_response is not None:
        return early_response

    # ---- TRAP phase: NO lock held while calling the model or Pillow ----
    sender_number = ctx["sender_number"]
    text = ctx["text"]
    new_findings = ctx["new_findings"]
    base_url = ctx["base_url"]

    extracted_amt = extract_amount_demanded(text)
    _set_ai_activity("thinking", time.time() * 1000 + (GEMINI_BUDGET_S + 5) * 1000)
    t_agent = time.perf_counter()
    thought, reply_text, media_url, trap_url = run_hijack_agent(text, base_url)
    model_ms = int((time.perf_counter() - t_agent) * 1000)

    media_base64 = None
    if media_url:
        latest_upi = get_contextual_payee(sender_number)
        effective_amt = extracted_amt if extracted_amt else "500"
        img_buf = create_fake_upi_image(upi=latest_upi, amount=effective_amt)
        media_base64 = base64.b64encode(img_buf.read()).decode("utf-8")

    amt_display = f"₹{extracted_amt}" if extracted_amt else "live context"
    intel_note = f" (IOCs: {', '.join(new_findings)})" if new_findings else ""
    bot_report = f"🎯 [TRAP ENGAGED] Suspect +{sender_number} replied{intel_note}. AI deployed bait reply ({amt_display})."

    # Compile the human delivery plan (read delay -> typing -> bubbles).
    # The model's own thinking time counts toward the human read delay.
    delivery_plan, delay_ms = _build_plan_for(
        reply_text, media=bool(media_base64), prompt_len=len(text), elapsed_ms=model_ms)

    # The reply becomes visible on the dashboard only when the human-timed
    # delivery reaches its first send - keeps the live feed in sync with
    # what the scammer actually sees, and drives the typing indicator.
    _visible_at = time.time() * 1000 + delay_ms
    _set_ai_activity("composing", _visible_at)

    with STATE_LOCK:
        _append_capped(STATE["scammer_chat"], {
            "role": "user_ai",
            "sender": "You (AI Hijack)",
            "text": reply_text,
            "time": datetime.now().strftime("%H:%M:%S"),
            "media": media_url,
            "trap_url": trap_url,
            "platform": ctx.get("platform", "whatsapp")
        })
        _append_capped(STATE["bot_command_chat"], {
            "role": "bot",
            "sender": "ScamTrap AI Bot",
            "text": bot_report,
            "time": datetime.now().strftime("%H:%M:%S")
        })

    # Session bookkeeping: stage counters + reply-variation history
    _advance_session(reply_text, bool(media_base64), bool(trap_url))
    _record_block(text, extracted_amt)

    return {
        "status": "TRAPPED",
        "should_reply": True,
        "reply_text": reply_text,
        "media_base64": media_base64,
        "trap_url": trap_url,
        "bot_report": bot_report,
        "is_media": bool(media_base64),
        "delivery_plan": delivery_plan,
        "recommended_delay_ms": delay_ms,
        "model_ms": model_ms,
        "total_delay_ms": model_ms + delay_ms,
        "extracted_amount": extracted_amt
    }


@app.post("/api/simulate-dm")
def simulate_dm(request: Request, payload: Dict[str, Any] = Body(...)):
    """Channel-agnostic demo injector: pushes a message through the exact same
    pipeline as WhatsApp (dedupe, intel, funnel, trap/monitor, human timing)
    for any platform - Instagram DM, Telegram, X, SMS, email.
    Returns the same response shape as /api/wa-incoming."""
    platform = (payload.get("platform") or "").strip().lower() or "instagram"
    sender = (payload.get("sender") or "").strip()
    text = (payload.get("text") or "").strip()
    if not sender or not text:
        return JSONResponse({"status": "error", "detail": "sender and text are required"}, status_code=400)
    return handle_wa_incoming(request, {
        "msg_id": f"SIM_{uuid.uuid4().hex[:12]}",
        "sender_jid": f"{sender}@{platform}",
        "sender_number": sender,
        "text": text,
        "platform": platform,
    })


@app.post("/api/bot-self-command")
def handle_bot_self_command(request: Request, payload: Dict[str, Any] = Body(...)):
    """Handles commands typed in 'Message Yourself' on Phone 1 (e.g., 'trap 9876543210', 'monitor 9876543210', 'status')."""
    with STATE_LOCK:
        text = payload.get("text", "").strip()
        last_incoming_from = payload.get("last_incoming_from", "")
        now_str = datetime.now().strftime("%H:%M:%S")
        lower_text = text.lower()

        # Extract target number if specified in the command
        clean_digits = re.sub(r"[^\d]", "", text)
        digits_matches = re.findall(r"\d{10,12}", clean_digits)
        target_clean = None
        if digits_matches:
            raw_num = digits_matches[0]
            target_clean = raw_num if (raw_num.startswith("91") and len(raw_num) == 12) else f"91{raw_num[-10:]}"
        elif last_incoming_from:
            last_sender, last_sender_is_lid = _normalize_sender_number("", last_incoming_from)
            if not last_sender_is_lid:
                target_clean = last_sender
        elif STATE["target_scammer"]:
            active_target, active_target_is_lid = _normalize_sender_number(STATE["target_scammer"], "")
            if not active_target_is_lid:
                target_clean = active_target
        elif STATE["incoming_threads"]:
            target_clean = next(
                (
                    number for number in reversed(STATE["incoming_threads"])
                    if not number.startswith("LID:")
                ),
                None
            )

        target_jid = None
        if target_clean:
            target_jid = f"{target_clean}@lid" if len(target_clean) > 13 else f"{target_clean}@c.us"

        # COMMAND 1: HELP
        if lower_text == "help":
            help_msg = (
                "🤖 *ScamTrap AI Bot Commands:*\n\n"
                "• *trap <mobile>* : Activate AI Honeypot auto-reply and bait on that number.\n"
                "• *monitor <mobile>* : Passively monitor & extract intel (UPI/Bank/Phone) WITHOUT replying.\n"
                "• *status* : Check active target, mode, Phone 1 link, and captured IOCs.\n"
                "• *reset* : Clear all case telemetry and reset session."
            )
            return {"status": "ok", "bot_confirm_msg": help_msg}

        # COMMAND 2: STATUS
        if lower_text == "status":
            total_iocs = sum(len(v) for v in STATE["extracted_intel"].values())
            status_msg = (
                f"🛡️ *ScamTrap System Status:*\n\n"
                f"📱 *Phone 1 (You):* +{STATE['wa_phone'] or 'Connected'}\n"
                f"🎯 *Active Target:* {STATE['target_scammer'] or 'None'}\n"
                f"⚡ *Current Mode:* {STATE['target_mode']}\n"
                f"💳 *Captured IOCs:* {total_iocs} ({len(STATE['extracted_intel']['upi_ids'])} UPIs, {len(STATE['extracted_intel']['bank_accounts'])} Banks, {len(STATE['extracted_intel']['phone_numbers'])} Phones)\n"
                f"🌐 *Canary IP Hits:* {len(STATE['canary_hits'])}"
            )
            return {"status": "ok", "bot_confirm_msg": status_msg}

        # COMMAND 3: RESET
        if lower_text == "reset":
            reset_session()
            return {"status": "ok", "bot_confirm_msg": "🔄 Session reset successfully. Telemetry and target cleared."}

        # COMMAND 4: MONITOR MODE ("monitor <number>" or "watch <number>" or "stop" or "un-trap")
        if any(k in lower_text for k in ["monitor", "watch", "stop", "un-trap", "untrap", "passive"]):
            if not target_clean:
                return {
                    "status": "error",
                    "bot_confirm_msg": "⚠️ Please specify a mobile number to monitor: e.g. `monitor 9876543210`"
                }

            # Remove from trapped, add to monitored
            if target_clean in STATE["trapped_numbers"]:
                STATE["trapped_numbers"].remove(target_clean)
            STATE["monitored_numbers"].add(target_clean)
            STATE["target_scammer"] = f"+{target_clean}"
            STATE["target_mode"] = "MONITOR"

            if target_clean in STATE["incoming_threads"]:
                STATE["incoming_threads"][target_clean]["mode"] = "MONITOR"
                STATE["incoming_threads"][target_clean]["is_trapped"] = False
                STATE["incoming_threads"][target_clean]["is_monitored"] = True

            bot_confirm = (
                f"👁️ *MONITOR MODE ACTIVATED*\n"
                f"Target: *+{target_clean}*\n\n"
                f"ScamTrap AI is now passively intercepting messages and extracting IOCs (UPI/Bank/Phone). "
                f"No automated replies will be sent to this number."
            )

            _append_capped(STATE["thought_logs"], {
                "time": now_str,
                "tag": "MODE_CHANGE",
                "thought": f"Switched +{target_clean} to PASSIVE MONITOR MODE."
            })

            return {
                "status": "ok",
                "bot_confirm_msg": bot_confirm,
                "target_scammer_jid": None,
                "scammer_reply_text": None,
                "scammer_media_base64": None
            }

        # COMMAND 5: TRAP MODE ("trap <number>" or "trap")
        if "trap" in lower_text or not lower_text.startswith(("/", "!", ".")):
            if not target_clean:
                return {
                    "status": "error",
                    "bot_confirm_msg": "⚠️ Please specify a mobile number to trap: e.g. `trap 9876543210`"
                }

            # Remove from monitored, add to trapped
            if target_clean in STATE["monitored_numbers"]:
                STATE["monitored_numbers"].remove(target_clean)
            STATE["trapped_numbers"].add(target_clean)
            STATE["target_scammer"] = f"+{target_clean}"
            STATE["target_mode"] = "TRAP"

            if target_clean in STATE["incoming_threads"]:
                STATE["incoming_threads"][target_clean]["mode"] = "TRAP"
                STATE["incoming_threads"][target_clean]["is_trapped"] = True
                STATE["incoming_threads"][target_clean]["is_monitored"] = False

            bot_confirm = (
                f"🎯 *TRAP ENGAGED (AI HIJACK ACTIVE)*\n"
                f"Target: *+{target_clean}*\n\n"
                f"ScamTrap AI Honeypot is actively baiting this scammer. "
                f"Fake UPI Limit Exceeded receipts and Canary Hold links are primed."
            )

            _append_capped(STATE["thought_logs"], {
                "time": now_str,
                "tag": "MODE_CHANGE",
                "thought": f"Engaged ACTIVE TRAP on +{target_clean}."
            })

            # Check if scammer already had a pending unreplied message.
            # The model call and PNG render deliberately run AFTER this lock
            # is released (see below) so /api/state and /receipt/ never stall.
            thread = STATE["incoming_threads"].get(target_clean, {})
            trap_ctx = {
                "bot_confirm": bot_confirm,
                "target_jid": target_jid,
                "last_scammer_msg": thread.get("last_msg"),
                "base_url": STATE["public_tunnel_url"] or str(request.base_url),
            }
        else:
            trap_ctx = None

        if trap_ctx is None:
            return {"status": "ok", "bot_confirm_msg": "Type `help` for available ScamTrap commands."}

    # ---- STATE_LOCK released: blocking work is allowed from here ----
    scammer_reply = None
    media_base64 = None

    if trap_ctx["last_scammer_msg"]:
        last_scammer_msg = trap_ctx["last_scammer_msg"]
        extracted_amt = extract_amount_demanded(last_scammer_msg)
        t_agent = time.perf_counter()
        thought, reply_text, media_url, trap_url = run_hijack_agent(last_scammer_msg, trap_ctx["base_url"])
        model_ms = int((time.perf_counter() - t_agent) * 1000)
        scammer_reply = reply_text

        if media_url:
            latest_upi = get_contextual_payee(target_clean)
            effective_amt = extracted_amt if extracted_amt else "500"
            img_buf = create_fake_upi_image(upi=latest_upi, amount=effective_amt)
            media_base64 = base64.b64encode(img_buf.read()).decode("utf-8")

        with STATE_LOCK:
            _append_capped(STATE["scammer_chat"], {
                "role": "user_ai",
                "sender": "You (AI Hijack)",
                "text": reply_text,
                "time": datetime.now().strftime("%H:%M:%S"),
                "media": media_url,
                "trap_url": trap_url
            })

        _advance_session(reply_text, bool(media_base64), bool(trap_url))
        delivery_plan, _ = _build_plan_for(
            reply_text, media=bool(media_base64), prompt_len=len(last_scammer_msg), elapsed_ms=model_ms)

        return {
            "status": "ok",
            "bot_confirm_msg": trap_ctx["bot_confirm"],
            "target_scammer_jid": trap_ctx["target_jid"] if scammer_reply else None,
            "scammer_reply_text": scammer_reply,
            "scammer_media_base64": media_base64,
            "scammer_delivery_plan": delivery_plan
        }

    return {
        "status": "ok",
        "bot_confirm_msg": trap_ctx["bot_confirm"],
        "target_scammer_jid": trap_ctx["target_jid"] if scammer_reply else None,
        "scammer_reply_text": scammer_reply,
        "scammer_media_base64": media_base64
    }


@app.post("/api/set-target-mode")
def set_target_mode(request: Request, payload: Dict[str, Any] = Body(...)):
    """Sets mode (TRAP, MONITOR, STANDBY) for a suspect phone number from the Dashboard."""
    with STATE_LOCK:
        phone_number = payload.get("phone_number", "").strip().replace("+", "").replace(" ", "").replace("-", "")
        mode = payload.get("mode", "STANDBY").upper()

        if not phone_number:
            return JSONResponse({"error": "Phone number required"}, status_code=400)

        # Clean to 12 digits (91XXXXXXXXXX) if 10 digits
        clean_num = phone_number if (phone_number.startswith("91") and len(phone_number) == 12) else f"91{phone_number[-10:]}" if len(phone_number) >= 10 else phone_number

        STATE["target_scammer"] = f"+{clean_num}"
        STATE["target_mode"] = mode

        if mode == "TRAP":
            if clean_num in STATE["monitored_numbers"]:
                STATE["monitored_numbers"].remove(clean_num)
            STATE["trapped_numbers"].add(clean_num)
        elif mode == "MONITOR":
            if clean_num in STATE["trapped_numbers"]:
                STATE["trapped_numbers"].remove(clean_num)
            STATE["monitored_numbers"].add(clean_num)
        else:  # STANDBY / DISENGAGE
            if clean_num in STATE["trapped_numbers"]:
                STATE["trapped_numbers"].remove(clean_num)
            if clean_num in STATE["monitored_numbers"]:
                STATE["monitored_numbers"].remove(clean_num)
            STATE["target_mode"] = "STANDBY"

        if clean_num in STATE["incoming_threads"]:
            STATE["incoming_threads"][clean_num]["mode"] = STATE["target_mode"]
            STATE["incoming_threads"][clean_num]["is_trapped"] = (mode == "TRAP")
            STATE["incoming_threads"][clean_num]["is_monitored"] = (mode == "MONITOR")

        _append_capped(STATE["thought_logs"], {
            "time": datetime.now().strftime("%H:%M:%S"),
            "tag": "DASHBOARD_CONTROL",
            "thought": f"Target +{clean_num} set to {STATE['target_mode']} mode."
        })

        return {"status": "ok", "phone": clean_num, "mode": STATE["target_mode"]}


@app.post("/api/toggle-trap")
def toggle_trap(request: Request, payload: Dict[str, Any] = Body(...)):
    """Legacy 1-Click toggle button support on SOC Dashboard."""
    with STATE_LOCK:
        phone_number = payload.get("phone_number", "").strip().replace("+", "").replace(" ", "").replace("-", "")
        if not phone_number:
            return JSONResponse({"error": "Phone number required"}, status_code=400)

        clean_num = phone_number if (phone_number.startswith("91") and len(phone_number) == 12) else f"91{phone_number[-10:]}" if len(phone_number) >= 10 else phone_number

        if clean_num in STATE["trapped_numbers"]:
            return set_target_mode(request, {"phone_number": clean_num, "mode": "STANDBY"})
        else:
            return set_target_mode(request, {"phone_number": clean_num, "mode": "TRAP"})


@app.get("/api/wa-outbox")
def get_wa_outbox():
    """Polled by wa_bridge.js to immediately dispatch messages/reports.

    Read-and-clear must be atomic, otherwise a concurrent canary hit could be
    dropped between the snapshot and the clear."""
    with STATE_LOCK:
        queue = list(STATE["outbox_queue"])
        STATE["outbox_queue"].clear()
    return {"queue": queue}


@app.post("/api/reset")
def reset_session():
    """Resets chat, IOCs, and telemetry for a fresh real-world interception session."""
    with STATE_LOCK:
        STATE["case_id"] = generate_case_id()
        STATE["trapped_numbers"].clear()
        STATE["monitored_numbers"].clear()
        STATE["target_scammer"] = None
        STATE["target_mode"] = "STANDBY"
        STATE["incoming_threads"].clear()
        STATE["scammer_chat"].clear()
        STATE["conversation_key"] = None
        STATE["outbox_queue"].clear()
        STATE["canary_hits"].clear()
        # A clean demo must also forget processed message IDs, otherwise every
        # replayed message is answered with DUPLICATE_IGNORED after a reset.
        STATE["processed_msg_ids"].clear()
        STATE["persona"] = None
        STATE["session"] = _fresh_session()
        STATE["ai_activity"] = {"active": False, "phase": "", "until_ms": 0.0}
        STATE["funnel"] = {"stage": 0, "payment_asks": 0, "blocked": 0, "stalled_amounts": []}
        STATE["scam_type_counts"] = {}
        STATE["aggression"] = {"current": 0, "average": 0, "peak": 0, "history": []}
        STATE["extracted_intel"] = {
            "upi_ids": set(),
            "phone_numbers": set(),
            "ifsc_codes": set(),
            "bank_accounts": set(),
        }
        STATE["bot_command_chat"] = [
            {
                "role": "bot",
                "sender": "ScamTrap AI Bot",
                "text": "🛡️ Session reset. Ready for new interception. Type 'trap <number>' or 'monitor <number>'.",
                "time": datetime.now().strftime("%H:%M:%S")
            }
        ]
        STATE["thought_logs"] = [
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "tag": "SYSTEM",
                "thought": "All telemetry reset to baseline."
            }
        ]
        return {"status": "ok"}


GEO_CACHE: Dict[str, Dict[str, Any]] = {}
GEO_CACHE_LOCK = threading.Lock()


def _is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _valid_geo_coordinates(lat: Any, lon: Any) -> Optional[tuple[float, float]]:
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return latitude, longitude


_GEO_PROVIDER_NAMES = ("ip-api.com", "ipapi.co", "ipwho.is")


def _parse_geo_payload(provider: str, data: Any) -> Optional[Dict[str, Any]]:
    """Normalise one provider's answer into the shared geo shape.

    Every free provider speaks a slightly different dialect (ip-api:
    regionName/lat, ipapi.co: country_name/latitude, ipwho.is:
    connection.asn), so the parser accepts the union of those keys and only
    refuses payloads that carry no place and no usable coordinate at all."""
    if not isinstance(data, dict):
        return None
    if data.get("error") or (provider == "ip-api.com" and data.get("status") == "fail"):
        return None
    conn = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    asn = data.get("asn") or conn.get("asn") or ""
    if isinstance(asn, int):
        asn = f"AS{asn}"
    elif asn and not str(asn).lower().startswith("as"):
        asn = f"AS{asn}"
    parsed = {
        "country": data.get("country_name") or data.get("country") or "",
        "region": data.get("region") or data.get("regionName") or "",
        "city": data.get("city") or "",
        "isp": data.get("isp") or conn.get("isp") or data.get("org") or conn.get("org") or "",
        "org": data.get("org") or conn.get("org") or data.get("isp") or conn.get("isp") or "",
        "as_num": str(asn or ""),
        "lat": data.get("latitude", data.get("lat")),
        "lon": data.get("longitude", data.get("lon")),
        "geo_source": provider,
    }
    if _valid_geo_coordinates(parsed["lat"], parsed["lon"]):
        parsed["lat"], parsed["lon"] = _valid_geo_coordinates(parsed["lat"], parsed["lon"])
    elif any(parsed.get(key) for key in ("country", "region", "city")):
        parsed["lat"] = None
        parsed["lon"] = None
    else:
        return None
    return parsed


def _fetch_geo_provider(provider: str, ip: str) -> tuple[Optional[Dict[str, Any]], str]:
    """One provider attempt: (parsed geo, "") on success, (None, reason) else."""
    try:
        if provider == "ip-api.com":
            response = requests.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,regionName,city,isp,org,as,lat,lon"},
                timeout=4,
            )
        elif provider == "ipapi.co":
            response = requests.get(
                f"https://ipapi.co/{ip}/json/",
                timeout=4,
                headers={"User-Agent": "ScamTrapAI/1.0"},
            )
        else:
            response = requests.get(f"https://ipwho.is/{ip}", timeout=4)
        if response.status_code != 200:
            return None, f"{provider} HTTP {response.status_code}"
        parsed = _parse_geo_payload(provider, response.json())
        if parsed is None:
            return None, f"{provider} no location data"
        return parsed, ""
    except (requests.RequestException, ValueError) as error:
        return None, f"{provider} {type(error).__name__}"


def _merge_geo_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Consensus across every provider that answered: place/ISP fields by
    majority vote, coordinates by averaging the agreeing providers (a single
    provider still wins on its own - it is just a one-vote consensus)."""
    merged: Dict[str, Any] = {}
    for key in ("country", "region", "city", "isp", "org", "as_num"):
        values = [str(r.get(key)) for r in results if r.get(key)]
        if not values:
            merged[key] = ""
            continue
        best, best_count = values[0], 0
        for candidate in values:
            count = values.count(candidate)
            if count > best_count:
                best, best_count = candidate, count
        merged[key] = best
    coords = [pair for pair in (_valid_geo_coordinates(r.get("lat"), r.get("lon")) for r in results) if pair]
    if coords:
        merged["lat"] = sum(pair[0] for pair in coords) / len(coords)
        merged["lon"] = sum(pair[1] for pair in coords) / len(coords)
        merged["geo_confidence"] = "multi_provider" if len(coords) > 1 else "single_provider"
        if len(coords) > 1:
            spread = max(
                abs(coords[i][0] - coords[j][0]) + abs(coords[i][1] - coords[j][1])
                for i in range(len(coords)) for j in range(i + 1, len(coords))
            )
            merged["provider_spread_deg"] = round(spread, 4)
    else:
        merged["lat"] = None
        merged["lon"] = None
    sources = list(dict.fromkeys(str(r.get("geo_source")) for r in results if r.get("geo_source")))
    merged["geo_source"] = "+".join(sources)
    return merged


def _geolocate_ip(ip: str) -> Dict[str, Any]:
    """Best-effort city/region/country/ISP/ASN lookup for a captured IP.

    Cached per IP so repeat hits from the same scammer never re-query.
    Queries free providers in order (ip-api.com -> ipapi.co -> ipwho.is) and
    stops as soon as one answer is confident (city + valid coordinates); a
    partial answer keeps the next provider in the loop and the answers are
    then merged by consensus, which is what makes the city-level estimate
    reliable when one provider is rate-limited or wrong. Private / loopback
    addresses are skipped entirely. These results are approximate network
    locations, not device GPS coordinates."""
    with GEO_CACHE_LOCK:
        if ip in GEO_CACHE:
            return dict(GEO_CACHE[ip])

    if not _is_public_ip(ip):
        return {"geo_status": "not_public_ip"}

    results: List[Dict[str, Any]] = []
    errors: List[str] = []
    geo: Dict[str, Any] = {}
    for provider in _GEO_PROVIDER_NAMES:
        parsed, error = _fetch_geo_provider(provider, ip)
        if error:
            errors.append(error)
        if parsed:
            results.append(parsed)
        if not results:
            continue
        geo = _merge_geo_results(results)
        if _valid_geo_coordinates(geo.get("lat"), geo.get("lon")) and (geo.get("city") or geo.get("country")):
            break

    if not results:
        geo = {
            "geo_status": "unavailable",
            "geo_error": "; ".join(errors) or "No provider returned location data",
        }
        print(f"⚠️ [GEOLOCATION] Lookup unavailable ({geo['geo_error']}). Check backend outbound internet/provider limits.")
        return dict(geo)

    geo = _merge_geo_results(results)
    coordinates = _valid_geo_coordinates(geo.get("lat"), geo.get("lon"))
    if coordinates:
        geo["lat"], geo["lon"] = coordinates
    else:
        geo["lat"] = None
        geo["lon"] = None

    geo["geo_status"] = "resolved" if coordinates else "partial"
    with GEO_CACHE_LOCK:
        GEO_CACHE[ip] = dict(geo)
    return dict(geo)


def _geo_summary(geo: Dict[str, Any]) -> str:
    place = ", ".join(p for p in [geo.get("city"), geo.get("region"), geo.get("country")] if p)
    return place or "Location unavailable"


def _request_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "true-client-ip", "x-real-ip"):
        value = (request.headers.get(header) or "").strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    forwarded = request.headers.get("x-forwarded-for") or ""
    first_hop = forwarded.split(",")[0].strip() if forwarded else ""
    try:
        return str(ipaddress.ip_address(first_hop))
    except ValueError:
        peer = request.client.host if request.client else "127.0.0.1"
        try:
            return str(ipaddress.ip_address(peer))
        except ValueError:
            return "127.0.0.1"


@app.post("/receipt/{receipt_id}/share-location")
@app.post("/pay/status/{receipt_id}/share-location")
def share_canary_location(
    receipt_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
):
    """Store device coordinates only after the visitor explicitly shares them."""
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    accuracy = payload.get("accuracy")
    if isinstance(lat, bool) or not isinstance(lat, (int, float)):
        return JSONResponse({"error": "Valid latitude and longitude are required"}, status_code=400)
    if isinstance(lon, bool) or not isinstance(lon, (int, float)):
        return JSONResponse({"error": "Valid latitude and longitude are required"}, status_code=400)

    coordinates = _valid_geo_coordinates(lat, lon)
    if coordinates is None:
        return JSONResponse({"error": "Coordinates are outside valid geographic ranges"}, status_code=400)
    if (
        isinstance(accuracy, bool)
        or not isinstance(accuracy, (int, float))
        or not math.isfinite(float(accuracy))
        or accuracy < 0
    ):
        return JSONResponse({"error": "A valid location accuracy is required"}, status_code=400)

    visitor_ip = _request_ip(request)
    with STATE_LOCK:
        hit = next(
            (
                item for item in reversed(STATE["canary_hits"])
                if item.get("receipt_id") == receipt_id and item.get("ip") == visitor_ip
            ),
            None,
        )
        if hit is None:
            return JSONResponse(
                {"error": "No matching canary visit was found for this connection"},
                status_code=404,
            )
        hit["ip_lat"] = hit.get("ip_lat", hit.get("lat"))
        hit["ip_lon"] = hit.get("ip_lon", hit.get("lon"))
        hit["ip_location"] = hit.get("ip_location", hit.get("location", "Location unavailable"))
        hit["lat"], hit["lon"] = coordinates
        hit["location"] = "Device location shared with permission"
        hit["location_method"] = "browser_geolocation_consent"
        hit["location_accuracy_m"] = round(float(accuracy), 1)
        hit["geo_status"] = "consent_shared"
        hit["geo_source"] = "Browser geolocation (visitor permission)"

    log_thought(
        "CANARY_LOCATION",
        f"Visitor explicitly shared device location for canary {receipt_id} "
        f"(accuracy ±{float(accuracy):.0f} m).",
    )
    return {"status": "ok", "message": "Your shared coordinates were recorded."}


_CONSENT_HIT_KEYS = {"lat", "lon", "location", "location_method", "location_accuracy_m", "geo_status", "geo_source"}


def _merge_canary_hit_locked(hit: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    """One dashboard row per (receipt id, IP): reopening the same link updates
    the existing row instead of flooding the feed with duplicates.
    Must be called with STATE_LOCK held. Returns (hit, created)."""
    for existing in reversed(STATE["canary_hits"]):
        if existing.get("receipt_id") != hit.get("receipt_id") or existing.get("ip") != hit.get("ip"):
            continue
        existing["visits"] = int(existing.get("visits", 1)) + 1
        consent_owned = existing.get("geo_status") == "consent_shared"
        for key, value in hit.items():
            if value in (None, ""):
                continue
            if consent_owned and key in _CONSENT_HIT_KEYS:
                continue
            existing[key] = value
        existing["timestamp"] = hit.get("timestamp") or existing.get("timestamp")
        existing["last_seen"] = existing["timestamp"]
        return existing, False
    hit["visits"] = 1
    _append_capped(STATE["canary_hits"], hit, cap=200)
    return hit, True


@app.post("/receipt/{receipt_id}/telemetry")
@app.post("/pay/status/{receipt_id}/telemetry")
def receipt_telemetry(receipt_id: str, request: Request, payload: Optional[Dict[str, Any]] = Body(default=None)):
    """Passive browser telemetry fired by the status page (screen, timezone,
    locale, GPU, canvas fingerprint, connection). Merged into the row this
    receipt + IP already opened, so the report shows WHAT the device is, not
    only where it came from."""
    ip = _request_ip(request)
    clean: Dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if not isinstance(key, str) or not key or len(key) > 40:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            if len(value) > 240:
                value = value[:240]
        elif isinstance(value, bool):
            value = value
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                continue
        else:
            continue
        clean[key] = value
    if not clean:
        return {"status": "ignored"}

    now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S IST")
    with STATE_LOCK:
        target = None
        for existing in reversed(STATE["canary_hits"]):
            if existing.get("receipt_id") == receipt_id and existing.get("ip") == ip:
                target = existing
                break
        if target is None:
            geo = _geolocate_ip(ip)
            target, _ = _merge_canary_hit_locked({
                "receipt_id": receipt_id,
                "ip": ip,
                "user_agent": request.headers.get("user-agent", "Unknown"),
                "os_device": "Unknown Device",
                "timestamp": now_str,
                "country": geo.get("country", ""),
                "region": geo.get("region", ""),
                "city": geo.get("city", ""),
                "isp": geo.get("isp", ""),
                "org": geo.get("org", ""),
                "as_num": geo.get("as_num", ""),
                "lat": geo.get("lat"),
                "lon": geo.get("lon"),
                "location": _geo_summary(geo),
                "geo_status": geo.get("geo_status", "unavailable"),
                "geo_source": geo.get("geo_source", ""),
            })
        target["telemetry"] = clean
        target["last_seen"] = now_str
        if "fingerprint" in clean:
            target["fingerprint"] = clean["fingerprint"]
    return {"status": "ok"}


@app.get("/receipt/{receipt_id}", response_class=HTMLResponse)
@app.get("/pay/status/{receipt_id}", response_class=HTMLResponse)
def canary_trap_receipt(receipt_id: str, request: Request):
    """Records the visit IP and offers an explicit browser-location consent flow.

    Sync `def` so FastAPI runs it on a worker thread; it is hit by the scammer
    and must never queue behind LLM work on the event loop."""
    ip = _request_ip(request)
    user_agent = request.headers.get("user-agent", "Unknown")
    now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S IST")

    geo = _geolocate_ip(ip)
    cf_country = (request.headers.get("cf-ipcountry") or "").strip().upper()
    if cf_country and len(cf_country) == 2 and not geo.get("country"):
        geo["country"] = cf_country
        if geo.get("geo_status") == "unavailable":
            geo["geo_status"] = "partial"

    os_device = "Unknown Device"
    ua_lower = user_agent.lower()
    if "android" in ua_lower:
        os_device = "Android / Chrome Mobile"
    elif "iphone" in ua_lower:
        os_device = "Apple iPhone / iOS Safari"
    elif "windows" in ua_lower:
        os_device = "Windows PC / Edge"
    elif "macintosh" in ua_lower:
        os_device = "MacBook / macOS Safari"
    elif "linux" in ua_lower:
        os_device = "Linux Machine"

    hit_record = {
        "receipt_id": receipt_id,
        "ip": ip,
        "user_agent": user_agent,
        "os_device": os_device,
        "timestamp": now_str,
        "country": geo.get("country", ""),
        "region": geo.get("region", ""),
        "city": geo.get("city", ""),
        "isp": geo.get("isp", ""),
        "org": geo.get("org", ""),
        "as_num": geo.get("as_num", ""),
        "lat": geo.get("lat"),
        "lon": geo.get("lon"),
        "location": _geo_summary(geo),
        "geo_status": geo.get("geo_status", "unavailable"),
        "geo_source": geo.get("geo_source", ""),
    }
    with STATE_LOCK:
        _, created = _merge_canary_hit_locked(hit_record)

    if created:
        log_thought("CANARY_IP", f"🎯 IP Captured: {ip} | {_geo_summary(geo)} | {geo.get('isp', 'Unknown ISP')} | Device: {os_device}")
        bot_alert = (
            f"🎯 [IP CAPTURED!] Scammer clicked Canary Link!\n"
            f"IP: {ip}\n"
            f"Location: {_geo_summary(geo)}\n"
            f"ISP / ASN: {geo.get('isp', 'Unknown')} {geo.get('as_num', '')}\n"
            f"Device: {os_device}"
        )
        with STATE_LOCK:
            _append_capped(STATE["outbox_queue"], {
                "target_jid": None,
                "text": None,
                "media_base64": None,
                "bot_report": bot_alert
            })

    esc_id = html.escape(receipt_id)
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Transaction status · {esc_id}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>body {{ font-family: 'Inter', system-ui, -apple-system, sans-serif; }}</style>
</head>
<body class="bg-[#F4F5F7] text-[#1B1D22] min-h-screen flex flex-col">
    <header class="bg-white border-b border-[#E4E6EB]">
        <div class="max-w-lg w-full mx-auto px-5 py-3.5 flex items-center justify-between">
            <div class="flex items-center gap-2.5">
                <div class="w-8 h-8 rounded-lg bg-[#1B1D22] text-white text-sm font-bold flex items-center justify-center">₹</div>
                <span class="text-sm font-semibold tracking-tight">Transaction status</span>
            </div>
            <span class="text-[11px] font-mono text-[#6B7280]">{esc_id}</span>
        </div>
    </header>

    <main class="flex-1 max-w-lg w-full mx-auto px-5 py-7 space-y-4">
        <section class="bg-white rounded-2xl border border-[#E4E6EB] shadow-sm p-6">
            <div class="flex items-start justify-between gap-3">
                <div>
                    <p class="text-[11px] uppercase tracking-wider font-semibold text-[#6B7280]">Payment status</p>
                    <h1 class="text-xl font-semibold mt-1">Pending verification</h1>
                </div>
                <span class="text-[11px] font-semibold px-2.5 py-1 rounded-full bg-[#FEF3C7] text-[#92400E] border border-[#FDE68A] whitespace-nowrap">Awaiting confirmation</span>
            </div>
            <dl class="mt-5 grid grid-cols-2 gap-3 text-sm">
                <div class="bg-[#F8F9FB] rounded-xl p-3 border border-[#EDEFF3]">
                    <dt class="text-[11px] text-[#6B7280]">Reference ID</dt>
                    <dd class="font-mono text-[13px] mt-0.5 break-all">{esc_id}</dd>
                </div>
                <div class="bg-[#F8F9FB] rounded-xl p-3 border border-[#EDEFF3]">
                    <dt class="text-[11px] text-[#6B7280]">Checked at</dt>
                    <dd class="font-mono text-[13px] mt-0.5">{html.escape(now_str)}</dd>
                </div>
            </dl>
            <p class="mt-4 text-xs text-[#6B7280] leading-relaxed">
                Status shown here is for this reference only and refreshes each time the link is opened.
                It never asks for a PIN, OTP or card number.
            </p>
        </section>

        <section class="bg-white rounded-2xl border border-[#E4E6EB] shadow-sm p-5">
            <details>
                <summary class="cursor-pointer text-[11px] font-bold uppercase tracking-wider text-[#6B7280] hover:text-[#1B1D22]">
                    ScamTrap AI security test - transparency notice
                </summary>
                <p class="text-xs text-[#6B7280] mt-2">Transparent canary-link visit</p>
                <div class="mt-2 text-[13px] text-[#3B3F46] space-y-2.5 leading-relaxed">
                    <p>This page is operated by ScamTrap AI for a security demonstration. Opening the canary link records your public IP address, browser, and an approximate IP-based location for the project dashboard.</p>
                    <p><b>Optional precise location:</b> selecting the button asks your browser to request permission. If granted, latitude, longitude, and the browser-reported accuracy are sent to the ScamTrap AI operator and shown on the dashboard and project report. You can decline without sharing device coordinates.</p>
                    <p class="text-xs text-[#6B7280]">IP-based estimates may indicate an ISP/VPN gateway, not a physical location. Shared coordinates are stored in the current in-memory project session. A secure HTTPS connection is required (localhost is also supported).</p>
                </div>
                <div class="mt-3 bg-[#F8F9FB] p-3 rounded-xl border border-[#EDEFF3] text-xs">
                    <span class="text-[#6B7280]">Reference ID:</span> <span class="font-mono">{esc_id}</span><br>
                    <span class="text-[#6B7280]">IP-based estimate:</span> {html.escape(_geo_summary(geo))}
                </div>
            </details>

            <p class="mt-5 text-[11px] font-bold uppercase tracking-wider text-[#6B7280]">Optional device-location sharing</p>
            <p class="text-xs text-[#6B7280] mt-1">
                Share your current location once to confirm this verification step. Nothing is requested unless
                you choose to share it, and declining changes nothing else on this page.
            </p>

            <button id="share-location" type="button" class="mt-3 w-full bg-[#0B57D0] hover:bg-[#0842A0] text-white font-semibold py-3 rounded-xl text-sm tracking-wide transition shadow-sm">
                Share device location
            </button>
            <p id="location-status" role="status" aria-live="polite" class="text-xs text-[#6B7280] mt-2 min-h-5">Location sharing has not been requested.</p>
        </section>
    </main>

    <div class="text-center py-3.5 text-xs text-[#6B7280] bg-white border-t border-[#E4E6EB]">
        Approximate IP location is not device GPS. Device coordinates are sent only after you grant browser permission.
    </div>
    <script>
        (function () {{
            try {{
                var scr = window.screen || {{}};
                function hash(s) {{
                    var h = 2166136261;
                    for (var i = 0; i < s.length; i++) {{ h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }}
                    return (h >>> 0).toString(16);
                }}
                function glInfo() {{
                    try {{
                        var c = document.createElement("canvas");
                        var gl = c.getContext("webgl") || c.getContext("experimental-webgl");
                        if (!gl) return "";
                        var d = gl.getExtension("WEBGL_debug_renderer_info");
                        return d ? (gl.getParameter(d.UNMASKED_VENDOR_WEBGL) + " | " + gl.getParameter(d.UNMASKED_RENDERER_WEBGL))
                                 : (gl.getParameter(gl.VENDOR) + " | " + gl.getParameter(gl.RENDERER));
                    }} catch (e) {{ return ""; }}
                }}
                function canvasSig() {{
                    try {{
                        var c = document.createElement("canvas");
                        c.width = 240; c.height = 60;
                        var ctx = c.getContext("2d");
                        ctx.textBaseline = "top";
                        ctx.font = "16px Arial";
                        ctx.fillStyle = "#f60";
                        ctx.fillRect(0, 0, 120, 30);
                        ctx.fillStyle = "#069";
                        ctx.fillText("scamtrap-canary-test", 2, 2);
                        return c.toDataURL();
                    }} catch (e) {{ return ""; }}
                }}
                var gl = glInfo();
                var canvas = canvasSig();
                var conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection || {{}};
                var tz = {{}};
                try {{ tz = Intl.DateTimeFormat().resolvedOptions() || {{}}; }} catch (e) {{}}
                var payload = {{
                    screen: (scr.width || 0) + "x" + (scr.height || 0) + "@" + (window.devicePixelRatio || 1),
                    viewport: (window.innerWidth || 0) + "x" + (window.innerHeight || 0),
                    color_depth: scr.colorDepth || 0,
                    timezone: tz.timeZone || "",
                    tz_offset: new Date().getTimezoneOffset(),
                    locale: tz.locale || "",
                    languages: (navigator.languages || [navigator.language || ""]).join(","),
                    platform: navigator.platform || "",
                    vendor: navigator.vendor || "",
                    touch_points: navigator.maxTouchPoints || 0,
                    cores: navigator.hardwareConcurrency || 0,
                    device_memory: navigator.deviceMemory || 0,
                    cookies: navigator.cookieEnabled === true,
                    do_not_track: navigator.doNotTrack || "",
                    connection: [conn.effectiveType || "", conn.downlink || "", conn.rtt || ""].join("/"),
                    gpu: gl,
                    canvas_hash: hash(canvas),
                    referrer: document.referrer || "",
                    title: document.title || "",
                    fingerprint: hash([gl, canvas, (scr.width || 0), (navigator.platform || ""), (tz.timeZone || ""), (navigator.languages || []).join(",")].join("|"))
                }};
                var body = JSON.stringify(payload);
                var base = location.pathname.replace(/\\/+$/, "");
                var url = base + "/telemetry";
                if (navigator.sendBeacon) {{
                    navigator.sendBeacon(url, new Blob([body], {{ type: "application/json" }}));
                }} else {{
                    fetch(url, {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, body: body, keepalive: true }}).catch(function () {{}});
                }}
            }} catch (e) {{}}
        }})();
    </script>
    <script>
        const shareButton = document.getElementById("share-location");
        const statusElement = document.getElementById("location-status");
        shareButton.addEventListener("click", () => {{
            if (!window.isSecureContext) {{
                statusElement.textContent = "Precise location sharing requires HTTPS. No device coordinates have been requested.";
                return;
            }}
            if (!navigator.geolocation) {{
                statusElement.textContent = "This browser does not support device location. No coordinates were sent.";
                return;
            }}
            shareButton.disabled = true;
            statusElement.textContent = "Waiting for your browser's location permission…";
            navigator.geolocation.getCurrentPosition(async (position) => {{
                statusElement.textContent = "Permission granted. Sending the location you chose to share…";
                try {{
                    const base = location.pathname.replace(/\\/+$/, "");
                    const response = await fetch(base + "/share-location", {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json" }},
                        body: JSON.stringify({{
                            latitude: position.coords.latitude,
                            longitude: position.coords.longitude,
                            accuracy: position.coords.accuracy
                        }})
                    }});
                    const result = await response.json();
                    if (!response.ok) throw new Error(result.error || "Location could not be saved.");
                    statusElement.textContent = "Your consented device location was shared with the ScamTrap AI operator.";
                    shareButton.textContent = "Location shared";
                }} catch (error) {{
                    statusElement.textContent = error.message || "Could not send the shared location. Please try again.";
                    shareButton.disabled = false;
                }}
            }}, (error) => {{
                const reasons = {{
                    1: "You declined location permission. No device coordinates were sent.",
                    2: "Your device could not determine its location. No coordinates were sent.",
                    3: "Location request timed out. No coordinates were sent."
                }};
                statusElement.textContent = reasons[error.code] || "Location permission was not granted. No coordinates were sent.";
                shareButton.disabled = false;
            }}, {{ enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }});
        }});
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


# =====================================================================
# 7. CLEAN & FOCUSED 2-PANEL FORENSIC COMMAND CENTER (GET /)
# Theme: Obsidian Grey-Black, Velvet Burgundy, Dusty Olive, Warm Ivory
# Large Legible Typography & Mode Controls
# =====================================================================
@app.get("/", response_class=HTMLResponse)
def get_soc_dashboard():
    """Renders the aesthetic, legible forensic dashboard."""
    html_content = """<!DOCTYPE html>
<html lang="en" class="h-full bg-[#0D0E12]">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ScamTrap AI — Autonomous WhatsApp Interception</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&family=JetBrains+Mono:wght@500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0D0E12; --panel: #14161C; --panel-2: #181B22; --line: #242833; --line-2: #2E333F;
            --ivory: #FAF7F2; --text-2: #C8C1B5; --text-3: #7C766B; --faint: #4D5260;
            --burgundy: #9F1239; --burgundy-hi: #BE123C; --rose: #E11D48;
            --olive: #9FB5A3; --olive-bg: #172119; --olive-line: #2C3C2F;
        }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--ivory); }
        .font-heading { font-family: 'Plus Jakarta Sans', sans-serif; }
        .font-mono { font-family: 'JetBrains Mono', monospace; }
        .num { font-variant-numeric: tabular-nums; }
        .card { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.25); }
        .label-xs { font-size: 10.5px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--text-3); }
        .custom-scroll::-webkit-scrollbar { width: 6px; height: 6px; }
        .custom-scroll::-webkit-scrollbar-track { background: var(--bg); }
        .custom-scroll::-webkit-scrollbar-thumb { background: var(--line); border-radius: 8px; }
        .custom-scroll::-webkit-scrollbar-thumb:hover { background: var(--line-2); }
        .wa-bubble-scammer { background-color: #1A1D24; color: var(--ivory); border: 1px solid #282C37; border-radius: 0 14px 14px 14px; }
        .wa-bubble-ai { background-color: #172B20; color: var(--ivory); border: 1px solid #2B4534; border-radius: 14px 0 14px 14px; }
        .typing-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); display: inline-block; animation: tdot 1.2s infinite ease-in-out; }
        .typing-dot:nth-child(2) { animation-delay: .18s; } .typing-dot:nth-child(3) { animation-delay: .36s; }
        @keyframes tdot { 0%,60%,100% { transform: translateY(0); opacity:.4 } 30% { transform: translateY(-4px); opacity:1 } }
        .pulse-ring { animation: pring 1.6s infinite; }
        @keyframes pring { 0% { box-shadow: 0 0 0 0 rgba(159,18,57,.45) } 70% { box-shadow: 0 0 0 7px rgba(159,18,57,0) } 100% { box-shadow: 0 0 0 0 rgba(159,18,57,0) } }
        #origin-map { height: 260px; width: 100%; border-radius: 14px; background: #101216; z-index: 10; border: 1px solid var(--line); }
        .leaflet-container { font-family: 'Inter', sans-serif; background: #101216; }
        .leaflet-bar a { background: #181B22 !important; color: var(--text-2) !important; border-color: var(--line) !important; }
        .leaflet-bar a:hover { background: #20242D !important; color: var(--ivory) !important; }
        .leaflet-control-attribution { background: rgba(13,14,18,.8) !important; color: var(--faint) !important; font-size: 9px !important; }
        .leaflet-control-attribution a { color: var(--text-3) !important; }
        .leaflet-popup-content-wrapper { background: #14161C !important; color: var(--ivory) !important; border: 1px solid #373D4D !important; border-radius: 14px !important; box-shadow: 0 10px 25px rgba(0,0,0,0.6) !important; padding: 0 !important; }
        .leaflet-popup-content { margin: 12px 14px !important; line-height: 1.4 !important; }
        .leaflet-popup-tip { background: #14161C !important; border: 1px solid #373D4D !important; }
        .leaflet-popup-close-button { color: #7C766B !important; padding: 6px 8px !important; }
        .leaflet-popup-close-button:hover { color: #FAF7F2 !important; }
        .custom-radar-container { background: transparent !important; border: none !important; }
        .custom-radar-pin { position: relative; width: 24px; height: 24px; }
        .radar-core { position: absolute; top: 6px; left: 6px; width: 12px; height: 12px; background: #E11D48; border: 2px solid #FFFFFF; border-radius: 50%; box-shadow: 0 0 12px #E11D48; }
        .radar-pulse { position: absolute; top: 0; left: 0; width: 24px; height: 24px; border-radius: 50%; background: rgba(225, 29, 72, 0.45); animation: radarPing 2s infinite ease-out; }
        @keyframes radarPing { 0% { transform: scale(0.5); opacity: 1; } 100% { transform: scale(2.3); opacity: 0; } }
        .seg-btn { transition: all .15s ease; border: 1px solid var(--line); background: var(--panel-2); color: var(--text-3); }
        .seg-btn.on-trap { background: var(--burgundy); color: var(--ivory); border-color: var(--burgundy-hi); box-shadow: 0 2px 8px rgba(159,18,57,.35); }
        .seg-btn.on-monitor { background: var(--olive-bg); color: var(--olive); border-color: var(--olive-line); }
        .seg-btn.on-standby { background: #1E222B; color: var(--text-2); border-color: var(--line-2); }
        .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 12px; background: var(--panel-2); border: 1px solid var(--line); font-size: 11px; font-weight: 600; color: var(--text-2); white-space: nowrap; }
        .chip b { color: var(--ivory); font-family: 'JetBrains Mono', monospace; }
        @media (max-width: 1023px) {
            body { height: auto !important; min-height: 100vh; overflow-y: auto !important; overflow-x: hidden !important; }
            main { overflow: visible !important; height: auto !important; }
            main > * { overflow: visible !important; max-height: none !important; min-height: 0 !important; }
            #chat-feed { min-height: 55vh; max-height: 72vh; }
        }
        @media (max-width: 640px) { #chat-feed { min-height: 50vh; padding: 14px !important; } }
    </style>
</head>
<body class="h-full flex flex-col overflow-hidden text-[#FAF7F2]">

    <!-- TOP NAVBAR -->
    <header class="bg-[#14161C] border-b border-[#242833] px-4 sm:px-7 py-3 flex items-center justify-between gap-3 shrink-0">
        <div class="flex items-center gap-3 min-w-0">
            <div class="w-9 h-9 shrink-0 rounded-xl bg-gradient-to-br from-[#9F1239] to-[#4D1222] flex items-center justify-center font-bold shadow-md border border-[#BE123C]/30">
                <svg class="w-5 h-5 text-[#FAF7F2]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>
            </div>
            <div class="min-w-0">
                <div class="flex items-center gap-2">
                    <h1 class="text-sm sm:text-base font-extrabold tracking-tight font-heading">ScamTrap AI</h1>
                    <span class="hidden sm:inline text-[10px] font-bold px-1.5 py-0.5 rounded-md bg-[#172119] text-[#9FB5A3] border border-[#2C3C2F]">LIVE</span>
                </div>
                <p class="hidden md:block text-xs text-[#7C766B] truncate">Autonomous WhatsApp interception &amp; intelligence extraction</p>
            </div>
            <div class="hidden xl:flex items-center gap-2 ml-3">
                <span class="chip kpi-chip">Msgs <b id="kpi-msgs">0</b></span>
                <span class="chip kpi-chip">IOCs <b id="kpi-iocs">0</b></span>
                <span class="chip kpi-chip">Canary <b id="kpi-hits">0</b></span>
            </div>
        </div>

        <div class="flex items-center gap-2 sm:gap-3 shrink-0">
            <div id="wa-status-badge" class="flex items-center gap-2 px-2.5 sm:px-3.5 py-1.5 rounded-xl bg-[#181B22] border border-[#242833] cursor-pointer hover:border-[#373D4D] transition max-w-[46vw] sm:max-w-none" onclick="toggleQrModal()">
                <span class="w-2.5 h-2.5 shrink-0 rounded-full bg-amber-400 animate-pulse" id="wa-status-dot"></span>
                <span id="wa-status-text" class="text-[#C8C1B5] font-medium truncate text-xs sm:text-sm">Checking WhatsApp...</span>
            </div>
            <button onclick="resetDemoSession()" class="px-2.5 sm:px-3.5 py-1.5 bg-[#181B22] hover:bg-[#20242D] text-[#C8C1B5] hover:text-[#FAF7F2] rounded-xl text-sm font-medium border border-[#242833] transition flex items-center gap-2">
                <svg class="w-4 h-4 text-[#7C766B]" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                <span class="hidden sm:inline">Reset</span>
            </button>
            <a href="/download-fir" class="px-3 sm:px-4 py-1.5 bg-[#9F1239] hover:bg-[#BE123C] text-[#FAF7F2] rounded-xl text-sm font-semibold shadow-md shadow-[#9F1239]/25 border border-[#BE123C]/40 transition flex items-center gap-2">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                <span class="hidden sm:inline">Download 1930 FIR (PDF)</span><span class="sm:hidden">FIR PDF</span>
            </a>
        </div>
    </header>

    <!-- MAIN 2-COLUMN -->
    <main class="flex-1 grid grid-cols-12 gap-3 sm:gap-4 p-3 sm:p-4 lg:overflow-hidden min-h-0">

        <!-- LEFT: intelligence column -->
        <aside class="col-span-12 lg:col-span-4 flex flex-col gap-3 lg:overflow-y-auto custom-scroll lg:min-h-0 lg:pr-1">

            <!-- TARGET -->
            <section class="card p-4">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2">
                        <span class="text-sm">🎯</span>
                        <span class="label-xs">Target Suspect</span>
                        <span id="target-channel-chip" class="text-[10px] font-bold px-1.5 py-0.5 rounded-md bg-[#20242D] text-[#7C766B] border border-[#2E333F] uppercase font-mono">whatsapp</span>
                    </div>
                    <span id="target-trap-badge" class="text-[11px] px-2.5 py-1 rounded-lg bg-[#20242D] text-[#7C766B] font-mono font-semibold border border-[#2E333F]">STANDBY</span>
                </div>
                <div class="rounded-xl bg-[#181B22] border border-[#242833] p-3.5 space-y-3">
                    <div class="flex items-start justify-between gap-2">
                        <div class="min-w-0">
                            <div class="label-xs mb-1">Real Phone Number / Handle</div>
                            <div id="target-phone-display" class="font-mono font-bold text-base sm:text-lg text-[#FAF7F2] break-all leading-snug">+91 Suspect Target</div>
                        </div>
                        <span id="target-mode-pill" class="shrink-0 text-[11px] px-2.5 py-1 rounded-lg bg-[#20242D] text-[#7C766B] font-mono font-medium border border-[#2E333F]">STANDBY</span>
                    </div>
                    <div class="grid grid-cols-3 gap-2 pt-2 border-t border-[#242833]">
                        <button onclick="setSuspectMode('TRAP')" id="btn-mode-trap" class="seg-btn on-trap px-2 py-2 rounded-xl text-xs font-bold">🎯 Trap</button>
                        <button onclick="setSuspectMode('MONITOR')" id="btn-mode-monitor" class="seg-btn px-2 py-2 rounded-xl text-xs font-bold">👁 Monitor</button>
                        <button onclick="setSuspectMode('STANDBY')" id="btn-mode-standby" class="seg-btn px-2 py-2 rounded-xl text-xs font-bold">Standby</button>
                    </div>
                </div>
                <div class="mt-3 text-[11px] text-[#7C766B] leading-relaxed">
                    Control via Phone 1 self-chat:
                    <code class="font-mono bg-black/40 px-1 py-0.5 rounded text-[#E11D48] font-semibold">trap &lt;number&gt;</code>,
                    <code class="font-mono bg-black/40 px-1 py-0.5 rounded text-[#9FB5A3] font-semibold">monitor &lt;number&gt;</code>,
                    <code class="font-mono bg-black/40 px-1 py-0.5 rounded text-[#93C5FD] font-semibold">status</code>
                </div>
            </section>

            <!-- KILL-CHAIN FUNNEL -->
            <section class="card p-4">
                <div class="flex items-center justify-between mb-1">
                    <div class="flex items-center gap-2"><span class="text-sm">🧭</span><span class="label-xs">Attack Kill-Chain</span></div>
                    <span id="funnel-stage-chip" class="text-[11px] font-bold px-2 py-0.5 rounded-lg bg-[#2A0E16] text-[#E11D48] border border-[#4D1222] font-mono">APPROACH</span>
                </div>
                <div id="funnel-steps" class="mt-3 space-y-0"></div>
                <div class="mt-3 grid grid-cols-2 gap-2">
                    <div class="rounded-xl bg-[#1D1017] border border-[#4D1222] p-2.5 text-center">
                        <div class="text-[10px] font-bold uppercase tracking-wider text-[#E11D48]">Stalled by AI</div>
                        <div class="text-lg font-extrabold text-[#FAF7F2] font-mono num" id="funnel-money">₹0</div>
                    </div>
                    <div class="rounded-xl bg-[#172119] border border-[#2C3C2F] p-2.5 text-center">
                        <div class="text-[10px] font-bold uppercase tracking-wider text-[#9FB5A3]">Payments Blocked</div>
                        <div class="text-lg font-extrabold text-[#9FB5A3] font-mono num" id="funnel-blocked">0</div>
                    </div>
                </div>
            </section>

            <!-- DOSSIER -->
            <section class="card p-4">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2"><span class="text-sm">🕵</span><span class="label-xs">Scammer Dossier</span></div>
                    <span id="dossier-risk-badge" class="text-[11px] font-bold px-2 py-0.5 rounded-lg bg-[#20242D] text-[#7C766B] border border-[#2E333F] font-mono">—</span>
                </div>
                <div class="flex items-center gap-4">
                    <div class="relative shrink-0">
                        <svg width="96" height="96" viewBox="0 0 96 96">
                            <circle cx="48" cy="48" r="40" fill="none" stroke="#20242D" stroke-width="10"/>
                            <circle id="risk-ring" cx="48" cy="48" r="40" fill="none" stroke="#9F1239" stroke-width="10"
                                    stroke-linecap="round" stroke-dasharray="251.3" stroke-dashoffset="251.3"
                                    transform="rotate(-90 48 48)" style="transition: stroke-dashoffset .6s ease, stroke .3s ease"/>
                        </svg>
                        <div class="absolute inset-0 flex flex-col items-center justify-center">
                            <span id="risk-score" class="text-xl font-extrabold font-mono num leading-none">0</span>
                            <span class="text-[9px] font-bold uppercase tracking-wider text-[#7C766B] mt-0.5">Risk</span>
                        </div>
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="label-xs mb-1">Aggression</div>
                        <svg viewBox="0 0 120 66" class="w-full max-w-[150px]">
                            <path d="M 12 60 A 48 48 0 0 1 108 60" fill="none" stroke="#20242D" stroke-width="10" stroke-linecap="round"/>
                            <path id="aggr-arc" d="M 12 60 A 48 48 0 0 1 108 60" fill="none" stroke="#9FB5A3" stroke-width="10"
                                  stroke-linecap="round" stroke-dasharray="151" stroke-dashoffset="151" style="transition: stroke-dashoffset .6s ease, stroke .3s ease"/>
                        </svg>
                        <div class="flex items-baseline gap-1.5 -mt-1">
                            <span id="aggr-value" class="text-base font-extrabold font-mono num">0</span>
                            <span id="aggr-label" class="text-[11px] font-semibold text-[#7C766B]">Calm</span>
                        </div>
                    </div>
                </div>
                <div class="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs border-t border-[#242833] pt-3">
                    <div class="min-w-0"><div class="label-xs">Playbook</div><div id="dossier-playbook" class="font-semibold text-[#FAF7F2] truncate">Awaiting pattern</div></div>
                    <div><div class="label-xs">Language</div><div id="dossier-language" class="font-semibold text-[#FAF7F2]">—</div></div>
                    <div><div class="label-xs">Payment Asks</div><div id="dossier-asks" class="font-semibold text-[#FAF7F2] font-mono num">0</div></div>
                    <div><div class="label-xs">Exchange Turns</div><div id="dossier-turns" class="font-semibold text-[#FAF7F2] font-mono num">0</div></div>
                </div>
            </section>

            <!-- SCAM-MIX DONUT -->
            <section class="card p-4">
                <div class="flex items-center justify-between mb-2">
                    <div class="flex items-center gap-2"><span class="text-sm">📊</span><span class="label-xs">Scam-Type Mix</span></div>
                    <span class="text-[11px] text-[#7C766B] font-mono num" id="donut-total">0 signals</span>
                </div>
                <div class="flex items-center gap-4">
                    <svg viewBox="0 0 120 120" class="w-[110px] h-[110px] shrink-0 -rotate-90">
                        <circle cx="60" cy="60" r="45" fill="none" stroke="#20242D" stroke-width="16"/>
                        <g id="donut-segments"></g>
                    </svg>
                    <div id="donut-legend" class="flex-1 min-w-0 space-y-1.5 text-xs overflow-y-auto custom-scroll max-h-[112px] pr-1">
                        <div class="text-[#4D5260] italic">No scam signals yet</div>
                    </div>
                </div>
            </section>

            <!-- MULE INTEL -->
            <section class="card p-4">
                <div class="flex items-center justify-between mb-3 pb-2 border-b border-[#242833]">
                    <div class="flex items-center gap-2"><span class="text-sm">💳</span><span class="label-xs">Captured Mule Intel</span></div>
                    <span id="ioc-count-badge" class="text-[11px] font-bold px-2 py-0.5 rounded-lg bg-[#172119] text-[#9FB5A3] border border-[#2C3C2F] font-mono">0 Found</span>
                </div>
                <div class="space-y-3 text-sm">
                    <div><span class="label-xs block mb-1.5">UPI IDs</span>
                        <div id="ioc-upi-list" class="flex flex-wrap gap-1.5"><span class="text-xs text-[#4D5260] italic">None extracted yet</span></div></div>
                    <div><span class="label-xs block mb-1.5">Phone Numbers</span>
                        <div id="ioc-phone-list" class="flex flex-wrap gap-1.5"><span class="text-xs text-[#4D5260] italic">None extracted yet</span></div></div>
                    <div><span class="label-xs block mb-1.5">Bank Accounts &amp; IFSC</span>
                        <div id="ioc-bank-list" class="flex flex-wrap gap-1.5"><span class="text-xs text-[#4D5260] italic">None extracted yet</span></div></div>
                </div>
            </section>

            <!-- ORIGIN MAP + CANARY -->
            <section class="card p-4">
                <div class="flex items-center justify-between mb-3">
                    <div class="flex items-center gap-2"><span class="text-sm">🌐</span><span class="label-xs">Canary IP Location Estimate</span></div>
                    <div class="flex items-center gap-1.5">
                        <button onclick="toggleMapBasemap()" id="btn-map-layer" class="text-[10px] font-bold px-2 py-1 rounded-lg bg-[#181B22] hover:bg-[#20242D] text-[#C8C1B5] border border-[#242833] transition flex items-center gap-1" title="Switch basemap (Street Map / Satellite)">
                            🛰️ <span id="map-layer-label">Satellite</span>
                        </button>
                        <button onclick="recenterMap()" class="text-[10px] font-bold px-2 py-1 rounded-lg bg-[#181B22] hover:bg-[#20242D] text-[#C8C1B5] border border-[#242833] transition flex items-center gap-1" title="Recenter on suspect">
                            🎯 Center
                        </button>
                        <span id="canary-status-badge" class="text-[11px] font-bold px-2 py-0.5 rounded-lg bg-[#20242D] text-[#7C766B] border border-[#2E333F] font-mono">0 Hits</span>
                    </div>
                </div>
                <div class="relative">
                    <div id="origin-map"></div>
                    <div id="map-empty" class="absolute inset-0 flex items-center justify-center text-xs text-[#C8C1B5] bg-[#0D0E12]/85 rounded-xl text-center px-4 pointer-events-none leading-relaxed">
                        No canary hit yet. Share the generated link to collect an approximate IP-based location. This is not device GPS.
                    </div>
                </div>
                <p class="mt-2 text-[10px] leading-relaxed text-[#7C766B]">Location is estimated from the visitor's public IP and may indicate an ISP/VPN gateway, not their physical address. No GPS permission or location API key is used.</p>
                <div id="canary-hits-box" class="mt-3 space-y-2">
                    <div class="text-xs text-[#4D5260] italic">Awaiting suspect interaction with Canary Link…</div>
                </div>
            </section>
        </aside>

        <!-- RIGHT: live conversation -->
        <section class="col-span-12 lg:col-span-8 card overflow-hidden flex flex-col min-h-0">
            <div class="px-4 sm:px-5 py-3 bg-[#181B22] border-b border-[#242833] flex items-center justify-between gap-3">
                <div class="flex items-center gap-3 min-w-0">
                    <div class="w-9 h-9 rounded-full bg-[#20242D] flex items-center justify-center text-[#C8C1B5] border border-[#2E333F] shrink-0">💬</div>
                    <div class="min-w-0">
                        <div class="flex items-center gap-2">
                            <span id="chat-header-phone" class="text-sm font-bold font-mono truncate">+91 Suspect Target</span>
                            <span id="chat-header-channel" class="hidden sm:inline text-[10px] font-bold px-1.5 py-0.5 rounded-md bg-[#20242D] text-[#7C766B] border border-[#2E333F] uppercase font-mono">whatsapp</span>
                            <span id="chat-header-pill" class="shrink-0 text-[11px] font-bold px-2 py-0.5 rounded-lg bg-[#20242D] text-[#7C766B] border border-[#2E333F] font-medium">STANDBY</span>
                        </div>
                        <p class="hidden sm:block text-xs text-[#7C766B]">Live WhatsApp Stream (Phone 1 ↔ Phone 2)</p>
                    </div>
                </div>
                <div class="hidden sm:block text-xs text-[#7C766B] font-mono num shrink-0" id="chat-total-msgs">0 messages</div>
            </div>

            <div id="chat-feed" class="flex-1 min-h-0 p-4 sm:p-5 bg-[#0A0B0E] overflow-y-auto custom-scroll space-y-4">
                <div class="text-center py-24 text-[#7C766B] text-sm">
                    <div class="inline-block px-5 py-2.5 rounded-full bg-[#14161C] border border-[#242833] text-xs font-medium">
                        🔒 End-to-end encrypted honeypot stream active. Waiting for messages from Phone 2…
                    </div>
                </div>
            </div>

            <!-- TYPING INDICATOR -->
            <div id="typing-indicator" class="hidden px-4 sm:px-5 pb-3 bg-[#0A0B0E]">
                <div class="inline-flex items-center gap-2 px-3.5 py-2.5 rounded-2xl bg-[#1A1D24] border border-[#282C37]">
                    <span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>
                    <span id="typing-text" class="text-xs font-medium text-[#C8C1B5] ml-1">AI is composing a reply…</span>
                </div>
            </div>

            <div class="px-4 sm:px-5 py-2.5 bg-[#101216] border-t border-[#242833] flex items-center gap-2.5">
                <span class="w-2 h-2 rounded-full bg-[#9FB5A3] pulse-ring shrink-0"></span>
                <span class="text-[11px] font-bold uppercase tracking-wider text-[#7C766B] shrink-0 font-heading">AI Engine</span>
                <span id="ai-live-thought" class="text-xs text-[#C8C1B5] font-medium truncate">Standing by for target lock…</span>
            </div>
        </section>
    </main>

    <!-- QR MODAL -->
    <div id="qr-modal" class="fixed inset-0 bg-[#0D0E12]/85 backdrop-blur-md z-50 flex items-center justify-center p-4 hidden" style="display:none">
        <div class="bg-[#14161C] border border-[#373D4D] rounded-3xl max-w-sm w-full p-6 shadow-2xl relative">
            <button onclick="toggleQrModal()" class="absolute top-4 right-4 text-[#7C766B] hover:text-[#FAF7F2] text-xl font-bold">✕</button>
            <div class="text-center space-y-1.5 mb-5">
                <div class="w-12 h-12 rounded-2xl bg-gradient-to-br from-[#9F1239] to-[#4D1222] text-[#FAF7F2] flex items-center justify-center mx-auto text-xl shadow-md border border-[#BE123C]/30">📱</div>
                <h3 class="text-base font-bold font-heading">Link Phone 1 (Victim WhatsApp)</h3>
                <p class="text-xs text-[#7C766B]">Scan this QR from WhatsApp → Settings → Linked Devices</p>
            </div>
            <div class="bg-[#FAF7F2] p-4 rounded-2xl flex items-center justify-center w-60 h-60 mx-auto shadow-xl" id="qr-canvas-holder">
                <div id="qr-loading-text" class="text-xs text-[#7C766B] font-medium text-center">Generating QR…<br/>Ensure wa_bridge.js is running.</div>
            </div>
        </div>
    </div>

    <!-- LOGIC -->
    <script>
        let currentQrString = "";
        let currentTargetPhone = "";
        let currentWaStatus = "";
        let serverSkew = 0;
        let originMap = null;
        let mapLayers = [];
        let lastRenderedChatLen = -1;
        let lastActivity = {};

        const DONUT_COLORS = ["#E11D48", "#9FB5A3", "#F59E0B", "#9F1239", "#60A5FA", "#A78BFA", "#2DD4BF", "#6B7280"];

        function esc(s) {
            return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
        }

        function formatPhoneNumber(raw) {
            if (!raw) return "+91 Suspect Target";
            const s = String(raw);
            if (/[A-Za-z]/.test(s)) return s.replace(/^\\+/, "");
            const d = s.replace(/[^\\d]/g, "");
            if (!d) return "+91 Suspect Target";
            if (d.length > 13) return `Private ID ${d.slice(0, 4)}…${d.slice(-4)}`;
            if (d.length === 12 && d.startsWith("91")) return `+91 ${d.slice(2, 7)} ${d.slice(7)}`;
            if (d.length === 10) return `+91 ${d.slice(0, 5)} ${d.slice(5)}`;
            if (d.length === 11 && d.startsWith("0")) return `+91 ${d.slice(1, 6)} ${d.slice(6)}`;
            if (d.length >= 11 && d.length <= 13) return `+${d.slice(0, d.length - 10)} ${d.slice(d.length - 10, d.length - 5)} ${d.slice(-5)}`;
            return `+${d}`;
        }

        function serverNow() { return Date.now() + serverSkew; }
        function fmtMoney(n) { return "₹" + Number(n || 0).toLocaleString("en-IN"); }

        function toggleQrModal() {
            const modal = document.getElementById("qr-modal");
            if (currentWaStatus === "CONNECTED" || currentWaStatus === "AUTHENTICATED") return;
            if (modal.style.display === "none" || modal.classList.contains("hidden")) {
                modal.classList.remove("hidden"); modal.style.display = "flex";
            } else {
                modal.classList.add("hidden"); modal.style.display = "none";
            }
        }

        async function setSuspectMode(mode) {
            if (!currentTargetPhone) {
                const promptNum = prompt("Enter target mobile number (e.g. 9876543210):");
                if (!promptNum) return;
                currentTargetPhone = promptNum.replace(/[^A-Za-z0-9_.\\-]/g, "");
            }
            try {
                await fetch("/api/set-target-mode", {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-API-Key": "__SCAMTRAP_API_KEY__" },
                    body: JSON.stringify({ phone_number: currentTargetPhone, mode: mode })
                });
                fetchState();
            } catch (err) { console.error("Set mode error:", err); }
        }

        async function resetDemoSession() {
            if (confirm("Reset current interception state and telemetry?")) {
                await fetch("/api/reset", { method: "POST", headers: { "X-API-Key": "__SCAMTRAP_API_KEY__" } });
                fetchState();
            }
        }

        function updateQrCode(rawQr) {
            if (!rawQr || rawQr === currentQrString) return;
            currentQrString = rawQr;
            const container = document.getElementById("qr-canvas-holder");
            container.innerHTML = "";
            new QRCode(container, { text: rawQr, width: 216, height: 216, colorDark: "#0D0E12", colorLight: "#FAF7F2", correctLevel: QRCode.CorrectLevel.M });
        }

        /* ---------- widgets ---------- */
        function renderFunnel(state) {
            const f = state.funnel || { stage: 0, stages: [], money_stalled: 0, blocked: 0, payment_asks: 0 };
            const stages = f.stages && f.stages.length ? f.stages : ["APPROACH","RAPPORT","PRESSURE","PAYMENT_ASK","BLOCKED"];
            const active = Math.max(0, Math.min(f.stage || 0, stages.length - 1));
            const box = document.getElementById("funnel-steps");
            box.innerHTML = stages.map((s, i) => {
                const done = i < active, cur = i === active;
                const dotCls = (done || cur) ? "bg-[#9F1239] text-[#FAF7F2] border border-[#BE123C]" + (cur ? " pulse-ring" : "")
                    : "bg-[#20242D] text-[#4D5260] border border-[#2E333F]";
                const labelCls = (done || cur) ? "text-[#FAF7F2]" : "text-[#4D5260]";
                const conn = i < stages.length - 1
                    ? `<div class="ml-[9px] w-px h-3 ${i < active ? 'bg-[#9F1239]' : 'bg-[#242833]'}"></div>` : "";
                const name = s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ");
                return `<div class="flex items-start gap-2.5">
                    <div class="w-[19px] h-[19px] rounded-full ${dotCls} flex items-center justify-center text-[10px] font-bold shrink-0">${done ? "✓" : (i + 1)}</div>
                    <div class="-mt-0.5">
                        <div class="text-xs font-bold ${labelCls}">${name}</div>
                        ${cur ? `<div class="text-[10px] text-[#E11D48] font-semibold">current stage</div>` : ""}
                    </div>
                </div>${conn}`;
            }).join("");
            document.getElementById("funnel-stage-chip").textContent = stages[active];
            document.getElementById("funnel-money").textContent = fmtMoney(f.money_stalled);
            document.getElementById("funnel-blocked").textContent = String(f.blocked || 0);
        }

        function renderDossier(state) {
            const d = state.dossier || {};
            const risk = Math.max(0, Math.min(100, d.risk_score || 0));
            const C = 251.3;
            const ring = document.getElementById("risk-ring");
            ring.style.strokeDashoffset = String(C - (risk / 100) * C);
            ring.setAttribute("stroke", risk < 30 ? "#9FB5A3" : risk < 55 ? "#F59E0B" : risk < 80 ? "#F97316" : "#E11D48");
            document.getElementById("risk-score").textContent = String(risk);
            const badge = document.getElementById("dossier-risk-badge");
            badge.textContent = d.risk_label || "—";
            const bl = (d.risk_label || "").toLowerCase();
            badge.className = "text-[11px] font-bold px-2 py-0.5 rounded-lg border font-mono " + (
                bl === "critical" || bl === "high" ? "bg-[#2A0E16] text-[#E11D48] border-[#4D1222]"
                : bl === "moderate" ? "bg-[#231A0E] text-[#F59E0B] border-[#4A3A1E]"
                : bl === "low" ? "bg-[#172119] text-[#9FB5A3] border-[#2C3C2F]"
                : "bg-[#20242D] text-[#7C766B] border-[#2E333F]");

            const agg = state.aggression || { current: 0, average: 0 };
            const a = Math.max(0, Math.min(100, agg.average || 0));
            const arcLen = 151;
            const arc = document.getElementById("aggr-arc");
            arc.style.strokeDashoffset = String(arcLen - (a / 100) * arcLen);
            arc.setAttribute("stroke", a < 30 ? "#9FB5A3" : a < 60 ? "#F59E0B" : "#E11D48");
            document.getElementById("aggr-value").textContent = String(agg.average || 0);
            document.getElementById("aggr-label").textContent = d.aggression_label || "Calm";

            document.getElementById("dossier-playbook").textContent = d.playbook || "Awaiting pattern";
            document.getElementById("dossier-language").textContent = d.language || "—";
            document.getElementById("dossier-asks").textContent = String(d.payment_asks || 0);
            document.getElementById("dossier-turns").textContent = String(d.turns || 0);
        }

        function renderDonut(state) {
            const counts = state.scam_type_counts || {};
            const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
            const total = entries.reduce((s, e) => s + e[1], 0);
            document.getElementById("donut-total").textContent = total + (total === 1 ? " signal" : " signals");
            const g = document.getElementById("donut-segments");
            const legend = document.getElementById("donut-legend");
            if (!total) {
                g.innerHTML = "";
                legend.innerHTML = `<div class="text-xs text-[#4D5260] italic">No scam signals yet</div>`;
                return;
            }
            const R = 45, C = 2 * Math.PI * R;
            let acc = 0;
            g.innerHTML = entries.map(([label, count], i) => {
                const frac = count / total;
                const color = DONUT_COLORS[i % DONUT_COLORS.length];
                const seg = `<circle cx="60" cy="60" r="${R}" fill="none" stroke="${color}" stroke-width="16"
                    stroke-dasharray="${(frac * C).toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-acc * C).toFixed(2)}"/>`;
                acc += frac;
                return seg;
            }).join("");
            legend.innerHTML = entries.slice(0, 6).map(([label, count], i) => `
                <div class="flex items-center justify-between gap-2">
                    <span class="flex items-center gap-1.5 min-w-0"><span class="w-2.5 h-2.5 rounded-sm shrink-0" style="background:${DONUT_COLORS[i % DONUT_COLORS.length]}"></span>
                    <span class="truncate text-[#C8C1B5]">${esc(label)}</span></span>
                    <b class="num text-[#FAF7F2] shrink-0">${count}</b>
                </div>`).join("");
        }

        let darkTileLayer = null;
        let satTileLayer = null;
        let isSatellite = false;
        let lastGeoHitsHash = "";

        function toggleMapBasemap() {
            if (!originMap) return;
            isSatellite = !isSatellite;
            const btnLabel = document.getElementById("map-layer-label");
            if (isSatellite) {
                if (darkTileLayer && originMap.hasLayer(darkTileLayer)) originMap.removeLayer(darkTileLayer);
                if (!satTileLayer) {
                    satTileLayer = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
                        maxZoom: 18,
                        attribution: "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community"
                    });
                }
                satTileLayer.addTo(originMap);
                if (btnLabel) btnLabel.textContent = "Street Map";
            } else {
                if (satTileLayer && originMap.hasLayer(satTileLayer)) originMap.removeLayer(satTileLayer);
                if (darkTileLayer) darkTileLayer.addTo(originMap);
                if (btnLabel) btnLabel.textContent = "Satellite";
            }
        }

        function recenterMap() {
            if (!originMap) return;
            const withGeo = (window.lastCanaryHits || []).map(h => ({
                hit: h, lat: Number(h.lat), lon: Number(h.lon)
            })).filter(({hit, lat, lon}) =>
                hit.lat !== null && hit.lat !== undefined && String(hit.lat).trim() !== "" &&
                hit.lon !== null && hit.lon !== undefined && String(hit.lon).trim() !== "" &&
                Number.isFinite(lat) && Number.isFinite(lon) &&
                lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
            );
            if (withGeo.length) {
                try {
                    originMap.fitBounds(L.latLngBounds(withGeo.map(({lat, lon}) => [lat, lon])).pad(0.35), {
                        animate: true, duration: 0.8, maxZoom: 10
                    });
                } catch (error) {
                    console.error("Could not center map on IP location estimates:", error);
                }
            } else {
                originMap.setView([22.5, 79.5], 4, { animate: true });
            }
        }

        function renderMap(hits) {
            window.lastCanaryHits = hits || [];
            const withGeo = (hits || []).map(h => {
                const consented = h.location_method === "browser_geolocation_consent";
                const latValue = consented ? h.lat : (h.ip_lat ?? h.lat);
                const lonValue = consented ? h.lon : (h.ip_lon ?? h.lon);
                return {
                    hit: h,
                    latValue,
                    lonValue,
                    lat: Number(latValue),
                    lon: Number(lonValue),
                    consented
                };
            }).filter(({latValue, lonValue, lat, lon}) =>
                latValue !== null && latValue !== undefined && String(latValue).trim() !== "" &&
                lonValue !== null && lonValue !== undefined && String(lonValue).trim() !== "" &&
                Number.isFinite(lat) && Number.isFinite(lon) &&
                lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
            );
            const emptyEl = document.getElementById("map-empty");
            if (emptyEl) {
                emptyEl.textContent = !(hits || []).length
                    ? "No canary hit yet. A visit may provide an approximate IP location; precise device location is optional and requires visitor permission."
                    : withGeo.length
                        ? ""
                        : "Canary hit received, but no valid location is available. IP-location lookup may be unavailable; device coordinates require visitor permission.";
                emptyEl.style.display = withGeo.length ? "none" : "flex";
            }
            if (typeof L === "undefined") return;

            if (!originMap) {
                originMap = L.map("origin-map", {
                    scrollWheelZoom: true,
                    zoomControl: true,
                    attributionControl: true,
                    doubleClickZoom: true,
                    dragging: true
                }).setView([22.5, 79.5], 4);

                darkTileLayer = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
                    maxZoom: 19,
                    attribution: "Tiles &copy; Esri &mdash; Sources: Esri, HERE, Garmin, FAO, NOAA, USGS, &copy; OpenStreetMap contributors, and the GIS User Community"
                }).addTo(originMap);

                setTimeout(() => { if (originMap) originMap.invalidateSize(); }, 300);
            }

            // Anti-jitter: Don't re-render or reset user zoom/pan if canary hits haven't changed!
            const currentHash = JSON.stringify(hits || []);
            if (currentHash === lastGeoHitsHash) {
                return;
            }
            lastGeoHitsHash = currentHash;

            mapLayers.forEach(m => originMap.removeLayer(m));
            mapLayers = [];

            const radarIcon = L.divIcon({
                className: 'custom-radar-container',
                html: '<div class="custom-radar-pin"><div class="radar-pulse"></div><div class="radar-core"></div></div>',
                iconSize: [24, 24],
                iconAnchor: [12, 12],
                popupAnchor: [0, -14]
            });

            withGeo.forEach(({hit: h, lat, lon, consented}) => {
                const marker = L.marker([lat, lon], { icon: radarIcon }).addTo(originMap);
                const locationLabel = consented
                    ? "VISITOR-SHARED DEVICE LOCATION"
                    : "APPROXIMATE IP LOCATION";
                const coordinateLabel = consented
                    ? `Device coordinates (±${Number(h.location_accuracy_m).toFixed(1)} m): ${lat.toFixed(4)}, ${lon.toFixed(4)}`
                    : `IP estimate: ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
                const secondaryIpLocation = consented && h.ip_lat != null && h.ip_lon != null
                    ? `<div style="margin-top: 4px; font-size: 10px; color: #7C766B;">Separate IP estimate: ${Number(h.ip_lat).toFixed(4)}, ${Number(h.ip_lon).toFixed(4)} · ${esc(h.ip_location || "approximate")}</div>`
                    : "";
                const popupContent = `
                    <div style="font-family: 'Inter', sans-serif; font-size: 12px; color: #FAF7F2; min-width: 200px;">
                        <div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #242833; padding-bottom: 6px; margin-bottom: 8px;">
                            <span style="font-weight: 800; font-family: 'JetBrains Mono', monospace; color: #E11D48; font-size: 13px;">${esc(h.ip)}</span>
                            <span style="font-size: 10px; background: #2A0E16; color: #E11D48; border: 1px solid #4D1222; padding: 2px 6px; border-radius: 4px; font-weight: 700;">SUSPECT</span>
                        </div>
                        <div style="margin-bottom: 4px;"><b style="color: #7C766B; font-size: 11px;">${locationLabel}:</b> <span style="font-weight: 600; color: #FAF7F2;">📍 ${esc(h.location || "Unknown")}</span></div>
                        <div style="margin-bottom: 4px;"><b style="color: #7C766B; font-size: 11px;">NETWORK:</b> <span style="color: #C8C1B5;">${esc([h.isp, h.as_num].filter(Boolean).join(" · ") || "Unknown ISP")}</span></div>
                        <div style="margin-bottom: 6px;"><b style="color: #7C766B; font-size: 11px;">DEVICE:</b> <span style="color: #9FB5A3;">${esc(h.os_device || "Unknown")}</span></div>
                        <div style="display: flex; justify-content: space-between; align-items: center; background: #181B22; padding: 4px 8px; border-radius: 6px; border: 1px solid #242833; font-family: 'JetBrains Mono', monospace; font-size: 10px; color: #7C766B;">
                            <span>${coordinateLabel}</span>
                            <span>${esc(h.timestamp ? h.timestamp.split(" ")[1] : "")}</span>
                        </div>
                        ${secondaryIpLocation}
                        <div style="margin-top: 6px; font-size: 10px; color: #7C766B;">${consented ? "Coordinates shared by visitor after browser permission." : "Approximate network location; not device GPS."}</div>
                    </div>`;
                marker.bindPopup(popupContent);
                mapLayers.push(marker);
            });

            if (withGeo.length) {
                try {
                    originMap.fitBounds(L.latLngBounds(withGeo.map(({lat, lon}) => [lat, lon])).pad(0.35), {
                        animate: true, maxZoom: 10
                    });
                } catch (error) {
                    console.error("Could not fit map to canary locations:", error);
                }
                setTimeout(() => originMap && originMap.invalidateSize(), 250);
            } else {
                originMap.setView([22.5, 79.5], 4);
            }
        }

        function renderCanary(hits) {
            const box = document.getElementById("canary-hits-box");
            document.getElementById("canary-status-badge").textContent = (hits || []).length + " Hits";
            if (!hits || !hits.length) {
                box.innerHTML = `<div class="text-xs text-[#4D5260] italic">Awaiting suspect interaction with Canary Link…</div>`;
                return;
            }
            box.innerHTML = hits.slice().reverse().map(h => {
                const coords = (h.lat !== null && h.lat !== undefined && h.lon !== null && h.lon !== undefined)
                    ? `<span class="text-[10px] text-[#7C766B] font-mono">${Number(h.lat).toFixed(2)}, ${Number(h.lon).toFixed(2)}</span>` : "";
                return `
                <div class="p-3 rounded-xl bg-[#181B22] border border-[#4D1222] space-y-1">
                    <div class="flex items-center justify-between gap-2">
                        <span class="font-bold text-[#E11D48] font-mono text-sm break-all">${esc(h.ip)}</span>
                        <span class="text-[10px] text-[#7C766B] font-mono shrink-0">${esc(h.timestamp || "")}</span>
                    </div>
                    <div class="text-xs text-[#FAF7F2] font-medium break-words">📍 ${esc(h.location || "Location unavailable")}</div>
                    <div class="text-[10px] text-[#7C766B]">${h.location_method === "browser_geolocation_consent" ? `Visitor-shared device location · accuracy ±${Number(h.location_accuracy_m).toFixed(1)} m` : h.geo_status === "unavailable" ? "IP-location lookup failed; check backend internet access/provider limits." : "Approximate IP-based estimate · not device GPS"}</div>
                    ${h.location_method === "browser_geolocation_consent" && h.ip_location ? `<div class="text-[10px] text-[#7C766B]">Separate approximate IP estimate: ${esc(h.ip_location)}</div>` : ""}
                    ${h.geo_source ? `<div class="text-[10px] text-[#7C766B]">Lookup: ${esc(h.geo_source)}</div>` : ""}
                    <div class="flex items-center justify-between gap-2">
                        <span class="text-[11px] text-[#C8C1B5] break-words">🌐 ${esc([h.isp, h.as_num].filter(Boolean).join(" · ") || "Unknown ISP")}</span>
                        ${coords}
                    </div>
                    <div class="text-[11px] text-[#9FB5A3] font-medium">${esc(h.os_device || "Unknown Device")}</div>
                </div>`;
            }).join("");
        }

        function setModeUI(isTrapped, isMonitored) {
            const segs = {
                TRAP: document.getElementById("btn-mode-trap"),
                MONITOR: document.getElementById("btn-mode-monitor"),
                STANDBY: document.getElementById("btn-mode-standby")
            };
            const active = isTrapped ? "TRAP" : isMonitored ? "MONITOR" : "STANDBY";
            Object.entries(segs).forEach(([key, el]) => {
                el.classList.remove("on-trap", "on-monitor", "on-standby");
                if (key === active) el.classList.add(key === "TRAP" ? "on-trap" : key === "MONITOR" ? "on-monitor" : "on-standby");
            });

            const badge = document.getElementById("target-trap-badge");
            const modePill = document.getElementById("target-mode-pill");
            const headerPill = document.getElementById("chat-header-pill");
            let cls, txt, pillTxt;
            if (isTrapped) { cls = "bg-[#2A0E16] text-[#E11D48] border-[#4D1222]"; txt = "🎯 ACTIVE TRAP"; pillTxt = "AI HIJACK"; }
            else if (isMonitored) { cls = "bg-[#172119] text-[#9FB5A3] border-[#2C3C2F]"; txt = "👁 MONITORING"; pillTxt = "MONITOR"; }
            else { cls = "bg-[#20242D] text-[#7C766B] border-[#2E333F]"; txt = "STANDBY"; pillTxt = "STANDBY"; }
            badge.className = "text-[11px] px-2.5 py-1 rounded-lg font-mono font-semibold border " + cls;
            badge.textContent = txt;
            modePill.className = "shrink-0 text-[11px] px-2.5 py-1 rounded-lg font-mono font-semibold border " + cls;
            modePill.textContent = pillTxt;
            headerPill.className = "shrink-0 text-[11px] font-bold px-2 py-0.5 rounded-lg border " + cls;
            headerPill.textContent = pillTxt;
        }

        function renderChat(state) {
            const all = state.scammer_chat || [];
            // Track visible_after_ms metadata for timeline analytics if present
            document.getElementById("chat-total-msgs").textContent = all.length + " messages";
            const feed = document.getElementById("chat-feed");

            if (!all.length) {
                if (lastRenderedChatLen !== 0) {
                    feed.innerHTML = `<div class="text-center py-24 text-[#7C766B] text-sm">
                        <div class="inline-block px-5 py-2.5 rounded-full bg-[#14161C] border border-[#242833] text-xs font-medium">
                            🔒 End-to-end encrypted honeypot stream active. Waiting for messages from Phone 2…
                        </div></div>`;
                    lastRenderedChatLen = 0;
                }
                return;
            }
            if (all.length === lastRenderedChatLen) return;
            lastRenderedChatLen = all.length;

            feed.innerHTML = all.map(m => {
                const isAI = m.role === "user_ai";
                const sender = isAI ? (m.sender || "ScamTrap AI Honeypot") : formatPhoneNumber(m.sender);
                const platform = esc((m.platform || "whatsapp").toUpperCase());
                return `
                <div class="flex flex-col ${isAI ? "items-end" : "items-start"} space-y-1.5">
                    <div class="max-w-[80%] px-4 py-3 text-[15px] ${isAI ? "wa-bubble-ai shadow-md" : "wa-bubble-scammer shadow-md"}">
                        <div class="flex items-center gap-1.5 mb-1.5">
                            <span class="text-xs font-bold ${isAI ? "text-[#9FB5A3]" : "text-[#C8C1B5]"} font-mono">${esc(sender)}</span>
                            <span class="text-[9px] font-bold px-1 py-0.5 rounded ${isAI ? "bg-[#2B4534] text-[#9FB5A3]" : "bg-[#20242D] text-[#7C766B]"}">${platform}</span>
                        </div>
                        ${m.media ? `
                            <div class="mb-2 rounded-xl overflow-hidden border border-[#2C3C2F] bg-black/40 p-2">
                                <img src="${m.media}" class="w-full max-h-60 object-cover rounded-lg cursor-pointer hover:opacity-90 transition-opacity" onclick="window.open('${m.media}')" alt="Fake UPI Glitch Receipt"/>
                                <div class="text-[11px] text-[#9FB5A3] mt-1.5 text-center font-mono font-semibold flex items-center justify-center gap-1">
                                    <span>⚠️</span> Fake NPCI U16 Glitch Screenshot
                                </div>
                            </div>` : ""}
                        <div class="leading-relaxed whitespace-pre-wrap break-words">${esc(m.text)}</div>
                        ${m.trap_url ? `
                            <a href="${m.trap_url}" target="_blank" class="mt-2.5 block p-2.5 rounded-xl bg-black/40 border border-[#BE123C]/50 text-xs font-mono break-all hover:bg-black/60 transition-colors">
                                🔗 Canary Link: <span class="text-[#E11D48] font-bold">${esc(m.trap_url)}</span>
                            </a>` : ""}
                        <div class="text-[11px] text-[#7C766B] text-right mt-1.5 font-mono num">${esc(m.time || "")}</div>
                    </div>
                </div>`;
            }).join("");
            feed.scrollTop = feed.scrollHeight;
        }

        function updateTyping(state) {
            const act = state.ai_activity || {};
            const el = document.getElementById("typing-indicator");
            const visible = act.active && serverNow() < (act.until_ms || 0);
            if (visible) {
                document.getElementById("typing-text").textContent =
                    act.phase === "thinking" ? "AI is analysing the message…" : "AI is composing a reply…";
                el.classList.remove("hidden");
                const feed = document.getElementById("chat-feed");
                feed.scrollTop = feed.scrollHeight;
            } else {
                el.classList.add("hidden");
            }
        }

        async function fetchState() {
            try {
                const res = await fetch("/api/state", { headers: { "X-API-Key": "__SCAMTRAP_API_KEY__" } });
                const state = await res.json();
                if (state.server_time_ms) serverSkew = state.server_time_ms - Date.now();
                currentWaStatus = state.wa_status || "";

                /* connection pill */
                const dot = document.getElementById("wa-status-dot");
                const txt = document.getElementById("wa-status-text");
                const qrModal = document.getElementById("qr-modal");
                if (state.wa_status === "CONNECTED" || state.wa_status === "AUTHENTICATED") {
                    dot.className = "w-2.5 h-2.5 rounded-full bg-[#9FB5A3] animate-pulse shrink-0";
                    txt.textContent = `Phone 1 linked (${formatPhoneNumber(state.wa_phone)})`;
                    qrModal.classList.add("hidden"); qrModal.style.display = "none";
                } else if (state.wa_status === "QR_READY") {
                    dot.className = "w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse shrink-0";
                    txt.textContent = "Scan QR to Link Phone 1";
                    updateQrCode(state.wa_qr_raw);
                } else {
                    dot.className = "w-2.5 h-2.5 rounded-full bg-[#E11D48] shrink-0";
                    txt.textContent = "WhatsApp Disconnected";
                }

                /* target: commanded target wins, else latest thread */
                const threads = Object.values(state.incoming_threads || {});
                if (state.target_scammer) currentTargetPhone = String(state.target_scammer).replace("+", "");
                else if (threads.length) currentTargetPhone = threads[threads.length - 1].sender_number;

                const display = formatPhoneNumber(currentTargetPhone);
                document.getElementById("target-phone-display").textContent = display;
                document.getElementById("chat-header-phone").textContent = display;

                const activeThread = (state.incoming_threads || {})[currentTargetPhone] || threads[threads.length - 1] || {};
                const channel = (activeThread.platform || "whatsapp").toUpperCase();
                document.getElementById("target-channel-chip").textContent = channel;
                document.getElementById("chat-header-channel").textContent = channel;

                const isTrapped = !!currentTargetPhone && (state.trapped_numbers || []).includes(currentTargetPhone);
                const isMonitored = !!currentTargetPhone && (state.monitored_numbers || []).includes(currentTargetPhone);
                setModeUI(isTrapped, isMonitored);

                /* intel */
                const upis = state.extracted_intel.upi_ids || [];
                const phones = state.extracted_intel.phone_numbers || [];
                const banks = state.extracted_intel.bank_accounts || [];
                const ifscs = state.extracted_intel.ifsc_codes || [];
                const iocTotal = upis.length + phones.length + banks.length + ifscs.length;
                document.getElementById("ioc-count-badge").textContent = `${iocTotal} Found`;
                document.getElementById("ioc-upi-list").innerHTML = upis.length
                    ? upis.map(u => `<span class="px-2.5 py-1 rounded-lg bg-[#172119] text-[#9FB5A3] border border-[#2C3C2F] font-mono text-xs font-semibold tracking-wide break-all">${esc(u)}</span>`).join("")
                    : `<span class="text-xs text-[#4D5260] italic">None extracted yet</span>`;
                document.getElementById("ioc-phone-list").innerHTML = phones.length
                    ? phones.map(p => `<span class="px-2.5 py-1 rounded-lg bg-[#2A0E16] text-[#E11D48] border border-[#4D1222] font-mono text-xs font-semibold tracking-wide">${esc(formatPhoneNumber(p))}</span>`).join("")
                    : `<span class="text-xs text-[#4D5260] italic">None extracted yet</span>`;
                const bankList = [...banks.map(b => `A/C: ${b}`), ...ifscs.map(i => `IFSC: ${i}`)];
                document.getElementById("ioc-bank-list").innerHTML = bankList.length
                    ? bankList.map(b => `<span class="px-2.5 py-1 rounded-lg bg-[#181B22] text-[#FAF7F2] border border-[#373D4D] font-mono text-xs font-semibold tracking-wide">${esc(b)}</span>`).join("")
                    : `<span class="text-xs text-[#4D5260] italic">None extracted yet</span>`;

                /* widgets */
                renderFunnel(state);
                renderDossier(state);
                renderDonut(state);
                renderCanary(state.canary_hits || []);
                renderMap(state.canary_hits || []);
                renderChat(state);
                lastActivity = state.ai_activity || {};
                updateTyping(state);

                const thoughts = state.thought_logs || [];
                if (thoughts.length) {
                    const latest = thoughts[thoughts.length - 1];
                    document.getElementById("ai-live-thought").textContent = `[${latest.tag || "AI"}] ${latest.thought || ""}`;
                }

                document.getElementById("kpi-msgs").textContent = String((state.scammer_chat || []).length);
                document.getElementById("kpi-iocs").textContent = String(iocTotal);
                document.getElementById("kpi-hits").textContent = String((state.canary_hits || []).length);
            } catch (err) { console.error("State poll error:", err); }
        }

        setInterval(fetchState, 1500);
        setInterval(() => updateTyping({ ai_activity: lastActivity }), 500);
        fetchState();
    </script>
</body>
</html>
"""
    # Inject the session API key so the browser dashboard stays authenticated
    # when it is reached through a public tunnel.
    html_content = html_content.replace("__SCAMTRAP_API_KEY__", API_KEY)
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if not PUBLIC_TUNNEL_URL:
        threading.Thread(target=_auto_tunnel_worker, daemon=True, name="cloudflared-tunnel").start()
    print(f"🛡️  ScamTrap AI listening on http://{host}:{port}")
    print(f"🔑 SCAMTRAP_API_KEY: {API_KEY}")
    if host == "127.0.0.1":
        print("ℹ️  Bound to loopback only. Set HOST=0.0.0.0 to expose it on your LAN.")
    uvicorn.run(app, host=host, port=port, log_level="info")
