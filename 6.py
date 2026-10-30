"""
6.py - gets the text of every link in your spreadsheet, through your Decodo proxy,
and saves it. Built for big lists: it keeps going where it left off if you stop it.

How it gets past Cloudflare cheaply:
  1. A browser opens the first page from each site and passes the Cloudflare check
     (that's the slow ~45 s part). The proxy keeps the same IP for a while, so the pass
     stays good.
  2. Every other page from that site is fetched as plain text with the same pass and
     the same IP - no browser, no images - which is fast and uses very little data.
  3. If the site asks again, it goes back to the browser. If plain text never works on a
     site, it uses the browser for that site (skipping images and fonts to save data).
  If an IP gets blocked, that browser moves to another of your proxy ports (another IP).

Each browser uses its own proxy port, so its own IP. Addresses are never printed:
the output only shows row numbers, so it's safe to share.

Results (in the data/ folder next to this file):
  data/6-text.csv   one row per spreadsheet row: status, how it was fetched, the page's
                    title and text (cut at 32,000 characters, Excel's limit per cell)
  data/6-text.db    the same, used to pick up where it left off - don't delete it mid-job

Set the settings below, save, and press Run. Stop any time with the stop button.
"""

# ---------------- settings ----------------
LINKS_FILE = ""  # full path to your .xlsx (same as in 4.py)
FIRST_ROW = 2  # first row with a link (row 1 is usually the headings)
LAST_ROW = 0  # 0 = to the end of the sheet
LIMIT = 50  # stop after this many lookups per run (0 = no limit). Keep small while on the 100 MB trial.

USE_PROXY = True  # False = use your home internet instead
PROXY_HOST = "us.decodo.com"  # copy from Decodo > Residential > Proxy setup
PROXY_PORTS = "10001-10010"  # the port range shown there; each port is its own sticky IP
PROXY_USER = "user-sp2e2o4qkx-sessionduration-30"  # the username shown there
PROXY_PASSWORD_FILE = "~/decodo-password.txt"  # a text file on your Mac holding only the password

BROWSERS = 2  # how many browsers work at once (each on its own IP). More = faster.
TEXT_ONLY = True  # after the Cloudflare pass, fetch pages without the browser (fast, little data)
HEADLESS = False  # False = you can watch the browser windows; True = hidden
REAL_CHROME = True  # use your installed Google Chrome
PAUSE_SECONDS = (2, 5)  # random pause between lookups, per browser
TIMEZONE = ""  # "" = your Mac's. Set it (e.g. "America/New_York") if the proxy country differs from yours.
# -------------------------------------------

MAX_TEXT_CHARS = 32000
MAX_TRIES = 3  # a row that fails this many times is left alone (shown as failed in the CSV)
PORT_COOLDOWN_SECONDS = 600  # a port that got blocked rests this long before it's used again
BLOCKS_BEFORE_PAUSE = 10  # this many blocked lookups in a row (all browsers) -> pause everything
PAUSE_ALL_MINUTES = 15

import asyncio
import csv
import logging
import os
import random
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB_FILE = os.path.join(DATA, "6-text.db")
CSV_FILE = os.path.join(DATA, "6-text.csv")

CHALLENGE_TITLES = ("just a moment", "verify you are human", "attention required", "checking your browser")
BLOCK_KINDS = {"challenge", "firewall", "fingerprint", "ip_ban", "rate_limit"}
SECRETS = []  # strings that must never be printed (the proxy password)
URL_RE = re.compile(r"""\b(?:https?|wss?)://[^\s"'<>)]+""", re.I)


# ---------- privacy ----------
def mask(text):
    text = URL_RE.sub("[link]", str(text))
    for s in SECRETS:
        text = text.replace(s, "[password]").replace(quote(s, safe=""), "[password]")
    return text


def say(msg):
    print(f"[{datetime.now():%H:%M:%S}] {mask(msg)}", flush=True)


class MaskLogs(logging.Filter):
    def filter(self, record):
        record.msg, record.args = mask(record.getMessage()), None
        return True


