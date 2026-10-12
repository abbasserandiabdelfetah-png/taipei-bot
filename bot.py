import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلا بك في بوت تايبيه! 🇹🇼\nاسألني عن برج 101، الأكل، الأسواق")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "101" in text or "برج" in text:
        msg = "🗼 برج Taipei 101: 508 متر، أجمل مكان في تايبيه!"
    elif "اكل" in text or "مطعم" in text:
        msg = "🍜 أكل تايبيه: Beef Noodle, Xiao Long Bao, Bubble Tea في Shilin"
    elif "سوق" in text:
        msg = "🌃 Shilin Night Market الأكبر، يفتح 6 مساء"
    else:
        msg = f"تايبيه رائعة! سؤالك: {update.message.text}"
    await update.message.reply_text(msg)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()

if __name__ == "__main__":
    main()
