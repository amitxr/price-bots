"""
Amazon India restock watcher (morning window).

Runs in a loop from start until END_TIME (Israel time), checking all products at
once every INTERVAL seconds. When a product can be bought it sends a Telegram
alert with a direct link. It alerts again only if the product sells out and
comes back.

"Can be bought" means any seller offer with an Add to Cart button, read from the
product's "See All Buying Options" panel (a ~25 KB request). The product page
alone is not enough: on 5 Oct the ₹2000 and ₹4000 cards were for sale there for
minutes while the page had no Buy button and Amazon search called them out of
stock. The full product page (~1.5 MB) is still loaded about every
PAGE_EVERY_SECONDS as a backup, and whenever the panel gives no clear answer.

Config (environment variables override settings.json, which the dashboard edits):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  AMAZON_URLS   - one or more product links (amzn.in short links are fine),
                  separated by commas or new lines
  END_TIME      - local Israel time to stop, HH:MM (default 09:15)
  INTERVAL      - seconds between rounds (default 5; a CAPTCHA round waits 3x)
  TEST_MODE     - "true" = check once, send the status of each product, exit
  RUN_ONCE      - "true" = check each product once with normal alerts, exit

Command line:
  --background  - the all-day check (run by Task Scheduler every few minutes):
                  one check, skipped while the morning watch is running
                  (it takes over if the watch stops early)

Every check is appended to data/amazon_history.jsonl for the dashboard. Alerts
compare against the last known status in that file: one message when a product
comes back in stock, and one when it sells out again (with how long it lasted).
A product that stays in stock is not reported again on every run.
"""

import os
import re
import json
import sys
import time
import random
import asyncio
import urllib.parse
import urllib.request
from html import unescape
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from botlib import ROOT, env, amazon_settings, record_event, prune_file, history_path, parse_events, WATCH_HEARTBEAT

if sys.stdout is None:  # started with pythonw by Task Scheduler: no console, so log to a file
    sys.stdout = sys.stderr = open(ROOT / "amazon_bot.log", "a", encoding="utf-8", buffering=1)

BACKGROUND = "--background" in sys.argv
if BACKGROUND:
    os.environ["BACKGROUND_RUN"] = "true"  # tags history events (see botlib.record_event)
    os.environ["RUN_ONCE"] = "true"

TZ = ZoneInfo("Asia/Jerusalem")
SETTINGS = amazon_settings()
URLS = [u.strip() for u in re.split(r"[,\n]", os.getenv("AMAZON_URLS") or "") if u.strip()] or SETTINGS["urls"]
END_TIME = os.getenv("END_TIME") or SETTINGS["end_time"]
INTERVAL = int(os.getenv("INTERVAL") or SETTINGS["interval"])
TEST_MODE = (os.getenv("TEST_MODE") or "").lower() == "true"
RUN_ONCE = TEST_MODE or (os.getenv("RUN_ONCE") or "").lower() == "true"
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

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
# The "See All Buying Options" panel. Every offer that can be bought has an Add to Cart button
# labelled with its seller and price.
OFFERS_URL = "https://www.amazon.in/gp/product/ajax/aodAjaxMain/?asin={asin}&pc=dp&experienceId=aodAjaxMain"
OFFER_RE = re.compile(r'aria-label="Add to Cart from seller (.+?) and price ([^"]+)"')
OFFER_ID_RE = re.compile(r'name="items\[0\.base\]\[offerListingId\]" value="([^"]+)"')
OFFERS_TITLE_RE = re.compile(r'id="aod-asin-title-text"[^>]*>\s*([^<]+)')
PAGE_EVERY_SECONDS = 30  # full product page as a backup this often


def send_telegram(msg: str, buttons=None):
    """Send a message; `buttons` is a list of (label, url) shown as link buttons under it."""
    print(msg)
    if not (TG_TOKEN and TG_CHAT):
        return
    fields = {"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": "true"}
    if buttons:
        fields["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": label, "url": url} for label, url in buttons if url]]}
        )
    body = urllib.parse.urlencode(fields).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body, timeout=20
        )
    except Exception as e:
        print(f"telegram error: {e}")


def asin_of(url: str):
    m = re.search(r"/dp/([A-Z0-9]{10})", url or "")
    return m.group(1) if m else None


