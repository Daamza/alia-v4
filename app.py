import os
import json
import base64
import redis
import requests
from flask import Flask, request, Response
from openai import OpenAI
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

# --- Configuración -------------------------------------------------------------
VERIFY_TOKEN          = os.getenv("WA_VERIFY_TOKEN")         # Token que definís en el dashboard de Meta
ACCESS_TOKEN          = os.getenv("WA_ACCESS_TOKEN")         # Tu Bearer Token de Cloud API
PHONE_NUMBER_ID       = os.getenv("WA_PHONE_NUMBER_ID")      # El ID de tu número de WhatsApp en Meta
OCR_SERVICE_URL       = "https://ocr-microsistema.onrender.com/ocr"
DERIVADOR_SERVICE_URL = "https://derivador-service-onrender.com/derivar"
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")

app    = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
r      = redis.from_url(REDIS_URL, decode_responses=True)

# --- Helper de sesiones -------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {
        'estado': None, 'ocr_fallos': 0, 'tipo_atencion': None,
        'nombre': None, 'direccion': None, 'localidad': None,
        'fecha_nacimiento': None, 'cobertura': None,
        'afiliado': None, 'estudios': None, 'imagen_base64': None
    }
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Cloud API: envío de mensaje ---------------------------------------------
def send_whatsapp(to, body_text):
    url = f"https://graph.facebook.com/v16.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body_text}
    }
    resp = requests.post(url, headers=headers, json=payload)
    if not resp.ok:
        print("Error enviando WhatsApp:", resp.status_code, resp.text)

# --- Verificación del webhook (GET) --------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# --- Auxiliares ALIA ----------------------------------------------------------
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, "%d/%m/%Y")
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
            paciente['estado'] = f"esperando_{campo}"
            return pregunta
    return None

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
    drive_svc = build("drive", "v3", credentials=creds_drive)
    # ... resto igual que antes ...
    # (creación de folder, hoja, encabezados, etc.)
    return client_gs.open(f"Turnos_{dia}").sheet1

def determinar_sede(localidad):
    loc = localidad.lower()
    if loc in ["castelar","ituzaingó","moron"]:
        return "CASTELAR", "Arias 2530"
    if loc in ["merlo","padua","paso del rey"]:
        return "MERLO", "Jujuy 847"
    if loc in ["tesei","hurlingham"]:
        return "TESEI", "Concepción Arenal 2694"
    return "GENERAL", "Nuestra sede principal"

def determinar_dia_turno(localidad):
    # Igual que antes…
    return "Lunes"

