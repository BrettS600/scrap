"""
19.py - works through your Card Ladder list (card_ladder_cert.csv in card-data) on this Mac, the same way the
List opener on GitHub (17.yml) does. Use one or the other: while this runs, keep the List opener disabled
(Actions > List opener > ... > Disable workflow), and enable it again when you go back to GitHub.

Each time you press Run (the triangle in VS Code), it:
  - gets the latest 11.py from GitHub (the same code the GitHub run uses);
  - gets only the list file from card-data, and only your extension's folder from results-links, using this
    Mac's own GitHub login;
  - picks up the progress the GitHub run saved (18.csv in scrap), so nothing it read is opened again;
  - opens the links still to go through Decodo's ISP plan, on the list's own addresses (ports 10006-10010, so your
    scans keep theirs), with your extension loaded, 3-8 seconds between pages;
  - saves each read to cardladder/ in your inbox repo as soon as it's read, and copies it to cardladder/ in
    scan-results (one history entry, no message), exactly like the GitHub run.
Its progress goes back to 18.csv in scrap every 10 minutes and when it stops, so the GitHub run carries on from
wherever this one got to. The progress is scrambled there, as always.

When the site turns an address away ("too many lookups" or its security check), that address rests 30 minutes
(longer if it's turned away again, up to 4 hours) and the link waits its turn without using a try.

Before the first run, these files go in your home folder (in TextEdit: New Document, Format > Make Plain Text,
paste the text, Cmd+S, type the name, press Shift+Cmd+H for your home folder, Save):
  - decodo-isp-password.txt   the ISP plan's proxy password (you have it already, from 14.py)
  - card-inbox-key.txt        the key that can write to card-inbox (you have it already)
  - scan-scramble-key.txt     the exact text you saved as the SCAN_SCRAMBLE_KEY secret in scrap
  - scan-results-key.txt      the key you saved as the SCAN_RESULTS_KEY secret. Without it, reads aren't copied to
                              scan-results from here; the GitHub run copies them the next time it runs.
Keep the Mac plugged in with the lid open. Stop with Ctrl+C: it finishes the pages in progress, saves, and saves
the progress to scrap. Press Run again to carry on from there.

The terminal, and the log in ~/scrap-private/list-log.txt, only show row numbers and counts.
"""

# ---------------- settings ----------------
LINKS_PER_START = 0  # 0 = keep going through the whole list. A number = that many links, then it stops (a test).
BROWSERS = 4  # browser windows at once, each on its own address
PAUSE_SECONDS = "3-8"  # wait between pages, in seconds, picked at random in this range
STOP_AFTER_FAILURES = 15  # stops when this many of the last 30 pages failed (an error or nothing read). 0 = never
REST_MINUTES = 30  # an address the site turns away rests this long (twice as long each time again, up to 4 hours)

PASSWORD_FILE = "~/decodo-isp-password.txt"  # the ISP plan's proxy password, only on this Mac
PROXY_HOST = "isp.decodo.com"
PROXY_PORTS = "10006-10010"  # the list's addresses (the scan run keeps 10001-10005). Add new ports here if you buy more.
PROXY_USER = "spdtx2m9da"

LIST_REPO = "BrettS600/card-data"
LIST_FILE = "data/cardladder/pic_certs/card_ladder_cert.csv"  # the only file taken from card-data
EXTENSION_REPO = "BrettS600/results-links"
EXTENSION_FOLDER = "cert-reader"  # the only folder taken from results-links
INBOX_REPO = "BrettS600/card-inbox"
INBOX_FOLDER = "cardladder"  # the reads go here
KEY_FILE = "~/card-inbox-key.txt"  # a GitHub key that can write to the inbox repo
SHARE_REPO = "BrettS600/scan-results"
SHARE_FOLDER = "cardladder"  # and a copy of each read here
SHARE_KEY_FILE = "~/scan-results-key.txt"  # a GitHub key that can write to scan-results
SCRAMBLE_FILE = "~/scan-scramble-key.txt"  # the same text as the SCAN_SCRAMBLE_KEY secret in scrap
PROGRESS_REPO = "BrettS600/scrap"
PROGRESS_FILE = "18.csv"  # the progress the GitHub list run keeps, shared with this Mac
FOLDER = "~/scrap-private"  # this Mac's copies, progress file and log

