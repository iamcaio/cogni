# ===========================================================================
#  Cogni - Build de release em UM comando
# ===========================================================================
#  Uso:   .\build.ps1 -Version 4.5.1
#         .\build.ps1 -Version 4.5.1 -SkipInstaller     (so o .exe)
#         .\build.ps1 -Version 4.5.1 -OnlyInstaller     (reaproveita dist\)
#         .\build.ps1 -Version 4.5.1 -OnlyVersion       (so sincroniza versao)
#
#  O que faz, na ordem:
#    1. Grava a versao em cogni.py, version_info.txt e cogni_installer.iss
#    2. pyinstaller --onedir a partir de cogni.spec
#    3. Compila o instalador com Inno Setup
#    4. Calcula o SHA-256 do instalador
#    5. Imprime o JSON do manifesto PRONTO para colar no Firebase
# ===========================================================================
#
#  NOTAS DE ROBUSTEZ (aprendidas na pratica, neste projeto)
#
#  1. GRAVACAO IDEMPOTENTE. Se o arquivo ja esta na versao pedida, ele NAO e
#     reescrito. Isso elimina a causa mais comum de "file is being used by
#     another process": reescrever arquivo que nao precisava mudar.
#
#  2. SEM BOM. `Set-Content -Encoding UTF8` no PowerShell 5.1 escreve um BOM
#     UTF-8 no inicio do arquivo, o que pode quebrar o parser de recursos do
#     PyInstaller. Aqui usamos WriteAllText com UTF8Encoding($false).
#
#  3. GRAVACAO ATOMICA COM RETRY. Escreve num .tmp e substitui. Se o arquivo
#     estiver travado (antivirus, editor aberto), tenta de novo com espera
#     crescente antes de desistir.
#
#  4. VALIDACAO ANTES DE GRAVAR. Se um regex nao casar, o script para ANTES de
#     escrever qualquer coisa. Falha no meio da sincronizacao de versao deixa
#     tres arquivos discordando entre si, que e o pior resultado possivel.
#
#  5. ESTE ARQUIVO E ASCII PURO. O Windows PowerShell 5.1 le .ps1 sem BOM como
#     Windows-1252; um travessao UTF-8 viraria tres caracteres, e o ultimo e
#     uma aspa tipografica que o PowerShell ACEITA como delimitador de string.
#     Cada acento viraria uma aspa fantasma e o parser morreria numa linha sem
#     defeito nenhum. Nao adicione acento aqui.
# ===========================================================================

param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [switch]$SkipInstaller,
    [switch]$OnlyVersion,
    [switch]$OnlyInstaller,
    [string]$BaseUrl = "https://github.com/iamcaio/cogni-dist/releases/download"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Etapa($n, $txt) { Write-Host "`n[$n] $txt" -ForegroundColor Cyan }
function Detalhe($txt)   { Write-Host "    $txt" -ForegroundColor DarkGray }
function Aviso($txt)     { Write-Host "    $txt" -ForegroundColor Yellow }

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Versao deve ser X.Y.Z (ex: 4.5.1). Recebido: '$Version'"
}
if ($OnlyInstaller -and $SkipInstaller) {
    throw "-OnlyInstaller e -SkipInstaller se contradizem."
}
if ($OnlyVersion -and ($OnlyInstaller -or $SkipInstaller)) {
    throw "-OnlyVersion nao compila nada, entao nao combina com os outros."
}

# ===========================================================================
#  Utilitarios de arquivo
# ===========================================================================

$Utf8SemBom = New-Object System.Text.UTF8Encoding($false)

function Read-TextFile([string]$Caminho) {
    if (-not (Test-Path -LiteralPath $Caminho)) {
        throw "Arquivo nao encontrado: $Caminho"
    }
    return [System.IO.File]::ReadAllText(
        (Resolve-Path -LiteralPath $Caminho), $Utf8SemBom)
}

