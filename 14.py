"""
14.py - works through your big list of links on this Mac through Decodo's ISP plan: 10 fixed addresses
(one per port), unlimited data on the paid plan. Each browser goes out on its own address.

It's 13.py's twin, made so the two can run at the same time without getting in each other's way:
  - it works from the END of the list upward (13.py goes from the top down);
  - it has its own folders, progress file and log (all with "isp" in the name, in ~/scrap-private);
  - each time it starts a round, it counts the links 13.py and your GitHub runs already finished as done.

Like 13.py, each time you press Run it gets the latest 11.py from GitHub, brings its own copy of your
results-links repo up to date, and carries on from where it stopped.

Before the first run, save the ISP plan's proxy password on this Mac, the way you saved the inbox key:
in Terminal type  pbpaste > ~/decodo-isp-password.txt && chmod 600 ~/decodo-isp-password.txt  but don't
press Enter yet; copy the password on Decodo's Proxy setup page, then press Enter. (A copied endpoint line,
host:port:username:password, works too.) The inbox key is read from ~/card-inbox-key.txt, as for 13.py.
Keep the Mac plugged in with the lid open. Stop with Ctrl+C: it finishes the pages in progress and saves.
It also stops by itself when too many pages fail (STOP_AFTER_FAILURES below), or the site keeps blocking.

The terminal, and the log in ~/scrap-private/isp-log.txt, only show row numbers and counts.
"""

# ---------------- settings ----------------
LINKS_PER_START = 40  # the trial has 0.1 GB of data: 40 links, then it stops. 0 = keep going through the list.
BROWSERS = 3  # browser windows at once, each on its own address (up to 10)
PAUSE_SECONDS = "3-8"  # wait between pages, in seconds, picked at random in this range
RETRY_FAILED = False  # True = also try links that already failed 3 times
STOP_AFTER_FAILURES = 15  # stops when this many of the last 30 pages failed (blocked, error, nothing read). 0 = never

PASSWORD_FILE = "~/decodo-isp-password.txt"  # the ISP plan's proxy password, only on this Mac
PROXY_HOST = "isp.decodo.com"
PROXY_PORTS = "10001-10010"  # each port is one of your 10 addresses
PROXY_USER = "spdtx2m9da"

RESULTS_REPO = "BrettS600/results-links"  # holds links.csv and your extension (cert-reader/)
LINKS_FILE = "links.csv"
EXTENSION_FOLDER = "cert-reader"
INBOX_REPO = "BrettS600/card-inbox"  # where the reads are saved (in its certs folder)
KEY_FILE = "~/card-inbox-key.txt"  # a GitHub key that can write to the inbox repo
FOLDER = "~/scrap-private"  # this Mac's copies, progress files and logs

WAIT_AFTER_ERRORS_MINUTES = 10  # lots of errors in a row (the internet dropped?): wait, then carry on
# -------------------------------------------

import asyncio
import csv
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_FOLDER = os.path.expanduser(FOLDER)
CLONE = os.path.join(HOME_FOLDER, "results-links-isp")
PROGRESS = os.path.join(HOME_FOLDER, "isp-progress.csv")
OTHER_PROGRESS = os.path.join(HOME_FOLDER, "mac-progress.csv")  # 13.py's progress (only read, never written)
LOG = os.path.join(HOME_FOLDER, "isp-log.txt")
WORK = os.path.join(HOME_FOLDER, "isp-work")
CODE_COPY = os.path.join(WORK, "scrap")
CODE_URL = os.environ.get("CODE_URL") or "https://github.com/BrettS600/scrap.git"
RESULTS_URL = os.environ.get("RESULTS_URL") or f"https://github.com/{RESULTS_REPO}.git"
STATUS_COLS = ["row", "link", "status", "tries", "time", "run"]
DONE = ("saved", "not found")
AUTH_WORDS = ("403", "401", "authentication failed", "could not read username", "permission to",
              "invalid username or password", "repository not found", "access denied", "write access")

