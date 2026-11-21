import os, re, glob, asyncio, tempfile, threading, shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
import yt_dlp
from telegram import Update
from telegram.ext import (ApplicationBuilder, CommandHandler,
                          MessageHandler, filters, ContextTypes)

TOKEN = os.environ["BOT_TOKEN"]
LIMIT = 49 * 1024 * 1024  # Telegram bot limit ~50MB
URL_RE = re.compile(r"https?://\S+")

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
        y.download([url])
    files = glob.glob(f"{outdir}/*")
    return files[0] if files else None

def get_video(url, outdir):
    # Try the best quality first
    path = fetch(url, "bv*+ba/b", outdir)
    if path and os.path.getsize(path) > LIMIT:
        os.remove(path)
        # Too big: best quality that fits the limit
        path = fetch(url, "b[filesize<48M]/b[filesize_approx<48M]/worst", outdir)
    return path

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
            path = await asyncio.to_thread(get_video, m.group(), d)
            if not path or os.path.getsize(path) > 50 * 1024 * 1024:
                await msg.edit_text("❌ The video is too large to send.")
                return
            with open(path, "rb") as f:
                await update.message.reply_video(
                    f, supports_streaming=True,
                    read_timeout=300, write_timeout=300)
            await msg.delete()
        except Exception as e:
            print("ERROR:", e, flush=True)
            await msg.edit_text("❌ Download failed.\n" + str(e)[:300])

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
