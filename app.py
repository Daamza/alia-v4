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
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDS_B64")
OCR_SERVICE_URL       = os.getenv("OCR_SERVICE_URL", "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL = os.getenv("DERIVADOR_SERVICE_URL", "https://derivador-service.onrender.com/derivar")

# --- Inicialización de clientes ----------------------------------------------
openai.api_key = OPENAI_API_KEY
r = redis.from_url(REDIS_URL, decode_responses=True)
app = Flask(__name__, static_folder='static')

# -------------------------------------------------------------------------------
# Funciones de sesión
# -------------------------------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {
        'estado': None,
        'tipo_atencion': None,
        'nombre': None,
        'direccion': None,
        'localidad': None,
        'fecha_nacimiento': None,
        'cobertura': None,
        'afiliado': None,
        'estudios': None,
        'imagen_base64': None,
        'dni': None
    }
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# -------------------------------------------------------------------------------
# Utilidades generales
# -------------------------------------------------------------------------------
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

def siguiente_campo_faltante(paciente):
    campos = [
        ('nombre',           "Por favor indícanos tu nombre completo:"),
        ('direccion',        "Ahora indícanos tu domicilio (calle y altura):"),
        ('localidad',        "¿En qué localidad vivís?"),
        ('fecha_nacimiento', "Por favor indícanos tu fecha de nacimiento (dd/mm/aaaa):"),
        ('cobertura',        "¿Cuál es tu cobertura médica?"),
        ('afiliado',         "¿Cuál es tu número de afiliado?")
    ]
    for campo, pregunta in campos:
        if not paciente.get(campo):
            paciente['estado'] = f'esperando_{campo}'
            return pregunta
    return None

def determinar_dia_turno(localidad):
    loc = (localidad or "").lower()
    wd  = datetime.today().weekday()
    if 'ituzaingo' in loc: return 'Lunes'
    if 'merlo' in loc or 'padua' in loc: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc: return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc: return 'Jueves'
    return 'Lunes'

def determinar_sede(localidad):
    loc = (localidad or "").lower()
    if loc in ['castelar','ituzaingo','moron']:
        return 'CASTELAR', 'Arias 2530'
    if loc in ['merlo','padua','paso del rey']:
        return 'MERLO', 'Jujuy 847'
    if loc in ['tesei','hurlingham']:
        return 'TESEI', 'Concepción Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

# -------------------------------------------------------------------------------
# Envío de WhatsApp (Cloud API)
# -------------------------------------------------------------------------------
def enviar_mensaje_whatsapp(to_number, body_text):
    url = f"https://graph.facebook.com/v16.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body_text}
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=5)
        if not resp.ok:
            print("Error WhatsApp:", resp.status_code, resp.text)
    except Exception as e:
        print("Exception WhatsApp:", e)

# -------------------------------------------------------------------------------
# Derivación a operador externa
# -------------------------------------------------------------------------------
def derivar_a_operador(payload):
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error derivando a operador:", e)

