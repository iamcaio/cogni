; ===========================================================================
;  Cogni - Script de instalacao (Inno Setup 6)
; ===========================================================================
;  Requisito unico: Inno Setup 6, gratuito, em https://jrsoftware.org/isdl.php
;
;  Compilar:  iscc cogni_installer.iss
;  Resultado: dist_installer\COGNI-Setup-<versao>.exe
;
;  NAO edite a versao aqui a mao. O build.ps1 reescreve o #define
;  MyAppVersion abaixo a partir do parametro -Version, junto com cogni.py e
;  version_info.txt. Editar em um lugar so e como as tres versoes passam a
;  discordar.
;
;  O que este instalador resolve, e que o pyinstaller sozinho nao resolve:
;    - duplo clique instala; nao ha pasta solta nem "qual .exe eu abro?"
;    - atalho no Menu Iniciar e (opcional) na Area de Trabalho
;    - entra em "Aplicativos instalados" do Windows, com desinstalador
;    - /SILENT permite que o proprio app se atualize sem perguntar nada
;    - PrivilegesRequired=lowest: instala por usuario, SEM pedir admin
;    - cria C:\COGNI\db e C:\COGNI\docs na instalacao
; ===========================================================================

#define MyAppName        "Cogni"
#define MyAppVersion     "4.6.9"
#define MyAppPublisher   "COGNI"
#define MyAppExeName     "COGNI.exe"

[Setup]
; GUID fixo e unico deste app. NAO reutilize o do NEXUS: o Windows usaria a
; mesma entrada em "Aplicativos instalados" e um instalador desinstalaria o
; outro.
AppId={{7B2E9F41-3C68-5A94-9D17-4E8A6C2B1F03}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}

; Instala em %LOCALAPPDATA%\Programs\Cogni -> nao exige administrador.
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; Sem OverridesAllowed: o update silencioso nunca pede elevacao (UAC) e nao falha
; se o usuario negar. Instala sempre por usuario.

OutputDir=C:\Dev\cogni\dist_installer
OutputBaseFilename=COGNI-Setup-{#MyAppVersion}
SetupIconFile=cogni.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

; Fecha o Cogni durante um update silencioso. A REABERTURA e feita pelo [Run]
; (RestartApplications=no): o app se encerra sozinho antes do instalador agir,
; entao o Restart Manager nao tem o que reiniciar - era por isso que o Cogni
; fechava e nao voltava. Mantido em "no" para nunca abrir duas vezes.
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter=*.exe

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Area de Trabalho"; GroupDescription: "Atalhos:"; Flags: checkedonce

[Dirs]
; Pastas de dados do usuario. Criadas na instalacao para o app nao ter de
; lidar com "pasta nao existe" no primeiro boot.
;
; uninsneveruninstall: o desinstalador NAO apaga estas pastas. Desinstalar o
; app nao deve destruir as anotacoes e os documentos do usuario.
Name: "C:\COGNI\db";   Flags: uninsneveruninstall
Name: "C:\COGNI\docs"; Flags: uninsneveruninstall

[Files]
; O PyInstaller --onedir gera dist\COGNI\ com o exe e o _internal.
Source: "dist\COGNI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID precisa ser IDENTICO a constante APP_AUMID do cogni.py e ao
; AUMID usado no toast do WindowsNotifier - sem isso a notificacao sai
; assinada como "Windows PowerShell" em vez de "Cogni" (mesma causa raiz ja
; resolvida no NEXUS: ver nexus_installer.iss).
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "iamcaio.Cogni"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "iamcaio.Cogni"; Tasks: desktopicon

[Run]
; Instalacao com assistente: caixa "Abrir o Cogni agora".
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir o {#MyAppName} agora"; Flags: nowait postinstall skipifsilent
; Update automatico (/SILENT): reabre o Cogni sozinho ao terminar.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: WizardSilent

[Code]
function WebView2Instalado: Boolean;
var
  pv, CH: String;
begin
  Result := False;
  CH := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  if RegQueryStringValue(HKLM32, CH, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then Result := True
  else if RegQueryStringValue(HKLM64, CH, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then Result := True
  else if RegQueryStringValue(HKCU, CH, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0') then Result := True;
end;

procedure InstalarWebView2;
var
  arq: String;
  rc: Integer;
begin
  if WebView2Instalado then Exit;
  try
    DownloadTemporaryFile('https://go.microsoft.com/fwlink/p/?LinkId=2124703', 'MicrosoftEdgeWebview2Setup.exe', '', nil);
    arq := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
    Exec(arq, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, rc);
  except
  end;
  if (not WebView2Instalado) and (not WizardSilent) then
    MsgBox('O Cogni precisa do Microsoft Edge WebView2 Runtime e nao foi possivel instala-lo automaticamente.' + #13#10 +
           'Instale em: https://go.microsoft.com/fwlink/p/?LinkId=2124703', mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then InstalarWebView2;
end;

[UninstallDelete]
; Limpa apenas cache. Os DADOS em C:\COGNI\db e C:\COGNI\docs NAO sao
; apagados, de proposito.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
Type: filesandordirs; Name: "C:\COGNI\ui"
Type: filesandordirs; Name: "{localappdata}\COGNI\ui"
