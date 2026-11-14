import asyncio
import io
import shutil
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.v2.content import OCR
from app.v2.llm import CheckError

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("tesseract"),
    reason="Native OCR acceptance runs on Linux with Tesseract",
)


def fixture_image():
    image = Image.new("RGB", (1200, 240), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
    draw = ImageDraw.Draw(image)
    for y, line in [
        (10, "Ordinary community discussion"),
        (80, "Обычное сообщение о встрече"),
        (150, "Звичайне повідомлення про зустріч"),
    ]:
        draw.text((15, y), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ImageBot:
    async def download(self, file_id, destination):
        destination.write(fixture_image())


async def test_native_ocr_three_languages():
    value = await OCR(ImageBot()).extract("local-fixture")
    assert "Ordinary" in value
    assert "Обычное" in value
    assert "Звичайне" in value


async def test_native_timeout_kills_child(tmp_path, monkeypatch):
    executable = tmp_path / "slow-ocr"
    executable.write_text(
        f"#!/bin/sh\nexec {sys.executable} -c 'import time; time.sleep(60)'\n"
    )
    executable.chmod(0o700)
    real = asyncio.create_subprocess_exec
    children = []

    async def record(*args, **kwargs):
        child = await real(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    with pytest.raises(CheckError, match="ocr_timeout"):
        await OCR(ImageBot(), executable=str(executable), timeout=0.2).extract(
            "local-fixture"
        )
    assert children and children[0].returncode is not None
