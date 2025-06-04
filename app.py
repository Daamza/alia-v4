# app.py

from flask import Flask, request, Response
import requests
import os
import json
from datetime import datetime
import redis
import base64
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

app = Flask(__name__)

# ————— Variables de entorno —————
META_ACCESS_TOKEN       = os.getenv("META_ACCESS_TOKEN")      # El token de Chat API (EAAX…)
META_PHONE_NUMBER_ID    = os.getenv("META_PHONE_NUMBER_ID")   # ID de tu número de prueba (656903770841867)
META_VERIFY_TOKEN       = os.getenv("META_VERIFY_TOKEN")      # p.ej. "ALIAV42025"
REDIS_URL               = os.getenv("REDIS_URL")              # tu URL de Redis en Render
GOOGLE_CREDS_B64        = os.getenv("GOOGLE_CREDENTIALS_BASE64")  # tu JSON de Google Sheets en base64
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY")         # tu API Key de OpenAI (sk-…)

# Cliente Redis para almacenar estados de sesión
r = redis.from_url(REDIS_URL, decode_responses=True)

# Cliente OpenAI
from openai import OpenAI
client_openai = OpenAI(api_key=OPENAI_API_KEY)


# ————— Funciones de sesión en Redis —————
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
        'imagen_base64': None
    }
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# ————— Funciones auxiliares —————
def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

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

    # Busca (o crea) la carpeta ALIA_TURNOS
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

    nombre_hoja = f"Turnos_{dia}"
    try:
        hoja = client_gs.open(nombre_hoja).sheet1
    except:
        hoja = client_gs.create(nombre_hoja).sheet1
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

def derivar_a_operador(tel):
    info = get_paciente(tel)
    payload = {
        'nombre':            info.get('nombre','No disponible'),
        'direccion':         info.get('direccion','No disponible'),
        'localidad':         info.get('localidad','No disponible'),
        'fecha_nacimiento':  info.get('fecha_nacimiento','No disponible'),
        'cobertura':         info.get('cobertura','No disponible'),
        'afiliado':          info.get('afiliado','No disponible'),
        'telefono_paciente': tel,
        'tipo_atencion':     info.get('tipo_atencion','No disponible'),
        'imagen_base64':     info.get('imagen_base64','')
    }
    # Llamada al derivador (servicio externo)
    try:
        requests.post("https://derivador-service.onrender.com/derivar", json=payload, timeout=5)
    except Exception as e:
        print("Error derivar:", e)

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