function Get-QuemTrava([string]$Caminho) {
    # Nao existe API simples em PowerShell para descobrir qual processo tem um
    # handle aberto. Em vez de fingir precisao, listamos os suspeitos que
    # REALMENTE estao rodando agora. Isso e acionavel; "arquivo em uso" nao e.
    $suspeitos = @{
        'Code'          = 'VS Code'
        'notepad'       = 'Bloco de Notas'
        'notepad++'     = 'Notepad++'
        'sublime_text'  = 'Sublime Text'
        'pycharm64'     = 'PyCharm'
        'devenv'        = 'Visual Studio'
        'Claude'        = 'Claude / Cowork (visualizador de arquivos)'
        'OneDrive'      = 'OneDrive (sincronizacao)'
        'Dropbox'       = 'Dropbox (sincronizacao)'
        'GoogleDriveFS' = 'Google Drive (sincronizacao)'
        'MsMpEng'       = 'Windows Defender (varredura em tempo real)'
        'SearchIndexer' = 'Indexador de Busca do Windows'
        'python'        = 'Python (processo antigo ainda vivo?)'
        'COGNI'         = 'COGNI.exe (o proprio app aberto)'
    }
    $achados = @()
    foreach ($nome in $suspeitos.Keys) {
        if (Get-Process -Name $nome -ErrorAction SilentlyContinue) {
            $achados += "$($suspeitos[$nome])  [$nome.exe]"
        }
    }
    return $achados
}

function Write-TextFileIfChanged {
    param(
        [Parameter(Mandatory)][string]$Caminho,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Conteudo,
        [int]$Tentativas = 6
    )

    $completo = (Resolve-Path -LiteralPath $Caminho).Path

    if ((Read-TextFile $completo) -ceq $Conteudo) {
        Detalhe "$(Split-Path $completo -Leaf): ja esta correto, nao reescrito"
        return $false
    }

    $tmp = "$completo.tmp"
    $espera = 250

    for ($i = 1; $i -le $Tentativas; $i++) {
        try {
            [System.IO.File]::WriteAllText($tmp, $Conteudo, $Utf8SemBom)
            Move-Item -LiteralPath $tmp -Destination $completo -Force
            Detalhe "$(Split-Path $completo -Leaf): atualizado"
            return $true
        }
        catch {
            # Catch generico de proposito. Com $ErrorActionPreference = "Stop",
            # o PowerShell embrulha IOException em ActionPreferenceStopException
            # e um catch tipado NAO capturaria.
            if (Test-Path -LiteralPath $tmp) {
                Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            }
            if ($i -eq $Tentativas) {
                Write-Host ""
                Write-Host "  ARQUIVO TRAVADO: $completo" -ForegroundColor Red
                Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
                Write-Host ""
                $quem = Get-QuemTrava $completo
                if ($quem.Count -gt 0) {
                    Write-Host "  Processos em execucao que costumam causar isto:" -ForegroundColor Yellow
                    foreach ($q in $quem) { Write-Host "    - $q" -ForegroundColor Yellow }
                }
                Write-Host ""
                Write-Host "  O que fazer, na ordem:" -ForegroundColor White
                Write-Host "    1. Feche o arquivo em qualquer editor aberto"
                Write-Host "    2. Feche o COGNI.exe, se estiver rodando"
                Write-Host "    3. Rode de novo. O script e idempotente."
                Write-Host "    4. Persistindo, adicione C:\Dev\cogni as exclusoes"
                Write-Host "       do Windows Defender."
                Write-Host ""
                throw "Nao foi possivel gravar $(Split-Path $completo -Leaf)."
            }
            Aviso "$(Split-Path $completo -Leaf) travado ($i/$Tentativas), aguardando $espera ms..."
            Start-Sleep -Milliseconds $espera
            $espera = [Math]::Min($espera * 2, 4000)
        }
    }
    return $false
}

