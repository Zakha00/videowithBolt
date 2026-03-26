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

# ─── Логирование ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфиг ─────────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Канал для подписки — замени на свой!
CHANNEL_ID = os.getenv("CHANNEL_ID", "@your_channel")          # @username или -100xxxxxxxxxx
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/your_channel")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Наш канал")

# Реклама в подписи к видео
AD_CAPTION = f"📥 Скачано через бот\n🔥 Подписывайся: {CHANNEL_URL}"

# ─── Антиспам (in-memory, достаточно для 100k при правильном деплое) ────────
# user_id -> timestamp последнего запроса
_last_request: dict[int, float] = defaultdict(float)
RATE_LIMIT_SECONDS = 10  # одно видео в 10 секунд


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - _last_request[user_id] < RATE_LIMIT_SECONDS:
        return True
    _last_request[user_id] = now
    return False


# ─── Web-сервер для Render ───────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, *args):
        pass  # Отключаем лишние логи


def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# ─── Bot & Dispatcher ────────────────────────────────────────────────────────
bot = Bot(token=TOKEN)
dp = Dispatcher()


# ─── Проверка подписки ───────────────────────────────────────────────────────
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        # Если бот не добавлен в канал или канал неверный — пропускаем
        logger.warning("Не удалось проверить подписку, пропускаем")
        return True


# ─── Команды ────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await db.upsert_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )
    count = await db.get_download_count(message.from_user.id)
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


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика для пользователя."""
    count = await db.get_download_count(message.from_user.id)
    remaining = max(0, FREE_LIMIT - count)
    is_sub = await check_subscription(message.from_user.id)
    sub_status = "✅ Подписан" if is_sub else "❌ Не подписан"

    text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"📥 Всего скачиваний: <b>{count}</b>\n"
        f"🎁 Бесплатных осталось: <b>{remaining}</b>\n"
        f"📢 Подписка на канал: {sub_status}"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Статистика для админов."""
    if message.from_user.id not in ADMIN_IDS:
        return

    total_users = await db.get_total_users()
    today = await db.get_today_stats()
    today_dl = today[1] if today else 0
    today_users = today[2] if today else 0

    text = (
        "🛠 <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"📥 Скачиваний сегодня: <b>{today_dl}</b>\n"
        f"🆕 Новых сегодня: <b>{today_users}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Рассылка (только для админов). /broadcast Текст сообщения"""
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Использование: /broadcast Текст")
        return

    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Рассылаю {len(user_ids)} пользователям...")

    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, не нарушаем лимиты

    await status_msg.edit_text(
        f"✅ Рассылка завершена\n"
        f"📤 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}"
    )


# ─── Callback: проверка подписки ────────────────────────────────────────────
@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(call: CallbackQuery):
    is_sub = await check_subscription(call.from_user.id)
    if is_sub:
        await db.set_subscribed(call.from_user.id, True)
        await call.message.edit_text(
            "✅ <b>Подписка подтверждена!</b>\n\n"
            "Теперь можешь скачивать без ограничений.\n"
            "Отправь ссылку на видео ⬇️",
            parse_mode="HTML"
        )
    else:
        await call.answer("❌ Ты ещё не подписался!", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=check_again_keyboard())


# ─── Основной обработчик ссылок ─────────────────────────────────────────────
@dp.message(F.text)
async def handle_url(message: Message):
    url = message.text.strip()

    if not is_valid_url(url):
        await message.answer("❌ Отправь ссылку на видео (начинается с http)")
        return

    user_id = message.from_user.id
    await db.upsert_user(user_id, message.from_user.username or "", message.from_user.first_name or "")

    # ── Антиспам ────────────────────────────────────────────────────────────
    if is_rate_limited(user_id):
        await message.answer(
            f"⏱ Подожди немного между запросами ({RATE_LIMIT_SECONDS} сек)"
        )
        return

    # ── Проверка лимита ─────────────────────────────────────────────────────
    count = await db.get_download_count(user_id)
    if count >= FREE_LIMIT:
        is_sub = await check_subscription(user_id)
        if not is_sub:
            await db.set_subscribed(user_id, False)
            remaining_text = (
                f"🎁 Ты использовал все <b>{FREE_LIMIT}</b> бесплатных скачивания.\n\n"
                f"Чтобы продолжить скачивать <b>бесплатно и без ограничений</b> — "
                f"подпишись на наш канал <b>{CHANNEL_NAME}</b>!\n\n"
                "После подписки нажми кнопку ниже ✅"
            )
            await message.answer(
                remaining_text,
                parse_mode="HTML",
                reply_markup=subscribe_keyboard(CHANNEL_URL, CHANNEL_ID),
            )
            return

    # ── Скачивание ──────────────────────────────────────────────────────────
    wait_msg = await message.answer("⏳ Скачиваю видео, подожди...")

    file_path = None
    try:
        file_path = await download_video(url)

        if not file_path or not __import__("os").path.exists(file_path):
            raise FileNotFoundError("Файл не создан")

        video = FSInputFile(file_path)
        await message.answer_video(video=video, caption=AD_CAPTION)
        await db.increment_downloads(user_id)
        await db.log_download(user_id, url, "success")

        # Показываем остаток бесплатных
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
                f"⚠️ Это было твоё последнее бесплатное скачивание!\n"
                f"Подпишись на канал чтобы продолжить: {CHANNEL_URL}"
            )

    except Exception as e:
        err_text = str(e)
        logger.error(f"Ошибка скачивания [{user_id}] {url}: {err_text}")
        await db.log_download(user_id, url, f"error: {err_text[:100]}")

        if "filesize" in err_text.lower() or "too large" in err_text.lower():
            await message.answer("❌ Видео слишком большое (лимит 50 МБ)")
        elif "private" in err_text.lower() or "unavailable" in err_text.lower():
            await message.answer("❌ Видео недоступно или приватное")
        elif "unsupported" in err_text.lower():
            await message.answer("❌ Этот сайт не поддерживается")
        else:
            await message.answer(f"❌ Не удалось скачать видео\nПопробуй другую ссылку")
    finally:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if file_path:
            cleanup(file_path)


# ─── Main ────────────────────────────────────────────────────────────────────
async def main():
    await db.init_db()
    logger.info("✅ База данных инициализирована")

    # Запускаем HTTP-сервер в отдельном потоке
    threading.Thread(target=run_web, daemon=True).start()
    logger.info("✅ Web-сервер запущен")

    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
