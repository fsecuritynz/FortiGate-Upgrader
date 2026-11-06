; Inno Setup script: FortiGate-Upgrader-<version>-Windows-x64-Setup.exe
; Built by packaging\build_windows.ps1 (or the release workflow) after PyInstaller:
;   iscc /DAppVersion=0.11.0 /DAppBuild=18 packaging\installer.iss
; Installs per user (no administrator rights), so the in-app updater can run it silently:
;   Setup.exe /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppBuild
  #define AppBuild "0"
#endif

[Setup]
AppId={{6E2D5B1C-6B0F-4C8E-9C1E-3F6A2B7D9E41}
AppName=FortiGate Upgrader
AppVersion={#AppVersion}
AppVerName=FortiGate Upgrader v{#AppVersion} build{#AppBuild}
AppPublisher=fsecuritynz
AppPublisherURL=https://github.com/fsecuritynz/FortiGate-Upgrader
AppUpdatesURL=https://github.com/fsecuritynz/FortiGate-Upgrader/releases
VersionInfoVersion={#AppVersion}.{#AppBuild}
DefaultDirName={autopf}\FortiGate Upgrader
DefaultGroupName=FortiGate Upgrader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=FortiGate-Upgrader-{#AppVersion}-Windows-x64-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\FortiGate Upgrader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\FortiGate Upgrader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; replace the whole app on upgrade so files removed in a new version don't linger
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\FortiGate Upgrader"; Filename: "{app}\FortiGate Upgrader.exe"
Name: "{autodesktop}\FortiGate Upgrader"; Filename: "{app}\FortiGate Upgrader.exe"; Tasks: desktopicon

[Run]
; also runs after a silent in-app update, which reopens the app
Filename: "{app}\FortiGate Upgrader.exe"; Description: "{cm:LaunchProgram,FortiGate Upgrader}"; Flags: nowait postinstall
