"""
Нормализация текста сообщений: раскрытие кастомных эмодзи (буквы-эмодзи).

Скамеры используют Telegram custom emoji пакеты (например, «Трендовый алфавит ⚡»),
где каждая буква — это кастомный эмодзи. В поле message.text вместо реальных букв
стоят placeholder-эмодзи, а визуальный вид определяется через entities типа
``custom_emoji``.  Этот модуль реконструирует читаемый текст из таких сообщений.
"""

import logging
import re
import struct
import unicodedata
from typing import Optional

from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Unicode-маппинг: «декоративные» символы → обычные ASCII/кириллица
# ---------------------------------------------------------------------------

def _build_unicode_letter_map() -> dict[str, str]:
    """
    Собирает маппинг из декоративных Unicode-блоков в обычные буквы.
    Покрывает: Mathematical Alphanumeric Symbols, Enclosed Alphanumerics,
    Fullwidth Latin, Regional Indicators, и другие распространённые
    «шрифтовые» замены.
    """
    mapping: dict[str, str] = {}

    # --- Mathematical Alphanumeric Symbols (U+1D400..U+1D7FF) ---
    # Различные стили: Bold, Italic, Bold Italic, Script, Fraktur,
    # Double-Struck, Sans-Serif, Monospace и т.д.
    # Каждый стиль содержит A-Z (26 букв) и a-z (26 букв).
    math_alpha_ranges = [
        # (start_upper, start_lower, name)
        (0x1D400, 0x1D41A, "Math Bold"),
        (0x1D434, 0x1D44E, "Math Italic"),
        (0x1D468, 0x1D482, "Math Bold Italic"),
        (0x1D49C, 0x1D4B6, "Math Script"),
        (0x1D4D0, 0x1D4EA, "Math Bold Script"),
        (0x1D504, 0x1D51E, "Math Fraktur"),
        (0x1D538, 0x1D552, "Math Double-Struck"),
        (0x1D56C, 0x1D586, "Math Bold Fraktur"),
        (0x1D5A0, 0x1D5BA, "Math Sans-Serif"),
        (0x1D5D4, 0x1D5EE, "Math Sans-Serif Bold"),
        (0x1D608, 0x1D622, "Math Sans-Serif Italic"),
        (0x1D63C, 0x1D656, "Math Sans-Serif Bold Italic"),
        (0x1D670, 0x1D68A, "Math Monospace"),
    ]
    for upper_start, lower_start, _name in math_alpha_ranges:
        for i in range(26):
            mapping[chr(upper_start + i)] = chr(ord('A') + i)
            mapping[chr(lower_start + i)] = chr(ord('a') + i)

    # Math Bold Digits (𝟎-𝟗)
    for i in range(10):
        mapping[chr(0x1D7CE + i)] = str(i)   # Bold
        mapping[chr(0x1D7D8 + i)] = str(i)   # Double-Struck
        mapping[chr(0x1D7E2 + i)] = str(i)   # Sans-Serif
        mapping[chr(0x1D7EC + i)] = str(i)   # Sans-Serif Bold
        mapping[chr(0x1D7F6 + i)] = str(i)   # Monospace

    # --- Fullwidth Latin (Ａ-Ｚ, ａ-ｚ, ０-９) ---
    for i in range(26):
        mapping[chr(0xFF21 + i)] = chr(ord('A') + i)  # Ａ..Ｚ
        mapping[chr(0xFF41 + i)] = chr(ord('a') + i)  # ａ..ｚ
    for i in range(10):
        mapping[chr(0xFF10 + i)] = str(i)              # ０..９

    # --- Enclosed Alphanumeric Supplement ---
    # Circled Latin Capital (🅐-🅩, U+1F150..U+1F169)
    for i in range(26):
        mapping[chr(0x1F150 + i)] = chr(ord('A') + i)
    # Negative Circled Latin Capital (🅰-🆉, U+1F170..U+1F189)
    for i in range(26):
        mapping[chr(0x1F170 + i)] = chr(ord('A') + i)
    # Squared Latin Capital (🅰-🆉 variant, U+1F130..U+1F149)
    for i in range(26):
        mapping[chr(0x1F130 + i)] = chr(ord('A') + i)

    # --- Regional Indicator Symbols (🇦-🇿, U+1F1E6..U+1F1FF) ---
    # Одиночные regional indicators — буквы A-Z
    for i in range(26):
        mapping[chr(0x1F1E6 + i)] = chr(ord('A') + i)

    # --- Circled numbers and letters (Ⓐ-Ⓩ, ⓐ-ⓩ, ①-⑳) ---
    for i in range(26):
        mapping[chr(0x24B6 + i)] = chr(ord('A') + i)   # Ⓐ..Ⓩ
        mapping[chr(0x24D0 + i)] = chr(ord('a') + i)   # ⓐ..ⓩ

    # --- Superscript / Subscript common ---
    mapping['⁰'] = '0'
    mapping['¹'] = '1'
    mapping['²'] = '2'
    mapping['³'] = '3'
    mapping['⁴'] = '4'
    mapping['⁵'] = '5'
    mapping['⁶'] = '6'
    mapping['⁷'] = '7'
    mapping['⁸'] = '8'
    mapping['⁹'] = '9'

    # --- Small caps and other letter-like symbols ---
    small_caps = {
        'ᴀ': 'A', 'ʙ': 'B', 'ᴄ': 'C', 'ᴅ': 'D', 'ᴇ': 'E', 'ꜰ': 'F',
        'ɢ': 'G', 'ʜ': 'H', 'ɪ': 'I', 'ᴊ': 'J', 'ᴋ': 'K', 'ʟ': 'L',
        'ᴍ': 'M', 'ɴ': 'N', 'ᴏ': 'O', 'ᴘ': 'P', 'ǫ': 'Q', 'ʀ': 'R',
        'ꜱ': 'S', 'ᴛ': 'T', 'ᴜ': 'U', 'ᴠ': 'V', 'ᴡ': 'W', 'x': 'X',
        'ʏ': 'Y', 'ᴢ': 'Z',
    }
    mapping.update(small_caps)

    return mapping


