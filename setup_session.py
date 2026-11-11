import os
import json
import socket
import asyncio
from pathlib import Path
from telethon import TelegramClient
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = str(BASE_DIR / "independent_tg_session")

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

def create_client():
    cfg_path = os.path.expandvars(r'%APPDATA%\TgWsProxy\config.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            host = cfg.get('host', '127.0.0.1')
            port = int(cfg.get('port', 1443))
            secret = cfg.get('secret', '')
            if secret:
                with socket.create_connection((host, port), timeout=1.0):
                    pass
                if not secret.startswith('dd') and len(secret) == 32:
                    secret = 'dd' + secret
                print(f"[*] Используется локальный прокси TgWsProxy ({host}:{port})")
                return TelegramClient(
                    SESSION_FILE, API_ID, API_HASH,
                    connection=ConnectionTcpMTProxyRandomizedIntermediate,
                    proxy=(host, port, secret),
                    device_model="Spotify Bio Service",
                    system_version="Windows 11",
                    app_version="1.0"
                )
        except Exception:
            pass
    return TelegramClient(
        SESSION_FILE, API_ID, API_HASH,
        device_model="Spotify Bio Service",
        system_version="Windows 11",
        app_version="1.0"
    )

async def main():
    print("Подключение к Telegram...")
    client = create_client()
    await client.start(
        phone=lambda: input("Введите номер телефона аккаунта: ").strip(),
        password=lambda: input("Введите облачный пароль (символы будут видны): ").strip()
    )
    me = await client.get_me()
    print(f"\n✅ Успешно создана отдельная независимая сессия для: {me.first_name} (ID: {me.id})")
    print(f"Файл сессии сохранен: {SESSION_FILE}.session")
    print("Теперь Telegram Desktop и скрипт имеют РАЗНЫЕ ключи авторизации и никогда не будут выкидывать друг друга!")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
