import os
import json
import base64
import requests
import redis
from datetime import datetime
from flask import Flask, request, Response
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuración de entorno ------------------------------------------------
# Define estas variables en Render (o en tu servidor) antes de desplegar:
META_VERIFY_TOKEN       = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN       = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID    = os.getenv("META_PHONE_NUMBER_ID")    # ej. "656903770841867"
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY")
REDIS_URL               = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64        = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OCR_SERVICE_URL         = os.getenv("OCR_SERVICE_URL",    "https://ocr-microsistema.onrender.com/ocr")
DERIVADOR_SERVICE_URL   = os.getenv("DERIVADOR_SERVICE_URL","https://derivador-service-onrender.com/derivar")

# Inicializo OpenAI
openai.api_key = OPENAI_API_KEY

# Redis para sesiones
r = redis.from_url(REDIS_URL, decode_responses=True)

# Flask app
app = Flask(__name__)

# -------------------------------------------------------------------------------
# Funciones de sesión
# -------------------------------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {
        'estado': None,
        'ocr_fallos': 0,
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
# Función para calcular edad
# -------------------------------------------------------------------------------
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

# -------------------------------------------------------------------------------
# Google Sheets / Drive
# -------------------------------------------------------------------------------
def crear_hoja_del_dia(dia):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_json  = base64.b64decode(GOOGLE_CREDS_B64).decode()
    creds       = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(creds_json), scope)
    client_gs   = gspread.authorize(creds)
    creds_drive = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    drive_svc = build('drive', 'v3', credentials=creds_drive)
    folder_id = None
    try:
        res = drive_svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and name='ALIA_TURNOS' and trashed=false",
            spaces='drive',
            fields='files(id)'
        ).execute()
        items = res.get('files', [])
        if items:
            folder_id = items[0]['id']
        else:
            meta = {'name': 'ALIA_TURNOS', 'mimeType': 'application/vnd.google-apps.folder'}
            folder = drive_svc.files().create(body=meta, fields='id').execute()
            folder_id = folder['id']
    except HttpError as e:
        print("Error Drive:", e)

    nombre = f"Turnos_{dia}"
    try:
        hoja = client_gs.open(nombre).sheet1
    except:
        hoja = client_gs.create(nombre).sheet1
        if folder_id:
            try:
                drive_svc.files().update(
                    fileId=hoja.spreadsheet.id,
                    addParents=folder_id,
                    removeParents='root',
                    fields='id,parents'
                ).execute()
            except HttpError as e:
                print("Error mover hoja:", e)
        hoja.append_row([
            "Fecha", "Nombre", "Teléfono", "Dirección", "Localidad",
            "Fecha de Nacimiento", "Cobertura", "Afiliado", "Estudios", "Indicaciones"
        ])
    return hoja

def determinar_dia_turno(localidad):
    loc = localidad.lower()
    wd  = datetime.today().weekday()
    if 'ituzaingó' in loc: return 'Lunes'
    if 'merlo' in loc or 'padua' in loc: return 'Martes' if wd < 4 else 'Viernes'
    if 'tesei' in loc or 'hurlingham' in loc: return 'Miércoles' if wd < 4 else 'Sábado'
    if 'castelar' in loc: return 'Jueves'
    return 'Lunes'

def determinar_sede(localidad):
    loc = localidad.lower()
    if loc in ['castelar','ituzaingó','moron']:
        return 'CASTELAR', 'Arias 2530'
    if loc in ['merlo','padua','paso del rey']:
        return 'MERLO', 'Jujuy 847'
    if loc in ['tesei','hurlingham']:
        return 'TESEI', 'Concepción Arenal 2694'
    return 'GENERAL', 'Nuestra sede principal'

# -------------------------------------------------------------------------------
# Derivar a operador (llama al microservicio) 
# -------------------------------------------------------------------------------
def derivar_a_operador_meta(payload):
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error al derivar a operador:", e)

# -------------------------------------------------------------------------------
# Función auxiliar: enviar mensaje por Cloud API (WhatsApp)
# -------------------------------------------------------------------------------
def enviar_mensaje_whatsapp(to_number, body_text):
    """
    to_number: string con el teléfono en formato internacional, p.ej. "5491138261717"
    body_text: texto que queremos enviar
    """
    url = f"https://graph.facebook.com/v16.0/{META_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": { "body": body_text }
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=5)
        if not resp.ok:
            print("Error enviando WhatsApp:", resp.status_code, resp.text)
    except Exception as e:
        print("Exception enviando WhatsApp:", e)

