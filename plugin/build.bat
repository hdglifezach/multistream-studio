@echo off
rem Builds build\multistream-studio.dll with MSVC (no CMake needed).
rem
rem Needs an "SDK root" laid out like tools\prepare_sdk.ps1 makes it (CI) or like a local dev box:
rem   %OBS_SDK_ROOT%\src\obs-studio-32.2.2-sources   OBS sources (headers only are used)
rem   %OBS_SDK_ROOT%\deps\qt6                        Qt 6 from obs-deps (windows-deps-qt6-<date>-x64.zip)
rem   %OBS_SDK_ROOT%\sdk\lib\obs.lib, obs-frontend-api.lib   import libs made from OBS's DLLs (tools\make_implibs.bat)
rem OBS_SDK_ROOT defaults to C:\OBS-Plugin-Dev (the owner's dev layout). obsconfig.h comes from this repo (sdk\include).
rem If cl.exe isn't on PATH yet, the VS 2022 Build Tools vcvars64.bat is tried.
setlocal
if "%OBS_SDK_ROOT%"=="" set OBS_SDK_ROOT=C:\OBS-Plugin-Dev
set R=%OBS_SDK_ROOT%
set S=%R%\src\obs-studio-32.2.2-sources
set Q=%R%\deps\qt6
set HERE=%~dp0
cd /d "%HERE%"
where cl.exe >nul 2>nul || call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>nul
where cl.exe >nul 2>nul || (echo cl.exe not found: run from a VS x64 developer prompt & exit /b 1)
if not exist "%S%\libobs\obs-module.h" (echo OBS sources not found at %S% & exit /b 1)
if not exist "%Q%\include\QtCore" (echo Qt6 not found at %Q% & exit /b 1)
if not exist "%R%\sdk\lib\obs.lib" (echo obs.lib not found in %R%\sdk\lib (run tools\make_implibs.bat) & exit /b 1)
if not exist build mkdir build
rc /nologo /fo "build\plugin.res" src\plugin.rc || exit /b 1
cl /nologo /std:c++17 /EHsc /MD /O2 /W3 /utf-8 /Zc:__cplusplus /permissive- /DUNICODE /D_UNICODE /DNOMINMAX /DWIN32_LEAN_AND_MEAN /DQT_NO_KEYWORDS ^
 /I"%HERE%sdk\include" /I"%S%\libobs" /I"%S%\frontend\api" /I"%S%\plugins\obs-websocket\lib" ^
 /I"%Q%\include" /I"%Q%\include\QtCore" /I"%Q%\include\QtGui" /I"%Q%\include\QtWidgets" ^
 /Fo"build\\" src\plugin.cpp /LD /Fe"build\multistream-studio.dll" ^
 /link /LIBPATH:"%R%\sdk\lib" /LIBPATH:"%Q%\lib" obs.lib obs-frontend-api.lib Qt6Core.lib Qt6Gui.lib Qt6Widgets.lib "build\plugin.res"
if errorlevel 1 exit /b 1
echo Built build\multistream-studio.dll
