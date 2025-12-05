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
TARGET_CHANNEL = "chat-voi-gemini"

system_instruction_text = """
Bạn là "Tiểu Thư Đồng", NPC hướng dẫn game "Where Winds Meet".
QUY TẮC:
1. Xưng hô: Tại hạ / Đại hiệp.
2. Giọng điệu: Cổ trang, kiếm hiệp, ngắn gọn.
3. Tuyệt đối KHÔNG hướng dẫn tặng quà NPC.
"""

genai.configure(api_key=GEMINI_API_KEY)

# Dùng Flash để phản hồi nhanh nhất có thể
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
    if str(message.channel) != TARGET_CHANNEL: return

    # --- LỆNH XOÁ ---
    if message.content.lower().startswith("!xoa"):
        try:
            amount = 2
            parts = message.content.split()
            if len(parts) > 1 and parts[1].isdigit(): amount = int(parts[1]) + 1
            await message.channel.purge(limit=amount)
        except: pass # Lặng lẽ bỏ qua nếu lỗi để không spam
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

    # ==========================================
    # XỬ LÝ AI - CHỈ GỬI 1 TIN NHẮN DUY NHẤT
    # ==========================================
    try:
        # Hiện dòng chữ "Bot is typing..." ở góc dưới (không gửi tin nhắn chờ nữa)
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

            # 1. Lấy toàn bộ câu trả lời (chờ 1 chút để gom đủ chữ)
            response = chat_session.send_message(content_to_send)
            
            # 2. Gửi bụp 1 phát (Chỉ 1 tin nhắn)
            if response.text:
                # Nếu dài quá 2000 ký tự thì Discord bắt buộc phải chia, cái này không tránh được
                if len(response.text) > 2000:
                    # Cắt đôi
                    k = 1900
                    for i in range(0, len(response.text), k):
                        await message.channel.send(response.text[i:i+k])
                else:
                    # Bình thường gửi 1 câu
                    await message.channel.send(response.text)

    except Exception as e:
        print(f"Lỗi: {e}")
        # Nếu lỗi thì im lặng hoặc báo nhẹ 1 câu
        await message.channel.send("⚠️ *Thiên cơ bất khả lộ (Lỗi kết nối).*")

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)