# -------------------------------------------------------------------------------
# Funciones auxiliares para flujo de orden
# -------------------------------------------------------------------------------
def siguiente_campo_faltante(paciente):
    orden = [
        ('nombre',           "Por favor indícanos tu nombre completo:"),
        ('direccion',        "Ahora indícanos tu domicilio:"),
        ('localidad',        "¿En qué localidad vivís?"),
        ('fecha_nacimiento', "Por favor indícanos tu fecha de nacimiento (dd/mm/aaaa):"),
        ('cobertura',        "¿Cuál es tu cobertura médica?"),
        ('afiliado',         "¿Cuál es tu número de afiliado?"),
        ('estudios',         "Por favor confírmanos los estudios solicitados:")
    ]
    for campo, pregunta in orden:
        if not paciente.get(campo):
            paciente['estado'] = f'esperando_{campo}'
            return pregunta
    return None

# -------------------------------------------------------------------------------
# WEBHOOK para WhatsApp Cloud API
# -------------------------------------------------------------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook_whatsapp():
    # ------------------------------------------
    # 1) Verificación del webhook (GET)
    # ------------------------------------------
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            return Response(challenge, status=200)
        return Response("Forbidden", status=403)

    # ------------------------------------------
    # 2) Procesar mensaje entrante (POST JSON)
    # ------------------------------------------
    data = request.get_json()
    if data.get("object") != "whatsapp_business_account":
        return Response("No event", status=200)

    try:
        entry    = data["entry"][0]
        changes  = entry["changes"][0]
        value    = changes["value"]
        messages = value.get("messages", [])
        if not messages:
            return Response("No messages", status=200)
        msg = messages[0]
    except Exception:
        return Response("Bad request", status=400)

    from_number = msg.get("from")       # ej. "5491138261717"
    tipo        = msg.get("type")       # "text", "image", etc.
    paciente    = get_paciente(from_number)

    # Si es texto y dice "reiniciar", reiniciamos flujo
    if tipo == "text":
        texto = msg["text"]["body"].strip().lower()
        if "reiniciar" in texto:
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Flujo reiniciado. ¿En qué puedo ayudarte hoy?")
            return Response("OK", status=200)

    # ------------------------------------------------------------------------
    # 3) Si es mensaje de texto, seguimos el flujo de texto
    # ------------------------------------------------------------------------
    if tipo == "text":
        texto = msg["text"]["body"].strip()
        msg_lower = texto.lower()

        # (A) Derivar a operador si el usuario lo pide
        if any(k in msg_lower for k in ["asistente", "ayuda", "operador"]) and paciente["estado"] is None:
            payload = {
                'nombre':            paciente.get('nombre','No disponible'),
                'direccion':         paciente.get('direccion','No disponible'),
                'localidad':         paciente.get('localidad','No disponible'),
                'fecha_nacimiento':  paciente.get('fecha_nacimiento','No disponible'),
                'cobertura':         paciente.get('cobertura','No disponible'),
                'afiliado':          paciente.get('afiliado','No disponible'),
                'telefono_paciente': from_number,
                'tipo_atencion':     paciente.get('tipo_atencion','No disponible'),
                'imagen_base64':     paciente.get('imagen_base64','')
            }
            derivar_a_operador_meta(payload)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Te derivamos a un operador. En breve te contactarán.")
            return Response("OK", status=200)

        # (B) Saludo / menú principal
        if paciente["estado"] is None and any(k in msg_lower for k in ["hola", "buenas"]):
            paciente["estado"] = "menu"
            save_paciente(from_number, paciente)
            menu_text = (
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción enviando el número:\n"
                "1. Pedir un turno\n"
                "2. Solicitar envío de resultados\n"
                "3. Contactar con un operador"
            )
            enviar_mensaje_whatsapp(from_number, menu_text)
            return Response("OK", status=200)

        # (C) Flujo cuando estado = 'menu'
        if paciente["estado"] == "menu":
            if texto == "1":
                paciente["estado"] = "menu_turno"
                save_paciente(from_number, paciente)
                sub_menu = (
                    "¿Dónde prefieres el turno? Elige un número:\n"
                    "1. Sede\n"
                    "2. Domicilio"
                )
                enviar_mensaje_whatsapp(from_number, sub_menu)
                return Response("OK", status=200)
            elif texto == "2":
                paciente["estado"] = "esperando_resultados_nombre"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Para enviarte tus resultados, por favor indícanos tu nombre completo:")
                return Response("OK", status=200)
            elif texto == "3":
                payload = {
                    'nombre':            paciente.get('nombre','No disponible'),
                    'direccion':         paciente.get('direccion','No disponible'),
                    'localidad':         paciente.get('localidad','No disponible'),
                    'fecha_nacimiento':  paciente.get('fecha_nacimiento','No disponible'),
                    'cobertura':         paciente.get('cobertura','No disponible'),
                    'afiliado':          paciente.get('afiliado','No disponible'),
                    'telefono_paciente': from_number,
                    'tipo_atencion':     paciente.get('tipo_atencion','No disponible'),
                    'imagen_base64':     paciente.get('imagen_base64','')
                }
                derivar_a_operador_meta(payload)
                clear_paciente(from_number)
                enviar_mensaje_whatsapp(from_number, "Te derivamos a un operador. En breve te contactarán.")
                return Response("OK", status=200)
            else:
                enviar_mensaje_whatsapp(from_number, "Opción no válida. Por favor elige 1, 2 o 3.")
                return Response("OK", status=200)

        # (D) Flujo cuando estado = 'menu_turno'
        if paciente["estado"] == "menu_turno":
            if texto == "1":
                paciente["tipo_atencion"] = "SEDE"
            elif texto == "2":
                paciente["tipo_atencion"] = "DOMICILIO"
            else:
                enviar_mensaje_whatsapp(from_number, "Elige 1 (Sede) o 2 (Domicilio), por favor.")
                return Response("OK", status=200)

            pregunta = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            enviar_mensaje_whatsapp(from_number, pregunta)
            return Response("OK", status=200)

        # (E) Flujo de resultados (estado empieza con 'esperando_resultados_')
        if paciente["estado"] and paciente["estado"].startswith("esperando_resultados_"):
            campo = paciente["estado"].split("_", 1)[1]  # puede ser nombre, dni, localidad

            if campo == "nombre":
                paciente["nombre"] = texto.title()
                paciente["estado"] = "esperando_resultados_dni"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Gracias. Ahora indícanos tu número de documento:")
                return Response("OK", status=200)

            if campo == "dni":
                paciente["dni"] = texto.strip()
                paciente["estado"] = "esperando_resultados_localidad"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Por último, indícanos tu localidad:")
                return Response("OK", status=200)

            if campo == "localidad":
                paciente["localidad"] = texto.title()
                payload = {
                    'nombre':            paciente.get('nombre','No disponible'),
                    'direccion':         paciente.get('direccion','No disponible'),
                    'localidad':         paciente.get('localidad','No disponible'),
                    'fecha_nacimiento':  paciente.get('fecha_nacimiento','No disponible'),
                    'cobertura':         paciente.get('cobertura','No disponible'),
                    'afiliado':          paciente.get('afiliado','No disponible'),
                    'telefono_paciente': from_number,
                    'tipo_atencion':     paciente.get('tipo_atencion','No disponible'),
                    'imagen_base64':     paciente.get('imagen_base64','')
                }
                derivar_a_operador_meta(payload)
                clear_paciente(from_number)
                confirmar = f"Solicitamos el envío de resultados para {paciente['nombre']} ({paciente['dni']}) en {paciente['localidad']}."
                enviar_mensaje_whatsapp(from_number, confirmar)
                return Response("OK", status=200)

        # (F) Flujo de datos secuenciales (turno) – estado = 'esperando_nombre', etc.
        if paciente["estado"] and paciente["estado"].startswith("esperando_") and "resultados" not in paciente["estado"]:
            campo = paciente["estado"].split("_", 1)[1]
            if campo in ["nombre", "localidad"]:
                paciente[campo] = texto.title()
            else:
                paciente[campo] = texto
            siguiente = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            if siguiente:
                enviar_mensaje_whatsapp(from_number, siguiente)
                return Response("OK", status=200)
            else:
                paciente["estado"] = "esperando_orden"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Envía la foto de tu orden médica o responde 'No tengo orden'.")
                return Response("OK", status=200)

        # (G) "No tengo orden"
        if paciente["estado"] == "esperando_orden" and msg_lower == "no tengo orden":
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Ok, continuamos sin orden médica. Te contactaremos si falta información.")
            return Response("OK", status=200)

        # (H) Fallback a GPT para consultas generales
        edad = calcular_edad(paciente.get("fecha_nacimiento", "")) or "desconocida"
        prompt_fb = (
            f"Paciente: {paciente.get('nombre','Paciente')}, Edad: {edad}\n"
            f"Pregunta: {texto}\n"
            "Responde únicamente si debe realizar ayuno (horas) o recolectar orina."
        )
        try:
            fb = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt_fb}]
            )
            respuesta_fb = fb.choices[0].message.content.strip()
            enviar_mensaje_whatsapp(from_number, respuesta_fb)
        except Exception as e:
            print("Error GPT fallback:", e)
            enviar_mensaje_whatsapp(from_number, "Error procesando tu consulta. Intentá más tarde.")
        return Response("OK", status=200)

    # ------------------------------------------------------------------------
    # 4) Si es imagen (orden médica), procesamos
    # ------------------------------------------------------------------------
    if tipo == "image":
        media_id = msg["image"]["id"]

        # 4.2) Obtener URL temporal de descarga
        url_media_meta = f"https://graph.facebook.com/v16.0/{media_id}"
        params_meta = { "access_token": META_ACCESS_TOKEN }
        try:
            resp_meta = requests.get(url_media_meta, params=params_meta, timeout=5)
            resp_meta.raise_for_status()
            media_url = resp_meta.json().get("url")
            if not media_url:
                raise Exception("No vino URL en meta de media.")
        except Exception as e:
            print("Error obteniendo media URL:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "No pudimos procesar tu orden. Te derivamos a un operador.")
            return Response("OK", status=200)

        # 4.3) Descargar la imagen
        try:
            resp_img = requests.get(media_url, timeout=10)
            resp_img.raise_for_status()
            img_bytes = resp_img.content
            b64 = base64.b64encode(img_bytes).decode()
        except Exception as e:
            print("Error descargando imagen:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "No pudimos procesar tu orden. Te derivamos a un operador.")
            return Response("OK", status=200)

        # 4.4) Enviar base64 a OCR_SERVICE_URL
        try:
            ocr_resp = requests.post(
                OCR_SERVICE_URL,
                json={'image_base64': b64},
                timeout=10
            )
            ocr_resp.raise_for_status()
            texto_ocr = ocr_resp.json().get("text", "").strip()
            if not texto_ocr:
                raise Exception("OCR devolvió texto vacío.")
        except Exception as e:
            print("Error OCR:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "No pudimos procesar tu orden. Te derivamos a un operador.")
            return Response("OK", status=200)

        # 4.5) Llamar a OpenAI para extraer JSON
        prompt = (
            "Analiza esta orden médica y devuelve un JSON con las claves:\n"
            "estudios, cobertura, afiliado.\n\n" + texto_ocr
        )
        try:
            pg = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            contenido = pg.choices[0].message.content.strip()
            datos = json.loads(contenido)
        except Exception as e:
            print("Error OpenAI Extraer JSON:", e, "\nContenido recibido:", contenido if 'contenido' in locals() else "")
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number,
                "Lo siento, hubo un error interpretando tu orden. ¿Podrías enviarla de nuevo o responder 'No tengo orden'?"
            )
            return Response("OK", status=200)

        # 4.6) Actualizar paciente
        paciente["estudios"]      = datos.get("estudios")
        paciente["cobertura"]     = datos.get("cobertura")
        paciente["afiliado"]      = datos.get("afiliado")
        paciente["imagen_base64"] = b64
        save_paciente(from_number, paciente)

        # 4.7) Preguntar siguiente campo
        pregunta = siguiente_campo_faltante(paciente)
        if pregunta:
            texto_detectado = json.dumps(datos, ensure_ascii=False)
            mensaje = f"Detectamos estos datos:\n{texto_detectado}\n\n{pregunta}"
            enviar_mensaje_whatsapp(from_number, mensaje)
            return Response("OK", status=200)

        # 4.8) Terminar flujo de orden
        sede, dir_sede = determinar_sede(paciente["localidad"])
        if paciente["tipo_atencion"] == "SEDE":
            mensaje_final = (
                f"El pre-ingreso se realizó correctamente. Te esperamos en la sede {sede} "
                f"({dir_sede}) de 07:40 a 11:00. ¡Muchas gracias!"
            )
        else:
            dia = determinar_dia_turno(paciente["localidad"])
            mensaje_final = (
                f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00. ¡Muchas gracias!"
            )
        clear_paciente(from_number)
        enviar_mensaje_whatsapp(from_number, mensaje_final)
        return Response("OK", status=200)

    # Si no es texto ni imagen, devolvemos OK
    return Response("OK", status=200)

# -------------------------------------------------------------------------------
# Arranque de la aplicación
# -------------------------------------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
