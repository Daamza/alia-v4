from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import base64
import os
import json
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

# Instanciamos Flask y OpenAI
app       = Flask(__name__)
client    = OpenAI(api_key=OPENAI_API_KEY)
pacientes = {}

# --- Funciones auxiliares ------------------------------------------------------
def responder_whatsapp(texto):
    """
    Envía solo el mensaje de texto (sin encuesta).
    """
    resp = MessagingResponse()
    resp.message(texto)
    return Response(str(resp), mimetype='application/xml')

def responder_final(texto):
    """
    Envía mensaje de texto y luego la encuesta.
    """
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
    creds_json    = base64.b64decode(GOOGLE_CREDS_B64).decode()
    creds         = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    client_gs     = gspread.authorize(creds)
    creds_drive   = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_service = build('drive', 'v3', credentials=creds_drive)

    # Carpeta ALIA_TURNOS
    folder_id = None
    try:
        results = drive_service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false",
            spaces='drive',
            fields='files(id)'
        ).execute()
        items = results.get('files', [])
        if items:
            folder_id = items[0]['id']
        else:
            meta   = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
            folder = drive_service.files().create(body=meta, fields='id').execute()
            folder_id = folder.get('id')
    except HttpError as e:
        print(f"Error al crear carpeta en Drive: {e}")

    nombre_hoja = f"Turnos_{dia}"
    try:
        hoja = client_gs.open(nombre_hoja).sheet1
    except:
        hoja = client_gs.create(nombre_hoja).sheet1
        if folder_id:
            try:
                drive_service.files().update(
                    fileId=hoja.spreadsheet.id,
                    addParents=folder_id,
                    removeParents='root',
                    fields='id,parents'
                ).execute()
            except HttpError as e:
                print(f"Error al mover hoja: {e}")
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
    """
    Retorna sede y dirección según localidad.
    """
    loc = localidad.lower()
    if loc in ['castelar','ituzaingó','moron']:
        return 'CASTELAR', 'Arias 2530'
    if loc in ['merlo','padua','paso del rey']:
        return 'MERLO', 'Jujuy 847'
    if loc in ['tesei','hurlingham']:
        return 'TESEI', 'Concepción Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

def derivar_a_operador(tel):
    info = pacientes.get(tel, {})
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
        print(f"Error derivando a operador: {e}")

