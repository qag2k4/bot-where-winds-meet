# -*- coding: utf-8 -*-
"""
Ekko Bot — Bản mới hoàn chỉnh (Text-only)
Giữ nguyên các chức năng đã yêu cầu:
 - Persona: Cửu Lưu Manh (cà khịa, phong cách giang hồ)
 - Không đọc ảnh (nếu gửi ảnh, bot trả lời 'Tại hạ mù lòa...')
 - Lưu lịch sử hội thoại vào SQLite
 - Tối ưu concurrency + retry cho Gemini (text-only)
 - Cooldown chống spam
 - Slash commands: /help, /reset, /set-persona, /history

Phiên bản này sử dụng model hợp lệ cho API v1 (free tier text):
 - models/gemini-1.5-flash-latest

Ghi chú: đặt biến môi trường DISCORD_TOKEN và GEMINI_API_KEY trước khi chạy.
"""

import os
import time
import asyncio
import sqlite3
import datetime
import logging
from typing import Tuple, List

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Gemini SDK (google.generativeai)
try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except Exception:
    genai = None
    GENAI_AVAILABLE = False

# ---------------------------
# Load ENV
# ---------------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TARGET_CHANNELS = os.getenv("TARGET_CHANNELS", "hoi-dap").split(",")
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "2"))
DB_PATH = os.getenv("DB_PATH", "ekko_bot.sqlite")
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "6"))
MAX_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1024"))
CONCURRENCY = int(os.getenv("API_CONCURRENCY", "2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# MODEL (text-only, v1 compatible)
MODEL_NAME = os.getenv("GEMINI_MODEL", "models/gemini-1.5-flash-latest")

# Persona
PERSONA_NAME = "Cửu Lưu Manh"
PERSONA_SYSTEM = (
    "Bạn là Cửu Lưu Manh — lão giang hồ lém lỉnh, cà khịa mạnh nhưng nghĩa khí. "
    "Giọng điệu phong trần, xưng hô 'tại hạ', 'bằng hữu', 'đại hiệp'. "
    "Chỉ hỗ trợ Where Winds Meet. Nếu ai gửi ảnh: 'Tại hạ mù lòa không đọc ảnh'. "
    "Luôn trả lời rõ ràng, có ví dụ, hướng dẫn step-by-step."
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ekko")

# ---------------------------
# Gemini INIT
# ---------------------------
GEMINI_OK = False
_api_semaphore = asyncio.Semaphore(CONCURRENCY)
if GENAI_AVAILABLE and GEMINI_KEY:
    try:
        genai.configure(api_key=GEMINI_KEY)
        # instantiate model object lazily in calls; keeping config only
        GEMINI_OK = True
        logger.info("✅ Gemini configured (text-only). Model default: %s", MODEL_NAME)
    except Exception as e:
        logger.exception("Failed to configure Gemini: %s", e)
else:
    logger.info("Gemini SDK or key missing; running in offline mode.")

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

async def db_exec(query: str, params: Tuple = ()):  # type: ignore
    loop = asyncio.get_running_loop()
    def _run():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()
    await loop.run_in_executor(None, _run)

async def db_all(query: str, params: Tuple = ()):  # type: ignore
    loop = asyncio.get_running_loop()
    def _run():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return rows
    return await loop.run_in_executor(None, _run)

async def save_chat(uid: int, cid: int, role: str, persona: str, content: str):
    ts = datetime.datetime.utcnow().isoformat()
    await db_exec(
        "INSERT INTO chats (user_id, channel_id, role, persona, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, cid, role, persona, content, ts)
    )

async def fetch_history(cid: int, limit: int = HISTORY_MESSAGES) -> List[Tuple]:
    rows = await db_all(
        "SELECT role, persona, content FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
        (cid, limit)
    )
    return list(reversed(rows))

# ---------------------------
# Cooldown
# ---------------------------
_user_last: dict = {}

def is_on_cooldown(user_id: int):
    now = time.time()
    last = _user_last.get(user_id)
    if last and now - last < COOLDOWN_SECONDS:
        return True, COOLDOWN_SECONDS - (now - last)
    return False, 0

def set_cooldown(user_id: int):
    _user_last[user_id] = time.time()

# ---------------------------
# Prompt builder
# ---------------------------
def build_prompt(system_text: str, history: List[Tuple], user_text: str) -> str:
    parts: List[str] = [system_text]
    if history:
        parts.append("-- Hội thoại gần đây --")
        for role, persona, content in history:
            label = "Đại hiệp" if role == 'user' else (persona or 'Bot')
            parts.append(f"[{label}] {content}")
    parts.append("-- Yêu cầu hiện tại --")
    parts.append(user_text)
    return "\n\n".join(parts)

# ---------------------------
# Gemini text call with retries
# ---------------------------
async def gemini_text_reply(system_text: str, user_text: str, channel_id: int) -> str:
    if not GEMINI_OK:
        return "Chưa cấu hình API key hoặc key lỗi."

    history = await fetch_history(channel_id)
    prompt = build_prompt(system_text, history, user_text)

    last_exc = None
    async with _api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                model = genai.GenerativeModel(MODEL_NAME)
                response = await model.generate_content_async(
                    [prompt],
                    generation_config={
                        "max_output_tokens": MAX_TOKENS,
                        "temperature": 0.7,
                    },
                )
                # normalize text
                txt = getattr(response, 'text', None)
                if not txt and hasattr(response, 'candidates'):
                    cand = response.candidates
                    if isinstance(cand, list) and cand:
                        txt = getattr(cand[0], 'content', None) or getattr(cand[0], 'text', None)
                if txt:
                    return str(txt)
                return str(response)
            except Exception as e:
                last_exc = e
                logger.warning("Gemini attempt %s failed: %s", attempt, repr(e))
                if attempt == MAX_RETRIES:
                    logger.error("Gemini all attempts failed: %s", repr(last_exc))
                    return "⚠️ Kết nối Gemini thất bại."
                await asyncio.sleep(min(1.5 * attempt, 8))

# ---------------------------
# Discord Bot
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)
app_tree = bot.tree

# per-user persona override
_user_persona: dict = {}

@app_tree.command(name="help", description="Hướng dẫn dùng bot Ekko")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Tàng Kinh Các", description="Hướng dẫn sử dụng", color=0xA62019)
    embed.add_field(name="Hoạt động tại", value=", ".join(TARGET_CHANNELS), inline=False)
    embed.add_field(name="Lệnh", value="`/help`, `/reset`, `/set-persona`, `/history`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@app_tree.command(name="reset", description="Xóa lịch sử chat của bạn trong kênh này")
async def slash_reset(interaction: discord.Interaction):
    await db_exec("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (interaction.user.id, interaction.channel.id))
    _user_persona.pop(interaction.user.id, None)
    await interaction.response.send_message("🍶 Đã quên chuyện cũ.", ephemeral=True)

@app_tree.command(name="set-persona", description="Đổi nhân vật (ví dụ: Cửu Lưu Manh)")
@app_commands.describe(persona_key="Nhập key persona, mặc định Cửu Lưu Manh")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"Đã đổi sang: `{persona_key}`", ephemeral=True)

@app_tree.command(name="history", description="Hiển thị lịch sử chat gần nhất trong kênh")
async def slash_history(interaction: discord.Interaction):
    rows = await fetch_history(interaction.channel.id)
    if not rows:
        await interaction.response.send_message("Không có lịch sử.", ephemeral=True)
        return
    texts = []
    for role, persona, content in rows:
        label = 'Bạn' if role == 'user' else (persona or 'Bot')
        texts.append(f"**{label}:** {content}")
    await interaction.response.send_message("\n".join(texts), ephemeral=True)

@bot.event
async def on_ready():
    try:
        await app_tree.sync()
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

    # If attachments present, reply blind
    if message.attachments:
        await message.channel.send("👀 Tại hạ mù lòa không đọc được ảnh nữa, gửi chữ đi bằng hữu!")
        return

    user_text = message.content.strip() if message.content else ""
    if not user_text:
        return

    cd, remain = is_on_cooldown(message.author.id)
    if cd:
        await message.reply(f"⏳ Đợi {int(remain)+1}s đã bằng hữu.")
        return

    persona = _user_persona.get(message.author.id, PERSONA_NAME)
    await save_chat(message.author.id, message.channel.id, 'user', persona, user_text)

    async with message.channel.typing():
        try:
            reply = await gemini_text_reply(PERSONA_SYSTEM, user_text, message.channel.id)
            # Ensure persona voice
            if not reply.startswith('Tại hạ'):
                reply = f"Tại hạ nói: {reply}"
        except Exception as e:
            logger.exception("Reply error: %s", e)
            reply = "⚠️ Lỗi không xác định."

    # send reply, split if too long
    if len(reply) > 2000:
        for i in range(0, len(reply), 1800):
            part = reply[i:i+1800]
            sent = await message.channel.send(part)
            try:
                await sent.add_reaction('🗑️')
            except Exception:
                pass
    else:
        sent = await message.channel.send(reply)
        try:
            await sent.add_reaction('🗑️')
        except Exception:
            pass

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
