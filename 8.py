"""
8.py - opens every link in your private links.csv through your Decodo proxy and collects
the bits of text you chose, without anything private ever leaving your Mac.

Everything private lives in one folder on your Mac (PRIVATE_FOLDER, default ~/scrap-private):
  links.csv            your links (you put it here)
  samples/             the first pages, saved complete, for the other chat to look at
  HOW-TO-fields.md     instructions for the other chat (written by the sample run)
  fields.json          what to collect from each page (the other chat writes this)
  results.csv          what was collected: one row per link
  8-results.db         the same, used to carry on where it left off

How a run goes:
  - No fields.json yet  -> SAMPLE RUN: opens the first SAMPLE_PAGES links, saves them into
    samples/, writes HOW-TO-fields.md and stops.
  - fields.json exists  -> FULL RUN: opens every link not done yet, pulls out those items,
    adds them to results.csv. Links you add to links.csv later are picked up next run
    (or automatically, every WATCH_MINUTES, if you set it).

Getting past Cloudflare cheaply (same as 6.py): a browser passes the check once per proxy IP;
after that pages come as plain text over the same IP with the same pass. If an item can only
be seen in the browser (some sites draw parts of the page with JavaScript), it uses the
browser for that site.

The output in the terminal only ever shows row numbers and counts - never links, page text
or what was collected - so it's safe to share.
"""

# ---------------- settings ----------------
PRIVATE_FOLDER = "~/scrap-private"  # your private folder on this Mac
LINKS_FILE = "links.csv"  # inside the private folder
LINK_COLUMN = 0  # 0 = the first link found in each row; or a column number (1 = first column) or its heading
SAMPLE_PAGES = 20  # how many pages the sample run saves
LIMIT = 50  # stop after this many lookups per run (0 = no limit). Keep small while on the 100 MB trial.
WATCH_MINUTES = 0  # 0 = stop when done; e.g. 30 = keep running and check links.csv for new links every 30 min

USE_PROXY = True  # False = use your home internet instead
PROXY_HOST = "us.decodo.com"  # from Decodo > Residential > Proxy setup
PROXY_PORTS = "10001-10010"  # each port is its own sticky IP
PROXY_USER = "user-sp2e2o4qkx-sessionduration-30"
PROXY_PASSWORD_FILE = "~/decodo-password.txt"  # a text file on your Mac holding only the password

BROWSERS = 2  # how many browsers work at once (each on its own IP). More = faster.
TEXT_ONLY = True  # after the Cloudflare pass, fetch pages without the browser (fast, little data)
HEADLESS = False  # False = you can watch the browser windows; True = hidden
REAL_CHROME = True  # use your installed Google Chrome
PAUSE_SECONDS = (2, 5)  # random pause between lookups, per browser
MATCH_TIMEZONE = True  # set each browser's time zone to where its proxy IP is (Cloudflare checks this)
# -------------------------------------------

IP_CHECK_URL = "https://ip.decodo.com/json"  # tells where a proxy IP is; used for MATCH_TIMEZONE
MAX_FIELD_CHARS = 32000  # Excel's limit per cell
MAX_TRIES = 3  # a link that fails this many times is left alone
PORT_COOLDOWN_SECONDS = 600  # a port that got blocked rests this long before it's used again
BLOCKS_BEFORE_PAUSE = 10  # this many blocked lookups in a row (all browsers) -> pause everything
PAUSE_ALL_MINUTES = 15

import asyncio
import csv
import hashlib
import json
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

FOLDER = os.path.expanduser(PRIVATE_FOLDER)
SAMPLES = os.path.join(FOLDER, "samples")
FIELDS_FILE = os.path.join(FOLDER, "fields.json")
HOWTO_FILE = os.path.join(FOLDER, "HOW-TO-fields.md")
RESULTS_CSV = os.path.join(FOLDER, "results.csv")
DB_FILE = os.path.join(FOLDER, "8-results.db")

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