UNICODE_LETTER_MAP = _build_unicode_letter_map()

# Regex: один или несколько символов из нашего маппинга подряд
_MAPPED_CHARS_RE = re.compile(
    '[' + re.escape(''.join(UNICODE_LETTER_MAP.keys())) + ']+',
)


def normalize_unicode_fonts(text: str) -> str:
    """
    Заменяет декоративные Unicode-буквы (Mathematical Bold, Fullwidth,
    Enclosed Alphanumerics и т.д.) на обычные ASCII-эквиваленты.
    """
    if not text:
        return text
    chars = list(text)
    changed = False
    for i, ch in enumerate(chars):
        replacement = UNICODE_LETTER_MAP.get(ch)
        if replacement is not None:
            chars[i] = replacement
            changed = True
    if changed:
        return ''.join(chars)
    return text


# ---------------------------------------------------------------------------
#  Реконструкция текста из кастомных эмодзи (Telegram custom_emoji entities)
# ---------------------------------------------------------------------------

async def _resolve_custom_emoji_stickers(
    bot: Bot,
    custom_emoji_ids: list[str],
) -> dict[str, str]:
    """
    Для каждого custom_emoji_id получает emoji alt-text через Telegram API.
    Возвращает маппинг {custom_emoji_id: emoji_alt_text}.
    Батчит запросы по 200 (лимит Telegram API).
    """
    result: dict[str, str] = {}
    batch_size = 200  # Telegram API limit

    unique_ids = list(set(custom_emoji_ids))

    for start in range(0, len(unique_ids), batch_size):
        batch = unique_ids[start:start + batch_size]
        try:
            stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=batch)
            for sticker in stickers:
                if sticker.custom_emoji_id and sticker.emoji:
                    result[sticker.custom_emoji_id] = sticker.emoji
        except Exception as exc:
            logger.warning(
                "Failed to resolve custom emoji stickers (batch %d-%d): %s",
                start, start + len(batch), exc,
            )

    return result


def _extract_entities(message: Message) -> list:
    """Извлекает entities из text или caption."""
    if message.entities:
        return list(message.entities)
    if message.caption_entities:
        return list(message.caption_entities)
    return []


def _get_full_text(message: Message) -> str:
    """Получает текст сообщения (text или caption)."""
    return (message.text or message.caption or "")


