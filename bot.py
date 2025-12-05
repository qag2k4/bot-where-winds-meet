# -*- coding: utf-8 -*-
# Ekko Bot — Phiên bản A (Hỗ trợ Gemini Vision thật sự)
# ---
# Bản hoàn chỉnh của bot Ekko, tối ưu cho Gemini Vision (nếu API key hỗ trợ Vision).
# Tính năng chính:
# - Xử lý ảnh upload từ Discord và gửi trực tiếp cho Gemini Vision
# - Persona: Cửu Lưu Manh (giang hồ, cà khịa mạnh, xưng hô kiếm hiệp)
# - Chỉ phân tích ảnh liên quan Where Winds Meet
# - Fallback: nếu không đọc được ảnh -> trả lời "bị mù"
# - Lưu lịch sử chat vào SQLite
# - Hạn chế concurrency để giảm rate-limit

import os
import io
import time
import asyncio
import sqlite3
import datetime
import traceback
import logging
from typing import List, Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Gemini SDK (optional)
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except Exception:
    genai = None
    GEMINI_AVAILABLE = False

from PIL import Image

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

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1024"))
API_CONCURRENCY = int(os.getenv("API_CONCURRENCY", "2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# ---------------------------
# Persona (Cửu Lưu Manh)
# ---------------------------
PERSONA_NAME = "Cửu Lưu Manh"
PERSONA_SYSTEM = (
    "Bạn là Cửu Lưu Manh — tên giang hồ lõi đời, miệng lưỡi sắc bén, cà khịa nặng tay nhưng tuyệt đối không thất lễ với bằng hữu. "
    "Phong thái kiếm hiệp, xưng hô theo giang hồ: 'tại hạ', 'bằng hữu', 'đại hiệp'. "
    "Chỉ phân tích Where Winds Meet — tuyệt đối KHÔNG đoán game khác. "
    "Khi người dùng gửi ảnh, phải lập tức phân tích: OCR chữ, dấu chỉ nhiệm vụ, bản đồ, UI, icon, vị trí, vật phẩm… "
    "Nếu ảnh liên quan đến nhiệm vụ, tự động suy luận nhiệm vụ, giải thích và hướng dẫn bước tiếp theo. "
    "Giọng lém lỉnh, phong trần, cà khịa mạnh nhưng luôn hỗ trợ chính xác."
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ekko")

# ---------------------------
# Gemini setup
# ---------------------------
ai_enabled = False
_api_semaphore = asyncio.Semaphore(API_CONCURRENCY)
MODEL_VISION = "gemini-1.5-pro"
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
# Database helpers
# ---------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_id INTEGER,
            role TEXT,
            persona TEXT,
            content TEXT,
            timestamp TEXT
        )
        """
    )
    conn.commit()
    conn.close()

init_db()

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

async def save_chat(user_id, channel_id, role, persona, content):
    ts = datetime.datetime.utcnow().isoformat()
    await db_execute(
        "INSERT INTO chats (user_id, channel_id, role, persona, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, channel_id, role, persona, content, ts)
    )

# ---------------------------
# Utils for images
# ---------------------------

def is_attachment_image(att: discord.Attachment) -> bool:
    try:
        if hasattr(att, 'content_type') and att.content_type and att.content_type.startswith("image/"):
            return True
    except Exception:
        pass
    name = getattr(att, 'filename', '') or getattr(att, 'name', '')
    return bool(name and name.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')))

async def attachment_to_pil(att: discord.Attachment) -> Optional[Image.Image]:
    try:
        data = await att.read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return img
    except Exception as e:
        logger.exception("Failed to read attachment %s: %s", getattr(att, 'filename', 'unknown'), e)
        return None

def summarize_image(pil_image: Image.Image) -> str:
    try:
        w, h = pil_image.size
        mode = pil_image.mode
        avg = pil_image.resize((1,1)).getpixel((0,0))
        if isinstance(avg, int):
            avg_str = str(avg)
        else:
            avg_str = ",".join(str(int(x)) for x in avg)
        return f"Kích thước: {w}x{h}; Mode: {mode}; Màu trung bình: {avg_str}"
    except Exception as e:
        logger.exception("Failed to summarize image: %s", e)
        return "(Không thể tóm tắt ảnh)"

# ---------------------------
# Cooldown
# ---------------------------
_user_last_call = {}

def is_on_cooldown(user_id):
    now = time.time()
    last = _user_last_call.get(user_id)
    if last and now - last < COOLDOWN_SECONDS:
        return True, COOLDOWN_SECONDS - (now - last)
    _user_last_call[user_id] = now
    return False, 0

# ---------------------------
# Gemini call (Vision) - FIXED FORMAT
# ---------------------------
async def gemini_send(user_text: str, system_text: str, images: Optional[List[Image.Image]] = None):
    if not ai_enabled:
        return type('obj', (object,), {'text': "Chưa cấu hình API Key hoặc Key lỗi."})

    # Build Gemini Vision payload as a list: text parts and binary image parts (no roles)
    parts = []
    if system_text:
        parts.append(system_text)
    if user_text:
        parts.append(user_text)
    if images:
        for img in images:
            bio = io.BytesIO()
            img.save(bio, format='PNG')
            bio.seek(0)
            parts.append({"mime_type": "image/png", "data": bio.read()})

    model = genai.GenerativeModel(MODEL_VISION)
    last_exc = None
    async with _api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await model.generate_content_async(
                    parts,
                    generation_config={"max_output_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.6},
                    safety_settings="BLOCK_ONLY_HIGH",
                )
                # Prefer result.text if present
                if hasattr(result, 'text') and result.text:
                    return result
                txt = getattr(result, 'text', None)
                if not txt and hasattr(result, 'candidates'):
                    cand = result.candidates
                    if isinstance(cand, list) and cand:
                        txt = getattr(cand[0], 'content', None) or getattr(cand[0], 'text', None)
                if txt:
                    return type('obj', (object,), {'text': txt})
                return result
            except Exception as e:
                last_exc = e
                logger.warning("Gemini attempt %s failed: %s", attempt, repr(e))
                if attempt == MAX_RETRIES:
                    logger.error("Gemini all attempts failed: %s", repr(last_exc))
                    return type('obj', (object,), {'text': "⚠️ Lỗi kết nối Gemini Vision. Vui lòng thử lại sau."})
                await asyncio.sleep(min(2 ** attempt, 8))

# ---------------------------
# Discord bot
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix='!', intents=intents)

_user_persona = {}

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.exception("Sync error: %s", e)
    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    # only process messages in target channels (by name)
    channel_name = getattr(message.channel, 'name', None)
    if channel_name not in TARGET_CHANNELS:
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
    images: List[Image.Image] = []
    if message.attachments:
        for att in message.attachments:
            if not is_attachment_image(att):
                continue
            img = await attachment_to_pil(att)
            if img:
                images.append(img)
                logger.info("Loaded image %s size=%sx%s", getattr(att, 'filename', 'unknown'), img.width, img.height)
            else:
                # Không đọc được ảnh
                await message.channel.send("👀 Tại hạ bị mù, nhìn không ra tấm ảnh này rồi bằng hữu à… thử gửi lại xem!")
                return

    if not user_text and not images:
        return

    persona_key = _user_persona.get(message.author.id, PERSONA_NAME)
    system_msg = PERSONA_SYSTEM

    await save_chat(message.author.id, message.channel.id, 'user', persona_key, user_text)

    async with message.channel.typing():
        try:
            if ai_enabled:
                result = await gemini_send(user_text, system_msg, images if images else None)
                reply_text = result.text if hasattr(result, 'text') else str(result)
            else:
                reply_text = "Chưa cấu hình API Key hoặc Key lỗi."

            # send reply (split if too long)
            if len(reply_text) > 2000:
                for i in range(0, len(reply_text), 1900):
                    part = reply_text[i:i+1900]
                    sent = await message.channel.send(part)
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

            await save_chat(message.author.id, message.channel.id, 'bot', persona_key, reply_text)
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

    rows = await db_fetchall("SELECT user_id FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 5", (msg.channel.id,))
    if user.id in [r[0] for r in rows]:
        await msg.delete()

# ---------------------------
# Run
# ---------------------------
if __name__ == '__main__':
    # keep_alive optional
    try:
        from keep_alive import keep_alive
        keep_alive()
    except Exception:
        logger.info("keep_alive not found or failed.")

    if not DISCORD_TOKEN:
        logger.warning("WARNING: Missing DISCORD_TOKEN")

    try:
        bot.run(DISCORD_TOKEN)
    except RuntimeError as e:
        # If event loop is already running (e.g., in REPL), schedule start on existing loop
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            logger.info("Event loop already running - scheduling bot.start on existing loop.")
            loop = asyncio.get_event_loop()
            loop.create_task(bot.start(DISCORD_TOKEN))
        else:
            raise
