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

# --- Minimal HTTP Server for Render Health Checks & UptimeRobot ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Study Room Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

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

# --- Visual UI & Banner Builders ---

def make_progress_bar(remaining_sec: int, total_sec: int, bar_length: int = 10) -> str:
    if total_sec <= 0:
        return "▰" * bar_length + " 100%"
    
    elapsed_sec = max(0, total_sec - remaining_sec)
    ratio = min(1.0, max(0.0, elapsed_sec / float(total_sec)))
    
    filled_len = int(round(bar_length * ratio))
    filled = "▰" * filled_len
    unfilled = "▱" * (bar_length - filled_len)
    percent = int(ratio * 100)
    
    return f"{filled}{unfilled} {percent}%"

def render_dashboard(subject: str, remaining_sec: int, total_sec: int, state: str, members: list) -> str:
    progress = make_progress_bar(remaining_sec, total_sec)
    
    mins = remaining_sec // 60
    secs = remaining_sec % 60
    time_str = f"{mins:02d}:{secs:02d}"

    if state == "running":
        badge = "🟢 SPRINT IN PROGRESS"
    elif state == "paused":
        badge = "🟡 SESSION PAUSED"
    elif state == "done":
        badge = "🎉 COMPLETE • BREAK TIME"
    else:
        badge = "⚪ STANDBY • READY TO START"

    if members:
        squad_preview = " • ".join(members[:4])
        if len(members) > 4:
            squad_preview += f" +{len(members) - 4} more"
        squad_line = f"👥 *Squad ({len(members)}):* {squad_preview}"
    else:
        squad_line = "👥 *Squad:* _No one has joined yet_"

    total_mins = total_sec // 60

    return (
        f"⚡ *STUDY ROOM ASSISTANT* ⚡\n\n"
        f"🎯 *Target:* `{subject}`\n"
        f"📡 *Status:* {badge}\n\n"
        f"```text\n"
        f"┌─────────────────────────┐\n"
        f"│        ⏳ {time_str}         │\n"
        f"└─────────────────────────┘\n"
        f"```\n"
        f"📊 *Progress:* `{progress}` `({time_str} / {total_mins:02d}:00)`\n\n"
        f"{squad_line}\n"
        f"───────────────────────────\n"
        f"💡 _Tip: Use /set 15 or click buttons to adjust!_\n"
    )

def render_completion_alert(subject: str, total_mins: int, members: list) -> str:
    mentions = " • ".join([f"*{m}*" for m in members]) if members else "Everyone"
    progress = make_progress_bar(0, total_mins * 60, bar_length=12)
    return (
        f"🏆 *MISSION COMPLETE // SPRINT CONCLUDED*\n\n"
        f"📊 *Progress:* `{progress}`\n\n"
        f"🎯 *Subject:* `{subject}`\n"
        f"⏱️ *Locked Focus:* `{total_mins} mins` logged\n"
        f"👥 *Squad:* {mentions}\n\n"
        f"🔓 *Chat permissions unlocked.*\n"
        f"☕ *Take a 5-minute breather before the next round.*"
    )
    
def render_break_over_alert(subject: str) -> str:
    return (
        f"⚡ *RECHARGE COMPLETE // READY FOR DEPLOYMENT*\n"
        f"```text\n"
        f"─── BREAK PROTOCOL TERMINATED ───\n"
        f"```\n"
        f"🧠 Time to dive back in. Last target was `{subject}`.\n"
        f"👉 Run `/study 25` to launch another sprint!"
    )

