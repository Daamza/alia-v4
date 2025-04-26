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

    # Buscar o crear carpeta ALIA_TURNOS
    folder_id = None
    try:
        results = drive_service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false",
            spaces='drive',
            fields='files(id, name)'
        ).execute()
        items = results.get('files', [])
        if items:
            folder_id = items[0]['id']
        else:
            meta = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
            folder = drive_service.files().create(body=meta, fields='id').execute()
            folder_id = folder.get('id')
    except HttpError as e:
        print(f"Error al acceder o crear carpeta ALIA_TURNOS: {e}")

    nombre_archivo = f"Turnos_{dia}"
    try:
        hoja = client.open(nombre_archivo).sheet1
    except:
        nueva = client.create(nombre_archivo)
        if folder_id:
            try:
                drive_service.files().update(
                    fileId=nueva.id,
                    addParents=folder_id,
                    removeParents='root',
                    fields='id, parents'
                ).execute()
            except HttpError as e:
                print(f"Error al mover la hoja a ALIA_TURNOS: {e}")
        hoja = nueva.sheet1
        hoja.append_row([
            "Fecha", "Nombre", "Teléfono", "Dirección", "Localidad",
            "Fecha de Nacimiento", "Cobertura", "Afiliado", "Estudios", "Indicaciones"
        ])
    return hoja


def determinar_dia_turno(localidad):
    loc = localidad.lower()
    hoy = datetime.today().weekday()
    if "ituzaingó" in loc:
        return "Lunes"
    if "merlo" in loc or "padua" in loc:
        return "Martes" if hoy < 4 else "Viernes"
    if "tesei" in loc or "hurlingham" in loc:
        return "Miércoles" if hoy < 4 else "Sábado"
    if "castelar" in loc:
        return "Jueves"
    return "Lunes"


def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, "%d/%m/%Y")
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None


def responder_whatsapp(texto):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{texto}</Message></Response>'
    return Response(xml, mimetype='application/xml')


def derivar_a_operador(tel):
    data = {
        "nombre": pacientes[tel].get("nombre", "No disponible"),
        "direccion": pacientes[tel].get("direccion", "No disponible"),
        "localidad": pacientes[tel].get("localidad", "No disponible"),
        "fecha_nacimiento": pacientes[tel].get("fecha_nacimiento", "No disponible"),
        "cobertura": pacientes[tel].get("cobertura", "No disponible"),
        "afiliado": pacientes[tel].get("afiliado", "No disponible"),
        "telefono_paciente": tel,
        "tipo_atencion": "SEDE" if pacientes[tel].get("localidad", "").lower() == "sede" else "DOMICILIO",
        "imagen_base64": pacientes[tel].get("imagen_base64", "")
    }
    try:
        requests.post("https://derivador-service.onrender.com/derivar", json=data)
    except Exception as e:
        print(f"Error derivación: {e}")

