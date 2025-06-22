import os
import json
import base64
import logging
import requests
import redis
from datetime import datetime, timedelta
from enum import Enum
from flask import Flask, request, Response, send_from_directory, jsonify
import openai
from requests.exceptions import RequestException
from tenacity import retry, stop_after_attempt, wait_fixed
from PIL import Image
import io
import re
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- Configuración de entorno ------------------------------------------------
META_VERIFY_TOKEN     = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID  = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDS_B64")
OCR_SERVICE_URL       = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service-onrender.com/derivar")
GOOGLE_SHEET_NAME     = os.getenv("GOOGLE_SHEET_NAME", "ALIA_Bot_Data")
ALIA_FOLDER_ID        = "14UsGNIz6MBhQNd0gVFeSe3UPBNyB8yrk"

# --- Lista de feriados (actualizar según 2025 en Argentina) ------------------
FERIADOS_2025 = [
    "2025-01-01", "2025-03-03", "2025-03-04", "2025-03-24", "2025-04-02",
    "2025-04-17", "2025-04-18", "2025-05-01", "2025-05-25", "2025-06-20",
    "2025-07-09", "2025-08-17", "2025-10-12", "2025-11-20", "2025-12-08", "2025-12-25"
]

# --- Inicialización de logging -----------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Inicialización de clientes ----------------------------------------------
openai.api_key    = OPENAI_API_KEY
redis_client      = redis.from_url(REDIS_URL, decode_responses=True)
app               = Flask(__name__, static_folder="static")

# --- Inicialización de Google Sheets y Google Drive --------------------------
def init_google_sheets():
    try:
        creds_json = json.loads(base64.b64decode(GOOGLE_CREDS_B64))
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
        client = gspread.authorize(creds)
        return client, creds
    except Exception as e:
        logger.error(f"Error inicializando Google Sheets: {e}")
        raise

sheets_client, sheets_creds = init_google_sheets()

def mover_a_carpeta(sheet, folder_id, creds):
    try:
        drive_service = build("drive", "v3", credentials=creds)
        file_id = sheet.id
import os import json import base64 import logging import requests import redis from datetime import datetime, timedelta from enum import Enum from flask import Flask, request, Response, send_from_directory, jsonify import openai from requests.exceptions import RequestException from tenacity import retry, stop_after_attempt, wait_fixed from PIL import Image import io import re import gspread from google.oauth2.service_account import Credentials from googleapiclient.discovery import build

--- Configuración de entorno ------------------------------------------------

META_VERIFY_TOKEN     = os.getenv("META_VERIFY_TOKEN") META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN") META_PHONE_NUMBER_ID  = os.getenv("META_PHONE_NUMBER_ID") OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY") REDIS_URL             = os.getenv("REDIS_URL") GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDS_B64") OCR_SERVICE_URL       = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr") DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service-onrender.com/derivar") GOOGLE_SHEET_NAME     = os.getenv("GOOGLE_SHEET_NAME", "ALIA_Bot_Data") ALIA_FOLDER_ID        = "14UsGNIz6MBhQNd0gVFeSe3UPBNyB8yrk"

--- Lista de feriados (2025 Argentina) -------------------------------------

FERIADOS_2025 = [ "2025-01-01", "2025-03-03", "2025-03-04", "2025-03-24", "2025-04-02", "2025-04-17", "2025-04-18", "2025-05-01", "2025-05-25", "2025-06-20", "2025-07-09", "2025-08-17", "2025-10-12", "2025-11-20", "2025-12-08", "2025-12-25" ]

--- Logging ---------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s") logger = logging.getLogger(name)

--- Clientes --------------------------------------------------------------

openai.api_key   = OPENAI_API_KEY redis_client     = redis.from_url(REDIS_URL, decode_responses=True) app              = Flask(name, static_folder="static")

--- Google Sheets & Drive ------------------------------------------------

