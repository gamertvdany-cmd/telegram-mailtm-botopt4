# main.py
import os
import asyncio
import httpx
import json
import re
import random
import string
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("TOKEN")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
OTP_DIGITS_MIN = 4
OTP_DIGITS_MAX = 8
OTP_REGEX = re.compile(r"\b(\d{" + str(OTP_DIGITS_MIN) + r"," + str(OTP_DIGITS_MAX) + r"})\b")
DATA_FILE = "usuarios.json"
MAILTM_BASE = "https://api.mail.tm"

# ---------------- Persistencia ----------------
def cargar_usuarios():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def guardar_usuarios(usuarios):
    with open(DATA_FILE, "w") as f:
        json.dump(usuarios, f)

usuarios = cargar_usuarios()          # chat_id (str) -> list of account dicts {email, token, id}
seen_messages = {}                    # message_id -> True

# ---------------- Mail.tm helpers ----------------
async def crear_correo_temporal():
    async with httpx.AsyncClient(timeout=30) as client:
        # dominios
        r = await client.get(f"{MAILTM_BASE}/domains")
        if r.status_code != 200:
            print("Mail.tm dominios error:", r.status_code, r.text)
            return None
        dominios = r.json().get("hydra:member", [])
        if not dominios:
            print("No hay dominios en Mail.tm")
            return None
        dominio = dominios[0]["domain"]

        # generar credenciales válidas
        nombre = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"{nombre}@{dominio}"
        password = "Temp1234!"  # cumple min length / mayúscula / número / símbolo

        payload = {"address": email, "password": password}
        r = await client.post(f"{MAILTM_BASE}/accounts", json=payload)
        if r.status_code not in (200, 201):
            print("Error creando cuenta:", r.status_code, r.text)
            return None

        # obtener token
        r = await client.post(f"{MAILTM_BASE}/token", json=payload)
        if r.status_code != 200:
            print("Error token:", r.status_code, r.text)
            return None
        token = r.json().get("token")
        # obtener id
        r = await client.get(f"{MAILTM_BASE}/me", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            print("Error me:", r.status_code, r.text)
            return None
        id_ = r.json().get("id")
        return {"email": email, "token": token, "id": id_}


async def list_messages(account):
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        r = await client.get(f"{MAILTM_BASE}/messages", headers=headers)
        if r.status_code != 200:
            # mostrar log corto
            print(f"list_messages error {account['email']}: {r.status_code} {r.text[:200]}")
            return []
        try:
            return r.json().get("hydra:member", [])
        except Exception:
            return []


async def delete_message(account, message_id):
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        await client.delete(f"{MAILTM_BASE}/messages/{message_id}", headers=headers)


# ---------------- Extractor robusto de OTP ----------------
def extract_otp_from_message(m):
    """
    m: mensaje JSON de mail.tm
    devuelve: (otp_text or None, debug_text)
    debug_text contiene fragmentos útiles para ver en logs (html/text length, sample hrefs)
    """
    # campos comunes
    text = m.get("text") or ""
    html = m.get("html") or ""
    subject = m.get("subject") or ""
    debug_parts = []

    # 1) buscar en text plano
    if text:
        debug_parts.append(f"text_len={len(text)}")
        match = OTP_REGEX.search(text)
        if match:
            return match.group(1), "found_in_text"

    # 2) parsear html y extraer texto
    if html:
        debug_parts.append(f"html_len={len(html)}")
        soup = BeautifulSoup(html, "html.parser")
        visible_text = soup.get_text(separator="\n", strip=True)
        match = OTP_REGEX.search(visible_text or "")
        if match:
            return match.group(1), "found_in_html_text"

        # 3) buscar en links (hrefs) parámetros que contengan códigos
        hrefs = [a.get("href") for a in soup.find_all("a", href=True)]
        debug_parts.append(f"hrefs={len(hrefs)}")
        for href in hrefs:
            try:
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                # buscar claves comunes
                for key in ("code", "otp", "token", "verify", "confirmation", "confirm"):
                    if key in qs:
                        for val in qs[key]:
                            mnum = OTP_REGEX.search(val)
                            if mnum:
                                return mnum.group(1), f"found_in_href_param_{key}"
                # si no hay params, buscar números en la ruta o en el href completo
                mnum = OTP_REGEX.search(href)
                if mnum:
                    return mnum.group(1), "found_in_href_digits"
            except Exception:
                continue

    # 4) buscar números en subject
    if subject:
        debug_parts.append(f"subject_len={len(subject)}")
        match = OTP_REGEX.search(subject)
        if match:
            return match.group(1), "found_in_subject"

    # 5) buscar en atributos data-* o valores inline (ej: data-code="1234")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(True):
            for attr, val in tag.attrs.items():
                if isinstance(val, str):
                    mnum = OTP_REGEX.search(val)
                    if mnum:
                        return mnum.group(1), f"found_in_attr_{attr}"
                elif isinstance(val, list):
                    for v in val:
                        mnum = OTP_REGEX.search(v)
                        if mnum:
                            return mnum.group(1), f"found_in_attrlist_{attr}"

    # 6) buscar en meta refresh (p.e. <meta http-equiv="refresh" content="0; url=https://...code=1234">)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        metas = soup.find_all("meta", attrs={"http-equiv": True})
        for meta in metas:
            content = meta.get("content", "")
            mnum = OTP_REGEX.search(content)
            if mnum:
                return mnum.group(1), "found_in_meta_refresh"

    # no encontrado
    return None, "not_found|" + "|".join(debug_parts)


# ---------------- Poller ----------------
async def poll_emails(app):
    while True:
        try:
            for chat_id_str, accounts in list(usuarios.items()):
                for acc in accounts:
                    messages = await list_messages(acc)
                    # debug: si quieres ver raw messages, imprime solo 1 por cuenta para no spamear
                    if messages:
                        print(f"[DEBUG] {acc['email']} messages_count={len(messages)}")
                    for m in messages:
                        message_id = m.get("id")
                        if not message_id:
                            continue
                        if message_id in seen_messages:
                            continue
                        seen_messages[message_id] = True

                        # intentar extraer OTP
                        otp, found_where = extract_otp_from_message(m)
                        # obtener snippet legible
                        html = m.get("html") or ""
                        text = m.get("text") or ""
                        snippet = (text.strip()[:400] if text else "") or (BeautifulSoup(html, "html.parser").get_text()[:400] if html else "")
                        if otp:
                            msg = f"📲 *OTP recibido* en `{acc['email']}`:\n`{otp}`\n\n_origen: {found_where}_"
                        else:
                            msg = f"📧 Nuevo mensaje en `{acc['email']}` (sin OTP detectado):\n\n{snippet}\n\n_debug: {found_where}_"

                        try:
                            await app.bot.send_message(chat_id=int(chat_id_str), text=msg, parse_mode="Markdown")
                        except Exception as e:
                            print("Error enviando Telegram:", e)

                        # intentar borrar mensaje (si quieres conservar, comentar esta línea)
                        try:
                            await delete_message(acc, message_id)
                        except Exception:
                            pass

        except Exception as e:
            print("Error en poll_emails:", e)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------- Comandos Telegram ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    usuarios.setdefault(chat_id, [])
    guardar_usuarios(usuarios)
    await update.message.reply_text(
        "✅ Bot listo con Mail.tm.\n"
        "Comandos:\n/new - crear correo\n/list - listar correos\n/delete <correo> - eliminar\n/inbox - contar mensajes procesados"
    )

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    account = await crear_correo_temporal()
    if not account:
        await update.message.reply_text("❌ Error creando correo temporal. Revisa logs en Render.")
        return
    usuarios.setdefault(chat_id, []).append(account)
    guardar_usuarios(usuarios)
    await update.message.reply_text(f"✅ Correo creado: `{account['email']}`\nUsa ese correo en el servicio (Amazon, etc.)", parse_mode="Markdown")

async def list_emails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    accounts = usuarios.get(chat_id, [])
    if not accounts:
        await update.message.reply_text("No tienes correos. Usa /new")
        return
    texto = "\n".join([a["email"] for a in accounts])
    await update.message.reply_text(f"📬 Tus correos:\n{texto}")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    accounts = usuarios.get(chat_id, [])
    if not context.args:
        await update.message.reply_text("Usa: /delete <correo>")
        return
    correo = context.args[0]
    for a in accounts:
        if a["email"] == correo:
            accounts.remove(a)
            guardar_usuarios(usuarios)
            await update.message.reply_text(f"🗑 Correo eliminado: {correo}")
            return
    await update.message.reply_text("Correo no encontrado.")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    accounts = usuarios.get(chat_id, [])
    if not accounts:
        await update.message.reply_text("No tienes correos.")
        return
    total = 0
    for acc in accounts:
        msgs = await list_messages(acc)
        total += len(msgs)
    await update.message.reply_text(f"Mensajes en bandeja: {total}")

# ---------------- Inicialización ----------------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("new", new_email))
    app.add_handler(CommandHandler("list", list_emails))
    app.add_handler(CommandHandler("delete", delete_email))
    app.add_handler(CommandHandler("inbox", inbox))

    # arrancar poller en background cuando exista loop
    async def start_polling_background():
        asyncio.create_task(poll_emails(app))
        print("Poller iniciado en background...")

    # schedule start_polling_background in existing loop before run_polling
    asyncio.get_event_loop().create_task(start_polling_background())
    print("Bot iniciado y poller corriendo...")
    app.run_polling()
