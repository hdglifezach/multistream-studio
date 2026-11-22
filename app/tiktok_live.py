"""
TikTok Live Automation Engine
Fetches dynamic RTMP stream keys via TikTok's Webcast API, using the
signed-in account's sessionid cookie. Requires an account with LIVE access.
"""

import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class TikTokLiveClient:
    def get_session_token(self, token=""):
        """Use the account's sessionid, falling back to TikTok LIVE Studio's cookie DB."""
        token = (token or "").strip()
        if len(token) > 10:
            return token

        cookie_db = os.path.expandvars(r"%APPDATA%\TikTok LIVE Studio\Network\Cookies")
        if os.path.exists(cookie_db):
            temp_db = cookie_db + ".tmp"
            try:
                with open(cookie_db, "rb") as src, open(temp_db, "wb") as dst:
                    dst.write(src.read())
                conn = sqlite3.connect(temp_db)
                row = conn.execute("SELECT value FROM cookies WHERE host_key LIKE '%tiktok.com%' AND name='sessionid'").fetchone()
                conn.close()
                if row and row[0]:  # newer builds encrypt cookies, leaving value empty
                    return row[0]
            except Exception:
                pass
            finally:
                try:
                    os.remove(temp_db)
                except Exception:
                    pass
        return None

    def _post(self, url, params, session_id, timeout):
        headers = {
            "User-Agent": USER_AGENT,
            "Cookie": f"sessionid={session_id}; sessionid_ss={session_id}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
        }
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def fetch_stream_key(self, session_token, title="Live Stream", game_tag_id=33):
        """Create a TikTok LIVE room and return its RTMP server + stream key."""
        session_id = self.get_session_token(session_token)
        if not session_id:
            return {"success": False, "error": "Not signed in to TikTok. Sign in (or paste your sessionid) in Setup."}

        params = {
            "title": title or "Live Stream",
            "live_type": 2,  # Game / Desktop Live
            "hashtag_id": 0,
            "game_tag_id": game_tag_id or 33,
            "gen_replay": 1,
        }
        try:
            res_json = json.loads(self._post("https://webcast.tiktok.com/webcast/room/create/", params, session_id, 12))
        except urllib.error.HTTPError as e:
            return {"success": False, "error": f"TikTok API error: HTTP {e.code} {e.reason}"}
        except ValueError:
            return {"success": False, "error": "TikTok sent back something that isn't JSON (the session may have expired)."}
        except Exception as e:
            return {"success": False, "error": f"Connection error: {e}"}

        data = res_json.get("data") or {}
        stream_url = data.get("stream_url") or {}
        rtmp_push = stream_url.get("rtmp_push_url", "")
        if not rtmp_push and stream_url.get("push_urls"):
            rtmp_push = stream_url["push_urls"][0]
        room_id = data.get("id_str") or data.get("room_id")

        if not rtmp_push:
            err = (res_json.get("message") or data.get("prompts")
                   or "TikTok didn't return a stream key. The account may not have LIVE access, or the session expired.")
            return {"success": False, "error": str(err)}

        # The stream key is everything after rtmp://host/app/
        match = re.match(r"^(rtmps?://[^/]+/[^/]+/)(.+)$", rtmp_push)
        if match:
            server_url, stream_key = match.group(1), match.group(2)
        else:
            server_url, _, stream_key = rtmp_push.rpartition("/")
            server_url += "/"

        return {"success": True, "server_url": server_url, "stream_key": stream_key, "room_id": str(room_id or "")}

    def close_room(self, session_token, room_id):
        """End a TikTok LIVE room session cleanly."""
        if not room_id:
            return {"success": True, "message": "No active room to close"}
        session_id = self.get_session_token(session_token)
        if not session_id:
            return {"success": False, "error": "No session token"}
        try:
            return {"success": True, "data": self._post("https://webcast.tiktok.com/webcast/room/finish_abnormal/", {"room_id": room_id}, session_id, 10)}
        except Exception as e:
            return {"success": False, "error": str(e)}
