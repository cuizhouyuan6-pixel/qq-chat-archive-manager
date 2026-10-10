#ifndef AppSource
  #define AppSource "..\build\dist\QQArchive"
#endif
#ifndef OutputPath
  #define OutputPath "..\build\installer"
#endif
#ifndef ProductId
  #define ProductId "{{F82D784A-406D-4514-86BB-A9AFE6CA47B2}"
#endif
#ifndef ConfigRoot
  #define ConfigRoot "{userappdata}\QQArchive"
#endif
#ifndef DesktopRoot
  #define DesktopRoot "{autodesktop}"
#endif
#define AdbHash0 GetSHA256OfFile(AppSource + "\tools\platform-tools\adb.exe")
#define AdbHash1 GetSHA256OfFile(AppSource + "\tools\platform-tools\AdbWinApi.dll")
#define AdbHash2 GetSHA256OfFile(AppSource + "\tools\platform-tools\AdbWinUsbApi.dll")
[Setup]
AppId={#ProductId}
AppName=QQ聊天记录管理器
AppVersion=18.1.0
AppPublisher=QQ聊天记录管理器
DefaultDirName={localappdata}\Programs\QQArchive
UsePreviousAppDir=yes
UsePreviousGroup=yes
AppendDefaultDirName=no
DisableDirPage=no
DirExistsWarning=no
DefaultGroupName=QQ聊天记录管理器
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes
DisableWelcomePage=no
OutputDir={#OutputPath}
OutputBaseFilename=QQArchive-v18.1-Setup
SetupIconFile=..\qqarchive\assets\app.ico
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\QQArchive-v18.1.ico
CloseApplications=yes
; ADB can serve other apps. Its files have their own hash and lock checks below.
CloseApplicationsFilterExcludes=adb.exe,AdbWinApi.dll,AdbWinUsbApi.dll
RestartApplications=no
SetupLogging=yes
[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Excludes: "\tools\platform-tools\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AppSource}\tools\platform-tools\adb.exe"; DestDir: "{app}\tools\platform-tools"; Flags: ignoreversion; Check: NeedAdbFile('adb.exe', '{#AdbHash0}')
Source: "{#AppSource}\tools\platform-tools\AdbWinApi.dll"; DestDir: "{app}\tools\platform-tools"; Flags: ignoreversion; Check: NeedAdbFile('AdbWinApi.dll', '{#AdbHash1}')
Source: "{#AppSource}\tools\platform-tools\AdbWinUsbApi.dll"; DestDir: "{app}\tools\platform-tools"; Flags: ignoreversion; Check: NeedAdbFile('AdbWinUsbApi.dll', '{#AdbHash2}')
Source: "{#AppSource}\tools\platform-tools\NOTICE.txt"; DestDir: "{app}\tools\platform-tools"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#AppSource}\tools\platform-tools\source.properties"; DestDir: "{app}\tools\platform-tools"; Flags: ignoreversion skipifsourcedoesntexist
[Icons]
Name: "{group}\QQ聊天记录管理器"; Filename: "{app}\QQArchive.exe"; IconFilename: "{app}\QQArchive-v18.1.ico"
Name: "{#DesktopRoot}\QQ聊天记录管理器"; Filename: "{app}\QQArchive.exe"; IconFilename: "{app}\QQArchive-v18.1.ico"; Check: WantDesktopIcon
[InstallDelete]
Type: files; Name: "{group}\QQ 留档.lnk"
Type: files; Name: "{#DesktopRoot}\QQ 留档.lnk"; Check: WantDesktopIcon
[Run]
Filename: "{app}\QQArchive.exe"; Description: "打开 QQ聊天记录管理器"; Flags: nowait postinstall skipifsilent
[UninstallDelete]
Type: files; Name: "{app}\data-directory.txt"
[Code]
var DataPage: TInputDirWizardPage;
    SavedData: String;
    LegacyDesktopShortcut: Boolean;


{ Compare bytes, not timestamps/version strings: unchanged ADB must not be
  registered with Restart Manager or overwritten while its server is alive. }
function NeedAdbFile(Name, ExpectedHash: String): Boolean;
var Target: String;
begin
  Target := AddBackslash(WizardDirValue)+'tools\platform-tools\'+Name;
  Result := True;
  if not FileExists(Target) then Exit;
  try
    Result := CompareText(GetSHA256OfFile(Target),ExpectedHash) <> 0;
    if not Result then Log('Reusing identical phone component: '+Name);
  except
    Log('Unable to verify phone component: '+Name);
  end;
end;

function OpenComponentForUpdate(Name: String; Access, Share: DWORD;
  Security: THandle; Creation, Attributes: DWORD; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseComponentHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function CheckAdbFile(Name, ExpectedHash: String): String;
var Target: String; Handle: THandle;
begin
  Result := '';
  Target := AddBackslash(WizardDirValue)+'tools\platform-tools\'+Name;
  if not NeedAdbFile(Name,ExpectedHash) then Exit;
  if not FileExists(Target) then Exit;
  { Request write/delete access without changing any file bytes. A running
    image, deny-write/delete handle, or insufficient permissions blocks here,
    before any program file is installed. Never terminate the global ADB server. }
  Handle := OpenComponentForUpdate(Target,$40010000,7,0,3,$80,0);
  if Handle = THandle(-1) then begin
    Result := '手机连接组件需要更新，但文件正在使用或没有写入权限：'+#13#10+
      Target+#13#10+#13#10+
      '本次尚未更新程序文件。请先完成手机传输并退出使用该组件的程序，释放占用后重试；如仍失败，请检查目录写入权限。'+#13#10+
      '安装器不会强制结束 ADB 或 QQ 进程。';
    Log('Phone component update blocked before installation: '+Name);
  end else CloseComponentHandle(Handle);
end;

function CheckPhoneComponents: String;
begin
  Result := CheckAdbFile('adb.exe','{#AdbHash0}');
  if Result <> '' then Exit;
  Result := CheckAdbFile('AdbWinApi.dll','{#AdbHash1}');
  if Result <> '' then Exit;
  Result := CheckAdbFile('AdbWinUsbApi.dll','{#AdbHash2}');
end;

function WantDesktopIcon: Boolean;
begin
  Result := WizardIsTaskSelected('desktopicon') or LegacyDesktopShortcut;
end;

{ Read only named path strings from the existing UTF-8 configuration. Never execute it. }
function JsonPath(Text, Key: String): String;
var P, I, Code: Integer; C: Char; Escaped: Boolean;
begin
  Result := ''; P := Pos('"' + Key + '"', Text);
  if P = 0 then Exit;
  I := P + Length(Key) + 2;
  while (I <= Length(Text)) and (Text[I] <> ':') do I := I + 1;
  I := I + 1;
  while (I <= Length(Text)) and (Text[I] <= ' ') do I := I + 1;
  if (I > Length(Text)) or (Text[I] <> '"') then Exit;
  I := I + 1; Escaped := False;
  while I <= Length(Text) do begin
    C := Text[I];
    if Escaped then begin
      if C = 'u' then begin
        if I + 4 > Length(Text) then begin Result := ''; Exit; end;
        Code := StrToIntDef('$' + Copy(Text,I+1,4),-1);
        if Code < 0 then begin Result := ''; Exit; end;
        Result := Result + Chr(Code); I := I + 4;
      end else if (C = '\') or (C = '/') or (C = '"') then Result := Result + C
      else begin Result := ''; Exit; end;
      Escaped := False;
    end else if C = '\' then Escaped := True
    else if C = '"' then Exit
    else Result := Result + C;
    I := I + 1;
  end;
  Result := '';
end;

function ExistingData: String;
var Lines: TArrayOfString; I: Integer; Text: String;
begin
  Result := '';
  if LoadStringsFromFile(ExpandConstant('{#ConfigRoot}\config.json'),Lines) then begin
    Text := ''; for I := 0 to GetArrayLength(Lines)-1 do Text := Text + Lines[I];
    Result := JsonPath(Text,'pending_data_dir');
    if Result = '' then Result := JsonPath(Text,'data_dir');
  end;
  if Result = '' then begin
    if LoadStringsFromFile(AddBackslash(WizardDirValue)+'data-directory.txt',Lines) then
      if GetArrayLength(Lines)>0 then Result := Trim(Lines[0]);
  end;
  if Result = '' then Result := GetPreviousData('DataDirectory','');
end;

procedure InitializeWizard;
begin
  LegacyDesktopShortcut := FileExists(ExpandConstant('{#DesktopRoot}\QQ 留档.lnk'));
  DataPage := CreateInputDirPage(wpSelectDir, '选择聊天数据保存位置',
    '更新安装自动沿用已有位置',
    '安装不会移动、清空或重复复制聊天数据。已有数据目录会自动保留；需要迁移时请在软件设置中操作。', False, '');
  DataPage.Add('数据文件夹：');
  SavedData := ExistingData;
  if SavedData <> '' then begin
    DataPage.Values[0] := SavedData;
    DataPage.Edits[0].ReadOnly := True; DataPage.Buttons[0].Enabled := False;
  end else DataPage.Values[0] := ExpandConstant('{param:DATA_DIR|{localappdata}\QQArchive\data}');
end;

function DirectoryProblem: String;
var D,A: String;
begin
  Result := '';
  if Trim(DataPage.Values[0]) = '' then begin Result := '请选择数据文件夹。'; Exit; end;
  D := Lowercase(AddBackslash(ExpandFileName(DataPage.Values[0])));
  A := Lowercase(AddBackslash(ExpandFileName(WizardDirValue)));
  { Existing layouts may contain a program subfolder next to data. Do not reject
    that safe layout, and never recursively remove directories during uninstall. }
  if D = A then Result := '程序与数据不能使用完全相同的文件夹。请选择独立的数据子文件夹或其他位置。'
  else if (Pos(A+'_internal\',D)=1) or (Pos(A+'tools\',D)=1) or (Pos(A+'licenses\',D)=1) then
    Result := '数据不能保存在程序依赖文件夹内，请选择其他位置。';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var Problem: String;
begin
  Result := True;
  if CurPageID = DataPage.ID then begin
    Problem := DirectoryProblem;
    if Problem <> '' then begin MsgBox(Problem, mbError, MB_OK); Result := False; end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Silent upgrades must use the same validation as the interactive wizard. }
  Result := DirectoryProblem;
  if Result = '' then Result := CheckPhoneComponents;
  Log('QQArchive program directory: '+WizardDirValue);
  Log('QQArchive data directory: '+DataPage.Values[0]);
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey,'DataDirectory',DataPage.Values[0]);
end;
procedure CurStepChanged(CurStep: TSetupStep);
var Lines: TArrayOfString;
begin
  if CurStep = ssPostInstall then begin
    if not ForceDirectories(DataPage.Values[0]) then
      RaiseException('无法创建聊天数据目录，请检查路径和权限。');
    SetArrayLength(Lines, 1);
    Lines[0] := DataPage.Values[0];
    if not SaveStringsToUTF8File(ExpandConstant('{app}\data-directory.txt'), Lines, False) then
      RaiseException('无法保存聊天数据目录配置。');
  end;
end;

