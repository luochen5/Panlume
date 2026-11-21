#define AppName "Panlume 盘灯"
#define AppVersion "2.0.0"
#define SourceDir "..\dist\Panlume"

[Setup]
AppId={{B7F89D2E-6C2A-4D8E-9E54-2C7A1D5E0F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion} 学习版
AppPublisher=Panlume Learning Project
DefaultDirName={localappdata}\Programs\Panlume
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Panlume-v{#AppVersion}-setup-windows-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName={#AppName} v{#AppVersion} 学习版
UninstallDisplayIcon={app}\Panlume.exe
SetupIconFile=..\pandeng\desktop\assets\pandeng.ico
VersionInfoVersion=2.0.0.0
VersionInfoDescription=Panlume desktop learning build
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "panlume.portable"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\Panlume 盘灯"; Filename: "{app}\Panlume.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Panlume.exe"
Name: "{autodesktop}\Panlume 盘灯"; Filename: "{app}\Panlume.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Panlume.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Panlume.exe"; Description: "启动 Panlume 盘灯"; Flags: nowait postinstall skipifsilent unchecked

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Panlume');
    if DirExists(DataDir) and (not UninstallSilent) then
    begin
      if MsgBox('是否同时删除本用户的 Panlume 个人数据库、配置、备份和研究记录？' + #13#10 + #13#10 +
                '默认请选择“否”以保留个人数据。选择“是”后无法通过卸载程序恢复。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        if not DelTree(DataDir, True, True, True) then
          MsgBox('部分个人数据未能删除，请稍后手动检查：' + DataDir, mbInformation, MB_OK);
      end;
    end;
  end;
end;
