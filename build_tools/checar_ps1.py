# -*- coding: utf-8 -*-
r"""
Cogni - checagem estrutural dos .ps1 sem executar PowerShell.

Uso:
    cd C:\Dev\cogni
    python build_tools\checar_ps1.py

NAO substitui rodar no Windows. Pega a classe de erro que mais aparece:
chave/parentese desbalanceado, string sem fechamento, param() fora de posicao
e byte nao-ASCII (que o PowerShell 5.1 transforma em aspa fantasma).
"""
import io
import os
import re
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALVOS = ['build.ps1', 'release.ps1']

problemas = []


def tirar_comentarios(texto):
    """Remove <# #>, comentario de linha e o conteudo das strings."""
    t = re.sub(r'<#.*?#>', '', texto, flags=re.S)
    saida = []
    for linha in t.splitlines():
        fora, aspas, i = [], None, 0
        while i < len(linha):
            c = linha[i]
            if aspas is None and c == '#':
                break
            if aspas is None and c in '"\'':
                aspas = c
            elif aspas == c:
                aspas = None
            fora.append(c)
            i += 1
        saida.append("".join(fora))
    return "\n".join(saida)


def estado_das_strings(src):
    """Maquina de estados fiel ao PowerShell. Devolve o estado no fim."""
    estado, i, inicio = 'fora', 0, None
    while i < len(src):
        c = src[i]
        if estado == 'fora':
            if src.startswith('<#', i):
                estado = 'bloco'; i += 2; continue
            if c == '#':
                estado = 'coment'; i += 1; continue
            if c == '`':
                i += 2; continue
            if c == '"':
                estado = 'dq'; inicio = src[:i].count('\n') + 1; i += 1; continue
            if c == "'":
                estado = 'sq'; inicio = src[:i].count('\n') + 1; i += 1; continue
        elif estado == 'dq':
            if c == '`':
                i += 2; continue
            if c == '"':
                estado = 'fora'; i += 1; continue
        elif estado == 'sq':
            if c == "'":
                if src.startswith("''", i):
                    i += 2; continue
                estado = 'fora'; i += 1; continue
        elif estado == 'coment':
            if c == '\n':
                estado = 'fora'
        elif estado == 'bloco':
            if src.startswith('#>', i):
                estado = 'fora'; i += 2; continue
        i += 1
    return estado, inicio


def checar(nome):
    caminho = os.path.join(RAIZ, nome)
    if not os.path.exists(caminho):
        problemas.append("%s nao encontrado" % nome)
        return

    print()
    print("=" * 66)
    print(" " + nome)
    print("=" * 66)

    bruto = open(caminho, 'rb').read()

    # ---- ASCII puro (a armadilha do travessao / Windows-1252)
    fora = [b for b in bruto if b > 127]
    tem_bom = bruto.startswith(b'\xef\xbb\xbf')
    if fora and not tem_bom:
        i = next(i for i, b in enumerate(bruto) if b > 127)
        linha = bruto[:i].count(b'\n') + 1
        print("  FALHA  %d byte(s) nao-ASCII, 1o na linha %d" % (len(fora), linha))
        print("         O PowerShell 5.1 leria como Windows-1252 e cada acento")
        print("         viraria uma aspa fantasma. Use ASCII ou grave com BOM.")
        problemas.append("%s: %d byte(s) nao-ASCII" % (nome, len(fora)))
    else:
        print("  OK     ASCII puro" if not fora else "  OK     nao-ASCII, mas com BOM")

    src = bruto.decode('utf-8', 'replace')

    # ---- strings balanceadas
    estado, inicio = estado_das_strings(src)
    if estado != 'fora':
        print("  FALHA  string/comentario ABERTO desde a linha %s (estado: %s)"
              % (inicio, estado))
        problemas.append("%s: string sem fechamento (linha %s)" % (nome, inicio))
    else:
        print("  OK     strings balanceadas")

    # ---- chaves, parenteses, colchetes
    codigo = tirar_comentarios(src)
    sem_str = re.sub(r'"(?:[^"`]|`.)*"', '""', codigo)
    sem_str = re.sub(r"'[^']*'", "''", sem_str)
    for ab, fe, rot in (('{', '}', 'chaves'),
                        ('(', ')', 'parenteses'),
                        ('[', ']', 'colchetes')):
        a, f = sem_str.count(ab), sem_str.count(fe)
        if a != f:
            print("  FALHA  %-11s abre=%d fecha=%d" % (rot, a, f))
            problemas.append("%s: %s desbalanceados (%d/%d)" % (nome, rot, a, f))
        else:
            print("  OK     %-11s %d / %d" % (rot, a, f))

    # ---- param() primeiro
    limpo = re.sub(r'(?m)^\s*#.*$', '', src).strip()
    if limpo.startswith('param('):
        print("  OK     param() primeiro statement")
    else:
        print("  FALHA  param() nao e o primeiro statement")
        problemas.append("%s: param() fora de posicao" % nome)

    # ---- funcoes definidas antes do uso
    for m in re.finditer(r'^function ([\w-]+)', src, re.M):
        f = m.group(1)
        pos_def = codigo.find("function " + f)
        usos = [x.start() for x in
                re.finditer(r'(?<![\w-])%s(?![\w-])' % re.escape(f), codigo)]
        if pos_def >= 0 and any(u < pos_def for u in usos):
            print("  FALHA  %s e chamada antes de ser definida" % f)
            problemas.append("%s: %s usada antes de definida" % (nome, f))


def main():
    print()
    print("#" * 66)
    print("#  Cogni - checagem estrutural dos scripts PowerShell")
    print("#" * 66)

    for a in ALVOS:
        checar(a)

    print()
    print("=" * 66)
    if problemas:
        print(" %d PROBLEMA(S)" % len(problemas))
        for p in problemas:
            print("   x " + p)
    else:
        print(" Nenhum problema estrutural.")
        print()
        print(" Lembrete: isto NAO executa PowerShell. Rode com -DryRun antes")
        print(" de valer de verdade.")
    print("=" * 66)
    print()
    return 1 if problemas else 0


if __name__ == '__main__':
    sys.exit(main())
