import os
import json
import base64
import logging
import requests
import redis
from datetime import datetime
from enum import Enum
from flask import Flask, request, Response, send_from_directory, jsonify
from openai import OpenAI
from requests.exceptions import RequestException
from tenacity import retry, stop_after_attempt, wait_fixed
from PIL import Image
import io
import re

# --- Configuración de entorno ------------------------------------------------
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")
OCR_SERVICE_URL = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service-onrender.com/derivar")

# --- Inicialización de logging ----------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Inicialización de clientes ----------------------------------------------
openai_client = OpenAI(api_key=OPENAI_API_KEY)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__, static_folder="static")

# --- Estados del bot --------------------------------------------------------
class BotState(Enum):
    NONE = None
    MENU = "menu"
    MENU_TURNO = "menu_turno"
    ESPERANDO_NOMBRE = "esperando_nombre"
    ESPERANDO_DIRECCION = "esperando_direccion"
    ESPERANDO_LOCALIDAD = "esperando_localidad"
    ESPERANDO_FECHA_NACIMIENTO = "esperando_fecha_nacimiento"
    ESPERANDO_COBERTURA = "esperando_cobertura"
    ESPERANDO_AFILIADO = "esperando_afiliado"
    ESPERANDO_ORDEN = "esperando_orden"
    ESPERANDO_ESTUDIOS_MANUAL = "esperando_estudios_manual"
    ESPERANDO_ESTUDIOS_CONFIRMACION = "esperando_estudios_confirmacion"
    ESPERANDO_RESULTADOS_NOMBRE = "esperando_resultados_nombre"
    ESPERANDO_RESULTADOS_DNI = "esperando_resultados_dni"
    ESPERANDO_RESULTADOS_LOCALIDAD = "esperando_resultados_localidad"

# --- Funciones de sesión ----------------------------------------------------
def get_paciente(tel: str) -> dict:
    """Obtiene los datos del paciente desde Redis o inicializa uno nuevo."""
    data = redis_client.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    paciente = {
        "estado": None,
        "tipo_atencion": None,
        "nombre": None,
        "direccion": None,
        "localidad": None,
        "fecha_nacimiento": None,
        "cobertura": None,
        "afiliado": None,
        "estudios": None,
        "imagen_base64": None,
        "dni": None
    }
    save_paciente(tel, paciente)
    return paciente

def save_paciente(tel: str, info: dict):
    """Guarda los datos del paciente en Redis con TTL de 24 horas."""
    redis_client.set(f"paciente:{tel}", json.dumps(info), ex=86400)

def clear_paciente(tel: str):
    """Elimina los datos del paciente de Redis."""
    redis_client.delete(f"paciente:{tel}")

# --- Utilidades generales ---------------------------------------------------
def calcular_edad(fecha_str: str) -> int:
    """Calcula la edad a partir de una fecha de nacimiento (dd/mm/aaaa)."""
    try:
        nac = datetime.strptime(fecha_str, "%d/%m/%Y")
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except ValueError:
        return None

def validate_fecha_nacimiento(fecha: str) -> bool:
    """Valida el formato de fecha de nacimiento (dd/mm/aaaa)."""
    if re.match(r"^\d{2}/\d{2}/\d{4}$", fecha):
        try:
            datetime.strptime(fecha, "%d/%m/%Y")
            return True
        except ValueError:
            return False
    return False

def validate_afiliado(afiliado: str) -> bool:
    """Valida que el número de afiliado sea alfanumérico."""
    return bool(re.match(r"^[a-zA-Z0-9]+$", afiliado))

