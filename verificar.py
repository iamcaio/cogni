# -*- coding: utf-8 -*-
r"""
Cogni - roda TODAS as verificacoes de uma vez.

Uso:
    cd C:\Dev\cogni
    python verificar.py

So Python. Nada a instalar.

------------------------------------------------------------------------------
A CHECAGEM 0 VEM PRIMEIRO DE PROPOSITO
------------------------------------------------------------------------------
Se o cogni.py nem compila, o resultado das outras checagens nao significa
nada. E ja aconteceu: um byte nulo escrito dentro de um comentario faz o
Python recusar o arquivo com "source code cannot contain null bytes", e nenhum
verificador que olha apenas o CONTEUDO perceberia.
------------------------------------------------------------------------------
"""
import collections
import io
import json
import os
import re
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
ALVO = os.path.join(RAIZ, 'cogni.py')

CONTROLE_OK = {9, 10, 13}

problemas = []
avisos = []


def ok(m):
    print("  OK     " + m)


def falha(m):
    print("  FALHA  " + m)
    problemas.append(m)


def aviso(m):
    print("  aviso  " + m)
    avisos.append(m)


def cabecalho(t):
    print()
    print("=" * 74)
    print(" " + t)
    print("=" * 74)


# ===========================================================================
#  0. INTEGRIDADE DOS ARQUIVOS
# ===========================================================================
def checar_integridade():
    cabecalho("0. INTEGRIDADE DOS ARQUIVOS DE TEXTO")

    alvos = []
    for pasta in (RAIZ, os.path.join(RAIZ, 'build_tools')):
        if not os.path.isdir(pasta):
            continue
        for nome in sorted(os.listdir(pasta)):
            if nome.endswith(('.py', '.ps1', '.iss', '.json', '.txt',
                              '.md', '.spec', '.js')):
                alvos.append(os.path.join(pasta, nome))

    locais = 0
    for caminho in alvos:
        rel = os.path.relpath(caminho, RAIZ)
        try:
            dados = open(caminho, 'rb').read()
        except OSError as e:
            falha("%s ilegivel: %s" % (rel, e))
            locais += 1
            continue

        ruins = collections.Counter(
            b for b in dados if b < 32 and b not in CONTROLE_OK)
        if ruins:
            b, _ = next(iter(ruins.items()))
            i = dados.find(bytes([b]))
            linha = dados[:i].count(b'\n') + 1
            falha("%s: byte 0x%02X na linha %d (%d no total)"
                  % (rel, b, linha, sum(ruins.values())))
            print("         %r" % dados[max(0, i - 45):i + 20])
            locais += 1
            continue

        if dados.startswith(b'\xef\xbb\xbf'):
            aviso("%s tem BOM UTF-8" % rel)

        # .ps1 SEM BOM precisa ser ASCII puro. O Windows PowerShell 5.1 le
        # esses arquivos como Windows-1252; um travessao UTF-8 vira tres
        # caracteres, e o ultimo (0x94) e uma aspa dupla tipografica que o
        # PowerShell ACEITA como delimitador de string. Cada acento vira uma
        # aspa fantasma e o parser morre numa linha sem defeito.
        if caminho.endswith('.ps1') and not dados.startswith(b'\xef\xbb\xbf'):
            fora = sum(1 for b in dados if b > 127)
            if fora:
                i = next(i for i, b in enumerate(dados) if b > 127)
                falha("%s: %d byte(s) nao-ASCII, 1o na linha %d"
                      % (rel, fora, dados[:i].count(b'\n') + 1))
                print("         O PowerShell 5.1 leria isso como Windows-1252")
                print("         e cada acento viraria uma aspa fantasma.")
                locais += 1
                continue

        if caminho.endswith('.py'):
            try:
                compile(dados, caminho, 'exec')
            except (SyntaxError, ValueError) as e:
                falha("%s nao compila" % rel)
                print("         %s: %s" % (type(e).__name__, str(e)[:100]))
                locais += 1
                continue

    if not locais:
        ok("%d arquivos: sem bytes de controle, todos os .py compilam" % len(alvos))


