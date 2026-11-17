"""Bounded, attributed reply context. Never fetch external chats or URLs."""


def text_links(text, entities):
    raw = text.encode("utf-16-le")
    links = []
    for entity in entities or []:
        if entity.type == "text_link" and entity.url:
            links.append(entity.url)
        elif entity.type in {"url", "email"}:
            links.append(
                raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode(
                    "utf-16-le", errors="replace"
                )
            )
    return list(dict.fromkeys(links))


def source_info(origin=None, chat=None, message_id=None):
    # These names are supplied by Telegram users, not verified organizations.
    source = {}
    if origin:
        source["origin_type"] = str(origin.type)
        chat = chat or getattr(origin, "chat", None)
        message_id = message_id or getattr(origin, "message_id", None)
    if chat:
        source.update(chat_id=chat.id, title=(chat.title or "")[:160])
        if chat.username:
            source["username"] = chat.username[:64]
    if message_id is not None:
        source["message_id"] = message_id
    return source


def reply_context(message):
    reply, external, quote = (
        message.reply_to_message,
        message.external_reply,
        message.quote,
    )
    if not (reply or external or quote):
        return None
    full = (reply.text or reply.caption or "") if reply else ""
    quoted = quote.text if quote else ""
    entities = (reply.entities or reply.caption_entities or []) if reply else []
    links = text_links(full, entities)
    media = reply or external
    has_media = bool(
        media
        and any(
            getattr(media, k, None)
            for k in (
                "photo",
                "document",
                "video",
                "animation",
                "audio",
                "voice",
                "video_note",
                "sticker",
            )
        )
    )
    source = (
        source_info(external.origin, external.chat, external.message_id)
        if external
        else source_info(chat=reply.chat, message_id=reply.message_id)
        if reply
        else {}
    )
    truncated = (
        len(full) > 2000
        or len(quoted) > 1000
        or len(links) > 10
        or any(len(x) > 1000 for x in links)
    )
    # A supplied quote is usable, explicitly partial context. Do not invent the rest.
    media_unread = has_media and not quoted.strip()
    return {
        "kind": "external_reply" if external else "reply" if reply else "quote",
        "source": source,
        "text": full[:2000],
        "quote": quoted[:1000],
        "links": [x[:1000] for x in links[:10]],
        "coverage": "full"
        if full.strip()
        else "quote_only"
        if quoted.strip()
        else "missing",
        "truncated": truncated,
        "media_unread": media_unread,
        "incomplete": truncated or media_unread or not (full.strip() or quoted.strip()),
    }


def context_lines(content):
    """Same context for initially delivered and reopened moderation cards."""
    context = content.get("reply_context")
    lines = []
    if content.get("forwarded"):
        title = (content.get("forward_source") or {}).get("title")
        lines.append("📨 Пересланное сообщение" + (": " + title if title else ""))
    if not context:
        old = content.get("quoted_context")
        if old:
            lines.append("↪️ Контекст ответа: " + old[:350])
        return lines
    if context.get("incomplete"):
        lines.append("ℹ️ Контекст неполный — нужно решение модератора.")
    elif context.get("coverage") == "quote_only":
        lines.append("ℹ️ Доступна только выбранная цитата.")
    source = context.get("source", {})
    if source.get("title"):
        lines.append("📎 Источник ответа: " + source["title"][:160])
    text, quote = context.get("text", ""), context.get("quote", "")
    if text:
        lines.append("↪️ Ответ на: " + text[:350])
    if quote and quote != text:
        lines.append("💬 Выбранная цитата: " + quote[:200])
    if context.get("links"):
        lines.append("🔗 Ссылка в исходнике: " + context["links"][0][:180])
    return lines
