from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import redis
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import base64
import os
import requests
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account

# --- Configuración -------------------------------------------------------------
OCR_SERVICE_URL       = "https://ocr-microsistema.onrender.com/ocr"
DERIVADOR_SERVICE_URL = "https://derivador-service.onrender.com/derivar"
GOOGLE_CREDS_B64      = os.getenv("GOOGLE_CREDENTIALS_BASE64")
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")
REDIS_URL             = os.getenv("REDIS_URL")

app    = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
r      = redis.from_url(REDIS_URL, decode_responses=True)

# --- Funciones de sesión -------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    p = {
        'estado': None,
        'ocr_fallos': 0,
        'tipo_atencion': None
    }
    r.set(f"paciente:{tel}", json.dumps(p))
    return p

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Auxiliares ---------------------------------------------------------------
def responder_whatsapp(texto):
    resp = MessagingResponse()
    resp.message(texto)
    return Response(str(resp), mimetype='application/xml')

def responder_final(texto):
    resp = MessagingResponse()
    resp.message(texto)
    encuesta = (
        "\n\n¡Gracias por comunicarte con ALIA! "
        "Ayudanos a mejorar completando esta encuesta: "
        "https://forms.gle/gHPbyMJfF18qYuUq9"
    )
    resp.message(encuesta)
    return Response(str(resp), mimetype='application/xml')

def calcular_edad(fecha_str):
    try:
        nac = datetime.strptime(fecha_str, '%d/%m/%Y')
        hoy = datetime.today()
        return hoy.year - nac.year - ((hoy.month, hoy.day) < (nac.month, nac.day))
    except:
        return None

# (Aquí incluirías crear_hoja_del_dia, determinar_dia_turno, determinar_sede, derivar_a_operador, igual que antes)

