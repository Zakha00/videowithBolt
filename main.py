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
from aiogram.types import (
    CallbackQuery, FSInputFile, Message,
    InputMediaPhoto, InputMediaVideo,
)

import database as db
from downloader import download, cleanup, is_valid_url, detect_type
from keyboards import format_keyboard, subscribe_keyboard, check_again_keyboard

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
TOKEN       = os.getenv("BOT_TOKEN", "")
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_ID  = os.getenv("CHANNEL_ID", "@your_channel")   # для проверки подписки

# ─── Антиспам ────────────────────────────────────────────────────────────────
_last_req: dict[int, float] = defaultdict(float)
RATE_LIMIT = 8  # секунд


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - _last_req[user_id] < RATE_LIMIT:
        return True
    _last_req[user_id] = now
    return False


# ─── Pending URLs (ждут выбора формата) ──────────────────────────────────────
_pending: dict[int, str] = {}   # user_id → url


# ─── Web-сервер для Render ────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *a):
        pass


def _run_web():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), _Handler).serve_forever()


# ─── Bot & Dispatcher ─────────────────────────────────────────────────────────
bot = Bot(token=TOKEN)
dp  = Dispatcher()


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def check_sub(user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status not in ("left", "kicked", "banned")
    except Exception:
        return True  # если бот не добавлен в канал — не блокируем


def fmt_label(fmt: str) -> str:
    return {"video": "📹 Видео", "audio": "🎵 MP3",
            "720p": "📱 720p", "1080p": "🖥 1080p", "photo": "🖼 Фото"}.get(fmt, fmt)


def _sub_text(user_id: int) -> str:
    """Красивое сообщение с просьбой подписаться."""
    ad_url, ad_name = db.get_next_ad_channel()
    if not ad_url:
        ad_url = "https://t.me/your_channel"
        ad_name = "наш канал"

    remaining = db.remaining_downloads(user_id)
    used      = db.get_user(user_id)[2] if db.get_user(user_id) else 0

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 <b>Доступ ограничен</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ты использовал <b>{used}</b> скачиваний.\n\n"
        f"Чтобы получить ещё <b>{db.BATCH_SIZE} скачиваний</b> — "
        f"подпишись на канал <b>{ad_name}</b> 👇\n\n"
        "После подписки нажми ✅"
    ), ad_url


# ─── /start ──────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
        except ValueError:
            pass

    db.upsert_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or "",
    )

    if referrer_id and referrer_id != message.from_user.id:
        if db.register_referral(referrer_id, message.from_user.id):
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 По твоей ссылке пришёл новый пользователь!\n"
                    f"Тебе начислено +{db.REFERRAL_BONUS} скачивания 🎁"
                )
            except Exception:
                pass

    rem   = db.remaining_downloads(message.from_user.id)
    total = db.downloads_allowed(message.from_user.id)
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"

    text = (
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        "Отправь мне ссылку и я скачаю видео с:\n"
        "  YouTube  •  TikTok  •  Instagram\n"
        "  Twitter/X  •  Facebook  •  Reddit\n"
        "  и 1000+ других сайтов\n\n"
        f"🎁 Доступно скачиваний: <b>{rem}/{total}</b>\n\n"
        f"👥 Приглашай друзей и получай +{db.REFERRAL_BONUS} скачивания за каждого:\n"
        f"<code>{ref_link}</code>"
    )
    await message.answer(text, parse_mode="HTML")


# ─── /stats ───────────────────────────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    uid   = message.from_user.id
    user  = db.get_user(uid)
    if not user:
        await message.answer("Ты ещё не скачивал ничего. Отправь ссылку!")
        return

    dl    = user[2]   # downloads
    rem   = db.remaining_downloads(uid)
    total = db.downloads_allowed(uid)
    refs  = db.get_referral_count(uid)
    is_s  = await check_sub(uid)
    sub_s = "✅ Подписан" if is_s else "❌ Не подписан"

    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"

    text = (
        "📊 <b>Твоя статистика</b>\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"📥 Всего скачиваний: <b>{dl}</b>\n"
        f"🎁 Осталось сейчас: <b>{rem} из {total}</b>\n"
        f"📢 Подписка: {sub_s}\n"
        f"👥 Приглашено друзей: <b>{refs}</b>\n\n"
        f"🔗 Твоя реферальная ссылка:\n<code>{ref_link}</code>"
    )
    await message.answer(text, parse_mode="HTML")


