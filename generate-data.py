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
OUT_PATH    = REPO_DIR / "usage-data.json"

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
}

with open(OUT_PATH, "w") as f:
    json.dump(output, f, indent=2)

print(f"✅ usage-data.json written — {len(today_models)} models tracked today")
