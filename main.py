import os
import json
import random
import requests
import re
import subprocess
import asyncio
import threading
import time
import base64
import io
from datetime import datetime

import nest_asyncio
from telethon.sync import TelegramClient
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PhoneNumberInvalidError, FloodWaitError
)
from telethon.sessions import StringSession
from pymongo import MongoClient
from flask import Flask, render_template_string, request, jsonify, send_file
import logging

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
nest_asyncio.apply()

# ================= ফোল্ডার ও কনফিগারেশন =================
IMAGE_SAVE_PATH = 'downloads/'
os.makedirs(IMAGE_SAVE_PATH, exist_ok=True)

# 🟢 Security: Environment Variables (Render Dashboard থেকে সেট করতে হবে)
API_ID = os.getenv('TG_API_ID')
API_HASH = os.getenv('TG_API_HASH')
CHANNEL_USERNAME = os.getenv('TG_CHANNEL')
MESSAGE_SCAN_LIMIT = int(os.getenv('SCAN_LIMIT', '1000'))
SESSION_STRING = os.getenv('TELEGRAM_STRING_SESSION', '')

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GAS_PROXY_URL = os.getenv('GAS_PROXY_URL')
GAS_WEB_APP_URL = os.getenv('GAS_WEB_APP_URL')
PHP_API_SECRET = os.getenv('PHP_API_SECRET')

# ================= MongoDB Setup =================
MONGO_URI = os.getenv("MONGO_URI")
db = None
logs_col = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        # 🟢 ডাটাবেস কানেকশন টেস্ট করা হচ্ছে (Ping)
        mongo_client.admin.command('ping')
        print("✅ MongoDB Successfully Connected!")
        
        db = mongo_client["assetprim_uploader"]
        logs_col = db["upload_logs"]
    except Exception as e:
        print(f"❌ MongoDB Connection Error: {e}")
else:
    print("⚠️ MONGO_URI environment variable is missing!")

app_state = {
    'is_running': False,
    'uploaded': 0,
    'failed': 0,
    'scanned': 0,
    'status_msg': 'Idle — Ready to start',
    'live_logs': []
}

tg_login_state = {'client': None, 'phone': None, 'stage': 'idle'}

DEVICE_MODEL = "iPhone 15 Pro Max"
SYSTEM_VERSION = "iOS 17.4.1"
APP_VERSION = "10.14"

tg_loop = asyncio.new_event_loop()

def _start_tg_loop():
    asyncio.set_event_loop(tg_loop)
    tg_loop.run_forever()

threading.Thread(target=_start_tg_loop, daemon=True).start()

def run_async(coro, timeout=30):
    future = asyncio.run_coroutine_threadsafe(coro, tg_loop)
    return future.result(timeout=timeout)

import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.1-flash-lite')


def add_live_log(msg):
    log_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(log_msg)
    app_state['live_logs'].append(log_msg)
    if len(app_state['live_logs']) > 60:
        app_state['live_logs'].pop(0)


# ================= MongoDB Functions =================
def log_process(msg_id, title, status):
    """ডাটাবেসে প্রসেসিং স্ট্যাটাস সেভ বা আপডেট করা"""
    if logs_col is None: return
    try:
        logs_col.update_one(
            {"msg_id": msg_id},
            {"$set": {
                "msg_id": msg_id,
                "title": title,
                "status": status,
                "timestamp": datetime.now()
            }},
            upsert=True
        )
    except Exception as e:
        add_live_log(f"⚠️ DB Error: {e}")

def is_processed(msg_id):
    """ডাটাবেসে আগে থেকে SUCCESS স্ট্যাটাস আছে কিনা চেক করা"""
    if logs_col is None: return False
    try:
        record = logs_col.find_one({"msg_id": msg_id, "status": "SUCCESS"})
        return record is not None
    except Exception:
        return False


def slugify(text):
    clean_text = re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')
    return clean_text[:40].strip('-')


def safe_num(val):
    match = re.search(r'\d+(\.\d+)?', str(val))
    return match.group() if match else "0"


