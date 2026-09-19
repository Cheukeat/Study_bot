import os
import asyncio
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from fakeredis import FakeAsyncRedis
import aiosqlite

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "study_room.db"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set.")

r = FakeAsyncRedis(decode_responses=True)

# --- Minimal HTTP Server for Render Health Checks ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Study Room Bot is alive!")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- Database Initialization ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            total_minutes INTEGER DEFAULT 0,
            current_streak INTEGER DEFAULT 0,
            last_study_date TEXT
        );
        """)
        await db.commit()

# --- Helper: Render Dashboard Card ---
def build_dashboard(subject: str, minutes_left: int, state: str = "Focus session"):
    text = (
        f"┌────────────────────────┐\n"
        f"│ **STUDY ROOM ASSISTANT**\n"
        f"│ **{subject}**\n"
        f"│\n"
        f"│       ⏳ **{minutes_left:02d}:00**\n"
        f"│     _{state}_\n"
        f"└────────────────────────┘\n\n"
        f"📅 **Quick Schedule**\n"
        f"• `08:00–10:00` — Self-study\n"
        f"• `14:00–16:00` — Outside class\n"
        f"• `Evening`     — Review\n"
    )
    return text

def build_keyboard(chat_id: int, is_paused: bool = False):
    play_pause_btn = (
        InlineKeyboardButton("▶️ Resume", callback_data=f"resume_{chat_id}")
        if is_paused
        else InlineKeyboardButton("⏸ Pause", callback_data=f"pause_{chat_id}")
    )
    keyboard = [
        [
            InlineKeyboardButton("▶️ Start", callback_data=f"start_{chat_id}"),
            play_pause_btn,
            InlineKeyboardButton("🔄 Reset", callback_data=f"reset_{chat_id}"),
        ],
        [
            InlineKeyboardButton("⏱ 25m", callback_data=f"set_25_{chat_id}"),
            InlineKeyboardButton("⏱ 50m", callback_data=f"set_50_{chat_id}"),
            InlineKeyboardButton("✋ Join", callback_data=f"join_{chat_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Handlers ---
async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Defaults
    subject = "Mathematics"
    duration = 25
    if context.args:
        subject = " ".join(context.args)

    await r.set(f"subject:{chat_id}", subject)
    await r.set(f"duration:{chat_id}", duration)
    await r.set(f"status:{chat_id}", "idle")

    text = build_dashboard(subject, duration, "Ready to start")
    reply_markup = build_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    await query.answer()

    subject = await r.get(f"subject:{chat_id}") or "Mathematics"
    duration = int(await r.get(f"duration:{chat_id}") or 25)

    if data.startswith("start_"):
        await r.set(f"status:{chat_id}", "running")
        text = build_dashboard(subject, duration, "Focus session active")
        await query.edit_message_text(text, reply_markup=build_keyboard(chat_id, is_paused=False), parse_mode="Markdown")

    elif data.startswith("pause_"):
        await r.set(f"status:{chat_id}", "paused")
        text = build_dashboard(subject, duration, "Session paused")
        await query.edit_message_text(text, reply_markup=build_keyboard(chat_id, is_paused=True), parse_mode="Markdown")

    elif data.startswith("resume_"):
        await r.set(f"status:{chat_id}", "running")
        text = build_dashboard(subject, duration, "Focus session active")
        await query.edit_message_text(text, reply_markup=build_keyboard(chat_id, is_paused=False), parse_mode="Markdown")

    elif data.startswith("reset_"):
        await r.set(f"status:{chat_id}", "idle")
        text = build_dashboard(subject, duration, "Session reset")
        await query.edit_message_text(text, reply_markup=build_keyboard(chat_id, is_paused=False), parse_mode="Markdown")

    elif data.startswith("set_25_"):
        await r.set(f"duration:{chat_id}", 25)
        text = build_dashboard(subject, 25, "Duration set to 25m")
        await query.edit_message_text(text, reply_markup=build_keyboard(chat_id), parse_mode="Markdown")

    elif data.startswith("set_50_"):
        await r.set(f"duration:{chat_id}", 50)
        text = build_dashboard(subject, 50, "Duration set to 50m")
        await query.edit_message_text(text, reply_markup=build_keyboard(chat_id), parse_mode="Markdown")

    elif data.startswith("join_"):
        user = update.effective_user
        await r.sadd(f"members:{chat_id}", user.first_name)
        members = await r.smembers(f"members:{chat_id}")
        await query.message.reply_text(f"✋ {user.first_name} joined! Total in room: {len(members)}")

# --- Main App ---
def main():
    asyncio.run(init_db())

    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("study", panel_command))
    app.add_handler(CallbackQueryHandler(button_router))

    print("Study Room Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
