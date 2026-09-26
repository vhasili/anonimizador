"""
Anonimizador de Historias Clinicas - 100% LOCAL (v6)
Mejoras: prompt few-shot, solapamiento, pasadas múltiples, expansión de nombres
"""
import streamlit as st
import pypdf, re, io, zipfile, os, json, unicodedata, tempfile, shutil
from datetime import datetime, timedelta


def _normalizar(s):
    """Minúsculas y sin tildes/diacríticos para comparaciones case/accent-insensitive."""
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()

# Datos del usuario (config, diccionario, exclusiones, presets): junto al script (uso con
# INICIAR_PC.bat o desde USB) o en la carpeta que indique la app de escritorio.
_DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
_DIR_DATOS = os.environ.get("ANONIMIZADOR_DATOS") or _DIR_SCRIPT
CONFIG_FILE = os.path.join(_DIR_DATOS, "config_anonimizador.json")
DICCIONARIO_FILE = os.path.join(_DIR_DATOS, "diccionario_aprendido.json")
EXCLUSIONES_FILE = os.path.join(_DIR_DATOS, "exclusiones_aprendidas.json")
PRESETS_FILE = os.path.join(_DIR_DATOS, "presets_anonimizador.json")

# Vista coloreada + editor con scroll acompasado y doble clic para deshacer (HTML propio)
import streamlit.components.v1 as _components
_componente_revisor = _components.declare_component(
    "revisor", path=os.path.join(_DIR_SCRIPT, "componentes", "revisor"))
MODELO_OLLAMA_DEFAULT = "qwen2.5:3b-instruct"
# Lector de PDF escaneados (OCR): modelo de visión de Ollama, se descarga bajo demanda
MODELO_OCR_DEFAULT = "qwen2.5vl:3b"
TAMANO_OCR_DEFAULT = "3,2 GB"


def es_modelo_ocr(nombre):
    return (nombre or "").startswith("qwen2.5vl")


def modelo_ocr_disponible(modelos):
    """El lector OCR instalado (el pequeño si hay varios), o None."""
    ocr = [m for m in modelos or [] if es_modelo_ocr(m)]
    return MODELO_OCR_DEFAULT if MODELO_OCR_DEFAULT in ocr else (ocr[0] if ocr else None)

# ---------------- Ollama ----------------
@st.cache_resource
def comprobar_ollama():
    try:
        import ollama
        respuesta = ollama.list()
        # ollama-python >= 0.4 devuelve ListResponse (Pydantic); < 0.4 devuelve dict
        items = getattr(respuesta, "models", None)
        if items is None and isinstance(respuesta, dict):
            items = respuesta.get("models", [])
        items = items or []

        nombres = []
        for m in items:
            nombre = getattr(m, "model", None) or getattr(m, "name", None)
            if nombre is None and isinstance(m, dict):
                nombre = m.get("model") or m.get("name")
            if nombre:
                nombres.append(nombre)
        return {"ok": True, "modelos": nombres, "ollama": ollama}
    except Exception as e:
        return {"ok": False, "error": str(e)}


PROMPT_PACIENTES = """Extrae TODOS los nombres y apellidos de PACIENTES del texto clínico (puede estar en castellano o gallego).

EJEMPLOS:
Texto: "Paciente María García López, 67 años, acude por..."
Respuesta: María García López

Texto: "Se cita a Don José Manuel Pérez Fernández el 12/03/2024"
Respuesta: José Manuel Pérez Fernández

Texto: "Paciente Xoán Ramón Fernández Outeiro, 72 anos, acude por..."
Respuesta: Xoán Ramón Fernández Outeiro

Texto: "No se observan alteraciones"
Respuesta: NINGUNO

REGLAS:
- Devuelve UN nombre completo por línea, tal como aparece en el texto
- Incluye nombre + todos los apellidos juntos (incluidas partículas: de, do, da, del, la)
- RESPETA la grafía ORIGINAL del texto (NO traduzcas nombres entre gallego y castellano: si pone "Xoán" devuelve "Xoán", no "Juan")
- NO incluyas: médicos, hospitales, ciudades, fármacos, siglas (TNM, PSA, HER2...)
- NO añadas explicaciones

TEXTO:
{texto}

NOMBRES DE PACIENTES (uno por línea, sin numeración):"""


PROMPT_MEDICOS = """Extrae TODOS los nombres y apellidos de MÉDICOS y personal sanitario del texto clínico (puede estar en castellano o gallego).

EJEMPLOS:
Texto: "Dr. Francisco Javier Martínez Rodríguez, urólogo"
Respuesta: Francisco Javier Martínez Rodríguez

Texto: "Comentado con García López del servicio de oncología"
Respuesta: García López

Texto: "firmado por: Dra. Ana Belén Sánchez"
Respuesta: Ana Belén Sánchez

Texto: "Dra. Uxía Pereira do Souto, servizo de Uroloxía"
Respuesta: Uxía Pereira do Souto

Texto: "Se realiza RTU"
Respuesta: NINGUNO

REGLAS:
- Devuelve UN nombre por línea
- Incluye apellidos aunque no haya "Dr./Dra." delante
- Incluye partículas de los apellidos: de, do, da, del, la
- RESPETA la grafía ORIGINAL del texto (NO traduzcas nombres entre gallego y castellano)
- NO incluyas: pacientes, hospitales, siglas médicas
- NO añadas explicaciones

TEXTO:
{texto}

NOMBRES DE MÉDICOS (uno por línea):"""


PROMPT_OTROS = """Extrae TODOS los nombres propios de PERSONAS que aparezcan en el texto y que no sean claramente pacientes ni médicos (familiares, acompañantes, enfermeros, cuidadores, cualquier persona mencionada). El texto puede estar en castellano o gallego.

EJEMPLOS:
Texto: "Acompañado por su hija Carmen Ruiz"
Respuesta: Carmen Ruiz

Texto: "Enfermera Laura Gómez administra la medicación"
Respuesta: Laura Gómez

Texto: "Acompañado pola súa filla Antía Castro"
Respuesta: Antía Castro

Texto: "Sin alteraciones"
Respuesta: NINGUNO

REGLAS:
- Solo personas (nombre + apellido o solo apellido)
- Incluye partículas: de, do, da, del, la
- RESPETA la grafía ORIGINAL del texto (NO traduzcas nombres entre gallego y castellano)
- NO incluyas: hospitales, farmacos, siglas, ciudades
- Uno por línea, sin explicaciones

TEXTO:
{texto}

OTROS NOMBRES (uno por línea):"""


def llamar_ollama(prompt, modelo, cliente, errores=None):
    try:
        r = cliente.chat(
            model=modelo,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1, "num_predict": 500},
        )
        return r["message"]["content"].strip()
    except Exception as e:
        if errores is not None:
            errores.append(str(e))
        return ""


def extraer_nombres_de_salida(salida):
    """Parsea la respuesta del modelo y extrae nombres limpios."""
    nombres = set()
    for linea in salida.split("\n"):
        n = linea.strip()
        # Quitar numeración, viñetas, comillas
        n = re.sub(r'^[\d\.\-•\*\)\s]+', '', n)
        # Sin tratamiento delante ('Dr. Pereira' -> 'Pereira'): así el 'Dr.' se queda en el texto
        n = re.sub(r'^(?:Dr|Dra|Doctor|Doctora|Dña|Don|Doña|Sr|Sra|Srta)\.?\s+', '', n, flags=re.IGNORECASE)
        n = n.strip('"\'.,;:')
        if not n or len(n) < 3: continue
        if "NINGUNO" in n.upper(): continue
        if n.upper() == n and len(n) <= 6: continue  # siglas
        # Filtrar líneas que parecen explicaciones
        if len(n.split()) > 6: continue  # frases largas
        if any(c in n for c in ['?', '!', '¿', '¡', ':']): continue
        nombres.add(n)
    return nombres


PROMPT_COMBINADO = """Eres un experto en anonimización de historias clínicas en castellano y gallego.

Extrae del texto TODOS los nombres propios de PERSONAS (pacientes, médicos, familiares, enfermería, cualquier persona mencionada por nombre y/o apellidos).

REGLAS ESTRICTAS:
- Devuelve UN nombre completo por línea, tal como aparece en el texto.
- Incluye siempre nombre + apellidos si aparecen juntos.
- Incluye también apellidos sueltos si el contexto indica que son personas (ej. "comentado con García López").
- NO incluyas: hospitales, ciudades, servicios médicos, fármacos, pruebas, diagnósticos, siglas clínicas (TNM, PSA, HER2, BCG, ECOG, RTU, TURB, etc.), enfermedades, ni topónimos.
- NO añadas numeración, comentarios ni explicaciones. Solo los nombres, uno por línea.
- Si no hay nombres, responde exactamente: NINGUNO

TEXTO A ANALIZAR:
{texto}

NOMBRES DETECTADOS (uno por línea):"""


# Caché global de respuestas de Ollama por (hash del trozo, modelo)
# Evita reprocesar el mismo texto si se repite el análisis
_CACHE_OLLAMA = {}


