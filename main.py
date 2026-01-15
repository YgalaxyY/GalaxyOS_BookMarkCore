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
from aiohttp import web
from github import Github, Auth
from huggingface_hub import InferenceClient
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# --- 1. НАСТРОЙКИ И ОКРУЖЕНИЕ ---
load_dotenv()

def safe_log(text):
    """Логирование в консоль Render"""
    try: print(f"[LOG] {text}")
    except Exception: pass

TG_TOKEN = os.getenv("TG_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
REPO_NAME = "YgalaxyY/BookMarkCore"
FILE_PATH = "index.html"

# Каскад моделей. Если первая тупит, пробуем следующую.
AI_MODELS_QUEUE = [
    "Qwen/Qwen2.5-72B-Instruct",             # Топ логика
    "meta-llama/Llama-3.3-70B-Instruct",     # Мощная, но популярная
    "meta-llama/Meta-Llama-3.1-8B-Instruct", # Быстрая
    "mistralai/Mistral-Nemo-Instruct-2407"   # Резерв
]

# Проверка токенов
if not all([TG_TOKEN, GITHUB_TOKEN, HF_TOKEN]):
    safe_log("⚠️ Warning: Tokens missing via .env (Check Render Environment)")

# Состояния FSM
class ToolForm(StatesGroup):
    wait_link = State()
    confirm_duplicate = State()
    select_category = State()

# Инициализация
bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
auth = Auth.Token(GITHUB_TOKEN)
gh = Github(auth=auth)


# --- 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def extract_url_from_text(text):
    """
    Ищет ссылки. Игнорирует ссылки на сам телеграм (t.me), 
    чтобы не сохранять ссылку на канал вместо инструмента.
    """
    urls = re.findall(r'(https?://[^\s<>")\]]+|www\.[^\s<>")\]]+)', text)
    clean_urls = []
    for u in urls:
        u = u.rstrip(').,;]') # Убираем точки/запятые в конце
        if "t.me" not in u and "telegram.me" not in u:
            clean_urls.append(u)
    return clean_urls[0] if clean_urls else "MISSING"

def clean_and_parse_json(raw_response):
    """
    Очищает ответ ИИ от Markdown, лишних запятых и парсит JSON.
    """
    text = raw_response.strip()
    
    # 1. Если ответ обернут в ```json ... ```, достаем внутренности
    json_block = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_block:
        text = json_block.group(1)
    else:
        # Иначе ищем от первой { до последней }
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            text = text[start:end+1]

    # 2. Чистим типичные ошибки LLM (висячие запятые)
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)

    # 3. Парсим
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass 
    
    # 4. Fallback: пробуем через Python AST (если кавычки одинарные)
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


# --- 3. МОЗГИ БОТА (ЭВРИСТИКА + ИИ) ---

def heuristic_analysis(text):
    """
    Быстрый анализ БЕЗ нейросети.
    Используется для 100% промптов или как запасной вариант.
    """
    # Список маркеров промпта (Русский и Английский)
    prompt_markers = [
        '<Role>', '<System>', '<Context>', '<Instructions>', '<Output_Format>',
        '<Роль>', '<Система>', '<Контекст>', '<Инструкции>', 
        'Act as a', 'You are a', 'Представь, что ты', 
        'Напиши промпт', 'System prompt:', 'Промт:', 'Prompt:', 
        'Напиши код', 'Write code'
    ]
    
    # Если найден маркер промпта — это ПРОМПТ.
    if any(marker in text for marker in prompt_markers):
        safe_log("⚡ Heuristic detected a PROMPT. Skipping AI.")
        
        # Пытаемся найти начало промпта, чтобы отрезать вступление
        start_idx = len(text)
        for marker in prompt_markers:
            idx = text.find(marker)
            if idx != -1 and idx < start_idx:
                start_idx = idx
        
        # Если нашли начало, берем текст оттуда. Если нет - весь текст.
        prompt_body = text[start_idx:].strip() if start_idx < len(text) else text
        
        # Генерируем заголовок из первой строки
        lines = text.split('\n')
        title = "AI Prompt"
        for line in lines:
            if len(line.strip()) > 10 and "http" not in line:
                title = line.strip()[:60] + "..."
                break

        return {
            "section": "prompts",
            "name": title,
            "desc": "System Prompt (Auto-detected)",
            "url": "#",
            "platform": "",
            "prompt_body": prompt_body,
            "confidence": 100,
            "alternative": None
        }

    # Если не промпт — возвращаем None, пусть работает ИИ
    return None

