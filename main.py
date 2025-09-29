# main.py
import os
import asyncio
import httpx
import json
import re
import random
import string
import time
import tempfile
from bs4 import BeautifulSoup
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")  # tu id de Telegram (como string). Solo este usuario puede generar keys.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 10))
DATA_FILE = "data.json"
OTP_REGEX = re.compile(r"\b(\d{4,8})\b")
MAILTM_BASE = "https://api.mail.tm"

# ---------------- Utilidades de persistencia ----------------
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
# estructura:
# data["usuarios"] : chat_id(str) -> list of account dicts {email, token, id}
# data["keys"] : key_str -> {"days":int, "created":ts, "used_by": chat_id or None, "used_at":ts or None}
# data["redemptions"]: chat_id(str) -> {"expiry": timestamp}

seen_messages = set()

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
            print(f"list_messages error {account.get('email')}: {r.status_code} {r.text[:200]}")
            return []
        try:
            return r.json().get("hydra:member", [])
        except Exception:
            return []

async def delete_message(account, message_id):
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {account['token']}"}
        await client.delete(f"{MAILTM_BASE}/messages/{message_id}", headers=headers)

# ---------------- HTML/text helpers ----------------
def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n", strip=True)
    return text

def extract_otp_from_text(text):
    m = OTP_REGEX.search(text or "")
    return m.group(0) if m else None

# ---------------- License / key helpers ----------------
def gen_key_string(length=12):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(random.choices(alphabet, k=length))

def create_key(days):
    k = gen_key_string(14)
    ts = int(time.time())
    data["keys"][k] = {"days": int(days), "created": ts, "used_by": None, "used_at": None}
    save_data(data)
    return k

def redeem_key_for_chat(key, chat_id_str):
    now = int(time.time())
    kinfo = data["keys"].get(key)
    if not kinfo:
        return False, "Key no encontrada"
    if kinfo.get("used_by"):
        return False, "Key ya usada"
    days = int(kinfo["days"])
    expiry = now + days * 86400
    data["redemptions"][chat_id_str] = {"expiry": expiry}
    kinfo["used_by"] = chat_id_str
    kinfo["used_at"] = now
    save_data(data)
    return True, expiry

def is_active(chat_id_str):
    r = data["redemptions"].get(chat_id_str)
    if not r:
        return False
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
                # Only poll for active users
                if not is_active(chat_id_str):
                    continue
                for acc in accounts:
                    messages = await list_messages(acc)
                    if messages:
                        print(f"[DEBUG] {acc['email']} messages_count={len(messages)}")
                    for m in messages:
                        message_id = m.get("id")
                        if not message_id or message_id in seen_messages:
                            continue
                        seen_messages.add(message_id)

                        # Prefer text, else html -> text
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

                        otp = extract_otp_from_text(body)
                        if otp:
                            send_text = f"📲 OTP recibido en `{acc['email']}`:\n`{otp}`\n_origen: {source}_"
                        else:
                            snippet = (body.strip()[:1500] + "...") if body and len(body) > 1500 else (body or "(sin cuerpo legible)")
                            send_text = f"📧 Nuevo mensaje en `{acc['email']}`\nAsunto: {subject}\n\n{snippet}\n\n_origen: {source}_"

                        try:
                            await app.bot.send_message(chat_id=int(chat_id_str), text=send_text, parse_mode="Markdown")
                            # If html present, also send raw html file for inspection
                            if html:
                                with tempfile.NamedTemporaryFile("w+", suffix=".html", delete=False, encoding="utf-8") as tf2:
                                    tf2.write(html)
                                    path_html = tf2.name
                                await app.bot.send_document(chat_id=int(chat_id_str), document=InputFile(path_html), filename=f"{acc['email']}_raw.html")
                                try:
                                    os.remove(path_html)
                                except Exception:
                                    pass
                        except Exception as e:
                            print("Error sending Telegram:", e)

                        # delete message from server
                        try:
                            await delete_message(acc, message_id)
                        except Exception:
                            pass
        except Exception as e:
            print("Error in poll_emails:", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- Telegram command handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    data["usuarios"].setdefault(chat_id_str, data["usuarios"].get(chat_id_str, []))
    save_data(data)
    await update.message.reply_text(
        "✅ Bot listo.\n"
        "Si ya tienes key, canjeala con /redeem <KEY>\n"
        "Comandos:\n/redeem <KEY>\n/status\n/new (crear correo, requiere key activa)\n/list\n/delete <correo>\n/inbox"
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

# user commands guarded by license
async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    if not is_active(chat_id_str):
        await update.message.reply_text("🔒 Necesitas una key válida. Pide una o canjea con /redeem <KEY>")
        return
    account = await crear_correo_temporal()
    if not account:
        await update.message.reply_text("❌ Error creando correo temporal. Revisa logs.")
        return
    data["usuarios"].setdefault(chat_id_str, []).append(account)
    save_data(data)
    await update.message.reply_text(f"✅ Correo creado: `{account['email']}`", parse_mode="Markdown")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    accounts = data["usuarios"].get(chat_id_str, [])
    if not accounts:
        await update.message.reply_text("No tienes correos. Usa /new (requiere key activa).")
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

# ---------------- Admin commands ----------------
def is_owner(update):
    try:
        return str(update.effective_user.id) == str(OWNER_ID)
    except Exception:
        return False

async def genkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return
    if not context.args:
        await update.message.reply_text("Usa: /genkey <dias>")
        return
    days = int(context.args[0])
    key = create_key(days)
    await update.message.reply_text(f"🔑 Key generada: `{key}` válida por {days} días", parse_mode="Markdown")

async def listkeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return
    lines = []
    for k, v in data["keys"].items():
        used = v.get("used_by")
        lines.append(f"{k} — {v['days']}d — used_by={used}")
    await update.message.reply_text("Keys:\n" + ("\n".join(lines) if lines else "(ninguna)"))

async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return
    if not context.args:
        await update.message.reply_text("Usa: /revoke <chat_id>")
        return
    chat_id_str = str(context.args[0])
    ok = revoke_chat(chat_id_str)
    await update.message.reply_text("✅ Revocado" if ok else "No encontrado")

async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Solo el admin puede usar este comando.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usa: /extend <chat_id> <dias>")
        return
    chat_id_str = str(context.args[0])
    days = int(context.args[1])
    new_exp = extend_redemption(chat_id_str, days)
    await update.message.reply_text(f"✅ Expiración extendida hasta {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(new_exp))}")

# ---------------- Inicialización ----------------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    app = ApplicationBuilder().token(TOKEN).build()

    # user commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("new", new_email))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("inbox", inbox_cmd))

    # admin
    app.add_handler(CommandHandler("genkey", genkey_cmd))
    app.add_handler(CommandHandler("listkeys", listkeys_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))

    # start poller in background
    async def start_polling_background():
        asyncio.create_task(poll_emails(app))
        print("Poller iniciado en background...")

    asyncio.get_event_loop().create_task(start_polling_background())
    print("Bot iniciado y poller corriendo...")
    app.run_polling()