# ============ DICCIONARIO APRENDIDO ============
# Nombres/apellidos que el usuario ha confirmado o añadido manualmente.
# Se cargan al arranque y se aplican SIEMPRE, incluso sin IA.
def _cargar_lista_json(ruta, clave):
    if os.path.exists(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return set(json.load(f).get(clave, []))
        except Exception:
            pass
    return set()

def _guardar_lista_json(ruta, clave, valores):
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump({clave: sorted(valores, key=_normalizar)}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def cargar_diccionario_aprendido():
    return _cargar_lista_json(DICCIONARIO_FILE, "nombres")

def guardar_diccionario_aprendido(nombres):
    _guardar_lista_json(DICCIONARIO_FILE, "nombres", nombres)

# Exclusiones: términos que el usuario ha revertido (falsos positivos) y que
# NO deben volver a anonimizarse como nombre.
def cargar_exclusiones():
    return _cargar_lista_json(EXCLUSIONES_FILE, "exclusiones")

def guardar_exclusiones(exclusiones):
    _guardar_lista_json(EXCLUSIONES_FILE, "exclusiones", exclusiones)


def _fusionar(actual, nuevos):
    """Une sin duplicar variantes que solo difieren en mayúsculas o espacios.
    Devuelve (conjunto_resultante, lista_de_añadidos)."""
    claves = {" ".join(x.split()).lower() for x in actual}
    añadidos = []
    for x in nuevos:
        x = " ".join(str(x).split())
        if x and x.lower() not in claves:
            claves.add(x.lower())
            añadidos.append(x)
    return set(actual) | set(añadidos), añadidos


def _resolver_conflictos(dic, exc):
    """Un término no puede estar en ambas listas; gana el diccionario (anonimizar es lo seguro)."""
    dic_norm = {_normalizar(x) for x in dic}
    conflictos = {x for x in exc if _normalizar(x) in dic_norm}
    return exc - conflictos, conflictos


def exportar_memoria_json(incluir_nombres=True, incluir_exclusiones=True):
    payload = {"tipo": "memoria_anonimizador", "version": 1,
               "exportado": datetime.now().isoformat(timespec="seconds")}
    if incluir_nombres:
        payload["nombres"] = sorted(cargar_diccionario_aprendido(), key=_normalizar)
    if incluir_exclusiones:
        payload["exclusiones"] = sorted(cargar_exclusiones(), key=_normalizar)
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def leer_memoria_importada(datos):
    """Acepta el archivo combinado, un diccionario_aprendido.json o un exclusiones_aprendidas.json.
    Devuelve (nombres, exclusiones)."""
    try:
        data = json.loads(datos.decode("utf-8-sig"))
    except Exception:
        raise ValueError("No es un JSON válido")
    if not isinstance(data, dict) or not ("nombres" in data or "exclusiones" in data):
        raise ValueError("No es un archivo de memoria del anonimizador")
    nombres = data.get("nombres", [])
    exclusiones = data.get("exclusiones", [])
    if not isinstance(nombres, list) or not isinstance(exclusiones, list):
        raise ValueError("Formato incorrecto: 'nombres' y 'exclusiones' deben ser listas")
    return [str(x) for x in nombres if str(x).strip()], [str(x) for x in exclusiones if str(x).strip()]


def fusionar_memoria(nombres_imp, exclusiones_imp, guardar=True):
    """Suma la memoria importada a la local. Devuelve (n_nombres_nuevos, n_exclusiones_nuevas, conflictos)."""
    dic, nuevos_n = _fusionar(cargar_diccionario_aprendido(), nombres_imp)
    exc, nuevos_e = _fusionar(cargar_exclusiones(), exclusiones_imp)
    exc, conflictos = _resolver_conflictos(dic, exc)
    nuevos_e = [x for x in nuevos_e if x not in conflictos]
    if guardar:
        guardar_diccionario_aprendido(dic)
        guardar_exclusiones(exc)
    return len(nuevos_n), len(nuevos_e), sorted(conflictos)


# ============ APRENDIZAJE A PARTIR DE LAS CORRECCIONES ============
_RX_TOKEN = re.compile(r'\[[A-Z_]+\]|\w+|\s+|[^\w\s]')

def _es_nombre_aprendible(s):
    return bool(s) and 4 <= len(s) <= 80 and len(s.split()) <= 6 and "\n" not in s


def _valor_aprendible(sus):
    """Texto a memorizar de una sustitución. En cabeceras solo se aprende el valor
    de los campos de nombre ('Paciente: X' -> 'X')."""
    orig = sus["original"].strip()
    if sus.get("categoria") == "CABECERA" and sus["etiqueta"] != "[NOMBRE]":
        return None
    return orig


def nombres_etiquetados_a_mano(antes, despues):
    """Tramos que el usuario ha sustituido a mano por [NOMBRE] en el editor."""
    import difflib
    a, b = _RX_TOKEN.findall(antes), _RX_TOKEN.findall(despues)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    encontrados = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        viejo = "".join(a[i1:i2]).strip()
        nuevo = "".join(b[j1:j2]).strip()
        if nuevo == "[NOMBRE]" and "[" not in viejo and _es_nombre_aprendible(viejo):
            encontrados.add(viejo)
    return encontrados


def aprender_de_correcciones(sustituciones, revertidos, texto_revisado=None, texto_editado=None):
    """Aprende de la revisión de un documento:
    - nombres no revertidos        -> diccionario
    - nombres revertidos           -> exclusiones (y se retiran del diccionario)
    - [NOMBRE] añadidos en el editor -> diccionario"""
    confirmados, descartados = set(), set()
    for i, s in enumerate(sustituciones):
        cat = s.get("categoria", "")
        es_nombre = "NOMBRES" in cat
        if not es_nombre and cat != "CABECERA":
            continue
        valor = _valor_aprendible(s)
        if not _es_nombre_aprendible(valor):
            continue
        if i in revertidos:
            # Revertir una cabecera no implica que el valor no sea un nombre
            if es_nombre:
                descartados.add(valor)
        else:
            confirmados.add(valor)

    if texto_revisado is not None and texto_editado is not None and texto_editado != texto_revisado:
        confirmados |= nombres_etiquetados_a_mano(texto_revisado, texto_editado)

    # Mismo término confirmado en un sitio y revertido en otro: ambiguo, no se aprende
    conf_norm = {_normalizar(x) for x in confirmados}
    desc_norm = {_normalizar(x) for x in descartados}
    ambiguos = conf_norm & desc_norm
    confirmados = {x for x in confirmados if _normalizar(x) not in ambiguos}
    descartados = {x for x in descartados if _normalizar(x) not in ambiguos}
    conf_norm -= ambiguos
    desc_norm -= ambiguos

    dic = cargar_diccionario_aprendido()
    exc = cargar_exclusiones()
    retirados = {x for x in dic if _normalizar(x) in desc_norm}
    dic -= retirados
    exc = {x for x in exc if _normalizar(x) not in conf_norm}
    dic, nuevos_n = _fusionar(dic, confirmados)
    exc, nuevos_e = _fusionar(exc, descartados)
    guardar_diccionario_aprendido(dic)
    guardar_exclusiones(exc)
    return {"nombres": len(nuevos_n), "exclusiones": len(nuevos_e), "retirados": len(retirados)}


def resumen_aprendizaje(res):
    partes = []
    if res["nombres"]: partes.append(f"{res['nombres']} nombre/s nuevo/s")
    if res["exclusiones"]: partes.append(f"{res['exclusiones']} exclusión/es nueva/s")
    if res["retirados"]: partes.append(f"{res['retirados']} retirado/s del diccionario")
    return ("📚 Aprendido: " + ", ".join(partes)) if partes else "📚 No había nada nuevo que aprender"


# ============ PRESETS DE CONFIGURACIÓN ============
def cargar_presets():
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def guardar_presets(presets):
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============ PATRONES ESPECÍFICOS HULA/SERGAS ============
# Se aplican SIEMPRE. Formato de identificadores del sistema sanitario gallego.
PATRONES_HULA_SERGAS = {
    # Historia HULA: puede ser numérica larga o con prefijo
    "HULA_HC":     (r'\b(?:HULA|H\.U\.L\.A\.)[\s\-]*\d{5,10}\b', '[HULA_HC]'),
    # Códigos SERGAS con prefijo típico
    "SERGAS_ID":   (r'\bSERGAS[\s\-:]*[A-Z0-9]{4,}\b', '[SERGAS_ID]'),
    # Número de historia clínica gallego (formato área-servicio-número)
    "NHC_GALICIA": (r'\b\d{2,4}[\-/]\d{3,6}[\-/]\d{2,4}\b(?![\-/]\d)', '[NHC]'),
    # Códigos de exploración/radiología habituales
    "EXPLORACION": (r'\b(?:EXP|EXPL|RX|TC|RM|ECO)[\s\-:]*\d{6,}\b', '[EXPLORACION_ID]'),
    # ID de episodio/ingreso
    "EPISODIO":    (r'\b(?:Episodio|Epis\.|Ep\.?)[\s\-:#Nº]*\d{5,}\b', '[EPISODIO]'),
}


def _hash_trozo(texto, modelo):
    import hashlib
    return hashlib.md5(f"{modelo}::{texto}".encode("utf-8")).hexdigest()


def trocear_texto(texto):
    # Trozos grandes: los modelos 7-8B aguantan bien 6000 caracteres
    TAM, SOL = 6000, 600
    trozos = []
    i = 0
    while i < len(texto):
        trozos.append(texto[i:i+TAM])
        if i + TAM >= len(texto): break
        i += TAM - SOL
    return trozos


class Cancelado(Exception):
    pass


def detectar_nombres_ollama(texto, modelo, cliente, hacer_pacientes=True, hacer_medicos=True, hacer_otros=True,
                            estado=None):
    """Detección optimizada: 1 sola pasada por trozo con prompt combinado.
    Los parámetros hacer_* controlan si se filtran después categorías concretas.
    Devuelve (nombres_por_categoria, errores).
    - Si las 3 activas o solo pacientes+otros: aplica NER general.
    - Si solo médicos activo: se hace pasada específica de médicos.
    Cachea respuestas por hash del trozo + modelo.
    `estado` (dict, opcional) recibe el avance trozo a trozo para la barra de progreso."""
    import time
    nombres_totales = set()
    errores = []
    estado = estado if estado is not None else {}

    trozos = trocear_texto(texto)
    estado.update(total=len(trozos), hechos=0, inicio_trozo=None)
    estado.setdefault("duraciones", [])

    # Elegir prompt según qué categorías se piden
    hacer_todos = hacer_pacientes and hacer_medicos and hacer_otros
    solo_medicos = hacer_medicos and not (hacer_pacientes or hacer_otros)

    for trozo in trozos:
        if estado.get("cancelar"):
            raise Cancelado()
        clave = _hash_trozo(trozo, modelo)

        if clave in _CACHE_OLLAMA:
            nombres_totales |= _CACHE_OLLAMA[clave]
            estado["hechos"] += 1
            continue

        t0 = time.time()
        estado["inicio_trozo"] = t0

        nombres_trozo = set()

        if solo_medicos:
            # Solo médicos: mantener el prompt específico
            salida = llamar_ollama(PROMPT_MEDICOS.format(texto=trozo), modelo, cliente, errores)
            nombres_trozo |= extraer_nombres_de_salida(salida)
        elif hacer_todos or (hacer_pacientes and hacer_otros and not hacer_medicos):
            # Prompt combinado (una sola llamada = 3x más rápido)
            salida = llamar_ollama(PROMPT_COMBINADO.format(texto=trozo), modelo, cliente, errores)
            nombres_trozo |= extraer_nombres_de_salida(salida)
        else:
            # Combinaciones raras (solo pacientes, o pacientes+médicos): fallback a prompts específicos
            prompts_activos = []
            if hacer_pacientes: prompts_activos.append(PROMPT_PACIENTES)
            if hacer_medicos:   prompts_activos.append(PROMPT_MEDICOS)
            if hacer_otros:     prompts_activos.append(PROMPT_OTROS)
            for prompt_tpl in prompts_activos:
                salida = llamar_ollama(prompt_tpl.format(texto=trozo), modelo, cliente, errores)
                nombres_trozo |= extraer_nombres_de_salida(salida)

        _CACHE_OLLAMA[clave] = nombres_trozo
        nombres_totales |= nombres_trozo
        estado["duraciones"].append(time.time() - t0)
        estado["hechos"] += 1
        estado["inicio_trozo"] = None

    return nombres_totales, errores


def expandir_nombres(nombres):
    """Para cada 'María García López' añade subsecuencias de 2+ palabras
    ('María García', 'García López', 'María García López'). NO añade palabras
    sueltas: apellidos frecuentes (García, López, Castro, Pereira...) y nombres
    de pila producen demasiados falsos positivos (topónimos, otros pacientes,
    nombres de centros)."""
    expandidos = set(nombres)
    for nombre in list(nombres):
        formas = [nombre]
        # 'Apellidos , Nombre' (formato de cabecera) -> también 'Nombre Apellidos',
        # y con el nombre compuesto recortado ('Souto Varela, Ana' / 'Ana Souto Varela')
        if nombre.count(",") == 1:
            apellidos, pila = (x.strip() for x in nombre.split(","))
            palabras_pila = pila.split()
            if apellidos and palabras_pila:
                for k in range(1, len(palabras_pila) + 1):
                    parte = " ".join(palabras_pila[:k])
                    formas += [f"{apellidos}, {parte}", f"{parte} {apellidos}"]
        for forma in formas:
            expandidos.add(forma)
            partes = forma.replace(",", " ").split()
            if len(partes) >= 2:
                for i in range(len(partes)):
                    for j in range(i + 2, len(partes) + 1):  # mínimo 2 palabras
                        sub = " ".join(partes[i:j])
                        if len(sub) >= 4:
                            expandidos.add(sub)
    return expandidos


_CLASES_TILDES = {"a": "aáàâäã", "e": "eéèêë", "i": "iíìîï", "o": "oóòôöõ",
                  "u": "uúùûü", "n": "nñ", "c": "cç"}

def patron_nombre(nombre):
    """Regex tolerante a tildes, mayúsculas, espacios y comas:
    'Souto Varela , Ana María' encuentra también 'SOUTO VÁRELA, Ana Maria'."""
    tokens = re.findall(r",|[^\s,]+", nombre)
    patron, previo = "", None
    for tok in tokens:
        if tok == ",":
            pieza = ","
        else:
            pieza = ""
            for ch in tok:
                base = _normalizar(ch)
                pieza += f"[{_CLASES_TILDES[base]}]" if base in _CLASES_TILDES else re.escape(ch)
        if previo is not None:
            patron += r"\s*" if "," in (tok, previo) else r"\s+"
        patron += pieza
        previo = tok
    return r"(?<!\w)" + patron + r"(?!\w)"


def _nombres_validos(candidatos):
    """Filtra siglas, palabras comunes, exclusiones aprendidas y términos cortos; más largos primero."""
    exclusiones_norm = {_normalizar(x) for x in cargar_exclusiones()}
    validos = [nd for nd in candidatos
               if nd.upper() not in SIGLAS_PROTEGIDAS
               and _normalizar(nd) not in PALABRAS_COMUNES_NORM
               and _normalizar(nd) not in exclusiones_norm
               and len(nd) >= 4]
    return sorted(validos, key=len, reverse=True)


def aplicar_nombres_nuevos(anon_base, sustituciones, nombres):
    """Busca SOLO `nombres` en el texto ya anonimizado, sin repetir la IA ni tocar lo ya sustituido.
    Las nuevas sustituciones se añaden al FINAL de la lista para no mover los índices
    de las casillas de reversión. Devuelve (texto, sustituciones, n_nuevas)."""
    etq = "[NOMBRE]"
    ocupados = [(s["pos_ini"], s["pos_fin"]) for s in sustituciones]
    nuevos = []
    for nd in _nombres_validos(expandir_nombres(nombres)):
        for m in re.finditer(patron_nombre(nd), anon_base, flags=re.IGNORECASE):
            a, b = m.start(), m.end()
            if all(b <= i or a >= f for i, f in ocupados):
                nuevos.append((a, b, m.group(0)))
                ocupados.append((a, b))
    if not nuevos:
        return anon_base, sustituciones, 0
    nuevos.sort()

    def desplazar(pos):
        return pos + sum(len(etq) - (f - i) for i, f, _ in nuevos if f <= pos)

    actualizadas = []
    for s in sustituciones:
        ini = desplazar(s["pos_ini"])
        actualizadas.append(dict(s, pos_ini=ini, pos_fin=ini + (s["pos_fin"] - s["pos_ini"])))
    partes, cursor = [], 0
    for i, f, orig in nuevos:
        partes += [anon_base[cursor:i], etq]
        cursor = f
        ini = desplazar(i)
        actualizadas.append({"original": orig, "etiqueta": etq, "pos_ini": ini,
                             "pos_fin": ini + len(etq), "categoria": "NOMBRES (dic.)"})
    partes.append(anon_base[cursor:])
    return "".join(partes), actualizadas, len(nuevos)


def marcar_nombres_en_texto(texto, nombres):
    """Sustituye `nombres` por [NOMBRE] en un texto libre (p.ej. lo ya editado a mano)."""
    for nd in _nombres_validos(expandir_nombres(nombres)):
        texto = re.sub(patron_nombre(nd), "[NOMBRE]", texto, flags=re.IGNORECASE)
    return texto


def nombres_pendientes(aplicados):
    """Nombres del diccionario que aún no se han buscado en un documento ya procesado."""
    return {x for x in cargar_diccionario_aprendido() if x not in aplicados}


# ---------------- PATRONES ----------------
PATRONES = {
    "DNI":      (r'\b\d{8}[A-HJ-NP-TV-Z]\b', '[DNI]'),
    "NIE":      (r'\b[XYZ]\d{7}[A-HJ-NP-TV-Z]\b', '[NIE]'),
    # NSS con separadores explícitos (formato clásico 2 + 7-8 + 2 dígitos)
    "NSS":     (r'\b\d{2}[\s\-/]\d{7,8}[\s\-/]\d{2}\b', '[NSS]'),
    # NSS con etiqueta previa (admite también formato pegado sin separadores)
    "NSS_CTX": (r'(\b(?:NSS|N\.?S\.?S\.?|N[ºo]\s*Afiliaci[oó]n|Seguridad\s+Social|Seguranza\s+Social)\s*[:#]?\s*)\d{10,12}\b',
                r'\1[NSS]'),
    "NASI":     (r'\b(?:NASI|N\.A\.S\.I\.)[\s:]*\d{3,}\b', '[NASI]'),
    "CIP":      (r'\b(?:CIP|CIPA)[\s:]*[A-Z0-9]{6,}\b', '[CIP]'),
    "TARJETA":  (r'\b(?:Tarjeta\s+Sanitaria|Tarxeta\s+Sanitaria|Health\s+Card|T\.S\.I\.)[\s:#Nº]*[A-Z0-9]{6,}\b', '[TARJETA_SANITARIA]'),
    "TELEFONO": (r'\b(?:\+34[\s-]?)?[6789]\d{2}[\s-]?\d{3}[\s-]?\d{3}\b', '[TELEFONO]'),
    "EMAIL":    (r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b', '[EMAIL]'),
    "CP":       (r'\b(?:CP|C\.P\.)\s*\d{5}\b', '[CODIGO_POSTAL]'),
    "NHC":      (r'\b(?:NHC|N\.H\.C\.|N[ºo]\s*Historia|Historia\s*Cl[ií]nica|Clinical\s*History|Hist[oó]ria\s*Cl[ií]nica)[\s:]*\d+\b', '[NHC]'),
    # Códigos de usuario con dígito final (p.ej. JPEREZG1, MAGLOPZ12) — casi inequívoco
    "INICIALES":     (r'(?<![A-Z0-9])[A-ZÑ]{4,8}\d{1,2}(?![A-Z0-9])', '[INICIALES]'),
    # Mismo tipo de código sin dígito, exigiendo etiqueta previa (Usuario:, Firmado por:, etc.)
    "INICIALES_CTX": (r'(\b(?:Usuario|User|Firmado\s+por|Validado\s+por|C[oó]digo|Cod\.?|Clave|Login)\s*[:#]?\s*)[A-ZÑ]{4,8}\b',
                      r'\1[INICIALES]'),
    "DIRECCION": (r'\b(?:Calle|C/|C\\|Avenida|Avda\.?|Plaza|Paseo|Rúa|Rua|R/|Praza|Travesía|Travesia|Street|St\.?|Avenue|Ave\.?|Road|Rd\.?)\s+[^\n,.;]{3,60}', '[DIRECCION]'),
}

# Fusionar patrones específicos del sistema HULA/SERGAS
PATRONES.update(PATRONES_HULA_SERGAS)

# Cada patrón corta el "valor" al encontrar 2+ espacios / tab / fin de línea,
# para no devorar campos adyacentes en la misma fila (p.ej. 'Paciente: X   Fecha: 12/03/2024').
_CAB_FIN = r'(?=[ \t]*(?:\*\*|__)?[ \t]*(?:$|\|)|\s{2,}|\t)'
# Solo es "cabecera" si la etiqueta va seguida de ':' (admite el valor en la línea siguiente)
# o separada del valor por 2+ espacios/tab, como en un formulario. Así una frase como
# 'Comentado con el servicio de Oncología' no se come el resto de la línea.
# Admite el formato Markdown: '**Paciente:** X', '**Paciente**: X', '| Paciente | X |'.
_CAB_INI = r'(?:^|(?<=[\s*_|>#\-]))'
_MD = r'(?:\*\*|__|\*|_)?'
_CAB_SEP = (r'(?:[ \t]*' + _MD + r'[ \t]*:[ \t]*' + _MD + r'[ \t]*(?:\n[ \t]*)?'
            r'|[ \t]*' + _MD + r'[ \t]*\|[ \t]*'
            r'|[ \t]{2,}|\t)')
_CAB_VALOR = r'[^\n|]+?' + _CAB_FIN

def _cabecera(etiquetas, valor=_CAB_VALOR):
    # Solo el grupo 'valor' se sustituye; la etiqueta ('Paciente:') queda tal cual
    return _CAB_INI + '(' + etiquetas + ')' + _CAB_SEP + '(?P<valor>' + valor + ')'

# (patrón, token) — cada campo de cabecera usa un token específico para que el
# revisor humano vea qué categoría fue anonimizada.
CAMPOS_CABECERA = [
    (_cabecera(r'Paciente|Patient|Nombre|Nome|Name|Apellidos|Apelidos|Surname|Surnames'), '[NOMBRE]'),
    (_cabecera(r'Domicilio|Direcci[oó]n|Enderezo|Address'), '[DIRECCION]'),
    (_cabecera(r'Localidad|Concello|Municipio|City|Town|Poblaci[oó]n'), '[LOCALIDAD]'),
    (_cabecera(r'Provincia|Province'), '[PROVINCIA]'),
    (_cabecera(r'M[eé]dico|M[eé]dica|Facultativo|Facultativa|Physician|Doctor|Dr\.?|Dra\.?'), '[NOMBRE]'),
    (_cabecera(r'N\.?\s*Colegiado|Colexiado|License'), '[COLEGIADO]'),
    (_cabecera(r'Especialidad|Especialidade|Specialty|Servicio|Servizo|Department'), '[ESPECIALIDAD]'),
    (_cabecera(r'F\.?\s*Nacimiento|Fecha\s+nac\.?|Data\s+nac\.?|Date\s+of\s+Birth|DOB|Birth\s*date'), '[FECHA]'),
    # Teléfono y NSS: solo el número, nunca el texto que venga detrás
    (_cabecera(r'Tel[eé]fono|Phone|Tel\.?', r'\+?\d[\d \-./()]{5,}\d'), '[TELEFONO]'),
    (_cabecera(r'NSS|N\.?S\.?S\.?|N[ºo]\s*Afiliaci[oó]n|Seguridad\s+Social|Social\s+Security',
               r'\d[\d \-/]{6,}\d'), '[NSS]'),
]

# Nombre detrás de un tratamiento (Dr., Dra., Don, Sra...): se sustituye SOLO el nombre.
# Palabras en mayúscula inicial + partículas; sin cruzar saltos de línea ni signos de puntuación.
_PAL_NOMBRE = r"[A-ZÁÉÍÓÚÑÜÇ][A-Za-zÁÉÍÓÚÑÜÇáéíóúñüç'’\-]+"
_PARTICULA = r"(?i:de|del|do|da|dos|das|la|las|los|y|e)"
RX_TITULO_NOMBRE = re.compile(
    r"(?<!\w)((?i:dr|dra|doctor|doctora|dña|don|doña|sr|sra|srta)\.?[ \t]+)"
    r"(" + _PAL_NOMBRE + r"(?:[ \t]+(?:" + _PARTICULA + r"[ \t]+)*" + _PAL_NOMBRE + r"){0,4})")

ESPECIALIDADES = [r"Urolog[ií]a",r"Uroloxía",r"Urology",r"Oncolog[ií]a\s+M[eé]dica",
    r"Oncoloxía\s+Médica",r"Medical\s+Oncology",r"Oncolog[ií]a\s+Radioter[aá]pica",
    r"Radiation\s+Oncology",r"Ginecolog[ií]a",r"Xinecoloxía",r"Gynecology",r"Nefrolog[ií]a",
    r"Nephrology",r"Anatom[ií]a\s+Patol[oó]gica",r"Anatomía\s+Patolóxica",r"Pathology",
    r"Radiolog[ií]a",r"Radioloxía",r"Radiology",r"Cardiolog[ií]a",r"Cardioloxía",r"Cardiology",
    r"Endocrinolog[ií]a",r"Endocrinoloxía",r"Endocrinology",r"Medicina\s+Interna",
    r"Internal\s+Medicine",r"Rehabilitaci[oó]n",r"Rehabilitation",r"Anestesiolog[ií]a",r"Anesthesiology"]
RX_ESPECIALIDAD = re.compile(r'\b(?:' + '|'.join(ESPECIALIDADES) + r')\b', re.IGNORECASE)

SIGLAS_PROTEGIDAS = {"TNM","PSA","HER2","RCC","NMIBC","MIBC","BCG","TURB","RTU","OS","PFS","DFS","ORR",
    "CTDNA","DNA","RNA","ECG","EKG","RMN","TAC","TC","PET","HB","HBA1C","GFR","FGE",
    "EAU","AUA","ESMO","ASCO","NCCN","SEOM","SEOR","AEU","IMRT","VMAT","EMDA","EV","ADC",
    "AINE","COX","LDH","ALT","AST","GGT","FA","PCR","VIH","HIV","VHB","VHC","HTA","DM",
    "ECOG","CIE","QMT","QT","RT","IO","ICI","CPNM","NSCLC","SCLC","HNSCC","MSI","MMR",
    "PD1","PDL1","CTLA4","VEGF","EGFR","ADT","SIADH","DIU","HBPM","HNF","IECA","ARA","BRB","ACO","GLP1",
    "IMC","TA","FC","FR","SATO2","SPO2","BUN","INR","TP","TTP","APTT","VSG","PCR","TSH","T3","T4",
    # Instituciones y siglas gallegas / SNS frecuentes en HCE
    "SERGAS","CHUS","CHUAC","CHUO","CHUF","CHOU","CHUVI","CHUP","POVISA","SNS","SERMAS",
    "SAS","SESCAM","SACYL","SESPA","SCS","IBSALUT","OSAKIDETZA","ICS","GVA",
    "SOGAMO","SOGAPAR","AGAMFEC","FEGAS","SOGADI"}

PALABRAS_COMUNES = {
    # Español
    "Paciente","Servicio","Hospital","Doctor","Doctora","Enfermera","Enfermero",
    "Medico","Medica","Médico","Médica","Centro","Urgencias","Consulta","Informe",
    "Historia","Diagnóstico","Tratamiento","Evolución","Exploración","Antecedentes",
    "Analítica","Revisión","Control","Seguimiento","Alta","Ingreso","Planta",
    "Quirófano","Biopsia","Tumor","Cirugía","Prueba","Pruebas","Estudio","Estudios",
    "Valoración","Juicio","Pronóstico","Anamnesis","Alergia","Alergias","Medicación",
    "Exploraciones","Recomendaciones","Observaciones","Motivo","Episodio",
    # Galego
    "Servizo","Médico","Médica","Enfermeiro","Enfermeira","Urxencias","Consulta",
    "Historia","Diagnóstico","Tratamento","Evolución","Exploración","Antecedentes",
    "Analítica","Revisión","Seguimento","Alta","Ingreso","Planta","Quirófano",
    "Biopsia","Cirurxía","Proba","Probas","Estudo","Estudos","Valoración","Xuízo",
    "Pronóstico","Alerxia","Alerxias","Medicación","Recomendacións","Observacións",
    "Motivo","Episodio",
    # Inglés (por si aparecen plantillas bilingües)
    "Patient","Service","Hospital","Doctor","Nurse","Emergency","History","Diagnosis",
    "Treatment","Follow-up","Admission","Discharge","Surgery","Biopsy","Tumor",
}
PALABRAS_COMUNES_NORM = {_normalizar(p) for p in PALABRAS_COMUNES}

MESES = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,
         "septiembre":9,"octubre":10,"noviembre":11,"diciembre":12,
         "xaneiro":1,"febreiro":2,"maio":5,"xuño":6,"xullo":7,"setembro":9,"outubro":10,"novembro":11,"decembro":12,
         "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,
         "august":8,"september":9,"october":10,"november":11,"december":12}
MESES_INV = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
             7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}

RX_FECHA_SLASH = re.compile(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b')
RX_FECHA_TEXTO = re.compile(r'\b(\d{1,2})\s+(?:de\s+)?(' + '|'.join(MESES.keys()) + r')\s+(?:de\s+|,?\s+)?(\d{4})\b', re.IGNORECASE)


def cargar_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"desplazamiento_dias": 0, "modelo_ollama": MODELO_OLLAMA_DEFAULT, "formato_salida": "md"}

def guardar_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _leer_bytes_pdf(archivo_pdf):
    """Devuelve los bytes del PDF aceptando ruta, file-like o UploadedFile de Streamlit."""
    if hasattr(archivo_pdf, "getvalue"):
        return archivo_pdf.getvalue()
    if hasattr(archivo_pdf, "read"):
        try:
            archivo_pdf.seek(0)
        except Exception:
            pass
        return archivo_pdf.read()
    with open(archivo_pdf, "rb") as f:
        return f.read()


def _aplicar_ocr(pdf_bytes):
    """Aplica OCR al PDF (solo páginas sin texto) y devuelve los bytes del PDF resultante.
    Requiere ocrmypdf + tesseract (con idioma 'spa') + ghostscript instalados."""
    try:
        import ocrmypdf
    except ImportError:
        raise ValueError(
            "El PDF parece escaneado y no hay OCR disponible. "
            "Instala ocrmypdf (pip install ocrmypdf) y Tesseract con idioma español."
        )
    tmp_in = tmp_out = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fi:
            fi.write(pdf_bytes)
            tmp_in = fi.name
        tmp_out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
        ocrmypdf.ocr(
            tmp_in, tmp_out,
            language="spa",
            skip_text=True,
            optimize=0,
            progress_bar=False,
            deskew=True,
        )
        with open(tmp_out, "rb") as f:
            return f.read()
    except ocrmypdf.exceptions.MissingDependencyError as e:
        raise ValueError(f"Falta dependencia para OCR (Tesseract/Ghostscript): {e}")
    except Exception as e:
        raise ValueError(f"Error al aplicar OCR: {e}")
    finally:
        for p in (tmp_in, tmp_out):
            if p and os.path.exists(p):
                try: os.remove(p)
                except Exception: pass


def _necesita_ocr(textos_por_pagina):
    """Heurística: decide si el PDF necesita OCR.
    Dispara OCR si (a) no hay texto, (b) >=40% de páginas están vacías,
    o (c) la media de caracteres por página es muy baja (<80)."""
    if not textos_por_pagina:
        return True
    total = "".join(textos_por_pagina).strip()
    if not total:
        return True
    n = len(textos_por_pagina)
    vacias = sum(1 for t in textos_por_pagina if len(t.strip()) < 20)
    if vacias / n >= 0.4:
        return True
    if len(total) / n < 80:
        return True
    return False


PROMPT_OCR = ("Transcribe literalmente todo el texto de esta página de una historia clínica, en su idioma "
              "original (castellano o gallego), respetando los saltos de línea. No resumas, no corrijas, "
              "no traduzcas y no añadas comentarios. Si la página no tiene texto, no respondas nada.")


def ocr_con_ia(pdf_bytes, paginas, ocr):
    """Lee con el modelo de visión de Ollama las páginas que no tienen texto (escaneadas).
    `ocr`: {"cliente": módulo ollama, "modelo": nombre, "estado": dict de progreso (opcional)}."""
    import time
    import pypdfium2 as pdfium
    estado = ocr.get("estado")
    estado = estado if estado is not None else {}
    pendientes = [i for i, t in enumerate(paginas) if len(t.strip()) < 200]
    estado.update(fase="ocr", ocr_total=len(pendientes), ocr_hechas=0, inicio_pagina=None)
    estado.setdefault("ocr_duraciones", [])
    documento = pdfium.PdfDocument(pdf_bytes)
    try:
        for k, i in enumerate(pendientes):
            if estado.get("cancelar"):
                raise Cancelado()
            pagina = documento[i]
            # ~1400 px en el lado largo: legible y sin disparar el tiempo de la IA
            escala = min(2.5, 1400 / max(pagina.get_width(), pagina.get_height()))
            imagen = pagina.render(scale=escala).to_pil().convert("L")
            buf = io.BytesIO()
            imagen.save(buf, format="PNG")
            t0 = time.time()
            estado["inicio_pagina"] = t0
            r = ocr["cliente"].chat(
                model=ocr["modelo"],
                messages=[{"role": "user", "content": PROMPT_OCR, "images": [buf.getvalue()]}],
                options={"temperature": 0, "num_predict": 4096},
            )
            texto = (r["message"]["content"] or "").strip()
            if len(texto) > len(paginas[i].strip()):
                paginas[i] = texto
            estado["ocr_duraciones"].append(time.time() - t0)
            estado["ocr_hechas"] = k + 1
            estado["inicio_pagina"] = None
    finally:
        documento.close()
    return paginas


def _hay_ocrmypdf():
    try:
        import ocrmypdf  # noqa: F401
        return True
    except ImportError:
        return False


def extraer_texto_pdf(archivo_pdf, ocr=None):
    pdf_bytes = _leer_bytes_pdf(archivo_pdf)
    lector = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if getattr(lector, "is_encrypted", False):
        try:
            if lector.decrypt("") == 0:
                raise ValueError("PDF cifrado con contraseña. No se puede procesar.")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"PDF cifrado y no se pudo abrir: {e}")

    paginas = [(p.extract_text() or "") for p in lector.pages]

    if _necesita_ocr(paginas):
        hecho = False
        if _hay_ocrmypdf():  # OCR clásico (Tesseract): más rápido, si está instalado
            try:
                lector = pypdf.PdfReader(io.BytesIO(_aplicar_ocr(pdf_bytes)))
                paginas = [(p.extract_text() or "") for p in lector.pages]
                hecho = True
            except ValueError:
                if not (ocr and ocr.get("modelo")):
                    raise
        if not hecho:
            if not (ocr and ocr.get("modelo")):
                raise ValueError(
                    "Este PDF está escaneado (es una imagen) y falta el lector de PDF escaneados (OCR). "
                    "Descárgalo en la barra lateral, apartado «📄 Lector de PDF escaneados» (solo una vez).")
            paginas = ocr_con_ia(pdf_bytes, paginas, ocr)

    texto = "\n".join(paginas)
    if not texto.strip():
        raise ValueError("El PDF no contiene texto extraíble ni siquiera tras aplicar OCR.")
    return texto


