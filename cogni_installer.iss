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
#define MyAppVersion     "4.5.7"
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
PrivilegesRequiredOverridesAllowed=dialog

OutputDir=dist_installer
OutputBaseFilename=COGNI-Setup-{#MyAppVersion}
SetupIconFile=cogni.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

; Fecha o Cogni automaticamente durante um update silencioso e o reabre.
CloseApplications=yes
RestartApplications=yes
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
; skipifsilent: num update automatico o app e reaberto pelo RestartApplications,
; nao por aqui. Sem isso o Cogni poderia abrir duas vezes.
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir o {#MyAppName} agora"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Limpa apenas cache. Os DADOS em C:\COGNI\db e C:\COGNI\docs NAO sao
; apagados, de proposito.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
