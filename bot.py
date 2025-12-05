# bot.py
import os
import io
import time
import asyncio
import sqlite3
import datetime
import traceback

import discord
from discord.ext import commands
from discord import app_commands

# Optional: Gemini SDK. If not present or no key, bot uses fallback replies.
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False

import PIL.Image

# ---------------------------
# Configuration
# ---------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")           # 반드시 설정
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", None)   # optional
TARGET_CHANNELS = ["hoi-dap"]  # list of channel names where bot replies
COOLDOWN_SECONDS = 5           # default cooldown per user (seconds)
DB_PATH = "ekko_bot.sqlite"    # SQLite file

# Personas (you can add more)
PERSONAS = {
    "tieu_thu_dong": {
        "name": "Tiểu Thư Đồng",
        "system": (
            "Bạn là 'Tiểu Thư Đồng', NPC hướng dẫn game Where Winds Meet.\n"
            "QUY TẮC:\n1. Xưng hô: Tại hạ / Đại hiệp.\n2. Giọng điệu: Cổ trang, kiếm hiệp, ngắn gọn.\n3. Tuyệt đối KHÔNG hướng dẫn tặng quà NPC."
        )
    }
}

DEFAULT_PERSONA = "tieu_thu_dong"

# ---------------------------
# Setup Gemini (if available)
# ---------------------------
ai_enabled = False
if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        # quick smoke: create model object (no network-heavy call)
        model = genai.GenerativeModel(model_name="gemini-1.5-flash",
                                      system_instruction=PERSONAS[DEFAULT_PERSONA]["system"])
        ai_enabled = True
        print("✅ Gemini configured. AI enabled.")
    except Exception as e:
        print("❌ Gemini configuration failed:", repr(e))
        ai_enabled = False
else:
    print("ℹ️ Gemini not available or no API key — running with fallback replies.")

# ---------------------------
# Discord bot setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
# We'll use app_commands for slash commands
tree = bot.tree

# ---------------------------
# In-memory cooldown and persona mapping
# ---------------------------
_user_last_call = {}         # user_id -> timestamp
_user_persona = {}           # user_id -> persona_key
# default persona assigned on first use; you can change via slash later