def extraer_texto_archivo(archivo, ocr=None):
    """Extrae texto de PDF, MD o TXT según la extensión. Los PDF escaneados pasan por OCR."""
    nombre = getattr(archivo, "name", "").lower()
    if nombre.endswith(".pdf"):
        return extraer_texto_pdf(archivo, ocr)
    elif nombre.endswith(".md") or nombre.endswith(".txt"):
        try:
            archivo.seek(0)
        except Exception:
            pass
        raw = archivo.read()
        # Detectar encoding: UTF-8 (con o sin BOM) primero, luego latin-1 como fallback
        for enc in ("utf-8-sig", "latin-1", "cp1252"):
            try:
                texto = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("No se pudo decodificar el archivo de texto.")
        if not texto.strip():
            raise ValueError("El archivo está vacío.")
        # Saltos de línea de Windows -> '\n', como los devuelve el editor del navegador
        return texto.replace("\r\n", "\n").replace("\r", "\n")
    else:
        # Fallback: intentar como texto
        try:
            return archivo.read().decode("utf-8", errors="replace")
        except Exception:
            raise ValueError(f"Formato de archivo no soportado: {nombre}")


def desplazar_fecha_slash(m, dias):
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100: y += 2000 if y < 50 else 1900
    try: return (datetime(y, mo, d) + timedelta(days=dias)).strftime("%d/%m/%Y")
    except: return m.group(0)

