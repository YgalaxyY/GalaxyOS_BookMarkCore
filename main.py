import os
import json
import uuid
import sys
import re
import asyncio
import base64
import html
import time
import ast
from aiohttp import web  # <--- НОВОЕ: Библиотека для веб-сервера (чтобы бот не спал)
from github import Github, Auth
from huggingface_hub import InferenceClient
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# --- LOAD ENVIRONMENT VARIABLES ---
load_dotenv()

def safe_log(text):
    """Безопасный вывод в консоль"""
    try: print(f"[LOG] {text}")
    except Exception: pass

# --- CONFIGURATION ---
TG_TOKEN = os.getenv("TG_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
LLAMA_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
REPO_NAME = "YgalaxyY/BookMarkCore"
FILE_PATH = "index.html"

# --- SYSTEM CHECK ---
if not all([TG_TOKEN, GITHUB_TOKEN, HF_TOKEN]):
    # На Render переменные могут быть в Environment, а не в .env, поэтому просто предупреждаем, а не выходим
    safe_log("⚠️ Внимание: Не все токены найдены в .env (это нормально для облака)")

# --- FSM STATES ---
class ToolForm(StatesGroup):
    wait_link = State()

# --- INITIALIZATION ---
bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
hf_client = InferenceClient(model=LLAMA_MODEL, token=HF_TOKEN)
auth = Auth.Token(GITHUB_TOKEN)
gh = Github(auth=auth)

# --- HELPER FUNCTIONS ---

def extract_url_from_text(text):
    """Ищет самую релевантную ссылку, игнорируя Telegram"""
    urls = re.findall(r'(https?://[^\s<>"]+|www\.[^\s<>"]+)', text)
    clean_urls = []
    for u in urls:
        u = u.rstrip(').,;]')
        if "t.me" not in u and "telegram.me" not in u:
            clean_urls.append(u)
    return clean_urls[0] if clean_urls else "MISSING"

def clean_and_parse_json(raw_response):
    """
    Супер-надежный парсер JSON.
    Умеет чинить ошибки с одинарными кавычками (Python dict).
    """
    text_to_parse = raw_response.strip()
    
    # 1. Попытка найти блок кода ```json ... ```
    json_block = re.search(r'```json\s*(\{.*?\})\s*```', raw_response, re.DOTALL)
    if json_block:
        text_to_parse = json_block.group(1)
    else:
        # 2. Если блока нет, ищем границы { ... }
        start = raw_response.find('{')
        end = raw_response.rfind('}')
        if start != -1 and end != -1:
            text_to_parse = raw_response[start:end+1]

    # 3. Пробуем распарсить как стандартный JSON
    try:
        return json.loads(text_to_parse)
    except json.JSONDecodeError:
        pass 

    # 4. ПЛАН Б: Пробуем распарсить как Python Dictionary
    try:
        return ast.literal_eval(text_to_parse)
    except Exception as e:
        safe_log(f"JSON Parse Failed completely: {e}")
        return None

def analyze_content_smart(text):
    """
    Мозг анализа контента с новой таксономией (11 категорий).
    """
    safe_log("AI Analysis started...")
    
    hard_found_url = extract_url_from_text(text)
    is_url_present = hard_found_url != "MISSING"
    
    system_prompt = (
        "You are 'Galaxy Intelligence' Core. Analyze incoming content and categorize it into JSON.\n"
        "TAXONOMY KEYS: 'ideas', 'prog', 'apk', 'prompts', 'study', 'ai', 'fun', 'shop', 'dev', 'sys', 'osint'.\n"
        "\nCATEGORY DEFINITIONS:\n"
        "1. 'ideas': Abstract thoughts, interesting notes, unclassifiable cool stuff.\n"
        "2. 'prog': Programming languages (Python, JS), IDEs, syntax, code theory.\n"
        "3. 'apk': Android/iOS apps. MUST extract 'platform': 'Android' or 'iOS' or 'Both'.\n"
        "4. 'prompts': Large text prompts for Neural Networks to copy-paste.\n"
        "5. 'study': Education, textbooks, presentations, teachers, lectures, science.\n"
        "6. 'ai': Galaxy Intelligence - General AI news, models, tools (not prompts).\n"
        "7. 'fun': Entertainments - Movies, games, cafes, interesting places, music.\n"
        "8. 'shop': Shopping list, gadgets, cool finds to buy.\n"
        "9. 'dev': Dev Laboratory - Libraries, utilities, production tools, APIs.\n"
        "10. 'sys': System Tuning - Windows/Linux optimization, .exe, fixes, cleaners.\n"
        "11. 'osint': Reconnaissance, people search, data leaks, investigation tools.\n"
        "\nIMPORTANT RULES:\n"
        "- USE DOUBLE QUOTES (\") FOR ALL KEYS AND STRINGS. Do NOT use single quotes.\n"
        "- Output valid JSON ONLY. No markdown, no conversational text.\n"
        "\nOUTPUT FORMAT:\n"
        "{\"section\": \"key_from_above\", \"name\": \"Short Title En\", \"desc\": \"Summary in Russian\", \"url\": \"link\", \"platform\": \"Android/iOS (only for apk)\", \"prompt_body\": \"full text (only for prompts)\"}"
    )

    user_prompt = (
        f"ANALYZE THIS POST:\n{text[:6000]}\n"
        f"HARDWARE SCAN: URL found -> {hard_found_url}\n"
    )

    try:
        response = hf_client.chat_completion(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=2500,
            temperature=0.1
        )
        data = clean_and_parse_json(response.choices[0].message.content.strip())
        
        if not data:
            return None

        # Post-Processing
        ai_url = data.get('url', '')
        if (ai_url in ["MISSING", "", None, "#"]) and is_url_present:
            data['url'] = hard_found_url
        
        section = data.get('section', 'ai').lower()
        if section == 'prompts' and "github.com" in str(data.get('url', '')):
            data['section'] = 'ai' 
            
        return data

    except Exception as e:
        safe_log(f"AI Error: {e}")
        return None

def generate_card_html(data):
    """Генерирует HTML-код карточки"""
    s = str(data.get('section', 'ai')).lower()
    
    name = html.escape(str(data.get('name', 'Resource')))
    url = str(data.get('url', '#'))
    desc = html.escape(str(data.get('desc', 'No description.')))
    p_body = html.escape(str(data.get('prompt_body', '')))
    platform = html.escape(str(data.get('platform', 'App')))

    meta = {
        "ideas":  {"icon": "lightbulb",      "color": "yellow"},
        "fun":    {"icon": "gamepad",        "color": "pink"},
        "shop":   {"icon": "cart-shopping",  "color": "rose"},
        "ai":     {"icon": "robot",          "color": "purple"},
        "prompts":{"icon": "key",            "color": "amber"},
        "study":  {"icon": "graduation-cap", "color": "indigo"},
        "prog":   {"icon": "code",           "color": "blue"},
        "dev":    {"icon": "flask",          "color": "emerald"},
        "apk":    {"icon": "mobile-screen",  "color": "green"},
        "sys":    {"icon": "microchip",      "color": "cyan"},
        "osint":  {"icon": "eye",            "color": "red"},
    }
    
    style = meta.get(s, meta["ai"])
    color = style["color"]
    icon = style["icon"]

    if s == 'prompts':
        p_id = f"p-{uuid.uuid4().hex[:6]}"
        return f"""
        <div class="glass-card p-8 rounded-[2rem] border-l-4 border-{color}-500 mb-6 reveal active relative overflow-hidden group">
            <div class="absolute top-0 right-0 p-4 opacity-10 group-hover:opacity-20 transition-opacity">
                <i class="fas fa-{icon} text-6xl text-{color}-500"></i>
            </div>
            <div class="relative z-10">
                <div class="flex justify-between items-center mb-4">
                    <div>
                        <span class="text-[9px] font-black text-{color}-400 tracking-widest uppercase">AI PROMPT</span>
                        <h3 class="text-xl font-bold text-white mt-1">{name}</h3>
                    </div>
                    <button onclick="copyToClipboard('{p_id}-text')" class="bg-white/5 hover:bg-{color}-500/20 border border-white/10 px-4 py-2 rounded-xl text-xs font-bold transition-all flex items-center gap-2">
                        <i class="fas fa-copy"></i> Copy
                    </button>
                </div>
                <div class="bg-black/30 rounded-xl p-4 border border-white/5">
                    <div id="{p_id}-text" class="text-xs text-gray-300 font-mono leading-relaxed whitespace-pre-wrap max-h-40 overflow-y-auto custom-scrollbar">{p_body}</div>
                </div>
                <p class="text-gray-500 text-xs mt-3 italic">{desc}</p>
            </div>
        </div>
        """
    
    if s == 'apk':
        return f"""
        <div class="glass-card p-8 rounded-[2rem] hover:bg-white/5 transition-all duration-300 reveal active border-t border-white/5 mb-6">
            <div class="flex items-start gap-4">
                <div class="w-12 h-12 rounded-2xl bg-{color}-500/10 flex items-center justify-center shrink-0 border border-{color}-500/20">
                    <i class="fas fa-{icon} text-{color}-400 text-lg"></i>
                </div>
                <div class="flex-1">
                    <div class="flex justify-between items-start">
                        <h3 class="text-lg font-bold text-gray-100 leading-tight mb-2">{name}</h3>
                        <span class="text-[9px] font-bold bg-{color}-500 text-black px-2 py-0.5 rounded uppercase tracking-wider">{platform}</span>
                    </div>
                    <p class="text-sm text-gray-400 leading-relaxed mb-4">{desc}</p>
                    <a href="{url}" target="_blank" class="inline-flex items-center gap-2 text-xs font-bold text-white hover:text-{color}-400 transition-colors group">
                        DOWNLOAD <i class="fas fa-download group-hover:translate-y-1 transition-transform"></i>
                    </a>
                </div>
            </div>
        </div>
        """

    return f"""
    <div class="glass-card p-8 rounded-[2rem] hover:bg-white/5 transition-all duration-300 reveal active border-t border-white/5 mb-6">
        <div class="flex items-start gap-4">
            <div class="w-12 h-12 rounded-2xl bg-{color}-500/10 flex items-center justify-center shrink-0 border border-{color}-500/20">
                <i class="fas fa-{icon} text-{color}-400 text-lg"></i>
            </div>
            <div class="flex-1">
                <div class="flex justify-between items-start">
                    <h3 class="text-lg font-bold text-gray-100 leading-tight mb-2">{name}</h3>
                    <span class="text-[9px] font-bold bg-{color}-500/20 text-{color}-300 px-2 py-1 rounded uppercase tracking-wider">{s}</span>
                </div>
                <p class="text-sm text-gray-400 leading-relaxed mb-4">{desc}</p>
                <a href="{url}" target="_blank" class="inline-flex items-center gap-2 text-xs font-bold text-white hover:text-{color}-400 transition-colors group">
                    OPEN RESOURCE <i class="fas fa-arrow-right group-hover:translate-x-1 transition-transform"></i>
                </a>
            </div>
        </div>
    </div>
    """

def sync_push_to_github(data):
    """Синхронный пуш на GitHub с защитой от дублей"""
    try:
        repo = gh.get_repo(REPO_NAME)
        branch = "main" 
        
        contents = repo.get_contents(FILE_PATH, ref=branch)
        html_content = contents.decoded_content.decode("utf-8")

        target_url = data.get('url', '')
        if target_url and target_url not in ["#", "MISSING"] and target_url in html_content:
            safe_log(f"Duplicate URL: {target_url}")
            return "DUPLICATE"

        sec_key = str(data.get('section', 'ai')).upper()
        target_marker = f"<!-- INSERT_{sec_key}_HERE -->"
        
        if target_marker not in html_content:
            safe_log(f"Marker {target_marker} NOT found in HTML!")
            return "MARKER_ERROR"

        new_card = generate_card_html(data)
        new_html = html_content.replace(target_marker, f"{new_card}\n{target_marker}")

        commit_msg = f"Add: {data.get('name')} [{sec_key}] via GalaxyBot"
        
        repo.update_file(
            path=contents.path,
            message=commit_msg,
            content=new_html,
            sha=contents.sha,
            branch=branch
        )
        return "OK"
    except Exception as e:
        safe_log(f"GitHub Push Error: {e}")
        return "GIT_ERROR"

# --- TELEGRAM HANDLERS ---

@dp.message(ToolForm.wait_link)
async def manual_link_handler(message: types.Message, state: FSMContext):
    state_data = await state.get_data()
    if 'tool_data' not in state_data:
        await message.answer("❌ Данные потеряны. Начни заново.")
        await state.clear()
        return

    user_link = message.text.strip()
    tool_data = state_data['tool_data']
    tool_data['url'] = "#" if user_link == "#" else user_link

    await state.clear()
    status = await message.answer("🔄 Обновляю базу данных Galaxy...")
    
    result = await asyncio.to_thread(sync_push_to_github, tool_data)
    
    if result == "OK":
        await status.edit_text(f"✅ **{tool_data['name']}** успешно добавлен в систему!")
    elif result == "DUPLICATE":
        await status.edit_text(f"⚠️ **{tool_data['name']}** уже существует!")
    else:
        await status.edit_text(f"❌ Ошибка системы (код: {result}).")

@dp.message(StateFilter(None), F.text | F.caption)
async def main_content_handler(message: types.Message, state: FSMContext):
    content = message.text or message.caption or ""
    
    if len(content.strip()) < 5 or content.startswith('/'):
        return

    safe_log(f"--- INCOMING DATA ---")
    status = await message.answer("🧠 Galaxy Intelligence: Анализирую входящие данные...")
    
    data = await asyncio.to_thread(analyze_content_smart, content)

    if not data:
        await status.edit_text("❌ Ошибка анализа (Невалидный JSON).")
        return

    section = str(data.get('section', 'ai')).lower()
    url = str(data.get('url', ''))
    name = data.get('name', 'Unknown')
    
    is_no_link_category = section in ['prompts', 'ideas', 'shop', 'fun']
    is_bad_url = (url in ["MISSING", "", "#", "None"] or "ygalaxyy" in url)

    if not is_no_link_category and is_bad_url:
        await state.update_data(tool_data=data)
        await state.set_state(ToolForm.wait_link)
        await status.edit_text(
            f"🧐 Объект: **{name}** -> Секция: `{section.upper()}`\n"
            "⚠️ Не обнаружен прямой линк. Отправь ссылку (или #)."
        )
    else:
        await status.edit_text(f"🚀 Интеграция **{name}** в кластер `{section.upper()}`...")
        
        result = await asyncio.to_thread(sync_push_to_github, data)
        
        if result == "OK":
            await status.edit_text(f"✅ Успешная интеграция: **{name}**")
        elif result == "DUPLICATE":
            await status.edit_text(f"🙅‍♂️ Дубликат данных: **{name}**")
        elif result == "MARKER_ERROR":
            await status.edit_text(f"❌ Критическая ошибка: Нет метки `<!-- INSERT_{section.upper()}_HERE -->`")
        else:
            await status.edit_text("❌ Сбой подключения к GitHub.")

# --- WEB SERVER ДЛЯ RENDER (ЧТОБЫ БОТ НЕ СПАЛ) ---
async def health_check(request):
    """Простой ответ на запрос, чтобы сервис считался живым"""
    return web.Response(text="Galaxy Bot is Alive!")

async def start_web_server():
    """Запуск маленького сайта"""
    # Render передает порт через переменную окружения PORT. По умолчанию 8080.
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    safe_log(f"🌍 Web server started on port {port}")

# --- MAIN ENTRY POINT ---
async def main():
    safe_log("🚀 GALAXY INTELLIGENCE BOT ONLINE")
    
    # Сначала запускаем веб-сервер
    await start_web_server()
    
    # Потом запускаем бота
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            safe_log("🛑 System Halt.")
            break
        except Exception as e:
            safe_log(f"🔥 System Failure: {e}")
            time.sleep(5)