# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import onvif

root = Path(SPEC).resolve().parent
site_dir = Path(onvif.__file__).resolve().parent.parent
datas = [
    (str(root / "web"), "web"),
    (str(site_dir / "wsdl"), "wsdl"),
]

a = Analysis(
    ["app.py"],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=["serial", "pystray._win32", "PIL._tkinter_finder"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="XgfzjRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True, name="XgfzjRecorder")
