"""
Amazon India restock watcher (morning window).

Runs in a loop from start until END_TIME (Israel time), reloading each product
page every INTERVAL seconds. When a product switches from "unavailable" to
"can be bought" it sends a Telegram alert with a direct link. It alerts again
only if the product sells out and comes back.

Config (environment variables override settings.json, which the dashboard edits):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  - same as the Eneba bot
  AMAZON_URLS   - one or more product links (amzn.in short links are fine),
                  separated by commas or new lines
  END_TIME      - local Israel time to stop, HH:MM (default 09:15)
  INTERVAL      - seconds between checks (default 25)
  TEST_MODE     - "true" = check once, send the status of each product, exit
  RUN_ONCE      - "true" = check each product once with normal alerts, exit

Command line:
  --background  - the all-day check (run by Task Scheduler every few minutes):
                  one check, skipped while the morning window is running

Every check is appended to data/amazon_history.jsonl for the dashboard. Restock
alerts compare against the last known status in that file, so a product that
stays in stock is not reported again on every run.
"""

import os
import re
import sys
import time
import random
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from botlib import amazon_settings, record_event, prune_file, history_path, parse_events

BACKGROUND = "--background" in sys.argv
if BACKGROUND:
    os.environ["BACKGROUND_RUN"] = "true"  # tags history events (see botlib.record_event)
    os.environ["RUN_ONCE"] = "true"

TZ = ZoneInfo("Asia/Jerusalem")
SETTINGS = amazon_settings()
START_TIME = SETTINGS["start_time"]
URLS = [u.strip() for u in re.split(r"[,\n]", os.getenv("AMAZON_URLS") or "") if u.strip()] or SETTINGS["urls"]
END_TIME = os.getenv("END_TIME") or SETTINGS["end_time"]
INTERVAL = int(os.getenv("INTERVAL") or SETTINGS["interval"])
TEST_MODE = (os.getenv("TEST_MODE") or "").lower() == "true"
RUN_ONCE = TEST_MODE or (os.getenv("RUN_ONCE") or "").lower() == "true"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

BUY_SELECTORS = [
    "#buy-now-button",
    "#add-to-cart-button",
    "#gc-buy-box-atc",
    "input[name='submit.buy-now']",
    "input[name='submit.add-to-cart']",
]
OUT_TEXTS = ["currently unavailable", "out of stock", "temporarily out of stock"]
CAPTCHA_TEXTS = ["enter the characters you see below", "type the characters you see"]
# Only the product's own price blocks. A page-wide ".a-price" also matches the
# "Customers also viewed" carousels, which is where wrong prices came from when
# the product itself has no price (out of stock).
PRICE_SELECTORS = [
    "#corePrice_feature_div .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#buybox .a-price .a-offscreen",
    "#price_inside_buybox",
    "#gc-live-preview-amount",
]


def send_telegram(msg: str):
    print(msg)
    if not (TG_TOKEN and TG_CHAT):
        return
    body = urllib.parse.urlencode(
        {"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": "true"}
    ).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body, timeout=20
        )
    except Exception as e:
        print(f"telegram error: {e}")


def past_end() -> bool:
    h, m = map(int, END_TIME.split(":"))
    now = datetime.now(TZ)
    return (now.hour, now.minute) >= (h, m)


def in_morning_window() -> bool:
    return START_TIME <= f"{datetime.now(TZ):%H:%M}" < END_TIME


def last_known_status() -> dict:
    """Last real in/out status per product link, from the history file."""
    path = history_path("amazon")
    last = {}
    if path.exists():
        for e in parse_events(path.read_text(encoding="utf-8")):
            if e.get("status") in ("in", "out") and not e.get("test"):
                last[e["link"]] = e["status"]
    return last


def check(page, url: str) -> dict:
    """Return {'status': 'in'|'out'|'captcha'|'unknown', 'title', 'price', 'url'}."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_selector("#productTitle, #availability, form[action*='validateCaptcha']", timeout=15000)
    except Exception:
        pass
    body = page.inner_text("body").lower()
    title = ""
    if page.locator("#productTitle").count():
        title = page.locator("#productTitle").first.inner_text().strip()
    price = ""
    for sel in PRICE_SELECTORS:
        if page.locator(sel).count():
            price = (page.locator(sel).first.text_content() or "").strip()
            if price:
                break
    info = {"title": title[:80] or "מוצר באמזון", "price": price, "url": page.url.split("?")[0]}

    if any(t in body for t in CAPTCHA_TEXTS) or page.locator("form[action*='validateCaptcha']").count():
        return {**info, "status": "captcha"}
    buyable = any(
        page.locator(s).count() and page.locator(s).first.is_visible() for s in BUY_SELECTORS
    )
    avail = ""
    if page.locator("#availability").count():
        avail = page.locator("#availability").first.inner_text().strip().lower()
    if buyable and not any(t in avail for t in OUT_TEXTS):
        return {**info, "status": "in"}
    if any(t in avail for t in OUT_TEXTS) or page.locator("#outOfStock").count():
        return {**info, "status": "out"}
    return {**info, "status": "unknown"}


def main():
    status_he = {"in": "✅ במלאי", "out": "❌ אזל", "captcha": "🤖 אמזון ביקשה CAPTCHA", "unknown": "❓ לא זוהה"}
    if BACKGROUND and in_morning_window():
        return  # the morning watch is already checking every few seconds
    captcha_streak = 0
    captcha_warned = False
    errors = 0
    prune_file(history_path("amazon"), days=7)
    known = last_known_status()
    last = {u: known.get(u) for u in URLS}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 1800},
        )
        page = ctx.new_page()

        while True:
            for u in URLS:
                try:
                    r = check(page, u)
                    errors = 0
                except Exception as e:
                    errors += 1
                    print(f"{datetime.now(TZ):%H:%M:%S} error on {u}: {e}")
                    alert = errors == 10
                    if alert:
                        send_telegram(f"⚠️ בוט אמזון: 10 שגיאות ברצף בטעינת הדף ({e})")
                    record_event("amazon", {"link": u, "status": "error", "error": str(e)[:200],
                                            "alert": alert, "test": TEST_MODE})
                    continue

                print(f"{datetime.now(TZ):%H:%M:%S} {r['status']:8} {r['price']:>10}  {r['title']}")
                alert = False

                if TEST_MODE:
                    send_telegram(
                        f"🧪 בדיקת בוט אמזון\n{r['title']}\nמצב: {status_he[r['status']]} {r['price']}\n{r['url']}"
                    )
                elif r["status"] == "captcha":
                    captcha_streak += 1
                    if captcha_streak >= 5 and not captcha_warned:
                        send_telegram("⚠️ בוט אמזון: אמזון חוסמת את הבדיקות (CAPTCHA). ייתכן שצריך להריץ מהמחשב בבית.")
                        captcha_warned = alert = True
                else:
                    captcha_streak = 0
                    if r["status"] == "in" and last[u] != "in":
                        send_telegram(f"🚨 חזר למלאי באמזון!\n{r['title']}\n{r['price']}\n{r['url']}")
                        alert = True
                    if r["status"] in ("in", "out"):
                        last[u] = r["status"]

                record_event("amazon", {"link": u, "url": r["url"], "product": r["title"], "status": r["status"],
                                        "price": r["price"], "alert": alert, "test": TEST_MODE})

            if RUN_ONCE or past_end():
                break
            time.sleep(INTERVAL + random.uniform(-5, 5))

        browser.close()


if __name__ == "__main__":
    main()