# --- Webhook WhatsApp ---------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    tel = request.form.get('From', '')
    try:
        body = request.form.get('Body', '').strip()
        msg  = body.lower()
        paciente = get_paciente(tel)

        # 0) Reiniciar
        if 'reiniciar' in msg:
            clear_paciente(tel)
            return responder_whatsapp("Flujo reiniciado. ¿En qué puedo ayudarte hoy?")

        # 1) Derivar a operador
        if any(k in msg for k in ['asistente','ayuda','operador']):
            derivar_a_operador(tel)
            clear_paciente(tel)
            return responder_final("Te derivamos a un operador. En breve te contactarán.")

        # 2) Saludo
        if any(k in msg for k in ['hola','buenas']):
            return responder_whatsapp(
                "Hola! Soy ALIA, tu asistente IA de laboratorio, puedes:\n"
                "• Pedir un turno\n"
                "• Solicitar envío de resultados\n"
                "• Contactarte con un operador"
            )

        # 3) Flujo de resultados secuencial
        if 'resultados' in msg and paciente['estado'] is None:
            paciente['estado'] = 'esperando_resultados_nombre'
            save_paciente(tel, paciente)
            return responder_whatsapp("Para enviarte tus resultados, por favor indícanos tu nombre completo:")

        if paciente['estado'] == 'esperando_resultados_nombre':
            paciente['nombre'] = body.title()
            paciente['estado'] = 'esperando_resultados_dni'
            save_paciente(tel, paciente)
            return responder_whatsapp("Gracias. Ahora, tu número de documento (solo números):")

        if paciente['estado'] == 'esperando_resultados_dni':
            if not body.isdigit():
                return responder_whatsapp("El DNI debe contener solo números. Intenta de nuevo:")
            paciente['dni'] = body
            paciente['estado'] = 'esperando_resultados_localidad'
            save_paciente(tel, paciente)
            return responder_whatsapp("Por último, indícanos tu localidad:")

        if paciente['estado'] == 'esperando_resultados_localidad':
            paciente['localidad'] = body.title()
            derivar_a_operador(tel)
            clear_paciente(tel)
            return responder_final(
                f"Solicitamos el envío de resultados para {paciente['nombre']} ({paciente['dni']}) en {paciente['localidad']}."
            )

        # 4) Flujo de turno
        if 'turno' in msg and paciente['estado'] is None:
            return responder_whatsapp("¿Prefieres un turno en una de nuestras sedes o atención a domicilio?")

        if 'sede' in msg and paciente['estado'] is None:
            paciente['estado'] = 'datos_nombre'
            paciente['tipo_atencion'] = 'SEDE'
            save_paciente(tel, paciente)
            return responder_whatsapp("Perfecto. Para comenzar, indícanos tu nombre completo:")

        if any(k in msg for k in ['domicilio','casa']) and paciente['estado'] is None:
            paciente['estado'] = 'datos_nombre'
            paciente['tipo_atencion'] = 'DOMICILIO'
            save_paciente(tel, paciente)
            return responder_whatsapp("Perfecto. Para comenzar, indícanos tu nombre completo:")

        if paciente['estado'] == 'datos_nombre':
            paciente['nombre'] = body.title()
            paciente['estado'] = 'datos_direccion'
            save_paciente(tel, paciente)
            return responder_whatsapp("Ahora, por favor indícanos tu domicilio:")

        if paciente['estado'] == 'datos_direccion':
            paciente['direccion'] = body
            paciente['estado'] = 'datos_localidad'
            save_paciente(tel, paciente)
            return responder_whatsapp("¿En qué localidad vivís?")

        if paciente['estado'] == 'datos_localidad':
            paciente['localidad'] = body.title()
            paciente['estado'] = 'datos_nacimiento'
            save_paciente(tel, paciente)
            return responder_whatsapp("Indica tu fecha de nacimiento (dd/mm/aaaa):")

        if paciente['estado'] == 'datos_nacimiento':
            # Validar fecha
            try:
                datetime.strptime(body, '%d/%m/%Y')
                paciente['fecha_nacimiento'] = body
                paciente['estado'] = 'datos_cobertura'
                save_paciente(tel, paciente)
                return responder_whatsapp("¿Cuál es tu cobertura médica?")
            except ValueError:
                return responder_whatsapp("Formato inválido. Usa dd/mm/aaaa:")

        if paciente['estado'] == 'datos_cobertura':
            paciente['cobertura'] = body.upper()
            paciente['estado'] = 'datos_afiliado'
            save_paciente(tel, paciente)
            return responder_whatsapp("¿Cuál es tu número de afiliado?")

        if paciente['estado'] == 'datos_afiliado':
            paciente['afiliado'] = body
            paciente['estado'] = 'esperando_orden'
            save_paciente(tel, paciente)
            return responder_whatsapp("Envía una foto de tu orden médica o responde 'No tengo orden'.")

        # 5) Procesar orden médica con reintento de OCR
        if paciente['estado'] == 'esperando_orden' and request.form.get('NumMedia') == '1':
            try:
                url = request.form.get('MediaUrl0')
                resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=5)
                b64 = base64.b64encode(resp.content).decode()
                paciente['imagen_base64'] = b64
                ocr_resp = requests.post(OCR_SERVICE_URL, json={'image_base64': b64}, timeout=10)
                texto_ocr = ocr_resp.json().get('text', '').strip() if ocr_resp.ok else ''
                if not texto_ocr:
                    raise Exception("OCR vacío")
            except Exception as e:
                paciente['ocr_fallos'] = paciente.get('ocr_fallos', 0) + 1
                save_paciente(tel, paciente)
                print(f"[OCR] Fallo #{paciente['ocr_fallos']} para {tel}: {e}")
                if paciente['ocr_fallos'] < 2:
                    return responder_whatsapp("No pudimos leer tu orden. Por favor envíala nuevamente.")
                else:
                    clear_paciente(tel)
                    derivar_a_operador(tel)
                    return responder_final("No pudimos procesar tu orden. Te derivamos a un operador.")

            # Si OCR fue exitoso...
            paciente['estado'] = 'confirmando_estudios'
            paciente['texto_ocr'] = texto_ocr
            save_paciente(tel, paciente)
            prompt = f"Analiza orden médica:\n{texto_ocr}\nExtrae estudios, cobertura y afiliado."
            try:
                pg = client.chat.completions.create(
                    model="gpt-4",
                    messages=[{"role":"user","content":prompt}]
                )
                estudios = pg.choices[0].message.content.strip()
            except Exception as e:
                print("Error GPT al extraer estudios:", e)
                estudios = ""
            paciente['estudios'] = estudios
            save_paciente(tel, paciente)
            return responder_whatsapp(
                f"Detectamos estos estudios:\n{estudios}\n¿Son correctos? Responde 'Sí' o 'No'."
            )

        # 6) Confirmación de estudios
        if paciente.get('estado') == 'confirmando_estudios':
            if 'sí' in msg or 'si' in msg:
                # Guardar en Google Sheets (opcional)
                try:
                    hoja = crear_hoja_del_dia(datetime.today().strftime('%d-%m-%Y'))
                    hoja.append_row([
                        datetime.now().strftime('%d/%m/%Y %H:%M'),
                        paciente.get('nombre'),
                        tel,
                        paciente.get('direccion'),
                        paciente.get('localidad'),
                        paciente.get('fecha_nacimiento'),
                        paciente.get('cobertura'),
                        paciente.get('afiliado'),
                        paciente.get('estudios'),
                        "Confirmado"
                    ])
                except Exception as e:
                    print("Error al guardar en Sheets:", e)

                # Preparar mensaje final
                if paciente['tipo_atencion'] == 'SEDE':
                    sede, dir_sede = determinar_sede(paciente['localidad'])
                    mensaje = (
                        f"El pre-ingreso se realizó correctamente. "
                        f"Te esperamos en la sede {sede} ({dir_sede}) "
                        "en el horario de 07:40 a 11:00. ¡Gracias!"
                    )
                else:
                    dia = determinar_dia_turno(paciente['localidad'])
                    mensaje = (
                        f"Tu turno se reservó para el día {dia}, "
                        "te visitaremos de 08:00 a 11:00. ¡Gracias!"
                    )

                clear_paciente(tel)
                return responder_final(mensaje)

            if 'no' in msg:
                paciente['estado'] = 'esperando_orden'
                save_paciente(tel, paciente)
                return responder_whatsapp("Reenvía una foto clara de tu orden o responde 'No tengo orden'.")
            return responder_whatsapp("Responde 'Sí', 'No' o 'No tengo orden'.")

        # 7) Manejador de estado inesperado
        allowed = {
            None,
            'esperando_resultados_nombre','esperando_resultados_dni','esperando_resultados_localidad',
            'datos_nombre','datos_direccion','datos_localidad','datos_nacimiento',
            'datos_cobertura','datos_afiliado','esperando_orden','confirmando_estudios'
        }
        if paciente.get('estado') not in allowed:
            print(f"[Estado inesperado] {paciente.get('estado')} para {tel}")
            clear_paciente(tel)
            return responder_whatsapp("Ocurrió un error inesperado. Reiniciamos el flujo. ¿En qué puedo ayudarte?")

        # 8) Fallback GPT (solo para indicaciones de ayuno/recoger orina)
        info  = paciente
        edad  = calcular_edad(info.get('fecha_nacimiento','')) or 'desconocida'
        texto = info.get('texto_ocr','')
        prompt_fb = (
            f"Paciente: {info.get('nombre','Paciente')}, Edad: {edad}\n"
            f"OCR: {texto}\nPregunta: {body}\n"
            "Responde únicamente si debe ayunar y si debe recolectar orina."
        )
        try:
            fb = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role":"user","content":prompt_fb}]
            )
            return responder_whatsapp(fb.choices[0].message.content.strip())
        except Exception as e:
            print("Error en fallback GPT:", e)
            return responder_whatsapp("Ocurrió un error procesando tu consulta. Reiniciamos.")
    except Exception as e:
        print(f"[Webhook error] {e} para {tel}")
        clear_paciente(tel)
        return responder_whatsapp("Ocurrió un error interno. Reiniciamos el flujo. ¿En qué puedo ayudarte?")

# --- Entrypoint ---------------------------------------------------------------
if __name__ == '__main__':
    puerto = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=puerto)
