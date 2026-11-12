# ===========================================================================
#  Cogni - publicar uma versao, de ponta a ponta
# ===========================================================================
#  Uso:
#     .\release.ps1 -Version 4.5.1 -Notes "- Corrigido X"
#     .\release.ps1 -Version 4.5.1 -DryRun     (mostra o que faria)
#     .\release.ps1 -Version 4.5.1 -SkipBuild  (reaproveita o build)
#
#  Faz, nesta ordem, parando em qualquer erro:
#     1. build.ps1 - sincroniza versao, compila exe e instalador
#     2. verificar.py - aborta se algo falhar
#     3. Commit no repositorio PRIVADO, com mensagem lida do codigo
#     4. Tag vX.Y.Z e push
#     5. Release no repositorio PUBLICO, com SO o instalador anexado
#     6. Imprime o manifesto para o Firebase
# ===========================================================================
#
#  POR QUE DOIS REPOSITORIOS
#
#      iamcaio/cogni        PRIVADO   codigo, docs, scripts, regras
#      iamcaio/cogni-dist   PUBLICO   somente o .exe anexado ao Release
#
#  O auto-updater precisa baixar de uma URL PUBLICA. Em repositorio privado o
#  link do Releases exige autenticacao, e a unica forma de o app se autenticar
#  seria embutir um token no executavel. Um token dentro de um .exe
#  distribuido E um token vazado, so com passos extras.
#
#  O repositorio publico NUNCA recebe commit de codigo. Este script anexa o
#  binario ao Release e nada mais - zero .py, zero .md, zero .json.
#
#  AVISO IMPORTANTE, para nao criar falsa sensacao de seguranca:
#  esconder o codigo NAO impede engenharia reversa. O PyInstaller empacota
#  BYTECODE, nao codigo de maquina. Com pyinstxtractor + decompyle3, qualquer
#  pessoa recupera Python legivel do instalador. Quem protege seus dados sao
#  as Regras do Firebase, que rodam no servidor. Ver LEIA-PRIMEIRO.md.
#
#  ESTE ARQUIVO E ASCII PURO. Nao adicione acento: o PowerShell 5.1 leria como
#  Windows-1252 e cada acento viraria uma aspa fantasma no codigo.
# ===========================================================================

param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [string]$RepoPrivado = "iamcaio/cogni",
    [string]$RepoPublico = "iamcaio/cogni-dist",
    [switch]$DryRun,
    [switch]$SkipBuild,
    # Retoma SO o passo 5 (Release no publico). Use quando o build, o commit e
    # a tag ja passaram e a publicacao falhou - assim voce nao precisa
    # inventar uma versao nova so para repetir a ultima etapa.
    [switch]$OnlyRelease
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Etapa($n, $txt) { Write-Host "`n[$n/6] $txt" -ForegroundColor Cyan }
function Detalhe($txt)   { Write-Host "      $txt" -ForegroundColor DarkGray }
function Aviso($txt)     { Write-Host "      $txt" -ForegroundColor Yellow }
function Executar($cmd) {
    if ($DryRun) { Write-Host "      [dry-run] $cmd" -ForegroundColor Magenta; return }
    Write-Host "      > $cmd" -ForegroundColor DarkGray
    Invoke-Expression $cmd
    if ($LASTEXITCODE -ne 0) { throw "Falhou: $cmd" }
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Versao deve ser X.Y.Z. Recebido: '$Version'"
}
if ($DryRun) {
    Write-Host "`n*** MODO DRY-RUN - nada sera alterado ***" -ForegroundColor Magenta
}

