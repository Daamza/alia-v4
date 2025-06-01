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
VERIFY_TOKEN          = os.getenv("WA_VERIFY_TOKEN")         # e.g. "aliaVerify2025"
ACCESS_TOKEN          = os.getenv("WA_ACCESS_TOKEN")         # e.g. "EAAG..."
PHONE_NUMBER_ID       = os.getenv("WA_PHONE_NUMBER_ID")      # e.g. "656903770841867"
OCR_SERVICE_URL       = "https://ocr-microsistema.onrender.com/ocr"
DERIVADOR_SERVICE_URL = "https://derivador-service-onrender.com/derivar"
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")

app    = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
r      = redis.from_url(REDIS_URL, decode_responses=True)

# --- Funciones de sesión -------------------------------------------------------
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
        'imagen_base64': None
    }
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Verificación del webhook (GET) --------------------------------------------
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(challenge, status=200)
    return Response("Forbidden", status=403)

# --- Envío de mensajes a través de la Cloud API --------------------------------
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
    resp = requests.post(url, headers=headers, json=payload, timeout=5)
    if not resp.ok:
        print("Error enviando WhatsApp:", resp.status_code, resp.text)

# --- Auxiliares ---------------------------------------------------------------
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
    if 'ituzaingó' in loc:
        return 'Lunes'
    if 'merlo' in loc or 'padua' in loc:
        return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc:
        return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc:
        return 'Jueves'
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

def derivar_a_operador(tel):
    info = get_paciente(tel)
    payload = {
        'nombre':            info.get('nombre','No disponible'),
        'direccion':         info.get('direccion','No disponible'),
        'localidad':         info.get('localidad','No disponible'),
        'fecha_nacimiento':  info.get('fecha_nacimiento','No disponible'),
        'cobertura':         info.get('cobertura','No disponible'),
        'afiliado':          info.get('afiliado','No disponible'),
        'telefono_paciente': tel,
        'tipo_atencion':     info.get('tipo_atencion','No disponible'),
        'imagen_base64':     info.get('imagen_base64','')
    }
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error derivar:", e)

