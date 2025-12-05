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
from dotenv import load_dotenv

# ---------------------------
# Load .env (nếu chạy local)
# ---------------------------
load_dotenv()

# ---------------------------
# keep_alive (Render ping)
# ---------------------------
try:
    from keep_alive import keep_alive
except ImportError:
    # Fallback nếu chưa tạo file keep_alive.py
    def keep_alive():
        print("Keep alive function not found.")

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
TARGET_CHANNELS = ["hoi-dap"]  # Tên kênh bot được phép chat

COOLDOWN_SECONDS = 2
DB_PATH = "ekko_bot.sqlite"

# Cấu hình Persona (Nhân vật)
PERSONAS = {
    "tieu_thu_dong": {
        "name": "Tiểu Thư Đồng",
        "system": (
            "Bạn là 'Tiểu Thư Đồng', NPC hướng dẫn game Where Winds Meet (Yến Vân Thập Lục Thanh).\n"
            "QUY TẮC:\n"
            "1. Xưng hô: Tại hạ / Đại hiệp.\n"
            "2. Giọng điệu: Cổ trang, kiếm hiệp, ngắn gọn, súc tích.\n"
            "3. Kiến thức game: Trong game Where Winds Meet, người chơi KHÔNG thể tặng quà cho NPC. Nếu được hỏi về việc tặng quà, hãy khẳng định là không có tính năng này."
        )
    }
}

DEFAULT_PERSONA = "tieu_thu_dong"

# ---------------------------
# Setup Gemini
# ---------------------------
ai_enabled = False
if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
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
# Memory Variables
# ---------------------------
_user_last_call = {}
_user_persona = {}

# ---------------------------
# Database Functions
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
# Gemini Logic (Đã sửa fix lỗi 404)
# ---------------------------
async def gemini_send(user_message, system_message, images=None):
    """
    Hàm gọi Gemini API.
    Đã sửa model_name thành 'gemini-1.5-flash-001' để tránh lỗi 404.
    """
    try:
        # Khởi tạo model với System Instruction (Persona) hiện tại
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash-001",  # <-- ĐÃ SỬA TÊN MODEL Ở ĐÂY
            system_instruction=system_message
        )

        contents = []

        # Thêm Text của User
        if user_message:
            contents.append(user_message)
        
        # Thêm Ảnh của User (nếu có)
        if images:
            for img in images:
                contents.append(img)

        # Gọi API (chạy trong executor để không chặn bot)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: model.generate_content(contents)
        )
    except Exception as e:
        # Nếu model flash-001 lỗi, thử fallback về gemini-pro (bản cũ nhưng ổn định)
        print(f"Lỗi gọi model flash-001: {e}")
        raise e

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
    # Chỉ hoạt động trong kênh chỉ định
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

    # Check Cooldown
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
                try:
                    img_bytes = await att.read()
                    img = PIL.Image.open(io.BytesIO(img_bytes))
                    image_list.append(img)
                except Exception:
                    pass

    if not user_text and not image_list:
        return

    # Xác định Persona
    persona_key = _user_persona.get(message.author.id, DEFAULT_PERSONA)
    system_message = PERSONAS[persona_key]["system"]

    # Lưu chat user vào DB
    await save_chat(message.author.id, message.channel.id, "user", persona_key, user_text)

    async with message.channel.typing():
        try:
            if ai_enabled:
                result = await gemini_send(
                    user_message=user_text,
                    system_message=system_message,
                    images=image_list
                )

                reply_text = result.text if hasattr(result, "text") else "..."
            else:
                reply_text = f"Tại hạ nhận được: {user_text or '[hình ảnh]'}\n(Chưa cấu hình GEMINI_API_KEY)"

            # Gửi tin nhắn (chia nhỏ nếu quá dài)
            if len(reply_text) > 2000:
                for i in range(0, len(reply_text), 1900):
                    sent = await message.channel.send(reply_text[i:i+1900])
                    await sent.add_reaction("🗑️")
            else:
                sent = await message.channel.send(reply_text)
                await sent.add_reaction("🗑️")

            # Lưu chat bot vào DB
            await save_chat(message.author.id, message.channel.id, "bot", persona_key, reply_text)

        except Exception as e:
            traceback.print_exc()
            await message.channel.send(f"⚠️ Có lỗi xảy ra khi gọi AI: {str(e)}")

# ---------------------------
# Reaction delete (Xóa tin nhắn bot)
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

        # Nếu user có quyền quản lý tin nhắn
        perm = msg.channel.permissions_for(user)
        if perm.manage_messages:
            await msg.delete()
            return

        # Nếu user là người vừa chat gần đây (kiểm tra DB)
        rows = await db_fetchall(
            "SELECT user_id FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 5",
            (msg.channel.id,)
        )
        recent_users = [r[0] for r in rows]
        if user.id in recent_users:
            await msg.delete()
            return

    except Exception:
        traceback.print_exc()

# ---------------------------
# START BOT
# ---------------------------
if __name__ == "__main__":
    keep_alive()
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN missing in Environment Variables")
    else:
        bot.run(DISCORD_TOKEN)
