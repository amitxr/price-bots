"""
Amazon India restock watcher (morning window).

Runs in a loop from start until END_TIME (Israel time), checking all products at
once every INTERVAL seconds. When a product can be bought it sends a Telegram
alert with a direct link. It alerts again only if the product sells out and
comes back. "For sale" is reported at once; "sold out" only after SOLD_OUT_AFTER
seconds with no check saying "in", so a flicker while the last units go is not
reported as sold out and back again.

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
HOT_START, HOT_END = SETTINGS["hot_start"], SETTINGS["hot_end"]
HOT_INTERVAL = float(os.getenv("HOT_INTERVAL") or SETTINGS["hot_interval"])
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
# "Sold out" only after this long with no check saying "in". When the last units go, the product
# page and the offers panel disagree for a minute or two (5 Oct: 9 in/out messages in 2 minutes).
SOLD_OUT_AFTER = 60


def send_telegram(msg: str, buttons=None) -> bool:
    """Send a message; `buttons` is a list of (label, url) shown as link buttons under it.

    Tries 3 times (the last one without buttons, in case Telegram rejects one of the links) and
    returns False if the message did not go out, so the caller can try again on the next check.
    """
    print(msg)
    if not (TG_TOKEN and TG_CHAT):
        return True
    for attempt in range(3):
        fields = {"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": "true"}
        if buttons and attempt < 2:
            fields["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": label, "url": url} for label, url in buttons if url]]}
            )
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                   data=urllib.parse.urlencode(fields).encode(), timeout=10)
            return True
        except Exception as e:
            print(f"telegram error (try {attempt + 1} of 3): {e}")
            time.sleep(1)
    return False


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


def in_hot_window() -> bool:
    return HOT_START <= f"{datetime.now(TZ):%H:%M}" < HOT_END


def came_back_recently() -> set:
    """Product links that were for sale at least once in the history file (the last 7 days)."""
    path = history_path("amazon")
    if not path.exists():
        return set()
    return {e["link"] for e in parse_events(path.read_text(encoding="utf-8"))
            if e.get("status") == "in" and not e.get("test")}


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
    """Per product link, from the history file: (stock, since, out_since).

    stock is the reported in/out state (events carry it as "stock"; older ones only have "status"),
    since is when it started, and out_since is when the checks started saying "out" without
    an "in" in between (None if the last check said "in"). Times are UTC ISO.
    """
    path = history_path("amazon")
    last = {}
    if path.exists():
        for e in parse_events(path.read_text(encoding="utf-8")):
            if e.get("status") in ("in", "out") and not e.get("test"):
                stock, since, out_since = last.get(e["link"], (None, None, None))
                new_stock = e.get("stock", e["status"])
                if new_stock != stock:
                    stock, since = new_stock, e["ts"]
                out_since = None if e["status"] == "in" else out_since or e["ts"]
                last[e["link"]] = (stock, since, out_since)
    return last


def seconds_since(ts) -> float:
    return (datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(ts)).total_seconds() if ts else 0


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
        return {**info, "status": "unknown", "why": f"HTTP {resp.status}, {len(html)} bytes"}
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


async def notify(msg: str, buttons=None) -> bool:
    """send_telegram without holding up the other checks while Telegram answers."""
    return await asyncio.to_thread(send_telegram, msg, buttons)


async def main():
    status_he = {"in": "✅ במלאי", "out": "❌ אזל", "captcha": "🤖 אמזון ביקשה CAPTCHA", "unknown": "❓ לא זוהה"}
    if BACKGROUND and watch_alive():
        return  # the morning watch is already checking every few seconds
    state = {"captcha_streak": 0, "captcha_warned": captcha_warned_recently(), "errors": 0}
    prune_file(history_path("amazon"), days=7)
    known = last_known_status()
    last = {u: known.get(u, (None,))[0] for u in URLS}  # reported stock: changes only through an alert
    since = {u: known.get(u, (None, None))[1] for u in URLS}  # when the current stock started
    out_since = {u: known.get(u, (None, None, None))[2] for u in URLS}  # first "out" since the last "in"

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

        page_locks = {u: asyncio.Lock() for u in URLS}  # one tab per product: one page load at a time
        alert_locks = {u: asyncio.Lock() for u in URLS}

        async def load_page(u: str) -> dict:
            async with page_locks[u]:
                page = await check(pages[u], target[u])
            if asin_of(page["url"]):
                target[u] = page["url"]
            return page

        async def check_one(u: str, use_offers=True, use_page=False):
            """Check one product, alert if needed and record it. The full page is also loaded when
            use_page is set or the offers panel gives no clear answer."""
            try:
                offers = page = None
                asin = asin_of(target[u])
                if use_offers and asin:
                    try:
                        offers = await check_offers(ctx, asin)
                    except Exception as e:
                        print(f"{datetime.now(TZ):%H:%M:%S} offers panel error on {u}: {e}")
                sure = offers and offers["status"] in ("in", "out")
                if offers and not sure:
                    print(f"{datetime.now(TZ):%H:%M:%S} offers panel unclear on {u} "
                          f"({offers['status']}{', ' + offers['why'] if offers.get('why') else ''}): loading the page")
                if not sure or use_page:
                    try:
                        page = await load_page(u)
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
            # The offers loop and the page loop of a product can finish together: decide and send
            # one at a time, so a restock is announced once.
            async with alert_locks[u]:
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
                        state["captcha_warned"] = alert = await notify(
                            "⚠️ בוט אמזון: אמזון חוסמת את הבדיקות (CAPTCHA). ייתכן שצריך להריץ מהמחשב בבית.")
                else:
                    state["captcha_streak"] = 0
                    now = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
                    out_since[u] = None if r["status"] == "in" else out_since[u] or now
                    if r["status"] == "in" and last[u] != "in":
                        hint = ("\nאין Buy Now בדף? See All Buying Options ← Add to Cart"
                                if r.get("via") == "offers" else "")
                        # Not sent (Telegram unreachable): the stock stays "out", so the next check tries again.
                        alert = await notify(f"🚨 זמין לקנייה באמזון!\n{r['title']}\n{r['price']}"
                                             f"{f' · מוכר: {seller}' if seller else ''}{hint}\n{r['url']}",
                                             buttons=product_buttons(r))
                    elif r["status"] == "out" and last[u] == "in" and seconds_since(out_since[u]) >= SOLD_OUT_AFTER:
                        in_minutes = minutes_since(since[u])
                        started = f" (מ-{datetime.fromisoformat(since[u]).astimezone(TZ):%H:%M})" if since[u] else ""
                        alert = await notify(f"❌ אזל שוב במלאי באמזון\n{r['title']}\n"
                                             f"היה זמין {in_minutes} דקות{started}\n{r['url']}",
                                             buttons=[("פתח מוצר", r["url"])])
                    if r["status"] in ("in", "out") and (alert or last[u] is None):  # stock changes only with its message
                        last[u], since[u] = r["status"], now

                record_event("amazon", {"link": u, "url": r["url"], "asin": asin_of(r["url"]),
                                        "product": r["title"], "status": r["status"],
                                        "price": r["price"], "alert": alert, "test": TEST_MODE,
                                        **({"stock": last[u]} if r["status"] in ("in", "out") and not TEST_MODE else {}),
                                        **({"seller": seller} if seller else {}),
                                        **({"via": "offers"} if r.get("via") == "offers" else {}),
                                        **({"in_minutes": in_minutes} if in_minutes is not None else {})})
            return r["status"]

        if RUN_ONCE:
            # All-day runs are a new process every minute: the full page only every 10 minutes there.
            full_page = datetime.now().minute % 10 == 0 if BACKGROUND else True
            try:
                await asyncio.gather(*(check_one(u, use_page=full_page) for u in URLS))
                await browser.close()
            except Exception as e:
                print(f"{datetime.now(TZ):%H:%M:%S} browser gone: {e}")
            return

        # The morning watch: every product has its own loops, so a slow product page never holds up
        # the offers checks (the ones that catch a restock first) of this or any other product.
        # Stock lasts under a minute, so the cards that have been coming back get the fastest checks
        # in the minutes they come back. The rest keep INTERVAL, to keep Amazon from showing CAPTCHAs.
        hot = came_back_recently() & set(URLS)
        print(f"{datetime.now(TZ):%H:%M:%S} watch: every {INTERVAL}s; "
              f"{len(hot)} card(s) every {HOT_INTERVAL:g}s from {HOT_START} to {HOT_END}")

        async def keep_checking(u: str, use_offers: bool, every: float, start_after: float = 0):
            await asyncio.sleep(start_after)
            while not past_end() and browser.is_connected():
                started = time.monotonic()
                try:
                    status = await check_one(u, use_offers=use_offers, use_page=not use_offers)
                except Exception as e:  # e.g. a dead tab that could not be reopened
                    print(f"{datetime.now(TZ):%H:%M:%S} check failed on {u}: {e}")
                    status = "error"
                wait = HOT_INTERVAL if use_offers and u in hot and in_hot_window() else every
                # Back off while Amazon is asking for CAPTCHAs, so it lets go sooner.
                wait *= (3 if status == "captcha" else 1) * random.uniform(0.8, 1.2)
                # Counted from the start of the check, so a 2 s rhythm is 2 s and not 2 s plus Amazon's answer.
                await asyncio.sleep(max(0.5, wait - (time.monotonic() - started)))

        loops = [asyncio.create_task(keep_checking(u, True, INTERVAL)) for u in URLS] + [
            asyncio.create_task(keep_checking(u, False, PAGE_EVERY_SECONDS, random.uniform(0, PAGE_EVERY_SECONDS)))
            for u in URLS]
        WATCH_HEARTBEAT.parent.mkdir(exist_ok=True)
        while not past_end() and browser.is_connected():
            WATCH_HEARTBEAT.touch()
            await asyncio.sleep(5)
        # Stop now, not after each loop's current wait, so this never overlaps the all-day check;
        # and remove the heartbeat so that check starts on its next minute instead of ~2 minutes later.
        for t in loops:
            t.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        WATCH_HEARTBEAT.unlink(missing_ok=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
