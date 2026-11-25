import os, re, glob, asyncio, tempfile, threading, shutil, subprocess
import datetime, zoneinfo
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import yt_dlp
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          MessageHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
LIMIT = 49 * 1024 * 1024  # Telegram bot limit ~50MB
URL_RE = re.compile(r"https?://\S+")
SEM = asyncio.Semaphore(2)  # max 2 downloads at the same time
DOWNLOAD_TIMEOUT = 600  # seconds, a download longer than this is stopped
COOKIE_SRC = "/etc/secrets/cookies.txt"

# Timezone for the daily cleanup (12:00 AM)
TZ = zoneinfo.ZoneInfo("Asia/Dhaka")

# Sent videos/audios waiting to be deleted at 12:00 AM: (chat_id, message_id)
PENDING = []

SITE_URL = "https://idownloadweb.onrender.com/"

# Only these platforms are supported
SUPPORTED_HOSTS = ("youtube.com", "youtu.be", "facebook.com", "fb.watch",
                   "instagram.com", "instagr.am", "tiktok.com")

# Message for any other website
UNSUPPORTED_TEXT = (
    "❌ Supported Platforms: YouTube, Facebook, Instagram, and TikTok only.\n\n"
    "Download videos or audio in your preferred quality from here:\n\n"
    "🔗 " + SITE_URL)

# Caption on every video
VIDEO_CAPTION = ("You can download it in whatever quality you prefer "
                 "using this website: " + SITE_URL)

# Message sent after the video and audio
FOOTER_TEXT = "Ai prompt, Movie Apps, Prem!um apps, Editing apps, all here 👉@techbymh👈"

# Lines added at the end of every failure message
SUPPORT_LINE = "Ask for a Solution: @TechByMH"
ERROR_LINE = "You can download it from: " + SITE_URL

def is_supported(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS)

def fail_text(text):
    return text + "\n" + SUPPORT_LINE + "\n" + ERROR_LINE

def short_error(err):
    """Turn a long technical error into one short, friendly line."""
    e = err.lower()
    if "live stream" in e or "is live" in e:
        return "❌ Live streams are not supported."
    if "timeout" in e:
        return "❌ Download took too long. Please try again later."
    if "youtube" in e and any(k in err for k in ("Failed to extract", "Sign in", "429", "403")):
        return "⚠️ YouTube is temporarily unavailable. Please try again later."
    if any(k in e for k in ("no video found", "log in", "login", "cookies",
                            "private", "not available", "unavailable",
                            "members-only", "sign in")):
        return "❌ Download failed. This video may be private or require login."
    if "unsupported url" in e:
        return "❌ Download failed. This link is not supported."
    return "❌ Download failed. Please try again later."

class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass

def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()

def fetch(url, fmt, outdir, use_cookies=False):
    opts = {
        "format": fmt,
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "socket_timeout": 30,
        # never record live streams (they never end)
        "match_filter": yt_dlp.utils.match_filter_func("!is_live"),
    }
    if use_cookies and os.path.exists(COOKIE_SRC):
        shutil.copy(COOKIE_SRC, "/tmp/cookies.txt")
        opts["cookiefile"] = "/tmp/cookies.txt"
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    if info and (info.get("is_live")
                 or info.get("live_status") in ("is_live", "is_upcoming")):
        raise ValueError("live stream is not supported")
    files = glob.glob(f"{outdir}/*")
    return (files[0] if files else None), info

def clean_dir(outdir):
    for f in glob.glob(f"{outdir}/*"):
        try:
            os.remove(f)
        except Exception:
            pass

def fetch_auto(url, fmt, outdir):
    """Try without cookies first. If it fails, retry with cookies.
    Returns (path, info, used_cookies)."""
    try:
        path, info = fetch(url, fmt, outdir, use_cookies=False)
        if path:
            print("FETCH OK: without cookies", flush=True)
            return path, info, False
        raise Exception("no file downloaded")
    except Exception as e:
        # No cookie file, or a live stream: do not retry
        if not os.path.exists(COOKIE_SRC) or "live stream" in str(e).lower():
            raise
        print("RETRY WITH COOKIES. First error:", repr(e)[:300], flush=True)
        clean_dir(outdir)
        path, info = fetch(url, fmt, outdir, use_cookies=True)
        print("FETCH OK: with cookies", flush=True)
        return path, info, True

