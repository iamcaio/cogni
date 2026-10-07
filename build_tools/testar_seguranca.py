# -*- coding: utf-8 -*-
"""
Testes de regressao de seguranca do lado Python. Sem rede, sem conta real.
Cada payload tenta criar um arquivo-marcador numa pasta temporaria; se o
marcador existir no fim, o payload EXECUTOU.

Uso:  python build_tools/testar_seguranca.py   (Windows)
"""
import os
import subprocess
import sys
import tempfile
import time
import types

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.modules.setdefault("webview", types.SimpleNamespace(windows=[]))
import cogni  # noqa: E402

falhas = []


def check(nome, cond):
    print(("  OK     " if cond else "  FALHA  ") + nome)
    if not cond:
        falhas.append(nome)


pasta = tempfile.mkdtemp(prefix="cogni_seg_")
marcador = os.path.join(pasta, "executou.txt")


def limpar():
    if os.path.exists(marcador):
        os.remove(marcador)


print("Notificacao do Windows (titulo/mensagem nao confiaveis)")
payloads = [
    "`$(ni '%s')" % marcador,
    "$(ni '%s')" % marcador,
    "`\"; ni '%s' #" % marcador,
    "'; ni '%s'; '" % marcador,
    "</text></binding></visual></toast><x>& \"'",
]
for pl in payloads:
    limpar()
    cogni.WindowsNotifier.notify(pl, pl, sound=False)
    time.sleep(5)
    check("payload nao executa: %r" % pl[:30], not os.path.exists(marcador))

print("Abertura de arquivos (bloqueio de executaveis)")
bat = os.path.join(pasta, "poc.bat")
with open(bat, "w") as f:
    f.write('@echo x> "%s"\r\n' % marcador)
api = cogni.CogniDesktopAPI.__new__(cogni.CogniDesktopAPI)
for v in [bat, bat + " ", bat + ".", bat + " . ", bat + ":x.exe",
          bat.upper(), "javascript:alert(1)", "file:///c:/windows/system32/calc.exe",
          "ms-msdt:/id x", r"C:\Windows\System32\calc.exe"]:
    limpar()
    r = api.open_external_target(v)
    time.sleep(1.5)
    check("recusado e nao executa: %r" % v[-26:],
          r.get("status") == "error" and not os.path.exists(marcador))

print()
if falhas:
    print("FALHAS: %d" % len(falhas))
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")
