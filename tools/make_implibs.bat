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
  dumpbin /nologo /exports "%BIN%\%%D.dll" > "%OUT%\%%D.exports.txt"
  echo LIBRARY %%D> "%OUT%\%%D.def"
  echo EXPORTS>> "%OUT%\%%D.def"
  rem dumpbin's export table: skip the header, take the 4th column (the symbol name)
  for /f "skip=19 tokens=4" %%F in ('type "%OUT%\%%D.exports.txt"') do @echo %%F>> "%OUT%\%%D.def"
  lib /nologo /def:"%OUT%\%%D.def" /machine:x64 /out:"%OUT%\%%D.lib" || exit /b 1
)
echo Import libraries written to %OUT%
