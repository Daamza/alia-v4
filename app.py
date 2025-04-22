from flask import Flask, request, Response
import pytesseract
from PIL import Image
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import base64
import io
import os
import json
from datetime import datetime

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")

pacientes = {}

def crear_hoja_del_dia(dia):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_json = base64.b64decode(os.getenv("GOOGLE_CREDENTIALS_BASE64")).decode()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    client = gspread.authorize(creds)

    try:
        hoja = client.open(f"Turnos_{dia}").sheet1
    except:
        hoja = client.create(f"Turnos_{dia}").sheet1
        hoja.append_row(["Fecha", "Nombre", "Teléfono", "Dirección", "Localidad", "Cobertura", "Afiliado", "Estudios", "Indicaciones"])

    return hoja

def determinar_dia_turno(localidad):
    localidad = localidad.lower()
    if "ituzaingó" in localidad:
        return "Lunes"
    elif "merlo" in localidad or "padua" in localidad:
        return "Martes" if datetime.today().weekday() < 4 else "Viernes"
    elif "tesei" in localidad or "hurlingham" in localidad:
        return "Miércoles" if datetime.today().weekday() < 4 else "Sábado"
    elif "castelar" in localidad:
        return "Jueves"
    else:
        return "Lunes"

def responder_whatsapp(mensaje):
    return Response(f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{mensaje}</Message>
</Response>""", mimetype='application/xml')

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    mensaje = request.form.get("Body", "").lower()
    telefono = request.form.get("From", "")
    nombre = pacientes.get(telefono, {}).get("nombre", "")

    if telefono not in pacientes:
        pacientes[telefono] = {}

    if "hola" in mensaje:
        return responder_whatsapp("Hola!!! soy ALIA, ¿en qué puedo ayudarte hoy?")

    elif "sede" in mensaje:
        return responder_whatsapp("Podés acercarte a cualquiera de nuestras sedes sin turno en el horario de 07:30hrs a 11:00hrs para extracciones de sangre, y de 07:30hrs a 17:00hrs para entrega de informes y recepción de muestras.")

    elif "domicilio" in mensaje:
        pacientes[telefono]["estado"] = "esperando_datos"
        return responder_whatsapp("Perfecto. Por favor, indicame: Nombre completo, Dirección, Localidad, Cobertura y Número de Afiliado (todo en un solo mensaje).")

    elif pacientes[telefono].get("estado") == "esperando_datos":
        partes = mensaje.split(",")
        if len(partes) >= 5:
            pacientes[telefono].update({
                "nombre": partes[0].strip().title(),
                "direccion": partes[1].strip(),
                "localidad": partes[2].strip(),
                "cobertura": partes[3].strip(),
                "afiliado": partes[4].strip(),
                "estado": "esperando_orden"
            })
            dia_turno = determinar_dia_turno(partes[2])
            pacientes[telefono]["dia"] = dia_turno

            hoja = crear_hoja_del_dia(dia_turno)
            hoja.append_row([str(datetime.now()), partes[0].strip(), telefono, partes[1].strip(), partes[2].strip(), partes[3].strip(), partes[4].strip(), "", "Pendiente"])

            return responder_whatsapp(f"¡Gracias {partes[0].strip()}! Agendamos tu turno para el día {dia_turno} entre las 08:00 y las 11:00 hs. ¿Podés enviarnos una foto de la orden médica?")

        return responder_whatsapp("Faltan datos. Por favor escribí: Nombre, Dirección, Localidad, Cobertura, Afiliado (todo separado por comas).")

    else:
        return responder_whatsapp("Disculpá, no entendí tu mensaje. Podés decir 'sede' o 'domicilio' para comenzar.")

if __name__ == "__main__":
    app.run()