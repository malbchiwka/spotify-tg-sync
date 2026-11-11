import os
import re
import sys
import glob
import time
import json
import socket
import ctypes
import struct
import asyncio
import logging
import threading
import subprocess
from pathlib import Path
from ctypes import wintypes

from PIL import Image, ImageDraw
import pystray
from telethon import TelegramClient
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.tl.functions.account import UpdateProfileRequest
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus
)

# Скрываем консольное окно сразу при запуске
try:
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "service.log"
SESSION_FILE = BASE_DIR / "independent_tg_session.session"
SESSION_PREFIX = str(BASE_DIR / "independent_tg_session")

handlers = [logging.FileHandler(LOG_FILE, encoding='utf-8')]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=handlers
)
logger = logging.getLogger("SpotifyTGBio")

def find_spotify_ldb():
    base = os.path.expandvars(r'%LOCALAPPDATA%\Spotify\Users')
    if os.path.exists(base):
        for u in os.listdir(base):
            cand = os.path.join(base, u, 'primary.ldb')
            if os.path.exists(cand):
                return cand
    return None

SPOTIFY_LDB_BASE = find_spotify_ldb()

STATE = {
    "status_text": "Запуск...",
    "paused": False,
    "stop_event": False,
    "tray_icon": None
}

# --- Spotify LDB & RAM Metadata Extractor ---
k32 = ctypes.windll.kernel32
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress', ctypes.c_size_t),
        ('AllocationBase', ctypes.c_size_t),
        ('AllocationProtect', wintypes.DWORD),
        ('PartitionId', wintypes.WORD),
        ('RegionSize', ctypes.c_size_t),
        ('State', wintypes.DWORD),
        ('Protect', wintypes.DWORD),
        ('Type', wintypes.DWORD),
    ]

k32.OpenProcess.restype = wintypes.HANDLE
k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

pat_trk = re.compile(rb'!col#col\.trk#\$spotify:track:([A-Za-z0-9]{22})#')
pat_xm  = re.compile(rb'!xmeta#cache#\x01\*#\$spotify:track:([A-Za-z0-9]{22})#')

TRACK_ARTISTS_CACHE = {}

def snappy_decompress(data):
    pos = 0; n = len(data); uncomp_len = 0; shift = 0
    while pos < n:
        b = data[pos]; pos += 1
        uncomp_len |= (b & 0x7f) << shift
        if not (b & 0x80): break
        shift += 7
    out = bytearray()
    while pos < n:
        tag = data[pos]; pos += 1; et = tag & 3
        if et == 0:
            ln = (tag >> 2) + 1
            if ln > 60:
                eb = ln - 60
                ln = int.from_bytes(data[pos:pos+eb], 'little') + 1; pos += eb
            out.extend(data[pos:pos+ln]); pos += ln
        elif et == 1:
            ln = ((tag >> 2) & 7) + 4
            off = ((tag & 0xe0) << 3) | data[pos]; pos += 1
            for _ in range(ln): out.append(out[-off])
        elif et == 2:
            ln = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos+2], 'little'); pos += 2
            for _ in range(ln): out.append(out[-off])
        elif et == 3:
            ln = (tag >> 2) + 1
            off = int.from_bytes(data[pos:pos+4], 'little'); pos += 4
            for _ in range(ln): out.append(out[-off])
    return bytes(out)

def read_varint(buf, pos):
    val = 0; shift = 0
    while True:
        b = buf[pos]; pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80): return val, pos
        shift += 7

def parse_block(block):
    num_restarts = struct.unpack('<I', block[-4:])[0]
    limit = len(block) - 4 * (num_restarts + 1)
    pos = 0; last_key = b''; items = []
    while pos < limit:
        shared, pos = read_varint(block, pos)
        non_shared, pos = read_varint(block, pos)
        val_len, pos = read_varint(block, pos)
        key_delta = block[pos:pos+non_shared]; pos += non_shared
        val = block[pos:pos+val_len]; pos += val_len
        key = last_key[:shared] + key_delta; last_key = key
        items.append((key, val))
    return items

