"""
Anonimizador de Historias Clínicas · app de escritorio (Windows y Mac)
Por Javier Casas Nebra

Ventana de control que:
  1. comprueba el motor de IA (Ollama) y, si falta, pregunta antes de instalarlo,
  2. comprueba que haya un modelo y, si no, pregunta cuál descargar,
  3. arranca el anonimizador (Streamlit) solo para este ordenador y lo abre en su propia ventana.

Ollama y los modelos NO van dentro de la app (pesan varios GB): se instalan bajo demanda.
Si junto a la app hay carpetas 'ollama' y 'modelos' (USB portátil), se usan esas.
Si junto a la app hay una carpeta 'datos', el diccionario y la configuración se guardan ahí.
"""
import os
import sys
import json
import time
import queue
import shutil
import socket
import tempfile
import threading
import subprocess
import urllib.request
import webbrowser

NOMBRE_APP = "AnonimizadorHC"
TITULO = "Anonimizador de Historias Clínicas"
ES_WIN = os.name == "nt"
ES_MAC = sys.platform == "darwin"
OLLAMA_URL = "http://127.0.0.1:11434"
MODELOS = [
    ("qwen2.5:3b", "Rápido · 1,9 GB · recomendado para ordenadores normales"),
    ("qwen2.5:7b", "Más preciso · 4,7 GB · para ordenadores potentes"),
]
MODELO_OCR = ("qwen2.5vl:3b", "3,2 GB")  # lector de PDF escaneados (modelo de visión)
URL_OLLAMA_WIN ="https://ollama.com/download/OllamaSetup.exe"
URL_OLLAMA_MAC = "https://ollama.com/download/Ollama-darwin.zip"
URL_OLLAMA_WEB = "https://ollama.com/download"
SIN_VENTANA = subprocess.CREATE_NO_WINDOW if ES_WIN else 0


