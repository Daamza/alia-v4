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

# Instancias
app      = Flask(__name__)
client   = OpenAI(api_key=OPENAI_API_KEY)
r        = redis.from_url(REDIS_URL, decode_responses=True)

# --- Helpers de sesión ---------------------------------------------------------
def get_paciente(tel):
    data = r.get(f"paciente:{tel}")
    if data:
        return json.loads(data)
    paciente = {'estado': None, 'ocr_fallos': 0, 'tipo_atencion': None}
    r.set(f"paciente:{tel}", json.dumps(paciente))
    return paciente

def save_paciente(tel, info):
    r.set(f"paciente:{tel}", json.dumps(info))

def clear_paciente(tel):
    r.delete(f"paciente:{tel}")

# --- Funciones auxiliares ------------------------------------------------------
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

# (Las demás funciones: crear_hoja_del_dia, determinar_dia_turno,
#  determinar_sede, derivar_a_operador quedan igual que antes)

# --- Webhook de WhatsApp -------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    body = request.form.get('Body','').strip()
    msg  = body.lower()
    tel  = request.form.get('From','')

    paciente = get_paciente(tel)

    # Comando reiniciar manual
    if 'reiniciar' in msg:
        clear_paciente(tel)
        return responder_whatsapp("Flujo reiniciado. ¿En qué puedo ayudarte hoy?")

    # 1) 'asistente' -> derivar + final
    if any(k in msg for k in ['asistente','ayuda','operador']):
        derivar_a_operador(tel)
        clear_paciente(tel)
        return responder_final("Estamos derivando tus datos a un operador. En breve te contactarán.")

    # 2) Saludo
    if any(k in msg for k in ['hola','buenas']):
        return responder_whatsapp(
            "Hola! Soy ALIA, tu asistente de laboratorio de viaje. ¿Qué querés hacer?\n"
            "• Pedir un turno\n"
            "• Solicitar informes\n"
            "• Contactarte con un operador"
        )

    # 3) Solicitar informes
    if 'informes' in msg and paciente['estado'] is None:
        paciente['estado'] = 'esperando_informes'
        save_paciente(tel, paciente)
        return responder_whatsapp("Para informes, envía: Nombre completo, Localidad.")

    if paciente['estado'] == 'esperando_informes':
        parts = [p.strip() for p in body.split(',')]
        if len(parts)>=2:
            paciente['nombre'], paciente['localidad'] = parts[:2]
            derivar_a_operador(tel)
            clear_paciente(tel)
            return responder_final(f"Solicitamos informes para {paciente['nombre']} en {paciente['localidad']}.")
        return responder_whatsapp("Datos incompletos. Envío: Nombre completo, Localidad.")

    # 4) Pedir turno
    if 'turno' in msg and paciente['estado'] is None:
        return responder_whatsapp("¿Turno en sede o atención a domicilio?")

    # 5) Pre-ingreso Sede/Domicilio
    if 'sede' in msg and paciente['estado'] is None:
        paciente['estado']       = 'esperando_datos'
        paciente['tipo_atencion']= 'SEDE'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Para SEDE, envía: Nombre, Localidad, Fecha (dd/mm/aaaa), Cobertura, Nº Afiliado."
        )
    if any(k in msg for k in ['domicilio','casa']) and paciente['estado'] is None:
        paciente['estado']       = 'esperando_datos'
        paciente['tipo_atencion']= 'DOMICILIO'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Para DOMICILIO, envía: Nombre, Dirección, Localidad, Fecha (dd/mm/aaaa), Cobertura, Nº Afiliado."
        )

    # 6) Procesar datos básicos
    if paciente['estado']=='esperando_datos':
        parts = [p.strip() for p in body.split(',')]
        if len(parts)<6:
            return responder_whatsapp("Faltan datos. Envía 6 campos separados por comas.")
        paciente.update({
            'nombre':parts[0].title(), 'direccion':parts[1],
            'localidad':parts[2], 'fecha_nacimiento':parts[3],
            'cobertura':parts[4], 'afiliado':parts[5]
        })
        paciente['estado']='esperando_orden'
        save_paciente(tel, paciente)
        return responder_whatsapp(
            "Ahora enviá tu orden médica en foto JPG/PNG o responde 'No tengo orden'."
        )

    # 7) 'No tengo orden'
    if paciente['estado']=='esperando_orden' and 'no tengo orden' in msg:
        clear_paciente(tel)
        return responder_final("Continuamos sin orden médica. Te contactaremos pronto.")

    # 8) Procesar orden
    if paciente['estado']=='esperando_orden' and request.form.get('NumMedia')=='1':
        # (flujo OCR/GPT igual que antes)
        # en caso de error:
        #   clear_paciente(tel)
        #   return responder_final("No pudimos procesar tu orden…")
        # si OK, avanzar a confirmar_estudios…
        pass

    # 9) Confirmación de estudios
    if paciente.get('estado')=='confirmando_estudios':
        if 'sí' in msg:
            clear_paciente(tel)
            return responder_final("¡Perfecto! Estudios registrados.")
        if 'no' in msg:
            paciente['estado']='esperando_orden'
            save_paciente(tel, paciente)
            return responder_whatsapp("Enviá otra foto o responde 'No tengo orden'.")
        return responder_whatsapp("Responde 'Sí', 'No' o 'No tengo orden'.")

    # 10) Fallback GPT
    # …

    return responder_whatsapp("No entendí. Escribí 'turno', 'informes' o 'ASISTENTE'.")

if __name__ == '__main__':
    puerto = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=puerto)