# ---------- links.csv ----------
URL_IN_CELL = re.compile(r"""(?:https?://|www\.)[^\s"'<>)]+""", re.I)


def cell_link(text):
    m = URL_IN_CELL.search(text or "")
    if not m:
        return ""
    link = m.group(0).rstrip(".,;")
    return link if link.lower().startswith("http") else "https://" + link


def read_links():
    """[(row, link)] in file order, one per row that has a link. Row 1 is the first line of the file."""
    path = os.path.join(FOLDER, LINKS_FILE)
    if not os.path.exists(path):
        sys.exit(f"Can't find {path}\nPut your CSV in {FOLDER} and name it {LINKS_FILE}.")
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    column = None
    if isinstance(LINK_COLUMN, int) and LINK_COLUMN > 0:
        column = LINK_COLUMN - 1
    elif isinstance(LINK_COLUMN, str) and LINK_COLUMN.strip() and rows:
        heads = [h.strip().lower() for h in rows[0]]
        if LINK_COLUMN.strip().lower() not in heads:
            sys.exit(f'No column headed "{LINK_COLUMN}" in {LINKS_FILE}. Check LINK_COLUMN at the top of 8.py.')
        column = heads.index(LINK_COLUMN.strip().lower())
    links, empty, seen = [], 0, set()
    for i, row in enumerate(rows, start=1):
        cells = [row[column]] if column is not None and column < len(row) else ([] if column is not None else row)
        link = next((cell_link(c) for c in cells if cell_link(c)), "")
        if not link:
            if any(c.strip() for c in row):
                empty += 1
            continue
        if link not in seen:
            seen.add(link)
            links.append((i, link))
    say(f"{LINKS_FILE}: {len(links):,} links found ({len(rows):,} lines).")
    if empty > max(5, len(links)):
        say("Most rows have no web address in them. If your CSV came from a spreadsheet with clickable links, "
            "the export may have kept only the link text - export again with the full addresses showing.")
    return links


