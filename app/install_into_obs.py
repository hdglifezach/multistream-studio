"""
Adds Multistream Studio to OBS Studio (run while OBS is closed):
  - two browser docks: "Multistream Studio" and "Multistream Chat"
  - the startup script (obs_multistream_launcher.py) in every scene collection
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OBS_DIR = Path(os.environ["APPDATA"]) / "obs-studio"
# Portable OBS: pass its folder, e.g.  install_into_obs.py C:\OBS-Multistream-Test
if len(sys.argv) > 1:
    OBS_DIR = Path(sys.argv[1]) / "config" / "obs-studio"
SCRIPT = (BASE_DIR / "obs_multistream_launcher.py").as_posix()
DOCKS = [
    {"title": "Multistream Studio", "url": "http://127.0.0.1:18890"},
    {"title": "Multistream Chat", "url": "http://127.0.0.1:18890/chat.html"},
]


def obs_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe"], capture_output=True, text=True).stdout
    return "obs64.exe" in out  # any OBS: running copies rewrite their config on exit


def add_docks():
    ini = OBS_DIR / "user.ini"
    if not ini.exists():
        ini = OBS_DIR / "global.ini"
    raw = ini.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    m = re.search(r"^ExtraBrowserDocks=([^\r\n]*)", text, re.M)
    docks = []
    if m:
        try:
            docks = json.loads(m.group(1).strip())
        except ValueError:
            docks = []
    urls = {d.get("url") for d in docks}
    docks += [d for d in DOCKS if d["url"] not in urls]
    line = "ExtraBrowserDocks=" + json.dumps(docks)
    if m:
        text = text[:m.start()] + line + text[m.end():]
    elif "[BasicWindow]" in text:
        text = text.replace("[BasicWindow]", "[BasicWindow]\r\n" + line, 1)
    else:
        text += "\r\n[BasicWindow]\r\n" + line + "\r\n"
    ini.write_bytes(("\ufeff" if bom else "").encode() + text.encode("utf-8"))
    print(f"Docks registered in {ini}")


def add_script():
    for f in sorted((OBS_DIR / "basic" / "scenes").glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except ValueError:
            continue
        tools = data.setdefault("modules", {}).setdefault("scripts-tool", [])
        if any(s.get("path", "").endswith("obs_multistream_launcher.py") for s in tools):
            continue
        tools.append({"path": SCRIPT, "settings": {}})
        f.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8", newline="\r\n")
        print(f"Startup script added to scene collection {f.stem}")


if __name__ == "__main__":
    if obs_running():
        print("Close OBS first, then run this again (OBS overwrites its settings when it closes).")
        sys.exit(1)
    add_docks()
    add_script()
    print("Done. Open OBS: Docks > Multistream Studio.")