def init_google_sheets(): try: creds_json = json.loads(base64.b64decode(GOOGLE_CREDS_B64)) scopes = [ "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive" ] creds = Credentials.from_service_account_info(creds_json, scopes=scopes) client = gspread.authorize(creds) return client, creds except Exception as e: logger.error(f"Error inicializando Google Sheets: {e}") raise

sheets_client, sheets_creds = init_google_sheets()

def mover_a_carpeta(sheet, folder_id, creds): try: drive = build("drive", "v3", credentials=creds) file_id = sheet.id meta = drive.files().get(fileId=file_id, fields='parents').execute() prev = ",".join(meta.get('parents', [])) drive.files().update( fileId=file_id, addParents=folder_id, removeParents=prev, fields='id, parents' ).execute() logger.info(f"Sheet movido a carpeta: {folder_id}") except Exception as e: logger.error(f"Error moviendo sheet: {e}")

--- Creación de hojas mensuales y diarias ---------------------------------

def get_monthly_sheet(date: datetime, sheet_type: str) -> gspread.Spreadsheet: name = f"{sheet_type}_{date.strftime('%Y-%m')}" try: return sheets_client.open(name) except gspread.exceptions.SpreadsheetNotFound: sheet = sheets_client.create(name) sheet.share(None, perm_type="anyone", role="writer") mover_a_carpeta(sheet, ALIA_FOLDER_ID, sheets_creds) logger.info(f"Hoja mensual creada: {name}") return sheet

def get_daily_worksheet(date: datetime, sheet_type: str) -> gspread.Worksheet: sheet = get_monthly_sheet(date, sheet_type) tab = date.strftime("%Y-%m-%d") try: return sheet.worksheet(tab) except gspread.exceptions.WorksheetNotFound: ws = sheet.add_worksheet(title=tab, rows=100, cols=20) headers = [ "Timestamp", "Nombre", "DNI", "Localidad", "Dirección", "Fecha de Nacimiento", "Edad", "Cobertura", "Afiliado", "Estudios", "Tipo de Atención" ] if sheet_type == "Sedes": headers.append("Sede") elif sheet_type == "Domicilios": headers.append("Domicilio") ws.append_row(headers) logger.info(f"Pestaña creada: {tab} en {sheet.title}") return ws

def get_resultados_sheet() -> gspread.Worksheet: try: book = sheets_client.open(GOOGLE_SHEET_NAME) try: return book.worksheet("Resultados") except gspread.exceptions.WorksheetNotFound: ws = book.add_worksheet(title="Resultados", rows=100, cols=20) ws.append_row(["Timestamp", "Nombre", "DNI", "Localidad"] ) return ws except gspread.exceptions.SpreadsheetNotFound: book = sheets_client.create(GOOGLE_SHEET_NAME) book.share(None, perm_type="anyone", role="writer") mover_a_carpeta(book, ALIA_FOLDER_ID, sheets_creds) ws = book.add_worksheet(title="Resultados", rows=100, cols=20) ws.append_row(["Timestamp", "Nombre", "DNI", "Localidad"] ) return ws

--- Estados del bot -------------------------------------------------------

class BotState(Enum): NONE                           = None MENU                           = "menu" MENU_TURNO                     = "menu_turno" ESPERANDO_NOMBRE               = "esperando_nombre" ESPERANDO_DIRECCION            = "esperando_direccion" ESPERANDO_LOCALIDAD            = "esperando_localidad" ESPERANDO_FECHA_NACIMIENTO     = "esperando_fecha_nacimiento" ESPERANDO_COBERTURA            = "esperando_cobertura" ESPERANDO_AFILIADO             = "esperando_afiliado" ESPERANDO_ORDEN                = "esperando_orden" ESPERANDO_ESTUDIOS_MANUAL      = "esperando_estudios_manual" ESPERANDO_ESTUDIOS_CONFIRMACION= "esperando_estudios_confirmacion" ESPERANDO_RESULTADOS_NOMBRE    = "esperando_resultados_nombre" ESPERANDO_RESULTADOS_DNI       = "esperando_resultados_dni" ESPERANDO_RESULTADOS_LOCALIDAD = "esperando_resultados_localidad"

