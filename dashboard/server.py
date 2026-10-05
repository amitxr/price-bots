"""
Local dashboard for the Amazon restock bot. Serves index.html and a small JSON API on 127.0.0.1 only.

  GET  /api/data?hours=24   latest status, alerts (7 days) and audit events (last `hours`)
  GET  /api/settings        current settings + next scheduled runs
  POST /api/settings        apply settings (settings.json / scheduled tasks)
  POST /api/run/amazon      run the Amazon check once on this PC now (normal alerts)
  POST /api/run/amazon-test run the Amazon bot once in test mode (status messages to Telegram)
"""

import os
import re
import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from botlib import (  # noqa: E402
    ROOT, WATCH_HEARTBEAT, amazon_settings, load_settings, save_settings, history_path, parse_events,
)

HOST, PORT = "127.0.0.1", int(os.getenv("DASHBOARD_PORT") or 8765)
TASK_NAME = "Amazon morning watch"
BACKGROUND_TASK = "Amazon all-day check"
INDEX = Path(__file__).resolve().parent / "index.html"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       creationflags=NO_WINDOW, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} failed")
    return r.stdout


def powershell(script, timeout=30):
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


# ---------- data ----------

def since(events, hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [e for e in events if datetime.fromisoformat(e["ts"]) >= cutoff]


def local_events(bot):
    path = history_path(bot)
    return parse_events(path.read_text(encoding="utf-8")) if path.exists() else []


def get_data(hours):
    amazon = local_events("amazon")
    latest_amazon = {}
    for e in amazon:
        if e.get("status") != "error" or e["link"] not in latest_amazon:
            latest_amazon[e["link"]] = e
    return {
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "running": {"amazon": is_running("amazon")},
        "watch_active": WATCH_HEARTBEAT.exists() and time.time() - WATCH_HEARTBEAT.stat().st_mtime < 120,
        "latest": {
            "amazon": [latest_amazon.get(u) or {"link": u} for u in amazon_settings()["urls"]],
        },
        "alerts": sorted([e for e in since(amazon, 24 * 7) if e.get("alert")],
                         key=lambda e: e["ts"], reverse=True),
        "audit": {"amazon": since(amazon, hours)},
    }


# ---------- settings ----------

def task_info(name=TASK_NAME):
    script = (
        f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop; "
        "$i = $t | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{ start = $t.Triggers[0].StartBoundary; "
        "next = if ($i.NextRunTime) { $i.NextRunTime.ToString('o') } else { $null }; "
        "state = [string]$t.State } | ConvertTo-Json -Compress"
    )
    try:
        info = json.loads(powershell(script))
        info["start"] = info["start"][11:16] if info.get("start") else None
        return info
    except Exception:
        return {"start": None, "next": None, "state": "missing"}


def get_settings():
    task = task_info()
    background = task_info(BACKGROUND_TASK)
    amazon = amazon_settings()
    if task["start"]:
        amazon["start_time"] = task["start"]
    return {
        "amazon": {**amazon, "next_run": task["next"], "task_state": task["state"],
                   "background_next_run": background["next"], "background_state": background["state"]},
    }


def set_background_task(minutes):
    """Create, update or remove the all-day check task (0 minutes = remove)."""
    if minutes == 0:
        powershell(f"Unregister-ScheduledTask -TaskName '{BACKGROUND_TASK}' -Confirm:$false -ErrorAction SilentlyContinue")
        return
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"  # no console window every few minutes
    powershell(
        f"$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument 'amazon_bot.py --background' -WorkingDirectory '{ROOT}'; "
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes {minutes}); "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew; "
        f"Register-ScheduledTask -TaskName '{BACKGROUND_TASK}' -Action $a -Trigger $t -Settings $s -Force "
        "-Description 'Checks Amazon India gift cards all day, outside the morning window' | Out-Null"
    )


