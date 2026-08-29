# -*- mode: python ; coding: utf-8 -*-
import sys


a = Analysis(
    ['psx_winctrl_pfp.py'],
    pathex=[],
    binaries=[],
    datas=[('psx.ico', '.')] if sys.platform == 'win32' else [],
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
    name='psx_winctrl_pfp',
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
    icon=['psx.ico'] if sys.platform == 'win32' else ['psx.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='psx_winctrl_pfp',
)
app = BUNDLE(
    coll,
    name='psx_winctrl_pfp.app',
    icon='psx.icns',
    bundle_identifier=None,
)