async def resolve_custom_emoji_text(
    bot: Bot,
    message: Message,
) -> Optional[str]:
    """
    Если сообщение содержит custom_emoji entities, реконструирует текст,
    заменяя placeholder-эмодзи на alt-text из стикеров.

    Возвращает:
    - Реконструированный текст, если были custom_emoji и удалось распознать
    - None, если custom_emoji не найдены или реконструкция не изменила текст
    """
    entities = _extract_entities(message)
    if not entities:
        return None

    # Собираем все custom_emoji entities
    custom_emoji_entities = [
        e for e in entities
        if e.type == "custom_emoji" and e.custom_emoji_id
    ]

    if not custom_emoji_entities:
        return None

    full_text = _get_full_text(message)
    if not full_text:
        return None

    logger.info(
        "Message contains %d custom_emoji entities, resolving...",
        len(custom_emoji_entities),
    )

    # Получаем alt-text для каждого кастомного эмодзи
    emoji_ids = [e.custom_emoji_id for e in custom_emoji_entities]
    emoji_map = await _resolve_custom_emoji_stickers(bot, emoji_ids)

    if not emoji_map:
        logger.warning("Could not resolve any custom emoji stickers")
        return None

    # Реконструируем текст: заменяем placeholder-эмодзи на alt-text
    # Telegram entities используют UTF-16 offset/length, но aiogram
    # корректно конвертирует в Python-строковые позиции через extract_from.
    # Мы собираем замены в обратном порядке (от конца к началу),
    # чтобы позиции не сдвигались.

    # Конвертируем текст в список UTF-16 code units для корректных offset/length
    encoded = full_text.encode('utf-16-le')
    utf16_units = len(encoded) // 2

    # Собираем замены: (offset, length, replacement_text)
    replacements: list[tuple[int, int, str]] = []
    resolved_count = 0

    for entity in custom_emoji_entities:
        emoji_alt = emoji_map.get(entity.custom_emoji_id)
        if emoji_alt:
            replacements.append((entity.offset, entity.length, emoji_alt))
            resolved_count += 1

    if not replacements:
        return None

    logger.info(
        "Resolved %d/%d custom emoji, reconstructing text",
        resolved_count, len(custom_emoji_entities),
    )

    # Сортируем по offset в обратном порядке для корректной замены
    replacements.sort(key=lambda r: r[0], reverse=True)

    # Работаем с UTF-16 представлением, чтобы offset/length были корректны
    # Конвертируем в список 16-bit units
    utf16_list = list(struct.unpack(f'<{utf16_units}H', encoded))

    for offset, length, replacement in replacements:
        # Конвертируем replacement в UTF-16 units
        repl_encoded = replacement.encode('utf-16-le')
        repl_units = list(struct.unpack(f'<{len(repl_encoded) // 2}H', repl_encoded))
        utf16_list[offset:offset + length] = repl_units

    # Собираем обратно строку
    result_bytes = struct.pack(f'<{len(utf16_list)}H', *utf16_list)
    result_text = result_bytes.decode('utf-16-le')

    # Дополнительно нормализуем Unicode-шрифты в alt-text
    result_text = normalize_unicode_fonts(result_text)

    logger.info(
        "Reconstructed text from custom emoji: %r",
        result_text[:300],
    )

    return result_text


async def normalize_message_text(
    bot: Bot,
    message: Message,
) -> tuple[str, bool]:
    """
    Основная точка входа для нормализации текста сообщения.

    Выполняет:
    1. Реконструкцию текста из custom_emoji entities
    2. Нормализацию декоративных Unicode-шрифтов

    Возвращает:
    - (нормализованный_текст, was_normalized)
    - was_normalized=True если текст был изменён
    """
    raw_text = _get_full_text(message)

    # Шаг 1: Пробуем реконструировать текст из custom_emoji
    reconstructed = await resolve_custom_emoji_text(bot, message)
    if reconstructed is not None:
        # Текст был реконструирован из custom emoji
        return reconstructed.strip(), True

    # Шаг 2: Нормализуем Unicode-шрифты в обычном тексте
    normalized = normalize_unicode_fonts(raw_text)
    if normalized != raw_text:
        return normalized.strip(), True

    return raw_text.strip(), False
