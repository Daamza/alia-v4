from flask import Flask, request, Response from twilio.twiml.messaging_response import MessagingResponse from openai import OpenAI import gspread from oauth2client.service_account import ServiceAccountCredentials import base64 import os import json import requests from datetime import datetime from googleapiclient.discovery import build from googleapiclient.errors import HttpError from google.oauth2 import service_account

--- Configuración -------------------------------------------------------------

OCR_SERVICE_URL       = "https://ocr-microsistema.onrender.com/ocr" DERIVADOR_SERVICE_URL = "https://derivador-service.onrender.com/derivar" GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64") TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID") TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN") OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")

Instanciamos Flask y OpenAI

app      = Flask(name) client   = OpenAI(api_key=OPENAI_API_KEY) pacientes = {}

--- Funciones auxiliares ------------------------------------------------------

def responder_whatsapp(texto): """ Envía el mensaje y, a continuación, solicita la encuesta siempre. """ resp = MessagingResponse() resp.message(texto) encuesta = ( "\n\n¡Gracias por comunicarte con ALIA! " "Ayudanos a mejorar completando esta encuesta: " "https://forms.gle/gHPbyMJfF18qYuUq9" ) resp.message(encuesta) return Response(str(resp), mimetype='application/xml')

def calcular_edad(fecha_str): try: nac = datetime.strptime(fecha_str, '%d/%m/%Y') hoy = datetime.today() return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day)) except: return None

def crear_hoja_del_dia(dia): scope = [ "https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive" ] creds_json = base64.b64decode(GOOGLE_CREDS_B64).decode() creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope) client_gs = gspread.authorize(creds) creds_drive = service_account.Credentials.from_service_account_info( json.loads(creds_json), scopes=["https://www.googleapis.com/auth/drive"] ) drive_service = build('drive', 'v3', credentials=creds_drive)

# Carpeta ALIA_TURNOS
folder_id = None
try:
    results = drive_service.files().list(
        q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false'",
        spaces='drive', fields='files(id)'
    ).execute()
    items = results.get('files', [])
    if items:
        folder_id = items[0]['id']
    else:
        meta = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
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

def determinar_dia_turno(localidad): loc = localidad.lower() wd  = datetime.today().weekday() if 'ituzaingó' in loc:                    return 'Lunes' if 'merlo' in loc or 'padua' in loc:      return 'Martes' if wd < 4 else 'Viernes' if 'tesei' in loc or 'hurlingham' in loc: return 'Miércoles' if wd < 4 else 'Sábado' if 'castelar' in loc:                     return 'Jueves' return 'Lunes'

def determinar_sede(localidad): """Retorna sede y dirección según localidad""" loc = localidad.lower() if loc in ['castelar','ituzaingó','moron']: return 'CASTELAR', 'Arias 2530' if loc in ['merlo','padua','paso del rey']: return 'MERLO', 'Jujuy 847' if loc in ['tesei','hurlingham']: return 'TESEI', 'Concepción Arenal 2694' return 'GENERAL', 'Nuestra sede principal'

def derivar_a_operador(tel): info = pacientes.get(tel, {}) payload = { 'nombre':             info.get('nombre','No disponible'), 'direccion':          info.get('direccion','No disponible'), 'localidad':          info.get('localidad','No disponible'), 'fecha_nacimiento':   info.get('fecha_nacimiento','No disponible'), 'cobertura':          info.get('cobertura','No disponible'), 'afiliado':           info.get('afiliado','No disponible'), 'telefono_paciente':  tel, 'tipo_atencion':      info.get('tipo_atencion','No disponible'), 'imagen_base64':      info.get('imagen_base64','') } try: requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5) except Exception as e: print(f"Error derivando a operador: {e}")

--- Webhook de WhatsApp -------------------------------------------------------

@app.route('/webhook', methods=['POST']) def whatsapp_webhook(): body = request.form.get('Body','').strip() msg  = body.lower() tel  = request.form.get('From','')

if tel not in pacientes:
    pacientes[tel] = {
        'estado': None,
        'reintentos': 0,
        'ocr_fallos': 0,
        'tipo_atencion': None
    }

# 1) Derivación directa
if any(k in msg for k in ['asistente','ayuda','operador']):
    derivar_a_operador(tel)
    return responder_whatsapp("Estamos derivando tus datos a un operador. En breve te contactarán.")

