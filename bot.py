import asyncio
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from google import genai
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

# ساختار حافظه گفتگوها (تا ۱۰ پیام اخیر)
user_chat_history: dict[int, list] = {}
MAX_HISTORY = 10


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
    "از ایموجی‌های مناسب استفاده کن و متن را با ساختار مشخص (بولتبوینت یا تیتر) بنویس."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_chat_history[user_id] = []
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "سلام! 👋\n"
        "من یک دستیار هوش مصنوعی ارتقایافته و پیشرفته هستم.\n\n"
        "• هر سوالی داری بنویس تا با دقت و جزئیات کامل جوابت رو بدم.\n"
        "• حافظه چت روشنه و موضوعات قبلی یادم می‌مونه.\n"
        "• با دکمه‌های پایین می‌تونی عکس باکیفیت بسازی یا حافظه رو پاک کنی.",
        reply_markup=MAIN_KEYBOARD,
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنمای استفاده:\n\n"
        f"• {BTN_NEW_IMAGE} → توصیف عکسی که می‌خوای رو بنویس تا با کیفیت بالا ساخته شه.\n"
        f"• {BTN_RESET} → حافظه گفتگو پاک میشه و از اول شروع می‌کنیم.\n"
        "• ارسال متن عادی → گفتگو و پرسش‌وپاسخ پیشرفته.",
        reply_markup=MAIN_KEYBOARD,
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_chat_history[user_id] = []
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "حافظه گفتگو کاملاً پاک شد! آماده‌ی موضوع جدید هستیم 🔄",
        reply_markup=MAIN_KEYBOARD,
    )


async def ask_for_image_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_for_image_prompt.add(user_id)
    await update.message.reply_text(
        "توضیح عکسی که می‌خوای رو با جزئیات بنویس (فارسی یا انگلیسی):\n"
        "مثال: یک ماشین اسپرت مدرن سرخ‌رنگ در حال حرکت در جاده برفی با نورپردازی سینمایی"
    )


# ارسال ایمن با قابلیت حفظ تاریخچه و مدیریت شلوغی
def generate_smart_chat_response(user_id: int, new_message: str) -> str:
    if user_id not in user_chat_history:
        user_chat_history[user_id] = []

    # اضافه کردن پیام جدید کاربر به تاریخچه
    history = user_chat_history[user_id]
    history.append({"role": "user", "parts": [new_message]})

    # نگه‌داشتن فقط پیام‌های اخیر
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2) :]
        user_chat_history[user_id] = history

    models_to_try = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-1.5-flash"]

    # ساخت متن شامل دستور سیستم و تاریخچه
    prompt_payload = f"System Instruction: {SYSTEM_INSTRUCTION}\n\n"
    for item in history:
        role_label = "User" if item["role"] == "user" else "Assistant"
        prompt_payload += f"{role_label}: {item['parts'][0]}\n"
    prompt_payload += "Assistant:"

    for model_name in models_to_try:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt_payload,
            )
            if response and response.text:
                reply = response.text.strip()
                # ذخیره پاسخ مدل در تاریخچه
                history.append({"role": "model", "parts": [reply]})
                return reply
        except Exception as e:
            logger.warning(f"Error on model {model_name}: {e}")

    raise RuntimeError("در حال حاضر تمامی سرورها مشغول هستند. دوباره تلاش کن.")


# ترجمه هوشمند پرامپت ساخت عکس
def translate_prompt_to_english(text: str) -> str:
    try:
        instruction = f"Enhance and translate this image prompt into a high-detail English prompt for Flux image generator. Return ONLY the English prompt: {text}"
        res = ai_client.models.generate_content(
            model="gemini-3.6-flash", contents=instruction
        )
        return res.text.strip()
    except Exception:
        return text


# ساخت عکس هوشمند
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text("🎨 در حال پردازش و ارتقای توصیف تصویر...")

    try:
        english_prompt = translate_prompt_to_english(prompt)
        encoded_prompt = urllib.parse.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1280&height=720&nologo=true&model=flux&enhance=true"

        await update.message.reply_photo(
            photo=image_url,
            caption=f"🖼️ **نتیجه ساخت تصویر**\n\n📝 درخواست شما: {prompt}",
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

    try:
        reply_text = generate_smart_chat_response(user_id, user_text)
        await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("خطا در دریافت پاسخ")
        await update.message.reply_text(
            f"مشکلی پیش آمد:\n{e}", reply_markup=MAIN_KEYBOARD
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

    logger.info("ربات هوشمند در حال اجراست...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
