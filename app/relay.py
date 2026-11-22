"""
Local multistream relay.

OBS streams once to rtmp://127.0.0.1:19350/live/multistream. An ffmpeg listener
receives it and turns it into MPEG-TS, which this module copies to one ffmpeg
"pusher" per platform. Each pusher forwards the stream (no re-encode) to that
platform, so platforms can be switched on and off while you're live.
Optional per-platform vertical crop re-encodes on the GPU (NVENC) for TikTok.
"""

import queue
import shutil
import subprocess
import threading
import time

RELAY_PORT = 19350
RELAY_SERVER = f"rtmp://127.0.0.1:{RELAY_PORT}/live"
RELAY_KEY = "multistream"
CHUNK = 188 * 128
NO_WINDOW = 0x08000000

def vertical_args(kbps):
    return ["-vf", "crop=ih*9/16:ih,scale=1080:1920",
            "-c:v", "h264_nvenc", "-preset", "p4", "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
            "-g", "120", "-c:a", "copy"]


def ffmpeg_path():
    return shutil.which("ffmpeg") or "ffmpeg"


class Pusher:
    """One platform: reads TS from a queue and pushes it to an RTMP(S) URL."""

    def __init__(self, name, url, vertical=False, bitrate=6000):
        self.name = name
        self.url = url
        self.vertical = vertical
        self.bitrate = bitrate
        self.q = queue.Queue(maxsize=400)  # ~10 MB of buffer before we give up on a slow upload
        self.wanted = True
        self.proc = None
        self.state = "connecting"
        self.error = ""
        self.sent = 0
        self.rate = (time.time(), 0, 0)  # (t, bytes, kbps)
        self.started_at = None
        self.retry = 2
        threading.Thread(target=self._run, daemon=True).start()

    def kbps(self):
        t, b, k = self.rate
        now = time.time()
        if now - t >= 1:
            k = int((self.sent - b) * 8 / 1000 / (now - t))
            self.rate = (now, self.sent, k)
        return k

    def feed(self, chunk):
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            self.error = "Upload too slow for this platform, reconnecting"
            self._kill()

    def stop(self):
        self.wanted = False
        self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def _run(self):
        while self.wanted:
            cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0?", "-map", "0:a:0?"]
            cmd += vertical_args(self.bitrate) if self.vertical else ["-c", "copy"]
            cmd += ["-f", "flv", self.url]
            with self.q.mutex:
                self.q.queue.clear()
            self.state = "connecting"
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            except Exception as e:
                self.state, self.error = "error", f"Could not start ffmpeg: {e}"
                return
            threading.Thread(target=self._read_errors, args=(self.proc,), daemon=True).start()
            started = time.time()
            try:
                while self.wanted and self.proc.poll() is None:
                    try:
                        chunk = self.q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    self.proc.stdin.write(chunk)
                    self.sent += len(chunk)
                    if self.state != "live" and time.time() - started > 3:
                        self.state, self.error, self.retry = "live", "", 2
                        self.started_at = self.started_at or time.time()
            except (BrokenPipeError, OSError):
                pass
            self._kill()
            if not self.wanted:
                break
            self.state = "reconnecting"
            self.started_at = None
            time.sleep(self.retry)
            self.retry = min(self.retry * 2, 30)
        self.state = "off"

    def _read_errors(self, proc):
        for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.error = text[-200:]


class Relay:
    def __init__(self):
        self.listener = None
        self.active = False
        self.pushers = {}
        self.lock = threading.Lock()
        self.last_input = 0.0
        self.received = 0
        self.rate = (time.time(), 0, 0)
        self.error = ""

    # ------------------------------------------------------------------ input side
    def start(self):
        """Begin listening for OBS. Safe to call repeatedly."""
        if self.active:
            return
        self.active = True
        threading.Thread(target=self._listen_loop, daemon=True).start()
        time.sleep(0.6)  # let ffmpeg bind the port before OBS connects

    def stop(self):
        self.active = False
        with self.lock:
            for p in self.pushers.values():
                p.stop()
            self.pushers.clear()
        if self.listener and self.listener.poll() is None:
            self.listener.kill()

    def receiving(self):
        return time.time() - self.last_input < 3

    def input_kbps(self):
        t, b, k = self.rate
        now = time.time()
        if now - t >= 1:
            k = int((self.received - b) * 8 / 1000 / (now - t))
            self.rate = (now, self.received, k)
        return k if self.receiving() else 0

    def _listen_loop(self):
        while self.active:
            cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-listen", "1",
                   "-i", f"{RELAY_SERVER}/{RELAY_KEY}", "-map", "0", "-c", "copy", "-f", "mpegts", "pipe:1"]
            try:
                self.listener = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            except Exception as e:
                self.error = f"Could not start ffmpeg: {e}"
                self.active = False
                return
            threading.Thread(target=self._read_listener_errors, args=(self.listener,), daemon=True).start()
            out = self.listener.stdout
            while True:
                chunk = out.read(CHUNK)
                if not chunk:
                    break
                self.last_input = time.time()
                self.received += len(chunk)
                with self.lock:
                    pushers = list(self.pushers.values())
                for p in pushers:
                    p.feed(chunk)
            self.listener.wait()
            time.sleep(0.3)  # OBS stopped or reconnecting: listen again

    def _read_listener_errors(self, proc):
        for line in proc.stderr:
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.error = text[-200:]

    # ------------------------------------------------------------------ output side
    def add(self, name, url, vertical=False, bitrate=6000):
        with self.lock:
            old = self.pushers.pop(name, None)
            if old:
                old.stop()
            self.pushers[name] = Pusher(name, url, vertical, bitrate)

    def remove(self, name):
        with self.lock:
            p = self.pushers.pop(name, None)
        if p:
            p.stop()

    def status(self, name):
        p = self.pushers.get(name)
        if not p:
            return {"state": "off", "kbps": 0, "error": "", "uptime": 0}
        state = p.state
        if state in ("connecting", "live") and not self.receiving():
            state = "waiting"  # platform is on, but OBS isn't sending yet
        return {
            "state": state,
            "kbps": p.kbps() if state == "live" else 0,
            "error": p.error if state != "live" else "",
            "uptime": int(time.time() - p.started_at) if p.started_at and state == "live" else 0,
        }
