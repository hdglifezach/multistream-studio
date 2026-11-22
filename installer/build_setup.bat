@echo off
rem Builds installer\out\MultistreamStudio-Setup.exe from this repo.
rem   1. app exe     (PyInstaller, app\app_main.py -> pkg\dist\MultistreamStudio\)
rem   2. payload     (app.zip + the plugin DLL built by plugin\build.bat + data\locale)
rem   3. setup exe   (PyInstaller onefile, setup_app.py + payload)
rem Env: PYTHON = interpreter with requirements.txt installed (default: python on PATH)
rem      SIGNED_DIR = optional folder holding already code-signed MultistreamStudio.exe and multistream-studio.dll (CI, stage 2):
rem                   they replace the unsigned ones before the payload is zipped.
setlocal
set HERE=%~dp0
set REPO=%HERE%..
if "%PYTHON%"=="" set PYTHON=python
cd /d "%HERE%"
if not exist "%REPO%\plugin\build\multistream-studio.dll" (echo plugin DLL missing: run plugin\build.bat first & exit /b 1)

rem app_credentials.json is private (per build machine, gitignored); public builds ship the empty template and the app
rem reads the shared public client IDs online at startup (see app\server.py REMOTE_APPS_URL).
if not exist "%HERE%pkg" mkdir "%HERE%pkg"
if exist "%REPO%\app\app_credentials.json" (copy /y "%REPO%\app\app_credentials.json" "%HERE%pkg\app_credentials.json" >nul) else (copy /y "%REPO%\app\app_credentials.example.json" "%HERE%pkg\app_credentials.json" >nul)

echo [1/3] App exe
"%PYTHON%" -m PyInstaller --noconfirm --clean --onedir --windowed --name MultistreamStudio ^
  --distpath "%HERE%pkg\dist" --workpath "%HERE%pkg\build" --specpath "%HERE%pkg" --paths "%REPO%\app" ^
  --version-file "%REPO%\app\version_info.txt" ^
  --add-data "%REPO%\app\web;web" --add-data "%HERE%pkg\app_credentials.json;." "%REPO%\app\app_main.py" >nul 2>"%HERE%build_app.log" || (echo app build failed, see build_app.log & exit /b 1)

echo [2/3] Payload
if exist payload rmdir /s /q payload
mkdir payload\plugin\bin\64bit payload\plugin\data\locale
copy /y "%REPO%\plugin\build\multistream-studio.dll" payload\plugin\bin\64bit\ >nul
copy /y "%REPO%\plugin\data\locale\en-US.ini" payload\plugin\data\locale\ >nul
if not "%SIGNED_DIR%"=="" (
  echo      using signed binaries from %SIGNED_DIR%
  copy /y "%SIGNED_DIR%\multistream-studio.dll" payload\plugin\bin\64bit\ >nul || exit /b 1
  copy /y "%SIGNED_DIR%\MultistreamStudio.exe" "%HERE%pkg\dist\MultistreamStudio\" >nul || exit /b 1
)
powershell -NoProfile -Command "Compress-Archive -Path '%HERE%pkg\dist\MultistreamStudio\*' -DestinationPath '%HERE%payload\app.zip' -Force" || exit /b 1

echo [3/3] Setup exe
"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed --name MultistreamStudio-Setup ^
  --distpath "%HERE%out" --workpath "%HERE%build" --specpath "%HERE%build" ^
  --version-file "%REPO%\app\version_info.txt" ^
  --add-data "%HERE%payload;payload" "%HERE%setup_app.py" >nul 2>"%HERE%build_setup.log" || (echo setup build failed, see build_setup.log & exit /b 1)
echo Built %HERE%out\MultistreamStudio-Setup.exe
