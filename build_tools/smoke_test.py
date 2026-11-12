r"""
Teste de fumaca do executavel: abre o COGNI.exe numa pasta de dados descartavel
(COGNI_BASE_DIR) e falha se o app nao subir de verdade.

Por que existe: o 4.6.5 foi publicado com um erro que impedia o app de abrir
(UnboundLocalError em __init__). Compilar e checar sintaxe nao pega isso; so
EXECUTAR pega. Este teste roda antes de publicar.

Uso:  python build_tools\smoke_test.py dist\COGNI\COGNI.exe [segundos] [-- args...]
Codigo de saida: 0 = o app abriu e ficou de pe; 1 = falhou.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def janelas_do_pid(pid):
    achadas = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            achadas.append((buf.value, cls.value, bool(u32.IsWindowVisible(hwnd))))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return achadas


def main():
    if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
        print("FALHA: executavel nao encontrado: %s" % (sys.argv[1:] or ["(vazio)"])[0])
        return 1
    exe = os.path.abspath(sys.argv[1])
    espera = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 15
    extra = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    tmp = tempfile.mkdtemp(prefix="cogni_smoke_")
    env = dict(os.environ, COGNI_BASE_DIR=tmp)
    print("[smoke] abrindo %s por %ds (dados em %s)" % (exe, espera, tmp))
    p = subprocess.Popen([exe] + extra, env=env, cwd=os.path.dirname(exe))
    erro = None
    janela = False
    try:
        t0 = time.time()
        while time.time() - t0 < espera:
            if p.poll() is not None:
                erro = "o processo encerrou sozinho (codigo %s)" % p.returncode
                break
            for titulo, classe, visivel in janelas_do_pid(p.pid):
                if "erro ao iniciar" in titulo.lower():
                    erro = "o app mostrou o dialogo de erro de inicializacao"
                if titulo.strip() == "Cogni" and classe.startswith("WindowsForms") and visivel:
                    janela = True
            if erro:
                break
            time.sleep(0.5)
        if not erro and not janela:
            erro = "a janela principal do Cogni nao apareceu em %ds" % espera
        if not erro and not os.path.isfile(os.path.join(tmp, "ui", "index.html")):
            erro = "a interface (ui\\index.html) nao foi gerada"
    finally:
        subprocess.call(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        shutil.rmtree(tmp, ignore_errors=True)
    if erro:
        print("[smoke] FALHA: " + erro)
        return 1
    print("[smoke] OK: o app abriu e ficou de pe por %ds" % espera)
    return 0


if __name__ == "__main__":
    sys.exit(main())
