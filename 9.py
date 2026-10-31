"""
9.py - the automatic link queue. It runs on GitHub's computers (10.yml starts it in your results
repo), so it keeps working with your laptop off.

  Queue 1, OPEN: opens each new link in your links file through your Decodo proxy and saves the
                 page. The saved pages are handed to queue 2.
  Queue 2, READ: pulls the items listed in fields.json out of each saved page and writes them to
                 results.csv, one row per link, then saves results.csv to the repo.

Files in the results repo:
  <your links>.csv   your links. Add more any time; a run starts by itself.
  fields.json        what to collect from each page (the other chat writes it)
  results.csv        one row per link (queue 2 writes it)
  samples/           the first pages, saved complete, plus HOW-TO-fields.md for the other chat
  check/             a few recent pages where some items came out empty, for fixing fields.json

The saved pages are kept with each run on GitHub (not in the repo) for 30 days, so queue 2 can
read them again when fields.json changes without reopening any links.

Until fields.json exists, a run only opens the first SAMPLE_PAGES links and saves them in samples/.

The log only ever shows row numbers and counts - never links, page text or what was collected.
Settings come from the env: section of 10.yml; the defaults are below.

By hand:  python 9.py plan | open | read
"""

import asyncio
import csv
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote, urlparse


def env(name, default):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_bool(name, default):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(float(env(name, str(default))))
    except ValueError:
        return default


# ---------------- settings (set them in 10.yml) ----------------
LINKS_FILE = env("LINKS_FILE", "")  # "" = the CSV in the repo's top folder that isn't results.csv
LINK_COLUMN = env("LINK_COLUMN", "0")  # 0 = the first web address in each row; or a column number or heading
SAMPLE_PAGES = env_int("SAMPLE_PAGES", 20)  # pages saved for the other chat before fields.json exists
LIMIT = env_int("LIMIT", 50)  # most links one run opens (0 = no limit)
RUN_MINUTES = env_int("RUN_MINUTES", 300)  # stop opening links after this long (GitHub stops a job at 6 hours)
BROWSERS = env_int("BROWSERS", 2)  # browsers at once, each on its own proxy IP

USE_PROXY = env_bool("USE_PROXY", True)
PROXY_HOST = env("PROXY_HOST", "us.decodo.com")
PROXY_PORTS = env("PROXY_PORTS", "10001-10010")
PROXY_USER = env("PROXY_USER", "user-sp2e2o4qkx-sessionduration-30")
PASSWORD = os.environ.get("DECODO_PASSWORD", "").strip()  # a GitHub secret; never printed

TEXT_ONLY = env_bool("TEXT_ONLY", True)  # after the Cloudflare pass, fetch pages without the browser
HEADLESS = env_bool("HEADLESS", False)  # 10.yml runs a hidden screen, so a "visible" browser works there
REAL_CHROME = env_bool("REAL_CHROME", True)  # GitHub's computers have Google Chrome
MATCH_TIMEZONE = env_bool("MATCH_TIMEZONE", True)  # browser clock = the proxy IP's time zone
PAUSE_SECONDS = tuple(float(x) for x in (env("PAUSE_SECONDS", "2-5").split("-") + ["5"])[:2])
MAX_FIELD_CHARS = env_int("MAX_FIELD_CHARS", 2000)  # longest text kept per item (keeps results.csv small)
MAX_TRIES = env_int("MAX_TRIES", 3)  # a link that fails this many times is left alone
REREAD = env_bool("REREAD", False)  # read every saved page again (10.yml sets this when fields.json changes)
RETRY_FAILED = env_bool("RETRY_FAILED", False)  # try links that already failed MAX_TRIES times again
REOPEN_MISSING = env_bool("REOPEN_MISSING", False)  # open pages again that gave fewer items than fields.json asks for
IP_CHECK_URL = env("IP_CHECK_URL", "https://ip.decodo.com/json")
# ---------------------------------------------------------------

CHECK_PAGES = 20  # pages with missing items kept in check/
RESULTS_MAX_MB = float(env("RESULTS_MAX_MB", "95"))  # GitHub refuses files over 100 MB; past this, results-2.csv
PORT_COOLDOWN_SECONDS = 600
BLOCKS_BEFORE_PAUSE = 10
PAUSE_ALL_MINUTES = 15
PAUSES_BEFORE_STOP = 3  # this many pauses in one run -> stop; the next run tries again later
ERRORS_BEFORE_STOP = 6  # this many errors in a row (not blocks) -> stop the run

REPO = os.path.abspath(env("REPO_DIR", "repo"))  # the results repo, checked out
PAGES = os.path.abspath(env("PAGES_DIR", "pages"))  # queue 1 saves pages here (uploaded as this run's bundle)
INBOX = os.path.abspath(env("INBOX_DIR", "inbox"))  # queue 2 finds bundles here, one folder per run
RUN_ID = env_int("GITHUB_RUN_ID", int(time.time()))
GH_REPO = env("GITHUB_REPOSITORY", "")

