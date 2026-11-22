"""
OBS Studio WebSocket Bridge (obs-websocket v5)
Starts/stops OBS's stream, points it at the local relay, and reads stats.
"""

import os
import sys
import json
import base64
import hashlib
import uuid
import threading
import time
from pathlib import Path

from paths import APP_DIR, DATA_DIR

try:
    import websocket
except ImportError:
    websocket = None


def _obs_config_dirs():
    """Every OBS config folder (the one holding user.ini and plugin_config\\) the app should look in:
    1. --obs-config-dir <path>   passed by the plugin when it launches the app (right for portable OBS too)
    2. %APPDATA%\\obs-studio      the installed OBS
    3. DATA_DIR\\obs_dirs.json    portable OBS folders the setup installed into
    4. APP_DIR\\dev_obs_dirs.txt  developer extras, one folder per line (gitignored)"""
    dirs = []
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--obs-config-dir" and i + 1 < len(argv):
            dirs.append(Path(argv[i + 1]))
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(Path(appdata) / "obs-studio")
    try:
        dirs += [Path(d) for d in json.loads((DATA_DIR / "obs_dirs.json").read_text(encoding="utf-8")).get("obs_config_dirs", [])]
    except Exception:
        pass
    try:
        dirs += [Path(l.strip()) for l in (APP_DIR / "dev_obs_dirs.txt").read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
    except Exception:
        pass
    return dirs


def _websocket_config_paths():
    return [d / "plugin_config" / "obs-websocket" / "config.json" for d in _obs_config_dirs()]


class OBSBridge:
    """One persistent obs-websocket connection with a background reader.

    The reader answers OBS's close handshake immediately. Without it, OBS's
    shutdown waits on us and hangs (seen 2026-11-20: "closing handshake timed out").
    """

    def __init__(self):
        self.ws = None
        self.connected = False
        self.host = "127.0.0.1"
        self.candidates = []  # (port, password) pairs from every OBS config we know
        self.last_error = ""
        self.lock = threading.RLock()  # connect/disconnect
        self.send_lock = threading.Lock()
        self.waiting = {}  # requestId -> [threading.Event, response]
        self.last_failed_connect = 0.0
        self.on_disconnect = None  # callback() run (on the reader thread) when a live connection to OBS is lost
        self.read_obs_credentials()

    def read_obs_credentials(self):
        """Collect port + password pairs from installed and portable OBS (only one runs at a time)."""
        self.candidates = []
        for path in _websocket_config_paths():
            if not path.exists():
                continue
            try:
                cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            pair = (int(cfg.get("server_port", 4455)), cfg.get("server_password", "") if cfg.get("auth_required", True) else "")
            if pair not in self.candidates:
                self.candidates.append(pair)
        if not self.candidates:
            self.candidates = [(4455, "")]

    # ------------------------------------------------------------------ connection
    def _try_identify(self, port, password):
        ws = websocket.create_connection(f"ws://{self.host}:{port}", timeout=2)
        hello = json.loads(ws.recv()).get("d", {})
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("authentication")
        if auth:
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        ws.send(json.dumps({"op": 1, "d": ident}))
        try:
            res = json.loads(ws.recv())
        except Exception:
            ws.close()
            return None  # OBS closes the socket on a wrong password
        if res.get("op") == 2:
            ws.settimeout(None)  # the reader thread blocks on recv
            return ws
        ws.close()
        return None

    def connect(self):
        if not websocket:
            self.last_error = "Python package 'websocket-client' is missing."
            return False
        with self.lock:
            if self.connected and self.ws:
                return True
            if time.time() - self.last_failed_connect < 3.0:
                return False
            self.read_obs_credentials()
            refused = reachable = False
            for port, pw in self.candidates:
                try:
                    ws = self._try_identify(port, pw)
                except Exception:
                    continue
                reachable = True
                if ws:
                    self.ws, self.connected, self.last_error = ws, True, ""
                    threading.Thread(target=self._reader, args=(ws,), daemon=True).start()
                    return True
                refused = True
            self.last_error = ("OBS rejected the WebSocket password (Tools > WebSocket Server Settings)." if refused or reachable
                               else "OBS is not running, or its WebSocket server is off (Tools > WebSocket Server Settings).")
            self.last_failed_connect = time.time()
            return False

    def _reader(self, ws):
        """Deliver responses to waiting requests; on OBS's close frame, reply and let go right away."""
        try:
            while True:
                opcode, data = ws.recv_data(control_frame=True)
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    break  # websocket-client already sent the close reply
                if opcode != websocket.ABNF.OPCODE_TEXT:
                    continue
                msg = json.loads(data.decode("utf-8"))
                d = msg.get("d", {})
                if msg.get("op") == 7:
                    slot = self.waiting.get(d.get("requestId"))
                    if slot:
                        slot[1] = d
                        slot[0].set()
        except Exception:
            pass
        lost = False
        with self.lock:
            if self.ws is ws:
                self.connected = False
                self.ws = None
                self.last_error = "OBS closed the connection (OBS may be shutting down)."
                self.last_failed_connect = time.time()  # don't reconnect into a closing OBS
                lost = True
        try:
            ws.close()
        except Exception:
            pass
        for slot in list(self.waiting.values()):
            slot[0].set()
        if lost and self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception:
                pass

    def disconnect(self):
        with self.lock:
            ws, self.ws, self.connected = self.ws, None, False
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def request(self, request_type, request_data=None, timeout=8):
        if not self.connect():
            return {"success": False, "error": self.last_error}
        req_id = str(uuid.uuid4())
        slot = [threading.Event(), None]
        self.waiting[req_id] = slot
        try:
            with self.send_lock:
                ws = self.ws
                if not ws:
                    return {"success": False, "error": self.last_error or "Not connected to OBS"}
                ws.send(json.dumps({"op": 6, "d": {"requestType": request_type, "requestId": req_id, "requestData": request_data or {}}}))
            if not slot[0].wait(timeout) or slot[1] is None:
                return {"success": False, "error": self.last_error or f"OBS didn't answer {request_type}"}
            status = slot[1].get("requestStatus", {})
            if status.get("result"):
                return {"success": True, "data": slot[1].get("responseData", {}) or {}}
            return {"success": False, "error": status.get("comment") or f"{request_type} failed", "code": status.get("code")}
        except Exception as e:
            self.disconnect()
            self.last_error = f"Lost connection to OBS: {e}"
            return {"success": False, "error": self.last_error}
        finally:
            self.waiting.pop(req_id, None)

    # ------------------------------------------------------------------ main stream
    def get_stream_status(self):
        res = self.request("GetStreamStatus")
        if res.get("success"):
            d = res["data"]
            return {
                "active": d.get("outputActive", False),
                "reconnecting": d.get("outputReconnecting", False),
                "timecode": d.get("outputTimecode", "00:00:00"),
                "congestion": d.get("outputCongestion", 0) or 0,
                "total_bytes": d.get("outputBytes", 0),
                "dropped_frames": d.get("outputSkippedFrames", 0),
                "total_frames": d.get("outputTotalFrames", 0),
            }
        return {"active": False, "error": res.get("error")}

    def start_stream(self):
        return self.request("StartStream")

    def stop_stream(self):
        return self.request("StopStream")

    def get_stream_service_raw(self):
        res = self.request("GetStreamServiceSettings")
        return res["data"] if res.get("success") else None

    def restore_stream_service(self, raw):
        raw = dict(raw)
        # obs-websocket rejects an empty settings object (a never-configured OBS); a blank key is equivalent.
        raw["streamServiceSettings"] = raw.get("streamServiceSettings") or {"key": ""}
        return self.request("SetStreamServiceSettings", raw)

    def set_stream_service_settings(self, server, key):
        return self.request("SetStreamServiceSettings", {
            "streamServiceType": "rtmp_custom",
            "streamServiceSettings": {"server": server, "key": key, "use_auth": False},
        })

    def vendor(self, vendor_name, request_type, request_data=None):
        """Call a plugin's obs-websocket vendor request; returns {"success", "data"|"error"}."""
        res = self.request("CallVendorRequest", {
            "vendorName": vendor_name, "requestType": request_type, "requestData": request_data or {},
        })
        if not res.get("success"):
            return res
        inner = res["data"].get("responseData", {}) or {}
        if inner.get("success") is False:
            return {"success": False, "error": inner.get("error", f"{request_type} failed")}
        return {"success": True, "data": inner}

    def get_stats(self):
        res = self.request("GetStats")
        return res["data"] if res.get("success") else {}
