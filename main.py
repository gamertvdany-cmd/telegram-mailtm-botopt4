import os
import asyncio
import http.client
import json
import random
import string
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("TOKEN")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
TEMPMAIL_HOST = "privatix-temp-mail-v1.p.rapidapi.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")  # OTP de 4-8 dígitos

# ---------------- Almacenamiento ----------------
usuarios = {}  # chat_id -> lista de {"email":..., "mail_id":...}
seen_messages = {}  # mail_id -> set(message_id) ya procesados

# ---------------- Funciones Privatix ----------------
def crear_correo_temporal():
    """Crea un correo temporal y obtiene su mail_id de Privatix."""
    conn = http.client.HTTPSConnection(TEMPMAIL_HOST)
    headers = {
        'x-rapidapi-key': RAPIDAPI_KEY,
        'x-rapidapi-host': TEMPMAIL_HOST
    }

    # Elegimos un dominio aleatorio
    conn.request("GET", "/request/domains/", headers=headers)
    res = conn.getresponse()
    data = res.read()
    dominios = json.loads(data)
    dominio = random.choice(dominios) if dominios else "privatix.com"

    # Generamos correo aleatorio
    nombre = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    correo = f"{nombre}@{dominio}"

    # Registrar el correo para obtener mail_id
    conn.request("GET", f"/request/mail/id/{correo}/", headers=headers)
    res = conn.getresponse()
    data = res.read()
    try:
        resp = json.loads(data)
        # Privatix normalmente devuelve mail_id en la respuesta
        mail_id = resp.get("id", correo)  # fallback al correo
    except:
        mail_id = correo

    return correo, mail_id

def list_messages(mail_id):
    """Lista mensajes de un mail_id."""
    conn = http.client.HTTPSConnection(TEMPMAIL_HOST)
    headers = {
        'x-rapidapi-key': RAPIDAPI_KEY,
        'x-rapidapi-host': TEMPMAIL_HOST
    }
    url = f"/request/mail/id/{mail_id}/"
    conn.request("GET", url, headers=headers)
    res = conn.getresponse()
    data = res.read()
    try:
        msgs = json.loads(data)
    except:
        msgs = []
    return msgs

def delete_mail(message_id):
    """Elimina un mensaje por su ID."""
    conn = http.client.HTTPSConnection(TEMPMAIL_HOST)
    headers = {
        'x-rapidapi-key': RAPIDAPI_KEY,
        'x-rapidapi-host': TEMPMAIL_HOST
    }
    url = f"/request/delete/id/{message_id}/"
    conn.request("GET", url, headers=headers)
    res = conn.getresponse()
    data = res.read()
    return data.decode("utf-8")

# ---------------- Poller ----------------
async def poll_emails(app):
    while True:
        try:
            for chat_id, correos in usuarios.items():
                for correo_info in correos:
                    mail_id = correo_info["mail_id"]
                    seen = seen_messages.setdefault(mail_id, set())
                    msgs = list_messages(mail_id)
                    for m in msgs:
                        message_id = str(m.get("id"))
                        if message_id in seen:
                            continue
                        seen.add(message_id)
                        body = m.get("body") or ""
                        match = OTP_REGEX.search(body)
                        otp = match.group(0) if match else None
                        texto = f"📲 Nuevo OTP en {correo_info['email']}:\n{otp}" if otp else f"📧 Nuevo mensaje en {correo_info['email']}:\n{body[:300]}"
                        try:
                            await app.bot.send_message(chat_id=chat_id, text=texto)
                        except Exception as e:
                            print("Error enviando Telegram:", e)
                        delete_mail(message_id)
        except Exception as e:
            print("Error en poll_emails:", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- Comandos Telegram ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in usuarios:
        usuarios[chat_id] = []
    await update.message.reply_text(
        "✅ Bot iniciado.\n"
        "Usa /new para crear un correo temporal.\n"
        "Usa /list para ver tus correos.\n"
        "Usa /delete <correo> para eliminar.\n"
        "Recibirás automáticamente OTP/mensajes."
    )

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correo, mail_id = crear_correo_temporal()
    usuarios.setdefault(chat_id, []).append({"email": correo, "mail_id": mail_id})
    seen_messages[mail_id] = set()
    await update.message.reply_text(f"✅ Nuevo correo creado: {correo}")

async def list_emails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correos = usuarios.get(chat_id, [])
    if not correos:
        await update.message.reply_text("No tienes correos. Usa /new.")
        return
    texto = "\n".join([c["email"] for c in correos])
    await update.message.reply_text(f"📬 Tus correos:\n{texto}")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correos = usuarios.get(chat_id, [])
    if not context.args:
        await update.message.reply_text("Usa: /delete <correo>")
        return
    correo = context.args[0]
    for c in correos:
        if c["email"] == correo:
            correos.remove(c)
            seen_messages.pop(c["mail_id"], None)
            await update.message.reply_text(f"🗑 Correo eliminado: {correo}")
            return
    await update.message.reply_text("Correo no encontrado.")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correos = usuarios.get(chat_id, [])
    if not correos:
        await update.message.reply_text("No tienes correos.")
        return
    total = sum(len(seen_messages.get(c["mail_id"], set())) for c in correos)
    await update.message.reply_text(f"Mensajes procesados en total: {total}")

# ---------------- Inicialización ----------------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_email))
    app.add_handler(CommandHandler("list", list_emails))
    app.add_handler(CommandHandler("delete", delete_email))
    app.add_handler(CommandHandler("inbox", inbox))

    # Poller en background
    async def start_polling_background():
        asyncio.create_task(poll_emails(app))
        print("Poller iniciado en background...")

    asyncio.get_event_loop().create_task(start_polling_background())
    print("Bot iniciado y poller corriendo...")
    app.run_polling()