# ===========================================================================
#  1. SEGURANCA DO COGNI.PY
# ===========================================================================
def checar_seguranca(fonte):
    cabecalho("1. AS QUATRO CORRECOES DE SEGURANCA")

    # ---- Falha 1: TLS
    print(" TLS")
    if re.search(r'^_ssl_ctx\s*=\s*ssl\._create_unverified_context\(\)',
                 fonte, re.M):
        falha("ainda existe _ssl_ctx global sem verificacao")
    elif re.search(r'^ssl_context\s*=\s*ssl\._create_unverified_context\(\)',
                   fonte, re.M):
        falha("ainda existe ssl_context global sem verificacao")
    else:
        ok("nenhum contexto permissivo como padrao")

    for padrao, desc in [
        (r'def _montar_ssl_strict', "monta contexto com verificacao (certifi)"),
        (r'_ssl_ctx_strict\s*=\s*_montar_ssl_strict\(\)', "contexto estrito definido"),
        (r'permitir_fallback=False', "fallback desligado por padrao"),
        (r'_TLS_DEGRADADO', "marca quando a conexao degrada"),
    ]:
        if re.search(padrao, fonte):
            ok(desc)
        else:
            falha("AUSENTE: " + desc)

    # autenticacao NUNCA pode ter fallback
    for nome, ancora in [('signInWithPassword', 'signInWithPassword'),
                         ('signUp', r':signUp'),
                         ('refresh token', 'TOKEN_URL')]:
        i = fonte.find(ancora if isinstance(ancora, str) else ancora)
        if i < 0:
            aviso("nao localizei a chamada de %s" % nome)
            continue
        trecho = fonte[max(0, i - 250):i + 450]
        if 'permitir_fallback=True' in trecho:
            falha("%s permite fallback inseguro - a senha pode trafegar "
                  "sem verificacao" % nome)
        else:
            ok("%-18s nunca degrada" % nome)

    # a leitura do no legado tambem precisa passar pelo _http_json
    if re.search(r'context=ssl_context', fonte):
        falha("ainda existe urlopen com context=ssl_context (no legado)")
    else:
        ok("leitura do no legado usa o _http_json verificado")

    # ---- Falha 2: execucao de arquivos
    print("\n Abertura de arquivos")
    if re.search(r'EXTENSOES_EXECUTAVEIS', fonte):
        n = len(re.findall(r'"\.\w+"', re.search(
            r'EXTENSOES_EXECUTAVEIS = \{(.*?)\}', fonte, re.S).group(1)))
        ok("%d extensoes executaveis bloqueadas" % n)
    else:
        falha("AUSENTE: bloqueio de extensoes executaveis")

    if re.search(r'Tipo de link nao permitido', fonte):
        ok("esquemas perigosos recusados (javascript:, file:, ms-msdt:)")
    else:
        falha("AUSENTE: bloqueio de esquemas de URL perigosos")

    # ---- Falha 3: XSS
    print("\n Editor de notas")
    for padrao, desc in [
        (r'function sanitizarHtml', "funcao sanitizarHtml"),
        (r"createElement\('template'\)", "parse via <template>, sem executar"),
        (r"nome\.startsWith\('on'\)", "remove qualquer atributo on*"),
        (r'createTreeWalker', "percorre elementos aninhados"),
        (r'charCodeAt\(_i\) > 32', "limpa caracteres de controle na URL"),
        (r"addEventListener\('paste'", "intercepta o colar"),
        (r"addEventListener\('drop'", "intercepta o arrastar"),
        (r"innerHTML =\s*\n?\s*sanitizarHtml\(note\.content", "sanitiza ao abrir a nota"),
        (r'note\.content = sanitizarHtml', "sanitiza ao salvar a nota"),
        (r'instalarSanitizacaoDoEditor\(\)', "instala os interceptadores"),
    ]:
        if re.search(padrao, fonte):
            ok(desc)
        else:
            falha("AUSENTE: " + desc)

    # a lista de permissao nao pode ter tag perigosa
    m = re.search(r'const TAGS_PERMITIDAS = new Set\(\[(.*?)\]\)', fonte, re.S)
    if m:
        tags = {t.upper() for t in re.findall(r"'([^']+)'", m.group(1))}
        ruins = tags & {'SCRIPT', 'IFRAME', 'OBJECT', 'EMBED', 'FORM', 'SVG',
                        'MATH', 'LINK', 'META', 'STYLE', 'BASE', 'IMG',
                        'INPUT', 'BUTTON', 'VIDEO', 'AUDIO', 'APPLET'}
        if ruins:
            falha("tags perigosas permitidas: %s" % ', '.join(sorted(ruins)))
        else:
            ok("%d tags permitidas, nenhuma perigosa" % len(tags))
    else:
        falha("nao encontrei TAGS_PERMITIDAS")

    m = re.search(r'const ATRIBUTOS_PERMITIDOS = new Set\(\[(.*?)\]\)', fonte, re.S)
    if m:
        attrs = {a.lower() for a in re.findall(r"'([^']+)'", m.group(1))}
        ruins = {a for a in attrs
                 if a.startswith('on') or a in {'src', 'srcdoc', 'srcset',
                                                'action', 'formaction', 'data'}}
        if ruins:
            falha("atributos perigosos permitidos: %s" % ', '.join(sorted(ruins)))
        else:
            ok("%d atributos permitidos, nenhum perigoso" % len(attrs))

    # ---- Falha 4: CSP e CDN
    print("\n CSP e dependencias")
    n_csp = len(re.findall(r'Content-Security-Policy', fonte))
    n_janelas = len(re.findall(r'<title>Cogni', fonte))
    if n_csp >= n_janelas and n_csp > 0:
        ok("CSP em todas as %d janelas HTML" % n_janelas)
    else:
        falha("CSP em %d de %d janelas HTML" % (n_csp, n_janelas))

    if re.search(r'connect-src[^"]*firebaseio\.com', fonte):
        ok("connect-src limita os destinos aos hosts do Firebase")
    else:
        falha("CSP sem connect-src restrito")

    sem_versao = re.findall(r'cdn\.jsdelivr\.net/npm/([a-z.\-]+)(?=["\'])', fonte)
    if sem_versao:
        falha("CDN sem versao fixa: %s" % ', '.join(set(sem_versao)))
    else:
        ok("bibliotecas de CDN com versao fixa")


