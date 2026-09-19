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
    MessageHandler,
    ContextTypes,
    filters,
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

STUDY_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_other_messages=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_documents=False,
    can_send_audios=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_add_web_page_previews=False,
)

OPEN_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_other_messages=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_documents=True,
    can_send_audios=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_add_web_page_previews=True,
)

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

# --- Visual Component Builders ---

def make_progress_bar(current: int, total: int, bar_length: int = 10) -> str:
    if total <= 0:
        return "▰" * bar_length + " 100%"
    elapsed = max(0, total - current)
    filled_len = int(round(bar_length * elapsed / float(total)))
    filled = "▰" * filled_len
    unfilled = "▱" * (bar_length - filled_len)
    percent = int(round((elapsed / float(total)) * 100))
    return f"{filled}{unfilled} `{percent}%`"

def render_dashboard(subject: str, remaining: int, total: int, state: str, members: list) -> str:
    progress = make_progress_bar(remaining, total)
    
    if state == "running":
        badge = "🟢 FOCUS SPRINT IN PROGRESS"
    elif state == "paused":
        badge = "🟡 SESSION PAUSED"
    elif state == "done":
        badge = "🎉 SPRINT COMPLETE • BREAK TIME"
    else:
        badge = "⚪ STANDBY • READY TO LAUNCH"

    if members:
        squad_preview = " • ".join(members[:4])
        if len(members) > 4:
            squad_preview += f" +{len(members) - 4} more"
        squad_line = f"👥 **Squad ({len(members)}):** {squad_preview}"
    else:
        squad_line = "👥 **Squad:** _No one has joined yet_"

    return (
        f"╔══════════════════════════╗\n"
        f"       ⚡ **STUDY ROOM ASSISTANT** ⚡\n"
        f"╚══════════════════════════╝\n\n"
        f"🎯 **Target:** `{subject}`\n"
        f"📡 **Status:** {badge}\n\n"
        f"```text\n"
        f"┌─────────────────────────┐\n"
        f"│       ⏳ {remaining:02d}:00          │\n"
        f"└─────────────────────────┘\n"
        f"```\n"
        f"Progress: {progress}\n\n"
        f"{squad_line}\n"
        f"───────────────────────────\n"
        f"💡 _Tip: Reply with `/set 15` or send `Physics 30` to customize!_\n"
    )

def render_keyboard(chat_id: int, is_running: bool = False, is_paused: bool = False):
    if not is_running:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Launch Sprint", callback_data=f"start_{chat_id}")],
            [
                InlineKeyboardButton("📐 Math", callback_data=f"sub_Math_{chat_id}"),
                InlineKeyboardButton("💻 Code", callback_data=f"sub_Code_{chat_id}"),
                InlineKeyboardButton("📖 Read", callback_data=f"sub_Read_{chat_id}"),
            ],
            [
                InlineKeyboardButton("➕ +5m", callback_data=f"add_5_{chat_id}"),
                InlineKeyboardButton("➖ -5m", callback_data=f"sub_5_{chat_id}"),
                InlineKeyboardButton("➕ +1m", callback_data=f"add_1_{chat_id}"),
                InlineKeyboardButton("➖ -1m", callback_data=f"sub_1_{chat_id}"),
            ]
        ])

    control_btn = (
        InlineKeyboardButton("▶️ Resume", callback_data=f"resume_{chat_id}")
        if is_paused
        else InlineKeyboardButton("⏸ Pause", callback_data=f"pause_{chat_id}")
    )

    return InlineKeyboardMarkup([
        [
            control_btn,
            InlineKeyboardButton("🔄 Reset", callback_data=f"reset_{chat_id}"),
            InlineKeyboardButton("✋ Join Squad", callback_data=f"join_{chat_id}"),
        ],
        [
            InlineKeyboardButton("➕ +5m", callback_data=f"add_5_{chat_id}"),
            InlineKeyboardButton("➖ -5m", callback_data=f"sub_5_{chat_id}"),
            InlineKeyboardButton("➕ +1m", callback_data=f"add_1_{chat_id}"),
            InlineKeyboardButton("➖ -1m", callback_data=f"sub_1_{chat_id}"),
        ]
    ])

