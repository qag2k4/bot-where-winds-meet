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
# Load .env (Để chạy local hoặc load biến môi trường an toàn)
# ---------------------------
load_dotenv()

# ---------------------------
# keep_alive (Render ping)
# ---------------------------
try:
    from keep_alive import keep_alive
except ImportError:
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
TARGET_CHANNELS = ["hoi-dap"]

COOLDOWN_SECONDS = 2
DB_PATH = "ekko_bot.sqlite"

# Cấu hình Persona
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
# Gemini Logic (Đã sửa chữa)
# ---------------------------
async def gemini_send(user_message, system_message, images=None):
    """
    Sử dụng model 'gemini-pro' cho Text (ổn định nhất) và 'gemini-1.5-flash' cho Ảnh.
    Ghép system_message trực tiếp vào prompt để tránh lỗi API.
    """
    
    # 1. Chuẩn bị nội dung gửi (Prompt ghép)
    full_prompt = []
    
    # Ghép tính cách vào trước câu hỏi
    if user_message:
        combined_text = f"[HƯỚNG DẪN ẨN]: {system_message}\n\n[NGƯỜI DÙNG HỎI]: {user_message}"
        full_prompt.append(combined_text)
    
    # Thêm ảnh nếu có
    if images:
        for img in images:
            full_prompt.append(img)
            
    # 2. Chọn Model phù hợp
    # Nếu có ảnh -> Bắt buộc dùng Flash (Pro text không xem được ảnh)
    # Nếu chỉ có chữ -> Dùng Pro (để tránh lỗi 404 của Flash)
    if images:
        target_model_name = "gemini-1.5-flash"
    else:
        target_model_name = "gemini-pro"
    
    # 3. Gọi API
    try:
        model = genai.GenerativeModel(target_model_name)
        loop = asyncio.get_event_loop()
        
        # Gọi hàm generate_content
        return await loop.run_in_executor(
            None, 
            lambda: model.generate_content(full_prompt)
        )

    except Exception as e:
        print(f"Lỗi gọi model {target_model_name}: {e}")
        # Nếu model chính lỗi, trả về object giả để bot không crash
        return type('obj', (object,), {'text': f"⚠️ Hệ thống AI ({target_model_name}) đang bận hoặc lỗi. Vui lòng thử lại sau."})

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
    embed = discord.Embed(title="📜 Tàng Kinh Các", description="Hướng dẫn sử dụng bot", color=0xA62019)
    embed.add_field(name="Kênh hoạt động", value=", ".join(TARGET_CHANNELS), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reset", description="Reset lịch sử chat")
async def slash_reset(interaction: discord.Interaction):
    await db_execute("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (interaction.user.id, interaction.channel.id))
    _user_persona.pop(interaction.user.id, None)
    await interaction.response.send_message("🍶 Đã quên chuyện cũ.", ephemeral=True)

@tree.command(name="set-persona", description="Đổi persona")
@app_commands.describe(persona_key="Nhập key (VD: tieu_thu_dong)")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    if persona_key not in PERSONAS:
        await interaction.response.send_message(f"Không có persona này.", ephemeral=True)
        return
    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"Đã đổi sang: `{persona_key}`", ephemeral=True)

# ---------------------------
# Ready event
# ---------------------------
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print("Slash commands synced.")
        
        # --- DEBUG: In danh sách model có sẵn ---
        if ai_enabled:
            print("\n--- Available Models ---")
            try:
                for m in genai.list_models():
                    if 'generateContent' in m.supported_generation_methods:
                        print(f"- {m.name}")
            except Exception:
                pass
            print("------------------------\n")
            
    except Exception as e:
        print("Sync error:", repr(e))
    print(f"Logged in as {bot.user}")

# ---------------------------
# Message handler
# ---------------------------
@bot.event
async def on_message(message):
    if message.author == bot.user: return
    if str(message.channel.name) not in TARGET_CHANNELS: return

    await bot.process_commands(message)
    lower = message.content.lower().strip()

    if lower.startswith("!reset"):
        await db_execute("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (message.author.id, message.channel.id))
        _user_persona.pop(message.author.id, None)
        await message.channel.send("🍶 Đã quên chuyện cũ.")
        return

    on_cd, remain = is_on_cooldown(message.author.id)
    if on_cd:
        await message.reply(f"⏳ Chờ {int(remain)+1}s.")
        return

    user_text = message.content if message.content else ""
    image_list = []
    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                try:
                    img_bytes = await att.read()
                    img = PIL.Image.open(io.BytesIO(img_bytes))
                    image_list.append(img)
                except: pass

    if not user_text and not image_list: return

    persona_key = _user_persona.get(message.author.id, DEFAULT_PERSONA)
    system_msg = PERSONAS[persona_key]["system"]

    await save_chat(message.author.id, message.channel.id, "user", persona_key, user_text)

    async with message.channel.typing():
        try:
            if ai_enabled:
                result = await gemini_send(user_text, system_msg, image_list)
                reply_text = result.text if hasattr(result, "text") else str(result)
            else:
                reply_text = "Chưa cấu hình API Key."

            # Xử lý tin nhắn dài
            if len(reply_text) > 2000:
                for i in range(0, len(reply_text), 1900):
                    sent = await message.channel.send(reply_text[i:i+1900])
                    await sent.add_reaction("🗑️")
            else:
                sent = await message.channel.send(reply_text)
                await sent.add_reaction("🗑️")
            
            await save_chat(message.author.id, message.channel.id, "bot", persona_key, reply_text)
        except Exception as e:
            traceback.print_exc()
            await message.channel.send("⚠️ Lỗi xử lý.")

# ---------------------------
# Reaction delete
# ---------------------------
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot: return
    msg = reaction.message
    if msg.author != bot.user or str(reaction.emoji) != "🗑️": return
    
    perm = msg.channel.permissions_for(user)
    if perm.manage_messages:
        await msg.delete()
        return

    rows = await db_fetchall("SELECT user_id FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 5", (msg.channel.id,))
    if user.id in [r[0] for r in rows]:
        await msg.delete()

# ---------------------------
# START BOT
# ---------------------------
if __name__ == "__main__":
    keep_alive()
    if not DISCORD_TOKEN:
        print("WARNING: DISCORD_TOKEN is missing!")
    bot.run(DISCORD_TOKEN)
