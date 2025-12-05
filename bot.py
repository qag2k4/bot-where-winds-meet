# -*- coding: utf-8 -*-
"""
Ekko Bot — Bản ổn định 100% (Nâng cấp Gemini + ổn định I/O + tối ưu parsing)
Phiên bản này giữ nguyên cấu trúc tổng thể, sửa tất cả lỗi cú pháp, bổ sung cơ chế retry/backoff, circuit-breaker, và câu trả lời phong cách kiếm hiệp khi API quá tải.
"""

import os
import time
import asyncio
import sqlite3
import datetime
import logging
import random
from typing import List, Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Gemini SDK (chuẩn mới)
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
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash-latest")

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
logger = logging.getLogger("ekko")

# ---------------------------
# Gemini INIT + Circuit Breaker
# ---------------------------
GEMINI_OK = False
G_MODEL = None
_api_semaphore = asyncio.Semaphore(CONCURRENCY)

# circuit breaker state
_circuit_open = False
_circuit_open_until = 0
_circuit_failures = 0
CIRCUIT_FAIL_THRESHOLD = 5
CIRCUIT_OPEN_SECONDS = 30

if GENAI_AVAILABLE and GEMINI_KEY:
    try:
        genai.configure(api_key=GEMINI_KEY)
        # create model lazily
        G_MODEL = genai.GenerativeModel(MODEL_NAME)
        GEMINI_OK = True
        logger.info("Gemini configured: %s", MODEL_NAME)
    except Exception as e:
        logger.exception("Gemini configure failed: %s", e)
        GEMINI_OK = False
else:
    logger.info("Gemini disabled (SDK missing or API key not set)")

# ---------------------------
# DB helpers
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

async def db_exec(query: str, params=()):
    loop = asyncio.get_running_loop()
    def _run():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        conn.close()
    await loop.run_in_executor(None, _run)

async def db_all(query: str, params=()):
    loop = asyncio.get_running_loop()
    def _run():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return rows
    return await loop.run_in_executor(None, _run)

async def save_chat(user_id: int, channel_id: int, role: str, persona: str, content: str):
    ts = datetime.datetime.utcnow().isoformat()
    await db_exec(
        "INSERT INTO chats (user_id, channel_id, role, persona, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, channel_id, role, persona, content, ts)
    )

async def fetch_history(channel_id: int, limit=HISTORY_MESSAGES):
    rows = await db_all(
        "SELECT role, persona, content FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
        (channel_id, limit)
    )
    return list(reversed(rows))

# ---------------------------
# Cooldown
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
# Kiếm hiệp error messages (random)
# ---------------------------
KIEM_HIEP_ERRORS = [
    "🍶 Ối chà! Gió độc quẩn quanh khiến API nghẽn mạch. Để tại hạ điều tức một chút.",
    "🍶 Máy chủ đang ngồi thiền nhập định, bằng hữu đợi chốc lát.",
    "🍶 Đường truyền loạn như chợ phiên, để tại hạ gom lại chân khí.",
    "🍶 Trời nổi phong ba, server rung như thuyền nan. Để tại hạ giữ thăng bằng rồi nói tiếp!",
]
KIEM_HIEP_ERRORS_HARD = [
    "🍶 Chà… chân nguyên tán loạn! Hệ thống ngã quay như cá chép. Đợi tại hạ dựng dậy.",
    "🍶 Có cao thủ đánh lén vào máy chủ! Để tại hạ trấn áp rồi hồi âm.",
    "🍶 Tâm pháp đứt gẫy — phải liệu cơm gắp mắm một chút, chờ tại hạ đã.",
]

# ---------------------------
# Prompt builder
# ---------------------------
def build_prompt(system_text: str, history: List, user_text: str) -> str:
    parts = [system_text]
    if history:
        parts.append("-- Hội thoại gần đây --")
        for role, persona, content in history:
            label = "Đại hiệp" if role == "user" else (persona or "Bot")
            parts.append(f"[{label}] {content}")
    parts.append("-- Yêu cầu hiện tại --")
    parts.append(user_text)
    return "\n".join(parts)

