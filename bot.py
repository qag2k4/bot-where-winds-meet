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
# Load .env
# ---------------------------
load_dotenv()

# ---------------------------
# Keep Alive
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
# Cấu hình
# ---------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", None)
TARGET_CHANNELS = ["hoi-dap"]

COOLDOWN_SECONDS = 2
DB_PATH = "ekko_bot.sqlite"

# Cấu hình Nhân vật
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
else:
    print("ℹ️ Gemini disabled (no key).")

# ---------------------------
# Discord Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------
# Memory
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
# Gemini Logic (SUPER SAFE VERSION)
# ---------------------------
async def gemini_send(user_message, system_message, images=None):
    """
    Hàm này sẽ thử danh sách các model từ mới đến cũ.
    Cái nào chạy được thì dùng, lỗi thì bỏ qua thử cái tiếp theo.
    """
    
    # 1. Ghép nội dung (Prompt Engineering) - Cách này an toàn nhất cho mọi model
    full_prompt = []
    if user_message:
        combined_text = f"HƯỚNG DẪN HỆ THỐNG: {system_message}\n\nNGƯỜI DÙNG HỎI: {user_message}"
        full_prompt.append(combined_text)
    
    if images:
        for img in images:
            full_prompt.append(img)

    # 2. Danh sách model để thử (Ưu tiên Flash vì nhanh, sau đó đến Pro)
    candidate_models = [
        "gemini-1.5-flash",
        "gemini-1.5-flash-001",
        "gemini-1.5-flash-latest",
        "gemini-1.5-pro",
        "gemini-pro",     # Bản ổn định nhất
        "gemini-1.0-pro"
    ]
    
    # Nếu có ảnh, chỉ thử các model hỗ trợ Vision
    if images:
        candidate_models = [
            "gemini-1.5-flash", 
            "gemini-1.5-flash-001", 
            "gemini-1.5-pro"
        ]

    loop = asyncio.get_event_loop()
    last_error = None

    # 3. Vòng lặp thử từng model
    for model_name in candidate_models:
        try:
            # print(f"Đang thử model: {model_name}...") # Debug
            model = genai.GenerativeModel(model_name)
            
            # Gọi API
            response = await loop.run_in_executor(
                None, 
                lambda: model.generate_content(full_prompt)
            )
            
            # Nếu chạy đến đây là thành công, trả về luôn
            return response

        except Exception as e:
            # Nếu lỗi, lưu lại và thử cái tiếp theo
            # print(f"Model {model_name} bị lỗi: {e}")
            last_error = e
            continue
    
    # 4. Nếu thử hết danh sách mà vẫn lỗi
    print(f"❌ TẤT CẢ MODEL ĐỀU LỖI. Lỗi cuối cùng: {last_error}")
    # Trả về object giả để không crash bot
    return type('obj', (object,), {'text': f"⚠️ Hệ thống AI đang bảo trì (Lỗi kết nối API). Vui lòng thử lại sau."})

# ---------------------------
# Cooldown
# ---------------------------
def is_on_cooldown(user_id):
    now = time.time()
    last = _user_last_call.get(user_id)
    if last and now - last < COOLDOWN_SECONDS:
        return True, COOLDOWN_SECONDS - (now - last)
    _user_last_call[user_id] = now
    return False, 0

# ---------------------------
# Slash Commands
# ---------------------------
@tree.command(name="help", description="Hướng dẫn dùng bot Ekko")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Tàng Kinh Các", description="Hướng dẫn sử dụng", color=0xA62019)
    embed.add_field(name="Hoạt động tại", value=", ".join(TARGET_CHANNELS), inline=False)
    embed.add_field(name="Lệnh", value="`/help`, `/reset`, `/set-persona`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reset", description="Xóa lịch sử chat")
async def slash_reset(interaction: discord.Interaction):
    await db_execute("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (interaction.user.id, interaction.channel.id))
    _user_persona.pop(interaction.user.id, None)
    await interaction.response.send_message("🍶 Đã quên chuyện cũ.", ephemeral=True)

@tree.command(name="set-persona", description="Đổi nhân vật")
@app_commands.describe(persona_key="Nhập key (VD: tieu_thu_dong)")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    if persona_key not in PERSONAS:
        await interaction.response.send_message(f"Không tìm thấy persona này.", ephemeral=True)
        return
    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"Đã đổi sang: `{persona_key}`", ephemeral=True)

# ---------------------------
# Events
# ---------------------------
@bot.event
async def on_ready():
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Sync error:", repr(e))
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    if str(message.channel.name) not in TARGET_CHANNELS: return

    await bot.process_commands(message)

    lower = message.content.lower().strip()
    if lower.startswith("!help"):
        await message.channel.send("Dùng `/help` để xem hướng dẫn.")
        return
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
                reply_text = "Chưa cấu hình API Key hoặc Key lỗi."

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
            await message.channel.send("⚠️ Lỗi không xác định.")

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

if __name__ == "__main__":
    keep_alive()
    if not DISCORD_TOKEN:
        print("WARNING: Missing DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)