# --- Webhook de WhatsApp -------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    body = request.form.get('Body','').strip()
    msg  = body.lower()
    tel  = request.form.get('From','')

    if tel not in pacientes:
        pacientes[tel] = {
            'estado': None,
            'ocr_fallos': 0,
            'tipo_atencion': None
        }

    # 1) Comando 'asistente'
    if any(k in msg for k in ['asistente','ayuda','operador']):
        derivar_a_operador(tel)
        return responder_final(
            "Estamos derivando tus datos a un operador. En breve te contactarán."
        )

    # 2) Saludo inicial
    if any(k in msg for k in ['hola','buenas']):
        return responder_whatsapp(
            "Hola! Soy ALIA, tu asistente de laboratorio de viaje. ¿En qué puedo ayudarte hoy?\n"
            "• Pedir un turno\n"
            "• Solicitar informes\n"
            "• Contactarte con un operador"
        )

    # 3) Solicitar informes
    if 'informes' in msg and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_informes'
        return responder_whatsapp(
            "Para solicitar informes, envía: Nombre completo, Localidad. Separados por coma."
        )

    if pacientes[tel]['estado'] == 'esperando_informes':
        partes = [p.strip() for p in body.split(',') if p.strip()]
        if len(partes) >= 2:
            nombre, localidad = partes[:2]
            pacientes[tel].update({'nombre': nombre.title(), 'localidad': localidad})
            derivar_a_operador(tel)
            return responder_final(
                f"Solicitamos informes para {nombre.title()} en {localidad}. La sede correspondiente los recibirá y te los enviará."
            )
        return responder_whatsapp(
            "Datos incompletos. Envía: Nombre completo, Localidad. Separados por coma."
        )

    # 4) Pedir turno
    if 'turno' in msg and pacientes[tel]['estado'] is None:
        return responder_whatsapp("¿Preferís turno en sede o atención a domicilio?")

    # 5) Pre-ingreso Sede/Domicilio
    if 'sede' in msg and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos'
        pacientes[tel]['tipo_atencion'] = 'SEDE'
        return responder_whatsapp(
            "Para pre-ingreso en SEDE, envía: Nombre completo, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado."
        )
    if any(k in msg for k in ['domicilio','casa']) and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos'
        pacientes[tel]['tipo_atencion'] = 'DOMICILIO'
        return responder_whatsapp(
            "Para DOMICILIO, envía: Nombre, Dirección, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado."
        )

    # 6) Procesar datos básicos
    if pacientes[tel]['estado'] == 'esperando_datos':
        partes = [p.strip() for p in body.split(',') if p.strip()]
        if len(partes) < 6:
            return responder_whatsapp(
                "Faltan datos. Envía: Nombre, Dirección/Localidad, Fecha, Cobertura, N° Afiliado."
            )
        nombre, direccion, localidad, fecha_nac, cobertura, afiliado = partes[:6]
        pacientes[tel].update({
            'nombre': nombre.title(),
            'direccion': direccion,
            'localidad': localidad,
            'fecha_nacimiento': fecha_nac,
            'cobertura': cobertura,
            'afiliado': afiliado
        })
        pacientes[tel]['estado'] = 'esperando_orden'
        return responder_whatsapp(
            "Ahora por favor envía tu orden médica en foto JPG/PNG o responde 'No tengo orden'."
        )

    # 7) Sin orden
    if pacientes[tel]['estado'] == 'esperando_orden' and 'no tengo orden' in msg:
        pacientes[tel]['estado'] = None
        return responder_final(
            "Entendido, continuamos sin orden médica. Pronto nos comunicaremos contigo."
        )

    # 8) Procesar orden médica (imagen)
    if pacientes[tel]['estado'] == 'esperando_orden' and request.form.get('NumMedia') == '1':
        url = request.form.get('MediaUrl0')
        try:
            resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=5)
            b64  = base64.b64encode(resp.content).decode()
            pacientes[tel]['imagen_base64'] = b64
            ocr = requests.post(OCR_SERVICE_URL, json={'image_base64': b64}, timeout=10)
            texto_ocr = ocr.json().get('text','').strip() if ocr.ok else ''
            if not texto_ocr:
                raise Exception("OCR vacío")
        except Exception:
            return responder_final(
                "No pudimos procesar tu orden. Te derivamos a un operador."
            )
        prompt = f"Analiza orden médica:\n{texto_ocr}\nExtrae estudios, cobertura y afiliado."
        try:
            resp_gpt = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}]
            )
            estudios = resp_gpt.choices[0].message.content.strip()
        except:
            estudios = ""
        pacientes[tel].update({'estado': 'confirmando_estudios', 'texto_ocr': texto_ocr})
        return responder_whatsapp(
            f"Detectamos estos estudios en tu orden:\n{estudios}\n¿Son correctos? Responde 'Sí' o 'No'."
        )

    # 9) Confirmación de estudios
    if pacientes[tel].get('estado') == 'confirmando_estudios':
        if 'sí' in msg or 'si' in msg:
            return responder_final("Perfecto, tus estudios han sido registrados correctamente.")
        if 'no' in msg:
            pacientes[tel]['estado'] = 'esperando_orden'
            return responder_whatsapp("Por favor, envía otra foto clara de la orden médica o responde 'No tengo orden'.")
        return responder_whatsapp("Responde 'Sí', 'No' o 'No tengo orden', por favor.")

    # 10) Fallback GPT
    info  = pacientes.get(tel, {})
    edad  = calcular_edad(info.get('fecha_nacimiento','')) or 'desconocida'
    texto = info.get('texto_ocr','')
    prompt_fb = (
        f"Paciente: {info.get('nombre','Paciente')}, Edad: {edad}\n"
        f"OCR: {texto}\nPregunta: {body}\n"
        "Responde únicamente ayuno y si recolectar orina."
    )
    try:
        resp_fb = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role":"user","content":prompt_fb}]
        )
        return responder_whatsapp(resp_fb.choices[0].message.content.strip())
    except:
        return responder_whatsapp("Error procesando tu consulta. Por favor intentá más tarde.")

# --- Entrypoint ---------------------------------------------------------------
if __name__ == '__main__':
    puerto = int(os.environ.get('PORT',10000))
    app.run(host='0.0.0.0', port=puerto)
