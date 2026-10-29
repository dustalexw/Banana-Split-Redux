; Banana Split Redux installer (Inno Setup 6). Built by packaging/build_windows_app.py.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0D0B9C-6B4E-4C59-9B57-3B0E1E6C5A11}
AppName=Banana Split Redux
AppVersion={#AppVersion}
AppVerName=Banana Split Redux {#AppVersion}
AppPublisher=Banana Split Redux
AppPublisherURL=https://github.com/dustalexw/Banana-Split-Redux
AppSupportURL=https://github.com/dustalexw/Banana-Split-Redux/issues
DefaultDirName={autopf}\Banana Split Redux
DefaultGroupName=Banana Split Redux
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\Banana Split Redux.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Banana Split Redux"; Filename: "{app}\Banana Split Redux.exe"
Name: "{autodesktop}\Banana Split Redux"; Filename: "{app}\Banana Split Redux.exe"; Tasks: desktopicon

[Registry]
; "Open with" for video files
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "Banana Split Redux"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\Banana Split Redux.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".mkv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".mp4"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".m4v"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".mov"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".avi"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".ts"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".wmv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\Banana Split Redux.exe\SupportedTypes"; ValueType: string; ValueName: ".webm"; ValueData: ""

[Run]
Filename: "{app}\Banana Split Redux.exe"; Description: "{cm:LaunchProgram,Banana Split Redux}"; Flags: nowait postinstall skipifsilent