# -------------------------------------------------------------------------------
# Lógica central de ALIA (texto e imagen)
# -------------------------------------------------------------------------------
def procesar_mensaje_alia(from_number: str, tipo: str, contenido: str) -> str:
    paciente = get_paciente(from_number)
    estado = paciente.get("estado") or ""

    # 1) Flujo esperando orden médica
    if estado == "esperando_orden":
        if tipo == "image":
            return procesar_mensaje_alia(from_number, "image", contenido)
        txt = contenido.strip().lower()
        if txt in ("no", "no tengo orden"):
            paciente["estado"] = "esperando_estudios_manual"
            save_paciente(from_number, paciente)
            return "Ok, continuamos sin orden médica.\nPor favor, escribe los estudios solicitados:"
        return "Por favor envía la foto de tu orden médica o responde 'no' para continuar sin orden."

    # 2) Sub-flujo MANUAL de estudios
    if estado == "esperando_estudios_manual" and tipo == "text":
        estudios_raw = contenido.strip()
        paciente["estudios"] = [e.strip() for e in estudios_raw.split(",")]
        paciente["estado"] = "esperando_estudios_confirmacion"
        save_paciente(from_number, paciente)

        estudios_str = ", ".join(paciente["estudios"])
        return f"Hemos recibido estos estudios: {estudios_str}.\n¿Los confirmas? (sí/no)"

    # 3) Confirmación de estudios manuales
    if estado == "esperando_estudios_confirmacion" and tipo == "text":
        txt = contenido.strip().lower()
        if txt in ("sí", "si", "s"):
            estudios_list = paciente["estudios"]
            prompt = (
                f"Estos son los estudios solicitados: {', '.join(estudios_list)}.\n"
                "Solo indica si requieren ayuno de 8 u 12 horas, y si requieren recolección de orina."
            )
            try:
                resp = openai.ChatCompletion.create(
                    model="gpt-4",
                    messages=[{"role":"user","content":prompt}]
                )
                instrucciones = resp.choices[0].message.content.strip()
            except:
                instrucciones = "No pude obtener indicaciones específicas. Por favor, consulta al laboratorio."

            if paciente.get("tipo_atencion") == "SEDE":
                sede, dir_sede = determinar_sede(paciente["localidad"])
                final = (
                    f"El pre-ingreso se realizó correctamente.\n"
                    f"Te esperamos en la sede {sede} ({dir_sede}) de 07:40 a 11:00.\n"
                    "Las prácticas quedan sujetas a autorización del prestador."
                )
            else:
                dia = determinar_dia_turno(paciente["localidad"])
                final = (
                    f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00.\n"
                    "Las prácticas quedan sujetas a autorización del prestador."
                )

            clear_paciente(from_number)
            return f"{instrucciones}\n\n{final}"
        else:
            paciente["estado"] = "esperando_estudios_manual"
            save_paciente(from_number, paciente)
            return "Entendido. Por favor, vuelve a escribir los estudios solicitados:"

    # 4) Procesamiento de texto genérico
    if tipo == "text":
        texto = contenido.strip()
        lower = texto.lower()

        if "reiniciar" in lower:
            clear_paciente(from_number)
            return "Flujo reiniciado. ¿En qué puedo ayudarte hoy?"

        if paciente["estado"] is None and any(k in lower for k in ["hola","buenas"]):
            paciente["estado"] = "menu"
            save_paciente(from_number, paciente)
            return (
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción:\n"
                "1. Pedir un turno\n"
                "2. Solicitar envío de resultados\n"
                "3. Contactar con un operador"
            )

        if estado == "menu":
            if texto == "1" or "turno" in lower:
                paciente["estado"] = "menu_turno"
                save_paciente(from_number, paciente)
                return "¿Dónde prefieres el turno?\n1. Sede\n2. Domicilio"
            if texto == "2" or "resultado" in lower:
                paciente["estado"] = "esperando_resultados_nombre"
                save_paciente(from_number, paciente)
                return "Para enviarte resultados, indícanos tu nombre completo:"
            if texto == "3" or any(k in lower for k in ["operador","ayuda","asistente"]):
                clear_paciente(from_number)
                # dérivo payload si quieres aquí...
                return "Te derivo a un operador. En breve te contactarán."
            return "Opción no válida. Elige 1, 2 o 3."

        if estado == "menu_turno":
            if texto == "1" or "sede" in lower:
                paciente["tipo_atencion"] = "SEDE"
            elif texto == "2" or "domicilio" in lower:
                paciente["tipo_atencion"] = "DOMICILIO"
            else:
                return "Por favor elige 1 o 2."
            pregunta = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            return pregunta

        # -- Flujo resultados corregido --
        if estado.startswith("esperando_resultados_"):
            campo = estado[len("esperando_resultados_"):]
            if campo == "nombre":
                paciente["nombre"] = texto.title()
                paciente["estado"] = "esperando_resultados_dni"
                save_paciente(from_number, paciente)
                return "Ahora indícanos tu número de documento:"
            if campo == "dni":
                paciente["dni"] = texto.strip()
                paciente["estado"] = "esperando_resultados_localidad"
                save_paciente(from_number, paciente)
                return "Finalmente, indícanos tu localidad:"
            if campo == "localidad":
                paciente["localidad"] = texto.title()
                clear_paciente(from_number)
                return (
                    f"Solicitamos envío de resultados para {paciente['nombre']} "
                    f"({paciente['dni']}) en {paciente['localidad']}."
                )

        # Flujo datos secuenciales para turno
        if estado.startswith("esperando_") and not estado.startswith("esperando_resultados_"):
            campo = estado.split("_",1)[1]
            paciente[campo] = texto.title() if campo in ["nombre","localidad"] else texto
            siguiente = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            if siguiente:
                return siguiente
            paciente["estado"] = "esperando_orden"
            save_paciente(from_number, paciente)
            return "Envía foto de tu orden médica o responde 'no' para continuar sin orden."

        # Fallback GPT
        prompt = (
            f"Paciente: {paciente.get('nombre','')} "
            f"(Edad {calcular_edad(paciente.get('fecha_nacimiento','')) or 'desconocida'})\n"
            f"Pregunta: {texto}\nResponde sólo si debe realizar ayuno de 8 u 12 horas, y si debe recolectar orina."
        )
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}]
            )
            return resp.choices[0].message.content.strip()
        except:
            return "No entendí tu consulta, ¿podrías reformularla?"

    # 5) Procesamiento de imagen (OCR + GPT)
    if tipo == "image":
        try:
            ocr_resp = requests.post(
                OCR_SERVICE_URL,
                json={'image_base64': contenido},
                timeout=10
            )
            ocr_resp.raise_for_status()
            texto_ocr = ocr_resp.json().get("text","").strip()
            if not texto_ocr:
                raise ValueError("OCR vacío")
        except:
            return "No pudimos procesar tu orden médica."

        prompt = (
            "Analiza esta orden médica y devuelve un JSON con las claves:\n"
            "estudios, cobertura, afiliado.\n\n" + texto_ocr
        )
        try:
            gpt = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}],
                temperature=0.0
            )
            datos = json.loads(gpt.choices[0].message.content.strip())
        except:
            return "Error interpretando tu orden médica."

        paciente.update({
            "estudios":      datos.get("estudios"),
            "cobertura":     datos.get("cobertura"),
            "afiliado":      datos.get("afiliado"),
            "imagen_base64": contenido
        })
        save_paciente(from_number, paciente)

        estudios_list = paciente["estudios"] or []
        estudios_str = ", ".join(estudios_list) if isinstance(estudios_list, list) else estudios_list
        paciente["estado"] = "esperando_estudios_confirmacion"
        save_paciente(from_number, paciente)
        return f"Hemos detectado estos estudios: {estudios_str}.\n¿Los confirmas? (sí/no)"

    return "No pude procesar tu mensaje."

