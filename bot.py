"""
Ekko Bot — Phiên bản B (Tối ưu cho Gemini Free Tier)
- Sửa lỗi kết nối Gemini
- Dùng gọi async chuẩn (generate_content_async)
- Retry thông minh + backoff
- Giới hạn đồng thời (semaphore) để tránh rate-limit
- Dùng các model "flash" ưu tiên cho Free Tier
- Xử lý ảnh an toàn (chuyển sang bytes nếu cần)
- Tối ưu lưu trữ SQLite bằng executor (không block loop)
- Thông báo lỗi thân thiện cho người dùng

Hướng dẫn: dán nguyên file này thay thế file cũ (ví dụ bot.py). Cấu hình environment cần có:
- DISCORD_TOKEN
- GEMINI_API_KEY (nếu dùng Gemini)

"""

import os
import io
import time
import asyncio
import sqlite3
import datetime
import traceback
import logging
import sys
from typing import List, Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Optional: Gemini SDK
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except Exception:
    genai = None
    GEMINI_AVAILABLE = False

import PIL.Image

# ---------------------------
# Load .env
# ---------------------------
load_dotenv()

# ---------------------------
# Config
# ---------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TARGET_CHANNELS = os.getenv("TARGET_CHANNELS", "hoi-dap").split(",")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "2"))
DB_PATH = os.getenv("DB_PATH", "ekko_bot.sqlite")

# Limits suitable for Free Tier
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "512"))
API_CONCURRENCY = int(os.getenv("API_CONCURRENCY", "2"))  # how many simultaneous Gemini calls
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Personas
PERSONAS = {
    "tieu_thu_dong": {
        "name": "Tiểu Thư Đồng",
        "system": (
            """
Bạn là 'Tiểu Thư Đồng', NPC hướng dẫn game Where Winds Meet (Yến Vân Thập Lục Thanh).

QUY TẮC:
1. Xưng hô: Tại hạ / Đại hiệp.
2. Giọng điệu: Cổ trang, kiếm hiệp, ngắn gọn, súc tích.
3. Kiến thức game: Trong game Where Winds Meet, người chơi KHÔNG thể tặng quà cho NPC. Nếu được hỏi về việc tặng quà, hãy khẳng định là không có tính năng này.
            """
        )
    }
}
DEFAULT_PERSONA = "tieu_thu_dong"

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ekko")

# ---------------------------
# Setup Gemini
# ---------------------------
ai_enabled = False
_api_semaphore = asyncio.Semaphore(API_CONCURRENCY)

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        ai_enabled = True
        logger.info("✅ Gemini configured.")
    except Exception as e:
        logger.exception("❌ Gemini config error: %s", e)
        ai_enabled = False
else:
    logger.info("ℹ️ Gemini disabled (no key or SDK).")

# ---------------------------
# Discord Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------
# Memory & cooldown
# ---------------------------
_user_last_call = {}
_user_persona = {}

# ---------------------------
# Database helpers (non-blocking via executor)
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
    loop = asyncio.get_running_loop()
    def _exec():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()
    await loop.run_in_executor(None, _exec)

async def db_fetchall(query, params=()):
    loop = asyncio.get_running_loop()
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
# Utility
# ---------------------------

def is_on_cooldown(user_id):
    now = time.time()
    last = _user_last_call.get(user_id)
    if last and now - last < COOLDOWN_SECONDS:
        return True, COOLDOWN_SECONDS - (now - last)
    _user_last_call[user_id] = now
    return False, 0

async def image_to_bytes(pil_image: PIL.Image.Image) -> bytes:
    """Convert PIL image to PNG bytes (in-memory)."""
    bio = io.BytesIO()
    pil_image.save(bio, format="PNG")
    bio.seek(0)
    return bio.read()

