
from flask import Flask, request, Response
import openai
import os

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
pacientes = {}

def responder_whatsapp(texto):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{texto}</Message></Response>'
    return Response(xml, mimetype='application/xml')

def derivar_a_operador(tel):
    print(f"Derivando a operador: {tel}")
    # Lógica de derivación real va aquí

@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    body = request.form.get('Body', '').strip()
    msg = body.lower()
    tel = request.form.get('From', '')

    if tel not in pacientes:
        pacientes[tel] = {'estado': None, 'reintentos': 0, 'ocr_fallos': 0}

    if any(k in msg for k in ['asistente', 'ayuda', 'operador']):
        derivar_a_operador(tel)
        return responder_whatsapp("Estamos derivando tus datos a un operador. En breve serás contactado.")

    if msg in ['hola', '¡hola!', 'hola!', 'buenas', 'buenos días']:
        return responder_whatsapp("Hola! Soy ALIA, tu asistente con IA de laboratorio. Escribí *ASISTENTE* en cualquier momento y serás derivado a un operador. ¿En qué puedo ayudarte hoy?")

    if 'turno' in msg:
        return responder_whatsapp("¿Preferís atenderte en alguna de nuestras *sedes* o necesitás atención a *domicilio*?")

    if 'sede' in msg and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos_sede'
        pacientes[tel]['reintentos'] = 0
        return responder_whatsapp("Perfecto. En *SEDE*, por favor escribí: Nombre completo, Localidad, Fecha de nacimiento (dd/mm/aaaa), Cobertura, N° de Afiliado. *Separalos con comas*.")

    if any(k in msg for k in ['domicilio', 'casa', 'venir']) and pacientes[tel]['estado'] is None:
        pacientes[tel]['estado'] = 'esperando_datos_domicilio'
        pacientes[tel]['reintentos'] = 0
        return responder_whatsapp("Perfecto. Para *DOMICILIO*, escribí: Nombre, Dirección, Localidad, Fecha (dd/mm/aaaa), Cobertura, N° Afiliado. *Separalos por comas*.")

    return responder_whatsapp("No entendí tu mensaje. Escribí *ASISTENTE* si necesitás ayuda.")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
