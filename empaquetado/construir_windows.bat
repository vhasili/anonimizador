@echo off
REM =====================================================================
REM Construye la app de Windows: dist\Anonimizador_Windows.zip
REM Requiere Python 3.10+ instalado. Solo hace falta en el PC que prepara la app.
REM =====================================================================
setlocal
cd /d "%~dp0"

echo [1/5] Preparando entorno de construccion (solo la primera vez tarda)...
if not exist venv_build python -m venv venv_build || goto error
call venv_build\Scripts\activate.bat || goto error
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requisitos.txt || goto error

echo [2/5] Generando icono...
python generar_icono.py || goto error

echo [3/5] Empaquetando (unos minutos)...
pyinstaller --noconfirm --clean --log-level WARN anonimizador.spec || goto error

echo [4/5] Comprobando la app...
if exist dist\diagnostico.txt del dist\diagnostico.txt
start "" /wait "dist\Anonimizador\Anonimizador.exe" --diagnostico "%CD%\dist\diagnostico.txt"
type dist\diagnostico.txt
echo.
findstr /C:"DIAGNOSTICO OK" dist\diagnostico.txt >nul || goto error

echo [5/5] Creando el ZIP para repartir...
copy /Y LEEME_APP.txt dist\Anonimizador\LEEME.txt >nul
powershell -NoProfile -Command "Compress-Archive -Path 'dist\Anonimizador' -DestinationPath 'dist\Anonimizador_Windows.zip' -Force" || goto error

echo.
echo LISTO: %CD%\dist\Anonimizador_Windows.zip
pause
exit /b 0

:error
echo.
echo *** Ha fallado la construccion. Revisa los mensajes de arriba. ***
pause
exit /b 1
