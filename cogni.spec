# -*- mode: python ; coding: utf-8 -*-
import certifi

a = Analysis(
    ['cogni.py'],
    pathex=[],
    binaries=[],
    # 'vendor' entra inteiro: Tailwind, Font Awesome, Chart.js e Inter
    # empacotados junto do .exe, para a interface abrir sem internet
    # (ver _recursos_dir()/_vendor_uri() em cogni.py).
    datas=[(certifi.where(), 'certifi'), ('vendor', 'vendor')],
    hiddenimports=['certifi', 'webview'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='COGNI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['cogni.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='COGNI',
)