SHARE_PROGRESS_MINUTES = 10  # how often the progress goes back to scrap while it runs
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
import threading
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_FOLDER = os.path.expanduser(FOLDER)
WORK = os.path.join(HOME_FOLDER, "list-work")
CODE_COPY = os.path.join(WORK, "scrap")  # the code (11.py) and the shared progress (18.csv)
LIST_COPY = os.path.join(HOME_FOLDER, "list-card-data")  # only the list file
EXTENSION_COPY = os.path.join(HOME_FOLDER, "list-extension")  # only the extension's folder
RUN_DIR = os.path.join(HOME_FOLDER, "list-run")  # what 11.py works from: the list and the extension
PROGRESS = os.path.join(HOME_FOLDER, "list-progress.csv")
LOG = os.path.join(HOME_FOLDER, "list-log.txt")
LINKS_NAME = "list.csv"
# (each of these can be changed from outside for testing; normally they're built from the settings above)
CODE_URL = os.environ.get("CODE_URL") or f"https://github.com/{PROGRESS_REPO}.git"
LIST_URL = os.environ.get("LIST_URL") or f"https://github.com/{LIST_REPO}.git"
EXTENSION_URL = os.environ.get("EXTENSION_URL") or f"https://github.com/{EXTENSION_REPO}.git"
PROXY_HOST = os.environ.get("TEST_PROXY_HOST") or PROXY_HOST
PROXY_PORTS = os.environ.get("TEST_PROXY_PORTS") or PROXY_PORTS
PROXY_USER = os.environ.get("TEST_PROXY_USER") or PROXY_USER
STATUS_COLS = ["row", "link", "status", "tries", "time", "run"]
DONE = ("saved", "not found")
AUTH_WORDS = ("403", "401", "authentication failed", "could not read username", "permission to",
              "invalid username or password", "repository not found", "access denied", "write access")
WHO = ["-c", "user.name=BrettS600", "-c", "user.email=328803362+BrettS600@users.noreply.github.com",
       "-c", "commit.gpgsign=false"]

opener = None  # 11.py, once loaded
secrets = []
progress_lock = threading.Lock()


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
    """Git with this Mac's own GitHub login (the keys in the files are only ever handed to 11.py)."""
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, hide((r.stdout or "") + (r.stderr or ""))


def last_line(out):
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    return lines[-1][:150] if lines else "no details"


def read_file(path):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8-sig") as f:
        return f.read().strip()


HOW_TO_SAVE = ("In TextEdit: New Document, Format > Make Plain Text, paste {what}, Cmd+S, name it {name}, press "
               "Shift+Cmd+H (your home folder) and Save. Then press Run again.")


# ---------- checks ----------
def check_python():
    if sys.version_info < (3, 10):
        raise Problem(f"This Python is {sys.version.split()[0]}; it needs 3.10 or newer. In VS Code: Cmd+Shift+P -> "
                      "'Python: Select Interpreter' -> pick the same 3.10+ Python you used for 14.py, then Run again.")


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


# ---------- this Mac's copies ----------
def refresh_copy(url, folder, what, only=None, must=True):
    """Brings this Mac's copy of a GitHub repo up to date - with only=a path, just that file or folder of it.
    Returns False if it had to use last time's copy."""
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
    if only:
        steps = [(["clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout", url, new], None),
                 (["sparse-checkout", "set", "--no-cone", "/" + only.strip("/")], new),
                 (["checkout", "-q", "-f"], new)]
    else:
        steps = [(["clone", "-q", "--depth", "1", url, new], None)]
    rc, out = 0, ""
    for args, cwd in steps:
        rc, out = git(args, cwd=cwd)
        if rc != 0:
            break
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
    """The path of the newest 11.py available (the scrap copy also brings the GitHub run's progress)."""
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
    raise Problem("Couldn't get 11.py from GitHub, and there's no 11.py next to 19.py. Download 11.py from the scrap "
                  "repo into the same folder as 19.py, then press Run again.")


