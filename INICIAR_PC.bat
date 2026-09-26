@echo off
REM =====================================================================
REM Anonimizador HC - Arranque en un PC con Windows (sin USB)
REM Comprueba Python, librerias, Ollama y modelos; pregunta antes de instalar.
REM Por Javier Casas Nebra
REM =====================================================================
setlocal
cd /d "%~dp0"
REM Segunda copia de este .bat lanzada en segundo plano: solo abre Chrome
if "%~1"==":abrir_chrome" goto abrir_chrome
title Anonimizador HC

echo.
echo ===================================================================
echo   ANONIMIZADOR DE HISTORIAS CLINICAS
echo   Por Javier Casas Nebra
echo ===================================================================
echo.

REM ---------- 1. Python ----------
echo [1/4] Comprobando Python...
python -c "import sys" >nul 2>&1
if not errorlevel 1 goto python_ok
echo.
echo  [!] No se ha encontrado Python en este ordenador.
choice /C SN /M " Quieres instalar Python 3.12 ahora? Unos 100 MB"
if errorlevel 2 goto sin_python
winget install -e --id Python.Python.3.12
echo.
echo  Python instalado. Cierra esta ventana y vuelve a abrir INICIAR_PC.bat
pause
exit /b 0
:sin_python
echo  Sin Python no se puede ejecutar el programa.
pause
exit /b 1
:python_ok

REM ---------- 2. Librerias de Python ----------
echo [2/4] Comprobando librerias...
python -c "import streamlit, pypdf, ollama, pypdfium2" >nul 2>&1
if not errorlevel 1 goto librerias_ok
echo.
echo  [!] Faltan librerias: streamlit, pypdf, ollama, reportlab, pypdfium2
choice /C SN /M " Quieres instalarlas ahora? Unos 150 MB"
if errorlevel 2 goto sin_librerias
python -m pip install --upgrade streamlit pypdf ollama reportlab pypdfium2
if errorlevel 1 goto sin_librerias
goto librerias_ok
:sin_librerias
echo  Sin estas librerias no se puede ejecutar el programa.
pause
exit /b 1
:librerias_ok

REM ---------- 3. Ollama (motor de IA) ----------
echo [3/4] Comprobando Ollama...
set "OLLAMA_EXE="
where ollama >nul 2>&1 && set "OLLAMA_EXE=ollama"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA_EXE goto ollama_instalado
echo.
echo  [!] Ollama, el motor de IA, no esta instalado.
echo      Sin Ollama el programa funciona, pero NO detecta nombres con IA.
choice /C SN /M " Quieres instalar Ollama ahora? Aprox. 1 GB"
if errorlevel 2 goto lanzar
winget install -e --id Ollama.Ollama
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA_EXE goto ollama_instalado
echo  No se pudo instalar automaticamente. Se abre la pagina de descarga.
start "" "https://ollama.com/download/windows"
goto lanzar
:ollama_instalado

"%OLLAMA_EXE%" list >nul 2>&1
if not errorlevel 1 goto ollama_activo
echo      Iniciando Ollama...
start "Ollama" /MIN "%OLLAMA_EXE%" serve
set /a INTENTOS=0
:esperar_ollama
timeout /t 2 /nobreak >nul
"%OLLAMA_EXE%" list >nul 2>&1
if not errorlevel 1 goto ollama_activo
set /a INTENTOS+=1
if %INTENTOS% LSS 15 goto esperar_ollama
echo  [!] Ollama no responde. El programa se abrira sin IA.
goto lanzar
:ollama_activo

REM ---------- 4. Modelos de IA ----------
echo [4/4] Comprobando modelos de IA...
"%OLLAMA_EXE%" list 2>nul | findstr /V /I /C:"NAME" /C:"embed" | findstr /R "[a-z]" >nul
if not errorlevel 1 goto lanzar
echo.
echo  [!] No hay ningun modelo de IA descargado.
echo      1 = qwen2.5:3b  - rapido, 1.9 GB - recomendado para ordenadores normales
echo      2 = qwen2.5:7b  - mas preciso, 4.7 GB - para ordenadores potentes
echo      3 = No descargar ahora
choice /C 123 /M " Que modelo quieres descargar?"
if errorlevel 3 goto lanzar
set "MODELO=qwen2.5:3b"
if errorlevel 2 set "MODELO=qwen2.5:7b"
"%OLLAMA_EXE%" pull %MODELO%

REM ---------- Arrancar el programa ----------
:lanzar
REM Evita que Streamlit pida un email en la primera ejecucion
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    mkdir "%USERPROFILE%\.streamlit" 2>nul
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

echo.
echo ===================================================================
echo   Abriendo el programa en el navegador...
echo   Si no se abre solo, entra en: http://localhost:8501
echo.
echo   NO CIERRES ESTA VENTANA mientras uses el programa.
echo   Para salir del todo: cierra el programa Y esta ventana negra.
echo ===================================================================
echo.
REM Abrir en Google Chrome si esta instalado; si no, en el navegador predeterminado
set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
set "HEADLESS=false"
if not defined CHROME goto arrancar_streamlit
set "HEADLESS=true"
start "" /B cmd /c ""%~f0" :abrir_chrome"
:arrancar_streamlit
python -m streamlit run "%~dp0anonimizador.py" --server.headless=%HEADLESS% --browser.gatherUsageStats=false --server.address=localhost --server.port=8501
pause
exit /b 0

REM ---------- Espera a que el programa responda y abre Chrome ----------
:abrir_chrome
set /a N=0
:esperar_servidor
curl -s -o nul http://localhost:8501/_stcore/health && goto chrome_listo
set /a N+=1
if %N% GEQ 60 exit /b 1
ping -n 2 127.0.0.1 >nul
goto esperar_servidor
:chrome_listo
REM --app: ventana propia sin pestanas ni barra de direcciones, como un programa
start "" "%CHROME%" --app=http://localhost:8501
exit /b 0