def cart_link(url: str):
    """Link that adds the product to the cart (needs a logged-in browser; the Amazon app may ignore it)."""
    asin = asin_of(url)
    return f"https://www.amazon.in/associates/addtocart?ASIN.1={asin}&Quantity.1=1" if asin else None


def product_buttons(r: dict):
    # The product page is the reliable one: on a phone it opens the logged-in Amazon app, one tap from Buy Now.
    # An offer found in the buying-options panel opens that panel, and the cart button adds that exact offer.
    if r.get("via") == "offers":
        cart = (f"https://www.amazon.in/gp/aws/cart/add.html?OfferListingId.1={r['offer_id']}&Quantity.1=1"
                if r.get("offer_id") else cart_link(r["url"]))
        return [("🛍️ פתח לקנייה", f"{r['url']}?aod=1"), ("🛒 הוסף לעגלה", cart)]
    return [("🛍️ פתח לקנייה", r["url"]), ("🛒 הוסף לעגלה", cart_link(r["url"]))]


def past_end() -> bool:
    h, m = map(int, END_TIME.split(":"))
    now = datetime.now(TZ)
    return (now.hour, now.minute) >= (h, m)


def watch_alive() -> bool:
    """True while the morning watch loop is running (it touches the heartbeat every round)."""
    try:
        return time.time() - WATCH_HEARTBEAT.stat().st_mtime < max(120, INTERVAL * 3)
    except FileNotFoundError:
        return False


def captcha_warned_recently(hours: int = 6) -> bool:
    """True if a CAPTCHA warning was sent in the last `hours` (each all-day run is a new process)."""
    path = history_path("amazon")
    if not path.exists():
        return False
    cutoff = time.time() - hours * 3600
    return any(
        e.get("status") == "captcha" and e.get("alert") and datetime.fromisoformat(e["ts"]).timestamp() >= cutoff
        for e in parse_events(path.read_text(encoding="utf-8"))
    )


def last_known_status() -> dict:
    """Last real in/out status per product link, and since when (UTC ISO), from the history file."""
    path = history_path("amazon")
    last = {}
    if path.exists():
        for e in parse_events(path.read_text(encoding="utf-8")):
            if e.get("status") in ("in", "out") and not e.get("test"):
                if last.get(e["link"], (None,))[0] != e["status"]:
                    last[e["link"]] = (e["status"], e["ts"])
    return last


def minutes_since(ts) -> int:
    return round((datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(ts)).total_seconds() / 60) if ts else 0


async def check(page, url: str) -> dict:
    """Return {'status': 'in'|'out'|'captcha'|'unknown', 'title', 'price', 'url'}; 'unknown' adds the page html."""
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_selector("#productTitle, #availability, form[action*='validateCaptcha']", timeout=15000)
    except Exception:
        pass
    # Amazon's soft bot check: a "Continue shopping" button with no characters to type.
    # Pressing it sets a cookie but lands on the home page, so the product is opened again.
    if await page.locator("form[action*='validateCaptcha']").count() and not await page.locator("#captchacharacters").count():
        try:
            await page.locator("form[action*='validateCaptcha'] button, form[action*='validateCaptcha'] input[type=submit]").first.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector("#productTitle, #availability", timeout=15000)
        except Exception:
            pass
    body = (await page.inner_text("body")).lower()
    title = ""
    if await page.locator("#productTitle").count():
        title = (await page.locator("#productTitle").first.inner_text()).strip()
    price = ""
    for sel in PRICE_SELECTORS:
        if await page.locator(sel).count():
            price = ((await page.locator(sel).first.text_content()) or "").strip()
            if price:
                break
    info = {"title": title[:80] or "מוצר באמזון", "price": price, "url": page.url.split("?")[0]}

    if any(t in body for t in CAPTCHA_TEXTS) or await page.locator("form[action*='validateCaptcha']").count():
        return {**info, "status": "captcha"}
    buyable = False
    for s in BUY_SELECTORS:
        if await page.locator(s).count() and await page.locator(s).first.is_visible():
            buyable = True
            break
    avail = ""
    if await page.locator("#availability").count():
        avail = (await page.locator("#availability").first.inner_text()).strip().lower()
    if buyable and not any(t in avail for t in OUT_TEXTS):
        return {**info, "status": "in"}
    if any(t in avail for t in OUT_TEXTS) or await page.locator("#outOfStock").count():
        return {**info, "status": "out"}
    return {**info, "status": "unknown"}