# ─── /history ─────────────────────────────────────────────────────────────────
@dp.message(Command("history"))
async def cmd_history(message: Message):
    rows = db.get_history(message.from_user.id)
    if not rows:
        await message.answer("📭 История скачиваний пуста.")
        return

    lines = ["📋 <b>Последние скачивания:</b>\n"]
    for i, (title, fmt, created_at) in enumerate(rows, 1):
        date = created_at[:10] if created_at else ""
        label = fmt_label(fmt)
        lines.append(f"{i}. {label} — <i>{title[:50]}</i>\n   <code>{date}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /ref ─────────────────────────────────────────────────────────────────────
@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid = message.from_user.id
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    refs = db.get_referral_count(uid)

    text = (
        "👥 <b>Реферальная программа</b>\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"За каждого приглашённого друга ты получаешь\n"
        f"<b>+{db.REFERRAL_BONUS} скачивания</b> бесплатно!\n\n"
        f"👥 Ты пригласил: <b>{refs}</b> чел.\n\n"
        f"🔗 Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        "Поделись с друзьями! 🚀"
    )
    await message.answer(text, parse_mode="HTML")


# ─── /admin ───────────────────────────────────────────────────────────────────
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    total_users = db.get_total_users()
    today       = db.get_today_stats()
    today_dl    = today[1] if today else 0
    all_dl      = db.get_total_downloads_all()
    channels    = db.get_ad_channels()
    ch_list = "\n".join(f"  [{c[0]}] {c[2]} — {c[1]}" for c in channels) or "  (нет каналов)"

    text = (
        "🛠 <b>Админ-панель</b>\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"📥 Скачиваний сегодня: <b>{today_dl}</b>\n"
        f"📊 Всего скачиваний: <b>{all_dl}</b>\n\n"
        f"📢 <b>Рекламные каналы:</b>\n{ch_list}\n\n"
        "<b>Команды управления:</b>\n"
        "/addad URL Название — добавить канал\n"
        "/delad ID — удалить канал\n"
        "/broadcast Текст — рассылка"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("addad"))
async def cmd_addad(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.removeprefix("/addad").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /addad https://t.me/channel Название канала")
        return
    url, name = parts[0], parts[1]
    db.add_ad_channel(url, name)
    await message.answer(f"✅ Канал добавлен: <b>{name}</b>", parse_mode="HTML")


@dp.message(Command("delad"))
async def cmd_delad(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        cid = int(message.text.removeprefix("/delad").strip())
        db.remove_ad_channel(cid)
        await message.answer(f"✅ Канал #{cid} удалён")
    except ValueError:
        await message.answer("Использование: /delad 1")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Использование: /broadcast Текст сообщения")
        return

    user_ids = db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Рассылаю {len(user_ids)} пользователям...")

    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ Готово!\n📤 Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML"
    )


# ─── Callback: проверка подписки ──────────────────────────────────────────────
@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery):
    uid = call.from_user.id
    if await check_sub(uid):
        db.grant_subscription(uid)
        rem = db.remaining_downloads(uid)
        await call.message.edit_text(
            "✅ <b>Подписка подтверждена!</b>\n\n"
            f"🎁 Тебе открыто <b>{db.BATCH_SIZE}</b> новых скачиваний.\n"
            f"Доступно сейчас: <b>{rem}</b>\n\n"
            "Отправь ссылку на видео ⬇️",
            parse_mode="HTML"
        )
    else:
        await call.answer("❌ Подписка не найдена. Подпишись и попробуй снова!", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=check_again_keyboard())
        except Exception:
            pass


# ─── Callback: выбор формата ──────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("fmt:"))
async def cb_format(call: CallbackQuery):
    uid = call.from_user.id
    fmt = call.data.split(":")[1]
    url = _pending.pop(uid, None)

    if not url:
        await call.answer("⏰ Ссылка устарела. Отправь её снова.", show_alert=True)
        return

    await call.message.edit_text(
        f"⏳ Скачиваю {fmt_label(fmt)}...\n<i>Обычно 5–20 секунд</i>",
        parse_mode="HTML"
    )

    result = None
    try:
        result = await download(url, fmt)

        ad_url, ad_name = db.get_next_ad_channel()
        caption = "🎉 <b>Готово!</b>"
        if ad_url:
            caption += f"\n\n📢 Подпишись: <a href='{ad_url}'>{ad_name}</a>"

        if fmt == "audio":
            audio = FSInputFile(result.path)
            await call.message.answer_audio(
                audio=audio,
                title=result.title[:64] if result.title else "Audio",
                caption=caption,
                parse_mode="HTML"
            )
        else:
            video = FSInputFile(result.path)
            await call.message.answer_video(
                video=video,
                caption=caption,
                parse_mode="HTML"
            )

        db.increment_downloads(uid)
        db.log_download(uid, url, result.title, fmt, "ok")

        rem = db.remaining_downloads(uid)
        if rem == 0:
            # Сразу предупреждаем о следующей подписке
            ad_url2, ad_name2 = db.get_next_ad_channel()
            if not ad_url2:
                ad_url2 = "https://t.me/your_channel"
                ad_name2 = "наш канал"
            await call.message.answer(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🔐 <b>Скачивания закончились!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Подпишись на <b>{ad_name2}</b>, чтобы получить ещё "
                f"<b>{db.BATCH_SIZE}</b> скачиваний 👇",
                parse_mode="HTML",
                reply_markup=subscribe_keyboard(ad_url2)
            )
        elif 0 < rem <= 2:
            await call.message.answer(
                f"⚠️ Осталось скачиваний: <b>{rem}</b>\n"
                "Скоро потребуется подписка на канал.",
                parse_mode="HTML"
            )

        try:
            await call.message.delete()
        except Exception:
            pass

    except ValueError as e:
        db.log_download(uid, url, "", fmt, "error_size")
        await call.message.edit_text(
            f"❌ <b>{e}</b>\n\nПопробуй выбрать формат <b>720p</b> — он меньше весит.",
            parse_mode="HTML",
            reply_markup=format_keyboard()
        )
        _pending[uid] = url

    except Exception as e:
        err = str(e).lower()
        db.log_download(uid, url, "", fmt, f"error: {str(e)[:80]}")
        logger.error(f"Download error [{uid}] [{fmt}] {url}: {e}")

        if "private" in err or "unavailable" in err or "not available" in err:
            msg = "❌ Видео недоступно или приватное."
        elif "unsupported" in err:
            msg = "❌ Этот сайт не поддерживается."
        elif "login" in err or "sign in" in err:
            msg = "❌ Видео требует авторизации (приватное)."
        else:
            msg = "❌ Не удалось скачать. Попробуй другую ссылку."

        await call.message.edit_text(msg, parse_mode="HTML")

    finally:
        if result:
            cleanup(result.path)


# ─── Основной обработчик ссылок ───────────────────────────────────────────────
@dp.message(F.text)
async def handle_url(message: Message):
    url = message.text.strip()
    uid = message.from_user.id

    if not is_valid_url(url):
        await message.answer(
            "❓ Отправь мне ссылку на видео.\n\n"
            "Поддерживаемые сайты:\n"
            "YouTube • TikTok • Instagram • Twitter/X • Facebook • Reddit и др."
        )
        return

    db.upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")

    # Антиспам
    if is_rate_limited(uid):
        await message.answer(f"⏱ Подожди немного между запросами.")
        return

    # Проверка лимита
    if db.needs_subscription(uid):
        is_s = await check_sub(uid)
        if not is_s:
            text, ad_url = _sub_text(uid)
            await message.answer(text, parse_mode="HTML", reply_markup=subscribe_keyboard(ad_url))
            return
        else:
            # Уже подписан — авто-даём батч
            db.grant_subscription(uid)
            await message.answer(
                f"✅ Отлично! Тебе начислено <b>{db.BATCH_SIZE}</b> новых скачиваний.",
                parse_mode="HTML"
            )

    # Сохраняем URL и показываем выбор формата
    _pending[uid] = url

    await message.answer(
        "🔗 <b>Ссылка получена!</b>\n\n"
        "Выбери формат:",
        parse_mode="HTML",
        reply_markup=format_keyboard()
    )


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    db.init_db()
    logger.info("✅ БД инициализирована (Turso)")

    threading.Thread(target=_run_web, daemon=True).start()
    logger.info("✅ Web-сервер запущен")

    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
