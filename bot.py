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

# ---------------------------
# keep_alive (Render ping)
# ---------------------------
from keep_alive import keep_alive

# Optional: Gemini SDK
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False

import PIL.Image

# ---------------------------
# Configuration
# ---------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", None)
TARGET_CHANNELS = ["hoi-dap"]

COOLDOWN_SECONDS = 2

DB_PATH = "ekko_bot.sqlite"

PERSONAS = {
    "tieu_thu_dong": {
        "name": "Tiểu Thư Đồng",
        "system": (
            "Bạn là 'Tiểu Thư Đồng', NPC hướng dẫn game Where Winds Meet.\n"
            "QUY TẮC:\n1. Xưng hô: Tại hạ / Đại hiệp.\n2. Giọng điệu: Cổ trang, kiếm hiệp, ngắn gọn.\n"
            "3. Tuyệt đối KHÔNG hướng dẫn tặng quà NPC."
        )
    }
}

DEFAULT_PERSONA = "tieu_thu_dong"

# ---------------------------
# Setup Gemini – API mới (0.4.0+)
# ---------------------------
ai_enabled = False
if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)

        # Không được dùng system_instruction trong GenerativeModel()
        model = genai.GenerativeModel("gemini-1.5-flash")

        ai_enabled = True
        print("✅ Gemini configured.")
    except Exception as e:
        print("❌ Gemini config error:", repr(e))
        ai_enabled = False
else:
    print("ℹ️ Gemini disabled (no key).")


# ---------------------------
# Discord setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------
# Cooldown + persona memory
# ---------------------------
_user_last_call = {}
_user_persona = {}

# ---------------------------
# Database
# ---------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_id INTEGER,
        role TEXT,
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
    return await loop.run_in_executor(None, _fetch)

init_db()

async def save_chat(user_id, channel_id, role, persona, content):
    ts = datetime.datetime.utcnow().isoformat()
    await db_execute(
        "INSERT INTO chats (user_id, channel_id, role, persona, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, channel_id, role, persona, content, ts)
    )

# ---------------------------
# Gemini send – API mới
# ---------------------------
async def gemini_send(chat_session, user_message, system_message, images=None):
    """
    API mới: phải gửi đúng format list messages.
    images: list PIL.Image
    """

    contents = [
        {"role": "system", "content": system_message},
    ]

    # User text
    if user_message:
        contents.append({"role": "user", "content": user_message})

    # User images
    if images:
        for img in images:
            contents.append({"role": "user", "content": img})

    # Gọi API blocking → chuyển vào executor
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: model.generate_content(contents)
    )

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
@tree.command(name="help", description="Hướng dẫn dùng bot Ekko")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 Tàng Kinh Các",
        description="Hướng dẫn sử dụng bot",
        color=0xA62019
    )
    embed.add_field(name="Hoạt động tại", value=", ".join(TARGET_CHANNELS), inline=False)
    embed.add_field(name="Lệnh", value="`/help`\n`/reset`\n`/set-persona`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reset", description="Reset lịch sử chat")
async def slash_reset(interaction: discord.Interaction):
    await db_execute(
        "DELETE FROM chats WHERE user_id = ? AND channel_id = ?",
        (interaction.user.id, interaction.channel.id)
    )
    _user_persona.pop(interaction.user.id, None)
    await interaction.response.send_message("🍶 Đã quên hết chuyện cũ.", ephemeral=True)

@tree.command(name="set-persona", description="Đổi persona")
@app_commands.describe(persona_key="Nhập key (VD: tieu_thu_dong)")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    if persona_key not in PERSONAS:
        await interaction.response.send_message(
            f"Persona `{persona_key}` không tồn tại.\nCó: {', '.join(PERSONAS.keys())}",
            ephemeral=True
        )
        return

    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"Đã đổi persona → `{persona_key}`", ephemeral=True)

# ---------------------------
# Ready event
# ---------------------------
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Sync error:", repr(e))
    print(f"Logged in as {bot.user}")

# ---------------------------
# Message handler
# ---------------------------
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if str(message.channel.name) not in TARGET_CHANNELS:
        return

    await bot.process_commands(message)

    lower = message.content.lower().strip()

    if lower.startswith("!help"):
        await message.channel.send("Dùng `/help` để xem hướng dẫn.")
        return

    if lower.startswith("!reset"):
        await db_execute(
            "DELETE FROM chats WHERE user_id = ? AND channel_id = ?",
            (message.author.id, message.channel.id)
        )
        _user_persona.pop(message.author.id, None)
        await message.channel.send("🍶 Đã quên chuyện cũ.")
        return

    on_cd, remain = is_on_cooldown(message.author.id)
    if on_cd:
        await message.reply(f"⏳ Chờ {int(remain)+1}s rồi nói tiếp.")
        return

    # Gom text + ảnh
    user_text = message.content if message.content else ""
    image_list = []

    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                img_bytes = await att.read()
                try:
                    img = PIL.Image.open(io.BytesIO(img_bytes))
                    image_list.append(img)
                except:
                    pass

    if not user_text and not image_list:
        return

    persona = _user_persona.get(message.author.id, DEFAULT_PERSONA)
    system_message = PERSONAS[persona]["system"]

    await save_chat(message.author.id, message.channel.id, "user", persona, user_text)

    async with message.channel.typing():
        try:
            if ai_enabled:
                result = await gemini_send(
                    chat_session=None,
                    user_message=user_text,
                    system_message=system_message,
                    images=image_list
                )

                reply_text = result.text if hasattr(result, "text") else "..."

            else:
                reply_text = f"Tại hạ nhận được: {user_text or '[hình ảnh]'}\n(Thêm GEMINI_API_KEY để trả lời sâu hơn.)"

            # Gửi tin dài
            if len(reply_text) > 2000:
                for i in range(0, len(reply_text), 1900):
                    sent = await message.channel.send(reply_text[i:i+1900])
                    await sent.add_reaction("🗑️")
            else:
                sent = await message.channel.send(reply_text)
                await sent.add_reaction("🗑️")

            await save_chat(message.author.id, message.channel.id, "bot", persona, reply_text)

        except Exception as e:
            traceback.print_exc()
            await message.channel.send("⚠️ Sự cố xử lý.")

# ---------------------------
# Reaction delete
# ---------------------------
@bot.event
async def on_reaction_add(reaction, user):
    try:
        if user.bot:
            return

        msg = reaction.message
        if msg.author != bot.user:
            return

        if str(reaction.emoji) != "🗑️":
            return

        perm = msg.channel.permissions_for(user)
        if perm.manage_messages:
            await msg.delete()
            return

        rows = await db_fetchall(
            "SELECT user_id FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 50",
            (msg.channel.id,)
        )
        recent = [r[0] for r in rows]
        if user.id in recent:
            await msg.delete()
            return
    except Exception:
        traceback.print_exc()

# ---------------------------
# START BOT + KEEP ALIVE
# ---------------------------
if __name__ == "__main__":
    keep_alive()
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN missing")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
