# -*- coding: utf-8 -*-
# Ekko Bot — Bản hoàn chỉnh (Text-only, cải tiến theo yêu cầu)
# - Không đọc ảnh
# - Tăng độ cà khịa (Cửu Lưu Manh)
# - Tối ưu concurrency / tốc độ
# - Cải thiện prompt + nhớ lịch sử (memory)
# - Thêm slash commands: /help, /reset, /set-persona, /history

import os
import io
import time
import asyncio
import sqlite3
import datetime
import traceback
import logging
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Gemini SDK (text-only)
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except Exception:
    genai = None
    GEMINI_AVAILABLE = False

# ---------------------------
# Load .env
# ---------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TARGET_CHANNELS = os.getenv("TARGET_CHANNELS", "hoi-dap").split(",")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "2"))
DB_PATH = os.getenv("DB_PATH", "ekko_bot.sqlite")
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1024"))
API_CONCURRENCY = int(os.getenv("API_CONCURRENCY", "2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "6"))  # number of recent messages to include as context

# Model TEXT ONLY (Free-ish)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-1.5-flash")

# Persona: Cửu Lưu Manh (cà khịa mạnh)
PERSONA_NAME = "Cửu Lưu Manh"
PERSONA_SYSTEM = (
    "Bạn là Cửu Lưu Manh — lão giang hồ lém lỉnh, miệng lưỡi sắc bén, thích cà khịa nặng tay nhưng vẫn có "
    "lòng nhân nghĩa. Xưng hô theo phong cách kiếm hiệp: 'tại hạ', 'bằng hữu', 'đại hiệp'. "
    "CHỈ HỖ TRỢ Where Winds Meet. Nếu người dùng gửi ảnh, hãy trả lời: 'Tại hạ mù lòa không đọc ảnh'. "
    "KHI TRẢ LỜI: dùng giọng lém lỉnh, phong trần, cà khịa mạnh (không chửi tục), đưa hướng dẫn cụ thể, bước-by-step, ví dụ thực tế trong game."
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
if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        ai_enabled = True
        logger.info("✅ Gemini TEXT mode configured.")
    except Exception as e:
        ai_enabled = False
        logger.exception("❌ Gemini init error: %s", e)
else:
    logger.info("ℹ️ Gemini disabled or SDK missing.")

# ---------------------------
# Database (async wrappers)
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

async def db_execute(query: str, params: Tuple = ()):  # type: ignore
    loop = asyncio.get_running_loop()
    def _exec():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()
    await loop.run_in_executor(None, _exec)

async def db_fetchall(query: str, params: Tuple = ()):  # type: ignore
    loop = asyncio.get_running_loop()
    def _fetch():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return rows
    return await loop.run_in_executor(None, _fetch)

async def save_chat(uid: int, cid: int, role: str, persona: str, content: str):
    ts = datetime.datetime.utcnow().isoformat()
    await db_execute(
        "INSERT INTO chats (user_id, channel_id, role, persona, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, cid, role, persona, content, ts)
    )

async def fetch_recent_history(cid: int, limit: int = HISTORY_MESSAGES) -> List[Tuple]:
    rows = await db_fetchall(
        "SELECT role, persona, content, timestamp FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
        (cid, limit)
    )
    # rows returned newest first; reverse to chronological
    return list(reversed(rows))

# ---------------------------
# Cooldown (improved)
# ---------------------------
_user_last = {}

def is_on_cooldown(uid: int):
    now = time.time()
    last = _user_last.get(uid)
    if last and now - last < COOLDOWN_SECONDS:
        return True, COOLDOWN_SECONDS - (now - last)
    return False, 0

def set_cooldown(uid: int):
    _user_last[uid] = time.time()

# ---------------------------
# Prompt builder (uses recent history)
# ---------------------------
def build_prompt(system_text: str, history: List[Tuple], user_text: str) -> str:
    """Build a compact prompt including persona system, a few recent messages, and current user prompt."""
    parts = [system_text]
    if history:
        parts.append("-- Bối cảnh hội thoại gần đây --")
        for role, persona, content, ts in history:
            # role: 'user' or 'bot'
            label = 'Đại hiệp' if role == 'user' else (persona or 'Bot')
            parts.append(f"[{label}] {content}")
    parts.append("-- Yêu cầu hiện tại --")
    parts.append(user_text)
    # join with double newlines to keep tokens low but readable
    return "\n\n".join(parts)

# ---------------------------
# Gemini TEXT (with history)
# ---------------------------
async def gemini_text_with_history(system_text: str, user_text: str, channel_id: int):
    if not ai_enabled:
        return "Chưa cấu hình API key hoặc key lỗi."

    history = await fetch_recent_history(channel_id, HISTORY_MESSAGES)
    prompt = build_prompt(system_text, history, user_text)

    model = genai.GenerativeModel(GEMINI_MODEL)
    last_exc = None

    async with _api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await model.generate_content_async(
                    [prompt],
                    generation_config={"max_output_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.7},
                    safety_settings="BLOCK_ONLY_HIGH",
                )
                # Normalize response
                if hasattr(result, 'text') and result.text:
                    return result.text
                txt = getattr(result, 'text', None)
                if not txt and hasattr(result, 'candidates'):
                    cand = result.candidates
                    if isinstance(cand, list) and cand:
                        txt = getattr(cand[0], 'content', None) or getattr(cand[0], 'text', None)
                if txt:
                    return txt
                return str(result)
            except Exception as e:
                last_exc = e
                logger.warning("Gemini attempt %s failed: %s", attempt, repr(e))
                if attempt == MAX_RETRIES:
                    logger.error("Gemini all attempts failed: %s", repr(last_exc))
                    return "⚠️ Kết nối tới Gemini thất bại."
                await asyncio.sleep(min(1.5 * attempt, 8))

# ---------------------------
# Discord Bot + Slash commands
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# persona per-user (allow changing)
_user_persona = {}

@tree.command(name="help", description="Hướng dẫn dùng bot Ekko")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Tàng Kinh Các", description="Hướng dẫn sử dụng", color=0xA62019)
    embed.add_field(name="Hoạt động tại", value=", ".join(TARGET_CHANNELS), inline=False)
    embed.add_field(name="Lệnh", value="`/help`, `/reset`, `/set-persona`, `/history`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reset", description="Xóa lịch sử chat của bạn trong kênh này")
async def slash_reset(interaction: discord.Interaction):
    await db_execute("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (interaction.user.id, interaction.channel.id))
    _user_persona.pop(interaction.user.id, None)
    await interaction.response.send_message("🍶 Đã quên chuyện cũ.", ephemeral=True)

@tree.command(name="set-persona", description="Đổi nhân vật (ví dụ: Cửu Lưu Manh)")
@app_commands.describe(persona_key="Nhập key persona, mặc định Cửu Lưu Manh")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    # For now only support the built-in persona name; keep extensible
    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"Đã đổi persona sang: `{persona_key}`", ephemeral=True)

@tree.command(name="history", description="Hiển thị lịch sử chat gần nhất trong kênh")
async def slash_history(interaction: discord.Interaction):
    rows = await fetch_recent_history(interaction.channel.id, HISTORY_MESSAGES)
    if not rows:
        await interaction.response.send_message("Không có lịch sử.", ephemeral=True)
        return
    texts = []
    for role, persona, content, ts in rows:
        label = 'Bạn' if role == 'user' else (persona or 'Bot')
        texts.append(f"**{label}:** {content}")
    await interaction.response.send_message("\n".join(texts), ephemeral=True)

@bot.event
async def on_ready():
    try:
        await tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.exception("Sync error: %s", e)
    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    channel_name = getattr(message.channel, 'name', None)
    if channel_name not in TARGET_CHANNELS:
        return

    await bot.process_commands(message)

    # If attachments present, reply with 'blind' message
    if message.attachments:
        await message.channel.send("👀 Tại hạ mù lòa không đọc được ảnh nữa, gửi chữ đi bằng hữu!")
        return

    user_text = message.content.strip() if message.content else ""
    if not user_text:
        return

    # Cooldown check
    on_cd, remain = is_on_cooldown(message.author.id)
    if on_cd:
        await message.reply(f"⏳ Đợi {int(remain)+1}s đã bằng hữu.")
        return

    persona = _user_persona.get(message.author.id, PERSONA_NAME)

    # Save user message
    await save_chat(message.author.id, message.channel.id, 'user', persona, user_text)

    async with message.channel.typing():
        try:
            reply = await gemini_text_with_history(PERSONA_SYSTEM, user_text, message.channel.id)
            # Post-process: ensure Cửu Lưu Manh voice — quick heuristic
            if not reply.startswith('Tại hạ') and 'Cửu Lưu Manh' in PERSONA_SYSTEM:
                reply = f"Tại hạ nói: {reply}"
        except Exception as e:
            logger.exception("Reply error: %s", e)
            reply = "⚠️ Lỗi không xác định."

    # Send reply (split if too long)
    if len(reply) > 2000:
        for i in range(0, len(reply), 1800):
            part = reply[i:i+1800]
            sent = await message.channel.send(part)
            try: await sent.add_reaction('🗑️')
            except: pass
    else:
        sent = await message.channel.send(reply)
        try: await sent.add_reaction('🗑️')
        except: pass

    # Save bot reply and set cooldown
    await save_chat(message.author.id, message.channel.id, 'bot', persona, reply)
    set_cooldown(message.author.id)

# ---------------------------
# Run
# ---------------------------
if __name__ == '__main__':
    try:
        from keep_alive import keep_alive
        keep_alive()
    except Exception:
        logger.info('keep_alive not present')

    if not DISCORD_TOKEN:
        logger.warning('Missing DISCORD_TOKEN')

    try:
        bot.run(DISCORD_TOKEN)
    except RuntimeError as e:
        if 'asyncio.run() cannot be called from a running event loop' in str(e):
            logger.info('Event loop already running - scheduling bot.start')
            loop = asyncio.get_event_loop()
            loop.create_task(bot.start(DISCORD_TOKEN))
        else:
            raise
