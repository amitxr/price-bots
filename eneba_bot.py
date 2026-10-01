"""
Eneba PSN India price watcher.

Opens the Eneba product page in a real (headless) browser and reads every available
value tile (e.g. "4000 INR / ₪170.65 / 23.44 INR per ₪1"). For each card it computes
the real rate after Eneba's checkout fee (INR / (price + fee)) and sends a Telegram
message if the best card reaches THRESHOLD.

Config (environment variables):
  TELEGRAM_BOT_TOKEN  - required, from @BotFather
  TELEGRAM_CHAT_ID    - required, your chat id
  THRESHOLD           - INR per ₪1 after the fee that triggers an alert (default 24)
  ENEBA_FEE           - checkout fee in ₪ added to each purchase (default 22)
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

from botlib import ENEBA_DEFAULT_URL, ENEBA_DEFAULT_FEE, env, record_event, prune_file, history_path

PRODUCT_URL = os.getenv("PRODUCT_URL") or ENEBA_DEFAULT_URL
THRESHOLD = float(os.getenv("THRESHOLD", "24") or 24)
FEE = float(os.getenv("ENEBA_FEE") or ENEBA_DEFAULT_FEE)
ALWAYS_NOTIFY = os.getenv("ALWAYS_NOTIFY", "false").lower() == "true"
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

SYMBOLS = {"₪": "ILS", "$": "USD", "€": "EUR", "£": "GBP"}
INR_LINE = re.compile(r"^([\d,]+)\s*INR$")
# Every available tile ends with the site's own rate line, e.g. "23.44 INR per ₪1".
RATE_LINE = re.compile(r"^[\d.,]+\s*INR per\s*\S+$")
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
    """Return the available tiles: list of {inr, price, currency, best}.

    A tile is the three lines "<n> INR", "<price>", "<rate> INR per ₪1", optionally preceded
    by "Best value". Anchoring on the rate line skips sold-out tiles and the "Value: 3000 INR"
    label above the tiles (which used to get paired with the next tile's price).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    tiles, seen = [], set()
    for i, line in enumerate(lines):
        if i < 2 or not RATE_LINE.match(line):
            continue
        m, pm = INR_LINE.match(lines[i - 2]), PRICE_LINE.match(lines[i - 1])
        if not (m and pm):
            continue
        sym = pm.group("pre") or pm.group("post") or "₪"
        tile = {
            "inr": float(m.group(1).replace(",", "")),
            "price": float(pm.group("num").replace(",", "")),
            "currency": SYMBOLS.get(sym, sym),
            "best": i >= 3 and lines[i - 3].lower() == "best value",
        }
        key = (tile["inr"], tile["price"])
        if key in seen:  # mobile/desktop layouts may render the same tile twice
            continue
        seen.add(key)
        tiles.append(tile)
    return tiles


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
        t["site_ratio"] = round(t["inr"] / t["ils"], 2)
        t["ratio"] = round(t["inr"] / (t["ils"] + FEE), 2)  # what you really get after the fee

    best = max(tiles, key=lambda t: t["ratio"])
    site_best = next((t for t in tiles if t["best"]), None)

    converted = "" if tiles[0]["currency"] == "ILS" else f" (הומר מ-{tiles[0]['currency']})"
    summary = (
        f"הכי משתלם: {int(best['inr'])} INR ב-₪{best['ils']:.2f} + עמלה ₪{FEE:g}{converted}\n"
        f"שער אחרי עמלה: {best['ratio']:.2f} INR לכל ₪1 (סף: {THRESHOLD:g})"
    )
    print(summary)
    for t in sorted(tiles, key=lambda t: t["inr"]):
        marks = ("  <- best" if t is best else "") + ("  [site: Best value]" if t["best"] else "")
        print(f"  {int(t['inr'])} INR  ₪{t['ils']:.2f}  site {t['site_ratio']:.2f}  after fee {t['ratio']:.2f}{marks}")

    alert = best["ratio"] >= THRESHOLD
    if alert:
        send_telegram(f"🎯 מחיר טוב ב-Eneba!\n{summary}\n{PRODUCT_URL}")
    elif ALWAYS_NOTIFY:
        send_telegram(f"ℹ️ בדיקת Eneba – עדיין מתחת לסף\n{summary}")

    return {
        "inr": best["inr"],
        "ils": best["ils"],
        "fee": FEE,
        "ratio": best["ratio"],
        "site_ratio": best["site_ratio"],
        "site_best_inr": site_best["inr"] if site_best else None,
        "tiles": [{k: t[k] for k in ("inr", "ils", "site_ratio", "ratio")} for t in sorted(tiles, key=lambda t: t["inr"])],
        "alert": alert,
    }


if __name__ == "__main__":
    main()
