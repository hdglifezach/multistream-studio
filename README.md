# Multistream Studio for OBS

Go live on **Twitch, YouTube, TikTok LIVE and Kick at the same time** from one OBS dock, with a **Wide 16:9** picture and a
**Tall 9:16** (phone) picture, using real OBS outputs. One installer, sign in with a button, tick which accounts go live.

> Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).
> See the project's [code signing policy](CODE_SIGNING_POLICY.md).

## Install

1. Have **OBS Studio 32 or newer** (Help > Check for updates). Older versions are refused: the plugin needs OBS 32's canvas API.
2. Close OBS and run `MultistreamStudio-Setup.exe` from the latest release.
3. Choose **Installed OBS** (Program Files; Windows asks for admin once, for the OBS plugin folder) or **Portable OBS folder**
   (browse to the folder that holds `bin`, `data` and `portable_mode.txt`; no admin needed if you can write there).
4. Click Install. Setup copies the app to your user profile, adds the OBS plugin, adds the "Multistream Studio" and "Multistream Chat"
   docks, and switches on OBS's built-in WebSocket server (local only, password protected) so the app can control OBS.
5. Open OBS. The plugin starts the app in the background. Docks are under **Docks** if hidden. The **Vertical** dock is the 9:16 canvas:
   pick a scene, press Edit to add sources, drag to move, drag the white corner to resize.
6. In the Multistream Studio dock: **+ Add account** (sign in) or paste a stream key, give each account Wide or Tall and a quality
   (Low / Good / Best), then **Go live**. Flip an account's switch to add or drop it while live. **End stream everywhere** stops all.

Uninstall: Windows Settings > Apps > Multistream Studio. Your accounts stay in `%APPDATA%\MultistreamStudio` in case you reinstall.

## How it works

- Every account gets its own OBS `rtmp_output`. Accounts on the same canvas with the same quality share one GPU encoder, so extra
  platforms cost upload bandwidth, not GPU. Upload is roughly the sum of the accounts' bitrates; the dock shows the estimate.
- OBS reconnects a dropped output by itself (about 1.5 h of retries). If a platform refuses the first connection or OBS gives up, the app
  restarts that one account 15 s -> 30 s -> 60 s apart, up to 20 times ("Trying again in N s"). Wrong keys/addresses are not retried.
- If OBS closes while live, the dock says so and the session ends; if the app restarts while OBS still streams, it picks the session back up.
- **Simple** mode: accounts, Wide/Tall, Low/Good/Best. **Advanced**: exact bitrate, encoder speed, resolution sent, dropped frames,
  "Upload struggling" (congestion), OBS FPS/CPU, app and plugin versions.
- TikTok: sign-in opens a browser window with its own profile; the key comes from TikTok's unofficial LIVE API or LIVE Producer page and
  needs an account allowed to LIVE from streaming software. TikTok can change this at any time.

Privacy: the app talks only to the platforms you sign in to, to OBS on `127.0.0.1`, and to the project's static OAuth redirect pages
(`tools.puppyskyee.com/ms-auth/`). No telemetry. Details in [CODE_SIGNING_POLICY.md](CODE_SIGNING_POLICY.md#privacy).

## Repository layout

| Folder | What |
|---|---|
| `app/` | The Python app: local server + API on `127.0.0.1:18890`, OAuth sign-in, TikTok helper, ffmpeg fallback relay, the dock pages (`web/`) |
| `plugin/` | The OBS plugin (C++, Qt): "Vertical" 1080x1920 canvas dock and the per-account outputs, controlled over obs-websocket vendor requests |
| `installer/` | `setup_app.py` (the setup window, installed + portable OBS) and `build_setup.bat` |
| `tools/` | `prepare_sdk.ps1` (pinned OBS/Qt downloads -> SDK layout), `make_implibs.bat`, `check_version.py` |
| `.github/workflows/build.yml` | The release build (GitHub-hosted Windows runner) with the SignPath steps |

## Build it yourself (Windows)

Needs Visual Studio 2022 Build Tools (C++ x64 + Windows SDK) and Python 3.12.

```bat
python -m pip install -r requirements.txt
powershell -File tools\prepare_sdk.ps1 -Root C:\obs-sdk :: downloads OBS 32.2.2 sources + Windows build and obs-deps Qt 6 (hash-checked); pwsh works too
set OBS_SDK_ROOT=C:\obs-sdk
plugin\build.bat                                        :: -> plugin\build\multistream-studio.dll
installer\build_setup.bat                               :: -> installer\out\MultistreamStudio-Setup.exe
```

`OBS_SDK_ROOT` defaults to `C:\OBS-Plugin-Dev`, the maintainer's layout. The plugin links against OBS's own DLLs through import libraries
made by `tools\make_implibs.bat`, so no libobs build is needed. The app's own platform client IDs live in `app/app_credentials.json`
(private, gitignored); public builds ship the empty `app_credentials.example.json` and load the shared public IDs from
`tools.puppyskyee.com/ms-auth/config.json` at startup.

### Run the app from source

`pip install websocket-client`, then `app\launch.bat` (or `python app\server.py`). To use a portable or test OBS, list its config folder
(the one with `user.ini`) in `app\dev_obs_dirs.txt`, one per line (gitignored), or start the app with `--obs-config-dir <folder>`.
`app\install_into_obs.bat` registers the docks and a start-up script in your OBS for running from source.

## License

GPL-2.0-or-later (see [LICENSE](LICENSE)). The plugin links against libobs, which is GPL-2.0-or-later, so the whole project uses the same
license. Qt is used under the LGPL as shipped by OBS.
