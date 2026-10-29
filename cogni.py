import sys
import os
import json
import re
import time
import pathlib
import traceback
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.error
import ssl

def show_native_error_dialog(title, message):
    """Exibe um alerta nativo do Windows em caso de erro critico de inicializacao."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0x10)
    except Exception:
        print(f"[{title}] {message}")

try:
    import webview
except Exception as e:
    err_msg = (
        "Nao foi possivel carregar a biblioteca 'pywebview'.\n\n"
        f"Detalhes do Erro:\n{traceback.format_exc()}\n\n"
        "Por favor, execute 'pip install pywebview' no terminal do seu computador."
    )
    show_native_error_dialog("Erro de Inicializacao - Cogni", err_msg)
    sys.exit(1)

APP_VERSION = "v4.5.8"

# Caminho legado: onde o Cogni gravava antes de existir login. Continua no
# banco como copia de seguranca; nada e apagado dele.
LEGACY_URL = "https://cogni-data-default-rtdb.firebaseio.com/database.json"
LOCAL_FALLBACK_PATH = r"C:\COGNI\db\database.json"

# Pasta de documentos do usuario. Criada no boot; o desinstalador nao a apaga.
DOCS_PATH = r"C:\COGNI\docs"

# Onde o HTML da interface e escrito em disco antes de ser aberto na janela
# (ver _escrever_ui_local). Fica junto com os outros dados do usuario, nao
# dentro da pasta de instalacao - assim funciona mesmo que o app esteja
# instalado num lugar sem permissao de escrita.
UI_LOCAL_PATH = r"C:\COGNI\ui"

# ---------------------------------------------------------------------------
# NOTIFICACOES DESKTOP (Windows Toast) - mesmo padrao validado no NEXUS.
# AUMID precisa ser IDENTICO ao do cogni_installer.iss, senao a notificacao
# sai assinada como "Windows PowerShell" em vez de "Cogni".
# ---------------------------------------------------------------------------
APP_AUMID = "iamcaio.Cogni"


def _registrar_identidade_windows():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_AUMID)
        return True
    except Exception as e:
        print(f"[Cogni] Nao foi possivel registrar o AppUserModelID: {e}")
        return False


class WindowsNotifier:
    """Dispara notificacoes nativas do Windows (Toast) em segundo plano,
    para avisar sobre tarefas com prazo perto ou vencido."""
    @staticmethod
    def notify(title, message, sound=True):
        def _run():
            try:
                clean_title = str(title or "Cogni").replace('"', '`"').replace('$', '`$').replace("'", "`'")
                clean_msg = str(message or "").replace('"', '`"').replace('$', '`$').replace("'", "`'")
                audio_tag = '<audio src="ms-winsoundevent:Notification.Default" />' if sound else '<audio silent="true" />'
                ps_script = f'''
                try {{
                    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
                    $template = @"
                    <toast>
                        <visual>
                            <binding template="ToastGeneric">
                                <text>{clean_title}</text>
                                <text>{clean_msg}</text>
                            </binding>
                        </visual>
                        {audio_tag}
                    </toast>
"@
                    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
                    $xml.LoadXml($template)
                    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
                    try {{
                        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{APP_AUMID}")
                        $notifier.Show($toast)
                    }} catch {{
                        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe")
                        $notifier.Show($toast)
                    }}
                }} catch {{ }}
                '''
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                    capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            except Exception as e:
                print(f"[Notifier] Erro ao disparar notificacao: {e}")
        threading.Thread(target=_run, daemon=True).start()

# ---------------------------------------------------------------------------
# INTERFACE 100% LOCAL - SEM DEPENDER DE INTERNET PARA ABRIR
# ---------------------------------------------------------------------------
# Antes, Tailwind, Font Awesome, Chart.js e a fonte Inter vinham de CDNs
# (cdn.tailwindcss.com, cdnjs.cloudflare.com, jsdelivr.net, fonts.googleapis).
# Sem internet no momento de abrir o app, a interface carregava sem estilo
# nenhum - texto cru, sem icones, sem fonte. Pior: cdn.tailwindcss.com manda
# um COMPILADOR Tailwind que roda no navegador a cada abertura, nunca um
# arquivo fixo - ou seja, dependia da rede toda vez, nao so na primeira.
#
# Esses quatro pacotes agora vao dentro de vendor\ (empacotados pelo
# PyInstaller via cogni.spec) e o Tailwind foi pre-compilado para um CSS
# estatico - sem compilador rodando no cliente, sem rede nenhuma.
#
# A nuvem (Firebase: login, dados, atualizacao) continua exatamente igual,
# porque e outra coisa: a interface abrir sem internet nao significa guardar
# tarefas sem internet - isso continua exigindo login na conta.
# ---------------------------------------------------------------------------
def _recursos_dir():
    """Pasta onde os arquivos empacotados pelo PyInstaller (datas do
    cogni.spec, incluindo vendor\\) ficam disponiveis em tempo de execucao.
    Em modo desenvolvimento (python cogni.py direto, sem build), cai na
    pasta do proprio script."""
    base = getattr(sys, '_MEIPASS', None)
    if base:
        return base
    return os.path.dirname(os.path.abspath(__file__))


def _vendor_uri(caminho_relativo):
    """URI file:// absoluta para um arquivo dentro de vendor/ (Tailwind,
    Font Awesome, Chart.js, Inter). Absoluta de proposito: assim o HTML
    escrito em C:\\COGNI\\ui nao depende de estar numa posicao relativa
    exata em relacao a pasta de instalacao do app."""
    partes = caminho_relativo.split('/')
    caminho = os.path.join(_recursos_dir(), 'vendor', *partes)
    return pathlib.Path(caminho).as_uri()


def _localizar_assets_html(html_str):
    """Troca as tags <script src="https://..."> / <link href="https://...">
    dos quatro CDNs pelos arquivos locais em vendor\\. Funciona tanto no
    HTML_CONTENT (janela principal) quanto no HTML menor da janela de
    Timer (PiP) - se uma tag nao existir no texto, o replace() so nao
    encontra nada e segue sem erro."""
    trocas = [
        (
            '<script src="https://cdn.tailwindcss.com/3.4.5"></script>',
            f'<link rel="stylesheet" href="{_vendor_uri("tailwind.local.css")}">'
        ),
        (
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">',
            f'<link rel="stylesheet" href="{_vendor_uri("fontawesome/css/all.min.css")}">'
        ),
        (
            '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>',
            f'<script src="{_vendor_uri("chart.umd.min.js")}"></script>'
        ),
        (
            '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">',
            f'<link rel="stylesheet" href="{_vendor_uri("fonts/inter.css")}">'
        ),
        # CSP: os quatro CDNs saem das diretivas, 'file:' entra - os
        # recursos agora chegam por arquivo local, nunca mais por rede.
        (
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net;",
            "script-src 'self' file: 'unsafe-inline' 'unsafe-eval';"
        ),
        (
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com;",
            "script-src 'self' file: 'unsafe-inline' 'unsafe-eval';"
        ),
        (
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com;",
            "style-src 'self' file: 'unsafe-inline';"
        ),
        (
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com;",
            "style-src 'self' file: 'unsafe-inline';"
        ),
        (
            "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com;",
            "font-src 'self' file: data:;"
        ),
        (
            "font-src 'self' data: https://cdnjs.cloudflare.com;",
            "font-src 'self' file: data:;"
        ),
    ]
    for antigo, novo in trocas:
        html_str = html_str.replace(antigo, novo)
    return html_str


def _escrever_ui_local(nome_arquivo, html_str):
    """Grava o HTML (ja com os assets localizados) em C:\\COGNI\\ui\\ e
    devolve a URI file:// dele.

    Precisa ser um ARQUIVO DE VERDADE em disco, nao html= passado direto
    para o pywebview: um documento sem uma origem de arquivo nao tem como
    resolver os caminhos file:// dos vendor_uri acima do jeito que todo
    navegador espera, e o comportamento varia entre motor de renderizacao.
    Escrever sempre no boot (sobrescrevendo) garante que uma atualizacao do
    .exe nunca deixe um index.html velho para tras.
    """
    os.makedirs(UI_LOCAL_PATH, exist_ok=True)
    caminho = os.path.join(UI_LOCAL_PATH, nome_arquivo)
    html_final = _localizar_assets_html(html_str)
    with open(caminho, 'w', encoding='utf-8') as f:
        f.write(html_final)
    return pathlib.Path(caminho).as_uri()

# ---------------------------------------------------------------------------
# CAMADA DE NUVEM (Firebase Realtime Database + Firebase Authentication)
# ---------------------------------------------------------------------------
# O MESMO banco atende os dois aplicativos sem misturar dados porque cada um
# grava embaixo do seu próprio ramo, e cada usuário embaixo do próprio UID:
#
#   /apps/cogni/users/<uid>/...       <- este aplicativo
#   /apps/donext/users/<uid>/...      <- o doNext
#   /database/...                     <- onde o Cogni grava hoje (preservado)
#
# Os dados antigos em /database sao PRESERVADOS e copiados para o espaco do
# primeiro usuario que entrar; nada e apagado de la.
# ---------------------------------------------------------------------------

APP_NAMESPACE = "cogni"

# ---------------------------------------------------------------------------
# Credenciais do projeto Firebase, já embutidas: o usuário final só informa
# e-mail e senha.
#
# Esta chave NÃO é secreta — ela apenas identifica o projeto e é visível em
# qualquer app Firebase. Quem impede o acesso aos dados são as Regras do
# Realtime Database, que exigem login e prendem cada usuário ao próprio UID.
# Por isso é essencial manter as Regras publicadas (firebase-rules.json).
# ---------------------------------------------------------------------------
FIREBASE_API_KEY = "AIzaSyAvB7jmXgziIMSG3JptOznthGQAP76LWXM"
FIREBASE_DB_URL = "https://cogni-data-default-rtdb.firebaseio.com"

IDENTITY_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token"

# ---------------------------------------------------------------------------
# ATUALIZACAO AUTOMATICA
# ---------------------------------------------------------------------------
# O manifesto de versao mora no MESMO Firebase que voce ja administra, num no
# de leitura publica. Publicar uma nova versao passa a ser: subir o instalador
# no repositorio publico e editar um JSON de 6 linhas no console do Firebase.
#
#   /apps/cogni/_release/stable  ->  { version, url, sha256, notes, ... }
#
# O binario NAO fica no Firebase (banda cara). O campo "url" aponta para o
# GitHub Releases do repositorio publico cogni-dist.
#
# CANAL "beta": troque UPDATE_CHANNEL para testar antes de liberar para todos.
# ---------------------------------------------------------------------------
UPDATE_CHANNEL = "stable"
UPDATE_MANIFEST_URL = (
    f"{FIREBASE_DB_URL}/apps/{APP_NAMESPACE}/_release/{UPDATE_CHANNEL}.json"
)

# Intervalo minimo entre checagens automaticas (segundos). 6 horas.
UPDATE_CHECK_INTERVALO = 6 * 60 * 60

# Tamanho maximo aceito para o download, em bytes. Barreira contra um
# manifesto adulterado apontando para um arquivo gigante.
UPDATE_TAMANHO_MAX = 400 * 1024 * 1024


def _versao_tupla(v):
    """'v4.5.1' -> (4, 5, 1). Trecho nao-numerico vira 0. Nunca levanta."""
    try:
        limpo = str(v or "").strip().lstrip("vV").split("-")[0].split("+")[0]
        partes = []
        for p in limpo.split(".")[:4]:
            digitos = "".join(c for c in p if c.isdigit())
            partes.append(int(digitos) if digitos else 0)
        while len(partes) < 3:
            partes.append(0)
        return tuple(partes)
    except Exception:
        return (0, 0, 0)


def _versao_maior(a, b):
    """True se a > b comparando semanticamente, nao como texto.

    Detalhe que quebra comparacao por string: '4.10.0' > '4.9.0' e verdadeiro
    numericamente e FALSO alfabeticamente. Por isso a comparacao e por tupla.
    """
    return _versao_tupla(a) > _versao_tupla(b)


class AutoUpdater:
    """
    Verificacao e aplicacao de atualizacoes do Cogni.

    Modelo de confianca, explicitado de proposito:
      * O manifesto e lido por HTTPS COM verificacao de certificado.
      * O instalador e baixado por HTTPS COM verificacao de certificado.
      * O arquivo baixado SO e executado se o SHA-256 conferir com o do
        manifesto. Sem hash no manifesto, o app se recusa a instalar.
      * O download vai para uma pasta do usuario, nunca para Program Files.

    Sem o passo do SHA-256, um auto-updater e uma porta de execucao remota de
    codigo. Ele nao e opcional.
    """

    def __init__(self, pasta_dados):
        import tempfile
        self.pasta = os.path.join(pasta_dados, "updates")
        try:
            os.makedirs(self.pasta, exist_ok=True)
        except Exception:
            self.pasta = tempfile.gettempdir()
        self.manifesto = None
        self.ultimo_erro = None
        self.ultima_checagem = 0
        self._prog = {"fase": "idle", "pct": 0, "recebido": 0,
                      "total": 0, "mensagem": ""}
        import threading
        self._lock = threading.Lock()

    # ------------------------------------------------------------ estado
    def _set(self, **kw):
        with self._lock:
            self._prog.update(kw)

    def progresso(self):
        with self._lock:
            return dict(self._prog)

    # --------------------------------------------------------- manifesto
    def verificar(self, forcar=False):
        """
        Le o manifesto e diz se existe versao nova.

        Devolve sempre um dict com 'status':
          up_to_date | update_available | error
        """
        agora = time.time()
        if not forcar and (agora - self.ultima_checagem) < UPDATE_CHECK_INTERVALO \
                and self.manifesto is not None:
            return self._resposta_do_manifesto()

        if _ssl_ctx_strict is None:
            return {"status": "error", "current": APP_VERSION,
                    "message": "Nao foi possivel montar um contexto TLS seguro "
                               "nesta maquina. Atualizacao desativada por seguranca."}

        st, corpo = _http_json(UPDATE_MANIFEST_URL, timeout=12,
                               ctx=_ssl_ctx_strict)
        self.ultima_checagem = agora

        if st != 200:
            self.ultimo_erro = f"HTTP {st} ao ler o manifesto."
            return {"status": "error", "current": APP_VERSION,
                    "message": "Nao foi possivel consultar atualizacoes agora. "
                               "Verifique a conexao."}
        if not isinstance(corpo, dict) or not corpo.get("version"):
            self.manifesto = None
            return {"status": "up_to_date", "current": APP_VERSION,
                    "message": "Nenhuma atualizacao publicada."}

        self.manifesto = corpo
        return self._resposta_do_manifesto()

    def _resposta_do_manifesto(self):
        m = self.manifesto or {}
        nova = m.get("version")
        if not nova or not _versao_maior(nova, APP_VERSION):
            return {"status": "up_to_date", "current": APP_VERSION,
                    "latest": nova or APP_VERSION}
        return {
            "status": "update_available",
            "current": APP_VERSION,
            "latest": nova,
            "notes": m.get("notes") or "",
            "size": m.get("size") or 0,
            "mandatory": bool(m.get("mandatory")),
            "hasHash": bool(m.get("sha256")),
        }

    # ---------------------------------------------------------- download
    def baixar_e_aplicar(self):
        """Roda em thread separada. Acompanhe por progresso()."""
        import threading
        t = threading.Thread(target=self._fluxo, daemon=True)
        t.start()
        return {"status": "started"}

    def _fluxo(self):
        try:
            self._baixar_e_aplicar_sync()
        except Exception as e:
            self._set(fase="error", pct=0,
                      mensagem=f"{type(e).__name__}: {e}")

    def _baixar_e_aplicar_sync(self):
        import hashlib
        import subprocess

        m = self.manifesto
        if not m:
            self.verificar(forcar=True)
            m = self.manifesto
            if not m:
                self._set(fase="error", mensagem="Nenhuma versao publicada.")
                return

        url = (m.get("url") or "").strip()
        esperado = (m.get("sha256") or "").strip().lower()

        if not url.lower().startswith("https://"):
            self._set(fase="error",
                      mensagem="O manifesto aponta para uma URL nao-HTTPS. "
                               "Instalacao bloqueada.")
            return
        if len(esperado) != 64:
            self._set(fase="error",
                      mensagem="O manifesto nao traz um SHA-256 valido. "
                               "Instalacao bloqueada por seguranca.")
            return
        if _ssl_ctx_strict is None:
            self._set(fase="error",
                      mensagem="Sem TLS verificavel nesta maquina. "
                               "Baixe a atualizacao manualmente.")
            return

        nome = os.path.basename(url.split("?")[0]) or "COGNI-update.exe"
        destino = os.path.join(self.pasta, nome)
        parcial = destino + ".part"

        self._set(fase="downloading", pct=0, recebido=0, total=0,
                  mensagem="Baixando atualizacao...")

        h = hashlib.sha256()
        recebido = 0
        req = urllib.request.Request(
            url, headers={"User-Agent": f"CogniDesktop/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=60,
                                    context=_ssl_ctx_strict) as resp:
            total = int(resp.headers.get("Content-Length") or m.get("size") or 0)
            if total and total > UPDATE_TAMANHO_MAX:
                self._set(fase="error",
                          mensagem="Arquivo maior que o limite permitido.")
                return
            self._set(total=total)
            with open(parcial, "wb") as f:
                while True:
                    bloco = resp.read(262144)
                    if not bloco:
                        break
                    recebido += len(bloco)
                    if recebido > UPDATE_TAMANHO_MAX:
                        f.close()
                        os.remove(parcial)
                        self._set(fase="error",
                                  mensagem="Download excedeu o limite permitido.")
                        return
                    h.update(bloco)
                    f.write(bloco)
                    pct = int(recebido * 100 / total) if total else 0
                    self._set(pct=min(pct, 99), recebido=recebido)

        obtido = h.hexdigest().lower()
        if obtido != esperado:
            try:
                os.remove(parcial)
            except Exception:
                pass
            self._set(fase="error", pct=0,
                      mensagem="A verificacao de integridade FALHOU. O arquivo "
                               "baixado nao corresponde ao publicado e foi "
                               "descartado. Nada foi instalado.")
            return

        try:
            if os.path.exists(destino):
                os.remove(destino)
            os.rename(parcial, destino)
        except Exception as e:
            self._set(fase="error", mensagem=f"Falha ao salvar: {e}")
            return

        self._set(fase="verified", pct=100,
                  mensagem="Integridade confirmada. Iniciando instalacao...")
        time.sleep(0.6)

        # /SILENT              -> sem assistente, so a barra de progresso
        # /CLOSEAPPLICATIONS   -> fecha o Cogni em execucao
        # /RESTARTAPPLICATIONS -> reabre o Cogni ao terminar
        # /NORESTART           -> nunca reinicia o Windows
        try:
            self._set(fase="installing", pct=100,
                      mensagem="O instalador vai abrir e o Cogni sera reiniciado.")
            subprocess.Popen(
                [destino, "/SILENT", "/CLOSEAPPLICATIONS",
                 "/RESTARTAPPLICATIONS", "/NORESTART"],
                close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            time.sleep(1.5)
            os._exit(0)
        except Exception as e:
            self._set(fase="error",
                      mensagem=f"Nao foi possivel iniciar o instalador: {e}")

# ---------------------------------------------------------------------------
# CONTEXTOS SSL
# ---------------------------------------------------------------------------
# Antes havia DOIS contextos sem verificacao de certificado: um `ssl_context`
# global e este `_ssl_ctx`. O app aceitava qualquer certificado apresentado.
#
# Isso significava que o e-mail e a SENHA enviados no login, e o token de
# acesso embutido na URL dos dados, podiam ser lidos por qualquer
# intermediario na rede: Wi-Fi de aeroporto, roteador comprometido, proxy
# corporativo que intercepta TLS. O atacante so precisava apresentar um
# certificado qualquer.
#
# Agora existem dois contextos com propositos distintos:
#   _ssl_ctx_strict   -> verificacao real. E o PADRAO para tudo.
#   _ssl_ctx_inseguro -> ultimo recurso, SOMENTE para trafego de dados, nunca
#                        para autenticacao nem para atualizacao, e sempre
#                        deixando rastro em _TLS_DEGRADADO.
#
# O certifi vai empacotado no executavel (ver cogni.spec), entao a verificacao
# funciona mesmo quando o repositorio de certificados do Windows esta quebrado
# — que era o motivo original da gambiarra.
# ---------------------------------------------------------------------------
def _montar_ssl_strict():
    """Contexto com verificacao real de certificado. Usa certifi se existir."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return None


_ssl_ctx_strict = _montar_ssl_strict()
_ssl_ctx_inseguro = ssl._create_unverified_context()

# Vira True se alguma requisicao de DADOS teve de ser repetida sem verificar o
# certificado. A interface mostra um aviso: conexao sem verificacao nao pode
# ser silenciosa.
_TLS_DEGRADADO = False
_TLS_MOTIVO = ""


def _http_json(url, payload=None, method="GET", timeout=15, ctx=None,
               permitir_fallback=False):
    """Requisição HTTP simples que devolve (status, dict|str). Nunca levanta.

    O padrao e VERIFICAR o certificado. `permitir_fallback=True` autoriza, em
    ultimo caso, repetir sem verificacao — usado apenas para leitura e escrita
    de dados, para o app nao travar numa rede problematica. NUNCA para
    autenticacao: ali a conexao falha em vez de degradar.
    """
    global _TLS_DEGRADADO, _TLS_MOTIVO
    data = None
    headers = {"User-Agent": f"CogniDesktop/{APP_VERSION}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    def _executar(contexto):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=contexto) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw) if raw.strip() else None
            except json.JSONDecodeError:
                return resp.status, raw

    contexto = ctx or _ssl_ctx_strict
    try:
        if contexto is None:
            raise ssl.SSLError("Nenhum contexto TLS verificavel disponivel.")
        return _executar(contexto)
    except urllib.error.HTTPError as e:
        try:
            corpo = json.loads(e.read().decode("utf-8"))
        except Exception:
            corpo = {"error": {"message": str(e)}}
        return e.code, corpo
    except (ssl.SSLError, ssl.SSLCertVerificationError) as e:
        if not permitir_fallback:
            return 0, {"error": {"message":
                "Nao foi possivel validar o certificado do servidor "
                f"({type(e).__name__}). A conexao foi recusada por seguranca. "
                "Verifique a data e a hora do computador e, se estiver em rede "
                "corporativa, se ha um proxy interceptando HTTPS."}}
        try:
            resultado = _executar(_ssl_ctx_inseguro)
            _TLS_DEGRADADO = True
            _TLS_MOTIVO = f"{type(e).__name__}: {e}"
            print(f"[COGNI TLS] AVISO: conexao sem verificacao de certificado. {e}")
            return resultado
        except Exception as e2:
            return 0, {"error": {"message": f"{type(e2).__name__}: {e2}"}}
    except urllib.error.URLError as e:
        motivo = getattr(e, "reason", e)
        if isinstance(motivo, ssl.SSLError) and permitir_fallback:
            try:
                resultado = _executar(_ssl_ctx_inseguro)
                _TLS_DEGRADADO = True
                _TLS_MOTIVO = str(motivo)
                print(f"[COGNI TLS] AVISO: conexao sem verificacao. {motivo}")
                return resultado
            except Exception as e2:
                return 0, {"error": {"message": f"{type(e2).__name__}: {e2}"}}
        return 0, {"error": {"message": f"{type(e).__name__}: {motivo}"}}
    except Exception as e:
        return 0, {"error": {"message": f"{type(e).__name__}: {e}"}}


def _mensagem_amigavel(codigo):
    """Traduz os códigos do Firebase Auth para algo legível."""
    return {
        "EMAIL_EXISTS": "Este e-mail já está cadastrado. Use 'Entrar'.",
        "EMAIL_NOT_FOUND": "E-mail não encontrado. Crie uma conta primeiro.",
        "INVALID_PASSWORD": "Senha incorreta.",
        "INVALID_LOGIN_CREDENTIALS": "E-mail ou senha incorretos.",
        "USER_DISABLED": "Esta conta foi desativada. Entre em contato com o suporte.",
        "WEAK_PASSWORD : Password should be at least 6 characters":
            "A senha precisa ter ao menos 6 caracteres.",
        "INVALID_EMAIL": "E-mail inválido.",
        "TOO_MANY_ATTEMPTS_TRY_LATER : Access to this account has been temporarily disabled due to many failed login attempts. You can immediately restore it by resetting your password or you can try again later.":
            "Muitas tentativas. Aguarde alguns minutos e tente de novo.",
        "OPERATION_NOT_ALLOWED":
            "Login por e-mail/senha está temporariamente indisponível. Tente novamente mais tarde.",
        "TOKEN_EXPIRED": "Sessão expirada. Entre novamente.",
    }.get(str(codigo), str(codigo))


class CloudSync:
    """
    Autenticação e sincronização com o Firebase.

    Princípio de segurança adotado: a nuvem NUNCA sobrescreve dados locais bons
    por engano. A decisão de quem vence usa o carimbo `_meta.updatedAt`, e um
    lado vazio jamais substitui um lado com conteúdo.
    """

    def __init__(self, pasta_db):
        self.config_file = os.path.join(pasta_db, "cloud_config.json")
        self.session_file = os.path.join(pasta_db, "cloud_session.json")
        # Já vem embutido: o usuário final só informa e-mail e senha.
        self.api_key = FIREBASE_API_KEY.strip()
        self.db_url = FIREBASE_DB_URL.strip().rstrip("/")
        self.id_token = None
        self.refresh_token = None
        self.uid = None
        self.email = None
        self.expira_em = 0
        self.ultimo_erro = ""
        self._carregar_config()
        self._carregar_sessao()

    # -------------------------- configuração --------------------------- #
    def _carregar_config(self):
        """
        Sobrescrita opcional. Serve para apontar o app para outro projeto sem
        recompilar; se o arquivo não existir, valem as constantes embutidas.
        """
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                chave = (cfg.get("apiKey") or "").strip()
                url = (cfg.get("databaseURL") or "").strip().rstrip("/")
                if chave:
                    self.api_key = chave
                if url:
                    self.db_url = url
        except Exception as e:
            print(f"[Cloud] Config ilegível: {e}")

    def salvar_config(self, api_key, db_url):
        self.api_key = (api_key or "").strip()
        self.db_url = (db_url or "").strip().rstrip("/")
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({"apiKey": self.api_key, "databaseURL": self.db_url}, f, indent=2)
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @property
    def configurado(self):
        return bool(self.api_key and self.db_url and not self.api_key.startswith("COLE_AQUI"))

    @property
    def logado(self):
        return bool(self.uid and self.refresh_token)

    # ---------------------------- sessão ------------------------------- #
    def _carregar_sessao(self):
        try:
            if os.path.exists(self.session_file):
                with open(self.session_file, "r", encoding="utf-8") as f:
                    s = json.load(f)
                self.refresh_token = s.get("refreshToken")
                self.uid = s.get("uid")
                self.email = s.get("email")
        except Exception as e:
            print(f"[Cloud] Sessão ilegível: {e}")

    def _salvar_sessao(self):
        try:
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump({"refreshToken": self.refresh_token,
                           "uid": self.uid, "email": self.email}, f, indent=2)
        except Exception as e:
            print(f"[Cloud] Falha ao gravar sessão: {e}")

    def _limpar_sessao(self):
        self.id_token = self.refresh_token = self.uid = self.email = None
        self.expira_em = 0
        try:
            if os.path.exists(self.session_file):
                os.remove(self.session_file)
        except Exception:
            pass

    # --------------------------- autenticação -------------------------- #
    def _aplicar_resposta_auth(self, corpo):
        self.id_token = corpo.get("idToken")
        self.refresh_token = corpo.get("refreshToken")
        self.uid = corpo.get("localId") or corpo.get("user_id")
        if corpo.get("email"):
            self.email = corpo["email"]
        try:
            self.expira_em = time.time() + int(corpo.get("expiresIn", 3600)) - 120
        except Exception:
            self.expira_em = time.time() + 3300
        self._salvar_sessao()

    def criar_conta(self, email, senha):
        if not self.configurado:
            return {"status": "error", "message": "Configure a chave da API e a URL do banco primeiro."}
        st, corpo = _http_json(f"{IDENTITY_URL}:signUp?key={self.api_key}",
                               {"email": email, "password": senha, "returnSecureToken": True},
                               method="POST")
        if st == 200 and isinstance(corpo, dict) and corpo.get("idToken"):
            self._aplicar_resposta_auth(corpo)
            return {"status": "success", "uid": self.uid, "email": self.email}
        msg = (corpo or {}).get("error", {}).get("message", "Falha ao criar conta.")
        return {"status": "error", "message": _mensagem_amigavel(msg)}

    def entrar(self, email, senha):
        if not self.configurado:
            return {"status": "error", "message": "Configure a chave da API e a URL do banco primeiro."}
        st, corpo = _http_json(f"{IDENTITY_URL}:signInWithPassword?key={self.api_key}",
                               {"email": email, "password": senha, "returnSecureToken": True},
                               method="POST")
        if st == 200 and isinstance(corpo, dict) and corpo.get("idToken"):
            self._aplicar_resposta_auth(corpo)
            return {"status": "success", "uid": self.uid, "email": self.email}
        msg = (corpo or {}).get("error", {}).get("message", "Falha ao entrar.")
        return {"status": "error", "message": _mensagem_amigavel(msg)}

    def sair(self):
        self._limpar_sessao()
        return {"status": "success"}

    def _token_valido(self):
        """Garante um idToken vivo, renovando pelo refreshToken quando preciso."""
        if not self.refresh_token:
            return None
        if self.id_token and time.time() < self.expira_em:
            return self.id_token
        st, corpo = _http_json(
            f"{TOKEN_URL}?key={self.api_key}",
            {"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            method="POST")
        if st == 200 and isinstance(corpo, dict) and corpo.get("id_token"):
            self.id_token = corpo["id_token"]
            self.refresh_token = corpo.get("refresh_token", self.refresh_token)
            self.uid = corpo.get("user_id", self.uid)
            try:
                self.expira_em = time.time() + int(corpo.get("expires_in", 3600)) - 120
            except Exception:
                self.expira_em = time.time() + 3300
            self._salvar_sessao()
            return self.id_token
        self.ultimo_erro = _mensagem_amigavel((corpo or {}).get("error", {}).get("message", "TOKEN_EXPIRED"))
        return None

    # ------------------------------ dados ------------------------------ #
    def _caminho_dados(self, token):
        # Cada app no seu ramo, cada usuário no seu UID: sem colisão.
        return f"{self.db_url}/apps/{APP_NAMESPACE}/users/{self.uid}/database.json?auth={token}"

    def baixar(self):
        if not (self.configurado and self.logado):
            return {"status": "offline", "message": "Não conectado."}
        token = self._token_valido()
        if not token:
            return {"status": "auth_error", "message": self.ultimo_erro or "Sessão expirada."}
        # permitir_fallback: leitura de DADOS pode, em ultimo caso, seguir sem
        # verificacao para o app nao travar numa rede problematica. Isso acende
        # _TLS_DEGRADADO e a interface avisa. Autenticacao nunca cai aqui.
        st, corpo = _http_json(self._caminho_dados(token),
                               permitir_fallback=True)
        if st == 200:
            if corpo is None:
                return {"status": "empty"}
            if isinstance(corpo, dict):
                return {"status": "ok", "data": corpo}
            return {"status": "error", "message": "Formato inesperado vindo da nuvem."}
        if st in (401, 403):
            return {"status": "auth_error",
                    "message": "Sem permissão. Confira as Regras do Realtime Database."}
        return {"status": "error",
                "message": (corpo or {}).get("error", f"HTTP {st}") if isinstance(corpo, dict) else f"HTTP {st}"}

    def enviar(self, dados_dict):
        if not (self.configurado and self.logado):
            return {"status": "offline", "message": "Não conectado."}
        token = self._token_valido()
        if not token:
            return {"status": "auth_error", "message": self.ultimo_erro or "Sessão expirada."}
        st, corpo = _http_json(self._caminho_dados(token), dados_dict,
                               method="PUT", permitir_fallback=True)
        if st in (200, 201, 204):
            return {"status": "success"}
        if st in (401, 403):
            return {"status": "auth_error",
                    "message": "Sem permissão de escrita. Confira as Regras do banco."}
        return {"status": "error",
                "message": (corpo or {}).get("error", f"HTTP {st}") if isinstance(corpo, dict) else f"HTTP {st}"}

    def resumo(self):
        return {
            "configured": self.configurado,
            "signedIn": self.logado,
            "email": self.email,
            # uid, apiKey e o caminho completo NAO sao expostos a interface.
            "databaseURL": self.db_url,
        }



# ---------------------------------------------------------------------------
# PAINEL SPOTIFY - opcao 3: player PROPRIO (Web API + Web Playback SDK)
# O Cogni desenha a interface (tema, sem janela nativa por cima). O audio toca
# num <iframe> invisivel que carrega o Web Playback SDK de um servidor local
# (127.0.0.1:SPOTIFY_PORT). Login por OAuth PKCE no navegador padrao; o token
# fica no Python (a interface chama a Web API por spotify_api).
# Exige Spotify Premium (regra do SDK) e um app no Spotify for Developers.
# Mesma arquitetura do NEXUS; porta propria (8899) para os dois abrirem juntos.
# ---------------------------------------------------------------------------
SPOTIFY_PORT = 8899
SPOTIFY_REDIRECT = "http://127.0.0.1:%d/callback" % SPOTIFY_PORT
SPOTIFY_SCOPES = ("streaming user-read-email user-read-private user-read-playback-state "
                  "user-modify-playback-state user-read-currently-playing "
                  "playlist-read-private playlist-read-collaborative user-library-read")

_SPOTIFY_PLAYER_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Player do Spotify</title></head><body>
<script>
(function () {
  var N = new URLSearchParams(location.search).get('n') || '';
  var player = null;
  function post(o) { try { o.nxsp = 1; parent.postMessage(o, '*'); } catch (e) { } }
  function slim(s) {
    if (!s) return null;
    var t = s.track_window && s.track_window.current_track;
    if (!t) return { paused: true, track: null };
    var imgs = (t.album && t.album.images) || [];
    return { paused: s.paused, position: s.position, duration: s.duration, ts: s.timestamp,
      shuffle: s.shuffle, repeat: s.repeat_mode,
      track: { uri: t.uri, name: t.name, artists: (t.artists || []).map(function (a) { return a.name; }),
               image: (imgs[imgs.length > 1 ? 1 : 0] || {}).url || '' },
      context: (s.context && s.context.uri) || '' };
  }
  window.onSpotifyWebPlaybackSDKReady = function () {
    player = new Spotify.Player({
      name: 'Cogni', volume: 0.6,
      getOAuthToken: function (cb) {
        fetch('/token?n=' + encodeURIComponent(N), { cache: 'no-store' })
          .then(function (r) { return r.json(); })
          .then(function (j) { if (j && j.token) cb(j.token); else post({ type: 'error', kind: 'authentication_error', msg: 'Sem token' }); })
          .catch(function () { post({ type: 'error', kind: 'authentication_error', msg: 'Falha ao obter token' }); });
      }
    });
    player.addListener('ready', function (e) { post({ type: 'ready', device_id: e.device_id }); });
    player.addListener('not_ready', function () { post({ type: 'not_ready' }); });
    player.addListener('player_state_changed', function (s) { post({ type: 'state', state: slim(s) }); });
    ['initialization_error', 'authentication_error', 'account_error', 'playback_error'].forEach(function (k) {
      player.addListener(k, function (e) { post({ type: 'error', kind: k, msg: (e && e.message) || k }); });
    });
    player.connect().then(function (ok) { if (!ok) post({ type: 'error', kind: 'connect', msg: 'Nao foi possivel iniciar o player' }); });
  };
  window.addEventListener('message', function (e) {
    if (e.source !== parent || !player) return;
    var m = e.data; if (!m || !m.nxsp) return;
    switch (m.cmd) {
      case 'toggle': player.togglePlay(); break;
      case 'next': player.nextTrack(); break;
      case 'prev': player.previousTrack(); break;
      case 'seek': player.seek(Math.max(0, Number(m.ms) || 0)); break;
      case 'volume': player.setVolume(Math.min(1, Math.max(0, Number(m.v)))); break;
      case 'activate': try { player.activateElement(); } catch (x) { } break;
      case 'state': player.getCurrentState().then(function (s) { post({ type: 'state', state: slim(s) }); }); break;
    }
  });
  var sc = document.createElement('script');
  sc.src = 'https://sdk.scdn.co/spotify-player.js';
  sc.onerror = function () { post({ type: 'error', kind: 'sdk', msg: 'Nao foi possivel carregar o SDK do Spotify (rede bloqueada?)' }); };
  document.head.appendChild(sc);
})();
</script></body></html>"""


def _dpapi(dados, cifrar):
    """Protege/abre bytes com a chave do usuario do Windows (DPAPI)."""
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    buf = ctypes.create_string_buffer(dados, len(dados))
    entrada = _BLOB(len(dados), ctypes.cast(buf, ctypes.c_void_p))
    saida = _BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if cifrar else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(saida)):
        raise OSError("DPAPI falhou")
    try:
        return ctypes.string_at(saida.pbData, saida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(saida.pbData))


class CogniDesktopAPI:
    """
    Ponte de Comunicacao entre Python e a Interface Web (JS/HTML).
    Gerencia sincronizacao em tempo real com o Firebase Realtime Database na nuvem,
    alem de manter backup local e migrar dados antigos automaticamente.
    """
    def send_desktop_notification(self, title, message, sound=True):
        """Notificacao nativa do Windows para avisar sobre tarefas com prazo."""
        try:
            WindowsNotifier.notify(title, message, sound=bool(sound))
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------ #
    # Painel Spotify (opcao 3)
    # ------------------------------------------------------------------ #
    def _sp_st(self):
        if not hasattr(self, "_spx"):
            import base64
            self._spx = {"cfg": None, "access": "", "exp": 0.0, "server": None, "auth": None,
                         "nonce": base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("="),
                         "erro": None, "lock": threading.Lock()}
        return self._spx

    def _sp_cfg(self):
        st = self._sp_st()
        if st["cfg"] is None:
            cfg = {}
            try:
                with open(os.path.join(self._base_dir, "spotify.json"), "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            st["cfg"] = cfg if isinstance(cfg, dict) else {}
        return st["cfg"]

    def _sp_cfg_save(self):
        caminho = os.path.join(self._base_dir, "spotify.json")
        tmp = caminho + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._sp_cfg(), f)
        os.replace(tmp, caminho)

    def _sp_refresh_get(self):
        import base64
        cfg = self._sp_cfg()
        r = cfg.get("refresh")
        if not r:
            return ""
        try:
            raw = base64.b64decode(r)
            if cfg.get("enc"):
                raw = _dpapi(raw, False)
            return raw.decode("utf-8")
        except Exception:
            return ""

    def _sp_refresh_set(self, token):
        import base64
        raw, enc = token.encode("utf-8"), False
        try:
            raw, enc = _dpapi(raw, True), True       # criptografado com a conta do Windows
        except Exception:
            pass
        cfg = self._sp_cfg()
        cfg["refresh"] = base64.b64encode(raw).decode("ascii")
        cfg["enc"] = enc
        self._sp_cfg_save()

    def _sp_http(self, method, url, body=None, headers=None, timeout=20):
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx_strict) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})

    def _sp_token_form(self, campos):
        import urllib.parse
        status, corpo, _ = self._sp_http(
            "POST", "https://accounts.spotify.com/api/token", urllib.parse.urlencode(campos),
            {"Content-Type": "application/x-www-form-urlencoded"})
        try:
            j = json.loads(corpo.decode("utf-8", "replace"))
        except Exception:
            j = {}
        return status, (j if isinstance(j, dict) else {})

    def _sp_access(self):
        """Access token valido (renova com o refresh token quando preciso)."""
        st = self._sp_st()
        with st["lock"]:
            if st["access"] and time.time() < st["exp"] - 60:
                return st["access"]
            ref, cid = self._sp_refresh_get(), self._sp_cfg().get("client_id", "")
            if not ref or not cid:
                return ""
            try:
                status, j = self._sp_token_form({"grant_type": "refresh_token", "refresh_token": ref, "client_id": cid})
            except Exception as e:
                st["erro"] = "Sem conexao com o Spotify: " + str(e)
                return ""
            if status == 200 and j.get("access_token"):
                st["access"], st["exp"] = j["access_token"], time.time() + int(j.get("expires_in", 3600))
                if j.get("refresh_token"):
                    self._sp_refresh_set(j["refresh_token"])
                st["erro"] = None
                return st["access"]
            if status in (400, 401):                 # refresh token revogado/expirado
                self._sp_limpar_sessao()
            st["erro"] = "O Spotify recusou a renovacao do login (%s)." % status
            return ""

    def _sp_limpar_sessao(self):
        st = self._sp_st()
        st["access"], st["exp"] = "", 0.0
        cfg = self._sp_cfg()
        cfg.pop("refresh", None)
        cfg.pop("enc", None)
        try:
            self._sp_cfg_save()
        except Exception:
            pass

    def _sp_servidor(self):
        """Sobe (uma vez) o servidor local: /callback (OAuth), /player e /token."""
        st = self._sp_st()
        if st["server"]:
            return True
        import http.server
        import html as _html
        import urllib.parse
        import base64
        api = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _send(self, code, ctype, corpo):
                b = corpo.encode("utf-8") if isinstance(corpo, str) else corpo
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(b)

            def _callback(self, q):
                code = (q.get("code") or [""])[0]
                state = (q.get("state") or [""])[0]
                err = (q.get("error") or [""])[0]
                auth, msg, ok = st["auth"], "", False
                if err:
                    msg = "Autorizacao negada (%s)." % err
                elif not auth or not code or state != auth["state"] or time.time() - auth["t"] > 600:
                    msg = "Pedido invalido ou expirado. Volte ao Cogni e clique em Conectar de novo."
                else:
                    try:
                        status, j = api._sp_token_form({
                            "grant_type": "authorization_code", "code": code, "redirect_uri": SPOTIFY_REDIRECT,
                            "client_id": api._sp_cfg().get("client_id", ""), "code_verifier": auth["verifier"]})
                        if status == 200 and j.get("refresh_token"):
                            st["access"], st["exp"] = j.get("access_token", ""), time.time() + int(j.get("expires_in", 3600))
                            api._sp_refresh_set(j["refresh_token"])
                            st["auth"], st["erro"], ok = None, None, True
                        else:
                            msg = "O Spotify recusou o codigo (%s) %s" % (status, j.get("error_description", ""))
                    except Exception as e:
                        msg = "Falha ao concluir o login: " + str(e)
                titulo = "Cogni conectado ao Spotify" if ok else "Nao foi possivel conectar"
                corpo = ("<!doctype html><meta charset=utf-8><title>Conexao Spotify</title>"
                         "<body style='background:#0a0a0a;color:#e5e5e5;font-family:Segoe UI,sans-serif;"
                         "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                         "<div style='text-align:center'><h2>%s</h2><p>%s</p></div></body>" % (
                             _html.escape(titulo),
                             "Pode fechar esta aba e voltar ao Cogni." if ok else _html.escape(msg)))
                self._send(200 if ok else 400, "text/html; charset=utf-8", corpo)

            def do_GET(self):
                try:
                    if (self.headers.get("Host") or "") != "127.0.0.1:%d" % SPOTIFY_PORT:
                        return self._send(403, "text/plain", "host")      # defesa contra DNS rebinding
                    u = urllib.parse.urlparse(self.path)
                    q = urllib.parse.parse_qs(u.query)
                    if u.path == "/callback":
                        return self._callback(q)
                    if (q.get("n") or [""])[0] != st["nonce"]:
                        return self._send(403, "text/plain", "nonce")
                    if u.path == "/player":
                        return self._send(200, "text/html; charset=utf-8", _SPOTIFY_PLAYER_HTML)
                    if u.path == "/token":
                        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
                            return self._send(403, "text/plain", "origem")
                        tok = api._sp_access()
                        return self._send(200 if tok else 401, "application/json", json.dumps({"token": tok}))
                    self._send(404, "text/plain", "nao encontrado")
                except Exception:
                    try:
                        self._send(500, "text/plain", "erro")
                    except Exception:
                        pass

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False      # no Windows, reuse permitiria outro processo roubar a porta

        try:
            srv = S(("127.0.0.1", SPOTIFY_PORT), H)
        except OSError as e:
            st["erro"] = "A porta %d esta ocupada (%s). Feche o outro programa ou outra copia do Cogni." % (SPOTIFY_PORT, e)
            return False
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        st["server"] = srv
        return True

    def spotify_info(self):
        st = self._sp_st()
        cfg = self._sp_cfg()
        conectado = bool(self._sp_refresh_get())
        out = {"clientId": bool(cfg.get("client_id")), "connected": conectado, "redirect": SPOTIFY_REDIRECT,
               "erro": st["erro"], "playerUrl": None}
        if conectado and self._sp_servidor():
            out["playerUrl"] = "http://127.0.0.1:%d/player?n=%s" % (SPOTIFY_PORT, st["nonce"])
            out["erro"] = st["erro"]
        return out

    def spotify_salvar_client(self, client_id):
        import re
        cid = str(client_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", cid):
            return {"status": "error", "message": "O Client ID tem 32 caracteres (letras a-f e numeros)."}
        cfg = self._sp_cfg()
        if cfg.get("client_id") != cid:
            self._sp_limpar_sessao()
        cfg["client_id"] = cid
        try:
            self._sp_cfg_save()
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "success"}

    def spotify_conectar(self):
        """Inicia o login (OAuth PKCE) no navegador padrao."""
        try:
            import base64
            import hashlib
            import urllib.parse
            cid = self._sp_cfg().get("client_id", "")
            if not cid:
                return {"status": "error", "message": "Informe o Client ID primeiro."}
            if not self._sp_servidor():
                return {"status": "error", "message": self._sp_st()["erro"] or "Servidor local indisponivel."}
            verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            state = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
            self._sp_st()["auth"] = {"verifier": verifier, "state": state, "t": time.time()}
            url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
                "client_id": cid, "response_type": "code", "redirect_uri": SPOTIFY_REDIRECT,
                "scope": SPOTIFY_SCOPES, "code_challenge_method": "S256",
                "code_challenge": challenge, "state": state})
            import webbrowser
            webbrowser.open(url)
            return {"status": "success", "url": url}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def spotify_abrir(self, url):
        """Abre no navegador padrao SO as duas paginas do Spotify que o painel usa."""
        u = str(url or "")
        if u.startswith("https://developer.spotify.com/dashboard") or u.startswith("https://accounts.spotify.com/authorize?"):
            webbrowser.open(u)
            return {"status": "success"}
        return {"status": "error", "message": "URL nao permitida."}

    def spotify_desconectar(self):
        self._sp_limpar_sessao()
        return {"status": "success"}

    def spotify_api(self, method, path, body=None):
        """Proxy da Web API do Spotify: o token nunca chega a interface."""
        import re
        try:
            method = str(method or "").upper()
            path = str(path or "")
            if method not in ("GET", "PUT", "POST"):
                return {"status": 0, "message": "Metodo nao permitido."}
            if not re.fullmatch(r"/v1/[A-Za-z0-9_\-/.:%=&?,+~]*", path) or ".." in path:
                return {"status": 0, "message": "Caminho nao permitido."}
            tok = self._sp_access()
            if not tok:
                return {"status": 401, "data": None, "message": "Nao conectado."}
            headers = {"Authorization": "Bearer " + tok}
            dados = None
            if body is not None:
                dados = json.dumps(body)
                headers["Content-Type"] = "application/json"
            status, raw, hdr = self._sp_http(method, "https://api.spotify.com" + path, dados, headers)
            parsed = None
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    parsed = None
            out = {"status": status, "data": parsed}
            if status == 429:
                out["retry"] = hdr.get("Retry-After")
            return out
        except Exception as e:
            return {"status": 0, "message": str(e)}

    def _spotify_encerrar(self):
        st = self._sp_st()
        srv, st["server"] = st["server"], None
        if srv:
            threading.Thread(target=srv.shutdown, daemon=True).start()

    def __init__(self):
        # Pasta raiz do app. Cada CONTA recebe a sua subpasta (users/<uid>),
        # para que dois usuarios no mesmo computador nunca compartilhem cache.
        self._base_dir = os.path.dirname(LOCAL_FALLBACK_PATH)
        try:
            os.makedirs(self._base_dir, exist_ok=True)
        except Exception as e:
            print(f"[Cogni] Falha ao preparar pasta base: {e}")
        # Pasta de documentos do usuario. Fica FORA de qualquer pasta de
        # programa e o desinstalador nao a apaga (ver cogni_installer.iss):
        # desinstalar o app nao deve destruir os documentos de estudo.
        try:
            os.makedirs(DOCS_PATH, exist_ok=True)
        except Exception as e:
            print(f"[Cogni] Falha ao preparar pasta de documentos: {e}")

        self._cloud = CloudSync(self._base_dir)
        self._ultimo_sync = None
        self.local_path = LOCAL_FALLBACK_PATH

        # Atualizacao automatica. A checagem roda em thread separada para nao
        # atrasar a abertura da janela nem um milissegundo.
        self._updater = AutoUpdater(self._base_dir)
        self._update_cache = None
        try:
            import threading
            threading.Thread(target=self._checar_update_em_background,
                             daemon=True).start()
        except Exception as e:
            print(f"[Cogni] Nao foi possivel iniciar a checagem de update: {e}")

        self._aplicar_caminhos_do_usuario()

    # ------------------------------------------------------------------ #
    # Atualizacao automatica (expostos ao JS)
    # ------------------------------------------------------------------ #
    def _checar_update_em_background(self):
        """Espera o app abrir e so depois consulta o manifesto."""
        try:
            time.sleep(6)
            self._update_cache = self._updater.verificar(forcar=True)
        except Exception as e:
            self._update_cache = {"status": "error", "message": str(e),
                                  "current": APP_VERSION}

    def update_cached(self):
        """Resultado da checagem automatica. None enquanto nao terminou."""
        return self._update_cache

    def update_check_now(self):
        """Checagem manual, disparada pelo botao da interface."""
        try:
            self._update_cache = self._updater.verificar(forcar=True)
            return self._update_cache
        except Exception as e:
            return {"status": "error", "message": str(e), "current": APP_VERSION}

    def update_install(self):
        """Inicia download, verificacao e instalacao. Acompanhe update_progress."""
        try:
            return self._updater.baixar_e_aplicar()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def update_progress(self):
        try:
            return self._updater.progresso()
        except Exception as e:
            return {"fase": "error", "pct": 0, "mensagem": str(e)}

    def update_channel_info(self):
        """Diagnostico: onde o app procura atualizacao e se o TLS esta ok."""
        return {
            "channel": UPDATE_CHANNEL,
            "manifest": UPDATE_MANIFEST_URL.split("?")[0],
            "tlsVerificavel": _ssl_ctx_strict is not None,
            "tlsDegradado": _TLS_DEGRADADO,
            "tlsMotivo": _TLS_MOTIVO,
            "pasta": self._updater.pasta,
        }

    def security_status(self):
        """
        Estado de seguranca da conexao, para a interface poder avisar.

        `tlsDegradado` vira True se em algum momento uma requisicao de DADOS
        teve de ser repetida sem verificar o certificado. Isso nunca acontece
        com login nem com atualizacao - esses recusam e falham. Mas o usuario
        precisa saber, porque significa que alguem ou alguma coisa esta no meio
        da conexao: antivirus com inspecao HTTPS, proxy corporativo, ou algo
        pior.
        """
        return {
            "tlsVerificavel": _ssl_ctx_strict is not None,
            "tlsDegradado": _TLS_DEGRADADO,
            "motivo": _TLS_MOTIVO,
            "authSempreVerificado": True,
        }

    def get_app_version(self):
        return APP_VERSION

    def get_docs_path(self):
        """Pasta onde o usuario guarda os documentos de estudo."""
        return DOCS_PATH

    def open_docs_folder(self):
        """Abre C:\\COGNI\\docs no Explorer."""
        try:
            os.makedirs(DOCS_PATH, exist_ok=True)
            os.startfile(DOCS_PATH)
            return {"status": "success", "path": DOCS_PATH}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------ #
    # Isolamento por conta
    # ------------------------------------------------------------------ #
    def _pasta_do_usuario(self):
        """
        Pasta de cache da conta atual. Sem conta, usa uma pasta neutra.
        """
        if self._cloud.logado and self._cloud.uid:
            return os.path.join(self._base_dir, "users", self._cloud.uid)
        return os.path.join(self._base_dir, "local")

    def _aplicar_caminhos_do_usuario(self):
        """
        Reaponta o cache para a pasta da conta atual. Sem isso, o cache da
        conta anterior seria lido pela conta seguinte no mesmo computador e
        acabaria sendo enviado para a nuvem dela.
        """
        pasta = self._pasta_do_usuario()
        try:
            os.makedirs(pasta, exist_ok=True)
        except Exception as e:
            print(f"[Cogni] Falha ao preparar pasta da conta: {e}")
        self.local_path = os.path.join(pasta, "database.json")
        self._ultimo_sync = None
        self._ensure_local_cache_exists()

    # ------------------------------------------------------------------ #
    # Conta (expostos ao JS)
    # ------------------------------------------------------------------ #
    def cloud_status(self):
        r = self._cloud.resumo()
        r["lastSync"] = self._ultimo_sync
        return r

    def cloud_save_config(self, api_key, database_url):
        return self._cloud.salvar_config(api_key, database_url)

    def cloud_sign_up(self, email, password):
        r = self._cloud.criar_conta(email, password)
        if r.get("status") == "success":
            self._aplicar_caminhos_do_usuario()
        return r

    def cloud_sign_in(self, email, password):
        r = self._cloud.entrar(email, password)
        if r.get("status") == "success":
            self._aplicar_caminhos_do_usuario()
        return r

    def cloud_sign_out(self):
        r = self._cloud.sair()
        self._aplicar_caminhos_do_usuario()
        return r

    @staticmethod
    def _peso(d):
        """Quanto conteudo real existe, para nunca deixar o vazio vencer."""
        try:
            return (len(d.get("tasks") or []) + len(d.get("notes") or []) +
                    len(d.get("flashcardDecks") or []) + len(d.get("postits") or []) +
                    len(d.get("studyLogs") or []) + len(d.get("subjects") or []))
        except Exception:
            return 0

    def cloud_legacy_preview(self):
        """Informa se ha dados no caminho antigo, para o app poder perguntar."""
        d = self._ler_legado()
        if not d:
            return {"available": False}
        return {"available": self._peso(d) > 0, "items": self._peso(d),
                "tasks": len(d.get("tasks") or []),
                "subjects": len(d.get("subjects") or []),
                "notes": len(d.get("notes") or [])}

    def cloud_import_legacy(self):
        """
        Copia /database para o espaco da conta atual. Acao EXPLICITA, disparada
        pelo usuario. O original em /database permanece intacto.
        """
        d = self._ler_legado()
        if not d or self._peso(d) == 0:
            return {"status": "error", "message": "Nao ha dados antigos para importar."}
        res = self._cloud.enviar(d)
        if res.get("status") != "success":
            return res
        texto = json.dumps(d, ensure_ascii=False)
        self._save_local_cache(texto)
        self._ultimo_sync = time.strftime("%H:%M:%S")
        return {"status": "success", "data": texto, "items": self._peso(d)}

    def _ler_legado(self):
        """Le /database (onde o Cogni gravava antes do login). Nao apaga nada.

        Passa pelo _http_json em vez de montar a requisicao a mao: assim esta
        leitura tambem ganha verificacao de certificado. O token de acesso vai
        na URL, entao trafegar sem verificacao aqui expunha o token.
        """
        try:
            token = self._cloud._token_valido()
            url = LEGACY_URL + (f"?auth={token}" if token else "")
            st, corpo = _http_json(url, timeout=10, permitir_fallback=True)
            if st == 200 and isinstance(corpo, dict) and corpo:
                return corpo
        except Exception as e:
            print(f"[Cogni Legado] Nao foi possivel ler /database: {e}")
        return None

    def _ensure_local_cache_exists(self):
        try:
            caminho = getattr(self, "local_path", LOCAL_FALLBACK_PATH)
            os.makedirs(os.path.dirname(caminho), exist_ok=True)
            if not os.path.exists(caminho):
                with open(caminho, "w", encoding="utf-8") as f:
                    f.write("{}")
        except Exception as e:
            print(f"[Python Cache] Falha ao preparar cache local: {e}")

    def load_state_from_disk(self):
        """
        Le os dados do espaco do usuario logado: /apps/cogni/users/<uid>/database

        Na PRIMEIRA entrada de uma conta, se esse espaco estiver vazio, copia o
        que existe no caminho antigo /database. O original permanece la, intacto,
        servindo de copia de seguranca.

        Mantem o contrato antigo: devolve uma STRING JSON.
        """
        if not (self._cloud.configurado and self._cloud.logado):
            # Sem conta: opera apenas com o cache local desta maquina.
            return self._load_local_cache() or "{}"

        remoto = self._cloud.baixar()

        if remoto.get("status") == "ok":
            dados = remoto["data"]
            local = self._load_local_cache()
            try:
                d_local = json.loads(local) if local else {}
            except Exception:
                d_local = {}

            # Um remoto mais pobre nunca substitui um local mais rico.
            if self._peso(d_local) > self._peso(dados):
                print("[Cogni] Cache local mais completo que a nuvem; enviando local.")
                self._cloud.enviar(d_local)
                return local

            texto = json.dumps(dados, ensure_ascii=False)
            self._save_local_cache(texto)
            self._ultimo_sync = time.strftime("%H:%M:%S")
            return texto

        if remoto.get("status") == "empty":
            # Espaco novo desta conta. A unica semeadura automatica permitida e
            # o cache LOCAL desta maquina: ele prova que este computador ja usava
            # o Cogni. O caminho legado /database e compartilhado e NAO pode ser
            # copiado sozinho, senao qualquer conta nova herdaria dados alheios.
            local = self._load_local_cache()
            if local and local.strip() not in ("null", "{}"):
                try:
                    d = json.loads(local)
                    if self._peso(d) > 0:
                        self._cloud.enviar(d)
                        self._ultimo_sync = time.strftime("%H:%M:%S")
                        return local
                except Exception:
                    pass
            return "{}"

        # Falha de rede ou permissao: segue com o cache local.
        print(f"[Cogni] Nuvem indisponivel ({remoto.get('message','')}); usando cache local.")
        return self._load_local_cache() or "{}"

    def save_state_to_disk(self, json_str):
        """
        Grava no cache local e envia para o espaco do usuario na nuvem.
        O caminho antigo /database NAO e mais escrito, mas continua preservado.
        """
        self._save_local_cache(json_str)

        if not (self._cloud.configurado and self._cloud.logado):
            return {"status": "offline_saved", "mode": "local_only",
                    "message": "Sem conta conectada."}

        try:
            dados = json.loads(json_str)
        except Exception as e:
            return {"status": "error", "message": f"JSON invalido: {e}"}

        res = self._cloud.enviar(dados)
        if res.get("status") == "success":
            self._ultimo_sync = time.strftime("%H:%M:%S")
            # Sem o caminho completo: ele contem o UID e nao deve circular.
            return {"status": "success", "mode": "cloud", "at": self._ultimo_sync}
        return {"status": "offline_saved", "mode": "local_only",
                "message": res.get("message", "")}

    def _save_local_cache(self, json_str):
        self._ensure_local_cache_exists()
        try:
            with open(self.local_path, "w", encoding="utf-8") as f:
                f.write(json_str)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"[Backup Local] Erro ao salvar cache local: {e}")

    def _load_local_cache(self):
        self._ensure_local_cache_exists()
        if os.path.exists(self.local_path):
            try:
                with open(self.local_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.strip() and content.strip() != "null":
                        return content
            except Exception as e:
                print(f"[Backup Local] Erro ao ler cache local: {e}")
        return None

    # Extensoes que o Windows EXECUTA em vez de abrir. os.startfile() nelas
    # equivale a rodar codigo. Como o caminho vem do banco (que sincroniza da
    # nuvem) e da interface (que renderiza HTML), tratamos como nao-confiavel.
    EXTENSOES_EXECUTAVEIS = {
        ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
        ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp", ".scr", ".pif",
        ".hta", ".cpl", ".jar", ".reg", ".lnk", ".url", ".inf", ".application",
        ".gadget", ".msc", ".sct", ".shb", ".shs", ".ws",
    }

    def open_external_target(self, target_path):
        try:
            if not target_path:
                return {"status": "error", "message": "Caminho nao especificado."}

            clean_path = str(target_path).strip('"\'')

            if clean_path.lower().startswith(("http://", "https://")):
                webbrowser.open(clean_path)
            elif ":" in clean_path.split("\\")[0].split("/")[0][2:]:
                # Bloqueia esquemas como file:, javascript:, ms-msdt:,
                # search-ms:. Um caminho do Windows tem ':' so na posicao 1
                # (C:\...), entao a partir do terceiro caractere nao deve haver
                # ':' antes da primeira barra.
                return {"status": "error",
                        "message": "Tipo de link nao permitido por seguranca."}
            else:
                norm_path = os.path.normpath(clean_path)

                ext = os.path.splitext(norm_path)[1].lower()
                if ext in self.EXTENSOES_EXECUTAVEIS:
                    return {"status": "error",
                            "message": f"Por seguranca, o Cogni nao abre arquivos "
                                       f"'{ext}', que o Windows executaria como "
                                       f"programa.\n\nAbra manualmente se confia "
                                       f"na origem:\n{norm_path}"}

                if os.path.exists(norm_path):
                    os.startfile(norm_path)
                else:
                    return {"status": "error", "message": f"Arquivo nao encontrado: {norm_path}"}
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def pick_file_native(self):
        try:
            window = webview.windows[0]
            result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
            if result:
                file_path = result[0] if isinstance(result, (list, tuple)) else result
                file_path = os.path.normpath(file_path)
                file_name = os.path.basename(file_path)
                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                return {
                    "status": "success",
                    "path": file_path,
                    "name": file_name,
                    "size": file_size
                }
            return {"status": "cancelled"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def export_csv(self, csv_text, suggested_name="tarefas_cogni.csv"):
        """Abre o dialogo nativo 'Salvar Como' e grava o CSV (UTF-8 com BOM,
        para o Excel reconhecer acentuacao sem perguntar a codificacao)."""
        try:
            window = webview.windows[0]
            caminho = window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=suggested_name,
                file_types=('Arquivos CSV (*.csv)',)
            )
            if not caminho:
                return {"status": "cancelled"}
            caminho = caminho[0] if isinstance(caminho, (list, tuple)) else caminho
            with open(caminho, 'w', encoding='utf-8-sig', newline='') as f:
                f.write(csv_text)
            return {"status": "success", "path": caminho}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def import_csv_pick(self):
        """Abre o dialogo nativo 'Abrir' e devolve o CONTEUDO do CSV escolhido
        como texto - o parsing fica no JS, perto de onde os dados sao usados."""
        try:
            window = webview.windows[0]
            result = window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=('Arquivos CSV (*.csv)', 'Todos os arquivos (*.*)')
            )
            if not result:
                return {"status": "cancelled"}
            caminho = result[0] if isinstance(result, (list, tuple)) else result
            # utf-8-sig engole o BOM se existir (Excel adiciona por padrao);
            # arquivos sem BOM continuam lidos normalmente.
            with open(caminho, 'r', encoding='utf-8-sig', errors='replace') as f:
                conteudo = f.read()
            return {"status": "success", "content": conteudo}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def open_pip_timer(self, duration_secs=1500, tema=None, materias=None):
        """Janela flutuante do Timer. Recebe o tema do usuario (cores) e a
        lista de materias; ao Concluir, devolve o tempo para a janela
        principal, que valida a materia e grava o registro."""
        try:
            duration_secs = max(1, int(duration_secs))
            hexa = re.compile(r'^#[0-9a-fA-F]{6}$')
            t = tema if isinstance(tema, dict) else {}
            cor = {k: (t.get(k) if isinstance(t.get(k), str) and hexa.match(t.get(k)) else d)
                   for k, d in (('bg', '#4f46e5'), ('light', '#eef2ff'), ('border', '#c7d2fe'), ('text', '#4f46e5'))}
            lista = []
            for m in (materias if isinstance(materias, list) else [])[:100]:
                if isinstance(m, dict) and isinstance(m.get('nome'), str):
                    c = m.get('cor')
                    lista.append({'nome': m['nome'][:120], 'cor': c if isinstance(c, str) and hexa.match(c) else '#64748b'})
            # '<' escapado: um nome de materia nunca fecha a tag <script>
            dados = json.dumps({'secs': duration_secs, 'cor': cor, 'materias': lista}).replace('<', '\\u003c')
            pip_html = _PIP_HTML.replace('__PIP_DATA__', dados)
            timer_uri = _escrever_ui_local('timer.html', pip_html)
            pip_api = _PipApi()
            window = webview.create_window('Cogni - Timer', url=timer_uri, js_api=pip_api,
                                           width=330, height=250, resizable=False, on_top=True)
            pip_api._win = window
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class _PipApi:
    """API minima da janela do Timer: so duas portas (concluir e fechar).
    A janela principal continua sendo a unica que toca nos dados."""
    def __init__(self):
        self._win = None

    def concluir(self, materia, segundos):
        try:
            segundos = int(segundos)
            if segundos > 0:
                webview.windows[0].evaluate_js(
                    "registrarSessaoFoco(%s, %d)" % (json.dumps(str(materia or '')[:120]), segundos))
            self.fechar()
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def fechar(self):
        if self._win is not None:
            threading.Timer(0.15, self._win.destroy).start()
        return {"status": "success"}


_PIP_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Cogni - Timer</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' file: 'unsafe-inline'; font-src 'self' file: data:; img-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none';">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
  :root { --bg:#4f46e5; --light:#eef2ff; --border:#c7d2fe; --text:#4f46e5; }
  * { box-sizing:border-box; margin:0; padding:0; }
  html,body { height:100%; }
  body { font-family:'Inter',system-ui,sans-serif; background:#f4f5f7; color:#111827; overflow:hidden; user-select:none; padding:10px; }
  .card { height:100%; background:#fff; border-radius:16px; box-shadow:0 6px 20px rgba(17,24,39,.06); padding:14px 16px; display:flex; flex-direction:column; position:relative; }
  .top { display:flex; align-items:center; justify-content:space-between; }
  .label { font-size:10px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:#9ca3af; display:flex; align-items:center; gap:6px; }
  .pill { font-size:10px; font-weight:600; color:#6b7280; background:#f3f4f6; border-radius:999px; padding:3px 10px; }
  .pill.on { background:var(--light); color:var(--text); }
  .mid { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; }
  #clock { font-size:54px; line-height:1; font-weight:800; letter-spacing:-.04em; color:var(--text); font-variant-numeric:tabular-nums; }
  #sub { margin-top:6px; font-size:11px; color:#9ca3af; }
  .row { display:flex; gap:8px; }
  button { font-family:inherit; cursor:pointer; border:0; transition:transform .2s ease-out, box-shadow .2s ease-out, background-color .15s; }
  button:hover { transform:translateY(-2px); box-shadow:0 4px 10px rgba(17,24,39,.10); }
  .btn { height:36px; border-radius:12px; font-size:12px; font-weight:700; display:flex; align-items:center; justify-content:center; gap:6px; padding:0 14px; }
  .primary { flex:1; background:var(--bg); color:#fff; }
  .primary:hover { filter:brightness(1.08); }
  .ghost { background:#fff; color:#374151; border:1px solid #e5e7eb; }
  .ghost:hover { background:#f9fafb; }
  .icon { width:36px; padding:0; color:#9ca3af; }
  .sheet { position:absolute; inset:0; background:#fff; border-radius:16px; padding:14px 16px; display:none; flex-direction:column; gap:8px; z-index:5; }
  .sheet.open { display:flex; }
  .sheet h2 { font-size:13px; font-weight:800; letter-spacing:-.01em; }
  .sheet p { font-size:11px; color:#9ca3af; }
  #list { flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:6px; padding:2px 2px 4px; }
  .opt { text-align:left; background:#fff; border:1px solid #eef0f3; border-radius:12px; padding:9px 12px; font-size:12px; font-weight:600; color:#374151; display:flex; align-items:center; gap:8px; flex-shrink:0; }
  .opt:hover { background:#f9fafb; border-color:var(--border); }
  .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; background:#9ca3af; }
  .link { background:none; color:#9ca3af; font-size:11px; font-weight:600; align-self:center; padding:4px 8px; }
  .link:hover { box-shadow:none; color:#6b7280; }
  @media (prefers-reduced-motion: reduce) { button { transition:none; } button:hover { transform:none; } }
</style>
</head>
<body>
<div class="card">
  <div class="top">
    <span class="label"><i class="fa-solid fa-stopwatch"></i> Foco</span>
    <span id="status" class="pill">Pronto</span>
  </div>
  <div class="mid">
    <div id="clock">00:00</div>
    <div id="sub">Nenhum tempo focado ainda</div>
  </div>
  <div class="row">
    <button id="btn-toggle" class="btn primary" onclick="toggle()"><i id="btn-icon" class="fa-solid fa-play"></i><span id="btn-text">Iniciar</span></button>
    <button class="btn ghost" onclick="concluir()"><i class="fa-solid fa-check"></i>Concluir</button>
    <button class="btn ghost icon" onclick="reset()" title="Reiniciar"><i class="fa-solid fa-rotate-left"></i></button>
  </div>
  <div id="sheet" class="sheet">
    <h2 id="sheet-title">Aplicar horas</h2>
    <p id="sheet-sub">Escolha onde registrar este tempo.</p>
    <div id="list"></div>
    <button class="link" onclick="fecharSheet()" id="sheet-back">Voltar</button>
  </div>
</div>
<script>
  const D = __PIP_DATA__;
  const r = document.documentElement.style;
  r.setProperty('--bg', D.cor.bg); r.setProperty('--light', D.cor.light);
  r.setProperty('--border', D.cor.border); r.setProperty('--text', D.cor.text);
  const $ = id => document.getElementById(id);
  let left = D.secs, running = false, iv = null;
  const elapsed = () => D.secs - left;
  function fmtMin(sec) { const m = Math.floor(sec / 60); return m < 1 ? 'menos de 1 min' : (m >= 60 ? Math.floor(m/60) + 'h ' + String(m%60).padStart(2,'0') + 'min' : m + ' min'); }
  function draw() {
    $('clock').textContent = String(Math.floor(left / 60)).padStart(2, '0') + ':' + String(left % 60).padStart(2, '0');
    $('sub').textContent = elapsed() > 0 ? 'Focado: ' + fmtMin(elapsed()) : 'Nenhum tempo focado ainda';
  }
  function setState(txt, on, btn, icon) {
    $('status').textContent = txt; $('status').className = 'pill' + (on ? ' on' : '');
    $('btn-text').textContent = btn; $('btn-icon').className = 'fa-solid ' + icon;
  }
  function toggle() {
    if (left <= 0) return;
    if (running) { clearInterval(iv); running = false; setState('Pausado', false, 'Continuar', 'fa-play'); return; }
    running = true; setState('Em foco', true, 'Pausar', 'fa-pause');
    iv = setInterval(() => {
      if (left > 0) { left--; draw(); }
      if (left <= 0) { clearInterval(iv); running = false; setState('Tempo esgotado', false, 'Iniciar', 'fa-play'); $('sub').textContent = 'Clique em Concluir para registrar'; }
    }, 1000);
  }
  function reset() { clearInterval(iv); running = false; left = D.secs; setState('Pronto', false, 'Iniciar', 'fa-play'); draw(); }
  function pause() { if (running) { clearInterval(iv); running = false; setState('Pausado', false, 'Continuar', 'fa-play'); } }
  function fecharSheet() { $('sheet').classList.remove('open'); }
  function enviar(materia) {
    const api = window.pywebview && window.pywebview.api;
    if (api) api.concluir(materia, elapsed());
  }
  function opcao(nome, cor, materia) {
    const b = document.createElement('button'); b.className = 'opt';
    const d = document.createElement('span'); d.className = 'dot'; if (cor) d.style.background = cor;
    const t = document.createElement('span'); t.textContent = nome;
    b.append(d, t); b.onclick = () => enviar(materia); return b;
  }
  function concluir() {
    pause();
    const list = $('list'); list.textContent = '';
    const sec = elapsed();
    if (sec < 60) {
      $('sheet-title').textContent = 'Tempo muito curto';
      $('sheet-sub').textContent = 'Menos de 1 minuto focado - nada foi registrado.';
      $('sheet-back').textContent = 'Voltar';
      const b = document.createElement('button'); b.className = 'btn ghost'; b.textContent = 'Fechar sem registrar';
      b.onclick = () => { const a = window.pywebview && window.pywebview.api; if (a) a.fechar(); };
      list.appendChild(b);
    } else {
      $('sheet-title').textContent = 'Aplicar ' + fmtMin(sec);
      $('sheet-sub').textContent = 'Em qual matéria foi esse tempo?';
      list.appendChild(opcao('Sem matéria (só no dia)', '#9ca3af', ''));
      D.materias.forEach(m => list.appendChild(opcao(m.nome, m.cor, m.nome)));
    }
    $('sheet').classList.add('open');
  }
  draw();
</script>
</body>
</html>"""

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="pt-BR" class="h-full bg-white">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cogni - Desktop Study Suite</title>

    <!-- ===================================================================
         POLITICA DE SEGURANCA DE CONTEUDO (CSP)
         ===================================================================
         Nao elimina XSS sozinha - o app tem todo o JS inline, entao
         'unsafe-inline' e inevitavel sem uma reescrita grande. O que ela FAZ:

           script-src  -> um <script src="http://site-do-atacante/x.js">
                          injetado numa nota nao carrega.
           connect-src -> impede que codigo injetado exfiltre suas anotacoes
                          para um servidor de terceiros via fetch.
           object-src  -> mata <object>, <embed>, <applet>.
           base-uri    -> impede sequestro de URLs relativas via <base>.
           form-action -> impede que um <form> injetado poste seus dados fora.
    -->
    <meta http-equiv="Content-Security-Policy" content="
        default-src 'self';
        script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net;
        style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com;
        font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com;
        img-src 'self' data: blob: https://*.scdn.co https://*.spotifycdn.com;
        connect-src 'self' https://cogni-data-default-rtdb.firebaseio.com https://identitytoolkit.googleapis.com https://securetoken.googleapis.com;
        object-src 'none';
        base-uri 'none';
        form-action 'none';
        frame-src http://127.0.0.1:8899;
    ">

    <!-- VERSOES FIXAS. 'npm/chart.js' sem versao entrega o que o CDN quiser
         hoje: uma versao nova quebra o app sem aviso, e um pacote
         comprometido vira execucao de codigo com acesso a ponte pywebview.
         Sempre fixe.

         MELHOR AINDA: baixe estes tres arquivos para uma pasta assets\ e
         referencie localmente. O app passa a funcionar offline e deixa de
         depender de tres CDNs de terceiros. -->
    <script src="https://cdn.tailwindcss.com/3.4.5"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">

    <!-- O tema customizado (cores notion-*, fonte Inter) antes era passado em
         tempo de execucao via tailwind.config, porque o Tailwind vinha de um
         compilador rodando no navegador (carregado por rede). Agora o
         Tailwind e um CSS estatico pre-compilado (vendor/tailwind.local.css,
         gerado com ESSE MESMO tema), entao nao existe mais um objeto
         `tailwind` global para configurar - um bloco de script fazendo isso
         so daria ReferenceError. -->
    <style>
        * { -webkit-tap-highlight-color: transparent; }
        body { user-select: none; -webkit-font-smoothing: antialiased; }
        .hidden { display: none !important; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #ffffff; }
        ::-webkit-scrollbar-thumb { background: #e5e7eb; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #d1d5db; }
        
        .canvas-area {
            background-image: radial-gradient(#d1d5db 1px, transparent 1px);
            background-size: 24px 24px;
        }

        .postit-draggable, .pip-draggable {
            touch-action: none;
            user-select: none;
            cursor: grab;
        }
        .postit-draggable:active, .pip-draggable:active { cursor: grabbing; }

        #note-editor-content:empty:before {
            content: attr(data-placeholder);
            color: #9ca3af;
            cursor: text;
        }
        #note-editor-content h1 { font-size: 1.625rem; font-weight: 800; margin-top: 0.75rem; margin-bottom: 0.375rem; color: #111827; }
        #note-editor-content h2 { font-size: 1.35rem; font-weight: 700; margin-top: 0.625rem; margin-bottom: 0.25rem; color: #1f2937; }
        #note-editor-content h3 { font-size: 1.125rem; font-weight: 600; margin-top: 0.5rem; margin-bottom: 0.25rem; color: #374151; }
        #note-editor-content ul { list-style-type: disc; padding-left: 1.5rem; margin-top: 0.25rem; margin-bottom: 0.25rem; }
        #note-editor-content ol { list-style-type: decimal; padding-left: 1.5rem; margin-top: 0.25rem; margin-bottom: 0.25rem; }
        #note-editor-content p { margin-bottom: 0.375rem; }

        .perspective-1000 { perspective: 1000px; }
        .flashcard-inner {
            position: relative; width: 100%; height: 100%;
            transition: transform 0.6s cubic-bezier(0.4, 0, 0.2, 1);
            transform-style: preserve-3d;
        }
        .flashcard-inner.is-flipped { transform: rotateY(180deg); }
        .flashcard-front, .flashcard-back {
            position: absolute; width: 100%; height: 100%;
            backface-visibility: hidden; border-radius: 1rem;
        }
        .flashcard-back { transform: rotateY(180deg); }

        /* GitHub Heatmap Tile Styling */
        .heatmap-cell {
            transition: transform 0.15s ease, filter 0.15s ease;
        }
        .heatmap-cell:hover {
            transform: scale(1.25);
            z-index: 10;
        }


        /* ---- Seu Progresso (mapa anual) ---- */
        .cg-year-card { background:#fff; border-radius:14px; padding:2rem; box-shadow:0 10px 30px rgba(17,24,39,0.08); }
        .cg-year-scroll { overflow-x:auto; padding-bottom:.5rem; }
        .cg-year-inner { min-width:800px; display:flex; flex-direction:column; gap:.5rem; }
        .cg-year-grid { display:grid; grid-auto-flow:column; grid-template-rows:repeat(7,1fr); gap:6px; }
        .cg-year-cell { position:relative; width:100%; aspect-ratio:1/1; border-radius:2px; border:0 !important; cursor:default; transition:opacity .15s, transform .15s; }
        .cg-year-cell:not([class*="bg-"]) { background:#f3f4f6; }
        .cg-year-cell:hover { opacity:.7; }
        #cg-tip { position:fixed; z-index:9999; background:#111827; color:#fff; font-size:11px; padding:5px 9px; border-radius:8px; white-space:nowrap; pointer-events:none; opacity:0; transition:opacity .12s; box-shadow:0 6px 16px rgba(0,0,0,.18); }
        .cg-year-months { display:flex; justify-content:space-between; padding:0 .5rem; margin-top:.5rem; font-size:10px; font-weight:500; letter-spacing:.1em; text-transform:uppercase; color:#9ca3af; }
        .cg-year-foot { display:flex; flex-wrap:wrap; align-items:center; gap:1rem; margin-top:1rem; font-size:11px; color:#9ca3af; }
        .cg-year-legend { display:flex; align-items:center; gap:4px; margin-left:auto; }
        .cg-year-legend div { width:12px; height:12px; border-radius:2px; border:0 !important; }

        /* ---- Hover "ima": sobe e e atraido ao mouse ---- */
        button:not(:disabled):not(.no-lift):not(.pointer-events-none):not(#mindmap-canvas-wrapper *):not(#study-card-inner *), .nav-btn, .cg-kpi[onclick] { transition: transform .2s ease-out, box-shadow .2s ease-out, background-color .15s, opacity .15s; }
        button:not(:disabled):not(.no-lift):not(.pointer-events-none):not(#mindmap-canvas-wrapper *):not(#study-card-inner *):hover, .nav-btn:hover, .cg-kpi[onclick]:hover { transform: translateY(-2px); }
        button:not(:disabled):not(.no-lift):not(#mindmap-canvas-wrapper *):not(#study-card-inner *):hover { box-shadow: 0 4px 10px rgba(17,24,39,.10); }
        .cg-kpi[onclick]:hover { box-shadow: 0 10px 24px rgba(17,24,39,.10) !important; }
        @media (prefers-reduced-motion: reduce) { * { transition: none !important; } *:hover { transform: none !important; } }


        /* ---- Modais de foco (mesmo padrao do app: cartao branco, cantos grandes, rotulos cinza) ---- */
        .cg-modal { width:100%; max-width:440px; background:#fff; border-radius:20px; padding:22px; box-shadow:0 24px 60px rgba(17,24,39,.22); display:flex; flex-direction:column; gap:18px; font-size:13px; max-height:94vh; overflow-y:auto; }
        .cg-modal-head { display:flex; align-items:center; justify-content:space-between; }
        .cg-modal-title { font-size:1.15rem; font-weight:800; letter-spacing:-.02em; color:#111827; }
        .cg-x { width:30px; height:30px; border-radius:10px; color:#9ca3af; background:transparent; }
        .cg-x:hover { background:#f3f4f6; color:#6b7280; }
        .cg-mb { margin-bottom:8px; }
        .cg-hint { margin-top:6px; font-size:11px; color:#9ca3af; }
        .cg-chips { display:flex; flex-wrap:wrap; gap:8px; }
        .cg-chip { height:34px; padding:0 14px; border-radius:12px; background:#f4f5f7; color:#4b5563; font-weight:700; font-size:12px; }
        .cg-chip:hover { background:#eceef1; }
        .cg-chip.on { background:var(--accent-bg,#4f46e5); color:#fff; }
        .cg-input, .cg-select { width:100%; height:40px; border:1px solid #e5e7eb; border-radius:12px; background:#fff; padding:0 12px; color:#111827; font-size:13px; font-family:inherit; outline:none; transition:border-color .15s, box-shadow .15s; }
        .cg-input:focus, .cg-select:focus { border-color:var(--accent-bg,#4f46e5); box-shadow:0 0 0 3px rgba(79,70,229,.10); }
        .cg-chip-input { width:84px; height:34px; text-align:center; background:#f4f5f7; border-color:transparent; font-weight:700; }
        .cg-select { appearance:none; -webkit-appearance:none; padding-right:36px; cursor:pointer;
            background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%239ca3af' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
            background-repeat:no-repeat; background-position:right 14px center; }
        .cg-two { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
        .cg-clock { background:#f7f8fa; border-radius:18px; padding:26px 12px 20px; text-align:center; }
        .cg-clock #timer-display { display:block; font-size:4.25rem; line-height:1; font-weight:800; letter-spacing:-.05em; font-variant-numeric:tabular-nums; font-family:inherit; }
        .cg-clock p { margin-top:10px; font-size:12px; font-weight:500; color:#9ca3af; }
        .cg-actions { display:flex; gap:8px; }
        .cg-btn { height:40px; border-radius:12px; padding:0 16px; font-weight:700; font-size:13px; display:inline-flex; align-items:center; justify-content:center; gap:7px; }
        .cg-btn-primary { flex:1; color:#fff; }
        .cg-btn-primary:hover { filter:brightness(1.08); }
        .cg-btn-ghost { background:#fff; border:1px solid #e5e7eb; color:#374151; }
        .cg-btn-ghost:hover { background:#f9fafb; }
        .cg-btn-ghost i { color:#9ca3af; }
        .cg-btn-icon { width:40px; padding:0; }
        .cg-modal-foot { border-top:1px solid #f0f1f3; padding-top:14px; text-align:center; }
        .cg-link { background:none; font-weight:700; font-size:12px; }
        .cg-link:hover { text-decoration:underline; transform:none !important; box-shadow:none !important; }


        /* ---- Painel lateral da tarefa (encaixado, nao sobrepoe) ---- */
        .cg-task-panel { width:400px; flex-shrink:0; background:#fff; border-left:1px solid #eef0f3; display:flex; flex-direction:column; min-height:0; z-index:5; }
        .cg-task-panel.hidden { display:none !important; }
        .cg-task-head { display:flex; align-items:center; justify-content:space-between; padding:18px 22px 6px; flex-shrink:0; }
        .cg-task-body { flex:1; overflow-y:auto; padding:14px 22px 22px; display:flex; flex-direction:column; gap:18px; font-size:13px; }
        .cg-task-foot { justify-content:flex-end; padding-top:14px; border-top:1px solid #f0f1f3; margin-top:auto; }
        .cg-task-panel [class*="bg-notion-lightBg"], .cg-task-panel [class*="bg-blue-50"], .cg-task-panel [class*="bg-rose-50"] { background:#f7f8fa !important; border:0 !important; border-radius:10px !important; padding:7px 10px !important; }
        .cg-task-panel [class*="text-blue-"], .cg-task-panel [class*="text-rose-"] { color:#4b5563 !important; }
        @media (max-width: 1100px) { .cg-task-panel { width:340px; } }

        /* ---- Caderno de Notas: coluna lateral direita (acompanha a rolagem) ---- */
        .nt-wrap { display:flex; gap:20px; align-items:flex-start; max-width:1100px; margin:0 auto; width:100%; }
        .nt-main { flex:1; min-width:0; }
        .nt-dock { position:sticky; top:0; align-self:flex-start; display:flex; gap:10px; flex-shrink:0; z-index:25; }
        .nt-panel { width:300px; display:none; max-height:calc(100vh - 150px); overflow-y:auto; }
        .nt-panel.open { display:block; }
        .nt-panel .nt-card { display:none; }
        .nt-panel .nt-card.on { display:flex; }
        .nt-rail { width:42px; display:flex; flex-direction:column; align-items:center; gap:6px; padding-top:2px; }
        .nt-rb { position:relative; width:36px; height:36px; border-radius:50%; display:flex; align-items:center; justify-content:center; background:transparent; color:#6b7280; font-size:14px; transition:background .15s,color .15s; cursor:pointer; }
        .nt-rb:hover { background:#e9ebef; color:#111827; }
        .nt-rb.on { background:#e3e6ec; color:#111827; box-shadow:inset 0 0 0 1px #d5d9e0; }
        .nt-rb::after { content:attr(data-tip); position:absolute; right:calc(100% + 8px); top:50%; transform:translateY(-50%); background:#1f2937; color:#fff; font-size:11px; font-weight:600; padding:4px 9px; border-radius:6px; white-space:nowrap; opacity:0; pointer-events:none; transition:opacity .12s; }
        .nt-rb:hover::after { opacity:1; }
        .nt-rbc { position:absolute; top:-2px; right:-2px; min-width:15px; height:15px; border-radius:999px; background:#4f46e5; color:#fff; font-size:9px; line-height:15px; text-align:center; padding:0 4px; display:none; }
        .nt-rbc.show { display:block; }
        @media (max-width: 1250px) { .nt-panel { position:absolute; right:52px; top:0; z-index:30; box-shadow:0 12px 32px rgba(15,23,42,.18); border-radius:12px; } }
        .nt-tabwrap { display:flex; align-items:stretch; background:var(--cg-sbbg,#fff); border-bottom:1px solid #eef0f3; flex-shrink:0; }
        .nt-tabwrap > #notes-tab-bar { border-bottom:0 !important; }
        .nt-tabtoggle { width:34px; flex-shrink:0; color:#9ca3af; font-size:13px; }
        .nt-tabtoggle:hover { color:#111827; }
        .nl-off { display:none !important; }
        #cg-sidebar { transition:width .2s cubic-bezier(.2,.8,.2,1); overflow:hidden; }
        #cg-sidebar.sb-collapsed { width:56px; }
        #cg-sidebar.sb-collapsed span, #cg-sidebar.sb-collapsed .sb-label, #cg-sidebar.sb-collapsed .sb-subjects { display:none !important; }
        #cg-sidebar.sb-collapsed .sb-top { justify-content:center; }
        #cg-sidebar.sb-collapsed .nav-btn { justify-content:center; padding-left:0; padding-right:0; }
        #cg-sidebar.sb-collapsed .p-3 { padding:8px 6px; }
        #cg-sidebar.sb-collapsed #sb-toggle i { transform:rotate(180deg); }
        .nt-card { background:#fff; border:1px solid #eef0f3; border-radius:12px; padding:12px 14px; display:flex; flex-direction:column; gap:8px; }
        .nt-h { display:flex; align-items:center; justify-content:space-between; gap:8px; font-size:12px; font-weight:700; color:#374151; }
        .nt-h i { margin-right:5px; }
        .nt-acts { display:flex; gap:10px; font-size:11px; font-weight:600; }
        .nt-badge { background:#f1f2f4; color:#6b7280; border-radius:999px; font-size:10px; padding:1px 7px; margin-left:4px; }
        .nt-select { width:100%; font-size:11px; border:1px solid #e5e7eb; border-radius:8px; padding:5px 8px; background:#fff; color:#374151; }
        .nt-list { display:flex; flex-direction:column; gap:6px; font-size:12px; }
        .nt-empty { color:#9ca3af; font-size:11px; }
        .nt-row { display:flex; align-items:center; gap:6px; background:#f7f8fa; border-radius:8px; padding:6px 8px; }
        .nt-row > button:not(.nt-chk):not(.nt-x) { flex:1; min-width:0; text-align:left; display:flex; align-items:center; gap:6px; }
        .nt-row .nt-t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .nt-row .nt-x { color:#d1d5db; flex-shrink:0; }
        .nt-row .nt-x:hover { color:#ef4444; }
        .nt-chk { width:16px; height:16px; border-radius:50%; border:1.5px solid #cbd5e1; background:#fff; flex-shrink:0; display:flex; align-items:center; justify-content:center; color:transparent; font-size:8px; padding:0; cursor:pointer; transition:all .15s; }
        .nt-chk:hover { border-color:#16a34a; color:#16a34a; }
        .nt-chk.on { background:#16a34a; border-color:#16a34a; color:#fff; }
        .nt-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; background:#9ca3af; }
        .nt-dot.done { background:#16a34a; } .nt-dot.doing { background:#2563eb; }

        /* ---- Painel Spotify (player proprio) + barra de aplicativos: segue o tema de cor do usuario ---- */
        .sp-panel { position: relative; width: 380px; flex-shrink: 0; display: flex; flex-direction: column; min-height: 0; background: #fff; border-left: 1px solid #eef0f3; z-index: 6; }
        .sp-panel.hidden { display: none !important; }
        .sp-resize { position: absolute; left: -4px; top: 0; bottom: 0; width: 8px; cursor: col-resize; z-index: 30; }
        .sp-resize:hover, .sp-resize.drag { background: var(--accent-light, #eef2ff); }
        .sp-head { height: 46px; display: flex; align-items: center; gap: 6px; padding: 0 14px; border-bottom: 1px solid #f0f1f3; font-size: 13px; font-weight: 700; color: #111827; flex-shrink: 0; }
        .sp-title i { margin-right: 6px; color: var(--accent-bg, #4f46e5); }
        .sp-sub { font-size: 11px; color: #9ca3af; font-weight: 600; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sp-acts { display: flex; gap: 2px; }
        .sp-acts button { width: 28px; height: 28px; border-radius: 8px; color: #9ca3af; font-size: 12px; cursor: pointer; }
        .sp-acts button:hover { background: #f4f5f7; color: #111827; }
        .sp-body { flex: 1; min-height: 0; display: flex; flex-direction: column; }
        .sp-card { padding: 18px; font-size: 12.5px; color: #4b5563; overflow-y: auto; line-height: 1.55; }
        .sp-card h3 { font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 4px; }
        .sp-card a { color: var(--accent-bg, #4f46e5); text-decoration: underline; cursor: pointer; }
        .sp-steps { margin: 10px 0 12px 18px; list-style: decimal; display: flex; flex-direction: column; gap: 6px; color: #6b7280; }
        .sp-code { display: block; margin-top: 4px; padding: 7px 9px; background: #f7f8fa; border-radius: 8px; font-family: Consolas, monospace; font-size: 11px; color: #111827; user-select: text; word-break: break-all; }
        .sp-input { width: 100%; background: #f7f8fa; border: 1px solid #eef0f3; border-radius: 10px; padding: 9px 12px; color: #111827; font-size: 12.5px; outline: none; user-select: text; }
        .sp-input:focus { border-color: var(--accent-bg, #4f46e5); box-shadow: 0 0 0 3px var(--accent-light, #eef2ff); }
        .sp-btn { margin-top: 10px; width: 100%; background: var(--accent-bg, #4f46e5); color: #fff; font-weight: 700; font-size: 12.5px; padding: 9px; border-radius: 10px; cursor: pointer; transition: filter .15s; }
        .sp-btn:hover { filter: brightness(1.1); }
        .sp-btn.ghost { background: #f4f5f7; color: #374151; }
        .sp-btn.ghost:hover { background: #eceef1; filter: none; }
        .sp-note { margin-top: 10px; font-size: 11.5px; color: #9ca3af; line-height: 1.5; }
        .sp-err { margin-top: 10px; padding: 8px 11px; border-radius: 10px; background: #fef2f2; color: #b91c1c; font-size: 11.5px; line-height: 1.4; }
        .sp-tabs { display: flex; gap: 6px; padding: 12px 14px 0; flex-shrink: 0; }
        .sp-tabs button { flex: 1; padding: 7px; border-radius: 10px; font-size: 12px; font-weight: 700; color: #6b7280; background: #f4f5f7; cursor: pointer; }
        .sp-tabs button.on { background: var(--accent-light, #eef2ff); color: var(--accent-bg, #4f46e5); }
        .sp-aviso { margin: 8px 14px 0; padding: 7px 11px; border-radius: 10px; background: #fef2f2; color: #b91c1c; font-size: 11.5px; line-height: 1.4; flex-shrink: 0; }
        .sp-aviso.ok { background: var(--accent-light, #eef2ff); color: var(--accent-bg, #4f46e5); }
        .sp-aviso:empty { display: none; }
        .sp-lista { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 10px 12px; }
        .sp-sec { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: #9ca3af; padding: 12px 6px 4px; }
        .sp-row { display: flex; align-items: center; gap: 10px; padding: 6px; border-radius: 10px; cursor: pointer; }
        .sp-row:hover { background: #f4f5f7; }
        .sp-row.tocando .sp-r-n { color: var(--accent-bg, #4f46e5); }
        .sp-cover { width: 40px; height: 40px; border-radius: 8px; object-fit: cover; background: #eceef1; flex-shrink: 0; }
        .sp-r-t { flex: 1; min-width: 0; }
        .sp-r-n { font-size: 12.5px; font-weight: 600; color: #111827; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .sp-r-s { font-size: 11px; color: #9ca3af; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .sp-r-b { width: 28px; height: 28px; border-radius: 50%; color: #6b7280; font-size: 11px; flex-shrink: 0; opacity: 0; cursor: pointer; }
        .sp-row:hover .sp-r-b { opacity: 1; }
        .sp-r-b:hover { background: #e5e7eb; color: #111827; }
        .sp-voltar { display: flex; align-items: center; gap: 6px; padding: 4px 4px 8px; font-size: 12.5px; font-weight: 700; color: #111827; }
        .sp-voltar button { width: 28px; height: 28px; border-radius: 8px; color: #6b7280; cursor: pointer; flex-shrink: 0; }
        .sp-voltar button:hover { background: #f4f5f7; color: #111827; }
        .sp-voltar button.sp-smart { color: var(--accent-bg, #4f46e5); }
        .sp-voltar span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sp-now { border-top: 1px solid #eef0f3; padding: 12px 14px; display: flex; flex-direction: column; gap: 9px; background: #fff; flex-shrink: 0; }
        .sp-now.hidden { display: none !important; }
        .sp-np { display: flex; align-items: center; gap: 10px; }
        .sp-ctrl { display: flex; align-items: center; gap: 3px; flex-shrink: 0; }
        .sp-ctrl button { width: 28px; height: 28px; border-radius: 50%; color: #4b5563; font-size: 12px; cursor: pointer; }
        .sp-ctrl button:hover { background: #f4f5f7; color: #111827; }
        .sp-ctrl button.on { color: var(--accent-bg, #4f46e5); background: var(--accent-light, #eef2ff); }
        .sp-ctrl .sp-pp { width: 34px; height: 34px; background: var(--accent-bg, #4f46e5); color: #fff; font-size: 13px; }
        .sp-ctrl .sp-pp:hover { background: var(--accent-bg, #4f46e5); color: #fff; filter: brightness(1.1); }
        .sp-seek, .sp-vol { display: flex; align-items: center; gap: 8px; font-size: 10px; color: #9ca3af; font-variant-numeric: tabular-nums; }
        .sp-seek input, .sp-vol input { flex: 1; accent-color: var(--accent-bg, #4f46e5); height: 4px; cursor: pointer; }
        .sp-vol i { width: 14px; text-align: center; }
        .sp-frame { position: fixed; left: -10px; top: -10px; width: 1px; height: 1px; border: 0; opacity: 0; pointer-events: none; }
        .cg-apprail { width: 48px; flex-shrink: 0; background: #fff; border-left: 1px solid #eef0f3; display: flex; flex-direction: column; align-items: center; padding-top: 12px; gap: 8px; z-index: 7; }
        .cg-apprail button { position: relative; width: 36px; height: 36px; border-radius: 50%; color: #9ca3af; font-size: 17px; cursor: pointer; transition: background .15s, color .15s; }
        .cg-apprail button:hover { background: #f4f5f7; color: #111827; }
        .cg-apprail button.on { background: var(--accent-light, #eef2ff); color: var(--accent-bg, #4f46e5); }
        .cg-apprail button::after { content: attr(data-tip); position: absolute; right: calc(100% + 8px); top: 50%; transform: translateY(-50%); background: #1f2937; color: #fff; font-size: 11px; font-weight: 600; padding: 4px 9px; border-radius: 6px; white-space: nowrap; opacity: 0; pointer-events: none; transition: opacity .12s; }
        .cg-apprail button:hover::after { opacity: 1; }

        /* ---- Tema: fundo cinza claro, cartoes brancos arredondados, icones 2D cinza ---- */
        body { background: #f4f5f7; }
        main { background: #f4f5f7 !important; }
        header { height: 46px; background: #ffffff !important; }
        [class*="bg-notion-sidebar"] { background: #ffffff !important; }
        aside { border-right-color: #eef0f3 !important; }
        .nav-btn { border-radius: 12px; padding: 0.55rem 0.75rem !important; font-size: 13px; color: #6b7280 !important; }
        .nav-btn:hover { background: #f3f4f6 !important; }
        .nav-btn.bg-notion-hover { background: var(--accent-bg, #4f46e5) !important; color: #ffffff !important; }
        .nav-btn.bg-notion-hover i { color: #ffffff !important; }
        [class*="hover:bg-notion-hover"] { border-radius: 10px; }
        main [class*="border-notion-border"][class*="bg-white"], main section .bg-white.border.rounded-lg {
            border-color: transparent !important; border-radius: 16px; box-shadow: 0 6px 20px rgba(17,24,39,0.06);
        }
        main h1 { font-weight: 900; letter-spacing: -0.04em; color: #111827; }
        .cg-hero { font-size: 3.25rem; line-height: 1; font-weight: 900; letter-spacing: -0.05em; color: #111827; }
        .mindmap-node:hover { outline: 1px dashed #cbd5e1; }
        .cg-label { font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: #9ca3af; }
        .cg-kpi { background: #ffffff; border-radius: 18px; padding: 1.1rem 1.25rem; box-shadow: 0 6px 20px rgba(17,24,39,0.06); }
        .cg-kpi-grid { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: repeat(3, auto); gap: 1.5rem; }
        .cg-kpi-big { grid-row: 1 / span 3; display: flex; flex-direction: column; justify-content: center; padding: 2rem; }
        .cg-kpi-bigvalue { font-size: 4.5rem; line-height: 1; font-weight: 900; letter-spacing: -0.05em; color: #111827; margin-top: 0.75rem; }
        @media (max-width: 760px) { .cg-kpi-grid { grid-template-columns: 1fr; } .cg-kpi-big { grid-row: auto; } }
        i[class*="text-indigo-"]:not(.cg-keep), i[class*="text-purple-"]:not(.cg-keep), i[class*="text-amber-"]:not(.cg-keep),
        i[class*="text-emerald-"]:not(.cg-keep), i[class*="text-blue-"]:not(.cg-keep), i[class*="text-cyan-"]:not(.cg-keep),
        i[class*="text-green-"]:not(.cg-keep), i[class*="text-yellow-"]:not(.cg-keep), i[class*="text-orange-"]:not(.cg-keep),
        i[class*="text-pink-"]:not(.cg-keep), i.accent-icon-color:not(.cg-keep) { color: #9ca3af !important; }
        /* Tabela de tarefas responsiva: colunas secundarias colapsam conforme a largura do container */
        #tasks-table-container { container-type: inline-size; }
        #tasks-table-container table { table-layout: fixed; width: 100%; }
        #tasks-table-container td, #tasks-table-container th { overflow-wrap: anywhere; }
        #tasks-table-container .tk-w-title { width: auto; }
        #tasks-table-container .tk-w-subj { width: 14%; }
        #tasks-table-container .tk-w-prog { width: 15%; }
        #tasks-table-container .tk-w-status { width: 13%; }
        #tasks-table-container .tk-w-date { width: 11%; }
        #tasks-table-container .tk-w-act { width: 44px; }
        #tasks-table-container .tk-w-chk, #tasks-table-container .tk-w-sym { width: 36px; }
        #tasks-table-container input[type="range"] { width: 100%; min-width: 40px; }
        #tasks-table-container select { max-width: 100%; }
        #tasks-table-container .tk-subj-inline { display: none; }
        @container (max-width: 860px) {
            #tasks-table-container .tk-col-date { display: none; }
            #tasks-table-container .tk-w-subj { width: 17%; }
            #tasks-table-container .tk-w-prog { width: 18%; }
            #tasks-table-container .tk-w-status { width: 17%; }
        }
        @container (max-width: 680px) {
            #tasks-table-container .tk-col-sym { display: none; }
            #tasks-table-container .tk-col-subj { display: none; }
            #tasks-table-container .tk-subj-inline { display: inline-block; }
            #tasks-table-container .tk-w-prog { width: 22%; }
            #tasks-table-container .tk-w-status { width: 22%; }
            #tasks-table-container .tk-pct { display: none; }
        }
        @container (max-width: 460px) {
            #tasks-table-container .tk-col-prog { display: none; }
            #tasks-table-container .tk-w-status { width: 40%; }
        }
    </style>
</head>
<body class="h-full bg-white text-gray-800 font-sans antialiased flex flex-col overflow-hidden">

    <!-- ================= TELA DE ENTRADA ================= -->
    <div id="loginGate" class="fixed inset-0 z-[70] bg-white hidden items-center justify-center p-6">
        <div class="w-full max-w-sm">
            <div class="flex items-center gap-3 mb-6 justify-center">
                <div class="w-10 h-10 rounded-xl accent-bg-badge flex items-center justify-center">
                    <i class="fa-solid fa-brain text-white text-lg"></i>
                </div>
                <div>
                    <div class="font-bold text-lg text-gray-900 tracking-tight leading-none">Cogni</div>
                    <div class="text-[11px] text-gray-500">Entre para sincronizar seus estudos</div>
                </div>
            </div>

            <div class="bg-white border border-gray-200 rounded-xl shadow-sm p-5 space-y-3">
                <input type="email" id="gateEmail" placeholder="seu@email.com" autocomplete="username"
                       class="w-full p-2.5 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-800 focus:outline-none focus:border-gray-400">
                <input type="password" id="gatePassword" placeholder="Senha (mínimo 6 caracteres)" autocomplete="current-password"
                       class="w-full p-2.5 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-800 focus:outline-none focus:border-gray-400">

                <div id="gateError" class="hidden text-[12px] text-rose-700 bg-rose-50 border border-rose-200 rounded-lg p-2"></div>

                <button id="gateSignIn" onclick="gateEntrar()"
                        class="w-full px-3 py-2.5 rounded-lg text-sm font-bold accent-bg-badge text-white hover:opacity-90 cursor-pointer transition">
                    Entrar
                </button>
                <button id="gateSignUp" onclick="gateCriarConta()"
                        class="w-full px-3 py-2 rounded-lg text-sm bg-gray-100 border border-gray-200 text-gray-700 hover:bg-gray-200 cursor-pointer transition">
                    Criar uma conta
                </button>
            </div>

            <p class="text-[11px] text-gray-400 text-center mt-4 leading-relaxed">
                Seus dados ficam isolados na sua conta.<br>Use o mesmo login em outros computadores para sincronizar.
            </p>
        </div>
    </div>


    <!-- HEADER TOPBAR -->
    <header class="h-9 bg-notion-sidebar border-b border-notion-border flex items-center justify-between px-3 text-xs text-notion-muted shrink-0 z-20">
        <div class="flex items-center space-x-3">
            <div class="flex items-center space-x-2 font-semibold text-gray-800">
                <i class="fa-solid fa-brain text-black text-sm"></i>
                <span class="font-bold tracking-tight text-gray-900">Cogni</span>
                <span id="app-version-badge" class="accent-bg-badge text-white text-[10px] px-1.5 py-0.5 rounded font-semibold">__APP_VERSION__</span>
            </div>

            <!-- Perfil: o avatar abre o modal de perfil. -->
            <button type="button" onclick="openProfileModal()" id="header-profile-btn" title="Perfil" class="shrink-0 rounded-full hover:opacity-80 transition cursor-pointer">
                <span id="user-avatar-container" class="block"></span>
            </button>

            <!-- Estado do banco na nuvem. -->
            <div class="flex items-center space-x-2 bg-white px-2.5 py-1 rounded-full border border-notion-border text-[11px]" id="cloudStatusBar" title="Banco de dados na nuvem">
                <span class="w-2 h-2 rounded-full bg-gray-300" id="save-dot"></span>
                <i class="fa-solid fa-cloud text-gray-400" id="cloud-status-icon"></i>
                <span class="text-gray-500 font-sans" id="last-save-time">Conectando...</span>
            </div>
        </div>

        <!-- Busca global: tarefas, cadernos de notas, matérias e post-its num
             unico campo, sem precisar saber em qual tela cada coisa esta. -->
        <div class="relative flex-1 max-w-sm mx-3">
            <i class="fa-solid fa-magnifying-glass absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400 text-[11px]"></i>
            <input type="text" id="global-search-input" placeholder="Buscar em tudo..." autocomplete="off"
                   oninput="buscaGlobal(this.value)" onkeydown="if(event.key==='Escape'){this.value='';buscaGlobal('');this.blur();}"
                   class="w-full bg-white border border-notion-border rounded-md pl-7 pr-2 py-1 text-[11px] text-gray-700 focus:outline-none focus:border-indigo-300">
            <div id="global-search-results" class="hidden absolute left-0 right-0 top-full mt-1 bg-white border border-notion-border rounded-md shadow-lg max-h-80 overflow-y-auto z-30 text-xs"></div>
        </div>

        <div class="flex items-center space-x-1.5 text-xs">
            <button type="button" onclick="exportarTarefasCSV()" title="Exportar tarefas (CSV)" class="bg-white hover:bg-gray-100 border border-notion-border text-gray-600 px-2.5 py-1 rounded-full font-medium flex items-center gap-1.5">
                <i class="fa-solid fa-file-export text-gray-400"></i><span>Exportar</span>
            </button>
            <button type="button" onclick="importarTarefasCSV()" title="Importar tarefas (CSV)" class="bg-white hover:bg-gray-100 border border-notion-border text-gray-600 px-2.5 py-1 rounded-full font-medium flex items-center gap-1.5">
                <i class="fa-solid fa-file-import text-gray-400"></i><span>Importar</span>
            </button>
        </div>
    </header>

    <div class="flex-1 flex overflow-hidden relative">
        <!-- SIDEBAR -->
        <aside id="cg-sidebar" class="w-64 bg-notion-sidebar border-r border-notion-border flex flex-col justify-between shrink-0 select-none z-10">
            <div class="p-3 space-y-4 overflow-y-auto">
                
                <div class="space-y-0.5">
                    <div class="sb-top flex items-center justify-between px-2 mb-1">
                        <div class="sb-label text-[10px] font-semibold text-notion-muted uppercase tracking-wider">Menu Principal</div>
                        <button type="button" id="sb-toggle" onclick="toggleSidebar()" title="Recolher menu" class="text-gray-400 hover:text-gray-700 p-0.5"><i class="fa-solid fa-angles-left text-[11px]"></i></button>
                    </div>
                    
                    <!-- Icones do Menu Principal em cinza neutro (2D), sem cor
                         por item - a cor so aparece como feedback de estado
                         (aba ativa, badges), nao decorando cada icone. -->
                    <button onclick="switchTab('dashboard')" id="nav-dashboard" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition bg-notion-hover">
                        <i class="fa-solid fa-chart-pie w-4 text-center text-gray-400"></i>
                        <span>Dashboard de Estudos</span>
                    </button>

                    <button onclick="filterBySubject('ALL')" id="nav-tasks" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-list-check w-4 text-center text-gray-400"></i>
                        <span>Tarefas & Projetos</span>
                        <span id="pending-count-badge" class="ml-auto bg-gray-200 text-gray-700 text-[10px] px-1.5 py-0.2 rounded-full font-bold">0</span>
                    </button>

                    <button onclick="switchTab('flashcards')" id="nav-flashcards" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-layer-group w-4 text-center text-gray-400"></i>
                        <span>Flashcards</span>
                        <span id="decks-count-badge" class="ml-auto bg-gray-200 text-gray-700 text-[10px] px-1.5 py-0.2 rounded-full font-bold">0</span>
                    </button>

                    <button onclick="switchTab('notes')" id="nav-notes" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-book-open w-4 text-center text-gray-400"></i>
                        <span>Caderno de Notas</span>
                    </button>

                    <button onclick="switchTab('postits')" id="nav-postits" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-note-sticky w-4 text-center text-gray-400"></i>
                        <span>Mural de Post-its</span>
                    </button>

                    <button onclick="switchTab('resources')" id="nav-resources" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-paperclip w-4 text-center text-gray-400"></i>
                        <span>Central de Anexos</span>
                    </button>

                    <button onclick="switchTab('calendar')" id="nav-calendar" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-calendar-days w-4 text-center text-gray-400"></i>
                        <span>Calendário</span>
                    </button>

                    <button onclick="switchTab('mindmap')" id="nav-mindmap" class="nav-btn w-full flex items-center space-x-2.5 px-2.5 py-1.5 rounded text-xs font-medium text-gray-700 hover:bg-notion-hover transition">
                        <i class="fa-solid fa-diagram-project w-4 text-center text-gray-400"></i>
                        <span>Mapa Mental</span>
                    </button>
                </div>

                <div class="sb-subjects space-y-1.5 pt-3 border-t border-notion-border">
                    <div class="flex items-center justify-between px-2 mb-1">
                        <span class="text-[10px] font-semibold text-notion-muted uppercase tracking-wider">Matérias Ativas</span>
                        <div class="flex items-center space-x-1">
                            <button onclick="deleteSelectedSubjects()" id="btn-bulk-delete-subjects" class="hidden text-xs text-red-600 hover:bg-red-50 p-0.5 rounded" title="Excluir Matérias Selecionadas">
                                <i class="fa-solid fa-trash-can"></i>
                            </button>
                            <button onclick="openSubjectModal()" class="text-xs accent-text-color hover:underline p-0.5"><i class="fa-solid fa-plus"></i> Nova</button>
                        </div>
                    </div>
                    <div id="subject-list" class="space-y-1.5"></div>
                </div>
            </div>
        </aside>

        <!-- MAIN VIEW AREA -->
        <main class="flex-1 bg-white overflow-y-auto relative flex flex-col min-w-0">
            
            <!-- VIEW DASHBOARD -->
            <section id="view-dashboard" class="view-content p-6 md:p-8 max-w-6xl mx-auto w-full space-y-6">
                <div class="flex flex-col sm:flex-row sm:items-end justify-between pb-2 gap-4">
                    <div>
                        <h1 class="cg-hero" id="dashboard-greeting"><span>Olá,</span> <span class="text-gray-400">Estudante</span></h1>
                        <p class="text-sm text-gray-400 mt-2">Faça o hoje valer a pena.</p>
                    </div>
                    <div class="flex flex-wrap space-x-2">
                        <button onclick="openFocusTimerModal()" class="accent-btn-bg hover:opacity-90 text-white text-xs px-3 py-2 rounded-md font-medium transition flex items-center space-x-1.5 shadow-sm">
                            <i class="fa-solid fa-stopwatch"></i>
                            <span>Timer de Foco</span>
                        </button>
                        <button onclick="openNewTaskModal()" class="bg-gray-900 hover:bg-black text-white text-xs px-3 py-2 rounded-md font-medium transition flex items-center space-x-1.5 shadow-sm">
                            <i class="fa-solid fa-plus"></i>
                            <span>Nova Tarefa</span>
                        </button>
                    </div>
                </div>

                <!-- KPIs: "Horas na Semana" em destaque (bloco grande a esquerda),
                     os outros tres empilhados a direita. Sem icones: so texto. -->
                <div class="cg-kpi-grid">
                    <div class="cg-kpi cg-kpi-big cursor-pointer" onclick="openFocusTimerModal()">
                        <div class="cg-label">Horas na Semana</div>
                        <div class="cg-kpi-bigvalue" id="kpi-focus-hours">0.0 h</div>
                        <div class="text-sm text-gray-400 mt-2" id="kpi-focus-hours-sub">Semana Atual</div>
                    </div>
                    <div class="cg-kpi cursor-pointer" onclick="switchTab('tasks')">
                        <div class="cg-label">Tarefas Concluídas</div>
                        <div class="flex items-baseline justify-between mt-2">
                            <span class="text-3xl font-black text-gray-900 tracking-tight" id="kpi-completed-tasks">0/0</span>
                            <span class="text-sm font-semibold text-gray-400" id="kpi-completion-rate">0%</span>
                        </div>
                    </div>
                    <div class="cg-kpi cursor-pointer" onclick="switchTab('flashcards')">
                        <div class="cg-label">Flashcards Retidos</div>
                        <div class="flex items-baseline justify-between mt-2">
                            <span class="text-3xl font-black text-gray-900 tracking-tight" id="kpi-flashcards-mastered">0/0</span>
                            <span class="text-sm font-semibold text-gray-400" id="kpi-flashcards-rate">0% dominados</span>
                        </div>
                    </div>
                    <div class="cg-kpi cursor-pointer" onclick="switchTab('notes')">
                        <div class="cg-label">Notas Cadastradas</div>
                        <div class="flex items-baseline justify-between mt-2">
                            <span class="text-3xl font-black text-gray-900 tracking-tight" id="kpi-notes-count">0</span>
                            <span class="text-sm font-semibold text-gray-400">Caderno</span>
                        </div>
                    </div>
                </div>

                <!-- CHARTS ROW: HORAS DA SEMANA E DISTRIBUIÇÃO -->
                <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div class="p-5 border border-notion-border rounded-lg bg-white shadow-sm">
                        <div class="flex items-center justify-between mb-4">
                            <h3 class="text-sm font-semibold text-gray-800 flex items-center gap-2">
                                <i class="fa-solid fa-chart-column accent-icon-color"></i> Horas Dedicadas na Semana
                            </h3>
                            <span class="text-[11px] font-mono font-medium text-gray-500 bg-gray-100 px-2 py-0.5 rounded" id="weekly-chart-range-label">Semana Atual</span>
                        </div>
                        <div class="h-56">
                            <canvas id="studyHoursChart"></canvas>
                        </div>
                    </div>

                    <div class="p-5 border border-notion-border rounded-lg bg-white shadow-sm flex flex-col">
                        <h3 class="text-sm font-semibold text-gray-800 mb-4 flex items-center gap-2">
                            <i class="fa-solid fa-chart-pie text-emerald-500"></i> Distribuição de Tarefas por Matéria
                        </h3>
                        <div class="h-56 flex-1 relative flex items-center justify-center">
                            <canvas id="subjectsChart"></canvas>
                        </div>
                    </div>
                </div>

                <!-- SEU PROGRESSO: mapa anual de consistencia -->
                <section>
                    <div class="flex items-center gap-4 px-2" style="margin-bottom:1.75rem; margin-top:.5rem">
                        <h3 class="cg-hero" style="font-size:1.6rem">Seu Progresso</h3>
                    </div>
                    <div class="cg-year-card">
                        <div class="cg-year-scroll">
                            <div class="cg-year-inner">
                                <div id="monthly-heatmap-grid" class="cg-year-grid"></div>
                                <div class="cg-year-months" id="cg-year-months"></div>
                            </div>
                        </div>
                        <div class="cg-year-foot">
                            <span id="monthly-total-hours-badge">0.0 h</span>
                            <span id="monthly-streak-badge">Sequência: 0 dias</span>
                            <span id="monthly-active-days-summary">0 dias ativos</span>
                            <span id="monthly-heatmap-sublabel" class="hidden"></span>
                            <span class="cg-year-legend" id="monthly-heatmap-legend"></span>
                        </div>
                    </div>
                </section>

                <!-- HORAS POR MATERIA -->
                <div class="border border-notion-border rounded-lg bg-white p-5 shadow-sm space-y-3">
                    <div class="flex items-center justify-between">
                        <h3 class="text-sm font-semibold text-gray-800 flex items-center gap-2">
                            <i class="fa-solid fa-clock accent-icon-color"></i> Horas por Matéria
                        </h3>
                        <select id="subject-hours-range" onchange="renderSubjectHours()" class="text-xs border border-notion-border rounded px-2 py-1 bg-white text-gray-700">
                            <option value="all">Total</option>
                            <option value="month">Este mês</option>
                            <option value="week">Esta semana</option>
                        </select>
                    </div>
                    <div id="subject-hours-list" class="space-y-3"></div>
                </div>

                <!-- PROGRESSO POR MATÉRIA -->
                <div class="border border-notion-border rounded-lg bg-white p-5 shadow-sm space-y-3">
                    <div class="flex items-center justify-between">
                        <h3 class="text-sm font-semibold text-gray-800 flex items-center gap-2">
                            <i class="fa-solid fa-graduation-cap accent-icon-color"></i> Progresso por Matéria (% de Tarefas & Etapas)
                        </h3>
                    </div>
                    <div id="dashboard-subject-progress-list" class="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1"></div>
                </div>
            </section>

            <!-- VIEW TASKS -->
            <section id="view-tasks" class="view-content hidden p-6 md:p-8 max-w-6xl mx-auto w-full space-y-6">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between border-b border-notion-border pb-4 gap-3">
                    <div>
                        <h1 class="text-2xl font-bold text-gray-900 tracking-tight flex items-center gap-2">
                            <span class="flex items-center gap-2"><i class="fa-solid fa-list-check text-gray-400 text-xl"></i>Tarefas & Projetos</span>
                            <span id="active-subject-filter-badge" class="hidden accent-bg-badge text-white text-xs px-2.5 py-1 rounded-full font-bold"></span>
                        </h1>
                    </div>
                    <div class="flex items-center space-x-2">
                        <div class="flex items-center bg-gray-100 p-0.5 rounded-lg border border-notion-border text-xs">
                            <button id="view-mode-table-btn" onclick="setTasksViewMode('table')" class="px-2.5 py-1 rounded font-bold bg-white accent-text-color shadow-2xs">Tabela</button>
                            <button id="view-mode-kanban-btn" onclick="setTasksViewMode('kanban')" class="px-2.5 py-1 rounded font-bold text-gray-600 hover:text-gray-900">Kanban</button>
                        </div>

                        <button onclick="deleteSelectedTasks()" id="btn-bulk-delete-tasks" class="hidden bg-red-600 hover:bg-red-700 text-white text-xs px-3 py-2 rounded-md font-medium shadow-sm">
                            <i class="fa-solid fa-trash mr-1"></i> Excluir Selecionadas (<span id="selected-tasks-count">0</span>)
                        </button>
                        <button onclick="filterBySubject('ALL')" id="btn-clear-subject-filter" class="hidden text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-2 rounded-md font-medium">
                            Mostrar Todas
                        </button>
                        <button onclick="openNewTaskModal()" class="bg-gray-900 hover:bg-black text-white text-xs px-3 py-2 rounded-md font-medium flex items-center space-x-1.5 shadow-sm">
                            <i class="fa-solid fa-plus"></i>
                            <span>Adicionar Tarefa</span>
                        </button>
                    </div>
                </div>

                <div class="flex flex-wrap items-center justify-between gap-3 bg-notion-lightBg p-3 rounded-lg border border-notion-border">
                    <div class="flex flex-wrap items-center gap-2">
                        <span class="text-xs font-medium text-notion-muted">Matéria:</span>
                        <select id="filter-subject" onchange="filterBySubject(this.value)" class="text-xs border border-notion-border rounded px-2 py-1.5 bg-white text-gray-700 focus:outline-none">
                            <option value="ALL">Todas as Matérias</option>
                        </select>

                        <select id="filter-priority" onchange="renderTasks()" class="text-xs border border-notion-border rounded px-2 py-1.5 bg-white text-gray-700 focus:outline-none">
                            <option value="ALL">Todas as Prioridades</option>
                            <option value="Alta">Alta Prioridade</option>
                            <option value="Média">Média Prioridade</option>
                            <option value="Baixa">Baixa Prioridade</option>
                        </select>
                    </div>
                    <input type="text" id="search-tasks" oninput="renderTasks()" placeholder="Buscar tarefa..." class="px-3 py-1.5 border border-notion-border rounded text-xs bg-white text-gray-800 focus:outline-none w-full sm:w-56">
                </div>

                <div id="tasks-table-container" class="border border-notion-border rounded-lg overflow-hidden bg-white shadow-sm">
                    <div>
                        <table class="w-full text-left text-xs text-gray-700">
                            <thead class="bg-notion-sidebar border-b border-notion-border text-notion-muted font-semibold">
                                <tr>
                                    <th class="tk-w-chk py-3 px-2 text-center">
                                        <input type="checkbox" id="select-all-tasks" onchange="toggleSelectAllTasks(this)" class="rounded border-gray-300 accent-text-color focus:ring-0 cursor-pointer">
                                    </th>
                                    <th class="tk-w-sym tk-col-sym py-3 px-2 text-center" title="Símbolo"><i class="fa-solid fa-shapes"></i></th>
                                    <th class="tk-w-title py-3 px-3">Título da Tarefa</th>
                                    <th class="tk-w-subj tk-col-subj py-3 px-3">Matéria</th>
                                    <th class="tk-w-prog tk-col-prog py-3 px-3">Progresso</th>
                                    <th class="tk-w-status py-3 px-3">Status</th>
                                    <th class="tk-w-date tk-col-date py-3 px-3">Data Limite</th>
                                    <th class="tk-w-act py-3 px-2 text-right" title="Ações"><i class="fa-solid fa-ellipsis"></i></th>
                                </tr>
                            </thead>
                            <tbody id="tasks-table-body" class="divide-y divide-notion-border"></tbody>
                        </table>
                    </div>
                </div>

                <div id="completed-tasks-wrapper" class="hidden border border-emerald-200 bg-emerald-50/30 rounded-lg p-4 space-y-3">
                    <button onclick="toggleCompletedTasksSection()" class="flex items-center justify-between w-full text-xs font-bold text-emerald-900">
                        <span class="flex items-center gap-2">
                            <i class="fa-solid fa-circle-check text-emerald-600"></i>
                            <span>Tarefas Concluídas (<span id="completed-tasks-count-label">0</span>)</span>
                        </span>
                        <i id="completed-chevron-icon" class="fa-solid fa-chevron-down"></i>
                    </button>
                    <div id="completed-tasks-container" class="space-y-2 pt-2 border-t border-emerald-200/60 hidden"></div>
                </div>

                <div id="tasks-kanban-container" class="hidden grid grid-cols-1 md:grid-cols-3 gap-4">
                    <div class="bg-gray-50 border border-notion-border rounded-xl p-3 space-y-3">
                        <div class="flex items-center justify-between text-xs font-bold text-gray-700 pb-2 border-b">
                            <span>A Fazer</span>
                            <span id="kanban-todo-count" class="bg-gray-200 text-gray-800 px-2 py-0.5 rounded-full text-[10px]">0</span>
                        </div>
                        <div id="kanban-col-todo" class="space-y-2 min-h-[150px]"></div>
                    </div>

                    <div class="bg-blue-50/40 border border-blue-200 rounded-xl p-3 space-y-3">
                        <div class="flex items-center justify-between text-xs font-bold text-blue-900 pb-2 border-b border-blue-200">
                            <span>Em Andamento</span>
                            <span id="kanban-doing-count" class="bg-blue-200 text-blue-900 px-2 py-0.5 rounded-full text-[10px]">0</span>
                        </div>
                        <div id="kanban-col-doing" class="space-y-2 min-h-[150px]"></div>
                    </div>

                    <div class="bg-emerald-50/40 border border-emerald-200 rounded-xl p-3 space-y-3">
                        <div class="flex items-center justify-between text-xs font-bold text-emerald-900 pb-2 border-b border-emerald-200">
                            <span>Concluído</span>
                            <span id="kanban-done-count" class="bg-emerald-200 text-emerald-900 px-2 py-0.5 rounded-full text-[10px]">0</span>
                        </div>
                        <div id="kanban-col-done" class="space-y-2 min-h-[150px]"></div>
                    </div>
                </div>
            </section>

            <!-- VIEW NOTES -->
            <section id="view-notes" class="view-content hidden flex-1 flex h-full">
                <div id="notes-list-col" class="w-64 border-r border-notion-border bg-notion-sidebar flex flex-col shrink-0">
                    <div class="p-3 border-b border-notion-border flex items-center justify-between">
                        <span class="text-xs font-semibold text-gray-700 flex items-center gap-1.5">
                            <i class="fa-solid fa-book-bookmark text-amber-500"></i> Suas Notas
                        </span>
                        <div class="flex items-center space-x-1">
                            <button onclick="deleteSelectedNotes()" id="btn-bulk-delete-notes" class="hidden text-xs text-red-600 hover:bg-red-50 p-1 rounded" title="Excluir Selecionadas">
                                <i class="fa-solid fa-trash"></i>
                            </button>
                            <button onclick="createNewNote()" class="text-xs bg-white hover:bg-gray-100 border border-notion-border px-2 py-1 rounded shadow-sm text-gray-700 transition">
                                <i class="fa-solid fa-plus mr-1"></i> Nova
                            </button>
                        </div>
                    </div>
                    <div id="notes-sidebar-list" class="p-2 space-y-1 overflow-y-auto flex-1"></div>
                </div>

                <div class="flex-1 flex flex-col bg-white overflow-hidden">
                    <!-- Abas de cadernos abertos ao mesmo tempo, estilo Notion/Google
                         Docs: abrir um caderno Y nao fecha o caderno X que ja estava
                         aberto. Ver renderNoteTabs()/openNoteInTab() abaixo. -->
                    <div class="nt-tabwrap">
                        <button type="button" id="nl-toggle" onclick="toggleListaNotas()" title="Ocultar lista de notas" class="nt-tabtoggle"><i class="fa-solid fa-table-columns"></i></button>
                        <div id="notes-tab-bar" class="flex items-center gap-1 px-3 pt-2 bg-notion-sidebar border-b border-notion-border overflow-x-auto shrink-0" style="flex:1;min-width:0"></div>
                    </div>
                    <div class="flex-1 overflow-y-auto p-8">
                    <div class="nt-wrap">
                    <div class="nt-main space-y-5">
                        <div class="flex items-center justify-end border-b border-notion-border pb-3">
                            <button onclick="deleteCurrentNote()" class="text-xs text-red-500 hover:text-red-700 px-2 py-1 rounded hover:bg-red-50">
                                <i class="fa-solid fa-trash mr-1"></i> Excluir Nota
                            </button>
                        </div>

                        <input type="text" id="note-editor-title" oninput="updateCurrentNote()" placeholder="Título da anotação..." class="w-full text-3xl font-bold text-gray-900 border-none outline-none placeholder-gray-300 bg-transparent">

                        <div class="sticky top-0 z-20 flex flex-wrap items-center gap-1.5 p-2 bg-notion-lightBg border border-notion-border rounded-lg text-xs shadow-sm">
                            <button type="button" onclick="execDocCmd('bold')" title="Negrito" class="w-7 h-7 bg-white border rounded font-bold flex items-center justify-center">B</button>
                            <button type="button" onclick="execDocCmd('italic')" title="Itálico" class="w-7 h-7 bg-white border rounded italic flex items-center justify-center">I</button>
                            <button type="button" onclick="execDocCmd('underline')" title="Sublinhado" class="w-7 h-7 bg-white border rounded underline flex items-center justify-center">U</button>
                            
                            <div class="h-4 w-px bg-gray-300 mx-0.5"></div>

                            <button type="button" onclick="execDocCmd('insertUnorderedList')" title="Lista Marcadores" class="w-7 h-7 bg-white border rounded flex items-center justify-center"><i class="fa-solid fa-list-ul"></i></button>
                            <button type="button" onclick="execDocCmd('insertOrderedList')" title="Lista Numerada" class="w-7 h-7 bg-white border rounded flex items-center justify-center"><i class="fa-solid fa-list-ol"></i></button>

                            <div class="h-4 w-px bg-gray-300 mx-0.5"></div>

                            <button type="button" onclick="execDocCmd('foreColor', '#dc2626')" class="w-4 h-4 rounded-full bg-red-600"></button>
                            <button type="button" onclick="execDocCmd('foreColor', '#2563eb')" class="w-4 h-4 rounded-full bg-blue-600"></button>
                            <button type="button" onclick="execDocCmd('foreColor', '#16a34a')" class="w-4 h-4 rounded-full bg-green-600"></button>

                            <button type="button" onclick="execDocCmd('hiliteColor', '#fef08a')" class="w-4 h-4 rounded-full bg-yellow-300 border" title="Marca-Texto Amarelo"></button>
                            <button type="button" onclick="execDocCmd('hiliteColor', '#bbf7d0')" class="w-4 h-4 rounded-full bg-emerald-300 border" title="Marca-Texto Verde"></button>
                            <button type="button" onclick="execDocCmd('hiliteColor', '#bfdbfe')" class="w-4 h-4 rounded-full bg-blue-300 border" title="Marca-Texto Azul"></button>

                            <button type="button" onmousedown="event.preventDefault()" onclick="criarTarefaDaSelecao()" title="Criar tarefa a partir do texto selecionado" class="ml-auto text-[11px] text-emerald-700 font-bold px-2 py-1 bg-white border rounded"><i class="fa-solid fa-list-check mr-1"></i>Tarefa</button>
                            <button type="button" onclick="clearFormattingCmd()" class="text-[11px] text-rose-600 font-bold px-2 py-1 bg-white border rounded">Limpar</button>
                        </div>

                        <div id="note-editor-content" 
                             contenteditable="true" 
                             oninput="updateCurrentNote()" 
                             data-placeholder="Digite suas anotações aqui..." 
                             class="w-full min-h-[350px] border border-notion-border rounded-lg p-5 outline-none text-sm leading-relaxed bg-white font-sans overflow-y-auto"></div>
                    </div>

                    <div class="nt-dock">
<div id="nt-panel" class="nt-panel">
                        <div class="nt-card" data-p="materia">
                            <div class="nt-h"><span><i class="fa-solid fa-folder-open accent-icon-color"></i> Matéria</span></div>
                            <select id="note-subject-select" onchange="updateNoteSubject(this.value)" class="nt-select"></select>
                        </div>

                        <div class="nt-card" data-p="anexos">
                            <div class="nt-h">
                                <span><i class="fa-solid fa-paperclip accent-icon-color"></i> Anexos &amp; Links</span>
                                <span class="nt-acts">
                                    <button type="button" onclick="addLinkToNote()" class="accent-text-color hover:underline">+ Link</button>
                                    <button type="button" onclick="attachFileToNote()" class="text-rose-600 hover:underline">+ Arquivo</button>
                                </span>
                            </div>
                            <div id="note-resources-list" class="nt-list"></div>
                        </div>

                        <div class="nt-card" data-p="tarefas">
                            <div class="nt-h">
                                <span><i class="fa-solid fa-list-check accent-icon-color"></i> Tarefas <b id="note-tasks-count" class="nt-badge">0</b></span>
                                <span class="nt-acts">
                                    <button type="button" onmousedown="event.preventDefault()" onclick="criarTarefaDaSelecao()" class="text-emerald-700 hover:underline" title="Cria uma tarefa com o texto selecionado na nota">+ Da seleção</button>
                                </span>
                            </div>
                            <select id="note-link-task-select" onchange="vincularTarefaANota(this.value); this.value='';" class="nt-select">
                                <option value="">+ Vincular tarefa existente</option>
                            </select>
                            <div id="note-linked-tasks-list" class="nt-list"></div>
                        </div>

                        <div class="nt-card" data-p="postits">
                            <div class="nt-h"><span><i class="fa-solid fa-note-sticky accent-icon-color"></i> Post-its <b id="note-postits-count" class="nt-badge">0</b></span></div>
                            <select id="note-link-postit-select" onchange="vincularPostitANota(this.value); this.value='';" class="nt-select">
                                <option value="">+ Vincular post-it</option>
                            </select>
                            <div id="note-linked-postits-list" class="nt-list"></div>
                        </div>
                    </div>
<div class="nt-rail">
    <button type="button" class="nt-rb" data-b="materia" data-tip="Matéria" onclick="ntAbrirPainel('materia')"><i class="fa-solid fa-folder-open"></i></button>
    <button type="button" class="nt-rb" data-b="anexos" data-tip="Anexos &amp; Links" onclick="ntAbrirPainel('anexos')"><i class="fa-solid fa-paperclip"></i><b id="rb-anexos" class="nt-rbc"></b></button>
    <button type="button" class="nt-rb" data-b="tarefas" data-tip="Tarefas" onclick="ntAbrirPainel('tarefas')"><i class="fa-solid fa-list-check"></i><b id="rb-tarefas" class="nt-rbc"></b></button>
    <button type="button" class="nt-rb" data-b="postits" data-tip="Post-its" onclick="ntAbrirPainel('postits')"><i class="fa-solid fa-note-sticky"></i><b id="rb-postits" class="nt-rbc"></b></button>
</div>
</div>
                    </div>
                    </div>
                </div>
            </section>

            <!-- VIEW POSTITS CANVAS -->
            <section id="view-postits" class="view-content hidden flex-1 flex flex-col h-full overflow-hidden">
                <div class="h-12 border-b border-notion-border px-6 flex items-center justify-between bg-white shrink-0">
                    <h2 class="text-sm font-bold text-gray-800 flex items-center gap-1.5">
                        <span class="flex items-center gap-2"><i class="fa-solid fa-note-sticky text-gray-400"></i>Mural de Post-its (Canvas)</span>
                    </h2>
                    <div class="flex items-center space-x-2">
                        <button onclick="organizePostitsBySubject()" class="accent-btn-bg hover:opacity-90 text-white text-xs px-3 py-1.5 rounded-md font-medium transition flex items-center space-x-1.5 shadow-sm">
                            <i class="fa-solid fa-diagram-project"></i>
                            <span>Organizar por Matéria</span>
                        </button>
                        <button onclick="createNewPostit()" class="bg-rose-500 hover:bg-rose-600 text-white text-xs px-3 py-1.5 rounded-md font-medium shadow-sm">
                            + Novo Post-it
                        </button>
                    </div>
                </div>

                <div id="canvas-container" class="flex-1 canvas-area relative overflow-auto p-6 min-h-[700px]"></div>
            </section>

            <!-- VIEW FLASHCARDS -->
            <section id="view-flashcards" class="view-content hidden p-6 md:p-8 max-w-6xl mx-auto w-full space-y-6">
                <div class="flex items-center justify-between border-b pb-4">
                    <h1 class="text-2xl font-bold text-gray-900 flex items-center gap-2"><i class="fa-solid fa-layer-group text-gray-400 text-xl"></i>Baralhos de Flashcards</h1>
                    <div class="flex items-center space-x-2">
                        <button onclick="deleteSelectedDecks()" id="btn-bulk-delete-decks" class="hidden bg-red-600 hover:bg-red-700 text-white text-xs px-3 py-2 rounded-md font-medium shadow-sm">
                            <i class="fa-solid fa-trash mr-1"></i> Excluir Selecionados (<span id="selected-decks-count">0</span>)
                        </button>
                        <button onclick="openDeckEditorModal()" class="accent-btn-bg hover:opacity-90 text-white text-xs px-3 py-2 rounded-md font-bold shadow-sm">+ Criar Baralho</button>
                    </div>
                </div>
                <div id="flashcard-decks-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5"></div>
            </section>

            <!-- VIEW CENTRAL RESOURCES -->
            <section id="view-resources" class="view-content hidden p-6 md:p-8 max-w-6xl mx-auto w-full space-y-6">
                <h1 class="text-2xl font-bold text-gray-900 pb-2 flex items-center gap-2"><i class="fa-solid fa-paperclip text-gray-400 text-xl"></i>Central de Anexos & Links</h1>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <div class="border rounded-lg p-5 bg-white shadow-sm space-y-3">
                        <h3 class="text-sm font-semibold border-b pb-2 flex items-center gap-2"><i class="fa-solid fa-globe text-blue-500"></i> Links & Videoaulas</h3>
                        <div id="all-links-list" class="space-y-2 text-xs"></div>
                    </div>
                    <div class="border rounded-lg p-5 bg-white shadow-sm space-y-3">
                        <h3 class="text-sm font-semibold border-b pb-2 flex items-center gap-2"><i class="fa-solid fa-file-pdf text-gray-400"></i> Arquivos do Computador</h3>
                        <div id="all-files-list" class="space-y-2 text-xs"></div>
                    </div>
                </div>
            </section>

            <!-- VIEW CALENDARIO -->
            <section id="view-calendar" class="view-content hidden p-6 md:p-8 max-w-6xl mx-auto w-full space-y-4">
                <div class="flex items-center justify-between border-b border-notion-border pb-4">
                    <h1 class="text-2xl font-bold text-gray-900 tracking-tight flex items-center gap-2"><i class="fa-solid fa-calendar-days text-gray-400 text-xl"></i>Calendário</h1>
                    <div class="flex items-center gap-2">
                        <button onclick="calendarPrevMonth()" class="w-8 h-8 border rounded-md hover:bg-gray-100 text-gray-600"><i class="fa-solid fa-chevron-left"></i></button>
                        <span id="calendar-month-label" class="text-sm font-semibold text-gray-800 w-36 text-center"></span>
                        <button onclick="calendarNextMonth()" class="w-8 h-8 border rounded-md hover:bg-gray-100 text-gray-600"><i class="fa-solid fa-chevron-right"></i></button>
                        <button onclick="calendarToday()" class="text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-2 rounded-md font-medium ml-2">Hoje</button>
                    </div>
                </div>
                <div class="grid grid-cols-7 gap-1.5 text-center text-[10px] font-semibold text-notion-muted uppercase">
                    <span>Dom</span><span>Seg</span><span>Ter</span><span>Qua</span><span>Qui</span><span>Sex</span><span>Sáb</span>
                </div>
                <div id="calendar-grid" class="grid grid-cols-7 gap-1.5"></div>
            </section>

            <!-- VIEW MAPA MENTAL -->
            <section id="view-mindmap" class="view-content hidden flex-1 flex h-full overflow-hidden">
                <div class="w-64 border-r border-notion-border bg-notion-sidebar flex flex-col shrink-0">
                    <div class="p-3 border-b border-notion-border flex items-center justify-between">
                        <span class="text-xs font-semibold text-gray-700 flex items-center gap-1.5">
                            <i class="fa-solid fa-diagram-project text-gray-400"></i> Mapas Mentais
                        </span>
                        <button onclick="createNewMindMap()" class="text-xs bg-white hover:bg-gray-100 border border-notion-border px-2 py-1 rounded shadow-sm text-gray-700 transition">
                            <i class="fa-solid fa-plus mr-1"></i> Novo
                        </button>
                    </div>
                    <div id="mindmap-sidebar-list" class="p-2 space-y-1 overflow-y-auto flex-1"></div>
                </div>

                <div class="flex-1 min-w-0 min-h-0 flex flex-col bg-white overflow-hidden">
                    <div id="mindmap-toolbar" class="hidden flex-wrap items-center gap-1.5 p-2 border-b border-notion-border bg-notion-lightBg text-xs shrink-0">
                        <input type="text" id="mindmap-title-input" oninput="updateCurrentMindMapTitle(this.value)" placeholder="Título do mapa..." class="font-bold text-sm border-none outline-none bg-transparent px-1 w-48">
                        <div class="h-4 w-px bg-gray-300 mx-1"></div>
                        <button type="button" onclick="addMindMapNode()" class="px-2.5 py-1.5 bg-white border rounded flex items-center gap-1.5"><i class="fa-solid fa-plus"></i> Tópico</button>
                        <button type="button" onclick="addMindMapNode('text')" class="px-2.5 py-1.5 bg-white border rounded flex items-center gap-1.5" title="Texto livre com fundo transparente"><i class="fa-solid fa-font"></i> Texto</button>
                        <button type="button" onclick="addMindMapTitle()" class="px-2.5 py-1.5 bg-white border rounded flex items-center gap-1.5" title="Assunto principal do mapa (tópico maior)"><i class="fa-solid fa-heading"></i> Título</button>
                        <button type="button" id="mindmap-btn-connect" onclick="toggleMindMapConnectMode()" class="px-2.5 py-1.5 bg-white border rounded flex items-center gap-1.5"><i class="fa-solid fa-diagram-project"></i> Conectar</button>
                        <button type="button" id="mindmap-btn-draw" onclick="toggleMindMapDrawMode()" class="px-2.5 py-1.5 bg-white border rounded flex items-center gap-1.5"><i class="fa-solid fa-pencil"></i> Desenhar</button>
                        <div id="mindmap-draw-color-grid" class="hidden flex items-center gap-1"></div>
                        <div class="h-4 w-px bg-gray-300 mx-1"></div>
                        <div class="flex items-center gap-1" title="Zoom (Ctrl + roda do mouse)">
                            <button type="button" onclick="mindMapZoom(-0.1)" class="w-7 h-7 bg-white border rounded"><i class="fa-solid fa-minus"></i></button>
                            <button type="button" id="mindmap-zoom-label" onclick="mindMapZoomReset()" class="px-2 h-7 bg-white border rounded font-mono w-14">100%</button>
                            <button type="button" onclick="mindMapZoom(0.1)" class="w-7 h-7 bg-white border rounded"><i class="fa-solid fa-plus"></i></button>
                        </div>
                        <button type="button" onclick="deleteCurrentMindMap()" class="px-2.5 py-1.5 bg-white border rounded text-rose-600 ml-auto"><i class="fa-solid fa-trash mr-1"></i>Excluir Mapa</button>
                    </div>

                    <div id="mindmap-node-toolbar" class="hidden flex-wrap items-center gap-2 p-2 border-b border-notion-border bg-gray-100 text-xs shrink-0">
                        <span class="font-semibold text-gray-700" id="mindmap-node-kind-label">Tópico:</span>
                        <textarea id="mindmap-node-text-input" rows="1" oninput="setMindMapNodeText(this.value)" placeholder="Escreva o texto aqui..." class="w-72 border rounded px-2 py-1 bg-white text-gray-800 resize-y focus:outline-none"></textarea>
                        <div id="mindmap-node-color-grid" class="flex items-center gap-1"></div>
                        <button type="button" id="mindmap-btn-textcolor" onclick="toggleMindMapNodeTextColor()" class="px-2 py-1 bg-white border rounded" title="Alternar texto claro/escuro"><i class="fa-solid fa-font"></i></button>
                        <button type="button" onclick="toggleMindMapNodeBold()" class="px-2 py-1 bg-white border rounded font-bold" title="Negrito">B</button>
                        <button type="button" onclick="cycleMindMapNodeFontSize()" class="px-2 py-1 bg-white border rounded" title="Tamanho da fonte"><i class="fa-solid fa-text-height"></i></button>
                        <button type="button" onclick="disconnectMindMapNode()" class="px-2 py-1 bg-white border rounded flex items-center gap-1.5" title="Remover todas as conexões deste tópico"><i class="fa-solid fa-link-slash"></i> Desconectar</button>
                        <button type="button" onclick="deleteMindMapNode(selectedMindMapNodeId)" class="px-2 py-1 bg-white border rounded text-rose-600 ml-auto" title="Excluir tópico"><i class="fa-solid fa-trash"></i></button>
                    </div>

                    <div id="mindmap-conn-toolbar" class="hidden flex-wrap items-center gap-2 p-2 border-b border-notion-border bg-gray-100 text-xs shrink-0">
                        <span class="font-semibold text-gray-700">Conexão selecionada</span>
                        <button type="button" onclick="deleteSelectedMindMapConnection()" class="px-2.5 py-1 bg-white border rounded text-rose-600 flex items-center gap-1.5"><i class="fa-solid fa-link-slash"></i> Desconectar</button>
                    </div>

                    <div id="mindmap-drawing-toolbar" class="hidden flex-wrap items-center gap-2 p-2 border-b border-notion-border bg-gray-100 text-xs shrink-0">
                        <span class="font-semibold text-gray-700">Traço selecionado</span>
                        <button type="button" onclick="deleteSelectedMindMapDrawing()" class="px-2.5 py-1 bg-white border rounded text-rose-600 flex items-center gap-1.5"><i class="fa-solid fa-trash"></i> Excluir este traço</button>
                    </div>

                    <!-- min-h-0/min-w-0: sem isso o item flex cresce ate o tamanho do
                         conteudo (2400x1400) em vez de encolher e rolar, e o
                         overflow-hidden do pai corta tudo - sem barra nenhuma. -->
                    <style>
                        #mindmap-canvas-wrapper { overflow: scroll; }
                        #mindmap-canvas-wrapper::-webkit-scrollbar { width: 14px; height: 14px; }
                        #mindmap-canvas-wrapper::-webkit-scrollbar-track { background: #f1f1f0; }
                        #mindmap-canvas-wrapper::-webkit-scrollbar-thumb { background: #b4b4b0; border-radius: 7px; border: 3px solid #f1f1f0; }
                        #mindmap-canvas-wrapper::-webkit-scrollbar-thumb:hover { background: #8e8e8a; }
                    </style>
                    <div id="mindmap-canvas-wrapper" class="flex-1 min-w-0 min-h-0 relative bg-gray-50" onwheel="mindMapWheel(event)">
                        <div id="mindmap-empty-state" class="p-10 text-center text-notion-muted text-sm">Crie um mapa mental ou selecione um na lista ao lado.</div>
                        <div id="mindmap-stage" class="relative hidden" style="width: 2400px; height: 1400px;">
                        <div id="mindmap-canvas" class="absolute top-0 left-0" style="width: 2400px; height: 1400px; transform-origin: 0 0;"
                             onmousedown="mindMapCanvasMouseDown(event)" onmousemove="mindMapCanvasMouseMove(event)"
                             onmouseup="mindMapCanvasMouseUp(event)" onmouseleave="mindMapCanvasMouseUp(event)">
                            <svg id="mindmap-svg" class="absolute inset-0" width="2400" height="1400" style="pointer-events:none;">
                                <defs>
                                    <marker id="mindmap-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                                        <path d="M0,0 L8,4 L0,8 Z" fill="#94a3b8"></path>
                                    </marker>
                                </defs>
                                <g id="mindmap-connections-layer"></g>
                                <g id="mindmap-drawings-layer"></g>
                            </svg>
                            <div id="mindmap-nodes-layer"></div>
                        </div>
                        </div>
                    </div>
                </div>
            </section>
        </main>

        <!-- PAINEL DE TAREFA: encaixado na lateral da tela de Tarefas (nao sobrepoe).
             Fecha sozinho ao trocar de tela - ver switchTab(). -->
        <aside id="task-modal" class="cg-task-panel hidden">
            <div class="cg-task-head">
                <h3 class="cg-modal-title" id="modal-task-header-title">Nova Tarefa</h3>
                <button type="button" class="cg-x" onclick="closeModal('task-modal')" title="Fechar"><i class="fa-solid fa-xmark"></i></button>
            </div>

            <form onsubmit="handleTaskSubmit(event)" class="cg-task-body">
                <input type="hidden" id="modal-task-id">

                <div>
                    <div class="cg-label cg-mb">Ícone da tarefa</div>
                    <div id="devops-icon-picker-grid" class="grid grid-cols-8 gap-1.5 p-2 rounded-xl" style="background:#f7f8fa"></div>
                    <input type="hidden" id="modal-task-selected-icon" value="fa-solid fa-book">
                </div>

                <div>
                    <div class="cg-label cg-mb">Título *</div>
                    <input type="text" id="modal-task-title" required placeholder="Título..." class="cg-input" onkeydown="if(event.key==='Enter'){event.preventDefault(); document.querySelector('#task-modal form').requestSubmit();}">
                </div>

                <div class="cg-two">
                    <div>
                        <div class="cg-label cg-mb">Matéria</div>
                        <select id="modal-task-project" class="cg-select"></select>
                    </div>
                    <div>
                        <div class="cg-label cg-mb">Prioridade</div>
                        <select id="modal-task-priority" class="cg-select">
                            <option value="Baixa">Baixa</option>
                            <option value="Média" selected>Média</option>
                            <option value="Alta">Alta</option>
                        </select>
                    </div>
                </div>

                <div class="cg-two">
                    <div>
                        <div class="cg-label cg-mb">Status</div>
                        <select id="modal-task-status" class="cg-select">
                            <option value="A Fazer">A Fazer</option>
                            <option value="Em Andamento">Em Andamento</option>
                            <option value="Concluído">Concluído</option>
                        </select>
                    </div>
                    <div>
                        <div class="cg-label cg-mb">Data limite</div>
                        <input type="date" id="modal-task-date" class="cg-input">
                    </div>
                </div>

                <div>
                    <div class="cg-label cg-mb">Etapas / Checklist</div>
                    <div id="modal-subtasks-list" class="space-y-1.5 mb-2"></div>
                    <div class="flex gap-2">
                        <input type="text" id="new-subtask-input" placeholder="Nova etapa..." class="cg-input" onkeydown="if(event.key==='Enter'){event.preventDefault(); addModalSubtask();}">
                        <button type="button" onclick="addModalSubtask()" class="cg-btn cg-btn-ghost">Adicionar</button>
                    </div>
                </div>

                <div>
                    <div class="cg-label cg-mb">Links / Videoaulas</div>
                    <div id="modal-links-list" class="space-y-1.5 mb-2"></div>
                    <div class="flex gap-2">
                        <input type="url" id="new-link-input" placeholder="https://..." class="cg-input" onkeydown="if(event.key==='Enter'){event.preventDefault(); addModalLink();}">
                        <button type="button" onclick="addModalLink()" class="cg-btn cg-btn-ghost">+ Link</button>
                    </div>
                </div>

                <div>
                    <div class="cg-label cg-mb">Anexos do computador</div>
                    <div id="modal-attachments-list" class="space-y-1.5 mb-2"></div>
                    <button type="button" onclick="pickModalAttachment()" class="cg-btn cg-btn-ghost" style="width:100%"><i class="fa-solid fa-folder-open"></i> Procurar arquivo</button>
                </div>

                <div class="cg-actions cg-task-foot">
                    <button type="button" onclick="closeModal('task-modal')" class="cg-btn cg-btn-ghost">Cancelar</button>
                    <button type="submit" class="cg-btn cg-btn-primary accent-btn-bg">Salvar tarefa</button>
                </div>
            </form>
        </aside>

        <!-- PAINEL SPOTIFY (player proprio) - encaixado na lateral direita, segue o tema de cor escolhido -->
        <aside id="sp-panel" class="sp-panel hidden">
            <div id="sp-resize" class="sp-resize" title="Arraste para redimensionar" onpointerdown="spResizeIniciar(event)"></div>
            <div class="sp-head">
                <span class="sp-title"><i class="fa-brands fa-spotify"></i> Spotify</span>
                <span id="sp-sub" class="sp-sub"></span>
                <span class="sp-acts" style="margin-left:auto">
                    <button type="button" id="sp-btn-sair" class="hidden" onclick="spDesconectar()" title="Desconectar a conta"><i class="fa-solid fa-right-from-bracket"></i></button>
                    <button type="button" onclick="spotifyToggle()" title="Fechar painel (a música continua tocando)"><i class="fa-solid fa-xmark"></i></button>
                </span>
            </div>
            <div id="sp-body" class="sp-body"><div class="sp-card">Carregando...</div></div>
            <div id="sp-now" class="sp-now hidden">
                <div class="sp-np">
                    <img id="sp-np-img" class="sp-cover" alt="" referrerpolicy="no-referrer">
                    <div class="sp-r-t">
                        <div id="sp-np-title" class="sp-r-n">Nada tocando</div>
                        <div id="sp-np-art" class="sp-r-s"></div>
                    </div>
                    <div class="sp-ctrl">
                        <button type="button" id="sp-shuf" onclick="spShuffle()" title="Modo aleatório"><i class="fa-solid fa-shuffle"></i></button>
                        <button type="button" onclick="spCmd('prev')" title="Anterior"><i class="fa-solid fa-backward-step"></i></button>
                        <button type="button" id="sp-pp" class="sp-pp" onclick="spCmd('toggle')" title="Tocar/Pausar"><i class="fa-solid fa-play"></i></button>
                        <button type="button" onclick="spCmd('next')" title="Próxima"><i class="fa-solid fa-forward-step"></i></button>
                    </div>
                </div>
                <div class="sp-seek">
                    <span id="sp-t0">0:00</span>
                    <input type="range" id="sp-seek" min="0" max="1000" value="0" oninput="spSeekInput(this.value)" onchange="spSeekFim(this.value)">
                    <span id="sp-t1">0:00</span>
                </div>
                <div class="sp-vol"><i class="fa-solid fa-volume-low"></i>
                    <input type="range" id="sp-vol" min="0" max="100" value="60" oninput="spCmd('volume', this.value / 100)">
                </div>
            </div>
        </aside>

        <!-- BARRA DE APLICATIVOS (lado direito) -->
        <div id="cg-apprail" class="cg-apprail">
            <button type="button" id="sp-rail-btn" onclick="spotifyToggle()" data-tip="Spotify"><i class="fa-brands fa-spotify"></i></button>
        </div>
    </div>

    <iframe id="sp-frame" class="sp-frame" allow="autoplay; encrypted-media" tabindex="-1" aria-hidden="true" title="Player do Spotify"></iframe>

    <!-- MODAL EVENTO DE CALENDARIO -->
    <div id="calendar-event-modal" class="fixed inset-0 bg-black/40 backdrop-blur-sm hidden flex items-center justify-center z-50 p-4">
        <div class="bg-white rounded-lg border shadow-xl w-full max-w-sm p-5 space-y-3 text-xs my-auto">
            <div class="flex justify-between items-center border-b pb-2">
                <h3 class="font-bold text-gray-800" id="calendar-event-modal-title">Novo Evento</h3>
                <button onclick="closeModal('calendar-event-modal')"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <input type="hidden" id="calendar-event-id">
            <div>
                <label class="block font-medium text-gray-700 mb-1">Título *</label>
                <input type="text" id="calendar-event-title" onkeydown="if(event.key==='Enter'){event.preventDefault(); saveCalendarEventSubmit();}" class="w-full border rounded px-3 py-2 text-gray-800 focus:outline-none">
            </div>
            <div class="grid grid-cols-2 gap-3">
                <div>
                    <label class="block font-medium text-gray-700 mb-1">Data *</label>
                    <input type="date" id="calendar-event-date" class="w-full border rounded px-2.5 py-1.5 text-gray-800 bg-white">
                </div>
                <div>
                    <label class="block font-medium text-gray-700 mb-1">Hora</label>
                    <input type="time" id="calendar-event-time" class="w-full border rounded px-2.5 py-1.5 text-gray-800 bg-white">
                </div>
            </div>
            <div>
                <label class="block font-medium text-gray-700 mb-1">Cor</label>
                <div id="calendar-event-color-grid" class="flex flex-wrap gap-2"></div>
            </div>
            <div>
                <label class="block font-medium text-gray-700 mb-1">Notas</label>
                <textarea id="calendar-event-notes" rows="2" class="w-full border rounded px-2.5 py-1.5 text-gray-800"></textarea>
            </div>
            <div class="flex justify-between items-center pt-2 border-t">
                <button type="button" id="calendar-event-delete-btn" onclick="deleteCalendarEventFromModal()" class="hidden text-rose-600 hover:bg-rose-50 px-3 py-1.5 rounded"><i class="fa-solid fa-trash mr-1"></i>Excluir</button>
                <div class="flex gap-2 ml-auto">
                    <button onclick="closeModal('calendar-event-modal')" class="px-3 py-1.5 border rounded">Cancelar</button>
                    <button onclick="saveCalendarEventSubmit()" class="px-4 py-1.5 accent-btn-bg text-white rounded font-medium">Salvar</button>
                </div>
            </div>
        </div>
    </div>

    <!-- MODALS SECTION -->
    <!-- MODAL DECK EDITOR -->
    <div id="deck-editor-modal" class="fixed inset-0 bg-black/40 backdrop-blur-sm hidden flex items-center justify-center z-50 p-4">
        <div class="bg-white rounded-xl border shadow-2xl w-full max-w-lg p-5 space-y-4 text-xs my-auto">
            <div class="flex justify-between items-center border-b pb-2">
                <h3 class="font-bold text-gray-800 text-sm" id="deck-editor-modal-title">Editar Baralho</h3>
                <button type="button" onclick="closeModal('deck-editor-modal')"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <input type="hidden" id="deck-editor-id">
            <div class="grid grid-cols-2 gap-3">
                <div>
                    <label class="block font-medium text-gray-700 mb-1">Título do Baralho *</label>
                    <input type="text" id="deck-editor-title" class="w-full border rounded px-2.5 py-1.5 text-gray-800" onkeydown="if(event.key==='Enter'){event.preventDefault(); saveDeckEditorSubmit();}">
                </div>
                <div>
                    <label class="block font-medium text-gray-700 mb-1">Matéria</label>
                    <select id="deck-editor-subject" class="w-full border rounded px-2.5 py-1.5 text-gray-800 bg-white"></select>
                </div>
            </div>

            <div>
                <label class="block font-medium text-gray-700 mb-1">Cartas do Baralho</label>
                <div id="deck-editor-cards-list" class="space-y-2 max-h-48 overflow-y-auto pr-1"></div>
                <button type="button" onclick="addDeckEditorCard()" class="mt-2 text-xs accent-btn-bg text-white font-bold px-3 py-1.5 rounded w-full">
                    + Adicionar Nova Carta
                </button>
            </div>

            <div class="flex justify-end space-x-2 pt-3 border-t">
                <button type="button" onclick="closeModal('deck-editor-modal')" class="px-3 py-1.5 border rounded">Cancelar</button>
                <button type="button" onclick="saveDeckEditorSubmit()" class="px-4 py-1.5 accent-btn-bg text-white rounded font-bold">Salvar Baralho</button>
            </div>
        </div>
    </div>

    <!-- MODAL TIMER -->
    <div id="focus-timer-modal" class="fixed inset-0 bg-black/40 backdrop-blur-sm hidden flex items-center justify-center z-50 p-4">
        <div class="cg-modal">
            <div class="cg-modal-head">
                <h3 class="cg-modal-title">Cronômetro de Foco</h3>
                <button type="button" class="cg-x" onclick="closeModal('focus-timer-modal')" title="Fechar"><i class="fa-solid fa-xmark"></i></button>
            </div>

            <div>
                <div class="cg-label cg-mb">Duração da sessão</div>
                <div class="cg-chips">
                    <button type="button" data-m="10" onclick="setTimerPreset(10)" class="cg-chip">10m</button>
                    <button type="button" data-m="25" onclick="setTimerPreset(25)" class="cg-chip on">25m</button>
                    <button type="button" data-m="40" onclick="setTimerPreset(40)" class="cg-chip">40m</button>
                    <button type="button" data-m="60" onclick="setTimerPreset(60)" class="cg-chip">60m</button>
                    <button type="button" data-m="120" onclick="setTimerPreset(120)" class="cg-chip">120m</button>
                    <input type="number" id="timer-custom-mins" min="1" placeholder="Outro" onchange="applyCustomTimerMins(this.value)" class="cg-input cg-chip-input" onkeydown="if(event.key==='Enter'){event.preventDefault(); applyCustomTimerMins(this.value);}">
                </div>
            </div>

            <div>
                <div class="cg-label cg-mb">Matéria desta sessão</div>
                <select id="timer-subject-select" class="cg-select"></select>
            </div>

            <div class="cg-clock">
                <span id="timer-display" class="accent-text-color">25:00</span>
                <p id="timer-status-label">Pronto para iniciar</p>
            </div>

            <div class="cg-actions">
                <button type="button" onclick="toggleTimer()" class="cg-btn cg-btn-primary accent-btn-bg">
                    <i class="fa-solid fa-play cg-keep" id="timer-icon-play"></i>
                    <span id="timer-btn-text">Iniciar Sessão</span>
                </button>
                <button type="button" onclick="concluirSessaoFoco()" class="cg-btn cg-btn-ghost" title="Registrar o tempo focado até agora"><i class="fa-solid fa-check"></i> Concluir</button>
                <button type="button" onclick="triggerDesktopPipWindow()" class="cg-btn cg-btn-ghost" title="Abrir em janela flutuante"><i class="fa-solid fa-window-restore"></i> PiP</button>
                <button type="button" onclick="resetTimer()" class="cg-btn cg-btn-ghost cg-btn-icon" title="Reiniciar"><i class="fa-solid fa-rotate-left"></i></button>
            </div>

            <div class="cg-modal-foot">
                <button type="button" onclick="openManualTimeModal()" class="cg-link accent-text-color">+ Registrar horas manualmente</button>
            </div>
        </div>
    </div>

    <!-- MODAL MANUAL TIME REGISTRATION -->
    <div id="manual-time-modal" class="fixed inset-0 bg-black/40 backdrop-blur-sm hidden flex items-center justify-center z-50 p-4">
        <div class="cg-modal">
            <div class="cg-modal-head">
                <h3 class="cg-modal-title">Registrar horas focadas</h3>
                <button type="button" class="cg-x" onclick="closeModal('manual-time-modal')" title="Fechar"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div>
                <div class="cg-label cg-mb">Matéria</div>
                <select id="manual-subject-select" class="cg-select"></select>
                <p class="cg-hint">Escolha "Sem matéria" para registrar só no dia.</p>
            </div>
            <div class="cg-two">
                <div>
                    <div class="cg-label cg-mb">Tempo (minutos)</div>
                    <input type="number" id="manual-minutes-input" min="1" placeholder="Ex: 45" class="cg-input" onkeydown="if(event.key==='Enter'){event.preventDefault(); saveManualTimeLog();}">
                </div>
                <div>
                    <div class="cg-label cg-mb">Data</div>
                    <input type="date" id="manual-date-input" class="cg-input">
                </div>
            </div>
            <div class="cg-actions" style="justify-content:flex-end">
                <button type="button" onclick="closeModal('manual-time-modal')" class="cg-btn cg-btn-ghost">Cancelar</button>
                <button type="button" onclick="saveManualTimeLog()" class="cg-btn cg-btn-primary accent-btn-bg" style="flex:0 0 auto">Salvar registro</button>
            </div>
        </div>
    </div>

    <!-- MODAL SUBJECT -->
    <div id="subject-modal" class="fixed inset-0 bg-black/40 backdrop-blur-sm hidden flex items-center justify-center z-50 p-4">
        <div class="bg-white rounded-lg border shadow-xl w-full max-w-sm p-5 space-y-4 text-xs my-auto">
            <h3 class="font-bold text-gray-800" id="subject-modal-title">Nova Matéria</h3>
            <input type="hidden" id="edit-subject-old-name">
            <div>
                <label class="block font-medium text-gray-700 mb-1">Nome da Matéria *</label>
                <input type="text" id="subject-name-input" onkeydown="if(event.key==='Enter'){event.preventDefault(); saveSubjectSubmit();}" placeholder="Ex: Engenharia..." class="w-full border rounded px-3 py-2 text-gray-800 focus:outline-none">
            </div>
            <div>
                <label class="block font-medium text-gray-700 mb-1">Cor</label>
                <div id="subject-color-picker-grid" class="flex flex-wrap gap-2"></div>
            </div>
            <div>
                <label class="block font-medium text-gray-700 mb-1">Ícone</label>
                <div id="subject-icon-picker-grid" class="grid grid-cols-8 gap-1.5 p-2 bg-notion-lightBg border border-notion-border rounded-lg"></div>
            </div>
            <div class="flex justify-end space-x-2 pt-2">
                <button onclick="closeModal('subject-modal')" class="px-3 py-1.5 border rounded">Cancelar</button>
                <button onclick="saveSubjectSubmit()" class="px-4 py-1.5 accent-btn-bg text-white rounded font-medium">Salvar</button>
            </div>
        </div>
    </div>

    <!-- MODAL PROFILE WITH THEME SELECTOR -->
    <div id="profile-modal" class="fixed inset-0 bg-black/40 backdrop-blur-sm hidden flex items-center justify-center z-50 p-4">
        <div class="bg-white rounded-xl border shadow-2xl w-full max-w-md p-5 space-y-4 text-xs my-auto">
            <div class="flex justify-between items-center border-b pb-2">
                <h3 class="font-bold text-gray-800">Personalizar Perfil & Tema</h3>
                <button onclick="closeModal('profile-modal')"><i class="fa-solid fa-xmark"></i></button>
            </div>

            <div class="space-y-3">
                <div class="flex items-center space-x-3">
                    <div id="profile-preview-box"></div>
                    <label class="bg-gray-100 hover:bg-gray-200 border px-3 py-1.5 rounded cursor-pointer font-bold">
                        Carregar Foto
                        <input type="file" id="profile-photo-file" accept="image/*" class="hidden" onchange="handleProfilePhotoUpload(event)">
                    </label>
                    <button type="button" id="btn-remover-foto" onclick="removerFotoPerfil()"
                            class="hidden bg-rose-50 hover:bg-rose-100 border border-rose-200 text-rose-700 px-3 py-1.5 rounded cursor-pointer font-bold">
                        <i class="fa-regular fa-trash-can mr-1"></i> Remover
                    </button>
                </div>
                <p id="foto-info" class="text-[11px] text-gray-400"></p>

                <div>
                    <label class="block font-medium mb-1">Nome / Apelido</label>
                    <input type="text" id="profile-name-input" class="w-full border rounded px-3 py-2" onkeydown="if(event.key==='Enter'){event.preventDefault(); saveProfileCustomization();}">
                </div>
                <div>
                    <label class="block font-medium mb-1">Curso / Foco</label>
                    <input type="text" id="profile-role-input" class="w-full border rounded px-3 py-2" onkeydown="if(event.key==='Enter'){event.preventDefault(); saveProfileCustomization();}">
                </div>
                <div>
                    <label class="block font-medium mb-1">Tema de Cores do Aplicativo (Ícones & Botões)</label>
                    <select id="accent-theme-select" onchange="changeAccentTheme(this.value)" class="w-full border rounded px-3 py-2 bg-white font-bold cursor-pointer">
                        <option value="indigo">Índigo (Padrão)</option>
                        <option value="rose">Rosa & Vermelho</option>
                        <option value="cyan">Azul & Ciano</option>
                        <option value="emerald">Esmeralda & Verde</option>
                        <option value="amber">Âmbar & Laranja</option>
                    </select>
                </div>
            </div>

            <div id="profileAccountBox" class="hidden pt-3 border-t space-y-2">
                <div class="flex items-center gap-2 text-[11px] text-gray-500">
                    <i class="fa-solid fa-circle-check text-emerald-500"></i>
                    <span class="truncate">Conectado como <span id="profileAccountEmail" class="text-gray-700 font-medium"></span></span>
                </div>
                <button onclick="sairDaContaCogni()" class="w-full px-3 py-1.5 rounded text-xs bg-rose-50 border border-rose-200 text-rose-700 hover:bg-rose-100 cursor-pointer transition">
                    <i class="fa-solid fa-right-from-bracket mr-1"></i> Sair desta conta
                </button>
            </div>

            <div class="flex justify-end space-x-2 pt-2 border-t">
                <button onclick="closeModal('profile-modal')" class="px-3 py-1.5 border rounded">Cancelar</button>
                <button onclick="saveProfileCustomization()" class="px-4 py-1.5 accent-btn-bg text-white rounded font-bold">Salvar Perfil</button>
            </div>
        </div>
    </div>

    <!-- FLASHCARD STUDY MODAL -->
    <div id="flashcard-study-modal" class="fixed inset-0 bg-black/40 backdrop-blur-md hidden flex items-center justify-center z-50 p-4">
        <div class="bg-white rounded-2xl shadow-2xl w-full max-w-xl overflow-hidden flex flex-col my-auto">
            <!-- Cabecalho: nome + materia alocada | numero da carta -->
            <div class="px-5 py-4 flex items-center justify-between gap-3 border-b border-notion-border">
                <div class="min-w-0">
                    <h3 class="text-base font-black tracking-tight text-gray-900 truncate" id="study-deck-title">Estudando Baralho</h3>
                    <p class="cg-label mt-0.5 truncate" id="study-card-subject">Matéria</p>
                </div>
                <div class="flex items-center gap-3 shrink-0">
                    <span id="study-card-counter" class="text-xs font-semibold text-gray-600 bg-gray-100 px-2.5 py-1 rounded-lg">1 / 1</span>
                    <button onclick="closeModal('flashcard-study-modal')" class="text-gray-400 hover:text-gray-700" title="Fechar"><i class="fa-solid fa-xmark"></i></button>
                </div>
            </div>

            <div class="p-5 space-y-4 flex flex-col">
                <div class="w-full h-64 perspective-1000 cursor-pointer" onclick="flipStudyCard()">
                    <div id="study-card-inner" class="flashcard-inner">
                        <div class="flashcard-front bg-white border border-notion-border p-6 flex flex-col">
                            <span class="cg-label">Pergunta</span>
                            <div class="my-auto text-center">
                                <p class="text-xl font-bold text-gray-900 leading-snug break-words" id="study-card-front-text"></p>
                            </div>
                            <span class="text-[11px] text-gray-400 text-center">Clique na carta para ver a resposta</span>
                        </div>

                        <div class="flashcard-back accent-light-bg border accent-border-color p-6 flex flex-col">
                            <span class="cg-label">Resposta</span>
                            <div class="my-auto text-center">
                                <p class="text-lg font-semibold text-gray-900 leading-snug break-words" id="study-card-back-text"></p>
                            </div>
                            <span class="text-[11px] text-gray-400 text-center">Avalie seu desempenho abaixo</span>
                        </div>
                    </div>
                </div>

                <p id="study-card-hint" class="hidden text-sm text-gray-600 bg-gray-50 border border-notion-border rounded-xl px-4 py-3 break-words"></p>

                <div class="flex items-center">
                    <button id="study-hint-btn" type="button" onclick="toggleStudyCardHint()" class="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-notion-border text-xs font-semibold text-gray-600 hover:bg-gray-50 transition">
                        <i class="fa-regular fa-lightbulb text-gray-400"></i> Dica
                    </button>
                </div>

                <div class="w-full grid grid-cols-3 gap-3 pt-4 border-t border-notion-border">
                    <button onclick="rateStudyCard('again')" class="py-2.5 rounded-lg border border-notion-border bg-white hover:bg-gray-50 text-gray-700 text-sm font-semibold transition flex items-center justify-center gap-2"><i class="fa-solid fa-xmark text-gray-400"></i> Errei</button>
                    <button onclick="rateStudyCard('hard')" class="py-2.5 rounded-lg border border-notion-border bg-white hover:bg-gray-50 text-gray-700 text-sm font-semibold transition flex items-center justify-center gap-2"><i class="fa-solid fa-minus text-gray-400"></i> Difícil</button>
                    <button onclick="rateStudyCard('easy')" class="py-2.5 rounded-lg accent-btn-bg hover:opacity-90 text-white text-sm font-semibold transition flex items-center justify-center gap-2"><i class="fa-solid fa-check"></i> Fácil</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ACCENT STYLES DYNAMIC INJECTION -->
    <style id="accent-theme-styles">
        .accent-text-color { color: #4f46e5; }
        .accent-bg-badge { background-color: #4f46e5; }
        .accent-btn-bg { background-color: #4f46e5; }
        .accent-icon-color { color: #4f46e5; }
        .accent-border-color { border-color: #c7d2fe; }
        .accent-light-bg { background-color: #eef2ff; }
    </style>

    <script>
        const DEVOPS_ICONS = [
            { id: 'fa-solid fa-book', label: 'Livro' },
            { id: 'fa-solid fa-graduation-cap', label: 'Graduação' },
            { id: 'fa-solid fa-pen-nib', label: 'Redação' },
            { id: 'fa-solid fa-calculator', label: 'Cálculo' },
            { id: 'fa-solid fa-flask', label: 'Laboratório' },
            { id: 'fa-solid fa-microscope', label: 'Pesquisa' },
            { id: 'fa-solid fa-compass', label: 'Geografia' },
            { id: 'fa-solid fa-brain', label: 'Conhecimento' },
            { id: 'fa-solid fa-briefcase', label: 'Gestão' },
            { id: 'fa-solid fa-chart-line', label: 'Finanças' },
            { id: 'fa-solid fa-sitemap', label: 'Organograma' },
            { id: 'fa-solid fa-handshake', label: 'Negócios' },
            { id: 'fa-solid fa-coins', label: 'Economia' },
            { id: 'fa-solid fa-bullseye', label: 'Metas / KPIs' },
            { id: 'fa-solid fa-users-gear', label: 'RH' },
            { id: 'fa-solid fa-boxes-stacked', label: 'Logística' }
        ];

        let appData = {
            userProfile: { name: '', role: '', avatarPhoto: '' },
            accentTheme: 'indigo',
            subjects: [],
            subjectMeta: {},
            selectedSubjectIdsForDelete: [],
            tasks: [],
            selectedTaskIds: [],
            flashcardDecks: [],
            selectedDeckIds: [],
            notes: [],
            selectedNoteIds: [],
            // Abas de cadernos abertas ao mesmo tempo (estilo Notion/Google
            // Docs). Guardadas no appData para sobreviver a fechar e reabrir
            // o app - ver renderNoteTabs()/openNoteInTab().
            openNoteTabs: [],
            activeNoteTabId: null,
            postits: [],
            studyLogs: [],
            calendarEvents: [],
            mindMaps: []
        };

        const THEME_PALETTES = {
            indigo: { 
                text: '#4f46e5', bg: '#4f46e5', border: '#c7d2fe', light: '#eef2ff',
                chartPalette: ['#4f46e5', '#06b6d4', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#64748b'],
                heatmap: {
                    l1: 'bg-indigo-100 border-indigo-200 text-indigo-700 font-semibold',
                    l2: 'bg-indigo-300 border-indigo-400 text-indigo-950 font-bold',
                    l3: 'bg-indigo-500 border-indigo-600 text-white font-extrabold',
                    l4: 'bg-indigo-700 border-indigo-800 text-white font-extrabold',
                    legend: ['bg-gray-100 border-gray-200', 'bg-indigo-100 border-indigo-200', 'bg-indigo-300 border-indigo-400', 'bg-indigo-500 border-indigo-600', 'bg-indigo-700 border-indigo-800']
                }
            },
            rose: { 
                text: '#e11d48', bg: '#e11d48', border: '#fecdd3', light: '#fff1f2',
                chartPalette: ['#e11d48', '#f59e0b', '#10b981', '#06b6d4', '#8b5cf6', '#3b82f6', '#64748b'],
                heatmap: {
                    l1: 'bg-rose-100 border-rose-200 text-rose-700 font-semibold',
                    l2: 'bg-rose-300 border-rose-400 text-rose-950 font-bold',
                    l3: 'bg-rose-500 border-rose-600 text-white font-extrabold',
                    l4: 'bg-rose-700 border-rose-800 text-white font-extrabold',
                    legend: ['bg-gray-100 border-gray-200', 'bg-rose-100 border-rose-200', 'bg-rose-300 border-rose-400', 'bg-rose-500 border-rose-600', 'bg-rose-700 border-rose-800']
                }
            },
            cyan: { 
                text: '#0284c7', bg: '#0284c7', border: '#bae6fd', light: '#f0f9ff',
                chartPalette: ['#0284c7', '#10b981', '#6366f1', '#f59e0b', '#ec4899', '#8b5cf6', '#64748b'],
                heatmap: {
                    l1: 'bg-sky-100 border-sky-200 text-sky-700 font-semibold',
                    l2: 'bg-sky-300 border-sky-400 text-sky-950 font-bold',
                    l3: 'bg-sky-500 border-sky-600 text-white font-extrabold',
                    l4: 'bg-sky-700 border-sky-800 text-white font-extrabold',
                    legend: ['bg-gray-100 border-gray-200', 'bg-sky-100 border-sky-200', 'bg-sky-300 border-sky-400', 'bg-sky-500 border-sky-600', 'bg-sky-700 border-sky-800']
                }
            },
            emerald: { 
                text: '#059669', bg: '#059669', border: '#a7f3d0', light: '#ecfdf5',
                chartPalette: ['#059669', '#0284c7', '#f59e0b', '#8b5cf6', '#ec4899', '#6366f1', '#64748b'],
                heatmap: {
                    l1: 'bg-emerald-100 border-emerald-200 text-emerald-700 font-semibold',
                    l2: 'bg-emerald-300 border-emerald-400 text-emerald-950 font-bold',
                    l3: 'bg-emerald-500 border-emerald-600 text-white font-extrabold',
                    l4: 'bg-emerald-700 border-emerald-800 text-white font-extrabold',
                    legend: ['bg-gray-100 border-gray-200', 'bg-emerald-100 border-emerald-200', 'bg-emerald-300 border-emerald-400', 'bg-emerald-500 border-emerald-600', 'bg-emerald-700 border-emerald-800']
                }
            },
            amber: { 
                text: '#d97706', bg: '#d97706', border: '#fde68a', light: '#fffbeb',
                chartPalette: ['#d97706', '#059669', '#0284c7', '#e11d48', '#8b5cf6', '#6366f1', '#64748b'],
                heatmap: {
                    l1: 'bg-amber-100 border-amber-200 text-amber-700 font-semibold',
                    l2: 'bg-amber-300 border-amber-400 text-amber-950 font-bold',
                    l3: 'bg-amber-500 border-amber-600 text-white font-extrabold',
                    l4: 'bg-amber-700 border-amber-800 text-white font-extrabold',
                    legend: ['bg-gray-100 border-gray-200', 'bg-amber-100 border-amber-200', 'bg-amber-300 border-amber-400', 'bg-amber-500 border-amber-600', 'bg-amber-700 border-amber-800']
                }
            }
        };

        function changeAccentTheme(themeKey) {
            appData.accentTheme = themeKey;
            applyAccentTheme(themeKey);
            renderDashboardStats();
            updateCharts();
            triggerAutoSave();
        }

        function applyAccentTheme(themeKey) {
            const pal = THEME_PALETTES[themeKey] || THEME_PALETTES.indigo;
            const styleEl = document.getElementById('accent-theme-styles');
            if (styleEl) {
                styleEl.innerHTML = `
                    .accent-text-color { color: ${pal.text} !important; }
                    .accent-bg-badge { background-color: ${pal.bg} !important; }
                    .accent-btn-bg { background-color: ${pal.bg} !important; }
                    .accent-icon-color { color: ${pal.text} !important; }
                    .accent-border-color { border-color: ${pal.border} !important; }
                    .accent-light-bg { background-color: ${pal.light} !important; }
                `;
            }
            document.documentElement.style.setProperty('--accent-bg', pal.bg);
            document.documentElement.style.setProperty('--accent-light', pal.light);
            document.documentElement.style.setProperty('--accent-border', pal.border);
            const sel = document.getElementById('accent-theme-select');
            if (sel) sel.value = themeKey;
        }

        let isAppDataLoadedFromDisk = false;
        let isInitializingFromDisk = false;
        let activeSubjectFilter = 'ALL';
        let currentActiveNoteId = null;
        let activeTasksViewMode = 'table';
        
        let modalSubtasksTemp = [];
        let modalLinksTemp = [];
        let modalAttachmentsTemp = [];
        let deckEditorCardsTemp = [];
        let selectedDevopsIcon = 'fa-solid fa-book';

        let timerSeconds = 25 * 60;
        let timerDurationConfigured = 25 * 60;
        let isTimerRunning = false;
        let timerInterval = null;

        let hoursChartInstance = null;
        let subjectsChartInstance = null;
        let tempProfilePhotoBase64 = '';
        let lastCloudJsonHash = '';

        function escapeJsString(str) {
            return String(str || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;');
        }
        function escapeHtml(str) {
            return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
        }

        /* ================================================================
           SANITIZACAO DO EDITOR DE NOTAS
           ================================================================
           O editor e um contenteditable: por natureza ele guarda HTML, e esse
           HTML volta para a tela via innerHTML. Isso e necessario para
           negrito, listas e links funcionarem.

           O problema: colar texto formatado de uma pagina web traz o HTML
           daquela pagina junto - inclusive <script>, onerror=, javascript: e
           <iframe>. Esse HTML fica GRAVADO na nota, sincroniza para a nuvem e
           roda de novo toda vez que a nota e aberta, em qualquer maquina.

           Num app pywebview isso e pior do que num site: o JavaScript da
           pagina tem acesso a window.pywebview.api, ou seja, a ponte para o
           Python. De "colei um texto" ate "abriram um arquivo na minha
           maquina" e um passo curto.

           A abordagem e lista de permissao: so passam as tags e atributos que
           o editor realmente usa. O resto e removido, mas o TEXTO e
           preservado - voce nao perde conteudo, so a formatacao exotica.
        */
        const TAGS_PERMITIDAS = new Set([
            'B','STRONG','I','EM','U','S','STRIKE','DEL','INS','MARK','SMALL',
            'SUB','SUP','BR','P','DIV','SPAN','H1','H2','H3','H4','H5','H6',
            'UL','OL','LI','BLOCKQUOTE','PRE','CODE','HR','A',
            'TABLE','THEAD','TBODY','TFOOT','TR','TD','TH','CAPTION','FONT'
        ]);
        const ATRIBUTOS_PERMITIDOS = new Set([
            'href','title','colspan','rowspan','align','color','face','start','type',
            // 'style' entra, mas nunca passa cru: o conteudo e filtrado por
            // sanitizarEstilo() abaixo, propriedade por propriedade. Sem
            // isso, Marca-Texto e cor de fonte (execCommand hiliteColor /
            // foreColor, que so existem via <span style="...">) eram
            // apagados toda vez que a nota era salva ou reaberta.
            'style'
        ]);

        // Unicas propriedades CSS que o editor realmente produz. Qualquer
        // outra (background-image, position, etc.) e descartada mesmo que
        // o valor pareça inofensivo.
        const CSS_PERMITIDO = new Set([
            'background-color', 'color',
            'font-weight', 'font-style', 'font-size', 'font-family',
            'text-decoration', 'text-decoration-line', 'text-align',
            'background'
        ]);

        // Formatos de valor aceitos: cores rgb()/rgba()/hex, palavras-chave
        // (bold, underline...), medidas (px/pt/em/rem/%) e nomes de fonte.
        const CSS_VALOR_OK = new RegExp(
            '^(' +
              'rgba?\\([0-9,.%\\s]+\\)' +
              '|#[0-9a-fA-F]{3,8}' +
              '|[a-zA-Z]+' +
              '|[0-9.]+(px|pt|em|rem|%)' +
              '|[a-zA-Z0-9 ,\'"\\-]+' +
            ')$'
        );

        function sanitizarEstilo(valor) {
            /* Bloqueio por substring ANTES de qualquer parsing: url(),
               expression() (IE legado, mas builds antigos do Chromium
               embarcado ainda aceitam em alguns contextos) e @import trazem
               o mesmo risco de HTML cru. Se aparecer qualquer um desses, o
               style inteiro e descartado, mesmo que o resto fosse valido. */
            const bruto = String(valor || '');
            const proibido = ['url(', 'expression(', 'javascript:', '@import',
                              '\\', '/*', '*/', '<'];
            const minusculo = bruto.toLowerCase();
            for (const p of proibido) {
                if (minusculo.indexOf(p) !== -1) return '';
            }

            // Reconstrucao propriedade-por-propriedade: o valor final NUNCA
            // e o que veio de fora, so o que sobreviveu ao allowlist.
            const saida = [];
            for (const decl of bruto.split(';')) {
                const i = decl.indexOf(':');
                if (i < 0) continue;
                const prop = decl.slice(0, i).trim().toLowerCase();
                const val  = decl.slice(i + 1).trim();
                if (!prop || !val) continue;
                if (!CSS_PERMITIDO.has(prop)) continue;
                if (val.length > 120) continue;
                if (!CSS_VALOR_OK.test(val)) continue;
                saida.push(prop + ': ' + val);
            }
            return saida.join('; ');
        }

        function _urlSegura(v) {
            /* Remove espacos e caracteres de controle antes de testar: o
               truque classico e "java<TAB>script:alert(1)", que passa numa
               checagem ingenua. O intervalo esta escrito com escapes
               explicitos porque caracteres de controle literais sao
               invisiveis no codigo. */
            var limpo = '';
            var _s = String(v || '');
            for (var _i = 0; _i < _s.length; _i++) {
                // Descarta tudo com codigo <= 32: caracteres de controle
                // (tab, nova linha, nulo) e o espaco. Sem intervalo de
                // classe, sem caractere invisivel no codigo-fonte.
                if (_s.charCodeAt(_i) > 32) { limpo += _s.charAt(_i); }
            }
            return /^(https?:|mailto:|#|\/|\.)/i.test(limpo);
        }

        function sanitizarHtml(sujo) {
            /* <template> monta a arvore SEM executar nada: imagens nao sao
               baixadas, scripts nao rodam, onerror nao dispara. */
            const tpl = document.createElement('template');
            tpl.innerHTML = String(sujo == null ? '' : sujo);

            const remover = [];
            const caminhar = document.createTreeWalker(
                tpl.content, NodeFilter.SHOW_ELEMENT, null, false);

            let no;
            while ((no = caminhar.nextNode())) {
                const tag = no.tagName.toUpperCase();
                if (!TAGS_PERMITIDAS.has(tag)) { remover.push(no); continue; }

                /* Copia a lista antes de mexer: remover atributo durante a
                   iteracao faz o indice pular elementos. */
                for (const attr of Array.from(no.attributes)) {
                    const nome = attr.name.toLowerCase();

                    /* on* cobre onclick, onerror, onload, onmouseover e todo
                       o resto de uma vez. E o vetor mais comum de XSS
                       colado. */
                    if (nome.startsWith('on') || !ATRIBUTOS_PERMITIDOS.has(nome)) {
                        no.removeAttribute(attr.name);
                        continue;
                    }
                    if (nome === 'href' && !_urlSegura(attr.value)) {
                        no.removeAttribute(attr.name);
                    }

                    // style passa pela reconstrucao de sanitizarEstilo(): so
                    // as propriedades da CSS_PERMITIDO sobrevivem. E o que
                    // mantem Marca-Texto/cor funcionando sem abrir a porta
                    // para url()/expression().
                    if (nome === 'style') {
                        const limpo = sanitizarEstilo(attr.value);
                        if (limpo) {
                            no.setAttribute('style', limpo);
                        } else {
                            no.removeAttribute('style');
                        }
                    }
                }

                if (tag === 'A' && no.getAttribute('href')) {
                    no.setAttribute('rel', 'noopener noreferrer');
                }
            }

            /* Remove a tag mas PRESERVA o texto de dentro. Excecao: script e
               style, onde o conteudo E o codigo. */
            for (const el of remover) {
                const pai = el.parentNode;
                if (!pai) continue;
                if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') {
                    pai.removeChild(el);
                } else {
                    while (el.firstChild) pai.insertBefore(el.firstChild, el);
                    pai.removeChild(el);
                }
            }
            return tpl.innerHTML;
        }

        /* Intercepta o colar e o arrastar: limpa ANTES de entrar no
           documento, para que o HTML sujo nunca chegue a ser gravado. */
        function instalarSanitizacaoDoEditor() {
            const ed = document.getElementById('note-editor-content');
            if (!ed || ed.dataset.sanitizado === '1') return;
            ed.dataset.sanitizado = '1';

            ed.addEventListener('paste', function (e) {
                e.preventDefault();
                const dt = e.clipboardData || window.clipboardData;
                if (!dt) return;
                const html = dt.getData('text/html');
                const texto = dt.getData('text/plain');
                if (html) {
                    document.execCommand('insertHTML', false, sanitizarHtml(html));
                } else {
                    document.execCommand('insertText', false, texto || '');
                }
            });

            ed.addEventListener('drop', function (e) {
                const dt = e.dataTransfer;
                if (!dt) return;
                const html = dt.getData('text/html');
                if (html) {
                    e.preventDefault();
                    document.execCommand('insertHTML', false, sanitizarHtml(html));
                }
            });
        }

        function normalizeAppData(data) {
            if (!data || typeof data !== 'object') data = {};
            
            function ensureArray(val) {
                if (!val) return [];
                if (Array.isArray(val)) return val.filter(item => item !== null && item !== undefined);
                if (typeof val === 'object') return Object.values(val).filter(item => item !== null && item !== undefined);
                return [];
            }

            data.userProfile = data.userProfile && typeof data.userProfile === 'object' ? data.userProfile : { name: '', role: '', avatarPhoto: '' };
            data.accentTheme = data.accentTheme || 'indigo';
            data.subjects = ensureArray(data.subjects);
            // Lista vazia e um estado VALIDO: nao recria exemplos.

            // So mantem metadados (cor/icone) de materias que ainda existem.
            data.subjectMeta = (data.subjectMeta && typeof data.subjectMeta === 'object') ? data.subjectMeta : {};
            Object.keys(data.subjectMeta).forEach(k => {
                if (!data.subjects.includes(k)) delete data.subjectMeta[k];
            });

            data.tasks = ensureArray(data.tasks).map(t => {
                if (t && typeof t === 'object') {
                    t.subtasks = ensureArray(t.subtasks);
                    t.links = ensureArray(t.links);
                    t.attachments = ensureArray(t.attachments);
                }
                return t;
            });

            data.flashcardDecks = ensureArray(data.flashcardDecks).map(d => {
                if (d && typeof d === 'object') {
                    d.cards = ensureArray(d.cards);
                }
                return d;
            });

            data.notes = ensureArray(data.notes).map(n => {
                if (n && typeof n === 'object') {
                    n.resources = ensureArray(n.resources);
                    n.linkedTaskIds = ensureArray(n.linkedTaskIds);
                    n.linkedPostitIds = ensureArray(n.linkedPostitIds);
                    if (typeof n.parentId === 'undefined') n.parentId = null;
                }
                return n;
            });
            // parentId apontando pra nota que nao existe mais (ou pra ela
            // mesma) vira raiz, em vez de sumir da arvore silenciosamente.
            const idsDeNotas = data.notes.map(n => n.id);
            data.notes.forEach(n => {
                if (n.parentId && (n.parentId === n.id || !idsDeNotas.includes(n.parentId))) n.parentId = null;
            });

            // So mantem abas de notas que ainda existem - uma nota excluida
            // em outra maquina nao pode deixar uma aba fantasma aqui.
            const idsValidos = data.notes.filter(n => n && n.id).map(n => n.id);
            data.openNoteTabs = ensureArray(data.openNoteTabs).filter(id => idsValidos.includes(id));
            if (!data.activeNoteTabId || !data.openNoteTabs.includes(data.activeNoteTabId)) {
                data.activeNoteTabId = data.openNoteTabs[0] || null;
            }

            data.postits = ensureArray(data.postits);
            data.studyLogs = ensureArray(data.studyLogs);
            data.calendarEvents = ensureArray(data.calendarEvents);
            data.mindMaps = ensureArray(data.mindMaps).map(m => {
                if (m && typeof m === 'object') {
                    m.nodes = ensureArray(m.nodes);
                    m.connections = ensureArray(m.connections);
                    m.drawings = ensureArray(m.drawings).filter(d => d && typeof d === 'object' && d.d).map((d, i) => { if (!d.id) d.id = 'draw-' + Date.now() + '-' + i; return d; });
                    const idsValidos = m.nodes.filter(n => n && n.id).map(n => n.id);
                    // Conexao que aponta pra um topico que nao existe mais
                    // (apagado em outra maquina) e descartada, nao deixada
                    // pendurada apontando pro vazio.
                    m.connections = m.connections.filter(c => c && idsValidos.includes(c.from) && idsValidos.includes(c.to));
                }
                return m;
            });
            data.selectedSubjectIdsForDelete = ensureArray(data.selectedSubjectIdsForDelete);
            data.selectedTaskIds = ensureArray(data.selectedTaskIds);
            data.selectedDeckIds = ensureArray(data.selectedDeckIds);
            data.selectedNoteIds = ensureArray(data.selectedNoteIds);

            return data;
        }

        function openModal(modalId) {
            const el = document.getElementById(modalId);
            if (el) {
                el.classList.remove('hidden');
                el.classList.add('flex');
            }
        }

        function closeModal(modalId) {
            const el = document.getElementById(modalId);
            if (el) {
                el.classList.add('hidden');
                el.classList.remove('flex');
            }
        }

        /* ---------------- Tela de entrada ---------------- */
        let contaCogni = { configured: false, signedIn: false };

        function mostrarGate() {
            const g = document.getElementById('loginGate');
            if (!g) return;
            g.classList.remove('hidden'); g.classList.add('flex');
            setTimeout(() => { const e = document.getElementById('gateEmail'); if (e) e.focus(); }, 80);
        }
        function esconderGate() {
            const g = document.getElementById('loginGate');
            if (!g) return;
            g.classList.add('hidden'); g.classList.remove('flex');
        }
        function gateErro(msg) {
            const e = document.getElementById('gateError');
            if (!e) return;
            if (!msg) { e.classList.add('hidden'); return; }
            e.innerText = msg; e.classList.remove('hidden');
        }
        function gateOcupado(v) {
            ['gateSignIn','gateSignUp'].forEach(id => {
                const b = document.getElementById(id);
                if (b) { b.disabled = v; b.style.opacity = v ? .55 : 1; }
            });
        }
        async function gateEntrar()     { await gateAutenticar('cloud_sign_in'); }
        async function gateCriarConta() { await gateAutenticar('cloud_sign_up'); }

        async function gateAutenticar(metodo) {
            const email = (document.getElementById('gateEmail').value || '').trim();
            const senha = document.getElementById('gatePassword').value || '';
            if (!email || !senha) { gateErro('Informe e-mail e senha.'); return; }
            gateErro(''); gateOcupado(true);
            try {
                const r = await window.pywebview.api[metodo](email, senha);
                if (r.status !== 'success') { gateErro(r.message || 'Falha na autenticação.'); return; }
                document.getElementById('gatePassword').value = '';
                contaCogni = await window.pywebview.api.cloud_status();
                esconderGate();
                pintarStatusNuvem();
                // Descarta resquicios antes de carregar a conta nova.
                isAppDataLoadedFromDisk = false; isInitializingFromDisk = false;
                limparEstadoCogni();
                loadStateFromDiskSafely();
            } catch (e) {
                gateErro('Erro inesperado: ' + e);
            } finally { gateOcupado(false); }
        }

        /* Estado do banco na nuvem, exibido no topo. */
        function pintarStatusNuvem(evento) {
            const ponto = document.getElementById('save-dot');
            const icone = document.getElementById('cloud-status-icon');
            const hora  = document.getElementById('last-save-time');
            const barra = document.getElementById('cloudStatusBar');
            if (!hora) return;

            if (!contaCogni.signedIn) {
                if (ponto) ponto.className = 'w-2 h-2 rounded-full bg-amber-400';
                if (icone) icone.className = 'fa-solid fa-cloud-arrow-up text-amber-500';
                if (hora) { hora.innerText = 'Desconectado'; hora.className = 'text-amber-600 font-sans'; }
                if (barra) barra.title = 'Nenhuma conta conectada';
                return;
            }

            if (ponto) ponto.className = 'w-2 h-2 rounded-full bg-emerald-500 animate-pulse';
            if (icone) icone.className = 'fa-solid fa-cloud text-emerald-600';
            if (barra) barra.title = 'Conectado como ' + (contaCogni.email || '');

            if (hora) {
                hora.className = 'text-gray-400 font-sans';
                const t = new Date().toLocaleTimeString('en-US', {hour12:true,hour:'2-digit',minute:'2-digit',second:'2-digit'});
                if (evento === 'salvo')      hora.innerText = `Enviado às ${t}`;
                else if (evento === 'baixado') hora.innerText = `Atualizado às ${t}`;
                else if (evento === 'offline') { hora.innerText = 'Offline (salvo local)'; hora.className = 'text-amber-600 font-sans'; }
                else if (!hora.innerText || hora.innerText.includes('Conectando')) hora.innerText = 'Sincronizado';
            }
        }

        async function sairDaContaCogni() {
            if (!confirm('Sair da conta? Seus dados continuam salvos nesta máquina.')) return;

            // Trava a gravacao ANTES de trocar de conta, para que nenhum
            // autosave pendente escreva estes dados na pasta da proxima conta.
            isAppDataLoadedFromDisk = false;
            limparEstadoCogni();

            await window.pywebview.api.cloud_sign_out();
            contaCogni = await window.pywebview.api.cloud_status();
            closeModal('profile-modal');
            pintarStatusNuvem();
            mostrarGate();
        }

        /* Zera o que ficou da conta anterior em memoria e na tela. */
        function limparEstadoCogni() {
            appData = {
                userProfile: { name: '', role: '', avatarPhoto: '' },
                accentTheme: 'indigo',
                subjects: [], subjectMeta: {}, selectedSubjectIdsForDelete: [],
                tasks: [], selectedTaskIds: [],
                flashcardDecks: [], selectedDeckIds: [],
                notes: [], selectedNoteIds: [],
                openNoteTabs: [], activeNoteTabId: null,
                postits: [], studyLogs: [], calendarEvents: [], mindMaps: []
            };
            lastCloudJsonHash = '';
            currentActiveNoteId = null;
            try {
                renderUserProfile();
                renderSubjectsList();
                renderTasks();
                renderFlashcardDecks();
                renderNotesSidebar();
                renderPostitsCanvas();
                renderResources();
                renderDashboardStats();
            } catch (e) {}
        }

        document.addEventListener('keydown', (e) => {
            const g = document.getElementById('loginGate');
            if (e.key === 'Enter' && g && !g.classList.contains('hidden')) {
                e.preventDefault(); gateEntrar();
            }
        });

        async function loadStateFromDiskSafely() {
            if (isAppDataLoadedFromDisk || isInitializingFromDisk) return;

            // Sem conta conectada, abre a tela de entrada em vez de carregar.
            try {
                if (window.pywebview && window.pywebview.api &&
                    typeof window.pywebview.api.cloud_status === 'function') {
                    contaCogni = await window.pywebview.api.cloud_status();
                    if (contaCogni.configured && !contaCogni.signedIn) { pintarStatusNuvem(); mostrarGate(); return; }
                    esconderGate();
                    pintarStatusNuvem();
                }
            } catch (e) { /* segue como app local */ }

            if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.load_state_from_disk === 'function') {
                isInitializingFromDisk = true;
                window.pywebview.api.load_state_from_disk().then(res => {
                    if (res) {
                        try {
                            const parsed = JSON.parse(res);
                            if (parsed && typeof parsed === 'object') {
                                appData = normalizeAppData(parsed);
                                lastCloudJsonHash = res;
                            }
                        } catch(e) {
                            console.error("[Cogni Cloud] Erro ao processar JSON retornado:", e);
                        }
                    }
                    isAppDataLoadedFromDisk = true;
                    isInitializingFromDisk = false;
                    initApp();
                }).catch(err => {
                    console.error("[Cogni Cloud] Falha ao carregar dados via API Python:", err);
                    isAppDataLoadedFromDisk = true;
                    isInitializingFromDisk = false;
                    initApp();
                });
            }
        }

        window.addEventListener('pywebviewready', () => {
            loadStateFromDiskSafely();
        });

        const pywebviewInitInterval = setInterval(() => {
            if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.load_state_from_disk === 'function') {
                clearInterval(pywebviewInitInterval);
                loadStateFromDiskSafely();
            }
        }, 50);

        setTimeout(() => {
            clearInterval(pywebviewInitInterval);
            if (!isAppDataLoadedFromDisk && !isInitializingFromDisk) {
                isAppDataLoadedFromDisk = true;
                initApp();
            }
        }, 3000);

        /* ------------------------------------------------------------------
           Sincronizacao periodica com a nuvem.

           Antes rodava a cada 3s SEM PARAR: 1.200 leituras por hora, ~28.800
           por dia, mesmo com o app parado em segundo plano. Agora:
             - intervalo de 30s;
             - nao consulta com a janela fora de foco (app minimizado/atras);
             - nao consulta logo apos uma gravacao local (o dado ja e nosso);
             - ao voltar o foco, faz UMA consulta imediata, para nao parecer
               travado depois de trocar de janela.
           ------------------------------------------------------------------ */
        const CLOUD_POLL_MS = 30000;
        const CLOUD_QUIET_AFTER_SAVE_MS = 5000;
        let ultimaGravacaoLocal = 0;
        let sincronizando = false;

        function podeConsultarNuvem() {
            if (!contaCogni.signedIn) return false;              // sem conta
            if (sincronizando) return false;                      // ja tem uma em curso
            if (document.hidden || !document.hasFocus()) return false;  // janela em segundo plano
            if (document.querySelector('.fixed:not(.hidden)')) return false;  // modal aberto
            if (Date.now() - ultimaGravacaoLocal < CLOUD_QUIET_AFTER_SAVE_MS) return false;
            return true;
        }

        function consultarNuvem(forcado) {
            if (!forcado && !podeConsultarNuvem()) return;
            if (!(window.pywebview && window.pywebview.api &&
                  typeof window.pywebview.api.load_state_from_disk === 'function')) return;

            sincronizando = true;
            window.pywebview.api.load_state_from_disk().then(res => {
                if (res && res !== lastCloudJsonHash && res.trim() !== "null") {
                    try {
                        const parsed = JSON.parse(res);
                        if (parsed && typeof parsed === 'object') {
                            lastCloudJsonHash = res;
                            appData = normalizeAppData(parsed);
                            renderUserProfile();
                            renderSubjectsList();
                            renderTasks();
                            renderFlashcardDecks();
                            renderNotesSidebar();
                            renderPostitsCanvas();
                            renderResources();
                            renderDashboardStats();
                            updateCharts();

                            pintarStatusNuvem('baixado');
                        }
                    } catch(e) {
                        console.error("[Cogni Sync Error]:", e);
                    }
                }
            }).catch(() => {}).finally(() => { sincronizando = false; });
        }

        function startRealtimeCloudSync() {
            setInterval(() => consultarNuvem(false), CLOUD_POLL_MS);

            // Voltou para a janela: busca novidades na hora, sem esperar os 30s.
            window.addEventListener('focus', () => {
                if (podeConsultarNuvem()) consultarNuvem(true);
            });
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden && podeConsultarNuvem()) consultarNuvem(true);
            });
        }

        /* Depois do login, se a conta estiver vazia e existirem dados antigos
           em /database, pergunta se deve importar. Nunca copia sozinho. */
        async function oferecerImportarLegado() {
            try {
                if (!contaCogni.signedIn) return;
                const vazio = (appData.tasks || []).length === 0 &&
                              (appData.subjects || []).length === 0 &&
                              (appData.notes || []).length === 0 &&
                              (appData.flashcardDecks || []).length === 0;
                if (!vazio) return;

                const p = await window.pywebview.api.cloud_legacy_preview();
                if (!p || !p.available) return;

                const ok = confirm(
                    'Encontramos dados salvos antes do login neste banco:\n\n' +
                    `• ${p.tasks} tarefa(s)\n• ${p.subjects} matéria(s)\n• ${p.notes} anotação(ões)\n\n` +
                    'Deseja importar para a sua conta?\n' +
                    '(Os dados originais continuam preservados no banco.)'
                );
                if (!ok) return;

                const r = await window.pywebview.api.cloud_import_legacy();
                if (r.status !== 'success') { alert('Não foi possível importar: ' + (r.message || '')); return; }
                appData = normalizeAppData(JSON.parse(r.data));
                lastCloudJsonHash = r.data;
                initApp();
            } catch (e) { console.warn('legado:', e); }
        }

        function initApp() {
            appData = normalizeAppData(appData);

            applyAccentTheme(appData.accentTheme);
            renderDevopsIconPickerGrid();
            renderUserProfile();
            renderSubjectsList();
            renderTasks();
            renderFlashcardDecks();
            renderNotesSidebar();
            renderPostitsCanvas();
            renderResources();
            renderDashboardStats();
            initCharts();

            // Restaura as abas de cadernos que estavam abertas da ultima vez
            // (igual Notion/Google Docs: fechar o app nao perde as abas).
            if (!currentActiveNoteId) {
                if (appData.activeNoteTabId && appData.notes.some(n => n.id === appData.activeNoteTabId)) {
                    openNote(appData.activeNoteTabId);
                } else if (appData.openNoteTabs && appData.openNoteTabs.length > 0) {
                    openNote(appData.openNoteTabs[appData.openNoteTabs.length - 1]);
                } else {
                    renderNoteTabs();
                }
            }

            startRealtimeCloudSync();
            setTimeout(oferecerImportarLegado, 600);
            setTimeout(otimizarFotoAntiga, 1200);

            setTimeout(checarPrazosDeTarefas, 4000);
            setInterval(checarPrazosDeTarefas, 5 * 60 * 1000);
        }

        /* ------------------------------------------------------------------
           NOTIFICACOES DESKTOP: avisa sobre tarefas vencendo hoje ou
           atrasadas. notifiedTaskIds evita repetir a mesma tarefa varias
           vezes no mesmo dia; limpa a cada virada de dia. */
        let notifiedTaskIds = new Set();
        let lastNotifCheckDate = null;

        function checarPrazosDeTarefas() {
            if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api.send_desktop_notification !== 'function') return;

            const hojeStr = new Date().toISOString().slice(0, 10);
            if (lastNotifCheckDate && lastNotifCheckDate !== hojeStr) notifiedTaskIds.clear();
            lastNotifCheckDate = hojeStr;

            const pendentes = (appData.tasks || []).filter(t => t.status !== 'Concluído' && t.dueDate);
            const vencemHoje = pendentes.filter(t => t.dueDate === hojeStr);
            const atrasadas = pendentes.filter(t => t.dueDate < hojeStr);

            const aNotificar = [];
            vencemHoje.forEach(t => { if (!notifiedTaskIds.has(t.id)) { aNotificar.push({ t, label: 'Vence hoje' }); notifiedTaskIds.add(t.id); } });
            atrasadas.slice(0, 3).forEach(t => { if (!notifiedTaskIds.has(t.id)) { aNotificar.push({ t, label: 'Atrasada' }); notifiedTaskIds.add(t.id); } });

            if (aNotificar.length === 0) return;

            if (aNotificar.length === 1) {
                const n = aNotificar[0];
                window.pywebview.api.send_desktop_notification(
                    `Cogni: ${n.label}`,
                    `${n.t.title} (${n.t.project || 'Sem matéria'})`,
                    true
                );
            } else {
                const resumo = aNotificar.map(n => `• ${n.t.title} (${n.label})`).slice(0, 4).join('\n');
                window.pywebview.api.send_desktop_notification(`Cogni: ${aNotificar.length} tarefas precisam de atenção`, resumo, true);
            }
        }

        /* Fotos gravadas antes desta versão podem ter vários MB e pesam em
           TODA leitura do banco. Aqui elas são reduzidas uma única vez e
           regravadas, então o problema se corrige sozinho na primeira abertura. */
        const FOTO_LIMITE_KB = 120;

        function otimizarFotoAntiga() {
            try {
                const p = appData.userProfile || {};
                const foto = p.avatarPhoto || '';
                if (!foto || typeof foto !== 'string') return;

                const kb = foto.length / 1024;
                if (kb <= FOTO_LIMITE_KB) return;   // já está num tamanho saudável

                const img = new Image();
                img.onload = function() {
                    try {
                        let { width, height } = img;
                        const escala = Math.min(1, FOTO_MAX_PX / Math.max(width, height));
                        width = Math.max(1, Math.round(width * escala));
                        height = Math.max(1, Math.round(height * escala));

                        const cv = document.createElement('canvas');
                        cv.width = width; cv.height = height;
                        const ctx = cv.getContext('2d');
                        ctx.fillStyle = '#ffffff';
                        ctx.fillRect(0, 0, width, height);
                        ctx.drawImage(img, 0, 0, width, height);

                        const menor = cv.toDataURL('image/jpeg', FOTO_QUALIDADE);
                        if (menor && menor.length < foto.length) {
                            appData.userProfile.avatarPhoto = menor;
                            renderUserProfile();
                            triggerAutoSave();
                            console.info(`[Cogni] Foto de perfil otimizada: ${Math.round(kb)} KB -> ${Math.round(menor.length/1024)} KB`);
                        }
                    } catch (e) { console.warn('Falha ao otimizar foto antiga:', e); }
                };
                img.onerror = function() {
                    // Imagem corrompida no banco: limpa para nao pesar a toa.
                    console.warn('[Cogni] Foto de perfil ilegivel; removida.');
                    appData.userProfile.avatarPhoto = '';
                    renderUserProfile();
                    triggerAutoSave();
                };
                img.src = foto;
            } catch (e) { console.warn('otimizarFotoAntiga:', e); }
        }

        function triggerAutoSave() {
            if (!isAppDataLoadedFromDisk) return;
            appData = normalizeAppData(appData);
            const jsonStr = JSON.stringify(appData, null, 2);
            lastCloudJsonHash = jsonStr;

            if (window.pywebview && window.pywebview.api) {
                ultimaGravacaoLocal = Date.now();
                window.pywebview.api.save_state_to_disk(jsonStr).then(res => {
                    pintarStatusNuvem((res && res.status === 'success') ? 'salvo' : 'offline');
                });
            }
            updateBadges();
        }

        function getSubjectProgress(subjectName) {
            const subjectTasks = appData.tasks.filter(t => t.project === subjectName);
            if (subjectTasks.length === 0) return 0;
            const sum = subjectTasks.reduce((acc, curr) => acc + (Number(curr.progress) || 0), 0);
            return Math.round(sum / subjectTasks.length);
        }

        function _subjectStats(subjectName) {
            const ts = appData.tasks.filter(t => t.project === subjectName);
            const done = ts.filter(t => t.status === 'Concluído').length;
            return { total: ts.length, done: done, pending: ts.length - done, progress: getSubjectProgress(subjectName) };
        }

        function _subjectProgressBlock(subjectName, grande) {
            const st = _subjectStats(subjectName);
            const completo = st.total > 0 && st.progress >= 100;
            const resumo = st.total === 0 ? 'Nenhuma tarefa ainda'
                : `${st.done} de ${st.total} tarefa${st.total === 1 ? '' : 's'} concluída${st.done === 1 ? '' : 's'}` + (st.pending ? ` · ${st.pending} pendente${st.pending === 1 ? '' : 's'}` : '');
            const corBarra = completo ? 'bg-emerald-500' : 'accent-bg-badge';
            return `
                <div class="flex items-center justify-between gap-2 mt-1.5">
                    <span class="text-[10px] text-gray-400 leading-tight truncate" title="${resumo}">${resumo}</span>
                    <span class="${grande ? 'text-sm' : 'text-[11px]'} font-semibold shrink-0 ${completo ? 'text-emerald-600' : 'text-gray-600'}">${st.progress}%</span>
                </div>
                <div class="w-full bg-gray-100 h-1 rounded-full mt-1 overflow-hidden" title="Média do andamento das tarefas desta matéria">
                    <div class="${corBarra} h-full rounded-full transition-all duration-300" style="width: ${st.progress}%"></div>
                </div>`;
        }

        function filterBySubject(subjectName) {
            activeSubjectFilter = subjectName;
            switchTab('tasks');

            const filterSelect = document.getElementById('filter-subject');
            if (filterSelect) filterSelect.value = subjectName;

            const badge = document.getElementById('active-subject-filter-badge');
            const clearBtn = document.getElementById('btn-clear-subject-filter');
            if (subjectName !== 'ALL') {
                if (badge) { badge.innerText = `Matéria: ${subjectName}`; badge.classList.remove('hidden'); }
                if (clearBtn) clearBtn.classList.remove('hidden');
            } else {
                if (badge) badge.classList.add('hidden');
                if (clearBtn) clearBtn.classList.add('hidden');
            }

            renderSubjectsList();
            renderTasks();
        }

        function renderSubjectsList() {
            const container = document.getElementById('subject-list');
            const filterSelect = document.getElementById('filter-subject');
            const modalSelect = document.getElementById('modal-task-project');

            if (!container) return;

            container.innerHTML = appData.subjects.map(s => {
                const isSelectedForDelete = appData.selectedSubjectIdsForDelete.includes(s);
                const isCurrentlyFiltered = activeSubjectFilter === s;

                return `
                    <div class="w-full flex flex-col p-2.5 rounded-lg text-xs text-gray-700 cursor-pointer transition-all duration-150 border select-none ${isCurrentlyFiltered ? 'border-indigo-400 accent-light-bg shadow-sm font-bold ring-1 ring-indigo-300' : 'border-notion-border bg-white hover:bg-gray-50'}" 
                         onclick="filterBySubject('${escapeJsString(s)}')">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center space-x-2 truncate min-w-0 flex-1 pr-1">
                                <input type="checkbox" ${isSelectedForDelete ? 'checked' : ''} 
                                       onchange="toggleSelectSubject('${escapeJsString(s)}', this.checked)" 
                                       onclick="event.stopPropagation()" 
                                       class="rounded border-gray-300 accent-text-color focus:ring-0 cursor-pointer shrink-0">
                                <span title="${escapeHtml(s)}" class="truncate flex items-center gap-1.5 font-semibold ${isCurrentlyFiltered ? 'accent-text-color font-bold' : 'text-gray-800'}">
                                    <i class="${_subjectMeta(s).icon} text-[11px] shrink-0" style="color: ${_subjectMeta(s).color}"></i>
                                    <span class="truncate">${escapeHtml(s)}</span>
                                </span>
                            </div>
                            <div class="flex items-center space-x-1 shrink-0">
                                <button type="button" onclick="event.stopPropagation(); openSubjectModal('${escapeJsString(s)}')" class="text-gray-400 hover:text-indigo-600 p-0.5 text-[11px]" title="Editar Matéria">
                                    <i class="fa-solid fa-pen"></i>
                                </button>
                            </div>
                        </div>
                        ${_subjectProgressBlock(s, false)}
                    </div>
                `;
            }).join('');

            if (filterSelect) {
                const currentVal = activeSubjectFilter || 'ALL';
                filterSelect.innerHTML = `<option value="ALL">Todas as Matérias</option>` + appData.subjects.map(s => `<option value="${escapeHtml(s)}" ${currentVal === s ? 'selected' : ''}>${escapeHtml(s)}</option>`).join('');
            }
            if (modalSelect) {
                // Opcao em branco primeiro: e o que permite representar "sem
                // materia ainda" quando a tarefa e criada a partir de "Todas
                // as Tarefas" - ver openNewTaskModal().
                modalSelect.innerHTML =
                    `<option value="">Selecione uma matéria...</option>` +
                    appData.subjects.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
            }

            const bulkBtn = document.getElementById('btn-bulk-delete-subjects');
            if (bulkBtn) {
                if (appData.selectedSubjectIdsForDelete.length > 0) bulkBtn.classList.remove('hidden');
                else bulkBtn.classList.add('hidden');
            }
        }

        function toggleSelectSubject(sName, checked) {
            if (checked) {
                if (!appData.selectedSubjectIdsForDelete.includes(sName)) appData.selectedSubjectIdsForDelete.push(sName);
            } else {
                appData.selectedSubjectIdsForDelete = appData.selectedSubjectIdsForDelete.filter(x => x !== sName);
            }
            renderSubjectsList();
        }

        function deleteSelectedSubjects() {
            const count = appData.selectedSubjectIdsForDelete.length;
            if (count === 0) return;
            if (confirm(`Excluir as ${count} matérias selecionadas?`)) {
                appData.subjects = appData.subjects.filter(s => !appData.selectedSubjectIdsForDelete.includes(s));
                if (appData.subjectMeta) {
                    appData.selectedSubjectIdsForDelete.forEach(s => delete appData.subjectMeta[s]);
                }
                appData.selectedSubjectIdsForDelete = [];
                activeSubjectFilter = 'ALL';
                triggerAutoSave();
                renderSubjectsList();
                renderTasks();
                updateCharts();
            }
        }

        // Paleta fixa e pequena de proposito: cores que ja existem no tema
        // (indigo/rose/cyan/emerald/amber) mais algumas neutras, para o
        // resultado sempre combinar com a interface em vez de abrir pra
        // qualquer hexadecimal.
        const SUBJECT_COLOR_PALETTE = ['#4f46e5', '#e11d48', '#0284c7', '#059669', '#d97706', '#7c3aed', '#db2777', '#64748b'];
        let selectedSubjectColor = SUBJECT_COLOR_PALETTE[0];
        let selectedSubjectIcon = 'fa-solid fa-book';

        function _subjectMeta(nome) {
            return (appData.subjectMeta && appData.subjectMeta[nome]) || { color: '#64748b', icon: 'fa-solid fa-book' };
        }

        function renderSubjectColorPickerGrid() {
            const grid = document.getElementById('subject-color-picker-grid');
            if (!grid) return;
            grid.innerHTML = SUBJECT_COLOR_PALETTE.map(c => `
                <button type="button" onclick="selectSubjectColor('${c}')"
                        class="w-6 h-6 rounded-full border-2 ${selectedSubjectColor === c ? 'border-gray-800 scale-110' : 'border-transparent'} transition"
                        style="background-color: ${c}" title="${c}"></button>
            `).join('');
        }

        function selectSubjectColor(cor) {
            selectedSubjectColor = cor;
            renderSubjectColorPickerGrid();
        }

        function renderSubjectIconPickerGrid() {
            const grid = document.getElementById('subject-icon-picker-grid');
            if (!grid) return;
            grid.innerHTML = DEVOPS_ICONS.map(ic => `
                <button type="button" onclick="selectSubjectIcon('${ic.id}')"
                        class="p-2 border rounded hover:bg-gray-100 flex flex-col items-center justify-center text-xs ${selectedSubjectIcon === ic.id ? 'accent-btn-bg text-white border-transparent font-bold shadow-xs' : 'bg-white text-gray-700'}"
                        title="${ic.label}">
                    <i class="${ic.id} text-sm"></i>
                </button>
            `).join('');
        }

        function selectSubjectIcon(iconId) {
            selectedSubjectIcon = iconId;
            renderSubjectIconPickerGrid();
        }

        function openSubjectModal(oldName = '') {
            document.getElementById('subject-name-input').value = oldName;
            document.getElementById('edit-subject-old-name').value = oldName;
            document.getElementById('subject-modal-title').innerText = oldName ? "Editar Matéria" : "Nova Matéria";

            const meta = oldName ? _subjectMeta(oldName) : { color: SUBJECT_COLOR_PALETTE[appData.subjects.length % SUBJECT_COLOR_PALETTE.length], icon: 'fa-solid fa-book' };
            selectedSubjectColor = meta.color;
            selectedSubjectIcon = meta.icon;
            renderSubjectColorPickerGrid();
            renderSubjectIconPickerGrid();

            openModal('subject-modal');
        }

        function saveSubjectSubmit() {
            const name = document.getElementById('subject-name-input').value.trim();
            const oldName = document.getElementById('edit-subject-old-name').value;

            if (!name) return;
            if (!appData.subjectMeta) appData.subjectMeta = {};

            if (oldName) {
                const idx = appData.subjects.indexOf(oldName);
                if (idx >= 0) appData.subjects[idx] = name;
                appData.tasks.forEach(t => { if (t.project === oldName) t.project = name; });
                appData.notes.forEach(n => { if (n.subject === oldName) n.subject = name; });
                appData.flashcardDecks.forEach(d => { if (d.subject === oldName) d.subject = name; });
                appData.postits.forEach(p => { if (p.attachedSubject === oldName) p.attachedSubject = name; });
                if (oldName !== name) delete appData.subjectMeta[oldName];
            } else {
                if (!appData.subjects.includes(name)) appData.subjects.push(name);
            }
            appData.subjectMeta[name] = { color: selectedSubjectColor, icon: selectedSubjectIcon };

            triggerAutoSave();
            closeModal('subject-modal');
            renderSubjectsList();
            renderTasks();
            renderDashboardStats();
            updateCharts();
        }

        function renderDevopsIconPickerGrid() {
            const container = document.getElementById('devops-icon-picker-grid');
            if (!container) return;
            container.innerHTML = DEVOPS_ICONS.map(ic => `
                <button type="button" onclick="selectDevopsIcon('${ic.id}')" 
                        class="p-2 border rounded hover:bg-gray-100 flex flex-col items-center justify-center text-xs ${selectedDevopsIcon === ic.id ? 'accent-btn-bg text-white border-transparent font-bold shadow-xs' : 'bg-white text-gray-700'}" 
                        title="${ic.label}">
                    <i class="${ic.id} text-sm"></i>
                </button>
            `).join('');
        }

        function selectDevopsIcon(iconId) {
            selectedDevopsIcon = iconId;
            document.getElementById('modal-task-selected-icon').value = iconId;
            renderDevopsIconPickerGrid();
        }

        function setTasksViewMode(mode) {
            activeTasksViewMode = mode;
            const tableContainer = document.getElementById('tasks-table-container');
            const kanbanContainer = document.getElementById('tasks-kanban-container');
            const btnTable = document.getElementById('view-mode-table-btn');
            const btnKanban = document.getElementById('view-mode-kanban-btn');

            if (mode === 'table') {
                tableContainer.classList.remove('hidden');
                kanbanContainer.classList.add('hidden');
                btnTable.className = "px-2.5 py-1 rounded font-bold bg-white accent-text-color shadow-2xs";
                btnKanban.className = "px-2.5 py-1 rounded font-bold text-gray-600 hover:text-gray-900";
            } else {
                tableContainer.classList.add('hidden');
                kanbanContainer.classList.remove('hidden');
                btnKanban.className = "px-2.5 py-1 rounded font-bold bg-white accent-text-color shadow-2xs";
                btnTable.className = "px-2.5 py-1 rounded font-bold text-gray-600 hover:text-gray-900";
            }
            renderTasks();
        }

        function renderTasks() {
            const search = (document.getElementById('search-tasks')?.value || '').toLowerCase();
            const prio = document.getElementById('filter-priority')?.value || 'ALL';

            let filtered = appData.tasks.filter(t => {
                const matchesSearch = t.title.toLowerCase().includes(search) || t.project.toLowerCase().includes(search);
                const matchesSubj = activeSubjectFilter === 'ALL' || t.project === activeSubjectFilter;
                const matchesPrio = prio === 'ALL' || t.priority === prio;
                return matchesSearch && matchesSubj && matchesPrio;
            });

            const pendingTasks = filtered.filter(t => t.status !== 'Concluído');
            const completedTasks = filtered.filter(t => t.status === 'Concluído');

            const bulkBtn = document.getElementById('btn-bulk-delete-tasks');
            const countLabel = document.getElementById('selected-tasks-count');
            if (bulkBtn) {
                if (appData.selectedTaskIds.length > 0) {
                    bulkBtn.classList.remove('hidden');
                    if (countLabel) countLabel.innerText = appData.selectedTaskIds.length;
                } else {
                    bulkBtn.classList.add('hidden');
                }
            }

            const tbody = document.getElementById('tasks-table-body');
            if (tbody) {
                if (pendingTasks.length === 0) {
                    tbody.innerHTML = `<tr><td colspan="8" class="text-center py-6 text-notion-muted text-xs">Nenhuma tarefa pendente encontrada.</td></tr>`;
                } else {
                    tbody.innerHTML = pendingTasks.map(task => {
                        const isSelected = appData.selectedTaskIds.includes(task.id);
                        const progressVal = task.progress || 0;
                        const hasSubtasks = task.subtasks && task.subtasks.length > 0;

                        return `
                            <tr class="hover:bg-notion-lightBg transition group cursor-pointer" onclick="editTask('${task.id}')" title="Clique para editar">
                                <td class="py-3 px-2 text-center align-top" onclick="event.stopPropagation()">
                                    <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectTask('${task.id}', this.checked)" class="rounded accent-text-color focus:ring-0 cursor-pointer">
                                </td>
                                <td class="tk-col-sym py-3 px-2 text-center text-sm accent-icon-color align-top"><i class="${task.icon || 'fa-solid fa-book'}"></i></td>
                                <td class="py-3 px-3 font-semibold text-gray-900 align-top">
                                    <div>${escapeHtml(task.title)}</div>
                                    <div class="tk-subj-inline mt-1"><span class="bg-gray-100 px-1.5 py-0.5 rounded text-[10px] font-medium text-gray-600">${escapeHtml(task.project)}${task.dueDate ? ' · ' + escapeHtml(task.dueDate) : ''}</span></div>
                                    ${hasSubtasks ? `
                                        <div class="mt-1.5 space-y-1 bg-gray-50/80 p-2 rounded border border-gray-200/60 max-h-32 overflow-y-auto" onclick="event.stopPropagation()">
                                            ${task.subtasks.map((st, idx) => `
                                                <label class="flex items-center space-x-2 text-[11px] text-gray-700 font-normal cursor-pointer hover:text-black">
                                                    <input type="checkbox" ${st.completed ? 'checked' : ''} onchange="toggleInlineTaskSubtask('${task.id}', ${idx})" class="rounded accent-text-color">
                                                    <span class="${st.completed ? 'line-through text-gray-400' : ''}">${escapeHtml(st.text)}</span>
                                                </label>
                                            `).join('')}
                                        </div>
                                    ` : ''}
                                    ${(task.attachments && task.attachments.length > 0) || (task.links && task.links.length > 0) ? `
                                        <div class="mt-1.5 flex flex-wrap gap-1.5 items-center">
                                            ${(task.attachments || []).map(a => `
                                                <button type="button" onclick="event.stopPropagation(); triggerOpenFile('${escapeJsString(a.path)}')" class="inline-flex items-center gap-1 text-[10px] font-medium bg-rose-50 text-rose-700 border border-rose-200 px-2 py-0.5 rounded hover:bg-rose-100 transition shadow-2xs cursor-pointer" title="Abrir: ${escapeHtml(a.path)}">
                                                    <i class="fa-solid fa-paperclip text-[9px]"></i>
                                                    <span class="truncate max-w-[130px]">${escapeHtml(a.name || 'Anexo')}</span>
                                                </button>
                                            `).join('')}
                                            ${(task.links || []).map(l => `
                                                <button type="button" onclick="event.stopPropagation(); triggerOpenFile('${escapeJsString(l.url)}')" class="inline-flex items-center gap-1 text-[10px] font-medium bg-blue-50 text-blue-700 border border-blue-200 px-2 py-0.5 rounded hover:bg-blue-100 transition shadow-2xs cursor-pointer" title="Abrir: ${escapeHtml(l.url)}">
                                                    <i class="fa-solid fa-link text-[9px]"></i>
                                                    <span class="truncate max-w-[130px]">${escapeHtml(l.url)}</span>
                                                </button>
                                            `).join('')}
                                        </div>
                                    ` : ''}
                                </td>
                                <td class="tk-col-subj py-3 px-3 align-top"><span class="bg-gray-100 px-2 py-0.5 rounded font-medium inline-block">${escapeHtml(task.project)}</span></td>
                                <td class="tk-col-prog py-3 px-3 align-top" onclick="event.stopPropagation()">
                                    <div class="flex items-center space-x-1.5">
                                        <input type="range" min="0" max="100" value="${progressVal}" onchange="changeTaskProgressDirect('${task.id}', this.value)" class="h-1.5 bg-gray-200 rounded cursor-pointer accent-text-color">
                                        <span class="tk-pct font-mono text-[11px] font-bold accent-text-color">${progressVal}%</span>
                                    </div>
                                </td>
                                <td class="py-3 px-3 align-top" onclick="event.stopPropagation()">
                                    <select onchange="updateTaskStatus('${task.id}', this.value)" class="text-[11px] px-1 py-0.5 rounded border font-medium bg-white w-full">
                                        <option value="A Fazer" ${task.status === 'A Fazer' ? 'selected' : ''}>A Fazer</option>
                                        <option value="Em Andamento" ${task.status === 'Em Andamento' ? 'selected' : ''}>Em Andamento</option>
                                        <option value="Concluído" ${task.status === 'Concluído' ? 'selected' : ''}>Concluído</option>
                                    </select>
                                </td>
                                <td class="tk-col-date py-3 px-3 text-notion-muted align-top">${task.dueDate || 'Sem data'}</td>
                                <td class="py-3 px-2 text-right align-top" onclick="event.stopPropagation()">
                                    <button onclick="deleteTask('${task.id}')" class="text-gray-500 hover:text-red-600" title="Excluir"><i class="fa-solid fa-trash"></i></button>
                                </td>
                            </tr>
                        `;
                    }).join('');
                }
            }

            const completedWrapper = document.getElementById('completed-tasks-wrapper');
            const completedContainer = document.getElementById('completed-tasks-container');
            const completedCountLabel = document.getElementById('completed-tasks-count-label');

            if (completedWrapper && completedContainer) {
                if (completedTasks.length > 0) {
                    completedWrapper.classList.remove('hidden');
                    if (completedCountLabel) completedCountLabel.innerText = completedTasks.length;

                    completedContainer.innerHTML = completedTasks.map(task => `
                        <div class="p-2.5 bg-white border border-emerald-200 rounded-lg flex items-center justify-between text-xs">
                            <div class="flex items-center space-x-2">
                                <i class="${task.icon || 'fa-solid fa-book'} text-emerald-600 cg-keep"></i>
                                <span class="line-through text-gray-500 font-medium">${escapeHtml(task.title)}</span>
                            </div>
                            <button onclick="updateTaskStatus('${task.id}', 'A Fazer')" class="text-[10px] bg-emerald-100 hover:bg-emerald-200 text-emerald-800 font-bold px-2 py-0.5 rounded">
                                Reativar
                            </button>
                        </div>
                    `).join('');
                } else {
                    completedWrapper.classList.add('hidden');
                }
            }

            const kanbanTodo = filtered.filter(t => t.status === 'A Fazer');
            const kanbanDoing = filtered.filter(t => t.status === 'Em Andamento');
            const kanbanDone = filtered.filter(t => t.status === 'Concluído');

            document.getElementById('kanban-todo-count').innerText = kanbanTodo.length;
            document.getElementById('kanban-doing-count').innerText = kanbanDoing.length;
            document.getElementById('kanban-done-count').innerText = kanbanDone.length;

            document.getElementById('kanban-col-todo').innerHTML = kanbanTodo.map(t => buildKanbanCard(t)).join('');
            document.getElementById('kanban-col-doing').innerHTML = kanbanDoing.map(t => buildKanbanCard(t)).join('');
            document.getElementById('kanban-col-done').innerHTML = kanbanDone.map(t => buildKanbanCard(t)).join('');

            updateBadges();
            renderDashboardStats();
        }

        function toggleInlineTaskSubtask(taskId, subtaskIdx) {
            const task = appData.tasks.find(t => t.id === taskId);
            if (task && task.subtasks && task.subtasks[subtaskIdx]) {
                task.subtasks[subtaskIdx].completed = !task.subtasks[subtaskIdx].completed;
                
                const doneCount = task.subtasks.filter(s => s.completed).length;
                task.progress = Math.round((doneCount / task.subtasks.length) * 100);

                if (task.progress === 100) task.status = 'Concluído';
                else if (task.progress > 0) task.status = 'Em Andamento';

                triggerAutoSave();
                renderTasks();
                renderSubjectsList();
                renderDashboardStats();
                updateCharts();
            }
        }

        function buildKanbanCard(task) {
            return `
                <div class="p-3 bg-white border border-notion-border rounded-lg shadow-2xs space-y-2 cursor-pointer hover:border-indigo-300 hover:shadow-sm transition" onclick="editTask('${task.id}')" title="Clique para editar">
                    <div class="flex items-center justify-between">
                        <span class="text-xs accent-icon-color"><i class="${task.icon || 'fa-solid fa-book'}"></i></span>
                        <span class="text-[10px] bg-gray-100 px-1.5 py-0.5 rounded font-medium">${escapeHtml(task.project)}</span>
                    </div>
                    <p class="text-xs font-bold text-gray-900">${escapeHtml(task.title)}</p>

                    ${task.subtasks && task.subtasks.length > 0 ? `
                        <div class="space-y-1 bg-gray-50 p-1.5 rounded border text-[11px]" onclick="event.stopPropagation()">
                            ${task.subtasks.map((st, idx) => `
                                <label class="flex items-center space-x-1.5 cursor-pointer">
                                    <input type="checkbox" ${st.completed ? 'checked' : ''} onchange="toggleInlineTaskSubtask('${task.id}', ${idx})" class="rounded accent-text-color">
                                    <span class="${st.completed ? 'line-through text-gray-400' : ''}">${escapeHtml(st.text)}</span>
                                </label>
                            `).join('')}
                        </div>
                    ` : ''}

                    ${(task.attachments && task.attachments.length > 0) || (task.links && task.links.length > 0) ? `
                        <div class="flex flex-wrap gap-1 items-center pt-1">
                            ${(task.attachments || []).map(a => `
                                <button type="button" onclick="event.stopPropagation(); triggerOpenFile('${escapeJsString(a.path)}')" class="inline-flex items-center gap-1 text-[10px] font-medium bg-rose-50 text-rose-700 border border-rose-200 px-1.5 py-0.5 rounded hover:bg-rose-100 transition truncate max-w-[150px] cursor-pointer" title="Abrir arquivo">
                                    <i class="fa-solid fa-paperclip text-[9px]"></i>
                                    <span class="truncate">${escapeHtml(a.name || 'Anexo')}</span>
                                </button>
                            `).join('')}
                            ${(task.links || []).map(l => `
                                <button type="button" onclick="event.stopPropagation(); triggerOpenFile('${escapeJsString(l.url)}')" class="inline-flex items-center gap-1 text-[10px] font-medium bg-blue-50 text-blue-700 border border-blue-200 px-1.5 py-0.5 rounded hover:bg-blue-100 transition truncate max-w-[150px] cursor-pointer" title="Abrir link">
                                    <i class="fa-solid fa-link text-[9px]"></i>
                                    <span class="truncate">${escapeHtml(l.url)}</span>
                                </button>
                            `).join('')}
                        </div>
                    ` : ''}

                    <div class="flex items-center justify-between text-[10px] text-notion-muted pt-1">
                        <span>Progresso: <b class="accent-text-color">${task.progress || 0}%</b></span>
                        <div class="flex space-x-1" onclick="event.stopPropagation()">
                            <button onclick="deleteTask('${task.id}')" class="hover:text-red-600" title="Excluir"><i class="fa-solid fa-trash"></i></button>
                        </div>
                    </div>
                </div>
            `;
        }

        function toggleCompletedTasksSection() {
            const container = document.getElementById('completed-tasks-container');
            const icon = document.getElementById('completed-chevron-icon');
            if (container) container.classList.toggle('hidden');
            if (icon) icon.classList.toggle('fa-chevron-up');
        }

        function toggleSelectTask(id, checked) {
            if (checked) {
                if (!appData.selectedTaskIds.includes(id)) appData.selectedTaskIds.push(id);
            } else {
                appData.selectedTaskIds = appData.selectedTaskIds.filter(x => x !== id);
            }
            renderTasks();
        }

        function toggleSelectAllTasks(checkbox) {
            const search = (document.getElementById('search-tasks')?.value || '').toLowerCase();
            const prio = document.getElementById('filter-priority')?.value || 'ALL';

            let visibleTasks = appData.tasks.filter(t => {
                const matchesSearch = t.title.toLowerCase().includes(search) || t.project.toLowerCase().includes(search);
                const matchesSubj = activeSubjectFilter === 'ALL' || t.project === activeSubjectFilter;
                const matchesPrio = prio === 'ALL' || t.priority === prio;
                return matchesSearch && matchesSubj && matchesPrio && t.status !== 'Concluído';
            });

            const visibleIds = visibleTasks.map(t => t.id);

            if (checkbox.checked) {
                visibleIds.forEach(id => {
                    if (!appData.selectedTaskIds.includes(id)) appData.selectedTaskIds.push(id);
                });
            } else {
                appData.selectedTaskIds = appData.selectedTaskIds.filter(id => !visibleIds.includes(id));
            }
            renderTasks();
        }

        function deleteSelectedTasks() {
            const count = appData.selectedTaskIds.length;
            if (count === 0) return;
            if (confirm(`Excluir as ${count} tarefas selecionadas?`)) {
                appData.tasks = appData.tasks.filter(t => !appData.selectedTaskIds.includes(t.id));
                appData.selectedTaskIds = [];
                triggerAutoSave();
                renderTasks();
                renderSubjectsList();
                renderDashboardStats();
                updateCharts();
            }
        }

        function changeTaskProgressDirect(id, val) {
            const task = appData.tasks.find(t => t.id === id);
            if (task) {
                task.progress = Number(val);
                if (task.progress === 100) task.status = 'Concluído';
                else if (task.progress > 0 && task.status === 'A Fazer') task.status = 'Em Andamento';
                triggerAutoSave();
                renderTasks();
                renderSubjectsList();
                renderDashboardStats();
                updateCharts();
            }
        }

        function updateTaskStatus(id, newStatus) {
            const task = appData.tasks.find(t => t.id === id);
            if (task) {
                task.status = newStatus;
                if (newStatus === 'Concluído') task.progress = 100;
                else if (newStatus === 'A Fazer' && task.progress === 100) task.progress = 0;
                triggerAutoSave();
                renderTasks();
                renderSubjectsList();
                renderDashboardStats();
                updateCharts();
            }
        }

        function openNewTaskModal() {
            document.getElementById('modal-task-header-title').innerText = "Nova Tarefa";
            document.getElementById('modal-task-id').value = "";
            document.getElementById('modal-task-title').value = "";
            document.getElementById('modal-task-date').value = "";
            document.getElementById('modal-task-priority').value = "Média";
            document.getElementById('modal-task-status').value = "A Fazer";
            selectedDevopsIcon = 'fa-solid fa-book';
            selectDevopsIcon('fa-solid fa-book');

            // Dentro de uma materia filtrada: a tarefa ja nasce naquela
            // materia, sem precisar escolher de novo. Em "Todas as Tarefas"
            // (activeSubjectFilter === 'ALL'): fica em branco, o usuario
            // escolhe - ver a opcao vazia adicionada em renderSubjectsList().
            const projectSelect = document.getElementById('modal-task-project');
            if (projectSelect) {
                projectSelect.value = (activeSubjectFilter && activeSubjectFilter !== 'ALL')
                    ? activeSubjectFilter
                    : '';
            }

            modalSubtasksTemp = [];
            modalLinksTemp = [];
            modalAttachmentsTemp = [];

            renderModalSubtasks();
            renderModalLinks();
            renderModalAttachments();

            // O painel mora na tela de Tarefas: se o usuario estiver em outra
            // tela (dashboard, nota vinculada, busca), leva ele pra la antes.
            const vt = document.getElementById('view-tasks');
            if (vt && vt.classList.contains('hidden')) switchTab('tasks');
            openModal('task-modal');
        }

        function addModalSubtask() {
            const input = document.getElementById('new-subtask-input');
            if (input && input.value.trim()) {
                modalSubtasksTemp.push({ id: 'st-' + Date.now(), text: input.value.trim(), completed: false });
                input.value = '';
                renderModalSubtasks();
            }
        }

        function toggleModalSubtask(idx) {
            if (modalSubtasksTemp[idx]) {
                modalSubtasksTemp[idx].completed = !modalSubtasksTemp[idx].completed;
                renderModalSubtasks();
            }
        }

        function removeModalSubtask(idx) {
            modalSubtasksTemp.splice(idx, 1);
            renderModalSubtasks();
        }

        function renderModalSubtasks() {
            const container = document.getElementById('modal-subtasks-list');
            if (!container) return;
            container.innerHTML = modalSubtasksTemp.map((sub, idx) => `
                <div class="flex items-center justify-between bg-notion-lightBg px-2 py-1 rounded border border-notion-border text-xs">
                    <div class="flex items-center space-x-2 flex-1 min-w-0">
                        <input type="checkbox" ${sub.completed ? 'checked' : ''} onchange="toggleModalSubtask(${idx})" class="rounded accent-text-color shrink-0">
                        <span onclick="editModalSubtask(${idx}, this)" title="Clique para editar" class="flex-1 min-w-0 break-words cursor-text ${sub.completed ? 'line-through text-gray-400' : 'text-gray-800'}">${escapeHtml(sub.text)}</span>
                    </div>
                    <button type="button" onclick="removeModalSubtask(${idx})" class="text-red-500 hover:text-red-700"><i class="fa-solid fa-times"></i></button>
                </div>
            `).join('');
        }

        function addModalLink() {
            const input = document.getElementById('new-link-input');
            if (input && input.value.trim()) {
                modalLinksTemp.push({ id: 'l-' + Date.now(), url: input.value.trim() });
                input.value = '';
                renderModalLinks();
            }
        }

        function removeModalLink(idx) {
            modalLinksTemp.splice(idx, 1);
            renderModalLinks();
        }

        // Clicar no texto da etapa troca ele por um campo editavel no lugar.
        // Enter ou clicar fora salva; Esc cancela. Etapa vazia nao e salva.
        function editModalSubtask(idx, span) {
            const sub = modalSubtasksTemp[idx];
            if (!sub || span.dataset.editing) return;
            span.dataset.editing = '1';
            const input = document.createElement('input');
            input.type = 'text';
            input.value = sub.text;
            input.className = 'w-full bg-white border rounded px-1.5 py-0.5 text-gray-800 focus:outline-none';
            span.replaceWith(input);
            input.focus();
            input.select();
            let done = false;
            const finish = (save) => {
                if (done) return;
                done = true;
                const v = input.value.trim();
                if (save && v) sub.text = v;
                renderModalSubtasks();
            };
            input.addEventListener('keydown', e => {
                if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); finish(true); }
                else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
            });
            input.addEventListener('blur', () => finish(true));
        }
        function renderModalLinks() {
            const container = document.getElementById('modal-links-list');
            if (!container) return;
            container.innerHTML = modalLinksTemp.map((l, idx) => `
                <div class="flex items-center justify-between bg-blue-50 px-2 py-1 rounded border border-blue-200 text-xs">
                    <span class="truncate font-mono text-blue-800">${escapeHtml(l.url)}</span>
                    <button type="button" onclick="removeModalLink(${idx})" class="text-red-500"><i class="fa-solid fa-times"></i></button>
                </div>
            `).join('');
        }

        function pickModalAttachment() {
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.pick_file_native().then(res => {
                    if (res && res.status === 'success') {
                        modalAttachmentsTemp.push({ id: 'att-' + Date.now(), name: res.name, path: res.path });
                        renderModalAttachments();
                    }
                });
            }
        }

        function removeModalAttachment(idx) {
            modalAttachmentsTemp.splice(idx, 1);
            renderModalAttachments();
        }

        function renderModalAttachments() {
            const container = document.getElementById('modal-attachments-list');
            if (!container) return;
            container.innerHTML = modalAttachmentsTemp.map((att, idx) => `
                <div class="flex items-center justify-between bg-rose-50 px-2 py-1 rounded border border-rose-200 text-xs">
                    <span class="truncate text-rose-800 font-medium">${escapeHtml(att.name)}</span>
                    <button type="button" onclick="removeModalAttachment(${idx})" class="text-red-500"><i class="fa-solid fa-times"></i></button>
                </div>
            `).join('');
        }

        function handleTaskSubmit(e) {
            e.preventDefault();
            const id = document.getElementById('modal-task-id').value || 't-' + Date.now();
            const title = document.getElementById('modal-task-title').value.trim();
            // Sem fallback para appData.subjects[0]: um valor vazio aqui e
            // "sem materia" de proposito (tarefa criada em "Todas as
            // Tarefas" sem o usuario escolher uma) - nao a materia errada
            // escolhida em silencio por ordem alfabetica/insercao.
            const project = document.getElementById('modal-task-project').value || '';
            const priority = document.getElementById('modal-task-priority').value;
            const status = document.getElementById('modal-task-status').value;
            const dueDate = document.getElementById('modal-task-date').value;

            // Sem etapas, o progresso vem do slider da lista de Tarefas: manter
            // o valor ja salvo (antes era zerado a cada salvamento do painel).
            const prevTask = appData.tasks.find(t => t.id === id);
            let progress = prevTask ? (Number(prevTask.progress) || 0) : 0;
            if (modalSubtasksTemp.length > 0) {
                const doneCount = modalSubtasksTemp.filter(s => s.completed).length;
                progress = Math.round((doneCount / modalSubtasksTemp.length) * 100);
            } else if (status === 'Concluído') {
                progress = 100;
            } else if (progress === 100) {
                progress = 0;
            }

            const taskObj = {
                id,
                icon: selectedDevopsIcon,
                title,
                project,
                priority,
                status: progress === 100 ? 'Concluído' : status,
                progress,
                dueDate,
                subtasks: modalSubtasksTemp,
                links: modalLinksTemp,
                attachments: modalAttachmentsTemp
            };

            const existingIndex = appData.tasks.findIndex(t => t.id === id);
            if (existingIndex >= 0 && appData.tasks[existingIndex].noteId) taskObj.noteId = appData.tasks[existingIndex].noteId;
            if (existingIndex >= 0) {
                appData.tasks[existingIndex] = taskObj;
            } else {
                appData.tasks.push(taskObj);
            }

            triggerAutoSave();
            closeModal('task-modal');
            renderTasks();
            renderSubjectsList();
            renderDashboardStats();
            updateCharts();
        }

        function editTask(taskId) {
            const task = appData.tasks.find(t => t.id === taskId);
            if (!task) return;

            openNewTaskModal();
            document.getElementById('modal-task-header-title').innerText = "Editar Tarefa";
            document.getElementById('modal-task-id').value = task.id;
            document.getElementById('modal-task-title').value = task.title;
            document.getElementById('modal-task-project').value = task.project;
            document.getElementById('modal-task-priority').value = task.priority;
            document.getElementById('modal-task-status').value = task.status;
            document.getElementById('modal-task-date').value = task.dueDate || '';

            selectDevopsIcon(task.icon || 'fa-solid fa-book');

            modalSubtasksTemp = task.subtasks ? [...task.subtasks] : [];
            modalLinksTemp = task.links ? [...task.links] : [];
            modalAttachmentsTemp = task.attachments ? [...task.attachments] : [];

            renderModalSubtasks();
            renderModalLinks();
            renderModalAttachments();
        }

        function deleteTask(taskId) {
            if (confirm('Excluir esta tarefa?')) {
                appData.tasks = appData.tasks.filter(t => t.id !== taskId);
                triggerAutoSave();
                renderTasks();
                renderSubjectsList();
                renderDashboardStats();
                updateCharts();
            }
        }

        /* ------------------------------------------------------------------
           EXPORTAR / IMPORTAR CSV (tarefas) */
        function _csvEscapeCell(valor) {
            let v = String(valor == null ? '' : valor);
            // Mitigacao de "CSV/formula injection": uma celula comecando com
            // =, +, -, @ e executada como formula se o usuario abrir o
            // arquivo no Excel. Um apostrofo na frente neutraliza sem mudar
            // o que aparece pra quem so le o texto.
            if (/^[=+\-@]/.test(v)) v = "'" + v;
            if (/[",\n]/.test(v)) v = '"' + v.replace(/"/g, '""') + '"';
            return v;
        }

        function exportarTarefasCSV() {
            const colunas = ['titulo', 'materia', 'prioridade', 'status', 'progresso', 'dataLimite'];
            const linhas = [colunas.join(',')];
            (appData.tasks || []).forEach(t => {
                linhas.push([
                    _csvEscapeCell(t.title), _csvEscapeCell(t.project), _csvEscapeCell(t.priority),
                    _csvEscapeCell(t.status), _csvEscapeCell(t.progress || 0), _csvEscapeCell(t.dueDate || '')
                ].join(','));
            });
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.export_csv(linhas.join('\r\n'), 'tarefas_cogni.csv').then(res => {
                    if (res.status === 'error') alert('Erro ao exportar: ' + res.message);
                });
            }
        }

        // Parser CSV minimo (RFC4180): respeita aspas, virgula e quebra de
        // linha dentro de campo entre aspas.
        function _csvParse(texto) {
            const linhas = [];
            let campo = '', linha = [], dentroAspas = false;
            const s = texto.replace(/\r\n/g, '\n').replace(/^﻿/, '');
            for (let i = 0; i < s.length; i++) {
                const c = s[i];
                if (dentroAspas) {
                    if (c === '"' && s[i + 1] === '"') { campo += '"'; i++; }
                    else if (c === '"') { dentroAspas = false; }
                    else { campo += c; }
                } else if (c === '"') { dentroAspas = true; }
                else if (c === ',') { linha.push(campo); campo = ''; }
                else if (c === '\n') { linha.push(campo); linhas.push(linha); linha = []; campo = ''; }
                else { campo += c; }
            }
            if (campo.length > 0 || linha.length > 0) { linha.push(campo); linhas.push(linha); }
            return linhas.filter(l => l.length > 1 || l[0] !== '');
        }

        function importarTarefasCSV() {
            if (!window.pywebview || !window.pywebview.api) return;
            window.pywebview.api.import_csv_pick().then(res => {
                if (res.status === 'cancelled') return;
                if (res.status === 'error') { alert('Erro ao importar: ' + res.message); return; }

                const linhas = _csvParse(res.content);
                if (linhas.length < 2) { alert('CSV vazio ou sem dados reconhecíveis.'); return; }

                const header = linhas[0].map(h => h.trim().toLowerCase());
                const idx = {
                    titulo: header.indexOf('titulo'), materia: header.indexOf('materia'),
                    prioridade: header.indexOf('prioridade'), status: header.indexOf('status'),
                    progresso: header.indexOf('progresso'), dataLimite: header.indexOf('datalimite')
                };
                if (idx.titulo === -1) { alert('CSV precisa ter ao menos a coluna "titulo".'); return; }

                let importadas = 0;
                linhas.slice(1).forEach(l => {
                    const titulo = (l[idx.titulo] || '').trim();
                    if (!titulo) return;
                    // Materias desconhecidas sao criadas na hora, para a
                    // importacao nao falhar silenciosamente nem perder o dado.
                    const materia = idx.materia >= 0 ? (l[idx.materia] || '').trim() : '';
                    if (materia && !appData.subjects.includes(materia)) appData.subjects.push(materia);

                    appData.tasks.push({
                        id: 't-' + Date.now() + '-' + importadas,
                        icon: 'fa-solid fa-book',
                        title: titulo,
                        project: materia,
                        priority: (idx.prioridade >= 0 && l[idx.prioridade]) || 'Média',
                        status: (idx.status >= 0 && l[idx.status]) || 'A Fazer',
                        progress: (idx.progresso >= 0 && parseInt(l[idx.progresso], 10)) || 0,
                        dueDate: (idx.dataLimite >= 0 && l[idx.dataLimite]) || '',
                        subtasks: [], links: [], attachments: []
                    });
                    importadas++;
                });

                triggerAutoSave();
                renderTasks();
                renderSubjectsList();
                renderDashboardStats();
                updateCharts();
                alert(`${importadas} tarefa(s) importada(s).`);
            });
        }

        function execDocCmd(cmd, value = null) {
            document.execCommand(cmd, false, value);
            const editor = document.getElementById('note-editor-content');
            if (editor) editor.focus();
            updateCurrentNote();
        }

        function clearFormattingCmd() {
            document.execCommand('removeFormat', false, null);
            document.execCommand('formatBlock', false, '<p>');
            const editor = document.getElementById('note-editor-content');
            if (editor) editor.focus();
            updateCurrentNote();
        }

        // Verdadeiro se "possivelFilhoId" e a propria nota "ancestralId" ou
        // descende dela - usado para recusar um drop que criaria um ciclo
        // (arrastar uma nota pra dentro da propria subnota).
        function _notaEhDescendente(ancestralId, possivelFilhoId) {
            let atual = appData.notes.find(n => n.id === possivelFilhoId);
            while (atual && atual.parentId) {
                if (atual.parentId === ancestralId) return true;
                atual = appData.notes.find(n => n.id === atual.parentId);
            }
            return false;
        }

        function _renderNoteNode(n, depth) {
            const isSelected = appData.selectedNoteIds.includes(n.id);
            const filhos = appData.notes.filter(c => c.parentId === n.id);
            const indent = depth * 16;
            return `
                <div draggable="true"
                     ondragstart="event.stopPropagation(); event.dataTransfer.setData('text/plain', '${n.id}')"
                     ondragover="event.preventDefault(); event.stopPropagation();"
                     ondrop="event.preventDefault(); event.stopPropagation(); moverNotaParaPai(event.dataTransfer.getData('text/plain'), '${n.id}')"
                     class="p-2 rounded hover:bg-notion-hover cursor-pointer transition flex items-center space-x-2 ${currentActiveNoteId === n.id ? 'bg-notion-hover font-semibold' : ''}"
                     style="margin-left: ${indent}px" onclick="openNoteInTab('${n.id}')">
                    <i class="fa-solid fa-grip-vertical text-gray-300 text-[10px] cursor-grab" title="Arraste para tornar subnota de outra"></i>
                    <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectNote('${n.id}', this.checked)" onclick="event.stopPropagation()" class="rounded accent-text-color focus:ring-0 cursor-pointer">
                    <div class="truncate flex-1">
                        <div class="text-xs text-gray-800 truncate">${escapeHtml(n.title) || 'Sem Título'}</div>
                        <div class="text-[10px] text-notion-muted truncate mt-0.5">${escapeHtml(n.subject)}</div>
                    </div>
                    <button type="button" onclick="event.stopPropagation(); createNewNote('${n.id}')" class="text-gray-400 hover:text-indigo-600 shrink-0" title="Nova subnota"><i class="fa-solid fa-plus text-[10px]"></i></button>
                </div>
                ${filhos.map(c => _renderNoteNode(c, depth + 1)).join('')}
            `;
        }

        function moverNotaParaPai(notaId, novoPaiId) {
            if (!notaId || notaId === novoPaiId) return;
            const nota = appData.notes.find(n => n.id === notaId);
            if (!nota) return;
            // Recusa criar ciclo: nota nao pode virar filha da propria subnota.
            if (_notaEhDescendente(notaId, novoPaiId)) return;
            nota.parentId = novoPaiId;
            triggerAutoSave();
            renderNotesSidebar();
        }

        function moverNotaParaRaiz(notaId) {
            const nota = appData.notes.find(n => n.id === notaId);
            if (!nota) return;
            nota.parentId = null;
            triggerAutoSave();
            renderNotesSidebar();
        }

        function renderNotesSidebar() {
            const container = document.getElementById('notes-sidebar-list');
            const bulkBtn = document.getElementById('btn-bulk-delete-notes');

            if (!container) return;

            if (bulkBtn) {
                if (appData.selectedNoteIds.length > 0) bulkBtn.classList.remove('hidden');
                else bulkBtn.classList.add('hidden');
            }

            const raizes = appData.notes.filter(n => !n.parentId);
            // Soltar sobre o fundo da lista (fora de qualquer nota) volta a
            // nota arrastada pra raiz - sem isso nao haveria como "desfazer"
            // uma subnota de volta a nota principal.
            container.setAttribute('ondragover', 'event.preventDefault();');
            container.setAttribute('ondrop', 'moverNotaParaRaiz(event.dataTransfer.getData("text/plain"))');
            container.innerHTML = raizes.map(n => _renderNoteNode(n, 0)).join('');
        }

        /* ------------------------------------------------------------------
           ABAS DE CADERNOS (estilo Notion / Google Docs)
           ------------------------------------------------------------------
           Antes, clicar numa nota na barra lateral SUBSTITUIA o conteudo do
           unico editor existente - abrir o caderno Y fechava o X. Agora
           appData.openNoteTabs guarda a lista de notas abertas ao mesmo
           tempo; openNoteInTab() adiciona/ativa uma aba sem descartar as
           outras, e closeNoteTab() fecha so a aba clicada. */
        function renderNoteTabs() {
            const bar = document.getElementById('notes-tab-bar');
            if (!bar) return;

            if (!appData.openNoteTabs || appData.openNoteTabs.length === 0) {
                bar.innerHTML = '';
                return;
            }

            bar.innerHTML = appData.openNoteTabs.map(id => {
                const note = appData.notes.find(n => n.id === id);
                if (!note) return '';
                const ativo = id === currentActiveNoteId;
                return `
                    <div class="group flex items-center gap-2 px-3 py-1.5 rounded-t-md border border-b-0 cursor-pointer text-xs max-w-[180px] transition ${ativo ? 'bg-white border-notion-border font-semibold text-gray-900' : 'bg-notion-sidebar border-transparent text-notion-muted hover:bg-notion-hover'}"
                         onclick="openNoteInTab('${note.id}')" title="${escapeHtml(note.title) || 'Sem Título'}">
                        <i class="fa-regular fa-file-lines text-[11px] ${ativo ? 'accent-icon-color' : ''}"></i>
                        <span class="truncate">${escapeHtml(note.title) || 'Sem Título'}</span>
                        <button type="button" onclick="closeNoteTab('${note.id}', event)" class="opacity-50 hover:opacity-100 hover:text-rose-600 shrink-0" title="Fechar aba">
                            <i class="fa-solid fa-xmark text-[11px]"></i>
                        </button>
                    </div>
                `;
            }).join('');
        }

        function openNoteInTab(id) {
            if (!appData.notes.some(n => n.id === id)) return;
            if (!appData.openNoteTabs) appData.openNoteTabs = [];
            if (!appData.openNoteTabs.includes(id)) appData.openNoteTabs.push(id);
            appData.activeNoteTabId = id;
            triggerAutoSave();
            openNote(id);
        }

        function closeNoteTab(id, event) {
            if (event) event.stopPropagation();
            appData.openNoteTabs = (appData.openNoteTabs || []).filter(x => x !== id);

            if (currentActiveNoteId === id) {
                const proximaId = appData.openNoteTabs[appData.openNoteTabs.length - 1] || null;
                if (proximaId) {
                    appData.activeNoteTabId = proximaId;
                    openNote(proximaId);
                } else {
                    currentActiveNoteId = null;
                    appData.activeNoteTabId = null;
                    document.getElementById('note-editor-title').value = '';
                    document.getElementById('note-editor-content').innerHTML = '';
                    renderNotesSidebar();
                    renderNoteTabs();
                }
            } else {
                renderNoteTabs();
            }
            triggerAutoSave();
        }

        function toggleSelectNote(id, checked) {
            if (checked) {
                if (!appData.selectedNoteIds.includes(id)) appData.selectedNoteIds.push(id);
            } else {
                appData.selectedNoteIds = appData.selectedNoteIds.filter(x => x !== id);
            }
            renderNotesSidebar();
        }

        function deleteSelectedNotes() {
            const count = appData.selectedNoteIds.length;
            if (count === 0) return;
            if (confirm(`Excluir as ${count} notas selecionadas?`)) {
                const idsExcluidos = appData.selectedNoteIds;
                appData.notes = appData.notes.filter(n => !idsExcluidos.includes(n.id));
                appData.selectedNoteIds = [];
                appData.openNoteTabs = (appData.openNoteTabs || []).filter(id => !idsExcluidos.includes(id));
                triggerAutoSave();
                renderNotesSidebar();
                if (idsExcluidos.includes(currentActiveNoteId)) {
                    const proximaId = appData.openNoteTabs[appData.openNoteTabs.length - 1] || (appData.notes[0] && appData.notes[0].id);
                    if (proximaId) openNoteInTab(proximaId);
                    else {
                        currentActiveNoteId = null;
                        appData.activeNoteTabId = null;
                        document.getElementById('note-editor-title').value = '';
                        document.getElementById('note-editor-content').innerHTML = '';
                        renderNoteTabs();
                    }
                } else {
                    renderNoteTabs();
                }
            }
        }

        function createNewNote(parentId = null) {
            const newNote = {
                id: 'n-' + Date.now(),
                title: '',
                subject: appData.subjects[0] || 'Geral',
                content: '',
                resources: [],
                linkedTaskIds: [],
                linkedPostitIds: [],
                parentId: parentId || null
            };
            appData.notes.unshift(newNote);
            triggerAutoSave();
            renderNotesSidebar();
            openNoteInTab(newNote.id);
        }

        function openNote(id) {
            currentActiveNoteId = id;
            const note = appData.notes.find(n => n.id === id);
            if (!note) return;

            renderNotesSidebar();
            renderNoteTabs();
            document.getElementById('note-editor-title').value = note.title || '';
            // Sanitiza tambem na LEITURA: notas criadas em versoes anteriores,
            // ou sincronizadas de outra maquina, podem ja conter HTML perigoso.
            document.getElementById('note-editor-content').innerHTML =
                sanitizarHtml(note.content || '');
            instalarSanitizacaoDoEditor();

            const subjectSelect = document.getElementById('note-subject-select');
            if (subjectSelect) {
                subjectSelect.innerHTML = appData.subjects.map(s => `<option value="${escapeHtml(s)}" ${note.subject === s ? 'selected' : ''}>${escapeHtml(s)}</option>`).join('');
            }

            renderNoteResources(note);
            renderNoteLinkedItems(note);
        }

        function updateNoteSubject(val) {
            if (!currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (note) {
                note.subject = val;
                triggerAutoSave();
                renderNotesSidebar();
            }
        }

        function updateCurrentNote() {
            if (!currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (note) {
                note.title = document.getElementById('note-editor-title').value;
                note.content = sanitizarHtml(
                    document.getElementById('note-editor-content').innerHTML);
                triggerAutoSave();
                renderNotesSidebar();
                renderNoteTabs();
            }
        }

        function deleteCurrentNote() {
            if (!currentActiveNoteId) return;
            if (confirm('Excluir esta anotação?')) {
                const idExcluido = currentActiveNoteId;
                appData.notes = appData.notes.filter(n => n.id !== idExcluido);
                appData.openNoteTabs = (appData.openNoteTabs || []).filter(id => id !== idExcluido);
                triggerAutoSave();
                renderNotesSidebar();
                const proximaId = appData.openNoteTabs[appData.openNoteTabs.length - 1] || (appData.notes[0] && appData.notes[0].id);
                if (proximaId) {
                    openNoteInTab(proximaId);
                } else {
                    currentActiveNoteId = null;
                    appData.activeNoteTabId = null;
                    document.getElementById('note-editor-title').value = '';
                    document.getElementById('note-editor-content').innerHTML = '';
                    renderNoteTabs();
                }
            }
        }

        function addLinkToNote() {
            if (!currentActiveNoteId) return;
            const url = prompt('Cole a URL (https://...):');
            if (url && url.trim()) {
                const note = appData.notes.find(n => n.id === currentActiveNoteId);
                if (note) {
                    if (!note.resources) note.resources = [];
                    note.resources.push({ type: 'link', url: url.trim() });
                    triggerAutoSave();
                    renderNoteResources(note);
                }
            }
        }

        function attachFileToNote() {
            if (!currentActiveNoteId) return;
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.pick_file_native().then(res => {
                    if (res && res.status === 'success') {
                        const note = appData.notes.find(n => n.id === currentActiveNoteId);
                        if (note) {
                            if (!note.resources) note.resources = [];
                            note.resources.push({ type: 'file', url: res.path, name: res.name });
                            triggerAutoSave();
                            renderNoteResources(note);
                        }
                    }
                });
            }
        }

        function removeNoteResource(idx) {
            if (!currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (note && note.resources) {
                note.resources.splice(idx, 1);
                triggerAutoSave();
                renderNoteResources(note);
            }
        }

        function renderNoteResources(note) {
            const container = document.getElementById('note-resources-list');
            if (!container) return;
            if (!note.resources || note.resources.length === 0) {
                container.innerHTML = `<span class="nt-empty">Nenhum link ou arquivo anexado.</span>`;
                ntAtualizarContadores(note);
                return;
            }
            container.innerHTML = note.resources.map((r, idx) => `
                <div class="nt-row">
                    <button type="button" onclick="triggerOpenFile('${escapeJsString(r.url)}')" class="hover:underline cursor-pointer ${r.type === 'file' ? 'text-rose-600' : 'accent-text-color'}" title="${escapeHtml(r.name || r.url)}">
                        <i class="fa-solid ${r.type === 'file' ? 'fa-file-pdf' : 'fa-link'} text-[10px]"></i><span class="nt-t">${escapeHtml(r.name || r.url)}</span>
                    </button>
                    <button type="button" onclick="removeNoteResource(${idx})" class="nt-x"><i class="fa-solid fa-xmark"></i></button>
                </div>
            `).join('');
            ntAtualizarContadores(note);
        }

        /* ------------------------------------------------------------------
           TAREFAS & POST-ITS VINCULADOS A UMA NOTA (referencia por id, o
           item continua existindo e editavel na sua propria tela). */
        function renderNoteLinkOptions(note) {
            const selTask = document.getElementById('note-link-task-select');
            const selPostit = document.getElementById('note-link-postit-select');
            if (selTask) {
                const jaVinculadas = note.linkedTaskIds || [];
                selTask.innerHTML = `<option value="">+ Vincular Tarefa</option>` +
                    appData.tasks.filter(t => !jaVinculadas.includes(t.id))
                        .map(t => `<option value="${t.id}">${escapeHtml(t.title)}</option>`).join('');
            }
            if (selPostit) {
                const jaVinculados = note.linkedPostitIds || [];
                selPostit.innerHTML = `<option value="">+ Vincular Post-it</option>` +
                    appData.postits.filter(p => !jaVinculados.includes(p.id))
                        .map(p => `<option value="${p.id}">${escapeHtml((p.text || 'Post-it sem texto').slice(0, 30))}</option>`).join('');
            }
        }

        function renderNoteLinkedItems(note) {
            const cTasks = document.getElementById('note-linked-tasks-list');
            const cPost = document.getElementById('note-linked-postits-list');
            if (!cTasks || !cPost) return;

            const tarefas = (note.linkedTaskIds || []).map(id => appData.tasks.find(x => x.id === id)).filter(Boolean);
            cTasks.innerHTML = tarefas.map(t => {
                return `
                    <div class="nt-row">
                        <button type="button" onclick="alternarTarefaConcluida('${t.id}')" class="nt-chk ${t.status === 'Concluído' ? 'on' : ''}" title="${t.status === 'Concluído' ? 'Reabrir tarefa' : 'Marcar como concluída'}"><i class="fa-solid fa-check"></i></button>
                        <button type="button" onclick="editTask('${t.id}')" class="hover:underline cursor-pointer text-gray-800" title="${escapeHtml(t.title)} — ${escapeHtml(t.status || '')}">
                            <span class="nt-t ${t.status === 'Concluído' ? 'line-through text-gray-400' : ''}">${escapeHtml(t.title)}</span>
                        </button>
                        <button type="button" onclick="desvincularTarefaDaNota('${t.id}')" class="nt-x" title="Desvincular"><i class="fa-solid fa-xmark"></i></button>
                    </div>`;
            }).join('') || `<span class="nt-empty">Nenhuma tarefa nesta nota.</span>`;
            const cnt = document.getElementById('note-tasks-count');
            if (cnt) cnt.textContent = tarefas.length;

            const postits = (note.linkedPostitIds || []).map(id => appData.postits.find(x => x.id === id)).filter(Boolean);
            ntAtualizarContadores(note);
            cPost.innerHTML = postits.map(p => `
                    <div class="nt-row">
                        <button type="button" onclick="switchTab('postits')" class="hover:underline cursor-pointer text-rose-600">
                            <i class="fa-solid fa-note-sticky text-[10px]"></i><span class="nt-t">${escapeHtml((p.text || 'Post-it sem texto').slice(0, 40))}</span>
                        </button>
                        <button type="button" onclick="desvincularPostitDaNota('${p.id}')" class="nt-x" title="Desvincular"><i class="fa-solid fa-xmark"></i></button>
                    </div>`).join('') || `<span class="nt-empty">Nenhum post-it vinculado.</span>`;
            const cnt2 = document.getElementById('note-postits-count');
            if (cnt2) cnt2.textContent = postits.length;

            renderNoteLinkOptions(note);
        }

        /* ---------------- Painel Spotify (player proprio: Web API + Web Playback SDK) ---------------- */
        const SP_ORIGEM = 'http://127.0.0.1:8899';
        let spAberto = false, spW = 380, spInfo = null, spMe = null, spDevice = '', spState = null;
        let spIniciado = false, spTab = 'playlists', spSeeking = false, spBuscaT = 0, spTimer = null, spPollConn = null;
        let spPlOffset = 0, spPlAtual = null, spShufflePref = null;
        try { spW = Number(localStorage.getItem('nx-sp-w')) || 380; } catch (e) { }

        function spAplicarLargura(w) {
            spW = Math.max(320, Math.min(700, Math.min(w, window.innerWidth - 640)));
            const p = document.getElementById('sp-panel');
            if (p) p.style.width = spW + 'px';
        }
        spAplicarLargura(spW);
        function spResizeIniciar(e) {
            e.preventDefault();
            const h = e.currentTarget, x0 = e.clientX, w0 = spW;
            h.setPointerCapture(e.pointerId);
            h.classList.add('drag');
            const mover = ev => spAplicarLargura(w0 + (x0 - ev.clientX));
            const soltar = () => {
                h.classList.remove('drag');
                h.removeEventListener('pointermove', mover);
                h.removeEventListener('pointerup', soltar);
                h.removeEventListener('pointercancel', soltar);
                try { localStorage.setItem('nx-sp-w', String(spW)); } catch (err) { }
            };
            h.addEventListener('pointermove', mover);
            h.addEventListener('pointerup', soltar);
            h.addEventListener('pointercancel', soltar);
        }

        function spApi() { return window.pywebview && window.pywebview.api; }
        async function spChamar(metodo, caminho, corpo) {
            const a = spApi();
            if (!a || !a.spotify_api) return { status: 0, message: 'Disponível só no aplicativo desktop.' };
            try { return await a.spotify_api(metodo, caminho, corpo === undefined ? null : corpo); }
            catch (e) { return { status: 0, message: String(e) }; }
        }
        const spOk = r => r && r.status >= 200 && r.status < 300;
        const spUriOk = u => /^spotify:(track|album|playlist|episode|artist):[A-Za-z0-9]{10,30}$/.test(String(u || ''));
        const spIdOk = i => /^[A-Za-z0-9]{10,30}$/.test(String(i || ''));
        const spImg = u => /^https:\/\/[a-z0-9.-]+\.(scdn\.co|spotifycdn\.com)\//i.test(String(u || '')) ? u : '';
        function spFmt(ms) { const s = Math.max(0, Math.floor((ms || 0) / 1000)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
        const spBody = () => document.getElementById('sp-body');

        function spotifyToggle() {
            spAberto = !spAberto;
            document.getElementById('sp-panel').classList.toggle('hidden', !spAberto);
            const b = document.getElementById('sp-rail-btn');
            if (b) b.classList.toggle('on', spAberto);
            if (spAberto && !spIniciado) { spIniciado = true; spCarregar(); }
        }

        function spCabecalho() {
            const sub = document.getElementById('sp-sub');
            const sair = document.getElementById('sp-btn-sair');
            const conectado = !!(spInfo && spInfo.connected);
            if (sub) sub.textContent = conectado && spMe ? (spMe.display_name || spMe.id || '') : '';
            if (sair) sair.classList.toggle('hidden', !(spInfo && (spInfo.connected || spInfo.clientId)));
            document.getElementById('sp-now').classList.toggle('hidden', !conectado);
        }

        async function spCarregar() {
            const a = spApi();
            if (!a || !a.spotify_info) { spBody().innerHTML = '<div class="sp-card">O Spotify só está disponível no aplicativo desktop.</div>'; return; }
            try { spInfo = await a.spotify_info(); } catch (e) { spBody().innerHTML = '<div class="sp-card sp-err">Falha ao iniciar: ' + escapeHtml(String(e)) + '</div>'; return; }
            spCabecalho();
            if (!spInfo.clientId) return spRenderSetup();
            if (!spInfo.connected) return spRenderConectar();
            spRenderPrincipal();
            spIniciarPlayer();
            if (!spMe) {
                const r = await spChamar('GET', '/v1/me');
                if (spOk(r)) { spMe = r.data; spCabecalho(); spListarPlaylists(); }
                else if (r.status === 403) spAviso('O Spotify bloqueou o app (403). No modo de desenvolvimento, o dono do app precisa ter Premium e a sua conta deve estar liberada em User Management no dashboard.');
            }
        }

        function spRenderSetup(erro) {
            spBody().innerHTML = `
                <div class="sp-card">
                    <h3>Conectar o Spotify (só uma vez)</h3>
                    <ol class="sp-steps">
                        <li>Abra o <a onclick="spotifyUrl('https://developer.spotify.com/dashboard')">Spotify for Developers</a> e crie um app.</li>
                        <li>Em <b>Redirect URIs</b>, adicione exatamente:<code class="sp-code">${escapeHtml(spInfo ? spInfo.redirect : '')}</code></li>
                        <li>Marque <b>Web API</b> e <b>Web Playback SDK</b> e salve.</li>
                        <li>Em <b>User Management</b>, adicione o e-mail da sua conta Spotify.</li>
                        <li>Copie o <b>Client ID</b> e cole abaixo.</li>
                    </ol>
                    <input id="sp-cid" class="sp-input" placeholder="Client ID (32 caracteres)" spellcheck="false" autocomplete="off">
                    <button type="button" class="sp-btn" onclick="spSalvarClient()">Salvar e continuar</button>
                    ${erro ? `<div class="sp-err">${escapeHtml(erro)}</div>` : ''}
                    <div class="sp-note">Exige conta <b>Premium</b> (regra do Spotify para tocar dentro de outros aplicativos). Seu login fica só neste computador.</div>
                </div>`;
        }
        async function spSalvarClient() {
            const v = (document.getElementById('sp-cid') || {}).value || '';
            const r = await spApi().spotify_salvar_client(v);
            if (r && r.status === 'success') return spCarregar();
            spRenderSetup(r && r.message);
        }

        function spRenderConectar(msg, url) {
            spBody().innerHTML = `
                <div class="sp-card">
                    <h3>Entrar no Spotify</h3>
                    <div class="sp-note" style="margin-top:0">${msg ? escapeHtml(msg) : 'O login abre no seu navegador padrão. Depois de autorizar, volte ao Cogni.'}</div>
                    ${url ? `<div class="sp-note">Se o navegador não abriu, <a onclick="spotifyUrl('${escapeHtml(escapeJsString(url))}')">clique aqui</a>.</div>` : ''}
                    <button type="button" class="sp-btn" onclick="spConectar()">Conectar ao Spotify</button>
                    ${spInfo && spInfo.erro ? `<div class="sp-err">${escapeHtml(spInfo.erro)}</div>` : ''}
                    <button type="button" class="sp-btn ghost" onclick="spTrocarClient()">Trocar Client ID</button>
                </div>`;
        }
        function spTrocarClient() { spInfo.clientId = false; spRenderSetup(); }
        async function spConectar() {
            const r = await spApi().spotify_conectar();
            if (!r || r.status !== 'success') { spRenderConectar((r && r.message) || 'Falha ao iniciar o login.'); return; }
            spRenderConectar('Aguardando você autorizar no navegador...', r.url);
            let n = 0;
            clearInterval(spPollConn);
            spPollConn = setInterval(async () => {
                n++;
                const i = await spApi().spotify_info();
                if (i && i.connected) { clearInterval(spPollConn); spPollConn = null; spCarregar(); }
                else if (n > 160) { clearInterval(spPollConn); spPollConn = null; spRenderConectar('Tempo esgotado. Tente conectar de novo.'); }
            }, 1500);
        }
        async function spDesconectar() {
            if (!confirm('Desconectar a conta do Spotify neste computador?')) return;
            clearInterval(spTimer); spTimer = null;
            const f = document.getElementById('sp-frame');
            if (f) { f.src = 'about:blank'; f.dataset.on = ''; }
            await spApi().spotify_desconectar();
            spMe = null; spDevice = ''; spState = null; spPlAtual = null;
            spCarregar();
        }
        function spotifyUrl(u) {
            const a = spApi();
            if (a && a.spotify_abrir) a.spotify_abrir(u);
        }

        function spRenderPrincipal() {
            spBody().innerHTML = `
                <div class="sp-tabs">
                    <button type="button" id="sp-tab-playlists" onclick="spAba('playlists')">Playlists</button>
                    <button type="button" id="sp-tab-buscar" onclick="spAba('buscar')">Buscar</button>
                </div>
                <div id="sp-aviso" class="sp-aviso"></div>
                <div id="sp-lista" class="sp-lista"></div>`;
            spAba(spTab);
        }
        function spAviso(msg, ok) { const el = document.getElementById('sp-aviso'); if (!el) return; el.textContent = msg || ''; el.classList.toggle('ok', !!ok); }
        function spAba(t) {
            spTab = t;
            ['playlists', 'buscar'].forEach(k => { const b = document.getElementById('sp-tab-' + k); if (b) b.classList.toggle('on', k === t); });
            if (t === 'playlists') { spPlAtual = null; spListarPlaylists(); }
            else spRenderBusca();
        }

        function spLinha(o) {
            // o: {act, uri, ctx, id, name, img, titulo, sub, play, atual}
            const d = ['act', 'uri', 'ctx', 'id', 'name'].map(k => o[k] ? ` data-${k}="${escapeHtml(o[k])}"` : '').join('');
            const img = spImg(o.img);
            return `<div class="sp-row ${o.atual ? 'tocando' : ''}"${d} onclick="spClick(this)">
                ${img ? `<img class="sp-cover" src="${escapeHtml(img)}" alt="" referrerpolicy="no-referrer">` : '<div class="sp-cover"></div>'}
                <div class="sp-r-t"><div class="sp-r-n">${escapeHtml(o.titulo || '')}</div><div class="sp-r-s">${escapeHtml(o.sub || '')}</div></div>
                ${o.play ? `<button type="button" class="sp-r-b" title="Tocar" data-act="${escapeHtml(o.play.act)}" data-uri="${escapeHtml(o.play.uri)}" onclick="event.stopPropagation(); spClick(this)"><i class="fa-solid fa-play"></i></button>` : ''}
            </div>`;
        }
        function spClick(el) {
            const d = el.dataset;
            if (d.act === 'play-ctx' && spUriOk(d.uri)) spTocar({ context_uri: d.uri });
            else if (d.act === 'play-track' && spUriOk(d.uri)) spTocar({ uris: [d.uri] });
            else if (d.act === 'play-in' && spUriOk(d.uri) && spUriOk(d.ctx)) spTocar({ context_uri: d.ctx, offset: { uri: d.uri } });
            else if (d.act === 'open-pl' && spIdOk(d.id) && spUriOk(d.uri)) spAbrirPlaylist(d.id, d.name || 'Playlist', d.uri, 0);
        }

        async function spListarPlaylists() {
            const el = document.getElementById('sp-lista');
            if (!el || spTab !== 'playlists') return;
            el.innerHTML = '<div class="sp-note" style="padding:8px">Carregando playlists...</div>';
            const r = await spChamar('GET', '/v1/me/playlists?limit=50');
            if (!spOk(r)) { el.innerHTML = `<div class="sp-err" style="margin:8px">${escapeHtml(spMsgErro(r))}</div>`; return; }
            const itens = ((r.data && r.data.items) || []).filter(p => p && spIdOk(p.id) && spUriOk(p.uri));
            if (!itens.length) { el.innerHTML = '<div class="sp-note" style="padding:8px">Nenhuma playlist na sua biblioteca.</div>'; return; }
            el.innerHTML = itens.map(p => {
                const total = (p.items && p.items.total != null) ? p.items.total : (p.tracks && p.tracks.total);
                const meu = spMe && p.owner && p.owner.id === spMe.id;
                const listavel = meu || p.collaborative;
                return spLinha({
                    act: listavel ? 'open-pl' : 'play-ctx', uri: p.uri, id: p.id, name: p.name,
                    img: p.images && p.images[p.images.length > 1 ? 1 : 0] && (p.images[p.images.length > 1 ? 1 : 0].url),
                    titulo: p.name, sub: ((p.owner && p.owner.display_name) || '') + (total != null ? ' · ' + total + ' faixas' : ''),
                    play: listavel ? { act: 'play-ctx', uri: p.uri } : null,
                    atual: spState && spState.context === p.uri
                });
            }).join('');
        }

        async function spAbrirPlaylist(id, nome, uri, offset) {
            const el = document.getElementById('sp-lista');
            if (!el) return;
            spPlAtual = { id, nome, uri };
            if (!offset) el.innerHTML = `<div class="sp-voltar"><button type="button" onclick="spAba('playlists')" title="Voltar"><i class="fa-solid fa-arrow-left"></i></button><span>${escapeHtml(nome)}</span><button type="button" class="sp-smart" onclick="spSmartShuffle('${id}', '${escapeHtml(escapeJsString(nome))}', '${uri}')" title="Smart Shuffle (aproximado): embaralha sem repetir artista em sequência e mistura músicas novas dos mesmos artistas"><i class="fa-solid fa-wand-magic-sparkles"></i></button><button type="button" onclick="spTocar({ context_uri: '${uri}' })" title="Tocar tudo"><i class="fa-solid fa-play"></i></button></div><div id="sp-faixas"><div class="sp-note" style="padding:8px">Carregando faixas...</div></div>`;
            const r = await spChamar('GET', `/v1/playlists/${id}/items?limit=100&offset=${offset}`);
            const box = document.getElementById('sp-faixas');
            if (!box) return;
            if (!spOk(r)) { box.innerHTML = `<div class="sp-err" style="margin:8px">${escapeHtml(spMsgErro(r))}</div>`; return; }
            const data = r.data || {};
            const faixas = (data.items || []).map(it => it && (it.item || it.track)).filter(t => t && typeof t === 'object' && spUriOk(t.uri));
            const html = faixas.map(t => spLinha({
                act: 'play-in', uri: t.uri, ctx: uri, titulo: t.name,
                sub: (t.artists || []).map(a => a.name).join(', '),
                img: t.album && t.album.images && t.album.images[t.album.images.length > 1 ? 1 : 0] && t.album.images[t.album.images.length > 1 ? 1 : 0].url,
                atual: spState && spState.track && spState.track.uri === t.uri
            })).join('');
            if (!offset) box.innerHTML = html || '<div class="sp-note" style="padding:8px">Playlist vazia.</div>';
            else { const mais = document.getElementById('sp-mais'); if (mais) mais.remove(); box.insertAdjacentHTML('beforeend', html); }
            if (data.next) box.insertAdjacentHTML('beforeend', `<button type="button" id="sp-mais" class="sp-btn ghost" onclick="spAbrirPlaylist('${id}', '${escapeHtml(escapeJsString(nome))}', '${uri}', ${offset + 100})">Carregar mais</button>`);
        }

        function spRenderBusca() {
            const el = document.getElementById('sp-lista');
            if (!el) return;
            el.innerHTML = `<input id="sp-q" class="sp-input" placeholder="Buscar músicas, álbuns e playlists" spellcheck="false" autocomplete="off" oninput="spBuscaAgendar(this.value)"><div id="sp-res"></div>`;
            const q = document.getElementById('sp-q'); if (q) q.focus();
        }
        function spBuscaAgendar(v) { clearTimeout(spBuscaT); spBuscaT = setTimeout(() => spBuscar(v), 450); }
        async function spBuscar(v) {
            const box = document.getElementById('sp-res');
            if (!box) return;
            v = String(v || '').trim();
            if (v.length < 2) { box.innerHTML = ''; return; }
            box.innerHTML = '<div class="sp-note" style="padding:8px">Buscando...</div>';
            const r = await spChamar('GET', '/v1/search?q=' + encodeURIComponent(v) + '&type=track,album,playlist&limit=10');
            if (!spOk(r)) { box.innerHTML = `<div class="sp-err" style="margin:8px">${escapeHtml(spMsgErro(r))}</div>`; return; }
            const d = r.data || {};
            const img = o => o && o.images && o.images[o.images.length > 1 ? 1 : 0] && o.images[o.images.length > 1 ? 1 : 0].url;
            const tracks = ((d.tracks && d.tracks.items) || []).filter(t => t && spUriOk(t.uri));
            const albums = ((d.albums && d.albums.items) || []).filter(a => a && spUriOk(a.uri));
            const lists = ((d.playlists && d.playlists.items) || []).filter(p => p && spUriOk(p.uri));
            let h = '';
            if (tracks.length) h += '<div class="sp-sec">Músicas</div>' + tracks.map(t => spLinha({ act: 'play-track', uri: t.uri, titulo: t.name, sub: (t.artists || []).map(a => a.name).join(', '), img: img(t.album), atual: spState && spState.track && spState.track.uri === t.uri })).join('');
            if (albums.length) h += '<div class="sp-sec">Álbuns</div>' + albums.map(a => spLinha({ act: 'play-ctx', uri: a.uri, titulo: a.name, sub: (a.artists || []).map(x => x.name).join(', '), img: img(a) })).join('');
            if (lists.length) h += '<div class="sp-sec">Playlists</div>' + lists.map(p => spLinha({ act: 'play-ctx', uri: p.uri, titulo: p.name, sub: (p.owner && p.owner.display_name) || '', img: img(p) })).join('');
            box.innerHTML = h || '<div class="sp-note" style="padding:8px">Nada encontrado.</div>';
        }

        function spMsgErro(r) {
            if (!r) return 'Sem resposta do Spotify.';
            if (r.status === 401) return 'Sessão expirada. Desconecte e conecte de novo.';
            if (r.status === 403) return 'O Spotify recusou o acesso (403). Confira se o dono do app tem Premium e se a sua conta está em User Management.';
            if (r.status === 429) return 'Muitas requisições ao Spotify. Aguarde ' + (r.retry || 'alguns') + ' segundos.';
            if (r.status === 0) return r.message || 'Sem conexão com o Spotify.';
            return 'Erro do Spotify (' + r.status + ').' + ((r.data && r.data.error && r.data.error.message) ? ' ' + r.data.error.message : '');
        }

        /* ---- player (iframe invisivel com o Web Playback SDK) ---- */
        function spIniciarPlayer() {
            const f = document.getElementById('sp-frame');
            if (!f || f.dataset.on === '1' || !spInfo || !spInfo.playerUrl) return;
            f.dataset.on = '1';
            f.src = spInfo.playerUrl;
            if (!window.__spMsg) { window.__spMsg = 1; window.addEventListener('message', spMensagem); }
            if (!spTimer) spTimer = setInterval(spTick, 500);
        }
        function spCmd(cmd, v, ms) {
            const f = document.getElementById('sp-frame');
            if (!f || !f.contentWindow) return;
            f.contentWindow.postMessage({ nxsp: 1, cmd, v, ms }, SP_ORIGEM);
        }
        function spMensagem(e) {
            const f = document.getElementById('sp-frame');
            if (!f || e.source !== f.contentWindow || e.origin !== SP_ORIGEM) return;
            const m = e.data;
            if (!m || !m.nxsp) return;
            if (m.type === 'ready') { spDevice = String(m.device_id || ''); spAviso(''); }
            else if (m.type === 'not_ready') { spDevice = ''; }
            else if (m.type === 'state') { spState = m.state; spAtualizarNow(); }
            else if (m.type === 'error') {
                const k = m.kind;
                spAviso(k === 'account_error' ? 'Esta conta não é Premium: o player embutido do Spotify exige Premium.'
                    : k === 'authentication_error' ? 'Login do Spotify inválido ou expirado. Desconecte e conecte de novo.'
                    : k === 'initialization_error' ? 'Este navegador embutido não suporta o player do Spotify (DRM): ' + (m.msg || '')
                    : k === 'playback_error' ? 'Erro ao reproduzir: ' + (m.msg || '')
                    : (m.msg || 'Erro no player do Spotify.'));
            }
        }
        async function spTocar(corpo) {
            if (!spDevice) { spAviso('O player do Cogni ainda está iniciando. Tente de novo em instantes.'); spIniciarPlayer(); return; }
            spAviso('');
            spCmd('activate');
            // aplica a escolha de aleatorio ANTES de iniciar, para a playlist ja tocar embaralhada
            if (spShufflePref !== null) await spChamar('PUT', '/v1/me/player/shuffle?state=' + spShufflePref + '&device_id=' + encodeURIComponent(spDevice));
            const r = await spChamar('PUT', '/v1/me/player/play?device_id=' + encodeURIComponent(spDevice), corpo);
            if (!spOk(r)) {
                spAviso(r.status === 404 ? 'O Spotify não achou o player do Cogni. Feche e abra o painel para recriá-lo.'
                    : r.status === 403 ? 'O Spotify recusou a reprodução (conta sem Premium ou item indisponível).'
                    : spMsgErro(r));
            }
        }

        function spShuffleAtivo() { return spShufflePref !== null ? spShufflePref : !!(spState && spState.shuffle); }
        function spShuffleBotao() {
            const b = document.getElementById('sp-shuf');
            if (!b) return;
            const on = spShuffleAtivo();
            b.classList.toggle('on', on);
            b.title = on ? 'Aleatório: ligado' : 'Aleatório: desligado';
        }
        async function spShuffle() {
            const novo = !spShuffleAtivo();
            spShufflePref = novo;
            spShuffleBotao();
            if (!spDevice) return;      // sem player ainda: vale a partir da proxima musica que tocar
            const r = await spChamar('PUT', '/v1/me/player/shuffle?state=' + novo + '&device_id=' + encodeURIComponent(spDevice));
            if (!spOk(r) && r.status !== 404) { spShufflePref = !novo; spShuffleBotao(); spAviso(spMsgErro(r)); }
        }

        /* ---- Smart Shuffle (aproximado) ----
           O Smart Shuffle oficial do Spotify nao existe na API. Esta versao: (1) embaralha a
           playlist sem repetir o mesmo artista em sequencia e (2) intercala musicas NOVAS
           (fora da playlist) de artistas que voce ja tem nela. Toca ate 100 faixas por vez. */
        const SP_SMART_MAX = 100;
        function spEmbaralhar(a) { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }
        function spEspacarArtistas(faixas) {
            const resto = faixas.slice(), saida = [];
            while (resto.length) {
                const recentes = saida.slice(-3).map(t => t.artist);
                let i = resto.findIndex(t => !recentes.includes(t.artist));
                if (i < 0) i = 0;
                saida.push(resto.splice(i, 1)[0]);
            }
            return saida;
        }
        let spSmartOcupado = false;
        async function spSmartShuffle(id, nome, uri) {
            if (spSmartOcupado) return;
            if (!spDevice) { spAviso('O player ainda está iniciando. Tente de novo em instantes.'); spIniciarPlayer(); return; }
            spSmartOcupado = true;
            try {
                spAviso('Montando o Smart Shuffle...', true);
                const todas = [];
                for (let pg = 0, off = 0; pg < 4; pg++, off += 100) {
                    const r = await spChamar('GET', `/v1/playlists/${id}/items?limit=100&offset=${off}`);
                    if (!spOk(r)) { spAviso(spMsgErro(r)); return; }
                    ((r.data && r.data.items) || []).forEach(it => {
                        const t = it && (it.item || it.track);
                        if (t && typeof t === 'object' && String(t.uri).startsWith('spotify:track:') && spUriOk(t.uri)) {
                            const ids = (t.artists || []).map(a => a && a.id).filter(spIdOk);
                            todas.push({ uri: t.uri, artist: ids[0] || t.uri, ids });
                        }
                    });
                    if (!(r.data && r.data.next)) break;
                }
                if (todas.length < 3) { spAviso('A playlist precisa de pelo menos 3 músicas para o Smart Shuffle.'); return; }
                const base = spEspacarArtistas(spEmbaralhar(todas.slice()));

                // descobertas: uma faixa nova (fora da playlist) de artistas que ja estao nela
                const conhecidas = new Set(todas.map(t => t.uri));
                const artistas = spEmbaralhar([...new Set(todas.flatMap(t => t.ids))]);
                const alvo = Math.min(10, Math.max(2, Math.round(base.length / 8)));
                const novas = [];
                for (const aid of artistas) {
                    if (novas.length >= alvo) break;
                    const ra = await spChamar('GET', `/v1/artists/${aid}/albums?include_groups=album,single&limit=10`);
                    if (!spOk(ra)) { if (ra.status === 429) break; continue; }
                    const albs = ((ra.data && ra.data.items) || []).filter(a => a && spIdOk(a.id));
                    if (!albs.length) continue;
                    const alb = albs[Math.floor(Math.random() * albs.length)];
                    const rt = await spChamar('GET', `/v1/albums/${alb.id}/tracks?limit=20`);
                    if (!spOk(rt)) continue;
                    const cand = ((rt.data && rt.data.items) || []).filter(t => t && String(t.uri).startsWith('spotify:track:') && spUriOk(t.uri) && !conhecidas.has(t.uri) && !novas.includes(t.uri));
                    if (cand.length) novas.push(cand[Math.floor(Math.random() * cand.length)].uri);
                }

                // intercala: 1 descoberta a cada ~4 faixas (a primeira so a partir da 4a)
                const fila = base.slice(0, Math.max(1, SP_SMART_MAX - novas.length)).map(t => t.uri);
                novas.forEach((u, i) => fila.splice(Math.min(fila.length, 3 + i * 5), 0, u));

                // desliga o shuffle do Spotify: a ordem montada aqui e que vale
                spShufflePref = false; spShuffleBotao();
                await spChamar('PUT', '/v1/me/player/shuffle?state=false&device_id=' + encodeURIComponent(spDevice));
                spCmd('activate');
                const rp = await spChamar('PUT', '/v1/me/player/play?device_id=' + encodeURIComponent(spDevice), { uris: fila });
                if (spOk(rp)) spAviso(`Smart Shuffle: ${fila.length - novas.length} músicas da playlist + ${novas.length} descobertas.`, true);
                else spAviso(spMsgErro(rp));
            } finally { spSmartOcupado = false; }
        }

        function spAtualizarNow() {
            spShuffleBotao();
            const t = spState && spState.track;
            const img = document.getElementById('sp-np-img');
            const src = t ? spImg(t.image) : '';
            if (img) { if (src) { img.src = src; img.style.visibility = 'visible'; } else { img.removeAttribute('src'); img.style.visibility = 'hidden'; } }
            document.getElementById('sp-np-title').textContent = t ? t.name : 'Nada tocando';
            document.getElementById('sp-np-art').textContent = t ? (t.artists || []).join(', ') : '';
            const pp = document.querySelector('#sp-pp i');
            if (pp) pp.className = 'fa-solid ' + (spState && !spState.paused ? 'fa-pause' : 'fa-play');
            document.getElementById('sp-t1').textContent = spFmt(spState && spState.duration);
            spTick();
        }
        function spTick() {
            if (!spState || !spState.track || spSeeking) return;
            let pos = spState.position || 0;
            if (!spState.paused && spState.ts) pos += Math.max(0, Date.now() - spState.ts);
            pos = Math.min(pos, spState.duration || pos);
            const r = document.getElementById('sp-seek');
            if (r) r.value = spState.duration ? Math.round(pos / spState.duration * 1000) : 0;
            document.getElementById('sp-t0').textContent = spFmt(pos);
        }
        function spSeekInput(v) {
            spSeeking = true;
            if (spState && spState.duration) document.getElementById('sp-t0').textContent = spFmt(v / 1000 * spState.duration);
        }
        function spSeekFim(v) {
            if (spState && spState.duration) spCmd('seek', undefined, Math.round(v / 1000 * spState.duration));
            setTimeout(() => { spSeeking = false; }, 400);
        }


        /* ---- Painel lateral da nota (estilo barra de apps): so abre um por vez ---- */
        let ntPainelAtivo = null;
        function ntAbrirPainel(nome) {
            ntPainelAtivo = (ntPainelAtivo === nome) ? null : nome;
            const painel = document.getElementById('nt-panel');
            if (!painel) return;
            painel.classList.toggle('open', !!ntPainelAtivo);
            painel.querySelectorAll('.nt-card').forEach(c => c.classList.toggle('on', c.dataset.p === ntPainelAtivo));
            document.querySelectorAll('.nt-rb').forEach(b => b.classList.toggle('on', b.dataset.b === ntPainelAtivo));
        }
        function ntAtualizarContadores(note) {
            const set = (id, n) => { const el = document.getElementById(id); if (!el) return; el.textContent = n; el.classList.toggle('show', n > 0); };
            set('rb-anexos', (note.resources || []).length);
            set('rb-tarefas', (note.linkedTaskIds || []).filter(i => appData.tasks.some(t => t.id === i)).length);
            set('rb-postits', (note.linkedPostitIds || []).filter(i => appData.postits.some(p => p.id === i)).length);
        }

        /* ---- Layout responsivo: menu principal e lista de notas recolhem em janela estreita ---- */
        let _sbPref = null, _nlPref = null;
        try { _sbPref = localStorage.getItem('cg-sb'); _nlPref = localStorage.getItem('cg-nl'); } catch (e) { }
        function aplicarLayoutResponsivo() {
            const w = window.innerWidth;
            const sb = document.getElementById('cg-sidebar');
            const nl = document.getElementById('notes-list-col');
            if (sb) {
                const rec = _sbPref !== null ? _sbPref === '1' : w < 1150;
                sb.classList.toggle('sb-collapsed', rec);
                const b = document.getElementById('sb-toggle');
                if (b) b.title = rec ? 'Expandir menu' : 'Recolher menu';
            }
            if (nl) {
                const rec = _nlPref !== null ? _nlPref === '1' : w < 1250;
                nl.classList.toggle('nl-off', rec);
                const b = document.getElementById('nl-toggle');
                if (b) b.title = rec ? 'Mostrar lista de notas' : 'Ocultar lista de notas';
            }
        }
        function toggleSidebar() {
            const sb = document.getElementById('cg-sidebar');
            _sbPref = sb.classList.contains('sb-collapsed') ? '0' : '1';
            try { localStorage.setItem('cg-sb', _sbPref); } catch (e) { }
            aplicarLayoutResponsivo();
        }
        function toggleListaNotas() {
            const nl = document.getElementById('notes-list-col');
            _nlPref = nl.classList.contains('nl-off') ? '0' : '1';
            try { localStorage.setItem('cg-nl', _nlPref); } catch (e) { }
            aplicarLayoutResponsivo();
        }
        window.addEventListener('resize', aplicarLayoutResponsivo);
        document.addEventListener('DOMContentLoaded', aplicarLayoutResponsivo);
        try { aplicarLayoutResponsivo(); } catch (e) { }

        function alternarTarefaConcluida(id) {
            const t = appData.tasks.find(x => x.id === id);
            if (!t) return;
            updateTaskStatus(id, t.status === 'Concluído' ? 'A Fazer' : 'Concluído');
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (note) renderNoteLinkedItems(note);
        }

        // Cria uma tarefa a partir do texto selecionado no editor da nota e
        // ja a vincula a ela (task.noteId <-> note.linkedTaskIds).
        function criarTarefaDaSelecao() {
            if (!currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (!note) return;
            const ed = document.getElementById('note-editor-content');
            const sel = window.getSelection ? window.getSelection() : null;
            let texto = '';
            if (sel && sel.rangeCount && ed && ed.contains(sel.anchorNode) && ed.contains(sel.focusNode)) {
                texto = sel.toString();
            }
            texto = texto.replace(/\s+/g, ' ').trim();
            if (!texto) {
                texto = (prompt('Nenhum texto selecionado. Título da nova tarefa:') || '').trim();
                if (!texto) return;
            }
            const titulo = texto.length > 120 ? texto.slice(0, 117) + '…' : texto;
            const id = 't-' + Date.now();
            appData.tasks.push({
                id, icon: 'fa-solid fa-book', title: titulo,
                project: note.subject || '', priority: 'Média', status: 'A Fazer',
                progress: 0, dueDate: '', subtasks: [], links: [], attachments: [],
                noteId: note.id
            });
            if (!note.linkedTaskIds) note.linkedTaskIds = [];
            note.linkedTaskIds.push(id);
            triggerAutoSave();
            renderNoteLinkedItems(note);
            if (ntPainelAtivo !== 'tarefas') ntAbrirPainel('tarefas');
            renderTasks();
            renderSubjectsList();
            renderDashboardStats();
            updateCharts();
        }

        function vincularTarefaANota(taskId) {
            if (!taskId || !currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (!note) return;
            if (!note.linkedTaskIds) note.linkedTaskIds = [];
            if (!note.linkedTaskIds.includes(taskId)) note.linkedTaskIds.push(taskId);
            triggerAutoSave();
            renderNoteLinkedItems(note);
        }

        function vincularPostitANota(postitId) {
            if (!postitId || !currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (!note) return;
            if (!note.linkedPostitIds) note.linkedPostitIds = [];
            if (!note.linkedPostitIds.includes(postitId)) note.linkedPostitIds.push(postitId);
            triggerAutoSave();
            renderNoteLinkedItems(note);
        }

        function desvincularTarefaDaNota(taskId) {
            if (!currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (!note || !note.linkedTaskIds) return;
            note.linkedTaskIds = note.linkedTaskIds.filter(id => id !== taskId);
            triggerAutoSave();
            renderNoteLinkedItems(note);
        }

        function desvincularPostitDaNota(postitId) {
            if (!currentActiveNoteId) return;
            const note = appData.notes.find(n => n.id === currentActiveNoteId);
            if (!note || !note.linkedPostitIds) return;
            note.linkedPostitIds = note.linkedPostitIds.filter(id => id !== postitId);
            triggerAutoSave();
            renderNoteLinkedItems(note);
        }

        function openFocusTimerModal() {
            const sel = document.getElementById('timer-subject-select');
            if (sel) {
                const atual = sel.value;
                sel.innerHTML = '<option value="">Sem matéria (só no dia)</option>' + (appData.subjects || []).map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
                if ((appData.subjects || []).includes(atual)) sel.value = atual;
            }
            openModal('focus-timer-modal');
        }

        function setTimerPreset(mins) {
            document.querySelectorAll('#focus-timer-modal .cg-chip').forEach(b => b.classList.toggle('on', Number(b.dataset.m) === mins));
            timerDurationConfigured = mins * 60;
            timerSeconds = timerDurationConfigured;
            updateTimerDisplay();
        }

        function applyCustomTimerMins(val) {
            const mins = parseInt(val);
            if (mins && mins > 0) {
                setTimerPreset(mins);
            }
        }

        function toggleTimer() {
            const btnText = document.getElementById('timer-btn-text');
            const iconPlay = document.getElementById('timer-icon-play');
            const statusLabel = document.getElementById('timer-status-label');

            if (isTimerRunning) {
                clearInterval(timerInterval);
                isTimerRunning = false;
                if (btnText) btnText.innerText = "Continuar Sessão";
                if (iconPlay) iconPlay.className = "fa-solid fa-play";
                if (statusLabel) statusLabel.innerText = "Sessão Pausada";
            } else {
                isTimerRunning = true;
                if (btnText) btnText.innerText = "Pausar";
                if (iconPlay) iconPlay.className = "fa-solid fa-pause";
                if (statusLabel) statusLabel.innerText = "Foco ativo...";

                timerInterval = setInterval(() => {
                    if (timerSeconds > 0) {
                        timerSeconds--;
                        updateTimerDisplay();
                    } else {
                        clearInterval(timerInterval);
                        isTimerRunning = false;
                        if (btnText) btnText.innerText = "Iniciar Sessão";
                        if (iconPlay) iconPlay.className = "fa-solid fa-play";
                        if (statusLabel) statusLabel.innerText = "Sessão concluída!";
                        
                        const sel = document.getElementById('timer-subject-select');
                        registrarSessaoFoco(sel ? sel.value : '', timerDurationConfigured);
                    }
                }, 1000);
            }
        }

        function resetTimer() {
            clearInterval(timerInterval);
            isTimerRunning = false;
            timerSeconds = timerDurationConfigured;
            updateTimerDisplay();
            document.getElementById('timer-btn-text').innerText = "Iniciar Sessão";
            document.getElementById('timer-icon-play').className = "fa-solid fa-play";
            document.getElementById('timer-status-label').innerText = "Pronto para iniciar";
        }

        function updateTimerDisplay() {
            const m = String(Math.floor(timerSeconds / 60)).padStart(2, '0');
            const s = String(timerSeconds % 60).padStart(2, '0');
            document.getElementById('timer-display').innerText = `${m}:${s}`;
        }

        function triggerDesktopPipWindow() {
            if (window.pywebview && window.pywebview.api) {
                const th = THEME_PALETTES[appData.accentTheme || 'indigo'] || THEME_PALETTES.indigo;
                const materias = (appData.subjects || []).map(n => ({ nome: n, cor: _subjectMeta(n).color }));
                window.pywebview.api.open_pip_timer(timerSeconds, { bg: th.bg, light: th.light, border: th.border, text: th.text }, materias);
            }
        }

        // ---- Registro de horas focadas (timer, PiP e manual passam por aqui) ----
        function dataLocalISO(d) {
            d = d || new Date();
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        }

        // materia '' = horas do dia, sem materia especifica
        function registrarSessaoFoco(materia, segundos, dataStr) {
            segundos = Math.round(Number(segundos) || 0);
            if (segundos <= 0) return;
            appData.studyLogs.push({
                id: 'sl-' + Date.now(),
                subject: (appData.subjects || []).includes(materia) ? materia : '',
                hours: Math.round(segundos / 36) / 100,   // 2 casas: 1 min nao vira 0
                date: dataStr || dataLocalISO()
            });
            triggerAutoSave();
            renderDashboardStats();
            updateCharts();
        }

        function concluirSessaoFoco() {
            if (isTimerRunning) toggleTimer();
            const feito = timerDurationConfigured - timerSeconds;
            if (feito < 60) { alert('Menos de 1 minuto focado - nada foi registrado.'); return; }
            const sel = document.getElementById('timer-subject-select');
            registrarSessaoFoco(sel ? sel.value : '', feito);
            resetTimer();
            closeModal('focus-timer-modal');
        }

        function fmtHoras(h) {
            const min = Math.round((Number(h) || 0) * 60);
            if (min < 60) return `${min} min`;
            const hh = Math.floor(min / 60), mm = min % 60;
            return mm ? `${hh}h ${String(mm).padStart(2, '0')}min` : `${hh}h`;
        }

        function renderSubjectHours() {
            const box = document.getElementById('subject-hours-list');
            if (!box) return;
            const range = (document.getElementById('subject-hours-range') || {}).value || 'all';
            const now = new Date();
            const monthKey = dataLocalISO(now).slice(0, 7);
            const mon = new Date(now); mon.setHours(0, 0, 0, 0); mon.setDate(mon.getDate() - ((mon.getDay() + 6) % 7));
            const sun = new Date(mon); sun.setDate(sun.getDate() + 6);
            const [wa, wb] = [dataLocalISO(mon), dataLocalISO(sun)];
            const subjects = appData.subjects || [];
            const sums = {}; let semMateria = 0;
            (appData.studyLogs || []).forEach(l => {
                if (!l || !l.date) return;
                if (range === 'month' && !l.date.startsWith(monthKey)) return;
                if (range === 'week' && (l.date < wa || l.date > wb)) return;
                const h = Number(l.hours) || 0;
                if (subjects.includes(l.subject)) sums[l.subject] = (sums[l.subject] || 0) + h; else semMateria += h;
            });
            const rows = subjects.map(n => ({ nome: n, h: sums[n] || 0, cor: _subjectMeta(n).color }));
            rows.sort((a, b) => b.h - a.h);
            if (semMateria > 0) rows.push({ nome: 'Sem matéria específica', h: semMateria, cor: '#9ca3af' });
            const total = rows.reduce((t, r) => t + r.h, 0);
            if (!rows.length) { box.innerHTML = '<p class="text-xs text-gray-400">Cadastre matérias para ver as horas por matéria.</p>'; return; }
            box.innerHTML = rows.map(r => {
                const pct = total > 0 ? Math.round((r.h / total) * 100) : 0;
                return `<div>
                    <div class="flex items-center justify-between text-xs mb-1">
                        <span class="flex items-center gap-2 text-gray-700 font-medium"><span style="width:8px;height:8px;border-radius:50%;background:${r.cor};display:inline-block"></span>${escapeHtml(r.nome)}</span>
                        <span class="text-gray-500"><b class="text-gray-800">${fmtHoras(r.h)}</b> &middot; ${pct}%</span>
                    </div>
                    <div style="height:6px;border-radius:999px;background:#f3f4f6;overflow:hidden"><div style="height:100%;width:${pct}%;background:${r.cor};border-radius:999px"></div></div>
                </div>`;
            }).join('') + `<div class="text-[11px] text-gray-400 pt-1">Total: <b class="text-gray-600">${fmtHoras(total)}</b></div>`;
        }

        function openManualTimeModal() {
            const select = document.getElementById('manual-subject-select');
            const dateInput = document.getElementById('manual-date-input');
            if (select) select.innerHTML = '<option value="">Sem matéria (só no dia)</option>' + appData.subjects.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
            if (dateInput) dateInput.value = dataLocalISO();
            openModal('manual-time-modal');
        }

        function saveManualTimeLog() {
            const subj = document.getElementById('manual-subject-select').value;
            const mins = parseInt(document.getElementById('manual-minutes-input').value);
            const dateVal = document.getElementById('manual-date-input').value;

            if (!mins || mins <= 0) {
                alert('Informe uma quantidade válida de minutos.');
                return;
            }

            registrarSessaoFoco(subj, mins * 60, dateVal || dataLocalISO());
            document.getElementById('manual-minutes-input').value = '';
            closeModal('manual-time-modal');
            renderDashboardStats();
            updateCharts();
            closeModal('focus-timer-modal');
        }

        function openProfileModal() {
            const p = appData.userProfile;
            document.getElementById('profile-name-input').value = p.name || '';
            document.getElementById('profile-role-input').value = p.role || '';
            tempProfilePhotoBase64 = p.avatarPhoto || '';
            const info = document.getElementById('foto-info');
            if (info) info.innerText = '';
            renderProfilePhotoPreview();

            const box = document.getElementById('profileAccountBox');
            const em = document.getElementById('profileAccountEmail');
            if (box && em) {
                if (contaCogni && contaCogni.signedIn) {
                    em.innerText = contaCogni.email || '';
                    box.classList.remove('hidden');
                } else {
                    box.classList.add('hidden');
                }
            }
            openModal('profile-modal');
        }

        /* A foto vai gravada DENTRO do banco, em base64. Guardar a imagem
           original (que pode ter vários MB) incha o Firebase inteiro, deixa
           cada leitura lenta e trava a árvore do console. Aqui ela é reduzida
           para 192px e convertida em JPEG antes de entrar no estado. */
        const FOTO_MAX_PX = 192;
        const FOTO_QUALIDADE = 0.82;

        function handleProfilePhotoUpload(e) {
            const file = e.target.files[0];
            if (!file) return;

            const info = document.getElementById('foto-info');
            if (info) { info.innerText = 'Processando imagem...'; info.className = 'text-[11px] text-gray-400'; }

            const reader = new FileReader();
            reader.onload = function(evt) {
                const original = evt.target.result;
                const tamOriginalKB = Math.round(original.length / 1024);

                const img = new Image();
                img.onload = function() {
                    try {
                        let { width, height } = img;
                        const escala = Math.min(1, FOTO_MAX_PX / Math.max(width, height));
                        width = Math.max(1, Math.round(width * escala));
                        height = Math.max(1, Math.round(height * escala));

                        const cv = document.createElement('canvas');
                        cv.width = width; cv.height = height;
                        const ctx = cv.getContext('2d');
                        // Fundo branco: JPEG não tem transparência, e sem isso
                        // um PNG transparente viraria fundo preto.
                        ctx.fillStyle = '#ffffff';
                        ctx.fillRect(0, 0, width, height);
                        ctx.drawImage(img, 0, 0, width, height);

                        tempProfilePhotoBase64 = cv.toDataURL('image/jpeg', FOTO_QUALIDADE);
                        const tamFinalKB = Math.round(tempProfilePhotoBase64.length / 1024);
                        if (info) {
                            info.innerText = `Imagem otimizada: ${tamOriginalKB} KB → ${tamFinalKB} KB (${width}×${height})`;
                            info.className = 'text-[11px] text-emerald-600';
                        }
                    } catch (err) {
                        console.warn('Falha ao redimensionar, usando original:', err);
                        tempProfilePhotoBase64 = original;
                        if (info) { info.innerText = 'Não foi possível otimizar a imagem.'; info.className = 'text-[11px] text-amber-600'; }
                    }
                    renderProfilePhotoPreview();
                    e.target.value = '';   // permite reenviar o mesmo arquivo
                };
                img.onerror = function() {
                    if (info) { info.innerText = 'Arquivo de imagem inválido.'; info.className = 'text-[11px] text-rose-600'; }
                    e.target.value = '';
                };
                img.src = original;
            };
            reader.readAsDataURL(file);
        }

        function removerFotoPerfil() {
            tempProfilePhotoBase64 = '';
            const inp = document.getElementById('profile-photo-file');
            if (inp) inp.value = '';
            const info = document.getElementById('foto-info');
            if (info) { info.innerText = 'Foto removida. Salve o perfil para confirmar.'; info.className = 'text-[11px] text-gray-500'; }
            renderProfilePhotoPreview();
        }

        function iniciaisDoPerfil() {
            const n = (document.getElementById('profile-name-input')?.value ||
                       (appData.userProfile || {}).name || 'CF').trim();
            const partes = n.split(/\s+/).filter(Boolean);
            const ini = partes.length >= 2 ? (partes[0][0] + partes[1][0]) : n.substring(0, 2);
            return (ini || 'CF').toUpperCase();
        }

        function renderProfilePhotoPreview() {
            const box = document.getElementById('profile-preview-box');
            const btn = document.getElementById('btn-remover-foto');
            if (!box) return;

            if (tempProfilePhotoBase64) {
                box.innerHTML = `<img src="${tempProfilePhotoBase64}" class="w-12 h-12 rounded-full object-cover shadow border">`;
                if (btn) btn.classList.remove('hidden');
            } else {
                box.innerHTML = `<div class="w-12 h-12 rounded-full accent-bg-badge text-white font-bold flex items-center justify-center text-sm shadow">${iniciaisDoPerfil()}</div>`;
                if (btn) btn.classList.add('hidden');
            }
        }

        function saveProfileCustomization() {
            const name = document.getElementById('profile-name-input').value.trim();
            const role = document.getElementById('profile-role-input').value.trim();

            if (name) {
                appData.userProfile = { name, role, avatarPhoto: tempProfilePhotoBase64 };
                renderUserProfile();
                renderDashboardStats();
                triggerAutoSave();
                closeModal('profile-modal');
            }
        }

        function renderUserProfile() {
            const p = appData.userProfile || {};
            const btn = document.getElementById('header-profile-btn');
            if (btn) btn.title = 'Perfil: ' + (p.name || 'Estudante Cogni') + (p.role ? ' · ' + p.role : '');

            const container = document.getElementById('user-avatar-container');
            if (container) {
                if (p.avatarPhoto) {
                    container.innerHTML = `<img src="${p.avatarPhoto}" class="w-7 h-7 rounded-full object-cover border">`;
                } else {
                    const init = escapeHtml((p.name || 'CF').substring(0, 2).toUpperCase());
                    container.innerHTML = `<div class="w-7 h-7 rounded-full accent-bg-badge text-white flex items-center justify-center font-bold text-[11px]">${init}</div>`;
                }
            }
        }

        function renderFlashcardDecks() {
            const grid = document.getElementById('flashcard-decks-grid');
            const bulkBtn = document.getElementById('btn-bulk-delete-decks');
            const countLabel = document.getElementById('selected-decks-count');

            if (!grid) return;

            if (bulkBtn) {
                if (appData.selectedDeckIds && appData.selectedDeckIds.length > 0) {
                    bulkBtn.classList.remove('hidden');
                    if (countLabel) countLabel.innerText = appData.selectedDeckIds.length;
                } else {
                    bulkBtn.classList.add('hidden');
                }
            }

            if (!appData.flashcardDecks || appData.flashcardDecks.length === 0) {
                grid.innerHTML = `<p class="col-span-full text-center text-notion-muted text-xs py-8">Nenhum baralho de flashcards criado.</p>`;
                return;
            }

            grid.innerHTML = appData.flashcardDecks.map(deck => {
                const total = deck.cards ? deck.cards.length : 0;
                const easyCount = deck.cards ? deck.cards.filter(c => c.status === 'mastered').length : 0;
                const hardCount = deck.cards ? deck.cards.filter(c => c.status === 'hard').length : 0;
                const wrongCount = deck.cards ? deck.cards.filter(c => c.status === 'again').length : 0;
                const isSelected = appData.selectedDeckIds && appData.selectedDeckIds.includes(deck.id);

                const pct = total > 0 ? Math.round((easyCount / total) * 100) : 0;

                return `
                    <div class="p-5 bg-white border border-notion-border rounded-xl space-y-4">
                        <div class="flex justify-between items-start gap-2">
                            <div class="flex items-center gap-2 min-w-0">
                                <input type="checkbox" ${isSelected ? 'checked' : ''} 
                                       onchange="toggleSelectDeck('${deck.id}', this.checked)" 
                                       class="rounded border-gray-300 accent-text-color focus:ring-0 cursor-pointer shrink-0">
                                <span class="cg-label truncate" title="${escapeHtml(deck.subject)}">${escapeHtml(deck.subject)}</span>
                            </div>
                            <div class="flex items-center gap-2 shrink-0">
                                <button onclick="openDeckEditorModal('${deck.id}')" class="text-xs text-gray-400 hover:text-gray-700" title="Editar Baralho"><i class="fa-solid fa-pen"></i></button>
                                <button onclick="deleteDeck('${deck.id}')" class="text-xs text-gray-400 hover:text-red-600" title="Excluir"><i class="fa-solid fa-trash"></i></button>
                            </div>
                        </div>
                        <div>
                            <h3 class="font-black text-lg tracking-tight text-gray-900 break-words">${escapeHtml(deck.title)}</h3>
                            <p class="text-xs text-gray-400 mt-0.5">${total} carta${total === 1 ? '' : 's'}</p>
                        </div>
                        <div>
                            <div class="flex items-center justify-between text-[11px] text-gray-500 mb-1.5">
                                <span class="flex items-center gap-3">
                                    <span title="Acertos"><i class="fa-solid fa-check text-gray-400"></i> ${easyCount}</span>
                                    <span title="Difíceis"><i class="fa-solid fa-minus text-gray-400"></i> ${hardCount}</span>
                                    <span title="Erros"><i class="fa-solid fa-xmark text-gray-400"></i> ${wrongCount}</span>
                                </span>
                                <span class="font-semibold text-gray-600">${pct}%</span>
                            </div>
                            <div class="w-full bg-gray-100 h-1 rounded-full overflow-hidden"><div class="accent-bg-badge h-full rounded-full transition-all" style="width: ${pct}%"></div></div>
                        </div>
                        <button onclick="startFlashcardStudy('${deck.id}')" ${total === 0 ? 'disabled' : ''} class="w-full py-2 accent-btn-bg hover:opacity-90 disabled:bg-gray-300 text-white font-semibold rounded-lg text-sm">
                            Estudar Baralho
                        </button>
                    </div>
                `;
            }).join('');
        }

        function toggleSelectDeck(id, checked) {
            if (!appData.selectedDeckIds) appData.selectedDeckIds = [];
            if (checked) {
                if (!appData.selectedDeckIds.includes(id)) appData.selectedDeckIds.push(id);
            } else {
                appData.selectedDeckIds = appData.selectedDeckIds.filter(x => x !== id);
            }
            renderFlashcardDecks();
        }

        function deleteSelectedDecks() {
            const count = (appData.selectedDeckIds || []).length;
            if (count === 0) return;
            if (confirm(`Excluir os ${count} baralhos selecionados?`)) {
                appData.flashcardDecks = appData.flashcardDecks.filter(d => !appData.selectedDeckIds.includes(d.id));
                appData.selectedDeckIds = [];
                triggerAutoSave();
                renderFlashcardDecks();
                renderDashboardStats();
            }
        }

        function openDeckEditorModal(deckId = null) {
            const subjectSelect = document.getElementById('deck-editor-subject');
            if (subjectSelect) {
                subjectSelect.innerHTML = appData.subjects.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
            }

            if (deckId) {
                const deck = appData.flashcardDecks.find(d => d.id === deckId);
                if (deck) {
                    document.getElementById('deck-editor-modal-title').innerText = "Editar Baralho";
                    document.getElementById('deck-editor-id').value = deck.id;
                    document.getElementById('deck-editor-title').value = deck.title;
                    if (subjectSelect) subjectSelect.value = deck.subject;
                    deckEditorCardsTemp = deck.cards ? JSON.parse(JSON.stringify(deck.cards)) : [];
                }
            } else {
                document.getElementById('deck-editor-modal-title').innerText = "Criar Novo Baralho";
                document.getElementById('deck-editor-id').value = "";
                document.getElementById('deck-editor-title').value = "";
                deckEditorCardsTemp = [
                    { id: 'c-1', front: '', back: '', hint: '', status: 'learning' }
                ];
            }

            renderDeckEditorCards();
            openModal('deck-editor-modal');
        }

        function addDeckEditorCard() {
            deckEditorCardsTemp.push({ id: 'c-' + Date.now(), front: '', back: '', hint: '', status: 'learning' });
            renderDeckEditorCards();
        }

        function removeDeckEditorCard(idx) {
            deckEditorCardsTemp.splice(idx, 1);
            renderDeckEditorCards();
        }

        function renderDeckEditorCards() {
            const container = document.getElementById('deck-editor-cards-list');
            if (!container) return;

            container.innerHTML = deckEditorCardsTemp.map((c, idx) => `
                <div class="p-3 bg-notion-lightBg border rounded-lg space-y-2 text-xs relative">
                    <button type="button" onclick="removeDeckEditorCard(${idx})" class="absolute top-2 right-2 text-red-500 hover:text-red-700"><i class="fa-solid fa-trash"></i></button>
                    <div class="font-bold accent-text-color">Carta ${idx + 1}</div>
                    <input type="text" placeholder="Frente / Pergunta..." value="${escapeHtml(c.front)}" oninput="deckEditorCardsTemp[${idx}].front = this.value" class="w-full border rounded px-2 py-1 bg-white">
                    <input type="text" placeholder="Verso / Resposta..." value="${escapeHtml(c.back)}" oninput="deckEditorCardsTemp[${idx}].back = this.value" class="w-full border rounded px-2 py-1 bg-white">
                    <input type="text" placeholder="Dica (opcional)..." value="${escapeHtml(c.hint || '')}" oninput="deckEditorCardsTemp[${idx}].hint = this.value" class="w-full border rounded px-2 py-1 bg-white text-amber-800">
                </div>
            `).join('');
        }

        function saveDeckEditorSubmit() {
            const id = document.getElementById('deck-editor-id').value || 'deck-' + Date.now();
            const title = document.getElementById('deck-editor-title').value.trim();
            const subject = document.getElementById('deck-editor-subject').value;

            if (!title) {
                alert('Informe um título para o baralho.');
                return;
            }

            const deckObj = {
                id,
                title,
                subject,
                cards: deckEditorCardsTemp
            };

            const existingIdx = appData.flashcardDecks.findIndex(d => d.id === id);
            if (existingIdx >= 0) {
                appData.flashcardDecks[existingIdx] = deckObj;
            } else {
                appData.flashcardDecks.push(deckObj);
            }

            triggerAutoSave();
            closeModal('deck-editor-modal');
            renderFlashcardDecks();
            renderDashboardStats();
        }

        function deleteDeck(id) {
            if (confirm('Excluir este baralho?')) {
                appData.flashcardDecks = appData.flashcardDecks.filter(d => d.id !== id);
                triggerAutoSave();
                renderFlashcardDecks();
                renderDashboardStats();
            }
        }

        let activeStudyCards = [];
        let activeCardIdx = 0;
        let activeDeckIdRef = null;

        function startFlashcardStudy(deckId) {
            const deck = appData.flashcardDecks.find(d => d.id === deckId);
            if (!deck || !deck.cards || deck.cards.length === 0) return;

            activeDeckIdRef = deckId;
            activeStudyCards = [...deck.cards];
            activeCardIdx = 0;

            document.getElementById('study-deck-title').innerText = deck.title;
            document.getElementById('study-card-subject').innerText = deck.subject;

            renderActiveStudyCard();
            openModal('flashcard-study-modal');
        }

        function renderActiveStudyCard() {
            if (activeCardIdx >= activeStudyCards.length) {
                alert('Baralho concluído!');
                closeModal('flashcard-study-modal');
                renderFlashcardDecks();
                renderDashboardStats();
                return;
            }

            const card = activeStudyCards[activeCardIdx];
            document.getElementById('study-card-counter').innerText = `${activeCardIdx + 1} / ${activeStudyCards.length}`;
            document.getElementById('study-card-front-text').innerText = card.front;
            document.getElementById('study-card-back-text').innerText = card.back;

            const hintEl = document.getElementById('study-card-hint');
            const hintBtn = document.getElementById('study-hint-btn');
            const temDica = !!(card.hint && card.hint.trim());
            hintEl.innerText = temDica ? card.hint : '';
            hintEl.classList.add('hidden');
            if (hintBtn) hintBtn.classList.toggle('hidden', !temDica);

            const inner = document.getElementById('study-card-inner');
            if (inner) inner.classList.remove('is-flipped');
        }

        function flipStudyCard() {
            const inner = document.getElementById('study-card-inner');
            if (inner) inner.classList.toggle('is-flipped');
        }

        function toggleStudyCardHint() {
            const hintEl = document.getElementById('study-card-hint');
            if (hintEl) hintEl.classList.toggle('hidden');
        }

        function rateStudyCard(rating) {
            const card = activeStudyCards[activeCardIdx];
            if (card) {
                if (rating === 'easy') card.status = 'mastered';
                else if (rating === 'hard') card.status = 'hard';
                else card.status = 'again';

                const deck = appData.flashcardDecks.find(d => d.id === activeDeckIdRef);
                if (deck && deck.cards) {
                    const originalCard = deck.cards.find(c => c.id === card.id);
                    if (originalCard) originalCard.status = card.status;
                }
            }

            activeCardIdx++;
            triggerAutoSave();
            renderActiveStudyCard();
        }

        const POSTIT_COLOR_CLASSES = {
            yellow: 'bg-amber-50/95 border-amber-300 text-amber-950',
            green: 'bg-emerald-50/95 border-emerald-300 text-emerald-950',
            blue: 'bg-blue-50/95 border-blue-300 text-blue-950',
            pink: 'bg-rose-50/95 border-rose-300 text-rose-950',
            purple: 'bg-purple-50/95 border-purple-300 text-purple-950'
        };

        function changePostitColor(id, colorKey) {
            const p = appData.postits.find(x => x.id === id);
            if (p) {
                p.colorKey = colorKey;
                p.color = POSTIT_COLOR_CLASSES[colorKey] || POSTIT_COLOR_CLASSES.yellow;
                triggerAutoSave();
                renderPostitsCanvas();
            }
        }

        function renderPostitsCanvas() {
            const container = document.getElementById('canvas-container');
            if (!container) return;

            container.innerHTML = appData.postits.map(p => {
                const colorClass = POSTIT_COLOR_CLASSES[p.colorKey] || p.color || POSTIT_COLOR_CLASSES.yellow;
                return `
                    <div id="${p.id}" class="postit-draggable absolute w-64 p-3.5 rounded-xl border shadow-md flex flex-col justify-between transition-colors ${colorClass}"
                         style="left: ${p.x || 24}px; top: ${p.y || 24}px;"
                         onmousedown="initPostitDrag(event, '${p.id}')">
                        <div class="flex items-center justify-between pb-1.5 border-b border-black/10 text-xs gap-1">
                            <select onchange="updatePostitSubject('${p.id}', this.value)" onmousedown="event.stopPropagation()" class="bg-transparent font-bold uppercase text-[10px] outline-none cursor-pointer truncate max-w-[100px]">
                                ${appData.subjects.map(s => `<option value="${escapeHtml(s)}" ${(p.attachedSubject || appData.subjects[0]) === s ? 'selected' : ''}>${escapeHtml(s)}</option>`).join('')}
                            </select>
                            <div class="flex items-center space-x-1 shrink-0" onmousedown="event.stopPropagation()">
                                <button type="button" onclick="changePostitColor('${p.id}', 'yellow')" class="w-3 h-3 rounded-full bg-amber-400 border border-amber-500 hover:scale-125 transition" title="Amarelo"></button>
                                <button type="button" onclick="changePostitColor('${p.id}', 'green')" class="w-3 h-3 rounded-full bg-emerald-400 border border-emerald-500 hover:scale-125 transition" title="Verde"></button>
                                <button type="button" onclick="changePostitColor('${p.id}', 'blue')" class="w-3 h-3 rounded-full bg-blue-400 border border-blue-500 hover:scale-125 transition" title="Azul"></button>
                                <button type="button" onclick="changePostitColor('${p.id}', 'pink')" class="w-3 h-3 rounded-full bg-rose-400 border border-rose-500 hover:scale-125 transition" title="Rosa"></button>
                                <button type="button" onclick="changePostitColor('${p.id}', 'purple')" class="w-3 h-3 rounded-full bg-purple-400 border border-purple-500 hover:scale-125 transition" title="Roxo"></button>
                                <button onclick="deletePostit('${p.id}')" class="text-xs text-red-600 hover:text-red-800 ml-1"><i class="fa-solid fa-trash"></i></button>
                            </div>
                        </div>
                        <textarea placeholder="Digite suas anotações..." 
                                  onmousedown="event.stopPropagation()" 
                                  oninput="updatePostitText('${p.id}', this.value)" 
                                  class="w-full h-24 bg-transparent resize-none outline-none text-xs font-medium mt-2 leading-relaxed">${escapeHtml(p.text || '')}</textarea>
                    </div>
                `;
            }).join('');
        }

        function createNewPostit() {
            const container = document.getElementById('canvas-container');
            const containerWidth = container ? container.clientWidth : 800;
            const cardWidth = 270;
            const gap = 20;
            const cols = Math.max(1, Math.floor((containerWidth - 40) / (cardWidth + gap)));
            const idx = appData.postits.length;
            const col = idx % cols;
            const row = Math.floor(idx / cols);

            const newP = {
                id: 'p-' + Date.now(),
                text: '',
                colorKey: 'yellow',
                color: POSTIT_COLOR_CLASSES.yellow,
                x: 24 + col * (cardWidth + gap),
                y: 24 + row * 190,
                attachedSubject: appData.subjects[0] || 'Geral'
            };
            appData.postits.push(newP);
            triggerAutoSave();
            renderPostitsCanvas();
        }

        function updatePostitSubject(id, subj) {
            const p = appData.postits.find(x => x.id === id);
            if (p) {
                p.attachedSubject = subj;
                triggerAutoSave();
            }
        }

        function updatePostitText(id, txt) {
            const p = appData.postits.find(x => x.id === id);
            if (p) {
                p.text = txt;
                triggerAutoSave();
            }
        }

        function deletePostit(id) {
            appData.postits = appData.postits.filter(p => p.id !== id);
            triggerAutoSave();
            renderPostitsCanvas();
        }

        function organizePostitsBySubject() {
            const container = document.getElementById('canvas-container');
            const containerWidth = container ? container.clientWidth : 800;
            const cardWidth = 270;
            const gap = 20;
            const cols = Math.max(1, Math.floor((containerWidth - 40) / (cardWidth + gap)));

            appData.postits.forEach((p, idx) => {
                const col = idx % cols;
                const row = Math.floor(idx / cols);
                p.x = 24 + col * (cardWidth + gap);
                p.y = 24 + row * 190;
            });
            triggerAutoSave();
            renderPostitsCanvas();
        }

        function initPostitDrag(e, id) {
            if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'BUTTON' || e.target.tagName === 'SELECT') return;
            const el = document.getElementById(id);
            if (!el) return;

            let rect = el.getBoundingClientRect();
            let offsetX = e.clientX - rect.left;
            let offsetY = e.clientY - rect.top;

            function onMouseMove(me) {
                const canvas = document.getElementById('canvas-container');
                const cRect = canvas.getBoundingClientRect();
                let nx = me.clientX - cRect.left - offsetX + canvas.scrollLeft;
                let ny = me.clientY - cRect.top - offsetY + canvas.scrollTop;
                el.style.left = Math.max(10, nx) + 'px';
                el.style.top = Math.max(10, ny) + 'px';
            }

            function onMouseUp() {
                const p = appData.postits.find(x => x.id === id);
                if (p) {
                    p.x = parseInt(el.style.left);
                    p.y = parseInt(el.style.top);
                    triggerAutoSave();
                }
                window.removeEventListener('mousemove', onMouseMove);
                window.removeEventListener('mouseup', onMouseUp);
            }

            window.addEventListener('mousemove', onMouseMove);
            window.addEventListener('mouseup', onMouseUp);
        }

        function renderResources() {
            const linksContainer = document.getElementById('all-links-list');
            const filesContainer = document.getElementById('all-files-list');

            const links = [];
            const files = [];

            appData.tasks.forEach(t => {
                if (t.links) t.links.forEach(l => links.push({ title: t.title, url: l.url, source: `Matéria: ${t.project}` }));
                if (t.attachments) t.attachments.forEach(a => files.push({ title: t.title, file: a.path, name: a.name, source: `Matéria: ${t.project}` }));
            });

            if (linksContainer) {
                linksContainer.innerHTML = links.length > 0 ? links.map(l => `
                    <div class="p-2 bg-notion-lightBg border rounded flex justify-between items-center">
                        <span class="truncate font-mono accent-text-color">${escapeHtml(l.url)}</span>
                        <button onclick="triggerOpenFile('${escapeJsString(l.url)}')" class="font-bold accent-text-color">Abrir</button>
                    </div>
                `).join('') : '<p class="text-notion-muted">Nenhum link cadastrado.</p>';
            }

            if (filesContainer) {
                filesContainer.innerHTML = files.length > 0 ? files.map(f => `
                    <div class="p-2 bg-notion-lightBg border rounded flex justify-between items-center">
                        <span class="truncate font-medium text-gray-800">${escapeHtml(f.name || f.file)}</span>
                        <button onclick="triggerOpenFile('${escapeJsString(f.file)}')" class="font-bold text-rose-600">Abrir</button>
                    </div>
                `).join('') : '<p class="text-notion-muted">Nenhum arquivo anexado.</p>';
            }
        }

        function triggerOpenFile(url) {
            if (window.pywebview && window.pywebview.api) {
                window.pywebview.api.open_external_target(url);
            }
        }

        function getWeeklyHoursData() {
            const now = new Date();
            const currentDayOfWeek = now.getDay(); 
            // Distancia em dias ate a Segunda-feira da semana atual (1 = Seg, 0 = Dom -> -6)
            const distanceToMon = currentDayOfWeek === 0 ? -6 : 1 - currentDayOfWeek;
            
            const monday = new Date(now);
            monday.setDate(now.getDate() + distanceToMon);
            monday.setHours(0,0,0,0);

            const sunday = new Date(monday);
            sunday.setDate(monday.getDate() + 6);

            const labels = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'];
            const dailySums = [0, 0, 0, 0, 0, 0, 0];

            const dateKeys = [];
            for (let i = 0; i < 7; i++) {
                const d = new Date(monday);
                d.setDate(monday.getDate() + i);
                const yyyy = d.getFullYear();
                const mm = String(d.getMonth() + 1).padStart(2, '0');
                const dd = String(d.getDate()).padStart(2, '0');
                dateKeys.push(`${yyyy}-${mm}-${dd}`);
            }

            (appData.studyLogs || []).forEach(log => {
                if (!log || !log.date) return;
                const idx = dateKeys.indexOf(log.date);
                if (idx !== -1) {
                    dailySums[idx] += Number(log.hours) || 0;
                }
            });

            const roundedSums = dailySums.map(val => Math.round(val * 10) / 10);
            const totalWeeklyHours = roundedSums.reduce((a, b) => a + b, 0);

            const monStr = `${String(monday.getDate()).padStart(2, '0')}/${String(monday.getMonth() + 1).padStart(2, '0')}`;
            const sunStr = `${String(sunday.getDate()).padStart(2, '0')}/${String(sunday.getMonth() + 1).padStart(2, '0')}`;
            const rangeLabel = `${monStr} a ${sunStr}`;

            return { labels, data: roundedSums, totalWeeklyHours: Math.round(totalWeeklyHours * 10) / 10, rangeLabel, dateKeys };
        }

        // Tooltip do mapa anual: fixo e limitado a tela, nunca cobre a propria celula
        (function () {
            let tip = null;
            const get = () => tip || (tip = Object.assign(document.body.appendChild(document.createElement('div')), { id: 'cg-tip' }));
            document.addEventListener('mouseover', e => {
                const c = e.target.closest && e.target.closest('.cg-year-cell');
                if (!c) return;
                const t = get(); t.textContent = c.dataset.tip || ''; t.style.opacity = '1';
                const r = c.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight;
                let top = r.top - h - 10; if (top < 6) top = r.bottom + 10;
                const left = Math.max(6, Math.min(window.innerWidth - w - 6, r.left + r.width / 2 - w / 2));
                t.style.top = top + 'px'; t.style.left = left + 'px';
            });
            document.addEventListener('mouseout', e => {
                const c = e.target.closest && e.target.closest('.cg-year-cell');
                if (c && tip) tip.style.opacity = '0';
            });
        })();

        function renderMonthlyHeatmap() {
            const gridContainer = document.getElementById('monthly-heatmap-grid');
            if (!gridContainer) return;

            const now = new Date();
            const year = now.getFullYear();
            const pad = n => String(n).padStart(2, '0');
            const keyOf = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
            const hm = (THEME_PALETTES[appData.accentTheme || 'indigo'] || THEME_PALETTES.indigo).heatmap;

            const byDate = {};
            (appData.studyLogs || []).forEach(l => {
                if (l && l.date) byDate[l.date] = (byDate[l.date] || 0) + (Number(l.hours) || 0);
            });

            // Sequencia atual (hoje ainda vazio nao quebra a contagem)
            let streak = 0;
            const c = new Date(now); c.setHours(0, 0, 0, 0);
            if (!(byDate[keyOf(c)] > 0)) c.setDate(c.getDate() - 1);
            while (byDate[keyOf(c)] > 0) { streak++; c.setDate(c.getDate() - 1); }

            let html = '', total = 0, active = 0;
            const end = new Date(year + 1, 0, 1);
            for (let d = new Date(year, 0, 1); d < end; d.setDate(d.getDate() + 1)) {
                const key = keyOf(d);
                const hrs = Math.round((byDate[key] || 0) * 100) / 100;
                total += hrs; if (hrs > 0) active++;
                let cls = '';
                // Dia realista de estudo: 1h a 4h. 4h ou mais = cor mais forte.
                if (hrs > 0 && hrs < 1) cls = hm.l1;
                else if (hrs >= 1 && hrs < 2) cls = hm.l2;
                else if (hrs >= 2 && hrs < 4) cls = hm.l3;
                else if (hrs >= 4) cls = hm.l4;
                const tip = `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${year} - ${hrs > 0 ? fmtHoras(hrs) + ' focados' : 'Sem registros'}`;
                html += `<div class="cg-year-cell ${cls}" data-tip="${tip}"></div>`;
            }
            gridContainer.innerHTML = html;

            const months = document.getElementById('cg-year-months');
            if (months) months.innerHTML = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'].map(m => `<span>${m}</span>`).join('');

            const legend = document.getElementById('monthly-heatmap-legend');
            if (legend) legend.innerHTML = `<span>Menos</span>` + hm.legend.map((l, i) => `<div class="${l}" title="${['Sem estudo', 'Menos de 1h', '1h a 2h', '2h a 4h', '4h ou mais'][i]}"></div>`).join('') + `<span>Mais</span>`;

            document.getElementById('monthly-total-hours-badge').innerText = `${Math.round(total * 10) / 10} h em ${year}`;
            document.getElementById('monthly-streak-badge').innerText = `Sequência: ${streak} dia${streak === 1 ? '' : 's'}`;
            document.getElementById('monthly-active-days-summary').innerText = `${active} dia${active === 1 ? '' : 's'} ativo${active === 1 ? '' : 's'} em ${year}`;
        }

        function renderDashboardStats() {
            const greeting = document.getElementById('dashboard-greeting');
            if (greeting) {
                const nome = (appData.userProfile && appData.userProfile.name) ? appData.userProfile.name.split(' ')[0] : 'Estudante';
                greeting.innerHTML = `<span>Olá,</span> <span class="text-gray-400">${escapeHtml(nome)}</span>`;
            }

            const completed = appData.tasks.filter(t => t.status === 'Concluído').length;
            const total = appData.tasks.length;
            const rate = total > 0 ? Math.round((completed / total) * 100) : 0;

            document.getElementById('kpi-completed-tasks').innerText = `${completed}/${total}`;
            document.getElementById('kpi-completion-rate').innerText = `${rate}%`;

            const weekly = getWeeklyHoursData();
            let totalAllTimeHours = 0;
            (appData.studyLogs || []).forEach(l => totalAllTimeHours += (Number(l.hours) || 0));

            document.getElementById('kpi-focus-hours').innerText = `${weekly.totalWeeklyHours} h`;
            document.getElementById('kpi-focus-hours-sub').innerText = `Semana Atual (${weekly.rangeLabel})`;
            document.getElementById('weekly-chart-range-label').innerText = `Semana (${weekly.rangeLabel})`;

            let totalCards = 0, totalMastered = 0;
            (appData.flashcardDecks || []).forEach(d => {
                if (d.cards) {
                    totalCards += d.cards.length;
                    totalMastered += d.cards.filter(c => c.status === 'mastered').length;
                }
            });
            const masteredRate = totalCards > 0 ? Math.round((totalMastered / totalCards) * 100) : 0;
            document.getElementById('kpi-flashcards-mastered').innerText = `${totalMastered}/${totalCards}`;
            document.getElementById('kpi-flashcards-rate').innerText = `${masteredRate}% dominados`;

            const subProgressList = document.getElementById('dashboard-subject-progress-list');
            if (subProgressList) {
                subProgressList.innerHTML = appData.subjects.map(s => {
                    return `
                        <div onclick="filterBySubject('${escapeJsString(s)}')" class="p-4 rounded-xl bg-notion-lightBg hover:bg-gray-100 cursor-pointer transition">
                            <div class="text-sm font-bold text-gray-800 truncate" title="${escapeHtml(s)}">${escapeHtml(s)}</div>
                            ${_subjectProgressBlock(s, true)}
                        </div>
                    `;
                }).join('');
            }

            renderMonthlyHeatmap();
            renderSubjectHours();
        }

        function updateBadges() {
            const pending = appData.tasks.filter(t => t.status !== 'Concluído').length;
            document.getElementById('pending-count-badge').innerText = pending;
            document.getElementById('decks-count-badge').innerText = (appData.flashcardDecks || []).length;
            document.getElementById('kpi-notes-count').innerText = appData.notes.length;
        }

        function initCharts() {
            try {
                if (hoursChartInstance) hoursChartInstance.destroy();
                if (subjectsChartInstance) subjectsChartInstance.destroy();

                const pal = THEME_PALETTES[appData.accentTheme || 'indigo'] || THEME_PALETTES.indigo;
                const weekly = getWeeklyHoursData();

                const ctxH = document.getElementById('studyHoursChart')?.getContext('2d');
                if (ctxH) {
                    hoursChartInstance = new Chart(ctxH, {
                        type: 'bar',
                        data: {
                            labels: weekly.labels,
                            datasets: [{ 
                                label: 'Horas Focadas', 
                                data: weekly.data, 
                                backgroundColor: pal.bg, 
                                borderRadius: 4 
                            }]
                        },
                        options: { 
                            responsive: true, 
                            maintainAspectRatio: false, 
                            plugins: { 
                                legend: { display: false },
                                tooltip: {
                                    callbacks: {
                                        label: function(context) {
                                            return `${context.parsed.y} h dedicadas`;
                                        }
                                    }
                                }
                            },
                            scales: {
                                y: {
                                    beginAtZero: true,
                                    ticks: {
                                        callback: function(value) { return value + ' h'; }
                                    }
                                }
                            }
                        }
                    });
                }

                const ctxS = document.getElementById('subjectsChart')?.getContext('2d');
                if (ctxS) {
                    const taskCounts = appData.subjects.map(s => appData.tasks.filter(t => t.project === s).length);
                    const totalTasks = taskCounts.reduce((a, b) => a + b, 0);

                    subjectsChartInstance = new Chart(ctxS, {
                        type: 'doughnut',
                        data: {
                            labels: appData.subjects,
                            datasets: [{
                                data: totalTasks > 0 ? taskCounts : appData.subjects.map(() => 0),
                                backgroundColor: pal.chartPalette
                            }]
                        },
                        options: { 
                            responsive: true, 
                            maintainAspectRatio: false, 
                            plugins: { 
                                legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10 } } } 
                            } 
                        }
                    });
                }
            } catch(e) {
                console.error("Erro ao inicializar gráficos:", e);
            }
        }

        function updateCharts() {
            initCharts();
        }

        /* ------------------------------------------------------------------
           BUSCA GLOBAL: tarefas, notas, materias e post-its num campo so. */
        function _buscaTextoPlano(html) {
            return String(html || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
        }

        function buscaGlobal(termoBruto) {
            const box = document.getElementById('global-search-results');
            if (!box) return;
            const termo = (termoBruto || '').trim().toLowerCase();
            if (termo.length < 2) { box.classList.add('hidden'); box.innerHTML = ''; return; }

            const resultados = [];

            (appData.tasks || []).forEach(t => {
                if ((t.title || '').toLowerCase().includes(termo)) {
                    resultados.push({ tipo: 'Tarefa', icone: 'fa-list-check', titulo: t.title, sub: t.project || 'Sem matéria', onclick: `irParaResultadoBusca('task','${t.id}')` });
                }
            });

            (appData.notes || []).forEach(n => {
                const tituloMatch = (n.title || '').toLowerCase().includes(termo);
                const corpoMatch = _buscaTextoPlano(n.content).toLowerCase().includes(termo);
                if (tituloMatch || corpoMatch) {
                    resultados.push({ tipo: 'Nota', icone: 'fa-book-open', titulo: n.title || 'Sem Título', sub: n.subject || '', onclick: `irParaResultadoBusca('note','${n.id}')` });
                }
            });

            (appData.subjects || []).forEach(s => {
                if (s.toLowerCase().includes(termo)) {
                    resultados.push({ tipo: 'Matéria', icone: 'fa-graduation-cap', titulo: s, sub: `${(appData.tasks || []).filter(t => t.project === s).length} tarefa(s)`, onclick: `irParaResultadoBusca('subject','${escapeJsString(s)}')` });
                }
            });

            (appData.postits || []).forEach(p => {
                if ((p.text || '').toLowerCase().includes(termo)) {
                    resultados.push({ tipo: 'Post-it', icone: 'fa-note-sticky', titulo: (p.text || '').slice(0, 60), sub: p.attachedSubject || '', onclick: `irParaResultadoBusca('postit','${p.id}')` });
                }
            });

            if (resultados.length === 0) {
                box.innerHTML = `<div class="p-3 text-gray-400 text-center">Nada encontrado para "${escapeHtml(termoBruto)}"</div>`;
            } else {
                box.innerHTML = resultados.slice(0, 25).map(r => `
                    <div class="px-3 py-2 hover:bg-notion-hover cursor-pointer flex items-center gap-2.5 border-b border-notion-border last:border-0" onclick="${r.onclick}">
                        <i class="fa-solid ${r.icone} text-gray-400 w-4 text-center"></i>
                        <div class="truncate flex-1">
                            <div class="truncate font-medium text-gray-800">${escapeHtml(r.titulo)}</div>
                            <div class="truncate text-[10px] text-notion-muted">${r.tipo} • ${escapeHtml(r.sub)}</div>
                        </div>
                    </div>
                `).join('');
            }
            box.classList.remove('hidden');
        }

        function irParaResultadoBusca(tipo, id) {
            document.getElementById('global-search-results').classList.add('hidden');
            document.getElementById('global-search-input').value = '';

            if (tipo === 'task') {
                const t = appData.tasks.find(x => x.id === id);
                filterBySubject((t && t.project) || 'ALL');
                if (t) editTask(t.id);
            } else if (tipo === 'note') {
                switchTab('notes');
                openNoteInTab(id);
            } else if (tipo === 'subject') {
                filterBySubject(id);
            } else if (tipo === 'postit') {
                switchTab('postits');
            }
        }

        document.addEventListener('click', (e) => {
            const box = document.getElementById('global-search-results');
            const input = document.getElementById('global-search-input');
            if (box && input && !box.contains(e.target) && e.target !== input) box.classList.add('hidden');
        });

        function switchTab(tabName) {
            if (tabName !== 'tasks') closeModal('task-modal');
            document.querySelectorAll('.view-content').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('.nav-btn').forEach(el => el.classList.remove('bg-notion-hover'));

            const activeView = document.getElementById(`view-${tabName}`);
            const activeBtn = document.getElementById(`nav-${tabName}`);

            if (activeView) activeView.classList.remove('hidden');
            if (activeBtn) activeBtn.classList.add('bg-notion-hover');

            if (tabName === 'dashboard') renderDashboardStats();
            if (tabName === 'flashcards') renderFlashcardDecks();
            if (tabName === 'resources') renderResources();
            if (tabName === 'postits') renderPostitsCanvas();
            if (tabName === 'calendar') renderCalendar();
            if (tabName === 'mindmap') renderMindMapSidebar();
        }

        /* ------------------------------------------------------------------
           CALENDARIO: grade mensal com eventos por dia. */
        let calendarViewDate = new Date();
        const CALENDAR_COLOR_PALETTE = ['#4f46e5', '#e11d48', '#0284c7', '#059669', '#d97706', '#7c3aed'];
        let selectedCalendarColor = CALENDAR_COLOR_PALETTE[0];

        function _dataLocalISO(d) {
            // toISOString() usa UTC e pode voltar um dia no fuso do Brasil;
            // monta a string a partir dos componentes locais.
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        }

        function calendarPrevMonth() { calendarViewDate.setMonth(calendarViewDate.getMonth() - 1); renderCalendar(); }
        function calendarNextMonth() { calendarViewDate.setMonth(calendarViewDate.getMonth() + 1); renderCalendar(); }
        function calendarToday() { calendarViewDate = new Date(); renderCalendar(); }

        function renderCalendar() {
            const grid = document.getElementById('calendar-grid');
            const label = document.getElementById('calendar-month-label');
            if (!grid || !label) return;

            const ano = calendarViewDate.getFullYear();
            const mes = calendarViewDate.getMonth();
            const nomesMes = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro'];
            label.innerText = `${nomesMes[mes]} ${ano}`;

            const primeiroDia = new Date(ano, mes, 1);
            const diasNoMes = new Date(ano, mes + 1, 0).getDate();
            const offset = primeiroDia.getDay();
            const hojeStr = _dataLocalISO(new Date());

            const eventosPorData = {};
            (appData.calendarEvents || []).forEach(ev => {
                if (!eventosPorData[ev.date]) eventosPorData[ev.date] = [];
                eventosPorData[ev.date].push(ev);
            });

            let celulas = '';
            for (let i = 0; i < offset; i++) celulas += `<div class="min-h-[90px] bg-gray-50/50 rounded-md"></div>`;

            for (let dia = 1; dia <= diasNoMes; dia++) {
                const dataStr = `${ano}-${String(mes + 1).padStart(2, '0')}-${String(dia).padStart(2, '0')}`;
                const ehHoje = dataStr === hojeStr;
                const eventos = (eventosPorData[dataStr] || []).sort((a, b) => (a.time || '').localeCompare(b.time || ''));

                celulas += `
                    <div class="min-h-[90px] p-1.5 rounded-md border ${ehHoje ? 'border-indigo-400 bg-indigo-50/40' : 'border-notion-border bg-white hover:bg-gray-50'} cursor-pointer transition" onclick="openCalendarEventModal('${dataStr}')">
                        <div class="text-[11px] font-semibold ${ehHoje ? 'accent-text-color' : 'text-gray-600'}">${dia}</div>
                        <div class="space-y-0.5 mt-1">
                            ${eventos.slice(0, 3).map(ev => `
                                <div onclick="event.stopPropagation(); openCalendarEventModal('${dataStr}','${ev.id}')" class="text-[9px] text-white px-1 py-0.5 rounded truncate" style="background-color: ${ev.color || '#4f46e5'}" title="${escapeHtml(ev.title)}${ev.time ? ' · ' + ev.time : ''}">
                                    ${ev.time ? escapeHtml(ev.time) + ' ' : ''}${escapeHtml(ev.title)}
                                </div>
                            `).join('')}
                            ${eventos.length > 3 ? `<div class="text-[9px] text-notion-muted">+${eventos.length - 3} mais</div>` : ''}
                        </div>
                    </div>
                `;
            }
            grid.innerHTML = celulas;
        }

        function renderCalendarColorGrid() {
            const grid = document.getElementById('calendar-event-color-grid');
            if (!grid) return;
            grid.innerHTML = CALENDAR_COLOR_PALETTE.map(c => `
                <button type="button" onclick="selectedCalendarColor='${c}'; renderCalendarColorGrid();"
                        class="w-6 h-6 rounded-full border-2 ${selectedCalendarColor === c ? 'border-gray-800 scale-110' : 'border-transparent'} transition"
                        style="background-color: ${c}"></button>
            `).join('');
        }

        function openCalendarEventModal(dataStr, eventId = '') {
            const ev = eventId ? (appData.calendarEvents || []).find(e => e.id === eventId) : null;
            document.getElementById('calendar-event-id').value = eventId || '';
            document.getElementById('calendar-event-title').value = ev ? ev.title : '';
            document.getElementById('calendar-event-date').value = ev ? ev.date : dataStr;
            document.getElementById('calendar-event-time').value = ev ? (ev.time || '') : '';
            document.getElementById('calendar-event-notes').value = ev ? (ev.notes || '') : '';
            document.getElementById('calendar-event-modal-title').innerText = ev ? 'Editar Evento' : 'Novo Evento';
            document.getElementById('calendar-event-delete-btn').classList.toggle('hidden', !ev);
            selectedCalendarColor = ev ? (ev.color || CALENDAR_COLOR_PALETTE[0]) : CALENDAR_COLOR_PALETTE[0];
            renderCalendarColorGrid();
            openModal('calendar-event-modal');
        }

        function saveCalendarEventSubmit() {
            const id = document.getElementById('calendar-event-id').value || 'ev-' + Date.now();
            const title = document.getElementById('calendar-event-title').value.trim();
            const date = document.getElementById('calendar-event-date').value;
            if (!title || !date) { alert('Preencha título e data.'); return; }

            const eventoObj = {
                id, title, date,
                time: document.getElementById('calendar-event-time').value || '',
                notes: document.getElementById('calendar-event-notes').value || '',
                color: selectedCalendarColor
            };

            if (!appData.calendarEvents) appData.calendarEvents = [];
            const idx = appData.calendarEvents.findIndex(e => e.id === id);
            if (idx >= 0) appData.calendarEvents[idx] = eventoObj; else appData.calendarEvents.push(eventoObj);

            triggerAutoSave();
            closeModal('calendar-event-modal');
            renderCalendar();
        }

        function deleteCalendarEventFromModal() {
            const id = document.getElementById('calendar-event-id').value;
            if (!id) return;
            if (confirm('Excluir este evento?')) {
                appData.calendarEvents = (appData.calendarEvents || []).filter(e => e.id !== id);
                triggerAutoSave();
                closeModal('calendar-event-modal');
                renderCalendar();
            }
        }

        /* ====================================================================
           MAPA MENTAL
           ====================================================================
           Topicos sao divs posicionados por x/y absolutos; conexoes e os
           tracos de desenho livre vivem numa camada SVG por cima. O texto do
           topico vem de um <textarea> e e renderizado via escapeHtml (nunca
           innerHTML cru) - entao isso NAO reabre a superficie de XSS que
           o sanitizarHtml() existe pra fechar no Caderno de Notas: innerText
           nunca interpreta marcacao, so texto puro.
           ==================================================================== */
        let currentActiveMindMapId = null;
        let mindMapMode = 'select'; // 'select' | 'connect' | 'draw'
        let mindMapConnectFromId = null;
        let selectedMindMapNodeId = null;
        let selectedMindMapDrawingId = null;
        let selectedMindMapConnId = null;
        let mindMapDrawColor = '#1f2937';
        let mindMapDraggingNodeId = null;
        let mindMapDragStartX = 0, mindMapDragStartY = 0;
        let mindMapDragNodeStartX = 0, mindMapDragNodeStartY = 0;
        let mindMapDragMoved = false;
        let mindMapDrawingPoints = null;
        const MINDMAP_NODE_COLORS = ['#4f46e5', '#e11d48', '#0284c7', '#059669', '#d97706', '#7c3aed', '#db2777', '#64748b'];
        const MINDMAP_FONT_SIZES = ['12px', '14px', '18px'];

        let mindMapScale = 1;
        const MINDMAP_W = 2400, MINDMAP_H = 1400;

        function applyMindMapScale() {
            const canvas = document.getElementById('mindmap-canvas');
            const stage = document.getElementById('mindmap-stage');
            const label = document.getElementById('mindmap-zoom-label');
            if (canvas) canvas.style.transform = `scale(${mindMapScale})`;
            // transform nao altera o tamanho de layout: o stage assume o
            // tamanho escalado, e e ele que faz as barras de rolagem
            // acompanharem o zoom.
            if (stage) { stage.style.width = (MINDMAP_W * mindMapScale) + 'px'; stage.style.height = (MINDMAP_H * mindMapScale) + 'px'; }
            if (label) label.innerText = Math.round(mindMapScale * 100) + '%';
        }

        function mindMapZoom(delta) {
            mindMapScale = Math.min(2, Math.max(0.3, Math.round((mindMapScale + delta) * 100) / 100));
            applyMindMapScale();
        }

        function mindMapZoomReset() { mindMapScale = 1; applyMindMapScale(); }

        function mindMapWheel(event) {
            if (!event.ctrlKey) return; // roda sozinha = rolagem normal
            event.preventDefault();
            mindMapZoom(event.deltaY < 0 ? 0.1 : -0.1);
        }

        function _currentMindMap() {
            return (appData.mindMaps || []).find(m => m.id === currentActiveMindMapId);
        }

        function renderMindMapSidebar() {
            const container = document.getElementById('mindmap-sidebar-list');
            if (!container) return;
            container.innerHTML = (appData.mindMaps || []).map(m => `
                <div class="p-2 rounded hover:bg-notion-hover cursor-pointer transition flex items-center justify-between gap-2 ${currentActiveMindMapId === m.id ? 'bg-notion-hover font-semibold' : ''}" onclick="openMindMap('${m.id}')">
                    <span class="truncate text-xs text-gray-800">${escapeHtml(m.title) || 'Sem Título'}</span>
                    <span class="text-[10px] text-notion-muted shrink-0">${(m.nodes || []).length} tóp.</span>
                </div>
            `).join('');
        }

        function createNewMindMap() {
            if (!appData.mindMaps) appData.mindMaps = [];
            const novo = { id: 'mm-' + Date.now(), title: '', subject: appData.subjects[0] || 'Geral', nodes: [{ id: 'node-title-' + Date.now(), kind: 'title', x: 80, y: 30, text: 'Assunto do mapa', color: '#111827', textColor: '#ffffff', bold: true, fontSize: '28px' }], connections: [], drawings: [] };
            appData.mindMaps.unshift(novo);
            triggerAutoSave();
            renderMindMapSidebar();
            openMindMap(novo.id);
        }

        function openMindMap(id) {
            const map = appData.mindMaps.find(m => m.id === id);
            if (!map) return;
            currentActiveMindMapId = id;
            selectedMindMapNodeId = null;
            mindMapMode = 'select';
            mindMapConnectFromId = null;

            document.getElementById('mindmap-empty-state').classList.add('hidden');
            document.getElementById('mindmap-stage').classList.remove('hidden');
            applyMindMapScale();
            document.getElementById('mindmap-toolbar').classList.remove('hidden');
            document.getElementById('mindmap-toolbar').classList.add('flex');
            document.getElementById('mindmap-node-toolbar').classList.add('hidden');
            document.getElementById('mindmap-node-toolbar').classList.remove('flex');
            document.getElementById('mindmap-title-input').value = map.title || '';

            updateMindMapModeButtons();
            renderMindMapSidebar();
            renderMindMapCanvas();
        }

        function updateCurrentMindMapTitle(val) {
            const map = _currentMindMap();
            if (!map) return;
            map.title = val;
            triggerAutoSave();
            renderMindMapSidebar();
        }

        function deleteCurrentMindMap() {
            if (!currentActiveMindMapId) return;
            if (!confirm('Excluir este mapa mental? Os tópicos, conexões e desenhos dele se perdem.')) return;
            appData.mindMaps = appData.mindMaps.filter(m => m.id !== currentActiveMindMapId);
            currentActiveMindMapId = null;
            triggerAutoSave();
            renderMindMapSidebar();

            document.getElementById('mindmap-stage').classList.add('hidden');
            document.getElementById('mindmap-toolbar').classList.add('hidden');
            document.getElementById('mindmap-toolbar').classList.remove('flex');
            document.getElementById('mindmap-node-toolbar').classList.add('hidden');
            document.getElementById('mindmap-node-toolbar').classList.remove('flex');
            document.getElementById('mindmap-empty-state').classList.remove('hidden');
        }

        function addMindMapNode(kind) {
            const map = _currentMindMap();
            if (!map) return;
            const n = map.nodes.length;
            const ehTexto = kind === 'text';
            const node = {
                id: 'node-' + Date.now() + '-' + n,
                kind: ehTexto ? 'text' : 'topic',
                x: 80 + (n % 6) * 160,
                y: 150 + Math.floor(n / 6) * 110,
                text: ehTexto ? 'Texto' : 'Novo tópico',
                color: ehTexto ? 'transparent' : MINDMAP_NODE_COLORS[n % MINDMAP_NODE_COLORS.length],
                textColor: ehTexto ? '#111827' : '#ffffff',
                bold: false,
                fontSize: MINDMAP_FONT_SIZES[ehTexto ? 1 : 0]
            };
            map.nodes.push(node);
            triggerAutoSave();
            renderMindMapSidebar();
            selectMindMapNode(node.id, true);
        }

        function addMindMapTitle() {
            const map = _currentMindMap();
            if (!map) return;
            const existente = map.nodes.find(n => n.kind === 'title');
            if (existente) { selectMindMapNode(existente.id, true); return; }
            const node = { id: 'node-title-' + Date.now(), kind: 'title', x: 80, y: 30, text: 'Assunto do mapa', color: '#111827', textColor: '#ffffff', bold: true, fontSize: '28px' };
            map.nodes.push(node);
            triggerAutoSave();
            selectMindMapNode(node.id, true);
        }

        function disconnectMindMapNode() {
            const map = _currentMindMap();
            if (!map || !selectedMindMapNodeId) return;
            const antes = map.connections.length;
            map.connections = map.connections.filter(c => c.from !== selectedMindMapNodeId && c.to !== selectedMindMapNodeId);
            if (map.connections.length !== antes) triggerAutoSave();
            renderMindMapCanvas();
        }

        function selectMindMapConnection(event, connId) {
            event.stopPropagation();
            selectedMindMapNodeId = null;
            selectedMindMapDrawingId = null;
            selectedMindMapConnId = connId;
            _hideMindMapToolbars();
            const tb = document.getElementById('mindmap-conn-toolbar');
            tb.classList.remove('hidden');
            tb.classList.add('flex');
            renderMindMapCanvas();
        }

        function deleteSelectedMindMapConnection() {
            const map = _currentMindMap();
            if (!map || !selectedMindMapConnId) return;
            map.connections = map.connections.filter(c => c.id !== selectedMindMapConnId);
            selectedMindMapConnId = null;
            _hideMindMapToolbars();
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function setMindMapNodeText(valor) {
            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === selectedMindMapNodeId);
            if (!node) return;
            node.text = String(valor).slice(0, 2000);
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function _hideMindMapToolbars() {
            ['mindmap-node-toolbar', 'mindmap-drawing-toolbar', 'mindmap-conn-toolbar'].forEach(id => {
                const el = document.getElementById(id);
                if (el) { el.classList.add('hidden'); el.classList.remove('flex'); }
            });
        }

        function selectMindMapDrawing(event, drawId) {
            event.stopPropagation();
            selectedMindMapNodeId = null;
            selectedMindMapConnId = null;
            selectedMindMapDrawingId = drawId;
            _hideMindMapToolbars();
            const tb = document.getElementById('mindmap-drawing-toolbar');
            tb.classList.remove('hidden');
            tb.classList.add('flex');
            renderMindMapCanvas();
        }

        function deleteSelectedMindMapDrawing() {
            const map = _currentMindMap();
            if (!map || !selectedMindMapDrawingId) return;
            map.drawings = map.drawings.filter(d => d.id !== selectedMindMapDrawingId);
            selectedMindMapDrawingId = null; selectedMindMapConnId = null;
            _hideMindMapToolbars();
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function mindMapNodeMouseDown(event, nodeId) {
            const el = event.currentTarget;
            event.stopPropagation();

            if (mindMapMode === 'connect') {
                if (!mindMapConnectFromId) {
                    mindMapConnectFromId = nodeId;
                } else if (mindMapConnectFromId !== nodeId) {
                    addMindMapConnection(mindMapConnectFromId, nodeId);
                    mindMapConnectFromId = null;
                }
                renderMindMapCanvas();
                return;
            }
            if (mindMapMode === 'draw') return;

            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === nodeId);
            if (!node) return;
            mindMapDraggingNodeId = nodeId;
            mindMapDragMoved = false;
            mindMapDragStartX = event.clientX;
            mindMapDragStartY = event.clientY;
            mindMapDragNodeStartX = node.x;
            mindMapDragNodeStartY = node.y;
        }

        function mindMapCanvasMouseDown(event) {
            if (mindMapMode === 'draw') {
                if (event.target.closest('.mindmap-node')) return;
                const rect = event.currentTarget.getBoundingClientRect();
                mindMapDrawingPoints = [[(event.clientX - rect.left) / mindMapScale, (event.clientY - rect.top) / mindMapScale]];
                return;
            }
            if (!event.target.closest('.mindmap-node')) {
                selectedMindMapNodeId = null;
                selectedMindMapDrawingId = null; selectedMindMapConnId = null;
                mindMapConnectFromId = null;
                _hideMindMapToolbars();
                renderMindMapCanvas();
            }
        }

        function mindMapCanvasMouseMove(event) {
            if (mindMapDraggingNodeId) {
                const dx = (event.clientX - mindMapDragStartX) / mindMapScale;
                const dy = (event.clientY - mindMapDragStartY) / mindMapScale;
                if (Math.abs(dx) > 3 || Math.abs(dy) > 3) mindMapDragMoved = true;
                const map = _currentMindMap();
                const node = map && map.nodes.find(n => n.id === mindMapDraggingNodeId);
                if (node) {
                    node.x = Math.max(0, mindMapDragNodeStartX + dx);
                    node.y = Math.max(0, mindMapDragNodeStartY + dy);
                    renderMindMapCanvas();
                }
                return;
            }
            if (mindMapDrawingPoints) {
                const rect = event.currentTarget.getBoundingClientRect();
                mindMapDrawingPoints.push([(event.clientX - rect.left) / mindMapScale, (event.clientY - rect.top) / mindMapScale]);
                _renderMindMapActiveStroke();
            }
        }

        function mindMapCanvasMouseUp() {
            if (mindMapDraggingNodeId) {
                const nodeId = mindMapDraggingNodeId;
                mindMapDraggingNodeId = null;
                if (mindMapDragMoved) triggerAutoSave();
                else selectMindMapNode(nodeId);
                return;
            }
            if (mindMapDrawingPoints) {
                if (mindMapDrawingPoints.length > 1) {
                    const map = _currentMindMap();
                    if (map) {
                        map.drawings.push({ id: 'draw-' + Date.now(), d: _mindMapPontosParaPathD(mindMapDrawingPoints), color: mindMapDrawColor });
                        triggerAutoSave();
                    }
                }
                mindMapDrawingPoints = null;
                renderMindMapCanvas();
            }
        }

        function _mindMapPontosParaPathD(pontos) {
            return pontos.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
        }

        function _renderMindMapActiveStroke() {
            const layer = document.getElementById('mindmap-drawings-layer');
            if (!layer || !mindMapDrawingPoints) return;
            let ativo = document.getElementById('mindmap-active-stroke');
            if (!ativo) {
                ativo = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                ativo.id = 'mindmap-active-stroke';
                ativo.setAttribute('fill', 'none');
                ativo.setAttribute('stroke-width', '2.5');
                ativo.setAttribute('stroke-linecap', 'round');
                ativo.setAttribute('stroke-linejoin', 'round');
                ativo.setAttribute('stroke', mindMapDrawColor);
                layer.appendChild(ativo);
            }
            ativo.setAttribute('d', _mindMapPontosParaPathD(mindMapDrawingPoints));
        }

        function addMindMapConnection(fromId, toId) {
            const map = _currentMindMap();
            if (!map) return;
            const jaExiste = map.connections.some(c => (c.from === fromId && c.to === toId) || (c.from === toId && c.to === fromId));
            if (jaExiste) return;
            map.connections.push({ id: 'conn-' + Date.now(), from: fromId, to: toId });
            triggerAutoSave();
        }

        function toggleMindMapConnectMode() {
            mindMapMode = mindMapMode === 'connect' ? 'select' : 'connect';
            mindMapConnectFromId = null;
            selectedMindMapNodeId = null;
            selectedMindMapDrawingId = null; selectedMindMapConnId = null;
            _hideMindMapToolbars();
            updateMindMapModeButtons();
            renderMindMapCanvas();
        }

        function toggleMindMapDrawMode() {
            mindMapMode = mindMapMode === 'draw' ? 'select' : 'draw';
            mindMapConnectFromId = null;
            selectedMindMapNodeId = null;
            selectedMindMapDrawingId = null; selectedMindMapConnId = null;
            _hideMindMapToolbars();
            updateMindMapModeButtons();
            renderMindMapCanvas();
        }

        function updateMindMapModeButtons() {
            const btnConnect = document.getElementById('mindmap-btn-connect');
            const btnDraw = document.getElementById('mindmap-btn-draw');
            const colorGrid = document.getElementById('mindmap-draw-color-grid');
            if (btnConnect) { btnConnect.classList.toggle('accent-btn-bg', mindMapMode === 'connect'); btnConnect.classList.toggle('text-white', mindMapMode === 'connect'); }
            if (btnDraw) { btnDraw.classList.toggle('accent-btn-bg', mindMapMode === 'draw'); btnDraw.classList.toggle('text-white', mindMapMode === 'draw'); }
            if (colorGrid) {
                const emModoDesenho = mindMapMode === 'draw';
                colorGrid.classList.toggle('hidden', !emModoDesenho);
                colorGrid.classList.toggle('flex', emModoDesenho);
                if (emModoDesenho && !colorGrid.dataset.montado) {
                    colorGrid.dataset.montado = '1';
                    colorGrid.innerHTML = MINDMAP_NODE_COLORS.concat(['#1f2937']).map(c => `<button type="button" onclick="mindMapDrawColor='${c}'" class="w-5 h-5 rounded-full border" style="background-color:${c}"></button>`).join('');
                }
            }
        }

        function selectMindMapNode(nodeId, focarTexto) {
            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === nodeId);
            selectedMindMapNodeId = nodeId;
            selectedMindMapDrawingId = null; selectedMindMapConnId = null;
            _hideMindMapToolbars();
            const toolbar = document.getElementById('mindmap-node-toolbar');
            toolbar.classList.remove('hidden');
            toolbar.classList.add('flex');
            const ehTexto = !!(node && node.kind === 'text');
            document.getElementById('mindmap-node-kind-label').innerText = ehTexto ? 'Texto:' : (node && node.kind === 'title' ? 'Título:' : 'Tópico:');
            document.getElementById('mindmap-btn-textcolor').classList.toggle('hidden', ehTexto);
            const input = document.getElementById('mindmap-node-text-input');
            input.value = node ? node.text : '';
            document.getElementById('mindmap-node-color-grid').innerHTML = (ehTexto ? MINDMAP_NODE_COLORS.concat(['#111827']) : MINDMAP_NODE_COLORS).map(c => `<button type="button" onclick="setMindMapNodeColor('${c}')" class="w-5 h-5 rounded-full border-2 border-white shadow" style="background-color:${c}"></button>`).join('');
            renderMindMapCanvas();
            if (focarTexto) { input.focus(); input.select(); }
        }

        function setMindMapNodeColor(color) {
            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === selectedMindMapNodeId);
            if (!node) return;
            if (node.kind === 'text') node.textColor = color; else node.color = color;
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function toggleMindMapNodeTextColor() {
            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === selectedMindMapNodeId);
            if (!node) return;
            node.textColor = node.textColor === '#ffffff' ? '#111827' : '#ffffff';
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function toggleMindMapNodeBold() {
            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === selectedMindMapNodeId);
            if (!node) return;
            node.bold = !node.bold;
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function cycleMindMapNodeFontSize() {
            const map = _currentMindMap();
            const node = map && map.nodes.find(n => n.id === selectedMindMapNodeId);
            if (!node) return;
            const idx = MINDMAP_FONT_SIZES.indexOf(node.fontSize);
            node.fontSize = MINDMAP_FONT_SIZES[(idx + 1) % MINDMAP_FONT_SIZES.length];
            triggerAutoSave();
            renderMindMapCanvas();
        }

        function deleteMindMapNode(nodeId) {
            if (!nodeId) return;
            const map = _currentMindMap();
            if (!map) return;
            if (!confirm('Excluir este tópico e as conexões dele?')) return;
            map.nodes = map.nodes.filter(n => n.id !== nodeId);
            map.connections = map.connections.filter(c => c.from !== nodeId && c.to !== nodeId);
            selectedMindMapNodeId = null;
            _hideMindMapToolbars();
            triggerAutoSave();
            renderMindMapSidebar();
            renderMindMapCanvas();
        }

        function _mindMapCenter(node) {
            const el = document.querySelector(`[data-node-id="${node.id}"]`);
            const w = (el && el.offsetWidth) || 120;
            const h = (el && el.offsetHeight) || 36;
            return [node.x + w / 2, node.y + h / 2];
        }

        function renderMindMapConnections(map) {
            const layer = document.getElementById('mindmap-connections-layer');
            if (!layer) return;
            layer.innerHTML = (map.connections || []).map(c => {
                const from = map.nodes.find(n => n.id === c.from);
                const to = map.nodes.find(n => n.id === c.to);
                if (!from || !to) return '';
                const [x1, y1] = _mindMapCenter(from);
                const [x2, y2] = _mindMapCenter(to);
                const sel = c.id === selectedMindMapConnId;
                const clicavel = mindMapMode === 'select';
                return `<g style="pointer-events:${clicavel ? 'stroke' : 'none'}; cursor:pointer;" onmousedown="selectMindMapConnection(event, '${escapeJsString(c.id)}')">
                    <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="transparent" stroke-width="14"></line>
                    <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${sel ? '#6366f1' : '#94a3b8'}" stroke-width="${sel ? 3 : 2}" marker-end="url(#mindmap-arrow)"></line>
                </g>`;
            }).join('');
        }

        function renderMindMapDrawings(map) {
            const layer = document.getElementById('mindmap-drawings-layer');
            if (!layer) return;
            const clicavel = mindMapMode === 'select';
            layer.innerHTML = (map.drawings || []).map(d => {
                const sel = d.id === selectedMindMapDrawingId;
                const cor = /^#[0-9a-fA-F]{3,8}$/.test(d.color || '') ? d.color : '#1f2937';
                const caminho = String(d.d).replace(/[^MLm0-9.,\s-]/g, '');
                return `<g data-draw-id="${escapeHtml(d.id)}" style="pointer-events:${clicavel ? 'stroke' : 'none'}; cursor:pointer;" onmousedown="selectMindMapDrawing(event, '${escapeJsString(d.id)}')">
                    ${sel ? `<path d="${caminho}" fill="none" stroke="#6366f1" stroke-opacity="0.3" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"></path>` : ''}
                    <path d="${caminho}" fill="none" stroke="transparent" stroke-width="14"></path>
                    <path d="${caminho}" fill="none" stroke="${cor}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>
                </g>`;
            }).join('');
        }

        function renderMindMapCanvas() {
            const map = _currentMindMap();
            if (!map) return;
            const nodesLayer = document.getElementById('mindmap-nodes-layer');
            nodesLayer.innerHTML = map.nodes.map(node => `
                <div data-node-id="${node.id}"
                     class="mindmap-node absolute select-none ${node.kind === 'title' ? 'px-6 py-4' : 'px-3 py-2'} ${node.kind === 'text' ? 'max-w-[420px] text-left' : (node.kind === 'title' ? 'rounded-2xl shadow-md min-w-[240px] max-w-[460px] text-center' : 'rounded-lg shadow-sm min-w-[120px] max-w-[220px] text-center')} ${selectedMindMapNodeId === node.id ? 'ring-2 ring-offset-1 ring-indigo-500' : ''} ${mindMapConnectFromId === node.id ? 'ring-2 ring-offset-1 ring-emerald-500' : ''}"
                     style="left:${node.x}px; top:${node.y}px; background-color:${node.color}; color:${node.textColor}; font-size:${node.fontSize}; font-weight:${node.bold ? '700' : '500'}; white-space:pre-wrap; overflow-wrap:anywhere; min-height:1.5em; ${node.kind === 'text' ? 'border-radius:6px;' : ''} cursor:${mindMapMode === 'draw' ? 'default' : 'grab'};"
                     onmousedown="mindMapNodeMouseDown(event, '${node.id}')"
                     ondblclick="event.stopPropagation(); selectMindMapNode('${node.id}', true)">${escapeHtml(node.text) || '&#8203;'}</div>
            `).join('');

            renderMindMapConnections(map);
            renderMindMapDrawings(map);
        }
    </script>
</body>
</html>
"""

def main():
    _registrar_identidade_windows()
    api = CogniDesktopAPI()
    index_uri = _escrever_ui_local('index.html', HTML_CONTENT.replace('__APP_VERSION__', APP_VERSION))
    window = webview.create_window(
        title='Cogni',
        url=index_uri,
        js_api=api,
        width=1280,
        height=800,
        min_size=(900, 600),
        resizable=True
    )
    window.events.closing += (lambda: api._spotify_encerrar())
    # Garantia de saida: nenhum processo fantasma prendendo o perfil do WebView2.
    window.events.closed += (lambda: threading.Thread(
        target=lambda: (time.sleep(5), os._exit(0)), daemon=True).start())
    # O player do Spotify (iframe invisivel) precisa tocar sem clique direto nele.
    _arg = "--autoplay-policy=no-user-gesture-required"
    _atual = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if _arg not in _atual:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (_atual + " " + _arg).strip()
    # Perfil PERSISTENTE (o padrao do pywebview e "privado"); mesma configuracao
    # em que o player do Spotify foi validado no NEXUS.
    perfil = os.path.join(os.path.dirname(api._base_dir), "webview")
    try:
        os.makedirs(perfil, exist_ok=True)
    except Exception:
        perfil = None
    # COGNI_DEBUG=1 liga o DevTools (botao direito > Inspecionar).
    webview.start(debug=(os.environ.get("COGNI_DEBUG") == "1"), private_mode=False, storage_path=perfil)

if __name__ == '__main__':
    main()