# 2) Saludo con opciones
if any(k in msg for k in ['hola','buenas']):
    return responder_whatsapp(
        "Hola! Soy ALIA, tu asistente de laboratorio de viaje. ¿En qué puedo ayudarte hoy?\n"
        "• Pedir un turno\n"
        "• Solicitar informes\n"
        "• Contactarte con un operador"
    )

# 3) Pedir turno
if 'turno' in msg:
    return responder_whatsapp("¿Preferís turno en sede o atención a domicilio?")

# 4) Solicitar informes
if 'informes' in msg:
    pacientes[tel]['estado'] = 'esperando_informes'
    pacientes[tel]['tipo_atencion'] = 'INFORMES'
    return responder_whatsapp(
        "Para solicitar informes, envía: Nombre completo, Localidad. Separados por coma."
    )

# 5) Flujo de informes
if pacientes[tel].get('estado') == 'esperando_informes':
    partes = [p.strip() for p in body.split(',') if p.strip()]
    if len(partes) >= 2:
        nombre, localidad = partes[:2]
        pacientes[tel].update({'nombre': nombre.title(), 'localidad': localidad})
        derivar_a_operador(tel)
        pacientes[tel]['estado'] = None
        return responder_whatsapp(
            f"Solicitamos informes para {nombre.title()} en {localidad}. La sede correspondiente los recibirá y te los enviará."
        )
    return responder_whatsapp("Datos incompletos. Envía: Nombre completo, Localidad. Separados por coma.")

# 6) Flujo SEDE
if 'sede' in msg and pacientes[tel]['estado'] is None:
    pacientes[tel].update({'estado': 'esperando_datos', 'tipo_atencion': 'SEDE'})
    return responder_whatsapp(
        "Para SEDE, envía: Nombre completo, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado. Separados por comas."
    )

# 7) Flujo DOMICILIO
if any(k in msg for k in ['domicilio','casa']) and pacientes[tel]['estado'] is None:
    pacientes[tel].update({'estado': 'esperando_datos', 'tipo_atencion': 'DOMICILIO'})
    return responder_whatsapp(
        "Para DOMICILIO, envía: Nombre, Dirección, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado. Separados por comas."
    )

# 8) Procesar datos de turno
if pacientes[tel]['estado'] == 'esperando_datos':
    partes = [p.strip() for p in body.split(',') if p.strip()]
    if len(partes) >= 6:
        nombre, direccion, localidad, fecha_nac, cobertura, afiliado = partes[:6]
        pacientes[tel].update({
            'nombre':           nombre.title(),
            'direccion':        direccion,
            'localidad':        localidad,
            'fecha_nacimiento': fecha_nac,
            'cobertura':        cobertura,
            'afiliado':         afiliado,
            'estado':           'procesado'
        })
        if pacientes[tel]['tipo_atencion'] == 'DOMICILIO':
            dia = determinar_dia_turno(localidad)
            hoja = crear_hoja_del_dia(dia)
            hoja.append_row([
                datetime.now().isoformat(), nombre.title(), tel,
                direccion, localidad, fecha_nac, cobertura, afiliado,
                '', 'Pendiente'
            ])
            return responder_whatsapp(
                f"Tu turno a domicilio quedó agendado para {dia} de 08:00 a 11:00 hs."
            )
        # SEDE
        sede, dir_sede = determinar_sede(localidad)
        return responder_whatsapp(
            f"¡Perfecto {nombre.title()}, tus datos fueron ingresados exitosamente a nuestra base de datos. "
            f"Puedes acercarte de lunes a sábados en el horario de 7:30 hrs a 11:00 hrs para realizar tus estudios por nuestra sede "
            f"{sede} localizada en {dir_sede}."
        )
    return responder_whatsapp(
        "Faltan datos. Envía: Nombre, Dirección, Localidad, Fecha, Cobertura, N° Afiliado. Separados por comas."
    )

# 9) Procesar orden médica (imagen) y 10) Fallback GPT...
# Aquí continúa el flujo que ya tenías implementado.

return responder_whatsapp("No entendí tu solicitud. Escribí 'turno', 'informes' o 'ASISTENTE'.")

--- Main ----------------------------------------------------------------------

if name == 'main': puerto = int(os.environ.get('PORT', 10000)) app.run(host='0.0.0.0', port=puerto)

