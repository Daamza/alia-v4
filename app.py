import os
import json
import base64
import io
import time
import requests
import redis
import unicodedata
from datetime import datetime
from flask import Flask, request, Response, send_from_directory, jsonify

import openai
from PIL import Image, ImageOps
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuración de entorno ------------------------------------------------
META_VERIFY_TOKEN     = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID  = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDS_BASE64")
OCR_SERVICE_URL       = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service-onrender.com/derivar")

# --- Inicialización de clientes ----------------------------------------------
openai.api_key = OPENAI_API_KEY
r = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__, static_folder='static')

# -------------------------------------------------------------------------------
# Utilidad: Normalizar texto eliminando tildes
# -------------------------------------------------------------------------------
def normalize(text: str) -> str:
    return unicodedata.normalize('NFKD', text or '') \
                      .encode('ASCII', 'ignore') \
                      .decode('ASCII') \
                      .lower()

# -------------------------------------------------------------------------------
# Preprocesamiento y llamada OCR
# -------------------------------------------------------------------------------
def preprocess_for_ocr(b64str: str) -> str:
    data = base64.b64decode(b64str)
    img = Image.open(io.BytesIO(data))
    img.thumbnail((1024, 1024))
    img = ImageOps.grayscale(img)
    img = img.point(lambda x: 0 if x < 128 else 255, '1')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

def call_ocr(b64: str) -> str:
    for i in range(3):
        try:
            resp = requests.post(
                OCR_SERVICE_URL,
                json={'image_base64': b64},
                timeout=15
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip()
        except Exception:
            time.sleep(2 ** i)
    return ""

# -------------------------------------------------------------------------------
# Funciones de sesión
# -------------------------------------------------------------------------------
def get_paciente(tel: str) -> dict:
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {
        'estado': None,
        'tipo_atencion': None,
        'nombre': None,
        'direccion': None,
        'localidad': None,
        'fecha_nacimiento': None,
        'cobertura': None,
        'afiliado': None,
        'estudios': None,
        'imagen_base64': None,
        'dni': None
    }
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel: str, info: dict):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel: str):
    r.delete(f"paciente:{tel}")

# -------------------------------------------------------------------------------
# Utilidades generales
# -------------------------------------------------------------------------------
def calcular_edad(fecha_str: str) -> int:
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

def siguiente_campo_faltante(paciente: dict) -> str:
    orden = [
        ('nombre',           "Por favor indícanos tu nombre completo:"),
        ('direccion',        "Ahora indícanos tu domicilio:"),
        ('localidad',        "¿En qué localidad vivís?"),
        ('fecha_nacimiento', "Por favor indícanos tu fecha de nacimiento (dd/mm/aaaa):"),
        ('cobertura',        "¿Cuál es tu cobertura médica?"),
        ('afiliado',         "¿Cuál es tu número de afiliado?")
    ]
    for campo, pregunta in orden:
        if not paciente.get(campo):
            paciente['estado'] = f'esperando_{campo}'
            return pregunta
    return None

def determinar_dia_turno(localidad: str) -> str:
    loc = normalize(localidad)
    wd  = datetime.today().weekday()
    if 'ituzaingo' in loc:       return 'Lunes'
    if 'merlo' in loc or 'padua' in loc:
        return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc:
        return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc:        return 'Jueves'
    return 'Lunes'

def determinar_sede(localidad: str) -> tuple:
    loc = normalize(localidad)
    if loc in ['castelar','ituzaingo','moron']:
        return 'CASTELAR', 'Arias 2530'
    if loc in ['merlo','padua','paso del rey']:
        return 'MERLO',   'Jujuy 847'
    if loc in ['tesei','hurlingham']:
        return 'TESEI',   'Concepción Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

# -------------------------------------------------------------------------------
# Envío WhatsApp (Cloud API)
# -------------------------------------------------------------------------------
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
        if not resp.ok:
            print("Error WhatsApp:", resp.status_code, resp.text)
    except Exception as e:
        print("Exception WhatsApp:", e)

