; Inno Setup script untuk LanDesk
; Build: lihat build.ps1 (otomatis memanggil ISCC dengan /DAppVersion, /DSrcDir, /DOutDir)
;
; Aplikasi dijalankan oleh runtime\pythonw.exe (Python resmi, ditandatangani PSF):
;   pythonw.exe "{app}\app\server.py"

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SrcDir
  #define SrcDir "..\..\build\stage\LanDesk"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

#define Launcher "{app}\runtime\pythonw.exe"

[Setup]
AppId={{6E4B3B1C-5C2E-4E4B-9F0A-1A7D2C9B8E31}
AppName=LanDesk
AppVersion={#AppVersion}
AppVerName=LanDesk {#AppVersion}
AppPublisher=LanDesk
DefaultDirName={autopf}\LanDesk
DefaultGroupName=LanDesk
DisableProgramGroupPage=yes
OutputDir={#OutDir}
OutputBaseFilename=LanDesk-Setup-{#AppVersion}-win64
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\app\icon.ico
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; boleh juga install "hanya untuk saya" tanpa admin (opsi firewall lalu dilewati)
PrivilegesRequiredOverridesAllowed=dialog commandline
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Buat shortcut di Desktop"; GroupDescription: "Shortcut:"
Name: "autostart"; Description: "Jalankan LanDesk otomatis saat login Windows"; GroupDescription: "Opsi:"; Flags: unchecked
Name: "firewall"; Description: "Izinkan LanDesk di Windows Firewall (jaringan Private: TCP 8765, UDP 8766 untuk pencarian)"; GroupDescription: "Opsi:"; Check: IsAdminInstallMode

[InstallDelete]
Type: files; Name: "{group}\LanDesk Host.lnk"
Type: files; Name: "{autodesktop}\LanDesk Host.lnk"
; sisa versi lama (PyInstaller)
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\LanDesk.exe"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\LanDesk"; Filename: "{#Launcher}"; Parameters: """{app}\app\server.py"""; IconFilename: "{app}\app\icon.ico"; WorkingDir: "{app}\app"; AppUserModelID: "LanDesk.Host"
Name: "{group}\Uninstall LanDesk"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LanDesk"; Filename: "{#Launcher}"; Parameters: """{app}\app\server.py"""; IconFilename: "{app}\app\icon.ico"; WorkingDir: "{app}\app"; AppUserModelID: "LanDesk.Host"; Tasks: desktopicon

[Registry]
; sama dengan checkbox "Jalankan otomatis saat login" di aplikasi
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LanDesk"; ValueData: """{#Launcher}"" ""{app}\app\server.py"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LanDesk"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""LanDesk"" dir=in action=allow program=""{#Launcher}"" protocol=TCP localport=8765 profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""LanDesk"" dir=in action=allow program=""{#Launcher}"" protocol=UDP localport=8766 profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{#Launcher}"; Parameters: """{app}\app\server.py"""; WorkingDir: "{app}\app"; Description: "Jalankan LanDesk sekarang"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{userstartup}\LanDesk Host.lnk"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\lib"
Type: filesandordirs; Name: "{app}\app"

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""Get-Process pythonw,python -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{app}\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v LanDesk /f"; Flags: runhidden; RunOnceId: "DelRun"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LanDesk"""; Flags: runhidden; RunOnceId: "DelFwRule"
