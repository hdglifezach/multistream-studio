"""
Multistream Studio setup (becomes MultistreamStudio-Setup.exe).

Installs, for the current Windows user:
  - the app             -> %LOCALAPPDATA%\\Programs\\MultistreamStudio\\   (always per-user, no admin)
  - the OBS plugin      -> installed OBS:  %ProgramData%\\obs-studio\\plugins\\multistream-studio\\   (needs admin)
                           portable OBS:   <obs>\\obs-plugins\\64bit + <obs>\\data\\obs-plugins\\multistream-studio\\
  - two OBS docks       -> "Multistream Studio" and "Multistream Chat" in OBS's user.ini
                           (installed: %APPDATA%\\obs-studio, portable: <obs>\\config\\obs-studio)
  - OBS's WebSocket server switched on (local only; the app needs it to talk to OBS)
  - an Uninstall entry  -> Windows Settings > Apps
Admin rights are asked for only when a target folder actually needs them (ProgramData, Program Files).
Command line: --uninstall | --installed | --portable <obs folder> | --go (start right away; used by the elevated relaunch).
"""

import ctypes
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog
import winreg
import zipfile
from pathlib import Path

APP_NAME = "Multistream Studio"
VERSION = "1.1.0"
MIN_OBS_MAJOR = 32  # the plugin uses the OBS 32 canvas API; on older OBS the DLL won't load at all
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "payload"
APP_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "MultistreamStudio"
DATA_DIR = Path(os.environ["APPDATA"]) / "MultistreamStudio"  # the app's own data (accounts); never deleted by uninstall
INSTALLED_PLUGIN_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "obs-studio" / "plugins" / "multistream-studio"
INSTALLED_CONFIG_DIR = Path(os.environ["APPDATA"]) / "obs-studio"
DOCKS = [
    {"title": "Multistream Studio", "url": "http://127.0.0.1:18890"},
    {"title": "Multistream Chat", "url": "http://127.0.0.1:18890/chat.html"},
]
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MultistreamStudio"
PORTABLE_MARKERS = ("portable_mode.txt", "obs_portable_mode.txt", "obs_portable_mode")
NO_WINDOW = 0x08000000


# ---------------------------------------------------------------------- OBS discovery
def file_version(path):
    """(major, minor, build, rev) from a Windows exe/dll, or None."""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ver.GetFileVersionInfoW(str(path), 0, size, buf)
        ptr = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        ms = ctypes.c_uint.from_address(ptr.value + 8).value  # VS_FIXEDFILEINFO.dwFileVersionMS
        ls = ctypes.c_uint.from_address(ptr.value + 12).value
        return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return None


def installed_obs_exe():
    """obs64.exe of the installed (non-portable) OBS, or None."""
    roots = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\OBS Studio") as k:
                roots.append(winreg.QueryValueEx(k, "")[0])
        except OSError:
            pass
    roots += [os.environ.get("ProgramFiles", r"C:\Program Files") + r"\obs-studio"]
    for r in roots:
        p = Path(r) / "bin" / "64bit" / "obs64.exe"
        if p.exists():
            return p
    return None


def portable_root(folder):
    """Normalise a user-picked folder (root, bin, or bin\\64bit) to the OBS root, or None if it isn't an OBS folder."""
    p = Path(folder).resolve()
    for cand in (p, p.parent, p.parent.parent):
        if (cand / "bin" / "64bit" / "obs64.exe").exists():
            return cand
    return None


def is_portable(root):
    return any((Path(root) / m).exists() for m in PORTABLE_MARKERS)


def version_ok(exe):
    """'' when fine, otherwise the message to show. Unknown version = allowed with a note."""
    v = file_version(exe)
    if v and v[0] < MIN_OBS_MAJOR:
        return (f"That is OBS Studio {v[0]}.{v[1]}. Multistream Studio needs OBS {MIN_OBS_MAJOR} or newer: "
                "update OBS (Help > Check for updates), then run this setup again.")
    return ""


