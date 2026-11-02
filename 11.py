"""
11.py - the link opener for your extension. It runs on GitHub's computers (12.yml starts it in
your results repo), so it keeps working with your laptop off.

For each link in your links file that isn't done yet, it:
  1. opens the page through your Decodo proxy, in a browser with your extension loaded;
  2. waits for Cloudflare's check to pass and for your extension to read the page;
  3. takes the extension's read and saves it to your inbox repo, in the same file format the
     extension uses, 100 reads per save (one git push), so GitHub's limit of about 500 API
     saves an hour doesn't apply.

The extension gets no key here, so it never sends anything itself (no double saves).
A link only counts as done once its read is safely saved. Progress is kept in opened.csv in the
results repo: one row per link, with its status.

To save proxy data, pages after the first on each proxy address skip pictures, fonts, styles,
video and ad/tracking scripts. If a page then gives no read, it's opened again in full, and after
3 of those the whole run loads pages in full.

The log only ever shows row numbers and counts - never links, page text or what was read.
Settings come from the env: section of 12.yml; the defaults are below.

By hand:  python 11.py plan | open
"""

import asyncio
import csv
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote, urlparse


def env(name, default):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_bool(name, default):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def env_num(name, default):
    try:
        return type(default)(float(env(name, str(default))))
    except ValueError:
        return default


# ---------------- settings (set them in 12.yml) ----------------
LINKS_FILE = env("LINKS_FILE", "")  # "" = the CSV in the repo's top folder (links.csv if there are several)
LINK_COLUMN = env("LINK_COLUMN", "0")  # 0 = the first web address in each row; or a column number or heading
LIMIT = env_num("LIMIT", 50)  # most links one run opens (0 = no limit)
RUN_MINUTES = env_num("RUN_MINUTES", 300)  # stop opening links after this long (GitHub stops a job at 6 hours)
BROWSERS = env_num("BROWSERS", 2)  # browsers at once, each on its own proxy IP

USE_PROXY = env_bool("USE_PROXY", True)
PROXY_HOST = env("PROXY_HOST", "us.decodo.com")
PROXY_PORTS = env("PROXY_PORTS", "10001-10010")
PROXY_USER = env("PROXY_USER", "user-sp2e2o4qkx-sessionduration-30")
PASSWORD = os.environ.get("DECODO_PASSWORD", "").strip()  # GitHub secret; never printed

EXTENSION_FOLDER = env("EXTENSION_FOLDER", "cert-reader")  # the extension's folder in the results repo
INBOX_REPO = env("INBOX_REPO", "BrettS600/card-inbox")  # where reads are saved
INBOX_FOLDER = env("INBOX_FOLDER", "certs")  # folder in that repo
KEY = os.environ.get("CARD_INBOX_KEY", "").strip()  # GitHub key that can write to INBOX_REPO; never printed
BATCH = env_num("BATCH", 100)  # reads per save (the first save of a run is after 5, to check the key early)

READ_WAIT_SECONDS = env_num("READ_WAIT_SECONDS", 25)  # longest wait for the extension's first read
SETTLE_SECONDS = env_num("SETTLE_SECONDS", 5)  # a read counts as finished after this long without changing
LIGHT = env_bool("LIGHT", True)  # skip pictures, fonts, styles, video and trackers (after each IP's first page)
HEADLESS = env_bool("HEADLESS", False)  # 12.yml runs a hidden screen, so a "visible" browser works there
MATCH_TIMEZONE = env_bool("MATCH_TIMEZONE", True)  # browser clock = the proxy IP's time zone
PAUSE_SECONDS = tuple(float(x) for x in (env("PAUSE_SECONDS", "1-3").split("-") + ["3"])[:2])
MAX_TRIES = env_num("MAX_TRIES", 3)  # a link that fails this many times is left alone
RETRY_FAILED = env_bool("RETRY_FAILED", False)  # try links that already failed MAX_TRIES times again
IP_CHECK_URL = env("IP_CHECK_URL", "https://ip.decodo.com/json")
# ---------------------------------------------------------------

FIRST_BATCH = 5
PORT_COOLDOWN_SECONDS = 600
BLOCKS_BEFORE_PAUSE = 10
PAUSE_ALL_MINUTES = 15
PAUSES_BEFORE_STOP = 3
ERRORS_BEFORE_STOP = 6
FULL_LOADS_BEFORE_LIGHT_OFF = 3

