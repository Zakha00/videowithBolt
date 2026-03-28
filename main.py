"""
Video Downloader Bot — production version
Поддержка: личные чаты + группы
"""
import asyncio
import logging
import os
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery, ErrorEvent, FSInputFile, Message,
    InputMediaPhoto, InputMediaVideo,
)

import database as db
from downloader import download, cleanup, is_valid_url, DownloadResult
from keyboards import (
    format_keyboard, subscribe_keyboard,
    check_again_keyboard, try_smaller_keyboard,
)

# ─── Логирование ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфиг ──────────────────────────────────────────────────────────────────
TOKEN      = os.getenv("BOT_TOKEN", "")
ADMIN_IDS  = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "")   # @username или -100xxx — для проверки подписки

# ─── Антиспам (in-memory) ─────────────────────────────────────────────────────
_last_req: dict[int, float] = defaultdict(float)
RATE_LIMIT = 8  # секунд между запросами одного пользователя


def is_rate_limited(uid: int) -> bool:
    now = time.time()
    if now - _last_req[uid] < RATE_LIMIT:
        return True
    _last_req[uid] = now
    return False


# ─── Pending URLs (ждут выбора формата) ──────────────────────────────────────
# user_id → (url, reply_to_message_id | None)
_pending: dict[int, tuple[str, int | None]] = {}

# ─── Web-сервер для Render ────────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass


def _run_web():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), _H).serve_forever()


# ─── Bot & Dispatcher ─────────────────────────────────────────────────────────
bot = Bot(token=TOKEN)
dp  = Dispatcher()


@dp.errors()
async def _errors_handler(event: ErrorEvent) -> None:
    """Логирует падения хендлеров; /help без БД работает, а /start и ссылки — нет при ошибке БД."""
    logger.exception("Ошибка в хендлере: %s", event.exception)
    msg = event.update.message
    if msg:
        try:
            await msg.answer(
                "⚠️ Не удалось выполнить запрос (часто это сбой базы Turso или сети). "
                "Попробуйте через минуту. Команда /help работает без сохранения данных."
            )
        except Exception:
            pass


# ─── Helpers ─────────────────────────────────────────────────────────────────

FMT_LABELS = {
    "video": "📹 Видео",
    "audio": "🎵 MP3",
    "720p":  "📱 720p",
    "1080p": "🖥 1080p",
    "photo": "🖼 Фото",
}


async def check_sub(uid: int) -> bool:
    if not CHANNEL_ID:
        return True  # проверка отключена если канал не указан
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        return m.status not in ("left", "kicked", "banned")
    except Exception:
        return True  # если бот не в канале — не блокируем


async def get_bot_username() -> str:
    me = await bot.get_me()
    return me.username


def _sub_wall(uid: int) -> tuple[str, str]:
    """Возвращает (текст сообщения, url канала) для стены подписки."""
    ad_url, ad_name = db.get_next_ad_channel()
    if not ad_url:
        ad_url  = "https://t.me/your_channel"
        ad_name = "наш канал"
    row  = db.get_user(uid)
    used = row[db.COL_DOWNLOADS] if row else 0

    text = (
        "╔══════════════════════╗\n"
        "║  🔐  Доступ закрыт  ║\n"
        "╚══════════════════════╝\n\n"
        f"Ты использовал <b>{used}</b> скачиваний.\n\n"
        f"Чтобы получить ещё <b>{db.BATCH_SIZE} скачиваний</b> бесплатно —\n"
        f"подпишись на канал <b>{ad_name}</b> 👇\n\n"
        "После подписки нажми кнопку <b>✅ Я подписался</b>"
    )
    return text, ad_url