def writable(folder):
    """Can this user write there without admin? Side-effect free: probes the deepest folder that already exists
    (creating a missing folder there needs the same right as writing a file in it)."""
    folder = Path(folder)
    while not folder.exists() and folder.parent != folder:
        folder = folder.parent
    try:
        probe = folder / f".ms-write-test-{os.getpid()}"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Target:
    """Where OBS lives and therefore where the plugin, docks and WebSocket config go."""

    def __init__(self, kind, root=None):
        self.kind = kind  # "installed" | "portable"
        self.root = Path(root) if root else None

    @classmethod
    def installed(cls):
        return cls("installed")

    @classmethod
    def portable(cls, folder):
        root = portable_root(folder)
        if not root:
            raise ValueError("That folder doesn't contain OBS (no bin\\64bit\\obs64.exe).")
        if not is_portable(root):
            raise ValueError("That OBS isn't in portable mode (no portable_mode.txt next to its bin folder). "
                             "Pick a portable OBS, or choose Installed OBS.")
        msg = version_ok(root / "bin" / "64bit" / "obs64.exe")
        if msg:
            raise ValueError(msg)
        return cls("portable", root)

    @property
    def exe(self):
        return self.root / "bin" / "64bit" / "obs64.exe" if self.root else installed_obs_exe()

    @property
    def plugin_bin_dir(self):  # where multistream-studio.dll goes
        return self.root / "obs-plugins" / "64bit" if self.root else INSTALLED_PLUGIN_DIR / "bin" / "64bit"

    @property
    def plugin_data_dir(self):  # where data\locale goes
        return self.root / "data" / "obs-plugins" / "multistream-studio" if self.root else INSTALLED_PLUGIN_DIR / "data"

    @property
    def config_dir(self):  # OBS's config folder (user.ini, plugin_config\...)
        return self.root / "config" / "obs-studio" if self.root else INSTALLED_CONFIG_DIR

    def needs_admin(self):
        if self.kind == "installed":
            return not writable(INSTALLED_PLUGIN_DIR.parent)
        return not (writable(self.plugin_bin_dir) and writable(self.config_dir))

    def describe(self):
        exe = self.exe
        if not exe:
            return "OBS Studio not found in Program Files. Install OBS 32 or newer first, or pick a portable OBS folder."
        v = file_version(exe)
        ver = f"{v[0]}.{v[1]}.{v[2]}" if v else "unknown version"
        where = f"portable, {self.root}" if self.root else str(exe.parent.parent.parent)
        return f"OBS Studio {ver} ({where})"

    def check(self):
        """Raise with a friendly message if this target can't be installed to."""
        exe = self.exe
        if self.kind == "installed":
            if not exe:
                return "OBS Studio wasn't found in the usual place; the plugin needs OBS 32 or newer."
            msg = version_ok(exe)
            if msg:
                raise RuntimeError(msg)
        return ""


# ---------------------------------------------------------------------- pieces
def obs_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return "obs64.exe" in out


def stop_app():
    subprocess.run(["taskkill", "/IM", "MultistreamStudio.exe", "/F"], capture_output=True, creationflags=NO_WINDOW)


def edit_docks(config_dir, add):
    """Add or remove our browser docks in OBS's user.ini (global.ini on older OBS). OBS must be closed.
    A never-launched OBS has no config yet: we create user.ini with just our key; OBS keeps existing keys on first run."""
    config_dir = Path(config_dir)
    ini = config_dir / "user.ini"
    if not ini.exists() and (config_dir / "global.ini").exists():
        ini = config_dir / "global.ini"
    if not ini.exists():
        if not add:
            return
        config_dir.mkdir(parents=True, exist_ok=True)
        ini = config_dir / "user.ini"
        ini.write_text("[BasicWindow]\r\n", encoding="utf-8-sig")
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
    ours = {d["url"] for d in DOCKS}
    docks = [d for d in docks if d.get("url") not in ours]
    if add:
        docks += DOCKS
    line = "ExtraBrowserDocks=" + json.dumps(docks)
    if m:
        text = text[:m.start()] + line + text[m.end():]
    elif "[BasicWindow]" in text:
        text = text.replace("[BasicWindow]", "[BasicWindow]\r\n" + line, 1)
    else:
        text += "\r\n[BasicWindow]\r\n" + line + "\r\n"
    ini.write_bytes(("\ufeff" if bom else "").encode() + text.encode("utf-8"))


