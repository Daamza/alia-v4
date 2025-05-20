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

# Instancias
app    = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
r      = redis.from_url(REDIS_URL, decode_responses=True)

# --- Funciones de sesión -------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {'estado': None, 'ocr_fallos': 0, 'tipo_atencion': None}
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Funciones auxiliares ------------------------------------------------------
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
    drive_svc   = build('drive', 'v3', credentials=creds_drive)

    # Carpeta ALIA_TURNOS
    folder_id = None
    try:
        res   = drive_svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false",
            spaces='drive',
            fields='files(id)'
        ).execute()
        items = res.get('files', [])
        if items:
            folder_id = items[0]['id']
        else:
            meta   = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
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

# --- Webhook de WhatsApp -------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    body     = request.form.get('Body', '').strip()
    msg      = body.lower()
    tel      = request.form.get('From', '')
    paciente = get_paciente(tel)

    # Manual: reiniciar el flujo
    if 'reiniciar' in msg:
        clear_paciente(tel)
        return responder_whatsapp("Flujo reiniciado. ¿En qué puedo ayudarte hoy?")

    # 1) Comando asistente
    if any(k in msg for k in ['asistente','ayuda','operador']):
        derivar_a_operador(tel)
        clear_paciente(tel)
        return responder_final("Te derivamos a un operador. En breve te contactarán.")

    # 2) Saludo
    if any(k in msg for k in ['hola','buenas']):
        return responder_whatsapp(
            "Hola! Soy ALIA, tu asistente IA de laboratorio, puedes.\n"
            "• Pedir un turno\n"
            "• Solicitar envío de resultados\n"
            "• Contactarte con un operador"
        )

    # 3) Solicitar resultados
    if 'resultados' in msg and paciente['estado'] is None:
        paciente['estado'] = 'esperando_resultados'
        save_paciente(tel, paciente)
        return responder_whatsapp("Para que recibir tus resultados envía: Nombre completo, Localidad.")

    if paciente['estado'] == 'esperando_resultados':
        parts = [p.strip() for p in body.split(',')]
        if len(parts) >= 2:
            paciente['nombre'], paciente['localidad'] = parts[:2]
            derivar_a_operador(tel)
            clear_paciente(tel)
            return responder_final(
                f"Solicitamos el envío de resultados para {paciente['nombre']} en {paciente['localidad']}."
            )
        return responder_whatsapp("Faltan datos. Envía: Nombre completo, Localidad.")

    # 4) Pedir turno
    if 'turno' in msg and paciente['estado'] is None:
        return responder_whatsapp("¿Prefieres un turno en una de nuestras sedes o atención a domicilio?")

    # 5) Pre-ingreso Sede/Domicilio
    if 'sede' in msg and paciente['estado'] is None:
        paciente['estado']        = 'esperando_datos'
        paciente['tipo_atencion'] = 'SEDE'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Para atenderte en una de nuestras sedes no es necesario tener turno previo, pero es recomendable realizar un pre-ingreso, por favor envía: Nombre, domicilio, Localidad, Fecha de nacimiento (dd/mm/aaaa), Cobertura, Nº Afiliado, separado por comas"
        )
    if any(k in msg for k in ['domicilio','casa']) and paciente['estado'] is None:
        paciente['estado']        = 'esperando_datos'
        paciente['tipo_atencion'] = 'DOMICILIO'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Para que te visitemos en tu domicilio por favor envía: Nombre, Dirección, Localidad, Fecha de nacimiento (dd/mm/aaaa), Cobertura, Nº Afiliado, separados por comas."
        )

    # 6) Procesar datos básicos
    if paciente['estado'] == 'esperando_datos':
        parts = [p.strip() for p in body.split(',')]
        if len(parts) < 6:
            return responder_whatsapp("Faltan campos. Envía 6 separados por comas.")
        paciente.update({
            'nombre':           parts[0].title(),
            'direccion':        parts[1],
            'localidad':        parts[2],
            'fecha_nacimiento': parts[3],
            'cobertura':        parts[4],
            'afiliado':         parts[5]
        })
        paciente['estado'] = 'esperando_orden'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Envía una foto o captura de pantalla de tu orden médica de análisis clínicos o responde 'No tengo orden'."
        )

    # 7) No tengo orden
    if paciente['estado'] == 'esperando_orden' and 'no tengo orden' in msg:
        clear_paciente(tel)
        return responder_final("Ok continuamos sin orden médica. de ser necesario te contactaremos para terminar el pre-ingreso.")

    # 8) Procesar orden médica
    if paciente['estado'] == 'esperando_orden' and request.form.get('NumMedia') == '1':
        url = request.form.get('MediaUrl0')
        try:
            resp = requests.get(
                url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=5
            )
            b64 = base64.b64encode(resp.content).decode()
            paciente['imagen_base64'] = b64
            ocr = requests.post(
                OCR_SERVICE_URL,
                json={'image_base64': b64},
                timeout=10
            )
            texto_ocr = ocr.json().get('text', '').strip() if ocr.ok else ''
            if not texto_ocr:
                raise Exception("OCR vacío")
        except:
            clear_paciente(tel)
            return responder_final("No pudimos procesar tu orden. Te derivamos a un operador.")

        prompt = f"Analiza orden médica:\n{texto_ocr}\nExtrae estudios, cobertura y afiliado."
        try:
            pg = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}]
            )
            estudios = pg.choices[0].message.content.strip()
        except:
            estudios = ""
        paciente['estado'] = 'confirmando_estudios'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            f"Detectamos estos estudios:\n{estudios}\n¿Son correctos? Responde 'Sí' o 'No'."
        )

    # 9) Confirmación de estudios
    if paciente.get('estado') == 'confirmando_estudios':
        if 'sí' in msg or 'si' in msg:
            sede, dir_sede = determinar_sede(paciente['localidad'])
            if paciente['tipo_atencion'] == 'SEDE':
                mensaje = (
                    f"El pre-ingreso se realizó correctamente. Te esperamos en la sede {sede} "
                    f"({dir_sede}) en el horario de 07:40hrs a 11:00hrs. Muchas gracias."
                )
            else:
                dia = determinar_dia_turno(paciente['localidad'])
                mensaje = (
                    f"Tu turno se reservó para el día {dia}, te estaremos visitando en "
                    f"el horario de 08:00hrs a 11:00hrs. Muchas gracias."
                )
            clear_paciente(tel)
            return responder_final(mensaje)
        if 'no' in msg:
            paciente['estado'] = 'esperando_orden'
            save_paciente(tel, paciente)
            return responder_whatsapp("Reenvía foto clara o responde 'No tengo orden'.")
        return responder_whatsapp("Responde 'Sí', 'No' o 'No tengo orden'.")

    # 10) Fallback GPT
    info  = paciente
    edad  = calcular_edad(info.get('fecha_nacimiento','')) or 'desconocida'
    texto = info.get('texto_ocr','')
    prompt_fb = (
        f"Paciente: {info.get('nombre','Paciente')}, Edad: {edad}\n"
        f"OCR: {texto}\nPregunta: {body}\n"
        "Responde únicamente ayuno y si recolectar orina."
    )
    try:
        fb = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt_fb}]
        )
        return responder_whatsapp(fb.choices[0].message.content.strip())
    except:
        return responder_whatsapp("Error procesando tu consulta. Intentá más tarde.")

# --- Entrypoint ---------------------------------------------------------------
if __name__ == '__main__':
    puerto = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=puerto)
