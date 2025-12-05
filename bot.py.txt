import discord
import google.generativeai as genai
import os
import io
import PIL.Image
from keep_alive import keep_alive

# Lấy Key từ biến môi trường của Server
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Hướng dẫn hệ thống (Luật chơi & Tính cách)
system_instruction_text = """
Bạn là trợ lý ảo chuyên gia về game "Where Winds Meet" (Yến Vân Thập Lục Thanh).
Luật bất biến: Trong game này, người chơi KHÔNG THỂ tặng quà (give gifts) cho NPC.
Hãy trả lời ngắn gọn, tự nhiên và luôn ghi nhớ ngữ cảnh cuộc trò chuyện.
"""

genai.configure(api_key=GEMINI_API_KEY)

# Khởi tạo 2 Model: Pro (Chính) và Flash (Dự phòng)
model_pro = genai.GenerativeModel(model_name='gemini-1.5-pro', system_instruction=system_instruction_text)
model_flash = genai.GenerativeModel(model_name='gemini-1.5-flash', system_instruction=system_instruction_text)

user_chats = {} # Lưu lịch sử chat
user_model_status = {} # Lưu trạng thái người dùng đang dùng Pro hay Flash

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'{client.user} đã sẵn sàng phục vụ!')
    await client.change_presence(activity=discord.Game(name="Gõ !help để xem hướng dẫn"))

@client.event
async def on_message(message):
    if message.author == client.user: return

    # --- LỆNH HỖ TRỢ ---
    if message.content.strip().lower() in ["!help", "!huongdan"]:
        embed = discord.Embed(title="📜 Cẩm Nang Bot", description=f"Chào {message.author.name}!", color=0xffd700)
        embed.add_field(name="Tính năng", value="Bot dùng **Gemini 1.5 Pro**. Tự chuyển sang **Flash** nếu quá tải.", inline=False)
        embed.add_field(name="Sử dụng", value="Chat bình thường, gửi ảnh để hỏi, hoặc gõ `!reset` để xóa trí nhớ.", inline=False)
        embed.add_field(name="Lưu ý", value="Trong game này KHÔNG thể tặng quà NPC.", inline=False)
        await message.channel.send(embed=embed)
        return

    if message.content.strip().lower() == "!reset":
        if message.author.id in user_chats: del user_chats[message.author.id]
        if message.author.id in user_model_status: del user_model_status[message.author.id]
        await message.channel.send("🧹 Đã xóa ký ức và khôi phục về chế độ Pro.")
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
                        image_data = await attachment.read()
                        content_to_send.append(PIL.Image.open(io.BytesIO(image_data)))

            if not content_to_send: return

            # Mặc định dùng Pro
            if user_id not in user_chats:
                user_chats[user_id] = model_pro.start_chat(history=[])
                user_model_status[user_id] = "PRO"

            chat_session = user_chats[user_id]
            sent_message = await message.channel.send("Wait a sec...")

            # Hàm gửi tin nhắn (Stream)
            async def stream_response(session, content):
                response_stream = session.send_message(content, stream=True)
                collected_text = ""
                last_edit_length = 0
                for chunk in response_stream:
                    if chunk.text:
                        collected_text += chunk.text
                        if len(collected_text) - last_edit_length > 100:
                            if len(collected_text) < 2000:
                                await sent_message.edit(content=collected_text)
                                last_edit_length = len(collected_text)
                            else:
                                await sent_message.edit(content=collected_text[:2000])
                if 0 < len(collected_text) < 2000: await sent_message.edit(content=collected_text)
                return collected_text

            try:
                # Thử gửi bằng Model hiện tại
                await stream_response(chat_session, content_to_send)
            except Exception as e:
                # Nếu lỗi -> Chuyển sang Flash (Fallback)
                print(f"Lỗi Pro: {e}. Chuyển sang Flash.")
                await sent_message.edit(content="⚠️ Pro quá tải, đang chuyển sang Flash tốc độ cao...")
                old_history = chat_session.history
                new_session = model_flash.start_chat(history=old_history)
                user_chats[user_id] = new_session
                user_model_status[user_id] = "FLASH"
                await stream_response(new_session, content_to_send)
                await message.channel.send("*(Đã trả lời bằng Flash)*")

    except Exception as e:
        print(f"Lỗi hệ thống: {e}")
        await message.channel.send("Lỗi kết nối.")

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)