--- Sesiones y Redis -----------------------------------------------------

def get_paciente(tel: str) -> dict: data = redis_client.get(f"paciente:{tel}") if data: return json.loads(data) p = { "estado":None, "tipo_atencion":None, "nombre":None, "direccion":None, "localidad":None, "fecha_nacimiento":None, "cobertura":None, "afiliado":None, "estudios":None, "imagen_base64":None, "dni":None } save_paciente(tel, p) return p

def save_paciente(tel: str, info: dict): redis_client.set(f"paciente:{tel}", json.dumps(info), ex=86400)

def clear_paciente(tel: str): redis_client.delete(f"paciente:{tel}")

# --- Utilidades generales ----------------------------------------------------
def calcular_edad(fecha_str: str) -> int:
    try:
        nac = datetime.strptime(fecha_str, "%d/%m/%Y")
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except ValueError:
        return None

def validate_fecha_nacimiento(fecha: str) -> bool:
    if re.match(r"^\d{2}/\d{2}/\d{4}$", fecha):
        try:
            datetime.strptime(fecha, "%d/%m/%Y")
            return True
        except ValueError:
            return False
    return False

def validate_afiliado(afiliado: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9]+$", afiliado))

def is_holiday(date: datetime) -> bool:
    return date.strftime("%Y-%m-%d") in FERIADOS_2025

def get_next_business_day(date: datetime, localidad: str) -> tuple:
    loc = (localidad or "").lower()
    target_days = {
        "ituzaingo": [0], "merlo": [1,4], "padua": [1,4],
        "tesei": [2,5], "hurlingham": [2,5], "castelar": [3]
    }.get(loc, [0])
    cd = date
    while True:
        cd += timedelta(days=1)
        if cd.weekday() == 6 or is_holiday(cd):
            continue
        if cd.weekday() in target_days:
            return cd, cd.strftime("%A").capitalize()

def count_domicilio_patients(date: datetime) -> int:
    try:
        ws = get_daily_worksheet(date, "Domicilios")
        return len(ws.get_all_records())
    except Exception as e:
        logger.error(f"Error contando pacientes en Domicilios {date}: {e}")
        return 0

def siguiente_campo_faltante(paciente: dict) -> str:
    pasos = [
        ("nombre", BotState.ESPERANDO_NOMBRE, "Por favor indícanos tu nombre completo:"),
        ("direccion", BotState.ESPERANDO_DIRECCION, "Ahora indícanos tu domicilio (calle y altura):"),
        ("localidad", BotState.ESPERANDO_LOCALIDAD, "¿En qué localidad vivís?"),
        ("fecha_nacimiento", BotState.ESPERANDO_FECHA_NACIMIENTO, "Por favor indícanos tu fecha de nacimiento (dd/mm/aaaa):"),
        ("cobertura", BotState.ESPERANDO_COBERTURA, "¿Cuál es tu cobertura médica?"),
        ("afiliado", BotState.ESPERANDO_AFILIADO, "¿Cuál es tu número de afiliado?")
    ]
    for campo, estado, pregunta in pasos:
        if not paciente.get(campo):
            paciente["estado"] = estado.value
            return pregunta
    return None

def determinar_dia_turno(localidad: str) -> tuple:
    loc = (localidad or "").lower()
    today = datetime.today()
    if "ituzaingo" in loc:
        dias = [0]
    elif "merlo" in loc or "padua" in loc:
        dias = [1,4]
    elif "tesei" in loc or "hurlingham" in loc:
        dias = [2,5]
    elif "castelar" in loc:
        dias = [3]
    else:
        dias = [0]
    cd = today
    while True:
        cd += timedelta(days=1)
        if cd.weekday() == 6 or is_holiday(cd):
            continue
        if cd.weekday() in dias:
            return cd, cd.strftime("%A").capitalize()

