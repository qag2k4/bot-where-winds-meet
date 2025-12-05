import discord
import google.generativeai as genai
import os
import io
import asyncio
import PIL.Image
from keep_alive import keep_alive


# ======================================================
# CẤU HÌNH
# ======================================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TARGET_CHANNEL = "hoi-dap"   # kênh Discord


system_instruction_text = """
Bạn là "Tiểu Thư Đồng", NPC hướng dẫn game Where Winds Meet.
Quy tắc:
1. Xưng hô: Tại hạ / Đại hiệp.
2. Giọng điệu: Cổ trang, kiếm hiệp.
3. Không hướng dẫn tặng quà NPC.
"""


# ======================================================
# KIỂM TRA API KEY GEMINI KHẢ DỤNG
# ======================================================
def verify_gemini_key():
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        test_model = genai.GenerativeModel("gemini-1.5-flash")
        test_model.generate_content("ping")
        print("🔥 API KEY GEMINI HOẠT ĐỘNG TỐT.")
        return True
    except Exception as e:
        print("❌ API KEY GEMINI KHÔNG HOẠT ĐỘNG.")
        print("Chi tiết lỗi:", repr(e))
        return False


# Kiểm tra key trước khi chạy bot
verify_gemini_key()

# Tạo model chính
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=system_instruction_text
)


# Lưu session riêng từng user
user_chats = {}

# Discord client
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"{client.user} đã xuất sơn!")
    await client.change_presence(activity=discord.Game(name="đang ngắm mây và chờ đại hiệp"))


# ======================================================
# HÀM CHẠY GEMINI TRONG THREAD – KHÔNG BLOCK
# ======================================================
async def gemini_send(chat_session, content):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, chat_session.send_message, content)


# ======================================================
# XỬ LÝ TIN NHẮN
# ======================================================
@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # chỉ trả lời trong đúng kênh
    if str(message.channel.name) != TARGET_CHANNEL:
        return

    author_id = message.author.id

    # ===== LỆNH XOÁ =====
    if message.content.lower().startswith("!xoa"):
        try:
            parts = message.content.split()
            amount = 2
            if len(parts) > 1 and parts[1].isdigit():
                amount = int(parts[1]) + 1
            await message.channel.purge(limit=amount)
        except Exception as e:
            print("Lỗi xóa tin:", repr(e))
        return

    # ===== LỆNH RESET =====
    if message.content.lower() == "!reset":
        user_chats.pop(author_id, None)
        await message.channel.send("🍶 *Tại hạ đã quên hết chuyện cũ.*")
        return

    # ===== LỆNH HELP =====
    if message.content.lower() in ["!help", "!huongdan"]:
        embed = discord.Embed(
            title="📜 Tàng Kinh Các",
            description="RUBY xin hầu chuyện!",
            color=0xA62019
        )
        embed.add_field(name="🏯 Hoạt động tại:", value=f"#{TARGET_CHANNEL}", inline=False)
        embed.add_field(name="🛠️ Lệnh", value="`!xoa`, `!reset`, `!help`", inline=False)
        await message.channel.send(embed=embed)
        return

    # ===== XỬ LÝ AI =====
    try:
        async with message.channel.typing():
            content_to_send = []

            # text
            if message.content:
                content_to_send.append(message.content)

            # ảnh
            if message.attachments:
                for file in message.attachments:
                    if file.content_type and file.content_type.startswith("image/"):
                        img_bytes = await file.read()
                        img = PIL.Image.open(io.BytesIO(img_bytes))
                        content_to_send.append(img)

            if not content_to_send:
                return

            # Tạo session mới nếu chưa có
            if author_id not in user_chats:
                user_chats[author_id] = model.start_chat(history=[])

            chat_session = user_chats[author_id]

            # Gửi qua Gemini
            response = await gemini_send(chat_session, content_to_send)

            if response and response.text:
                # Discord giới hạn 2000 ký tự
                if len(response.text) > 2000:
                    for chunk in range(0, len(response.text), 1900):
                        await message.channel.send(response.text[chunk:chunk+1900])
                else:
                    await message.channel.send(response.text)

    except Exception as e:
        print("🔥 Lỗi AI:", repr(e))
        await message.channel.send("⚠️ *Thiên cơ bất khả lộ (Sự cố kết nối với AI).*")


# ======================================================
# CHẠY BOT
# ======================================================
if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)
