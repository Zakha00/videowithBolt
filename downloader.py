import asyncio
import os
import yt_dlp
from pathlib import Path

DOWNLOAD_PATH = Path("downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

FREE_LIMIT = 5  # сколько бесплатных скачиваний


def _sync_download(url: str) -> str:
    """Синхронная загрузка (запускается в executor)."""
    ydl_opts = {
        "outtmpl": str(DOWNLOAD_PATH / "%(id)s.%(ext)s"),
        # Приоритет: mp4 до 50MB, иначе лучшее что есть
        "format": "bestvideo[ext=mp4][filesize<50M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<50M]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # Таймаут соединения
        "socket_timeout": 30,
        # Максимальный размер 50 МБ
        "max_filesize": 50 * 1024 * 1024,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        # Проверяем расширение (могло смениться при merge)
        if not os.path.exists(path):
            # Попробуем найти файл с тем же именем
            stem = Path(path).stem
            for f in DOWNLOAD_PATH.iterdir():
                if f.stem == stem:
                    path = str(f)
                    break
        return path


async def download_video(url: str) -> str:
    """Асинхронная обёртка над yt-dlp."""
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, _sync_download, url)
    return path


def cleanup(file_path: str):
    """Удаляет файл после отправки."""
    try:
        os.remove(file_path)
    except Exception:
        pass


def is_valid_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")
