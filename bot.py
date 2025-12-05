import discord
import google.generativeai as genai
import os
import io
import PIL.Image
from keep_alive import keep_alive

# ==========================================
# CẤU HÌNH
# ==========================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# TÊN KÊNH BOT HOẠT ĐỘNG
TARGET_CHANNEL = "chat-voi-gemini"

# CÀI ĐẶT NHÂN CÁCH
system_instruction_text = """
Bạn là "Tiểu Thư Đồng", NPC hướng dẫn game "Where Winds Meet".
QUY TẮC:
1. Xưng hô: Tại hạ / Đại hiệp.
2. Giọng điệu: Cổ trang, kiếm hiệp.
3. Tuyệt đối KHÔNG hướng dẫn tặng quà NPC (Game này không có tính năng đó).
"""

genai.configure(api_key=GEMINI_API_KEY)

# SỬ DỤNG BẢN FLASH (NHANH VÀ KHÔNG BỊ LỖI KẾT NỐI)
model = genai.GenerativeModel(
    model_name='gemini-1.5-flash', 
    system_instruction=system_instruction_text
)

user_chats = {} 

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'{client.user} đã xuất sơn!')
    await client.change_presence(activity=discord.Game(name=f"Đàm đạo tại #{TARGET_CHANNEL}"))

@client.event
async def on_message(message):
    if message.author == client.user: return

    # 1. KIỂM TRA KÊNH
    if str(message.channel) != TARGET_CHANNEL:
        return

    # 2. LỆNH XOÁ TIN NHẮN (!xoa)
    if message.content.lower().startswith("!xoa"):
        try:
            amount = 2
            parts = message.content.split()
            if len(parts) > 1 and parts[1].isdigit():
                amount = int(parts[1]) + 1
            
            await message.channel.purge(limit=amount)
            temp_msg = await message.channel.send("🌪️ *Vùuuu... Tại hạ đã dọn dẹp xong!*")
            await temp_msg.delete(delay=3)
        except:
            await message.channel.send("⚠️ Tại hạ thiếu quyền 'Manage Messages'.")
        return

    # 3. LỆNH RESET (!reset)
    if message.content.strip().lower() == "!reset":
        if message.author.id in user_chats: del user_chats[message.author.id]
        await message.channel.send("🍶 *Đã quên hết chuyện cũ.*")
        return

    # 4. LỆNH HELP (!help)
    if message.content.strip().lower() in ["!help", "!huongdan"]:
        embed = discord.Embed(title="📜 Tàng Kinh Các", description="Tiểu Thư Đồng kính chào!", color=0xA62019)
        embed.add_field(name="📍 Hoạt động", value=f"Duy nhất tại: **#{TARGET_CHANNEL}**", inline=False)
        embed.add_field(name="🛠️ Lệnh", value="`!xoa`, `!reset`", inline=False)
        await message.channel.send(embed=embed)
        return

    # 5. XỬ LÝ AI (Dùng Flash ổn định)
    try:
        async with message.channel.typing():
            user_id = message.author.id
            content_to_send = []
            if message.content: content_to_send.append(message.content)
            if message.attachments:
                for attachment in message.attachments:
                    if any(attachment.content_type.startswith(t) for t in ["image/"]):
                        content_to_send.append(PIL.Image.open(io.BytesIO(await attachment.read())))

            if not content_to_send: return

            if user_id not in user_chats:
                user_chats[user_id] = model.start_chat(history=[])

            chat_session = user_chats[user_id]
            sent_message = await message.channel.send("⏳ *Tại hạ đang suy ngẫm...*")

            # Streaming
            response_stream = chat_session.send_message(content_to_send, stream=True)
            collected_text = ""
            last_edit_length = 0
            
            for chunk in response_stream:
                if chunk.text:
                    collected_text += chunk.text
                    # Giảm tần suất edit để tránh lỗi Discord rate limit
                    if len(collected_text) - last_edit_length > 150: 
                        if len(collected_text) < 2000:
                            await sent_message.edit(content=collected_text)
                            last_edit_length = len(collected_text)
                        else:
                             await sent_message.edit(content=collected_text[:2000])

            if 0 < len(collected_text) < 2000: 
                await sent_message.edit(content=collected_text)

    except Exception as e:
        print(f"Lỗi: {e}")
        # Nếu vẫn lỗi thì khả năng cao là Key bị chết hẳn
        await message.channel.send(f"⚠️ *Lỗi kết nối (Key AI có vấn đề hoặc quá tải).*")

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)