def desplazar_fecha_texto(m, dias):
    try:
        d = int(m.group(1)); mo = MESES[m.group(2).lower()]; y = int(m.group(3))
        nueva = datetime(y, mo, d) + timedelta(days=dias)
        return f"{nueva.day} de {MESES_INV[nueva.month]} de {nueva.year}"
    except: return m.group(0)

def procesar_fechas(texto, modo, dias):
    if modo == "No tocar": return texto, 0
    if modo == "Borrar":
        n = len(RX_FECHA_SLASH.findall(texto)) + len(RX_FECHA_TEXTO.findall(texto))
        texto = RX_FECHA_SLASH.sub('[FECHA]', texto)
        texto = RX_FECHA_TEXTO.sub('[FECHA]', texto)
        return texto, n
    n = 0
    def f1(m):
        nonlocal n; n += 1; return desplazar_fecha_slash(m, dias)
    def f2(m):
        nonlocal n; n += 1; return desplazar_fecha_texto(m, dias)
    texto = RX_FECHA_SLASH.sub(f1, texto)
    texto = RX_FECHA_TEXTO.sub(f2, texto)
    return texto, n


def aplicar_nombres(texto, nombres):
    if not nombres: return texto, 0
    total = 0
    vistos = set()
    for nombre in sorted(nombres, key=len, reverse=True):
        # Limpieza: quitar prefijos de palabras comunes que el LLM puede arrastrar
        # (p.ej. si devuelve "Paciente García López" → nos quedamos con "García López")
        partes = nombre.split()
        while partes and _normalizar(partes[0]) in PALABRAS_COMUNES_NORM:
            partes = partes[1:]
        while partes and _normalizar(partes[-1]) in PALABRAS_COMUNES_NORM:
            partes = partes[:-1]
        nombre = " ".join(partes)

        if not nombre or nombre in vistos: continue
        vistos.add(nombre)
        if nombre.upper() in SIGLAS_PROTEGIDAS: continue
        if _normalizar(nombre) in PALABRAS_COMUNES_NORM: continue
        if len(nombre) < 4: continue
        patron = r'\b' + re.escape(nombre) + r'\b'
        n = len(re.findall(patron, texto, flags=re.IGNORECASE))
        if n:
            total += n
            texto = re.sub(patron, '[NOMBRE]', texto, flags=re.IGNORECASE)
    return texto, total


def anonimizar(texto, opciones, modo_fechas, dias, ollama_info, modelo, progreso=None, estado=None):
    """Anonimiza texto en una sola pasada con regiones absolutas.
    Devuelve (texto_anonimizado, contador, errores_ollama, sustituciones)
    donde sustituciones = [{original, etiqueta, pos_ini, pos_fin, categoria}]
    con posiciones referidas al TEXTO FINAL anonimizado, ordenadas de izda a dcha."""
    contador = {}
    errores_ollama = []
    regiones = []  # (ini_orig, fin_orig, original, reemplazo, categoria)

    def _buscar(patron, texto, flags=0):
        return list(re.finditer(patron, texto, flags=flags))

    # 1) CABECERAS
    if opciones.get("CABECERA", True):
        for patron, token in CAMPOS_CABECERA:
            for m in _buscar(patron, texto, flags=re.IGNORECASE | re.MULTILINE):
                regiones.append((m.start("valor"), m.end("valor"), m.group("valor"),
                                 token, "CABECERA"))
        for m in RX_TITULO_NOMBRE.finditer(texto):
            regiones.append((m.start(2), m.end(2), m.group(2), "[NOMBRE]", "NOMBRES (Dr./Sr.)"))

    # 2) ESPECIALIDADES
    if opciones.get("ESPECIALIDAD", True):
        for m in _buscar(RX_ESPECIALIDAD.pattern, texto, flags=re.IGNORECASE):
            regiones.append((m.start(), m.end(), m.group(0),
                             "[ESPECIALIDAD]", "ESPECIALIDAD"))

    # 3) IDENTIFICADORES
    for nombre, (patron, reemplazo) in PATRONES.items():
        clave_opcion = nombre.removesuffix("_CTX") if nombre.endswith("_CTX") else nombre
        if not opciones.get(clave_opcion, True):
            continue
        for m in _buscar(patron, texto, flags=re.IGNORECASE):
            try:
                repl = m.expand(reemplazo)
            except Exception:
                repl = reemplazo
            regiones.append((m.start(), m.end(), m.group(0), repl, nombre))

    # 4) FECHAS
    if modo_fechas == "Borrar":
        for m in _buscar(RX_FECHA_SLASH.pattern, texto):
            regiones.append((m.start(), m.end(), m.group(0), "[FECHA]", "FECHAS"))
        for m in _buscar(RX_FECHA_TEXTO.pattern, texto, flags=re.IGNORECASE):
            regiones.append((m.start(), m.end(), m.group(0), "[FECHA]", "FECHAS"))
    elif modo_fechas == "No tocar":
        pass
    else:
        for m in _buscar(RX_FECHA_SLASH.pattern, texto):
            regiones.append((m.start(), m.end(), m.group(0),
                             desplazar_fecha_slash(m, dias), "FECHAS"))
        for m in _buscar(RX_FECHA_TEXTO.pattern, texto, flags=re.IGNORECASE):
            regiones.append((m.start(), m.end(), m.group(0),
                             desplazar_fecha_texto(m, dias), "FECHAS"))

    # 5) NOMBRES (IA)
    hacer_pac = opciones.get("NER_PACIENTES", True)
    hacer_med = opciones.get("NER_MEDICOS", False)
    hacer_otr = opciones.get("NER_OTROS", True)

    # Nombres del diccionario aprendido (siempre se aplican si NER está activo,
    # aunque Ollama no esté disponible)
    dic_nombres = cargar_diccionario_aprendido() if (hacer_pac or hacer_med or hacer_otr) else set()

    nombres_ia = set()
    if (hacer_pac or hacer_med or hacer_otr) and ollama_info.get("ok"):
        pasadas = sum([hacer_pac, hacer_med, hacer_otr])
        if progreso: progreso.text(f"🤖 IA buscando nombres ({pasadas} pasada/s)...")
        nombres_ia, errores_ollama = detectar_nombres_ollama(
            texto, modelo, ollama_info["ollama"], hacer_pac, hacer_med, hacer_otr, estado=estado)

    # Unir IA + diccionario aprendido y expandir
    todos = nombres_ia | dic_nombres
    if todos:
        for nd in _nombres_validos(expandir_nombres(todos)):
            for m in _buscar(patron_nombre(nd), texto, flags=re.IGNORECASE):
                categoria = "NOMBRES (IA)" if nd in nombres_ia else "NOMBRES (dic.)"
                regiones.append((m.start(), m.end(), m.group(0),
                                 "[NOMBRE]", categoria))

    # RESOLVER SOLAPAMIENTOS: las más largas ganan; descartar solapadas
    regiones.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    aceptadas = []
    ultimo_fin = -1
    for r in regiones:
        if r[0] >= ultimo_fin:
            aceptadas.append(r)
            ultimo_fin = r[1]

    # APLICAR Y REGISTRAR POSICIONES FINALES
    sustituciones = []
    partes = []
    pos_orig = 0
    pos_final_actual = 0
    for ini, fin, orig, repl, cat in aceptadas:
        trozo_pre = texto[pos_orig:ini]
        partes.append(trozo_pre)
        pos_final_actual += len(trozo_pre)
        # Aquí va la sustitución
        pos_ini_final = pos_final_actual
        partes.append(repl)
        pos_final_actual += len(repl)
        pos_fin_final = pos_final_actual
        sustituciones.append({
            "original": orig, "etiqueta": repl,
            "pos_ini": pos_ini_final, "pos_fin": pos_fin_final,
            "categoria": cat,
        })
        pos_orig = fin
    partes.append(texto[pos_orig:])
    resultado = "".join(partes)

    # Contadores por categoría
    for s in sustituciones:
        contador[s["categoria"]] = contador.get(s["categoria"], 0) + 1

    return resultado, contador, errores_ollama, sustituciones


# Estimación de segundos por documento según tamaño del modelo (CPU).
# Son aproximaciones muy gruesas; sólo sirven para la cifra de aviso en UI.
_ETA_MODELO_CPU = [
    ("1.5b", 15), ("3b", 40), ("7b", 120), ("8b", 150),
    ("12b", 240), ("14b", 300), ("27b", 600), ("32b", 700), ("70b", 1800),
]
def reconstruir_sustituciones(original, anonimizado):
    """Devuelve lista de {original, etiqueta} alineando los textos.
    Usa difflib para detectar bloques sustituidos en orden de aparición."""
    import difflib
    sm = difflib.SequenceMatcher(None, original, anonimizado, autojunk=False)
    resultado = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        # bloque cambiado: original[i1:i2] -> anonimizado[j1:j2]
        orig_chunk = original[i1:i2]
        anon_chunk = anonimizado[j1:j2]
        # Detectar todas las etiquetas dentro del chunk anonimizado
        etiquetas = list(re.finditer(r'\[[A-Z_]+\]', anon_chunk))
        if not etiquetas:
            continue
        if len(etiquetas) == 1:
            resultado.append({"original": orig_chunk.strip(), "etiqueta": etiquetas[0].group()})
        else:
            # Varias etiquetas en un solo bloque — repartir el original heurísticamente
            partes = re.split(r'\[[A-Z_]+\]', anon_chunk)
            originales = orig_chunk
            for k, m in enumerate(etiquetas):
                izq = partes[k]
                der = partes[k+1] if k+1 < len(partes) else ""
                # Aproximación: el texto original equivalente a esta etiqueta
                # es el trozo entre 'izq' y 'der' en el original
                if izq and izq in originales:
                    originales = originales.split(izq, 1)[1]
                if der and der in originales:
                    valor = originales.split(der, 1)[0]
                    originales = originales.split(der, 1)[1]
                else:
                    valor = originales
                    originales = ""
                if valor.strip():
                    resultado.append({"original": valor.strip(), "etiqueta": m.group()})
    return resultado


def revertir_sustituciones(anonimizado, sustituciones, indices_a_revertir):
    """Revierte selectivamente sustituciones usando posiciones absolutas.
    Procesa de derecha a izquierda para que las posiciones no se descuadren."""
    if not indices_a_revertir:
        return anonimizado
    # Ordenar índices a revertir por pos_ini DESCENDENTE
    a_revertir = sorted(
        [(sustituciones[i]["pos_ini"], sustituciones[i]["pos_fin"], sustituciones[i]["original"])
         for i in indices_a_revertir if 0 <= i < len(sustituciones)],
        key=lambda x: -x[0]
    )
    resultado = anonimizado
    for ini, fin, original in a_revertir:
        resultado = resultado[:ini] + original + resultado[fin:]
    return resultado



    m = (modelo or "").lower()
    for clave, seg in _ETA_MODELO_CPU:
        if clave in m:
            return seg
    return 60


# Formatos de salida (sin cambios respecto a v5)
def estimar_segundos_doc(modelo):
    """Estimación por documento tras optimizaciones (1 pasada, trozos grandes, caché)."""
    if not modelo: return 20
    m = modelo.lower()
    # Valores ajustados: 1 pasada combinada + trozos de 6000 chars = ~1/3 del tiempo anterior
    if "1.5b" in m or "1b" in m: return 6
    if "3b"  in m: return 10
    if "7b"  in m: return 18
    if "8b"  in m: return 20
    if "9b"  in m: return 24
    if "13b" in m: return 40
    if "14b" in m: return 45
    return 20


def seg_por_trozo(config, modelo):
    """Segundos que tarda la IA por trozo de texto en ESTE ordenador (aprendido), o una estimación."""
    return config.get("seg_por_trozo", {}).get(modelo) or estimar_segundos_doc(modelo)


