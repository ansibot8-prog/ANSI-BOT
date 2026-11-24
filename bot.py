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
PORT = int(os.environ.get("PORT", 10000))
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app_flask = Flask(__name__)
application = None

# --- LÓGICA DE GOOGLE SHEETS ---

def make_g_request(action, params=None):
    """Función auxiliar para interactuar con Google Script"""
    if not GOOGLE_SCRIPT_URL:
        return {"status": "error", "message": "Google Script URL no configurada"}
    
    if params is None:
        params = {}
    params['action'] = action
    try:
        response = requests.get(GOOGLE_SCRIPT_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error contacting Google Sheets: {e}")
        return {"status": "error", "message": str(e)}

# --- LÓGICA DE COOKIES Y NETFLIX (Adaptada de Net2bot) ---

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
    text = text.strip()
    if not (text.startswith('[') and text.endswith(']')):
        return False
    try:
        json.loads(text)
        return True
    except:
        return False

def _is_netscape_cookie(text: str) -> bool:
    lines = text.strip().split('\n')
    if len(lines) < 1:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '\t' in line:
            parts = line.split('\t')
            if len(parts) >= 7:
                return True
        else:
            parts = line.split()
            if len(parts) >= 7:
                domain = parts[0]
                flag = parts[1]
                path = parts[2]
                secure = parts[3]
                try:
                    expiration = int(parts[4])
                    name = parts[5]
                    if domain.startswith('.') and flag in ['TRUE', 'FALSE'] and path == '/' and secure in ['TRUE', 'FALSE']:
                        return True
                except (ValueError, IndexError):
                    continue
    return False

def _looks_like_cookie_header(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("cookie:") or (";" in text and "=" in text)

def _extract_json_cookies(json_str: str) -> Dict[str, str]:
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
        logger.error(f"Error parsing JSON cookies: {e}")
        return {}

def _extract_netscape_cookies(netscape_str: str) -> Dict[str, str]:
    kv: Dict[str, str] = {}
    lines = netscape_str.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '\t' in line:
            parts = line.split('\t')
            delimiter = '\t'
        else:
            parts = line.split()
            delimiter = ' '
        
        if len(parts) >= 7:
            try:
                if delimiter == '\t':
                    domain = parts[0]; flag = parts[1]; path = parts[2]
                    secure = parts[3]; expiration = parts[4]; name = parts[5]; value = parts[6]
                else:
                    domain = parts[0]; flag = parts[1]; path = parts[2]; secure = parts[3]
                    try: expiration = int(parts[4])
                    except ValueError: continue
                    name = parts[5]
                    match = re.match(r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$', line)
                    if match: value = match.group(7).strip()
                    else: value = ' '.join(parts[6:])
                
                if name and value:
                    kv[name] = value
            except (ValueError, IndexError):
                continue
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
    if _is_json_cookie(cookie_input):
        kv = _extract_json_cookies(cookie_input)
        if kv: return "; ".join([f"{k}={v}" for k, v in kv.items()])
    
    if _is_netscape_cookie(cookie_input):
        kv = _extract_netscape_cookies(cookie_input)
        if kv: return "; ".join([f"{k}={v}" for k, v in kv.items()])
    
    if _looks_like_cookie_header(cookie_input):
        cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", cookie_input).strip()
        if "=" not in cleaned: return None
        kv = _extract_cookie_kv_pairs(cookie_input)
        if kv: return "; ".join([f"{k}={v}" for k, v in kv.items()])
        return cleaned
    
    kv = _extract_cookie_kv_pairs(cookie_input)
    if kv: return "; ".join([f"{k}={v}" for k, v in kv.items()])
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
        if not token or "=" not in token: continue
        k, v = token.split("=", 1)
        if k.strip().lower() == "netflixid": return v.strip()
    return None

def _generate_nftoken(cookie_header: str, timeout_s: int = 20) -> Tuple[bool, str, Optional[str]]:
    netflix_id = _extract_netflix_id_from_cookie_header(cookie_header)
    if not netflix_id:
        return False, "Invalid or expired cookie. NetflixId not found.", None

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
        r.raise_for_status()
        data = r.json()

        td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
        nftoken = td.get("token")

        if not nftoken:
            return False, "Invalid or expired cookie. Token generation failed.", None

        return True, "Token API returned a valid nftoken.", nftoken
    except Exception as e:
        logger.error(f"Error generating nftoken: {e}")
        return False, "Invalid or expired cookie.", None

def _scrub_text(text: str) -> str:
    if not text: return "Unknown"
    return str(text)

def decode_text(value):
    if value is None: return "Unknown"
    value = str(value)
    try: value = codecs.decode(value, "unicode_escape")
    except Exception: pass
    value = html.unescape(value)
    value = re.sub(r'\\x([0-9A-Fa-f]{2})', lambda m: bytes.fromhex(m.group(1)).decode('latin1'), value)
    return value.strip()

def _check_netflix_cookie(cookie_header: str, timeout_s: int = 25) -> Dict[str, str]:
    cookie_dict: Dict[str, str] = {}
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token: continue
        k, v = token.split("=", 1)
        cookie_dict[k.strip()] = v.strip()

    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No NetflixId found in cookies"}

    session = requests.Session()
    session.cookies.update(cookie_dict)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    urls = ["https://www.netflix.com/YourAccount", "https://www.netflix.com/account", "https://www.netflix.com/account/membership"]

    try:
        resp = None; txt = ""
        for url in urls:
            try:
                r = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True, verify=False)
                if r.status_code == 200 and "Account" in (r.text or ""):
                    resp = r; txt = r.text or ""; break
            except Exception: continue

        if not resp or resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code if resp else 'error'}"}

        if ("login" in (resp.url or "").lower()) or ("signin" in (resp.url or "").lower()):
            return {"ok": False, "reason": "Redirected to login"}

        def find(pattern: str) -> Optional[str]:
            m = re.search(pattern, txt)
            return decode_text(m.group(1)) if m else None

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')
        plan_raw = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')
        plan = plan_raw or None
        country = find(r'"countryOfSignup"\s*:\s*"([^"]+)"') or find(r'"countryCode"\s*:\s*"([^"]+)"') or find(r'"currentCountry"\s*:\s*"([^"]+)"')
        email = find(r'"emailAddress"\s*:\s*"([^"]+)"') or find(r'"email"\s*:\s*"([^"]+)"') or find(r'"loginId"\s*:\s*"([^"]+)"')
        member_since = find(r'"memberSince":"([^"]+)"')
        next_billing = find(r'"nextBillingDate":\{[^}]*"date":"([^T"]+)"') or find(r'"nextBilling"[^}]*"value":"([^"]+)"')
        plan_price = find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"') or find(r'"formattedPlanPrice"\s*:\s*"([^"]+)"')
        payment = find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"') or find(r'"paymentMethodType"\s*:\s*"([^"]+)"')
        card = find(r'"paymentCardDisplayString"\s*:\s*"([^"]+)"') or find(r'"displayText"\s*:\s*"([^"]+)"')
        phone = find(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"') or find(r'"phoneNumber"\s*:\s*"([^"]+)"')
        phone_ver = "Yes" if re.search(r'"isVerified":true', txt) else "No" if re.search(r'"isVerified":false', txt) else None
        quality = find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"') or find(r'"maxVideoQuality"\s*:\s*"([^"]+)"')
        streams = find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)') or find(r'"maxStreams"\s*:\s*"?([0-9]+)"?')
        hold = "Yes" if re.search(r'"isUserOnHold":true', txt) else "No" if re.search(r'"isUserOnHold":false', txt) else None
        extra = "Yes" if re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":true', txt) else "No" if re.search(r'"showExtraMemberSection"', txt) else None
        email_ver = "Yes" if re.search(r'"emailVerified"\s*:\s*true', txt) else "No" if re.search(r'"emailVerified"\s*:\s*false', txt) else None
        guid = find(r'"userGuid":\s*"([^"]+)"') or find(r'"ownerGuid":\s*"([^"]+)"')

        status_match = re.search(r'"membershipStatus":\s*"([^"]+)"', txt)
        ms = status_match.group(1) if status_match else None

        is_prem = ms == "CURRENT_MEMBER" if ms else bool(plan and "free" not in str(plan).lower())
        has_account = ("Account" in txt)
        if not has_account: return {"ok": False, "reason": "No account data found"}

        profiles: list = []
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles", timeout=15, verify=False)
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles: profiles = re.findall(r'"displayName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles: profiles = re.findall(r'"name"\s*:\s*"([^"]+)"', rp.text or "")
        except Exception: pass

        def decode_profile_name(name):
           try: name = codecs.decode(name, "unicode_escape")
           except Exception: pass
           return html.unescape(name)

        profiles_str = (", ".join(decode_profile_name(_scrub_text(p)) for p in profiles) if profiles else None)

        return {
            "ok": True, "premium": str(is_prem), "name": name or "Unknown", "country": country or "Unknown",
            "plan": plan or "Unknown", "plan_price": plan_price or "Unknown", "member_since": member_since or "Unknown",
            "next_billing": next_billing or "Unknown", "payment_method": payment or "Unknown", "masked_card": card or "Unknown",
            "phone": phone or "Unknown", "phone_verified": phone_ver or "Unknown", "video_quality": quality or "Unknown",
            "max_streams": streams or "Unknown", "on_payment_hold": hold or "Unknown", "extra_member": extra or "Unknown",
            "email_verified": email_ver or "Unknown", "email": email or "Unknown", "profiles": profiles_str or "Unknown",
            "user_guid": guid or "Unknown", "membership_status": ms or "Unknown",
        }
    except Exception as e:
        logger.error(f"Error checking netflix cookie: {e}")
        return {"ok": False, "reason": str(e)}

# --- TELEGRAM HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Netflix Cookie Manager**\n\n"
        "1. Envía tu cookie con `/add <cookie>`\n"
        "2. Usa `/use` para generar tokens.\n"
        "3. Usa `/list` para ver usuarios."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 **Instrucciones:**\n\n"
        "1. Ve a netflix.com y abre la consola del navegador (F12).\n"
        "2. Escribe: `document.cookie` y copia todo lo que sale.\n"
        "3. Envíamelo así: `/add TU_COOKIE_AQUI`\n\n"
        "✅ Tus datos se guardan automáticamente."
    )

async def add_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Uso: /add <tu_cookie>")
        return

    raw_cookie = " ".join(args)
    username = update.effective_user.username or f"user_{user_id}"

    logger.info(f"Intentando añadir cookie para {username} ({user_id})")
    
    result = make_g_request('add', {'user_id': user_id, 'username': username, 'cookie': raw_cookie})

    if result.get('status') in ['created', 'updated']:
        await update.message.reply_text(f"✅ ¡Cookie guardada correctamente!\n\nID: {user_id}\nUsuario: {username}")
    else:
        await update.message.reply_text(f"❌ Error al guardar: {result.get('message')}")

async def use_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    result = make_g_request('get', {'user_id': user_id})
    
    if result.get('status') != 'found':
        await update.message.reply_text("❌ No encontré ninguna cookie guardada para ti. Usa /add primero.")
        return

    cookie_data = result.get('data', {})
    raw_cookie = cookie_data.get('cookie', '')
    
    # Construir header limpio
    cookie_header = _build_cookie_header(raw_cookie)
    if not cookie_header:
        await update.message.reply_text("❌ Error parseando la cookie guardada.")
        return

    msg = await update.message.reply_text("🔎 Verificando cookie y generando tokens...")

    loop = asyncio.get_running_loop()
    
    # 1. Generar Token Móvil
    ok_token, detail_token, nftoken_mobile = await loop.run_in_executor(None, _generate_nftoken, cookie_header)
    
    # 2. Verificar Cuenta
    acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)
    
    # Links
    phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken_mobile}" if nftoken_mobile else "#"
    desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken_mobile}" if nftoken_mobile else "#"
    
    # Formateo de salida
    status_line = f"Verification: {detail_token}\n"
    
    acct_html = ""
    if acct.get("ok"):
        acct_html = (
            "<b>Account details</b>:\n"
            f"• Name: {html.escape(str(acct.get('name','Unknown')))}\n"
            f"• Plan: {html.escape(str(acct.get('plan','Unknown')))} ({html.escape(str(acct.get('plan_price','Unknown')))})\n"
            f"• Profiles: {html.escape(str(acct.get('profiles','Unknown')))}\n"
            f"• Country: {html.escape(str(acct.get('country','Unknown')))}\n"
        )

    final_msg = (
        f"✅ Valid cookie detected.\n\n"
        f"Session login links:\n"
        f"📱 Phone: <a href='{phone_link}'>Open link</a>\n"
        f"🖥️ Desktop: <a href='{desktop_link}'>Open link</a>\n\n"
        f"{status_line}{acct_html}"
    )

    await msg.edit_text(final_msg, parse_mode="HTML", disable_web_page_preview=True)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = make_g_request('list')
    if result.get('status') == 'ok':
        count = result.get('count', 0)
        users = result.get('users', [])
        
        msg = f"📊 **Usuarios registrados: {count}**\n\n"
        for u in users[:50]:
            last = u.get('last_used', 'Nunca')
            msg += f"👤 @{u.get('username', 'anon')} (ID: {u['user_id']}) - Usado: {last}\n"
            
        if count > 50: msg += "\n...y más."
        await update.message.reply_text(msg, parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Error al listar usuarios.")

def run_bot_in_thread():
    global application
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!")
        return

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("add", add_cookie))
        application.add_handler(CommandHandler("use", use_cookie))
        application.add_handler(CommandHandler("list", list_users))
        loop.run_until_complete(application.run_polling(stop_signals=()))
    except Exception as e:
        logger.error(f"❌ Bot error: {e}", exc_info=True)
        application = None

@app_flask.route("/", methods=["GET"])
def index():
    bot_status = "🟢 Running" if application else "🔴 Stopped"
    return f"<h1>Bot Status: {bot_status}</h1>", 200

@app_flask.route("/health", methods=["GET"])
def health():
    status = "healthy" if application else "unhealthy"
    return {"status": status, "bot_running": application is not None}, 200 if application else 503

if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN environment variable is not set!")
        sys.exit(1)

    bot_thread = Thread(target=run_bot_in_thread, daemon=False)
    bot_thread.start()
    time.sleep(5)
    app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
