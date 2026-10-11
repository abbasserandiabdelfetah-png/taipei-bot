"""Telegram image analyst for daily maximum-temperature markets at Taipei RCSS."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv
from PIL import Image, ImageOps
from telegram import Message, Update
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


ROOT_DIR = Path(__file__).resolve().parent
METHODOLOGY_PATH = ROOT_DIR / "methodology_prompt.md"
CORRECTIONS_PATH = ROOT_DIR / "data" / "corrections.json"

ALBUM_MIN_IMAGE_COUNT = 4
ALBUM_MAX_IMAGE_COUNT = 5
ALBUM_SETTLE_SECONDS = 2.0
DEFAULT_AMOUNT_USD = Decimal("200")
MAX_IMAGE_EDGE = 1800
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

AMOUNT_PATTERN = re.compile(
    r"(?:(?P<before>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\s*(?:\$|USD)"
    r"|(?:\$|USD)\s*(?P<after>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?))",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(r"^-?\d+(?:[.,]\d+)?$")
TAIPEI_TZ = timezone(timedelta(hours=8), name="Asia/Taipei")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("taipei-weather-bot")

_anthropic_client: anthropic.AsyncAnthropic | None = None
_corrections_lock = asyncio.Lock()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"متغير البيئة {name} مطلوب.")
    return value


def _parse_amount(caption: str | None) -> Decimal:
    """Read a USD amount from a caption; otherwise use the requested default."""
    if not caption:
        return DEFAULT_AMOUNT_USD

    match = AMOUNT_PATTERN.search(caption)
    if match:
        raw_amount = (
            match.group("before") or match.group("after") or ""
        ).replace(",", "")
    else:
        bare_amount = caption.strip()
        if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", bare_amount):
            return DEFAULT_AMOUNT_USD
        raw_amount = bare_amount.replace(",", ".")
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        return DEFAULT_AMOUNT_USD
    if not amount.is_finite() or amount <= 0:
        return DEFAULT_AMOUNT_USD
    return amount


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _telegram_chunks(text: str) -> list[str]:
    """Split long model responses at paragraph boundaries when possible."""
    remaining = text.strip()
    chunks: list[str] = []
    while len(remaining) > MAX_TELEGRAM_MESSAGE_LENGTH:
        boundary = remaining.rfind("\n", 0, MAX_TELEGRAM_MESSAGE_LENGTH)
        if boundary < MAX_TELEGRAM_MESSAGE_LENGTH // 2:
            boundary = MAX_TELEGRAM_MESSAGE_LENGTH
        chunks.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_long_message(
    application: Application, chat_id: int, text: str
) -> None:
    for chunk in _telegram_chunks(text):
        await application.bot.send_message(chat_id=chat_id, text=chunk)


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and user.id == context.application.bot_data["allowed_user_id"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context) or update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "أرسل ألبومًا واحدًا يضم أربع صور أساسية: توقعَي mblue وECMWF، "
        "ورصد محطة RCSS، وأسعار Polymarket. صورة MSM خامسة اختيارية للمعلومة فقط. "
        "أضف المبلغ في تعليق الألبوم، أو سيُستخدم 200 دولار.\n"
        "لتسجيل النتيجة الفعلية لاحقًا استخدم:\n"
        "/result الذروة_الخام_الأعلى الذروة_الفعلية"
    )


async def result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context) or update.effective_message is None:
        return

    if len(context.args) != 2 or any(
        NUMBER_PATTERN.fullmatch(value) is None for value in context.args
    ):
        await update.effective_message.reply_text(
            "الصيغة: /result الذروة_الخام_الأعلى الذروة_الفعلية\n"
            "أدخل درجتي الحرارة بالأرقام، مثل: /result 34 32.5"
        )
        return

    try:
        raw_peak = Decimal(context.args[0].replace(",", "."))
        actual_peak = Decimal(context.args[1].replace(",", "."))
    except InvalidOperation:
        await update.effective_message.reply_text(
            "تعذّر قراءة درجتي الحرارة. أرسلهما كرقمين."
        )
        return

    if not raw_peak.is_finite() or not actual_peak.is_finite():
        await update.effective_message.reply_text(
            "يجب أن تكون درجتا الحرارة قيمتين رقميتين صالحَتين."
        )
        return

    async with _corrections_lock:
        try:
            existing = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                raise ValueError("ملف التصحيحات ليس قائمة JSON.")
            record = {
                "raw_peak_highest": _format_decimal(raw_peak),
                "actual_peak": _format_decimal(actual_peak),
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            existing.append(record)
            serialized = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=CORRECTIONS_PATH.parent,
                    prefix=".corrections-",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_path = temporary_file.name
                    temporary_file.write(serialized)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_path, CORRECTIONS_PATH)
            finally:
                if temporary_path and os.path.exists(temporary_path):
                    os.unlink(temporary_path)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.exception("Could not update corrections.json")
            await update.effective_message.reply_text(
                "تعذّر حفظ التصحيح في data/corrections.json. "
                "تحقّق من أن الملف موجود وصيغته JSON صحيحة."
            )
            return

    await update.effective_message.reply_text(
        "تم تسجيل التصحيح.\n"
        f"الذروة الخام الأعلى: {_format_decimal(raw_peak)}°م\n"
        f"الذروة الفعلية: {_format_decimal(actual_peak)}°م"
    )


def _photo_file_id(message: Message) -> tuple[str, str] | None:
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, photo.file_unique_id
    if message.document and message.document.mime_type:
        if message.document.mime_type.startswith("image/"):
            document = message.document
            return document.file_id, document.file_unique_id
    return None


async def _download_image(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    telegram_file = await context.bot.get_file(file_id)
    return bytes(await telegram_file.download_as_bytearray())


def _normalize_image(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()


def _methodology_system_prompt(methodology: str) -> str:
    return (
        "أنت محلل لأسواق عقود الذروة اليومية لدرجة الحرارة في تايبيه، محطة RCSS. "
        "حلّل محتوى الصور بصريًا ولا تعتمد على ترتيب إرسالها للتعرّف عليها. "
        "ميّز الصور، بحسب محتواها، بين توقعات تطبيق الطقس mblue وECMWF وMSM، "
        "ورصد RCSS بصيغة METAR، وأسعار عقود Polymarket. "
        "اتبع تحديد المنهجية للمصادر الأساسية والاختيارية حرفيًا؛ لا تفرض "
        "اشتراطات اكتمال إضافية من عندك، ولا تستخدم MSM لاختيار نطاق الدرجات "
        "إذا نصت المنهجية على أنه للمعلومة فقط. "
        "طبّق نص المنهجية المرفق كما هو وبالترتيب الثابت الذي يحدده، مع قواعده "
        "الثماني ومرجع الصعود؛ لا تحذف خطوة ولا تضف قاعدة أو تخمينًا من عندك. "
        "إذا كان النص لا يحدد طريقةً لازمة، أو كانت صورة/قراءة غير واضحة، "
        "صرّح بذلك ولا تختلق قيمة. "
        "اكتب الرد بالعربية الفصحى كنص مناسب لرسالة تيليجرام، دون جداول "
        "Markdown أو تنسيق Markdown. استخدم فقرات قصيرة أو نقاطًا نصية عند الحاجة.\n\n"
        "نص المنهجية الذي وضعه المستخدم، من دون تعديل:\n"
        "<methodology>\n"
        f"{methodology}\n"
        "</methodology>"
    )


async def _analyze_images(images: list[bytes], amount_usd: Decimal) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=_required_env("ANTHROPIC_API_KEY")
        )

    methodology = METHODOLOGY_PATH.read_text(encoding="utf-8")
    if not methodology.strip():
        raise ValueError("ملف المنهجية فارغ.")

    correction_history = _recent_corrections_for_prompt()
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "حلّل الصور المرسلة لعقد الذروة القصوى اليومية في تايبيه "
                "لمحطة RCSS. قد تكون أربع صور أساسية مع صورة خامسة اختيارية. "
                "حدّد وظيفة كل صورة من محتواها أولًا، ثم نفّذ "
                "المنهجية المرفقة بترتيبها الثابت. "
                f"المبلغ المذكور للأخذ به حيث تسمح المنهجية: "
                f"{_format_decimal(amount_usd)} دولار أمريكي.\n\n"
                "سجل التصحيحات التالي مستخرج من data/corrections.json. "
                "فرق التصحيح محسوب على أنه الذروة الفعلية ناقص الذروة الخام، "
                "والوقت محوّل إلى توقيت تايبيه. استخدم أحدث النتائج وفق نافذة "
                "المنهجية، ولا تعتبر وقت التسجيل تاريخ السوق إن لم تؤيده الصور. "
                "إذا لم تكفِ السجلات لتغطية 3 إلى 5 أيام، فاذكر نقصها ولا تخترع "
                "تصحيحًا تاريخيًا:\n"
                f"{correction_history}"
            ),
        }
    ]
    for index, image_bytes in enumerate(images, start=1):
        normalized = _normalize_image(image_bytes)
        content.extend(
            [
                {"type": "text", "text": f"الصورة {index}:"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(normalized).decode("ascii"),
                    },
                },
            ]
        )

    response = await _anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        system=_methodology_system_prompt(methodology),
        messages=[{"role": "user", "content": content}],
    )
    answer = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not answer:
        raise RuntimeError("Claude returned no text.")
    return answer


class CorrectionLogError(ValueError):
    """Raised when the correction history cannot be safely used."""


def _recent_corrections_for_prompt() -> str:
    try:
        history = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorrectionLogError("تعذرت قراءة data/corrections.json.") from exc

    if not isinstance(history, list):
        raise CorrectionLogError("يجب أن يكون سجل التصحيحات قائمة JSON.")

    parsed_entries: list[tuple[datetime, dict[str, str]]] = []
    for index, entry in enumerate(history, start=1):
        if not isinstance(entry, dict):
            raise CorrectionLogError(f"سجل التصحيح رقم {index} غير صالح.")
        try:
            raw_peak = Decimal(str(entry["raw_peak_highest"]))
            actual_peak = Decimal(str(entry["actual_peak"]))
            recorded_at = datetime.fromisoformat(
                str(entry["recorded_at_utc"]).replace("Z", "+00:00")
            )
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise CorrectionLogError(
                f"سجل التصحيح رقم {index} يفتقد بيانات صالحة."
            ) from exc

        if (
            not raw_peak.is_finite()
            or not actual_peak.is_finite()
            or recorded_at.tzinfo is None
        ):
            raise CorrectionLogError(f"سجل التصحيح رقم {index} غير صالح.")

        local_time = recorded_at.astimezone(TAIPEI_TZ)
        parsed_entries.append(
            (
                recorded_at,
                {
                    "recorded_at_taipei": local_time.isoformat(timespec="minutes"),
                    "raw_peak_highest_c": _format_decimal(raw_peak),
                    "actual_peak_c": _format_decimal(actual_peak),
                    "correction_actual_minus_raw_c": _format_decimal(
                        actual_peak - raw_peak
                    ),
                },
            )
        )

    latest_entries = [
        record for _, record in sorted(parsed_entries, key=lambda item: item[0])
    ][-5:]
    if not latest_entries:
        return "لا توجد تصحيحات مسجلة بعد."
    return json.dumps(latest_entries, ensure_ascii=False, indent=2)


async def _finalize_album(
    application: Application, album_key: str, chat_id: int
) -> None:
    await asyncio.sleep(ALBUM_SETTLE_SECONDS)
    albums: dict[str, dict[str, Any]] = application.bot_data["albums"]
    album = albums.pop(album_key, None)
    if album is None:
        return

    count = len(album["images"])
    if not ALBUM_MIN_IMAGE_COUNT <= count <= ALBUM_MAX_IMAGE_COUNT:
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                f"وصلت {count} صورًا؛ أرسل أربع صور أساسية على الأقل في ألبوم واحد. "
                "يمكن إضافة صورة MSM خامسة اختيارية. أعد إرسال الألبوم كاملًا."
            ),
        )
        return

    if not METHODOLOGY_PATH.is_file() or not METHODOLOGY_PATH.read_text(
        encoding="utf-8"
    ).strip():
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                "ملف methodology_prompt.md فارغ. أضف المنهجية كاملةً، "
                "بترتيبها الثابت وقواعدها الثماني ومرجع الصعود، ثم أعد إرسال الصور."
            ),
        )
        return

    amount_usd = _parse_amount(album.get("caption"))
    status_message = await application.bot.send_message(
        chat_id=chat_id,
        text=(
            f"تم استلام {count} صور. جارٍ تحليلها بمبلغ "
            f"{_format_decimal(amount_usd)} دولار…"
        ),
    )
    try:
        answer = await _analyze_images(album["images"], amount_usd)
    except ValueError as exc:
        if str(exc) == "ملف المنهجية فارغ.":
            explanation = (
                "ملف methodology_prompt.md فارغ. أضف المنهجية كاملةً ثم أعد الإرسال."
            )
        elif isinstance(exc, CorrectionLogError):
            logger.exception("Invalid corrections history")
            explanation = (
                "تعذّر قراءة سجل التصحيحات data/corrections.json. "
                "تحقّق من أن الملف يحتوي على قائمة JSON وسجلات صالحة."
            )
        else:
            logger.exception("Invalid analysis input")
            explanation = "تعذّر تحليل الصور. تحقّق من وضوحها ثم أعد إرسال الألبوم."
        await status_message.edit_text(explanation)
        return
    except anthropic.APIError:
        logger.exception("Anthropic API request failed")
        await status_message.edit_text(
            "تعذّر الاتصال بخدمة التحليل. تحقّق من إعداد ANTHROPIC_API_KEY "
            "ثم أعد إرسال الألبوم."
        )
        return
    except Exception:
        logger.exception("Unexpected analysis failure")
        await status_message.edit_text(
            "حدث خطأ أثناء تحليل الصور. تحقّق من إعدادات البوت وحاول مجددًا."
        )
        return

    await status_message.delete()
    await _send_long_message(application, chat_id, answer)


async def receive_photo_album(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_allowed(update, context):
        return

    message = update.effective_message
    if message is None or message.chat is None:
        return
    if not message.media_group_id:
        await message.reply_text(
            "أرسل أربع صور أساسية على الأقل معًا في ألبوم تيليجرام واحد، "
            "وليس كصور منفردة. صورة MSM الخامسة اختيارية."
        )
        return

    image_file = _photo_file_id(message)
    if image_file is None:
        return
    file_id, file_unique_id = image_file
    album_key = f"{message.chat_id}:{message.media_group_id}"
    albums: dict[str, dict[str, Any]] = context.application.bot_data["albums"]
    album = albums.setdefault(
        album_key,
        {"images": [], "unique_ids": set(), "caption": None, "task": None},
    )
    if file_unique_id in album["unique_ids"]:
        return

    prior_task = album.get("task")
    if prior_task is not None:
        prior_task.cancel()
    album["task"] = None

    try:
        image_bytes = await _download_image(context, file_id)
    except Exception:
        logger.exception("Could not download Telegram image")
        await message.reply_text("تعذّر تحميل إحدى الصور. أعد إرسال الألبوم كاملًا.")
        prior_task = album.get("task")
        if prior_task is not None:
            prior_task.cancel()
        albums.pop(album_key, None)
        return

    album["images"].append(image_bytes)
    album["unique_ids"].add(file_unique_id)
    if message.caption and not album["caption"]:
        album["caption"] = message.caption

    if len(album["images"]) > ALBUM_MAX_IMAGE_COUNT:
        prior_task = album.get("task")
        if prior_task is not None:
            prior_task.cancel()
        albums.pop(album_key, None)
        await message.reply_text(
            "وصل أكثر من خمس صور في الألبوم. أرسل أربع صور أساسية، "
            "ويمكن إضافة MSM كصورة خامسة اختيارية."
        )
        return

    album["task"] = asyncio.create_task(
        _finalize_album(context.application, album_key, message.chat_id)
    )


async def shutdown(application: Application) -> None:
    for album in application.bot_data.get("albums", {}).values():
        task = album.get("task")
        if task is not None:
            task.cancel()
    if _anthropic_client is not None:
        await _anthropic_client.close()


async def _post_init(application: Application) -> None:
    # Check every required setting before polling starts, without logging secrets.
    _required_env("TELEGRAM_BOT_TOKEN")
    _required_env("ANTHROPIC_API_KEY")
    allowed_user_id = _required_env("ALLOWED_USER_ID")
    try:
        application.bot_data["allowed_user_id"] = int(allowed_user_id)
    except ValueError as exc:
        raise RuntimeError("يجب أن يكون ALLOWED_USER_ID رقمًا صحيحًا.") from exc
    application.bot_data["albums"] = {}


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")
    token = _required_env("TELEGRAM_BOT_TOKEN")
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("result", result))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photo_album)
    )
    try:
        application.run_polling()
    except InvalidToken:
        logger.error(
            "Telegram rejected the bot token. Revoke it in BotFather and update "
            "TELEGRAM_BOT_TOKEN through Replit Secrets."
        )


if __name__ == "__main__":
    main()
