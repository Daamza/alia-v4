from flask import Flask, request, Response
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import base64
import os
import json
from datetime import datetime
import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
pacientes = {}

def crear_hoja_del_dia(dia):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_json = base64.b64decode(os.getenv("GOOGLE_CREDENTIALS_BASE64")).decode()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    client = gspread.authorize(creds)
    creds_drive = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_service = build('drive', 'v3', credentials=creds_drive)

    folder_id = None
    try:
        results = drive_service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false",
            spaces='drive', fields='files(id,name)'
        ).execute()
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
                drive_service.files().update(
                    fileId=hoja.spreadsheet.id,
                    addParents=folder_id,
                    removeParents='root',
                    fields='id,parents'
                ).execute()
            except HttpError as e:
                print(f"Error mover hoja: {e}")
        hoja.append_row([
            "Fecha","Nombre","Teléfono","Dirección","Localidad",
            "Fecha de Nacimiento","Cobertura","Afiliado","Estudios","Indicaciones"
        ])
    return hoja

def determinar_dia_turno(localidad):
    loc = localidad.lower()
    wd = datetime.today().weekday()
    if 'ituzaingó' in loc: return 'Lunes'
    if 'merlo' in loc or 'padua' in loc: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc: return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc: return 'Jueves'
    return 'Lunes'

def asignar_sede(localidad_usuario):
    loc = localidad_usuario.lower()
    if 'ituzaingó' in loc or 'castelar' in loc:
        return 'CASTELAR', 'Arias 2530, Castelar'
    if 'tesei' in loc or 'hurlingham' in loc:
        return 'TESEI', 'Concepción Arenal 2890, Villa Tesei'
    if 'merlo' in loc or 'padua' in loc:
        return 'MERLO', 'Jujuy 845, Merlo'
    return 'CASTELAR', 'Arias 2530, Castelar'

def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month,hoy.day) < (nac.month,nac.day))
    except:
        return None