def siguiente_campo_faltante(paciente: dict) -> str:
    """Determina el próximo campo faltante y actualiza el estado."""
    campos = [
        ("nombre", BotState.ESPERANDO_NOMBRE, "Por favor indícanos tu nombre completo:"),
        ("direccion", BotState.ESPERANDO_DIRECCION, "Ahora indícanos tu domicilio (calle y altura):"),
        ("localidad", BotState.ESPERANDO_LOCALIDAD, "¿En qué localidad vivís?"),
        ("fecha_nacimiento", BotState.ESPERANDO_FECHA_NACIMIENTO, "Por favor indícanos tu fecha de nacimiento (dd/mm/aaaa):"),
        ("cobertura", BotState.ESPERANDO_COBERTURA, "¿Cuál es tu cobertura médica?"),
        ("afiliado", BotState.ESPERANDO_AFILIADO, "¿Cuál es tu número de afiliado?")
    ]
    for campo, estado, pregunta in campos:
        if not paciente.get(campo):
            paciente["estado"] = estado.value
            return pregunta
    return None

def determinar_dia_turno(localidad: str) -> str:
    """Determina el día de turno según la localidad."""
    loc = (localidad or "").lower()
    wd = datetime.today().weekday()
    if "ituzaingo" in loc:
        return "Lunes"
    if "merlo" in loc or "padua" in loc:
        return "Martes" if wd < 4 else "Viernes"
    if "tesei" in loc or "hurlingham" in loc:
        return "Miércoles" if wd < 4 else "Sábado"
    if "castelar" in loc:
        return "Jueves"
    return "Lunes"

def determinar_sede(localidad: str) -> tuple:
    """Determina la sede y dirección según la localidad."""
    loc = (localidad or "").lower()
    if loc in ["castelar", "ituzaingo", "moron"]:
        return "CASTELAR", "Arias 2530"
    if loc in ["merlo", "padua", "paso del rey"]:
        return "MERLO", "Jujuy 847"
    if loc in ["tesei", "hurlingham"]:
        return "TESEI", "Concepción Arenal 2694"
    return "GENERAL", "Nuestra sede principal"

# --- Envío de WhatsApp (Cloud API) ------------------------------------------
def enviar_mensaje_whatsapp(to_number: str, body_text: str):
    """Envía un mensaje de WhatsApp al número especificado."""
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
        logger.error(f"Error enviando mensaje a {to_number}: {str(e)}")

# --- Derivación a operador externa ------------------------------------------
@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def derivar_a_operador(payload: dict):
    """Deriva el caso a un operador humano."""
    try:
        resp = requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
        resp.raise_for_status()
        logger.info("Caso derivado a operador")
    except RequestException as e:
        logger.error(f"Error derivando a operador: {str(e)}")

# --- Procesamiento de imágenes ----------------------------------------------
def compress_image(img_bytes: bytes) -> bytes:
    """Comprime una imagen para reducir el tamaño antes de enviarla al OCR."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img = img.resize((1024, 1024), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Error comprimiendo imagen: {str(e)}")
        return img_bytes

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def call_ocr_service(image_b64: str) -> dict:
    """Llama al servicio de OCR para extraer texto de una imagen."""
    resp = requests.post(OCR_SERVICE_URL, json={"image_base64": image_b64}, timeout=10)
    resp.raise_for_status()
    return resp.json()

# --- Lógica de OpenAI ------------------------------------------------------
def get_instrucciones_estudios(estudios_list: list) -> str:
    """Obtiene instrucciones de ayuno y recolección de orina, usando caché."""
    cache_key = f"instrucciones:{hash(','.join(sorted(estudios_list)))}"
    cached = redis_client.get(cache_key)
    if cached:
        return cached

    prompt = f"""
Estos son los estudios solicitados: {', '.join(estudios_list)}.
Eres un asistente de laboratorio especializado en indicar ayuno y recolección de orina. Tu tarea:

1. **Ayuno para estudios de sangre**  
   - Por defecto “Ayuno de 8 horas”.  
   - Si alguno forma parte de un perfil **lipídico** (colesterol total, HDL, LDL, triglicéridos…), **hepático** (AST, ALT, fosfatasa alcalina, bilirrubinas…) u **hormonal** (TSH, T4 libre, cortisol, estradiol…), entonces “Ayuno de 12 horas”.  
   - **Excepción**: para “Pirens” (y cualquier estudio similar en orina de 24 h con componente sanguíneo), se aplica **“Ayuno de 8 horas”**.

