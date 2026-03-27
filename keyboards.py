from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def format_keyboard() -> InlineKeyboardMarkup:
    """Выбор формата скачивания."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📹 Видео", callback_data="fmt:video"),
        InlineKeyboardButton(text="🎵 MP3",   callback_data="fmt:audio"),
    )
    builder.row(
        InlineKeyboardButton(text="📱 720p",  callback_data="fmt:720p"),
        InlineKeyboardButton(text="🖥 1080p", callback_data="fmt:1080p"),
    )
    builder.row(
        InlineKeyboardButton(text="🖼 Фото",  callback_data="fmt:photo"),
    )
    return builder.as_markup()


def subscribe_keyboard(channel_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📢 Подписаться", url=channel_url)
    )
    builder.row(
        InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")
    )
    return builder.as_markup()


def check_again_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 Проверить снова", callback_data="check_sub")
    )
    return builder.as_markup()
