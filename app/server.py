"""
OBS Multistream Studio Backend Server

How streaming works:
  OBS streams once to the local relay (relay.py). The relay forwards that one
  stream to every account that's switched on, so plain OBS (no plugins) can go
  live on any number of Twitch, YouTube, TikTok and Kick accounts at once.
  Each account is sent either the Wide (16:9) picture or the Tall (9:16) one.

How accounts work:
  Twitch / YouTube / Kick: official sign-in in the default browser (oauth.py)
  fills in the account name, picture and stream key.
  TikTok: a browser sign-in window with its own profile per account
  (auth_manager.py) + TikTok's LIVE API / LIVE Producer page.
  Any platform can also take a pasted server + key.
"""

import copy
import html
import json
import socket
import sys
import threading
import time
import uuid
import http.server
import socketserver
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from auth_manager import AuthManager, PROFILE_ROOT
from paths import APP_DIR, DATA_DIR
from oauth import OAuth, REDIRECTS
from obs_bridge import OBSBridge
from relay import Relay, RELAY_SERVER, RELAY_KEY
from tiktok_live import TikTokLiveClient

PORT = 18890
CONFIG_PATH = DATA_DIR / "config.json"
# The app's own developer IDs for each platform (like StreamElements ships theirs).
# Filled in once by whoever builds the app; users then just click Sign in.
APP_CREDENTIALS_PATH = APP_DIR / "app_credentials.json"
WEB_DIR = APP_DIR / "web"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

PLATFORM_IDS = ["twitch", "youtube", "tiktok", "kick"]
NAMES = {"twitch": "Twitch", "youtube": "YouTube", "tiktok": "TikTok", "kick": "Kick"}
OAUTH_PLATFORMS = ("twitch", "youtube", "kick")
NEEDS_SECRET = ("youtube",)  # Kick's secret lives in the server-side helper
OPENABLE = ("https://dev.twitch.tv/", "https://console.cloud.google.com/", "https://kick.com/",
            "https://studio.youtube.com/", "https://www.tiktok.com/", "https://livecenter.tiktok.com/",
            "https://www.youtube.com/")

SECRET_FIELDS = ("key", "session_token")


def new_account(platform):
    return {
        "id": uuid.uuid4().hex[:12], "platform": platform, "enabled": True, "connected": False,
        "source": "",  # "signin" | "manual"
        "username": "", "display_name": "", "avatar": "", "server": "", "key": "",
        "canvas": "vertical" if platform == "tiktok" else "main",  # which picture this account gets
        # Encoding, per account: quality preset (low/good/best/custom), bitrate kbps, encoder speed, output size
        "quality": "good", "bitrate": 6000, "speed": "quality", "res": "full",
        # TikTok only
        "session_token": "", "auto_key": True, "title": "Live Stream", "room_id": "",
    }


# Picture quality presets (kbps + encoder speed). Simple mode picks one of these; Advanced can type exact numbers.
QUALITY_PRESETS = {"low": (3500, "fast"), "good": (6000, "quality"), "best": (8000, "best")}
DEFAULT_SETTINGS = {"ui_mode": "simple"}
# Output sizes an account can send (scaled on the GPU by the plugin); "full" = the canvas as-is.
RES_SIZES = {"main": {"1080": (1920, 1080), "720": (1280, 720), "480": (854, 480)},
             "vertical": {"1080": (1080, 1920), "720": (720, 1280), "480": (480, 854)}}

config_lock = threading.RLock()


def migrate(data):
    """v2 configs had one fixed slot per platform; turn those into accounts."""
    accounts = []
    old_tt = data.get("tiktok") or {}
    for pid, p in (data.get("platforms") or {}).items():
        if pid not in PLATFORM_IDS or not p.get("connected"):
            continue
        a = new_account(pid)
        a.update({k: p.get(k, a[k]) for k in ("enabled", "source", "username", "display_name", "avatar", "server", "key")})
        a["connected"] = True
        if pid == "tiktok":
            a.update(session_token=old_tt.get("session_token", ""), auto_key=old_tt.get("auto_key", True),
                     title=old_tt.get("title", "Live Stream"),
                     canvas="vertical" if old_tt.get("vertical", True) else "main")
            old_profile = PROFILE_ROOT / "tiktok"
            if old_profile.exists():
                try:
                    old_profile.rename(PROFILE_ROOT / a["id"])
                except OSError:
                    pass
        accounts.append(a)
    return accounts