def fallback_if_ai_fails(text):
    """
    Если ИИ упал, пытаемся хоть как-то определить категорию.
    """
    safe_log("🔧 AI Failed completely. Using Fallback logic.")
    
    url = extract_url_from_text(text)
    lines = text.split('\n')
    title = lines[0][:50] + "..." if lines else "New Resource"

    # Если ссылка на GitHub -> Dev
    if "github.com" in url:
        return {"section": "dev", "name": title, "desc": "GitHub Repo", "url": url, "prompt_body": "", "confidence": 100}
    
    # Иначе -> Ideas
    return {"section": "ideas", "name": title, "desc": text[:100]+"...", "url": url if url != "MISSING" else "#", "prompt_body": "", "confidence": 50}

async def analyze_content_full_cycle(text):
    """
    ГЛАВНЫЙ ЦИКЛ АНАЛИЗА:
    1. Эвристика (проверка на явный промпт).
    2. Каскад нейросетей (Qwen -> Llama...).
    3. Fallback (если все упало).
    """
    
    # Шаг 1: Эвристика
    heuristic_data = heuristic_analysis(text)
    if heuristic_data:
        return heuristic_data

    # Шаг 2: Нейросети
    hard_found_url = extract_url_from_text(text)
    is_url_present = hard_found_url != "MISSING"

    # ФИНАЛЬНЫЙ СИСТЕМНЫЙ ПРОМПТ
    system_prompt = (
        "### ROLE: Galaxy Intelligence Core (Strict Classifier)\n\n"
        "### CATEGORY LOGIC (Check strict order):\n"
        "1. 'osint' (SECURITY): Hacking, exploits, pentesting, privacy, leaks, deanonymization.\n"
        "2. 'prompts' (TEXT INPUTS): The actual text meant to be typed into ChatGPT/Midjourney. (Keywords: 'Act as', 'System:', 'Prompt:').\n"
        "   *ACTION: Copy the prompt text to 'prompt_body'.*\n"
        "3. 'sys' (SYSTEM): Windows/Linux tools, cleaners, ISOs, drivers, terminal commands.\n"
        "4. 'apk' (MOBILE): Apps for Android/iOS.\n"
        "5. 'study' (EDUCATION): Tutorials, research papers, creating presentations/slides, finding citations, university tools.\n"
        "   *Rule: Tools like 'ChatSlide' or 'Gamma' belong here.*\n"
        "6. 'dev' (CODE): Libraries, APIs, Web-builders, VS Code, No-Code tools.\n"
        "7. 'shop' (COMMERCE): Goods, prices.\n"
        "8. 'fun' (LEISURE): Games, movies, entertainment.\n"
        "9. 'ai' (GENERAL AI): News, models, chatbots. (ONLY if not Study/Dev/Prompts).\n"
        "10. 'prog' (SYNTAX): Code snippets.\n"
        "11. 'ideas' (FALLBACK): General notes.\n\n"
        "### OUTPUT JSON:\n"
        "{\n"
        "  \"section\": \"category\",\n"
        "  \"alternative\": \"alt_category_or_none\",\n"
        "  \"confidence\": 90,\n"
        "  \"name\": \"Short English Title\",\n"
        "  \"desc\": \"Summary in Russian\",\n"
        "  \"url\": \"Link or 'none'\",\n"
        "  \"platform\": \"Android/iOS/none\",\n"
        "  \"prompt_body\": \"Full prompt text or 'none'\"\n"
        "}\n"
        "### RULES: Double quotes JSON. No empty fields (use 'none')."
    )

    user_prompt = f"ANALYZE:\n{text[:8000]}\nURL: {hard_found_url}"

    for model_name in AI_MODELS_QUEUE:
        safe_log(f"🤖 Asking: {model_name}...")
        try:
            client = InferenceClient(model=model_name, token=HF_TOKEN)
            # Увеличил таймаут и токены
            response = await asyncio.to_thread(
                client.chat_completion,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                max_tokens=4000,
                temperature=0.1
            )
            content = response.choices[0].message.content.strip()
            data = clean_and_parse_json(content)
            
            if data:
                safe_log(f"✅ Success: {model_name}")
                # Нормализация данных
                ai_url = data.get('url', '')
                if str(ai_url).lower() in ["none", "missing", "", "#"]:
                     data['url'] = hard_found_url if is_url_present else "#"
                
                # Заглушки для пустых полей
                for key in ['platform', 'prompt_body', 'alternative']:
                    if data.get(key) in ['none', None]: data[key] = None
                
                if 'confidence' not in data: data['confidence'] = 100
                return data
            
        except Exception as e:
            safe_log(f"❌ Fail {model_name}: {e}")
            await asyncio.sleep(1) # Даем паузу перед следующей моделью
            continue 

    # Шаг 3: Если всё сломалось
    return fallback_if_ai_fails(text)


