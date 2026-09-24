import asyncio
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- تنظیمات اولیه ---
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError(
        "لطفاً TELEGRAM_BOT_TOKEN و GEMINI_API_KEY را در تنظیمات وارد کن."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# اتصال به جمنای
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ذخیره جلسات چت کاربران
user_sessions: dict[int, any] = {}
waiting_for_image_prompt: set[int] = set()

BTN_NEW_IMAGE = "🎨 ساخت عکس"
BTN_RESET = "🔄 شروع گفتگوی جدید"
BTN_HELP = "❓ راهنما"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_NEW_IMAGE)],
        [KeyboardButton(BTN_RESET), KeyboardButton(BTN_HELP)],
    ],
    resize_keyboard=True,
)

SYSTEM_INSTRUCTION = (
    "تو یک دستیار هوش مصنوعی فوق‌العاده باهوش، دقیق، خوش‌اخلاق و حرفه‌ای هستی. "
    "پاسخ‌های تو باید کاملاً جامع، مفید، خوانا و با جزئیات کافی باشند. "
    "از ایموجی‌های مناسب استفاده کن و متن را با ساختار مشخص و تیتربندی بنویس."
)


# --- سرور سلامت برای Render ---
def start_health_server():
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("ربات فعال است ✅".encode("utf-8"))

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"سرور سلامت روی پورت {port} بالا آمد.")
    server.serve_forever()


def get_or_create_chat(user_id: int):
    """ایجاد یا دریافت جلسه چت کاربر"""
    if user_id not in user_sessions:
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.7,
        )
        # استفاده از مدل رسمی و پایدار gemini-2.5-flash
        user_sessions[user_id] = ai_client.chats.create(
            model="gemini-2.5-flash",
            config=config,
        )
    return user_sessions[user_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    waiting_for_image_prompt.discard(user_id)
    
    await update.message.reply_text(
        "سلام! 👋\n"
        "من یک دستیار هوش مصنوعی هوشمند و پیشرفته هستم.\n\n"
        "• هر سوالی داری بپرس تا کامل و باجزئیات پاسخت رو بدم.\n"
        "• حافظه چت فعاله و موضوعات قبلی رو به خاطر می‌سپارم.\n"
        "• با دکمه‌های پایین می‌تونی عکس بسازی یا گفتگوت رو ریست کنی.",
        reply_markup=MAIN_KEYBOARD,
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنمای استفاده:\n\n"
        f"• {BTN_NEW_IMAGE} → توصیف عکس رو بنویس تا با کیفیت بالا ساخته بشه.\n"
        f"• {BTN_RESET} → حافظه چت پاک میشه و گفتگو از ابتدا شروع میشه.\n"
        "• ارسال متن معمولی → گفتگو با هوش مصنوعی.",
        reply_markup=MAIN_KEYBOARD,
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "حافظه گفتگو کاملاً پاک شد! آماده شروع موضوع جدید هستیم 🔄",
        reply_markup=MAIN_KEYBOARD,
    )


async def ask_for_image_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_for_image_prompt.add(user_id)
    await update.message.reply_text(
        "توضیح عکسی که می‌خوای رو با جزئیات بنویس (فارسی یا انگلیسی):\n"
        "مثال: یک ماشین اسپرت مدرن سرخ‌رنگ در حال حرکت در جاده برفی"
    )


def translate_prompt_to_english(text: str) -> str:
    """ترجمه و ارتقای توصیف تصویر به انگلیسی"""
    try:
        instruction = f"Enhance and translate this image prompt into a clear English prompt for AI image generator. Return ONLY the English prompt: {text}"
        res = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=instruction,
        )
        return res.text.strip()
    except Exception as e:
        logger.warning(f"Translation error: {e}")
        return text


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text("🎨 در حال پردازش و ساخت تصویر...")

    try:
        english_prompt = translate_prompt_to_english(prompt)
        encoded_prompt = urllib.parse.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&model=flux"

        await update.message.reply_photo(
            photo=image_url,
            caption=f"🖼️ **درخواست شما:** {prompt}",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as e:
        logger.exception("خطا در ساخت عکس")
        await update.message.reply_text(
            f"مشکلی در ساخت عکس پیش آمد:\n{e}", reply_markup=MAIN_KEYBOARD
        )
    finally:
        await status_msg.delete()


async def chat_with_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    user_id = update.effective_user.id
    await update.message.chat.send_action(ChatAction.TYPING)

    # مدل‌هایی که در صورت شلوغی سرور به ترتیب امتحان می‌شوند
    models_to_try = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"]
    
    for model_name in models_to_try:
        try:
            chat = get_or_create_chat(user_id)
            # اگر مدل تغییر کند، مدل چت هم به‌روز می‌شود
            chat._model = model_name
            
            response = chat.send_message(user_text)
            if response and response.text:
                await update.message.reply_text(response.text.strip(), reply_markup=MAIN_KEYBOARD)
                return
        except Exception as e:
            logger.warning(f"Error on model {model_name}: {e}")
            await asyncio.sleep(1)

    await update.message.reply_text(
        "در حال حاضر سرورهای جمنای با ترافیک بالا مواجه شده‌اند. لطفاً چند لحظه بعد مجدداً پیام دهید.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text == BTN_NEW_IMAGE:
        await ask_for_image_prompt(update, context)
        return

    if text == BTN_RESET:
        await reset(update, context)
        return

    if text == BTN_HELP:
        await show_help(update, context)
        return

    if user_id in waiting_for_image_prompt:
        waiting_for_image_prompt.discard(user_id)
        await generate_image(update, context, text)
        return

    await chat_with_gemini(update, context, text)


async def main_async():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    logger.info("ربات در حال اجراست...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