function Invoke-Regex {
    # Aplica um replace exigindo exatamente uma ocorrencia. Um regex que
    # silenciosamente nao casa produz release com versoes discordantes.
    param(
        [Parameter(Mandatory)][string]$Texto,
        [Parameter(Mandatory)][string]$Padrao,
        [Parameter(Mandatory)][string]$Novo,
        [Parameter(Mandatory)][string]$Rotulo
    )
    $n = ([regex]$Padrao).Matches($Texto).Count
    if ($n -ne 1) {
        throw "[$Rotulo] o padrao casou $n vez(es), esperado exatamente 1.`n" +
              "  padrao: $Padrao`n" +
              "  O arquivo mudou de forma inesperada. Nada foi gravado."
    }
    # Regex.Replace trata '$' na substituicao como referencia de grupo.
    $seguro = $Novo.Replace('$', '$$')
    return ([regex]$Padrao).Replace($Texto, $seguro, 1)
}

function Find-ISCC {
    # 1. PATH
    $noPath = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($noPath) { return $noPath.Source }

    # 2. Registro - a unica forma que funciona com instalacao fora do padrao
    $chaves = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1'
    )
    foreach ($k in $chaves) {
        try {
            $loc = (Get-ItemProperty -Path $k -ErrorAction Stop).InstallLocation
            if ($loc) {
                $exe = Join-Path $loc "ISCC.exe"
                if (Test-Path -LiteralPath $exe) { return $exe }
            }
        } catch { }
    }

    # 3 e 4. Caminhos comuns, incluindo instalacao por usuario
    $candidatos = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidatos) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }

    # Inno Setup 5 nao serve: o .iss usa diretivas exclusivas da 6.
    $v5 = @(
        "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 5\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($v5) {
        Aviso "Encontrado Inno Setup 5 em '$v5', mas este script exige a 6."
        Aviso "Instale a 6 (pode conviver com a 5): https://jrsoftware.org/isdl.php"
    }

    return $null
}

# ===========================================================================
#  1. SINCRONIZAR A VERSAO
# ===========================================================================
Etapa 1 "Sincronizando a versao $Version"

foreach ($a in @("cogni.py", "version_info.txt", "cogni_installer.iss")) {
    if (-not (Test-Path -LiteralPath $a)) {
        throw "Arquivo obrigatorio ausente: $a (rode dentro de C:\Dev\cogni)"
    }
}

$p      = $Version.Split('.')
$quad   = "$($p[0]), $($p[1]), $($p[2]), 0"
$dotted = "$Version.0"

# --- Monta TUDO na memoria primeiro. So depois grava. ---
$py = Read-TextFile "cogni.py"
$py = Invoke-Regex $py 'APP_VERSION = "v[\d\.]+"' "APP_VERSION = `"v$Version`"" 'cogni.py APP_VERSION'

$vi = Read-TextFile "version_info.txt"
$vi = Invoke-Regex $vi 'filevers=\([\d, ]+\)' "filevers=($quad)" 'version_info filevers'
$vi = Invoke-Regex $vi 'prodvers=\([\d, ]+\)' "prodvers=($quad)" 'version_info prodvers'
$vi = Invoke-Regex $vi "StringStruct\('FileVersion', '[\d\.]+'\)" `
                       "StringStruct('FileVersion', '$dotted')"  'version_info FileVersion'
$vi = Invoke-Regex $vi "StringStruct\('ProductVersion', '[\d\.]+'\)" `
                       "StringStruct('ProductVersion', '$dotted')" 'version_info ProductVersion'

$iss = Read-TextFile "cogni_installer.iss"
$iss = Invoke-Regex $iss '#define MyAppVersion\s+"[\d\.]+"' `
                         "#define MyAppVersion     `"$Version`"" 'iss MyAppVersion'

# --- Agora sim, grava. Cada gravacao e no-op se nada mudou. ---
$mudou = $false
$mudou = (Write-TextFileIfChanged -Caminho "cogni.py"            -Conteudo $py)  -or $mudou
$mudou = (Write-TextFileIfChanged -Caminho "version_info.txt"    -Conteudo $vi)  -or $mudou
$mudou = (Write-TextFileIfChanged -Caminho "cogni_installer.iss" -Conteudo $iss) -or $mudou

if (-not $mudou) { Detalhe "Os tres arquivos ja estavam na versao $Version." }

if ($OnlyVersion) {
    Write-Host "`nVersao sincronizada. Nada foi compilado (-OnlyVersion)." -ForegroundColor Green
    exit 0
}

