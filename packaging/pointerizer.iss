; Inno Setup script for Pointerizer.
; To ship a new version: bump MyAppVersion below, then run build.ps1.

#define MyAppVersion "1.0.0"

[Setup]
; AppId must never change — it's how Windows knows a new installer is an
; upgrade of the same app rather than a second install.
AppId={{a8f7b76b-02d3-4676-ac7c-0878eea6498c}
AppName=Pointerizer
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\Pointerizer
DefaultGroupName=Pointerizer
UninstallDisplayIcon={app}\Pointerizer.exe
; per-user install: no admin prompt, and the app folder stays writable
; (recordings live next to the exe)
PrivilegesRequired=lowest
; paths are relative to this .iss (packaging\), so reach up to the repo root with ..\
OutputDir=..\dist
OutputBaseFilename=PointerizerSetup
SetupIconFile=..\assets\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: desktopicon; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\Pointerizer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Pointerizer"; Filename: "{app}\Pointerizer.exe"
Name: "{autodesktop}\Pointerizer"; Filename: "{app}\Pointerizer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Pointerizer.exe"; Description: "Launch Pointerizer"; Flags: nowait postinstall skipifsilent
