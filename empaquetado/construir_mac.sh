#!/bin/bash
# =====================================================================
# Construye la app de Mac: dist/Anonimizador_Mac.dmg
# Hay que ejecutarlo EN UN MAC (desde Windows no se puede crear una app de Mac).
# Uso: abre Terminal, escribe  bash  , arrastra este archivo a la ventana y pulsa Intro.
# Requiere Python 3.10+ de python.org (trae tkinter, necesario para la ventana de control).
# La app sale para el tipo de procesador del Mac donde se construye
# (Apple Silicon M1/M2/M3... o Intel): constrúyela en un Mac del mismo tipo que los que la usarán.
# =====================================================================
set -e
cd "$(dirname "$0")"

PY=python3
if ! $PY -c "import tkinter" 2>/dev/null; then
  echo "*** Falta Python con tkinter. Instala Python 3.12 desde https://www.python.org/downloads/macos/ y repite. ***"
  exit 1
fi

echo "[1/5] Preparando entorno de construccion (solo la primera vez tarda)..."
[ -d venv_build_mac ] || $PY -m venv venv_build_mac
source venv_build_mac/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requisitos.txt

echo "[2/5] Generando icono..."
python generar_icono.py

echo "[3/5] Empaquetando (unos minutos)..."
pyinstaller --noconfirm --clean --log-level WARN anonimizador.spec

echo "[4/5] Comprobando la app..."
rm -f dist/diagnostico.txt
"dist/Anonimizador.app/Contents/MacOS/Anonimizador" --diagnostico "$PWD/dist/diagnostico.txt"
cat dist/diagnostico.txt; echo
grep -q "DIAGNOSTICO OK" dist/diagnostico.txt

echo "[5/5] Creando el DMG para repartir..."
rm -rf dist/dmg dist/Anonimizador_Mac.dmg
mkdir -p dist/dmg
cp -R dist/Anonimizador.app dist/dmg/
cp LEEME_APP.txt dist/dmg/LEEME.txt
ln -s /Applications dist/dmg/Aplicaciones
hdiutil create -quiet -volname "Anonimizador HC" -srcfolder dist/dmg -ov -format UDZO dist/Anonimizador_Mac.dmg

echo
echo "LISTO: $PWD/dist/Anonimizador_Mac.dmg"
