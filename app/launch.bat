@echo off
rem Developer: run the app from source (needs Python 3.12+ with `pip install websocket-client`) and open the dock in a browser.
cd /d "%~dp0"
where pythonw.exe >nul 2>nul && (start "" pythonw.exe server.py) || (start "" python.exe server.py)
timeout /t 2 >nul
start http://127.0.0.1:18890
