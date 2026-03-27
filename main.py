import os
import base64
import requests
from datetime import datetime
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
from google import genai
from google.genai import types
import io
try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

app = Flask(__name__)
configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])
client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = 'jackygejung/hrod-bot'
MODEL = 'gemini-3-flash-preview'

chat_history = defaultdict(lambda: deque(maxlen=50))
last_images = {}
memory_cache = {}
CACHE_TTL = 180


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
            profile = line_bot_api.get_group_member_profile(event.source.group_id, user_id)
        elif hasattr(event.source, 'room_id'):
            profile = line_bot_api.get_room_member_profile(event.source.room_id, user_id)
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


def gemini_text(prompt, use_search=False):
    try:
        tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else []
        config = types.GenerateContentConfig(tools=tools) if tools else None
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=config
        )
        text = response.text if response.text else "ขออภัยครับ ตอบไม่ได้"
        if use_search and response.candidates:
            grounding = getattr(response.candidates[0], 'grounding_metadata', None)
            if grounding and hasattr(grounding, 'web_search_queries') and grounding.web_search_queries:
                queries = ", ".join(grounding.web_search_queries[:3])
                text += f"\n\n🔍 ค้นหา: {queries}"
        return text
    except Exception as e:
        return f"ข้อผิดพลาด: {str(e)}"


def gemini_vision(image_base64, prompt):
    try:
        image_data = base64.b64decode(image_base64)
        response = client.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
                prompt
            ]
        )
        return response.text if response.text else "ดูรูปไม่ได้ครับ"
    except Exception as e:
        return f"ข้อผิดพลาด: {str(e)}"


def _mem_path(room_id):
    return f"memory/{room_id}.md"


def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }


def read_memory(room_id):
    if not GITHUB_TOKEN:
        return "", None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_mem_path(room_id)}"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            content = base64.b64decode(data['content']).decode('utf-8')
            return content, data['sha']
    except Exception:
        pass
    return "", None


def read_memory_cached(room_id):
    now = datetime.now()
    cached = memory_cache.get(room_id)
    if cached and (now - cached['ts']).seconds < CACHE_TTL:
        return cached['content'], cached['sha']
    content, sha = read_memory(room_id)
    memory_cache[room_id] = {'content': content, 'sha': sha, 'ts': now}
    return content, sha


def write_memory(room_id, content, sha=None):
    if not GITHUB_TOKEN:
        return False
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_mem_path(room_id)}"
    encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    body = {"message": f"memory: update {room_id[:10]}", "content": encoded}
    if sha:
        body["sha"] = sha
    try:
        resp = requests.put(url, json=body, headers=_gh_headers(), timeout=5)
        if resp.status_code in [200, 201]:
            new_sha = resp.json()['content']['sha']
            memory_cache[room_id] = {'content': content, 'sha': new_sha, 'ts': datetime.now()}
            return True
    except Exception:
        pass
    return False


def add_memory_entry(room_id, user_name, note):
    content, sha = read_memory(room_id)
    now = datetime.now().strftime("%d/%m %H:%M")
    if not content:
        content = "# Memory\n\n"
    content += f"- [{now}] **{user_name}**: {note}\n"
    return write_memory(room_id, content, sha)


def clear_memory(room_id):
    _, sha = read_memory(room_id)
    return write_memory(room_id, "# Memory\n\n", sha)