# --- 4. ГЕНЕРАЦИЯ HTML ---

def generate_card_html(data):
    s = str(data.get('section', 'ai')).lower()
    
    # Экранирование для безопасности
    name = html.escape(str(data.get('name', 'Resource')))
    url = str(data.get('url', '#'))
    desc = html.escape(str(data.get('desc', 'No description.')))
    # Prompt Body НЕ экранируем полностью, но чистим от </xmp>
    p_body = str(data.get('prompt_body', '')).replace('</xmp>', '')
    platform = html.escape(str(data.get('platform', 'App')))

    # Настройки стилей
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

    # ШАБЛОН ДЛЯ PROMPTS
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
                    <div id="{p_id}-text" class="text-xs text-gray-300 font-mono leading-relaxed whitespace-pre-wrap max-h-40 overflow-y-auto custom-scrollbar"><xmp>{p_body}</xmp></div>
                </div>
                <p class="text-gray-500 text-xs mt-3 italic">{desc}</p>
            </div>
        </div>
        """
    
    # ШАБЛОН ДЛЯ APK
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

    # СТАНДАРТНЫЙ ШАБЛОН
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


# --- 5. ЗАПИСЬ НА GITHUB ---

def sync_push_to_github(data, force=False):
    """Синхронный пуш"""
    try:
        repo = gh.get_repo(REPO_NAME)
        branch = "main" 
        contents = repo.get_contents(FILE_PATH, ref=branch)
        html_content = contents.decoded_content.decode("utf-8")

        target_url = data.get('url', '')
        clean_target = target_url.rstrip('/')
        
        # Проверка дубликатов (если не Force Push)
        if not force and target_url and target_url not in ["#", "MISSING"] and (clean_target in html_content):
            safe_log(f"Duplicate found: {target_url}")
            return "DUPLICATE"

        sec_key = str(data.get('section', 'ai')).upper()
        target_marker = f"<!-- INSERT_{sec_key}_HERE -->"
        
        if target_marker not in html_content:
            return "MARKER_ERROR"

        new_card = generate_card_html(data)
        new_html = html_content.replace(target_marker, f"{new_card}\n{target_marker}")

        commit_msg = f"Add: {data.get('name')} [{sec_key}] via GalaxyBot"
        repo.update_file(contents.path, commit_msg, new_html, contents.sha, branch)
        return "OK"
    except Exception as e:
        safe_log(f"GitHub Push Error: {e}")
        return "GIT_ERROR"


# --- 6. TELEGRAM HANDLERS ---

@dp.callback_query(F.data.startswith("cat_"), ToolForm.select_category)
async def process_category_selection(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора категории (при сомнениях)"""
    selected_cat = callback.data.split("_")[1]
    state_data = await state.get_data()
    tool_data = state_data.get('tool_data')
    
    if not tool_data:
        await callback.message.edit_text("❌ Данные устарели.")
        await state.clear()
        return

    tool_data['section'] = selected_cat
    await callback.message.edit_text(f"👌 Выбрано: **{selected_cat.upper()}**. Загружаю...")
    
    result = await asyncio.to_thread(sync_push_to_github, tool_data)
    if result == "OK": await callback.message.edit_text(f"✅ Добавлено в `{selected_cat.upper()}`!")
    else: await callback.message.edit_text(f"❌ Ошибка (код: {result}).")
    await state.clear()

@dp.callback_query(F.data.in_({"dup_yes", "dup_no"}), ToolForm.confirm_duplicate)
async def process_duplicate_decision(callback: types.CallbackQuery, state: FSMContext):
    """Обработка дубликатов (Да/Нет)"""
    state_data = await state.get_data()
    tool_data = state_data.get('tool_data')
    if not tool_data:
        await callback.message.edit_text("❌ Данные устарели.")
        await state.clear()
        return

    if callback.data == "dup_no":
        await callback.message.edit_text("🙅‍♂️ Отмена.")
        await state.clear()
    else:
        await callback.message.edit_text("🚀 Force Push...")
        result = await asyncio.to_thread(sync_push_to_github, tool_data, force=True)
        if result == "OK": await callback.message.edit_text(f"✅ Добавлено (Force)!")
        else: await callback.message.edit_text(f"❌ Ошибка.")
        await state.clear()

