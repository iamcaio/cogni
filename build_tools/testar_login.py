# -*- coding: utf-8 -*-
"""
Testes do fluxo de login/cadastro/sessao, SEM rede e SEM conta real.
O Firebase e simulado; nada e enviado a producao.

Uso:  python build_tools/testar_login.py
"""
import json
import os
import sys
import tempfile
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


resposta = {}


def fake_http(url, payload=None, method="GET", timeout=15, ctx=None,
              permitir_fallback=False):
    for chave, val in resposta.items():
        if chave in url:
            return val
    return 0, {"error": {"message": "sem rota"}}


cogni._http_json = fake_http
ok_auth = (200, {"idToken": "t", "refreshToken": "r", "localId": "uid1",
                 "email": "a@b.com", "expiresIn": "3600"})


def novo():
    return cogni.CloudSync(tempfile.mkdtemp())


print("Cadastro / entrada")
c = novo()
check("e-mail vazio recusado", c.entrar("", "x")["status"] == "error")
check("e-mail sem @ recusado", c.criar_conta("abc", "123456")["message"] == "E-mail inválido.")
check("senha curta no cadastro recusada sem rede",
      "6 caracteres" in c.criar_conta("a@b.com", "123")["message"])
resposta.clear(); resposta["signUp"] = ok_auth
r = c.criar_conta("  a@b.com  ", "123456")
check("cadastro valido aceita e-mail com espacos", r["status"] == "success" and c.logado)
resposta.clear()
resposta["signInWithPassword"] = (400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
check("senha errada: mensagem amigavel",
      novo().entrar("a@b.com", "errada")["message"] == "E-mail ou senha incorretos.")
resposta["signInWithPassword"] = (400, {"error": {"message":
    "TOO_MANY_ATTEMPTS_TRY_LATER : texto novo qualquer do servidor"}})
check("codigo com texto diferente ainda traduzido (prefixo)",
      "Muitas tentativas" in novo().entrar("a@b.com", "123456")["message"])
resposta["signInWithPassword"] = (400, {"error": {"message":
    "OPERATION_NOT_ALLOWED : Password sign-in is disabled for this project."}})
check("OPERATION_NOT_ALLOWED traduzido",
      "indisponível" in novo().entrar("a@b.com", "123456")["message"])
resposta["signInWithPassword"] = (0, {"error": {"message": "URLError: timed out"}})
check("sem internet: mensagem clara",
      "Sem conexão" in novo().entrar("a@b.com", "123456")["message"])

print("Sessao salva (usuario ja logado ao abrir o app)")
c = novo()
resposta.clear(); resposta["signUp"] = ok_auth
c.criar_conta("a@b.com", "123456")
c2 = cogni.CloudSync(os.path.dirname(c.session_file))  # reabre o app
check("sessao restaurada do disco", c2.logado)
resposta.clear()
resposta["securetoken"] = (400, {"error": {"message": "TOKEN_EXPIRED"}})
check("refreshToken recusado => 'invalida'", c2.validar_sessao() == "invalida")
resposta.clear()
c3 = cogni.CloudSync(os.path.dirname(c.session_file))
resposta["securetoken"] = (0, {"error": {"message": "URLError"}})
check("sem rede => 'offline' (nao derruba a sessao)",
      c3.validar_sessao() == "offline" and c3.logado)
resposta.clear()
c4 = cogni.CloudSync(os.path.dirname(c.session_file))
resposta["securetoken"] = (200, {"id_token": "n", "refresh_token": "r2",
                                 "user_id": "uid1", "expires_in": "3600"})
check("refresh valido => 'ok'", c4.validar_sessao() == "ok")

print("API: sessao morta volta para a tela de login")
d = tempfile.mkdtemp()
api = cogni.CogniDesktopAPI.__new__(cogni.CogniDesktopAPI)
api._base_dir = d
api._cloud = cogni.CloudSync(d)
api._ultimo_sync = None
api.local_path = os.path.join(d, "x", "database.json")
resposta.clear(); resposta["signUp"] = ok_auth
api._cloud.criar_conta("a@b.com", "123456")
api._cloud._sessao_validada = False
api._cloud.id_token = None  # app recem-aberto: so ha o refreshToken do disco
resposta.clear()
resposta["securetoken"] = (400, {"error": {"message": "INVALID_REFRESH_TOKEN"}})
st = api.cloud_status()
check("cloud_status descarta sessao morta (signedIn=False)", st["signedIn"] is False)
check("arquivo de sessao removido", not os.path.exists(api._cloud.session_file))

print()
if falhas:
    print("FALHAS: %d" % len(falhas))
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")
