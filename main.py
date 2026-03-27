import os
import base64
from collections import defaultdict, deque
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent, FileMessageContent
)
from linebot.v3.exceptions import InvalidSignatureError
import google.generativeai as genai

app = Flask(__name__)
configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])
genai.configure(api_key=os.environ['GEMINI_API_KEY'])

# Chat history: {room_id: deque of "Name: text"}
chat_history = defaultdict(lambda: deque(maxlen=50))
# Last image per room: {room_id: {"base64": str, "sender": str}}
last_images = {}


def get_room_id(event):
    if hasattr(event.source, 'group_id'):
        return event.source.group_id
    elif hasattr(event.source, 'room_id'):
        return event.source.room_id
    return event.source.user_id


def get_user_name(event, api_client):
    try:
        line_bot_api = MessagingApi(api_client)
        user_id = event.source.user_id
        if hasattr(event.source, 'group_id'):
            profile = line_bot_api.get_group_member_profile(
                event.source.group_id, user_id)
        elif hasattr(event.source, 'room_id'):
            profile = line_bot_api.get_room_member_profile(
                event.source.room_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return "สมาชิก"


def send_reply(api_client, reply_token, text):
    line_bot_api = MessagingApi(api_client)
    line_bot_api.reply_message_with_http_info(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)]
        )
    )


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    user_text = event.message.text
    room_id = get_room_id(event)

    with ApiClient(configuration) as api_client:
        user_name = get_user_name(event, api_client)

        # Store all messages with sender name for context
        if not user_text.lower().startswith('ai'):
            chat_history[room_id].append(f"{user_name}: {user_text}")
            return

        command = user_text[2:].strip().lower()

        # Summarize chat history
        if command in ['สรุปแชท', 'สรุป', 'สรุปการสนทนา', 'summarize', 'summary']:
            messages = list(chat_history[room_id])
            if not messages:
                reply_text = "ยังไม่มีข้อความที่ฉันจำได้เลยครับ 😅"
            else:
                history_text = '\n'.join(f"- {m}" for m in messages)
                prompt = (
                    f"สรุปการสนทนาต่อไปนี้เป็นภาษาไทย "
                    f"ระบุชื่อผู้พูดด้วย กระชับและได้ใจความ:\n\n{history_text}"
                )
                model = genai.GenerativeModel(
                    'gemini-2.0-flash',
                    system_instruction="คุณคือ HROD BOT สรุปการสนทนาเป็นภาษาไทย ระบุชื่อผู้พูดด้วย"
                )
                response = model.generate_content(prompt)
                reply_text = "📋 สรุปการสนทนา:\n\n" + response.text

        # Analyze last image in detail
        elif command in ['ดูรูป', 'วิเคราะห์รูป', 'อ่านรูป', 'รูปล่าสุด', 'ดูรูปล่าสุด']:
            if room_id not in last_images:
                reply_text = "ยังไม่มีรูปที่ฉันจำได้ครับ ลองส่งรูปมาแล้วถามอีกครั้งนะครับ 😊"
            else:
                img = last_images[room_id]
                image_data = {
                    "mime_type": "image/jpeg",
                    "data": img['base64']
                }
                model = genai.GenerativeModel('gemini-2.0-flash')
                response = model.generate_content([
                    image_data,
                    f"วิเคราะห์และอธิบายรูปนี้อย่างละเอียดเป็นภาษาไทย รูปส่งโดย {img['sender']}"
                ])
                reply_text = f"🖼️ วิเคราะห์รูปจาก {img['sender']}:\n\n" + response.text

        # General question with context awareness
        else:
            question = user_text[2:].strip()
            if not question:
                question = "สวัสดี ช่วยอะไรได้บ้าง?"

            # Build context from recent chat history
            recent = list(chat_history[room_id])[-10:]
            context_str = ""
            if recent:
                context_str = "บริบทการสนทนาล่าสุด:\n" + "\n".join(f"  {m}" for m in recent) + "\n\n"

            model = genai.GenerativeModel(
                'gemini-2.0-flash',
                system_instruction=(
                    f"คุณคือ HROD BOT ผู้ช่วย AI ที่ฉลาดและเป็นมิตร "
                    f"ตอบเป็นภาษาไทยเสมอ กระชับ ชัดเจน สนุกสนาน "
                    f"ผู้ถามชื่อ {user_name} "
                    f"ถ้ามีบริบทการสนทนาให้นำมาประกอบการตอบด้วย"
                )
            )
            response = model.generate_content(context_str + question)
            reply_text = response.text

        send_reply(api_client, event.reply_token, reply_text)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    room_id = get_room_id(event)

    with ApiClient(configuration) as api_client:
        user_name = get_user_name(event, api_client)

        # Download and store image
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(event.message.id)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        last_images[room_id] = {"base64": image_base64, "sender": user_name}
        chat_history[room_id].append(f"{user_name}: [ส่งรูปภาพ]")

        # Auto-describe the image briefly
        model = genai.GenerativeModel('gemini-2.0-flash')
        response = model.generate_content([
            {"mime_type": "image/jpeg", "data": image_base64},
            "อธิบายรูปภาพนี้สั้นๆ 1-2 ประโยค เป็นภาษาไทย"
        ])

        desc = response.text
        reply_text = (
            f"🖼️ {user_name} ส่งรูป: {desc}\n\n"
            f"(พิมพ์ 'AI วิเคราะห์รูป' เพื่อดูรายละเอียดเพิ่มเติม)"
        )
        send_reply(api_client, event.reply_token, reply_text)


@handler.add(MessageEvent, message=FileMessageContent)
def handle_file(event):
    room_id = get_room_id(event)

    with ApiClient(configuration) as api_client:
        user_name = get_user_name(event, api_client)
        file_name = event.message.file_name

        # Download file content
        blob_api = MessagingApiBlob(api_client)
        file_bytes = blob_api.get_message_content(event.message.id)
        chat_history[room_id].append(f"{user_name}: [ส่งไฟล์: {file_name}]")

        try:
            # Try reading as UTF-8 text
            text_content = file_bytes.decode('utf-8')
            if len(text_content) > 3000:
                text_content = text_content[:3000] + "...(ตัดทอน)"

            model = genai.GenerativeModel(
                'gemini-2.0-flash',
                system_instruction="คุณคือ HROD BOT สรุปและวิเคราะห์เนื้อหาไฟล์เป็นภาษาไทย"
            )
            response = model.generate_content(
                f"สรุปเนื้อหาของไฟล์ '{file_name}':\n\n{text_content}"
            )
            reply_text = (
                f"📄 ไฟล์จาก {user_name} ({file_name}):\n\n"
                + response.text
            )
        except UnicodeDecodeError:
            reply_text = (
                f"📄 {user_name} ส่งไฟล์: {file_name}\n"
                f"ไฟล์ประเภทนี้ยังอ่านไม่ได้ครับ ลองส่งเป็น .txt นะครับ 😊"
            )

        send_reply(api_client, event.reply_token, reply_text)


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
