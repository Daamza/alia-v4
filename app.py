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

# --- Funciones auxiliares ---

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

def derivar_a_operador(telefono):
    datos = pacientes.get(telefono, {})
    payload = {
        "nombre": datos.get("nombre", "No disponible"),
        "direccion": datos.get("direccion", "No disponible"),
        "localidad": datos.get("localidad", "No disponible"),
        "fecha_nacimiento": datos.get("fecha_nacimiento", "No disponible"),
        "cobertura": datos.get("cobertura", "No disponible"),
        "afiliado": datos.get("afiliado", "No disponible"),
        "telefono_paciente": telefono,
        "tipo_atencion": datos.get("tipo_atencion", "Consulta"),
        "imagen_base64": datos.get("imagen_base64", "")
    }
    try:
        requests.post("https://derivador-service.onrender.com/derivar", json=payload, timeout=10)
    except Exception as e:
        print(f"Error al derivar: {e}")

# --- Webhook principal ---

@app.route("/webhook", methods=["POST"])
def webhook():
    mensaje = request.form.get("Body", "").lower()
    telefono = request.form.get("From", "")
    
    if telefono not in pacientes:
        pacientes[telefono] = {"estado": "inicio"}

    if "asistente" in mensaje:
        derivar_a_operador(telefono)
        return responder_whatsapp("Te estamos derivando con un operador, serás contactado en breve.")

    estado = pacientes[telefono].get("estado")

    if estado == "inicio" and "hola" in mensaje:
        pacientes[telefono]["estado"] = "esperando_opcion"
        return responder_whatsapp("Hola!!! Soy ALIA.\n¿Sobre qué querés consultar hoy?\n- SEDE\n- DOMICILIO\n- INFORMES")

    if estado == "esperando_opcion":
        if "sede" in mensaje:
            pacientes[telefono].update({
                "nombre": "Paciente sede",
                "direccion": "",
                "localidad": "sede",
                "fecha_nacimiento": "",
                "cobertura": "Desconocida",
                "afiliado": "No aplica",
                "estado": "esperando_orden",
                "tipo_atencion": "SEDE",
                "dia": datetime.today().strftime("%A")
            })
            crear_hoja_del_dia(pacientes[telefono]["dia"]).append_row([
                str(datetime.now()), "Paciente sede", telefono, "", "sede", "", "Desconocida", "No aplica", "", "Pendiente"])
            return responder_whatsapp("Perfecto. Envianos una foto o PDF de la orden médica.")

        elif "domicilio" in mensaje:
            pacientes[telefono].update({"estado": "esperando_datos", "tipo_atencion": "DOMICILIO"})
            return responder_whatsapp("Por favor, enviá: Nombre, Dirección, Localidad, Fecha nacimiento, Cobertura, Afiliado (separado por comas).")

        elif "informes" in mensaje:
            pacientes[telefono]["tipo_atencion"] = "INFORMES"
            derivar_a_operador(telefono)
            return responder_whatsapp("Te estamos derivando con un operador, serás contactado en breve.")

        return responder_whatsapp("No entendí. Escribí SEDE, DOMICILIO o INFORMES.")

    if estado == "esperando_datos":
        partes = mensaje.split(",")
        if len(partes) >= 6:
            pacientes[telefono].update({
                "nombre": partes[0].strip().title(),
                "direccion": partes[1].strip(),
                "localidad": partes[2].strip(),
                "fecha_nacimiento": partes[3].strip(),
                "cobertura": partes[4].strip(),
                "afiliado": partes[5].strip(),
                "estado": "esperando_orden",
                "dia": datetime.today().strftime("%A")
            })
            crear_hoja_del_dia(pacientes[telefono]["dia"]).append_row([
                str(datetime.now()), *partes[:6], "", "Pendiente"])
            return responder_whatsapp("Gracias. Agendamos tu turno. Enviá una foto o PDF de la orden médica.")
        return responder_whatsapp("Faltan datos. Reenviá todo separado por comas.")

    if pacientes[telefono].get("estado") == "esperando_orden" and request.form.get("NumMedia") == "1":
        try:
            media_url = request.form.get("MediaUrl0")
            img = requests.get(media_url, auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")), timeout=10)
            imagen_b64 = base64.b64encode(img.content).decode()
            pacientes[telefono]["imagen_base64"] = imagen_b64

            ocr = requests.post("https://ocr-microsistema.onrender.com/ocr", json={"image_base64": imagen_b64}, timeout=20)
            texto_ocr = ocr.json().get("text", "") if ocr.ok else ""
            if not texto_ocr:
                return responder_whatsapp("No pudimos procesar tu orden. Escribí ASISTENTE para que te ayudemos.")

            prompt = f"Paciente: {pacientes[telefono]['nombre']}, Edad: {calcular_edad(pacientes[telefono]['fecha_nacimiento'])}\n\n{texto_ocr}\n\nExtraé: estudios, cobertura, número de afiliado e indicaciones."
            gpt = openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}]
            )
            resultado = gpt.choices[0].message.content
            pacientes[telefono]["estado"] = "completo"
            pacientes[telefono]["texto_ocr"] = texto_ocr

            crear_hoja_del_dia(pacientes[telefono]["dia"]).append_row([
                "Orden médica", pacientes[telefono]["nombre"], telefono,
                pacientes[telefono]["direccion"], pacientes[telefono]["localidad"],
                pacientes[telefono]["fecha_nacimiento"], pacientes[telefono]["cobertura"],
                pacientes[telefono]["afiliado"], texto_ocr, resultado
            ])
            return responder_whatsapp(f"Gracias. Estas son tus indicaciones:\n\n{resultado}\n\n¡Te esperamos!")

        except requests.exceptions.Timeout:
            return responder_whatsapp("El sistema está tardando en responder. Escribí ASISTENTE para derivarte.")
        except Exception as e:
            print("ERROR OCR/GPT:", e)
            return responder_whatsapp("Tuvimos un inconveniente. Escribí ASISTENTE para derivarte.")

    # Fallback GPT
    nombre = pacientes[telefono].get("nombre", "Paciente")
    edad = calcular_edad(pacientes[telefono].get("fecha_nacimiento", "")) or "desconocida"
    texto_ocr = pacientes[telefono].get("texto_ocr", "")
    prompt = (
        f"Paciente: {nombre}, Edad: {edad}\nTexto OCR: {texto_ocr}\n\n"
        f"Consulta: {mensaje}\n"
        "Indicá sólo si necesita ayuno (cuántas horas) o recolección de orina."
        " Si no se puede determinar, decí: 'Escribí ASISTENTE para que te ayudemos'."
    )
    try:
        r = openai.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": prompt}])
        return responder_whatsapp(r.choices[0].message.content.strip())
    except:
        return responder_whatsapp("No pude entender tu consulta. Escribí ASISTENTE para ser derivado.")

if __name__ == "__main__":
    app.run()
