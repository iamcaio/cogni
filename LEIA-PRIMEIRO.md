# Cogni — arquitetura de publicação e auditoria

## Não publicar o `.py` não impede engenharia reversa

O PyInstaller **não compila** Python para código de máquina. Ele empacota o
bytecode dentro do `.exe`. Nesta conversa eu extraí a `apiKey` de dentro do
`NEXUS.exe` com 15 linhas de Python, descomprimindo o arquivo PYZ embutido.

Com `pyinstxtractor` + um decompilador (`decompyle3`, `uncompyle6`) — duas
ferramentas públicas e gratuitas — qualquer pessoa transforma o `.exe` de volta
em Python legível, com nomes de função e docstrings.

**Quem tem o instalador tem o código.** Esconder o `cogni.py` do GitHub
dificulta a vida de um curioso, não de alguém que queira mesmo olhar.

| Medida | Efeito real |
|---|---|
| Não publicar o `.py` | quase nulo — o `.exe` tem o código |
| **Regras do Firebase corretas** | **decisivo** — rodam no servidor |
| **Não embutir segredo de servidor no app** | **decisivo** |
| Assinatura de código | não esconde, mas prova que o instalador é seu |
| Obfuscação (PyArmor) | eleva a barreira, custa licença, quebra com frequência |
| Reescrever em Go/Rust | aí sim vira binário nativo — mas é reescrever o app |

A pergunta que importa não é *"alguém consegue ler meu código?"* — consegue. É
*"alguém consegue acessar meus dados ou se passar por mim?"*. Essa resposta
depende das Regras do Realtime Database e de você não colocar credencial de
servidor no cliente.

## A arquitetura escolhida: dois repositórios

Seu objetivo continua legítimo por outros motivos — não facilitar cópia, não
expor lógica de negócio, não entregar um mapa pronto.

```
iamcaio/cogni        PRIVADO   codigo, docs, scripts, regras
iamcaio/cogni-dist   PUBLICO   somente o .exe anexado ao Release
```

**Por que não um só repositório privado.** O auto-updater precisa baixar o
instalador de uma URL **pública**. Em repositório privado, o link do Releases
exige autenticação, e a única forma de o app se autenticar seria embutir um
token dentro do executável. Um token dentro de um `.exe` distribuído **é um
token vazado**, só com passos extras. Seria trocar um problema imaginário por
um real.

O `cogni-dist` **nunca recebe `git commit` de código**. Só o binário anexado
ao Release. Zero `.py`, zero `.md`, zero `.json`.

### Criar os dois repositórios

```powershell
# 1. Privado, para o codigo
gh repo create iamcaio/cogni --private --source=C:\Dev\cogni --remote=origin

# 2. Publico, so para distribuir
gh repo create iamcaio/cogni-dist --public --description "Instaladores do Cogni"
```

Sem o `gh`: crie pela interface do GitHub, marcando **Private** no primeiro e
**Public** no segundo, os dois **sem README**.

---

## Auditoria do `cogni.py` — as mesmas quatro falhas do NEXUS

Verificado no arquivo que você enviou. Não é suposição:

| # | Falha | Linha | Gravidade |
|---|---|---|---|
| 1 | TLS sem verificação, **em dobro** (`ssl_context` e `_ssl_ctx`) | 38, 73 | CRÍTICA |
| 2 | `os.startfile()` em qualquer caminho existente | 571 | ALTA |
| 3 | XSS armazenado no editor de notas (`innerHTML` direto) | 2928 | ALTA |
| 4 | Três CDNs, sem CSP, `chart.js` sem versão fixa | 605, 687, 689 | MÉDIA |

Zero ocorrências de criptografia no arquivo.

**A falha 1 em detalhe.** É a mesma que fazia e-mail e senha trafegarem
interceptáveis em qualquer Wi-Fi hostil. O Cogni tem dois contextos
permissivos em vez de um:

```python
ssl_context = ssl._create_unverified_context()   # linha 38
_ssl_ctx = ssl._create_unverified_context()      # linha 73
```

O app aceita qualquer certificado apresentado. Num Wi-Fi de aeroporto, o
atacante lê a senha em texto claro dentro do TLS. Também vale para o token de
acesso, que viaja na URL das chamadas de dados.

**A falha 2 em detalhe.** `os.startfile()` no Windows **executa** o arquivo com
o programa associado. Para `.exe`, `.bat`, `.ps1`, `.lnk` isso é execução de
código. E o caminho vem do banco, que sincroniza da nuvem.

**A falha 3 em detalhe.** O editor de notas grava HTML e o devolve com
`innerHTML`, sem handler de colar. Você copia texto formatado de uma página
web, vem `<img onerror=...>` junto, e isso **executa toda vez que a nota é
aberta** — em qualquer máquina sua. Num app pywebview o JavaScript alcança
`window.pywebview.api`, a ponte para o Python. Combinado com a falha 2: colar
um texto virava executar um programa.

---

## Caminhos definidos

| O quê | Onde |
|---|---|
| Código | `C:\Dev\cogni` |
| Banco de dados | `C:\COGNI\db` |
| Documentos | `C:\COGNI\docs` |
| Nó no Firebase | `/apps/cogni/users/<uid>/database` |
| Manifesto de atualização | `/apps/cogni/_release/stable` |

O `cogni.py` já aponta para `/apps/cogni/users/<uid>/database` — essa parte
estava correta desde o início. O `LOCAL_FALLBACK_PATH` estava em
`C:\Cogni\db\database.json`, que no Windows é o mesmo que `C:\COGNI\db`
(sistema não diferencia maiúsculas). A pasta `docs` passa a ser criada pelo
app.

---

## Limite desta sessão

O sandbox onde eu executo scripts caiu. Para o Cogni eu **não consigo
executar nada**: sem `py_compile`, sem teste de sintaxe, sem os testes de
regressão que usei no NEXUS.

Nesta conversa, cinco ferramentas minhas tiveram defeito, e nas cinco vezes
foi a **execução** que revelou — nunca a revisão. Trate tudo que eu entregar
para o Cogni como revisado, não testado, e rode os verificadores antes de
confiar.
