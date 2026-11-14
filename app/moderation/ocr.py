import asyncio
import io
import logging
from typing import Optional, Tuple

from aiogram import Bot

logger = logging.getLogger(__name__)


async def _download_file_bytes(bot: Bot, file_id: str) -> Optional[bytes]:
    """
    Скачивает файл по file_id из Telegram в память (bytes).
    Ничего на диск не пишем.
    """
    buf = io.BytesIO()

    # aiogram 3.x: часто есть bot.download(file_id, destination=...)
    if hasattr(bot, "download"):
        try:
            await bot.download(file_id, destination=buf)
            return buf.getvalue()
        except TypeError:
            # сигнатура может отличаться
            pass
        except Exception as exc:
            logger.debug("bot.download failed: %s", exc)

    # fallback: get_file + download_file
    try:
        f = await bot.get_file(file_id)
        buf = io.BytesIO()
        if hasattr(bot, "download_file"):
            await bot.download_file(f.file_path, destination=buf)
            return buf.getvalue()
    except Exception as exc:
        logger.debug("download via get_file/download_file failed: %s", exc)

    return None


def _normalize_text(text: str) -> str:
    text = (text or "").strip()
    text = " ".join(text.split())
    return text


def _ocr_sync(image_bytes: bytes, lang: str) -> str:
    """
    Синхронный OCR (гоняем в отдельном потоке через asyncio.to_thread).
    """
    from PIL import Image
    import pytesseract

    img = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(img, lang=lang)


async def ocr_image_file_id(
    bot: Bot,
    file_id: str,
    *,
    lang: str = "rus+eng",
    timeout_seconds: int = 12,
) -> Tuple[Optional[str], Optional[str]]:
    """
    OCR по telegram file_id.
    Возвращает (text, error_code). error_code=None если ок.
    """
    data = await _download_file_bytes(bot, file_id)
    if not data:
        return None, "download_failed"

    try:
        task = asyncio.to_thread(_ocr_sync, data, lang)
        raw_text = await asyncio.wait_for(task, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return None, "ocr_timeout"
    except Exception as exc:
        logger.warning("OCR failed: %s", exc, exc_info=True)
        return None, "ocr_failed"

    text = _normalize_text(raw_text)
    if not text:
        return None, "empty"

    return text, None