def determinar_sede(localidad: str) -> tuple:
    loc = (localidad or "").lower()
    if loc in ["castelar","ituzaingo","moron"]:
        return "CASTELAR","Arias 2530"
    if loc in ["merlo","padua","paso del rey"]:
        return "MERLO","Jujuy 847"
    if loc in ["tesei","hurlingham"]:
        return "TESEI","Concepción Arenal 2694"
    return "GENERAL","Nuestra sede principal"

# --- Registro en Google Sheets ----------------------------------------------
def registrar_turno(paciente: dict, date: datetime, sheet_type: str, sede: str=None):
    try:
        ws = get_daily_worksheet(date, sheet_type)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        estudios = paciente.get("estudios") or []
        estudios_str = ", ".join(estudios) if isinstance(estudios, list) else estudios
        row = [
            ts, paciente.get("nombre",""), paciente.get("dni",""),
            paciente.get("localidad",""), paciente.get("dirección",""),
            paciente.get("fecha_nacimiento",""), calcular_edad(paciente.get("fecha_nacimiento","")) or "",
            paciente.get("cobertura",""), paciente.get("afiliado",""),
            estudios_str, paciente.get("tipo_atencion","")
        ]
        if sheet_type == "Sedes":
            row.append(sede or "")
        ws.append_row(row)
        logger.info(f"Turno registrado para {paciente.get('nombre')} en {sheet_type} ({date.strftime('%Y-%m-%d')})")
    except Exception as e:
        logger.error(f"Error registrando turno en Google Sheets: {e}")

def registrar_resultado(paciente: dict):
    try:
        ws = get_resultados_sheet()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [ts, paciente.get("nombre",""), paciente.get("dni",""), paciente.get("localidad","")]
        ws.append_row(row)
        logger.info(f"Solicitud de resultado registrada para {paciente.get('nombre')}")
    except Exception as e:
        logger.error(f"Error registrando resultado en Google Sheets: {e}")

# --- Envío de WhatsApp (Cloud API) -------------------------------------------
def enviar_mensaje_whatsapp(to_number: str, body_text: str):
    url = f"https://graph.facebook.com/v16.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body_text}
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=5)
        resp.raise_for_status()
        logger.info(f"Mensaje enviado a {to_number}")
    except RequestException as e:
        logger.error(f"Error enviando mensaje a {to_number}: {e}")

# --- Derivación a operador externa -------------------------------------------
@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def derivar_a_operador(payload: dict):
    try:
        resp = requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
        resp.raise_for_status()
        logger.info("Caso derivado a operador")
    except RequestException as e:
        logger.error(f"Error derivando a operador: {e}")

# --- Procesamiento de imágenes -----------------------------------------------
def compress_image(img_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img = img.resize((1024,1024), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Error comprimiendo imagen: {e}")
        return img_bytes

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def call_ocr_service(image_b64: str) -> dict:
    resp = requests.post(OCR_SERVICE_URL, json={"image_base64": image_b64}, timeout=10)
    resp.raise_for_status()
    return resp.json()

# --- Lógica de OpenAI --------------------------------------------------------
def get_instrucciones_estudios(estudios_list: list) -> str:
    cache_key = f"instrucciones:{hash(','.join(sorted(estudios_list)))}"
    cached = redis_client.get(cache_key)
    if cached:
        return cached

    prompt = f"""
Estos son los estudios solicitados: {', '.join(estudios_list)}.
Eres un asistente de laboratorio especializado en indicar ayuno y recolección de orina. Tu tarea:

1. **Ayuno para estudios de sangre**
   - Por defecto “Ayuno de 8 horas”.
   - Si alguno forma parte de un perfil **lipídico**, **hepático** u **hormonal**, entonces “Ayuno de 12 horas”.
   - Excepción para “Pirens”: “Ayuno de 8 horas”.

2. **Recolección para estudios de orina**
   - Si hay análisis de **microalbuminuria** sin “espontánea” o cualquier “clearance” renal: “Recolectar orina de 24 horas”.
   - Si hay “primera orina de la mañana” o “sedimento urinario”: “Recolectar primera orina de la mañana”.

3. **Salida final:**  
   - Ayuno de sangre: “Ayuno de X horas” o “No requiere ayuno”.  
   - Recolección de orina: “Recolectar Y” o “No requiere recolección de orina”.
"""
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt}],
            temperature=0.0
        )
        instrucciones = resp.choices[0].message.content.strip()
        redis_client.set(cache_key, instrucciones, ex=86400)
        return instrucciones
    except openai.OpenAIError as e:
        logger.error(f"Error OpenAI: {e}")
        return "No pude obtener indicaciones específicas. Por favor, consulta al laboratorio."