def parse_ldb(filepath):
    with open(filepath, 'rb') as f: data = f.read()
    footer = data[-48:]; pos = 0
    _, pos = read_varint(footer, pos); _, pos = read_varint(footer, pos)
    idx_off, pos = read_varint(footer, pos); idx_sz, pos = read_varint(footer, pos)
    idx_raw = data[idx_off:idx_off+idx_sz]
    idx_block = snappy_decompress(idx_raw) if data[idx_off+idx_sz] == 1 else idx_raw
    all_kv = []
    for _, handle in parse_block(idx_block):
        hpos = 0
        boff, hpos = read_varint(handle, hpos); bsz, hpos = read_varint(handle, hpos)
        braw = data[boff:boff+bsz]
        bdata = snappy_decompress(braw) if data[boff+bsz] == 1 else braw
        for k, v in parse_block(bdata):
            user_key = k[:-8]
            seq_type = struct.unpack('<Q', k[-8:])[0]
            all_kv.append((user_key, v, seq_type & 0xff, seq_type >> 8))
    return all_kv

def parse_proto_multi(v):
    pos = 0; fields = {}
    while pos < len(v):
        try: tag, pos = read_varint(v, pos)
        except IndexError: break
        fnum = tag >> 3; wtype = tag & 7
        if wtype == 0: val, pos = read_varint(v, pos)
        elif wtype == 2:
            ln, pos = read_varint(v, pos)
            val = v[pos:pos+ln]; pos += ln
        elif wtype == 5: val = v[pos:pos+4]; pos += 4
        elif wtype == 1: val = v[pos:pos+8]; pos += 8
        else: break
        fields.setdefault(fnum, []).append(val)
    return fields

def _store_in_cache(title, artists):
    if not title or title == '?' or not artists:
        return
    t_low = title.strip().lower()
    a0_low = artists[0].strip().lower()
    prev = TRACK_ARTISTS_CACHE.get((t_low, a0_low))
    if not prev or len(artists) >= len(prev):
        TRACK_ARTISTS_CACHE[(t_low, a0_low)] = artists
    prev_t = TRACK_ARTISTS_CACHE.get((t_low, ""))
    if not prev_t or len(artists) >= len(prev_t):
        TRACK_ARTISTS_CACHE[(t_low, "")] = artists

def load_ldb_cache():
    try:
        if not SPOTIFY_LDB_BASE or not os.path.exists(SPOTIFY_LDB_BASE):
            return
        for fn in sorted(glob.glob(SPOTIFY_LDB_BASE + r'\*.ldb')):
            try:
                for k, v, vt, seq in parse_ldb(fn):
                    if vt != 1 or not v:
                        continue
                    if k.startswith(b'!col#col.trk#$'):
                        tf = parse_proto_multi(v)
                        title = tf.get(2, [b'?'])[0].decode('utf-8', 'ignore')
                        artists = [a.decode('utf-8', 'ignore') for a in tf.get(8, [])]
                        _store_in_cache(title, artists)
                    elif k.startswith(b'!xmeta#cache#\x01*#$spotify:track:'):
                        af = parse_proto_multi(v)
                        if 2 in af:
                            any_f = parse_proto_multi(af[2][0])
                            if 2 in any_f:
                                tr_f = parse_proto_multi(any_f[2][0])
                                title = tr_f.get(2, [b'?'])[0].decode('utf-8', 'ignore')
                                artists = []
                                for art_b in tr_f.get(4, []):
                                    art_f = parse_proto_multi(art_b)
                                    if 2 in art_f:
                                        artists.append(art_f[2][0].decode('utf-8', 'ignore'))
                                _store_in_cache(title, artists)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"LDB cache load error: {e}")

