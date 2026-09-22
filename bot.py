import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("videobot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")
MAX_MB = int(os.getenv("MAX_MB", "49"))            # حد تليجرام للبوتات العادية 50MB
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))
MAX_MINUTES = int(os.getenv("MAX_MINUTES", "20"))  # أطول فيديو مسموح
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)    # اختياري: لأمر /stats
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "8080"))              # مطلوب من Render/Koyeb عشان يعتبر الخدمة شغّالة
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")  # بدون @ ، مثال: mjeed_downloads

def prepare_cookies_file() -> str:
    """ينسخ ملف الكوكيز (لو موجود) لمكان قابل للكتابة، لأن yt-dlp يحاول يحدّثه
    و/etc/secrets/ في Render للقراءة فقط."""
    src = COOKIES_FILE
    if not src or not os.path.exists(src):
        return ""
    dest = "/tmp/cookies_local.txt" if os.path.isdir("/tmp") else "cookies_local.txt"
    try:
        shutil.copy(src, dest)
        log.info(f"cookies copied to writable path: {dest}")
        return dest
    except Exception:
        log.exception("failed to copy cookies file")
        return src


COOKIES_FILE_ACTIVE = prepare_cookies_file()
SUPPORTED = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "twitter.com", "x.com", "facebook.com", "fb.watch", "snapchat.com",
)

FORMATS = {
    "best": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[ext=mp4]/b",
    "720": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[ext=mp4]/b",
    "480": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b[ext=mp4]/b",
}

# ---------- قاعدة البيانات (كاش + إحصائيات) ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, file_id TEXT, kind TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen TEXT DEFAULT CURRENT_TIMESTAMP)")
db.execute(
    "CREATE TABLE IF NOT EXISTS downloads "
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, key TEXT, "
    "cached INTEGER DEFAULT 0, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
)
db.commit()

pending: dict[str, dict] = {}  # token -> {url, key, title}
sem = asyncio.Semaphore(MAX_PARALLEL)
dp = Dispatcher()


def track_user(user_id: int):
    db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    db.commit()


def track_download(user_id: int, key: str, cached: bool):
    db.execute(
        "INSERT INTO downloads(user_id, key, cached) VALUES(?,?,?)",
        (user_id, key, 1 if cached else 0),
    )
    db.commit()


def is_supported(url: str) -> bool:
    return any(d in url.lower() for d in SUPPORTED)


def fmt_duration(sec: int) -> str:
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def base_opts() -> dict:
    opts = {
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    if COOKIES_FILE_ACTIVE and os.path.exists(COOKIES_FILE_ACTIVE):
        opts["cookiefile"] = COOKIES_FILE_ACTIVE
    return opts


def extract_info_sync(url: str) -> dict:
    with yt_dlp.YoutubeDL(base_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            info = entries[0]
    return {
        "key": f"{info.get('extractor_key', 'x')}:{info.get('id', uuid.uuid4().hex)}",
        "title": (info.get("title") or "فيديو")[:100],
        "duration": info.get("duration") or 0,
    }


def download_sync(url: str, mode: str, workdir: str) -> Path:
    opts = base_opts()
    opts["outtmpl"] = f"{workdir}/%(id)s.%(ext)s"
    opts["merge_output_format"] = "mp4"
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        opts["format"] = FORMATS[mode]

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    files = sorted(Path(workdir).glob("*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("no file produced")
    return files[0]


def quality_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 أعلى جودة", callback_data=f"dl:best:{token}"),
                InlineKeyboardButton(text="📺 720p", callback_data=f"dl:720:{token}"),
            ],
            [
                InlineKeyboardButton(text="📱 480p", callback_data=f"dl:480:{token}"),
                InlineKeyboardButton(text="🎵 صوت MP3", callback_data=f"dl:audio:{token}"),
            ],
        ]
    )


def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 انضم للقناة", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton(text="✅ تحققت، تابع", callback_data="check_sub")],
        ]
    )


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if not CHANNEL_USERNAME:
        return True  # لو ما حطينا قناة، ما نفرض اشتراك
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        log.warning("subscription check failed")
        return True  # لو صار خطأ تقني، ما نمنع المستخدم


def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if "sign in" in msg or "login" in msg or "cookies" in msg:
        return "المنصة طلبت تسجيل دخول، الرابط قد يكون خاص أو يحتاج إعداد إضافي."
    if "private" in msg or "unavailable" in msg or "not found" in msg or "removed" in msg:
        return "الفيديو خاص أو محذوف."
    return "تعذّر التحميل، تأكد من الرابط وحاول مرة ثانية."


async def send_media(msg: Message, kind: str, media, caption: str) -> Message:
    if kind == "audio":
        return await msg.answer_audio(media, caption=caption)
    return await msg.answer_video(media, caption=caption, supports_streaming=True)


# ---------- الأوامر ----------
@dp.message(CommandStart())
async def start(m: Message):
    track_user(m.from_user.id)
    await m.answer(
        "أهلاً 👋\n"
        "أرسل لي رابط فيديو من يوتيوب، تيك توك (بدون علامة مائية)، انستقرام، "
        "تويتر وغيرها وأنا أحمّله لك بأفضل جودة.\n\n"
        "💡 لصق الرابط يكفي، ما تحتاج أوامر."
    )