# -------------------------------------------------------------------------------
# Webhook WhatsApp (verificación y eventos)
# -------------------------------------------------------------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook_whatsapp():
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("Forbidden", status=403)

    data = request.get_json(force=True)
    if data.get("object","").lower() != "whatsapp_business_account":
        return Response("No event", status=200)

    entry   = data["entry"][0]
    msg     = entry["changes"][0]["value"]["messages"][0]
    from_nr = msg["from"]
    tipo    = msg["type"]

    if tipo == "text":
        txt  = msg["text"]["body"]
        rply = procesar_mensaje_alia(from_nr, "text", txt)
        enviar_mensaje_whatsapp(from_nr, rply)

    elif tipo == "image":
        mid  = msg["image"]["id"]
        meta = requests.get(
            f"https://graph.facebook.com/v16.0/{mid}",
            params={"access_token": META_ACCESS_TOKEN}, timeout=5
        ).json()
        url  = meta.get("url")
        img  = requests.get(url, timeout=10).content
        b64  = base64.b64encode(img).decode()
        rply = procesar_mensaje_alia(from_nr, "image", b64)
        enviar_mensaje_whatsapp(from_nr, rply)

    return Response("OK", status=200)

# -------------------------------------------------------------------------------
# Widget & página de ejemplo
# -------------------------------------------------------------------------------
@app.route("/widget.js")
def serve_widget():
    return send_from_directory(app.static_folder, "widget.js")

@app.route("/", methods=["GET"])
def serve_index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/chat", methods=["GET"])
def serve_chat():
    return send_from_directory(app.static_folder, "chat.html")

@app.route("/chat", methods=["POST"])
def api_chat():
    data    = request.get_json(force=True)
    session = data.get("session", "demo")
    if "image" in data and (data["image"].startswith("iVBOR") or data["image"].startswith("/9j/")):
        reply = procesar_mensaje_alia(session, "image", data["image"])
    else:
        msg   = data.get("message","").strip()
        reply = procesar_mensaje_alia(session, "text", msg)
    return jsonify({"reply": reply})

# -------------------------------------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