# ===========================================================================
#  2. PYINSTALLER
# ===========================================================================
if ($OnlyInstaller) {
    Etapa 2 "Reaproveitando o build existente (-OnlyInstaller)"

    if (-not (Test-Path "dist\COGNI\COGNI.exe")) {
        throw "-OnlyInstaller exige um dist\COGNI\COGNI.exe ja compilado.`n" +
              "  Rode primeiro sem -OnlyInstaller."
    }

    # Guarda-corpo: empacotar um binario antigo dentro de um instalador novo
    # produz um release em que o app diz uma versao e o instalador diz outra,
    # e o auto-updater entra em loop oferecendo a mesma atualizacao.
    $verExe = (Get-Item "dist\COGNI\COGNI.exe").VersionInfo.FileVersion
    if ($verExe -and ($verExe -replace '[,\s]', '.') -notlike "$Version*") {
        throw "O dist\COGNI\COGNI.exe existente e da versao '$verExe', mas voce " +
              "pediu $Version.`n  Rode sem -OnlyInstaller para recompilar."
    }
    Detalhe "dist\COGNI\COGNI.exe: versao $verExe (compativel)"
}
else {
    Etapa 2 "Compilando o executavel (pyinstaller)"

    foreach ($d in @("build", "dist")) {
        if (Test-Path $d) {
            try { Remove-Item $d -Recurse -Force }
            catch { throw "Nao foi possivel limpar '$d': $($_.Exception.Message)`n" +
                          "  Feche o COGNI.exe se ele estiver rodando de dentro de dist\." }
        }
    }

    python -m pip install --quiet --upgrade pyinstaller pywebview certifi
    if ($LASTEXITCODE -ne 0) { throw "pip falhou ao instalar as dependencias." }

    pyinstaller --noconfirm --clean cogni.spec
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller retornou codigo $LASTEXITCODE." }
    if (-not (Test-Path "dist\COGNI\COGNI.exe")) {
        throw "pyinstaller nao gerou dist\COGNI\COGNI.exe"
    }
}

$mb = [math]::Round((Get-ChildItem "dist\COGNI" -Recurse |
        Measure-Object Length -Sum).Sum / 1MB, 1)
Detalhe "dist\COGNI\ -> $mb MB"

# certifi empacotado? Sem ele o auto-updater se bloqueia por seguranca.
$temCertifi = (Get-ChildItem "dist\COGNI" -Recurse -Filter "cacert.pem" `
                -ErrorAction SilentlyContinue).Count -gt 0
if ($temCertifi) { Detalhe "certifi/cacert.pem incluido: TLS verificavel OK" }
else { Aviso "cacert.pem NAO encontrado. O auto-updater vai se recusar a instalar, e o LOGIN pode falhar. Rode: pip install certifi" }

# Teste de fumaca: o exe PRECISA abrir. Compilar nao prova isso (o 4.6.5 saiu
# quebrado por esse motivo). Se falhar, o build para aqui.
Detalhe "teste de fumaca: abrindo o exe por 15s..."
python build_tools\smoke_test.py dist\COGNI\COGNI.exe 15
if ($LASTEXITCODE -ne 0) { throw "Teste de fumaca FALHOU: o executavel nao abre. Nada foi empacotado." }

if ($SkipInstaller) {
    Write-Host "`nPronto (sem instalador). Teste: .\dist\COGNI\COGNI.exe" -ForegroundColor Green
    exit 0
}

# ===========================================================================
#  3. INSTALADOR
# ===========================================================================
Etapa 3 "Compilando o instalador (Inno Setup)"

