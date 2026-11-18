# -*- mode: python ; coding: utf-8 -*-
from datetime import datetime
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

build_name = os.environ.get("BIOEM_BUILD_NAME")
if not build_name:
    build_name = "Preview_beta-" + datetime.now().strftime("%Y%b%d").upper()

datas = [
    ("bioem_app.ico", "."),
    ("BioEM_Watcher.ijm", "."),
]
datas += collect_data_files("customtkinter")

hiddenimports = [
    "bioem_config",
    "nas_indexer",
    "bioem_torch_align",
    "scipy.sparse.linalg",
    "scipy.sparse.csgraph._validation",
    "tifffile",
]
hiddenimports += collect_submodules("scipy.sparse")
hiddenimports += collect_submodules("scipy._lib")
try:
    hiddenimports += collect_submodules(
        "scipy._external",
        filter=lambda name: "dask" not in name and "cupy" not in name,
    )
except Exception:
    pass
try:
    hiddenimports += collect_submodules("imagecodecs")
except Exception:
    pass

a = Analysis(
    ["pull_from_NAS.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "jupyter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=build_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="bioem_app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=build_name,
)