def formatear_duracion(seg):
    seg = max(0, int(round(seg)))
    if seg < 60:
        return f"{seg} s"
    m, s = divmod(seg, 60)
    if m < 60:
        return f"{m} min {s:02d} s" if m < 5 else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def texto_a_markdown(texto, nombre):
    cab = f"# Historia clínica anonimizada\n\n**Archivo:** {nombre}  \n**Fecha anonimización:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
    out = []
    for linea in texto.split("\n"):
        s = linea.strip()
        if re.match(r'^[A-ZÁÉÍÓÚÑ\s/]{3,}:?\s*$', s) and len(s) >= 4:
            out.append(f"\n## {s.rstrip(':')}\n")
        else:
            out.append(linea)
    return cab + "\n".join(out)

def texto_a_pdf(texto, nombre):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
    except ImportError: return None
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    est = getSampleStyleSheet()
    story = [Paragraph("<b>Historia clínica anonimizada</b>", est["Title"]),
             Paragraph(f"Archivo: {nombre}", est["Normal"]),
             Paragraph(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M')}", est["Normal"]),
             Spacer(1, 12)]
    for p in texto.split("\n"):
        p = p.strip()
        if p:
            p = p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(p, est["Normal"]))
            story.append(Spacer(1, 4))
    doc.build(story)
    return buf.getvalue()

def _etiquetas_del_programa():
    etiquetas = {"[NOMBRE]", "[FECHA]", "[ESPECIALIDAD]"}
    for _, repl in PATRONES.values():
        etiquetas |= set(re.findall(r"\[[A-Z_]+\]", repl))
    etiquetas |= {tok for _, tok in CAMPOS_CABECERA}
    return etiquetas

_RX_ETIQUETA_SALIDA = None

def quitar_etiquetas(texto):
    """Borra [NOMBRE], [DNI]... del texto final (menos palabras para analizarlo después).
    Se lleva también un espacio de al lado para no dejar huecos dobles."""
    global _RX_ETIQUETA_SALIDA
    if _RX_ETIQUETA_SALIDA is None:
        alt = "|".join(re.escape(e) for e in sorted(_etiquetas_del_programa(), key=len, reverse=True))
        _RX_ETIQUETA_SALIDA = re.compile(rf"[ \t](?:{alt})|(?:{alt})[ \t]?")
    return _RX_ETIQUETA_SALIDA.sub("", texto)


def generar_salida(texto, base, fmt, quitar=False):
    if quitar:
        texto = quitar_etiquetas(texto)
    if fmt == "md": return texto_a_markdown(texto, base).encode("utf-8"), f"{base}_ANON.md", "text/markdown"
    if fmt == "pdf":
        pdf = texto_a_pdf(texto, base)
        if pdf is None: return texto.encode("utf-8"), f"{base}_ANON.txt", "text/plain"
        return pdf, f"{base}_ANON.pdf", "application/pdf"
    return texto.encode("utf-8"), f"{base}_ANON.txt", "text/plain"


# ==================== INTERFAZ ====================
st.set_page_config(page_title="Anonimizador HC · Javier Casas Nebra", layout="wide")


# ---- Lector de PDF escaneados (OCR): descarga bajo demanda desde la barra lateral ----
def _descargar_en_segundo_plano(cliente, nombre, estado):
    try:
        for ev in cliente.pull(nombre, stream=True):
            estado["hecho"] = getattr(ev, "completed", None) or 0
            estado["total"] = getattr(ev, "total", None) or 0
        estado["fin"] = "ok"
    except Exception as e:
        estado["fin"] = f"No se pudo descargar el lector OCR: {e}"


@st.fragment(run_every=1.0)
def _progreso_descarga_ocr():
    # Se refresca sola cada segundo sin bloquear el resto del programa
    estado = st.session_state.get("descarga_ocr")
    if estado is None:
        return
    if estado.get("fin"):
        del st.session_state["descarga_ocr"]
        if estado["fin"] != "ok":
            st.session_state["msg_ocr"] = estado["fin"]
        comprobar_ollama.clear()  # para que aparezca el modelo recién descargado
        st.rerun()
    total, hecho = estado.get("total", 0), estado.get("hecho", 0)
    st.progress(hecho / total if total else 0.0,
                text=f"Descargando lector OCR… {hecho / 1e6:,.0f} de {total / 1e6:,.0f} MB".replace(",", ".")
                if total else "Descargando lector OCR…")
    st.caption("Puedes seguir usando el programa mientras tanto.")


def seccion_lector_ocr(ollama_info):
    import threading
    st.divider(); st.header("📄 Lector de PDF escaneados")
    modelo_ocr = modelo_ocr_disponible(ollama_info.get("modelos"))
    if modelo_ocr:
        st.caption(f"✅ Instalado (`{modelo_ocr}`). Los PDF escaneados se leen solos; "
                   "tardan más que un PDF con texto.")
        return
    if st.session_state.get("msg_ocr"):
        st.warning(st.session_state.pop("msg_ocr"))
    if "descarga_ocr" in st.session_state:
        _progreso_descarga_ocr()
        return
    st.caption("No está descargado. Sin él, los PDF escaneados (fotos o escaneos sin texto) no se pueden leer.")
    if st.button(f"⬇️ Descargar lector OCR ({TAMANO_OCR_DEFAULT})",
                 help=f"Descarga el modelo {MODELO_OCR_DEFAULT} de Ollama (solo una vez). Todo sigue siendo local."):
        estado = {}
        threading.Thread(target=_descargar_en_segundo_plano,
                         args=(ollama_info["ollama"], MODELO_OCR_DEFAULT, estado), daemon=True).start()
        st.session_state["descarga_ocr"] = estado
        st.rerun()

# Banner fijo discreto con autoría (visible siempre durante el scroll)
st.markdown("""
<style>
.autoria-fija {
    position: fixed;
    bottom: 8px;
    right: 14px;
    z-index: 9999;
    background: rgba(40,40,40,0.78);
    color: #f0f0f0;
    padding: 4px 10px;
    border-radius: 14px;
    font-size: 11px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    letter-spacing: 0.3px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    pointer-events: none;
}
</style>
<div class="autoria-fija">© Javier Casas Nebra</div>
""", unsafe_allow_html=True)

st.title("🔒 Anonimizador de Historias Clínicas")
st.caption("Creado por **Javier Casas Nebra** · 100% local · IA con Ollama · 3 pasadas de detección")

config = cargar_config()
ollama_info = comprobar_ollama()

if ollama_info.get("ok"):
    mods = ollama_info.get("modelos", [])
    if mods: st.success(f"✅ Ollama: {', '.join(mods)}")
    else: st.warning("⚠️ Ollama está corriendo pero no hay modelos descargados. Ejecuta p.ej. `ollama pull qwen2.5:3b-instruct`")
else:
    st.warning("⚠️ Ollama no detectado — IA desactivada")

with st.sidebar:
    st.header("Modo")
    modo = st.radio(" ", ["📄 Un archivo", "📚 Varios archivos"], label_visibility="collapsed")
    st.divider()
    st.header("💾 Formato salida")
    fopts = {"Markdown (.md)": "md", "PDF (.pdf)": "pdf", "Texto (.txt)": "txt"}
    labs = list(fopts.keys())
    idx = [v for v in fopts.values()].index(config.get("formato_salida", "md"))
    fsel = st.radio(" ", labs, index=idx, label_visibility="collapsed")
    formato = fopts[fsel]
    if formato != config.get("formato_salida"): config["formato_salida"] = formato; guardar_config(config)
    opts_etq = ["Desaparece (menos palabras)", "Queda marcado: [NOMBRE], [DNI]…"]
    sel_etq = st.radio("Lo anonimizado, al exportar:", opts_etq,
                       index=0 if config.get("quitar_etiquetas", True) else 1,
                       help="Mientras revisas siempre ves las etiquetas de colores; esto solo afecta al archivo exportado. "
                            "Las fechas desplazadas se mantienen.")
    quitar_etq = sel_etq == opts_etq[0]
    if quitar_etq != config.get("quitar_etiquetas", True):
        config["quitar_etiquetas"] = quitar_etq
        guardar_config(config)
    st.divider()
    st.header("🗓️ Fechas")
    mf = st.radio("¿Fechas?", ["Desplazar (conserva intervalos)", "No tocar", "Borrar"], index=0)
    dd = config.get("desplazamiento_dias", 0)
    if mf.startswith("Desplazar"):
        dd = st.number_input("Días", min_value=-3650, max_value=3650, value=int(dd), step=1)
        if dd != config.get("desplazamiento_dias", 0): config["desplazamiento_dias"] = int(dd); guardar_config(config)
        st.caption(f"📌 {dd} días")
    if ollama_info.get("ok") and ollama_info.get("modelos"):
        st.divider(); st.header("🤖 Modelo IA")
        # Los modelos de "embeddings" no sirven para buscar nombres, y el lector OCR va aparte
        ms = ([m for m in ollama_info["modelos"] if "embed" not in m and not es_modelo_ocr(m)]
              or ollama_info["modelos"])
        modelo_guardado = config.get("modelo_ollama")
        if modelo_guardado and modelo_guardado not in ms:
            st.info(f"ℹ️ El modelo guardado `{modelo_guardado}` no está descargado. "
                    f"Descárgalo con: `ollama pull {modelo_guardado}`")
        # Sin preferencia guardada: el modelo recomendado (rápido) antes que el primero de la lista
        preferidos = [modelo_guardado, MODELO_OLLAMA_DEFAULT, "qwen2.5:3b", "qwen2.5:7b"]
        idx = next((ms.index(m) for m in preferidos if m in ms), 0)
        modelo_sel = st.selectbox("Modelo", ms, index=idx)
        if modelo_sel != config.get("modelo_ollama"): config["modelo_ollama"] = modelo_sel; guardar_config(config)
        if modelo_sel in config.get("seg_por_trozo", {}):
            st.caption(f"⏱️ En este ordenador: ~{formatear_duracion(seg_por_trozo(config, modelo_sel))} "
                       f"por cada ~6.000 caracteres (medido)")
        else:
            st.caption(f"⏱️ ETA aprox. CPU: ~{estimar_segundos_doc(modelo_sel)}s/doc")
        if MODELO_OLLAMA_DEFAULT not in ms:
            st.caption(f"💡 Recomendado para CPU + ES/GL: `{MODELO_OLLAMA_DEFAULT}`")

        # Precalentar modelo (carga en RAM/GPU antes del primer documento)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔥 Precalentar", help="Carga el modelo en memoria para ahorrar 20-30s en el 1er documento"):
                try:
                    with st.spinner("Precalentando..."):
                        ollama_info["ollama"].chat(
                            model=modelo_sel,
                            messages=[{"role": "user", "content": "Hola"}],
                            options={"num_predict": 5}
                        )
                    st.success("✅ Listo")
                except Exception as e:
                    st.error(f"Error: {e}")
        with c2:
            if st.button("🖥️ Ver GPU", help="Muestra si Ollama está usando GPU o solo CPU"):
                try:
                    ps = ollama_info["ollama"].ps()
                    models_running = getattr(ps, "models", None) or (ps.get("models", []) if isinstance(ps, dict) else [])
                    if models_running:
                        for m in models_running:
                            size = getattr(m, "size", None) or (m.get("size", 0) if isinstance(m, dict) else 0)
                            size_vram = getattr(m, "size_vram", None) or (m.get("size_vram", 0) if isinstance(m, dict) else 0)
                            if size_vram > 0:
                                pct = int(100 * size_vram / max(size, 1))
                                st.success(f"⚡ GPU activa ({pct}% del modelo en VRAM)")
                            else:
                                st.info("🐢 Solo CPU (más lento pero funciona)")
                    else:
                        st.info("Modelo no cargado. Pulsa 'Precalentar' primero.")
                except Exception as e:
                    st.error(f"Error: {e}")
    else:
        modelo_sel = MODELO_OLLAMA_DEFAULT
    if ollama_info.get("ok"):
        seccion_lector_ocr(ollama_info)
    st.divider(); st.header("¿Qué anonimizar?")
    opciones = {
        "CABECERA": st.checkbox("Cabeceras", True),
        "DNI": st.checkbox("DNI", True), "NIE": st.checkbox("NIE", True),
        "NSS": st.checkbox("NSS", True), "NASI": st.checkbox("NASI", True),
        "CIP": st.checkbox("CIP/CIPA", True), "TARJETA": st.checkbox("T.Sanitaria", True),
        "TELEFONO": st.checkbox("Teléfonos", True), "EMAIL": st.checkbox("Emails", True),
        "CP": st.checkbox("CP", True), "NHC": st.checkbox("NHC", True),
        "DIRECCION": st.checkbox("Direcciones", True),
        "ESPECIALIDAD": st.checkbox("Especialidades", True),
        "INICIALES": st.checkbox("Iniciales usuario", True),
        "NER_PACIENTES": st.checkbox("🤖 IA: nombres de PACIENTES", True, disabled=not ollama_info.get("ok")),
        "NER_MEDICOS":   st.checkbox("🤖 IA: nombres de MÉDICOS", False, disabled=not ollama_info.get("ok"), help="Desmarca si quieres conservar los nombres de los facultativos (trazabilidad, docencia)"),
        "NER_OTROS":     st.checkbox("🤖 IA: otros (familiares, enfermería)", True, disabled=not ollama_info.get("ok")),
    }

    # ---- Presets guardados ----
    st.divider(); st.header("💾 Presets de configuración")
    presets = cargar_presets()
    if presets:
        preset_names = ["(ninguno)"] + list(presets.keys())
        preset_sel = st.selectbox("Aplicar preset", preset_names)
        if preset_sel != "(ninguno)":
            if st.button("📥 Cargar preset seleccionado"):
                p = presets[preset_sel]
                if "modelo_ollama" in p: config["modelo_ollama"] = p["modelo_ollama"]
                if "desplazamiento_dias" in p: config["desplazamiento_dias"] = p["desplazamiento_dias"]
                if "formato_salida" in p: config["formato_salida"] = p["formato_salida"]
                guardar_config(config)
                st.success(f"Preset '{preset_sel}' cargado. La página se recargará.")
                st.rerun()
            if st.button("🗑️ Eliminar preset"):
                del presets[preset_sel]
                guardar_presets(presets)
                st.rerun()
    nuevo_nombre = st.text_input("Guardar config actual como preset", placeholder="ej: 'oncológico', 'lote-rápido'")
    if st.button("💾 Guardar preset") and nuevo_nombre.strip():
        presets[nuevo_nombre.strip()] = {
            "modelo_ollama": config.get("modelo_ollama"),
            "desplazamiento_dias": config.get("desplazamiento_dias"),
            "formato_salida": config.get("formato_salida"),
        }
        guardar_presets(presets)
        st.success(f"Preset '{nuevo_nombre}' guardado")
        st.rerun()

    # ---- Memoria aprendida (diccionario + exclusiones) ----
    st.divider(); st.header("📚 Memoria aprendida")
    aprender_auto = st.checkbox(
        "Aprender automáticamente al exportar/descargar", True,
        help="Al descargar un documento revisado: los nombres confirmados pasan al diccionario, "
             "los revertidos a exclusiones y los [NOMBRE] que escribas en el editor también se aprenden.")

    _LISTAS = {"dic": (cargar_diccionario_aprendido, guardar_diccionario_aprendido),
               "exc": (cargar_exclusiones, guardar_exclusiones)}

    def _añadir_a_lista(clave):
        # Callback: se ejecuta antes de redibujar, así puede vaciar la casilla
        valor = " ".join(st.session_state.get(f"add_{clave}", "").split())
        if valor:
            cargar, guardar = _LISTAS[clave]
            items, añadidos = _fusionar(cargar(), [valor])
            guardar(items)
            # Un término no puede estar a la vez en diccionario y exclusiones
            cargar_otra, guardar_otra = _LISTAS["exc" if clave == "dic" else "dic"]
            guardar_otra({x for x in cargar_otra() if _normalizar(x) != _normalizar(valor)})
            st.session_state[f"msg_{clave}"] = (f"✅ Añadido: {valor}" if añadidos
                                                else f"Ya estaba: {valor}")
        st.session_state[f"add_{clave}"] = ""

    def _quitar_de_lista(clave, valor):
        cargar, guardar = _LISTAS[clave]
        guardar({x for x in cargar() if x != valor})

    def _lista_editable(titulo, items, guardar, clave, placeholder, ayuda):
        st.text_input(f"Añadir a {titulo.lower()}", placeholder=placeholder,
                      help=ayuda, key=f"add_{clave}")
        st.button("➕ Añadir", key=f"btn_add_{clave}", on_click=_añadir_a_lista, args=(clave,))
        if st.session_state.get(f"msg_{clave}"):
            st.success(st.session_state.pop(f"msg_{clave}"))
        if items:
            with st.expander(f"Ver / eliminar {titulo.lower()} ({len(items)})"):
                filtro = st.text_input("Buscar", key=f"filtro_{clave}")
                visibles = [x for x in sorted(items, key=_normalizar)
                            if _normalizar(filtro) in _normalizar(x)]
                for n in visibles[:200]:
                    c1, c2 = st.columns([4, 1])
                    c1.write(f"• {n}")
                    c2.button("🗑️", key=f"del_{clave}_{n}", on_click=_quitar_de_lista, args=(clave, n))
                if len(visibles) > 200:
                    st.caption(f"… y {len(visibles) - 200} más (usa el buscador)")

    dic_actual = cargar_diccionario_aprendido()
    exc_actual = cargar_exclusiones()
    st.caption(f"**{len(dic_actual)}** nombres que se anonimizan SIEMPRE · "
               f"**{len(exc_actual)}** exclusiones que NUNCA se anonimizan como nombre")

    tab_dic, tab_exc, tab_comp = st.tabs(["Diccionario", "Exclusiones", "Compartir"])
    with tab_dic:
        _lista_editable("Diccionario", dic_actual, guardar_diccionario_aprendido, "dic",
                        "ej: 'García López'",
                        "Se anonimizará en todos los futuros documentos, aunque la IA falle.")
    with tab_exc:
        _lista_editable("Exclusiones", exc_actual, guardar_exclusiones, "exc",
                        "ej: 'Juan Canalejo'",
                        "Falsos positivos: la IA no los volverá a marcar como nombre.")
    with tab_comp:
        st.caption("⚠️ El diccionario contiene nombres reales. Compártelo solo por canales "
                   "seguros del hospital, nunca por correo personal o mensajería.")
        ts_mem = datetime.now().strftime('%Y%m%d')
        modo_mem = st.radio("Exportar como",
                            ["Un solo archivo (nombres + exclusiones)", "Archivos separados"],
                            key="modo_export_memoria")
        if modo_mem.startswith("Un solo"):
            st.download_button("⬇️ Exportar memoria", exportar_memoria_json(),
                               file_name=f"memoria_anonimizador_{ts_mem}.json",
                               mime="application/json")
        else:
            st.download_button("⬇️ Exportar diccionario", exportar_memoria_json(incluir_exclusiones=False),
                               file_name=f"diccionario_aprendido_{ts_mem}.json",
                               mime="application/json")
            st.download_button("⬇️ Exportar exclusiones", exportar_memoria_json(incluir_nombres=False),
                               file_name=f"exclusiones_aprendidas_{ts_mem}.json",
                               mime="application/json")

        st.markdown("**Importar memoria de otro residente**")
        archivos_mem = st.file_uploader(
            "Archivos .json (combinado, diccionario o exclusiones)", type=["json"],
            accept_multiple_files=True, key="import_memoria")
        if archivos_mem:
            imp_n, imp_e, errores_imp = [], [], []
            for f in archivos_mem:
                try:
                    n, e = leer_memoria_importada(f.getvalue())
                    imp_n += n; imp_e += e
                except ValueError as ex:
                    errores_imp.append(f"{f.name}: {ex}")
            for err in errores_imp:
                st.error(err)
            if imp_n or imp_e:
                pn, pe, pc = fusionar_memoria(imp_n, imp_e, guardar=False)
                st.caption(f"Se añadirían **{pn}** nombres y **{pe}** exclusiones nuevas"
                           + (f" · {len(pc)} en conflicto (se quedan en el diccionario)" if pc else ""))
                if st.button("🔀 Fusionar con mi memoria", type="primary"):
                    n_n, n_e, confl = fusionar_memoria(imp_n, imp_e)
                    st.session_state["msg_memoria"] = (
                        f"✅ Fusionado: {n_n} nombres y {n_e} exclusiones nuevas"
                        + (f". Conflictos resueltos a favor del diccionario: {', '.join(confl[:10])}"
                           + ("…" if len(confl) > 10 else "") if confl else ""))
                    st.rerun()
        if st.session_state.get("msg_memoria"):
            st.success(st.session_state.pop("msg_memoria"))