async def process_with_gemini(text):
    prompt = f"""You are a professional course marketplace content writer.
Convert the user's raw course information into clean, professional, marketplace-ready details.
Return ONLY a valid JSON object. Do not include markdown tags like ```json.

JSON Structure:
1. "title": Professional, attractive course title.
2. "slug": Short, clean, SEO-friendly lowercase slug using hyphens (Max 3-4 words).
3. "short_description": 1-2 concise sentences explaining what they will learn.
4. "description": 1-2 short paragraphs explaining the course. Do not invent details.
5. "what_you_will_learn": An array of 6-9 concise bullet points based ONLY on provided info.
6. "price": Suggested price (number only, standard market rate).
7. "category_name": Best matching category name.
8. "mega_link": Extract Mega.nz link if present, else empty.
9. "drive_link": Extract Google Drive link if present, else empty.

Text to process:
{text}"""
    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        clean_json = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_json)
    except Exception as error:
        add_live_log(f"❌ Gemini Error: {error}")
        return None


def get_drive_details(drive_url):
    if not drive_url:
        return {"total_lessons": 0, "total_duration": 0, "preview_url": ""}
    try:
        res = requests.get(GAS_WEB_APP_URL, params={"url": drive_url}, timeout=15)
        data = res.json()
        if data.get("success"):
            return {
                "total_lessons": data.get("count", 0),
                "total_duration": data.get("total_minutes", 0),
                "preview_url": data.get("preview_url", "")
            }
    except Exception:
        pass
    return {"total_lessons": 0, "total_duration": 0, "preview_url": ""}


def get_mega_details(mega_url):
    if not mega_url:
        return {"total_lessons": 0, "total_duration": 0, "preview_url": ""}
    js_code = """
    const { File } = require('megajs');
    async function scanMega(url) {
        let videoCount = 0;
        try {
            const folder = File.fromURL(url);
            await folder.loadAttributes();
            const countFiles = (node) => {
                if (node.directory) { if (node.children) node.children.forEach(countFiles);
                } else { if ((node.name||"").toLowerCase().match(/\\.(mp4|mkv|avi)$/)) videoCount++; }
            };
            countFiles(folder);
        } catch(e){}
        console.log(JSON.stringify({ count: videoCount }));
    }
    const url = process.argv[2]; if (url) scanMega(url);
    """
    with open("fast_mega_scanner.js", "w", encoding="utf-8") as f:
        f.write(js_code)
    try:
        result = subprocess.run(["node", "fast_mega_scanner.js", mega_url], capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout.strip())
        vc = data.get("count", 0)
        return {"total_lessons": vc, "total_duration": vc * random.randint(30, 45) if vc > 0 else 0, "preview_url": ""}
    except Exception:
        return {"total_lessons": 0, "total_duration": 0, "preview_url": ""}


# ================= Telegram Login =================
async def tg_check_authorized():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, device_model=DEVICE_MODEL,
                             system_version=SYSTEM_VERSION, app_version=APP_VERSION, loop=tg_loop)
    await client.connect()
    authorized = await client.is_user_authorized()
    await client.disconnect()
    return authorized


async def tg_send_code(phone):
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, device_model=DEVICE_MODEL,
                             system_version=SYSTEM_VERSION, app_version=APP_VERSION, loop=tg_loop)
    await client.connect()
    if await client.is_user_authorized():
        await client.disconnect()
        return {"status": "already_authorized"}
    await client.send_code_request(phone)
    tg_login_state.update({'client': client, 'phone': phone, 'stage': 'code_sent'})
    return {"status": "code_sent"}


async def tg_verify_code(code):
    client = tg_login_state.get('client')
    phone = tg_login_state.get('phone')
    if not client or not phone:
        return {"status": "error", "message": "আগে ফোন নম্বর দিয়ে কোড পাঠাও।"}
    try:
        await client.sign_in(phone=phone, code=code)
        
        # 🟢 সেশন সেভ এবং প্রিন্ট করা
        new_session = client.session.save()
        add_live_log(f"🔑 YOUR NEW SESSION STRING: {new_session}")
        add_live_log("✅ Login Success! উপরের স্ট্রিংটি কপি করে Render এ সেভ করুন।")
        
        await client.disconnect()
        tg_login_state.update({'client': None, 'stage': 'done'})
        return {"status": "success"}
    except SessionPasswordNeededError:
        tg_login_state['stage'] = 'need_password'
        return {"status": "need_password"}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return {"status": "error", "message": "কোডটি ভুল অথবা মেয়াদোত্তীর্ণ। আবার চেষ্টা করো।"}
    except Exception as error:
        return {"status": "error", "message": str(error)}