# --- Repeating Ticker ---

async def timer_tick_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    msg_id = job_data["msg_id"]

    status = await r.get(f"status:{chat_id}")
    if status != "running":
        return

    remaining = int(await r.get(f"remaining:{chat_id}") or 0)
    total = int(await r.get(f"total:{chat_id}") or 25)
    subject = await r.get(f"subject:{chat_id}") or "General Focus"
    members = list(await r.smembers(f"members:{chat_id}"))

    remaining -= 1

    if remaining <= 0:
        context.job.schedule_removal()
        await r.set(f"status:{chat_id}", "idle")
        await r.delete(f"remaining:{chat_id}")

        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
        except Exception:
            pass

        text = render_dashboard(subject, 0, total, "done", members)
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=render_keyboard(chat_id, is_running=False),
                parse_mode="Markdown"
            )
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔔 **Focus Complete!** Outstanding sprint on **{subject}**.\n☕ Permissions unlocked. Take a 5-minute break!"
        )
        return

    await r.set(f"remaining:{chat_id}", remaining)

    text = render_dashboard(subject, remaining, total, "running", members)
    try:
        await context.bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=render_keyboard(chat_id, is_running=True, is_paused=False),
            parse_mode="Markdown"
        )
    except Exception:
        pass

# --- Command & Message Handlers ---

async def study_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    subject = "Mathematics"
    duration = 25

    if context.args:
        if context.args[-1].isdigit():
            duration = int(context.args[-1])
            if len(context.args) > 1:
                subject = " ".join(context.args[:-1])
        else:
            subject = " ".join(context.args)

    await r.set(f"subject:{chat_id}", subject)
    await r.set(f"total:{chat_id}", duration)
    await r.set(f"remaining:{chat_id}", duration)
    await r.set(f"status:{chat_id}", "idle")
    await r.delete(f"members:{chat_id}")
    await r.sadd(f"members:{chat_id}", user.first_name)

    members = [user.first_name]
    text = render_dashboard(subject, duration, duration, "idle", members)
    reply_markup = render_keyboard(chat_id, is_running=False)
    msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    await r.set(f"msg_id:{chat_id}", msg.message_id)

