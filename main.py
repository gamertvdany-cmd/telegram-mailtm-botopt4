import random
import string

async def crear_correo_temporal():
    async with httpx.AsyncClient() as client:
        # Obtener dominio disponible
        r = await client.get(f"{MAILTM_BASE}/domains")
        dominios = r.json().get("hydra:member", [])
        dominio = dominios[0]["domain"] if dominios else "mail.tm"

        # Generar email válido
        nombre = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"{nombre}@{dominio}"
        password = "Temp1234!"  # mínimo 8 caracteres, mayúscula, número y símbolo

        # Crear cuenta
        payload = {"address": email, "password": password}
        r = await client.post(f"{MAILTM_BASE}/accounts", json=payload)
        if r.status_code not in [200, 201]:
            print("Error Mail.tm:", r.status_code, r.text)
            return None

        # Obtener token
        r = await client.post(f"{MAILTM_BASE}/token", json=payload)
        if r.status_code != 200:
            print("Error Mail.tm token:", r.status_code, r.text)
            return None
        token = r.json()["token"]

        # Obtener id de la cuenta
        r = await client.get(f"{MAILTM_BASE}/me", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            print("Error Mail.tm me:", r.status_code, r.text)
            return None
        id_ = r.json()["id"]

        return {"email": email, "token": token, "id": id_}

