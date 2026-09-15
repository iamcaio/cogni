# Cogni — build e publicação

Guia único. Siga na ordem. A Parte 1 é obrigatória antes do primeiro
`git push` — depois dele, alguns erros não têm desfazer.

> **Leia o `LEIA-PRIMEIRO.md` antes.** Ele explica por que esconder o código
> não impede engenharia reversa, e por que a publicação usa dois
> repositórios.

---

## Sumário

- [Parte 0 — Ferramentas](#parte-0--ferramentas)
- [Parte 1 — Os dois repositórios](#parte-1--os-dois-repositórios)
- [Parte 2 — Firebase](#parte-2--firebase)
- [Parte 3 — Gerar o build](#parte-3--gerar-o-build)
- [Parte 4 — Publicar](#parte-4--publicar)
- [Parte 5 — O ciclo de atualização](#parte-5--o-ciclo-de-atualização)
- [Parte 6 — Como o usuário instala](#parte-6--como-o-usuário-instala)
- [Parte 7 — Estado de segurança](#parte-7--estado-de-segurança)
- [Solução de problemas](#solução-de-problemas)

---

## Parte 0 — Ferramentas

Instale tudo antes de começar. **Obrigatórias:**

```powershell
pip install pyinstaller pywebview certifi
winget install JRSoftware.InnoSetup
```

`certifi` **não é opcional** neste app. Sem ele o Cogni não valida
certificado, e como a verificação passou a ser obrigatória para autenticação,
**o login para de funcionar**.

**Opcional, mas poupa trabalho manual em toda publicação:**

```powershell
winget install GitHub.cli
```

Feche e reabra o PowerShell — o PATH precisa recarregar. Então autentique:

```powershell
gh auth login
```

Ele faz quatro perguntas. As respostas, na ordem:

| Pergunta | Resposta |
|---|---|
| *Where do you use GitHub?* | `GitHub.com` |
| *What is your preferred protocol for Git operations?* | `HTTPS` |
| *Authenticate Git with your GitHub credentials?* | `Y` |
| *How would you like to authenticate GitHub CLI?* | `Login with a web browser` |

Use as setas para escolher e Enter para confirmar.

#### O código e onde colar

Depois da última resposta, o terminal mostra algo assim:

```
! First copy your one-time code: A1B2-C3D4
Press Enter to open github.com in your browser...
```

1. **Copie o código** `A1B2-C3D4` do terminal (selecione com o mouse e
   Ctrl+C)
2. **Pressione Enter.** O `gh` abre o navegador sozinho, já na página certa:
   `https://github.com/login/device`
3. Na página, cole o código no campo e clique em **Continue**
4. Clique em **Authorize github**
5. Volte ao terminal: ele detecta sozinho e mostra
   `Logged in as iamcaio`

O código **expira em 15 minutos**. Se demorar, rode `gh auth login` de novo
para receber outro.

Se o navegador não abrir sozinho, abra `https://github.com/login/device`
manualmente — a página aceita o código de qualquer dispositivo, inclusive do
celular.

Para conferir depois:

```powershell
gh auth status
```

### Sem o `gh`, dá para fazer tudo?

Sim. Ele só automatiza duas coisas: criar repositório e anexar o instalador ao
Release. Os dois têm caminho pela interface do GitHub, documentado abaixo.

O `release.ps1` **detecta** se o `gh` existe: se não existir, ele faz todo o
resto e imprime as instruções do upload manual. Não quebra.

Se você pretende publicar várias versões, instale — o passo manual de anexar
o `.exe` a cada release cansa rápido.

---

## Parte 1 — Os dois repositórios

```
iamcaio/cogni        PRIVADO   codigo, docs, scripts, regras
iamcaio/cogni-dist   PUBLICO   somente o .exe anexado ao Release
```

### 1.1 Criar — pela interface do GitHub

Este é o caminho que funciona sem instalar nada.

**Repositório privado (código):**

1. Abra https://github.com/new
2. Repository name: `cogni`
3. Marque **Private**
4. **Não** marque *Add a README*, *.gitignore* nem *license* — qualquer arquivo
   inicial cria um commit no remoto e o seu primeiro push é rejeitado por
   históricos divergentes
5. Create repository

**Repositório público (instaladores):**

1. https://github.com/new
2. Repository name: `cogni-dist`
3. Marque **Public**
4. Sem README, sem nada
5. Create repository

Depois, no PowerShell:

```powershell
cd C:\Dev\cogni
git init
git remote add origin https://github.com/iamcaio/cogni.git
git remote -v          # confirme que aparece /cogni.git, NAO /cogni-dist.git
```

> Confira o `git remote -v` com atenção. Se `origin` apontar para o
> `cogni-dist`, um push enviaria o código-fonte para o repositório público. O
> `release.ps1` tem uma trava contra isso, mas um `git push` manual não passa
> por ela.

### 1.1b Criar — com o `gh` (atalho)

Se você instalou o `gh` e rodou `gh auth login`:

```powershell
cd C:\Dev\cogni
gh repo create iamcaio/cogni --private --source=. --remote=origin
gh repo create iamcaio/cogni-dist --public --description "Instaladores do Cogni"
```

Duas linhas em vez de dois formulários. O resultado é idêntico.

### 1.2 Identidade do git

Na primeira vez o git recusa o commit com "Author identity unknown". O e-mail
fica gravado **dentro de cada commit, para sempre**.

Este repositório é privado, mas use o `noreply` do GitHub de qualquer forma —
se um dia ele virar público, ou se você conceder acesso a alguém, o endereço
vai junto.

1. GitHub → Settings → Emails → marque **Keep my email addresses private**
2. Copie o endereço `12345678+seu-usuario@users.noreply.github.com`

```powershell
git config user.name "Seu Nome"
git config user.email "12345678+seu-usuario@users.noreply.github.com"
git config user.email          # confira
```

**Não use o e-mail do trabalho.** Este é um projeto pessoal.

### 1.3 Verificações antes do primeiro commit

```powershell
git add -A .
git status --short
```

Nada de `db/`, `docs/`, `dist/`, `build/` ou `*.exe` pode aparecer. Confirme:

```powershell
git check-ignore -v db
git check-ignore -v dist_installer
```

Depois:

```powershell
python verificar.py
git commit -m "Cogni 4.5.0"
git branch -M main
git push -u origin main
```

Logo após o push, **abra o repositório no navegador** e confirme que ele está
marcado `Private` e que não há pasta `db`.

### 1.4 A trava contra o erro mais caro

O `release.ps1` recusa o push se o remoto `origin` apontar para o repositório
**público**. Esse erro — um `git remote` trocado — publicaria o código-fonte
inteiro, e é exatamente o que a arquitetura de dois repositórios existe para
evitar.

---

## Parte 2 — Firebase

### 2.1 Publique as Regras

```powershell
python verificar.py
```

Ele valida as regras junto com o resto. Passando, cole o
`firebase-rules.json` em Console → Realtime Database → Regras → Publicar.

| Caminho | Regra | Motivo |
|---|---|---|
| `apps/cogni/_release` | `.read: true` | O app checa a versão **antes** do login. Contém só número de versão, URL pública e hash. Sem `.write`: só o console escreve. |
| `apps/cogni/users/$uid` | `auth.uid == $uid` | É isto que impede um usuário de ler os dados de outro. |
| `apps/nexus/*` | preservado | **Não remova.** Publicar sem essa parte tira o acesso do NEXUS. |
| `apps/donext/*` | leitura só | Backup imutável. |

> O arquivo cobre os **três** apps. Publicar uma versão que só tenha o `cogni`
> derrubaria o NEXUS na hora.

### 2.2 O nó `_release` vem depois

Publique **só as Regras** agora e siga para a Parte 3.

O nó `/apps/cogni/_release/stable` é criado na
[Parte 4.3](#43-crie-o-nó-_release-no-firebase), depois do build — porque ele
guarda o hash e a URL de um instalador que ainda não existe.

No Realtime Database não existe caminho vazio: um nó nasce quando recebe dado.
Criá-lo antes de ter o manifesto significaria preencher à mão os sete campos
e depois corrigir o hash, que é justamente o campo que não se digita.

---

## Parte 3 — Gerar o build

### 3.1 Confirme as ferramentas

Já instaladas na [Parte 0](#parte-0--ferramentas). Para conferir:

```powershell
python -c "import certifi; print('certifi ok')"
python -c "import PyInstaller; print('pyinstaller ok')"
```

O `build.ps1` avisa se o `cacert.pem` não aparecer no build — esse aviso é
sério: sem ele o login falha.

### 3.2 Compile

```powershell
cd C:\Dev\cogni
.\build.ps1 -Version 4.5.1 -Notes "Correcoes de seguranca"
```

O script sincroniza a versão nos três arquivos, compila, empacota, calcula o
SHA-256 e imprime o manifesto pronto.

**É este passo que cria a pasta `dist_installer\`.** Antes de rodá-lo ela não
existe — o `build.ps1` é quem a gera, com dois arquivos dentro:

```
dist_installer\COGNI-Setup-4.5.1.exe      o instalador que voce distribui
dist_installer\manifest-4.5.1.json        o manifesto para o Firebase
```

Se o Inno Setup não estiver instalado, você fica só com
`dist\COGNI\COGNI.exe` e nenhum `dist_installer`. O script diz isso
explicitamente e dá o comando para instalar.

| Flag | Para quê |
|---|---|
| `-SkipInstaller` | Só o `.exe` |
| `-OnlyInstaller` | Reaproveita o `dist\COGNI` já compilado |
| `-OnlyVersion` | Só sincroniza a versão |
| `-BaseUrl` | Sobrescreve a URL do manifesto |

### 3.3 Teste antes de publicar

```powershell
python verificar.py
.\dist\COGNI\COGNI.exe
```

No app, confirme:

- suas anotações e matérias aparecem
- o login funciona (é aqui que se vê se o `certifi` foi empacotado)
- a pasta `C:\COGNI\docs` foi criada
- o painel de atualização mostra **"TLS verificado"**

---

## Parte 4 — Publicar

A ordem desta parte importa. **Manifesto é o último passo, sempre.** Se você
publicar o manifesto antes do instalador existir na URL, todo usuário que
abrir o Cogni verá uma atualização que falha ao baixar.

### 4.1 Um comando

```powershell
.\release.ps1 -Version 4.5.1 -Notes "- Correcoes de seguranca"
```

Ele faz, parando em qualquer erro:

1. `build.ps1`
2. `verificar.py`
3. Commit no **privado**, com mensagem **lida do `cogni.py`**
4. Tag `v4.5.1` e push
5. Release no **público**, com **só o instalador** anexado
6. Imprime o manifesto

Antes, veja o que faria: `.\release.ps1 -Version 4.5.1 -DryRun`

### 4.2 Confirme que a URL baixa o arquivo

Abra no navegador a URL que aparece no campo `url` do manifesto impresso:

```
https://github.com/iamcaio/cogni-dist/releases/download/v4.5.1/COGNI-Setup-4.5.1.exe
```

**Se ela não baixar o instalador, pare aqui.** Publicar o manifesto com uma
URL que não funciona é o erro mais chato deste processo: o app vê a
atualização, tenta baixar, falha, e você vai procurar defeito no updater.

### 4.3 Crie o nó `_release` no Firebase

Agora sim — o manifesto existe e a URL está confirmada.

O caminho é **`_release`, no singular**:

```
/apps/cogni/_release/stable
```

**Na primeira vez**, o nó não existe. No Realtime Database não há caminho
vazio: ele nasce quando recebe dado.

1. Console → Realtime Database → aba **Dados**
2. Expanda `apps` → `cogni`
3. Passe o mouse em `cogni`, clique no **`+`**
4. Nome: `_release` → clique no **`+`** ao lado do valor (isso aninha em vez
   de gravar um valor solto)
5. Nome: `stable` → aninhe outra vez
6. Nome: `version`, valor: `4.5.1` → **Adicionar**

O caminho passa a existir. Para preencher os outros seis campos de uma vez:

7. Clique em `stable` na árvore
8. Menu **⋮** → **Importar JSON** → escolha
   `dist_installer\manifest-4.5.1.json`

> **Importar JSON substitui o nó em que você está.** Faça só dentro de
> `stable`. Em `/apps/cogni` apagaria `users` — suas anotações e matérias.

**Nas versões seguintes**, o nó já existe: vá direto ao passo 7.

### 4.4 Confirme a leitura pública

Abra no navegador, **deslogado**:

```
https://cogni-data-default-rtdb.firebaseio.com/apps/cogni/_release/stable.json
```

Tem de devolver o JSON — é exatamente assim que o Cogni lê, antes de qualquer
login. `null` = nome do nó errado. `Permission denied` = Regras não publicadas.

### Sem o `gh`: o passo 5 vira manual

O `release.ps1` detecta a ausência do `gh`, executa os passos 1 a 4 e 6
normalmente, e imprime as instruções do upload. Você faz:

1. Abra `https://github.com/iamcaio/cogni-dist/releases/new`
2. Em **Choose a tag**, digite `v4.5.1` e clique em *Create new tag on publish*
3. Release title: `Cogni 4.5.1`
4. Arraste **somente** o `dist_installer\COGNI-Setup-4.5.1.exe`
5. **Publish release**

> **Anexe só o instalador.** Nada de `.py`, `.md`, `.json` ou a pasta `dist\`.
> O ponto do repositório público é conter apenas o binário.

Depois, copie a URL do arquivo anexado (botão direito → Copiar endereço do
link) e confirme que ela bate com o campo `url` do manifesto que o script
imprimiu. Se não bater, corrija o manifesto antes de colar no Firebase.

### As quatro travas

| Trava | O que impede |
|---|---|
| `origin` apontando para o repo público | **publicar o código-fonte** |
| Versão menor ou igual à última tag | quem tem a maior nunca receberia |
| Tag existente | Release apontando para código diferente do declarado |
| `verificar.py` falhando | publicar com falha de segurança ou versões desalinhadas |

### Por que a mensagem do commit é gerada, não escrita

Uma versão vive em **seis** lugares: `cogni.py`, `version_info.txt`, o `.iss`,
a mensagem do commit, a tag e o manifesto.

No NEXUS, mantendo isso à mão, eles divergiram: um commit dizia "6.1.1" e
continha código 6.1.0. Nada quebrou, mas a pergunta *"que código está rodando
na máquina do usuário?"* ficou sem resposta — e é a única que importa quando
aparece um bug.

O `release.ps1` lê a versão do `cogni.py` **depois** do build. A mensagem não
pode mentir porque não é digitada.

### O passo que continua manual

Colar o manifesto no Firebase. As Regras não dão `.write` ao nó `_release`,
então nenhum token grava ali — nem o script. Esse nó decide qual arquivo é
baixado e **executado** na máquina dos usuários; se um token pudesse alterá-lo,
roubar o token bastaria para comprometer todos.

**Antes de colar, abra a URL do manifesto e confirme que ela baixa o arquivo.**

---

## Parte 5 — O ciclo de atualização

```powershell
# 1. Edite o cogni.py
# 2. Um comando:
.\release.ps1 -Version 4.5.2 -Notes "- Corrigido X"
# 3. Cole o JSON impresso no Firebase
```

Do lado do usuário: em até 6 horas, ou ao reabrir o app, aparece o aviso de
versão nova. Ele clica, o Cogni baixa, **confere o SHA-256**, fecha e reabre
atualizado.

### Nunca diminua o número da versão

O updater compara **numericamente**. Se você publicou `4.5.2` e depois compila
`4.5.1`, quem tem a `4.5.2` **nunca receberá** a `4.5.1` — o app dirá "está
atualizado", corretamente. E o seu teste vai parecer que o updater quebrou.

Escolha o número e só avance.

### Testar antes de liberar

Troque `UPDATE_CHANNEL` para `"beta"` na sua cópia, publique o manifesto em
`/apps/cogni/_release/beta` e valide. Depois copie para `stable`.

### Segurança da atualização

Um auto-updater é, por definição, um canal de execução remota de código. Cinco
travas:

1. Manifesto lido por HTTPS **com verificação de certificado**
2. Download por HTTPS **com verificação**, sem fallback
3. O arquivo só executa se o **SHA-256 bater**. Sem hash, o app se recusa
4. URL não-HTTPS no manifesto → bloqueado
5. Limite de 400 MB

**Não remova a verificação de hash para "simplificar".** Sem ela, quem
controlar o manifesto controla o que roda na máquina de todos os usuários.

---

## Parte 6 — Como o usuário instala

Mande o `COGNI-Setup-4.5.1.exe`. Duplo clique, Avançar, Instalar.

- Não pede senha de administrador (instala em `%LOCALAPPDATA%\Programs\Cogni`)
- Cria atalho no Menu Iniciar e na Área de Trabalho
- Cria `C:\COGNI\db` e `C:\COGNI\docs`
- Aparece em "Aplicativos instalados", com desinstalador

**Desinstalar não apaga seus dados.** As duas pastas estão marcadas com
`uninsneveruninstall` no `.iss`.

**SmartScreen.** Na primeira vez o Windows mostra "aplicativo não reconhecido",
porque o instalador não tem assinatura digital. "Mais informações" → "Executar
assim mesmo".

---

## Parte 7 — Estado de segurança

Auditoria de 15 de setembro de 2026. Quatro correções entraram:

| Área | O que passou a valer |
|---|---|
| TLS | Verificação obrigatória. **Login e atualização nunca degradam** — recusam e falham. Os dois contextos permissivos foram removidos. |
| Abertura de arquivos | 30 extensões executáveis bloqueadas; esquemas `javascript:`, `file:`, `ms-msdt:` recusados. |
| Editor de notas | Sanitização por lista de permissão em quatro pontos: colar, arrastar, abrir e salvar. |
| Dependências | CSP nas **duas** janelas (principal e Timer); versões de biblioteca fixadas. |

Rode a qualquer momento:

```powershell
python verificar.py
```

São 60 checagens: integridade dos arquivos, as quatro correções, caminhos do
banco, updater, ponte Python↔JS, versões alinhadas e regras do Firebase.

### Uma checagem que vale entender

O verificador confere **método duplicado na ponte Python↔JS**. Se alguém criar
um método que já existe na classe, o Python **não dá erro** — o último
silenciosamente vence. É um bug que nenhum teste de sintaxe pega e que muda
comportamento sem sinal.

### O que continua em aberto

**Sem assinatura de código.** Todo usuário vê o SmartScreen, e não há como
provar que o instalador é seu. Certificado EV custa US$ 200–400/ano. Para o
seu círculo, aceitável; para distribuição ampla, deixa de ser.

**CSP com `unsafe-inline`.** Todo o JavaScript é inline. A CSP limita o
estrago, mas não impede execução de script injetado.

**Dependência de três CDNs.** Versões fixas, mas um CDN comprometido roda
código com acesso à ponte Python. A correção é baixar os arquivos para uma
pasta `assets\` e referenciar localmente.

**Sem criptografia nas anotações.** Elas estão em texto claro no banco e na
nuvem. Se você guardar credencial ou dado sensível ali, quem tiver acesso à
sua conta Firebase lê tudo. Use um gerenciador de senhas para credenciais.

---

## Solução de problemas

### `The string is missing the terminator: "` num `.ps1`

O PowerShell aponta uma linha sem defeito. O problema é **encoding**.

O Windows PowerShell 5.1 lê `.ps1` **sem BOM** como Windows-1252. Um travessão
UTF-8 (`E2 80 94`) vira três caracteres, e o último (`0x94`) é `”` — uma aspa
tipográfica que **o PowerShell aceita como delimitador de string**. Cada acento
vira uma aspa fantasma.

**Regra deste projeto: `.ps1` é ASCII puro.** A checagem 0 do `verificar.py`
recusa `.ps1` com byte não-ASCII sem BOM.

### `source code cannot contain null bytes`

Um byte nulo entrou no `cogni.py`, geralmente por um intervalo de caracteres
escrito com caracteres literais em vez de escapes.

```powershell
python build_tools\corrigir_urlsegura.py
```

Ele remove os bytes de controle, valida com `compile()` e **restaura o backup
se não compilar**.

### `gh : O termo 'gh' nao e reconhecido`

O GitHub CLI não está instalado. Ele é **opcional** — veja a
[Parte 0](#parte-0--ferramentas) e a [Parte 1.1](#11-criar--pela-interface-do-github),
que fazem tudo pela interface do GitHub.

Se quiser instalar:

```powershell
winget install GitHub.cli
# FECHE e reabra o PowerShell (o PATH precisa recarregar)
gh auth login
```

O `gh auth login` é o passo que mais se esquece. Sem ele, o `gh` está
instalado mas todo comando falha com erro de autenticação — o que parece outro
problema. O fluxo completo, com o código e onde colar, está na
[Parte 0](#o-código-e-onde-colar).

### `gh` instalado mas os comandos falham com erro de autenticação

Você instalou e não rodou `gh auth login`, ou o token expirou.

```powershell
gh auth status
```

Se disser que não está autenticado, rode `gh auth login` de novo.

### A pasta `dist_installer` não existe

Ela é **criada pelo `build.ps1`**, não vem no projeto. Se não existe, o build
ainda não rodou — ou rodou e parou antes de empacotar.

```powershell
cd C:\Dev\cogni
.\build.ps1 -Version 4.5.1
```

Se o build terminar e você tiver `dist\COGNI\COGNI.exe` mas **não** tiver
`dist_installer`, o Inno Setup não está instalado. O script avisa isso e dá o
comando. Veja a seção abaixo.

### `ISCC.exe nao encontrado`

O Inno Setup 6 não está instalado. O `.exe` **já foi gerado** — teste com
`.\dist\COGNI\COGNI.exe`. Depois:

```powershell
winget install JRSoftware.InnoSetup
# feche e reabra o PowerShell
.\build.ps1 -Version 4.5.1 -OnlyInstaller
```

**Inno Setup 5 não serve** — o `.iss` usa diretivas exclusivas da 6.

### `Set-Content : The process cannot access the file`

1. Feche o arquivo em qualquer editor
2. Feche o `COGNI.exe`
3. Rode de novo — o script é idempotente
4. Persistindo, adicione `C:\Dev\cogni` às exclusões do Defender

### O login parou de funcionar depois das correções

O `certifi` não foi empacotado. Agora a verificação de certificado é
obrigatória para autenticação, então sem ele o login falha.

```powershell
pip install certifi
.\build.ps1 -Version 4.5.1
```

### "Uma conexão de dados foi feita sem verificar o certificado"

Alguém ou alguma coisa está no meio da sua conexão. Em ordem de probabilidade:

1. Antivírus com inspeção HTTPS (Kaspersky, Avast, ESET)
2. Proxy corporativo
3. Data e hora do computador erradas
4. Rede hostil — se estiver em Wi-Fi público, **saia e não faça login**

Login e atualização não são afetados: eles recusam e falham.

### O executável abre e fecha na hora

```powershell
pyinstaller --noconfirm --clean --console cogni.spec
.\dist\COGNI\COGNI.exe
```

---

## Arquivos do projeto

| Arquivo | Para quê |
|---|---|
| `cogni.py` | O aplicativo |
| `cogni.spec` | PyInstaller, com `certifi` |
| `version_info.txt` | Metadados do exe |
| `cogni_installer.iss` | Instalador; cria `C:\COGNI\db` e `docs` |
| `build.ps1` | Compila: versão, exe, instalador, hash, manifesto |
| `release.ps1` | **Publica: privado + público, com as quatro travas** |
| `verificar.py` | 60 checagens. Rode antes de tudo |
| `firebase-rules.json` | Regras dos três apps |
| `.gitignore` | Bloqueia `db/`, `docs/`, builds, `*.exe` |
| `LEIA-PRIMEIRO.md` | Auditoria e por que dois repositórios |
| `build_tools\corrigir_urlsegura.py` | Remove bytes de controle do fonte |