# --- Webhook WhatsApp (POST) --------------------------------------------------
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    data = request.get_json(force=True)
    if data.get("object") != "whatsapp_business_account":
        return "Ignored", 200

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            msgs  = value.get("messages")
            if not msgs:
                continue

            msg     = msgs[0]
            from_   = msg["from"]
            tipo    = msg.get("type")
            paciente = get_paciente(from_)

            # Manejo de mensajes de texto
            if tipo == "text":
                texto = msg["text"]["body"].strip().lower()

                if "reiniciar" in texto:
                    clear_paciente(from_)
                    send_whatsapp(from_, "Flujo reiniciado. ¿En qué puedo ayudarte hoy?")
                    continue

                if paciente["estado"] is None and any(k in texto for k in ["hola","buenas","turno"]):
                    paciente["estado"] = "menu"
                    save_paciente(from_, paciente)
                    send_whatsapp(from_, 
                        "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción enviando el número:\n"
                        "1. Pedir un turno\n"
                        "2. Solicitar resultado\n"
                        "3. Contactar operador"
                    )
                    continue

                if paciente["estado"] == "menu":
                    if texto == "1":
                        paciente["estado"] = "menu_turno"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "¿Dónde prefieres el turno? Elige:\n1. Sede\n2. Domicilio")
                        continue
                    elif texto == "2":
                        paciente["estado"] = "esperando_resultados_nombre"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "Para enviarte tus resultados, por favor indícanos tu nombre completo:")
                        continue
                    elif texto == "3":
                        derivar_a_operador(from_)
                        clear_paciente(from_)
                        send_whatsapp(from_, "Te derivamos a un operador. En breve te contactarán.")
                        continue
                    else:
                        send_whatsapp(from_, "Opción no válida. Por favor elige 1, 2 o 3.")
                        continue

                if paciente["estado"] == "menu_turno":
                    if texto == "1":
                        paciente["tipo_atencion"] = "SEDE"
                    elif texto == "2":
                        paciente["tipo_atencion"] = "DOMICILIO"
                    else:
                        send_whatsapp(from_, "Elige 1 (Sede) o 2 (Domicilio), por favor.")
                        continue
                    save_paciente(from_, paciente)
                    pregunta = siguiente_campo_faltante(paciente)
                    send_whatsapp(from_, pregunta)
                    continue

                if paciente["estado"] == "esperando_resultados_nombre":
                    paciente["nombre"] = msg["text"]["body"].strip()
                    paciente["estado"] = "esperando_resultados_dni"
                    save_paciente(from_, paciente)
                    send_whatsapp(from_, "Gracias. Ahora indícanos tu número de documento:")
                    continue

                if paciente["estado"] == "esperando_resultados_dni":
                    paciente["dni"] = msg["text"]["body"].strip()
                    paciente["estado"] = "esperando_resultados_localidad"
                    save_paciente(from_, paciente)
                    send_whatsapp(from_, "Por último, indícanos tu localidad:")
                    continue

                if paciente["estado"] == "esperando_resultados_localidad":
                    paciente["localidad"] = msg["text"]["body"].strip()
                    derivar_a_operador(from_)
                    clear_paciente(from_)
                    send_whatsapp(from_, 
                        f"Solicitamos el envío de resultados para {paciente['nombre']} ({paciente.get('dni','')}) en {paciente['localidad']}."
                    )
                    continue

                if paciente["estado"] and paciente["estado"].startswith("esperando_") and "resultados" not in paciente["estado"]:
                    campo = paciente["estado"].split("_", 1)[1]
                    paciente[campo] = msg["text"]["body"].strip()
                    save_paciente(from_, paciente)
                    pregunta = siguiente_campo_faltante(paciente)
                    if pregunta:
                        send_whatsapp(from_, pregunta)
                    else:
                        paciente["estado"] = "esperando_orden"
                        save_paciente(from_, paciente)
                        send_whatsapp(from_, "Envía la foto de tu orden médica o escribe 'No tengo orden'.")
                    continue

                # Fallback GPT
                edad = calcular_edad(paciente.get("fecha_nacimiento","")) or "desconocida"
                prompt_fb = (
                    f"Paciente: {paciente.get('nombre','Paciente')}, Edad: {edad}\n"
                    f"Pregunta: {msg['text']['body']}\n"
                    "Responde únicamente si debe realizar ayuno (horas) o recolectar orina."
                )
                try:
                    fb = client.chat.completions.create(
                        model="gpt-4",
                        messages=[{"role":"user","content":prompt_fb}]
                    )
                    respuesta = fb.choices[0].message.content.strip()
                except:
                    respuesta = "Error procesando tu consulta. Intentá más tarde."
                send_whatsapp(from_, respuesta)

            # Manejo de mensajes de imagen (orden médica)
            elif tipo == "image":
                media_id = msg["image"]["id"]
                media_data = requests.get(
                    f"https://graph.facebook.com/v16.0/{media_id}",
                    params={"access_token": ACCESS_TOKEN},
                    timeout=5
                ).json()
                media_url = media_data.get("url")
                resp = requests.get(media_url, params={"access_token": ACCESS_TOKEN}, timeout=5)
                b64  = base64.b64encode(resp.content).decode()

                ocr = requests.post(OCR_SERVICE_URL, json={"image_base64": b64}, timeout=10)
                texto_ocr = ocr.json().get("text", "").strip()
                prompt = "Analiza esta orden y devuelve un JSON con las claves: estudios, cobertura, afiliado.\n\n" + texto_ocr
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
                    sede, dir_sede = determinar_sede(paciente["localidad"])
                    if paciente["tipo_atencion"] == "SEDE":
                        msgok = f"Pre-ingreso listo. Te esperamos en {sede} ({dir_sede}) de 07:40 a 11:00. ¡Muchas gracias!"
                    else:
                        dia = determinar_dia_turno(paciente["localidad"])
                        msgok = f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00. ¡Muchas gracias!"
                    clear_paciente(from_)
                    send_whatsapp(from_, msgok)

    return "OK", 200

# --- Entrypoint ---------------------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
