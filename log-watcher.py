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

# ── Event patterns ────────────────────────────────────────────────────────────
PATTERNS = [
    # Model selected as primary
    (re.compile(r'\[gateway\] agent model: (.+)$'),
     lambda m: ("model_selected", m.group(1).strip(), f"Primary model: {m.group(1).strip()}", None)),

    # Sub-agent spawned (session key contains 'subagent')
    (re.compile(r'subagent[:/]([a-f0-9\-]{36}).*?model[=: ]+([\w/.\-]+)', re.I),
     lambda m: ("subagent_spawned", m.group(2), f"Sub-agent spawned → {m.group(2)}", m.group(1))),

    # Rate limit / all models failed
    (re.compile(r'All models failed.*?(anthropic/[\w.\-]+).*?rate_limit', re.S),
     lambda m: ("rate_limit", m.group(1), "⚠️ Rate limit — all Claude models exhausted", None)),

    # Subagent completion
    (re.compile(r'Subagent announce completion'),
     lambda m: ("subagent_complete", None, "Sub-agent task completed", None)),

    # Config reload / model change
    (re.compile(r'\[reload\] config change detected'),
     lambda m: ("config_reload", None, "Config reloaded", None)),

    # Model fallback
    (re.compile(r'trying fallback.*?(anthropic/[\w.\-]+|google/[\w.\-]+)', re.I),
     lambda m: ("model_fallback", m.group(1), f"Falling back to {m.group(1)}", None)),
]

TS_RE    = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
SESS_RE  = re.compile(r'sessionKey=([^\s,]+)')
MODEL_RE = re.compile(r'(anthropic/[\w.\-]+|google/[\w.\-]+)')

def push_event(event_type, model, message, session_key=None, metadata=None):
    payload = json.dumps({
        "event_type":  event_type,
        "model":       model,
        "message":     message,
        "session_key": session_key,
        "metadata":    metadata or {},
    }).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:
        print(f"[push error] {e}")

def parse_line(line):
    ts_m = TS_RE.match(line)
    if not ts_m:
        return

    for pattern, extractor in PATTERNS:
        m = pattern.search(line)
        if m:
            try:
                event_type, model, message, session_key = extractor(m)
                # Try to grab session key from line if not extracted
                if not session_key:
                    sm = SESS_RE.search(line)
                    session_key = sm.group(1) if sm else None
                push_event(event_type, model, message, session_key)
                print(f"[{ts_m.group(1)}] {event_type} | {model} | {message}")
            except Exception as e:
                print(f"[parse error] {e}")
            return  # one event per line

def tail_log(path):
    """Tail a file from the end, yielding new lines as they appear."""
    with open(path, "r", errors="replace") as f:
        f.seek(0, 2)  # seek to end
        print(f"[watcher] Tailing {path} — pushing events to Supabase...")
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                time.sleep(0.5)

def main():
    while not LOG_PATH.exists():
        print(f"[watcher] Waiting for {LOG_PATH}...")
        time.sleep(5)

    for line in tail_log(LOG_PATH):
        parse_line(line.strip())

if __name__ == "__main__":
    main()
