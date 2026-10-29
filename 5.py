"""
5.py - API finder. Opens the page once in the stealth browser, scrolls it, and records
every background data call (XHR/fetch) the page makes. Many sites load their listings
from a JSON endpoint; polling that directly is far lighter than loading the whole page.

Set URL below (usually the same page as 4.py) and press Run. It prints the most likely
data endpoints and saves all of them to data/5-api-calls.json.
If 4.py has run before, this reuses its saved cookies, so you won't re-solve Cloudflare.
"""

# ---------------- settings ----------------
URL = ""
HEADLESS = True
REAL_CHROME = True
SCROLLS = 5  # scroll down this many times to trigger lazy-loaded results
SHOW = 15  # how many calls to print
# -------------------------------------------

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE_FILE = os.path.join(DATA, "4-browser-state.json")
OUT = os.path.join(DATA, "5-api-calls.json")
PREVIEW_CHARS = 2000


def scroll(page):
    for _ in range(SCROLLS):
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(900)
    page.wait_for_timeout(2500)  # let the last calls finish


def is_json(resp):
    ctype = (resp.headers.get("content-type") or "").lower()
    head = (resp.body or b"")[:1].strip()
    return "json" in ctype or head in (b"{", b"[")


def main():
    if not URL:
        sys.exit("Set URL at the top of 5.py first, then Run again.")

    from scrapling.fetchers import StealthySession

    os.makedirs(DATA, exist_ok=True)
    session = StealthySession(
        headless=HEADLESS,
        real_chrome=REAL_CHROME,
        solve_cloudflare=True,
        hide_canvas=True,
        block_webrtc=True,
        timeout=60000,
        record_requests=True,
    )
    try:
        session.start()
    except Exception as e:
        if REAL_CHROME:
            sys.exit(f"Couldn't start Google Chrome ({e}).\nInstall Chrome, or set REAL_CHROME = False and Run again.")
        raise
    try:
        if os.path.exists(STATE_FILE):
            try:
                session.load_state(STATE_FILE)
            except Exception:
                pass
        page = session.fetch(URL, network_idle=True, page_action=scroll)
        print(f"Loaded {URL} (HTTP {page.status})")
        calls = [r for r in session.network.search(include_static=False, limit=None)
                 if r.meta.get("resource_type") in ("xhr", "fetch")]
    finally:
        session.close()

    rows = []
    for r in calls:
        body = r.body or b""
        rows.append({
            "method": r.method,
            "status": r.status,
            "url": r.url,
            "json": is_json(r),
            "bytes": len(body),
            "content_type": r.headers.get("content-type", ""),
            "request_body": (r.meta.get("request_body") or b"").decode("utf-8", "replace")[:PREVIEW_CHARS],
            "preview": body[:PREVIEW_CHARS].decode("utf-8", "replace"),
        })
    # Most likely listing endpoints first: JSON, successful, biggest.
    rows.sort(key=lambda x: (not x["json"], not (200 <= (x["status"] or 0) < 300), -x["bytes"]))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    if not rows:
        print("No background data calls seen - this page probably puts the data straight into its HTML,"
              " so 4.py with an ITEM_SELECTOR is the way to go.")
        return
    print(f"\n{len(rows)} background calls. Top {min(SHOW, len(rows))} (JSON first, biggest first):\n")
    for x in rows[:SHOW]:
        tag = "JSON" if x["json"] else "    "
        print(f"{tag}  {x['status']}  {x['method']:4}  {x['bytes']:>8,} B  {x['url'][:150]}")
    print(f"\nAll of them, with a preview of each response, are in {OUT}")


if __name__ == "__main__":
    main()