REPO = os.path.abspath(env("REPO_DIR", "repo"))  # the results repo, checked out
WORK = os.path.abspath(env("WORK_DIR", os.path.join(tempfile.gettempdir(), "link-opener")))
INBOX_URL = env("INBOX_URL", "")  # for testing; normally built from INBOX_REPO and KEY
RUN_ID = env_num("GITHUB_RUN_ID", int(time.time()))
STATUS_FILE = os.path.join(REPO, "opened.csv")

CHALLENGE_TITLES = ("just a moment", "verify you are human", "attention required", "checking your browser")
BLOCK_KINDS = {"challenge", "firewall", "fingerprint", "ip_ban", "rate_limit"}
TRACKERS = {
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googlesyndication.com",
    "googleadservices.com", "adservice.google.com", "connect.facebook.net", "facebook.net", "hotjar.com",
    "hotjar.io", "clarity.ms", "segment.com", "segment.io", "cdn.segment.com", "bat.bing.com",
    "scorecardresearch.com", "quantserve.com", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "adsrvr.org", "amazon-adsystem.com", "analytics.tiktok.com", "sc-static.net", "ct.pinterest.com",
    "js-agent.newrelic.com", "bam.nr-data.net", "fullstory.com", "mouseflow.com", "crazyegg.com",
    "optimizely.com", "branch.io", "adnxs.com", "rubiconproject.com", "pubmatic.com", "casalemedia.com",
    "moatads.com", "chartbeat.com", "mixpanel.com", "amplitude.com", "heap.io", "intercomcdn.com",
}
SECRETS = [s for s in (PASSWORD, KEY) if len(s) >= 6]  # (links with a password in them are hidden whole anyway)
URL_RE = re.compile(r"""\b(?:https?|wss?|file)://[^\s"'<>)]+""", re.I)
HOSTS_RE = None  # the hostnames in the links file, hidden from all output
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


# ---------- privacy and output ----------
def mask(text):
    text = URL_RE.sub("[link]", str(text))
    if HOSTS_RE is not None:
        text = HOSTS_RE.sub("[the site]", text)
    for s in SECRETS:
        text = text.replace(s, "[secret]").replace(quote(s, safe=""), "[secret]")
    return text


def say(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {mask(msg)}", flush=True)


class MaskLogs(logging.Filter):
    def filter(self, record):
        record.msg, record.args = mask(record.getMessage()), None
        return True


def short(e):
    return mask(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")[:180]


class Stop(Exception):
    """Something the owner has to fix; the run stops and says what."""


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(mask(l) for l in lines) + "\n\n")


def output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={mask(v)}\n")


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
NOT_LINKS = re.compile(r"(results(-\d+)?|opened)\.csv", re.I)


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
            raise Stop("LINKS_FILE in 12.yml names a file that isn't in the repo's top folder.")
        return path
    names = sorted(n for n in os.listdir(REPO) if n.lower().endswith(".csv") and not NOT_LINKS.fullmatch(n))
    if not names:
        return None
    pick = next((n for n in names if n.lower() == "links.csv"), names[0])
    if len(names) > 1:
        say(f"There are {len(names)} CSV files in the top folder; using "
            f"{'links.csv' if pick.lower() == 'links.csv' else 'the first one by name'}. Set LINKS_FILE in 12.yml to choose.")
    return os.path.join(REPO, pick)


def hide_hosts(links):
    global HOSTS_RE
    hosts = set()
    for _, link in links:
        h = (urlparse(link).hostname or "").lower()
        if h:
            hosts.update({h, h[4:] if h.startswith("www.") else h})
    if hosts:
        parts = sorted((re.escape(h) for h in hosts), key=len, reverse=True)
        HOSTS_RE = re.compile(r"(?<![\w.-])(?:" + "|".join(parts) + r")(?![\w-])", re.I)


def read_links():
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
            raise Stop("LINK_COLUMN in 12.yml names a column heading that isn't in the links file.")
        column = heads.index(LINK_COLUMN.lower())
    links, empty, seen = [], 0, set()
    for i, row in enumerate(rows, start=1):
        cells = [row[column]] if column is not None and column < len(row) else ([] if column is not None else row)
        link = next((cell_link(c) for c in cells if cell_link(c)), "")
        if not link:
            empty += any(c.strip() for c in row)
            continue
        if link not in seen:
            seen.add(link)
            links.append((i, link))
    hide_hosts(links)
    if empty > max(5, len(links)):
        say("Most rows of the links file have no web address in them. If it came from a spreadsheet with "
            "clickable links, the export may have kept only the link text - export again with the addresses showing.")
    return links


# ---------- opened.csv ----------
STATUS_COLS = ["row", "link", "status", "tries", "time", "run"]


def load_status():
    status = {}
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, newline="", encoding="utf-8-sig", errors="replace") as f:
            for r in csv.DictReader(f):
                if r.get("link"):
                    status[r["link"]] = {"row": to_int(r.get("row")), "link": r["link"], "status": r.get("status") or "",
                                         "tries": to_int(r.get("tries")), "time": r.get("time") or "", "run": r.get("run") or ""}
    return status


