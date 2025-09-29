import os
import asyncio
import httpx
import json
import re
import random
import string
import time
import tempfile
import imgkit
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")  # tu Telegram user ID
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
DATA_FILE = "data.json"
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
MAILTM_BASE = "https://api.mail.tm"

# ---------------- Persistencia ----------------
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"usuarios": {}, "keys": {}, "redemptions": {}}
    return {"usuarios": {}, "keys": {}, "redemptions": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

data = load_data()
seen_messages = set()

# ---------------- Mail.tm ----------------
async def crear_correo_temporal():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{MAILTM_BASE}/domains")
        dominios = r.json().get("hydra:member", [])
        if not dominios: return None
        dominio = dominios[0]["domain"]

        nombre = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"{nombre}@{dominio}"
        password = "Temp1234!"

        payload = {"address": email, "password": password}
        r = await client.post(f"{MAILTM_BASE}/accounts", json=payload)
        if r.status_code not in (200,201): return None

        r = await client.post(f"{MAILTM_BASE}/token", json=payload)
        if r.status_code != 200: return None
        token = r.json().get("token")

        r = await client.get(f"{MAILTM_BASE}/me", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200: return None
        id_ = r.json().get("id")
        return {"email": email, "token": token, "id": id_}

async def list_messages(account):
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        r = await client.get(f"{MAILTM_BASE}/messages", headers=headers)
        if r.status_code != 200: return []
        return r.json().get("hydra:member", [])

async def delete_message(account, message_id):
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        await client.delete(f"{MAILTM_BASE}/messages/{message_id}", headers=headers)

# ---------------- HTML a texto ----------------
def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n", strip=True)

def extract_otp_from_text(text):
    m = OTP_REGEX.search(text or "")
    return m.group(0) if m else None

# ---------------- Keys ----------------
def gen_key_string(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def create_key(days):
    k = gen_key_string(14)
    ts = int(time.time())
    data["keys"][k] = {"days": int(days), "created": ts, "used_by": None, "used_at": None}
    save_data(data)
    return k

def redeem_key_for_chat(key, chat_id_str):
    now = int(time.time())
    kinfo = data["keys"].get(key)
    if not kinfo: return False, "Key no encontrada"
    if kinfo.get("used_by"): return False, "Key ya usada"
    days = int(kinfo["days"])
    expiry = now + days * 86400
    data["redemptions"][chat_id_str] = {"expiry": expiry}
    kinfo["used_by"] = chat_id_str
    kinfo["used_at"] = now
    save_data(data)
    return True, expiry

def is_active(chat_id_str):
    r = data["redemptions"].get(chat_id_str)
    if not r: return False
    return int(time.time()) < int(r.get("expiry", 0))

def extend_redemption(chat_id_str, days):
    now = int(time.time())
    current = data["redemptions"].get(chat_id_str)
    if current and int(current.get("expiry",0)) > now:
        new_expiry = int(current["expiry"]) + int(days)*86400
    else:
        new_expiry = now + int(days)*86400
    data["redemptions"][chat_id_str] = {"expiry": new_expiry}
    save_data(data)
    return new_expiry

def revoke_chat(chat_id_str):
    if chat_id_str in data["redemptions"]:
        del data["redemptions"][chat_id_str]
        save_data(data)
        return True
    return False

# ---------------- Poller ----------------
async def poll_emails(app):
    while True:
        try:
            for chat_id_str, accounts in list(data["usuarios"].items()):
                if not is_active(chat_id_str): continue
                for acc in accounts:
                    messages = await list_messages(acc)
                    for m in messages:
                        message_id = m.get("id")
                        if not message_id or message_id in seen_messages: continue
                        seen_messages.add(message_id)

                        text = m.get("text") or ""
                        html = m.get("html") or ""
                        subject = m.get("subject") or ""
                        body = text if text else html_to_text(html)
                        otp = extract_otp_from_text(body)
                        msg_text = f"📧 `{acc['email']}`\nAsunto: {subject}\n\n"
                        if otp:
                            msg_text = f"📲 OTP detectado: `{otp}`\n\n" + msg_text
                        msg_text += body[:1500] + ("..." if len(body)>1500 else "")

                        # Enviar como imagen (HTML completo)
                        if html:
                            with tempfile.NamedTemporaryFile("w+", suffix=".html", delete=False, encoding="utf-8") as tf:
                                tf.write(html)
                                html_file = tf.name
                            png_file = html_file.replace(".html",".png")
                            try:
                                imgkit.from_file(html_file, png_file)
                                await app.bot.send_photo(chat_id=int(chat_id_str), photo=InputFile(png_file))
                            except Exception as e:
                                print("Error generando imagen HTML:", e)
                            finally:
                                for f in [html_file, png_file]:
                                    if os.path.exists(f): os.remove(f)
                        # Enviar texto también
                        await app.bot.send_message(chat_id=int(chat_id_str), text=msg_text, parse_mode="Markdown")

                        # adjuntos
                        attachments = m.get("attachments", [])
                        for att in attachments:
                            att_url = att.get("url")
                            att_name = att.get("filename", "file")
                            if att_url:
                                try:
                                    async with httpx.AsyncClient(timeout=30) as client:
                                        r = await client.get(att_url, headers={"Authorization": f"Bearer {acc['token']}"})
                                        if r.status_code == 200:
                                            content = r.content
                                            if att_name.lower().endswith((".jpg",".jpeg",".png",".gif")):
                                                await app.bot.send_photo(chat_id=int(chat_id_str), photo=content, caption=att_name)
                                            else:
                                                await app.bot.send_document(chat_id=int(chat_id_str), document=content, filename=att_name)
                                except Exception as e:
                                    print("Error adjunto:", e)

                        await delete_message(acc, message_id)
        except Exception as e:
            print("Error en poller:", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- Comandos ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    data["usuarios"].setdefault(chat_id_str, data["usuarios"].get(chat_id_str, []))
    save_data(data)
    await update.message.reply_text(
        "✅ Bot listo.\nSi tienes key, canjeala con /redeem <KEY>\n"
        "Comandos usuario:\n/redeem <KEY>\n/status\n/new\n/list\n/delete <correo>\n/inbox\n/checkadmin"
    )

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Usa: /redeem <KEY>")
        return
    key = context.args[0].strip().upper()
    ok, res = redeem_key_for_chat(key, chat_id_str)
    if not ok:
        await update.message.reply_text(f"❌ {res}")
        return
    expiry = res
    await update.message.reply_text(f"✅ Key aplicada. Acceso hasta: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry))}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    if is_active(chat_id_str):
        exp = data["redemptions"][chat_id_str]["expiry"]
        await update.message.reply_text(f"🔓 Tu acceso expira el {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(exp)))}")
    else:
        await update.message.reply_text("🔒 No tienes acceso activo. Canjea una key con /redeem <KEY>")

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    if not is_active(chat_id_str):
        await update.message.reply_text("🔒 Necesitas una key válida.")
        return
    account = await crear_correo_temporal()
    if not account:
        await update.message.reply_text("❌ Error creando correo temporal.")
        return
    data["usuarios"].setdefault(chat_id_str, []).append(account)
    save_data(data)
    await update.message.reply_text(f"✅ Correo creado: `{account['email']}`", parse_mode="Markdown")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    accounts = data["usuarios"].get(chat_id_str, [])
    if not accounts:
        await update.message.reply_text("No tienes correos.")
        return
    text = "\n".join([a["email"] for a in accounts])
    await update.message.reply_text(f"📬 Tus correos:\n{text}")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    accounts = data["usuarios"].get(chat_id_str, [])
    if not context.args:
        await update.message.reply_text("Usa: /delete <correo>")
        return
    correo = context.args[0]
    for a in list(accounts):
        if a["email"] == correo:
            accounts.remove(a)
            save_data(data)
            await update.message.reply_text(f"🗑 Correo eliminado: {correo}")
            return
    await update.message.reply_text("Correo no encontrado.")

async def inbox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    accounts = data["usuarios"].get(chat_id_str, [])
    total = 0
    for acc in accounts:
        msgs = await list_messages(acc)
        total += len(msgs)
    await update.message.reply_text(f"Mensajes en bandeja: {total}")

# ---------------- Admin ----------------
def is_owner(update):
    try:
        return str(update.effective_user.id) == str(OWNER_ID)
    except Exception:
        return False

async def checkadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) == str(OWNER_ID):
        await update.message.reply_text(f"✅ Eres el admin. Tu ID: {user_id}")
    else:
        await update.message.reply_text(f"❌ No eres el admin. Tu ID: {user_id}")

async def genkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return
    if not context.args:
        await update.message.reply_text("Usa: /genkey <dias>")
        return
