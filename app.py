import os
import json
import base64
import requests
import redis
from datetime import datetime
from flask import Flask, request, Response, send_from_directory, jsonify

import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuración de entorno ------------------------------------------------
META_VERIFY_TOKEN       = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN       = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID    = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY")
REDIS_URL               = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64        = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OCR_SERVICE_URL         = os.getenv(
    "OCR_SERVICE_URL",
    "https://ocr-microsistema.onrender.com/ocr"
)
DERIVADOR_SERVICE_URL   = os.getenv(
    "DERIVADOR_SERVICE_URL",
    "https://derivador-service.onrender.com/derivar"
)

# --- Inicialización de clientes ----------------------------------------------
openai.api_key = OPENAI_API_KEY
r = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__, static_folder='static')

# -------------------------------------------------------------------------------
# Funciones de sesión
# -------------------------------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {
        'estado': None,
        'ocr_fallos': 0,
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

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# -------------------------------------------------------------------------------
# Utilidades
# -------------------------------------------------------------------------------
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

def siguiente_campo_faltante(paciente):
    orden = [
        ('nombre',           "Por favor indícanos tu nombre completo:"),
        ('direccion',        "Ahora indícanos tu domicilio:"),
        ('localidad',        "¿En qué localidad vivís?"),
        ('fecha_nacimiento', "Por favor indícanos tu fecha de nacimiento (dd/mm/aaaa):"),
        ('cobertura',        "¿Cuál es tu cobertura médica?"),
        ('afiliado',         "¿Cuál es tu número de afiliado?"),
        ('estudios',         "Por favor confírmanos los estudios solicitados:")
    ]
    for campo, pregunta in orden:
        if not paciente.get(campo):
            paciente['estado'] = f'esperando_{campo}'
            return pregunta
    return None

# -------------------------------------------------------------------------------
# Google Sheets / Drive
# -------------------------------------------------------------------------------
def crear_hoja_del_dia(dia):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_json  = base64.b64decode(GOOGLE_CREDS_B64).decode()
    creds       = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    client_gs   = gspread.authorize(creds)
    creds_drive = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_svc = build('drive', 'v3', credentials=creds_drive)

    folder_id = None
    try:
        res = drive_svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false",
            spaces='drive',
            fields='files(id)'
        ).execute()
        items = res.get('files', [])
        if items:
            folder_id = items[0]['id']
        else:
            meta = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
            folder = drive_svc.files().create(body=meta, fields='id').execute()
            folder_id = folder['id']
    except HttpError as e:
        print("Error Drive:", e)

    nombre = f"Turnos_{dia}"
    try:
        hoja = client_gs.open(nombre).sheet1
    except:
        hoja = client_gs.create(nombre).sheet1
        if folder_id:
            try:
                drive_svc.files().update(
                    fileId=hoja.spreadsheet.id,
                    addParents=folder_id,
                    removeParents='root',
                    fields='id,parents'
                ).execute()
            except HttpError as e:
                print("Error mover hoja:", e)
        hoja.append_row([
            "Fecha", "Nombre", "Teléfono", "Dirección", "Localidad",
            "Fecha de Nacimiento", "Cobertura", "Afiliado", "Estudios", "Indicaciones"
        ])
    return hoja

def determinar_dia_turno(localidad):
    loc = localidad.lower()
    wd  = datetime.today().weekday()
    if 'ituzaingó' in loc: return 'Lunes'
    if 'merlo' in loc or 'padua' in loc: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc: return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc: return 'Jueves'
    return 'Lunes'

def determinar_sede(localidad):
    loc = localidad.lower()
    if loc in ['castelar','ituzaingó','moron']:
        return 'CASTELAR', 'Arias 2530'
    if loc in ['merlo','padua','paso del rey']:
        return 'MERLO', 'Jujuy 847'
    if loc in ['tesei','hurlingham']:
        return 'TESEI', 'Concepción Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

# -------------------------------------------------------------------------------
# Derivación a operador
# -------------------------------------------------------------------------------
def derivar_a_operador(payload):
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error derivando a operador:", e)

# -------------------------------------------------------------------------------
# Envío de WhatsApp Cloud API
# -------------------------------------------------------------------------------
def enviar_mensaje_whatsapp(to_number, body_text):
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
            print("Error enviando WhatsApp:", resp.status_code, resp.text)
    except Exception as e:
        print("Exception enviando WhatsApp:", e)