# -OnlyRelease implica pular o build: o instalador ja existe em disco.
if ($OnlyRelease) {
    $SkipBuild = $true
    Write-Host "`n*** -OnlyRelease: retomando SO o passo 5 ***" -ForegroundColor Magenta
    Write-Host "    build, verificacao, commit e tag serao pulados." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
#  0. A versao nao pode ser menor que a ultima publicada
# ---------------------------------------------------------------------------
$tags = @(git tag -l "v*" 2>$null)
if ($OnlyRelease) {
    # Com -OnlyRelease a tag desta versao JA existe, de proposito: e a que
    # estamos terminando de publicar. Checar "maior que a ultima" aqui
    # abortaria o retry, que e exatamente o que queremos permitir.
    Detalhe "-OnlyRelease: checagem de versao pulada"
} elseif ($tags.Count -gt 0) {
    $maior = $tags |
        ForEach-Object { [version]($_ -replace '^v', '') } |
        Sort-Object -Descending |
        Select-Object -First 1
    if ([version]$Version -le $maior) {
        Write-Host ""
        Write-Host "  A versao $Version nao e maior que a ultima tag (v$maior)." -ForegroundColor Red
        Write-Host ""
        Write-Host "  O updater compara numericamente: quem tem v$maior instalada" -ForegroundColor Yellow
        Write-Host "  NUNCA receberia a $Version. O app diria 'esta atualizado' -" -ForegroundColor Yellow
        Write-Host "  corretamente - e o seu teste pareceria quebrado." -ForegroundColor Yellow
        Write-Host ""
        throw "Use uma versao maior que $maior."
    }
    Detalhe "ultima tag: v$maior -> publicando $Version"
} else {
    Aviso "nenhuma tag local. Se criou releases pela interface do GitHub,"
    Aviso "rode 'git fetch --tags' antes para esta checagem valer."
}

# ---------------------------------------------------------------------------
#  1. BUILD
# ---------------------------------------------------------------------------
Etapa 1 "Sincronizando versao e compilando"
if ($SkipBuild) {
    Detalhe "-SkipBuild: reaproveitando o build existente"
} else {
    $argsBuild = "-Version $Version -BaseUrl `"https://github.com/$RepoPublico/releases/download`""
    if ($Notes) { $argsBuild += " -Notes `"$Notes`"" }
    Executar ".\build.ps1 $argsBuild"
}

# ---------------------------------------------------------------------------
#  2. VERIFICAR - a versao real vem daqui, nao do parametro
# ---------------------------------------------------------------------------
Etapa 2 "Verificacoes"
if (-not $DryRun) {
    python verificar.py
    if ($LASTEXITCODE -ne 0) {
        throw "verificar.py falhou. Corrija antes de publicar."
    }
} else {
    Detalhe "[dry-run] python verificar.py"
}

# Nunca publica um exe que nao abre (foi o que aconteceu no 4.6.5).
if (-not $DryRun) {
    python build_tools\smoke_test.py dist\COGNI\COGNI.exe 15
    if ($LASTEXITCODE -ne 0) { throw "Teste de fumaca FALHOU: o executavel nao abre. Nada foi publicado." }
}

# Le a versao DO CODIGO. Se divergir do parametro, o build nao aplicou.
$py = Get-Content -Raw -Encoding UTF8 "cogni.py"
if ($py -match 'APP_VERSION = "v([\d.]+)"') {
    $versaoReal = $Matches[1]
} else {
    throw "Nao foi possivel ler APP_VERSION do cogni.py."
}
if ($versaoReal -ne $Version -and -not $DryRun) {
    throw "O codigo diz $versaoReal mas voce pediu $Version. O build nao aplicou a versao."
}
Detalhe "versao confirmada no codigo: $versaoReal"

# ---------------------------------------------------------------------------
#  3. COMMIT no repositorio PRIVADO - mensagem derivada, nunca digitada
# ---------------------------------------------------------------------------
Etapa 3 "Commit no repositorio privado ($RepoPrivado)"

$msg = "Cogni $versaoReal"
if ($Notes) {
    $primeira = ($Notes -split "`n")[0].Trim().TrimStart('-', ' ')
    if ($primeira) { $msg += " - $primeira" }
}
Detalhe "mensagem: $msg"

if ($OnlyRelease) {
    Detalhe "-OnlyRelease: commit, tag e push pulados (ja foram feitos)"
}

# Trava de seguranca: se o remoto 'origin' for o repositorio PUBLICO, abortar.
# Um push de codigo para la exporia tudo que estamos tentando nao expor.
$origem = (git remote get-url origin 2>$null)
if ($origem) {
    Detalhe "origin: $origem"
    if ($origem -match [regex]::Escape($RepoPublico)) {
        Write-Host ""
        Write-Host "  PERIGO: o remoto 'origin' aponta para o repositorio PUBLICO" -ForegroundColor Red
        Write-Host "  ($RepoPublico). Um push enviaria o codigo-fonte para la." -ForegroundColor Red
        Write-Host ""
        Write-Host "  Corrija com:" -ForegroundColor White
        Write-Host "    git remote set-url origin https://github.com/$RepoPrivado.git"
        Write-Host ""
        throw "origin aponta para o repositorio publico. Nada foi enviado."
    }
} else {
    Aviso "nenhum remoto 'origin' configurado. Rode:"
    Aviso "  git remote add origin https://github.com/$RepoPrivado.git"
}

if (-not $OnlyRelease) {
    Executar "git add -A ."
    $pendente = git diff --cached --name-only
    if (-not $pendente -and -not $DryRun) {
        Aviso "nada para commitar - o codigo ja esta commitado nesta versao"
    } else {
        Executar "git commit -m `"$msg`""
    }
}

# ---------------------------------------------------------------------------
#  4. TAG E PUSH
# ---------------------------------------------------------------------------
Etapa 4 "Tag v$versaoReal e push"

if ($OnlyRelease) {
    Detalhe "pulado (-OnlyRelease)"
} else {

$existe = git tag -l "v$versaoReal"
if ($existe) {
    throw "A tag v$versaoReal ja existe. Escolha outra versao - reutilizar tag " +
          "faz o Release apontar para codigo diferente do que ele diz conter.`n" +
          "  Se a tag esta certa e apenas a PUBLICACAO falhou, retome com:`n" +
          "    .\release.ps1 -Version $versaoReal -OnlyRelease"
}
Executar "git tag -a v$versaoReal -m `"$msg`""

if ($origem) {
    Executar "git push origin main"
    Executar "git push origin v$versaoReal"
} else {
    Aviso "sem remoto configurado: push ignorado"
}

}  # fim do else de -OnlyRelease

# ---------------------------------------------------------------------------
#  5. RELEASE no repositorio PUBLICO - somente o instalador
# ---------------------------------------------------------------------------
Etapa 5 "Release no repositorio publico ($RepoPublico)"

$setup = "C:\Dev\cogni\dist_installer\COGNI-Setup-$versaoReal.exe"

if (-not (Test-Path $setup)) {
    Aviso "instalador nao encontrado em $setup"
    Aviso "rode sem -SkipBuild, ou suba o arquivo manualmente."
} else {
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if ($gh) {
        # -------------------------------------------------------------------
        # O GitHub recusa criar Release em repositorio SEM NENHUM COMMIT, com
        # "HTTP 422: Repository is empty". Um repositorio criado sem README
        # nao tem commit nem branch padrao, e nao ha onde ancorar a tag.
        #
        # Em vez de deixar isso estourar na sua cara, damos o primeiro commit
        # aqui: um README de tres linhas dizendo que o repositorio so hospeda
        # instaladores. Ele nao expoe nada - nenhum codigo, nenhuma estrutura.
        # -------------------------------------------------------------------
        $vazio = $false
        try {
            gh api "repos/$RepoPublico/commits" --jq 'length' 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { $vazio = $true }
        } catch { $vazio = $true }

        if ($vazio) {
            Aviso "$RepoPublico esta vazio (sem commits) - criando o README inicial"
            $texto = "# Cogni - Instaladores`n`n" +
                     "Este repositorio contem apenas os instaladores do Cogni.`n" +
                     "Nao ha codigo-fonte aqui.`n"
            $b64 = [Convert]::ToBase64String(
                [Text.Encoding]::UTF8.GetBytes($texto))
            Executar ("gh api repos/$RepoPublico/contents/README.md -X PUT " +
                      "-f message=`"Repositorio de distribuicao`" " +
                      "-f content=`"$b64`"")
            Detalhe "primeiro commit criado; o Release agora tem onde ancorar"
            Start-Sleep -Seconds 2
        }

        $notasRelease = if ($Notes) { $Notes } else { "Versao $versaoReal" }
        # --repo aponta para o PUBLICO. --target nao e usado: o repositorio
        # publico nao tem o codigo, entao a tag do Release e criada nele
        # diretamente, sem vinculo com o commit do privado.
        Executar ("gh release create v$versaoReal `"$setup`" " +
                  "--repo $RepoPublico " +
                  "--title `"Cogni $versaoReal`" " +
                  "--notes `"$notasRelease`"")
        Detalhe "instalador publicado - e SOMENTE o instalador"
    } else {
        Aviso "GitHub CLI (gh) nao instalado - upload manual necessario:"
        Aviso "  1. https://github.com/$RepoPublico/releases/new?tag=v$versaoReal"
        Aviso "  2. anexe SOMENTE $setup"
        Aviso "  3. Publish release"
        Aviso ""
        Aviso "Para automatizar: winget install GitHub.cli"
    }
}

# ---------------------------------------------------------------------------
#  6. MANIFESTO
# ---------------------------------------------------------------------------
Etapa 6 "Manifesto para o Firebase"

$manifesto = "C:\Dev\cogni\dist_installer\manifest-$versaoReal.json"
if (Test-Path $manifesto) {
    $conteudo = Get-Content -Raw $manifesto
    Write-Host ""
    Write-Host "=======================================================" -ForegroundColor Yellow
    Write-Host " COLE EM /apps/cogni/_release/stable (console Firebase)" -ForegroundColor Yellow
    Write-Host "=======================================================" -ForegroundColor Yellow
    Write-Host $conteudo
    Write-Host "=======================================================" -ForegroundColor Yellow
} else {
    Aviso "manifesto nao encontrado em $manifesto"
}

Write-Host ""
Write-Host "Codigo no privado, instalador no publico, como $versaoReal." -ForegroundColor Green
Write-Host ""
Write-Host "FALTA UM PASSO, E ELE E MANUAL DE PROPOSITO:" -ForegroundColor White
Write-Host "  Colar o manifesto acima no console do Firebase." -ForegroundColor White
Write-Host ""
Write-Host "As Regras nao dao .write ao no _release, entao nenhum token pode" -ForegroundColor DarkGray
Write-Host "gravar ali - nem este script. Esse no decide qual arquivo sera" -ForegroundColor DarkGray
Write-Host "baixado e EXECUTADO na maquina dos seus usuarios; se um token" -ForegroundColor DarkGray
Write-Host "pudesse alterar, roubar o token bastaria para comprometer todos." -ForegroundColor DarkGray
Write-Host ""
Write-Host "Antes de colar, ABRA A URL do manifesto e confirme que ela baixa" -ForegroundColor White
Write-Host "o arquivo. Se colar antes, todo usuario vera uma atualizacao que" -ForegroundColor White
Write-Host "falha." -ForegroundColor White