# ===========================================================================
#  2. BANCO E CAMINHOS
# ===========================================================================
def checar_caminhos(fonte):
    cabecalho("2. BANCO E CAMINHOS")

    pares = [
        ('namespace', r'APP_NAMESPACE = "([^"]+)"', 'cogni'),
        ('URL do banco', r'FIREBASE_DB_URL = "([^"]+)"',
         'https://cogni-data-default-rtdb.firebaseio.com'),
    ]
    for nome, padrao, esperado in pares:
        m = re.search(padrao, fonte)
        if not m:
            falha("%s: nao encontrado" % nome)
        elif m.group(1) != esperado:
            falha("%s = %r, esperado %r" % (nome, m.group(1), esperado))
        else:
            ok("%-14s %s" % (nome, m.group(1)))

    if (re.search(r'candidatos = \[.*?r"C:\\COGNI"', fonte, re.S)
            and 'os.path.join(_BASE_DADOS, "docs")' in fonte
            and 'os.path.join(_BASE_DADOS, "db", "database.json")' in fonte):
        ok("pastas de dados  C:\\COGNI (db, docs) com fallback em %LOCALAPPDATA%")
    else:
        falha("pastas de dados: C:\\COGNI com fallback nao encontrado")

    if re.search(r'/apps/\{APP_NAMESPACE\}/users/\{self\.uid\}/database', fonte):
        ok("caminho        /apps/cogni/users/<uid>/database")
    else:
        falha("o caminho do no de dados nao segue /apps/cogni/users/<uid>/database")

    mu = re.search(r'APP_UPDATE_VERSION = "v(\d+)\.(\d+)\.(\d+)"', fonte)
    if mu and tuple(int(x) for x in mu.groups()) > (4, 6, 9):
        ok("versao de atualizacao  v%s.%s.%s (maior que as instalacoes antigas 4.6.x)" % mu.groups())
    else:
        falha("APP_UPDATE_VERSION ausente ou <= 4.6.9: instalacoes antigas nao receberiam a atualizacao")

    if re.search(r'_release/\{UPDATE_CHANNEL\}', fonte):
        ok("manifesto      /apps/cogni/_release/<canal>")
    else:
        falha("manifesto de atualizacao nao aponta para /apps/cogni/_release")


