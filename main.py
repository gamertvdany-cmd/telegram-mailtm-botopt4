import os
import asyncio
import httpx
import json
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ.get("TOKEN")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
OTP_REGEX = r"\b(\d{4,8})\b"

usuarios = {}  # chat_id -> list of {"email":..., "token":..., "id":...}
seen_messages = {}  # message_id ya procesados

MAILTM_BASE = "https://api.mail.tm"

# ---------------- Funciones Mail.tm ----------------
async def crear_correo_temporal():
    async with httpx.AsyncClient() as client:
        # Obtener dominio disponible
        r = await client.get(f"{MAILTM_BASE}/domains")
        dominios = r.json()["hydra:member"]
        dominio = dominios[0]["domain"] if dominios else "mail.tm"

        # Generar email aleatorio
        nombre = ''.join([chr(c) for c in os.urandom(6)])
        email = f"{nombre}@{dominio}"
        password = "Temp1234!"  # password para API

        # Crear cuenta
        payload = {"address": email, "password": password}
        r = await client.post(f"{MAILTM_BASE}/accounts", json=payload)
        if r.status_code not in [200, 201]:
            return None, None

        # Obtener token de sesión
        r = await client.post(f"{MAILTM_BASE}/token", json=payload)
        token = r.json()["token"]
        # Obtener id de la cuenta
        r = await client.get(f"{MAILTM_BASE}/me", headers={"Authorization": f"Bearer {token}"})
        id_ = r.json()["id"]
        return {"email": email, "token": token, "id": id_}

async def list_messages(account):
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        r = await client.get(f"{MAILTM_BASE}/messages", headers=headers)
        try:
            return r.json()["hydra:member"]
        except:
            return []

async def delete_message(account, message_id):
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        await client.delete(f"{MAILTM_BASE}/messages/{message_id}", headers=headers)

# ---------------- Poller ----------------
async def poll_emails(app):
    while True:
        try:
            for chat_id, accounts in usuarios.items():
                for acc in accounts:
                    messages = await list_messages(acc)
                    for m in messages:
                        message_id = m["id"]
                        if message_id in seen_messages:
                            continue
                        seen_messages[message_id] = True
                        body = m.get("text") or m.get("html") or ""
                        import re
                        match = re.search(OTP_REGEX, body)
                        otp = match.group(0) if match else None
                        texto = f"📲 Nuevo OTP en {acc['email']}:\n{otp}" if otp else f"📧 Nuevo mensaje en {acc['email']}:\n{body[:300]}"
                        try:
                            await app.bot.send_message(chat_id=chat_id, text=texto)
                        except Exception as e:
                            print("Error enviando Telegram:", e)
                        await delete_message(acc, message_id)
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
    account = await crear_correo_temporal()
    if not account:
        await update.message.reply_text("❌ Error creando correo temporal")
        return
    usuarios.setdefault(chat_id, []).append(account)
    await update.message.reply_text(f"✅ Nuevo correo creado: {account['email']}")

async def list_emails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    accounts = usuarios.get(chat_id, [])
    if not accounts:
        await update.message.reply_text("No tienes correos. Usa /new.")
        return
    texto = "\n".join([acc["email"] for acc in accounts])
    await update.message.reply_text(f"📬 Tus correos:\n{texto}")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    accounts = usuarios.get(chat_id, [])
    if not context.args:
        await update.message.reply_text("Usa: /delete <correo>")
        return
    correo = context.args[0]
    for acc in accounts:
        if acc["email"] == correo:
            accounts.remove(acc)
            await update.message.reply_text(f"🗑 Correo eliminado: {correo}")
            return
    await update.message.reply_text("Correo no encontrado.")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    accounts = usuarios.get(chat_id, [])
    if not accounts:
        await update.message.reply_text("No tienes correos.")
        return
    total = sum(1 for acc in accounts for m in await list_messages(acc))
    await update.message.reply_text(f"Mensajes en total: {total}")

# ---------------- Inicialización ----------------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TOKEN).build()

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
