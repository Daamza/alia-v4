from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import redis
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import base64
import os
import requests
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

# --- Configuración -------------------------------------------------------------
OCR_SERVICE_URL       = "https://ocr-microsistema.onrender.com/ocr"
DERIVADOR_SERVICE_URL = "https://derivador-service.onrender.com/derivar"
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64")
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
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

# --- Auxiliares de respuesta --------------------------------------------------
def responder_whatsapp(texto):
    resp = MessagingResponse()
    resp.message(texto)
    return Response(str(resp), mimetype='application/xml')

def responder_final(texto):
    resp = MessagingResponse()
    resp.message(texto)
    encuesta = (
        "\n\n¡Gracias por comunicarte con ALIA! "
        "Ayudanos a mejorar completando esta encuesta: "
        "https://forms.gle/gHPbyMJfF18qYuUq9"
    )
    resp.message(encuesta)
    return Response(str(resp), mimetype='application/xml')

def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

# --- Funciones Google Sheets / Drive ------------------------------------------
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

# --- Lógica de flujo de preguntas ---------------------------------------------
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

# --- Webhook WhatsApp ---------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    body = request.form.get('Body', '').strip()
    msg  = body.lower()
    tel  = request.form.get('From', '')
    paciente = get_paciente(tel)

    # 1) OCR automático al recibir imagen --------------------------
    num_media = int(request.form.get('NumMedia', 0))
    if num_media > 0:
        url  = request.form.get('MediaUrl0')
        resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=5)
        b64  = base64.b64encode(resp.content).decode()

        ocr = requests.post(OCR_SERVICE_URL, json={'image_base64': b64}, timeout=10)
        texto_ocr = ocr.json().get('text', '').strip()
        if not texto_ocr:
            clear_paciente(tel)
            return responder_final("No pudimos procesar tu orden. Te derivamos a un operador.")

        # Pedimos JSON al GPT
        prompt = (
            "Analiza esta orden médica y devuelve un JSON con las claves:\n"
            "estudios, cobertura, afiliado.\n\n" + texto_ocr
        )
        pg = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt}]
        )
        contenido = pg.choices[0].message.content.strip()

        # Manejo de posible JSON mal-formado
        try:
            datos = json.loads(contenido)
        except json.JSONDecodeError:
            print("JSON parse error, contenido recibido:", contenido)
            clear_paciente(tel)
            return responder_whatsapp(
                "Lo siento, hubo un error interpretando tu orden. "
                "¿Podrías enviarla de nuevo o responder 'No tengo orden'?"
            )

        # Si todo ok, actualizo paciente y sigo flujo
        paciente.update({
            'estudios':      datos.get('estudios'),
            'cobertura':     datos.get('cobertura'),
            'afiliado':      datos.get('afiliado'),
            'imagen_base64': b64
        })
        save_paciente(tel, paciente)

        pregunta = siguiente_campo_faltante(paciente)
        if pregunta:
            return responder_whatsapp(
                f"Detectamos estos datos:\n{json.dumps(datos, ensure_ascii=False)}\n\n{pregunta}"
            )

        # Si no faltan campos, confirmo turno
        sede, dir_sede = determinar_sede(paciente['localidad'])
        if paciente['tipo_atencion'] == 'SEDE':
            mensaje = (
                f"El pre-ingreso se realizó correctamente. Te esperamos en la sede {sede} "
                f"({dir_sede}) de 07:40 a 11:00. ¡Muchas gracias!"
            )
        else:
            dia = determinar_dia_turno(paciente['localidad'])
            mensaje = (
                f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00. "
                "¡Muchas gracias!"
            )
        clear_paciente(tel)
        return responder_final(mensaje)

    # 2) Resto del flujo (reiniciar, menú, turno, resultados…) ------
    if 'reiniciar' in msg:
        clear_paciente(tel)
        return responder_whatsapp("Flujo reiniciado. ¿En qué puedo ayudarte hoy?")

    if any(k in msg for k in ['asistente','ayuda','operador']) and paciente['estado'] is None:
        derivar_a_operador(tel)
        clear_paciente(tel)
        return responder_final("Te derivamos a un operador. En breve te contactarán.")

    if any(k in msg for k in ['hola','buenas']) and paciente['estado'] is None:
        paciente['estado'] = 'menu'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción enviando el número:\n"
            "1. Pedir un turno\n"
            "2. Solicitar envío de resultados\n"
            "3. Contactar con un operador"
        )

    if paciente['estado'] == 'menu':
        if msg == '1':
            clear_paciente(tel)
            return responder_whatsapp("¿Turno en sede o a domicilio?")
        if msg == '2':
            paciente['estado'] = 'esperando_resultados_nombre'
            save_paciente(tel, paciente)
            return responder_whatsapp("Tu nombre completo para resultados:")
        if msg == '3':
            derivar_a_operador(tel)
            clear_paciente(tel)
            return responder_final("Te conectamos con un operador.")
        return responder_whatsapp("Elige 1, 2 o 3, por favor.")

    # … aquí el resto EXACTO de tu flujo de resultados y datos secuenciales …

    # 3) Fallback GPT para consultas generales
    info  = paciente
    edad  = calcular_edad(info.get('fecha_nacimiento','')) or 'desconocida'
    texto = info.get('texto_ocr','')
    prompt_fb = (
        f"Paciente: {info.get('nombre','Paciente')}, Edad: {edad}\n"
        f"OCR: {texto}\nPregunta: {body}\n"
        "Responde únicamente si debe realizar ayuno (horas) o recolectar orina."
    )
    try:
        fb = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt_fb}]
        )
        return responder_whatsapp(fb.choices[0].message.content.strip())
    except:
        return responder_whatsapp("Error procesando tu consulta. Intentá más tarde.")

if __name__ == '__main__':
    puerto = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=puerto)