# ————— Función para enviar mensajes por Cloud API —————
def enviar_mensaje_whatsapp(to_number, mensaje_texto):
    """
    Envía un mensaje de texto simple por WhatsApp Cloud API a `to_number`.
    `to_number` debe incluir prefijo internacional sin signos: p.ej. "5491138261717".
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
        "text": {
            "body": mensaje_texto
        }
    }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=5)
        if not resp.ok:
            print("Error enviando WhatsApp:", resp.status_code, resp.text)
        return resp.status_code, resp.text
    except Exception as e:
        print("Excepción enviando WhatsApp:", e)
        return None, str(e)


# ————— Rutas de Flask —————
@app.route('/webhook', methods=['GET', 'POST'])
def webhook_whatsapp():
    # ——— 1) Webhook verification (GET) ———
    if request.method == 'GET':
        mode      = request.args.get("hub.mode")
        verify_tk = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and verify_tk == META_VERIFY_TOKEN:
            # Respondemos el challenge correctamente (200 + texto plano)
            return Response(challenge, status=200, mimetype='text/plain')
        else:
            return Response("Forbidden", status=403)

    # ——— 2) Mensajes entrantes (POST) ———
    # Facebook nos envía un JSON con la info del mensaje.
    payload = request.get_json()
    # Ejemplo de payload:
    # {
    #   "object": "whatsapp_business_account",
    #   "entry": [
    #     {
    #       "id": "109232…",
    #       "changes": [
    #         {
    #           "value": {
    #             "messaging_product": "whatsapp",
    #             "metadata": {
    #               "display_phone_number": "5491138261717",
    #               "phone_number_id": "656903770841867"
    #             },
    #             "messages": [
    #               {
    #                 "from": "54911XXXXXXX",      # número del usuario
    #                 "id": "wamid.HBgMM…",
    #                 "timestamp": "168…",
    #                 "text": {
    #                   "body": "hola"
    #                 },
    #                 "type": "text"
    #               }
    #             ]
    #           },
    #           "field": "messages"
    #         }
    #       ]
    #     }
    #   ]
    # }

    # Extraemos la sección "messages" si existe
    try:
        cambios = payload.get("entry", [])[0].get("changes", [])[0]
        valor   = cambios.get("value", {})
        mensajes = valor.get("messages", [])
    except:
        mensajes = []

    if mensajes:
        msg = mensajes[0]
        tipo = msg.get("type")
        origen = msg.get("from")         # p.ej. "54911XXXXXXX"
        texto_recibido = msg.get("text", {}).get("body", "").strip().lower()

        # Obtenemos o inicializamos la sesión del paciente
        paciente = get_paciente(origen)

        # ————— A) Si el usuario escribe "reiniciar" siempre reiniciamos flujo —————
        if paciente['estado'] is None and "reiniciar" in texto_recibido:
            clear_paciente(origen)
            enviar_mensaje_whatsapp(origen, "Flujo reiniciado. ¿En qué puedo ayudarte hoy?")
            return Response(status=200)

        # ————— B) Derivar a operador si dice “operador”, “ayuda” o “asistente” —————
        if paciente['estado'] is None and any(k in texto_recibido for k in ['asistente','ayuda','operador']):
            derivar_a_operador(origen)
            clear_paciente(origen)
            enviar_mensaje_whatsapp(origen, "Te derivamos a un operador. En breve te contactarán.")
            return Response(status=200)

        # ————— C) Saludo y menú principal —————
        if paciente['estado'] is None and any(k in texto_recibido for k in ['hola','buenas']):
            paciente['estado'] = 'menu'
            save_paciente(origen, paciente)
            menu_text = (
                "Hola! Soy ALIA, tu asistente IA de laboratorio. Elige una opción enviando el número:\n"
                "1. Pedir un turno\n"
                "2. Solicitar envío de resultados\n"
                "3. Contactar con un operador"
            )
            enviar_mensaje_whatsapp(origen, menu_text)
            return Response(status=200)

        # ————— D) Selección del menú principal —————
        if paciente.get('estado') == 'menu':
            if texto_recibido == '1':
                paciente['estado'] = 'menu_turno'
                save_paciente(origen, paciente)
                texto = (
                    "¿Dónde prefieres el turno? Elige un número:\n"
                    "1. Sede\n"
                    "2. Domicilio"
                )
                enviar_mensaje_whatsapp(origen, texto)
            elif texto_recibido == '2':
                paciente['estado'] = 'esperando_resultados_nombre'
                save_paciente(origen, paciente)
                enviar_mensaje_whatsapp(origen, "Para enviarte tus resultados, por favor indicá tu nombre completo:")
            elif texto_recibido == '3':
                derivar_a_operador(origen)
                clear_paciente(origen)
                enviar_mensaje_whatsapp(origen, "Te derivamos a un operador. En breve te contactarán.")
            else:
                enviar_mensaje_whatsapp(origen, "Opción no válida. Por favor elige 1, 2 o 3.")
            return Response(status=200)

        # ————— E) Sub-menú “Pedir turno” —————
        if paciente.get('estado') == 'menu_turno':
            if texto_recibido == '1':
                paciente['tipo_atencion'] = 'SEDE'
            elif texto_recibido == '2':
                paciente['tipo_atencion'] = 'DOMICILIO'
            else:
                enviar_mensaje_whatsapp(origen, "Elige 1 (Sede) o 2 (Domicilio), por favor.")
                return Response(status=200)

            save_paciente(origen, paciente)
            siguiente = siguiente_campo_faltante(paciente)
            enviar_mensaje_whatsapp(origen, siguiente)
            return Response(status=200)

        # ————— F) Flujo “Solicitar envío de resultados” —————
        if paciente.get('estado') == 'esperando_resultados_nombre':
            paciente['nombre'] = msg.get("text", {}).get("body", "").title()
            paciente['estado'] = 'esperando_resultados_dni'
            save_paciente(origen, paciente)
            enviar_mensaje_whatsapp(origen, "Gracias. Ahora indicá tu número de documento:")
            return Response(status=200)

        if paciente.get('estado') == 'esperando_resultados_dni':
            paciente['dni'] = msg.get("text", {}).get("body", "").strip()
            paciente['estado'] = 'esperando_resultados_localidad'
            save_paciente(origen, paciente)
            enviar_mensaje_whatsapp(origen, "Por último, indicá tu localidad:")
            return Response(status=200)

        if paciente.get('estado') == 'esperando_resultados_localidad':
            paciente['localidad'] = msg.get("text", {}).get("body", "").title()
            derivar_a_operador(origen)
            clear_paciente(origen)
            texto_final = (
                f"Solicitamos el envío de resultados para {paciente['nombre']} "
                f"({paciente['dni']}) en {paciente['localidad']}."
            )
            enviar_mensaje_whatsapp(origen, texto_final)
            return Response(status=200)

        # ————— G) Flujo de datos secuenciales “Pedir turno” —————
        if paciente.get('estado') and paciente['estado'].startswith('esperando_'):
            campo = paciente['estado'].split('_', 1)[1]
            # extrae valor del mensaje entrante (solo texto)
            valor = msg.get("text", {}).get("body", "")
            # Capitaliza en mayúsculas iniciales si corresponde
            paciente[campo] = valor.title() if campo in ['nombre','localidad'] else valor
            save_paciente(origen, paciente)

            # Chequea si falta otro campo
            pregunta = siguiente_campo_faltante(paciente)
            if pregunta:
                enviar_mensaje_whatsapp(origen, pregunta)
                return Response(status=200)

            # Si ya llenó todos los campos, pasamos a esperar la orden (imagen OCR)
            paciente['estado'] = 'esperando_orden'
            save_paciente(origen, paciente)
            enviar_mensaje_whatsapp(origen, "Envía una foto de tu orden médica o responde 'No tengo orden'.")
            return Response(status=200)

        # ————— H) Manejo de “No tengo orden” en turno —————
        if paciente.get('estado') == 'esperando_orden' and "no tengo orden" in texto_recibido:
            clear_paciente(origen)
            enviar_mensaje_whatsapp(origen, "Ok, continuamos sin orden. De ser necesario, te contactaremos.")
            return Response(status=200)

        # ————— I) Manejo de imagen de orden para OCR —————
        # Cuando llega un mensaje de tipo “image”, Meta lo envía como un objeto “image” en lugar de “text”.
        # Verificamos si el JSON trae "image" bajo el mismo mensaje:
        if paciente.get('estado') == 'esperando_orden' and 'image' in msg:
            # Extraemos el media object
            # Meta te da un ID de media, debes recuperar la URL con un GET:
            media_id = msg['image']['id']
            # Paso 1: Obtener la URL del archivo (usando tu token)
            url_media = f"https://graph.facebook.com/v16.0/{media_id}"
            url_media += f"?fields=url&access_token={META_ACCESS_TOKEN}"
            try:
                r_media = requests.get(url_media, timeout=5)
                archivo = r_media.json().get('url')
                # Paso 2: Descargar la imagen
                resp_img = requests.get(archivo, timeout=5)
                b64 = base64.b64encode(resp_img.content).decode()
            except Exception as e:
                clear_paciente(origen)
                enviar_mensaje_whatsapp(origen, "No pudimos descargar tu orden. Te derivamos a un operador.")
                return Response(status=200)

            # Paso 3: Llamamos a tu servicio de OCR externo
            OCR_SERVICE_URL = "https://ocr-microsistema.onrender.com/ocr"
            try:
                ocr = requests.post(OCR_SERVICE_URL, json={'image_base64': b64}, timeout=10)
                texto_ocr = ocr.json().get('text', '').strip() if ocr.ok else ''
                if not texto_ocr:
                    raise Exception("OCR vacío")
            except:
                clear_paciente(origen)
                enviar_mensaje_whatsapp(origen, "No pudimos procesar tu orden. Te derivamos a un operador.")
                return Response(status=200)

            # Paso 4: Pedimos a OpenAI (GPT-4) que extraiga los campos de la orden
            prompt = f"Analiza esta orden médica y devuelve un JSON con las claves:\n'estudios', 'cobertura', 'afiliado'.\n\n{texto_ocr}"
            try:
                pg = client_openai.chat.completions.create(
                    model="gpt-4",
                    messages=[{"role":"user","content":prompt}]
                )
                contenido = pg.choices[0].message.content.strip()
            except Exception as e:
                contenido = ""

            # Intentamos parsear el JSON que devolvió GPT
            try:
                datos = json.loads(contenido)
            except json.JSONDecodeError:
                clear_paciente(origen)
                enviar_mensaje_whatsapp(
                    origen,
                    "Lo siento, hubo un error interpretando tu orden. "
                    "¿Podrías enviarla de nuevo o responder 'No tengo orden'?"
                )
                return Response(status=200)

            # Guardamos los datos extraídos en Redis
            paciente.update({
                'estudios':      datos.get('estudios'),
                'cobertura':     datos.get('cobertura'),
                'afiliado':      datos.get('afiliado'),
                'imagen_base64': b64
            })
            save_paciente(origen, paciente)

            # Preguntamos el siguiente campo faltante (si existe)
            siguiente = siguiente_campo_faltante(paciente)
            if siguiente:
                texto_intermedio = (
                    f"Detectamos estos datos:\n{json.dumps(datos, ensure_ascii=False)}\n\n{siguiente}"
                )
                enviar_mensaje_whatsapp(origen, texto_intermedio)
                return Response(status=200)

            # Si no faltan campos, terminamos reservando el turno
            sede, dir_sede = determinar_sede(paciente['localidad'])
            if paciente['tipo_atencion'] == 'SEDE':
                mensaje = (
                    f"El pre-ingreso se realizó correctamente. Te esperamos en la sede {sede} "
                    f"({dir_sede}) de 07:40 a 11:00. ¡Muchas gracias!"
                )
            else:
                dia = determinar_dia_turno(paciente['localidad'])
                mensaje = (
                    f"Tu turno se reservó para el día {dia}, te visitaremos de 08:00 a 11:00. "
                    "¡Muchas gracias!"
                )
            clear_paciente(origen)
            enviar_mensaje_whatsapp(origen, mensaje)
            return Response(status=200)

        # ————— J) Fallback: si el usuario escribe cualquier otra cosa —————
        info = paciente
        edad = calcular_edad(info.get('fecha_nacimiento','')) or 'desconocida'
        prompt_fb = (
            f"Paciente: {info.get('nombre','Paciente')}, Edad: {edad}\n"
            f"OCR: {info.get('imagen_base64','')[:30]}...\n"
            f"Pregunta: {texto_recibido}\n"
            "Responde únicamente si debe realizar ayuno (horas) o recolectar orina."
        )
        try:
            fb = client_openai.chat.completions.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt_fb}]
            )
            resp_text = fb.choices[0].message.content.strip()
        except:
            resp_text = "Error procesando tu consulta. Intentá más tarde."

        enviar_mensaje_whatsapp(origen, resp_text)
    else:
        # A veces Facebook envía otros events (por ejemplo actualizaciones de message_status)
        pass

    return Response(status=200)


# ————— Punto de entrada —————
if __name__ == '__main__':
    puerto = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=puerto)