async def tg_verify_password(password):
    client = tg_login_state.get('client')
    if not client:
        return {"status": "error", "message": "সেশন পাওয়া যায়নি, আবার প্রথম থেকে শুরু করো।"}
    try:
        await client.sign_in(password=password)
        
        # 🟢 সেশন সেভ এবং প্রিন্ট করা
        new_session = client.session.save()
        add_live_log(f"🔑 YOUR NEW SESSION STRING: {new_session}")
        add_live_log("✅ Login Success! উপরের স্ট্রিংটি কপি করে Render এ সেভ করুন।")
        
        await client.disconnect()
        tg_login_state.update({'client': None, 'stage': 'done'})
        return {"status": "success"}
    except Exception as error:
        return {"status": "error", "message": str(error)}


# ================= কোর ইঞ্জিন =================
async def run_bot_engine():
    global app_state
    app_state['status_msg'] = 'Connecting to Telegram...'
    add_live_log("🚀 ইঞ্জিন স্টার্ট হচ্ছে... (MongoDB + StringSession Mode)")

    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, device_model=DEVICE_MODEL,
                                 system_version=SYSTEM_VERSION, app_version=APP_VERSION)
        await client.connect()

        if not await client.is_user_authorized():
            add_live_log("⚠️ টেলিগ্রাম সেশন লগিন করা নেই! উপরের প্যানেল থেকে লগিন করো।")
            app_state['is_running'] = False
            return

        add_live_log("✅ টেলিগ্রাম কানেকশন সফল!")
        target_channel = str(CHANNEL_USERNAME).strip()

        if '/+' in target_channel or target_channel.startswith('+'):
            invite_hash = target_channel.split('+')[-1].replace('/', '').strip()
            try:
                updates = await client(ImportChatInviteRequest(invite_hash))
                target_channel = updates.chats[0].id
            except Exception:
                pass
        elif not target_channel.lstrip('-').isdigit():
            if 't.me/' in target_channel:
                target_channel = target_channel.split('t.me/')[-1].replace('/', '').strip()
            if not target_channel.startswith('@'):
                target_channel = '@' + target_channel
        else:
            target_channel = int(target_channel)

        add_live_log(f"📡 চ্যানেল স্ক্যান শুরু হচ্ছে: {target_channel} (limit {MESSAGE_SCAN_LIMIT})")

        async for message in client.iter_messages(target_channel, limit=MESSAGE_SCAN_LIMIT):
            if not app_state['is_running']:
                add_live_log("🛑 ইঞ্জিন স্টপ করা হয়েছে।")
                break

            if not (message.text or message.photo):
                continue

            msg_id = message.id
            app_state['scanned'] += 1

            if is_processed(msg_id):
                continue

            text_content = message.text or ""
            if not ("drive.google.com" in text_content.lower() or "mega.nz" in text_content.lower()):
                continue

            add_live_log(f"🔍 নতুন কোর্স পাওয়া গেছে (ID: {msg_id})। Gemini প্রসেসিং চলছে...")
            ai_data = await process_with_gemini(text_content)

            if not ai_data or not ai_data.get('title'):
                app_state['failed'] += 1
                add_live_log("❌ Gemini ডেটা দিতে ব্যর্থ হয়েছে।")
                log_process(msg_id, f"msg-{msg_id}", "FAILED - Gemini")
                continue

            full_desc = ai_data.get('description', '')
            learning_points = ai_data.get('what_you_will_learn', [])
            if learning_points:
                full_desc += "\n\nWhat You'll Learn:\n" + ''.join(f"- {p}\n" for p in learning_points)

            course_info = {"total_lessons": 0, "total_duration": 0, "preview_url": ""}
            active_url = ""
            if ai_data.get('drive_link'):
                active_url = ai_data['drive_link']
                course_info = get_drive_details(active_url)
            elif ai_data.get('mega_link'):
                active_url = ai_data['mega_link']
                course_info = get_mega_details(active_url)

            img_b64, img_name = "", ""
            if message.photo:
                img_name = f"course_{msg_id}_{int(datetime.now().timestamp())}.jpg"
                local_filepath = os.path.join(IMAGE_SAVE_PATH, img_name)
                await client.download_media(message.photo, local_filepath)
                with open(local_filepath, "rb") as img_file:
                    img_b64 = base64.b64encode(img_file.read()).decode('utf-8')
                os.remove(local_filepath)  # Space বাঁচানোর জন্য ইমেজ ডিলিট করা হচ্ছে

            random_discount = random.randint(8, 20)

            payload = {
                "api_key": PHP_API_SECRET,
                "title": str(ai_data.get('title', '')),
                "slug": slugify(ai_data.get('slug') or ai_data.get('title', '')),
                "description": str(full_desc),
                "short_description": str(ai_data.get('short_description') or ''),
                "price": safe_num(ai_data.get('price')),
                "discount_price": str(random_discount),
                "category_id": "1",
                "video_preview_url": str(course_info.get('preview_url') or ''),
                "total_lessons": str(course_info.get('total_lessons') or 0),
                "total_duration": str(course_info.get('total_duration') or 0),
                "google_drive_url": str(active_url or ''),
                "telegram_channel": "",
                "language": "English",
                "level": "advanced",
                "image_base64": img_b64,
                "image_name": img_name
            }

            try:
                add_live_log("🌐 Apps Script Proxy হয়ে সার্ভারে ডেটা পাঠানো হচ্ছে...")
                response = requests.post(GAS_PROXY_URL, json=payload, timeout=40)
                raw_text = response.text.strip()

                if not raw_text:
                    app_state['failed'] += 1
                    add_live_log(f"❌ ফাঁকা রেসপন্স (HTTP {response.status_code})")
                    log_process(msg_id, ai_data['title'], "FAILED - Blank API")
                    continue

                if "aes.js" in raw_text or "toNumbers" in raw_text:
                    app_state['failed'] += 1
                    add_live_log("❌ JS Challenge পাওয়া গেছে!")
                    log_process(msg_id, ai_data['title'], "FAILED - Blocked by Host")
                    continue

                json_str = raw_text
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match:
                    json_str = match.group(0)

                try:
                    result = json.loads(json_str)
                except ValueError:
                    app_state['failed'] += 1
                    add_live_log(f"❌ API রেসপন্স JSON নয়।")
                    log_process(msg_id, ai_data['title'], "FAILED - Invalid JSON")
                    continue

                if result.get("status") == "success":
                    app_state['uploaded'] += 1
                    add_live_log(f"✅ আপলোড সফল: {ai_data['title'][:35]}")
                    log_process(msg_id, ai_data['title'], "SUCCESS")
                else:
                    app_state['failed'] += 1
                    add_live_log(f"❌ API এরর: {result.get('message')}")
                    log_process(msg_id, ai_data['title'], "FAILED")

            except Exception as error:
                app_state['failed'] += 1
                add_live_log(f"❌ রিকোয়েস্ট ফেইল: {error}")
                log_process(msg_id, ai_data['title'], "FAILED - Network")

            sleep_time = random.uniform(5.5, 9.5)
            add_live_log(f"⏳ {sleep_time:.1f} সেকেন্ড অপেক্ষা করা হচ্ছে...")
            await asyncio.sleep(sleep_time)

        if app_state['is_running']:
            add_live_log("🏁 স্ক্যান সম্পন্ন হয়েছে!")
            app_state['status_msg'] = 'Scan complete — Idle'

    except Exception as error:
        app_state['status_msg'] = 'Error occurred'
        add_live_log(f"💥 ক্রিটিক্যাল এরর: {error}")
    finally:
        app_state['is_running'] = False
        if 'client' in locals():
            await client.disconnect()