# --- Lógica central de ALIA --------------------------------------------------
def handle_esperando_orden(from_number: str, content: str, paciente: dict) -> str:
    if content.strip().lower() in ("no","no tengo orden"):
        paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_MANUAL.value
        save_paciente(from_number, paciente)
        return "Ok, continuamos sin orden médica.\nPor favor, escribe los estudios solicitados:"
    return "Por favor envía la foto de tu orden médica o responde 'no' para continuar sin orden."

def handle_estudios_manual(from_number: str, content: str, paciente: dict) -> str:
    paciente["estudios"] = [e.strip() for e in content.split(",")]
    paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_CONFIRMACION.value
    save_paciente(from_number, paciente)
    return f"Hemos recibido estos estudios: {', '.join(paciente['estudios'])}.\n¿Los confirmas? (sí/no)"

def handle_estudios_confirmacion(from_number: str, content: str, paciente: dict) -> str:
    if content.strip().lower() in ("sí","si","s"):
        instrucciones = get_instrucciones_estudios(paciente["estudios"])
        localidad, tipo = paciente.get("localidad",""), paciente.get("tipo_atencion")
        if tipo == "SEDE":
            sede, dir_sede = determinar_sede(localidad)
            date, dia = determinar_dia_turno(localidad)
            registrar_turno(paciente, date, "Sedes", sede)
            final = (
                f"El pre-ingreso se realizó correctamente.\n"
                f"Te esperamos en la sede {sede} ({dir_sede}) el {dia} ({date.strftime('%d/%m/%Y')}) de 07:40 a 11:00.\n"
                "Las prácticas quedan sujetas a autorización del prestador."
            )
        else:
            date, dia = determinar_dia_turno(localidad)
            while count_domicilio_patients(date) >= 15:
                date, dia = get_next_business_day(date, localidad)
            registrar_turno(paciente, date, "Domicilios")
            final = (
                f"Tu turno se reservó para el día {dia} ({date.strftime('%d/%m/%Y')}), te visitaremos de 08:00 a 11:00.\n"
                "Las prácticas quedan sujetas a autorización del prestador."
            )
        clear_paciente(from_number)
        return f"{instrucciones}\n\n{final}"
    paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_MANUAL.value
    save_paciente(from_number, paciente)
    return "Entendido. Por favor, vuelve a escribir los estudios solicitados."

def handle_menu(from_number: str, content: str, paciente: dict) -> str:
    txt = content.strip().lower()
    if txt == "1" or "turno" in txt:
        paciente["estado"] = BotState.MENU_TURNO.value
        save_paciente(from_number, paciente)
        return "¿Dónde prefieres el turno?\n1. Sede\n2. Domicilio"
    if txt == "2" or "resultado" in txt:
        paciente["estado"] = BotState.ESPERANDO_RESULTADOS_NOMBRE.value
        save_paciente(from_number, paciente)
        return "Para enviarte resultados, indícanos tu nombre completo:"
    if "operador" in txt or "ayuda" in txt or "asistente" in txt:
        derivar_a_operador({"from_number": from_number, "paciente": paciente})
        clear_paciente(from_number)
        return "Te derivo a un operador. En breve te contactarán."
    return "Opción no válida. Elige 1, 2 o 3."

def handle_menu_turno(from_number: str, content: str, paciente: dict) -> str:
    txt = content.strip().lower()
    if txt == "1" or "sede" in txt:
        paciente["tipo_atencion"] = "SEDE"
    elif txt == "2" or "domicilio" in txt:
        paciente["tipo_atencion"] = "DOMICILIO"
    else:
        return "Por favor elige 1 o 2."
    pregunta = siguiente_campo_faltante(paciente)
    save_paciente(from_number, paciente)
    return pregunta