def get_list(must=True):
    """Only the list file from card-data, into the folder 11.py works from. False if it used last time's."""
    fresh = refresh_copy(LIST_URL, LIST_COPY, "your list in card-data", only=LIST_FILE, must=must)
    source = os.path.join(LIST_COPY, LIST_FILE)
    if not os.path.isfile(source):
        if must:
            raise Problem(f"There's no {os.path.basename(LIST_FILE)} in card-data where 19.py looks for it "
                          f"({os.path.dirname(LIST_FILE)}). Check LIST_FILE at the top of 19.py.")
        return False
    os.makedirs(RUN_DIR, exist_ok=True)
    shutil.copyfile(source, os.path.join(RUN_DIR, LINKS_NAME + ".tmp"))
    os.replace(os.path.join(RUN_DIR, LINKS_NAME + ".tmp"), os.path.join(RUN_DIR, LINKS_NAME))
    return fresh


def get_extension():
    """Only your extension's folder from results-links."""
    refresh_copy(EXTENSION_URL, EXTENSION_COPY, "your extension", only=EXTENSION_FOLDER + "/")
    source = os.path.join(EXTENSION_COPY, EXTENSION_FOLDER)
    if not os.path.isfile(os.path.join(source, "manifest.json")):
        raise Problem(f"There's no {EXTENSION_FOLDER} folder with a manifest.json in results-links. Check "
                      "EXTENSION_FOLDER at the top of 19.py.")
    target = os.path.join(RUN_DIR, EXTENSION_FOLDER)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)


def load_opener(path):
    spec = importlib.util.spec_from_file_location("link_opener", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "ADDRESS_REST_MINUTES") or os.path.abspath(mod.STATUS_FILE) != os.path.abspath(PROGRESS):
        raise Problem("The 11.py it found is an older version that can't run this list. Download the latest 11.py "
                      "from the scrap repo into the same folder as 19.py, then press Run again.")
    return mod


# ---------- progress, shared with the GitHub run ----------
def read_rows(path):
    if not os.path.isfile(path):
        return []
    csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            return [r for r in csv.DictReader(f) if r.get("link")]
    except OSError:
        return []


def write_rows(path, rows):
    with open(path + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, STATUS_COLS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (int(float(r.get("row") or 0)), r["link"])):
            w.writerow({k: r.get(k, "") for k in STATUS_COLS})
    os.replace(path + ".tmp", path)


def rank(r):
    try:
        tries = int(float(r.get("tries") or 0))
    except ValueError:
        tries = 0
    return r.get("status") in DONE, tries, r.get("time") or ""


def merge(*lists):
    """One record per link (a scrambled code): a finished one wins, then the one with more tries, then the newer."""
    out = {}
    for rows in lists:
        for r in rows:
            if r["link"] not in out or rank(r) > rank(out[r["link"]]):
                out[r["link"]] = r
    return list(out.values())


def take_github_progress():
    """What the GitHub run saved counts here too (the scrap copy was just brought up to date)."""
    theirs = read_rows(os.path.join(CODE_COPY, PROGRESS_FILE))
    mine = read_rows(PROGRESS)
    rows = merge(mine, theirs)
    write_rows(PROGRESS, rows)
    done_there = sum(1 for r in theirs if r.get("status") in DONE)
    if done_there:
        note(f"Picked up the GitHub run's progress: {done_there:,} links it already finished count as done.")
    return theirs


def share_progress(quiet=False):
    """Puts this Mac's progress into 18.csv in scrap (merged with whatever is there), so the GitHub run knows."""
    with progress_lock:
        for attempt in range(3):
            rc, out = git(["fetch", "-q", "--depth", "1", "origin", "HEAD"], cwd=CODE_COPY)
            if rc == 0:
                rc, out = git(["reset", "-q", "--hard", "FETCH_HEAD"], cwd=CODE_COPY)
            if rc != 0:
                break
            target = os.path.join(CODE_COPY, PROGRESS_FILE)
            rows = merge(read_rows(target), read_rows(PROGRESS))
            done = sum(1 for r in rows if r.get("status") in DONE)
            write_rows(target, rows)
            git(["add", PROGRESS_FILE], cwd=CODE_COPY)
            if git(["diff", "--cached", "--quiet"], cwd=CODE_COPY)[0] == 0:
                return True  # nothing new
            rc, out = git(WHO + ["commit", "-q", "-m", f"{PROGRESS_FILE}: list progress from the Mac ({done:,} done)"],
                          cwd=CODE_COPY)
            if rc == 0:
                rc, out = git(["push", "-q", "origin", "HEAD"], cwd=CODE_COPY)
            if rc == 0:
                if not quiet:
                    note(f"Progress saved to scrap ({done:,} links done), so the GitHub run carries on from here.")
                return True
            if any(w in out.lower() for w in AUTH_WORDS):
                break
            time.sleep(5 * (attempt + 1))
    note(f"Couldn't save the progress to scrap ({last_line(out)}). It's kept on this Mac and goes up next time; until "
         "then the GitHub run doesn't know about these reads, so keep it disabled.")
    return False