@dp.message(Command("stats"))
async def stats(m: Message):
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        return
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    today = db.execute("SELECT COUNT(*) FROM downloads WHERE date(ts)=date('now')").fetchone()[0]
    cached = db.execute("SELECT COUNT(*) FROM downloads WHERE cached=1").fetchone()[0]
    await m.answer(
        f"📊 الإحصائيات\n"
        f"👥 المستخدمين: {users}\n"
        f"⬇️ إجمالي التحميلات: {total}\n"
        f"📅 تحميلات اليوم: {today}\n"
        f"⚡ من الكاش (فورية): {cached}"
    )


@dp.callback_query(F.data == "check_sub")
async def on_check_sub(cb: CallbackQuery):
    if await is_subscribed(cb.bot, cb.from_user.id):
        await cb.answer("تم التحقق ✅", show_alert=True)
        await cb.message.edit_text("تمام 👍 دحين أرسل رابط الفيديو اللي تبغاه.")
    else:
        await cb.answer("لسا ما انضممت للقناة 🙏", show_alert=True)


@dp.message(F.text.regexp(URL_RE))
async def on_link(m: Message):
    track_user(m.from_user.id)

    if not await is_subscribed(m.bot, m.from_user.id):
        await m.answer(
            "🔒 قبل ما نحمّل لك، انضم لقناتنا أول (مرة وحدة بس):",
            reply_markup=join_keyboard(),
        )
        return

    url = URL_RE.search(m.text).group(0)
    if not is_supported(url):
        await m.answer("هذا الرابط غير مدعوم حالياً 🙏")
        return

    wait = await m.answer("🔎 جاري فحص الرابط...")
    try:
        info = await asyncio.to_thread(extract_info_sync, url)
    except Exception as e:
        log.exception("info failed")
        await wait.edit_text(f"❌ {friendly_error(e)}")
        return

    if info["duration"] and info["duration"] > MAX_MINUTES * 60:
        await wait.edit_text(
            f"⏱ الفيديو مدته {fmt_duration(info['duration'])} وهو أطول من المسموح ({MAX_MINUTES} دقيقة)."
        )
        return

    token = uuid.uuid4().hex[:10]
    pending[token] = {"url": url, "key": info["key"], "title": info["title"]}
    if len(pending) > 500:
        pending.pop(next(iter(pending)))

    dur = f" ({fmt_duration(info['duration'])})" if info["duration"] else ""
    await wait.edit_text(
        f"🎞 {info['title']}{dur}\n\nاختر الجودة:",
        reply_markup=quality_keyboard(token),
    )


@dp.callback_query(F.data.startswith("dl:"))
async def on_choice(cb: CallbackQuery):
    _, mode, token = cb.data.split(":")
    data = pending.get(token)
    if not data:
        await cb.answer("انتهت صلاحية الطلب، أرسل الرابط مرة ثانية", show_alert=True)
        return

    if not await is_subscribed(cb.bot, cb.from_user.id):
        await cb.answer()
        await cb.message.edit_text(
            "🔒 قبل ما نحمّل لك، انضم لقناتنا أول (مرة وحدة بس):",
            reply_markup=join_keyboard(),
        )
        return

    await cb.answer()

    user_id = cb.from_user.id
    kind = "audio" if mode == "audio" else "video"
    cache_key = f"{data['key']}:{mode}"
    me = await cb.bot.me()
    caption = f"{data['title']}\n\n📥 @{me.username}"
    status = await cb.message.edit_text("⏳ جاري التحميل...")

    # 1) الكاش: إذا أحد طلب نفس الفيديو من قبل يوصل فوراً
    row = db.execute("SELECT file_id, kind FROM cache WHERE key=?", (cache_key,)).fetchone()
    if row:
        try:
            await send_media(cb.message, row[1], row[0], caption)
            track_download(user_id, cache_key, cached=True)
            await status.delete()
            return
        except Exception:
            log.warning("cached file_id failed, re-downloading")

    # 2) تحميل جديد
    workdir = tempfile.mkdtemp(prefix="vb_")
    try:
        async with sem:
            path = await asyncio.to_thread(download_sync, data["url"], mode, workdir)

        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > MAX_MB:
            await status.edit_text(
                f"الملف حجمه {size_mb:.0f}MB وهو أكبر من حد تليجرام ({MAX_MB}MB).\n"
                "جرّب جودة أقل 👇",
                reply_markup=quality_keyboard(token),
            )
            return

        await status.edit_text("📤 جاري الرفع...")
        sent = await send_media(cb.message, kind, FSInputFile(path), caption)
        file_id = sent.audio.file_id if kind == "audio" else sent.video.file_id
        db.execute("INSERT OR REPLACE INTO cache(key, file_id, kind) VALUES(?,?,?)", (cache_key, file_id, kind))
        db.commit()
        track_download(user_id, cache_key, cached=False)
        await status.delete()
    except Exception as e:
        log.exception("download failed")
        await status.edit_text(f"❌ {friendly_error(e)}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def health(request):
    return web.Response(text="ok")


async def run_health_server():
    """سيرفر ويب بسيط عشان Render يعتبر الخدمة (والبوت) شغّالة، ويستخدمه UptimeRobot لإبقائه صاحي."""
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"health server listening on {PORT}")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("ضع BOT_TOKEN في ملف .env")
    await run_health_server()
    session = AiohttpSession(timeout=600)
    bot = Bot(BOT_TOKEN, session=session)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
