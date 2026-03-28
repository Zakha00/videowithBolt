import asyncio
import os
import re
from pathlib import Path

import yt_dlp

DOWNLOAD_PATH = Path("downloads")
DOWNLOAD_PATH.mkdir(exist_ok=True)

MAX_MB = 48   # Telegram limit with margin

_FORMAT_OPTS = {
    "video": {
        "format": "best[ext=mp4]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    },
    "720p": {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo[height<=1080]+bestaudio/best",
        "merge_output_format": "mp4",
    },
    "1080p": {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    },
    "audio": {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    },
}


class DownloadResult:
    __slots__ = ("path", "title", "fmt", "extra_photos")

    def __init__(self, path: str, title: str, fmt: str,
                 extra_photos: list[str] | None = None):
        self.path         = path
        self.title        = title
        self.fmt          = fmt
        self.extra_photos = extra_photos or []   # для каруселей


def _find_file(video_id: str, suffix: str | None = None) -> str | None:
    """Ищет файл по ID в папке загрузок."""
    for f in DOWNLOAD_PATH.iterdir():
        if video_id in f.stem:
            if suffix is None or f.suffix.lower() == suffix:
                return str(f)
    return None


def _sync_download(url: str, fmt: str) -> DownloadResult:
    out_tpl = str(DOWNLOAD_PATH / "%(id)s.%(ext)s")
    opts = {
        "outtmpl":       out_tpl,
        "quiet":         False,
        "no_warnings":   False,
        "noplaylist":    True,
        "socket_timeout": 30,
        "ignoreerrors":  False,
        **_FORMAT_OPTS[fmt],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info  = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Не удалось получить информацию о видео")

        title = info.get("title", "")
        vid   = info.get("id", "")

        if fmt == "audio":
            path = _find_file(vid, ".mp3")
            if not path:
                path = _find_file(vid)
        else:
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                path = _find_file(vid, ".mp4")
            if not path:
                path = _find_file(vid)

        if not path or not os.path.exists(path):
            raise FileNotFoundError("Файл не создан после скачивания")

        mb = os.path.getsize(path) / (1024 * 1024)
        if mb > MAX_MB:
            cleanup(path)
            raise ValueError(
                f"Видео весит {mb:.0f} МБ — слишком большое для Telegram.\n"
                "Попробуй формат 720p"
            )

        return DownloadResult(path=path, title=title, fmt=fmt)


def _sync_download_photos(url: str) -> DownloadResult:
    """Скачивает фото/карусель (Instagram, VK и т.п.)."""
    out_tpl = str(DOWNLOAD_PATH / "%(id)s.%(ext)s")
    opts = {
        "outtmpl":       out_tpl,
        "quiet":         False,
        "no_warnings":   False,
        "format":        "best",
        "socket_timeout": 30,
        "writethumbnail": False,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("Не удалось получить информацию")

        title = info.get("title", "")
        vid   = info.get("id", "")

    photo_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    video_exts = {".mp4", ".mov", ".webm", ".avi", ".mkv"}

    files = sorted(
        str(f) for f in DOWNLOAD_PATH.iterdir()
        if vid in f.stem
    )

    if not files:
        raise FileNotFoundError("Файлы не найдены после скачивания")

    photo_files = [f for f in files if Path(f).suffix.lower() in photo_exts]
    video_files = [f for f in files if Path(f).suffix.lower() in video_exts]

    if photo_files:
        return DownloadResult(path=photo_files[0], title=title, fmt="photo",
                              extra_photos=photo_files[1:])
    elif video_files:
        return DownloadResult(path=video_files[0], title=title, fmt="video",
                              extra_photos=video_files[1:])
    else:
        return DownloadResult(path=files[0], title=title, fmt="photo",
                              extra_photos=files[1:])


async def download(url: str, fmt: str = "video") -> DownloadResult:
    loop = asyncio.get_event_loop()
    if fmt == "photo":
        return await loop.run_in_executor(None, _sync_download_photos, url)
    return await loop.run_in_executor(None, _sync_download, url, fmt)


def cleanup(*paths: str):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def is_valid_url(text: str) -> bool:
    return bool(re.match(r"https?://\S+", text.strip()))