async def check_offers(ctx, asin: str) -> dict:
    """Read the "See All Buying Options" panel. Same result shape as check(), plus 'seller' and 'offer_id'."""
    resp = await ctx.request.get(OFFERS_URL.format(asin=asin), timeout=20000,
                                 headers={"Referer": f"https://www.amazon.in/dp/{asin}"})
    html = await resp.text()
    m = OFFERS_TITLE_RE.search(html)
    info = {"title": unescape(m.group(1)).strip()[:80] if m else "מוצר באמזון", "price": "", "seller": "",
            "url": f"https://www.amazon.in/dp/{asin}", "via": "offers"}
    if "validateCaptcha" in html or any(t in html.lower() for t in CAPTCHA_TEXTS):
        return {**info, "status": "captcha"}
    if resp.status != 200 or 'id="aod-container"' not in html:
        return {**info, "status": "unknown"}
    offers = OFFER_RE.findall(html)
    if offers or 'name="submit.addToCart"' in html:
        seller, price = offers[0] if offers else ("", "")
        offer_id = OFFER_ID_RE.search(html)
        return {**info, "status": "in", "seller": unescape(seller), "price": unescape(price),
                "offer_id": offer_id.group(1) if offer_id else None}
    return {**info, "status": "out"}


def combine(offers, page):
    """One answer from the buying-options panel and the product page (either may be None)."""
    if not page:
        return offers
    if not offers:
        return page
    if offers["status"] == "in":
        return offers
    if page["status"] == "in":
        return page
    return offers if offers["status"] == "out" else page


async def notify(msg: str, buttons=None):
    """send_telegram without holding up the other tabs while Telegram answers."""
    await asyncio.to_thread(send_telegram, msg, buttons)


