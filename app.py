import os
import json
import base64
import requests
import redis
from datetime import datetime
from flask import Flask, request, Response, send_from_directory, jsonify

import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuración de entorno ------------------------------------------------
META_VERIFY_TOKEN     = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID  = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OCR_SERVICE_URL       = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service.onrender.com/derivar")

# --- Inicialización de clientes ----------------------------------------------
openai.api_key = OPENAI_API_KEY
r = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__, static_folder='static')

# --- Funciones de sesión -----------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {k: None for k in ['estado','tipo_atencion','nombre','direccion','localidad','fecha_nacimiento','cobertura','afiliado','estudios','imagen_base64','dni']}
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Utilidades generales ----------------------------------------------------
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

def siguiente_campo_faltante(p):
    orden = [
        ('nombre','Tu nombre completo:'),
        ('direccion','Tu dirección (calle y altura):'),
        ('localidad','Localidad:'),
        ('fecha_nacimiento','Fecha de nacimiento (dd/mm/aaaa):'),
        ('cobertura','Cobertura médica:'),
        ('afiliado','Número de afiliado:')
    ]
    for campo, pregunta in orden:
        if not p.get(campo):
            p['estado'] = f'esperando_{campo}'
            return pregunta
    return None

def determinar_dia_turno(loc):
    l = (loc or '').lower()
    wd = datetime.today().weekday()
    if 'ituzaingo' in l: return 'Lunes'
    if 'merlo' in l or 'padua' in l: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in l or 'hurlingham' in l: return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in l: return 'Jueves'
    return 'Lunes'

def determinar_sede(loc):
    l = (loc or '').lower()
    if l in ['castelar','ituzaingó','moron']: return 'CASTELAR', 'Arias 2530'
    if l in ['merlo','padua','paso del rey']: return 'MERLO', 'Jujuy 847'
    if l in ['tesei','hurlingham']: return 'TESEI', 'Concepción Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

# --- Envío de WhatsApp -------------------------------------------------------
def enviar_mensaje_whatsapp(to, text):
    url = f"https://graph.facebook.com/v16.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=5)
        if not resp.ok:
            print("Error WhatsApp:", resp.status_code, resp.text)
    except Exception as e:
        print("Excepción WhatsApp:", e)

def derivar_a_operador(payload):
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error al derivar:", e)