def get_video(url, outdir):
    # Try the best quality first
    path, info, used_cookies = fetch_auto(url, "bv*+ba/b", outdir)
    if path and os.path.getsize(path) > LIMIT:
        os.remove(path)
        # Too big: best quality that fits the limit (same cookie mode)
        path, info = fetch(url, "b[filesize<48M]/b[filesize_approx<48M]/worst",
                           outdir, use_cookies=used_cookies)
    return path, info

def extract_audio(video_path, outdir):
    """Extract audio from the video. Returns file path or None."""
    out_m4a = os.path.join(outdir, "audio.m4a")
    out_mp3 = os.path.join(outdir, "audio.mp3")
    # Fast: copy the audio stream as is (works for AAC audio)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "copy", out_m4a],
        capture_output=True)
    if r.returncode == 0 and os.path.exists(out_m4a) and os.path.getsize(out_m4a) > 1024:
        return out_m4a
    # Fallback: convert to mp3
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "libmp3lame", "-q:a", "4", out_mp3],
        capture_output=True)
    if r.returncode == 0 and os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1024:
        return out_mp3
    return None

def audio_meta(info):
    """Audio title and performer from the video info."""
    info = info or {}
    title = (info.get("track") or info.get("title") or "Audio").strip()
    performer = (info.get("artist") or info.get("uploader")
                 or info.get("channel") or "").strip()
    title = title.replace("\n", " ")[:100]
    performer = performer.replace("\n", " ")[:64]
    return title, performer

async def midnight_cleanup(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs every day at 12:00 AM and deletes all sent videos/audios."""
    items = PENDING[:]
    PENDING.clear()
    for chat_id, message_id in items:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            print("DELETE ERROR:", e, flush=True)

def schedule_delete(ctx, sent):
    PENDING.append((sent.chat_id, sent.message_id))

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a YouTube, TikTok, Facebook or Instagram link and I'll send you the video.")

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("Please send a video link.")
        return
    url = m.group()

    # Any other website: send the "supported platforms" message
    if not is_supported(url):
        await update.message.reply_text(UNSUPPORTED_TEXT)
        return

    msg = await update.message.reply_text("⏳ Downloading...")
    with tempfile.TemporaryDirectory() as d:
        try:
            async with SEM:
                path, info = await asyncio.wait_for(
                    asyncio.to_thread(get_video, url, d),
                    timeout=DOWNLOAD_TIMEOUT)
            if not path:
                await msg.edit_text(
                    fail_text("❌ Download failed. Please try again later."))
                return
            if os.path.getsize(path) > 50 * 1024 * 1024:
                await msg.edit_text(
                    fail_text("❌ The video is too large to send."))
                return

            # 1) Video (with caption, deleted at 12:00 AM)
            with open(path, "rb") as f:
                sent_video = await update.message.reply_video(
                    f, caption=VIDEO_CAPTION, supports_streaming=True,
                    read_timeout=300, write_timeout=300)
            schedule_delete(ctx, sent_video)

            # 2) Audio (separate message, named after the video)
            try:
                audio = await asyncio.to_thread(extract_audio, path, d)
                if audio and os.path.getsize(audio) <= 50 * 1024 * 1024:
                    title, performer = audio_meta(info)
                    safe = re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:60] or "audio"
                    fname = safe + os.path.splitext(audio)[1]
                    with open(audio, "rb") as a:
                        sent_audio = await update.message.reply_audio(
                            a, title=title, performer=performer or None,
                            filename=fname,
                            read_timeout=300, write_timeout=300)
                    schedule_delete(ctx, sent_audio)
            except Exception as e:
                print("AUDIO ERROR:", e, flush=True)

            # 3) Your message (stays, not deleted)
            if FOOTER_TEXT:
                await update.message.reply_text(FOOTER_TEXT)

            await msg.delete()
        except Exception as e:
            print("ERROR:", repr(e), flush=True)  # full error stays in the logs only
            await msg.edit_text(fail_text(short_error(str(e) or type(e).__name__)))

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    # Every day at 12:00 AM (Asia/Dhaka): delete all sent videos/audios
    app.job_queue.run_daily(
        midnight_cleanup,
        time=datetime.time(0, 0, tzinfo=TZ))
    app.run_polling()
