# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH).parent

a = Analysis(
    [str(project_root / "ccswitch_batch_sender.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "assets"), "assets")],
    hiddenimports=[
        "ccswitch_batch_sender_ui",
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.ttk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "pytest", "numpy", "pandas"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CCSwitchBatchSender",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project_root / "assets" / "app.ico"),
    version=str(project_root / "packaging" / "version_info.txt"),
)