# --- Lógica central de ALIA --------------------------------------------------
def procesar_mensaje_alia(tel, tipo, contenido):
    p = get_paciente(tel)

    if p.get("estado") == "esperando_orden":
        if tipo == "image":
            return procesar_mensaje_alia(tel, "image", contenido)
        txt = contenido.strip().lower()
        if txt in ("no", "no tengo orden"):
            p["estado"] = "esperando_estudios_manual"
            save_paciente(tel, p)
            return "Ok, escribí los estudios solicitados:"
        return "Enviá foto de tu orden médica o escribí 'no' si no tenés."

    if p.get("estado") == "esperando_estudios_manual" and tipo == "text":
        prompt = f"Estos son los estudios solicitados:\n{contenido}\n\nDevuelve JSON con clave 'estudios'."
        try:
            gpt = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0
            )
            datos = json.loads(gpt.choices[0].message.content.strip())
            p["estudios"] = datos.get("estudios")
        except:
            p["estudios"] = [x.strip() for x in contenido.split(",")]
        save_paciente(tel, p)

        if p.get("tipo_atencion") == "SEDE":
            sede, dir_sede = determinar_sede(p.get("localidad"))
            msg = f"Pre-ingreso completo. Te esperamos en {sede} ({dir_sede}) de 07:40 a 11:00."
        else:
            dia = determinar_dia_turno(p.get("localidad"))
            msg = f"Tu turno fue asignado para el día {dia}, entre 08:00 y 11:00."
        clear_paciente(tel)
        return msg

    if tipo == "text":
        txt = contenido.strip()
        low = txt.lower()

        if "reiniciar" in low:
            clear_paciente(tel)
            return "Flujo reiniciado. ¿Cómo puedo ayudarte?"

        if p["estado"] is None and any(k in low for k in ["hola","buenas"]):
            p["estado"] = "menu"
            save_paciente(tel, p)
            return "Hola, soy ALIA. ¿Qué querés hacer?\n1. Pedir turno\n2. Resultados\n3. Operador"

        if p.get("estado") == "menu":
            if "1" in low or "turno" in low:
                p["estado"] = "menu_turno"
                save_paciente(tel, p)
                return "¿Dónde querés el turno?\n1. Sede\n2. Domicilio"
            if "2" in low or "resultado" in low:
                p["estado"] = "esperando_resultados_nombre"
                save_paciente(tel, p)
                return "Decime tu nombre completo:"
            if "3" in low or "operador" in low:
                clear_paciente(tel)
                return "Te derivamos a un operador."
            return "Opción no válida. Elegí 1, 2 o 3."

        if p.get("estado") == "menu_turno":
            if "1" in low or "sede" in low:
                p["tipo_atencion"] = "SEDE"
            elif "2" in low or "domicilio" in low:
                p["tipo_atencion"] = "DOMICILIO"
            else:
                return "Por favor elegí 1 o 2."
            pregunta = siguiente_campo_faltante(p)
            save_paciente(tel, p)
            return pregunta

        if p.get("estado", "").startswith("esperando_resultados_"):
            campo = p["estado"].split("_")[2]
            p[campo] = txt
            if campo == "nombre":
                p["estado"] = "esperando_resultados_dni"
                save_paciente(tel, p)
                return "DNI por favor:"
            if campo == "dni":
                p["estado"] = "esperando_resultados_localidad"
                save_paciente(tel, p)
                return "Localidad:"
            if campo == "localidad":
                msg = f"Se solicitó el envío de resultados para {p['nombre']} ({p['dni']}) en {p['localidad']}."
                clear_paciente(tel)
                return msg

        if p.get("estado", "").startswith("esperando_"):
            campo = p["estado"].split("_")[1]
            p[campo] = txt
            siguiente = siguiente_campo_faltante(p)
            save_paciente(tel, p)
            if siguiente:
                return siguiente
            p["estado"] = "esperando_orden"
            save_paciente(tel, p)
            return "¿Tenés una orden médica? Enviá una imagen o escribí 'no'."

        edad = calcular_edad(p.get("fecha_nacimiento") or '') or 'desconocida'
        prompt = f"Paciente: {p.get('nombre','')} (Edad {edad})\nConsulta: {txt}\nResponder si debe hacer ayuno o recolectar orina."
        try:
            gpt = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}]
            )
            return gpt.choices[0].message.content.strip()
        except:
            return "No entendí. ¿Podés reformularlo?"

    if tipo == "image":
        try:
            r_ocr = requests.post(
                OCR_SERVICE_URL,
                json={"image_base64": contenido},
                timeout=10
            )
            r_ocr.raise_for_status()
            texto_ocr = r_ocr.json().get("text","").strip()
        except:
            return "No pudimos leer tu orden médica."
        prompt = (
            "Extraé de esta orden: estudios, cobertura y afiliado. Devolveme un JSON.\n\n"
            + texto_ocr
        )
        try:
            gpt = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}],
                temperature=0
            )
            datos = json.loads(gpt.choices[0].message.content.strip())
        except:
            return "No pude interpretar la orden."
        p.update({
            "estudios": datos.get("estudios"),
            "cobertura": datos.get("cobertura"),
            "afiliado": datos.get("afiliado"),
            "imagen_base64": contenido
        })
        save_paciente(tel, p)
        siguiente = siguiente_campo_faltante(p)
        if siguiente:
            return f"Detectamos:\n{json.dumps(datos, ensure_ascii=False)}\n\n{siguiente}"
        sede, dir_sede = determinar_sede(p["localidad"])
        if p["tipo_atencion"] == "SEDE":
            msg = f"Pre-ingreso completo. Te esperamos en {sede} ({dir_sede}) de 07:40 a 11:00."
        else:
            dia = determinar_dia_turno(p["localidad"])
            msg = f"Tu turno fue asignado para el día {dia}, entre 08:00 y 11:00."
        clear_paciente(tel)
        return msg

    return "No pude procesar tu mensaje."

# --- Endpoints del chat y páginas ---
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session = data.get("session", "demo")
    if "image" in data:
        reply = procesar_mensaje_alia(session, "image", data["image"])
    else:
        reply = procesar_mensaje_alia(session, "text", data.get("message",""))
    return jsonify({"reply": reply})

@app.route("/", methods=["GET"])
def serve_index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/chat", methods=["GET"])
def serve_chat():
    return send_from_directory(app.static_folder, "chat.html")

# --- Arranque del servidor ---
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
