import os
import asyncio
import datetime
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
    raise ValueError("Error: BOT_TOKEN not found. Make sure it is defined in your .env file.")

# In-memory Redis simulation
r = FakeAsyncRedis(decode_responses=True)

# Permission Profiles
STUDY_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_other_messages=False,  # Blocks stickers, GIFs, animations, games
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

# --- Bot Command Handlers ---

async def start_study(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    is_active = await r.get(f"active_session:{chat_id}")
    if is_active:
        await update.message.reply_text("⚠️ A study session is already active in this room!")
        return

    duration = 25
    if context.args:
        try:
            duration = int(context.args[0])
            if duration < 1 or duration > 120:
                await update.message.reply_text("Please choose a duration between 1 and 120 minutes.")
                return
        except ValueError:
            pass

    try:
        await context.bot.set_chat_permissions(chat_id=chat_id, permissions=STUDY_PERMISSIONS)
    except Exception as e:
        print(f"[Warn] Could not set chat permissions: {e}")

    await r.set(f"active_session:{chat_id}", duration, ex=(duration + 10) * 60)
    await r.sadd(f"members:{chat_id}", f"{user.id}:{user.first_name}")

    keyboard = [[InlineKeyboardButton("✋ Join Session", callback_data=f"join_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = await update.message.reply_text(
        f"⏳ **Study Session Started!**\n\n"
        f"• Duration: **{duration} minutes**\n"
        f"• Host: {user.first_name}\n"
        f"• *Media, stickers, and GIFs are locked for focus.*\n\n"
        f"Click below to join!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

    context.job_queue.run_once(
        finish_session,
        when=duration * 60,
        data={"chat_id": chat_id, "duration": duration, "msg_id": msg.message_id},
        name=f"study_{chat_id}"
    )

async def join_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    user = update.effective_user

    is_active = await r.get(f"active_session:{chat_id}")
    if not is_active:
        await query.edit_message_text("This session has already ended.")
        return

    await r.sadd(f"members:{chat_id}", f"{user.id}:{user.first_name}")
    members = await r.smembers(f"members:{chat_id}")
    member_names = [m.split(":", 1)[1] for m in members]

    duration = await r.get(f"active_session:{chat_id}")
    keyboard = [[InlineKeyboardButton("✋ Join Session", callback_data=f"join_{chat_id}")]]

    await query.edit_message_text(
        f"⏳ **Study Session in Progress!**\n\n"
        f"• Duration: **{duration} minutes**\n"
        f"• *Media locked for focus.*\n"
        f"• Active Participants ({len(member_names)}):\n  - " + "\n  - ".join(member_names),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def finish_session(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    duration = data["duration"]

    members = await r.smembers(f"members:{chat_id}")
    today = datetime.date.today().isoformat()
    mentions = []

    async with aiosqlite.connect(DB_PATH) as db:
        for member in members:
            user_id, name = member.split(":", 1)
            user_id = int(user_id)
            mentions.append(f"[{name}](tg://user?id={user_id})")

            cursor = await db.execute("SELECT current_streak, last_study_date FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()

            if not row:
                await db.execute(
                    "INSERT INTO users (user_id, username, total_minutes, current_streak, last_study_date) VALUES (?, ?, ?, ?, ?)",
                    (user_id, name, duration, 1, today)
                )
            else:
                streak, last_date = row
                if last_date != today:
                    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
                    streak = streak + 1 if last_date == yesterday else 1

                await db.execute(
                    "UPDATE users SET total_minutes = total_minutes + ?, current_streak = ?, last_study_date = ?, username = ? WHERE user_id = ?",
                    (duration, streak, today, name, user_id)
                )
        await db.commit()

    await r.delete(f"active_session:{chat_id}")
    await r.delete(f"members:{chat_id}")

    try:
        await context.bot.set_chat_permissions(chat_id=chat_id, permissions=OPEN_PERMISSIONS)
    except Exception as e:
        print(f"[Warn] Could not restore chat permissions: {e}")

    break_duration = 5  # minutes
    ping_list = ", ".join(mentions) if mentions else "Everyone"

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 **Focus Complete! Break Time!**\n\n"
             f"Great focus session, {ping_list}!\n"
             f"☕ Chat permissions unlocked. Take a **{break_duration}-minute break**.",
        parse_mode="Markdown"
    )

    context.job_queue.run_once(
        finish_break,
        when=break_duration * 60,
        data={"chat_id": chat_id},
        name=f"break_{chat_id}"
    )

async def finish_break(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await context.bot.send_message(
        chat_id=chat_id,
        text="☕ **Break is over!**\n\nReady for another round? Use `/study 25` to begin.",
        parse_mode="Markdown"
    )

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT username, total_minutes, current_streak FROM users ORDER BY total_minutes DESC LIMIT 5"
        )
        rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("No study records found. Start with `/study 25`!")
        return

    text = "🏆 **Study Room Leaderboard**\n\n"
    for rank, (name, minutes, streak) in enumerate(rows, 1):
        hours = round(minutes / 60, 1)
        text += f"{rank}. **{name}** — {hours} hrs | 🔥 {streak} day streak\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# --- App Runner ---
def main():
    asyncio.run(init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("study", start_study))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CallbackQueryHandler(join_session, pattern=r"^join_"))

    print("Study Room Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()~