opener = None  # 11.py, once loaded
secrets = []


class Problem(Exception):
    """Something to fix before it can carry on; the message says what."""


def hide(text):
    text = str(text)
    if opener is not None:
        return opener.mask(text)
    for s in secrets:
        text = text.replace(s, "[secret]")
    return text


def note(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {hide(msg)}", flush=True)


class Tee:
    """Shows output in the terminal and also keeps it in the log file."""

    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def git(args, cwd=None, timeout=600):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, hide((r.stdout or "") + (r.stderr or ""))


def last_line(out):
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    return lines[-1][:150] if lines else "no details"


# ---------- checks ----------
def check_python():
    if sys.version_info < (3, 10):
        raise Problem(f"This Python is {sys.version.split()[0]}; it needs 3.10 or newer. In VS Code: Cmd+Shift+P -> "
                      "'Python: Select Interpreter' -> pick the same 3.10+ Python you used for 13.py, then Run again.")


def check_git():
    rc, _ = git(["--version"], timeout=120)
    if rc != 0:
        raise Problem("Git isn't set up on this Mac yet, and it's needed to save the reads. Run  xcode-select --install  "
                      "in Terminal, then press Run again.")


def check_scrapling():
    try:
        version = importlib.metadata.version("scrapling")
    except importlib.metadata.PackageNotFoundError:
        raise Problem("Scrapling isn't installed for this Python. Run 3.py first (it installs it), then press Run again.")
    if not version.startswith("0.5"):
        note(f"This was built for Scrapling 0.5, and this Python has {version}. If anything misbehaves, run 3.py again.")


# ---------- this Mac's copies of the repos ----------
def refresh_copy(url, folder, what, must=True):
    """Brings this Mac's copy of a GitHub repo up to date. Returns False if it had to use last time's copy."""
    if os.path.isdir(os.path.join(folder, ".git")):
        rc, out = git(["fetch", "-q", "--depth", "1", "origin", "HEAD"], cwd=folder)
        if rc == 0:
            rc, out = git(["reset", "-q", "--hard", "FETCH_HEAD"], cwd=folder)
        if rc == 0:
            return True
        note(f"Couldn't update the copy of {what} ({last_line(out)}); using the copy from last time.")
        return False
    new = folder + ".new"
    shutil.rmtree(new, ignore_errors=True)
    rc, out = git(["clone", "-q", "--depth", "1", url, new])
    if rc != 0:
        shutil.rmtree(new, ignore_errors=True)
        if must:
            raise Problem(f"Couldn't copy {what} from GitHub ({last_line(out)}). Check the internet connection, "
                          "then press Run again.")
        return False
    shutil.rmtree(folder, ignore_errors=True)
    os.replace(new, folder)
    return True


def get_opener_code():
    """The path of the newest 11.py available."""
    got = refresh_copy(CODE_URL, CODE_COPY, "the code (11.py)", must=False)
    latest = os.path.join(CODE_COPY, "11.py")
    if got and os.path.isfile(latest):
        return latest
    beside = os.path.join(HERE, "11.py")
    for path in (latest, beside):
        if os.path.isfile(path):
            note("Couldn't get the latest 11.py from GitHub, so using " +
                 ("the copy from last time." if path == latest else "the 11.py in your scrap folder."))
            return path
    raise Problem("Couldn't get 11.py from GitHub, and there's no 11.py next to 14.py. Download 11.py from the scrap "
                  "repo into the same folder as 14.py, then press Run again.")


def load_opener(path):
    spec = importlib.util.spec_from_file_location("link_opener", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "setup_logging") or os.path.abspath(mod.STATUS_FILE) != os.path.abspath(PROGRESS):
        raise Problem("The 11.py it found is an older version that can't run on the Mac. Download the latest 11.py "
                      "from the scrap repo into the same folder as 14.py, then press Run again.")
    real_plan = mod.plan

    def plan_from_the_end(quiet=False):  # 13.py goes from the top down; this one goes from the bottom up
        todo, links, status = real_plan(quiet)
        return todo[::-1], links, status

    mod.plan = plan_from_the_end
    return mod


# ---------- progress ----------
def read_rows(path):
    if not os.path.isfile(path):
        return []
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            return [r for r in csv.DictReader(f) if r.get("link")]
    except OSError:
        return []


def count_others_as_done():
    """Links that 13.py or your GitHub runs finished count as done here too (their files are only read)."""
    mine = {r["link"]: r for r in read_rows(PROGRESS)}
    added = 0
    for path in (os.path.join(CLONE, "opened.csv"), OTHER_PROGRESS):
        for r in read_rows(path):
            if r.get("status") in DONE and (mine.get(r["link"]) or {}).get("status") not in DONE:
                mine[r["link"]] = r
                added += 1
    if added or not os.path.exists(PROGRESS):
        with open(PROGRESS + ".tmp", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, STATUS_COLS)
            w.writeheader()
            for r in mine.values():
                w.writerow({k: r.get(k, "") for k in STATUS_COLS})
        os.replace(PROGRESS + ".tmp", PROGRESS)
    if added:
        note(f"Counted {added} links that 13.py or your GitHub runs finished as done.")


def set_up_key():
    """Gives 11.py the inbox key, and checks GitHub accepts it."""
    url = os.environ.get("INBOX_URL", "")
    path = os.path.expanduser(KEY_FILE)
    key = ""
    if not url and os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            key = f.read().strip()
    if key:
        secrets.append(key)
        os.environ["CARD_INBOX_KEY"] = key
        os.environ["KEY_WHERE"] = f"the key in {KEY_FILE}"
        url = f"https://x-access-token:{quote(key, safe='')}@github.com/{INBOX_REPO}.git"
    elif not url:  # no key file: try this Mac's own GitHub login, if git has one saved
        url = os.environ["INBOX_URL"] = f"https://github.com/{INBOX_REPO}.git"
        os.environ["KEY_WHERE"] = "this Mac's GitHub login"
    no_helper = ["-c", "credential.helper="] if key else []  # never let git store the key in the Keychain
    rc, out = git(no_helper + ["ls-remote", "-q", url, "HEAD"], timeout=90)
    if rc == 0:
        return
    if not any(w in out.lower() for w in AUTH_WORDS):
        raise Problem(f"Couldn't reach GitHub ({last_line(out)}). Check the internet connection, then press Run again.")
    if key:
        raise Problem(f"GitHub turned down the key in {KEY_FILE}: it's wrong, expired, or can't reach {INBOX_REPO}. "
                      "Make a new one and save it in that file again (see 1.md), then press Run again.")
    raise Problem(f"There's no inbox key in {KEY_FILE} yet. Make one and save it there (see 1.md), then press Run again.")


def set_up_proxy():
    """Gives 11.py the ISP plan's password. 11.py checks Decodo accepts it before opening any links."""
    path = os.path.expanduser(PASSWORD_FILE)
    password = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            password = f.read().strip()
    if password.startswith(PROXY_HOST + ":") and password.count(":") >= 3:
        password = password.split(":", 3)[3]  # a whole copied endpoint line: host:port:username:password
    if not password:
        raise Problem(f"There's no proxy password in {PASSWORD_FILE} yet. In Terminal type  pbpaste > "
                      f"{PASSWORD_FILE} && chmod 600 {PASSWORD_FILE}  (don't press Enter yet), copy the password on "
                      "Decodo's Proxy setup page, then press Enter. Then press Run again.")
    secrets.append(password)
    os.environ.update({"DECODO_PASSWORD": password, "PROXY_HOST": PROXY_HOST, "PROXY_PORTS": PROXY_PORTS,
                       "PROXY_USER": PROXY_USER})


def keep_awake():
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-d", "-i", "-s", "-w", str(os.getpid())])
        note("The Mac won't sleep while this runs (keep it plugged in with the lid open).")


# ---------- running ----------
def wait(minutes, why):
    note(f"{why} - waiting {minutes:g} min, then carrying on. Ctrl+C stops.")
    time.sleep(minutes * 60)


def work_through_the_list():
    error_stops = 0
    while True:
        count_others_as_done()
        try:
            job = asyncio.run(opener.open_links())
        except opener.Stop as e:  # (11.py's wording is for GitHub; here the password is in a file)
            raise Problem(str(e).replace("the DECODO_PASSWORD secret and PROXY_USER in 12.yml",
                                         f"{PASSWORD_FILE} and PROXY_USER at the top of 14.py"))
        if job is None:
            return
        if job.why == "user":
            note("Stopped. Press Run again to carry on from here.")
            return
        if job.why == "failing":
            note("Stopped by itself: too many of the last pages failed. It's usually the site or the proxy having a "
                 "bad patch. Press Run again later; it carries on from here.")
            return
        if job.why == "blocked":
            note("Stopped by itself: the site kept blocking these addresses, even after pauses. Press Run again later; "
                 "a longer PAUSE_SECONDS at the top of 14.py may help.")
            return
        if LINKS_PER_START:
            note("Test done. If it looks fine, set LINKS_PER_START = 0 at the top of 14.py and press Run to work "
                 "through the list.")
            return
        todo, _, _ = opener.plan(quiet=True)
        if not todo:
            note("Every link on the list is done.")
            return
        progress = job.saver.saved > 0 or job.stats["not found"] > 0
        if job.why == "errors" or not progress:
            error_stops = error_stops + 1 if not progress else 1
            if error_stops >= 3:
                raise Problem("Still nothing but errors after several tries. Check the internet connection and "
                              "Decodo's dashboard (is the plan active, with data left?). Then press Run.")
            wait(WAIT_AFTER_ERRORS_MINUTES, "Lots of errors in a row (maybe the internet dropped)")
        else:
            error_stops = 0
        refresh_copy(RESULTS_URL, CLONE, "your results-links repo", must=False)  # picks up new links


def main():
    global opener
    os.makedirs(WORK, exist_ok=True)
    sys.stdout, sys.stderr = Tee(sys.stdout, LOG), Tee(sys.stderr, LOG)
    print(f"\n===== 14.py started, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC =====", flush=True)
    try:
        check_python()
        check_git()
        check_scrapling()
        code = get_opener_code()
        refresh_copy(RESULTS_URL, CLONE, "your results-links repo")
        set_up_key()
        set_up_proxy()
        os.environ.update({
            "REPO_DIR": CLONE, "LINKS_FILE": LINKS_FILE, "LINK_COLUMN": "0", "EXTENSION_FOLDER": EXTENSION_FOLDER,
            "INBOX_REPO": INBOX_REPO, "INBOX_FOLDER": "certs", "STATUS_FILE": PROGRESS, "WORK_DIR": WORK,
            "USE_PROXY": "true", "MATCH_TIMEZONE": "true", "LIGHT": "true", "HEADLESS": "false",
            "BROWSERS": str(BROWSERS), "PAUSE_SECONDS": PAUSE_SECONDS, "LIMIT": str(LINKS_PER_START),
            "RUN_MINUTES": "0", "RETRY_FAILED": "true" if RETRY_FAILED else "false",
            "FAIL_WINDOW": "30", "FAIL_STOP": str(STOP_AFTER_FAILURES),
        })
        opener = load_opener(code)
        opener.setup_logging()
        keep_awake()
        note(f"{'Up to ' + str(LINKS_PER_START) + ' links this time' if LINKS_PER_START else 'The whole list'}, "
             f"from the end of the list upward, {BROWSERS} browser{'s' if BROWSERS != 1 else ''}, "
             f"{PAUSE_SECONDS} s between pages, through Decodo's ISP plan (each browser on its own address).")
        work_through_the_list()
    except Problem as e:
        note(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        note("Stopped. Anything not saved yet gets opened again next time you press Run.")
    except Exception:
        print(hide(traceback.format_exc()), file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
