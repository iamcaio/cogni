# -*- coding: utf-8 -*-
r"""
Corrige a funcao _urlSegura do cogni.py e remove bytes de controle.

Uso:
    cd C:\Dev\cogni
    python build_tools\corrigir_urlsegura.py

POR QUE ESTE SCRIPT EXISTE

Ao escrever a sanitizacao anti-XSS, eu precisava de uma expressao que removesse
espacos e caracteres de controle de uma URL, para barrar o truque classico
"java<TAB>script:alert(1)".

A forma natural em JavaScript e um intervalo de classe de caractere com escapes
Unicode. A ferramenta que usei para editar o arquivo converteu esses escapes
nos CARACTERES LITERAIS correspondentes - incluindo o byte 0x00. E o Python
recusa qualquer arquivo-fonte que contenha byte nulo:

    SyntaxError: source code cannot contain null bytes

Isso aconteceu tres vezes neste projeto. A solucao definitiva nao e escrever o
escape "do jeito certo": e NAO USAR intervalo de caracteres. A versao abaixo
compara codigos numericos num laco - explicita, legivel, e sem nenhum
caractere invisivel no codigo.

O script e idempotente: rodar duas vezes nao causa dano.
"""
import io
import os
import re
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALVO = os.path.join(RAIZ, 'cogni.py')

# Implementacao nova, sem intervalo de caracteres.
NOVA = b"""            var limpo = '';
            var _s = String(v || '');
            for (var _i = 0; _i < _s.length; _i++) {
                // Descarta tudo com codigo <= 32: caracteres de controle
                // (tab, nova linha, nulo) e o espaco. Sem intervalo de
                // classe, sem caractere invisivel no codigo-fonte.
                if (_s.charCodeAt(_i) > 32) { limpo += _s.charAt(_i); }
            }
"""


def main():
    if not os.path.exists(ALVO):
        print("cogni.py nao encontrado em %s" % RAIZ)
        return 2

    bruto = open(ALVO, 'rb').read()
    original = bruto

    # ---------------------------------------------------- diagnostico
    controles = {}
    for b in bruto:
        if b < 32 and b not in (9, 10, 13):
            controles[b] = controles.get(b, 0) + 1
    print("bytes de controle encontrados: %s"
          % ({hex(k): v for k, v in controles.items()} or "nenhum"))

    # ------------------------------- 1. troca a linha do replace(/[...]/g)
    # Casa a linha inteira do `const limpo = ... .replace(/[qualquer coisa]/g, '')`
    # incluindo o caso em que ha bytes de controle literais dentro da classe.
    padrao = re.compile(
        rb"[ \t]*(?:const|let|var)\s+limpo\s*=\s*String\(v \|\| ''\)"
        rb"\.replace\(/\[[^\]]{0,20}\]/g, ''\);[ \t]*\r?\n")

    achados = padrao.findall(bruto)
    if achados:
        bruto = padrao.sub(NOVA, bruto, count=1)
        print("linha do _urlSegura substituida (%d ocorrencia)" % len(achados))
    else:
        if b'_s.charCodeAt(_i) > 32' in bruto:
            print("_urlSegura ja esta na versao corrigida")
        else:
            print("AVISO: nao encontrei a linha esperada do _urlSegura.")
            print("       Confira manualmente a funcao no cogni.py.")

    # --------------------------------- 2. remove bytes de controle restantes
    limpos = bytearray()
    removidos = 0
    for b in bruto:
        if b < 32 and b not in (9, 10, 13):
            removidos += 1
            continue
        limpos.append(b)
    bruto = bytes(limpos)
    if removidos:
        print("%d byte(s) de controle removido(s)" % removidos)

    # -------------------------------------------- 3. BOM e newline final
    if bruto.startswith(b'\xef\xbb\xbf'):
        bruto = bruto[3:]
        print("BOM UTF-8 removido")
    if not bruto.endswith(b'\n'):
        bruto += b'\n'
        print("quebra de linha final adicionada")

    if bruto == original:
        print("\nnada a mudar.")
        return 0

    # --------------------------------------------------- 4. grava e valida
    backup = ALVO + '.antes-correcao'
    if not os.path.exists(backup):
        open(backup, 'wb').write(original)
        print("backup: %s" % os.path.basename(backup))

    open(ALVO, 'wb').write(bruto)

    try:
        compile(bruto, ALVO, 'exec')
        print("\ncogni.py compila: OK")
    except (SyntaxError, ValueError) as e:
        print("\nFALHA: o arquivo nao compila: %s: %s" % (type(e).__name__, e))
        print("Restaurando o backup.")
        open(ALVO, 'wb').write(original)
        return 1

    # confirma que o JS da sanitizacao ficou coerente
    texto = bruto.decode('utf-8', 'replace')
    for nome, padrao_ in [
        ('function sanitizarHtml', r'function sanitizarHtml'),
        ('function _urlSegura', r'function _urlSegura'),
        ('laco charCodeAt', r'charCodeAt\(_i\) > 32'),
        ('checagem de on*', r"nome\.startsWith\('on'\)"),
        ('parse via template', r"createElement\('template'\)"),
        ('intercepta colar', r"addEventListener\('paste'"),
    ]:
        print("  %-22s %s" % (nome, "ok" if re.search(padrao_, texto) else "AUSENTE"))

    print("\nPronto.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