# ---------- what to collect ----------
def load_fields():
    try:
        with open(FIELDS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except ValueError as e:
        sys.exit(f"fields.json isn't valid JSON ({e}). Ask the other chat to fix it, then Run again.")
    raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    if not isinstance(raw, dict) or not raw:
        sys.exit('fields.json should look like {"fields": {"name": "css selector", ...}}. See HOW-TO-fields.md.')
    fields = {}
    for name, spec in raw.items():
        if isinstance(spec, str):
            spec = {"xpath": spec} if spec.lstrip("(").startswith("/") else {"css": spec}
        if not isinstance(spec, dict) or not (spec.get("css") or spec.get("xpath")):
            sys.exit(f'fields.json: "{name}" needs a "css" or "xpath" selector. See HOW-TO-fields.md.')
        fields[str(name)] = spec
    return fields


def extract(page, fields):
    """{name: text} and how many were found. Never printed."""
    out, found = {}, 0
    for name, spec in fields.items():
        try:
            nodes = page.xpath(spec["xpath"]) if spec.get("xpath") else page.css(spec["css"])
        except Exception:
            nodes = []
        values = []
        for n in nodes:
            v = n.get_all_text(separator=" ", strip=True) if hasattr(n, "get_all_text") else str(n)
            v = " ".join(str(v).split())
            if v:
                values.append(v)
        if spec.get("regex") and values:
            matched = []
            for v in values:
                m = re.search(spec["regex"], v)
                if m:
                    matched.append(m.group(1) if m.groups() else m.group(0))
            values = matched
        if not spec.get("all"):
            values = values[:1]
        value = " | ".join(values)[:MAX_FIELD_CHARS]
        out[name] = value
        found += bool(value)
    return out, found


HOWTO = """# How to write fields.json

You're the chat that's allowed to see these pages. The pages in `samples/` were opened through a proxy
by `8.py`, which runs on this Mac. Read a few of them, find where each piece of text the owner wants sits
in the page's HTML, and write `fields.json` in this folder. `8.py` then pulls exactly those pieces out of
every page and saves them to `results.csv` here.

Each sample is saved twice: `row-N.html` (the page's HTML, which is what the selectors run on) and
`row-N.txt` (just its text, which is easier to read).

## Format

```json
{
  "fields": {
    "title": "h1",
    "price": ".product-price",
    "description": {"css": "#description", "all": true},
    "image_alt": "img.main::attr(alt)",
    "sku": {"css": ".details li", "all": true, "regex": "SKU[:\\\\s]*(\\\\S+)"},
    "seller": {"xpath": "//div[@class='seller']//a/text()"}
  }
}
```

- The key is the column name in `results.csv`, and the value is where to find it.
- A plain string is a **CSS selector**; it takes the text of the first match. `::text` and `::attr(name)`
  work too, like `meta[property='og:title']::attr(content)`.
- An object can have `"css"` or `"xpath"`, plus:
  - `"all": true` keeps every match, joined with " | ";
  - `"regex": "..."` keeps only matches of the pattern, or of its first ( ) group.
- Prefer selectors that hold across all pages: ids, stable class names, `meta` tags or structured data,
  not position-based ones like `div:nth-child(7)`.
- Some sites draw parts of the page with JavaScript, and the samples show the finished page. `8.py`
  handles this: if a field is missing from the plain page, it reopens that page in the browser.

After saving `fields.json`, the owner runs `8.py` again and it collects from every link. Check `results.csv`
after the first batch, and fix `fields.json` if a column comes out empty or wrong. To redo links that are
already finished (for example after changing `fields.json`), delete `8-results.db` and run `8.py` again;
`results.csv` is rebuilt.
"""


# ---------- results ----------
def open_db():
    os.makedirs(FOLDER, exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.execute("""CREATE TABLE IF NOT EXISTS results (
        key TEXT PRIMARY KEY, row INTEGER, link TEXT, status TEXT, how TEXT, found INTEGER, wanted INTEGER,
        data TEXT, tries INTEGER DEFAULT 0, at TEXT)""")
    db.commit()
    return db


def key_of(link):
    return hashlib.sha1(link.encode("utf-8")).hexdigest()


def save_result(db, row, link, status, how="", values=None, found=0, wanted=0):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("""INSERT INTO results(key, row, link, status, how, found, wanted, data, tries, at)
                  VALUES (?,?,?,?,?,?,?,?,1,?)
                  ON CONFLICT(key) DO UPDATE SET row=excluded.row, status=excluded.status, how=excluded.how,
                  found=excluded.found, wanted=excluded.wanted, data=excluded.data,
                  tries=results.tries+1, at=excluded.at""",
               (key_of(link), row, link, status, how, found, wanted, json.dumps(values or {}, ensure_ascii=False), now))
    db.commit()


def export_csv(db, fields):
    names = list(fields)
    tmp = RESULTS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["row", "link", "status"] + names + ["found", "how", "tries", "time"])
        for row, link, status, how, found, wanted, data, tries, at in db.execute(
                "SELECT row, link, status, how, found, wanted, data, tries, at FROM results ORDER BY row"):
            values = json.loads(data or "{}")
            w.writerow([row, link, status] + [values.get(n, "") for n in names]
                       + [f"{found} of {wanted}" if wanted else "", how, tries, at])
    os.replace(tmp, RESULTS_CSV)


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
    if status in (404, 410):
        return "not_found"
    if status is not None and status >= 500:
        return "error"
    return None


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
        self.resting = {}

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
        self.passed = set()

    def proxy_for_browser(self):
        if not USE_PROXY:
            return None
        return {"server": f"http://{PROXY_HOST}:{self.port}", "username": PROXY_USER, "password": self.job.password}

    def proxy_for_text(self):
        if not USE_PROXY:
            return None
        return f"http://{quote(PROXY_USER, safe='')}:{quote(self.job.password, safe='')}@{PROXY_HOST}:{self.port}"

    async def ip_timezone(self):
        """Where this port's IP is, so the browser's clock can match it."""
        from scrapling.fetchers import FetcherSession

        try:
            async with FetcherSession(retries=1, timeout=20, proxy=self.proxy_for_text()) as s:
                r = await s.get(IP_CHECK_URL)
            info = json.loads((r.body or b"{}").decode("utf-8", "ignore"))
            return (info.get("city") or {}).get("time_zone") or info.get("time_zone") or info.get("timezone")
        except Exception:
            return None

    async def open(self, new_ip=False):
        from scrapling.fetchers import AsyncStealthySession

        await self.close()
        if USE_PROXY and (new_ip or self.port is None):
            self.job.ports.give_back(self.port, blocked=new_ip)
            self.port = await self.job.ports.take()
        self.passed.clear()
        options = dict(headless=HEADLESS, real_chrome=REAL_CHROME, solve_cloudflare=True, hide_canvas=True,
                       block_webrtc=True, timeout=60000, retries=2)
        if USE_PROXY:
            options["proxy"] = self.proxy_for_browser()
            if MATCH_TIMEZONE:
                tz = await self.ip_timezone()
                if tz:
                    options["timezone_id"] = tz
                    say(f"Browser {self.n}: port {self.port}, clock set to {tz}.")
                else:
                    say(f"Browser {self.n}: port {self.port} (couldn't look up its time zone - using your Mac's).")
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

    async def via_browser(self, row, url, site, light=None):
        moves, second_look, reopened = 0, False, False
        while True:
            use_light = (site in self.passed) if light is None else light
            try:
                kind, page = await self.browser_fetch(url, use_light)
            except Exception as e:
                kind, page = "error", None
                say(f"Row {row}: browser error ({short(e)}).")
            if kind is None or kind == "not_found":
                self.passed.add(site)
                return kind, "browser", page
            if kind == "error":
                if not reopened:
                    reopened = True
                    await self.open()
                    continue
                return "error", "browser", page
            if kind == "challenge" and not second_look:
                second_look = True
                await asyncio.sleep(random.uniform(4, 8))
                continue
            if USE_PROXY and moves < 2:
                moves, second_look = moves + 1, False
                old = self.port
                await self.open(new_ip=True)
                say(f"Row {row}: blocked ({kind.replace('_', ' ')}) on port {old} - Browser {self.n} moved to port {self.port}.")
                if kind == "rate_limit":
                    await asyncio.sleep(random.uniform(20, 40))
                continue
            return kind, "browser", page

    async def lookup(self, row, url, fields=None):
        """Returns (status, how, page, values, found). status None = ok."""
        job, site = self.job, site_of(url)
        if self.browser is None:
            await self.open()

        if fields is None:  # sample run: always the finished page from the browser
            kind, how, page = await self.via_browser(row, url, site)
            return kind, how, page, None, 0

        if TEXT_ONLY and site in self.passed and site not in job.browser_only and self.ua:
            try:
                kind, page = await self.text_fetch(url)
            except Exception as e:
                kind, page = "error", None
                say(f"Row {row}: plain fetch failed ({short(e)}) - using the browser.")
            if kind == "not_found":
                return kind, "text", page, {}, 0
            if kind is None:
                values, found = extract(page, fields)
                if found == len(fields) or site in job.plain_is_enough:
                    job.text_worked.add(site)
                    return None, "text", page, values, found
                # Something's missing from the plain page - maybe it's drawn by JavaScript. Check in the browser.
                kind2, how2, page2 = await self.via_browser(row, url, site)
                if kind2 is None:
                    values2, found2 = extract(page2, fields)
                    if found2 > found:
                        job.js_needed[site] = job.js_needed.get(site, 0) + 1
                        if job.js_needed[site] >= 3 and site not in job.browser_only:
                            job.browser_only.add(site)
                            say("Some items only appear once the page's JavaScript runs - "
                                "using the browser for this site (skipping images and fonts).")
                        return None, "browser", page2, values2, found2
                    job.same_in_both[site] = job.same_in_both.get(site, 0) + 1
                    if job.same_in_both[site] >= 3:
                        job.plain_is_enough.add(site)  # missing items are just missing; stop double-checking
                    return None, "text", page, values, found
                return kind2, how2, page2, {}, 0
            if kind in BLOCK_KINDS:
                self.passed.discard(site)
                if site not in job.text_worked:
                    job.text_misses[site] = job.text_misses.get(site, 0) + 1
                    if job.text_misses[site] >= 3:
                        job.browser_only.add(site)
                        say("Plain-text fetching doesn't get past this site's protection - "
                            "using the browser for it (skipping images and fonts).")

        kind, how, page = await self.via_browser(row, url, site)
        if kind is None:
            values, found = extract(page, fields)
            return None, how, page, values, found
        return kind, how, page, {}, 0


class Job:
    def __init__(self, password):
        self.password = password
        self.ports = Ports(PROXY_PORTS) if USE_PROXY else None
        self.browser_only, self.text_worked, self.plain_is_enough = set(), set(), set()
        self.text_misses, self.js_needed, self.same_in_both = {}, {}, {}
        self.blocks_in_a_row = 0
        self.pause_until = 0
        self.done = 0
        self.stats = {"ok": 0, "blocked": 0, "error": 0, "not_found": 0, "text": 0, "browser": 0, "found": 0, "wanted": 0}
        self.started = time.time()


async def worker_loop(worker, queue, job, db, fields, limit):
    while True:
        if limit and job.done >= limit:
            return
        try:
            row, url = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        if time.time() < job.pause_until:
            await asyncio.sleep(job.pause_until - time.time())
        job.done += 1
        try:
            status, how, page, values, found = await worker.lookup(row, url, fields)
        except Exception as e:
            status, how, page, values, found = "error", "", None, {}, 0
            say(f"Row {row}: {short(e)}")

        if fields is None:  # sample run
            if status is None and page is not None:
                os.makedirs(SAMPLES, exist_ok=True)
                with open(os.path.join(SAMPLES, f"row-{row}.html"), "wb") as f:
                    f.write(page.body or b"")
                with open(os.path.join(SAMPLES, f"row-{row}.txt"), "w", encoding="utf-8") as f:
                    f.write(re.sub(r"\n{3,}", "\n\n", str(page.get_all_text(separator="\n", strip=True))))
                job.stats["ok"] += 1
                say(f"Row {row}: saved as a sample.")
            else:
                job.stats["error" if status in (None, "error") else ("not_found" if status == "not_found" else "blocked")] += 1
                say(f"Row {row}: {str(status).replace('_', ' ')} - not saved.")
        elif status is None:
            save_result(db, row, url, "ok", how, values, found, len(fields))
            job.stats["ok"] += 1
            job.stats[how] += 1
            job.stats["found"] += found
            job.stats["wanted"] += len(fields)
            job.blocks_in_a_row = 0
            say(f"Row {row}: ok - {found} of {len(fields)} items ({'plain text' if how == 'text' else 'browser'}).")
        else:
            save_result(db, row, url, status, how, {}, 0, len(fields))
            bucket = "blocked" if status in BLOCK_KINDS else ("not_found" if status == "not_found" else "error")
            job.stats[bucket] += 1
            say(f"Row {row}: {status.replace('_', ' ')}.")
            if bucket == "blocked":
                job.blocks_in_a_row += 1
                if job.blocks_in_a_row >= BLOCKS_BEFORE_PAUSE and time.time() >= job.pause_until:
                    job.pause_until = time.time() + PAUSE_ALL_MINUTES * 60
                    job.blocks_in_a_row = 0
                    say(f"{BLOCKS_BEFORE_PAUSE} blocked lookups in a row - pausing everything for {PAUSE_ALL_MINUTES} min.")
        if fields is not None and job.done % 25 == 0:
            export_csv(db, fields)
            report(job, fields)
        await asyncio.sleep(random.uniform(*PAUSE_SECONDS))


def report(job, fields):
    s = job.stats
    mins = (time.time() - job.started) / 60
    if fields is None:
        say(f"Samples: {s['ok']} saved, {s['blocked']} blocked, {s['error']} errors, {s['not_found']} not found - {mins:.0f} min.")
        return
    filled = f", {100 * s['found'] / s['wanted']:.0f}% of items found" if s["wanted"] else ""
    say(f"So far: {s['ok']} ok ({s['text']} plain text, {s['browser']} browser){filled}, {s['blocked']} blocked, "
        f"{s['error']} errors, {s['not_found']} not found - {mins:.0f} min. Decodo's Usage statistics shows data used.")


async def one_round(password, db, fields):
    links = read_links()
    if fields is None:
        todo = links[:SAMPLE_PAGES]
        say(f"Sample run: opening the first {len(todo)} links and saving them to {SAMPLES}.")
        limit = len(todo)
    else:
        finished = {k for (k,) in db.execute(
            "SELECT key FROM results WHERE status IN ('ok','not_found') OR tries >= ?", (MAX_TRIES,))}
        todo = [(r, u) for r, u in links if key_of(u) not in finished]
        say(f"{len(links) - len(todo):,} links already done, {len(todo):,} to go"
            + (f" - this run does up to {LIMIT}." if LIMIT else "."))
        limit = LIMIT
    if not todo:
        if fields is not None:
            export_csv(db, fields)
        return 0

    queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    job = Job(password)
    count = max(1, min(BROWSERS, len(todo), len(job.ports.free) if USE_PROXY else BROWSERS))
    workers = [Worker(i + 1, job) for i in range(count)]
    say(f"Starting {count} browser(s)" + (" through Decodo." if USE_PROXY else " on your home internet."))
    try:
        await asyncio.gather(*(worker_loop(w, queue, job, db, fields, limit) for w in workers))
    finally:
        for w in workers:
            await w.close()
        if fields is not None:
            export_csv(db, fields)
        report(job, fields)
    if fields is None:
        with open(HOWTO_FILE, "w", encoding="utf-8") as f:
            f.write(HOWTO)
        say(f"Done. Next: give the other chat the folder {FOLDER} and ask it to read HOW-TO-fields.md and the "
            "pages in samples, then write fields.json with the items you want. Then Run 8.py again.")
    else:
        say(f"Saved: {RESULTS_CSV}")
    return job.done


async def run():
    password = ""
    if USE_PROXY:
        pw_path = os.path.expanduser(PROXY_PASSWORD_FILE)
        try:
            password = open(pw_path, encoding="utf-8").read().strip()
        except OSError:
            sys.exit(f"Can't read the proxy password file at {pw_path}\n"
                     "Make a text file there that holds only your Decodo proxy password.")
        if not password:
            sys.exit(f"The password file at {pw_path} is empty.")
        SECRETS.append(password)
    if not os.path.isdir(FOLDER):
        sys.exit(f"Can't find the folder {FOLDER}\nMake it, put your links.csv inside, then Run again.")

    db = open_db()
    fields = load_fields() if os.path.exists(FIELDS_FILE) else None
    if fields is None:
        await one_round(password, db, None)
        return
    say(f"Collecting {len(fields)} items per page (names not shown, since they can hint at the site).")
    while True:
        done = await one_round(password, db, fields)
        if not WATCH_MINUTES:
            if not done:
                say(f"Nothing left to do. Results: {RESULTS_CSV}")
            return
        say(f"Checking {LINKS_FILE} for new links again in {WATCH_MINUTES} min (stop any time).")
        await asyncio.sleep(WATCH_MINUTES * 60)


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
