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


conversation_memory: dict[int, list] = {}
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "سلام! 👋\n"
        "من یک ربات هوش مصنوعی (قدرت‌گرفته از Gemini) هستم.\n\n"
        "• هر پیام متنی بفرستی، باهات چت می‌کنم.\n"
        "• با دکمه‌های پایین هم می‌تونی عکس رایگان بسازی یا چت رو ریست کنی.",
        reply_markup=MAIN_KEYBOARD,
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنمای استفاده:\n\n"
        f"• {BTN_NEW_IMAGE} → توصیف عکسی که می‌خوای رو بنویس.\n"
        f"• {BTN_RESET} → حافظه چت پاک میشه.\n"
        "• پیام عادی → چت با Gemini.",
        reply_markup=MAIN_KEYBOARD,
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_memory.pop(user_id, None)
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "حافظه گفتگو پاک شد!", reply_markup=MAIN_KEYBOARD
    )


async def ask_for_image_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_for_image_prompt.add(user_id)
    await update.message.reply_text(
        "توضیح عکسی که می‌خوای رو بنویس (می‌تونی فارسی یا انگلیسی بنویسی):\n"
        "مثال: یک توپ فوتبال روی چمن"
    )


# ترجمه خودکار متن عکس به انگلیسی جهت جلوگیری از اشتباه در تصویرسازی
def translate_prompt_to_english(text: str) -> str:
    try:
        prompt_instruction = f"Translate the following image description to a precise, clear English prompt for AI image generation. Output ONLY the English translation, nothing else: {text}"
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt_instruction,
        )
        return response.text.strip()
    except Exception as e:
        logger.warning(f"Translation failed, using raw prompt: {e}")
        return text


# ساخت عکس رایگان با موتور Pollinations
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text("🎨 در حال ترجمه و ساخت عکس...")

    try:
        # ترجمه پرامپت فارسی به انگلیسی توسط جمنای
        english_prompt = translate_prompt_to_english(prompt)
        logger.info(f"Original: {prompt} -> English: {english_prompt}")

        encoded_prompt = urllib.parse.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&model=flux"

        await update.message.reply_photo(
            photo=image_url, caption=f"🖼️ {prompt}", reply_markup=MAIN_KEYBOARD
        )
    except Exception as e:
        logger.exception("خطا در ساخت عکس")
        await update.message.reply_text(
            f"مشکلی در ساخت عکس پیش آمد:\n{e}", reply_markup=MAIN_KEYBOARD
        )
    finally:
        await status_msg.delete()


# چت رایگان با Gemini
async def chat_with_gemini(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=user_text,
        )
        reply_text = response.text
        await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("خطا در دریافت پاسخ از Gemini")
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

    logger.info("ربات در حال اجراست...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # زنده نگه داشتن ربات
    await asyncio.Event().wait()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
