"""
3.py - one-time setup. Press Run once; you don't need it again after it says "Setup done".

It installs Scrapling (the version pinned in 2.txt) and the browser it drives,
then checks whether Google Chrome is installed, because 4.py and 5.py use your
real Chrome by default.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd):
    print("\n$ " + " ".join(cmd))
    return subprocess.call(cmd)


def main():
    if sys.version_info < (3, 10):
        print(
            f"This Python is {sys.version.split()[0]}; Scrapling needs 3.10 or newer.\n"
            "In VS Code: Cmd+Shift+P -> 'Python: Select Interpreter' -> pick a 3.10+ Python, then Run again."
        )
        sys.exit(1)

    pip = [sys.executable, "-m", "pip", "install", "--upgrade", "-r", os.path.join(HERE, "2.txt")]
    in_venv = sys.prefix != sys.base_prefix
    if run(pip) != 0:
        if in_venv:
            sys.exit("pip install failed - see the error above.")
        # Homebrew/system Pythons refuse plain installs (PEP 668); install for this user instead.
        print("\nPlain install refused; retrying as a per-user install...")
        if run(pip + ["--user", "--break-system-packages"]) != 0:
            sys.exit("pip install failed - see the error above.")

    # Downloads the Chromium build that Scrapling's stealth browser (Patchright) expects.
    # Only used when REAL_CHROME = False, but cheap to have as a fallback.
    if run([sys.executable, "-m", "patchright", "install", "chromium"]) != 0:
        sys.exit("Browser download failed - see the error above.")

    chrome_paths = [
        "/Applications/Google Chrome.app",
        os.path.expanduser("~/Applications/Google Chrome.app"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
    ]
    if any(os.path.exists(p) for p in chrome_paths):
        print("\nGoogle Chrome found - 4.py and 5.py will use it (REAL_CHROME = True).")
    else:
        print(
            "\nGoogle Chrome was not found. Either install it from google.com/chrome,\n"
            "or set REAL_CHROME = False at the top of 4.py and 5.py to use the bundled Chromium."
        )

    print("\nSetup done.")


if __name__ == "__main__":
    main()
