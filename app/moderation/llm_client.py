import json
import logging
from typing import Optional

import httpx

from app.config import settings
from .schemas import LlmModerationResult

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
Ты модератор телеграм-чата украинских беженцев в Швейцарии.

По тексту одного сообщения определи, является ли оно мошенническим (скамом) или нет.

Скамом считаются, в частности:
- обещания лёгкого или очень высокого заработка без квалификации/языка;
- предложения работы без указания реального работодателя/компании и легального контракта;
- крипто- и инвестиционные схемы, торговля USDT и т.п. от неизвестных лиц;
- пирамиды, MLM, "сетевой бизнес";
- схемы, где зовут писать в личку или на внешний канал для "заработка", "инвестиций", "быстрых денег";
- фишинговые ссылки, сбор конфиденциальных данных.

Обычные вопросы, бытовое общение, новости, обсуждения и честные вакансии с понятным работодателем НЕ являются скамом.

Ответ верни строго в формате JSON БЕЗ лишнего текста:

{
  "label": "SCAM" или "OK",
  "category": "job_scam" | "crypto" | "investment" | "phishing" | "other" | "none",
  "confidence": число от 0 до 1,
  "reason": "краткое объяснение на русском"
}
""".strip()


async def classify_text(text: str) -> Optional[LlmModerationResult]:
    """
    Отправляет текст в OpenAI и возвращает результат модерации.
    При ошибке возвращает None.
    """
    payload = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 256,
    }

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
            )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("HTTP error during LLM call: %s", exc, exc_info=True)
        return None

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        logger.error("Unexpected LLM response structure: %s; data=%r", exc, data)
        return None

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM JSON content: %r", content)
        return None

    try:
        result = LlmModerationResult(**parsed, raw_response=data)
    except Exception as exc:
        logger.error("Failed to validate LLM result: %s; parsed=%r", exc, parsed)
        return None

    return result