def start_background_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot_engine())


# ================= Flask Web GUI =================
app = Flask(__name__)

# নিশ্চিত করুন যে templates/index.html ফোল্ডারে আপনার HTML ফাইলটি আছে
try:
    with open('templates/index.html', 'r', encoding='utf-8') as f:
        HTML_TEMPLATE = f.read()
except FileNotFoundError:
    HTML_TEMPLATE = "<h1>HTML File Not Found!</h1><p>দয়া করে templates ফোল্ডারের ভেতর index.html ফাইলটি তৈরি করুন।</p>"

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/status')
def status():
    return jsonify(app_state)

@app.route('/start', methods=['POST'])
def start():
    if not app_state['is_running']:
        app_state['is_running'] = True
        threading.Thread(target=start_background_loop, daemon=True).start()
    return jsonify({"msg": "Started"})

@app.route('/stop', methods=['POST'])
def stop():
    app_state['is_running'] = False
    app_state['status_msg'] = 'Stopping — please wait for current loop to finish'
    return jsonify({"msg": "Stopped"})

@app.route('/telegram_status')
def telegram_status():
    try:
        authorized = run_async(tg_check_authorized(), timeout=15)
    except Exception:
        authorized = False
    return jsonify({"authorized": authorized})

@app.route('/telegram_send_code', methods=['POST'])
def telegram_send_code():
    data = request.get_json(force=True) or {}
    phone = data.get('phone', '').strip()
    if not phone:
        return jsonify({"status": "error", "message": "ফোন নম্বর দাও"}), 400
    try:
        return jsonify(run_async(tg_send_code(phone), timeout=30))
    except FloodWaitError as error:
        return jsonify({"status": "error", "message": f"{error.seconds} সেকেন্ড পরে চেষ্টা করো।"})
    except PhoneNumberInvalidError:
        return jsonify({"status": "error", "message": "ফোন নম্বর সঠিক ফরম্যাটে দাও।"})
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)})

