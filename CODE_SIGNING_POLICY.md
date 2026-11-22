# Code Signing Policy

Multistream Studio publishes its Windows release files (the setup, the app and the OBS plugin DLL) from this public
repository. This page is the project's **code signing policy** as required by the
[SignPath Foundation](https://signpath.org/terms) open-source program.

> Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org)

## What is signed

- `MultistreamStudio-Setup.exe` (the installer), `MultistreamStudio.exe` (the app) and `multistream-studio.dll` (the OBS plugin),
  all for Windows x64. Every signed file carries `ProductName = Multistream Studio` and the release `ProductVersion` (see `VERSION`).
- Only binaries built by this repository's GitHub Actions workflow ([`.github/workflows/build.yml`](.github/workflows/build.yml)) from
  a commit on `main` or a `v*` tag are ever submitted for signing. Nothing built on a developer PC, and no third-party binary, is
  signed with the project certificate.

## How a release is built and signed

1. GitHub Actions (GitHub-hosted `windows-latest` runner only; no self-hosted runners) checks out the commit, downloads the pinned
   OBS Studio 32.2.2 sources and Windows build and obs-deps Qt 6 (SHA-256 checked), builds the plugin DLL with MSVC, the app exe with
   PyInstaller from `requirements.txt`, and the setup exe.
2. The unsigned app exe and DLL are uploaded as a workflow artifact and submitted to SignPath. After an approver approves the request,
   the signed files come back, the setup exe is rebuilt with them and submitted as a second signing request.
3. The signed setup and DLL are attached to the GitHub release together with their SHA-256 sums.

A signature therefore means: this file is the automated build of the source code at the noted commit of this repository, including its
build scripts and workflow.

## Team and roles

- **Authors (committers):** the repository owner (`<github-owner>`). Changes from anyone else arrive as pull requests.
- **Reviewers:** the repository owner reviews and merges every pull request before it can reach `main`.
- **Approvers:** the repository owner approves every signing request in SignPath.

Everyone with write access to this repository or to the SignPath organization must use multi-factor authentication. Signing access is
removed as soon as a person no longer needs it.

## Privacy

This program will not transfer any information to other networked systems unless specifically requested by the user or the person
installing or operating it. Concretely, Multistream Studio talks only to:

- the streaming platforms the user chooses to sign in to or stream to (Twitch, YouTube, Kick, TikTok), using the user's own account;
- `127.0.0.1` (OBS's WebSocket server and the app's own local dock pages; nothing is exposed to the network);
- `tools.puppyskyee.com/ms-auth/`, the project's static OAuth redirect ("bounce") pages and the public list of platform client IDs
  (`config.json`), plus the Kick token-exchange helper, which only forwards the one-time sign-in code to Kick. No account data,
  stream keys or usage data are sent there, and nothing is stored there.

There is no telemetry, crash reporting or update check. Stream keys and sign-in tokens stay in `%APPDATA%\MultistreamStudio\config.json`
on the user's PC.

## System changes and uninstallation

The setup says what it will do before you click Install: it copies the app to `%LOCALAPPDATA%\Programs\MultistreamStudio`, adds the OBS
plugin (to `%ProgramData%\obs-studio\plugins` for an installed OBS, or into the chosen portable OBS folder), adds two docks to OBS's
`user.ini`, and switches on OBS's built-in WebSocket server (loopback only, password protected). Administrator rights are requested
only when the target folder needs them. **Uninstall** is available from Windows Settings > Apps (or `MultistreamStudio-Setup.exe --uninstall`)
and removes everything except the user's accounts file.

## Reporting a problem with a signed file

Open an issue on this repository, or email the repository owner (see the GitHub profile). Suspected malware or a compromised build is
treated as a security incident: the release is pulled and SignPath is informed.