# ==================== CARPETAS ====================
def carpeta_recursos():
    """Donde están anonimizador.py y componentes/ (dentro de la app, o el proyecto en modo código)."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def carpeta_app():
    """Carpeta donde el usuario tiene la app (la que contiene el .exe o el .app)."""
    if getattr(sys, "frozen", False):
        exe = os.path.dirname(sys.executable)
        if ES_MAC and exe.endswith(os.path.join("Contents", "MacOS")):
            return os.path.abspath(os.path.join(exe, "..", "..", ".."))
        return exe
    return carpeta_recursos()


def carpeta_datos():
    portable = os.path.join(carpeta_app(), "datos")
    if os.path.isdir(portable):
        return portable
    if ES_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif ES_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.path.expanduser("~/.local/share")
    carpeta = os.path.join(base, NOMBRE_APP)
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def es_portable():
    return os.path.isdir(os.path.join(carpeta_app(), "datos"))


def leer_ajustes():
    try:
        with open(os.path.join(carpeta_datos(), "lanzador.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_ajustes(ajustes):
    try:
        with open(os.path.join(carpeta_datos(), "lanzador.json"), "w", encoding="utf-8") as f:
            json.dump(ajustes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ==================== UTILIDADES ====================
def proceso_vivo(pid):
    if ES_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        codigo = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(codigo))
        k32.CloseHandle(h)
        return codigo.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def url_responde(url, espera=2):
    try:
        with urllib.request.urlopen(url, timeout=espera) as r:
            return r.status == 200
    except Exception:
        return False


def puerto_libre(inicio=8501):
    for p in range(inicio, inicio + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("No hay ningún puerto libre")


def descargar(url, destino, al_avanzar):
    with urllib.request.urlopen(url, timeout=30) as r, open(destino, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        hecho = 0
        while True:
            bloque = r.read(1 << 20)
            if not bloque:
                break
            f.write(bloque)
            hecho += len(bloque)
            al_avanzar(hecho, total)


def mb(n):
    return f"{n / 1e6:,.0f} MB".replace(",", ".")


# ==================== OLLAMA ====================
def buscar_ollama():
    nombre = "ollama.exe" if ES_WIN else "ollama"
    candidatos = [os.path.join(carpeta_app(), "ollama", nombre), shutil.which("ollama")]
    if ES_WIN:
        candidatos.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"))
    if ES_MAC:
        candidatos += ["/Applications/Ollama.app/Contents/Resources/ollama",
                       os.path.expanduser("~/Applications/Ollama.app/Contents/Resources/ollama"),
                       "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"]
    return next((c for c in candidatos if c and os.path.isfile(c)), None)


def app_ollama_mac():
    for c in ("/Applications/Ollama.app", os.path.expanduser("~/Applications/Ollama.app")):
        if os.path.isdir(c):
            return c
    return None


def modelos_instalados():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
            datos = json.load(r)
        return [m.get("name") or m.get("model") for m in datos.get("models", [])]
    except Exception:
        return []


def es_modelo_ocr(nombre):
    return (nombre or "").startswith("qwen2.5vl")


def descargar_modelo(nombre, al_avanzar):
    peticion = urllib.request.Request(
        OLLAMA_URL + "/api/pull", data=json.dumps({"model": nombre, "stream": True}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(peticion, timeout=60) as r:
        for linea in r:
            if not linea.strip():
                continue
            ev = json.loads(linea)
            if ev.get("error"):
                raise RuntimeError(ev["error"])
            al_avanzar(ev.get("status", ""), ev.get("completed") or 0, ev.get("total") or 0)
            if ev.get("status") == "success":
                return


# ==================== MODO SERVIDOR ====================
def modo_servidor(puerto, pid_padre):
    """Proceso hijo: ejecuta Streamlit con el anonimizador. Se cierra solo si se cierra la ventana de control."""
    if sys.stdout is None or sys.stderr is None:  # app sin consola
        registro = open(os.path.join(tempfile.gettempdir(), "anonimizador_servidor.log"), "w",
                        encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or registro
        sys.stderr = sys.stderr or registro

    def vigilar():
        while proceso_vivo(pid_padre):
            time.sleep(2)
        os._exit(0)
    threading.Thread(target=vigilar, daemon=True).start()

    from streamlit.web import cli as stcli
    sys.argv = ["streamlit", "run", os.path.join(carpeta_recursos(), "anonimizador.py"),
                "--global.developmentMode=false",
                "--server.headless=true",
                "--server.address=localhost",   # solo accesible desde este ordenador
                f"--server.port={puerto}",
                "--server.fileWatcherType=none",
                "--browser.gatherUsageStats=false",
                "--client.toolbarMode=minimal"]
    sys.exit(stcli.main())


def modo_diagnostico(salida):
    """Comprueba que la app lleva todo lo necesario (lo usan los scripts de construcción).
    Escribe el resultado en el archivo `salida` (la app no tiene consola)."""
    try:
        import streamlit, pypdf, ollama, pypdfium2  # noqa: F401
        from reportlab.platypus import SimpleDocTemplate  # noqa: F401
        import tkinter
        ventana = tkinter.Tk()  # comprueba que Tcl/Tk va dentro de la app (sin mostrar nada)
        ventana.withdraw()
        ventana.update()
        ventana.destroy()
        rec = carpeta_recursos()
        for f in ("anonimizador.py", os.path.join("componentes", "revisor", "index.html")):
            if not os.path.isfile(os.path.join(rec, f)):
                raise RuntimeError(f"falta {f}")
        resultado = f"DIAGNOSTICO OK · streamlit {streamlit.__version__}"
    except Exception as e:
        resultado = f"DIAGNOSTICO FALLO · {type(e).__name__}: {e}"
    with open(salida, "w", encoding="utf-8") as f:
        f.write(resultado)


# ==================== NAVEGADOR ====================
def abrir_ventana(url):
    """Abre el programa en una ventana propia (Chrome o Edge en modo aplicación) o en el navegador."""
    if ES_MAC:
        if os.path.isdir("/Applications/Google Chrome.app"):
            subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}"])
            return
    elif ES_WIN:
        pf, pf86, local = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
        for exe in (os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
                    os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
                    os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
                    os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
                    os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe")):
            if os.path.isfile(exe):
                subprocess.Popen([exe, f"--app={url}"], creationflags=SIN_VENTANA)
                return
    webbrowser.open(url)


def crear_acceso_directo_windows():
    destino = sys.executable
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Anonimizador HC.lnk'));"
          f"$s.TargetPath='{destino}';$s.WorkingDirectory='{os.path.dirname(destino)}';"
          f"$s.IconLocation='{destino},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=SIN_VENTANA, timeout=30)


# ==================== VENTANA DE CONTROL ====================
class Lanzador:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        if ES_WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.raiz = tk.Tk()
        self.raiz.title(TITULO)
        self.raiz.resizable(False, False)
        self.raiz.protocol("WM_DELETE_WINDOW", self.al_cerrar)
        marco = ttk.Frame(self.raiz, padding=22)
        marco.pack(fill="both", expand=True)
        ttk.Label(marco, text=TITULO, font=("Segoe UI" if ES_WIN else "Helvetica", 15, "bold")).pack(anchor="w")
        ttk.Label(marco, text="Por Javier Casas Nebra · 100% local", foreground="#666").pack(anchor="w", pady=(0, 14))
        self.estado = ttk.Label(marco, text="Preparando…", width=58)
        self.estado.pack(anchor="w")
        self.barra = ttk.Progressbar(marco, length=440, mode="indeterminate")
        self.barra.pack(fill="x", pady=8)
        self.barra.start(12)
        self.detalle = ttk.Label(marco, text="", foreground="#666", width=58)
        self.detalle.pack(anchor="w")
        botones = ttk.Frame(marco)
        botones.pack(fill="x", pady=(16, 0))
        self.b_abrir = ttk.Button(botones, text="Abrir el programa", command=self.abrir, state="disabled")
        self.b_abrir.pack(side="left")
        ttk.Button(botones, text="Salir", command=self.al_cerrar).pack(side="right")

        self.cola = queue.Queue()
        self.servidor = None
        self.ollama_propio = None
        self.url = None
        self.raiz.after(100, self.atender_cola)
        threading.Thread(target=self.preparar, daemon=True).start()

    # --- comunicación hilo de trabajo <-> ventana (tkinter solo desde el hilo principal) ---
    def en_ventana(self, funcion, *args):
        self.cola.put((funcion, args, None))

    def preguntar(self, funcion, *args):
        listo, caja = threading.Event(), {}
        self.cola.put((funcion, args, (listo, caja)))
        listo.wait()
        return caja.get("r")

    def atender_cola(self):
        try:
            while True:
                funcion, args, espera = self.cola.get_nowait()
                r = funcion(*args)
                if espera:
                    espera[1]["r"] = r
                    espera[0].set()
        except queue.Empty:
            pass
        self.raiz.after(100, self.atender_cola)

    def poner_estado(self, texto, detalle=""):
        self.en_ventana(lambda: (self.estado.config(text=texto), self.detalle.config(text=detalle)))

    def poner_progreso(self, hecho, total):
        def f():
            if total:
                if self.barra["mode"] != "determinate":
                    self.barra.stop()
                    self.barra.config(mode="determinate", maximum=100)
                self.barra["value"] = 100 * hecho / total
            elif self.barra["mode"] != "indeterminate":
                self.barra.config(mode="indeterminate")
                self.barra.start(12)
        self.en_ventana(f)

    def si_no(self, titulo, texto):
        from tkinter import messagebox
        return self.preguntar(lambda: messagebox.askyesno(titulo, texto, parent=self.raiz))

    def aviso(self, titulo, texto):
        from tkinter import messagebox
        return self.preguntar(lambda: messagebox.showinfo(titulo, texto, parent=self.raiz))

    def elegir_modelo(self, ofrecer_ocr):
        """Diálogo con los modelos recomendados (y el lector OCR). Devuelve la lista a descargar."""
        def dialogo():
            tk, ttk = self.tk, self.ttk
            d = tk.Toplevel(self.raiz)
            d.title("Modelo de IA")
            d.transient(self.raiz)
            d.grab_set()
            m = ttk.Frame(d, padding=20)
            m.pack()
            ttk.Label(m, text="No hay ningún modelo de IA descargado.\n¿Cuál quieres descargar? (solo una vez)",
                      justify="left").pack(anchor="w", pady=(0, 10))
            eleccion = tk.StringVar(value=MODELOS[0][0])
            for nombre, desc in MODELOS:
                ttk.Radiobutton(m, text=f"{nombre}  ·  {desc}", value=nombre, variable=eleccion).pack(anchor="w")
            con_ocr = tk.BooleanVar(value=True)
            if ofrecer_ocr:
                ttk.Checkbutton(m, variable=con_ocr,
                                text=f"Y también el lector de PDF escaneados (OCR) · {MODELO_OCR[1]}"
                                ).pack(anchor="w", pady=(10, 0))
            resultado = {"r": []}

            def aceptar():
                resultado["r"] = [eleccion.get()] + ([MODELO_OCR[0]] if ofrecer_ocr and con_ocr.get() else [])
                d.destroy()
            fila = ttk.Frame(m)
            fila.pack(fill="x", pady=(14, 0))
            ttk.Button(fila, text="Descargar", command=aceptar).pack(side="left")
            ttk.Button(fila, text="Ahora no (sin IA)", command=d.destroy).pack(side="right")
            self.raiz.wait_window(d)
            return resultado["r"]
        return self.preguntar(dialogo)

    # --- pasos ---
    def preparar(self):
        try:
            datos = carpeta_datos()
            if self.instancia_abierta(datos):
                return
            self.preparar_ollama()
            self.arrancar_programa(datos)
        except Exception as e:
            self.poner_estado("✖ No se ha podido arrancar", str(e)[:200])
            self.poner_progreso(0, 1)

    def instancia_abierta(self, datos):
        """Si ya hay un anonimizador abierto, en vez de arrancar otro se muestra ese."""
        try:
            with open(os.path.join(datos, "instancia.json"), encoding="utf-8") as f:
                inst = json.load(f)
        except Exception:
            return False
        url = f"http://localhost:{inst.get('puerto')}"
        if proceso_vivo(int(inst.get("pid", 0))) and url_responde(url + "/_stcore/health"):
            abrir_ventana(url)
            self.en_ventana(self.raiz.destroy)
            return True
        return False

    def preparar_ollama(self):
        self.poner_estado("Comprobando el motor de IA (Ollama)…")
        if url_responde(OLLAMA_URL + "/api/tags"):
            return self.preparar_modelo()
        exe = buscar_ollama()
        if not exe and not (ES_MAC and app_ollama_mac()):
            if not self.si_no("Motor de IA", "Ollama, el motor de IA, no está instalado en este ordenador.\n\n"
                              "Sin él el programa funciona, pero NO detecta nombres con IA "
                              "(sí con el diccionario y las reglas).\n\n"
                              "¿Quieres descargarlo e instalarlo ahora? (aprox. 1 GB)"):
                return
            if not self.instalar_ollama():
                return
            exe = buscar_ollama()
        self.encender_ollama(exe)
        if url_responde(OLLAMA_URL + "/api/tags"):
            self.preparar_modelo()
        else:
            self.aviso("Motor de IA", "Ollama no responde. El programa se abrirá sin IA.")

    def instalar_ollama(self):
        try:
            carpeta = tempfile.mkdtemp(prefix="ollama_")
            if ES_WIN:
                instalador = os.path.join(carpeta, "OllamaSetup.exe")
                self.poner_estado("Descargando Ollama…")
                descargar(URL_OLLAMA_WIN, instalador,
                          lambda h, t: (self.poner_progreso(h, t), self.poner_estado("Descargando Ollama…", f"{mb(h)} de {mb(t)}")))
                self.poner_progreso(0, 0)
                self.poner_estado("Instalando Ollama…", "Sigue los pasos del instalador que se ha abierto")
                subprocess.run([instalador])
            elif ES_MAC:
                zipf = os.path.join(carpeta, "Ollama-darwin.zip")
                self.poner_estado("Descargando Ollama…")
                descargar(URL_OLLAMA_MAC, zipf,
                          lambda h, t: (self.poner_progreso(h, t), self.poner_estado("Descargando Ollama…", f"{mb(h)} de {mb(t)}")))
                self.poner_progreso(0, 0)
                self.poner_estado("Instalando Ollama…")
                destino = os.path.expanduser("~/Applications")
                os.makedirs(destino, exist_ok=True)
                # ditto conserva los enlaces y permisos internos de la app (zipfile no)
                subprocess.run(["ditto", "-x", "-k", zipf, destino], check=True)
            else:
                raise RuntimeError("Sistema no soportado para instalación automática")
            shutil.rmtree(carpeta, ignore_errors=True)
            return True
        except Exception as e:
            webbrowser.open(URL_OLLAMA_WEB)
            self.aviso("Motor de IA", f"No se pudo instalar automáticamente ({e}).\n\n"
                                      "Se ha abierto la página de descarga: instálalo y vuelve a abrir el anonimizador.")
            return False

    def encender_ollama(self, exe):
        self.poner_estado("Encendiendo el motor de IA…")
        if ES_MAC and app_ollama_mac() and not (exe or "").startswith(carpeta_app()):
            subprocess.Popen(["open", "-g", app_ollama_mac()])
        elif exe:
            entorno = dict(os.environ)
            modelos_portables = os.path.join(carpeta_app(), "modelos")
            if os.path.isdir(modelos_portables):
                entorno["OLLAMA_MODELS"] = modelos_portables
            self.ollama_propio = subprocess.Popen([exe, "serve"], env=entorno, creationflags=SIN_VENTANA,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if url_responde(OLLAMA_URL + "/api/tags"):
                return
            time.sleep(1)

    def preparar_modelo(self):
        modelos = modelos_instalados()
        charla = [m for m in modelos if m and "embed" not in m and not es_modelo_ocr(m)]
        tiene_ocr = any(es_modelo_ocr(m) for m in modelos)
        ajustes = leer_ajustes()
        a_descargar = []
        if not charla:
            a_descargar = self.elegir_modelo(ofrecer_ocr=not tiene_ocr)
            ajustes["ocr_preguntado"] = True
        elif not tiene_ocr and not ajustes.get("ocr_preguntado"):
            ajustes["ocr_preguntado"] = True  # se pregunta una sola vez; luego, desde el programa
            if self.si_no("Lector de PDF escaneados",
                          "Para leer PDF escaneados (fotos o escaneos, sin texto) hace falta el lector "
                          f"OCR ({MODELO_OCR[0]}, {MODELO_OCR[1]}).\n\n¿Quieres descargarlo ahora?\n\n"
                          "También puedes hacerlo más tarde desde el programa "
                          "(barra lateral → Lector de PDF escaneados)."):
                a_descargar = [MODELO_OCR[0]]
        guardar_ajustes(ajustes)

        for nombre in a_descargar:
            que = "el lector de PDF escaneados" if es_modelo_ocr(nombre) else f"el modelo {nombre}"
            try:
                def avance(estado, hecho, total, que=que):
                    self.poner_progreso(hecho, total)
                    self.poner_estado(f"Descargando {que} (solo esta vez)…",
                                      f"{mb(hecho)} de {mb(total)}" if total else estado)
                descargar_modelo(nombre, avance)
            except Exception as e:
                self.aviso("Modelo de IA", f"No se pudo descargar {que} ({e}).")
            self.poner_progreso(0, 0)

    def arrancar_programa(self, datos):
        self.poner_estado("Abriendo el anonimizador…", "La primera vez puede tardar unos segundos")
        puerto = puerto_libre()
        entorno = dict(os.environ, ANONIMIZADOR_DATOS=datos)
        if getattr(sys, "frozen", False):
            orden = [sys.executable, "--servidor", str(puerto), str(os.getpid())]
        else:
            orden = [sys.executable, os.path.abspath(__file__), "--servidor", str(puerto), str(os.getpid())]
        self.servidor = subprocess.Popen(orden, env=entorno, creationflags=SIN_VENTANA,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL)
        self.url = f"http://localhost:{puerto}"
        for _ in range(120):
            if self.servidor.poll() is not None:
                raise RuntimeError("El programa se ha cerrado al arrancar")
            if url_responde(self.url + "/_stcore/health", 1):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("El programa tarda demasiado en arrancar")
        try:
            with open(os.path.join(datos, "instancia.json"), "w", encoding="utf-8") as f:
                json.dump({"puerto": puerto, "pid": os.getpid()}, f)
        except Exception:
            pass
        abrir_ventana(self.url)

        def listo():
            self.barra.stop()
            self.barra.config(mode="determinate", maximum=100, value=100)
            self.estado.config(text="✔ El anonimizador está en marcha")
            self.detalle.config(text="Deja esta ventana abierta (puedes minimizarla). Para terminar, pulsa Salir.")
            self.b_abrir.config(state="normal")
        self.en_ventana(listo)
        self.ofrecer_acceso_directo()

    def ofrecer_acceso_directo(self):
        if not (ES_WIN and getattr(sys, "frozen", False)) or es_portable():
            return
        ajustes = leer_ajustes()
        if ajustes.get("acceso_directo_preguntado"):
            return
        ajustes["acceso_directo_preguntado"] = True
        guardar_ajustes(ajustes)
        if self.si_no("Acceso directo", "¿Quieres un acceso directo en el escritorio para abrir el anonimizador?"):
            try:
                crear_acceso_directo_windows()
            except Exception:
                pass

    # --- botones ---
    def abrir(self):
        if self.url:
            abrir_ventana(self.url)

    def al_cerrar(self):
        from tkinter import messagebox
        if self.servidor and not messagebox.askyesno(
                "Salir", "¿Cerrar el anonimizador?\n\nLa ventana del programa dejará de funcionar.", parent=self.raiz):
            return
        for p in (self.servidor, self.ollama_propio):
            if p and p.poll() is None:
                p.terminate()
        try:
            os.remove(os.path.join(carpeta_datos(), "instancia.json"))
        except Exception:
            pass
        self.raiz.destroy()

    def ejecutar(self):
        self.raiz.mainloop()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--servidor":
        modo_servidor(int(sys.argv[2]), int(sys.argv[3]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "--diagnostico":
        modo_diagnostico(sys.argv[2])
    else:
        Lanzador().ejecutar()
