"""
4.py - the watcher. Opens one page in a stealth browser every ~90 seconds from your
own internet connection (your home IP) and tells you when something new shows up.

Fill in URL below (and ITEM_SELECTOR if you want new-listing detection), then press Run.
Stop it with the stop button / Ctrl+C - it saves the browser's cookies on the way out,
so the next run reuses the Cloudflare pass instead of solving a fresh challenge.

Everything it writes goes in the data/ folder next to this file:
  data/4-checks.csv         one row per check (time, status, blocked?, items, new)
  data/4-items.csv          every new item the first time it was seen
  data/4-seen.json          items already seen, so a restart doesn't re-alert
  data/4-browser-state.json cookies + local storage (keep private, don't share)
"""

# ---------------- settings ----------------
URL = ""  # the page to watch, e.g. a search-results page sorted by newest

# Optional. CSS selector matching ONE result/listing on the page (e.g. "li.result").
# Set it and the watcher reports each new listing; leave it "" and it just
# reports when the page's text changes.
ITEM_SELECTOR = ""
PRICE_SELECTOR = ""  # optional, inside each item (e.g. ".price")

# Optional. Wait for this selector before reading the page (helps on slow, JS-heavy sites).
WAIT_SELECTOR = ""

EVERY_SECONDS = 90  # average gap between checks
JITTER_SECONDS = 20  # random +/- so checks don't land on a perfect beat
MAX_CHECKS = 0  # 0 = keep going until you stop it

HEADLESS = True  # False shows the Chrome window (try this if challenges keep looping)
REAL_CHROME = True  # use your installed Google Chrome (looks most like a real person)

MAX_BLOCKS_IN_A_ROW = 5  # stop after this many blocked checks in a row
MAX_BACKOFF_SECONDS = 1800  # blocked -> wait doubles each time, up to 30 min
# -------------------------------------------

import csv
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime
from urllib.parse import urljoin

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE_FILE = os.path.join(DATA, "4-browser-state.json")
SEEN_FILE = os.path.join(DATA, "4-seen.json")
CHECKS_CSV = os.path.join(DATA, "4-checks.csv")
ITEMS_CSV = os.path.join(DATA, "4-items.csv")

BLOCK_STATUSES = {403, 429, 503}
CHALLENGE_TITLES = ("just a moment", "attention required", "verify you are human", "access denied")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def say(msg):
    print(f"[{now()}] {msg}", flush=True)


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("url") == URL and data.get("selector") == ITEM_SELECTOR:
            return set(data.get("keys", [])), data.get("page_hash")
    except (OSError, ValueError):
        pass
    return None, None  # None = no baseline yet


def save_seen(keys, page_hash):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"url": URL, "selector": ITEM_SELECTOR, "keys": sorted(keys), "page_hash": page_hash}, f)
    os.replace(tmp, SEEN_FILE)


def is_blocked(page):
    title = (page.css("title::text").get("") or "").strip().lower()
    if page.status in BLOCK_STATUSES:
        return True, f"HTTP {page.status}"
    if any(t in title for t in CHALLENGE_TITLES):
        return True, f'challenge page ("{title}")'
    return False, ""


def read_items(page):
    items = []
    for el in page.css(ITEM_SELECTOR):
        text = " ".join(el.get_all_text(separator=" ", strip=True).split())
        href = el.attrib.get("href") if el.tag == "a" else el.css("a::attr(href)").get()
        link = urljoin(page.url or URL, href) if href else ""
        price = ""
        if PRICE_SELECTOR:
            p = el.css(PRICE_SELECTOR)
            price = " ".join(p[0].get_all_text(separator=" ", strip=True).split()) if p else ""
        key = link or text
        if key:
            items.append({"key": key, "text": text[:300], "link": link, "price": price})
    return items