@app.route('/telegram_verify_code', methods=['POST'])
def telegram_verify_code():
    data = request.get_json(force=True) or {}
    code = data.get('code', '').strip()
    try:
        return jsonify(run_async(tg_verify_code(code), timeout=30))
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)})

@app.route('/telegram_verify_password', methods=['POST'])
def telegram_verify_password():
    data = request.get_json(force=True) or {}
    password = data.get('password', '')
    try:
        return jsonify(run_async(tg_verify_password(password), timeout=30))
    except Exception as error:
        return jsonify({"status": "error", "message": str(error)})

# ================= Log Import & Export to MongoDB =================
@app.route('/import_log', methods=['POST'])
def import_log():
    if 'file' not in request.files:
        return "কোনো ফাইল সিলেক্ট করা হয়নি", 400
    file = request.files['file']
    if file.filename == '':
        return "কোনো ফাইল পাওয়া যায়নি", 400
    
    if logs_col is None:
        return "ডাটাবেস কানেকশন নেই!", 500
        
    try:
        content = file.read().decode('utf-8', errors='ignore')
        count = 0
        for line in content.splitlines():
            # ফাইলের প্রতিটি লাইন থেকে msg_id, status এবং title বের করে MongoDB-তে সেভ করা
            match = re.search(r'msg_id:(\d+)\s*\|\s*([^|]+)\s*\|\s*(.*)', line)
            if match:
                msg_id = int(match.group(1))
                status = match.group(2).strip()
                title = match.group(3).strip()
                
                logs_col.update_one(
                    {"msg_id": msg_id},
                    {"$set": {
                        "msg_id": msg_id, 
                        "title": title, 
                        "status": status, 
                        "timestamp": datetime.now()
                    }},
                    upsert=True
                )
                count += 1
                
        add_live_log(f"📥 সফলভাবে {count}টি লগ ফাইল থেকে MongoDB-তে ইম্পোর্ট হয়েছে!")
        return "<script>alert('Log Imported to Database Successfully!'); window.location='/';</script>"
    except Exception as e:
        return f"লগ ইম্পোর্ট করতে সমস্যা হয়েছে: {e}", 500


@app.route('/export_log', methods=['GET'])
def export_log():
    if logs_col is None:
        return "ডাটাবেস কানেকশন নেই!", 500
        
    try:
        # কোনো লোকাল ফাইল না বানিয়ে সরাসরি মেমোরি থেকে ডাটা ডাউনলোড করানো
        records = logs_col.find({})
        output = io.StringIO()
        for r in records:
            ts = r.get('timestamp', '')
            msg_id = r.get('msg_id', '')
            status = r.get('status', '')
            title = r.get('title', '')
            output.write(f"[{ts}] msg_id:{msg_id} | {status} | {title}\n")
        
        mem = io.BytesIO()
        mem.write(output.getvalue().encode('utf-8'))
        mem.seek(0)
        output.close()
        
        return send_file(
            mem,
            mimetype='text/plain',
            as_attachment=True,
            download_name='upload_log.txt'
        )
    except Exception as e:
        return f"Error exporting logs: {e}", 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Server is starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