def save_status(status):
    with open(STATUS_FILE + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, STATUS_COLS)
        w.writeheader()
        for r in sorted(status.values(), key=lambda r: (r["row"], r["link"])):
            w.writerow({k: r.get(k, "") for k in STATUS_COLS})
    os.replace(STATUS_FILE + ".tmp", STATUS_FILE)


def is_done(r):
    if r is None:
        return False
    if r["status"] in ("saved", "not found"):
        return True
    return (not RETRY_FAILED) and r["tries"] >= MAX_TRIES


def plan(quiet=False):
    links = read_links()
    if links is None:
        if not quiet:
            say("There's no links CSV in the repo's top folder yet. Upload it there and a run starts by itself.")
        return [], [], {}
    status = load_status()
    for row, link in links:  # keep row numbers current if rows moved
        if link in status:
            status[link]["row"] = row
    todo = [(r, u) for r, u in links if not is_done(status.get(u))]
    if not quiet:
        say(f"{len(links):,} links: {len(links) - len(todo):,} done, {len(todo):,} to go"
            + (f". This run opens up to {min(LIMIT, len(todo)):,}." if LIMIT and todo else "."))
    return todo, links, status


# ---------- the extension ----------
def find_extension():
    """(folder, popup page) of the extension in the results repo."""
    folder = os.path.join(REPO, EXTENSION_FOLDER)
    if not os.path.isfile(os.path.join(folder, "manifest.json")):
        found = []
        for base, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if not d.startswith(".") and base.count(os.sep) - REPO.count(os.sep) < 3]
            if "manifest.json" in files:
                found.append(base)
        if len(found) != 1:
            raise Stop(f"Can't find the extension: there's no manifest.json in the '{EXTENSION_FOLDER}' folder of this "
                       "repo. Upload the extension's folder, or set EXTENSION_FOLDER in 12.yml to its name.")
        folder = found[0]
    folder = os.path.realpath(folder)
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8-sig") as f:
            manifest = json.load(f)
    except ValueError:
        raise Stop("The extension's manifest.json isn't valid JSON.")
    popup = ((manifest.get("action") or manifest.get("browser_action") or {}).get("default_popup")
             or (manifest.get("options_ui") or {}).get("page") or manifest.get("options_page") or "popup.html")
    return folder, popup.lstrip("/")


def extension_id(folder):
    """The id Chromium gives an unpacked extension without a "key": from its folder's path."""
    h = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in h)


# ---------- saving to the inbox repo ----------
AUTH_WORDS = ("403", "401", "authentication failed", "could not read username", "permission to",
              "invalid username or password", "repository not found", "access denied", "write access")


def git(args, cwd=None, timeout=600):
    env_ = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env_)
    return r.returncode, mask((r.stdout or "") + (r.stderr or ""))


def inbox_url():
    return INBOX_URL or f"https://x-access-token:{quote(KEY, safe='')}@github.com/{INBOX_REPO}.git"


