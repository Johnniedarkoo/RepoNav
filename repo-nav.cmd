@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0repo-nav.ps1" %*
exit /b %errorlevel%
