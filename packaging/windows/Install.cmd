@echo off
setlocal
cd /d "%~dp0"
echo ==============================================
echo   LanDesk - installer
echo   (klik kanan ^> Run as administrator supaya
echo    rule firewall ikut dibuat otomatis)
echo ==============================================
echo.
set OPTS=
choice /c YN /n /m "Jalankan LanDesk otomatis saat login Windows? [Y/N] "
if errorlevel 2 goto skipauto
set OPTS=-Autostart
:skipauto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %OPTS%
echo.
choice /c YN /n /m "Jalankan LanDesk sekarang? [Y/N] "
if errorlevel 2 goto end
start "" "%LOCALAPPDATA%\Programs\LanDesk\runtime\pythonw.exe" "%LOCALAPPDATA%\Programs\LanDesk\app\server.py"
:end
pause