# ==================== UTILIDADES PARA MODO LOTE ====================

# Detecta encabezados típicos de actos clínicos
PATRONES_INICIO_ACTO = [
    r'(?:^|\n)\s*INFORME\s+DE\s+[A-ZÁÉÍÓÚÑ\s]+',
    r'(?:^|\n)\s*CONSULTA\s+DE\s+[A-ZÁÉÍÓÚÑ\s]+',
    r'(?:^|\n)\s*EVOLUCI[OÓ]N\s+DE\s+ENFERMER[IÍ]A',
    r'(?:^|\n)\s*HOJA\s+DE\s+EVOLUCI[OÓ]N',
    r'(?:^|\n)\s*INFORME\s+QUIR[UÚ]RGICO',
    r'(?:^|\n)\s*INFORME\s+DE\s+ANATOM[IÍ]A\s+PATOL[OÓ]GICA',
    r'(?:^|\n)\s*INFORME\s+DE\s+ALTA',
    r'(?:^|\n)\s*INFORME\s+DE\s+URGENCIAS',
    r'(?:^|\n)\s*ANAMNESIS\s*:?',
    r'(?:^|\n)\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s*[-–:]\s*[A-Za-zÁÉÍÓÚÑáéíóúñ]',
    r'(?:^|\n)\s*-{5,}\s*\n',
    r'(?:^|\n)\s*={5,}\s*\n',
]
RX_INICIO_ACTO = re.compile('|'.join(PATRONES_INICIO_ACTO), re.IGNORECASE | re.MULTILINE)


def detectar_fecha_acto(texto_acto):
    """Devuelve la primera fecha encontrada en el acto (datetime) o None."""
    m = RX_FECHA_SLASH.search(texto_acto)
    if m:
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100: y += 2000 if y < 50 else 1900
            return datetime(y, mo, d)
        except ValueError:
            pass
    m2 = RX_FECHA_TEXTO.search(texto_acto)
    if m2:
        try:
            return datetime(int(m2.group(3)), MESES[m2.group(2).lower()], int(m2.group(1)))
        except (ValueError, KeyError):
            pass
    return None


def trocear_en_actos(texto, nombre_origen):
    """Devuelve lista de dicts {origen, fecha, texto}."""
    posiciones = [m.start() for m in RX_INICIO_ACTO.finditer(texto)]
    if not posiciones or posiciones[0] > 0:
        posiciones = [0] + posiciones
    actos = []
    for i, ini in enumerate(posiciones):
        fin = posiciones[i+1] if i+1 < len(posiciones) else len(texto)
        trozo = texto[ini:fin].strip()
        if len(trozo) < 30: continue
        actos.append({
            "origen": nombre_origen,
            "fecha": detectar_fecha_acto(trozo),
            "texto": trozo,
        })
    return actos


# ==================== RESALTADO HTML PARA VISTA COLOREADA ====================

ETIQUETAS_COLORES = {
    "[DNI]": "#ffcccc", "[NIE]": "#ffcccc", "[NSS]": "#ffcccc",
    "[NASI]": "#ffcccc", "[CIP]": "#ffcccc", "[NHC]": "#ffcccc",
    "[TARJETA_SANITARIA]": "#ffcccc", "[NUM_SEG_SOCIAL]": "#ffcccc",
    "[HULA_HC]": "#ffcccc", "[SERGAS_ID]": "#ffcccc",
    "[EXPLORACION_ID]": "#ffcccc", "[EPISODIO]": "#ffcccc",
    "[NOMBRE]": "#fff4a3",
    "[FECHA]": "#cce5ff",
    "[ANONIMIZADO]": "#d4edda", "[DIRECCION]": "#d4edda",
    "[CODIGO_POSTAL]": "#d4edda",
    "[ESPECIALIDAD]": "#e6ccff", "[INICIALES]": "#e6ccff",
    "[TELEFONO]": "#ffd9b3", "[EMAIL]": "#ffd9b3",
}



# ==================== INTERFAZ MODO LOTE NUEVO ====================
def revertidos_de(doc):
    """Índices de las sustituciones deshechas en este documento (se modifica en sitio)."""
    return doc.setdefault("revertidos", set())


def texto_con_reversiones(anon_base, sustituciones, revertidos):
    """Texto a mostrar tras deshacer las sustituciones marcadas, y posiciones de las que siguen activas.
    Los índices de `revertidos` son los de la lista `sustituciones` (no los del orden por posición)."""
    sus_activas, partes = [], []
    cursor = offset = 0
    for i in sorted(range(len(sustituciones)), key=lambda k: sustituciones[k]["pos_ini"]):
        s = sustituciones[i]
        partes.append(anon_base[cursor:s["pos_ini"]])
        if i in revertidos:
            partes.append(s["original"])
            offset += len(s["original"]) - (s["pos_fin"] - s["pos_ini"])
        else:
            partes.append(anon_base[s["pos_ini"]:s["pos_fin"]])
            sus_activas.append({
                "original": s["original"], "etiqueta": s["etiqueta"],
                "pos_ini": s["pos_ini"] + offset, "pos_fin": s["pos_fin"] + offset,
                "categoria": s.get("categoria", ""),
            })
        cursor = s["pos_fin"]
    partes.append(anon_base[cursor:])
    return "".join(partes), sus_activas


def texto_final_doc(doc):
    """(revertidos, texto revisado, texto final). El final es el del editor si se ha tocado a mano."""
    revertidos = revertidos_de(doc)
    mostrado, _ = texto_con_reversiones(doc["anon_base"], doc["sustituciones"], revertidos)
    final = doc["editor"] if doc.get("editor") is not None else mostrado
    return revertidos, mostrado, final


def posiciones_en_mostrado(sustituciones, revertidos):
    """(ini, fin) de cada sustitución en el texto mostrado, esté deshecha o no."""
    pos, offset = {}, 0
    for i in sorted(range(len(sustituciones)), key=lambda k: sustituciones[k]["pos_ini"]):
        s = sustituciones[i]
        largo_base = s["pos_fin"] - s["pos_ini"]
        largo = len(s["original"]) if i in revertidos else largo_base
        ini = s["pos_ini"] + offset
        pos[i] = (ini, ini + largo)
        offset += largo - largo_base
    return pos


def _enesima(texto, sub, n):
    k = -1
    for _ in range(n + 1):
        k = texto.find(sub, k + 1)
        if k < 0:
            return None
    return k


def trasladar_reversiones(editor, mostrado_prev, pos_prev, cambios):
    """Lleva al texto editado a mano lo que se acaba de deshacer/rehacer.
    `cambios`: {i: texto nuevo}. Localiza cada trozo por su contexto; si el usuario ha
    reescrito justo esa zona, no se toca. Devuelve (texto, n_no_aplicados)."""
    fallidos = 0
    for i in sorted(cambios, key=lambda k: pos_prev[k][0], reverse=True):
        ini, fin = pos_prev[i]
        viejo = mostrado_prev[ini:fin]
        contexto = mostrado_prev[max(0, ini - 30):ini] + viejo + mostrado_prev[fin:fin + 30]
        j = editor.find(contexto)
        if j >= 0 and editor.find(contexto, j + 1) < 0:
            k = j + (ini - max(0, ini - 30))
        else:
            k = _enesima(editor, viejo, mostrado_prev.count(viejo, 0, ini))
        if k is None:
            fallidos += 1
            continue
        editor = editor[:k] + cambios[i] + editor[k + len(viejo):]
    return editor, fallidos


def segmentos_vista(anon_base, sustituciones, revertidos):
    segs, cursor = [], 0
    for i in sorted(range(len(sustituciones)), key=lambda k: sustituciones[k]["pos_ini"]):
        s = sustituciones[i]
        if s["pos_ini"] > cursor:
            segs.append({"t": anon_base[cursor:s["pos_ini"]]})
        etq = anon_base[s["pos_ini"]:s["pos_fin"]]
        segs.append({"i": i, "etq": etq, "orig": s["original"], "rev": i in revertidos,
                     "color": ETIQUETAS_COLORES.get(etq, "#cce5ff")})  # fechas desplazadas -> azul
        cursor = s["pos_fin"]
    segs.append({"t": anon_base[cursor:]})
    return segs


