<#
.SYNOPSIS
  Lays out the "SDK root" that plugin\build.bat needs, from pinned downloads (CI) or from an existing local copy.

  Layout produced under -Root:
    src\obs-studio-32.2.2-sources\   OBS Studio 32.2.2 sources (headers)
    deps\qt6\                        Qt 6 from obs-deps windows-deps-qt6-2026-07-15-x64.zip
    obs-bin\bin\64bit\               OBS Studio 32.2.2 Windows x64 release (obs.dll, obs-frontend-api.dll)
    sdk\lib\                         obs.lib / obs-frontend-api.lib made from those DLLs (tools\make_implibs.bat)

.PARAMETER Root        Where to put it (e.g. $env:RUNNER_TEMP\obs-sdk).
.PARAMETER FromLocal   Developer shortcut: copy src\ and deps\qt6 from an existing layout (e.g. C:\OBS-Plugin-Dev) instead of downloading.
.PARAMETER ObsBin      With -FromLocal: a folder holding obs.dll + obs-frontend-api.dll (e.g. an OBS install's bin\64bit).
#>
param(
    [Parameter(Mandatory = $true)] [string] $Root,
    [string] $FromLocal = "",
    [string] $ObsBin = ""
)
$ErrorActionPreference = "Stop"
$ObsVersion = "32.2.2"
$DepsDate = "2026-07-15"
# Pinned downloads (sha256 from the GitHub release pages, 2026-11-21).
$Downloads = @(
    @{ Name = "obs-src.zip"; Url = "https://github.com/obsproject/obs-studio/archive/refs/tags/$ObsVersion.zip"; Sha256 = "" },  # GitHub source archives carry no published hash
    @{ Name = "obs-win.zip"; Url = "https://github.com/obsproject/obs-studio/releases/download/$ObsVersion/OBS-Studio-$ObsVersion-Windows-x64.zip"; Sha256 = "4d6e40e3ab155f56b30de517380566a206d74b63cdf5ad49aa596924768f97e1" },
    @{ Name = "qt6.zip";     Url = "https://github.com/obsproject/obs-deps/releases/download/$DepsDate/windows-deps-qt6-$DepsDate-x64.zip"; Sha256 = "7c7f985711d80467bdc1795b6592275a27d5b0e5a2c7a61db1f2c1d08d6a5579" }
)
$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ToolsDir
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$Root = (Resolve-Path $Root).Path
$Src = Join-Path $Root "src\obs-studio-$ObsVersion-sources"
$Qt = Join-Path $Root "deps\qt6"
$Bin = Join-Path $Root "obs-bin"
$Lib = Join-Path $Root "sdk\lib"

if ($FromLocal) {
    Write-Host "Linking SDK pieces from $FromLocal (directory junctions)"
    New-Item -ItemType Directory -Force -Path (Split-Path $Src), (Split-Path $Qt) | Out-Null
    if (-not (Test-Path $Src)) { New-Item -ItemType Junction -Path $Src -Target (Join-Path $FromLocal "src\obs-studio-$ObsVersion-sources") | Out-Null }
    if (-not (Test-Path $Qt)) { New-Item -ItemType Junction -Path $Qt -Target (Join-Path $FromLocal "deps\qt6") | Out-Null }
    if (-not $ObsBin) { throw "-ObsBin is required with -FromLocal" }
    New-Item -ItemType Directory -Force -Path (Join-Path $Bin "bin\64bit") | Out-Null
    Copy-Item (Join-Path $ObsBin "obs.dll"), (Join-Path $ObsBin "obs-frontend-api.dll") (Join-Path $Bin "bin\64bit")
} else {
    $Dl = Join-Path $Root "downloads"
    New-Item -ItemType Directory -Force -Path $Dl | Out-Null
    foreach ($d in $Downloads) {
        $file = Join-Path $Dl $d.Name
        if (-not (Test-Path $file)) {
            Write-Host "Downloading $($d.Url)"
            Invoke-WebRequest -Uri $d.Url -OutFile $file -UseBasicParsing
        }
        if ($d.Sha256) {
            $got = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
            if ($got -ne $d.Sha256) { throw "SHA-256 mismatch for $($d.Name): got $got, expected $($d.Sha256)" }
            Write-Host "  sha256 ok"
        }
    }
    if (-not (Test-Path $Src)) {
        $tmp = Join-Path $Root "src\_unzip"
        Expand-Archive (Join-Path $Dl "obs-src.zip") $tmp -Force
        Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $Src
        Remove-Item $tmp -Recurse -Force
    }
    if (-not (Test-Path $Qt)) { Expand-Archive (Join-Path $Dl "qt6.zip") $Qt -Force }
    if (-not (Test-Path (Join-Path $Bin "bin\64bit\obs.dll"))) { Expand-Archive (Join-Path $Dl "obs-win.zip") $Bin -Force }
}

# Sanity checks on what build.bat will use
foreach ($must in @((Join-Path $Src "libobs\obs-module.h"), (Join-Path $Src "frontend\api\obs-frontend-api.h"),
                    (Join-Path $Src "plugins\obs-websocket\lib\obs-websocket-api.h"), (Join-Path $Qt "include\QtCore\qconfig.h"),
                    (Join-Path $Qt "lib\Qt6Widgets.lib"), (Join-Path $Bin "bin\64bit\obs.dll"), (Join-Path $Bin "bin\64bit\obs-frontend-api.dll"))) {
    if (-not (Test-Path $must)) { throw "Missing after SDK prep: $must" }
}
$qtVer = (Select-String -Path (Join-Path $Qt "include\QtCore\qconfig.h") -Pattern 'QT_VERSION_STR "([^"]+)"').Matches[0].Groups[1].Value
$obsVer = (Get-Item (Join-Path $Bin "bin\64bit\obs.dll")).VersionInfo.ProductVersion
Write-Host "OBS DLLs: $obsVer   Qt: $qtVer"

# Import libraries from the OBS DLLs (needs dumpbin/lib from MSVC on PATH, or the VS Build Tools default path)
& cmd /c "`"$ToolsDir\make_implibs.bat`" `"$(Join-Path $Bin 'bin\64bit')`" `"$Lib`""
if ($LASTEXITCODE -ne 0) { throw "make_implibs.bat failed" }
foreach ($must in @("obs.lib", "obs-frontend-api.lib")) { if (-not (Test-Path (Join-Path $Lib $must))) { throw "Missing $must" } }
Write-Host "SDK ready at $Root  (set OBS_SDK_ROOT=$Root)"
