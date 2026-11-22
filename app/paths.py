"""
Where Multistream Studio keeps its files.

- APP_DIR: the shipped files (web UI, app_credentials.json). Inside the packaged
  .exe that's PyInstaller's unpack folder; when run from source it's this folder.
- DATA_DIR: per-user data (config.json with accounts and keys, TikTok browser
  profiles). Packaged: %APPDATA%\\MultistreamStudio. From source: this folder,
  so a developer copy keeps working exactly as before.
"""

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "MultistreamStudio" if FROZEN else Path(__file__).resolve().parent
DATA_DIR.mkdir(parents=True, exist_ok=True)
