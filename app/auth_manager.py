"""
OBS Multistream Studio - TikTok sign-in helper

TikTok has no official way to hand a stream key to an app, so this opens a real
browser window (Chrome if installed, otherwise Edge, which every Windows PC has)
with its own saved profile in browser-profile/tiktok. It talks to that window
over the browser's built-in DevTools protocol, so no extra software is needed:
  - after sign-in it reads the sessionid cookie (used by tiktok_live.py), and
  - it can read the server + key from TikTok LIVE Producer, TikTok's official
    stream-key page (accounts with LIVE access only).
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from paths import DATA_DIR

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PROFILE_ROOT = DATA_DIR / "browser-profile"
PRODUCER_URL = "https://livecenter.tiktok.com/producer"

# Finds an rtmp(s) server and a stream key on the LIVE Producer page, whatever its layout.
FIND_KEY_JS = r"""
(() => {
  const inputs = [...document.querySelectorAll('input, textarea')].map(e => (e.value || '').trim());
  const texts = [...document.querySelectorAll('span, div, p, code')].filter(e => !e.children.length).map(e => (e.textContent || '').trim());
  const isServer = v => /^rtmps?:\/\/\S+$/i.test(v);
  const isKey = v => /^[A-Za-z0-9_\-?=&.]{16,}$/.test(v) && /\d/.test(v) && !/^user\d+$/i.test(v);
  const server = inputs.find(isServer) || texts.find(isServer) || '';
  const key = inputs.find(v => v !== server && isKey(v)) || (server ? texts.find(v => v !== server && isKey(v)) : '') || '';
  const noAccess = /not eligible|no access|don't have access|unavailable/i.test(document.body.innerText || '');
  return JSON.stringify({server, key, noAccess});
})()
"""


def find_browser():
    return next((b for b in BROWSERS if os.path.exists(b)), None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """A browser window driven over the DevTools protocol."""

    def __init__(self, url, profile_dir, headless=False):
        exe = find_browser()
        if not exe:
            raise RuntimeError("Couldn't find Chrome or Edge on this PC.")
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        args = [exe, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={self.port}",
                "--no-first-run", "--no-default-browser-check", "--window-size=520,800"]
        if headless:
            args.append("--headless=new")
        else:
            args.append(f"--app={url}")
        self.proc = subprocess.Popen(args + ([url] if headless else []))
        self._msg_id = 0
        self.ws = None
        for _ in range(60):
            try:
                info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1).read())
                self.ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
                break
            except Exception:
                time.sleep(0.5)
        if not self.ws:
            self.close()
            raise RuntimeError("The browser window didn't respond. Close any open TikTok sign-in windows and try again.")

    def alive(self):
        return self.proc.poll() is None

    def call(self, method, params=None, session=None):
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:
            res = json.loads(self.ws.recv())
            if res.get("id") == self._msg_id:
                if "error" in res:
                    raise RuntimeError(res["error"].get("message", "DevTools error"))
                return res.get("result", {})

    def cookies(self, domain):
        return {c["name"]: c["value"] for c in self.call("Storage.getCookies").get("cookies", []) if domain in c.get("domain", "")}

    def page_session(self):
        targets = self.call("Target.getTargets")["targetInfos"]
        page = next(t for t in targets if t["type"] == "page")
        return self.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})["sessionId"]

    def evaluate(self, expression, session):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, session)
        return res.get("result", {}).get("value")

    def close(self):
        try:
            if self.ws:
                self.call("Browser.close")
        except Exception:
            pass
        try:
            self.proc.wait(5)
        except Exception:
            self.proc.kill()


def profile_dir(account_id):
    return PROFILE_ROOT / account_id


class AuthManager:
    def __init__(self, on_tiktok_login):
        """on_tiktok_login(account_id, session_token, username, avatar) runs after a successful sign-in."""
        self.on_tiktok_login = on_tiktok_login
        self.states = {}  # account id -> {"state", "message"}
        self.lock = threading.Lock()  # one TikTok browser window at a time

    def get_status(self, account_id):
        return dict(self.states.get(account_id) or {"state": "idle", "message": ""})

    def busy(self):
        return any(s.get("state") == "waiting" for s in self.states.values())

    # ------------------------------------------------------------------ sign in
    def start_login_flow(self, account_id):
        """Open a sign-in window using this account's own browser profile (a new id = a fresh login)."""
        if self.busy():
            return {"success": False, "error": "A TikTok sign-in window is already open. Finish or close it first."}
        if not find_browser():
            return {"success": False, "error": "Couldn't find Chrome or Edge on this PC."}
        self.states[account_id] = {"state": "waiting", "message": "Sign in to TikTok in the window that just opened."}
        threading.Thread(target=self._run_tiktok_login, args=(account_id,), daemon=True).start()
        return {"success": True}

    def _run_tiktok_login(self, account_id):
        with self.lock:
            browser = None
            try:
                browser = Browser("https://www.tiktok.com/login", profile_dir(account_id))
                deadline = time.time() + 600
                while time.time() < deadline and browser.alive():
                    try:
                        cookies = browser.cookies("tiktok.com")
                    except Exception:
                        break
                    session_id = cookies.get("sessionid") or cookies.get("sessionid_ss")
                    if session_id:
                        username, avatar = self._read_profile(browser)
                        self.on_tiktok_login(account_id, session_id, username, avatar)
                        self.states[account_id] = {"state": "done", "message": f"Connected as @{username}" if username else "Connected to TikTok"}
                        time.sleep(1.2)
                        break
                    time.sleep(1)
                if self.states.get(account_id, {}).get("state") == "waiting":
                    self.states[account_id] = {"state": "error", "message": "The TikTok window was closed before signing in finished."}
            except Exception as e:
                self.states[account_id] = {"state": "error", "message": str(e)}
            finally:
                if browser:
                    browser.close()

    @staticmethod
    def _read_profile(browser):
        """Signed-in TikTok username + avatar: the account-info API first, then the data TikTok embeds in its home page."""
        try:
            session = browser.page_session()
            raw = browser.evaluate(
                "fetch('https://www.tiktok.com/passport/web/account/info/?aid=1459', {credentials: 'include'})"
                ".then(r => r.json()).then(j => JSON.stringify(j.data || {})).catch(() => '{}')", session)
            info = json.loads(raw or "{}")
            if info.get("username"):
                return info["username"], info.get("avatar_url", "")
            browser.call("Page.navigate", {"url": "https://www.tiktok.com/"}, session)
            for _ in range(10):
                time.sleep(1)
                raw = browser.evaluate(
                    "(() => { try { const d = JSON.parse(document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__').textContent);"
                    " const u = (d.__DEFAULT_SCOPE__['webapp.app-context'] || {}).user || {};"
                    " return JSON.stringify({name: u.uniqueId || '', avatar: u.avatarUri || ''}); } catch (e) { return '{}'; } })()", session)
                found = json.loads(raw or "{}")
                if found.get("name"):
                    avatar = found.get("avatar") or ""
                    return found["name"], avatar[0] if isinstance(avatar, list) and avatar else avatar
        except Exception:
            pass
        return "", ""

    def forget(self, account_id):
        """Sign out for real: delete this account's browser profile so the next sign-in starts fresh."""
        self.states.pop(account_id, None)
        d = profile_dir(account_id)
        for _ in range(5):
            if not d.exists():
                return
            shutil.rmtree(d, ignore_errors=True)
            time.sleep(0.5)

    # ------------------------------------------------------------------ LIVE Producer key
    def fetch_producer_key(self, account_id, timeout=40):
        """Read server + key from TikTok LIVE Producer using this account's saved sign-in (hidden window)."""
        with self.lock:
            browser = None
            try:
                browser = Browser(PRODUCER_URL, profile_dir(account_id), headless=True)
                session = browser.page_session()
                deadline = time.time() + timeout
                while time.time() < deadline:
                    time.sleep(2)
                    found = json.loads(browser.evaluate(FIND_KEY_JS, session) or "{}")
                    if found.get("server") and found.get("key"):
                        return {"success": True, "server_url": found["server"], "stream_key": found["key"], "room_id": ""}
                    if found.get("noAccess"):
                        return {"success": False, "error": "This TikTok account can't go LIVE from streaming software yet."}
                return {"success": False, "error": "This TikTok account can't go LIVE from streaming software yet (TikTok LIVE Producer shows no stream key)."}
            except Exception as e:
                return {"success": False, "error": str(e)}
            finally:
                if browser:
                    browser.close()
