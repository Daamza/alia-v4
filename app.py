from flask import Flask, request, Response
import openai
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

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
pacientes = {}

# --- Funciones auxiliares ---
def responder_whatsapp(texto):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{texto}</Message></Response>'
    return Response(xml, mimetype='application/xml')

def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

def crear_hoja_del_dia(dia):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_json = base64.b64decode(os.getenv("GOOGLE_CREDENTIALS_BASE64")).decode()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    client = gspread.authorize(creds)
    creds_drive = service_account.Credentials.from_service_account_info(json.loads(creds_json), scopes=["https://www.googleapis.com/auth/drive"])
    drive_service = build('drive', 'v3', credentials=creds_drive)

    folder_id = None
    try:
        results = drive_service.files().list(q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false", spaces='drive', fields='files(id,name)').execute()
        items = results.get('files', [])
        if items:
            folder_id = items[0]['id']
        else:
            meta = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
            folder = drive_service.files().create(body=meta, fields='id').execute()
            folder_id = folder.get('id')
    except HttpError as e:
        print(f"Error al acceder/crear carpeta: {e}")

    nombre_hoja = f"Turnos_{dia}"
    try:
        hoja = client.open(nombre_hoja).sheet1
    except:
        hoja = client.create(nombre_hoja).sheet1
        if folder_id:
            try:
                drive_service.files().update(fileId=hoja.spreadsheet.id, addParents=folder_id, removeParents='root', fields='id,parents').execute()
            except HttpError as e:
                print(f"Error mover hoja: {e}")
        hoja.append_row(["Fecha", "Nombre", "Teléfono", "Dirección", "Localidad", "Fecha de Nacimiento", "Cobertura", "Afiliado", "Estudios", "Indicaciones"])
    return hoja

def determinar_dia_turno(localidad):
    loc = localidad.lower()
    wd = datetime.today().weekday()
    if 'ituzaingó' in loc: return 'Lunes'
    if 'merlo' in loc or 'padua' in loc: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc: return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc: return 'Jueves'
    return 'Lunes'

def derivar_a_operador(tel):
    info = pacientes.get(tel, {})
    payload = {
        'nombre': info.get('nombre','No disponible'),
        'direccion': info.get('direccion','No disponible'),
        'localidad': info.get('localidad','No disponible'),
        'fecha_nacimiento': info.get('fecha_nacimiento','No disponible'),
        'cobertura': info.get('cobertura','No disponible'),
        'afiliado': info.get('afiliado','No disponible'),
        'telefono_paciente': tel,
        'tipo_atencion': 'SEDE' if info.get('localidad','').lower()=='sede' else 'DOMICILIO',
        'imagen_base64': info.get('imagen_base64','')
    }
    try:
        requests.post('https://derivador-service.onrender.com/derivar', json=payload)
    except Exception as e:
        print(f"Error derivar operador: {e}")

@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    body = request.form.get('Body','').strip()
    msg = body.lower()
    tel = request.form.get('From','')

    if tel not in pacientes:
        pacientes[tel] = {'estado': None, 'reintentos': 0, 'ocr_fallos': 0}

    if any(k in msg for k in ['asistente', 'ayuda', 'operador']):
        derivar_a_operador(tel)
        return responder_whatsapp("Estamos derivando tus datos a un operador. En breve serás contactado.")

    if any(k in msg for k in ['hola', '¡hola!', 'hola!', 'buenas', 'buenos días']):
        return responder_whatsapp("Hola! Soy ALIA, tu asistente con IA de laboratorio. Escribí *ASISTENTE* en cualquier momento y serás derivado a un operador. ¿En qué puedo ayudarte hoy?")

    if 'turno' in msg:
        return responder_whatsapp("¿Preferís atenderte en alguna de nuestras *sedes* o necesitás atención a *domicilio*?")

    if 'sede' in msg and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos_sede'
        return responder_whatsapp("Perfecto. En *SEDE*, escribí: Nombre completo, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado. *Separalos por comas*.")

    if any(k in msg for k in ['domicilio', 'venir', 'casa']) and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos_domicilio'
        return responder_whatsapp("Perfecto. Para *DOMICILIO*, escribí: Nombre, Dirección, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado. *Separalos por comas*.")

    if pacientes[tel]['estado'] == 'esperando_datos_domicilio':
        parts = [p.strip() for p in body.split(',') if p.strip()]
        if len(parts) >= 6:
            nombre, direccion, loc, fecha_nac, cob, af = parts[:6]
            pacientes[tel].update({'nombre': nombre.title(), 'direccion': direccion, 'localidad': loc, 'fecha_nacimiento': fecha_nac, 'cobertura': cob, 'afiliado': af, 'estado': 'esperando_orden'})
            dia = determinar_dia_turno(loc)
            hoja = crear_hoja_del_dia(dia)
            hoja.append_row([datetime.now().isoformat(), nombre.title(), tel, direccion, loc, fecha_nac, cob, af, '', 'Pendiente'])
            return responder_whatsapp(f"Tu turno fue agendado para el día {dia} entre las 08:00 y 11:00 hs. Ahora, por favor, envía la orden médica (foto o PDF).")
        else:
            return responder_whatsapp("Faltan datos. Enviá: Nombre, Dirección, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado. Separalos por comas.")

    if pacientes[tel]['estado'] == 'esperando_orden' and request.form.get('NumMedia') == '1':
        url = request.form.get('MediaUrl0')
        try:
            resp = requests.get(url, auth=(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN')))
            b64 = base64.b64encode(resp.content).decode()
            pacientes[tel]['imagen_base64'] = b64
            ocr = requests.post('https://ocr-microsistema.onrender.com/ocr', json={'image_base64': b64})
            texto_ocr = ocr.json().get('text', '').strip() if ocr.ok else ''
            if not texto_ocr:
                raise Exception('OCR vacío')
        except Exception:
            pacientes[tel]['ocr_fallos'] += 1
            if pacientes[tel]['ocr_fallos'] >= 3:
                derivar_a_operador(tel)
                return responder_whatsapp("No pudimos procesar la orden médica. Te derivamos a un *operador*.")
            return responder_whatsapp("La imagen no se pudo procesar correctamente. Enviá otra foto clara o escribí *ASISTENTE* para ayuda.")

        prompt = f"Analiza orden médica:\n{texto_ocr}\nExtrae estudios, cobertura, afiliado"
        chat = openai.chat.completions.create(model='gpt-4', messages=[{'role': 'user', 'content': prompt}])
        res = chat.choices[0].message.content
        pacientes[tel].update({'estado': 'confirmando_estudios', 'texto_ocr': texto_ocr, 'resumen_estudios': res})
        return responder_whatsapp(f"Detectamos estos estudios en tu orden:\n{res}\n¿Son correctos? Respondé *Sí* o *No*.")

    if pacientes[tel].get('estado') == 'confirmando_estudios':
        if 'sí' in msg or 'si' in msg:
            pacientes[tel]['estado'] = 'completo'
            return responder_whatsapp("Perfecto. Tus estudios han sido registrados correctamente.")
        elif 'no' in msg:
            pacientes[tel]['estado'] = 'esperando_orden'
            return responder_whatsapp("Reenvianos una foto clara de la orden médica o escribí *ASISTENTE* para ayuda.")
        else:
            return responder_whatsapp("¿Los estudios detectados son correctos? Por favor respondé *Sí* o *No*.")

    info = pacientes.get(tel, {})
    edad = calcular_edad(info.get('fecha_nacimiento', '')) or 'desconocida'
    texto = info.get('texto_ocr', '')
    prompt_fb = f"Paciente:{info.get('nombre','Paciente')},Edad:{edad}\nOCR:{texto}\nPregunta:{body}\nResponde solo cuánto ayuno debe hacer y si debe recolectar orina."
    fb = openai.chat.completions.create(model='gpt-4', messages=[{'role': 'user', 'content': prompt_fb}])
    mensaje = fb.choices[0].message.content.strip()
    mensaje += ("\n\n¡Gracias por comunicarte con ALIA! Si querés ayudarnos a mejorar, completá esta breve encuesta: https://forms.gle/gHPbyMJfF18qYuUq9\n\nEscribí *ASISTENTE* en cualquier momento para ser derivado a un operador.")
    return responder_whatsapp(mensaje)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
