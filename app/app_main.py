"""
Entry point for the packaged MultistreamStudio.exe (no console window).
Output goes to %APPDATA%\\MultistreamStudio\\app.log instead of a console.
"""

import sys

from paths import DATA_DIR

if sys.stdout is None or sys.stderr is None:
    log = open(DATA_DIR / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

import server  # noqa: E402  (after the output redirect)

if __name__ == "__main__":
    server.run()