async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows setting time and subject via `/set 15` or `/set Physics 30`."""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: `/set 15` or `/set Biology 45`", parse_mode="Markdown")
        return

    subject = await r.get(f"subject:{chat_id}") or "Mathematics"
    remaining = int(await r.get(f"remaining:{chat_id}") or 25)
    total = int(await r.get(f"total:{chat_id}") or 25)
    status = await r.get(f"status:{chat_id}") or "idle"
    members = list(await r.smembers(f"members:{chat_id}"))
    msg_id = await r.get(f"msg_id:{chat_id}")

    if context.args[-1].isdigit():
        new_duration = int(context.args[-1])
        new_duration = max(1, min(180, new_duration))
        remaining = new_duration
        total = new_duration
        if len(context.args) > 1:
            subject = " ".join(context.args[:-1])
    else:
        subject = " ".join(context.args)

    await r.set(f"subject:{chat_id}", subject)
    await r.set(f"remaining:{chat_id}", remaining)
    await r.set(f"total:{chat_id}", total)

    is_running = (status == "running")
    is_paused = (status == "paused")
    text = render_dashboard(subject, remaining, total, status, members)
    reply_markup = render_keyboard(chat_id, is_running=is_running, is_paused=is_paused)

    if msg_id:
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=int(msg_id),
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            await update.message.reply_text(f"✅ Updated dashboard to **{subject}** ({remaining} min)!", parse_mode="Markdown")
            return
        except Exception:
            pass

    # If previous card not found, post a fresh one
    new_msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    await r.set(f"msg_id:{chat_id}", new_msg.message_id)

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    user = update.effective_user
    await query.answer()

    subject = await r.get(f"subject:{chat_id}") or "Mathematics"
    remaining = int(await r.get(f"remaining:{chat_id}") or 25)
    total = int(await r.get(f"total:{chat_id}") or 25)
    status = await r.get(f"status:{chat_id}") or "idle"
    msg_id = int(await r.get(f"msg_id:{chat_id}") or query.message.message_id)
    members = list(await r.smembers(f"members:{chat_id}"))

    if data.startswith("start_"):
        await r.set(f"status:{chat_id}", "running")
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=STUDY_PERMISSIONS)
        except Exception:
            pass

        for job in context.job_queue.get_jobs_by_name(f"tick_{chat_id}"):
            job.schedule_removal()

        context.job_queue.run_repeating(
            timer_tick_job,
            interval=60,
            first=60,
            data={"chat_id": chat_id, "msg_id": msg_id},
            name=f"tick_{chat_id}"
        )

        text = render_dashboard(subject, remaining, total, "running", members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=True, is_paused=False), parse_mode="Markdown")

    elif data.startswith("pause_"):
        await r.set(f"status:{chat_id}", "paused")
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
        except Exception:
            pass

        text = render_dashboard(subject, remaining, total, "paused", members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=True, is_paused=True), parse_mode="Markdown")

    elif data.startswith("resume_"):
        await r.set(f"status:{chat_id}", "running")
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=STUDY_PERMISSIONS)
        except Exception:
            pass

        text = render_dashboard(subject, remaining, total, "running", members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=True, is_paused=False), parse_mode="Markdown")

    elif data.startswith("reset_"):
        for job in context.job_queue.get_jobs_by_name(f"tick_{chat_id}"):
            job.schedule_removal()

        await r.set(f"status:{chat_id}", "idle")
        await r.set(f"remaining:{chat_id}", total)
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
        except Exception:
            pass

        text = render_dashboard(subject, total, total, "idle", members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=False), parse_mode="Markdown")

    elif data.startswith("sub_"):
        new_sub = data.split("_")[1]
        await r.set(f"subject:{chat_id}", new_sub)
        text = render_dashboard(new_sub, remaining, total, status, members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=(status != "idle"), is_paused=(status == "paused")), parse_mode="Markdown")

    # Granular +/- buttons (allows stepping down below 5 to 4, 3, 2, 1)
    elif data.startswith("add_5_"):
        remaining = min(180, remaining + 5)
        total = max(total, remaining)
        await r.set(f"remaining:{chat_id}", remaining)
        await r.set(f"total:{chat_id}", total)
        text = render_dashboard(subject, remaining, total, status, members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=(status != "idle"), is_paused=(status == "paused")), parse_mode="Markdown")

    elif data.startswith("sub_5_"):
        if remaining > 5:
            remaining -= 5
        else:
            remaining = max(1, remaining - 1)  # Steps down by 1 if <= 5
        await r.set(f"remaining:{chat_id}", remaining)
        text = render_dashboard(subject, remaining, total, status, members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=(status != "idle"), is_paused=(status == "paused")), parse_mode="Markdown")

    elif data.startswith("add_1_"):
        remaining = min(180, remaining + 1)
        total = max(total, remaining)
        await r.set(f"remaining:{chat_id}", remaining)
        await r.set(f"total:{chat_id}", total)
        text = render_dashboard(subject, remaining, total, status, members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=(status != "idle"), is_paused=(status == "paused")), parse_mode="Markdown")

    elif data.startswith("sub_1_"):
        remaining = max(1, remaining - 1)  # Directly step down to 4, 3, 2, 1
        await r.set(f"remaining:{chat_id}", remaining)
        text = render_dashboard(subject, remaining, total, status, members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=(status != "idle"), is_paused=(status == "paused")), parse_mode="Markdown")

    elif data.startswith("join_"):
        await r.sadd(f"members:{chat_id}", user.first_name)
        members = list(await r.smembers(f"members:{chat_id}"))
        text = render_dashboard(subject, remaining, total, status, members)
        await query.edit_message_text(text, reply_markup=render_keyboard(chat_id, is_running=(status != "idle"), is_paused=(status == "paused")), parse_mode="Markdown")

# --- App Runner ---
def main():
    asyncio.run(init_db())

    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("study", study_command))
    app.add_handler(CommandHandler("set", set_command))
    app.add_handler(CallbackQueryHandler(button_router))

    print("Study Room Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