def handle_datos_secuenciales(from_number: str, content: str, paciente: dict) -> str:
    campo = paciente["estado"].split("_",1)[1]
    if campo == "fecha_nacimiento" and not validate_fecha_nacimiento(content):
        return "Formato de fecha inválido (dd/mm/aaaa). Intenta de nuevo:"
    if campo == "afiliado" and not validate_afiliado(content):
        return "Número de afiliado inválido. Usa solo letras y números:"
    paciente[campo] = content.title() if campo in ["nombre","localidad"] else content
    siguiente = siguiente_campo_faltante(paciente)
    save_paciente(from_number, paciente)
    if siguiente:
        return siguiente
    paciente["estado"] = BotState.ESPERANDO_ORDEN.value
    save_paciente(from_number, paciente)
    return "Envía foto de tu orden médica o responde 'no' para continuar sin orden."

def handle_resultados(from_number: str, content: str, paciente: dict) -> str:
    campo = paciente["estado"].split("_",1)[1]
    if campo == "nombre":
        paciente["nombre"] = content.title()
        paciente["estado"] = BotState.ESPERANDO_RESULTADOS_DNI.value
        save_paciente(from_number, paciente)
        return "Ahora indícanos tu número de documento:"
    if campo == "dni":
        paciente["dni"] = content.strip()
        paciente["estado"] = BotState.ESPERANDO_RESULTADOS_LOCALIDAD.value
        save_paciente(from_number, paciente)
        return "Finalmente, indícanos tu localidad:" 
    if campo == "localidad":
        paciente["localidad"] = content.title()
        registrar_resultado(paciente)
        clear_paciente(from_number)
        return f"Solicitamos envío de resultados para {paciente['nombre']} ({paciente['dni']}) en {paciente['localidad']}."

def handle_image(from_number: str, content: str, paciente: dict) -> str:
    try:
        compressed = compress_image(base64.b64decode(content))
        b64 = base64.b64encode(compressed).decode()
        ocr_data = call_ocr_service(b64)
        texto_ocr = ocr_data.get("text","").strip()
        if not texto_ocr:
            return "No pudimos procesar tu orden médica."
        prompt = (
            f"Analiza esta orden médica y devuelve un JSON con las claves:\n"
            f"estudios, cobertura, afiliado.\n\n{texto_ocr}"
        )
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt}],
            temperature=0.0
        )
        datos = json.loads(resp.choices[0].message.content.strip())
        paciente.update({
            "estudios": datos.get("estudios"),
            "cobertura": datos.get("cobertura"),
            "afiliado": datos.get("afiliado"),
            "imagen_base64": content
        })
        save_paciente(from_number, paciente)
        paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_CONFIRMACION.value
        return f"Hemos detectado estos estudios: {', '.join(paciente['estudios'])}.\n¿Los confirmas? (sí/no)"
    except Exception as e:
        logger.error(f"Error procesando imagen: {e}")
        return "Error interpretando tu orden médica."

