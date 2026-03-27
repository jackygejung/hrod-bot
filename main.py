import os
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

    # ตรวจสอบ trigger: ต้องขึ้นต้นด้วย "AI" หรือ "ai"
    if not user_text.lower().startswith('ai'):
        return

    # ตัด "AI " ออก เหลือแค่คำถามจริงๆ
    user_text = user_text[2:].strip()
    if not user_text:
        user_text = "สวัสดี ช่วยอะไรได้บ้าง?"

    # ส่งข้อความให้ Groq AI ตอบ
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
            {"role": "user", "content": user_text}
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