def render_cancel_alert(user_name: str, subject: str) -> str:
    return (
        f"🛑 *SESSION OVERRIDE // ABORTED*\n"
        f"```text\n"
        f"─── TIMER TERMINATED BY ADMIN ───\n"
        f"```\n"
        f"👤 *Operator:* {user_name}\n"
        f"🎯 *Target Cancelled:* `{subject}`\n"
        f"🔓 *Chat restrictions have been reset to normal.*"
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
                InlineKeyboardButton("➕ +5m", callback_data=f"add5_{chat_id}"),
                InlineKeyboardButton("➖ -5m", callback_data=f"sub5_{chat_id}"),
                InlineKeyboardButton("➕ +1m", callback_data=f"add1_{chat_id}"),
                InlineKeyboardButton("➖ -1m", callback_data=f"sub1_{chat_id}"),
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
            InlineKeyboardButton("✋ Join", callback_data=f"join_{chat_id}"),
        ],
        [
            InlineKeyboardButton("➕ +5m", callback_data=f"add5_{chat_id}"),
            InlineKeyboardButton("➖ -5m", callback_data=f"sub5_{chat_id}"),
            InlineKeyboardButton("➕ +1m", callback_data=f"add1_{chat_id}"),
            InlineKeyboardButton("➖ -1m", callback_data=f"sub1_{chat_id}"),
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

    remaining_sec = int(await r.get(f"remaining_sec:{chat_id}") or 0)
    total_sec = int(await r.get(f"total_sec:{chat_id}") or 1500)
    subject = await r.get(f"subject:{chat_id}") or "General Focus"
    members = list(await r.smembers(f"members:{chat_id}"))

    remaining_sec -= 60

    if remaining_sec <= 0:
        context.job.schedule_removal()
        await r.set(f"status:{chat_id}", "idle")
        await r.set(f"remaining_sec:{chat_id}", 0)

        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
        except Exception:
            pass

        text = render_dashboard(subject, 0, total_sec, "done", members)
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=render_keyboard(chat_id, is_running=False),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"[Warn] edit error: {e}")

        total_mins = total_sec // 60
        alert_text = render_completion_alert(subject, total_mins, members)
        
        break_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("☕ Start 5m Break", callback_data=f"break5_{chat_id}")],
            [InlineKeyboardButton("🚀 Next Sprint (25m)", callback_data=f"start_{chat_id}")]
        ])
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=alert_text,
            reply_markup=break_keyboard,
            parse_mode="Markdown"
        )
        return

    await r.set(f"remaining_sec:{chat_id}", remaining_sec)

    text = render_dashboard(subject, remaining_sec, total_sec, "running", members)
    try:
        await context.bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=msg_id,
            reply_markup=render_keyboard(chat_id, is_running=True, is_paused=False),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[Warn] tick edit error: {e}")

# --- Command Handlers ---

async def study_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    subject = "Mathematics"
    duration_min = 25

    if context.args:
        if context.args[-1].isdigit():
            duration_min = int(context.args[-1])
            if len(context.args) > 1:
                subject = " ".join(context.args[:-1])
        else:
            subject = " ".join(context.args)

    duration_min = max(1, min(180, duration_min))
    total_sec = duration_min * 60

    await r.set(f"subject:{chat_id}", subject)
    await r.set(f"total_sec:{chat_id}", total_sec)
    await r.set(f"remaining_sec:{chat_id}", total_sec)
    await r.set(f"status:{chat_id}", "idle")
    await r.delete(f"members:{chat_id}")
    await r.sadd(f"members:{chat_id}", user.first_name)

    members = [user.first_name]
    text = render_dashboard(subject, total_sec, total_sec, "idle", members)
    reply_markup = render_keyboard(chat_id, is_running=False)
    msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    await r.set(f"msg_id:{chat_id}", msg.message_id)

