import os
import asyncio
import http.client
import json
import re
import random
import string
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("TOKEN")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
TEMPMAIL_HOST = "privatix-temp-mail-v1.p.rapidapi.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")  # OTP de 4-8 dígitos

# ---------------- Almacenamiento ----------------
usuarios = {}        # chat_id -> lista de correos
seen_messages = {}   # email -> set(mail_id) procesados

# ---------------- Utilidades ----------------
def generar_correo_temporal():
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{suffix}@privatix.com"

def list_messages(email):
    conn = http.client.HTTPSConnection(TEMPMAIL_HOST)
    headers = {
        'x-rapidapi-key': RAPIDAPI_KEY,
        'x-rapidapi-host': TEMPMAIL_HOST
    }
    url = f"/request/mail/id/{email}/"
    conn.request("GET", url, headers=headers)
    res = conn.getresponse()
    data = res.read()
    try:
        msgs = json.loads(data)
    except:
        msgs = []
    return msgs

def delete_mail(mail_id):
    conn = http.client.HTTPSConnection(TEMPMAIL_HOST)
    headers = {
        'x-rapidapi-key': RAPIDAPI_KEY,
        'x-rapidapi-host': TEMPMAIL_HOST
    }
    url = f"/request/delete/id/{mail_id}/"
    conn.request("GET", url, headers=headers)
    res = conn.getresponse()
    data = res.read()
    return data.decode("utf-8")

# ---------------- Poller ----------------
async def poll_emails(app):
    while True:
        try:
            for chat_id, correos in usuarios.items():
                for email in correos:
                    seen = seen_messages.setdefault(email, set())
                    msgs = list_messages(email)
                    for m in msgs:
                        mail_id = str(m.get("id"))
                        if mail_id in seen:
                            continue
                        seen.add(mail_id)
                        body = m.get("body") or ""
                        match = OTP_REGEX.search(body)
                        otp = match.group(0) if match else None
                        texto = f"📲 Nuevo OTP recibido en {email}:\n{otp}" if otp else f"📧 Nuevo correo en {email}:\n{body[:300]}"
                        try:
                            await app.bot.send_message(chat_id=chat_id, text=texto)
                        except Exception as e:
                            print("Error enviando Telegram:", e)
                        delete_mail(mail_id)
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
        "Usa /new para crear un nuevo correo temporal.\n"
        "Usa /list para ver tus correos.\n"
        "Usa /delete <correo> para eliminar uno.\n"
        "Recibirás automáticamente los OTP/mensajes de tus correos."
    )

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correo = generar_correo_temporal()
    usuarios.setdefault(chat_id, []).append(correo)
    seen_messages[correo] = set()
    await update.message.reply_text(f"✅ Nuevo correo creado: {correo}")

async def list_emails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correos = usuarios.get(chat_id, [])
    if not correos:
        await update.message.reply_text("No tienes correos asignados. Usa /new para crear uno.")
        return
    texto = "\n".join(correos)
    await update.message.reply_text(f"📬 Tus correos:\n{texto}")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correos = usuarios.get(chat_id, [])
    if not context.args:
        await update.message.reply_text("Usa: /delete <correo>")
        return
    correo = context.args[0]
    if correo not in correos:
        await update.message.reply_text("Correo no encontrado en tu lista.")
        return
    correos.remove(correo)
    seen_messages.pop(correo, None)
    await update.message.reply_text(f"🗑 Correo eliminado: {correo}")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    correos = usuarios.get(chat_id, [])
    if not correos:
        await update.message.reply_text("No tienes correos asignados.")
        return
    total = sum(len(seen_messages.get(email, set())) for email in correos)
    await update.message.reply_text(f"Mensajes procesados en total: {total}")

# ---------------- Inicialización ----------------
async def on_startup(app):
    asyncio.create_task(poll_emails(app))
    print("Poller de emails iniciado en background...")

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TOKEN).build()

    # Añadir handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_email))
    app.add_handler(CommandHandler("list", list_emails))
    app.add_handler(CommandHandler("delete", delete_email))
    app.add_handler(CommandHandler("inbox", inbox))

    # Ejecutar bot y lanzar poller al inicio
    app.run_polling(post_init=on_startup)
