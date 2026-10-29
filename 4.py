"""
4.py - the watcher. Opens one page in a stealth browser every ~90 seconds from your
own internet connection (your home IP) and tells you when something new shows up.

Fill in URL below (and ITEM_SELECTOR if you want new-listing detection), then press Run.
Stop it with the stop button / Ctrl+C - it saves the browser's cookies on the way out,
so the next run reuses the Cloudflare pass instead of solving a fresh challenge.

When it gets blocked it works out what kind of block it was, tries the fix that kind
usually needs, and remembers what worked for that site (data/4-learned.json). When it
runs out of fixes it goes quiet - sleeps for hours, then makes one test check - instead
of hammering the site. It never quits on its own; stop it whenever you like.

Everything it writes goes in the data/ folder next to this file:
  data/4-checks.csv         one row per check
  data/4-blocks.csv         every block: what kind, and what it did about it
  data/4-items.csv          every new item the first time it was seen
  data/4-learned.json       what it has learned per site (speed, browser, selector)
  data/4-seen.json          items already seen, so a restart doesn't re-alert
  data/4-adaptive.db        listing "fingerprints" used to re-find them after a redesign
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

EVERY_SECONDS = 90  # starting gap between checks (it adjusts this itself over time)
JITTER_SECONDS = 20  # random +/- so checks don't land on a perfect beat
MAX_CHECKS = 0  # 0 = keep going until you stop it

HEADLESS = True  # False shows the Chrome window (it switches this itself if it helps)
REAL_CHROME = True  # use your installed Google Chrome (looks most like a real person)

# Auto-fix and learning
AUTO_FIX = True  # False = only back off and sleep, never change settings
FASTEST_SECONDS = 60  # it never speeds up past this
SLOWEST_SECONDS = 900  # slow-downs stop here (15 min)
SPEED_UP_AFTER_HOURS = 6  # this long without a block -> try checking ~10% faster
SLEEP_HOURS = [3, 6, 12, 24]  # out of fixes: sleep this long before each quiet test check
RESET_LEARNING = False  # True = forget what it learned about this site (set back to False after)
# -------------------------------------------

TRIAL_HOURS = 2  # a speed-up has to survive this long to be kept
MAX_BACKOFF_SECONDS = 1800  # waits between fix attempts double up to this (30 min)
OFFLINE_RETRY_SECONDS = 60
UPDATE_CHECK_HOURS = 24

import csv
import email.utils
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE_FILE = os.path.join(DATA, "4-browser-state.json")
SEEN_FILE = os.path.join(DATA, "4-seen.json")
LEARNED_FILE = os.path.join(DATA, "4-learned.json")
ADAPTIVE_DB = os.path.join(DATA, "4-adaptive.db")
CHECKS_CSV = os.path.join(DATA, "4-checks.csv")
BLOCKS_CSV = os.path.join(DATA, "4-blocks.csv")
ITEMS_CSV = os.path.join(DATA, "4-items.csv")

LABELS = {
    "rate_limit": "rate limit",
    "challenge": "challenge page it couldn't pass",
    "fingerprint": "browser-fingerprint ban",
    "firewall": "firewall block",
    "ip_ban": "IP ban",
    "soft_block": "quiet block (page loaded but its content was missing)",
    "error": "repeated page errors",
}

# What to try, in order, for each kind of block. Each try also waits twice as long as the last.
LADDER = {
    "rate_limit": ["wait", "slow_down", "slow_down", "slow_down"],
    "challenge": ["wait", "visible_chrome", "fresh_session", "slow_down"],
    "fingerprint": ["fresh_session", "visible_chrome", "switch_browser"],
    "firewall": ["fresh_session", "visible_chrome", "slow_down"],
    "soft_block": ["fresh_session", "visible_chrome", "slow_down"],
    "error": ["wait", "fresh_session", "wait"],
    "ip_ban": [],  # nothing on this computer fixes a banned IP - go straight to sleeping
}
FIX_TEXT = {
    "wait": "waiting it out",
    "slow_down": "slowing down",
    "visible_chrome": "switching to a visible Chrome window",
    "fresh_session": "starting a fresh browser session (new cookies and fingerprint)",
    "switch_browser": "switching browser (Chrome <-> bundled Chromium)",
}
SPEED_KINDS = {"rate_limit", "challenge", "firewall", "soft_block", "fingerprint"}  # blocks that can mean "too fast"
OFFLINE_ERRORS = ("ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED", "ERR_NETWORK_CHANGED",
                  "ERR_ADDRESS_UNREACHABLE", "ERR_NETWORK_ACCESS_DENIED", "ERR_PROXY_CONNECTION_FAILED")
CHALLENGE_TITLES = ("just a moment", "verify you are human", "attention required", "checking your browser")


# ---------- small helpers ----------
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


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def hget(headers, name):
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


def retry_after_seconds(headers):
    value = hget(headers, "retry-after")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return None


def fmt(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


def check_for_scrapling_update():
    try:
        from importlib.metadata import version
        from urllib.request import urlopen

        have = version("scrapling")
        with urlopen("https://pypi.org/pypi/scrapling/json", timeout=5) as r:
            latest = json.load(r)["info"]["version"]
        if latest != have:
            say(f"Note: Scrapling {latest} is out (you have {have}). 2.txt pins the tested version - "
                "update it on purpose, not blind, since new versions sometimes rename options.")
    except Exception:
        pass  # offline or PyPI unreachable - not important


# ---------- what it has learned about each site ----------
class Learned:
    def __init__(self):
        self.site = urlparse(URL).netloc or URL
        everything = read_json(LEARNED_FILE, {})
        p = {} if RESET_LEARNING else everything.get(self.site, {})
        base = {"every": EVERY_SECONDS, "headless": HEADLESS, "real_chrome": REAL_CHROME, "item_selector": ITEM_SELECTOR}
        old = p.get("base", {})
        # If you changed a setting at the top, your new value wins over what was learned.
        if old.get("every") != EVERY_SECONDS:
            p.pop("every", None), p.pop("floor", None), p.pop("trial", None)
        if old.get("headless") != HEADLESS:
            p.pop("headless", None)
        if old.get("real_chrome") != REAL_CHROME:
            p.pop("real_chrome", None)
        if old.get("item_selector") != ITEM_SELECTOR:
            p.pop("item_selector", None), p.pop("good_links", None), p.pop("had_items", None)
        p["base"] = base
        p.setdefault("every", EVERY_SECONDS)
        p.setdefault("headless", HEADLESS)
        p.setdefault("real_chrome", REAL_CHROME)
        p.setdefault("blocks", {})
        p.setdefault("clean_since", time.time())
        self.p = p
        self.save()

    def __getitem__(self, k):
        return self.p.get(k)

    def __setitem__(self, k, v):
        self.p[k] = v

    def save(self):
        everything = read_json(LEARNED_FILE, {})
        everything[self.site] = self.p
        write_json(LEARNED_FILE, everything)

    def summary(self):
        bits = [f"every ~{fmt(self.p['every'])}"]
        if self.p.get("floor"):
            bits.append(f"never faster than {fmt(self.p['floor'] * 1.1)}")
        if self.p["headless"] != HEADLESS:
            bits.append("visible Chrome" if not self.p["headless"] else "hidden Chrome")
        if self.p["real_chrome"] != REAL_CHROME:
            bits.append("bundled Chromium" if not self.p["real_chrome"] else "real Chrome")
        if self.p.get("item_selector"):
            bits.append(f'listings at "{self.p["item_selector"]}"')
        if self.p.get("last_fix_that_worked"):
            bits.append(f"last fix that worked: {FIX_TEXT[self.p['last_fix_that_worked']]}")
        return ", ".join(bits)


class Watcher:
    def __init__(self):
        from scrapling.fetchers import StealthySession
        from scrapling.parser import Selector

        self.StealthySession, self.Selector = StealthySession, Selector
        self.learned = Learned()
        seen = read_json(SEEN_FILE, {})
        same = seen.get("url") == URL and seen.get("selector") == ITEM_SELECTOR
        self.seen = set(seen.get("keys", [])) if same and "keys" in seen else None
        self.last_hash = seen.get("page_hash") if same else None
        self.session = None
        self.last_layout_warning = 0

    # ----- browser -----
    def open_session(self, fresh=False):
        self.close_session(save=not fresh)
        if fresh and os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        s = self.StealthySession(
            headless=self.learned["headless"],
            real_chrome=self.learned["real_chrome"],
            solve_cloudflare=True,  # solves Turnstile / "Just a moment" pages when they show up
            hide_canvas=True,  # canvas fingerprint noise
            block_webrtc=True,  # no local-IP leak through WebRTC
            timeout=60000,  # the Cloudflare solver needs up to ~60 s
        )
        s.start()
        if not fresh and os.path.exists(STATE_FILE):
            try:
                s.load_state(STATE_FILE)
            except Exception as e:
                say(f"Couldn't load saved cookies ({e}); continuing without them.")
        self.session = s

    def close_session(self, save=True):
        if self.session is None:
            return
        if save:
            try:
                self.session.save_state(STATE_FILE)
            except Exception:
                pass
        try:
            self.session.close()
        except Exception:
            pass
        self.session = None

    # ----- one check -----
    def classify(self, page):
        status = page.status
        title = (page.css("title::text").get("") or "").strip().lower()
        text = (page.body or b"")[:300000].decode("utf-8", "ignore").lower()
        m = re.search(r"\berror(?: code)?:?\s*(10\d\d)\b", text) if "cloudflare" in text else None
        cf_code = m.group(1) if m else ""
        if status == 429 or cf_code == "1015" or "you are being rate limited" in text:
            return "rate_limit", f"HTTP {status}" + (f", Cloudflare {cf_code}" if cf_code else "")
        if cf_code in ("1005", "1006", "1007", "1008"):
            return "ip_ban", f"Cloudflare {cf_code}"
        if cf_code == "1010":
            return "fingerprint", "Cloudflare 1010"
        if cf_code == "1020" or "sorry, you have been blocked" in text:
            return "firewall", f"HTTP {status}" + (f", Cloudflare {cf_code}" if cf_code else "")
        if any(t in title for t in CHALLENGE_TITLES) or hget(page.headers, "cf-mitigated") == "challenge":
            return "challenge", f'HTTP {status}, "{title}"'
        if status == 403 or "access denied" in title:
            return "firewall", f"HTTP {status}" + (f', "{title}"' if title else "")
        if status is not None and status >= 500:
            return "error", f"HTTP {status} (site trouble)"
        return None, ""

    def adaptive_view(self, page):
        return self.Selector(page.body or b"", url=URL, adaptive=True,
                             storage_args={"storage_file": ADAPTIVE_DB, "url": URL})

    def find_items(self, page):
        """Return (elements, how). Uses the learned selector, then Scrapling's adaptive matching."""
        sel = self.learned["item_selector"] or ITEM_SELECTOR
        try:
            els = page.css(sel)
        except Exception:
            els = []
        try:
            view = self.adaptive_view(page)
        except Exception:
            return list(els), "selector"
        if els:
            try:
                view.css(sel, auto_save=True, identifier=ITEM_SELECTOR)  # keep the fingerprint fresh
            except Exception:
                pass
            return list(els), "selector"
        try:
            found = view.css(ITEM_SELECTOR, adaptive=True, identifier=ITEM_SELECTOR)
        except Exception:
            found = []
        if not found:
            return [], None
        first = found[0]
        els = [first] + list(first.find_similar())
        for cls in (first.attrib.get("class") or "").split():
            candidate = f"{first.tag}.{cls}"
            try:
                n = len(page.css(candidate))
            except Exception:
                continue
            if n >= max(1, 0.8 * len(els)):
                self.learned["item_selector"] = candidate
                say(f'The site changed its layout - listings are now at "{candidate}". Using that from now on.')
                return list(page.css(candidate)), "learned"
        say(f"The site changed its layout - found {len(els)} listings again by matching their old look.")
        return els, "adaptive"

    def read_items(self, page, els):
        items = []
        for el in els:
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

    def check(self):
        """One page load. Returns (kind, detail, status, found, new_count, retry_after); kind None = fine."""
        fetch_kwargs = {}
        if WAIT_SELECTOR:
            fetch_kwargs.update(wait_selector=WAIT_SELECTOR, wait_selector_state="attached")
        try:
            page = self.session.fetch(URL, **fetch_kwargs)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}".splitlines()[0][:200]
            kind = "offline" if any(x in str(e) for x in OFFLINE_ERRORS) else "error"
            return kind, msg, "", 0, 0, None

        kind, detail = self.classify(page)
        if kind:
            return kind, detail, page.status, 0, 0, retry_after_seconds(page.headers)

        found, new = 0, []
        if ITEM_SELECTOR:
            els, how = self.find_items(page)
            links = len(page.css("a[href]"))
            if not els:
                good = self.learned["good_links"]
                if good and links < 0.2 * good:
                    return "soft_block", f"0 listings and {links} links (normally ~{good})", page.status, 0, 0, None
                if time.time() - self.last_layout_warning > 3600:
                    self.last_layout_warning = time.time()
                    if self.learned["had_items"]:
                        say("The page looks normal but no listings were found - the layout probably changed "
                            "more than it can follow. Update ITEM_SELECTOR.")
                    else:
                        say("0 items matched ITEM_SELECTOR - check the selector (or try WAIT_SELECTOR).")
            else:
                self.learned["had_items"] = True
                self.learned["good_links"] = links
            items = self.read_items(page, els)
            found = len(items)
            if self.seen is None:
                if items:
                    self.seen = {i["key"] for i in items}
                    say(f"Baseline: {found} items on the page. New ones will be reported from now on.")
            else:
                new = [i for i in items if i["key"] not in self.seen]
                for i in new:
                    self.seen.add(i["key"])
                    append_csv(ITEMS_CSV, ["first_seen", "price", "text", "link"],
                               [now(), i["price"], i["text"], i["link"]])
                    say(f"NEW: {i['price'] + '  ' if i['price'] else ''}{i['text'][:120]}  {i['link']}")
                if items and not new:
                    say(f"OK - {found} items, nothing new.")
        else:
            text = page.get_all_text(separator=" ", strip=True)
            h = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
            if self.last_hash is None:
                say("Baseline saved. Changes to the page's text will be reported.")
            elif h != self.last_hash:
                new = ["page changed"]
                say("CHANGED - the page's text is different from the last check.")
            else:
                say("OK - no change.")
            self.last_hash = h

        write_json(SEEN_FILE, {"url": URL, "selector": ITEM_SELECTOR,
                               **({"keys": sorted(self.seen)} if self.seen is not None else {}),
                               "page_hash": self.last_hash})
        try:
            self.session.save_state(STATE_FILE)
        except Exception:
            pass
        return None, "", page.status, found, len(new), None

    def log_check(self, result, seconds):
        kind, detail, status, found, new, _ = result
        L = self.learned
        append_csv(CHECKS_CSV, ["time", "result", "detail", "status", "items_found", "new", "seconds",
                                "every_s", "headless", "real_chrome"],
                   [now(), kind or "ok", detail, status, found, new, round(seconds, 1),
                    round(L["every"]), L["headless"], L["real_chrome"]])

    def log_block(self, kind, detail, status, action):
        L = self.learned
        append_csv(BLOCKS_CSV, ["time", "kind", "detail", "status", "every_s", "headless", "real_chrome", "action"],
                   [now(), kind, detail, status, round(L["every"]), L["headless"], L["real_chrome"], action])

    # ----- learning -----
    def after_clean_check(self):
        L, t = self.learned, time.time()
        trial = L["trial"]
        if trial and t >= trial["until"]:
            L["trial"] = None
            say(f"Checking every ~{fmt(L['every'])} has held up - keeping it.")
        elif not trial and AUTO_FIX and t - L["clean_since"] >= SPEED_UP_AFTER_HOURS * 3600:
            L["clean_since"] = t
            target = max(FASTEST_SECONDS, L["every"] * 0.9)
            if L["floor"]:
                target = max(target, L["floor"] * 1.1)
            if target < L["every"] - 1:
                L["trial"] = {"from": L["every"], "until": t + TRIAL_HOURS * 3600}
                say(f"{SPEED_UP_AFTER_HOURS} h without a block - trying a check every ~{fmt(target)} "
                    f"(was {fmt(L['every'])}).")
                L["every"] = target

    def note_block(self, kind):
        L = self.learned
        L["blocks"][kind] = L["blocks"].get(kind, 0) + 1
        L["clean_since"] = time.time()
        if kind in SPEED_KINDS and L["trial"]:
            tried, back = L["every"], L["trial"]["from"]
            L["floor"] = max(L["floor"] or 0, tried)
            L["every"], L["trial"] = back, None
            say(f"That speed-up was too fast for this site - back to ~{fmt(back)}, and it won't go "
                f"below ~{fmt(tried * 1.1)} again.")
        elif kind == "rate_limit":
            L["floor"] = max(L["floor"] or 0, L["every"])

    def apply_fix(self, fix):
        """Apply one fix. Returns False if it didn't apply (so the next one should be tried)."""
        L = self.learned
        if fix == "wait":
            return True
        if fix == "slow_down":
            if L["every"] >= SLOWEST_SECONDS:
                return False
            L["every"] = min(SLOWEST_SECONDS, L["every"] * 1.5)
            say(f"Checking every ~{fmt(L['every'])} from now on.")
            return True
        if fix == "visible_chrome":
            if not L["headless"]:
                return False
            L["headless"] = False
            try:
                self.open_session()
                return True
            except Exception as e:
                L["headless"] = True
                say(f"Couldn't open a visible Chrome window ({str(e).splitlines()[0][:120]}).")
                self.open_session()
                return False
        if fix == "fresh_session":
            self.open_session(fresh=True)
            return True
        if fix == "switch_browser":
            L["real_chrome"] = not L["real_chrome"]
            try:
                self.open_session(fresh=True)
                return True
            except Exception as e:
                L["real_chrome"] = not L["real_chrome"]
                say(f"Couldn't switch browser ({str(e).splitlines()[0][:120]}).")
                self.open_session(fresh=True)
                return False
        return False

    # ----- out of fixes -----
    def sleep_mode(self, kind):
        level = 0
        while True:
            hours = SLEEP_HOURS[min(level, len(SLEEP_HOURS) - 1)]
            reason = "This IP is banned by the site - only time or a different IP fixes that. " if kind == "ip_ban" else ""
            say(f"Out of fixes for: {LABELS.get(kind, kind)}. {reason}Going quiet for {hours} h, then one test check. "
                "(You can stop it any time.)")
            self.log_block(kind, "out of fixes", "", f"sleep {hours} h")
            self.close_session()
            time.sleep(hours * 3600)
            try:
                self.open_session(fresh=True)
            except Exception as e:
                say(f"Couldn't start the browser ({e}); trying again after the next sleep.")
                level += 1
                continue
            started = time.time()
            result = self.check()
            self.log_check(result, time.time() - started)
            k = result[0]
            if k is None:
                L = self.learned
                L["every"] = min(SLOWEST_SECONDS, L["every"] * 1.5)
                L["trial"] = None
                say(f"Test check passed - watching again, now every ~{fmt(L['every'])}.")
                self.learned.save()
                return
            if k == "offline":
                say("No internet connection during the test check - will try again after the next sleep.")
                continue
            self.note_block(k)
            kind = k
            say(f"Still blocked ({LABELS.get(k, k)}).")
            level += 1

    # ----- main loop -----
    def run(self):
        L = self.learned
        L["clean_since"] = time.time()  # only time spent actually running counts toward speeding up
        if L["blocks"]:
            say(f"What it has learned about {L.site}: {L.summary()}.")
        try:
            self.open_session()
        except Exception as e:
            if L["real_chrome"]:
                sys.exit(f"Couldn't start Google Chrome ({e}).\nInstall Chrome, or set REAL_CHROME = False and Run again.")
            raise
        if os.path.exists(STATE_FILE):
            say("Reusing saved browser cookies.")
        say(f"Watching {URL} every ~{fmt(L['every'])}. Stop with the stop button or Ctrl+C.")

        checks, steps, last_fix, wait, last_update_check = 0, {}, None, None, 0
        while True:
            if time.time() - last_update_check > UPDATE_CHECK_HOURS * 3600:
                last_update_check = time.time()
                check_for_scrapling_update()

            started = time.time()
            result = self.check()
            kind, detail, status, _, _, retry_after = result
            self.log_check(result, time.time() - started)

            if kind is None:
                if steps:
                    say("Unblocked" + (f" - {FIX_TEXT[last_fix]} worked." if last_fix and last_fix != "wait" else "."))
                    if last_fix:
                        L["last_fix_that_worked"] = last_fix
                steps, last_fix, wait = {}, None, None
                self.after_clean_check()
                ratio = JITTER_SECONDS / EVERY_SECONDS if EVERY_SECONDS else 0
                delay = random.uniform(L["every"] * (1 - ratio), L["every"] * (1 + ratio)) - (time.time() - started)
            elif kind == "offline":
                say(f"No internet connection ({detail}) - trying again in {OFFLINE_RETRY_SECONDS}s.")
                delay = OFFLINE_RETRY_SECONDS
            else:
                self.note_block(kind)
                ladder = LADDER.get(kind, []) if AUTO_FIX else ["wait"] * 4
                fix = None
                while steps.get(kind, 0) < len(ladder):
                    candidate = ladder[steps.get(kind, 0)]
                    steps[kind] = steps.get(kind, 0) + 1
                    if self.apply_fix(candidate):
                        fix = candidate
                        break
                if fix is None:
                    self.log_block(kind, detail, status, "out of fixes")
                    self.sleep_mode(kind)
                    steps, last_fix, wait = {}, None, None
                    delay = L["every"]
                else:
                    last_fix = fix if fix != "wait" else (last_fix or "wait")
                    wait = min(MAX_BACKOFF_SECONDS, max(wait or L["every"], L["every"]) * 2)
                    if retry_after:
                        wait = min(6 * 3600, retry_after + random.uniform(5, 30))
                    say(f"Blocked: {LABELS[kind]} ({detail}). Fix: {FIX_TEXT[fix]}. Next try in {fmt(wait)}.")
                    self.log_block(kind, detail, status, fix)
                    delay = wait

            L.save()
            checks += 1
            if MAX_CHECKS and checks >= MAX_CHECKS:
                say(f"Done - {checks} checks.")
                return
            time.sleep(max(5, delay))


def main():
    if not URL:
        sys.exit("Set URL at the top of 4.py first (the page you want to watch), then Run again.")
    os.makedirs(DATA, exist_ok=True)
    w = Watcher()
    try:
        w.run()
    except KeyboardInterrupt:
        say("Stopped.")
    finally:
        w.learned.save()
        w.close_session()


if __name__ == "__main__":
    main()
