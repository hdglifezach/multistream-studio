"""
OBS Multistream Studio - OBS startup script
Add this file in OBS under Tools > Scripts. It starts the Multistream Studio
server in the background when OBS opens (if it isn't running already).
The server keeps running after OBS closes, so the dock reconnects instantly.
"""

import os
import socket
import subprocess
import sys

import obspython as obs  # noqa: F401  (only importable inside OBS)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(BASE_DIR, "server.py")
PORT = 18890


def _pythonw():
    candidates = [
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
        os.path.join(sys.prefix, "pythonw.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"),
    ]
    return next((p for p in candidates if os.path.exists(p)), "pythonw.exe")


def _server_running():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def script_description():
    return ("<b>Multistream Studio</b><br>Starts the Multistream Studio server when OBS opens. "
            "Open the dock from Docks &gt; Multistream Studio.")


def script_load(settings):
    if _server_running():
        return
    try:
        subprocess.Popen([_pythonw(), SERVER], cwd=BASE_DIR, creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception as e:
        print(f"[Multistream Studio] Could not start server: {e}")
