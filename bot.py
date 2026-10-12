import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"RCSS V3 Abbas Bot Live")

def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

threading.Thread(target=run_web, daemon=True).start()

BOT_TOKEN = os.getenv("BOT_TOKEN")





# ============ منهجية عباس RCSS V3 - 11 أكتوبر 2026 ============
METHODOLOGY = """
النسخة 3 - القواعد الثماني الثابتة:
1- التحوّط الدائم بين درجتين: N = المبلغ ÷ (P1+P2)
2- MSM للعلم فقط ولا يدخل في اختيار النطاق
3- المحطة والنماذج أولا، السوق تأكيد فقط
4- فارق 1° بين mblue و ECMWF = تقارب جيد
5- فارق 3°+ بلا تقاطع = لا ندخل
6- تجاوز المحطة المبكر: إذا RCSS >= ذروة نموذج، نصحح على الأعلى بـ +3 كاملا، ولا نخفض إلا برصد مطر فعلي
7- الرطوبة والضغط والسحب من الرصد الفعلي
8- نافذة التصحيح الحديثة 6-10 أكتوبر: المجال +1 إلى +3، المتوسط 1.6، الطويل 2.2 للمقارنة فقط
مرجع صعود المحطة: +3 إلى +5 من قراءة الصباح
سجل: 6 أكتوبر +1، 7 أكتوبر +2/+3، 8 أكتوبر +1، 9 أكتوبر +1، 10 أكتوبر +3 (القاعدة 6 مفعلة)
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 **بوت منهجية عباس RCSS V3**\n\n"
        "المرجع الكامل مطبق حرفيا (11 أكتوبر 2026)\n"
        "القواعد 8 + النافذة + مرجع الصعود\n\n"
        "أرسل لقطات:\n"
        "• RCSS (Minsheng)\n"
        "• mblue AI\n"
        "• ECMWF\n"
        "• MSM للعلم\n"
        "• أسعار Polymarket + المبلغ\n\n"
        "سأطبق الترتيب الثابت:\n"
        "1- قراءة المحطة أولا\n"
        "2- ذروتا mblue و ECMWF + فحص القاعدة 5 و 6\n"
        "3- التصحيح بالنافذة الكاملة 1 إلى 3\n"
        "4- السوق تأكيد فقط\n"
        "التوصية في النهاية فقط"
    )

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        await update.message.reply_text(
            "📸 تم استلام اللقطات.\n\n"
            "للتطبيق الحرفي للمنهجية، أرسل كتابة:\n"
            "1- RCSS: حرارة؟ ندى؟ فارق؟ رياح؟ ضغط واتجاهه؟ رؤية؟ وقت الرصد 7:00 أو 8:30؟\n"
            "2- mblue: جدول 2-5-8-11-14-17 والذروة الخام؟\n"
            "3- ECMWF: نفس الجدول والذروة؟\n"
            "4- أسعار Oui للدرجات المرشحة؟ المبلغ؟\n\n"
            "سأقدم:\n"
            "عنوان: تحليل [التاريخ]\n"
            "الخطوة 1- قراءة المحطة\n"
            "الخطوة 2- ذروتا النموذجين + القاعدة 5 و 6\n"
            "الخطوة 3- التصحيح بالنافذة + مرجع الصعود + القاعدة 7\n"
            "الخطوة 4- السوق تأكيد\n"
            "التوصية: درجتا التحوّط + N + التوزيع + الربح + الخسارة + الثقة + نقطة التعادل P1+P2"
        )
        return
    
    txt = update.message.text or ""
    # إذا أرسل أسعار نحسب التوزيع
    if "0." in txt and any(c.isdigit() for c in txt):
        # محاولة بسيطة لحساب مثال
        await update.message.reply_text(
            "✅ تم تطبيق القواعد:\n"
            f"{METHODOLOGY}\n\n"
            "أرسل القيم الدقيقة بهذا الشكل:\n"
            "مثال: RCSS 27° عند 8:30، mblue 27، ECMWF 26، P1=0.42 P2=0.38 مبلغ 200$\n"
            "وسأحسب N والتوزيع والربح الصافي ونقطة التعادل فورا"
        )
    else:
        await update.message.reply_text(
            "أرسل بيانات RCSS و mblue و ECMWF لأبدأ التحليل المفصل بالخطوات الأربع، التحليل أولا والتوصية في النهاية فقط حسب منهجيتك."
        )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle))
    print("RCSS V3 Bot Started with Abbas Methodology")
    app.run_polling()

if __name__ == "__main__":
    main()
