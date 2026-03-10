#!/usr/bin/env python3
"""
MetaVision AI Usage Cockpit — Data Generator
Parses ~/.openclaw/logs/gateway.log + credit-budget.json
Outputs usage-data.json for the GitHub Pages dashboard.
Run via cron every hour.
"""

import json, re, os
from datetime import datetime, date, timezone
from collections import defaultdict
from pathlib import Path

REPO_DIR    = Path(__file__).parent
LOG_PATH    = Path.home() / ".openclaw/logs/gateway.log"
BUDGET_PATH = Path.home() / ".openclaw/credit-budget.json"
ENV_PATH    = REPO_DIR / ".env.local"
OUT_PATH    = REPO_DIR / "usage-data.json"

# Load admin key from .env.local
ANTHROPIC_ADMIN_KEY = None
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("ANTHROPIC_ADMIN_KEY="):
            ANTHROPIC_ADMIN_KEY = line.split("=", 1)[1].strip()

# ── Pricing (per 1M tokens, input/output average estimate) ──────────────────
PRICING = {
    "claude-sonnet-4-6":   0.009,   # $3 in / $15 out avg ~$9/MTok
    "claude-opus-4-6":     0.045,   # $15 in / $75 out avg
    "claude-haiku-4-5":    0.0024,  # $0.8 in / $4 out avg
    "claude-sonnet-4-5":   0.009,
    "claude-opus-4-5":     0.045,
    "claude-haiku":        0.0024,
    "claude-sonnet":       0.009,
    "claude-opus":         0.045,
    "gemini-3.1-pro":      0.003,   # $1.25 in / $5 out avg
    "gemini-flash":        0.0002,  # $0.075 in / $0.30 out avg
    "gemini-pro":          0.003,
}
AVG_TOKENS_PER_CALL = 2000  # rough estimate per call

def price_for(model_id):
    m = model_id.lower()
    for key, price in PRICING.items():
        if key in m:
            return price
    return 0.005  # fallback

def classify_model(model_id):
    m = model_id.lower()
    if "haiku" in m:         return "haiku"
    if "sonnet" in m:        return "sonnet"
    if "opus" in m:          return "opus"
    if "flash" in m:         return "gemini-flash"
    if "gemini" in m:        return "gemini-pro"
    return "other"

def family_label(cls):
    return {
        "haiku":        "Claude Haiku",
        "sonnet":       "Claude Sonnet",
        "opus":         "Claude Opus",
        "gemini-flash": "Gemini Flash",
        "gemini-pro":   "Gemini Pro",
        "other":        "Other",
    }.get(cls, cls)

# ── Parse log ────────────────────────────────────────────────────────────────
date_re   = re.compile(r'^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})')
model_re  = re.compile(r'(anthropic|google)/(claude[^\s\|,:")\]]+|gemini[^\s\|,:")\]]+)')
rl_re     = re.compile(r'rate.?limit', re.I)
err_re    = re.compile(r'errorCode=UNAVAILABLE', re.I)

usage_by_day  = defaultdict(lambda: defaultdict(int))   # date -> family -> count
raw_by_day    = defaultdict(lambda: defaultdict(int))   # date -> full_id -> count
rate_events   = []
errors_today  = 0
today_str     = date.today().isoformat()

if LOG_PATH.exists():
    with open(LOG_PATH, errors="replace") as f:
        for line in f:
            dm = date_re.match(line)
            if not dm:
                continue
            day  = dm.group(1)
            time = dm.group(2)

            models = model_re.findall(line)
            for provider, model in models:
                if ".params" in model or model.endswith(")"):
                    continue
                full = f"{provider}/{model}"
                cls  = classify_model(model)
                usage_by_day[day][cls] += 1
                raw_by_day[day][full]  += 1

            if rl_re.search(line) or err_re.search(line):
                if day == today_str:
                    errors_today += 1
                    rate_events.append({
                        "time": time,
                        "date": day,
                        "msg":  line[line.find("errorMessage="):line.find("errorMessage=")+120].replace("errorMessage=","").strip() if "errorMessage=" in line else "Rate limit / error"
                    })

# ── Budget ───────────────────────────────────────────────────────────────────
budget = {"spend_cap": None, "set_at": None}
if BUDGET_PATH.exists():
    with open(BUDGET_PATH) as f:
        b = json.load(f)
        budget["spend_cap"] = b.get("balance")   # "balance" = configured cap, not remaining
        budget["set_at"]    = b.get("setAt")

# ── Build per-day chart data (last 14 days) ──────────────────────────────────
from datetime import timedelta
import urllib.request, urllib.parse