def get_memory_lines(room_id):
    content, _ = read_memory_cached(room_id)
    return [l for l in (content or "").split('\n') if l.startswith('- [')]


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

        if not user_text.lower().startswith('ai'):
            chat_history[room_id].append(f"{user_name}: {user_text}")
            return

        command = user_text[2:].strip()
        cmd_lower = command.lower()

        if (cmd_lower.startswith('จำว่า ') or cmd_lower.startswith('โน๊ต ') or
                cmd_lower.startswith('note ') or cmd_lower.startswith('บันทึกว่า ')):
            note = ""
            for prefix in ['จำว่า ', 'โน๊ต ', 'note ', 'บันทึกว่า ']:
                if cmd_lower.startswith(prefix):
                    note = command[len(prefix):].strip()
                    break
            if not note:
                reply_text = "จะให้จำว่าอะไรครับ? เช่น: AI จำว่า ประชุมทุกวันจันทร์ 9 โมง"
            elif not GITHUB_TOKEN:
                reply_text = "ยังไม่ได้ตั้งค่า GITHUB_TOKEN ครับ"
            else:
                ok = add_memory_entry(room_id, user_name, note)
                reply_text = (f"จำแล้วครับ!\n\n{note}\n\n(พิมพ์ AI ดูโน๊ต เพื่อดูทั้งหมด)"
                              if ok else "บันทึกไม่สำเร็จครับ")

        elif cmd_lower in ['ความจำ', 'จำอะไรบ้าง', 'โน๊ต', 'ดูโน๊ต',
                           'บันทึก', 'ดูบันทึก', 'ดูความจำ', 'memory', 'note']:
            if not GITHUB_TOKEN:
                reply_text = "ยังไม่ได้ตั้งค่า GITHUB_TOKEN ครับ"
            else:
                lines = get_memory_lines(room_id)
                if not lines:
                    reply_text = "ยังไม่มีโน๊ตในกลุ่มนี้เลยครับ\n\nพิมพ์ AI จำว่า [ข้อความ] เพื่อบันทึก"
                else:
                    shown = lines[-15:]
                    reply_text = f"โน๊ตของกลุ่ม ({len(lines)} รายการ):\n\n" + "\n".join(shown)

        elif cmd_lower in ['ลบโน๊ต', 'ลบความจำ', 'ล้างโน๊ต', 'ล้างความจำ', 'ลบบันทึก', 'clear memory']:
            if not GITHUB_TOKEN:
                reply_text = "ยังไม่ได้ตั้งค่า GITHUB_TOKEN ครับ"
            else:
                ok = clear_memory(room_id)
                reply_text = "ลบโน๊ตทั้งหมดแล้วครับ" if ok else "ลบไม่สำเร็จครับ"

        elif cmd_lower in ['สรุปแชท', 'สรุป', 'สรุปการสนทนา', 'summarize', 'summary']:
            messages = list(chat_history[room_id])
            if not messages:
                reply_text = "ยังไม่มีข้อความที่ฉันจำได้เลยครับ"
            else:
                history_text = '\n'.join(f"- {m}" for m in messages)
                prompt = f"สรุปการสนทนาต่อไปนี้เป็นภาษาไทย ระบุชื่อผู้พูดด้วย กระชับและได้ใจความ:\n\n{history_text}"
                reply_text = "สรุปการสนทนา:\n\n" + gemini_text(prompt)

        elif (cmd_lower.startswith('ค้นหา ') or cmd_lower.startswith('search ') or
              cmd_lower.startswith('หา ') or cmd_lower.startswith('เสิร์ช ')):
            query = ""
            for prefix in ['ค้นหา ', 'search ', 'หา ', 'เสิร์ช ']:
                if cmd_lower.startswith(prefix):
                    query = command[len(prefix):].strip()
                    break
            if not query:
                reply_text = "จะให้ค้นหาอะไรครับ? เช่น: AI ค้นหา ราคาทองวันนี้"
            else:
                prompt = f"ค้นหาและสรุปข้อมูลเกี่ยวกับ '{query}' เป็นภาษาไทย กระชับและได้ใจความ"
                reply_text = "🔍 " + gemini_text(prompt, use_search=True)

        elif cmd_lower in ['ดูรูป', 'วิเคราะห์รูป', 'อ่านรูป', 'รูปล่าสุด', 'ดูรูปล่าสุด']:
            if room_id not in last_images:
                reply_text = "ยังไม่มีรูปที่ฉันจำได้ครับ ลองส่งรูปมาแล้วถามอีกครั้ง"
            else:
                img = last_images[room_id]
                prompt = f"วิเคราะห์และอธิบายรูปนี้อย่างละเอียดเป็นภาษาไทย รูปส่งโดย {img['sender']}"
                reply_text = f"วิเคราะห์รูปจาก {img['sender']}:\n\n" + gemini_vision(img['base64'], prompt)

        else:
            question = command.strip() or "สวัสดี ช่วยอะไรได้บ้าง?"
            recent = list(chat_history[room_id])[-10:]
            context_str = ""
            if recent:
                context_str = "บริบทสนทนา:\n" + "\n".join(f"  {m}" for m in recent) + "\n\n"
            mem_str = ""
            if GITHUB_TOKEN:
                mem_lines = get_memory_lines(room_id)
                if mem_lines:
                    mem_str = "โน๊ตของกลุ่มนี้:\n" + "\n".join(mem_lines[-10:]) + "\n\n"
            news_keywords = ['ข่าว', 'วันนี้', 'ล่าสุด', 'ตอนนี้', 'ราคา', 'พยากรณ์', 'อากาศ', 'หุ้น', 'news', 'today', 'latest', 'price']
            use_search = any(kw in question.lower() for kw in news_keywords)
            full_prompt = (
                f"คุณคือ HROD BOT ผู้ช่วย AI ที่ฉลาดและเป็นมิตร ตอบเป็นภาษาไทยเสมอ "
                f"กระชับ ชัดเจน สนุกสนาน ผู้ถามชื่อ {user_name} "
                f"ถ้ามีบริบทหรือโน๊ตของกลุ่มให้นำมาประกอบการตอบด้วย\n\n"
                f"{mem_str}{context_str}{question}"
            )
            reply_text = gemini_text(full_prompt, use_search=use_search)

        send_reply(api_client, event.reply_token, reply_text)


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    room_id = get_room_id(event)
    with ApiClient(configuration) as api_client:
        user_name = get_user_name(event, api_client)
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(event.message.id)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        last_images[room_id] = {"base64": image_base64, "sender": user_name}
        chat_history[room_id].append(f"{user_name}: [ส่งรูปภาพ]")
        desc = gemini_vision(image_base64, "อธิบายรูปภาพนี้สั้นๆ 1-2 ประโยค เป็นภาษาไทย")
        reply_text = f"{user_name} ส่งรูป: {desc}\n\n(พิมพ์ AI วิเคราะห์รูป เพื่อดูรายละเอียดเพิ่มเติม)"
        send_reply(api_client, event.reply_token, reply_text)