async def _send_result(
    result: DownloadResult,
    target: Message,
    uid: int,
    url: str,
    reply_id: int | None = None,
):
    """Отправляет скачанный файл (видео / аудио / фото / карусель)."""
    ad_url, ad_name = db.get_next_ad_channel()
    caption = "✅ <b>Готово!</b>"
    if ad_url:
        caption += f"\n\n📢 <a href='{ad_url}'>{ad_name}</a>"

    kwargs: dict = {"parse_mode": "HTML", "caption": caption}
    if reply_id:
        kwargs["reply_to_message_id"] = reply_id

    fmt = result.fmt

    if fmt == "audio":
        f = FSInputFile(result.path)
        await target.answer_audio(
            audio=f,
            title=(result.title or "Audio")[:64],
            **kwargs,
        )

    elif fmt == "photo":
        all_files = [result.path] + result.extra_photos
        if len(all_files) == 1:
            f = FSInputFile(all_files[0])
            await target.answer_photo(photo=f, **kwargs)
        else:
            # Карусель (до 10 фото)
            media = []
            for i, fp in enumerate(all_files[:10]):
                mf = FSInputFile(fp)
                if i == 0:
                    media.append(InputMediaPhoto(media=mf, caption=caption, parse_mode="HTML"))
                else:
                    media.append(InputMediaPhoto(media=mf))
            await target.answer_media_group(media=media)

    else:
        f = FSInputFile(result.path)
        await target.answer_video(video=f, **kwargs)

    # Обновляем счётчики
    db.increment_downloads(uid)
    db.log_download(uid, url, result.title, fmt, "ok")

    # Предупреждение о лимите
    rem = db.remaining_downloads(uid)
    if rem == 0:
        ad_url2, ad_name2 = db.get_next_ad_channel()
        if not ad_url2:
            ad_url2, ad_name2 = "https://t.me/your_channel", "наш канал"
        await target.answer(
            "╔══════════════════════╗\n"
            "║  ⚠️  Лимит исчерпан  ║\n"
            "╚══════════════════════╝\n\n"
            f"Подпишись на <b>{ad_name2}</b> чтобы получить ещё "
            f"<b>{db.BATCH_SIZE}</b> скачиваний 👇",
            parse_mode="HTML",
            reply_markup=subscribe_keyboard(ad_url2),
        )
    elif 0 < rem <= 2:
        await target.answer(
            f"⚠️ Осталось скачиваний: <b>{rem}</b>. "
            "Скоро потребуется подписка на канал.",
            parse_mode="HTML",
        )


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid  = message.from_user.id
    text = message.text or ""
    args = text.split(maxsplit=1)

    try:
        db.upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
    except Exception as e:
        logger.exception("БД (upsert_user) в /start: %s", e)
        await message.answer(
            "⚠️ База данных сейчас недоступна. Проверьте переменные "
            "<code>TURSO_DB_URL</code> и <code>TURSO_DB_TOKEN</code> на Render.",
            parse_mode="HTML",
        )
        return

    # Реферал
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1][4:])
        except ValueError:
            ref_id = None
        if ref_id is not None:
            try:
                if db.register_referral(ref_id, uid):
                    try:
                        await bot.send_message(
                            ref_id,
                            f"🎉 По твоей ссылке пришёл новый пользователь!\n"
                            f"+{db.REFERRAL_BONUS} скачивания тебе 🎁"
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.exception("БД (referral) в /start: %s", e)

    try:
        rem   = db.remaining_downloads(uid)
        total = db.downloads_allowed(uid)
    except Exception as e:
        logger.exception("БД (лимиты) в /start: %s", e)
        await message.answer(
            "⚠️ Не удалось прочитать лимиты из базы. Проверьте Turso на Render.",
            parse_mode="HTML",
        )
        return

    bu = await get_bot_username()
    rlink = f"https://t.me/{bu}?start=ref_{uid}"

    await message.answer(
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        "Отправь мне ссылку и я скачаю видео с:\n"
        "  <b>YouTube  •  TikTok  •  Instagram</b>\n"
        "  <b>Twitter/X  •  Facebook  •  Reddit</b>\n"
        "  и 1000+ других сайтов 🌐\n\n"
        f"🎁 Бесплатных скачиваний: <b>{rem}/{total}</b>\n\n"
        f"👥 Пригласи друзей и получи +{db.REFERRAL_BONUS} скачивания за каждого:\n"
        f"<code>{rlink}</code>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /help
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Как пользоваться ботом</b>\n\n"
        "1️⃣ Отправь ссылку на видео\n"
        "2️⃣ Выбери формат (видео / MP3 / 720p / 1080p / фото)\n"
        "3️⃣ Получи файл!\n\n"
        "<b>Поддерживаемые сайты:</b>\n"
        "YouTube, TikTok, Instagram, Twitter/X,\n"
        "Facebook, Reddit, VK, Twitch и др.\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/stats — твоя статистика\n"
        "/history — история скачиваний\n"
        "/ref — реферальная программа\n"
        "/help — эта справка\n\n"
        "⚠️ Файлы >48 МБ не поддерживает Telegram — выбирай 720p.",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /stats
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    uid  = message.from_user.id
    row  = db.get_user(uid)

    if not row:
        await message.answer("Ты ещё ничего не скачивал. Просто отправь ссылку!")
        return

    dl    = row[db.COL_DOWNLOADS]
    rem   = db.remaining_downloads(uid)
    total = db.downloads_allowed(uid)
    refs  = db.get_referral_count(uid)
    is_s  = await check_sub(uid)
    bu    = await get_bot_username()
    rlink = f"https://t.me/{bu}?start=ref_{uid}"

    await message.answer(
        "📊 <b>Твоя статистика</b>\n"
        "──────────────────\n"
        f"📥 Скачиваний всего:  <b>{dl}</b>\n"
        f"🎁 Доступно сейчас:  <b>{rem} из {total}</b>\n"
        f"📢 Подписка: {'✅ активна' if is_s else '❌ нет'}\n"
        f"👥 Приглашено друзей: <b>{refs}</b>\n\n"
        f"🔗 Реф. ссылка:\n<code>{rlink}</code>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /history
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("history"))
async def cmd_history(message: Message):
    rows = db.get_history(message.from_user.id)
    if not rows:
        await message.answer("📭 История скачиваний пуста.")
        return

    lines = ["📋 <b>Последние скачивания:</b>\n"]
    for i, (title, fmt, ts) in enumerate(rows, 1):
        date  = (ts or "")[:10]
        label = FMT_LABELS.get(fmt, fmt)
        lines.append(f"<b>{i}.</b> {label} — <i>{title[:50]}</i>\n    <code>{date}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
#  /ref
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid  = message.from_user.id
    bu   = await get_bot_username()
    link = f"https://t.me/{bu}?start=ref_{uid}"
    refs = db.get_referral_count(uid)

    await message.answer(
        "👥 <b>Реферальная программа</b>\n"
        "──────────────────\n\n"
        f"За каждого приглашённого друга ты получаешь\n"
        f"<b>+{db.REFERRAL_BONUS} скачивания</b> бесплатно!\n\n"
        f"Ты уже пригласил: <b>{refs}</b> чел.\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN команды
# ══════════════════════════════════════════════════════════════════════════════

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message.from_user.id):
        return

    total  = db.get_total_users()
    today  = db.get_today_stats()
    t_dl   = today[1] if today else 0
    t_new  = today[2] if today else 0
    all_dl = db.get_total_downloads_all()

    chs = db.get_ad_channels()
    if chs:
        ch_lines = "\n".join(
            f"  [{c[0]}] {'✅' if c[3] else '❌'} {c[2]} — {c[1]}"
            for c in chs
        )
    else:
        ch_lines = "  (нет каналов — добавь через /addad)"

    top = db.get_top_users(5)
    top_lines = "\n".join(
        f"  {i+1}. {r[1] or r[2] or r[0]} — {r[3]} скач."
        for i, r in enumerate(top)
    ) or "  (нет данных)"

    await message.answer(
        "🛠 <b>Админ-панель</b>\n"
        "════════════════════\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🆕 Новых сегодня:       <b>{t_new}</b>\n"
        f"📥 Скачиваний сегодня:  <b>{t_dl}</b>\n"
        f"📊 Всего скачиваний:    <b>{all_dl}</b>\n\n"
        f"📢 <b>Рекламные каналы:</b>\n{ch_lines}\n\n"
        f"🏆 <b>Топ-5 пользователей:</b>\n{top_lines}\n\n"
        "<b>Управление:</b>\n"
        "/addad URL Название — добавить канал\n"
        "/delad ID — удалить канал\n"
        "/offad ID / /onad ID — выкл/вкл канал\n"
        "/broadcast Текст — рассылка всем",
        parse_mode="HTML",
    )


@dp.message(Command("addad"))
async def cmd_addad(message: Message):
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.removeprefix("/addad").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование:\n/addad https://t.me/channel Название")
        return
    url, name = parts[0].strip(), parts[1].strip()
    db.add_ad_channel(url, name)
    await message.answer(f"✅ Канал добавлен: <b>{name}</b>", parse_mode="HTML")


@dp.message(Command("delad"))
async def cmd_delad(message: Message):
    if not _is_admin(message.from_user.id):
        return
    try:
        cid = int(message.text.removeprefix("/delad").strip())
        db.remove_ad_channel(cid)
        await message.answer(f"✅ Канал #{cid} удалён.")
    except ValueError:
        await message.answer("Использование: /delad 3")


@dp.message(Command("offad"))
async def cmd_offad(message: Message):
    if not _is_admin(message.from_user.id):
        return
    try:
        cid = int(message.text.removeprefix("/offad").strip())
        db.toggle_ad_channel(cid, False)
        await message.answer(f"⏸ Канал #{cid} отключён.")
    except ValueError:
        await message.answer("Использование: /offad 3")


@dp.message(Command("onad"))
async def cmd_onad(message: Message):
    if not _is_admin(message.from_user.id):
        return
    try:
        cid = int(message.text.removeprefix("/onad").strip())
        db.toggle_ad_channel(cid, True)
        await message.answer(f"▶️ Канал #{cid} включён.")
    except ValueError:
        await message.answer("Использование: /onad 3")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Использование: /broadcast Текст сообщения")
        return

    ids = db.get_all_user_ids()
    sent, failed = 0, 0
    sm = await message.answer(f"⏳ Рассылаю {len(ids)} пользователям...")

    for uid in ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)   # ~20 msg/s — в рамках лимитов Telegram

    await sm.edit_text(
        f"✅ Рассылка готова!\n📤 Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Callback: проверка подписки
# ══════════════════════════════════════════════════════════════════════════════
@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery):
    uid = call.from_user.id
    if await check_sub(uid):
        db.grant_subscription(uid)
        rem = db.remaining_downloads(uid)
        await call.message.edit_text(
            "✅ <b>Подписка подтверждена!</b>\n\n"
            f"Тебе начислено <b>+{db.BATCH_SIZE}</b> скачиваний.\n"
            f"Доступно сейчас: <b>{rem}</b>\n\n"
            "Отправь ссылку на видео ⬇️",
            parse_mode="HTML",
        )
    else:
        await call.answer(
            "❌ Подписка не найдена. Подпишись и попробуй снова!",
            show_alert=True,
        )
        try:
            await call.message.edit_reply_markup(reply_markup=check_again_keyboard())
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Callback: выбор формата — скачиваем
# ══════════════════════════════════════════════════════════════════════════════
@dp.callback_query(F.data.startswith("fmt:"))
async def cb_format(call: CallbackQuery):
    uid = call.from_user.id
    fmt = call.data.split(":")[1]

    pending = _pending.pop(uid, None)
    if not pending:
        await call.answer("⏰ Ссылка устарела. Отправь её снова.", show_alert=True)
        return

    url, reply_id = pending

    await call.message.edit_text(
        f"⏳ Скачиваю {FMT_LABELS.get(fmt, fmt)}…\n"
        "<i>Обычно занимает 5–30 секунд</i>",
        parse_mode="HTML",
    )

    result: DownloadResult | None = None
    all_paths: list[str] = []

    try:
        result = await download(url, fmt)
        all_paths = [result.path] + result.extra_photos

        await _send_result(result, call.message, uid, url, reply_id)

        try:
            await call.message.delete()
        except Exception:
            pass

    except ValueError as e:
        db.log_download(uid, url, "", fmt, "error_size")
        _pending[uid] = (url, reply_id)
        await call.message.edit_text(
            f"❌ <b>{e}</b>",
            parse_mode="HTML",
            reply_markup=try_smaller_keyboard(),
        )

    except FileNotFoundError as e:
        db.log_download(uid, url, "", fmt, "error_not_found")
        logger.error(f"File not found [{uid}] [{fmt}] {url}: {e}")
        await call.message.edit_text(
            "❌ Файл не найден после скачивания.\n"
            "Попробуй другой формат или другую ссылку."
        )

    except Exception as e:
        err = str(e).lower()
        db.log_download(uid, url, "", fmt, f"err:{str(e)[:80]}")
        logger.exception(f"Download error [{uid}] [{fmt}] {url}")

        if "private" in err or "unavailable" in err or "not available" in err:
            msg = "❌ Видео недоступно или приватное."
        elif "unsupported" in err or "no video formats" in err or "no formats found" in err:
            msg = "❌ Не удалось скачать в этом формате.\nПопробуй другой формат."
        elif "login" in err or "sign in" in err:
            msg = "❌ Видео требует входа (приватный аккаунт)."
        elif "not found" in err or "http error 404" in err:
            msg = "❌ Видео не найдено — возможно удалено."
        elif "timeout" in err or "timed out" in err:
            msg = "❌ Превышено время ожидания. Попробуй ещё раз."
        else:
            msg = f"❌ Не удалось скачать.\n\nОшибка: {str(e)[:100]}"

        await call.message.edit_text(msg, parse_mode="HTML")

    finally:
        cleanup(*all_paths)


# ══════════════════════════════════════════════════════════════════════════════
#  Команды для групп
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(
    Command("video", "audio"),
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def group_cmd_download(message: Message):
    """В группе: /video <url> или /audio <url>"""
    uid  = message.from_user.id
    db.upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
    db.register_group(message.chat.id)

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not is_valid_url(parts[1]):
        await message.reply("Использование: /video https://youtube.com/...")
        return

    url = parts[1].strip()
    fmt = "audio" if message.text.split()[0].lstrip("/").startswith("audio") else "video"

    if db.needs_subscription(uid):
        is_s = await check_sub(uid)
        if not is_s:
            text, ad_url = _sub_wall(uid)
            await message.reply(text, parse_mode="HTML",
                                reply_markup=subscribe_keyboard(ad_url))
            return
        else:
            db.grant_subscription(uid)

    if is_rate_limited(uid):
        await message.reply(f"⏱ Подожди {RATE_LIMIT} сек.")
        return

    wait = await message.reply(f"⏳ Скачиваю {FMT_LABELS[fmt]}…")
    result = None
    all_paths = []
    try:
        result    = await download(url, fmt)
        all_paths = [result.path] + result.extra_photos
        await _send_result(result, message, uid, url, message.message_id)
        await wait.delete()
    except ValueError as e:
        await wait.edit_text(f"❌ {e}")
    except FileNotFoundError as e:
        logger.error(f"File not found in group [{uid}] {url}: {e}")
        await wait.edit_text("❌ Файл не найден. Попробуй другой формат.")
    except Exception as e:
        err = str(e).lower()
        logger.exception(f"Group dl [{uid}] {url}")
        if "no video formats" in err or "no formats found" in err:
            await wait.edit_text("❌ Не удалось скачать в этом формате.")
        else:
            await wait.edit_text(f"❌ Не удалось скачать.\n{str(e)[:80]}")
    finally:
        cleanup(*all_paths)


# ══════════════════════════════════════════════════════════════════════════════
#  Основной обработчик ссылок (личный чат)
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(F.text, F.chat.type == ChatType.PRIVATE, ~F.text.startswith("/"))
async def handle_url(message: Message):
    uid = message.from_user.id
    url = (message.text or "").strip()

    if not is_valid_url(url):
        await message.answer(
            "❓ Отправь мне ссылку на видео.\n\n"
            "Поддерживаю: YouTube, TikTok, Instagram,\n"
            "Twitter/X, Facebook, Reddit и многие другие.\n\n"
            "Напиши /help для справки."
        )
        return

    try:
        db.upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
    except Exception as e:
        logger.exception("БД (upsert_user) при ссылке: %s", e)
        await message.answer(
            "⚠️ Не удалось сохранить профиль в базе (Turso). "
            "Проверьте секреты на Render и статус базы.",
            parse_mode="HTML",
        )
        return

    if is_rate_limited(uid):
        await message.answer("⏱ Не так быстро! Подожди немного.")
        return

    # Стена подписки
    if db.needs_subscription(uid):
        is_s = await check_sub(uid)
        if not is_s:
            text, ad_url = _sub_wall(uid)
            await message.answer(text, parse_mode="HTML",
                                 reply_markup=subscribe_keyboard(ad_url))
            return
        else:
            # Уже подписан — выдаём батч автоматически
            db.grant_subscription(uid)
            rem = db.remaining_downloads(uid)
            await message.answer(
                f"✅ Подписка подтверждена! Тебе начислено <b>+{db.BATCH_SIZE}</b> скачиваний.\n"
                f"Доступно: <b>{rem}</b>",
                parse_mode="HTML",
            )

    # Сохраняем URL и показываем форматы
    _pending[uid] = (url, None)
    await message.answer(
        "🔗 <b>Ссылка получена!</b>\n\nВыбери формат:",
        parse_mode="HTML",
        reply_markup=format_keyboard(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Реакция на ссылку в группе (без команды)
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(
    F.text,
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def group_url_hint(message: Message):
    """Если пользователь скинул ссылку в группе — подсказка."""
    if not is_valid_url(message.text or ""):
        return
    # Проверяем — это похоже на видео-ссылку?
    known = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com",
             "twitter.com", "x.com", "facebook.com", "reddit.com", "vk.com")
    if not any(d in message.text for d in known):
        return

    bu = await get_bot_username()
    await message.reply(
        f"🎬 Хочешь скачать это видео?\n"
        f"Напиши команду: <code>/video {message.text}</code>\n"
        f"или открой бота: @{bu}",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    db.init_db()
    logger.info("✅ БД инициализирована (Turso)")

    threading.Thread(target=_run_web, daemon=True).start()
    logger.info("✅ Web-сервер запущен")

    logger.info("🚀 Бот запущен!")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    asyncio.run(main())