def page_hash(page):
    text = page.get_all_text(separator=" ", strip=True)
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def main():
    if not URL:
        sys.exit("Set URL at the top of 4.py first (the page you want to watch), then Run again.")

    from scrapling.fetchers import StealthySession

    os.makedirs(DATA, exist_ok=True)
    seen, last_hash = load_seen()
    fetch_kwargs = {}
    if WAIT_SELECTOR:
        fetch_kwargs.update(wait_selector=WAIT_SELECTOR, wait_selector_state="attached")

    session = StealthySession(
        headless=HEADLESS,
        real_chrome=REAL_CHROME,
        solve_cloudflare=True,  # solves Turnstile / "Just a moment" pages when they show up
        hide_canvas=True,  # canvas fingerprint noise
        block_webrtc=True,  # no local-IP leak through WebRTC
        timeout=60000,  # the Cloudflare solver needs up to ~60 s
    )
    try:
        session.start()
    except Exception as e:
        if REAL_CHROME:
            sys.exit(f"Couldn't start Google Chrome ({e}).\nInstall Chrome, or set REAL_CHROME = False and Run again.")
        raise

    if os.path.exists(STATE_FILE):
        try:
            session.load_state(STATE_FILE)
            say("Reusing saved browser cookies.")
        except Exception as e:
            say(f"Couldn't load saved cookies ({e}); starting fresh.")

    say(f"Watching {URL} every ~{EVERY_SECONDS}s. Stop with the stop button or Ctrl+C.")
    checks = blocks = 0
    wait = EVERY_SECONDS
    try:
        while True:
            started = time.time()
            status, blocked, reason, found, new = "", False, "", 0, []
            try:
                page = session.fetch(URL, **fetch_kwargs)
                status = page.status
                blocked, reason = is_blocked(page)
            except Exception as e:
                blocked, reason = True, f"error: {type(e).__name__}: {e}"

            if blocked:
                blocks += 1
                wait = min(MAX_BACKOFF_SECONDS, max(wait, EVERY_SECONDS) * 2)
                say(f"Blocked ({reason}). {blocks} in a row - next try in {wait // 60}m {wait % 60}s.")
            else:
                blocks, wait = 0, EVERY_SECONDS
                if ITEM_SELECTOR:
                    items = read_items(page)
                    found = len(items)
                    if seen is None:
                        seen = {i["key"] for i in items}
                        say(f"Baseline: {found} items on the page. New ones will be reported from now on.")
                    else:
                        new = [i for i in items if i["key"] not in seen]
                        for i in new:
                            seen.add(i["key"])
                            append_csv(ITEMS_CSV, ["first_seen", "price", "text", "link"],
                                       [now(), i["price"], i["text"], i["link"]])
                            say(f"NEW: {i['price'] + '  ' if i['price'] else ''}{i['text'][:120]}  {i['link']}")
                        if found == 0:
                            say("0 items matched ITEM_SELECTOR - check the selector (or try WAIT_SELECTOR).")
                        elif not new:
                            say(f"OK - {found} items, nothing new.")
                    save_seen(seen, last_hash)
                else:
                    h = page_hash(page)
                    if last_hash is None:
                        say("Baseline saved. Changes to the page's text will be reported.")
                    elif h != last_hash:
                        new = ["page changed"]
                        say("CHANGED - the page's text is different from the last check.")
                    else:
                        say("OK - no change.")
                    last_hash = h
                    save_seen(seen or set(), last_hash)
                try:
                    session.save_state(STATE_FILE)
                except Exception:
                    pass

            append_csv(CHECKS_CSV, ["time", "status", "blocked", "reason", "items_found", "new", "seconds"],
                       [now(), status, blocked, reason, found, len(new), round(time.time() - started, 1)])

            checks += 1
            if MAX_CHECKS and checks >= MAX_CHECKS:
                say(f"Done - {checks} checks.")
                break
            if blocks >= MAX_BLOCKS_IN_A_ROW:
                say("Stopping: blocked too many times in a row. Try HEADLESS = False, a slower EVERY_SECONDS, "
                    "or run 5.py to look for the site's API instead.")
                break

            delay = wait if blocked else random.uniform(EVERY_SECONDS - JITTER_SECONDS, EVERY_SECONDS + JITTER_SECONDS)
            time.sleep(max(5, delay - (time.time() - started) if not blocked else delay))
    except KeyboardInterrupt:
        say("Stopped.")
    finally:
        try:
            session.save_state(STATE_FILE)
        except Exception:
            pass
        session.close()


if __name__ == "__main__":
    main()
