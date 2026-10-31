@echo off
setlocal
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
if not exist node_modules (call npm install --no-audit --no-fund || exit /b 1)
call npm run dev