def load_config():
    with config_lock:
        cfg = {"accounts": [], "settings": copy.deepcopy(DEFAULT_SETTINGS)}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                raw = data["accounts"] if "accounts" in data else migrate(data)
                for a in raw:
                    if not isinstance(a, dict):
                        continue
                    full = new_account(a.get("platform") if a.get("platform") in PLATFORM_IDS else "twitch")
                    full.update(a)
                    full["platform"] = full["platform"] if full["platform"] in PLATFORM_IDS else "twitch"
                    cfg["accounts"].append(full)
                if data.get("obs_service_backup"):
                    cfg["obs_service_backup"] = data["obs_service_backup"]
                st = data.get("settings") or {}
                if st.get("ui_mode") in ("simple", "advanced"):
                    cfg["settings"]["ui_mode"] = st["ui_mode"]
            except Exception as e:
                # Never silently replace a broken config with an empty one: keep the original next to it.
                broken = CONFIG_PATH.with_name(f"config.broken-{time.strftime('%Y%m%d-%H%M%S')}.json")
                try:
                    CONFIG_PATH.replace(broken)
                    print(f"config.json couldn't be read ({e}); kept it as {broken.name} and started fresh.")
                except OSError:
                    pass
        return cfg


def save_config(cfg):
    with config_lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


def update_config(fn):
    with config_lock:
        cfg = load_config()
        fn(cfg)
        save_config(cfg)
        return cfg


def find(cfg, account_id):
    return next((a for a in cfg["accounts"] if a["id"] == account_id), None)


REMOTE_APPS_URL = "https://tools.puppyskyee.com/ms-auth/config.json"
_remote_apps = {"at": 0.0, "data": {}, "busy": False, "fetched": threading.Event()}


