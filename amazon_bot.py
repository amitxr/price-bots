"""
Amazon India restock watcher (morning window).

Runs in a loop from start until END_TIME (Israel time), reloading each product
page every INTERVAL seconds. When a product switches from "unavailable" to
"can be bought" it sends a Telegram alert with a direct link. It alerts again
only if the product sells out and comes back.

Config (environment variables):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  - same as the Eneba bot
  AMAZON_URLS   - one or more product links (amzn.in short links are fine),
                  separated by commas or new lines
  END_TIME      - local Israel time to stop, HH:MM (default 09:15)
  INTERVAL      - seconds between checks (default 25)
  TEST_MODE     - "true" = check once, send the status of each product, exit
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

TZ = ZoneInfo("Asia/Jerusalem")
DEFAULT_URLS = "https://amzn.in/d/05i6gRjT,https://amzn.in/d/06DEU3Zq"
URLS = [u.strip() for u in re.split(r"[,\n]", os.getenv("AMAZON_URLS") or DEFAULT_URLS) if u.strip()]
END_TIME = os.getenv("END_TIME") or "09:15"
INTERVAL = int(os.getenv("INTERVAL") or 25)
TEST_MODE = (os.getenv("TEST_MODE") or "").lower() == "true"
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
    for sel in ["#corePrice_feature_div .a-offscreen", ".a-price .a-offscreen", "#gc-live-preview-amount"]:
        if page.locator(sel).count():
            price = page.locator(sel).first.inner_text().strip()
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
    last = {u: None for u in URLS}
    captcha_streak = 0
    captcha_warned = False
    errors = 0

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
                    if errors == 10:
                        send_telegram(f"⚠️ בוט אמזון: 10 שגיאות ברצף בטעינת הדף ({e})")
                    continue

                print(f"{datetime.now(TZ):%H:%M:%S} {r['status']:8} {r['price']:>10}  {r['title']}")

                if TEST_MODE:
                    send_telegram(
                        f"🧪 בדיקת בוט אמזון\n{r['title']}\nמצב: {status_he[r['status']]} {r['price']}\n{r['url']}"
                    )
                    continue

                if r["status"] == "captcha":
                    captcha_streak += 1
                    if captcha_streak >= 5 and not captcha_warned:
                        send_telegram("⚠️ בוט אמזון: אמזון חוסמת את הבדיקות (CAPTCHA). ייתכן שצריך להריץ מהמחשב בבית.")
                        captcha_warned = True
                    continue
                captcha_streak = 0

                if r["status"] == "in" and last[u] != "in":
                    send_telegram(f"🚨 חזר למלאי באמזון!\n{r['title']}\n{r['price']}\n{r['url']}")
                if r["status"] in ("in", "out"):
                    last[u] = r["status"]

            if TEST_MODE or past_end():
                break
            time.sleep(INTERVAL + random.uniform(-5, 5))

        browser.close()


if __name__ == "__main__":
    main()