# ---------------------------
# Gemini caller với retry/backoff + circuit-breaker
# ---------------------------
async def gemini_text_reply(system_text: str, user_text: str, channel_id: int) -> str:
    global _circuit_open, _circuit_open_until, _circuit_failures
    # circuit open check
    if _circuit_open and time.time() < _circuit_open_until:
        return random.choice(KIEM_HIEP_ERRORS)
    elif _circuit_open and time.time() >= _circuit_open_until:
        # try half-open
        _circuit_open = False
        _circuit_failures = 0

    if not GEMINI_OK:
        return random.choice(KIEM_HIEP_ERRORS)

    history = await fetch_history(channel_id)
    prompt = build_prompt(system_text, history, user_text)

    last_exc = None
    async with _api_semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info("Gemini attempt %s (attempt %d)", MODEL_NAME, attempt)

                response = await G_MODEL.generate_content_async(
                    contents=[{"role": "user", "parts": [prompt]}],
                    generation_config={"max_output_tokens": MAX_TOKENS, "temperature": 0.7}
                )

                # parse
                text = None
                if hasattr(response, 'text') and response.text:
                    text = response.text
                elif hasattr(response, 'candidates') and response.candidates:
                    cand = response.candidates[0]
                    text = getattr(cand, 'content', None) or getattr(cand, 'text', None)

                if text:
                    # success — reset failure counter
                    _circuit_failures = 0
                    return str(text).strip()

                last_exc = Exception('Empty response')
                logger.warning('Gemini returned empty response')

            except Exception as e:
                last_exc = e
                _circuit_failures += 1
                logger.warning('Gemini fail %s (attempt %d): %s', MODEL_NAME, attempt, repr(e))

                # if failures reach threshold, open circuit
                if _circuit_failures >= CIRCUIT_FAIL_THRESHOLD:
                    _circuit_open = True
                    _circuit_open_until = time.time() + CIRCUIT_OPEN_SECONDS
                    logger.error('Circuit opened for %s seconds', CIRCUIT_OPEN_SECONDS)
                    return random.choice(KIEM_HIEP_ERRORS_HARD)

                if attempt == MAX_RETRIES:
                    logger.error('Gemini final fail: %s', repr(last_exc))
                    return random.choice(KIEM_HIEP_ERRORS)

                # exponential backoff with jitter
                backoff = min(1.0 * (2 ** (attempt - 1)), 10)
                jitter = random.uniform(0, 0.5)
                await asyncio.sleep(backoff + jitter)

    return random.choice(KIEM_HIEP_ERRORS)

# ---------------------------
# Discord Bot
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)
app_tree = bot.tree
_user_persona = {}

@app_tree.command(name="help", description="Hướng dẫn dùng bot Ekko")
async def slash_help(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Tàng Kinh Các", description="Hướng dẫn sử dụng", color=0xA62019)
    embed.add_field(name="Hoạt động tại", value=", ".join(TARGET_CHANNELS), inline=False)
    embed.add_field(name="Lệnh", value="`/help`, `/reset`, `/set-persona`, `/history`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@app_tree.command(name="reset", description="Xóa lịch sử chat")
async def slash_reset(interaction: discord.Interaction):
    await db_exec("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (interaction.user.id, interaction.channel.id))
    _user_persona.pop(interaction.user.id, None)
    await interaction.response.send_message("🍶 Đã quên chuyện cũ.", ephemeral=True)

@app_tree.command(name="set-persona", description="Đổi persona")
@app_commands.describe(persona_key="Tên persona")
async def slash_set_persona(interaction: discord.Interaction, persona_key: str):
    _user_persona[interaction.user.id] = persona_key
    await interaction.response.send_message(f"🍶 Tại hạ đã đổi phong cách sang **{persona_key}**.", ephemeral=True)

@app_tree.command(name="history", description="Xem 6 tin nhắn gần nhất")
async def slash_history(interaction: discord.Interaction):
    rows = await fetch_history(interaction.channel.id)
    if not rows:
        return await interaction.response.send_message("📭 Chưa có gì trong tàng thư.", ephemeral=True)
    text = "\n".join([f"**{r[0]}**: {r[2]}" for r in rows])
    await interaction.response.send_message(text, ephemeral=True)

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.exception("Slash sync failed: %s", e)
    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    channel_name = getattr(message.channel, 'name', None)
    if channel_name not in TARGET_CHANNELS:
        return

    await bot.process_commands(message)

    lower = message.content.lower().strip() if message.content else ""
    if lower.startswith("!help"):
        await message.channel.send("Dùng `/help` để xem hướng dẫn.")
        return
    if lower.startswith("!reset"):
        await db_exec("DELETE FROM chats WHERE user_id = ? AND channel_id = ?", (message.author.id, message.channel.id))
        _user_persona.pop(message.author.id, None)
        await message.channel.send("🍶 Đã quên chuyện cũ.")
        return

    on_cd, remain = is_on_cooldown(message.author.id)
    if on_cd:
        await message.reply(f"🍶 Đại hiệp khoan vội! Chờ {int(remain)+1}s để tại hạ điều tức.")
        return

    user_text = message.content.strip() if message.content else ""
    if not user_text:
        return

    persona_key = _user_persona.get(message.author.id, PERSONA_NAME)
    await save_chat(message.author.id, message.channel.id, 'user', persona_key, user_text)
    set_cooldown(message.author.id)

    async with message.channel.typing():
        try:
            reply = await gemini_text_reply(PERSONA_SYSTEM, user_text, message.channel.id)
            if not reply.startswith('🍶'):
                # keep persona prefix
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

    await save_chat(message.author.id, message.channel.id, 'bot', persona_key, reply)

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    msg = reaction.message
    if msg.author != bot.user or str(reaction.emoji) != '🗑️':
        return

    perm = msg.channel.permissions_for(user)
    if perm.manage_messages:
        await msg.delete()
        return

    rows = await db_all("SELECT user_id FROM chats WHERE channel_id = ? ORDER BY id DESC LIMIT 6", (msg.channel.id,))
    if user.id in [r[0] for r in rows]:
        await msg.delete()

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