# -------------------------------------------------------------------------------
# Función centralizada de procesamiento de ALIA
# -------------------------------------------------------------------------------
def procesar_mensaje_alia(from_number: str, tipo: str, contenido: str) -> str:
    paciente = get_paciente(from_number)

    # — Texto —
    if tipo == "text":
        texto = contenido.strip()
        lower = texto.lower()

        if "reiniciar" in lower:
            clear_paciente(from_number)
            return "Flujo reiniciado. ¿En qué puedo ayudarte hoy?"

        if paciente["estado"] is None and any(k in lower for k in ["asistente","ayuda","operador"]):
            clear_paciente(from_number)
            return "Te derivo a un operador. En breve te contactarán."

        if paciente["estado"] is None and any(k in lower for k in ["hola","buenas"]):
            paciente["estado"] = "menu"
            save_paciente(from_number, paciente)
            return (
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción:\n"
                "1. Pedir un turno\n2. Solicitar envío de resultados\n3. Contactar con un operador"
            )

        if paciente["estado"] == "menu":
            # opción 1 = “1” o “turno”
            if texto == "1" or "turno" in lower:
                paciente["estado"] = "menu_turno"
                save_paciente(from_number, paciente)
                return "¿Dónde prefieres el turno? 1. Sede  2. Domicilio"
            # opción 2 = “2” o “resultados”
            elif texto == "2" or "resultado" in lower:
                paciente["estado"] = "esperando_resultados_nombre"
                save_paciente(from_number, paciente)
                return "Para enviarte resultados, indícanos tu nombre completo:"
            # opción 3 = “3” o “operador”/“ayuda”/“asistente”
            elif texto == "3" or any(k in lower for k in ["operador","ayuda","asistente"]):
                clear_paciente(from_number)
                return "Te derivo a un operador. En breve te contactarán."
            else:
                return "Opción no válida. Elige 1, 2 o 3 o escribe “turno”, “resultados” o “operador”."

        if paciente["estado"] == "menu_turno":
            # aceptamos 1 o la palabra "sede"
            if texto == "1" or "sede" in lower:
                paciente["tipo_atencion"] = "SEDE"
            # aceptamos 2 o la palabra "domicilio"
            elif texto == "2" or "domicilio" in lower:
                paciente["tipo_atencion"] = "DOMICILIO"
            else:
                return "Por favor elige 1, 2, o escribe “sede” o “domicilio”."
            pregunta = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            return pregunta

        if paciente["estado"] and paciente["estado"].startswith("esperando_") \
           and "resultados" not in paciente["estado"]:
            campo = paciente["estado"].split("_",1)[1]
            paciente[campo] = texto.title() if campo in ["nombre","localidad"] else texto
            siguiente = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            if siguiente:
                return siguiente
            paciente["estado"] = "esperando_orden"
            save_paciente(from_number, paciente)
            return "Envía foto de tu orden médica o responde 'No tengo orden'."

        if paciente["estado"] and paciente["estado"].startswith("esperando_resultados_"):
            campo = paciente["estado"].split("_",1)[1]
            if campo == "nombre":
                paciente["nombre"] = texto.title()
                paciente["estado"] = "esperando_resultados_dni"
                save_paciente(from_number, paciente)
                return "Ahora tu número de documento:"
            if campo == "dni":
                paciente["dni"] = texto
                paciente["estado"] = "esperando_resultados_localidad"
                save_paciente(from_number, paciente)
                return "Finalmente, tu localidad:"
            if campo == "localidad":
                paciente["localidad"] = texto.title()
                clear_paciente(from_number)
                return f"Solicitamos envío de resultados para {paciente['nombre']} ({paciente['dni']}) en {paciente['localidad']}."

        if paciente["estado"] == "esperando_orden" and lower == "no tengo orden":
            clear_paciente(from_number)
            return "Continuamos sin orden médica. Te contactaremos si hace falta."

        # Fallback GPT
        edad = calcular_edad(paciente.get("fecha_nacimiento","")) or "desconocida"
        prompt = (
            f"Paciente: {paciente.get('nombre','')} (Edad {edad})\n"
            f"Pregunta: {texto}\nResponde solo si debe ayunar o recolectar orina."
        )
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}]
            )
            return resp.choices[0].message.content.strip()
        except:
            return "Error procesando la consulta. Intentá más tarde."

    # — Imagen —
    if tipo == "image":
        return "Orden recibida. (Demo OCR pendiente)"

    return "No pude procesar tu mensaje."

# -------------------------------------------------------------------------------
# Webhook WhatsApp: GET verifica, POST procesa y envía
# -------------------------------------------------------------------------------
@app.route("/webhook", methods=["GET", "POST"])
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
        user_text = msg["text"]["body"]
        reply = procesar_mensaje_alia(from_nr, "text", user_text)
        enviar_mensaje_whatsapp(from_nr, reply)
        return Response("OK", status=200)

    if tipo == "image":
        media_id = msg["image"]["id"]
        meta     = requests.get(
            f"https://graph.facebook.com/v16.0/{media_id}",
            params={"access_token": META_ACCESS_TOKEN},
            timeout=5
        ).json()
        media_url = meta.get("url")
        img       = requests.get(media_url, timeout=10).content
        b64       = base64.b64encode(img).decode()
        reply     = procesar_mensaje_alia(from_nr, "image", b64)
        enviar_mensaje_whatsapp(from_nr, reply)
        return Response("OK", status=200)

    return Response("OK", status=200)

# -------------------------------------------------------------------------------
# Interfaz demo: servir chat.html y API de chat
# -------------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def serve_root():
    return send_from_directory(app.static_folder, "chat.html")

@app.route("/chat", methods=["GET"])
def serve_chat():
    return send_from_directory(app.static_folder, "chat.html")

@app.route("/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True)
    if "image" in data and data["image"].startswith(("iVBOR","/9j/")):  # base64 heurístico
        b64 = data["image"]
        reply = procesar_mensaje_alia("demo", "image", b64)
    else:
        user_msg = data.get("message","").strip()
        reply = procesar_mensaje_alia("demo", "text", user_msg)
    return jsonify({"reply": reply})
    
# -------------------------------------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
