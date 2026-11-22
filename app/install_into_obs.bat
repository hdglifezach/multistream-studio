@echo off
rem Developer: register the docks + the start-up script in your OBS (close OBS first). Pass a portable OBS folder as %1 if you use one.
cd /d "%~dp0"
python install_into_obs.py %*
pause
