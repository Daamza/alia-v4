from flask import Flask, request, Response
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import base64
import os
import json
from datetime import datetime
import requests

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
        hoja.append_row(["Fecha", "Nombre", "Teléfono", "Dirección", "Localidad", "Fecha de Nacimiento", "Cobertura", "Afiliado", "Estudios", "Indicaciones"])
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

def calcular_edad(fecha_nacimiento):
    try:
        fecha = datetime.strptime(fecha_nacimiento, "%d/%m/%Y")
        hoy = datetime.today()
        edad = hoy.year - fecha.year - ((hoy.month, hoy.day) < (fecha.month, fecha.day))
        return edad
    except:
        return None

def responder_whatsapp(mensaje):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{mensaje}</Message></Response>'
    return Response(xml, mimetype='application/xml')

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    mensaje = request.form.get("Body", "").lower()
    telefono = request.form.get("From", "")
    
    if telefono not in pacientes:
        pacientes[telefono] = {}

    # Derivación automática si el paciente escribe "asistente"
    if "asistente" in mensaje:
        return responder_whatsapp("Te estamos derivando a un operador humano. Comunicate al +54 9 11 xxxx xxxx.")

    # Inicio de conversación
    if "hola" in mensaje:
        return responder_whatsapp("Hola!!! soy ALIA, ¿en qué puedo ayudarte hoy?\nRecordá que si necesitás ayuda humana podés escribir ASISTENTE en cualquier momento.")

    elif "sede" in mensaje:
        pacientes[telefono] = {
            "nombre": "Paciente sede",
            "direccion": "",
            "localidad": "sede",
            "fecha_nacimiento": "",
            "cobertura": "Desconocida",
            "afiliado": "No aplica",
            "estado": "esperando_orden",
            "dia": datetime.today().strftime("%A")
        }
        hoja = crear_hoja_del_dia(pacientes[telefono]["dia"])
        hoja.append_row([str(datetime.now()), "Paciente sede", telefono, "", "sede", "", "Desconocida", "No aplica", "", "Pendiente"])
        return responder_whatsapp("Perfecto. Para continuar, por favor envianos una foto de la orden médica.")

    elif "domicilio" in mensaje:
        pacientes[telefono]["estado"] = "esperando_datos"
        return responder_whatsapp("Perfecto. Por favor, indicame: Nombre completo, Dirección, Localidad, Fecha de nacimiento (dd/mm/aaaa), Cobertura y Número de Afiliado (todo en un solo mensaje separado por comas).")

    elif pacientes[telefono].get("estado") == "esperando_datos":
        partes = mensaje.split(",")
        if len(partes) >= 6:
            pacientes[telefono].update({
                "nombre": partes[0].strip().title(),
                "direccion": partes[1].strip(),
                "localidad": partes[2].strip(),
                "fecha_nacimiento": partes[3].strip(),
                "cobertura": partes[4].strip(),
                "afiliado": partes[5].strip(),
                "estado": "esperando_orden"
            })
            dia_turno = determinar_dia_turno(partes[2])
            pacientes[telefono]["dia"] = dia_turno
            hoja = crear_hoja_del_dia(dia_turno)
            hoja.append_row([
                str(datetime.now()),
                partes[0].strip(),
                telefono,
                partes[1].strip(),
                partes[2].strip(),
                partes[3].strip(),
                partes[4].strip(),
                partes[5].strip(),
                "",
                "Pendiente"
            ])
            return responder_whatsapp(f"¡Gracias {partes[0].strip()}! Agendamos tu turno para el día {dia_turno} entre las 08:00 y las 11:00 hs. ¿Podés enviarnos una foto de la orden médica?")
        return responder_whatsapp("Faltan datos. Por favor escribí: Nombre, Dirección, Localidad, Fecha de nacimiento, Cobertura y Afiliado (todo separado por comas).")

    elif pacientes[telefono].get("estado") == "esperando_orden" and request.form.get("NumMedia") == "1":
        media_url = request.form.get("MediaUrl0")
        image_response = requests.get(media_url, auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")))
        image_base64 = base64.b64encode(image_response.content).decode()
        ocr_response = requests.post("https://ocr-microsistema.onrender.com/ocr", json={"image_base64": image_base64})

        if ocr_response.ok:
            try:
                texto_ocr = ocr_response.json().get("text", "")
            except json.JSONDecodeError:
                return responder_whatsapp("Hubo un problema con el servicio de OCR. ¿Podés intentar enviar la imagen de nuevo?")
        else:
            return responder_whatsapp("No pudimos conectar con el servicio OCR. Intentá más tarde.")

        prompt = f"Analizá esta orden médica:\n{texto_ocr}\nExtraé: estudios, cobertura, número de afiliado e indicaciones específicas."
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        resultado = response.choices[0].message.content
        hoja = crear_hoja_del_dia(pacientes[telefono]["dia"])
        hoja.append_row([
            "Orden médica",
            pacientes[telefono]["nombre"],
            telefono,
            pacientes[telefono].get("direccion", ""),
            pacientes[telefono].get("localidad", ""),
            pacientes[telefono].get("fecha_nacimiento", ""),
            pacientes[telefono].get("cobertura", ""),
            pacientes[telefono].get("afiliado", ""),
            texto_ocr,
            resultado
        ])
        pacientes[telefono]["texto_ocr"] = texto_ocr
        pacientes[telefono]["estado"] = "completo"
        return responder_whatsapp(f"Gracias. Estas son tus indicaciones:\n\n{resultado}\n\n¡Te esperamos!")

    # Fallback GPT especializado
    else:
        nombre = pacientes[telefono].get("nombre", "Paciente")
        fecha_nacimiento = pacientes[telefono].get("fecha_nacimiento", "")
        edad = calcular_edad(fecha_nacimiento) if fecha_nacimiento else "desconocida"
        texto_ocr = pacientes[telefono].get("texto_ocr", "")

        prompt_fallback = (
            f"Paciente: {nombre}, Edad: {edad}\n"
            f"Texto extraído de orden médica: {texto_ocr}\n"
            f"Pregunta del paciente: {mensaje}\n"
            f"Respondé SOLAMENTE si el paciente necesita hacer ayuno (y cuántas horas) y si debe recolectar orina. "
            f"No respondas otros temas, si no se puede determinar decí que debe consultar a su médico."
        )
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt_fallback}]
        )
        texto = response.choices[0].message.content.strip()
        return responder_whatsapp(texto)

if __name__ == "__main__":
    app.run()
