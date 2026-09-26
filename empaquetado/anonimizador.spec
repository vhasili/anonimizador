# -*- mode: python ; coding: utf-8 -*-
# Receta de PyInstaller para Windows y Mac. Se usa desde construir_windows.bat / construir_mac.sh.
# IMPORTANTE: nunca se empaquetan los datos del usuario (diccionario, exclusiones, configuración),
# porque contienen nombres reales; la app los guarda en la carpeta del usuario de cada ordenador.
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

RAIZ = os.path.abspath(os.path.join(SPECPATH, ".."))
ES_MAC = sys.platform == "darwin"

datas = [
    (os.path.join(RAIZ, "anonimizador.py"), "."),
    (os.path.join(RAIZ, "componentes"), "componentes"),
]
binaries = []
hiddenimports = [
    # lo que usa anonimizador.py (Streamlit lo ejecuta como archivo, PyInstaller no lo analiza)
    "difflib", "hashlib", "unicodedata", "zipfile", "tempfile", "concurrent.futures",
    "streamlit.components.v1",
]
datas += collect_data_files("streamlit") + copy_metadata("streamlit")
hiddenimports += collect_submodules("streamlit")
for paquete in ("pypdf", "ollama", "reportlab", "pypdfium2", "pypdfium2_raw"):
    d, b, h = collect_all(paquete)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SPECPATH, "lanzador.py")],
    pathex=[RAIZ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["IPython", "notebook", "matplotlib", "scipy", "torch", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Anonimizador",
    console=False,
    icon=os.path.join(SPECPATH, "icono.png" if ES_MAC else "icono.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Anonimizador")

if ES_MAC:
    app = BUNDLE(
        coll,
        name="Anonimizador.app",
        icon=os.path.join(SPECPATH, "icono.png"),
        bundle_identifier="es.hula.anonimizador",
        info_plist={
            "CFBundleDisplayName": "Anonimizador HC",
            "CFBundleShortVersionString": "1.0",
            "NSHighResolutionCapable": True,
        },
    )
