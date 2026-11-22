"""
Official sign-in (OAuth) for Twitch, YouTube and Kick.

The sign-in page opens in the default browser. After the user approves, the
provider sends them back to http://localhost:18890/auth/<platform>, and we use
the token once to read their name, picture and stream key.

Each platform needs a free one-time "developer app" (a client ID) that the
user registers on that site; the Setup screen walks them through it.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 18890
# Twitch and Kick require HTTPS redirect URLs, so they go to a tiny bounce page on
# tools.puppyskyee.com that forwards the browser to the app on that same PC
# (http://localhost:18890/auth/<platform>). Google "Desktop app" clients accept loopback directly.
BOUNCE = "https://tools.puppyskyee.com/ms-auth"
REDIRECTS = {
    "twitch": f"{BOUNCE}/twitch.html",
    "youtube": f"http://127.0.0.1:{PORT}/auth/youtube",
    "kick": f"{BOUNCE}/kick.html",
}


KICK_TOKEN_HELPER = "https://tools.puppyskyee.com/ms-auth/kick/token"  # holds the Kick secret server-side


def _http(url, data=None, headers=None, method=None):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    # Kick's Cloudflare blocks the default "Python-urllib" agent (error 1010).
    headers = {"User-Agent": "MultistreamStudio/1.0", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = detail.get("message") or detail.get("error_description") or detail.get("error") or detail
            if isinstance(msg, dict):
                msg = msg.get("message", str(msg))
        except Exception:
            msg = e.reason
        raise RuntimeError(f"{e.code}: {msg}")


class OAuth:
    def __init__(self):
        self.pending = {}  # state -> {platform, verifier, created}
        self.status = {}   # platform -> {"state": "waiting"|"done"|"error", "message": str}

    # ------------------------------------------------------------------ start
    def begin(self, platform, app, key):
        """key = the account id being refreshed, or "new:<platform>" when adding an account."""
        """Return the provider URL to open, remembering state/PKCE for the callback."""
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.pending = {k: v for k, v in self.pending.items() if time.time() - v["created"] < 900}
        self.pending[state] = {"platform": platform, "verifier": verifier, "created": time.time(), "key": key}
        self.status[key] = {"state": "waiting", "message": "Finish signing in in your browser."}
        redirect = REDIRECTS[platform]

        if platform == "twitch":
            return "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode({
                "client_id": app["client_id"], "redirect_uri": redirect, "response_type": "token",
                "scope": "channel:read:stream_key", "state": state, "force_verify": "true",
            }, quote_via=urllib.parse.quote)
        if platform == "youtube":
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
                "client_id": app["client_id"], "redirect_uri": redirect, "response_type": "code",
                "scope": "https://www.googleapis.com/auth/youtube.readonly", "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
            }, quote_via=urllib.parse.quote)
        if platform == "kick":
            return "https://id.kick.com/oauth/authorize?" + urllib.parse.urlencode({
                "client_id": app["client_id"], "redirect_uri": redirect, "response_type": "code",
                "scope": "user:read channel:read streamkey:read", "state": state,
                "code_challenge": challenge, "code_challenge_method": "S256",
            }, quote_via=urllib.parse.quote)
        raise ValueError(platform)

    def take_state(self, platform, state):
        entry = self.pending.pop(state or "", None)
        if not entry or entry["platform"] != platform:
            raise RuntimeError("This sign-in link expired. Click Connect again in Multistream Studio.")
        return entry

    def fail(self, key, message):
        self.status[key] = {"state": "error", "message": message}

    def done(self, key, name):
        self.status[key] = {"state": "done", "message": f"Connected as {name}"}

    # ------------------------------------------------------------------ finish
    def finish_twitch(self, app, token):
        h = {"Client-Id": app["client_id"], "Authorization": f"Bearer {token}"}
        user = _http("https://api.twitch.tv/helix/users", headers=h)["data"][0]
        key = _http(f"https://api.twitch.tv/helix/streams/key?broadcaster_id={user['id']}", headers=h)["data"][0]["stream_key"]
        return {
            "username": user["login"], "display_name": user["display_name"], "avatar": user.get("profile_image_url", ""),
            "server": "rtmp://live.twitch.tv/app", "key": key,
        }

    def finish_youtube(self, app, code, verifier):
        tok = _http("https://oauth2.googleapis.com/token", {
            "client_id": app["client_id"], "client_secret": app.get("client_secret", ""), "code": code,
            "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": REDIRECTS["youtube"],
        })
        h = {"Authorization": f"Bearer {tok['access_token']}"}
        chans = _http("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true", headers=h).get("items", [])
        if not chans:
            raise RuntimeError("That Google account has no YouTube channel.")
        snip = chans[0]["snippet"]
        try:
            streams = _http("https://www.googleapis.com/youtube/v3/liveStreams?part=snippet,cdn&mine=true&maxResults=50", headers=h).get("items", [])
        except RuntimeError as e:
            if "not enabled for live streaming" in str(e):
                raise RuntimeError(f"Signed in as {snip['title']}, but this channel can't live stream yet. "
                                   "Turn it on at youtube.com/features (Live streaming), wait up to 24 hours, then connect again.")
            raise
        streams = [s for s in streams if s.get("cdn", {}).get("ingestionInfo", {}).get("streamName")]
        if not streams:
            raise RuntimeError("No YouTube stream key found yet. Open YouTube Studio > Create > Go live once "
                               "(the first time, YouTube can take up to 24 hours to turn on live streaming), then connect again.")
        # Prefer the channel's default key (the one YouTube Studio's Stream tab uses).
        streams.sort(key=lambda s: "default" not in s["snippet"].get("title", "").lower())
        info = streams[0]["cdn"]["ingestionInfo"]
        return {
            "username": snip.get("customUrl") or snip["title"], "display_name": snip["title"],
            "avatar": snip.get("thumbnails", {}).get("default", {}).get("url", ""),
            "server": info.get("ingestionAddress") or "rtmp://a.rtmp.youtube.com/live2", "key": info["streamName"],
        }

    def finish_kick(self, app, code, verifier):
        if app.get("client_secret"):  # developer copy with the secret locally
            tok = _http("https://id.kick.com/oauth/token", {
                "grant_type": "authorization_code", "client_id": app["client_id"], "client_secret": app["client_secret"],
                "redirect_uri": REDIRECTS["kick"], "code": code, "code_verifier": verifier,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:  # normal copies: the owner's server adds the secret
            tok = _http(KICK_TOKEN_HELPER, json.dumps({"code": code, "code_verifier": verifier}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
            if not tok.get("access_token"):
                raise RuntimeError(tok.get("error") or "Kick sign-in helper didn't return a token.")
        h = {"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json"}
        chan = _http("https://api.kick.com/public/v1/channels", headers=h)["data"][0]
        user = {}
        try:
            user = _http("https://api.kick.com/public/v1/users", headers=h)["data"][0]
        except Exception:
            pass
        stream = chan.get("stream") or {}
        server, key = stream.get("url", ""), stream.get("key", "")
        if not key:
            raise RuntimeError("Kick didn't share a stream key. Make sure the app has the streamkey:read scope ticked.")
        if server and "/app" not in server:
            server = server.rstrip("/") + "/app"
        return {
            "username": chan.get("slug", ""), "display_name": user.get("name") or chan.get("slug", ""),
            "avatar": user.get("profile_picture", ""), "server": server, "key": key,
        }