async def cancel_study(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if update.effective_chat.type in ["group", "supergroup"]:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status not in ["creator", "administrator"]:
            await update.message.reply_text("⚠️ Only group administrators can abort an active sprint.")
            return

    for job in context.job_queue.get_jobs_by_name(f"tick_{chat_id}"):
        job.schedule_removal()

    subject = await r.get(f"subject:{chat_id}") or "General Focus"
    await r.set(f"status:{chat_id}", "idle")

    try:
        await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
    except Exception:
        pass

    cancel_text = render_cancel_alert(user.first_name, subject)
    await update.message.reply_text(cancel_text, parse_mode="Markdown")

async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: `/set 15` or `/set Biology 45`", parse_mode="Markdown")
        return

    subject = await r.get(f"subject:{chat_id}") or "Mathematics"
    status = await r.get(f"status:{chat_id}") or "idle"
    members = list(await r.smembers(f"members:{chat_id}"))
    msg_id = await r.get(f"msg_id:{chat_id}")

    if context.args[-1].isdigit():
        new_min = max(1, min(180, int(context.args[-1])))
        total_sec = new_min * 60
        remaining_sec = total_sec
        if len(context.args) > 1:
            subject = " ".join(context.args[:-1])
    else:
        subject = " ".join(context.args)
        total_sec = int(await r.get(f"total_sec:{chat_id}") or 1500)
        remaining_sec = int(await r.get(f"remaining_sec:{chat_id}") or 1500)

    await r.set(f"subject:{chat_id}", subject)
    await r.set(f"total_sec:{chat_id}", total_sec)
    await r.set(f"remaining_sec:{chat_id}", remaining_sec)

    is_running = (status == "running")
    is_paused = (status == "paused")
    text = render_dashboard(subject, remaining_sec, total_sec, status, members)
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
            await update.message.reply_text(f"✅ Dashboard updated: *{subject}* ({total_sec // 60}m)", parse_mode="Markdown")
            return
        except Exception:
            pass

    new_msg = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    await r.set(f"msg_id:{chat_id}", new_msg.message_id)

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    user = update.effective_user

    subject = await r.get(f"subject:{chat_id}") or "Mathematics"
    remaining_sec = int(await r.get(f"remaining_sec:{chat_id}") or 1500)
    total_sec = int(await r.get(f"total_sec:{chat_id}") or 1500)
    status = await r.get(f"status:{chat_id}") or "idle"
    msg_id = int(await r.get(f"msg_id:{chat_id}") or query.message.message_id)
    members = list(await r.smembers(f"members:{chat_id}"))

    toast_msg = None

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
        status = "running"
        toast_msg = "🚀 Sprint launched!"

    elif data.startswith("pause_"):
        await r.set(f"status:{chat_id}", "paused")
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
        except Exception:
            pass
        status = "paused"
        toast_msg = "⏸ Paused"

    elif data.startswith("resume_"):
        await r.set(f"status:{chat_id}", "running")
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=STUDY_PERMISSIONS)
        except Exception:
            pass
        status = "running"
        toast_msg = "▶️ Resumed"

    elif data.startswith("reset_"):
        for job in context.job_queue.get_jobs_by_name(f"tick_{chat_id}"):
            job.schedule_removal()

        status = "idle"
        remaining_sec = total_sec
        await r.set(f"status:{chat_id}", "idle")
        await r.set(f"remaining_sec:{chat_id}", total_sec)
        try:
            await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
        except Exception:
            pass
        toast_msg = "🔄 Reset"

    elif data.startswith("sub_"):
        new_sub = data.split("_")[1]
        subject = new_sub
        await r.set(f"subject:{chat_id}", new_sub)
        toast_msg = f"Target: {new_sub}"

    elif data.startswith("add5_"):
        remaining_sec = min(180 * 60, remaining_sec + 300)
        if status == "idle":
            total_sec = remaining_sec
        else:
            total_sec = max(total_sec, remaining_sec)
        await r.set(f"remaining_sec:{chat_id}", remaining_sec)
        await r.set(f"total_sec:{chat_id}", total_sec)
        toast_msg = f"⏱️ +5m ({remaining_sec // 60}m)"

    elif data.startswith("sub5_"):
        if remaining_sec > 300:
            remaining_sec -= 300
        else:
            remaining_sec = max(60, remaining_sec - 60)
        
        if status == "idle":
            total_sec = remaining_sec
        await r.set(f"remaining_sec:{chat_id}", remaining_sec)
        await r.set(f"total_sec:{chat_id}", total_sec)
        toast_msg = f"⏱️ -5m ({remaining_sec // 60}m)"

    elif data.startswith("add1_"):
        remaining_sec = min(180 * 60, remaining_sec + 60)
        if status == "idle":
            total_sec = remaining_sec
        else:
            total_sec = max(total_sec, remaining_sec)
        await r.set(f"remaining_sec:{chat_id}", remaining_sec)
        await r.set(f"total_sec:{chat_id}", total_sec)
        toast_msg = f"⏱️ +1m ({remaining_sec // 60}m)"

    elif data.startswith("sub1_"):
        remaining_sec = max(60, remaining_sec - 60)
        if status == "idle":
            total_sec = remaining_sec
        await r.set(f"remaining_sec:{chat_id}", remaining_sec)
        await r.set(f"total_sec:{chat_id}", total_sec)
        toast_msg = f"⏱️ -1m ({remaining_sec // 60}m)"

    elif data.startswith("join_"):
        await r.sadd(f"members:{chat_id}", user.first_name)
        members = list(await r.smembers(f"members:{chat_id}"))
        toast_msg = f"✋ {user.first_name} joined!"

    if toast_msg:
        await query.answer(toast_msg)
    else:
        await query.answer()

    is_running = (status == "running")
    is_paused = (status == "paused")
    text = render_dashboard(subject, remaining_sec, total_sec, status, members)
    reply_markup = render_keyboard(chat_id, is_running=is_running, is_paused=is_paused)

    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        print(f"[Error editing message]: {e}")

# --- App Runner ---
def main():
    asyncio.run(init_db())

    server_thread = threading.Thread(target=run_health_server, daemon=True)
    server_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("study", study_command))
    app.add_handler(CommandHandler("cancel", cancel_study))
    app.add_handler(CommandHandler("set", set_command))
    app.add_handler(CallbackQueryHandler(button_router))

    print("Study Room Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