def procesar_mensaje_alia(from_number: str, tipo: str, contenido: str) -> str:
    paciente = get_paciente(from_number)
    estado   = BotState(paciente.get("estado") or BotState.NONE.value)
    txt      = contenido.strip().lower()

    if tipo == "text":
        if "reiniciar" in txt:
            clear_paciente(from_number)
            return "Flujo reiniciado. ¿En qué puedo ayudarte hoy?"
        if estado == BotState.NONE and any(k in txt for k in ["hola","buenas"]):
            paciente["estado"] = BotState.MENU.value
            save_paciente(from_number, paciente)
            return (
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción:\n"
                "1. Pedir un turno\n"
                "2. Solicitar envío de resultados\n"
                "3. Contactar con un operador"
            )
        if estado == BotState.MENU:
            return handle_menu(from_number, contenido, paciente)
        if estado == BotState.MENU_TURNO:
            return handle_menu_turno(from_number, contenido, paciente)
        if estado == BotState.ESPERANDO_ORDEN:
            return handle_esperando_orden(from_number, contenido, paciente)
        if estado == BotState.ESPERANDO_ESTUDIOS_MANUAL:
            return handle_estudios_manual(from_number, contenido, paciente)
        if estado == BotState.ESPERANDO_ESTUDIOS_CONFIRMACION:
            return handle_estudios_confirmacion(from_number, contenido, paciente)
        if estado in [
            BotState.ESPERANDO_RESULTADOS_NOMBRE,
            BotState.ESPERANDO_RESULTADOS_DNI,
            BotState.ESPERANDO_RESULTADOS_LOCALIDAD
        ]:
            return handle_resultados(from_number, contenido, paciente)
        if estado in [
            BotState.ESPERANDO_NOMBRE, BotState.ESPERANDO_DIRECCION,
            BotState.ESPERANDO_LOCALIDAD, BotState.ESPERANDO_FECHA_NACIMIENTO,
            BotState.ESPERANDO_COBERTURA, BotState.ESPERANDO_AFILIADO
        ]:
            return handle_datos_secuenciales(from_number, contenido, paciente)

        try:
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":f"Pregunta: {contenido}"}],
                temperature=0.0
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error en fallback GPT: {e}")
            return "No entendí tu consulta, ¿podrías reformularla?"

    if tipo == "image" and estado == BotState.ESPERANDO_ORDEN:
        return handle_image(from_number, contenido, paciente)

    return "No pude procesar tu mensaje."

# --- Webhook WhatsApp (verificación y eventos) -------------------------------
@app.route("/webhook", methods=["GET","POST"])
def webhook_whatsapp():
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("Forbidden", status=403)

    data = request.get_json(force=True)
    if not data or data.get("object","").lower() != "whatsapp_business_account":
        return Response("No event", status=200)

    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
    except Exception:
        return Response("Invalid event", status=400)

    from_nr = msg.get("from")
    tipo    = msg.get("type")
    if not from_nr or not tipo:
        return Response("Missing fields", status=400)

    if tipo == "text":
        txt = msg.get("text",{}).get("body","")
        rply = procesar_mensaje_alia(from_nr, "text", txt)
        enviar_mensaje_whatsapp(from_nr, rply)
    elif tipo == "image":
        mid = msg.get("image",{}).get("id")
        if not mid:
            return Response("Missing image ID", status=400)
        try:
            meta = requests.get(
                f"https://graph.facebook.com/v16.0/{mid}",
                params={"access_token": META_ACCESS_TOKEN}, timeout=5
            ).json()
            url = meta.get("url")
            img = requests.get(url, timeout=10).content
            b64 = base64.b64encode(img).decode()
            rply = procesar_mensaje_alia(from_nr, "image", b64)
            enviar_mensaje_whatsapp(from_nr, rply)
        except Exception as e:
            logger.error(f"Error imagen WhatsApp: {e}")
            return Response("Error processing image", status=400)

    return Response("OK", status=200)

# --- Widget & página de ejemplo ----------------------------------------------
@app.route("/widget.js")
def serve_widget():
    return send_from_directory(app.static_folder, "widget.js")

@app.route("/", methods=["GET"])
def serve_index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/chat", methods=["GET"])
def serve_chat():
    return send_from_directory(app.static_folder, "chat.html")

@app.route("/chat", methods=["POST"])
def api_chat():
    data    = request.get_json(force=True)
    session = data.get("session","demo")
    if "image" in data and (data["image"].startswith("iVBOR") or data["image"].startswith("/9j/")):
        reply = procesar_mensaje_alia(session, "image", data["image"])
    else:
        msg   = data.get("message","").strip()
        reply = procesar_mensaje_alia(session, "text", msg)
    return jsonify({"reply": reply})

# --- Ejecución del servidor --------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT",10000))
    app.run(host="0.0.0.0", port=puerto)
