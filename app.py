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

# --- ConfiguraciÃ³n de entorno ------------------------------------------------
META_VERIFY_TOKEN     = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID  = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OCR_SERVICE_URL       = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service.onrender.com/derivar")

# --- InicializaciÃ³n de clientes ----------------------------------------------
openai.api_key = OPENAI_API_KEY
r = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__, static_folder='static')

# --- Funciones de sesiÃ³n -----------------------------------------------------
def get_paciente(tel):
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

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Utilidades generales ----------------------------------------------------
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

def siguiente_campo_faltante(paciente):
    orden = [
        ('nombre',           "Por favor indÃ­canos tu nombre completo:"),
        ('direccion',        "Ahora indÃ­canos tu domicilio (calle y altura):"),
        ('localidad',        "Â¿En quÃ© localidad vivÃ­s?"),
        ('fecha_nacimiento', "Por favor indÃ­canos tu fecha de nacimiento (dd/mm/aaaa):"),
        ('cobertura',        "Â¿CuÃ¡l es tu cobertura mÃ©dica?"),
        ('afiliado',         "Â¿CuÃ¡l es tu nÃºmero de afiliado?"),
        ('estudios',         "Por favor confÃ­rmanos los estudios solicitados:")
    ]
    for campo, pregunta in orden:
        if not paciente.get(campo):
            paciente['estado'] = f'esperando_{campo}'
            return pregunta
    return None

def determinar_dia_turno(localidad):
    loc = (localidad or "").lower()
    wd  = datetime.today().weekday()
    if 'ituzaingÃ³' in loc: return 'Lunes'
    if 'merlo' in loc or 'padua' in loc: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc: return 'MiÃ©rcoles' if wd < 4 else 'SÃ¡bado'
    if 'castelar' in loc: return 'Jueves'
    return 'Lunes'

def determinar_sede(localidad):
    loc = (localidad or "").lower()
    if loc in ['castelar','ituzaingÃ³','moron']:
        return 'CASTELAR', 'Arias 2530'
    if loc in ['merlo','padua','paso del rey']:
        return 'MERLO', 'Jujuy 847'
    if loc in ['tesei','hurlingham']:
        return 'TESEI', 'ConcepciÃ³n Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

# --- EnvÃ­o de WhatsApp (Cloud API) -------------------------------------------
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
            print("Error WhatsApp:", resp.status_code, resp.text)
    except Exception as e:
        print("Exception WhatsApp:", e)

# --- DerivaciÃ³n a operador externa --------------------------------------------
def derivar_a_operador(payload):
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error derivando a operador:", e)

# --- Webhook WhatsApp --------------------------------------------------------
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
        meta = requests.get(f"https://graph.facebook.com/v16.0/{mid}",
                            params={"access_token": META_ACCESS_TOKEN}, timeout=5).json()
        url  = meta.get("url")
        img  = requests.get(url, timeout=10).content
        b64  = base64.b64encode(img).decode()
        rply = procesar_mensaje_alia(from_nr, "image", b64)
        enviar_mensaje_whatsapp(from_nr, rply)

    return Response("OK", status=200)

# --- Widget & PÃ¡gina ---------------------------------------------------------
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
    session = data.get("session", "demo")
    if "image" in data and (data["image"].startswith("iVBOR") or data["image"].startswith("/9j/")):
        reply = procesar_mensaje_alia(session, "image", data["image"])
    else:
        msg   = data.get("message","").strip()
        reply = procesar_mensaje_alia(session, "text", msg)
    return jsonify({"reply": reply})

# --- LÃ³gica principal de ALIA (corregido) ------------------------------------
def procesar_mensaje_alia(from_number: str, tipo: str, contenido: str) -> str:
    paciente = get_paciente(from_number)

    if paciente.get("estado") == "esperando_orden":
        if tipo == "image":
            return procesar_mensaje_alia(from_number, "image", contenido)
        txt = contenido.strip().lower()
        if txt in ("no", "no tengo orden"):
            paciente["estado"] = "esperando_estudios_manual"
            save_paciente(from_number, paciente)
            return "Ok, continuamos sin orden mÃ©dica. Por favor, escribÃ­ los estudios solicitados:"
        return "Por favor envÃ­a la foto de tu orden mÃ©dica o responde 'no' para continuar sin orden."

    # AquÃ­ continuarÃ­a todo tu flujo original (sin tocar)

    return "No pude procesar tu mensaje."

# --- Run ----------------------------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