def responder_whatsapp(texto):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{texto}</Message></Response>'
    return Response(xml, mimetype='application/xml')

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
        pacientes[tel] = {'estado': None}

    if any(k in msg for k in ['resultados','informe','informes', 'asistente']):
        derivar_a_operador(tel)
        return responder_whatsapp('Estamos derivando tus datos a un operador. En breve serás contactado.')

    if msg in ['hola', '¡hola!', 'hola!', 'buenas', 'buenos días']:
        return responder_whatsapp('Hola! Soy ALIA, tu asistente con IA de laboratorio. Escribe "asistente" en cualquier momento y serás derivado a un operador. ¿En qué puedo ayudarte hoy?')

    if 'turno' in msg and msg not in ['sede','domicilio']:
        return responder_whatsapp('¿Prefieres atenderte en alguna de nuestras sedes o necesitás atención a domicilio? escribe alguna de las dos opciones')

    if msg == 'sede' and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos_sede'
        return responder_whatsapp('por favor envía tu Nombre y Apellido, Localidad, Fecha nacimiento (dd/mm/aaaa), Cobertura, N° Afiliado, separados por comas.')

    if pacientes[tel]['estado'] == 'esperando_datos_sede':
        parts = [p.strip() for p in body.split(',')]
        if len(parts) == 5:
            nombre, loc, fecha_nac, cob, af = parts
            sede, dir_sede = asignar_sede(loc)
            pacientes[tel].update({'nombre': nombre.title(), 'localidad': loc, 'estado': 'completo'})
            hoja = crear_hoja_del_dia(sede)
            hoja.append_row([
                datetime.now().isoformat(), nombre.title(), tel, sede, dir_sede,
                fecha_nac, cob, af, '', ''
            ])
            return responder_whatsapp(f"Hola {nombre.title()}, tus datos se han ingresado correctamente. Podés acercarte de lunes a sábado de 07:30 a 11:00 hs en {sede} ({dir_sede}).")
        else:
            return responder_whatsapp('Faltan datos. Envíalos separados por comas.')

    if msg == 'domicilio' and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos'
        return responder_whatsapp('Envía por favor tu Nombre y Apellido, Dirección, Localidad, Fecha de Nac. (dd/mm/aaaa), Cobertura, N° Afiliado, separados por comas.')
    if pacientes[tel]['estado'] == 'esperando_datos':
        parts = [p.strip() for p in body.split(',')]
        if len(parts) >= 6:
            nombre, direccion, loc, fecha_nac, cob, af = parts[:6]
            dia = determinar_dia_turno(loc)
            pacientes[tel].update({
                'nombre': nombre.title(), 'direccion': direccion, 'localidad': loc,
                'fecha_nacimiento': fecha_nac, 'cobertura': cob, 'afiliado': af,
                'estado': 'esperando_orden'
            })
            hoja = crear_hoja_del_dia(dia)
            hoja.append_row([
                datetime.now().isoformat(), nombre, tel, direccion, loc, fecha_nac,
                cob, af, '', 'Pendiente'
            ])
            return responder_whatsapp(f'Tu turno fue agendado con éxito para el día {dia} entre las 08:00 y 11:00 hs. Ahora, por favor, envía la orden médica.')
        else:
            return responder_whatsapp('Faltan datos. Por favor, envía los 6 campos separados por comas.')

    if pacientes[tel].get('estado') == 'esperando_orden' and request.form.get('NumMedia') == '1':
        url = request.form.get('MediaUrl0')
        try:
            resp = requests.get(url, auth=(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN')))
            b64 = base64.b64encode(resp.content).decode()
            pacientes[tel]['imagen_base64'] = b64
            oc = requests.post('https://ocr-microsistema.onrender.com/ocr', json={'image_base64': b64})
            texto_ocr = oc.json().get('text', '').strip() if oc.ok else ''
            if not texto_ocr: raise Exception('OCR vacío')
        except Exception:
            derivar_a_operador(tel)
            return responder_whatsapp('Tuvimos un problema al procesar la orden. Podés esperar unos minutos o escribir ASISTENTE para ser derivado a un operador.')

        prompt = f"Analiza orden médica:\n{texto_ocr}\nExtrae estudios, cobertura, afiliado"
        chat = openai.chat.completions.create(model='gpt-4', messages=[{'role': 'user', 'content': prompt}])
        res = chat.choices[0].message.content
        hoja = crear_hoja_del_dia(datetime.today().strftime('%A'))
        hoja.append_row([
            "Orden médica", pacientes[tel]['nombre'], tel, pacientes[tel].get('direccion', ''),
            pacientes[tel].get('localidad', ''), pacientes[tel].get('fecha_nacimiento', ''),
            pacientes[tel].get('cobertura', ''), pacientes[tel].get('afiliado', ''), texto_ocr, res
        ])
        pacientes[tel].update({'estado': 'confirmando_estudios', 'texto_ocr': texto_ocr, 'resumen_estudios': res})
        return responder_whatsapp(f'Detectamos los siguientes estudios en tu orden médica:\n{res}\n¿Son correctos? (Sí / No)')

    if pacientes[tel].get('estado') == 'confirmando_estudios':
        if 'sí' in msg or 'si' in msg:
            pacientes[tel]['estado'] = 'completo'
            return responder_whatsapp('Perfecto. Tus estudios han sido registrados correctamente.')
        elif 'no' in msg:
            pacientes[tel]['estado'] = 'esperando_orden'
            return responder_whatsapp('Por favor, vuelve a enviar una foto clara de la orden médica o escribe ASISTENTE para ser derivado a un operador.')
        else:
            return responder_whatsapp('¿Los estudios detectados son correctos? Por favor responde Sí o No.')

    info = pacientes.get(tel, {})
    edad = calcular_edad(info.get('fecha_nacimiento', '')) or 'desconocida'
    texto = info.get('texto_ocr', '')
    prompt_fb = (
        f"Paciente:{info.get('nombre','Paciente')},Edad:{edad}\n"
        f"OCR:{texto}\n"
        f"Pregunta:{body}\n"
        "Responde solo cuánto ayuno debe hacer y si debe recolectar orina."
    )
    fb = openai.chat.completions.create(model='gpt-4', messages=[{'role': 'user', 'content': prompt_fb}])
    return responder_whatsapp(fb.choices[0].message.content)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