# ===========================================================================
#  3. UPDATER
# ===========================================================================
def checar_updater(fonte):
    cabecalho("3. ATUALIZACAO AUTOMATICA")

    for padrao, desc in [
        (r'class AutoUpdater', "classe AutoUpdater"),
        (r'def _versao_maior', "comparacao semantica de versao"),
        (r'_versao_tupla', "versao convertida para tupla, nao texto"),
        (r'len\(esperado\) != 64', "exige SHA-256 de 64 caracteres"),
        (r'obtido != esperado', "compara o hash do arquivo baixado"),
        (r'descartado\. Nada foi instalado', "descarta o arquivo se o hash falhar"),
        (r'startswith\(UPDATE_URL_PREFIXO\)', "so aceita URL do repositorio oficial (HTTPS)"),
        (r'UPDATE_TAMANHO_MAX', "limite de tamanho do download"),
        (r'ctx=_ssl_ctx_strict', "manifesto lido com TLS verificado"),
        (r'context=_ssl_ctx_strict', "download com TLS verificado"),
        (r'/SILENT', "instalacao silenciosa"),
    ]:
        if re.search(padrao, fonte):
            ok(desc)
        else:
            falha("AUSENTE: " + desc)

    # o download NUNCA pode aceitar fallback inseguro
    i = fonte.find('def _baixar_e_aplicar_sync')
    if i > 0 and 'permitir_fallback=True' in fonte[i:i + 4000]:
        falha("o download da atualizacao permite fallback inseguro")
    elif i > 0:
        ok("o download nunca degrada o TLS")


# ===========================================================================
#  4. PONTE PYTHON <-> JS
# ===========================================================================
def checar_ponte(fonte):
    cabecalho("4. PONTE PYTHON <-> JAVASCRIPT")

    m = re.search(r'class CogniDesktopAPI.*?(?=\nHTML_CONTENT|\ndef main)',
                  fonte, re.S)
    if not m:
        aviso("nao localizei a classe CogniDesktopAPI para comparar")
        return
    metodos = set(re.findall(r'^    def ([a-zA-Z]\w*)\(', m.group(0), re.M))
    chamadas = set(re.findall(r'pywebview\.api\.([a-zA-Z_]\w*)', fonte))

    falta = sorted(c for c in chamadas if c not in metodos)
    if falta:
        falha("o JS chama metodos que nao existem em Python: %s"
              % ', '.join(falta))
    else:
        ok("%d chamadas do JS, todas com metodo em Python" % len(chamadas))

    dup = [n for n in metodos
           if len(re.findall(r'^    def %s\(' % n, m.group(0), re.M)) > 1]
    if dup:
        falha("metodos duplicados (o ultimo silenciosamente vence): %s"
              % ', '.join(dup))
    else:
        ok("nenhum metodo duplicado")

    for n in ('update_cached', 'update_check_now', 'update_install',
              'update_progress', 'update_channel_info', 'security_status',
              'get_docs_path', 'open_docs_folder'):
        if n not in metodos:
            falha("metodo novo ausente: %s" % n)


