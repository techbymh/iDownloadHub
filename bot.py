import os, re, glob, asyncio, tempfile, threading, shutil, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
import yt_dlp
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          MessageHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
LIMIT = 49 * 1024 * 1024  # Telegram bot limit ~50MB
URL_RE = re.compile(r"https?://\S+")
SEM = asyncio.Semaphore(2)  # max 2 downloads at the same time

# Message sent after the video and audio
FOOTER_TEXT = "Ai prompt, Movie Apps, Prem!um apps, Editing apps, all here 👉@techbymh👈"

# Line added at the end of every failure message
SUPPORT_LINE = "Ask for a Solution: @TechByMH"

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

def fetch(url, fmt, outdir):
    opts = {
        "format": fmt,
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
    }
    if os.path.exists("/etc/secrets/cookies.txt"):
        shutil.copy("/etc/secrets/cookies.txt", "/tmp/cookies.txt")
        opts["cookiefile"] = "/tmp/cookies.txt"
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    files = glob.glob(f"{outdir}/*")
    return (files[0] if files else None), info

def get_video(url, outdir):
    # Try the best quality first
    path, info = fetch(url, "bv*+ba/b", outdir)
    if path and os.path.getsize(path) > LIMIT:
        os.remove(path)
        # Too big: best quality that fits the limit
        path, info = fetch(url, "b[filesize<48M]/b[filesize_approx<48M]/worst", outdir)
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

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a YouTube, TikTok, Facebook or Instagram link and I'll send you the video.")

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("Please send a video link.")
        return
    msg = await update.message.reply_text("⏳ Downloading...")
    with tempfile.TemporaryDirectory() as d:
        try:
            async with SEM:
                path, info = await asyncio.to_thread(get_video, m.group(), d)
            if not path or os.path.getsize(path) > 50 * 1024 * 1024:
                await msg.edit_text(
                    "❌ The video is too large to send.\n" + SUPPORT_LINE)
                return

            # 1) Video (no caption)
            with open(path, "rb") as f:
                await update.message.reply_video(
                    f, supports_streaming=True,
                    read_timeout=300, write_timeout=300)

            # 2) Audio (separate message, named after the video)
            try:
                audio = await asyncio.to_thread(extract_audio, path, d)
                if audio and os.path.getsize(audio) <= 50 * 1024 * 1024:
                    title, performer = audio_meta(info)
                    safe = re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:60] or "audio"
                    fname = safe + os.path.splitext(audio)[1]
                    with open(audio, "rb") as a:
                        await update.message.reply_audio(
                            a, title=title, performer=performer or None,
                            filename=fname,
                            read_timeout=300, write_timeout=300)
            except Exception as e:
                print("AUDIO ERROR:", e, flush=True)

            # 3) Your message
            if FOOTER_TEXT:
                await update.message.reply_text(FOOTER_TEXT)

            await msg.delete()
        except Exception as e:
            print("ERROR:", e, flush=True)
            err = str(e)
            if "youtube" in err.lower() and any(
                    k in err for k in ("Failed to extract", "Sign in", "429", "403")):
                await msg.edit_text(
                    "⚠️ YouTube is temporarily unavailable. Please try again later.\n"
                    + SUPPORT_LINE)
            else:
                await msg.edit_text(
                    "❌ Download failed.\n" + err[:300] + "\n" + SUPPORT_LINE)

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
