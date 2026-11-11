@echo off
rem Keeps every Claude account mirrored in the background, starting at each Windows sign-in.
rem Undo with: py "%~dp0resync.py" --uninstall
py "%~dp0resync.py" --install
echo.
pause
