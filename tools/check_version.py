"""Fails if the version isn't the same everywhere (SignPath requires one product version per build).
Checked: VERSION, app/server.py, installer/setup_app.py, plugin/src/plugin.cpp, plugin/src/plugin.rc, app/version_info.txt."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
want = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
checks = {
    "app/server.py": r'APP_VERSION = "([^"]+)"',
    "installer/setup_app.py": r'VERSION = "([^"]+)"',
    "plugin/src/plugin.cpp": r'PLUGIN_VERSION = "([^"]+)"',
    "plugin/src/plugin.rc": r'"ProductVersion",\s*"([^"]+)"',
    "app/version_info.txt": r"StringStruct\('ProductVersion',\s*'([^']+)'\)",
}
bad = []
for rel, pattern in checks.items():
    text = (ROOT / rel).read_text(encoding="utf-8")
    found = re.findall(pattern, text)
    if not found or any(v != want for v in found):
        bad.append(f"{rel}: {found or 'not found'}")
# the numeric 4-part forms too
rc = (ROOT / "plugin/src/plugin.rc").read_text(encoding="utf-8")
vi = (ROOT / "app/version_info.txt").read_text(encoding="utf-8")
tuple4 = tuple(int(x) for x in (want.split(".") + ["0", "0"])[:4])
commas = ",".join(map(str, tuple4))
if not re.search(rf"^FILEVERSION\s+{commas}\s*$", rc, re.M) or not re.search(rf"^PRODUCTVERSION\s+{commas}\s*$", rc, re.M):
    bad.append("plugin/src/plugin.rc: FILEVERSION/PRODUCTVERSION tuple")
vi_flat = vi.replace(" ", "")
if f"filevers=({commas})" not in vi_flat or f"prodvers=({commas})" not in vi_flat:
    bad.append("app/version_info.txt: filevers/prodvers tuple")
if bad:
    print(f"Version mismatch (VERSION says {want}):")
    for b in bad:
        print("  " + b)
    sys.exit(1)
print(f"version {want} is consistent everywhere")