$iscc = Find-ISCC

if (-not $iscc) {
    Write-Host ""
    Write-Host "  O Inno Setup 6 nao esta instalado nesta maquina." -ForegroundColor Red
    Write-Host ""
    Write-Host "  O EXECUTAVEL FOI GERADO COM SUCESSO. Falta so empacotar." -ForegroundColor Green
    Write-Host "  Voce ja pode testar o app agora:" -ForegroundColor Green
    Write-Host "      .\dist\COGNI\COGNI.exe" -ForegroundColor White
    Write-Host ""
    Write-Host "  Para gerar o instalador:" -ForegroundColor White
    Write-Host "      winget install JRSoftware.InnoSetup"
    Write-Host "  ou  https://jrsoftware.org/isdl.php"
    Write-Host ""
    Write-Host "  Depois, FECHE e reabra o PowerShell e rode:" -ForegroundColor White
    Write-Host "      .\build.ps1 -Version $Version -OnlyInstaller" -ForegroundColor White
    Write-Host ""
    throw "ISCC.exe nao encontrado. Veja as instrucoes acima."
}
Detalhe "ISCC: $iscc"

& $iscc "cogni_installer.iss" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Inno Setup retornou codigo $LASTEXITCODE." }

$DistInstaller = "C:\Dev\cogni\dist_installer"
$setup = Join-Path $DistInstaller "COGNI-Setup-$Version.exe"
if (-not (Test-Path $setup)) { throw "Inno Setup nao gerou $setup" }

# ===========================================================================
#  4. SHA-256
# ===========================================================================
Etapa 4 "Calculando SHA-256"

$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $setup).Length
Detalhe $hash

# ===========================================================================
#  5. MANIFESTO
# ===========================================================================
Etapa 5 "Manifesto para o Firebase"

$notas = if ($Notes) { $Notes } else { "Versao $Version" }
$manifesto = [ordered]@{
    version   = $Version
    kind      = "installer"
    url       = "$BaseUrl/v$Version/COGNI-Setup-$Version.exe"
    sha256    = $hash
    size      = $size
    notes     = $notas
    mandatory = $false
} | ConvertTo-Json -Depth 3

[System.IO.File]::WriteAllText(
    (Join-Path $DistInstaller "manifest-$Version.json"),
    $manifesto, $Utf8SemBom)

# Notas da versao tambem em history/<versao>: quando "stable" avancar, quem
# atualizar pulando versoes ainda ve o patch notes desta.
$hist = [ordered]@{ notes = $notas } | ConvertTo-Json -Depth 3
$vh = $Version.Replace(".", "_")
[System.IO.File]::WriteAllText(
    (Join-Path $DistInstaller "history-$Version.json"),
    $hist, $Utf8SemBom)

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Yellow
Write-Host " COLE EM /apps/cogni/_release/stable (console Firebase)" -ForegroundColor Yellow
Write-Host "=======================================================" -ForegroundColor Yellow
Write-Host $manifesto
Write-Host "=======================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "Opcional: cole dist_installer\history-$Version.json em /apps/cogni/_release/history/$vh" -ForegroundColor DarkGray
Write-Host "Instalador:  $setup" -ForegroundColor Green
Write-Host "Tamanho:     $([math]::Round($size/1MB,1)) MB" -ForegroundColor Green
Write-Host ""
Write-Host "PROXIMOS PASSOS (nesta ordem, sempre):" -ForegroundColor White
Write-Host "  1. Suba $setup no Releases de iamcaio/cogni-dist (tag v$Version)"
Write-Host "  2. Confirme que a URL do manifesto BAIXA o arquivo"
Write-Host "  3. SO DEPOIS cole o JSON no Firebase"
Write-Host ""
Write-Host "Se inverter a ordem 3 -> 1, todo usuario que abrir o Cogni" -ForegroundColor DarkYellow
Write-Host "vera uma atualizacao que ainda nao existe para download." -ForegroundColor DarkYellow