def keep_sharing_progress(stop):
    while not stop.wait(SHARE_PROGRESS_MINUTES * 60):
        try:
            share_progress(quiet=True)
        except Exception:
            pass


def check_scramble(github_rows):
    """If the scramble text doesn't match the secret, none of the GitHub run's codes match a link on the list, and
    every link would be opened again. Better to stop and say so."""
    saved = {r["link"] for r in github_rows if r.get("status") == "saved"}
    if not saved:
        return
    codes = {opener.key_of(u) for _, u in (opener.read_links() or [])}
    if not codes & saved:
        raise Problem(f"The text in {SCRAMBLE_FILE} doesn't match the SCAN_SCRAMBLE_KEY secret in scrap: none of the "
                      "links the GitHub run finished are recognized. Copy in the exact text you saved as that secret, "
                      "then press Run again. (Nothing was opened.)")


# ---------- keys ----------
def set_up_keys():
    """Hands 11.py the keys from the files on this Mac, and checks GitHub accepts the inbox key."""
    scramble = read_file(SCRAMBLE_FILE)
    if not scramble:
        raise Problem(f"There's no {SCRAMBLE_FILE} yet. It must hold the exact text you saved as the SCAN_SCRAMBLE_KEY "
                      "secret in scrap, so this Mac and the GitHub run keep one progress record. "
                      + HOW_TO_SAVE.format(what="just that text", name=os.path.basename(SCRAMBLE_FILE)))
    secrets.append(scramble)
    os.environ["LINK_KEY"] = scramble

    url = os.environ.get("INBOX_URL", "")
    key = "" if url else read_file(KEY_FILE)
    if not key and not url:
        raise Problem(f"There's no inbox key in {KEY_FILE} yet. "
                      + HOW_TO_SAVE.format(what="just the key", name=os.path.basename(KEY_FILE)))
    if key:
        secrets.append(key)
        os.environ.update({"CARD_INBOX_KEY": key, "KEY_WHERE": f"the key in {KEY_FILE}"})
        url = f"https://x-access-token:{quote(key, safe='')}@github.com/{INBOX_REPO}.git"
    no_helper = ["-c", "credential.helper="] if key else []  # never let git store the key in the Keychain
    rc, out = git(no_helper + ["ls-remote", "-q", url, "HEAD"], timeout=90)
    if rc != 0:
        if not any(w in out.lower() for w in AUTH_WORDS):
            raise Problem(f"Couldn't reach GitHub ({last_line(out)}). Check the internet connection, then press Run again.")
        raise Problem(f"GitHub turned down the key in {KEY_FILE}: it's wrong, expired, or can't reach {INBOX_REPO}. "
                      "Save a working one in that file, then press Run again.")

    share_key = read_file(SHARE_KEY_FILE)
    os.environ.update({"SHARE_REPO": SHARE_REPO, "SHARE_FOLDER": SHARE_FOLDER,
                       "SHARE_KEY_WHERE": f"the key in {SHARE_KEY_FILE}"})
    if share_key:
        secrets.append(share_key)
        os.environ["SHARE_KEY"] = share_key
    elif not os.environ.get("SHARE_URL"):
        os.environ["SHARE_REPO"] = ""
        note(f"There's no {SHARE_KEY_FILE}, so reads aren't copied to scan-results from here. The GitHub run copies "
             "them the next time it runs.")