# -------------------------------------------------------------------------------
# Procesamiento de imagen (OCR + GPT)
# -------------------------------------------------------------------------------
def handle_image(from_number: str, b64: str) -> str:
    paciente = get_paciente(from_number)

    # Preprocesa la imagen
    b64_pre = preprocess_for_ocr(b64)

    # Llama al OCR remoto con retry
    texto_ocr = call_ocr(b64_pre)
    if not texto_ocr:
        return "No pudimos procesar tu orden médica, ¿podrías enviarla con mejor iluminación?"

    # GPT extrae JSON
    prompt = (
        "Analiza esta orden médica y devuelve un JSON con las claves:\n"
        "estudios, cobertura, afiliado.\n\n" + texto_ocr
    )
    try:
        gpt = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt}],
            temperature=0.0
        )
        datos = json.loads(gpt.choices[0].message.content.strip())
    except Exception:
        return "Error interpretando tu orden médica."

    paciente.update({
        "estudios":      datos.get("estudios"),
        "cobertura":     datos.get("cobertura"),
        "afiliado":      datos.get("afiliado"),
        "imagen_base64": b64
    })
    save_paciente(from_number, paciente)

    siguiente = siguiente_campo_faltante(paciente)
    if siguiente:
        return f"Detectamos:\n{json.dumps(datos, ensure_ascii=False)}\n\n{siguiente}"

    # Cierra turno
    sede, dir_sede = determinar_sede(paciente["localidad"])
    if paciente["tipo_atencion"] == "SEDE":
        texto_fin = (
            f"El pre-ingreso se realizó correctamente. Te esperamos en la sede {sede} "
            f"({dir_sede}) de 07:40 a 11:00. Las prácticas quedan sujetas a autorización del prestador."
        )
    else:
        dia = determinar_dia_turno(paciente["localidad"])
        texto_fin = (
            f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00. "
            "Las prácticas quedan sujetas a autorización del prestador."
        )
    clear_paciente(from_number)
    return texto_fin

# -------------------------------------------------------------------------------
# Lógica central de ALIA
# -------------------------------------------------------------------------------
def procesar_mensaje_alia(from_number: str, tipo: str, contenido: str) -> str:
    paciente = get_paciente(from_number)

    if tipo == "text" and "turno" in contenido.lower():
        paciente["estado"] = "menu_turno"
        save_paciente(from_number, paciente)
        return "¿Dónde prefieres el turno? 1. Sede   2. Domicilio"

    if tipo == "image":
        return handle_image(from_number, contenido)

    if paciente.get("estado") == "esperando_orden" and tipo == "text":
        txt = contenido.strip().lower()
        if txt in ("no", "no tengo orden"):
            paciente["estado"] = "esperando_estudios_manual"
            save_paciente(from_number, paciente)
            return "Ok, continuamos sin orden médica. Por favor, escribe los estudios solicitados:"
        return "Por favor envía la foto de tu orden médica o responde 'no' para continuar sin orden."

    if paciente.get("estado") == "esperando_estudios_manual" and tipo == "text":
        estudios_raw = contenido.strip()
        prompt = (
            "Estos son los estudios solicitados de un paciente:\n"
            f"{estudios_raw}\n\n"
            "Devuélveme un JSON con clave estudios, donde el valor sea una lista de nombres."
        )
        try:
            gpt = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}],
                temperature=0.0
            )
            datos = json.loads(gpt.choices[0].message.content.strip())
            estudios = datos.get("estudios", [])
        except:
            estudios = [e.strip() for e in estudios_raw.split(",") if e.strip()]

        paciente["estudios"] = estudios
        paciente["estado"] = "confirmar_estudios"
        save_paciente(from_number, paciente)
        return (
            f"Estas son tus prácticas: {', '.join(estudios)}.\n"
            "Por favor confirma escribiendo 'sí' para continuar o 'no' para volver a ingresarlos."
        )

    if paciente.get("estado") == "confirmar_estudios" and tipo == "text":
        txt = contenido.strip().lower()
        if txt in ("sí","si"):
            estudios = paciente["estudios"]
            prompt = (
                "Para cada uno de los siguientes estudios de laboratorio:\n"
                f"{', '.join(estudios)}\n\n"
                "Indica de forma concisa si requiere AYUNO (y cuántas horas) o RECOLECCIÓN DE ORINA."
            )
            try:
                gpt = openai.ChatCompletion.create(
                    model="gpt-4",
                    messages=[{"role":"user","content":prompt}],
                    temperature=0.0
                )
                instrucciones = gpt.choices[0].message.content.strip()
            except:
                instrucciones = "No pude obtener instrucciones específicas."

            if paciente.get("tipo_atencion") == "SEDE":
                sede, dir_sede = determinar_sede(paciente["localidad"])
                texto_fin = (
                    f"El pre-ingreso se realizó correctamente. Te esperamos en la sede {sede} "
                    f"({dir_sede}) de 07:40 a 11:00. Las prácticas quedan sujetas a autorización del prestador."
                )
            else:
                dia = determinar_dia_turno(paciente["localidad"])
                texto_fin = (
                    f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00. "
                    "Las prácticas quedan sujetas a autorización del prestador."
                )
            clear_paciente(from_number)
            return f"{instrucciones}\n\n{texto_fin}"
        else:
            paciente["estado"] = "esperando_estudios_manual"
            paciente["estudios"] = None
            save_paciente(from_number, paciente)
            return "Entendido. Por favor, vuelve a escribir los estudios solicitados."

    if tipo == "text":
        texto = contenido.strip()
        lower = texto.lower()

        if "reiniciar" in lower:
            clear_paciente(from_number)
            return "Flujo reiniciado. ¿En qué puedo ayudarte hoy?"

        if paciente["estado"] is None and any(k in lower for k in ["hola","buenas"]):
            paciente["estado"] = "menu"
            save_paciente(from_number, paciente)
            return (
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción:\n"
                "1. Pedir un turno\n2. Solicitar envío de resultados\n3. Contactar con un operador"
            )

        if paciente.get("estado") == "menu":
            if texto == "1" or "turno" in lower:
                paciente["estado"] = "menu_turno"
                save_paciente(from_number, paciente)
                return "¿Dónde prefieres el turno? 1. Sede   2. Domicilio"
            elif texto == "2" or "resultado" in lower:
                paciente["estado"] = "esperando_resultados_nombre"
                save_paciente(from_number, paciente)
                return "Para enviarte resultados, indícanos tu nombre completo:"
            elif texto == "3" or any(k in lower for k in ["operador","ayuda","asistente"]):
                clear_paciente(from_number)
                return "Te derivo a un operador. En breve te contactarán."
            else:
                return "Opción no válida. Elige 1, 2 o 3."

        if paciente.get("estado") == "menu_turno":
            if texto == "1" or "sede" in lower:
                paciente["tipo_atencion"] = "SEDE"
            elif texto == "2" or "domicilio" in lower:
                paciente["tipo_atencion"] = "DOMICILIO"
            else:
                return "Por favor elige 1 o 2."
            pregunta = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            return pregunta

        if paciente.get("estado", "").startswith("esperando_resultados_"):
            campo = paciente["estado"].split("_",1)[1]
            if campo == "nombre":
                paciente["nombre"] = texto.title()
                paciente["estado"] = "esperando_resultados_dni"
                save_paciente(from_number, paciente)
                return "Ahora indícanos tu número de documento:"
            if campo == "dni":
                paciente["dni"] = texto
                paciente["estado"] = "esperando_resultados_localidad"
                save_paciente(from_number, paciente)
                return "Finalmente, tu localidad:"
            if campo == "localidad":
                paciente["localidad"] = texto.title()
                clear_paciente(from_number)
                return (
                    f"Solicitamos envío de resultados para {paciente['nombre']} "
                    f"({paciente['dni']}) en {paciente['localidad']}."
                )

        if paciente.get("estado", "").startswith("esperando_"):
            campo = paciente["estado"].split("_",1)[1]
            paciente[campo] = texto.title() if campo in ["nombre","localidad"] else texto
            siguiente = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            if siguiente:
                return siguiente
            paciente["estado"] = "esperando_orden"
            save_paciente(from_number, paciente)
            return "Envía foto de tu orden médica o responde 'no' para continuar sin orden."

        # fallback GPT para ayuno/orina
        edad = calcular_edad(paciente.get("fecha_nacimiento","")) or "desconocida"
        prompt = (
            f"Paciente: {paciente.get('nombre','')} (Edad {edad})\n"
            f"Consulta: {texto}\n\n"
            "Eres un asistente de laboratorio. "
            "Si la consulta corresponde a preparación para una prueba de laboratorio, "
            "responde especificando SI se debe realizar AYUNO (y cuántas horas) o RECOLECCIÓN DE ORINA. "
            "Si no aplica, di brevemente que no tienes información específica."
        )
        try:
            res = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}],
                temperature=0
            )
            return res.choices[0].message.content.strip()
        except:
            return "Lo siento, no entendí tu consulta. ¿Podrías reformularla?"

    return "No pude procesar tu mensaje."