def short(e):
    return mask(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")[:180]


# ---------- spreadsheet ----------
URL_IN_CELL = re.compile(r"""(?:https?://|www\.)[^\s"'<>)]+""", re.I)


def cell_link(cell):
    target = cell.hyperlink.target if cell.hyperlink is not None and cell.hyperlink.target else ""
    for text in (target, cell.value if isinstance(cell.value, str) else ""):
        m = URL_IN_CELL.search(text or "")
        if m:
            link = m.group(0).rstrip(".,;")
            return link if link.lower().startswith("http") else "https://" + link
    return ""


def is_black(cell):
    fill = cell.fill
    if fill is None or fill.fill_type in (None, "none"):
        return False
    c = fill.fgColor
    kind = getattr(c, "type", None)
    if kind == "rgb" and isinstance(c.rgb, str) and len(c.rgb) >= 6:
        r, g, b = (int(c.rgb[-6:][i:i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b < 40
    if kind == "theme":
        return c.theme == 1 and (c.tint or 0) <= 0.25
    if kind == "indexed":
        return c.indexed in (8, 64)
    return False


def read_links():
    """{row: link} for every row that has one. The black-highlighted link wins; else the row's first link."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("Reading the spreadsheet needs openpyxl - run 3.py again, then Run 6.py.")
    path = os.path.expanduser(LINKS_FILE.strip().strip("'\""))
    if not path:
        sys.exit("Set LINKS_FILE at the top of 6.py to your spreadsheet's path, then Run again.")
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    if not os.path.exists(path):
        sys.exit(f"Can't find the spreadsheet at {path}\nCheck LINKS_FILE at the top of 6.py.")
    say("Reading the spreadsheet (a big one can take a minute)...")
    try:
        ws = load_workbook(path).active
    except Exception as e:
        sys.exit(f"Couldn't open the spreadsheet ({type(e).__name__}). It needs to be an .xlsx file.")
    last = LAST_ROW or ws.max_row
    links = {}
    for row in ws.iter_rows(min_row=FIRST_ROW, max_row=last):
        found = [(c, cell_link(c)) for c in row]
        found = [(c, link) for c, link in found if link]
        if found:
            black = [(c, link) for c, link in found if is_black(c)]
            cell, link = (black or found)[0]
            links[cell.row] = link
    say(f"Found links in {len(links):,} rows (rows {FIRST_ROW}-{last}).")
    return links


# ---------- results database ----------
def open_db():
    os.makedirs(DATA, exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.execute("""CREATE TABLE IF NOT EXISTS results (
        row INTEGER PRIMARY KEY, status TEXT, how TEXT, chars INTEGER,
        title TEXT, text TEXT, tries INTEGER DEFAULT 0, at TEXT)""")
    db.commit()
    return db


def save_result(db, row, status, how="", title="", text=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("""INSERT INTO results(row, status, how, chars, title, text, tries, at) VALUES (?,?,?,?,?,?,1,?)
                  ON CONFLICT(row) DO UPDATE SET status=excluded.status, how=excluded.how, chars=excluded.chars,
                  title=excluded.title, text=excluded.text, tries=results.tries+1, at=excluded.at""",
               (row, status, how, len(text), title, text, now))
    db.commit()


def export_csv(db):
    tmp = CSV_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["row", "status", "how", "chars", "title", "text", "tries", "time"])
        for r in db.execute("SELECT row, status, how, chars, title, text, tries, at FROM results ORDER BY row"):
            r = list(r)
            r[5] = (r[5] or "")[:MAX_TEXT_CHARS]
            w.writerow(r)
    os.replace(tmp, CSV_FILE)


# ---------- pages ----------
def classify(page):
    status = page.status
    title = (page.css("title::text").get("") or "").strip().lower()
    text = (page.body or b"")[:300000].decode("utf-8", "ignore").lower()
    m = re.search(r"\berror(?: code)?:?\s*(10\d\d)\b", text) if "cloudflare" in text else None
    code = m.group(1) if m else ""
    headers = {k.lower(): v for k, v in (page.headers or {}).items()}
    if status == 429 or code == "1015" or "you are being rate limited" in text:
        return "rate_limit"
    if code in ("1005", "1006", "1007", "1008"):
        return "ip_ban"
    if code == "1010":
        return "fingerprint"
    if code == "1020" or "sorry, you have been blocked" in text:
        return "firewall"
    if any(t in title for t in CHALLENGE_TITLES) or headers.get("cf-mitigated") == "challenge":
        return "challenge"
    if status == 403 or "access denied" in title:
        return "firewall"
    if status == 404 or status == 410:
        return "not_found"
    if status is not None and status >= 500:
        return "error"
    return None


def page_text(page):
    text = page.get_all_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", str(text))
    title = (page.css("title::text").get("") or "").strip()
    return title, text


def site_of(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def pick_impersonation(ua):
    """The curl_cffi browser profile closest to the browser that earned the pass."""
    try:
        from curl_cffi.requests import BrowserType
        names = [b.value for b in BrowserType]
    except Exception:
        return "chrome"
    m = re.search(r"Chrome/(\d+)", ua or "")
    if not m:
        return "chrome"
    want, mac = int(m.group(1)), "Macintosh" in ua
    best = None
    for n in names:
        mm = re.fullmatch(r"chrome(\d+)(?:a)?(_macos)?", n)
        if not mm or (bool(mm.group(2)) != mac and any(re.fullmatch(rf"chrome{mm.group(1)}_macos", x) for x in names)):
            continue
        v = int(mm.group(1))
        if v <= want and (best is None or v > best[0] or (v == best[0] and bool(mm.group(2)) == mac)):
            best = (v, n)
    return best[1] if best else "chrome"


# ---------- proxy ports ----------
class Ports:
    def __init__(self, spec):
        ports = []
        for part in str(spec).replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-")
                ports += list(range(int(a), int(b) + 1))
            elif part:
                ports.append(int(part))
        self.free = ports
        self.resting = {}  # port -> time it can be used again

    async def take(self):
        while True:
            now = time.time()
            for p, until in list(self.resting.items()):
                if until <= now:
                    del self.resting[p]
                    self.free.append(p)
            if self.free:
                return self.free.pop(0)
            wait = max(5, min(self.resting.values()) - now)
            say(f"All proxy ports are resting after blocks - waiting {int(wait // 60)}m {int(wait % 60)}s.")
            await asyncio.sleep(wait)

    def give_back(self, port, blocked=False):
        if port is None:
            return
        if blocked:
            self.resting[port] = time.time() + PORT_COOLDOWN_SECONDS
        else:
            self.free.append(port)


# ---------- one browser + its plain-text twin, on one proxy port ----------
class Worker:
    def __init__(self, n, job):
        self.n, self.job = n, job
        self.port = None
        self.browser = None
        self.ua, self.lang = None, None
        self.passed = set()  # sites this IP has a pass for

    def proxy_for_browser(self):
        if not USE_PROXY:
            return None
        return {"server": f"http://{PROXY_HOST}:{self.port}", "username": PROXY_USER, "password": self.job.password}

    def proxy_for_text(self):
        if not USE_PROXY:
            return None
        return f"http://{quote(PROXY_USER, safe='')}:{quote(self.job.password, safe='')}@{PROXY_HOST}:{self.port}"

    def label(self):
        return f"Browser {self.n}" + (f" (port {self.port})" if USE_PROXY else "")

    async def open(self, new_ip=False):
        from scrapling.fetchers import AsyncStealthySession

        await self.close()
        if USE_PROXY and (new_ip or self.port is None):
            self.job.ports.give_back(self.port, blocked=new_ip)
            self.port = await self.job.ports.take()
        self.passed.clear()
        options = dict(headless=HEADLESS, real_chrome=REAL_CHROME, solve_cloudflare=True, hide_canvas=True,
                       block_webrtc=True, timeout=60000, retries=2)
        if TIMEZONE:
            options["timezone_id"] = TIMEZONE
        proxy = self.proxy_for_browser()
        if proxy:
            options["proxy"] = proxy
        self.browser = AsyncStealthySession(**options)
        await self.browser.start()

    async def close(self):
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

    async def browser_fetch(self, url, light):
        async def learn(page):
            self.ua = await page.evaluate("navigator.userAgent")
            langs = await page.evaluate("navigator.languages")
            if langs:
                self.lang = ",".join([langs[0]] + [f"{l};q={max(0.1, 0.9 - i * 0.1):.1f}" for i, l in enumerate(langs[1:])])

        page = await self.browser.fetch(url, page_action=learn, disable_resources=light, google_search=False)
        return classify(page), page

    async def text_fetch(self, url):
        from scrapling.fetchers import FetcherSession

        site = site_of(url)
        cookies = {}
        for c in await self.browser.context.cookies():
            d = (c.get("domain") or "").lstrip(".").lower()
            d = d[4:] if d.startswith("www.") else d
            if d and (site == d or site.endswith("." + d) or d.endswith("." + site)):
                cookies[c["name"]] = c["value"]
        headers = {"User-Agent": self.ua}
        if self.lang:
            headers["Accept-Language"] = self.lang
        async with FetcherSession(impersonate=pick_impersonation(self.ua), stealthy_headers=False, retries=1,
                                  timeout=30, proxy=self.proxy_for_text(), follow_redirects=True) as s:
            page = await s.get(url, headers=headers, cookies=cookies)
        return classify(page), page

    async def lookup(self, row, url):
        """Returns (status, how, page). status None = got the text."""
        job, site = self.job, site_of(url)
        if self.browser is None:
            await self.open()

        # 1. Plain text with the pass this IP already has.
        if TEXT_ONLY and site in self.passed and site not in job.browser_only and self.ua:
            try:
                kind, page = await self.text_fetch(url)
            except Exception as e:
                kind, page = "error", None
                say(f"Row {row}: plain fetch failed ({short(e)}) - using the browser.")
            if kind is None or kind == "not_found":
                job.text_worked.add(site)
                return kind, "text", page
            if kind in BLOCK_KINDS:
                self.passed.discard(site)
                if site not in job.text_worked:
                    job.text_misses[site] = job.text_misses.get(site, 0) + 1
                    if job.text_misses[site] >= 3:
                        job.browser_only.add(site)
                        say("Plain-text fetching doesn't get past this site's protection - "
                            "using the browser for it (skipping images and fonts).")

        # 2. The browser (light = skip images/fonts once this IP has passed the site).
        moves, second_look, reopened = 0, False, False
        while True:
            light = site in self.passed
            try:
                kind, page = await self.browser_fetch(url, light)
            except Exception as e:
                kind, page = "error", None
                say(f"Row {row}: browser error ({short(e)}).")
            if kind is None or kind == "not_found":
                self.passed.add(site)
                return kind, "browser", page
            if kind == "error":
                if not reopened:
                    reopened = True
                    await self.open()  # fresh browser, same IP
                    continue
                return "error", "browser", page
            # Blocked. A challenge often clears on a second look on the same IP (the pass is set by then).
            if kind == "challenge" and not second_look:
                second_look = True
                await asyncio.sleep(random.uniform(4, 8))
                continue
            if USE_PROXY and moves < 2:
                moves, second_look = moves + 1, False
                old = self.port
                await self.open(new_ip=True)
                say(f"Row {row}: blocked ({kind.replace('_', ' ')}) on port {old} - moving {self.label()} to a new IP.")
                if kind == "rate_limit":
                    await asyncio.sleep(random.uniform(20, 40))
                continue
            return kind, "browser", page


class Job:
    def __init__(self, password):
        self.password = password
        self.ports = Ports(PROXY_PORTS) if USE_PROXY else None
        self.browser_only, self.text_worked, self.text_misses = set(), set(), {}
        self.blocks_in_a_row = 0
        self.pause_until = 0
        self.done = 0
        self.stats = {"ok": 0, "blocked": 0, "error": 0, "not_found": 0, "text": 0, "browser": 0, "bytes": 0}
        self.started = time.time()


async def worker_loop(worker, queue, job, db):
    while True:
        if LIMIT and job.done >= LIMIT:
            return
        try:
            row, url = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        if time.time() < job.pause_until:
            await asyncio.sleep(job.pause_until - time.time())
        job.done += 1
        try:
            status, how, page = await worker.lookup(row, url)
        except Exception as e:
            status, how, page = "error", "", None
            say(f"Row {row}: {short(e)}")
        if page is not None:
            job.stats["bytes"] += len(page.body or b"")
        if status is None:
            title, text = page_text(page)
            save_result(db, row, "ok", how, title, text)
            job.stats["ok"] += 1
            job.stats[how] += 1
            job.blocks_in_a_row = 0
            say(f"Row {row}: ok - {len(text):,} characters ({'plain text' if how == 'text' else 'browser'}).")
        else:
            save_result(db, row, status, how)
            bucket = "blocked" if status in BLOCK_KINDS else ("not_found" if status == "not_found" else "error")
            job.stats[bucket] += 1
            say(f"Row {row}: {status.replace('_', ' ')}.")
            if bucket == "blocked":
                job.blocks_in_a_row += 1
                if job.blocks_in_a_row >= BLOCKS_BEFORE_PAUSE and time.time() >= job.pause_until:
                    job.pause_until = time.time() + PAUSE_ALL_MINUTES * 60
                    job.blocks_in_a_row = 0
                    say(f"{BLOCKS_BEFORE_PAUSE} blocked lookups in a row - pausing everything for {PAUSE_ALL_MINUTES} min.")
        if job.done % 25 == 0:
            export_csv(db)
            report(job)
        await asyncio.sleep(random.uniform(*PAUSE_SECONDS))


def report(job):
    s = job.stats
    n = max(1, s["ok"] + s["blocked"] + s["error"] + s["not_found"])
    mins = (time.time() - job.started) / 60
    say(f"So far: {s['ok']} ok ({s['text']} plain text, {s['browser']} browser), {s['blocked']} blocked, "
        f"{s['error']} errors, {s['not_found']} not found - {mins:.0f} min, "
        f"~{s['bytes'] / n / 1024:.0f} KB of page per lookup (Decodo's Usage statistics shows the real total).")


async def run():
    password = ""
    if USE_PROXY:
        pw_path = os.path.expanduser(PROXY_PASSWORD_FILE)
        try:
            password = open(pw_path, encoding="utf-8").read().strip()
        except OSError:
            sys.exit(f"Can't read the proxy password file at {pw_path}\n"
                     "Make a text file there that holds only your Decodo proxy password (see 1.md).")
        if not password:
            sys.exit(f"The password file at {pw_path} is empty.")
        SECRETS.append(password)

    links = read_links()
    db = open_db()
    finished = {r for (r,) in db.execute("SELECT row FROM results WHERE status IN ('ok','not_found') OR tries >= ?",
                                         (MAX_TRIES,))}
    todo = [(r, u) for r, u in sorted(links.items()) if r not in finished]
    say(f"{len(links) - len(todo):,} rows already done, {len(todo):,} to go"
        + (f" - this run does up to {LIMIT}." if LIMIT else "."))
    if not todo:
        export_csv(db)
        say(f"Nothing left to do. Results: {CSV_FILE}")
        return

    queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    job = Job(password)
    count = max(1, min(BROWSERS, len(todo), len(job.ports.free) if USE_PROXY else BROWSERS))
    workers = [Worker(i + 1, job) for i in range(count)]
    say(f"Starting {count} browser(s)" + (f" on Decodo ports {PROXY_PORTS}." if USE_PROXY else " on your home internet."))
    try:
        await asyncio.gather(*(worker_loop(w, queue, job, db) for w in workers))
    finally:
        for w in workers:
            await w.close()
        export_csv(db)
        report(job)
        say(f"Saved: {CSV_FILE}")


def main():
    from scrapling.fetchers import AsyncStealthySession, FetcherSession  # noqa: F401 - sets up Scrapling's logger

    logging.getLogger("scrapling").addFilter(MaskLogs())
    logging.getLogger("scrapling").setLevel(logging.WARNING)  # its per-page lines would flood the output
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        say("Stopped. Run it again to carry on from where it left off.")
    except SystemExit:
        raise
    except Exception:
        print(mask(traceback.format_exc()), file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