def scan_spotify_ram():
    try:
        out = subprocess.check_output(
            'powershell -NoProfile -WindowStyle Hidden -Command "(Get-Process Spotify -ErrorAction SilentlyContinue).Id"',
            shell=True, text=True, creationflags=0x08000000
        )
        pids = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        for pid in pids:
            hproc = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
            if not hproc:
                continue
            mbi = MEMORY_BASIC_INFORMATION()
            addr = 0
            while k32.VirtualQueryEx(hproc, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                if mbi.State == 0x1000 and (mbi.Protect & 0x04) and mbi.RegionSize < 50_000_000:
                    buf = ctypes.create_string_buffer(mbi.RegionSize)
                    nread = ctypes.c_size_t(0)
                    if k32.ReadProcessMemory(hproc, mbi.BaseAddress, buf, mbi.RegionSize, ctypes.byref(nread)):
                        raw = buf.raw[:nread.value]
                        for m in pat_trk.finditer(raw):
                            pos = m.end() + 8
                            if pos < len(raw):
                                try:
                                    vlen, vpos = read_varint(raw, pos)
                                    if 0 < vlen < 2000 and vpos + vlen <= len(raw):
                                        v = raw[vpos:vpos+vlen]
                                        tf = parse_proto_multi(v)
                                        title = tf.get(2, [b'?'])[0].decode('utf-8', 'ignore')
                                        artists = [a.decode('utf-8', 'ignore') for a in tf.get(8, [])]
                                        _store_in_cache(title, artists)
                                except Exception:
                                    pass
                        for m in pat_xm.finditer(raw):
                            for offset in (8, 0):
                                pos = m.end() + offset
                                if pos < len(raw):
                                    try:
                                        vlen, vpos = read_varint(raw, pos)
                                        if 0 < vlen < 5000 and vpos + vlen <= len(raw):
                                            v = raw[vpos:vpos+vlen]
                                            af = parse_proto_multi(v)
                                            if 2 in af:
                                                any_f = parse_proto_multi(af[2][0])
                                                if 2 in any_f:
                                                    tr_f = parse_proto_multi(any_f[2][0])
                                                    title = tr_f.get(2, [b'?'])[0].decode('utf-8', 'ignore')
                                                    artists = []
                                                    for art_b in tr_f.get(4, []):
                                                        art_f = parse_proto_multi(art_b)
                                                        if 2 in art_f:
                                                            artists.append(art_f[2][0].decode('utf-8', 'ignore'))
                                                    _store_in_cache(title, artists)
                                    except Exception:
                                        pass
                addr += mbi.RegionSize
            k32.CloseHandle(hproc)
    except Exception as e:
        logger.debug(f"RAM scan error: {e}")

def resolve_full_artists(title: str, smtc_artist: str) -> list[str]:
    t_low = title.strip().lower()
    a_low = smtc_artist.strip().lower()

    cached = TRACK_ARTISTS_CACHE.get((t_low, a_low)) or TRACK_ARTISTS_CACHE.get((t_low, ""))
    if not cached or len(cached) == 1:
        scan_spotify_ram()
        cached = TRACK_ARTISTS_CACHE.get((t_low, a_low)) or TRACK_ARTISTS_CACHE.get((t_low, ""))

    if cached:
        return cached

    if smtc_artist:
        return [a.strip() for a in smtc_artist.replace(";", ",").split(",") if a.strip()]
    return []

async def get_current_spotify_track():
    try:
        manager = await SessionManager.request_async()
        sessions = manager.get_sessions()
        for session in sessions:
            app_id = (session.source_app_user_model_id or "").lower()
            if "spotify" in app_id:
                info = session.get_playback_info()
                if info and info.playback_status == PlaybackStatus.PLAYING:
                    props = await session.try_get_media_properties_async()
                    if props and props.title:
                        return {
                            "title": props.title.strip(),
                            "artist": (props.artist or "").strip()
                        }
    except Exception as e:
        logger.debug(f"Media session error: {e}")
    return None

def format_bio(track):
    if not track:
        return "Слушает: -"
    
    title = track["title"]
    smtc_artist = track["artist"]
    artists_list = resolve_full_artists(title, smtc_artist)

    if artists_list:
        artist_formatted = ", ".join(artists_list[:2])
        text = f"Слушает: {title} – {artist_formatted}"
    else:
        text = f"Слушает: {title}"
    
    if len(text) > 70:
        text = text[:69].rstrip() + "…"
    return text

def update_tray_tooltip(text: str):
    STATE["status_text"] = text
    icon = STATE.get("tray_icon")
    if icon:
        try:
            icon.title = f"Spotify TG (+888)\n{text}"[:127]
            icon.update_menu()
        except Exception:
            pass

def get_independent_client():
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
                logger.info(f"Используется локальный прокси TgWsProxy ({host}:{port})")
                return TelegramClient(
                    SESSION_PREFIX, 2040, 'b18441a1ff607e10a989891a5462e627',
                    connection=ConnectionTcpMTProxyRandomizedIntermediate,
                    proxy=(host, port, secret),
                    device_model="Spotify Bio Service",
                    system_version="Windows 11",
                    app_version="1.0"
                )
        except Exception as e:
            logger.debug(f"TgWsProxy недоступен, прямое подключение: {e}")
    return TelegramClient(
        SESSION_PREFIX, 2040, 'b18441a1ff607e10a989891a5462e627',
        device_model="Spotify Bio Service",
        system_version="Windows 11",
        app_version="1.0"
    )

async def sync_loop():
    logger.info("Загрузка локального кэша треков Spotify...")
    load_ldb_cache()
    scan_spotify_ram()
    logger.info(f"Загружено треков в кэш: {len(TRACK_ARTISTS_CACHE) // 2}")

    while not STATE["stop_event"]:
        if not SESSION_FILE.exists():
            update_tray_tooltip("Требуется вход (запустите auth_setup.bat)")
            logger.warning("Файл сессии independent_tg_session.session не найден. Запустите auth_setup.bat!")
            for _ in range(10):
                if STATE["stop_event"]:
                    break
                await asyncio.sleep(1)
            continue

        try:
            update_tray_tooltip("Подключение к Telegram...")
            logger.info("Подключение к независимой сессии Telegram (+888)...")
            client = get_independent_client()
            await client.connect()
            
            if not await client.is_user_authorized():
                update_tray_tooltip("Требуется авторизация (auth_setup.bat)")
                logger.warning("Сессия не авторизована! Запустите auth_setup.bat.")
                await client.disconnect()
                await asyncio.sleep(10)
                continue

            me = await client.get_me()
            logger.info(f"Успешно подключен (независимая сессия): {me.first_name} (ID: {me.id})")

            current_bio = None
            last_track_key = None

            while not STATE["stop_event"]:
                try:
                    if not client.is_connected():
                        logger.warning("Соединение с Telegram потеряно, переподключение...")
                        break

                    if STATE["paused"]:
                        update_tray_tooltip("На паузе (синхронизация отключена)")
                        await asyncio.sleep(2)
                        continue

                    track = await get_current_spotify_track()
                    track_key = (track["title"], track["artist"]) if track else None
                    
                    if track and track_key != last_track_key:
                        scan_spotify_ram()
                        last_track_key = track_key
                    elif not track:
                        last_track_key = None

                    target_bio = format_bio(track)

                    if target_bio != current_bio:
                        logger.info(f"Обновление описания: '{target_bio}'")
                        await client(UpdateProfileRequest(about=target_bio))
                        current_bio = target_bio
                        update_tray_tooltip(target_bio)

                except Exception as e:
                    logger.error(f"Ошибка в цикле проверки трека: {e}")
                    if not client.is_connected():
                        break

                await asyncio.sleep(4)

            await client.disconnect()

        except Exception as e:
            update_tray_tooltip("Ожидание сети / прокси...")
            logger.error(f"Ошибка подключения Telegram: {e}. Повтор через 15 секунд...")
            for _ in range(15):
                if STATE["stop_event"]:
                    break
                await asyncio.sleep(1)

# --- System Tray Icon ---
def create_tray_image(paused=False):
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg_color = (120, 120, 120, 255) if paused else (29, 185, 84, 255)
    draw.ellipse((4, 4, 60, 60), fill=bg_color)
    draw.arc((16, 20, 48, 42), start=200, end=340, fill=(255, 255, 255, 255), width=5)
    draw.arc((19, 28, 45, 48), start=200, end=340, fill=(255, 255, 255, 255), width=4)
    draw.arc((22, 36, 42, 54), start=200, end=340, fill=(255, 255, 255, 255), width=4)
    return img

def on_toggle_pause(icon, item):
    STATE["paused"] = not STATE["paused"]
    icon.icon = create_tray_image(paused=STATE["paused"])
    if STATE["paused"]:
        update_tray_tooltip("На паузе")
    else:
        update_tray_tooltip("Активно")

def on_open_log(icon, item):
    if os.path.exists(LOG_FILE):
        os.startfile(str(LOG_FILE))

def on_exit(icon, item):
    STATE["stop_event"] = True
    icon.stop()
    os._exit(0)

def run_background_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(sync_loop())

def main():
    worker = threading.Thread(target=run_background_thread, daemon=True)
    worker.start()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: f"Статус: {STATE['status_text']}", lambda icon, item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda item: "Возобновить" if STATE["paused"] else "Приостановить",
            on_toggle_pause
        ),
        pystray.MenuItem("Открыть лог", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", on_exit)
    )

    icon = pystray.Icon(
        "SpotifyTGBio",
        create_tray_image(paused=False),
        "Spotify TG (+888)",
        menu
    )
    STATE["tray_icon"] = icon
    icon.run()

if __name__ == "__main__":
    main()
