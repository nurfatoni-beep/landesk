# Installer LanDesk berbasis script (alternatif setup.exe untuk PC dengan Smart App Control).
# Dipanggil oleh Install.cmd / Uninstall.cmd. Install per-user, tidak butuh admin.
#
# Aplikasi dijalankan oleh runtime\pythonw.exe (Python resmi, ditandatangani PSF), jadi tidak
# ada exe tanpa tanda tangan yang bisa diblokir Smart App Control.
param(
    [switch]$Uninstall,
    [switch]$Autostart,
    [switch]$NoDesktop
)
$ErrorActionPreference = "Stop"
$Dest = Join-Path $env:LOCALAPPDATA "Programs\LanDesk"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "LanDesk"
$DesktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "LanDesk.lnk"
$OldDesktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "LanDesk Host.lnk"
$StartupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "LanDesk Host.lnk"
$UninstKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LanDesk"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$Pyw = Join-Path $Dest "runtime\pythonw.exe"
$Script = Join-Path $Dest "app\server.py"
$Icon = Join-Path $Dest "app\icon.ico"

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments = "", [string]$IconFile = "", [string]$WorkDir = "") {
    $dir = Split-Path $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $sh = New-Object -ComObject WScript.Shell
    $l = $sh.CreateShortcut($Path)
    $l.TargetPath = $Target
    $l.Arguments = $Arguments
    $l.WorkingDirectory = if ($WorkDir) { $WorkDir } else { Split-Path $Target }
    $l.IconLocation = if ($IconFile) { "$IconFile,0" } else { "$Target,0" }
    $l.Save()
}

function Stop-LanDesk {
    Get-Process -Name "pythonw", "python", "LanDesk" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Dest, [StringComparison]::OrdinalIgnoreCase) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 700
}

if ($Uninstall) {
    Write-Host "Menghapus LanDesk..." -ForegroundColor Cyan
    Stop-LanDesk
    foreach ($p in @($DesktopLnk, $OldDesktopLnk, $StartupLnk)) { if (Test-Path $p) { Remove-Item $p -Force } }
    if (Test-Path $StartMenu) { Remove-Item $StartMenu -Recurse -Force }
    if (Test-Path $UninstKey) { Remove-Item $UninstKey -Recurse -Force }
    Remove-ItemProperty -Path $RunKey -Name "LanDesk" -ErrorAction SilentlyContinue
    if ($IsAdmin) { netsh advfirewall firewall delete rule name="LanDesk" | Out-Null }
    if (Test-Path $Dest) {
        # Uninstall.cmd mungkin berjalan dari folder ini -> hapus sesudah script selesai
        Start-Process -WindowStyle Hidden cmd.exe -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$Dest`""
    }
    Write-Host "LanDesk sudah dihapus. (Config & password tetap di $env:APPDATA\LanDesk)" -ForegroundColor Green
    exit 0
}

# ---------------- install ----------------
$Src = $PSScriptRoot
if (-not (Test-Path (Join-Path $Src "runtime\pythonw.exe"))) { throw "File aplikasi tidak lengkap di $Src" }
$Version = "1.0.0"
$VerFile = Join-Path $Src "VERSION"
if (Test-Path $VerFile) { $Version = (Get-Content $VerFile -TotalCount 1).Trim() }
Write-Host "Memasang LanDesk $Version ke $Dest" -ForegroundColor Cyan

Stop-LanDesk
if (Test-Path $Dest) {
    # bersihkan versi sebelumnya (termasuk versi lama berbasis PyInstaller)
    foreach ($d in @("runtime", "lib", "app", "_internal")) {
        $p = Join-Path $Dest $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force }
    }
    $old = Join-Path $Dest "LanDesk.exe"
    if (Test-Path $old) { Remove-Item $old -Force }
}
New-Item -ItemType Directory -Force $Dest | Out-Null
Get-ChildItem $Src | Where-Object { $_.Name -ne "Install.cmd" } | Copy-Item -Destination $Dest -Recurse -Force

$Args0 = "`"$Script`""
$WorkDir = Join-Path $Dest "app"
foreach ($old in @("LanDesk - Reset password.lnk", "LanDesk Host.lnk")) {
    $p = Join-Path $StartMenu $old
    if (Test-Path $p) { Remove-Item $p -Force }
}
if (Test-Path $OldDesktopLnk) { Remove-Item $OldDesktopLnk -Force }
New-Shortcut (Join-Path $StartMenu "LanDesk.lnk") $Pyw $Args0 $Icon $WorkDir
New-Shortcut (Join-Path $StartMenu "Uninstall LanDesk.lnk") (Join-Path $Dest "Uninstall.cmd")
if (-not $NoDesktop) { New-Shortcut $DesktopLnk $Pyw $Args0 $Icon $WorkDir }
if (Test-Path $StartupLnk) { Remove-Item $StartupLnk -Force }  # versi lama memakai shortcut Startup
if ($Autostart) {
    # sama dengan checkbox "Jalankan otomatis saat login" di aplikasi
    New-ItemProperty -Path $RunKey -Name "LanDesk" -Value "`"$Pyw`" `"$Script`" --minimized" -PropertyType String -Force | Out-Null
} else {
    # perbarui path kalau autostart sudah aktif dari versi sebelumnya
    $cur = (Get-ItemProperty -Path $RunKey -Name "LanDesk" -ErrorAction SilentlyContinue).LanDesk
    if ($cur) { New-ItemProperty -Path $RunKey -Name "LanDesk" -Value "`"$Pyw`" `"$Script`" --minimized" -PropertyType String -Force | Out-Null }
}

# Muncul di Settings > Apps > Installed apps
New-Item -Force $UninstKey | Out-Null
$size = [int]((Get-ChildItem $Dest -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
$props = @{
    DisplayName = "LanDesk"; DisplayVersion = $Version; Publisher = "LanDesk"
    DisplayIcon = $Icon; InstallLocation = $Dest; EstimatedSize = $size
    UninstallString = "`"$(Join-Path $Dest 'Uninstall.cmd')`""; NoModify = 1; NoRepair = 1
}
foreach ($k in $props.Keys) {
    $type = if ($props[$k] -is [int]) { "DWord" } else { "String" }
    New-ItemProperty -Path $UninstKey -Name $k -Value $props[$k] -PropertyType $type -Force | Out-Null
}

if ($IsAdmin) {
    netsh advfirewall firewall delete rule name="LanDesk" | Out-Null
    netsh advfirewall firewall add rule name="LanDesk" dir=in action=allow program="$Pyw" protocol=TCP localport=8765 profile=private | Out-Null
    netsh advfirewall firewall add rule name="LanDesk" dir=in action=allow program="$Pyw" protocol=UDP localport=8766 profile=private | Out-Null
    Write-Host "Rule firewall 'LanDesk' (jaringan Private, TCP 8765 + UDP 8766) dibuat." -ForegroundColor Green
} else {
    Write-Host "Catatan: tidak dijalankan sebagai admin, jadi firewall tidak diubah." -ForegroundColor Yellow
    Write-Host "         Saat LanDesk pertama jalan, klik 'Allow' pada dialog Windows Firewall." -ForegroundColor Yellow
}
Write-Host "Selesai. Buka 'LanDesk' dari Start Menu atau Desktop." -ForegroundColor Green
