#!/usr/bin/env python3
"""
MetaVision AI Pipeline Watcher
Tails gateway.log in real-time, parses events, pushes to Supabase.
Runs as a launchd daemon — zero agent/token involvement.
"""

import re, json, time, urllib.request, os
from datetime import datetime, timezone
from pathlib import Path

SUPABASE_URL = "https://pxwlwtlowvhytiyepjzk.supabase.co"
SUPABASE_KEY = "sb_publishable_c7zkCkMIifUa3DNS4UfLeA_DNP9Uill"
LOG_PATH     = Path.home() / ".openclaw/logs/gateway.log"
ENDPOINT     = f"{SUPABASE_URL}/rest/v1/pipeline_events"

# Model pricing (cost per call, ~2k tokens avg)
PRICING = {
    "claude-sonnet-4-6":  0.018,
    "claude-opus-4-6":    0.090,
    "claude-haiku-4-5":   0.004,
    "claude-sonnet":      0.018,
    "claude-opus":        0.090,
    "claude-haiku":       0.004,
    "gemini-3.1-pro":     0.006,
    "gemini-3-flash":     0.0004,
    "gemini-flash":       0.0004,
    "gemini-pro":         0.006,
}

def cost_for(model):
    if not model: return 0.0
    m = model.lower()
    for key, price in PRICING.items():
        if key in m: return price
    return 0.005

# Track open spans: runId -> {start_ms, model, event_type}
_open_spans = {}

TS_RE    = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([.+]\S+)?')
MODEL_RE = re.compile(r'(anthropic/[\w.\-]+|google/[\w.\-]+)')
SESS_RE  = re.compile(r'sessionKey=([^\s,]+)')
RUNID_RE = re.compile(r'runId=([^\s,]+)')

# ── Event patterns ────────────────────────────────────────────────────────────
PATTERNS = [
    (re.compile(r'\[gateway\] agent model: (.+)$'),
     "model_selected",
     lambda m: (m.group(1).strip(), f"Primary model active: {m.group(1).strip()}", None)),

    (re.compile(r'All models failed.*?rate_limit', re.S),
     "rate_limit",
     lambda m: (None, "⚠️ Rate limit — all Claude models exhausted", None)),

    (re.compile(r'Subagent announce completion'),
     "subagent_complete",
     lambda m: (None, "Sub-agent task completed", None)),

    (re.compile(r'\[reload\] config change detected'),
     "config_reload",
     lambda m: (None, "Config reloaded", None)),

    (re.compile(r'trying fallback.*?(anthropic/[\w.\-]+|google/[\w.\-]+)', re.I),
     "model_fallback",
     lambda m: (m.group(1), f"Falling back to {m.group(1)}", None)),
]

def push_event(event_type, model, message, session_key=None, duration_ms=None, metadata=None):
    payload = json.dumps({
        "event_type":  event_type,
        "model":       model,
        "message":     message,
        "session_key": session_key,
        "duration_ms": duration_ms,
        "metadata":    metadata or {
            "cost_estimate": round(cost_for(model), 6) if model else 0,
            "provider": "anthropic" if model and "claude" in model.lower() else ("google" if model else None),
        },
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5): pass
    except Exception as e:
        print(f"[push error] {e}")

def parse_line(line):
    ts_m = TS_RE.match(line)
    if not ts_m: return
    now_ms = int(time.time() * 1000)

    # Session key
    sm = SESS_RE.search(line)
    session_key = sm.group(1) if sm else None

    # Run ID for duration tracking
    rm = RUNID_RE.search(line)
    run_id = rm.group(1) if rm else None

    # Model from line
    mm = MODEL_RE.search(line)
    line_model = mm.group(0) if mm else None

    for pattern, event_type, extractor in PATTERNS:
        m = pattern.search(line)
        if m:
            try:
                model, message, extra = extractor(m)
                model = model or line_model
                duration_ms = None

                # Track spans for duration
                if event_type == "subagent_complete" and run_id and run_id in _open_spans:
                    span = _open_spans.pop(run_id)
                    duration_ms = now_ms - span["start_ms"]
                    message = f"{message} ({round(duration_ms/1000,1)}s)"
                elif run_id:
                    _open_spans[run_id] = {"start_ms": now_ms, "model": model}

                push_event(event_type, model, message, session_key, duration_ms)
                print(f"[{ts_m.group(1)}] {event_type} | {model} | {message}")
            except Exception as e:
                print(f"[parse error] {e}")
            return

def tail_log(path):
    with open(path, "r", errors="replace") as f:
        f.seek(0, 2)
        print(f"[watcher] Tailing {path} — pushing events to Supabase...")
        while True:
            line = f.readline()
            if line: yield line
            else: time.sleep(0.5)

def main():
    while not LOG_PATH.exists():
        print(f"[watcher] Waiting for {LOG_PATH}...")
        time.sleep(5)
    for line in tail_log(LOG_PATH):
        parse_line(line.strip())

if __name__ == "__main__":
    main()