# ---------------------------
# Database helpers (run in executor to avoid blocking)
# ---------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        role TEXT,            -- 'user' or 'bot'
        persona TEXT,
        content TEXT,
        timestamp TEXT
    )
    """)
    conn.commit()
    conn.close()

async def db_execute(query, params=()):
    loop = asyncio.get_event_loop()
    def _exec():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()
    await loop.run_in_executor(None, _exec)

async def db_fetchall(query, params=()):
    loop = asyncio.get_event_loop()
    def _fetch():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return rows
    rows = await loop.run_in_executor(None, _fetch)
    return rows

# Initialize DB at import/run
init_db()

# ---------------------------
# Utility: save chat to DB
# ---------------------------
async def save_chat(user_id, channel_id, role, persona, content):
    ts = datetime.datetime.utcnow().isoformat()
    await db_execute(
        "INSERT INTO chats (user_id, channel_id, role, persona, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, channel_id, role, persona, content, ts)
    )

# ---------------------------
# Utility: Gemini send in executor (since SDK is sync)
# ---------------------------
async def gemini_send(chat_session, content):
    # chat_session is whatever model.start_chat returned
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, chat_session.send_message, content)

# ---------------------------
# Cooldown check
# ---------------------------
def is_on_cooldown(user_id):
    now = time.time()
    last = _user_last_call.get(user_id)
    if last and now - last < COOLDOWN_SECONDS:
        return True, COOLDOWN_SECONDS - (now - last)
    _user_last_call[user_id] = now
    return False, 0

# ---------------------------
# Slash commands
# ---------------------------
@tree.command(name="help", description="Hiện trợ giúp cho bot Ekko")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Tàng Kinh Các - Hướng dẫn", description="Tiểu Thư Đồng kính chào!", color=0xA62019)
    embed.add_field(name="📍 Hoạt động", value=f"Tại: channels {', '.join(TARGET_CHANNELS)}", inline=False)
    embed.add_field(name="🛠️ Lệnh", value="`/help` — trợ giúp\n`/reset` — xóa lịch sử\n`/set-persona <persona_key>` — đổi persona (nếu có)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reset", description="Reset (quên) lịch sử trò chuyện của bạn")
async def slash_reset(interaction: discord.Interaction):
    user_id = interaction.user.id
    # delete chat rows for this user in this channel
    await db_execute("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (user_id, interaction.channel.id))
    _user_persona.pop(user_id, None)
    await interaction.response.send_message("🍶 *Tiểu Thư Đồng đã quên hết chuyện xưa.*", ephemeral=True)

@tree.command(name="set-persona", description="Đổi persona cho cuộc trò chuyện của bạn")
@app_commands.describe(persona_key="Nhập persona key (ví dụ tieu_thu_dong)")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    if persona_key not in PERSONAS:
        await interaction.response.send_message(f"⚠️ Persona `{persona_key}` không tồn tại. Keys: {', '.join(PERSONAS.keys())}", ephemeral=True)
        return
    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"✅ Persona được đổi thành `{persona_key}`.", ephemeral=True)

# ---------------------------
# Event: on_ready -> sync commands
# ---------------------------
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print(f"{bot.user} đã sẵn sàng. Slash commands synced.")
    except Exception as e:
        print("Warning: cannot sync slash commands:", repr(e))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------------------------
# Event: on_message -> primary handler
# ---------------------------
@bot.event
async def on_message(message):
    # ignore self
    if message.author == bot.user:
        return

    # only respond in target channels
    if str(message.channel.name) not in TARGET_CHANNELS:
        return

    # keep regular commands processing
    await bot.process_commands(message)

    # basic commands via text
    lower = message.content.strip().lower()
    if lower.startswith("!help") or lower.startswith("!huongdan"):
        # mimic slash help
        await message.channel.send("Gõ `/help` để xem hướng dẫn, hoặc dùng `/reset`, `/set-persona`.")
        return
    if lower.startswith("!reset"):
        await db_execute("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (message.author.id, message.channel.id))
        _user_persona.pop(message.author.id, None)
        await message.channel.send("🍶 *Đã quên hết chuyện cũ.*")
        return

    # cooldown
    on_cd, seconds_left = is_on_cooldown(message.author.id)
    if on_cd:
        await message.reply(f"⏳ Đại hiệp bình tĩnh — hãy chờ {int(seconds_left)+1}s trước khi gửi câu mới.")
        return

    # prepare content to send to AI (text + first image if exists)
    content_to_send = []
    if message.content:
        content_to_send.append(message.content)

    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                img_bytes = await att.read()
                try:
                    img = PIL.Image.open(io.BytesIO(img_bytes))
                    content_to_send.append(img)
                except Exception:
                    # ignore unreadable images
                    pass

    if not content_to_send:
        return

    # persona
    persona = _user_persona.get(message.author.id, DEFAULT_PERSONA)
    system_instruction = PERSONAS.get(persona, PERSONAS[DEFAULT_PERSONA])["system"]

    # ensure user chat session: we'll store a simple sentinel; with Gemini model.start_chat if available
    chat_session = None
    if ai_enabled:
        # If Gemini available, ensure chat_session object per user stored in DB? we store in-memory for session object
        # We'll keep a lightweight sessions dict on bot object
        if not hasattr(bot, "ai_sessions"):
            bot.ai_sessions = {}
        if message.author.id not in bot.ai_sessions:
            # create session with persona system instruction
            bot.ai_sessions[message.author.id] = model.start_chat(history=[])
            # Overwrite the system instruction if gemini model supports it per chat API; otherwise it's kept in model config
        chat_session = bot.ai_sessions[message.author.id]

    # save user message to DB
    await save_chat(message.author.id, message.channel.id, "user", persona, str(message.content))

    # indicate typing
    async with message.channel.typing():
        reply_text = None
        try:
            if ai_enabled and chat_session is not None:
                # attach or set system instruction if possible - we rely on the model created earlier
                # gemini_send runs in executor to avoid blocking
                response = await gemini_send(chat_session, content_to_send)
                # response may have .text
                if response and getattr(response, "text", None):
                    reply_text = response.text
                else:
                    reply_text = "Tiểu Thư Đồng im lặng như tờ..."
            else:
                # Fallback reply if no AI key: do a simple templated answer to keep UX pleasant
                # This is intentionally simple — replace with better local model if you deploy one.
                text_summary = content_to_send[0] if isinstance(content_to_send[0], str) else "[hình ảnh]"
                reply_text = f"Tại hạ nhận được: {text_summary}\n(Nếu muốn trả lời sâu hơn, gắn GEMINI_API_KEY vào biến môi trường.)"

            # send reply (split if long)
            if reply_text:
                if len(reply_text) > 2000:
                    for i in range(0, len(reply_text), 1900):
                        sent = await message.channel.send(reply_text[i:i+1900])
                        # add auto-delete reaction to each chunk
                        try:
                            await sent.add_reaction("🗑️")
                        except Exception:
                            pass
                else:
                    sent = await message.channel.send(reply_text)
                    try:
                        await sent.add_reaction("🗑️")
                    except Exception:
                        pass

                # save bot reply into DB
                await save_chat(message.author.id, message.channel.id, "bot", persona, reply_text)

        except Exception as e:
            # log and graceful fallback
            traceback.print_exc()
            await message.channel.send("⚠️ *Thiên cơ bất khả lộ (Sự cố xử lý).*")

# ---------------------------
# Reaction auto-delete: nếu ai react 🗑️ vào message của bot, xóa message
# ---------------------------
@bot.event
async def on_reaction_add(reaction, user):
    try:
        # ignore bot reactions
        if user.bot:
            return

        msg = reaction.message
        # only handle our bot's messages
        if msg.author != bot.user:
            return

        # only delete when icon is trash can
        if str(reaction.emoji) != "🗑️":
            return

        # allow deletion if the reactor is the original requester (we saved requester via DB but simplest: allow anyone with manage_messages)
        # We'll allow: the original user who started the thread (if present in DB for last chat) OR users with manage_messages
        # Determine if user has manage_messages in that channel
        perm = msg.channel.permissions_for(user)
        if perm.manage_messages:
            await msg.delete()
            return

        # If not moderator, allow delete only if this user was the last user for which message was generated
        # We'll check DB: find last 'user' chat in this channel for this user; and last 'bot' chat for same user
        rows = await db_fetchall("SELECT user_id, content, timestamp FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 50", (msg.channel.id,))
        # find most recent user_id that matches this reactor
        recent_user_ids = [r[0] for r in rows]
        # simplest heuristic: if reactor.user_id appears in recent entries, allow
        if user.id in recent_user_ids:
            # allow the user to delete the bot message
            await msg.delete()
            return

        # otherwise ignore
    except Exception:
        traceback.print_exc()

# ---------------------------
# Run the bot
# ---------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set. Exiting.")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
