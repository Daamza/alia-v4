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
META_VERIFY_TOKEN       = os.getenv("META_VERIFY_TOKEN")
META_ACCESS_TOKEN       = os.getenv("META_ACCESS_TOKEN")
META_PHONE_NUMBER_ID    = os.getenv("META_PHONE_NUMBER_ID")
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY")
REDIS_URL               = os.getenv("REDIS_URL")
GOOGLE_CREDS_B64        = os.getenv("GOOGLE_CREDENTIALS_BASE64")
OCR_SERVICE_URL         = os.getenv(
    "OCR_SERVICE_URL",
    "https://ocr-microsistema.onrender.com/ocr"
)
DERIVADOR_SERVICE_URL   = os.getenv(
    "DERIVADOR_SERVICE_URL",
    "https://derivador-service.onrender.com/derivar"
)

# --- Inicialización de clientes ----------------------------------------------
# OpenAI
openai.api_key = OPENAI_API_KEY

# Redis
r = redis.from_url(REDIS_URL, decode_responses=True)

# Flask
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
# Derivar a operador
# -------------------------------------------------------------------------------
def derivar_a_operador(payload):
    try:
        requests.post(DERIVADOR_SERVICE_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error al derivar a operador:", e)

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
        "to": to_nunber,
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
# Siguiente campo faltante (turno)
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
# Webhook WhatsApp (GET para verif, POST para mensajes)
# -------------------------------------------------------------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook_whatsapp():
    # --- Verificación inicial (Facebook) ---
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == META_VERIFY_TOKEN:
            print("Webhook verificado correctamente")
            return Response(challenge, status=200)
        print("Webhook verificación fallida:", mode, token)
        return Response("Forbidden", status=403)

    # --- Procesamos evento entrante ---
    data = request.get_json(force=True)
    print("=== NUEVA PETICIÓN AL WEBHOOK ===\n", json.dumps(data, indent=2, ensure_ascii=False))
    if data.get("object") != "WHATSAPP_BUSINESS_ACCOUNT":
        return Response("No event", status=200)

    entry    = data["entry"][0]
    changes  = entry["changes"][0]
    value    = changes["value"]
    messages = value.get("messages", [])
    if not messages:
        return Response("No messages", status=200)
    msg = messages[0]

    from_number = msg.get("from")
    tipo        = msg.get("type")
    paciente    = get_paciente(from_number)

    # --- Texto simple ---
    if tipo == "text":
        texto = msg["text"]["body"].strip()
        lower = texto.lower()

        # Reiniciar
        if "reiniciar" in lower:
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Flujo reiniciado. ¿En qué puedo ayudarte hoy?")
            return Response("OK", status=200)

        # Derivar a operador
        if any(k in lower for k in ["asistente","ayuda","operador"]) and paciente["estado"] is None:
            derivar_a_operador({
                'nombre': paciente.get('nombre','-'),
                'direccion': paciente.get('direccion','-'),
                'localidad': paciente.get('localidad','-'),
                'fecha_nacimiento': paciente.get('fecha_nacimiento','-'),
                'cobertura': paciente.get('cobertura','-'),
                'afiliado': paciente.get('afiliado','-'),
                'telefono_paciente': from_number,
                'tipo_atencion': paciente.get('tipo_atencion','-'),
                'imagen_base64': paciente.get('imagen_base64','')
            })
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Te derivamos a un operador. En breve te contactarán.")
            return Response("OK", status=200)

        # Saludo / menú
        if paciente["estado"] is None and any(k in lower for k in ["hola","buenas"]):
            paciente["estado"] = "menu"
            save_paciente(from_number, paciente)
            enviar_mensaje_whatsapp(from_number,
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción enviando el número:\n"
                "1. Pedir un turno\n"
                "2. Solicitar envío de resultados\n"
                "3. Contactar con un operador"
            )
            return Response("OK", status=200)

        # Menú principal
        if paciente["estado"] == "menu":
            if texto == "1":
                paciente["estado"] = "menu_turno"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number,
                    "¿Dónde prefieres el turno? Elige un número:\n1. Sede\n2. Domicilio"
                )
            elif texto == "2":
                paciente["estado"] = "esperando_resultados_nombre"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number,
                    "Para enviarte tus resultados, por favor indícanos tu nombre completo:"
                )
            elif texto == "3":
                derivar_a_operador({
                    'nombre': paciente.get('nombre','-'),
                    'direccion': paciente.get('direccion','-'),
                    'localidad': paciente.get('localidad','-'),
                    'fecha_nacimiento': paciente.get('fecha_nacimiento','-'),
                    'cobertura': paciente.get('cobertura','-'),
                    'afiliado': paciente.get('afiliado','-'),
                    'telefono_paciente': from_number,
                    'tipo_atencion': paciente.get('tipo_atencion','-'),
                    'imagen_base64': paciente.get('imagen_base64','')
                })
                clear_paciente(from_number)
                enviar_mensaje_whatsapp(from_number, "Te derivamos a un operador. En breve te contactarán.")
            else:
                enviar_mensaje_whatsapp(from_number, "Opción no válida. Elige 1, 2 o 3.")
            return Response("OK", status=200)

        # Sub-menú turno
        if paciente["estado"] == "menu_turno":
            if texto == "1":
                paciente["tipo_atencion"] = "SEDE"
            elif texto == "2":
                paciente["tipo_atencion"] = "DOMICILIO"
            else:
                enviar_mensaje_whatsapp(from_number, "Elige 1 (Sede) o 2 (Domicilio).")
                return Response("OK", status=200)
            pregunta = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            enviar_mensaje_whatsapp(from_number, pregunta)
            return Response("OK", status=200)

        # Flujo resultados
        if paciente["estado"] and paciente["estado"].startswith("esperando_resultados_"):
            campo = paciente["estado"].split("_",1)[1]
            if campo == "nombre":
                paciente["nombre"] = texto.title()
                paciente["estado"] = "esperando_resultados_dni"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Ahora indícanos tu número de documento:")
            elif campo == "dni":
                paciente["dni"] = texto.strip()
                paciente["estado"] = "esperando_resultados_localidad"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Por último, indícanos tu localidad:")
            elif campo == "localidad":
                paciente["localidad"] = texto.title()
                derivar_a_operador({
                    'nombre': paciente.get('nombre','-'),
                    'direccion': paciente.get('direccion','-'),
                    'localidad': paciente.get('localidad','-'),
                    'fecha_nacimiento': paciente.get('fecha_nacimiento','-'),
                    'cobertura': paciente.get('cobertura','-'),
                    'afiliado': paciente.get('afiliado','-'),
                    'telefono_paciente': from_number,
                    'tipo_atencion': paciente.get('tipo_atencion','-'),
                    'imagen_base64': paciente.get('imagen_base64','')
                })
                clear_paciente(from_number)
                enviar_mensaje_whatsapp(from_number,
                    f"Solicitamos el envío de resultados para {paciente['nombre']} ({paciente['dni']}) en {paciente['localidad']}."
                )
            return Response("OK", status=200)

        # Flujo turno secuencial
        if paciente["estado"] and paciente["estado"].startswith("esperando_") and "resultados" not in paciente["estado"]:
            campo = paciente["estado"].split("_",1)[1]
            paciente[campo] = texto.title() if campo in ["nombre","localidad"] else texto
            siguiente = siguiente_campo_faltante(paciente)
            save_paciente(from_number, paciente)
            if siguiente:
                enviar_mensaje_whatsapp(from_number, siguiente)
            else:
                paciente["estado"] = "esperando_orden"
                save_paciente(from_number, paciente)
                enviar_mensaje_whatsapp(from_number, "Envía la foto de tu orden médica o responde 'No tengo orden'.")
            return Response("OK", status=200)

        # No tengo orden
        if paciente["estado"] == "esperando_orden" and lower == "no tengo orden":
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Ok, continuamos sin orden médica.")
            return Response("OK", status=200)

        # Fallback GPT
        edad = calcular_edad(paciente.get("fecha_nacimiento","")) or "desconocida"
        prompt_fb = (
            f"Paciente: {paciente.get('nombre','')} (Edad {edad})\n"
            f"Pregunta: {texto}\n"
            "Responde sólo si debe realizar ayuno (horas) o recolectar orina."
        )
        try:
            fb = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt_fb}]
            )
            enviar_mensaje_whatsapp(from_number, fb.choices[0].message.content.strip())
        except Exception as e:
            print("Error GPT fallback:", e)
            enviar_mensaje_whatsapp(from_number, "Error procesando tu consulta.")
        return Response("OK", status=200)

    # --- Imagen (orden médica) ---
    if tipo == "image":
        media_id = msg["image"]["id"]
        # 1) obtener URL temporal
        try:
            meta = requests.get(
                f"https://graph.facebook.com/v16.0/{media_id}",
                params={"access_token": META_ACCESS_TOKEN},
                timeout=5
            ).json()
            media_url = meta.get("url")
            if not media_url:
                raise Exception("Sin URL en media meta")
        except Exception as e:
            print("Error meta media URL:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "No pudimos procesar tu orden.")
            return Response("OK", status=200)
        # 2) descargar imagen
        try:
            img = requests.get(media_url, timeout=10).content
            b64 = base64.b64encode(img).decode()
        except Exception as e:
            print("Error bajando imagen:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "No pudimos procesar tu orden.")
            return Response("OK", status=200)
        # 3) OCR
        try:
            ocr = requests.post(
                OCR_SERVICE_URL,
                json={'image_base64': b64},
                timeout=10
            ).json().get("text","").strip()
            if not ocr:
                raise Exception("OCR vacío")
        except Exception as e:
            print("Error OCR:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "No pudimos procesar tu orden.")
            return Response("OK", status=200)
        # 4) OpenAI extrae JSON
        prompt = f"Analiza esta orden médica y devuelve un JSON con claves estudios, cobertura, afiliado.\n\n{ocr}"
        try:
            res = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt}],
                temperature=0.0
            )
            datos = json.loads(res.choices[0].message.content.strip())
        except Exception as e:
            print("Error OpenAI JSON:", e)
            clear_paciente(from_number)
            enviar_mensaje_whatsapp(from_number, "Error interpretando tu orden.")
            return Response("OK", status=200)
        # 5) guardo datos y pregunto lo que falte
        paciente.update({
            "estudios": datos.get("estudios"),
            "cobertura": datos.get("cobertura"),
            "afiliado": datos.get("afiliado"),
            "imagen_base64": b64
        })
        save_paciente(from_number, paciente)
        siguiente = siguiente_campo_faltante(paciente)
        if siguiente:
            enviar_mensaje_whatsapp(
                from_number,
                f"Detectamos:\n{json.dumps(datos, ensure_ascii=False)}\n\n{siguiente}"
            )
            return Response("OK", status=200)
        # 6) terminar flujo
        sede, dir_sede = determinar_sede(paciente["localidad"])
        if paciente["tipo_atencion"] == "SEDE":
            texto_fin = f"Pre-ingreso OK. Te esperamos en {sede} ({dir_sede}) de 07:40 a 11:00."
        else:
            dia = determinar_dia_turno(paciente["localidad"])
            texto_fin = f"Turno para {dia}, te visitaremos de 08:00 a 11:00."
        clear_paciente(from_number)
        enviar_mensaje_whatsapp(from_number, texto_fin)
        return Response("OK", status=200)

    # resto de tipos: OK
    return Response("OK", status=200)

# -------------------------------------------------------------------------------
if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=puerto)