def file_for(rec, ms):
    """(file name, bytes) for one read, exactly as the extension names and writes it. None if incomplete."""
    rec = {k: v for k, v in rec.items() if k != "file"}
    grader = re.sub(r"[^a-z0-9-]", "", str(rec.get("grader") or "").lower())
    cert = str(rec.get("cert") or "")
    if not grader or not re.fullmatch(r"\d{4,14}", cert) or not rec.get("read_at"):
        return None
    return f"{grader}_{cert}_{ms}.json", json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def push_reads(records):
    """Saves {key: record} to the inbox repo in one commit. Returns how many were saved."""
    ms = int(time.time() * 1000)
    files = []
    for i, rec in enumerate(records.values()):
        f = file_for(rec, ms + i)
        if f:
            files.append(f)
    if not files:
        return 0
    os.makedirs(WORK, exist_ok=True)
    for attempt in range(6):
        folder = tempfile.mkdtemp(dir=WORK, prefix="inbox-")
        try:
            rc, out = git(["clone", "-q", "--depth", "1", inbox_url(), folder])
            if rc == 0:
                os.makedirs(os.path.join(folder, INBOX_FOLDER), exist_ok=True)
                for name, data in files:
                    with open(os.path.join(folder, INBOX_FOLDER, name), "wb") as f:
                        f.write(data)
                git(["config", "user.name", "github-actions[bot]"], cwd=folder)
                git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=folder)
                git(["add", "-A"], cwd=folder)
                rc, out = git(["commit", "-q", "-m", f"{len(files)} reads from the link opener"], cwd=folder)
                if rc == 0:
                    rc, out = git(["push", "-q", "origin", "HEAD"], cwd=folder)
            if rc == 0:
                return len(files)
            if any(w in out.lower() for w in AUTH_WORDS):
                raise Stop("GitHub turned down the CARD_INBOX_KEY secret: it's wrong, expired, or can't write to "
                           f"{INBOX_REPO}. Make a new key with Contents: Read and write on that repo and save it as the "
                           "secret again. Reads from this run weren't saved; those links get opened again next run.")
            say(f"Saving to the inbox repo didn't go through (try {attempt + 1}): {out.strip()[:150]}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        time.sleep(min(60, 5 * 2 ** attempt))
    return 0


class Saver:
    """Collects reads from all browsers and saves them in batches."""

    def __init__(self, status):
        self.status = status
        self.pending = {}  # key -> record
        self.links = {}  # key -> links it came from
        self.saved = 0
        self.saves = 0
        self.lock = asyncio.Lock()

    def add(self, reads, link):
        for key, rec in reads.items():
            old = self.pending.get(key)
            if old is None or str(rec.get("read_at", "")) >= str(old.get("read_at", "")):
                self.pending[key] = rec
            self.links.setdefault(key, set()).add(link)

    async def flush(self, force=False):
        async with self.lock:
            want = FIRST_BATCH if self.saves == 0 else BATCH
            if not self.pending or (not force and len(self.pending) < want):
                return
            batch, links = dict(self.pending), {k: set(v) for k, v in self.links.items()}
            try:
                n = await asyncio.to_thread(push_reads, batch)
            except Stop:
                raise
            except Exception as e:
                say(f"Saving to the inbox repo failed ({short(e)}).")
                n = 0
            if not n:
                say(f"Couldn't save {len(batch)} reads to the inbox repo; trying again with the next batch.")
                return
            for key in batch:
                if self.pending.get(key) is batch[key]:
                    self.pending.pop(key, None)
                    for link in links.get(key, ()):
                        r = self.status.get(link)
                        if r and r["status"] == "read":
                            r.update(status="saved", tries=r["tries"] + 1)
                    self.links.pop(key, None)
            self.saved += n
            self.saves += 1
            save_status(self.status)
            say(f"Saved {n} read{'s' if n != 1 else ''} to the inbox repo ({self.saved} this run).")


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


class Ports:
    def __init__(self, spec):
        ports = []
        for part in str(spec).replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-")
                ports += list(range(int(a), int(b) + 1))
            elif part:
                ports.append(int(part))
        random.shuffle(ports)
        self.free, self.resting = ports, {}

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


JS_READS = "() => chrome.storage.local.get('records').then(r => r.records || {})"
JS_FORGET = """async (taken) => {
  const s = await chrome.storage.local.get(['records', 'outbox']);
  const r = s.records || {}, o = s.outbox || {};
  for (const [k, at] of taken) { if (r[k] && r[k].read_at === at) { delete r[k]; delete o[k]; } }
  await chrome.storage.local.set({records: r, outbox: o});
}"""
JS_SETUP = "async () => { await chrome.storage.local.remove('ghKey'); await chrome.storage.local.set({autoSave: false}); }"


class Worker:
    """One browser with the extension loaded, on one proxy port (so one IP)."""

    def __init__(self, n, job):
        self.n, self.job = n, job
        self.port = None
        self.browser = None
        self.ext = None
        self.passed = set()
        self.profile = os.path.join(WORK, f"browser-{n}")  # kept between restarts: the cache saves proxy data

    async def ip_timezone(self):
        from scrapling.fetchers import FetcherSession

        try:
            async with FetcherSession(retries=1, timeout=20, proxy=self.job.proxy_url(self.port)) as s:
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
        folder = self.job.ext_folder
        options = dict(headless=HEADLESS, real_chrome=False, solve_cloudflare=True, hide_canvas=True, block_webrtc=True,
                       timeout=60000, retries=2, user_data_dir=self.profile,
                       extra_flags=[f"--disable-extensions-except={folder}", f"--load-extension={folder}"])
        if USE_PROXY:
            options["proxy"] = {"server": f"http://{PROXY_HOST}:{self.port}", "username": PROXY_USER, "password": PASSWORD}
        if MATCH_TIMEZONE:
            tz = await self.ip_timezone()
            if tz:
                options["timezone_id"] = tz
            say(f"Browser {self.n}: " + (f"port {self.port}, " if USE_PROXY else "")
                + (f"clock set to {tz}." if tz else "couldn't look up its time zone, using the default."))
        self.browser = AsyncStealthySession(**options)
        await self.browser.start()
        ctx = self.browser.context
        ctx.on("requestfinished", self.job.count_bytes)
        self.ext = await ctx.new_page()
        url = f"chrome-extension://{self.job.ext_id}/{self.job.ext_popup}"
        try:
            await self.ext.goto(url, timeout=20000)
            await self.ext.evaluate(JS_SETUP, isolated_context=False)
        except Exception as e:
            ids = {w.url.split("/")[2] for w in ctx.service_workers if w.url.startswith("chrome-extension://")}
            if len(ids) == 1 and self.job.ext_id not in ids:
                self.job.ext_id = ids.pop()
                await self.ext.goto(f"chrome-extension://{self.job.ext_id}/{self.job.ext_popup}", timeout=20000)
                await self.ext.evaluate(JS_SETUP, isolated_context=False)
            else:
                raise Stop(f"The extension didn't load in the browser ({short(e)}). Check that its folder in this repo "
                           "is the whole extension, with manifest.json at its top.")

    async def close(self):
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser, self.ext = None, None

    async def reads(self):
        return await self.ext.evaluate(JS_READS, isolated_context=False)

    async def forget(self, reads):
        await self.ext.evaluate(JS_FORGET, [[k, r.get("read_at")] for k, r in reads.items()], isolated_context=False)

    async def wait_for_read(self, page, box):
        """Runs after Cloudflare's check: waits until the extension has read the page and stopped changing."""
        try:
            title = (await page.title() or "").lower()
            if any(t in title for t in CHALLENGE_TITLES):
                return
            start, last, since = time.time(), None, None
            while True:
                found = await self.reads()
                sig = json.dumps(found, sort_keys=True)
                now = time.time()
                if found and sig == last:
                    if now - since >= SETTLE_SECONDS:
                        break
                elif found:
                    last, since = sig, now
                if not found and now - start > READ_WAIT_SECONDS:
                    break
                if found and now - start > READ_WAIT_SECONDS + 30:
                    break
                await asyncio.sleep(0.5)
            box["reads"] = found
        except Exception as e:
            box["error"] = short(e)

    async def visit(self, url, light):
        box = {}

        async def action(page):
            await self.wait_for_read(page, box)

        options = dict(page_action=action, google_search=False, disable_resources=light)
        if light:
            options["blocked_domains"] = TRACKERS
        page = await self.browser.fetch(url, **options)
        return classify(page), box.get("reads") or {}

    async def lookup(self, row, url):
        """(status, reads). status None = read by the extension."""
        job, site = self.job, site_of(url)
        if self.browser is None:
            await self.open()
        leftovers = await self.reads()
        if leftovers:  # a read that arrived late for an earlier link: keep it
            job.saver.add(leftovers, "")
            await self.forget(leftovers)
        moves, second_look, reopened, full_retry = 0, False, False, False
        while True:
            light = LIGHT and not job.light_off and site in self.passed and not full_retry
            try:
                kind, reads = await self.visit(url, light)
            except Exception as e:
                kind, reads = "error", {}
                say(f"Row {row}: browser error ({short(e)}).")
            if kind is None:
                self.passed.add(site)
                if reads:
                    if full_retry:
                        job.full_needed += 1
                        if job.full_needed >= FULL_LOADS_BEFORE_LIGHT_OFF and not job.light_off:
                            job.light_off = True
                            say("Your extension needs the full page on this site, so pages now load in full.")
                    await self.forget(reads)
                    return None, reads
                if light:
                    full_retry = True  # maybe skipping something stopped the read: try once in full
                    continue
                return "no read", {}
            if kind == "not_found":
                return kind, {}
            if kind == "error":
                if not reopened:
                    reopened = True
                    await self.open()
                    continue
                return "error", {}
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
            return kind, {}


class Job:
    def __init__(self, status, folder, popup):
        self.status = status
        self.ports = Ports(PROXY_PORTS) if USE_PROXY else None
        self.ext_folder, self.ext_popup, self.ext_id = folder, popup, extension_id(folder)
        self.saver = Saver(status)
        self.light_off, self.full_needed = not LIGHT, 0
        self.blocks_in_a_row, self.errors_in_a_row, self.pauses, self.pause_until = 0, 0, 0, 0
        self.started = time.time()
        self.deadline = self.started + RUN_MINUTES * 60
        self.taken, self.stop, self.out_of_time, self.problem = 0, False, False, None
        self.bytes = 0
        self.stats = {"read": 0, "no read": 0, "blocked": 0, "error": 0, "not found": 0}

    def proxy_url(self, port):
        if not USE_PROXY:
            return None
        return f"http://{quote(PROXY_USER, safe='')}:{quote(PASSWORD, safe='')}@{PROXY_HOST}:{port}"

    async def count_bytes(self, request):
        try:
            if request.url.startswith(("http://", "https://")):
                sizes = await request.sizes()
                self.bytes += (sizes.get("responseBodySize") or 0) + (sizes.get("responseHeadersSize") or 0) \
                    + (sizes.get("requestHeadersSize") or 0) + (sizes.get("requestBodySize") or 0)
        except Exception:
            pass


def mark(job, row, link, status_text, count_try=True):
    r = job.status.setdefault(link, {"row": row, "link": link, "status": "", "tries": 0, "time": "", "run": ""})
    r.update(row=row, status=status_text, time=now_utc(), run=str(RUN_ID))
    if count_try:
        r["tries"] += 1


async def worker_loop(worker, queue, job):
    while not job.stop:
        if LIMIT and job.taken >= LIMIT:
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
            kind, reads = await worker.lookup(row, url)
        except Stop:
            raise
        except Exception as e:
            kind, reads = "error", {}
            say(f"Row {row}: {short(e)}")

        if kind is None:
            job.stats["read"] += 1
            job.blocks_in_a_row = job.errors_in_a_row = 0
            mark(job, row, url, "read", count_try=False)  # counted once it's saved
            job.saver.add(reads, url)
            say(f"Row {row}: read by the extension ({len(reads)} read{'s' if len(reads) != 1 else ''}).")
            await job.saver.flush()
        else:
            bucket = ("blocked" if kind in BLOCK_KINDS else "not found" if kind == "not_found"
                      else "no read" if kind == "no read" else "error")
            job.stats[bucket] += 1
            mark(job, row, url, status_name(kind))
            say(f"Row {row}: {status_name(kind)}.")
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
    pages = s["read"] + s["no read"] + s["not found"]
    data = (f" Proxy data: about {job.bytes / 1e6:.2f} MB"
            + (f", {job.bytes / 1e3 / pages:.0f} KB per page." if pages else ".")) if USE_PROXY else ""
    line = (f"{'Done' if final else 'So far'}: {s['read']} read by the extension, {job.saver.saved} saved, "
            f"{s['no read']} no read, {s['blocked']} blocked, {s['error']} errors, {s['not found']} not found - "
            f"{mins:.0f} min.{data}")
    say(line)
    return line


async def check_proxy(job):
    from scrapling.fetchers import FetcherSession

    try:
        async with FetcherSession(retries=2, timeout=30, proxy=job.proxy_url(job.ports.free[0])) as s:
            r = await s.get(IP_CHECK_URL)
        status, problem = r.status, ""
    except Exception as e:
        status, problem = (407 if "407" in str(e) else None), short(e)
    if status == 407:
        raise Stop("Decodo turned down the proxy login: either the password is wrong or the plan is out of data. Check "
                   "Decodo's dashboard, the DECODO_PASSWORD secret and PROXY_USER in 12.yml. No links were tried.")
    if status is None:
        raise Stop(f"Couldn't reach the Decodo proxy ({problem}). No links were tried; the next run tries again.")
    say("Proxy password accepted." if status == 200 else f"The proxy answered (status {status}); carrying on.")


def check_key():
    rc, out = git(["ls-remote", "-q", inbox_url(), "HEAD"], timeout=60)
    if rc != 0:
        if any(w in out.lower() for w in AUTH_WORDS):
            raise Stop(f"GitHub turned down the CARD_INBOX_KEY secret: it's wrong, expired, or can't reach {INBOX_REPO}. "
                       "No links were tried.")
        raise Stop(f"Couldn't reach the inbox repo ({out.strip()[:120]}). No links were tried; the next run tries again.")
    say("Inbox key accepted.")


async def open_links():
    todo, links, status = plan()
    if not todo:
        output(more="false")
        return
    if USE_PROXY and not PASSWORD:
        raise Stop("The DECODO_PASSWORD secret isn't set. In this repo: Settings > Secrets and variables > Actions > "
                   "New repository secret. No links were tried.")
    if not KEY and not INBOX_URL:
        raise Stop("The CARD_INBOX_KEY secret isn't set: the GitHub key that can write to the inbox repo. In this repo: "
                   "Settings > Secrets and variables > Actions > New repository secret. No links were tried.")
    folder, popup = find_extension()
    job = Job(status, folder, popup)
    check_key()
    if USE_PROXY:
        await check_proxy(job)
    queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    want = min(LIMIT, len(todo)) if LIMIT else len(todo)
    count = max(1, min(BROWSERS, want, len(job.ports.free) if USE_PROXY else BROWSERS))
    workers = [Worker(i + 1, job) for i in range(count)]
    say(f"Starting {count} browser(s) with the extension" + (" through Decodo." if USE_PROXY else " without a proxy."))
    tasks = [asyncio.create_task(worker_loop(w, queue, job)) for w in workers]
    try:
        waiting = set(tasks)
        while waiting:  # if one browser hits a problem, the others finish their link and stop too
            done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_EXCEPTION)
            if any(t.exception() for t in done):
                job.stop = True
        for t in tasks:
            e = t.exception()
            if isinstance(e, Stop):
                job.problem = job.problem or str(e)
            elif e is not None:
                say(f"A browser stopped with an error: {short(e)}")
    finally:
        for w in workers:  # take any last reads before the browsers close
            try:
                if w.ext is not None:
                    last = await w.reads()
                    if last:
                        job.saver.add(last, "")
            except Exception:
                pass
            await w.close()
        if job.problem is None:
            try:
                await job.saver.flush(force=True)
            except Stop as e:
                job.problem = str(e)
        unsaved = 0
        for r in status.values():
            if r["status"] == "read":
                r["status"] = "not saved"
                unsaved += 1
        save_status(status)
        line = report(job, final=True)
        if unsaved:
            say(f"{unsaved} read(s) couldn't be saved; those links get opened again next run.")
        if job.out_of_time:
            say(f"Reached RUN_MINUTES ({RUN_MINUTES}); the rest wait for the next run.")
        summary([f"**Link opener** - {line}"])  # a stop reason is added by main()
        left = sum(1 for _, u in links if not is_done(status.get(u)))
        output(more=str(bool(left) and job.saver.saved > 0 and not job.problem).lower(),
               message=f"Opened links: {job.saver.saved} saved, {left} to go")
    if job.problem:
        raise Stop(job.problem)


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what not in ("plan", "open"):
        sys.exit("Use: python 11.py plan | open")
    if not os.path.isdir(REPO):
        sys.exit(f"Can't find the results repo folder ({REPO}).")
    try:
        if what == "plan":
            todo, _, _ = plan()
            output(work=str(bool(todo)).lower())
        else:
            from scrapling.fetchers import AsyncStealthySession, FetcherSession  # noqa: F401 - sets up its logger

            logging.getLogger("scrapling").addFilter(MaskLogs())
            logging.getLogger("scrapling").setLevel(logging.WARNING)
            asyncio.run(open_links())
    except Stop as e:
        say(str(e))
        summary([f"**Stopped:** {e}"])
        sys.exit(1)
    except Exception:
        print(mask(traceback.format_exc()), file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
