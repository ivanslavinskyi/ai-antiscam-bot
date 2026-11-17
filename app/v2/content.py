"""Extract only the publication; quoted messages remain separate context."""

import asyncio
import io
import tempfile
import unicodedata
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.v2.llm import CheckError
from app.v2.message_context import reply_context, source_info, text_links


def normalize(value):
    # Do not substitute Latin x with Cyrillic/uppercase or alter URL spelling.
    value = unicodedata.normalize("NFKC", value)
    return "".join(
        c for c in value if c in "\n\t" or unicodedata.category(c) not in {"Cf", "Cc"}
    )


def extract(message):
    own = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    links = text_links(own, entities)
    context = reply_context(message)
    quoted = (context["text"] or context["quote"]) if context else ""
    photo = message.photo[-1] if message.photo else None
    unsupported = any(
        getattr(message, name, None) is not None
        for name in (
            "video",
            "animation",
            "voice",
            "audio",
            "video_note",
            "sticker",
            "poll",
            "location",
            "contact",
        )
    )
    if message.document:
        if (message.document.mime_type or "").startswith("image/"):
            photo = message.document
        else:
            unsupported = True
    return {
        "own": own,
        "entities": [
            entity.model_dump(mode="json", exclude_none=True) for entity in entities
        ],
        "links": links,
        "quoted_context": quoted[:2000],
        "reply_context": context,
        "forwarded": message.forward_origin is not None,
        "forward_source": source_info(message.forward_origin)
        if message.forward_origin
        else None,
        "image_file_id": photo.file_id if photo else None,
        "image_size": photo.file_size if photo else None,
        "image_is_document": bool(photo and message.document),
        "unsupported": unsupported,
    }


class OCR:
    def __init__(
        self,
        bot,
        concurrency=1,
        executable="tesseract",
        *,
        max_bytes=10 * 1024 * 1024,
        max_pixels=20_000_000,
        timeout=12,
    ):
        self.bot, self.executable = bot, executable
        self.limit = asyncio.Semaphore(concurrency)
        self.max_bytes, self.max_pixels, self.timeout = max_bytes, max_pixels, timeout

    async def extract(self, file_id, declared_size=None):
        if declared_size and declared_size > self.max_bytes:
            raise CheckError("image_size_limit")
        async with self.limit:
            max_bytes = self.max_bytes

            class LimitedBuffer(io.BytesIO):
                def write(self, data):
                    if self.tell() + len(data) > max_bytes:
                        raise CheckError("image_size_limit")
                    return super().write(data)

            buffer = LimitedBuffer()
            try:
                async with asyncio.timeout(self.timeout):
                    await self.bot.download(file_id, destination=buffer)
            except TimeoutError as exc:
                raise CheckError("image_download_timeout") from exc
            data = buffer.getvalue()

            def validate():
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width * image.height > self.max_pixels:
                            raise CheckError("image_pixel_limit")
                        image.verify()
                except (
                    UnidentifiedImageError,
                    OSError,
                    Image.DecompressionBombError,
                ) as exc:
                    raise CheckError("invalid_image") from exc

            await asyncio.to_thread(validate)
            with tempfile.TemporaryDirectory(prefix="antiscam-ocr-") as tmp:
                path = Path(tmp) / "input"
                await asyncio.to_thread(path.write_bytes, data)
                try:
                    process = await asyncio.create_subprocess_exec(
                        self.executable,
                        str(path),
                        "stdout",
                        "-l",
                        "rus+ukr+eng",
                        "--psm",
                        "3",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    raise CheckError("ocr_unavailable") from exc
                try:
                    stdout, _ = await asyncio.wait_for(
                        process.communicate(), self.timeout
                    )
                except (TimeoutError, asyncio.CancelledError) as exc:
                    if process.returncode is None:
                        process.kill()
                    await process.communicate()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise CheckError("ocr_timeout")
                if process.returncode:
                    raise CheckError("ocr_failed")
                return stdout.decode("utf-8", errors="replace").strip()
