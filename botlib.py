"""
Shared helpers for the bots and the dashboard: local settings file and JSONL history.

settings.json (local only, edited from the dashboard):
  {"amazon": {"urls": [...], "start_time": "06:30", "end_time": "09:15", "interval": 25, "background_interval": 5}}

History files live in data/<bot>_history.jsonl, one JSON event per line, "ts" in UTC ISO format.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SETTINGS_FILE = ROOT / "settings.json"

ENEBA_DEFAULT_URL = "https://www.eneba.com/psn-playstation-network-card-rs-3000-in-psn-key-india"
AMAZON_DEFAULTS = {
    "urls": ["https://amzn.in/d/05i6gRjT", "https://amzn.in/d/06DEU3Zq"],
    "start_time": "06:30",
    "end_time": "09:15",
    "interval": 25,
    "background_interval": 5,  # minutes between all-day checks outside the window (0 = off)
}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def amazon_settings() -> dict:
    """Amazon settings: settings.json over defaults."""
    return {**AMAZON_DEFAULTS, **load_settings().get("amazon", {})}


def history_path(bot: str) -> Path:
    return DATA_DIR / f"{bot}_history.jsonl"


def record_event(bot: str, event: dict):
    DATA_DIR.mkdir(exist_ok=True)
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "bot": bot, **event}
    if os.getenv("MANUAL_RUN") == "true":  # started from the dashboard's "Run now"
        event["manual"] = True
    if os.getenv("BACKGROUND_RUN") == "true":  # Amazon all-day check
        event["background"] = True
    with open(history_path(bot), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def parse_events(text: str) -> list:
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def prune_file(path, days: int):
    """Drop events older than `days` days (keeps the file if nothing to drop)."""
    path = Path(path)
    if not path.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events = parse_events(path.read_text(encoding="utf-8"))
    kept = [e for e in events if datetime.fromisoformat(e["ts"]) >= cutoff]
    if len(kept) != len(events):
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept), encoding="utf-8")
