import asyncio
import logging
import os
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command
from aiogram.types import CallbackQuery, FSInputFile, Message

import database as db
from downloader import cleanup, download_video, is_valid_url, FREE_LIMIT
from keyboards import check_again_keyboard, subscribe_keyboard

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

CHANNEL_ID = os.getenv("CHANNEL_ID", "@nookatbazar123")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/nookatbazar123")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Наш канал")

AD_CAPTION = f"📥 Скачано через бот\n🔥 Подписывайся: {CHANNEL_URL}"

# ─── Антиспам ────────────────────────────────────────────────────────────────
_last_request: dict[int, float] = defaultdict(float)
RATE_LIMIT_SECONDS = 10


def safe_db_call(fn, *args, **kwargs):
    """Не даёт ошибкам БД ломать обработку апдейта."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.exception(f"Ошибка БД в {fn.__name__}: {e}")
        return None


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - _last_request[user_id] < RATE_LIMIT_SECONDS:
        return True
    _last_request[user_id] = now
    return False


# ─── Web-сервер для Render ────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, *args):
        pass


def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# ─── Bot & Dispatcher ─────────────────────────────────────────────────────────
bot: Bot | None = None
dp = Dispatcher()


# ─── Проверка подписки ────────────────────────────────────────────────────────
async def check_subscription(user_id: int) -> bool:
    if bot is None:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        logger.warning("Не удалось проверить подписку, пропускаем")
        return True


# ─── /start ──────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    safe_db_call(
        db.upsert_user,
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )
    count = safe_db_call(db.get_download_count, message.from_user.id) or 0
    remaining = max(0, FREE_LIMIT - count)

    text = (
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        "Я скачаю видео с:\n"
        "• YouTube  • TikTok  • Instagram\n"
        "• Twitter/X  • VK  • и 1000+ сайтов\n\n"
        f"🎁 Бесплатных скачиваний: <b>{remaining}/{FREE_LIMIT}</b>\n\n"
        "Просто отправь ссылку на видео ⬇️"
    )
    await message.answer(text, parse_mode="HTML")


# ─── /stats ───────────────────────────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    count = safe_db_call(db.get_download_count, message.from_user.id) or 0
    remaining = max(0, FREE_LIMIT - count)
    is_sub = await check_subscription(message.from_user.id)
    sub_status = "✅ Подписан" if is_sub else "❌ Не подписан"

    text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"📥 Всего скачиваний: <b>{count}</b>\n"
        f"🎁 Бесплатных осталось: <b>{remaining}</b>\n"
        f"📢 Подписка: {sub_status}"
    )
    await message.answer(text, parse_mode="HTML")


# ─── /admin ───────────────────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    total_users = safe_db_call(db.get_total_users) or 0
    today = safe_db_call(db.get_today_stats)
    today_dl = today[1] if today else 0

    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"📥 Скачиваний сегодня: <b>{today_dl}</b>"
    )
    await message.answer(text, parse_mode="HTML")


# ─── /broadcast ───────────────────────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Использование: /broadcast Текст")
        return

    user_ids = safe_db_call(db.get_all_user_ids) or []
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Рассылаю {len(user_ids)} пользователям...")

    for uid in user_ids:
        try:
            if bot is None:
                break
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек

    await status_msg.edit_text(
        f"✅ Рассылка завершена\n"
        f"📤 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )


# ─── Callback: проверка подписки ──────────────────────────────────────────────
@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(call: CallbackQuery):
    is_sub = await check_subscription(call.from_user.id)
    if is_sub:
        safe_db_call(db.set_subscribed, call.from_user.id, True)
        await call.message.edit_text(
            "✅ <b>Подписка подтверждена!</b>\n\n"
            "Теперь можешь скачивать без ограничений.\n"
            "Отправь ссылку на видео ⬇️",
            parse_mode="HTML"
        )
    else:
        await call.answer("❌ Ты ещё не подписался!", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=check_again_keyboard())


# ─── Основной обработчик ──────────────────────────────────────────────────────
@dp.message(F.text)
async def handle_url(message: Message):
    url = message.text.strip()

    if not is_valid_url(url):
        await message.answer("❌ Отправь ссылку на видео (начинается с http)")
        return

    user_id = message.from_user.id
    safe_db_call(db.upsert_user, user_id, message.from_user.username or "", message.from_user.first_name or "")

    if is_rate_limited(user_id):
        await message.answer(f"⏱ Подожди {RATE_LIMIT_SECONDS} сек между запросами")
        return

    count = safe_db_call(db.get_download_count, user_id) or 0
    if count >= FREE_LIMIT:
        is_sub = await check_subscription(user_id)
        if not is_sub:
            safe_db_call(db.set_subscribed, user_id, False)
            await message.answer(
                f"🎁 Ты использовал все <b>{FREE_LIMIT}</b> бесплатных скачивания.\n\n"
                f"Чтобы продолжить — подпишись на канал <b>{CHANNEL_NAME}</b>!\n\n"
                "После подписки нажми кнопку ниже ✅",
                parse_mode="HTML",
                reply_markup=subscribe_keyboard(CHANNEL_URL, CHANNEL_ID),
            )
            return

    wait_msg = await message.answer("⏳ Скачиваю видео, подожди...")

    file_path = None
    try:
        file_path = await download_video(url)

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Файл не создан")

        video = FSInputFile(file_path)
        await message.answer_video(video=video, caption=AD_CAPTION)
        safe_db_call(db.increment_downloads, user_id)
        safe_db_call(db.log_download, user_id, url, "success")

        new_count = count + 1
        remaining = FREE_LIMIT - new_count
        if 0 < remaining <= 2:
            await message.answer(
                f"⚠️ Осталось бесплатных скачиваний: <b>{remaining}</b>\n"
                f"Подпишись на {CHANNEL_URL} чтобы не потерять доступ!",
                parse_mode="HTML",
            )
        elif remaining == 0:
            await message.answer(
                f"⚠️ Это было последнее бесплатное скачивание!\n"
                f"Подпишись на канал чтобы продолжить: {CHANNEL_URL}"
            )

    except Exception as e:
        err_text = str(e)
        logger.error(f"Ошибка [{user_id}] {url}: {err_text}")
        safe_db_call(db.log_download, user_id, url, f"error: {err_text[:100]}")

        if "filesize" in err_text.lower() or "too large" in err_text.lower():
            await message.answer("❌ Видео слишком большое (лимит 50 МБ)")
        elif "private" in err_text.lower() or "unavailable" in err_text.lower():
            await message.answer("❌ Видео недоступно или приватное")
        elif "unsupported" in err_text.lower():
            await message.answer("❌ Этот сайт не поддерживается")
        else:
            await message.answer("❌ Не удалось скачать. Попробуй другую ссылку")
    except Exception as e:
        logger.exception(f"Критическая ошибка в handle_url: {e}")
        await message.answer("❌ Временная ошибка сервера. Попробуй ещё раз через 10-20 секунд.")
    finally:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if file_path:
            cleanup(file_path)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global bot
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    bot = Bot(token=TOKEN)

    threading.Thread(target=run_web, daemon=True).start()
    logger.info("✅ Web-сервер запущен")

    await db.init_db()
    logger.info("✅ База данных (Turso) инициализирована")

    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("❌ Фатальная ошибка при запуске")
        raise