FIELDS_FILE = os.path.join(REPO, "fields.json")
SAMPLES = os.path.join(REPO, "samples")
SAMPLE_INDEX = os.path.join(SAMPLES, "index.csv")
HOWTO_FILE = os.path.join(SAMPLES, "HOW-TO-fields.md")
CHECK = os.path.join(REPO, "check")
STAGE = os.path.join(INBOX, "_check")  # pages picked for check/, copied aside before bundles are cleaned up

CHALLENGE_TITLES = ("just a moment", "verify you are human", "attention required", "checking your browser")
BLOCK_KINDS = {"challenge", "firewall", "fingerprint", "ip_ban", "rate_limit"}
SECRETS = [PASSWORD] if PASSWORD else []
URL_RE = re.compile(r"""\b(?:https?|wss?)://[^\s"'<>)]+""", re.I)
HOSTS_RE = None  # the hostnames in the links file, hidden from all output (some network errors name them)
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


# ---------- privacy ----------
def mask(text):
    text = URL_RE.sub("[link]", str(text))
    if HOSTS_RE is not None:
        text = HOSTS_RE.sub("[the site]", text)
    for s in SECRETS:
        text = text.replace(s, "[password]").replace(quote(s, safe=""), "[password]")
    return text


def say(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {mask(msg)}", flush=True)


class MaskLogs(logging.Filter):
    def filter(self, record):
        record.msg, record.args = mask(record.getMessage()), None
        return True


def short(e):
    return mask(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")[:180]


def fail(msg):
    say(msg)
    summary([f"**Stopped:** {msg}"])
    sys.exit(1)


def output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={mask(v)}\n")


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(mask(l) for l in lines) + "\n\n")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def status_name(kind):
    return {"not_found": "not found", "ip_ban": "ip ban", "rate_limit": "rate limit"}.get(kind, kind or "error")


# ---------- the links file ----------
URL_IN_CELL = re.compile(r"""(?:https?://|www\.)[^\s"'<>)]+""", re.I)
RESULT_NAME = re.compile(r"results(?:-(\d+))?\.csv", re.I)


def cell_link(text):
    m = URL_IN_CELL.search(text or "")
    if not m:
        return ""
    link = m.group(0).rstrip(".,;")
    return link if link.lower().startswith("http") else "https://" + link


def links_path():
    if LINKS_FILE:
        path = os.path.join(REPO, LINKS_FILE)
        if not os.path.isfile(path):
            fail("LINKS_FILE in 10.yml names a file that isn't in the repo's top folder.")
        return path
    names = sorted(n for n in os.listdir(REPO) if n.lower().endswith(".csv") and not RESULT_NAME.fullmatch(n))
    if not names:
        return None
    if len(names) > 1:
        pick = next((n for n in names if n.lower() == "links.csv"), names[0])
        say(f"There are {len(names)} CSV files besides results.csv; using "
            f"{'links.csv' if pick.lower() == 'links.csv' else 'the first one by name'}. "
            "Set LINKS_FILE in 10.yml to choose.")
        return os.path.join(REPO, pick)
    return os.path.join(REPO, names[0])


def hide_hosts(links):
    global HOSTS_RE
    hosts = set()
    for _, link in links:
        h = (urlparse(link).hostname or "").lower()
        if h:
            hosts.add(h)
            hosts.add(h[4:] if h.startswith("www.") else h)
    if hosts:
        parts = sorted((re.escape(h) for h in hosts), key=len, reverse=True)
        HOSTS_RE = re.compile(r"(?<![\w.-])(?:" + "|".join(parts) + r")(?![\w-])", re.I)


def read_links(quiet=False):
    """[(row, link)] in file order, one per row that has a link. Row 1 is the file's first line."""
    path = links_path()
    if path is None:
        return None
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    column = None
    if LINK_COLUMN.isdigit() and int(LINK_COLUMN) > 0:
        column = int(LINK_COLUMN) - 1
    elif not LINK_COLUMN.isdigit() and rows:
        heads = [h.strip().lower() for h in rows[0]]
        if LINK_COLUMN.lower() not in heads:
            fail("LINK_COLUMN in 10.yml names a column heading that isn't in the links file.")
        column = heads.index(LINK_COLUMN.lower())
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
    hide_hosts(links)
    if not quiet and empty > max(5, len(links)):
        say("Most rows of the links file have no web address in them. If it came from a spreadsheet with "
            "clickable links, the export may have kept only the link text - export again with the addresses showing.")
    return links


# ---------- what to collect ----------
def load_fields():
    if not os.path.exists(FIELDS_FILE):
        return None
    try:
        with open(FIELDS_FILE, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except ValueError as e:
        fail(f"fields.json isn't valid JSON ({str(e)[:80]}). Ask the other chat to fix it.")
    raw = raw.get("fields", raw) if isinstance(raw, dict) else raw
    if not isinstance(raw, dict) or not raw:
        fail('fields.json should look like {"fields": {"name": "css selector", ...}}. See samples/HOW-TO-fields.md.')
    fields = {}
    for name, spec in raw.items():
        if isinstance(spec, str):
            spec = {"xpath": spec} if spec.lstrip("(").startswith("/") else {"css": spec}
        if not isinstance(spec, dict) or not (spec.get("css") or spec.get("xpath")):
            fail(f'fields.json: item number {list(raw).index(name) + 1} needs a "css" or "xpath" selector.')
        if str(name).lower() in ("row", "link", "status", "found", "how", "tries", "time", "run"):
            fail(f'fields.json: item number {list(raw).index(name) + 1} uses a name results.csv already has; rename it.')
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
                try:
                    m = re.search(spec["regex"], v)
                except re.error:
                    m = None
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

You're the chat that's allowed to see these pages. The pages in this `samples/` folder were opened
through a proxy by the link queue, which runs on GitHub. Read a few of them, find where each piece of text
the owner wants sits in the page's HTML, and write `fields.json`. It goes in the **top folder** of this
repo, next to the links file. As soon as `fields.json` is there, the queue starts by itself: it pulls
exactly those pieces out of every page and saves them in `results.csv`, one row per link.

Each sample is saved twice: `row-N.html` (the page's HTML, which the selectors run on) and `row-N.txt`
(just its text, which is easier to read). `index.csv` says which link each one came from.

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
- Each item is cut at {chars} characters (MAX_FIELD_CHARS in the workflow file), so `results.csv` stays
  under GitHub's 100 MB limit.
- Some sites draw parts of the page with JavaScript, and the samples show the finished page. The queue
  handles this: if an item is missing from the plain page, it reopens that page in the browser.

## Checking and fixing

- The `found` column in `results.csv` says how many items came out of each page, like `4 of 5`.
- `check/` holds up to {check} recent pages where some items came out empty. Look at those to fix a selector.
- When `fields.json` changes, the queue reads every saved page again (pages are kept for 30 days), so
  `results.csv` is redone without reopening any links.
- A page saved before an item was added can only give that item if it's in the saved copy. If a new item
  is drawn by JavaScript, those rows stay short: on the repo's Actions tab, open "Link queue", press
  "Run workflow" and tick "Open pages again that came out with missing items".
"""


# ---------- results.csv ----------
BASE = ["row", "link", "status"]
TAIL = ["found", "how", "tries", "time", "run"]


def result_parts():
    parts = []
    for n in os.listdir(REPO):
        m = RESULT_NAME.fullmatch(n)
        if m:
            parts.append((int(m.group(1) or 1), n))
    return [os.path.join(REPO, n) for _, n in sorted(parts)]


def load_results():
    results = {}
    for path in result_parts():
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            for r in csv.DictReader(f):
                link = (r.get("link") or "").strip()
                if not link:
                    continue
                values = {k: v or "" for k, v in r.items() if k is not None and k not in BASE + TAIL}
                results[link] = dict(row=to_int(r.get("row")), link=link, status=(r.get("status") or "").strip(),
                                     values=values, found=r.get("found") or "", how=r.get("how") or "",
                                     tries=to_int(r.get("tries")), time=r.get("time") or "", run=to_int(r.get("run")))
    return results


def save_results(results, names):
    def line(values):
        s = io.StringIO()
        csv.writer(s).writerow(values)
        return s.getvalue()

    head = line(BASE + names + TAIL)
    limit = RESULTS_MAX_MB * 1024 * 1024
    parts, cur, size = [], [head], len(head.encode("utf-8")) + 3
    for r in sorted(results.values(), key=lambda r: (r["row"] or 0, r["link"])):
        l = line([r["row"], r["link"], r["status"]] + [r["values"].get(n, "") for n in names]
                 + [r["found"], r["how"], r["tries"], r["time"], r["run"]])
        b = len(l.encode("utf-8"))
        if size + b > limit and len(cur) > 1:
            parts.append(cur)
            cur, size = [head], len(head.encode("utf-8")) + 3
        cur.append(l)
        size += b
    parts.append(cur)
    old, written = set(result_parts()), []
    for i, lines in enumerate(parts, 1):
        path = os.path.join(REPO, "results.csv" if i == 1 else f"results-{i}.csv")
        with open(path + ".tmp", "w", encoding="utf-8-sig", newline="") as f:
            f.write("".join(lines))
        os.replace(path + ".tmp", path)
        written.append(path)
    for p in old - set(written):
        os.remove(p)
    if len(parts) > 1:
        say(f"results.csv would pass GitHub's 100 MB limit, so the results are split over {len(parts)} files "
            "(results.csv, results-2.csv, ...). A smaller MAX_FIELD_CHARS keeps them in one.")


def short_of_items(r):
    m = re.fullmatch(r"(\d+) of (\d+)", r.get("found") or "")
    return bool(m) and int(m.group(1)) < int(m.group(2))


def is_done(r):
    if r is None:
        return False
    if r["status"] == "ok" and REOPEN_MISSING and short_of_items(r):
        return False
    if r["status"] in ("ok", "not found"):
        return True
    return (not RETRY_FAILED) and r["tries"] >= MAX_TRIES


def sample_index():
    """{link: (file, row, run, time)} for the saved samples."""
    out = {}
    if os.path.exists(SAMPLE_INDEX):
        with open(SAMPLE_INDEX, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("link") and r.get("file") and os.path.exists(os.path.join(SAMPLES, r["file"])):
                    out[r["link"]] = (r["file"], to_int(r.get("row")), to_int(r.get("run")), r.get("time") or "")
    return out


# ---------- what's left to do ----------
def plan(quiet=False):
    """(mode, todo, links, results, fields). mode is 'samples', 'full' or 'none'."""
    links = read_links(quiet)
    fields = load_fields()
    if links is None:
        if not quiet:
            say("There's no links CSV in the repo's top folder yet. Upload it there and a run starts by itself.")
        return "none", [], [], {}, fields
    results = load_results()
    if fields is None:
        if sample_index():
            if not quiet:
                say(f"{len(links):,} links. The samples are saved; waiting for fields.json "
                    "(see samples/HOW-TO-fields.md).")
            return "none", [], links, results, None
        todo = links[:SAMPLE_PAGES]
        if not quiet:
            say(f"{len(links):,} links. No fields.json yet, so this run opens the first {len(todo)} "
                "and saves them in samples/ for the other chat.")
        return ("samples" if todo else "none"), todo, links, results, None
    samples = sample_index()
    todo = [(r, u) for r, u in links if not is_done(results.get(u)) and not (u in samples and u not in results)]
    if not quiet:
        say(f"{len(links):,} links: {len(links) - len(todo):,} done, {len(todo):,} to go"
            + (f". This run opens up to {min(LIMIT, len(todo)):,}." if LIMIT and todo else "."))
    return ("full" if todo else "none"), todo, links, results, fields


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


class Ports:
    def __init__(self, spec):
        ports = []
        for part in str(spec).replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-")
                ports += list(range(int(a), int(b) + 1))
            elif part:
                ports.append(int(part))
        random.shuffle(ports)  # runs start on different IPs
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


class Worker:
    """One browser and its plain-text twin, on one proxy port (so one IP)."""

    def __init__(self, n, job):
        self.n, self.job = n, job
        self.port = None
        self.browser = None
        self.ua, self.lang = None, None
        self.passed = set()

    def proxy_for_browser(self):
        if not USE_PROXY:
            return None
        return {"server": f"http://{PROXY_HOST}:{self.port}", "username": PROXY_USER, "password": PASSWORD}

    def proxy_for_text(self):
        if not USE_PROXY:
            return None
        return f"http://{quote(PROXY_USER, safe='')}:{quote(PASSWORD, safe='')}@{PROXY_HOST}:{self.port}"

    async def ip_timezone(self):
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
            say(f"Browser {self.n}: " + (f"port {self.port}, " if USE_PROXY else "")
                + (f"clock set to {tz}." if tz else "couldn't look up its time zone, using the default."))
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
                say(f"Row {row}: blocked ({status_name(kind)}) on port {old} - Browser {self.n} moved to port {self.port}.")
                if kind == "rate_limit":
                    await asyncio.sleep(random.uniform(20, 40))
                continue
            return kind, "browser", page

    async def lookup(self, row, url, fields=None):
        """(status, how, page). status None = opened fine. fields only decide plain text vs browser."""
        job, site = self.job, site_of(url)
        if self.browser is None:
            await self.open()

        if fields is None:  # samples: always the finished page from the browser
            return await self.via_browser(row, url, site)

        if TEXT_ONLY and site in self.passed and site not in job.browser_only and self.ua:
            try:
                kind, page = await self.text_fetch(url)
            except Exception as e:
                kind, page = "error", None
                say(f"Row {row}: plain fetch failed ({short(e)}) - using the browser.")
            if kind == "not_found":
                return kind, "text", page
            if kind is None:
                _, found = extract(page, fields)
                if found == len(fields) or site in job.plain_is_enough:
                    job.text_worked.add(site)
                    return None, "text", page
                # Something's missing from the plain page - maybe it's drawn by JavaScript. Check in the browser.
                kind2, how2, page2 = await self.via_browser(row, url, site)
                if kind2 is None:
                    _, found2 = extract(page2, fields)
                    if found2 > found:
                        job.js_needed[site] = job.js_needed.get(site, 0) + 1
                        if job.js_needed[site] >= 3 and site not in job.browser_only:
                            job.browser_only.add(site)
                            say("Some items only appear once the page's JavaScript runs - "
                                "using the browser for this site (skipping images and fonts).")
                        return None, "browser", page2
                    job.same_in_both[site] = job.same_in_both.get(site, 0) + 1
                    if job.same_in_both[site] >= 3:
                        job.plain_is_enough.add(site)  # missing items are just missing; stop double-checking
                    return None, "text", page
                return kind2, how2, page2
            if kind in BLOCK_KINDS:
                self.passed.discard(site)
                if site not in job.text_worked:
                    job.text_misses[site] = job.text_misses.get(site, 0) + 1
                    if job.text_misses[site] >= 3:
                        job.browser_only.add(site)
                        say("Plain-text fetching doesn't get past this site's protection - "
                            "using the browser for it (skipping images and fonts).")

        return await self.via_browser(row, url, site)


class Job:
    def __init__(self, mode):
        self.mode = mode
        self.ports = Ports(PROXY_PORTS) if USE_PROXY else None
        self.browser_only, self.text_worked, self.plain_is_enough = set(), set(), set()
        self.text_misses, self.js_needed, self.same_in_both = {}, {}, {}
        self.blocks_in_a_row, self.pauses, self.errors_in_a_row = 0, 0, 0
        self.pause_until = 0
        self.started = time.time()
        self.deadline = self.started + RUN_MINUTES * 60
        self.taken = 0
        self.stop = False
        self.out_of_time = False
        self.stats = {"opened": 0, "text": 0, "browser": 0, "blocked": 0, "error": 0, "not found": 0}


class Bundle:
    """The saved pages of this run (uploaded by 10.yml and handed to queue 2), plus a list of every try."""

    def __init__(self, mode):
        self.mode = mode
        self.dir = os.path.join(PAGES, "samples") if mode == "samples" else PAGES
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(PAGES, "mode.json"), "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "run": RUN_ID}, f)
        self.list = open(os.path.join(PAGES, "manifest.jsonl"), "a", encoding="utf-8")

    def save(self, row, link, status, how, page):
        entry = {"row": row, "link": link, "status": "opened" if status is None else status_name(status),
                 "how": how, "at": now_utc()}
        if status is None and page is not None:
            name = f"row-{row}.html"
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(page.body or b"")
            entry["file"] = ("samples/" if self.mode == "samples" else "") + name
            entry["enc"] = getattr(page, "encoding", None) or "utf-8"
            if self.mode == "samples":
                with open(os.path.join(self.dir, f"row-{row}.txt"), "w", encoding="utf-8") as f:
                    f.write(re.sub(r"\n{3,}", "\n\n", str(page.get_all_text(separator="\n", strip=True))))
        self.list.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.list.flush()

    def close(self):
        self.list.close()


async def worker_loop(worker, queue, job, fields, bundle):
    while not job.stop:
        if job.mode == "full" and LIMIT and job.taken >= LIMIT:
            return
        if time.time() > job.deadline:
            job.out_of_time = True
            return
        try:
            row, url = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        if time.time() < job.pause_until:
            await asyncio.sleep(job.pause_until - time.time())
        job.taken += 1
        try:
            status, how, page = await worker.lookup(row, url, fields)
        except Exception as e:
            status, how, page = "error", "", None
            say(f"Row {row}: {short(e)}")
        try:
            bundle.save(row, url, status, how, page)
        except Exception as e:
            status = "error"
            say(f"Row {row}: couldn't save the page ({short(e)}).")

        if status is None:
            job.stats["opened"] += 1
            job.stats[how] = job.stats.get(how, 0) + 1
            job.blocks_in_a_row = job.errors_in_a_row = 0
            say(f"Row {row}: opened ({'plain text' if how == 'text' else 'browser'}).")
        else:
            bucket = "blocked" if status in BLOCK_KINDS else ("not found" if status == "not_found" else "error")
            job.stats[bucket] += 1
            say(f"Row {row}: {status_name(status)}.")
            job.errors_in_a_row = job.errors_in_a_row + 1 if bucket == "error" else 0
            if job.errors_in_a_row >= ERRORS_BEFORE_STOP:
                job.stop = True
                say(f"{ERRORS_BEFORE_STOP} errors in a row - something's wrong with the connection, so this run stops. "
                    "The next run tries again.")
                return
            if bucket == "blocked":
                job.blocks_in_a_row += 1
                if job.blocks_in_a_row >= BLOCKS_BEFORE_PAUSE and time.time() >= job.pause_until:
                    job.pauses += 1
                    job.blocks_in_a_row = 0
                    if job.pauses >= PAUSES_BEFORE_STOP:
                        job.stop = True
                        say("Still blocked after several pauses - stopping this run. The next run tries again later.")
                        return
                    job.pause_until = time.time() + PAUSE_ALL_MINUTES * 60
                    say(f"{BLOCKS_BEFORE_PAUSE} blocked lookups in a row - pausing for {PAUSE_ALL_MINUTES} min.")
        if job.taken % 25 == 0:
            report(job)
        await asyncio.sleep(random.uniform(*PAUSE_SECONDS))


def report(job, final=False):
    s = job.stats
    mins = (time.time() - job.started) / 60
    line = (f"{'Queue 1 done' if final else 'So far'}: {s['opened']} opened ({s.get('text', 0)} plain text, "
            f"{s.get('browser', 0)} browser), {s['blocked']} blocked, {s['error']} errors, "
            f"{s['not found']} not found - {mins:.0f} min.")
    say(line)
    return line


async def check_proxy(port):
    """Stop early, before any link is tried, if Decodo turns the password down."""
    from scrapling.fetchers import FetcherSession

    url = f"http://{quote(PROXY_USER, safe='')}:{quote(PASSWORD, safe='')}@{PROXY_HOST}:{port}"
    try:
        async with FetcherSession(retries=2, timeout=30, proxy=url) as s:
            r = await s.get(IP_CHECK_URL)
        status, problem = r.status, ""
    except Exception as e:
        status, problem = (407 if "407" in str(e) else None), short(e)
    if status == 407:
        fail("Decodo turned down the proxy login: either the password is wrong or the plan is out of data. Check "
             "Decodo's dashboard, the DECODO_PASSWORD secret (Settings > Secrets and variables > Actions) and "
             "PROXY_USER in 10.yml. No links were tried.")
    if status is None:
        fail(f"Couldn't reach the Decodo proxy ({problem}). No links were tried; the next run tries again.")
    say("Proxy password accepted." if status == 200 else f"The proxy answered (status {status}); carrying on.")


async def open_pages():
    mode, todo, links, results, fields = plan()
    if mode == "none":
        return
    if USE_PROXY and not PASSWORD:
        fail("The Decodo password isn't set. In this repo: Settings > Secrets and variables > Actions > "
             "New repository secret, name DECODO_PASSWORD, paste the password, Add secret. Then run again.")
    job = Job(mode)
    if USE_PROXY:
        await check_proxy(job.ports.free[0])
    bundle = Bundle(mode)
    queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    want = len(todo) if mode == "samples" or not LIMIT else min(LIMIT, len(todo))
    count = max(1, min(BROWSERS, want, len(job.ports.free) if USE_PROXY else BROWSERS))
    workers = [Worker(i + 1, job) for i in range(count)]
    say(f"Starting {count} browser(s)" + (" through Decodo." if USE_PROXY else " without a proxy."))
    try:
        await asyncio.gather(*(worker_loop(w, queue, job, fields, bundle) for w in workers))
    finally:
        for w in workers:
            await w.close()
        bundle.close()
        line = report(job, final=True)
        if job.out_of_time:
            say(f"Reached RUN_MINUTES ({RUN_MINUTES}); the rest wait for the next run.")
        summary([f"**Queue 1 ({'samples' if mode == 'samples' else 'open'})** - {line}"])


# ---------- queue 2 ----------
def artifact_runs():
    """Run numbers whose saved-page bundles GitHub still keeps."""
    if not GH_REPO:
        return []
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{GH_REPO}/actions/artifacts?per_page=100", "--jq",
             '.artifacts[] | select(.expired | not) | select(.name | startswith("pages-")) | .name'],
            capture_output=True, text=True, timeout=300, check=True).stdout
    except Exception as e:
        say(f"Couldn't list the saved pages of earlier runs ({short(e)}).")
        return []
    return sorted({int(n[6:]) for n in out.split() if re.fullmatch(r"pages-\d+", n)})


def fetch_bundle(run):
    dest = os.path.join(INBOX, str(run))
    if os.path.isdir(dest):
        return dest
    try:
        subprocess.run(["gh", "run", "download", str(run), "-n", f"pages-{run}", "-D", dest, "-R", GH_REPO],
                       capture_output=True, text=True, timeout=1800, check=True)
        return dest
    except Exception as e:
        say(f"Couldn't fetch the saved pages of run {run} ({short(e)}).")
        shutil.rmtree(dest, ignore_errors=True)
        return None


def bundle_mode(folder):
    try:
        with open(os.path.join(folder, "mode.json"), encoding="utf-8") as f:
            return json.load(f).get("mode")
    except (OSError, ValueError):
        return None


def bundle_entries(folder):
    entries = []
    try:
        with open(os.path.join(folder, "manifest.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass  # a line cut short when a run was stopped
    except OSError:
        pass
    return entries


def parse(path, url, enc):
    from scrapling.parser import Selector

    with open(path, "rb") as f:
        body = f.read()
    try:
        return Selector(content=body, url=url, encoding=enc or "utf-8")
    except Exception:
        return Selector(content=body.decode("utf-8", "replace"), url=url)


class Reader:
    def __init__(self, fields, results, rows):
        self.fields, self.results, self.rows = fields, results, rows
        self.stats = {"read": 0, "found": 0, "wanted": 0, "failed": 0, "not found": 0}
        self.missing = set()  # rows where items came out empty
        self.staged = {}  # row -> copy of its page, for check/ (only the newest CHECK_PAGES rows)
        self.new_ok = 0

    def stage(self, row, path):
        if row in self.staged or len(self.staged) < CHECK_PAGES or row > min(self.staged):
            os.makedirs(STAGE, exist_ok=True)
            self.staged[row] = os.path.join(STAGE, f"row-{row}.html")
            shutil.copyfile(path, self.staged[row])
            if len(self.staged) > CHECK_PAGES:
                os.remove(self.staged.pop(min(self.staged)))

    def record_page(self, link, row, run, path, enc, how, at, tries):
        try:
            page = parse(path, link, enc)
            values, found = extract(page, self.fields)
        except Exception as e:
            say(f"Row {row}: couldn't read the saved page ({short(e)}).")
            values, found = {}, 0
        n = len(self.fields)
        self.results[link] = dict(row=row, link=link, status="ok", values=values, found=f"{found} of {n}",
                                  how=how, tries=tries, time=at, run=run)
        self.stats["read"] += 1
        self.stats["found"] += found
        self.stats["wanted"] += n
        if found < n:
            self.missing.add(row)
            self.stage(row, path)
        else:
            self.missing.discard(row)
            if row in self.staged:
                os.remove(self.staged.pop(row))

    def take_bundle(self, run, folder):
        if bundle_mode(folder) != "full":
            return
        for e in bundle_entries(folder):
            link = e.get("link") or ""
            if not link:
                continue
            old = self.results.get(link)
            if old and old["run"] > run:
                continue  # a later try already counted
            new_try = not (old and old["run"] == run)
            tries = (old["tries"] if old else 0) + (1 if new_try else 0)
            row = self.rows.get(link, to_int(e.get("row")))
            if e.get("status") == "opened" and e.get("file") and os.path.exists(os.path.join(folder, e["file"])):
                self.record_page(link, row, run, os.path.join(folder, e["file"]), e.get("enc"), e.get("how", ""),
                                 e.get("at", ""), tries)
                if new_try:
                    self.new_ok += 1
            elif old and old["status"] == "ok":
                if new_try:
                    self.stats["failed"] += 1
                continue  # never replace a good result with a failed one
            else:
                status = e.get("status") if e.get("status") != "opened" else "error"
                self.results[link] = dict(row=row, link=link, status=status, values=old["values"] if old else {},
                                          found="", how=e.get("how", ""), tries=tries, time=e.get("at", ""), run=run)
                if new_try:
                    self.stats["not found" if status == "not found" else "failed"] += 1

    def take_samples(self):
        for link, (name, row, run, at) in sample_index().items():
            old = self.results.get(link)
            if old is not None and not (REREAD and old["run"] <= run):
                continue
            self.record_page(link, self.rows.get(link, row), run, os.path.join(SAMPLES, name), "utf-8", "browser",
                             at or (old["time"] if old else ""), old["tries"] if old else 1)


def keep_samples(folder):
    """Sample run: move the saved pages into the repo's samples/ folder for the other chat."""
    os.makedirs(SAMPLES, exist_ok=True)
    rows = []
    for e in bundle_entries(folder):
        if e.get("status") != "opened" or not e.get("file"):
            continue
        src = os.path.join(folder, e["file"])
        if not os.path.exists(src):
            continue
        name = os.path.basename(e["file"])
        shutil.copyfile(src, os.path.join(SAMPLES, name))
        txt = src[:-5] + ".txt"
        if os.path.exists(txt):
            shutil.copyfile(txt, os.path.join(SAMPLES, name[:-5] + ".txt"))
        rows.append([name, e.get("row", ""), e["link"], RUN_ID, e.get("at", "")])
    if not rows:
        return 0
    with open(SAMPLE_INDEX, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "row", "link", "run", "time"])
        w.writerows(rows)
    with open(HOWTO_FILE, "w", encoding="utf-8") as f:
        f.write(HOWTO.replace("{chars}", f"{MAX_FIELD_CHARS:,}").replace("{check}", str(CHECK_PAGES)))
    return len(rows)


def refresh_check(reader, replace):
    if reader.staged or replace:
        shutil.rmtree(CHECK, ignore_errors=True)
        if reader.staged:
            os.makedirs(CHECK, exist_ok=True)
            for row, path in reader.staged.items():
                shutil.copyfile(path, os.path.join(CHECK, f"row-{row}.html"))
    if not os.path.isdir(CHECK):
        return
    short_rows = {r["row"] for r in reader.results.values() if r["status"] == "ok" and short_of_items(r)}
    for name in os.listdir(CHECK):
        m = re.fullmatch(r"row-(\d+)\.html", name)
        if m and int(m.group(1)) not in short_rows:
            os.remove(os.path.join(CHECK, name))  # that row has all its items now
    if not any(n.endswith(".html") for n in os.listdir(CHECK)):
        shutil.rmtree(CHECK, ignore_errors=True)
        return
    with open(os.path.join(CHECK, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Pages with missing items\n\nThese saved pages gave fewer items than `fields.json` asks for "
                "(see the `found` column in `results.csv` for the same rows). Use them to fix `fields.json`; "
                "when it changes, every saved page is read again. This folder is replaced on each run that "
                "finds pages like these.\n")


def read_pages():
    fields = load_fields()
    links = read_links(quiet=True) or []
    here = os.path.join(INBOX, str(RUN_ID))
    has_here = os.path.isdir(here)

    if fields is None:
        n = keep_samples(here) if has_here and bundle_mode(here) == "samples" else 0
        if n:
            say(f"Saved {n} sample pages in samples/. Next: give the other chat this repo's samples folder and "
                "ask it to read samples/HOW-TO-fields.md and write fields.json in the top folder.")
            summary([f"**Queue 2** - saved {n} sample pages in `samples/`. Next: the other chat writes `fields.json`."])
            output(message=f"Samples: {n} pages", more="false")
        else:
            say("Nothing to read yet (no fields.json).")
            output(more="false")
        return

    results = load_results()
    rows = {link: row for row, link in links}
    reader = Reader(fields, results, rows)
    reader.take_samples()

    kept = artifact_runs()
    if REREAD:
        earlier = [r for r in kept if r != RUN_ID]
        say("fields.json changed (or a re-read was asked for), so every saved page is read again.")
    else:
        newest = max([r["run"] for r in results.values()] + [0])
        earlier = [r for r in kept if newest < r != RUN_ID]  # bundles whose reading never finished
        if earlier:
            say(f"Found {len(earlier)} earlier bundle(s) of saved pages that weren't read yet - reading them too.")
    for run in earlier:
        folder = fetch_bundle(run)
        if folder:
            reader.take_bundle(run, folder)
            shutil.rmtree(folder, ignore_errors=True)
    if has_here:
        reader.take_bundle(RUN_ID, here)

    save_results(results, list(fields))
    refresh_check(reader, replace=REREAD)

    s = reader.stats
    pct = f"{100 * s['found'] / s['wanted']:.0f}% of items found" if s["wanted"] else "no pages to read"
    missing = sorted(reader.missing)
    _, todo, _, _, _ = plan(quiet=True)
    done = sum(1 for _, u in links if is_done(results.get(u)))
    line = (f"Queue 2 done: {s['read']} pages read, {pct}; {s['failed']} couldn't be opened this time, {s['not found']} not found. "
            f"results.csv: {done:,} of {len(links):,} links done, {len(todo):,} to go.")
    say(line)
    if missing:
        say(f"{len(missing)} page(s) had missing items, e.g. rows {', '.join(map(str, missing[:15]))}"
            f"{'...' if len(missing) > 15 else ''}. Copies are in check/.")
    summary([f"**Queue 2** - {line}"] + ([f"{len(missing)} page(s) had missing items; copies in `check/`."] if missing else []))
    more = bool(todo) and reader.new_ok > 0
    output(message=f"Results: {s['read']} pages read, {done} of {len(links)} links done", more=str(more).lower())


# ---------- start ----------
def main():
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what not in ("plan", "open", "read"):
        sys.exit("Use: python 9.py plan | open | read")
    if not os.path.isdir(REPO):
        sys.exit(f"Can't find the results repo folder ({REPO}).")
    try:
        if what == "plan":
            mode, todo, _, _, _ = plan()
            output(work=str(mode != "none").lower(), mode=mode)
        elif what == "open":
            from scrapling.fetchers import AsyncStealthySession, FetcherSession  # noqa: F401 - sets up its logger

            logging.getLogger("scrapling").addFilter(MaskLogs())
            logging.getLogger("scrapling").setLevel(logging.WARNING)
            asyncio.run(open_pages())
        else:
            read_pages()
    except SystemExit:
        raise
    except Exception:
        print(mask(traceback.format_exc()), file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
