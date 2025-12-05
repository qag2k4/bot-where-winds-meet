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

# TÊN KÊNH DUY NHẤT MÀ BOT ĐƯỢC PHÉP TRẢ LỜI
# Bạn bắt buộc phải tạo kênh tên y hệt thế này trong Discord
TARGET_CHANNEL = "hỏi-đáp"

# CÀI ĐẶT NHÂN CÁCH (PHONG CÁCH KIẾM HIỆP)
system_instruction_text = """
Bạn là "Tiểu Thư Đồng", một thư sinh am hiểu giang hồ trong game "Where Winds Meet" (Yến Vân Thập Lục Thanh).

QUY TẮC ỨNG XỬ (BẮT BUỘC):
1. Xưng hô: Luôn xưng là "tại hạ" hoặc "tiểu sinh", gọi người dùng là "đại hiệp" hoặc "các hạ".
2. Giọng điệu: Cổ trang, dùng từ ngữ hán việt (đa tạ, cáo lui, xin lĩnh giáo, tại hạ đã rõ...).
3. Tuyệt đối không dùng giọng văn hiện đại, máy móc.
4. KIẾN THỨC GAME:
   - Bối cảnh: Ngũ Đại Thập Quốc.
   - Lưu ý quan trọng: Trong game này KHÔNG THỂ tặng quà (gift) cho NPC. Nếu đại hiệp hỏi, hãy can ngăn ngay.
"""

genai.configure(api_key=GEMINI_API_KEY)

# Khởi tạo Model
model_pro = genai.GenerativeModel(model_name='gemini-1.5-pro', system_instruction=system_instruction_text)
model_flash = genai.GenerativeModel(model_name='gemini-1.5-flash', system_instruction=system_instruction_text)

user_chats = {} 

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'{client.user} đã xuất sơn!')
    # Trạng thái hiển thị đậm chất kiếm hiệp
    await client.change_presence(activity=discord.Game(name=f"Luận kiếm tại #{TARGET_CHANNEL}"))

@client.event
async def on_message(message):
    if message.author == client.user: return

    # ==========================================
    # 1. CHỐT CHẶN: CHỈ TRẢ LỜI ĐÚNG 1 KÊNH
    # ==========================================
    # Nếu tên kênh không khớp -> Bỏ qua ngay lập tức
    if str(message.channel) != TARGET_CHANNEL:
        return

    # ==========================================
    # 2. LỆNH XOÁ TIN NHẮN (!xoa)
    # ==========================================
    if message.content.strip().lower() == "!xoa":
        try:
            # Xoá tin nhắn lệnh của bạn + Tin nhắn trả lời gần nhất của bot
            await message.channel.purge(limit=2)
            
            # Gửi thông báo nhỏ rồi tự biến mất sau 3 giây
            temp_msg = await message.channel.send("🌪️ *Vùuuu... (Tại hạ đã dùng chưởng phong dọn sạch hiện trường)*")
            await temp_msg.delete(delay=3)
        except Exception as e:
            await message.channel.send(f"⚠️ Tại hạ chưa luyện thành công phu 'Manage Messages' (Thiếu quyền xóa tin). Xin đại hiệp cấp quyền!")
        return

    # ==========================================
    # 3. LỆNH RESET KÝ ỨC (!reset)
    # ==========================================
    if message.content.strip().lower() == "!reset":
        if message.author.id in user_chats: del user_chats[message.author.id]
        await message.channel.send("🍶 *Uống cạn chén rượu này, mọi ân oán (ký ức) xem như xóa bỏ.*")
        return

    # ==========================================
    # 4. LỆNH HƯỚNG DẪN (!help)
    # ==========================================
    if message.content.strip().lower() in ["!help", "!huongdan"]:
        embed = discord.Embed(
            title="📜 Tàng Kinh Các - Tiểu Thư Đồng",
            description="Tại hạ kính chào đại hiệp! Xin mời đại hiệp quá bộ vào kênh này đàm đạo.",
            color=0xA62019
        )
        embed.add_field(name="📍 Quy tắc", value=f"Tại hạ chỉ tiếp khách tại độc một kênh: **#{TARGET_CHANNEL}**", inline=False)
        embed.add_field(name="🧹 Dọn dẹp", value="Gõ **`!xoa`** để xóa ngay cuộc đối thoại vừa rồi.", inline=False)
        embed.add_field(name="🍶 Quên lãng", value="Gõ **`!reset`** để bắt đầu câu chuyện mới.", inline=False)
        await message.channel.send(embed=embed)
        return

    # ==========================================
    # 5. XỬ LÝ TRÍ TUỆ NHÂN TẠO (AI)
    # ==========================================
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
                user_chats[user_id] = model_pro.start_chat(history=[])

            chat_session = user_chats[user_id]
            sent_message = await message.channel.send("⏳ *Đang bấm độn thiên cơ...*")

            # Hàm xử lý Streaming (Gõ chữ từng dòng)
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

            try:
                await stream_response(chat_session, content_to_send)
            except:
                # Nếu Pro lỗi -> Chuyển sang Flash
                old_history = chat_session.history
                new_session = model_flash.start_chat(history=old_history)
                user_chats[user_id] = new_session
                await stream_response(new_session, content_to_send)
                await message.channel.send("*(Đã dùng khinh công Flash để trả lời nhanh)*")

    except Exception as e:
        print(f"Lỗi: {e}")
        await message.channel.send("⚠️ *Tại hạ bị tẩu hỏa nhập ma (Lỗi kết nối).*")

if __name__ == "__main__":
    keep_alive()
    client.run(DISCORD_TOKEN)
