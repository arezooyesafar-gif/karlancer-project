import asyncio
import base64
import datetime
import datetime as dt
import difflib
import json
import logging
import math
import os
import random
import re
import secrets
import string
import struct
import subprocess
import sys
import tempfile
import time as _time
import zlib
from logging.handlers import RotatingFileHandler

import aiofiles
import httpx
import socks
from bson.objectid import ObjectId
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from redis.asyncio import Redis
from telethon import Button, TelegramClient, connection, errors, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.functions.account import (
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
)
from urllib.parse import parse_qs, unquote, urlparse

from config import *

_reseller_token = os.getenv("RESELLER_BOT_TOKEN", "").strip()
_reseller_db = os.getenv("RESELLER_DB_NAME", "").strip()
_reseller_admins = os.getenv("RESELLER_ADMINS", "").strip()
_session_name = os.getenv("RESELLER_SESSION", "bot").strip() or "bot"
_log_file = os.getenv("RESELLER_LOG", "bot.log").strip() or "bot.log"
IS_RESELLER = bool(_reseller_token)
if _reseller_token:
    bot_token = _reseller_token
if _reseller_db:
    NAME_DB = _reseller_db
if _reseller_admins:
    ADMINS = []
    for _part in _reseller_admins.split(","):
        _part = _part.strip()
        if _part.lstrip("-").isdigit():
            ADMINS.append(int(_part))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            _log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("reportbot")


class _DropConnectionNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Connection reset" in msg or "during disconnect" in msg:
            return False
        if "Server closed the connection" in msg:
            return False
        return True


for _noisy_logger in (
    "httpx",
    "httpcore",
    "telethon",
    "telethon.network",
    "telethon.network.mtprotosender",
    "telethon.network.connection",
    "telethon.network.connection.connection",
    "telethon.client.updates",
    "telethon.client.telegrambaseclient",
):
    _lg = logging.getLogger(_noisy_logger)
    _lg.setLevel(logging.CRITICAL)
    _lg.addFilter(_DropConnectionNoise())

from emoji_patch import apply_patch

bot = TelegramClient(_session_name, api_id_admin, api_hash_admin).start(bot_token=bot_token)

apply_patch(bot, bot_token)

def normalize_menu_text(text: str) -> str:
    if not text:
        return ""

    text = str(text).strip()
    text = re.sub(r"\S?\\?\[-?\d+\\?\]", " ", text)
    text = re.sub(r"[^0-9A-Za-z\u0600-\u06FF\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

mongo = MongoClient(MONGOURL, serverSelectionTimeoutMS=8000)
try:
    mongo.admin.command("ping")
except Exception as e:
    raise SystemExit(
        f"MongoDB connection failed: {e}\n"
        f"Check MONGOURL in config.py. If auth is enabled use:\n"
        f"mongodb://USER:PASSWORD@127.0.0.1:27017/?authSource=admin"
    ) from e
db = mongo[NAME_DB]
ITEMS_PER_PAGE = 5
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
UPDATE_DEDUP_TTL = 15 * 60


async def _first_delivery(key: str) -> bool:
    try:
        return bool(await redis.set(key, "1", ex=UPDATE_DEDUP_TTL, nx=True))
    except Exception:
        return True


@bot.on(events.CallbackQuery)
async def _drop_duplicate_callback(event):
    if not await _first_delivery(f"upd:cb:{event.query.query_id}"):
        raise events.StopPropagation


@bot.on(events.NewMessage(incoming=True))
async def _drop_duplicate_message(event):
    if not event.is_private:
        return
    if not await _first_delivery(f"upd:msg:{event.chat_id}:{event.id}"):
        raise events.StopPropagation


CTX_TTL = 15 * 60
EXEC_TTL = 30 * 60
LOCK_TTL = 12 * 60

REPORT_CONCURRENCY = int(os.getenv("REPORT_CONCURRENCY", "10"))
REPORT_CONCURRENCY_TEXT = int(os.getenv("REPORT_CONCURRENCY_TEXT", "8"))
REPORT_DELAY_MIN = float(os.getenv("REPORT_DELAY_MIN", "0.15"))
REPORT_DELAY_MAX = float(os.getenv("REPORT_DELAY_MAX", "0.45"))
REPORT_DELAY_TEXT_MIN = float(os.getenv("REPORT_DELAY_TEXT_MIN", "0.2"))
REPORT_DELAY_TEXT_MAX = float(os.getenv("REPORT_DELAY_TEXT_MAX", "0.6"))
REPORT_STEP_DELAY_MIN = float(os.getenv("REPORT_STEP_DELAY_MIN", "0.05"))
REPORT_STEP_DELAY_MAX = float(os.getenv("REPORT_STEP_DELAY_MAX", "0.15"))
REPORT_CONN_ROUNDS = int(os.getenv("REPORT_CONN_ROUNDS", "40"))
REPORT_GLOBAL_MIN_GAP = float(os.getenv("REPORT_GLOBAL_MIN_GAP", "0.05"))
ACCOUNT_OP_TIMEOUT = int(os.getenv("ACCOUNT_OP_TIMEOUT", "50"))
REPORT_MSG_IDS_PER_ACCOUNT = int(os.getenv("REPORT_MSG_IDS_PER_ACCOUNT", "1"))
REPORT_RPC_TIMEOUT = float(os.getenv("REPORT_RPC_TIMEOUT", "12"))
PREFLIGHT_JOIN_TIMEOUT = float(os.getenv("PREFLIGHT_JOIN_TIMEOUT", "18"))

_report_rpc_lock = asyncio.Lock()
_report_rpc_last = 0.0


def _account_op_timeout(ctx: dict) -> float:
    ids = ctx.get("msg_ids") or []
    n = min(len(ids), max(1, REPORT_MSG_IDS_PER_ACCOUNT)) if ids else 1
    return float(max(35, min(ACCOUNT_OP_TIMEOUT, 25 + n * 20)))


def _pick_msg_ids_for_session(ctx: dict, session_id: str) -> list[int]:
    ids = _safe_int32_ids(list(ctx.get("msg_ids") or []))
    if not ids:
        return []
    cap = max(1, int(REPORT_MSG_IDS_PER_ACCOUNT))
    if len(ids) <= cap:
        return ids
    start = sum(ord(c) for c in str(session_id)) % len(ids)
    return [ids[(start + i) % len(ids)] for i in range(cap)]


def _infer_entity_kind(target: str, current: str | None = None) -> str:
    cur = (current or "").strip().lower()
    if cur and cur not in ("unknown", "?", ""):
        return cur
    kind, token = _parse_target(target or "")
    if kind == "invite":
        return "invite"
    if kind == "id" and token:
        tid = str(token)
        if tid.startswith("-100") or tid.lstrip("-").isdigit():
            return "channel"
    if kind == "username":
        return "channel"
    return cur or "unknown"


async def _safe_disconnect(cli) -> None:
    if not cli:
        return
    try:
        if getattr(cli, "is_connected", lambda: False)():
            await cli.disconnect()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    except Exception:
        pass


def _is_text_comment_mode(ctx: dict) -> bool:
    return (ctx.get("comment_source") or "").strip().lower() == "text"


def _needs_slow_pace(ctx: dict) -> bool:
    return False


def _report_inter_delay(ctx: dict) -> float:
    return random.uniform(REPORT_DELAY_MIN, REPORT_DELAY_MAX)


def _flood_seconds_from_info(info) -> int:
    s = str(info or "")
    m = re.search(r"FLOODWAIT_(\d+)", s, re.I)
    if m:
        return max(1, min(int(m.group(1)), 180))
    return 0


def _is_conn_fail_info(info) -> bool:
    s = str(info or "").upper()
    return any(
        x in s
        for x in (
            "CONNECTION",
            "RESET",
            "TIMEOUT",
            "DISCONNECT",
            "SERVERCLOSED",
            "OSERROR",
            "CONNECT",
            "CONN_",
        )
    )


async def _report_step_pause() -> None:
    await asyncio.sleep(random.uniform(REPORT_STEP_DELAY_MIN, REPORT_STEP_DELAY_MAX))


async def _global_report_gate() -> None:
    global _report_rpc_last
    async with _report_rpc_lock:
        now = _time.monotonic()
        wait = REPORT_GLOBAL_MIN_GAP - (now - _report_rpc_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _report_rpc_last = _time.monotonic()

INT32_MAX = 2147483647
INT32_MIN = -2147483648

def _safe_int32_ids(ids: list) -> list[int]:
    out: list[int] = []
    for i in ids or []:
        try:
            v = int(i)
        except Exception:
            continue
        if INT32_MIN <= v <= INT32_MAX:
            out.append(v)
    return out

def _server_usage() -> dict:
    try:
        import psutil

        cpu_percent = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()

        usage_data = {
            "cpu_percent": round(float(cpu_percent), 1),
            "ram_percent": round(float(vm.percent), 1),
            "ram_used_gb": round(float(vm.used) / (1024**3), 2),
            "ram_total_gb": round(float(vm.total) / (1024**3), 2),
        }

        return usage_data

    except Exception as e:
        logger.warning(f"psutil failed ({e}). Trying fallback methods...")

        try:
            if os.name == "posix":
                vals = {}
                with open("/proc/meminfo", "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.split(":", 1)
                        if len(parts) < 2:
                            continue
                        k = parts[0]
                        num_str = parts[1].strip().split()[0]
                        vals[k] = int(num_str)

                total = vals.get("MemTotal", 0)
                avail = vals.get("MemAvailable", 0)
                used = max(0, total - avail)

                load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0
                cpu_count = os.cpu_count() or 1

                usage_data = {
                    "cpu_percent": round(min(100.0, (load1 / cpu_count) * 100), 1),
                    "ram_percent": round((used / total) * 100, 1) if total else 0,
                    "ram_used_gb": round(used / 1024 / 1024, 2),
                    "ram_total_gb": round(total / 1024 / 1024, 2),
                }

                return usage_data

            else:
                logger.error(
                    "psutil is not installed. Cannot fetch usage on Windows without psutil."
                )
                return {
                    "cpu_percent": 0,
                    "ram_percent": 0,
                    "ram_used_gb": 0,
                    "ram_total_gb": 0,
                }

        except Exception as fallback_e:
            logger.error(f"Failed to get server usage: {fallback_e}")
            return {
                "cpu_percent": 0,
                "ram_percent": 0,
                "ram_used_gb": 0,
                "ram_total_gb": 0,
            }

def _format_runtime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"

def _report_elapsed_seconds(stats: dict) -> int:
    started = int(stats.get("started_at") or 0)
    if started <= 0:
        return 0
    return max(0, _now_ts() - started)

def _format_report_status(uid: int, stats: dict) -> str:
    ok = int(stats.get("ok", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)
    return f"✅ موفق: {ok}\n❌ ناموفق: {failed}"

def _report_live_buttons(uid: int):
    return [
        [Button.inline(txt(uid, "status_button"), data=f"status:{uid}".encode())],
        [Button.inline(txt(uid, "stop_button"), data=f"stop:{uid}".encode())],
    ]

async def _save_report_stats(uid: int, stats: dict, ttl: int = 600):
    await redis.set(
        f"report_stats:{uid}", json.dumps(stats, ensure_ascii=False), ex=ttl
    )

async def _safe_edit_progress(msg, uid: int, stats: dict, *, _state: dict | None = None):

    state = _state if _state is not None else {}
    now = _now_ts()
    if now < int(state.get("flood_until") or 0):
        return
    text = (
        "🚀 عملیات ریپورت در حال انجام است...\n\n"
        "برای مشاهده آمار دقیق، روی دکمه وضعیت کلیک کنید."
    )
    buttons = _report_live_buttons(uid)
    if text == state.get("last_text"):
        return
    try:
        await msg.edit(text, buttons=buttons)
        state["last_text"] = text
    except errors.FloodWaitError as e:
        wait = max(5, min(int(getattr(e, "seconds", 30) or 30), 120))
        state["flood_until"] = now + wait
        logger.warning("progress edit FloodWait %ss — pausing status edits", wait)
    except Exception as e:
        err = str(e).lower()
        if "not modified" in err or "message is not modified" in err:
            state["last_text"] = text
            return
        if "flood" in err:
            state["flood_until"] = now + 30
        logger.debug("progress edit skipped: %s", e)

def _format_ping_status(
    uid: int, total: int, online: int, failed: int, pending: int
) -> str:
    progress_percent = round((100 * (online + failed)) / total) if total > 0 else 0
    bar_length = 20
    filled = round(bar_length * progress_percent / 100)
    bar = "█" * filled + "░" * (bar_length - filled)

    return txt(
        uid,
        "ping_progress",
        total_accounts=total,
        online_count=online,
        failed_count=failed,
        pending_count=pending,
        progress_percent=progress_percent,
        progress_bar=bar,
    )

async def lock_account(sess: str, uid: int):
    await redis.set(f"lock:{sess}", str(uid), ex=LOCK_TTL)

async def unlock_account(sess: str):
    await redis.delete(f"lock:{sess}")

async def _lock_owner(sess: str) -> int | None:
    raw = await redis.get(f"lock:{sess}")
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None

async def is_account_locked(sess: str) -> bool:
    owner = await _lock_owner(sess)
    if owner is None:
        return False
    try:
        if await is_report_really_running(owner):
            return True
    except Exception:
        return True
    try:
        await unlock_account(sess)
    except Exception:
        pass
    return False


async def _filter_unlocked_sessions(sessions: list[str], uid: int) -> list[str]:
    """Drop sessions locked by an actively running report; clear stale locks."""
    if not sessions:
        return []
    owners: dict[str, int] = {}
    for s in sessions:
        owner = await _lock_owner(s)
        if owner is not None:
            owners[s] = owner
    live: dict[int, bool] = {}
    for owner in set(owners.values()):
        try:
            live[owner] = await is_report_really_running(owner)
        except Exception:
            live[owner] = True
    unlocked: list[str] = []
    for s in sessions:
        owner = owners.get(s)
        if owner is None:
            unlocked.append(s)
            continue
        if live.get(owner):
            continue
        try:
            await unlock_account(s)
        except Exception:
            pass
        unlocked.append(s)
    if not unlocked:
        logger.warning(
            "all %s sessions busy in a running report uid=%s",
            len(sessions),
            uid,
        )
    return unlocked

PEER_CACHE: dict[tuple[str, str], object] = {}
PEER_CACHE_TTL = int(
    os.getenv("PEER_CACHE_TTL", "600")
)
PEER_CACHE_TS: dict[tuple[str, str], float] = {}

def _peer_cache_get(cache_key: tuple[str, str]):
    ts = PEER_CACHE_TS.get(cache_key)
    if ts is None:
        return None
    if (_time.monotonic() - ts) > PEER_CACHE_TTL:
        PEER_CACHE.pop(cache_key, None)
        PEER_CACHE_TS.pop(cache_key, None)
        return None
    val = PEER_CACHE.get(cache_key)

    if isinstance(val, str) or val is False or val is None or val is True:
        PEER_CACHE.pop(cache_key, None)
        PEER_CACHE_TS.pop(cache_key, None)
        return None
    return val

def _peer_cache_set(cache_key: tuple[str, str], value):
    if value is None or value is False or isinstance(value, str):
        return
    PEER_CACHE[cache_key] = value
    PEER_CACHE_TS[cache_key] = _time.monotonic()

def _peer_cache_pop(cache_key: tuple[str, str]):
    PEER_CACHE.pop(cache_key, None)
    PEER_CACHE_TS.pop(cache_key, None)

ADMIN_IDS = (
    set(ADMINS)
    if isinstance(ADMINS, (list, tuple, set))
    else ({ADMINS} if ADMINS else set())
)
MAIN_ADMIN_ID = next(iter(ADMIN_IDS), None)

def load_additional_admins():
    try:
        docs = db["admins"].find({})
        for doc in docs:
            uid = doc.get("user_id")
            if uid:
                ADMIN_IDS.add(uid)
    except Exception:
        pass

load_additional_admins()

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def get_main_admin_with_accounts():
    for admin_id in ADMIN_IDS:
        if db["accounts"].count_documents({"admin_id": admin_id}) > 0:
            return admin_id
    return MAIN_ADMIN_ID

MAIN_ADMIN_ID = get_main_admin_with_accounts()

PLUS_SUBS_COL = db["plus_subscriptions"]
USER_SETTINGS_COL = db["user_settings"]
DEFAULT_LANG = "fa"

def get_user_lang(uid: int) -> str:
    doc = USER_SETTINGS_COL.find_one({"user_id": uid})
    return doc.get("language", DEFAULT_LANG) if doc else DEFAULT_LANG

def set_user_lang(uid: int, lang: str):
    USER_SETTINGS_COL.update_one(
        {"user_id": uid}, {"$set": {"language": lang}}, upsert=True
    )

with open("translations.json", "r", encoding="utf-8") as f:
    TRANSLATIONS = json.load(f)

try:
    with open("report_options_translations.json", "r", encoding="utf-8") as f:
        REPORT_OPT_TRANSLATIONS = json.load(f)
except FileNotFoundError:
    REPORT_OPT_TRANSLATIONS = {}

def _report_opt_text(uid: int, o) -> str:
    lang = get_user_lang(uid)
    if lang == "en":
        return o.text
    key = o.option.decode("utf-8", errors="ignore")
    return REPORT_OPT_TRANSLATIONS.get(key, {}).get(lang, o.text)

def _opt_key_bytes(opt: bytes | None) -> str:
    if not opt:
        return ""
    return bytes(opt).decode("utf-8", errors="ignore")

def _encode_opt_callback(prefix: str, option: bytes) -> bytes:

    payload = base64.urlsafe_b64encode(bytes(option)).rstrip(b"=")
    return f"{prefix}b64:".encode("ascii") + payload

def _decode_opt_callback(data: bytes, prefix: str) -> bytes | None:

    if not data:
        return None
    pref = prefix.encode("ascii")
    if not data.startswith(pref):
        return None
    rest = data[len(pref) :]
    if rest.startswith(b"b64:"):
        raw = rest[4:]
        pad = b"=" * ((4 - len(raw) % 4) % 4)
        try:
            return base64.urlsafe_b64decode(raw + pad)
        except Exception:
            return None

    return bytes(rest) if rest else None

_ADULT_OPT_KEYS = {
    "5",
    "52",
    "53",
    "54",
    "55",
    "56",
    "57",
    "a4",
}
_VIOLENCE_OPT_KEYS = {"3", "31", "32", "33", "34", "35", "36", "37", "38"}
_DRUGS_OPT_KEYS = {"4", "41", "411", "412", "413", "414", "42", "421", "422", "423", "43", "44", "45", "46", "47", "a5"}
_PERSONAL_OPT_KEYS = {"6", "61", "62", "63", "64", "65"}

def _path_option_keys(ctx_snapshot: dict) -> set[str]:
    keys = set()
    for step in ctx_snapshot.get("path") or []:
        key = (step.get("key") or _opt_key_bytes(_norm_option(step.get("option")))).strip()
        if key:
            keys.add(key)
    return keys

def _path_looks_adult(ctx_snapshot: dict) -> bool:
    keys = _path_option_keys(ctx_snapshot)
    if keys & _ADULT_OPT_KEYS or any(k.startswith("5") for k in keys):
        return True
    for step in ctx_snapshot.get("path") or []:
        text = (step.get("text") or "").lower()
        if any(
            w in text
            for w in (
                "porn",
                "پورن",
                "بزرگسال",
                "adult",
                "جنسی",
                "اباح",
                "sexual",
            )
        ):
            return True
    return False

def _path_looks_violence(ctx_snapshot: dict) -> bool:
    keys = _path_option_keys(ctx_snapshot)
    if keys & _VIOLENCE_OPT_KEYS or any(k.startswith("3") for k in keys):
        return True
    for step in ctx_snapshot.get("path") or []:
        text = (step.get("text") or "").lower()
        if any(w in text for w in ("violence", "خشونت", "عنف", "terror", "ترور")):
            return True
    return False

def _path_looks_drugs(ctx_snapshot: dict) -> bool:
    keys = _path_option_keys(ctx_snapshot)
    if keys & _DRUGS_OPT_KEYS or any(k.startswith("4") for k in keys):
        return True
    for step in ctx_snapshot.get("path") or []:
        text = (step.get("text") or "").lower()
        if any(w in text for w in ("drug", "مخدر", "نیکوتین", "سلاح", "weapon")):
            return True
    return False

def _path_looks_personal(ctx_snapshot: dict) -> bool:
    keys = _path_option_keys(ctx_snapshot)
    if keys & _PERSONAL_OPT_KEYS or any(
        k == "6" or k.startswith("6") for k in keys
    ):
        return True
    for step in ctx_snapshot.get("path") or []:
        text = _fold_opt_text(
            f"{step.get('text') or ''} {step.get('raw_text') or ''}"
        )
        if any(
            w in text
            for w in (
                "personal detail",
                "personal data",
                "personal info",
                "private info",
                "private photo",
                "phone number",
                "اطلاعات شخصی",
                "تصاویر خصوصی",
                "شماره تلفن",
                "رمزهای سرقت",
                "بيانات شخصية",
                "صور خاصة",
                "رقم هاتف",
            )
        ):
            return True
    return False

def _should_auto_comment(ctx_snapshot: dict) -> bool:

    return (ctx_snapshot or {}).get("mode") in ("scam", "fake")

def _path_needs_channel_ban(ctx_snapshot: dict) -> bool:
    if (ctx_snapshot or {}).get("mode") in ("scam", "fake"):
        return True
    return (
        _path_looks_adult(ctx_snapshot)
        or _path_looks_violence(ctx_snapshot)
        or _path_looks_drugs(ctx_snapshot)
        or _path_looks_personal(ctx_snapshot)
    )

_OPTION_TEXT_ALIASES: dict[str, tuple[str, ...]] = {
    "3": ("violence", "violent", "خشونت", "عنف"),
    "31": (
        "insult",
        "misinfo",
        "misleading",
        "توهین",
        "نادرست",
        "إهانات",
        "مضللة",
    ),
    "32": (
        "shocking",
        "disturbing",
        "graphic",
        "تصویری",
        "آزاردهنده",
        "صادم",
        "مزعج",
    ),
    "33": (
        "extreme violence",
        "dismember",
        "gore",
        "خشونت شدید",
        "قطع",
        "تمزيق",
        "عنف شديد",
    ),
    "34": ("hate", "نفرت", "كراهية", "symbol"),
    "35": ("incitement", "call to violence", "فراخوان", "دعوة للعنف"),
    "36": ("organized crime", "سازمان‌یافته", "جريمة منظمة"),
    "37": ("terror", "ترور", "إرهاب"),
    "38": ("animal", "حیوان", "حيوانات"),
    "5": (
        "illegal adult",
        "adult content",
        "بزرگسال",
        "للبالغين",
        "adult",
    ),
    "52": ("sex service", "خدمات جنسی", "خدمات جنسية"),
    "53": (
        "non-consensual",
        "without consent",
        "بدون رضایت",
        "دون موافقة",
        "revenge porn",
    ),
    "54": ("other illegal sexual", "سایر محتوای جنسی", "جنسي غير قانوني آخر"),
    "55": ("animal", "حیوان", "حيوانات"),
    "56": ("child abuse", "کودک", "أطفال", "children"),
    "57": (
        "pornograph",
        "porn",
        "پورنو",
        "اباح",
        "إباحي",
        "مواد إباحية",
    ),
    "4": ("illegal goods", "کالاها", "خدمات غیرقانونی", "سلع", "غير قانونية"),
    "42": ("drug", "مخدر", "مواد مخدر"),
    "422": ("illegal drug", "مخدر غیرقانونی", "مخدرات غير قانونية"),
    "6": (
        "personal detail",
        "personal details",
        "personal data",
        "personal info",
        "private information",
        "اطلاعات شخصی",
        "بيانات شخصية",
    ),
    "61": (
        "private photo",
        "private image",
        "intimate photo",
        "تصاویر خصوصی",
        "صور خاصة",
    ),
    "62": (
        "phone number",
        "phone",
        "شماره تلفن",
        "رقم هاتف",
        "mobile number",
    ),
    "63": ("address", "آدرس", "عنوان", "home address"),
    "64": (
        "stolen credential",
        "stolen password",
        "leaked password",
        "اطلاعات یا رمز",
        "رمزهای سرقت",
        "بيانات أو بيانات اعتماد مسروقة",
        "credential",
    ),
    "65": (
        "other personal",
        "سایر اطلاعات شخصی",
        "معلومات شخصية أخرى",
        "other private",
    ),
}

def _fold_opt_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s

def _infer_step_key(step: dict) -> str:
    key = (step.get("key") or "").strip()
    if key:
        return key
    texts = {
        _fold_opt_text(step.get("text") or ""),
        _fold_opt_text(step.get("raw_text") or ""),
    }
    texts.discard("")
    if not texts:
        return ""
    for k, langs in (REPORT_OPT_TRANSLATIONS or {}).items():
        for v in (langs or {}).values():
            if isinstance(v, str) and _fold_opt_text(v) in texts:
                return str(k)
    for k, aliases in _OPTION_TEXT_ALIASES.items():
        for a in aliases:
            af = _fold_opt_text(a)
            if not af:
                continue
            if any(af in t or t in af for t in texts):
                return str(k)
    return _opt_key_bytes(_norm_option(step.get("option")))

def _option_all_labels(o) -> list[str]:

    labels = []
    raw = (getattr(o, "text", None) or "").strip()
    if raw:
        labels.append(raw)
    try:
        key = bytes(o.option).decode("utf-8", "ignore")
    except Exception:
        key = ""
    if key:
        for v in (REPORT_OPT_TRANSLATIONS.get(key) or {}).values():
            if isinstance(v, str) and v.strip():
                labels.append(v.strip())
    return labels

def _score_option_for_step(o, step: dict) -> float:

    key = _infer_step_key(step)
    try:
        ok = bytes(o.option).decode("utf-8", "ignore")
    except Exception:
        ok = ""
    if key and ok == key:
        return 100.0

    labels = [_fold_opt_text(x) for x in _option_all_labels(o)]
    score = 0.0

    aliases = _OPTION_TEXT_ALIASES.get(key) or ()
    for lab in labels:
        for a in aliases:
            a = _fold_opt_text(a)
            if not a:
                continue
            if a in lab or lab in a:
                score = max(score, 80.0 + min(len(a), 10))
                break

    for field in ("raw_text", "text"):
        wt = _fold_opt_text(step.get(field) or "")
        if not wt or len(wt) < 2:
            continue
        for lab in labels:
            if wt == lab:
                score = max(score, 95.0)
            elif wt in lab or lab in wt:
                score = max(score, 70.0)
            else:
                r = difflib.SequenceMatcher(None, wt, lab).ratio()
                if r >= 0.72:
                    score = max(score, 60.0 + r * 20.0)

    wt_all = {
        _fold_opt_text(step.get("text") or ""),
        _fold_opt_text(step.get("raw_text") or ""),
    }
    wt_all.discard("")
    if ok and wt_all:
        for v in (REPORT_OPT_TRANSLATIONS.get(ok) or {}).values():
            if isinstance(v, str) and _fold_opt_text(v) in wt_all:
                score = max(score, 90.0)

    if key and ok and (ok.startswith(key) or key.startswith(ok)) and len(key) >= 2:
        score = max(score, 50.0)

    return score

def _match_report_choice(res_options, step: dict):

    if not res_options:
        return None
    wanted = _norm_option(step.get("option"))
    if wanted:
        choice = next((o for o in res_options if o.option == wanted), None)
        if choice:
            return choice

    key = _infer_step_key(step)
    if key:
        choice = next(
            (
                o
                for o in res_options
                if o.option.decode("utf-8", "ignore") == key
            ),
            None,
        )
        if choice:
            return choice

    scored = [(_score_option_for_step(o, step), o) for o in res_options]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    if best_score >= 45.0:
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= second + 5.0 or second < 45.0:
            return best

    aliases = _OPTION_TEXT_ALIASES.get(key) or ()
    if aliases:
        hits = []
        for o in res_options:
            blob = " | ".join(_fold_opt_text(x) for x in _option_all_labels(o))
            if any(_fold_opt_text(a) in blob for a in aliases):
                hits.append(o)
        if len(hits) == 1:
            return hits[0]
        if hits:
            return hits[0]

    if best_score >= 30.0:
        return best

    logger.warning(
        "report option mismatch key=%r text=%r raw=%r wanted=%r available=%s",
        key,
        step.get("text"),
        step.get("raw_text"),
        wanted,
        [(o.option, o.text) for o in res_options],
    )
    return None

def _path_step_from_option(uid: int, option: bytes, options_list=None) -> dict:
    key = _opt_key_bytes(option)
    text = ""
    raw_text = ""
    if options_list:
        hit = next((o for o in options_list if o.option == option), None)
        if hit:
            raw_text = (hit.text or "").strip()
            text = _report_opt_text(uid, hit)
    if not text:
        lang = get_user_lang(uid)
        text = (REPORT_OPT_TRANSLATIONS.get(key) or {}).get(lang) or key
    if not raw_text:

        raw_text = (REPORT_OPT_TRANSLATIONS.get(key) or {}).get("en") or ""
    return {"text": text, "raw_text": raw_text, "option": option, "key": key}


REPORT_REASON_CODES = (
    "spam",
    "violence",
    "porn",
    "child",
    "fake",
    "scam",
    "drugs",
    "personal",
    "copyright",
    "other",
)

REPORT_API_LIMIT_SCAM = "API_LIMIT:no_InputReportReasonScam->InputReportReasonOther"


class _ReasonSpec:
    __slots__ = ("code", "label", "peer_reason", "levels", "forbidden_keys", "forbidden_aliases")

    def __init__(self, code, label, peer_reason, levels, forbidden_keys=(), forbidden_aliases=()):
        self.code = code
        self.label = label
        self.peer_reason = peer_reason
        self.levels = levels
        self.forbidden_keys = frozenset(forbidden_keys)
        self.forbidden_aliases = tuple(forbidden_aliases)


_SCAM_ROOT = [("7", ("scam or fraud", "scam", "fraud", "کلاهبرداری یا فریب", "کلاهبرداری", "احتيال أو خداع", "احتيال"))]
_IMPERSONATION_ALIASES = ("impersonation", "impersonat", "جعل هویت", "انتحال الهوية", "انتحال")
_SCAM_ONLY_ALIASES = (
    "fraudulent",
    "deceptive",
    "unrealistic financial",
    "financial claim",
    "phishing",
    "متقلب",
    "فریبنده",
    "فیشینگ",
    "احتيالية",
    "مضللة",
    "تصيد",
)

REPORT_REASON_SPECS = {
    "spam": _ReasonSpec(
        "spam",
        "Spam",
        types.InputReportReasonSpam,
        [
            [("9", ("spam", "اسپم", "رسائل مزعجة"))],
            [
                ("92", ("promoting other content", "ترویج محتوای دیگر", "الترويج لمحتوى آخر")),
                ("91", ("promoting illegal content", "ترویج محتوای غیرقانونی", "الترويج لمحتوى غير قانوني")),
                ("93", ("insults or false information", "توهین یا اطلاعات نادرست", "إهانات أو معلومات مضللة")),
            ],
        ],
    ),
    "violence": _ReasonSpec(
        "violence",
        "Violence",
        types.InputReportReasonViolence,
        [
            [("3", ("violence", "خشونت", "عنف"))],
            [
                ("32", ("graphic or disturbing", "محتوای تصویری یا آزاردهنده", "محتوى صادم أو مزعج")),
                ("35", ("calling for violence", "فراخوان خشونت", "دعوة للعنف")),
                ("33", ("extreme violence", "خشونت شدید", "عنف شديد")),
                ("37", ("terrorism", "تروریسم", "إرهاب")),
                ("34", ("hate speech", "نفرت‌پراکنی", "خطاب أو رموز الكراهية")),
                ("36", ("organized crime", "جرم سازمان‌یافته", "جريمة منظمة")),
                ("38", ("animal abuse", "آزار حیوانات", "إساءة معاملة الحيوانات")),
                ("31", ("insults or false information", "توهین یا اطلاعات نادرست", "إهانات أو معلومات مضللة")),
            ],
        ],
    ),
    "porn": _ReasonSpec(
        "porn",
        "Pornography",
        types.InputReportReasonPornography,
        [
            [("5", ("illegal adult content", "illegal adult", "adult content", "محتوای بزرگسال", "للبالغين"))],
            [("57", ("pornography", "پورنوگرافی", "مواد إباحية", "إباحية"))],
        ],
        forbidden_keys=("56",),
        forbidden_aliases=("child", "کودک", "أطفال"),
    ),
    "child": _ReasonSpec(
        "child",
        "Child Abuse",
        types.InputReportReasonChildAbuse,
        [
            [("2", ("child abuse", "سوءاستفاده از کودکان", "إساءة معاملة الأطفال"))],
            [
                ("21", ("child sexual abuse", "سوءاستفاده جنسی از کودک", "استغلال جنسي للأطفال")),
                ("22", ("child physical abuse", "سوءاستفاده جسمی از کودک", "إساءة جسدية للأطفال")),
            ],
        ],
    ),
    "fake": _ReasonSpec(
        "fake",
        "Fake",
        types.InputReportReasonFake,
        [
            _SCAM_ROOT,
            [("71", _IMPERSONATION_ALIASES)],
        ],
        forbidden_keys=("72", "73", "74"),
        forbidden_aliases=_SCAM_ONLY_ALIASES,
    ),
    "scam": _ReasonSpec(
        "scam",
        "Scam",
        None,
        [
            _SCAM_ROOT,
            [
                ("74", ("fraudulent seller", "fraudulent", "فروشنده، محصول یا خدمات متقلب", "متقلب", "بائع أو منتج أو خدمة احتيالية")),
                ("72", ("deceptive or unrealistic financial", "unrealistic financial", "deceptive", "وعده‌های مالی فریبنده", "ادعاءات مالية مضللة")),
                ("73", ("malware, phishing", "phishing", "بدافزار، فیشینگ", "فیشینگ", "برمجيات خبيثة، تصيد", "تصيد")),
            ],
        ],
        forbidden_keys=("71",),
        forbidden_aliases=_IMPERSONATION_ALIASES,
    ),
    "drugs": _ReasonSpec(
        "drugs",
        "Illegal Drugs",
        types.InputReportReasonIllegalDrugs,
        [
            [("4", ("illegal goods and services", "illegal goods", "کالاها و خدمات غیرقانونی", "سلع وخدمات غير قانونية"))],
            [("42", ("drugs", "مواد مخدر", "مخدرات"))],
            [
                ("422", ("illegal drugs", "مواد مخدر غیرقانونی", "مخدرات غير قانونية")),
                ("423", ("other substances", "سایر مواد", "مخدرات أخرى")),
                ("421", ("nicotine", "نیکوتین", "النيكوتين")),
            ],
        ],
    ),
    "personal": _ReasonSpec(
        "personal",
        "Personal Details",
        types.InputReportReasonPersonalDetails,
        [
            [("6", ("personal data", "personal details", "اطلاعات شخصی", "بيانات شخصية"))],
            [
                ("65", ("other personal information", "سایر اطلاعات شخصی", "معلومات شخصية أخرى")),
                ("62", ("phone number", "شماره تلفن", "رقم هاتف")),
                ("61", ("private images", "تصاویر خصوصی", "صور خاصة")),
                ("63", ("address", "آدرس")),
                ("64", ("stolen data", "credentials", "رمزهای سرقت‌شده", "بيانات اعتماد مسروقة")),
            ],
        ],
    ),
    "copyright": _ReasonSpec(
        "copyright",
        "Copyright",
        types.InputReportReasonCopyright,
        [[("8", ("copyright", "کپی‌رایت", "کپی رایت", "حق نشر", "حقوق النشر"))]],
    ),
    "other": _ReasonSpec(
        "other",
        "Other",
        types.InputReportReasonOther,
        [
            [("a", ("other", "سایر موارد", "أخرى"))],
            [("a2", ("something else", "چیز دیگری", "شيء آخر"))],
        ],
    ),
    "geo": _ReasonSpec(
        "geo",
        "Irrelevant location",
        types.InputReportReasonGeoIrrelevant,
        [],
    ),
}


def _reason_spec(code: str | None) -> _ReasonSpec | None:
    return REPORT_REASON_SPECS.get((code or "").strip().lower())


def _option_key(o) -> str:
    try:
        return bytes(o.option).decode("utf-8", "ignore")
    except Exception:
        return ""


def _option_text_folded(o) -> str:
    return _fold_opt_text(getattr(o, "text", None) or "")


def _text_has_alias(text: str, aliases) -> bool:
    text = _fold_opt_text(text)
    if not text:
        return False
    return any(_fold_opt_text(a) and _fold_opt_text(a) in text for a in aliases)


def _option_forbidden_for(code: str | None, o) -> bool:
    spec = _reason_spec(code)
    if spec is None:
        return False
    if _option_key(o) in spec.forbidden_keys:
        return True
    return _text_has_alias(getattr(o, "text", None) or "", spec.forbidden_aliases)


def _pick_reason_option(options, code: str, level: int):
    spec = _reason_spec(code)
    if spec is None or not options or level >= len(spec.levels):
        return None
    allowed = [o for o in options if not _option_forbidden_for(code, o)]
    cands = spec.levels[level]
    for _key, aliases in cands:
        folded = {_fold_opt_text(a) for a in aliases}
        hit = next((o for o in allowed if _option_text_folded(o) in folded), None)
        if hit is None:
            hit = next((o for o in allowed if _text_has_alias(o.text or "", aliases)), None)
        if hit is not None:
            return hit
    for key, _aliases in cands:
        for o in allowed:
            if _option_key(o) == key:
                return o
    return None


def _reason_sub_options(options, code: str) -> list:
    return [o for o in (options or []) if not _option_forbidden_for(code, o)]


_ROOT_CATEGORY_RULES = (
    ("child", ("2",), ("child abuse", "سوءاستفاده از کودکان", "إساءة معاملة الأطفال")),
    ("violence", ("3",), ("violence", "خشونت", "عنف")),
    ("goods", ("4",), ("illegal goods", "کالاها و خدمات غیرقانونی", "سلع وخدمات غير قانونية")),
    ("porn", ("5",), ("illegal adult", "adult content", "محتوای بزرگسال", "للبالغين")),
    ("personal", ("6",), ("personal data", "personal details", "اطلاعات شخصی", "بيانات شخصية")),
    ("scam", ("7",), ("scam", "fraud", "کلاهبرداری", "احتيال")),
    ("copyright", ("8",), ("copyright", "کپی‌رایت", "کپی رایت", "حق نشر", "حقوق النشر")),
    ("spam", ("9",), ("spam", "اسپم", "رسائل مزعجة")),
    (
        "other",
        ("a", "1", "b"),
        (
            "other",
            "something else",
            "don't like",
            "not illegal",
            "سایر موارد",
            "خوشم نمی",
            "غیرقانونی نیست",
            "أخرى",
            "لا يعجبني",
            "ليس غير قانوني",
        ),
    ),
)
_CHILD_ALIASES = ("child", "کودک", "أطفال")
_DRUG_ALIASES = ("drug", "nicotine", "substance", "مواد مخدر", "مخدر", "نیکوتین", "سایر مواد", "مخدرات", "النيكوتين")
_ADULT_ALIASES = ("illegal adult", "adult content", "محتوای بزرگسال", "للبالغين")


def _step_key(step: dict) -> str:
    opt = _norm_option(step.get("option"))
    if opt:
        return _opt_key_bytes(opt)
    return (step.get("key") or "").strip()


def _step_texts(step: dict) -> str:
    return " | ".join(
        t for t in (step.get("raw_text") or "", step.get("text") or "") if t
    )


def _step_matches(step: dict, aliases, keys=()) -> bool:
    if keys and _step_key(step) in keys:
        return True
    return _text_has_alias(_step_texts(step), aliases)


def _reason_code_from_path(path: list | None) -> str:
    steps = [s for s in (path or []) if isinstance(s, dict)]
    if not steps:
        return "other"
    for step in reversed(steps):
        opt = _norm_option(step.get("option")) or b""
        if bytes(opt).startswith(b"pr:"):
            code = bytes(opt)[3:].decode("utf-8", "ignore").strip().lower()
            return code if code in REPORT_REASON_SPECS else "other"
    root, subs = steps[0], steps[1:]
    category = None
    for cat, _keys, aliases in _ROOT_CATEGORY_RULES:
        if _text_has_alias(_step_texts(root), aliases):
            category = cat
            break
    if category is None:
        rk = _step_key(root)
        category = next((cat for cat, keys, _a in _ROOT_CATEGORY_RULES if rk in keys), None)
    if category == "scam":
        if any(_step_matches(s, _IMPERSONATION_ALIASES, ("71",)) for s in subs):
            return "fake"
        return "scam"
    if category == "porn":
        if any(_step_matches(s, _CHILD_ALIASES, ("56",)) for s in subs):
            return "child"
        return "porn"
    if category == "goods":
        if any(
            _step_matches(s, _DRUG_ALIASES) or _step_key(s).startswith("42")
            for s in subs
        ):
            return "drugs"
        return "other"
    if category == "other":
        if any(_step_matches(s, _ADULT_ALIASES, ("a4",)) for s in subs):
            return "porn"
        return "other"
    if category:
        return category
    for step in reversed(steps):
        if _step_matches(step, _IMPERSONATION_ALIASES):
            return "fake"
        if _step_matches(step, _SCAM_ONLY_ALIASES + ("scam", "fraud")):
            return "scam"
        if _step_matches(step, _CHILD_ALIASES):
            return "child"
        if _step_matches(step, ("porn", "پورن", "إباحي")):
            return "porn"
        if _step_matches(step, ("illegal drug", "مواد مخدر", "مخدرات")):
            return "drugs"
        if _step_matches(step, ("personal", "اطلاعات شخصی", "بيانات شخصية")):
            return "personal"
        if _step_matches(step, ("copyright", "کپی‌رایت", "حق نشر", "حقوق النشر")):
            return "copyright"
        if _step_matches(step, ("violence", "terror", "خشونت", "عنف")):
            return "violence"
        if _step_matches(step, ("spam", "اسپم")):
            return "spam"
    return "other"


def _peer_reason_for_code(code: str | None):
    spec = _reason_spec(code)
    if spec is None:
        return types.InputReportReasonOther(), "UNKNOWN_REASON->Other"
    if spec.peer_reason is None:
        return types.InputReportReasonOther(), REPORT_API_LIMIT_SCAM
    return spec.peer_reason(), ""


_MODE_REASON = {"scam": "scam", "fake": "fake"}


def _mode_reason_code(mode: str | None) -> str | None:
    return _MODE_REASON.get((mode or "").strip().lower())


def _reason_label(code: str | None) -> str:
    spec = _reason_spec(code)
    return spec.label if spec else str(code or "?")


def _path_keys_for_log(path: list | None) -> list:
    return [_step_key(s) for s in (path or []) if isinstance(s, dict)]

TEXT_OVERRIDES = {}
_ov_doc = db.settings.find_one({"key": "text_overrides"})
if _ov_doc and isinstance(_ov_doc.get("value"), dict):
    TEXT_OVERRIDES = _ov_doc["value"]

def extract_text_vars(text: str) -> list:
    try:
        return [f for _, f, _, _ in string.Formatter().parse(text) if f]
    except ValueError:
        return []

def get_bot_text(key: str, lang: str) -> str:
    ov = TEXT_OVERRIDES.get(key, {}).get(lang)
    if isinstance(ov, str):
        return ov
    val = TRANSLATIONS.get(key, {}).get(lang, "")
    return val if isinstance(val, str) else ""

def search_bot_texts(query: str, limit: int = 10) -> list:
    q = (query or "").strip().lower()
    if not q:
        return []
    scored = []
    for key, langs in TRANSLATIONS.items():
        if not isinstance(langs, dict):
            continue
        for lang, original in langs.items():
            if not isinstance(original, str):
                continue
            current = get_bot_text(key, lang)
            if not current:
                continue
            t = current.lower()
            if q in t:
                score = 2.0 + difflib.SequenceMatcher(None, q, t).ratio()
            else:
                score = difflib.SequenceMatcher(None, q, t).ratio()
                if score < 0.30:
                    continue
            scored.append((score, key, lang, current))
    scored.sort(key=lambda x: x[0], reverse=True)
    logger.info(f"[edit_texts] search {query!r} -> {len(scored)} results")
    for s, k, l, t in scored[:limit]:
        logger.info(
            f"[edit_texts]   hit: score={s:.2f} key={k} lang={l} text={t[:50]!r}"
        )
    return scored[:limit]

def _extract_custom_emoji_text(message) -> str:
    text = message.raw_text or ""
    entities = message.entities or []
    custom = [e for e in entities if isinstance(e, types.MessageEntityCustomEmoji)]
    if not custom:
        return text
    try:
        from telethon.helpers import add_surrogate, del_surrogate

        raw = add_surrogate(text)
        fixed = True
    except Exception as imp_err:
        logger.info(f"[edit_texts] surrogate helpers unavailable: {imp_err}")
        raw = text
        fixed = False
    edits = []
    for e in custom:
        emoji_char = raw[e.offset : e.offset + e.length]
        logger.info(
            f"[edit_texts] premium emoji captured: char={emoji_char!r} document_id={e.document_id} offset={e.offset}"
        )
        edits.append((e.offset, e.length, f"{emoji_char}[{e.document_id}]"))
    for off, ln, repl in sorted(edits, key=lambda x: x[0], reverse=True):
        raw = raw[:off] + repl + raw[off + ln :]
    result = del_surrogate(raw) if fixed else raw
    logger.info(f"[edit_texts] stored text after premium emoji convert: {result!r}")
    return result

MENU_ACTIONS_MAP = {}
menu_keys = [
    ("menu_accounts", "accounts"),
    ("menu_report_msg", "report_msg"),
    ("menu_report_story", "report_story"),
    ("menu_report_bot", "report_bot"),
    ("menu_partners", "partners"),
    ("menu_transfer", "transfer"),
    ("cancel", "back"),
    ("back_btn", "back"),
    ("language_button", "language"),
    ("menu_my_credit", "my_credit"),
    ("menu_referral", "referral"),
    ("buy_normal_subscription", "buy_normal"),
    ("buy_special_subscription", "buy_special"),
    ("menu_report_scam", "report_scam"),
    ("menu_report_fake", "report_fake"),
    ("menu_report_profile", "report_profile"),
    ("menu_join_request", "join_request"),
    ("menu_send_pv", "send_pv"),
    ("menu_ai_analyze", "ai_analyze"),
    ("menu_report_manage", "report_manage"),
]
_LEGACY_MENU_TEXTS = (
    "ریپورت اسکم | جعلی",
    "الإبلاغ عن احتيال | مزيف",
    "Report Scam | Fake",
)


def _drop_obsolete_menu_overrides() -> None:
    scam = TEXT_OVERRIDES.get("menu_report_scam")
    if not isinstance(scam, dict):
        return
    stale = [lang for lang, v in scam.items() if isinstance(v, str) and "|" in v]
    if not stale:
        return
    for lang in stale:
        scam.pop(lang, None)
    if not scam:
        TEXT_OVERRIDES.pop("menu_report_scam", None)
    try:
        db.settings.update_one(
            {"key": "text_overrides"}, {"$set": {"value": TEXT_OVERRIDES}}, upsert=True
        )
    except Exception as e:
        logger.warning("menu override cleanup failed: %s", e)


def rebuild_menu_actions_map() -> None:
    fresh: dict[str, str] = {}
    for legacy in _LEGACY_MENU_TEXTS:
        fresh[legacy] = "back"
        fresh[normalize_menu_text(legacy)] = "back"
    for key, action in menu_keys:
        for lang in ("fa", "ar", "en"):
            for text in (
                (TRANSLATIONS.get(key) or {}).get(lang),
                (TEXT_OVERRIDES.get(key) or {}).get(lang),
            ):
                if isinstance(text, str) and text.strip():
                    fresh[text.strip()] = action
                    norm = normalize_menu_text(text)
                    if norm:
                        fresh[norm] = action
    MENU_ACTIONS_MAP.clear()
    MENU_ACTIONS_MAP.update(fresh)


_drop_obsolete_menu_overrides()
rebuild_menu_actions_map()


def txt(uid: int, text_key: str, **kwargs) -> str:
    lang = get_user_lang(uid)
    base = TRANSLATIONS.get(text_key) or {}
    default = base.get(lang, base.get(DEFAULT_LANG, text_key)) if isinstance(base, dict) else text_key
    text = (TEXT_OVERRIDES.get(text_key) or {}).get(lang) or default
    if kwargs and isinstance(text, str):
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            if text is not default and isinstance(default, str):
                try:
                    return default.format(**kwargs)
                except (KeyError, IndexError, ValueError):
                    return default
    return text

try:
    PLUS_SUBS_COL.create_index([("user_id", 1), ("admin_id", 1)], unique=True)
except Exception:
    pass

def _now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())

def _user_id_filter(uid) -> dict:
    try:
        i = int(uid)
    except Exception:
        return {"user_id": uid}
    return {"$or": [{"user_id": i}, {"user_id": str(i)}]}

def _as_expiry_ts(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dt.datetime):
        try:
            return int(v.timestamp())
        except Exception:
            return 0
    try:
        return int(v)
    except Exception:
        return 0

def get_plus_subscription(user_id: int) -> dict | None:
    doc = PLUS_SUBS_COL.find_one(_user_id_filter(user_id))
    if doc and _days_left(_as_expiry_ts(doc.get("expires_at", 0))) > 0:
        return doc
    return None

def add_plus_subscription(
    user_id: int, days: int, max_accounts: int, added_by: int, admin_id: int
):
    if admin_id is None:
        return None
    uid = int(user_id)
    PLUS_SUBS_COL.delete_many(_user_id_filter(uid))
    expires_ts = _now_ts() + max(0, int(days)) * 86400
    doc = {
        "user_id": uid,
        "admin_id": int(admin_id),
        "expires_at": expires_ts,
        "max_accounts": int(max_accounts),
        "created_at": _now_ts(),
        "added_by": int(added_by),
    }
    PLUS_SUBS_COL.insert_one(doc)
    return expires_ts

def extend_plus_subscription(user_id: int, more_days: int) -> int | None:
    sub = get_plus_subscription(user_id)
    if not sub:
        return None
    new_exp = max(_as_expiry_ts(sub["expires_at"]), _now_ts()) + more_days * 86400
    PLUS_SUBS_COL.update_one(
        _user_id_filter(user_id),
        {"$set": {"user_id": int(user_id), "expires_at": new_exp}},
    )
    return new_exp

def remove_plus_subscription(user_id: int):
    PLUS_SUBS_COL.delete_many(_user_id_filter(user_id))

PARTNERS_COL = db["partners"]
PARTNER_REQUESTS_COL = db["partner_requests"]
try:
    PARTNERS_COL.create_index(
        [("main_user_id", 1), ("partner_user_id", 1)], unique=True
    )
except Exception:
    pass

async def add_partner(main_user_id: int, partner_user_id: int):
    if main_user_id == partner_user_id:
        return False, "self_partner_error"
    if PARTNERS_COL.find_one(
        {"main_user_id": main_user_id, "partner_user_id": partner_user_id}
    ):
        return False, "partner_already_exists"
    PARTNERS_COL.insert_one(
        {
            "main_user_id": main_user_id,
            "partner_user_id": partner_user_id,
            "added_at": datetime.datetime.now(datetime.timezone.utc),
        }
    )
    return True, "partner_added"

async def is_partner(uid: int) -> bool:
    main_users = await get_main_users(uid)
    return len(main_users) > 0

async def remove_partner(main_user_id: int, partner_user_id: int):
    res = PARTNERS_COL.delete_one(
        {"main_user_id": main_user_id, "partner_user_id": partner_user_id}
    )
    return res.deleted_count > 0

async def get_partners(main_user_id: int) -> list[int]:
    docs = PARTNERS_COL.find({"main_user_id": main_user_id}, {"partner_user_id": 1})
    return [doc["partner_user_id"] for doc in docs]

async def get_main_users(partner_user_id: int) -> list[int]:
    docs = PARTNERS_COL.find({"partner_user_id": partner_user_id}, {"main_user_id": 1})
    return [doc["main_user_id"] for doc in docs]

def set_bank_info(card_number: str, card_holder: str):
    SETTINGS_COL.update_one(
        {"key": "bank_info"},
        {"$set": {"value": {"card_number": card_number, "card_holder": card_holder}}},
        upsert=True,
    )

def get_bank_info() -> dict:
    doc = SETTINGS_COL.find_one({"key": "bank_info"})
    if doc and "value" in doc:
        return doc["value"]
    return {"card_number": "", "card_holder": ""}

_CRYPTO_SETTINGS_KEY = "crypto_payment"
_CRYPTO_DEFAULTS = {
    "enabled": False,
    "wallet": "",
    "memo": "",
    "network": "TON",
    "symbol": "GRAM_IRT",
    "asset_name": "GRAM",
    "decimals": 4,
    "auto_verify": True,
    "jetton_master": "EQC47093oX5Xhb0xuk2lCr2RhS8rj-vul61u4W2UH5ORmG_O",
    "amount_tolerance": 0.02,
    "jetton_decimals": 9,
}

def get_crypto_settings() -> dict:
    try:
        doc = db["settings"].find_one({"key": _CRYPTO_SETTINGS_KEY})
    except Exception:
        doc = None
    val = dict(_CRYPTO_DEFAULTS)
    if doc and isinstance(doc.get("value"), dict):
        raw = doc["value"]
        val["wallet"] = str(raw.get("wallet") or "").strip()
        val["memo"] = str(raw.get("memo") or "").strip()
        val["network"] = str(raw.get("network") or _CRYPTO_DEFAULTS["network"]).strip() or "TON"
        val["symbol"] = str(raw.get("symbol") or _CRYPTO_DEFAULTS["symbol"]).strip() or "GRAM_IRT"
        val["asset_name"] = (
            str(raw.get("asset_name") or _CRYPTO_DEFAULTS["asset_name"]).strip() or "GRAM"
        )
        try:
            val["decimals"] = max(2, min(8, int(raw.get("decimals") or 4)))
        except Exception:
            val["decimals"] = 4
        val["auto_verify"] = bool(raw.get("auto_verify", True))
        val["jetton_master"] = str(
            raw.get("jetton_master") or _CRYPTO_DEFAULTS["jetton_master"]
        ).strip()
        try:
            val["amount_tolerance"] = max(
                0.0, min(0.2, float(raw.get("amount_tolerance") or 0.02))
            )
        except Exception:
            val["amount_tolerance"] = 0.02
        try:
            val["jetton_decimals"] = max(0, min(18, int(raw.get("jetton_decimals") or 9)))
        except Exception:
            val["jetton_decimals"] = 9

        val["enabled"] = bool(raw.get("enabled", False)) and bool(val["wallet"])
    return val

def save_crypto_settings(settings: dict):
    cur = get_crypto_settings()
    cur.update(settings or {})
    cur["wallet"] = str(cur.get("wallet") or "").strip()
    cur["memo"] = str(cur.get("memo") or "").strip()
    cur["network"] = str(cur.get("network") or "TON").strip() or "TON"
    cur["symbol"] = str(cur.get("symbol") or "GRAM_IRT").strip() or "GRAM_IRT"
    cur["asset_name"] = str(cur.get("asset_name") or "GRAM").strip() or "GRAM"
    try:
        cur["decimals"] = max(2, min(8, int(cur.get("decimals") or 4)))
    except Exception:
        cur["decimals"] = 4
    cur["auto_verify"] = bool(cur.get("auto_verify", True))
    cur["jetton_master"] = str(
        cur.get("jetton_master") or _CRYPTO_DEFAULTS["jetton_master"]
    ).strip()
    try:
        cur["amount_tolerance"] = max(
            0.0, min(0.2, float(cur.get("amount_tolerance") or 0.02))
        )
    except Exception:
        cur["amount_tolerance"] = 0.02
    try:
        cur["jetton_decimals"] = max(0, min(18, int(cur.get("jetton_decimals") or 9)))
    except Exception:
        cur["jetton_decimals"] = 9
    cur["enabled"] = bool(cur.get("enabled")) and bool(cur.get("wallet"))
    db["settings"].update_one(
        {"key": _CRYPTO_SETTINGS_KEY},
        {"$set": {"value": cur}},
        upsert=True,
    )
    return cur

def crypto_pay_available() -> bool:

    s = get_crypto_settings()
    return bool(s.get("enabled")) and bool((s.get("wallet") or "").strip())

_REFERRAL_SETTINGS_KEY = "referral_settings"
_REFERRAL_DEFAULTS = {
    "enabled": True,
    "required_invites": 10,
    "reward_days": 2,
    "once_only": True,
}

BOT_USERS_COL = db["bot_users"]
REFERRER_STATS_COL = db["referrer_stats"]
try:
    REFERRER_STATS_COL.create_index("user_id", unique=True)
except Exception as e:
    logger.warning("referrer_stats index: %s", e)
try:
    BOT_USERS_COL.create_index("user_id", unique=True)
except Exception:
    pass
try:
    BOT_USERS_COL.create_index("referred_by")
except Exception:
    pass
try:
    REFERRER_STATS_COL.create_index("user_id", unique=True)
except Exception:
    pass

_BOT_USERNAME_CACHE = {"u": ""}

async def get_bot_username() -> str:
    if _BOT_USERNAME_CACHE.get("u"):
        return _BOT_USERNAME_CACHE["u"]
    try:
        me = await bot.get_me()
        _BOT_USERNAME_CACHE["u"] = (me.username or "").strip()
    except Exception:
        _BOT_USERNAME_CACHE["u"] = ""
    return _BOT_USERNAME_CACHE["u"]

def get_referral_settings() -> dict:
    try:
        doc = db["settings"].find_one({"key": _REFERRAL_SETTINGS_KEY})
    except Exception:
        doc = None
    val = dict(_REFERRAL_DEFAULTS)
    if doc and isinstance(doc.get("value"), dict):
        raw = doc["value"]
        val["enabled"] = bool(raw.get("enabled", True))
        val["once_only"] = bool(raw.get("once_only", True))
        try:
            val["required_invites"] = max(1, int(raw.get("required_invites") or 10))
        except Exception:
            val["required_invites"] = 10
        try:
            val["reward_days"] = max(1, int(raw.get("reward_days") or 2))
        except Exception:
            val["reward_days"] = 2
    return val

def save_referral_settings(settings: dict):
    cur = get_referral_settings()
    cur.update(settings or {})
    cur["enabled"] = bool(cur.get("enabled", True))
    cur["once_only"] = bool(cur.get("once_only", True))
    try:
        cur["required_invites"] = max(1, int(cur.get("required_invites") or 10))
    except Exception:
        cur["required_invites"] = 10
    try:
        cur["reward_days"] = max(1, int(cur.get("reward_days") or 2))
    except Exception:
        cur["reward_days"] = 2
    db["settings"].update_one(
        {"key": _REFERRAL_SETTINGS_KEY},
        {"$set": {"value": cur}},
        upsert=True,
    )
    return cur

def parse_referral_payload(payload: str) -> int | None:
    p = (payload or "").strip()
    if not p:
        return None
    low = p.lower()
    if low.startswith("ref_"):
        p = p[4:]
    elif low.startswith("ref"):
        p = p[3:]
    else:
        return None
    p = p.strip().lstrip("_")
    try:
        rid = int(p)
        return rid if rid > 0 else None
    except Exception:
        return None

def referral_link_for(uid: int, bot_username: str) -> str:
    uname = (bot_username or "").lstrip("@")
    if not uname:
        return f"ref{uid}"
    return f"https://t.me/{uname}?start=ref{uid}"

def _is_known_bot_user(uid: int) -> bool:
    uid = int(uid)
    if BOT_USERS_COL.find_one({"user_id": uid}):
        return True
    if SUBS_COL.find_one(_user_id_filter(uid)):
        return True
    if PLUS_SUBS_COL.find_one(_user_id_filter(uid)):
        return True
    if USER_SETTINGS_COL.find_one({"user_id": uid}):
        return True
    try:
        if db["accounts"].find_one({"admin_id": uid}):
            return True
    except Exception:
        pass
    return False

def get_referrer_invite_count(referrer_id: int) -> int:
    return int(BOT_USERS_COL.count_documents({"referred_by": int(referrer_id)}))

def get_referrer_stats(referrer_id: int) -> dict:
    doc = REFERRER_STATS_COL.find_one({"user_id": int(referrer_id)}) or {}
    return {
        "user_id": int(referrer_id),
        "total_invites": int(doc.get("total_invites") or get_referrer_invite_count(referrer_id)),
        "rewards_claimed": int(doc.get("rewards_claimed") or 0),
    }

async def grant_subscription_days(user_id: int, days: int, *, note: str = "") -> int | None:

    days = max(0, int(days))
    if days <= 0:
        return None
    uid = int(user_id)
    grant_note = note or "referral"
    rec = SUBS_COL.find_one(_user_id_filter(uid))
    if rec:
        return await extend_user_days(uid, days, note=grant_note)
    return await add_subscription(uid, days, added_by=0, note=grant_note)

async def credit_referrer_if_needed(referrer_id: int):
    cfg = get_referral_settings()
    if not cfg.get("enabled"):
        return
    referrer_id = int(referrer_id)
    if referrer_id in ADMIN_IDS:
        return
    required = int(cfg.get("required_invites") or 10)
    reward_days = int(cfg.get("reward_days") or 2)
    total = get_referrer_invite_count(referrer_id)
    stats = get_referrer_stats(referrer_id)
    claimed = int(stats.get("rewards_claimed") or 0)
    due = (total // required) - claimed
    if due <= 0:
        REFERRER_STATS_COL.update_one(
            {"user_id": referrer_id},
            {"$set": {"user_id": referrer_id, "total_invites": total}},
            upsert=True,
        )
        return
    claimed_update = REFERRER_STATS_COL.update_one(
        {"user_id": referrer_id, "rewards_claimed": claimed},
        {
            "$set": {
                "user_id": referrer_id,
                "total_invites": total,
                "rewards_claimed": claimed + due,
                "last_reward_at": _ts_now(),
            }
        },
    )
    if claimed_update.matched_count == 0:
        logger.warning(
            "referral reward skipped (concurrent claim) referrer=%s claimed=%s",
            referrer_id,
            claimed,
        )
        return
    grant_days = due * reward_days
    await grant_subscription_days(
        referrer_id, grant_days, note=f"referral:{due}x{required}"
    )
    try:
        await bot.send_message(
            referrer_id,
            txt(
                referrer_id,
                "referral_reward_msg",
                invites=total,
                days=grant_days,
                required=required,
                reward=reward_days,
            ),
        )
    except Exception:
        pass

async def process_start_referral(uid: int, payload: str = ""):
    uid = int(uid)
    referrer_id = parse_referral_payload(payload)
    if referrer_id and referrer_id == uid:
        referrer_id = None
    cfg = get_referral_settings()
    if not cfg.get("enabled"):
        referrer_id = None
    once_only = bool(cfg.get("once_only", True))
    now = datetime.datetime.now(datetime.timezone.utc)

    existing = BOT_USERS_COL.find_one({"user_id": uid})

    if existing:
        if existing.get("referred_by") is not None:
            BOT_USERS_COL.update_one({"user_id": uid}, {"$set": {"last_seen": now}})
            return
        if once_only or existing.get("referral_locked"):
            BOT_USERS_COL.update_one({"user_id": uid}, {"$set": {"last_seen": now}})
            return
        if not referrer_id:
            BOT_USERS_COL.update_one({"user_id": uid}, {"$set": {"last_seen": now}})
            return
        res = BOT_USERS_COL.update_one(
            {"user_id": uid, "referred_by": None, "referral_locked": {"$ne": True}},
            {
                "$set": {
                    "referred_by": int(referrer_id),
                    "referral_locked": True,
                    "last_seen": now,
                }
            },
        )
        if not res.modified_count:
            return
    else:
        if once_only and _is_known_bot_user(uid):
            BOT_USERS_COL.update_one(
                {"user_id": uid},
                {
                    "$setOnInsert": {
                        "user_id": uid,
                        "first_seen": now,
                        "referred_by": None,
                        "referral_locked": True,
                    },
                    "$set": {"last_seen": now},
                },
                upsert=True,
            )
            return
        try:
            BOT_USERS_COL.insert_one(
                {
                    "user_id": uid,
                    "first_seen": now,
                    "last_seen": now,
                    "referred_by": int(referrer_id) if referrer_id else None,
                    "referral_locked": True,
                }
            )
        except Exception:
            return

    if referrer_id:
        total = get_referrer_invite_count(referrer_id)
        REFERRER_STATS_COL.update_one(
            {"user_id": int(referrer_id)},
            {
                "$set": {"user_id": int(referrer_id), "total_invites": total},
                "$setOnInsert": {"rewards_claimed": 0},
            },
            upsert=True,
        )
        await credit_referrer_if_needed(referrer_id)
        try:
            await bot.send_message(
                referrer_id,
                txt(referrer_id, "referral_new_invite", user=uid),
            )
        except Exception:
            pass

_bitpin_price_cache: dict = {"ts": 0.0, "symbol": "", "price": 0.0}

async def fetch_bitpin_price(symbol: str = "GRAM_IRT") -> float:
    symbol = (symbol or "GRAM_IRT").strip().upper()
    now = _time.time()
    if (
        _bitpin_price_cache.get("symbol") == symbol
        and _bitpin_price_cache.get("price")
        and now - float(_bitpin_price_cache.get("ts") or 0) < 45
    ):
        return float(_bitpin_price_cache["price"])
    urls = [
        "https://api.bitpin.ir/api/v1/mkt/tickers/",
        "https://api.bitpin.market/api/v1/mkt/tickers/",
    ]
    last_err = None
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
            items = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol") or "").upper()
                if sym != symbol:
                    continue
                price = float(item.get("price") or 0)
                if price > 0:
                    _bitpin_price_cache.update(
                        {"ts": now, "symbol": symbol, "price": price}
                    )
                    return price
        except Exception as e:
            last_err = e
            continue
    if last_err:
        logger.warning("Bitpin price fetch failed symbol=%s: %s", symbol, last_err)
    raise ValueError("BITPIN_PRICE_UNAVAILABLE")

def calc_gram_amount(price_toman: float, gram_irt: float, decimals: int = 4) -> float:
    if gram_irt <= 0:
        raise ValueError("INVALID_GRAM_PRICE")
    factor = 10 ** max(2, min(8, int(decimals or 4)))
    raw = float(price_toman) / float(gram_irt)
    return math.ceil(raw * factor) / factor

def format_crypto_amount(amount: float, decimals: int = 4) -> str:
    d = max(2, min(8, int(decimals or 4)))
    s = f"{float(amount):.{d}f}".rstrip("0").rstrip(".")
    return s or "0"

CRYPTO_USED_TX_COL = db["crypto_used_txs"]
try:
    CRYPTO_USED_TX_COL.create_index("tx_hash", unique=True)
except Exception:
    pass

def _gen_payment_code(uid: int) -> str:
    return f"RPT{uid % 100000}{secrets_token()}"

def secrets_token(n: int = 5) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))

def _normalize_addr(addr: str) -> str:
    return (addr or "").strip()

def _addr_match(a: str, b: str) -> bool:
    a = _normalize_addr(a)
    b = _normalize_addr(b)
    if not a or not b:
        return False
    if a == b:
        return True

    return a[-40:] == b[-40:] if len(a) > 40 and len(b) > 40 else False

def _amount_close(got: float, need: float, tol: float) -> bool:
    if need <= 0:
        return False
    return abs(got - need) <= max(need * tol, 10 ** (-6))

_http_client: httpx.AsyncClient | None = None
_tonapi_lock = asyncio.Lock()
_tonapi_last = 0.0

def _shared_http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=25.0)
    return _http_client

def _jetton_divisor() -> float:
    try:
        dec = int(get_crypto_settings().get("jetton_decimals") or 9)
    except Exception:
        dec = 9
    dec = max(0, min(18, dec))
    return float(10 ** dec)

def _raw_token_amount(raw_amt: float, *, jetton: bool) -> float:
    if raw_amt <= 0:
        return 0.0
    scale = _jetton_divisor() if jetton else 1e9
    if raw_amt > 1000:
        return raw_amt / scale
    return raw_amt

async def _tonapi_get(path: str, params: dict | None = None) -> dict | list | None:
    global _tonapi_last
    url = f"https://tonapi.io/v2{path}"
    last_err = None
    for attempt in range(3):
        try:
            async with _tonapi_lock:
                gap = 0.35 - (_time.monotonic() - _tonapi_last)
                if gap > 0:
                    await asyncio.sleep(gap)
                _tonapi_last = _time.monotonic()
            r = await _shared_http().get(url, params=params or {})
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP_{r.status_code}"
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            if r.status_code >= 400:
                logger.warning("tonapi get %s status=%s", path, r.status_code)
                return {"_error": True, "status": r.status_code}
            return r.json()
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.6 * (attempt + 1))
    logger.warning("tonapi get failed %s: %s", path, last_err)
    return {"_error": True, "status": 0}

async def fetch_incoming_crypto_payments(wallet: str, since_ts: int) -> list[dict]:

    wallet = _normalize_addr(wallet)
    if not wallet:
        return []
    out: list[dict] = []
    seen = set()
    crypto = get_crypto_settings()
    jetton = (crypto.get("jetton_master") or "").strip()

    data = await _tonapi_get(f"/accounts/{wallet}/events", {"limit": 40})
    api_down = isinstance(data, dict) and bool(data.get("_error"))
    events = None if api_down else ((data or {}).get("events") if isinstance(data, dict) else None)
    for ev in events or []:
        ts = int(ev.get("timestamp") or 0)
        if since_ts and ts and ts < since_ts - 60:
            continue
        event_id = str(ev.get("event_id") or "")
        for action in ev.get("actions") or []:
            if action.get("status") and action.get("status") != "ok":
                continue
            atype = action.get("type")
            if atype == "TonTransfer":
                body = action.get("TonTransfer") or {}
                recipient = ((body.get("recipient") or {}).get("address")) or ""

                amount_nano = float(body.get("amount") or 0)
                amount = amount_nano / 1e9
                comment = str(body.get("comment") or "")
                txh = event_id or str(body.get("tx_hash") or "")
                key = f"ton:{txh}:{amount}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "amount": amount,
                        "comment": comment,
                        "tx_hash": txh,
                        "kind": "TON",
                        "ts": ts,
                        "recipient": recipient,
                    }
                )
            elif atype == "JettonTransfer":
                body = action.get("JettonTransfer") or {}
                jetton_addr = (
                    ((body.get("jetton") or {}).get("address"))
                    or str(body.get("jetton_address") or "")
                )
                if jetton and jetton_addr and not (
                    _addr_match(jetton_addr, jetton)
                    or jetton[-20:] in jetton_addr
                    or jetton_addr[-20:] in jetton
                ):
                    continue
                qty = body.get("amount") or body.get("quantity") or "0"
                try:
                    raw_amt = float(qty)
                except Exception:
                    raw_amt = 0.0

                amount = _raw_token_amount(raw_amt, jetton=True)
                comment = str(body.get("comment") or "")
                txh = event_id or str(body.get("tx_hash") or "")
                key = f"jetton:{txh}:{amount}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "amount": amount,
                        "comment": comment,
                        "tx_hash": txh,
                        "kind": "JETTON",
                        "ts": ts,
                        "recipient": ((body.get("recipient") or {}).get("address")) or "",
                    }
                )


    if jetton:
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                r = await client.get(
                    "https://toncenter.com/api/v3/jetton/transfers",
                    params={
                        "owner_address": wallet,
                        "direction": "in",
                        "limit": 30,
                        "sort": "desc",
                        "jetton_master": jetton,
                    },
                )
                if r.status_code < 400:
                    payload = r.json()
                    for tr in payload.get("jetton_transfers") or []:
                        if tr.get("transaction_aborted"):
                            continue
                        ts = int(tr.get("transaction_now") or 0)
                        if since_ts and ts and ts < since_ts - 60:
                            continue
                        try:
                            amount = _raw_token_amount(float(tr.get("amount") or 0), jetton=True)
                        except Exception:
                            continue
                        txh = str(tr.get("transaction_hash") or "")
                        key = f"tc:{txh}:{amount}"
                        if not txh or key in seen:
                            continue
                        seen.add(key)
                        out.append(
                            {
                                "amount": amount,
                                "comment": "",
                                "tx_hash": txh,
                                "kind": "JETTON",
                                "ts": ts,
                            }
                        )
        except Exception as e:
            logger.warning("toncenter jetton fetch failed: %s", e)
            api_down = True

    if api_down and not out:
        return [{"_api_error": True}]
    return out

def _tx_already_used(tx_hash: str) -> bool:
    if not tx_hash:
        return True
    return CRYPTO_USED_TX_COL.find_one({"tx_hash": tx_hash}) is not None

def _claim_tx_used(tx_hash: str, request_id: str, user_id: int) -> bool:
    if not tx_hash:
        return True
    try:
        CRYPTO_USED_TX_COL.insert_one(
            {
                "tx_hash": tx_hash,
                "request_id": str(request_id),
                "user_id": int(user_id),
                "used_at": _ts_now(),
            }
        )
        return True
    except DuplicateKeyError:
        return False
    except Exception as e:
        logger.warning("claim tx failed %s: %s", tx_hash, e)
        return False

def _mark_tx_used(tx_hash: str, request_id: str, user_id: int):
    _claim_tx_used(tx_hash, request_id, user_id)

def _extract_tx_hash(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    for sep in (
        "transaction/",
        "transactions/",
        "tx/",
        "events/",
        "event/",
        "tonviewer.com/",
        "tonscan.org/",
        "tonapi.io/",
    ):
        if sep in low:
            idx = low.find(sep) + len(sep)
            t = t[idx:]
            low = t.lower()
            break
    t = t.split("?")[0].split("#")[0].strip().strip("/")
    if "/" in t:
        t = t.rstrip("/").split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9_\-+/=]", "", t)
    if len(cleaned) < 16:
        return ""
    return cleaned

def _tx_hash_match(a: str, b: str) -> bool:
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True

    if len(a) >= 16 and len(b) >= 16 and (a in b or b in a):
        return True
    return False

def _payments_from_tonapi_event(ev: dict) -> list[dict]:
    out = []
    if not isinstance(ev, dict):
        return out
    ts = int(ev.get("timestamp") or 0)
    event_id = str(ev.get("event_id") or "")
    for action in ev.get("actions") or []:
        if action.get("status") and action.get("status") != "ok":
            continue
        atype = action.get("type")
        if atype == "TonTransfer":
            body = action.get("TonTransfer") or {}
            amount = float(body.get("amount") or 0) / 1e9
            out.append(
                {
                    "amount": amount,
                    "comment": str(body.get("comment") or ""),
                    "tx_hash": event_id or str(body.get("tx_hash") or ""),
                    "kind": "TON",
                    "ts": ts,
                    "recipient": ((body.get("recipient") or {}).get("address")) or "",
                }
            )
        elif atype == "JettonTransfer":
            body = action.get("JettonTransfer") or {}
            qty = body.get("amount") or body.get("quantity") or "0"
            try:
                raw_amt = float(qty)
            except Exception:
                raw_amt = 0.0
            amount = _raw_token_amount(raw_amt, jetton=True)
            out.append(
                {
                    "amount": amount,
                    "comment": str(body.get("comment") or ""),
                    "tx_hash": event_id or str(body.get("tx_hash") or ""),
                    "kind": "JETTON",
                    "ts": ts,
                    "recipient": ((body.get("recipient") or {}).get("address")) or "",
                }
            )
    return out

async def verify_submitted_tx_hash(req: dict, tx_hash: str) -> dict | None:

    tx_hash = _extract_tx_hash(tx_hash)
    if not tx_hash or _tx_already_used(tx_hash):
        return None
    wallet = (req.get("crypto_wallet") or get_crypto_settings().get("wallet") or "").strip()
    need = float(req.get("crypto_amount") or 0)
    if not wallet or need <= 0:
        return None
    tol = float(get_crypto_settings().get("amount_tolerance") or 0.02)

    candidates: list[dict] = []
    ev = await _tonapi_get(f"/events/{tx_hash}")
    if isinstance(ev, dict) and (ev.get("actions") or ev.get("event_id")):
        candidates.extend(_payments_from_tonapi_event(ev))

    created = req.get("created_at")
    if isinstance(created, dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        since = int(created.timestamp()) - 300
    else:
        since = _ts_now() - 7200
    for p in await fetch_incoming_crypto_payments(wallet, since):
        if _tx_hash_match(str(p.get("tx_hash") or ""), tx_hash):
            candidates.append(p)

    for p in candidates:
        th = str(p.get("tx_hash") or tx_hash)
        if _tx_already_used(th):
            continue
        if not _amount_close(float(p.get("amount") or 0), need, tol):
            continue
        recip = str(p.get("recipient") or "")
        if recip and wallet and not _addr_match(recip, wallet):
            continue
        hit = dict(p)
        hit["tx_hash"] = th or tx_hash
        return hit
    return None

async def find_crypto_payment_match(req: dict) -> dict | None:
    submitted = str(req.get("submitted_tx_hash") or "").strip()
    if submitted:
        hit = await verify_submitted_tx_hash(req, submitted)
        if hit:
            return hit
    wallet = (req.get("crypto_wallet") or get_crypto_settings().get("wallet") or "").strip()
    need = float(req.get("crypto_amount") or 0)
    code = str(req.get("payment_code") or "").strip().upper().replace(" ", "")
    if not wallet or need <= 0:
        return None
    created = req.get("created_at")
    if isinstance(created, dt.datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        since = int(created.timestamp()) - 120
    else:
        since = _ts_now() - 3600
    tol = float(get_crypto_settings().get("amount_tolerance") or 0.02)
    payments = await fetch_incoming_crypto_payments(wallet, since)
    if payments and payments[0].get("_api_error"):
        return {"_api_error": True}
    amount_hits = []
    for p in payments:
        txh = str(p.get("tx_hash") or "")
        if _tx_already_used(txh):
            continue
        if not _amount_close(float(p.get("amount") or 0), need, tol):
            continue
        amount_hits.append(p)
        comment = str(p.get("comment") or "").upper().replace(" ", "")
        if code and comment and code in comment:
            return p
        if not code:
            return p
    if len(amount_hits) == 1:
        return amount_hits[0]
    return None

async def approve_purchase_request(req: dict, *, approved_by: int = 0, tx_hash: str = ""):
    request_id = req.get("_id")
    user_id = int(req["user_id"])
    plan = req["plan"]
    plan_days = int(req.get("days") or 0)
    if plan_days <= 0:
        prices = get_subscription_prices()
        plan_days = int(prices.get(plan, {}).get("days") or 0)
    claimed = PURCHASE_REQUESTS_COL.update_one(
        {
            "_id": request_id if not isinstance(request_id, str) else ObjectId(request_id),
            "status": {"$in": ["pending", "pending_crypto"]},
        },
        {
            "$set": {
                "status": "approved",
                "admin_note": tx_hash or "",
                "resolved_at": datetime.datetime.now(datetime.timezone.utc),
            }
        },
    )
    if not claimed.modified_count:
        return False
    oid = request_id if not isinstance(request_id, str) else ObjectId(request_id)
    if tx_hash and not _claim_tx_used(tx_hash, request_id, user_id):
        PURCHASE_REQUESTS_COL.update_one(
            {"_id": oid},
            {"$set": {"status": "pending_crypto", "admin_note": "tx_already_used"}},
        )
        return False
    if plan == "special":
        max_accounts = int(req.get("max_accounts") or 0)
        add_plus_subscription(
            user_id,
            plan_days,
            max_accounts,
            added_by=approved_by or user_id,
            admin_id=approved_by or (MAIN_ADMIN_ID or user_id),
        )
    else:
        await add_subscription(
            user_id,
            plan_days,
            added_by=approved_by or user_id,
            note=f"purchase:{request_id}",
        )
    try:
        plan_name = get_plan_name(user_id, plan)
        await bot.send_message(
            user_id,
            txt(user_id, "purchase_approved", plan_name=plan_name, days=plan_days),
        )
        welcome = txt(user_id, "main_menu_welcome")
        await bot.send_message(user_id, welcome, buttons=await kb_main(user_id))
    except Exception as e:
        logger.warning("approve notify failed uid=%s: %s", user_id, e)
    return True

_FORCE_JOIN_KEY = "force_join"

def get_force_join() -> dict:
    doc = SETTINGS_COL.find_one({"key": _FORCE_JOIN_KEY})
    val = {"enabled": False, "channels": []}
    if not doc or not isinstance(doc.get("value"), dict):
        return val
    raw = doc["value"]
    val["enabled"] = bool(raw.get("enabled", False))
    channels = []
    for ch in raw.get("channels") or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("chat_id") or "").strip()
        title = str(ch.get("title") or "").strip()
        if not cid or not title:
            continue
        channels.append(
            {
                "id": str(ch.get("id") or cid),
                "chat_id": cid,
                "title": title[:64],
                "username": str(ch.get("username") or "").strip().lstrip("@"),
                "url": str(ch.get("url") or "").strip(),
            }
        )
    val["channels"] = channels
    return val

def save_force_join(settings: dict):
    cur = get_force_join()
    if "enabled" in (settings or {}):
        cur["enabled"] = bool(settings["enabled"])
    if "channels" in (settings or {}) and isinstance(settings["channels"], list):
        cur["channels"] = settings["channels"]
    SETTINGS_COL.update_one(
        {"key": _FORCE_JOIN_KEY},
        {"$set": {"value": cur}},
        upsert=True,
    )
    return cur

def _fj_channel_url(ch: dict) -> str:
    url = (ch.get("url") or "").strip()
    if url.startswith("http"):
        return url
    uname = (ch.get("username") or "").strip().lstrip("@")
    if uname:
        return f"https://t.me/{uname}"
    return ""

def _channel_chat_ref(ch: dict):
    chat_ref = ch.get("chat_id") or ch.get("username") or ch.get("id")
    if not chat_ref:
        return None
    ref = str(chat_ref).strip()
    if ref.lstrip("-").isdigit():
        return int(ref)
    return ref


async def force_join_status(user_id: int, ch: dict) -> tuple[str, str]:
    chat_ref = _channel_chat_ref(ch)
    if chat_ref is None:
        return "unknown", "NO_CHAT_REF"
    try:
        entity = await bot.get_input_entity(chat_ref)
        res = await bot(
            functions.channels.GetParticipantRequest(
                channel=entity, participant=int(user_id)
            )
        )
    except errors.UserNotParticipantError:
        return "missing", ""
    except Exception as e:
        name = _tg_error_name(e) if isinstance(e, errors.RPCError) else e.__class__.__name__
        blob = f"{name} {e.__class__.__name__} {e}".upper()
        if "USER_NOT_PARTICIPANT" in blob or "USERNOTPARTICIPANT" in blob:
            return "missing", ""
        logger.warning(
            "force join check failed uid=%s ch=%s: %s", user_id, ch.get("title"), name
        )
        return "unknown", str(name or "ERROR")[:80]
    part = getattr(res, "participant", None)
    if isinstance(part, types.ChannelParticipantLeft):
        return "missing", ""
    if isinstance(part, types.ChannelParticipantBanned) and getattr(part, "left", False):
        return "missing", ""
    return "member", ""


async def _warn_admins_join_check(ch: dict, reason: str) -> None:
    key = f"jcheck_warn:{ch.get('chat_id') or ch.get('username') or ''}"
    try:
        if not await redis.set(key, "1", ex=3600, nx=True):
            return
    except Exception:
        return
    label = ch.get("title") or ch.get("chat_id") or ch.get("username") or "?"
    for admin in ADMINS:
        try:
            await bot.send_message(
                admin, txt(admin, "join_check_misconfig", channel=label, error=reason)
            )
        except Exception:
            pass


async def check_user_force_joined(user_id: int, ch: dict) -> bool:
    status, reason = await force_join_status(user_id, ch)
    if status == "unknown":
        await _warn_admins_join_check(ch, reason)
        return True
    return status == "member"

async def get_missing_force_joins(user_id: int) -> list[dict]:
    cfg = get_force_join()
    if not cfg.get("enabled"):
        return []
    missing = []
    for ch in cfg.get("channels") or []:
        if not await check_user_force_joined(user_id, ch):
            missing.append(ch)
    return missing

def build_force_join_buttons(uid: int, channels: list[dict] | None = None):
    cfg = get_force_join()
    channels = channels if channels is not None else (cfg.get("channels") or [])
    rows = []
    for ch in channels:
        url = _fj_channel_url(ch)
        title = (ch.get("title") or "Channel")[:64]
        if url:
            rows.append([Button.url(f"📢 {title}", url)])
        else:
            rows.append(
                [Button.inline(f"📢 {title}", data=b"fj_check")]
            )
    rows.append([Button.inline(txt(uid, "fj_check_btn"), data=b"fj_check")])
    return rows

async def show_force_join_gate(event, uid: int, missing: list[dict] | None = None):
    cfg = get_force_join()
    channels = missing if missing is not None else (cfg.get("channels") or [])
    text = txt(uid, "fj_user_prompt")
    buttons = build_force_join_buttons(uid, channels)
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(text, buttons=buttons)
            return
        except errors.MessageNotModifiedError:
            return
        except Exception:
            pass
    try:
        await event.respond(text, buttons=buttons)
    except Exception:
        try:
            await event.reply(text, buttons=buttons)
        except Exception:
            pass

async def ensure_force_join(event, uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    cfg = get_force_join()
    if not cfg.get("enabled") or not (cfg.get("channels") or []):
        return True
    missing = await get_missing_force_joins(uid)
    if not missing:
        return True
    await show_force_join_gate(event, uid, missing)
    return False

PURCHASE_REQUESTS_COL = db["purchase_requests"]
SETTINGS_COL = db["settings"]

def init_subscription_settings():
    if not SETTINGS_COL.find_one({"key": "subscription_prices"}):
        SETTINGS_COL.insert_one(
            {
                "key": "subscription_prices",
                "value": {
                    "normal": [
                        {"days": 1, "price": 100, "max_accounts": 0},
                        {"days": 30, "price": 2000, "max_accounts": 0},
                    ],
                    "special": [
                        {"days": 30, "price": 5000, "max_accounts": 5},
                        {"days": 90, "price": 10000, "max_accounts": 10},
                    ],
                },
            }
        )

init_subscription_settings()

_AI_SETTINGS_KEY = "ai_report_comments"
_AI_DEFAULTS = {
    "enabled": False,
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "refresh_every": 10,
    "cache": {},
}

def _ai_safe_kind(kind: str) -> str:
    sk = re.sub(r"[^a-zA-Z0-9_:\-.]", "", str(kind or "").strip())[:80]
    return sk or "general"

def get_ai_settings() -> dict:
    doc = SETTINGS_COL.find_one({"key": _AI_SETTINGS_KEY})
    val = {
        "enabled": False,
        "api_key": "",
        "base_url": _AI_DEFAULTS["base_url"],
        "model": _AI_DEFAULTS["model"],
        "refresh_every": 10,
        "cache": {},
    }
    if doc and isinstance(doc.get("value"), dict):
        raw = doc["value"]
        val["enabled"] = bool(raw.get("enabled", False))
        val["api_key"] = str(raw.get("api_key") or "")
        val["base_url"] = (
            str(raw.get("base_url") or _AI_DEFAULTS["base_url"]).rstrip("/")
        )
        val["model"] = str(raw.get("model") or _AI_DEFAULTS["model"])
        try:
            val["refresh_every"] = max(1, min(1000, int(raw.get("refresh_every") or 10)))
        except Exception:
            val["refresh_every"] = 10
        cache = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
        for kind, item in cache.items():
            if not isinstance(item, dict):
                continue
            sk = _ai_safe_kind(kind)
            try:
                used = max(0, int(item.get("used") or 0))
            except Exception:
                used = 0
            val["cache"][sk] = {
                "text": str(item.get("text") or ""),
                "used": used,
            }
    return val

def save_ai_settings(settings: dict):
    cur = get_ai_settings()
    incoming = dict(settings or {})
    replace_cache = "cache" in incoming and isinstance(incoming.get("cache"), dict)
    cache_patch = incoming.pop("cache", None) if replace_cache else None
    cur.update(incoming)
    if replace_cache:
        if cache_patch == {}:
            cur["cache"] = {}
        else:
            cache = dict(cur.get("cache") or {})
            for kind, item in (cache_patch or {}).items():
                if not isinstance(item, dict):
                    continue
                cache[_ai_safe_kind(kind)] = {
                    "text": str(item.get("text") or ""),
                    "used": max(0, int(item.get("used") or 0)),
                }
            cur["cache"] = cache
    cur["base_url"] = str(cur.get("base_url") or _AI_DEFAULTS["base_url"]).rstrip("/")
    try:
        cur["refresh_every"] = max(1, min(1000, int(cur.get("refresh_every") or 10)))
    except Exception:
        cur["refresh_every"] = 10
    SETTINGS_COL.update_one(
        {"key": _AI_SETTINGS_KEY},
        {"$set": {"value": cur}},
        upsert=True,
    )
    return cur

def _path_reason_summary(ctx_snapshot: dict) -> str:
    parts = []
    for step in ctx_snapshot.get("path") or []:
        t = (step.get("text") or step.get("raw_text") or "").strip()
        if t:
            parts.append(t)
    return " > ".join(parts)

def _ctx_reason_code(ctx_snapshot: dict) -> str:
    ctx_snapshot = ctx_snapshot or {}
    return _mode_reason_code(ctx_snapshot.get("mode")) or _reason_code_from_path(
        ctx_snapshot.get("path") or []
    )

def _ai_kind_for_ctx(ctx_snapshot: dict) -> str:
    code = _ctx_reason_code(ctx_snapshot)
    if code in ("scam", "fake"):
        return code
    if _path_looks_adult(ctx_snapshot):
        return "porn"
    if _path_looks_violence(ctx_snapshot):
        return "violence"
    if _path_looks_drugs(ctx_snapshot):
        return "drugs"
    if _path_looks_personal(ctx_snapshot):
        return "personal"
    keys = sorted(_path_option_keys(ctx_snapshot))
    if keys:
        return _ai_safe_kind("opt:" + "-".join(keys))
    mode = (ctx_snapshot.get("mode") or "general").strip() or "general"
    return _ai_safe_kind(f"mode:{mode}")

def _ai_user_prompt(kind: str, target: str = "", reason: str = "") -> str:
    where = f" Target: {target}." if target else ""
    reason = (reason or "").strip() or kind or "Terms of Service violation"
    return (
        "Write one unique Telegram report comment for moderators. "
        f"Report reason: {reason}. "
        "English only, serious tone, 2-4 sentences, plain text only, no quotes."
        f"{where}"
    )

class AIRequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = int(status or 0)
        self.message = str(message or "").strip()
        super().__init__(
            f"HTTP {self.status}: {self.message}" if self.status else self.message
        )


_AI_COMPLETION_TOKEN_MODELS = ("o1", "o3", "o4", "gpt-5")
_AI_RETRY_STATUSES = (408, 409, 429, 500, 502, 503, 504)


def _ai_base_url(settings: dict) -> str:
    return (settings.get("base_url") or _AI_DEFAULTS["base_url"]).strip().rstrip("/")


def _ai_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _ai_error_message(r) -> str:
    try:
        data = r.json()
    except Exception:
        return re.sub(r"\s+", " ", (r.text or "")).strip()[:300] or r.reason_phrase
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        parts = [str(err.get("message") or "").strip(), str(err.get("code") or err.get("type") or "").strip()]
        return " | ".join(p for p in parts if p)[:300]
    if isinstance(err, str):
        return err[:300]
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])[:300]
    return str(data)[:300]


def _ai_message_text(data) -> str:
    try:
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
    except Exception:
        return ""
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return str(content or "").strip()


async def _openai_chat(
    settings: dict,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    timeout: float = 90.0,
) -> str:
    key = (settings.get("api_key") or "").strip()
    if not key:
        raise ValueError("NO_API_KEY")
    model = (settings.get("model") or _AI_DEFAULTS["model"]).strip()
    url = f"{_ai_base_url(settings)}/chat/completions"
    use_completion_tokens = model.lower().rsplit("/", 1)[-1].startswith(
        _AI_COMPLETION_TOKEN_MODELS
    )
    send_temperature = not use_completion_tokens
    last_error: AIRequestError | None = None
    net_failures = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, 6):
            if net_failures >= 2:
                break
            payload: dict = {"model": model, "messages": messages}
            if use_completion_tokens:
                payload["max_completion_tokens"] = max(max_tokens * 4, 2000)
            else:
                payload["max_tokens"] = max_tokens
            if send_temperature:
                payload["temperature"] = temperature
            try:
                r = await client.post(url, headers=_ai_headers(key), json=payload)
            except httpx.TimeoutException:
                net_failures += 1
                last_error = AIRequestError(0, "TIMEOUT")
                await asyncio.sleep(2)
                continue
            except httpx.HTTPError as e:
                net_failures += 1
                last_error = AIRequestError(0, f"CONNECT_FAILED ({e.__class__.__name__})")
                await asyncio.sleep(2)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    raise AIRequestError(200, "INVALID_JSON")
                return _ai_message_text(data)
            msg = _ai_error_message(r)
            low = msg.lower()
            if r.status_code == 400:
                if "max_tokens" in low and not use_completion_tokens:
                    use_completion_tokens = True
                    continue
                if "max_completion_tokens" in low and use_completion_tokens:
                    use_completion_tokens = False
                    continue
                if "temperature" in low and send_temperature:
                    send_temperature = False
                    continue
            last_error = AIRequestError(r.status_code, msg)
            if r.status_code in _AI_RETRY_STATUSES and "quota" not in low and attempt < 5:
                try:
                    wait = float(r.headers.get("retry-after") or 0)
                except Exception:
                    wait = 0.0
                await asyncio.sleep(min(20.0, wait or 2.0 * attempt))
                continue
            break
    logger.warning("AI request failed model=%s url=%s: %s", model, url, last_error)
    raise last_error or AIRequestError(0, "UNKNOWN")


def ai_error_text(uid: int, e: Exception) -> str:
    if isinstance(e, AIRequestError):
        status = e.status
        low = e.message.lower()
        if status == 401:
            hint = txt(uid, "ai_err_401")
        elif status == 403 and ("country" in low or "region" in low or "territory" in low):
            hint = txt(uid, "ai_err_region")
        elif status == 403:
            hint = txt(uid, "ai_err_403")
        elif status == 404:
            hint = txt(uid, "ai_err_404")
        elif status == 429 and "quota" in low:
            hint = txt(uid, "ai_err_quota")
        elif status == 429:
            hint = txt(uid, "ai_err_429")
        elif status >= 500:
            hint = txt(uid, "ai_err_5xx")
        elif status == 0:
            hint = txt(uid, "ai_err_network")
        else:
            hint = ""
        detail = str(e)[:220]
        return f"{detail}\n{hint}" if hint else detail
    if isinstance(e, ValueError):
        return str(e)[:150]
    return e.__class__.__name__


async def _openai_chat_completion(settings: dict, prompt: str) -> str:
    text = await _openai_chat(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "You write short English report comments for Telegram moderation. "
                    "Output plain text only."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=220,
        temperature=0.95,
        timeout=45.0,
    )
    text = re.sub(r'^["“”\']+|["“”\']+$', "", text).strip()
    if not text:
        raise ValueError("EMPTY_AI_TEXT")
    return text[:4000]


async def _openai_list_models(settings: dict) -> list[str]:
    key = (settings.get("api_key") or "").strip()
    if not key:
        raise ValueError("NO_API_KEY")
    url = f"{_ai_base_url(settings)}/models"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=_ai_headers(key))
    except httpx.TimeoutException:
        raise AIRequestError(0, "TIMEOUT")
    except httpx.HTTPError as e:
        raise AIRequestError(0, f"CONNECT_FAILED ({e.__class__.__name__})")
    if r.status_code != 200:
        raise AIRequestError(r.status_code, _ai_error_message(r))
    try:
        data = r.json()
    except Exception:
        raise AIRequestError(200, "INVALID_JSON")
    ids = []
    for item in data.get("data") or []:
        mid = item.get("id") if isinstance(item, dict) else None
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())
    ids = sorted(set(ids))
    preferred = [
        m
        for m in ids
        if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-", "ft:"))
        or "gpt" in m.lower()
    ]
    return preferred or ids

_ai_refresh_locks: dict[str, asyncio.Lock] = {}

def _ai_lock(kind: str) -> asyncio.Lock:
    if kind not in _ai_refresh_locks:
        _ai_refresh_locks[kind] = asyncio.Lock()
    return _ai_refresh_locks[kind]

async def _get_ai_cached_comment(
    kind: str, target: str = "", reason: str = ""
) -> str | None:
    settings = get_ai_settings()
    if not settings.get("enabled") or not (settings.get("api_key") or "").strip():
        return None
    kind = _ai_safe_kind(kind)
    every = int(settings.get("refresh_every") or 10)
    async with _ai_lock(kind):
        settings = get_ai_settings()
        cache = dict((settings.get("cache") or {}).get(kind) or {})
        text = (cache.get("text") or "").strip()
        used = int(cache.get("used") or 0)
        if text and used < every:
            cache["used"] = used + 1
            settings.setdefault("cache", {})[kind] = cache
            save_ai_settings({"cache": settings["cache"]})
            return text[:4000]
        try:
            fresh = await _openai_chat_completion(
                settings, _ai_user_prompt(kind, target, reason=reason)
            )
        except Exception as e:
            logger.warning("AI comment refresh failed kind=%s: %s", kind, e)
            if text:
                cache["used"] = used + 1
                settings.setdefault("cache", {})[kind] = cache
                save_ai_settings({"cache": settings["cache"]})
                return text[:4000]
            return None
        settings.setdefault("cache", {})[kind] = {"text": fresh, "used": 1}
        save_ai_settings({"cache": settings["cache"]})
        return fresh[:4000]

def get_packages(plan: str) -> list:
    doc = SETTINGS_COL.find_one({"key": "subscription_prices"})
    if not doc:
        return []
    data = doc.get("value", {})
    if isinstance(data.get(plan), dict):
        old = data[plan]
        return [
            {
                "days": old.get("days", 30),
                "price": old.get("price", 0),
                "max_accounts": old.get("max_accounts", 0),
            }
        ]
    return data.get(plan, [])

def set_packages(plan: str, packages: list):
    doc = SETTINGS_COL.find_one({"key": "subscription_prices"})
    if not doc:
        data = {"normal": [], "special": []}
    else:
        data = doc.get("value", {})
    data[plan] = packages
    SETTINGS_COL.update_one(
        {"key": "subscription_prices"}, {"$set": {"value": data}}, upsert=True
    )

def add_package(plan: str, days: int, price: int, max_accounts: int = 0):
    packages = get_packages(plan)
    packages.append({"days": days, "price": price, "max_accounts": max_accounts})
    set_packages(plan, packages)

def remove_package(plan: str, index: int):
    packages = get_packages(plan)
    if 0 <= index < len(packages):
        packages.pop(index)
        set_packages(plan, packages)
        return True
    return False

def get_subscription_prices():
    doc = SETTINGS_COL.find_one({"key": "subscription_prices"})
    return doc.get("value", {}) if doc else {}

def set_subscription_prices(prices: dict):
    SETTINGS_COL.update_one(
        {"key": "subscription_prices"}, {"$set": {"value": prices}}, upsert=True
    )

PANEL_PERMISSION_ACTIONS = [
    "accounts",
    "report_msg",
    "report_story",
    "report_bot",
    "report_scam",
    "report_fake",
    "report_profile",
    "join_request",
    "partners",
]

def init_panel_permissions():
    doc = SETTINGS_COL.find_one({"key": "panel_permissions"})
    if not doc:
        SETTINGS_COL.insert_one(
            {
                "key": "panel_permissions",
                "value": {
                    "normal": list(PANEL_PERMISSION_ACTIONS),
                    "special": list(PANEL_PERMISSION_ACTIONS),
                },
            }
        )
        return

    value = doc.get("value") or {}
    changed = False
    for plan in ("normal", "special"):
        actions = list(value.get(plan) or [])
        if "report_profile" not in actions:

            if "report_scam" in actions:
                actions.insert(actions.index("report_scam") + 1, "report_profile")
            else:
                actions.append("report_profile")
            value[plan] = actions
            changed = True
        if "report_fake" not in actions and "report_scam" in actions:
            actions.insert(actions.index("report_scam") + 1, "report_fake")
            value[plan] = actions
            changed = True
    if changed:
        SETTINGS_COL.update_one(
            {"key": "panel_permissions"}, {"$set": {"value": value}}
        )

init_panel_permissions()

def get_panel_permissions() -> dict:
    doc = SETTINGS_COL.find_one({"key": "panel_permissions"})
    if not doc:
        return {
            "normal": list(PANEL_PERMISSION_ACTIONS),
            "special": list(PANEL_PERMISSION_ACTIONS),
        }
    value = doc.get("value", {})
    normal = value.get("normal")
    special = value.get("special")
    if normal is None:
        normal = list(PANEL_PERMISSION_ACTIONS)
    if special is None:
        special = list(PANEL_PERMISSION_ACTIONS)
    return {"normal": normal, "special": special}

def set_panel_permissions(plan: str, actions: list):
    perms = get_panel_permissions()
    perms[plan] = actions
    SETTINGS_COL.update_one(
        {"key": "panel_permissions"}, {"$set": {"value": perms}}, upsert=True
    )

def toggle_panel_permission(plan: str, action: str):
    perms = get_panel_permissions()
    actions = perms.get(plan, [])
    if action in actions:
        actions.remove(action)
    else:
        actions.append(action)
    set_panel_permissions(plan, actions)
    return actions

def get_user_plan(uid: int) -> str:
    if get_plus_subscription(uid):
        return "special"
    return "normal"

def has_panel_permission(uid: int, action: str) -> bool:
    if uid in ADMIN_IDS:
        return True
    if action not in PANEL_PERMISSION_ACTIONS:
        return True
    plan = get_user_plan(uid)
    perms = get_panel_permissions()
    return action in perms.get(plan, [])

def create_purchase_request(
    user_id: int,
    plan: str,
    price: int,
    photo_file_id: str,
    days: int,
    max_accounts: int = 0,
    payment_method: str = "card",
    crypto_amount: float = 0,
    crypto_price: float = 0,
    crypto_asset: str = "",
    crypto_network: str = "",
    crypto_wallet: str = "",
    payment_code: str = "",
    status: str = "pending",
):
    doc = {
        "user_id": user_id,
        "plan": plan,
        "price": price,
        "receipt_photo_id": photo_file_id,
        "days": days,
        "max_accounts": max_accounts,
        "payment_method": payment_method or "card",
        "crypto_amount": float(crypto_amount or 0),
        "crypto_price": float(crypto_price or 0),
        "crypto_asset": crypto_asset or "",
        "crypto_network": crypto_network or "",
        "crypto_wallet": crypto_wallet or "",
        "payment_code": (payment_code or "").strip().upper(),
        "status": status or "pending",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "admin_note": "",
    }
    return PURCHASE_REQUESTS_COL.insert_one(doc).inserted_id

def update_purchase_request_status(request_id, status: str, admin_note: str = ""):
    PURCHASE_REQUESTS_COL.update_one(
        {"_id": ObjectId(request_id)},
        {
            "$set": {
                "status": status,
                "admin_note": admin_note,
                "resolved_at": datetime.datetime.now(datetime.timezone.utc),
            }
        },
    )

def get_pending_requests():
    return list(
        PURCHASE_REQUESTS_COL.find(
            {"status": {"$in": ["pending", "pending_crypto"]}}
        ).sort("created_at", -1)
    )

async def crypto_auto_verify_task():

    while True:
        try:
            await asyncio.sleep(45)
        except asyncio.CancelledError:
            raise
        try:
            if not get_crypto_settings().get("auto_verify", True):
                continue
            if not get_crypto_settings().get("enabled"):
                continue

            pending = list(
                PURCHASE_REQUESTS_COL.find(
                    {"status": "pending_crypto", "payment_method": "crypto"}
                )
                .sort("created_at", 1)
                .limit(30)
            )
            for req in pending:
                try:
                    match = await find_crypto_payment_match(req)
                    if not match or match.get("_api_error"):
                        continue
                    fresh = PURCHASE_REQUESTS_COL.find_one(
                        {"_id": req["_id"], "status": "pending_crypto"}
                    )
                    if not fresh:
                        continue
                    await approve_purchase_request(
                        fresh,
                        approved_by=0,
                        tx_hash=str(match.get("tx_hash") or ""),
                    )
                    uid = int(fresh.get("user_id") or 0)
                    if uid:
                        try:
                            await ctx_pop(uid)
                        except Exception:
                            pass
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                txt(
                                    admin_id,
                                    "crypto_auto_admin",
                                    user=uid,
                                    amount=format_crypto_amount(
                                        float(fresh.get("crypto_amount") or 0), 4
                                    ),
                                    asset=fresh.get("crypto_asset") or "GRAM",
                                    tx=str(match.get("tx_hash") or "")[:24],
                                ),
                            )
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("crypto auto-verify one failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("crypto auto-verify loop: %s", e)

@bot.on(events.CallbackQuery(pattern=b"set_prices"))
async def admin_price_menu(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    await ctx_pop(uid)
    text = txt(uid, "price_menu_text")
    buttons = [
        [Button.inline(txt(uid, "manage_normal"), data=b"price_normal")],
        [Button.inline(txt(uid, "manage_special"), data=b"price_special")],
        [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
    ]
    await event.edit(text, buttons=buttons)

@bot.on(events.CallbackQuery(pattern=rb"^price_(normal|special)$"))
async def price_plan_menu(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    try:
        await event.answer()
    except Exception:
        pass
    plan = event.data.decode().split("_", 1)[1]
    try:
        await send_price_plan_menu(uid, plan, edit_msg=event)
    except Exception:
        await send_price_plan_menu(uid, plan)

@bot.on(events.CallbackQuery(pattern=b"price_add:"))
async def price_add_start(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    plan = event.data.decode().split(":")[1]
    ctx = {"mode": "add_package", "plan": plan, "step": "days"}
    await ctx_set(uid, ctx)
    await event.answer()
    await event.edit(
        txt(uid, "enter_days"), buttons=conv_cancel_buttons(uid, b"set_prices")
    )

@bot.on(events.NewMessage)
async def price_add_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        await ctx_pop(uid)
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "add_package":
        return
    step = ctx.get("step")
    plan = ctx.get("plan") or "normal"
    raw = (event.raw_text or "").strip()
    try:
        value = int(raw.replace(",", ""))
    except ValueError:
        value = None
    minimum = 0 if step == "max_accounts" else 1
    if value is None or value < minimum:
        await event.reply(txt(uid, "invalid_number"))
        raise events.StopPropagation

    if step == "days":
        ctx["days"] = value
        ctx["step"] = "price"
        await ctx_set(uid, ctx)
        await event.reply(txt(uid, "enter_price"))
    elif step == "price":
        ctx["price"] = value
        if plan == "special":
            ctx["step"] = "max_accounts"
            await ctx_set(uid, ctx)
            await event.reply(txt(uid, "enter_max_accounts"))
        else:
            add_package(plan, ctx["days"], value, 0)
            await ctx_pop(uid)
            await event.reply(txt(uid, "package_added"))
            await send_price_plan_menu(uid, plan)
    elif step == "max_accounts":
        add_package(plan, ctx["days"], ctx["price"], value)
        await ctx_pop(uid)
        await event.reply(txt(uid, "package_added"))
        await send_price_plan_menu(uid, plan)
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"price_remove:"))
async def price_remove(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    parts = event.data.decode().split(":")
    try:
        plan = parts[1]
        idx = int(parts[2])
    except (IndexError, ValueError):
        await event.answer(txt(uid, "invalid_input"), alert=True)
        return
    remove_package(plan, idx)
    await event.answer(txt(uid, "package_removed"), alert=True)
    try:
        await send_price_plan_menu(uid, plan, edit_msg=event)
    except Exception:
        await send_price_plan_menu(uid, plan)

async def drop_account(
    session_id: str, reason: str = "unknown_reason", send_notification: bool = True
):
    phone = None
    admin_id = None
    try:
        obj_id = ObjectId(session_id)
    except Exception:
        return
    try:
        acc = db["accounts"].find_one({"_id": obj_id})
        if acc:
            phone = acc.get("phone")
            admin_id = acc.get("admin_id")
    except Exception:
        pass
    try:
        db["accounts"].delete_one({"_id": obj_id})
    except Exception:
        pass
    if phone:
        try:
            path = f"session/{phone}.json"
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    try:
        for key in list(PEER_CACHE.keys()):
            if key[0] == session_id:
                _peer_cache_pop(key)
    except Exception:
        pass
    try:
        _SESS_PHONE_CACHE.pop(str(session_id), None)
    except Exception:
        pass
    if send_notification and phone and admin_id:
        try:
            text = txt(admin_id, "account_deleted_reason", phone=phone, reason=reason)
            await bot.send_message(admin_id, text)
        except Exception:
            pass


_SESS_PHONE_CACHE: dict[str, str] = {}


def sess_phone(sess_id: str) -> str:
    sid = str(sess_id or "")
    if not sid:
        return "?"
    cached = _SESS_PHONE_CACHE.get(sid)
    if cached:
        return cached
    phone = sid[-8:]
    try:
        acc = db["accounts"].find_one({"_id": ObjectId(sid)}, {"phone": 1})
        if acc and acc.get("phone"):
            phone = str(acc.get("phone")).replace(" ", "")
    except Exception:
        pass
    _SESS_PHONE_CACHE[sid] = phone
    return phone


def _report_log_bits(sess_id: str, ctx_snapshot: dict | None = None, **extra) -> str:
    ctx_snapshot = ctx_snapshot or {}
    bits = [
        f"phone={sess_phone(sess_id)}",
        f"target={(ctx_snapshot.get('target') or '?')!s}"[:120],
        f"mode={ctx_snapshot.get('mode') or '?'}",
    ]
    try:
        ids = _pick_msg_ids_for_session(ctx_snapshot, sess_id)
    except Exception:
        ids = list(ctx_snapshot.get("msg_ids") or [])[:8]
    if ids:
        bits.append(f"msg_ids={ids}")
    kind = ctx_snapshot.get("entity_kind") or _infer_entity_kind(
        ctx_snapshot.get("target") or ""
    )
    if kind:
        bits.append(f"entity={kind}")
    for k, v in extra.items():
        if v is not None:
            bits.append(f"{k}={v}")
    return " ".join(bits)


def join_choice_buttons(uid: int) -> list:
    return [
        [
            Button.inline(txt(uid, "join_yes_btn"), b"join_yes"),
            Button.inline(txt(uid, "join_no_btn"), b"join_no"),
        ],
        [Button.inline(txt(uid, "cancel_choice"), b"join_cancel")],
    ]


async def _probe_target_kind(target: str) -> str:
    """Classify target via bot: user|bot|channel|chat|invite|unknown."""
    kind, token = _parse_target(target)
    if kind == "invite":
        return "invite"
    if kind == "unknown" or not token:
        return "unknown"
    if kind == "id":
        tid = str(token)
        if tid.startswith("-100") or (tid.lstrip("-").isdigit() and len(tid) >= 8):
            return "channel"
    try:
        if kind == "username":
            ent = await bot.get_entity(token)
        elif kind == "id":
            ent = await bot.get_entity(int(token))
        else:
            return "unknown"
        if isinstance(ent, types.User):
            return "bot" if getattr(ent, "bot", False) else "user"
        if isinstance(ent, types.Channel):
            return "channel"
        if isinstance(ent, types.Chat):
            return "chat"
    except Exception as e:
        logger.info("probe target kind failed target=%r: %s", target, e)
        if kind == "id":
            return "channel"
    return "unknown"

async def _resolve_story_peer(cli, target: str, sess_key: str, skip_join: bool = False):

    peer = await join_group(cli, target, sess_key, skip_join=skip_join)
    if _is_valid_peer(peer):
        return peer

    try:
        kind, token = _parse_target(target)
        if kind == "username" and token:
            ent = await cli.get_entity(token)
            return _entity_to_peer(ent)
        if kind == "id" and token:
            ent = await cli.get_entity(int(token))
            return _entity_to_peer(ent)
        ent = await cli.get_entity(target)
        return _entity_to_peer(ent)
    except Exception as e:
        logger.warning("resolve story peer failed target=%r: %s", target, e)
        return None

def _story_item_meta(s) -> dict | None:

    if s is None:
        return None
    deleted_t = getattr(types, "StoryItemDeleted", None)
    skipped_t = getattr(types, "StoryItemSkipped", None)
    if deleted_t and isinstance(s, deleted_t):
        return None
    if skipped_t and isinstance(s, skipped_t):
        return None

    if type(s).__name__ in ("StoryItemDeleted", "StoryItemSkipped"):
        return None
    sid = getattr(s, "id", None)
    if not isinstance(sid, int) or sid <= 0:
        return None
    ts = int(s.date.timestamp()) if getattr(s, "date", None) else None
    return {"id": sid, "date": ts}

async def fetch_peer_stories_meta(
    sample_sess: str, target, limit: int = 100
) -> list[dict]:
    cli, fail = await connect_session_client(sample_sess)
    if not cli:
        logger.warning("fetch stories: no client sess=%s fail=%s", sample_sess, fail)
        return []
    try:
        peer = await _resolve_story_peer(cli, target, sample_sess, skip_join=False)
        if not peer:
            logger.warning("fetch stories: cannot resolve peer target=%r", target)
            return []
        res = await cli(functions.stories.GetPeerStoriesRequest(peer=peer))
        items = getattr(res, "stories", None)
        story_list = getattr(items, "stories", []) if items else []
        meta = []
        for s in list(story_list)[: max(1, int(limit))]:
            m = _story_item_meta(s)
            if m:
                meta.append(m)
        return meta
    except errors.RPCError as e:
        if _is_frozen(e) or _is_unauthorized_like(e):
            await drop_account(sample_sess, reason="frozen/unauthorized")
        logger.warning("fetch stories rpc: %s", e)
        return []
    except Exception as e:
        logger.warning("fetch stories error: %s", e)
        return []
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass

async def fetch_highlights_page(
    sample_sess: str, target, limit: int = 30, offset_id: int = 0
) -> tuple[list[dict], int | None]:
    cli, fail = await connect_session_client(sample_sess)
    if not cli:
        return [], None
    try:
        peer = await _resolve_story_peer(cli, target, sample_sess, skip_join=False)
        if not peer:
            return [], None
        stories = []
        try:
            res = await cli(
                functions.stories.GetStoriesArchiveRequest(
                    peer=peer, offset_id=offset_id, limit=limit
                )
            )
            stories = getattr(res, "stories", []) or []
        except Exception:
            try:
                res = await cli(
                    functions.stories.GetPinnedStoriesRequest(
                        peer=peer, offset_id=offset_id, limit=limit
                    )
                )
                stories = getattr(res, "stories", []) or []
            except Exception:
                stories = []
        items = []
        for s in stories:
            m = _story_item_meta(s)
            if m:
                items.append(m)
        next_offset = (min([m["id"] for m in items]) - 1) if items else None
        return items, next_offset
    except errors.RPCError as e:
        if _is_frozen(e) or _is_unauthorized_like(e):
            await drop_account(sample_sess, reason="frozen/unauthorized")
        return [], None
    except Exception as e:
        logger.warning("fetch highlights error: %s", e)
        return [], None
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass

async def add_accunte(event, conv, phone):
    uid = UID(event)
    try:
        file_path = f"session/{phone}.json"
        async with aiofiles.open(file_path, "r", encoding="utf-8") as file:
            content = await file.read()
            user_data = json.loads(content)
        api_id_new = user_data["api_id"]
        api_hash_new = user_data["api_hash"]
        phone_new = user_data["phone"].replace(".", "")
        device_profile = _pick_device_profile()
        login_proxy = pick_proxy_dict(None)
        proxy_kw = proxy_client_kwargs(login_proxy) if login_proxy else {}
        if login_proxy and login_proxy.get("id"):
            user_data["proxy_id"] = login_proxy["id"]
        new_client = TelegramClient(
            StringSession(),
            api_id_new,
            api_hash_new,
            timeout=20,
            **proxy_kw,
            **_client_device_kwargs(device_profile),
        )
        await new_client.connect()
        try:
            if not await new_client.is_user_authorized():
                try:
                    phone_hash = await new_client.send_code_request(phone_new)
                    phone_code_hash = phone_hash.phone_code_hash
                    await conv.send_message(
                        txt(uid, "send_code_prompt"), buttons=conv_cancel_buttons(uid)
                    )
                    code_login = await menu_safe_response(conv)
                    code_text = re.sub(r"\D", "", code_login.raw_text or "")
                    if not re.fullmatch(r"\d{4,8}", code_text):
                        await event.reply(txt(uid, "invalid_code"))
                        return False
                except errors.rpcerrorlist.PhoneNumberInvalidError:
                    await event.reply(txt(uid, "wrong_number"))
                    return False
                except errors.FloodWaitError:
                    raise
                except errors.RPCError as e:
                    await event.reply(txt(uid, "error_occurred", error=_rpc_err_info(e)))
                    return False
                try:
                    await new_client.sign_in(
                        phone_new, code_text, phone_code_hash=phone_code_hash
                    )
                    await event.reply(txt(uid, "login_success"))
                    string_session = new_client.session.save()
                    user_data["StringSession"] = string_session
                    user_data["device_profile"] = device_profile
                    async with aiofiles.open(file_path, "w", encoding="utf-8") as tf:
                        await tf.write(
                            json.dumps(user_data, ensure_ascii=False)
                        )
                    return user_data
                except errors.SessionPasswordNeededError:
                    await conv.send_message(
                        txt(uid, "2fa_password"), buttons=conv_cancel_buttons(uid)
                    )
                    password_2fa = await menu_safe_response(conv)
                    try:
                        await new_client.sign_in(password=(password_2fa.raw_text or "").strip())
                        await event.reply(txt(uid, "login_success"))
                        string_session = new_client.session.save()
                        user_data["StringSession"] = string_session
                        user_data["device_profile"] = device_profile
                        async with aiofiles.open(
                            file_path, "w", encoding="utf-8"
                        ) as tf:
                            await tf.write(
                                json.dumps(user_data, ensure_ascii=False)
                            )
                        return user_data
                    except (
                        errors.SessionPasswordNeededError,
                        errors.PasswordHashInvalidError,
                    ):
                        await event.reply(txt(uid, "wrong_password"))
                        return False
                    except errors.FloodWaitError:
                        raise
                    except errors.RPCError as e:
                        await event.reply(txt(uid, "error_occurred", error=_rpc_err_info(e)))
                        return False
                except errors.PhoneCodeInvalidError:
                    await event.reply(txt(uid, "wrong_login_code"))
                    return False
                except errors.PhoneCodeExpiredError:
                    await event.reply(txt(uid, "code_expired"))
                    return False
                except errors.PasswordHashInvalidError:
                    await event.reply(txt(uid, "wrong_2fa_password"))
                    return False
                except errors.FloodWaitError:
                    raise
                except errors.RPCError as e:
                    await event.reply(txt(uid, "error_occurred", error=_rpc_err_info(e)))
                    return False
            else:
                await event.reply(txt(uid, "already_logged_in"))
                user_data["device_profile"] = device_profile
                string_session = new_client.session.save()
                user_data["StringSession"] = string_session
                return user_data
        except errors.FloodWaitError as e:
            await event.reply(txt(uid, "flood_wait", seconds=e.seconds))
            return False
        finally:
            await _safe_disconnect(new_client)
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        logger.warning("add account connect failed phone=%s: %s", phone, e.__class__.__name__)
        try:
            await event.reply(txt(uid, "error_occurred", error="CONNECT_FAILED"))
        except Exception:
            pass
        return False

def _pick_device_profile() -> dict:
    profiles = globals().get("DEVICE_PROFILES") or []
    if isinstance(profiles, list) and profiles:
        chosen = random.choice(profiles)
        if isinstance(chosen, dict) and chosen.get("device_model"):
            return {
                "device_model": str(chosen.get("device_model") or "Samsung Galaxy A32"),
                "system_version": str(chosen.get("system_version") or "SDK 31"),
                "app_version": str(
                    chosen.get("app_version") or "plus messenger 12.10.1.1"
                ),
                "lang_code": str(chosen.get("lang_code") or "en"),
                "system_lang_code": str(chosen.get("system_lang_code") or "en-US"),
            }
    return {
        "device_model": "Samsung Galaxy A32",
        "system_version": "SDK 31",
        "app_version": "plus messenger 12.10.1.1",
        "lang_code": "en",
        "system_lang_code": "en-US",
    }

def _client_device_kwargs(profile: dict | None = None) -> dict:

    p = profile if isinstance(profile, dict) else None
    if not p or not p.get("device_model"):
        p = _pick_device_profile()
    return {
        "device_model": p.get("device_model") or "Samsung Galaxy A32",
        "system_version": p.get("system_version") or "SDK 31",
        "app_version": p.get("app_version") or "plus messenger 12.10.1.1",
        "lang_code": p.get("lang_code") or "en",
        "system_lang_code": p.get("system_lang_code") or "en-US",
    }

def _ensure_account_device_profile(user: dict) -> dict:

    profile = user.get("device_profile")
    if isinstance(profile, dict) and profile.get("device_model"):
        return profile
    profile = _pick_device_profile()
    try:
        db["accounts"].update_one(
            {"_id": user["_id"]}, {"$set": {"device_profile": profile}}
        )
    except Exception:
        pass
    return profile

def _proxy_id_for(host: str, port: int, extra: str = "") -> str:
    return base64.urlsafe_b64encode(f"{host}:{port}:{extra}".encode()).decode()[:22]

def parse_mtproto_proxy_line(line: str) -> dict | None:

    raw = (line or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if "t.me/proxy" not in low and "tg://proxy" not in low and "server=" not in low:
        return None
    try:
        if "://" not in raw and "server=" in raw:
            raw = "https://t.me/proxy?" + raw.lstrip("?")
        if raw.lower().startswith("tg://"):
            raw = "https://t.me/" + raw.split("://", 1)[1]
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        host = (qs.get("server") or [None])[0]
        port_s = (qs.get("port") or [None])[0]
        secret = (qs.get("secret") or [None])[0]
        if not host or not port_s or not secret:
            return None
        host = unquote(str(host).strip())
        secret = unquote(str(secret).strip())
        port_i = int(str(port_s).strip())
        if not host or not secret or not (1 <= port_i <= 65535):
            return None
        pid = _proxy_id_for(host, port_i, secret[:24])
        return {
            "id": pid,
            "type": "mtproto",
            "host": host,
            "port": port_i,
            "secret": secret,
            "username": None,
            "password": None,
            "raw": f"https://t.me/proxy?server={host}&port={port_i}&secret={secret}",
            "enabled": True,
            "fails": 0,
        }
    except Exception:
        return None

def parse_proxy_line(line: str) -> dict | None:
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    mt = parse_mtproto_proxy_line(raw)
    if mt:
        return mt
    raw = raw.replace("socks5://", "").replace("socks5h://", "").replace(
        "http://", ""
    ).replace("https://", "")
    host = port = user = password = None
    if "@" in raw:
        creds, hostport = raw.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        parts = hostport.split(":")
        if len(parts) >= 2:
            host, port = parts[0], parts[1]
    else:
        parts = raw.split(":")
        if len(parts) == 2:
            host, port = parts[0], parts[1]
        elif len(parts) >= 4:
            host, port, user = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
        else:
            return None
    try:
        port_i = int(str(port).strip())
    except Exception:
        return None
    host = (host or "").strip()
    if not host or not (1 <= port_i <= 65535):
        return None
    pid = _proxy_id_for(host, port_i, user or "")
    return {
        "id": pid,
        "type": "socks5",
        "host": host,
        "port": port_i,
        "username": (user or "").strip() or None,
        "password": (password or "").strip() or None,
        "secret": None,
        "raw": f"{host}:{port_i}"
        + (f":{user}:{password}" if user is not None else ""),
        "enabled": True,
        "fails": 0,
    }

def proxy_to_tuple(p: dict):

    if not p:
        return None
    if (p.get("type") or "socks5") == "mtproto":
        return None
    host = p.get("host")
    port = p.get("port")
    if not host or not port:
        return None
    user = p.get("username")
    password = p.get("password")
    if user:
        return (socks.SOCKS5, host, int(port), True, user, password or "")
    return (socks.SOCKS5, host, int(port))

def proxy_client_kwargs(p: dict | None) -> dict:

    if not p:
        return {}
    ptype = (p.get("type") or "socks5").lower()
    host = p.get("host")
    port = p.get("port")
    if not host or not port:
        return {}
    if ptype == "mtproto":
        secret = (p.get("secret") or "").strip()
        if not secret:
            return {}
        return {
            "connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (host, int(port), secret),
        }
    tup = proxy_to_tuple(p)
    return {"proxy": tup} if tup else {}

def get_proxy_config() -> dict:
    doc = SETTINGS_COL.find_one({"key": "proxy_config"})
    if not doc:
        return {"enabled": False, "proxies": []}
    return {
        "enabled": bool(doc.get("enabled", False)),
        "proxies": list(doc.get("proxies") or []),
    }

def save_proxy_config(enabled: bool, proxies: list):
    SETTINGS_COL.update_one(
        {"key": "proxy_config"},
        {"$set": {"enabled": bool(enabled), "proxies": proxies}},
        upsert=True,
    )

def init_proxy_settings():
    doc = SETTINGS_COL.find_one({"key": "proxy_config"})
    if doc:
        return
    proxies = []
    if os.path.exists("proxy.txt"):
        try:
            with open("proxy.txt", "r", encoding="utf-8") as f:
                for line in f:
                    parsed = parse_proxy_line(line)
                    if parsed and not any(x["id"] == parsed["id"] for x in proxies):
                        proxies.append(parsed)
        except Exception as e:
            logger.warning("proxy.txt import failed: %s", e)
    SETTINGS_COL.insert_one(
        {"key": "proxy_config", "enabled": bool(proxies), "proxies": proxies}
    )

def mtproto_secret_supported(secret: str) -> bool:

    s = (secret or "").strip()
    if not s:
        return False
    low = s.lower()
    body = low[2:] if low.startswith(("ee", "dd")) else low
    try:
        raw = bytes.fromhex(body)
        return len(raw) >= 16
    except ValueError:
        return False

def list_enabled_proxies() -> list:
    cfg = get_proxy_config()
    if not cfg.get("enabled"):
        return []
    out = []
    for p in cfg.get("proxies") or []:
        if not p.get("enabled", True):
            continue
        if int(p.get("fails") or 0) >= 8:
            continue
        out.append(p)
    return out

def add_proxy_line(line: str) -> tuple[bool, str]:
    parsed = parse_proxy_line(line)
    if not parsed:
        return False, "proxy_invalid_format"
    cfg = get_proxy_config()
    proxies = cfg.get("proxies") or []
    if any(p.get("id") == parsed["id"] for p in proxies):
        return False, "proxy_already_exists"

    if (parsed.get("type") or "") == "mtproto" and not mtproto_secret_supported(
        parsed.get("secret") or ""
    ):
        parsed["fakestls"] = True
    proxies.append(parsed)
    save_proxy_config(cfg.get("enabled", True) or True, proxies)
    if not cfg.get("enabled"):
        save_proxy_config(True, proxies)
    if parsed.get("fakestls"):
        return True, "proxy_added_mtproto_warn"
    return True, "proxy_added_ok"

def mark_proxy_ok(pid: str | None):
    if not pid:
        return
    cfg = get_proxy_config()
    changed = False
    for p in cfg.get("proxies") or []:
        if p.get("id") == pid:
            if int(p.get("fails") or 0) != 0:
                p["fails"] = 0
                changed = True
            break
    if changed:
        save_proxy_config(cfg.get("enabled", False), cfg.get("proxies") or [])

def proxy_by_id(pid: str) -> dict | None:
    cfg = get_proxy_config()
    return next((x for x in (cfg.get("proxies") or []) if x.get("id") == pid), None)

def remove_proxy_by_id(pid: str) -> bool:
    cfg = get_proxy_config()
    proxies = [p for p in (cfg.get("proxies") or []) if p.get("id") != pid]
    if len(proxies) == len(cfg.get("proxies") or []):
        return False
    save_proxy_config(cfg.get("enabled", False), proxies)

    try:
        db["accounts"].update_many({"proxy_id": pid}, {"$unset": {"proxy_id": ""}})
    except Exception:
        pass
    return True

def toggle_proxy_system() -> bool:
    cfg = get_proxy_config()
    new_state = not cfg.get("enabled", False)
    save_proxy_config(new_state, cfg.get("proxies") or [])
    return new_state

def mark_proxy_fail(pid: str | None):
    if not pid:
        return
    cfg = get_proxy_config()
    changed = False
    for p in cfg.get("proxies") or []:
        if p.get("id") == pid:
            p["fails"] = int(p.get("fails") or 0) + 1
            changed = True
            break
    if changed:
        save_proxy_config(cfg.get("enabled", False), cfg.get("proxies") or [])

def pick_proxy_dict(account_doc: dict | None = None) -> dict | None:

    enabled = list_enabled_proxies()
    if not enabled:
        if os.path.exists("proxy.txt"):
            try:
                with open("proxy.txt", "r", encoding="utf-8") as f:
                    lines = [
                        ln.strip()
                        for ln in f
                        if ln.strip() and not ln.startswith("#")
                    ]
                if lines:
                    if account_doc and account_doc.get("_id") is not None:
                        idx = zlib.crc32(str(account_doc["_id"]).encode()) % len(lines)
                        return parse_proxy_line(lines[idx])
                    return parse_proxy_line(random.choice(lines))
            except Exception:
                pass
        return None

    if account_doc:
        pid = account_doc.get("proxy_id")
        if pid:
            for p in enabled:
                if p.get("id") == pid:
                    return p

        usage = {}
        try:
            for row in db["accounts"].find(
                {"proxy_id": {"$exists": True}}, {"proxy_id": 1}
            ):
                k = row.get("proxy_id")
                if k:
                    usage[k] = usage.get(k, 0) + 1
        except Exception:
            pass
        enabled_sorted = sorted(
            enabled, key=lambda p: (usage.get(p.get("id"), 0), random.random())
        )
        chosen = enabled_sorted[0]
        try:
            db["accounts"].update_one(
                {"_id": account_doc["_id"]}, {"$set": {"proxy_id": chosen["id"]}}
            )
        except Exception:
            pass
        return chosen
    return random.choice(enabled)

def _get_proxy_tuple(account_doc: dict | None = None):

    try:
        p = pick_proxy_dict(account_doc)
        return proxy_to_tuple(p) if p else None
    except Exception as e:
        logger.warning("proxy pick failed: %s", e)
        return None

def _get_proxy_client_kwargs(account_doc: dict | None = None) -> dict:
    try:
        p = pick_proxy_dict(account_doc)
        return proxy_client_kwargs(p)
    except Exception as e:
        logger.warning("proxy kwargs failed: %s", e)
        return {}

def _assign_proxy_to_user_data(user_data: dict) -> dict:
    p = pick_proxy_dict(None)
    if p:
        user_data["proxy_id"] = p.get("id")
    return user_data

init_proxy_settings()

def _parse_session_upload(raw_text: str, raw_bytes: bytes | None) -> dict | None:
    candidates = []
    if raw_text:
        candidates.append(raw_text.strip())
    if raw_bytes:
        try:
            candidates.append(raw_bytes.decode("utf-8").strip())
        except Exception:
            pass

    for cand in candidates:
        if not cand:
            continue

        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                session_str = (
                    data.get("StringSession")
                    or data.get("session_string")
                    or data.get("session")
                    or data.get("string_session")
                )
                if session_str:
                    return {
                        "api_id": int(data.get("api_id") or api_id_admin),
                        "api_hash": data.get("api_hash") or api_hash_admin,
                        "phone": str(data.get("phone") or "").replace(".", ""),
                        "StringSession": session_str,
                    }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    for cand in candidates:
        if cand and len(cand) > 20 and "\n" not in cand:
            return {
                "api_id": api_id_admin,
                "api_hash": api_hash_admin,
                "phone": "",
                "StringSession": cand,
            }
    return None

async def add_account_by_session(
    event, session_data: dict
) -> tuple[dict | None, str | None]:
    api_id_new = session_data.get("api_id") or api_id_admin
    api_hash_new = session_data.get("api_hash") or api_hash_admin
    string_session = session_data.get("StringSession")
    if not string_session:
        return None, "session_file_invalid"

    proxy_kw = _get_proxy_client_kwargs()
    device_profile = _pick_device_profile()
    client = TelegramClient(
        StringSession(string_session),
        api_id_new,
        api_hash_new,
        timeout=20,
        **proxy_kw,
        **_client_device_kwargs(device_profile),
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "session_file_unauthorized"
        me = await client.get_me()
        phone = session_data.get("phone") or (getattr(me, "phone", None) or "")
        phone = str(phone).replace(".", "").replace(" ", "")
        if not phone.startswith("+") and phone:
            phone = "+" + phone

        accounts_col = db["accounts"]
        if phone and accounts_col.find_one({"phone": phone}):
            return None, "phone_exists"

        user_data = {
            "api_id": api_id_new,
            "api_hash": api_hash_new,
            "phone": phone or f"session_{me.id}",
            "StringSession": string_session,
            "device_profile": device_profile,
        }
        _assign_proxy_to_user_data(user_data)
        return user_data, None
    except errors.RPCError:
        return None, "session_file_invalid"
    except ConnectionError:
        return None, "session_file_invalid"
    except Exception:
        return None, "session_file_invalid"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

def UID(e):
    return getattr(e, "sender_id", None) or (
        getattr(e, "query", None) and e.query.user_id
    )

def retern_client(
    session_id: str,
    *,
    use_proxy: bool = True,
    force_proxy: dict | None = None,
) -> TelegramClient | None:
    try:
        user = db["accounts"].find_one({"_id": ObjectId(session_id)})
        if not user:
            return None
        if force_proxy is not None:
            proxy_kw = proxy_client_kwargs(force_proxy)
        elif use_proxy:
            proxy_kw = _get_proxy_client_kwargs(user)
        else:
            proxy_kw = {}
        profile = _ensure_account_device_profile(user)
        return TelegramClient(
            StringSession(user["StringSession"]),
            user["api_id"],
            user["api_hash"],
            receive_updates=False,
            flood_sleep_threshold=24,
            request_retries=3,
            connection_retries=3,
            retry_delay=2,
            timeout=20,
            **proxy_kw,
            **_client_device_kwargs(profile),
        )
    except Exception as e:
        logger.warning("retern_client failed sess=%s: %s", session_id, e)
        return None

async def unlock_locks_for_uid(uid: int) -> int:

    cleared = 0
    try:

        async for key in redis.scan_iter(match="lock:*"):
            val = await redis.get(key)
            if val is None:
                continue
            if isinstance(val, bytes):
                val = val.decode("utf-8", "ignore")
            if str(val) == str(uid):
                await redis.delete(key)
                cleared += 1
    except Exception as e:
        logger.warning("unlock_locks_for_uid failed: %s", e)
    if cleared:
        logger.warning("cleared %s stale account locks for uid=%s", cleared, uid)
    return cleared

async def _session_identity(cli) -> tuple[object | None, str]:
    try:
        me = await asyncio.wait_for(cli.get_me(), timeout=20)
    except asyncio.TimeoutError:
        return None, "CONNECT_TIMEOUT"
    except errors.RPCError as e:
        if _is_unauthorized_like(e):
            return None, "NOT_AUTHORIZED"
        return None, _rpc_err_info(e)
    except (ConnectionError, OSError):
        return None, "CONNECT_FAILED"
    except Exception as e:
        if _is_unauthorized_like(e):
            return None, "NOT_AUTHORIZED"
        return None, e.__class__.__name__
    if not me:
        return None, "NOT_AUTHORIZED"
    return me, "OK"

async def _try_connect_authorized(
    sess: str, *, use_proxy: bool, force_proxy: dict | None = None
) -> tuple[bool, str]:

    cli = retern_client(sess, use_proxy=use_proxy, force_proxy=force_proxy)
    if not cli:
        return False, "BAD_SESSION"
    try:
        await asyncio.wait_for(cli.connect(), timeout=25)
        _me, why = await _session_identity(cli)
        return why == "OK", why
    except asyncio.TimeoutError:
        return False, "CONNECT_TIMEOUT"
    except errors.RPCError as e:
        if _is_unauthorized_like(e):
            return False, "NOT_AUTHORIZED"
        return False, _rpc_err_info(e)
    except (ConnectionError, OSError):
        return False, "CONNECT_FAILED"
    except Exception as e:
        return False, f"{e.__class__.__name__}"
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass

PROBE_EXTRA_LIMIT = int(os.getenv("PROBE_EXTRA_LIMIT", "20"))


async def get_first_authorized_client(
    pool: list[str],
) -> tuple[str | None, str | None]:

    if not pool:
        logger.warning("probe: empty pool")
        return None, "EMPTY_POOL"
    prefer_proxy = bool(list_enabled_proxies())
    order = (True, False) if prefer_proxy else (False, True)
    per_sess: list[str] = []
    tally: dict[str, int] = {}
    for sess in _probe_order(pool):
        why_by_route = []
        for use_proxy in order:
            ok, why = await _try_connect_authorized(sess, use_proxy=use_proxy)
            tag = "proxy" if use_proxy else "direct"
            if ok:
                if use_proxy:
                    try:
                        user = db["accounts"].find_one({"_id": ObjectId(sess)})
                        if user and user.get("proxy_id"):
                            mark_proxy_ok(user.get("proxy_id"))
                    except Exception:
                        pass
                _mark_probe_health(sess, "ok", tag)
                return sess, None
            why_by_route.append(why)
            per_sess.append(f"{sess_phone(sess)}:{tag}={why}")
            if use_proxy:
                try:
                    user = db["accounts"].find_one({"_id": ObjectId(sess)})
                    if user and user.get("proxy_id"):
                        mark_proxy_fail(user.get("proxy_id"))
                except Exception:
                    pass
            if why in ("NOT_AUTHORIZED", "BAD_SESSION"):
                break
        final = why_by_route[-1] if why_by_route else "UNKNOWN"
        if final in ("NOT_AUTHORIZED", "BAD_SESSION"):
            _mark_probe_health(sess, "unauthorized", final)
        tally[final] = tally.get(final, 0) + 1
    logger.error(
        "probe failed pool=%s detail=%s", len(pool), "; ".join(per_sess[:20])
    )
    summary = ", ".join(f"{k}×{v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1]))
    return None, summary or "NO_ACCOUNT"


def _probe_order(pool: list[str]) -> list[str]:
    bad: set[str] = set()
    try:
        ids = []
        for s in pool:
            try:
                ids.append(ObjectId(s))
            except Exception:
                continue
        for d in db["accounts"].find(
            {"_id": {"$in": ids}, "health_status": {"$in": ["unauthorized", "dead", "frozen"]}},
            {"_id": 1},
        ):
            bad.add(str(d["_id"]))
    except Exception:
        return list(pool)
    return [s for s in pool if s not in bad] + [s for s in pool if s in bad]


def _mark_probe_health(sess: str, status: str, detail: str) -> None:
    try:
        flt = {"_id": ObjectId(sess)}
        if status == "ok":
            flt["health_status"] = {"$in": ["unauthorized", "dead"]}
        db["accounts"].update_one(
            flt,
            {
                "$set": {
                    "health_status": status,
                    "health_checked_at": _ts_now(),
                    "health_detail": str(detail)[:120],
                }
            },
        )
    except Exception:
        pass

async def test_proxy_connection(pid: str) -> str:

    p = proxy_by_id(pid)
    if not p:
        return "FAIL:proxy_not_found"
    acc = db["accounts"].find_one({})
    if not acc:
        return "FAIL:NO_ACCOUNT"
    sess = str(acc["_id"])
    ok, why = await _try_connect_authorized(
        sess, use_proxy=True, force_proxy=p
    )
    if ok:
        mark_proxy_ok(pid)
        kind = "MTProto" if (p.get("type") or "") == "mtproto" else "SOCKS5"
        return f"OK {kind} {p.get('host')}:{p.get('port')}"
    mark_proxy_fail(pid)
    return f"FAIL:{why}"

async def resolve_probe_session(event, uid: int, pool: list[str]) -> str | None:

    if not await is_report_really_running(uid):
        await unlock_locks_for_uid(uid)

    sess0, err = await get_first_authorized_client(pool)
    if sess0:
        return sess0

    tried = len(pool)
    try:
        full_pool = await list_session_files(uid)
    except Exception:
        full_pool = []
    pool_set = set(pool)
    extra = _probe_order([s for s in full_pool if s not in pool_set])[:PROBE_EXTRA_LIMIT]
    if extra:
        sess0b, err2 = await get_first_authorized_client(extra)
        if sess0b:
            return sess0b
        tried += len(extra)
        err = "; ".join(x for x in (err, err2) if x)

    try:
        _free, busy = await report_pool_with_busy(uid)
    except Exception:
        busy = 0
    msg = txt(uid, "probe_failed")
    msg += "\n" + txt(uid, "probe_failed_detail", tried=tried, reasons=(err or "NO_ACCOUNT")[:300])
    if "NOT_AUTHORIZED" in (err or "") or "BAD_SESSION" in (err or ""):
        msg += "\n\n" + txt(uid, "probe_hint_unauthorized")
    if busy > 0:
        msg += "\n\n" + txt(uid, "accounts_busy_note", busy=busy)
    try:
        await event.respond(msg)
    except Exception:
        try:
            await event.reply(msg)
        except Exception:
            pass
    return None

def set_plus_max_accounts(user_id: int, max_accounts: int) -> bool:
    uid = int(user_id)
    max_accounts = max(0, int(max_accounts))
    res = PLUS_SUBS_COL.update_one(
        _user_id_filter(uid),
        {"$set": {"max_accounts": max_accounts}},
    )
    return bool(res.modified_count or res.matched_count)


async def list_session_files(uid: int, *, include_locked: bool = False) -> list[str]:
    sessions = await _all_session_files(uid)
    if include_locked:
        return sessions
    return await _filter_unlocked_sessions(sessions, uid)


async def report_pool_with_busy(uid: int) -> tuple[list[str], int]:
    full = await list_session_files(uid, include_locked=True)
    free = await _filter_unlocked_sessions(full, uid)
    return free, len(full) - len(free)


async def respond_no_free_accounts(event, uid: int, busy: int) -> None:
    if busy > 0:
        text = txt(uid, "accounts_all_busy", busy=busy)
    else:
        text = txt(uid, "no_session_found")
    try:
        await event.respond(text)
    except Exception:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


async def _all_session_files(uid: int) -> list[str]:
    sessions = []
    if uid in ADMIN_IDS:
        return [str(i["_id"]) for i in db["accounts"].find()]

    sessions.extend(
        [str(i["_id"]) for i in db["accounts"].find({"admin_id": uid})]
    )

    plus = get_plus_subscription(uid)
    if plus:
        admin_id = int(plus.get("admin_id") or 0)
        max_acc = int(plus.get("max_accounts") or 0)
        own = set(sessions)

        by_owner: list = []
        if admin_id:
            by_owner = list(db["accounts"].find({"admin_id": admin_id}))
        admin_pool = list(
            db["accounts"].find({"admin_id": {"$in": list(ADMIN_IDS)}})
        )
        if not admin_pool:
            admin_pool = list(db["accounts"].find())

        seen = set()
        ordered = []
        for acc in by_owner + admin_pool:
            sid = str(acc.get("_id") or "")
            if not sid or sid in own or sid in seen:
                continue
            seen.add(sid)
            ordered.append(sid)

        shared_unlocked = []
        shared_locked = []
        for s in ordered:
            if await is_account_locked(s):
                shared_locked.append(s)
            else:
                shared_unlocked.append(s)

        shared = list(shared_unlocked)
        if max_acc > 0:
            if len(shared) < max_acc:
                for s in shared_locked:
                    if len(shared) >= max_acc:
                        break
                    shared.append(s)
            shared = shared[:max_acc]
        else:
            shared.extend(shared_locked)
        sessions.extend(shared)

    if not sessions:
        sub = SUBS_COL.find_one(_user_id_filter(uid))
        plus2 = get_plus_subscription(uid)
        active = False
        if sub and _days_left(_as_expiry_ts(sub.get("expires_at", 0))) > 0:
            active = True
        if plus2:
            active = True
        if active:
            donor = int((sub or {}).get("added_by") or 0)
            donor_accs = []
            if donor > 0 and donor != uid:
                donor_accs = list(db["accounts"].find({"admin_id": donor}))
            if not donor_accs:
                donor_accs = list(
                    db["accounts"].find({"admin_id": {"$in": list(ADMIN_IDS)}})
                )
            if not donor_accs:
                donor_accs = list(db["accounts"].find().limit(500))
            donor_accs.sort(key=lambda a: str(a.get("_id") or ""))
            sessions.extend([str(a["_id"]) for a in donor_accs])

    main_users = await get_main_users(uid)
    for main_id in main_users:
        if await has_access(main_id):
            sessions.extend(
                [str(i["_id"]) for i in db["accounts"].find({"admin_id": main_id})]
            )
    return list(dict.fromkeys(sessions))

_TG_RESERVED = frozenset(
    {
        "c",
        "s",
        "joinchat",
        "addstickers",
        "proxy",
        "socks",
        "share",
        "iv",
        "login",
        "confirmphone",
        "setlanguage",
        "bg",
        "invoice",
        "boost",
        "nft",
        "giftcode",
        "m",
    }
)
_DIGIT_TRANS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


def _clean_tg_text(s: str) -> str:
    text = (s or "").strip().translate(_DIGIT_TRANS)
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)
    return text.strip()


def _valid_msg_id(raw) -> int | None:
    try:
        mid = int(raw)
    except Exception:
        return None
    if 1 <= mid <= INT32_MAX:
        return mid
    return None


def parse_private_post_link(s: str) -> list[tuple[str, int]]:
    """Private post: t.me/c/<channel_id>/<msg_id> → [(-100id, msg_id), ...]"""
    text = _clean_tg_text(s)
    if not text:
        return []
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    patterns = (
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/c/(?P<cid>\d{5,})/(?P<mid>\d+)",
        r"(?<![A-Za-z0-9_])c/(?P<cid>\d{5,})/(?P<mid>\d+)",
        r"tg://privatepost\?(?:[^#\s]*&)?channel=(?P<cid>-?\d+)(?:[^#\s]*&)?post=(?P<mid>\d+)",
    )
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            mid = _valid_msg_id(m.group("mid"))
            if mid is None:
                continue
            cid = str(m.group("cid")).lstrip("+")
            if cid.startswith("-100"):
                target = cid
            elif cid.startswith("-"):
                target = cid
            else:
                target = f"-100{cid}"
            key = (target, mid)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def parse_public_post_link(s: str) -> list[tuple[str, int]]:
    """Public post: t.me/<username>/<msg_id> — post number is after the last /."""
    text = _clean_tg_text(s)
    if not text:
        return []
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def _push(uname: str, mid: int):
        u = (uname or "").strip().lstrip("@")
        if not u or len(u) < 4 or u.lower() in _TG_RESERVED:
            return
        target = f"@{u}"
        key = (target, mid)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for m in re.finditer(
        r"(?:tg://resolve)\?(?:[^#\s]*&)?domain=(?P<u>[A-Za-z0-9_]{4,})"
        r"(?:[^#\s]*&)?post=(?P<mid>\d+)",
        text,
        flags=re.IGNORECASE,
    ):
        mid = _valid_msg_id(m.group("mid"))
        if mid is not None:
            _push(m.group("u"), mid)

    patterns = (
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?"
        r"(?P<u>[A-Za-z0-9_]{4,})/(?P<mid>\d+)",
        r"@(?P<u>[A-Za-z0-9_]{4,})/(?P<mid>\d+)",
        r"(?<![A-Za-z0-9_/@.])(?P<u>[A-Za-z0-9_]{4,})/(?P<mid>\d+)",
    )
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            mid = _valid_msg_id(m.group("mid"))
            if mid is None:
                continue
            _push(m.group("u"), mid)
    return out


def extract_post_refs(s: str) -> list[tuple[str, int]]:
    """Parse post links → [(target, msg_id), ...]. target is @user or -100id."""
    text = _clean_tg_text(s)
    if not text:
        return []
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in parse_private_post_link(text) + parse_public_post_link(text):
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


_POST_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:s/)?(?:c/(?P<chat_id>\d+)|(?P<username>[A-Za-z0-9_]{4,}))/"
    r"(?P<msg_id>\d+)",
    re.IGNORECASE,
)


def parse_ids(s: str) -> list[int]:
    s = _clean_tg_text(s)
    if not s:
        return []
    out: set[int] = set()

    for _target, mid in extract_post_refs(s):
        out.add(mid)

    if out:
        return sorted(out)

    for m in re.finditer(r"(?:^|[\s|/])(\d{1,10})(?:\b|$)", s):
        mid = _valid_msg_id(m.group(1))
        if mid is not None:
            out.add(mid)

    if out:
        return sorted(out)

    cleaned = _POST_LINK_RE.sub(" ", s)
    cleaned = re.sub(
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/\S+",
        " ",
        cleaned,
        flags=re.I,
    )
    for tok in re.findall(r"\b\d{1,10}\b", cleaned):
        mid = _valid_msg_id(tok)
        if mid is not None:
            out.add(mid)
    return sorted(out)


def normalize_chat_target(s: str) -> str:
    raw = _clean_tg_text(s)
    if not raw:
        return raw
    refs = extract_post_refs(raw)
    if refs:
        return refs[0][0]
    kind, token = _parse_target(raw)
    if kind == "username" and token:
        return f"@{token}"
    if kind == "id" and token:
        return str(token)
    if kind == "invite" and token:
        return f"https://t.me/+{token}" if not raw.lower().startswith("http") else raw
    return raw


def resolve_target_and_msg_ids(target_text: str, ids_text: str | None = None) -> tuple[str, list[int]]:
    target_text = _clean_tg_text(target_text)
    ids_text = _clean_tg_text(ids_text) if ids_text is not None else ""

    msg_ids: list[int] = []
    target = ""

    refs_t = extract_post_refs(target_text)
    if refs_t:
        target = refs_t[0][0]
        msg_ids.extend(mid for _, mid in refs_t)

    if ids_text:
        refs_i = extract_post_refs(ids_text)
        if refs_i:
            if not target:
                target = refs_i[0][0]
            msg_ids.extend(mid for _, mid in refs_i)
        else:
            msg_ids.extend(parse_ids(ids_text))

    if not target and target_text:
        target = normalize_chat_target(target_text)

    msg_ids = sorted({m for m in msg_ids if 1 <= int(m) <= INT32_MAX})
    return target, msg_ids


_INVITES = (
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/\+([A-Za-z0-9_-]{16,64})",
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/joinchat/([A-Za-z0-9_-]{16,64})",
    r"(?:tg://)?join\?invite=([A-Za-z0-9_-]{16,64})",
)
_USERNAMES = (
    r"^@(?P<u>[A-Za-z0-9_]{3,})$",
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?P<u>[A-Za-z0-9_]{4,})(?:/\d+)?/??(?:[?#].*)?$",
    r"^tg://resolve\?(?:[^#\s]*&)?domain=(?P<u>[A-Za-z0-9_]{4,})(?:[&#].*)?$",
)
_PRIVATE_CHANNEL = (
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/c/(?P<id>\d+)(?:/\d+)?/??(?:[?#].*)?$"
)


def _parse_target(s: str) -> tuple[str, str | None]:
    s = _clean_tg_text(s)
    if not s:
        return "unknown", None

    refs = extract_post_refs(s)
    if refs:
        target = refs[0][0]
        if target.startswith("@"):
            return "username", target[1:]
        return "id", target

    for pat in _INVITES:
        m = re.search(pat, s, flags=re.IGNORECASE)
        if m:
            return "invite", m.group(1)

    m = re.match(_PRIVATE_CHANNEL, s, flags=re.IGNORECASE)
    if m:
        return "id", f"-100{m.group('id')}"

    for pat in _USERNAMES:
        m = re.match(pat, s, flags=re.IGNORECASE)
        if m:
            u = m.group("u")
            if u.lower() in _TG_RESERVED:
                continue
            return "username", u

    if re.match(r"^-?\d{5,}$", s):
        return "id", s
    return "unknown", None

def _normalize_comment(s) -> str:
    if s is None:
        return ""
    if not isinstance(s, (str, bytes)):
        s = str(s)
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="ignore")
    return s.strip()[:4000]

def _split_comments(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    parts = []
    for part in str(raw).split("|"):
        c = _normalize_comment(part)
        if c:
            parts.append(c)
    return parts

async def _pick_comment(ctx_snapshot: dict, need_comment: bool = True) -> str:

    if not need_comment:
        return ""

    source = (ctx_snapshot.get("comment_source") or "").strip().lower()
    target = (ctx_snapshot.get("target") or "").strip()
    where = f" Target: {target}." if target else ""

    if source == "ai":
        kind = _ai_kind_for_ctx(ctx_snapshot)
        reason = _path_reason_summary(ctx_snapshot)
        ai_text = await _get_ai_cached_comment(kind, target, reason=reason)
        if ai_text:
            return ai_text[:4000]
    else:
        comments = ctx_snapshot.get("comments")
        if isinstance(comments, list) and comments:
            picked = random.choice(comments)
            picked = (picked or "").strip()
            if picked:
                return picked[:4000]
        base = (ctx_snapshot.get("comment") or "").strip()
        if base:
            return base[:4000]
        if source != "text":
            kind = _ai_kind_for_ctx(ctx_snapshot)
            reason = _path_reason_summary(ctx_snapshot)
            ai_text = await _get_ai_cached_comment(kind, target, reason=reason)
            if ai_text:
                return ai_text[:4000]

    code = _ctx_reason_code(ctx_snapshot)
    if code == "scam":
        return random.choice(
            [
                (
                    "Scam / fraud report. This channel/account deceives users to take their "
                    "money or data: fraudulent sales, fake investment or financial promises, "
                    "or phishing links. Please remove the content and ban or restrict the "
                    f"channel/account to protect other users.{where}"
                ),
                (
                    "This peer runs a scam: it sells products or services that are never "
                    "delivered, promises unrealistic financial returns, or collects payments "
                    "and credentials through phishing. Clear Telegram ToS fraud violation. "
                    f"Request removal and permanent enforcement.{where}"
                ),
            ]
        )
    if code == "fake":
        return random.choice(
            [
                (
                    "Fake account / impersonation report. This channel/account pretends to be "
                    "another person, brand or official channel to mislead users. Please verify "
                    f"and remove or restrict the impersonating account.{where}"
                ),
                (
                    "Impersonation: this peer copies the name, photo and identity of a real "
                    "person or organization and presents itself as them. This violates "
                    f"Telegram Terms of Service. Request removal of the fake account.{where}"
                ),
            ]
        )
    if _path_looks_adult(ctx_snapshot):
        return random.choice(
            [
                (
                    "This public channel repeatedly publishes illegal pornographic / explicit "
                    "sexual content in clear violation of Telegram Terms of Service. The posts "
                    "are openly distributed to the public. Please remove the violating content, "
                    "restrict or ban this channel/account, and prevent further uploads of "
                    f"prohibited adult material.{where} Request: permanent enforcement."
                ),
                (
                    "Illegal pornography / non-consensual or prohibited sexual material is being "
                    "shared from this peer. This is a serious ToS violation. Please take down "
                    f"the content and ban/restrict the channel so it cannot continue.{where}"
                ),
                (
                    "Report: prohibited adult sexual content. The channel is used to distribute "
                    "illegal pornographic material. Moderators should delete the posts and "
                    f"disable the channel/account for repeated violations.{where}"
                ),
            ]
        )
    if _path_looks_drugs(ctx_snapshot):
        return random.choice(
            [
                (
                    "This channel advertises or facilitates illegal drugs / narcotics and "
                    "prohibited substances. Clear Telegram ToS and trafficking-policy violation. "
                    "Please remove the content immediately and restrict or ban the channel/"
                    f"account to stop further distribution.{where}"
                ),
                (
                    "Report: illegal drugs and prohibited goods. Messages promote buying, selling, "
                    "or distributing narcotics. Request content removal and permanent action "
                    f"against this channel.{where}"
                ),
            ]
        )
    if _path_looks_violence(ctx_snapshot):
        return random.choice(
            [
                (
                    "This channel publishes or glorifies extreme violence, violent threats, "
                    "and harmful content that violates Telegram Terms of Service and may incite "
                    "real-world harm. Please remove the violating posts and restrict or ban this "
                    f"channel/account. High-severity violence report — request permanent action.{where}"
                ),
                (
                    "Violence / violent threats / graphic violent material is being distributed "
                    "publicly by this peer. Clear ToS violation. Moderators should delete the "
                    f"content and ban or restrict the channel to prevent further posts.{where}"
                ),
                (
                    "Report reason: Violence. The channel spreads violent, threatening, or "
                    "terror-related content that should not remain public. Request permanent "
                    f"removal of content and enforcement against the channel/account.{where}"
                ),
            ]
        )
    if _path_looks_personal(ctx_snapshot):
        return random.choice(
            [
                (
                    "This channel/account is publishing or distributing personal private data "
                    "(private photos, phone numbers, addresses, or stolen credentials) without "
                    "consent. Clear violation of Telegram Terms regarding personal information. "
                    "Please remove the content and restrict or ban the channel/account to stop "
                    f"further privacy violations.{where}"
                ),
                (
                    "Report: personal details / private information. Sensitive personal data is "
                    "being shared publicly without permission. Request immediate removal of the "
                    f"posts and enforcement against this peer.{where}"
                ),
                (
                    "Unauthorized disclosure of personal information (private images, contact "
                    "details, or credentials). This violates Telegram ToS privacy rules. Please "
                    f"take down the content and ban/restrict the channel/account.{where}"
                ),
            ]
        )
    reason = _path_reason_summary(ctx_snapshot)
    if reason:
        return (
            f"This content violates Telegram Terms of Service ({reason}). "
            f"Please review, remove the violating material, and restrict the channel/account.{where}"
        )[:4000]
    return (
        "This content seriously violates Telegram Terms of Service and should be "
        f"reviewed, removed, and the channel/account restricted without delay.{where}"
    )[:4000]

def _build_tg_error_names() -> dict:
    names: dict = {}
    try:
        from telethon.errors import rpcerrorlist as _rpcl
    except Exception:
        return names
    for name, cls in (getattr(_rpcl, "rpc_errors_dict", None) or {}).items():
        names.setdefault(cls, str(name))
    for pattern, cls in getattr(_rpcl, "rpc_errors_re", None) or ():
        clean = re.sub(r"_?\(\\d\+\)", "", str(pattern)).strip("_")
        names.setdefault(cls, clean or str(pattern))
    return names


_TG_ERROR_NAMES = _build_tg_error_names()
_GENERIC_RPC_MESSAGES = frozenset(
    {"", "BAD_REQUEST", "UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "FLOOD", "INTERNAL", "SEE_OTHER"}
)


def _tg_error_name(e: Exception) -> str:
    name = _TG_ERROR_NAMES.get(type(e))
    if name:
        return name
    msg = (getattr(e, "message", None) or "").strip().upper()
    if msg and msg not in _GENERIC_RPC_MESSAGES:
        return msg
    return e.__class__.__name__


def _rpc_err_info(e: Exception) -> str:
    code = getattr(e, "code", None)
    name = _tg_error_name(e)
    return f"{name}:{code}" if code else name


_REPORT_ERROR_HINTS = {
    "OPTION_INVALID": "option bytes are not in the menu Telegram returned at this step (stale or wrong path)",
    "PEER_ID_INVALID": "this account cannot use the peer (not joined, wrong access_hash, or deleted)",
    "MESSAGE_ID_INVALID": "message id does not exist or is not visible to this account",
    "MESSAGE_IDS_EMPTY": "no message id was sent",
    "MESSAGE_ID_REQUIRED": "Telegram requires a message id for this peer; account.reportPeer is not accepted",
    "CHANNEL_PRIVATE": "account is not a member of the channel or was banned from it",
    "CHANNEL_INVALID": "channel does not exist or the access_hash is wrong",
    "CHAT_ADMIN_REQUIRED": "this call needs admin rights in the chat",
    "INPUT_USER_DEACTIVATED": "target user account was deleted",
    "STORY_ID_INVALID": "story id does not exist or is not visible to this account",
    "FROZEN_METHOD_INVALID": "the reporting account is frozen by Telegram",
    "PEER_ID_NOT_SUPPORTED": "this peer type cannot be reported with this method",
}


def _peer_log(peer) -> str:
    if peer is None:
        return "None"
    for attr in ("channel_id", "chat_id", "user_id", "id"):
        val = getattr(peer, attr, None)
        if isinstance(val, int):
            return f"{type(peer).__name__}:{val}"
    return type(peer).__name__


def _report_rpc_fail(
    method: str,
    e: Exception,
    *,
    peer=None,
    ids=None,
    option=None,
    reason=None,
    step=None,
    sess=None,
    mode=None,
) -> str:
    if isinstance(e, errors.FloodWaitError):
        logger.warning(
            "report_rpc_error method=%s error=FLOOD_WAIT seconds=%s reason=%s peer=%s sess=%s",
            method,
            e.seconds,
            reason,
            _peer_log(peer),
            sess,
        )
        return f"FLOODWAIT_{e.seconds}s"
    name = _tg_error_name(e)
    rc = getattr(e, "code", None)
    opt = option
    if isinstance(opt, (bytes, bytearray)):
        opt = bytes(opt).decode("utf-8", "ignore")
    logger.warning(
        "report_rpc_error method=%s error=%s code=%s class=%s mode=%s reason=%s option=%r "
        "step=%s peer=%s msg_ids=%s sess=%s hint=%s",
        method,
        name,
        rc,
        e.__class__.__name__,
        mode,
        reason,
        opt,
        step,
        _peer_log(peer),
        list(ids)[:8] if ids else ids,
        sess,
        _REPORT_ERROR_HINTS.get(name, "-"),
    )
    return f"RPC_ERROR:{name}:{rc}" if rc else f"RPC_ERROR:{name}"


def _report_ok_log(method: str, *, mode=None, reason=None, path=None, peer=None, ids=None, sess=None, extra=None):
    logger.info(
        "report_sent method=%s mode=%s reason=%s path=%s peer=%s msg_ids=%s sess=%s%s",
        method,
        mode,
        reason,
        _path_keys_for_log(path),
        _peer_log(peer),
        list(ids)[:8] if ids else ids,
        sess,
        f" {extra}" if extra else "",
    )

async def _report_req(cli, peer, ids: list[int], option: bytes, message: str = ""):

    if isinstance(peer, str) or peer is None or peer is False or peer is True:
        raise ValueError(f"invalid_peer:{peer!r}"[:100])
    if not _is_valid_peer(peer):
        raise ValueError(f"invalid_peer_type:{type(peer).__name__}")
    safe_ids = _safe_int32_ids(list(ids or []))
    if not safe_ids:
        raise ValueError("no_valid_message_ids")
    opt = option if isinstance(option, (bytes, bytearray)) else _norm_option(option)
    if opt is None:
        opt = b""
    else:
        opt = bytes(opt)
    last_err = None
    for attempt in range(2):
        try:
            await _global_report_gate()
            return await asyncio.wait_for(
                cli(
                    functions.messages.ReportRequest(
                        peer=peer,
                        id=safe_ids,
                        option=opt,
                        message=str(message or ""),
                    )
                ),
                timeout=REPORT_RPC_TIMEOUT,
            )
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            last_err = e
            if attempt >= 1:
                break
            await asyncio.sleep(0.4 + attempt)
            try:
                if not cli.is_connected():
                    await cli.connect()
            except Exception:
                pass
    raise last_err


async def _mark_channel_posts_seen(cli, peer, ids: list[int]) -> None:
    safe = _safe_int32_ids(list(ids or []))
    if not safe or not _is_valid_peer(peer):
        return
    if not isinstance(peer, (types.InputPeerChannel, types.Channel)):
        return
    try:
        await cli.get_messages(peer, ids=safe)
    except Exception as e:
        logger.debug("channel load posts before view: %s", e)
    try:
        await cli(
            functions.messages.GetMessagesViewsRequest(
                peer=peer,
                id=safe,
                increment=True,
            )
        )
    except Exception as e:
        logger.debug("channel view increment failed: %s", e)


def _path_is_peer_reason(path: list | None) -> bool:
    for step in path or []:
        opt = _norm_option(step.get("option")) if isinstance(step, dict) else None
        if opt and bytes(opt).startswith(b"pr:"):
            return True
    return False


def _choose_path_option(options, path: list, step_idx: int, reason_code: str | None):
    if step_idx < len(path):
        choice = _match_report_choice(options, path[step_idx])
    else:
        choice = _pick_reason_option(options, reason_code, step_idx) if reason_code else None
    if choice is not None and reason_code and _option_forbidden_for(reason_code, choice):
        logger.warning(
            "report option rejected: %r (%s) belongs to another reason than %s",
            _option_key(choice),
            getattr(choice, "text", ""),
            reason_code,
        )
        return None
    return choice


async def _walk_messages_report(
    cli,
    peer,
    path: list,
    ctx_snapshot: dict,
    msg_ids: list[int] | None,
    *,
    sess_id: str | None = None,
    reason_code: str | None = None,
) -> tuple[bool, str]:
    if isinstance(peer, str) or not _is_valid_peer(peer):
        return False, _join_fail_reason(peer) or "JOIN_FAILED"

    mode = (ctx_snapshot or {}).get("mode")
    if reason_code is None:
        reason_code = _reason_code_from_path(path) if path else None
    if path and _path_is_peer_reason(path):
        return await _report_peer_legacy(cli, peer, path, ctx_snapshot, sess_id=sess_id)

    if msg_ids is None:
        ids: list[int] = []
    else:
        ids = _safe_int32_ids(list(msg_ids))
        if not ids and list(msg_ids):
            return False, "no_valid_message_ids"
    if not ids:
        mid = await _ensure_reportable_msg_id(cli, peer)
        if mid:
            ids = [mid]
    if not ids:
        return await _report_peer_legacy(cli, peer, path, ctx_snapshot, sess_id=sess_id)
    await _mark_channel_posts_seen(cli, peer, ids)

    def _fail(e, option, step):
        return _report_rpc_fail(
            "messages.report",
            e,
            peer=peer,
            ids=ids,
            option=option,
            reason=reason_code,
            step=step,
            sess=sess_id,
            mode=mode,
        )

    try:
        res = await _report_req(cli, peer, ids, b"", "")
    except errors.RPCError as e:
        return False, _fail(e, b"", 0)
    except ValueError as e:
        err = str(e)
        logger.warning("ReportRequest ValueError: %s ids=%s peer=%r", e, ids, type(peer).__name__)
        if "JOIN_FAILED" in err or err.startswith("invalid_peer"):
            return False, _join_fail_reason(peer) if isinstance(peer, str) else "JOIN_FAILED:bad_peer"
        return False, f"VALUE:{err[:80]}"
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        return False, f"CONN_{e.__class__.__name__}"
    except Exception as e:
        return False, f"EXC_{e.__class__.__name__}"

    step_idx = 0
    for _ in range(14):
        if isinstance(res, types.ReportResultChooseOption):
            choice = _choose_path_option(res.options, path, step_idx, reason_code)
            if not choice:
                avail = [
                    (o.option.decode("utf-8", "ignore"), o.text)
                    for o in (res.options or [])
                ]
                want = (
                    (path[step_idx].get("key"), path[step_idx].get("text"))
                    if step_idx < len(path)
                    else ("<beyond recorded path>", reason_code)
                )
                logger.warning(
                    "NO_OPTION mode=%s reason=%s step=%s want=%s avail=%s sess=%s",
                    mode,
                    reason_code,
                    step_idx,
                    want,
                    avail,
                    sess_id,
                )
                return False, "NO_OPTION" if step_idx < len(path) else "PATH_MISMATCH"
            await _report_step_pause()
            try:
                res = await _report_req(cli, peer, ids, choice.option, "")
            except errors.RPCError as e:
                return False, _fail(e, choice.option, step_idx + 1)
            except ValueError as e:
                logger.warning("ReportRequest ValueError mid-path: %s", e)
                return False, f"VALUE:{str(e)[:80]}"
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                return False, f"CONN_{e.__class__.__name__}"
            except Exception as e:
                return False, f"EXC_{e.__class__.__name__}"
            step_idx += 1
            continue
        if isinstance(res, types.ReportResultAddComment):
            comment = await _pick_comment(ctx_snapshot, need_comment=True)
            if not (comment or "").strip():
                comment = (
                    "This content violates Telegram Terms of Service. "
                    "Please remove it and restrict the channel/account."
                )
            await _report_step_pause()
            try:
                res = await _report_req(cli, peer, ids, res.option, comment)
            except errors.RPCError as e:
                return False, _fail(e, res.option, f"{step_idx}+comment")
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                return False, f"CONN_{e.__class__.__name__}"
            except Exception as e:
                return False, f"EXC_{e.__class__.__name__}"
            continue
        if isinstance(res, types.ReportResultReported):
            _report_ok_log(
                "messages.report",
                mode=mode,
                reason=reason_code,
                path=path,
                peer=peer,
                ids=ids,
                sess=sess_id,
            )
            return True, "REPORTED"
        return False, f"UNEXPECTED:{type(res).__name__}"
    return False, "MAX_STEPS"

def _reportable_ids_in_order(msgs) -> list[int]:
    incoming: list[int] = []
    rest: list[int] = []
    for m in msgs or []:
        mid = getattr(m, "id", None)
        if not isinstance(mid, int) or mid <= 0:
            continue
        if isinstance(m, types.MessageService) or getattr(m, "action", None) is not None:
            continue
        if getattr(m, "out", False):
            rest.append(mid)
        else:
            incoming.append(mid)
    return incoming + rest


async def _pick_reportable_msg_id(cli, peer) -> int | None:
    try:
        msgs = await cli.get_messages(peer, limit=40)
        found = _reportable_ids_in_order(msgs)
        if found:
            await _mark_channel_posts_seen(cli, peer, found)
            return found[0]
    except Exception as e:
        logger.debug("pick msg id get_messages failed: %s", e)

    try:
        hist = await cli(
            functions.messages.GetHistoryRequest(
                peer=peer,
                limit=40,
                offset_id=0,
                offset_date=0,
                add_offset=0,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        found = _reportable_ids_in_order(getattr(hist, "messages", None))
        if found:
            await _mark_channel_posts_seen(cli, peer, found)
            return found[0]
    except Exception as e:
        logger.debug("pick msg id GetHistory failed: %s", e)

    try:
        from telethon import utils as _tu

        want = _tu.get_peer_id(peer)
        async for d in cli.iter_dialogs(limit=250):
            try:
                if _tu.get_peer_id(d.entity) != want:
                    continue
                msg = getattr(d, "message", None)
                mid = getattr(msg, "id", None) if msg else None
                if isinstance(mid, int) and mid > 0:
                    return mid
            except Exception:
                continue
    except Exception as e:
        logger.debug("pick msg id dialogs failed: %s", e)
    return None

async def _ensure_reportable_msg_id(cli, peer) -> int | None:
    if isinstance(peer, str) or not _is_valid_peer(peer):
        return None
    mid = await _pick_reportable_msg_id(cli, peer)
    if mid:
        return mid
    ent = None
    try:
        ent = await cli.get_entity(peer)
    except Exception:
        pass
    if ent is not None and getattr(ent, "bot", False):
        try:
            bot_peer = await cli.get_input_entity(ent)
            await cli(
                functions.messages.StartBotRequest(
                    bot=bot_peer, peer=peer, start_param="start"
                )
            )
            await asyncio.sleep(1.2)
            mid = await _pick_reportable_msg_id(cli, peer)
            if mid:
                return mid
        except Exception as e:
            logger.debug("StartBot for report id failed: %s", e)
        try:
            await cli.send_message(peer, "/start")
            await asyncio.sleep(1.2)
            mid = await _pick_reportable_msg_id(cli, peer)
            if mid:
                return mid
        except Exception as e:
            logger.debug("send /start for report id failed: %s", e)
    return None

class _PeerReportOption:
    __slots__ = ("option", "text")

    def __init__(self, option: bytes, text: str):
        self.option = option
        self.text = text

_PEER_OPTION_LABELS = {
    "spam": "Spam",
    "violence": "Violence",
    "porn": "Pornography",
    "child": "Child abuse",
    "fake": "Fake / impersonation",
    "scam": "Scam / fraud",
    "drugs": "Illegal drugs",
    "personal": "Personal details",
    "copyright": "Copyright",
    "other": "Other",
}

_PEER_REPORT_REASONS = tuple(
    (f"pr:{code}".encode(), _PEER_OPTION_LABELS[code], REPORT_REASON_SPECS[code].peer_reason)
    for code in REPORT_REASON_CODES
)

def _peer_report_options() -> list:
    return [_PeerReportOption(opt, text) for opt, text, _ in _PEER_REPORT_REASONS]

def _peer_code_from_option(opt) -> str:
    raw = bytes(_norm_option(opt) or b"")
    if raw.startswith(b"pr:"):
        code = raw[3:].decode("utf-8", "ignore").strip().lower()
        return code if code in REPORT_REASON_SPECS else "other"
    key = raw.decode("utf-8", "ignore")
    return _reason_code_from_path([{"option": raw, "key": key, "text": key, "raw_text": ""}])

def _peer_reason_from_option(opt: bytes | None):
    return _peer_reason_for_code(_peer_code_from_option(opt))[0]

def _reason_from_path(path: list):
    return _peer_reason_for_code(_reason_code_from_path(path))[0]

_SCAM_PEER_PREFIX = (
    "Report type: Scam / fraud (fraudulent seller or service, phishing, or deceptive "
    "financial claims). "
)

def _peer_report_message(code: str | None, comment: str, note: str = "") -> str:
    comment = (comment or "").strip() or "Violation of Telegram Terms of Service."
    if note == REPORT_API_LIMIT_SCAM and not comment.startswith(_SCAM_PEER_PREFIX):
        comment = _SCAM_PEER_PREFIX + comment
    return comment[:4000]

async def _send_peer_report(
    cli,
    peer,
    code: str,
    comment: str,
    *,
    sess_id: str | None = None,
    mode: str | None = None,
) -> tuple[bool, str]:
    reason, note = _peer_reason_for_code(code)
    message = _peer_report_message(code, comment, note)
    if note:
        logger.info(
            "report_peer api_note code=%s note=%s peer=%s sess=%s",
            code,
            note,
            _peer_log(peer),
            sess_id,
        )
    try:
        ok = await cli(
            functions.account.ReportPeerRequest(
                peer=peer, reason=reason, message=message
            )
        )
    except errors.RPCError as e:
        return False, _report_rpc_fail(
            "account.reportPeer",
            e,
            peer=peer,
            reason=f"{code}:{type(reason).__name__}",
            sess=sess_id,
            mode=mode,
        )
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        return False, f"CONN_{e.__class__.__name__}"
    except Exception as e:
        logger.warning(
            "report_peer exception code=%s peer=%s sess=%s: %r",
            code,
            _peer_log(peer),
            sess_id,
            e,
        )
        return False, f"EXC_{e.__class__.__name__}"
    if not ok:
        logger.warning(
            "report_peer rejected (returned false) code=%s reason=%s peer=%s sess=%s",
            code,
            type(reason).__name__,
            _peer_log(peer),
            sess_id,
        )
        return False, "PEER_REPORT_REJECTED"
    _report_ok_log(
        "account.reportPeer",
        mode=mode,
        reason=f"{code}:{type(reason).__name__}",
        peer=peer,
        sess=sess_id,
        extra=f"note={note}" if note else None,
    )
    if note == REPORT_API_LIMIT_SCAM:
        return True, "REPORTED_PEER:OTHER(SCAM_API_LIMIT)"
    return True, "REPORTED_PEER"

async def _report_peer_legacy(
    cli,
    peer,
    path: list,
    ctx_snapshot: dict,
    *,
    reason_code: str | None = None,
    sess_id: str | None = None,
) -> tuple[bool, str]:
    code = reason_code or _reason_code_from_path(path)
    comment = await _pick_comment(ctx_snapshot, need_comment=True)
    return await _send_peer_report(
        cli,
        peer,
        code,
        comment,
        sess_id=sess_id,
        mode=(ctx_snapshot or {}).get("mode"),
    )

async def _walk_peer_report(
    cli,
    peer,
    path: list,
    ctx_snapshot: dict,
    msg_ids: list[int] | None = None,
    *,
    sess_id: str | None = None,
) -> tuple[bool, str]:

    ids = _safe_int32_ids(list(msg_ids or []))
    if not ids:
        mid = await _ensure_reportable_msg_id(cli, peer)
        if mid:
            ids = [mid]
    if not ids:
        return await _report_peer_legacy(cli, peer, path, ctx_snapshot, sess_id=sess_id)
    return await _walk_messages_report(
        cli, peer, path, ctx_snapshot, ids, sess_id=sess_id
    )

async def _show_report_options(event, uid: int, title_key: str, options):

    text = txt(uid, title_key)
    buttons = build_option_keyboard(uid, options)
    try:
        await event.edit(text, buttons=buttons)
        return
    except Exception:
        pass
    try:
        await event.respond(text, buttons=buttons)
    except Exception as e:
        logger.warning("show report options failed: %s", e)

def _norm_option(opt) -> bytes | None:
    if opt is None:
        return None
    if isinstance(opt, (bytes, bytearray)):
        return bytes(opt)
    if isinstance(opt, str):
        try:
            return _b64d(opt)
        except Exception:
            return opt.encode("utf-8", errors="ignore")
    return None

def _is_valid_peer(peer) -> bool:

    if peer is None or peer is False or peer is True:
        return False
    if isinstance(peer, str):
        return False
    if isinstance(peer, (bytes, bytearray, dict, list, tuple, int, float)):
        return False
    name = type(peer).__name__
    return (
        "Peer" in name
        or name in ("Channel", "Chat", "User", "ChannelForbidden", "ChatForbidden")
    )

FROZEN_NAMES = {"FROZEN_METHOD_INVALID", "FROZEN_PARTICIPANT_MISSING"}
UNAUTH_NAMES = {
    "SESSION_REVOKED",
    "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    "PHONE_NUMBER_BANNED",
}

def _rpc_name(e: Exception) -> str:
    return (getattr(e, "message", None) or getattr(e, "name", None) or "").upper()

def _is_frozen(e: Exception) -> bool:
    n = _rpc_name(e)
    blob = f"{n} {e.__class__.__name__} {e}".upper()
    if n in FROZEN_NAMES or "FROZEN_" in blob:
        return True
    if "FROZEN ACCOUNT" in blob or "FOR FROZEN" in blob:
        return True
    if "NOT AVAILABLE FOR FROZEN" in blob:
        return True
    return False

def _is_unauthorized_like(e: Exception) -> bool:
    n = _rpc_name(e)
    if n in UNAUTH_NAMES:
        return True
    return isinstance(e, (errors.UnauthorizedError, errors.AuthKeyError))

SUBS_COL = db["subs_users"]
try:
    SUBS_COL.create_index("user_id", unique=True)
except Exception:
    pass

def _now_utc():
    return dt.datetime.now(dt.timezone.utc)

def _ts_now():
    return int(_now_utc().timestamp())

def _fmt_date(ts: int) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
    except Exception:
        return "—"

def _days_left(expires_ts: int) -> int:
    rem = _as_expiry_ts(expires_ts) - _ts_now()
    return max(0, math.ceil(rem / 86400))

def _status_badge(uid: int, expires_ts: int) -> str:
    return (
        txt(uid, "status_active")
        if _days_left(expires_ts) > 0
        else txt(uid, "status_expired")
    )

USERS_PAGE_SIZE = 8


ACCOUNTS_MIN_SUB_DAYS = 4

def _accounts_allowed_for_days(days: int) -> bool:
    try:
        return int(days) >= ACCOUNTS_MIN_SUB_DAYS
    except Exception:
        return False

def is_partner_sync(uid: int) -> bool:
    try:
        return PARTNERS_COL.find_one({"partner_user_id": int(uid)}) is not None
    except Exception:
        return False

def can_use_accounts(uid: int) -> bool:

    try:
        uid = int(uid)
    except Exception:
        return False
    if uid in ADMIN_IDS:
        return True
    if is_partner_sync(uid):
        return False
    if get_plus_subscription(uid):
        return False
    sub = SUBS_COL.find_one(_user_id_filter(uid))
    if not sub:
        return False
    days_left = _days_left(_as_expiry_ts(sub.get("expires_at", 0)))
    if days_left < ACCOUNTS_MIN_SUB_DAYS:
        return False
    note = str(sub.get("note") or "").lower()
    if "referral" in note:
        return False
    grant = int(sub.get("grant_days") or 0)
    if 0 < grant < ACCOUNTS_MIN_SUB_DAYS:
        return False
    if "accounts_allowed" in sub and not bool(sub.get("accounts_allowed")):
        return False
    return True

async def add_subscription(user_id: int, days: int, added_by: int, note: str = ""):
    uid = int(user_id)
    days = max(0, int(days))
    expires_ts = _ts_now() + days * 86400
    note_l = str(note or "").lower()
    if "referral" in note_l or days < ACCOUNTS_MIN_SUB_DAYS:
        accounts_allowed = False
    else:
        accounts_allowed = _accounts_allowed_for_days(days)
    doc = {
        "user_id": uid,
        "expires_at": int(expires_ts),
        "created_at": _ts_now(),
        "added_by": int(added_by),
        "note": note or "",
        "grant_days": days,
        "accounts_allowed": accounts_allowed,
    }

    SUBS_COL.delete_many({"user_id": str(uid)})
    SUBS_COL.update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "expires_at": doc["expires_at"],
                "note": doc["note"],
                "added_by": doc["added_by"],
                "grant_days": days,
                "accounts_allowed": accounts_allowed,
            },
            "$setOnInsert": {"created_at": doc["created_at"]},
        },
        upsert=True,
    )
    return expires_ts

async def get_user_record(user_id: int):
    sub = SUBS_COL.find_one(_user_id_filter(user_id))
    if sub:
        return sub
    else:
        if int(user_id) in ADMIN_IDS:
            return True
        return None

async def del_user(user_id: int):
    SUBS_COL.delete_many(_user_id_filter(user_id))

async def extend_user_days(user_id: int, more_days: int, note: str | None = None):
    rec = await get_user_record(user_id)
    if not rec or rec is True:
        return None
    more_days = max(0, int(more_days))
    expires_ts = _as_expiry_ts(rec.get("expires_at", 0))
    base = max(expires_ts, _ts_now())
    new_exp = base + more_days * 86400
    prev_grant = int(rec.get("grant_days") or 0)
    new_grant = prev_grant + more_days
    merged_note = note if note is not None else (rec.get("note") or "")
    note_l = str(merged_note or "").lower()
    if "referral" in note_l or new_grant < ACCOUNTS_MIN_SUB_DAYS:
        accounts_allowed = False
    else:
        accounts_allowed = bool(rec.get("accounts_allowed")) or _accounts_allowed_for_days(
            new_grant
        ) or _accounts_allowed_for_days(more_days)
    update = {
        "user_id": int(user_id),
        "expires_at": int(new_exp),
        "grant_days": new_grant,
        "accounts_allowed": accounts_allowed,
    }
    if note is not None:
        update["note"] = note
    SUBS_COL.update_one(
        _user_id_filter(user_id),
        {"$set": update},
        upsert=True,
    )
    return new_exp

async def set_user_days_from_now(user_id: int, days: int):
    days = max(0, int(days))
    new_exp = _ts_now() + days * 86400
    accounts_allowed = _accounts_allowed_for_days(days)
    SUBS_COL.update_one(
        _user_id_filter(user_id),
        {
            "$set": {
                "user_id": int(user_id),
                "expires_at": int(new_exp),
                "grant_days": days,
                "accounts_allowed": accounts_allowed,
            }
        },
        upsert=True,
    )
    return new_exp

async def list_users_page(uid: int, page: int = 0):
    total = SUBS_COL.count_documents({})
    cur = (
        SUBS_COL.find({}, {"_id": 0})
        .sort("user_id", 1)
        .skip(page * USERS_PAGE_SIZE)
        .limit(USERS_PAGE_SIZE)
    )
    items = []
    for rec in cur:
        user = rec.get("user_id")
        exp = int(rec.get("expires_at", 0))
        items.append(
            {
                "user_id": user,
                "expires_at": exp,
                "days_left": _days_left(exp),
                "status": _status_badge(uid, exp),
                "note": rec.get("note", "") or "",
            }
        )
    return total, items

async def has_access(uid: int) -> bool:
    try:
        uid = int(uid)
    except Exception:
        return False
    if uid in ADMIN_IDS:
        return True
    sub = SUBS_COL.find_one(_user_id_filter(uid))
    if sub and _days_left(_as_expiry_ts(sub.get("expires_at", 0))) > 0:
        return True
    if db["accounts"].count_documents({"admin_id": uid}) > 0:
        return True
    try:
        main_users = await get_main_users(uid)
    except Exception:
        main_users = []
    for main_id in main_users:
        if db["accounts"].count_documents({"admin_id": int(main_id)}) > 0:
            return True
    plus = get_plus_subscription(uid)
    return bool(plus)

def build_users_keyboard(uid: int, total_count: int, items: list, page: int):
    rows = []
    if not items:
        rows.append([Button.inline(txt(uid, "no_users"), data=b"noop")])
    else:
        for it in items:
            user = it["user_id"]
            rows.append(
                [
                    Button.inline(
                        f"👤[5373012449597335010] {user} • {it['status']} • {it['days_left']} {txt(uid, 'days')}",
                        data=f"user={user}".encode(),
                    ),
                    Button.inline(
                        txt(uid, "delete_btn"), data=f"del_user={user}".encode()
                    ),
                ]
            )
    total_pages = (total_count + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(
            Button.inline(txt(uid, "prev_page"), data=f"users_page={page - 1}".encode())
        )
    if page + 1 < total_pages:
        nav.append(
            Button.inline(txt(uid, "next_page"), data=f"users_page={page + 1}".encode())
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            Button.inline(txt(uid, "add_user_btn"), data=b"add_user"),
            Button.inline(txt(uid, "back_btn"), data=b"admin_back"),
        ]
    )
    return rows

def build_user_detail_keyboard(uid: int):
    return [
        [
            Button.inline(txt(uid, "extend_30"), data=f"extend_user={uid}:30".encode()),
            Button.inline(txt(uid, "extend_90"), data=f"extend_user={uid}:90".encode()),
        ],
        [
            Button.inline(
                txt(uid, "set_days_manually"), data=f"set_days={uid}".encode()
            ),
            Button.inline(txt(uid, "delete_user_btn"), data=f"del_user={uid}".encode()),
        ],
        [Button.inline(txt(uid, "user_list_btn"), data=b"users")],
    ]

def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")

def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))

def _serialize_report_options(options) -> list[dict]:
    out = []
    for o in options or []:
        try:
            opt_b = bytes(o.option)
        except Exception:
            continue
        out.append({"option": _b64e(opt_b), "text": (o.text or "")})
    return out

class _ReportOptSnap:
    __slots__ = ("option", "text")

    def __init__(self, option: bytes, text: str = ""):
        self.option = option
        self.text = text or ""

def _deserialize_report_options(raw) -> list:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        opt = item.get("option")
        if isinstance(opt, str):
            try:
                opt = _b64d(opt)
            except Exception:
                continue
        if not isinstance(opt, (bytes, bytearray)):
            continue
        out.append(_ReportOptSnap(bytes(opt), item.get("text") or ""))
    return out

def _remember_report_options(ctx: dict, options) -> None:
    ctx["last_report_options"] = _serialize_report_options(options)

def _ctx_dumpable(ctx: dict) -> dict:
    out = dict(ctx)
    sample = out.get("sample") or {}
    out["sample"] = {"sess": sample.get("sess")} if sample else {}
    if isinstance(out.get("selected_story_ids"), set):
        out["selected_story_ids"] = sorted(out["selected_story_ids"])
    path = out.get("path") or []
    new_path = []
    for item in path:
        opt = item.get("option")
        if isinstance(opt, (bytes, bytearray)):
            opt = _b64e(opt)
        new_path.append(
            {
                "text": item.get("text", ""),
                "raw_text": item.get("raw_text", ""),
                "option": opt,
                "key": item.get("key") or "",
            }
        )
    out["path"] = new_path
    if isinstance(out.get("add_comment_option"), (bytes, bytearray)):
        out["add_comment_option"] = _b64e(out["add_comment_option"])
    if "stories" in out:
        out.pop("stories", None)
    return out

def _ctx_loadable(d: dict) -> dict:
    if not d:
        return {}
    if "selected_story_ids" in d and isinstance(d["selected_story_ids"], list):
        d["selected_story_ids"] = set(d["selected_story_ids"])
    if "path" in d:
        new_path = []
        for item in d["path"]:
            opt = item.get("option")
            if isinstance(opt, str):
                try:
                    opt = _b64d(opt)
                except Exception:
                    pass
            new_path.append(
                {
                    "text": item.get("text", ""),
                    "raw_text": item.get("raw_text", ""),
                    "option": opt,
                    "key": item.get("key") or "",
                }
            )
        d["path"] = new_path
    if isinstance(d.get("add_comment_option"), str):
        try:
            d["add_comment_option"] = _b64d(d["add_comment_option"])
        except Exception:
            d["add_comment_option"] = None
    samp = d.get("sample") or {}
    d["sample"] = {"sess": samp.get("sess")} if samp else {}
    return d

async def ctx_get(uid: int) -> dict | None:
    data = await redis.get(f"ctx:{uid}")
    if not data:
        return None
    try:
        obj = json.loads(data)
        return _ctx_loadable(obj)
    except Exception:
        return None

async def ctx_set(uid: int, ctx: dict):
    dump = _ctx_dumpable(ctx)
    await redis.set(f"ctx:{uid}", json.dumps(dump, ensure_ascii=False), ex=CTX_TTL)

async def ctx_pop(uid: int):
    await redis.delete(f"ctx:{uid}")

async def set_stop_flag(uid: int):
    await redis.set(f"stop:{uid}", "1", ex=3600)

async def clear_stop_flag(uid: int):
    await redis.delete(f"stop:{uid}")

async def is_stop_flagged(uid: int) -> bool:
    return await redis.exists(f"stop:{uid}") > 0

REPORT_HB_TTL = 600
_LIVE_REPORTS: dict[int, dict] = {}

async def touch_report_heartbeat(uid: int, ttl: int = REPORT_HB_TTL):
    if int(uid) not in _LIVE_REPORTS:
        return
    await redis.set(f"report_hb:{uid}", str(_now_ts()), ex=ttl)
    try:
        await redis.set(f"report_active:{int(uid)}", "1", ex=LOCK_TTL)
    except Exception:
        pass

async def clear_report_runtime(uid: int, *, clear_stop: bool = True):

    raw = await redis.get(f"active_pool:{uid}")
    if raw:
        try:
            pool = json.loads(raw)
            for sess in pool:
                try:
                    await unlock_account(sess)
                except Exception:
                    pass
        except Exception:
            pass
    await redis.delete(f"active_pool:{uid}")
    await redis.delete(f"report_active:{uid}")
    await redis.delete(f"report_hb:{uid}")
    await redis.delete(f"report_ui:{uid}")
    await redis.delete(f"report_released:{uid}")
    if clear_stop:
        await clear_stop_flag(uid)

async def _heartbeat_age(uid: int) -> int | None:
    hb = await redis.get(f"report_hb:{uid}")
    if not hb:
        return None
    try:
        if isinstance(hb, bytes):
            hb = hb.decode()
        return _now_ts() - int(hb)
    except Exception:
        return None

async def is_report_really_running(uid: int) -> bool:
    uid = int(uid)
    if uid in _LIVE_REPORTS:
        return True
    active = await redis.get(f"report_active:{uid}")
    if not active:
        return False
    age = await _heartbeat_age(uid)
    if age is None:
        return False
    return age <= REPORT_HB_TTL

async def kb_main(uid: int):
    if not await has_access(uid):
        buttons = [
            [Button.text(txt(uid, "buy_normal_subscription"), resize=True)],
            [Button.text(txt(uid, "buy_special_subscription"), resize=True)],
            [
                Button.text(txt(uid, "menu_my_credit"), resize=True),
                Button.text(txt(uid, "menu_referral"), resize=True),
            ],
            [Button.text(txt(uid, "language_button"), resize=True)],
        ]
        return buttons

    else:
        buttons = []
        if can_use_accounts(uid):
            buttons.append([Button.text(txt(uid, "menu_accounts"), resize=True)])
        buttons.extend(
            [
                [
                    Button.text(txt(uid, "menu_report_msg"), single_use=True),
                    Button.text(txt(uid, "menu_report_story"), single_use=True),
                    Button.text(txt(uid, "menu_report_bot"), single_use=True),
                ],
                [
                    Button.text(txt(uid, "menu_report_scam"), single_use=True),
                    Button.text(txt(uid, "menu_report_fake"), single_use=True),
                    Button.text(txt(uid, "menu_report_profile"), single_use=True),
                ],
                [
                    Button.text(txt(uid, "menu_join_request"), resize=True),
                    Button.text(txt(uid, "menu_send_pv"), resize=True),
                ],
            ]
        )
        ai_row = [Button.text(txt(uid, "menu_ai_analyze"), resize=True)]
        if uid in ADMIN_IDS:
            ai_row.append(Button.text(txt(uid, "menu_report_manage"), resize=True))
        buttons.append(ai_row)
        if not get_plus_subscription(uid):
            buttons.append([Button.text(txt(uid, "menu_partners"), resize=True)])
        buttons.append(
            [
                Button.text(txt(uid, "menu_my_credit"), resize=True),
                Button.text(txt(uid, "menu_referral"), resize=True),
            ]
        )
        buttons.append([Button.text(txt(uid, "language_button"), resize=True)])

        return buttons

_REPORT_KIND_FA = {
    "msg": "ریپورت پیام",
    "story": "ریپورت استوری",
    "scam": "ریپورت اسکم (کلاهبرداری)",
    "fake": "ریپورت فیک (جعل هویت)",
    "profile": "ریپورت پروفایل",
    "bot": "ریپورت ربات",
    "dialog": "ریپورت گفتگو",
}

def set_report_notify_channel(channel):
    if isinstance(channel, dict):
        value = {
            "chat_id": str(channel.get("chat_id") or "").strip(),
            "url": str(channel.get("url") or "").strip(),
            "title": str(channel.get("title") or channel.get("chat_id") or "").strip()[:80],
            "username": str(channel.get("username") or "").strip().lstrip("@"),
        }
    else:
        value = (channel or "").strip()
    SETTINGS_COL.update_one(
        {"key": "report_notify_channel"},
        {"$set": {"value": value}},
        upsert=True,
    )

def _report_channel_ref() -> dict | None:
    doc = SETTINGS_COL.find_one({"key": "report_notify_channel"})
    if not doc:
        return None
    val = doc.get("value")
    if isinstance(val, dict):
        cid = str(val.get("chat_id") or "").strip()
        if not cid:
            return None
        username = str(val.get("username") or "").strip().lstrip("@")
        url = str(val.get("url") or "").strip()
        if not url and username:
            url = f"https://t.me/{username}"
        title = str(val.get("title") or cid).strip() or cid
        return {
            "chat_id": cid,
            "username": username,
            "title": title,
            "url": url,
            "id": cid,
        }
    channel = str(val or "").strip()
    if not channel:
        return None
    url = ""
    username = ""
    if channel.startswith("@"):
        username = channel[1:]
        url = f"https://t.me/{username}"
    elif channel.startswith("http"):
        url = channel
    return {
        "chat_id": channel,
        "username": username or channel.lstrip("@"),
        "title": channel,
        "url": url,
        "id": channel,
    }

def get_report_notify_channel() -> str:
    ch = _report_channel_ref()
    if not ch:
        return ""
    title = ch.get("title") or ch.get("chat_id") or ""
    url = (ch.get("url") or "").strip()
    if url and url not in title:
        return f"{title} | {url}"
    return title

def _notify_send_target(ch: dict):
    ref = ch.get("chat_id") or ch.get("username") or ""
    try:
        if str(ref).lstrip("-").isdigit():
            return int(ref)
    except Exception:
        pass
    return ref

async def _chat_from_invite(token: str):
    info = await bot(functions.messages.CheckChatInviteRequest(hash=token))
    if isinstance(info, types.ChatInviteAlready) and getattr(info, "chat", None):
        return info.chat
    if getattr(info, "request_needed", False):
        return None
    updates = await bot(functions.messages.ImportChatInviteRequest(hash=token))
    for chat in getattr(updates, "chats", None) or []:
        if isinstance(chat, (types.Channel, types.Chat)):
            return chat
    return None

async def _resolve_report_notify_input(raw: str) -> tuple[dict | None, str]:
    kind, token = _parse_target(raw)
    if kind == "username" and token:
        return {
            "chat_id": f"@{token}",
            "url": f"https://t.me/{token}",
            "title": f"@{token}",
            "username": token,
        }, ""
    if kind == "id" and token:
        chat_id = str(token)
        if not chat_id.startswith("-") and chat_id.startswith("100") and len(chat_id) >= 13:
            chat_id = f"-{chat_id}"
        title = chat_id
        username = ""
        try:
            ent = await bot.get_entity(int(chat_id))
            title = (getattr(ent, "title", None) or chat_id)[:80]
            username = (getattr(ent, "username", None) or "").strip()
        except Exception as e:
            logger.warning("notify channel id resolve failed %s: %s", chat_id, e.__class__.__name__)
        return {
            "chat_id": chat_id,
            "url": f"https://t.me/{username}" if username else "",
            "title": title,
            "username": username,
        }, ""
    if kind != "invite" or not token:
        return None, "report_notify_bad"
    url = f"https://t.me/+{token}"
    try:
        chat = await _chat_from_invite(token)
    except errors.UserAlreadyParticipantError:
        try:
            info = await bot(functions.messages.CheckChatInviteRequest(hash=token))
            chat = info.chat if isinstance(info, types.ChatInviteAlready) else None
        except Exception as e:
            logger.warning("notify invite recheck failed: %s", e)
            chat = None
    except Exception as e:
        logger.warning("notify invite join failed: %s", e)
        return None, "report_notify_invite_fail"
    if chat is None:
        return None, "report_notify_invite_fail"
    try:
        peer_id = await bot.get_peer_id(chat)
    except Exception as e:
        logger.warning("notify invite peer failed: %s", e)
        return None, "report_notify_invite_fail"
    username = (getattr(chat, "username", None) or "").strip()
    title = (getattr(chat, "title", None) or url)[:80]
    return {
        "chat_id": str(peer_id),
        "url": f"https://t.me/{username}" if username else url,
        "title": title,
        "username": username,
    }, ""

async def _report_channel_entity(ch: dict):
    ref = _channel_chat_ref(ch)
    if ref is None:
        return None
    try:
        return await bot.get_entity(ref)
    except Exception as e:
        logger.warning("report channel entity failed ref=%s: %s", ref, e.__class__.__name__)
        return None


async def _report_channel_join_url(ch: dict) -> str:
    url = (ch.get("url") or "").strip()
    if url.startswith("http"):
        return url
    username = (ch.get("username") or "").strip().lstrip("@")
    if username and not username.lstrip("-").isdigit():
        return f"https://t.me/{username}"
    title = (ch.get("title") or "").strip()
    link = ""
    ent = await _report_channel_entity(ch)
    if ent is not None:
        title = (getattr(ent, "title", None) or title or "").strip()
        uname = (getattr(ent, "username", None) or "").strip()
        if not uname:
            for u in getattr(ent, "usernames", None) or []:
                if getattr(u, "active", False) and getattr(u, "username", None):
                    uname = u.username
                    break
        if uname:
            username = uname
            link = f"https://t.me/{uname}"
    if not link and ent is not None:
        try:
            full = await bot(functions.channels.GetFullChannelRequest(channel=ent))
            inv = getattr(full.full_chat, "exported_invite", None)
            link = (getattr(inv, "link", None) or "").strip()
        except Exception as e:
            logger.warning("report channel full info failed: %s", e.__class__.__name__)
    if not link:
        try:
            exported = await bot(
                functions.messages.ExportChatInviteRequest(peer=ent or _notify_send_target(ch))
            )
            link = (getattr(exported, "link", None) or "").strip()
        except Exception as e:
            logger.warning("report channel invite export failed: %s", e.__class__.__name__)
    if not link:
        return ""
    saved = {
        "chat_id": str(ch.get("chat_id") or ""),
        "url": link,
        "title": (title or ch.get("chat_id") or link)[:80],
        "username": username,
    }
    set_report_notify_channel(saved)
    ch["url"] = link
    ch["title"] = saved["title"]
    ch["username"] = username
    return link


def _report_channel_label(uid: int, ch: dict) -> str:
    label = str(ch.get("title") or "").strip()
    if not label or label.lstrip("-").isdigit():
        username = (ch.get("username") or "").strip().lstrip("@")
        if username and not username.lstrip("-").isdigit():
            return f"@{username}"
        return txt(uid, "report_channel_default_label")
    return label


async def build_report_join_prompt(uid: int, ch: dict) -> tuple[str, list]:
    url = await _report_channel_join_url(ch)
    ch = _report_channel_ref() or ch
    label = _report_channel_label(uid, ch)
    channel_line = f"{label}\n🔗 {url}" if url else label
    text = txt(uid, "report_must_join", channel=channel_line)
    rows = []
    if url:
        rows.append([Button.url(txt(uid, "report_join_channel_btn"), url)])
    else:
        text += "\n\n" + txt(uid, "report_join_no_link")
        await _warn_admins_join_check(ch, "NO_JOIN_LINK")
    rows.append([Button.inline(txt(uid, "report_join_check_btn"), data=b"rnjoin:check")])
    return text, rows


async def ensure_report_channel_member(event, uid: int, *, resume: str | None = None) -> bool:
    if uid in ADMIN_IDS:
        return True
    ch = _report_channel_ref()
    if not ch:
        return True
    if await check_user_force_joined(uid, ch):
        return True
    if resume:
        prev = await ctx_get(uid) or {}
        prev["pending_report_resume"] = resume
        await ctx_set(uid, prev)
    text, rows = await build_report_join_prompt(uid, ch)
    try:
        await event.respond(text, buttons=rows)
    except Exception:
        try:
            await bot.send_message(uid, text, buttons=rows)
        except Exception:
            pass
    return False

async def _continue_after_report_join(event, uid: int, resume: str | None):
    if resume == "msg":
        await _start_dest_kind_wizard(event, "msg")
    elif resume == "scam":
        await _start_dest_kind_wizard(event, "scam")
    elif resume == "fake":
        await _start_dest_kind_wizard(event, "fake")
    elif resume == "story":
        await wizard_story(event)
    elif resume == "bot":
        await wizard_bot_dialog(event)
    elif resume == "profile":
        await wizard_profile(event)
    elif resume == "ai":
        await wizard_ai_analyze(event)
    elif resume == "report":
        ctx = await ctx_get(uid) or {}
        mode = ctx.get("mode")
        if mode and ctx.get("pool"):
            await start_continuous_report(
                event,
                uid,
                ctx,
                mode,
                bool(ctx.get("comment") or ctx.get("awaiting_comment")),
            )

async def notify_report_started(uid: int, mode: str, accounts: int, target: str):
    ch = _report_channel_ref()
    if not ch:
        return
    kind = _REPORT_KIND_FA.get(mode, mode or "—")
    text = (
        "#اطلاع_رسانی\n"
        f"•کاربر : {uid}\n"
        f"•درحال گزارش تعداد اکانت : {int(accounts)} تا می باشد\n"
        f"• نوع گزارش : {kind}"
    )
    try:
        await bot.send_message(_notify_send_target(ch), text)
    except Exception as e:
        logger.warning("report notify failed channel=%s: %s", ch.get("chat_id"), e)

async def is_session_released(uid: int, sess: str) -> bool:
    try:
        return bool(await redis.sismember(f"report_released:{uid}", str(sess)))
    except Exception:
        return False

async def release_report_accounts(owner_uid: int, count: int) -> int:
    count = max(0, int(count))
    if count <= 0:
        return 0
    raw = await redis.get(f"active_pool:{int(owner_uid)}")
    if not raw:
        return 0
    try:
        pool = json.loads(raw)
    except Exception:
        return 0
    key = f"report_released:{int(owner_uid)}"
    already = set(await redis.smembers(key) or [])
    picked = [str(s) for s in pool if str(s) not in already][:count]
    if not picked:
        return 0
    await redis.sadd(key, *picked)
    await redis.expire(key, LOCK_TTL)
    for sess in picked:
        try:
            await unlock_account(sess)
        except Exception:
            pass
    left = [str(s) for s in pool if str(s) not in already and str(s) not in set(picked)]
    if not left:
        await set_stop_flag(int(owner_uid))
    return len(picked)

async def _report_list_item(owner: int, stats: dict | None = None) -> dict:
    stats = stats or {}
    live = _LIVE_REPORTS.get(int(owner)) or {}
    if not stats:
        raw = await redis.get(f"report_stats:{owner}")
        if raw:
            try:
                stats = json.loads(raw)
            except Exception:
                stats = {}
    pool_raw = await redis.get(f"active_pool:{owner}")
    total = int(live.get("total") or stats.get("total_accounts") or 0)
    if pool_raw:
        try:
            total = len(json.loads(pool_raw))
        except Exception:
            pass
    released = 0
    try:
        released = int(await redis.scard(f"report_released:{owner}") or 0)
    except Exception:
        released = 0
    return {
        "uid": int(owner),
        "mode": live.get("mode") or stats.get("mode") or "",
        "target": live.get("target") or stats.get("target") or "",
        "total": total,
        "released": released,
        "active": max(0, total - released),
        "ok": int(stats.get("ok") or live.get("ok") or 0),
        "failed": int(stats.get("failed") or live.get("failed") or 0),
    }

async def list_running_reports() -> list[dict]:
    found: dict[int, dict] = {}
    for owner in list(_LIVE_REPORTS):
        try:
            found[int(owner)] = await _report_list_item(int(owner))
        except Exception as e:
            logger.warning("live report list uid=%s: %s", owner, e)
    try:
        async for key in redis.scan_iter(match="report_active:*"):
            try:
                owner = int(str(key).rsplit(":", 1)[-1])
            except Exception:
                continue
            if owner in found:
                continue
            if not await is_report_really_running(owner):
                continue
            found[owner] = await _report_list_item(owner)
    except Exception as e:
        logger.warning("list running reports: %s", e)
    return list(found.values())

async def show_lang_menu(event):
    uid = UID(event)
    buttons = [
        [Button.inline(txt(uid, "lang_fa"), data=b"lang:fa")],
        [Button.inline(txt(uid, "lang_ar"), data=b"lang:ar")],
        [Button.inline(txt(uid, "lang_en"), data=b"lang:en")],
    ]
    await event.reply(txt(uid, "lang_menu_text"), buttons=buttons)

async def show_my_credit(event):
    uid = UID(event)
    parts = [txt(uid, "my_credit_title")]
    has_any = False

    if uid in ADMIN_IDS:
        parts.append(txt(uid, "my_credit_admin"))
        has_any = True

    sub = SUBS_COL.find_one(_user_id_filter(uid))
    if sub:
        exp = _as_expiry_ts(sub.get("expires_at", 0))
        parts.append(
            txt(
                uid,
                "my_credit_normal",
                status=_status_badge(uid, exp),
                exp=_fmt_date(exp),
                days=_days_left(exp),
            )
        )
        has_any = True

    plus_doc = PLUS_SUBS_COL.find_one(_user_id_filter(uid))
    if plus_doc:
        exp = _as_expiry_ts(plus_doc.get("expires_at", 0))
        max_acc = int(plus_doc.get("max_accounts") or 0)
        parts.append(
            txt(
                uid,
                "my_credit_plus",
                status=_status_badge(uid, exp),
                exp=_fmt_date(exp),
                days=_days_left(exp),
                max=max_acc if max_acc > 0 else "∞",
            )
        )
        has_any = True

    if can_use_accounts(uid):
        try:
            acc_count = db["accounts"].count_documents({"admin_id": int(uid)})
        except Exception:
            acc_count = 0
        parts.append(txt(uid, "my_credit_accounts", count=acc_count))

    if not has_any:
        parts.insert(1, txt(uid, "my_credit_none"))

    buttons = [
        [Button.url(txt(uid, "contact_developer_btn"), "https://t.me/nifrtt")]
    ]
    await event.reply("\n\n".join(parts), buttons=buttons)

async def _save_pending_ref(uid: int, payload: str):
    payload = (payload or "").strip()
    if not payload:
        return
    try:
        await redis.set(f"pending_ref:{int(uid)}", payload, ex=7 * 86400)
    except Exception:
        pass

async def _pop_pending_ref(uid: int) -> str:
    key = f"pending_ref:{int(uid)}"
    try:
        val = await redis.get(key)
        if val is None:
            return ""
        try:
            await redis.delete(key)
        except Exception:
            pass
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="ignore").strip()
        return str(val).strip()
    except Exception:
        return ""

async def show_referral(event):
    uid = UID(event)
    cfg = get_referral_settings()
    if not cfg.get("enabled"):
        await event.reply(txt(uid, "referral_disabled"))
        return
    uname = await get_bot_username()
    link = referral_link_for(uid, uname)
    invited = get_referrer_invite_count(uid)
    required = int(cfg.get("required_invites") or 10)
    reward = int(cfg.get("reward_days") or 2)
    stats = get_referrer_stats(uid)
    claimed = int(stats.get("rewards_claimed") or 0)
    rem = invited % required
    left = required if rem == 0 else (required - rem)
    text = txt(
        uid,
        "referral_info",
        link=link,
        invited=invited,
        required=required,
        reward=reward,
        left=left,
        claimed=claimed * reward,
    )
    await event.reply(text)

def build_option_keyboard(
    uid: int, options: list[types.MessageReportOption], cols: int = 2
):
    btns = [
        Button.inline(
            _report_opt_text(uid, o), data=_encode_opt_callback("mr:", o.option)
        )
        for o in options
    ]
    rows = [btns[i : i + cols] for i in range(0, len(btns), cols)]
    rows.append([Button.inline(txt(uid, "report_option_cancel"), data=b"mr:cancel")])
    return rows

def dialog_reason_title(uid: int, options) -> str:
    text = txt(uid, "select_report_reason")
    if any(bytes(getattr(o, "option", b"") or b"").startswith(b"pr:") for o in options or []):
        text = f"{text}\n\n{txt(uid, 'peer_reason_scam_note')}"
    return text

def build_dialog_keyboard(
    uid: int, options: list[types.MessageReportOption], cols: int = 2
):
    btns = [
        Button.inline(
            _report_opt_text(uid, o), data=_encode_opt_callback("db:", o.option)
        )
        for o in options
    ]
    rows = [btns[i : i + cols] for i in range(0, len(btns), cols)]
    rows.append([Button.inline(txt(uid, "cancel_btn"), data=b"db:cancel")])
    return rows

def get_accuntes_buttons(uid: int, db, admin_id: int, page: int = 0):
    buttons = []
    accounts_col = db["accounts"]
    buttons.append(
        [Button.inline(txt(uid, "add_account_btn"), data=b"add_new_accounts")]
    )

    if admin_id in ADMIN_IDS:
        filter_query = {}
    else:
        filter_query = {"admin_id": admin_id}

    total_count = accounts_col.count_documents(filter_query)
    total_pages = (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    skip = page * ITEMS_PER_PAGE
    items = list(accounts_col.find(filter_query).skip(skip).limit(ITEMS_PER_PAGE))
    for acc in items:
        title = acc.get("phone", txt(uid, "unknown_phone"))
        if admin_id in ADMIN_IDS:
            owner = acc.get("admin_id")
            if owner != admin_id:
                title += txt(uid, "owner_label", owner=owner)
        sid = str(acc["_id"])
        buttons.append(
            [
                Button.inline(
                    f"🗂[5431736674147114227]{title}", data=f"get_code={sid}".encode()
                ),
                Button.inline(
                    txt(uid, "delete_btn"), data=f"delete_accounts={sid}".encode()
                ),
            ]
        )

    nav = []
    if page > 0:
        nav.append(
            Button.inline(txt(uid, "prev_page"), data=f"page_acc={page - 1}".encode())
        )
    if (page + 1) < total_pages:
        nav.append(
            Button.inline(txt(uid, "next_page"), data=f"page_acc={page + 1}".encode())
        )
    if nav:
        buttons.append(nav)
    return buttons

def get_accounts_buttons_from_sessions(
    uid: int, db, sessions: list[str], admin_id: int, page: int = 0
):
    buttons = []
    buttons.append(
        [Button.inline(txt(uid, "add_account_btn"), data=b"add_new_accounts")]
    )
    total_count = len(sessions)
    total_pages = (total_count + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    skip = page * ITEMS_PER_PAGE
    page_sessions = sessions[skip : skip + ITEMS_PER_PAGE]

    for sid in page_sessions:
        acc = db["accounts"].find_one({"_id": ObjectId(sid)})
        if not acc:
            continue
        title = acc.get("phone", txt(uid, "unknown_phone"))
        if admin_id in ADMIN_IDS:
            owner = acc.get("admin_id")
            if owner != admin_id:
                title += txt(uid, "owner_label", owner=owner)
        buttons.append(
            [
                Button.inline(
                    f"🗂[5431736674147114227]{title}", data=f"get_code={sid}".encode()
                ),
                Button.inline(
                    txt(uid, "delete_btn"), data=f"delete_accounts={sid}".encode()
                ),
            ]
        )

    nav = []
    if page > 0:
        nav.append(
            Button.inline(txt(uid, "prev_page"), data=f"page_acc={page - 1}".encode())
        )
    if (page + 1) < total_pages:
        nav.append(
            Button.inline(txt(uid, "next_page"), data=f"page_acc={page + 1}".encode())
        )
    if nav:
        buttons.append(nav)
    return buttons

def build_story_keyboard_sections_paged(
    uid: int,
    active_meta: list[dict],
    hl_items: list[dict],
    selected: set[int],
    page_num: int,
    has_prev: bool,
    has_next: bool,
):
    rows = []
    rows.append([Button.inline(txt(uid, "stories_active_header"), data=b"st:noop")])
    for m in active_meta:
        sid = m["id"]
        mark = (
            "✅[5206607081334906820]" if sid in selected else "❌[5210952531676504517]"
        )
        ts = ""
        if m.get("date"):
            dt2 = datetime.datetime.fromtimestamp(m["date"])
            ts = dt2.strftime("%H:%M %Y-%m-%d")
        label = f"{mark} #{sid} • {ts}" if ts else f"{mark} #{sid}"
        rows.append([Button.inline(label, data=f"st:toggle:{sid}".encode())])
    if active_meta:
        rows.append(
            [
                Button.inline(txt(uid, "select_all_active"), data=b"st:act_all"),
                Button.inline(txt(uid, "deselect_active"), data=b"st:act_none"),
            ]
        )
    rows.append([Button.inline(" ", data=b"st:noop")])
    rows.append(
        [Button.inline(txt(uid, "stories_hl_header", page=page_num), data=b"st:noop")]
    )
    for m in hl_items:
        sid = m["id"]
        mark = (
            "✅[5206607081334906820]" if sid in selected else "❌[5210952531676504517]"
        )
        ts = ""
        if m.get("date"):
            dt2 = datetime.datetime.fromtimestamp(m["date"])
            ts = dt2.strftime("%H:%M %Y-%m-%d")
        label = f"{mark} #{sid} • {ts}" if ts else f"{mark} #{sid}"
        rows.append([Button.inline(label, data=f"st:toggle:{sid}".encode())])
    nav = []
    if has_prev:
        nav.append(Button.inline(txt(uid, "prev_highlight"), data=b"st:hl_prev"))
    if has_next:
        nav.append(Button.inline(txt(uid, "next_highlight"), data=b"st:hl_next"))
    if nav:
        rows.append(nav)
    if hl_items:
        rows.append(
            [
                Button.inline(txt(uid, "select_all_hl"), data=b"st:hl_all"),
                Button.inline(txt(uid, "deselect_hl"), data=b"st:hl_none"),
            ]
        )
    rows.append(
        [
            Button.inline(txt(uid, "continue_btn"), data=b"st:next"),
            Button.inline(txt(uid, "cancel_btn"), data=b"st:cancel"),
        ]
    )
    return rows

def _is_join_request_pending_error(e: Exception) -> bool:

    if isinstance(e, getattr(errors, "InviteRequestSentError", ())):
        return True
    name = e.__class__.__name__
    if name in ("InviteRequestSentError",):
        return True
    msg = (getattr(e, "message", None) or str(e) or "").upper()

    return (
        "INVITE_REQUEST_SENT" in msg
        or "JOIN_REQUEST" in name.upper()
        or name.upper() == "INVITEREQUESTSENTERROR"
    )

def _classify_join_error(e: Exception) -> str:

    if _is_join_request_pending_error(e):
        return "pending"
    name = e.__class__.__name__
    msg = (getattr(e, "message", None) or str(e) or "").upper()
    if isinstance(e, ValueError) or name == "ValueError":
        if "EMPTY USERNAME" in msg:
            return "bad_username"
        if "ENTITY" in msg or "CANNOT FIND" in msg or "USERNAME" in msg:
            return "bad_username"
        return "bad_peer"
    if isinstance(e, errors.FloodWaitError) or "FLOOD" in name.upper() or "FLOOD_WAIT" in msg:
        return "flood"
    if isinstance(e, getattr(errors, "ChannelsTooMuchError", ())) or "CHANNELS_TOO_MUCH" in msg:
        return "too_many"
    if isinstance(e, getattr(errors, "UserBannedInChannelError", ())) or "USER_BANNED" in msg:
        return "banned"
    if isinstance(e, getattr(errors, "ChannelPrivateError", ())) or "CHANNEL_PRIVATE" in msg:
        return "private"
    if isinstance(
        e, (getattr(errors, "InviteHashInvalidError", ()), getattr(errors, "InviteHashExpiredError", ()))
    ) or "INVITE_HASH" in msg:
        return "bad_invite"
    if isinstance(e, getattr(errors, "UsersTooMuchError", ())) or "USERS_TOO_MUCH" in msg:
        return "full"
    if isinstance(e, getattr(errors, "InviteHashEmptyError", ())):
        return "bad_invite"
    if "USERNAME" in name.upper() or "USERNAME_NOT_OCCUPIED" in msg or "USERNAME_INVALID" in msg:
        return "bad_username"
    if "PEER_ID_INVALID" in msg or name in ("PeerIdInvalidError", "UserIdInvalidError"):
        return "bad_peer"
    if _is_frozen(e) or "FROZEN" in msg or "FROZEN" in name.upper() or "FOR FROZEN" in msg:
        return "frozen"
    if "AUTH" in name.upper() or "UNAUTHORIZED" in msg or "SESSION_REVOKED" in msg:
        return "auth"
    if "TIMEOUT" in name.upper() or "DISCONNECT" in msg or "CONNECTION" in msg:
        return "timeout"
    short = name.replace("Error", "") or "rpc"
    if short.lower() in ("value",):
        return "bad_peer"
    return short[:32]

async def _sleep_flood_wait(e: errors.FloodWaitError, *, cap: int = 75) -> None:
    secs = max(1, min(int(getattr(e, "seconds", 1) or 1), cap))
    logger.warning("join: FloodWait %ss (capped sleep)", secs)
    await asyncio.sleep(secs + 1)

async def _rpc_with_join_retry(factory, *, attempts: int = 3, label: str = "join"):

    last = None
    for i in range(attempts):
        try:
            return await factory()
        except errors.FloodWaitError as e:
            last = e
            if i >= attempts - 1:
                raise
            await _sleep_flood_wait(e)
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            last = e
            if i >= attempts - 1:
                raise
            logger.warning("%s transient %s (try %s/%s)", label, e, i + 1, attempts)
            await asyncio.sleep(1.5 * (i + 1))
        except errors.RPCError as e:

            msg = (getattr(e, "message", None) or str(e) or "").upper()
            if "TIMEOUT" in msg or "WAIT" in e.__class__.__name__.upper():
                last = e
                if i >= attempts - 1:
                    raise
                await asyncio.sleep(1.5 * (i + 1))
                continue
            raise
    if last:
        raise last
    raise RuntimeError(f"{label} retry exhausted")

def _peer_from_chat(chat):
    if isinstance(chat, types.Channel):
        return types.InputPeerChannel(chat.id, chat.access_hash)
    if isinstance(chat, types.Chat):
        return types.InputPeerChat(chat.id)
    if isinstance(chat, types.User):
        return types.InputPeerUser(chat.id, chat.access_hash)
    return chat

def _tl_seq(value):

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []

def _peer_from_updates(updates):

    if updates is None:
        return None
    seen: set[int] = set()
    stack = [updates]
    while stack:
        obj = stack.pop()
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)

        for chat in _tl_seq(getattr(obj, "chats", None)):
            peer = _peer_from_chat(chat)
            if peer is not None:
                return peer

        nested = getattr(obj, "updates", None)
        if isinstance(nested, (list, tuple)):
            for upd in nested:
                chat = getattr(upd, "chat", None) or getattr(upd, "channel", None)
                if chat is not None:
                    peer = _peer_from_chat(chat)
                    if peer is not None:
                        return peer

                if getattr(upd, "chats", None) is not None or getattr(
                    upd, "updates", None
                ) is not None:
                    stack.append(upd)
        elif nested is not None and nested is not obj:

            stack.append(nested)
    return None

def _entity_to_peer(entity):
    if isinstance(entity, types.Channel):
        return types.InputPeerChannel(entity.id, entity.access_hash)
    if isinstance(entity, types.Chat):
        return types.InputPeerChat(entity.id)
    if isinstance(entity, types.User):
        return types.InputPeerUser(entity.id, entity.access_hash)
    return entity

async def _resolve_username_entity(client, token: str):
    token = (token or "").lstrip("@").strip()
    if not token:
        raise ValueError("empty username")
    token = re.sub(r"[^A-Za-z0-9_]", "", token)
    if len(token) < 4:
        raise ValueError("empty username")
    last_err = None
    candidates = [token]
    low = token.lower()
    if low != token:
        candidates.append(low)

    for candidate in candidates:
        for getter in (
            lambda c=candidate: client.get_entity(c),
            lambda c=candidate: client.get_entity("@" + c),
            lambda c=candidate: client.get_input_entity(c),
            lambda c=candidate: client.get_input_entity("@" + c),
        ):
            try:
                ent = await getter()
                if ent is not None:
                    if hasattr(ent, "access_hash") or hasattr(ent, "channel_id") or hasattr(ent, "user_id"):
                        try:
                            return await client.get_entity(ent)
                        except Exception:
                            return ent
                    return ent
            except Exception as e:
                last_err = e

        try:
            res = await client(
                functions.contacts.ResolveUsernameRequest(username=candidate)
            )
            for chat in getattr(res, "chats", None) or []:
                return chat
            for user in getattr(res, "users", None) or []:
                return user
            peer = getattr(res, "peer", None)
            if peer is not None:
                return await client.get_entity(peer)
        except Exception as e:
            last_err = e

        try:
            res = await client(functions.contacts.SearchRequest(q=candidate, limit=20))
            want = candidate.lower()
            for u in getattr(res, "users", None) or []:
                if (getattr(u, "username", None) or "").lower() == want:
                    return u
            for c in getattr(res, "chats", None) or []:
                if (getattr(c, "username", None) or "").lower() == want:
                    return c
        except Exception as e:
            last_err = e

    if last_err:
        logger.warning("resolve @%s exhausted: %s", token, last_err)
        raise last_err
    raise ValueError(f"username_not_found:{token}")


async def _bot_can_resolve_target(target: str) -> tuple[bool, str]:
    """Check with bot client whether a public @/link exists."""
    kind, token = _parse_target(target)
    if kind == "username" and token:
        try:
            ent = await bot.get_entity(token)
            if ent is not None:
                return True, type(ent).__name__
        except Exception as e:
            return False, e.__class__.__name__
        try:
            ent = await _resolve_username_entity(bot, token)
            return True, type(ent).__name__
        except Exception as e:
            return False, e.__class__.__name__
    if kind == "id" and token:
        try:
            await bot.get_entity(int(token))
            return True, "id"
        except Exception as e:
            return False, e.__class__.__name__
    if kind == "invite":
        return True, "invite"
    return False, "unknown"


def _is_hard_target_error(info) -> bool:
    s = str(info or "").upper()
    return any(
        x in s
        for x in (
            "USERNAME_NOT_FOUND",
            "PROFILE_NOT_USER",
            "USERNAME_NOT_OCCUPIED",
            "USERNAME_INVALID",
        )
    )


def _is_resolve_fail_info(info) -> bool:
    s = str(info or "").upper()
    return "BAD_USERNAME" in s or "USERNAME_NOT" in s or "USERNAME_INVALID" in s


_AUTH_DROP_ERRORS = (
    "AUTHKEYUNREGISTERED",
    "SESSIONREVOKED",
    "SESSIONEXPIRED",
    "USERDEACTIVATED",
    "USERDEACTIVATEDBAN",
    "AUTHKEYDUPLICATED",
    "AUTHKEYPERMEMPTY",
)


def _should_drop_session_error(err) -> bool:
    name = (err.__class__.__name__ if err is not None else "") or ""
    msg = (getattr(err, "message", None) or str(err) or "").upper()
    blob = f"{name} {msg}".upper()
    return any(x in blob for x in _AUTH_DROP_ERRORS)


async def _resolve_profile_peer(client, target: str, sess_key: str | None = None):
    """Resolve user OR channel/chat peer for profile/peer reporting."""
    target = normalize_chat_target(target)
    kind, token = _parse_target(target)
    if kind in ("invite", "unknown"):
        return None

    entity = None
    try:
        if kind == "username" and token:
            entity = await _resolve_username_entity(client, token)
        elif kind == "id" and token:
            tid = int(token)
            try:
                entity = await client.get_entity(tid)
            except Exception:
                if not str(token).startswith("-100") and tid > 0:
                    entity = await client.get_entity(int(f"-100{tid}"))
                else:
                    raise
        else:
            entity = await client.get_entity(target)
    except Exception as e:
        logger.warning("resolve profile target=%r failed: %s", target, e)
        entity = None

    if entity is not None:
        peer = _entity_to_peer(entity)
        if _is_valid_peer(peer):
            return peer

    peer = await join_group(
        client, target, sess_key, skip_join=True, send_join_request=False
    )
    if _is_valid_peer(peer):
        return peer
    return None


def _join_fail_reason(peer) -> str | None:

    if peer is None or peer is False:
        return "JOIN_FAILED"
    if peer == "pending":
        return "JOIN_PENDING"
    if isinstance(peer, str):
        return peer if peer.startswith("JOIN_") else f"JOIN_FAILED:{peer}"
    return None

def _format_wizard_error(uid: int, payload) -> str:
    if payload == "JOIN_PENDING":
        return txt(uid, "join_pending_hint")
    if payload == "JOIN_FAILED" or (
        isinstance(payload, str) and payload.startswith("JOIN_FAILED")
    ):
        detail = ""
        if isinstance(payload, str) and ":" in payload:
            detail = payload.split(":", 1)[1].strip()
        hints = {
            "private": "کانال/گروه خصوصیه یا اکانت دسترسی نداره.",
            "banned": "اکانت داخل این کانال بن شده.",
            "flood": "تلگرام موقتاً لیمیت جوین گذاشته؛ کمی بعد دوباره بزن.",
            "too_many": "اکانت به سقف کانال‌ها رسیده.",
            "full": "گروه/کانال پر است.",
            "bad_invite": "لینک دعوت نامعتبر یا منقضی شده.",
            "need_invite": "برای این هدف لینک دعوت t.me/+ لازم است.",
            "skip_join_no_peer": "بدون جوین نمی‌شود؛ گزینه بله را بزن یا لینک عمومی بفرست.",
            "bad_username": "یوزرنیم/@ اشتباه است یا وجود ندارد.",
            "bad_peer": "هدف نامعتبر است یا اکانت آن را پیدا نکرد.",
            "Value": "هدف نامعتبر است یا اکانت آن را پیدا نکرد.",
            "auth": "سشن اکانت خراب/لاگ‌اوت شده.",
            "frozen": "اکانت فریز شده.",
            "timeout": "اتصال قطع شد؛ دوباره تلاش کن.",
            "rpc": "خطای موقت تلگرام/پروکسی؛ با اکانت دیگر یا کمی بعد دوباره بزن.",
        }
        extra = hints.get(detail, "")
        base = txt(uid, "join_failed_hint")
        return f"{base}\n({detail})" if detail and not extra else (
            f"{base}\n• {extra}" if extra else base
        )
    if payload in (
        "ChannelPrivateError",
        "CHANNEL_PRIVATE",
        "EXC_ChannelPrivateError",
    ):
        return txt(uid, "channel_private_hint")
    if payload == "MESSAGE_ID_REQUIRED":
        return (
            "❌ هیچ پیامی از هدف پیدا نشد.\n"
            "برای بات‌ها یک‌بار چت را باز کنید؛ برای پروفایل دوباره تلاش کنید."
        )
    if payload in ("PROFILE_NOT_USER", "USERNAME_NOT_FOUND"):
        return txt(uid, "profile_not_user")
    if isinstance(payload, str) and (
        payload.startswith("EXC_ValueError") or payload.startswith("VALUE:")
    ):
        return txt(uid, "report_value_error_hint")
    if isinstance(payload, str) and payload.endswith("_OPTION_UNAVAILABLE"):
        code = payload[: -len("_OPTION_UNAVAILABLE")].lower()
        if f"{code}_option_unavailable" in TRANSLATIONS:
            return txt(uid, f"{code}_option_unavailable")
        return txt(uid, "reason_option_unavailable", reason=_reason_label(code))
    if payload in ("NO_OPTION", "PATH_MISMATCH"):
        return txt(uid, "report_err_no_option")
    if isinstance(payload, str) and payload.startswith("RPC_ERROR:"):
        name = (payload.split(":") + [""])[1]
        hint_key = {
            "OPTION_INVALID": "report_err_option_invalid",
            "MESSAGE_ID_INVALID": "report_err_message_id_invalid",
            "MESSAGE_IDS_EMPTY": "report_err_message_id_invalid",
            "PEER_ID_INVALID": "report_err_peer_id_invalid",
            "CHANNEL_PRIVATE": "channel_private_hint",
        }.get(name)
        if hint_key:
            return txt(uid, hint_key)
        return txt(uid, "report_err_rpc", error=name or payload)
    return txt(uid, "error_occurred", error=payload)

async def join_group(
    client,
    link_or_username: str,
    sess_key: str | None = None,
    skip_join: bool = False,
    send_join_request: bool = False,
):

    target = (link_or_username or "").strip()
    if not target:
        return False
    sess = sess_key or "default"
    cache_key = (sess, target)
    if not skip_join:
        cached = _peer_cache_get(cache_key)
        if cached is not None and _is_valid_peer(cached):
            return cached
        if cached is not None:
            _peer_cache_pop(cache_key)
    kind, token = _parse_target(target)
    if kind == "unknown":
        logger.warning("join_group: unknown target format: %r", target)
        return False
    try:
        if kind == "username":
            try:
                entity = await _rpc_with_join_retry(
                    lambda: _resolve_username_entity(client, token),
                    label=f"get_entity(@{token})",
                )
            except Exception as e:
                logger.warning(
                    "join_group: get_entity(@%s) failed: %s [%s]",
                    token,
                    e,
                    _classify_join_error(e),
                )
                return f"JOIN_FAILED:{_classify_join_error(e)}"
            if not skip_join:
                if isinstance(entity, types.Channel):
                    needs_req = bool(getattr(entity, "join_request", False))
                    if needs_req and not send_join_request:
                        peer = _entity_to_peer(entity)
                        st = await check_membership(client, peer)
                        if st == "joined":
                            _peer_cache_set(cache_key, peer)
                            return peer

                        if getattr(entity, "username", None) or token:
                            logger.info(
                                "join_group: public @%s has join_request flag — "
                                "using peer for report without join-request",
                                token,
                            )
                            try:
                                await client(
                                    functions.channels.JoinChannelRequest(
                                        channel=entity
                                    )
                                )
                            except errors.UserAlreadyParticipantError:
                                pass
                            except Exception as e:
                                if _is_join_request_pending_error(e):
                                    logger.warning(
                                        "join_group: @%s join-request sent; keep peer",
                                        token,
                                    )
                                else:
                                    logger.warning(
                                        "join_group: optional join @%s: %s", token, e
                                    )
                            if not skip_join:
                                _peer_cache_set(cache_key, peer)
                            return peer
                        logger.info(
                            "join_group: @%s needs join request — skip (use Join Request menu)",
                            token,
                        )
                        return "pending"
                    try:

                        async def _join_ch():
                            return await client(
                                functions.channels.JoinChannelRequest(channel=entity)
                            )

                        await _rpc_with_join_retry(
                            _join_ch, label=f"JoinChannel(@{token})"
                        )
                    except errors.UserAlreadyParticipantError:
                        pass
                    except Exception as e:
                        if _is_join_request_pending_error(e):
                            peer = _entity_to_peer(entity)
                            if getattr(entity, "username", None) or token:
                                logger.warning(
                                    "join_group: @%s join-request pending — keep public peer",
                                    token,
                                )
                                if not skip_join:
                                    _peer_cache_set(cache_key, peer)
                                return peer
                            logger.warning(
                                "join_group: join request pending for @%s: %s", token, e
                            )
                            return "pending"
                        reason = _classify_join_error(e)
                        logger.warning(
                            "join_group: JoinChannel(@%s) failed: %s [%s]",
                            token,
                            e,
                            reason,
                        )

                        peer = _entity_to_peer(entity)
                        if not skip_join:
                            _peer_cache_set(cache_key, peer)
                        return peer
                elif getattr(entity, "bot", False):
                    try:
                        await client.send_message(entity, "/start")
                    except errors.FloodWaitError as e:
                        await _sleep_flood_wait(e)
                        try:
                            await client.send_message(entity, "/start")
                        except Exception as e2:
                            logger.warning("Start bot failed after flood: %s", e2)
                    except Exception as e:
                        logger.warning("Start bot failed: %s", e)
                elif isinstance(entity, types.Chat):
                    logger.warning(
                        "Target resolved to basic Chat (id=%s); need invite link.",
                        entity.id,
                    )
                    return "JOIN_FAILED:need_invite"

            peer = _entity_to_peer(entity)
            if not skip_join:
                _peer_cache_set(cache_key, peer)
            return peer

        if kind == "invite":
            invite_hash = token
            info = None

            try:

                async def _check():
                    return await client(
                        functions.messages.CheckChatInviteRequest(hash=invite_hash)
                    )

                info = await _rpc_with_join_retry(_check, label="CheckChatInvite")
            except (errors.InviteHashInvalidError, errors.InviteHashExpiredError):
                logger.warning("join_group: invalid/expired invite hash")
                return "JOIN_FAILED:bad_invite"
            except errors.RPCError as e:
                if _is_join_request_pending_error(e):
                    return "pending"
                logger.warning(
                    "[join_req] invite check rpc: %s [%s]", e, _classify_join_error(e)
                )
                info = None
            except Exception as e:
                logger.warning("[join_req] invite check error: %s", e)
                info = None

            if isinstance(info, types.ChatInviteAlready) and getattr(info, "chat", None):
                peer = _peer_from_chat(info.chat)
                if not skip_join:
                    _peer_cache_set(cache_key, peer)
                return peer

            request_needed = (
                bool(getattr(info, "request_needed", False)) if info else False
            )

            if skip_join:

                if isinstance(info, types.ChatInviteAlready) and getattr(
                    info, "chat", None
                ):
                    return _peer_from_chat(info.chat)
                return "pending" if request_needed else "JOIN_FAILED:skip_join_no_peer"

            if request_needed and not send_join_request:
                logger.info(
                    "join_group: invite needs approval — not sending request (use Join Request menu)"
                )
                return "pending"


            try:

                async def _import():
                    return await client(
                        functions.messages.ImportChatInviteRequest(hash=invite_hash)
                    )

                updates = await _rpc_with_join_retry(
                    _import, attempts=4, label="ImportChatInvite"
                )
            except errors.UserAlreadyParticipantError:
                try:
                    info2 = await client(
                        functions.messages.CheckChatInviteRequest(hash=invite_hash)
                    )
                    if isinstance(info2, types.ChatInviteAlready) and getattr(
                        info2, "chat", None
                    ):
                        peer = _peer_from_chat(info2.chat)
                        _peer_cache_set(cache_key, peer)
                        return peer
                except Exception:
                    pass
                return "JOIN_FAILED:already_no_peer"
            except errors.InviteRequestSentError:
                return "pending"
            except (errors.InviteHashInvalidError, errors.InviteHashExpiredError):
                return "JOIN_FAILED:bad_invite"
            except errors.RPCError as e:
                if _is_join_request_pending_error(e):
                    return "pending"
                reason = _classify_join_error(e)
                logger.warning(
                    "[join_req] import invite rpc failed: %s [%s]",
                    e,
                    reason,
                )
                return f"JOIN_FAILED:{reason}"
            except Exception as e:
                reason = _classify_join_error(e)
                logger.warning(
                    "[join_req] import invite error: %s [%s]",
                    e,
                    reason,
                )
                return f"JOIN_FAILED:{reason}"

            peer = _peer_from_updates(updates)
            if peer:
                _peer_cache_set(cache_key, peer)
                return peer

            await asyncio.sleep(1.0)
            try:
                info3 = await client(
                    functions.messages.CheckChatInviteRequest(hash=invite_hash)
                )
                if isinstance(info3, types.ChatInviteAlready) and getattr(
                    info3, "chat", None
                ):
                    peer = _peer_from_chat(info3.chat)
                    _peer_cache_set(cache_key, peer)
                    return peer
                if request_needed or bool(getattr(info3, "request_needed", False)):
                    return "pending"
            except errors.InviteRequestSentError:
                return "pending"
            except Exception as e:
                logger.warning("[join_req] post-import recheck failed: %s", e)

            return "pending" if request_needed else "JOIN_FAILED:no_peer_after_import"

        if kind == "id":
            try:
                peer_id = int(token or target)
                entity = await _rpc_with_join_retry(
                    lambda: client.get_entity(peer_id),
                    label=f"get_entity(id={peer_id})",
                )
            except Exception as e:
                reason = _classify_join_error(e)
                if str(token or target).startswith("-100"):
                    reason = "need_invite"
                logger.warning(
                    "join_group: get_entity(id=%s) failed: %s [%s]",
                    token or target,
                    e,
                    reason,
                )
                return f"JOIN_FAILED:{reason}"
            if isinstance(entity, types.Channel):
                if not skip_join:
                    needs_req = bool(getattr(entity, "join_request", False))
                    if needs_req and not send_join_request:
                        peer = types.InputPeerChannel(entity.id, entity.access_hash)
                        st = await check_membership(client, peer)
                        if st == "joined":
                            _peer_cache_set(cache_key, peer)
                            return peer
                        logger.info(
                            "join_group: id=%s needs join request — skip (use Join Request menu)",
                            entity.id,
                        )
                        return "pending"
                    try:

                        async def _join_id():
                            return await client(
                                functions.channels.JoinChannelRequest(channel=entity)
                            )

                        await _rpc_with_join_retry(
                            _join_id, label=f"JoinChannel(id={entity.id})"
                        )
                    except errors.UserAlreadyParticipantError:
                        pass
                    except Exception as e:
                        if _is_join_request_pending_error(e):
                            return "pending"
                        reason = _classify_join_error(e)
                        logger.warning(
                            "join_group: join by id failed: %s [%s]", e, reason
                        )
                        if reason in ("banned", "private", "too_many", "full", "flood"):
                            return f"JOIN_FAILED:{reason}"
                peer = types.InputPeerChannel(entity.id, entity.access_hash)
            elif isinstance(entity, types.Chat):
                peer = types.InputPeerChat(entity.id)
            elif isinstance(entity, types.User):
                peer = types.InputPeerUser(entity.id, entity.access_hash)
            else:
                peer = entity
            if not skip_join:
                _peer_cache_set(cache_key, peer)
            return peer
    except Exception as e:
        logger.exception("join_group unexpected error for %r: %s", target, e)
        return False
    return False

async def check_membership(client, peer) -> str:
    try:
        if isinstance(peer, types.InputPeerChannel):
            await client(
                functions.channels.GetParticipantRequest(
                    channel=peer, participant=types.InputPeerSelf()
                )
            )
            return "joined"
        if isinstance(peer, types.InputPeerChat):
            full = await client(
                functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
            )
            participants = getattr(
                getattr(full, "full_chat", None), "participants", None
            )
            me = await client.get_me()
            plist = getattr(participants, "participants", None) or []
            for p in plist:
                if getattr(p, "user_id", None) == me.id:
                    return "joined"
            return "not_member"
        if isinstance(peer, types.InputPeerUser):
            try:
                history = await client(
                    functions.messages.GetHistoryRequest(
                        peer=peer,
                        limit=1,
                        offset_id=0,
                        offset_date=None,
                        add_offset=0,
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                )
                msgs = getattr(history, "messages", None) or []
                return "joined" if msgs else "not_member"
            except errors.RPCError as e:
                logger.warning(f"[join_req] user history check rpc error: {e}")
                return "failed"
        return "failed"
    except errors.UserNotParticipantError:
        return "not_member"
    except errors.ChatAdminRequiredError:

        return "joined"
    except errors.ChannelPrivateError:
        return "not_member"
    except errors.RPCError as e:
        logger.warning(f"[join_req] membership check rpc error: {e}")
        return "failed"
    except Exception as e:
        logger.warning(f"[join_req] membership check error: {e}")
        return "failed"

_PREFLIGHT_HARD_FAIL = frozenset(
    {
        "banned",
        "private",
        "bad_invite",
        "bad_username",
        "bad_peer",
        "bad_session",
        "unauthorized",
        "auth",
        "frozen",
        "too_many",
        "full",
        "need_invite",
    }
)


def _preflight_keep_accounts(stats: dict) -> tuple[list, list, list, dict]:
    reasons = dict(stats.pop("__reasons__", {}) or {})
    joined, retryable, skipped = [], [], []
    for sess, st in list(stats.items()):
        if sess.startswith("__"):
            continue
        reason = (reasons.get(sess) or "").strip().lower()
        if st in ("joined", "already"):
            joined.append(sess)
        elif st == "pending":
            skipped.append(sess)
        elif st == "failed":
            if reason in _PREFLIGHT_HARD_FAIL:
                skipped.append(sess)
            else:
                retryable.append(sess)
        else:
            retryable.append(sess)
    return joined, retryable, skipped, reasons


async def preflight_join_check(uid: int, pool: list, target: str) -> dict:

    sem = asyncio.Semaphore(6)
    results = {}
    reasons = {}

    async def check_one(sess):
        async with sem:
            cli = None
            try:
                cli, fail = await connect_session_client(sess)
                if not cli:
                    results[sess] = "failed"
                    reason = (fail or "bad_session").lower()
                    if "not_authorized" in reason or reason == "not_authorized":
                        reason = "unauthorized"
                    reasons[sess] = reason
                    return
                res = await asyncio.wait_for(
                    join_group_with_status(cli, target, sess),
                    timeout=PREFLIGHT_JOIN_TIMEOUT,
                )
                results[sess] = (
                    "joined"
                    if res["status"] in ("joined", "already")
                    else res["status"]
                )
                if res.get("reason"):
                    reasons[sess] = res["reason"]
                if (reasons.get(sess) or "").lower() == "frozen":
                    try:
                        db["accounts"].update_one(
                            {"_id": ObjectId(sess)},
                            {
                                "$set": {
                                    "health_status": "frozen",
                                    "health_checked_at": _ts_now(),
                                    "health_detail": "preflight",
                                }
                            },
                        )
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                results[sess] = "failed"
                reasons[sess] = "timeout"
            except Exception as e:
                results[sess] = "failed"
                reasons[sess] = _classify_join_error(e)
                if reasons[sess] == "frozen":
                    try:
                        db["accounts"].update_one(
                            {"_id": ObjectId(sess)},
                            {
                                "$set": {
                                    "health_status": "frozen",
                                    "health_checked_at": _ts_now(),
                                    "health_detail": "preflight",
                                }
                            },
                        )
                    except Exception:
                        pass
            finally:
                await _safe_disconnect(cli)
            await asyncio.sleep(0.2)

    await asyncio.gather(*(check_one(s) for s in pool))
    logger.info(
        "[join_req] preflight for %s on %s: %s reasons=%s",
        uid,
        target,
        list(results.values()),
        reasons,
    )

    results["__reasons__"] = reasons
    return results

async def join_request_checker_task():
    while True:
        try:
            await asyncio.sleep(1800)
        except asyncio.CancelledError:
            raise
        try:
            now_ts = _now_ts()
            pending_reqs = list(
                db["join_requests"].find(
                    {"accounts.status": {"$in": ["pending", "failed"]}}
                ).limit(100)
            )
            for req in pending_reqs:
                uid = req["user_id"]
                newly = 0
                changed = False
                for acc in req.get("accounts", []):
                    if acc.get("status") not in ("pending", "failed"):
                        continue

                    first_seen = (
                        acc.get("first_checked_at") or acc.get("checked_at") or now_ts
                    )
                    if "first_checked_at" not in acc:
                        acc["first_checked_at"] = first_seen
                        changed = True
                    if now_ts - first_seen > 86400:
                        acc["status"] = "rejected"
                        acc["checked_at"] = now_ts
                        changed = True
                        continue
                    try:
                        if await is_account_locked(acc["sess"]):
                            continue
                    except Exception:
                        continue
                    cli = retern_client(acc["sess"])
                    if not cli:
                        continue
                    try:
                        await asyncio.wait_for(cli.connect(), timeout=25)
                        if not await cli.is_user_authorized():
                            continue
                        res = await join_group_with_status(
                            cli, req["target"], acc["sess"]
                        )
                        if res["status"] in ("joined", "already"):
                            acc["status"] = "joined"
                            newly += 1
                        elif res["status"] == "pending":
                            acc["status"] = "pending"
                        elif res["status"] == "failed" and now_ts - first_seen > 86400:
                            acc["status"] = "rejected"
                        acc["checked_at"] = now_ts
                        changed = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    finally:
                        try:
                            await cli.disconnect()
                        except Exception:
                            pass
                if changed:
                    db["join_requests"].update_one(
                        {"_id": req["_id"]}, {"$set": {"accounts": req["accounts"]}}
                    )
                if newly:
                    try:
                        await bot.send_message(
                            uid,
                            txt(
                                uid,
                                "jr_approved_notice",
                                count=newly,
                                target=req["target"],
                            ),
                        )
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[join_req] checker task error: {e}")

async def sample_step(
    uid: int, ctx: dict, option_bytes: bytes | None = None, message: str | None = None
):

    sample_sess = (ctx.get("sample") or {}).get("sess")
    if not sample_sess:
        return ("error", "NO_SAMPLE_SESS")
    cli, fail = await connect_session_client(sample_sess)
    if not cli:
        return ("error", fail or "BAD_SAMPLE_SESSION")
    res = None
    try:
        peer = await join_group(
            cli, ctx["target"], sample_sess, skip_join=ctx.get("skip_join", False)
        )
        if not _is_valid_peer(peer):

            kind, _tok = _parse_target(ctx.get("target") or "")
            if kind == "username":
                peer = await join_group(
                    cli, ctx["target"], sample_sess, skip_join=True
                )
        if ctx.get("mode") == "story":

            peer = await _resolve_story_peer(
                cli, ctx["target"], sample_sess, skip_join=ctx.get("skip_join", False)
            ) or peer
        fail = _join_fail_reason(peer)
        if fail or not _is_valid_peer(peer):
            if fail == "JOIN_PENDING" or peer == "pending":
                return ("error", "JOIN_PENDING")
            return ("error", fail or "JOIN_FAILED")

        path = list(ctx.get("path") or [])
        for step in path:
            step["option"] = _norm_option(step.get("option"))

        async def _report(option: bytes, msg: str = ""):
            if not _is_valid_peer(peer) or isinstance(peer, str):
                raise ValueError(f"invalid_peer:{peer!r}"[:100])
            if ctx["mode"] in ("msg", "scam", "fake"):
                ids = _safe_int32_ids(list(ctx.get("msg_ids") or []))
                if not ids:
                    raise ValueError("no_valid_message_ids")
                return await cli(
                    functions.messages.ReportRequest(
                        peer=peer, id=[ids[0]], option=option or b"", message=msg or ""
                    )
                )
            selected = ctx.get("selected_story_ids") or set()
            sids = _safe_int32_ids(list(selected))
            if not sids:
                raise ValueError("no_story_selected")
            sid = sids[0]
            return await cli(
                functions.stories.ReportRequest(
                    peer=peer, id=[sid], option=option or b"", message=msg or ""
                )
            )

        async def _report_with_refresh(option: bytes, msg: str = ""):

            nonlocal peer
            try:
                return await _report(option, msg)
            except errors.ChannelPrivateError:
                logger.warning(
                    "sample ChannelPrivate — force rejoin target=%s sess=%s",
                    ctx.get("target"),
                    sample_sess,
                )
                try:
                    _peer_cache_pop((sample_sess, ctx.get("target") or ""))
                except Exception:
                    pass
                if ctx.get("mode") == "story":
                    fresh = await _resolve_story_peer(
                        cli,
                        ctx["target"],
                        sample_sess,
                        skip_join=False,
                    )
                else:
                    fresh = await join_group(
                        cli,
                        ctx["target"],
                        sample_sess,
                        skip_join=False,
                    )
                if not _is_valid_peer(fresh):
                    fresh = await join_group(
                        cli,
                        ctx["target"],
                        sample_sess,
                        skip_join=True,
                    )
                if not _is_valid_peer(fresh):
                    raise
                peer = fresh
                await asyncio.sleep(0.8)
                return await _report(option, msg)

        reason_code = _mode_reason_code(ctx.get("mode")) or (
            _reason_code_from_path(path) if path else None
        )
        method = "stories.report" if ctx.get("mode") == "story" else "messages.report"

        def _fail(e, option, step):
            return _report_rpc_fail(
                method,
                e,
                peer=peer,
                ids=ctx.get("msg_ids") if method == "messages.report" else ctx.get("selected_story_ids"),
                option=option,
                reason=reason_code,
                step=step,
                sess=sample_sess,
                mode=ctx.get("mode"),
            )

        try:
            res = await _report_with_refresh(b"", "")
        except ValueError as e:
            return ("error", str(e))
        except struct.error as e:
            logger.error("sample_step: struct.error in ReportRequest: %s", e)
            return ("error", "report_serialization_failed")
        except errors.ChannelPrivateError:
            return ("error", "ChannelPrivateError")
        except errors.RPCError as e:
            if _is_frozen(e) or _is_unauthorized_like(e):
                raise
            return ("error", _fail(e, b"", 0))

        step_idx = 0
        for _ in range(14):
            if isinstance(res, types.ReportResultChooseOption):
                if step_idx >= len(path):
                    return ("choose", res.options)
                choice = _choose_path_option(res.options, path, step_idx, reason_code)
                if not choice:
                    logger.warning(
                        "sample_step: NO_OPTION mode=%s reason=%s step=%s path=%s avail=%s",
                        ctx.get("mode"),
                        reason_code,
                        step_idx,
                        [(s.get("key"), s.get("text")) for s in path],
                        [(_option_key(o), o.text) for o in (res.options or [])],
                    )
                    return ("error", "NO_OPTION")
                try:
                    res = await _report_with_refresh(choice.option, "")
                except struct.error as e:
                    logger.error("sample_step: struct.error mid-path: %s", e)
                    return ("error", "report_serialization_failed")
                except errors.ChannelPrivateError:
                    return ("error", "ChannelPrivateError")
                except errors.RPCError as e:
                    if _is_frozen(e) or _is_unauthorized_like(e):
                        raise
                    return ("error", _fail(e, choice.option, step_idx + 1))
                step_idx += 1
                continue
            if isinstance(res, types.ReportResultAddComment):
                ctx["add_comment_option"] = res.option
                if message is not None:
                    comment = (message or "").strip() or await _pick_comment(
                        ctx, need_comment=True
                    )
                    try:
                        res = await _report_with_refresh(res.option, comment)
                    except struct.error as e:
                        logger.error("sample_step: struct.error on comment: %s", e)
                        return ("error", "report_serialization_failed")
                    except errors.ChannelPrivateError:
                        return ("error", "ChannelPrivateError")
                    except errors.RPCError as e:
                        if _is_frozen(e) or _is_unauthorized_like(e):
                            raise
                        return ("error", _fail(e, res.option, f"{step_idx}+comment"))
                    continue
                return ("comment", res.option)
            if isinstance(res, types.ReportResultReported):
                _report_ok_log(
                    method,
                    mode=ctx.get("mode"),
                    reason=reason_code,
                    path=path,
                    peer=peer,
                    sess=sample_sess,
                    extra="sample=1",
                )
                return ("done", None)
            logger.warning("sample_step unexpected result: %r", type(res))
            return ("error", "UNEXPECTED")
        return ("error", "MAX_STEPS")

    except errors.RPCError as e:
        if _is_frozen(e) or _is_unauthorized_like(e):
            await drop_account(sample_sess, reason="frozen/unauthorized")
        return ("error", f"JOIN_FAILED:{_classify_join_error(e)}")
    except Exception as e:
        logger.exception("sample_step unexpected error: %s", e)
        return ("error", f"EXC_{e.__class__.__name__}")
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass

async def sample_step_with_pool_fallback(
    uid: int,
    ctx: dict,
    pool: list[str],
    option_bytes: bytes | None = None,
    message: str | None = None,
):
    candidates = [s for s in (pool or []) if s]
    cur = (ctx.get("sample") or {}).get("sess")
    ordered = []
    if cur:
        ordered.append(cur)
    for s in candidates:
        if s not in ordered:
            ordered.append(s)
    if len(ordered) > 1:
        tail = ordered[1:]
        random.shuffle(tail)
        ordered = [ordered[0]] + tail
    last = ("error", "JOIN_FAILED")
    for sess in ordered[: min(10, len(ordered))]:
        ctx["sample"] = {"sess": sess}
        await ctx_set(uid, ctx)
        status, payload = await sample_step(
            uid, ctx, option_bytes=option_bytes, message=message
        )
        if status != "error":
            return status, payload
        last = (status, payload)
        err = str(payload or "")
        if err in (
            "JOIN_PENDING",
            "NO_OPTION",
            "no_valid_message_ids",
            "no_story_selected",
        ):
            return status, payload
        if err.startswith("RPC_ERROR:"):
            if any(x in err for x in ("OPTION_INVALID", "MESSAGE_ID_INVALID", "MESSAGE_IDS_EMPTY")):
                return status, payload
            last = (status, payload)
            logger.warning(
                "sample fallback uid=%s phone=%s err=%s target=%s",
                uid,
                sess_phone(sess),
                err,
                (ctx.get("target") or "")[:80],
            )
            continue
        retryable = (
            err.startswith("JOIN_FAILED")
            or err in ("ChannelPrivateError", "BAD_SAMPLE_SESSION", "NO_SAMPLE_SESS")
            or "CHANNEL_PRIVATE" in err
            or err.startswith("EXC_")
            or err.endswith("Error")
        )
        if not retryable:
            return status, payload
        logger.warning(
            "sample fallback uid=%s phone=%s err=%s target=%s",
            uid,
            sess_phone(sess),
            err,
            (ctx.get("target") or "")[:80],
        )
    return last

async def connect_session_client(
    session_id: str,
) -> tuple[TelegramClient | None, str | None]:
    prefer_proxy = bool(list_enabled_proxies())
    order = (True, False) if prefer_proxy else (False, True)
    last_err = "CONNECT_FAILED"
    unauthorized_count = 0
    drop_reason = None
    for use_proxy in order:
        cli = retern_client(session_id, use_proxy=use_proxy)
        if not cli:
            continue
        try:
            await asyncio.wait_for(cli.connect(), timeout=25)
            _me, why = await _session_identity(cli)
            if why != "OK":
                if why == "NOT_AUTHORIZED":
                    unauthorized_count += 1
                    last_err = "NOT_AUTHORIZED"
                else:
                    last_err = why
                await _safe_disconnect(cli)
                continue
            if use_proxy:
                try:
                    user = db["accounts"].find_one({"_id": ObjectId(session_id)})
                    if user and user.get("proxy_id"):
                        mark_proxy_ok(user.get("proxy_id"))
                except Exception:
                    pass
            return cli, None
        except Exception as e:
            last_err = e.__class__.__name__
            if _should_drop_session_error(e):
                drop_reason = last_err
            logger.warning(
                "connect sess=%s proxy=%s failed: %s",
                session_id,
                use_proxy,
                last_err,
            )
            await _safe_disconnect(cli)
            if use_proxy:
                try:
                    user = db["accounts"].find_one({"_id": ObjectId(session_id)})
                    if user and user.get("proxy_id"):
                        mark_proxy_fail(user.get("proxy_id"))
                except Exception:
                    pass

    if drop_reason:
        try:
            await drop_account(session_id, reason=drop_reason)
        except Exception:
            pass
        return None, drop_reason

    if unauthorized_count > 0 and last_err == "NOT_AUTHORIZED":
        logger.warning(
            "sess=%s not authorized on %s attempt(s) — kept in DB (no auto-delete)",
            session_id,
            unauthorized_count,
        )
    return None, last_err

async def _report_target_posts(
    cli,
    session_id: str,
    peer,
    path: list,
    ctx_snapshot: dict,
    peer_cache: dict | None,
    *,
    reason_code: str,
):
    ids_list = _pick_msg_ids_for_session(ctx_snapshot, session_id)
    if not ids_list:
        return False, "no_valid_message_ids", peer, ids_list

    async def _once(p):
        last = "NONE"
        for mid in ids_list:
            ok, info = await _walk_messages_report(
                cli, p, path, ctx_snapshot, [mid], sess_id=session_id, reason_code=reason_code
            )
            if ok:
                return True, "REPORTED"
            last = info
            logger.warning(
                "%s report fail mid=%s info=%s %s",
                reason_code,
                mid,
                info,
                _report_log_bits(session_id, ctx_snapshot),
            )
            await _report_step_pause()
        return False, last

    ok, info = await _once(peer)
    if not ok and any(x in str(info).upper() for x in ("CHANNEL_PRIVATE", "PRIVATE", "JOIN")):
        try:
            _peer_cache_pop((session_id, ctx_snapshot.get("target") or ""))
        except Exception:
            pass
        if peer_cache is not None:
            peer_cache.pop(session_id, None)
        fresh = await join_group(
            cli,
            ctx_snapshot["target"],
            session_id,
            skip_join=False,
        )
        if _is_valid_peer(fresh):
            peer = fresh
            if peer_cache is not None:
                peer_cache[session_id] = peer
            ok, info = await _once(peer)
    return ok, info, peer, ids_list


async def _run_scam_report(cli, session_id, peer, path, ctx_snapshot, peer_cache):
    code = _reason_code_from_path(path)
    if code != "scam":
        logger.error(
            "scam handler refused: path resolves to %r, not scam path=%s %s",
            code,
            _path_keys_for_log(path),
            _report_log_bits(session_id, ctx_snapshot),
        )
        return (session_id, False, f"SCAM_PATH_INVALID:{code}")
    ok, info, peer, _ids = await _report_target_posts(
        cli, session_id, peer, path, ctx_snapshot, peer_cache, reason_code="scam"
    )
    if not ok:
        return (session_id, False, f"SCAM_{info}")
    comment = await _pick_comment(ctx_snapshot, need_comment=True)
    peer_ok, peer_info = await _send_peer_report(
        cli, peer, "scam", comment, sess_id=session_id, mode="scam"
    )
    if peer_ok:
        return (session_id, True, "REPORTED+CHANNEL")
    logger.warning(
        "scam channel-level report failed after the post report: %s %s",
        peer_info,
        _report_log_bits(session_id, ctx_snapshot),
    )
    return (session_id, True, f"REPORTED;CHANNEL_{peer_info}")


async def _run_fake_report(cli, session_id, peer, path, ctx_snapshot, peer_cache):
    code = _reason_code_from_path(path)
    if code != "fake":
        logger.error(
            "fake handler refused: path resolves to %r, not fake path=%s %s",
            code,
            _path_keys_for_log(path),
            _report_log_bits(session_id, ctx_snapshot),
        )
        return (session_id, False, f"FAKE_PATH_INVALID:{code}")
    ok, info, peer, _ids = await _report_target_posts(
        cli, session_id, peer, path, ctx_snapshot, peer_cache, reason_code="fake"
    )
    if not ok:
        return (session_id, False, f"FAKE_{info}")
    comment = await _pick_comment(ctx_snapshot, need_comment=True)
    peer_ok, peer_info = await _send_peer_report(
        cli, peer, "fake", comment, sess_id=session_id, mode="fake"
    )
    if peer_ok:
        return (session_id, True, "REPORTED+CHANNEL")
    logger.warning(
        "fake channel-level report failed after the post report: %s %s",
        peer_info,
        _report_log_bits(session_id, ctx_snapshot),
    )
    return (session_id, True, f"REPORTED;CHANNEL_{peer_info}")


async def run_path_with_client(
    uid: int,
    session_id: str,
    ctx_snapshot: dict,
    need_comment: bool = False,
    *,
    cli=None,
    peer_cache: dict | None = None,
):
    own_cli = cli is None
    if own_cli:
        cli, fail = await connect_session_client(session_id)
        if not cli:
            return (session_id, False, fail or "BAD_SESSION")
    try:
        cached = None
        if peer_cache is not None:
            cached = peer_cache.get(session_id)
        if cached is not None and _is_valid_peer(cached):
            peer = cached
        else:
            try:
                await asyncio.wait_for(
                    cli(functions.account.UpdateStatusRequest(offline=False)),
                    timeout=8,
                )
            except Exception:
                pass
            try:
                peer = await asyncio.wait_for(
                    join_group(
                        cli,
                        ctx_snapshot["target"],
                        session_id,
                        skip_join=ctx_snapshot.get("skip_join", False),
                    ),
                    timeout=PREFLIGHT_JOIN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return (session_id, False, "JOIN_TIMEOUT")
            if ctx_snapshot.get("mode") == "story":
                try:
                    peer = await asyncio.wait_for(
                        _resolve_story_peer(
                            cli,
                            ctx_snapshot["target"],
                            session_id,
                            skip_join=ctx_snapshot.get("skip_join", False),
                        ),
                        timeout=PREFLIGHT_JOIN_TIMEOUT,
                    ) or peer
                except asyncio.TimeoutError:
                    pass
            if peer_cache is not None and _is_valid_peer(peer):
                peer_cache[session_id] = peer
        if not _is_valid_peer(peer):
            reason = _join_fail_reason(peer) or "JOIN_FAILED"
            return (session_id, False, reason)

        path = ctx_snapshot.get("path", []) or []

        for step in path:
            step["option"] = _norm_option(step.get("option"))

        story_reason = _reason_code_from_path(path) if path else None

        async def _walk_story_report(sid: int) -> tuple[bool, str]:
            def _fail(e, option, step):
                return _report_rpc_fail(
                    "stories.report",
                    e,
                    peer=peer,
                    ids=[sid],
                    option=option,
                    reason=story_reason,
                    step=step,
                    sess=session_id,
                    mode="story",
                )

            try:
                await _global_report_gate()
                res = await cli(
                    functions.stories.ReportRequest(
                        peer=peer, id=[int(sid)], option=b"", message=""
                    )
                )
            except errors.RPCError as e:
                return False, _fail(e, b"", 0)
            except (ConnectionError, OSError) as e:
                return False, f"CONN_{e.__class__.__name__}"
            except Exception as e:
                return False, f"EXC_{e.__class__.__name__}"
            step_idx = 0
            for _ in range(14):
                if isinstance(res, types.ReportResultChooseOption):
                    choice = _choose_path_option(res.options, path, step_idx, story_reason)
                    if not choice:
                        avail = [
                            (o.option.decode("utf-8", "ignore"), o.text)
                            for o in (res.options or [])
                        ]
                        logger.warning(
                            "story NO_OPTION reason=%s step=%s want=%s avail=%s sess=%s",
                            story_reason,
                            step_idx,
                            (path[step_idx].get("key"), path[step_idx].get("text"))
                            if step_idx < len(path)
                            else "<beyond recorded path>",
                            avail,
                            session_id,
                        )
                        return False, "NO_OPTION" if step_idx < len(path) else "PATH_MISMATCH"
                    await _report_step_pause()
                    try:
                        await _global_report_gate()
                        res = await cli(
                            functions.stories.ReportRequest(
                                peer=peer,
                                id=[int(sid)],
                                option=choice.option,
                                message="",
                            )
                        )
                    except errors.RPCError as e:
                        return False, _fail(e, choice.option, step_idx + 1)
                    except (ConnectionError, OSError) as e:
                        return False, f"CONN_{e.__class__.__name__}"
                    except Exception as e:
                        return False, f"EXC_{e.__class__.__name__}"
                    step_idx += 1
                    continue
                if isinstance(res, types.ReportResultAddComment):
                    comment = await _pick_comment(ctx_snapshot, need_comment=True)
                    if not (comment or "").strip():
                        comment = (
                            "This story violates Telegram Terms of Service. "
                            "Please remove it and restrict the account."
                        )
                    await _report_step_pause()
                    try:
                        await _global_report_gate()
                        res = await cli(
                            functions.stories.ReportRequest(
                                peer=peer,
                                id=[int(sid)],
                                option=res.option,
                                message=comment,
                            )
                        )
                    except errors.RPCError as e:
                        return False, _fail(e, res.option, f"{step_idx}+comment")
                    except (ConnectionError, OSError) as e:
                        return False, f"CONN_{e.__class__.__name__}"
                    except Exception as e:
                        return False, f"EXC_{e.__class__.__name__}"
                    continue
                if isinstance(res, types.ReportResultReported):
                    _report_ok_log(
                        "stories.report",
                        mode="story",
                        reason=story_reason,
                        path=path,
                        peer=peer,
                        ids=[sid],
                        sess=session_id,
                    )
                    return True, "REPORTED"
                return False, f"UNEXPECTED:{type(res).__name__}"
            return False, "MAX_STEPS"

        if ctx_snapshot["mode"] == "scam":
            return await _run_scam_report(
                cli, session_id, peer, path, ctx_snapshot, peer_cache
            )

        if ctx_snapshot["mode"] == "fake":
            return await _run_fake_report(
                cli, session_id, peer, path, ctx_snapshot, peer_cache
            )

        if ctx_snapshot["mode"] == "msg":
            ids_list = _pick_msg_ids_for_session(ctx_snapshot, session_id)
            if not ids_list:
                return (session_id, False, "no_valid_message_ids")

            last_info = "NONE"
            any_ok = False
            for mid in ids_list:
                ok, info = await _walk_messages_report(
                    cli, peer, path, ctx_snapshot, [mid], sess_id=session_id
                )
                if ok:
                    any_ok = True
                    break
                last_info = info
                logger.warning(
                    "msg report fail mid=%s info=%s %s",
                    mid,
                    info,
                    _report_log_bits(session_id, ctx_snapshot),
                )
                if "CHANNEL_PRIVATE" in str(info).upper():
                    try:
                        _peer_cache_pop(
                            (session_id, ctx_snapshot.get("target") or "")
                        )
                    except Exception:
                        pass
                    if peer_cache is not None:
                        peer_cache.pop(session_id, None)
                    fresh = await join_group(
                        cli,
                        ctx_snapshot["target"],
                        session_id,
                        skip_join=False,
                    )
                    if _is_valid_peer(fresh):
                        peer = fresh
                        if peer_cache is not None:
                            peer_cache[session_id] = peer
                        ok2, info2 = await _walk_messages_report(
                            cli, peer, path, ctx_snapshot, [mid], sess_id=session_id
                        )
                        if ok2:
                            any_ok = True
                            break
                        last_info = info2
                await _report_step_pause()
            if not any_ok:
                return (session_id, False, last_info)

            if _path_needs_channel_ban(ctx_snapshot):
                peer_ok, peer_info = await _walk_peer_report(
                    cli, peer, path, ctx_snapshot, msg_ids=ids_list[:1], sess_id=session_id
                )
                if peer_ok:
                    return (session_id, True, "REPORTED+CHANNEL")

                if "MESSAGE_ID_REQUIRED" in str(peer_info):
                    logger.info(
                        "channel peer-report skipped (needs msg id): path=%s",
                        [(s.get("key"), s.get("text")) for s in path],
                    )
                else:
                    logger.warning(
                        "channel peer-report after msg failed: %s path=%s target=%s",
                        peer_info,
                        [(s.get("key"), s.get("text")) for s in path],
                        ctx_snapshot.get("target"),
                    )
                return (session_id, True, f"REPORTED;CHANNEL_{peer_info}")
            return (session_id, True, "REPORTED")

        sids = _safe_int32_ids(sorted(ctx_snapshot.get("selected_story_ids") or []))
        if not sids:
            return (session_id, False, "no_story_selected")
        any_ok = False
        last_info = "NONE"
        for sid in sids:
            ok, info = await _walk_story_report(sid)
            if ok:
                any_ok = True
            else:
                last_info = info
                logger.warning(
                    "story report fail sid=%s info=%s %s",
                    sid,
                    info,
                    _report_log_bits(session_id, ctx_snapshot),
                )
                if str(info).startswith("FLOODWAIT") or info.startswith("REMOVED"):
                    break
            await _report_step_pause()
        if not any_ok:
            return (session_id, False, last_info)
        return (session_id, True, "REPORTED")
    except errors.FloodWaitError as e:
        return (session_id, False, f"FLOODWAIT_{e.seconds}s")
    except errors.RPCError as e:
        if _is_frozen(e) or _is_unauthorized_like(e):
            await drop_account(session_id, reason="frozen/unauthorized")
            return (session_id, False, "REMOVED_FROZEN_OR_DELETED")
        return (
            session_id,
            False,
            _report_rpc_fail(
                "report_worker",
                e,
                reason=_reason_code_from_path(ctx_snapshot.get("path") or []),
                sess=session_id,
                mode=ctx_snapshot.get("mode"),
            ),
        )
    except ValueError as e:
        logger.warning(
            "report worker ValueError %s: %s",
            _report_log_bits(session_id, ctx_snapshot),
            e,
        )
        return (session_id, False, f"VALUE:{str(e)[:80]}")
    except (ConnectionError, OSError) as e:
        return (session_id, False, f"CONN_{e.__class__.__name__}")
    except Exception as e:
        logger.exception(
            "report worker unexpected %s: %s",
            _report_log_bits(session_id, ctx_snapshot),
            e,
        )
        return (session_id, False, f"EXC_{e.__class__.__name__}:{str(e)[:60]}")
    finally:
        if own_cli:
            await _safe_disconnect(cli)

async def run_dialog_with_client(
    uid: int, sess_file: str, ctx_snapshot: dict, need_comment: bool, rpt: int = 1, *, cli=None
):
    own_cli = cli is None
    if own_cli:
        cli, fail = await connect_session_client(sess_file)
        if not cli:
            return (sess_file, 0, rpt, fail or "BAD_SESSION")
    try:
        await cli(functions.account.UpdateStatusRequest(offline=False))
        if ctx_snapshot.get("mode") == "profile":
            peer = await _resolve_profile_peer(
                cli, ctx_snapshot["target"], sess_file
            )
            if not peer:
                logger.warning(
                    "profile resolve miss %s",
                    _report_log_bits(sess_file, ctx_snapshot),
                )
                return (sess_file, 0, rpt, "USERNAME_NOT_FOUND")
            path = list(ctx_snapshot.get("path") or [])
            for step in path:
                step["option"] = _norm_option(step.get("option"))
            ok = 0
            last = "NONE"
            rpt = max(0, int(rpt))
            for _ in range(rpt):
                ok_peer, info_peer = await _report_peer_legacy(
                    cli, peer, path, ctx_snapshot, sess_id=sess_file
                )
                if ok_peer:
                    ok += 1
                    last = info_peer
                else:
                    last = info_peer
                    logger.warning(
                        "profile peer-report fail %s info=%s",
                        _report_log_bits(sess_file, ctx_snapshot),
                        info_peer,
                    )
                    break
                await asyncio.sleep(random.uniform(1.0, 2.2))
            return (sess_file, ok, rpt, last)

        peer = await join_group(
            cli,
            ctx_snapshot["target"],
            sess_file,
            skip_join=ctx_snapshot.get("skip_join", False),
        )
        if not _is_valid_peer(peer):
            return (sess_file, 0, rpt, _join_fail_reason(peer) or "JOIN_FAILED")
        path = list(ctx_snapshot.get("path") or [])
        for step in path:
            step["option"] = _norm_option(step.get("option"))
        ok = 0
        last = "NONE"
        rpt = max(0, int(rpt))
        reason_code = _reason_code_from_path(path) if path else None

        if _path_is_peer_reason(path):
            for _ in range(max(1, rpt)):
                ok_peer, info_peer = await _report_peer_legacy(
                    cli, peer, path, ctx_snapshot, sess_id=sess_file
                )
                last = info_peer
                if not ok_peer:
                    break
                ok += 1
                await asyncio.sleep(random.uniform(1.0, 2.2))
            return (sess_file, ok, rpt, last)

        report_ids = _safe_int32_ids(list(ctx_snapshot.get("msg_ids") or []))
        if not report_ids:
            mid = await _ensure_reportable_msg_id(cli, peer)
            if mid:
                report_ids = [mid]
        if not report_ids:
            ok_peer, info_peer = await _report_peer_legacy(
                cli, peer, path, ctx_snapshot, sess_id=sess_file
            )
            if ok_peer:
                return (sess_file, max(1, rpt), rpt, info_peer)
            return (sess_file, 0, rpt, info_peer)

        async def _send(option: bytes, message: str, step):
            nonlocal last
            try:
                return await cli(
                    functions.messages.ReportRequest(
                        peer=peer,
                        id=report_ids[:1],
                        option=option,
                        message=message,
                    )
                )
            except errors.RPCError as e:
                if _is_frozen(e) or _is_unauthorized_like(e):
                    await drop_account(sess_file, reason="frozen/unauthorized")
                    last = "REMOVED_FROZEN_OR_DELETED"
                else:
                    last = _report_rpc_fail(
                        "messages.report",
                        e,
                        peer=peer,
                        ids=report_ids[:1],
                        option=option,
                        reason=reason_code,
                        step=step,
                        sess=sess_file,
                        mode=ctx_snapshot.get("mode"),
                    )
            except Exception as e:
                last = f"EXC_{e.__class__.__name__}"
            return None

        for _ in range(rpt):
            if not _is_valid_peer(peer) or isinstance(peer, str):
                last = _join_fail_reason(peer) or "JOIN_FAILED"
                break
            res = await _send(b"", "", 0)
            if res is None:
                break
            step_idx = 0
            for _ in range(12):
                if isinstance(res, types.ReportResultChooseOption):
                    choice = _choose_path_option(res.options, path, step_idx, reason_code)
                    if not choice:
                        logger.warning(
                            "dialog NO_OPTION reason=%s step=%s path=%s avail=%s sess=%s",
                            reason_code,
                            step_idx,
                            _path_keys_for_log(path),
                            [(_option_key(o), o.text) for o in (res.options or [])],
                            sess_file,
                        )
                        last = "NO_OPTION" if step_idx < len(path) else "PATH_MISMATCH"
                        break
                    res = await _send(choice.option, "", step_idx + 1)
                    if res is None:
                        break
                    step_idx += 1
                    continue
                if isinstance(res, types.ReportResultAddComment):
                    msg_text = await _pick_comment(ctx_snapshot, need_comment=True)
                    res = await _send(res.option, msg_text, f"{step_idx}+comment")
                    if res is None:
                        break
                    continue
                if isinstance(res, types.ReportResultReported):
                    ok += 1
                    last = "REPORTED"
                    _report_ok_log(
                        "messages.report",
                        mode=ctx_snapshot.get("mode"),
                        reason=reason_code,
                        path=path,
                        peer=peer,
                        ids=report_ids[:1],
                        sess=sess_file,
                    )
                    break
                last = "UNEXPECTED"
                break
            if last.startswith(("FLOODWAIT", "REMOVED", "RPC_", "EXC_")):
                break
            await asyncio.sleep(random.uniform(1.2, 2.6))
        return (sess_file, ok, rpt, last)
    finally:
        if own_cli:
            await _safe_disconnect(cli)

async def ask_pool_admin(event, uid):
    btns = [
        [Button.inline(txt(uid, "select_pool_my"), data=b"pool_mine")],
        [Button.inline(txt(uid, "select_pool_all"), data=b"pool_all")],
        [Button.inline(txt(uid, "cancel_btn"), data=b"pool_cancel")],
    ]
    await event.reply(txt(uid, "which_pool"), buttons=btns)

async def ask_num_accounts(event, uid, pool, busy: int = 0):
    max_acc = len(pool)
    if max_acc == 0:
        await respond_no_free_accounts(event, uid, busy)
        return None
    prompt = txt(uid, "how_many_accounts", max=max_acc)
    if busy > 0:
        prompt += "\n\n" + txt(uid, "accounts_busy_note", busy=busy)
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                prompt,
                buttons=conv_cancel_buttons(uid),
            )
            resp = await menu_safe_response(conv)
            try:
                num = int(resp.raw_text.strip())
            except Exception:
                await conv.send_message(txt(uid, "invalid_number"))
                return None
            if num < 1 or num > max_acc:
                await conv.send_message(txt(uid, "invalid_number_range", max=max_acc))
                return None
            await conv.send_message(
                f"✅ دریافت شد: {num} اکانت انتخاب شد.\n⏳ در حال آماده‌سازی عملیات..."
            )
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return
    return num

async def _auto_comment_then_start(event, uid, ctx, payload, mode: str):
    auto_msg = await _pick_comment(ctx, need_comment=True)
    ctx["comment"] = auto_msg
    ctx["comments"] = [auto_msg]
    status2, err = await sample_step_with_pool_fallback(
        uid,
        ctx,
        ctx.get("pool") or [],
        option_bytes=payload,
        message=auto_msg,
    )
    await ctx_set(uid, ctx)
    if status2 == "done":
        try:
            await event.edit(txt(uid, "path_complete"))
        except Exception:
            try:
                await event.respond(txt(uid, "path_complete"))
            except Exception:
                pass
        await start_continuous_report(event, uid, ctx, mode, need_comment=True)
    else:
        if str(err or "") in (
            "ChannelPrivateError",
            "CHANNEL_PRIVATE",
            "EXC_ChannelPrivateError",
        ):
            await event.respond(txt(uid, "channel_private_hint"))
        else:
            await event.respond(
                _format_wizard_error(uid, err or "COMMENT_AUTO_FAILED")
            )
        await ctx_pop(uid)

async def ask_comment_source(
    event, uid, ctx, *, mode: str, need_comment: bool = True, sample_option=None
):
    ctx["awaiting_comment_source"] = True
    ctx["comment_source_resume"] = {
        "mode": mode,
        "need_comment": bool(need_comment),
        "do_sample": sample_option is not None,
    }
    if sample_option is not None:
        ctx["add_comment_option"] = sample_option
        ctx["comment_needs_sample"] = True
    else:
        ctx["comment_needs_sample"] = False
    await ctx_set(uid, ctx)
    buttons = [
        [Button.inline(txt(uid, "comment_source_ai"), b"csrc:ai")],
        [Button.inline(txt(uid, "comment_source_text"), b"csrc:text")],
        [Button.inline(txt(uid, "cancel_btn"), data=b"csrc:cancel")],
    ]
    text = txt(uid, "comment_source_prompt")
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        try:
            await event.respond(text, buttons=buttons)
        except Exception:
            await event.reply(text, buttons=buttons)

async def _finish_after_comment_source(event, uid, ctx):
    resume = ctx.pop("comment_source_resume", None) or {}
    mode = resume.get("mode") or ctx.get("mode") or "scam"
    need_comment = bool(resume.get("need_comment", True))
    do_sample = bool(
        resume.get("do_sample")
        or ctx.get("comment_needs_sample")
        or ctx.get("add_comment_option")
    )
    ctx["awaiting_comment_source"] = False
    if (ctx.get("comment_source") or "").lower() == "ai":
        ctx.pop("comment", None)
        ctx.pop("comments", None)
    await ctx_set(uid, ctx)

    if (
        do_sample
        and ctx.get("add_comment_option") is not None
        and mode in ("msg", "story", "scam", "fake")
    ):
        msg = await _pick_comment(ctx, need_comment=True)
        status2, err = await sample_step_with_pool_fallback(
            uid,
            ctx,
            ctx.get("pool") or [],
            option_bytes=ctx.get("add_comment_option"),
            message=msg,
        )
        await ctx_set(uid, ctx)
        if status2 != "done":
            if str(err or "") in (
                "ChannelPrivateError",
                "CHANNEL_PRIVATE",
                "EXC_ChannelPrivateError",
            ):
                await event.respond(txt(uid, "channel_private_hint"))
            else:
                await event.respond(_format_wizard_error(uid, err or "COMMENT_FAILED"))
            await ctx_pop(uid)
            return
        try:
            await event.edit(txt(uid, "path_complete"))
        except Exception:
            try:
                await event.respond(txt(uid, "path_complete"))
            except Exception:
                pass
    ctx.pop("comment_needs_sample", None)
    await ctx_set(uid, ctx)
    await start_continuous_report(event, uid, ctx, mode, need_comment)

@bot.on(events.CallbackQuery(pattern=b"^csrc:"))
async def on_comment_source_choice(event: events.CallbackQuery.Event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or not ctx.get("awaiting_comment_source"):
        await event.answer()
        return
    data = (event.data or b"").decode()
    if data == "csrc:ai":
        s = get_ai_settings()
        if not s.get("enabled") or not (s.get("api_key") or "").strip():
            await event.answer(txt(uid, "ai_no_key"), alert=True)
            return
    await event.answer()
    if data == "csrc:cancel":
        try:
            await event.edit(txt(uid, "cancel"))
        except Exception:
            pass
        await ctx_pop(uid)
        return
    if data == "csrc:text":
        resume = ctx.get("comment_source_resume") or {}
        ctx["comment_source"] = "text"
        ctx["awaiting_comment_source"] = False
        ctx["awaiting_comment"] = True
        ctx["comment_needs_sample"] = bool(
            resume.get("do_sample") or ctx.get("add_comment_option")
        )
        await ctx_set(uid, ctx)
        try:
            await event.edit(txt(uid, "send_comment"))
        except Exception:
            await event.respond(txt(uid, "send_comment"))
        return
    if data == "csrc:ai":
        ctx["comment_source"] = "ai"
        await _finish_after_comment_source(event, uid, ctx)
        return

async def start_continuous_report(event, uid, ctx, mode, need_comment):
    if not await ensure_report_channel_member(event, uid, resume="report"):
        return
    if mode not in ("scam", "fake") and (ctx.get("comment_source") or "").strip().lower() not in (
        "ai",
        "text",
    ):
        await ask_comment_source(
            event,
            uid,
            ctx,
            mode=mode,
            need_comment=True,
            sample_option=None,
        )
        return

    pool = ctx.get("pool", [])
    if not pool:
        await event.reply(txt(uid, "empty_pool"))
        await ctx_pop(uid)
        return

    if await is_report_really_running(uid):
        await event.reply(
            txt(uid, "report_already_running"),
            buttons=[
                [
                    Button.inline(
                        txt(uid, "stop_button"), data=f"stop:{uid}".encode()
                    )
                ]
            ],
        )
        return

    run_mode = (ctx.get("report_run_mode") or "").strip().lower()
    if run_mode not in ("loop", "fixed"):
        ctx["pending_report"] = {"mode": mode, "need_comment": bool(need_comment)}
        ctx["awaiting_run_mode"] = True
        await ctx_set(uid, ctx)
        try:
            await event.respond(
                txt(uid, "report_run_mode_prompt"),
                buttons=[
                    [Button.inline(txt(uid, "report_run_loop"), data=b"runmode:loop")],
                    [Button.inline(txt(uid, "report_run_fixed"), data=b"runmode:fixed")],
                    [Button.inline(txt(uid, "cancel_btn"), data=b"runmode:cancel")],
                ],
            )
        except Exception:
            await event.reply(
                txt(uid, "report_run_mode_prompt"),
                buttons=[
                    [Button.inline(txt(uid, "report_run_loop"), data=b"runmode:loop")],
                    [Button.inline(txt(uid, "report_run_fixed"), data=b"runmode:fixed")],
                    [Button.inline(txt(uid, "cancel_btn"), data=b"runmode:cancel")],
                ],
            )
        return

    per_account = max(1, int(ctx.get("reports_per_account") or 1)) if run_mode == "fixed" else 0

    target_now = (ctx.get("target") or "").strip()
    bot_ok, bot_info = await _bot_can_resolve_target(target_now)
    if not bot_ok and _parse_target(target_now)[0] == "username":
        await event.reply(
            txt(
                uid,
                "report_target_invalid_stop",
                target=target_now[:120],
                reason=f"bot_resolve:{bot_info}",
            )
        )
        await ctx_pop(uid)
        return

    if (
        not ctx.get("skip_join")
        and not ctx.get("jr_confirmed")
        and mode in ("msg", "scam", "fake", "dialog")
    ):
        stats = await preflight_join_check(uid, pool, ctx["target"])
        joined, retryable, skipped, reasons = _preflight_keep_accounts(stats)
        if bot_ok:
            for s, st in list(stats.items()):
                if str(s).startswith("__"):
                    continue
                reason = (reasons.get(s) or "").lower()
                if st == "failed" and reason in (
                    "bad_username",
                    "bad_peer",
                    "timeout",
                    "flood",
                    "rpc",
                ):
                    if s not in joined and s not in retryable:
                        retryable.append(s)
                    if s in skipped:
                        skipped = [x for x in skipped if x != s]
        keep = list(dict.fromkeys(joined + retryable))
        if not keep:
            pending_only = [
                s
                for s, st in stats.items()
                if not str(s).startswith("__") and st == "pending"
            ]
            if pending_only and not joined and not retryable:
                await event.reply(txt(uid, "jr_all_pending"))
            else:
                await event.reply(txt(uid, "jr_all_failed"))
                if bot_ok:
                    try:
                        await event.reply(txt(uid, "report_accounts_cannot_resolve", target=target_now[:80]))
                    except Exception:
                        pass
                elif reasons:
                    try:
                        top = {}
                        for r in reasons.values():
                            top[r] = top.get(r, 0) + 1
                        summary = ", ".join(f"{k}:{v}" for k, v in sorted(top.items()))
                        await event.reply(txt(uid, "jr_fail_reasons", reasons=summary))
                    except Exception:
                        pass
            await ctx_pop(uid)
            return

        pool = keep
        ctx["pool"] = pool
        await ctx_set(uid, ctx)
        await event.reply(
            txt(
                uid,
                "jr_joined_ok",
                count=len(joined),
                retry=len(retryable),
                skipped=len(skipped),
                total=len(pool),
            )
        )

    for sess in pool:
        await lock_account(sess, uid)
    await redis.set(f"active_pool:{uid}", json.dumps(pool), ex=LOCK_TTL)
    await redis.set(f"report_active:{uid}", "1", ex=LOCK_TTL)
    _LIVE_REPORTS[int(uid)] = {
        "uid": int(uid),
        "mode": mode,
        "target": (ctx.get("target") or "")[:200],
        "total": len(pool),
        "ok": 0,
        "failed": 0,
    }
    try:
        await touch_report_heartbeat(uid)
        await clear_stop_flag(uid)

        ctx["entity_kind"] = _infer_entity_kind(
            ctx.get("target") or "", ctx.get("entity_kind")
        )
        await ctx_set(uid, ctx)

        logger.info(
            "report start uid=%s mode=%s target=%s entity=%s msg_ids=%s accounts=%s phones=%s",
            uid,
            mode,
            (ctx.get("target") or "")[:120],
            ctx.get("entity_kind") or "?",
            list(ctx.get("msg_ids") or [])[:8],
            len(pool),
            [sess_phone(s) for s in pool[:15]],
        )
        await notify_report_started(uid, mode, len(pool), (ctx.get("target") or "")[:120])

        random.shuffle(pool)
        is_slow = _needs_slow_pace(ctx)
        conc_cap = REPORT_CONCURRENCY_TEXT if is_slow else REPORT_CONCURRENCY
        concurrency = max(1, min(conc_cap, len(pool)))
        op_timeout = _account_op_timeout(ctx)
        if run_mode == "fixed":
            start_text = txt(uid, "report_started_fixed", count=per_account, accounts=len(pool))
        else:
            start_text = txt(uid, "report_started_loop")
        msg = await event.reply(
            start_text,
            buttons=_report_live_buttons(uid),
        )

        stats = {
            "ok": 0,
            "failed": 0,
            "att": 0,
            "completed": 0,
            "total_accounts": len(pool),
            "concurrency": concurrency,
            "mode": mode,
            "run_mode": run_mode,
            "reports_per_account": per_account,
            "target": (ctx.get("target") or "")[:200],
            "started_at": _now_ts(),
        }
        await _save_report_stats(uid, stats)
        edit_state: dict = {}
        await _safe_edit_progress(msg, uid, stats, _state=edit_state)
        lock = asyncio.Lock()
        queue = asyncio.Queue()
        for sess in pool:
            await queue.put(sess)
        ctx_snapshot = {k: v for k, v in ctx.items() if k != "sample" and k != "pool"}
        ctx_snapshot["report_run_mode"] = run_mode
        ctx_snapshot["reports_per_account"] = per_account
        done_counts: dict[str, int] = {s: 0 for s in pool}
        abort_state = {
            "hits": 0,
            "resolve_hits": 0,
            "notified": False,
            "reason": "",
            "bot_ok": bool(bot_ok),
        }

        PROGRESS_EDIT_EVERY = max(8, int(os.getenv("PROGRESS_EDIT_EVERY", "12")))

        async def progress_ticker():
            while not await is_stop_flagged(uid):
                await asyncio.sleep(PROGRESS_EDIT_EVERY)
                await touch_report_heartbeat(uid)
                async with lock:
                    snap = dict(stats)
                await _save_report_stats(uid, snap)
                await _safe_edit_progress(msg, uid, snap, _state=edit_state)

        async def run_one(sess, cli=None, peer_cache=None):
            timeout = op_timeout
            if mode in ("msg", "story", "scam", "fake"):
                _, success, info = await asyncio.wait_for(
                    run_path_with_client(
                        uid,
                        sess,
                        ctx_snapshot,
                        need_comment,
                        cli=cli,
                        peer_cache=peer_cache,
                    ),
                    timeout=timeout,
                )
            else:
                rpt_n = 3 if _path_needs_channel_ban(ctx_snapshot) else 1
                _, ok_n, _, info = await asyncio.wait_for(
                    run_dialog_with_client(
                        uid, sess, ctx_snapshot, need_comment, rpt=rpt_n, cli=cli
                    ),
                    timeout=timeout,
                )
                success = ok_n > 0
            return success, info

        async def _maybe_abort_bad_target(info) -> bool:
            reason = str(info or "")
            hard = _is_hard_target_error(reason)
            resolve_fail = _is_resolve_fail_info(reason)
            if not hard and not resolve_fail:
                return False
            if hard and not abort_state["bot_ok"]:
                abort_state["hits"] += 1
                abort_state["reason"] = reason
                need = 2
            elif resolve_fail:
                abort_state["resolve_hits"] += 1
                abort_state["reason"] = reason
                need = max(3, min(6, max(2, len(pool) // 2 + 1)))
                if abort_state["resolve_hits"] < need:
                    return False
            else:
                return False

            if hard and not abort_state["bot_ok"] and abort_state["hits"] < need:
                return False

            if not abort_state["notified"]:
                abort_state["notified"] = True
                await set_stop_flag(uid)
                try:
                    if abort_state["bot_ok"] and resolve_fail:
                        await msg.reply(
                            txt(
                                uid,
                                "report_accounts_cannot_resolve",
                                target=(ctx_snapshot.get("target") or "")[:120],
                            )
                        )
                    else:
                        await msg.reply(
                            txt(
                                uid,
                                "report_target_invalid_stop",
                                target=(ctx_snapshot.get("target") or "")[:120],
                                reason=abort_state["reason"],
                            )
                        )
                except Exception:
                    pass
            return True

        async def worker(worker_id: int):
            nonlocal stats
            await asyncio.sleep(worker_id * random.uniform(0.05, 0.2))
            while not await is_stop_flagged(uid):
                try:
                    sess = queue.get_nowait()
                except asyncio.QueueEmpty:
                    if run_mode == "fixed":
                        async with lock:
                            if all(done_counts.get(s, 0) >= per_account for s in pool):
                                return
                    await asyncio.sleep(0.15)
                    continue

                if await is_session_released(uid, sess):
                    try:
                        await unlock_account(sess)
                    except Exception:
                        pass
                    queue.task_done()
                    continue

                is_cancelled = False
                max_rounds = (
                    per_account
                    if run_mode == "fixed"
                    else max(1, REPORT_CONN_ROUNDS)
                )
                cli = None
                peer_cache: dict = {}
                try:
                    cli, fail = await connect_session_client(sess)
                    if not cli:
                        async with lock:
                            stats["failed"] += 1
                            stats["att"] += 1
                            stats["completed"] += 1
                            done_counts[sess] = done_counts.get(sess, 0) + 1
                            await _save_report_stats(uid, stats)
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                    else:
                        rounds = 0
                        while (
                            rounds < max_rounds
                            and not await is_stop_flagged(uid)
                            and not await is_session_released(uid, sess)
                        ):
                            if run_mode == "fixed":
                                async with lock:
                                    if done_counts.get(sess, 0) >= per_account:
                                        break

                            success = False
                            info = None
                            try:
                                if cli and not cli.is_connected():
                                    await cli.connect()
                                logger.info(
                                    "report try uid=%s %s",
                                    uid,
                                    _report_log_bits(sess, ctx_snapshot),
                                )
                                success, info = await run_one(
                                    sess, cli=cli, peer_cache=peer_cache
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "account operation timeout uid=%s %s",
                                    uid,
                                    _report_log_bits(sess, ctx_snapshot),
                                )
                                info = "timeout"
                            except asyncio.CancelledError:
                                is_cancelled = True
                                raise
                            except Exception as e:
                                info = str(e)
                                logger.exception(
                                    "account operation failed uid=%s %s error=%s",
                                    uid,
                                    _report_log_bits(sess, ctx_snapshot),
                                    info,
                                )

                            async with lock:
                                if success:
                                    stats["ok"] += 1
                                    abort_state["hits"] = 0
                                    logger.info(
                                        "report ok uid=%s %s info=%s",
                                        uid,
                                        _report_log_bits(sess, ctx_snapshot),
                                        info,
                                    )
                                else:
                                    logger.error(
                                        "report failed uid=%s %s info=%s",
                                        uid,
                                        _report_log_bits(sess, ctx_snapshot, info=info),
                                        info,
                                    )
                                    stats["failed"] += 1
                                stats["att"] += 1
                                stats["completed"] += 1
                                done_counts[sess] = done_counts.get(sess, 0) + 1
                                await _save_report_stats(uid, stats)

                            rounds += 1

                            if not success and await _maybe_abort_bad_target(info):
                                break

                            flood_s = _flood_seconds_from_info(info)
                            if flood_s:
                                await asyncio.sleep(
                                    min(flood_s, 20) + random.uniform(0.5, 1.2)
                                )
                                peer_cache.pop(sess, None)
                                break
                            elif _is_conn_fail_info(info) or info == "timeout":
                                await asyncio.sleep(random.uniform(0.8, 1.8))
                                peer_cache.pop(sess, None)
                                break
                            else:
                                await asyncio.sleep(_report_inter_delay(ctx_snapshot))

                            if run_mode == "fixed":
                                async with lock:
                                    if done_counts.get(sess, 0) >= per_account:
                                        break
                except asyncio.CancelledError:
                    is_cancelled = True
                    raise
                finally:
                    await _safe_disconnect(cli)
                    queue.task_done()
                    if await is_session_released(uid, sess):
                        try:
                            await unlock_account(sess)
                        except Exception:
                            pass
                    elif not await is_stop_flagged(uid) and not is_cancelled:
                        if run_mode == "loop":
                            await queue.put(sess)
                        else:
                            async with lock:
                                still_need = done_counts.get(sess, 0) < per_account
                            if still_need:
                                await queue.put(sess)

        tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]
        ticker_task = asyncio.create_task(progress_ticker())
        last_lock_refresh = _now_ts()
        try:
            while any(not t.done() for t in tasks):
                if await is_stop_flagged(uid):
                    break
                if run_mode == "fixed":
                    async with lock:
                        if all(done_counts.get(s, 0) >= per_account for s in pool):
                            break
                await touch_report_heartbeat(uid)
                await asyncio.sleep(1)
                if _now_ts() - last_lock_refresh > 600:
                    released_now = set(await redis.smembers(f"report_released:{uid}") or [])
                    for _sess in pool:
                        if str(_sess) in released_now:
                            try:
                                await unlock_account(_sess)
                            except Exception:
                                pass
                            continue
                        await lock_account(_sess, uid)
                    await redis.set(f"active_pool:{uid}", json.dumps(pool), ex=LOCK_TTL)
                    await redis.set(f"report_active:{uid}", "1", ex=LOCK_TTL)
                    await touch_report_heartbeat(uid)
                    last_lock_refresh = _now_ts()
        finally:
            _LIVE_REPORTS.pop(int(uid), None)
            for t in tasks:
                if not t.done():
                    t.cancel()
            if not ticker_task.done():
                ticker_task.cancel()
            await asyncio.gather(*tasks, ticker_task, return_exceptions=True)
            for sess in pool:
                try:
                    await unlock_account(sess)
                except Exception:
                    pass
            await clear_report_runtime(uid)
            await _save_report_stats(uid, stats, ttl=1800)
            await _safe_edit_progress(msg, uid, stats, _state=edit_state)
            try:
                elapsed = _format_runtime(_report_elapsed_seconds(stats))
                after_buttons = await report_after_buttons(uid, ctx, pool)
                await msg.reply(
                    f"✅ عملیات تمام شد.\n"
                    f"✅ موفق: {stats['ok']}\n"
                    f"❌ ناموفق: {stats['failed']}\n"
                    f"⏱ زمان: {elapsed}",
                    buttons=after_buttons or None,
                )
            except Exception:
                pass
            await ctx_pop(uid)
    finally:
        _LIVE_REPORTS.pop(int(uid), None)


AFTER_REPORT_JOB_TTL = 7 * 24 * 3600
LEAVE_CONCURRENCY = 5


def _is_group_target(ctx: dict) -> bool:
    if (ctx.get("mode") or "") not in ("msg", "scam", "fake"):
        return False
    if (ctx.get("entity_kind") or "channel") not in ("channel", "chat"):
        return False
    return _parse_target(ctx.get("target") or "")[0] in ("username", "invite", "id")


async def report_after_buttons(uid: int, ctx: dict, pool: list) -> list:
    if not _is_group_target(ctx or {}):
        return []
    jid = secrets.token_hex(4)
    job = {"uid": int(uid), "target": ctx.get("target") or "", "pool": list(pool or [])}
    try:
        await redis.set(f"after_job:{jid}", json.dumps(job), ex=AFTER_REPORT_JOB_TTL)
    except Exception:
        return []
    rows = []
    if not ctx.get("skip_join"):
        rows.append([Button.inline(txt(uid, "leave_btn"), data=f"leave:{jid}".encode())])
    rows.append([Button.inline(txt(uid, "target_status_btn"), data=f"tstat:{jid}".encode())])
    return rows


async def _load_after_job(event, uid: int, jid: str) -> dict | None:
    raw = await redis.get(f"after_job:{jid}")
    job = None
    if raw:
        try:
            job = json.loads(raw)
        except Exception:
            job = None
    if not job or (int(job.get("uid") or 0) != int(uid) and uid not in ADMIN_IDS):
        await event.answer(txt(uid, "after_job_expired"), alert=True)
        return None
    return job


async def _leave_one(sess: str, target: str) -> str:
    try:
        if await is_account_locked(sess):
            return "busy"
    except Exception:
        return "busy"
    cli, _fail = await connect_session_client(sess)
    if not cli:
        return "failed"
    try:
        peer = await join_group(cli, target, sess, skip_join=True)
        if not _is_valid_peer(peer):
            return "not_member"
        if isinstance(peer, types.InputPeerChat):
            await cli(
                functions.messages.DeleteChatUserRequest(
                    chat_id=peer.chat_id, user_id=types.InputUserSelf()
                )
            )
        else:
            await cli(functions.channels.LeaveChannelRequest(channel=peer))
        return "left"
    except errors.RPCError as e:
        name = _tg_error_name(e)
        if name in ("USER_NOT_PARTICIPANT", "CHANNEL_PRIVATE", "CHAT_ADMIN_REQUIRED"):
            return "not_member"
        logger.warning("leave failed sess=%s target=%s: %s", sess, target[:80], name)
        return "failed"
    except Exception as e:
        logger.warning("leave failed sess=%s target=%s: %s", sess, target[:80], e.__class__.__name__)
        return "failed"
    finally:
        _peer_cache_pop((sess, target))
        await _safe_disconnect(cli)


async def leave_target_with_pool(target: str, pool: list) -> dict:
    counts = {"left": 0, "not_member": 0, "busy": 0, "failed": 0}
    sem = asyncio.Semaphore(LEAVE_CONCURRENCY)

    async def one(sess):
        async with sem:
            result = await _leave_one(sess, target)
        counts[result] = counts.get(result, 0) + 1

    await asyncio.gather(*(one(s) for s in pool))
    return counts


@bot.on(events.CallbackQuery(pattern=rb"^leave:[0-9a-f]{8}$"))
async def on_leave_target(event):
    uid = UID(event)
    jid = event.data.decode().split(":", 1)[1]
    job = await _load_after_job(event, uid, jid)
    if not job:
        return
    try:
        if not await redis.set(f"leave_run:{jid}", "1", ex=AFTER_REPORT_JOB_TTL, nx=True):
            await event.answer(txt(uid, "leave_already"), alert=True)
            return
    except Exception:
        pass
    await event.answer()
    target = job.get("target") or ""
    pool = list(job.get("pool") or [])
    await event.respond(txt(uid, "leave_started", count=len(pool), target=target))
    counts = await leave_target_with_pool(target, pool)
    await event.respond(
        txt(
            uid,
            "leave_done",
            target=target,
            left=counts["left"],
            not_member=counts["not_member"],
            busy=counts["busy"],
            failed=counts["failed"],
        )
    )


def _restriction_text(ent) -> str:
    reasons = []
    for r in getattr(ent, "restriction_reason", None) or []:
        text = (getattr(r, "text", "") or getattr(r, "reason", "") or "").strip()
        platform = getattr(r, "platform", "") or ""
        if text:
            reasons.append(f"{text} ({platform})" if platform else text)
    return " | ".join(dict.fromkeys(reasons))


async def _resolve_target_entity(target: str, pool: list):
    kind, token = _parse_target(target)
    if kind == "username" and token:
        try:
            return await bot.get_entity(token), ""
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            return None, "USERNAME_NOT_FOUND"
        except errors.RPCError as e:
            name = _tg_error_name(e)
            if name in ("CHANNEL_PRIVATE", "CHANNEL_INVALID"):
                return None, name
        except Exception:
            pass
    for sess in pool[:5]:
        try:
            if await is_account_locked(sess):
                continue
        except Exception:
            continue
        cli, _fail = await connect_session_client(sess)
        if not cli:
            continue
        try:
            peer = await join_group(cli, target, sess, skip_join=True)
            if not _is_valid_peer(peer):
                return None, _join_fail_reason(peer) or "NOT_FOUND"
            return await cli.get_entity(peer), ""
        except errors.RPCError as e:
            return None, _tg_error_name(e)
        except Exception as e:
            return None, e.__class__.__name__
        finally:
            await _safe_disconnect(cli)
    return None, "NO_FREE_ACCOUNT"


async def target_status_text(uid: int, target: str, pool: list) -> str:
    ent, err = await _resolve_target_entity(target, pool)
    if ent is None:
        key = "target_status_gone" if err in ("USERNAME_NOT_FOUND", "CHANNEL_PRIVATE", "CHANNEL_INVALID") else "target_status_unknown"
        return txt(uid, key, target=target, error=err)
    yes, no = txt(uid, "yes"), txt(uid, "no")
    restricted = bool(getattr(ent, "restricted", False))
    reason = _restriction_text(ent) if restricted else ""
    return txt(
        uid,
        "target_status_result",
        target=target,
        title=(getattr(ent, "title", None) or getattr(ent, "first_name", None) or target)[:60],
        restricted=yes if restricted else no,
        reason=reason or "—",
        scam=yes if getattr(ent, "scam", False) else no,
        fake=yes if getattr(ent, "fake", False) else no,
    )


@bot.on(events.CallbackQuery(pattern=rb"^tstat:[0-9a-f]{8}$"))
async def on_target_status(event):
    uid = UID(event)
    jid = event.data.decode().split(":", 1)[1]
    job = await _load_after_job(event, uid, jid)
    if not job:
        return
    await event.answer(txt(uid, "target_status_checking"))
    text = await target_status_text(uid, job.get("target") or "", list(job.get("pool") or []))
    await event.respond(text)


@bot.on(events.NewMessage(pattern=r"^/targetstatus(?:@\w+)?(?:\s+(.+))?$"))
async def target_status_command(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    raw = ((event.pattern_match.group(1) if event.pattern_match else "") or "").strip()
    target = normalize_chat_target(raw) if raw else ""
    if not target or _parse_target(target)[0] == "unknown":
        await event.respond(txt(uid, "target_status_usage"))
        raise events.StopPropagation
    await event.respond(txt(uid, "target_status_checking"))
    pool = await list_session_files(uid)
    await event.respond(await target_status_text(uid, target, pool))
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"^runmode:"))
async def on_report_run_mode(event: events.CallbackQuery.Event):
    await event.answer()
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or not ctx.get("awaiting_run_mode"):
        return
    data = event.data
    pending = ctx.get("pending_report") or {}
    mode = pending.get("mode") or ctx.get("mode")
    need_comment = bool(pending.get("need_comment", False))
    if not mode or not ctx.get("pool"):
        await event.edit(txt(uid, "session_not_found"))
        await ctx_pop(uid)
        return

    if data == b"runmode:cancel":
        try:
            await event.edit(txt(uid, "cancel"))
        except Exception:
            pass
        await ctx_pop(uid)
        return

    if data == b"runmode:loop":
        ctx["report_run_mode"] = "loop"
        ctx["reports_per_account"] = 0
        ctx["awaiting_run_mode"] = False
        ctx.pop("pending_report", None)
        await ctx_set(uid, ctx)
        try:
            await event.edit(txt(uid, "report_run_loop_selected"))
        except Exception:
            await event.respond(txt(uid, "report_run_loop_selected"))
        await start_continuous_report(event, uid, ctx, mode, need_comment)
        return

    if data == b"runmode:fixed":
        ctx["awaiting_run_mode"] = False
        await ctx_set(uid, ctx)
        try:
            await event.edit(txt(uid, "report_run_fixed_selected"))
        except Exception:
            pass
        try:
            async with standard_conversation(uid, timeout=90) as conv:
                await conv.send_message(
                    txt(uid, "report_count_prompt"),
                    buttons=conv_cancel_buttons(uid),
                )
                resp = await menu_safe_response(conv)
                try:
                    num = int((resp.raw_text or "").strip())
                except Exception:
                    await conv.send_message(txt(uid, "invalid_number"))
                    await ctx_pop(uid)
                    return
                if num < 1 or num > 10000:
                    await conv.send_message(txt(uid, "report_count_invalid"))
                    await ctx_pop(uid)
                    return
                await conv.send_message(txt(uid, "report_count_received", count=num))
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                await bot.send_message(uid, txt(uid, "timeout_or_invalid"))
            except Exception:
                pass
            await ctx_pop(uid)
            return

        fresh = await ctx_get(uid) or ctx
        fresh["report_run_mode"] = "fixed"
        fresh["reports_per_account"] = num
        fresh["pending_report"] = {"mode": mode, "need_comment": need_comment}
        fresh.pop("awaiting_run_mode", None)
        await ctx_set(uid, fresh)
        await start_continuous_report(event, uid, fresh, mode, need_comment)
        return

@bot.on(events.NewMessage(pattern="/language"))
async def lang_menu(event):
    uid = UID(event)
    await ctx_pop(uid)
    buttons = [
        [Button.inline(txt(uid, "lang_fa"), data=b"lang:fa")],
        [Button.inline(txt(uid, "lang_ar"), data=b"lang:ar")],
        [Button.inline(txt(uid, "lang_en"), data=b"lang:en")],
    ]
    await event.reply(txt(uid, "lang_menu_text"), buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b"lang:"))
async def set_lang(event):
    uid = UID(event)
    lang = event.data.decode().split(":", 1)[1]
    if lang not in ("fa", "ar", "en"):
        await event.answer(txt(uid, "invalid_input"), alert=True)
        return
    await event.answer()
    set_user_lang(uid, lang)
    try:
        await event.edit(txt(uid, "lang_changed"))
    except Exception:
        pass
    welcome = txt(uid, "main_menu_welcome")
    if not await has_access(uid):
        welcome += "\n\n" + txt(uid, "no_access")
    await bot.send_message(uid, welcome, buttons=await kb_main(uid))

@bot.on(events.CallbackQuery(pattern=rb"^status:\d+$"))
async def on_status(event):
    viewer = UID(event)
    owner = int(event.data.decode().split(":")[1])
    if owner != viewer and viewer not in ADMIN_IDS:
        await event.answer(txt(viewer, "no_permission"), alert=True)
        return
    raw = await redis.get(f"report_stats:{owner}")
    if raw:
        stats = json.loads(raw)
        await event.answer(_format_report_status(viewer, stats)[:190], alert=True)
    else:
        await event.answer(txt(viewer, "no_stats"), alert=True)

async def continue_bot_wizard(uid, ctx, event):
    if uid in ADMIN_IDS and not ctx.get("forced_pool"):
        await ask_pool_admin(event, uid)
        return

    if ctx.get("forced_pool"):
        pool_requested = ctx["forced_pool"]
        active_pool = await list_session_files(uid)
        selected = [s for s in pool_requested if s in active_pool]
        if not selected:
            await event.respond(txt(uid, "no_requested_accounts_available"))
            await ctx_pop(uid)
            return
    else:
        pool, busy = await report_pool_with_busy(uid)
        if not pool:
            await respond_no_free_accounts(event, uid, busy)
            await ctx_pop(uid)
            return
        num = await ask_num_accounts(event, uid, pool, busy)
        if num is None:
            await ctx_pop(uid)
            return
        selected = random.sample(pool, min(num, len(pool)))

    ctx["pool"] = selected
    sess0 = await resolve_probe_session(event, uid, selected)
    if not sess0:
        await ctx_pop(uid)
        return

    cli = retern_client(sess0)
    try:
        await cli.connect()
        peer = await join_group(
            cli, ctx["target"], sess0, skip_join=ctx.get("skip_join", False)
        )
        if not _is_valid_peer(peer):
            await event.respond(txt(uid, "dialog_cannot_open"))
            await ctx_pop(uid)
            return

        ctx["sample"] = {"sess": sess0}
        await ctx_set(uid, ctx)

        status, payload = await dialog_probe(cli, peer, next_opt=None, sess_id=sess0)
        if status == "choose":
            _remember_report_options(ctx, payload)
            await ctx_set(uid, ctx)
            await event.respond(
                dialog_reason_title(uid, payload),
                buttons=build_dialog_keyboard(uid, payload),
            )
        elif status == "comment":
            await ask_comment_source(
                event, uid, ctx, mode="bot", need_comment=True, sample_option=payload
            )
        elif status == "done":
            await event.edit(txt(uid, "path_complete"))
            await start_continuous_report(event, uid, ctx, "bot", need_comment=False)
        else:
            await event.respond(_format_wizard_error(uid, payload))
            await ctx_pop(uid)
    finally:
        await cli.disconnect()

async def continue_msg_wizard(uid, ctx, event):
    if _parse_target(ctx.get("target") or "")[0] == "id" and _is_private_peer_target(ctx.get("target") or ""):
        await event.respond(txt(uid, "wizard_public_not_private"))
        await ctx_pop(uid)
        return
    if uid in ADMIN_IDS and not ctx.get("forced_pool"):
        await ask_pool_admin(event, uid)
        return

    if ctx.get("forced_pool"):
        pool_requested = ctx["forced_pool"]
        active_pool = await list_session_files(uid)
        selected = [s for s in pool_requested if s in active_pool]
        if not selected:
            await event.respond(txt(uid, "no_requested_accounts_available"))
            await ctx_pop(uid)
            return
    else:
        pool, busy = await report_pool_with_busy(uid)
        if not pool:
            await respond_no_free_accounts(event, uid, busy)
            await ctx_pop(uid)
            return
        num = await ask_num_accounts(event, uid, pool, busy)
        if num is None:
            await ctx_pop(uid)
            return
        selected = random.sample(pool, min(num, len(pool)))

    ctx["pool"] = selected
    sess0 = await resolve_probe_session(event, uid, selected)
    if not sess0:
        await ctx_pop(uid)
        return
    ctx["sample"] = {"sess": sess0}
    await ctx_set(uid, ctx)
    status, payload = await sample_step_with_pool_fallback(uid, ctx, selected)
    if status == "choose":
        _remember_report_options(ctx, payload)
    await ctx_set(uid, ctx)
    if status == "choose":
        await _show_report_options(event, uid, "select_report_reason", payload)
    elif status == "comment":
        await ask_comment_source(
            event, uid, ctx, mode="msg", need_comment=True, sample_option=payload
        )
    elif status == "done":
        try:
            await event.edit(txt(uid, "path_complete"))
        except Exception:
            await event.respond(txt(uid, "path_complete"))
        await start_continuous_report(event, uid, ctx, "msg", need_comment=False)
    else:
        await event.respond(_format_wizard_error(uid, payload))
        await ctx_pop(uid)

async def continue_story_wizard(uid, ctx, event):
    if uid in ADMIN_IDS and not ctx.get("forced_pool"):
        await ask_pool_admin(event, uid)
        return

    if ctx.get("forced_pool"):
        pool_requested = ctx["forced_pool"]
        active_pool = await list_session_files(uid)
        selected = [s for s in pool_requested if s in active_pool]
        if not selected:
            await event.respond(txt(uid, "no_requested_accounts_available"))
            await ctx_pop(uid)
            return
    else:
        pool, busy = await report_pool_with_busy(uid)
        if not pool:
            await respond_no_free_accounts(event, uid, busy)
            await ctx_pop(uid)
            return
        num = await ask_num_accounts(event, uid, pool, busy)
        if num is None:
            await ctx_pop(uid)
            return
        selected = random.sample(pool, min(num, len(pool)))

    ctx["pool"] = selected
    sess0 = await resolve_probe_session(event, uid, selected)
    if not sess0:
        await ctx_pop(uid)
        return
    await event.respond(txt(uid, "story_fetching"))
    active_meta = await fetch_peer_stories_meta(sess0, ctx["target"], limit=200)
    hl_page_0, hl_next = await fetch_highlights_page(
        sess0, ctx["target"], limit=30, offset_id=0
    )
    if not active_meta and not hl_page_0:
        await event.respond(txt(uid, "no_stories"))
        await ctx_pop(uid)
        return
    ctx["stories_active_meta"] = active_meta
    ctx["stories_hl_pages"] = [{"offset": 0, "items": hl_page_0}]
    ctx["hl_next_offset"] = hl_next
    ctx["hl_current_idx"] = 0
    ctx["selected_story_ids"] = set()
    ctx["sample"] = {"sess": sess0}
    await ctx_set(uid, ctx)
    page = ctx["stories_hl_pages"][0]["items"]
    await event.respond(
        txt(uid, "story_select_prompt"),
        buttons=build_story_keyboard_sections_paged(
            uid,
            active_meta,
            page,
            ctx["selected_story_ids"],
            page_num=1,
            has_prev=False,
            has_next=(hl_next is not None),
        ),
    )

async def continue_dialog_wizard(uid, ctx, event):
    mode = ctx.get("mode") or "dialog"
    if uid in ADMIN_IDS and not ctx.get("forced_pool"):
        await ask_pool_admin(event, uid)
        return

    if ctx.get("forced_pool"):
        pool_requested = ctx["forced_pool"]
        active_pool = await list_session_files(uid)
        selected = [s for s in pool_requested if s in active_pool]
        if not selected:
            await event.respond(txt(uid, "no_requested_accounts_available"))
            await ctx_pop(uid)
            return
    else:
        pool, busy = await report_pool_with_busy(uid)
        if not pool:
            await respond_no_free_accounts(event, uid, busy)
            await ctx_pop(uid)
            return
        num = await ask_num_accounts(event, uid, pool, busy)
        if num is None:
            await ctx_pop(uid)
            return
        selected = random.sample(pool, min(num, len(pool)))

    ctx["pool"] = selected
    sess0 = await resolve_probe_session(event, uid, selected)
    if not sess0:
        await ctx_pop(uid)
        return
    cli = retern_client(sess0)
    try:
        await cli.connect()
        if mode == "profile":
            peer = await _resolve_profile_peer(cli, ctx["target"], sess0)
            if not peer:
                await event.respond(txt(uid, "profile_not_user"))
                await ctx_pop(uid)
                return
        else:
            peer = await join_group(
                cli, ctx["target"], sess0, skip_join=ctx.get("skip_join", False)
            )
            if not _is_valid_peer(peer):
                await event.respond(txt(uid, "dialog_cannot_open"))
                await ctx_pop(uid)
                return
        ctx["sample"] = {"sess": sess0}
        await ctx_set(uid, ctx)
        status, payload = await dialog_probe(
            cli,
            peer,
            next_opt=None,
            sess_id=sess0,
            prefer_peer=(mode == "profile"),
        )
        if status == "choose":
            _remember_report_options(ctx, payload)
            await ctx_set(uid, ctx)
            await event.respond(
                dialog_reason_title(uid, payload),
                buttons=build_dialog_keyboard(uid, payload),
            )
        elif status == "comment":
            await ask_comment_source(
                event, uid, ctx, mode=mode, need_comment=True, sample_option=payload
            )
        elif status == "done":
            await event.edit(txt(uid, "path_complete"))
            await start_continuous_report(event, uid, ctx, mode, need_comment=False)
        else:
            await event.respond(_format_wizard_error(uid, payload))
            await ctx_pop(uid)
    finally:
        await cli.disconnect()

async def _auto_walk_reason(
    uid: int,
    ctx: dict,
    pool: list,
    code: str,
    *,
    choose_at_level: int | None = None,
    allow_user_choice: bool = True,
):
    status, payload = await sample_step_with_pool_fallback(uid, ctx, pool)
    spec = _reason_spec(code)
    for _ in range(8):
        if status in ("comment", "done"):
            got = _reason_code_from_path(ctx.get("path") or [])
            if got != code:
                logger.error(
                    "report menu ended on a path for %r while %r was requested path=%s target=%s",
                    got,
                    code,
                    _path_keys_for_log(ctx.get("path")),
                    (ctx.get("target") or "")[:80],
                )
                return "error", f"{code.upper()}_OPTION_UNAVAILABLE"
            return status, payload
        if status != "choose":
            return status, payload
        level = len(ctx.get("path") or [])
        user_level = level == choose_at_level or spec is None or level >= len(spec.levels)
        if user_level:
            allowed = _reason_sub_options(payload, code)
            if len(allowed) == 1:
                choice = allowed[0]
            elif allowed and allow_user_choice:
                return "choose", allowed
            else:
                choice = _pick_reason_option(payload, code, level)
        else:
            choice = _pick_reason_option(payload, code, level)
        if choice is None:
            logger.warning(
                "report reason unavailable code=%s level=%s path=%s avail=%s target=%s",
                code,
                level,
                _path_keys_for_log(ctx.get("path")),
                [(_option_key(o), o.text) for o in (payload or [])],
                (ctx.get("target") or "")[:80],
            )
            return "error", f"{code.upper()}_OPTION_UNAVAILABLE"
        ctx.setdefault("path", []).append(
            _path_step_from_option(uid, choice.option, options_list=payload)
        )
        await ctx_set(uid, ctx)
        status, payload = await sample_step_with_pool_fallback(
            uid, ctx, ctx.get("pool") or pool
        )
    return "error", "MAX_STEPS"


async def _pick_post_report_pool(uid, ctx, event) -> list | None:
    if ctx.get("forced_pool"):
        pool_requested = ctx["forced_pool"]
        active_pool = await list_session_files(uid)
        selected = [s for s in pool_requested if s in active_pool]
        if not selected:
            await event.respond(txt(uid, "no_requested_accounts_available"))
            await ctx_pop(uid)
            return None
        return selected
    pool, busy = await report_pool_with_busy(uid)
    if not pool:
        await respond_no_free_accounts(event, uid, busy)
        await ctx_pop(uid)
        return None
    num = await ask_num_accounts(event, uid, pool, busy)
    if num is None:
        await ctx_pop(uid)
        return None
    return random.sample(pool, min(num, len(pool)))


async def _after_reason_walk(event, uid, ctx, mode: str, status, payload):
    if status == "choose":
        _remember_report_options(ctx, payload)
        await ctx_set(uid, ctx)
        await _show_report_options(event, uid, f"{mode}_pick_subtype", payload)
    elif status == "comment":
        await _auto_comment_then_start(event, uid, ctx, payload, mode)
    elif status == "done":
        try:
            await event.edit(txt(uid, "path_complete"))
        except Exception:
            await event.respond(txt(uid, "path_complete"))
        await start_continuous_report(event, uid, ctx, mode, need_comment=True)
    else:
        await event.respond(_format_wizard_error(uid, payload))
        await ctx_pop(uid)


async def probe_scam_path(event, uid, ctx, selected):
    ctx["pool"] = selected
    ctx["path"] = []
    sess0 = await resolve_probe_session(event, uid, selected)
    if not sess0:
        await ctx_pop(uid)
        return
    ctx["sample"] = {"sess": sess0}
    await ctx_set(uid, ctx)
    status, payload = await _auto_walk_reason(
        uid, ctx, selected, "scam", choose_at_level=1
    )
    await _after_reason_walk(event, uid, ctx, "scam", status, payload)


async def probe_fake_path(event, uid, ctx, selected):
    ctx["pool"] = selected
    ctx["path"] = []
    sess0 = await resolve_probe_session(event, uid, selected)
    if not sess0:
        await ctx_pop(uid)
        return
    ctx["sample"] = {"sess": sess0}
    await ctx_set(uid, ctx)
    status, payload = await _auto_walk_reason(uid, ctx, selected, "fake")
    await _after_reason_walk(event, uid, ctx, "fake", status, payload)


async def continue_scam_wizard(uid, ctx, event):
    if _parse_target(ctx.get("target") or "")[0] == "id" and _is_private_peer_target(ctx.get("target") or ""):
        await event.respond(txt(uid, "wizard_public_not_private"))
        await ctx_pop(uid)
        return
    if uid in ADMIN_IDS and not ctx.get("forced_pool"):
        await ask_pool_admin(event, uid)
        return
    selected = await _pick_post_report_pool(uid, ctx, event)
    if not selected:
        return
    await probe_scam_path(event, uid, ctx, selected)


async def continue_fake_wizard(uid, ctx, event):
    if _parse_target(ctx.get("target") or "")[0] == "id" and _is_private_peer_target(ctx.get("target") or ""):
        await event.respond(txt(uid, "wizard_public_not_private"))
        await ctx_pop(uid)
        return
    if uid in ADMIN_IDS and not ctx.get("forced_pool"):
        await ask_pool_admin(event, uid)
        return
    selected = await _pick_post_report_pool(uid, ctx, event)
    if not selected:
        return
    await probe_fake_path(event, uid, ctx, selected)

@bot.on(events.CallbackQuery(pattern=b"join_yes|join_no|join_cancel"))
async def on_join_choice(event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or not ctx.get("awaiting_join_choice"):
        await event.answer()
        return

    data = event.data
    if (
        data == b"join_no"
        and ctx.get("mode") in ("msg", "scam", "fake", "story")
        and _parse_target(ctx.get("target") or "")[0] == "invite"
    ):
        await event.answer(txt(uid, "join_private_required"), alert=True)
        return
    await event.answer()
    if data == b"join_cancel":
        try:
            await event.edit(txt(uid, "cancel"))
        except Exception:
            pass
        await ctx_pop(uid)
        return

    ctx["skip_join"] = data == b"join_no"
    ctx["awaiting_join_choice"] = False
    await ctx_set(uid, ctx)

    mode = ctx.get("mode")

    if mode == "bot":
        choice_text = (
            txt(uid, "choice_bot_no")
            if ctx["skip_join"]
            else txt(uid, "choice_bot_yes")
        )
    else:
        choice_text = (
            txt(uid, "choice_no") if ctx["skip_join"] else txt(uid, "choice_yes")
        )

    await event.edit(txt(uid, "you_chose", choice=choice_text))

    if mode == "msg":
        await continue_msg_wizard(uid, ctx, event)
    elif mode == "story":
        await continue_story_wizard(uid, ctx, event)
    elif mode == "dialog":
        await continue_dialog_wizard(uid, ctx, event)
    elif mode == "profile":
        await continue_dialog_wizard(uid, ctx, event)
    elif mode == "scam":
        await continue_scam_wizard(uid, ctx, event)
    elif mode == "fake":
        await continue_fake_wizard(uid, ctx, event)
    elif mode == "bot":
        await continue_bot_wizard(uid, ctx, event)

@bot.on(events.CallbackQuery(pattern=rb"jrcheck:\d+"))
async def on_jr_check(event):
    uid = int(event.data.decode().split(":")[1])
    if UID(event) != uid:
        return
    await event.answer(txt(uid, "jr_checking"))
    req = db["join_requests"].find_one({"user_id": uid}, sort=[("requested_at", -1)])
    if not req:
        await event.answer(txt(uid, "invalid_input"), alert=True)
        return
    now_ts = _now_ts()
    for acc in req.get("accounts", []):
        if acc.get("status") not in ("pending", "failed", "rejected"):
            continue
        if (
            acc.get("status") == "rejected"
            and now_ts - acc.get("checked_at", now_ts) > 86400
        ):
            continue
        cli = retern_client(acc["sess"])
        if not cli:
            continue
        try:
            await cli.connect()
            if not await cli.is_user_authorized():
                continue
            res = await join_group_with_status(cli, req["target"], acc["sess"])
            if res["status"] in ("joined", "already"):
                acc["status"] = "joined"
            elif res["status"] == "pending":
                acc["status"] = "pending"
            elif res["status"] == "failed":
                if now_ts - acc.get("checked_at", now_ts) > 86400:
                    acc["status"] = "rejected"
                else:
                    acc["status"] = "pending"
            acc["checked_at"] = now_ts
        except Exception:
            pass
        finally:
            try:
                await cli.disconnect()
            except Exception:
                pass
    db["join_requests"].update_one(
        {"_id": req["_id"]}, {"$set": {"accounts": req["accounts"]}}
    )
    joined = sum(1 for a in req["accounts"] if a["status"] in ("joined", "already"))
    pending = sum(1 for a in req["accounts"] if a["status"] == "pending")
    failed = sum(1 for a in req["accounts"] if a["status"] in ("failed", "rejected"))
    logger.warning(
        f"[join_req] manual check for {uid}: joined={joined} pending={pending} failed={failed}"
    )
    await event.edit(
        txt(uid, "join_req_stats", joined=joined, pending=pending, failed=failed),
        buttons=[
            [
                Button.inline(
                    txt(uid, "join_req_check_btn"), data=f"jrcheck:{uid}".encode()
                )
            ]
        ],
    )

@bot.on(events.CallbackQuery(pattern=rb"jrchoice:(continue|wait|cancel)"))
async def on_jr_choice(event):
    await event.answer()
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or "jr_joined_pool" not in ctx:
        return
    action = event.data.decode().split(":")[1]
    if action == "cancel":
        await event.edit(txt(uid, "cancel"))
        await ctx_pop(uid)
        return
    if action == "continue":
        joined = ctx.get("jr_joined_pool") or []
        if not joined:
            await event.answer(txt(uid, "jr_all_pending"), alert=True)
            return
        ctx["pool"] = joined
        ctx["jr_confirmed"] = True
        need_comment = ctx.pop("jr_need_comment", False)
        for k in ("jr_joined_pool", "jr_pending_pool", "jr_wait_count"):
            ctx.pop(k, None)
        await ctx_set(uid, ctx)
        await event.edit(txt(uid, "received"))
        await start_continuous_report(event, uid, ctx, ctx["mode"], need_comment)
        return
    wait_count = ctx.get("jr_wait_count", 0) + 1
    ctx["jr_wait_count"] = wait_count
    await ctx_set(uid, ctx)
    await event.respond(txt(uid, "jr_checking"))
    await asyncio.sleep(60)
    fresh_ctx = await ctx_get(uid)
    if not fresh_ctx or "jr_joined_pool" not in fresh_ctx:
        return
    ctx = fresh_ctx
    pending = ctx.get("jr_pending_pool") or []
    if pending:
        stats = await preflight_join_check(uid, pending, ctx["target"])
        newly, retryable, skipped, _reasons = _preflight_keep_accounts(stats)
        still_pending = [
            s
            for s, st in stats.items()
            if not str(s).startswith("__") and st == "pending"
        ]
        if newly or retryable or skipped or still_pending != pending:
            ctx["jr_joined_pool"] = list(
                dict.fromkeys((ctx.get("jr_joined_pool") or []) + newly + retryable)
            )
            ctx["jr_pending_pool"] = still_pending
            await ctx_set(uid, ctx)
    joined = ctx.get("jr_joined_pool") or []
    if not pending:
        if joined:
            await event.edit(
                txt(uid, "jr_all_approved_now"),
                buttons=[
                    [
                        Button.inline(
                            txt(uid, "jr_btn_continue", count=len(joined)),
                            data=b"jrchoice:continue",
                        )
                    ],
                    [Button.inline(txt(uid, "cancel_btn"), data=b"jrchoice:cancel")],
                ],
            )
        else:
            await event.respond(txt(uid, "jr_all_failed"))
            await ctx_pop(uid)
        return
    if wait_count >= 3:
        await event.edit(
            txt(uid, "jr_wait_limit", joined=len(joined), pending=len(pending)),
            buttons=[
                [
                    Button.inline(
                        txt(uid, "jr_btn_continue", count=len(joined)),
                        data=b"jrchoice:continue",
                    )
                ],
                [Button.inline(txt(uid, "cancel_btn"), data=b"jrchoice:cancel")],
            ],
        )
        return
    await event.edit(
        txt(uid, "jr_partial", joined=len(joined), pending=len(pending), failed=0),
        buttons=[
            [
                Button.inline(
                    txt(uid, "jr_btn_continue", count=len(joined)),
                    data=b"jrchoice:continue",
                )
            ],
            [
                Button.inline(txt(uid, "jr_btn_wait"), data=b"jrchoice:wait"),
                Button.inline(txt(uid, "cancel_btn"), data=b"jrchoice:cancel"),
            ],
        ],
    )

@bot.on(events.CallbackQuery(pattern=rb"stop:\d+"))
async def on_stop(event):
    uid = int(event.data.decode().split(":")[1])
    if UID(event) != uid and UID(event) not in ADMIN_IDS:
        return
    await event.answer(txt(uid, "stopping"))
    running = await is_report_really_running(uid)
    await set_stop_flag(uid)
    if not running:
        await clear_report_runtime(uid, clear_stop=False)
    try:
        await event.edit(txt(uid, "report_stopping"))
    except Exception:
        try:
            await event.respond(txt(uid, "report_stopping"))
        except Exception as e:
            logger.warning("stop reply failed uid=%s: %s", uid, e)

async def show_report_manage(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await show_main_menu(event, uid)
        return
    running = await list_running_reports()
    if not running:
        await event.reply(txt(uid, "report_manage_idle"))
        return
    lines = [txt(uid, "report_manage_head")]
    rows = []
    for item in running[:15]:
        kind = _REPORT_KIND_FA.get(item["mode"], item["mode"] or "—")
        lines.append(
            txt(
                uid,
                "report_manage_row",
                user=item["uid"],
                active=item["active"],
                total=item["total"],
                kind=kind,
                target=(item["target"] or "—")[:40],
            )
        )
        row = [
            Button.inline(
                txt(uid, "stop_button"),
                data=f"stop:{item['uid']}".encode(),
            )
        ]
        if uid in ADMIN_IDS:
            row.insert(
                0,
                Button.inline(
                    f"🔓 آزاد {item['uid']}",
                    data=f"rfree:{item['uid']}".encode(),
                ),
            )
        rows.append(row)
    await event.reply("\n".join(lines), buttons=rows)

@bot.on(events.CallbackQuery(pattern=rb"^rfree:\d+$"))
async def on_release_accounts(event):
    admin = UID(event)
    if admin not in ADMIN_IDS:
        return
    owner = int(event.data.decode().split(":")[1])
    await event.answer()
    try:
        async with standard_conversation(admin, timeout=90) as conv:
            await conv.send_message(
                txt(admin, "report_release_ask", user=owner),
                buttons=conv_cancel_buttons(admin),
            )
            raw = ((await menu_safe_response(conv)).raw_text or "").strip()
            if not raw.isdigit() or int(raw) < 1:
                await conv.send_message(txt(admin, "invalid_number"))
                return
            freed = await release_report_accounts(owner, int(raw))
            await conv.send_message(txt(admin, "report_release_done", count=freed, user=owner))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            await bot.send_message(admin, txt(admin, "timeout_or_invalid"))
        except Exception:
            pass

@bot.on(events.CallbackQuery(pattern=b"pool_cancel"))
async def on_pool_cancel(event):
    uid = UID(event)
    try:
        await event.answer()
    except Exception:
        pass
    await ctx_pop(uid)
    await event.edit(
        txt(uid, "cancel"),
        buttons=[[Button.inline(txt(uid, "back_btn"), data=b"start_menu")]],
    )

@bot.on(events.CallbackQuery(pattern=b"pool_mine|pool_all"))
async def on_admin_pool_choice(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    ctx = await ctx_get(uid)
    if not ctx:
        await event.answer(txt(uid, "admin_session_expired"), alert=True)
        return
    try:
        await event.answer()
    except Exception:
        pass
    choice = event.data.decode()
    if choice == "pool_mine":
        all_pool = [str(a["_id"]) for a in db["accounts"].find({"admin_id": uid})]
    else:
        all_pool = [str(a["_id"]) for a in db["accounts"].find()]
    pool = await _filter_unlocked_sessions(all_pool, uid)
    busy = len(all_pool) - len(pool)
    ctx["pool"] = pool
    await ctx_set(uid, ctx)
    await event.edit(txt(uid, "received"))
    num = await ask_num_accounts(event, uid, pool, busy)
    if num is None:
        await ctx_pop(uid)
        return
    selected_pool = random.sample(pool, min(num, len(pool)))
    ctx["pool"] = selected_pool
    if ctx["mode"] == "msg":
        sess0 = await resolve_probe_session(event, uid, selected_pool)
        if not sess0:
            await ctx_pop(uid)
            return
        ctx["sample"] = {"sess": sess0}
        await ctx_set(uid, ctx)
        status, payload = await sample_step_with_pool_fallback(uid, ctx, selected_pool)
        if status == "choose":
            _remember_report_options(ctx, payload)
            await ctx_set(uid, ctx)
            await event.respond(
                txt(uid, "select_report_reason"),
                buttons=build_option_keyboard(uid, payload),
            )
        elif status == "comment":
            await ask_comment_source(
                event, uid, ctx, mode=ctx["mode"], need_comment=True, sample_option=payload
            )
        elif status == "done":
            await event.edit(txt(uid, "path_complete_start"))
            await start_continuous_report(
                event, uid, ctx, ctx["mode"], need_comment=False
            )
        else:
            await event.respond(_format_wizard_error(uid, payload))
            await ctx_pop(uid)
    elif ctx["mode"] == "story":
        sess0 = await resolve_probe_session(event, uid, selected_pool)
        if not sess0:
            await ctx_pop(uid)
            return
        ctx["sample"] = {"sess": sess0}
        await ctx_set(uid, ctx)
        await event.respond(txt(uid, "story_fetching"))
        active_meta = await fetch_peer_stories_meta(sess0, ctx["target"], limit=200)
        hl_page_0, hl_next = await fetch_highlights_page(
            sess0, ctx["target"], limit=30, offset_id=0
        )
        if not active_meta and not hl_page_0:
            await event.respond(txt(uid, "no_stories"))
            await ctx_pop(uid)
            return
        ctx["stories_active_meta"] = active_meta
        ctx["stories_hl_pages"] = [{"offset": 0, "items": hl_page_0}]
        ctx["hl_next_offset"] = hl_next
        ctx["hl_current_idx"] = 0
        ctx["selected_story_ids"] = set()
        await ctx_set(uid, ctx)
        page = ctx["stories_hl_pages"][0]["items"]
        await event.respond(
            txt(uid, "story_select_prompt"),
            buttons=build_story_keyboard_sections_paged(
                uid,
                active_meta,
                page,
                ctx["selected_story_ids"],
                page_num=1,
                has_prev=False,
                has_next=(hl_next is not None),
            ),
        )
        return
    elif ctx["mode"] == "scam":
        await probe_scam_path(event, uid, ctx, selected_pool)
    elif ctx["mode"] == "fake":
        await probe_fake_path(event, uid, ctx, selected_pool)

    else:
        mode = ctx.get("mode") or "dialog"
        sess0 = await resolve_probe_session(event, uid, selected_pool)
        if not sess0:
            await ctx_pop(uid)
            return
        cli = retern_client(sess0)
        try:
            await cli.connect()
            if mode == "profile":
                peer = await _resolve_profile_peer(cli, ctx["target"], sess0)
                if not peer:
                    await event.respond(txt(uid, "profile_not_user"))
                    await ctx_pop(uid)
                    return
            else:
                peer = await join_group(
                    cli, ctx["target"], sess0, skip_join=ctx.get("skip_join", False)
                )
                if not _is_valid_peer(peer):
                    await event.respond(txt(uid, "dialog_cannot_open"))
                    await ctx_pop(uid)
                    return
            ctx["sample"] = {"sess": sess0}
            await ctx_set(uid, ctx)
            status, payload = await dialog_probe(
                cli,
                peer,
                next_opt=None,
                sess_id=sess0,
                prefer_peer=(mode == "profile"),
            )
            if status == "choose":
                _remember_report_options(ctx, payload)
                await ctx_set(uid, ctx)
                await event.respond(
                    dialog_reason_title(uid, payload),
                    buttons=build_dialog_keyboard(uid, payload),
                )
            elif status == "comment":
                await ask_comment_source(
                    event,
                    uid,
                    ctx,
                    mode=ctx.get("mode") or "dialog",
                    need_comment=True,
                    sample_option=payload,
                )
            elif status == "done":
                try:
                    await event.edit(txt(uid, "path_complete_start"))
                except Exception:
                    await event.respond(txt(uid, "path_complete_start"))
                await start_continuous_report(
                    event, uid, ctx, mode, need_comment=True
                )
            else:
                await event.respond(_format_wizard_error(uid, payload))
                await ctx_pop(uid)
        finally:
            await cli.disconnect()

_DEST_ENTITY = {
    "pubch": "channel",
    "prvch": "channel",
    "pubgrp": "chat",
    "prvgrp": "chat",
}
_DEST_LABEL = {
    "pubch": "dest_pub_channel",
    "prvch": "dest_prv_channel",
    "pubgrp": "dest_pub_group",
    "prvgrp": "dest_prv_group",
}

_DEST_POST_PROMPT = {
    "pubch": "wizard_pub_channel_prompt",
    "pubgrp": "wizard_pub_group_prompt",
    "prvch": "wizard_prv_channel_post_prompt",
    "prvgrp": "wizard_prv_group_post_prompt",
}
_DEST_INVITE_PROMPT = {
    "prvch": "wizard_prv_channel_invite_prompt",
    "prvgrp": "wizard_prv_group_invite_prompt",
}
_DEST_TEXT_TO_CODE = {}
for _code, _key in _DEST_LABEL.items():
    for _lang in ("fa", "ar", "en"):
        _label = (TRANSLATIONS.get(_key) or {}).get(_lang) or ""
        if _label.strip():
            _DEST_TEXT_TO_CODE[_label.strip()] = _code
            _DEST_TEXT_TO_CODE[normalize_menu_text(_label)] = _code

def _dest_kind_buttons(uid: int):
    return [
        [
            Button.text(txt(uid, "dest_pub_channel"), resize=True),
            Button.text(txt(uid, "dest_prv_channel"), resize=True),
        ],
        [
            Button.text(txt(uid, "dest_pub_group"), resize=True),
            Button.text(txt(uid, "dest_prv_group"), resize=True),
        ],
        [Button.text(txt(uid, "back_btn"), resize=True)],
    ]

def _dest_code_from_text(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    return _DEST_TEXT_TO_CODE.get(raw) or _DEST_TEXT_TO_CODE.get(normalize_menu_text(raw))

def _is_private_peer_target(target: str) -> bool:
    kind, token = _parse_target(target or "")
    if kind != "id" or not token:
        return False
    text = str(token)
    return text.startswith("-100") or (text.lstrip("-").isdigit() and len(text) >= 8)

def _forced_join_pool(uid: int, targets: list[str]):
    ors = [{"target": t} for t in targets if t]
    if not ors:
        return None, None
    req = db["join_requests"].find_one({"user_id": uid, "$or": ors})
    if not req:
        return None, None
    when = req.get("requested_at")
    req_time = when.strftime("%Y-%m-%d %H:%M") if when else ""
    raw_accs = req.get("accounts") or []
    if raw_accs and isinstance(raw_accs[0], dict):
        pool = [
            a["sess"]
            for a in raw_accs
            if a.get("status") in ("joined", "already") and a.get("sess")
        ]
    else:
        pool = raw_accs
    return req_time, pool or None

async def _read_post_target(conv, uid: int, prompt_key: str):
    await conv.send_message(txt(uid, prompt_key), buttons=conv_cancel_buttons(uid))
    raw = (await menu_safe_response(conv)).raw_text.strip()
    target, msg_ids = resolve_target_and_msg_ids(raw)
    if _parse_target(target)[0] == "unknown":
        await conv.send_message(txt(uid, "invalid_target"))
        return None
    if not msg_ids:
        await conv.send_message(
            txt(uid, "wizard_msg_ids_prompt"), buttons=conv_cancel_buttons(uid)
        )
        ids_raw = (await menu_safe_response(conv)).raw_text.strip()
        target2, msg_ids = resolve_target_and_msg_ids(target, ids_raw)
        if _parse_target(target2)[0] != "unknown":
            target = target2
        if not msg_ids:
            msg_ids = parse_ids(ids_raw)
        if not msg_ids:
            await conv.send_message(txt(uid, "wizard_invalid_msg_ids"))
            return None
    return raw, target, msg_ids

_DEST_INTRO = {"scam": "report_scam_intro", "fake": "report_fake_intro"}

async def _start_dest_kind_wizard(event, mode: str):
    uid = UID(event)
    if not await has_access(uid):
        return
    if not await ensure_report_channel_member(event, uid, resume=mode):
        return
    await ctx_pop(uid)
    await ctx_set(uid, {"mode": mode, "awaiting_dest_kind": True})
    text = txt(uid, "report_dest_ask")
    if mode in _DEST_INTRO:
        text = f"{txt(uid, _DEST_INTRO[mode])}\n\n{text}"
    await event.respond(text, buttons=_dest_kind_buttons(uid))

async def wizard_msg(event: events.NewMessage.Event):
    await _start_dest_kind_wizard(event, "msg")

async def wizard_scam(event: events.NewMessage.Event):
    await _start_dest_kind_wizard(event, "scam")

async def wizard_fake(event: events.NewMessage.Event):
    await _start_dest_kind_wizard(event, "fake")

async def _collect_post_report(event, uid: int, mode: str, code: str):
    private = str(code).startswith("prv")
    entity_kind = _DEST_ENTITY.get(code, "channel")
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            prompt = _DEST_POST_PROMPT.get(code) or (
                "wizard_private_post_prompt" if private else "wizard_msg_target_prompt"
            )
            parsed = await _read_post_target(conv, uid, prompt)
            if not parsed:
                await ctx_pop(uid)
                return
            target_raw, target, msg_ids = parsed
            invite = ""
            if private:
                await conv.send_message(
                    txt(uid, _DEST_INVITE_PROMPT.get(code, "wizard_private_invite_prompt")),
                    buttons=conv_cancel_buttons(uid),
                )
                invite_raw = (await menu_safe_response(conv)).raw_text.strip()
                kind, token = _parse_target(invite_raw)
                if kind != "invite" or not token:
                    await conv.send_message(txt(uid, "wizard_private_invite_bad"))
                    await ctx_pop(uid)
                    return
                invite = f"https://t.me/+{token}"
                join_target = invite
            else:
                if _is_private_peer_target(target):
                    await conv.send_message(txt(uid, "wizard_public_not_private"))
                    await ctx_pop(uid)
                    return
                join_target = target

            shown = invite or target
            try:
                key = "wizard_private_ready" if private else "wizard_msg_ids_detected"
                await conv.send_message(
                    txt(
                        uid,
                        key,
                        ids=", ".join(map(str, msg_ids)),
                        target=shown,
                        invite=shown,
                    )
                )
            except Exception:
                pass

            req_time, forced_pool = _forced_join_pool(
                uid, [join_target, target, target_raw, invite]
            )
            if req_time:
                await conv.send_message(txt(uid, "join_already_requested", time=req_time))

            if not private:
                probed = await _probe_target_kind(target)
                if probed in ("user", "bot"):
                    await conv.send_message(txt(uid, "target_is_user_hint"))
                    await ctx_pop(uid)
                    return
                if probed in ("channel", "chat"):
                    entity_kind = probed

            ctx = {
                "mode": mode,
                "target": join_target,
                "msg_ids": msg_ids,
                "path": [],
                "awaiting_comment": False,
                "add_comment_option": None,
                "comment": "",
                "pool": [],
                "forced_pool": forced_pool,
                "sample": {},
                "selected_story_ids": set(),
                "entity_kind": entity_kind,
                "skip_join": False,
                "awaiting_join_choice": True,
                "awaiting_dest_kind": False,
            }
            await ctx_set(uid, ctx)
            await conv.send_message(
                txt(uid, "join_choice_target", target=shown),
                buttons=join_choice_buttons(uid),
            )
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            await ctx_pop(uid)
            await bot.send_message(uid, txt(uid, "timeout_or_invalid"))
        except Exception:
            pass

@bot.on(events.CallbackQuery(pattern=rb"^destkind:(pubch|prvch|pubgrp|prvgrp)$"))
async def on_report_dest_kind(event):
    uid = UID(event)
    ctx = await ctx_get(uid) or {}
    if not ctx.get("awaiting_dest_kind"):
        await event.answer()
        return
    mode = ctx.get("mode") or "msg"
    if mode not in ("msg", "scam", "fake"):
        await event.answer()
        return
    code = event.data.decode().split(":", 1)[1]
    await event.answer()
    try:
        await event.edit(txt(uid, _DEST_LABEL.get(code, "report_dest_ask")))
    except Exception:
        pass
    await _collect_post_report(event, uid, mode, code)

@bot.on(events.NewMessage(func=lambda e: bool(getattr(e, "text", None))))
async def on_dest_kind_text(event):
    if getattr(event, "out", False):
        return
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or not ctx.get("awaiting_dest_kind"):
        return
    raw = (event.raw_text or "").strip()
    code = _dest_code_from_text(raw)
    if not code:
        if "t.me/" in raw.lower() or raw.startswith("@"):
            await event.respond(
                txt(uid, "report_dest_first"), buttons=_dest_kind_buttons(uid)
            )
            raise events.StopPropagation
        return
    mode = ctx.get("mode") or "msg"
    if mode not in ("msg", "scam", "fake"):
        return
    ctx["awaiting_dest_kind"] = False
    await ctx_set(uid, ctx)
    await event.respond(txt(uid, _DEST_LABEL.get(code, "report_dest_ask")))
    await _collect_post_report(event, uid, mode, code)
    raise events.StopPropagation


async def wizard_story(event: events.NewMessage.Event):
    uid = UID(event)
    if not await has_access(uid):
        return
    if not await ensure_report_channel_member(event, uid, resume="story"):
        return
    await ctx_pop(uid)
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            await conv.send_message(
                txt(uid, "wizard_story_target_prompt"), buttons=conv_cancel_buttons(uid)
            )
            target = normalize_chat_target(
                (await menu_safe_response(conv)).raw_text.strip()
            )
            if _parse_target(target)[0] == "unknown":
                await conv.send_message(txt(uid, "invalid_target"))
                return

            forced_pool = None
            req = db["join_requests"].find_one({"user_id": uid, "target": target})
            if req:
                req_time = req["requested_at"].strftime("%Y-%m-%d %H:%M")
                await conv.send_message(
                    txt(uid, "join_already_requested", time=req_time)
                )
                _raw_accs = req.get("accounts", [])
                if _raw_accs and isinstance(_raw_accs[0], dict):
                    forced_pool = [
                        a["sess"]
                        for a in _raw_accs
                        if a.get("status") in ("joined", "already")
                    ]
                else:
                    forced_pool = _raw_accs

            ctx = {
                "mode": "story",
                "target": target,
                "stories_active_meta": [],
                "hl_page_size": 30,
                "stories_hl_pages": [],
                "hl_current_idx": 0,
                "hl_next_offset": None,
                "selected_story_ids": set(),
                "path": [],
                "awaiting_comment": False,
                "add_comment_option": None,
                "comment": "",
                "pool": [],
                "forced_pool": forced_pool,
                "sample": {},
                "skip_join": False,
                "awaiting_join_choice": True,
            }
            await ctx_set(uid, ctx)
            await conv.send_message(
                txt(uid, "join_choice_target", target=target),
                buttons=join_choice_buttons(uid),
            )
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

async def wizard_bot_dialog(event: events.NewMessage.Event):
    uid = UID(event)
    if not await has_access(uid):
        return
    if not await ensure_report_channel_member(event, uid, resume="bot"):
        return
    await ctx_pop(uid)
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            await conv.send_message(
                txt(uid, "wizard_dialog_target_prompt"),
                buttons=conv_cancel_buttons(uid),
            )
            target = normalize_chat_target(
                (await menu_safe_response(conv)).raw_text.strip()
            )
            if _parse_target(target)[0] == "unknown":
                await conv.send_message(txt(uid, "invalid_target"))
                return

            forced_pool = None
            req = db["join_requests"].find_one({"user_id": uid, "target": target})
            if req:
                req_time = req["requested_at"].strftime("%Y-%m-%d %H:%M")
                await conv.send_message(
                    txt(uid, "join_already_requested", time=req_time)
                )
                _raw_accs = req.get("accounts", [])
                if _raw_accs and isinstance(_raw_accs[0], dict):
                    forced_pool = [
                        a["sess"]
                        for a in _raw_accs
                        if a.get("status") in ("joined", "already")
                    ]
                else:
                    forced_pool = _raw_accs

            ctx = {
                "mode": "dialog",
                "target": target,
                "path": [],
                "awaiting_comment": False,
                "add_comment_option": None,
                "comment": "",
                "pool": [],
                "forced_pool": forced_pool,
                "sample": {},
                "awaiting_join_choice": True,
            }
            await ctx_set(uid, ctx)

            await conv.send_message(
                txt(uid, "join_prompt_bot"),
                buttons=[
                    [Button.inline(txt(uid, "choice_bot_yes"), b"join_yes")],
                    [Button.inline(txt(uid, "choice_bot_no"), b"join_no")],
                    [Button.inline(txt(uid, "cancel_choice"), b"join_cancel")],
                ],
            )

    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

async def wizard_profile(event: events.NewMessage.Event):
    uid = UID(event)
    if not await has_access(uid):
        return
    if not await ensure_report_channel_member(event, uid, resume="profile"):
        return
    await ctx_pop(uid)
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            await conv.send_message(
                txt(uid, "wizard_profile_target_prompt"),
                buttons=conv_cancel_buttons(uid),
            )
            raw = (await menu_safe_response(conv)).raw_text.strip()
            target = normalize_chat_target(raw)
            kind, token = _parse_target(target)
            if kind == "unknown" or kind == "invite" or not token:
                await conv.send_message(txt(uid, "profile_not_user"))
                return
            if kind == "username":
                target = f"@{token}"
            elif kind == "id":
                target = str(token)

            entity_kind = await _probe_target_kind(target)
            if entity_kind == "invite":
                await conv.send_message(txt(uid, "profile_not_user"))
                return
            if entity_kind in ("channel", "chat"):
                try:
                    await conv.send_message(
                        txt(uid, "profile_channel_note", target=target)
                    )
                except Exception:
                    pass

            ctx = {
                "mode": "profile",
                "target": target,
                "entity_kind": entity_kind,
                "path": [],
                "awaiting_comment": False,
                "add_comment_option": None,
                "comment": "",
                "pool": [],
                "forced_pool": None,
                "sample": {},
                "skip_join": True,
                "awaiting_join_choice": False,
            }
            await ctx_set(uid, ctx)

        ctx = await ctx_get(uid)
        if ctx and ctx.get("mode") == "profile":
            await continue_dialog_wizard(uid, ctx, event)

    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

SEND_PV_CONCURRENCY = int(os.getenv("SEND_PV_CONCURRENCY", "5"))
SEND_PV_DELAY_MIN = float(os.getenv("SEND_PV_DELAY_MIN", "0.8"))
SEND_PV_DELAY_MAX = float(os.getenv("SEND_PV_DELAY_MAX", "2.0"))
SEND_PV_TARGETS = ["notoscam", "AbuseNotifications"]

async def _send_pv_one(sess: str, uid: int, text: str) -> tuple[int, int, dict]:
    cli = retern_client(sess)
    if not cli:
        return 0, len(SEND_PV_TARGETS), {"bad_session": len(SEND_PV_TARGETS)}
    ok = 0
    failed = 0
    reasons: dict[str, int] = {}
    await lock_account(sess, uid)
    try:
        await cli.connect()
        if not await cli.is_user_authorized():
            return 0, len(SEND_PV_TARGETS), {"unauthorized": len(SEND_PV_TARGETS)}
        for tgt in SEND_PV_TARGETS:
            try:
                entity = await _resolve_username_entity(cli, tgt)
                await cli.send_message(entity, text)
                ok += 1
            except errors.FloodWaitError as e:
                failed += 1
                reasons[f"FLOODWAIT_{e.seconds}s"] = reasons.get(f"FLOODWAIT_{e.seconds}s", 0) + 1
            except errors.RPCError as e:
                failed += 1
                key = _rpc_err_info(e).split(":", 1)[0][:40]
                reasons[key] = reasons.get(key, 0) + 1
            except Exception as e:
                failed += 1
                key = f"EXC_{e.__class__.__name__}"
                reasons[key] = reasons.get(key, 0) + 1
            await asyncio.sleep(random.uniform(SEND_PV_DELAY_MIN, SEND_PV_DELAY_MAX))
        return ok, failed, reasons
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        return 0, len(SEND_PV_TARGETS), {f"CONN_{e.__class__.__name__}": len(SEND_PV_TARGETS)}
    except Exception as e:
        return 0, len(SEND_PV_TARGETS), {f"EXC_{e.__class__.__name__}": len(SEND_PV_TARGETS)}
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass
        await unlock_account(sess)

async def run_send_pv(event, uid: int, text: str, pool: list[str]):
    total_sends = len(pool) * len(SEND_PV_TARGETS)
    stats = {"ok": 0, "failed": 0, "done": 0, "total": total_sends}
    reasons: dict[str, int] = {}
    progress = await event.respond(
        txt(uid, "send_pv_started", accounts=len(pool))
    )
    sem = asyncio.Semaphore(max(1, min(SEND_PV_CONCURRENCY, len(pool))))
    lock = asyncio.Lock()
    last_edit = {"at": 0.0}

    async def _edit(force: bool = False):
        now = _time.monotonic()
        if not force and now - last_edit["at"] < 5:
            return
        last_edit["at"] = now
        try:
            await progress.edit(txt(uid, "send_pv_progress", **stats))
        except Exception:
            pass

    async def worker(sess: str):
        async with sem:
            ok, failed, reas = await _send_pv_one(sess, uid, text)
            async with lock:
                stats["ok"] += ok
                stats["failed"] += failed
                stats["done"] += ok + failed
                for k, v in reas.items():
                    reasons[k] = reasons.get(k, 0) + v
                if failed:
                    logger.warning(
                        "send_pv phone=%s ok=%s failed=%s reasons=%s",
                        sess_phone(sess),
                        ok,
                        failed,
                        reas,
                    )
            await _edit()

    await asyncio.gather(*(worker(s) for s in pool), return_exceptions=True)
    await _edit(force=True)
    summary = txt(uid, "send_pv_done", **stats)
    if reasons:
        summary += "\n" + ", ".join(f"{k}×{v}" for k, v in reasons.items())
    await event.respond(summary)

async def wizard_send_pv(event: events.NewMessage.Event):
    uid = UID(event)
    if not await has_access(uid):
        return
    await ctx_pop(uid)
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            await conv.send_message(
                txt(uid, "send_pv_text_prompt"), buttons=conv_cancel_buttons(uid)
            )
            text = ((await menu_safe_response(conv)).raw_text or "").strip()
            if not text:
                await conv.send_message(txt(uid, "invalid_input"))
                return
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            await bot.send_message(uid, txt(uid, "timeout_or_invalid"))
        except Exception:
            pass
        return

    pool, busy = await report_pool_with_busy(uid)
    if not pool:
        await respond_no_free_accounts(event, uid, busy)
        return
    num = await ask_num_accounts(event, uid, pool, busy)
    if not num:
        return
    selected = random.sample(pool, min(num, len(pool)))
    await run_send_pv(event, uid, text, selected)

AI_ANALYZE_LIMIT = int(os.getenv("AI_ANALYZE_LIMIT", "60"))

_AI_REASONS = REPORT_REASON_CODES
_AI_MODE_FOR_REASON = {"scam": "scam", "fake": "fake"}

async def _openai_chat_raw(
    settings: dict, system: str, user_prompt: str, *, max_tokens: int = 800
) -> str:
    return await _openai_chat(
        settings,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.2,
    )

async def _ai_analyze_channel(target: str, messages: list[tuple[int, str]]) -> list[dict]:
    settings = get_ai_settings()
    if not settings.get("enabled") or not (settings.get("api_key") or "").strip():
        raise ValueError("AI_NOT_CONFIGURED")
    lines = []
    for mid, text in messages:
        snippet = re.sub(r"\s+", " ", (text or "")).strip()[:300]
        if snippet:
            lines.append(f"[{mid}] {snippet}")
    if not lines:
        return []
    system = (
        "You are a strict Telegram content moderator. You detect posts that violate "
        "Telegram Terms of Service and are reportable. Output JSON only."
    )
    user_prompt = (
        "Analyze the following channel/group posts. For EACH post that is reportable "
        "(scam/fraud, impersonation/fake account, spam, violence/terror, pornography, "
        "child abuse, illegal drugs, copyright, or leaking personal data), include it in "
        "the output. Pick ONE reason from exactly this set: "
        "scam (fraud, phishing, fraudulent seller or service, fake financial promises), "
        "fake (impersonating another person, brand or channel), spam, violence, porn, "
        "child, drugs, personal, copyright, other. "
        "Give a short 'why' in Persian (max 12 words). "
        'Return ONLY a JSON array like: '
        '[{"id":123,"reason":"scam","why":"..."}]. '
        "If nothing is reportable, return []. Do not include safe posts.\n\n"
        "Posts:\n" + "\n".join(lines)
    )
    raw = await _openai_chat_raw(settings, system, user_prompt, max_tokens=1200)
    m = re.search(r"\[.*\]", raw, flags=re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    valid_ids = {mid for mid, _ in messages}
    out = []
    seen = set()
    for it in arr:
        if not isinstance(it, dict):
            continue
        try:
            mid = int(it.get("id"))
        except Exception:
            continue
        if mid not in valid_ids or mid in seen:
            continue
        reason = str(it.get("reason") or "other").strip().lower()
        if reason not in _AI_REASONS:
            reason = "other"
        why = re.sub(r"\s+", " ", str(it.get("why") or "")).strip()[:120]
        out.append({"id": mid, "reason": reason, "why": why})
        seen.add(mid)
    return out

async def _fetch_channel_messages(
    sess: str, target: str, limit: int
) -> tuple[object, str | None, list[tuple[int, str]]]:
    cli = retern_client(sess)
    if not cli:
        return None, None, []
    try:
        await asyncio.wait_for(cli.connect(), timeout=25)
        if not await cli.is_user_authorized():
            return None, None, []
        peer = await join_group(cli, target, sess, skip_join=True)
        if not _is_valid_peer(peer):
            peer = await _resolve_profile_peer(cli, target, sess)
        if not _is_valid_peer(peer):
            return None, None, []
        username = None
        kind, token = _parse_target(target)
        if kind == "username" and token:
            username = token
        msgs = await cli.get_messages(peer, limit=limit)
        out = []
        for m in msgs or []:
            mid = getattr(m, "id", None)
            body = getattr(m, "message", None) or ""
            if isinstance(mid, int) and mid > 0 and body.strip():
                out.append((mid, body))
        if out:
            await _mark_channel_posts_seen(cli, peer, [mid for mid, _ in out])
        return peer, username, out
    except Exception as e:
        logger.warning("ai analyze fetch fail target=%s: %s", target, e)
        return None, None, []
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass

def _build_post_link(target: str, username: str | None, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    kind, token = _parse_target(target)
    if kind == "id" and token:
        tok = str(token)
        if tok.startswith("-100"):
            tok = tok[4:]
        elif tok.startswith("-"):
            tok = tok[1:]
        return f"https://t.me/c/{tok}/{msg_id}"
    return f"{target} (#{msg_id})"

async def wizard_ai_analyze(event: events.NewMessage.Event):
    uid = UID(event)
    if not await has_access(uid):
        return
    if not await ensure_report_channel_member(event, uid, resume="ai"):
        return
    settings = get_ai_settings()
    if not settings.get("enabled") or not (settings.get("api_key") or "").strip():
        await event.respond(txt(uid, "ai_analyze_not_configured"))
        return
    await ctx_pop(uid)
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            await conv.send_message(
                txt(uid, "ai_analyze_target_prompt"), buttons=conv_cancel_buttons(uid)
            )
            raw = (await menu_safe_response(conv)).raw_text.strip()
            target = normalize_chat_target(raw)
            kind, token = _parse_target(target)
            if kind not in ("username", "id") or not token:
                await conv.send_message(txt(uid, "ai_analyze_invalid_target"))
                return
            target = f"@{token}" if kind == "username" else str(token)
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            await bot.send_message(uid, txt(uid, "timeout_or_invalid"))
        except Exception:
            pass
        return

    pool, busy = await report_pool_with_busy(uid)
    if not pool:
        if busy:
            await event.respond(txt(uid, "ai_analyze_accounts_busy"))
        else:
            await event.respond(txt(uid, "no_session_found"))
        return
    sess0 = await resolve_probe_session(event, uid, pool)
    if not sess0:
        return

    await event.respond(txt(uid, "ai_analyze_fetching", target=target))
    peer, username, messages = await _fetch_channel_messages(
        sess0, target, AI_ANALYZE_LIMIT
    )
    if not messages:
        await event.respond(txt(uid, "ai_analyze_no_messages"))
        return

    await event.respond(txt(uid, "ai_analyze_working", count=len(messages)))
    try:
        findings = await _ai_analyze_channel(target, messages)
    except ValueError as e:
        if str(e) == "AI_NOT_CONFIGURED":
            await event.respond(txt(uid, "ai_analyze_not_configured"))
        else:
            await event.respond(txt(uid, "ai_analyze_failed", error=ai_error_text(uid, e)))
        return
    except Exception as e:
        logger.warning("ai analyze failed uid=%s target=%s: %s", uid, target, e)
        await event.respond(txt(uid, "ai_analyze_failed", error=ai_error_text(uid, e)))
        return

    if not findings:
        await event.respond(txt(uid, "ai_analyze_none_reportable"))
        return

    header = txt(uid, "ai_analyze_result_header", target=target, count=len(findings))
    lines = [header]
    for f in findings:
        link = _build_post_link(target, username, f["id"])
        lines.append(
            txt(uid, "ai_analyze_item", link=link, reason=_reason_label(f["reason"]), why=f["why"] or "-")
        )
    await event.respond("\n".join(lines), link_preview=False)

    num = await ask_num_accounts(event, uid, pool, busy)
    if not num:
        return
    selected = random.sample(pool, min(num, len(pool)))

    reason_counts: dict[str, int] = {}
    for f in findings:
        reason_counts[f["reason"]] = reason_counts.get(f["reason"], 0) + 1
    dominant = max(reason_counts, key=reason_counts.get)
    reportable_ids = [f["id"] for f in findings if f["reason"] == dominant]
    others = {r: c for r, c in reason_counts.items() if r != dominant}

    await event.respond(txt(uid, "ai_analyze_reporting", count=len(reportable_ids)))
    if others:
        await event.respond(
            txt(
                uid,
                "ai_analyze_other_reasons",
                reason=_reason_label(dominant),
                others=", ".join(f"{_reason_label(r)}: {c}" for r, c in others.items()),
            )
        )

    ai_mode = _AI_MODE_FOR_REASON.get(dominant, "msg")
    ctx = {
        "mode": ai_mode,
        "target": target,
        "entity_kind": _infer_entity_kind(target, None),
        "msg_ids": reportable_ids,
        "path": [],
        "pool": selected,
        "forced_pool": None,
        "sample": {"sess": sess0},
        "skip_join": True,
        "awaiting_join_choice": False,
        "comment": "",
        "comment_source": "ai",
        "report_run_mode": "fixed",
        "reports_per_account": 1,
        "ai_reason": dominant,
    }
    await ctx_set(uid, ctx)

    status, payload = await _auto_walk_reason(
        uid, ctx, selected, dominant, allow_user_choice=False
    )
    logger.info(
        "ai report path uid=%s reason=%s mode=%s status=%s path=%s",
        uid,
        dominant,
        ai_mode,
        status,
        _path_keys_for_log(ctx.get("path")),
    )
    if status in ("done", "comment"):
        await ctx_set(uid, ctx)
        await start_continuous_report(event, uid, ctx, ai_mode, need_comment=True)
    else:
        await event.respond(
            txt(uid, "ai_analyze_failed", error=str(payload)[:150])
        )
        await ctx_pop(uid)

async def process_single_session(sess: str, target: str, now_ts: int) -> dict:
    cli = retern_client(sess)
    if not cli:
        return {
            "sess": sess,
            "status": "failed",
            "reason": "bad_session",
            "checked_at": now_ts,
            "first_checked_at": now_ts,
        }

    try:
        await cli.connect()
        if await cli.is_user_authorized():

            res = await join_group_with_status(
                cli, target, sess, send_join_request=True
            )
            return {
                "sess": sess,
                "status": res["status"],
                "reason": res.get("reason"),
                "checked_at": now_ts,
                "first_checked_at": now_ts,
            }
        else:
            return {
                "sess": sess,
                "status": "failed",
                "reason": "unauthorized",
                "checked_at": now_ts,
                "first_checked_at": now_ts,
            }
    except Exception as e:
        return {
            "sess": sess,
            "status": "failed",
            "reason": _classify_join_error(e),
            "checked_at": now_ts,
            "first_checked_at": now_ts,
        }
    finally:
        try:
            await cli.disconnect()
        except Exception:
            pass

async def wizard_join_request(event: events.NewMessage.Event):
    uid = UID(event)
    if not await has_access(uid):
        return
    await ctx_pop(uid)
    try:
        async with standard_conversation(uid, timeout=240) as conv:
            await conv.send_message(
                txt(uid, "join_req_link_prompt"), buttons=conv_cancel_buttons(uid)
            )
            target = (await menu_safe_response(conv)).raw_text.strip()
            if _parse_target(target)[0] == "unknown":
                await conv.send_message(txt(uid, "invalid_target"))
                return

            pool, busy = await report_pool_with_busy(uid)
            if not pool:
                await conv.send_message(
                    txt(uid, "accounts_all_busy", busy=busy)
                    if busy
                    else txt(uid, "no_session_found")
                )
                return

            jr_prompt = txt(uid, "join_req_num_prompt", max=len(pool))
            if busy:
                jr_prompt += "\n\n" + txt(uid, "accounts_busy_note", busy=busy)
            await conv.send_message(
                jr_prompt,
                buttons=conv_cancel_buttons(uid),
            )
            num_resp = await menu_safe_response(conv)
            try:
                num = int(num_resp.raw_text.strip())
            except Exception:
                await conv.send_message(txt(uid, "invalid_number"))
                return
            if num < 1 or num > len(pool):
                await conv.send_message(txt(uid, "invalid_number_range", max=len(pool)))
                return

            selected_pool = random.sample(pool, num)
            msg = await conv.send_message(txt(uid, "sending_join_requests"))

            now_ts = _now_ts()

            semaphore = asyncio.Semaphore(2)

            async def sem_task(sess):
                async with semaphore:
                    res = await process_single_session(sess, target, now_ts)
                    await asyncio.sleep(2.5)
                    return res

            tasks = [sem_task(sess) for sess in selected_pool]
            accounts_status = await asyncio.gather(*tasks)

            await asyncio.to_thread(
                db["join_requests"].update_one,
                {"user_id": uid, "target": target},
                {
                    "$set": {
                        "status": "pending",
                        "accounts": accounts_status,
                        "requested_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                },
                upsert=True,
            )

            joined = sum(
                1 for a in accounts_status if a["status"] in ("joined", "already")
            )
            pending = sum(1 for a in accounts_status if a["status"] == "pending")
            failed = sum(
                1 for a in accounts_status if a["status"] in ("failed", "rejected")
            )
            reason_counts = {}
            for a in accounts_status:
                if a["status"] not in ("failed", "rejected"):
                    continue
                r = a.get("reason") or "rpc"
                reason_counts[r] = reason_counts.get(r, 0) + 1
            reason_line = ""
            if reason_counts:
                parts = [
                    f"{k}×{v}"
                    for k, v in sorted(reason_counts.items(), key=lambda x: -x[1])[:4]
                ]
                reason_line = "\n" + txt(
                    uid, "jr_fail_reasons", reasons=", ".join(parts)
                )

            logger.warning(
                "[join_req] wizard result for %s: joined=%s pending=%s failed=%s reasons=%s",
                uid,
                joined,
                pending,
                failed,
                reason_counts,
            )
            await msg.edit(
                txt(
                    uid, "join_req_stats", joined=joined, pending=pending, failed=failed
                )
                + reason_line,
                buttons=[
                    [
                        Button.inline(
                            txt(uid, "join_req_check_btn"),
                            data=f"jrcheck:{uid}".encode(),
                        )
                    ]
                ],
            )

    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

async def join_group_with_status(
    client,
    link_or_username: str,
    sess_key: str | None = None,
    *,
    send_join_request: bool = False,
) -> dict:
    target = (link_or_username or "").strip()
    kind, _token = _parse_target(target)

    if kind == "invite":
        pre_peer = await join_group(client, link_or_username, sess_key, skip_join=True)
        if pre_peer and pre_peer not in (False, "pending") and _is_valid_peer(pre_peer):
            st = await check_membership(client, pre_peer)
            if st == "joined":
                return {"status": "already", "peer": pre_peer, "reason": None}

    peer = await join_group(
        client,
        link_or_username,
        sess_key,
        skip_join=False,
        send_join_request=send_join_request,
    )
    if peer == "pending" or peer == "JOIN_PENDING":
        return {"status": "pending", "peer": None, "reason": "pending"}
    if isinstance(peer, str) and peer.startswith("JOIN_FAILED"):
        return {
            "status": "failed",
            "peer": None,
            "reason": peer.split(":", 1)[-1] if ":" in peer else "join_false",
        }
    if not peer:
        return {"status": "failed", "peer": None, "reason": "join_false"}
    if not _is_valid_peer(peer):
        return {"status": "failed", "peer": None, "reason": "invalid_peer"}

    st = await check_membership(client, peer)
    if st == "joined":
        return {"status": "joined", "peer": peer, "reason": None}

    if st == "not_member":
        await asyncio.sleep(1.5)
        st2 = await check_membership(client, peer)
        if st2 == "joined":
            return {"status": "joined", "peer": peer, "reason": None}

        if kind == "invite" and send_join_request:
            return {"status": "joined", "peer": peer, "reason": None}
        if kind == "invite" and not send_join_request:
            return {"status": "joined", "peer": peer, "reason": None}

        if kind == "username":
            return {"status": "joined", "peer": peer, "reason": "public_peer"}
        return {"status": "failed", "peer": peer, "reason": "not_member"}


    if kind in ("username", "invite") and _is_valid_peer(peer):
        return {"status": "joined", "peer": peer, "reason": st or "check_soft"}
    return {"status": "failed", "peer": peer, "reason": st or "check_failed"}

def is_menu_or_command(event) -> bool:
    text = (event.raw_text or "").strip()
    if not text:
        return False

    cmd = text.split("@", 1)[0].lower()
    if cmd in ("/done", "done"):
        return False
    if text.startswith("/"):
        return True
    return text in MENU_ACTIONS_MAP or normalize_menu_text(text) in MENU_ACTIONS_MAP

def _is_done_command(text: str | None) -> bool:
    t = (text or "").strip().split("@", 1)[0].lower()
    return t in ("/done", "done")

PENDING_CONVERSATIONS = {}

def _conv_key(entity):
    return (
        int(entity) if isinstance(entity, int) or str(entity).isdigit() else str(entity)
    )

class MenuInterrupt(Exception):
    pass

class StandardConversation:
    def __init__(
        self, client, entity, timeout=120, *, exclusive=True, clear_ctx_on_menu=True
    ):
        self.client = client
        self.entity = entity
        self.timeout = timeout
        self.exclusive = exclusive
        self.clear_ctx_on_menu = clear_ctx_on_menu
        self.key = _conv_key(entity)
        self.queue = asyncio.Queue(maxsize=1)
        self.active = False

    async def __aenter__(self):
        old = PENDING_CONVERSATIONS.get(self.key)
        if self.exclusive and old is not None and old is not self and old.active:
            uid = self.key if isinstance(self.key, int) else UID(self.entity) or 0
            try:
                await self.client.send_message(
                    self.entity,
                    txt(uid, "conv_busy"),
                    buttons=conv_cancel_buttons(uid),
                )
            except Exception:
                pass
            raise MenuInterrupt()
        PENDING_CONVERSATIONS[self.key] = self
        self.active = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if PENDING_CONVERSATIONS.get(self.key) is self:
            PENDING_CONVERSATIONS.pop(self.key, None)
        self.active = False
        return False

    async def send_message(self, *args, **kwargs):
        return await self.client.send_message(self.entity, *args, **kwargs)

    async def get_response(self):
        try:
            resp = await asyncio.wait_for(self.queue.get(), timeout=self.timeout)
        except asyncio.TimeoutError:
            if PENDING_CONVERSATIONS.get(self.key) is self:
                PENDING_CONVERSATIONS.pop(self.key, None)
            self.active = False
            raise
        if isinstance(resp, MenuInterrupt):
            raise resp
        return resp

    async def ask(self, text, **send_kwargs):
        await self.send_message(text, **send_kwargs)
        return await self.get_response()

def standard_conversation(
    entity=None, *, timeout=120, exclusive=True, clear_ctx_on_menu=True
):
    return StandardConversation(
        bot,
        entity,
        timeout=timeout,
        exclusive=exclusive,
        clear_ctx_on_menu=clear_ctx_on_menu,
    )

def conv_cancel_buttons(uid, back_data=None):
    data = b"conv_cancel" if back_data is None else b"conv_cancel:" + back_data
    return [[Button.inline(txt(uid, "cancel_btn"), data=data)]]

def admin_back_buttons(uid):
    return [[Button.inline(txt(uid, "back_btn"), data=b"admin_back")]]

def main_back_buttons(uid):
    return [[Button.inline(txt(uid, "back_btn"), data=b"start_menu")]]

@bot.on(events.CallbackQuery(pattern=rb"conv_cancel(?::.*)?"))
async def conv_cancel_handler(event):
    uid = UID(event)
    key = _conv_key(uid)
    raw = event.data or b"conv_cancel"
    back_data = b"start_menu"
    if raw.startswith(b"conv_cancel:"):
        back_data = raw.split(b":", 1)[1] or b"start_menu"
    conv = PENDING_CONVERSATIONS.pop(key, None)
    await ctx_pop(uid)
    if conv and conv.active:
        conv.active = False
        try:
            conv.queue.put_nowait(MenuInterrupt())
        except Exception:
            pass
    try:
        await event.answer()
    except Exception:
        pass

    if back_data == b"start_menu":
        try:
            welcome = txt(uid, "main_menu_welcome")
            await event.edit(welcome, buttons=await kb_main(uid))
        except Exception:
            await event.edit(
                txt(uid, "cancel"),
                buttons=[[Button.inline(txt(uid, "back_btn"), data=b"start_menu")]],
            )
    elif back_data == b"admin_back":
        await event.edit(txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid))
    elif back_data == b"admin_user_list":
        total, items = await list_users_page(uid, 0)
        await event.edit(
            txt(uid, "users_list"), buttons=build_users_keyboard(uid, total, items, 0)
        )
    elif back_data == b"plus_manage":
        try:
            await plus_manage(event)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    elif back_data == b"partner_menu":
        try:
            await partner_menu(event)
        except Exception:
            welcome = txt(uid, "main_menu_welcome")
            await event.edit(welcome, buttons=await kb_main(uid))
    elif back_data == b"set_prices":
        try:
            await admin_price_menu(event)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    elif back_data == b"proxy_menu":
        text, rows = build_proxy_menu(uid, 0)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    elif back_data == b"ai_menu":
        text, rows = build_ai_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    elif back_data == b"broadcast":
        await event.edit(
            txt(uid, "broadcast_prompt"),
            buttons=conv_cancel_buttons(uid, b"admin_back"),
        )
        await ctx_set(uid, {"mode": "broadcast", "step": "await_message"})
    elif back_data == b"fj_menu":
        text, rows = build_fj_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    elif back_data == b"ref_menu":
        text, rows = build_referral_admin_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    elif back_data == b"crypto_menu":
        text, rows = await build_crypto_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.edit(
                txt(uid, "admin_menu_hello"), buttons=build_admin_buttons(uid)
            )
    else:
        await event.edit(
            txt(uid, "cancel"),
            buttons=[[Button.inline(txt(uid, "back_btn"), data=back_data)]],
        )
    raise events.StopPropagation

def _is_image_receipt(event) -> bool:
    if getattr(event, "photo", None):
        return True
    doc = getattr(event, "document", None)
    if not doc:
        return False
    mime = (getattr(doc, "mime_type", None) or "").lower()
    if mime.startswith("image/"):
        return True
    for attr in getattr(doc, "attributes", None) or []:
        name = (getattr(attr, "file_name", None) or "").lower()
        if name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")):
            return True
    return False

@bot.on(events.NewMessage)
async def _standard_conversation_router(event):
    uid = UID(event)
    key = _conv_key(uid)
    conv = PENDING_CONVERSATIONS.get(key)
    if not conv or not conv.active:
        return
    try:
        pctx = await ctx_get(uid)
        if (
            pctx
            and pctx.get("mode") == "purchase"
            and pctx.get("awaiting_receipt")
            and _is_image_receipt(event)
        ):
            return
    except Exception:
        pass
    if is_menu_or_command(event):
        PENDING_CONVERSATIONS.pop(key, None)
        conv.active = False
        if conv.clear_ctx_on_menu:
            await ctx_pop(uid)
        try:
            conv.queue.put_nowait(MenuInterrupt())
        except asyncio.QueueFull:
            try:
                conv.queue.get_nowait()
                conv.queue.put_nowait(MenuInterrupt())
            except Exception:
                pass
        await asyncio.sleep(0.05)
        return
    try:
        conv.queue.put_nowait(event)
    except asyncio.QueueFull:
        pass
    raise events.StopPropagation

async def menu_safe_response(conv):

    resp = await conv.get_response()
    if is_menu_or_command(resp):
        try:
            await ctx_pop(UID(resp))
        except Exception:
            pass
        raise MenuInterrupt()
    return resp

@bot.on(
    events.NewMessage(
        func=lambda e: (
            e.text
            and not getattr(e, "out", False)
            and (
                e.text.strip() in MENU_ACTIONS_MAP
                or normalize_menu_text(e.text) in MENU_ACTIONS_MAP
            )
        )
    )
)
async def main_menu_text_handler(event: events.NewMessage.Event):
    uid = UID(event)
    raw_text = event.text.strip()
    action = MENU_ACTIONS_MAP.get(raw_text) or MENU_ACTIONS_MAP.get(
        normalize_menu_text(raw_text)
    )

    if not action:
        return

    if action not in ("language",) and not await ensure_force_join(event, uid):
        raise events.StopPropagation

    if action in PANEL_PERMISSION_ACTIONS and not has_panel_permission(uid, action):
        await event.reply(txt(uid, "no_permission"))
        raise events.StopPropagation

    await ctx_pop(uid)

    if action == "accounts":
        if not can_use_accounts(uid):
            await event.reply(txt(uid, "accounts_not_allowed_short_sub"))
            raise events.StopPropagation
        await handle_accounts_menu(event)
    elif action == "report_msg":
        await wizard_msg(event)
    elif action == "report_story":
        await wizard_story(event)
    elif action == "report_bot":
        await wizard_bot_dialog(event)
    elif action == "partners":
        await partner_menu(event)
    elif action == "transfer":
        await transfer_own(event)
    elif action in ("cancel", "back"):
        await show_main_menu(event, uid)
    elif action == "language":
        await show_lang_menu(event)
    elif action == "my_credit":
        await show_my_credit(event)
    elif action == "referral":
        await show_referral(event)
    elif action == "buy_normal":
        await start_purchase(event, "normal")
    elif action == "buy_special":
        await start_purchase(event, "special")
    elif action == "report_scam":
        await wizard_scam(event)
    elif action == "report_fake":
        await wizard_fake(event)

    elif action == "report_profile":
        await wizard_profile(event)

    elif action == "join_request":
        await wizard_join_request(event)

    elif action == "send_pv":
        await wizard_send_pv(event)

    elif action == "ai_analyze":
        await wizard_ai_analyze(event)
    elif action == "report_manage":
        await show_report_manage(event)
    raise events.StopPropagation

async def start_purchase(event, plan):
    uid = UID(event)
    if await has_access(uid):
        await event.reply(txt(uid, "already_has_access"))
        return
    pending = PURCHASE_REQUESTS_COL.find_one(
        {"user_id": uid, "status": {"$in": ["pending", "pending_crypto"]}}
    )
    if pending:
        await event.reply(txt(uid, "purchase_request_pending"))
        return

    packages = get_packages(plan)
    if not packages:
        await event.reply(txt(uid, "no_packages_available"))
        return

    rows = []
    for idx, pkg in enumerate(packages):
        days = pkg["days"]
        price = pkg["price"]
        max_acc = pkg.get("max_accounts", 0)
        if plan == "special":
            label = f"{days} {txt(uid, 'days')} - {price} {txt(uid, 'currency')} (max: {max_acc})"
        else:
            label = f"{days} {txt(uid, 'days')} - {price} {txt(uid, 'currency')}"
        rows.append([Button.inline(label, data=f"pkg_select:{plan}:{idx}".encode())])
    rows.append([Button.inline(txt(uid, "cancel_btn"), data=b"pkg_cancel")])

    ctx = {"mode": "select_package", "plan": plan}
    await ctx_set(uid, ctx)
    await event.reply(txt(uid, "select_package_prompt"), buttons=rows)

@bot.on(events.CallbackQuery(pattern=b"pkg_select:"))
async def package_selected(event):
    uid = UID(event)
    data = event.data.decode().split(":")
    plan = data[1]
    idx = int(data[2])
    packages = get_packages(plan)
    if idx >= len(packages):
        await event.answer(txt(uid, "invalid_package"), alert=True)
        return
    pkg = packages[idx]

    try:
        key = _conv_key(uid)
        conv = PENDING_CONVERSATIONS.pop(key, None)
        if conv and getattr(conv, "active", False):
            conv.active = False
            try:
                conv.queue.put_nowait(MenuInterrupt())
            except Exception:
                pass
    except Exception:
        pass

    ctx = await ctx_get(uid) or {}
    ctx["mode"] = "purchase"
    ctx["plan"] = plan
    ctx["days"] = pkg["days"]
    ctx["price"] = pkg["price"]
    ctx["max_accounts"] = pkg.get("max_accounts", 0)
    ctx["awaiting_receipt"] = False
    ctx["awaiting_tx_hash"] = False
    ctx["payment_method"] = ""
    await ctx_set(uid, ctx)

    crypto = get_crypto_settings()
    rows = [
        [Button.inline(txt(uid, "pay_method_card"), data=b"pay_method:card")],
    ]
    if crypto_pay_available():
        rows.append(
            [Button.inline(txt(uid, "pay_method_crypto"), data=b"pay_method:crypto")]
        )
    else:
        logger.info(
            "crypto pay hidden uid=%s enabled=%s wallet=%s",
            uid,
            crypto.get("enabled"),
            bool((crypto.get("wallet") or "").strip()),
        )
    rows.append([Button.inline(txt(uid, "cancel_btn"), data=b"pkg_cancel")])
    await event.edit(
        txt(
            uid,
            "pay_method_prompt",
            days=pkg["days"],
            price=pkg["price"],
        ),
        buttons=rows,
    )

async def _show_card_payment_instructions(event, uid: int, ctx: dict):
    bank = get_bank_info()
    if bank["card_number"] and bank["card_holder"]:
        bank_text = txt(
            uid, "bank_info_text", holder=bank["card_holder"], card=bank["card_number"]
        )
    else:
        bank_text = txt(uid, "bank_not_set")
    text = f"{txt(uid, 'send_receipt_photo')}\n\n{bank_text}"
    buttons = [[Button.inline(txt(uid, "cancel_btn"), data=b"pkg_cancel")]]
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)

async def _show_crypto_payment_instructions(event, uid: int, ctx: dict):
    crypto = get_crypto_settings()
    if not crypto_pay_available():
        await event.answer(txt(uid, "crypto_not_configured"), alert=True)
        return False
    pending = PURCHASE_REQUESTS_COL.find_one(
        {
            "user_id": uid,
            "status": {"$in": ["pending", "pending_crypto"]},
        }
    )
    if pending:
        await event.answer(txt(uid, "purchase_request_pending"), alert=True)
        return False
    try:
        gram_price = await fetch_bitpin_price(crypto.get("symbol") or "GRAM_IRT")
        amount = calc_gram_amount(
            ctx.get("price") or 0, gram_price, crypto.get("decimals") or 4
        )
    except Exception as e:
        logger.warning("crypto price calc failed: %s", e)
        await event.answer(txt(uid, "crypto_price_fail"), alert=True)
        return False

    payment_code = _gen_payment_code(uid)
    fixed_memo = (crypto.get("memo") or "").strip()

    display_memo = payment_code if not fixed_memo else f"{fixed_memo} {payment_code}"

    request_id = create_purchase_request(
        uid,
        ctx.get("plan"),
        int(ctx.get("price") or 0),
        "",
        int(ctx.get("days") or 0),
        int(ctx.get("max_accounts") or 0),
        payment_method="crypto",
        crypto_amount=amount,
        crypto_price=gram_price,
        crypto_asset=crypto.get("asset_name") or "GRAM",
        crypto_network=crypto.get("network") or "TON",
        crypto_wallet=crypto.get("wallet") or "",
        payment_code=payment_code,
        status="pending_crypto",
    )

    ctx["payment_method"] = "crypto"
    ctx["crypto_amount"] = amount
    ctx["crypto_price"] = gram_price
    ctx["crypto_asset"] = crypto.get("asset_name") or "GRAM"
    ctx["crypto_network"] = crypto.get("network") or "TON"
    ctx["crypto_wallet"] = crypto.get("wallet") or ""
    ctx["payment_code"] = payment_code
    ctx["purchase_request_id"] = str(request_id)
    ctx["awaiting_receipt"] = False
    ctx["awaiting_tx_hash"] = True
    await ctx_set(uid, ctx)

    text = txt(
        uid,
        "crypto_pay_text",
        asset=ctx["crypto_asset"],
        network=ctx["crypto_network"],
        amount=format_crypto_amount(amount, crypto.get("decimals") or 4),
        wallet=ctx["crypto_wallet"],
        price_irt=int(gram_price),
        toman=ctx.get("price") or 0,
        memo=display_memo,
        code=payment_code,
    )
    buttons = [
        [
            Button.inline(
                txt(uid, "crypto_check_btn"),
                data=f"crypto_check:{request_id}".encode(),
            )
        ],
        [Button.inline(txt(uid, "cancel_btn"), data=b"pkg_cancel")],
    ]
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)
    return True

async def _finalize_crypto_auto_approve(uid: int, req: dict, match: dict, event=None):
    await approve_purchase_request(
        req, approved_by=0, tx_hash=str(match.get("tx_hash") or "")
    )
    await ctx_pop(uid)
    msg = txt(uid, "crypto_auto_ok", tx=str(match.get("tx_hash") or "")[:20])
    if event is not None:
        try:
            await event.respond(msg, buttons=await kb_main(uid))
        except Exception:
            pass
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                txt(
                    admin_id,
                    "crypto_auto_admin",
                    user=uid,
                    amount=format_crypto_amount(float(req.get("crypto_amount") or 0), 4),
                    asset=req.get("crypto_asset") or "GRAM",
                    tx=str(match.get("tx_hash") or "")[:24],
                ),
            )
        except Exception:
            pass

@bot.on(events.CallbackQuery(pattern=b"^pay_method:(card|crypto)$"))
async def on_pay_method(event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "purchase":
        await event.answer(txt(uid, "session_not_found"), alert=True)
        return
    method = event.data.decode().split(":", 1)[1]
    if method == "card":
        await event.answer()
        ctx["payment_method"] = "card"
        ctx["awaiting_receipt"] = True
        ctx["awaiting_tx_hash"] = False
        ctx["crypto_amount"] = 0
        ctx["crypto_price"] = 0
        await ctx_set(uid, ctx)
        await _show_card_payment_instructions(event, uid, ctx)
        raise events.StopPropagation
    await event.answer()
    ok = await _show_crypto_payment_instructions(event, uid, ctx)
    if not ok:
        return
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"^crypto_check:"))
async def on_crypto_check(event):
    uid = UID(event)
    rid = event.data.decode().split(":", 1)[1]
    try:
        oid = ObjectId(rid)
    except Exception:
        await event.answer(txt(uid, "request_invalid"), alert=True)
        return
    req = PURCHASE_REQUESTS_COL.find_one({"_id": oid})
    if not req or int(req.get("user_id") or 0) != int(uid):
        if uid not in ADMIN_IDS or not req:
            await event.answer(txt(uid, "request_invalid"), alert=True)
            return
    if req.get("status") == "approved":
        await event.answer(txt(uid, "crypto_already_approved"), alert=True)
        return
    if req.get("status") not in ("pending_crypto", "pending"):
        await event.answer(txt(uid, "request_invalid"), alert=True)
        return
    if not get_crypto_settings().get("auto_verify", True):
        await event.answer(txt(uid, "crypto_auto_off"), alert=True)
        return
    await event.answer(txt(uid, "crypto_checking"), alert=False)
    match = await find_crypto_payment_match(req)
    if match and match.get("_api_error"):
        try:
            await event.respond(txt(uid, "crypto_api_down"))
        except Exception as e:
            logger.warning("crypto api down reply failed: %s", e)
        return
    if not match:
        try:
            await event.respond(txt(uid, "crypto_not_found"))
        except Exception:
            pass
        return
    await approve_purchase_request(
        req, approved_by=0, tx_hash=str(match.get("tx_hash") or "")
    )
    await ctx_pop(uid)
    try:
        await event.edit(
            txt(uid, "crypto_auto_ok", tx=str(match.get("tx_hash") or "")[:16]),
            buttons=await kb_main(uid),
        )
    except Exception:
        await event.respond(
            txt(uid, "crypto_auto_ok", tx=str(match.get("tx_hash") or "")[:16]),
            buttons=await kb_main(uid),
        )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                txt(
                    admin_id,
                    "crypto_auto_admin",
                    user=uid,
                    amount=format_crypto_amount(float(req.get("crypto_amount") or 0), 4),
                    asset=req.get("crypto_asset") or "GRAM",
                    tx=str(match.get("tx_hash") or "")[:24],
                ),
            )
        except Exception:
            pass
    raise events.StopPropagation

def _is_crypto_tx_hash_message(event) -> bool:
    if not getattr(event, "text", None):
        return False
    if getattr(event, "photo", None) or getattr(event, "document", None):
        return False
    text = (event.text or "").strip()
    if not text or text.startswith("/"):
        return False
    if text in MENU_ACTIONS_MAP or normalize_menu_text(text) in MENU_ACTIONS_MAP:
        return False
    return bool(_extract_tx_hash(text))

@bot.on(events.NewMessage(func=_is_crypto_tx_hash_message))
async def handle_crypto_tx_hash(event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "purchase" or not ctx.get("awaiting_tx_hash"):
        return
    if (ctx.get("payment_method") or "").lower() != "crypto":
        return
    tx_hash = _extract_tx_hash(event.text or "")
    if not tx_hash:
        await event.reply(txt(uid, "crypto_hash_invalid"))
        raise events.StopPropagation

    rid = ctx.get("purchase_request_id")
    req = None
    if rid:
        try:
            req = PURCHASE_REQUESTS_COL.find_one(
                {
                    "_id": ObjectId(rid),
                    "user_id": uid,
                    "status": {"$in": ["pending_crypto", "pending"]},
                }
            )
        except Exception:
            req = None
    if not req:
        req = PURCHASE_REQUESTS_COL.find_one(
            {
                "user_id": uid,
                "status": "pending_crypto",
                "payment_method": "crypto",
            }
        )
    if not req:
        await event.reply(txt(uid, "request_invalid"))
        raise events.StopPropagation

    PURCHASE_REQUESTS_COL.update_one(
        {"_id": req["_id"]},
        {"$set": {"submitted_tx_hash": tx_hash}},
    )
    req["submitted_tx_hash"] = tx_hash

    if not get_crypto_settings().get("auto_verify", True):
        PURCHASE_REQUESTS_COL.update_one(
            {"_id": req["_id"]}, {"$set": {"status": "pending"}}
        )
        await ctx_pop(uid)
        await event.reply(txt(uid, "crypto_hash_received_manual"))
        for admin_id in ADMIN_IDS:
            try:
                plan_name = get_plan_name(admin_id, req.get("plan"))
                await bot.send_message(
                    admin_id,
                    txt(
                        admin_id,
                        "purchase_request_detail_crypto_hash",
                        user_id=uid,
                        plan_name=plan_name,
                        days=req.get("days") or 0,
                        price=req.get("price") or 0,
                        amount=format_crypto_amount(float(req.get("crypto_amount") or 0), 4),
                        asset=req.get("crypto_asset") or "GRAM",
                        network=req.get("crypto_network") or "TON",
                        wallet=req.get("crypto_wallet") or "—",
                        rate=int(req.get("crypto_price") or 0),
                        tx=tx_hash,
                        date=datetime.datetime.now(datetime.timezone.utc).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                    ),
                    buttons=[
                        [
                            Button.inline(
                                txt(admin_id, "admin_approve_button"),
                                data=f"purchase_approve:{req['_id']}".encode(),
                            ),
                            Button.inline(
                                txt(admin_id, "admin_reject_button"),
                                data=f"purchase_reject:{req['_id']}".encode(),
                            ),
                        ]
                    ],
                )
            except Exception:
                pass
        raise events.StopPropagation

    await event.reply(txt(uid, "crypto_checking"))
    match = await verify_submitted_tx_hash(req, tx_hash)
    if not match:
        match = await find_crypto_payment_match(req)
    if match:
        await _finalize_crypto_auto_approve(uid, req, match, event=event)
        raise events.StopPropagation

    await event.reply(txt(uid, "crypto_hash_saved_pending", tx=tx_hash[:20]))
    raise events.StopPropagation

@bot.on(events.NewMessage(func=_is_image_receipt))
async def handle_receipt_photo(event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "purchase" or not ctx.get("awaiting_receipt"):
        return

    if (ctx.get("payment_method") or "").lower() == "crypto" or ctx.get("awaiting_tx_hash"):
        await event.reply(txt(uid, "crypto_send_hash_not_photo"))
        raise events.StopPropagation

    plan = ctx["plan"]
    price = ctx["price"]
    days = ctx["days"]
    max_accounts = ctx.get("max_accounts", 0)
    payment_method = (ctx.get("payment_method") or "card").strip().lower() or "card"
    crypto_amount = float(ctx.get("crypto_amount") or 0)
    crypto_price = float(ctx.get("crypto_price") or 0)
    crypto_asset = str(ctx.get("crypto_asset") or "")
    crypto_network = str(ctx.get("crypto_network") or "")
    crypto_wallet = str(ctx.get("crypto_wallet") or "")

    media_id = None
    if getattr(event, "photo", None):
        media_id = getattr(event.photo, "id", None)
    if media_id is None and getattr(event, "document", None):
        media_id = getattr(event.document, "id", None)
    photo_id = str(media_id or event.id)

    request_id = create_purchase_request(
        uid,
        plan,
        price,
        photo_id,
        days,
        max_accounts,
        payment_method=payment_method,
        crypto_amount=crypto_amount,
        crypto_price=crypto_price,
        crypto_asset=crypto_asset,
        crypto_network=crypto_network,
        crypto_wallet=crypto_wallet,
        status="pending",
    )
    await ctx_pop(uid)

    try:
        await event.reply(txt(uid, "receipt_received"))
    except Exception:
        try:
            await event.respond(txt(uid, "receipt_received"))
        except Exception as e:
            logger.warning("receipt ack failed uid=%s: %s", uid, e)

    temp_path = None
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        temp_path = temp_file.name
        temp_file.close()

        await event.message.download_media(file=temp_path)

        for admin_id in ADMIN_IDS:
            try:
                plan_name = get_plan_name(admin_id, plan)
                if payment_method == "crypto":
                    text = txt(
                        admin_id,
                        "purchase_request_detail_crypto",
                        user_id=uid,
                        plan_name=plan_name,
                        days=days,
                        price=price,
                        amount=format_crypto_amount(crypto_amount, 4),
                        asset=crypto_asset or "GRAM",
                        network=crypto_network or "TON",
                        wallet=crypto_wallet or "—",
                        rate=int(crypto_price or 0),
                        date=datetime.datetime.now(datetime.timezone.utc).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                    )
                else:
                    text = txt(
                        admin_id,
                        "purchase_request_detail",
                        user_id=uid,
                        plan_name=plan_name,
                        days=days,
                        price=price,
                        date=datetime.datetime.now(datetime.timezone.utc).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                    )
                buttons = [
                    [
                        Button.inline(
                            txt(admin_id, "admin_approve_button"),
                            data=f"purchase_approve:{request_id}".encode(),
                        ),
                        Button.inline(
                            txt(admin_id, "admin_reject_button"),
                            data=f"purchase_reject:{request_id}".encode(),
                        ),
                    ]
                ]
                await bot.send_file(
                    admin_id, file=temp_path, caption=text, buttons=buttons
                )
            except Exception as e:
                logger.warning(f"Error notifying admin {admin_id}: {e}")
    except Exception as e:
        logger.warning(f"Error downloading photo: {e}")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"^st:"))
async def on_story_select(event: events.CallbackQuery.Event):
    await event.answer()
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "story":
        return

    ctx["selected_story_ids"] = set(
        _safe_int32_ids(list(ctx.get("selected_story_ids") or []))
    )
    data = event.data.decode()
    if data == "st:noop":
        return
    if data == "st:cancel":
        await event.edit(txt(uid, "cancel"))
        await ctx_pop(uid)
        return
    if data == "st:act_all":
        act_ids = [m["id"] for m in ctx.get("stories_active_meta", [])]
        ctx["selected_story_ids"].update(act_ids)
    elif data == "st:act_none":
        act_ids = {m["id"] for m in ctx.get("stories_active_meta", [])}
        ctx["selected_story_ids"] = {
            i for i in ctx["selected_story_ids"] if i not in act_ids
        }
    elif data == "st:hl_prev":
        if ctx["hl_current_idx"] > 0:
            ctx["hl_current_idx"] -= 1
    elif data == "st:hl_next":
        next_idx = ctx["hl_current_idx"] + 1
        if next_idx < len(ctx["stories_hl_pages"]):
            ctx["hl_current_idx"] = next_idx
        else:
            nxt = ctx.get("hl_next_offset")
            if nxt is None:
                await event.answer(txt(uid, "no_next_page"), alert=False)
            else:
                items, new_next = await fetch_highlights_page(
                    ctx["sample"]["sess"],
                    ctx["target"],
                    limit=ctx.get("hl_page_size", 30),
                    offset_id=nxt,
                )
                ctx["stories_hl_pages"].append({"offset": nxt, "items": items})
                ctx["hl_current_idx"] = next_idx
                ctx["hl_next_offset"] = new_next
    elif data == "st:hl_all":
        page_items = ctx["stories_hl_pages"][ctx["hl_current_idx"]]["items"]
        ctx["selected_story_ids"].update([m["id"] for m in page_items])
    elif data == "st:hl_none":
        page_items = ctx["stories_hl_pages"][ctx["hl_current_idx"]]["items"]
        page_ids = {m["id"] for m in page_items}
        ctx["selected_story_ids"] = {
            i for i in ctx["selected_story_ids"] if i not in page_ids
        }
    elif data == "st:next":
        if not ctx.get("selected_story_ids"):
            await event.answer(txt(uid, "at_least_one_story"), alert=True)
            return

        ctx["selected_story_ids"] = set(
            _safe_int32_ids(list(ctx.get("selected_story_ids") or []))
        )
        if not ctx["selected_story_ids"]:
            await event.answer(txt(uid, "at_least_one_story"), alert=True)
            return
        sess0 = ctx.get("sample", {}).get("sess")
        if not sess0:
            sess0 = await resolve_probe_session(event, uid, ctx.get("pool") or [])
            if not sess0:
                await ctx_pop(uid)
                return
            ctx["sample"] = {"sess": sess0}
        status, payload = await sample_step(uid, ctx, option_bytes=None)
        if status == "choose":
            _remember_report_options(ctx, payload)
        await ctx_set(uid, ctx)
        if status == "choose":
            await event.respond(
                txt(uid, "select_report_reason"),
                buttons=build_option_keyboard(uid, payload),
            )
            return
        if status == "comment":
            await ask_comment_source(
                event, uid, ctx, mode="story", need_comment=True, sample_option=payload
            )
            return
        if status == "done":
            await event.edit(txt(uid, "path_complete"))
            await start_continuous_report(event, uid, ctx, "story", need_comment=False)
            return
        await event.respond(_format_wizard_error(uid, payload))
        await ctx_pop(uid)
        return
    else:
        try:
            _, _, sid = data.split(":")
            sid = int(sid)
            sel = ctx["selected_story_ids"]
            if sid in sel:
                sel.remove(sid)
            else:
                sel.add(sid)
        except Exception:
            pass
    await ctx_set(uid, ctx)
    active_meta = ctx.get("stories_active_meta", [])
    pages = ctx.get("stories_hl_pages") or [{"offset": 0, "items": []}]
    idx = int(ctx.get("hl_current_idx") or 0)
    if idx < 0:
        idx = 0
    if idx >= len(pages):
        idx = len(pages) - 1
        ctx["hl_current_idx"] = idx
    hl_page = pages[idx]["items"]
    has_prev = idx > 0
    has_next = (idx + 1 < len(pages)) or (ctx.get("hl_next_offset") is not None)
    try:
        await event.edit(
            txt(uid, "story_select_prompt"),
            buttons=build_story_keyboard_sections_paged(
                uid,
                active_meta,
                hl_page,
                ctx.get("selected_story_ids") or set(),
                page_num=idx + 1,
                has_prev=has_prev,
                has_next=has_next,
            ),
        )
    except Exception:
        await event.respond(
            txt(uid, "story_select_prompt"),
            buttons=build_story_keyboard_sections_paged(
                uid,
                active_meta,
                hl_page,
                ctx.get("selected_story_ids") or set(),
                page_num=idx + 1,
                has_prev=has_prev,
                has_next=has_next,
            ),
        )

@bot.on(events.CallbackQuery(pattern=b"^mr:"))
async def on_menu_choose(event: events.CallbackQuery.Event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") not in ("msg", "story", "scam", "fake"):
        await event.answer(txt(uid, "session_not_found"), alert=True)
        return
    data = event.data
    if data == b"mr:cancel":
        await event.answer()
        try:
            await event.edit(txt(uid, "cancel"))
        except Exception:
            pass
        await ctx_pop(uid)
        return
    opt = _decode_opt_callback(data, "mr:")
    if not opt:
        opt = _norm_option(data[3:])
    if not opt:
        await event.answer(txt(uid, "invalid_input"), alert=True)
        return
    mode = ctx["mode"]
    reason_code = _mode_reason_code(mode)
    last_opts = _deserialize_report_options(ctx.get("last_report_options"))
    if reason_code:
        picked = next((o for o in last_opts if o.option == opt), None)
        if picked is None or _option_forbidden_for(reason_code, picked):
            logger.warning(
                "menu choice rejected mode=%s option=%r offered=%s",
                mode,
                opt,
                [_option_key(o) for o in last_opts],
            )
            await event.answer(txt(uid, "invalid_input"), alert=True)
            return
    await event.answer()
    ctx.setdefault("path", []).append(
        _path_step_from_option(uid, opt, options_list=last_opts or None)
    )
    status, payload = await sample_step(uid, ctx, option_bytes=opt)
    if status == "choose" and reason_code:
        payload = _reason_sub_options(payload, reason_code)
        if not payload:
            status, payload = "error", f"{reason_code.upper()}_OPTION_UNAVAILABLE"
    if status == "choose":
        _remember_report_options(ctx, payload)
    await ctx_set(uid, ctx)
    if status == "choose":
        await _show_report_options(event, uid, "select_next_option", payload)
        return
    if status == "comment":
        if reason_code:
            await _auto_comment_then_start(event, uid, ctx, payload, mode)
            return
        await ask_comment_source(
            event,
            uid,
            ctx,
            mode=mode,
            need_comment=True,
            sample_option=payload,
        )
        return
    if status == "done":
        try:
            await event.edit(txt(uid, "path_complete"))
        except Exception:
            await event.respond(txt(uid, "path_complete"))
        await start_continuous_report(
            event,
            uid,
            ctx,
            mode,
            need_comment=True if _path_needs_channel_ban(ctx) else False,
        )
        return
    await event.respond(_format_wizard_error(uid, payload))
    await ctx_pop(uid)

@bot.on(events.NewMessage)
async def on_comment(event: events.NewMessage.Event):
    uid = UID(event)
    if is_menu_or_command(event):
        await ctx_pop(uid)
        return
    ctx = await ctx_get(uid)
    if (
        not ctx
        or ctx.get("mode") not in ("msg", "story", "scam", "fake")
        or not ctx.get("awaiting_comment")
    ):
        return
    raw = event.raw_text or ""
    comments = _split_comments(raw)
    if not comments:
        await event.reply(txt(uid, "ai_invalid"))
        raise events.StopPropagation
    ctx["comments"] = comments
    ctx["comment"] = comments[0]
    ctx["comment_source"] = "text"
    ctx["awaiting_comment"] = False
    needs_sample = bool(
        ctx.pop("comment_needs_sample", False) or ctx.get("add_comment_option")
    )
    await ctx_set(uid, ctx)

    if needs_sample:
        sample_comment = await _pick_comment(ctx, need_comment=True)
        status, payload = await sample_step_with_pool_fallback(
            uid,
            ctx,
            ctx.get("pool") or [],
            option_bytes=ctx.get("add_comment_option"),
            message=sample_comment,
        )
        await ctx_set(uid, ctx)
        if status == "done":
            await event.reply(txt(uid, "report_sample_sent"))
            await start_continuous_report(
                event, uid, ctx, ctx["mode"], need_comment=True
            )
        else:
            err = str(payload or "")
            if err in (
                "ChannelPrivateError",
                "CHANNEL_PRIVATE",
                "EXC_ChannelPrivateError",
            ) or "CHANNEL_PRIVATE" in err:
                await event.reply(txt(uid, "channel_private_hint"))
            elif status != "error":
                await event.reply(txt(uid, "report_sample_failed"))
            else:
                await event.reply(
                    txt(uid, "comment_send_error", error=payload)
                )
            await ctx_pop(uid)
    else:
        try:
            await event.reply(txt(uid, "received_comment"))
        except Exception:
            pass
        await start_continuous_report(event, uid, ctx, ctx["mode"], need_comment=True)
    raise events.StopPropagation

async def dialog_probe(
    cli,
    peer,
    next_opt: bytes | None,
    sess_id: str | None = None,
    *,
    prefer_peer: bool = False,
    path: list | None = None,
):
    async def _peer_report():
        if path:
            code = _reason_code_from_path(path)
        else:
            code = _peer_code_from_option(next_opt)
        ok, info = await _send_peer_report(
            cli,
            peer,
            code,
            "Violation of Telegram Terms of Service.",
            sess_id=sess_id,
            mode="profile" if prefer_peer else "dialog",
        )
        if ok:
            return ("done", None)
        if info.startswith("RPC_ERROR:") and any(
            x in info
            for x in (
                "FROZEN",
                "SESSION_REVOKED",
                "AUTH_KEY_UNREGISTERED",
                "USER_DEACTIVATED",
                "PHONE_NUMBER_BANNED",
            )
        ):
            if sess_id:
                await drop_account(sess_id, reason="frozen/unauthorized")
            return ("error", "REMOVED_FROZEN_OR_DELETED")
        return ("error", info)

    if prefer_peer:
        if next_opt is None:
            return ("choose", _peer_report_options())
        return await _peer_report()

    mid = await _ensure_reportable_msg_id(cli, peer)
    ids = [mid] if mid else []
    if not ids:
        if next_opt is None:
            return ("choose", _peer_report_options())
        return await _peer_report()
    if next_opt is not None and bytes(_norm_option(next_opt) or b"").startswith(b"pr:"):
        return await _peer_report()
    try:
        res = await cli(
            functions.messages.ReportRequest(
                peer=peer, id=ids, option=next_opt or b"", message=""
            )
        )
    except errors.RPCError as e:
        if sess_id and (_is_frozen(e) or _is_unauthorized_like(e)):
            await drop_account(sess_id, reason="frozen/unauthorized")
            return ("error", "REMOVED_FROZEN_OR_DELETED")
        return (
            "error",
            _report_rpc_fail(
                "messages.report",
                e,
                peer=peer,
                ids=ids,
                option=next_opt or b"",
                reason=_reason_code_from_path(path) if path else None,
                step=len(path or []),
                sess=sess_id,
                mode="dialog",
            ),
        )
    except Exception as e:
        return ("error", f"EXC_{e.__class__.__name__}")
    if isinstance(res, types.ReportResultChooseOption):
        return ("choose", res.options)
    if isinstance(res, types.ReportResultAddComment):
        return ("comment", res.option)
    if isinstance(res, types.ReportResultReported):
        return ("done", None)
    return ("error", "UNEXPECTED")

@bot.on(events.CallbackQuery(pattern=b"^db:"))
async def on_dialog_choose(event: events.CallbackQuery.Event):
    uid = UID(event)
    ctx = await ctx_get(uid)
    mode = (ctx or {}).get("mode")
    if not ctx or mode not in ("dialog", "profile"):
        await event.answer(txt(uid, "session_not_found"), alert=True)
        return
    data = event.data
    if data == b"db:cancel":
        await event.answer()
        try:
            await event.edit(txt(uid, "cancel"))
        except Exception:
            pass
        await ctx_pop(uid)
        return
    opt = _decode_opt_callback(data, "db:")
    if not opt:
        opt = _norm_option(data[3:])
    if not opt:
        await event.answer(txt(uid, "invalid_input"), alert=True)
        return
    await event.answer()
    sess0 = ctx.get("sample", {}).get("sess")
    if not sess0:
        sess0 = await resolve_probe_session(event, uid, ctx.get("pool") or [])
        if not sess0:
            await ctx_pop(uid)
            return
        ctx["sample"] = {"sess": sess0}
    cli, fail = await connect_session_client(sess0)
    if not cli:
        await event.respond(
            txt(uid, "sample_session_lost") + (f"\n{fail}" if fail else "")
        )
        await ctx_pop(uid)
        return
    try:
        if mode == "profile":
            peer = await _resolve_profile_peer(cli, ctx["target"], sess0)
            if not peer:
                await event.respond(txt(uid, "profile_not_user"))
                await ctx_pop(uid)
                return
        else:
            peer = await join_group(
                cli, ctx["target"], sess0, skip_join=ctx.get("skip_join", False)
            )
            if not _is_valid_peer(peer):
                await event.respond(txt(uid, "dialog_cannot_open"))
                await ctx_pop(uid)
                return
        ctx.setdefault("path", []).append(
            _path_step_from_option(
                uid,
                opt,
                options_list=_deserialize_report_options(ctx.get("last_report_options")),
            )
        )
        await ctx_set(uid, ctx)
        status, payload = await dialog_probe(
            cli,
            peer,
            next_opt=opt,
            sess_id=sess0,
            prefer_peer=(mode == "profile"),
            path=ctx.get("path") or [],
        )
        if status == "choose":
            _remember_report_options(ctx, payload)
            await ctx_set(uid, ctx)
            await event.edit(
                txt(uid, "select_next_option"),
                buttons=build_dialog_keyboard(uid, payload),
            )
        elif status == "comment":
            await ask_comment_source(
                event, uid, ctx, mode=mode, need_comment=True, sample_option=payload
            )
        elif status == "done":
            try:
                await event.edit(txt(uid, "path_complete"))
            except Exception:
                await event.respond(txt(uid, "path_complete"))
            await start_continuous_report(
                event, uid, ctx, mode, need_comment=True
            )
        else:
            await event.respond(_format_wizard_error(uid, payload))
            await ctx_pop(uid)
    finally:
        await _safe_disconnect(cli)

@bot.on(events.NewMessage)
async def on_dialog_comment(event: events.NewMessage.Event):
    uid = UID(event)
    if is_menu_or_command(event):
        await ctx_pop(uid)
        return
    ctx = await ctx_get(uid)
    if (
        not ctx
        or ctx.get("mode") not in ("dialog", "profile", "bot")
        or not ctx.get("awaiting_comment")
    ):
        return
    raw = event.raw_text or ""
    comments = _split_comments(raw)
    if not comments:
        await event.reply(txt(uid, "ai_invalid"))
        raise events.StopPropagation
    ctx["comments"] = comments
    ctx["comment"] = comments[0]
    ctx["comment_source"] = "text"
    ctx["awaiting_comment"] = False
    ctx.pop("comment_needs_sample", None)
    await ctx_set(uid, ctx)

    try:
        await event.reply(txt(uid, "received_comment"))
    except Exception:
        pass
    await start_continuous_report(event, uid, ctx, ctx["mode"], need_comment=True)
    raise events.StopPropagation

async def partner_menu(event):
    uid = UID(event)
    if not await has_access(uid):
        return

    if get_plus_subscription(uid):
        try:
            await event.edit(txt(uid, "plus_no_partner"))
        except Exception:
            await event.reply(txt(uid, "plus_no_partner"))
        return
    partners = await get_partners(uid)
    if partners:
        text = (
            txt(uid, "partner_menu_title")
            + "\n"
            + "\n".join(f"🔗[5271604874419647061] {p}" for p in partners)
        )
    else:
        text = txt(uid, "no_partners")
    btns = [
        [Button.inline(txt(uid, "add_partner_btn"), data=b"partner_add")],
        [Button.inline(txt(uid, "remove_partner_btn"), data=b"partner_remove")],
    ]
    try:
        await event.edit(text, buttons=btns)
    except Exception:
        await event.reply(text, buttons=btns)

@bot.on(events.CallbackQuery(pattern=b"partner_add"))
async def partner_add(event):
    uid = UID(event)
    if not await has_access(uid):
        return
    if await is_partner(uid):
        await event.answer(txt(uid, "partner_cannot_add"), alert=True)
        return
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                txt(uid, "partner_add_prompt"),
                buttons=conv_cancel_buttons(uid, b"partner_menu"),
            )
            resp = await menu_safe_response(conv)
            try:
                ent = await bot.get_entity(resp.raw_text.strip())
                partner_id = ent.id
            except Exception:
                try:
                    partner_id = int(resp.raw_text.strip())
                except Exception:
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
            existing_req = PARTNER_REQUESTS_COL.find_one(
                {"requester_id": uid, "partner_id": partner_id, "status": "pending"}
            )
            if existing_req:
                await conv.send_message(txt(uid, "partner_already_requested"))
                return
            if PARTNERS_COL.find_one(
                {"main_user_id": uid, "partner_user_id": partner_id}
            ):
                await conv.send_message(txt(uid, "partner_already_exists"))
                return
            req_doc = {
                "requester_id": uid,
                "partner_id": partner_id,
                "status": "pending",
                "requested_at": datetime.datetime.now(datetime.timezone.utc),
            }
            result = PARTNER_REQUESTS_COL.insert_one(req_doc)
            req_id = str(result.inserted_id)
            for admin_id in ADMIN_IDS:
                try:
                    notif_text = txt(
                        admin_id,
                        "partner_request_notification",
                        requester=uid,
                        partner=partner_id,
                    )
                    await bot.send_message(
                        admin_id,
                        notif_text,
                        buttons=[
                            [
                                Button.inline(
                                    txt(admin_id, "approve"),
                                    data=f"partner_req:approve:{req_id}".encode(),
                                ),
                                Button.inline(
                                    txt(admin_id, "reject"),
                                    data=f"partner_req:reject:{req_id}".encode(),
                                ),
                            ]
                        ],
                    )
                except Exception:
                    pass
            await conv.send_message(txt(uid, "partner_request_sent"))

    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

@bot.on(events.CallbackQuery(pattern=rb"partner_req:(approve|reject):"))
async def partner_request_handler(event):
    await event.answer()
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer(txt(uid, "only_admin"), alert=True)
        return
    parts = event.data.decode().split(":")
    action = parts[1]
    req_id = parts[2]
    req = PARTNER_REQUESTS_COL.find_one({"_id": ObjectId(req_id), "status": "pending"})
    if not req:
        try:
            await event.respond(
                txt(uid, "request_invalid"), buttons=admin_back_buttons(uid)
            )
        except Exception:
            await event.answer(txt(uid, "request_invalid"), alert=True)
        return
    requester_id = req["requester_id"]
    partner_id = req["partner_id"]
    if action == "approve":
        success, msg_key = await add_partner(requester_id, partner_id)
        if success:
            PARTNER_REQUESTS_COL.update_one(
                {"_id": ObjectId(req_id)},
                {
                    "$set": {
                        "status": "approved",
                        "resolved_by": uid,
                        "resolved_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                },
            )
            try:
                await bot.send_message(requester_id, txt(requester_id, msg_key))
            except Exception:
                pass
            try:
                await event.edit(
                    txt(
                        uid,
                        "partner_approve_admin",
                        requester=requester_id,
                        partner=partner_id,
                    ),
                    buttons=None,
                )
            except Exception:
                pass
        else:
            PARTNER_REQUESTS_COL.update_one(
                {"_id": ObjectId(req_id)},
                {
                    "$set": {
                        "status": "rejected",
                        "resolved_by": uid,
                        "resolved_at": datetime.datetime.now(datetime.timezone.utc),
                    }
                },
            )
            try:
                await bot.send_message(requester_id, txt(requester_id, msg_key))
            except Exception:
                pass
            await event.edit(
                txt(uid, "partner_approve_error", error=msg_key), buttons=None
            )
    else:
        PARTNER_REQUESTS_COL.update_one(
            {"_id": ObjectId(req_id)},
            {
                "$set": {
                    "status": "rejected",
                    "resolved_by": uid,
                    "resolved_at": datetime.datetime.now(datetime.timezone.utc),
                }
            },
        )
        try:
            await bot.send_message(
                requester_id,
                txt(requester_id, "partner_rejected_user", partner=partner_id),
            )
        except Exception:
            pass
        try:
            await event.edit(
                txt(
                    uid,
                    "partner_rejected_admin",
                    requester=requester_id,
                    partner=partner_id,
                ),
                buttons=None,
            )
        except Exception:
            pass

@bot.on(events.CallbackQuery(pattern=b"partner_remove"))
async def partner_remove(event):
    uid = UID(event)
    if not await has_access(uid):
        return
    partners = await get_partners(uid)
    if not partners:
        await event.answer(txt(uid, "no_partners"), alert=True)
        return
    rows = []
    for p in partners:
        rows.append(
            [
                Button.inline(
                    f"❌[5210952531676504517] {p}", data=f"partner_del:{p}".encode()
                )
            ]
        )
    rows.append([Button.inline(txt(uid, "back_btn"), data=b"partner_menu")])
    await event.edit(txt(uid, "select_to_remove"), buttons=rows)

@bot.on(events.CallbackQuery(pattern=b"partner_del:"))
async def partner_delete(event):
    uid = UID(event)
    if not await has_access(uid):
        return
    target = int(event.data.decode().split(":")[1])
    await remove_partner(uid, target)
    await event.answer(txt(uid, "deleted"), alert=True)
    await partner_menu(event)

@bot.on(events.CallbackQuery(pattern=b"partner_menu"))
async def back_to_partner_menu(event):
    await event.answer()
    await partner_menu(event)

RESELLERS_COL = db["resellers"]

def _reseller_redis_url(db_index: int) -> str:
    base = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    if re.search(r"/\d+$", base):
        return re.sub(r"/\d+$", f"/{int(db_index)}", base)
    return base.rstrip("/") + f"/{int(db_index)}"

def _reseller_pid_alive(pid: int) -> bool:
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _kill_pid(pid: int):
    pid = int(pid or 0)
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass

def _next_reseller_redis_db() -> int:
    used = set()
    for doc in RESELLERS_COL.find({}, {"redis_db": 1}):
        try:
            used.add(int(doc.get("redis_db") or 0))
        except Exception:
            pass
    for n in range(1, 16):
        if n not in used:
            return n
    raise RuntimeError("REDIS_DB_FULL")

def reseller_is_running(doc: dict) -> bool:
    return _reseller_pid_alive(int(doc.get("pid") or 0))

def stop_reseller_process(doc: dict, *, disable: bool):
    _kill_pid(int(doc.get("pid") or 0))
    update = {"pid": 0}
    if disable:
        update["enabled"] = False
    RESELLERS_COL.update_one({"_id": doc["_id"]}, {"$set": update})
    doc.update(update)

def start_reseller_process(doc: dict) -> int:
    rid = str(doc["_id"])
    admins = [int(x) for x in (doc.get("admin_ids") or [])]
    if not admins:
        raise RuntimeError("NO_ADMIN")
    env = os.environ.copy()
    env["RESELLER_BOT_TOKEN"] = str(doc.get("token") or "")
    env["RESELLER_DB_NAME"] = str(doc.get("db_name") or "")
    env["RESELLER_ADMINS"] = ",".join(str(x) for x in admins)
    env["RESELLER_SESSION"] = f"reseller_{rid}"
    env["RESELLER_LOG"] = f"reseller_{rid}.log"
    env["REDIS_URL"] = _reseller_redis_url(int(doc.get("redis_db") or 1))
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)],
        env=env,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
    )
    RESELLERS_COL.update_one(
        {"_id": doc["_id"]},
        {"$set": {"pid": proc.pid, "enabled": True}},
    )
    doc["pid"] = proc.pid
    doc["enabled"] = True
    return proc.pid

def resume_enabled_resellers():
    if IS_RESELLER:
        return
    for doc in RESELLERS_COL.find({"enabled": True}):
        if reseller_is_running(doc):
            continue
        try:
            start_reseller_process(doc)
            logger.info("reseller resumed id=%s pid=%s", doc.get("_id"), doc.get("pid"))
        except Exception as e:
            logger.warning("reseller resume failed id=%s: %s", doc.get("_id"), e)

def reseller_stats_text(uid: int, doc: dict) -> str:
    running = reseller_is_running(doc)
    users = accounts = subs = 0
    try:
        rdb = mongo[str(doc.get("db_name"))]
        users = rdb["bot_users"].count_documents({})
        accounts = rdb["accounts"].count_documents({})
        subs = rdb["subs_users"].count_documents({})
    except Exception as e:
        logger.warning("reseller stats failed: %s", e)
    admins = ", ".join(str(x) for x in (doc.get("admin_ids") or [])) or "—"
    uname = doc.get("bot_username") or "—"
    return txt(
        uid,
        "reseller_card",
        title=doc.get("title") or "—",
        username=uname,
        status=txt(uid, "reseller_on") if running else txt(uid, "reseller_off"),
        users=users,
        accounts=accounts,
        subs=subs,
        admins=admins,
    )

async def _lookup_bot(token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = r.json()
        if data.get("ok") and isinstance(data.get("result"), dict):
            return data["result"]
    except Exception as e:
        logger.warning("reseller getMe failed: %s", e)
    return None

def build_admin_buttons(uid):
    rows = [
        [
            Button.inline(txt(uid, "admin_users"), b"users"),
            Button.inline(txt(uid, "admin_add_user"), b"add_user"),
        ],
        [Button.inline(txt(uid, "admin_transfer_accounts"), b"transfer_accounts")],
        [Button.inline(txt(uid, "admin_partner_requests"), b"partner_requests_list")],
        [Button.inline(txt(uid, "admin_plus_manage"), b"plus_manage")],
        [
            Button.inline(txt(uid, "admin_user_list"), b"admin_user_list"),
            Button.inline(
                txt(uid, "admin_delete_all_sessions"), b"admin_delete_all_sessions"
            ),
        ],
        [
            Button.inline(txt(uid, "admin_add_admin"), b"add_admin"),
            Button.inline(txt(uid, "admin_remove_admin"), b"remove_admin"),
        ],
        [Button.inline(txt(uid, "admin_set_start_msg"), b"set_start_msg")],
        [Button.inline(txt(uid, "admin_broadcast"), b"broadcast")],
        [Button.inline(txt(uid, "admin_force_join"), b"fj_menu")],
        [Button.inline(txt(uid, "admin_restart_bot"), b"restart_bot")],
        [
            Button.inline(txt(uid, "admin_set_prices"), b"set_prices"),
            Button.inline(
                txt(uid, "manage_purchase_requests"), b"manage_purchase_requests"
            ),
        ],
        [Button.inline(txt(uid, "admin_set_bank"), b"set_bank")],
        [Button.inline(txt(uid, "admin_crypto_pay"), b"crypto_menu")],
        [Button.inline(txt(uid, "admin_referral"), b"ref_menu")],
        [Button.inline(txt(uid, "admin_proxy_manage"), b"proxy_menu")],
        [Button.inline(txt(uid, "admin_account_health"), b"acc_health")],
        [Button.inline(txt(uid, "admin_report_notify"), b"rnch:menu")],
        [Button.inline(txt(uid, "admin_ai_comments"), b"ai_menu")],
        [
            Button.inline(txt(uid, "admin_edit_texts"), b"edit_texts"),
            Button.inline(txt(uid, "admin_permissions"), b"admin_permissions"),
        ],
    ]
    if not IS_RESELLER:
        rows.append([Button.inline(txt(uid, "admin_resellers"), b"rsl:menu")])
    return rows

@bot.on(events.CallbackQuery(pattern=rb"^rsl:"))
async def on_reseller_panel(event):
    uid = UID(event)
    if IS_RESELLER or uid not in ADMIN_IDS:
        return
    await event.answer()
    data = event.data.decode()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    def _doc(rid: str):
        try:
            return RESELLERS_COL.find_one({"_id": ObjectId(rid)})
        except Exception:
            return None

    async def _show_menu():
        rows = [[Button.inline(txt(uid, "reseller_add_btn"), data=b"rsl:add")]]
        for doc in RESELLERS_COL.find().sort("created_at", -1).limit(20):
            mark = "🟢" if reseller_is_running(doc) else "🔴"
            title = str(doc.get("title") or "bot")[:24]
            rows.append(
                [
                    Button.inline(
                        f"{mark} {title}",
                        data=f"rsl:open:{doc['_id']}".encode(),
                    )
                ]
            )
        rows.append([Button.inline(txt(uid, "back_btn"), data=b"admin_back")])
        try:
            await event.edit(txt(uid, "reseller_menu"), buttons=rows)
        except Exception:
            await event.respond(txt(uid, "reseller_menu"), buttons=rows)

    async def _show_one(doc):
        rid = str(doc["_id"])
        rows = []
        if reseller_is_running(doc):
            rows.append([Button.inline(txt(uid, "reseller_stop_btn"), data=f"rsl:off:{rid}".encode())])
        else:
            rows.append([Button.inline(txt(uid, "reseller_start_btn"), data=f"rsl:on:{rid}".encode())])
        rows.append(
            [
                Button.inline(txt(uid, "reseller_add_admin_btn"), data=f"rsl:addadm:{rid}".encode()),
                Button.inline(txt(uid, "reseller_del_admin_btn"), data=f"rsl:adms:{rid}".encode()),
            ]
        )
        rows.append([Button.inline(txt(uid, "reseller_delete_btn"), data=f"rsl:delask:{rid}".encode())])
        rows.append([Button.inline(txt(uid, "back_btn"), data=b"rsl:menu")])
        text = reseller_stats_text(uid, doc)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)

    if action == "menu":
        await _show_menu()
        return

    if action == "add":
        try:
            async with standard_conversation(uid, timeout=180) as conv:
                await conv.send_message(
                    txt(uid, "reseller_ask_title"),
                    buttons=conv_cancel_buttons(uid, b"admin_back"),
                )
                title = ((await menu_safe_response(conv)).raw_text or "").strip()[:40]
                if not title:
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
                await conv.send_message(
                    txt(uid, "reseller_ask_token"),
                    buttons=conv_cancel_buttons(uid, b"admin_back"),
                )
                token = ((await menu_safe_response(conv)).raw_text or "").strip()
                ident = await _lookup_bot(token)
                if not ident or not ident.get("id"):
                    await conv.send_message(txt(uid, "reseller_bad_token"))
                    return
                if token == bot_token or RESELLERS_COL.find_one({"token": token}):
                    await conv.send_message(txt(uid, "reseller_token_used"))
                    return
                await conv.send_message(
                    txt(uid, "reseller_ask_admin"),
                    buttons=conv_cancel_buttons(uid, b"admin_back"),
                )
                raw_admin = ((await menu_safe_response(conv)).raw_text or "").strip()
                if not raw_admin.lstrip("-").isdigit():
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
                admin_id = int(raw_admin)
                try:
                    redis_db = _next_reseller_redis_db()
                except RuntimeError:
                    await conv.send_message(txt(uid, "reseller_redis_full"))
                    return
                slug = secrets.token_hex(4)
                safe_name = re.sub(r"[^A-Za-z0-9_]", "", NAME_DB) or "report"
                doc = {
                    "title": title,
                    "token": token,
                    "bot_username": ident.get("username") or "",
                    "bot_id": int(ident.get("id") or 0),
                    "db_name": f"{safe_name}_r{slug}",
                    "redis_db": redis_db,
                    "admin_ids": [admin_id],
                    "enabled": False,
                    "pid": 0,
                    "created_at": _ts_now(),
                    "created_by": uid,
                }
                ins = RESELLERS_COL.insert_one(doc)
                doc["_id"] = ins.inserted_id
                try:
                    start_reseller_process(doc)
                except Exception as e:
                    logger.warning("reseller start failed: %s", e)
                    await conv.send_message(txt(uid, "reseller_start_failed", error=e.__class__.__name__))
                    return
                await conv.send_message(
                    txt(uid, "reseller_created", username=doc.get("bot_username") or title, admin=admin_id)
                )
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                await bot.send_message(uid, txt(uid, "timeout_or_invalid"))
            except Exception:
                pass
        return

    rid = parts[2] if len(parts) > 2 else ""
    doc = _doc(rid)
    if not doc:
        await event.answer(txt(uid, "invalid_input"), alert=True)
        return

    if action == "open":
        await _show_one(doc)
        return
    if action == "on":
        if not reseller_is_running(doc):
            try:
                start_reseller_process(doc)
            except Exception as e:
                logger.warning("reseller on failed: %s", e)
                await event.respond(txt(uid, "reseller_start_failed", error=e.__class__.__name__))
                return
        doc = _doc(rid) or doc
        await _show_one(doc)
        return
    if action == "off":
        stop_reseller_process(doc, disable=True)
        doc = _doc(rid) or doc
        await _show_one(doc)
        return
    if action == "addadm":
        try:
            async with standard_conversation(uid, timeout=90) as conv:
                await conv.send_message(
                    txt(uid, "reseller_ask_admin"),
                    buttons=conv_cancel_buttons(uid, b"admin_back"),
                )
                raw_admin = ((await menu_safe_response(conv)).raw_text or "").strip()
                if not raw_admin.lstrip("-").isdigit():
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
                admin_id = int(raw_admin)
                ids = [int(x) for x in (doc.get("admin_ids") or [])]
                if admin_id not in ids:
                    ids.append(admin_id)
                RESELLERS_COL.update_one({"_id": doc["_id"]}, {"$set": {"admin_ids": ids}})
                doc["admin_ids"] = ids
                if doc.get("enabled") or reseller_is_running(doc):
                    stop_reseller_process(doc, disable=False)
                    start_reseller_process(doc)
                await conv.send_message(txt(uid, "reseller_admin_added", admin=admin_id))
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            return
        return
    if action == "adms":
        rows = []
        for aid in doc.get("admin_ids") or []:
            rows.append(
                [Button.inline(f"❌ {aid}", data=f"rsl:rm:{rid}:{aid}".encode())]
            )
        rows.append([Button.inline(txt(uid, "back_btn"), data=f"rsl:open:{rid}".encode())])
        await event.edit(txt(uid, "reseller_admins_title"), buttons=rows)
        return
    if action == "rm":
        try:
            drop_id = int(parts[3])
        except Exception:
            return
        ids = [int(x) for x in (doc.get("admin_ids") or []) if int(x) != drop_id]
        if not ids:
            await event.answer(txt(uid, "reseller_need_admin"), alert=True)
            return
        RESELLERS_COL.update_one({"_id": doc["_id"]}, {"$set": {"admin_ids": ids}})
        doc["admin_ids"] = ids
        if reseller_is_running(doc):
            stop_reseller_process(doc, disable=False)
            start_reseller_process(doc)
        doc = _doc(rid) or doc
        await _show_one(doc)
        return
    if action == "delask":
        await event.edit(
            txt(uid, "reseller_delete_confirm", title=doc.get("title") or ""),
            buttons=[
                [Button.inline(txt(uid, "reseller_delete_btn"), data=f"rsl:del:{rid}".encode())],
                [Button.inline(txt(uid, "back_btn"), data=f"rsl:open:{rid}".encode())],
            ],
        )
        return
    if action == "del":
        stop_reseller_process(doc, disable=True)
        RESELLERS_COL.delete_one({"_id": doc["_id"]})
        await _show_menu()
        return

@bot.on(events.CallbackQuery(pattern=b"rnjoin:check"))
async def on_report_join_check(event):
    uid = UID(event)
    ch = _report_channel_ref()
    if uid in ADMIN_IDS or not ch or await check_user_force_joined(uid, ch):
        try:
            await event.answer(txt(uid, "report_join_ok"))
        except Exception:
            pass
        ctx = await ctx_get(uid) or {}
        resume = ctx.pop("pending_report_resume", None)
        if ctx:
            await ctx_set(uid, ctx)
        else:
            await ctx_pop(uid)
        try:
            await event.edit(txt(uid, "report_join_ok"))
        except Exception:
            await event.respond(txt(uid, "report_join_ok"))
        if resume:
            await _continue_after_report_join(event, uid, resume)
        return
    try:
        await event.answer(txt(uid, "report_join_still_missing"), alert=True)
    except Exception:
        pass
    text, rows = await build_report_join_prompt(uid, ch)
    try:
        await event.edit(text, buttons=rows)
    except Exception:
        pass

@bot.on(events.CallbackQuery(pattern=rb"^rnch:(menu|set|clear)$"))
async def on_report_notify_channel(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    await event.answer()
    action = event.data.decode().split(":")[1]

    async def _menu():
        cur = get_report_notify_channel() or "—"
        text = txt(uid, "report_notify_menu", channel=cur)
        buttons = [
            [Button.inline(txt(uid, "report_notify_set"), data=b"rnch:set")],
            [Button.inline(txt(uid, "report_notify_clear"), data=b"rnch:clear")],
            [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
        ]
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)

    if action == "menu":
        await _menu()
        return
    if action == "clear":
        set_report_notify_channel("")
        await _menu()
        return
    try:
        async with standard_conversation(uid, timeout=90) as conv:
            await conv.send_message(
                txt(uid, "report_notify_ask"),
                buttons=conv_cancel_buttons(uid, b"admin_back"),
            )
            raw = ((await menu_safe_response(conv)).raw_text or "").strip()
            channel, err = await _resolve_report_notify_input(raw)
            if err or not channel:
                await conv.send_message(txt(uid, err or "report_notify_bad"))
                return
            set_report_notify_channel(channel)
            label = channel.get("title") or channel.get("chat_id")
            try:
                await bot.send_message(_notify_send_target(channel), txt(uid, "report_notify_test"))
                await conv.send_message(txt(uid, "report_notify_saved", channel=label))
            except Exception as e:
                logger.warning("notify channel test failed: %s", e)
                await conv.send_message(txt(uid, "report_notify_not_admin", channel=label))
            saved = _report_channel_ref() or channel
            join_url = await _report_channel_join_url(saved)
            if join_url:
                await conv.send_message(txt(uid, "report_notify_join_link", url=join_url))
            else:
                await conv.send_message(txt(uid, "report_notify_no_link", channel=label))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            await bot.send_message(uid, txt(uid, "timeout_or_invalid"))
        except Exception:
            pass

@bot.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_entry(event: events.NewMessage.Event):
    sender_id = event.sender_id
    if sender_id not in ADMIN_IDS:
        return
    await ctx_pop(sender_id)
    btn = build_admin_buttons(sender_id)
    try:
        await event.edit(txt(sender_id, "admin_menu_hello"), buttons=btn)
    except Exception:
        await event.respond(txt(sender_id, "admin_menu_hello"), buttons=btn)

_broadcast_running = False
_BROADCAST_SRC: dict[int, object] = {}

def _broadcast_file_media(msg) -> bool:
    media = getattr(msg, "media", None)
    if not media:
        return False
    if isinstance(media, types.MessageMediaWebPage):
        return False
    return True

def collect_broadcast_recipients() -> list[int]:
    ids: set[int] = set()
    cols = [SUBS_COL, PLUS_SUBS_COL, USER_SETTINGS_COL]
    try:
        cols.append(BOT_USERS_COL)
    except Exception:
        pass
    for col in cols:
        try:
            for doc in col.find({}, {"user_id": 1}):
                try:
                    uid = int(doc.get("user_id"))
                    if uid > 0:
                        ids.add(uid)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        for aid in db["accounts"].distinct("admin_id"):
            try:
                uid = int(aid)
                if uid > 0:
                    ids.add(uid)
            except Exception:
                pass
    except Exception:
        pass
    for aid in ADMIN_IDS:
        try:
            uid = int(aid)
            if uid > 0:
                ids.add(uid)
        except Exception:
            pass
    return sorted(ids)

async def _broadcast_send_one(uid: int, src_msg, *, text_fallback: str = "") -> str:
    text = ""
    if src_msg is not None:
        text = (getattr(src_msg, "message", None) or "").strip()
    if not text:
        text = (text_fallback or "").strip()
    has_file = src_msg is not None and _broadcast_file_media(src_msg)

    async def _send_text() -> str:
        if not text:
            return "fail"
        await bot.send_message(uid, text)
        return "ok"

    try:
        try:
            await bot.get_input_entity(uid)
        except Exception:
            pass
        if has_file:
            try:
                await bot.send_file(
                    uid,
                    src_msg.media,
                    caption=text[:1024] or None,
                )
                return "ok"
            except Exception as e:
                logger.warning("broadcast media fallback uid=%s: %s", uid, e)
                return await _send_text()
        return await _send_text()
    except errors.FloodWaitError as e:
        wait = min(60, int(getattr(e, "seconds", 1) or 1) + 1)
        await asyncio.sleep(wait)
        try:
            if has_file:
                await bot.send_file(uid, src_msg.media, caption=text[:1024] or None)
            else:
                return await _send_text()
            return "ok"
        except Exception:
            try:
                return await _send_text()
            except Exception:
                return "fail"
    except (
        errors.UserIsBlockedError,
        errors.InputUserDeactivatedError,
        errors.PeerIdInvalidError,
        errors.UserIdInvalidError,
    ):
        return "blocked"
    except Exception as e:
        name = e.__class__.__name__.upper()
        msg = (getattr(e, "message", None) or str(e) or "").upper()
        if any(
            x in name or x in msg
            for x in (
                "BLOCKED",
                "DEACTIVATED",
                "PEER_ID_INVALID",
                "USER_ID_INVALID",
                "CHAT_WRITE_FORBIDDEN",
                "USER_IS_BOT",
                "BOT_METHOD_INVALID",
            )
        ):
            return "blocked"
        if text:
            try:
                await bot.send_message(uid, text)
                return "ok"
            except Exception:
                pass
        logger.warning("broadcast send fail uid=%s: %s", uid, e)
        return "fail"

async def run_broadcast(
    admin_id: int,
    from_chat: int,
    msg_id: int,
    recipients: list[int],
    *,
    text_fallback: str = "",
):
    global _broadcast_running
    started = _ts_now()
    ok = blocked = fail = 0
    try:
        src_msg = _BROADCAST_SRC.pop(int(admin_id), None)
        if isinstance(src_msg, list):
            src_msg = src_msg[0] if src_msg else None
        if src_msg is None:
            try:
                src_msg = await bot.get_messages(from_chat, ids=msg_id)
            except Exception as e:
                logger.warning("broadcast get_messages chat=%s id=%s: %s", from_chat, msg_id, e)
            if isinstance(src_msg, list):
                src_msg = src_msg[0] if src_msg else None
        if not src_msg:
            try:
                peer = await bot.get_input_entity(from_chat)
                src_msg = await bot.get_messages(peer, ids=msg_id)
            except Exception as e:
                logger.warning("broadcast get_messages retry: %s", e)
            if isinstance(src_msg, list):
                src_msg = src_msg[0] if src_msg else None
        if not src_msg and not (text_fallback or "").strip():
            await bot.send_message(admin_id, txt(admin_id, "broadcast_invalid"))
            return

        total = len(recipients)
        for i, uid in enumerate(recipients, 1):
            if uid == admin_id:

                pass
            result = await _broadcast_send_one(
                uid, src_msg, text_fallback=text_fallback
            )
            if result == "ok":
                ok += 1
            elif result == "blocked":
                blocked += 1
            else:
                fail += 1
            if i % 25 == 0:
                try:
                    await bot.send_message(
                        admin_id,
                        txt(
                            admin_id,
                            "broadcast_progress",
                            done=i,
                            total=total,
                            ok=ok,
                            blocked=blocked,
                            fail=fail,
                        ),
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.07)
    except Exception as e:
        logger.exception("broadcast crashed: %s", e)
        try:
            await bot.send_message(
                admin_id, txt(admin_id, "broadcast_invalid") + f"\n{e}"
            )
        except Exception:
            pass
    finally:
        _broadcast_running = False
        _BROADCAST_SRC.pop(int(admin_id), None)
    elapsed = max(1, _ts_now() - started)
    try:
        await bot.send_message(
            admin_id,
            txt(
                admin_id,
                "broadcast_report",
                ok=ok,
                blocked=blocked,
                fail=fail,
                total=len(recipients),
                seconds=elapsed,
            ),
            buttons=admin_back_buttons(admin_id),
        )
    except Exception:
        pass

@bot.on(events.CallbackQuery(pattern=rb"^broadcast$|^bc_(yes|no)$"))
async def broadcast_admin_handler(event):
    global _broadcast_running
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer()
        return
    data = (event.data or b"").decode()

    if data == "broadcast":
        if _broadcast_running:
            await event.answer(txt(uid, "broadcast_busy"), alert=True)
            return
        await event.answer()
        await ctx_set(uid, {"mode": "broadcast", "step": "await_message"})
        try:
            await event.edit(
                txt(uid, "broadcast_prompt"),
                buttons=conv_cancel_buttons(uid, b"admin_back"),
            )
        except Exception:
            await event.respond(
                txt(uid, "broadcast_prompt"),
                buttons=conv_cancel_buttons(uid, b"admin_back"),
            )
        raise events.StopPropagation

    if data == "bc_no":
        await event.answer()
        await ctx_pop(uid)
        _BROADCAST_SRC.pop(int(uid), None)
        try:
            await event.edit(txt(uid, "broadcast_cancelled"), buttons=admin_back_buttons(uid))
        except Exception:
            await event.respond(
                txt(uid, "broadcast_cancelled"), buttons=admin_back_buttons(uid)
            )
        raise events.StopPropagation

    if data == "bc_yes":
        ctx = await ctx_get(uid)
        if not ctx or ctx.get("mode") != "broadcast" or ctx.get("step") != "confirm":
            await event.answer(txt(uid, "broadcast_invalid"), alert=True)
            return
        if _broadcast_running:
            await event.answer(txt(uid, "broadcast_busy"), alert=True)
            return
        recipients = collect_broadcast_recipients()
        if not recipients:
            await event.answer(txt(uid, "broadcast_empty"), alert=True)
            await ctx_pop(uid)
            return
        from_chat = int(ctx.get("from_chat") or event.chat_id or uid)
        msg_id = int(ctx.get("msg_id") or 0)
        text_fallback = str(ctx.get("text") or "")
        if not msg_id and not text_fallback.strip():
            await event.answer(txt(uid, "broadcast_invalid"), alert=True)
            return
        await event.answer()
        await ctx_pop(uid)
        _broadcast_running = True
        try:
            await event.edit(txt(uid, "broadcast_started", count=len(recipients)))
        except Exception:
            await event.respond(txt(uid, "broadcast_started", count=len(recipients)))
        bot.loop.create_task(
            run_broadcast(
                uid,
                from_chat,
                msg_id,
                recipients,
                text_fallback=text_fallback,
            )
        )
        raise events.StopPropagation

@bot.on(events.NewMessage(incoming=True))
async def broadcast_message_capture(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "broadcast" or ctx.get("step") != "await_message":
        return

    text = (event.raw_text or event.message.message or "").strip()
    has_media = _broadcast_file_media(event.message)
    if not text and not has_media:
        await event.reply(txt(uid, "broadcast_invalid"))
        raise events.StopPropagation

    recipients = collect_broadcast_recipients()
    if not recipients:
        await ctx_pop(uid)
        await event.reply(txt(uid, "broadcast_empty"), buttons=admin_back_buttons(uid))
        raise events.StopPropagation

    preview = (text or "—")[:400]
    _BROADCAST_SRC[int(uid)] = event.message
    ctx = {
        "mode": "broadcast",
        "step": "confirm",
        "from_chat": int(event.chat_id),
        "msg_id": int(event.id),
        "text": text,
        "has_media": has_media,
    }
    await ctx_set(uid, ctx)
    key = "broadcast_confirm_media" if has_media else "broadcast_confirm"
    buttons = [
        [Button.inline(txt(uid, "broadcast_yes"), data=b"bc_yes")],
        [Button.inline(txt(uid, "broadcast_no"), data=b"bc_no")],
        [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
    ]
    await event.reply(
        txt(uid, key, count=len(recipients), preview=preview),
        buttons=buttons,
    )
    raise events.StopPropagation

def build_fj_menu(uid: int):
    cfg = get_force_join()
    status = txt(uid, "fj_status_on") if cfg.get("enabled") else txt(uid, "fj_status_off")
    channels = cfg.get("channels") or []
    text = txt(uid, "fj_menu_text", status=status, count=len(channels))
    rows = [
        [Button.inline(txt(uid, "fj_toggle_btn", status=status), data=b"fj_toggle")],
        [Button.inline(txt(uid, "fj_add_btn"), data=b"fj_add")],
    ]
    for ch in channels:
        cid = ch.get("id") or ch.get("chat_id")
        title = (ch.get("title") or cid or "?")[:40]
        rows.append(
            [
                Button.inline(
                    txt(uid, "fj_del_btn", title=title),
                    data=f"fj_del={cid}".encode(),
                )
            ]
        )
    rows.append([Button.inline(txt(uid, "back_btn"), data=b"admin_back")])
    return text, rows

@bot.on(
    events.CallbackQuery(
        pattern=rb"^fj_(menu|toggle|add|check)$|^fj_del="
    )
)
async def force_join_handler(event):
    uid = UID(event)
    data = (event.data or b"").decode()

    if data == "fj_check":
        if uid in ADMIN_IDS:
            await event.answer()
            await start_menu(event)
            raise events.StopPropagation
        missing = await get_missing_force_joins(uid)
        if missing:
            names = "\n".join(f"• {c.get('title') or c.get('chat_id')}" for c in missing)
            try:
                await event.answer(txt(uid, "fj_still_missing", list=names)[:180], alert=True)
            except Exception:
                pass
            await show_force_join_gate(event, uid, missing)
            raise events.StopPropagation
        try:
            await event.answer(txt(uid, "fj_ok"), alert=False)
        except Exception:
            pass
        try:
            final_payload = await _pop_pending_ref(uid)
            await process_start_referral(uid, final_payload)
        except Exception as e:
            logger.warning("referral after force-join failed: %s", e)
        custom = db.settings.find_one({"key": "start_message"})
        welcome = (
            custom["value"]
            if custom and "value" in custom
            else txt(uid, "main_menu_welcome")
        )
        if not await has_access(uid):
            welcome += "\n\n" + txt(uid, "no_access")
        try:
            await event.edit(welcome, buttons=await kb_main(uid))
        except Exception:
            await event.respond(welcome, buttons=await kb_main(uid))
        raise events.StopPropagation

    if uid not in ADMIN_IDS:
        await event.answer()
        return

    if data == "fj_menu":
        await event.answer()
        text, rows = build_fj_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "fj_toggle":
        cfg = get_force_join()
        save_force_join({"enabled": not bool(cfg.get("enabled"))})
        await event.answer()
        text, rows = build_fj_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data.startswith("fj_del="):
        cid = data.split("=", 1)[1]
        cfg = get_force_join()
        channels = [
            c
            for c in (cfg.get("channels") or [])
            if str(c.get("id") or "") != cid and str(c.get("chat_id") or "") != cid
        ]
        save_force_join({"channels": channels})
        await event.answer(txt(uid, "fj_deleted"), alert=False)
        text, rows = build_fj_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        raise events.StopPropagation

    if data == "fj_add":
        await event.answer()
        await ctx_set(uid, {"mode": "force_join_add", "step": "title"})
        try:
            await event.edit(
                txt(uid, "fj_prompt_title"),
                buttons=conv_cancel_buttons(uid, b"fj_menu"),
            )
        except Exception:
            await event.respond(
                txt(uid, "fj_prompt_title"),
                buttons=conv_cancel_buttons(uid, b"fj_menu"),
            )
        raise events.StopPropagation

@bot.on(events.NewMessage)
async def force_join_add_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "force_join_add":
        return
    step = ctx.get("step")
    raw = (event.raw_text or "").strip()
    if not raw:
        await event.reply(txt(uid, "fj_invalid"))
        raise events.StopPropagation

    if step == "title":
        ctx["title"] = raw[:64]
        ctx["step"] = "channel"
        await ctx_set(uid, ctx)
        await event.reply(
            txt(uid, "fj_prompt_channel"),
            buttons=conv_cancel_buttons(uid, b"fj_menu"),
        )
        raise events.StopPropagation

    if step == "channel":
        chat_input = raw
        pending_invite = ""
        if "t.me/" in chat_input:
            full = chat_input if chat_input.startswith("http") else f"https://{chat_input.lstrip('/')}"
            path = full.split("t.me/", 1)[-1].split("?")[0].strip("/")
            if path.startswith("+") or path.startswith("joinchat/"):
                pending_invite = full
                ctx["pending_invite"] = pending_invite
                ctx["step"] = "channel_id_for_invite"
                await ctx_set(uid, ctx)
                await event.reply(
                    txt(uid, "fj_prompt_channel") + "\n(-100...)",
                    buttons=conv_cancel_buttons(uid, b"fj_menu"),
                )
                raise events.StopPropagation
            chat_input = path

        try:
            if str(chat_input).lstrip("-").isdigit():
                entity = await bot.get_entity(int(chat_input))
            else:
                entity = await bot.get_entity(str(chat_input).lstrip("@"))
            chat_id = await bot.get_peer_id(entity)
            username = (getattr(entity, "username", None) or "").strip()
            url = f"https://t.me/{username}" if username else (ctx.get("pending_invite") or "")
            if not url:
                ctx["pending_chat_id"] = str(chat_id)
                ctx["pending_username"] = ""
                ctx["step"] = "url"
                await ctx_set(uid, ctx)
                await event.reply(
                    txt(uid, "fj_prompt_url"),
                    buttons=conv_cancel_buttons(uid, b"fj_menu"),
                )
                raise events.StopPropagation
            await _fj_save_channel(
                uid,
                event,
                title=ctx.get("title") or "Channel",
                chat_id=str(chat_id),
                username=username,
                url=url,
            )
        except events.StopPropagation:
            raise
        except Exception as e:
            logger.warning("force join resolve failed: %s", e)
            await event.reply(txt(uid, "fj_resolve_fail"))
        raise events.StopPropagation

    if step == "channel_id_for_invite":
        invite = ctx.get("pending_invite") or ""
        try:
            if raw.lstrip("-").isdigit():
                entity = await bot.get_entity(int(raw))
            else:
                entity = await bot.get_entity(raw.lstrip("@"))
            chat_id = await bot.get_peer_id(entity)
            await _fj_save_channel(
                uid,
                event,
                title=ctx.get("title") or "Channel",
                chat_id=str(chat_id),
                username="",
                url=invite,
            )
        except Exception as e:
            logger.warning("force join invite resolve failed: %s", e)
            await event.reply(txt(uid, "fj_resolve_fail"))
        raise events.StopPropagation

    if step == "url":
        url = raw
        if not url.startswith("http"):
            if url.startswith("t.me/"):
                url = "https://" + url
            else:
                await event.reply(txt(uid, "fj_invalid"))
                raise events.StopPropagation
        await _fj_save_channel(
            uid,
            event,
            title=ctx.get("title") or "Channel",
            chat_id=str(ctx.get("pending_chat_id") or ""),
            username=str(ctx.get("pending_username") or ""),
            url=url,
        )
        raise events.StopPropagation

async def _fj_save_channel(uid, event, *, title: str, chat_id: str, username: str, url: str):
    cfg = get_force_join()
    channels = list(cfg.get("channels") or [])
    if any(str(c.get("chat_id")) == str(chat_id) for c in channels):
        await event.reply(txt(uid, "fj_exists"))
        await ctx_pop(uid)
        text, rows = build_fj_menu(uid)
        await event.respond(text, buttons=rows)
        return
    entry = {
        "id": str(chat_id),
        "chat_id": str(chat_id),
        "title": (title or "Channel")[:64],
        "username": (username or "").lstrip("@"),
        "url": url or "",
    }
    if not entry["url"] and entry["username"]:
        entry["url"] = f"https://t.me/{entry['username']}"
    channels.append(entry)
    save_force_join({"channels": channels, "enabled": True if channels else cfg.get("enabled")})
    await ctx_pop(uid)
    await event.reply(txt(uid, "fj_added"))
    text, rows = build_fj_menu(uid)
    await event.respond(text, buttons=rows)

def _proxy_mask(p: dict) -> str:
    host = p.get("host", "?")
    port = p.get("port", "?")
    ptype = (p.get("type") or "socks5").lower()
    kind = "MT" if ptype == "mtproto" else "S5"
    warn = "⚠" if p.get("fakestls") else ""
    auth = "🔐" if (p.get("username") or p.get("secret")) else "🔓"
    fails = int(p.get("fails") or 0)
    en = "✅" if p.get("enabled", True) else "⛔"
    return (
        f"{en}{auth}{warn}[{kind}] {host}:{port}"
        + (f" (fail:{fails})" if fails else "")
    )

def build_proxy_menu(uid: int, page: int = 0):
    cfg = get_proxy_config()
    proxies = cfg.get("proxies") or []
    enabled = cfg.get("enabled", False)
    status = txt(uid, "proxy_status_on") if enabled else txt(uid, "proxy_status_off")
    text = txt(
        uid,
        "proxy_menu_text",
        status=status,
        count=len(proxies),
        active=len([p for p in proxies if p.get("enabled", True)]),
    )
    page_size = 6
    total_pages = max(1, (len(proxies) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    chunk = proxies[page * page_size : (page + 1) * page_size]
    rows = [
        [
            Button.inline(
                txt(uid, "proxy_toggle_btn", status=status), data=b"proxy_toggle"
            )
        ],
        [
            Button.inline(txt(uid, "proxy_add_btn"), data=b"proxy_add"),
            Button.inline(txt(uid, "proxy_import_file_btn"), data=b"proxy_import_file"),
        ],
    ]
    for p in chunk:
        pid = p.get("id", "")
        rows.append(
            [
                Button.inline(_proxy_mask(p), data=f"proxy_info={pid}".encode()),
                Button.inline(
                    txt(uid, "proxy_test_btn"), data=f"proxy_test={pid}".encode()
                ),
                Button.inline(
                    txt(uid, "proxy_delete_btn"), data=f"proxy_del={pid}".encode()
                ),
            ]
        )
    nav = []
    if page > 0:
        nav.append(
            Button.inline(txt(uid, "prev_page"), data=f"proxy_page={page-1}".encode())
        )
    if page < total_pages - 1:
        nav.append(
            Button.inline(txt(uid, "next_page"), data=f"proxy_page={page+1}".encode())
        )
    if nav:
        rows.append(nav)
    if proxies:
        rows.append(
            [Button.inline(txt(uid, "proxy_clear_fails_btn"), data=b"proxy_clear_fails")]
        )
    rows.append([Button.inline(txt(uid, "back_btn"), data=b"admin_back")])
    return text, rows

@bot.on(events.CallbackQuery(pattern=rb"^proxy_(menu|toggle|add|import_file|clear_fails)$|^proxy_(page|del|info|test)="))
async def proxy_admin_handler(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer()
        return
    data = event.data.decode()

    if data.startswith("proxy_test="):
        pid = data.split("=", 1)[1]
        await event.answer(txt(uid, "proxy_testing"), alert=False)
        result = await test_proxy_connection(pid)
        try:
            await event.respond(f"🔌 تست پروکسی:\n{result}")
        except Exception:
            pass
        text, rows = build_proxy_menu(uid, 0)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        return

    await event.answer()

    if data == "proxy_menu" or data.startswith("proxy_page="):
        page = int(data.split("=", 1)[1]) if data.startswith("proxy_page=") else 0
        text, rows = build_proxy_menu(uid, page)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        return

    if data == "proxy_toggle":
        new_state = toggle_proxy_system()
        text, rows = build_proxy_menu(uid, 0)
        try:
            await event.edit(
                text
                + "\n\n"
                + txt(uid, "proxy_toggled_on" if new_state else "proxy_toggled_off"),
                buttons=rows,
            )
        except Exception:
            await event.respond(text, buttons=rows)
        return

    if data == "proxy_clear_fails":
        cfg = get_proxy_config()
        for p in cfg.get("proxies") or []:
            p["fails"] = 0
        save_proxy_config(cfg.get("enabled", False), cfg.get("proxies") or [])
        await event.answer(txt(uid, "proxy_fails_cleared"), alert=False)
        text, rows = build_proxy_menu(uid, 0)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        return

    if data == "proxy_import_file":
        added = 0
        skipped = 0
        if not os.path.exists("proxy.txt"):
            await event.answer(txt(uid, "proxy_file_missing"), alert=True)
            return
        cfg = get_proxy_config()
        proxies = cfg.get("proxies") or []
        try:
            with open("proxy.txt", "r", encoding="utf-8") as f:
                for line in f:
                    parsed = parse_proxy_line(line)
                    if not parsed:
                        continue
                    if any(p.get("id") == parsed["id"] for p in proxies):
                        skipped += 1
                        continue
                    proxies.append(parsed)
                    added += 1
        except Exception as e:
            await event.answer(str(e)[:100], alert=True)
            return
        save_proxy_config(True if added else cfg.get("enabled", False), proxies)
        await event.answer(
            txt(uid, "proxy_import_done", added=added, skipped=skipped), alert=True
        )
        text, rows = build_proxy_menu(uid, 0)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        return

    if data.startswith("proxy_del="):
        pid = data.split("=", 1)[1]
        if remove_proxy_by_id(pid):
            await event.answer(txt(uid, "proxy_deleted"), alert=False)
        else:
            await event.answer(txt(uid, "proxy_not_found"), alert=True)
        text, rows = build_proxy_menu(uid, 0)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        return

    if data.startswith("proxy_info="):
        pid = data.split("=", 1)[1]
        cfg = get_proxy_config()
        p = next((x for x in (cfg.get("proxies") or []) if x.get("id") == pid), None)
        if not p:
            await event.answer(txt(uid, "proxy_not_found"), alert=True)
            return
        kind = "MTProto" if (p.get("type") or "") == "mtproto" else "SOCKS5"
        auth = "secret" if p.get("secret") else ("yes" if p.get("username") else "no")
        msg = txt(
            uid,
            "proxy_info_text",
            host=p.get("host"),
            port=p.get("port"),
            auth=f"{kind}/{auth}",
            fails=p.get("fails", 0),
            enabled="on" if p.get("enabled", True) else "off",
        )
        await event.answer(msg, alert=True)
        return

    if data == "proxy_add":
        await ctx_set(uid, {"mode": "proxy_add", "step": "await_line"})
        await event.edit(
            txt(uid, "proxy_add_prompt"),
            buttons=conv_cancel_buttons(uid, b"proxy_menu"),
        )
        return

@bot.on(events.NewMessage)
async def proxy_add_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "proxy_add":
        return
    raw = (event.raw_text or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    added = 0
    last_key = "proxy_invalid_format"
    for line in lines or [raw]:
        ok, key = add_proxy_line(line)
        last_key = key
        if ok:
            added += 1
    await ctx_pop(uid)
    if added > 1:
        await event.reply(txt(uid, "proxy_import_done", added=added, skipped=len(lines) - added))
    else:
        await event.reply(txt(uid, last_key))
    text, rows = build_proxy_menu(uid, 0)
    await event.respond(text, buttons=rows)
    raise events.StopPropagation

_ai_models_cache: dict[int, list[str]] = {}

def _ai_mask_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "-"
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:4]}…{k[-4:]}"

def _ai_cache_line(settings: dict, kind: str) -> str:
    every = int(settings.get("refresh_every") or 10)
    item = ((settings.get("cache") or {}).get(kind) or {})
    text = (item.get("text") or "").strip()
    used = int(item.get("used") or 0)
    if not text:
        return f"0/{every}"
    return f"{used}/{every}"

def build_ai_menu(uid: int):
    s = get_ai_settings()
    status = txt(uid, "ai_status_on") if s.get("enabled") else txt(uid, "ai_status_off")
    cache = s.get("cache") or {}
    cached_n = sum(1 for v in cache.values() if isinstance(v, dict) and (v.get("text") or "").strip())
    text = txt(
        uid,
        "ai_menu_text",
        status=status,
        model=s.get("model") or "-",
        every=s.get("refresh_every") or 10,
        key=_ai_mask_key(s.get("api_key") or ""),
        url=s.get("base_url") or "-",
        cache=cached_n,
    )
    rows = [
        [
            Button.inline(
                txt(uid, "ai_toggle_btn", status=status), data=b"ai_toggle"
            )
        ],
        [
            Button.inline(txt(uid, "ai_set_key_btn"), data=b"ai_set_key"),
            Button.inline(txt(uid, "ai_set_url_btn"), data=b"ai_set_url"),
        ],
        [
            Button.inline(
                txt(uid, "ai_set_every_btn", every=s.get("refresh_every") or 10),
                data=b"ai_set_every",
            )
        ],
        [Button.inline(txt(uid, "ai_models_btn"), data=b"ai_models")],
        [Button.inline(txt(uid, "ai_clear_cache_btn"), data=b"ai_clear_cache")],
        [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
    ]
    return text, rows

def build_ai_models_menu(uid: int, page: int = 0):
    models = _ai_models_cache.get(uid) or []
    page_size = 8
    total_pages = max(1, (len(models) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    chunk = models[page * page_size : (page + 1) * page_size]
    cur = (get_ai_settings().get("model") or "").strip()
    text = txt(uid, "ai_models_title", page=page + 1, pages=total_pages)
    rows = []
    for i, mid in enumerate(chunk):
        idx = page * page_size + i
        mark = "✅ " if mid == cur else ""
        label = (mark + mid)[:60]
        rows.append(
            [Button.inline(label, data=f"ai_model_pick={idx}".encode())]
        )
    nav = []
    if page > 0:
        nav.append(
            Button.inline(txt(uid, "prev_page"), data=f"ai_models_page={page-1}".encode())
        )
    if page < total_pages - 1:
        nav.append(
            Button.inline(txt(uid, "next_page"), data=f"ai_models_page={page+1}".encode())
        )
    if nav:
        rows.append(nav)
    rows.append([Button.inline(txt(uid, "back_btn"), data=b"ai_menu")])
    return text, rows

@bot.on(
    events.CallbackQuery(
        pattern=rb"^ai_(menu|toggle|set_key|set_url|set_every|models|clear_cache)$|^ai_(models_page|model_pick)="
    )
)
async def ai_admin_handler(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer()
        return
    data = event.data.decode()

    if data == "ai_models" or data.startswith("ai_models_page="):
        page = int(data.split("=", 1)[1]) if data.startswith("ai_models_page=") else 0
        if data == "ai_models":
            s = get_ai_settings()
            if not (s.get("api_key") or "").strip():
                await event.answer(txt(uid, "ai_no_key"), alert=True)
                return
            await event.answer()
            try:
                models = await _openai_list_models(s)
            except Exception as e:
                logger.warning("AI list models failed: %s", e)
                try:
                    await event.respond(txt(uid, "ai_models_fail", error=ai_error_text(uid, e)))
                except Exception:
                    pass
                return
            if not models:
                try:
                    await event.respond(txt(uid, "ai_models_fail", error="EMPTY"))
                except Exception:
                    pass
                return
            _ai_models_cache[uid] = models
            page = 0
        else:
            await event.answer()
        text, rows = build_ai_models_menu(uid, page)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        return

    if data.startswith("ai_model_pick="):
        try:
            idx = int(data.split("=", 1)[1])
        except Exception:
            await event.answer(txt(uid, "ai_invalid"), alert=True)
            return
        models = _ai_models_cache.get(uid) or []
        if idx < 0 or idx >= len(models):
            await event.answer(txt(uid, "ai_invalid"), alert=True)
            return
        model = models[idx]
        save_ai_settings({"model": model})
        await event.answer(txt(uid, "ai_model_set", model=model[:40]), alert=False)
        text, rows = build_ai_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        return

    if data == "ai_clear_cache":
        save_ai_settings({"cache": {}})
        await event.answer(txt(uid, "ai_cache_cleared"), alert=False)
        text, rows = build_ai_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        return

    await event.answer()

    if data == "ai_menu":
        text, rows = build_ai_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        return

    if data == "ai_toggle":
        s = get_ai_settings()
        save_ai_settings({"enabled": not bool(s.get("enabled"))})
        text, rows = build_ai_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        return

    if data == "ai_set_key":
        await ctx_set(uid, {"mode": "ai_settings", "step": "api_key"})
        await event.edit(
            txt(uid, "ai_prompt_key"),
            buttons=conv_cancel_buttons(uid, b"ai_menu"),
        )
        return

    if data == "ai_set_url":
        await ctx_set(uid, {"mode": "ai_settings", "step": "base_url"})
        await event.edit(
            txt(uid, "ai_prompt_url"),
            buttons=conv_cancel_buttons(uid, b"ai_menu"),
        )
        return

    if data == "ai_set_every":
        await ctx_set(uid, {"mode": "ai_settings", "step": "refresh_every"})
        await event.edit(
            txt(uid, "ai_prompt_every"),
            buttons=conv_cancel_buttons(uid, b"ai_menu"),
        )
        return

@bot.on(events.NewMessage)
async def ai_settings_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "ai_settings":
        return
    step = ctx.get("step")
    raw = (event.raw_text or "").strip()
    if not raw:
        await event.reply(txt(uid, "ai_invalid"))
        raise events.StopPropagation

    if step == "api_key":
        save_ai_settings({"api_key": raw})
    elif step == "base_url":
        if not re.match(r"^https?://", raw, re.I):
            await event.reply(txt(uid, "ai_invalid"))
            raise events.StopPropagation
        save_ai_settings({"base_url": raw.rstrip("/")})
    elif step == "refresh_every":
        try:
            n = int(raw)
            if n < 1 or n > 1000:
                raise ValueError("range")
        except Exception:
            await event.reply(txt(uid, "ai_invalid"))
            raise events.StopPropagation
        save_ai_settings({"refresh_every": n})
    else:
        return

    await ctx_pop(uid)
    await event.reply(txt(uid, "ai_saved"))
    text, rows = build_ai_menu(uid)
    await event.respond(text, buttons=rows)
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"set_bank"))
async def admin_set_bank(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    ctx = {"mode": "set_bank", "step": "card_holder"}
    await ctx_set(uid, ctx)
    await event.edit(
        txt(uid, "set_bank_prompt_name"),
        buttons=conv_cancel_buttons(uid, b"admin_back"),
    )

@bot.on(events.NewMessage)
async def admin_set_bank_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        await ctx_pop(uid)
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "set_bank":
        return
    step = ctx.get("step")
    raw = event.raw_text.strip()

    if step == "card_holder":
        ctx["card_holder"] = raw
        ctx["step"] = "card_number"
        await ctx_set(uid, ctx)
        await event.reply(txt(uid, "set_bank_prompt_card"))
    elif step == "card_number":
        card_holder = ctx.get("card_holder", "")
        card_number = raw.replace(" ", "").replace("-", "")
        if not re.fullmatch(r"\d{10,24}", card_number):
            await event.reply(txt(uid, "invalid_card"), buttons=admin_back_buttons(uid))
            return
        set_bank_info(card_number, card_holder)
        await ctx_pop(uid)
        await event.reply(
            txt(uid, "bank_saved", holder=card_holder, card=card_number),
            buttons=admin_back_buttons(uid),
        )
        await admin_entry(event)
    raise events.StopPropagation

async def build_crypto_menu(uid: int):
    s = get_crypto_settings()
    status = txt(uid, "crypto_status_on") if s.get("enabled") else txt(uid, "crypto_status_off")
    auto_st = (
        txt(uid, "crypto_status_on")
        if s.get("auto_verify", True)
        else txt(uid, "crypto_status_off")
    )
    wallet = s.get("wallet") or "-"
    if len(wallet) > 18:
        wallet = f"{wallet[:8]}…{wallet[-6:]}"
    price_line = "—"
    try:
        p = await fetch_bitpin_price(s.get("symbol") or "GRAM_IRT")
        price_line = f"{int(p):,}"
    except Exception:
        pass
    text = txt(
        uid,
        "crypto_menu_text",
        status=status,
        auto=auto_st,
        network=s.get("network") or "TON",
        asset=s.get("asset_name") or "GRAM",
        symbol=s.get("symbol") or "GRAM_IRT",
        wallet=wallet,
        memo=s.get("memo") or "-",
        price=price_line,
    )
    rows = [
        [Button.inline(txt(uid, "crypto_toggle_btn", status=status), data=b"crypto_toggle")],
        [
            Button.inline(
                txt(uid, "crypto_auto_toggle_btn", status=auto_st),
                data=b"crypto_auto_toggle",
            )
        ],
        [Button.inline(txt(uid, "crypto_set_wallet_btn"), data=b"crypto_set_wallet")],
        [Button.inline(txt(uid, "crypto_set_memo_btn"), data=b"crypto_set_memo")],
        [Button.inline(txt(uid, "crypto_refresh_price_btn"), data=b"crypto_refresh")],
        [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
    ]
    return text, rows

@bot.on(
    events.CallbackQuery(
        pattern=rb"^crypto_(menu|toggle|auto_toggle|set_wallet|set_memo|refresh)$"
    )
)
async def crypto_admin_handler(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer()
        return
    data = (event.data or b"").decode()

    if data == "crypto_refresh":
        try:
            _bitpin_price_cache["ts"] = 0
            p = await fetch_bitpin_price(get_crypto_settings().get("symbol") or "GRAM_IRT")
            await event.answer(f"GRAM ≈ {int(p):,} IRT", alert=True)
        except Exception:
            await event.answer(txt(uid, "crypto_price_fail"), alert=True)
        text, rows = await build_crypto_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            pass
        raise events.StopPropagation

    await event.answer()

    if data == "crypto_menu":
        text, rows = await build_crypto_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "crypto_toggle":
        s = get_crypto_settings()
        if not (s.get("wallet") or "").strip():
            await event.answer(txt(uid, "crypto_need_wallet"), alert=True)
            return
        save_crypto_settings({"enabled": not bool(s.get("enabled"))})
        text, rows = await build_crypto_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "crypto_auto_toggle":
        s = get_crypto_settings()
        save_crypto_settings({"auto_verify": not bool(s.get("auto_verify", True))})
        text, rows = await build_crypto_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "crypto_set_wallet":
        await ctx_set(uid, {"mode": "crypto_settings", "step": "wallet"})
        await event.edit(
            txt(uid, "crypto_prompt_wallet"),
            buttons=conv_cancel_buttons(uid, b"crypto_menu"),
        )
        raise events.StopPropagation

    if data == "crypto_set_memo":
        await ctx_set(uid, {"mode": "crypto_settings", "step": "memo"})
        await event.edit(
            txt(uid, "crypto_prompt_memo"),
            buttons=conv_cancel_buttons(uid, b"crypto_menu"),
        )
        raise events.StopPropagation

@bot.on(events.NewMessage)
async def crypto_settings_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "crypto_settings":
        return
    step = ctx.get("step")
    raw = (event.raw_text or "").strip()
    if step == "wallet":
        if len(raw) < 10:
            await event.reply(txt(uid, "crypto_invalid"))
            raise events.StopPropagation
        save_crypto_settings({"wallet": raw, "enabled": True})
    elif step == "memo":
        save_crypto_settings({"memo": "" if raw == "-" else raw})
    else:
        return
    await ctx_pop(uid)
    await event.reply(txt(uid, "crypto_saved"))
    text, rows = await build_crypto_menu(uid)
    await event.respond(text, buttons=rows)
    raise events.StopPropagation

def build_referral_admin_menu(uid: int):
    s = get_referral_settings()
    status = txt(uid, "crypto_status_on") if s.get("enabled") else txt(uid, "crypto_status_off")
    once = txt(uid, "crypto_status_on") if s.get("once_only", True) else txt(uid, "crypto_status_off")
    text = txt(
        uid,
        "ref_menu_text",
        status=status,
        required=int(s.get("required_invites") or 10),
        reward=int(s.get("reward_days") or 2),
        once=once,
    )
    rows = [
        [Button.inline(txt(uid, "ref_toggle_btn", status=status), data=b"ref_toggle")],
        [Button.inline(txt(uid, "ref_once_btn", status=once), data=b"ref_once")],
        [Button.inline(txt(uid, "ref_set_required_btn"), data=b"ref_set_required")],
        [Button.inline(txt(uid, "ref_set_reward_btn"), data=b"ref_set_reward")],
        [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
    ]
    return text, rows

@bot.on(
    events.CallbackQuery(pattern=rb"^ref_(menu|toggle|once|set_required|set_reward)$")
)
async def referral_admin_handler(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer()
        return
    data = (event.data or b"").decode()
    await event.answer()

    if data == "ref_menu":
        text, rows = build_referral_admin_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "ref_toggle":
        s = get_referral_settings()
        save_referral_settings({"enabled": not bool(s.get("enabled"))})
        text, rows = build_referral_admin_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "ref_once":
        s = get_referral_settings()
        save_referral_settings({"once_only": not bool(s.get("once_only", True))})
        text, rows = build_referral_admin_menu(uid)
        try:
            await event.edit(text, buttons=rows)
        except Exception:
            await event.respond(text, buttons=rows)
        raise events.StopPropagation

    if data == "ref_set_required":
        await ctx_set(uid, {"mode": "referral_settings", "step": "required"})
        await event.edit(
            txt(uid, "ref_prompt_required"),
            buttons=conv_cancel_buttons(uid, b"ref_menu"),
        )
        raise events.StopPropagation

    if data == "ref_set_reward":
        await ctx_set(uid, {"mode": "referral_settings", "step": "reward"})
        await event.edit(
            txt(uid, "ref_prompt_reward"),
            buttons=conv_cancel_buttons(uid, b"ref_menu"),
        )
        raise events.StopPropagation

@bot.on(events.NewMessage)
async def referral_settings_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "referral_settings":
        return
    step = ctx.get("step")
    raw = (event.raw_text or "").strip()
    try:
        n = int(raw)
        if n < 1 or n > 3650:
            raise ValueError("range")
    except Exception:
        await event.reply(txt(uid, "ref_invalid"))
        raise events.StopPropagation
    if step == "required":
        save_referral_settings({"required_invites": n})
    elif step == "reward":
        save_referral_settings({"reward_days": n})
    else:
        return
    await ctx_pop(uid)
    await event.reply(txt(uid, "ref_saved"))
    text, rows = build_referral_admin_menu(uid)
    await event.respond(text, buttons=rows)
    raise events.StopPropagation

@bot.on(events.NewMessage)
async def edit_texts_conversation(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    if is_menu_or_command(event):
        await ctx_pop(uid)
        return
    ctx = await ctx_get(uid)
    if not ctx or ctx.get("mode") != "edit_texts":
        return
    step = ctx.get("step")
    if step == "await_search":
        query = (event.raw_text or "").strip()
        if not query:
            return
        results = search_bot_texts(query)
        if not results:
            await event.reply(
                txt(uid, "edit_texts_no_result"), buttons=admin_back_buttons(uid)
            )
            return
        ctx["step"] = "await_choice"
        ctx["results"] = [{"key": k, "lang": l, "text": t} for sc, k, l, t in results]
        await ctx_set(uid, ctx)
        parts = [txt(uid, "edit_texts_results")]
        for i, (sc, k, l, t) in enumerate(results, 1):
            short = t if len(t) <= 60 else t[:57] + "..."
            parts.append(f"{i}. [{l}] {k}\n{short}")
        btns = []
        row = []
        for i in range(len(results)):
            row.append(Button.inline(str(i + 1), data=f"txtedit={i}".encode()))
            if len(row) == 2:
                btns.append(row)
                row = []
        if row:
            btns.append(row)
        btns.append([Button.inline(txt(uid, "cancel_btn"), data=b"edit_texts_cancel")])
        await event.reply("\n\n".join(parts), buttons=btns)
        raise events.StopPropagation
    elif step == "await_new_text":
        new_text = _extract_custom_emoji_text(event.message)
        logger.info(f"[edit_texts] new text received from admin {uid}: {new_text!r}")
        chosen = ctx.get("chosen")
        if not chosen:
            await ctx_pop(uid)
            return
        needed_vars = extract_text_vars(chosen["text"])
        new_vars = extract_text_vars(new_text)
        missing = [v for v in needed_vars if v not in new_vars]
        if missing:
            logger.warning(f"[edit_texts] rejected: missing vars {missing}")
            await event.reply(
                txt(
                    uid,
                    "edit_texts_missing_vars",
                    vars=" ".join("{" + v + "}" for v in missing),
                ),
                buttons=admin_back_buttons(uid),
            )
            return
        TEXT_OVERRIDES.setdefault(chosen["key"], {})[chosen["lang"]] = new_text
        db.settings.update_one(
            {"key": "text_overrides"}, {"$set": {"value": TEXT_OVERRIDES}}, upsert=True
        )
        TRANSLATIONS.setdefault(chosen["key"], {})[chosen["lang"]] = new_text
        rebuild_menu_actions_map()
        try:
            async with aiofiles.open("translations.json", "w", encoding="utf-8") as tf:
                content = json.dumps(TRANSLATIONS, ensure_ascii=False, indent=2)
                await tf.write(content)
            logger.info(
                f"[edit_texts] translations.json updated for {chosen['key']}[{chosen['lang']}]"
            )
        except Exception as write_err:
            logger.warning(
                f"[edit_texts] failed to write translations.json: {write_err}"
            )
        logger.info(
            f"[edit_texts] saved override: {chosen['key']}[{chosen['lang']}] = {new_text!r}"
        )
        await ctx_pop(uid)
        await event.reply(
            txt(uid, "edit_texts_saved", key=chosen["key"], lang=chosen["lang"]),
            buttons=admin_back_buttons(uid),
        )
        raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"admin_user_list"))
async def admin_user_list(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    page = 0
    raw = event.data.decode()
    if "=" in raw:
        page = int(raw.split("=")[1])
    pipeline = [
        {"$group": {"_id": "$admin_id", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
        {"$skip": page * 10},
        {"$limit": 10},
    ]
    total_pipeline = [{"$group": {"_id": "$admin_id"}}, {"$count": "total"}]
    total_docs = list(db["accounts"].aggregate(total_pipeline))
    total_users = total_docs[0]["total"] if total_docs else 0
    results = list(db["accounts"].aggregate(pipeline))
    if not results:
        await event.edit(txt(uid, "no_accounts_title"), buttons=admin_back_buttons(uid))
        return
    rows = []
    for doc in results:
        admin_id = doc["_id"]
        count = doc["count"]
        rows.append(
            [
                Button.inline(
                    f"👤[5373012449597335010] {admin_id} • {count} {txt(uid, 'accounts_count')}",
                    data=f"admin_view_user={admin_id}".encode(),
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(
            Button.inline(
                txt(uid, "prev_page"), data=f"admin_user_list={page - 1}".encode()
            )
        )
    if (page + 1) * 10 < total_users:
        nav.append(
            Button.inline(
                txt(uid, "next_page"), data=f"admin_user_list={page + 1}".encode()
            )
        )
    if nav:
        rows.append(nav)
    rows.append([Button.inline(txt(uid, "admin_back_general"), data=b"admin_back")])
    await event.edit(
        txt(uid, "users_page_title", page=page + 1, total=total_users), buttons=rows
    )

@bot.on(events.CallbackQuery(pattern=b"admin_view_user="))
async def admin_view_user(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    target_admin = int(event.data.decode().split("=")[1])
    accounts = list(db["accounts"].find({"admin_id": target_admin}))
    if not accounts:
        await event.edit(txt(uid, "no_accounts"), buttons=admin_back_buttons(uid))
        return
    rows = []
    for acc in accounts:
        sid = str(acc["_id"])
        phone = acc.get("phone", txt(uid, "unknown_phone"))
        rows.append(
            [
                Button.inline(
                    f"📱[5409357944619802453] {phone}",
                    data=f"admin_get_code={sid}".encode(),
                ),
                Button.inline(
                    txt(uid, "delete_btn"), data=f"admin_delete_session={sid}".encode()
                ),
                Button.inline(
                    txt(uid, "sessions_btn"), data=f"admin_sessions={sid}".encode()
                ),
            ]
        )
    rows.append(
        [
            Button.inline(
                txt(uid, "delete_all_user_sessions", count=len(accounts)),
                data=f"admin_delete_all_user_sessions={target_admin}".encode(),
            )
        ]
    )
    rows.append(
        [Button.inline(txt(uid, "admin_back_to_list"), data=b"admin_user_list")]
    )
    await event.edit(txt(uid, "admin_account_list", user_id=target_admin), buttons=rows)

async def read_login_code_text(uid: int, sid: str) -> str:
    try:
        user = db["accounts"].find_one({"_id": ObjectId(sid)})
    except Exception:
        user = None
    if not user:
        return txt(uid, "account_not_found")
    client = retern_client(sid)
    if not client:
        await drop_account(sid, reason="invalid_session")
        return txt(uid, "session_invalid_deleted")
    try:
        await asyncio.wait_for(client.connect(), timeout=25)
        if not await client.is_user_authorized():
            await drop_account(sid, reason="unauthorized")
            return txt(uid, "account_not_logged_in_deleted")
        messages = await client.get_messages(777000, limit=3)
        for msg in messages:
            m = re.search(r"(?<!\d)(\d{5,6})(?!\d)", msg.message or "")
            if m:
                return txt(uid, "your_code", code=m.group(1))
        return txt(uid, "code_not_received")
    except errors.RPCError as e:
        if _is_frozen(e) or _is_unauthorized_like(e) or _should_drop_session_error(e):
            await drop_account(sid, reason="frozen/unauthorized")
            return txt(uid, "account_frozen_deleted")
        return txt(uid, "error_occurred", error=_rpc_err_info(e))
    except asyncio.TimeoutError:
        return txt(uid, "error_occurred", error="CONNECT_TIMEOUT")
    except (ConnectionError, OSError):
        return txt(uid, "error_occurred", error="CONNECT_FAILED")
    finally:
        await _safe_disconnect(client)


@bot.on(events.CallbackQuery(pattern=b"admin_get_code="))
async def admin_get_code(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    try:
        await event.answer()
    except Exception:
        pass
    sid = event.data.decode().split("=", 1)[1]
    text = await read_login_code_text(uid, sid)
    await event.respond(text, buttons=admin_back_buttons(uid))
    raise events.StopPropagation

@bot.on(events.CallbackQuery(pattern=b"partner_requests_list"))
async def list_partner_requests(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    reqs = list(
        PARTNER_REQUESTS_COL.find({"status": "pending"}).sort("requested_at", -1)
    )
    if not reqs:
        await event.edit(
            txt(uid, "no_pending_requests"),
            buttons=[[Button.inline(txt(uid, "back_btn"), data=b"admin_back")]],
        )
        return
    text = txt(uid, "pending_requests_title") + "\n\n"
    rows = []
    for req in reqs:
        rid = req["_id"]
        text += f"👤[5373012449597335010] {req['requester_id']} ➕[5397916757333654639] {req['partner_id']}\n"
        rows.append(
            [
                Button.inline(
                    "✅[5206607081334906820]",
                    data=f"partner_req:approve:{rid}".encode(),
                ),
                Button.inline(
                    "❌[5210952531676504517]", data=f"partner_req:reject:{rid}".encode()
                ),
            ]
        )
    rows.append([Button.inline(txt(uid, "back_btn"), data=b"admin_back")])
    await event.edit(text, buttons=rows)

@bot.on(events.CallbackQuery(pattern=b"add_admin"))
async def add_admin_handler(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                txt(uid, "add_admin_prompt"),
                buttons=conv_cancel_buttons(uid, b"admin_back"),
            )
            resp = await menu_safe_response(conv)
            try:
                ent = await bot.get_entity(resp.raw_text.strip())
                new_id = ent.id
            except Exception:
                try:
                    new_id = int(resp.raw_text.strip())
                except Exception:
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
            if new_id in ADMIN_IDS:
                await conv.send_message(txt(uid, "already_admin"))
                return
            db["admins"].insert_one(
                {
                    "user_id": new_id,
                    "added_by": uid,
                    "added_at": datetime.datetime.now(datetime.timezone.utc),
                }
            )
            ADMIN_IDS.add(new_id)
            await conv.send_message(txt(uid, "admin_added_success", id=new_id))

    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

@bot.on(events.CallbackQuery(pattern=b"remove_admin"))
async def remove_admin_handler(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    additional = [uid for uid in ADMIN_IDS if uid not in ADMINS]
    if not additional:
        await event.answer(txt(uid, "no_extra_admins"), alert=True)
        return
    rows = []
    for aid in additional:
        rows.append(
            [
                Button.inline(
                    f"❌[5210952531676504517] {aid}", data=f"del_admin:{aid}".encode()
                )
            ]
        )
    rows.append([Button.inline(txt(uid, "back_btn"), data=b"admin_back")])
    await event.edit(txt(uid, "extra_admins"), buttons=rows)

@bot.on(events.CallbackQuery(pattern=b"del_admin:"))
async def del_admin_exec(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    target = int(event.data.decode().split(":")[1])
    db["admins"].delete_one({"user_id": target})
    ADMIN_IDS.discard(target)
    await event.answer(txt(uid, "deleted"), alert=True)
    await admin_entry(event)

@bot.on(events.CallbackQuery(pattern=b"admin_back"))
async def admin_back(event):
    await event.answer()
    await admin_entry(event)

@bot.on(events.CallbackQuery(pattern=b"start_menu"))
async def start_menu_cb(event):
    await start_menu(event)

async def show_main_menu(event, uid: int | None = None):
    uid = int(uid or UID(event))
    await ctx_pop(uid)
    custom = db.settings.find_one({"key": "start_message"})
    welcome = custom["value"] if custom and "value" in custom else txt(uid, "main_menu_welcome")
    if not await has_access(uid):
        welcome += "\n\n" + txt(uid, "no_access")
    try:
        await event.respond(welcome, buttons=await kb_main(uid))
    except Exception:
        try:
            await event.reply(welcome, buttons=await kb_main(uid))
        except Exception:
            pass

@bot.on(events.NewMessage(pattern=r"^/start(?:\s+(\S+))?$", forwards=False))
async def start_menu(event):
    uid = UID(event)
    payload = ""
    try:
        if getattr(event, "pattern_match", None) and event.pattern_match.group(1):
            payload = (event.pattern_match.group(1) or "").strip()
    except Exception:
        payload = ""
    if payload:
        await _save_pending_ref(uid, payload)
    await ctx_pop(uid)
    if not await ensure_force_join(event, uid):
        return
    try:
        final_payload = payload or (await _pop_pending_ref(uid))
        await process_start_referral(uid, final_payload)
    except Exception as e:
        logger.warning("referral process failed uid=%s: %s", uid, e)
    custom = db.settings.find_one({"key": "start_message"})
    if custom and "value" in custom:
        welcome = custom["value"]
    else:
        welcome = txt(uid, "main_menu_welcome")
    if not await has_access(uid):
        welcome += "\n\n" + txt(uid, "no_access")
    try:
        await event.edit(welcome, buttons=await kb_main(uid))
    except Exception:
        await event.respond(welcome, buttons=await kb_main(uid))

async def handle_accounts_menu(event):
    sender_id = event.sender_id
    if not await has_access(sender_id):
        return
    if not can_use_accounts(sender_id):
        await event.respond(txt(sender_id, "accounts_not_allowed_short_sub"))
        return
    sessions = await list_session_files(sender_id, include_locked=True)
    if not sessions:
        add_btn = [
            [Button.inline(txt(sender_id, "add_account_btn"), data=b"add_new_accounts")]
        ]
        await event.respond(txt(sender_id, "no_accounts_available"), buttons=add_btn)
        return
    await redis.set(f"user_sessions:{sender_id}", json.dumps(sessions), ex=300)
    buttons = get_accounts_buttons_from_sessions(
        sender_id, db, sessions, sender_id, page=0
    )
    await event.reply(await accounts_list_title(sender_id, sessions), buttons=buttons)


async def accounts_list_title(uid: int, sessions: list[str]) -> str:
    try:
        free = await _filter_unlocked_sessions(sessions, uid)
        busy = len(sessions) - len(free)
    except Exception:
        busy = 0
    return txt(uid, "account_list_title") + "\n" + txt(
        uid, "account_list_count", total=len(sessions), busy=busy
    )

async def transfer_own(event):
    uid = UID(event)
    if not await has_access(uid):
        return
    if not can_use_accounts(uid):
        await event.reply(txt(uid, "accounts_not_allowed_short_sub"))
        return
    if get_plus_subscription(uid):
        await event.reply(txt(uid, "plus_no_transfer"))
        return

    if await is_partner(uid):
        await event.reply(txt(uid, "transfer_not_allowed_for_partner"))
        return
    try:
        async with standard_conversation(uid, timeout=180) as conv:
            await conv.send_message(
                txt(uid, "transfer_prompt_dst"), buttons=conv_cancel_buttons(uid)
            )
            target_resp = await menu_safe_response(conv)
            target_raw = target_resp.raw_text.strip()
            try:
                target_entity = await bot.get_entity(target_raw)
                target_uid = target_entity.id
            except Exception:
                try:
                    target_uid = int(target_raw)
                except Exception:
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
            if target_uid == uid:
                await conv.send_message(txt(uid, "transfer_self"))
                return
            await conv.send_message(
                txt(
                    uid,
                    "transfer_own_confirm",
                    count=len(await list_session_files(uid)),
                    target=target_uid,
                ),
                buttons=conv_cancel_buttons(uid),
            )
            confirm = await menu_safe_response(conv)
            if confirm.raw_text.strip() != txt(uid, "yes"):
                await conv.send_message(txt(uid, "transfer_cancelled"))
                return
            sub = SUBS_COL.find_one({"user_id": uid})
            if sub:
                target_sub = SUBS_COL.find_one({"user_id": target_uid})
                if target_sub:
                    new_exp = max(
                        sub.get("expires_at", 0), target_sub.get("expires_at", 0)
                    )
                    SUBS_COL.update_one(
                        {"user_id": target_uid}, {"$set": {"expires_at": new_exp}}
                    )
                else:
                    SUBS_COL.insert_one(
                        {
                            "user_id": target_uid,
                            "expires_at": sub["expires_at"],
                            "created_at": sub.get("created_at", _ts_now()),
                            "added_by": uid,
                            "note": f"منتقل شده از {uid}",
                        }
                    )
                SUBS_COL.delete_one({"user_id": uid})
            accs = db["accounts"].find({"admin_id": uid})
            count = 0
            for acc in accs:
                db["accounts"].update_one(
                    {"_id": acc["_id"]}, {"$set": {"admin_id": target_uid}}
                )
                count += 1
            PARTNERS_COL.update_many(
                {"main_user_id": uid}, {"$set": {"main_user_id": target_uid}}
            )
            await conv.send_message(
                txt(uid, "transfer_own_done", count=count, target=target_uid)
            )

    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return

@bot.on(events.CallbackQuery(pattern=rb"session_upload_done"))
async def session_upload_done_handler(event):
    uid = UID(event)
    key = _conv_key(uid)
    conv = PENDING_CONVERSATIONS.get(key)
    try:
        await event.answer()
    except Exception:
        pass
    if not conv or not conv.active:
        return

    class _FakeDoneResp:
        raw_text = "__session_upload_done__"
        text = "__session_upload_done__"
        document = None
        entities = None

    try:
        conv.queue.put_nowait(_FakeDoneResp())
    except asyncio.QueueFull:
        pass
    raise events.StopPropagation

@bot.on(
    events.CallbackQuery(
        pattern=rb"page_acc|add_new_accounts|add_via_phone|add_via_session|get_code|delete_accounts"
    )
)
async def callback_accounts(event):
    await event.answer()
    raw_data = event.data
    sender_id = UID(event)
    parts = raw_data.decode().split("=")
    if not await has_access(sender_id):
        await event.reply(txt(sender_id, "no_access"))
        raise events.StopPropagation
    if not can_use_accounts(sender_id):
        try:
            await event.edit(txt(sender_id, "accounts_not_allowed_short_sub"))
        except Exception:
            await event.reply(txt(sender_id, "accounts_not_allowed_short_sub"))
        raise events.StopPropagation
    key = parts[0]
    val = parts[1] if len(parts) > 1 else ""
    if key == "page_acc":
        page = int(val) if val.isdigit() else 0
        sessions_json = await redis.get(f"user_sessions:{sender_id}")
        if not sessions_json:
            await event.edit(txt(sender_id, "session_expired"))
            return
        sessions = json.loads(sessions_json)
        buttons = get_accounts_buttons_from_sessions(
            sender_id, db, sessions, sender_id, page
        )
        await event.edit(
            await accounts_list_title(sender_id, sessions), buttons=buttons
        )
    elif key == "get_code":
        sid = val
        allowed = sender_id in ADMIN_IDS
        if not allowed:
            try:
                allowed = sid in set(await list_session_files(sender_id, include_locked=True))
            except Exception:
                allowed = False
        if not allowed:
            await event.respond(txt(sender_id, "account_not_found"))
            raise events.StopPropagation
        text = await read_login_code_text(sender_id, sid)
        await event.respond(text)
    elif key == "add_new_accounts":
        add_method_buttons = [
            [
                Button.inline(
                    txt(sender_id, "add_via_phone_btn"), data=b"add_via_phone"
                ),
                Button.inline(
                    txt(sender_id, "add_via_session_btn"), data=b"add_via_session"
                ),
            ],
        ]
        await event.edit(
            txt(sender_id, "add_account_choose_method"), buttons=add_method_buttons
        )
    elif key == "add_via_phone":
        try:

            async with standard_conversation(sender_id, timeout=900) as conv:
                accounts_col = db["accounts"]
                added_count = 0
                failed_count = 0
                await event.edit(
                    txt(sender_id, "send_phone"),
                    buttons=conv_cancel_buttons(sender_id),
                )
                while True:
                    number = await menu_safe_response(conv)
                    raw = (number.raw_text or number.text or "").strip()
                    if _is_done_command(raw):
                        break

                    phone = raw.replace(" ", "").replace("-", "")
                    if not re.fullmatch(r"\+?\d{10,15}", phone):
                        await conv.send_message(
                            txt(sender_id, "invalid_phone")
                            + "\n"
                            + txt(sender_id, "send_phone"),
                            buttons=conv_cancel_buttons(sender_id),
                        )
                        continue

                    if accounts_col.find_one({"phone": phone}):
                        await conv.send_message(
                            txt(sender_id, "phone_exists")
                            + "\n"
                            + txt(sender_id, "send_next_phone"),
                            buttons=conv_cancel_buttons(sender_id),
                        )
                        continue

                    os.makedirs("session", exist_ok=True)
                    tmp_path = f"session/{phone}.json"
                    data = {
                        "api_id": api_id_admin,
                        "api_hash": api_hash_admin,
                        "phone": phone,
                    }
                    async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                        await f.write(json.dumps(data, ensure_ascii=False))

                    string_session_accunte = await add_accunte(
                        event, conv, phone=phone
                    )
                    if string_session_accunte:
                        string_session_accunte["admin_id"] = sender_id
                        accounts_col.insert_one(string_session_accunte)
                        added_count += 1
                        await redis.delete(f"user_sessions:{sender_id}")
                        await conv.send_message(
                            txt(sender_id, "send_next_phone"),
                            buttons=conv_cancel_buttons(sender_id),
                        )
                    else:
                        failed_count += 1
                        try:
                            os.remove(tmp_path)
                        except FileNotFoundError:
                            pass
                        await conv.send_message(
                            txt(sender_id, "phone_add_retry"),
                            buttons=conv_cancel_buttons(sender_id),
                        )

                await redis.delete(f"user_sessions:{sender_id}")
                if added_count == 0 and failed_count == 0:
                    await event.respond(
                        txt(sender_id, "phone_add_cancelled_empty"),
                        buttons=get_accuntes_buttons(sender_id, db, sender_id),
                    )
                else:
                    await event.respond(
                        txt(
                            sender_id,
                            "phone_add_summary",
                            added=added_count,
                            failed=failed_count,
                        ),
                        buttons=get_accuntes_buttons(sender_id, db, sender_id),
                    )
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
    elif key == "add_via_session":
        try:
            async with standard_conversation(sender_id, timeout=300) as conv:
                accounts_col = db["accounts"]
                added_count = 0
                failed_count = 0
                done_buttons = [
                    [
                        Button.inline(
                            txt(sender_id, "session_upload_done_btn"),
                            data=b"session_upload_done",
                        )
                    ],
                    [Button.inline(txt(sender_id, "cancel_btn"), data=b"conv_cancel")],
                ]
                await event.edit(
                    txt(sender_id, "send_session_files"), buttons=done_buttons
                )
                while True:
                    resp = await menu_safe_response(conv)
                    if (
                        getattr(resp, "raw_text", None)
                        and resp.raw_text.strip() == "__session_upload_done__"
                    ):
                        break
                    doc = resp.document if getattr(resp, "document", None) else None
                    raw_text = (resp.raw_text or "").strip()
                    raw_bytes = None
                    if doc:
                        try:
                            raw_bytes = await resp.download_media(bytes)
                        except Exception:
                            raw_bytes = None
                    if not doc and not raw_text:
                        await conv.send_message(
                            txt(sender_id, "session_file_invalid"), buttons=done_buttons
                        )
                        continue
                    session_data = _parse_session_upload(
                        raw_text if not doc else "", raw_bytes
                    )
                    if not session_data:
                        failed_count += 1
                        await conv.send_message(
                            txt(sender_id, "session_file_invalid"), buttons=done_buttons
                        )
                        continue
                    user_data, err_key = await add_account_by_session(
                        event, session_data
                    )
                    if err_key:
                        failed_count += 1
                        await conv.send_message(
                            txt(sender_id, err_key), buttons=done_buttons
                        )
                        continue
                    user_data["admin_id"] = sender_id
                    accounts_col.insert_one(user_data)
                    added_count += 1
                    await conv.send_message(
                        txt(
                            sender_id,
                            "session_file_added_one",
                            phone=user_data.get("phone", ""),
                        ),
                        buttons=done_buttons,
                    )
                await redis.delete(f"user_sessions:{sender_id}")
                await event.respond(
                    txt(
                        sender_id,
                        "session_upload_summary",
                        added=added_count,
                        failed=failed_count,
                    ),
                    buttons=get_accuntes_buttons(sender_id, db, sender_id),
                )
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
    elif key == "delete_accounts":
        sid = val
        if sender_id in ADMIN_IDS:
            await drop_account(sid, send_notification=False)
        else:
            user = db["accounts"].find_one(
                {"_id": ObjectId(sid), "admin_id": sender_id}
            )
            if user:
                await drop_account(sid, send_notification=False)
        await redis.delete(f"user_sessions:{sender_id}")
        await event.edit(
            txt(sender_id, "account_removed"),
            buttons=get_accuntes_buttons(sender_id, db, sender_id),
        )
    raise events.StopPropagation

def get_plan_name(uid: int, plan: str) -> str:
    lang = get_user_lang(uid)
    return TRANSLATIONS.get("plan_names", {}).get(lang, {}).get(plan, plan)

@bot.on(events.CallbackQuery)
async def callback_admin(event):
    raw = event.data.decode()
    sender_id = event.sender_id
    if sender_id not in ADMIN_IDS:
        return
    parts = raw.split("=", 1)
    key = parts[0]
    val = parts[1] if len(parts) > 1 else ""
    if key in ("broadcast", "bc_yes", "bc_no") or key.startswith("bc_"):
        return
    try:
        await event.answer()
    except Exception:
        pass
    if key in ("fj_menu", "fj_toggle", "fj_add", "fj_check") or key.startswith("fj_"):
        return
    if (
        key.startswith("crypto_")
        or key.startswith("pay_method")
        or key.startswith("ref_")
        or key.startswith("purchase_")
        or key in ("pay_method", "crypto_menu", "ref_menu", "pkg_cancel", "pkg_select")
    ):
        return
    if key in ("users", "users_page"):
        page = int(val) if (key == "users_page" and val.isdigit()) else 0
        total, items = await list_users_page(sender_id, page)
        kb = build_users_keyboard(sender_id, total, items, page)
        text = txt(sender_id, "users_page_title", page=page + 1, total=total)
        try:
            await event.edit(text, buttons=kb)
        except Exception:
            await event.respond(text, buttons=kb)
    elif key == "edit_texts":
        await ctx_set(sender_id, {"mode": "edit_texts", "step": "await_search"})
        await event.edit(
            txt(sender_id, "edit_texts_prompt"),
            buttons=[
                [Button.inline(txt(sender_id, "cancel_btn"), data=b"edit_texts_cancel")]
            ],
        )
    elif key == "edit_texts_cancel":
        await ctx_pop(sender_id)
        await event.edit(
            txt(sender_id, "cancel"),
            buttons=[[Button.inline(txt(sender_id, "back_btn"), data=b"start_menu")]],
        )
    elif key == "txtedit":
        ctx = await ctx_get(sender_id)
        if (
            not ctx
            or ctx.get("mode") != "edit_texts"
            or ctx.get("step") != "await_choice"
        ):
            return
        idx = int(val) if val.isdigit() else -1
        results = ctx.get("results", [])
        if not (0 <= idx < len(results)):
            await event.answer(txt(sender_id, "invalid_input"), alert=True)
            return
        chosen = results[idx]
        ctx["step"] = "await_new_text"
        ctx["chosen"] = chosen
        await ctx_set(sender_id, ctx)
        logger.info(
            f"[edit_texts] admin {sender_id} picked: key={chosen['key']} lang={chosen['lang']}"
        )
        needed_vars = extract_text_vars(chosen["text"])
        msg_txt = txt(
            sender_id,
            "edit_texts_send_new",
            key=chosen["key"],
            lang=chosen["lang"],
            current=chosen["text"],
        )
        if needed_vars:
            msg_txt += "\n\n" + txt(
                sender_id,
                "edit_texts_vars_hint",
                vars=" ".join("{" + v + "}" for v in needed_vars),
            )
        await event.edit(msg_txt)
    elif key == "add_user":
        try:
            async with standard_conversation(sender_id, timeout=180) as conv:
                await event.delete()
                await conv.send_message(
                    txt(sender_id, "add_user_id_prompt"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_user_list"),
                )
                ref = (await menu_safe_response(conv)).raw_text.strip()
                try:
                    ent = await bot.get_entity(ref)
                    user_id = ent.id
                except Exception:
                    try:
                        user_id = int(ref)
                    except Exception:
                        await conv.send_message(txt(sender_id, "invalid_input"))
                        return
                await conv.send_message(
                    txt(sender_id, "set_days_prompt"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_user_list"),
                )
                days_txt = (await menu_safe_response(conv)).raw_text.strip()
                try:
                    days = max(0, int(days_txt))
                except Exception:
                    await conv.send_message(txt(sender_id, "invalid_number"))
                    return
                await conv.send_message(
                    txt(sender_id, "add_user_note_prompt"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_user_list"),
                )
                note = (await menu_safe_response(conv)).raw_text.strip()
                if note == "-":
                    note = ""
                exp_ts = await add_subscription(
                    user_id, days, added_by=sender_id, note=note
                )
                await conv.send_message(
                    txt(
                        sender_id,
                        "user_added_success",
                        userid=user_id,
                        exp=_fmt_date(exp_ts),
                        days=_days_left(exp_ts),
                    )
                )
                try:
                    await bot.send_message(
                        user_id,
                        txt(
                            user_id,
                            "purchase_approved",
                            plan_name=txt(user_id, "plan_normal"),
                            days=days,
                        ),
                    )
                except Exception:
                    pass
                try:
                    welcome = txt(user_id, "main_menu_welcome")
                    await bot.send_message(
                        user_id, welcome, buttons=await kb_main(user_id)
                    )
                except Exception:
                    pass
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
        total, items = await list_users_page(sender_id, 0)
        await event.respond(
            txt(sender_id, "users_list"),
            buttons=build_users_keyboard(sender_id, total, items, 0),
        )
    elif key == "user":
        try:
            uid = int(val)
        except Exception:
            await event.answer(txt(sender_id, "invalid_id"), alert=True)
            return
        rec = await get_user_record(uid)
        if not rec:
            await event.edit(
                txt(sender_id, "user_not_found"), buttons=admin_back_buttons(sender_id)
            )
            return
        exp = int(rec.get("expires_at", 0))
        text = txt(
            sender_id,
            "user_subscription_detail",
            user_id=uid,
            status=_status_badge(sender_id, exp),
            exp=_fmt_date(exp),
            days=_days_left(exp),
            note=rec.get("note", "") or "—",
        )
        await event.edit(text, buttons=build_user_detail_keyboard(uid))
    elif key == "extend_user":
        try:
            uid_str, days_str = val.split(":", 1)
            uid = int(uid_str)
            more = int(days_str)
        except Exception:
            await event.answer(txt(sender_id, "invalid_extend_format"), alert=True)
            return
        new_exp = await extend_user_days(uid, more)
        if not new_exp:
            await event.edit(
                txt(sender_id, "user_not_found"), buttons=admin_back_buttons(sender_id)
            )
            return
        await event.edit(
            txt(
                sender_id,
                "extended_success",
                user_id=uid,
                exp=_fmt_date(new_exp),
                days=_days_left(new_exp),
            ),
            buttons=build_user_detail_keyboard(uid),
        )
    elif key == "set_days":
        try:
            uid = int(val)
        except Exception:
            await event.answer(txt(sender_id, "invalid_id"), alert=True)
            return
        try:
            async with standard_conversation(sender_id, timeout=120) as conv:
                await conv.send_message(
                    txt(sender_id, "set_days_prompt"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_user_list"),
                )
                days_txt = (await menu_safe_response(conv)).raw_text.strip()
                try:
                    days = max(0, int(days_txt))
                except Exception:
                    await conv.send_message(txt(sender_id, "invalid_number"))
                    return
                new_exp = await set_user_days_from_now(uid, days)
                await conv.send_message(
                    txt(
                        sender_id,
                        "set_days_success",
                        exp=_fmt_date(new_exp),
                        days=_days_left(new_exp),
                    )
                )
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
        await event.respond(
            txt(sender_id, "back_to_details"), buttons=build_user_detail_keyboard(uid)
        )
    elif key == "del_user":
        try:
            uid = int(val)
        except Exception:
            await event.answer(txt(sender_id, "invalid_id"), alert=True)
            return
        await del_user(uid)
        await event.edit(
            txt(sender_id, "user_deleted"), buttons=admin_back_buttons(sender_id)
        )
        total, items = await list_users_page(sender_id, 0)
        await event.edit(
            txt(sender_id, "users_list"),
            buttons=build_users_keyboard(sender_id, total, items, 0),
        )

    elif key == "transfer_accounts":
        try:
            async with standard_conversation(sender_id, timeout=180) as conv:
                await conv.send_message(
                    txt(sender_id, "transfer_prompt_src"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_back"),
                )
                src_raw = (await menu_safe_response(conv)).raw_text.strip()
                try:
                    src_uid = int(src_raw)
                except Exception:
                    await conv.send_message(txt(sender_id, "invalid_input"))
                    return
                await conv.send_message(
                    txt(sender_id, "transfer_prompt_dst"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_back"),
                )
                dst_raw = (await menu_safe_response(conv)).raw_text.strip()
                try:
                    dst_uid = int(dst_raw)
                except Exception:
                    await conv.send_message(txt(sender_id, "invalid_input"))
                    return
                if src_uid == dst_uid:
                    await conv.send_message(txt(sender_id, "transfer_self"))
                    return
                cnt = db["accounts"].count_documents({"admin_id": src_uid})
                if cnt == 0:
                    await conv.send_message(txt(sender_id, "source_no_accounts"))
                    return
                await conv.send_message(
                    txt(
                        sender_id, "transfer_confirm", cnt=cnt, src=src_uid, dst=dst_uid
                    ),
                    buttons=conv_cancel_buttons(sender_id, b"admin_back"),
                )
                confirm = await menu_safe_response(conv)
                if confirm.raw_text.strip() != txt(sender_id, "yes"):
                    await conv.send_message(txt(sender_id, "transfer_cancelled"))
                    return
                db["accounts"].update_many(
                    {"admin_id": src_uid}, {"$set": {"admin_id": dst_uid}}
                )
                await conv.send_message(txt(sender_id, "transfer_done", cnt=cnt))
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
        await admin_entry(event)
    elif key == "set_start_msg":
        try:
            async with standard_conversation(sender_id, timeout=120) as conv:
                await conv.send_message(
                    txt(sender_id, "set_start_msg_prompt"),
                    buttons=conv_cancel_buttons(sender_id, b"admin_back"),
                )
                text = (await menu_safe_response(conv)).raw_text.strip()
                if text == "-":
                    db.settings.delete_one({"key": "start_message"})
                    await conv.send_message(txt(sender_id, "start_msg_reset"))
                else:
                    db.settings.update_one(
                        {"key": "start_message"}, {"$set": {"value": text}}, upsert=True
                    )
                    await conv.send_message(txt(sender_id, "start_msg_saved"))
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
        await admin_entry(event)
    elif key == "restart_bot":
        try:
            await event.answer()
        except Exception:
            pass
        try:
            if not await redis.set("restart_bot_lock", "1", ex=120, nx=True):
                return
        except Exception:
            pass
        try:
            await event.edit(
                "♻️ ری‌استارت امن شروع شد...\nدر حال توقف عملیات فعال و پاکسازی lockها.",
                buttons=admin_back_buttons(sender_id),
            )
        except Exception:
            pass
        try:
            stopped = await stop_all_reports_for_restart()
            await redis.set("bot_pending_restart", str(sender_id), ex=600)
            await event.reply(
                "✅ پاکسازی انجام شد. بات تا چند لحظه دیگر ری‌استارت می‌شود."
                + (f"\n🛑 ریپورت‌های متوقف‌شده: {stopped}" if stopped else ""),
            )
        except Exception as e:
            await redis.delete("restart_bot_lock")
            await event.respond(
                txt(sender_id, "restart_error", error=e),
                buttons=admin_back_buttons(sender_id),
            )
            return
        task = asyncio.get_running_loop().create_task(restart_process())
        _RESTART_TASKS.add(task)

    elif key == "manage_purchase_requests":
        pending = get_pending_requests()
        if not pending:
            await event.edit(
                txt(sender_id, "no_pending_requests"),
                buttons=admin_back_buttons(sender_id),
            )
            return
        text = txt(sender_id, "pending_requests_title") + "\n\n"
        rows = []
        for req in pending:
            rid = str(req["_id"])
            uid_req = req["user_id"]
            price = req["price"]
            plan = req["plan"]
            plan_name = get_plan_name(sender_id, plan)
            if (req.get("payment_method") or "") == "crypto":
                amt = format_crypto_amount(float(req.get("crypto_amount") or 0), 4)
                asset = req.get("crypto_asset") or "GRAM"
                text += f"👤 {uid_req} • {plan_name} • {amt} {asset} ({price} تومان)\n"
            else:
                text += f"👤 {uid_req} • {plan_name} • {price} تومان\n"
            rows.append(
                [
                    Button.inline(
                        "✅[5206607081334906820]",
                        data=f"purchase_approve:{rid}".encode(),
                    ),
                    Button.inline(
                        "❌[5210952531676504517]",
                        data=f"purchase_reject:{rid}".encode(),
                    ),
                ]
            )
        rows.append([Button.inline(txt(sender_id, "back_btn"), data=b"admin_back")])
        await event.edit(text, buttons=rows)

def migrate_old_prices():
    doc = SETTINGS_COL.find_one({"key": "subscription_prices"})
    if not doc:
        return
    data = doc.get("value", {})
    changed = False
    for plan in ["normal", "special"]:
        if isinstance(data.get(plan), dict):
            old = data[plan]
            data[plan] = [
                {
                    "days": old.get("days", 30),
                    "price": old.get("price", 0),
                    "max_accounts": old.get("max_accounts", 0),
                }
            ]
            changed = True
    if changed:
        SETTINGS_COL.update_one(
            {"key": "subscription_prices"}, {"$set": {"value": data}}
        )

@bot.on(events.CallbackQuery(pattern=b"purchase_approve:|purchase_reject:"))
async def handle_purchase_decision(event):
    await event.answer()
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return

    data = event.data.decode()
    parts = data.split(":")
    action_full = parts[0]
    request_id = parts[1]

    action = action_full.split("_")[1]

    req = PURCHASE_REQUESTS_COL.find_one(
        {
            "_id": ObjectId(request_id),
            "status": {"$in": ["pending", "pending_crypto"]},
        }
    )
    if not req:
        await event.respond(
            txt(uid, "request_invalid"), buttons=admin_back_buttons(uid)
        )
        return

    if action == "approve":
        user_id = req["user_id"]
        plan = req["plan"]
        await approve_purchase_request(req, approved_by=uid)
        await event.edit(
            txt(uid, "purchase_approved_admin", user=user_id, plan=plan),
            buttons=admin_back_buttons(uid),
        )

    else:
        try:
            async with standard_conversation(uid, timeout=60) as conv:
                await conv.send_message(
                    txt(uid, "enter_reject_reason"),
                    buttons=conv_cancel_buttons(uid, b"admin_back"),
                )
                reason_resp = await menu_safe_response(conv)
                reason = reason_resp.raw_text.strip()
                if reason == "-":
                    reason = ""

        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
        update_purchase_request_status(request_id, "rejected", admin_note=reason)
        try:
            await bot.send_message(
                req["user_id"],
                txt(
                    req["user_id"],
                    "purchase_rejected",
                    reason=reason or txt(req["user_id"], "no_reason_provided"),
                ),
            )
        except Exception:
            pass
        await event.edit(
            txt(uid, "purchase_rejected_admin", user=req["user_id"]),
            buttons=admin_back_buttons(uid),
        )

async def send_price_plan_menu(chat_id, plan, edit_msg=None):
    packages = get_packages(plan)
    text = txt(chat_id, "packages_list_title", plan=txt(chat_id, f"plan_{plan}"))
    rows = []
    for idx, pkg in enumerate(packages):
        days = pkg["days"]
        price = pkg["price"]
        max_acc = pkg.get("max_accounts", 0)
        if plan == "special":
            label = f"{days} {txt(chat_id, 'days')} - {price} {txt(chat_id, 'currency')} - {txt(chat_id, 'max_accounts_label')}: {max_acc}"
        else:
            label = (
                f"{days} {txt(chat_id, 'days')} - {price} {txt(chat_id, 'currency')}"
            )
        rows.append(
            [
                Button.inline(label, data=b"noop"),
                Button.inline(
                    "❌[5210952531676504517]",
                    data=f"price_remove:{plan}:{idx}".encode(),
                ),
            ]
        )
    rows.append(
        [
            Button.inline(
                txt(chat_id, "add_package_btn"), data=f"price_add:{plan}".encode()
            )
        ]
    )
    rows.append([Button.inline(txt(chat_id, "back_btn"), data=b"set_prices")])
    if edit_msg:
        await edit_msg.edit(text, buttons=rows)
    else:
        await bot.send_message(chat_id, text, buttons=rows)

@bot.on(events.CallbackQuery(pattern=b"pkg_cancel"))
async def package_cancel(event):
    uid = UID(event)
    try:
        PURCHASE_REQUESTS_COL.update_many(
            {"user_id": uid, "status": "pending_crypto"},
            {
                "$set": {
                    "status": "cancelled",
                    "resolved_at": datetime.datetime.now(datetime.timezone.utc),
                }
            },
        )
    except Exception:
        pass
    await ctx_pop(uid)
    await event.edit(
        txt(uid, "cancel"),
        buttons=[[Button.inline(txt(uid, "back_btn"), data=b"start_menu")]],
    )

@bot.on(events.CallbackQuery(pattern=b"plus_add"))
async def plus_add(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    try:
        try:
            async with standard_conversation(uid, timeout=180) as conv:
                await conv.send_message(
                    txt(uid, "plus_add_user_prompt"),
                    buttons=conv_cancel_buttons(uid, b"plus_manage"),
                )
                try:
                    user_resp = await menu_safe_response(conv)
                    user_id = int(user_resp.raw_text.strip())
                except Exception:
                    await conv.send_message(txt(uid, "invalid_input"))
                    return
                await conv.send_message(
                    txt(uid, "plus_add_days_prompt"),
                    buttons=conv_cancel_buttons(uid, b"plus_manage"),
                )
                try:
                    days_resp = await menu_safe_response(conv)
                    days = int(days_resp.raw_text.strip())
                    if days <= 0:
                        raise ValueError
                except Exception:
                    await conv.send_message(txt(uid, "invalid_number"))
                    return
                await conv.send_message(
                    txt(uid, "plus_add_max_prompt"),
                    buttons=conv_cancel_buttons(uid, b"plus_manage"),
                )
                try:
                    max_resp = await menu_safe_response(conv)
                    max_acc = int(max_resp.raw_text.strip())
                    if max_acc < 0:
                        raise ValueError
                except Exception:
                    await conv.send_message(txt(uid, "invalid_number"))
                    return
                exp = add_plus_subscription(
                    user_id, days, max_acc, added_by=uid, admin_id=uid
                )
                await conv.send_message(
                    txt(uid, "plus_added", user_id=user_id, exp=_fmt_date(exp))
                )
        except MenuInterrupt:
            return
        except asyncio.TimeoutError:
            try:
                _tuid = UID(event)
                await ctx_pop(_tuid)
                await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
            except Exception:
                pass
            return
    except Exception as e:
        await event.respond(
            txt(uid, "error_occurred", error=str(e)), buttons=admin_back_buttons(uid)
        )
    await plus_manage(event)

@bot.on(events.CallbackQuery(pattern=b"plus_manage"))
async def plus_manage(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    subs = list(PLUS_SUBS_COL.find().sort("created_at", -1))
    if not subs:
        text = txt(uid, "plus_no_active")
        buttons = [[Button.inline(txt(uid, "plus_add_btn"), data=b"plus_add")]]
    else:
        text = txt(uid, "plus_list_title") + "\n\n"
        buttons = []
        for sub in subs:
            user_id = sub.get("user_id")
            admin_id = sub.get("admin_id")
            expires_at = sub.get("expires_at", 0)
            max_acc = sub.get("max_accounts", 0)
            days = _days_left(expires_at)
            status = (
                "🟢[5915872676311733083]" if days > 0 else "🔴[6044112265102235520]"
            )
            text += (
                txt(
                    uid,
                    "plus_sub_info",
                    status=status,
                    user_id=user_id,
                    admin_id=admin_id,
                    max=max_acc,
                    days=days,
                )
                + "\n"
            )
            buttons.append(
                [
                    Button.inline(
                        txt(uid, "plus_extend_btn", user_id=user_id),
                        data=f"plus_extend={user_id}".encode(),
                    ),
                    Button.inline(
                        txt(uid, "plus_set_max_btn", max=max_acc),
                        data=f"plus_setmax={user_id}".encode(),
                    ),
                    Button.inline(
                        txt(uid, "plus_remove_btn"),
                        data=f"plus_remove={user_id}".encode(),
                    ),
                ]
            )
        buttons.append([Button.inline(txt(uid, "plus_add_btn"), data=b"plus_add")])
        buttons.append([Button.inline(txt(uid, "plus_refresh"), data=b"plus_manage")])
    buttons.append([Button.inline(txt(uid, "admin_back_general"), data=b"admin_back")])
    try:
        if event.data.decode() == "plus_manage":
            await event.delete()
            await event.respond(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)
    except errors.MessageNotModifiedError:
        pass

@bot.on(events.CallbackQuery(pattern=b"plus_extend="))
async def plus_extend(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    user_id = int(event.data.decode().split("=")[1])
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                txt(uid, "plus_extend_prompt", user_id=user_id),
                buttons=conv_cancel_buttons(uid, b"plus_manage"),
            )
            resp = await menu_safe_response(conv)
            try:
                days = int(resp.raw_text.strip())
                if days <= 0:
                    raise ValueError
            except Exception:
                await conv.send_message(txt(uid, "invalid_number"))
                return
            new_exp = extend_plus_subscription(user_id, days)
            if new_exp:
                await conv.send_message(
                    txt(uid, "plus_extended", exp=_fmt_date(new_exp))
                )
            else:
                await conv.send_message(txt(uid, "plus_not_found"))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return
    await plus_manage(event)

@bot.on(events.CallbackQuery(pattern=b"plus_setmax="))
async def plus_set_max(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    user_id = int(event.data.decode().split("=", 1)[1])
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                txt(uid, "plus_set_max_prompt", user_id=user_id),
                buttons=conv_cancel_buttons(uid, b"plus_manage"),
            )
            resp = await menu_safe_response(conv)
            try:
                max_acc = int(resp.raw_text.strip())
                if max_acc < 0:
                    raise ValueError()
            except Exception:
                await conv.send_message(txt(uid, "invalid_number"))
                return
            if set_plus_max_accounts(user_id, max_acc):
                await conv.send_message(
                    txt(
                        uid,
                        "plus_set_max_done",
                        user_id=user_id,
                        max=max_acc if max_acc > 0 else "∞",
                    )
                )
            else:
                await conv.send_message(txt(uid, "plus_not_found"))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return
    await plus_manage(event)

@bot.on(events.CallbackQuery(pattern=b"plus_remove="))
async def plus_remove(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    user_id = int(event.data.decode().split("=")[1])
    try:
        async with standard_conversation(uid, timeout=30) as conv:
            await conv.send_message(
                txt(uid, "plus_remove_confirm", user_id=user_id),
                buttons=conv_cancel_buttons(uid, b"plus_manage"),
            )
            resp = await menu_safe_response(conv)
            if resp.raw_text.strip() != txt(uid, "yes"):
                await conv.send_message(txt(uid, "transfer_cancelled"))
                return
            remove_plus_subscription(user_id)
            await conv.send_message(txt(uid, "plus_removed", user_id=user_id))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return
    await plus_manage(event)

def build_permissions_menu_keyboard(uid: int):
    return [
        [
            Button.inline(f"{txt(uid, 'perm_normal_btn')}", data=b"perm_plan:normal"),
            Button.inline(f"{txt(uid, 'perm_special_btn')}", data=b"perm_plan:special"),
        ],
        [Button.inline(txt(uid, "back_btn"), data=b"admin_back")],
    ]

@bot.on(events.CallbackQuery(pattern=b"admin_permissions"))
async def admin_permissions_menu(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    await ctx_pop(uid)
    text = txt(uid, "permissions_menu_text")
    buttons = build_permissions_menu_keyboard(uid)
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)

def build_perm_plan_keyboard(uid: int, plan: str):
    perms = get_panel_permissions()
    active = set(perms.get(plan, []))
    rows = []
    current_row = []

    for index, action in enumerate(PANEL_PERMISSION_ACTIONS):
        icon = (
            txt(uid, "perm_enabled_icon")
            if action in active
            else txt(uid, "perm_disabled_icon")
        )
        label = f"{icon} {txt(uid, f'menu_{action}')}"
        current_row.append(
            Button.inline(label, data=f"perm_toggle:{plan}:{action}".encode())
        )

        if len(current_row) == 2 or index == len(PANEL_PERMISSION_ACTIONS) - 1:
            rows.append(current_row)
            current_row = []

    rows.append([Button.inline(txt(uid, "back_btn"), data=b"admin_permissions")])
    return rows

@bot.on(events.CallbackQuery(pattern=b"perm_plan:"))
async def perm_plan_menu(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    plan = event.data.decode().split(":")[1]
    text = txt(uid, "perm_plan_menu_text", plan=txt(uid, f"plan_{plan}"))
    buttons = build_perm_plan_keyboard(uid, plan)
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)

@bot.on(events.CallbackQuery(pattern=b"perm_toggle:"))
async def perm_toggle(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    parts = event.data.decode().split(":")
    plan = parts[1]
    action = parts[2]
    toggle_panel_permission(plan, action)
    text = txt(uid, "perm_plan_menu_text", plan=txt(uid, f"plan_{plan}"))
    buttons = build_perm_plan_keyboard(uid, plan)
    try:
        await event.edit(text, buttons=buttons)
    except errors.MessageNotModifiedError:
        pass

@bot.on(events.CallbackQuery(pattern=b"admin_sessions="))
async def admin_show_sessions(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    sid = event.data.decode().split("=", 1)[1].split(":", 1)[0]
    client = retern_client(sid)
    if not client:
        await event.answer(txt(uid, "session_invalid"), alert=True)
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await drop_account(sid, reason="unauthorized")
            await event.answer(txt(uid, "not_logged_in"), alert=True)
            return
        auths = await client(GetAuthorizationsRequest())
        try:
            await event.answer()
        except Exception:
            pass
        if not auths.authorizations:
            await event.respond(
                txt(uid, "no_sessions"), buttons=admin_back_buttons(uid)
            )
            return
        rows = []
        for auth in auths.authorizations:
            device = auth.device_model or txt(uid, "unknown_device")
            location = auth.country or txt(uid, "unknown_location")
            if auth.date_created:
                if isinstance(auth.date_created, datetime.datetime):
                    date_str = auth.date_created.strftime("%Y-%m-%d %H:%M")
                else:
                    date_str = datetime.datetime.fromtimestamp(
                        auth.date_created
                    ).strftime("%Y-%m-%d %H:%M")
            else:
                date_str = txt(uid, "unknown_date")
            status = (
                txt(uid, "session_current")
                if auth.current
                else txt(uid, "session_other")
            )
            label = f"{status} {device} • {location} • {date_str}"
            if auth.current:
                rows.append([Button.inline(label, data=b"noop")])
            else:
                rows.append(
                    [
                        Button.inline(label, data=b"noop"),
                        Button.inline(
                            txt(uid, "kill_session_btn"),
                            data=f"admin_kill_session={sid}:{auth.hash}".encode(),
                        ),
                    ]
                )
        rows.append(
            [
                Button.inline(
                    txt(uid, "kill_all_btn"),
                    data=f"admin_kill_all_sessions={sid}".encode(),
                )
            ]
        )
        acc = db["accounts"].find_one({"_id": ObjectId(sid)})
        admin_id = acc.get("admin_id") if acc else None
        if admin_id:
            rows.append(
                [
                    Button.inline(
                        txt(uid, "admin_back_to_list"),
                        data=f"admin_view_user={admin_id}".encode(),
                    )
                ]
            )
        else:
            rows.append(
                [Button.inline(txt(uid, "admin_back_general"), data=b"admin_back")]
            )
        await event.edit(txt(uid, "active_sessions_title", sid=sid[:6]), buttons=rows)
    except errors.RPCError as e:
        await event.respond(
            txt(uid, "error_occurred", error=e.__class__.__name__),
            buttons=admin_back_buttons(uid),
        )
    finally:
        await client.disconnect()

@bot.on(events.CallbackQuery(pattern=b"admin_kill_all_sessions="))
async def admin_kill_all_sessions(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    sid = event.data.decode().split("=")[1]
    client = retern_client(sid)
    if not client:
        await event.answer(txt(uid, "session_invalid"), alert=True)
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await drop_account(sid, reason="unauthorized")
            await event.answer(txt(uid, "not_logged_in"), alert=True)
            return
        auths = await client(GetAuthorizationsRequest())
        count = 0
        for auth in auths.authorizations:
            if not auth.current:
                await client(ResetAuthorizationRequest(hash=auth.hash))
                count += 1
        await event.answer(txt(uid, "sessions_killed", count=count), alert=True)
        await admin_show_sessions(event)
    except errors.RPCError as e:
        await event.respond(
            txt(uid, "error_occurred", error=e.__class__.__name__),
            buttons=admin_back_buttons(uid),
        )
    finally:
        await client.disconnect()

@bot.on(events.CallbackQuery(pattern=b"admin_kill_session="))
async def admin_kill_session(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    parts = event.data.decode().split("=")
    sid = parts[1].split(":")[0]
    auth_hash = int(parts[1].split(":")[1])
    client = retern_client(sid)
    if not client:
        await event.answer(txt(uid, "session_invalid"), alert=True)
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await drop_account(sid, reason="unauthorized")
            await event.answer(txt(uid, "not_logged_in"), alert=True)
            return
        await client(ResetAuthorizationRequest(hash=auth_hash))
        await event.answer(txt(uid, "session_killed"), alert=True)
        await admin_show_sessions(event)
    except errors.RPCError as e:
        await event.respond(
            txt(uid, "error_occurred", error=e.__class__.__name__),
            buttons=admin_back_buttons(uid),
        )
    finally:
        await client.disconnect()

@bot.on(events.CallbackQuery(pattern=b"admin_delete_session="))
async def admin_delete_session(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    sid = event.data.decode().split("=")[1]
    try:
        acc = db["accounts"].find_one({"_id": ObjectId(sid)})
    except Exception:
        acc = None
    await drop_account(sid, send_notification=False)
    await event.answer(txt(uid, "account_removed"), alert=True)
    if acc and acc.get("admin_id"):
        admin_id = acc["admin_id"]
        await event.edit(
            txt(uid, "account_deleted_back"),
            buttons=[
                [
                    Button.inline(
                        txt(uid, "admin_back_to_list"),
                        data=f"admin_view_user={admin_id}".encode(),
                    )
                ]
            ],
        )
    else:
        await event.edit(
            txt(uid, "account_deleted_back"),
            buttons=[
                [Button.inline(txt(uid, "admin_back_general"), data=b"admin_back")]
            ],
        )

@bot.on(events.CallbackQuery(pattern=b"admin_delete_all_user_sessions="))
async def admin_delete_all_user_sessions(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    target_admin = int(event.data.decode().split("=")[1])
    count = db["accounts"].count_documents({"admin_id": target_admin})
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                txt(uid, "delete_user_confirm", count=count, user_id=target_admin),
                buttons=conv_cancel_buttons(uid, b"admin_user_list"),
            )
            resp = await menu_safe_response(conv)
            if resp.raw_text.strip() != txt(uid, "yes"):
                await conv.send_message(txt(uid, "transfer_cancelled"))
                return
            accs = list(db["accounts"].find({"admin_id": target_admin}, {"_id": 1}))
            deleted = 0
            for acc in accs:
                await drop_account(str(acc["_id"]), send_notification=False)
                deleted += 1
            await conv.send_message(txt(uid, "delete_all_done", count=deleted))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return
    await event.edit(
        txt(uid, "back_to_user_list"),
        buttons=[[Button.inline(txt(uid, "user_list_btn"), data=b"admin_user_list")]],
    )

@bot.on(events.CallbackQuery(pattern=b"admin_delete_all_sessions"))
async def admin_delete_all_sessions(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    total = db["accounts"].count_documents({})
    try:
        async with standard_conversation(uid, timeout=60) as conv:
            await conv.send_message(
                txt(uid, "delete_all_confirm", total=total),
                buttons=conv_cancel_buttons(uid, b"admin_back"),
            )
            resp = await menu_safe_response(conv)
            if resp.raw_text.strip() != txt(uid, "yes"):
                await conv.send_message(txt(uid, "transfer_cancelled"))
                return
            all_accs = list(db["accounts"].find({}, {"_id": 1}))
            count = 0
            for acc in all_accs:
                await drop_account(str(acc["_id"]), send_notification=False)
                count += 1
            await conv.send_message(txt(uid, "delete_all_done", count=count))
    except MenuInterrupt:
        return
    except asyncio.TimeoutError:
        try:
            _tuid = UID(event)
            await ctx_pop(_tuid)
            await bot.send_message(_tuid, txt(_tuid, "timeout_or_invalid"))
        except Exception:
            pass
        return
    await event.edit(
        txt(uid, "delete_all_final"),
        buttons=[[Button.inline(txt(uid, "back_btn"), data=b"admin_back")]],
    )

async def _probe_account_health(sess_id: str) -> dict:
    """Deep health check: connect → auth → get_me → freeze-sensitive RPCs."""
    acc = None
    try:
        acc = db["accounts"].find_one({"_id": ObjectId(sess_id)})
    except Exception:
        pass
    phone = (acc or {}).get("phone") or sess_id[-6:]
    admin_id = (acc or {}).get("admin_id")
    result = {
        "sess": sess_id,
        "phone": phone,
        "admin_id": admin_id,
        "status": "error",
        "detail": "",
        "user_id": None,
    }

    prefer_proxy = bool(list_enabled_proxies())
    order = (True, False) if prefer_proxy else (False, True)
    last_err = "CONNECT_FAILED"

    async def _mark_and_return(cli, status: str, detail: str):
        result["status"] = status
        result["detail"] = detail
        await _safe_disconnect(cli)
        try:
            db["accounts"].update_one(
                {"_id": ObjectId(sess_id)},
                {
                    "$set": {
                        "health_status": status,
                        "health_checked_at": _ts_now(),
                        "health_detail": str(detail)[:120],
                    }
                },
            )
        except Exception:
            pass
        return result

    for use_proxy in order:
        cli = retern_client(sess_id, use_proxy=use_proxy)
        if not cli:
            last_err = "BAD_SESSION"
            continue
        try:
            await asyncio.wait_for(cli.connect(), timeout=25)
            if not await cli.is_user_authorized():
                last_err = "NOT_AUTHORIZED"
                await _safe_disconnect(cli)
                continue

            me = await asyncio.wait_for(cli.get_me(), timeout=20)
            if not me:
                last_err = "NO_ME"
                await _safe_disconnect(cli)
                continue
            result["user_id"] = getattr(me, "id", None)
            if getattr(me, "phone", None):
                result["phone"] = me.phone

            try:
                await asyncio.wait_for(
                    cli(functions.account.UpdateStatusRequest(offline=False)),
                    timeout=15,
                )
            except Exception as e:
                if _is_frozen(e):
                    return await _mark_and_return(cli, "frozen", e.__class__.__name__)
                if _is_unauthorized_like(e) or _should_drop_session_error(e):
                    return await _mark_and_return(cli, "dead", e.__class__.__name__)
                last_err = e.__class__.__name__

            freeze_probes = (
                (
                    "Search",
                    lambda: cli(
                        functions.contacts.SearchRequest(q="telegram", limit=1)
                    ),
                ),
                (
                    "ResolveUsername",
                    lambda: cli(
                        functions.contacts.ResolveUsernameRequest(username="telegram")
                    ),
                ),
                (
                    "GetAuthorizations",
                    lambda: cli(GetAuthorizationsRequest()),
                ),
            )
            for label, factory in freeze_probes:
                try:
                    await asyncio.wait_for(factory(), timeout=20)
                except Exception as e:
                    if _is_frozen(e):
                        logger.info(
                            "health freeze hit phone=%s via %s: %s",
                            result["phone"],
                            label,
                            e,
                        )
                        return await _mark_and_return(
                            cli, "frozen", f"{label}:{e.__class__.__name__}"
                        )
                    if _is_unauthorized_like(e) or _should_drop_session_error(e):
                        return await _mark_and_return(cli, "dead", e.__class__.__name__)
                    msg = (getattr(e, "message", None) or str(e) or "").upper()
                    if "FROZEN" in msg:
                        return await _mark_and_return(
                            cli, "frozen", f"{label}:{e.__class__.__name__}"
                        )
                    last_err = f"{label}:{e.__class__.__name__}"

            result["status"] = "ok"
            result["detail"] = "proxy" if use_proxy else "direct"
            await _safe_disconnect(cli)
            try:
                db["accounts"].update_one(
                    {"_id": ObjectId(sess_id)},
                    {
                        "$set": {
                            "health_status": "ok",
                            "health_checked_at": _ts_now(),
                            "health_detail": result["detail"],
                        }
                    },
                )
            except Exception:
                pass
            return result
        except Exception as e:
            last_err = e.__class__.__name__
            if _is_frozen(e):
                return await _mark_and_return(cli, "frozen", last_err)
            if _is_unauthorized_like(e) or _should_drop_session_error(e):
                return await _mark_and_return(cli, "dead", last_err)
            await _safe_disconnect(cli)
            continue

    if last_err == "NOT_AUTHORIZED":
        result["status"] = "unauthorized"
    elif last_err in ("BAD_SESSION",):
        result["status"] = "dead"
    else:
        result["status"] = "error"
    result["detail"] = last_err
    try:
        db["accounts"].update_one(
            {"_id": ObjectId(sess_id)},
            {
                "$set": {
                    "health_status": result["status"],
                    "health_checked_at": _ts_now(),
                    "health_detail": str(last_err)[:120],
                }
            },
        )
    except Exception:
        pass
    return result



def _format_health_progress(uid, total, done, counts: dict) -> str:
    pct = round(100 * done / total) if total else 0
    bar_len = 20
    filled = round(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    return txt(
        uid,
        "acc_health_progress",
        total=total,
        done=done,
        ok=counts.get("ok", 0),
        frozen=counts.get("frozen", 0),
        unauthorized=counts.get("unauthorized", 0),
        dead=counts.get("dead", 0),
        error=counts.get("error", 0),
        progress_percent=pct,
        progress_bar=bar,
    )


def _format_health_report(uid, total, results: list[dict]) -> str:
    counts = {"ok": 0, "frozen": 0, "unauthorized": 0, "dead": 0, "error": 0}
    lines_bad = []
    for r in results:
        st = r.get("status") or "error"
        counts[st] = counts.get(st, 0) + 1
        if st != "ok":
            phone = r.get("phone") or "?"
            detail = r.get("detail") or ""
            lines_bad.append(f"• {phone} → {st}" + (f" ({detail})" if detail else ""))

    text = txt(
        uid,
        "acc_health_done",
        total=total,
        ok=counts.get("ok", 0),
        frozen=counts.get("frozen", 0),
        unauthorized=counts.get("unauthorized", 0),
        dead=counts.get("dead", 0),
        error=counts.get("error", 0),
    )
    if counts.get("busy"):
        text += "\n" + txt(uid, "acc_health_busy_line", busy=counts["busy"])
    if lines_bad:
        chunk = "\n".join(lines_bad[:40])
        if len(lines_bad) > 40:
            chunk += f"\n… +{len(lines_bad) - 40}"
        text += "\n\n" + txt(uid, "acc_health_bad_list") + "\n" + chunk
    if counts.get("error") or counts.get("busy"):
        text += "\n\n" + txt(uid, "acc_health_keep_note")
    return text


HEALTH_DELETABLE = ("frozen", "unauthorized", "dead")


async def run_account_health_scan(event, uid: int):
    accounts = list(db["accounts"].find({}, {"_id": 1, "phone": 1}))
    total = len(accounts)
    if total == 0:
        await event.respond(txt(uid, "ping_no_accounts"), buttons=admin_back_buttons(uid))
        return

    status_msg = await event.respond(txt(uid, "acc_health_loading", total=total))
    sem = asyncio.Semaphore(max(2, min(4, REPORT_CONCURRENCY)))
    results: list[dict] = []
    counts = {"ok": 0, "frozen": 0, "unauthorized": 0, "dead": 0, "error": 0}
    done = 0
    lock = asyncio.Lock()
    last_edit = 0

    async def one(acc):
        nonlocal done, last_edit
        sid = str(acc["_id"])
        try:
            busy = await is_account_locked(sid)
        except Exception:
            busy = False
        if busy:
            r = {
                "sess": sid,
                "phone": acc.get("phone") or sid[-6:],
                "status": "busy",
                "detail": "IN_REPORT",
            }
        else:
            async with sem:
                r = await _probe_account_health(sid)
        async with lock:
            results.append(r)
            st = r.get("status") or "error"
            counts[st] = counts.get(st, 0) + 1
            done += 1
            now = _ts_now()
            if done >= total or now - last_edit >= 5:
                try:
                    await status_msg.edit(
                        _format_health_progress(uid, total, done, counts)
                    )
                    last_edit = now
                except Exception:
                    pass

    await asyncio.gather(*(one(a) for a in accounts))

    bad = [r for r in results if r.get("status") in HEALTH_DELETABLE]
    await redis.set(
        f"acc_health_bad:{uid}",
        json.dumps(
            [{"sess": r["sess"], "status": r["status"], "phone": r.get("phone")} for r in bad],
            ensure_ascii=False,
        ),
        ex=3600,
    )

    text = _format_health_report(uid, total, results)
    buttons = admin_back_buttons(uid)
    if bad:
        buttons = [
            [
                Button.inline(
                    txt(uid, "acc_health_delete_bad_btn", n=len(bad)),
                    data=b"acc_health_del_bad",
                )
            ],
            [Button.inline(txt(uid, "acc_health_rerun_btn"), data=b"acc_health")],
        ] + buttons
    try:
        await status_msg.edit(text, buttons=buttons)
    except Exception:
        await event.respond(text, buttons=buttons)


@bot.on(events.CallbackQuery(pattern=rb"^acc_health(?:_del_bad)?$"))
async def admin_account_health(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        await event.answer()
        return
    data = (event.data or b"").decode()
    await event.answer()

    if data == "acc_health_del_bad":
        raw = await redis.get(f"acc_health_bad:{uid}")
        if not raw:
            await event.respond(txt(uid, "acc_health_no_cache"), buttons=admin_back_buttons(uid))
            return
        try:
            bad = json.loads(raw)
        except Exception:
            bad = []
        deleted = 0
        for item in bad:
            sid = item.get("sess")
            if not sid or item.get("status") not in HEALTH_DELETABLE:
                continue
            try:
                await drop_account(
                    sid,
                    reason=f"health:{item.get('status') or 'bad'}",
                    send_notification=False,
                )
                deleted += 1
            except Exception:
                pass
        await redis.delete(f"acc_health_bad:{uid}")
        await event.respond(
            txt(uid, "acc_health_deleted", n=deleted),
            buttons=admin_back_buttons(uid),
        )
        raise events.StopPropagation

    try:
        await event.edit(txt(uid, "acc_health_start"))
    except Exception:
        await event.respond(txt(uid, "acc_health_start"))
    await run_account_health_scan(event, uid)
    raise events.StopPropagation


@bot.on(events.NewMessage(pattern="/ping"))
async def ping_all_accounts(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    await run_account_health_scan(event, uid)


async def _dry_check_reason(cli, peer, msg_id: int, code: str, root_options):
    spec = _reason_spec(code)
    options = root_options
    steps: list = []
    for level in range(len(spec.levels)):
        choice = _pick_reason_option(options, code, level)
        if choice is None:
            return f"missing@{level}", steps, options
        steps.append((_option_key(choice), choice.text))
        if level == len(spec.levels) - 1:
            return "ok", steps, options
        try:
            await _global_report_gate()
            res = await cli(
                functions.messages.ReportRequest(
                    peer=peer, id=[msg_id], option=choice.option, message=""
                )
            )
        except errors.RPCError as e:
            info = _report_rpc_fail(
                "messages.report",
                e,
                peer=peer,
                ids=[msg_id],
                option=choice.option,
                reason=code,
                step=level + 1,
                mode="reportcheck",
            )
            return info, steps, options
        except Exception as e:
            return f"EXC_{e.__class__.__name__}", steps, options
        if not isinstance(res, types.ReportResultChooseOption):
            return f"ended@{level}:{type(res).__name__}", steps, options
        options = res.options
    return "ok", steps, options


def _reportcheck_peer_line(code: str) -> str:
    reason, note = _peer_reason_for_code(code)
    if note == REPORT_API_LIMIT_SCAM:
        return f"reportPeer: ⚠️ API has no Scam reason → {type(reason).__name__} + scam text"
    return f"reportPeer: {type(reason).__name__}"


@bot.on(events.NewMessage(pattern=r"^/reportcheck(?:@\w+)?(?:\s+(.+))?$"))
async def report_check_command(event):
    uid = UID(event)
    if uid not in ADMIN_IDS:
        return
    raw = ((event.pattern_match.group(1) if event.pattern_match else "") or "").strip()
    target, msg_ids = resolve_target_and_msg_ids(raw) if raw else ("", [])
    msg_ids = _safe_int32_ids(msg_ids or [])
    if not raw or _parse_target(target)[0] == "unknown" or not msg_ids:
        await event.respond(
            "Usage: /reportcheck https://t.me/<channel>/<post_id>\n"
            "Reads Telegram's report menu for that post with one account and shows the "
            "option every reason (Scam, Fake, ...) would use. No report is filed."
        )
        raise events.StopPropagation
    pool = await list_session_files(uid)
    if not pool:
        await event.respond(txt(uid, "no_session_found"))
        raise events.StopPropagation
    sess = await resolve_probe_session(event, uid, pool)
    if not sess:
        raise events.StopPropagation
    cli, fail = await connect_session_client(sess)
    if not cli:
        await event.respond(f"❌ account connect failed: {fail}")
        raise events.StopPropagation
    msg_id = msg_ids[0]
    try:
        peer = await join_group(cli, target, sess, skip_join=True)
        if not _is_valid_peer(peer):
            await event.respond(
                f"❌ cannot open {target} with this account: {_join_fail_reason(peer) or 'JOIN_FAILED'}"
            )
            raise events.StopPropagation
        try:
            res = await cli(
                functions.messages.ReportRequest(peer=peer, id=[msg_id], option=b"", message="")
            )
        except errors.RPCError as e:
            info = _report_rpc_fail(
                "messages.report", e, peer=peer, ids=[msg_id], option=b"", step=0, mode="reportcheck"
            )
            await event.respond(f"❌ {target}/{msg_id}: {info}")
            raise events.StopPropagation
        if not isinstance(res, types.ReportResultChooseOption):
            await event.respond(f"⚠️ Telegram returned {type(res).__name__} instead of a menu.")
            raise events.StopPropagation
        root = res.options
        lines = [
            f"🔎 Report check {target} post {msg_id} (account {sess_phone(sess)})",
            "Telegram menu: " + ", ".join(f"{_option_key(o)}={o.text}" for o in root),
            "",
        ]
        for code in REPORT_REASON_CODES:
            status, steps, last_opts = await _dry_check_reason(cli, peer, msg_id, code, root)
            path_txt = " › ".join(f"{k} «{t}»" for k, t in steps) or "-"
            label = _reason_label(code)
            if status == "ok":
                line = f"✅ {label}: messages.report {path_txt}"
                if code == "scam":
                    subs = _reason_sub_options(last_opts, "scam")
                    line += " | scam options: " + ", ".join(
                        f"{_option_key(o)} «{o.text}»" for o in subs
                    )
            elif status.startswith("missing@"):
                line = f"❌ {label}: Telegram did not offer this option (level {status[8:]}, after: {path_txt})"
            else:
                line = f"❌ {label}: {status} (after: {path_txt})"
            lines.append(line)
            lines.append(f"    {_reportcheck_peer_line(code)}")
            logger.info("reportcheck target=%s msg=%s code=%s status=%s path=%s", target, msg_id, code, status, steps)
        text = "\n".join(lines)
        for i in range(0, len(text), 3900):
            await event.respond(text[i : i + 3900], link_preview=False)
    finally:
        await _safe_disconnect(cli)
    raise events.StopPropagation

async def send_expiry_reminders():
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    threshold = 3
    contact_btn = [[Button.url("💬 @nifrtt", "https://t.me/nifrtt")]]

    async def _notify(user_id, expires_at, col, id_query):
        days = _days_left(expires_at)
        if days > threshold:
            return
        if days > 0:
            last_reminder = (col.find_one(id_query) or {}).get("last_reminder_date", "")
            if last_reminder == today:
                return
            try:
                lang = get_user_lang(user_id)
                text = TRANSLATIONS["subscription_expiring_reminder"][lang].format(
                    days=days, date=_fmt_date(expires_at)
                )
                await bot.send_message(user_id, text, buttons=contact_btn)
                col.update_one(id_query, {"$set": {"last_reminder_date": today}})
            except Exception:
                pass
            return

        last_exp = (col.find_one(id_query) or {}).get("last_expired_reminder_date", "")
        if last_exp == today:
            return

        rem = _as_expiry_ts(expires_at) - _ts_now()
        if rem < -86400:
            return
        try:
            lang = get_user_lang(user_id)
            text = TRANSLATIONS["subscription_expired_reminder"][lang]
            await bot.send_message(user_id, text, buttons=contact_btn)
            col.update_one(id_query, {"$set": {"last_expired_reminder_date": today}})
        except Exception:
            pass

    for sub in list(SUBS_COL.find({})):
        user_id = sub.get("user_id")
        if user_id is None:
            continue
        try:
            uid = int(user_id)
        except Exception:
            continue
        await _notify(
            uid,
            sub.get("expires_at", 0),
            SUBS_COL,
            _user_id_filter(uid),
        )

    for sub in list(PLUS_SUBS_COL.find({})):
        user_id = sub.get("user_id")
        if user_id is None:
            continue
        try:
            uid = int(user_id)
        except Exception:
            continue
        await _notify(
            uid,
            sub.get("expires_at", 0),
            PLUS_SUBS_COL,
            _user_id_filter(uid),
        )

async def daily_reminder_task():
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            target = now.replace(hour=13, minute=0, second=0, microsecond=0)
            if now >= target:
                target += dt.timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            await asyncio.sleep(sleep_seconds)
            await send_expiry_reminders()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("daily reminder task error: %s", e)
            await asyncio.sleep(60)

def _close_mongo_quiet():
    try:
        mongo.close()
    except Exception:
        pass

def main():
    import atexit
    import signal

    global _broadcast_running
    _broadcast_running = False

    atexit.register(_close_mongo_quiet)
    for _noisy in ("pymongo", "pymongo.topology", "pymongo.connection"):
        logging.getLogger(_noisy).setLevel(logging.ERROR)

    if not IS_RESELLER:
        try:
            resume_enabled_resellers()
        except Exception as e:
            logger.warning("reseller resume on boot: %s", e)
    loop = bot.loop
    if not loop.run_until_complete(acquire_instance_lock()):
        logger.critical(
            "another instance of this bot is still running with the same token; stop it first"
        )
        raise SystemExit(1)
    logger.info("Reporter bot is running…")
    bg_tasks = [
        loop.create_task(daily_reminder_task()),
        loop.create_task(join_request_checker_task()),
        loop.create_task(crypto_auto_verify_task()),
        loop.create_task(recover_stale_reports()),
        loop.create_task(instance_lock_keeper()),
        loop.create_task(announce_restart()),
    ]

    async def _graceful_shutdown():
        for t in bg_tasks:
            t.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        try:
            await release_instance_lock()
        except Exception:
            pass
        _close_mongo_quiet()
        try:
            await bot.disconnect()
        except Exception:
            pass

    def _on_stop(*_args):
        if not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_graceful_shutdown(), loop=loop))

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: _on_stop())
            except Exception:
                pass

    try:
        bot.run_until_disconnected()
    finally:
        for t in bg_tasks:
            if not t.done():
                t.cancel()
        _close_mongo_quiet()
        if _RESTART_REQUESTED:
            os.execv(sys.executable, [sys.executable] + sys.argv)

async def stop_all_reports_for_restart(wait_seconds: float = 8.0) -> int:
    live = list(_LIVE_REPORTS)
    for owner in live:
        try:
            await set_stop_flag(owner)
        except Exception:
            pass
    deadline = _time.monotonic() + wait_seconds
    while _LIVE_REPORTS and _time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    try:
        async for key in redis.scan_iter(match="report_active:*"):
            try:
                await clear_report_runtime(int(str(key).rsplit(":", 1)[-1]))
            except Exception:
                pass
    except Exception as e:
        logger.warning("restart cleanup: %s", e)
    return len(live)


_RESTART_REQUESTED = False
_RESTART_TASKS: set = set()


async def restart_process():
    global _RESTART_REQUESTED
    await asyncio.sleep(0.5)
    _RESTART_REQUESTED = True
    try:
        await release_instance_lock()
    except Exception:
        pass
    try:
        await asyncio.wait_for(bot.disconnect(), timeout=20)
    except Exception:
        pass
    _close_mongo_quiet()
    os.execv(sys.executable, [sys.executable] + sys.argv)


INSTANCE_TOKEN = secrets.token_hex(8)
INSTANCE_TTL = 30


def _instance_key() -> str:
    return f"bot_instance:{str(bot_token).strip().split(':', 1)[0]}"


async def acquire_instance_lock(rounds: int = 9) -> bool:
    key = _instance_key()
    for i in range(rounds):
        try:
            if await redis.set(key, INSTANCE_TOKEN, ex=INSTANCE_TTL, nx=True):
                return True
        except Exception as e:
            logger.warning("instance lock unavailable: %s", e)
            return True
        if i == 0:
            logger.warning("another bot instance is running; waiting for it to stop")
        await asyncio.sleep(5)
    return False


async def release_instance_lock():
    key = _instance_key()
    if await redis.get(key) == INSTANCE_TOKEN:
        await redis.delete(key)


async def instance_lock_keeper():
    key = _instance_key()
    while True:
        await asyncio.sleep(10)
        try:
            cur = await redis.get(key)
            if cur == INSTANCE_TOKEN:
                await redis.expire(key, INSTANCE_TTL)
            elif cur is None:
                await redis.set(key, INSTANCE_TOKEN, ex=INSTANCE_TTL, nx=True)
            else:
                logger.critical("another bot instance with the same token is running")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def announce_restart():
    try:
        raw = await redis.get("bot_pending_restart")
        await redis.delete("bot_pending_restart", "restart_bot_lock")
    except Exception:
        return
    if not raw:
        return
    try:
        admin = int(raw)
        await bot.send_message(admin, txt(admin, "restart_done"), buttons=admin_back_buttons(admin))
    except Exception as e:
        logger.warning("restart notice failed: %s", e)


async def recover_stale_reports():
    try:
        async for key in redis.scan_iter(match="report_active:*"):
            try:
                uid = int(str(key).rsplit(":", 1)[-1])
            except Exception:
                continue
            if uid in _LIVE_REPORTS:
                continue
            logger.warning("clearing report left from a previous run uid=%s", uid)
            await clear_report_runtime(uid)
    except Exception as e:
        logger.warning("recover stale reports: %s", e)

if __name__ == "__main__":
    main()