def enable_websocket(config_dir):
    """Switch on OBS's built-in WebSocket server (off by default) so the app can talk to OBS.
    Keeps an existing port/password; otherwise generates a password. OBS must be closed."""
    cfg_path = Path(config_dir) / "plugin_config" / "obs-websocket" / "config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except ValueError:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
    if cfg.get("server_enabled") is True and cfg.get("server_password"):
        return  # already on: leave the user's setup alone
    cfg.setdefault("server_port", 4455)
    cfg.setdefault("alerts_enabled", False)
    cfg["auth_required"] = True
    if not cfg.get("server_password"):
        cfg["server_password"] = secrets.token_urlsafe(18)
    cfg["server_enabled"] = True
    cfg["first_load"] = False
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


OBS_DIRS_FILE = DATA_DIR / "obs_dirs.json"


def remember_obs_config_dir(config_dir, add):
    """Tell the app where a portable OBS keeps its config (the app reads the WebSocket password from there).
    The plugin also passes this when it launches the app; this file covers starting the app by hand."""
    dirs = []
    try:
        dirs = json.loads(OBS_DIRS_FILE.read_text(encoding="utf-8")).get("obs_config_dirs", [])
    except Exception:
        pass
    dirs = [d for d in dirs if Path(d) != Path(config_dir)]
    if add:
        dirs.append(str(config_dir))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OBS_DIRS_FILE.write_text(json.dumps({"obs_config_dirs": dirs}, indent=2), encoding="utf-8")


def register_uninstall(target):
    setup_copy = APP_DIR / "MultistreamStudio-Setup.exe"
    if getattr(sys, "frozen", False):
        shutil.copy2(sys.executable, setup_copy)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "Mistlight Studios")
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, str(APP_DIR / "MultistreamStudio.exe"))
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(APP_DIR))
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'"{setup_copy}" --uninstall')
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "InstallMode", 0, winreg.REG_SZ, target.kind)
        winreg.SetValueEx(k, "PortableRoot", 0, winreg.REG_SZ, str(target.root or ""))