async def main():
    status_he = {"in": "✅ במלאי", "out": "❌ אזל", "captcha": "🤖 אמזון ביקשה CAPTCHA", "unknown": "❓ לא זוהה"}
    if BACKGROUND and watch_alive():
        return  # the morning watch is already checking every few seconds
    watching = not RUN_ONCE  # the long-running morning loop
    state = {"captcha_streak": 0, "captcha_warned": captcha_warned_recently(), "errors": 0, "round": 0}
    page_every = max(1, round(PAGE_EVERY_SECONDS / INTERVAL))  # rounds between full product page loads
    prune_file(history_path("amazon"), days=7)
    known = last_known_status()
    last = {u: known.get(u, (None,))[0] for u in URLS}
    since = {u: known.get(u, (None, None))[1] for u in URLS}  # when the current status started

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 1800},
        )
        # Pictures and fonts are most of each page load and not needed to read the buy box.
        # Stylesheets stay: the Buy button visibility check depends on them.
        await ctx.route("**/*", lambda route: route.abort()
                        if route.request.resource_type in ("image", "media", "font") else route.continue_())
        pages = {u: await ctx.new_page() for u in URLS}  # one tab per product, all checked at once
        target = dict(zip(URLS, URLS))  # becomes the full /dp/ link once known, skipping the amzn.in redirect
        for u in URLS:  # the buying-options panel needs the ASIN, which amzn.in links only give by redirecting
            if not asin_of(u):
                try:
                    resp = await ctx.request.get(u, max_redirects=0, timeout=20000)
                    if asin_of(resp.headers.get("location")):
                        target[u] = f"https://www.amazon.in/dp/{asin_of(resp.headers['location'])}"
                except Exception as e:
                    print(f"could not resolve {u}: {e}")

        async def check_one(u: str):
            try:
                offers = page = None
                asin = asin_of(target[u])
                if asin:
                    try:
                        offers = await check_offers(ctx, asin)
                    except Exception as e:
                        print(f"{datetime.now(TZ):%H:%M:%S} offers panel error on {u}: {e}")
                sure = offers and offers["status"] in ("in", "out")
                # All-day runs are a new process every minute: the full page only every 10 minutes there.
                backup_due = datetime.now().minute % 10 == 0 if BACKGROUND else state["round"] % page_every == 0
                if not sure or backup_due:
                    try:
                        page = await check(pages[u], target[u])
                        if asin_of(page["url"]):
                            target[u] = page["url"]
                    except Exception as e:
                        if not sure:
                            raise
                        print(f"{datetime.now(TZ):%H:%M:%S} product page error on {u} (offers panel answered): {e}")
                        if "crashed" in str(e) or "closed" in str(e):
                            pages[u] = await ctx.new_page()
                r = combine(offers, page)
                state["errors"] = 0
            except Exception as e:
                state["errors"] += 1
                print(f"{datetime.now(TZ):%H:%M:%S} error on {u}: {e}")
                alert = state["errors"] == 10
                if alert:
                    await notify(f"⚠️ בוט אמזון: 10 שגיאות ברצף בטעינת הדף ({e})")
                record_event("amazon", {"link": u, "status": "error", "error": str(e)[:200],
                                        "alert": alert, "test": TEST_MODE})
                if "crashed" in str(e) or "closed" in str(e):
                    # A dead tab fails every later check, so open a fresh one.
                    # If the browser itself is gone this raises, and the all-day check takes over.
                    pages[u] = await ctx.new_page()
                return "error"

            seller = r.get("seller") or ""
            print(f"{datetime.now(TZ):%H:%M:%S} {r['status']:8} {r['price']:>10}  {r['title']}"
                  f"{f'  [{seller}]' if seller else ''}{'' if page else '  (offers)'}")
            alert = False
            in_minutes = None  # set when a product sells out again: how long it was available

            if TEST_MODE:
                await notify(
                    f"🧪 בדיקת בוט אמזון\n{r['title']}\nמצב: {status_he[r['status']]} {r['price']}"
                    f"{f' · מוכר: {seller}' if seller else ''}\n{r['url']}",
                    buttons=product_buttons(r),
                )
            elif r["status"] == "captcha":
                state["captcha_streak"] += 1
                if state["captcha_streak"] >= 5 and not state["captcha_warned"]:
                    await notify("⚠️ בוט אמזון: אמזון חוסמת את הבדיקות (CAPTCHA). ייתכן שצריך להריץ מהמחשב בבית.")
                    state["captcha_warned"] = alert = True
            else:
                state["captcha_streak"] = 0
                if r["status"] == "in" and last[u] != "in":
                    hint = ("\nאין Buy Now בדף? See All Buying Options ← Add to Cart"
                            if r.get("via") == "offers" else "")
                    await notify(f"🚨 זמין לקנייה באמזון!\n{r['title']}\n{r['price']}"
                                 f"{f' · מוכר: {seller}' if seller else ''}{hint}\n{r['url']}",
                                 buttons=product_buttons(r))
                    alert = True
                elif r["status"] == "out" and last[u] == "in":
                    in_minutes = minutes_since(since[u])
                    started = f" (מ-{datetime.fromisoformat(since[u]).astimezone(TZ):%H:%M})" if since[u] else ""
                    await notify(f"❌ אזל שוב במלאי באמזון\n{r['title']}\n"
                                 f"היה זמין {in_minutes} דקות{started}\n{r['url']}",
                                 buttons=[("פתח מוצר", r["url"])])
                    alert = True
                if r["status"] in ("in", "out") and r["status"] != last[u]:
                    last[u] = r["status"]
                    since[u] = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")

            record_event("amazon", {"link": u, "url": r["url"], "asin": asin_of(r["url"]),
                                    "product": r["title"], "status": r["status"],
                                    "price": r["price"], "alert": alert, "test": TEST_MODE,
                                    **({"seller": seller} if seller else {}),
                                    **({"via": "offers"} if r.get("via") == "offers" else {}),
                                    **({"in_minutes": in_minutes} if in_minutes is not None else {})})
            return r["status"]

        while True:
            if watching:
                WATCH_HEARTBEAT.parent.mkdir(exist_ok=True)
                WATCH_HEARTBEAT.touch()
            try:
                statuses = await asyncio.gather(*(check_one(u) for u in URLS))
            except Exception as e:
                print(f"{datetime.now(TZ):%H:%M:%S} browser gone: {e}")
                return  # the all-day check takes over
            state["round"] += 1
            if RUN_ONCE or past_end():
                break
            # Back off while Amazon is asking for CAPTCHAs, so it lets go sooner.
            slow = 3 if "captcha" in statuses else 1
            await asyncio.sleep(INTERVAL * slow * random.uniform(0.8, 1.2))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
