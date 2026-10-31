@echo off
setlocal
cd /d "%~dp0"
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
if not exist node_modules (call npm ci --no-audit --no-fund || exit /b 1)
call npm run build || exit /b 1
if not exist portable mkdir portable
copy /y "src-tauri\target\release\polystrata-manager.exe" "portable\PolyStrataManager.exe" || exit /b 1
echo Portable build: %CD%\portable\PolyStrataManager.exe
