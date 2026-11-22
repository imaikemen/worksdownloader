; Inno Setup 6 Script for 作品鉴赏downloader V1.0.1
; Supports both GitHub Actions CI and local builds via -DExeSource=...

#define MyAppName "作品鉴赏downloader"
#define MyAppVersion "1.0.1"
#define MyAppExeName "作品鉴赏downloader-V1.0.1.exe"
#define MyAppPublisher "WorksAppreciationDownloader Open Source"
#ifndef ExeSource
  #define ExeSource "..\dist\作品鉴赏downloader-V1.0.1.exe"
#endif
#ifndef OutputDirPath
  #define OutputDirPath "..\dist\installer"
#endif

[Setup]
AppId={{8E7C4E5A-9D42-4B7C-A9E4-10104E6D8A01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} V{#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\LICENSE
OutputDir={#OutputDirPath}
OutputBaseFilename=作品鉴赏downloader-V{#MyAppVersion}-Setup
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Uninstallable=yes
UninstallDisplayName={#MyAppName} V{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式选项:"
Name: "startmenuicon"; Description: "创建开始菜单快捷方式（含卸载入口）"; GroupDescription: "快捷方式选项:"

[Files]
Source: "{#ExeSource}"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\USAGE.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}\{#MyAppName} V{#MyAppVersion}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenuicon
Name: "{autoprograms}\{#MyAppName}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName} V{#MyAppVersion}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\WorksAppreciationDownloader');
    if DirExists(DataDir) and (not UninstallSilent) then
    begin
      if MsgBox(
        '是否同时清除本地设置、常看作者列表与下载历史缓存？' + #13#10 +
        '（目录：' + DataDir + '）' + #13#10 + #13#10 +
        '提示：您已经下载保存到本地的作品文件不会被删除。',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2
      ) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
