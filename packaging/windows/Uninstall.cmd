@echo off
copy /y "%~dp0install.ps1" "%TEMP%\landesk-uninstall.ps1" >nul
cd /d "%TEMP%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\landesk-uninstall.ps1" -Uninstall & del "%TEMP%\landesk-uninstall.ps1" & pause & exit
