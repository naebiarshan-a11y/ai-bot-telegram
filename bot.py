"""
ربات تلگرام هوش مصنوعی
- پاسخ‌دهی هوشمند به پیام‌های متنی با استفاده از OpenAI (GPT)
- ساخت عکس با دستور /image با استفاده از DALL·E

نحوه اجرا:
1. مقادیر TELEGRAM_BOT_TOKEN و OPENAI_API_KEY را در فایل .env قرار بده
2. pip install -r requirements.txt
3. python bot.py
"""

import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- تنظیمات اولیه ---
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError(
        "لطفاً TELEGRAM_BOT_TOKEN و OPENAI_API_KEY را در فایل .env تنظیم کن."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)


# --- سرور HTTP کوچک، فقط برای اینکه رندر (Render) سرویس را «رایگان + بیدار» نگه دارد ---
# پلتفرم‌هایی مثل UptimeRobot باید به همین آدرس هر ۱۰-۱۴ دقیقه سر بزنند.
def start_health_server():
    port = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("ربات فعال است ✅".encode("utf-8"))

        def log_message(self, format, *args):
            pass  # جلوگیری از شلوغ شدن لاگ‌ها با هر درخواست پینگ

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"سرور سلامت روی پورت {port} بالا آمد.")
    server.serve_forever()

# حافظه ساده برای نگه‌داری چند پیام آخر هر کاربر (اختیاری، در حافظه - با ری‌استارت پاک می‌شود)
conversation_memory: dict[int, list[dict]] = {}
MAX_HISTORY = 10  # تعداد پیام‌های قبلی که نگه داشته می‌شود

# وضعیت هر کاربر: آیا منتظریم پیام بعدی‌اش را به‌عنوان توضیح عکس بفرستد؟
waiting_for_image_prompt: set[int] = set()

# --- متن دکمه‌های منو ---
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


# --- شروع / نمایش منوی اصلی ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "سلام! 👋\n"
        "من یک ربات هوش مصنوعی هستم.\n\n"
        "• هر پیام متنی بفرستی، باهات چت می‌کنم.\n"
        "• با دکمه‌های پایین صفحه هم می‌تونی عکس بسازی یا گفتگو رو از نو شروع کنی.",
        reply_markup=MAIN_KEYBOARD,
    )


# --- راهنما ---
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنمای استفاده:\n\n"
        f"• {BTN_NEW_IMAGE} → بعد از زدنش، توضیح عکسی که می‌خوای رو بنویس.\n"
        f"• {BTN_RESET} → حافظه‌ی گفتگو پاک می‌شه و از اول شروع می‌کنیم.\n"
        "• هر پیام دیگه‌ای که بفرستی، به‌عنوان یک پیام چت عادی جواب داده می‌شه.",
        reply_markup=MAIN_KEYBOARD,
    )


# --- پاک کردن حافظه گفتگو ---
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversation_memory.pop(user_id, None)
    waiting_for_image_prompt.discard(user_id)
    await update.message.reply_text(
        "حافظه گفتگو پاک شد. می‌تونیم از اول شروع کنیم!", reply_markup=MAIN_KEYBOARD
    )


# --- وقتی کاربر دکمه «ساخت عکس» را می‌زند ---
async def ask_for_image_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    waiting_for_image_prompt.add(user_id)
    await update.message.reply_text(
        "بسیار خب! توضیح بده چه عکسی می‌خوای بسازم.\n"
        "مثال: یک گربه فضانورد روی ماه، سبک نقاشی دیجیتال"
    )


# --- ساخت واقعی عکس با DALL·E ---
async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    status_msg = await update.message.reply_text("🎨 در حال ساخت عکس...")

    try:
        result = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        image_url = result.data[0].url
        await update.message.reply_photo(
            photo=image_url, caption=f"🖼️ {prompt}", reply_markup=MAIN_KEYBOARD
        )
    except Exception as e:
        logger.exception("خطا در ساخت عکس")
        await update.message.reply_text(
            f"متاسفانه در ساخت عکس مشکلی پیش اومد:\n{e}", reply_markup=MAIN_KEYBOARD
        )
    finally:
        await status_msg.delete()


# --- پاسخ‌دهی چت معمولی با GPT ---
async def chat_with_gpt(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    user_id = update.effective_user.id
    await update.message.chat.send_action(ChatAction.TYPING)

    history = conversation_memory.get(user_id, [])
    history.append({"role": "user", "content": user_text})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "تو یک دستیار هوشمند و مفید در تلگرام هستی. پاسخ‌ها را کوتاه، دوستانه و به همان زبانی که کاربر می‌نویسد بده.",
                },
                *history,
            ],
        )
        reply_text = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply_text})

        conversation_memory[user_id] = history[-MAX_HISTORY:]

        await update.message.reply_text(reply_text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("خطا در دریافت پاسخ از OpenAI")
        await update.message.reply_text(
            f"متاسفانه مشکلی پیش اومد:\n{e}", reply_markup=MAIN_KEYBOARD
        )


# --- مسیریابی همه پیام‌های متنی (دکمه‌ها + چت + توضیح عکس) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ۱. بررسی دکمه‌های منو
    if text == BTN_NEW_IMAGE:
        await ask_for_image_prompt(update, context)
        return

    if text == BTN_RESET:
        await reset(update, context)
        return

    if text == BTN_HELP:
        await show_help(update, context)
        return

    # ۲. اگر منتظر توضیح عکس بودیم، این پیام را به‌عنوان پرامپت عکس بفرست
    if user_id in waiting_for_image_prompt:
        waiting_for_image_prompt.discard(user_id)
        await generate_image(update, context, text)
        return

    # ۳. در غیر این صورت، پیام چت عادی است
    await chat_with_gpt(update, context, text)


def main():
    # سرور سلامت را در یک ترد جدا اجرا می‌کنیم تا رندر سرویس را «وب‌سرویس فعال» تشخیص بدهد
    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
