import os
import time
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Base directory
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

TG_API_ID = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
DEFAULT_BIO = os.getenv("DEFAULT_BIO", "Listening to music")

SPOTIFY_CACHE_PATH = str(BASE_DIR / ".spotify_cache")
SESSION_PATH = str(BASE_DIR / "tg_session")

MAX_BIO_LENGTH = 70  # Telegram bio hard limit

def format_track_bio(track_name: str, artists: list[str]) -> str:
    """
    Красивое форматирование описания:
    🎧 Track — Artist
    Если не влезает в 70 символов, аккуратно обрезает с '…'.
    """
    artist_str = ", ".join(artists)
    base_prefix = "🎧 "
    candidate = f"{base_prefix}{track_name} — {artist_str}"
    
    if len(candidate) <= MAX_BIO_LENGTH:
        return candidate
    
    # Если слишком длинно, пробуем только первого исполнителя
    if len(artists) > 1:
        candidate_single = f"{base_prefix}{track_name} — {artists[0]}"
        if len(candidate_single) <= MAX_BIO_LENGTH:
            return candidate_single

    # Если всё ещё длинно, обрезаем
    cutoff = MAX_BIO_LENGTH - 1
    return candidate[:cutoff].rstrip() + "…"

def init_spotify():
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope="user-read-currently-playing user-read-playback-state",
        cache_path=SPOTIFY_CACHE_PATH,
        open_browser=True
    )
    return spotipy.Spotify(auth_manager=sp_oauth)

async def main():
    if not TG_API_ID or not TG_API_HASH:
        logger.error("Ошибка: TG_API_ID или TG_API_HASH не указаны в .env!")
        return

    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        logger.error("Ошибка: SPOTIFY_CLIENT_ID или SPOTIFY_CLIENT_SECRET не указаны в .env!")
        return

    logger.info("Инициализация Telegram клиента...")
    client = TelegramClient(SESSION_PATH, int(TG_API_ID), TG_API_HASH)
    await client.start()
    
    me = await client.get_me()
    logger.info(f"Успешный вход в Telegram: @{me.username or me.first_name}")

    logger.info("Инициализация Spotify...")
    sp = init_spotify()

    current_bio_status = None
    last_error_time = 0

    logger.info("Скрипт синхронизации запущен!")

    while True:
        try:
            current = sp.current_playback()
            target_bio = DEFAULT_BIO

            if current and current.get("is_playing") and current.get("item"):
                item = current["item"]
                # Проверяем, что это трек, а не эпизод подкаста
                if item.get("type") == "track":
                    track_name = item.get("name", "Unknown Track")
                    artists = [a["name"] for a in item.get("artists", [])]
                    target_bio = format_track_bio(track_name, artists)

            # Если био изменилось
            if target_bio != current_bio_status:
                logger.info(f"Обновление описания: '{target_bio}'")
                await client(UpdateProfileRequest(about=target_bio))
                current_bio_status = target_bio

        except Exception as e:
            # Чтобы не спамить в лог при временных сетевых сбоях
            now = time.time()
            if now - last_error_time > 30:
                logger.warning(f"Ошибка при обновлении (повтор через 15с): {e}")
                last_error_time = now

        await asyncio.sleep(15)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Скрипт остановлен.")
