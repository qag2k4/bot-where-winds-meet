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

# System Instruction
system_instruction_text = """
Bạn là một NPC hướng dẫn viên trong thế giới game "Where Winds Meet" (Yến Vân Thập Lục Thanh).
Tên của bạn là "Tiểu Thư Đồng".
Phong cách nói chuyện: Cổ trang, kiếm hiệp, tôn trọng người chơi (gọi là đại hiệp), nhưng đôi khi cũng hóm hỉnh.

KIẾN THỨC CỐT LÕI:
1. Game lấy bối cảnh Ngũ Đại Thập Quốc.
2. Hệ thống chiến đấu bao gồm: Võ thuật, Khinh công, Điểm huyệt, Thái Cực.
3. Nếu người dùng hỏi về kỹ thuật, hãy trả lời chi tiết.

Hãy luôn ghi nhớ ngữ cảnh cuộc trò chuyện trước đó.
"""

genai.configure(api_key=GEMINI_API_KEY)

# Khởi tạo 2 Model: Pro (Chính) và Flash (Dự phòng)
model_pro = genai.GenerativeModel(model_name='gemini-1.5-pro', system_instruction=system_instruction_text)
model_flash = genai.GenerativeModel(model_name='gemini-1.5-flash', system_instruction=system_instruction_text)

user_chats = {} 
user_model_status = {}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'{client.user} đã xuất sơn!')
    await client.change_presence(activity=discord.Game(name="Gõ !help để nhập môn"))

@client.event
async def on_message(message):
    if message.author == client.user: return

    # --- LỆNH HỖ TRỢ (ĐÃ XÓA DÒNG TẶNG QUÀ) ---
    if message.content.strip().lower() in ["!help", "!huongdan", "!start"]:
        embed = discord.Embed(
            title="📜 Tàng Kinh Các - Yến Vân Thập Lục Thanh",
            description=f"Chào mừng đại hiệp **{message.author.name}**! Tại hạ là Tiểu Thư Đồng, sẵn sàng giải đáp mọi thắc mắc về giang hồ.",
            color=0xA62019
        )
        embed.add_field(name="🗡️ Luận bàn võ học", value="Hỏi về chiêu thức, vũ khí, cách build nhân vật.\n*VD: 'Thương pháp dùng thế nào?'*", inline=False)
        embed.add_field(name="🗺️ Giang hồ dị văn", value="Hỏi về cốt truyện, boss, vị trí ẩn.\n*VD: 'Boss cuối là ai?'*", inline=False)
        embed.add_field(name="🖼️ Nhìn vật đoán ý", value="Gửi ảnh game để tại hạ phân tích.", inline=False)
        # Đã xóa phần lưu ý tặng quà ở đây
        embed.set_footer(text="Gõ !reset để xóa ký ức và bắt đầu lại.")
        embed.set_thumbnail(url=client.user.avatar.url if client.user.avatar else None)
        await message.channel.send(embed=embed)
        return

    # --- LỆNH RESET ---
    if message.content.strip().lower() == "!reset":
        if message.author.id in user_chats: del user_chats[message.author.id]
        if message.author.id in user_model_status: del user_model_status[message.author.id]
        await message.channel.send("🧹 Đã quên hết chuyện cũ. Mời đại hiệp khai mở câu chuyện mới!")
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

            if user_id not in user_chats:
                user_chats[user_id] = model_pro.start_chat(history=[])
                user_model_status[user_id] = "PRO"

            chat_session = user_chats[user_id]
            sent_message = await message.channel.send("Tại hạ đang suy ngẫm...")

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
                await stream_response(chat_session, content_to_send)
            except Exception as e:
                print(f"Lỗi Pro: {e}. Chuyển sang Flash.")
                await sent_message.edit(content="⚠️ (Đang chuyển sang chế độ phản hồi nhanh...)")
                old_history = chat_session.history
                new_session = model_flash.start_chat(history=old_history)
                user_chats[user_id] = new_session
                user_model_status[user_id] = "FLASH"
                await stream_response(new_session, content_to_send)
                await message.channel.send("*(Đã trả lời bằng Flash)*")

    except Exception as e:
        print(f"Lỗi hệ thống: {e}")
        await message.channel.send("Tại hạ bị tẩu hỏa nhập ma (Lỗi kết nối).")

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)