# -------------------------------------------------------------------------------
# Webhook WhatsApp (verificación y eventos)
# -------------------------------------------------------------------------------
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
    if data.get("object","").lower() != "whatsapp_business_account":
        return Response("No event", status=200)

    entry   = data["entry"][0]
    msg     = entry["changes"][0]["value"]["messages"][0]
    from_nr = msg["from"]
    tipo    = msg["type"]

    if tipo == "text":
        txt  = msg["text"]["body"]
        rply = procesar_mensaje_alia(from_nr, "text", txt)
        enviar_mensaje_whatsapp(from_nr, rply)
    elif tipo == "image":
        mid  = msg["image"]["id"]
        meta = requests.get(
            f"https://graph.facebook.com/v16.0/{mid}",
            params={"access_token": META_ACCESS_TOKEN}, timeout=5
        ).json()
        url  = meta.get("url")
        img  = requests.get(url, timeout=10).content
        b64  = base64.b64encode(img).decode()
        rply = procesar_mensaje_alia(from_nr, "image", b64)
        enviar_mensaje_whatsapp(from_nr, rply)

    return Response("OK", status=200)

# -------------------------------------------------------------------------------
# Demo Web (“/” y “/chat”)
# -------------------------------------------------------------------------------
@app.route("/", methods=["GET"])
@app.route("/chat", methods=["GET"])
def serve_chat():
    return send_from_directory(app.static_folder, "chat.html")

@app.route("/chat", methods=["POST"])
def api_chat():
    data    = request.get_json(force=True)
    session = data.get("session", "demo")
    if data.get("image"):
        reply = handle_image(session, data["image"])
    else:
        msg   = data.get("message","").strip()
        reply = procesar_mensaje_alia(session, "text", msg)
    return jsonify({"reply": reply})

if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