def render_revision(doc, idx_doc):
    """Vista coloreada + editor (componente propio). Devuelve (revertidos, texto revisado, texto final)."""
    sus = doc["sustituciones"]
    uid = doc.get("uid", idx_doc)
    clave = f"revisor_{idx_doc}"
    revertidos = revertidos_de(doc)

    # 1) Lo que llega del componente: dobles clics y texto del editor
    valor = st.session_state.get(clave) or {}
    vistos = doc.setdefault("eventos_vistos", [])
    if valor.get("uid") == uid:
        for ev in valor.get("eventos") or []:
            if ev.get("id") in vistos:
                continue
            vistos.append(ev.get("id"))
            i = ev.get("i", -1)
            if ev.get("tipo") == "alternar" and 0 <= i < len(sus):
                (revertidos.discard if i in revertidos else revertidos.add)(i)
        del vistos[:-200]
        if valor.get("editor") is not None and valor.get("ver") == doc.get("editor_ver", 0):
            doc["editor"] = valor["editor"]

    # 2) Si has editado a mano y cambian las reversiones, se llevan también al editor
    previo = doc.get("rev_prev")
    if (previo is not None and previo != revertidos and doc.get("editor") is not None
            and doc.get("mostrado_prev") is not None):
        cambios = {i: (sus[i]["original"] if i in revertidos
                       else doc["anon_base"][sus[i]["pos_ini"]:sus[i]["pos_fin"]])
                   for i in previo ^ revertidos}
        doc["editor"], fallidos = trasladar_reversiones(
            doc["editor"], doc["mostrado_prev"], posiciones_en_mostrado(sus, previo), cambios)
        doc["editor_ver"] = doc.get("editor_ver", 0) + 1
    else:
        fallidos = 0
    mostrado, _ = texto_con_reversiones(doc["anon_base"], sus, revertidos)
    doc["rev_prev"], doc["mostrado_prev"] = set(revertidos), mostrado
    final = doc["editor"] if doc.get("editor") is not None else mostrado

    # 3) Pintar
    _componente_revisor(segmentos=segmentos_vista(doc["anon_base"], sus, revertidos),
                        editor=final, ver=doc.get("editor_ver", 0), vistos=vistos[-100:],
                        uid=uid, alto=600, key=clave, default=None)
    if fallidos:
        st.warning(f"⚠️ {fallidos} cambio/s no se han podido pasar al editor porque esa zona "
                   "la habías reescrito a mano. Revísalo allí.")
    return revertidos, mostrado, final


def buscar_nombres_nuevos_en_doc(doc, idx_doc):
    """Aplica al documento ya procesado solo los nombres añadidos al diccionario después. Sin IA."""
    pendientes = nombres_pendientes(doc.get("dic_aplicado", set()))
    if not pendientes:
        return 0
    texto, sus, n = aplicar_nombres_nuevos(doc["anon_base"], doc["sustituciones"], pendientes)
    doc["dic_aplicado"] = doc.get("dic_aplicado", set()) | pendientes
    if n:
        doc["anon_base"], doc["sustituciones"] = texto, sus
        doc["cont"]["NOMBRES (dic.)"] = doc["cont"].get("NOMBRES (dic.)", 0) + n
        doc["rev_prev"] = doc["mostrado_prev"] = None  # las posiciones han cambiado
        if doc.get("editor") is not None:
            doc["editor"] = marcar_nombres_en_texto(doc["editor"], pendientes)
            doc["editor_ver"] = doc.get("editor_ver", 0) + 1
    return n


# ==================== TRABAJO EN SEGUNDO PLANO CON BARRA DE PROGRESO ====================
# El documento se procesa en un hilo aparte y el trabajo se guarda en session_state:
# si el usuario toca algo mientras tanto, Streamlit reinicia el script, pero el trabajo
# sigue corriendo y la barra se vuelve a enganchar a él (no se pierde ni empieza de cero).

def _procesar_documento(nombre, datos, estado, opciones, mf, dd, ollama_info, modelo, modelo_ocr):
    estado["fase"] = "leyendo"
    try:
        f = io.BytesIO(datos)
        f.name = nombre
        ocr = ({"cliente": ollama_info["ollama"], "modelo": modelo_ocr, "estado": estado}
               if modelo_ocr else None)
        texto = extraer_texto_archivo(f, ocr)
        estado["fase"] = "analizando"
        dic = cargar_diccionario_aprendido()
        anon, cont, errs, sus = anonimizar(texto, opciones, mf, dd, ollama_info, modelo, estado=estado)
        return {"nombre": nombre, "original": texto, "anon": anon, "anon_base": anon, "cont": cont,
                "ok": True, "sustituciones": sus, "revertidos": set(), "errs": errs, "dic_aplicado": dic}
    except Cancelado:
        raise
    except Exception as e:
        return {"nombre": nombre, "original": "", "anon": "", "cont": {}, "ok": False, "error": str(e)}
    finally:
        estado["fase"] = "hecho"


def iniciar_trabajo(archivos, opciones, mf, dd, ollama_info, modelo, paralelismo, clave):
    import concurrent.futures, time
    ia = bool(ollama_info.get("ok")) and any(
        opciones.get(k) for k in ("NER_PACIENTES", "NER_MEDICOS", "NER_OTROS"))
    modelo_ocr = modelo_ocr_disponible(ollama_info.get("modelos")) if ollama_info.get("ok") else None
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=paralelismo)
    estados = [{"fase": "en cola"} for _ in archivos]
    futuros = [ex.submit(_procesar_documento, f.name, f.getvalue(), est, dict(opciones),
                         mf, dd, ollama_info, modelo, modelo_ocr)
               for f, est in zip(archivos, estados)]
    return {"clave": clave, "ex": ex, "futuros": futuros, "estados": estados,
            "nombres": [f.name for f in archivos], "inicio": time.time(), "modelo": modelo, "ia": ia,
            "modelo_ocr": modelo_ocr, "paralelismo": paralelismo}


def cancelar_trabajo(clave_estado):
    trabajo = st.session_state.pop(clave_estado, None)
    if trabajo:
        for e in trabajo["estados"]:
            e["cancelar"] = True
        trabajo["ex"].shutdown(wait=False, cancel_futures=True)
        st.session_state[f"{clave_estado}_cancelado"] = trabajo["clave"]


