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

# --- Funciones auxiliares ---

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
    return responder_whatsapp("Hola, soy ALIA y el webhook está funcionando correctamente.")

if __name__ == '__main__':
    app.run()
