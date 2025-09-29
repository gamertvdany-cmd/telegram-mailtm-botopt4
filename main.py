# main.py
import os
import asyncio
import httpx
import json
import re
import random
import string
import tempfile
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------- CONFIG ----------
TOKEN = os.environ.get("TOKEN")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
DATA_FILE = "usuarios.json"
MAILTM_BASE = "https://api.mail.tm"

# ---------- Persistencia ----------
def cargar_usuarios():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def guardar_usuarios(usuarios):
    with open(DATA_FILE, "w") as f:
        json.dump(usuarios, f)

usuarios = cargar_usuarios()   # chat_id (str) -> list of account dicts {email, token, id}
seen_messages = {}             # message_id -> True

# ---------- Mail.tm helpers ----------
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
        password = "Temp1234!"  # cumple requisitos básicos

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

# ---------- Extractor and formatter ----------
def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    return text

def extract_otp_from_text(text):
    m = OTP_REGEX.search(text or "")
    return m.group(0) if m else None

# ---------- Poller ----------
async def poll_emails(app):
    while True:
        try:
            for chat_id_str, accounts in list(usuarios.items()):
                for acc in accounts:
                    messages = await list_messages(acc)
                    if messages:
                        print(f"[DEBUG] {acc['email']} messages_count={len(messages)}")
                    for m in messages:
                        message_id = m.get("id")
                        if not message_id or message_id in seen_messages:
                            continue
                        seen_messages[message_id] = True

                        # Preferir text plano; si no, convertir html
                        text = m.get("text") or ""
                        html = m.get("html") or ""
                        subject = m.get("subject") or ""

                        if text:
                            body = text
                            source = "text"
                        elif html:
                            body = html_to_text(html)
                            source = "html->text"
                        else:
                            body = ""
                            source = "empty"

                        # buscar OTP
                        otp = extract_otp_from_text(body)
                        # Preparar mensaje a enviar
                        if otp:
                            send_text = f"📲 *OTP recibido* en `{acc['email']}`:\n`{otp}`\n\n_origen: {source}_"
                        else:
                            # incluir subject + snippet; y además enviar TODO el HTML si existe
                            snippet = (body.strip()[:1500] + "...") if body and len(body) > 1500 else (body or "(sin cuerpo legible)")
                            send_text = f"📧 *Nuevo mensaje* en `{acc['email']}`\nAsunto: {subject}\n\n{snippet}\n\n_origen: {source}_"

                        # enviar texto (si no muy largo) o enviar archivo con todo el html + text
                        try:
                            if len(send_text) < 3500:
                                await app.bot.send_message(chat_id=int(chat_id_str), text=send_text, parse_mode="Markdown")
                            else:
                                # si el mensaje de resumen es gigante (poco probable), mandarlo como archivo
                                with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as tf:
                                    tf.write(send_text)
                                    temp_path = tf.name
                                await app.bot.send_document(chat_id=int(chat_id_str), document=InputFile(temp_path))
                                try:
                                    os.remove(temp_path)
                                except Exception:
                                    pass

                            # además, siempre enviar el HTML completo como archivo si existe (seguro que quieres "todo el html")
                            if html:
                                # crear archivo .html con contenido original
                                with tempfile.NamedTemporaryFile("w+", suffix=".html", delete=False, encoding="utf-8") as tf2:
                                    tf2.write(html)
                                    path_html = tf2.name
                                await app.bot.send_document(chat_id=int(chat_id_str), document=InputFile(path_html), filename=f"{acc['email']}_raw.html")
                                try:
                                    os.remove(path_html)
                                except Exception:
                                    pass
                            elif text and len(body) > 1500:
                                # si solo text y muy largo, mandar archivo con body completo
                                with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as tf3:
                                    tf3.write(body)
                                    path_txt = tf3.name
                                await app.bot.send_document(chat_id=int(chat_id_str), document=InputFile(path_txt), filename=f"{acc['email']}_raw.txt")
                                try:
                                    os.remove(path_txt)
                                except Exception:
                                    pass

                        except Exception as e:
                            print("Error enviando Telegram:", e)

                        # borrar mensaje en server (opcional)
                        try:
                            await delete_message(acc, message_id)
                        except Exception:
                            pass

        except Exception as e:
            print("Error en poll_emails:", e)

        await asyncio.sleep(POLL_INTERVAL)

# ---------- Comandos Telegram ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    usuarios.setdefault(chat_id, [])
    guardar_usuarios(usuarios)
    await update.message.reply_text(
        "✅ Bot listo (Mail.tm).\nComandos:\n/new - crear correo\n/list - listar\n/delete <correo> - eliminar\n/inbox - contar mensajes"
    )

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    account = await crear_correo_temporal()
    if not account:
        await update.message.reply_text("❌ Error creando correo temporal. Revisa logs en Render.")
        return
    usuarios.setdefault(chat_id, []).append(account)
    guardar_usuarios(usuarios)
    await update.message.reply_text(f"✅ Correo creado:\n`{account['email']}`", parse_mode="Markdown")

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

# ---------- Inicialización ----------
if __name__ == "__main__":
    import logging, os
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("new", new_email))
    app.add_handler(CommandHandler("list", list_emails))
    app.add_handler(CommandHandler("delete", delete_email))
    app.add_handler(CommandHandler("inbox", inbox))

    async def start_polling_background():
        asyncio.create_task(poll_emails(app))
        print("Poller iniciado en background...")

    # schedule poller before run_polling in current loop
    asyncio.get_event_loop().create_task(start_polling_background())
    print("Bot iniciado y poller corriendo...")
    app.run_polling()