# --- Webhook WhatsApp (POST) --------------------------------------------------
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    data = request.get_json(force=True)
    # Meta manda un objeto “entry” con cambios
    if data.get("object") != "whatsapp_business_account":
        return "Ignored", 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            msgs  = value.get("messages")
            if not msgs:
                continue

            msg    = msgs[0]
            from_  = msg["from"]            # número del usuario, p.ej. "5491138261717"
            tipo   = msg.get("type")
            paciente = get_paciente(from_)

            # --- Si es texto
            if tipo == "text":
                texto = msg["text"]["body"].strip().lower()

                # Reiniciar flujo
                if "reiniciar" in texto:
                    clear_paciente(from_)
                    send_whatsapp(from_, "Flujo reiniciado. ¿En qué puedo ayudarte hoy?")
                    continue

                # Saludo o turno
                if paciente["estado"] is None and any(k in texto for k in ["hola","buenas","turno"]):
                    paciente["estado"] = "menu"
                    save_paciente(from_, paciente)
                    send_whatsapp(from_, 
                        "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige:\n"
                        "1. Pedir un turno\n"
                        "2. Solicitar resultado\n"
                        "3. Contactar operador"
                    )
                    continue

                # Menú principal
                if paciente["estado"] == "menu":
                    if texto == "1":
                        paciente["estado"] = "menu_turno"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "1. Sede\n2. Domicilio")
                        continue
                    if texto == "2":
                        paciente["estado"] = "esperando_resultados_nombre"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "Tu nombre completo para resultados:")
                        continue
                    if texto == "3":
                        # lógica de derivar...
                        clear_paciente(from_)
                        send_whatsapp(from_, "Te derivamos a un operador.")
                        continue
                    send_whatsapp(from_, "Elige 1, 2 o 3.")
                    continue

                # Sub-menú turno
                if paciente["estado"] == "menu_turno":
                    if texto == "1":
                        paciente["tipo_atencion"] = "SEDE"
                    elif texto == "2":
                        paciente["tipo_atencion"] = "DOMICILIO"
                    else:
                        send_whatsapp(from_, "Elige 1 o 2.")
                        continue
                    save_paciente(from_, paciente)
                    pregunta = siguiente_campo_faltante(paciente)
                    send_whatsapp(from_, pregunta)
                    continue

                # Flujo resultados
                if paciente["estado"] and paciente["estado"].startswith("esperando_resultados_"):
                    # parecido a Twilio: guardás nombre, dni, localidad
                    campo = paciente["estado"].split("_")[-1]
                    paciente[campo] = msg["text"]["body"].strip()
                    if campo == "nombre":
                        paciente["estado"] = "esperando_resultados_dni"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "Ahora tu DNI:")
                        continue
                    if campo == "dni":
                        paciente["estado"] = "esperando_resultados_localidad"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "Por último localidad:")
                        continue
                    if campo == "localidad":
                        # derivar...
                        clear_paciente(from_)
                        send_whatsapp(from_, 
                            f"Solicitamos envío de resultados para {paciente['nombre']} ({paciente['dni']}) en {paciente['localidad']}."
                        )
                        continue

                # Flujo secuencial turno (texto)
                if paciente["estado"] and paciente["estado"].startswith("esperando_") and "resultados" not in paciente["estado"]:
                    campo = paciente["estado"].split("_")[-1]
                    paciente[campo] = msg["text"]["body"].strip()
                    save_paciente(from_, paciente)
                    siguiente = siguiente_campo_faltante(paciente)
                    if siguiente:
                        send_whatsapp(from_, siguiente)
                    else:
                        paciente["estado"] = "esperando_orden"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "Envía la foto de tu orden o escribe 'No tengo orden'.")
                    continue

                # Fallback GPT
                edad = calcular_edad(paciente.get("fecha_nacimiento","")) or "desconocida"
                prompt = (
                    f"Paciente: {paciente.get('nombre','')} Edad: {edad}\n"
                    f"Pregunta: {msg['text']['body']}\n"
                    "Responde solo ayuno y recolección de orina."
                )
                try:
                    fb = client.chat.completions.create(
                        model="gpt-4",
                        messages=[{"role":"user","content":prompt}]
                    )
                    respuesta = fb.choices[0].message.content.strip()
                except:
                    respuesta = "Error procesando tu consulta."
                send_whatsapp(from_, respuesta)

            # --- Si es media (orden médica) ---
            elif tipo == "image":
                media_id = msg["image"]["id"]
                # Descarga la imagen
                media_url = requests.get(
                    f"https://graph.facebook.com/v16.0/{media_id}",
                    params={"access_token": ACCESS_TOKEN}
                ).json().get("url")
                resp = requests.get(media_url, params={"access_token": ACCESS_TOKEN}, timeout=5)
                b64  = base64.b64encode(resp.content).decode()

                # OCR + GPT JSON
                ocr = requests.post(OCR_SERVICE_URL, json={"image_base64": b64}, timeout=10)
                texto = ocr.json().get("text","").strip()
                prompt = "Analiza esta orden y devuelve JSON con estudios, cobertura, afiliado:\n\n" + texto
                pg     = client.chat.completions.create(model="gpt-4", messages=[{"role":"user","content":prompt}])
                try:
                    datos = json.loads(pg.choices[0].message.content.strip())
                except:
                    clear_paciente(from_)
                    send_whatsapp(from_, "Error interpretando tu orden. Envía de nuevo o escribe 'No tengo orden'.")
                    continue

                paciente.update({
                    "estudios": datos.get("estudios"),
                    "cobertura": datos.get("cobertura"),
                    "afiliado": datos.get("afiliado"),
                    "imagen_base64": b64
                })
                save_paciente(from_, paciente)

                pregunta = siguiente_campo_faltante(paciente)
                if pregunta:
                    send_whatsapp(from_, f"Detectamos:\n{json.dumps(datos, ensure_ascii=False)}\n\n{pregunta}")
                else:
                    # confirmación final
                    sede, dir_sede = determinar_sede(paciente["localidad"])
                    if paciente["tipo_atencion"] == "SEDE":
                        msgok = f"Pre-ingreso listo. Te esperamos en {sede} ({dir_sede}) 07:40–11:00."
                    else:
                        dia = determinar_dia_turno(paciente["localidad"])
                        msgok = f"Turno {dia} 08:00–11:00 a domicilio. Gracias."
                    clear_paciente(from_)
                    send_whatsapp(from_, msgok)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
