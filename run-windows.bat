@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)

if not exist ".venv\Scripts\python.exe" (
  echo [LanDesk] Membuat virtualenv...
  %PY% -m venv .venv || (echo Python 3 belum terpasang. Install dari https://www.python.org & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
)

rem Izinkan port di Windows Firewall (sekali, butuh admin; diabaikan kalau gagal)
netsh advfirewall firewall show rule name="LanDesk" >nul 2>nul || netsh advfirewall firewall add rule name="LanDesk" dir=in action=allow protocol=TCP localport=8765 profile=private >nul 2>nul

".venv\Scripts\python.exe" server.py %*
pause
