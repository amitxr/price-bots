"""
Local dashboard for the price bots. Serves index.html and a small JSON API on 127.0.0.1 only.

  GET  /api/data?hours=24   latest status, alerts (7 days) and audit events (last `hours`)
  GET  /api/settings        current settings + next scheduled runs
  POST /api/settings        apply settings (GitHub variables / settings.json / scheduled task)
  POST /api/run/eneba       run the Eneba check once on this PC now
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
    ROOT, ENEBA_DEFAULT_URL, amazon_settings, load_settings, save_settings, history_path, parse_events,
)

HOST, PORT = "127.0.0.1", int(os.getenv("DASHBOARD_PORT") or 8765)
REPO = os.getenv("GITHUB_REPO") or "amitxr/price-bots"
TASK_NAME = "Amazon morning watch"
BACKGROUND_TASK = "Amazon all-day check"
ENEBA_CRON_HOURS_UTC = (0, 12)  # must match .github/workflows/eneba-check.yml
INDEX = Path(__file__).resolve().parent / "index.html"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_cache = {}


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       creationflags=NO_WINDOW, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} failed")
    return r.stdout


def powershell(script, timeout=30):
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


# ---------- data ----------

def eneba_events():
    def fetch():
        text = run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                    f"repos/{REPO}/contents/eneba_history.jsonl?ref=data"])
        return parse_events(text)
    try:
        return cached("eneba_events", 60, fetch), None
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            return [], None  # no history pushed yet
        return [], f"Could not load Eneba history from GitHub: {msg[:160]}"


def since(events, hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [e for e in events if datetime.fromisoformat(e["ts"]) >= cutoff]


def local_events(bot):
    path = history_path(bot)
    return parse_events(path.read_text(encoding="utf-8")) if path.exists() else []


def get_data(hours):
    github_eneba, eneba_error = eneba_events()
    # Runs started with "Run now" happen on this PC and are only in the local file.
    eneba = sorted(github_eneba + local_events("eneba"), key=lambda e: e["ts"])
    amazon = local_events("amazon")
    latest_amazon = {}
    for e in amazon:
        if e.get("status") != "error" or e["link"] not in latest_amazon:
            latest_amazon[e["link"]] = e
    return {
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "errors": {"eneba": eneba_error},
        "running": {"eneba": is_running("eneba"), "amazon": is_running("amazon")},
        "latest": {
            "eneba": eneba[-1] if eneba else None,
            "amazon": [latest_amazon.get(u) or {"link": u} for u in amazon_settings()["urls"]],
        },
        "alerts": sorted([e for e in since(eneba + amazon, 24 * 7) if e.get("alert")],
                         key=lambda e: e["ts"], reverse=True),
        "audit": {"eneba": since(eneba, hours), "amazon": since(amazon, hours)},
    }


# ---------- settings ----------

def gh_variable(name):
    try:
        return run(["gh", "variable", "get", name, "--repo", REPO]).strip()
    except RuntimeError:
        return ""


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


def next_eneba_run():
    now = datetime.now(timezone.utc)
    for day in range(2):
        for h in ENEBA_CRON_HOURS_UTC:
            t = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now:
                return t.isoformat()


def get_settings():
    eneba = cached("eneba_vars", 60, lambda: {
        "threshold": float(gh_variable("THRESHOLD") or 24),
        "product_url": gh_variable("PRODUCT_URL") or ENEBA_DEFAULT_URL,
    })
    task = task_info()
    background = task_info(BACKGROUND_TASK)
    amazon = amazon_settings()
    if task["start"]:
        amazon["start_time"] = task["start"]
    return {
        "eneba": {**eneba, "next_run": next_eneba_run(), "schedule": "Every 12 hours (GitHub)"},
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
    eneba, amazon = body.get("eneba", {}), body.get("amazon", {})

    threshold = eneba.get("threshold")
    try:
        threshold = float(threshold)
        if not 1 <= threshold <= 200:
            raise ValueError
    except (TypeError, ValueError):
        errors["eneba.threshold"] = "Enter a number between 1 and 200."
    product_url = (eneba.get("product_url") or "").strip()
    if not product_url.startswith("https://www.eneba.com/"):
        errors["eneba.product_url"] = "Must be an https://www.eneba.com/ link."

    try:
        interval = int(amazon.get("interval"))
        if not 10 <= interval <= 600:
            raise ValueError
    except (TypeError, ValueError):
        errors["amazon.interval"] = "Enter whole seconds between 10 and 600."
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
    if threshold != current["eneba"]["threshold"]:
        run(["gh", "variable", "set", "THRESHOLD", "--repo", REPO, "--body", f"{threshold:g}"])
    if product_url != current["eneba"]["product_url"]:
        run(["gh", "variable", "set", "PRODUCT_URL", "--repo", REPO, "--body", product_url])
    # GitHub may return the old value for a few seconds after a set, so cache what we wrote.
    _cache["eneba_vars"] = (time.time(), {"threshold": threshold, "product_url": product_url})

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


def run_eneba():
    s = get_settings()["eneba"]
    started = start_bot("eneba", "eneba_bot.py", {
        "MANUAL_RUN": "true", "THRESHOLD": f"{s['threshold']:g}", "PRODUCT_URL": s["product_url"], "ALWAYS_NOTIFY": "false",
    })
    return {"ok": True, "started": started, "message": "Checking Eneba…" if started else "Eneba check is already running."}


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
            "/api/run/eneba": run_eneba,
            "/api/run/amazon": run_amazon,
            "/api/run/amazon-test": run_amazon_test,
        }
        fn = routes.get(urlparse(self.path).path)
        if fn:
            self.handle_api(fn)
        else:
            self.send(404, {"ok": False, "message": "Not found"})


if __name__ == "__main__":
    print(f"Price bots dashboard: http://{HOST}:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