@handler.add(MessageEvent, message=FileMessageContent)
def handle_file(event):
    room_id = get_room_id(event)
    with ApiClient(configuration) as api_client:
        user_name = get_user_name(event, api_client)
        file_name = event.message.file_name
        blob_api = MessagingApiBlob(api_client)
        file_bytes = blob_api.get_message_content(event.message.id)
        chat_history[room_id].append(f"{user_name}: [ส่งไฟล์: {file_name}]")

        if file_name.lower().endswith('.pdf'):
            if not HAS_PYPDF:
                reply_text = f"ยังไม่ได้ติดตั้ง pypdf ครับ"
            else:
                try:
                    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                    pages = len(reader.pages)
                    text_content = ""
                    for page in reader.pages:
                        text_content += page.extract_text() or ""
                        if len(text_content) > 4000:
                            text_content = text_content[:4000] + "...(ตัดทอน)"
                            break
                    if not text_content.strip():
                        reply_text = f"PDF จาก {user_name} ({file_name}, {pages} หน้า)\nไม่สามารถดึงข้อความได้ครับ อาจเป็น PDF รูปภาพ"
                    else:
                        prompt = f"สรุปเนื้อหาของ PDF '{file_name}' ({pages} หน้า) เป็นภาษาไทย กระชับและได้ใจความ:\n\n{text_content}"
                        summary = gemini_text(prompt)
                        reply_text = f"PDF จาก {user_name} ({file_name}, {pages} หน้า):\n\n" + summary
                except Exception as e:
                    reply_text = f"อ่าน PDF ไม่ได้ครับ: {str(e)}"
        else:
            try:
                text_content = file_bytes.decode('utf-8')
                if len(text_content) > 3000:
                    text_content = text_content[:3000] + "...(ตัดทอน)"
                prompt = f"สรุปเนื้อหาของไฟล์ '{file_name}':\n\n{text_content}"
                summary = gemini_text(prompt)
                reply_text = f"ไฟล์จาก {user_name} ({file_name}):\n\n" + summary
            except UnicodeDecodeError:
                reply_text = f"{user_name} ส่งไฟล์: {file_name}\nไฟล์ประเภทนี้ยังอ่านไม่ได้ครับ ลองส่งเป็น .txt หรือ .pdf นะครับ"

        send_reply(api_client, event.reply_token, reply_text)


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
