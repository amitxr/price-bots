"""
Eneba PSN India price watcher.

Opens the Eneba product page in a real (headless) browser, reads the value tiles
(e.g. "4000 INR / ₪171.33"), finds the one marked "Best value", computes how many
INR you get per ₪1, and sends a Telegram message if it reaches THRESHOLD.

Config (environment variables):
  TELEGRAM_BOT_TOKEN  - required, from @BotFather
  TELEGRAM_CHAT_ID    - required, your chat id
  THRESHOLD           - INR per ₪1 that triggers an alert (default 24)
  PRODUCT_URL         - page to watch (default: PSN India Rs.3000 page)
  ALWAYS_NOTIFY       - "true" to get a status message every run, even below threshold
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse

from playwright.sync_api import sync_playwright

from botlib import ENEBA_DEFAULT_URL, env, record_event, prune_file, history_path

PRODUCT_URL = os.getenv("PRODUCT_URL") or ENEBA_DEFAULT_URL
THRESHOLD = float(os.getenv("THRESHOLD", "24") or 24)
ALWAYS_NOTIFY = os.getenv("ALWAYS_NOTIFY", "false").lower() == "true"
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

SYMBOLS = {"₪": "ILS", "$": "USD", "€": "EUR", "£": "GBP"}
INR_LINE = re.compile(r"^([\d,]+)\s*INR$")
PRICE_LINE = re.compile(
    r"^(?P<pre>[₪$€£]|ILS|USD|EUR|GBP)?\s*(?P<num>[\d,]+\.\d{1,2})\s*(?P<post>[₪$€£]|ILS|USD|EUR|GBP)?$"
)


# ---------- scraping ----------

def fetch_page_text(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 2000},
        )
        page = ctx.new_page()
        # Ask Eneba for shekel prices; if the site ignores it we convert later.
        sep = "&" if "?" in url else "?"
        page.goto(f"{url}{sep}currency=ILS", wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("text=/INR per/", timeout=45000)
        except Exception:
            pass  # parse whatever loaded; errors are reported below
        text = page.inner_text("body")
        browser.close()
        return text


def parse_tiles(text: str):
    """Return list of dicts: {inr, price, currency, best}."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    tiles = []
    for i, line in enumerate(lines):
        m = INR_LINE.match(line)
        if not m:
            continue
        inr = float(m.group(1).replace(",", ""))
        best = i > 0 and lines[i - 1].lower() == "best value"
        # price is within the next few lines
        for nxt in lines[i + 1 : i + 4]:
            pm = PRICE_LINE.match(nxt)
            if pm:
                sym = pm.group("pre") or pm.group("post") or "₪"
                cur = SYMBOLS.get(sym, sym)
                price = float(pm.group("num").replace(",", ""))
                tiles.append({"inr": inr, "price": price, "currency": cur, "best": best})
                break
    # de-duplicate (mobile/desktop layouts may render the same tile twice)
    seen, unique = set(), []
    for t in tiles:
        key = (t["inr"], t["price"], t["currency"])
        if key not in seen:
            seen.add(key)
            unique.append(t)
        elif t["best"]:
            for u in unique:
                if (u["inr"], u["price"], u["currency"]) == key:
                    u["best"] = True
    return unique


def to_ils_rate(currency: str) -> float:
    """How many ₪ one unit of `currency` is worth."""
    if currency == "ILS":
        return 1.0
    with urllib.request.urlopen(f"https://open.er-api.com/v6/latest/{currency}", timeout=20) as r:
        data = json.load(r)
    return float(data["rates"]["ILS"])


# ---------- telegram ----------

def send_telegram(msg: str):
    if not (TG_TOKEN and TG_CHAT):
        print("Telegram not configured; message would be:\n" + msg)
        return
    body = urllib.parse.urlencode(
        {"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": "true"}
    ).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body, timeout=20
    )


# ---------- main ----------

def main():
    event = run_check()
    prune_file(history_path("eneba"), days=30)
    record_event("eneba", {"threshold": THRESHOLD, "url": PRODUCT_URL, **event})
    if event.get("error"):
        sys.exit(1)


def run_check() -> dict:
    """Check the page, send Telegram messages, and return the result as a history event."""
    try:
        text = fetch_page_text(PRODUCT_URL)
    except Exception as e:
        send_telegram(f"⚠️ בוט Eneba: לא הצלחתי לטעון את הדף ({e})")
        return {"error": f"Page failed to load: {str(e)[:200]}", "alert": True}

    tiles = parse_tiles(text)
    if not tiles:
        send_telegram("⚠️ בוט Eneba: הדף נטען אבל לא מצאתי מחירים. ייתכן שהאתר חסם או שינה עיצוב.")
        print(text[:3000])
        return {"error": "Page loaded but no prices were found", "alert": True}

    rate = to_ils_rate(tiles[0]["currency"])
    for t in tiles:
        t["ils"] = round(t["price"] * rate, 2)
        t["ratio"] = round(t["inr"] / t["ils"], 2)

    best = next((t for t in tiles if t["best"]), None)
    label = "Best value"
    if best is None:  # fallback: compute it ourselves
        best = max(tiles, key=lambda t: t["ratio"])
        label = "הכי משתלם (חושב ע״י הבוט)"

    converted = "" if tiles[0]["currency"] == "ILS" else f" (הומר מ-{tiles[0]['currency']})"
    summary = (
        f"{label}: {int(best['inr'])} INR ב-₪{best['ils']:.2f}{converted}\n"
        f"שער: {best['ratio']:.2f} INR לכל ₪1 (סף: {THRESHOLD:g})"
    )
    print(summary)
    for t in sorted(tiles, key=lambda t: t["inr"]):
        print(f"  {int(t['inr'])} INR  ₪{t['ils']:.2f}  {t['ratio']:.2f}{'  <- best' if t is best else ''}")

    alert = best["ratio"] >= THRESHOLD
    if alert:
        send_telegram(f"🎯 מחיר טוב ב-Eneba!\n{summary}\n{PRODUCT_URL}")
    elif ALWAYS_NOTIFY:
        send_telegram(f"ℹ️ בדיקת Eneba – עדיין מתחת לסף\n{summary}")

    return {
        "inr": best["inr"],
        "ils": best["ils"],
        "ratio": best["ratio"],
        "source": "site" if label == "Best value" else "computed",
        "alert": alert,
    }


if __name__ == "__main__":
    main()