def recorded_target():
    """The target the last install used (from the uninstall registry entry); installed OBS if nothing is recorded."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            mode = winreg.QueryValueEx(k, "InstallMode")[0]
            root = winreg.QueryValueEx(k, "PortableRoot")[0]
        if mode == "portable" and root:
            return Target("portable", root)
    except OSError:
        pass
    return Target.installed()


def install(target, say):
    say("Checking OBS…")
    note = target.check()
    say("Stopping any running copy…")
    stop_app()
    say("Copying the app…")
    if APP_DIR.exists():
        for item in APP_DIR.iterdir():
            if item.name != "MultistreamStudio-Setup.exe":
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BUNDLE / "app.zip") as z:
        z.extractall(APP_DIR)
    say("Adding the OBS plugin…")
    target.plugin_bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUNDLE / "plugin" / "bin" / "64bit" / "multistream-studio.dll", target.plugin_bin_dir / "multistream-studio.dll")
    if target.plugin_data_dir.exists():
        shutil.rmtree(target.plugin_data_dir)
    shutil.copytree(BUNDLE / "plugin" / "data", target.plugin_data_dir)
    say("Adding the docks to OBS…")
    edit_docks(target.config_dir, add=True)
    say("Switching on OBS's WebSocket server…")
    enable_websocket(target.config_dir)
    if target.kind == "portable":
        remember_obs_config_dir(target.config_dir, add=True)
    register_uninstall(target)
    return note


def uninstall(say, target=None):
    target = target or recorded_target()
    say("Stopping the app…")
    stop_app()
    say("Removing the OBS plugin…")
    (target.plugin_bin_dir / "multistream-studio.dll").unlink(missing_ok=True)
    shutil.rmtree(target.plugin_data_dir, ignore_errors=True)
    if target.kind == "installed":
        shutil.rmtree(INSTALLED_PLUGIN_DIR, ignore_errors=True)
    say("Removing the docks from OBS…")
    edit_docks(target.config_dir, add=False)
    if target.kind == "portable":
        remember_obs_config_dir(target.config_dir, add=False)
    say("Removing the app…")
    for item in APP_DIR.iterdir() if APP_DIR.exists() else []:
        if item.name != "MultistreamStudio-Setup.exe":  # can't delete ourselves while running
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    # Your accounts in %APPDATA%\MultistreamStudio are kept, so reinstalling remembers them.
    # OBS's WebSocket server is left on (it's harmless and other tools may use it).


def relaunch_elevated(args):
    """Run this setup again as admin with the given arguments; returns True if Windows accepted."""
    joined = " ".join(f'"{a}"' for a in args)
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, joined
    else:
        exe, params = sys.executable, f'"{__file__}" {joined}'
    try:
        return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)) > 32
    except Exception:
        return False


# ---------------------------------------------------------------------- window
class Window:
    BG, PANEL, TEXT, MUTED, ACCENT, RED, GREEN = "#0e1014", "#161920", "#eceef3", "#8d94a5", "#7c5cff", "#fca5a5", "#86efac"

    def __init__(self, uninstalling, preset_kind="installed", preset_root="", go=False):
        self.uninstalling = uninstalling
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {'Uninstall' if uninstalling else 'Setup'}")
        self.root.configure(bg=self.BG)
        self.root.geometry("520x430" if not uninstalling else "460x300")
        self.root.resizable(False, False)
        tk.Label(self.root, text=APP_NAME, bg=self.BG, fg=self.TEXT, font=("Segoe UI Semibold", 18)).pack(pady=(22, 4))
        if uninstalling:
            intro = "This removes Multistream Studio and its OBS plugin and docks.\nYour signed-in accounts are kept in case you reinstall."
        else:
            intro = ("Go live on Twitch, YouTube, TikTok and Kick at once, with a wide and a tall (phone) picture, inside OBS.\n\n"
                     "Setup will: install the app for your user, add the OBS plugin, add two docks to OBS,\n"
                     "and switch on OBS's WebSocket server (local to this PC). Close OBS first.")
        tk.Label(self.root, text=intro, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9), justify="center").pack()

        self.kind = tk.StringVar(value=preset_kind)
        self.portable_path = tk.StringVar(value=preset_root)
        if not uninstalling:
            box = tk.Frame(self.root, bg=self.PANEL, padx=12, pady=8)
            box.pack(pady=(14, 6), padx=22, fill="x")
            opt = dict(bg=self.PANEL, fg=self.TEXT, selectcolor=self.BG, activebackground=self.PANEL, activeforeground=self.TEXT,
                       font=("Segoe UI", 10), anchor="w", command=self.refresh)
            tk.Radiobutton(box, text="Installed OBS Studio (Program Files)", variable=self.kind, value="installed", **opt).pack(fill="x")
            self.installed_info = tk.Label(box, text="", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9), anchor="w", padx=22)
            self.installed_info.pack(fill="x")
            tk.Radiobutton(box, text="Portable OBS folder", variable=self.kind, value="portable", **opt).pack(fill="x", pady=(6, 0))
            row = tk.Frame(box, bg=self.PANEL)
            row.pack(fill="x", padx=(22, 0))
            tk.Entry(row, textvariable=self.portable_path, bg=self.BG, fg=self.TEXT, insertbackground=self.TEXT, relief="flat",
                     font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True, ipady=3)
            tk.Button(row, text="Browse…", command=self.browse, bg=self.BG, fg=self.TEXT, relief="flat", font=("Segoe UI", 9),
                      padx=8).pack(side="left", padx=(6, 0))
            self.portable_info = tk.Label(box, text="", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9), anchor="w", padx=22,
                                          wraplength=430, justify="left")
            self.portable_info.pack(fill="x")
            self.portable_path.trace_add("write", lambda *_: self.refresh())

        self.status = tk.Label(self.root, text="", bg=self.BG, fg=self.TEXT, font=("Segoe UI", 10), wraplength=460, justify="center")
        self.status.pack(pady=(10, 8))
        self.button = tk.Button(self.root, text="Uninstall" if uninstalling else "Install", command=self.go,
                                bg=self.ACCENT, fg="white", activebackground="#9b82ff", activeforeground="white",
                                relief="flat", font=("Segoe UI Semibold", 12), padx=36, pady=8, cursor="hand2")
        self.button.pack()
        if not uninstalling:
            self.refresh()
        if go:
            self.root.after(300, self.go)

    def refresh(self):
        inst = Target.installed()
        self.installed_info.config(text=inst.describe())
        p = self.portable_path.get().strip()
        if not p:
            self.portable_info.config(text="Pick the folder that holds OBS's bin, data and portable_mode.txt.", fg=self.MUTED)
        else:
            try:
                t = Target.portable(p)
                self.portable_info.config(text=t.describe() + ("" if not t.needs_admin() else "  (needs admin to write there)"), fg=self.MUTED)
            except ValueError as e:
                self.portable_info.config(text=str(e), fg=self.RED)

    def browse(self):
        d = filedialog.askdirectory(title="Choose your portable OBS folder")
        if d:
            self.kind.set("portable")
            self.portable_path.set(d.replace("/", "\\"))

    def say(self, text, color=None):
        self.root.after(0, lambda: self.status.config(text=text, fg=color or self.TEXT))

    def current_target(self):
        if self.uninstalling:
            return recorded_target()
        if self.kind.get() == "portable":
            return Target.portable(self.portable_path.get().strip())
        return Target.installed()

    def go(self):
        if obs_running():
            self.say("Please close OBS first, then click the button again.", self.RED)
            return
        try:
            target = self.current_target()
        except ValueError as e:
            self.say(str(e), self.RED)
            return
        if target.needs_admin() and not is_admin():
            args = ["--uninstall"] if self.uninstalling else (["--portable", str(target.root)] if target.root else ["--installed"])
            if relaunch_elevated(args + ["--go"]):
                self.root.destroy()
                return
            self.say("Windows didn't allow admin rights, so Setup can't write to that folder.", self.RED)
            return
        self.button.config(state="disabled")
        threading.Thread(target=self.work, args=(target,), daemon=True).start()

    def work(self, target):
        try:
            if self.uninstalling:
                uninstall(self.say, target)
                self.say("Removed. You can close this window.", self.GREEN)
            else:
                note = install(target, self.say)
                where = f"your portable OBS in {target.root}" if target.root else "OBS"
                self.say(f"Done! Open {where}: you'll find the \"Multistream Studio\" dock and the \"Vertical\" dock.\n"
                         "If they're hidden, turn them on from OBS's Docks menu." + (f"\n\nNote: {note}" if note else ""), self.GREEN)
            self.root.after(0, lambda: self.button.config(text="Close", state="normal", command=self.root.destroy))
        except Exception as e:
            self.say(f"Something went wrong: {e}", self.RED)
            self.root.after(0, lambda: self.button.config(state="normal"))

    def run(self):
        self.root.mainloop()


def parse_args(argv):
    kind, root, go = "installed", "", "--go" in argv
    if "--portable" in argv:
        kind = "portable"
        i = argv.index("--portable")
        root = argv[i + 1] if i + 1 < len(argv) else ""
    return kind, root, go


if __name__ == "__main__":
    kind, root, go = parse_args(sys.argv[1:])
    Window("--uninstall" in sys.argv, kind, root, go).run()