# ===========================================================================
#  5. VERSOES E REGRAS
# ===========================================================================
def checar_versoes_e_regras(fonte):
    cabecalho("5. VERSOES ALINHADAS")

    achados = {}
    m = re.search(r'APP_VERSION = "v([\d.]+)"', fonte)
    achados['cogni.py'] = m.group(1) if m else None

    for arq, padrao in [('version_info.txt', r"FileVersion', '([\d.]+)\.0'"),
                        ('cogni_installer.iss', r'MyAppVersion\s+"([\d.]+)"')]:
        try:
            t = io.open(os.path.join(RAIZ, arq), encoding='utf-8').read()
            mm = re.search(padrao, t)
            achados[arq] = mm.group(1) if mm else None
        except OSError:
            achados[arq] = None

    for k, v in achados.items():
        print("  %-22s %s" % (k, v or "NAO ENCONTRADA"))
    distintas = {v for v in achados.values() if v}
    if len(distintas) == 1 and None not in achados.values():
        ok("todas em %s" % distintas.pop())
    else:
        falha("versoes divergentes - rode: .\\build.ps1 -Version X.Y.Z -OnlyVersion")

    cabecalho("6. REGRAS DO FIREBASE")
    try:
        d = json.load(io.open(os.path.join(RAIZ, 'firebase-rules.json'),
                              encoding='utf-8'))
        apps = d['rules']['apps']

        if apps['cogni']['_release'].get('.read') is not True:
            falha("apps/cogni/_release/.read deve ser true: o app checa a "
                  "versao ANTES do login")
        else:
            ok("_release do Cogni tem leitura publica")

        if '.write' in apps['cogni']['_release']:
            aviso("apps/cogni/_release tem .write; sem ele, so o console "
                  "escreve - que e o desejado")
        else:
            ok("_release sem .write: nenhum token grava ali")

        r = apps['cogni']['users']['$uid']
        for dd in ('.read', '.write'):
            if 'auth.uid == $uid' not in str(r.get(dd, '')):
                falha("apps/cogni/users/$uid%s sem 'auth.uid == $uid': um "
                      "usuario leria os dados de outro" % dd)
        if all('auth.uid == $uid' in str(r.get(dd, ''))
               for dd in ('.read', '.write')):
            ok("isolamento por UID no ramo do Cogni")

        if 'nexus' in apps:
            ok("ramo do NEXUS preservado nas regras")
        else:
            aviso("as regras nao mencionam o ramo do NEXUS; publicar este "
                  "arquivo tiraria o acesso do outro app")
    except Exception as e:
        falha("firebase-rules.json: %s" % e)


def main():
    if not os.path.exists(ALVO):
        print("cogni.py nao encontrado em %s" % RAIZ)
        return 2

    print()
    print("#" * 74)
    print("#  Cogni - verificacao completa")
    print("#  pasta: %s" % RAIZ)
    print("#" * 74)

    checar_integridade()

    fonte = io.open(ALVO, encoding='utf-8').read()
    checar_seguranca(fonte)
    checar_caminhos(fonte)
    checar_updater(fonte)
    checar_ponte(fonte)
    checar_versoes_e_regras(fonte)

    print()
    print("=" * 74)
    if problemas:
        print(" %d PROBLEMA(S) - NAO COMPILE NEM PUBLIQUE" % len(problemas))
        for p in problemas:
            print("   x " + p)
    else:
        print(" TUDO OK")
        print()
        print(" O que isto NAO cobre:")
        print("   - nao executa o app: abra o COGNI.exe e confirme que")
        print("     suas anotacoes aparecem e que o login funciona")
        print("   - nao valida a sintaxe do JavaScript embutido")
        print("   - nao testa a atualizacao de ponta a ponta")
    if avisos:
        print("\n %d aviso(s)." % len(avisos))
    print("=" * 74)
    print()
    return 1 if problemas else 0


if __name__ == '__main__':
    sys.exit(main())