2. **Recolección para estudios de orina**  
   - Si hay análisis de **microalbuminuria** **sin** mención de “espontánea” ni “primera orina”, o cualquier “clearance” o “depuración” renal (p. ej. “clearance de creatinina”), o “Pirens”, o “recolección de orina de 24 horas”:  
     → **“Recolectar orina de 24 horas”**.  
   - Si hay “microalbuminuria en orina espontánea” o “primera orina de la mañana” o “orina matutina”, o “sedimento urinario” o las siglas **O-C**:  
     → **“Recolectar primera orina de la mañana”**.

3. **Salida final**  
   Al terminar el análisis, **entrega solo dos líneas**:  
   1. **Ayuno de sangre**:  
      - Si hay estudios de sangre, indica “Ayuno de X horas” (8 o 12).  
      - Si **no** hay estudios de sangre, indica “No requiere ayuno”.  
   2. **Recolección de orina**:  
      - Si hay estudios de orina, indica “Recolectar Y” donde Y es “primera orina de la mañana” y/o “orina de 24 horas” (si aplican ambas, sepáralas con “ y ”).  
      - Si **no** hay estudios de orina, indica “No requiere recolección de orina”.
"""
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        instrucciones = resp.choices[0].message.content.strip()
        redis_client.set(cache_key, instrucciones, ex=86400)
        return instrucciones
    except openai.OpenAIError as e:
        logger.error(f"Error OpenAI: {str(e)}")
        return "No pude obtener indicaciones específicas. Por favor, consulta al laboratorio."

# --- Lógica central de ALIA -------------------------------------------------
def handle_esperando_orden(from_number: str, content: str, paciente: dict) -> str:
    """Maneja el estado esperando_orden (texto o imagen)."""
    if content.strip().lower() in ("no", "no tengo orden"):
        paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_MANUAL.value
        save_paciente(from_number, paciente)
        return "Ok, continuamos sin orden médica.\nPor favor, escribe los estudios solicitados:"
    return "Por favor envía la foto de tu orden médica o responde 'no' para continuar sin orden."

def handle_estudios_manual(from_number: str, content: str, paciente: dict) -> str:
    """Maneja el ingreso manual de estudios."""
    estudios_raw = content.strip()
    paciente["estudios"] = [e.strip() for e in estudios_raw.split(",")]
    paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_CONFIRMACION.value
    save_paciente(from_number, paciente)
    estudios_str = ", ".join(paciente["estudios"])
    return f"Hemos recibido estos estudios: {estudios_str}.\n¿Los confirmas? (sí/no)"

def handle_estudios_confirmacion(from_number: str, content: str, paciente: dict) -> str:
    """Maneja la confirmación de estudios."""
    txt = content.strip().lower()
    if txt in ("sí", "si", "s"):
        estudios_list = paciente["estudios"]
        instrucciones = get_instrucciones_estudios(estudios_list)
        if paciente.get("tipo_atencion") == "SEDE":
            sede, dir_sede = determinar_sede(paciente["localidad"])
            final = (
                f"El pre-ingreso se realizó correctamente.\n"
                f"Te esperamos en la sede {sede} ({dir_sede}) de 07:40 a 11:00.\n"
                "Las prácticas quedan sujetas a autorización del prestador."
            )
        else:
            dia = determinar_dia_turno(paciente["localidad"])
            final = (
                f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00.\n"
                "Las prácticas quedan sujetas a autorización del prestador."
            )
        clear_paciente(from_number)
        return f"{instrucciones}\n\n{final}"
    paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_MANUAL.value
    save_paciente(from_number, paciente)
    return "Entendido. Por favor, vuelve a escribir los estudios solicitados:"

def handle_menu(from_number: str, content: str, paciente: dict) -> str:
    """Maneja el menú principal."""
    txt = content.strip().lower()
    if txt == "1" or "turno" in txt:
        paciente["estado"] = BotState.MENU_TURNO.value
        save_paciente(from_number, paciente)
        return "¿Dónde prefieres el turno?\n1. Sede\n2. Domicilio"
    if txt == "2" or "resultado" in txt:
        paciente["estado"] = BotState.ESPERANDO_RESULTADOS_NOMBRE.value
        save_paciente(from_number, paciente)
        return "Para enviarte resultados, indícanos tu nombre completo:"
    if txt == "3" or any(k in txt for k in ["operador", "ayuda", "asistente"]):
        derivar_a_operador({"from_number": from_number, "paciente": paciente})
        clear_paciente(from_number)
        return "Te derivo a un operador. En breve te contactarán."
    return "Opción no válida. Elige 1, 2 o 3."

def handle_menu_turno(from_number: str, content: str, paciente: dict) -> str:
    """Maneja la selección de tipo de turno."""
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
    """Maneja los datos secuenciales para turnos."""
    campo = paciente["estado"].split("_", 1)[1]
    if campo == "fecha_nacimiento" and not validate_fecha_nacimiento(content):
        return "Formato de fecha inválido (dd/mm/aaaa). Intenta de nuevo:"
    if campo == "afiliado" and not validate_afiliado(content):
        return "Número de afiliado inválido. Usa solo letras y números:"
    paciente[campo] = content.title() if campo in ["nombre", "localidad"] else content
    siguiente = siguiente_campo_faltante(paciente)
    save_paciente(from_number, paciente)
    if siguiente:
        return siguiente
    paciente["estado"] = BotState.ESPERANDO_ORDEN.value
    save_paciente(from_number, paciente)
    return "Envía foto de tu orden médica o responde 'no' para continuar sin orden."

def handle_resultados(from_number: str, content: str, paciente: dict) -> str:
    """Maneja el flujo de solicitud de resultados."""
    campo = paciente["estado"].split("_", 1)[1]
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
        msg = (
            f"Solicitamos envío de resultados para {paciente['nombre']} "
            f"({paciente['dni']}) en {paciente['localidad']}."
        )
        clear_paciente(from_number)
        return msg

def handle_image(from_number: str, content: str, paciente: dict) -> str:
    """Procesa una imagen de orden médica."""
    try:
        compressed_img = compress_image(base64.b64decode(content))
        compressed_b64 = base64.b64encode(compressed_img).decode()
        ocr_data = call_ocr_service(compressed_b64)
        texto_ocr = ocr_data.get("text", "").strip()
        if not texto_ocr:
            return "No pudimos procesar tu orden médica."

        prompt = f"Analiza esta orden médica y devuelve un JSON con las claves:\nestudios, cobertura, afiliado.\n\n{texto_ocr}"
        resp = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
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
        estudios_list = paciente["estudios"] or []
        estudios_str = ", ".join(estudios_list) if isinstance(estudios_list, list) else estudios_list
        paciente["estado"] = BotState.ESPERANDO_ESTUDIOS_CONFIRMACION.value
        save_paciente(from_number, paciente)
        return f"Hemos detectado estos estudios: {estudios_str}.\n¿Los confirmas? (sí/no)"
    except (RequestException, json.JSONDecodeError, openai.OpenAIError) as e:
        logger.error(f"Error procesando imagen de {from_number}: {str(e)}")
        return "Error interpretando tu orden médica."

def procesar_mensaje_alia(from_number: str, tipo: str, contenido: str) -> str:
    """Procesa un mensaje entrante y devuelve la respuesta del bot."""
    paciente = get_paciente(from_number)
    estado = BotState(paciente.get("estado") or BotState.NONE.value)
    txt = contenido.strip().lower()

    if tipo == "text":
        if "reiniciar" in txt:
            clear_paciente(from_number)
            return "Flujo reiniciado. ¿En qué puedo ayudarte hoy?"
        if estado == BotState.NONE and any(k in txt for k in ["hola", "buenas"]):
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
        if estado in [BotState.ESPERANDO_RESULTADOS_NOMBRE, BotState.ESPERANDO_RESULTADOS_DNI, BotState.ESPERANDO_RESULTADOS_LOCALIDAD]:
            return handle_resultados(from_number, contenido, paciente)
        if estado in [BotState.ESPERANDO_NOMBRE, BotState.ESPERANDO_DIRECCION, BotState.ESPERANDO_LOCALIDAD, 
                      BotState.ESPERANDO_FECHA_NACIMIENTO, BotState.ESPERANDO_COBERTURA, BotState.ESPERANDO_AFILIADO]:
            return handle_datos_secuenciales(from_number, contenido, paciente)
        
        # Fallback para preguntas libres
        prompt = (
            f"Paciente: {paciente.get('nombre', '')} "
            f"(Edad {calcular_edad(paciente.get('fecha_nacimiento', '')) or 'desconocida'})\n"
            f"Pregunta: {contenido}\nResponde sólo si debe realizar ayuno o recolectar orina."
        )
        try:
            resp = openai_client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            return resp.choices[0].message.content.strip()
        except openai.OpenAIError as e:
            logger.error(f"Error OpenAI en fallback: {str(e)}")
            return "No entendí tu consulta, ¿podrías reformularla?"

    if tipo == "image" and estado == BotState.ESPERANDO_ORDEN:
        return handle_image(from_number, contenido, paciente)

    return "No pude procesar tu mensaje."

# --- Webhook WhatsApp (verificación y eventos) ------------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook_whatsapp():
    """Maneja eventos y verificaciones del webhook de WhatsApp."""
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("Forbidden", status=403)

    data = request.get_json(force=True)
    if not data or data.get("object", "").lower() != "whatsapp_business_account":
        return Response("No event", status=200)

    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError):
        logger.warning("Evento WhatsApp inválido: %s", data)
        return Response("Invalid event", status=400)

    from_nr = msg.get("from")
    tipo = msg.get("type")
    if not from_nr or not tipo:
        return Response("Missing required fields", status=400)

    if tipo == "text":
        txt = msg.get("text", {}).get("body", "")
        rply = procesar_mensaje_alia(from_nr, "text", txt)
        enviar_mensaje_whatsapp(from_nr, rply)
    elif tipo == "image":
        mid = msg.get("image", {}).get("id")
        if not mid:
            return Response("Missing image ID", status=400)
        try:
            meta = requests.get(
                f"https://graph.facebook.com/v16.0/{mid}",
                params={"access_token": META_ACCESS_TOKEN}, timeout=5
            ).json()
            url = meta.get("url")
            if not url:
                return Response("Invalid image URL", status=400)
            img = requests.get(url, timeout=10).content
            b64 = base64.b64encode(img).decode()
            rply = procesar_mensaje_alia(from_nr, "image", b64)
            enviar_mensaje_whatsapp(from_nr, rply)
        except RequestException as e:
            logger.error(f"Error procesando imagen de WhatsApp: {str(e)}")
            return Response("Error processing image", status=400)

    return Response("OK", status=200)

# --- Widget & página de ejemplo ---------------------------------------------
@app.route("/widget.js")
def serve_widget():
    """Sirve el archivo widget.js."""
    return send_from_directory(app.static_folder, "widget.js")

@app.route("/", methods=["GET"])
def serve_index():
    """Sirve la página principal."""
    return send_from_directory(app.static_folder, "index.html")

@app.route("/chat", methods=["GET"])
def serve_chat():
    """Sirve la página de chat."""
    return send_from_directory(app.static_folder, "chat.html")

@app.route("/chat", methods=["POST"])
def api_chat():
    """Maneja solicitudes de chat desde el widget."""
    data = request.get_json(force=True)
    session = data.get("session", "demo")
    if "image" in data and (data["image"].startswith("iVBOR") or data["image"].startswith("/9j/")):
        reply = procesar_mensaje_alia(session, "image", data["image"])
    else:
        msg = data.get("message", "").strip()
        reply = procesar_mensaje_alia(session, "text", msg)
    return jsonify({"reply": reply})

# --- Ejecución del servidor ------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