def apply_settings(body):
    errors = {}
    amazon = body.get("amazon", {})

    try:
        interval = int(amazon.get("interval"))
        if not 5 <= interval <= 600:
            raise ValueError
    except (TypeError, ValueError):
        errors["amazon.interval"] = "Enter whole seconds between 5 and 600."
    try:
        background_interval = int(amazon.get("background_interval"))
        if not 0 <= background_interval <= 120:
            raise ValueError
    except (TypeError, ValueError):
        errors["amazon.background_interval"] = "Enter whole minutes between 1 and 120, or 0 to turn it off."
    start, end = amazon.get("start_time", ""), amazon.get("end_time", "")
    if not TIME_RE.match(start):
        errors["amazon.start_time"] = "Use HH:MM."
    if not TIME_RE.match(end):
        errors["amazon.end_time"] = "Use HH:MM."
    elif TIME_RE.match(start) and end <= start:
        errors["amazon.end_time"] = "End time must be after the start time."
    urls = [u.strip() for u in amazon.get("urls", []) if u.strip()]
    if not urls or len(urls) > 10 or not all(re.match(r"^https://(www\.)?(amazon\.in|amzn\.in)/", u) for u in urls):
        errors["amazon.urls"] = "Add 1–10 links starting with https://amzn.in/ or https://www.amazon.in/."
    if errors:
        return {"ok": False, "errors": errors}

    current = get_settings()
    settings = load_settings()
    settings["amazon"] = {"urls": urls, "start_time": start, "end_time": end, "interval": interval,
                          "background_interval": background_interval}
    save_settings(settings)
    task_exists = current["amazon"]["background_state"] != "missing"
    if background_interval != current["amazon"]["background_interval"] or task_exists != (background_interval > 0):
        set_background_task(background_interval)
    if start != current["amazon"]["start_time"]:
        powershell(f"Set-ScheduledTask -TaskName '{TASK_NAME}' "
                   f"-Trigger (New-ScheduledTaskTrigger -Daily -At '{start}') | Out-Null")
    return {"ok": True, "settings": get_settings()}


# ---------- actions ----------

_procs = {}  # job name -> Popen, for jobs started from the dashboard


def is_running(job):
    p = _procs.get(job)
    return p is not None and p.poll() is None


def start_bot(job, script, env):
    """Run a bot script once on this PC in the background (one instance per job)."""
    if is_running(job):
        return False
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **env}
    with open(ROOT / f"{script.removesuffix('.py')}.log", "a", encoding="utf-8") as log:
        _procs[job] = subprocess.Popen([str(python if python.exists() else sys.executable), script], cwd=ROOT,
                                       env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    return True


def run_amazon():
    started = start_bot("amazon", "amazon_bot.py", {"MANUAL_RUN": "true", "RUN_ONCE": "true"})
    return {"ok": True, "started": started, "message": "Checking Amazon…" if started else "Amazon check is already running."}


def run_amazon_test():
    started = start_bot("amazon", "amazon_bot.py", {"TEST_MODE": "true"})
    return {"ok": True, "started": started,
            "message": "Sending Amazon test messages to Telegram…" if started else "An Amazon check is already running."}


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_api(self, fn):
        try:
            self.send(200, fn())
        except Exception as e:
            self.send(500, {"ok": False, "message": str(e)[:300]})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, INDEX.read_bytes(), "text/html")
        elif url.path == "/api/data":
            hours = int(parse_qs(url.query).get("hours", ["24"])[0])
            self.handle_api(lambda: get_data(hours))
        elif url.path == "/api/settings":
            self.handle_api(get_settings)
        else:
            self.send(404, {"ok": False, "message": "Not found"})

    def do_POST(self):
        # Only accept requests from the dashboard page itself.
        if self.headers.get("Origin") not in (None, f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"ok": False, "message": "Forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        routes = {
            "/api/settings": lambda: apply_settings(body),
            "/api/run/amazon": run_amazon,
            "/api/run/amazon-test": run_amazon_test,
        }
        fn = routes.get(urlparse(self.path).path)
        if fn:
            self.handle_api(fn)
        else:
            self.send(404, {"ok": False, "message": "Not found"})


if __name__ == "__main__":
    print(f"Amazon bot dashboard: http://{HOST}:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
