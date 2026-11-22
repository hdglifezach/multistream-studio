@echo off
rem Makes obs.lib and obs-frontend-api.lib (MSVC import libraries) from OBS Studio's own DLLs, so the plugin can link
rem against the exact OBS release it targets without building libobs.
rem   make_implibs.bat <OBS bin\64bit folder containing obs.dll and obs-frontend-api.dll> <output folder>
setlocal
set BIN=%~1
set OUT=%~2
if "%BIN%"=="" (echo usage: make_implibs.bat ^<obs bin\64bit^> ^<out dir^> & exit /b 1)
if "%OUT%"=="" (echo usage: make_implibs.bat ^<obs bin\64bit^> ^<out dir^> & exit /b 1)
where dumpbin.exe >nul 2>nul || call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>nul
where dumpbin.exe >nul 2>nul || (echo dumpbin.exe not found: run from a VS x64 developer prompt & exit /b 1)
if not exist "%OUT%" mkdir "%OUT%"
for %%D in (obs obs-frontend-api) do (
  if not exist "%BIN%\%%D.dll" (echo %BIN%\%%D.dll not found & exit /b 1)
  dumpbin /nologo /exports "%BIN%\%%D.dll" > "%OUT%\%%D.exports.txt" || exit /b 1
  rem Export table rows look like "   12   B 00001234 obs_source_create"; take the name column (works for any header length).
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$names = Get-Content '%OUT%\%%D.exports.txt' | ForEach-Object { if ($_ -match '^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]{8}\s+(\S+)') { $matches[1] } }; if ($names.Count -lt 10) { exit 1 }; Set-Content -Path '%OUT%\%%D.def' -Value (@('LIBRARY %%D', 'EXPORTS') + $names) -Encoding ASCII; Write-Host ('  ' + $names.Count + ' exports from %%D.dll')" || (echo could not parse exports of %%D.dll & exit /b 1)
  lib /nologo /def:"%OUT%\%%D.def" /machine:x64 /out:"%OUT%\%%D.lib" || exit /b 1
)
echo Import libraries written to %OUT%
