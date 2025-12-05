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

# TÊN KÊNH: Bạn đã đổi thành "hoi-dap"
# Lưu ý: Trong Discord tên kênh phải viết thường, không dấu cách.
# Nếu kênh của bạn có dấu (ví dụ "hỏi-đáp"), bạn phải sửa dòng dưới này y hệt thế.
TARGET_CHANNEL = "hoi-dap"

system_instruction_text = """
Bạn là "Tiểu Thư Đồng", NPC hướng dẫn game "Where Winds Meet".
QUY TẮC:
1. Xưng hô: Tại hạ / Đại hiệp.
2. Giọng điệu: Cổ trang, kiếm hiệp, ngắn gọn.
3. Tuyệt đối KHÔNG hướng dẫn tặng quà NPC.
"""

genai.configure(api_key=GEMINI_API_KEY)

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
    # CẬP NHẬT TRẠNG THÁI MỚI TẠI ĐÂY
    await client.change_presence(activity=discord.Game(name="đang chuẩn bị tái xuất giang hồ"))

@client.event
async def on_message(message):
    if message.author == client.user: return
    
    # Kiểm tra đúng kênh hoi-dap mới được trả lời
    if str(message.channel.name) != TARGET_CHANNEL: return

    # --- LỆNH XOÁ ---
    if message.content.lower().startswith("!xoa"):
        try:
            amount = 2
            parts = message.content.split()
            if len(parts) > 1 and parts[1].isdigit(): amount = int(parts[1]) + 1
            await message.channel.purge(limit=amount)
        except: pass
        return

    # --- LỆNH RESET ---
    if message.content.strip().lower() == "!reset":
        if message.author.id in user_chats: del user_chats[message.author.id]
        await message.channel.send("🍶 *Đã quên hết chuyện cũ.*")
        return

    # --- LỆNH HELP ---
    if message.content.strip().lower() in ["!help", "!huongdan"]:
        embed = discord.Embed(title="📜 Tàng Kinh Các", description="Tiểu Thư Đồng kính chào!", color=0xA62019)
        embed.add_field(name="📍 Hoạt động", value=f"Tại: **#{TARGET_CHANNEL}**", inline=False)
        embed.add_field(name="🛠️ Lệnh", value="`!xoa`, `!reset`", inline=False)
        await message.channel.send(embed=embed)
        return

    # --- XỬ LÝ AI ---
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
            response = chat_session.send_message(content_to_send)
            
            if response.text:
                if len(response.text) > 2000:
                    k = 1900
                    for i in range(0, len(response.text), k):
                        await message.channel.send(response.text[i:i+k])
                else:
                    await message.channel.send(response.text)

    except Exception as e:
        print(f"Lỗi: {e}")
        await message.channel.send("⚠️ *Thiên cơ bất khả lộ (Lỗi kết nối).*")

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)