# --- Webhook principal ---

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    msg = request.form.get("Body", "").lower()
    tel = request.form.get("From", "")

    if tel not in pacientes:
        pacientes[tel] = {}

    # Derivaciones e inicio
    if any(k in msg for k in ["resultados", "informe", "informes"]):
        derivar_a_operador(tel)
        return responder_whatsapp("Te estamos derivando con un operador para ayudarte con informes o resultados. Serás contactado en breve.")
    if "asistente" in msg:
        derivar_a_operador(tel)
        return responder_whatsapp("Te estamos derivando con un operador, serás contactado en breve.")
    if "hola" in msg:
        return responder_whatsapp("Hola!!! Soy ALIA, ¿en qué puedo ayudarte hoy?\nRecordá que si necesitás ayuda humana podés escribir ASISTENTE en cualquier momento.")
    if "turno" in msg and not any(x in msg for x in ["domicilio", "sede"]):
        return responder_whatsapp("¿Qué modalidad preferís? Escribí SEDE o DOMICILIO para continuar.")

    # Flujo SEDE
    if "sede" in msg and pacientes[tel].get("estado") != "esperando_datos_sede":
        pacientes[tel]["localidad"] = "sede"
        pacientes[tel]["estado"] = "esperando_datos_sede"
        return responder_whatsapp(
            "Perfecto. En SEDE, envíanos tu Nombre completo, Fecha de nacimiento (dd/mm/aaaa), Cobertura y Número de Afiliado, separados por comas."
        )
    if pacientes[tel].get("estado") == "esperando_datos_sede":
        partes = msg.split(",")
        if len(partes) >= 4:
            nombre = partes[0].strip().title()
            fecha_nac = partes[1].strip()
            cobertura = partes[2].strip()
            afiliado = partes[3].strip()
            pacientes[tel].update({
                "nombre": nombre,
                "direccion": "",
                "fecha_nacimiento": fecha_nac,
                "cobertura": cobertura,
                "afiliado": afiliado,
                "estado": "esperando_orden",
                "dia": datetime.today().strftime("%A")
            })
            hoja = crear_hoja_del_dia(pacientes[tel]["dia"])
            hoja.append_row([
                str(datetime.now()), nombre, tel, "", "sede",
                fecha_nac, cobertura, afiliado, "", "Pendiente"
            ])
            return responder_whatsapp(
                f"¡Gracias {nombre}! Tu turno en sede es entre las 08:00 y las 11:00 hs. Por favor envíanos una foto o PDF de la orden médica."
            )
        return responder_whatsapp(
            "Faltan datos. Envía: Nombre, Fecha de nacimiento, Cobertura y Afiliado separados por comas."
        )

    # Flujo DOMICILIO
    if "domicilio" in msg and pacientes[tel].get("estado") != "esperando_datos":
        pacientes[tel]["estado"] = "esperando_datos"
        return responder_whatsapp(
            "Perfecto. Envía Nombre completo, Dirección, Localidad, Fecha de nacimiento (dd/mm/aaaa), Cobertura y Afiliado, separados por comas."
        )
    if pacientes[tel].get("estado") == "esperando_datos":
        partes = msg.split(",")
        if len(partes) >= 6:
            nombre, direccion, loc, fecha_nac, cobertura, afiliado = [p.strip() for p in partes[:6]]
            pacientes[tel].update({
                "nombre": nombre.title(),
                "direccion": direccion,
                "localidad": loc,
                "fecha_nacimiento": fecha_nac,
                "cobertura": cobertura,
                "afiliado": afiliado,
                "estado": "esperando_orden"
            })
            dia_turno = determinar_dia_turno(loc)
            pacientes[tel]["dia"] = dia_turno
            hoja = crear_hoja_del_dia(dia_turno)
            hoja.append_row([
                str(datetime.now()), nombre, tel, direccion, loc,
                fecha_nac, cobertura, afiliado, "", "Pendiente"
            ])
            return responder_whatsapp(
                f"¡Gracias {nombre}! Agendamos tu turno para {dia_turno} entre las 08:00 y las 11:00 hs. ¿Podés enviarnos la orden médica?"
            )
        return responder_whatsapp(
            "Faltan datos. Envía Nombre, Dirección, Localidad, Fecha de nacimiento, Cobertura y Afiliado separados por comas."
        )

    # Procesar orden médica
    if pacientes[tel].get("estado") == "esperando_orden" and request.form.get("NumMedia") == "1":
        media = request.form.get("MediaUrl0")
        try:
            resp = requests.get(media, auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")))
            img_b64 = base64.b64encode(resp.content).decode()
            pacientes[tel]["imagen_base64"] = img_b64
            ocr = requests.post("https://ocr-microsistema.onrender.com/ocr", json={"image_base64": img_b64})
            if ocr.ok:
                texto_ocr = ocr.json().get("text", "").strip()
                if not texto_ocr:
                    raise Exception("OCR vacío")
            else:
                raise Exception("OCR falló")
        except:
            derivar_a_operador(tel)
            return responder_whatsapp("¡Ups! No pudimos procesar tu orden. Te derivamos a un operador.")
        prompt = f"Analizá esta orden médica:\n{texto_ocr}\nExtraé estudios, cobertura, afiliado e indica ayuno y recolección de orina."
        res = openai.chat.completions.create(model="gpt-4", messages=[{"role":"user","content":prompt}])
        resultado = res.choices[0].message.content
        hoja = crear_hoja_del_dia(pacientes[tel]["dia"])
        hoja.append_row([
            "Orden médica", pacientes[tel]["nombre"], tel, pacientes[tel].get("direccion",""),
            pacientes[tel].get("localidad",""), pacientes[tel].get("fecha_nacimiento",""),
            pacientes[tel].get("cobertura",""), pacientes[tel].get("afiliado",""),
            texto_ocr, resultado
        ])
        pacientes[tel]["texto_ocr"] = texto_ocr
        pacientes[tel]["estado"] = "completo"
        return responder_whatsapp(f"Gracias. Estas son tus indicaciones:\n{resultado}\n¡Te esperamos!")

    # Fallback GPT limitado a ayuno e orina
    nombre = pacientes[tel].get("nombre","Paciente")
    fecha_nac = pacientes[tel].get("fecha_nacimiento","")
    edad = calcular_edad(fecha_nac) if fecha_nac else "desconocida"
    texto_ocr = pacientes[tel].get("texto_ocr","")
    prompt_fb = (
        f"Paciente: {nombre}, Edad: {edad}\nTexto OCR: {texto_ocr}\nPregunta: {msg}\n"
        "Respondé SOLO si necesita ayuno y cuántas horas, y si debe recolectar orina."
        " Si no se puede, decí: '¡Opss! Necesitamos más ayuda. Escribí ASISTENTE para ser derivado.'"
    )
    fb = openai.chat.completions.create(model="gpt-4", messages=[{"role":"user","content":prompt_fb}])
    return responder_whatsapp(fb.choices[0].message.content.strip())

if __name__ == "__main__":
    app.run()
