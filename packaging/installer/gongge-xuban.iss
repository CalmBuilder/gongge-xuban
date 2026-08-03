; packaging/installer/gongge-xuban.iss — Inno Setup 脚本（产物为 Gongge-Xuban）
; 由 build_windows.ps1 调用：ISCC.exe packaging\installer\gongge-xuban.iss
; VERSION 通过环境变量传入（GetEnv）

[Setup]
AppId=cn.gongge.xuban.desktop
AppName=共格·序伴
AppVersion={#GetEnv('VERSION')}
AppVerName=共格·序伴 {#GetEnv('VERSION')}
AppPublisher=共格·序伴
DefaultDirName={autopf}\Gongge-Xuban
DefaultGroupName=Gongge-Xuban
OutputDir=..\out
OutputBaseFilename=Gongge-Xuban-setup
SetupIconFile=..\assets\gongge-xuban.ico
UninstallDisplayIcon={app}\gongge-xuban.exe
UninstallDisplayName=共格·序伴
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64os
PrivilegesRequired=lowest
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=no
DisableReadyPage=no
VersionInfoVersion={#GetEnv('WINDOWS_VERSION_INFO_VERSION')}
VersionInfoProductName=共格·序伴
VersionInfoProductVersion={#GetEnv('WINDOWS_VERSION_INFO_VERSION')}
VersionInfoCompany=共格·序伴
VersionInfoDescription=共格·序伴 Installer
#if GetEnv('WINDOWS_SIGN_ENABLED') == '1'
SignTool=gongge-xuban
SignedUninstaller=yes
#endif

[Files]
; PyInstaller onedir 产物整体安装
Source: "..\out\gongge-xuban\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Registry]
Root: HKCU; Subkey: "Software\Classes\gongge-xuban"; ValueType: string; ValueData: "URL:Gongge-Xuban Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\gongge-xuban"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\gongge-xuban\DefaultIcon"; ValueType: string; ValueData: "{app}\gongge-xuban.exe,0"
Root: HKCU; Subkey: "Software\Classes\gongge-xuban\shell\open\command"; ValueType: string; ValueData: """{app}\gongge-xuban.exe"" ""%1"""

[Icons]
Name: "{group}\共格·序伴"; Filename: "{app}\gongge-xuban.exe"; AppUserModelID: "cn.gongge.xuban.desktop"
Name: "{autodesktop}\共格·序伴"; Filename: "{app}\gongge-xuban.exe"; AppUserModelID: "cn.gongge.xuban.desktop"

[Run]
Filename: "{app}\gongge-xuban.exe"; Description: "启动 共格·序伴"; Flags: postinstall nowait skipifsilent
