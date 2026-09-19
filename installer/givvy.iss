; Whatnot Givvy installer (Inno Setup 6). Built by installer/build.py:
;   ISCC /DAppVer=1.2.3 /DSrc=..\dist\WhatnotGivvy /DOut=..\dist installer\givvy.iss
;
; Why Inno Setup and not the old self-made installer: that one was a PyInstaller
; "one-file" exe (a program that unpacks itself into TEMP and runs from there), which
; is exactly the shape Windows Defender's heuristics flag as a trojan. It also had no
; entry in Settings > Apps. Inno Setup is a standard installer: per-user install, a
; real uninstall entry, in-place upgrades, no self-extraction.
;
; It only copies the program. The Android emulator is downloaded from Google by the
; app itself on first run (Settings > Emulator check), because those files may not be
; redistributed.

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef Src
  #define Src "..\dist\WhatnotGivvy"
#endif
#ifndef Out
  #define Out "..\dist"
#endif
#ifndef AppIdSuffix
  #define AppIdSuffix ""
#endif

[Setup]
AppId={{6F2B5C1E-7A43-4E0B-9D5B-5C1F0B7E9A21}{#AppIdSuffix}
AppName=Whatnot Givvy{#AppIdSuffix}
AppVersion={#AppVer}
AppVerName=Whatnot Givvy{#AppIdSuffix} {#AppVer}
AppPublisher=Whatnot Givvy
DefaultDirName={localappdata}\Programs\WhatnotGivvy{#AppIdSuffix}
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#Out}
OutputBaseFilename=WhatnotGivvySetup
SetupIconFile=..\givvy\assets\givvy.ico
UninstallDisplayIcon={app}\WhatnotGivvy.exe
UninstallDisplayName=Whatnot Givvy{#AppIdSuffix}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; No AppMutex: in silent mode Inno aborts at once if it sees it, without waiting, which breaks self-update.
; Restart Manager closes a running copy instead (the app exits cleanly on WM_CLOSE).
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#AppVer}
VersionInfoProductName=Whatnot Givvy
VersionInfoDescription=Whatnot Givvy Setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; AppUserModelID must match givvy/winshell.py so the taskbar button belongs to this shortcut
Name: "{userprograms}\Whatnot Givvy{#AppIdSuffix}"; Filename: "{app}\WhatnotGivvy.exe"; WorkingDir: "{app}"; AppUserModelID: "Givvy.WhatnotGivvy.App"
Name: "{userdesktop}\Whatnot Givvy{#AppIdSuffix}"; Filename: "{app}\WhatnotGivvy.exe"; WorkingDir: "{app}"; AppUserModelID: "Givvy.WhatnotGivvy.App"; Tasks: desktopicon

[Run]
Filename: "{app}\WhatnotGivvy.exe"; WorkingDir: "{app}"; Description: "Open Whatnot Givvy now (it sets up the emulator on first run)"; Flags: nowait postinstall skipifsilent
; the in-app updater runs Setup silently with /RELAUNCH=1 and wants the app back afterwards
Filename: "{app}\WhatnotGivvy.exe"; WorkingDir: "{app}"; Flags: nowait; Check: RelaunchWanted

[UninstallDelete]
; settings, history, logs and screenshots are created at run time, so Inno does not know them
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
  params: String;
begin
  { Before the program files go: ask about the emulators and the Android SDK, which live outside the
    program folder. A silent uninstall must never stop to ask, so it removes nothing out there. }
  if CurUninstallStep = usUninstall then
  begin
    if UninstallSilent then params := '--uninstall-cleanup --silent' else params := '--uninstall-cleanup';
    Exec(ExpandConstant('{app}\WhatnotGivvy.exe'), params, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, rc);
  end;
end;

function RelaunchWanted: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