@dp.message(ToolForm.wait_link)
async def manual_link_handler(message: types.Message, state: FSMContext):
    """Обработка ручного ввода ссылки"""
    state_data = await state.get_data()
    if 'tool_data' not in state_data:
        await message.answer("❌ Данные потеряны.")
        await state.clear()
        return

    user_link = message.text.strip()
    # Берем старые данные, просто добавляем ссылку
    tool_data = state_data['tool_data']
    tool_data['url'] = "#" if user_link == "#" else user_link

    status = await message.answer(f"🔗 Ссылка принята. Деплою **{tool_data['name']}**...")
    result = await asyncio.to_thread(sync_push_to_github, tool_data)
    
    if result == "OK":
        await status.edit_text(f"✅ **{tool_data['name']}** успешно добавлен!")
        await state.clear()
    elif result == "DUPLICATE":
        # Если дубликат даже с новой ссылкой
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Добавить", callback_data="dup_yes")],
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data="dup_no")]
        ])
        await state.update_data(tool_data=tool_data)
        await state.set_state(ToolForm.confirm_duplicate)
        await status.edit_text(f"⚠️ Дубликат! Добавить?", reply_markup=keyboard)
    else:
        await status.edit_text(f"❌ Ошибка.")
        await state.clear()

@dp.message(StateFilter(None), F.text | F.caption)
async def main_content_handler(message: types.Message, state: FSMContext):
    """ГЛАВНЫЙ ОБРАБОТЧИК"""
    content = message.text or message.caption or ""
    if len(content.strip()) < 5: return

    status = await message.answer("🧠 Galaxy AI: Анализ...")
    
    # ЗАПУСКАЕМ ПОЛНЫЙ ЦИКЛ (Эвристика -> ИИ -> Fallback)
    data = await analyze_content_full_cycle(content)

    if not data:
        await status.edit_text("❌ Критическая ошибка анализа.")
        return

    section = str(data.get('section', 'ai')).lower()
    confidence = data.get('confidence', 100)
    alt_section = data.get('alternative')
    name = data.get('name', 'Unknown')
    url = str(data.get('url', ''))
    
    # 1. Если ИИ сомневается
    if confidence < 80 and alt_section and alt_section != section:
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [
                types.InlineKeyboardButton(text=f"📂 {section.upper()}", callback_data=f"cat_{section}"),
                types.InlineKeyboardButton(text=f"📂 {alt_section.upper()}", callback_data=f"cat_{alt_section}")
            ],
            [types.InlineKeyboardButton(text="❌ Отмена", callback_data="dup_no")]
        ])
        await state.update_data(tool_data=data)
        await state.set_state(ToolForm.select_category)
        await status.edit_text(f"🤔 **Сомнения** ({confidence}%)\nОбъект: **{name}**", reply_markup=keyboard)
        return

    # 2. Проверка ссылки (нужна ли она)
    is_no_link = section in ['prompts', 'ideas', 'shop', 'fun']
    is_bad = (url in ["MISSING", "", "#", "None"] or "ygalaxyy" in url)

    if not is_no_link and is_bad:
        await state.update_data(tool_data=data)
        await state.set_state(ToolForm.wait_link)
        await status.edit_text(f"🧐 **{name}** [{section.upper()}]\n⚠️ Пришли ссылку.")
    else:
        await status.edit_text(f"🚀 Деплой **{name}**...")
        result = await asyncio.to_thread(sync_push_to_github, data)
        if result == "OK": await status.edit_text(f"✅ Успешно: **{name}**")
        elif result == "DUPLICATE":
            keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="✅ Добавить", callback_data="dup_yes")],
                [types.InlineKeyboardButton(text="❌ Отмена", callback_data="dup_no")]
            ])
            await state.update_data(tool_data=data)
            await state.set_state(ToolForm.confirm_duplicate)
            await status.edit_text(f"⚠️ Дубликат!", reply_markup=keyboard)
        elif result == "MARKER_ERROR": await status.edit_text(f"❌ Нет метки HTML.")
        else: await status.edit_text("❌ Сбой GitHub.")

# --- WEB SERVER ---
async def health_check(request):
    return web.Response(text="Galaxy Bot is Alive!")

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    safe_log(f"🌍 Web server started on port {port}")

async def main():
    safe_log("🚀 GALAXY INTELLIGENCE BOT ONLINE")
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
        except Exception:
            time.sleep(5)