# ---------------------------
# Gemini: improved function for Free Tier
# ---------------------------
async def gemini_send(user_message: str, system_message: str, images: Optional[List[PIL.Image.Image]] = None):
    """
    Calls Gemini in a robust way for Free Tier:
    - Limits concurrency with semaphore
    - Retries with exponential backoff
    - Uses flash models by preference
    - Returns an object with attribute `text`
    """
    if not ai_enabled:
        return type('obj', (object,), { 'text': "Chưa cấu hình API Key hoặc Key lỗi." })

    # Prefer flash models that are more likely allowed on free tier
    MODEL_TEXT_ONLY = [
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro"
    ]
    MODEL_VISION = [
        "gemini-2.0-flash",
        "gemini-1.5-pro"
    ]

    candidate_models = MODEL_VISION if images else MODEL_TEXT_ONLY

    # Build prompt
    system_part = system_message or ""
    user_part = user_message or ""
    # Keep prompt compact to save tokens
    full_prompt_text = f"HƯỚNG DẪN HỆ THỐNG: {system_part}\n\nNGƯỜI DÙNG: {user_part}"

    # Convert images to bytes if present
    image_blobs = []
    if images:
        for img in images:
            try:
                image_blobs.append(await image_to_bytes(img))
            except Exception:
                pass

    last_exception = None

    # Acquire semaphore to avoid too many simultaneous outbound calls
    async with _api_semaphore:
        for model_name in candidate_models:
            attempt = 0
            while attempt < MAX_RETRIES:
                attempt += 1
                try:
                    model = genai.GenerativeModel(model_name)

                    # Prepare request: SDKs differ; the following attempts to be generic
                    # Use generate_content_async with a compact payload
                    # Note: safety_settings reduces chance of being blocked
                    response = await model.generate_content_async(
                        [full_prompt_text],
                        safety_settings="BLOCK_ONLY_HIGH",
                        generation_config={
                            "max_output_tokens": MAX_OUTPUT_TOKENS,
                            "temperature": 0.6
                        }
                    )

                    # If response has text-like attribute
                    if hasattr(response, 'text') and response.text:
                        return response

                    # Some SDKs return list or structure
                    # Try to extract plausible text
                    # This keeps code defensive without raising
                    try:
                        # Try standard attributes
                        text = getattr(response, 'text', None)
                        if not text and hasattr(response, 'candidates'):
                            cand = response.candidates
                            if isinstance(cand, list) and len(cand) > 0:
                                text = getattr(cand[0], 'content', None) or getattr(cand[0], 'text', None)
                        if text:
                            return type('obj', (object,), { 'text': text })
                    except Exception:
                        pass

                    # If no usable text, raise to try next model
                    raise RuntimeError("Không nhận được văn bản từ model")

                except Exception as e:
                    last_exception = e
                    backoff = min(2 ** attempt, 8)
                    logger.warning("Model %s attempt %s failed: %s — backoff %ss", model_name, attempt, repr(e), backoff)
                    await asyncio.sleep(backoff)
                    continue

        # If we get here, all models/retries failed
        logger.error("TẤT CẢ MODEL ĐỀU LỖI. Lỗi cuối: %s", repr(last_exception))
        return type('obj', (object,), { 'text': "⚠️ Hệ thống AI đang bảo trì (Lỗi kết nối API). Vui lòng thử lại sau." })

# ---------------------------
# Discord commands & events
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

@bot.event
async def on_ready():
    try:
        await tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.exception("Sync error: %s", e)
    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if str(message.channel.name) not in TARGET_CHANNELS:
        return

    await bot.process_commands(message)

    lower = message.content.lower().strip() if message.content else ""
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
                    img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    image_list.append(img)
                except Exception:
                    pass

    if not user_text and not image_list:
        return

    persona_key = _user_persona.get(message.author.id, DEFAULT_PERSONA)
    system_msg = PERSONAS.get(persona_key, PERSONAS[DEFAULT_PERSONA])["system"]

    await save_chat(message.author.id, message.channel.id, "user", persona_key, user_text)

    async with message.channel.typing():
        try:
            if ai_enabled:
                result = await gemini_send(user_text, system_msg, image_list if image_list else None)
                reply_text = result.text if hasattr(result, "text") else str(result)
            else:
                reply_text = "Chưa cấu hình API Key hoặc Key lỗi."

            # Ensure reply length fits Discord limits
            if len(reply_text) > 2000:
                for i in range(0, len(reply_text), 1900):
                    sent = await message.channel.send(reply_text[i:i+1900])
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

            await save_chat(message.author.id, message.channel.id, "bot", persona_key, reply_text)
        except Exception as e:
            traceback.print_exc()
            await message.channel.send("⚠️ Lỗi không xác định. Xin hãy thử lại sau.")

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    msg = reaction.message
    if msg.author != bot.user or str(reaction.emoji) != "🗑️":
        return

    perm = msg.channel.permissions_for(user)
    if perm.manage_messages:
        await msg.delete()
        return

    # allow only users who recently chatted in this channel to delete
    rows = await db_fetchall("SELECT user_id FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 5", (msg.channel.id,))
    if user.id in [r[0] for r in rows]:
        await msg.delete()

if __name__ == "__main__":
    # Keep alive import optional
    try:
        from keep_alive import keep_alive
        keep_alive()
    except Exception:
        logger.info("keep_alive not found or failed.")

    if not DISCORD_TOKEN:
        logger.warning("WARNING: Missing DISCORD_TOKEN")

    # Start the bot in a way that works both in normal scripts and in environments
    # where an asyncio event loop is already running (e.g. Jupyter).
    try:
        bot.run(DISCORD_TOKEN)
    except RuntimeError as e:
        # Handle "asyncio.run() cannot be called from a running event loop"
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            logger.info("Event loop already running - starting bot using create_task on current loop.")
            loop = asyncio.get_event_loop()
            # Schedule bot.start as a task on the existing loop
            loop.create_task(bot.start(DISCORD_TOKEN))
        else:
            raise