def _mediana(valores, por_defecto):
    valores = sorted(valores)
    return valores[len(valores) // 2] if valores else por_defecto


def calcular_progreso(trabajo, seg_trozo, seg_pagina):
    """Devuelve (fracción 0-1, texto para la barra, segundos transcurridos).
    Todo se mide en segundos estimados: páginas escaneadas (OCR) + trozos de texto para la IA."""
    import time
    ahora = time.time()
    estados = trabajo["estados"]
    peso_final = 1.0  # reglas y demás: casi instantáneo
    conocidos = [e["total"] for e in estados if "total" in e]
    media = (sum(conocidos) / len(conocidos)) if conocidos else (1 if trabajo["ia"] else 0)
    # Tiempos reales de ESTE trabajo si ya hay alguno medido (lo ya analizado otras veces
    # sale de la caché al instante y no sirve para estimar)
    seg_trozo = _mediana([d for e in estados for d in e.get("duraciones", [])], seg_trozo)
    seg_pagina = _mediana([d for e in estados for d in e.get("ocr_duraciones", [])], seg_pagina)
    en_paralelo = max(1, min(trabajo.get("paralelismo", 1),
                             sum(1 for e in estados if e.get("fase") != "hecho")))

    def parcial(inicio, seg):
        # Avance estimado dentro de la página/trozo en curso (sin pasar del 90% hasta que termine)
        return min(0.9, (ahora - inicio) / seg) if inicio else 0.0

    total = hecho = 0.0
    for e in estados:
        doc = e.get("ocr_total", 0) * seg_pagina + e.get("total", media) * seg_trozo + peso_final
        total += doc
        if e.get("fase") == "hecho":
            hecho += doc
            continue
        hecho += (e.get("ocr_hechas", 0) + parcial(e.get("inicio_pagina"), seg_pagina)) * seg_pagina
        hecho += (e.get("hechos", 0) + parcial(e.get("inicio_trozo"), seg_trozo)) * seg_trozo
    p = hecho / total if total else 0.0
    transcurrido = ahora - trabajo["inicio"]
    restante = max(0.0, total - hecho) / en_paralelo

    n = len(estados)
    n_hechos = sum(1 for e in estados if e.get("fase") == "hecho")
    e0 = estados[0]
    if any(e.get("fase") == "ocr" for e in estados):
        accion = "🔎 Leyendo PDF escaneado (OCR)"
    elif any(e.get("fase") == "leyendo" for e in estados) and not conocidos:
        accion = "📄 Leyendo el documento"
    elif trabajo["ia"]:
        accion = "🤖 La IA está buscando nombres"
    else:
        accion = "⚙️ Anonimizando"
    if n > 1:
        detalle = f"documento {min(n_hechos + 1, n)} de {n}"
    elif e0.get("fase") == "ocr" and e0.get("ocr_total"):
        detalle = f"página {min(e0.get('ocr_hechas', 0) + 1, e0['ocr_total'])} de {e0['ocr_total']}"
    elif conocidos and conocidos[0] > 1:
        detalle = f"parte {min(e0.get('hechos', 0) + 1, conocidos[0])} de {conocidos[0]}"
    else:
        detalle = ""
    texto = " · ".join(x for x in [accion, detalle, f"{int(p * 100)}%"] if x)
    if trabajo["ia"] or restante >= 5:
        texto += f" · quedan ~{formatear_duracion(restante)}"
        if restante >= 90:
            texto += f" (acabará hacia las {(datetime.now() + timedelta(seconds=restante)):%H:%M})"
    return p, texto, transcurrido


def seguir_trabajo(clave_estado, config):
    """Muestra la barra de progreso hasta que termina el trabajo guardado en session_state
    y devuelve la lista de resultados. Si se cancela, recarga la página."""
    import time
    trabajo = st.session_state[clave_estado]
    hueco_cancelar = st.empty()
    if hueco_cancelar.button("⏹️ Cancelar", key=f"btn_cancelar_{clave_estado}"):
        cancelar_trabajo(clave_estado)
        st.rerun()
    seg_trozo = seg_por_trozo(config, trabajo["modelo"])
    # Primera vez sin medir: una página escaneada tarda del orden de minuto y medio en CPU
    seg_pagina = config.get("seg_por_pagina_ocr", {}).get(trabajo.get("modelo_ocr")) or 90
    barra = st.progress(0.0, text="Preparando…")
    nota = st.empty()
    while not all(f.done() for f in trabajo["futuros"]):
        p, texto, transcurrido = calcular_progreso(trabajo, seg_trozo, seg_pagina)
        barra.progress(min(p, 0.99), text=texto)
        nota.caption(f"⏱️ Llevas {formatear_duracion(transcurrido)}. Puedes usar otros programas "
                     "mientras tanto; aunque toques algo aquí, el trabajo no se pierde.")
        time.sleep(0.5)

    resultados = []
    for nombre, f in zip(trabajo["nombres"], trabajo["futuros"]):
        try:
            resultados.append(f.result())
        except Exception as e:
            resultados.append({"nombre": nombre, "original": "", "anon": "", "cont": {}, "ok": False,
                               "error": str(e) or "Cancelado"})
    for k, r in enumerate(resultados):
        r["uid"] = f"{trabajo['inicio']:.3f}-{k}"  # identifica el documento en el componente revisor
    transcurrido = time.time() - trabajo["inicio"]
    barra.progress(1.0, text=f"✅ Terminado en {formatear_duracion(transcurrido)}")
    nota.empty()
    hueco_cancelar.empty()

    # Aprender cuánto tardan la IA (por trozo) y el OCR (por página) en este ordenador
    # (mediana: la carga inicial del modelo no pesa)
    for clave_cfg, clave_est, modelo in (("seg_por_trozo", "duraciones", trabajo["modelo"]),
                                         ("seg_por_pagina_ocr", "ocr_duraciones", trabajo.get("modelo_ocr"))):
        duraciones = [d for e in trabajo["estados"] for d in e.get(clave_est, [])]
        if duraciones and modelo:
            medida = _mediana(duraciones, 0)
            tiempos = config.setdefault(clave_cfg, {})
            previo = tiempos.get(modelo)
            tiempos[modelo] = round(medida if previo is None else (previo + medida) / 2, 1)
            guardar_config(config)

    trabajo["ex"].shutdown(wait=False)
    del st.session_state[clave_estado]
    st.toast(f"✅ Anonimización terminada en {formatear_duracion(transcurrido)}")
    return resultados


def render_panel_reversion(doc, texto_revisado=None, texto_editado=None):
    """Pinta el panel de reversión con pestañas por categoría.
    Lo deshecho se guarda en doc["revertidos"]: las casillas solo lo reflejan (Streamlit borra el
    estado de las casillas que no se pintan, p.ej. las de los otros documentos del lote)."""
    sustituciones = doc["sustituciones"]
    if not sustituciones:
        return
    idx_doc = doc.get("uid", "0")
    rev = revertidos_de(doc)

    def _poner(indices, valor):
        for i in indices:
            (rev.add if valor else rev.discard)(i)

    def _alternar(i, clave):
        _poner([i], st.session_state[clave])

    # Etiquetas legibles por categoría
    NOMBRES_CAT = {
        "CABECERA": "🟩 Cabeceras",
        "ESPECIALIDAD": "🟪 Especialidades",
        "DNI": "🟥 DNI",
        "NIE": "🟥 NIE",
        "NSS": "🟥 NSS",
        "NASI": "🟥 NASI",
        "CIP": "🟥 CIP/CIPA",
        "TARJETA": "🟥 Tarjeta sanitaria",
        "TELEFONO": "🟧 Teléfonos",
        "EMAIL": "🟧 Emails",
        "CP": "🟩 Códigos postales",
        "NHC": "🟥 NHC",
        "INICIALES": "🟪 Iniciales",
        "DIRECCION": "🟩 Direcciones",
        "FECHAS": "🟦 Fechas",
        "NOMBRES (IA)": "🟨 Nombres (IA)",
        "NOMBRES (dic.)": "🟨 Nombres (diccionario)",
        "NOMBRES (Dr./Sr.)": "🟨 Nombres (Dr./Sr.)",
        "HULA_HC": "🟥 Historia HULA",
        "SERGAS_ID": "🟥 ID SERGAS",
        "NHC_GALICIA": "🟥 NHC Galicia",
        "EXPLORACION": "🟥 ID Exploración",
        "EPISODIO": "🟥 Episodio",
    }

    # Agrupar índices por categoría
    grupos = {}
    for i, s in enumerate(sustituciones):
        cat = s.get("categoria", "OTROS")
        grupos.setdefault(cat, []).append(i)

    with st.expander(
        f"🔁 Revertir sustituciones · {len(sustituciones)} detectadas en {len(grupos)} categoría/s",
        expanded=False
    ):
        st.caption("Marca/desmarca para revertir · La vista se actualiza automáticamente")

        # Botones globales
        col_a, col_b, col_c, _ = st.columns([1, 1, 1.5, 3])
        todos = range(len(sustituciones))
        with col_a:
            st.button("☑️ Marcar todo", key=f"mark_all_{idx_doc}", on_click=_poner, args=(todos, True))
        with col_b:
            st.button("⬜ Desmarcar todo", key=f"unmark_all_{idx_doc}", on_click=_poner, args=(todos, False))
        with col_c:
            if st.button("📚 Aprender de mis correcciones", key=f"learn_{idx_doc}",
                         help="Nombres confirmados → diccionario · nombres revertidos → exclusiones · "
                              "[NOMBRE] escritos a mano en el editor → diccionario."):
                res = aprender_de_correcciones(sustituciones, rev, texto_revisado, texto_editado)
                st.success(resumen_aprendizaje(res))

        # Pestañas por categoría, ordenadas por número de sustituciones (descendente)
        cats_ordenadas = sorted(grupos.keys(), key=lambda c: -len(grupos[c]))
        nombres_pestanas = [f"{NOMBRES_CAT.get(c, c)} ({len(grupos[c])})" for c in cats_ordenadas]
        tabs_cat = st.tabs(nombres_pestanas)

        for tab_cat, cat in zip(tabs_cat, cats_ordenadas):
            with tab_cat:
                indices = grupos[cat]
                # Botones marcar/desmarcar todos los de esta categoría
                cc1, cc2, _ = st.columns([1, 1, 4])
                with cc1:
                    st.button("☑️ Toda categoría", key=f"mark_cat_{idx_doc}_{cat}",
                              on_click=_poner, args=(indices, True))
                with cc2:
                    st.button("⬜ Ninguna", key=f"unmark_cat_{idx_doc}_{cat}",
                              on_click=_poner, args=(indices, False))

                cols = st.columns(3)
                for n, i in enumerate(indices):
                    sus = sustituciones[i]
                    with cols[n % 3]:
                        etiqueta_corta = sus["etiqueta"]
                        original_corto = sus["original"][:40] + ("…" if len(sus["original"]) > 40 else "")
                        label = f"{etiqueta_corta} ← *{original_corto}*"
                        key_rev = f"rev_{idx_doc}_{i}"
                        st.session_state[key_rev] = i in rev
                        st.checkbox(label, key=key_rev, on_change=_alternar, args=(i, key_rev))


def render_modo_lote(opciones, mf, dd, ollama_info, modelo_sel, formato, aprender_auto, config):
    archivos = st.file_uploader("Sube PDF, MD o TXT (varios)", type=["pdf","md","txt"], accept_multiple_files=True, key="lote_up")
    if not archivos:
        st.info("👆 Sube uno o varios PDFs")
        return

    st.write(f"📚 **{len(archivos)} archivos**")

    # Opciones de exportación
    c1, c2 = st.columns(2)
    with c1:
        modo_export = st.radio(
            "Modo de salida",
            ["Separados (un archivo por documento)",
             "Unidos en un solo Markdown"],
            index=0
        )
    with c2:
        medido = modelo_sel in config.get("seg_por_trozo", {})
        st.caption(f"⏱️ Tiempo aproximado con {modelo_sel}: ~{formatear_duracion(len(archivos) * seg_por_trozo(config, modelo_sel))} "
                   f"({'medido en este ordenador' if medido else 'estimación; se ajusta tras el primer uso'}; "
                   f"los documentos largos tardan más)")
        st.caption(f"🟥 ID  🟨 Nombres  🟦 Fechas  🟩 Cabeceras  🟪 Especialidades  🟧 Contacto")

    # Selector de paralelismo
    c3, _ = st.columns([1, 3])
    with c3:
        paralelismo = st.slider("⚡ Documentos en paralelo", 1, 4, 2,
                                help="Procesar varios documentos a la vez. Consume más RAM pero acelera lotes grandes. Recomendado 2 en CPU, 3-4 con GPU.")

    # PASO 1: Procesar
    if "lote_resultados" not in st.session_state:
        st.session_state["lote_resultados"] = None

    en_marcha = "trabajo_lote" in st.session_state
    if not en_marcha and st.session_state.pop("trabajo_lote_cancelado", None):
        st.warning("⏹️ Anonimización cancelada")
    if not en_marcha and st.button("🚀 Procesar todos", type="primary"):
        st.session_state.pop("trabajo_lote_cancelado", None)
        st.session_state["trabajo_lote"] = iniciar_trabajo(
            archivos, opciones, mf, dd, ollama_info, modelo_sel, paralelismo, "lote")
        en_marcha = True

    if en_marcha:
        nuevos = seguir_trabajo("trabajo_lote", config)
        st.session_state["lote_doc_sel"] = 0  # empezar por el primer documento del lote nuevo
        st.session_state["lote_resultados"] = nuevos
        errs_totales = [e for r in nuevos for e in r.get("errs", [])]
        if errs_totales:
            st.warning(f"⚠️ {len(errs_totales)} fallos en IA. Primero: {errs_totales[0]}")

    resultados = st.session_state.get("lote_resultados")
    if not resultados:
        return

    # PASO 2: Validación con pestañas, vista coloreada con tooltip y panel de reversión
    st.divider()
    st.subheader("✏️ Revisa y edita cada documento antes de exportar")
    st.caption("Pasa el ratón sobre lo coloreado para ver el original; doble clic para deshacerlo. "
               "Escribe tus cambios en el editor de la derecha.")

    docs_ok = [(idx, r) for idx, r in enumerate(resultados) if r["ok"]]
    pendientes = set().union(*(nombres_pendientes(r.get("dic_aplicado", set())) for _, r in docs_ok)) if docs_ok else set()
    # Hueco fijo para avisos que aparecen y desaparecen: así lo de debajo no se redibuja desde cero
    with st.container():
        if pendientes and st.button(f"🔄 Buscar {len(pendientes)} nombre/s nuevo/s del diccionario en los "
                                    f"documentos (rápido, sin repetir la IA)", type="primary", key="reproceso_lote"):
            # Sin st.rerun(): recargar a medias haría que el visor se volviera a crear desde cero
            total = sum(buscar_nombres_nuevos_en_doc(r, str(idx)) for idx, r in docs_ok)
            st.info(f"✅ {total} sustitución/es nueva/s de [NOMBRE]" if total
                    else "No aparecen en los documentos (revisa cómo están escritos)")

    # Selector de documento: recuerda en cuál estabas aunque la página se actualice
    n = len(resultados)
    if not 0 <= st.session_state.get("lote_doc_sel", 0) < n:
        st.session_state["lote_doc_sel"] = 0

    def _mover(paso):
        st.session_state["lote_doc_sel"] = (st.session_state.get("lote_doc_sel", 0) + paso) % n

    c_ant, c_sel, c_sig = st.columns([1, 8, 1])
    c_ant.button("◀", key="doc_anterior", on_click=_mover, args=(-1,), help="Documento anterior")
    c_sel.selectbox("Documento", list(range(n)), key="lote_doc_sel", label_visibility="collapsed",
                    format_func=lambda i: f"📄 {i + 1}/{n} · {resultados[i]['nombre']}"
                                          + ("" if resultados[i]["ok"] else " ❌"))
    c_sig.button("▶", key="doc_siguiente", on_click=_mover, args=(1,), help="Documento siguiente")

    idx = st.session_state["lote_doc_sel"]
    r = resultados[idx]
    if not r["ok"]:
        st.error(f"❌ {r.get('error','Error desconocido')}")
    else:
        revertidos, texto_mostrado, final = render_revision(r, str(idx))
        render_panel_reversion(r, texto_revisado=texto_mostrado, texto_editado=final)
        with st.expander("Detalle de categorías anonimizadas"):
            for k, v in r["cont"].items():
                if v > 0: st.write(f"- **{k}**: {v}")

    # PASO 3: Exportar definitivos
    st.divider()
    if st.button("⬇️ Exportar definitivos", type="primary"):
        total = {"nombres": 0, "exclusiones": 0, "retirados": 0}
        for i, doc in enumerate(resultados):
            if not doc["ok"]:
                continue
            revertidos, texto_mostrado, doc["anon"] = texto_final_doc(doc)
            if aprender_auto:
                res = aprender_de_correcciones(doc["sustituciones"], revertidos, texto_mostrado, doc["anon"])
                for k in total:
                    total[k] += res[k]
        if aprender_auto:
            st.info(resumen_aprendizaje(total))
        exportar_lote(resultados, modo_export, formato, config.get("quitar_etiquetas", True))


def exportar_lote(resultados, modo_export, formato, quitar):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    if modo_export.startswith("Separados"):
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in resultados:
                if not r["ok"]: continue
                base = os.path.splitext(r["nombre"])[0]
                datos, fn, _ = generar_salida(r["anon"], base, formato, quitar)
                zf.writestr(fn, datos)
        st.download_button("⬇️ Descargar ZIP", zip_buf.getvalue(), on_click="ignore",
                           file_name=f"anonimizados_{ts}.zip", mime="application/zip")
        return

    # Modo "Unidos en un solo Markdown"
    partes = [f"# Historia clínica anonimizada (consolidada)\n\n*Fecha de anonimización: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n---\n"]
    for r in resultados:
        if not r["ok"]: continue
        texto = quitar_etiquetas(r["anon"]) if quitar else r["anon"]
        partes.append(f"\n## 📄 {r['nombre']}\n\n{texto}\n\n---\n")
    contenido = "\n".join(partes)

    st.download_button(
        "⬇️ Descargar Markdown unificado",
        contenido.encode("utf-8"),
        file_name=f"historia_consolidada_{ts}.md",
        mime="text/markdown",
        on_click="ignore",
    )

if modo.startswith("📄"):
    archivo = st.file_uploader("Sube PDF, MD o TXT", type=["pdf","md","txt"])
    if archivo:
        # Cachear el resultado por nombre para no reprocesar al cambiar checkboxes
        cache_key = f"un_archivo_{archivo.name}_{archivo.size}"
        if st.session_state.get("un_archivo_cache_key") != cache_key:
            trabajo = st.session_state.get("trabajo_solo")
            if trabajo is not None and trabajo["clave"] != cache_key:
                cancelar_trabajo("trabajo_solo")  # han subido otro archivo
                trabajo = None
            if trabajo is None:
                error_previo = st.session_state.get("un_archivo_error")
                if error_previo and error_previo[0] == cache_key:
                    st.error(f"❌ {error_previo[1]}")
                    st.stop()
                if st.session_state.get("trabajo_solo_cancelado") == cache_key:
                    st.warning("⏹️ Anonimización cancelada")
                    if not st.button("▶️ Anonimizar de nuevo", type="primary"):
                        st.stop()
                st.session_state.pop("trabajo_solo_cancelado", None)
                st.session_state["trabajo_solo"] = iniciar_trabajo(
                    [archivo], opciones, mf, dd, ollama_info, modelo_sel, 1, cache_key)
            r = seguir_trabajo("trabajo_solo", config)[0]
            if not r["ok"]:
                st.session_state["un_archivo_error"] = (cache_key, r["error"])
                st.error(f"❌ {r['error']}")
                st.stop()
            st.session_state["un_archivo_cache_key"] = cache_key
            st.session_state["un_archivo_data"] = {
                "orig": r["original"], "anon_base": r["anon_base"], "cont": r["cont"],
                "errs": r["errs"], "sustituciones": r["sustituciones"], "nombre": r["nombre"],
                "dic_aplicado": r["dic_aplicado"], "uid": r["uid"],
            }

        data = st.session_state.get("un_archivo_data", {})
        idx_doc = "solo"

        cont = data.get("cont", {})
        errs = data.get("errs", [])
        sustituciones = data.get("sustituciones", [])

        # Hueco fijo para avisos que aparecen y desaparecen: así lo de debajo no se redibuja desde cero
        with st.container():
            pendientes = nombres_pendientes(data.get("dic_aplicado", set()))
            if pendientes and st.button(f"🔄 Buscar {len(pendientes)} nombre/s nuevo/s del diccionario "
                                        f"(rápido, sin repetir la IA)", type="primary"):
                # Sin st.rerun(): recargar a medias haría que el visor se volviera a crear desde cero
                n = buscar_nombres_nuevos_en_doc(data, idx_doc)
                st.info(f"✅ {n} sustitución/es nueva/s de [NOMBRE]" if n
                        else "No aparecen en el documento (revisa cómo están escritos)")
            if errs:
                st.warning(f"⚠️ {len(errs)} llamada(s) al modelo IA fallaron. Primer error: {errs[0]}")
            st.success(f"✅ Sustituciones detectadas: {sum(cont.values())}")

        revertidos, texto_mostrado, final = render_revision(data, idx_doc)

        render_panel_reversion(data, texto_revisado=texto_mostrado, texto_editado=final)

        with st.expander("Detalle de categorías anonimizadas"):
            for k, v in cont.items():
                if v > 0: st.write(f"- **{k}**: {v}")

        st.warning("⚠️ Revisa antes de usar.")
        base = os.path.splitext(archivo.name)[0]
        # Exportar en dos pasos: un botón normal recibe siempre lo último escrito en el editor;
        # un botón de descarga directo prepara el archivo ANTES de recibir la última edición.
        if st.button("✅ Exportar", type="primary", key="exportar_solo"):
            if aprender_auto:
                st.info(resumen_aprendizaje(
                    aprender_de_correcciones(sustituciones, revertidos, texto_mostrado, final)))
            quitar = config.get("quitar_etiquetas", True)
            st.session_state["export_solo"] = (cache_key, formato, quitar, final,
                                               generar_salida(final, base, formato, quitar))
        exp = st.session_state.get("export_solo")
        if exp and exp[:4] == (cache_key, formato, config.get("quitar_etiquetas", True), final):
            datos, fn, mime = exp[4]
            st.download_button(f"⬇️ Descargar {fn}", datos, file_name=fn, mime=mime, on_click="ignore")
        elif exp and exp[0] == cache_key:
            st.caption("✏️ Has cambiado el texto o el formato: pulsa **✅ Exportar** otra vez.")
    else:
        st.info("👆 Sube un PDF")
else:
    render_modo_lote(opciones, mf, dd, ollama_info, modelo_sel, formato, aprender_auto, config)
