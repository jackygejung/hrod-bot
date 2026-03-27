import os
from collections import defaultdict, deque
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError
from groq import Groq

app = Flask(__name__)

# LINE setup
configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# Groq setup
groq_client = Groq(api_key=os.environ['GROQ_API_KEY'])

# เก็บประวัติแชทแต่ละกลุ่ม/แชท (50 ข้อความล่าสุด)
chat_history = defaultdict(lambda: deque(maxlen=50))

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
def handle_message(event):
    user_text = event.message.text

    # ระบุห้องแชท (กลุ่ม หรือ DM)
    if hasattr(event.source, 'group_id'):
        room_id = event.source.group_id
    elif hasattr(event.source, 'room_id'):
        room_id = event.source.room_id
    else:
        room_id = event.source.user_id

    # เก็บทุกข้อความเข้า history (ยกเว้นคำสั่ง AI)
    if not user_text.lower().startswith('ai'):
        chat_history[room_id].append(user_text)
        return  # ไม่ตอบถ้าไม่มี trigger

    # --- มี trigger "AI" ---
    command = user_text[2:].strip().lower()

    # คำสั่งสรุปแชท
    if command in ['สรุปแชท', 'สรุป', 'สรุปการสนทนา', 'summarize', 'summary']:
        messages = list(chat_history[room_id])
        if not messages:
            reply_text = "ยังไม่มีข้อความในกลุ่มที่ฉันจำได้เลยนะครับ 😅"
        else:
            history_text = '\n'.join(f"- {m}" for m in messages)
            prompt = f"สรุปการสนทนาต่อไปนี้เป็นภาษาไทย กระชับและได้ใจความ:\n\n{history_text}"
            response = groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "คุณคือ HROD BOT ผู้ช่วย AI สรุปการสนทนาเป็นภาษาไทยให้กระชับและได้ใจความ"},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.3-70b-versatile",
                max_tokens=600,
            )
            reply_text = "📋 สรุปการสนทนา:\n\n" + response.choices[0].message.content
    else:
        # คำถามทั่วไป
        question = user_text[2:].strip()
        if not question:
            question = "สวัสดี ช่วยอะไรได้บ้าง?"

        response = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "คุณคือ HROD BOT ผู้ช่วย AI ที่ฉลาดและเป็นมิตร "
                        "ตอบเป็นภาษาไทยเสมอ ตอบกระชับ ชัดเจน และสนุกสนาน "
                        "ถ้าถามเรื่องทั่วไปก็ตอบได้เลย"
                    )
                },
                {"role": "user", "content": question}
            ],
            model="llama-3.3-70b-versatile",
            max_tokens=500,
        )
        reply_text = response.choices[0].message.content

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