def set_up_proxy():
    """Gives 11.py the ISP plan's password. 11.py checks Decodo accepts it before opening any links."""
    password = read_file(PASSWORD_FILE)
    if password.startswith(PROXY_HOST + ":") and password.count(":") >= 3:
        password = password.split(":", 3)[3]  # a whole copied endpoint line: host:port:username:password
    if not password:
        raise Problem(f"There's no proxy password in {PASSWORD_FILE} yet. "
                      + HOW_TO_SAVE.format(what="just the password", name=os.path.basename(PASSWORD_FILE)))
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
        try:
            job = asyncio.run(opener.open_links())
        except opener.Stop as e:  # (11.py's wording is for GitHub; here the password is in a file)
            raise Problem(str(e).replace("the DECODO_PASSWORD secret and PROXY_USER in 12.yml",
                                         f"{PASSWORD_FILE} and PROXY_USER at the top of 19.py"))
        finally:
            share_progress()
        if job is None:
            note("Every link on the list is done.")
            return
        if job.why == "user":
            note("Stopped. Press Run again to carry on from here.")
            return
        if job.why == "failing":
            note("Stopped by itself: too many of the last pages failed. It's usually the site or the proxy having a "
                 "bad patch. Press Run again later; it carries on from here.")
            return
        if LINKS_PER_START:
            note("Test done. If it looks fine, set LINKS_PER_START = 0 at the top of 19.py and press Run to work "
                 "through the list.")
            return
        get_list(must=False)  # picks up links added since
        todo, _, _ = opener.plan(quiet=True)
        if not todo:
            note("Every link on the list is done.")
            return
        progress = job.saver.saved > 0 or job.stats["not found"] > 0
        if job.why == "errors" or not progress:
            error_stops = error_stops + 1 if not progress else 1
            if error_stops >= 3:
                raise Problem("Still nothing but errors after several tries. Check the internet connection and "
                              "Decodo's dashboard (is the plan active?). Then press Run.")
            wait(WAIT_AFTER_ERRORS_MINUTES, "Lots of errors in a row (maybe the internet dropped)")
        else:
            error_stops = 0


def main():
    global opener
    os.makedirs(WORK, exist_ok=True)
    sys.stdout, sys.stderr = Tee(sys.stdout, LOG), Tee(sys.stderr, LOG)
    print(f"\n===== 19.py started, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC =====", flush=True)
    stop_sharing = threading.Event()
    try:
        check_python()
        check_git()
        check_scrapling()
        code = get_opener_code()
        get_list()
        get_extension()
        set_up_keys()
        set_up_proxy()
        os.environ.update({
            "REPO_DIR": RUN_DIR, "LINKS_FILE": LINKS_NAME, "LINK_COLUMN": "0", "EXTENSION_FOLDER": EXTENSION_FOLDER,
            "INBOX_REPO": INBOX_REPO, "INBOX_FOLDER": INBOX_FOLDER, "STATUS_FILE": PROGRESS, "WORK_DIR": WORK,
            "USE_PROXY": "true", "MATCH_TIMEZONE": "true", "LIGHT": "true", "HEADLESS": "false",
            "BROWSERS": str(BROWSERS), "PAUSE_SECONDS": PAUSE_SECONDS, "LIMIT": str(LINKS_PER_START),
            "RUN_MINUTES": "0", "RETRY_FAILED": "false", "FAIL_WINDOW": "30", "FAIL_STOP": str(STOP_AFTER_FAILURES),
            "FAST_SAVE": "true", "BATCH": "1", "WATCH_MINUTES": "0", "RESTART_ON_CLEAR": "false",
            "NEWEST_FIRST": "false", "SITE_GAPS": "5:180,3:60,2:20",
            "ADDRESS_REST_MINUTES": str(REST_MINUTES), "ADDRESS_REST_MAX_MINUTES": "240",
        })
        github_rows = take_github_progress()
        opener = load_opener(code)
        opener.setup_logging()
        check_scramble(github_rows)
        keep_awake()
        note("Reminder: keep the List opener on GitHub disabled while this runs, so the two don't open the same links.")
        note(f"{'Up to ' + str(LINKS_PER_START) + ' links this time' if LINKS_PER_START else 'The whole list'}, "
             f"from the top down, {BROWSERS} browser{'s' if BROWSERS != 1 else ''}, {PAUSE_SECONDS} s between pages, "
             f"through Decodo's ISP plan on ports {PROXY_PORTS}.")
        threading.Thread(target=keep_sharing_progress, args=(stop_sharing,), daemon=True).start()
        work_through_the_list()
    except Problem as e:
        note(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        note("Stopped. Anything not saved yet gets opened again next time you press Run.")
    except Exception:
        print(hide(traceback.format_exc()), file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        stop_sharing.set()


if __name__ == "__main__":
    main()
