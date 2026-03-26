import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart
import yt_dlp

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Bot is running')

def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

threading.Thread(target=run_web).start()

TOKEN = "7992634454:AAGYAg70kCp5ye79FeuerTAnsgMe5Gg4zZY"

bot = Bot(token=TOKEN)
dp = Dispatcher()

DOWNLOAD_PATH = "downloads"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

AD_TEXT = "\n\n📢 Подписывайтесь: @your_channel"


def download_video(url):
    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_PATH}/%(title)s.%(ext)s',
        'format': 'mp4',  # важно!
        'noplaylist': True,
        'quiet': True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)
print("BOT STARTED")

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Отправь ссылку 📎")


@dp.message()
async def handler(message: Message):
    url = message.text.strip()

    if "http" not in url:
        await message.answer("❌ Нужна ссылка")
        return

    await message.answer("⏳ Скачиваю...")

    try:
        file_path = download_video(url)

        # Проверка существования файла
        if not os.path.exists(file_path):
            await message.answer("❌ Файл не найден после загрузки")
            return

        video = FSInputFile(file_path)

        await message.answer_video(
            video=video,
            caption="Готово 🎉" + AD_TEXT
        )

        os.remove(file_path)

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def main():
    print("Бот работает 🚀")
    await dp.start_polling(bot)
    
if __name__ == "__main__":
    asyncio.run(main())
