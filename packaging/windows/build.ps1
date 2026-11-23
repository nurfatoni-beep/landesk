# Build LanDesk untuk Windows: folder aplikasi + installer (Inno Setup) + zip portable
#
# Kenapa tidak pakai PyInstaller? Exe buatan PyInstaller punya hash unik tiap build dan tidak
# ditandatangani, sehingga Smart App Control kadang memblokirnya. Di sini aplikasi dijalankan
# oleh python.exe/pythonw.exe RESMI (ditandatangani Python Software Foundation) yang dibundel
# bersama library dari PyPI, jadi tidak ada exe "asing" yang perlu dinilai Windows.
#
# Layout hasil:
#   LanDesk\runtime\   python.exe, pythonw.exe, DLLs, Lib (stdlib), tcl  (salinan Python resmi)
#   LanDesk\lib\       dependency (pip install --target)
#   LanDesk\app\       server.py, gui.py, static\, icon.ico
#
# Kebutuhan: Python 3.10+ dari python.org (64-bit, dengan tcl/tk) dan Inno Setup 6
# Pakai:     powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
param(
    [string]$Python = "",
    [string]$WorkDir = "$env:TEMP\landesk-build"
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\.."
$Pkg = Resolve-Path "$PSScriptRoot\.."

$Version = (Select-String -Path "$Root\server.py" -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "== LanDesk $Version ==" -ForegroundColor Cyan

if (-not $Python) {
    $cand = @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "py", "python")
    foreach ($c in $cand) { if (Get-Command $c -ErrorAction SilentlyContinue) { $Python = $c; break } }
}
$PyHome = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
$PyTag = (& $Python -c "import sys; print(f'{sys.version_info[0]}{sys.version_info[1]}')").Trim()
if (-not (Test-Path "$PyHome\pythonw.exe") -or -not (Test-Path "$PyHome\tcl")) {
    throw "Butuh Python resmi dari python.org (dengan pythonw.exe dan tcl/tk). Ditemukan: $PyHome"
}
Write-Host "Python runtime: $PyHome (python$PyTag)"

# --- 1. stage bersih
$Stage = "$WorkDir\stage\LanDesk"
if (Test-Path "$WorkDir\stage") { Remove-Item "$WorkDir\stage" -Recurse -Force }
New-Item -ItemType Directory -Force "$Stage\runtime", "$Stage\lib", "$Stage\app" | Out-Null

# --- 2. runtime Python resmi (tanpa test, idle, pip, dll)
Get-ChildItem $PyHome -File | Where-Object { $_.Name -match '^(python.*\.(exe|dll)|vcruntime.*\.dll|LICENSE\.txt)$' } |
    Copy-Item -Destination "$Stage\runtime"
robocopy "$PyHome\DLLs" "$Stage\runtime\DLLs" /E /NFL /NDL /NJH /NJS /NP /XF "_test*.pyd" "*.pdb" | Out-Null
robocopy "$PyHome\tcl" "$Stage\runtime\tcl" /E /NFL /NDL /NJH /NJS /NP /XD "demos" "tix*" | Out-Null
robocopy "$PyHome\Lib" "$Stage\runtime\Lib" /E /NFL /NDL /NJH /NJS /NP `
    /XD "site-packages" "test" "tests" "idlelib" "ensurepip" "lib2to3" "turtledemo" "__pycache__" "venv" "pydoc_data" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy gagal ($LASTEXITCODE)" }
# ._pth: mode terisolasi, path relatif ke folder runtime (tidak terpengaruh Python lain di PC)
"Lib`r`nDLLs`r`n..\lib`r`n..\app`r`n" | Set-Content -Encoding ASCII "$Stage\runtime\python$PyTag._pth"

# --- 3. dependency dari PyPI (wheel resmi). simplejpeg dilewati: DLL-nya sering diblokir Smart App Control
$Req = "$WorkDir\requirements-win.txt"
Get-Content "$Root\requirements.txt" | Where-Object { $_ -notmatch '^simplejpeg' } | Set-Content -Encoding ASCII $Req
& $Python -m pip install -q --disable-pip-version-check --no-compile --only-binary=:all: `
    --target "$Stage\lib" -r $Req
if ($LASTEXITCODE) { throw "pip install gagal" }
Get-ChildItem "$Stage\lib" -Directory -Filter "bin" | Remove-Item -Recurse -Force

# --- 4. aplikasi
& "$Stage\runtime\python.exe" "$Pkg\make_icon.py" "$Pkg" | Out-Null   # runtime stage sudah punya Pillow
if ($LASTEXITCODE) { throw "make_icon gagal" }
Copy-Item "$Root\server.py", "$Root\gui.py", "$Root\dashboard.py", "$Root\remote.py", "$Root\clipboard.py", `
    "$Root\updater.py", "$Root\requirements.txt", "$Pkg\icon.ico" "$Stage\app"
Copy-Item "$Root\static" "$Stage\app\static" -Recurse
Copy-Item "$Root\README.md" $Stage
[IO.File]::WriteAllText("$Stage\VERSION", "$Version`r`n", [Text.Encoding]::ASCII)

# --- 5. pre-compile .pyc (pakai runtime hasil stage sekaligus sebagai uji)
& "$Stage\runtime\python.exe" -m compileall -q -j 0 "$Stage\runtime\Lib" "$Stage\lib" "$Stage\app" | Out-Null
$v = & "$Stage\runtime\python.exe" "$Stage\app\server.py" --version
if ($v -notmatch $Version) { throw "Smoke test gagal: '$v'" }
& "$Stage\runtime\python.exe" -c "import tkinter, numpy, PIL, aiohttp, mss, pynput, pystray, dxcam, server, gui, dashboard, remote, clipboard, updater; from PIL import ImageTk; print('import ok')"
if ($LASTEXITCODE) { throw "Smoke test import gagal" }

# --- 6. installer (Inno Setup)
$Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
New-Item -ItemType Directory -Force "$Root\dist" | Out-Null
if ($Iscc) {
    & $Iscc /Qp "/DAppVersion=$Version" "/DSrcDir=$Stage" "/DOutDir=$Root\dist" "$PSScriptRoot\landesk.iss"
    if ($LASTEXITCODE) { throw "ISCC gagal" }
} else {
    Write-Warning "Inno Setup 6 tidak ditemukan, setup.exe dilewati (winget install JRSoftware.InnoSetup)"
}

# --- 7. zip portable + installer berbasis script (untuk PC dengan Smart App Control)
foreach ($f in @("Install.cmd", "Uninstall.cmd", "install.ps1")) {
    # paksa CRLF: file .cmd ber-LF bisa membuat goto/label di cmd.exe kacau
    $txt = [IO.File]::ReadAllText("$PSScriptRoot\$f") -replace "`r?`n", "`r`n"
    [IO.File]::WriteAllText("$Stage\$f", $txt, [Text.Encoding]::ASCII)
}
$Zip = "$Root\dist\LanDesk-$Version-win64-portable.zip"
if (Test-Path $Zip) { Remove-Item $Zip }
Compress-Archive -Path $Stage -DestinationPath $Zip -CompressionLevel Optimal
Write-Host "Selesai. Hasil di $Root\dist" -ForegroundColor Green
Get-ChildItem "$Root\dist" -File | Format-Table Name, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