def _fetch_remote_apps():
    try:
        req = urllib.request.Request(REMOTE_APPS_URL, headers={"User-Agent": "MultistreamStudio"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict):
            _remote_apps["data"] = data
    except Exception:
        pass  # offline or server down: the built-in copy still works
    finally:
        _remote_apps["busy"] = False
        _remote_apps["fetched"].set()


def remote_apps(wait=False):
    """Shared sign-in IDs from the owner's server, so every copy picks up changes. Cached 10 min.
    The fetch runs in the background so a slow network never stalls the dock's status polling;
    wait=True (used when a sign-in starts) gives the very first fetch up to 3 s to land."""
    if time.time() - _remote_apps["at"] > 600 and not _remote_apps["busy"]:
        _remote_apps["at"] = time.time()
        _remote_apps["busy"] = True
        threading.Thread(target=_fetch_remote_apps, daemon=True).start()
    if wait and not _remote_apps["data"]:
        _remote_apps["fetched"].wait(3)
    return _remote_apps["data"]


def load_apps(wait=False):
    apps = {pid: {"client_id": "", "client_secret": ""} for pid in OAUTH_PLATFORMS}
    try:
        data = json.loads(APP_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        for pid in apps:
            apps[pid].update({k: str(v) for k, v in (data.get(pid) or {}).items() if k in apps[pid]})
    except Exception:
        pass
    for pid, v in remote_apps(wait).items():
        if pid in apps and isinstance(v, dict) and v.get("client_id"):
            apps[pid]["client_id"] = str(v["client_id"])
            if pid == "youtube" and v.get("client_secret"):  # Google desktop secrets aren't confidential
                apps[pid]["client_secret"] = str(v["client_secret"])
    return apps


def app_ready(pid, apps):
    a = apps.get(pid, {})
    return bool(a.get("client_id") and (pid not in NEEDS_SECRET or a.get("client_secret")))


def save_signin(platform, key, info):
    """Store a finished sign-in. key = existing account id, or "new:<platform>".
    Signing in to an account that's already added updates it instead of making a duplicate."""
    def apply(cfg):
        a = find(cfg, key)
        if not a:
            a = next((x for x in cfg["accounts"] if x["platform"] == platform and info.get("username")
                      and x["username"] == info["username"]), None)
        if not a:
            a = new_account(platform)
            cfg["accounts"].append(a)
        a.update({k: info.get(k, "") for k in ("username", "display_name", "avatar", "server", "key")})
        a.update(connected=True, source="signin")
    update_config(apply)


def _on_tiktok_login(account_id, session_token, username, avatar):
    def apply(cfg):
        a = find(cfg, account_id)
        if not a:
            a = new_account("tiktok")
            a["id"] = account_id  # keeps the browser profile it signed in with
            cfg["accounts"].append(a)
        a.update(connected=True, source="signin", session_token=session_token, username=username,
                 display_name=f"@{username}" if username else "TikTok account", avatar=avatar)
    update_config(apply)


APP_VERSION = "1.1.0"

obs = OBSBridge()
relay = Relay()
oauth = OAuth()
tiktok = TikTokLiveClient()
tiktok_auth = AuthManager(_on_tiktok_login)
# notice: one plain-language line the dock shows (e.g. "OBS closed, so the stream ended"), cleared on the next Go live.
session = {"live": False, "started": 0.0, "engine": "", "notice": "", "notice_at": 0.0, "stopped_at": 0.0}
action_lock = threading.RLock()  # re-entrant: set_enabled -> stop_all, supervisor -> start_account
pending_tiktok = set()  # account ids of TikTok sign-ins that haven't finished yet

PLUGIN_VENDOR = "multistream-studio"


def notify(text):
    session["notice"], session["notice_at"] = text, time.time()
    print(text)


def _on_obs_disconnected():
    """OBS closed (or crashed) under us: every output died with it, so the session is over."""
    if session["live"]:
        with action_lock:
            if session["live"]:
                session["live"] = False
                session["stopped_at"] = time.time()
                tiktok_jobs.clear()
                started_at.clear()
                retry_state.clear()
                if session.get("engine") == "relay":
                    relay.stop()
                notify("OBS closed, so the stream ended on every account. Open OBS and press Go live again.")


obs.on_disconnect = _on_obs_disconnected


def plugin_call(request_type, data=None):
    return obs.vendor(PLUGIN_VENDOR, request_type, data)


def plugin_available():
    """True when our OBS plugin is loaded: it gives every account its own OBS output and a real 9:16 canvas."""
    return plugin_call("status").get("success", False)


class Rates:
    """kbps from cumulative byte counters."""

    def __init__(self):
        self.last = {}

    def kbps(self, key, total):
        now = time.time()
        prev = self.last.get(key)
        if not prev or total < prev[1]:
            self.last[key] = (now, total, 0)
            return 0
        if now - prev[0] < 0.8:
            return prev[2]
        k = int((total - prev[1]) * 8 / 1000 / (now - prev[0]))
        self.last[key] = (now, total, k)
        return k


rates = Rates()
started_at = {}  # account id -> when its output was asked to start (plugin engine)
retry_state = {}  # account id -> {"n": retries so far, "next": time of the next retry, "code": last stop code}


# ---------------------------------------------------------------------------- streaming
def push_url(a):
    return a["server"].rstrip("/") + "/" + a["key"]


def ready(a):
    if a["platform"] == "tiktok" and a["source"] == "signin" and a.get("auto_key"):
        return a["connected"]  # its key is fetched when going live
    return bool(a["connected"] and a["server"] and a["key"])


tiktok_jobs = {}       # account id -> {"state": "fetching"|"error", "message": str} while a key is being fetched
tiktok_no_access = {}  # account id -> time TikTok last said "no LIVE access" (skip the slow check for 30 min)


def refresh_tiktok_key(a):
    """TikTok keys are single-use per LIVE, so fetch a fresh one for signed-in accounts."""
    if a["source"] != "signin" or not a.get("auto_key"):
        return {"success": True}
    if time.time() - tiktok_no_access.get(a["id"], 0) < 1800:
        return {"success": False, "error": "This TikTok account can't go LIVE from streaming software yet."}
    res = tiktok.fetch_stream_key(a["session_token"], title=a.get("title") or "Live Stream")
    if not res.get("success"):
        # Fall back to TikTok's own LIVE Producer page, read with this account's saved sign-in.
        producer = tiktok_auth.fetch_producer_key(a["id"])
        if not producer.get("success"):
            if "can't go LIVE" in (producer.get("error") or ""):
                tiktok_no_access[a["id"]] = time.time()
            return {"success": False, "error": producer.get("error")}
        res = producer

    def apply(c):
        x = find(c, a["id"])
        if x:
            x.update(server=res["server_url"], key=res["stream_key"], room_id=res["room_id"])
    update_config(apply)
    a.update(server=res["server_url"], key=res["stream_key"], room_id=res["room_id"])
    return {"success": True}


def start_account(a):
    if a["platform"] == "tiktok" and a["source"] == "signin" and a.get("auto_key") and not a.get("_key_ready"):
        # Getting a TikTok key can take ~30 s; don't hold up the other accounts.
        tiktok_jobs[a["id"]] = {"state": "fetching", "message": "Getting a TikTok stream key…"}

        def job():
            res = refresh_tiktok_key(a)
            if not session["live"] or not find(load_config(), a["id"]):
                tiktok_jobs.pop(a["id"], None)
                return
            if not res.get("success"):
                tiktok_jobs[a["id"]] = {"state": "error", "message": res.get("error") or "Couldn't get a TikTok stream key."}
                return
            tiktok_jobs.pop(a["id"], None)
            start_account({**a, "_key_ready": True})
        threading.Thread(target=job, daemon=True).start()
        return {"success": True, "pending": True}
    if not (a["server"] and a["key"]):
        return {"success": False, "error": "This account has no stream key yet."}
    if session.get("engine") == "plugin":
        started_at[a["id"]] = time.time()
        canvas = "vertical" if a["canvas"] == "vertical" else "main"
        w, h = RES_SIZES[canvas].get(a.get("res"), (0, 0))
        return plugin_call("start_output", {"id": a["id"], "canvas": canvas, "server": a["server"].rstrip("/"), "key": a["key"],
                                             "bitrate": int(a.get("bitrate") or 6000), "preset": a.get("speed") or "quality",
                                             "width": w, "height": h})
    relay.add(a["id"], push_url(a), vertical=(a["canvas"] == "vertical"), bitrate=int(a.get("bitrate") or 6000))
    return {"success": True}


def stop_account(account_id):
    retry_state.pop(account_id, None)
    if session.get("engine") == "plugin":
        started_at.pop(account_id, None)
        plugin_call("stop_output", {"id": account_id})
    else:
        relay.remove(account_id)


# ---------------------------------------------------------------------------- supervisor
# OBS reconnects a dropped output by itself (the plugin asks for 20 tries, 3 s apart with back-off, about 1.5 h).
# It does NOT retry when the very first connection fails (platform down, DNS hiccup, PC woke from sleep) or once
# those tries run out: the output just stops. This thread restarts such outputs while the session is live,
# 15 s -> 30 s -> 60 s apart, and gives up after 20 restarts. Bad keys/addresses are never retried.
RETRYABLE_CODES = {-2, -4, -5}
RETRY_DELAYS = (15, 30, 60)
RETRY_MAX = 20


def _supervise_plugin(cfg, outputs):
    now = time.time()
    for a in cfg["accounts"]:
        if not (a["enabled"] and ready(a)) or a["id"] in tiktok_jobs:
            continue
        o = outputs.get(a["id"])
        st = retry_state.get(a["id"])
        if o and o.get("active"):
            if st and now - st["last"] > 20:  # back for a while: forget the retry history
                retry_state.pop(a["id"], None)
            continue
        if a["id"] not in started_at:
            continue  # was never started in this session (e.g. the user turned it on with no key)
        if now - started_at[a["id"]] < 8:
            continue  # still connecting
        code = (o or {}).get("stop_code") or 0
        if o is None:
            code = -5  # OBS lost the output entirely (e.g. a scene collection switch)
        if code not in RETRYABLE_CODES:
            continue
        if not st:
            st = retry_state[a["id"]] = {"n": 0, "next": now + RETRY_DELAYS[0], "code": code, "last": now}
            continue
        st["code"] = code
        if now < st["next"]:
            continue
        if st["n"] >= RETRY_MAX:
            continue
        st["n"] += 1
        st["last"] = now
        st["next"] = now + RETRY_DELAYS[min(st["n"], len(RETRY_DELAYS) - 1)]
        print(f"supervisor: restarting {a['platform']} account {a['id']} (stop code {code}, try {st['n']})")
        start_account(a)


def _adopt_running_outputs(outputs):
    """The app restarted while OBS kept streaming: pick the session back up instead of showing 'offline'."""
    cfg = load_config()
    ids = {a["id"] for a in cfg["accounts"]}
    live = [o for o in outputs.values() if o.get("active") and o["id"] in ids]
    if live:
        session.update(live=True, engine="plugin", started=time.time(), notice="")
        for o in live:
            started_at[o["id"]] = time.time() - 30
        notify("Picked up a stream that was already running in OBS.")


def supervisor():
    while True:
        time.sleep(5)
        try:
            with action_lock:
                if not obs.connect():
                    continue
                if session["live"] and session.get("engine") == "plugin":
                    res = plugin_call("status")
                    if res.get("success"):
                        outputs = {o["id"]: o for o in (res["data"].get("outputs") or [])}
                        _supervise_plugin(load_config(), outputs)
                elif not session["live"] and time.time() - session.get("stopped_at", 0) > 30:
                    # (outputs can take a few seconds to wind down after End stream; don't re-adopt those)
                    res = plugin_call("status")
                    if res.get("success"):
                        _adopt_running_outputs({o["id"]: o for o in (res["data"].get("outputs") or [])})
        except Exception as e:
            print(f"supervisor error: {e}")


def point_obs_at_relay():
    status = obs.get_stream_status()
    current = obs.get_stream_service_raw() or {}
    settings = current.get("streamServiceSettings") or {}
    on_relay = settings.get("server", "").rstrip("/") == RELAY_SERVER and settings.get("key") == RELAY_KEY
    if status.get("active"):
        if on_relay:
            return {"success": True}
        return {"success": False, "error": "OBS is already streaming somewhere else. Stop that stream in OBS first."}
    if not on_relay:
        if current and not load_config().get("obs_service_backup"):
            update_config(lambda c: c.__setitem__("obs_service_backup", current))
        res = obs.set_stream_service_settings(RELAY_SERVER, RELAY_KEY)
        if not res.get("success"):
            return res
    return obs.start_stream()


def restore_obs_settings_later():
    """Put the user's own Settings > Stream back once OBS has actually stopped."""
    def worker():
        for _ in range(40):
            time.sleep(0.5)
            if not obs.get_stream_status().get("active"):
                backup = load_config().get("obs_service_backup")
                if backup and obs.restore_stream_service(backup).get("success"):
                    update_config(lambda c: c.pop("obs_service_backup", None))
                return
    threading.Thread(target=worker, daemon=True).start()


def go_live():
    with action_lock:
        cfg = load_config()
        targets = [a for a in cfg["accounts"] if a["enabled"] and ready(a)]
        if not targets:
            return {"success": False, "error": "Switch on at least one connected account first."}
        if not obs.connect():
            return {"success": False, "error": obs.last_error}
        session["notice"] = ""
        retry_state.clear()
        started_at.clear()
        if plugin_available():
            # Each account gets its own OBS output; OBS's own Start Streaming isn't used.
            session["engine"] = "plugin"
            results = {a["id"]: start_account(a) for a in targets}
            if not any(r["success"] for r in results.values()):
                plugin_call("stop_all")
                return {"success": False, "results": results, "error": "None of the accounts could start."}
            session.update(live=True, started=time.time())
            return {"success": True, "results": results}
        session["engine"] = "relay"
        relay.start()
        if relay.error and not relay.active:
            return {"success": False, "error": relay.error}
        results = {a["id"]: start_account(a) for a in targets}
        if not any(r["success"] for r in results.values()):
            relay.stop()
            return {"success": False, "results": results, "error": "None of the accounts could start."}
        res = point_obs_at_relay()
        if not res.get("success"):
            relay.stop()
            return {"success": False, "error": res.get("error"), "results": results}
        session.update(live=True, started=time.time())
        return {"success": True, "results": results}


def close_tiktok_rooms(cfg):
    for a in cfg["accounts"]:
        if a["platform"] == "tiktok" and a.get("room_id"):
            tiktok.close_room(a["session_token"], a["room_id"])
    update_config(lambda c: [x.update(room_id="") for x in c["accounts"] if x["platform"] == "tiktok"])


def stop_all():
    with action_lock:
        session["live"] = False
        session["stopped_at"] = time.time()
        tiktok_jobs.clear()
        retry_state.clear()
        if session.get("engine") == "plugin":
            plugin_call("stop_all")
            started_at.clear()
        else:
            if obs.get_stream_status().get("active"):
                obs.stop_stream()
            relay.stop()
            restore_obs_settings_later()
        close_tiktok_rooms(load_config())
        return {"success": True}


def set_enabled(account_id, on):
    with action_lock:
        cfg = update_config(lambda c: (find(c, account_id) or {}).update(enabled=bool(on)))
        a = find(cfg, account_id)
        if not a or not session["live"]:
            return {"success": True}
        if on:
            return start_account(a)
        stop_account(account_id)
        if not [x for x in cfg["accounts"] if x["enabled"] and ready(x)]:
            return stop_all()
        return {"success": True}


def restart_account_later(account_id, delay=1.5):
    """Apply changed encoding to one live account: stop its output, then start it again with the new settings.
    Re-checked after the pause so an account never comes back if the stream ended or it was switched off meanwhile."""
    def worker():
        with action_lock:
            if not session["live"]:
                return
            stop_account(account_id)
        time.sleep(delay)
        with action_lock:
            a = find(load_config(), account_id)
            if session["live"] and a and a["enabled"] and ready(a):
                start_account(a)
    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------- status
def plugin_state(a, outputs):
    o = outputs.get(a["id"])
    if not o:
        return {"state": "connecting" if a["id"] in started_at else "off", "kbps": 0, "error": "", "uptime": 0}
    if o.get("active"):
        up = int(time.time() - started_at.get(a["id"], time.time()))
        return {"state": "reconnecting" if o.get("reconnecting") else "live",
                "kbps": rates.kbps(a["id"], o.get("total_bytes", 0)), "error": "", "uptime": up,
                "note": "Connection dropped, OBS is reconnecting…" if o.get("reconnecting") else "",
                "dropped": o.get("dropped_frames", 0), "frames": o.get("total_frames", 0),
                "congestion": round(float(o.get("congestion") or 0), 2)}
    err = o.get("last_error") or ""
    code = o.get("stop_code") or 0
    if a["id"] in started_at and time.time() - started_at[a["id"]] < 8 and not err and not code:
        return {"state": "connecting", "kbps": 0, "error": "", "uptime": 0}
    friendly = STOP_REASONS.get(code, "This account stopped streaming.")
    st = retry_state.get(a["id"])
    if st and st["n"] < RETRY_MAX:
        wait = max(0, int(st["next"] - time.time()))
        return {"state": "reconnecting", "kbps": 0, "uptime": 0, "error": friendly,
                "note": f"Trying again in {wait} s" if wait else "Trying again…"}
    if st:
        friendly += " Gave up after 20 tries: flip this account off and on to try again."
    return {"state": "error", "kbps": 0, "error": friendly + (f" ({err})" if err else ""), "uptime": 0}


# OBS output stop codes (libobs obs-defs.h) in plain words.
STOP_REASONS = {
    -1: "The server address looks wrong. Check it in this account's settings.",
    -2: "Couldn't reach the platform's server. Check your internet, or the server address.",
    -3: "The platform rejected the stream key. Sign in again or paste a fresh key.",
    -4: "Something went wrong while streaming to this account.",
    -5: "The connection dropped and couldn't reconnect.",
    -6: "This platform doesn't accept the video/audio settings being sent.",
    -8: "The video encoder failed. Try a lower quality or restart OBS.",
}


def public_account(a, apps, outputs=None):
    job = tiktok_jobs.get(a["id"]) if session["live"] and a["enabled"] else None
    if job:
        st = {"state": "connecting" if job["state"] == "fetching" else "error", "kbps": 0, "uptime": 0,
              "error": job["message"] if job["state"] == "error" else "", "note": job["message"] if job["state"] == "fetching" else ""}
    elif not (session["live"] and a["enabled"]):
        st = {"state": "off", "kbps": 0, "error": "", "uptime": 0}
    elif session.get("engine") == "plugin":
        st = plugin_state(a, outputs or {})
    else:
        st = relay.status(a["id"])
        e = st.get("error", "")
        if e:
            low = e.lower()
            if "-138" in e or "refused" in low or "error opening output" in low or "timed out" in low:
                st["error"] = STOP_REASONS[-2]
            elif "401" in e or "403" in e or "unauthorized" in low or "forbidden" in low:
                st["error"] = STOP_REASONS[-3]
    login = tiktok_auth.get_status(a["id"]) if a["platform"] == "tiktok" else oauth.status.get(a["id"], {})
    return {
        **{k: v for k, v in a.items() if k not in SECRET_FIELDS},
        "server": a["server"] if a["source"] == "manual" else "",
        "key_set": bool(a["key"]), "ready": ready(a),
        "app_ready": a["platform"] == "tiktok" or app_ready(a["platform"], apps),
        "login": login, **st,
    }


def build_status():
    cfg = load_config()
    obs_on = obs.connect()
    stream = obs.get_stream_status() if obs_on else {}
    stats = obs.get_stats() if obs_on else {}
    apps = load_apps()
    plugin = plugin_call("status") if obs_on else {"success": False}
    outputs = {o["id"]: o for o in (plugin.get("data", {}).get("outputs") or [])} if plugin.get("success") else {}
    accounts = [public_account(a, apps, outputs) for a in cfg["accounts"]]
    video = obs.request("GetVideoSettings") if obs_on else {"success": False}
    vd = video.get("data", {}) if video.get("success") else {}
    main_w, main_h = vd.get("outputWidth", 1920), vd.get("outputHeight", 1080)
    fps = round(vd.get("fpsNumerator", 30) / max(1, vd.get("fpsDenominator", 1)), 2) if vd else 30
    encoder = (plugin.get("data", {}) if plugin.get("success") else {}).get("encoder", "")
    for acc in accounts:
        canvas = "vertical" if acc["canvas"] == "vertical" else "main"
        full = (1080, 1920) if canvas == "vertical" else (main_w, main_h)
        w, h = RES_SIZES[canvas].get(acc.get("res"), full)
        if w >= full[0] or h >= full[1]:
            w, h = full  # never upscale
        acc["send"] = {"width": w, "height": h, "fps": fps, "bitrate": acc.get("bitrate", 6000), "encoder": encoder}
    adding = {pid: oauth.status.get(f"new:{pid}", {}) for pid in OAUTH_PLATFORMS}
    tt_new = [tiktok_auth.get_status(i) for i in pending_tiktok]
    adding["tiktok"] = next((s for s in tt_new if s["state"] == "waiting"), tt_new[-1] if tt_new else {})
    return {
        "obs": {
            "connected": obs_on, "error": "" if obs_on else obs.last_error,
            "streaming": stream.get("active", False), "reconnecting": stream.get("reconnecting", False),
            "fps": round(stats.get("activeFps", 0)), "cpu": round(stats.get("cpuUsage", 0), 1),
            "frame_ms": round(stats.get("averageFrameRenderTime", 0), 1),
            "render_missed": stats.get("renderSkippedFrames", 0), "render_total": stats.get("renderTotalFrames", 0),
            "encode_skipped": stats.get("outputSkippedFrames", 0), "encode_total": stats.get("outputTotalFrames", 0),
        },
        "live": session["live"],
        "notice": session.get("notice", ""),
        "version": APP_VERSION,
        "engine": session.get("engine") if session["live"] else ("plugin" if plugin.get("success") else "relay"),
        "plugin": plugin.get("data", {}) if plugin.get("success") else None,
        "live_seconds": int(time.time() - session["started"]) if session["live"] else 0,
        "receiving": True if session.get("engine") == "plugin" else relay.receiving(),
        "input_kbps": relay.input_kbps(),
        "output_kbps": sum(a["kbps"] for a in accounts),
        "accounts": accounts,
        "settings": cfg["settings"],
        "adding": adding,
        "apps_ready": {pid: (pid == "tiktok" or app_ready(pid, apps)) for pid in PLATFORM_IDS},
        "redirects": REDIRECTS,
    }


# ---------------------------------------------------------------------------- auth callback pages
def result_page(ok, title, detail):
    color = "#22c55e" if ok else "#ef4444"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1014;color:#e8eaf0;font:15px/1.5 "Segoe UI",system-ui,sans-serif}}
.c{{max-width:420px;padding:32px;border:1px solid #262b36;border-radius:14px;background:#161920;text-align:center}}
.i{{width:52px;height:52px;border-radius:50%;margin:0 auto 14px;display:grid;place-items:center;background:{color}22;color:{color};font-size:26px;font-weight:700}}
h1{{font-size:19px;margin:0 0 6px}}p{{color:#8a91a1;margin:0}}</style></head>
<body><div class="c"><div class="i">{"✓" if ok else "!"}</div><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p></div></body></html>"""


TWITCH_CALLBACK = """<!doctype html><html><head><meta charset="utf-8"><title>Connecting Twitch…</title></head>
<body style="background:#0e1014;color:#e8eaf0;font:15px 'Segoe UI',sans-serif;display:grid;place-items:center;min-height:100vh;margin:0">
<p id="m">Connecting Twitch…</p>
<script>
const h = new URLSearchParams(location.hash.slice(1)), q = new URLSearchParams(location.search);
fetch('/api/auth/twitch/token', {method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({access_token: h.get('access_token'), state: h.get('state') || q.get('state'), error: q.get('error_description') || q.get('error')})})
  .then(r => r.text()).then(t => { document.open(); document.write(t); document.close(); });
</script></body></html>"""


def finish_oauth(pid, state, error, finish):
    """Shared callback handling. finish(app, entry) returns account info."""
    key = f"new:{pid}"
    try:
        entry = oauth.take_state(pid, state)
        key = entry.get("key") or key
        if error:
            raise RuntimeError(error)
        info = finish(load_apps()[pid], entry)
        save_signin(pid, key, info)
        oauth.done(key, info["display_name"])
        return result_page(True, f"{NAMES[pid]} connected", f"Signed in as {info['display_name']}. You can close this tab and go back to OBS.")
    except Exception as e:
        oauth.fail(key, str(e))
        return result_page(False, f"{NAMES[pid]} didn't connect", str(e))


# ---------------------------------------------------------------------------- HTTP
class StudioRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _trusted(self):
        """Block other websites (and DNS-rebinding) from driving the local API."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.replace("http://", "") in ALLOWED_HOSTS

    def send_html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def json_response(self, data, code=200):
        res = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(res)))
        self.end_headers()
        self.wfile.write(res)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path.startswith("/api/"):
            if not self._trusted():
                return self.json_response({"error": "Forbidden"}, 403)
            try:
                if url.path == "/api/status":
                    return self.json_response(build_status())
                if url.path == "/api/twitch_channel":
                    tw = [a for a in load_config()["accounts"] if a["platform"] == "twitch" and a["username"]]
                    tw.sort(key=lambda a: not a["enabled"])
                    return self.json_response({"channel": tw[0]["username"] if tw else ""})
            except Exception as e:  # a bug must show as an error in the dock, not as "App offline"
                return self.json_response({"success": False, "error": f"Server error: {e}"}, 500)
            return self.json_response({"error": "Not Found"}, 404)
        if url.path == "/auth/twitch":
            return self.send_html(TWITCH_CALLBACK)
        if url.path == "/auth/youtube":
            return self.send_html(finish_oauth("youtube", q.get("state"), q.get("error"),
                                               lambda app, e: oauth.finish_youtube(app, q.get("code", ""), e["verifier"])))
        if url.path == "/auth/kick":
            return self.send_html(finish_oauth("kick", q.get("state"), q.get("error_description") or q.get("error"),
                                               lambda app, e: oauth.finish_kick(app, q.get("code", ""), e["verifier"])))
        if url.path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        if not self._trusted():
            return self.json_response({"error": "Forbidden"}, 403)
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            data = {}
        pid = data.get("platform")
        if pid is not None and pid not in PLATFORM_IDS:
            return self.json_response({"success": False, "error": "Unknown platform"}, 400)
        try:
            if url.path == "/api/auth/twitch/token":
                return self.send_html(finish_oauth("twitch", data.get("state"), data.get("error") or (None if data.get("access_token") else "Twitch didn't send a sign-in token."),
                                                   lambda app, e: oauth.finish_twitch(app, data["access_token"])))
            self.json_response(self.handle_api_post(url.path, pid, data))
        except Exception as e:
            self.json_response({"success": False, "error": f"Server error: {e}"}, 500)

    def handle_api_post(self, path, pid, data):
        aid = data.get("id")
        if path == "/api/golive":
            return go_live()
        if path == "/api/stopall":
            return stop_all()
        if path == "/api/accounts/enabled":
            return set_enabled(aid, data.get("on"))

        if path in ("/api/accounts/add", "/api/accounts/reconnect"):
            if path.endswith("reconnect"):
                a = find(load_config(), aid)
                if not a:
                    return {"success": False, "error": "That account doesn't exist any more."}
                pid, key = a["platform"], a["id"]
            else:
                key = f"new:{pid}"
            if pid == "tiktok":
                tid = key if not key.startswith("new:") else uuid.uuid4().hex[:12]  # new id = fresh browser profile
                res = tiktok_auth.start_login_flow(tid)
                if res.get("success") and key.startswith("new:"):
                    pending_tiktok.clear()
                    pending_tiktok.add(tid)
                return res
            apps = load_apps(wait=True)
            if not app_ready(pid, apps):
                return {"success": False, "error": f"{NAMES[pid]} sign-in isn't switched on in this copy of the app yet."}
            url = oauth.begin(pid, apps[pid], key)
            webbrowser.open(url)
            return {"success": True}

        if path == "/api/accounts/remove":
            with action_lock:
                a = find(load_config(), aid)
                if not a:
                    return {"success": True}
                stop_account(aid)
                if a["platform"] == "tiktok" and a.get("room_id"):
                    tiktok.close_room(a["session_token"], a["room_id"])
                cfg = update_config(lambda c: c.__setitem__("accounts", [x for x in c["accounts"] if x["id"] != aid]))
                oauth.status.pop(aid, None)
                if a["platform"] == "tiktok":
                    threading.Thread(target=tiktok_auth.forget, args=(aid,), daemon=True).start()
                if session["live"] and not [x for x in cfg["accounts"] if x["enabled"] and ready(x)]:
                    stop_all()  # that was the last account on air
            return {"success": True}

        if path == "/api/accounts/update":
            allowed = {"canvas": ("main", "vertical"), "speed": ("fast", "quality", "best"), "res": ("full", "1080", "720", "480"),
                       "auto_key": (True, False), "title": None, "display_name": None}
            fields = data.get("fields") or {}
            if not isinstance(fields, dict):
                return {"success": False, "error": "Bad request"}
            for k, choices in allowed.items():
                if k in fields and (choices is not None and fields[k] not in choices or choices is None and not isinstance(fields[k], str)):
                    return {"success": False, "error": f"'{fields[k]}' isn't a valid {k}."}
            try:
                bitrate = max(500, min(50000, int(fields["bitrate"]))) if "bitrate" in fields else None
            except (TypeError, ValueError):
                return {"success": False, "error": "Bitrate must be a number of kbps (500 to 50000)."}

            def apply(cfg):
                a = find(cfg, aid)
                if not a:
                    return
                for k, v in fields.items():
                    if k in allowed:
                        a[k] = v.strip()[:200] if isinstance(v, str) else v
                if fields.get("quality") in QUALITY_PRESETS:
                    a["quality"] = fields["quality"]
                    a["bitrate"], a["speed"] = QUALITY_PRESETS[fields["quality"]]
                if bitrate is not None:
                    a["bitrate"] = bitrate
                if "bitrate" in fields or "speed" in fields:
                    a["quality"] = next((q for q, v in QUALITY_PRESETS.items() if v == (a["bitrate"], a["speed"])), "custom")
            cfg = update_config(apply)
            a = find(cfg, aid)
            encoding = {"canvas", "quality", "bitrate", "speed", "res"} & set(fields)
            if a and encoding and session["live"] and a["enabled"]:
                # Apply now: restart just this account's output (a few seconds' blip for it only).
                restart_account_later(aid)
                return {"success": True, "restarting": True}
            return {"success": True}

        if path == "/api/accounts/manual":
            server, key = (data.get("server") or "").strip(), (data.get("key") or "").strip()
            if not server.startswith(("rtmp://", "rtmps://")) or not key:
                return {"success": False, "error": "Enter a server that starts with rtmp:// or rtmps:// and a stream key."}

            def apply(cfg):
                a = find(cfg, aid) if aid else None
                if not a:
                    a = new_account(pid)
                    cfg["accounts"].append(a)
                a.update(server=server, key=key, connected=True, source="manual", auto_key=False,
                         display_name=(data.get("name") or "").strip() or a["display_name"] or f"{NAMES[a['platform']]} (stream key)")
            update_config(apply)
            return {"success": True}

        if path == "/api/settings":
            def apply(cfg):
                if data.get("ui_mode") in ("simple", "advanced"):
                    cfg["settings"]["ui_mode"] = data["ui_mode"]
            return {"success": True, "settings": update_config(apply)["settings"]}

        if path == "/api/open":
            link = data.get("url", "")
            if not link.startswith(OPENABLE):
                return {"success": False, "error": "That link isn't allowed."}
            webbrowser.open(link)
            return {"success": True}

        if path == "/api/app":  # developer setup: store the app's own platform IDs
            apps = load_apps()
            for f in ("client_id", "client_secret"):
                if data.get(f):
                    apps[pid][f] = data[f].strip()
            APP_CREDENTIALS_PATH.write_text(json.dumps(apps, indent=2), encoding="utf-8")
            return {"success": True}

        return {"success": False, "error": "Unknown API endpoint"}


class StudioServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def already_running():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def run():
    if already_running():
        print(f"OBS Multistream Studio is already running on http://127.0.0.1:{PORT}")
        sys.exit(0)
    save_config(load_config())  # writes the migrated (accounts) format on first start
    print(f"Starting OBS Multistream Studio {APP_VERSION} on http://127.0.0.1:{PORT}")
    remote_apps()  # start fetching the shared sign-in IDs right away (in the background)
    threading.Thread(target=supervisor, daemon=True, name="supervisor").start()
    with StudioServer(("127.0.0.1", PORT), StudioRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            relay.stop()


if __name__ == "__main__":
    run()
