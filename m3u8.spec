# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['m3u8.py'],
    pathex=[],
    binaries=[('exe/ffmpeg.exe', 'exe'), ('exe/N_m3u8DL-RE.exe', 'exe')],
    datas=[('lioil.ico', '.'), ('exe', 'exe'), ('browsers', 'browsers')],
    hiddenimports=[],
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
    name='m3u8',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['lioil.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='m3u8',
)