def fetch_anthropic_usage(starting_at, ending_at=None):
    """Fetch real token usage from Anthropic Admin API."""
    if not ANTHROPIC_ADMIN_KEY:
        return []
    params = f"starting_at={starting_at}&bucket_width=1d&group_by[]=model"
    if ending_at:
        params += f"&ending_at={ending_at}"
    url = f"https://api.anthropic.com/v1/organizations/usage_report/messages?{params}"
    req = urllib.request.Request(url, headers={
        "x-api-key": ANTHROPIC_ADMIN_KEY,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("data", [])
    except Exception as e:
        print(f"⚠️  Anthropic API error: {e}")
        return []

# Published pricing (per million tokens)
MODEL_PRICING = {
    "claude-sonnet-4-6":  {"input": 3.0,  "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75},
    "claude-opus-4-6":    {"input": 15.0, "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75},
    "claude-haiku-4-5":   {"input": 0.8,  "output": 4.0,   "cache_read": 0.08, "cache_write": 1.0},
    "claude-sonnet-4-5":  {"input": 3.0,  "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75},
    "claude-opus-4-5":    {"input": 15.0, "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75},
}

def calc_cost(result):
    model_id = (result.get("model") or "").lower()
    pricing  = None
    for key, p in MODEL_PRICING.items():
        if key in model_id:
            pricing = p
            break
    if not pricing:
        pricing = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}

    inp    = result.get("uncached_input_tokens", 0)
    out    = result.get("output_tokens", 0)
    cached = result.get("cache_read_input_tokens", 0)
    cc     = result.get("cache_creation", {})
    cw     = cc.get("ephemeral_5m_input_tokens", 0) + cc.get("ephemeral_1h_input_tokens", 0)
    M      = 1_000_000
    return (inp * pricing["input"] + out * pricing["output"] +
            cached * pricing["cache_read"] + cw * pricing["cache_write"]) / M
last14 = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]

chart = {
    "labels": [d[5:] for d in last14],   # MM-DD
    "sonnet":       [usage_by_day[d].get("sonnet", 0)       for d in last14],
    "opus":         [usage_by_day[d].get("opus", 0)         for d in last14],
    "haiku":        [usage_by_day[d].get("haiku", 0)        for d in last14],
    "gemini_pro":   [usage_by_day[d].get("gemini-pro", 0)   for d in last14],
    "gemini_flash": [usage_by_day[d].get("gemini-flash", 0) for d in last14],
}

# ── Today's model breakdown with cost estimates ───────────────────────────────
today_models = []
for full_id, count in sorted(raw_by_day.get(today_str, {}).items(), key=lambda x: -x[1]):
    provider_part, model_part = full_id.split("/", 1)
    est_cost = count * (AVG_TOKENS_PER_CALL / 1_000_000) * price_for(model_part) * 1_000_000
    today_models.append({
        "model":    full_id,
        "calls":    count,
        "provider": provider_part,
        "family":   family_label(classify_model(model_part)),
        "est_cost": round(est_cost, 4),
    })

# ── Overall cost split (last 14 days) ────────────────────────────────────────
cost_split = defaultdict(float)
for day in last14:
    for full_id, count in raw_by_day.get(day, {}).items():
        _, model_part = full_id.split("/", 1)
        cls   = classify_model(model_part)
        label = family_label(cls)
        cost_split[label] += count * (AVG_TOKENS_PER_CALL / 1_000_000) * price_for(model_part) * 1_000_000

# ── Fetch real Anthropic usage ────────────────────────────────────────────────
anthropic_real = {"today": [], "by_day": {}, "source": "estimated"}
start_7   = (date.today() - timedelta(days=6)).isoformat() + "T00:00:00Z"
today_iso = date.today().isoformat() + "T00:00:00Z"

raw_buckets = fetch_anthropic_usage(start_7)
latest_day_with_data = None
if raw_buckets:
    anthropic_real["source"] = "anthropic_api"
    for bucket in raw_buckets:
        day = bucket["starting_at"][:10]
        results = bucket.get("results", [])
        if not results:
            continue
        day_data = []
        for r in results:
            model = r.get("model") or "unknown"
            cost  = calc_cost(r)
            day_data.append({
                "model":         model,
                "input_tokens":  r.get("uncached_input_tokens", 0),
                "output_tokens": r.get("output_tokens", 0),
                "cache_read":    r.get("cache_read_input_tokens", 0),
                "cost":          round(cost, 6),
            })
        anthropic_real["by_day"][day] = day_data
        latest_day_with_data = day
    # Use most recent day with data (API has ~24h lag)
    if latest_day_with_data:
        anthropic_real["today"]       = anthropic_real["by_day"][latest_day_with_data]
        anthropic_real["latest_day"]  = latest_day_with_data
        anthropic_real["total_cost"]  = {
            day: round(sum(m["cost"] for m in models), 4)
            for day, models in anthropic_real["by_day"].items()
        }

# ── Assemble output ───────────────────────────────────────────────────────────
output = {
    "generated_at":    datetime.now(timezone.utc).isoformat(),
    "budget":          budget,
    "errors_today":    errors_today,
    "rate_events":     rate_events[-20:],
    "chart":           chart,
    "today_models":    today_models[:12],
    "cost_split":      dict(cost_split),
    "total_sessions":  72,
    "anthropic_real":  anthropic_real,
}

with open(OUT_PATH, "w") as f:
    json.dump(output, f, indent=2)

print(f"✅ usage-data.json written — {len(today_models)} models tracked today")
